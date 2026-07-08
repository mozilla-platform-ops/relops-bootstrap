"""
Tests for the SSH client's host-key handling — reprovision targets rotate their host key
on every EACS, so worker connections use a tool-owned known_hosts (never the operator's
~/.ssh/known_hosts) and clear stale entries around a wipe.
"""

from __future__ import annotations

import os
import stat
from unittest.mock import patch

import pytest

from orchestrator.clients import ssh
from orchestrator.errors import ReprovisionError


def test_run_uses_tool_known_hosts_not_operators():
    with patch("orchestrator.clients.ssh._admin_identity_file", return_value=None), \
         patch("orchestrator.clients.ssh.subprocess.run") as m:
        ssh.run("macmini-m4-80.example.com", "true", check=False)
        argv = m.call_args[0][0]
    joined = " ".join(argv)
    assert "UserKnownHostsFile=" in joined
    assert "reprovision/known_hosts" in joined  # our file
    assert "/.ssh/known_hosts" not in joined     # never the operator's


def test_run_uses_admin_identity_when_configured():
    with patch("orchestrator.clients.ssh._admin_identity_file", return_value="/tmp/fake-admin.key"), \
         patch("orchestrator.clients.ssh.subprocess.run") as m:
        ssh.run("host.example.com", "true", check=False)
        argv = m.call_args[0][0]
    assert "-i" in argv and "/tmp/fake-admin.key" in argv
    assert "IdentitiesOnly=yes" in argv  # our fetched key, not the agent's


def test_run_no_identity_when_not_configured():
    with patch("orchestrator.clients.ssh._admin_identity_file", return_value=None), \
         patch("orchestrator.clients.ssh.subprocess.run") as m:
        ssh.run("host.example.com", "true", check=False)
        argv = m.call_args[0][0]
    assert "-i" not in argv  # fall back to agent / default identities


def test_admin_identity_file_writes_private_key_0600():
    ssh._admin_identity_file.cache_clear()
    with patch("orchestrator.clients.ssh.ssh_admin_key", return_value="PRIVATE-KEY-BODY"):
        path = ssh._admin_identity_file()
    try:
        with open(path) as fh:
            assert fh.read() == "PRIVATE-KEY-BODY\n"  # trailing newline added for ssh
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    finally:
        os.remove(path)
        ssh._admin_identity_file.cache_clear()


def test_run_255_gives_friendly_vpn_error():
    class CP:
        returncode = 255
        stdout = b""
        stderr = b"ssh: connect to host ... port 22: Operation timed out"

    with patch("orchestrator.clients.ssh._admin_identity_file", return_value=None), \
         patch("orchestrator.clients.ssh.subprocess.run", return_value=CP()):
        with pytest.raises(ReprovisionError) as ei:
            ssh.run("macmini-m4-80.example.com", "true")  # check=True by default
    assert "VPN" in str(ei.value)


def test_run_check_false_returns_result_without_raising():
    class CP:
        returncode = 255
        stdout = b""
        stderr = b"nope"

    with patch("orchestrator.clients.ssh._admin_identity_file", return_value=None), \
         patch("orchestrator.clients.ssh.subprocess.run", return_value=CP()):
        cp = ssh.run("host", "true", check=False)  # guards inspect returncode themselves
    assert cp.returncode == 255


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
