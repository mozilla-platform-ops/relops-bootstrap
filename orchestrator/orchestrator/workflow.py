"""
The end-to-end EACS workflow as a single function. CLI subcommands call individual
steps for partial-failure recovery; `reprovision` calls them all in order.

The steps are deliberately small functions with clear in/out contracts so they're
re-runnable. If step N fails, the operator can resume by calling step N+1 directly
once they've fixed whatever was broken.
"""

from __future__ import annotations

import shlex
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import ui
from .clients import simplemdm, ssh, taskcluster
from .config import get_settings
from .errors import ReprovisionError
from .hostnames import validate_short
from .role_map import role_for_hostname
from .secrets import simplemdm_api_key, ssh_admin_key, ssh_admin_password, tc_credentials


@dataclass
class HostContext:
    hostname: str  # short name, e.g. macmini-m4-81
    fqdn: str  # full DNS name to ssh to
    role: str  # puppet role
    worker_pool_id: str  # e.g. releng-hardware/gecko-t-osx-1500-m4
    worker_group: str = "mdc1"
    simplemdm_device_id: int | None = None
    pre_wipe_enrolled_at: str | None = None  # captured by step_wipe; used to detect a *fresh* re-enroll


def resolve(hostname: str) -> HostContext:
    """Look up everything we need about a host from its short name."""
    # Validate before the name reaches SSH / expect / SimpleMDM / VNC. Every CLI
    # subcommand and the runner flow funnel through resolve(), so this one gate
    # constrains the whole orchestrator to well-formed fleet hostnames.
    hostname = validate_short(hostname)
    role = role_for_hostname(hostname)

    # A role backs a prod pool by convention, but the SAME role also backs its `-staging`
    # sibling (e.g. gecko_t_osx_1500_m4 → both gecko-t-osx-1500-m4 and -staging). The role
    # can't disambiguate, so resolve the real pool from where the worker is actually registered
    # in TC: probe staging first, then prod. If it's registered in neither (a fresh host not yet
    # booted into TC), fall back to the prod pool — quarantine/drain then no-op on the 404.
    prod_pool_by_role = {
        "gecko_t_osx_1500_m4": "releng-hardware/gecko-t-osx-1500-m4",
        "gecko_t_osx_1400_r8": "releng-hardware/gecko-t-osx-1400-r8",
    }
    base_pool = prod_pool_by_role.get(role)
    if not base_pool:
        raise ValueError(f"no worker pool mapping for role '{role}'")
    worker_group = "mdc1"
    pool = taskcluster.find_registered_pool([f"{base_pool}-staging", base_pool], worker_group, hostname) or base_pool

    fqdn = f"{hostname}.test.releng.mdc1.mozilla.com"
    device = simplemdm.find_device_by_name(hostname)
    device_id = device["id"] if device else None

    return HostContext(
        hostname=hostname,
        fqdn=fqdn,
        role=role,
        worker_pool_id=pool,
        simplemdm_device_id=device_id,
    )


def check() -> None:
    """Read-only preflight: confirm every credential resolves from the vault. No host, no API
    writes, no changes — safe to run anytime (onboarding a coworker, before a demo). Reports one
    line per credential and exits non-zero if any required one fails."""
    ui.step("CHECK", "resolving credentials from the RelOps vault (read-only)")
    problems = 0

    def _try(label: str, getter, *, required: bool = True) -> None:
        nonlocal problems
        try:
            val = getter()
        except ReprovisionError as e:
            ui.err(f"{label}: {e}")
            problems += 1
            return
        if val:
            ui.ok(f"{label} — resolves")
        elif required:
            ui.err(f"{label}: not configured (empty)")
            problems += 1
        else:
            ui.warn(f"{label}: not configured (optional — only needed for quarantine/drain)")

    _try("admin password", ssh_admin_password)
    _try("admin SSH key", ssh_admin_key)
    _try("SimpleMDM API key", simplemdm_api_key)
    _try("Taskcluster clientId", lambda: tc_credentials()[0], required=False)
    _try("Taskcluster token", lambda: tc_credentials()[1], required=False)

    if problems:
        raise ReprovisionError(f"{problems} credential(s) didn't resolve — see the ✗ line(s) above")
    ui.ok("all credentials resolve — you're good to go")


# --- workflow steps ---


def step_quarantine(ctx: HostContext) -> None:
    until = (datetime.now(timezone.utc) + timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    ui.step("QUARANTINE", "tell Taskcluster to stop scheduling tasks on this worker")
    ui.wire(f"PUT queue/v1 quarantineWorker {ctx.worker_pool_id}/{ctx.worker_group}/{ctx.hostname}")
    taskcluster.quarantine(ctx.worker_pool_id, ctx.worker_group, ctx.hostname, until)
    ui.ok(f"quarantined until {until[:10]}")


def step_drain(ctx: HostContext) -> None:
    s = get_settings()
    ui.step("DRAIN", "let the worker finish its in-flight task (2 consecutive idle polls)")
    ui.wire(f"queue.getWorker {ctx.hostname} → inspect recentTasks run states")
    deadline = time.monotonic() + s.drain_max_wait_seconds
    consecutive_idle = 0
    drained = False
    with ui.waiting("checking for an active task") as tick:
        while time.monotonic() < deadline:
            busy = taskcluster.is_currently_busy(ctx.worker_pool_id, ctx.worker_group, ctx.hostname)
            if not busy:
                consecutive_idle += 1
                # Require 2 consecutive idle polls so we don't race a worker that's
                # between two tasks (recentTasks shows the completed one; the new one
                # hasn't propagated yet).
                tick(f"idle {consecutive_idle}/2 — confirming")
                if consecutive_idle >= 2:
                    drained = True
                    break
            else:
                consecutive_idle = 0
                tick("worker busy — waiting for the task to finish")
            time.sleep(s.drain_poll_seconds)
    if not drained:
        raise TimeoutError(f"drain wait exceeded {s.drain_max_wait_seconds}s")
    ui.ok("drained — no task in flight")


def step_wipe(ctx: HostContext) -> None:
    if not ctx.simplemdm_device_id:
        raise ReprovisionError(f"{ctx.hostname} not found in SimpleMDM")
    # A prior EACS may have rotated this host's SSH key; clear any stale entry from the tool's
    # known_hosts so the verify connection accept-new's the current key instead of failing.
    ui.step("WIPE · EACS", "Erase All Content & Settings — DoNotObliterate (fails safe, never obliterates)")
    ssh.forget_host_key(ctx.fqdn)
    # Guard: EACS needs an escrowed Bootstrap Token. Without it, the erase either fails
    # (DoNotObliterate) or full-obliterates into a long headless macOS reinstall. Refuse to
    # wipe a box that can't EACS — verify BST over ssh first (operator key, no password).
    ui.wire(f"ssh admin@{ctx.hostname} sudo profiles status -type bootstraptoken")
    bst = ssh.run(ctx.fqdn, "sudo profiles status -type bootstraptoken", check=False)
    # Distinguish "couldn't verify over ssh" from "genuinely not escrowed" — otherwise an
    # ssh failure (VPN down, first-connection host key, missing operator key) masquerades as
    # a missing Bootstrap Token and sends the operator down the wrong path.
    if bst.returncode != 0:
        raise ReprovisionError(
            f"{ctx.fqdn}: couldn't verify the Bootstrap Token over ssh (exit {bst.returncode}) — "
            f"NOT wiping. Check VPN + SSH access to the host first.\n"
            f"{bst.stderr.decode(errors='replace').strip()}"
        )
    if b"escrowed to server: YES" not in bst.stdout:
        raise ReprovisionError(
            f"{ctx.fqdn}: Bootstrap Token not escrowed — EACS can't run, so a wipe would fail or "
            f"full-obliterate (headless reinstall). Mint + escrow first (reprovision mint, escrow-bst), "
            f"or wipe manually via the SimpleMDM UI if a full obliterate is truly intended."
        )
    ui.ok("Bootstrap Token escrowed — EACS can run")
    # Final gate against wiping a busy worker. Quarantine only stops NEW task claims — a task
    # claimed just before quarantine keeps running. `drain` waits that out in the full run, but
    # re-verify right here so a direct `wipe` (or a drain miss) can never erase a worker mid-task.
    # FAIL CLOSED: a destructive EACS must require positive proof of idle. If we can't confirm
    # (TC unreachable / bad creds) OR the worker is busy, ABORT — never wipe on uncertainty.
    # (Previously this failed OPEN — warn + proceed — which let a running worker get wiped.)
    ui.wire(f"queue.getWorker {ctx.hostname} → confirm no task in flight")
    try:
        busy = taskcluster.is_currently_busy(ctx.worker_pool_id, ctx.worker_group, ctx.hostname)
    except Exception as e:  # noqa: BLE001 — any TC/auth failure → can't verify idle → refuse to wipe
        raise ReprovisionError(
            f"{ctx.hostname}: couldn't confirm the worker is idle via Taskcluster ({e}) — refusing "
            f"to wipe. A reprovision must never EACS a worker that might be running a task. Fix TC "
            f"access (clientId/token) and retry, or wipe manually once you've confirmed it's drained."
        ) from e
    if busy:
        raise ReprovisionError(
            f"{ctx.hostname} is still running a task — NOT wiping. Quarantine + drain first "
            f"(`reprovision quarantine`, `reprovision drain`) or wait for the task to finish."
        )
    ui.ok("no task in flight")
    # Record the current enrolled_at so wait_for_reenroll can detect a *fresh* enrollment
    # (status alone is unreliable: it stays "enrolled" until the erase actually executes).
    ctx.pre_wipe_enrolled_at = simplemdm.get_device(ctx.simplemdm_device_id).get("attributes", {}).get("enrolled_at")
    ui.wire(f"SimpleMDM POST /devices/{ctx.simplemdm_device_id}/wipe  obliteration_behavior=DoNotObliterate")
    simplemdm.wipe(ctx.simplemdm_device_id)
    ui.ok("erase command accepted by SimpleMDM")


def step_wait_for_reenroll(ctx: HostContext) -> None:
    s = get_settings()
    # Baseline: the pre-wipe enrolled_at (from step_wipe). If unset (step run standalone),
    # capture the current value now. We wait for a DIFFERENT enrolled_at + status=enrolled,
    # so we don't false-return on the pre-wipe enrollment (status lags the erase).
    baseline = ctx.pre_wipe_enrolled_at
    if baseline is None:
        baseline = simplemdm.get_device(ctx.simplemdm_device_id).get("attributes", {}).get("enrolled_at")
    ui.step("RE-ENROLL", "erase → reboot → DEP re-enrollment · typically ~5 min")
    ui.wire(f"SimpleMDM GET /devices/{ctx.simplemdm_device_id}  (poll enrolled_at ≠ {baseline})")
    deadline = time.monotonic() + s.wipe_max_wait_seconds
    start = time.monotonic()
    next_poll = 0.0
    reenrolled = False
    a: dict = {}
    with ui.waiting("waiting for a fresh enrollment", eta_seconds=300) as tick:
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_poll:
                next_poll = now + s.wipe_poll_seconds
                a = simplemdm.get_device(ctx.simplemdm_device_id).get("attributes", {})
                if a.get("status") == "enrolled" and a.get("enrolled_at") != baseline:
                    reenrolled = True
                    break
            tick(_reenroll_phase(now - start))
            time.sleep(1)
    if not reenrolled:
        raise TimeoutError("device did not re-enroll within window")
    ui.ok(f"re-enrolled — fresh enrolled_at {a.get('enrolled_at')}")


# Expected phases of the ~5-min EACS→DEP window, keyed to elapsed seconds. Not literal
# device state (we only poll enrolled_at) — a truthful "typical timeline" so the wait reads
# as progress, not a hang.
def _reenroll_phase(elapsed: float) -> str:
    for cutoff, label in (
        (60, "erasing volume (EACS)"),
        (120, "rebooting into Setup Assistant"),
        (210, "DEP check-in with SimpleMDM"),
        (300, "installing SCEP + CLT profiles"),
    ):
        if elapsed < cutoff:
            return f"{label} …"
    return "finishing enrollment …"


def step_mint(ctx: HostContext) -> None:
    """
    Mint the first SecureToken via an interactive password login.

    Required because DEP skips Setup Assistant on this fleet: admin exists but holds
    no SecureToken and isn't a volume owner until an interactive (PAM) login. Proven
    by A/B on m4-81 (2026-07-02): with this login the bootstrap finishes; without it,
    it wedges at the BST wait-loop and times out. Idempotent — skips if already ENABLED.
    """
    ui.step("MINT SECURETOKEN", "DEP skips Setup Assistant, so admin has no token until an interactive login")
    # The box just re-enrolled post-EACS with a fresh host key; forget the old one so the
    # SecureToken status check (which uses ssh.run) doesn't fail on a key mismatch.
    ssh.forget_host_key(ctx.fqdn)
    with ui.waiting("waiting for sshd (relops-ssh pkg lands during convergence)"):
        ssh.wait_for_sshd(ctx.fqdn)
    if "ENABLED" in ssh.secure_token_status(ctx.fqdn):
        ui.ok("admin already holds a SecureToken — skipping mint")
        return
    ui.wire(f"expect: ssh admin@{ctx.hostname} (keyboard-interactive PAM login → grants first SecureToken)")
    ssh.password_login(ctx.fqdn)
    enabled = False
    with ui.waiting("verifying the SecureToken came up ENABLED") as tick:
        for i in range(6):
            if "ENABLED" in ssh.secure_token_status(ctx.fqdn):
                enabled = True
                break
            tick(f"not yet — retry {i + 1}/6")
            time.sleep(5)
    if not enabled:
        raise ReprovisionError(f"{ctx.fqdn}: admin SecureToken not ENABLED after mint")
    ui.ok("admin SecureToken ENABLED")


def step_escrow_bst(ctx: HostContext) -> None:
    """
    Escrow the Bootstrap Token so the box is MDM-EACS-able next cycle.

    Runs non-interactively with -user/-password (the bare form prompts for a username
    and fails). Requires admin to already hold a SecureToken — so step_mint must run
    first. NB: the bootstrap script skips its own BST-escrow when a token already
    exists, so on the pre-minted path this step is what actually escrows the BST.
    """
    s = get_settings()
    ui.step("ESCROW BOOTSTRAP TOKEN", "escrow the BST so this box is EACS-able next cycle")
    ui.wire(f"ssh admin@{ctx.hostname} sudo profiles install -type bootstraptoken -user {s.ssh_admin_user} -password ••••••")
    install_cmd = (
        f"sudo profiles install -type bootstraptoken "
        f"-user {s.ssh_admin_user} -password {shlex.quote(ssh_admin_password())}"
    )
    try:
        ssh.run(ctx.fqdn, install_cmd)
    except ReprovisionError as e:
        # ssh.run already scrubs the command (which embeds the password); add a mint hint.
        raise ReprovisionError(f"{e}\n    (has admin minted a SecureToken? run `reprovision mint` first)") from None
    cp = ssh.run(ctx.fqdn, "sudo profiles status -type bootstraptoken")
    if b"escrowed to server: YES" not in cp.stdout:
        raise ReprovisionError(f"BST escrow check failed:\n{cp.stdout.decode()}")
    ui.ok("Bootstrap Token escrowed to server")


def step_wait_for_sentinel(ctx: HostContext) -> None:
    s = get_settings()
    ui.step("BOOTSTRAP", "the freshly-enrolled host provisions itself — zero operator SSH from here")
    ui.wire("signed bootstrap PKG (managed install) lands via SimpleMDM during DEP convergence")
    ui.wire("→ host fetches its vault.yaml over mTLS from the forge LB (step-ca SCEP client cert)")
    ui.wire(f"→ puppet apply: role {ctx.role} — generic-worker, users, TCC perms, launch daemons")
    ui.wire("→ generic-worker self-registers with Taskcluster (Hawk) and starts claiming work")
    ui.wire(f"ssh admin@{ctx.hostname} test -f /var/log/m4-bootstrap-complete  (poll for the sentinel it writes)")
    deadline = time.monotonic() + s.bootstrap_max_wait_seconds
    found = False
    with ui.waiting("waiting for the bootstrap sentinel") as tick:
        while time.monotonic() < deadline:
            if ssh.file_exists(ctx.fqdn, "/var/log/m4-bootstrap-complete"):
                found = True
                break
            tick("puppet converging on the freshly-enrolled host")
            time.sleep(s.bootstrap_poll_seconds)
    if not found:
        raise TimeoutError("bootstrap sentinel did not appear in time")
    ui.ok("bootstrap complete — /var/log/m4-bootstrap-complete present")


def step_unquarantine(ctx: HostContext) -> None:
    ui.step("UNQUARANTINE", "return the worker to service")
    ui.wire(f"PUT queue/v1 quarantineWorker {ctx.hostname}  (quarantineUntil → past)")
    taskcluster.unquarantine(ctx.worker_pool_id, ctx.worker_group, ctx.hostname)
    ui.ok("returned to service")


def reprovision(hostname: str, *, skip_wipe: bool = False, unquarantine: bool = False) -> None:
    """Full E2E workflow. skip_wipe lets operators re-run later steps after a wipe.

    unquarantine defaults to False: by design a host stays quarantined through wipe +
    reprovision (and the fleet has no un-quarantine key wired yet). Pass unquarantine=True
    to return the host to service at the end — the eventual prod-return flow, once a
    queue:quarantine-scoped credential is available.

    Admin-password hardening is a DEP config concern, not a workflow step: set a strong,
    random admin password in the SimpleMDM DEP account-setup and point the mint at it via
    REPROVISION_SSH_ADMIN_PASSWORD. (SimpleMDM's rotate_admin_password can't be used — it
    requires an auto-generated managed password, which the mint can't read back.)
    """
    ctx = resolve(hostname)
    started = time.monotonic()
    ui.banner(ctx.hostname, ctx.role, ctx.worker_pool_id)

    # Show the whole pipeline up front so it never reads as a single opaque action.
    phases = ["QUARANTINE", "DRAIN"]
    if not skip_wipe:
        phases += ["WIPE", "RE-ENROLL"]
    phases += ["MINT", "ESCROW BST", "BOOTSTRAP"]
    if unquarantine:
        phases += ["UNQUARANTINE"]
    ui.flow(phases)

    step_quarantine(ctx)
    step_drain(ctx)
    if not skip_wipe:
        step_wipe(ctx)
        step_wait_for_reenroll(ctx)
    step_mint(ctx)  # mint SecureToken (must precede escrow_bst)
    step_escrow_bst(ctx)
    # No vault-delivery and no bootstrap-trigger steps:
    #  - vault: the bootstrap fetches vault.yaml itself over mTLS using its SCEP cert.
    #  - bootstrap: it's delivered as a signed PKG (managed install) that lands during DEP
    #    convergence once admin logs in (the mint), so nothing needs to trigger it. We just
    #    wait for the sentinel it writes.
    step_wait_for_sentinel(ctx)
    # Default: leave the host quarantined (matches current fleet reality; no un-quarantine
    # key wired). Only return it to service when explicitly asked — the eventual prod flow.
    if unquarantine:
        step_unquarantine(ctx)
    ui.summary(ctx.hostname, time.monotonic() - started, quarantined=not unquarantine)
