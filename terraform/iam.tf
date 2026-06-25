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

# Cloud Build can deploy the broker image to Cloud Run (used by CI on commit).
resource "google_project_iam_member" "cloudbuild_run_admin" {
  project = var.project_id
  role    = "roles/run.admin"
  member  = "serviceAccount:${data.google_project.project.number}@cloudbuild.gserviceaccount.com"
}

resource "google_project_iam_member" "cloudbuild_sa_user" {
  project = var.project_id
  role    = "roles/iam.serviceAccountUser"
  member  = "serviceAccount:${data.google_project.project.number}@cloudbuild.gserviceaccount.com"
}
