"""
Tests for the SSH client's host-key handling — reprovision targets rotate their host key
on every EACS, so worker connections use a tool-owned known_hosts (never the operator's
~/.ssh/known_hosts) and clear stale entries around a wipe.
"""

from __future__ import annotations

from unittest.mock import patch

from orchestrator.clients import ssh


def test_run_uses_tool_known_hosts_not_operators():
    with patch("orchestrator.clients.ssh.subprocess.run") as m:
        ssh.run("macmini-m4-80.example.com", "true", check=False)
        argv = m.call_args[0][0]
    joined = " ".join(argv)
    assert "UserKnownHostsFile=" in joined
    assert "reprovision/known_hosts" in joined  # our file
    assert "/.ssh/known_hosts" not in joined     # never the operator's


def test_forget_host_key_scoped_to_tool_file():
    with patch("orchestrator.clients.ssh.subprocess.run") as m:
        ssh.forget_host_key("macmini-m4-80.example.com")
        argv = m.call_args[0][0]
    assert argv[0] == "ssh-keygen"
    assert "-R" in argv and "macmini-m4-80.example.com" in argv
    # -f <our file>: operate on the tool's known_hosts, not the operator's
    assert "-f" in argv
    assert any("reprovision/known_hosts" in a for a in argv)
    assert not any("/.ssh/known_hosts" in a for a in argv)
