"""
SimpleMDM API client. Wraps just the endpoints reprovision uses.
"""

from __future__ import annotations

import httpx

from ..config import get_settings

BASE = "https://a.simplemdm.com/api/v1"


def _auth() -> httpx.BasicAuth:
    return httpx.BasicAuth(get_settings().simplemdm_api_key, "")


def find_device_by_name(name: str) -> dict | None:
    """Returns the device record for the given name (e.g., 'macmini-m4-81'), or None."""
    r = httpx.get(f"{BASE}/devices", auth=_auth(), params={"search": name}, timeout=30)
    r.raise_for_status()
    for d in r.json().get("data", []):
        if d.get("attributes", {}).get("name") == name:
            return d
    return None


def get_device(device_id: int) -> dict:
    r = httpx.get(f"{BASE}/devices/{device_id}", auth=_auth(), timeout=30)
    r.raise_for_status()
    return r.json()["data"]


def wipe(device_id: int, *, obliteration_behavior: str = "ObliterateWithWarning") -> None:
    """EACS-equivalent on Apple Silicon when obliteration_behavior=ObliterateWithWarning."""
    r = httpx.post(
        f"{BASE}/devices/{device_id}/wipe",
        auth=_auth(),
        data={
            "obliteration_behavior": obliteration_behavior,
            "disable_activation_lock": "true",
        },
        timeout=30,
    )
    r.raise_for_status()


def trigger_script(script_id: int, device_id: int) -> int:
    """
    Trigger a script-job for one device. Returns the job id so the caller can poll.
    """
    r = httpx.post(
        f"{BASE}/script_jobs",
        auth=_auth(),
        data={"script_id": script_id, "device_ids": str(device_id)},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["data"]["id"]


def get_script_job(job_id: int) -> dict:
    r = httpx.get(f"{BASE}/script_jobs/{job_id}", auth=_auth(), timeout=30)
    r.raise_for_status()
    return r.json()["data"]
