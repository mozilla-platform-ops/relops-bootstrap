# LB-layer mTLS for vault-broker.
#
# Architecture:
#   - Trust Config defines the accepted client cert anchor (step-ca root) and
#     intermediate CA. The LB walks chains up to root for client cert validation.
#   - Server TLS Policy attaches the Trust Config to the LB with
#     client_validation_mode = ALLOW_INVALID_OR_MISSING_CLIENT_CERT. That mode
#     forwards traffic to backend even WITHOUT a valid client cert; the broker
#     decides whether the path requires a cert. This is necessary because the
#     same LB serves /scep/* (no cert — Apple's mdmclient is the requester) AND
#     /secret/* (requires cert).
#   - Custom request headers on the broker backend service inject cert info
#     (SPIFFE URI, chain-verified flag, presence flag) so the broker can do
#     header-based auth instead of validating JWT signatures itself.
#
# Why not validate cert in the broker (the old JWT-signed-by-cert design)?
# macOS keychain ACL gates SecKeyCreateSignature to network-stack helpers
# (configd, nehelper, NEIKEv2Provider) — not to arbitrary Mozilla-signed
# binaries. mTLS routes through those helpers, so the OS signs during the TLS
# handshake even though our user code can't. The LB validates the cert chain;
# the broker just reads the forwarded SPIFFE URI.

resource "google_project_service" "networksecurity" {
  service            = "networksecurity.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "certificatemanager" {
  service            = "certificatemanager.googleapis.com"
  disable_on_destroy = false
}

# Trust Config lives under Certificate Manager (not Network Security despite
# the naming overlap). The Server TLS Policy below references it by fully
# qualified resource name.
resource "google_certificate_manager_trust_config" "broker" {
  name        = "vault-broker-trust-config"
  location    = "global"
  description = "Step-CA root + intermediate for vault-broker client cert validation"

  trust_stores {
    trust_anchors {
      pem_certificate = file("${path.module}/trust/root.pem")
    }
    intermediate_cas {
      pem_certificate = file("${path.module}/trust/intermediate.pem")
    }
  }

  depends_on = [google_project_service.certificatemanager]
}

resource "google_network_security_server_tls_policy" "broker" {
  name     = "vault-broker-tls-policy"
  location = "global"

  mtls_policy {
    client_validation_mode = "ALLOW_INVALID_OR_MISSING_CLIENT_CERT"
    # Trust config must be referenced by project NUMBER, not project ID — the
    # Network Security API normalizes the field to the number form and the
    # field is immutable, so a project-ID form here forces a delete+recreate
    # of the policy on every plan (and a brief broker mTLS outage during apply).
    client_validation_trust_config = "projects/${data.google_project.project.number}/locations/global/trustConfigs/${google_certificate_manager_trust_config.broker.name}"
  }

  depends_on = [google_project_service.networksecurity]
}
