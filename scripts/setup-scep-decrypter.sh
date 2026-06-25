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
  echo "=== existing decrypter for ${PROVISIONER}; checking it chains to the intermediate CA ===" >&2
  if openssl verify -CAfile "\$STEPPATH/certs/intermediate_ca.crt" -partial_chain "\$CRT" 2>/dev/null \\
      | grep -q "OK"; then
    echo "  ✅ existing cert is CA-issued — keeping it" >&2
  else
    echo "  ⚠️ existing cert is NOT CA-issued (self-signed or unrelated) — regenerating" >&2
    echo "     Apple SCEP requires the RA encryption cert to chain to the SCEP server CA." >&2
    rm -f "\$CRT" "\$KEY"
  fi
fi

if [ ! -f "\$CRT" ] || [ ! -f "\$KEY" ]; then
  echo "=== generating RSA-2048 decrypter keypair + CA-signed cert for ${PROVISIONER} ===" >&2

  # Decrypt the intermediate CA private key into a tmp file. step-ca stores it
  # encrypted with the CA password.
  INT_KEY_DEC=\$(mktemp)
  trap "shred -u \$INT_KEY_DEC 2>/dev/null || rm -f \$INT_KEY_DEC" EXIT
  openssl pkey \\
    -in "\$STEPPATH/secrets/intermediate_ca_key" \\
    -passin "file:\$STEPPATH/secrets/password" \\
    -out "\$INT_KEY_DEC" 2>/dev/null

  CSR=\$(mktemp)
  EXT=\$(mktemp)
  trap "shred -u \$INT_KEY_DEC \$CSR \$EXT 2>/dev/null || rm -f \$INT_KEY_DEC \$CSR \$EXT" EXIT

  openssl genrsa -out "\$KEY" 2048 2>/dev/null

  # RFC 8894 §2.2.1: the SCEP CA signing cert and RA encryption cert SHALL share
  # an identifying value, typically the subject DN. We use the intermediate's
  # subject DN verbatim so Apple's mdmclient recognizes them as a pair.
  openssl req -new -key "\$KEY" \\
    -out "\$CSR" \\
    -subj "/O=Mozilla RelOps Bootstrap CA/CN=Mozilla RelOps Bootstrap CA Intermediate CA" 2>/dev/null

  cat > "\$EXT" <<EXTEND
basicConstraints = critical, CA:FALSE
keyUsage = critical, keyEncipherment, dataEncipherment
extendedKeyUsage = emailProtection
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid,issuer
EXTEND

  openssl x509 -req -in "\$CSR" \\
    -CA "\$STEPPATH/certs/intermediate_ca.crt" \\
    -CAkey "\$INT_KEY_DEC" \\
    -CAcreateserial \\
    -out "\$CRT" \\
    -days 3650 \\
    -sha256 \\
    -extfile "\$EXT" 2>/dev/null

  chmod 0644 "\$CRT"
  chmod 0600 "\$KEY"

  echo "  ✅ decrypter cert signed by step-ca intermediate" >&2
fi

ls -la "\$CRT" "\$KEY"

echo "=== update ${PROVISIONER} provisioner ===" >&2
# --include-root forces step-ca to return the root cert in the GetCACert
# response. Some SCEP clients (Apple's mdmclient included) require the full
# chain to validate before submitting PKIOperation.
step ca provisioner update "${PROVISIONER}" \\
  --scep-decrypter-certificate-file="\$STEPPATH/\$CRT" \\
  --scep-decrypter-key-file="\$STEPPATH/\$KEY" \\
  --include-root \\
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
