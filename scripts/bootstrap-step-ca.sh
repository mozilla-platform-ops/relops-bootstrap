#!/bin/bash
# bootstrap-step-ca.sh — initialize the step-ca server + add SCEP provisioners.
#
# Runs ON the step-ca VM. Idempotent: skips CA init if /home/step/.step exists,
# skips SCEP provisioner add if it already exists.
#
# The caller (operator on their laptop) is expected to:
#   1. Generate a random SCEP challenge and SCP it to /tmp/_scep_challenge
#   2. Generate / SCP an x509 cert template to /tmp/scep-x509-template.json
#      (a sample is in scripts/scep-x509-template.json.example)
#   3. SCP this script to /tmp/ and run it
#   4. Capture the root cert PEM from stdout
#   5. Push the cert + challenge to Secret Manager
#
# Why this script lives in-repo and not in terraform's startup script:
#   step ca init requires interactive password generation + handles the CA root key
#   material. We never want terraform to know those passwords. Operators run this
#   under a human session that won't end up in terraform state or CI logs.

set -euo pipefail

if ! command -v step-ca >/dev/null; then
  echo "ERROR: step-ca not installed (the VM startup script should have done that)" >&2
  exit 1
fi

[ -r /tmp/_scep_challenge ]         || { echo "missing /tmp/_scep_challenge"; exit 2; }
[ -r /tmp/scep-x509-template.json ] || { echo "missing /tmp/scep-x509-template.json"; exit 2; }

sudo chown step:step /tmp/_scep_challenge /tmp/scep-x509-template.json

sudo -u step bash <<'STEPUSER'
set -euo pipefail
export STEPPATH=/home/step/.step

if [ ! -d "$STEPPATH/config" ]; then
  echo "=== step ca init ===" >&2
  mkdir -p "$STEPPATH/secrets"
  chmod 0700 "$STEPPATH/secrets"
  openssl rand -base64 32 | tr -d '\n' > "$STEPPATH/secrets/password"
  openssl rand -base64 32 | tr -d '\n' > "$STEPPATH/secrets/provisioner-password"
  chmod 0600 "$STEPPATH/secrets/password" "$STEPPATH/secrets/provisioner-password"

  # --dns values must include every hostname/IP that any caller (incl. step CLI
  # itself when calling the admin API over loopback) will use to reach this CA.
  step ca init \
    --name "Mozilla RelOps Bootstrap CA" \
    --dns "step-ca.relops.mozilla" \
    --dns "$(/bin/hostname -I | awk '{print $1}')" \
    --address ":443" \
    --provisioner "admin@mozilla.com" \
    --password-file "$STEPPATH/secrets/password" \
    --provisioner-password-file "$STEPPATH/secrets/provisioner-password" \
    --deployment-type standalone >&2
fi
STEPUSER

sudo systemctl enable step-ca >/dev/null 2>&1
sudo systemctl restart step-ca
sleep 4
sudo systemctl is-active step-ca >&2

sudo -u step bash <<'STEPUSER'
set -euo pipefail
export STEPPATH=/home/step/.step
CHALLENGE=$(cat /tmp/_scep_challenge)

if step ca provisioner list \
     --ca-url=https://step-ca.relops.mozilla:443 \
     --root="$STEPPATH/certs/root_ca.crt" 2>/dev/null \
   | grep -q '"name": "scep-no-sip"'; then
  echo "scep-no-sip provisioner already configured, skipping add" >&2
else
  echo "=== adding scep-no-sip SCEP provisioner ===" >&2
  step ca provisioner add scep-no-sip \
    --type=SCEP \
    --force-cn \
    --challenge="$CHALLENGE" \
    --x509-template=/tmp/scep-x509-template.json \
    --admin-provisioner=admin@mozilla.com \
    --admin-password-file="$STEPPATH/secrets/provisioner-password" \
    --ca-url=https://step-ca.relops.mozilla:443 \
    --root="$STEPPATH/certs/root_ca.crt" >&2
fi
STEPUSER

# Reload step-ca so the new provisioner takes effect.
sudo systemctl restart step-ca
sleep 4
sudo systemctl is-active step-ca >&2

# Emit root cert PEM on stdout (caller captures).
sudo cat /home/step/.step/certs/root_ca.crt

# Wipe the temp files — caller is responsible for re-uploading the challenge to
# Secret Manager from their own host.
sudo rm -f /tmp/_scep_challenge /tmp/scep-x509-template.json
