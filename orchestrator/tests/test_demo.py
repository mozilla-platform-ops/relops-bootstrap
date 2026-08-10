"""The `reprovision demo` replays must run start-to-finish without touching a host/network."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from orchestrator import demo


def test_demo_runs_and_touches_nothing():
    # No ssh / SimpleMDM / Taskcluster clients are imported or called by the demo; stub the
    # sleeps so it completes instantly. Any accidental real call would blow up here.
    with patch("orchestrator.demo.time.sleep"):
        demo.run_demo()


@pytest.mark.parametrize("flow", demo.FLOWS)
def test_every_flow_runs_and_touches_nothing(flow):
    # A demo that raises mid-replay in front of an audience is the failure mode worth pinning,
    # so every registered flow gets exercised end-to-end.
    with patch("orchestrator.demo.time.sleep"):
        demo.run(flow)


def test_demo_flow_accepts_a_host_override():
    with patch("orchestrator.demo.time.sleep"):
        demo.run("provision", "macmini-m4-250")


def test_unknown_flow_fails_loudly():
    # Silently defaulting would mean showing the wrong replay to an audience.
    with pytest.raises(ValueError, match="unknown demo flow"):
        demo.run("wipe-everything")


def test_batch_replay_shows_a_mixed_result_set():
    # The batch replay's entire point is demonstrating skipped-vs-failed classification, so a
    # sweep that silently became all-green would make it pointless.
    states = {row[1] for row in demo._SWEEP}
    assert "ok" in states and "skipped" in states
    # And the wave must show the group-assignment miss, the thing the pkg gate exists to catch.
    assert any("bootstrap pkg hasn't landed" in row[2] for row in demo._WAVE)
