"""
The end-to-end EACS workflow as a single function. CLI subcommands call individual
steps for partial-failure recovery; `reprovision` calls them all in order.

The steps are deliberately small functions with clear in/out contracts so they're
re-runnable. If step N fails, the operator can resume by calling step N+1 directly
once they've fixed whatever was broken.
"""

from __future__ import annotations

import shlex
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import ui
from .clients import simplemdm, ssh, taskcluster
from .config import get_settings
from .errors import NotReadyError, ReprovisionError
from .hostnames import validate_short
from .role_map import role_for_hostname
from .secrets import simplemdm_api_key, ssh_admin_key, ssh_admin_password, tc_credentials


@dataclass
class HostContext:
    hostname: str  # short name, e.g. macmini-m4-81
    fqdn: str  # full DNS name to ssh to
    role: str  # puppet role
    worker_pool_id: str  # e.g. releng-hardware/gecko-t-osx-1500-m4
    worker_group: str = "mdc1"
    simplemdm_device_id: int | None = None
    pre_wipe_enrolled_at: str | None = None  # captured by step_wipe; used to detect a *fresh* re-enroll
    registered: bool = True  # is the worker currently registered in TC? False => skip quarantine/drain


_PROD_POOL_BY_ROLE = {
    "gecko_t_osx_1500_m4": "releng-hardware/gecko-t-osx-1500-m4",
    "gecko_t_osx_1400_r8": "releng-hardware/gecko-t-osx-1400-r8",
}


def candidate_pools(role: str) -> list[str]:
    """The pools a role's worker could be registered in, staging first.

    A role backs a prod pool by convention, but the SAME role also backs its `-staging`
    sibling (e.g. gecko_t_osx_1500_m4 → both gecko-t-osx-1500-m4 and -staging), so the role
    alone can't disambiguate — callers probe these in order against TC. Shared by resolve()
    and the fresh-host quarantine so the two can't drift onto different pool names.
    """
    base_pool = _PROD_POOL_BY_ROLE.get(role)
    if not base_pool:
        raise ValueError(f"no worker pool mapping for role '{role}'")
    return [f"{base_pool}-staging", base_pool]


def resolve(hostname: str) -> HostContext:
    """Look up everything we need about a host from its short name."""
    # Validate before the name reaches SSH / expect / SimpleMDM / VNC. Every CLI
    # subcommand and the runner flow funnel through resolve(), so this one gate
    # constrains the whole orchestrator to well-formed fleet hostnames.
    hostname = validate_short(hostname)
    role = role_for_hostname(hostname)

    # Resolve the real pool from where the worker is actually registered in TC. If it's
    # registered in neither (a fresh host not yet booted into TC), fall back to the prod pool —
    # quarantine/drain then no-op on the 404.
    pools = candidate_pools(role)
    base_pool = pools[-1]
    worker_group = "mdc1"
    found_pool = taskcluster.find_registered_pool(pools, worker_group, hostname)
    # If registered nowhere (fresh host not yet in TC, or a prior wipe that never finished),
    # fall back to the prod pool for any pool-scoped call, and flag it so the reprovision flow
    # skips quarantine/drain — there's nothing scheduling tasks on an unregistered worker, so
    # quarantining it would just 404 (fail-closed) and wedge the run.
    pool = found_pool or base_pool

    fqdn = f"{hostname}.test.releng.mdc1.mozilla.com"
    device = simplemdm.find_device_by_name(hostname)
    device_id = device["id"] if device else None

    return HostContext(
        hostname=hostname,
        fqdn=fqdn,
        role=role,
        worker_pool_id=pool,
        simplemdm_device_id=device_id,
        registered=found_pool is not None,
    )


def resolve_offline(hostname: str) -> HostContext:
    """Build a HostContext from the hostname alone — no SimpleMDM, no Taskcluster.

    `resolve()` costs a SimpleMDM device lookup and up to two TC pool probes per host, which
    the wipe/quarantine steps need. The fresh-host path doesn't: preflight only touches the box
    over SSH, and a readiness sweep across 55 hosts shouldn't need an MDM API key at all. The
    fields those steps would fill are left unset, so anything that requires them fails loudly
    rather than acting on a half-populated context.
    """
    hostname = validate_short(hostname)
    return HostContext(
        hostname=hostname,
        fqdn=f"{hostname}.test.releng.mdc1.mozilla.com",
        role=role_for_hostname(hostname),
        worker_pool_id="",  # unset on purpose: no pool-scoped call belongs on this path
        registered=False,
    )


def check() -> None:
    """Read-only preflight: confirm every credential resolves from the vault. No host, no API
    writes, no changes — safe to run anytime (onboarding a coworker, before a demo). Reports one
    line per credential and exits non-zero if any required one fails."""
    ui.step("CHECK", "resolving credentials from the RelOps vault (read-only)")
    problems = 0

    def _try(label: str, getter, *, required: bool = True) -> None:
        nonlocal problems
        try:
            val = getter()
        except ReprovisionError as e:
            ui.err(f"{label}: {e}")
            problems += 1
            return
        if val:
            ui.ok(f"{label} — resolves")
        elif required:
            ui.err(f"{label}: not configured (empty)")
            problems += 1
        else:
            ui.warn(f"{label}: not configured (optional — only needed for quarantine/drain)")

    _try("admin password", ssh_admin_password)
    _try("admin SSH key", ssh_admin_key)
    _try("SimpleMDM API key", simplemdm_api_key)
    _try("Taskcluster clientId", lambda: tc_credentials()[0], required=False)
    _try("Taskcluster token", lambda: tc_credentials()[1], required=False)

    if problems:
        raise ReprovisionError(f"{problems} credential(s) didn't resolve — see the ✗ line(s) above")
    ui.ok("all credentials resolve — you're good to go")


# --- workflow steps ---

# Written by the bootstrap driver when everything has converged; presence == done.
SENTINEL = "/var/log/m4-bootstrap-complete"
# Laid down by the signed bootstrap pkg at install time. Presence == the managed install ran,
# i.e. the host is in a group that carries the pkg. The driver self-cleans its *driver* script
# and LaunchDaemon on completion but leaves this payload in place, so it stays a valid signal
# after a host has finished bootstrapping.
BOOTSTRAP_PKG_PAYLOAD = "/usr/local/sbin/m4-bootstrap.sh"
# Where the OS-upgrade script is staged when driven over SSH instead of as an MDM script job.
# /var/root so it is root-only by location as well as by mode.
OS_UPGRADE_REMOTE = "/var/root/macos-upgrade.sh"


def _os_version_matches(actual: str, expected: str) -> bool:
    """True when `actual` is `expected` or a point release of it.

    Prefix-with-dot, not a bare startswith: "15.3" must accept 15.3 and 15.3.1 but reject
    15.30 (a real version string shape) and 15.4.
    """
    return actual == expected or actual.startswith(expected + ".")


def step_preflight(
    ctx: HostContext,
    *,
    expected_os: str = "",
    require_sip_disabled: bool = True,
) -> None:
    """Refuse to provision a fresh host that isn't at its target OS / SIP state yet.

    This is the gate that keeps a batch honest. Three failure modes it exists to prevent,
    each one observed on a previous rollout:

    1. **OS install lands mid-bootstrap.** On the 2026-05-12 batch a pending "upgrade to 15.3"
       MDM job fired on ~12 of 25 hosts while puppet was converging and took them all offline
       for 25-45 min. The OS must already be at target before the host enters the bootstrap
       group — so we assert it here rather than hoping the ordering held.
    2. **SIP came back on.** The fleet's TCC grants for this role are written straight into the
       system TCC DB (`add_tcc_perms.sh` takes that branch only when `csrutil status` says
       disabled). A host that quietly re-enabled SIP still *finishes* bootstrap — it just
       finishes wrong, with the PPPC-dependent branch on a role that has no PPPC profile. That
       is far more expensive to find later than to catch here.
    3. **Box isn't up yet.** Fresh DEP hosts appear over ~15 min. Waiting the full window
       inside a batch slot burns a concurrency slot for one host; "not ready, skip, re-run"
       gets the other 54 moving.

    Raises NotReadyError (exit 2 — go fix / come back) for all three, never ReprovisionError.
    """
    s = get_settings()
    expected_os = expected_os or s.provision_expected_os
    ui.step("PREFLIGHT", "verify the host is at its target OS + SIP state before we commit to it")

    ui.wire(f"tcp connect {ctx.fqdn}:22  (fresh DEP hosts come up over ~15 min)")
    try:
        ssh.wait_for_sshd(ctx.fqdn, timeout=s.preflight_sshd_wait_seconds)
    except TimeoutError:
        raise NotReadyError(
            f"{ctx.fqdn}: sshd not reachable within {s.preflight_sshd_wait_seconds}s — "
            "still converging, powered off, or not on the VPN"
        ) from None
    ui.ok("sshd reachable")

    ui.wire(f"ssh admin@{ctx.hostname} sw_vers -productVersion")
    cp = ssh.run(ctx.fqdn, "sw_vers -productVersion", check=False)
    actual_os = cp.stdout.decode(errors="replace").strip()
    if cp.returncode != 0 or not actual_os:
        raise NotReadyError(f"{ctx.fqdn}: couldn't read the OS version over ssh (admin key installed yet?)")
    if not _os_version_matches(actual_os, expected_os):
        raise NotReadyError(
            f"{ctx.fqdn}: macOS {actual_os}, expected {expected_os} — let the MDM in-place update "
            "finish before moving this host into the bootstrap group (an OS install landing "
            "mid-puppet takes the box down for 25-45 min)"
        )
    ui.ok(f"macOS {actual_os}")

    # Same detection as ronin_puppet's add_tcc_perms.sh, so preflight and puppet can't disagree
    # about which TCC branch this host is on.
    ui.wire(f"ssh admin@{ctx.hostname} csrutil status")
    cp = ssh.run(ctx.fqdn, "csrutil status", check=False)
    sip_raw = cp.stdout.decode(errors="replace").strip()
    sip_disabled = "disabled" in sip_raw.lower()
    if require_sip_disabled and not sip_disabled:
        raise NotReadyError(
            f"{ctx.fqdn}: SIP is not disabled ({sip_raw or 'no output'}) — puppet would take the "
            "PPPC/system-DB-read-only branch on a role that has no PPPC profile. Disable SIP in "
            "Recovery first, or pass --allow-sip-enabled if this host is meant to be SIP-on"
        )
    ui.ok("SIP disabled" if sip_disabled else f"SIP enabled — allowed by request ({sip_raw})")

    # Informational: mint/escrow handle both of these, so they gate nothing. Printed because
    # "already ENABLED / already escrowed" is the difference between a fresh host and one
    # somebody already half-provisioned by hand.
    token = ssh.secure_token_status(ctx.fqdn)
    ui.info(f"admin SecureToken: {token or 'unknown'} (mint will grant it if needed)")
    cp = ssh.run(ctx.fqdn, "sudo profiles status -type bootstraptoken", check=False)
    escrowed = b"escrowed to server: YES" in cp.stdout
    ui.info(f"Bootstrap Token escrowed: {'YES' if escrowed else 'no — escrow step will fix'}")

    # Reported, NOT gated. In the intended rollout order the readiness sweep runs BEFORE hosts
    # are moved into the bootstrap group, so the pkg is legitimately absent here and failing on
    # it would fail every host in the sweep. Across a batch this line doubles as the "has the
    # group move happened yet?" indicator. `provision` gates on it separately, later, once the
    # host has had a login and time to converge.
    ui.info(
        f"bootstrap pkg: {'landed' if ssh.file_exists(ctx.fqdn, BOOTSTRAP_PKG_PAYLOAD) else 'not yet'}"
        " (installed by bootstrap-group membership)"
    )
    if ssh.file_exists(ctx.fqdn, SENTINEL):
        ui.warn(f"sentinel {SENTINEL} already present — this host has bootstrapped before")


def step_wait_for_bootstrap_pkg(ctx: HostContext) -> None:
    """Confirm the bootstrap pkg actually landed, before committing to the long sentinel wait.

    The pkg is a managed install driven by SimpleMDM **assignment-group membership** — moving a
    host into the bootstrap group *is* the trigger. A host in the wrong group therefore never
    bootstraps, and without this check `step_wait_for_sentinel` polls the full
    bootstrap_max_wait_seconds (an hour by default) before failing with "sentinel did not appear
    in time" — which points at the bootstrap when the real answer is "wrong group". Across a
    55-host batch that is an extremely expensive way to discover a group-assignment mistake.

    A short bounded poll rather than an instant check, because a group move that just happened
    leaves an MDM check-in pending and check-in on these boxes is often boot-only.

    Deliberately runs AFTER mint/escrow, not before: pkg delivery has been observed to land
    during convergence once admin logs in (i.e. around the mint), so gating on it earlier could
    fail a host that was going to be fine. mint and escrow are both idempotent and cheap, so
    there's nothing to unwind.
    """
    s = get_settings()
    if ssh.file_exists(ctx.fqdn, SENTINEL):
        ui.ok("host has already bootstrapped — pkg check not needed")
        return

    ui.step("BOOTSTRAP PKG", "confirm the signed pkg landed — i.e. the host is in the bootstrap group")
    ui.wire(f"ssh admin@{ctx.hostname} test -f {BOOTSTRAP_PKG_PAYLOAD}")
    deadline = time.monotonic() + s.bootstrap_pkg_max_wait_seconds
    found = False
    with ui.waiting("waiting for the managed install to land") as tick:
        while time.monotonic() < deadline:
            if ssh.file_exists(ctx.fqdn, BOOTSTRAP_PKG_PAYLOAD):
                found = True
                break
            tick("not yet — MDM check-in on these boxes is often boot-only")
            time.sleep(s.bootstrap_pkg_poll_seconds)

    if not found:
        raise NotReadyError(
            f"{ctx.fqdn}: bootstrap pkg hasn't landed after {s.bootstrap_pkg_max_wait_seconds}s "
            f"({BOOTSTRAP_PKG_PAYLOAD} missing) — is this host in the bootstrap group? The pkg is "
            "a managed install triggered by group membership, so nothing will bootstrap until it is"
        )
    ui.ok("bootstrap pkg present — the host will provision itself")


def _os_upgrade_script(expected_os: str) -> str:
    """The packaged upgrade script with the credential and target substituted in.

    Same file that gets pasted into SimpleMDM (scripts/simplemdm-macos-upgrade.sh is a symlink
    to it) — one body, so the MDM copy and the SSH-driven copy cannot drift. Driving it from
    here is strictly better than the MDM path on secrets: the password is resolved from the
    vault at fire time and reaches the host over an ssh stdin pipe, so it never sits in the
    SimpleMDM script body and never appears in an argv.
    """
    from importlib import resources

    body = (resources.files("orchestrator") / "data" / "macos-upgrade.sh").read_text()
    body = body.replace('ADMIN_PASSWORD="INSERT_HERE"', f'ADMIN_PASSWORD={shlex.quote(ssh_admin_password())}')
    if expected_os:
        body = body.replace('TARGET_VERSION="15.3"', f'TARGET_VERSION={shlex.quote(expected_os)}')
    return body


def step_os_update(ctx: HostContext, *, expected_os: str = "") -> None:
    """Kick the in-place macOS upgrade on one host, then return — it reboots on its own.

    Staged over SSH rather than fired as a SimpleMDM script job, for three reasons: the
    credential stays out of the SimpleMDM UI (see _os_upgrade_script), we get a real exit code
    instead of SimpleMDM's job-status API (documented unreliable — it reports `pending` after a
    script has already run), and it composes with the batch driver's concurrency bound.

    That bound is the point. `releng-pxe1` has been served by `python3 -m http.server`, which
    is single-threaded AND has a listen backlog of 5 — past five pending connections the rest
    are refused outright. 55 hosts pulling a ~14GB installer at once is not slow, it's a pile
    of timeouts. Pacing costs almost nothing in wall clock because the link is the bottleneck
    either way; it just converts failures into a queue.

    Returns once the upgrade is *launched*. The script downloads, installs the pkg, and reboots
    into a one-shot LaunchDaemon that runs startosinstall — tens of minutes, unattended. Poll
    for arrival separately with `--action preflight`, which is the same OS check.
    """
    s = get_settings()
    expected_os = expected_os or s.provision_expected_os
    ui.step("OS UPDATE", f"in-place upgrade to macOS {expected_os} — launches, then the host reboots itself")

    with ui.waiting("waiting for sshd"):
        ssh.wait_for_sshd(ctx.fqdn, timeout=s.preflight_sshd_wait_seconds)

    cp = ssh.run(ctx.fqdn, "sw_vers -productVersion", check=False)
    current = cp.stdout.decode(errors="replace").strip()
    if current and _os_version_matches(current, expected_os):
        ui.ok(f"already on macOS {current} — nothing to do")
        return

    ui.wire(f"scp → {OS_UPGRADE_REMOTE} (0700, credential substituted from the vault)")
    ssh.write_file_as_root(ctx.fqdn, OS_UPGRADE_REMOTE, _os_upgrade_script(expected_os).encode(), mode="0700")

    # Detached: the download alone outlives any sane ssh timeout, and the script ends in a
    # reboot that would kill the channel anyway. setsid+nohup so it survives our disconnect.
    ui.wire(f"ssh admin@{ctx.hostname} sudo nohup {OS_UPGRADE_REMOTE} (detached; log /var/log/macos-upgrade.log)")
    ssh.run(ctx.fqdn, f"sudo /usr/bin/nohup {OS_UPGRADE_REMOTE} >/dev/null 2>&1 & echo launched", check=False)

    # Confirm it actually started rather than dying on a precondition — the script's own guards
    # (placeholder credential, no SecureToken, low disk) all fail within a second or two.
    time.sleep(5)
    cp = ssh.run(ctx.fqdn, "sudo tail -5 /var/log/macos-upgrade.log 2>/dev/null", check=False)
    tail = cp.stdout.decode(errors="replace").strip()
    if "[ERROR]" in tail:
        raise NotReadyError(f"{ctx.fqdn}: upgrade refused to start —\n    " + tail.replace("\n", "\n    "))
    ui.ok(f"upgrade launched — macOS {current or 'unknown'} → {expected_os}")
    ui.info("host downloads ~14GB, installs, then reboots into startosinstall (tens of minutes)")
    ui.info("confirm arrival later with: reprovision batch <file> --action preflight")


def step_quarantine(ctx: HostContext, until: str | None = None, info: str = "") -> None:
    if not until:
        until = (datetime.now(timezone.utc) + timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    ui.step("QUARANTINE", "tell Taskcluster to stop scheduling tasks on this worker")
    ui.wire(f"PUT queue/v1 quarantineWorker {ctx.worker_pool_id}/{ctx.worker_group}/{ctx.hostname}")
    taskcluster.quarantine(ctx.worker_pool_id, ctx.worker_group, ctx.hostname, until, info)
    ui.ok(f"quarantined until {until[:10]}")


def step_drain(ctx: HostContext) -> None:
    s = get_settings()
    ui.step("DRAIN", "let the worker finish its in-flight task (2 consecutive idle polls)")
    ui.wire(f"queue.getWorker {ctx.hostname} → inspect recentTasks run states")
    deadline = time.monotonic() + s.drain_max_wait_seconds
    consecutive_idle = 0
    drained = False
    with ui.waiting("checking for an active task") as tick:
        while time.monotonic() < deadline:
            busy = taskcluster.is_currently_busy(ctx.worker_pool_id, ctx.worker_group, ctx.hostname)
            if not busy:
                consecutive_idle += 1
                # Require 2 consecutive idle polls so we don't race a worker that's
                # between two tasks (recentTasks shows the completed one; the new one
                # hasn't propagated yet).
                tick(f"idle {consecutive_idle}/2 — confirming")
                if consecutive_idle >= 2:
                    drained = True
                    break
            else:
                consecutive_idle = 0
                tick("worker busy — waiting for the task to finish")
            time.sleep(s.drain_poll_seconds)
    if not drained:
        raise TimeoutError(f"drain wait exceeded {s.drain_max_wait_seconds}s")
    ui.ok("drained — no task in flight")


def step_wipe(ctx: HostContext) -> None:
    if not ctx.simplemdm_device_id:
        raise ReprovisionError(f"{ctx.hostname} not found in SimpleMDM")
    # A prior EACS may have rotated this host's SSH key; clear any stale entry from the tool's
    # known_hosts so the verify connection accept-new's the current key instead of failing.
    ui.step("WIPE · EACS", "Erase All Content & Settings — DoNotObliterate (fails safe, never obliterates)")
    ssh.forget_host_key(ctx.fqdn)
    # Guard: EACS needs an escrowed Bootstrap Token. Without it, the erase either fails
    # (DoNotObliterate) or full-obliterates into a long headless macOS reinstall. Refuse to
    # wipe a box that can't EACS — verify BST over ssh first (operator key, no password).
    ui.wire(f"ssh admin@{ctx.hostname} sudo profiles status -type bootstraptoken")
    bst = ssh.run(ctx.fqdn, "sudo profiles status -type bootstraptoken", check=False)
    # Distinguish "couldn't verify over ssh" from "genuinely not escrowed" — otherwise an
    # ssh failure (VPN down, first-connection host key, missing operator key) masquerades as
    # a missing Bootstrap Token and sends the operator down the wrong path.
    if bst.returncode != 0:
        raise ReprovisionError(
            f"{ctx.fqdn}: couldn't verify the Bootstrap Token over ssh (exit {bst.returncode}) — "
            f"NOT wiping. Check VPN + SSH access to the host first.\n"
            f"{bst.stderr.decode(errors='replace').strip()}"
        )
    if b"escrowed to server: YES" not in bst.stdout:
        # Not escrowed → EACS would fail (DoNotObliterate) or full-obliterate into a headless
        # reinstall. The runner has admin creds and a registered host's admin already holds a
        # SecureToken, so escrow it NOW rather than aborting — this self-heals hosts whose prior
        # bootstrap never finished the escrow (e.g. a run that wedged at the Safari step, like
        # m4-115). step_escrow_bst re-verifies and raises if it still can't escrow; only then do
        # we refuse to wipe.
        ui.warn("Bootstrap Token not escrowed — escrowing it now before the wipe")
        try:
            step_escrow_bst(ctx)
        except ReprovisionError as e:
            raise ReprovisionError(
                f"{ctx.fqdn}: Bootstrap Token not escrowed and auto-escrow failed — NOT wiping.\n"
                f"  {e}\n"
                f"  Mint + escrow manually (reprovision mint, escrow-bst), or wipe via the SimpleMDM "
                f"UI if a full obliterate is truly intended (requires admin to hold a SecureToken)."
            ) from None
    else:
        ui.ok("Bootstrap Token escrowed — EACS can run")
    # Final gate against wiping a busy worker. Quarantine only stops NEW task claims — a task
    # claimed just before quarantine keeps running. `drain` waits that out in the full run, but
    # re-verify right here so a direct `wipe` (or a drain miss) can never erase a worker mid-task.
    # FAIL CLOSED: a destructive EACS must require positive proof of idle. If we can't confirm
    # (TC unreachable / bad creds) OR the worker is busy, ABORT — never wipe on uncertainty.
    # (Previously this failed OPEN — warn + proceed — which let a running worker get wiped.)
    ui.wire(f"queue.getWorker {ctx.hostname} → confirm no task in flight")
    try:
        busy = taskcluster.is_currently_busy(ctx.worker_pool_id, ctx.worker_group, ctx.hostname)
    except Exception as e:  # noqa: BLE001 — any TC/auth failure → can't verify idle → refuse to wipe
        raise ReprovisionError(
            f"{ctx.hostname}: couldn't confirm the worker is idle via Taskcluster ({e}) — refusing "
            f"to wipe. A reprovision must never EACS a worker that might be running a task. Fix TC "
            f"access (clientId/token) and retry, or wipe manually once you've confirmed it's drained."
        ) from e
    if busy:
        raise ReprovisionError(
            f"{ctx.hostname} is still running a task — NOT wiping. Quarantine + drain first "
            f"(`reprovision quarantine`, `reprovision drain`) or wait for the task to finish."
        )
    ui.ok("no task in flight")
    # Record the current enrolled_at so wait_for_reenroll can detect a *fresh* enrollment
    # (status alone is unreliable: it stays "enrolled" until the erase actually executes).
    ctx.pre_wipe_enrolled_at = simplemdm.get_device(ctx.simplemdm_device_id).get("attributes", {}).get("enrolled_at")
    ui.wire(f"SimpleMDM POST /devices/{ctx.simplemdm_device_id}/wipe  obliteration_behavior=DoNotObliterate")
    simplemdm.wipe(ctx.simplemdm_device_id)
    ui.ok("erase command accepted by SimpleMDM")


def step_wait_for_reenroll(ctx: HostContext) -> None:
    s = get_settings()
    # Baseline: the pre-wipe enrolled_at (from step_wipe). If unset (step run standalone),
    # capture the current value now. We wait for a DIFFERENT enrolled_at + status=enrolled,
    # so we don't false-return on the pre-wipe enrollment (status lags the erase).
    baseline = ctx.pre_wipe_enrolled_at
    if baseline is None:
        baseline = simplemdm.get_device(ctx.simplemdm_device_id).get("attributes", {}).get("enrolled_at")
    ui.step("RE-ENROLL", "erase → reboot → DEP re-enrollment · typically ~5 min")
    ui.wire(f"SimpleMDM GET /devices/{ctx.simplemdm_device_id}  (poll enrolled_at ≠ {baseline})")
    deadline = time.monotonic() + s.wipe_max_wait_seconds
    start = time.monotonic()
    next_poll = 0.0
    reenrolled = False
    a: dict = {}
    with ui.waiting("waiting for a fresh enrollment", eta_seconds=300) as tick:
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_poll:
                next_poll = now + s.wipe_poll_seconds
                a = simplemdm.get_device(ctx.simplemdm_device_id).get("attributes", {})
                if a.get("status") == "enrolled" and a.get("enrolled_at") != baseline:
                    reenrolled = True
                    break
            tick(_reenroll_phase(now - start))
            time.sleep(1)
    if not reenrolled:
        raise TimeoutError("device did not re-enroll within window")
    ui.ok(f"re-enrolled — fresh enrolled_at {a.get('enrolled_at')}")


# Expected phases of the ~5-min EACS→DEP window, keyed to elapsed seconds. Not literal
# device state (we only poll enrolled_at) — a truthful "typical timeline" so the wait reads
# as progress, not a hang.
def _reenroll_phase(elapsed: float) -> str:
    for cutoff, label in (
        (60, "erasing volume (EACS)"),
        (120, "rebooting into Setup Assistant"),
        (210, "DEP check-in with SimpleMDM"),
        (300, "installing SCEP + CLT profiles"),
    ):
        if elapsed < cutoff:
            return f"{label} …"
    return "finishing enrollment …"


def step_mint(ctx: HostContext) -> None:
    """
    Mint the first SecureToken via an interactive password login.

    Required because DEP skips Setup Assistant on this fleet: admin exists but holds
    no SecureToken and isn't a volume owner until an interactive (PAM) login. Proven
    by A/B on m4-81 (2026-07-02): with this login the bootstrap finishes; without it,
    it wedges at the BST wait-loop and times out. Idempotent — skips if already ENABLED.
    """
    ui.step("MINT SECURETOKEN", "DEP skips Setup Assistant, so admin has no token until an interactive login")
    # The box just re-enrolled post-EACS with a fresh host key; forget the old one so the
    # SecureToken status check (which uses ssh.run) doesn't fail on a key mismatch.
    ssh.forget_host_key(ctx.fqdn)
    with ui.waiting("waiting for sshd (relops-ssh pkg lands during convergence)"):
        ssh.wait_for_sshd(ctx.fqdn)
    if "ENABLED" in ssh.secure_token_status(ctx.fqdn):
        ui.ok("admin already holds a SecureToken — skipping mint")
        return
    ui.wire(f"expect: ssh admin@{ctx.hostname} (keyboard-interactive PAM login → grants first SecureToken)")
    ssh.password_login(ctx.fqdn)
    enabled = False
    with ui.waiting("verifying the SecureToken came up ENABLED") as tick:
        for i in range(6):
            if "ENABLED" in ssh.secure_token_status(ctx.fqdn):
                enabled = True
                break
            tick(f"not yet — retry {i + 1}/6")
            time.sleep(5)
    if not enabled:
        raise ReprovisionError(f"{ctx.fqdn}: admin SecureToken not ENABLED after mint")
    ui.ok("admin SecureToken ENABLED")


def step_escrow_bst(ctx: HostContext) -> None:
    """
    Escrow the Bootstrap Token so the box is MDM-EACS-able next cycle.

    Runs non-interactively with -user/-password (the bare form prompts for a username
    and fails). Requires admin to already hold a SecureToken — so step_mint must run
    first. NB: the bootstrap script skips its own BST-escrow when a token already
    exists, so on the pre-minted path this step is what actually escrows the BST.
    """
    s = get_settings()
    ui.step("ESCROW BOOTSTRAP TOKEN", "escrow the BST so this box is EACS-able next cycle")
    ui.wire(f"ssh admin@{ctx.hostname} sudo profiles install -type bootstraptoken -user {s.ssh_admin_user} -password ••••••")
    install_cmd = (
        f"sudo profiles install -type bootstraptoken "
        f"-user {s.ssh_admin_user} -password {shlex.quote(ssh_admin_password())}"
    )
    try:
        ssh.run(ctx.fqdn, install_cmd)
    except ReprovisionError as e:
        # ssh.run already scrubs the command (which embeds the password); add a mint hint.
        raise ReprovisionError(f"{e}\n    (has admin minted a SecureToken? run `reprovision mint` first)") from None
    cp = ssh.run(ctx.fqdn, "sudo profiles status -type bootstraptoken")
    if b"escrowed to server: YES" not in cp.stdout:
        raise ReprovisionError(f"BST escrow check failed:\n{cp.stdout.decode()}")
    ui.ok("Bootstrap Token escrowed to server")


def step_wait_for_sentinel(ctx: HostContext) -> None:
    s = get_settings()
    ui.step("BOOTSTRAP", "the freshly-enrolled host provisions itself — zero operator SSH from here")
    ui.wire("signed bootstrap PKG (managed install) lands via SimpleMDM during DEP convergence")
    ui.wire("→ host fetches its vault.yaml over mTLS from the forge LB (step-ca SCEP client cert)")
    ui.wire(f"→ puppet apply: role {ctx.role} — generic-worker, users, TCC perms, launch daemons")
    ui.wire("→ generic-worker self-registers with Taskcluster (Hawk) and starts claiming work")
    ui.wire(f"ssh admin@{ctx.hostname} test -f {SENTINEL}  (poll for the sentinel it writes)")
    deadline = time.monotonic() + s.bootstrap_max_wait_seconds
    found = False
    with ui.waiting("waiting for the bootstrap sentinel") as tick:
        while time.monotonic() < deadline:
            if ssh.file_exists(ctx.fqdn, SENTINEL):
                found = True
                break
            tick("puppet converging on the freshly-enrolled host")
            time.sleep(s.bootstrap_poll_seconds)
    if not found:
        raise TimeoutError("bootstrap sentinel did not appear in time")
    ui.ok(f"bootstrap complete — {SENTINEL} present")


def step_quarantine_on_register(ctx: HostContext) -> None:
    """Wait for a fresh worker to appear in Taskcluster, then quarantine it on sight.

    A fresh DEP host is not registered in TC, and `queue.quarantineWorker` 404s on a worker
    that doesn't exist yet — that's why the reprovision flow skips quarantine for unregistered
    hosts. So the only way to hold a brand-new box out of the pool is to watch for it and
    quarantine the moment it shows up.

    **This narrows the race; it does not close it.** The driver writes the sentinel, then
    worker-runner starts generic-worker, which registers and can `claimWork` immediately —
    observed at roughly a minute after the sentinel. We poll every few seconds from the
    sentinel onward, so the exposure is seconds rather than however long it takes an operator
    to notice, but a task claimed inside that window does run on an unvalidated host. If you
    need a hard guarantee, don't put the host in the bootstrap group until the pool is drained,
    or accept the window knowingly.

    Fails closed: if TC credentials aren't configured we refuse up front rather than spend the
    bootstrap window discovering we can't quarantine anything.
    """
    s = get_settings()
    client_id, access_token = tc_credentials()
    if not (client_id and access_token):
        raise ReprovisionError(
            "quarantine-on-register needs Taskcluster credentials "
            "(REPROVISION_TC_CLIENT_ID / _ACCESS_TOKEN, or their _REF defaults) — "
            "refusing to wait for a registration we couldn't act on"
        )

    pools = candidate_pools(ctx.role)
    ui.step("QUARANTINE ON REGISTER", "hold the fresh worker out of the pool the moment it appears")
    ui.wire(f"queue.getWorker {' | '.join(pools)} / {ctx.worker_group} / {ctx.hostname}  (poll)")

    deadline = time.monotonic() + s.quarantine_on_register_max_wait_seconds
    found_pool: str | None = None
    with ui.waiting("waiting for the worker to register with Taskcluster") as tick:
        while time.monotonic() < deadline:
            found_pool = taskcluster.find_registered_pool(pools, ctx.worker_group, ctx.hostname)
            if found_pool:
                break
            tick("not in a pool yet — worker-runner starts generic-worker after the sentinel")
            time.sleep(s.quarantine_on_register_poll_seconds)

    if not found_pool:
        raise ReprovisionError(
            f"{ctx.hostname} never registered in {' or '.join(pools)} within "
            f"{s.quarantine_on_register_max_wait_seconds}s — bootstrap finished but the worker "
            "didn't come up; check worker-runner and /var/tmp/semaphore/run-buildbot on the host"
        )

    ctx.worker_pool_id = found_pool
    ui.ok(f"registered in {found_pool}")
    step_quarantine(ctx, info="fresh host — quarantined on registration pending validation")


def step_unquarantine(ctx: HostContext) -> None:
    ui.step("UNQUARANTINE", "return the worker to service")
    ui.wire(f"PUT queue/v1 quarantineWorker {ctx.hostname}  (quarantineUntil → past)")
    taskcluster.unquarantine(ctx.worker_pool_id, ctx.worker_group, ctx.hostname)
    ui.ok("returned to service")


def reprovision(hostname: str, *, skip_wipe: bool = False, unquarantine: bool = False) -> None:
    """Full E2E workflow. skip_wipe lets operators re-run later steps after a wipe.

    unquarantine defaults to False: by design a host stays quarantined through wipe +
    reprovision (and the fleet has no un-quarantine key wired yet). Pass unquarantine=True
    to return the host to service at the end — the eventual prod-return flow, once a
    queue:quarantine-scoped credential is available.

    Admin-password hardening is a DEP config concern, not a workflow step: set a strong,
    random admin password in the SimpleMDM DEP account-setup and point the mint at it via
    REPROVISION_SSH_ADMIN_PASSWORD. (SimpleMDM's rotate_admin_password can't be used — it
    requires an auto-generated managed password, which the mint can't read back.)
    """
    ctx = resolve(hostname)
    started = time.monotonic()
    ui.banner(ctx.hostname, ctx.role, ctx.worker_pool_id)

    # Show the whole pipeline up front so it never reads as a single opaque action.
    phases = []
    if ctx.registered:
        phases += ["QUARANTINE", "DRAIN"]
    if not skip_wipe:
        phases += ["WIPE", "RE-ENROLL"]
    phases += ["MINT", "ESCROW BST", "BOOTSTRAP"]
    if unquarantine and ctx.registered:
        phases += ["UNQUARANTINE"]
    ui.flow(phases)

    if ctx.registered:
        step_quarantine(ctx)
        step_drain(ctx)
    else:
        ui.warn(f"{ctx.hostname} isn't registered in Taskcluster — skipping quarantine/drain (nothing to drain)")
    if not skip_wipe:
        step_wipe(ctx)
        step_wait_for_reenroll(ctx)
    step_mint(ctx)  # mint SecureToken (must precede escrow_bst)
    step_escrow_bst(ctx)
    # No vault-delivery and no bootstrap-trigger steps:
    #  - vault: the bootstrap fetches vault.yaml itself over mTLS using its SCEP cert.
    #  - bootstrap: it's delivered as a signed PKG (managed install) that lands during DEP
    #    convergence once admin logs in (the mint), so nothing needs to trigger it. We just
    #    wait for the sentinel it writes.
    step_wait_for_sentinel(ctx)
    # Default: leave the host quarantined (matches current fleet reality; no un-quarantine
    # key wired). Only return it to service when explicitly asked — the eventual prod flow.
    # Skip if we never quarantined it (host was unregistered at start).
    if unquarantine and ctx.registered:
        step_unquarantine(ctx)
    ui.summary(ctx.hostname, time.monotonic() - started, quarantined=not unquarantine)


def provision(
    hostname: str,
    *,
    expected_os: str = "",
    require_sip_disabled: bool = True,
    wait: bool = True,
    quarantine_on_register: bool = False,
) -> None:
    """Bring a **fresh** DEP-enrolled host to prod. No EACS, no wipe — nothing here can erase.

    For factory-clean hardware that auto-enrolled via DEP/ADE, where `reprovision run`'s
    wipe/re-enroll phases are not just unnecessary but destructive. The one-command form of
    the "Fresh (B)" column in the 2026-07-08 handoff, which until now had to be driven as
    three separate subcommands — with `run` sitting right there as the obvious-looking
    alternative that would wipe the box.

    Where this sits in the wider rollout, for a SIP-off fleet:

        DEP enroll (intake group)
          → `reprovision mint`        # grants admin a SecureToken, i.e. a volume owner —
          →                           #   Recovery needs one to authenticate `csrutil disable`
          → operator: csrutil disable in Recovery
          → MDM: in-place update to the target OS
          → move host into the bootstrap group   # membership installs the signed pkg = "go"
          → `reprovision provision`   # THIS: preflight gate → mint (no-op) → escrow BST →
                                      #   wait for the pkg's sentinel

    `mint` is idempotent and safe to run early, which is what makes that ordering work: the
    same step that mints the volume owner for the Recovery trip is a no-op by the time this
    command re-runs it.

    quarantine_on_register holds the finished host out of the pool (see
    step_quarantine_on_register for what that does and doesn't guarantee). It needs `wait`,
    since there's no registration to catch if we're not staying for the bootstrap.
    """
    if quarantine_on_register and not wait:
        raise ReprovisionError(
            "--quarantine-on-register needs the bootstrap wait: the worker registers at the very "
            "end, so there's nothing to catch if we return after the BST escrow. Drop --no-wait, "
            "or quarantine later with `reprovision quarantine-on-register <host>`"
        )
    # Check the TC credentials NOW, not in ~40 minutes. The whole point of the flag is that the
    # host doesn't take work; discovering we can't quarantine it only once it's already claiming
    # tasks is the one outcome worth engineering against.
    if quarantine_on_register:
        client_id, access_token = tc_credentials()
        if not (client_id and access_token):
            raise ReprovisionError(
                "--quarantine-on-register needs Taskcluster credentials "
                "(REPROVISION_TC_CLIENT_ID / _ACCESS_TOKEN, or their _REF defaults) — "
                "checked up front so this fails now rather than after the bootstrap"
            )

    ctx = resolve_offline(hostname)
    started = time.monotonic()
    ui.banner(ctx.hostname, ctx.role, "fresh DEP host — no wipe")

    phases = ["PREFLIGHT", "MINT", "ESCROW BST"]
    if wait:
        phases += ["BOOTSTRAP PKG", "BOOTSTRAP"]
    if quarantine_on_register:
        phases.append("QUARANTINE ON REGISTER")
    ui.flow(phases)

    step_preflight(ctx, expected_os=expected_os, require_sip_disabled=require_sip_disabled)
    step_mint(ctx)  # mint SecureToken (must precede escrow_bst)
    step_escrow_bst(ctx)
    if wait:
        # Confirm the group move actually happened before committing to the hour-long sentinel
        # poll. Cheap, and turns "wrong SimpleMDM group" from a silent timeout into a named skip.
        step_wait_for_bootstrap_pkg(ctx)
        # The bootstrap pkg is a managed install driven by group membership, so there is
        # nothing to trigger — we only wait for the sentinel it writes.
        step_wait_for_sentinel(ctx)
    else:
        ui.info("--no-wait: credentials are in place; sweep the sentinel later with `wait-sentinel`")

    if quarantine_on_register:
        step_quarantine_on_register(ctx)

    elapsed = time.monotonic() - started
    ui.provisioned(ctx.hostname, elapsed, waited=wait, quarantined=quarantine_on_register)
