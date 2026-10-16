"""
Tests for tart VM host maintenance (orchestrator/tart_host.py). ssh / simplemdm / taskcluster
are mocked, so nothing touches a real host or API. The safety properties get the most
coverage: excluded hosts, minor-only OS updates, the serial cross-check, a surprise major
version, and "any failure leaves the VM workers quarantined".
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from orchestrator import tart_host
from orchestrator.clients import simplemdm
from orchestrator.errors import NotReadyError, ReprovisionError


def _cp(out: str = "", rc: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=out.encode(), stderr=b"")


def _host(ids=("mac-a8876c", "mac-39334d")) -> tart_host.TartHost:
    return tart_host.TartHost(
        hostname="macmini-m4-249",
        fqdn="macmini-m4-249.test.releng.mdc1.mozilla.com",
        worker_ids=list(ids),
    )


def _fake_ssh(table: dict[str, object]):
    """ssh.run stand-in: first table key that is a substring of the command wins.
    Values are str (stdout), a CompletedProcess, or a callable returning one of those."""

    def run(_host, command, *, stdin=None, check=True, timeout=None):
        for key, val in table.items():
            if key in command:
                val = val() if callable(val) else val
                return val if isinstance(val, subprocess.CompletedProcess) else _cp(val)
        return _cp("")

    return run


# --- pure helpers ---


def test_worker_id_for_mac_uses_last_three_octets():
    assert tart_host.worker_id_for_mac("7e:98:e1:a9:78:94") == "mac-a97894"
    assert tart_host.worker_id_for_mac("E6:E7:66:0B:A0:BF") == "mac-0ba0bf"


def test_read_worker_ids_parses_each_slot_in_order():
    out = (
        'sequoia-tester-2 {"display":{"width":1920},"macAddress":"f2:fe:70:39:33:4d","cpuCount":4}\n'
        'sequoia-tester-1 {"macAddress" : "f6:83:6c:a8:87:6c"}\n'
    )
    with patch("orchestrator.tart_host.ssh.run", _fake_ssh({"sequoia-tester-*": out})):
        assert tart_host.read_worker_ids(_host()) == ["mac-a8876c", "mac-39334d"]


def test_version_compare_treats_junk_as_too_old():
    assert tart_host._ver("15.8.1") >= tart_host._ver("15.8")
    assert tart_host._ver("15.3") < tart_host._ver("15.8")
    assert tart_host._ver("") < tart_host._ver("15.8")


# --- resolve / preflight ---


@pytest.mark.parametrize("host", sorted(tart_host.EXCLUDED))
def test_resolve_refuses_excluded_canaries(host):
    with pytest.raises(NotReadyError, match="excluded"):
        tart_host.resolve(host)


def test_preflight_refuses_other_major():
    with patch("orchestrator.tart_host.ssh.run", _fake_ssh({"sw_vers": "27.0.1"})):
        with pytest.raises(NotReadyError, match="macOS 27.0.1"):
            tart_host.step_preflight(_host())


def test_preflight_refuses_when_console_is_not_admin():
    table = {"sw_vers": "15.8.1", "/dev/console": "root"}
    with patch("orchestrator.tart_host.ssh.run", _fake_ssh(table)):
        with pytest.raises(NotReadyError, match="console"):
            tart_host.step_preflight(_host())


def test_preflight_refuses_without_escrowed_bst():
    table = {"sw_vers": "15.3", "/dev/console": "admin", "bootstraptoken": "escrowed to server: NO"}
    with (
        patch("orchestrator.tart_host.ssh.run", _fake_ssh(table)),
        patch("orchestrator.tart_host.ssh.secure_token_status", return_value="ENABLED for user admin"),
    ):
        with pytest.raises(NotReadyError, match="Bootstrap Token"):
            tart_host.step_preflight(_host())


# --- SimpleMDM update_os client ---


def test_update_os_requires_version_type():
    """The API defaults to latest_MAJOR_version. Omitting it must be impossible, not a 27 upgrade."""
    with patch("orchestrator.clients.simplemdm._request") as req:
        with pytest.raises(TypeError):
            simplemdm.update_os(123, os_update_mode="force_update")  # type: ignore[call-arg]
        req.assert_not_called()


def test_update_os_rejects_unknown_values():
    with patch("orchestrator.clients.simplemdm._request") as req:
        with pytest.raises(ValueError):
            simplemdm.update_os(123, os_update_mode="force_update", version_type="latest")
        with pytest.raises(ValueError):
            simplemdm.update_os(123, os_update_mode="now", version_type="latest_minor_version")
        req.assert_not_called()


def test_update_os_posts_minor_request():
    with patch("orchestrator.clients.simplemdm._request") as req:
        simplemdm.update_os(123, os_update_mode="force_update", version_type="latest_minor_version")
    req.assert_called_once_with(
        "POST",
        "/devices/123/update_os",
        data={"os_update_mode": "force_update", "version_type": "latest_minor_version"},
    )


# --- OS update step ---


def test_os_update_skips_when_already_new_enough():
    with (
        patch("orchestrator.tart_host.ssh.run", _fake_ssh({"sw_vers": "15.8.1"})),
        patch("orchestrator.tart_host.simplemdm.update_os") as upd,
    ):
        assert tart_host.step_os_update(_host(), min_os="15.8") == "15.8.1"
        upd.assert_not_called()


def test_os_update_serial_mismatch_never_sends_the_command():
    device = {"id": 42, "attributes": {"serial_number": "AAAA"}}
    with (
        patch("orchestrator.tart_host.ssh.run", _fake_ssh({"sw_vers": "15.3"})),
        patch("orchestrator.tart_host.ssh.platform_serial", return_value="BBBB"),
        patch("orchestrator.tart_host.simplemdm.find_device_by_name", return_value=device),
        patch("orchestrator.tart_host.simplemdm.update_os") as upd,
    ):
        with pytest.raises(ReprovisionError, match="serial mismatch"):
            tart_host.step_os_update(_host(), min_os="15.8")
        upd.assert_not_called()


def _os_update_landing_on(final: str):
    versions = iter(["15.3", "15.3", final])  # skip check, first poll, then the new version
    device = {"id": 42, "attributes": {"serial_number": "SER"}}
    return (
        patch("orchestrator.tart_host.ssh.run", _fake_ssh({"sw_vers": lambda: next(versions)})),
        patch("orchestrator.tart_host.ssh.platform_serial", return_value="SER"),
        patch("orchestrator.tart_host.ssh.wait_for_sshd"),
        patch("orchestrator.tart_host.simplemdm.find_device_by_name", return_value=device),
        patch("orchestrator.tart_host.simplemdm.update_os"),
        patch("orchestrator.tart_host.time.sleep"),
    )


def test_os_update_sends_minor_only_and_returns_new_version():
    p = _os_update_landing_on("15.8.1")
    with p[0], p[1], p[2], p[3], p[4] as upd, p[5]:
        assert tart_host.step_os_update(_host(), min_os="15.8") == "15.8.1"
    upd.assert_called_once_with(42, os_update_mode="force_update", version_type="latest_minor_version")


def test_os_update_rejects_a_major_jump():
    p = _os_update_landing_on("27.0.1")
    with p[0], p[1], p[2], p[3], p[4], p[5]:
        with pytest.raises(ReprovisionError, match="NOT macOS 15"):
            tart_host.step_os_update(_host(), min_os="15.8")


# --- puppet ---


def test_puppet_apply_fails_if_allowlist_missing():
    table = {
        "puppet apply": _cp("Notice: /Stage[main]/x", rc=2),
        "local-network": "",
        "log -1": "checkout abc tart",
    }
    with patch("orchestrator.tart_host.ssh.run", _fake_ssh(table)):
        with pytest.raises(ReprovisionError, match="allowlist"):
            tart_host.step_puppet_apply(_host(), branch="master")


def test_puppet_apply_reports_reboot_required():
    table = {
        "puppet apply": _cp("Notice: ... REBOOT REQUIRED for it to take effect", rc=2),
        "local-network": "10.49.56.161/32",
        "log -1": "checkout abc tart",
    }
    with patch("orchestrator.tart_host.ssh.run", _fake_ssh(table)):
        assert tart_host.step_puppet_apply(_host(), branch="master") is True


def test_puppet_apply_raises_on_failure_exit():
    with patch("orchestrator.tart_host.ssh.run", _fake_ssh({"puppet apply": _cp("Error: boom", rc=4)})):
        with pytest.raises(ReprovisionError, match="exit 4"):
            tart_host.step_puppet_apply(_host(), branch="master")


# --- verify ---


def _verify_with(pulls: str, denials: str = "0"):
    return patch(
        "orchestrator.tart_host.ssh.run",
        _fake_ssh({"tartworker-*.out": pulls, "Local network prohibited": denials}),
    )


def test_verify_passes_when_every_slot_reverted_cleanly():
    with _verify_with("tartworker-1.out 2 0\ntartworker-2.out 1 0"):
        assert tart_host.step_verify_pulls(_host(), boot_epoch=1_800_000_000) is True


def test_verify_raises_on_a_failed_pull():
    with _verify_with("tartworker-1.out 2 1\ntartworker-2.out 1 0"):
        with pytest.raises(ReprovisionError, match="pull failures"):
            tart_host.step_verify_pulls(_host(), boot_epoch=1_800_000_000)


def test_verify_raises_on_local_network_denials():
    with _verify_with("tartworker-1.out 1 0\ntartworker-2.out 1 0", denials="3"):
        with pytest.raises(ReprovisionError, match="Local network prohibited"):
            tart_host.step_verify_pulls(_host(), boot_epoch=1_800_000_000)


def test_verify_quiet_pool_is_unverified_not_a_failure():
    with (
        _verify_with("tartworker-1.out 0 0\ntartworker-2.out 1 0"),
        patch("orchestrator.tart_host.time.sleep"),
        patch("orchestrator.tart_host.time.monotonic", side_effect=[0, 0, 10**9]),
    ):
        assert tart_host.step_verify_pulls(_host(), boot_epoch=1_800_000_000, timeout=60) is False


# --- the whole flow: failures leave the workers quarantined ---


def _flow_patches(**overrides):
    steps = {
        "resolve": MagicMock(return_value=_host()),
        "step_preflight": MagicMock(return_value="15.3"),
        "step_resolve_workers": MagicMock(),
        "step_quarantine": MagicMock(),
        "step_drain": MagicMock(),
        "step_os_update": MagicMock(return_value="15.8.1"),
        "step_puppet_apply": MagicMock(return_value=True),
        "step_reboot_healthy": MagicMock(return_value=1_800_000_000),
        "step_wait_registered": MagicMock(),
        "step_unquarantine": MagicMock(),
        "step_verify_pulls": MagicMock(return_value=True),
    }
    steps.update(overrides)
    return steps


def _run_flow(steps, **kw):
    with patch.multiple("orchestrator.tart_host", **steps), patch("orchestrator.tart_host.ui"):
        tart_host.update("macmini-m4-249", **kw)


def test_flow_happy_path_order():
    steps = _flow_patches()
    _run_flow(steps)
    steps["step_unquarantine"].assert_called_once()
    assert steps["step_quarantine"].call_count == 1


def test_flow_failure_before_unquarantine_leaves_workers_quarantined():
    steps = _flow_patches(step_os_update=MagicMock(side_effect=ReprovisionError("still on 15.3")))
    with pytest.raises(ReprovisionError):
        _run_flow(steps)
    steps["step_quarantine"].assert_called_once()
    steps["step_unquarantine"].assert_not_called()
    steps["step_puppet_apply"].assert_not_called()


def test_flow_verify_failure_requarantines():
    steps = _flow_patches(step_verify_pulls=MagicMock(side_effect=ReprovisionError("pull failures")))
    with pytest.raises(ReprovisionError):
        _run_flow(steps)
    steps["step_unquarantine"].assert_called_once()
    assert steps["step_quarantine"].call_count == 2  # initial + re-quarantine


def test_flow_puppet_only_skips_os_update_and_reboots_only_if_needed():
    steps = _flow_patches(
        step_puppet_apply=MagicMock(return_value=False),
        boot_time=MagicMock(return_value=1_800_000_000),
    )
    _run_flow(steps, puppet_only=True)
    steps["step_os_update"].assert_not_called()
    steps["step_reboot_healthy"].assert_not_called()
