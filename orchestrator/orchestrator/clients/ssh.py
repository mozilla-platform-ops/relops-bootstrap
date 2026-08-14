"""
Thin SSH wrapper around the local ssh binary (no paramiko/asyncssh dep). Reuses
the operator's ssh-agent, but deliberately uses a *tool-owned* known_hosts file
(see _tool_known_hosts) rather than the operator's ~/.ssh/known_hosts — reprovision
targets rotate their host key on every EACS, and a single stale/bad line in the
operator's file would make ssh-keygen -R refuse the whole file.
"""

from __future__ import annotations

import atexit
import os
import shlex
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from ..config import get_settings
from ..errors import ReprovisionError
from ..secrets import ssh_admin_key, ssh_admin_password


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


_identity_lock = threading.Lock()
_identity_path: str | None = None


def _admin_identity_file() -> str | None:
    """Materialize the vault-fetched admin private key to a 0600 temp file for `ssh -i`.

    Returns None when no admin key is configured — ssh then falls back to the agent / default
    identities (the pre-existing behavior, so operators who already have the key on disk keep
    working).

    Cached per process, but the cache is **validated against the filesystem** on every call and
    re-materialized if the file has gone away. It must be: hangar-tart-health-agent runs this
    module as a LaunchDaemon with `--loop`, i.e. one process alive for weeks, and a temp file
    does not live that long. When this was a plain lru_cache, the first thing to remove the file
    from /tmp broke every subsequent sweep *permanently* until the daemon restarted --
    `_identity_opts()` kept handing ssh `-i <deleted path>` together with `IdentitiesOnly=yes`,
    which forbids falling back to the agent, so auth could only fail:

        ssh failed twice: Warning: Identity file /tmp/reprovision-admin-XXXX.key not accessible:
        No such file or directory. ... Permission denied (publickey,password,keyboard-interactive)

    Observed on macmini-m4-81 on 2026-08-04 against the whole tart VM fleet: the agent had been
    up since 2026-07-30 19:00 and /tmp held no reprovision-admin key at all. The helper was
    written for the short-lived `reprovision` CLI (materialize once, drop at exit); commit
    13895c2 pointed a long-lived daemon at it without revisiting that assumption.

    Thread-safe: the agent collects hosts through a ThreadPoolExecutor, so several threads can
    call this at once and must not each write their own key.
    """
    global _identity_path
    with _identity_lock:
        if _identity_path is not None and os.path.exists(_identity_path):
            return _identity_path
        key = ssh_admin_key()  # itself lru_cached, so re-materializing costs no secret fetch
        if not key:
            return None
        fd, path = tempfile.mkstemp(prefix="reprovision-admin-", suffix=".key")
        os.write(fd, key.encode() if key.endswith("\n") else (key + "\n").encode())
        os.close(fd)
        os.chmod(path, 0o600)  # ssh refuses a group/world-readable private key
        atexit.register(lambda p=path: os.path.exists(p) and os.remove(p))
        _identity_path = path
        return path


def _reset_admin_identity_cache() -> None:
    """Forget the materialized key path (tests; also correct after an admin-key rotation)."""
    global _identity_path
    with _identity_lock:
        _identity_path = None


# Back-compat for callers/tests that used the old lru_cache surface.
_admin_identity_file.cache_clear = _reset_admin_identity_cache  # type: ignore[attr-defined]


def _identity_opts() -> list[str]:
    """`ssh -i <fetched key>` (+ IdentitiesOnly so the agent's keys don't shadow it), or []."""
    path = _admin_identity_file()
    if not path:
        return []
    return ["-o", "IdentitiesOnly=yes", "-i", path]


def user_host(hostname: str) -> str:
    """`<admin user>@<hostname>` — public so other tools in this package don't guess the user.

    hangar-tart-health-agent runs as root under launchd, where an ssh with no user
    becomes `root@` and is refused by every worker.
    """
    return _user_host(hostname)


def admin_ssh_opts() -> list[str]:
    """The ssh options any tool here should use to reach a worker.

    Kept public and in one place so the admin identity, the accept-new policy and the
    tool-owned known_hosts stay consistent across callers — notably so a rotated admin
    key takes effect everywhere at once. Callers that need their own timeout run their
    own subprocess with these options rather than going through run().
    """
    return [
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"UserKnownHostsFile={_tool_known_hosts()}",
        "-o", "ConnectTimeout=15",
        *_identity_opts(),
    ]


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
        raise ReprovisionError(f"password login to {user}@{hostname} denied (wrong admin password?)")
    if cp.returncode == 3:
        raise ReprovisionError(f"password login to {user}@{hostname} timed out — on the VPN?")
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


def platform_serial(hostname: str) -> str:
    """Hardware serial number, or '' if unreachable.

    This is the only reliable join key between a hostname and a SimpleMDM device record. A fresh
    DEP arrival is named `Mac mini` in SimpleMDM (with a `device_name` like `Mac mini (39)`) — the
    hostname appears nowhere in the record, because the hostname comes from DHCP and nothing in
    ronin_puppet ever runs `scutil --set`. So the host has to tell us who it is.

    `ioreg` rather than `system_profiler`: it's near-instant, needs no sudo, and doesn't spin up
    the whole SPHardwareDataType collector.
    """
    cp = run(
        hostname,
        "/usr/sbin/ioreg -l | /usr/bin/awk -F'\"' '/IOPlatformSerialNumber/{print $4}'",
        check=False,
    )
    return cp.stdout.decode(errors="replace").strip()


def run(hostname: str, command: str, *, stdin: bytes | None = None, check: bool = True) -> subprocess.CompletedProcess:
    """Run `command` over SSH on hostname. Returns CompletedProcess.

    With check=True, a transport failure (exit 255 — can't connect: off-VPN, host down, key
    mismatch) or any nonzero exit raises a clean ReprovisionError. The error never echoes
    `command`, which may embed the admin password. Callers that inspect the result themselves
    (the wipe guard, sentinel poll) pass check=False and read `returncode`.
    """
    timeout = get_settings().ssh_command_timeout_seconds
    args = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"UserKnownHostsFile={_tool_known_hosts()}",
        "-o", "ConnectTimeout=15",
        *_identity_opts(),
        _user_host(hostname),
        command,
    ]
    try:
        cp = subprocess.run(args, input=stdin, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise ReprovisionError(f"ssh to {hostname} timed out after {timeout}s — are you on the VPN?") from None
    if check and cp.returncode != 0:
        detail = cp.stderr.decode(errors="replace").strip().splitlines()
        tail = detail[-1] if detail else f"exit {cp.returncode}"
        if cp.returncode == 255:
            raise ReprovisionError(f"can't reach {hostname} over ssh — are you on the VPN? (ssh: {tail})")
        raise ReprovisionError(f"remote command on {hostname} failed (exit {cp.returncode}): {tail}")
    return cp


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
