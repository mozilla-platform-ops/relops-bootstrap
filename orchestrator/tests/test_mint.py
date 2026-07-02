"""
Tests for the SecureToken mint + BST escrow steps.

The mint is the load-bearing bit of Path C automation: DEP skips Setup Assistant,
so admin holds no SecureToken until an interactive (PAM) password login. These tests
mock the ssh layer so nothing touches a real host.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from orchestrator import workflow


def _ctx():
    return workflow.HostContext(
        hostname="macmini-m4-81",
        fqdn="macmini-m4-81.test.releng.mdc1.mozilla.com",
        role="gecko_t_osx_1500_m4",
        worker_pool_id="releng-hardware/gecko-t-osx-1500-m4",
        simplemdm_device_id=1962176,
    )


def test_mint_is_idempotent_when_already_enabled():
    """If admin already holds a SecureToken, we must NOT attempt a password login."""
    with patch("orchestrator.workflow.ssh.secure_token_status", return_value="ENABLED for user admin"), \
         patch("orchestrator.workflow.ssh.password_login") as login:
        workflow.step_mint(_ctx())
        login.assert_not_called()


def test_mint_logs_in_then_verifies_enabled():
    """DISABLED -> password_login -> then status flips to ENABLED -> success."""
    statuses = iter(["DISABLED for user admin", "ENABLED for user admin"])
    with patch("orchestrator.workflow.ssh.secure_token_status", side_effect=lambda *_: next(statuses)), \
         patch("orchestrator.workflow.ssh.password_login") as login:
        workflow.step_mint(_ctx())
        login.assert_called_once()


def test_mint_raises_if_token_never_enables():
    """If the token never comes up ENABLED after the login, the step must fail loudly."""
    with patch("orchestrator.workflow.ssh.secure_token_status", return_value="DISABLED for user admin"), \
         patch("orchestrator.workflow.ssh.password_login"), \
         patch("orchestrator.workflow.time.sleep"):  # don't actually wait through the retries
        with pytest.raises(RuntimeError):
            workflow.step_mint(_ctx())


def test_escrow_bst_is_non_interactive():
    """escrow_bst must pass -user/-password (the bare form prompts and fails)."""
    cmds = []

    def fake_run(host, cmd, **_):
        cmds.append(cmd)
        class CP:
            stdout = b"profiles: Bootstrap Token escrowed to server: YES"
        return CP()

    with patch("orchestrator.workflow.ssh.run", side_effect=fake_run):
        workflow.step_escrow_bst(_ctx())
    install = next(c for c in cmds if "profiles install" in c)
    assert "-type bootstraptoken" in install
    assert "-user admin" in install
    assert "-password" in install


def test_escrow_bst_raises_when_not_escrowed():
    with patch("orchestrator.workflow.ssh.run") as run:
        run.return_value.stdout = b"profiles: Bootstrap Token escrowed to server: NO"
        with pytest.raises(RuntimeError):
            workflow.step_escrow_bst(_ctx())
