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


def _resolve_mdm_device(ctx: HostContext) -> dict:
    """Find the host's SimpleMDM device record. Serial over SSH first, name as a fallback.

    Hostname is NOT a usable key on fresh hardware. A DEP arrival enrolls as `Mac mini` (with a
    `device_name` like `Mac mini (39)`); the hostname is assigned by DHCP and never written into
    the device record, because nothing in ronin_puppet runs `scutil --set`. Searching the API for
    `macmini-m4-241` therefore returns a clean, entirely misleading zero hits — which is exactly
    what happened to the first four hosts of wave 1 (2026-08-14): all four "skipped" as
    not-enrolled when they were enrolled, on 15.3, and answering SSH.

    So we ask the host for its serial, which is unique and present from enrollment. The name
    fallback exists for hosts that HAVE been renamed (every already-provisioned r8 shows
    `name='macmini-r8-118'`), so a re-run against the existing fleet still resolves without SSH.
    """
    serial = ssh.platform_serial(ctx.fqdn)
    if serial:
        ui.info(f"serial {serial} (from the host — SimpleMDM doesn't know its hostname)")
        device = simplemdm.find_device_by_serial(serial)
        if device is not None:
            return device
        raise NotReadyError(
            f"{ctx.hostname}: serial {serial} isn't in SimpleMDM — the host is up but not enrolled. "
            "Check the DEP/ADE assignment for this serial."
        )

    # No SSH. Fall back to name, which only works post-rename.
    device = simplemdm.find_device_by_name(ctx.hostname)
    if device is not None:
        return device
    raise NotReadyError(
        f"{ctx.hostname}: can't reach the host over SSH to read its serial, and no SimpleMDM "
        f"device is named {ctx.hostname!r}. Fresh DEP arrivals enroll as 'Mac mini', so the "
        "hostname is not a usable lookup key — bring SSH up (admin key / relops_key_admin) and "
        "retry, or pass the device explicitly."
    )


def step_add_to_group(
    ctx: HostContext,
    *,
    group_id: int | None = None,
    quarantine_on_register: bool = False,
) -> None:
    """ADD the host to the bootstrap assignment group — the action that starts everything.

    This was the last manual touch in the path: mint → os-update → preflight → *click in the
    SimpleMDM UI* → provision. Adding a host to the bootstrap group delivers the bootstrap pkg,
    /etc/puppet_role, the CLT, the admin key and passwordless sudo all at once, so it is a single
    "go" action rather than one of several gates. That also means a mis-click is expensive, which
    is why it's worth automating across a 49-host wave rather than repeating it by hand.

    Idempotent: a host already in the group is reported and left alone, so re-running a batch after
    a partial failure costs nothing. Additive only — see add_device_to_assignment_group.
    """
    s = get_settings()
    gid = group_id or s.bootstrap_group_id
    ui.step("ADD TO GROUP", f"SimpleMDM assignment group {gid} — this is what triggers the bootstrap")

    group = simplemdm.get_assignment_group(gid)
    name = group.get("attributes", {}).get("name", "?")
    napps = len(group.get("relationships", {}).get("apps", {}).get("data", []))
    ui.info(f"group {gid} = {name} ({napps} apps)")

    # Verify the group can actually bootstrap anything before we hand it a host. A group with no
    # apps attached delivers no pkg, and the host would then sit until step_wait_for_bootstrap_pkg
    # times out an hour later pointing at the wrong culprit.
    if napps == 0:
        raise ReprovisionError(
            f"assignment group {gid} ({name}) has no apps attached — adding a host to it would "
            "deliver no bootstrap pkg. Check the group in SimpleMDM before running this."
        )

    device = _resolve_mdm_device(ctx)
    device_id = int(device["id"])

    if device_id in simplemdm.assignment_group_device_ids(gid):
        ui.ok(f"already in {name} — no add needed")
        # Membership does NOT prove the pkg was ever pushed: this path skips push_apps, so a host
        # added by hand in the UI (or added while push failed) can sit in the group forever with
        # nothing installed. We don't push here — push_apps hits every member of the group and
        # would re-run the postinstall on hosts that are mid-bootstrap — so just say so loudly.
        if not ssh.file_exists(ctx.fqdn, BOOTSTRAP_PKG_PAYLOAD):
            ui.warn(
                f"in the group but {BOOTSTRAP_PKG_PAYLOAD} is missing — the managed install never "
                "ran on this host. Push the group's apps from SimpleMDM, or remove and re-add it."
            )
    else:
        ui.wire(f"POST /assignment_groups/{gid}/devices/{device_id}   (additive; never a move)")
        simplemdm.add_device_to_assignment_group(gid, device_id)
        ui.wire(f"POST /assignment_groups/{gid}/push_apps")
        simplemdm.push_apps(gid)
        ui.ok(f"added to {name} and pushed — the bootstrap pkg should land shortly")

    if quarantine_on_register:
        # Budget must span the WHOLE bootstrap, not just the registration gap: pkg install,
        # puppet, several reboots, sentinel, then worker start. Wave 1 measured ~30 min from the
        # group add to the worker appearing, against a default watch budget of 900s — so the
        # default would have expired well before there was anything to quarantine.
        s2 = get_settings()
        step_quarantine_on_register(
            ctx,
            max_wait_seconds=s2.bootstrap_max_wait_seconds + s2.quarantine_on_register_max_wait_seconds,
        )


# Profiles whose absence has already cost real debugging time. Matched on a name SUBSTRING, not
# an id, so the warning survives a profile being rebuilt (which changes its id) or renamed around
# the stem. Anything missing from the baseline is reported; these get the story attached.
_LOAD_BEARING_PROFILES: tuple[tuple[str, str], ...] = (
    (
        "Skip Setup Assistant",
        "a freshly-erased host then stops on the 'Select your Wi-Fi network' pane, which nothing "
        "ever dismisses on an ethernet-only DC machine — and it presents as 'Safari automation is "
        "broken', not as a stuck Setup Assistant (m4-214, cost most of a day)",
    ),
    (
        "SSH Keygen Wrapper",
        "Full Disk Access for the SSH keygen wrapper — lost in the same m4-214 incident",
    ),
    (
        "CI Worker Support Binaries",
        "the PPPC profile granting system-level TCC to the worker support binaries. Under SIP a "
        "profile is the ONLY way those grants can land, so a SIP-on host without it diverges from "
        "the prod fleet silently",
    ),
)


# What share of a group's devices must belong to some OTHER group before a device that doesn't is
# treated as an outlier rather than as normal variation. Two thirds: high enough that a legitimate
# split (half a wave earmarked for a different role) isn't flagged, low enough to catch the single
# mis-clicked host among 40.
_MEMBERSHIP_QUORUM = 2 / 3


def _membership_outliers(
    gid: int, target_ids: list[int]
) -> dict[int, tuple[str, list[int]]]:
    """Devices missing a group that almost all of their peers are in — i.e. moved, not added.

    Profile parity alone under-reports this. A device record does not list its assignment groups,
    so this inverts the group->devices relationship once and asks the question peer-wise, with no
    reference group involved: 39 of 40 hosts are in DEP Enrollment, so the 40th is the anomaly.

    Catches strictly more than the profile diff, because the groups a mis-clicked host loses are
    not only profile-bearing. Found live on 2026-08-19: one bootstrap-group device was in that
    group ALONE, having lost DEP Enrollment (Skip Setup Assistant, FDA), Relops Public SSH Key,
    Sudoers and Enable SSH. Without the admin key the orchestrator cannot reach it at all — every
    step is SSH — so the host is unprovisionable, and no profile-only check would say why.
    """
    quorum = len(target_ids) * _MEMBERSHIP_QUORUM
    out: dict[int, tuple[str, list[int]]] = {}
    for group in simplemdm.assignment_groups():
        other = int(group["id"])
        if other == gid:
            continue
        ids = {int(d["id"]) for d in group.get("relationships", {}).get("devices", {}).get("data", [])}
        if len([d for d in target_ids if d in ids]) < quorum:
            continue
        missing = [d for d in target_ids if d not in ids]
        if missing:
            out[other] = (group.get("attributes", {}).get("name", "?"), missing)
    return out


def _device_label(device_id: int) -> str:
    """id + serial + name. A bare device id is not identifying enough to act on.

    A fresh DEP arrival is named "Mac mini", so on 2026-08-19 a device id alone was mistaken for
    macmini-m4-214 on nothing more than a matching enrollment date. The serial is what an operator
    can actually search for in the SimpleMDM UI.
    """
    try:
        a = simplemdm.get_device(device_id).get("attributes", {})
    except ReprovisionError:
        return f"device {device_id}"
    name, serial = a.get("name") or "?", a.get("serial_number") or "?"
    return f"device {device_id} (serial {serial}, named {name!r})"


def step_group_parity(
    *,
    group_id: int | None = None,
    reference_group_id: int | None = None,
    reference_sample: int | None = None,
    max_devices: int = 0,
    hostname: str | None = None,
) -> None:
    """Do the hosts in the bootstrap group get the profiles a working prod host gets?

    Read-only, API-only: no SSH, no writes, safe on a live fleet. Nothing else in the toolchain
    answers this, and it is the one question the m4-214 incident turned on — a host missing Skip
    Setup Assistant and the FDA SSH Keygen Wrapper hung at first boot and presented as a Safari
    fault. The postmortem's advice was to diff `profiles show -type configuration` against a
    known-good host by hand, which needs SSH to a box that by definition may not be reachable yet.
    This asks SimpleMDM instead, before a wave starts.

    The baseline is the INTERSECTION of the profile sets of several sampled reference-group
    devices, not one sampled host: an intersection can't be skewed by a single atypical prod box,
    and a profile that every sampled host carries is one the fleet genuinely standardises on.

    Deliberately compares EFFECTIVE per-device sets rather than the groups themselves — see
    simplemdm.device_profiles. A group-level diff flags the two m4-214 profiles as missing from
    the bootstrap group, which is true and irrelevant: its devices receive both from the additive
    DEP Enrollment group. Crying wolf on that exact pair would train the operator to ignore this.

    Raises ReprovisionError (exit 1) when target devices lack baseline profiles.
    """
    s = get_settings()
    gid = group_id or s.bootstrap_group_id
    ref_gid = reference_group_id or s.reference_group_id
    n_sample = reference_sample or s.group_parity_reference_sample

    ui.step("GROUP PARITY", "do these hosts get the profiles a working prod host gets? (read-only)")

    if gid == ref_gid:
        raise ReprovisionError(
            f"group and reference group are both {gid} — nothing to compare. Pass "
            "--reference-group-id to measure against a different group."
        )

    ref_name = simplemdm.get_assignment_group(ref_gid).get("attributes", {}).get("name", "?")
    ref_devices = simplemdm.assignment_group_device_ids(ref_gid)[:n_sample]
    if not ref_devices:
        raise ReprovisionError(
            f"reference group {ref_gid} ({ref_name}) has no devices — nothing to build a baseline "
            "from. Point --reference-group-id at a populated production group."
        )

    baseline: dict[int, str] | None = None
    for did in ref_devices:
        profiles = simplemdm.device_profiles(did)
        baseline = profiles if baseline is None else {i: n for i, n in baseline.items() if i in profiles}
    assert baseline is not None
    ui.info(
        f"baseline: {len(baseline)} profile(s) common to {len(ref_devices)} device(s) "
        f"in {ref_gid} ({ref_name})"
    )
    if not baseline:
        raise ReprovisionError(
            f"the {len(ref_devices)} sampled devices in {ref_gid} share no profiles at all — that "
            "group is too heterogeneous to be a baseline. Sample fewer, or pick another group."
        )

    # Targets: one named host, or the group's own membership.
    if hostname:
        device = _resolve_mdm_device(resolve_offline(hostname))
        targets = [(hostname, int(device["id"]))]
        ui.info(f"checking {hostname} (device {targets[0][1]})")
    else:
        name = simplemdm.get_assignment_group(gid).get("attributes", {}).get("name", "?")
        ids = simplemdm.assignment_group_device_ids(gid)
        if not ids:
            raise ReprovisionError(f"group {gid} ({name}) has no devices to check")
        if max_devices:
            ids = ids[:max_devices]
        # A DEP arrival is named "Mac mini" in SimpleMDM, so device ids are the only stable label
        # here. Not worth a GET per device to print a name they mostly don't have yet.
        # Labelled lazily: naming 40 devices up front costs 40 GETs to print ids the operator
        # mostly doesn't need. Only devices with a gap get resolved to a serial.
        targets = [(f"device {i}", i) for i in ids]
        ui.info(f"checking {len(targets)} device(s) in {gid} ({name})")

    # profile id -> (name, devices lacking it)
    gaps: dict[int, tuple[str, list[str]]] = {}
    for label, did in targets:
        have = simplemdm.device_profiles(did)
        for pid, pname in baseline.items():
            if pid not in have:
                gaps.setdefault(pid, (pname, []))[1].append(label)

    total = len(targets)
    if gaps:
        ui.warn(f"{len(gaps)} profile(s) missing from at least one device")
    else:
        ui.ok(f"every checked device has all {len(baseline)} baseline profile(s)")

    # Second, independent question: is any device missing a GROUP its peers are all in? Catches
    # the mis-clicked move, including the app-bearing groups a profile diff cannot see.
    outliers = _membership_outliers(gid, [did for _label, did in targets]) if not hostname else {}
    if outliers:
        ui.warn(f"{len(outliers)} group(s) that most of these devices are in, some are not")
    elif not hostname:
        ui.ok("group membership is consistent across the group")

    if not gaps and not outliers:
        ui.ok("parity: no gap against the prod fleet")
        return

    sections: list[str] = []

    if gaps:
        lines = []
        for pid, (pname, lacking) in sorted(gaps.items(), key=lambda kv: -len(kv[1][1])):
            why = next((story for stem, story in _LOAD_BEARING_PROFILES if stem in pname), "")
            line = f"{pname} (profile {pid}) — missing on {len(lacking)}/{total} device(s)"
            if len(lacking) <= 3:
                line += "\n      " + "\n      ".join(
                    _device_label(d) for _l, d in targets if _l in lacking
                )
            if why:
                line += f"\n      ^ {why}"
            lines.append(line)
        sections.append(
            f"profile parity gap against {ref_gid} ({ref_name}):\n  - " + "\n  - ".join(lines)
        )

    if outliers:
        lines = []
        for other, (oname, missing) in sorted(outliers.items(), key=lambda kv: -len(kv[1][1])):
            lines.append(
                f"{oname} ({other}) — {total - len(missing)}/{total} of these devices are in it, "
                f"{len(missing)} are not:\n      "
                + "\n      ".join(_device_label(d) for d in missing[:5])
            )
        sections.append(
            "group membership outliers — these look MOVED rather than ADDED:\n  - "
            + "\n  - ".join(lines)
        )

    raise ReprovisionError(
        "\n\n".join(sections)
        + "\n\nFix by attaching the profile to the group in SimpleMDM (a profile reaches devices "
        "via its own `groups` relationship), or by ADDING the device to the groups it is missing. "
        "Never MOVE it — a move strips the source group's profiles and apps, which is how this "
        "state arises in the first place."
    )


def step_validate(ctx: HostContext, *, expected_refresh_hz: float | None = None) -> None:
    """Read-only fitness check on a bootstrapped host: is it actually able to run tasks?

    This fills the gap the quarantine message already promises. `--quarantine-on-register` holds a
    fresh host "pending validation", but nothing validated anything — so the only way a host proved
    itself unfit was by failing real work. macmini-m4-242 destroyed 15 production tasks (mochitest,
    jsreftest, web-platform-tests) at ~43s each before anyone looked, because its KVM presented
    1280x1024@75Hz and mozharness fatally halts a pre-test refresh-rate check at anything but 60Hz.
    Every other signal on that host was perfect: puppet green, sentinel present, worker up, disk
    fine, semaphores byte-identical to a working host.

    Deliberately runs AFTER bootstrap, not as part of preflight: reading the display needs the
    logged-in cltbld session, and on a fresh host cltbld does not exist until puppet creates it. A
    preflight version would silently pass exactly the hosts it was meant to catch.

    Read-only — safe on live workers. Raises NotReadyError (exit 2, "skipped") when the host hasn't
    bootstrapped yet, and ReprovisionError (exit 1) when it has and is unfit.
    """
    s = get_settings()
    want_hz = expected_refresh_hz or s.validate_expected_refresh_hz
    ui.step("VALIDATE", "is this host fit to take work? (read-only)")

    if not ssh.file_exists(ctx.fqdn, SENTINEL):
        raise NotReadyError(
            f"{ctx.hostname}: {SENTINEL} missing — host hasn't finished bootstrapping, nothing to "
            "validate yet"
        )

    problems: list[str] = []

    # The display check first: it's the one that passes every other signal and still eats tasks.
    ui.wire(f"ssh admin@{ctx.hostname} launchctl asuser $(id -u cltbld) … CGDisplayModeGetRefreshRate")
    mode = ssh.display_mode(ctx.fqdn)
    if mode is None:
        # Unknown, not fine. A host whose GUI session we can't reach can't run tests either.
        problems.append(
            "couldn't read the display mode — no cltbld GUI session? Without it mozharness's "
            "pre-test refresh-rate check can't pass either"
        )
    else:
        hz, w, h = mode
        if abs(hz - want_hz) < 0.5:
            ui.ok(f"display {w}x{h} @ {hz:.2f}Hz")
        else:
            problems.append(
                f"display is {w}x{h} @ {hz:.2f}Hz, expected {want_hz:.2f}Hz — mozharness halts every "
                "task on this before running a single test. Usually the KVM isn't set correctly."
            )

    puppet_ok = ssh.run(
        ctx.fqdn,
        "sudo grep -o '\"success\": [a-z]*' /opt/puppet_environments/last_run_metadata.json "
        "2>/dev/null | head -1 | awk '{print $2}'",
        check=False,
    ).stdout.decode(errors="replace").strip()
    if puppet_ok == "true":
        ui.ok("last puppet run succeeded")
    else:
        problems.append(f"last puppet run reported success={puppet_ok or 'unknown'}")

    worker_up = ssh.run(
        ctx.fqdn, "pgrep -f 'start-worker ' >/dev/null && echo up || echo down", check=False
    ).stdout.decode(errors="replace").strip()
    if worker_up == "up":
        ui.ok("generic-worker is running")
    else:
        # Not fatal on its own: these hosts reboot between tasks, so a down worker can just mean
        # we caught it mid-cycle. Report it without failing the host on timing alone.
        ui.warn("generic-worker isn't running right now (may be mid-reboot between tasks)")

    if problems:
        raise ReprovisionError(
            f"{ctx.hostname} is NOT fit to take work:\n  - " + "\n  - ".join(problems)
        )
    ui.ok(f"{ctx.hostname} looks fit — safe to unquarantine")


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


def step_quarantine_on_register(ctx: HostContext, *, max_wait_seconds: int | None = None) -> None:
    """Wait for a fresh worker to appear in Taskcluster, then quarantine it on sight.

    `max_wait_seconds` overrides the default budget. The default is sized for a watch started
    once bootstrap is already finished (the `provision` path), where registration is a minute or
    so away. Started from `add-to-group` the watch instead spans the ENTIRE bootstrap — pkg
    install, puppet, several reboots, sentinel, worker start — which measured ~30 min on wave 1,
    so that caller passes a much larger budget. Getting this wrong is not a harmless timeout: the
    watch would give up before the worker registered and the host would go live unheld, which is
    the exact failure this step exists to prevent.

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

    budget = max_wait_seconds or s.quarantine_on_register_max_wait_seconds
    pools = candidate_pools(ctx.role)
    ui.step("QUARANTINE ON REGISTER", "hold the fresh worker out of the pool the moment it appears")
    ui.wire(f"queue.getWorker {' | '.join(pools)} / {ctx.worker_group} / {ctx.hostname}  (poll)")
    ui.info(f"watch budget {budget}s")

    deadline = time.monotonic() + budget
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
            f"{ctx.hostname} never registered in {' or '.join(pools)} within {budget}s — the "
            "worker didn't come up; check worker-runner and /var/tmp/semaphore/run-buildbot on "
            "the host. NB: the host may still register later and would then be UNHELD."
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
