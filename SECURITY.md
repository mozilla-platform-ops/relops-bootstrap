# Security model & verification guide

This document is written for a security reviewer. It states what the system
claims about its own security, points at **where each claim is enforced in the
code/infra**, and gives the **exact command to verify it against live state** —
so no claim here has to be taken on trust. It also lists the honest residual
risks and the things we deliberately do *not* defend against.

Companion reading: [`README.md`](README.md) (architecture + bring-up),
[`docs/deep-dive.html`](docs/deep-dive.html) (threat model, dead ends),
[`broker/README.md`](broker/README.md) (the broker's auth model).

---

## What the system is

An MDM-enrolled (macOS) or operator-attested (Linux) CI worker fetches its
per-role `vault.yaml` from a brokered Secret Manager over **mutual TLS**:

```
worker (SCEP client cert)
  → HTTPS LB  (Cloud Armor + mTLS termination; injects X-Client-Cert-* headers)
    → vault-broker (Cloud Run; validates headers, role-binds, reads the secret)
      → Secret Manager  vault-<role>
```

The reprovision *orchestration* (EACS → re-enroll → mint → puppet) is driven by
the `reprovision` CLI, run either directly or by a Puppet-managed **on-network
runner** that Hangar queues jobs to. This document covers the **secret-delivery
security model**; the reprovision safety guards are summarized at the end.

---

## The load-bearing invariant (read this first)

The broker performs **no cryptographic validation of its own** beyond parsing
the leaf cert to read its role. It trusts the load balancer's
`X-Client-Cert-Chain-Verified` header. That is safe **only** because of two
infrastructure facts, and the entire model collapses if either regresses:

1. **The LB overwrites the `X-Client-Cert-*` header names it injects**, so an
   inbound client cannot forge them.
2. **The broker is unreachable except through the LB** (Cloud Run ingress =
   internal-and-cloud-load-balancing), so nothing can present hand-crafted
   headers directly.

Both are verified in the table below (rows **H1** and **H2**). If a reviewer
checks nothing else, check those two.

---

## Claim → enforcement → verification

Line numbers are approximate (verify against the current tree); commands assume
`gcloud config set project relops-bootstrap` and appropriate IAM.

### Network perimeter

| # | Claim | Enforced in | Verify |
|---|---|---|---|
| **N1** | Only the MDC1 egress IP can reach the LB; everything else is denied before TLS. | `terraform/lb.tf` — `google_compute_security_policy.broker`, default rule `deny(403)` at priority 2147483647, allow `63.245.209.101/32` at 1000; attached to **both** broker and step-ca backends. | `gcloud compute security-policies describe <policy> --format='yaml(rules)'` — confirm default action `deny(403)` and the single allow CIDR. |
| **N2** | The broker cannot be reached except via the LB. | `terraform/vault_broker.tf` — `ingress = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"`. | `gcloud run services describe vault-broker --region us-central1 --format='value(metadata.annotations["run.googleapis.com/ingress"])'` → `internal-and-cloud-load-balancing`. |
| **N3** | step-ca has no public ingress except HTTPS (LB/SCEP) and IAP-only SSH. | `terraform/network.tf` — `allow-step-ca-inbound` (443/8443 from trusted CIDRs + Google LB ranges), `allow-step-ca-ssh-iap` (22 from `35.235.240.0/20`). No `0.0.0.0/0`. | `gcloud compute firewall-rules list --filter='network:relops-bootstrap' --format='table(name,sourceRanges.list(),allowed[].map().firewall_rule().list())'` |

### mTLS / header trust (the invariant)

| # | Claim | Enforced in | Verify |
|---|---|---|---|
| **H1** | The LB sets the `X-Client-Cert-*` header values itself, overwriting inbound values for those names — a client can't spoof `Chain-Verified: true`. | `terraform/lb.tf` — backend-service `custom_request_headers` list (9 `X-Client-Cert-*` names). | Read `terraform/lb.tf`; confirm the broker reads only names in that list (`broker/app/main.py:51-59`). Note: header names **not** in the list are pass-through — the broker must not read any such name (it doesn't). |
| **H2** | The broker trusts `X-Client-Cert-Chain-Verified` and does **not** re-verify the chain. | `broker/app/auth.py:113-118` (`verify_mtls_headers`); comment at `auth.py:19-20` states this explicitly. | Read `auth.py`; confirm no chain-building/verification call exists. This is why **N2** is load-bearing. |
| **H3** | mTLS chain validation happens at the LB against our CA only. | `terraform/mtls.tf` — `trust_config` with `trust/root.pem` + `trust/intermediate.pem`; server TLS policy `ALLOW_INVALID_OR_MISSING_CLIENT_CERT`. | `gcloud certificate-manager trust-configs describe <name> --format=yaml`; the "allow invalid or missing" mode is intentional so cert-less `/scep/*` works — enforcement of `/secret/*` is in the broker, not the LB. |

### Broker authorization

| # | Claim | Enforced in | Verify |
|---|---|---|---|
| **B1** | No cert / unverified chain / missing leaf → **401**. | `broker/app/auth.py:113-118` → `main.py:72-76`. | `pytest broker` — `test_missing_cert_present_header_401`, `test_chain_not_verified_401`, `test_missing_leaf_header_401`. |
| **B2** | Role is read from the cert SAN's SPIFFE URI (leaf parsed + URL-decoded by the broker, not from an LB SPIFFE header). | `broker/app/auth.py:72-96` (`_extract_spiffe`, `unquote`). | `pytest broker` — `test_spiffe_url_encoded_space_in_cn` (Mac%20mini→200), `test_no_spiffe_in_san_401`. |
| **B3** | Cert role must exactly equal the URL path role, else **403**. | `broker/app/main.py:92-102`. | `pytest broker` — `test_role_mismatch_403`. |
| **B4** | Per-cert-serial rate limit, else **429**. **In-memory, per-instance** — a speed bump, not a global quota. | `broker/app/main.py:78-90`; `broker/app/replay_cache.py` (token bucket, `dict`+`Lock`, per-process). | `pytest broker` — `test_rate_limit_returns_429`. Limitation is real: an autoscaled deployment enforces a higher aggregate limit. Fix path: shared store (Memorystore). |
| **B5** | The broker reads only `vault-<role>` built from the *validated* role (no path traversal to other secrets). | `broker/app/main.py:104` (`secret_id = f"vault-{role}"`, only after B3 passes); `broker/app/secrets.py:13-17`. | Read `main.py`; the role is the URL path segment, already equality-checked against the cert. |

### IAM / least privilege

| # | Claim | Enforced in | Verify |
|---|---|---|---|
| **I1** | The broker SA can read only `vault-*` + `step-ca-root-cert` secrets; no project-wide grant. | `terraform/iam.tf` — per-secret `secretAccessor` via `for_each`; only project-wide role is `logging.logWriter`. | `gcloud projects get-iam-policy relops-bootstrap --flatten='bindings[].members' --filter='bindings.members:vault-broker-run'` — expect only `secretAccessor` (per-secret) + `logWriter`. |
| **I2** | `allUsers` holds `run.invoker` on the broker — safe because of **N2**, not a hole. | `terraform/vault_broker.tf`. | `gcloud run services get-iam-policy vault-broker --region us-central1` shows `allUsers`/`roles/run.invoker`; combined with **N2** the URL is unreachable off-LB. No Google IAM token gates the broker — all auth is mTLS + header checks + ingress. |
| **I3** | step-ca VM SA has no secret access (only log/metric write). | `terraform/iam.tf`. | `gcloud projects get-iam-policy … --filter='bindings.members:step-ca-vm'`. |

### Audit

| # | Claim | Enforced in | Verify |
|---|---|---|---|
| **A1** | Every served secret is logged with cert serial, role, host, bytes; denials logged too. | `broker/app/main.py:114-120` (`secret.served`); `auth.failed`/`auth.rate_limited`/`auth.role_mismatch`. `auth.failed` omits the serial (not yet trusted). | `gcloud logging read 'resource.type="cloud_run_revision" resource.labels.service_name="vault-broker" jsonPayload.event="secret.served"' --limit 20`. |
| **A2** | GCP Cloud Audit Logs independently record every Secret Manager access. | Platform-level via the broker SA. | `gcloud logging read 'protoPayload.serviceName="secretmanager.googleapis.com" protoPayload.methodName="google.cloud.secretmanager.v1.SecretManagerService.AccessSecretVersion"' --limit 20`. |

### Certificate / key handling

| # | Claim | Enforced in | Verify |
|---|---|---|---|
| **C1** | macOS worker key is non-exfiltrable — keychain ACL restricts signing to OS network-stack helpers; key not extractable. | `mdm/scep-relops.mobileconfig.template` — `KeyIsExtractable=false`, `AllowAllAppsAccess=false`, `Keysize=2048`, `Key Type=RSA`. Design rationale in `docs/deep-dive.html` §5. | On a worker: `security find-identity -v /Library/Keychains/System.keychain`; attempt export → `errSecInteractionNotAllowed`. |
| **C2** | Each cert is bound to exactly one role via the provisioner's x509 template — the worker has no say in its role. | `scripts/bootstrap-step-ca.sh` — SPIFFE URI template `spiffe://relops.mozilla/host/{CN}/role/<role>` rendered per provisioner. | `gcloud compute ssh step-ca --zone us-central1-a --tunnel-through-iap --command 'sudo -u step step ca provisioner list'`. |
| **C3** | Cert lifetime is 24h. | **Not pinned** — `ca.json` has empty global claims and no per-provisioner overrides, so this is step-ca's *default* (24h). | `gcloud compute ssh step-ca … --command 'sudo grep -i claims /home/step/.step/config/ca.json'` → confirm none set. **Recommendation: pin `maxTLSCertDuration`/`defaultTLSCertDuration` explicitly if the threat model is to rely on it.** |

---

## Residual risks we do not fully mitigate (stated honestly)

These are covered in depth in `docs/deep-dive.html` §8; summarized here so a
reviewer sees them up front rather than discovering them:

- **Shared SCEP challenge.** One challenge per provisioner, stored in Secret
  Manager + embedded in the SimpleMDM profile. A leak + MDC1 network access lets
  an attacker enroll a cert for that role. Bounded by Cloud Armor (**N1**). Not
  yet rotated or per-role. This is the lightest crypto in the system.
- **step-ca compromise** mints any cert → reads any vault. Bounded by IAP-only
  SSH, no public ingress, encrypted key at rest. Same trust we place in GCP
  admins fleet-wide.
- **Broker compromise** reads all `vault-*` secrets — same blast radius as an
  operator's 1Password access. Surface kept tiny (≈150 lines, no crypto of its
  own beyond leaf parsing).
- **In-memory rate limit** (B4) does not coordinate across Cloud Run instances.
- **Linux key on disk.** `/etc/relops-bootstrap/key.pem` (root:root 0600) is
  copyable by root — but root can already read the resulting `vault.yaml`.
- **CRL / revocation** not implemented; 24h cert lifetime is the current bound.

## Current rollout status (verify before trusting a claim of "live")

Only the **m4 family is live end-to-end** (provisioner + populated secret):
`gecko_t_osx_1500_m4`, `…_m4_staging`, `…_m4_no_sip`. The six `gecko_t_linux_*`
roles have provisioners but **empty** secrets (not functional). `r8`/`1015` are
not started. Verify:

```bash
for s in $(gcloud secrets list --format='value(name)' | grep '^vault-'); do
  echo "$(gcloud secrets versions list "$s" --filter=state=enabled --format='value(name)' | wc -l | tr -d ' ') $s"
done
```

## Decommissioned surface

The **cron provisioner** (`relops-provisioner` Cloud Run + `relops-provisioner-tick`
Cloud Scheduler + its SAs, IAM, allowlist/kill-switch secrets, and GCS state
bucket) — a superseded path that fired SimpleMDM script-jobs — was
**decommissioned (2026-07)** in favor of the signed PKG + Hangar runner. It is
removed from Terraform and torn down in GCP. The shared `simplemdm-api-token`
secret was retained (relocated to `terraform/simplemdm_secret.tf`). Confirm:

```bash
gcloud run services list --format='value(metadata.name)'          # only vault-broker
gcloud scheduler jobs list --location=us-central1                 # empty
gcloud secrets list --format='value(name)' | grep provisioner-    # empty
gcloud secrets list --format='value(name)' | grep simplemdm       # simplemdm-api-token (retained)
```

## Terraform apply safety

`terraform plan` should be quiet. Two things a reviewer/operator must know:

- **The step-ca boot image is pinned** to a specific version in `step_ca.tf`
  (`data.google_compute_image.debian` uses `name`, not `family`). This is
  deliberate: a `family` lookup resolves to the latest image, so every apply
  would try to **replace the CA VM**. The CA keys live on a separate
  `prevent_destroy` persistent disk (`google_compute_disk.step_ca_state`,
  mounted at `/home/step`) and survive a VM replace, but a replace still swaps
  the boot disk and changes the internal IP — i.e. takes the CA offline. Bump
  the pinned image deliberately, never via a family auto-resolve.
- **Known benign drift:** the live `vault-broker` service carries an empty
  `scaling {}` block not present in config, so `plan` shows one in-place update
  (removing `min_instance_count`/`manual_instance_count` = 0). It changes no
  behavior; reconcile it deliberately, not as a side effect of another apply.
  Until then, prefer `-target` when applying unrelated changes.

## Reprovision safety guards (orchestrator)

The `reprovision` pipeline will not wipe a busy worker: quarantine → drain (2
consecutive idle polls) → **pre-wipe idle re-check that aborts if a task is
running** → Bootstrap-Token-escrow guard → EACS with
`obliteration_behavior=DoNotObliterate` (fails safe). See
`orchestrator/orchestrator/workflow.py` (`step_drain`, `step_wipe`) and
`clients/taskcluster.py` (`is_currently_busy`). Secrets are resolved at runtime
(env → `op://` 1Password → GCP Secret Manager); none are hardcoded
(`orchestrator/orchestrator/config.py`, `secrets.py`).
