#!/bin/bash
# Runs ON the step-ca VM. Generates an RSA-2048 decrypter cert+key for the
# scep-no-sip provisioner and configures the provisioner to use it.
#
# Required because step-ca's intermediate is ECDSA P-256 and Apple SCEP uses
# RSA for PKCS#7 envelope key wrap — without an RSA decrypter, PKIOperation
# requests from Apple's mdmclient fail to decrypt. Symptom you'd see without
# this: step-ca startup log line:
#     "failed validating SCEP authority: SCEP provisioner X does not have a decrypter certificate"
# and Apple's MDM enrollment loops on retries.
#
# Idempotent: skips key/cert generation if files already exist.
#
# Operator-side driver:
#   gcloud compute scp scripts/setup-scep-decrypter.sh step-ca:/tmp/ \
#     --zone=us-central1-a --project=relops-bootstrap --tunnel-through-iap
#   gcloud compute ssh step-ca \
#     --zone=us-central1-a --project=relops-bootstrap --tunnel-through-iap \
#     --command='chmod +x /tmp/setup-scep-decrypter.sh && /tmp/setup-scep-decrypter.sh'

set -euo pipefail

PROVISIONER="${1:-scep-no-sip}"

sudo -u step bash <<STEPUSER
set -euo pipefail
export STEPPATH=/home/step/.step
cd "\$STEPPATH"

CRT="certs/${PROVISIONER}-decrypter.crt"
KEY="secrets/${PROVISIONER}-decrypter.key"

if [ -f "\$CRT" ] && [ -f "\$KEY" ]; then
  echo "=== existing decrypter for ${PROVISIONER}; checking it's NOT marked CA:TRUE ===" >&2
  if openssl x509 -in "\$CRT" -text -noout | grep -q "CA:TRUE"; then
    echo "  ⚠️ existing cert has CA:TRUE — regenerating without it (Apple SCEP rejects CA-flagged certs as encryption candidates)" >&2
    rm -f "\$CRT" "\$KEY"
  fi
fi

if [ ! -f "\$CRT" ] || [ ! -f "\$KEY" ]; then
  echo "=== generating RSA-2048 decrypter keypair + self-signed cert for ${PROVISIONER} ===" >&2
  openssl genrsa -out "\$KEY" 2048 2>/dev/null
  openssl req -new -x509 \\
    -key "\$KEY" \\
    -out "\$CRT" \\
    -days 3650 \\
    -subj "/CN=${PROVISIONER}-decrypter/O=Mozilla RelOps Bootstrap CA" \\
    -addext "basicConstraints=critical,CA:FALSE" \\
    -addext "keyUsage=critical,keyEncipherment,dataEncipherment" \\
    -addext "extendedKeyUsage=emailProtection" 2>/dev/null
  chmod 0600 "\$KEY"
fi

ls -la "\$CRT" "\$KEY"

echo "=== update ${PROVISIONER} provisioner ===" >&2
step ca provisioner update "${PROVISIONER}" \\
  --scep-decrypter-certificate-file="\$STEPPATH/\$CRT" \\
  --scep-decrypter-key-file="\$STEPPATH/\$KEY" \\
  --admin-provisioner=admin@mozilla.com \\
  --admin-password-file="\$STEPPATH/secrets/provisioner-password" \\
  --ca-url=https://step-ca.relops.mozilla:443 \\
  --root="\$STEPPATH/certs/root_ca.crt" 2>&1 | tail -3
STEPUSER

echo "=== restart step-ca to reload config ==="
sudo systemctl restart step-ca
sleep 4
sudo systemctl is-active step-ca

echo "=== startup logs — look for any remaining SCEP authority warning ==="
sudo journalctl -u step-ca --since "1 minute ago" --no-pager 2>&1 \
  | grep -iE "scep|authority" | tail -10 \
  || echo "(no SCEP-related logs in the last minute = good)"
