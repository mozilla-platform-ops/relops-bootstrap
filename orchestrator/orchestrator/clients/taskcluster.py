"""
Thin Taskcluster client. Wraps just the endpoints we need: quarantine /
unquarantine + worker status polling. Avoids the official taskcluster Python
client to keep the orchestrator dep set small.
"""

from __future__ import annotations

import httpx

from ..config import get_settings

# States that mean the worker is still actively handling the task. Anything in
# {completed, failed, exception} means the task is done from the worker's POV.
ACTIVE_RUN_STATES = {"pending", "running", "claimed"}


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


def get_task_status(task_id: str) -> dict:
    """GET /task/{taskId}/status — returns {status: {runs: [...], state: ...}}."""
    base = get_settings().tc_root_url
    r = httpx.get(f"{base}/api/queue/v1/task/{task_id}/status", auth=_auth(), timeout=15)
    r.raise_for_status()
    return r.json()["status"]


def quarantine(worker_pool_id: str, worker_group: str, worker_id: str, until: str) -> dict:
    """
    Set quarantineUntil = ISO timestamp. Use a far-future date to quarantine
    "indefinitely" and call unquarantine() to clear.
    """
    # TC Queue quarantineWorker is a PUT to the worker resource itself with
    # {quarantineUntil} — there is no ".../quarantine" subpath (that 404s).
    url = _provisioner_url(worker_pool_id, worker_group, worker_id)
    r = httpx.put(url, auth=_auth(), json={"quarantineUntil": until}, timeout=15)
    r.raise_for_status()
    return r.json()


def unquarantine(worker_pool_id: str, worker_group: str, worker_id: str) -> dict:
    """Clear quarantine — set it to a past date."""
    return quarantine(worker_pool_id, worker_group, worker_id, until="1970-01-01T00:00:00.000Z")


def is_currently_busy(
    worker_pool_id: str,
    worker_group: str,
    worker_id: str,
    *,
    check_recent_n: int = 5,
) -> bool:
    """
    Returns True if the worker has an in-flight task right now.

    Heuristic: look at the worker's `recentTasks` (most-recent-first). For each,
    fetch the task status and find the latest run on THIS worker. If that run is
    still in an active state (pending/running/claimed), the worker is busy.

    We check the top N (default 5) recent tasks because a worker can claim a new
    task that doesn't show up in `recentTasks` immediately, but the previously-
    claimed task's status will still be `running` until the worker reports back.

    Returns False if the worker has no recent tasks or every recent task on this
    worker is in a terminal state.
    """
    try:
        worker = get_worker(worker_pool_id, worker_group, worker_id)
    except httpx.HTTPStatusError as e:
        # 404 from a worker that hasn't claimed yet is "not busy."
        if e.response.status_code == 404:
            return False
        raise

    recent = worker.get("recentTasks", [])[:check_recent_n]
    for task_ref in recent:
        task_id = task_ref.get("taskId")
        if not task_id:
            continue
        try:
            status = get_task_status(task_id)
        except httpx.HTTPStatusError:
            continue

        # Find the LATEST run that was on this worker (most recent runId first).
        for run in reversed(status.get("runs", [])):
            if run.get("workerId") == worker_id and run.get("workerGroup") == worker_group:
                if run.get("state") in ACTIVE_RUN_STATES:
                    return True
                # Latest run on this worker is done; check the next task.
                break

    return False
