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
# Until forge.relops.mozilla.com DNS resolves + we have a cert, use the step-ca
# external IP directly. The CA cert has 34.61.3.27 in its SAN so TLS validates.
STEP_CA_URL="${STEP_CA_URL:-https://34.61.3.27/scep/scep-no-sip}"

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

PAYLOAD_UUID=$(uuidgen)
PROFILE_UUID=$(uuidgen)
CHALLENGE=$(cat "$CHALLENGE_FILE")

# Substitute. Use a sentinel char unlikely to appear in any value (and not '/'
# because URL contains it).
render() {
  sed \
    -e "s|{{STEP_CA_URL}}|${STEP_CA_URL}|" \
    -e "s|{{SCEP_CHALLENGE}}|${CHALLENGE}|" \
    -e "s|{{COMMON_NAME}}|%ComputerName%|" \
    -e "s|{{PAYLOAD_UUID}}|${PAYLOAD_UUID}|" \
    -e "s|{{PROFILE_UUID}}|${PROFILE_UUID}|" \
    "$TEMPLATE"
}

if [ "$OUT_PATH" = "-" ]; then
  render
else
  render > "$OUT_PATH"
  chmod 0600 "$OUT_PATH"
  echo "rendered to $OUT_PATH" >&2
  echo "  STEP_CA_URL    = $STEP_CA_URL" >&2
  echo "  PAYLOAD_UUID   = $PAYLOAD_UUID" >&2
  echo "  PROFILE_UUID   = $PROFILE_UUID" >&2
  echo "  challenge      = (from step-ca-scep-challenge in $GCP_PROJECT)" >&2
  echo >&2
  echo "Upload to SimpleMDM as a Custom Configuration Profile, then 'rm $OUT_PATH'." >&2
fi
