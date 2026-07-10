#!/bin/bash
# reissue-runner-cert.sh — one command, run on your laptop, to give the Hangar
# reprovision-runner a fresh mTLS cert end-to-end:
#
#   1. mint a new cert from step-ca            (scripts/mint-runner-cert.sh)
#   2. inject ONLY client_cert/client_key into the runner host's /var/root/vault.yaml
#      — round-tripped through a real YAML parser ON the host, with a timestamped
#        backup, so the other secrets and the indentation can't be mangled
#   3. run puppet on the host (pulls latest runner code + installs cert + restarts)
#   4. tail the runner log so you can see it reconnect
#
# Usage:
#   scripts/reissue-runner-cert.sh [hostname] [not-after]
#     hostname   default: macmini-m4-81
#     not-after  default: 23h  (step-ca global cap is 24h; see mint-runner-cert.sh)
#
# Requirements on your laptop: gcloud authed to the relops-bootstrap project (for
# the step-ca mint) and SSH access to admin@<hostname> on the VPN.
#
# This is the STOPGAP path for cert_mode: vault. The durable fix is
# cert_mode: step_renew (relops-bootstrap#28), which removes this chore entirely.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RAW="${1:-macmini-m4-81}"
NOT_AFTER="${2:-23h}"
SSH_USER="${REPROVISION_SSH_USER:-admin}"

# The cert CN/SPIFFE uses the SHORT name; SSH needs the FQDN (short names don't
# resolve off the worker network). Accept either form as the argument.
SHORT="${RAW%%.*}"
if [[ "$RAW" == *.* ]]; then
  SSH_HOST="$RAW"
else
  SSH_HOST="${SHORT}.test.releng.mdc1.mozilla.com"
fi
SSH_HOST="${REPROVISION_SSH_HOST:-$SSH_HOST}"   # override if you use an ssh_config alias
TARGET="${SSH_USER}@${SSH_HOST}"

CRT_LOCAL="/tmp/runner-${SHORT}.bundle.crt"
KEY_LOCAL="/tmp/runner-${SHORT}.key"

say() { printf '\n\033[1;36m▸ %s\033[0m\n' "$*"; }

# ── 1. mint ───────────────────────────────────────────────────────────────────
say "1/4  minting a fresh ${NOT_AFTER} cert for ${SHORT} from step-ca"
NOT_AFTER="$NOT_AFTER" "$HERE/mint-runner-cert.sh" "$SHORT" "$NOT_AFTER" >/dev/null
[[ -s "$CRT_LOCAL" && -s "$KEY_LOCAL" ]] || { echo "mint did not produce $CRT_LOCAL / $KEY_LOCAL" >&2; exit 1; }
echo "   minted → $CRT_LOCAL (+ key)"

# ── 2. stage the cert on the host, then inject it into vault.yaml ──────────────
say "2/4  installing the cert into ${SSH_HOST}:/var/root/vault.yaml (backup + validated)"
REMOTE_TMP="/tmp/reissue-runner-cert.$$"
ssh "$TARGET" "mkdir -p '$REMOTE_TMP' && chmod 700 '$REMOTE_TMP'"
# shellcheck disable=SC2064
trap "ssh '$TARGET' 'rm -rf $REMOTE_TMP' >/dev/null 2>&1 || true" EXIT
scp -q "$CRT_LOCAL" "$TARGET:$REMOTE_TMP/client_cert.pem"
scp -q "$KEY_LOCAL" "$TARGET:$REMOTE_TMP/client_key.pem"

# Edit vault.yaml with puppet's own ruby (always present on a puppet host, has YAML).
# Load → set the two fields under reprovision_runner → dump to a temp → re-parse to
# confirm valid AND that our two values round-tripped → atomically replace, 0600 root:wheel.
# Every other secret in the file is preserved untouched.
ssh "$TARGET" "sudo bash -s" <<REMOTE
set -euo pipefail
VAULT=/var/root/vault.yaml
RB=/opt/puppetlabs/puppet/bin/ruby
[ -x "\$RB" ] || RB=\$(command -v ruby)
[ -x "\$RB" ] || { echo 'no ruby found on host' >&2; exit 1; }
[ -f "\$VAULT" ] || { echo "\$VAULT not found" >&2; exit 1; }

STAMP=\$(date +%Y%m%d-%H%M%S)
cp -p "\$VAULT" "\$VAULT.bak-\$STAMP"

"\$RB" -ryaml -e '
  vault, certf, keyf = ARGV
  data = YAML.load_file(vault)
  raise "vault.yaml is not a mapping" unless data.is_a?(Hash)
  data["reprovision_runner"] ||= {}
  cert = File.read(certf)
  key  = File.read(keyf)
  data["reprovision_runner"]["client_cert"] = cert
  data["reprovision_runner"]["client_key"]  = key
  tmp = vault + ".tmp"
  File.write(tmp, data.to_yaml)
  # re-parse and confirm our values survived before we swap it in
  rt = YAML.load_file(tmp)
  raise "round-trip mismatch (cert)" unless rt.dig("reprovision_runner","client_cert") == cert
  raise "round-trip mismatch (key)"  unless rt.dig("reprovision_runner","client_key")  == key
  File.rename(tmp, vault)
' "\$VAULT" "$REMOTE_TMP/client_cert.pem" "$REMOTE_TMP/client_key.pem"

chown root:wheel "\$VAULT"
chmod 600 "\$VAULT"
echo "   vault.yaml updated (backup: \$VAULT.bak-\$STAMP)"
REMOTE

# ── 3. run puppet (pulls latest runner code + installs cert + restarts runner) ─
say "3/4  running puppet on ${SSH_HOST} (pulls latest code, installs cert, restarts runner)"
ssh "$TARGET" "sudo /usr/local/bin/run-puppet.sh" 2>&1 | tail -20

# ── 4. show the runner reconnecting ───────────────────────────────────────────
say "4/4  runner status (Ctrl-C to stop tailing) — expect 'auth: mTLS cert' with no cert errors"
ssh "$TARGET" "tail -n 15 /var/log/reprovision-runner/runner.err /var/log/reprovision-runner/runner.out 2>/dev/null || true"
echo
echo "done. cert expires in ~${NOT_AFTER} — durable fix tracked in relops-bootstrap#28."
