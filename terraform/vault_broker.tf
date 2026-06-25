# Cloud Run service that brokers Secret Manager reads for per-host CI workers.
#
# Auth model (enforced in broker app code, not at the LB):
#   1. Host presents a JWT in the Authorization header
#   2. JWT was signed by the host's SCEP-issued client cert (issued by our step-ca)
#   3. Broker validates JWT signature against the step-ca root cert (bundled in image)
#   4. JWT claims: { sub: "<hostname>", role: "<puppet_role>", aud: <broker_url>, exp: <iat+60s> }
#   5. Broker reads vault-${role} from Secret Manager and returns the YAML content
#
# Defense-in-depth layers:
#   - Cloud Run ingress = INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER (no direct internet access)
#   - LB frontend allowlists trusted_source_cidrs (MDC1 worker network)
#   - Broker app validates the JWT chain
#   - Broker SA can read ONLY vault-* secrets (per-secret IAM binding in iam.tf)
#   - Every secret read is logged to Cloud Audit Logs with the requesting cert's SAN

resource "google_service_account" "vault_broker_run" {
  account_id   = "vault-broker-run"
  display_name = "vault-broker Cloud Run service"
  description  = "Serves per-role vault.yaml content to CI workers authenticating with SCEP-issued client certs"
}

resource "google_cloud_run_v2_service" "vault_broker" {
  name     = "vault-broker"
  location = var.region

  # Internal-only ingress; external hosts reach it via the GCP HTTPS load balancer
  # (with the source-CIDR allowlist on the frontend).
  ingress = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"

  lifecycle {
    ignore_changes = [
      # Image is managed by Cloud Build / manual deploy after first apply.
      template[0].containers[0].image,
      client,
      client_version,
    ]
  }

  template {
    service_account = google_service_account.vault_broker_run.email

    scaling {
      min_instance_count = var.vault_broker_min_instances
      max_instance_count = var.vault_broker_max_instances
    }

    containers {
      image = var.vault_broker_image

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle = true
      }

      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }

      # The broker code reads this at startup and uses it to validate
      # the chain of incoming JWT-signing certs. The actual cert content is added as
      # a Secret Manager secret post-CA-bootstrap so we don't bake the root into the image.
      env {
        name  = "STEP_CA_ROOT_CERT_SECRET"
        value = "step-ca-root-cert"
      }

      env {
        name  = "LOG_JSON"
        value = "true"
      }
    }
  }

  depends_on = [
    google_project_service.apis,
    google_secret_manager_secret.vault,
  ]
}

# Allow unauthenticated invocations at the Cloud Run layer — auth happens in the broker app
# (JWT validation against the SCEP root cert). The LB frontend's source-CIDR allowlist is the
# outer fence; the in-app JWT check is the inner fence.
#
# Note: "unauthenticated" here means "no Google IAM bearer token required to call the URL."
# Hosts still MUST present a valid JWT signed by their SCEP cert or the broker returns 401.
resource "google_cloud_run_v2_service_iam_member" "vault_broker_public" {
  project  = google_cloud_run_v2_service.vault_broker.project
  location = google_cloud_run_v2_service.vault_broker.location
  name     = google_cloud_run_v2_service.vault_broker.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# A separate Secret Manager container holds the step-ca root cert (PEM).
# Populated after step-ca is bootstrapped:
#   gcloud secrets versions add step-ca-root-cert --data-file=root_ca.pem
resource "google_secret_manager_secret" "step_ca_root_cert" {
  secret_id = "step-ca-root-cert"

  replication {
    auto {}
  }

  labels = {
    purpose = "step-ca-root-cert-pem"
  }

  depends_on = [google_project_service.apis]
}

# SCEP shared challenge — the value the SimpleMDM SCEP Custom Profile sends to authenticate
# enrollment requests. Stored here so the orchestrator / operators can retrieve it when
# configuring the MDM profile without keeping it on disk in 1Password too.
resource "google_secret_manager_secret" "step_ca_scep_challenge" {
  secret_id = "step-ca-scep-challenge"

  replication {
    auto {}
  }

  labels = {
    purpose = "step-ca-scep-challenge"
  }

  depends_on = [google_project_service.apis]
}
