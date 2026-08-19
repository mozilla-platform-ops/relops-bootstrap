"""
Fan `reprovision` out over a list of hosts, with a bounded number in flight.

Built for hardware-refresh batches: 55 fresh M4s auto-enrolling via DEP is not something you
drive one terminal tab at a time, and the previous 25-host batch showed what goes wrong when
you try (an afternoon instead of 20 minutes, three latent gaps stacked up, no per-host record
of what actually happened).

Two design choices worth knowing:

**Each host runs as its own `reprovision` subprocess.** Not threads calling into workflow.py.
A subprocess gives real isolation — one host's traceback, hang or `sys.exit` can't take the
batch down — and it means all the presentation code keeps working unchanged: rich degrades to
plain text when its output isn't a TTY, so each per-host log ends up readable. Threads sharing
one global Console would interleave 55 spinners into noise. This is also how the reprovision
runner already invokes the CLI, so there's one pattern, not two.

**Exit codes carry the classification.** The child exits 2 for NotReadyError (host isn't at the
target OS, SIP still on, box not up yet) and 1 for a real failure. That distinction is the
whole point of a batch report: "38 ok, 15 not ready yet, 2 broken" tells you to re-run in an
hour and go look at two hosts. A single failure count tells you nothing.

Concurrency defaults to 3 (REPROVISION_BATCH_MAX_CONCURRENT). The ceiling is MDC1 network and
imaging throughput, not local CPU — the same reason the runner pins RUNNER_MAX_CONCURRENT=3.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import ui
from .config import get_settings
from .errors import ReprovisionError
from .hostnames import validate_short

# Exit codes the child CLI uses. Kept here as well as in cli.py because this module reads them.
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_NOT_READY = 2

ACTIONS = (
    "preflight",
    "mint",
    "os-update",
    "add-to-group",
    "quarantine-on-register",
    "validate",
    "provision",
)


@dataclass
class HostResult:
    hostname: str
    state: str  # "ok" | "skipped" | "failed"
    detail: str
    seconds: float
    log_path: Path


def read_host_file(path: str) -> list[str]:
    """Parse a host list: one short hostname per line, `#` comments and blanks ignored.

    Every name is validated up front and a bad one aborts the whole batch. Fail-fast is right
    here: a typo in a 55-line file is a file to fix, not 54 hosts to provision while one name
    silently does nothing — and these names flow into SSH argv and an expect script.
    """
    try:
        raw = Path(path).read_text()
    except OSError as e:
        raise ReprovisionError(f"can't read host file {path}: {e}") from None

    hosts: list[str] = []
    problems: list[str] = []
    for lineno, line in enumerate(raw.splitlines(), start=1):
        name = line.split("#", 1)[0].strip()
        if not name:
            continue
        try:
            hosts.append(validate_short(name))
        except ValueError:
            problems.append(f"  line {lineno}: {name!r}")

    if problems:
        raise ReprovisionError(
            "host file has unusable hostnames (nothing ran):\n" + "\n".join(problems)
        )
    if not hosts:
        raise ReprovisionError(f"no hostnames in {path}")

    seen: set[str] = set()
    unique = [h for h in hosts if not (h in seen or seen.add(h))]
    if len(unique) != len(hosts):
        ui.warn(f"host file lists {len(hosts) - len(unique)} duplicate(s) — deduplicated")
    return unique


def _reprovision_exe() -> str:
    """The `reprovision` CLI beside this interpreter (venv-relative), or a bare PATH lookup.

    Same resolution the runner uses, for the same reason: this can run somewhere PATH has no
    venv/bin.
    """
    sibling = os.path.join(os.path.dirname(sys.executable), "reprovision")
    return sibling if os.path.exists(sibling) else "reprovision"


def _child_cmd(
    host: str,
    action: str,
    *,
    expected_os: str,
    allow_sip_enabled: bool,
    wait: bool,
    quarantine_on_register: bool = False,
    watch_max_wait_seconds: int = 0,
) -> list[str]:
    exe = _reprovision_exe()
    if action == "preflight":
        cmd = [exe, "preflight", host]
    elif action == "os-update":
        # No SIP flag: the upgrade doesn't care about SIP state, and the OS gate would be
        # self-defeating -- this IS the step that gets the host to the target version.
        cmd = [exe, "os-update", host]
        if expected_os:
            cmd += ["--expected-os", expected_os]
        return cmd
    elif action == "mint":
        # No gate flags: mint runs BEFORE the Recovery trip and the OS update, because Recovery
        # needs a volume owner and mint is what creates one. Gating it would deadlock the order.
        return [exe, "mint", host]
    elif action == "validate":
        # Read-only fitness check; no gate flags, and it inspects the RUNNING host rather than
        # gating on a target version, so --expected-os doesn't apply.
        return [exe, "validate", host]
    elif action == "quarantine-on-register":
        # Pure Taskcluster polling: no SSH, no SimpleMDM, no gate flags. The budget is passed
        # explicitly because the default is sized for a watch that starts AFTER bootstrap; a watch
        # started at group-add has to span the whole thing, and getting that wrong is not a
        # harmless timeout — the host goes live UNHELD, the exact failure the watch prevents.
        cmd = [exe, "quarantine-on-register", host]
        if watch_max_wait_seconds:
            cmd += ["--max-wait-seconds", str(watch_max_wait_seconds)]
        return cmd
    elif action == "add-to-group":
        # No gate flags: nothing here inspects the OS version or SIP state. It does need SSH,
        # though — the host's serial is the only join key to its SimpleMDM device record, since a
        # DEP arrival enrolls as "Mac mini" and never learns its DHCP-assigned hostname.
        cmd = [exe, "add-to-group", host]
        if quarantine_on_register:
            cmd.append("--quarantine-on-register")
        return cmd
    else:
        cmd = [exe, "provision", host]
        if not wait:
            cmd.append("--no-wait")
        if quarantine_on_register:
            cmd.append("--quarantine-on-register")
    if expected_os:
        cmd += ["--expected-os", expected_os]
    if allow_sip_enabled:
        cmd.append("--allow-sip-enabled")
    return cmd


def _last_meaningful_line(text: str) -> str:
    """The most useful one-liner from a child's log, for the summary table's detail column.

    Prefers the last error/warning line the CLI printed (they carry the reason); falls back to
    the last non-empty line.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for line in reversed(lines):
        if line.startswith(("✗", "▲")):
            return line.lstrip("✗▲ ").strip()
    return lines[-1] if lines else ""


def _prewarm_secret_env() -> dict[str, str]:
    """Resolve every secret ONCE in the parent and hand the values to children via env.

    Each host runs as its own `reprovision` subprocess, and each of those independently resolved
    every secret it needed from 1Password. A 10-host batch therefore fired 10+ `op read` calls
    within a few seconds, and 1Password's desktop integration drops under that: batch-3 mint lost
    **9 of 10 hosts** to "couldn't read op://... " / "timed out reading op://..." on 2026-08-14,
    with nothing wrong with any host. Earlier batches lost preflight and add-to-group runs the same
    way. It was the single biggest source of failed hosts across the wave.

    `_resolve()` prefers a direct env value over the ref, so populating REPROVISION_* here means the
    children never call `op` at all. Best-effort per secret: a batch action that doesn't need the
    Taskcluster credential shouldn't fail because that credential wouldn't resolve, and any secret
    we skip just falls back to the child resolving it itself.

    Trade-off, deliberately taken: this puts secret material in the child's environment, readable by
    other processes of the same user. The alternative is the status quo, where the same material is
    read N times over a channel that demonstrably fails mid-wave. Single-operator laptop use, and
    the children already hold these values in memory.
    """
    from . import secrets as _secrets

    wanted = {
        "REPROVISION_SSH_ADMIN_KEY": _secrets.ssh_admin_key,
        "REPROVISION_SSH_ADMIN_PASSWORD": _secrets.ssh_admin_password,
        "REPROVISION_SIMPLEMDM_API_KEY": _secrets.simplemdm_api_key,
    }
    out: dict[str, str] = {}
    for var, getter in wanted.items():
        try:
            val = getter()
        except Exception as e:  # noqa: BLE001 - any resolution failure is non-fatal here
            ui.warn(f"{var}: not pre-warmed ({type(e).__name__}) — children will resolve it themselves")
            continue
        if val:
            out[var] = val
    try:
        cid, token = _secrets.tc_credentials()
        if cid and token:
            out["REPROVISION_TC_CLIENT_ID"] = cid
            out["REPROVISION_TC_ACCESS_TOKEN"] = token
    except Exception:  # noqa: BLE001 - optional; only quarantine/drain need it
        pass
    return out


def _run_one(
    host: str, cmd: list[str], log_dir: Path, timeout: int, env: dict[str, str] | None = None
) -> HostResult:
    log_path = log_dir / f"{host}.log"
    started = time.monotonic()
    with log_path.open("wb") as log:
        log.write(f"$ {' '.join(cmd)}\n\n".encode())
        log.flush()
        try:
            child_env = {**os.environ, **(env or {})}
            cp = subprocess.run(
                cmd, stdout=log, stderr=subprocess.STDOUT, timeout=timeout, check=False, env=child_env
            )
            code = cp.returncode
        except subprocess.TimeoutExpired:
            log.write(f"\n\nBATCH: killed after {timeout}s\n".encode())
            code = None

    elapsed = time.monotonic() - started
    text = log_path.read_text(errors="replace")

    if code is None:
        return HostResult(host, "failed", f"timed out after {timeout}s", elapsed, log_path)
    if code == EXIT_OK:
        return HostResult(host, "ok", "", elapsed, log_path)
    state = "skipped" if code == EXIT_NOT_READY else "failed"
    return HostResult(host, state, _last_meaningful_line(text) or f"exit {code}", elapsed, log_path)


def _log_dir() -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = Path.home() / ".local" / "state" / "reprovision" / f"batch-{stamp}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _mmss(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}"


def _write_roster(log_dir: Path, hosts: list[str]) -> Path:
    """Record which hosts are (or may be) in the group, so the watch phase is re-runnable.

    A file rather than a printed list: the resume command is then a real command an operator can
    paste at 6pm without re-deriving 49 hostnames from scrollback.
    """
    roster = log_dir / "added.txt"
    roster.write_text(
        "# hosts added to the bootstrap group by this batch — they are bootstrapping NOW.\n"
        "# Re-attach watchers:  reprovision batch <this file> --action quarantine-on-register\n"
        + "".join(f"{h}\n" for h in hosts)
    )
    return roster


def _add_to_group_then_watch(
    hosts: list[str],
    *,
    concurrency: int = 0,
    expected_os: str = "",
    allow_sip_enabled: bool = False,
    per_host_timeout: int = 0,
    dry_run: bool = False,
) -> int:
    """Run the SimpleMDM add and the Taskcluster registration watch as two separate phases.

    `add-to-group --quarantine-on-register` couples an action bound by the SimpleMDM API (three
    calls per host, no SSH to pace it) to a ~30-minute wait bound by Taskcluster. One
    `--concurrency` cannot serve both: raise it for wall-clock and you hammer SimpleMDM, lower it
    for SimpleMDM and 33 hosts serialise into five and a half hours.

    On 2026-08-14 at `-j12` this looked like 5 hosts failing. What actually happened: **12 hosts
    had already been added to the group** — the POST succeeded and the follow-up `push_apps`
    429'd — so killing the batch orphaned 12 live, autonomous bootstraps with no watcher. They
    would have registered and started claiming production work unvalidated. The runbook has
    carried a two-command workaround for this ever since; this is that workaround, built in.

    Phase 1 is clamped to `simplemdm_max_concurrent`, independent of `--concurrency`. Phase 2 runs
    every host at once: a watcher is an idle poll loop, so the binding resource is neither the API
    nor the network but local process count.
    """
    s = get_settings()
    asked = concurrency or s.batch_max_concurrent
    add_j = min(asked, s.simplemdm_max_concurrent)
    watch_budget = s.bootstrap_max_wait_seconds + s.quarantine_on_register_max_wait_seconds

    ui.step("BATCH", f"add-to-group then watch × {len(hosts)} host(s), in two phases")
    ui.info(
        f"phase 1: add to the group, {add_j} at a time (SimpleMDM-bound) · "
        f"phase 2: watch for registration, all {len(hosts)} at once (Taskcluster-bound)"
    )
    if asked > add_j:
        ui.warn(
            f"-j {asked} ignored for the add phase: it is SimpleMDM-bound, clamped to {add_j}. "
            "At -j12 the 429 retry budget blew and left 12 hosts added-but-unwatched "
            "(2026-08-14). Raise REPROVISION_SIMPLEMDM_MAX_CONCURRENT if you really mean it."
        )

    # One directory for both phases, resolved before either starts: an interrupt during phase 1
    # still has to be able to write the resume roster somewhere findable.
    root = None if dry_run else _log_dir()

    added: dict[str, HostResult] = {}
    try:
        add_failed = run_batch(
            hosts,
            action="add-to-group",
            concurrency=add_j,
            expected_os=expected_os,
            allow_sip_enabled=allow_sip_enabled,
            quarantine_on_register=False,
            per_host_timeout=per_host_timeout,
            dry_run=dry_run,
            results_out=added,
            log_dir=None if root is None else root / "add",
            watch_follows=True,
        )
    except KeyboardInterrupt:
        _report_orphans(hosts, added, root)
        raise

    # Watch every host that MIGHT now be in the group — "ok" and "failed" alike. A failed add is
    # precisely the 2026-08-14 case: the add landed and the push 429'd, so the host is in the group
    # and bootstrapping while the batch called it a failure. Only "skipped" (exit 2 — e.g. no SSH,
    # so its serial was never read) means it was definitely never added. Over-watching wastes an
    # idle poll loop; under-watching puts an unvalidated host into production.
    watch = list(hosts) if dry_run else [
        h for h in hosts if h in added and added[h].state != "skipped"
    ]
    if not watch:
        ui.warn("no host was added to the group — nothing to watch")
        return add_failed

    if root is not None:
        ui.info(f"roster: {_write_roster(root, watch)}")

    try:
        watch_failed = run_batch(
            watch,
            action="quarantine-on-register",
            concurrency=len(watch),
            per_host_timeout=per_host_timeout or watch_budget + 300,
            dry_run=dry_run,
            watch_max_wait_seconds=watch_budget,
            log_dir=None if root is None else root / "watch",
        )
    except KeyboardInterrupt:
        _report_orphans(watch, added, root)
        raise

    return add_failed + watch_failed


def _report_orphans(
    hosts: list[str], added: dict[str, HostResult], log_dir: Path | None
) -> None:
    """On interrupt, say which hosts are bootstrapping unwatched — and how to re-attach.

    Silence here is what turned a Ctrl-C into 12 unheld production workers.
    """
    maybe = [h for h in hosts if h not in added or added[h].state != "skipped"]
    if not maybe:
        return
    ui.err(
        f"INTERRUPTED with {len(maybe)} host(s) possibly already in the bootstrap group. The "
        "bootstrap is autonomous: they will finish, register, and claim production work with "
        "nothing holding them."
    )
    if log_dir is None:
        ui.err("re-attach watchers now:  reprovision batch <hosts> --action quarantine-on-register")
        return
    roster = _write_roster(log_dir, maybe)
    ui.err(f"re-attach watchers now:  reprovision batch {roster} --action quarantine-on-register")


def run_batch(
    hosts: list[str],
    *,
    action: str = "provision",
    concurrency: int = 0,
    expected_os: str = "",
    allow_sip_enabled: bool = False,
    wait: bool = True,
    quarantine_on_register: bool = False,
    per_host_timeout: int = 0,
    dry_run: bool = False,
    watch_max_wait_seconds: int = 0,
    results_out: dict[str, HostResult] | None = None,
    log_dir: Path | None = None,
    watch_follows: bool = False,
) -> int:
    """Drive `action` across `hosts`. Returns the number of hosts that FAILED (not skipped).

    A caller can therefore treat "some hosts aren't ready yet" as a normal, re-runnable
    outcome while still getting a nonzero exit when something is actually broken.
    """
    if action not in ACTIONS:
        raise ReprovisionError(f"unknown batch action {action!r} — one of {', '.join(ACTIONS)}")

    # One --concurrency cannot serve both halves of this action; split it. See the function.
    if action == "add-to-group" and quarantine_on_register:
        return _add_to_group_then_watch(
            hosts,
            concurrency=concurrency,
            expected_os=expected_os,
            allow_sip_enabled=allow_sip_enabled,
            per_host_timeout=per_host_timeout,
            dry_run=dry_run,
        )

    s = get_settings()
    concurrency = concurrency or s.batch_max_concurrent
    expected_os = expected_os or s.provision_expected_os
    if action == "quarantine-on-register" and not watch_max_wait_seconds:
        # The child's own default (900s) assumes the watch starts once bootstrap has finished.
        # Reached through a batch it never does — the operator is attaching watchers to hosts that
        # are mid-bootstrap — so size it for the whole thing here instead of making the runbook
        # tell people to export REPROVISION_QUARANTINE_ON_REGISTER_MAX_WAIT_SECONDS=5400.
        watch_max_wait_seconds = s.bootstrap_max_wait_seconds + s.quarantine_on_register_max_wait_seconds
    def _cmd_for(host: str) -> list[str]:
        return _child_cmd(
            host,
            action,
            expected_os=expected_os,
            allow_sip_enabled=allow_sip_enabled,
            wait=wait,
            quarantine_on_register=quarantine_on_register,
            watch_max_wait_seconds=watch_max_wait_seconds,
        )

    # A provision blocks on the bootstrap sentinel, so give the child the sentinel budget plus
    # headroom for preflight/mint/escrow — and for the registration watch when it's asked for.
    # preflight and mint are quick by comparison.
    if not per_host_timeout:
        if action == "provision" and wait:
            per_host_timeout = s.bootstrap_max_wait_seconds + 900
            if quarantine_on_register:
                per_host_timeout += s.quarantine_on_register_max_wait_seconds
        elif action == "os-update":
            # Launch-and-return: staging the script plus the started-cleanly check. The ~14GB
            # download runs detached and outlives this call by design.
            per_host_timeout = 600
        elif action == "quarantine-on-register":
            # Must outlast the child's own watch budget, or the batch kills the watcher and the
            # host it was holding goes live unheld.
            per_host_timeout = watch_max_wait_seconds + 300
        elif action == "validate":
            per_host_timeout = 300
        elif action == "add-to-group":
            # Three SimpleMDM calls plus one SSH round-trip for the serial; seconds, bar the 429
            # backoff. With --quarantine-on-register it instead blocks for the whole bootstrap,
            # so the timeout has to cover that or the child gets killed mid-watch and the host
            # goes live unheld — the failure the flag exists to prevent.
            per_host_timeout = 300
            if quarantine_on_register:
                per_host_timeout = (
                    s.bootstrap_max_wait_seconds + s.quarantine_on_register_max_wait_seconds + 300
                )
        else:
            per_host_timeout = 1800

    # Describe the gates this action ACTUALLY enforces, per _child_cmd. Printing a blanket
    # "gate: macOS 15.3 · SIP must be disabled" on every action was actively misleading: on a
    # `--action mint` run over SIP-on hosts it read as though SIP state had been validated and
    # passed, when mint is handed neither flag and checks neither thing.
    if action == "validate":
        gate_note = "read-only fitness check on already-bootstrapped hosts"
    elif action == "quarantine-on-register":
        gate_note = "Taskcluster-bound watch only — no SimpleMDM call, no SSH, no OS/SIP gate"
    elif action in ("mint", "add-to-group"):
        gate_note = f"no OS/SIP gate — {action} doesn't inspect the running OS"
    elif action == "os-update":
        gate_note = f"target macOS {expected_os} · SIP not checked"
    else:
        sip_note = "SIP-on allowed" if allow_sip_enabled else "SIP must be disabled"
        gate_note = f"gate: macOS {expected_os} · {sip_note}"
    ui.step("BATCH", f"{action} × {len(hosts)} host(s), {concurrency} at a time")
    ui.info(f"{gate_note} · per-host timeout {_mmss(per_host_timeout)}")
    if action == "provision" and not wait:
        ui.info("--no-wait: mint + escrow only; sweep sentinels afterwards")
    if action == "os-update":
        ui.info("launches the upgrade and returns; hosts reboot on their own — sweep with --action preflight after")
    if action == "validate":
        ui.info("exit 2 = not bootstrapped yet (skipped) · exit 1 = bootstrapped but UNFIT to take work")
    if action == "quarantine-on-register":
        ui.info(
            f"watch budget {_mmss(watch_max_wait_seconds)} per host — sized to span a whole "
            "bootstrap, not just the registration gap"
        )
    if action == "add-to-group":
        ui.info("ADD only, never a move; already-member hosts are skipped — then wait for the pkg to land")
        if quarantine_on_register:
            ui.info("watching for registration and quarantining on sight (blocks for the whole bootstrap)")
        elif watch_follows:
            # Don't cry wolf: the operator DID ask to hold these hosts, and phase 2 does it.
            ui.info("watchers are attached in phase 2, immediately after this phase completes")
        else:
            ui.info(
                "NOT quarantining: the bootstrap is autonomous, so these hosts will go live and "
                "claim production work unvalidated — pass --quarantine-on-register for fresh hardware"
            )
    if action == "provision":
        ui.info(
            "hosts will be quarantined on registration"
            if quarantine_on_register
            else "hosts will NOT be quarantined — they claim work as soon as they register"
        )

    if dry_run:
        ui.warn("--dry-run: nothing will be executed")
        for host in hosts:
            ui.wire(" ".join(_cmd_for(host)))
        return 0

    # An explicit dir lets a multi-phase run keep every phase under one batch directory, so the
    # operator has a single place to look and the resume roster sits beside the logs.
    log_dir = log_dir or _log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    ui.info(f"logs: {log_dir}")

    # Resolve secrets once, here, before any child starts. See _prewarm_secret_env: N children
    # each hitting 1Password is what lost 9 of 10 hosts on batch-3 mint.
    secret_env = _prewarm_secret_env()
    if secret_env:
        ui.info(f"pre-warmed {len(secret_env)} credential(s) — children won't call 1Password")

    # The caller's dict when given one, populated as each host completes rather than at the end:
    # _add_to_group_then_watch needs to know which hosts are already in the group even if the
    # operator Ctrl-Cs mid-phase, because those hosts keep bootstrapping either way.
    results: dict[str, HostResult] = results_out if results_out is not None else {}
    started = time.monotonic()
    done = 0
    total = len(hosts)

    def _work(host: str) -> HostResult:
        return _run_one(host, _cmd_for(host), log_dir, per_host_timeout, secret_env)

    # Progress is one line per completion rather than a live table: a batch runs for hours,
    # often over ssh or in a scrollback someone reads later, and an overwritten live region
    # leaves no record of the order things happened in.
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="batch") as pool:
        futures = {pool.submit(_work, host): host for host in hosts}
        for future in _as_completed_quietly(futures):
            host = futures[future]
            result = future.result()
            results[host] = result
            done += 1
            emit = {"ok": ui.ok, "skipped": ui.warn, "failed": ui.err}[result.state]
            detail = f" — {result.detail}" if result.detail else ""
            emit(f"[{done}/{total}] {host}: {result.state} ({_mmss(result.seconds)}){detail}")

    # Report in the order the operator supplied, not completion order — easier to diff against
    # the host file and spot who's missing.
    rows = [
        (h, results[h].state, results[h].detail, _mmss(results[h].seconds))
        for h in hosts
        if h in results
    ]
    ui.batch_summary(rows, log_dir=str(log_dir), elapsed_seconds=time.monotonic() - started)

    failed = sum(1 for r in results.values() if r.state == "failed")
    skipped = sum(1 for r in results.values() if r.state == "skipped")
    if skipped:
        ui.info(f"re-run the same command to pick up the {skipped} skipped host(s) — it's idempotent")
    return failed


def _as_completed_quietly(futures):
    """`concurrent.futures.as_completed`, isolated so tests can drive it deterministically."""
    from concurrent.futures import as_completed

    return as_completed(futures)
