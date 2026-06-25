# vault.yaml auto-fetch block for the SimpleMDM bootstrap script-job.
#
# Drop this between the "Wait for SecureToken on admin" block and the existing
# "Wait for /var/root/vault.yaml" loop in m4-simplemdm-bootstrap-sip-safari.sh
# (and analogous scripts for other roles). The existing wait-loop will then
# find the file already present and continue without hand-dropping.
#
# Requirements:
#   - /etc/puppet_role file present (delivered by MDM Custom Attribute)
#   - Dev - SCEP MDM profile pushed; SCEP cert in /Library/Keychains/System.keychain
#   - host source IP in trusted_source_cidrs at the LB (MDC1 worker network)
#
# Why this works without a LaunchDaemon: the SimpleMDM script-job runs as root
# at enrollment time. curl with CURL_SSL_BACKEND=securetransport uses the
# SCEP-issued keychain identity directly — TLS signing happens inside
# SecureTransport (which IS in the keychain ACL allowlist for sign), so no
# UI prompt, no PKCS12 export, no key on disk.

echo "=== fetch vault.yaml via mTLS ==="

# Read role from MDM-delivered Custom Attribute (existing script already does
# this later; we duplicate here so this block is self-contained)
if [ ! -r /etc/puppet_role ]; then
    echo "Waiting for /etc/puppet_role..."
    for _ in $(seq 1 60); do
        [ -r /etc/puppet_role ] && break
        sleep 10
    done
fi
if [ ! -r /etc/puppet_role ]; then
    echo "ERROR: /etc/puppet_role not delivered. Aborting."
    exit 1
fi
ROLE=$(/usr/bin/tr -d '[:space:]' < /etc/puppet_role)
echo "role: $ROLE"

# Find the SCEP-issued identity by issuer DN. Subject CN comes from
# %ComputerName% at SCEP-enrollment time and isn't stable across re-enrolls;
# issuer DN is.
ISSUER_CN="Mozilla RelOps Bootstrap CA Intermediate CA"
IDENTITY_CN=""
echo "Waiting for SCEP cert (issued by '$ISSUER_CN') in System keychain..."
deadline=$(( $(/bin/date +%s) + 300 ))
while [ "$(/bin/date +%s)" -lt "$deadline" ] && [ -z "$IDENTITY_CN" ]; do
    while IFS= read -r line; do
        if [[ $line =~ \)\ ([0-9A-Fa-f]+)\ \"(.+)\"$ ]]; then
            sha="${BASH_REMATCH[1]}"
            cn="${BASH_REMATCH[2]}"
            if /usr/bin/security find-certificate -Z "$sha" -p /Library/Keychains/System.keychain 2>/dev/null \
                | /usr/bin/openssl x509 -noout -issuer 2>/dev/null \
                | /usr/bin/grep -q "$ISSUER_CN"; then
                IDENTITY_CN="$cn"
                break
            fi
        fi
    done < <(/usr/bin/security find-identity -v /Library/Keychains/System.keychain 2>/dev/null)
    [ -z "$IDENTITY_CN" ] && /bin/sleep 5
done

if [ -z "$IDENTITY_CN" ]; then
    echo "ERROR: no SCEP identity found in keychain after 5min. Aborting."
    exit 1
fi
echo "using identity: '$IDENTITY_CN'"

# Fetch vault.yaml. SecureTransport curl is the load-bearing trick:
# the OS-level network stack has keychain sign access, our shell does not.
TMP_YAML=$(/usr/bin/mktemp /var/root/.vault-yaml.XXXXXX)
HTTP=$(CURL_SSL_BACKEND=securetransport /usr/bin/curl -sS --fail-with-body --max-time 30 \
    --cert "$IDENTITY_CN" \
    -w '%{http_code}' \
    -o "$TMP_YAML" \
    "https://forge.relops.mozilla.com/secret/$ROLE") || true

if [ "$HTTP" != "200" ]; then
    echo "ERROR: broker returned HTTP $HTTP"
    /bin/cat "$TMP_YAML" 2>/dev/null
    /bin/rm -f "$TMP_YAML"
    exit 1
fi

/bin/chmod 0600 "$TMP_YAML"
/usr/sbin/chown root:wheel "$TMP_YAML"
/bin/mv "$TMP_YAML" /var/root/vault.yaml
echo "vault.yaml fetched ($(/usr/bin/wc -c < /var/root/vault.yaml) bytes)"
