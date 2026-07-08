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
    # Guard: EACS needs an escrowed Bootstrap Token. Without it, the erase either fails
    # (DoNotObliterate) or full-obliterates into a long headless macOS reinstall. Refuse to
    # wipe a box that can't EACS — verify BST over ssh first (operator key, no password).
    bst = ssh.run(ctx.fqdn, "sudo profiles status -type bootstraptoken", check=False)
    if b"escrowed to server: YES" not in bst.stdout:
        raise RuntimeError(
            f"{ctx.fqdn}: Bootstrap Token not escrowed — EACS can't run, so a wipe would fail or "
            f"full-obliterate (headless reinstall). Mint + escrow first (reprovision mint, escrow-bst), "
            f"or wipe manually via the SimpleMDM UI if a full obliterate is truly intended."
        )
    # Record the current enrolled_at so wait_for_reenroll can detect a *fresh* enrollment
    # (status alone is unreliable: it stays "enrolled" until the erase actually executes).
    ctx.pre_wipe_enrolled_at = simplemdm.get_device(ctx.simplemdm_device_id).get("attributes", {}).get("enrolled_at")
    console.print(f"[bold]wipe[/]: EACS (DoNotObliterate) on device {ctx.simplemdm_device_id}")
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


def step_unquarantine(ctx: HostContext) -> None:
    console.print(f"[bold]unquarantine[/]: {ctx.hostname}")
    taskcluster.unquarantine(ctx.worker_pool_id, ctx.worker_group, ctx.hostname)


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
    console.print(f"=> reprovisioning {ctx.hostname} (role={ctx.role}, pool={ctx.worker_pool_id})")
    step_quarantine(ctx)
    step_drain(ctx)
    if not skip_wipe:
        step_wipe(ctx)
        step_wait_for_reenroll(ctx)
    step_mint(ctx)  # mint SecureToken (must precede escrow_bst)
    step_escrow_bst(ctx)
    # No vault-delivery and no bootstrap-trigger steps:
    #  - vault: the bootstrap fetches vault.yaml itself over mTLS (SCEP) — Path C.
    #  - bootstrap: it's delivered as a signed PKG (managed install) that lands during DEP
    #    convergence once admin logs in (the mint), so nothing needs to trigger it. We just
    #    wait for the sentinel it writes.
    step_wait_for_sentinel(ctx)
    # Default: leave the host quarantined (matches current fleet reality; no un-quarantine
    # key wired). Only return it to service when explicitly asked — the eventual prod flow.
    if unquarantine:
        step_unquarantine(ctx)
        console.print(f"[bold green]done[/]: {ctx.hostname} reprovisioned and returned to service")
    else:
        console.print(f"[bold green]done[/]: {ctx.hostname} reprovisioned (still quarantined; pass --unquarantine to return to service)")
