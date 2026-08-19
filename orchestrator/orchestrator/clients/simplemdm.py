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


def _paginated(path: str, *, limit: int = 100) -> list[dict]:
    """Every page of a SimpleMDM list endpoint, following has_more/starting_after.

    Not optional for correctness: /custom_configuration_profiles returns has_more=True at the
    default page size on this account, so a single unpaginated GET silently reports a partial
    profile set — and a parity check built on a partial set reports missing profiles that are
    simply on page two.
    """
    out: list[dict] = []
    params: dict[str, object] = {"limit": limit}
    while True:
        page_json = _request("GET", path, params=params).json()
        page = page_json.get("data", [])
        out += page
        if not page_json.get("has_more") or not page:
            return out
        params["starting_after"] = page[-1]["id"]


def device_profiles(device_id: int) -> dict[int, str]:
    """Configuration profiles SimpleMDM considers assigned to this device, {id: name}.

    This is the *effective* set, and that is the whole point. Profiles reach a device by several
    independent paths — assignment groups, device groups — and only this endpoint composes them.
    Comparing assignment groups to each other instead is actively misleading: the bootstrap group
    is NOT attached to "Skip Setup Assistant - All Screens" or the FDA "SSH Keygen Wrapper", yet
    its devices hold both, because a DEP arrival is still in the additive DEP Enrollment group.
    A group-level diff therefore flags exactly the two profiles from the m4-214 postmortem as
    missing when they are in fact delivered — the one false alarm this check cannot afford.

    NB: assignment-group records carry no `profiles` relationship at all (verified 2026-08-19:
    the keys are apps / device_groups / devices / media). The link lives on the profile side, as
    `relationships.groups`.
    """
    return {
        int(p["id"]): p.get("attributes", {}).get("name", f"profile {p['id']}")
        for p in _paginated(f"/devices/{device_id}/profiles")
    }


def assignment_groups() -> list[dict]:
    """Every assignment group, with its device membership. One paginated sweep.

    Cheaper and more useful than asking per device: a device record does not list the assignment
    groups it belongs to, so the only way to answer "what groups is this host in?" is to invert
    this. 47 groups on this account as of 2026-08-19.
    """
    return _paginated("/assignment_groups")


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


def apps() -> list[dict]:
    """Every app (pkg) in the account. One paginated sweep."""
    return _paginated("/apps")


def assignment_group_app_ids(group_id: int) -> list[int]:
    """App IDs attached to the group.

    NB: relationship IDs come back as ints here, as with devices.
    """
    rel = get_assignment_group(group_id).get("relationships", {})
    return [int(a["id"]) for a in rel.get("apps", {}).get("data", [])]


def add_app_to_assignment_group(group_id: int, app_id: int) -> None:
    """Attach an app to an assignment group. This is what makes an upload do anything.

    Uploading a pkg and attaching it are separate operations in SimpleMDM, and an unattached app
    is completely inert with nothing surfacing that fact — p_role_tart_worker sat uploaded and
    unattached on 2026-08-19 while the hosts it was built for went on with no role file.

    Production groups are refused, using the same guard as the device writes. Attaching a NEW app
    to a live production group pushes it to every member: 2017918 had 130+ devices taking work.
    That is the "never add the bootstrap pkg to a production group" footgun in its other form, and
    a change with that blast radius should be made in the UI with a human looking at it.
    """
    if group_id in PROTECTED_GROUP_IDS:
        raise ReprovisionError(
            f"refusing to attach app {app_id} to assignment group {group_id} "
            f"({PROTECTED_GROUP_IDS[group_id]}). Attaching an app there pushes it to every member, "
            "mid-task. Attach to the bootstrap/staging group instead, or do it in the UI "
            "deliberately."
        )
    _request("POST", f"/assignment_groups/{group_id}/apps/{app_id}")


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
