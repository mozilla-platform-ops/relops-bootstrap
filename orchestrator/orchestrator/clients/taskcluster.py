"""
Thin Taskcluster client. Wraps just the endpoints we need: quarantine / unquarantine
and worker status polling. Avoids the official taskcluster Python client to keep the
orchestrator dep set small.
"""

from __future__ import annotations

import httpx

from ..config import get_settings


def _auth() -> tuple[str, str]:
    s = get_settings()
    return (s.tc_client_id, s.tc_access_token)


def _provisioner_url(worker_pool_id: str, worker_group: str, worker_id: str) -> str:
    # Worker pools look like "releng-hardware/gecko-t-osx-1500-m4"
    provisioner, worker_type = worker_pool_id.split("/", 1)
    base = get_settings().tc_root_url
    return f"{base}/api/queue/v1/provisioners/{provisioner}/worker-types/{worker_type}/workers/{worker_group}/{worker_id}"


def get_worker(worker_pool_id: str, worker_group: str, worker_id: str) -> dict:
    """Returns the worker record incl. quarantineUntil + recentTasks."""
    url = _provisioner_url(worker_pool_id, worker_group, worker_id)
    r = httpx.get(url, auth=_auth(), timeout=15)
    r.raise_for_status()
    return r.json()


def quarantine(worker_pool_id: str, worker_group: str, worker_id: str, until: str) -> dict:
    """
    Set quarantineUntil = ISO timestamp. Use a far-future date (e.g., 2099-01-01)
    to quarantine "indefinitely" and call unquarantine() to clear.
    """
    url = _provisioner_url(worker_pool_id, worker_group, worker_id) + "/quarantine"
    r = httpx.post(url, auth=_auth(), json={"quarantineUntil": until}, timeout=15)
    r.raise_for_status()
    return r.json()


def unquarantine(worker_pool_id: str, worker_group: str, worker_id: str) -> dict:
    """Clear quarantine — set it to a past date."""
    return quarantine(worker_pool_id, worker_group, worker_id, until="1970-01-01T00:00:00.000Z")


def claimed_task_count(worker_pool_id: str, worker_group: str, worker_id: str) -> int:
    """
    Returns the count of tasks the worker is *currently* working on. Used for the
    drain wait. TC doesn't expose this directly; we approximate by looking at the
    `recentTasks` and checking each one's resolution state.

    TODO: replace with the proper TC API call once we've confirmed the right endpoint
    (the queue's listClaimedWork is the natural one but requires the worker's own
    credentials, which the operator orchestrator wouldn't have).
    """
    # Placeholder — see the get_worker() return for what's available. The cleanest
    # observable signal today is the worker's `lastDateActive` timestamp and the
    # `claimWork` listings from worker telemetry. Reach out to the TC API folks
    # before relying on this in production.
    return 0
