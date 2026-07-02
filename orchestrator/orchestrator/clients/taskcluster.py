"""
Thin Taskcluster client. Wraps just the endpoints we need: quarantine /
unquarantine + worker status polling. Avoids the official taskcluster Python
client to keep the orchestrator dep set small.
"""

from __future__ import annotations

import taskcluster
from taskcluster.exceptions import TaskclusterRestFailure

from ..config import get_settings

# States that mean the worker is still actively handling the task. Anything in
# {completed, failed, exception} means the task is done from the worker's POV.
ACTIVE_RUN_STATES = {"pending", "running", "claimed"}


def _queue() -> "taskcluster.Queue":
    s = get_settings()
    opts: dict = {"rootUrl": s.tc_root_url}
    if s.tc_client_id and s.tc_access_token:
        opts["credentials"] = {"clientId": s.tc_client_id, "accessToken": s.tc_access_token}
    return taskcluster.Queue(opts)


# All TC calls go through the official client: even the per-worker GET and task
# status require Hawk auth (only the worker *list* endpoint is public). Using the
# lib for everything means one auth path and correct Hawk signing throughout.


def get_worker(worker_pool_id: str, worker_group: str, worker_id: str) -> dict:
    """Returns the worker record incl. quarantineUntil + recentTasks (Hawk-authed)."""
    provisioner, worker_type = worker_pool_id.split("/", 1)
    return _queue().getWorker(provisioner, worker_type, worker_group, worker_id)


def get_task_status(task_id: str) -> dict:
    """queue.status(taskId) -> {runs: [...], state: ...}."""
    return _queue().status(task_id)["status"]


def quarantine(worker_pool_id: str, worker_group: str, worker_id: str, until: str) -> dict:
    """
    Set quarantineUntil = ISO timestamp. Use a far-future date to quarantine
    "indefinitely" and call unquarantine() to clear.
    """
    provisioner, worker_type = worker_pool_id.split("/", 1)
    return _queue().quarantineWorker(provisioner, worker_type, worker_group, worker_id, {"quarantineUntil": until})


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
    except TaskclusterRestFailure as e:
        # 404 from a worker that hasn't claimed yet (e.g. just after a wipe) is "not busy."
        if getattr(e, "status_code", None) == 404:
            return False
        raise

    recent = worker.get("recentTasks", [])[:check_recent_n]
    for task_ref in recent:
        task_id = task_ref.get("taskId")
        if not task_id:
            continue
        try:
            status = get_task_status(task_id)
        except TaskclusterRestFailure:
            continue

        # Find the LATEST run that was on this worker (most recent runId first).
        for run in reversed(status.get("runs", [])):
            if run.get("workerId") == worker_id and run.get("workerGroup") == worker_group:
                if run.get("state") in ACTIVE_RUN_STATES:
                    return True
                # Latest run on this worker is done; check the next task.
                break

    return False
