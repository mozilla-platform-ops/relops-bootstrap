"""
CLI entry point. `reprovision <hostname>` for full workflow; individual subcommands
for re-running steps after partial failure.
"""

from __future__ import annotations

import typer

from . import ui, workflow
from .errors import ReprovisionError

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


@_app.command()
def quarantine(hostname: str) -> None:
    workflow.step_quarantine(workflow.resolve(hostname))


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
    workflow.step_mint(workflow.resolve(hostname))


@_app.command()
def escrow_bst(hostname: str) -> None:
    """SSH in and run `sudo profiles install -type bootstraptoken -user admin -password …` (needs mint first)."""
    workflow.step_escrow_bst(workflow.resolve(hostname))



@_app.command()
def wait_sentinel(hostname: str) -> None:
    workflow.step_wait_for_sentinel(workflow.resolve(hostname))


@_app.command()
def demo(
    host: str = typer.Option("macmini-m4-88", "--host", help="Hostname to show on screen during the demo."),
) -> None:
    """Play a safe, no-host replay of the full flow — for live demos (touches nothing)."""
    from . import demo as _demo

    _demo.run_demo(host)


def app() -> None:
    """Entry point. Turns expected operational failures — not signed in to 1Password, off the
    VPN, host unreachable, BST not escrowed, timeouts — into one clean red line instead of a
    traceback. Anything that isn't one of these propagates normally so real bugs stay visible."""
    try:
        _app()
    except (ReprovisionError, TimeoutError, ValueError) as e:
        ui.err(str(e))
        raise SystemExit(1) from None


if __name__ == "__main__":
    app()
