#!/bin/bash
# install-on-worker.sh — drop the relops-bootstrap LaunchDaemon onto a worker
# from the operator's laptop and trigger it once.
#
# Run from a checkout of the relops-bootstrap repo. Expects ssh+sudo access
# to admin@<hostname> (your existing operator key).
#
# Usage:
#   scripts/install-on-worker.sh <hostname> <role>
#
# e.g. scripts/install-on-worker.sh macmini-m4-81.test.releng.mdc1.mozilla.com gecko_t_osx_1500_m4_no_sip
#
# Idempotent: re-runs overwrite the files and re-kickstart the daemon.

set -euo pipefail

if [ "$#" -ne 2 ]; then
    echo "usage: $0 <hostname> <puppet-role>" >&2
    exit 2
fi
HOST=$1
ROLE=$2

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
SCRIPT_SRC="$REPO_ROOT/scripts/relops-bootstrap.sh"
PLIST_SRC="$REPO_ROOT/mdm/com.mozilla.relops.bootstrap.plist"

for f in "$SCRIPT_SRC" "$PLIST_SRC"; do
    [ -r "$f" ] || { echo "missing: $f" >&2; exit 3; }
done

SSH_OPTS=(-o IdentitiesOnly=yes -o ConnectTimeout=10)

# Stage the files in /tmp first, then move into place via sudo
scp "${SSH_OPTS[@]}" "$SCRIPT_SRC" "admin@$HOST:/tmp/relops-bootstrap.sh"
scp "${SSH_OPTS[@]}" "$PLIST_SRC" "admin@$HOST:/tmp/com.mozilla.relops.bootstrap.plist"

ssh "${SSH_OPTS[@]}" "admin@$HOST" "ROLE='$ROLE' sudo bash" <<'REMOTE'
set -euo pipefail

install -m 0755 -o root -g wheel /tmp/relops-bootstrap.sh /usr/local/bin/relops-bootstrap.sh
install -m 0644 -o root -g wheel /tmp/com.mozilla.relops.bootstrap.plist /Library/LaunchDaemons/com.mozilla.relops.bootstrap.plist
rm /tmp/relops-bootstrap.sh /tmp/com.mozilla.relops.bootstrap.plist

# Role marker (script reads this to know what to ask for)
printf '%s\n' "$ROLE" > /var/root/.relops-role
chmod 0600 /var/root/.relops-role
chown root:wheel /var/root/.relops-role

# (Re-)load the LaunchDaemon. bootstrap fails if already loaded; that's fine.
launchctl bootstrap system /Library/LaunchDaemons/com.mozilla.relops.bootstrap.plist 2>/dev/null || true
launchctl kickstart -k system/com.mozilla.relops.bootstrap

# Wait up to 6 minutes for the script to finish writing vault.yaml. The
# inner script itself waits 5min for SCEP enrollment.
deadline=$(( $(date +%s) + 360 ))
while [ "$(date +%s)" -lt "$deadline" ]; do
    state=$(launchctl print system/com.mozilla.relops.bootstrap 2>/dev/null \
        | awk '/^[[:space:]]*state =/ {print $3}')
    if [ "$state" = "not" ] && [ -s /var/root/vault.yaml ]; then
        echo "vault.yaml present ($(wc -c < /var/root/vault.yaml) bytes)"
        exit 0
    fi
    sleep 5
done

echo "ERROR: vault.yaml didn't appear within 6min"
echo "--- /var/log/relops-bootstrap.log (last 30 lines) ---"
tail -30 /var/log/relops-bootstrap.log 2>/dev/null
exit 4
REMOTE

echo "$HOST: relops-bootstrap installed + vault.yaml fetched (role=$ROLE)"
