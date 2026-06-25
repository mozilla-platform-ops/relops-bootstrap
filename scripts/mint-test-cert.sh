#!/bin/bash
# mint-test-cert.sh — mint a test client cert from step-ca with the SPIFFE URI SAN
# that the broker expects. Cert + key are emitted to /tmp/test.crt and /tmp/test.key
# on the operator's laptop.
#
# Usage:
#   scripts/mint-test-cert.sh <hostname> [role]
#     hostname: cert CN (e.g., test-broker-client)
#     role:     puppet role for the SPIFFE URI SAN (default: gecko_t_osx_1500_m4_no_sip)
#
# This mints via the `admin@mozilla.com` JWK provisioner (NOT SCEP) so we can pass
# the SPIFFE URI explicitly. Apple's mdmclient gets the same kind of cert via the
# SCEP flow because the scep-no-sip provisioner's template injects the same SAN.

set -euo pipefail

HOST="${1:?usage: $0 <hostname> [role]}"
ROLE="${2:-gecko_t_osx_1500_m4_no_sip}"
SPIFFE="spiffe://relops.mozilla/host/${HOST}/role/${ROLE}"

OUT_DIR=/tmp
CRT="$OUT_DIR/test.crt"
KEY="$OUT_DIR/test.key"
INT="$OUT_DIR/intermediates.crt"

# Use a remote tmp on the VM to avoid sending the admin password locally.
gcloud compute ssh step-ca --zone=us-central1-a --project=relops-bootstrap --tunnel-through-iap \
  --command="set -euo pipefail
sudo -u step bash <<STEPUSER
set -euo pipefail
export STEPPATH=/home/step/.step
rm -f /tmp/_minted.crt /tmp/_minted.key /tmp/_minted.int

step ca certificate '${HOST}' /tmp/_minted.crt /tmp/_minted.key \\
  --san '${SPIFFE}' \\
  --provisioner=admin@mozilla.com \\
  --provisioner-password-file=\\\$STEPPATH/secrets/provisioner-password \\
  --not-after 1h \\
  --ca-url=https://step-ca.relops.mozilla:443 \\
  --root=\\\$STEPPATH/certs/root_ca.crt 2>&1 | tail -3 >&2

# Also export the intermediate so the broker's chain validation works.
cp \\\$STEPPATH/certs/intermediate_ca.crt /tmp/_minted.int
STEPUSER
sudo chmod 0644 /tmp/_minted.crt /tmp/_minted.key /tmp/_minted.int
" 2>&1 | grep -v "^Warning:" | tail -5

# Pull the artifacts down.
gcloud compute scp step-ca:/tmp/_minted.crt step-ca:/tmp/_minted.key step-ca:/tmp/_minted.int \
  "$OUT_DIR/" --zone=us-central1-a --project=relops-bootstrap --tunnel-through-iap 2>/dev/null

mv "$OUT_DIR/_minted.crt" "$CRT"
mv "$OUT_DIR/_minted.key" "$KEY"
mv "$OUT_DIR/_minted.int" "$INT"
chmod 0600 "$KEY"

# Cleanup remote.
gcloud compute ssh step-ca --zone=us-central1-a --project=relops-bootstrap --tunnel-through-iap \
  --command="sudo rm -f /tmp/_minted.crt /tmp/_minted.key /tmp/_minted.int" 2>/dev/null

echo "minted:"
echo "  cert:          $CRT"
echo "  key:           $KEY"
echo "  intermediates: $INT"
echo
echo "cert SAN check:"
openssl x509 -in "$CRT" -text -noout | grep -A2 "Subject Alternative Name"
