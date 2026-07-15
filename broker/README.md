# vault-broker

FastAPI service that brokers Secret Manager reads for CI workers authenticated
by an SCEP-issued **mTLS client cert**. Given a request that the load balancer
has already mTLS-authenticated, the broker maps the cert's embedded puppet role
to a `vault-<role>` secret and returns it.

> **Auth model at a glance:** the GCP HTTPS Load Balancer terminates mTLS and
> validates the client-cert chain against its Trust Config (step-ca root +
> intermediate). It forwards the parsed cert to the broker as request headers.
> The broker does **not** do any cryptography beyond parsing the leaf to read
> the role out of the SAN — it trusts the LB's chain verdict. There is **no
> JWT, no bearer token, no replay cache, and no CRL** in the running service.
> (An earlier design used a cert-signed JWT; it was abandoned because macOS
> will not let non-Apple code sign with a keychain key — see the repo
> [deep-dive](../docs/deep-dive.html), "Dead end 1".)

## Request flow

1. A worker holds an SCEP-issued client cert whose SAN carries a SPIFFE URI:
   `spiffe://relops.mozilla/host/<hostname>/role/<puppet_role>`.
2. The worker makes an mTLS `GET /secret/<role>` to `forge.relops.mozilla.com`
   (macOS via `CURL_SSL_BACKEND=securetransport curl --cert "<CN>"`, Linux via
   `curl --cert cert.pem --key key.pem`).
3. The **load balancer** terminates TLS, validates the client-cert chain, and
   forwards the request to this service (Cloud Run) with these headers injected:

   | Header | Meaning |
   |---|---|
   | `X-Client-Cert-Present` | `true` if the client presented a cert |
   | `X-Client-Cert-Chain-Verified` | `true` if the chain validated against the Trust Config |
   | `X-Client-Cert-Error` | LB validation error code, if any |
   | `X-Client-Cert-Serial-Number` | hex serial — used as the audit + rate-limit key |
   | `X-Client-Cert-Leaf` | base64-encoded leaf PEM |

   The LB **overwrites** these exact header names on every request (an inbound
   client cannot spoof them), and Cloud Run ingress is locked to the LB
   (`internal-and-cloud-load-balancing`), so the broker is unreachable except
   through the LB. See [Trust boundary](#trust-boundary) — this is load-bearing.
4. The **broker** (`app/auth.py` → `app/main.py`):
   - Requires `X-Client-Cert-Present: true` **and** `X-Client-Cert-Chain-Verified: true`
     **and** a non-empty `X-Client-Cert-Leaf` — otherwise **401**.
   - Decodes the leaf PEM and extracts the SPIFFE URI from the SAN, **URL-decoding**
     it (so `Mac%20mini` → `Mac mini`; the LB's own SPIFFE parser drops URL-encoded
     URIs, which is why we parse the leaf ourselves). No SPIFFE URI in the expected
     shape → **401**.
   - Applies a per-cert-serial rate limit → **429** if exceeded.
   - Checks the role in the cert SAN **equals** the `{role}` in the URL path →
     **403** on mismatch (body names both roles).
   - Reads `vault-<role>` from Secret Manager via the broker's per-secret-bound
     service account and returns it as `text/yaml`. Missing/unreadable secret → **500**.

## Trust boundary

The broker performs **no chain verification of its own** — it trusts
`X-Client-Cert-Chain-Verified`. Two infrastructure facts make that safe, and a
security review should confirm both (see [`../SECURITY.md`](../SECURITY.md)):

- **Header integrity.** The LB sets the `X-Client-Cert-*` header values itself
  via `custom_request_headers` (`terraform/lb.tf`), overwriting any inbound
  values for those names. A client cannot present a forged
  `X-Client-Cert-Chain-Verified: true`.
- **No path around the LB.** Cloud Run ingress is
  `INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER` (`terraform/vault_broker.tf`), so the
  service only accepts traffic from the LB / internal sources. The Cloud Run URL
  is not directly reachable from the internet even though `allUsers` holds
  `roles/run.invoker` — the invoker grant is the standard pattern for an
  LB-fronted service and is gated entirely by that ingress setting. **If the
  ingress setting were relaxed, a caller who could reach the URL directly and
  set the headers by hand would bypass authentication.** That is the single most
  important invariant in the service.

## Config (environment variables)

The running service reads exactly these settings (`app/config.py`); there are no
JWT/CRL/replay knobs:

| Var | Default | Purpose |
|---|---|---|
| `GCP_PROJECT_ID` | (auto on Cloud Run) | Project housing Secret Manager |
| `SPIFFE_TRUST_DOMAIN` | `relops.mozilla` | Validates the SAN URI prefix `spiffe://<domain>/host/…` |
| `RATE_LIMIT_REQUESTS_PER_MINUTE` | `60` | Per-cert-serial refill rate (token bucket, ≈1/sec) |
| `RATE_LIMIT_BURST` | `10` | Per-cert-serial burst capacity |
| `LOG_JSON` | `true` | Structured logs for Cloud Logging |
| `LOG_LEVEL` | `INFO` | |

## Rate limiting — a known limitation

The rate limiter (`app/replay_cache.py`, a token bucket keyed on cert serial) is
**in-memory and per-Cloud-Run-instance**. It slows brute-force enumeration from a
single instance's perspective, but it does **not** coordinate across instances,
so an autoscaled deployment enforces a higher effective aggregate limit than the
per-instance numbers suggest. It is a speed bump, not a hard quota. The upgrade
path (noted in the module) is a shared store (Memorystore/Redis). Do not describe
it as a hard global rate limit.

(The module/file is named `replay_cache` for historical reasons; the running
code contains only the rate limiter — there is no nonce/replay cache.)

## Logs / audit

Two independent audit trails:

- **Application logs** — every served request emits `secret.served` with `role`,
  `hostname`, `cert_serial`, `bytes`. Denials emit `auth.failed` (401),
  `auth.rate_limited` (429), `auth.role_mismatch` (403), or `secret.read_failed`
  (500). Note: `auth.failed` does not include the cert serial because the serial
  is not trusted until the leaf is parsed.
- **GCP Cloud Audit Logs** — mirror every Secret Manager access at the platform
  layer via the broker's service account, independent of app logging
  (tamper-resistant, canonical).

## What compromise buys an attacker

The broker's service account can read **only** the `vault-*` secrets plus the
`step-ca-root-cert` secret (per-secret IAM bindings in `terraform/iam.tf`; no
project-wide grant). A full broker compromise therefore exposes every role's
`vault.yaml` — the same blast radius as an operator's 1Password vault access. It
cannot sign anything, reach step-ca, or touch other GCP resources.

## Local development

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest -v          # auth flow is validated by tests; running the server needs real GCP creds
```

## Tests

`tests/test_main.py` covers the happy path plus the negative cases a reviewer
cares about: missing `Present` header → 401, `Chain-Verified: false` → 401,
empty leaf → 401, non-SPIFFE SAN → 401, role mismatch → 403, URL-encoded CN
(`Mac%20mini`) → 200, and rate-limit → 429. `tests/test_replay_cache.py` covers
the token bucket (burst, refill, per-key isolation).
