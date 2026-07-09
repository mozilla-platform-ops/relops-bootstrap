"""reprovision-runner — the on-network side that pulls jobs from Hangar and runs the CLI.

httpx is mocked; nothing hits the network."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx

from orchestrator import runner


class _Cfg:
    api = "http://hangar/api"
    token = "t"
    runner_id = "runner-1"
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
