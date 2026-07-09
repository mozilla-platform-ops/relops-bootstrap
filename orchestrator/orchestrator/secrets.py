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
from .errors import ReprovisionError


class SecretResolutionError(ReprovisionError):
    """A secret couldn't be fetched — almost always "not signed in" or "not installed"."""


def _last_line(text: str) -> str:
    lines = [ln for ln in (text or "").strip().splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def _vault_of(ref: str) -> str:
    """Vault segment of an op://<vault>/<item>/<field> ref, for a helpful error."""
    parts = ref.split("/")
    return parts[2] if ref.startswith("op://") and len(parts) > 2 else "the"


def _op_read(ref: str) -> str:
    try:
        cp = subprocess.run(["op", "read", ref], capture_output=True, text=True, timeout=30, check=True)
    except FileNotFoundError:
        raise SecretResolutionError(
            f"1Password CLI (`op`) isn't installed — `brew install 1password-cli` — needed to read {ref}."
        ) from None
    except subprocess.CalledProcessError as e:
        detail = _last_line(e.stderr)
        low = detail.lower()
        # A 403 means signed in but not authorized — re-signin won't help; you need vault access.
        if "403" in low or "forbidden" in low or "authorized" in low:
            hint = (
                f"you're signed in, but your 1Password account isn't authorized for this item — "
                f"ask a RelOps admin to add you to the '{_vault_of(ref)}' vault"
            )
        else:  # 401 / "not currently signed in" / etc.
            hint = "run  eval $(op signin)  and retry"
        raise SecretResolutionError(
            f"couldn't read {ref} from 1Password — {hint}."
            + (f"\n    op: {detail}" if detail else "")
        ) from None
    return cp.stdout.rstrip("\r\n")


def _gcloud_secret(secret_id: str, project: str) -> str:
    try:
        cp = subprocess.run(
            ["gcloud", "secrets", "versions", "access", "latest", "--secret", secret_id, "--project", project],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
    except FileNotFoundError:
        raise SecretResolutionError(
            f"gcloud isn't installed — needed to read Secret Manager id '{secret_id}'."
        ) from None
    except subprocess.CalledProcessError as e:
        detail = _last_line(e.stderr)
        low = detail.lower()
        if "permission" in low or "403" in low or "forbidden" in low or "denied" in low:
            hint = f"your account lacks access to secret '{secret_id}' in project '{project}' — ask for IAM access"
        else:
            hint = "run  gcloud auth login  and retry"
        raise SecretResolutionError(
            f"couldn't read Secret Manager id '{secret_id}' — {hint}."
            + (f"\n    gcloud: {detail}" if detail else "")
        ) from None
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
    key = _resolve(s.simplemdm_api_key, s.simplemdm_api_key_ref, s.gcp_project)
    if not key:
        raise RuntimeError(
            "No SimpleMDM API key: set REPROVISION_SIMPLEMDM_API_KEY, or "
            "REPROVISION_SIMPLEMDM_API_KEY_REF (an op:// ref or a Secret Manager id)."
        )
    return key


@functools.lru_cache(maxsize=None)
def ssh_admin_key() -> str:
    """The admin account's private key, or '' if not configured (then ssh falls back to the
    agent / default identities). Optional — unlike the password, no hard error when unset."""
    s = get_settings()
    return _resolve(s.ssh_admin_key, s.ssh_admin_key_ref, s.gcp_project)


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
