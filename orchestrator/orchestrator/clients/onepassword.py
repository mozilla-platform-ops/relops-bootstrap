"""
1Password helper. Shells out to `op` rather than depending on a Python SDK, so the
operator's existing `op` config (Service Account token or SSO) is the source of truth.

Used by the workflow's `deliver_vault` step today. When the SCEP + broker
infrastructure is live and `deliver_vault` becomes a no-op, this helper is unused.
"""

from __future__ import annotations

import subprocess


def read(reference: str) -> bytes:
    """
    Read a 1Password reference like:
        op://RelOps Vault/vault-gecko_t_osx_1500_m4/notesPlain

    Returns the raw value as bytes. The `op` CLI must be authenticated on the operator's
    laptop (e.g., `op signin` or OP_SERVICE_ACCOUNT_TOKEN env var).
    """
    cp = subprocess.run(
        ["op", "read", reference],
        check=True,
        capture_output=True,
        timeout=30,
    )
    return cp.stdout
