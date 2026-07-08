# 🛰️ relops-bootstrap

> Zero-touch provisioning infrastructure for Mozilla RelOps CI workers
> (macOS today, eventually all hardware).

GCP-native, workload-identity-style secret delivery via SCEP-issued per-host
client certs and **mTLS at the load balancer**. The same primitive — *a
DEP-enrolled host that holds an SCEP client cert can fetch its role's secrets
from a brokered Secret Manager* — covers every operator-driven flow:

| 🚀 | first-time provisioning of brand-new hardware out of DEP enrollment |
|----|----|
| 🔄 | re-provisioning an existing host via EACS *(Erase All Content and Settings)* |
| 🪪 | re-keying a host whose cert was compromised or whose role changed |
| ⏳ | routine cert rotation — SCEP renews on its own, no operator action |

No laptop step. No shared secret copied out of a password manager. No SSH
session to type `vault.yaml` into. Every secret read is logged with the
requesting cert's serial number — *"who pulled what, when"* is a one-liner
in Cloud Audit Logs.

**Status:** Validated end-to-end on m4-80 + m4-81 (2026-07) — EACS → DEP enroll →
SimpleMDM profiles + signed **bootstrap PKG** → SCEP cert in keychain → operator mints
the SecureToken (one interactive ssh login) → bootstrap PKG fetches vault.yaml → puppet →
worker in Taskcluster. Driven by the `orchestrator/` CLI (`reprovision`). The one required
human step per host is the **SecureToken mint** — DEP skips Setup Assistant, so admin holds
no token until a PAM (password) login; everything else is hands-off. (The bootstrap is now a
signed PKG that lands during DEP convergence — no GCP/script-job trigger.)

---

## 🏗️ Architecture

```
                 ┌─────────────────────────────────────────────────┐
                 │   host (m4 Mac Mini, fresh out of DEP)          │
                 │                                                 │
                 │   1. DEP enroll (Setup Assistant skipped) →      │
                 │      managed admin (fixed DEP password)          │
                 │   2. Operator mints the first SecureToken via    │
                 │      one interactive ssh login; BST escrowed     │
                 │   3. SCEP profile → mdmclient → keypair +        │
                 │      cert in System keychain                     │
                 │   4. Signed bootstrap PKG (managed install):     │
                 │      CURL_SSL_BACKEND=securetransport \          │
                 │      curl --cert "<CN>" https://forge/secret/X   │
                 │      (TLS handshake signs via OS network stack,  │
                 │       which holds the keychain sign ACL)         │
                 └─────────────────┬───────────────────────────────┘
                                   │ mTLS
                                   ▼
       ┌──────────────────────  GCP project: relops-bootstrap  ──────────────────────┐
       │                                                                              │
       │   🛡️ HTTPS LB                                                                │
       │      • Cloud Armor (source-CIDR allowlist, MDC1 NAT)                         │
       │      • Trust Config: step-ca root + intermediate (validates client cert)     │
       │      • Server TLS Policy: ALLOW_INVALID_OR_MISSING (so /scep/* still works)  │
       │      • Forwards X-Client-Cert-{Present,Chain-Verified,Leaf,Serial,SPIFFE}    │
       │      • Live at https://forge.relops.mozilla.com (Google-managed cert)        │
       │                                                                              │
       │           │                                          │                       │
       │           ▼                                          ▼                       │
       │   🪪 step-ca                              📜 vault-broker                    │
       │   GCE VM (zonal NEG)                      Cloud Run (serverless NEG)         │
       │   SCEP issuer per puppet role             • Reads X-Client-Cert-Leaf         │
       │   Cert SPIFFE URI =                       • URL-decodes SPIFFE URI from SAN  │
       │     spiffe://relops.mozilla/              • Checks role matches URL path     │
       │       host/<CN>/role/<role>               • Reads vault-<role> secret        │
       │                                           • Per-cert-serial rate limit       │
       │                                           • Logs to Cloud Audit              │
       │                                                                              │
       │                                           🔐 Secret Manager                  │
       │                                              vault-<role> per puppet role    │
       │                                              (mirrors 1P "RelOps Vault")     │
       │                                                                              │
       └──────────────────────────────────────────────────────────────────────────────┘
```

### Why mTLS-at-LB instead of JWT-signed-by-cert?

macOS keychain ACL gates `SecKeyCreateSignature` to a hard-coded list of
network-stack helpers (`configd`, `nehelper`, `NEIKEv2Provider`). A
Developer-ID-signed binary running as root in a LaunchDaemon — let alone our
bootstrap shell — gets `errSecInteractionNotAllowed` when trying to sign with
a keychain-stored key. We proved this end of every plausible workaround
(`AllowAllAppsAccess=true`, `KeyIsExtractable=true`, `SecItemExport`, ACME
via the data-protection keychain). The keychain genuinely will not let our
code use the SCEP key directly.

But the keychain ACL *does* allow signing from the OS network stack — which is
exactly the path `curl` takes when invoked with `CURL_SSL_BACKEND=securetransport`.
The TLS handshake's CertificateVerify is signed *inside* SecureTransport
(via the helpers in the allowlist), not in our process. Our code never
touches the private key. No PKCS12 extraction. No key on disk. The cert
never leaves the keychain.

The LB validates the cert chain via its Trust Config (step-ca root +
intermediate) and forwards parsed cert info to the broker as request
headers. The broker reads `X-Client-Cert-Leaf` and parses the SPIFFE URI
out of the SAN — we don't trust the LB's built-in SPIFFE parser because it
silently drops URI SANs with URL-encoded chars (e.g. `Mac%20mini`).

---

## 📂 What's in this repo

```
.
├── terraform/                    🏗️  GCP infra
│   ├── main.tf, variables.tf, outputs.tf
│   ├── vault_broker.tf           ─ Cloud Run service + IAM
│   ├── step_ca.tf, network.tf    ─ step-ca GCE VM + VPC + firewalls
│   ├── lb.tf                     ─ HTTPS LB + Cloud Armor + custom request headers
│   ├── mtls.tf                   ─ Trust Config + Server TLS Policy
│   ├── trust/{root,intermediate}.pem ─ step-ca PEMs the Trust Config points at
│   ├── iam.tf                    ─ scoped IAM bindings (least privilege)
│   ├── secrets.tf                ─ per-puppet-role secret containers
│   └── artifact_registry.tf      ─ broker image registry
│
├── broker/                       📜  FastAPI vault-broker service
│   ├── app/                      ─ reads X-Client-Cert-* headers, parses leaf,
│   │                               URL-decodes SPIFFE, role-binds, fetches secret
│   ├── tests/                    ─ pytest happy-path + adversarial cases
│   └── Dockerfile, pyproject.toml
│
├── orchestrator/                 🧰  Operator CLI (`reprovision`)
│   └── ... TC (Hawk) + SimpleMDM clients + workflow steps (quarantine→wipe→mint→escrow→wait)
│                                   see orchestrator/README.md for the pipeline + golden paths
│
├── pkg/                          📦  Signed bootstrap PKG builder (current delivery)
│   ├── build.sh                  ─ builds + Developer-ID-signs the component pkg (43AQ936H96)
│   ├── payload/                  ─ /usr/local/sbin/m4-bootstrap.sh + entrypoint LaunchDaemon
│   └── scripts/postinstall       ─ kicks the entrypoint on install
│
├── provisioner/                  ⏰  GCP cron auto-trigger — six default-deny guards
│   └── app/                      ─ Cloud Run: Scheduler tick → guard checks → SimpleMDM
│                                   script-job. The earlier delivery path; the signed PKG
│                                   (pkg/) now lands the bootstrap at DEP convergence instead.
│
├── mdm/                          📱  SimpleMDM artifacts
│   ├── scep-relops.mobileconfig.template
│   ├── acme-relops.mobileconfig.template     ─ historical (ACME path investigated)
│   └── com.mozilla.relops.bootstrap.plist     ─ optional periodic refresh daemon
│
├── scripts/                      🛠️  Operator helpers + worker bootstrap
│   ├── bootstrap-step-ca.sh                  ─ idempotent step-ca init
│   ├── render-scep-mobileconfig.sh           ─ render SCEP profile for SimpleMDM upload
│   ├── simplemdm-m4-no-sip-bootstrap.sh      ─ ⭐ bootstrap script — now shipped as a signed PKG (managed install)
│   ├── mint-securetoken.sh                    ─ automate the SecureToken mint (+ BST escrow) over ssh
│   ├── simplemdm-vault-fetch-snippet.sh      ─ just the fetch block, for splicing
│   ├── relops-bootstrap.sh                   ─ LaunchDaemon variant for periodic refresh
│   ├── install-on-worker.sh                  ─ operator-laptop installer
│   ├── integration-test-broker.py            ─ end-to-end test via real step-ca cert
│   ├── INSTALL-on-worker.md                  ─ install guide + threat-model table
│   └── fetch-vault-mtls.swift                ─ historical URLSession variant (dead end)
│
├── docs/                         📖  rendered walkthrough (index.html + deep-dive.html)
├── .github/workflows/test.yml    ✅  CI: pytest + terraform fmt/validate
└── cloudbuild.yaml               🚀  build + push + deploy broker on commit
```

---

## 🧭 Bringing it up

One-time operator steps. After this, provisioning a worker is driven by the
`orchestrator/` CLI (`reprovision`) — see [`orchestrator/README.md`](orchestrator/README.md)
for the **fresh vs reprovision** golden paths. The bootstrap ships as a signed PKG.

### 1️⃣  Create the GCP project + state bucket

```bash
gcloud projects create relops-bootstrap --name="RelOps Bootstrap"
gcloud config set project relops-bootstrap
gcloud billing projects link relops-bootstrap --billing-account="$BILLING_ID"

gcloud storage buckets create gs://relops-bootstrap-tfstate \
  --location=us-central1 --uniform-bucket-level-access --public-access-prevention
gcloud storage buckets update gs://relops-bootstrap-tfstate --versioning
```

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
| `vault_broker_url` | Cloud Run URL of the broker (debug only — production traffic via LB) |
| `step_ca_ip` | static external IP of the step-ca VM |
| `broker_lb_ip` | static external IP of the LB (point DNS here) |
| `artifact_registry_repo_url` | where to `docker push` the broker image |
| `secret_ids` | Secret Manager containers waiting for content |

### 3️⃣  Initialize step-ca

See [`scripts/README.md`](scripts/README.md) — `scripts/bootstrap-step-ca.sh`
is the idempotent operator-side bootstrap. Per-role SCEP provisioners get
added afterward with `step ca provisioner add scep-<role> --type=SCEP ...`.

### 4️⃣  Build + deploy the broker

```bash
gcloud builds submit broker \
  --tag us-central1-docker.pkg.dev/relops-bootstrap/vault-broker/vault-broker:latest \
  --project relops-bootstrap
gcloud run deploy vault-broker \
  --image us-central1-docker.pkg.dev/relops-bootstrap/vault-broker/vault-broker:latest \
  --region us-central1 --project relops-bootstrap
```

(Or push to `main` and let Cloud Build do it via `cloudbuild.yaml`.)

### 5️⃣  Populate per-role vault secrets

For each puppet role, mirror the 1Password entry into Secret Manager:

```bash
op read "op://RelOps Vault/vault-gecko_t_osx_1500_m4/notesPlain" \
  | gcloud secrets versions add vault-gecko_t_osx_1500_m4 --data-file=-
```

### 6️⃣  Upload the SCEP profile to SimpleMDM

`scripts/render-scep-mobileconfig.sh` produces a .mobileconfig with the
step-ca root embedded + SCEP enrollment payload. Upload to SimpleMDM as a
Custom Configuration Profile, assign to the device group whose hosts you
want to provision via the broker.

### 7️⃣  Ship the bootstrap as a signed PKG

`scripts/simplemdm-m4-no-sip-bootstrap.sh` is packaged as a **signed PKG** (managed
install) assigned to the device group, so it lands during DEP convergence and runs on its
own — no SimpleMDM script-job, no GCP trigger. It:

1. Waits for admin to hold a SecureToken — the operator mints it via **one interactive ssh
   login** (DEP skips Setup Assistant, so admin has no token until a PAM login; a script
   can't grant the *first* SecureToken). BST escrow happens alongside the mint.
2. Discovers the SCEP identity in the keychain by issuer DN
3. Fetches the role-scoped `vault.yaml` from the broker via SecureTransport curl
4. Clones ronin_puppet, sets up ssh-to-localhost, installs the m4-bootstrap-driver
   LaunchDaemon (which loops `run-puppet.sh` until the safari semaphores fire)

The mint + BST escrow are driven operator-side by `reprovision mint` / `reprovision
escrow-bst` (or the standalone `scripts/mint-securetoken.sh`).

---

## 🔐 Security model

| Layer | Mechanism | Protects against |
|---|---|---|
| Cloud Run ingress | `INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER` | random internet traffic |
| HTTPS LB | Cloud Armor source-CIDR allowlist | off-network attackers |
| HTTPS LB | mTLS via Trust Config (step-ca root + intermediate) | requests without a valid step-ca-issued cert |
| Broker app | Reads LB-forwarded `X-Client-Cert-Chain-Verified` + parses leaf for SPIFFE | spoofed headers (LB strips client-supplied X-Client-Cert-* before adding its own) |
| Broker app | Role from cert SPIFFE URI must match URL path role | role escalation / cross-host secret theft |
| Broker app | Per-cert-serial rate limit | credential abuse |
| Broker IAM | Per-secret binding (no project-wide grants) | lateral movement if broker is compromised |
| Cloud Audit | Every secret read logged with cert serial | after-the-fact incident response |
| Cert lifecycle | SCEP auto-renew, short cert lifetime | long-lived compromised credentials |
| Cert private key | macOS System keychain ACL-restricted to network-stack helpers | key exfiltration by non-OS code |

---

## 🌐 Live endpoints

| URL | What it is |
|---|---|
| `https://forge.relops.mozilla.com` | vault-broker (path `/secret/{role}`) AND step-ca (`/scep/*`), HTTPS LB-fronted with Google-managed server cert + mTLS for `/secret/*` |
| Cloud Run direct URL | broker, for debug-only (no LB-forwarded mTLS headers; broker returns 401) |

Path C status (m4 role): proven end-to-end. The validating call from a
real EACS'd m4 returns the full role-scoped vault.yaml with the broker's
strict checks all green.

## 🛠️ Open work

- 🪪 **Per-role expansion** — currently one step-ca SCEP provisioner +
  matching SimpleMDM profile for the `gecko_t_osx_1500_m4` role. Repeat for
  `gecko_t_osx_1500_m4_staging`, `gecko_t_osx_1400_r8`, `gecko_t_osx_1015`.
  Each is mechanical: new provisioner with template hardcoding the role in
  the SPIFFE URI + new "Dev - SCEP - <role>" SimpleMDM profile + populate
  the corresponding Secret Manager secret.
- 🔁 **Cloud Build trigger** — `cloudbuild.yaml` exists, OAuth-flow UI
  got stuck during initial wire-up; switch to a PAT-based webhook trigger
  or retry the GitHub App connection. Manual `gcloud builds submit` works.
- 🔭 **Widen `trusted_source_cidrs`** beyond `63.245.209.101/32` if other
  Mozilla NAT IPs need access (other datacenters, VPN egress, etc.).
- ✅ **Bootstrap via signed PKG** — DONE (2026-07): the bootstrap is a signed PKG
  (managed install) that lands at DEP convergence instead of a triggered script-job.
- 🔑 **Streamline operator creds** — `reprovision` still needs SimpleMDM / TC / admin-pw
  env vars pasted per session. Move to runtime fetch (Secret Manager / 1Password) and/or a
  Hangar IAP-gated action. The TC `quarantine-worker` token also needs periodic re-issue.

---

## 🤝 Sibling project

[`hangar`](https://github.com/mozilla-platform-ops/hangar) — the RelOps fleet
dashboard. Same GCP folder, same terraform patterns, same FastAPI stack. This
repo borrows hangar's layout conventions.
