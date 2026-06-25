# step-ca GCE VM.
#
# Why GCE and not Cloud Run: step-ca needs persistent state (the CA database, intermediate
# private key, audit logs). Cloud Run's filesystem is ephemeral and a managed disk on GCE is
# a cleaner story than mounting a network filesystem into Cloud Run.
#
# IMPORTANT — manual steps that intentionally stay out of Terraform:
#
#   1. Initial CA bootstrap (generates root+intermediate keys, sets the CA password).
#      Done once, on the VM, by an operator after first terraform apply:
#         step ca init --name "Mozilla RelOps Bootstrap CA" --dns step-ca.relops.mozilla \
#                      --address ":443" --provisioner admin@mozilla.com
#      Then copy /home/step/.step/certs/root_ca.crt off the VM and load it as:
#         gcloud secrets versions add step-ca-root-cert --data-file=root_ca.crt
#
#   2. Add a SCEP provisioner:
#         step ca provisioner add scep-relops --type SCEP \
#                      --challenge "<random-challenge>" --force-cn
#      The challenge value is what the SimpleMDM SCEP Custom Profile will use as
#      the shared secret. Store it in 1P + add to a Secret Manager secret so the
#      MDM Custom Profile can read it via the orchestrator (not committed to TF).
#
# Bootstrap is one-time; subsequent renewals are automatic via SCEP.

# Latest Debian 12 image — auto-resolved by family name, no manual version pinning.
data "google_compute_image" "debian" {
  family  = "debian-12"
  project = "debian-cloud"
}

resource "google_compute_instance" "step_ca" {
  name         = "step-ca"
  machine_type = var.step_ca_machine_type
  zone         = var.zone
  tags         = ["step-ca"]

  boot_disk {
    initialize_params {
      image = data.google_compute_image.debian.self_link
      size  = 20
      type  = "pd-balanced"
    }
  }

  network_interface {
    subnetwork = google_compute_subnetwork.step_ca.id

    access_config {
      nat_ip = google_compute_address.step_ca.address
    }
  }

  service_account {
    email = google_service_account.step_ca_vm.email
    scopes = [
      "https://www.googleapis.com/auth/logging.write",
      "https://www.googleapis.com/auth/monitoring.write",
    ]
  }

  metadata_startup_script = <<-EOT
    #!/bin/bash
    set -euo pipefail
    apt-get update
    apt-get install -y curl ca-certificates

    # Install step CLI + step-ca server from the official .deb packages.
    # Pin to specific versions (auto-bumped manually after testing). The version-less
    # download URL exists too but pinning protects against silent server-side changes.
    STEP_VERSION="0.30.6"
    STEP_CA_VERSION="0.30.2"
    cd /tmp
    curl -fsSLO "https://github.com/smallstep/cli/releases/download/v$${STEP_VERSION}/step-cli_$${STEP_VERSION}-1_amd64.deb"
    curl -fsSLO "https://github.com/smallstep/certificates/releases/download/v$${STEP_CA_VERSION}/step-ca_$${STEP_CA_VERSION}-1_amd64.deb"
    dpkg -i step-cli_*.deb step-ca_*.deb
    rm -f /tmp/step-*.deb

    # Create the step user. step-ca runs as this user.
    if ! id step >/dev/null 2>&1; then
      useradd --system --create-home --shell /bin/bash step
    fi

    # systemd unit for step-ca — leaves the bootstrap (`step ca init`) to the operator.
    cat > /etc/systemd/system/step-ca.service <<'UNIT'
    [Unit]
    Description=step-ca
    After=network-online.target
    Wants=network-online.target

    [Service]
    User=step
    Group=step
    Environment=STEPPATH=/home/step/.step
    ExecStart=/usr/bin/step-ca /home/step/.step/config/ca.json --password-file=/home/step/.step/secrets/password
    Restart=on-failure
    RestartSec=5
    LimitNOFILE=65536
    AmbientCapabilities=CAP_NET_BIND_SERVICE

    [Install]
    WantedBy=multi-user.target
    UNIT

    systemctl daemon-reload
    # Intentionally NOT starting yet — operator must run `step ca init` first.
    # After that, run: systemctl enable --now step-ca
  EOT

  depends_on = [
    google_project_service.apis,
    google_compute_subnetwork.step_ca,
  ]

  allow_stopping_for_update = true
}
