# 🛰️ relops-bootstrap

> Zero-touch provisioning infrastructure for Mozilla RelOps CI workers
> (macOS today, eventually all hardware).

GCP-native, workload-identity-style secret delivery via SCEP-issued per-host
client certs. The same primitive — *a DEP-enrolled host that holds an SCEP
client cert can fetch its role's secrets from a brokered Secret Manager* —
covers every operator-driven flow against a worker:

| 🚀 | first-time provisioning of brand-new hardware out of DEP enrollment |
|----|----|
| 🔄 | re-provisioning an existing host via EACS *(Erase All Content and Settings)* |
| 🪪 | re-keying a host whose cert was compromised or whose role changed |
| ⏳ | routine cert rotation — SCEP renews on its own, no operator action |

No laptop step. No shared secret copied out of a password manager. No SSH
session to type `vault.yaml` into. Every secret read is logged with the
requesting cert's serial number — *"who pulled what, when"* is a one-liner
in Cloud Audit Logs.

---

## 🏗️ Architecture

```
                 ┌─────────────────────────────────────────────────┐
                 │   host (m4 Mac Mini, fresh out of DEP)          │
                 │                                                 │
                 │   1. Setup Assistant → admin user + SecureToken │
                 │   2. ssh: profiles install -type bootstraptoken │
                 │   3. SCEP profile → mdmclient → keypair in      │
                 │      Secure Enclave, cert in System keychain    │
                 │   4. Bootstrap script mints a 60s JWT signed    │
                 │      by the cert's private key                  │
                 │   5. curl https://broker/secret/<role>          │
                 └─────────────────┬───────────────────────────────┘
                                   │
                                   ▼
       ┌──────────────────────  GCP project: relops-bootstrap  ──────────────────────┐
       │                                                                              │
       │   🪪 step-ca  ────►  📜 vault-broker  ────►  🔐 Secret Manager               │
       │   GCE VM             Cloud Run                                               │
       │   SCEP issuer        validate JWT             vault-<role> per puppet role   │
       │   ~24h client        chain-check cert         (mirrors 1P "RelOps Vault")    │
       │   certs              role from cert SAN                                      │
       │                      JTI replay check                                        │
       │                      per-cert rate limit                                     │
       │                      logs to Cloud Audit                                     │
       │                                                                              │
       │   🛡️ HTTPS LB + Cloud Armor (source-CIDR allowlist) in front of broker     │
       │      → live at https://forge.relops.mozilla.com (Google-managed cert)        │
       │                                                                              │
       └──────────────────────────────────────────────────────────────────────────────┘
```

---

## 📂 What's in this repo

```
.
├── terraform/                    🏗️  GCP infra (Cloud Run, Secret Manager, step-ca VM, LB)
│   ├── main.tf, variables.tf, outputs.tf
│   ├── vault_broker.tf           ─ Cloud Run service + Secret Manager containers
│   ├── step_ca.tf, network.tf    ─ step-ca GCE VM + VPC + firewalls
│   ├── lb.tf                     ─ HTTPS LB + Cloud Armor allowlist
│   ├── iam.tf                    ─ scoped IAM bindings (least privilege)
│   ├── secrets.tf                ─ per-puppet-role secret containers
│   └── artifact_registry.tf      ─ broker image registry
│
├── broker/                       📜  FastAPI vault-broker service
│   ├── app/                      ─ auth (JWT + cert chain), revocation (CRL), replay cache, rate limit
│   ├── tests/                    ─ 31 tests covering happy + adversarial paths
│   └── Dockerfile, pyproject.toml
│
├── orchestrator/                 🧰  Operator CLI (`reprovision`)
│   ├── orchestrator/             ─ TC + SimpleMDM + 1P clients + workflow steps
│   ├── tests/
│   └── pyproject.toml
│
├── mdm/                          📱  SimpleMDM artifacts
│   └── scep-relops.mobileconfig.template
│
├── .github/workflows/test.yml    ✅  CI: pytest + terraform fmt/validate
└── cloudbuild.yaml               🚀  build + push + deploy broker on commit
```

---

## 🧭 Bringing it up

One-time operator steps. After this, the whole worker-provisioning flow is
`reprovision <hostname>` from any operator laptop.

### 1️⃣  Create the GCP project + state bucket

```bash
gcloud projects create relops-bootstrap --name="RelOps Bootstrap"
gcloud config set project relops-bootstrap
gcloud billing projects link relops-bootstrap --billing-account="$BILLING_ID"

gcloud storage buckets create gs://relops-bootstrap-tfstate \
  --location=us-central1 --uniform-bucket-level-access --public-access-prevention
gcloud storage buckets update gs://relops-bootstrap-tfstate --versioning
```

Enable the `backend "gcs"` block in `terraform/main.tf` once the bucket exists.

### 2️⃣  Terraform apply

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
# fill in trusted_source_cidrs (MDC1 worker network) and broker_hostname
terraform init
terraform apply
```

📤 **Outputs you'll need next:**

| Output | Used for |
|---|---|
| `vault_broker_url` | Cloud Run URL of the broker |
| `step_ca_ip` | static external IP of the step-ca VM (goes into the SCEP profile) |
| `broker_lb_ip` | static external IP of the LB (point DNS here) |
| `artifact_registry_repo_url` | where to `docker push` the broker image |
| `secret_ids` | Secret Manager containers waiting for content |

### 3️⃣  Initialize step-ca

See [`scripts/README.md`](scripts/README.md) — `scripts/bootstrap-step-ca.sh`
is the idempotent operator-side bootstrap. It generates random CA passwords,
initializes the CA on the persistent disk, adds the SCEP provisioner with a
role-aware x509 template, and outputs the root cert PEM for capture +
Secret Manager upload.

### 4️⃣  Build + deploy the broker

```bash
IMAGE=us-central1-docker.pkg.dev/relops-bootstrap/vault-broker/vault-broker:v0.1.0
docker buildx build --platform linux/amd64 -t $IMAGE broker
docker push $IMAGE
gcloud run deploy vault-broker --image=$IMAGE --region=us-central1
```

(Or push to `main` and let Cloud Build do it via `cloudbuild.yaml`.)

### 5️⃣  Populate per-role vault secrets

For each puppet role, mirror the 1Password entry into Secret Manager:

```bash
op read "op://RelOps Vault/vault-gecko_t_osx_1500_m4/notesPlain" \
  | gcloud secrets versions add vault-gecko_t_osx_1500_m4 --data-file=-
```

### 6️⃣  Upload the SCEP profile to SimpleMDM

See [mdm/README.md](mdm/README.md) for the substitution dance. Assign to the
device group whose hosts you want to provision via the broker.

---

## 🔐 Security model

| Layer | Mechanism | Protects against |
|---|---|---|
| Cloud Run ingress | `INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER` | random internet traffic |
| HTTPS LB | Cloud Armor source-CIDR allowlist | off-network attackers with a stolen cert |
| Broker app | JWT chain-verifies against the step-ca root | forged JWTs |
| Broker app | Claim checks (`sub`, `role`, `exp`, `aud`, `jti`) + alg whitelist | replay, role escalation, `alg=none` |
| Broker app | JTI replay cache + per-cert-serial rate limit | credential abuse |
| Broker IAM | Per-secret binding (no project-wide grants) | lateral movement if broker is compromised |
| Cloud Audit | Every secret read logged with cert serial | after-the-fact incident response |
| Cert lifecycle | SCEP auto-renew, short cert lifetime | long-lived compromised credentials |
| Cert private key | Apple Secure Enclave (non-extractable) | key exfiltration from disk |
| WIF | No SA key files anywhere | SA key leakage |

---

## 🌐 Live endpoints

| URL | What it is |
|---|---|
| `https://forge.relops.mozilla.com` | vault-broker, HTTPS LB-fronted, Google-managed cert, Cloud Armor source-CIDR allowlist |
| `https://step-ca.relops.mozilla` *(intra-VM only)* / `https://34.61.3.27` *(MDC1 only)* | step-ca SCEP + admin API, MDC1 source-CIDR allowlist |

Authorized callers (MDC1 NAT egress `63.245.209.101/32`) get `HTTP 200` on `/healthz`
and `HTTP 401` on `/secret/{role}` without a valid JWT — all the way through the
LB to the broker. Off-network traffic gets `403` from Cloud Armor.

## 🛠️ Open work

- 🔁 **Cloud Build trigger** — `cloudbuild.yaml` exists, OAuth-flow UI got stuck during initial wire-up; switch to a PAT-based webhook trigger or retry the GitHub App connection
- 🧪 **End-to-end SCEP enrollment test on macmini-m4-81** — SCEP profile uploaded to SimpleMDM, currently in `NotNow` retry loop
- 🪪 **Populate the per-role vault.yaml secrets** from 1Password into Secret Manager
- 🔒 **mTLS at the LB layer** (defense-in-depth on top of the broker's JWT check) — Server TLS Policy + Trust Config
- 🔭 **Widen `trusted_source_cidrs`** beyond `63.245.209.101/32` if other Mozilla NAT IPs need access

---

## 🤝 Sibling project

[`hangar`](https://github.com/mozilla-platform-ops/hangar) — the RelOps fleet
dashboard. Same GCP folder, same terraform patterns, same FastAPI stack. This
repo borrows hangar's layout conventions.
