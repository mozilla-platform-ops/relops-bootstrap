# Minimal network for the step-ca VM.
# Custom VPC (no auto-mode subnets) so we control the CIDR layout explicitly.
# step-ca lives in its own subnet so we can firewall it from the rest of the project later.

resource "google_compute_network" "bootstrap" {
  name                    = "bootstrap-vpc"
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"

  depends_on = [google_project_service.apis]
}

resource "google_compute_subnetwork" "step_ca" {
  name                     = "step-ca-subnet"
  ip_cidr_range            = "10.10.0.0/24"
  region                   = var.region
  network                  = google_compute_network.bootstrap.id
  private_ip_google_access = true
}

# step-ca exposes its SCEP / ACME endpoints over HTTPS.
# Inbound from MDC1 worker network only — locked down via the same allowlist the broker uses.
# Outbound: needed for software updates + sending logs to GCP services.

resource "google_compute_firewall" "step_ca_inbound" {
  name    = "allow-step-ca-inbound"
  network = google_compute_network.bootstrap.name

  description = "HTTPS to step-ca from MDC1 worker network only"

  allow {
    protocol = "tcp"
    ports    = ["443", "8443"] # 8443 is the step-ca default; 443 if we front it
  }

  source_ranges = length(var.trusted_source_cidrs) > 0 ? var.trusted_source_cidrs : ["127.0.0.1/32"]
  target_tags   = ["step-ca"]
}

# Reserve a static external IP so the SCEP server URL doesn't rotate on VM restart.
# This goes into the SimpleMDM SCEP Custom Profile URL field — needs to be stable.
resource "google_compute_address" "step_ca" {
  name         = "step-ca-ip"
  region       = var.region
  address_type = "EXTERNAL"
}
