"""
`reprovision demo` — safe, no-host replays of the flows, for live demos.

Compresses real multi-minute cycles into ~35s of screen time each and touches nothing (no ssh,
no SimpleMDM, no Taskcluster). Each replay drives the exact same `ui` layer as the real run, so
what the audience sees on the big screen is the production look, just faster.

Three flows:
  reprovision   — EACS an existing prod host and bring it back (the original)
  provision     — a fresh DEP-enrolled host from the rack to prod
  batch         — the hardware-refresh rollout: readiness sweep, then the provision wave

The `batch` replay is the one to show for a refresh: it demonstrates the skipped-vs-failed
classification on a deliberately mixed set of hosts, which is the behaviour that makes a
55-host run legible rather than a wall of output.
"""

from __future__ import annotations

import time

from . import ui

HOST = "macmini-m4-88"
ROLE = "gecko_t_osx_1500_m4"
POOL = "releng-hardware/gecko-t-osx-1500-m4"
DEVICE = 1962176

FLOWS = ("reprovision", "provision", "batch")


def _beat(tick, msg: str, secs: float) -> None:
    tick(msg)
    time.sleep(secs)


def run_demo(host: str = HOST) -> None:
    from .workflow import _reenroll_phase  # reuse the real phase labels

    ui.banner(host, ROLE, POOL)
    ui.flow(["QUARANTINE", "DRAIN", "WIPE", "RE-ENROLL", "MINT", "ESCROW BST", "BOOTSTRAP"])

    ui.step("QUARANTINE", "tell Taskcluster to stop scheduling tasks on this worker")
    ui.wire(f"PUT queue/v1 quarantineWorker {POOL}/mdc1/{host}")
    time.sleep(0.8)
    ui.ok("quarantined until 2027-07-08")

    ui.step("DRAIN", "let the worker finish its in-flight task (2 consecutive idle polls)")
    ui.wire(f"queue.getWorker {host} → inspect recentTasks run states")
    with ui.waiting("checking for an active task") as tick:
        _beat(tick, "worker busy — waiting for the task to finish", 1.5)
        _beat(tick, "idle 1/2 — confirming", 1.0)
        _beat(tick, "idle 2/2 — confirming", 0.8)
    ui.ok("drained — no task in flight")

    ui.step("WIPE · EACS", "Erase All Content & Settings — DoNotObliterate (fails safe, never obliterates)")
    ui.wire(f"ssh admin@{host} sudo profiles status -type bootstraptoken")
    time.sleep(0.9)
    ui.ok("Bootstrap Token escrowed — EACS can run")
    ui.wire(f"SimpleMDM POST /devices/{DEVICE}/wipe  obliteration_behavior=DoNotObliterate")
    time.sleep(0.8)
    ui.ok("erase command accepted by SimpleMDM")

    ui.step("RE-ENROLL", "erase → reboot → DEP re-enrollment · typically ~5 min")
    ui.wire(f"SimpleMDM GET /devices/{DEVICE}  (poll enrolled_at ≠ 2026-07-08T18:02:11Z)")
    frames = 64
    with ui.waiting("waiting for a fresh enrollment", eta_seconds=300) as tick:
        for i in range(frames + 1):
            frac = i / frames
            tick(_reenroll_phase(frac * 300), frac=frac)  # simulated 5-min clock
            time.sleep(12 / frames)
    ui.ok("re-enrolled — fresh enrolled_at 2026-07-08T18:41:55Z")

    ui.step("MINT SECURETOKEN", "DEP skips Setup Assistant, so admin has no token until an interactive login")
    with ui.waiting("waiting for sshd (relops-ssh pkg lands during convergence)"):
        time.sleep(1.8)
    ui.wire(f"expect: ssh admin@{host} (keyboard-interactive PAM login → grants first SecureToken)")
    with ui.waiting("verifying the SecureToken came up ENABLED") as tick:
        _beat(tick, "not yet — retry 1/6", 1.3)
        _beat(tick, "not yet — retry 2/6", 1.1)
    ui.ok("admin SecureToken ENABLED")

    ui.step("ESCROW BOOTSTRAP TOKEN", "escrow the BST so this box is EACS-able next cycle")
    ui.wire(f"ssh admin@{host} sudo profiles install -type bootstraptoken -user admin -password ••••••")
    time.sleep(1.1)
    ui.ok("Bootstrap Token escrowed to server")

    ui.step("BOOTSTRAP", "the freshly-enrolled host provisions itself — zero operator SSH from here")
    ui.wire("signed bootstrap PKG (managed install) lands via SimpleMDM during DEP convergence")
    ui.wire("→ host fetches its vault.yaml over mTLS from the forge LB (step-ca SCEP client cert)")
    ui.wire(f"→ puppet apply: role {ROLE} — generic-worker, users, TCC perms, launch daemons")
    ui.wire("→ generic-worker self-registers with Taskcluster (Hawk) and starts claiming work")
    ui.wire(f"ssh admin@{host} test -f /var/log/m4-bootstrap-complete  (poll for the sentinel it writes)")
    with ui.waiting("waiting for the bootstrap sentinel") as tick:
        for msg in (
            "SCEP cert in keychain → curl --cert vault-broker (mTLS)",
            "puppet applying gecko_t_osx_1500_m4",
            "generic-worker registering in Taskcluster",
        ):
            tick(msg)
            time.sleep(1.7)
    ui.ok("bootstrap complete — /var/log/m4-bootstrap-complete present")

    ui.summary(host, 928, quarantined=True)  # 15:28 — the representative real-world duration


def run_provision_demo(host: str = "macmini-m4-201") -> None:
    """A fresh DEP-enrolled host, rack to prod. No wipe anywhere in this flow."""
    ui.banner(host, ROLE, "fresh DEP host — no wipe")
    ui.flow(["PREFLIGHT", "MINT", "ESCROW BST", "BOOTSTRAP PKG", "BOOTSTRAP", "QUARANTINE ON REGISTER"])

    ui.step("PREFLIGHT", "verify the host is at its target OS + SIP state before we commit to it")
    ui.wire(f"tcp connect {host}.test.releng.mdc1.mozilla.com:22  (fresh DEP hosts come up over ~15 min)")
    time.sleep(0.9)
    ui.ok("sshd reachable")
    ui.wire(f"ssh admin@{host} sw_vers -productVersion")
    time.sleep(0.7)
    ui.ok("macOS 15.3")
    ui.wire(f"ssh admin@{host} csrutil status")
    time.sleep(0.7)
    ui.ok("SIP disabled")
    ui.info("admin SecureToken: DISABLED (mint will grant it if needed)")
    ui.info("Bootstrap Token escrowed: no — escrow step will fix")
    ui.info("bootstrap pkg: landed (installed by bootstrap-group membership)")

    ui.step("MINT SECURETOKEN", "DEP skips Setup Assistant, so admin has no token until an interactive login")
    ui.wire(f"expect: ssh admin@{host} (keyboard-interactive PAM login → grants first SecureToken)")
    with ui.waiting("verifying the SecureToken came up ENABLED") as tick:
        _beat(tick, "not yet — retry 1/6", 1.2)
    ui.ok("admin SecureToken ENABLED")

    ui.step("ESCROW BOOTSTRAP TOKEN", "escrow the BST so this box is EACS-able next cycle")
    ui.wire(f"ssh admin@{host} sudo profiles install -type bootstraptoken -user admin -password ••••••")
    time.sleep(1.0)
    ui.ok("Bootstrap Token escrowed to server")

    ui.step("BOOTSTRAP PKG", "confirm the signed pkg landed — i.e. the host is in the bootstrap group")
    ui.wire(f"ssh admin@{host} test -f /usr/local/sbin/m4-bootstrap.sh")
    time.sleep(0.8)
    ui.ok("bootstrap pkg present — the host will provision itself")

    ui.step("BOOTSTRAP", "the freshly-enrolled host provisions itself — zero operator SSH from here")
    ui.wire("→ host fetches its vault.yaml over mTLS from the forge LB (step-ca SCEP client cert)")
    ui.wire(f"→ puppet apply: role {ROLE} — generic-worker, users, TCC perms, launch daemons")
    with ui.waiting("waiting for the bootstrap sentinel") as tick:
        for msg in (
            "SCEP cert in keychain → curl --cert vault-broker (mTLS)",
            "puppet applying gecko_t_osx_1500_m4",
            "safari remote-automation enabled, semaphores written",
        ):
            _beat(tick, msg, 1.6)
    ui.ok("bootstrap complete — /var/log/m4-bootstrap-complete present")

    ui.step("QUARANTINE ON REGISTER", "hold the fresh worker out of the pool the moment it appears")
    ui.wire(f"queue.getWorker {POOL}-staging | {POOL} / mdc1 / {host}  (poll)")
    with ui.waiting("waiting for the worker to register with Taskcluster") as tick:
        _beat(tick, "not in a pool yet — worker-runner starts generic-worker after the sentinel", 1.6)
    ui.ok(f"registered in {POOL}")
    ui.wire(f"PUT queue/v1 quarantineWorker {POOL}/mdc1/{host}")
    time.sleep(0.7)
    ui.ok("quarantined until 2027-08-06")

    ui.provisioned(host, 1_142, waited=True, quarantined=True)  # 19:02, representative


# A deliberately mixed set: the point of the batch replay is that the report separates
# "not ready yet, re-run" from "actually broken", which is what makes 55 hosts legible.
_SWEEP = [
    ("macmini-m4-201", "ok", "", "0:06"),
    ("macmini-m4-202", "ok", "", "0:05"),
    ("macmini-m4-203", "skipped", "SIP is not disabled (enabled) — disable SIP in Recovery first", "0:05"),
    ("macmini-m4-204", "ok", "", "0:06"),
    ("macmini-m4-205", "skipped", "macOS 15.2, expected 15.3 — let the MDM in-place update finish", "0:05"),
    ("macmini-m4-206", "skipped", "sshd not reachable within 60s — still converging or powered off", "1:00"),
]

_WAVE = [
    ("macmini-m4-201", "ok", "", "18:41"),
    ("macmini-m4-202", "ok", "", "19:07"),
    ("macmini-m4-204", "skipped", "bootstrap pkg hasn't landed — is this host in the bootstrap group?", "5:12"),
]


def run_batch_demo(_host: str = "") -> None:
    """The hardware-refresh rollout: readiness sweep, then the provision wave."""
    ui.banner("55-host refresh", ROLE, POOL)

    ui.step("BATCH", "preflight × 6 host(s), 3 at a time")
    ui.info("gate: macOS 15.3 · SIP must be disabled · per-host timeout 30:00")
    ui.info("logs: ~/.local/state/reprovision/batch-20260806-142330")
    for i, (host, state, detail, dur) in enumerate(_SWEEP, start=1):
        time.sleep(0.5)
        emit = {"ok": ui.ok, "skipped": ui.warn, "failed": ui.err}[state]
        emit(f"[{i}/{len(_SWEEP)}] {host}: {state} ({dur})" + (f" — {detail}" if detail else ""))
    ui.batch_summary(_SWEEP, log_dir="~/.local/state/reprovision/batch-20260806-142330", elapsed_seconds=131)

    ui.info("read-only — nothing was changed. Move the passing hosts into the bootstrap group, then:")
    time.sleep(1.2)

    ui.step("BATCH", "provision × 3 host(s), 3 at a time")
    ui.info("gate: macOS 15.3 · SIP must be disabled · per-host timeout 90:00")
    ui.info("hosts will be quarantined on registration")
    for i, (host, state, detail, dur) in enumerate(_WAVE, start=1):
        time.sleep(1.4)
        emit = {"ok": ui.ok, "skipped": ui.warn, "failed": ui.err}[state]
        emit(f"[{i}/{len(_WAVE)}] {host}: {state} ({dur})" + (f" — {detail}" if detail else ""))
    ui.batch_summary(_WAVE, log_dir="~/.local/state/reprovision/batch-20260806-145012", elapsed_seconds=1_147)
    ui.info("re-run the same command to pick up the 1 skipped host(s) — it's idempotent")


def run(flow: str = "reprovision", host: str = "") -> None:
    """Dispatch to a replay by name. Unknown names fail loudly rather than silently defaulting.

    An empty `host` lets each flow use its own representative default, so `--host` stays
    optional without one flow's default leaking into another's.
    """
    if flow not in FLOWS:
        raise ValueError(f"unknown demo flow {flow!r} — one of {', '.join(FLOWS)}")
    fn = {"reprovision": run_demo, "provision": run_provision_demo, "batch": run_batch_demo}[flow]
    fn(host) if host else fn()
