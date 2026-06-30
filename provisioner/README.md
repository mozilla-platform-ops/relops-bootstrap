# relops-provisioner

> Cold Mac mini in a rack. Cron tick from Cloud Scheduler. Six guards say yes.
> Bootstrap script fires through SimpleMDM. Worker re-registers in Taskcluster.
> Nobody touched a keyboard.

That's the whole pitch. It's the last load-bearing manual step in the Mozilla
RelOps macOS bootstrap — the bit where someone had to SSH or click a button
to push the bootstrap script after EACS — collapsed into a Cloud Run service
that polls every five minutes and decides for itself.

## What it does in one tick

```
Cloud Scheduler ───POST /run──► Cloud Run service
                                       │
                                       ├──► SimpleMDM /assignment_groups/{id}
                                       │      "who lives here?"
                                       │
                                       ├──► Taskcluster Queue API
                                       │      "is this host alive? quarantined? cold?"
                                       │
                                       ├──► GCS rate-limit bucket
                                       │      "did we fire on this host in the last 24h?"
                                       │
                                       ├──► Secret Manager (3 secrets)
                                       │      "kill switch on? hostname allowlisted?"
                                       │
                                       └──► [if all six guards pass]
                                            SimpleMDM /script_jobs POST
                                            → bootstrap script delivered → puppet runs → worker comes back
```

One inbound RPC (from Scheduler), six guard checks, at most one outbound write
(to SimpleMDM). Everything else is read-only.

## The six guards (default-deny)

Every guard must pass — `evaluate_all_guards` is an `all()`, not an `any()`.
The order is cheapest-first so the logs surface useful per-device telemetry
even when something fails early.

| # | Guard | What it checks | Failure mode |
|---|---|---|---|
| 1 | `kill_switch` | `provisioner-enabled` Secret Manager value is exactly `true` | Emergency stop — one secret edit halts every fire on the next tick |
| 2 | `allowlist` | Hostname is in the `provisioner-allowlist` secret | Per-host opt-in. Empty allowlist → nothing fires, ever |
| 3 | `not_locked` | Device's SimpleMDM `provisioning_locked` Custom Attribute is not `true` | Operator pin: park a single host as untouchable without redeploying |
| 4 | `mdm_state` | SimpleMDM reports `dep_enrolled` + `is_user_approved_enrollment` + `is_supervised`, all True | BST escrow preconditions. Catches misconfigured enrollments before they cause silent EACS failures later |
| 5 | `rate_limit` | Host hasn't had a fire recorded in the last 24h (GCS-backed) | Defense against guard-logic bugs. Burning the same host every five minutes would be bad |
| 6 | `tc_state` | (No TC record) OR (TC record AND currently quarantined) | Production protection. Active workers are off-limits; quarantine is the operator's "refresh me" consent signal |

See `app/guards.py` — one function per guard, each with a one-sentence "why"
comment. If you can't summarize a guard in one sentence, don't change it.

## The re-provisioning workflow

The whole point. Five steps, only one of which involves an operator hand:

1. **Allowlist the host** in the `provisioner-allowlist` Secret Manager value
   *(one-time per host, not per cycle)*
2. **Quarantine the worker** in Taskcluster *(your consent signal: "I want
   this one refreshed")*
3. **EACS the device** via SimpleMDM *(the only disruptive action)*
4. *(zero-touch from here)* Provisioner's next tick fires the bootstrap
   script → puppet runs → worker re-registers in TC
5. **Un-quarantine** in Taskcluster, worker is back in service

Steps 1–3 are deliberate operator actions. Step 4 is the part this service
exists to automate. Step 5 is a single click. The legacy alternative was an
operator sitting at a terminal walking each host through a runbook one step
at a time.

## Default-safe deployment posture

Three independent "do nothing" defaults stack on top of each other. You can
break any one of them and the other two still hold:

- `DRY_RUN=true` env var on the Cloud Run service. The firing branch in
  `main.py` is structurally unreachable under dry-run — `simplemdm.create_script_job`
  is only called inside `else: # not dry_run`.
- `provisioner-allowlist` secret starts empty (or missing). Empty allowlist
  → every device skipped with reason `allowlist`.
- `provisioner-enabled` secret starts unset. Anything other than the literal
  string `true` trips the kill switch, every device skipped.

Flipping to live requires three deliberate operator actions on three
different surfaces. Mis-clicking one of them doesn't ship anything.

## Observability

Every tick emits one `reconciler.summary` line and one `device.skipped` or
`device.would_fire` / `device.fired` per evaluated host. The `all_results`
field on a skip tells you *every* guard's vote so you can see which one
caught it (and which ones would have caught it next).

```bash
# Tail decisions live
gcloud logging tail \
  'resource.type="cloud_run_revision"
   resource.labels.service_name="relops-provisioner"' \
  --project=relops-bootstrap \
  --format="value(textPayload)"

# All fires in the last day
gcloud logging read \
  'resource.type="cloud_run_revision"
   resource.labels.service_name="relops-provisioner"
   textPayload=~"device.fired"' \
  --project=relops-bootstrap --freshness=1d
```

## Killing it without a redeploy

```bash
# Stop all fires on the next tick (≤5 min)
printf 'false' | gcloud secrets versions add provisioner-enabled \
  --project=relops-bootstrap --data-file=-

# Pause the scheduler entirely (no ticks at all)
gcloud scheduler jobs pause relops-provisioner-tick \
  --project=relops-bootstrap --location=us-central1
```

Resume via `gcloud secrets versions add provisioner-enabled` with `true`,
or `gcloud scheduler jobs resume ...`.

## Smoke-test playbook

Three tests verify the safety surface against live state without firing
anything real. Each is one secret-flip + one manual tick:

### Allowlist guard

```bash
# Empty the allowlist
printf '# empty\n' | gcloud secrets versions add provisioner-allowlist \
  --project=relops-bootstrap --data-file=-
gcloud scheduler jobs run relops-provisioner-tick \
  --project=relops-bootstrap --location=us-central1
# Expect: device.skipped reason='allowlist: hostname ... not on allowlist'
# Restore by re-adding the hostname.
```

### Kill switch

```bash
printf 'false' | gcloud secrets versions add provisioner-enabled \
  --project=relops-bootstrap --data-file=-
gcloud scheduler jobs run relops-provisioner-tick \
  --project=relops-bootstrap --location=us-central1
# Expect: device.skipped reason='kill_switch: ...'
# Restore: same command with 'true'.
```

### Production protection (live test, won't fire)

Run against a TC-active, non-quarantined production worker. Expect
`device.skipped reason='tc_state: worker is registered in TC and not
quarantined ...'`. This is the most important guard; validating it
against real production state was a stated goal of the design.

## Configuration knobs

All operator-tunable settings live in `app/config.py` as a Pydantic
`Settings` class. The few you actually touch:

- `dry_run` (env: `DRY_RUN`) — default `true`. Flip via terraform var
  `provisioner_dry_run` and `terraform apply`.
- `target_groups` — the `{SimpleMDM assignment_group_id → {role, script_id}}`
  map. Adding a new role to auto-provisioning is a code change here; that
  friction is intentional (it needs a PR review).
- `rate_limit_per_device_hours` — default 24. Don't go below 1 without
  thinking about it.

Secrets are referenced by Secret Manager name only — the values themselves
live in GCP, never in code.

## Deployment

```bash
# Build + push
cd provisioner
gcloud builds submit \
  --tag=us-central1-docker.pkg.dev/relops-bootstrap/relops-provisioner/relops-provisioner:vX.Y.Z \
  --project=relops-bootstrap .

# Roll the new image (terraform owns service shape, gcloud owns image)
gcloud run deploy relops-provisioner \
  --image=us-central1-docker.pkg.dev/relops-bootstrap/relops-provisioner/relops-provisioner:vX.Y.Z \
  --region=us-central1 --project=relops-bootstrap --quiet
```

Infrastructure (Cloud Run + Cloud Scheduler + Secret Manager + GCS bucket +
Artifact Registry + IAM) lives in `terraform/provisioner.tf` and applies
with `terraform apply`.

## Known limits and follow-ups

A short, honest catalog of what this version doesn't do yet:

- **No `provisioned_at` custom attribute check.** SimpleMDM doesn't expose
  per-device script-job outcomes via the API, so an idempotency guard against
  "this host was already auto-bootstrapped" can't be built from script-job
  history. The intended fix: have the bootstrap script write a
  `provisioned_at` SimpleMDM custom attribute on success (their
  `POST /script_jobs` accepts a `custom_attribute` parameter for this), and
  add a guard reading it.
- **BST-on-cltbld blind spot.** `mdm_state` (guard 4) checks the *enrollment*
  preconditions for Bootstrap Token escrow but can't see *which user* holds
  the escrowed token. The known gotcha — admin needs to GUI-login first on
  M-series so BST doesn't land on cltbld — still requires manual hygiene at
  bootstrap time.
- **Power-management profile dependency.** A Mac without a power-management
  profile pushed via MDM will sleep mid-bootstrap and stall script delivery
  until something (an SSH login, a button press) wakes it. Production
  assignment groups need the power-management profile applied; the no-sip
  test assignment group hit this exact issue and got the profile pushed.
- **Single-region.** Cloud Run, Cloud Scheduler, and the state bucket are
  all `us-central1`. Not a problem at fleet scale, just noting it.

## File-by-file tour

| File | Purpose |
|---|---|
| `app/main.py` | FastAPI app, `POST /run` handler, orchestration loop. Lifespan is intentionally side-effect-free so transient failures don't crash-loop the container. |
| `app/guards.py` | The six guards, the `Device` dataclass, the `evaluate_all_guards` orchestrator. Most important file in the repo for safety review. |
| `app/config.py` | Pydantic settings. Defaults are safe-by-construction. |
| `app/simplemdm.py` | Thin SimpleMDM API client. Only the endpoints we actually use. |
| `app/taskcluster.py` | Thin TC client. Uses the Queue API, not worker-manager — hardware workers live on the queue side. |
| `app/state.py` | GCS-backed `last_fired_at` per device. Dumb-simple key → ISO timestamp. |
| `app/secrets.py` | Secret Manager helper. |
| `Dockerfile` | Python 3.11-slim + uvicorn on port 8080. |
