"""
CLI entry point. `reprovision <hostname>` for full workflow; individual subcommands
for re-running steps after partial failure.
"""

from __future__ import annotations

import typer

from . import ui, workflow
from .errors import NotReadyError, ReprovisionError

_app = typer.Typer(
    no_args_is_help=True,
    pretty_exceptions_enable=False,  # let expected failures reach app()'s clean handler
    help="Drive an end-to-end EACS reprovision of a CI worker.",
)


@_app.command()
def run(
    hostname: str = typer.Argument(..., help="Short hostname, e.g. macmini-m4-81"),
    unquarantine: bool = typer.Option(
        False,
        "--unquarantine",
        help="Return the host to service at the end. Default off: host stays quarantined "
        "through reprovision (needs a queue:quarantine-scoped credential).",
    ),
) -> None:
    """Full workflow: quarantine -> drain -> wipe -> reenroll -> mint -> BST -> bootstrap.

    Vault is fetched by the bootstrap script over mTLS (SCEP), so there is no vault-delivery step.
    By default the host stays quarantined throughout; pass --unquarantine to return it to service.
    """
    workflow.reprovision(hostname, unquarantine=unquarantine)


_EXPECTED_OS_OPT = typer.Option(
    "", "--expected-os", help="Required macOS version (default: REPROVISION_PROVISION_EXPECTED_OS, 15.3)."
)
_ALLOW_SIP_OPT = typer.Option(
    False, "--allow-sip-enabled", help="Don't require SIP to be disabled (for the SIP-on flow)."
)
_QUARANTINE_ON_REGISTER_OPT = typer.Option(
    False,
    "--quarantine-on-register",
    help="Hold the host out of the pool: watch for it to register in Taskcluster and quarantine "
    "it on sight. Needs TC credentials.",
)


@_app.command()
def provision(
    hostname: str = typer.Argument(..., help="Short hostname, e.g. macmini-m4-201"),
    expected_os: str = _EXPECTED_OS_OPT,
    allow_sip_enabled: bool = _ALLOW_SIP_OPT,
    no_wait: bool = typer.Option(
        False, "--no-wait", help="Stop after mint + BST escrow; don't block on the bootstrap sentinel."
    ),
    quarantine_on_register: bool = _QUARANTINE_ON_REGISTER_OPT,
) -> None:
    """Provision a FRESH DEP-enrolled host: preflight -> mint -> escrow BST -> bootstrap.

    For factory-clean hardware that auto-enrolled via DEP. Unlike `run` there is no wipe in
    this path at all — nothing it can do will erase the host.
    """
    workflow.provision(
        hostname,
        expected_os=expected_os,
        require_sip_disabled=not allow_sip_enabled,
        wait=not no_wait,
        quarantine_on_register=quarantine_on_register,
    )


@_app.command()
def quarantine_on_register(hostname: str) -> None:
    """Wait for a fresh worker to appear in Taskcluster, then quarantine it on sight.

    A worker that isn't registered yet can't be quarantined (`quarantineWorker` 404s), so this
    watches for it. Narrows the window between registration and the first claimed task to
    seconds — it does not eliminate it. Use standalone for hosts already mid-bootstrap.
    """
    workflow.step_quarantine_on_register(workflow.resolve_offline(hostname))


@_app.command()
def os_update(
    hostname: str = typer.Argument(..., help="Short hostname, e.g. macmini-m4-201"),
    expected_os: str = _EXPECTED_OS_OPT,
) -> None:
    """Launch the in-place macOS upgrade on a fresh host, then return (it reboots itself).

    Staged over SSH, so the admin password is resolved from the vault at fire time and never
    stored in the SimpleMDM script body. Idempotent: a no-op if the host is already at target.
    Confirm arrival later with `preflight` — the upgrade runs unattended for tens of minutes.
    """
    workflow.step_os_update(workflow.resolve_offline(hostname), expected_os=expected_os)


@_app.command()
def preflight(
    hostname: str = typer.Argument(..., help="Short hostname, e.g. macmini-m4-201"),
    expected_os: str = _EXPECTED_OS_OPT,
    allow_sip_enabled: bool = _ALLOW_SIP_OPT,
) -> None:
    """Read-only readiness check on one host: OS version, SIP state, SecureToken/BST status.

    Changes nothing and needs no SimpleMDM or Taskcluster credential — just the admin SSH key.
    Exits 2 (not 1) when the host simply isn't ready yet.
    """
    workflow.step_preflight(
        workflow.resolve_offline(hostname),
        expected_os=expected_os,
        require_sip_disabled=not allow_sip_enabled,
    )


@_app.command()
def batch(
    hosts_file: str = typer.Argument(..., help="File with one short hostname per line ('#' comments ok)."),
    action: str = typer.Option(
        "provision", "--action", help="What to run per host: preflight | mint | provision."
    ),
    concurrency: int = typer.Option(
        0, "--concurrency", "-j", help="How many hosts in flight (default 3 — MDC1 throughput, not CPU)."
    ),
    expected_os: str = _EXPECTED_OS_OPT,
    allow_sip_enabled: bool = _ALLOW_SIP_OPT,
    no_wait: bool = typer.Option(False, "--no-wait", help="For --action provision: skip the sentinel wait."),
    quarantine_on_register: bool = _QUARANTINE_ON_REGISTER_OPT,
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the per-host commands and exit."),
) -> None:
    """Run one action across a list of hosts, a few at a time, with per-host logs.

    Each host runs as its own `reprovision` subprocess, so one bad host can't take the batch
    down. Hosts that aren't ready yet are reported as skipped, not failed, and re-running the
    same command picks them up — every action here is idempotent.
    """
    from . import batch as _batch

    hosts = _batch.read_host_file(hosts_file)
    failed = _batch.run_batch(
        hosts,
        action=action,
        concurrency=concurrency,
        expected_os=expected_os,
        allow_sip_enabled=allow_sip_enabled,
        wait=not no_wait,
        quarantine_on_register=quarantine_on_register,
        dry_run=dry_run,
    )
    if failed:
        raise SystemExit(1)


@_app.command()
def quarantine(
    hostname: str,
    until: str = typer.Option("", "--until", help="ISO-8601 quarantineUntil (default: 365 days out)."),
    info: str = typer.Option("", "--info", help="Audit reason stored as quarantineInfo."),
) -> None:
    workflow.step_quarantine(workflow.resolve(hostname), until=until or None, info=info)


@_app.command()
def unquarantine(hostname: str) -> None:
    workflow.step_unquarantine(workflow.resolve(hostname))


@_app.command()
def drain(hostname: str) -> None:
    workflow.step_drain(workflow.resolve(hostname))


@_app.command()
def wipe(hostname: str) -> None:
    """Trigger EACS-equivalent wipe via SimpleMDM API."""
    workflow.step_wipe(workflow.resolve(hostname))


@_app.command()
def wait_reenroll(hostname: str) -> None:
    workflow.step_wait_for_reenroll(workflow.resolve(hostname))


@_app.command()
def mint(hostname: str) -> None:
    """Mint the admin SecureToken via an interactive password login (idempotent)."""
    # resolve_offline: this step is SSH-only (fqdn + hostname). Going through resolve() cost a
    # SimpleMDM device lookup per host, which at -j3 rate-limited the API and failed a host in
    # the wave-1 batch (429 on macmini-m4-244, 2026-08-14) for a value the step never reads.
    workflow.step_mint(workflow.resolve_offline(hostname))


@_app.command()
def escrow_bst(hostname: str) -> None:
    """SSH in and run `sudo profiles install -type bootstraptoken -user admin -password …` (needs mint first)."""
    workflow.step_escrow_bst(workflow.resolve_offline(hostname))  # SSH-only; see mint()



@_app.command()
def wait_sentinel(hostname: str) -> None:
    workflow.step_wait_for_sentinel(workflow.resolve_offline(hostname))  # SSH-only; see mint()


@_app.command()
def wait_bootstrap_pkg(hostname: str) -> None:
    """Confirm the signed bootstrap pkg landed — i.e. the host is in the bootstrap group.

    The pkg is a managed install triggered by SimpleMDM group membership, so a host in the
    wrong group never bootstraps. Exits 2 if it hasn't landed.
    """
    workflow.step_wait_for_bootstrap_pkg(workflow.resolve_offline(hostname))


@_app.command()
def check() -> None:
    """Read-only preflight — confirm every credential resolves from the vault (no changes)."""
    workflow.check()


@_app.command()
def demo(
    flow: str = typer.Option(
        "reprovision",
        "--flow",
        help="Which replay: reprovision (EACS an existing host) | provision (fresh DEP host) | "
        "batch (the hardware-refresh rollout).",
    ),
    host: str = typer.Option("", "--host", help="Hostname to show on screen (default: per-flow)."),
) -> None:
    """Play a safe, no-host replay of a flow — for live demos (touches nothing).

    Same `ui` layer as the real run, so the screen looks like production, just faster. No ssh,
    no SimpleMDM, no Taskcluster.
    """
    from . import demo as _demo

    _demo.run(flow, host)


def app() -> None:
    """Entry point. Turns expected operational failures — not signed in to 1Password, off the
    VPN, host unreachable, BST not escrowed, timeouts — into one clean red line instead of a
    traceback. Anything that isn't one of these propagates normally so real bugs stay visible.

    Exit codes: 0 success · 2 host not ready (wrong OS, SIP on, box not up — come back later)
    · 1 everything else. `reprovision batch` reads the 2 to report skipped separately from
    failed, so keep them distinct.
    """
    try:
        _app()
    except NotReadyError as e:  # subclass of ReprovisionError — must be caught first
        ui.warn(str(e))
        raise SystemExit(2) from None
    except (ReprovisionError, TimeoutError, ValueError) as e:
        ui.err(str(e))
        raise SystemExit(1) from None


if __name__ == "__main__":
    app()
