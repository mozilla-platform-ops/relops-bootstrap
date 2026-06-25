# Container image registry for the vault-broker service.
# Cloud Build pushes images here; vault-broker Cloud Run pulls from here.

resource "google_artifact_registry_repository" "broker" {
  location      = var.region
  repository_id = "vault-broker"
  description   = "Container images for the vault-broker Cloud Run service"
  format        = "DOCKER"

  depends_on = [google_project_service.apis]
}

# Broker SA can pull images (Cloud Run impersonates this SA when pulling on startup).
resource "google_artifact_registry_repository_iam_member" "broker_run_pull" {
  location   = google_artifact_registry_repository.broker.location
  repository = google_artifact_registry_repository.broker.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.vault_broker_run.email}"
}

# Cloud Build pushes new images on each CI run.
resource "google_artifact_registry_repository_iam_member" "broker_cloudbuild_push" {
  location   = google_artifact_registry_repository.broker.location
  repository = google_artifact_registry_repository.broker.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${data.google_project.project.number}@cloudbuild.gserviceaccount.com"
}
