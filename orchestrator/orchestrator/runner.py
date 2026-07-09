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

Scope (Phase 2 MVP): executes `reprovision run <host>` and streams the CLI's stdout lines back
as job events. Fine-grained structured step streaming (via a ui event-emitter) is a later
enhancement; the CLI's own safety guards (BST, busy-worker, DoNotObliterate) apply unchanged.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

import httpx


class Config:
    def __init__(self) -> None:
        self.api = os.environ.get("HANGAR_API_URL", "").rstrip("/")
        self.token = os.environ.get("REPROVISION_RUNNER_TOKEN", "")
        self.client_cert = os.environ.get("RUNNER_CLIENT_CERT", "")
        self.client_key = os.environ.get("RUNNER_CLIENT_KEY", "")
        self.poll = int(os.environ.get("RUNNER_POLL_SECONDS", "10"))
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
    client.post(
        f"{cfg.api}/reprovision/runner/jobs/{job_id}/complete",
        headers=cfg.headers,
        json={"success": success, "detail": detail[:500]},
        timeout=30,
    )


def _run_job(client: httpx.Client, cfg: Config, job: dict) -> None:
    job_id, host = job["id"], job["short"]
    _event(client, cfg, job_id, f"runner {cfg.runner_id} starting: reprovision run {host}")
    try:
        proc = subprocess.Popen(
            ["reprovision", "run", host],
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


def main() -> None:
    cfg = Config()
    auth = "mTLS cert" if cfg.client_cert else "shared token"
    print(f"reprovision-runner [{cfg.runner_id}] → {cfg.api} (auth: {auth}, poll {cfg.poll}s)")
    with httpx.Client(**cfg.client_kwargs) as client:
        while True:
            try:
                job = _claim(client, cfg)
            except httpx.HTTPError as e:
                print(f"claim failed: {e}")
                time.sleep(cfg.poll)
                continue
            if not job:
                time.sleep(cfg.poll)
                continue
            print(f"claimed job {job['id']} → {job['short']}")
            try:
                _run_job(client, cfg, job)
            except Exception as e:  # noqa: BLE001 — always report a terminal outcome for the job
                _complete(client, cfg, job["id"], False, f"runner error: {e}")
            print(f"finished job {job['id']}")


if __name__ == "__main__":
    main()
