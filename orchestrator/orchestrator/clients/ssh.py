"""
Thin SSH wrapper. Uses the local ssh binary so the operator's existing ssh-agent
+ known_hosts are reused. No paramiko/asyncssh dep.
"""

from __future__ import annotations

import shlex
import subprocess

from ..config import get_settings


def _user_host(hostname: str) -> str:
    return f"{get_settings().ssh_admin_user}@{hostname}"


def run(hostname: str, command: str, *, stdin: bytes | None = None, check: bool = True) -> subprocess.CompletedProcess:
    """Run `command` over SSH on hostname. Returns CompletedProcess."""
    timeout = get_settings().ssh_command_timeout_seconds
    args = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"ConnectTimeout=15",
        _user_host(hostname),
        command,
    ]
    return subprocess.run(args, input=stdin, capture_output=True, timeout=timeout, check=check)


def write_file_as_root(hostname: str, remote_path: str, content: bytes, mode: str = "0600") -> None:
    """
    SCP-style file drop with sudo. Pipes the content into `sudo tee` on the host,
    then chmod's it. Used for /var/root/vault.yaml.
    """
    # tee writes the file; chmod restricts perms; chown ensures root:wheel.
    cmd = (
        f"sudo tee {shlex.quote(remote_path)} > /dev/null && "
        f"sudo chmod {mode} {shlex.quote(remote_path)} && "
        f"sudo chown root:wheel {shlex.quote(remote_path)}"
    )
    run(hostname, cmd, stdin=content, check=True)


def file_exists(hostname: str, path: str) -> bool:
    cp = run(hostname, f"test -f {shlex.quote(path)} && echo yes || echo no", check=False)
    return cp.stdout.strip() == b"yes"
