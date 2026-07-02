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
    s = get_settings()
    user = s.ssh_admin_user
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
  -re "(P|p)assword:" {{ send -- "{s.ssh_admin_password}\\r"; exp_continue }}
  -re "(denied|failed|Authentication)" {{ exit 2 }}
  timeout {{ exit 3 }}
  eof
}}
"""
    cp = subprocess.run(
        ["expect", "-"],
        input=script.encode(),
        capture_output=True,
        timeout=s.ssh_command_timeout_seconds,
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
