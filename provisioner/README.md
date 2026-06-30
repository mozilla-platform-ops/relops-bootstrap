# relops-provisioner

Cloud Run polling reconciler. Every 5 minutes, Cloud Scheduler invokes the
`/run` endpoint. The service walks the configured SimpleMDM assignment
groups, evaluates 6 safety guards per device, and creates a SimpleMDM
script-job against any device that passes all 6.

## The 6 guards (default-deny)

All must pass before the script-job fires. See `app/guards.py` for the
canonical implementations and the one-sentence "why" comment on each.

1. **`kill_switch`** — `provisioner-enabled` Secret Manager value is exactly `true`
2. **`allowlist`** — hostname in the explicit `provisioner-allowlist` Secret Manager value
3. **`not_locked`** — SimpleMDM device's `provisioning_locked` Custom Attribute is not `true`
4. **`mdm_state`** — SimpleMDM reports `dep_enrolled` AND `is_user_approved_enrollment` AND `is_supervised`, all True. Necessary preconditions for Bootstrap Token escrow and future EACS-ability. Not sufficient — SimpleMDM doesn't expose whether the BST landed on the privileged user (admin) vs the task user (cltbld) — but catches the easier misconfiguration cases.
5. **`rate_limit`** — this device hasn't had a script-job fired within the last 24 hours
6. **`tc_state`** — composite Taskcluster check: device is eligible iff (no TC record at all) OR (TC record exists AND device is currently quarantined). Quarantine is operator consent — the explicit "I'm refreshing this worker" signal. An active TC registration that isn't quarantined means the worker is in production service: hands off.

## Operator workflow for re-provisioning an existing worker

1. Make sure the host is in the `provisioner-allowlist` secret (one-time per host)
2. Quarantine the worker in Taskcluster (signals re-provisioning consent)
3. EACS the host (wipes local disk)
4. Within ≤5 min, the provisioner's next scheduler tick fires the bootstrap script-job against the host
5. Worker re-registers with Taskcluster after bootstrap completes
6. Un-quarantine the worker in Taskcluster — it's back in service

Steps 1–2 are the explicit operator opt-in; step 3 is the disruptive action;
steps 4–5 are the zero-touch part.

## Removed guards (history)

- **`no_prior_success`** — checked SimpleMDM script-job history per device. SimpleMDM's `GET /script_jobs` doesn't expose per-device outcomes (only aggregate counts), so it wasn't implementable against the public API. Future replacement: have the bootstrap script write a `provisioned_at` SimpleMDM custom attribute on completion and add a guard that checks for it.
- **`tc_not_alive`, `no_recent_task`, `not_quarantined`** (the original three TC guards) — collapsed into the composite `tc_state` guard above. The originals treated quarantine as a hard veto ("operator pulled this, don't touch"), which broke the re-provisioning workflow where quarantine *is* the consent signal. The new composite makes quarantine positive consent and treats "alive in TC without quarantine" as the only true do-not-touch state.

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
