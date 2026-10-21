"""reprovision-runner — the on-network side that pulls jobs from Hangar and runs the CLI.

httpx is mocked; nothing hits the network."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from orchestrator import runner


class _Cfg:
    api = "http://hangar/api"
    token = "t"
    runner_id = "runner-1"
    unquarantine = False
    headers = {"X-Reprovision-Runner-Token": "t", "X-Reprovision-Runner-Id": "runner-1"}


def test_claim_calls_claim_endpoint_and_returns_job():
    client = MagicMock()
    client.post.return_value.json.return_value = {"job": {"id": 7, "short": "macmini-m4-80"}}
    job = runner._claim(client, _Cfg())
    assert job == {"id": 7, "short": "macmini-m4-80"}
    assert client.post.call_args[0][0].endswith("/reprovision/runner/claim")


def test_claim_returns_none_when_queue_empty():
    client = MagicMock()
    client.post.return_value.json.return_value = {"job": None}
    assert runner._claim(client, _Cfg()) is None


def test_event_is_best_effort_and_never_raises():
    client = MagicMock()
    client.post.side_effect = httpx.HTTPError("boom")
    runner._event(client, _Cfg(), 1, "some progress")  # must not raise


def test_complete_posts_outcome():
    client = MagicMock()
    runner._complete(client, _Cfg(), 5, True, "reprovisioned")
    assert client.post.call_args[0][0].endswith("/reprovision/runner/jobs/5/complete")
    assert client.post.call_args.kwargs["json"]["success"] is True


def test_job_cmd_reprovision_prefers_sibling_of_interpreter(tmp_path):
    # A `reprovision` next to sys.executable is used even when PATH lacks the venv.
    import sys

    sibling = tmp_path / "reprovision"
    sibling.write_text("#!/bin/sh\n")
    with patch.object(sys, "executable", str(tmp_path / "python")):
        assert runner._job_cmd("macmini-m4-80", {}, _Cfg()) == [str(sibling), "run", "macmini-m4-80"]


def test_job_cmd_reprovision_falls_back_to_path_lookup():
    import sys

    with patch.object(sys, "executable", "/nonexistent/dir/python"):
        assert runner._job_cmd("macmini-m4-80", {}, _Cfg()) == ["reprovision", "run", "macmini-m4-80"]


def test_job_cmd_reprovision_appends_unquarantine_flag():
    """unquarantine=True adds --unquarantine so the host returns to service after reprovision."""
    import sys

    class _CfgUnq(_Cfg):
        unquarantine = True

    with patch.object(sys, "executable", "/nonexistent/dir/python"):
        assert runner._job_cmd("macmini-m4-80", {}, _CfgUnq()) == \
            ["reprovision", "run", "macmini-m4-80", "--unquarantine"]
        # default cfg leaves the host quarantined — flag absent
        assert "--unquarantine" not in runner._job_cmd("macmini-m4-80", {}, _Cfg())


def test_job_cmd_quarantine_passes_until_and_info():
    """A quarantine job runs the `quarantine` subcommand with --until/--info."""
    import sys

    with patch.object(sys, "executable", "/nonexistent/dir/python"):
        job = {"action": "quarantine", "params": {"until": "2026-08-01T00:00:00.000Z", "info": "by rcurran"}}
        assert runner._job_cmd("macmini-m4-80", job, _Cfg()) == \
            ["reprovision", "quarantine", "macmini-m4-80", "--until", "2026-08-01T00:00:00.000Z", "--info", "by rcurran"]


def test_job_cmd_unquarantine():
    import sys

    with patch.object(sys, "executable", "/nonexistent/dir/python"):
        job = {"action": "unquarantine", "params": {}}
        assert runner._job_cmd("macmini-m4-80", job, _Cfg()) == ["reprovision", "unquarantine", "macmini-m4-80"]


def test_job_cmd_explicit_reprovision_action_runs_the_workflow():
    job = {"action": "reprovision", "params": {}}
    assert runner._job_cmd("macmini-m4-80", job, _Cfg())[1:] == ["run", "macmini-m4-80"]


def test_job_cmd_unknown_action_is_rejected_not_run():
    # A Hangar newer than this runner can queue an action it doesn't know. Falling through
    # to `run` would turn that into an EACS wipe.
    with pytest.raises(ValueError, match="unknown action"):
        runner._job_cmd("macmini-r8-124", {"action": "some-future-action", "params": {}}, _Cfg())


def test_run_job_fails_unknown_action_without_starting_anything():
    client = MagicMock()
    with patch("orchestrator.runner.subprocess.Popen") as popen:
        runner._run_job(client, _Cfg(), {"id": 11, "short": "macmini-r8-124", "action": "some-future-action"})
    popen.assert_not_called()
    body = client.post.call_args.kwargs["json"]
    assert body["success"] is False and "unknown action" in body["detail"]


def test_complete_retries_until_success():
    client = MagicMock()
    ok = MagicMock()
    client.post.side_effect = [httpx.HTTPError("boom"), ok]
    with patch("orchestrator.runner.time.sleep"):
        runner._complete(client, _Cfg(), 5, True, "done")
    assert client.post.call_count == 2
    ok.raise_for_status.assert_called_once()


def test_complete_gives_up_without_raising_after_retries():
    client = MagicMock()
    client.post.side_effect = httpx.HTTPError("boom")
    with patch("orchestrator.runner.time.sleep"):
        runner._complete(client, _Cfg(), 5, False, "x")  # must not raise — best-effort backstop is Hangar's reaper
    assert client.post.call_count == 5


def test_run_job_reports_failure_when_cli_missing():
    # subprocess is mocked so the real `reprovision` CLI is never invoked.
    client = MagicMock()
    with patch("orchestrator.runner.subprocess.Popen", side_effect=FileNotFoundError), \
         patch("orchestrator.runner._complete") as complete:
        runner._run_job(client, _Cfg(), {"id": 9, "short": "macmini-m4-80"})
    _client, _cfg, job_id, success, detail = complete.call_args[0]
    assert job_id == 9 and success is False and "not found" in detail


def test_run_job_streams_lines_and_completes_success():
    client = MagicMock()
    fake = MagicMock()
    fake.stdout = iter(["▸ WIPE · EACS\n", "  ✓ erase accepted\n", "\n"])
    fake.wait.return_value = 0
    with patch("orchestrator.runner.subprocess.Popen", return_value=fake), \
         patch("orchestrator.runner._event") as event, \
         patch("orchestrator.runner._complete") as complete:
        runner._run_job(client, _Cfg(), {"id": 3, "short": "macmini-m4-80"})
    assert complete.call_args[0][3] is True          # success
    assert event.call_count >= 2                      # streamed the non-blank stdout lines


# --- concurrent runner: the guard wrapper always reports a terminal outcome ---

def test_run_job_guarded_reports_failure_on_crash():
    """If _run_job raises, the guard must still _complete the job (else it stays 'open'
    forever and wedges the host + the concurrent pool slot)."""
    client = MagicMock()
    with patch("orchestrator.runner._run_job", side_effect=RuntimeError("boom")), \
         patch("orchestrator.runner._complete") as complete:
        runner._run_job_guarded(client, _Cfg(), {"id": 7, "short": "macmini-m4-111"})
    complete.assert_called_once()
    args = complete.call_args.args
    assert args[2] == 7 and args[3] is False  # job_id, success=False


def test_max_concurrent_defaults_and_reads_env(monkeypatch):
    monkeypatch.setenv("HANGAR_API_URL", "http://hangar/api")
    monkeypatch.setenv("REPROVISION_RUNNER_TOKEN", "t")
    monkeypatch.delenv("RUNNER_MAX_CONCURRENT", raising=False)
    assert runner.Config().max_concurrent == 3  # default = staging pool size
    monkeypatch.setenv("RUNNER_MAX_CONCURRENT", "5")
    assert runner.Config().max_concurrent == 5


# --- graceful drain: SIGTERM stops claims but lets in-flight jobs finish ---

class _PollCfg(_Cfg):
    poll = 0.05
    max_concurrent = 3


def test_serve_on_stop_claims_nothing_more_and_waits_for_in_flight_job():
    """launchd SIGTERMs the runner on every restart (cert renewal, puppet reload). Returning
    while a job runs would let launchd kill its `reprovision` subprocess mid-EACS."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    started, release, stop = threading.Event(), threading.Event(), threading.Event()
    jobs = iter([{"id": 1, "short": "macmini-m4-111"}])

    def fake_job(*_a):
        started.set()
        release.wait(5)

    def fake_claim(*_a):
        # Stop lands while the fill pass is still claiming: the next claim must not happen.
        job = next(jobs, None)
        if job is None:
            started.wait(2)
            stop.set()
        return job

    with patch("orchestrator.runner._claim", side_effect=fake_claim) as claim, \
         patch("orchestrator.runner._run_job_guarded", side_effect=fake_job), \
         ThreadPoolExecutor(max_workers=3) as pool:
        t = threading.Thread(target=runner._serve, args=(MagicMock(), _PollCfg(), pool, stop))
        t.start()
        assert started.wait(2)
        stop.wait(2)
        claims_at_stop = claim.call_count
        t.join(0.3)
        assert t.is_alive(), "returned while a job was still running"
        assert claim.call_count == claims_at_stop, "claimed new work after stop"
        release.set()
        t.join(2)
        assert not t.is_alive(), "did not return after the in-flight job finished"


def test_serve_returns_promptly_when_stopped_and_idle():
    import threading
    from concurrent.futures import ThreadPoolExecutor

    stop = threading.Event()
    stop.set()
    with patch("orchestrator.runner._claim") as claim, ThreadPoolExecutor(max_workers=1) as pool:
        runner._serve(MagicMock(), _PollCfg(), pool, stop)
    claim.assert_not_called()


def test_main_installs_a_sigterm_drain_handler(monkeypatch):
    import signal

    monkeypatch.setenv("HANGAR_API_URL", "http://hangar/api")
    monkeypatch.setenv("REPROVISION_RUNNER_TOKEN", "t")
    previous = signal.getsignal(signal.SIGTERM)
    seen = {}

    def fake_serve(_client, _cfg, _pool, stop):
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        seen["stopped"] = stop.is_set()

    try:
        with patch("orchestrator.runner._serve", side_effect=fake_serve), \
             patch("orchestrator.runner.httpx.Client"):
            runner.main()
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert seen["stopped"] is True


def test_job_cmd_tart_host_update_passes_params():
    import sys

    with patch.object(sys, "executable", "/nonexistent/dir/python"):
        job = {"id": 77, "action": "tart-host-update", "params": {"branch": "master", "min_os": "15.8"}}
        assert runner._job_cmd("macmini-m4-249", job, _Cfg()) == [
            "reprovision", "tart-host-update", "macmini-m4-249", "--job", "77",
            "--branch", "master", "--min-os", "15.8",
        ]


def test_job_cmd_tart_host_update_puppet_only():
    import sys

    with patch.object(sys, "executable", "/nonexistent/dir/python"):
        job = {"id": 78, "action": "tart-host-update", "params": {"puppet_only": True}}
        assert runner._job_cmd("macmini-m4-249", job, _Cfg()) == [
            "reprovision", "tart-host-update", "macmini-m4-249", "--job", "78", "--puppet-only",
        ]


# --- cert reload without restart + startup reset ---


class _CertCfg(_Cfg):
    def __init__(self, cert, key):
        self.client_cert, self.client_key = str(cert), str(key)

    @property
    def client_kwargs(self):
        return {"cert": (self.client_cert, self.client_key)}


def test_reloading_client_rebuilds_only_when_cert_files_change(tmp_path):
    """A renewed cert must be picked up without restarting the runner (a restart killed m4-191's job)."""
    import os

    cert, key = tmp_path / "c.crt", tmp_path / "c.key"
    cert.write_text("a")
    key.write_text("a")
    made = []
    with patch("orchestrator.runner.httpx.Client", side_effect=lambda **kw: made.append(kw) or MagicMock()):
        c = runner._ReloadingClient(_CertCfg(cert, key))
        first = c._current()
        assert c._current() is first and len(made) == 1  # unchanged files: same client
        st = cert.stat()
        os.utime(cert, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))  # step-ca renewal rewrites the file
        second = c._current()
        assert second is not first and len(made) == 2


def test_reloading_client_with_token_auth_builds_once():
    made = []
    cfg = _Cfg()
    cfg.client_cert = cfg.client_key = ""
    cfg.client_kwargs = {}
    with patch("orchestrator.runner.httpx.Client", side_effect=lambda **kw: made.append(kw) or MagicMock()):
        c = runner._ReloadingClient(cfg)
        c._current()
        c._current()
    assert len(made) == 1


def test_reset_reports_orphaned_jobs(capsys):
    client = MagicMock()
    client.post.return_value = MagicMock(status_code=200, json=lambda: {"failed": [73]})
    runner._reset(client, _Cfg())
    assert client.post.call_args.args[0] == "http://hangar/api/reprovision/runner/reset"
    assert "orphaned job(s) [73]" in capsys.readouterr().out


def test_reset_is_best_effort_against_an_older_hangar(capsys):
    client = MagicMock()
    resp = MagicMock()
    resp.raise_for_status.side_effect = httpx.HTTPStatusError("404", request=MagicMock(), response=MagicMock())
    client.post.return_value = resp
    runner._reset(client, _Cfg())  # must not raise
    assert "startup reset skipped" in capsys.readouterr().out


def test_reloading_client_get_delegates_for_the_screen_agent(tmp_path):
    cert, key = tmp_path / "c.crt", tmp_path / "c.key"
    cert.write_text("a")
    key.write_text("a")
    inner = MagicMock()
    with patch("orchestrator.runner.httpx.Client", return_value=inner):
        runner._ReloadingClient(_CertCfg(cert, key)).get("http://hangar/api/screen/agent/requests", timeout=30)
    inner.get.assert_called_once_with("http://hangar/api/screen/agent/requests", timeout=30)
