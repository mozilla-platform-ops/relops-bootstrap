#!/bin/bash
# relops-bootstrap.sh — fetch role-scoped vault.yaml via keychain-backed mTLS.
#
# Installed as /usr/local/bin/relops-bootstrap.sh on each worker. Invoked by
# /Library/LaunchDaemons/com.mozilla.relops.bootstrap.plist at boot and once
# per day. Reads role from /var/root/.relops-role (one-line file with the
# Puppet role name, e.g. gecko_t_osx_1500_m4_no_sip).
#
# How the cert auth works without extracting the key from the keychain:
#   - macOS keychain ACL gates SecKeyCreateSignature to a fixed list of
#     network-stack helpers (configd, nehelper, NEIKEv2Provider).
#   - curl on macOS 15+ ships with both SecureTransport AND LibreSSL backends.
#     Defaults to LibreSSL.
#   - CURL_SSL_BACKEND=securetransport tells curl to use SecureTransport,
#     which is part of the OS network stack and HAS sign access to keychain
#     identities. --cert "<CN>" then looks up the identity by Subject CN.
#
# Net result: TLS handshake signing happens inside SecureTransport (allowed),
# our process never touches the private key, no PKCS12 export needed, no
# private key ever lands on disk.

set -euo pipefail

LOGFILE=/var/log/relops-bootstrap.log
ROLE_FILE=/var/root/.relops-role
VAULT_OUT=/var/root/vault.yaml
BROKER_HOST=forge.relops.mozilla.com
IDENTITY_LABEL="${IDENTITY_LABEL:-Mac mini}"

exec >>"$LOGFILE" 2>&1
echo "=== $(date -u '+%Y-%m-%dT%H:%M:%SZ') relops-bootstrap starting ==="

# 1. Role discovery
if [ ! -r "$ROLE_FILE" ]; then
    echo "ERROR: missing $ROLE_FILE; can't determine role"
    exit 2
fi
ROLE=$(tr -d '[:space:]' < "$ROLE_FILE")
echo "role: $ROLE"

# 2. Wait for SCEP enrollment to complete (mdmclient may still be applying
#    the profile on first boot). Poll for up to 5 minutes.
WAIT_DEADLINE=$(( $(date +%s) + 300 ))
while ! security find-identity -v /Library/Keychains/System.keychain 2>/dev/null | grep -q "\"${IDENTITY_LABEL}\""; do
    if [ "$(date +%s)" -ge "$WAIT_DEADLINE" ]; then
        echo "ERROR: identity '${IDENTITY_LABEL}' not in System keychain after 5min"
        exit 3
    fi
    sleep 5
done
echo "identity '$IDENTITY_LABEL' present"

# 3. Fetch vault.yaml via mTLS. SecureTransport backend reads the cert
#    directly from /Library/Keychains/System.keychain by CN.
TMP_YAML=$(mktemp /var/root/.vault-yaml.XXXXXX)
trap 'rm -f "$TMP_YAML"' EXIT

HTTP=$(CURL_SSL_BACKEND=securetransport /usr/bin/curl -sS --fail-with-body --max-time 30 \
    --cert "$IDENTITY_LABEL" \
    -w '%{http_code}' \
    -o "$TMP_YAML" \
    "https://${BROKER_HOST}/secret/${ROLE}") || {
    echo "ERROR: curl exited non-zero (HTTP=$HTTP)"
    cat "$TMP_YAML" 2>/dev/null || true
    exit 4
}

if [ "$HTTP" != "200" ]; then
    echo "ERROR: broker returned HTTP $HTTP"
    cat "$TMP_YAML" 2>/dev/null || true
    exit 5
fi

# 4. Sanity check the response looks like vault.yaml, not an error body
if ! grep -q '^generic_worker:\|^worker:' "$TMP_YAML"; then
    echo "ERROR: response doesn't look like vault.yaml"
    head -20 "$TMP_YAML"
    exit 6
fi

# 5. Atomic move into place
chmod 0600 "$TMP_YAML"
chown root:wheel "$TMP_YAML"
mv "$TMP_YAML" "$VAULT_OUT"
trap - EXIT

echo "vault.yaml refreshed ($(wc -c < "$VAULT_OUT") bytes)"
echo "=== $(date -u '+%Y-%m-%dT%H:%M:%SZ') relops-bootstrap success ==="
