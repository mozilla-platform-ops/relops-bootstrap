#!/bin/bash
# mint-tart-enrollment-token.sh — mint a single-use step-ca enrollment token for a
# tart VM host, so the host can generate its own key and enroll a FILE-BASED,
# SELF-RENEWING client cert (ronin_puppet modules/macos_step_cert).
#
# WHY A TOKEN AND NOT A CERT
# --------------------------
# Contrast with mint-runner-cert.sh, which mints the cert AND key on the CA and
# copies both to the host. That is a stopgap: the private key exists off-host.
# Here the CA only issues a short-lived *token*. The host uses it once to generate
# its own key locally and request a cert — the key never leaves the host, and the
# token is worthless after use. A short-lived token in vault is far weaker material
# than a long-lived private key would be.
#
# WHY THE TART HOSTS NEED THIS AT ALL
# -----------------------------------
# They already hold an MDM/SCEP cert, but that is a one-shot bootstrap credential:
# no self-renewal, and KeyIsExtractable=false so `step ca renew` cannot touch it.
# tart-run-vm.sh needs an identity at RUNTIME (it fetches the worker vault on every
# VM launch). On 2026-07-27 all of macmini-m4-235..239 held SCEP certs that had
# been expired for nine days. See bootstrap-step-ca.sh's JWK_ROLES block.
#
# The SPIFFE URI SAN the vault-broker authorizes on is stamped by the
# provisioner's x509 template, NOT by this token — so no --san is passed here.
# Verify it landed with the openssl line this script prints at the end.
#
# Usage:
#   scripts/mint-tart-enrollment-token.sh <hostname> [role] [token-lifetime]
#     hostname         cert CN, e.g. macmini-m4-235
#     role             puppet role  (default: gecko_t_osx_1500_m_vms)
#     token-lifetime   how long the token stays usable (default: 1h)
#
#   scripts/mint-tart-enrollment-token.sh macmini-m4-235
#
# The token is printed to stdout. It is a bearer credential until it expires or is
# used: do not paste it into a ticket, a chat log, or a commit.

set -euo pipefail

HOST="${1:?usage: mint-tart-enrollment-token.sh <hostname> [role] [token-lifetime]}"
ROLE="${2:-gecko_t_osx_1500_m_vms}"
LIFETIME="${3:-1h}"

# Provisioner naming mirrors bootstrap-step-ca.sh's JWK_ROLES table: one
# provisioner per puppet role, so a leaked token cannot mint for another role.
PROVISIONER="jwk-tart-${ROLE//_/-}"

echo "minting enrollment token: CN=${HOST}, role=${ROLE}, provisioner=${PROVISIONER}, valid ${LIFETIME}" >&2

TOKEN=$(gcloud compute ssh step-ca --zone=us-central1-a --project=relops-bootstrap --tunnel-through-iap \
  --command="sudo -u step bash -c '
set -euo pipefail
export STEPPATH=/home/step/.step
step ca token \"${HOST}\" \
  --provisioner=\"${PROVISIONER}\" \
  --provisioner-password-file=\$STEPPATH/secrets/jwk-provisioner-password \
  --not-after \"${LIFETIME}\" \
  --ca-url=https://step-ca.relops.mozilla:443 \
  --root=\$STEPPATH/certs/root_ca.crt
'" 2>/dev/null | tr -d '\r' | grep -E '^ey' | tail -1)

if [ -z "${TOKEN}" ]; then
  echo "ERROR: no token returned. Check that provisioner ${PROVISIONER} exists" >&2
  echo "       (step ca provisioner list) and that" >&2
  echo "       /home/step/.step/secrets/jwk-provisioner-password is present on the CA." >&2
  exit 1
fi

cat <<EOF

Enrollment token for ${HOST} (role ${ROLE}), valid ${LIFETIME}:

${TOKEN}

Install it as a TOP-LEVEL key in /var/root/vault.yaml on ${HOST} — top-level, not
nested under \`tart:\`, or hiera's 'first' merge shadows the whole tart hash:

  tart_step_enrollment_token: ${TOKEN}

Then enable the renewing identity in the host's ronin_settings / role data:

  tart:
    step_cert_enabled: true
    cert_source: 'file'

and apply. NOTE: the tart hosts have no /usr/local/bin/run-puppet.sh and no puppet
agent daemon — puppet is operator-applied from the on-host checkout under
/opt/puppet_environments/mozilla-platform-ops/ronin_puppet. Check what ref that
checkout is on before applying; m4-235 was found pinned to the stale feature
branch add-tart-worker-role (2026-07-27), so a plain apply there would NOT include
macos_step_cert at all:

  ssh admin@${HOST} 'sudo git -C /opt/puppet_environments/mozilla-platform-ops/ronin_puppet rev-parse --abbrev-ref HEAD'

To exercise just this module without dragging in every unrelated change since that
pin, stage the module alone and apply it (noop first):

  D=/opt/puppet_environments/mozilla-platform-ops/ronin_puppet
  ssh admin@${HOST} "sudo /opt/puppetlabs/bin/puppet apply --noop \\
    --modulepath=\$D/modules -e \"include macos_step_cert\""

Verify the cert enrolled AND carries the SPIFFE role the broker authorizes on.
Use -text: macOS ships LibreSSL, which does not support \`-ext subjectAltName\` and
prints its help output instead, making the SAN look absent when it is present.

  ssh admin@${HOST} 'sudo openssl x509 -in /etc/step-cert/tart-client.crt \\
    -noout -subject -dates && sudo openssl x509 -in /etc/step-cert/tart-client.crt \\
    -noout -text | grep -A1 "Alternative Name"'

Expect a URI SAN of:
  spiffe://relops.mozilla/host/${HOST}/role/${ROLE}

Then confirm the renew daemon is loaded (it renews at ~2/3 of the 168h lifetime):

  ssh admin@${HOST} 'sudo launchctl print system/com.mozilla.tart-certrenew | head -5'

The token is single-use. Remove it from vault.yaml once the cert exists.
EOF
