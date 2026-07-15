# SimpleMDM API token secret.
#
# Retained after the relops-provisioner (cron reconciler) was decommissioned
# in favor of the signed-PKG + Hangar on-network-runner reprovision path. This
# secret container is kept because it is a shared, general-purpose credential:
# the `reprovision` orchestrator can read it (optional override via
# REPROVISION_SIMPLEMDM_API_KEY_REF=simplemdm-api-token; the default source is
# 1Password op://RelOps/SimpleMDM API admin/password), and operators use it as a
# convenient `gcloud secrets versions access` source.
#
# Resource address is unchanged from its former home in provisioner.tf, so
# relocating the block here is a no-op to Terraform state (no destroy/recreate).
resource "google_secret_manager_secret" "simplemdm_api_token" {
  secret_id = "simplemdm-api-token"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}
