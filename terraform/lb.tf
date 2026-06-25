# HTTPS Load Balancer in front of the vault-broker Cloud Run service.
#
# Trust model layers (outside-in):
#   1. Cloud Armor allows only trusted_source_cidrs to reach the LB at all.
#   2. LB terminates TLS (cert wired in once a hostname is provisioned).
#   3. Cloud Run accepts only LB ingress (INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER).
#   4. Broker app validates the JWT-signed-by-cert + role check + jti + rate limit.
#
# mTLS at the LB layer is deferred — when we add it, the Server TLS Policy +
# Trust Config will replace the simple google_compute_target_https_proxy below.

# Static external anycast IP for the LB. Reserve it always so DNS can point at
# something stable even before we attach the cert.
resource "google_compute_global_address" "broker" {
  name = "vault-broker-ip"
}

# Serverless NEG — the LB's representation of a Cloud Run service.
resource "google_compute_region_network_endpoint_group" "broker" {
  name                  = "vault-broker-neg"
  network_endpoint_type = "SERVERLESS"
  region                = var.region

  cloud_run {
    service = google_cloud_run_v2_service.vault_broker.name
  }
}

# Cloud Armor policy: source-CIDR allowlist (default-deny if no CIDRs configured).
# Apply to the backend service below.
resource "google_compute_security_policy" "broker" {
  name        = "vault-broker-armor"
  description = "Source-CIDR allowlist for the vault-broker LB"

  # Rule priority 1000: allow trusted CIDRs.
  rule {
    action   = "allow"
    priority = 1000
    description = "Allow from MDC1 worker network"

    match {
      versioned_expr = "SRC_IPS_V1"
      config {
        # Empty trusted_source_cidrs → effective deny: this rule matches a
        # never-real CIDR so nothing gets through. trusted_source_cidrs
        # gets populated via terraform.tfvars once MDC1 ranges are known.
        src_ip_ranges = length(var.trusted_source_cidrs) > 0 ? var.trusted_source_cidrs : ["192.0.2.0/32"]
      }
    }
  }

  # Default rule (required by Cloud Armor): deny everything not matched above.
  rule {
    action   = "deny(403)"
    priority = 2147483647
    description = "Default deny"

    match {
      versioned_expr = "SRC_IPS_V1"
      config {
        src_ip_ranges = ["*"]
      }
    }
  }
}

# Backend service wiring the NEG to Cloud Armor.
resource "google_compute_backend_service" "broker" {
  name                  = "vault-broker-backend"
  protocol              = "HTTPS"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  security_policy       = google_compute_security_policy.broker.id

  backend {
    group = google_compute_region_network_endpoint_group.broker.id
  }

  log_config {
    enable      = true
    sample_rate = 1.0
  }
}

# --- SCEP-through-LB backend (so Apple mdmclient gets a publicly-trusted cert) ---
#
# An Internet NEG with step-ca's external IP as endpoint; LB connects to step-ca
# over HTTPS for backend traffic. Backend cert is step-ca's self-CA-signed (cert
# SAN includes 34.61.3.27 + step-ca.relops.mozilla) — the backend service uses
# its TLS settings to validate against step-ca's root via a Trust Config.

resource "google_compute_global_network_endpoint_group" "step_ca" {
  name                  = "step-ca-neg"
  network_endpoint_type = "INTERNET_IP_PORT"
  default_port          = 443
}

resource "google_compute_global_network_endpoint" "step_ca" {
  global_network_endpoint_group = google_compute_global_network_endpoint_group.step_ca.name
  ip_address                    = google_compute_address.step_ca.address
  port                          = 443
}

# Backend service for SCEP traffic. Same Cloud Armor policy as the broker
# (same allowlist). HTTPS protocol; backend cert validation defers to the
# system trust store + the cert's SAN match on the IP. step-ca's TLS cert
# is signed by step-ca's own CA so chain validation will fail unless we
# also provide a Backend Authentication Config with the step-ca root.
#
# For v1 we accept that the LB skips strict backend cert validation by
# relying on the IP-restricted Internet NEG (only LB → step-ca traffic) and
# Cloud Armor on the front door. A tighter setup would attach a
# Backend Authentication Config with step-ca's root in a Trust Config.
resource "google_compute_backend_service" "step_ca" {
  name                  = "step-ca-backend"
  protocol              = "HTTPS"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  security_policy       = google_compute_security_policy.broker.id
  timeout_sec           = 30

  backend {
    group = google_compute_global_network_endpoint_group.step_ca.id
  }

  log_config {
    enable      = true
    sample_rate = 1.0
  }
}

# URL map: route /scep/* traffic to step-ca, everything else to broker.
resource "google_compute_url_map" "broker" {
  name            = "vault-broker-urlmap"
  default_service = google_compute_backend_service.broker.id

  host_rule {
    hosts        = [var.broker_hostname == "" ? "default.invalid" : var.broker_hostname]
    path_matcher = "main"
  }

  path_matcher {
    name            = "main"
    default_service = google_compute_backend_service.broker.id

    path_rule {
      paths   = ["/scep", "/scep/*"]
      service = google_compute_backend_service.step_ca.id
    }
  }
}

# Google-managed cert. Created only when broker_hostname is set so the cert
# resource doesn't sit in PROVISIONING_FAILED forever waiting for DNS that
# doesn't point anywhere yet.
resource "google_compute_managed_ssl_certificate" "broker" {
  count = var.broker_hostname == "" ? 0 : 1

  name = "vault-broker-cert"

  managed {
    domains = [var.broker_hostname]
  }
}

resource "google_compute_target_https_proxy" "broker" {
  count = var.broker_hostname == "" ? 0 : 1

  name             = "vault-broker-https-proxy"
  url_map          = google_compute_url_map.broker.id
  ssl_certificates = [google_compute_managed_ssl_certificate.broker[0].id]
}

resource "google_compute_global_forwarding_rule" "broker" {
  count = var.broker_hostname == "" ? 0 : 1

  name                  = "vault-broker-https"
  ip_address            = google_compute_global_address.broker.id
  ip_protocol           = "TCP"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  port_range            = "443"
  target                = google_compute_target_https_proxy.broker[0].id
}
