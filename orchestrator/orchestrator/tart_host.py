"""
Maintenance for tart VM hosts (gecko-t-osx-1500-m-vms): drain, macOS minor update via
SimpleMDM, puppet apply, reboot, verify, back into service -- one command.

Separate from workflow.py on purpose. That module's resolve()/quarantine/drain assume a
bare-metal worker whose TC workerId is the hostname, and role_map maps every macmini-m4-*
to the bare-metal role. A tart host is different: the TC workers are its VMs (`mac-` + the
last three octets of each guest's MAC), in a different pool. Rather than generalise the
bare-metal steps -- which guard real wipes (incident 2026-07-10) -- this module carries its
own small context and calls the same client functions.

Every failure after the quarantine leaves the workers QUARANTINED. A host is never left
half-done and taking tasks.
"""

from __future__ import annotations

import re
import shlex
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from rich.markup import escape

from . import ui
from .clients import simplemdm, ssh, taskcluster
from .errors import NotReadyError, ReprovisionError
from .hostnames import validate_short

POOL = "releng-hardware/gecko-t-osx-1500-m-vms"
WORKER_GROUP = "mdc1"
PUPPET_ROLE = "tart_worker"
RONIN = "/opt/puppet_environments/mozilla-platform-ops/ronin_puppet"
REGISTRY_ALLOW = "10.49.56.161/32"
TART = "/usr/local/bin/tart"
LOGS = "/Users/admin/Library/Logs"

# Hosts this tool refuses to touch, with the reason. Applying master to them would silently
# undo something deliberate. Edit this list when the reason goes away.
EXCLUDED = {
    "macmini-m4-117": "1920x1080 tester-image canary (pinned to ronin branch tart-candidate-1920x1080)",
    "macmini-m4-189": "1920x1080 tester-image canary (pinned to ronin branch tart-candidate-1920x1080)",
}

# The tester GUESTS must stay on 15.3; the HOSTS may move within macOS 15. A host that lands
# on another major is untested with the 15.3 guests and is left quarantined.
HOST_MAJOR = 15


@dataclass
class TartHost:
    hostname: str
    fqdn: str
    worker_ids: list[str] = field(default_factory=list)  # one per slot, sorted by slot name
    simplemdm_device_id: int | None = None


def _ver(v: str) -> tuple[int, ...]:
    """'15.8.1' -> (15, 8, 1). Non-numeric junk -> (0,), so it never compares as new enough."""
    try:
        return tuple(int(p) for p in v.strip().split("."))
    except ValueError:
        return (0,)


def _out(cp) -> str:
    return cp.stdout.decode(errors="replace").strip()


def _sh(h: TartHost, cmd: str, *, check: bool = True, timeout: int | None = None) -> str:
    return _out(ssh.run(h.fqdn, cmd, check=check, timeout=timeout))


# --- reads ---------------------------------------------------------------------------------


_MAC_RE = re.compile(r'"macAddress"\s*:\s*"([0-9a-fA-F:]{17})"')


def worker_id_for_mac(mac: str) -> str:
    """tart guest MAC -> TC workerId. The guest's set_hostname.sh derives it the same way."""
    return "mac-" + "".join(mac.lower().split(":")[3:])


def read_worker_ids(h: TartHost) -> list[str]:
    """One workerId per sequoia-tester-N slot, read from each VM's config.json."""
    out = _sh(
        h,
        "for d in /Users/admin/.tart/vms/sequoia-tester-*; do "
        'echo "$(basename "$d") $(tr -d \'\\n\' < "$d/config.json" 2>/dev/null)"; done',
    )
    ids = []
    for line in sorted(out.splitlines()):
        m = _MAC_RE.search(line)
        if m:
            ids.append(worker_id_for_mac(m.group(1)))
    return ids


def os_version(h: TartHost) -> str:
    return _sh(h, "sw_vers -productVersion")


def boot_time(h: TartHost) -> int:
    return int(_sh(h, "sysctl -n kern.boottime | cut -d' ' -f4 | tr -d ,") or 0)


def running_vms(h: TartHost) -> int:
    out = _sh(h, f"sudo -u admin -H {TART} list | awk '$1==\"local\" && $NF==\"running\"' | wc -l", check=False)
    return int(out or 0)


def allowlist(h: TartHost) -> str:
    return _sh(
        h,
        "sudo defaults read com.apple.network.local-network AllowedEthernetLocalNetworkAddresses 2>/dev/null"
        " | tr -d ' \\n\"()'",
        check=False,
    )


def local_network_denials_since_boot(h: TartHost) -> int:
    out = _sh(
        h,
        "sudo log show --last boot --style compact --predicate "
        "'process == \"tart\" AND eventMessage CONTAINS \"Local network prohibited\"' 2>/dev/null"
        " | grep -c prohibited",
        check=False,
        timeout=600,
    )
    return int(out or 0)


def slot_pulls_since(h: TartHost, since_iso: str) -> dict[str, tuple[int, int]]:
    """Per tartworker log: (reverts, failed pulls) with a timestamp strictly after since_iso."""
    out = _sh(
        h,
        f"for f in {LOGS}/tartworker-*.out; do "
        f"r=$(awk 'substr($0,1,19)>\"{since_iso}\"' \"$f\" | grep -c 'reverted to fresh'); "
        f"p=$(awk 'substr($0,1,19)>\"{since_iso}\"' \"$f\" | grep -c 'pull .* failed'); "
        'echo "$(basename "$f") $r $p"; done',
        check=False,
    )
    res = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3:
            res[parts[0]] = (int(parts[1]), int(parts[2]))
    return res


def _fmt_pulls(pulls: dict) -> str:
    """{'tartworker-1.out': (2, 1)} -> 'slot 1: 2 reverts, 1 failed pull' -- no brackets.

    Values may be (reverts, failed) tuples or a bare failed-pull count."""
    parts = []
    for name, val in pulls.items():
        r, p = val if isinstance(val, tuple) else (None, val)
        slot = name.replace("tartworker-", "slot ").replace(".out", "")
        bits = ([f"{r} revert{'s' * (r != 1)}"] if r is not None else []) + [f"{p} failed pull{'s' * (p != 1)}"]
        parts.append(f"{slot}: {', '.join(bits)}")
    return "; ".join(parts) or "no slots"


def _fmt_reverts(pulls: dict) -> str:
    """Success-path summary that never contains the word 'failed'."""
    return ", ".join(
        f"{n.replace('tartworker-', 'slot ').replace('.out', '')} {r} revert{'s' * (r != 1)}"
        for n, (r, _) in pulls.items()
    ) or "no slots"


# --- steps ---------------------------------------------------------------------------------


def resolve(hostname: str) -> TartHost:
    hostname = validate_short(hostname)
    if hostname in EXCLUDED:
        raise NotReadyError(f"{hostname} is excluded from tart-host-update: {EXCLUDED[hostname]}")
    return TartHost(hostname=hostname, fqdn=f"{hostname}.test.releng.mdc1.mozilla.com")


def step_preflight(h: TartHost, *, min_free_gb: int = 40) -> str:
    """Read-only. Returns the current macOS version."""
    ui.step("PREFLIGHT", "macOS 15, admin console + SecureToken, Bootstrap Token escrowed, disk, VMs")
    ver = os_version(h)
    if _ver(ver)[0] != HOST_MAJOR:
        raise NotReadyError(f"{h.hostname} is on macOS {ver}; this tool only handles macOS {HOST_MAJOR} hosts")
    console = _sh(h, "stat -f %Su /dev/console")
    if console != "admin":
        raise NotReadyError(f"console is '{escape(console)}', not admin -- tart needs admin's GUI session")
    if "ENABLED" not in ssh.secure_token_status(h.fqdn):
        raise NotReadyError("admin has no SecureToken -- a macOS update would fail")
    bst = _sh(h, "sudo profiles status -type bootstraptoken", check=False)
    if "escrowed to server: YES" not in bst:
        raise NotReadyError("Bootstrap Token not escrowed -- run `reprovision escrow-bst` first")
    free = int(_sh(h, "df -g / | awk 'NR==2{print $4}'") or 0)
    if free < min_free_gb:
        raise NotReadyError(f"only {free}G free (need {min_free_gb}G for the update)")
    dirty = _sh(h, f"sudo git -C {RONIN} status --porcelain", check=False)
    if dirty:
        raise NotReadyError(f"uncommitted changes in the on-host checkout {RONIN}")
    ui.ok(f"macOS {ver}, console=admin, SecureToken + BST ok, {free}G free, {running_vms(h)} VMs running")
    return ver


def step_resolve_mdm_device(h: TartHost) -> None:
    """Find the SimpleMDM device and prove it is THIS box (hardware serial), before any change.

    Name lookup has picked the wrong device before; the serial is the only reliable join key.
    Done up front so a missing or mismatched record fails the run before the quarantine.
    """
    ui.step("MDM DEVICE", "find the SimpleMDM record and match its serial to the host")
    device = simplemdm.find_device_by_name(h.hostname)
    if not device:
        raise NotReadyError(f"no SimpleMDM device named {h.hostname}")
    mdm_serial = device.get("attributes", {}).get("serial_number", "")
    box_serial = ssh.platform_serial(h.fqdn)
    if not box_serial or mdm_serial != box_serial:
        raise NotReadyError(
            f"serial mismatch: SimpleMDM device {device['id']} has {mdm_serial!r}, the host reports {box_serial!r}"
        )
    h.simplemdm_device_id = int(device["id"])
    ui.ok(f"device {h.simplemdm_device_id}, serial {box_serial}")


def step_resolve_workers(h: TartHost) -> None:
    ui.step("WORKERS", "map each VM slot to its Taskcluster workerId and check TC knows it")
    ids = read_worker_ids(h)
    if not ids:
        raise ReprovisionError("no sequoia-tester-* VMs with a MAC found -- is this a tart host?")
    for wid in ids:
        taskcluster.get_worker(POOL, WORKER_GROUP, wid)  # raises on 404: stale MAC sidecar etc.
    h.worker_ids = ids
    ui.ok(", ".join(ids))


def step_quarantine(h: TartHost, info: str) -> None:
    until = (datetime.now(timezone.utc) + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    ui.step("QUARANTINE", f"stop scheduling on {', '.join(h.worker_ids)}")
    for wid in h.worker_ids:
        taskcluster.quarantine(POOL, WORKER_GROUP, wid, until, info)
    ui.ok(f"quarantined until {until[:10]}")


def step_unquarantine(h: TartHost) -> None:
    ui.step("UNQUARANTINE", f"back into service: {', '.join(h.worker_ids)}")
    for wid in h.worker_ids:
        taskcluster.unquarantine(POOL, WORKER_GROUP, wid)
    ui.ok("in service")


def step_drain(h: TartHost, *, poll: int = 15, max_wait: int = 3600) -> None:
    """Same rule as workflow.step_drain (2 consecutive idle polls), per VM worker."""
    ui.step("DRAIN", "let each VM finish its in-flight task")
    deadline = time.monotonic() + max_wait
    for wid in h.worker_ids:
        idle = 0
        while idle < 2:
            if time.monotonic() > deadline:
                raise ReprovisionError(f"drain of {wid} exceeded {max_wait}s")
            if taskcluster.is_currently_busy(POOL, WORKER_GROUP, wid):
                idle = 0
            else:
                idle += 1
            if idle < 2:
                time.sleep(poll)
        ui.ok(f"{wid} idle")


def step_os_update(h: TartHost, *, min_os: str, timeout: int = 3 * 3600, poll: int = 60) -> str:
    """Minor-only macOS update via SimpleMDM, then wait for the host to come back on it.

    Skipped if the host is already at or past min_os. Returns the version it ends on.
    """
    cur = os_version(h)
    ui.step("OS UPDATE", f"SimpleMDM update_os, latest MINOR version (now {cur}, need >= {min_os})")
    if _ver(cur) >= _ver(min_os):
        ui.ok(f"already on {cur} -- skipping")
        return cur
    # Re-check right before sending, even though update() already did it in preflight: this step
    # can be called on its own, and the cost is one API call.
    step_resolve_mdm_device(h)
    ui.wire(f"POST devices/{h.simplemdm_device_id}/update_os force_update latest_minor_version")
    simplemdm.update_os(h.simplemdm_device_id, os_update_mode="force_update", version_type="latest_minor_version")

    deadline = time.monotonic() + timeout
    with ui.waiting("waiting for the update to install and the host to come back", eta_seconds=3600) as tick:
        while time.monotonic() < deadline:
            cp = ssh.run(h.fqdn, "sw_vers -productVersion", check=False)
            v = _out(cp) if cp.returncode == 0 else ""
            if v and v != cur:
                break
            tick(f"still {v or 'unreachable'}")
            time.sleep(poll)
        else:
            raise ReprovisionError(f"still on {cur} after {timeout // 60} min -- check the device in SimpleMDM")
    if _ver(v)[0] != HOST_MAJOR:
        raise ReprovisionError(f"host came back on macOS {v} -- NOT macOS {HOST_MAJOR}; leaving it quarantined")
    if _ver(v) < _ver(min_os):
        raise ReprovisionError(f"host came back on {v}, below the required {min_os}")
    ssh.wait_for_sshd(h.fqdn, timeout=900)
    ui.ok(f"now on macOS {v}")
    return v


def step_puppet_apply(h: TartHost, *, branch: str) -> bool:
    """Apply tart_worker from `branch`. Returns True if the host needs a reboot."""
    ui.step("PUPPET", f"apply {PUPPET_ROLE} from ronin_puppet {branch}")
    b = shlex.quote(branch)
    _sh(h, f"sudo git -C {RONIN} fetch --quiet origin {b} && sudo git -C {RONIN} checkout --quiet -B {b} origin/{b}")
    # Everything printed from the host is escaped: rich reads [..] as markup, and puppet output
    # is full of brackets (File[/var/sbom] crashed the first real run on m4-249, 2026-10-16).
    ui.ok(escape(_sh(h, f"sudo git -C {RONIN} log -1 --format='checkout %h %s'")))
    cp = ssh.run(
        h.fqdn,
        f"cd {RONIN} && sudo FACTER_puppet_role={PUPPET_ROLE} /opt/puppetlabs/bin/puppet apply --detailed-exitcodes "
        f"--modulepath=./modules:./r10k_modules --hiera_config=./hiera.yaml -e 'include roles_profiles::roles::{PUPPET_ROLE}'",
        check=False,
        timeout=1800,
    )
    out = _out(cp)
    for line in out.splitlines():
        if "Notice: /Stage" in line or "Error" in line:
            ui.info(escape(line[:200]))
    if cp.returncode not in (0, 2):  # --detailed-exitcodes: 2 = changes applied
        raise ReprovisionError(f"puppet apply failed (exit {cp.returncode})")
    if allowlist(h) != REGISTRY_ALLOW:
        raise ReprovisionError(f"Local Network allowlist is {allowlist(h)!r}, expected {REGISTRY_ALLOW!r}")
    ui.ok(f"applied (exit {cp.returncode}); allowlist {REGISTRY_ALLOW}")
    return "REBOOT REQUIRED" in out


def step_reboot_healthy(h: TartHost, *, timeout: int = 1800, poll: int = 20) -> int:
    """Reboot and wait for: new boot, admin console, every slot running, same workerIds.

    Returns the new boot time (epoch seconds).
    """
    ui.step("REBOOT", "restart, then wait for admin's session and every VM")
    before = boot_time(h)
    expected = len(h.worker_ids)
    ssh.run(h.fqdn, "sudo shutdown -r now", check=False)
    deadline = time.monotonic() + timeout
    with ui.waiting("waiting for the host and its VMs") as tick:
        while time.monotonic() < deadline:
            time.sleep(poll)
            try:
                bt = boot_time(h)
                if bt <= before:
                    tick("not rebooted yet")
                    continue
                console = _sh(h, "stat -f %Su /dev/console", check=False)
                vms = running_vms(h)
            except ReprovisionError:
                tick("unreachable")
                continue
            if console == "admin" and vms >= expected:
                break
            tick(f"console={console or '?'} vms={vms}/{expected}")
        else:
            raise ReprovisionError(
                f"not healthy {timeout // 60} min after reboot -- check {LOGS}/tartworker-*.err "
                "(VZError loops = no GUI session; see the autologin notes)"
            )
    ids = read_worker_ids(h)
    if ids != h.worker_ids:
        raise ReprovisionError(
            f"workerIds changed across the reboot: {', '.join(h.worker_ids)} -> {', '.join(ids) or 'none'}"
        )
    ui.ok(f"back: console=admin, {expected} VMs running, workerIds unchanged")
    return bt


def step_wait_registered(h: TartHost, *, since_epoch: int, timeout: int = 1200, poll: int = 30) -> None:
    """Each VM worker has checked in with TC since the reboot (quarantined workers still poll)."""
    ui.step("REGISTER", "wait for each VM worker to check in with Taskcluster")
    since = datetime.fromtimestamp(since_epoch, timezone.utc).isoformat()
    deadline = time.monotonic() + timeout
    pending = list(h.worker_ids)
    while pending and time.monotonic() < deadline:
        pending = [
            w for w in pending
            if (taskcluster.get_worker(POOL, WORKER_GROUP, w).get("lastDateActive") or "") <= since
        ]
        if pending:
            time.sleep(poll)
    if pending:
        raise ReprovisionError(f"not checked in since the reboot: {', '.join(pending)}")
    ui.ok("all VM workers active")


def step_verify_pulls(h: TartHost, *, boot_epoch: int, timeout: int = 7200, poll: int = 120) -> bool:
    """After un-quarantining: every slot reverts at least once and no pull fails.

    Pulls in the first ~3 minutes after boot are ignored -- the network isn't up yet and the
    first pull fails with 'No network route' on every host, allowlist or not.

    Returns True if verified. Raises if a pull fails or Local Network denials appear (the caller
    re-quarantines). Returns False if the pool was too quiet to produce evidence in time: the
    host is healthy by every other check, so it stays in service, flagged UNVERIFIED.
    """
    ui.step("VERIFY", "each slot reverts after a task and pulls from the registry without failing")
    since = datetime.fromtimestamp(boot_epoch + 180, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    deadline = time.monotonic() + timeout
    with ui.waiting("waiting for every slot to finish a task") as tick:
        while True:
            pulls = slot_pulls_since(h, since)
            failed = {s: p for s, (r, p) in pulls.items() if p}
            if failed:
                raise ReprovisionError(f"pull failures after the update: {_fmt_pulls(failed)}")
            denials = local_network_denials_since_boot(h)
            if denials:
                raise ReprovisionError(f"{denials} 'Local network prohibited' events for tart since boot")
            done = [s for s, (r, _) in pulls.items() if r >= 1]
            if pulls and len(done) == len(pulls):
                ui.ok(f"verified: all pulls clean, 0 Local Network denials ({_fmt_reverts(pulls)})")
                return True
            if time.monotonic() > deadline:
                ui.warn(f"UNVERIFIED after {timeout // 60} min -- pool too quiet; so far: {_fmt_pulls(pulls)}")
                return False
            tick(f"{len(done)}/{len(pulls)} slots have reverted since boot")
            time.sleep(poll)


# --- the whole thing -----------------------------------------------------------------------


def update(hostname: str, *, branch: str = "master", min_os: str = "15.8", puppet_only: bool = False,
           job: str = "") -> None:
    h = resolve(hostname)
    ui.banner(h.hostname, PUPPET_ROLE, POOL)
    step_preflight(h)
    if not puppet_only:
        step_resolve_mdm_device(h)
    step_resolve_workers(h)
    step_quarantine(h, info=f"tart-host-update {job}".strip())
    try:
        step_drain(h)
        if not puppet_only:
            step_os_update(h, min_os=min_os)
        needs_reboot = step_puppet_apply(h, branch=branch)
        if needs_reboot or not puppet_only:
            boot = step_reboot_healthy(h)
        else:
            boot = boot_time(h)
        step_wait_registered(h, since_epoch=boot)
    except Exception:
        ui.err(f"failed -- {', '.join(h.worker_ids)} left QUARANTINED")
        raise
    step_unquarantine(h)
    try:
        verified = step_verify_pulls(h, boot_epoch=boot)
    except Exception:
        step_quarantine(h, info=f"tart-host-update {job} verify failed".strip())
        ui.err(f"verify failed -- {', '.join(h.worker_ids)} re-QUARANTINED")
        raise
    # Last line on purpose: the runner reports a job's final stdout line as its Hangar detail.
    ui.ok(
        f"tart-host-update complete: {h.hostname} back in service, "
        + ("pulls verified" if verified else "pulls UNVERIFIED (pool too quiet)")
    )
