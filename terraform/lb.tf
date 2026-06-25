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
# Zonal NEG of type GCE_VM_IP_PORT — connects directly to step-ca's internal
# VPC IP via Google's internal networking. Internet NEG was the wrong choice
# here: Internet NEGs are for non-Google-Cloud backends; using one with a
# GCE VM's external IP makes GCP refuse to route (hairpin-like behavior).

resource "google_compute_network_endpoint_group" "step_ca" {
  name                  = "step-ca-neg"
  network_endpoint_type = "GCE_VM_IP_PORT"
  network               = google_compute_network.bootstrap.id
  subnetwork            = google_compute_subnetwork.step_ca.id
  zone                  = var.zone
  default_port          = 443
}

resource "google_compute_network_endpoint" "step_ca" {
  network_endpoint_group = google_compute_network_endpoint_group.step_ca.name
  zone                   = var.zone
  instance               = google_compute_instance.step_ca.name
  ip_address             = google_compute_instance.step_ca.network_interface[0].network_ip
  port                   = 443
}

# Health check for the zonal NEG (required, unlike serverless NEG). Probes
# step-ca's /health endpoint. Health checks come from Google's standard
# probe ranges — already allowed in the step-ca firewall rule.
resource "google_compute_health_check" "step_ca" {
  name                = "step-ca-health"
  check_interval_sec  = 30
  timeout_sec         = 10
  healthy_threshold   = 1
  unhealthy_threshold = 3

  https_health_check {
    port         = 443
    request_path = "/health"
    # step-ca's /health returns plain "ok"; cert validation is N/A for the
    # health probe path because GCP doesn't verify backend cert chains on
    # health checks against private IPs.
  }
}

resource "google_compute_backend_service" "step_ca" {
  name                  = "step-ca-backend"
  protocol              = "HTTPS"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  security_policy       = google_compute_security_policy.broker.id
  timeout_sec           = 30
  health_checks         = [google_compute_health_check.step_ca.id]

  backend {
    group           = google_compute_network_endpoint_group.step_ca.id
    balancing_mode  = "RATE"
    max_rate_per_endpoint = 100
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
      paths   = ["/scep", "/scep/*", "/acme", "/acme/*"]
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
