"""
Tests for runtime secret resolution (env → op:// → GCP Secret Manager).
subprocess is mocked so nothing shells out to `op`/`gcloud`.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from orchestrator import secrets


def test_resolve_direct_value_wins():
    # even with a ref present, an explicit env value takes precedence
    assert secrets._resolve("direct", "op://V/I/f", "proj") == "direct"


def test_resolve_empty_ref_returns_empty():
    assert secrets._resolve("", "", "proj") == ""


def test_resolve_op_ref_reads_1password():
    with patch("orchestrator.secrets._op_read", return_value="from-op") as m:
        assert secrets._resolve("", "op://Vault/Item/field", "proj") == "from-op"
        m.assert_called_once_with("op://Vault/Item/field")


def test_resolve_plain_ref_reads_secret_manager():
    with patch("orchestrator.secrets._gcloud_secret", return_value="from-sm") as m:
        assert secrets._resolve("", "my-secret-id", "relops-bootstrap") == "from-sm"
        m.assert_called_once_with("my-secret-id", "relops-bootstrap")


def test_op_read_invokes_op_and_strips_newline():
    class CP:
        stdout = "value\n"

    with patch("orchestrator.secrets.subprocess.run", return_value=CP()) as m:
        assert secrets._op_read("op://V/I/f") == "value"
        assert m.call_args[0][0][:2] == ["op", "read"]


def test_gcloud_secret_invokes_gcloud_and_strips_newline():
    class CP:
        stdout = "token\n"

    with patch("orchestrator.secrets.subprocess.run", return_value=CP()) as m:
        assert secrets._gcloud_secret("sid", "proj") == "token"
        argv = m.call_args[0][0]
        assert argv[:3] == ["gcloud", "secrets", "versions"]
        assert "--secret" in argv and "sid" in argv and "proj" in argv


def test_op_read_not_signed_in_gives_friendly_error():
    import subprocess

    err = subprocess.CalledProcessError(1, ["op", "read"], stderr="[ERROR] you are not currently signed in")
    with patch("orchestrator.secrets.subprocess.run", side_effect=err):
        with pytest.raises(secrets.SecretResolutionError) as ei:
            secrets._op_read("op://V/I/f")
    assert "op signin" in str(ei.value)  # tells the operator the one-command fix


def test_op_read_forbidden_points_at_vault_access_not_signin():
    import subprocess

    err = subprocess.CalledProcessError(
        1, ["op", "read"],
        stderr="[ERROR] ... (403) (Forbidden), You aren't authorized to access this resource.",
    )
    with patch("orchestrator.secrets.subprocess.run", side_effect=err):
        with pytest.raises(secrets.SecretResolutionError) as ei:
            secrets._op_read("op://RelOps/SimpleMDM API admin/password")
    msg = str(ei.value)
    assert "authorized" in msg.lower() and "RelOps" in msg  # names the vault to get access to
    assert "op signin" not in msg  # don't misdirect an authorization problem to re-signin


def test_op_read_not_installed_gives_friendly_error():
    with patch("orchestrator.secrets.subprocess.run", side_effect=FileNotFoundError):
        with pytest.raises(secrets.SecretResolutionError) as ei:
            secrets._op_read("op://V/I/f")
    assert "install" in str(ei.value).lower()


def test_ssh_admin_password_raises_when_unresolved():
    secrets.ssh_admin_password.cache_clear()
    with patch("orchestrator.secrets._resolve", return_value=""):
        with pytest.raises(RuntimeError):
            secrets.ssh_admin_password()
    secrets.ssh_admin_password.cache_clear()


def test_simplemdm_api_key_raises_when_unresolved():
    secrets.simplemdm_api_key.cache_clear()
    with patch("orchestrator.secrets._resolve", return_value=""):
        with pytest.raises(RuntimeError):
            secrets.simplemdm_api_key()
    secrets.simplemdm_api_key.cache_clear()
