"""
Thin Taskcluster client. Wraps just the endpoints we need: quarantine /
unquarantine + worker status polling. Avoids the official taskcluster Python
client to keep the orchestrator dep set small.
"""

from __future__ import annotations

import taskcluster
from taskcluster.exceptions import TaskclusterRestFailure

from ..config import get_settings
from ..secrets import tc_credentials

# A run is DONE only in these states; anything else (running / pending / unscheduled) means
# the worker is still on the task. Modelled on the proven android-tools safe_runner
# (worker_health.status.get_hosts_running_jobs), which calls a host busy whenever a recent
# task's state isn't terminal. Checking the complement (rather than an allow-list) avoids
# missing states like "unscheduled".
TERMINAL_RUN_STATES = {"completed", "failed", "exception"}


def _queue() -> "taskcluster.Queue":
    opts: dict = {"rootUrl": get_settings().tc_root_url}
    client_id, access_token = tc_credentials()
    if client_id and access_token:
        opts["credentials"] = {"clientId": client_id, "accessToken": access_token}
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

    Returns False if the worker has no recent tasks or every recent task is in a
    terminal state (or is definitively running on a *different* worker).

    Detection gates on the TASK-level state (like safe_runner) and attributes the
    task to this worker by **workerId only** — NOT the old (workerId AND workerGroup)
    per-run match. During the claim window Taskcluster can report a run as running
    before its workerGroup field is populated, so the stricter match false-negatived a
    live task and a reprovision EACS'd a worker mid-task (incident 2026-07-10). We bias
    to false-POSITIVE (drain/wait longer) over false-NEGATIVE (wipe mid-task), the safe
    direction for a destructive action.
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

        # Task finished (completed/failed/exception) → it isn't holding this worker.
        if status.get("state") in TERMINAL_RUN_STATES:
            continue

        # Non-terminal task (unscheduled/pending/running). Treat it as OURS (busy)
        # unless its latest run is definitively on a *different* worker — e.g. the task
        # was retried elsewhere. workerId alone identifies the physical box; a missing
        # workerId (claim window, or no run yet) counts as ours → busy (safe default).
        runs = status.get("runs") or []
        latest_worker_id = runs[-1].get("workerId") if runs else None
        if latest_worker_id and latest_worker_id != worker_id:
            continue

        return True

    return False
