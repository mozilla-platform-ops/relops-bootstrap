"""
Tests for the Intel builder macOS 15 upgrade (orchestrator/builder_upgrade.py). ssh and
taskcluster are mocked. Coverage follows the incidents the steps exist for: the T2 gate,
relops (not admin) as the login, kcpassword aside before the console logout and back before
the reboot, a host that comes back on 10.15, an empty puppet catalog, the sudo password never
in argv, and "any failure leaves the worker quarantined".
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from orchestrator import builder_upgrade as bu
from orchestrator.errors import NotReadyError, ReprovisionError

FQDN = "macmini-r8-124.test.releng.mdc1.mozilla.com"


def _cp(out: str = "", rc: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=out.encode(), stderr=b"")


def _host(**kw) -> bu.BuilderHost:
    h = bu.BuilderHost(hostname="macmini-r8-124", fqdn=FQDN, role="nss_3_b_osx_1015",
                       target_role="nss_3_b_osx_1500", pool="releng-hardware/nss-3-b-osx-1015",
                       client_id="project/releng/generic-worker/datacenter-nss-3-b-osx",
                       task_user="task_174593007134490")
    for k, v in kw.items():
        setattr(h, k, v)
    return h


class FakeSSH:
    """First table key that is a substring of the command wins. Records every call."""

    def __init__(self, table: dict[str, object]):
        self.table = table
        self.calls: list[tuple[str, str | None, bytes | None]] = []

    def run(self, _host, command, *, stdin=None, check=True, timeout=None, user=None):
        self.calls.append((command, user, stdin))
        for key, val in self.table.items():
            if key in command:
                val = val() if callable(val) else val
                return val if isinstance(val, subprocess.CompletedProcess) else _cp(val)
        return _cp("")

    def commands(self) -> list[str]:
        return [c for c, _, _ in self.calls]


PREFLIGHT_OK = {
    "sw_vers -productVersion": "10.15.7",
    "hw.model": "Macmini8,1",
    "SPiBridgeDataType": "21P3049",
    "sudo -n true": _cp(""),
    "cat /etc/puppet_role": "nss_3_b_osx_1015",
    "grep -c hiera_vault": "0",
    "PUPPET_BRANCH": "",
    "worker-runner-config.yaml": 'workerPoolID: "releng-hardware/nss-3-b-osx-1015"\n  clientID: project/releng/generic-worker/datacenter-nss-3-b-osx',
    "current-task-user.json": "task_174593007134490",
    "autoLoginUser": "task_174593007134490",
    "df -g": "144",
    "du -sg": "1",
    "AutomaticallyInstallMacOSUpdates": "0",
}


def _preflight(overrides: dict | None = None):
    fake = FakeSSH({**(overrides or {}), **{k: v for k, v in PREFLIGHT_OK.items() if k not in (overrides or {})}})
    h = bu.resolve("macmini-r8-124")
    with patch("orchestrator.builder_upgrade.ssh.run", fake.run), \
         patch("orchestrator.builder_upgrade.taskcluster.get_worker", return_value={}):
        ver = bu.step_preflight(h)
    return h, ver, fake


# --- pure helpers / resolve ---


def test_t2_major_parses_and_fails_closed():
    assert bu.t2_major("20P5058") == 20
    assert bu.t2_major("24P6005") == 24
    assert bu.t2_major("17P6610") == 17
    assert bu.t2_major("") == 0
    assert bu.t2_major("garbage") == 0


def test_resolve_refuses_non_r8():
    with pytest.raises(NotReadyError, match="not an Intel r8 builder"):
        bu.resolve("macmini-m4-81")


def test_every_1015_role_maps_to_its_1500_twin():
    for old, new in bu.ROLE_UPGRADES.items():
        assert old.endswith("_1015") and new == old.replace("_1015", "_1500")


# --- preflight ---


def test_preflight_fills_in_host_and_logs_in_as_relops():
    h, ver, fake = _preflight()
    assert ver == "10.15.7"
    assert (h.role, h.target_role) == ("nss_3_b_osx_1015", "nss_3_b_osx_1500")
    assert h.pool == "releng-hardware/nss-3-b-osx-1015"
    assert h.client_id.endswith("datacenter-nss-3-b-osx")
    assert h.task_user == "task_174593007134490"
    assert {u for _, u, _ in fake.calls} == {"relops"}


@pytest.mark.parametrize("fw", ["17P6610", "18P4347", ""])
def test_preflight_refuses_t2_below_20p(fw):
    with pytest.raises(NotReadyError, match="DFU revive"):
        _preflight({"SPiBridgeDataType": fw})


def test_preflight_skips_t2_gate_once_on_15():
    # A resumed run: already upgraded, firmware is whatever the upgrade left.
    _, ver, _ = _preflight({"sw_vers -productVersion": "15.8.1", "SPiBridgeDataType": "24P6005",
                            "cat /etc/puppet_role": "nss_3_b_osx_1500"})
    assert ver == "15.8.1"


@pytest.mark.parametrize("ver", ["13.4", "13.6.5"])
def test_preflight_accepts_app_services_on_13(ver):
    _, got, _ = _preflight({"sw_vers -productVersion": ver, "cat /etc/puppet_role": "applicationservices_1_b_osx_1015"})
    assert got == ver


@pytest.mark.parametrize("ver", ["10.14.6", "11.7", "14.7.5"])
def test_preflight_refuses_other_source_versions(ver):
    with pytest.raises(NotReadyError, match="10.15 or 13"):
        _preflight({"sw_vers -productVersion": ver})


def test_preflight_refuses_branch_override():
    with pytest.raises(NotReadyError, match="override"):
        _preflight({"PUPPET_BRANCH": 'PUPPET_BRANCH="mac-builder-disk-leaks"'})


def test_preflight_refuses_hiera_vault():
    with pytest.raises(NotReadyError, match="hiera_vault"):
        _preflight({"grep -c hiera_vault": "3"})


def test_preflight_refuses_unknown_role():
    with pytest.raises(NotReadyError, match="not a 1015 builder role"):
        _preflight({"cat /etc/puppet_role": "gecko_t_osx_1400_r8"})


def test_preflight_refuses_autologin_mismatch():
    with pytest.raises(NotReadyError, match="autoLoginUser"):
        _preflight({"autoLoginUser": "cltbld"})


def test_preflight_refuses_when_disk_too_small_even_after_caches():
    with pytest.raises(NotReadyError, match="free"):
        _preflight({"df -g": "30", "du -sg": "10"})


def test_preflight_counts_caches_toward_free_space():
    _preflight({"df -g": "20", "du -sg": "49"})  # r8-241: 49G of cache made the difference


# --- stop worker ---


def test_stop_worker_sets_kcpassword_aside_before_logging_out():
    console = iter(["task_174593007134490", "root", "root"])
    fake = FakeSSH({"stat -f %Su /dev/console": lambda: next(console)})
    with patch("orchestrator.builder_upgrade.ssh.run", fake.run):
        bu.step_stop_worker(_host())
    cmds = fake.commands()
    aside = next(i for i, c in enumerate(cmds) if bu.KCPASSWORD_ASIDE in c)
    logout = next(i for i, c in enumerate(cmds) if "bootout gui/" in c)
    assert aside < logout, "autologin re-fires after a logout unless kcpassword is moved first"
    assert any(bu.SEMAPHORE in c and "rm -f" in c for c in cmds)


def test_stop_worker_fails_if_task_user_comes_straight_back():
    fake = FakeSSH({"stat -f %Su /dev/console": "task_174593007134490"})
    with patch("orchestrator.builder_upgrade.ssh.run", fake.run), patch("orchestrator.builder_upgrade.time.sleep"):
        with pytest.raises(ReprovisionError, match="still logged in"):
            bu.step_stop_worker(_host(), logout_wait=0)


# --- os update ---


def test_os_update_skips_when_already_on_15():
    fake = FakeSSH({"sw_vers -productVersion": "15.8.1"})
    with patch("orchestrator.builder_upgrade.ssh.run", fake.run):
        assert bu.step_os_update(_host()) == "15.8.1"
    assert not any("startosinstall" in c for c in fake.commands())


def test_os_update_fails_if_host_comes_back_on_10_15():
    reach = iter([False, True])  # rebooted (unreachable), then back
    fake = FakeSSH({"sw_vers -productVersion": "10.15.7"})
    with patch("orchestrator.builder_upgrade.ssh.run", fake.run), \
         patch("orchestrator.builder_upgrade.ssh.file_exists", return_value=True), \
         patch("orchestrator.builder_upgrade._reachable", lambda h: next(reach)), \
         patch("orchestrator.builder_upgrade.time.sleep"):
        with pytest.raises(ReprovisionError, match="BridgeRestore"):
            bu.step_os_update(_host())


def test_os_update_launches_installer_with_root_redirect():
    vers = iter(["10.15.7", "15.8.1", "15.8.1"])
    reach = iter([False, True])
    fake = FakeSSH({"sw_vers -productVersion": lambda: next(vers), "SPiBridgeDataType": "24P6005"})
    with patch("orchestrator.builder_upgrade.ssh.run", fake.run), \
         patch("orchestrator.builder_upgrade.ssh.file_exists", return_value=True), \
         patch("orchestrator.builder_upgrade.ssh.wait_for_sshd"), \
         patch("orchestrator.builder_upgrade._reachable", lambda h: next(reach)), \
         patch("orchestrator.builder_upgrade.time.sleep"):
        assert bu.step_os_update(_host()) == "15.8.1"
    launch = next(c for c in fake.commands() if "startosinstall" in c)
    # `sudo nohup ... > /var/log/x` opens the log as relops and never starts the installer.
    assert launch.startswith("sudo sh -c ") and "/var/log/startosinstall.log" in launch


# --- sudo ---


def test_sudo_password_goes_over_stdin_not_argv():
    state = {"n": 0}

    def sudo_n():
        state["n"] += 1
        return _cp("", rc=1 if state["n"] == 1 else 0)

    fake = FakeSSH({"sudo -n true": sudo_n})
    with patch("orchestrator.builder_upgrade.ssh.run", fake.run), \
         patch("orchestrator.builder_upgrade.builder_sudo_password", return_value="s3cret-pw"):
        bu.step_ensure_sudo(_host())
    cmd, _, stdin = next(c for c in fake.calls if "sudo -S" in c[0])
    assert "s3cret-pw" not in cmd
    assert stdin == b"s3cret-pw\n"
    assert "/etc/sudoers.d/" in cmd  # puppet purges sudoers.d, so it cleans up after itself


def test_sudo_without_password_tells_operator_what_to_do():
    fake = FakeSSH({"sudo -n true": _cp("", rc=1)})
    with patch("orchestrator.builder_upgrade.ssh.run", fake.run), \
         patch("orchestrator.builder_upgrade.builder_sudo_password", return_value=""), \
         patch("orchestrator.builder_upgrade.time.sleep"):
        with pytest.raises(NotReadyError, match="SimpleMDM group"):
            bu.step_ensure_sudo(_host(), wait=0)


# --- puppet ---


def test_puppet_fails_on_empty_catalog():
    # r8-270: a stale certname compiled nothing and run-puppet.sh still said "succeeded".
    fake = FakeSSH({"cat /tmp/builder-upgrade-puppet": "Notice: Compiled catalog for macmini-r8-124.local in 0.04 seconds\nPuppet apply succeeded!"})
    with patch("orchestrator.builder_upgrade.ssh.run", fake.run):
        with pytest.raises(ReprovisionError, match="never compiled a catalog"):
            bu.step_puppet(_host())


def test_puppet_switches_role_before_running():
    log = f"Notice: Compiled catalog for {FQDN} in 1.5 seconds\nNotice: /Stage[main]/Packages::Mercurial/Exec[install_mercurial_py311]/returns: executed successfully\n"
    fake = FakeSSH({"cat /tmp/builder-upgrade-puppet": log,
                    "last_run_metadata.json": '{"success": true, "role": "nss_3_b_osx_1500"}'})
    with patch("orchestrator.builder_upgrade.ssh.run", fake.run):
        bu.step_puppet(_host())
    cmds = fake.commands()
    role = next(i for i, c in enumerate(cmds) if "/etc/puppet_role" in c)
    first_run = next(i for i, c in enumerate(cmds) if "run-puppet.sh" in c)
    assert role < first_run and "nss_3_b_osx_1500" in cmds[role]


# --- return to service ---


def test_return_restores_kcpassword_before_semaphore_and_reboot():
    boots = iter([100, 200, 200])
    fake = FakeSSH({"kern.boottime": lambda: str(next(boots)), "stat -f %Su /dev/console": "task_174593007134490"})
    with patch("orchestrator.builder_upgrade.ssh.run", fake.run), \
         patch("orchestrator.builder_upgrade.ssh.file_exists", return_value=True), \
         patch("orchestrator.builder_upgrade.taskcluster.get_worker", return_value={"lastDateActive": "2099-01-01T00:00:00"}), \
         patch("orchestrator.builder_upgrade.time.sleep"):
        bu.step_return(_host())
    cmds = fake.commands()
    restore = next(i for i, c in enumerate(cmds) if f"mv {bu.KCPASSWORD_ASIDE} {bu.KCPASSWORD}" in c)
    semaphore = next(i for i, c in enumerate(cmds) if "touch " + bu.SEMAPHORE in c)
    reboot = next(i for i, c in enumerate(cmds) if "shutdown -r" in c)
    assert restore < semaphore < reboot


def test_return_refuses_to_reboot_without_kcpassword():
    fake = FakeSSH({})
    with patch("orchestrator.builder_upgrade.ssh.run", fake.run), \
         patch("orchestrator.builder_upgrade.ssh.file_exists", return_value=False):
        with pytest.raises(ReprovisionError, match="exit 69"):
            bu.step_return(_host())
    assert not any("shutdown" in c for c in fake.commands())


# --- the whole thing ---


def _patch_steps(fail_at: str | None = None):
    names = ["step_drain", "step_stop_worker", "step_clear_caches", "step_os_update", "step_ensure_sudo",
             "step_install_xcode", "step_install_clt", "step_puppet", "step_verify", "step_return"]
    patches = {}
    for n in names:
        side = ReprovisionError(f"boom in {n}") if n == fail_at else None
        patches[n] = patch(f"orchestrator.builder_upgrade.{n}", side_effect=side)
    return patches


def _run_upgrade(fail_at=None, **kw):
    ps = _patch_steps(fail_at)
    mocks = {n: p.start() for n, p in ps.items()}
    try:
        with patch("orchestrator.builder_upgrade.os_version", return_value="10.15.7"), \
             patch("orchestrator.builder_upgrade.step_preflight", return_value="10.15.7"), \
             patch("orchestrator.builder_upgrade.already_upgraded", return_value=False), \
             patch("orchestrator.builder_upgrade.step_quarantine") as q, \
             patch("orchestrator.builder_upgrade.step_unquarantine") as uq:
            try:
                bu.upgrade("macmini-r8-124", **kw)
            except ReprovisionError:
                pass
            return q, uq, mocks
    finally:
        for p in ps.values():
            p.stop()


def test_failure_mid_run_leaves_worker_quarantined():
    q, uq, mocks = _run_upgrade(fail_at="step_install_xcode", unquarantine=True)
    q.assert_called_once()
    uq.assert_not_called()
    mocks["step_puppet"].assert_not_called()


def test_success_stays_quarantined_unless_asked():
    q, uq, _ = _run_upgrade()
    q.assert_called_once()
    uq.assert_not_called()


def test_success_with_unquarantine_puts_it_back():
    _, uq, _ = _run_upgrade(unquarantine=True)
    uq.assert_called_once()


def test_dry_run_changes_nothing():
    q, uq, mocks = _run_upgrade(dry_run=True)
    q.assert_not_called()
    assert not any(m.called for m in mocks.values())


def test_already_upgraded_host_is_left_alone():
    with patch("orchestrator.builder_upgrade.os_version", return_value="15.8.1"), \
         patch("orchestrator.builder_upgrade.has_passwordless_sudo", return_value=True), \
         patch("orchestrator.builder_upgrade.step_preflight", return_value="15.8.1"), \
         patch("orchestrator.builder_upgrade.already_upgraded", return_value=True), \
         patch("orchestrator.builder_upgrade.step_quarantine") as q:
        bu.upgrade("macmini-r8-241")
    q.assert_not_called()


# --- runner ---


def test_runner_maps_builder_upgrade_action():
    from orchestrator import runner

    cmd = runner._job_cmd("macmini-r8-124", {"id": 7, "action": "builder-upgrade", "params": {"unquarantine": True}}, None)
    assert cmd[1:] == ["builder-upgrade", "macmini-r8-124", "--job", "7", "--unquarantine"]
    dry = runner._job_cmd("macmini-r8-124", {"id": 8, "action": "builder-upgrade", "params": {"dry_run": True}}, None)
    assert "--dry-run" in dry and "--unquarantine" not in dry
