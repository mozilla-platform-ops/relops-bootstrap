"""
Runtime secret resolution — so operators never paste creds into the shell.

For each secret the orchestrator needs, resolution order is:

  1. the explicit `REPROVISION_*` env var (override / CI), else
  2. a configured *reference* (`REPROVISION_*_REF`):
       - "op://Vault/Item/field"  → read via the 1Password CLI (`op read`)
       - anything else            → a GCP Secret Manager secret id, read via
                                     `gcloud secrets versions access` (reuses the
                                     operator's existing `gcloud auth login`)

So the operator keeps *references* in `.env` (safe to commit-adjacent) and the
actual secrets live in Secret Manager / 1Password, fetched at run time. One-time
`gcloud auth login` / `op signin` and no secret ever touches the shell/history.

Resolved values are cached for the process (fetch once per run).
"""

from __future__ import annotations

import functools
import subprocess

from .config import get_settings


def _op_read(ref: str) -> str:
    cp = subprocess.run(["op", "read", ref], capture_output=True, text=True, timeout=30, check=True)
    return cp.stdout.rstrip("\r\n")


def _gcloud_secret(secret_id: str, project: str) -> str:
    cp = subprocess.run(
        ["gcloud", "secrets", "versions", "access", "latest", "--secret", secret_id, "--project", project],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return cp.stdout.rstrip("\r\n")


def _resolve(direct: str, ref: str, project: str) -> str:
    """env value wins; else fetch from the ref (op:// or Secret Manager id); else ''."""
    if direct:
        return direct
    if not ref:
        return ""
    if ref.startswith("op://"):
        return _op_read(ref)
    return _gcloud_secret(ref, project)


@functools.lru_cache(maxsize=None)
def simplemdm_api_key() -> str:
    s = get_settings()
    return _resolve(s.simplemdm_api_key, s.simplemdm_api_key_ref, s.gcp_project)


@functools.lru_cache(maxsize=None)
def tc_credentials() -> tuple[str, str]:
    s = get_settings()
    return (
        _resolve(s.tc_client_id, s.tc_client_id_ref, s.gcp_project),
        _resolve(s.tc_access_token, s.tc_access_token_ref, s.gcp_project),
    )


@functools.lru_cache(maxsize=None)
def ssh_admin_password() -> str:
    s = get_settings()
    pw = _resolve(s.ssh_admin_password, s.ssh_admin_password_ref, s.gcp_project)
    if not pw:
        raise RuntimeError(
            "No admin password: set REPROVISION_SSH_ADMIN_PASSWORD, or "
            "REPROVISION_SSH_ADMIN_PASSWORD_REF (an op:// ref or a Secret Manager id)."
        )
    return pw
