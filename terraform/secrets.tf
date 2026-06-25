# One Secret Manager container per puppet role.
# Terraform creates the containers; values are populated separately:
#
#   gcloud secrets versions add vault-${role} \
#     --project=${project_id} \
#     --data-file=path/to/vault.yaml
#
# That keeps secret values out of terraform state and out of git.
# The corresponding entry in 1Password remains the source of truth; this is the
# GCP-side mirror that the broker reads to serve hosts.

resource "google_secret_manager_secret" "vault" {
  for_each = toset(var.puppet_roles)

  secret_id = "vault-${each.value}"

  replication {
    auto {}
  }

  labels = {
    purpose = "puppet-vault-yaml"
    role    = each.value
  }

  depends_on = [google_project_service.apis]
}
