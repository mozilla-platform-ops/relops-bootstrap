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

# Cloud Build SA bindings are intentionally NOT in here yet.
#
# The default `<project_num>@cloudbuild.gserviceaccount.com` SA is only auto-created
# on first build, which means terraform can't bind to it before the first build.
# Modern best practice anyway is a custom Cloud Build SA — when we wire up the
# Cloud Build trigger, we'll create that SA explicitly and bind these roles to it.
