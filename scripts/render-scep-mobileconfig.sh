#!/bin/bash
# render-scep-mobileconfig.sh — produce a SimpleMDM-uploadable .mobileconfig from
# mdm/scep-relops.mobileconfig.template.
#
# Reads the SCEP challenge from Secret Manager, generates UUIDs, substitutes the
# step-ca URL. The output contains the live SCEP challenge — do NOT commit it.
#
# Usage:
#   scripts/render-scep-mobileconfig.sh                 # outputs to /tmp/scep-relops.mobileconfig
#   scripts/render-scep-mobileconfig.sh /path/to/out    # custom output path
#   scripts/render-scep-mobileconfig.sh - > out.mc      # output to stdout
#
# Env overrides:
#   STEP_CA_URL=https://forge.relops.mozilla.com/scep/scep-no-sip  (defaults to IP-based URL)
#   GCP_PROJECT=relops-bootstrap

set -euo pipefail

GCP_PROJECT="${GCP_PROJECT:-relops-bootstrap}"
# Default SCEP URL goes through the forge.relops.mozilla.com LB (publicly-trusted
# Google-managed cert). The LB path-routes /scep/* to step-ca via an Internet NEG.
# Override with STEP_CA_URL=https://34.61.3.27/scep/scep-no-sip to bypass the LB.
STEP_CA_URL="${STEP_CA_URL:-https://forge.relops.mozilla.com/scep/scep-no-sip}"

OUT_PATH="${1:-/tmp/scep-relops.mobileconfig}"
TEMPLATE="$(dirname "$0")/../mdm/scep-relops.mobileconfig.template"

[ -r "$TEMPLATE" ] || { echo "ERROR: template not readable at $TEMPLATE" >&2; exit 2; }

# Strict perms while temp files are around — challenge is sensitive.
umask 077

CHALLENGE_FILE=$(mktemp)
trap 'rm -f "$CHALLENGE_FILE"' EXIT

# Pull challenge from Secret Manager.
if ! gcloud secrets versions access latest \
       --secret=step-ca-scep-challenge \
       --project="$GCP_PROJECT" > "$CHALLENGE_FILE" 2>/dev/null; then
  echo "ERROR: couldn't read step-ca-scep-challenge from project $GCP_PROJECT" >&2
  echo "       (check gcloud auth and that the secret has a version)" >&2
  exit 3
fi

if ! command -v uuidgen >/dev/null; then
  echo "ERROR: uuidgen not on PATH" >&2; exit 4
fi

SCEP_PAYLOAD_UUID=$(uuidgen)
ROOTCA_PAYLOAD_UUID=$(uuidgen)
PROFILE_UUID=$(uuidgen)
CHALLENGE=$(cat "$CHALLENGE_FILE")

# Pull the step-ca root cert from Secret Manager, convert PEM -> DER -> base64
# (single-line). The mobileconfig embeds this as the PKCS#1 cert payload so
# Apple installs it in the System Trust Store BEFORE the SCEP payload runs.
ROOTCA_PEM_FILE=$(mktemp)
ROOTCA_DER_FILE=$(mktemp)
trap 'rm -f "$CHALLENGE_FILE" "$ROOTCA_PEM_FILE" "$ROOTCA_DER_FILE"' EXIT

if ! gcloud secrets versions access latest \
       --secret=step-ca-root-cert \
       --project="$GCP_PROJECT" > "$ROOTCA_PEM_FILE" 2>/dev/null; then
  echo "ERROR: couldn't read step-ca-root-cert from project $GCP_PROJECT" >&2
  exit 5
fi

openssl x509 -in "$ROOTCA_PEM_FILE" -outform DER -out "$ROOTCA_DER_FILE"
ROOTCA_DER_BASE64=$(base64 < "$ROOTCA_DER_FILE" | tr -d '\n')

# Substitute. Each value goes through a literal sed s|...|...| — values that
# contain '/' (the URL) are fine because we use '|' as the delimiter.
render() {
  sed \
    -e "s|{{STEP_CA_URL}}|${STEP_CA_URL}|" \
    -e "s|{{SCEP_CHALLENGE}}|${CHALLENGE}|" \
    -e "s|{{COMMON_NAME}}|%ComputerName%|" \
    -e "s|{{SCEP_PAYLOAD_UUID}}|${SCEP_PAYLOAD_UUID}|" \
    -e "s|{{ROOTCA_PAYLOAD_UUID}}|${ROOTCA_PAYLOAD_UUID}|" \
    -e "s|{{ROOTCA_DER_BASE64}}|${ROOTCA_DER_BASE64}|" \
    -e "s|{{PROFILE_UUID}}|${PROFILE_UUID}|" \
    "$TEMPLATE"
}

if [ "$OUT_PATH" = "-" ]; then
  render
else
  render > "$OUT_PATH"
  chmod 0600 "$OUT_PATH"
  echo "rendered to $OUT_PATH" >&2
  echo "  STEP_CA_URL          = $STEP_CA_URL" >&2
  echo "  SCEP_PAYLOAD_UUID    = $SCEP_PAYLOAD_UUID" >&2
  echo "  ROOTCA_PAYLOAD_UUID  = $ROOTCA_PAYLOAD_UUID" >&2
  echo "  PROFILE_UUID         = $PROFILE_UUID" >&2
  echo "  challenge            = (from step-ca-scep-challenge in $GCP_PROJECT)" >&2
  echo "  root CA              = (from step-ca-root-cert in $GCP_PROJECT, embedded as PKCS#1)" >&2
  echo >&2
  echo "Upload to SimpleMDM as a Custom Configuration Profile, then 'rm $OUT_PATH'." >&2
fi
