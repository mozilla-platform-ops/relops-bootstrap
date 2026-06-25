output "vault_broker_url" {
  description = "Cloud Run URL for the vault-broker service. Bootstrap scripts call this."
  value       = google_cloud_run_v2_service.vault_broker.uri
}

output "vault_broker_service_account" {
  description = "Service account email used by the vault-broker Cloud Run service"
  value       = google_service_account.vault_broker_run.email
}

output "step_ca_ip" {
  description = "Static external IP of the step-ca VM. Use this in the SimpleMDM SCEP Custom Profile URL."
  value       = google_compute_address.step_ca.address
}

output "step_ca_vm_name" {
  description = "Compute Engine instance name for the step-ca VM. SSH in with this name for the manual `step ca init` bootstrap step."
  value       = google_compute_instance.step_ca.name
}

output "artifact_registry_repo_url" {
  description = "Artifact Registry URL for pushing the vault-broker image"
  value       = "${google_artifact_registry_repository.broker.location}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.broker.repository_id}"
}

output "secret_ids" {
  description = "Secret Manager secret IDs for each puppet role's vault.yaml"
  value       = [for s in google_secret_manager_secret.vault : s.secret_id]
}
