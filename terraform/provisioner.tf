# Cloud Run service + Cloud Scheduler + Secret Manager + GCS state bucket
# for the relops-provisioner polling reconciler.
#
# This service is independent of vault-broker — separate IAM, separate
# service account, separate secrets. The provisioner SA can call SimpleMDM
# (via its API token) and write the GCS rate-limit state; nothing else
# in the project.
#
# Default deployment posture is dry-run + empty allowlist. Two operator
# actions are required to actually fire any script-job:
#   1. Populate the simplemdm-api-token secret with a real token
#   2. Add hostnames to the provisioner-allowlist secret
#   3. Set provisioner-enabled secret to "true"
#   4. Flip the Cloud Run env var DRY_RUN=false (via terraform.tfvars)
# Until all four are done, the service is harmless.

resource "google_service_account" "provisioner_run" {
  account_id   = "relops-provisioner-run"
  display_name = "relops-provisioner Cloud Run service"
  description  = "Polls SimpleMDM and fires script-jobs for cold workers, gated by 8 safety guards."
}

# ----- Secrets the provisioner reads -----

resource "google_secret_manager_secret" "simplemdm_api_token" {
  secret_id = "simplemdm-api-token"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret" "provisioner_allowlist" {
  secret_id = "provisioner-allowlist"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret" "provisioner_enabled" {
  secret_id = "provisioner-enabled"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

# Read access for the provisioner SA, narrow to these three secrets only.
resource "google_secret_manager_secret_iam_member" "provisioner_token" {
  secret_id = google_secret_manager_secret.simplemdm_api_token.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.provisioner_run.email}"
}

resource "google_secret_manager_secret_iam_member" "provisioner_allowlist_access" {
  secret_id = google_secret_manager_secret.provisioner_allowlist.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.provisioner_run.email}"
}

resource "google_secret_manager_secret_iam_member" "provisioner_kill_switch_access" {
  secret_id = google_secret_manager_secret.provisioner_enabled.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.provisioner_run.email}"
}

# ----- GCS bucket for per-device rate-limit state -----

resource "google_storage_bucket" "provisioner_state" {
  name                        = "relops-bootstrap-provisioner-state"
  location                    = "US"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false # rate-limit data prevents accidental re-fires; never auto-delete

  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition { age = 30 }
    action { type = "Delete" }
  }
}

resource "google_storage_bucket_iam_member" "provisioner_state_rw" {
  bucket = google_storage_bucket.provisioner_state.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.provisioner_run.email}"
}

# ----- Artifact Registry repo for the provisioner image -----

resource "google_artifact_registry_repository" "provisioner" {
  location      = var.region
  repository_id = "relops-provisioner"
  description   = "Container images for the relops-provisioner Cloud Run service"
  format        = "DOCKER"

  depends_on = [google_project_service.apis]
}

resource "google_artifact_registry_repository_iam_member" "provisioner_run_pull" {
  location   = google_artifact_registry_repository.provisioner.location
  repository = google_artifact_registry_repository.provisioner.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.provisioner_run.email}"
}

# ----- Cloud Run service -----

resource "google_cloud_run_v2_service" "provisioner" {
  name     = "relops-provisioner"
  location = var.region

  # Internal-only ingress — Cloud Scheduler is the only authorized caller.
  ingress = "INGRESS_TRAFFIC_INTERNAL_ONLY"

  lifecycle {
    ignore_changes = [
      template[0].containers[0].image,
      client,
      client_version,
    ]
  }

  template {
    service_account = google_service_account.provisioner_run.email

    scaling {
      min_instance_count = 0
      max_instance_count = 2
    }

    containers {
      image = var.provisioner_image

      resources {
        limits = {
          cpu    = "1"
          memory = "256Mi"
        }
        cpu_idle = true
      }

      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }

      # DRY_RUN defaults to "true" via Pydantic config; setting it
      # explicitly here so it's auditable in terraform state. Flip to
      # "false" via terraform.tfvars after dry-run validation.
      env {
        name  = "DRY_RUN"
        value = var.provisioner_dry_run ? "true" : "false"
      }
    }
  }

  depends_on = [
    google_project_service.apis,
    google_secret_manager_secret.simplemdm_api_token,
    google_secret_manager_secret.provisioner_allowlist,
    google_secret_manager_secret.provisioner_enabled,
    google_storage_bucket.provisioner_state,
  ]
}

# ----- Cloud Scheduler invokes the service every N minutes -----

resource "google_service_account" "provisioner_scheduler" {
  account_id   = "relops-provisioner-cron"
  display_name = "Cloud Scheduler invoker for relops-provisioner"
}

# Allow the scheduler SA to invoke the Cloud Run service.
resource "google_cloud_run_v2_service_iam_member" "scheduler_invoker" {
  project  = google_cloud_run_v2_service.provisioner.project
  location = google_cloud_run_v2_service.provisioner.location
  name     = google_cloud_run_v2_service.provisioner.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.provisioner_scheduler.email}"
}

resource "google_cloud_scheduler_job" "provisioner_tick" {
  name             = "relops-provisioner-tick"
  description      = "Fires the relops-provisioner reconciler every 5 minutes."
  schedule         = "*/5 * * * *"
  time_zone        = "UTC"
  attempt_deadline = "300s"
  region           = var.region

  retry_config {
    retry_count = 0 # don't pile up; next tick will reconcile anyway
  }

  http_target {
    uri         = "${google_cloud_run_v2_service.provisioner.uri}/run"
    http_method = "POST"

    oidc_token {
      service_account_email = google_service_account.provisioner_scheduler.email
      audience              = google_cloud_run_v2_service.provisioner.uri
    }
  }

  depends_on = [google_project_service.apis]
}
