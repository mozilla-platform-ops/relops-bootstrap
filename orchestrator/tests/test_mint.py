"""
Tests for the SecureToken mint, BST escrow, admin-password rotation, and the
reprovision() step sequence. The ssh / simplemdm / taskcluster layers are mocked
so nothing touches a real host or API.
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


# --- mint ---

def test_mint_is_idempotent_when_already_enabled():
    """If admin already holds a SecureToken, we must NOT attempt a password login."""
    with patch("orchestrator.workflow.ssh.wait_for_sshd"), \
         patch("orchestrator.workflow.ssh.secure_token_status", return_value="ENABLED for user admin"), \
         patch("orchestrator.workflow.ssh.password_login") as login:
        workflow.step_mint(_ctx())
        login.assert_not_called()


def test_mint_logs_in_then_verifies_enabled():
    """DISABLED -> password_login -> then status flips to ENABLED -> success."""
    statuses = iter(["DISABLED for user admin", "ENABLED for user admin"])
    with patch("orchestrator.workflow.ssh.wait_for_sshd"), \
         patch("orchestrator.workflow.ssh.secure_token_status", side_effect=lambda *_: next(statuses)), \
         patch("orchestrator.workflow.ssh.password_login") as login:
        workflow.step_mint(_ctx())
        login.assert_called_once()


def test_mint_raises_if_token_never_enables():
    """If the token never comes up ENABLED after the login, the step must fail loudly."""
    with patch("orchestrator.workflow.ssh.wait_for_sshd"), \
         patch("orchestrator.workflow.ssh.secure_token_status", return_value="DISABLED for user admin"), \
         patch("orchestrator.workflow.ssh.password_login"), \
         patch("orchestrator.workflow.time.sleep"):  # don't wait through the retries
        with pytest.raises(RuntimeError):
            workflow.step_mint(_ctx())


# --- escrow_bst ---

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


# --- rotate ---

def test_rotate_calls_simplemdm():
    with patch("orchestrator.workflow.simplemdm.rotate_admin_password") as rot:
        workflow.step_rotate_admin_password(_ctx())
        rot.assert_called_once_with(1962176)


# --- reprovision() sequence ---

def _run_reprovision_capturing(**kwargs) -> list[str]:
    """Run reprovision() with every step stubbed; return the ordered list of step names."""
    calls: list[str] = []

    def rec(name):
        return lambda ctx: calls.append(name)

    with patch("orchestrator.workflow.resolve", return_value=_ctx()), \
         patch("orchestrator.workflow.step_quarantine", rec("quarantine")), \
         patch("orchestrator.workflow.step_drain", rec("drain")), \
         patch("orchestrator.workflow.step_wipe", rec("wipe")), \
         patch("orchestrator.workflow.step_wait_for_reenroll", rec("reenroll")), \
         patch("orchestrator.workflow.step_mint", rec("mint")), \
         patch("orchestrator.workflow.step_escrow_bst", rec("escrow")), \
         patch("orchestrator.workflow.step_trigger_bootstrap_script", rec("trigger")), \
         patch("orchestrator.workflow.step_wait_for_sentinel", rec("sentinel")), \
         patch("orchestrator.workflow.step_rotate_admin_password", rec("rotate")), \
         patch("orchestrator.workflow.step_unquarantine", rec("unquarantine")):
        workflow.reprovision("macmini-m4-81", **kwargs)
    return calls


def test_reprovision_default_flow():
    """Default: mint before escrow, NO rotate, NO unquarantine, no vault step."""
    calls = _run_reprovision_capturing()
    assert "unquarantine" not in calls  # quarantine persists by default
    assert "rotate" not in calls  # SimpleMDM rotate incompatible with fixed-password mint; off by default
    assert calls.index("mint") < calls.index("escrow")  # mint precedes BST escrow
    assert not hasattr(workflow, "step_deliver_vault")  # Path C: no 1Password vault drop


def test_reprovision_rotate_flag_on():
    """With rotate_admin=True the rotate step runs, only after the worker lands."""
    calls = _run_reprovision_capturing(rotate_admin=True)
    assert calls.index("sentinel") < calls.index("rotate")


def test_reprovision_unquarantine_flag_returns_to_service():
    calls = _run_reprovision_capturing(unquarantine=True)
    assert calls[-1] == "unquarantine"  # runs, and last (after rotate)
