# relops-provisioner

Cloud Run polling reconciler. Every 5 minutes, Cloud Scheduler invokes the
`/run` endpoint. The service walks the configured SimpleMDM device groups,
evaluates 7 safety guards per device, and creates a SimpleMDM script-job
against any device that passes all 7.

## The 7 guards (default-deny)

All must pass before the script-job fires. See `app/guards.py` for the
canonical implementations and the one-sentence "why" comment on each.

1. **`kill_switch`** — `provisioner-enabled` Secret Manager value is exactly `true`
2. **`allowlist`** — hostname in the explicit `provisioner-allowlist` Secret Manager value
3. **`not_locked`** — SimpleMDM device's `provisioning_locked` Custom Attribute is not `true`
4. **`rate_limit`** — this device hasn't had a script-job fired within the last 24 hours
5. **`tc_not_alive`** — Taskcluster doesn't show the worker checked in within last 10 min
6. **`no_recent_task`** — Taskcluster shows no task completion in last 6 hours
7. **`not_quarantined`** — Taskcluster `quarantineUntil` is not in the future

## Removed guard: `no_prior_success`

An earlier design had an 8th guard that checked SimpleMDM script-job history
per-device. SimpleMDM's `GET /script_jobs` doesn't expose per-device outcomes
(only aggregate counts across whatever device set the job targeted), so the
guard wasn't implementable against the public API.

The replacement plan is to have the bootstrap script set a `provisioned_at`
SimpleMDM custom attribute on completion (SimpleMDM's `POST /script_jobs`
accepts a `custom_attribute` parameter for exactly this) and add a new guard
that checks for that attribute. Until that's wired up, the protection that
guard would have given is approximately covered by `tc_not_alive` (already-
provisioned workers register in TC and look alive) and `rate_limit` (24h
floor between fires per device).

## Default-safe deployment

The service ships defaulting to **DRY_RUN** mode. In dry-run, every
"would_fire" decision is logged with the device hostname, role, and
script-job id, but no SimpleMDM API mutation happens. Logs go to
Cloud Logging where they can be queried.

To go live:

1. Populate `simplemdm-api-token` secret with a real SimpleMDM API token
2. Add hostnames to `provisioner-allowlist` (newline-separated, `#` comments)
3. Set `provisioner-enabled` to `true`
4. Set `target_groups` in `app/config.py` to the real (SimpleMDM group id) → (puppet role, script-job id) map
5. Set `provisioner_dry_run = false` in `terraform.tfvars` and `terraform apply`

Each of these is a one-line operator action and is independently reversible.

## Kill switch

To halt all firing immediately without redeploying:

```bash
gcloud secrets versions add provisioner-enabled \
  --data-file=- --project=relops-bootstrap <<< "false"
```

Next tick (within 5 min) the service will see `kill_switch_enabled=false`
and skip every device with reason "kill switch disabled".

## Observability

Every tick logs a `reconciler.summary` line with counts of evaluated,
skipped, fired, would_fire. Per-device decisions log as
`device.skipped` (with `reason`) or `device.would_fire` / `device.fired`.

Query in Cloud Logging:

```
resource.type="cloud_run_revision"
resource.labels.service_name="relops-provisioner"
jsonPayload.event="device.fired"
```

## Adding a new role to auto-provisioning

1. Get the SimpleMDM group id (URL when viewing the group in the SimpleMDM web UI)
2. Get the SimpleMDM script-job id (URL of the bootstrap script-job)
3. Add the entry to `target_groups` in `app/config.py`:
   ```python
   "151249": {"role": "gecko_t_osx_1500_m4", "script_job_id": 87654},
   ```
4. Re-deploy. Until the per-host conditions are met, no script-job fires.
