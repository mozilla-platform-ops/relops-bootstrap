# Cloud Run service that brokers Secret Manager reads for per-host CI workers.
#
# Auth model (mTLS at LB layer, header-forwarded identity to broker):
#   1. Host's URLSession does TLS handshake against forge.relops.mozilla.com
#   2. macOS Network framework presents the SCEP-issued client cert via
#      network-stack helpers (configd, nehelper) that DO have keychain sign
#      access (our own code can't — that's why JWT-signed-by-cert was a dead end)
#   3. LB validates cert chain against the Trust Config (step-ca root +
#      intermediate, see mtls.tf) and injects X-Client-Cert-* headers
#   4. Broker reads X-Client-Cert-SPIFFE, parses host/role from the SPIFFE URI
#   5. Broker reads vault-${role} from Secret Manager, returns YAML content
#
# Defense-in-depth layers:
#   - Cloud Run ingress = INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER (no direct internet)
#   - LB frontend allowlists trusted_source_cidrs (MDC1 worker network)
#   - LB validates client cert chain to step-ca root via Trust Config
#   - Broker reads forwarded SPIFFE URI + does role↔URL match check
#   - Broker SA can read ONLY vault-* secrets (per-secret IAM binding in iam.tf)
#   - Every secret read is logged to Cloud Audit Logs with the cert serial

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

# Allow unauthenticated invocations at the Cloud Run layer — auth happens via mTLS
# at the LB (LB validates client cert chain to step-ca root) plus the broker's
# SPIFFE URI role-match check on the forwarded headers.
#
# Note: "unauthenticated" here means "no Google IAM bearer token required to call the URL."
# Hosts MUST present a valid step-ca-issued client cert during TLS handshake, or
# the broker returns 401 on the X-Client-Cert-* header check.
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
