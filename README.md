# relops-bootstrap

Zero-touch provisioning infrastructure for Mozilla RelOps CI workers (macOS,
eventually all hardware).

This is the "Path C" architecture: GCP-native, workload-identity-style secret
delivery via SCEP-issued per-host client certs. It replaces the manual
"open 1Password, copy vault.yaml, SSH paste" handoff that the older Path A
(operator-laptop + 1Password CLI) uses today.

**It's not just for EACS re-provisioning** — the same workflow applies to:

- First-time provisioning of brand-new hardware coming out of DEP enrollment
- Re-provisioning an existing host via EACS (Erase All Content and Settings)
- Re-keying a host whose cert was compromised or whose role changed
- Routine cert rotation (SCEP handles renewal natively, no operator action)

The unifying primitive is "a freshly-DEP'd host that holds an SCEP-issued
client cert can fetch its role's secrets from a brokered Secret Manager."
Every operator-driven workflow above collapses to that.

## Architecture

```
host (m4 Mac Mini, fresh out of DEP — first boot OR post-EACS)
   │
   │ 1. Setup Assistant runs in Auto Advance, creates admin user with SecureToken
   │ 2. orchestrator (on operator laptop) ssh's in, runs:
   │      sudo profiles install -type bootstraptoken
   │ 3. SimpleMDM Custom Profile (SCEP payload) → triggers Apple's mdmclient
   │    SCEP flow → keypair generated in Secure Enclave, cert installed
   │    in System keychain by Apple's mdmclient
   │ 4. Bootstrap script mints a 60s JWT, signed by the cert's private key
   │ 5. curls https://vault-broker.../secret/${role} with JWT in Authorization
   │
   ▼
GCP project relops-bootstrap
   │
   ├── step-ca VM (GCE)
   │     SCEP server. Issues short-lived (~24h) client certs.
   │     Root cert PEM is loaded into Secret Manager so the broker can validate.
   │
   ├── vault-broker (Cloud Run)
   │     1. Validates JWT signature against step-ca root cert
   │     2. Reads vault-${role} from Secret Manager
   │     3. Logs the request (cert SAN, role, timestamp) to Cloud Audit Logs
   │     4. Returns vault.yaml content as response body
   │
   └── Secret Manager
         One secret per puppet role: vault-gecko_t_osx_1500_m4, etc.
         Values mirror 1Password "RelOps Vault" entries (1P stays the human-facing source of truth).
```

## What's in this repo

```
terraform/
├── main.tf              Provider config + API enablement
├── variables.tf         Inputs (project_id, region, puppet_roles list, ...)
├── secrets.tf           Secret Manager containers (one per puppet role)
├── vault_broker.tf      Cloud Run service + SA for the broker
├── iam.tf               Scoped IAM (broker SA → vault-* secrets only)
├── artifact_registry.tf Container image registry for the broker
├── network.tf           VPC + subnet + firewall for the step-ca VM
├── step_ca.tf           GCE VM running step-ca
├── outputs.tf           URLs, IPs, SA emails — used by orchestrator + SimpleMDM config
└── terraform.tfvars.example  Copy → terraform.tfvars + fill in real values
```

## Bringing it up

These are one-time operator steps. Once done, the whole flow is `./reprovision` from any operator's laptop.

### 1. Create the GCP project + state bucket

```bash
gcloud projects create relops-bootstrap --name="RelOps Bootstrap"
gcloud config set project relops-bootstrap
gcloud billing projects link relops-bootstrap --billing-account="$BILLING_ID"

gcloud storage buckets create gs://relops-bootstrap-tfstate \
  --location=us-central1 --uniform-bucket-level-access
```

Then uncomment the `backend "gcs"` block in `terraform/main.tf`.

### 2. Terraform apply

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
# edit terraform.tfvars: set trusted_source_cidrs to MDC1 worker network
terraform init
terraform plan
terraform apply
```

Outputs after first apply:
- `vault_broker_url` — Cloud Run URL of the broker
- `step_ca_ip` — static external IP of the step-ca VM (use this in the SimpleMDM SCEP profile)
- `artifact_registry_repo_url` — push broker images here
- `secret_ids` — empty containers waiting to be populated

### 3. Initialize step-ca (one-time, manual)

SSH into the step-ca VM and bootstrap the CA. See the top-of-file comments in `terraform/step_ca.tf` for the exact commands. The result is a `root_ca.crt` you copy to the broker:

```bash
gcloud secrets versions add step-ca-root-cert --data-file=root_ca.crt
```

### 4. Populate the vault secrets from 1Password

For each puppet role, push the role's vault.yaml content into Secret Manager:

```bash
op read "op://RelOps Vault/vault-gecko_t_osx_1500_m4/vault.yaml" \
  | gcloud secrets versions add vault-gecko_t_osx_1500_m4 --data-file=-
```

(In the steady state, the orchestrator does this transparently. For first bootstrap it's manual once.)

### 5. Build + deploy the broker container

The broker code itself isn't in this repo yet — that's the next chunk of work. Stub Cloud Run service is in place; `var.vault_broker_image` defaults to GCP's hello-world container until the broker is built.

### 6. Add the SCEP profile to SimpleMDM

Custom Configuration Profile, payload type `com.apple.security.scep`, URL pointing at the `step_ca_ip` output, challenge = the value you set in `step ca provisioner add scep-relops`.

Assign to the `gecko-t-osx-1500-m4-no-sip` group for first testing on m4-81.

## Security model

| Layer | Mechanism | What it protects against |
|---|---|---|
| Cloud Run ingress | INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER | Random internet traffic |
| LB frontend | Source-CIDR allowlist (MDC1 only) | Off-network attackers with a stolen cert |
| Broker app | JWT signature against step-ca root | Forged JWTs |
| Broker app | JWT claim checks (sub, role, exp, aud) | Replay attacks, role escalation |
| Broker IAM | Per-secret IAM binding (no project-wide grants) | Lateral movement if broker is compromised |
| Cloud Audit Logs | Every secret read logged with cert SAN | After-the-fact incident response |
| Cert lifecycle | SCEP auto-renew, ~24h cert lifetime | Long-lived compromised credentials |
| Cert private key | Stored in Apple Secure Enclave | Key exfiltration from disk |
| WIF (Cloud Run → SM) | No SA key files anywhere | SA key leakage |

## Open work

- Broker service code (Python or Go, ~250 LOC). Lives in a separate `broker/` directory we haven't created yet.
- Cloud Build pipeline (`cloudbuild.yaml`) to push broker image on commit.
- Orchestrator CLI tool that operators run on their laptops to drive an EACS end-to-end.
- HTTPS Load Balancer for the broker (currently Cloud Run is direct via .run.app URL; LB needed for source-CIDR allowlist enforcement).

## Related

- [project_eacs_workflow_design.md](../.claude/memory/project_eacs_workflow_design.md) — design doc for the whole EACS workflow this fits into
- [hangar terraform](../hangar/terraform/) — sibling project's terraform; this one borrows the file layout patterns
