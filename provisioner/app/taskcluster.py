"""Thin Taskcluster API client. Only the endpoints we use for guard checks.

We hit the QUEUE API (not worker-manager) because worker-manager only tracks
provisioner-spawned workers; bare-metal hardware shows up only in the queue
side. The queue records track quarantine state, last task, and expiry.

No auth headers — these are public read-only TC endpoints.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx


class TaskclusterClient:
    def __init__(self, root_url: str, *, timeout: float = 15.0) -> None:
        self._root = root_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout, headers={"Accept": "application/json"})

    @staticmethod
    def _worker_type(role: str) -> str:
        """Convention from ronin_puppet hiera: TC worker type matches the
        puppet role with underscores swapped for hyphens."""
        return role.replace("_", "-")

    def _fetch_queue_worker(self, role: str, hostname: str) -> dict | None:
        """Query the queue API for one (provisioner, workerType, workerGroup,
        workerId) tuple. We try both mdc1 and mdc2 since we don't know the
        host's actual workerGroup. Returns the raw queue worker record or
        None if not found."""
        worker_type = self._worker_type(role)
        for group in ("mdc1", "mdc2"):
            url = (
                f"{self._root}/api/queue/v1/provisioners/releng-hardware"
                f"/worker-types/{worker_type}/workers/{group}/{hostname}"
            )
            resp = self._client.get(url)
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            return resp.json()
        return None

    def get_worker(self, role: str, hostname: str) -> dict | None:
        """Return parsed worker state, or None if TC has no record.

        Queue API record carries: quarantineUntil, firstClaim, expires,
        recentTasks, actions. No 'lastChecked' field — workers register
        once and stay until expiry; checking in isn't a separate signal."""
        raw = self._fetch_queue_worker(role, hostname)
        if raw is None:
            return None
        # 'lastChecked' is our internal abstraction; for queue workers we
        # treat the most recent task time as the freshness proxy.
        return {
            "lastChecked": _most_recent_run_time(raw),
            "quarantineUntil": _parse_iso8601(raw.get("quarantineUntil")),
            "state": "registered",
        }

    def most_recent_task_completion(self, role: str, hostname: str) -> datetime | None:
        """Approximate timestamp of the most recent run by this worker, or
        None if no record / no recent tasks. The queue API doesn't surface
        per-run timestamps directly; recentTasks is a recency-ordered list
        of {taskId, runId}, so the existence of any entries means 'this
        worker has been active in TC's rolling window' which is the signal
        we actually care about for the production-protection guard."""
        raw = self._fetch_queue_worker(role, hostname)
        if raw is None:
            return None
        return _most_recent_run_time(raw)


def _most_recent_run_time(raw: dict) -> datetime | None:
    """Queue worker doesn't have a 'lastChecked' field. If recentTasks is
    non-empty, the worker has been active recently — return 'now' as a
    conservative proxy so the freshness guards trip. If empty, return
    None (worker is truly idle, eligible for re-provisioning)."""
    if raw.get("recentTasks"):
        return datetime.now(timezone.utc)
    return None


def _parse_iso8601(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
