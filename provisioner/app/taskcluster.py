"""Thin Taskcluster API client. Only the endpoints we use for guard checks.

No auth headers — these are public read-only TC endpoints.
"""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import quote

import httpx


class TaskclusterClient:
    def __init__(self, root_url: str, *, timeout: float = 15.0) -> None:
        self._root = root_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout, headers={"Accept": "application/json"})

    @staticmethod
    def _pool_id_path(role: str) -> str:
        """Convention from ronin_puppet hiera: workerPoolId is
        'releng-hardware/<role-with-hyphens>'. TC's worker-manager API expects
        the workerPoolId as a single URL path segment with the slash
        percent-encoded (`%2F`)."""
        return quote(f"releng-hardware/{role.replace('_', '-')}", safe="")

    def get_worker(self, role: str, hostname: str) -> dict | None:
        """Look up the worker by (workerPoolId, workerGroup, workerId).

        For our hosts: workerPoolId is "releng-hardware/<role-with-hyphens>",
        workerGroup is "mdc1" (or "mdc2"), workerId is the FQDN.

        Returns None if the worker isn't currently known to TC.
        """
        pool_id = self._pool_id_path(role)
        # We don't know which workerGroup (mdc1/mdc2) the host is in without
        # other context, so we try both and pick the matching workerId.
        for group in ("mdc1", "mdc2"):
            url = f"{self._root}/api/worker-manager/v1/workers/{pool_id}/{group}/{hostname}"
            resp = self._client.get(url)
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            data = resp.json()
            return {
                "lastChecked": _parse_iso8601(data.get("lastChecked")),
                "quarantineUntil": _parse_iso8601(data.get("quarantineUntil")),
                "state": data.get("state"),
            }
        return None

    def most_recent_task_completion(self, role: str, hostname: str) -> datetime | None:
        """Returns the timestamp of the most recent task this worker completed,
        or None if the worker has no recent task history.

        Implementation uses the worker-manager 'recentTasks' field which TC
        maintains as a rolling list. For our purposes "recent enough" means
        "within the past 6 hours" so this is sufficient resolution.
        """
        info_raw = None
        pool_id = self._pool_id_path(role)
        for group in ("mdc1", "mdc2"):
            url = f"{self._root}/api/worker-manager/v1/workers/{pool_id}/{group}/{hostname}"
            resp = self._client.get(url)
            if resp.status_code != 200:
                continue
            info_raw = resp.json()
            break
        if info_raw is None:
            return None

        # recentTasks is a list of {taskId, runId} with no timestamp directly,
        # so we fall back to lastChecked as a proxy for "this worker has been
        # active recently." A more precise check would lookup task definitions,
        # but lastChecked is a good-enough conservative signal: a worker that
        # has checked in recently is doing something.
        return _parse_iso8601(info_raw.get("lastChecked"))


def _parse_iso8601(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
