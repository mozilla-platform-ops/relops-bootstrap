"""hangar-screen-agent — the on-network side that pushes VNC frames to Hangar.

httpx is mocked; asyncvnc is imported lazily inside _grab so these run without it installed."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from orchestrator import screen_agent


class _Cfg:
    api = "http://hangar/api"
    headers = {"X-Reprovision-Runner-Id": "r1"}
    client_cert = "/c"
    runner_id = "r1"


def test_pending_returns_watched_hosts():
    client = MagicMock()
    client.get.return_value.json.return_value = {"hosts": ["macmini-m4-80.test"]}
    assert screen_agent._pending(client, _Cfg()) == ["macmini-m4-80.test"]
    assert client.get.call_args[0][0].endswith("/screen/requests")


def test_push_posts_jpeg_body_with_content_type():
    client = MagicMock()
    screen_agent._push(client, _Cfg(), "macmini-m4-80.test", b"\xff\xd8jpeg")
    assert client.post.call_args[0][0].endswith("/screen/macmini-m4-80.test/frame")
    assert client.post.call_args.kwargs["headers"]["Content-Type"] == "image/jpeg"
    assert client.post.call_args.kwargs["content"] == b"\xff\xd8jpeg"


def test_admin_password_required(monkeypatch):
    monkeypatch.delenv("REPROVISION_SSH_ADMIN_PASSWORD", raising=False)
    with pytest.raises(SystemExit):
        screen_agent._admin_password()
