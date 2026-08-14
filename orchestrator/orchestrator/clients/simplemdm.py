"""
SimpleMDM API client. Wraps just the endpoints reprovision uses.
"""

from __future__ import annotations

import time

import httpx

from ..errors import ReprovisionError
from ..secrets import simplemdm_api_key

BASE = "https://a.simplemdm.com/api/v1"

# Groups this client will never write to, by ID. These are live production assignment groups;
# 2017918 alone had 132 devices taking work as of 2026-08-14. The bootstrap pkg must never be
# attached to one — every member would run the bootstrap, mid-task. Hard-coded rather than
# configurable on purpose: a guard you can override with an env var is not a guard.
PROTECTED_GROUP_IDS = {
    2017918: "gecko-t-osx-1500-m4 — production, 132 live devices",
}


def _auth() -> httpx.BasicAuth:
    return httpx.BasicAuth(simplemdm_api_key(), "")


# Retry budget for SimpleMDM's rate limiter. Every call in this module goes through _request so
# nothing can skip it. This is not hypothetical: at `-j3`, `batch --action mint` did three
# concurrent device lookups and SimpleMDM answered one of them with a 429, which surfaced as a raw
# httpx traceback and failed macmini-m4-244 out of a wave (2026-08-14). `add-to-group` is pure API
# with no SSH to slow it down, so at wave scale it would hit this constantly.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 6
_BACKOFF_BASE_SECONDS = 2.0


def _request(method: str, path: str, **kw: object) -> httpx.Response:
    """One SimpleMDM call, retrying rate-limits and transient 5xx with exponential backoff.

    Honours Retry-After when the server sends it, since guessing longer than instructed just
    wastes wall-clock across a 49-host wave and guessing shorter gets you throttled again.
    """
    last: httpx.Response | None = None
    for attempt in range(_MAX_ATTEMPTS):
        r = httpx.request(method, f"{BASE}{path}", auth=_auth(), timeout=30, **kw)  # type: ignore[arg-type]
        if r.status_code not in _RETRY_STATUSES:
            r.raise_for_status()
            return r
        last = r
        if attempt == _MAX_ATTEMPTS - 1:
            break
        delay = _BACKOFF_BASE_SECONDS * (2**attempt)
        retry_after = r.headers.get("Retry-After")
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                pass
        time.sleep(delay)
    assert last is not None
    raise ReprovisionError(
        f"SimpleMDM {method} {path} still returning {last.status_code} after {_MAX_ATTEMPTS} "
        f"attempts — the API is rate-limiting or down. Lower --concurrency and retry; every "
        f"action is idempotent, so re-running is safe."
    )


def find_device_by_name(name: str) -> dict | None:
    """Returns the device record for the given name (e.g., 'macmini-m4-81'), or None."""
    r = _request("GET", "/devices", params={"search": name})
    for d in r.json().get("data", []):
        if d.get("attributes", {}).get("name") == name:
            return d
    return None


def find_device_by_serial(serial: str) -> dict | None:
    """Returns the device record for a hardware serial, or None.

    Prefer this over find_device_by_name for anything touching fresh hardware. A DEP arrival is
    named `Mac mini` in SimpleMDM until something renames it, so name lookup finds either nothing
    or the wrong box — a search for "Mac mini" returns every unnamed device in the fleet. The
    serial is unique and present from enrollment.
    """
    r = _request("GET", "/devices", params={"search": serial})
    for d in r.json().get("data", []):
        if d.get("attributes", {}).get("serial_number") == serial:
            return d
    return None


def get_device(device_id: int) -> dict:
    return _request("GET", f"/devices/{device_id}").json()["data"]


def get_assignment_group(group_id: int) -> dict:
    return _request("GET", f"/assignment_groups/{group_id}").json()["data"]


def assignment_group_device_ids(group_id: int) -> list[int]:
    """Device IDs currently in the group.

    NB: relationship IDs come back as ints here, unlike most of this API where they're strings.
    """
    rel = get_assignment_group(group_id).get("relationships", {})
    return [int(d["id"]) for d in rel.get("devices", {}).get("data", [])]


def add_device_to_assignment_group(group_id: int, device_id: int) -> None:
    """ADD a device to an assignment group. Purely additive — never moves or unassigns.

    Assignment groups in SimpleMDM are additive by design, and that matters here: *moving* a host
    out of a group strips every profile that group carried. On m4-214 (2026-08-12) a move out of
    the prod group silently removed Skip Setup Assistant and the FDA SSH Keygen Wrapper, and the
    host then hung on the Wi-Fi pane at first boot — which presented as "Safari automation is
    broken" and cost most of a day. There is deliberately no remove/move function in this client.
    """
    if group_id in PROTECTED_GROUP_IDS:
        raise ReprovisionError(
            f"refusing to add device {device_id} to assignment group {group_id} "
            f"({PROTECTED_GROUP_IDS[group_id]}). That group carries the bootstrap trigger to every "
            "member; adding the bootstrap pkg to a live production group would run the bootstrap on "
            "hosts that are mid-task. Add hosts to the bootstrap group instead."
        )
    _request("POST", f"/assignment_groups/{group_id}/devices/{device_id}")


def push_apps(group_id: int) -> None:
    """Tell SimpleMDM to install the group's apps on its members now.

    Membership alone doesn't reliably trigger the managed install on these boxes — MDM check-in is
    often boot-only, so without this the pkg may not land until the next reboot.
    """
    if group_id in PROTECTED_GROUP_IDS:
        raise ReprovisionError(
            f"refusing to push apps to assignment group {group_id} ({PROTECTED_GROUP_IDS[group_id]})"
        )
    _request("POST", f"/assignment_groups/{group_id}/push_apps")


def wipe(device_id: int, *, obliteration_behavior: str = "DoNotObliterate") -> None:
    """
    Erase the device. Default `DoNotObliterate` = EACS-only: if Erase All Content & Settings
    can't run (e.g. no escrowed Bootstrap Token), the erase FAILS rather than falling back to a
    full obliterate. Critical for headless minis: `ObliterateWithWarning` on a box without an
    escrowed BST does a *full* wipe → a long network macOS reinstall the KVM can't even show,
    and possibly a physical DFU restore. Only pass `ObliterateWithWarning`/`Always` when you
    deliberately want a full wipe and can physically recover the machine.
    """
    _request(
        "POST",
        f"/devices/{device_id}/wipe",
        data={
            "obliteration_behavior": obliteration_behavior,
            "disable_activation_lock": "true",
        },
    )


# NB: no script_jobs client. The bootstrap used to be triggered as a SimpleMDM script-job;
# it's now delivered as a signed PKG (managed install) that lands during DEP convergence.
