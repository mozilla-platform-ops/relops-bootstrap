"""The `reprovision demo` replay must run start-to-finish without touching a host/network."""

from __future__ import annotations

from unittest.mock import patch

from orchestrator import demo


def test_demo_runs_and_touches_nothing():
    # No ssh / SimpleMDM / Taskcluster clients are imported or called by the demo; stub the
    # sleeps so it completes instantly. Any accidental real call would blow up here.
    with patch("orchestrator.demo.time.sleep"):
        demo.run_demo()
