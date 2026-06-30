"""Thin SimpleMDM API client. Only the endpoints we actually use.

SimpleMDM API docs: https://api.simplemdm.com/docs/
Auth: HTTP basic with the API token as username, blank password.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx


class SimpleMDMClient:
    BASE_URL = "https://a.simplemdm.com/api/v1"

    def __init__(self, api_token: bytes | str, *, timeout: float = 30.0) -> None:
        token = api_token.decode() if isinstance(api_token, bytes) else api_token
        self._client = httpx.Client(
            base_url=self.BASE_URL,
            auth=(token, ""),
            timeout=timeout,
            headers={"Accept": "application/json"},
        )

    def list_devices_in_assignment_group(self, assignment_group_id: str) -> list[dict]:
        """List all devices currently in a SimpleMDM Assignment Group.

        SimpleMDM has two grouping concepts: device_groups (static taxonomy)
        and assignment_groups (used for app/script targeting). RelOps fleet
        membership for puppet roles lives in assignment_groups, so that's
        what we walk.

        The /assignment_groups/{id} endpoint returns device IDs in its
        relationships.devices.data; we then GET each device individually for
        hostname + custom attributes. N+1 calls, but N is small (each
        assignment_group has at most a few dozen devices).

        Returns a flat list of {id, hostname, last_seen_at, custom_attributes}.
        """
        # Step 1: enumerate device IDs in the assignment group
        resp = self._client.get(f"/assignment_groups/{assignment_group_id}")
        resp.raise_for_status()
        ag_body = resp.json()
        device_refs = ag_body.get("data", {}).get("relationships", {}).get("devices", {}).get("data", [])
        device_ids = [ref["id"] for ref in device_refs]

        # Step 2: fetch each device's full record
        devices: list[dict] = []
        for device_id in device_ids:
            dev_resp = self._client.get(f"/devices/{device_id}")
            dev_resp.raise_for_status()
            raw = dev_resp.json().get("data", {})
            attrs = raw.get("attributes", {})
            devices.append({
                "id": raw["id"],
                "hostname": attrs.get("name") or attrs.get("device_name") or "",
                "last_seen_at": _parse_iso8601(attrs.get("last_seen_at")),
                "custom_attributes": _flatten_custom_attrs(raw),
                # MDM-state preconditions for BST escrow / future EACS-ability.
                "dep_enrolled": bool(attrs.get("dep_enrolled")),
                "is_user_approved_enrollment": bool(attrs.get("is_user_approved_enrollment")),
                "is_supervised": bool(attrs.get("is_supervised")),
            })
        return devices

    def create_script_job(self, script_id: int, device_id: int) -> int:
        """Trigger script_id on one device by creating a new script_job.

        SimpleMDM model: scripts are templates, script_jobs are one-off
        executions. POST /script_jobs takes form-data with script_id +
        a comma-separated device_ids string. Response carries the
        assigned script_job id.

        This is the only mutating call this service makes. Audit-log on
        every invocation.
        """
        resp = self._client.post(
            "/script_jobs",
            data={"script_id": script_id, "device_ids": str(device_id)},
        )
        resp.raise_for_status()
        return int(resp.json()["data"]["id"])


def _parse_iso8601(s: str | None) -> datetime | None:
    if not s:
        return None
    # SimpleMDM uses "2026-06-26T18:32:11.000Z"
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def _flatten_custom_attrs(raw_device: dict) -> dict[str, str]:
    """Custom attributes come back as a list of {name, value} dicts. Flatten."""
    relationships = raw_device.get("relationships", {})
    attrs_list = relationships.get("custom_attribute_values", {}).get("data", [])
    return {item["attributes"]["name"]: item["attributes"]["value"] for item in attrs_list if "attributes" in item}
