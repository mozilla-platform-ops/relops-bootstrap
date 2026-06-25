# Least-privilege IAM for the vault-broker service account.
#
# The broker SA gets `roles/secretmanager.secretAccessor` ONLY on individual
# vault-${role} secrets, NEVER project-wide. If the broker is compromised, the
# attacker can't pivot to any other secrets in the project.

resource "google_secret_manager_secret_iam_member" "broker_vault_accessor" {
  for_each = google_secret_manager_secret.vault

  project   = each.value.project
  secret_id = each.value.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.vault_broker_run.email}"
}

# Broker needs to read the step-ca root cert to validate incoming JWTs.
resource "google_secret_manager_secret_iam_member" "broker_step_ca_root_accessor" {
  project   = google_secret_manager_secret.step_ca_root_cert.project
  secret_id = google_secret_manager_secret.step_ca_root_cert.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.vault_broker_run.email}"
}

# Broker writes its own access logs to Cloud Logging (audit trail of vault.yaml reads).
resource "google_project_iam_member" "broker_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.vault_broker_run.email}"
}

# step-ca VM service account — separate identity, no secret access at all.
# Only writes logs and (optionally) reads its own CA password from Secret Manager.
resource "google_service_account" "step_ca_vm" {
  account_id   = "step-ca-vm"
  display_name = "step-ca GCE VM"
  description  = "Runs the step-ca server that issues SCEP certs to CI workers"
}

resource "google_project_iam_member" "step_ca_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.step_ca_vm.email}"
}

resource "google_project_iam_member" "step_ca_metric_writer" {
  project = var.project_id
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${google_service_account.step_ca_vm.email}"
}

# Custom Cloud Build service account. Modern gcloud defaults `gcloud builds submit`
# to the project's compute default SA which has no perms in this project (Mozilla
# org-policy disables auto-editor on default SAs). Using a dedicated SA keeps
# Cloud Build's grant set explicit and least-privilege.
resource "google_service_account" "cloudbuild_run" {
  account_id   = "cloudbuild-run"
  display_name = "Cloud Build runner"
  description  = "Runs Cloud Build jobs that build + push the vault-broker image and deploy Cloud Run"
}

# Cloud Build needs to read source uploads, write logs.
resource "google_project_iam_member" "cloudbuild_run_builder" {
  project = var.project_id
  role    = "roles/cloudbuild.builds.builder"
  member  = "serviceAccount:${google_service_account.cloudbuild_run.email}"
}

# Push images to AR.
resource "google_artifact_registry_repository_iam_member" "cloudbuild_run_ar_writer" {
  location   = google_artifact_registry_repository.broker.location
  repository = google_artifact_registry_repository.broker.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.cloudbuild_run.email}"
}

# Deploy new revisions to Cloud Run.
resource "google_cloud_run_v2_service_iam_member" "cloudbuild_run_deployer" {
  project  = google_cloud_run_v2_service.vault_broker.project
  location = google_cloud_run_v2_service.vault_broker.location
  name     = google_cloud_run_v2_service.vault_broker.name
  role     = "roles/run.admin"
  member   = "serviceAccount:${google_service_account.cloudbuild_run.email}"
}

# Cloud Build acts-as vault-broker-run when deploying so the new revision keeps the
# right runtime identity.
resource "google_service_account_iam_member" "cloudbuild_run_act_as_broker" {
  service_account_id = google_service_account.vault_broker_run.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.cloudbuild_run.email}"
}
