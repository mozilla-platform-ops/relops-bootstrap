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

ACTIONS = ("preflight", "mint", "os-update", "provision")


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


def _run_one(host: str, cmd: list[str], log_dir: Path, timeout: int) -> HostResult:
    log_path = log_dir / f"{host}.log"
    started = time.monotonic()
    with log_path.open("wb") as log:
        log.write(f"$ {' '.join(cmd)}\n\n".encode())
        log.flush()
        try:
            cp = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, timeout=timeout, check=False)
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
) -> int:
    """Drive `action` across `hosts`. Returns the number of hosts that FAILED (not skipped).

    A caller can therefore treat "some hosts aren't ready yet" as a normal, re-runnable
    outcome while still getting a nonzero exit when something is actually broken.
    """
    if action not in ACTIONS:
        raise ReprovisionError(f"unknown batch action {action!r} — one of {', '.join(ACTIONS)}")

    s = get_settings()
    concurrency = concurrency or s.batch_max_concurrent
    expected_os = expected_os or s.provision_expected_os
    def _cmd_for(host: str) -> list[str]:
        return _child_cmd(
            host,
            action,
            expected_os=expected_os,
            allow_sip_enabled=allow_sip_enabled,
            wait=wait,
            quarantine_on_register=quarantine_on_register,
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
        else:
            per_host_timeout = 1800

    sip_note = "SIP-on allowed" if allow_sip_enabled else "SIP must be disabled"
    ui.step("BATCH", f"{action} × {len(hosts)} host(s), {concurrency} at a time")
    ui.info(f"gate: macOS {expected_os} · {sip_note} · per-host timeout {_mmss(per_host_timeout)}")
    if action == "provision" and not wait:
        ui.info("--no-wait: mint + escrow only; sweep sentinels afterwards")
    if action == "os-update":
        ui.info("launches the upgrade and returns; hosts reboot on their own — sweep with --action preflight after")
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

    log_dir = _log_dir()
    ui.info(f"logs: {log_dir}")

    results: dict[str, HostResult] = {}
    started = time.monotonic()
    done = 0
    total = len(hosts)

    def _work(host: str) -> HostResult:
        return _run_one(host, _cmd_for(host), log_dir, per_host_timeout)

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
