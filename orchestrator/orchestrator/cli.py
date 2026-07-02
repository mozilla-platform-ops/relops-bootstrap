"""
CLI entry point. `reprovision <hostname>` for full workflow; individual subcommands
for re-running steps after partial failure.
"""

from __future__ import annotations

import typer

from . import workflow

app = typer.Typer(no_args_is_help=True, help="Drive an end-to-end EACS reprovision of a CI worker.")


@app.command()
def run(
    hostname: str = typer.Argument(..., help="Short hostname, e.g. macmini-m4-81"),
    unquarantine: bool = typer.Option(
        False,
        "--unquarantine",
        help="Return the host to service at the end. Default off: host stays quarantined "
        "through reprovision (needs a queue:quarantine-scoped credential).",
    ),
    rotate_admin: bool = typer.Option(
        False,
        "--rotate-admin/--no-rotate-admin",
        help="Rotate the SimpleMDM auto-admin password after landing (default OFF). Only works "
        "for enrollments using an auto-generated managed admin password — incompatible with the "
        "fixed bootstrap password the mint needs, so off by default.",
    ),
) -> None:
    """Full workflow: quarantine -> drain -> wipe -> reenroll -> mint -> BST -> bootstrap -> rotate.

    Vault is fetched by the bootstrap script over mTLS (SCEP), so there is no vault-delivery step.
    By default the host stays quarantined throughout; pass --unquarantine to return it to service.
    """
    workflow.reprovision(hostname, unquarantine=unquarantine, rotate_admin=rotate_admin)


@app.command()
def quarantine(hostname: str) -> None:
    workflow.step_quarantine(workflow.resolve(hostname))


@app.command()
def unquarantine(hostname: str) -> None:
    workflow.step_unquarantine(workflow.resolve(hostname))


@app.command()
def drain(hostname: str) -> None:
    workflow.step_drain(workflow.resolve(hostname))


@app.command()
def wipe(hostname: str) -> None:
    """Trigger EACS-equivalent wipe via SimpleMDM API."""
    workflow.step_wipe(workflow.resolve(hostname))


@app.command()
def wait_reenroll(hostname: str) -> None:
    workflow.step_wait_for_reenroll(workflow.resolve(hostname))


@app.command()
def mint(hostname: str) -> None:
    """Mint the admin SecureToken via an interactive password login (idempotent)."""
    workflow.step_mint(workflow.resolve(hostname))


@app.command()
def escrow_bst(hostname: str) -> None:
    """SSH in and run `sudo profiles install -type bootstraptoken -user admin -password …` (needs mint first)."""
    workflow.step_escrow_bst(workflow.resolve(hostname))


@app.command()
def trigger_bootstrap(hostname: str) -> None:
    workflow.step_trigger_bootstrap_script(workflow.resolve(hostname))


@app.command()
def rotate_admin(hostname: str) -> None:
    """Rotate the auto-admin password to a fresh SimpleMDM-generated one (needs escrowed BST)."""
    workflow.step_rotate_admin_password(workflow.resolve(hostname))


@app.command()
def wait_sentinel(hostname: str) -> None:
    workflow.step_wait_for_sentinel(workflow.resolve(hostname))


if __name__ == "__main__":
    app()
