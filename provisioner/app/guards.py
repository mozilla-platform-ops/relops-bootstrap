"""
The seven safety guards that decide whether a device is eligible for
automated script-job firing. Default-deny — *all* must pass; any single
failure means "skip this device."

This is the load-bearing safety surface. Read this file when changing
any provisioning logic. If you can't explain why a guard exists in one
sentence, don't change it.

Historical note: an eighth guard (no_prior_success) checked SimpleMDM
script-job history per device. SimpleMDM's list endpoint doesn't expose
per-device outcomes (only aggregate counts), so it was removed. The
intended future replacement: bootstrap script writes a
`provisioned_at` custom attribute on completion; new guard checks
that attribute. See README for the follow-up.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .config import get_settings
from .state import RateLimitState
from .taskcluster import TaskclusterClient


@dataclass(frozen=True)
class Device:
    """Minimum we need to evaluate guards for one device."""
    simplemdm_id: int
    hostname: str          # FQDN, used as Taskcluster worker_id
    role: str              # e.g. "gecko_t_osx_1500_m4"
    group_assigned_at: datetime
    custom_attributes: dict[str, str]


@dataclass(frozen=True)
class GuardResult:
    passed: bool
    reason: str            # short human-readable explanation


# ---------------------------------------------------------------------------
# Guard 1: hostname is on the explicit allowlist
# ---------------------------------------------------------------------------
# Default-deny. The allowlist lives in Secret Manager so it can be edited
# without redeploying the service. Empty list → never fire.
def guard_allowlist(device: Device, allowlist: set[str]) -> GuardResult:
    if device.hostname in allowlist:
        return GuardResult(True, "on allowlist")
    return GuardResult(False, f"hostname {device.hostname!r} not on allowlist")


# ---------------------------------------------------------------------------
# Guard 2: worker is not currently alive in Taskcluster
# ---------------------------------------------------------------------------
# Production-protection. If TC says the worker is currently in the pool and
# was last-checked recently, do NOT touch it — it's probably running tasks.
def guard_tc_not_alive(device: Device, tc: TaskclusterClient) -> GuardResult:
    info = tc.get_worker(device.role, device.hostname)
    if info is None:
        return GuardResult(True, "no TC worker record found (cold)")
    last_checked = info.get("lastChecked")
    if last_checked is None:
        return GuardResult(True, "TC worker has no lastChecked")
    age = datetime.now(timezone.utc) - last_checked
    threshold = timedelta(minutes=get_settings().tc_alive_threshold_minutes)
    if age < threshold:
        return GuardResult(False, f"worker checked in {age.total_seconds():.0f}s ago — alive")
    return GuardResult(True, f"last TC check-in {age.total_seconds():.0f}s ago (cold)")


# ---------------------------------------------------------------------------
# Guard 3: worker has not completed a task in the last N hours
# ---------------------------------------------------------------------------
# Production-protection, stronger than guard 2. Even if the worker isn't
# currently alive (it may be mid-reboot, network blip, etc), if it ran a
# task recently we treat it as "working" and refuse to disturb.
def guard_no_recent_task_activity(device: Device, tc: TaskclusterClient) -> GuardResult:
    most_recent = tc.most_recent_task_completion(device.role, device.hostname)
    if most_recent is None:
        return GuardResult(True, "no recent task history in TC")
    age = datetime.now(timezone.utc) - most_recent
    threshold = timedelta(hours=get_settings().tc_recent_task_threshold_hours)
    if age < threshold:
        return GuardResult(False, f"task completed {age.total_seconds()/3600:.1f}h ago")
    return GuardResult(True, f"no task activity for {age.total_seconds()/3600:.1f}h")


# ---------------------------------------------------------------------------
# Guard 4: worker is not quarantined in Taskcluster
# ---------------------------------------------------------------------------
# Quarantine means an operator intentionally pulled the worker. Auto-
# provisioning would un-do that intent. Hard skip.
def guard_not_quarantined(device: Device, tc: TaskclusterClient) -> GuardResult:
    info = tc.get_worker(device.role, device.hostname)
    if info is None:
        return GuardResult(True, "no TC worker record")
    quarantine_until = info.get("quarantineUntil")
    if quarantine_until and quarantine_until > datetime.now(timezone.utc):
        return GuardResult(False, f"quarantined until {quarantine_until.isoformat()}")
    return GuardResult(True, "not quarantined")


# ---------------------------------------------------------------------------
# Guard 5: per-device rate limit
# ---------------------------------------------------------------------------
# Even if all other guards pass, refuse to fire more than once per N hours
# against the same device. Defense against guard-logic bugs that could
# loop-fire.
def guard_rate_limit(device: Device, state: RateLimitState) -> GuardResult:
    last_fired = state.last_fired(device.hostname)
    if last_fired is None:
        return GuardResult(True, "no prior fire recorded")
    age = datetime.now(timezone.utc) - last_fired
    threshold = timedelta(hours=get_settings().rate_limit_per_device_hours)
    if age < threshold:
        return GuardResult(False, f"last fired {age.total_seconds()/3600:.1f}h ago < {threshold.total_seconds()/3600:.0f}h limit")
    return GuardResult(True, f"last fired {age.total_seconds()/3600:.1f}h ago")


# ---------------------------------------------------------------------------
# Guard 6: global kill switch
# ---------------------------------------------------------------------------
# A Secret Manager value the operator can flip to halt all firing
# without redeploying. Read on every tick.
def guard_kill_switch(enabled: bool) -> GuardResult:
    if enabled:
        return GuardResult(True, "kill switch enabled (firing allowed)")
    return GuardResult(False, "kill switch disabled (auto-provisioning paused)")


# ---------------------------------------------------------------------------
# Guard 7: per-device opt-out via SimpleMDM Custom Attribute
# ---------------------------------------------------------------------------
# If an operator sets `provisioning_locked=true` on a specific device in
# SimpleMDM, the reconciler never touches it. Useful for hand-tuned hosts.
def guard_not_locked(device: Device) -> GuardResult:
    if device.custom_attributes.get("provisioning_locked", "").lower() == "true":
        return GuardResult(False, "provisioning_locked=true on device")
    return GuardResult(True, "not locked")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GuardEvaluation:
    """Per-device result of running every guard, in order."""
    device: Device
    results: list[tuple[str, GuardResult]]   # [(guard_name, GuardResult), ...]

    @property
    def passed(self) -> bool:
        return all(r.passed for _, r in self.results)

    @property
    def first_failure(self) -> str | None:
        for name, r in self.results:
            if not r.passed:
                return f"{name}: {r.reason}"
        return None


def evaluate_all_guards(
    device: Device,
    *,
    allowlist: set[str],
    kill_switch_enabled: bool,
    tc: TaskclusterClient,
    state: RateLimitState,
) -> GuardEvaluation:
    """Run every guard against one device. Order matters for log readability
    but not correctness — all guards must pass."""

    # Order chosen so cheapest/most-categorical checks come first:
    # kill switch, allowlist, lock attribute (no API calls), local state
    # (rate limit, no API), then Taskcluster (two API calls).
    results = [
        ("kill_switch",       guard_kill_switch(kill_switch_enabled)),
        ("allowlist",         guard_allowlist(device, allowlist)),
        ("not_locked",        guard_not_locked(device)),
        ("rate_limit",        guard_rate_limit(device, state)),
        ("tc_not_alive",      guard_tc_not_alive(device, tc)),
        ("no_recent_task",    guard_no_recent_task_activity(device, tc)),
        ("not_quarantined",   guard_not_quarantined(device, tc)),
    ]
    return GuardEvaluation(device=device, results=results)
