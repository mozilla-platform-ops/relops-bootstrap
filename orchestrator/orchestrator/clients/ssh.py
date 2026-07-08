"""
Thin SSH wrapper. Uses the local ssh binary so the operator's existing ssh-agent
+ known_hosts are reused. No paramiko/asyncssh dep.
"""

from __future__ import annotations

import shlex
import socket
import subprocess
import time
from pathlib import Path

from ..config import get_settings
from ..secrets import ssh_admin_password


def _user_host(hostname: str) -> str:
    return f"{get_settings().ssh_admin_user}@{hostname}"


def _tool_known_hosts() -> str:
    """A known_hosts file owned by this tool, kept out of the operator's ~/.ssh/known_hosts.

    Two reasons the operator's file is the wrong place for reprovision targets:
      1. EACS regenerates a host's SSH keys every cycle, so a stored key legitimately
         goes stale and trips StrictHostKeyChecking on the next connect.
      2. Personal known_hosts are often large/hand-edited; a single malformed line makes
         `ssh-keygen -R` refuse to touch the whole file (seen in the field).
    Using our own file means neither can wedge the flow: we accept-new here and clear a
    host's entry (forget_host_key) when we know its key just rotated.
    """
    path = Path.home() / ".config" / "reprovision" / "known_hosts"
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def forget_host_key(hostname: str) -> None:
    """Drop hostname from the tool's known_hosts so the next connect accept-new's the current
    key. Idempotent, and scoped to our own file — the operator's ~/.ssh/known_hosts is never
    touched. Call this when a host's key has (legitimately) rotated, i.e. around EACS."""
    subprocess.run(
        ["ssh-keygen", "-f", _tool_known_hosts(), "-R", hostname],
        capture_output=True,
        timeout=15,
        check=False,
    )


def wait_for_sshd(hostname: str, *, timeout: int = 900, port: int = 22, poll: int = 15) -> None:
    """Block until sshd is listening on hostname:port (the relops-ssh prestage pkg brings it
    up a few minutes into DEP convergence). Checks the socket only — auth-independent."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((hostname, port), timeout=5):
                return
        except OSError:
            time.sleep(poll)
    raise TimeoutError(f"sshd on {hostname}:{port} not reachable within {timeout}s")


def password_login(hostname: str) -> None:
    """
    Perform an *interactive* password SSH login to mint the first SecureToken.

    On this fleet DEP skips Setup Assistant, so no account is a volume owner at
    enrollment and admin has no SecureToken. The first token is only granted by a
    PAM (password) login — key-based ssh does NOT trigger it. This is the automated
    equivalent of the operator's manual `ssh admin@host` + typing the password.

    We drive it with `expect` (present on the operator's macOS) because sshpass is
    unreliable against macOS keyboard-interactive. The authentication itself mints
    the token; the remote command (`true`) is irrelevant.
    """
    user = get_settings().ssh_admin_user
    password = ssh_admin_password()
    script = f"""
set timeout 45
log_user 0
spawn ssh -F /dev/null \\
  -o PubkeyAuthentication=no \\
  -o PreferredAuthentications=keyboard-interactive,password \\
  -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/dev/null \\
  -o NumberOfPasswordPrompts=1 -o ConnectTimeout=20 \\
  {user}@{hostname} true
expect {{
  -re "(P|p)assword:" {{ send -- "{password}\\r"; exp_continue }}
  -re "(denied|failed|Authentication)" {{ exit 2 }}
  timeout {{ exit 3 }}
  eof
}}
"""
    cp = subprocess.run(
        ["expect", "-"],
        input=script.encode(),
        capture_output=True,
        timeout=get_settings().ssh_command_timeout_seconds,
        check=False,
    )
    if cp.returncode == 2:
        raise RuntimeError(f"password login to {user}@{hostname} denied (wrong ssh_admin_password?)")
    if cp.returncode == 3:
        raise RuntimeError(f"password login to {user}@{hostname} timed out")
    # returncode 0 (or other) — the auth is what mattered; caller verifies the token.


def secure_token_status(hostname: str) -> str:
    """Return the admin SecureToken status word (e.g. 'ENABLED'/'DISABLED'), '' if unreachable."""
    user = get_settings().ssh_admin_user
    cp = run(
        hostname,
        f"sudo sysadminctl -secureTokenStatus {user} 2>&1 | sed 's/.*Secure token is //'",
        check=False,
    )
    return cp.stdout.decode(errors="replace").strip()


def run(hostname: str, command: str, *, stdin: bytes | None = None, check: bool = True) -> subprocess.CompletedProcess:
    """Run `command` over SSH on hostname. Returns CompletedProcess."""
    timeout = get_settings().ssh_command_timeout_seconds
    args = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"UserKnownHostsFile={_tool_known_hosts()}",
        "-o", "ConnectTimeout=15",
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
