"""`reprovision batch` — host-file parsing, exit-code classification, concurrency bound."""

from __future__ import annotations

import subprocess
import threading
from unittest.mock import patch

import pytest

from orchestrator import batch
from orchestrator.errors import ReprovisionError


@pytest.fixture(autouse=True)
def _no_prewarm(request):
    """run_batch resolves secrets once up front; unit tests must not call 1Password to do it.

    Tests of _prewarm_secret_env itself opt out with @pytest.mark.real_prewarm — otherwise this
    fixture shadows the function under test and they assert against the mock (which is exactly
    how two of them first passed vacuously).
    """
    if request.node.get_closest_marker("real_prewarm"):
        yield
        return
    with patch("orchestrator.batch._prewarm_secret_env", return_value={}):
        yield


# --- host file parsing ---


def test_read_host_file_skips_comments_and_blanks(tmp_path):
    f = tmp_path / "hosts.txt"
    f.write_text(
        "# batch 1 — rack 4\n"
        "macmini-m4-201\n"
        "\n"
        "  macmini-m4-202  \n"
        "macmini-m4-203  # was DOA, reseated\n"
    )
    assert batch.read_host_file(str(f)) == ["macmini-m4-201", "macmini-m4-202", "macmini-m4-203"]


def test_read_host_file_aborts_on_a_bad_hostname(tmp_path):
    # Fail-fast: a typo in a 55-line file is a file to fix, not 54 hosts to provision while
    # one line silently does nothing.
    f = tmp_path / "hosts.txt"
    f.write_text("macmini-m4-201\nmacmini-m4-2o2\nmacmini-m4-203\n")
    with pytest.raises(ReprovisionError, match="line 2"):
        batch.read_host_file(str(f))


def test_read_host_file_rejects_shell_metacharacters(tmp_path):
    f = tmp_path / "hosts.txt"
    f.write_text("macmini-m4-201; touch /tmp/pwned\n")
    with pytest.raises(ReprovisionError, match="unusable hostnames"):
        batch.read_host_file(str(f))


def test_read_host_file_dedupes(tmp_path):
    f = tmp_path / "hosts.txt"
    f.write_text("macmini-m4-201\nmacmini-m4-202\nmacmini-m4-201\n")
    assert batch.read_host_file(str(f)) == ["macmini-m4-201", "macmini-m4-202"]


def test_read_host_file_rejects_an_empty_list(tmp_path):
    f = tmp_path / "hosts.txt"
    f.write_text("# nothing here\n")
    with pytest.raises(ReprovisionError, match="no hostnames"):
        batch.read_host_file(str(f))


def test_read_host_file_missing_file():
    with pytest.raises(ReprovisionError, match="can't read host file"):
        batch.read_host_file("/nonexistent/hosts.txt")


# --- child command construction ---


def test_child_cmd_provision_includes_the_gate_flags():
    cmd = batch._child_cmd(
        "macmini-m4-201", "provision", expected_os="15.3", allow_sip_enabled=True, wait=False
    )
    assert cmd[1:] == ["provision", "macmini-m4-201", "--no-wait", "--expected-os", "15.3", "--allow-sip-enabled"]


def test_child_cmd_passes_quarantine_on_register_through():
    cmd = batch._child_cmd(
        "macmini-m4-201",
        "provision",
        expected_os="",
        allow_sip_enabled=False,
        wait=True,
        quarantine_on_register=True,
    )
    assert "--quarantine-on-register" in cmd


def test_child_cmd_mint_takes_no_gate_flags():
    # mint runs BEFORE the Recovery trip and the OS update, so gating it on either would
    # deadlock the intended order: Recovery needs a volume owner, which is what mint creates.
    cmd = batch._child_cmd("macmini-m4-201", "mint", expected_os="15.3", allow_sip_enabled=False, wait=True)
    assert cmd[1:] == ["mint", "macmini-m4-201"]


def test_child_cmd_preflight_is_read_only():
    cmd = batch._child_cmd("macmini-m4-201", "preflight", expected_os="", allow_sip_enabled=False, wait=True)
    assert cmd[1] == "preflight"


def test_run_batch_rejects_an_unknown_action():
    with pytest.raises(ReprovisionError, match="unknown batch action"):
        batch.run_batch(["macmini-m4-201"], action="wipe")


# --- classification ---


def _fake_subprocess(codes: dict[str, int], output: dict[str, str] | None = None):
    """Stand in for subprocess.run: exit code (and log text) keyed by hostname in argv."""
    output = output or {}

    def _run(cmd, stdout=None, stderr=None, timeout=None, check=False, env=None):
        host = next(c for c in cmd if c.startswith("macmini-"))
        if stdout is not None and host in output:
            stdout.write(output[host].encode())
            stdout.flush()
        return subprocess.CompletedProcess(args=cmd, returncode=codes[host], stdout=None, stderr=None)

    return _run


def test_batch_classifies_ok_skipped_and_failed(tmp_path):
    hosts = ["macmini-m4-201", "macmini-m4-202", "macmini-m4-203"]
    codes = {"macmini-m4-201": 0, "macmini-m4-202": 2, "macmini-m4-203": 1}
    output = {
        "macmini-m4-202": "▲ macmini-m4-202: macOS 26.1, expected 15.3\n",
        "macmini-m4-203": "✗ password login denied (wrong admin password?)\n",
    }
    rows: list = []
    with patch("orchestrator.batch.subprocess.run", side_effect=_fake_subprocess(codes, output)), \
         patch("orchestrator.batch._log_dir", return_value=tmp_path), \
         patch("orchestrator.batch.ui.batch_summary", side_effect=lambda r, **kw: rows.extend(r)):
        failed = batch.run_batch(hosts, action="provision", concurrency=3)

    # exit 2 is "come back later", so it must NOT count toward the failure exit code.
    assert failed == 1
    assert [(h, state) for h, state, _detail, _t in rows] == [
        ("macmini-m4-201", "ok"),
        ("macmini-m4-202", "skipped"),
        ("macmini-m4-203", "failed"),
    ]
    # The reason survives into the summary table so the operator doesn't have to open 55 logs.
    assert "macOS 26.1" in dict((h, d) for h, _s, d, _t in rows)["macmini-m4-202"]


def test_batch_reports_in_the_operator_supplied_order(tmp_path):
    # Completion order is nondeterministic; the report must be diffable against the host file.
    hosts = [f"macmini-m4-2{n:02d}" for n in range(1, 8)]
    codes = dict.fromkeys(hosts, 0)
    rows: list = []
    with patch("orchestrator.batch.subprocess.run", side_effect=_fake_subprocess(codes)), \
         patch("orchestrator.batch._log_dir", return_value=tmp_path), \
         patch("orchestrator.batch.ui.batch_summary", side_effect=lambda r, **kw: rows.extend(r)):
        batch.run_batch(hosts, action="preflight", concurrency=4)
    assert [h for h, _s, _d, _t in rows] == hosts


def test_batch_writes_one_log_per_host(tmp_path):
    hosts = ["macmini-m4-201", "macmini-m4-202"]
    with patch("orchestrator.batch.subprocess.run", side_effect=_fake_subprocess(dict.fromkeys(hosts, 0))), \
         patch("orchestrator.batch._log_dir", return_value=tmp_path), \
         patch("orchestrator.batch.ui.batch_summary"):
        batch.run_batch(hosts, action="preflight")
    for host in hosts:
        log = tmp_path / f"{host}.log"
        assert log.exists()
        assert host in log.read_text()  # the invocation is recorded at the top


def test_batch_honours_the_concurrency_bound(tmp_path):
    # The ceiling is MDC1 network/imaging throughput; exceeding it is what took hosts offline
    # in bulk on previous rollouts.
    hosts = [f"macmini-m4-2{n:02d}" for n in range(1, 13)]
    lock = threading.Lock()
    in_flight = 0
    peak = 0

    def _run(cmd, stdout=None, stderr=None, timeout=None, check=False, env=None):
        nonlocal in_flight, peak
        with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        try:
            threading.Event().wait(0.02)
        finally:
            with lock:
                in_flight -= 1
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=None, stderr=None)

    with patch("orchestrator.batch.subprocess.run", side_effect=_run), \
         patch("orchestrator.batch._log_dir", return_value=tmp_path), \
         patch("orchestrator.batch.ui.batch_summary"):
        batch.run_batch(hosts, action="preflight", concurrency=3)

    assert peak <= 3


def test_batch_survives_one_host_timing_out(tmp_path):
    hosts = ["macmini-m4-201", "macmini-m4-202"]

    def _run(cmd, stdout=None, stderr=None, timeout=None, check=False, env=None):
        host = next(c for c in cmd if c.startswith("macmini-"))
        if host == "macmini-m4-201":
            raise subprocess.TimeoutExpired(cmd, timeout or 1)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=None, stderr=None)

    rows: list = []
    with patch("orchestrator.batch.subprocess.run", side_effect=_run), \
         patch("orchestrator.batch._log_dir", return_value=tmp_path), \
         patch("orchestrator.batch.ui.batch_summary", side_effect=lambda r, **kw: rows.extend(r)):
        failed = batch.run_batch(hosts, action="provision", concurrency=2, per_host_timeout=1)

    assert failed == 1
    by_host = {h: (state, detail) for h, state, detail, _t in rows}
    assert by_host["macmini-m4-201"][0] == "failed"
    assert "timed out" in by_host["macmini-m4-201"][1]
    assert by_host["macmini-m4-202"][0] == "ok"  # the healthy host still completed


def test_batch_extends_the_timeout_for_the_registration_watch(tmp_path):
    # The child now has more to do; a timeout sized for bootstrap alone would kill it mid-watch.
    seen: dict[str, int] = {}

    def _run(cmd, stdout=None, stderr=None, timeout=None, check=False, env=None):
        seen["timeout"] = timeout
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=None, stderr=None)

    with patch("orchestrator.batch.subprocess.run", side_effect=_run), \
         patch("orchestrator.batch._log_dir", return_value=tmp_path), \
         patch("orchestrator.batch.ui.batch_summary"):
        batch.run_batch(["macmini-m4-201"], action="provision", quarantine_on_register=True)
    with_watch = seen["timeout"]

    with patch("orchestrator.batch.subprocess.run", side_effect=_run), \
         patch("orchestrator.batch._log_dir", return_value=tmp_path), \
         patch("orchestrator.batch.ui.batch_summary"):
        batch.run_batch(["macmini-m4-201"], action="provision", quarantine_on_register=False)

    assert with_watch > seen["timeout"]


def test_batch_dry_run_executes_nothing(tmp_path):
    with patch("orchestrator.batch.subprocess.run") as run, \
         patch("orchestrator.batch._log_dir", return_value=tmp_path) as log_dir:
        failed = batch.run_batch(["macmini-m4-201"], action="provision", dry_run=True)
    assert failed == 0
    run.assert_not_called()
    log_dir.assert_not_called()  # no log directory created for a dry run


# --- the banner must describe the gates the action actually enforces ---


def _banner(action: str, *, allow_sip_enabled: bool = False, tmp_path=None) -> str:
    """Capture the single ui.info() gate line run_batch prints before doing any work."""
    lines: list[str] = []
    with patch("orchestrator.batch.subprocess.run",
               return_value=subprocess.CompletedProcess(args=[], returncode=0)), \
         patch("orchestrator.batch._log_dir", return_value=tmp_path), \
         patch("orchestrator.batch.ui.batch_summary"), \
         patch("orchestrator.batch.ui.step"), \
         patch("orchestrator.batch.ui.info", side_effect=lambda m: lines.append(m)):
        batch.run_batch(["macmini-m4-201"], action=action, allow_sip_enabled=allow_sip_enabled)
    return "\n".join(lines)


@pytest.mark.parametrize("action", ["mint", "os-update"])
def test_banner_does_not_claim_a_sip_gate_for_ungated_actions(action, tmp_path):
    """mint and os-update are handed no --allow-sip-enabled, so they check no SIP state.

    Printing "SIP must be disabled" on those runs read as though SIP had been validated and
    passed — badly wrong when driving a deliberately SIP-ON wave (RELOPS-2515, 2026-08-14).
    """
    out = _banner(action, tmp_path=tmp_path)
    assert "SIP must be disabled" not in out
    assert "SIP-on allowed" not in out


@pytest.mark.parametrize("allow,expected", [(False, "SIP must be disabled"), (True, "SIP-on allowed")])
def test_banner_still_reports_the_sip_gate_where_it_is_real(allow, expected, tmp_path):
    for action in ("preflight", "provision"):
        assert expected in _banner(action, allow_sip_enabled=allow, tmp_path=tmp_path)


def test_banner_calls_os_update_version_a_target_not_a_gate(tmp_path):
    # os-update IS the step that reaches the target version, so gating on it would be
    # self-defeating; _child_cmd passes --expected-os as the destination, not a precondition.
    out = _banner("os-update", tmp_path=tmp_path)
    assert "target macOS" in out and "gate:" not in out


def test_action_help_lists_every_supported_action():
    """batch.ACTIONS is the source of truth; the --action help text drifted off it.

    It advertised only preflight|mint|provision, so `--action os-update` looked unsupported
    even though it works — which is how a wave nearly got driven the long way round.
    """
    import inspect

    from orchestrator import cli

    help_text = inspect.signature(cli.batch).parameters["action"].default.help
    for a in batch.ACTIONS:
        assert a in help_text, f"--action help omits {a!r}"


# --- secrets are resolved once in the parent, not N times in the children ---


def test_prewarm_puts_resolved_secrets_in_the_child_env(tmp_path):
    """N children each calling `op` is what lost 9 of 10 hosts on batch-3 mint.

    _resolve() prefers a direct env value over the op:// ref, so populating REPROVISION_* in the
    child env means the children never invoke `op` at all.
    """
    seen = {}

    def _run(cmd, stdout=None, stderr=None, timeout=None, check=False, env=None):
        seen.update(env or {})
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=None, stderr=None)

    with patch("orchestrator.batch.subprocess.run", side_effect=_run), \
         patch("orchestrator.batch._log_dir", return_value=tmp_path), \
         patch("orchestrator.batch.ui.batch_summary"), \
         patch("orchestrator.batch._prewarm_secret_env", return_value={"REPROVISION_SSH_ADMIN_KEY": "k"}):
        batch.run_batch(["macmini-m4-245"], action="preflight")
    assert seen.get("REPROVISION_SSH_ADMIN_KEY") == "k"
    assert "PATH" in seen, "child env must inherit os.environ, not replace it"


@pytest.mark.real_prewarm
def test_prewarm_is_best_effort_and_never_aborts_the_batch():
    """A batch action that doesn't need a given credential must not fail because it wouldn't
    resolve — and a secret we skip just falls back to the child resolving it itself.
    """
    boom = Exception("1Password desktop app not running")
    with patch("orchestrator.secrets.ssh_admin_key", side_effect=boom), \
         patch("orchestrator.secrets.ssh_admin_password", side_effect=boom), \
         patch("orchestrator.secrets.simplemdm_api_key", side_effect=boom), \
         patch("orchestrator.secrets.tc_credentials", side_effect=boom), \
         patch("orchestrator.batch.ui.warn"):
        assert batch._prewarm_secret_env() == {}


@pytest.mark.real_prewarm
def test_prewarm_skips_empty_values():
    with patch("orchestrator.secrets.ssh_admin_key", return_value=""), \
         patch("orchestrator.secrets.ssh_admin_password", return_value="pw"), \
         patch("orchestrator.secrets.simplemdm_api_key", return_value=""), \
         patch("orchestrator.secrets.tc_credentials", return_value=("", "")):
        env = batch._prewarm_secret_env()
    assert env == {"REPROVISION_SSH_ADMIN_PASSWORD": "pw"}
