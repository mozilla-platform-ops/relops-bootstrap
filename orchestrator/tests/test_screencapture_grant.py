"""
Tests for the Screen Recording (ScreenCapture TCC) grant step. Bug 2073303.

The ssh layer is mocked throughout; nothing touches a real host.

The behaviour that matters here is the three-way split on the payload's exit code.
Getting it wrong is expensive in opposite directions: treating a real failure as a
skip hands back a host that silently cannot screen-capture (the original bug, which
took 30 days and 499 oranges to notice), while treating a skip as a failure aborts a
reprovision over a host that merely happened to be mid-task.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from orchestrator import workflow
from orchestrator.errors import ReprovisionError


def _ctx():
    return workflow.HostContext(
        hostname="macmini-m4-265",
        fqdn="macmini-m4-265.test.releng.mdc1.mozilla.com",
        role="gecko_t_osx_1500_m4",
        worker_pool_id="releng-hardware/gecko-t-osx-1500-m4",
    )


class _CP:
    def __init__(self, out: str):
        self.stdout = out.encode()
        self.returncode = 0


def _run_with(output: str):
    """Patch ssh so the payload 'returns' output; yields the run mock."""
    return patch(
        "orchestrator.workflow.ssh.run", side_effect=lambda *a, **k: _CP(output)
    )


def test_granted_is_success():
    with (
        patch("orchestrator.workflow.ssh.write_file_as_root"),
        patch(
            "orchestrator.workflow._screencapture_script", return_value="#!/bin/bash\n"
        ),
        _run_with("[screencapture] granted /usr/local/bin/start-worker (2/0)\nrc=0"),
    ):
        workflow.step_screencapture_grant(_ctx())  # must not raise


@pytest.mark.parametrize(
    "reason",
    [
        "[SKIP] SIP is off — macos_tcc_perms already grants this host",
        "[SKIP] host is running a task — retry when idle",
        "[SKIP] cltbld does not own the console session yet",
    ],
)
def test_skip_conditions_do_not_raise(reason):
    """Exit 3 is 'not applicable / not now'. The host is still fine to hand back."""
    with (
        patch("orchestrator.workflow.ssh.write_file_as_root"),
        patch(
            "orchestrator.workflow._screencapture_script", return_value="#!/bin/bash\n"
        ),
        _run_with(f"{reason}\nrc=3"),
        patch("orchestrator.workflow.time.sleep"),
    ):
        workflow.step_screencapture_grant(_ctx())  # must not raise


def _run_sequence(*outputs: str):
    """Patch ssh so successive payload runs return successive outputs."""
    it = iter(outputs)

    def fake(_host, cmd, **_k):
        return _CP(next(it) if "rc=$?" in cmd else "")

    return patch("orchestrator.workflow.ssh.run", side_effect=fake)


def test_transient_skip_is_retried_until_granted():
    """Right after the bootstrap cltbld may not own the console yet. Giving up on the
    first skip hands the host back with no grant, so wait it out instead."""
    with (
        patch("orchestrator.workflow.ssh.write_file_as_root"),
        patch(
            "orchestrator.workflow._screencapture_script", return_value="#!/bin/bash\n"
        ),
        _run_sequence(
            "[SKIP] cltbld does not own the console session yet\nrc=3",
            "[SKIP] cltbld does not own the console session yet\nrc=3",
            "[screencapture] Screen Recording granted\nrc=0",
        ),
        patch("orchestrator.workflow.time.sleep") as sleep,
        patch("orchestrator.workflow.ui.ok") as ok,
    ):
        workflow.step_screencapture_grant(_ctx())
    assert sleep.call_count == 2
    ok.assert_called_once()


def test_permanent_skip_is_not_retried():
    """SIP off will not change while we wait; do not burn five minutes on it."""
    with (
        patch("orchestrator.workflow.ssh.write_file_as_root") as write,
        patch(
            "orchestrator.workflow._screencapture_script", return_value="#!/bin/bash\n"
        ),
        _run_with("[SKIP] SIP is off — macos_tcc_perms already grants this host\nrc=3"),
        patch("orchestrator.workflow.time.sleep") as sleep,
    ):
        workflow.step_screencapture_grant(_ctx())
    sleep.assert_not_called()
    write.assert_called_once()


def test_transient_skip_gives_up_after_the_retry_budget():
    with (
        patch("orchestrator.workflow.ssh.write_file_as_root") as write,
        patch(
            "orchestrator.workflow._screencapture_script", return_value="#!/bin/bash\n"
        ),
        _run_with("[SKIP] host is running a task — retry when idle\nrc=3"),
        patch("orchestrator.workflow.time.sleep"),
        patch("orchestrator.workflow.ui.warn") as warn,
    ):
        workflow.step_screencapture_grant(_ctx())  # must not raise
    assert write.call_count == workflow.SCREENCAPTURE_ATTEMPTS
    warn.assert_called_once()


@pytest.mark.parametrize(
    "output",
    [
        "[ERROR] worker binary is not Developer-ID signed (Identifier=a.out)\nrc=1",
        "[ERROR] a ScreenCapture PPPC override is installed (2 entries)\nrc=1",
        "[ERROR] /usr/local/bin/start-worker landed flags=12 (MDM-managed, TCC ignores it)\nrc=1",
        "[ERROR] /usr/local/bin/start-worker not granted (got 0/6)\nrc=1",
    ],
)
def test_real_failures_raise(output):
    """A host that cannot hold the grant must fail loudly, not be quietly returned to the pool."""
    with (
        patch("orchestrator.workflow.ssh.write_file_as_root"),
        patch(
            "orchestrator.workflow._screencapture_script", return_value="#!/bin/bash\n"
        ),
        _run_with(output),
    ):
        with pytest.raises(ReprovisionError):
            workflow.step_screencapture_grant(_ctx())


def test_missing_rc_is_treated_as_failure():
    """Truncated/garbled output must not be read as success."""
    with (
        patch("orchestrator.workflow.ssh.write_file_as_root"),
        patch(
            "orchestrator.workflow._screencapture_script", return_value="#!/bin/bash\n"
        ),
        _run_with("something went sideways"),
    ):
        with pytest.raises(ReprovisionError):
            workflow.step_screencapture_grant(_ctx())


def test_payload_is_cleaned_up_from_the_host():
    """The staged script carries the admin credential; it must not be left behind."""
    calls = []

    def _run(fqdn, cmd, **kw):
        calls.append(cmd)
        return _CP("rc=0")

    with (
        patch("orchestrator.workflow.ssh.write_file_as_root"),
        patch(
            "orchestrator.workflow._screencapture_script", return_value="#!/bin/bash\n"
        ),
        patch("orchestrator.workflow.ssh.run", side_effect=_run),
    ):
        workflow.step_screencapture_grant(_ctx())

    assert any(
        c.startswith("sudo rm -f ") and workflow.SCREENCAPTURE_REMOTE in c
        for c in calls
    ), calls


def test_script_substitutes_the_credential_placeholders():
    """The packaged body must not reach the host with placeholders intact."""
    with patch("orchestrator.workflow.ssh_admin_password", return_value="s3cr3t"):
        body = workflow._screencapture_script()
    assert 'ADMIN_PASSWORD="INSERT_HERE"' not in body
    assert 'ADMIN_USER="INSERT_USER_HERE"' not in body
    assert "s3cr3t" in body
    # The payload's own guard against an unsubstituted placeholder must survive.
    assert '[ "$ADMIN_PASSWORD" = "INSERT_HERE" ]' in body


def test_script_grants_and_verifies_bash():
    """RELOPS-2454: the screenshot LaunchAgent runs as /bin/bash, so bash must be both
    granted and part of the success check. Dropping it from either leaves every failure
    screenshot on a SIP-on host wallpaper-only, with the step still reporting success.
    """
    with patch("orchestrator.workflow.ssh_admin_password", return_value="s3cr3t"):
        body = workflow._screencapture_script()
    assert "SCREENSHOT_CLIENT=/bin/bash" in body
    # granted() + verify; the guard keeps an emptied CLIENTS legal under bash 3.2 set -u
    assert body.count('${CLIENTS[@]+"${CLIENTS[@]}"} "$SCREENSHOT_CLIENT"') == 2
    assert 'keystroke "/bin/bash"' in body


def test_unsigned_worker_binaries_still_grant_bash():
    """Roles without taskcluster_signed_binaries (the staging pools) run ad-hoc worker
    builds. That used to be a hard fail, which ended every staging reprovision in an
    error before bash was granted. It must skip only the worker binaries."""
    with patch("orchestrator.workflow.ssh_admin_password", return_value="s3cr3t"):
        body = workflow._screencapture_script()
    assert 'fail "worker binary is not Developer-ID signed' not in body
    assert "CLIENTS=()" in body and "GRANT_WORKERS=0" in body
    assert 'osascript - "$creds" "$GRANT_WORKERS"' in body
    assert "if grantWorkers then set workerNames to" in body


def test_step_is_in_both_flows():
    """Regression guard: the grant must not silently drop out of the sequences.

    EACS wipes TCC, so a reprovision that skips this hands back a host that cannot
    screen-capture and says nothing about it.
    """
    import inspect

    assert "step_screencapture_grant" in inspect.getsource(workflow.reprovision)
    assert "step_screencapture_grant" in inspect.getsource(workflow.provision)
