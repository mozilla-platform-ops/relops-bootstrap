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

from rich.console import Console

from .clients import simplemdm, ssh, taskcluster
from .config import get_settings
from .role_map import role_for_hostname

console = Console()


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
    role = role_for_hostname(hostname)

    # Worker pool derived from role for now. Roles map 1:1 to pools by convention.
    pool_by_role = {
        "gecko_t_osx_1500_m4": "releng-hardware/gecko-t-osx-1500-m4",
        "gecko_t_osx_1400_r8": "releng-hardware/gecko-t-osx-1400-r8",
    }
    pool = pool_by_role.get(role)
    if not pool:
        raise ValueError(f"no worker pool mapping for role '{role}'")

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


# --- workflow steps ---


def step_quarantine(ctx: HostContext) -> None:
    until = (datetime.now(timezone.utc) + timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    console.print(f"[bold]quarantine[/]: {ctx.hostname} until {until}")
    taskcluster.quarantine(ctx.worker_pool_id, ctx.worker_group, ctx.hostname, until)


def step_drain(ctx: HostContext) -> None:
    s = get_settings()
    console.print(f"[bold]drain[/]: waiting for {ctx.hostname} to finish its current task")
    deadline = time.monotonic() + s.drain_max_wait_seconds
    consecutive_idle = 0
    while time.monotonic() < deadline:
        busy = taskcluster.is_currently_busy(ctx.worker_pool_id, ctx.worker_group, ctx.hostname)
        if not busy:
            consecutive_idle += 1
            # Require 2 consecutive idle polls so we don't race a worker that's
            # between two tasks (recentTasks shows the completed one; the new one
            # hasn't propagated yet).
            if consecutive_idle >= 2:
                console.print("  → drained.")
                return
        else:
            consecutive_idle = 0
        time.sleep(s.drain_poll_seconds)
    raise TimeoutError(f"drain wait exceeded {s.drain_max_wait_seconds}s")


def step_wipe(ctx: HostContext) -> None:
    if not ctx.simplemdm_device_id:
        raise RuntimeError(f"{ctx.hostname} not found in SimpleMDM")
    # Record the current enrolled_at so wait_for_reenroll can detect a *fresh* enrollment
    # (status alone is unreliable: it stays "enrolled" until the erase actually executes).
    ctx.pre_wipe_enrolled_at = simplemdm.get_device(ctx.simplemdm_device_id).get("attributes", {}).get("enrolled_at")
    console.print(f"[bold]wipe[/]: EACS-equivalent obliteration on device {ctx.simplemdm_device_id}")
    simplemdm.wipe(ctx.simplemdm_device_id)


def step_wait_for_reenroll(ctx: HostContext) -> None:
    s = get_settings()
    # Baseline: the pre-wipe enrolled_at (from step_wipe). If unset (step run standalone),
    # capture the current value now. We wait for a DIFFERENT enrolled_at + status=enrolled,
    # so we don't false-return on the pre-wipe enrollment (status lags the erase).
    baseline = ctx.pre_wipe_enrolled_at
    if baseline is None:
        baseline = simplemdm.get_device(ctx.simplemdm_device_id).get("attributes", {}).get("enrolled_at")
    console.print(f"[bold]wait_for_reenroll[/]: polling SimpleMDM for a fresh enrollment (was {baseline})")
    deadline = time.monotonic() + s.wipe_max_wait_seconds
    while time.monotonic() < deadline:
        a = simplemdm.get_device(ctx.simplemdm_device_id).get("attributes", {})
        if a.get("status") == "enrolled" and a.get("enrolled_at") != baseline:
            console.print(f"  → re-enrolled ({a.get('enrolled_at')}).")
            return
        time.sleep(s.wipe_poll_seconds)
    raise TimeoutError("device did not re-enroll within window")


def step_mint(ctx: HostContext) -> None:
    """
    Mint the first SecureToken via an interactive password login.

    Required because DEP skips Setup Assistant on this fleet: admin exists but holds
    no SecureToken and isn't a volume owner until an interactive (PAM) login. Proven
    by A/B on m4-81 (2026-07-02): with this login the bootstrap finishes; without it,
    it wedges at the BST wait-loop and times out. Idempotent — skips if already ENABLED.
    """
    console.print(f"[bold]mint[/]: ensuring {ctx.fqdn} admin holds a SecureToken")
    console.print("  → waiting for sshd (relops-ssh pkg lands during convergence)…")
    ssh.wait_for_sshd(ctx.fqdn)
    if "ENABLED" in ssh.secure_token_status(ctx.fqdn):
        console.print("  → already SecureToken-ENABLED; skipping mint.")
        return
    console.print("  → minting via interactive password login…")
    ssh.password_login(ctx.fqdn)
    for _ in range(6):
        if "ENABLED" in ssh.secure_token_status(ctx.fqdn):
            console.print("  → SecureToken ENABLED.")
            return
        time.sleep(5)
    raise RuntimeError(f"{ctx.fqdn}: admin SecureToken not ENABLED after mint")


def step_escrow_bst(ctx: HostContext) -> None:
    """
    Escrow the Bootstrap Token so the box is MDM-EACS-able next cycle.

    Runs non-interactively with -user/-password (the bare form prompts for a username
    and fails). Requires admin to already hold a SecureToken — so step_mint must run
    first. NB: the bootstrap script skips its own BST-escrow when a token already
    exists, so on the pre-minted path this step is what actually escrows the BST.
    """
    s = get_settings()
    console.print(f"[bold]escrow_bst[/]: escrowing Bootstrap Token on {ctx.fqdn}")
    ssh.run(
        ctx.fqdn,
        f"sudo profiles install -type bootstraptoken -user {s.ssh_admin_user} -password {shlex.quote(s.ssh_admin_password)}",
    )
    cp = ssh.run(ctx.fqdn, "sudo profiles status -type bootstraptoken")
    if b"escrowed to server: YES" not in cp.stdout:
        raise RuntimeError(f"BST escrow check failed:\n{cp.stdout.decode()}")
    console.print("  → BST escrowed.")


def step_trigger_bootstrap_script(ctx: HostContext) -> int:
    s = get_settings()
    if not s.bootstrap_script_id:
        raise RuntimeError("REPROVISION_BOOTSTRAP_SCRIPT_ID is not set")
    console.print(f"[bold]trigger_bootstrap[/]: SimpleMDM script {s.bootstrap_script_id}")
    job_id = simplemdm.trigger_script(s.bootstrap_script_id, ctx.simplemdm_device_id)
    return job_id


def step_wait_for_sentinel(ctx: HostContext) -> None:
    s = get_settings()
    console.print(f"[bold]wait_for_sentinel[/]: polling {ctx.fqdn} for /var/log/m4-bootstrap-complete")
    deadline = time.monotonic() + s.bootstrap_max_wait_seconds
    while time.monotonic() < deadline:
        if ssh.file_exists(ctx.fqdn, "/var/log/m4-bootstrap-complete"):
            console.print("  → sentinel present.")
            return
        time.sleep(s.bootstrap_poll_seconds)
    raise TimeoutError("bootstrap sentinel did not appear in time")


def step_rotate_admin_password(ctx: HostContext) -> None:
    """
    Rotate the auto-admin password to a fresh SimpleMDM-generated one, post-bootstrap.

    The bootstrap needs the fixed DEP account-setup password (so the mint can log in);
    this secures it afterward. Relies on the escrowed BST (MDM resets the password while
    preserving the SecureToken). The new password lives in the SimpleMDM UI; the operator
    ssh key keeps working regardless.
    """
    if not ctx.simplemdm_device_id:
        raise RuntimeError(f"{ctx.hostname} not found in SimpleMDM")
    console.print(f"[bold]rotate_admin[/]: rotating auto-admin password on device {ctx.simplemdm_device_id}")
    # Best-effort: the worker is already provisioned by the time we get here, so a rotation
    # failure must not fail the whole run. Warn loudly and continue. Re-run `rotate-admin`
    # once the underlying cause is fixed.
    try:
        simplemdm.rotate_admin_password(ctx.simplemdm_device_id)
        console.print("  → rotation requested (202); new password is in the SimpleMDM UI.")
    except Exception as e:  # noqa: BLE001 — provisioning already succeeded; never crash on the post-step
        console.print(f"[yellow]  → WARN: admin-password rotation failed: {e}[/]")
        console.print("[yellow]     Worker is provisioned and quarantined regardless; fix + re-run `rotate-admin`.[/]")


def step_unquarantine(ctx: HostContext) -> None:
    console.print(f"[bold]unquarantine[/]: {ctx.hostname}")
    taskcluster.unquarantine(ctx.worker_pool_id, ctx.worker_group, ctx.hostname)


def reprovision(
    hostname: str, *, skip_wipe: bool = False, unquarantine: bool = False, rotate_admin: bool = False
) -> None:
    """Full E2E workflow. skip_wipe lets operators re-run later steps after a wipe.

    unquarantine defaults to False: by design a host stays quarantined through wipe +
    reprovision (and the fleet has no un-quarantine key wired yet). Pass unquarantine=True
    to return the host to service at the end — the eventual prod-return flow, once a
    queue:quarantine-scoped credential is available.

    rotate_admin defaults to False: SimpleMDM's rotate_admin_password only works when the
    enrollment uses "auto-generate unique local admin password", which is incompatible with
    the fixed bootstrap password the mint needs (and SimpleMDM won't expose the generated
    password via API). It returns "macOS Auto Admin password can not be rotated" otherwise.
    Left in place (opt-in) for enrollments that do use a managed auto-admin; the real
    hardening for fixed-password fleets is a strong DEP password or an on-box reset.
    """
    ctx = resolve(hostname)
    console.print(f"=> reprovisioning {ctx.hostname} (role={ctx.role}, pool={ctx.worker_pool_id})")
    step_quarantine(ctx)
    step_drain(ctx)
    if not skip_wipe:
        step_wipe(ctx)
        step_wait_for_reenroll(ctx)
    step_mint(ctx)  # mint SecureToken (must precede escrow_bst)
    step_escrow_bst(ctx)
    # No vault delivery step: Path C — the bootstrap script fetches vault.yaml itself
    # via mTLS using its SCEP-issued cert. (Was a 1Password op-read + SSH drop.)
    step_trigger_bootstrap_script(ctx)
    step_wait_for_sentinel(ctx)
    if rotate_admin:
        # Secure the auto-admin password now that bootstrap (which needed the fixed
        # password) is done. Safe: MDM uses the escrowed BST, SecureToken is preserved.
        step_rotate_admin_password(ctx)
    # Default: leave the host quarantined (matches current fleet reality; no un-quarantine
    # key wired). Only return it to service when explicitly asked — the eventual prod flow.
    if unquarantine:
        step_unquarantine(ctx)
        console.print(f"[bold green]done[/]: {ctx.hostname} reprovisioned and returned to service")
    else:
        console.print(f"[bold green]done[/]: {ctx.hostname} reprovisioned (still quarantined; pass --unquarantine to return to service)")
