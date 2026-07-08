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

    with patch("orchestrator.workflow.ssh.run", side_effect=fake_run), \
         patch("orchestrator.workflow.ssh_admin_password", return_value="s3cr3t-pw"):
        workflow.step_escrow_bst(_ctx())
    install = next(c for c in cmds if "profiles install" in c)
    assert "-type bootstraptoken" in install
    assert "-user admin" in install
    assert "-password" in install
    assert "s3cr3t-pw" in install  # the resolved password is passed, not an empty string


def test_escrow_bst_ssh_error_does_not_leak_password():
    """A failed escrow must raise a clean error — the admin password must never appear."""
    import subprocess

    def boom(host, cmd, **_):
        raise subprocess.CalledProcessError(255, ["ssh", host, cmd])

    with patch("orchestrator.workflow.ssh.run", side_effect=boom), \
         patch("orchestrator.workflow.ssh_admin_password", return_value="s3cr3t-pw"):
        with pytest.raises(RuntimeError) as ei:
            workflow.step_escrow_bst(_ctx())
    assert "s3cr3t-pw" not in str(ei.value)
    assert ei.value.__suppress_context__  # `from None` — no chained CalledProcessError leaks the cmd


def test_escrow_bst_raises_when_not_escrowed():
    with patch("orchestrator.workflow.ssh.run") as run, \
         patch("orchestrator.workflow.ssh_admin_password", return_value="s3cr3t-pw"):
        run.return_value.stdout = b"profiles: Bootstrap Token escrowed to server: NO"
        with pytest.raises(RuntimeError):
            workflow.step_escrow_bst(_ctx())


# --- wipe guard ---

def test_wipe_aborts_without_escrowed_bst():
    """Refuse to wipe a box that can't EACS (no escrowed BST) — else it full-obliterates."""
    with patch("orchestrator.workflow.ssh.run") as run:
        run.return_value.returncode = 0  # ssh succeeded; the box genuinely has no BST
        run.return_value.stdout = b"profiles: Bootstrap Token escrowed to server: NO"
        with pytest.raises(RuntimeError, match="not escrowed"):
            workflow.step_wipe(_ctx())


def test_wipe_aborts_when_ssh_check_fails_without_claiming_no_bst():
    """An ssh failure must NOT masquerade as a missing Bootstrap Token, and must not wipe."""
    with patch("orchestrator.workflow.ssh.run") as run, \
         patch("orchestrator.workflow.simplemdm.wipe") as wipe:
        run.return_value.returncode = 255  # e.g. first-connection host key / VPN down
        run.return_value.stdout = b""
        run.return_value.stderr = b"Host key verification failed."
        with pytest.raises(RuntimeError, match="couldn't verify"):
            workflow.step_wipe(_ctx())
        wipe.assert_not_called()


def test_wipe_proceeds_with_escrowed_bst():
    with patch("orchestrator.workflow.ssh.run") as run, \
         patch("orchestrator.workflow.simplemdm.get_device", return_value={"attributes": {"enrolled_at": "x"}}), \
         patch("orchestrator.workflow.simplemdm.wipe") as wipe:
        run.return_value.returncode = 0
        run.return_value.stdout = b"profiles: Bootstrap Token escrowed to server: YES"
        workflow.step_wipe(_ctx())
        wipe.assert_called_once()


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
         patch("orchestrator.workflow.step_wait_for_sentinel", rec("sentinel")), \
         patch("orchestrator.workflow.step_unquarantine", rec("unquarantine")):
        workflow.reprovision("macmini-m4-81", **kwargs)
    return calls


def test_reprovision_default_flow():
    """Default: mint before escrow, NO unquarantine, no vault step."""
    calls = _run_reprovision_capturing()
    assert "unquarantine" not in calls  # quarantine persists by default
    assert calls.index("mint") < calls.index("escrow")  # mint precedes BST escrow
    assert not hasattr(workflow, "step_deliver_vault")  # Path C: no 1Password vault drop
    assert not hasattr(workflow, "step_rotate_admin_password")  # rotation is a DEP-config concern


def test_reprovision_unquarantine_flag_returns_to_service():
    calls = _run_reprovision_capturing(unquarantine=True)
    assert calls[-1] == "unquarantine"  # runs, and last
