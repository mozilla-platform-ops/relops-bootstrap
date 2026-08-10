"""The fresh-host path: `step_preflight` gating and `provision`'s no-wipe guarantee."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from orchestrator import workflow
from orchestrator.errors import NotReadyError, ReprovisionError

HOST = "macmini-m4-201"


def _cp(stdout: bytes = b"", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["ssh"], returncode=returncode, stdout=stdout, stderr=b"")


def _ctx():
    return workflow.resolve_offline(HOST)


class _Host:
    """Canned responses for the handful of remote reads preflight makes."""

    def __init__(self, *, os_version="15.3", sip="System Integrity Protection status: disabled.",
                 token="ENABLED", escrowed=True, sentinel=False):
        self.os_version = os_version
        self.sip = sip
        self.token = token
        self.escrowed = escrowed
        self.sentinel = sentinel

    def run(self, _fqdn, command, **_kw):
        if "sw_vers" in command:
            return _cp(self.os_version.encode())
        if "csrutil" in command:
            return _cp(self.sip.encode())
        if "bootstraptoken" in command:
            body = b"escrowed to server: YES" if self.escrowed else b"escrowed to server: NO"
            return _cp(body)
        return _cp()

    def install(self):
        return (
            patch("orchestrator.workflow.ssh.wait_for_sshd", return_value=None),
            patch("orchestrator.workflow.ssh.run", side_effect=self.run),
            patch("orchestrator.workflow.ssh.secure_token_status", return_value=self.token),
            patch("orchestrator.workflow.ssh.file_exists", return_value=self.sentinel),
        )


def _preflight(host: _Host, **kwargs):
    patches = host.install()
    for p in patches:
        p.start()
    try:
        workflow.step_preflight(_ctx(), **kwargs)
    finally:
        for p in patches:
            p.stop()


# --- resolve_offline ---


def test_resolve_offline_makes_no_api_calls():
    # A 55-host readiness sweep must not need a SimpleMDM key or TC creds.
    with patch("orchestrator.workflow.simplemdm.find_device_by_name") as mdm, \
         patch("orchestrator.workflow.taskcluster.find_registered_pool") as tc:
        ctx = workflow.resolve_offline(HOST)
    mdm.assert_not_called()
    tc.assert_not_called()
    assert ctx.hostname == HOST
    assert ctx.fqdn == f"{HOST}.test.releng.mdc1.mozilla.com"
    assert ctx.role == "gecko_t_osx_1500_m4"
    assert ctx.registered is False
    # Unset on purpose — nothing pool-scoped belongs on the fresh path.
    assert ctx.worker_pool_id == ""


def test_resolve_offline_rejects_a_bad_hostname():
    with pytest.raises(ValueError, match="refusing"):
        workflow.resolve_offline("macmini-m4-201; rm -rf /")


# --- OS version matching ---


@pytest.mark.parametrize(
    "actual,expected,ok",
    [
        ("15.3", "15.3", True),
        ("15.3.1", "15.3", True),   # point release of the target is fine
        ("15.30", "15.3", False),   # must not match on a bare prefix
        ("15.4", "15.3", False),
        ("15.2", "15.3", False),
        ("26.1", "15.3", False),    # shipped-with OS on new hardware
    ],
)
def test_os_version_matching(actual, expected, ok):
    assert workflow._os_version_matches(actual, expected) is ok


# --- the gate ---


def test_preflight_passes_on_a_ready_host():
    _preflight(_Host())  # no raise


def test_preflight_skips_a_host_still_on_the_shipped_os():
    # The failure this exists to prevent: an MDM OS install landing mid-puppet took ~12 of 25
    # hosts offline for 25-45 min on the 2026-05-12 batch.
    with pytest.raises(NotReadyError, match="macOS 26.1, expected 15.3"):
        _preflight(_Host(os_version="26.1"))


def test_preflight_skips_when_sip_is_enabled():
    with pytest.raises(NotReadyError, match="SIP is not disabled"):
        _preflight(_Host(sip="System Integrity Protection status: enabled."))


def test_preflight_allows_sip_on_when_asked():
    _preflight(_Host(sip="System Integrity Protection status: enabled."), require_sip_disabled=False)


def test_preflight_skips_when_sshd_is_not_up():
    # Fresh DEP hosts appear over ~15 min; not-up-yet is "skip and re-run", not a failure.
    with patch("orchestrator.workflow.ssh.wait_for_sshd", side_effect=TimeoutError("nope")):
        with pytest.raises(NotReadyError, match="sshd not reachable"):
            workflow.step_preflight(_ctx())


def test_preflight_skips_when_the_os_version_read_fails():
    host = _Host()
    host.run = lambda _fqdn, command, **_kw: (
        _cp(b"", returncode=255) if "sw_vers" in command else _cp()
    )
    with pytest.raises(NotReadyError, match="couldn't read the OS version"):
        _preflight(host)


def test_not_ready_is_a_reprovision_error():
    # cli.app() relies on the subclass relationship (and on catching NotReadyError first).
    assert issubclass(NotReadyError, ReprovisionError)


# --- provision() ---


def test_provision_runs_the_steps_in_order_and_never_wipes():
    calls: list[str] = []
    with patch("orchestrator.workflow.step_preflight", side_effect=lambda *a, **k: calls.append("preflight")), \
         patch("orchestrator.workflow.step_mint", side_effect=lambda *a, **k: calls.append("mint")), \
         patch("orchestrator.workflow.step_escrow_bst", side_effect=lambda *a, **k: calls.append("escrow")), \
         patch("orchestrator.workflow.step_wait_for_bootstrap_pkg"), \
         patch("orchestrator.workflow.step_wait_for_sentinel", side_effect=lambda *a, **k: calls.append("sentinel")), \
         patch("orchestrator.workflow.step_wipe", side_effect=AssertionError("provision must never wipe")), \
         patch("orchestrator.workflow.step_quarantine", side_effect=AssertionError("no pool calls")):
        workflow.provision(HOST)

    # mint before escrow is load-bearing: the BST escrow needs an existing SecureToken holder.
    # (the pkg gate's own position is pinned by test_provision_gates_on_the_pkg_before_the_sentinel_wait)
    assert calls == ["preflight", "mint", "escrow", "sentinel"]


def test_provision_no_wait_stops_after_escrow():
    with patch("orchestrator.workflow.step_preflight"), \
         patch("orchestrator.workflow.step_mint"), \
         patch("orchestrator.workflow.step_escrow_bst"), \
         patch("orchestrator.workflow.step_wait_for_sentinel") as sentinel:
        workflow.provision(HOST, wait=False)
    sentinel.assert_not_called()


def test_provision_stops_at_the_gate():
    # A host that fails preflight must not get mint/escrow attempted anyway.
    with patch("orchestrator.workflow.step_preflight", side_effect=NotReadyError("macOS 26.1")), \
         patch("orchestrator.workflow.step_mint") as mint, \
         patch("orchestrator.workflow.step_escrow_bst") as escrow:
        with pytest.raises(NotReadyError):
            workflow.provision(HOST)
    mint.assert_not_called()
    escrow.assert_not_called()


POOL = "releng-hardware/gecko-t-osx-1500-m4"


# --- bootstrap pkg gate ---


def _pkg_settings(settings, *, max_wait=60):
    settings.return_value.bootstrap_pkg_poll_seconds = 0
    settings.return_value.bootstrap_pkg_max_wait_seconds = max_wait


def test_pkg_gate_passes_once_the_managed_install_lands():
    # A group move that just happened leaves an MDM check-in pending, so the first polls
    # legitimately find nothing.
    seen = [False, False, False, True]  # sentinel absent, then pkg absent twice, then present
    with patch("orchestrator.workflow.ssh.file_exists", side_effect=seen), \
         patch("orchestrator.workflow.get_settings") as settings:
        _pkg_settings(settings)
        workflow.step_wait_for_bootstrap_pkg(_ctx())  # no raise


def test_pkg_gate_skips_a_host_in_the_wrong_group():
    # Without this the sentinel wait burns the full hour before failing with a message that
    # points at the bootstrap instead of the group assignment.
    with patch("orchestrator.workflow.ssh.file_exists", return_value=False), \
         patch("orchestrator.workflow.get_settings") as settings:
        _pkg_settings(settings, max_wait=0)
        with pytest.raises(NotReadyError, match="is this host in the bootstrap group"):
            workflow.step_wait_for_bootstrap_pkg(_ctx())


def test_pkg_gate_is_a_noop_on_an_already_bootstrapped_host():
    # Sentinel present => the pkg obviously ran; don't re-poll for a payload that may predate
    # the check.
    with patch("orchestrator.workflow.ssh.file_exists", return_value=True) as exists, \
         patch("orchestrator.workflow.get_settings") as settings:
        _pkg_settings(settings)
        workflow.step_wait_for_bootstrap_pkg(_ctx())
    # One call: the sentinel probe. It must not go on to poll for the payload.
    assert exists.call_count == 1
    assert exists.call_args.args[1] == workflow.SENTINEL


def test_preflight_reports_the_pkg_but_does_not_gate_on_it():
    # The readiness sweep runs BEFORE hosts are moved into the bootstrap group, so gating here
    # would fail every host in the sweep.
    host = _Host(sentinel=False)
    patches = host.install()
    for p in patches:
        p.start()
    try:
        workflow.step_preflight(_ctx())  # pkg absent (file_exists -> False), must not raise
    finally:
        for p in patches:
            p.stop()


def test_provision_gates_on_the_pkg_before_the_sentinel_wait():
    calls: list[str] = []
    with patch("orchestrator.workflow.step_preflight"), \
         patch("orchestrator.workflow.step_mint"), \
         patch("orchestrator.workflow.step_escrow_bst"), \
         patch(
             "orchestrator.workflow.step_wait_for_bootstrap_pkg",
             side_effect=lambda *a: calls.append("pkg"),
         ), \
         patch("orchestrator.workflow.step_wait_for_sentinel", side_effect=lambda *a: calls.append("sentinel")):
        workflow.provision(HOST)
    assert calls == ["pkg", "sentinel"]


def test_provision_no_wait_skips_the_pkg_gate_too():
    # Nothing is going to wait on the sentinel, so there's nothing to protect.
    with patch("orchestrator.workflow.step_preflight"), \
         patch("orchestrator.workflow.step_mint"), \
         patch("orchestrator.workflow.step_escrow_bst"), \
         patch("orchestrator.workflow.step_wait_for_bootstrap_pkg") as pkg:
        workflow.provision(HOST, wait=False)
    pkg.assert_not_called()


# --- quarantine on register ---


def test_candidate_pools_probes_staging_before_prod():
    # The role backs both; staging must be tried first or a staging worker gets quarantined in
    # the wrong pool (the 404 that PR #35 fixed).
    assert workflow.candidate_pools("gecko_t_osx_1500_m4") == [f"{POOL}-staging", POOL]


def test_candidate_pools_rejects_an_unmapped_role():
    with pytest.raises(ValueError, match="no worker pool mapping"):
        workflow.candidate_pools("gecko_t_linux_2404_talos")


def test_quarantine_on_register_waits_then_quarantines_in_the_discovered_pool():
    # Registration trails the sentinel, so the first few polls legitimately find nothing.
    seen = [None, None, POOL]
    with patch("orchestrator.workflow.tc_credentials", return_value=("cid", "tok")), \
         patch("orchestrator.workflow.taskcluster.find_registered_pool", side_effect=seen), \
         patch("orchestrator.workflow.get_settings") as settings, \
         patch("orchestrator.workflow.step_quarantine") as quarantine:
        settings.return_value.quarantine_on_register_poll_seconds = 0
        settings.return_value.quarantine_on_register_max_wait_seconds = 60
        ctx = _ctx()
        workflow.step_quarantine_on_register(ctx)

    quarantine.assert_called_once()
    # The pool must come from where the worker actually showed up, not from the role.
    assert quarantine.call_args.args[0].worker_pool_id == POOL
    assert "fresh host" in quarantine.call_args.kwargs["info"]


def test_quarantine_on_register_fails_closed_without_tc_credentials():
    # Refuse up front rather than spend the bootstrap window discovering we can't quarantine.
    with patch("orchestrator.workflow.tc_credentials", return_value=("", "")), \
         patch("orchestrator.workflow.taskcluster.find_registered_pool") as find, \
         patch("orchestrator.workflow.step_quarantine") as quarantine:
        with pytest.raises(ReprovisionError, match="needs Taskcluster credentials"):
            workflow.step_quarantine_on_register(_ctx())
    find.assert_not_called()
    quarantine.assert_not_called()


def test_quarantine_on_register_raises_if_the_worker_never_appears():
    with patch("orchestrator.workflow.tc_credentials", return_value=("cid", "tok")), \
         patch("orchestrator.workflow.taskcluster.find_registered_pool", return_value=None), \
         patch("orchestrator.workflow.get_settings") as settings, \
         patch("orchestrator.workflow.step_quarantine") as quarantine:
        settings.return_value.quarantine_on_register_poll_seconds = 0
        settings.return_value.quarantine_on_register_max_wait_seconds = 0
        with pytest.raises(ReprovisionError, match="never registered"):
            workflow.step_quarantine_on_register(_ctx())
    quarantine.assert_not_called()


def test_provision_quarantines_after_the_sentinel():
    calls: list[str] = []
    with patch("orchestrator.workflow.tc_credentials", return_value=("cid", "tok")), \
         patch("orchestrator.workflow.step_preflight"), \
         patch("orchestrator.workflow.step_mint"), \
         patch("orchestrator.workflow.step_escrow_bst"), \
         patch("orchestrator.workflow.step_wait_for_bootstrap_pkg"), \
         patch("orchestrator.workflow.step_wait_for_sentinel", side_effect=lambda *a: calls.append("sentinel")), \
         patch(
             "orchestrator.workflow.step_quarantine_on_register",
             side_effect=lambda *a: calls.append("quarantine"),
         ):
        workflow.provision(HOST, quarantine_on_register=True)

    # Order matters: the worker only registers after the bootstrap finishes.
    assert calls == ["sentinel", "quarantine"]


def test_provision_rejects_quarantine_on_register_with_no_wait():
    # Nothing to catch — the registration happens after we'd have returned.
    with patch("orchestrator.workflow.step_preflight") as pre:
        with pytest.raises(ReprovisionError, match="needs the bootstrap wait"):
            workflow.provision(HOST, wait=False, quarantine_on_register=True)
    pre.assert_not_called()  # refused before touching the host


def test_provision_checks_tc_credentials_before_touching_the_host():
    # The flag's whole value is that the host doesn't take work; finding out we can't
    # quarantine only after a 40-minute bootstrap defeats it.
    with patch("orchestrator.workflow.tc_credentials", return_value=("", "")), \
         patch("orchestrator.workflow.step_preflight") as pre, \
         patch("orchestrator.workflow.step_mint") as mint:
        with pytest.raises(ReprovisionError, match="fails now rather than after"):
            workflow.provision(HOST, quarantine_on_register=True)
    pre.assert_not_called()
    mint.assert_not_called()


def test_provision_does_not_quarantine_by_default():
    with patch("orchestrator.workflow.step_preflight"), \
         patch("orchestrator.workflow.step_mint"), \
         patch("orchestrator.workflow.step_escrow_bst"), \
         patch("orchestrator.workflow.step_wait_for_bootstrap_pkg"), \
         patch("orchestrator.workflow.step_wait_for_sentinel"), \
         patch("orchestrator.workflow.step_quarantine_on_register") as quarantine, \
         patch("orchestrator.workflow.tc_credentials") as creds:
        workflow.provision(HOST)
    quarantine.assert_not_called()
    creds.assert_not_called()  # and no credential fetch when the flag is off


def test_provision_passes_the_gate_options_through():
    with patch("orchestrator.workflow.step_preflight") as pre, \
         patch("orchestrator.workflow.step_mint"), \
         patch("orchestrator.workflow.step_escrow_bst"), \
         patch("orchestrator.workflow.step_wait_for_bootstrap_pkg"), \
         patch("orchestrator.workflow.step_wait_for_sentinel"):
        workflow.provision(HOST, expected_os="15.6", require_sip_disabled=False)
    assert pre.call_args.kwargs == {"expected_os": "15.6", "require_sip_disabled": False}


# --- os-update ---


def test_os_upgrade_script_substitutes_the_credential_and_target():
    import shlex

    pw = "s3cr3t pw'with quotes"  # the shape that breaks naive substitution
    with patch("orchestrator.workflow.ssh_admin_password", return_value=pw):
        body = workflow._os_upgrade_script("15.4")

    # The placeholder that shipped in SimpleMDM script 8903 must be gone...
    assert 'ADMIN_PASSWORD="INSERT_HERE"' not in body
    # ...replaced with a *shell-quoted* value, so quotes/spaces can't break the script or
    # inject. The raw string deliberately does NOT appear — that's the quoting working.
    assert pw not in body
    assert f"ADMIN_PASSWORD={shlex.quote(pw)}" in body
    assert f"TARGET_VERSION={shlex.quote('15.4')}" in body

    # And the quoted form must actually parse back to the original under real shell rules.
    line = next(ln for ln in body.splitlines() if ln.startswith("ADMIN_PASSWORD="))
    assert shlex.split(line)[0] == f"ADMIN_PASSWORD={pw}"


def test_os_upgrade_script_keeps_the_placeholder_guard():
    # Belt and braces: even with substitution, the guard must survive into the shipped body so
    # a hand-pasted SimpleMDM copy still refuses to burn 14GB on a placeholder.
    with patch("orchestrator.workflow.ssh_admin_password", return_value="pw"):
        body = workflow._os_upgrade_script("")
    assert "refusing to run" in body


def test_os_update_is_a_noop_when_already_at_target():
    with patch("orchestrator.workflow.ssh.wait_for_sshd"), \
         patch("orchestrator.workflow.ssh.run", return_value=_cp(b"15.3")) as run, \
         patch("orchestrator.workflow.ssh.write_file_as_root") as write:
        workflow.step_os_update(_ctx(), expected_os="15.3")
    write.assert_not_called()          # nothing staged
    assert run.call_count == 1         # just the version probe


def test_os_update_stages_and_launches():
    calls = []

    def _run(_fqdn, command, **_kw):
        calls.append(command)
        if "sw_vers" in command:
            return _cp(b"15.1")
        if "tail" in command:
            return _cp(b"[INFO] downloading http://releng-pxe1...")
        return _cp(b"launched")

    with patch("orchestrator.workflow.ssh.wait_for_sshd"), \
         patch("orchestrator.workflow.ssh.run", side_effect=_run), \
         patch("orchestrator.workflow.ssh.write_file_as_root") as write, \
         patch("orchestrator.workflow.ssh_admin_password", return_value="pw"), \
         patch("orchestrator.workflow.time.sleep"):
        workflow.step_os_update(_ctx(), expected_os="15.3")

    # Staged root-only, at a root-only path.
    assert write.call_args.args[1] == workflow.OS_UPGRADE_REMOTE
    assert write.call_args.kwargs["mode"] == "0700"
    # Launched detached — the ~14GB download outlives any ssh timeout and ends in a reboot.
    assert any("nohup" in c for c in calls)


def test_os_update_surfaces_a_refused_start_as_not_ready():
    # The script's own guards (placeholder credential, no SecureToken, low disk) fail within
    # seconds. Catching that here is the difference between "skipped, go fix it" and a host
    # that silently never upgrades.
    def _run(_fqdn, command, **_kw):
        if "sw_vers" in command:
            return _cp(b"15.1")
        if "tail" in command:
            return _cp(b"[ERROR] admin holds no SecureToken, so it is not a volume owner")
        return _cp(b"launched")

    with patch("orchestrator.workflow.ssh.wait_for_sshd"), \
         patch("orchestrator.workflow.ssh.run", side_effect=_run), \
         patch("orchestrator.workflow.ssh.write_file_as_root"), \
         patch("orchestrator.workflow.ssh_admin_password", return_value="pw"), \
         patch("orchestrator.workflow.time.sleep"):
        with pytest.raises(NotReadyError, match="SecureToken"):
            workflow.step_os_update(_ctx(), expected_os="15.3")


def test_os_upgrade_script_validates_the_credential_before_downloading():
    # The fleet is split: hosts enrolled before the DEP static-password change still have the
    # old default. A SecureToken can be ENABLED under either one, so the token check alone
    # can't catch a wrong password -- and the cost of finding out at startosinstall is ~14GB
    # and a reboot per host.
    with patch("orchestrator.workflow.ssh_admin_password", return_value="pw"):
        body = workflow._os_upgrade_script("15.3")
    assert "dscl . -authonly" in body
    # ...and it must come before the download, or it saves nothing.
    assert body.index("dscl . -authonly") < body.index("downloading $REMOTE_URL")
