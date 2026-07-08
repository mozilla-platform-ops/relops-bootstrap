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
) -> None:
    """Full workflow: quarantine -> drain -> wipe -> reenroll -> mint -> BST -> bootstrap.

    Vault is fetched by the bootstrap script over mTLS (SCEP), so there is no vault-delivery step.
    By default the host stays quarantined throughout; pass --unquarantine to return it to service.
    """
    workflow.reprovision(hostname, unquarantine=unquarantine)


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
def wait_sentinel(hostname: str) -> None:
    workflow.step_wait_for_sentinel(workflow.resolve(hostname))


if __name__ == "__main__":
    app()
