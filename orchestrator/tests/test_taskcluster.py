"""
Tests for orchestrator.clients.taskcluster.is_currently_busy.

Mocks the client functions because we don't want to hit the real TC API in unit tests.
"""

from __future__ import annotations

from unittest.mock import patch


from orchestrator.clients import taskcluster


WORKER_POOL = "releng-hardware/gecko-t-osx-1500-m4"
WORKER_GROUP = "mdc1"
WORKER_ID = "macmini-m4-81"


def _worker_record(recent_tasks: list[dict]) -> dict:
    return {
        "taskQueueId": WORKER_POOL,
        "workerGroup": WORKER_GROUP,
        "workerId": WORKER_ID,
        "recentTasks": recent_tasks,
    }


def _task_status_running_on_this_worker() -> dict:
    return {
        "state": "running",
        "runs": [
            {
                "runId": 0,
                "state": "running",
                "workerId": WORKER_ID,
                "workerGroup": WORKER_GROUP,
            }
        ],
    }


def _task_status_completed() -> dict:
    return {
        "state": "completed",
        "runs": [
            {
                "runId": 0,
                "state": "completed",
                "workerId": WORKER_ID,
                "workerGroup": WORKER_GROUP,
            }
        ],
    }


def _task_status_running_on_other_worker() -> dict:
    return {
        "state": "running",
        "runs": [
            {
                "runId": 0,
                "state": "running",
                "workerId": "macmini-m4-99",
                "workerGroup": WORKER_GROUP,
            }
        ],
    }


def _task_status_running_this_worker_group_unset() -> dict:
    """Regression for the 2026-07-10 incident: a live run on THIS worker whose workerGroup
    hasn't been populated yet (claim window). The old (workerId AND workerGroup) match
    false-negatived this → the worker read as idle → EACS mid-task."""
    return {
        "state": "running",
        "runs": [
            {
                "runId": 0,
                "state": "running",
                "workerId": WORKER_ID,
                # workerGroup deliberately absent
            }
        ],
    }


def _task_status_pending_no_runs() -> dict:
    """Task assigned to this worker but no run yet (claim window) — treat as busy (safe)."""
    return {"state": "pending", "runs": []}


def test_idle_worker_returns_false():
    with patch.object(taskcluster, "get_worker", return_value=_worker_record([])):
        assert taskcluster.is_currently_busy(WORKER_POOL, WORKER_GROUP, WORKER_ID) is False


def test_worker_with_running_task_returns_true():
    with patch.object(taskcluster, "get_worker", return_value=_worker_record([{"taskId": "T1", "runId": 0}])):
        with patch.object(taskcluster, "get_task_status", return_value=_task_status_running_on_this_worker()):
            assert taskcluster.is_currently_busy(WORKER_POOL, WORKER_GROUP, WORKER_ID) is True


def test_worker_with_completed_recent_task_returns_false():
    with patch.object(taskcluster, "get_worker", return_value=_worker_record([{"taskId": "T1", "runId": 0}])):
        with patch.object(taskcluster, "get_task_status", return_value=_task_status_completed()):
            assert taskcluster.is_currently_busy(WORKER_POOL, WORKER_GROUP, WORKER_ID) is False


def test_recent_task_now_running_on_other_worker_returns_false():
    """The same task was retried on a different worker — this worker is no longer busy with it."""
    with patch.object(taskcluster, "get_worker", return_value=_worker_record([{"taskId": "T1", "runId": 0}])):
        with patch.object(taskcluster, "get_task_status", return_value=_task_status_running_on_other_worker()):
            assert taskcluster.is_currently_busy(WORKER_POOL, WORKER_GROUP, WORKER_ID) is False


def test_running_on_this_worker_with_unpopulated_group_returns_true():
    """Regression (incident 2026-07-10): a live run on this worker with workerGroup not yet set
    must read as BUSY. The old (workerId AND workerGroup) match returned False here and a
    reprovision wiped the worker mid-task."""
    with patch.object(taskcluster, "get_worker", return_value=_worker_record([{"taskId": "T1", "runId": 0}])):
        with patch.object(taskcluster, "get_task_status", return_value=_task_status_running_this_worker_group_unset()):
            assert taskcluster.is_currently_busy(WORKER_POOL, WORKER_GROUP, WORKER_ID) is True


def test_pending_task_no_runs_returns_true():
    """A non-terminal task assigned to this worker with no run yet (claim window) → busy (safe)."""
    with patch.object(taskcluster, "get_worker", return_value=_worker_record([{"taskId": "T1", "runId": 0}])):
        with patch.object(taskcluster, "get_task_status", return_value=_task_status_pending_no_runs()):
            assert taskcluster.is_currently_busy(WORKER_POOL, WORKER_GROUP, WORKER_ID) is True


def test_multiple_recent_tasks_any_running_returns_true():
    """One older task is done, the newest one is still running."""
    recent = [{"taskId": "Tnew", "runId": 0}, {"taskId": "Told", "runId": 0}]
    statuses = {"Tnew": _task_status_running_on_this_worker(), "Told": _task_status_completed()}

    def fake_status(tid):
        return statuses[tid]

    with patch.object(taskcluster, "get_worker", return_value=_worker_record(recent)):
        with patch.object(taskcluster, "get_task_status", side_effect=fake_status):
            assert taskcluster.is_currently_busy(WORKER_POOL, WORKER_GROUP, WORKER_ID) is True


def test_404_worker_returns_false():
    """Worker hasn't claimed anything yet — TC returns 404. Treat as not busy."""
    from taskcluster.exceptions import TaskclusterRestFailure

    err = TaskclusterRestFailure("not found", None)
    err.status_code = 404

    with patch.object(taskcluster, "get_worker", side_effect=err):
        assert taskcluster.is_currently_busy(WORKER_POOL, WORKER_GROUP, WORKER_ID) is False


def _not_found():
    from taskcluster.exceptions import TaskclusterRestFailure

    err = TaskclusterRestFailure("not found", None)
    err.status_code = 404
    return err


PROD_POOL = "releng-hardware/gecko-t-osx-1500-m4"
STAGING_POOL = "releng-hardware/gecko-t-osx-1500-m4-staging"


def test_find_registered_pool_returns_staging_when_registered_there():
    """A host in the -staging pool resolves to staging even though its role maps to prod."""
    def fake_get_worker(pool_id, group, wid):
        if pool_id == STAGING_POOL:
            return _worker_record([])
        raise _not_found()

    with patch.object(taskcluster, "get_worker", side_effect=fake_get_worker):
        assert taskcluster.find_registered_pool([STAGING_POOL, PROD_POOL], WORKER_GROUP, WORKER_ID) == STAGING_POOL


def test_find_registered_pool_falls_through_to_prod():
    """Not in staging (404) but registered in prod → prod."""
    def fake_get_worker(pool_id, group, wid):
        if pool_id == PROD_POOL:
            return _worker_record([])
        raise _not_found()

    with patch.object(taskcluster, "get_worker", side_effect=fake_get_worker):
        assert taskcluster.find_registered_pool([STAGING_POOL, PROD_POOL], WORKER_GROUP, WORKER_ID) == PROD_POOL


def test_find_registered_pool_none_when_registered_nowhere():
    """A fresh host in neither pool → None (caller falls back to the prod pool)."""
    with patch.object(taskcluster, "get_worker", side_effect=_not_found()):
        assert taskcluster.find_registered_pool([STAGING_POOL, PROD_POOL], WORKER_GROUP, WORKER_ID) is None


def test_find_registered_pool_reraises_non_404():
    """A non-404 error (auth, 5xx) must propagate, not be swallowed as 'not in this pool'."""
    from taskcluster.exceptions import TaskclusterRestFailure

    err = TaskclusterRestFailure("server error", None)
    err.status_code = 500
    import pytest

    with patch.object(taskcluster, "get_worker", side_effect=err):
        with pytest.raises(TaskclusterRestFailure):
            taskcluster.find_registered_pool([STAGING_POOL, PROD_POOL], WORKER_GROUP, WORKER_ID)
