"""`batch --action add-to-group --quarantine-on-register` must not be one `-j` serving two resources.

The add is bound by the SimpleMDM API (three calls per host, no SSH to pace it); the watch is a
~30-minute wait bound by Taskcluster. Sharing one concurrency knob between them is what produced
the worst near-miss of wave 1: at `-j12` on 2026-08-14 five hosts *reported* failure while twelve
had already been added to the group, so killing the batch orphaned twelve autonomous bootstraps
with nothing watching them. They would have gone into production unvalidated.

These tests pin the properties that stop that recurring, not the implementation:
concurrency is clamped per resource, a *failed* add is still watched, and an interrupt leaves a
resume roster on disk.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from orchestrator import batch
from orchestrator.config import get_settings


@pytest.fixture(autouse=True)
def _no_prewarm():
    with patch("orchestrator.batch._prewarm_secret_env", return_value={}):
        yield


HOSTS = [f"macmini-m4-2{n:02d}" for n in range(40, 52)]  # 12 hosts — the -j12 run


class _Recorder:
    """Stands in for subprocess.run, recording every child invocation by phase."""

    def __init__(self, codes: dict[tuple[str, str], int] | None = None):
        self.codes = codes or {}
        self.calls: list[dict] = []
        self.peak: dict[str, int] = {}
        self._in_flight: dict[str, int] = {}
        self._lock = threading.Lock()

    def __call__(self, cmd, stdout=None, stderr=None, timeout=None, check=False, env=None):
        action = cmd[1]
        host = next(c for c in cmd if c.startswith("macmini-"))
        with self._lock:
            self._in_flight[action] = self._in_flight.get(action, 0) + 1
            self.peak[action] = max(self.peak.get(action, 0), self._in_flight[action])
            self.calls.append({"action": action, "host": host, "cmd": cmd, "timeout": timeout})
        try:
            threading.Event().wait(0.01)
        finally:
            with self._lock:
                self._in_flight[action] -= 1
        return subprocess.CompletedProcess(
            args=cmd, returncode=self.codes.get((action, host), 0), stdout=None, stderr=None
        )

    def hosts_for(self, action: str) -> list[str]:
        return [c["host"] for c in self.calls if c["action"] == action]


def _run(rec: _Recorder, tmp_path: Path, hosts=None, **kw):
    with patch("orchestrator.batch.subprocess.run", side_effect=rec), \
         patch("orchestrator.batch._log_dir", return_value=tmp_path), \
         patch("orchestrator.batch.ui.batch_summary"):
        return batch.run_batch(
            hosts or HOSTS, action="add-to-group", quarantine_on_register=True, **kw
        )


def test_the_coupled_invocation_runs_as_two_distinct_phases(tmp_path):
    rec = _Recorder()
    _run(rec, tmp_path)
    # Sorted: both phases run concurrently, so completion order is not deterministic.
    assert sorted(rec.hosts_for("add-to-group")) == sorted(HOSTS)
    assert sorted(rec.hosts_for("quarantine-on-register")) == sorted(HOSTS)
    # and the add child is no longer asked to do the watching itself
    for call in rec.calls:
        if call["action"] == "add-to-group":
            assert "--quarantine-on-register" not in call["cmd"]


def test_the_add_phase_is_clamped_to_the_simplemdm_cap_whatever_j_says(tmp_path):
    """-j is the knob operators reach for to speed up the watch. It must not reach SimpleMDM."""
    rec = _Recorder()
    _run(rec, tmp_path, concurrency=12)
    assert rec.peak["add-to-group"] <= get_settings().simplemdm_max_concurrent


def test_the_watch_phase_is_not_throttled_by_the_simplemdm_cap(tmp_path):
    """A watcher is an idle poll loop; serialising 12 of them at -j2 costs hours for nothing."""
    rec = _Recorder()
    _run(rec, tmp_path, concurrency=12)
    assert rec.peak["quarantine-on-register"] > get_settings().simplemdm_max_concurrent


def test_a_host_whose_add_FAILED_is_still_watched(tmp_path):
    """The 2026-08-14 case exactly: the POST landed, the follow-up push_apps 429'd.

    The host is in the group and bootstrapping regardless of what the batch called it, so not
    watching it is how an unvalidated worker reaches production.
    """
    rec = _Recorder(codes={("add-to-group", "macmini-m4-241"): 1})
    _run(rec, tmp_path)
    assert "macmini-m4-241" in rec.hosts_for("quarantine-on-register")


def test_a_host_that_was_never_ready_is_not_watched(tmp_path):
    """Exit 2 means the add never happened (no SSH, so its serial was never read)."""
    rec = _Recorder(codes={("add-to-group", "macmini-m4-241"): batch.EXIT_NOT_READY})
    _run(rec, tmp_path)
    assert "macmini-m4-241" not in rec.hosts_for("quarantine-on-register")


def test_the_watch_child_gets_a_bootstrap_spanning_budget_explicitly(tmp_path):
    """The child's 900s default assumes bootstrap already finished; here it hasn't even started.

    Passed as an argument rather than left to REPROVISION_QUARANTINE_ON_REGISTER_MAX_WAIT_SECONDS,
    because a budget that expires before the worker registers puts the host live UNHELD.
    """
    rec = _Recorder()
    _run(rec, tmp_path)
    s = get_settings()
    watch = next(c for c in rec.calls if c["action"] == "quarantine-on-register")
    assert "--max-wait-seconds" in watch["cmd"]
    budget = int(watch["cmd"][watch["cmd"].index("--max-wait-seconds") + 1])
    assert budget >= s.bootstrap_max_wait_seconds
    # the subprocess timeout must outlast the child's own budget, or the batch kills the watcher
    assert watch["timeout"] > budget


def test_an_interrupt_mid_add_leaves_a_resume_roster_on_disk(tmp_path):
    """Silence on Ctrl-C is what turned an interrupt into twelve unheld production workers."""
    rec = _Recorder()
    calls: list[str] = []

    def _boom(cmd, **kw):
        host = next(c for c in cmd if c.startswith("macmini-"))
        calls.append(host)
        if len(calls) > 2:
            raise KeyboardInterrupt
        return rec(cmd, **kw)

    errs: list[str] = []
    with patch("orchestrator.batch.subprocess.run", side_effect=_boom), \
         patch("orchestrator.batch._log_dir", return_value=tmp_path), \
         patch("orchestrator.batch.ui.batch_summary"), \
         patch("orchestrator.batch.ui.err", side_effect=errs.append), \
         pytest.raises(KeyboardInterrupt):
        batch.run_batch(HOSTS, action="add-to-group", quarantine_on_register=True, concurrency=2)

    roster = tmp_path / "added.txt"
    assert roster.exists(), "no resume roster written — the operator has no way back"
    listed = [ln for ln in roster.read_text().splitlines() if not ln.startswith("#")]
    assert listed, "roster names no hosts"
    assert any("quarantine-on-register" in e for e in errs), "no resume command surfaced"


def test_each_phase_keeps_its_own_logs_under_one_batch_directory(tmp_path):
    # Both phases run the same hostnames; sharing one flat directory would have phase 2 overwrite
    # phase 1's log and lose the record of what the add actually did.
    rec = _Recorder()
    _run(rec, tmp_path, hosts=HOSTS[:2])
    assert (tmp_path / "add" / "macmini-m4-240.log").exists()
    assert (tmp_path / "watch" / "macmini-m4-240.log").exists()


def test_dry_run_shows_both_phases_and_touches_nothing(tmp_path):
    wires: list[str] = []
    with patch("orchestrator.batch.subprocess.run") as run, \
         patch("orchestrator.batch._log_dir") as log_dir, \
         patch("orchestrator.batch.ui.batch_summary"), \
         patch("orchestrator.batch.ui.wire", side_effect=wires.append):
        assert batch.run_batch(
            HOSTS[:2], action="add-to-group", quarantine_on_register=True, dry_run=True
        ) == 0
    run.assert_not_called()
    log_dir.assert_not_called()
    joined = "\n".join(wires)
    assert "add-to-group" in joined and "quarantine-on-register" in joined
