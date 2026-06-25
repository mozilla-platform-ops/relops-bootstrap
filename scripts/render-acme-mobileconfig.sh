#!/bin/bash
# render-acme-mobileconfig.sh — produce a SimpleMDM-uploadable .mobileconfig
# from mdm/acme-relops.mobileconfig.template.
#
# ACME enrollment puts the resulting cert + key in macOS's modern
# data-protection keychain (unlike SCEP, which leaves the key in a legacy/modern
# hybrid state that's hard to use from non-interactive contexts). If this
# experiment pans out, ACME replaces SCEP as the enrollment mechanism.
#
# Usage: scripts/render-acme-mobileconfig.sh [/path/to/output]
# Env overrides:
#   ACME_DIRECTORY_URL=https://forge.relops.mozilla.com/acme/acme-no-sip/directory

set -euo pipefail

GCP_PROJECT="${GCP_PROJECT:-relops-bootstrap}"
ACME_DIRECTORY_URL="${ACME_DIRECTORY_URL:-https://forge.relops.mozilla.com/acme/acme-no-sip/directory}"
OUT_PATH="${1:-/tmp/acme-relops.mobileconfig}"
TEMPLATE="$(dirname "$0")/../mdm/acme-relops.mobileconfig.template"

[ -r "$TEMPLATE" ] || { echo "ERROR: template not readable at $TEMPLATE" >&2; exit 2; }

umask 077

ROOTCA_PEM_FILE=$(mktemp)
ROOTCA_DER_FILE=$(mktemp)
trap 'rm -f "$ROOTCA_PEM_FILE" "$ROOTCA_DER_FILE"' EXIT

if ! gcloud secrets versions access latest \
       --secret=step-ca-root-cert \
       --project="$GCP_PROJECT" > "$ROOTCA_PEM_FILE" 2>/dev/null; then
  echo "ERROR: couldn't read step-ca-root-cert from project $GCP_PROJECT" >&2
  exit 3
fi

openssl x509 -in "$ROOTCA_PEM_FILE" -outform DER -out "$ROOTCA_DER_FILE"
ROOTCA_DER_BASE64=$(base64 < "$ROOTCA_DER_FILE" | tr -d '\n')

if ! command -v uuidgen >/dev/null; then
  echo "ERROR: uuidgen not on PATH" >&2; exit 4
fi

ACME_PAYLOAD_UUID=$(uuidgen)
ROOTCA_PAYLOAD_UUID=$(uuidgen)
PROFILE_UUID=$(uuidgen)

sed \
  -e "s|{{ACME_DIRECTORY_URL}}|${ACME_DIRECTORY_URL}|" \
  -e "s|{{COMMON_NAME}}|%ComputerName%|" \
  -e "s|{{ACME_PAYLOAD_UUID}}|${ACME_PAYLOAD_UUID}|" \
  -e "s|{{ROOTCA_PAYLOAD_UUID}}|${ROOTCA_PAYLOAD_UUID}|" \
  -e "s|{{ROOTCA_DER_BASE64}}|${ROOTCA_DER_BASE64}|" \
  -e "s|{{PROFILE_UUID}}|${PROFILE_UUID}|" \
  "$TEMPLATE" > "$OUT_PATH"

chmod 0600 "$OUT_PATH"
echo "rendered to $OUT_PATH" >&2
echo "  ACME_DIRECTORY_URL   = $ACME_DIRECTORY_URL" >&2
echo "  ACME_PAYLOAD_UUID    = $ACME_PAYLOAD_UUID" >&2
echo "  ROOTCA_PAYLOAD_UUID  = $ROOTCA_PAYLOAD_UUID" >&2
echo "  PROFILE_UUID         = $PROFILE_UUID" >&2
echo "  root CA              = (from step-ca-root-cert in $GCP_PROJECT)" >&2
echo >&2
echo "Upload to SimpleMDM as a NEW Custom Configuration Profile (different from Dev - SCEP)," >&2
echo "assign to gecko-t-osx-1500-m4-no-sip, push. Then 'rm $OUT_PATH'." >&2
