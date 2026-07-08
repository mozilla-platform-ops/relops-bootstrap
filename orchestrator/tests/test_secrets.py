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


def test_ssh_admin_password_raises_when_unresolved():
    secrets.ssh_admin_password.cache_clear()
    with patch("orchestrator.secrets._resolve", return_value=""):
        with pytest.raises(RuntimeError):
            secrets.ssh_admin_password()
    secrets.ssh_admin_password.cache_clear()
