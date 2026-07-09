"""
`reprovision demo` — a safe, no-host replay of the full flow, for live demos.

Compresses the real ~15-minute cycle into ~35s of screen time and touches nothing (no ssh,
no SimpleMDM, no Taskcluster). It drives the exact same `ui` layer as the real run, so what
the audience sees on the big screen is the production look, just faster.
"""

from __future__ import annotations

import time

from . import ui

HOST = "macmini-m4-88"
ROLE = "gecko_t_osx_1500_m4"
POOL = "releng-hardware/gecko-t-osx-1500-m4"
DEVICE = 1962176


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
