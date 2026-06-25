# vault-broker

FastAPI service that brokers Secret Manager reads for MDM-enrolled CI workers
authenticated via SCEP-issued client certs.

## What it does

1. Worker has a SCEP-issued client cert (private key in Secure Enclave). Cert's
   SAN encodes a SPIFFE URI: `spiffe://relops.mozilla/host/<hostname>/role/<puppet_role>`.
2. Worker mints a short-lived (~60s) JWT signed by the cert's private key, with the
   cert chain in the `x5c` header and a unique `jti`.
3. Worker calls `GET /secret/<role>` with `Authorization: Bearer <jwt>`.
4. Broker:
   - Verifies the cert chain against the step-ca root cert.
   - Verifies the cert serial is **not on the step-ca CRL** (when polling is enabled).
   - Verifies the JWT signature using the cert's public key.
   - Validates JWT claims (`aud`, `exp`, `iat`, `jti`) against an **algorithm whitelist** (default `RS256, ES256`).
   - Bounds JWT lifetime (default ≤ 300s).
   - Checks **`jti` against a replay cache** (returns 401 on repeats within TTL).
   - Applies a **per-cert-serial token-bucket rate limit** (default 60 req/min, burst 10).
   - Extracts (hostname, role) from the cert SAN.
   - Checks the URL role matches the cert SAN role (403 on mismatch).
   - Reads `vault-<role>` from Secret Manager.
   - Returns the YAML content (with structured-log audit trail).

## Local development

```bash
# Install deps (uses pip and editable install; will move to uv if Hangar does)
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

# Run tests
pytest -v

# Run the server locally (will fail at startup because no real GCP creds —
# tests are the right way to validate the auth flow without GCP)
uvicorn app.main:app --reload
```

## Config (environment variables)

| Var | Default | Purpose |
|---|---|---|
| `GCP_PROJECT_ID` | (auto on Cloud Run) | Project housing Secret Manager |
| `STEP_CA_ROOT_CERT_SECRET` | `step-ca-root-cert` | SM secret holding root PEM |
| `JWT_AUDIENCE` | broker URL | Expected `aud` claim |
| `JWT_LEEWAY_SECONDS` | `10` | Clock-skew tolerance |
| `JWT_MAX_LIFETIME_SECONDS` | `300` | Max acceptable JWT lifetime |
| `JWT_ALLOWED_ALGS_CSV` | `RS256,ES256` | JWT alg whitelist (CSV; `alg=none`/HS* are not accepted) |
| `JWT_REQUIRE_JTI` | `true` | Require jti claim for replay protection |
| `JTI_CACHE_TTL_SECONDS` | `360` | How long jti is considered "seen" |
| `JTI_CACHE_MAX_SIZE` | `10000` | LRU bound on jti cache |
| `RATE_LIMIT_REQUESTS_PER_MINUTE` | `60` | Per-cert-serial steady-state rate |
| `RATE_LIMIT_BURST` | `10` | Per-cert-serial burst capacity |
| `CRL_URL` | (empty) | step-ca CRL URL; empty = revocation check disabled |
| `CRL_REFRESH_SECONDS` | `300` | Background refresh interval |
| `CRL_FETCH_TIMEOUT_SECONDS` | `15` | Per-fetch HTTP timeout |
| `SPIFFE_TRUST_DOMAIN` | `relops.mozilla` | Validates SAN URI prefix |
| `LOG_JSON` | `true` | Structured logs for Cloud Logging |
| `LOG_LEVEL` | `INFO` | |

## Auth model details

**What the cert proves**: that step-ca (and therefore the relops PKI we control)
issued this cert to a host with the SAN role/hostname encoded in it. Compromise of
a host means compromise of *that host's* role, not the fleet.

**What the JWT proves**: liveness — proves the cert's private key is on the calling
host *right now*, not just that the cert exists. Short lifetime (default 60s) bounds
the replay window.

**What we deliberately *don't* check**: the JWT body's contents (other than the standard
claims). The cert SAN is authoritative; the JWT body is informational. A host that tries
to fetch a role its cert isn't authorized for gets a 403.

## Logs

Every served request produces a structured log line: `secret.served` with fields
`role`, `hostname`, `cert_serial`, `bytes`. Failures produce `auth.failed` /
`auth.role_mismatch` / `secret.read_failed`.

GCP Cloud Audit Logs ALSO mirror every Secret Manager read at the platform layer
(via the broker's SA), independent of our app logging. Two audit trails: ours
(rich, with cert serial) and GCP's (canonical, tamper-resistant).
