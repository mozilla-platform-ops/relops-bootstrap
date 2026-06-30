"""
relops-provisioner — Cloud Run service that polls SimpleMDM for devices
in target groups and fires per-device script-jobs against ones that need
provisioning, gated by the eight guards in app.guards.

Entry point is POST /run, invoked by Cloud Scheduler on a fixed cadence.
The service has internal-only ingress (no public endpoint); Cloud Scheduler
is the only authorized caller.

Default behavior is DRY_RUN — the service logs what it *would* do but
never actually fires a script-job. Flip DRY_RUN=false (via the Cloud Run
env var) only after eyeballing the dry-run logs.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

from .config import get_settings
from .guards import Device, evaluate_all_guards
from .secrets import read_secret
from .simplemdm import SimpleMDMClient
from .state import RateLimitState
from .taskcluster import TaskclusterClient

log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Lifespan is intentionally side-effect-free. Secrets and clients are
    # constructed inside /run so a missing-secret or transient-API failure
    # surfaces as a 5xx on one tick rather than crash-looping the container.
    # (The scheduler tries again 5 minutes later; we can fix the secret without
    # a redeploy.)
    settings = get_settings()
    log.info(
        "startup",
        project=settings.gcp_project_id,
        dry_run=settings.dry_run,
        target_groups=list(settings.target_groups.keys()),
    )
    yield


app = FastAPI(title="relops-provisioner", version="0.1.0", lifespan=lifespan)


@app.get("/healthz", response_class=PlainTextResponse)
async def healthz() -> str:
    return "ok"


@app.post("/run")
async def run_reconciler(request: Request) -> dict:
    """Single reconciliation pass.

    Invoked by Cloud Scheduler. Returns a summary dict that lands in
    Cloud Logging for at-a-glance observability."""

    settings = get_settings()

    # Construct clients on every tick. simplemdm token read fails closed:
    # if we cannot read the api token, do not pretend to evaluate anything.
    try:
        simplemdm = SimpleMDMClient(api_token=read_secret(settings.simplemdm_api_token_secret).decode())
    except Exception as e:
        log.error("simplemdm_token.read_failed", error=str(e))
        return {"error": "cannot read simplemdm api token", "detail": str(e)}

    tc = TaskclusterClient(root_url=settings.tc_root_url)
    rate_state = RateLimitState(
        bucket=settings.rate_limit_state_bucket,
        prefix=settings.rate_limit_state_prefix,
    )

    # Load operator-controlled state on every tick so changes take effect
    # without requiring a redeploy.
    try:
        allowlist_raw = read_secret(settings.allowlist_secret)
        allowlist = {line.strip() for line in allowlist_raw.decode().splitlines() if line.strip() and not line.startswith("#")}
    except Exception as e:
        log.error("allowlist.read_failed", error=str(e))
        return {"error": "cannot read allowlist", "detail": str(e)}

    try:
        kill_switch_raw = read_secret(settings.kill_switch_secret).decode().strip().lower()
        kill_switch_enabled = kill_switch_raw in ("true", "1", "yes", "on", "enabled")
    except Exception as e:
        log.error("kill_switch.read_failed", error=str(e))
        # Safer to fail closed: if we can't read the kill switch, assume disabled.
        kill_switch_enabled = False

    summary = {
        "dry_run": settings.dry_run,
        "kill_switch_enabled": kill_switch_enabled,
        "allowlist_size": len(allowlist),
        "groups_checked": 0,
        "devices_evaluated": 0,
        "devices_skipped": 0,
        "devices_fired": 0,
        "devices_would_fire": 0,
        "errors": [],
    }

    # Walk every configured (assignment_group, role, script_id) tuple
    for assignment_group_id, group_cfg in settings.target_groups.items():
        summary["groups_checked"] += 1
        try:
            raw_devices = simplemdm.list_devices_in_assignment_group(assignment_group_id)
        except Exception as e:
            log.error("simplemdm.list_devices_failed", assignment_group=assignment_group_id, error=str(e))
            summary["errors"].append({"assignment_group": assignment_group_id, "error": str(e)})
            continue

        for raw in raw_devices:
            # SimpleMDM, Taskcluster, and our allowlist all key off the short
            # hostname ("macmini-m4-81"). TC's workerId validator explicitly
            # rejects dots so FQDNs are wrong everywhere.
            device = Device(
                simplemdm_id=raw["id"],
                hostname=raw["hostname"],
                role=group_cfg["role"],
                group_assigned_at=raw["last_seen_at"],   # closest proxy SimpleMDM gives us
                custom_attributes=raw.get("custom_attributes") or {},
            )
            summary["devices_evaluated"] += 1

            eval_result = evaluate_all_guards(
                device,
                allowlist=allowlist,
                kill_switch_enabled=kill_switch_enabled,
                tc=tc,
                state=rate_state,
            )

            if not eval_result.passed:
                log.info(
                    "device.skipped",
                    hostname=device.hostname,
                    role=device.role,
                    reason=eval_result.first_failure,
                    all_results={name: r.passed for name, r in eval_result.results},
                )
                summary["devices_skipped"] += 1
                continue

            # All guards passed — fire or pretend to.
            if settings.dry_run:
                log.info(
                    "device.would_fire",
                    hostname=device.hostname,
                    role=device.role,
                    script_id=group_cfg["script_id"],
                )
                summary["devices_would_fire"] += 1
            else:
                try:
                    script_job_id = simplemdm.create_script_job(group_cfg["script_id"], device.simplemdm_id)
                    rate_state.record_fire(device.hostname)
                    log.info(
                        "device.fired",
                        hostname=device.hostname,
                        role=device.role,
                        script_id=group_cfg["script_id"],
                        script_job_id=script_job_id,
                    )
                    summary["devices_fired"] += 1
                except Exception as e:
                    log.error(
                        "device.fire_failed",
                        hostname=device.hostname,
                        error=str(e),
                    )
                    summary["errors"].append({"hostname": device.hostname, "error": str(e)})

    log.info("reconciler.summary", **summary)
    return summary
