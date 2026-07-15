# step-ca GCE VM.
#
# Why GCE and not Cloud Run: step-ca needs persistent state (the CA database, intermediate
# private key, audit logs). Cloud Run's filesystem is ephemeral and a managed disk on GCE
# is the right home for CA state.
#
# IMPORTANT — manual one-time steps after first apply (intentionally not in TF):
#
#   1. CA bootstrap. SSH in (via IAP), run:
#        sudo -u step bash
#        export STEPPATH=/home/step/.step
#        mkdir -p $STEPPATH/secrets && chmod 700 $STEPPATH/secrets
#        openssl rand -base64 32 | tr -d '\n' > $STEPPATH/secrets/password
#        openssl rand -base64 32 | tr -d '\n' > $STEPPATH/secrets/provisioner-password
#        chmod 600 $STEPPATH/secrets/*
#        step ca init --name "Mozilla RelOps Bootstrap CA" --dns step-ca.relops.mozilla \
#                     --dns <static-ip> --address ":443" --provisioner admin@mozilla.com \
#                     --password-file $STEPPATH/secrets/password \
#                     --provisioner-password-file $STEPPATH/secrets/provisioner-password \
#                     --deployment-type standalone
#        exit  # back to admin
#        sudo systemctl enable --now step-ca
#      Then push the root cert to Secret Manager:
#        sudo cat /home/step/.step/certs/root_ca.crt | \
#          gcloud secrets versions add step-ca-root-cert --data-file=-
#
#   2. Add SCEP provisioner (one per puppet role; each provisioner has its own
#      challenge + x509 template that injects the role-specific SPIFFE SAN):
#        CHALLENGE=$(openssl rand -base64 32 | tr -d '\n+/=')
#        sudo -u step step ca provisioner add scep-no-sip --type=SCEP --force-cn \
#          --challenge="$CHALLENGE" --x509-template=/tmp/scep-x509-template.json \
#          --admin-provisioner=admin@mozilla.com \
#          --admin-password-file=/home/step/.step/secrets/provisioner-password \
#          --ca-url=https://step-ca.relops.mozilla:443 \
#          --root=/home/step/.step/certs/root_ca.crt
#        sudo systemctl restart step-ca
#        printf "%s" "$CHALLENGE" | \
#          gcloud secrets versions add step-ca-scep-challenge --data-file=-

# Debian 12 boot image, PINNED to a specific version.
#
# Do NOT use `family = "debian-12"` here: a family lookup auto-resolves to the
# latest published image, so every `terraform apply` would see a new image and
# want to REPLACE this VM. The CA keys live on the attached step-ca-state disk
# (below) and survive a replace, but a replace still swaps in a fresh boot disk
# (losing the step-ca service setup) and changes the VM's internal IP — i.e. it
# takes the CA offline. Pinning makes `apply` deterministic. Bump this string
# deliberately (and validate) when you actually want a new base image.
data "google_compute_image" "debian" {
  name    = "debian-12-bookworm-v20260609"
  project = "debian-cloud"
}

# Persistent disk for CA state (root + intermediate keys, ca.json, db, secrets).
# Lives independently of the VM so a VM recreate or boot-disk replacement doesn't
# wipe the CA. prevent_destroy guards against accidental terraform destroy of the
# whole project taking the CA with it.
resource "google_compute_disk" "step_ca_state" {
  name = "step-ca-state"
  type = "pd-balanced"
  zone = var.zone
  size = 10

  labels = {
    purpose = "step-ca-state"
  }

  lifecycle {
    prevent_destroy = true
  }
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

  # State disk. device_name surfaces as /dev/disk/by-id/google-<device_name> inside
  # the VM; startup script picks it up by that path.
  attached_disk {
    source      = google_compute_disk.step_ca_state.id
    device_name = "step-ca-state"
    mode        = "READ_WRITE"
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

    # Install step CLI + step-ca server.
    STEP_VERSION="0.30.6"
    STEP_CA_VERSION="0.30.2"
    cd /tmp
    curl -fsSLO "https://github.com/smallstep/cli/releases/download/v$${STEP_VERSION}/step-cli_$${STEP_VERSION}-1_amd64.deb"
    curl -fsSLO "https://github.com/smallstep/certificates/releases/download/v$${STEP_CA_VERSION}/step-ca_$${STEP_CA_VERSION}-1_amd64.deb"
    dpkg -i step-cli_*.deb step-ca_*.deb
    rm -f /tmp/step-*.deb

    # /etc/hosts entry so `step ca provisioner add` (which calls step-ca's admin
    # API over TLS) validates against the cert's SAN. The cert has step-ca.relops.mozilla
    # in its DNS SAN; localhost isn't there. Map it to 127.0.0.1 for the loopback case.
    grep -q 'step-ca.relops.mozilla' /etc/hosts || \
      echo '127.0.0.1 step-ca.relops.mozilla' >> /etc/hosts

    # step user (idempotent).
    if ! id step >/dev/null 2>&1; then
      useradd --system --create-home --shell /bin/bash step
    fi

    # Persistent disk for CA state. Format on first attach (idempotent); mount at
    # /home/step so step user's home + .step dir live on the persistent disk.
    DISK_DEV=/dev/disk/by-id/google-step-ca-state
    MOUNT_POINT=/home/step

    # Wait briefly for the disk node to appear post-boot.
    for _ in $(seq 1 30); do
      [ -e "$DISK_DEV" ] && break
      sleep 2
    done

    if [ -e "$DISK_DEV" ]; then
      # Format with ext4 + label on first attach (mkfs is a no-op if filesystem already there).
      if ! blkid "$DISK_DEV" >/dev/null 2>&1; then
        mkfs.ext4 -F -L step-ca-state "$DISK_DEV"
      fi

      # fstab entry (idempotent).
      if ! grep -q 'step-ca-state' /etc/fstab; then
        echo 'LABEL=step-ca-state /home/step ext4 defaults,nofail 0 2' >> /etc/fstab
      fi

      # Mount if not already mounted. On first attach the mount point is empty;
      # subsequent boots find existing CA state on the disk.
      if ! mountpoint -q "$MOUNT_POINT"; then
        mkdir -p "$MOUNT_POINT"
        mount "$MOUNT_POINT"
        chown step:step "$MOUNT_POINT"
      fi
    fi

    # systemd unit for step-ca.
    cat > /etc/systemd/system/step-ca.service <<'UNIT'
    [Unit]
    Description=step-ca
    After=network-online.target home-step.mount
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
    # Don't auto-start; operator runs `step ca init` first, then enables the unit.
  EOT

  depends_on = [
    google_project_service.apis,
    google_compute_subnetwork.step_ca,
  ]

  allow_stopping_for_update = true
}
