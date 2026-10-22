"""
In-place upgrade of an Intel r8 builder (Mac mini 2018, `*_b_osx_1015` roles, on macOS 10.15
or 13) to macOS 15:
same pool, same vault.yaml, new `*_b_osx_1500` role. One command, drivable from Hangar.

Separate from workflow.py for the same reason as tart_host.py: those steps guard EACS wipes
of M4 testers (admin@, SecureToken, Bootstrap Token). A builder has none of that -- it is
reached as relops@, never wiped, and its pool comes from its own worker-runner config.

Every step here was done by hand on r8-241/242/368/369/370 in Oct 2026; the comments say
what broke without it. docs/builder-macos15-upgrade.md has the background.

Safe to re-run: each step checks whether its work is already done, so a run that failed
halfway resumes. Every failure after the quarantine leaves the worker QUARANTINED.
"""

from __future__ import annotations

import re
import shlex
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from importlib import resources

from rich.markup import escape

from . import ui
from .clients import ssh, taskcluster
from .errors import NotReadyError, ReprovisionError
from .hostnames import validate_short
from .secrets import builder_sudo_password

USER = "relops"
WORKER_GROUP = "mdc1"
TARGET_OS = "15.8.1"
TARGET_MAJOR = 15
# T2 bridgeOS below 20P can't stage the 24P firmware the upgrade needs: 17P6610 boots straight
# back to 10.15 (r8-195, four tries), 18P4347 hung the installer and went dark (r8-139). Those
# hosts need a DFU revive first. 20P and up go through hands-off (r8-368/369/370).
MIN_T2_MAJOR = 20
XCODE_URL = "https://ronin-puppet-package-repo.s3.us-west-2.amazonaws.com/macos/public/15/Xcode_16.4.xip"
XCODE_BUILD = "16F6"
CLT_LABEL = "Command Line Tools for Xcode-16.4"
INSTALLER = "/Applications/Install macOS Sequoia.app"
XCODE_REMOTE = "/var/root/install-xcode.sh"
SEMAPHORE = "/var/tmp/semaphore/run-buildbot"
KCPASSWORD = "/etc/kcpassword"
KCPASSWORD_ASIDE = "/etc/kcpassword.disabled"
CLT_TRIGGER = "/tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress"
# Installer + upgrade + Xcode. r8-241 had 49G of worker cache; clear_caches frees it.
MIN_FREE_GB = 60
# Where builders start from: gecko/nss are on 10.15, applicationservices on macOS 13.
SOURCE_OS = ((10, 15), (13,))

ROLE_UPGRADES = {
    "gecko_1_b_osx_1015": "gecko_1_b_osx_1500",
    "gecko_3_b_osx_1015": "gecko_3_b_osx_1500",
    "nss_1_b_osx_1015": "nss_1_b_osx_1500",
    "nss_3_b_osx_1015": "nss_3_b_osx_1500",
    "applicationservices_1_b_osx_1015": "applicationservices_1_b_osx_1500",
    "applicationservices_3_b_osx_1015": "applicationservices_3_b_osx_1500",
}
# A second puppet run should change nothing but this always-run exec.
ALWAYS_RUNS = ("Exec[install_mercurial_py311]",)


@dataclass
class BuilderHost:
    hostname: str
    fqdn: str
    role: str = ""  # the role it has now
    target_role: str = ""
    pool: str = ""  # from its own worker-runner config: vault.yaml's worker: hash is the truth
    client_id: str = ""
    task_user: str = ""  # the pinned multiuser task user it autologins as


def _ver(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in v.strip().split("."))
    except ValueError:
        return (0,)


def t2_major(firmware: str) -> int:
    """'20P5058' -> 20. Anything unparseable -> 0 (fails the gate)."""
    m = re.match(r"(\d+)P", firmware.strip())
    return int(m.group(1)) if m else 0


def _out(cp) -> str:
    return cp.stdout.decode(errors="replace").strip()


def _sh(h: BuilderHost, cmd: str, *, check: bool = True, timeout: int | None = None, stdin: bytes | None = None) -> str:
    return _out(ssh.run(h.fqdn, cmd, check=check, timeout=timeout, stdin=stdin, user=USER))


def _reachable(h: BuilderHost) -> bool:
    try:
        return ssh.run(h.fqdn, "true", check=False, timeout=30, user=USER).returncode == 0
    except ReprovisionError:
        return False


# --- reads ---------------------------------------------------------------------------------


def os_version(h: BuilderHost) -> str:
    return _sh(h, "sw_vers -productVersion")


def t2_firmware(h: BuilderHost) -> str:
    return _sh(h, "system_profiler SPiBridgeDataType 2>/dev/null | awk -F': ' '/Firmware Version/{print $2}'", check=False)


def boot_time(h: BuilderHost) -> int:
    return int(_sh(h, "sysctl -n kern.boottime | cut -d' ' -f4 | tr -d ,") or 0)


def console_user(h: BuilderHost) -> str:
    return _sh(h, "stat -f %Su /dev/console", check=False)


def has_passwordless_sudo(h: BuilderHost) -> bool:
    return ssh.run(h.fqdn, "sudo -n true", check=False, user=USER).returncode == 0


def worker_config(h: BuilderHost) -> tuple[str, str]:
    """(workerPoolID, clientID) from worker-runner-config.yaml -- never prints the token."""
    out = _sh(h, "sudo -n grep -E '^ *(workerPoolID|clientID):' /opt/worker/worker-runner-config.yaml", check=False)
    pool = client = ""
    for line in out.splitlines():
        key, _, val = line.strip().partition(":")
        val = val.strip().strip('"')
        if key == "workerPoolID":
            pool = val
        elif key == "clientID":
            client = val
    return pool, client


def pinned_task_user(h: BuilderHost) -> str:
    return _sh(
        h,
        "sudo -n /opt/puppetlabs/puppet/bin/ruby -rjson -e "
        "'puts JSON.parse(File.read(\"/opt/worker/current-task-user.json\"))[\"name\"]'",
        check=False,
    )


def autologin_user(h: BuilderHost) -> str:
    return _sh(h, "sudo -n defaults read /Library/Preferences/com.apple.loginwindow autoLoginUser", check=False)


def free_gb(h: BuilderHost) -> int:
    return int(_sh(h, "df -g / | awk 'NR==2{print $4}'") or 0)


def reclaimable_gb(h: BuilderHost) -> int:
    out = _sh(h, "sudo -n du -sg /opt/worker/cache /opt/worker/tasks /Users/cltbld/caches 2>/dev/null | awk '{s+=$1} END{print s+0}'", check=False, timeout=600)
    return int(out or 0)


def xcode_ready(h: BuilderHost) -> bool:
    out = _sh(h, "xcodebuild -version 2>/dev/null", check=False)
    return f"Build version {XCODE_BUILD}" in out


def clt_ready(h: BuilderHost) -> bool:
    return ssh.run(h.fqdn, "test -x /Library/Developer/CommandLineTools/usr/bin/git", check=False, user=USER).returncode == 0


# --- steps ---------------------------------------------------------------------------------


def resolve(hostname: str) -> BuilderHost:
    hostname = validate_short(hostname)
    if not hostname.startswith("macmini-r8-"):
        raise NotReadyError(f"{hostname} is not an Intel r8 builder (macmini-r8-*)")
    return BuilderHost(hostname=hostname, fqdn=f"{hostname}.test.releng.mdc1.mozilla.com")


def step_preflight(h: BuilderHost) -> str:
    """Read-only. Fills in role/pool/client/task user. Returns the current macOS version.

    Accepts a host that is already on macOS 15 under either role, so a failed run resumes."""
    ui.step("PREFLIGHT", "T2 >= 20P, builder role, stock hiera, no override, pinned task user, disk")
    ver = os_version(h)
    on_target = _ver(ver)[0] == TARGET_MAJOR
    if not on_target and not any(_ver(ver)[: len(src)] == src for src in SOURCE_OS):
        raise NotReadyError(f"{h.hostname} is on macOS {ver}; this tool upgrades builders on 10.15 or 13")
    model = _sh(h, "sysctl -n hw.model")
    if model != "Macmini8,1":
        raise NotReadyError(f"{h.hostname} is a {model}, not a Mac mini 2018 (Macmini8,1)")
    fw = t2_firmware(h)
    if not on_target and t2_major(fw) < MIN_T2_MAJOR:
        raise NotReadyError(
            f"T2 firmware is {fw or 'unknown'}: below {MIN_T2_MAJOR}P the upgrade can't stage the new "
            "bridgeOS and fails (r8-195, r8-139). Needs a DFU revive with Apple Configurator first."
        )
    if not has_passwordless_sudo(h):
        raise NotReadyError(f"{USER} has no passwordless sudo on {h.hostname}")

    h.role = _sh(h, "cat /etc/puppet_role")
    if h.role in ROLE_UPGRADES:
        h.target_role = ROLE_UPGRADES[h.role]
    elif h.role in ROLE_UPGRADES.values():
        h.target_role = h.role  # role already switched by a previous run
    else:
        raise NotReadyError(f"role {h.role!r} is not a 1015 builder role this tool knows")

    if _sh(h, "sudo -n grep -c hiera_vault /etc/puppetlabs/code/environments/production/hiera.yaml", check=False) not in ("", "0"):
        raise NotReadyError("environment hiera.yaml uses hiera_vault (r8-202 had this) -- restore the stock file first")
    override = _sh(h, "sudo -n grep -E '^PUPPET_BRANCH' /opt/puppet_environments/ronin_settings", check=False)
    if override:
        raise NotReadyError(f"ronin branch override in place ({escape(override)}) -- remove it first")
    h.pool, h.client_id = worker_config(h)
    h.task_user = pinned_task_user(h)
    if not h.pool or not h.client_id:
        raise NotReadyError("couldn't read workerPoolID/clientID from /opt/worker/worker-runner-config.yaml")
    if not h.task_user.startswith("task_"):
        raise NotReadyError(f"pinned task user is {h.task_user!r}; expected task_<n> from current-task-user.json")
    al = autologin_user(h)
    if al and al != h.task_user:
        raise NotReadyError(f"autoLoginUser is {al!r} but the pinned task user is {h.task_user!r}")

    free, reclaim = free_gb(h), reclaimable_gb(h)
    if not on_target and free + reclaim < MIN_FREE_GB:
        raise NotReadyError(f"only {free}G free (+{reclaim}G of caches); need {MIN_FREE_GB}G")
    managed = _sh(h, "defaults read '/Library/Managed Preferences/com.apple.SoftwareUpdate' AutomaticallyInstallMacOSUpdates 2>/dev/null", check=False)
    if managed != "0":
        ui.warn("no update-suppression profile: macOS can auto-install updates and wipe /var/tmp (r8-202 sat dead 7h)")
    taskcluster.get_worker(h.pool, WORKER_GROUP, h.hostname)  # raises if TC doesn't know it
    ui.ok(f"macOS {ver}, T2 {fw}, {h.role} -> {h.target_role}, {h.pool}, task user {h.task_user}, {free}G free (+{reclaim}G caches)")
    return ver


def step_quarantine(h: BuilderHost, info: str) -> None:
    until = (datetime.now(timezone.utc) + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    ui.step("QUARANTINE", f"stop scheduling on {h.hostname} in {h.pool}")
    taskcluster.quarantine(h.pool, WORKER_GROUP, h.hostname, until, info)
    ui.ok(f"quarantined until {until[:10]}")


def step_unquarantine(h: BuilderHost) -> None:
    ui.step("UNQUARANTINE", f"back into service in {h.pool}")
    taskcluster.unquarantine(h.pool, WORKER_GROUP, h.hostname)
    ui.ok("in service")


def step_drain(h: BuilderHost, *, poll: int = 15, max_wait: int = 3 * 3600) -> None:
    """Two consecutive idle polls, like workflow.step_drain. Builders run ~100 min debug builds."""
    ui.step("DRAIN", "let the in-flight task finish")
    deadline, idle = time.monotonic() + max_wait, 0
    with ui.waiting("waiting for the worker to go idle") as tick:
        while idle < 2:
            if time.monotonic() > deadline:
                raise ReprovisionError(f"drain exceeded {max_wait // 60} min")
            idle = 0 if taskcluster.is_currently_busy(h.pool, WORKER_GROUP, h.hostname) else idle + 1
            if idle < 2:
                tick("busy" if idle == 0 else "idle, confirming")
                time.sleep(poll)
    ui.ok("idle")


def step_stop_worker(h: BuilderHost, *, logout_wait: int = 60) -> None:
    """Stop worker-runner and log the task user out of the console.

    Autologin leaves the pinned task user holding the console (its loginwindow + Aqua session),
    which gets in the installer's way. Autologin re-fires after every logout, not just at boot,
    so kcpassword moves aside FIRST -- otherwise the bootout succeeds and the task user is back
    in seconds (r8-368). autoLoginUser is left alone; step_return moves kcpassword back.
    """
    ui.step("STOP WORKER", "remove the semaphore, stop worker-runner, log out the console task user")
    _sh(h, f"sudo rm -f {SEMAPHORE}")
    _sh(h, "sudo launchctl bootout system/org.mozilla.worker-runner 2>/dev/null; true", check=False)
    _sh(h, f"if [ -e {KCPASSWORD} ]; then sudo mv {KCPASSWORD} {KCPASSWORD_ASIDE}; fi")
    who = console_user(h)
    if who.startswith("task_"):
        _sh(h, f"sudo launchctl bootout gui/$(id -u {shlex.quote(who)})", check=False)
        deadline = time.monotonic() + logout_wait
        while console_user(h).startswith("task_") and time.monotonic() < deadline:
            time.sleep(4)
        who = console_user(h)
        if who.startswith("task_"):
            raise ReprovisionError(f"{who} is still logged in at the console after the logout")
    ui.ok(f"worker stopped, kcpassword set aside, console={who or '?'}")


def step_clear_caches(h: BuilderHost) -> None:
    """As root: /opt/worker/cache is 0700, so a bare `sudo rm` glob expands as relops and
    deletes nothing. Also stale 2020-era cltbld caches (22G on r8-195), a leftover installer
    staging dir, and the Xcode the previous run moved aside."""
    ui.step("CLEAR CACHES", "worker cache + tasks, stale caches, leftover installer data")
    before = free_gb(h)
    # Deleting ~20G of hg stores outlasts ssh.run's 120s default (r8-138, 2026-10-22): the rm
    # finished on the host but the tool had already given up.
    _sh(h, "sudo sh -c 'rm -rf /opt/worker/cache/* /opt/worker/tasks/* && echo {} > /opt/worker/directory-caches.json'", timeout=1800)
    _sh(h, "sudo rm -rf /Users/cltbld/caches '/System/Volumes/Data/macOS Install Data' /Applications/Xcode.app.pre-*", check=False, timeout=1800)
    ui.ok(f"{before}G -> {free_gb(h)}G free")


def step_os_update(h: BuilderHost, *, down_timeout: int = 45 * 60, up_timeout: int = 2 * 3600, poll: int = 30) -> str:
    """Fetch the full installer, run startosinstall detached, wait for it to come back on 15.

    The CLI installer works hands-off on 20P+ T2s (r8-368, 2026-10-19). Back on 10.15 means
    the T2 firmware didn't take -- check /Library/Logs/DiagnosticReports/BridgeRestore_*.
    """
    cur = os_version(h)
    ui.step("OS UPDATE", f"macOS {cur} -> {TARGET_OS} with startosinstall")
    if _ver(cur)[0] == TARGET_MAJOR:
        ui.ok(f"already on {cur} -- skipping")
        return cur
    if not ssh.file_exists(h.fqdn, f"{INSTALLER}/Contents/Resources/startosinstall", user=USER):
        ui.wire(f"softwareupdate --fetch-full-installer --full-installer-version {TARGET_OS}")
        _sh(h, f"sudo softwareupdate --fetch-full-installer --full-installer-version {TARGET_OS}", timeout=3600)
    # The redirect must run as root: `sudo nohup ... > /var/log/x` opens the log as relops.
    ui.wire("startosinstall --agreetolicense --nointeraction --forcequitapps (detached)")
    _sh(
        h,
        f"sudo sh -c 'nohup \"{INSTALLER}/Contents/Resources/startosinstall\" --agreetolicense --nointeraction "
        "--forcequitapps > /var/log/startosinstall.log 2>&1 &'",
    )
    deadline = time.monotonic() + down_timeout
    with ui.waiting("installer preparing (~15 min), then it reboots", eta_seconds=900) as tick:
        while _reachable(h):
            if time.monotonic() > deadline:
                tail = _sh(h, "sudo tail -3 /var/log/startosinstall.log", check=False)
                raise ReprovisionError(f"installer didn't reboot within {down_timeout // 60} min: {escape(tail)}")
            try:
                tick(_sh(h, "sudo tail -c 40 /var/log/startosinstall.log | tr '\\r' '\\n' | tail -1", check=False) or "preparing")
            except ReprovisionError:
                tick("rebooting")
            time.sleep(poll)
    deadline = time.monotonic() + up_timeout
    with ui.waiting("installing; the host reboots a few times", eta_seconds=1800) as tick:
        while True:
            if time.monotonic() > deadline:
                raise ReprovisionError(f"no ssh {up_timeout // 60} min after the installer rebooted -- check the KVM")
            if _reachable(h):
                v = os_version(h)
                if _ver(v)[0] == TARGET_MAJOR:
                    break
                raise ReprovisionError(
                    f"came back on macOS {v}: the upgrade didn't take. Usually the T2 firmware -- "
                    "see /Library/Logs/DiagnosticReports/BridgeRestore_* on the host."
                )
            tick("installing")
            time.sleep(poll)
    ssh.wait_for_sshd(h.fqdn, timeout=900)
    ui.ok(f"now on macOS {v}, T2 {t2_firmware(h)}")
    return v


def step_ensure_sudo(h: BuilderHost, *, wait: int = 15 * 60, poll: int = 30) -> None:
    """The upgrade resets /etc/sudoers to Apple's stock file, so relops loses NOPASSWD.

    Puppet puts it back, but puppet needs sudo. Wait for the SimpleMDM sudo pkg; else, if a
    password is configured, drop a NOPASSWD file in /etc/sudoers.d (puppet purges that dir on
    its first run, so it cleans up after itself). The password goes over ssh stdin, never argv.
    """
    ui.step("SUDO", "passwordless sudo for relops after the upgrade")
    if has_passwordless_sudo(h):
        ui.ok("already passwordless")
        return
    pw = builder_sudo_password()
    if pw:
        ui.wire("sudo -S: temporary /etc/sudoers.d/zz-builder-upgrade (puppet removes it)")
        _sh(
            h,
            "sudo -S -p '' sh -c \"echo 'relops ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/zz-builder-upgrade "
            '&& chmod 0440 /etc/sudoers.d/zz-builder-upgrade"',
            stdin=(pw + "\n").encode(),
        )
    else:
        deadline = time.monotonic() + wait
        with ui.waiting("waiting for the SimpleMDM sudo pkg") as tick:
            while not has_passwordless_sudo(h):
                if time.monotonic() > deadline:
                    raise NotReadyError(
                        "relops still needs a password for sudo. Add the host to the SimpleMDM group with the "
                        "sudo pkg, or set REPROVISION_BUILDER_SUDO_PASSWORD(_REF), then re-run (it resumes)."
                    )
                tick("not yet")
                time.sleep(poll)
    if not has_passwordless_sudo(h):
        raise ReprovisionError("sudo is still not passwordless")
    ui.ok("passwordless")


def step_install_xcode(h: BuilderHost) -> None:
    """Before puppet: with Xcode 12.x still selected, /usr/bin/git crashes in xcodebuild's
    plugin loader on macOS 15, and run-puppet.sh dies silently at its first git command."""
    ui.step("XCODE", f"Xcode {XCODE_BUILD} from the ronin package bucket (signature + build checked)")
    if xcode_ready(h):
        ui.ok(f"Xcode {XCODE_BUILD} already selected -- skipping")
        return
    script = (resources.files("orchestrator") / "data" / "install-xcode.sh").read_bytes()
    ssh.write_file_as_root(h.fqdn, XCODE_REMOTE, script, mode="0700", user=USER)
    ui.wire(f"sudo bash {XCODE_REMOTE} <xip> {XCODE_BUILD}  (~3G download, ~15 min expand)")
    out = _sh(h, f"sudo bash {XCODE_REMOTE} {shlex.quote(XCODE_URL)} {XCODE_BUILD} 2>&1", timeout=3600)
    if not xcode_ready(h):
        raise ReprovisionError(f"Xcode {XCODE_BUILD} not selected after install: {escape(out[-300:])}")
    _sh(h, f"sudo rm -f {XCODE_REMOTE}", check=False)
    ui.ok(f"Xcode {XCODE_BUILD} selected")


def step_install_clt(h: BuilderHost) -> None:
    """The upgrade leaves a CommandLineTools stub with no usr/bin/git. Done here because the
    download outlasts puppet's old 300s exec timeout (ronin #1470 raises it)."""
    ui.step("CLT", CLT_LABEL)
    if clt_ready(h):
        ui.ok("present -- skipping")
        return
    _sh(h, f"sudo touch {CLT_TRIGGER}")
    try:
        _sh(h, f"sudo softwareupdate -i {shlex.quote(CLT_LABEL)} --verbose", timeout=3600)
    finally:
        _sh(h, f"sudo rm -f {CLT_TRIGGER}", check=False)
    if not clt_ready(h):
        raise ReprovisionError("Command Line Tools still missing usr/bin/git after the install")
    ui.ok("installed")


def _puppet_run(h: BuilderHost, log: str) -> str:
    # run-puppet.sh retries forever on ^Error:; cap it. Its output goes to a log on the host
    # so a dropped ssh session can't kill the run.
    _sh(h, f"sudo sh -c \"perl -e 'alarm 900; exec @ARGV' /usr/local/bin/run-puppet.sh > {log} 2>&1\"", check=False, timeout=1200)
    return _sh(h, f"sudo cat {log}", check=False)


def step_puppet(h: BuilderHost) -> None:
    """Switch the role and converge twice. The first run restores sudoers and rewrites the
    worker config for the new role; the second must compile for the FQDN and change nothing
    but the always-run mercurial exec. An empty catalog (r8-270's stale certname) or a run
    that dies before compiling both fail here instead of reporting success."""
    ui.step("PUPPET", f"/etc/puppet_role -> {h.target_role}, then run-puppet.sh twice")
    _sh(h, f"echo {shlex.quote(h.target_role)} | sudo tee /etc/puppet_role >/dev/null")
    for n in (1, 2):
        out = _puppet_run(h, f"/tmp/builder-upgrade-puppet{n}.log")
        if f"Compiled catalog for {h.fqdn}" not in out:
            raise ReprovisionError(f"puppet run {n} never compiled a catalog for {h.fqdn}: {escape(out[-300:])}")
        errors = [line for line in out.splitlines() if line.startswith("Error:")]
        if errors:
            raise ReprovisionError(f"puppet run {n}: {escape(errors[0][:250])}")
        changed = [line for line in out.splitlines() if line.startswith("Notice: /Stage")]
        if n == 2:
            extra = [line for line in changed if not any(a in line for a in ALWAYS_RUNS)]
            if extra:
                ui.warn(f"second run still changed {len(extra)}: {escape(extra[0][:200])}")
        ui.ok(f"run {n}: {len(changed)} change(s)")
    meta = _sh(h, "sudo cat /opt/puppet_environments/last_run_metadata.json", check=False)
    if '"success": true' not in meta or f'"role": "{h.target_role}"' not in meta:
        raise ReprovisionError("last_run_metadata.json doesn't show a successful run of the new role")


def step_verify(h: BuilderHost) -> None:
    ui.step("VERIFY", "sudo, worker config, Xcode, CLT, autologin")
    problems = []
    if not has_passwordless_sudo(h):
        problems.append("relops sudo needs a password")
    pool, client = worker_config(h)
    if (pool, client) != (h.pool, h.client_id):
        problems.append(f"worker config is {pool}/{client}, expected {h.pool}/{h.client_id}")
    if not xcode_ready(h):
        problems.append(f"Xcode {XCODE_BUILD} not selected")
    if not clt_ready(h):
        problems.append("CommandLineTools git missing")
    if autologin_user(h) != h.task_user:
        problems.append(f"autoLoginUser is not {h.task_user}")
    # The password never leaves the host.
    auth = _sh(
        h,
        "U=$(sudo -n /opt/puppetlabs/puppet/bin/ruby -rjson -e 'puts JSON.parse(File.read(\"/opt/worker/current-task-user.json\"))[\"name\"]'); "
        "P=$(sudo -n /opt/puppetlabs/puppet/bin/ruby -rjson -e 'print JSON.parse(File.read(\"/opt/worker/current-task-user.json\"))[\"password\"]'); "
        'dscl . -authonly "$U" "$P" >/dev/null 2>&1 && echo valid || echo invalid',
        check=False,
    )
    if auth != "valid":
        problems.append("task user's password doesn't authenticate")
    if problems:
        raise ReprovisionError("; ".join(problems))
    ui.ok(f"{pool}, {client}, Xcode {XCODE_BUILD}, CLT, autologin {h.task_user}")


def step_return(h: BuilderHost, *, reboot_timeout: int = 1800, register_timeout: int = 1200, poll: int = 20) -> None:
    """kcpassword back, semaphore, reboot, wait for autologin + Taskcluster.

    kcpassword first: nothing on a builder rewrites it (generic-worker's early return skips
    autologin setup; puppet's autologin_user is only in tester/tart profiles). Without it the
    host boots to the login window, gw loops on 'Error checking for interactive user', exits 69,
    and worker-runner reboots it every ~6 min (r8-202, Sept 2026)."""
    ui.step("RETURN", "restore autologin, touch the semaphore, reboot, wait for the worker")
    _sh(h, f"if [ -e {KCPASSWORD_ASIDE} ]; then sudo mv {KCPASSWORD_ASIDE} {KCPASSWORD}; fi")
    if not ssh.file_exists(h.fqdn, KCPASSWORD, user=USER):
        raise ReprovisionError(f"{KCPASSWORD} is missing -- autologin would fail and gw would loop on exit 69")
    _sh(h, f"sudo mkdir -p {SEMAPHORE.rsplit('/', 1)[0]} && sudo touch {SEMAPHORE}")
    before = boot_time(h)
    ssh.run(h.fqdn, "sudo shutdown -r now", check=False, user=USER)
    deadline = time.monotonic() + reboot_timeout
    with ui.waiting(f"waiting for the reboot and {h.task_user}'s autologin") as tick:
        while True:
            time.sleep(poll)
            if time.monotonic() > deadline:
                raise ReprovisionError(f"{h.task_user} not logged in {reboot_timeout // 60} min after the reboot")
            try:
                if boot_time(h) <= before:
                    tick("not rebooted yet")
                    continue
                who = console_user(h)
            except ReprovisionError:
                tick("unreachable")
                continue
            if who == h.task_user:
                break
            tick(f"console={who or '?'}")
    since = datetime.fromtimestamp(boot_time(h), timezone.utc).isoformat()
    deadline = time.monotonic() + register_timeout
    while (taskcluster.get_worker(h.pool, WORKER_GROUP, h.hostname).get("lastDateActive") or "") <= since:
        if time.monotonic() > deadline:
            raise ReprovisionError("worker hasn't checked in with Taskcluster since the reboot")
        time.sleep(30)
    ui.ok(f"back: console={h.task_user}, worker active in {h.pool}")


def already_upgraded(h: BuilderHost, ver: str) -> bool:
    """On 15, new role, Xcode + CLT in place, and the worker running (semaphore present)."""
    return (
        _ver(ver)[0] == TARGET_MAJOR
        and h.role == h.target_role
        and xcode_ready(h)
        and clt_ready(h)
        and ssh.file_exists(h.fqdn, SEMAPHORE, user=USER)
    )


# --- the whole thing -----------------------------------------------------------------------


def upgrade(hostname: str, *, unquarantine: bool = False, dry_run: bool = False, job: str = "") -> None:
    h = resolve(hostname)
    # Resuming after the OS step: the upgrade reset sudoers, and preflight needs sudo to read
    # the worker config. The host is still quarantined from the run that got it this far.
    if not dry_run and _ver(os_version(h))[0] == TARGET_MAJOR and not has_passwordless_sudo(h):
        step_ensure_sudo(h)
    ver = step_preflight(h)
    ui.banner(h.hostname, f"{h.role} -> {h.target_role}", h.pool)
    if already_upgraded(h, ver):
        # Nothing to do, and a run would quarantine and reboot a working prod host.
        ui.ok(f"builder-upgrade: {h.hostname} is already on macOS {ver} as {h.target_role} and in service -- nothing to do")
        return
    if dry_run:
        ui.ok(f"builder-upgrade dry run: {h.hostname} is eligible")
        return
    step_quarantine(h, info=f"builder-upgrade {job}".strip())
    try:
        step_drain(h)
        step_stop_worker(h)
        step_clear_caches(h)
        step_os_update(h)
        step_ensure_sudo(h)
        step_install_xcode(h)
        step_install_clt(h)
        step_puppet(h)
        step_verify(h)
        step_return(h)
    except Exception:
        ui.err(f"failed -- {h.hostname} left QUARANTINED; re-run builder-upgrade to resume")
        raise
    if unquarantine:
        step_unquarantine(h)
    # Last line on purpose: the runner reports a job's final stdout line as its Hangar detail.
    ui.ok(
        f"builder-upgrade complete: {h.hostname} on macOS {TARGET_OS} as {h.target_role}, "
        + ("back in service" if unquarantine else "still QUARANTINED -- unquarantine when ready")
    )
