#!/bin/bash
# mint-scep-cert.sh — operator-side SCEP enrollment helper for Linux workers.
#
# Mints a step-ca-issued client cert for <hostname> bound to <puppet-role>:
#   1. Reads the SCEP challenge from Secret Manager via the operator's
#      gcloud auth (this is the gate — only operators with read access
#      to step-ca-scep-challenge can mint certs).
#   2. Generates an RSA-2048 keypair + CSR with challengePassword.
#   3. Runs `sscep enroll` against the per-role SCEP provisioner on step-ca
#      (e.g. /scep/scep-gecko-t-linux-2404-talos for that role) through the
#      forge.relops.mozilla.com HTTPS LB.
#
# Output: <output-dir>/cert.pem + <output-dir>/key.pem (0600).
# The matching per-role provisioner stamps a SPIFFE URI into the cert SAN
# binding the cert to that role; the broker enforces the role match later.
#
# Usage:
#   scripts/mint-scep-cert.sh <hostname> <puppet-role> <output-dir>
#
# e.g. scripts/mint-scep-cert.sh worker-01.relops.mozops.net \
#        gecko_t_linux_2404_talos /tmp/relops-cert-out
#
# Prerequisites on the operator's laptop:
#   - sscep   (brew install sscep / apt install sscep)
#   - openssl (preinstalled most places)
#   - gcloud  (authed against the relops-bootstrap project)

set -euo pipefail

if [ "$#" -ne 3 ]; then
    echo "usage: $0 <hostname> <puppet-role> <output-dir>" >&2
    exit 2
fi
HOST=$1
ROLE=$2
OUTDIR=$3

GCP_PROJECT="${GCP_PROJECT:-relops-bootstrap}"
BROKER_HOST="${BROKER_HOST:-forge.relops.mozilla.com}"

# Per-role provisioner name: underscores → hyphens, prefixed `scep-` (Phase-1 convention).
PROV_NAME="scep-${ROLE//_/-}"
SCEP_URL="https://${BROKER_HOST}/scep/${PROV_NAME}"

for tool in sscep openssl gcloud; do
    command -v "$tool" >/dev/null || {
        echo "ERROR: $tool not on PATH (sscep: brew/apt install sscep)" >&2
        exit 3
    }
done

mkdir -p "$OUTDIR"
chmod 0700 "$OUTDIR"

CHALLENGE=$(gcloud secrets versions access latest \
    --secret=step-ca-scep-challenge \
    --project="$GCP_PROJECT") || {
    echo "ERROR: couldn't read step-ca-scep-challenge — gcloud auth + project?" >&2
    exit 4
}

KEY_PATH="$OUTDIR/key.pem"
CERT_PATH="$OUTDIR/cert.pem"
CSR_PATH=$(mktemp)
CA_PATH=$(mktemp)
CONFIG_PATH=$(mktemp)
# shellcheck disable=SC2064
trap "rm -f '$CSR_PATH' '$CA_PATH' '$CONFIG_PATH'" EXIT

cat > "$CONFIG_PATH" <<EOF
[req]
prompt = no
distinguished_name = dn
attributes = scep_attrs

[dn]
O = Mozilla
OU = RelOps
CN = ${HOST}

[scep_attrs]
challengePassword = ${CHALLENGE}
EOF

openssl req -new -newkey rsa:2048 -nodes \
    -config "$CONFIG_PATH" \
    -keyout "$KEY_PATH" \
    -out "$CSR_PATH" 2>/dev/null
chmod 0600 "$KEY_PATH"

# sscep getca → fetch the SCEP server's CA chain (used to verify the SCEP
# response signature). step-ca returns root + intermediate.
sscep getca -u "$SCEP_URL" -c "$CA_PATH" >/dev/null 2>&1 || {
    echo "ERROR: sscep getca failed against $SCEP_URL" >&2
    exit 5
}

# sscep enroll → POST the CSR, get a signed cert back.
#   -E aes        : symmetric algorithm for SCEP message encryption
#   -S sha256     : signature algorithm
#   -t 1 -n 3     : poll once, max 3 attempts (sane defaults for synchronous CA)
if ! sscep enroll \
        -u "$SCEP_URL" \
        -c "$CA_PATH" \
        -k "$KEY_PATH" \
        -r "$CSR_PATH" \
        -l "$CERT_PATH" \
        -E aes -S sha256 -t 1 -n 3 2>/dev/null; then
    echo "ERROR: sscep enroll failed against $SCEP_URL" >&2
    exit 6
fi
chmod 0600 "$CERT_PATH"

echo "minted: $CERT_PATH (CN=$HOST, role=$ROLE, expires $(openssl x509 -in "$CERT_PATH" -noout -enddate | sed 's/notAfter=//'))" >&2
