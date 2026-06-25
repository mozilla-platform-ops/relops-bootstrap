# MDM artifacts

Files in this directory are uploaded to SimpleMDM (or whichever MDM is used) as
Custom Configuration Profiles. They are *templates* — the operator fills in the
real challenge/URL values from Secret Manager before uploading.

## scep-relops.mobileconfig.template

Apple SCEP Custom Profile payload. When installed on an MDM-enrolled host,
Apple's `mdmclient` runs the SCEP enrollment dance:

1. Generates a keypair in the **Secure Enclave** (P-256 EC; private key
   non-extractable, never on disk)
2. Builds a CSR with the configured Subject
3. Submits CSR to the configured SCEP URL using the configured challenge
4. Installs the returned cert in the **System keychain**
5. Renews automatically before expiry

## Template substitutions to make before uploading

| Placeholder | Source | What it is |
|---|---|---|
| `{{STEP_CA_URL}}` | `terraform output broker_lb_hostname` (or `step-ca.relops.mozilla` once DNS is provisioned) | Full URL to step-ca's SCEP endpoint, e.g. `https://step-ca.relops.mozilla/scep/scep-relops` |
| `{{SCEP_CHALLENGE}}` | `gcloud secrets versions access latest --secret=step-ca-scep-challenge --project=relops-bootstrap` | Shared challenge configured on the step-ca SCEP provisioner |
| `{{COMMON_NAME}}` | dynamic per host | macOS substitutes `%HardwareUUID%` or similar; we'll use `%ComputerName%` so the cert CN matches the worker hostname |
| `{{PAYLOAD_UUID}}` | generate a new UUID | Unique per profile version; regenerate when re-uploading |
| `{{PROFILE_UUID}}` | generate a new UUID | Outer profile UUID |

## Substituting

```bash
SCEP_CHALLENGE=$(gcloud secrets versions access latest \
  --secret=step-ca-scep-challenge --project=relops-bootstrap)

PAYLOAD_UUID=$(uuidgen)
PROFILE_UUID=$(uuidgen)

sed \
  -e "s|{{STEP_CA_URL}}|https://step-ca.relops.mozilla/scep/scep-relops|" \
  -e "s|{{SCEP_CHALLENGE}}|${SCEP_CHALLENGE}|" \
  -e "s|{{COMMON_NAME}}|%ComputerName%|" \
  -e "s|{{PAYLOAD_UUID}}|${PAYLOAD_UUID}|" \
  -e "s|{{PROFILE_UUID}}|${PROFILE_UUID}|" \
  scep-relops.mobileconfig.template > /tmp/scep-relops.mobileconfig

# Upload /tmp/scep-relops.mobileconfig to SimpleMDM → Custom Configuration Profile
# Assign to the gecko-t-osx-1500-m4-no-sip group for the m4-81 experiment.
# Delete /tmp/scep-relops.mobileconfig after upload (it contains the challenge).
```

## Per-host vs shared SCEP profile

This profile uses a single shared challenge for one SCEP provisioner.
That means: any host enrolled in MDM with this profile can request a cert.
Apple's SCEP wraps the request itself in a PKCS#7 signed by MDM's identity
cert, so SimpleMDM-issued enrollment requests can't be forged by a non-MDM
host — the shared challenge is a second factor on top of that.

For a higher-bar threat model, step-ca supports **per-host challenges**
(provisioner option `--challenge-secret-from-script` style). Future work
when fleet grows large enough to warrant it.
