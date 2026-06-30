"""
The six safety guards that decide whether a device is eligible for
automated script-job firing. Default-deny — *all* must pass; any single
failure means "skip this device."

This is the load-bearing safety surface. Read this file when changing
any provisioning logic. If you can't explain why a guard exists in one
sentence, don't change it.

Design pivot (2026-06-30): the original eight guards treated quarantine
as a hard veto ("operator pulled this worker, don't touch"). But that
broke the re-provisioning workflow, where quarantine is the operator's
signal that the worker is ready for refresh ("quarantine → EACS →
auto-fire → un-quarantine"). The three independent TC guards
(tc_not_alive, no_recent_task, not_quarantined) were collapsed into
one composite `tc_state` guard with this semantic:

    fire is allowed iff (no TC record at all)
                     OR (TC record + worker is currently quarantined)

i.e. quarantine becomes positive operator consent rather than negative
veto. The allowlist (guard #2) remains the load-bearing opt-in — to
provision/re-provision a host, the operator must (a) allowlist it AND
(b) quarantine it.

Historical: an earlier no_prior_success guard checked SimpleMDM
script-job history per device. SimpleMDM's list endpoint doesn't expose
per-device outcomes (only aggregate counts), so it was removed. Future
replacement: bootstrap script writes a `provisioned_at` custom
attribute on completion; new guard checks that attribute.
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
    hostname: str          # short hostname; used as TC workerId
    role: str              # e.g. "gecko_t_osx_1500_m4"
    group_assigned_at: datetime
    custom_attributes: dict[str, str]
    # MDM preconditions for Bootstrap Token escrow / future EACS-ability.
    # All three must be True for SimpleMDM to be able to manage the BST
    # flow at all — necessary, not sufficient.
    dep_enrolled: bool
    is_user_approved_enrollment: bool
    is_supervised: bool


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
# Guard 2: composite Taskcluster state — operator-intent check
# ---------------------------------------------------------------------------
# Eligibility logic (see module docstring for the why):
#   * no TC record    → eligible (new HW, or aged-out registration)
#   * record + quarantined → eligible (operator's "refresh me" signal)
#   * record + not quarantined → skip (worker is in production service)
#
# Quarantine is operator consent here, not veto. The allowlist (guard 3)
# is the explicit opt-in that gates which hosts this logic even applies to.
def guard_tc_state(device: Device, tc: TaskclusterClient) -> GuardResult:
    info = tc.get_worker(device.role, device.hostname)
    if info is None:
        return GuardResult(True, "no TC record — eligible (fresh or aged-out)")
    quarantine_until = info.get("quarantineUntil")
    if quarantine_until and quarantine_until > datetime.now(timezone.utc):
        return GuardResult(True, f"quarantined until {quarantine_until.isoformat()} — operator consent")
    return GuardResult(False, "worker is registered in TC and not quarantined — in service, hands off")


# ---------------------------------------------------------------------------
# Guard 3: MDM preconditions for Bootstrap Token / future EACS
# ---------------------------------------------------------------------------
# Bootstrap Token escrow requires DEP enrollment + User-Approved MDM
# Enrollment + Supervised mode. If any of these isn't True, future EACS
# commands will silently fail and re-provisioning won't work — better to
# refuse to fire now than to leave a host in a state that can't be
# managed later.
#
# This is a NECESSARY but NOT SUFFICIENT check. SimpleMDM doesn't expose
# whether the BST actually landed on the privileged user (admin) vs the
# task user (cltbld) — that gotcha can still bite even with this guard
# passing. The guard catches the easier "MDM doesn't have the rights"
# misconfiguration cases.
def guard_mdm_state(device: Device) -> GuardResult:
    missing = []
    if not device.dep_enrolled:
        missing.append("dep_enrolled")
    if not device.is_user_approved_enrollment:
        missing.append("is_user_approved_enrollment")
    if not device.is_supervised:
        missing.append("is_supervised")
    if missing:
        return GuardResult(False, f"MDM preconditions not met: {', '.join(missing)} = False")
    return GuardResult(True, "DEP + UAE + supervised — MDM has BST management rights")


# ---------------------------------------------------------------------------
# Guard 4: per-device rate limit
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
# Guard 5: global kill switch
# ---------------------------------------------------------------------------
# A Secret Manager value the operator can flip to halt all firing
# without redeploying. Read on every tick.
def guard_kill_switch(enabled: bool) -> GuardResult:
    if enabled:
        return GuardResult(True, "kill switch enabled (firing allowed)")
    return GuardResult(False, "kill switch disabled (auto-provisioning paused)")


# ---------------------------------------------------------------------------
# Guard 6: per-device opt-out via SimpleMDM Custom Attribute
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
    # (rate limit, no API), then Taskcluster (one API call).
    results = [
        ("kill_switch",  guard_kill_switch(kill_switch_enabled)),
        ("allowlist",    guard_allowlist(device, allowlist)),
        ("not_locked",   guard_not_locked(device)),
        ("mdm_state",    guard_mdm_state(device)),
        ("rate_limit",   guard_rate_limit(device, state)),
        ("tc_state",     guard_tc_state(device, tc)),
    ]
    return GuardEvaluation(device=device, results=results)
