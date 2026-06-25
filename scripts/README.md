# scripts/

Operator-side helpers. None of these run automatically — they're one-time
bootstrap utilities for an operator on their laptop or running on the step-ca
VM via SSH.

## bootstrap-step-ca.sh

Runs ON the step-ca VM. Initializes the CA and adds the SCEP provisioner.
Idempotent — safe to re-run after a VM recreate (preserves CA state on the
persistent disk that's attached via terraform).

**Operator-side driver** to actually use it:

```bash
# 1. Random challenge (32 chars, base64-ish).
umask 077
openssl rand -base64 32 | tr -d '\n+/=' | head -c 32 > /tmp/_scep_challenge

# 2. x509 cert template (currently hardcodes role=gecko_t_osx_1500_m4_no_sip;
#    edit for other roles, or maintain one per role).
cp scripts/scep-x509-template.json.example /tmp/scep-x509-template.json

# 3. Ship the script + inputs to the VM.
gcloud compute scp \
  scripts/bootstrap-step-ca.sh \
  /tmp/_scep_challenge \
  /tmp/scep-x509-template.json \
  step-ca:/tmp/ \
  --zone=us-central1-a --project=relops-bootstrap --tunnel-through-iap

# 4. Run it and capture the root cert PEM.
gcloud compute ssh step-ca \
  --zone=us-central1-a --project=relops-bootstrap --tunnel-through-iap \
  --command='chmod +x /tmp/bootstrap-step-ca.sh && /tmp/bootstrap-step-ca.sh' \
  | awk '/-----BEGIN CERTIFICATE-----/,/-----END CERTIFICATE-----/' \
  > /tmp/root_ca.crt

# 5. Push to Secret Manager.
gcloud secrets versions add step-ca-root-cert \
  --project=relops-bootstrap --data-file=/tmp/root_ca.crt
gcloud secrets versions add step-ca-scep-challenge \
  --project=relops-bootstrap --data-file=/tmp/_scep_challenge

# 6. Restart broker so its lifespan handler re-reads the new cert.
gcloud run services update vault-broker \
  --region=us-central1 --project=relops-bootstrap \
  --update-env-vars=BROKER_RELOAD_TRIGGER=$(date +%s)

# 7. Clean up local sensitive material.
rm -f /tmp/root_ca.crt /tmp/_scep_challenge /tmp/scep-x509-template.json
```

## scep-x509-template.json.example

step-ca's Go-template-driven x509 cert template for SCEP enrollments. The
SCEP provisioner uses this to compose certs from CSRs — preserves whatever
Subject + SANs the host's CSR carried, then **appends a SPIFFE URI SAN** of
the form:

```
spiffe://relops.mozilla/host/{Subject.CommonName}/role/<puppet_role>
```

The role is hardcoded in the template (one provisioner per role). When a new
puppet role gets brought into the SCEP flow, copy this file, change the role
string, and `step ca provisioner add scep-<role>` with the new template.
