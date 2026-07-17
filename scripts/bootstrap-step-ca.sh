#!/bin/bash
# bootstrap-step-ca.sh — initialize the step-ca server + add SCEP provisioners.
#
# Runs ON the step-ca VM. Idempotent: skips CA init if /home/step/.step exists,
# skips SCEP provisioner add if it already exists.
#
# The caller (operator on their laptop) is expected to:
#   1. Generate a random SCEP challenge and SCP it to /tmp/_scep_challenge
#   2. SCP this script to /tmp/ and run it
#   3. Capture the root cert PEM from stdout
#   4. Push the cert + challenge to Secret Manager
#
# The x509 cert template is EMBEDDED below and rendered once per role from the
# SCEP_ROLES table (one SCEP provisioner per macOS puppet role), so a CA rebuild
# recreates every provisioner automatically. (Previously the template was SCP'd to
# /tmp/scep-x509-template.json for a single hardcoded provisioner;
# scripts/scep-x509-template.json.example remains as a reference of the rendered shape.)
#
# Why this script lives in-repo and not in terraform's startup script:
#   step ca init requires interactive password generation + handles the CA root key
#   material. We never want terraform to know those passwords. Operators run this
#   under a human session that won't end up in terraform state or CI logs.

set -euo pipefail

if ! command -v step-ca >/dev/null; then
  echo "ERROR: step-ca not installed (the VM startup script should have done that)" >&2
  exit 1
fi

[ -r /tmp/_scep_challenge ] || { echo "missing /tmp/_scep_challenge"; exit 2; }

# Base x509 template for SCEP-issued client certs — identical for every provisioner
# (macOS and Linux) except __SPIFFE_ROLE__, substituted per provisioner in the add loop
# below. The vault-broker authorizes on the puppet role stamped into this SPIFFE URI.
cat > /tmp/scep-x509-template.base.json <<'TMPL'
{
  "subject": {{ toJson .Subject }},
  "sans": [
    {{- range $i, $s := .SANs }}{{- if $i }},{{- end }}
    {{ toJson $s }}
    {{- end }},
    { "type": "uri", "value": "spiffe://relops.mozilla/host/{{ .Subject.CommonName }}/role/__SPIFFE_ROLE__" }
  ],
  "keyUsage": ["digitalSignature","keyEncipherment"],
  "extKeyUsage": ["clientAuth"]
}
TMPL

sudo chown step:step /tmp/_scep_challenge /tmp/scep-x509-template.base.json

sudo -u step bash <<'STEPUSER'
set -euo pipefail
export STEPPATH=/home/step/.step

if [ ! -d "$STEPPATH/config" ]; then
  echo "=== step ca init ===" >&2
  mkdir -p "$STEPPATH/secrets"
  chmod 0700 "$STEPPATH/secrets"
  openssl rand -base64 32 | tr -d '\n' > "$STEPPATH/secrets/password"
  openssl rand -base64 32 | tr -d '\n' > "$STEPPATH/secrets/provisioner-password"
  chmod 0600 "$STEPPATH/secrets/password" "$STEPPATH/secrets/provisioner-password"

  # --dns values must include every hostname/IP that any caller (incl. step CLI
  # itself when calling the admin API over loopback) will use to reach this CA.
  step ca init \
    --name "Mozilla RelOps Bootstrap CA" \
    --dns "step-ca.relops.mozilla" \
    --dns "$(/bin/hostname -I | awk '{print $1}')" \
    --address ":443" \
    --provisioner "admin@mozilla.com" \
    --password-file "$STEPPATH/secrets/password" \
    --provisioner-password-file "$STEPPATH/secrets/provisioner-password" \
    --deployment-type standalone >&2
fi
STEPUSER

sudo systemctl enable step-ca >/dev/null 2>&1
sudo systemctl restart step-ca
sleep 4
sudo systemctl is-active step-ca >&2

sudo -u step bash <<'STEPUSER'
set -euo pipefail
export STEPPATH=/home/step/.step
CHALLENGE=$(cat /tmp/_scep_challenge)

# SCEP requires an RSA decrypter cert/key (the RA cert the client encrypts its
# request to). This CA is EC, so step-ca can NOT auto-use the CA key for SCEP —
# every SCEP provisioner MUST be given an explicit RSA decrypter, or GetCACert
# returns only the CA cert (x-x509-ca-cert) and clients fail with MDM-SCEP:14003
# "unable to generate CSR" (hit 2026-07; the loop below previously omitted it).
# Use ONE shared decrypter for all provisioners: reuse the existing one if the CA
# already has SCEP provisioners (keeps them consistent), else mint a new RSA one
# signed by the CA.
DECRYPTER_CRT="$STEPPATH/secrets/scep-decrypter.crt"
DECRYPTER_KEY="$STEPPATH/secrets/scep-decrypter.key"
if [ ! -s "$DECRYPTER_CRT" ] || [ ! -s "$DECRYPTER_KEY" ]; then
  if python3 - "$STEPPATH/config/ca.json" "$DECRYPTER_CRT" "$DECRYPTER_KEY" <<'PY'
import base64, json, sys
caj, crt, key = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    d = json.load(open(caj))
except Exception:
    sys.exit(1)
for p in d.get("authority", {}).get("provisioners", []):
    if p.get("type", "").upper() == "SCEP" and p.get("decrypterCertificate") and p.get("decrypterKeyPEM"):
        open(crt, "wb").write(base64.b64decode(p["decrypterCertificate"]))
        open(key, "wb").write(base64.b64decode(p["decrypterKeyPEM"]))
        sys.exit(0)
sys.exit(1)
PY
  then
    echo "=== reusing existing SCEP decrypter from ca.json ===" >&2
  else
    echo "=== minting new RSA SCEP decrypter (CA-signed) ===" >&2
    step certificate create "Mozilla RelOps SCEP Decrypter" \
      "$DECRYPTER_CRT" "$DECRYPTER_KEY" \
      --ca "$STEPPATH/certs/intermediate_ca.crt" \
      --ca-key "$STEPPATH/secrets/intermediate_ca_key" \
      --ca-password-file "$STEPPATH/secrets/password" \
      --kty RSA --size 2048 --not-after 87600h \
      --no-password --insecure --force >&2
  fi
  chmod 0600 "$DECRYPTER_CRT" "$DECRYPTER_KEY"
fi

# One SCEP provisioner per puppet role. Each needs a populated vault-<role> secret
# in Secret Manager + (macOS) a SimpleMDM SCEP profile or (Linux) an sscep enroll,
# both pointing at /scep/<name>. Add a role here and a rebuild recreates its
# provisioner automatically. This table must match the live CA's provisioner list
# (`step ca provisioner list`) so the CA is reproducible from code.
#
# NOTE ON STATUS (2026-07): only the macOS m4 family has populated vault-* secrets
# and is live. The gecko_t_linux_* provisioners are in service on the CA but their
# secrets are still empty (the Linux path is scaffolded, not yet functional) — they
# are codified here so the CA stays reproducible, not because Linux is live.
declare -A SCEP_ROLES=(
  ["scep-no-sip"]="gecko_t_osx_1500_m4"
  ["scep-osx-1500-m4-staging"]="gecko_t_osx_1500_m4_staging"
  # Tart VM tester pool — the tart host authenticates as the VM pool role to
  # fetch + inject the VM's vault (see Bug 2049579 remediation).
  ["scep-gecko-t-osx-1500-m-vms"]="gecko_t_osx_1500_m_vms"
  ["scep-gecko-t-osx-1500-m-vms-staging"]="gecko_t_osx_1500_m_vms_staging"
  # macOS Apple Silicon build (arm64). L3 (gecko_3_b / enterprise_3_b) are release-trusted;
  # per-role SCEP challenges are recommended before those pools carry live secrets (see below).
  ["scep-gecko-1-b-osx-arm64"]="gecko_1_b_osx_arm64"
  ["scep-gecko-3-b-osx-arm64"]="gecko_3_b_osx_arm64"
  ["scep-enterprise-1-b-osx-arm64"]="enterprise_1_b_osx_arm64"
  ["scep-enterprise-3-b-osx-arm64"]="enterprise_3_b_osx_arm64"
  # Linux
  ["scep-gecko-t-linux-talos"]="gecko_t_linux_talos"
  ["scep-gecko-t-linux-2204-talos"]="gecko_t_linux_2204_talos"
  ["scep-gecko-t-linux-2404-talos"]="gecko_t_linux_2404_talos"
  ["scep-gecko-t-linux-2404-talos-wayland"]="gecko_t_linux_2404_talos_wayland"
  ["scep-gecko-t-linux-netperf"]="gecko_t_linux_netperf"
  ["scep-gecko-t-linux-2404-netperf"]="gecko_t_linux_2404_netperf"
)

for name in "${!SCEP_ROLES[@]}"; do
  role="${SCEP_ROLES[$name]}"
  if step ca provisioner list \
       --ca-url=https://step-ca.relops.mozilla:443 \
       --root="$STEPPATH/certs/root_ca.crt" 2>/dev/null \
     | grep -q "\"name\": \"${name}\""; then
    echo "${name} provisioner already configured, skipping add" >&2
    continue
  fi
  echo "=== adding ${name} SCEP provisioner (role ${role}) ===" >&2
  tmpl="/tmp/scep-x509-template-${name}.json"
  sed "s/__SPIFFE_ROLE__/${role}/" /tmp/scep-x509-template.base.json > "$tmpl"
  step ca provisioner add "${name}" \
    --type=SCEP \
    --force-cn \
    --challenge="$CHALLENGE" \
    --x509-template="$tmpl" \
    --scep-decrypter-certificate="$DECRYPTER_CRT" \
    --scep-decrypter-key="$DECRYPTER_KEY" \
    --admin-provisioner=admin@mozilla.com \
    --admin-password-file="$STEPPATH/secrets/provisioner-password" \
    --ca-url=https://step-ca.relops.mozilla:443 \
    --root="$STEPPATH/certs/root_ca.crt" >&2
  rm -f "$tmpl"
done
STEPUSER

# Reload step-ca so the new provisioner takes effect.
sudo systemctl restart step-ca
sleep 4
sudo systemctl is-active step-ca >&2

# Emit root cert PEM on stdout (caller captures).
sudo cat /home/step/.step/certs/root_ca.crt

# Wipe the temp files — caller is responsible for re-uploading the challenge to
# Secret Manager from their own host.
sudo rm -f /tmp/_scep_challenge /tmp/scep-x509-template.base.json
