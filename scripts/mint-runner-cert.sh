#!/bin/bash
# mint-runner-cert.sh — mint an mTLS client cert for the Hangar reprovision-runner
# (cert_mode: vault) and print exactly how to install it on the runner host.
#
# This is the STOPGAP path while step-ca is not reachable from MDC1. The durable
# fix is cert_mode: step_renew (auto-rotating, key never leaves the host) — see
# ronin_puppet modules/reprovision_runner/README.md. Use this until then.
#
# Usage:
#   scripts/mint-runner-cert.sh [hostname] [not-after] [role]
#     hostname   cert CN + SPIFFE host  (default: macmini-m4-81)
#     not-after  cert lifetime          (default: 23h)
#     role       puppet role for the SPIFFE URI SAN (default: gecko_t_osx_1500_m4_no_sip)
#
#   NOT_AFTER=23h scripts/mint-runner-cert.sh macmini-m4-81
#
# NOTE ON LIFETIME: every step-ca provisioner currently uses the global 24h cap
# (`step ca provisioner list` → maxTLSCertDuration). So the practical ceiling here
# is ~24h; the default is 23h to stay safely under it. A longer static cert needs a
# CA-policy change (raise the claim or add a dedicated runner provisioner) — but the
# right answer for "stop re-minting daily" is step_renew, which renews within the
# 24h window and so is unaffected by the cap.
#
# Unlike mint-test-cert.sh (a 1h throwaway), this emits a runner-named bundle and the
# copy/paste for /var/root/vault.yaml.

set -euo pipefail

HOST="${1:-macmini-m4-81}"
NOT_AFTER="${NOT_AFTER:-${2:-23h}}"
ROLE="${3:-gecko_t_osx_1500_m4_no_sip}"
SPIFFE="spiffe://relops.mozilla/host/${HOST}/role/${ROLE}"

OUT_DIR="${OUT_DIR:-/tmp}"
CRT="$OUT_DIR/runner-${HOST}.crt"        # leaf
KEY="$OUT_DIR/runner-${HOST}.key"
INT="$OUT_DIR/runner-${HOST}.int"        # intermediate(s)
BUNDLE="$OUT_DIR/runner-${HOST}.bundle.crt"  # leaf + intermediate (what vault.yaml wants)

echo "minting runner cert: CN=${HOST}, SAN=${SPIFFE}, not-after=${NOT_AFTER}"

# Mint on the step-ca VM so the provisioner password never leaves it (same pattern
# as mint-test-cert.sh). JWK provisioner lets us set the SPIFFE URI explicitly.
gcloud compute ssh step-ca --zone=us-central1-a --project=relops-bootstrap --tunnel-through-iap \
  --command="set -euo pipefail
sudo -u step bash <<STEPUSER
set -euo pipefail
export STEPPATH=/home/step/.step
rm -f /tmp/_runner.crt /tmp/_runner.key /tmp/_runner.int

step ca certificate '${HOST}' /tmp/_runner.crt /tmp/_runner.key \\
  --san '${SPIFFE}' \\
  --provisioner=admin@mozilla.com \\
  --provisioner-password-file=\\\$STEPPATH/secrets/provisioner-password \\
  --not-after '${NOT_AFTER}' \\
  --ca-url=https://step-ca.relops.mozilla:443 \\
  --root=\\\$STEPPATH/certs/root_ca.crt 2>&1 | tail -3 >&2

cp \\\$STEPPATH/certs/intermediate_ca.crt /tmp/_runner.int
STEPUSER
sudo chmod 0644 /tmp/_runner.crt /tmp/_runner.key /tmp/_runner.int
" 2>&1 | grep -v "^Warning:" | tail -5

gcloud compute scp step-ca:/tmp/_runner.crt step-ca:/tmp/_runner.key step-ca:/tmp/_runner.int \
  "$OUT_DIR/" --zone=us-central1-a --project=relops-bootstrap --tunnel-through-iap 2>/dev/null

mv "$OUT_DIR/_runner.crt" "$CRT"
mv "$OUT_DIR/_runner.key" "$KEY"
mv "$OUT_DIR/_runner.int" "$INT"
chmod 0600 "$KEY"
cat "$CRT" "$INT" > "$BUNDLE"   # runner presents leaf + intermediate

gcloud compute ssh step-ca --zone=us-central1-a --project=relops-bootstrap --tunnel-through-iap \
  --command="sudo rm -f /tmp/_runner.crt /tmp/_runner.key /tmp/_runner.int" 2>/dev/null

NOT_AFTER_STAMP="$(openssl x509 -in "$CRT" -noout -enddate | cut -d= -f2)"

cat <<EOF

minted:
  bundle (cert+intermediate): $BUNDLE
  key:                        $KEY
  expires:                    $NOT_AFTER_STAMP

SAN check:
$(openssl x509 -in "$CRT" -text -noout | grep -A1 "Subject Alternative Name" | sed 's/^/  /')

Install on the runner host (cert_mode: vault). Put these two into
/var/root/vault.yaml under the reprovision_runner key (NOT committed):

  reprovision_runner:
    client_cert: |
$(sed 's/^/      /' "$BUNDLE")
    client_key: |
$(sed 's/^/      /' "$KEY")

Then on ${HOST} (rewrites the on-disk cert from vault.yaml and kicks the runner):
  ssh admin@${HOST} 'sudo /usr/local/bin/run-puppet.sh'

Reminder: this expires in ~${NOT_AFTER}. The durable fix is cert_mode: step_renew
(needs step-ca reachable from MDC1) — see ronin_puppet reprovision_runner/README.md.
EOF
