#!/bin/bash
# relops-bootstrap.sh — extract SCEP identity, fetch vault.yaml via mTLS.
#
# Installed as /usr/local/bin/relops-bootstrap.sh on each worker. Invoked by
# /Library/LaunchDaemons/com.mozilla.relops.bootstrap.plist at boot and once
# per day. Reads role from /var/root/.relops-role (one-line file with the
# Puppet role name, e.g. gecko_t_osx_1500_m4_no_sip).
#
# Idempotent. Safe to re-run; only refreshes vault.yaml if broker returns 200.

set -euo pipefail

LOGFILE=/var/log/relops-bootstrap.log
WORKDIR=/var/root/.relops-bootstrap
ROLE_FILE=/var/root/.relops-role
VAULT_OUT=/var/root/vault.yaml
BROKER_HOST=forge.relops.mozilla.com
IDENTITY_LABEL="${IDENTITY_LABEL:-Mac mini}"

# All output to /var/log so launchd's stdout/stderr capture has the same.
exec >>"$LOGFILE" 2>&1
echo "=== $(date -u '+%Y-%m-%dT%H:%M:%SZ') relops-bootstrap starting ==="

# 1. Role discovery — Puppet writes this; manual bootstrap can `echo <role> > $ROLE_FILE`
if [ ! -r "$ROLE_FILE" ]; then
    echo "ERROR: missing $ROLE_FILE; can't determine role"
    exit 2
fi
ROLE=$(tr -d '[:space:]' < "$ROLE_FILE")
echo "role: $ROLE"

mkdir -p "$WORKDIR"
chmod 0700 "$WORKDIR"
chown root:wheel "$WORKDIR"

CERT_PEM="$WORKDIR/cert.pem"
KEY_PEM="$WORKDIR/key.pem"
INTERMEDIATE_PEM="$WORKDIR/intermediate.pem"
FULLCHAIN_PEM="$WORKDIR/fullchain.pem"

# 2. Wait for SCEP enrollment if MDM is still applying the profile. On a fresh
#    boot, mdmclient runs concurrently with us and the keychain may not have
#    the cert yet. Poll for up to 5 minutes.
WAIT_DEADLINE=$(( $(date +%s) + 300 ))
while ! security find-identity -v /Library/Keychains/System.keychain 2>/dev/null | grep -q "\"${IDENTITY_LABEL}\""; do
    if [ "$(date +%s)" -ge "$WAIT_DEADLINE" ]; then
        echo "ERROR: identity '${IDENTITY_LABEL}' not in System keychain after 5min"
        exit 3
    fi
    sleep 5
done
echo "identity '$IDENTITY_LABEL' present"

# 3. Extract identity to PKCS12 via the swift helper. Bash variable holds the
#    one-shot passphrase; never written to disk.
PKCS12_PATH="$WORKDIR/identity.p12"
P12_PASS=$(openssl rand -hex 24)

if ! /usr/bin/swift /usr/local/lib/relops-bootstrap/extract-identity.swift \
        "$IDENTITY_LABEL" "$PKCS12_PATH" "$P12_PASS" >/dev/null; then
    echo "ERROR: extract-identity.swift failed"
    exit 4
fi

# 4. Split PKCS12 → PEM cert + PEM key. -nodes keeps the key unencrypted on
#    disk (perms-protected at /var/root/.relops-bootstrap/).
openssl pkcs12 -in "$PKCS12_PATH" -clcerts -nokeys -passin "pass:$P12_PASS" -out "$CERT_PEM" 2>/dev/null
openssl pkcs12 -in "$PKCS12_PATH" -nocerts -nodes -passin "pass:$P12_PASS" -out "$KEY_PEM" 2>/dev/null

# Wipe the PKCS12 — the unencrypted PEM key is the steady-state credential
rm -f "$PKCS12_PATH"

# 5. Intermediate CA — for the LB to build the chain. We extract it from the
#    keychain too (mdmclient placed it there via the SCEP profile root payload).
security find-certificate -c "Mozilla RelOps Bootstrap CA Intermediate CA" -p \
    /Library/Keychains/System.keychain > "$INTERMEDIATE_PEM"

cat "$CERT_PEM" "$INTERMEDIATE_PEM" > "$FULLCHAIN_PEM"

chmod 0644 "$CERT_PEM" "$INTERMEDIATE_PEM" "$FULLCHAIN_PEM"
chmod 0600 "$KEY_PEM"
chown root:wheel "$CERT_PEM" "$INTERMEDIATE_PEM" "$FULLCHAIN_PEM" "$KEY_PEM"

# 6. Validate cert is currently valid (not expired). If expired, fail loud
#    so something else (Puppet, paging) can trigger SCEP re-enrollment.
if ! openssl x509 -in "$CERT_PEM" -noout -checkend 60 >/dev/null; then
    echo "ERROR: cert at $CERT_PEM is expired or expires within 60s"
    exit 5
fi

# 7. mTLS fetch of vault.yaml. fail-with-body so 4xx body lands in the log
#    for debug. -sS = silent + show errors. atomic write to /var/root/.
TMP_YAML=$(mktemp /var/root/.vault-yaml.XXXXXX)
trap 'rm -f "$TMP_YAML"' EXIT

HTTP=$(/usr/bin/curl -sS --fail-with-body --max-time 30 \
    --cert "$FULLCHAIN_PEM" --key "$KEY_PEM" \
    -w '%{http_code}' \
    -o "$TMP_YAML" \
    "https://${BROKER_HOST}/secret/${ROLE}") || {
    echo "ERROR: curl exited non-zero"
    cat "$TMP_YAML" 2>/dev/null || true
    exit 6
}

if [ "$HTTP" != "200" ]; then
    echo "ERROR: broker returned HTTP $HTTP"
    cat "$TMP_YAML" 2>/dev/null || true
    exit 7
fi

# Sanity: response must look like YAML, not an HTTP error body
if ! grep -q '^generic_worker:\|^worker:' "$TMP_YAML"; then
    echo "ERROR: response doesn't look like vault.yaml"
    head -20 "$TMP_YAML"
    exit 8
fi

# 8. Atomic move into place
chmod 0600 "$TMP_YAML"
chown root:wheel "$TMP_YAML"
mv "$TMP_YAML" "$VAULT_OUT"
trap - EXIT

echo "vault.yaml refreshed ($(wc -c < "$VAULT_OUT") bytes)"
echo "=== $(date -u '+%Y-%m-%dT%H:%M:%SZ') relops-bootstrap success ==="
