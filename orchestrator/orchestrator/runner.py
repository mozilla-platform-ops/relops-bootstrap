"""
reprovision-runner — the on-network execution side of Hangar's reprovision action.

Hangar (Cloud Run) can't SSH to MDC1, so it only *queues* reprovision jobs. This runner lives
on the VPN (a relops box / MDC1 jump host / operator machine), pulls jobs from Hangar over
authenticated HTTPS — **outbound only, no inbound to MDC1** — runs the real `reprovision` CLI,
and streams progress + outcome back. It is the only component that holds SSH/admin creds; Hangar
holds none. See hangar/docs/reprovision-mdc1-runner-design.md.

Config (env):
  HANGAR_API_URL            e.g. https://hangar.relops.mozilla.com/api  (or http://localhost:8000/api)
  Auth — one of:
    RUNNER_CLIENT_CERT +    mTLS (preferred): PEM client cert + key issued by step-ca. The
    RUNNER_CLIENT_KEY         forge-style LB validates the chain and forwards the identity;
                              no Google/IAP identity needed on this host.
    REPROVISION_RUNNER_TOKEN  shared token (local/dev or a non-mTLS ingress).
  RUNNER_POLL_SECONDS       claim poll interval when the queue is empty (default 10)
  RUNNER_ID                 label for this runner (default: hostname)

Run:  reprovision-runner        (or: python -m orchestrator.runner)

Scope (Phase 2 MVP): executes `reprovision run <host>` (with `--unquarantine` by default, so a
reprovisioned host returns to service automatically — set RUNNER_UNQUARANTINE=false to opt out)
and streams the CLI's stdout lines back as job events. Fine-grained structured step streaming
(via a ui event-emitter) is a later enhancement; the CLI's own safety guards (BST, busy-worker,
DoNotObliterate) apply unchanged.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

import httpx
from concurrent.futures import ThreadPoolExecutor

from .hostnames import validate_short


class Config:
    def __init__(self) -> None:
        self.api = os.environ.get("HANGAR_API_URL", "").rstrip("/")
        self.token = os.environ.get("REPROVISION_RUNNER_TOKEN", "")
        self.client_cert = os.environ.get("RUNNER_CLIENT_CERT", "")
        self.client_key = os.environ.get("RUNNER_CLIENT_KEY", "")
        self.poll = int(os.environ.get("RUNNER_POLL_SECONDS", "10"))
        # How many reprovisions run concurrently. Each is I/O-bound (waiting on reboots /
        # re-enrollment / SSH), so a small pool lets a whole pool of workers reprovision "at
        # once" rather than serially. Default 3 (the 1500 staging pool size); raise via env.
        self.max_concurrent = max(1, int(os.environ.get("RUNNER_MAX_CONCURRENT", "3")))
        # Return the host to service (un-quarantine) at the end of a successful reprovision.
        # Default on: the whole point of a reprovision is to put a fresh worker back in service,
        # so the operator doesn't have to un-quarantine by hand afterwards. quarantine +
        # unquarantine are the same queue.quarantineWorker call, so the runner's existing TC
        # creds already scope it. Set RUNNER_UNQUARANTINE=false to leave hosts quarantined
        # (e.g. to inspect a reprovisioned box before it starts claiming tasks).
        self.unquarantine = os.environ.get("RUNNER_UNQUARANTINE", "true").lower() in ("1", "true", "yes")
        self.runner_id = os.environ.get("RUNNER_ID", socket.gethostname())
        if not self.api:
            sys.exit("set HANGAR_API_URL")
        if not self.token and not (self.client_cert and self.client_key):
            sys.exit("set RUNNER_CLIENT_CERT + RUNNER_CLIENT_KEY (mTLS) or REPROVISION_RUNNER_TOKEN")

    @property
    def headers(self) -> dict[str, str]:
        h = {"X-Reprovision-Runner-Id": self.runner_id}
        if self.token:  # cert-authed runners don't need the token header
            h["X-Reprovision-Runner-Token"] = self.token
        return h

    @property
    def client_kwargs(self) -> dict:
        """httpx.Client kwargs — an mTLS client cert when configured. The server cert is
        Google-managed (public CA), so default system-CA verification applies."""
        if self.client_cert and self.client_key:
            return {"cert": (self.client_cert, self.client_key)}
        return {}


def _claim(client: httpx.Client, cfg: Config) -> dict | None:
    r = client.post(f"{cfg.api}/reprovision/runner/claim", headers=cfg.headers, timeout=30)
    r.raise_for_status()
    return r.json().get("job")


def _event(client: httpx.Client, cfg: Config, job_id: int, message: str) -> None:
    # Best-effort: a dropped progress line must never fail the run.
    try:
        client.post(
            f"{cfg.api}/reprovision/runner/jobs/{job_id}/event",
            headers=cfg.headers,
            json={"message": message[:500]},
            timeout=15,
        )
    except httpx.HTTPError:
        pass


def _complete(client: httpx.Client, cfg: Config, job_id: int, success: bool, detail: str) -> None:
    # A dropped completion wedges the host: the job stays "open" in Hangar and no
    # new reprovision can be enqueued for it. So — unlike _event — this retries
    # with backoff and checks the response, rather than silently succeeding on a
    # transient blip. Hangar also has a stale-job reaper as a backstop.
    last: Exception | None = None
    for attempt in range(5):
        try:
            r = client.post(
                f"{cfg.api}/reprovision/runner/jobs/{job_id}/complete",
                headers=cfg.headers,
                json={"success": success, "detail": detail[:500]},
                timeout=30,
            )
            r.raise_for_status()
            return
        except httpx.HTTPError as e:
            last = e
            time.sleep(min(2 ** attempt, 30))
    print(f"WARNING: could not report completion of job {job_id} after retries: {last}")


def _reprovision_cmd(host: str, unquarantine: bool = False) -> list[str]:
    """The `reprovision` CLI to run, resolved next to *this* runner's interpreter.

    The runner and the CLI are installed in the same venv, so the CLI lives beside
    sys.executable. Resolving it explicitly means it works whether or not the venv
    is "activated" on PATH — e.g. when launched by a LaunchDaemon or over
    `ssh host exec .venv/bin/reprovision-runner`, where PATH has no venv/bin. Falls
    back to a bare PATH lookup for editable/dev installs.
    """
    sibling = os.path.join(os.path.dirname(sys.executable), "reprovision")
    exe = sibling if os.path.exists(sibling) else "reprovision"
    cmd = [exe, "run", host]
    if unquarantine:
        cmd.append("--unquarantine")
    return cmd


def _run_job(client: httpx.Client, cfg: Config, job: dict) -> None:
    job_id, host = job["id"], job["short"]
    # Never trust the control plane's hostname blindly — validate before it reaches
    # the CLI (and, through it, SSH/expect/SimpleMDM). A malformed name fails the job
    # cleanly instead of shelling out. The CLI re-validates in resolve() too.
    try:
        host = validate_short(host)
    except ValueError as e:
        _complete(client, cfg, job_id, False, f"rejected: {e}")
        return
    cmd = _reprovision_cmd(host, cfg.unquarantine)
    _event(client, cfg, job_id, f"runner {cfg.runner_id} starting: {' '.join(cmd[1:])}")
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        _complete(client, cfg, job_id, False, "reprovision CLI not found on the runner host")
        return

    last = ""
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            last = line
            _event(client, cfg, job_id, line)
    rc = proc.wait()
    detail = f"exit {rc}" + (f" — {last}" if last else "")
    _complete(client, cfg, job_id, rc == 0, detail)


def _run_job_guarded(client: httpx.Client, cfg: Config, job: dict) -> None:
    """Run a job and ALWAYS report a terminal outcome, so one job's crash never wedges the
    host (its ReprovisionJob would stay 'open' forever) or the concurrent pool."""
    try:
        _run_job(client, cfg, job)
    except Exception as e:  # noqa: BLE001 — always report a terminal outcome for the job
        _complete(client, cfg, job["id"], False, f"runner error: {e}")


def main() -> None:
    cfg = Config()
    auth = "mTLS cert" if cfg.client_cert else "shared token"
    print(
        f"reprovision-runner [{cfg.runner_id}] → {cfg.api} "
        f"(auth: {auth}, poll {cfg.poll}s, max_concurrent {cfg.max_concurrent})"
    )
    # httpx.Client is thread-safe (pooled connections), so one client is shared across job
    # threads. Claim up to max_concurrent jobs and run them in parallel — a whole pool of
    # workers reprovisions "at once" instead of serially.
    with httpx.Client(**cfg.client_kwargs) as client, \
            ThreadPoolExecutor(max_workers=cfg.max_concurrent, thread_name_prefix="job") as pool:
        active: set = set()
        while True:
            active = {f for f in active if not f.done()}  # reap finished jobs
            while len(active) < cfg.max_concurrent:  # fill idle capacity, FIFO
                try:
                    job = _claim(client, cfg)
                except httpx.HTTPError as e:
                    print(f"claim failed: {e}")
                    break
                if not job:
                    break  # queue empty
                print(f"claimed job {job['id']} → {job['short']} ({len(active) + 1}/{cfg.max_concurrent} active)")
                active.add(pool.submit(_run_job_guarded, client, cfg, job))
            time.sleep(cfg.poll)
            print(f"finished job {job['id']}")


if __name__ == "__main__":
    main()
