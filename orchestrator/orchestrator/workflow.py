"""
The end-to-end EACS workflow as a single function. CLI subcommands call individual
steps for partial-failure recovery; `reprovision` calls them all in order.

The steps are deliberately small functions with clear in/out contracts so they're
re-runnable. If step N fails, the operator can resume by calling step N+1 directly
once they've fixed whatever was broken.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from rich.console import Console

from .clients import onepassword, simplemdm, ssh, taskcluster
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


def resolve(hostname: str) -> HostContext:
    """Look up everything we need about a host from its short name."""
    role = role_for_hostname(hostname)

    # Worker pool derived from role for now. Roles map 1:1 to pools by convention.
    pool_by_role = {
        "gecko_t_osx_1500_m4": "releng-hardware/gecko-t-osx-1500-m4",
        "gecko_t_osx_1500_m4_no_sip": "releng-hardware/gecko-t-osx-1500-m4-no-sip",
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
    while time.monotonic() < deadline:
        # See note in clients/taskcluster.py — claimed_task_count is a placeholder.
        # Operator should `ssh ctx.fqdn pgrep -fl 'task-X'` to confirm in v1.
        n = taskcluster.claimed_task_count(ctx.worker_pool_id, ctx.worker_group, ctx.hostname)
        if n == 0:
            console.print("  → drained.")
            return
        time.sleep(s.drain_poll_seconds)
    raise TimeoutError(f"drain wait exceeded {s.drain_max_wait_seconds}s")


def step_wipe(ctx: HostContext) -> None:
    if not ctx.simplemdm_device_id:
        raise RuntimeError(f"{ctx.hostname} not found in SimpleMDM")
    console.print(f"[bold]wipe[/]: EACS-equivalent obliteration on device {ctx.simplemdm_device_id}")
    simplemdm.wipe(ctx.simplemdm_device_id)


def step_wait_for_reenroll(ctx: HostContext) -> None:
    s = get_settings()
    console.print("[bold]wait_for_reenroll[/]: polling SimpleMDM for post-DEP check-in")
    deadline = time.monotonic() + s.wipe_max_wait_seconds
    while time.monotonic() < deadline:
        d = simplemdm.get_device(ctx.simplemdm_device_id)
        status = d.get("attributes", {}).get("status")
        if status == "enrolled":
            console.print("  → enrolled.")
            return
        time.sleep(s.wipe_poll_seconds)
    raise TimeoutError("device did not re-enroll within window")


def step_escrow_bst(ctx: HostContext) -> None:
    """sudo profiles install -type bootstraptoken — solves Apple Silicon BST custody without VNC."""
    console.print(f"[bold]escrow_bst[/]: sshing to admin@{ctx.fqdn} to escrow Bootstrap Token")
    ssh.run(ctx.fqdn, "sudo profiles install -type bootstraptoken")
    cp = ssh.run(ctx.fqdn, "sudo profiles status -type bootstraptoken")
    if b"escrowed to server: YES" not in cp.stdout:
        raise RuntimeError(f"BST escrow check failed:\n{cp.stdout.decode()}")
    console.print("  → BST escrowed.")


def step_deliver_vault(ctx: HostContext) -> None:
    """
    Today: fetch the role's vault.yaml from 1Password, SCP-drop to /var/root/vault.yaml.

    Future: when SCEP + broker infra is live, this becomes a no-op — the bootstrap
    script on the host fetches vault.yaml from the broker using its SCEP cert.
    Selectable via a flag on the CLI.
    """
    settings = get_settings()
    reference = f"op://{settings.onepassword_vault}/vault-{ctx.role}/notesPlain"
    console.print(f"[bold]deliver_vault[/]: reading {reference} from 1Password")
    content = onepassword.read(reference)
    console.print(f"[bold]deliver_vault[/]: dropping /var/root/vault.yaml on {ctx.fqdn} ({len(content)} bytes)")
    ssh.write_file_as_root(ctx.fqdn, "/var/root/vault.yaml", content, mode="0600")


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


def step_unquarantine(ctx: HostContext) -> None:
    console.print(f"[bold]unquarantine[/]: {ctx.hostname}")
    taskcluster.unquarantine(ctx.worker_pool_id, ctx.worker_group, ctx.hostname)


def reprovision(hostname: str, *, skip_wipe: bool = False) -> None:
    """Full E2E workflow. skip_wipe lets operators re-run later steps after a wipe."""
    ctx = resolve(hostname)
    console.print(f"=> reprovisioning {ctx.hostname} (role={ctx.role}, pool={ctx.worker_pool_id})")
    step_quarantine(ctx)
    step_drain(ctx)
    if not skip_wipe:
        step_wipe(ctx)
        step_wait_for_reenroll(ctx)
    step_escrow_bst(ctx)
    step_deliver_vault(ctx)
    step_trigger_bootstrap_script(ctx)
    step_wait_for_sentinel(ctx)
    step_unquarantine(ctx)
    console.print(f"[bold green]done[/]: {ctx.hostname} reprovisioned")
