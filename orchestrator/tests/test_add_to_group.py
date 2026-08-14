"""`reprovision add-to-group` — the SimpleMDM action that triggers a bootstrap.

Membership in the bootstrap assignment group delivers the bootstrap pkg, /etc/puppet_role, the
CLT, the admin key and passwordless sudo in one shot, so this single call is the "go" for a host.
That makes the guards here more interesting than the happy path: an additive-only write (a *move*
strips profiles), a hard refusal on production groups, and idempotency so a re-run after a partial
batch failure is free.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from orchestrator import workflow
from orchestrator.clients import simplemdm
from orchestrator.errors import NotReadyError, ReprovisionError

HOST = "macmini-m4-241"
BOOTSTRAP_GID = 2417981
PROD_GID = 2017918


def _group(*, apps: int = 7, device_ids: tuple[int, ...] = ()) -> dict:
    return {
        "id": BOOTSTRAP_GID,
        "attributes": {"name": "gecko-t-osx-1500-m4-bootstrap"},
        "relationships": {
            "apps": {"data": [{"id": i} for i in range(apps)]},
            "devices": {"data": [{"id": d} for d in device_ids]},
        },
    }


# --- the production-group guard ---


def test_refuses_to_add_a_device_to_a_production_group():
    """The bootstrap pkg on a live prod group would run the bootstrap on every member, mid-task.

    2017918 had 132 devices taking work when this was written.
    """
    with patch("orchestrator.clients.simplemdm._request") as req, \
         pytest.raises(ReprovisionError, match="refusing to add"):
        simplemdm.add_device_to_assignment_group(PROD_GID, 1962219)
    req.assert_not_called()  # the guard must fire BEFORE any HTTP call


def test_refuses_to_push_apps_to_a_production_group():
    with patch("orchestrator.clients.simplemdm._request") as req, \
         pytest.raises(ReprovisionError, match="refusing to push"):
        simplemdm.push_apps(PROD_GID)
    req.assert_not_called()


def test_the_client_exposes_no_way_to_remove_or_move_a_device():
    """A move strips the source group's profiles — m4-214 lost Skip Setup Assistant and FDA that
    way and then hung on the Wi-Fi pane. The safest implementation of "never move" is to not
    ship the verb at all.
    """
    for forbidden in ("remove_device_from_assignment_group", "move_device", "unassign_device"):
        assert not hasattr(simplemdm, forbidden), f"{forbidden} should not exist"


# --- idempotency ---


def test_already_a_member_is_a_no_op():
    ctx = workflow.resolve_offline(HOST)
    with patch("orchestrator.workflow.simplemdm.get_assignment_group", return_value=_group(device_ids=(555,))), \
         patch("orchestrator.workflow.ssh.platform_serial", return_value="W4LT930Y9Q"), \
         patch("orchestrator.workflow.ssh.file_exists", return_value=True), \
         patch("orchestrator.workflow.simplemdm.find_device_by_serial", return_value={"id": 555}), \
         patch("orchestrator.workflow.simplemdm.add_device_to_assignment_group") as add, \
         patch("orchestrator.workflow.simplemdm.push_apps") as push:
        workflow.step_add_to_group(ctx, group_id=BOOTSTRAP_GID)
    add.assert_not_called()
    push.assert_not_called()


def test_adds_and_pushes_when_not_a_member():
    ctx = workflow.resolve_offline(HOST)
    with patch("orchestrator.workflow.simplemdm.get_assignment_group", return_value=_group(device_ids=(999,))), \
         patch("orchestrator.workflow.ssh.platform_serial", return_value="W4LT930Y9Q"), \
         patch("orchestrator.workflow.simplemdm.find_device_by_serial", return_value={"id": 555}), \
         patch("orchestrator.workflow.simplemdm.add_device_to_assignment_group") as add, \
         patch("orchestrator.workflow.simplemdm.push_apps") as push:
        workflow.step_add_to_group(ctx, group_id=BOOTSTRAP_GID)
    add.assert_called_once_with(BOOTSTRAP_GID, 555)
    push.assert_called_once_with(BOOTSTRAP_GID)


# --- failing loudly instead of stranding a host ---


def test_empty_group_is_refused_before_any_write():
    """A group with no apps delivers no pkg. Adding a host to it would look fine, then the host
    would sit until step_wait_for_bootstrap_pkg times out blaming the bootstrap.
    """
    ctx = workflow.resolve_offline(HOST)
    with patch("orchestrator.workflow.simplemdm.get_assignment_group", return_value=_group(apps=0)), \
         patch("orchestrator.workflow.simplemdm.add_device_to_assignment_group") as add, \
         pytest.raises(ReprovisionError, match="no apps attached"):
        workflow.step_add_to_group(ctx, group_id=BOOTSTRAP_GID)
    add.assert_not_called()


def test_unknown_device_is_not_ready_not_a_failure():
    """A host that hasn't finished DEP enrolling isn't broken, it's early — exit 2, so a batch
    reports it as skipped and picks it up on the next sweep.
    """
    ctx = workflow.resolve_offline(HOST)
    with patch("orchestrator.workflow.simplemdm.get_assignment_group", return_value=_group()), \
         patch("orchestrator.workflow.ssh.platform_serial", return_value=""), \
         patch("orchestrator.workflow.simplemdm.find_device_by_name", return_value=None), \
         pytest.raises(NotReadyError, match="not a usable lookup key"):
        workflow.step_add_to_group(ctx, group_id=BOOTSTRAP_GID)


# --- rate-limit backoff (this is what makes the action survive a 49-host wave) ---


def _resp(status: int, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, headers=headers or {}, json={"data": []},
                          request=httpx.Request("GET", "https://a.simplemdm.com/api/v1/devices"))


def test_retries_a_429_then_succeeds():
    calls = [_resp(429), _resp(429), _resp(200)]
    with patch("orchestrator.clients.simplemdm.httpx.request", side_effect=calls) as req, \
         patch("orchestrator.clients.simplemdm.time.sleep") as slept, \
         patch("orchestrator.clients.simplemdm._auth"):
        simplemdm.find_device_by_name(HOST)
    assert req.call_count == 3
    assert slept.call_count == 2
    # exponential, not a flat retry
    assert [c.args[0] for c in slept.call_args_list] == [2.0, 4.0]


def test_honours_retry_after_when_it_is_longer_than_our_backoff():
    with patch("orchestrator.clients.simplemdm.httpx.request",
               side_effect=[_resp(429, {"Retry-After": "30"}), _resp(200)]), \
         patch("orchestrator.clients.simplemdm.time.sleep") as slept, \
         patch("orchestrator.clients.simplemdm._auth"):
        simplemdm.find_device_by_name(HOST)
    assert slept.call_args_list[0].args[0] == 30.0


def test_a_garbage_retry_after_falls_back_to_backoff():
    with patch("orchestrator.clients.simplemdm.httpx.request",
               side_effect=[_resp(429, {"Retry-After": "soon"}), _resp(200)]), \
         patch("orchestrator.clients.simplemdm.time.sleep") as slept, \
         patch("orchestrator.clients.simplemdm._auth"):
        simplemdm.find_device_by_name(HOST)
    assert slept.call_args_list[0].args[0] == 2.0


def test_gives_up_with_an_actionable_error_not_an_httpx_traceback():
    """The 429 that failed m4-244 surfaced as a bare httpx.HTTPStatusError with a link to MDN,
    which says nothing about what to do. Exhausting the budget should name the remedy.
    """
    with patch("orchestrator.clients.simplemdm.httpx.request", return_value=_resp(429)), \
         patch("orchestrator.clients.simplemdm.time.sleep"), \
         patch("orchestrator.clients.simplemdm._auth"), \
         pytest.raises(ReprovisionError, match="rate-limiting or down"):
        simplemdm.find_device_by_name(HOST)


def test_does_not_retry_a_real_client_error():
    """404/403 won't fix themselves; retrying six times just delays the message."""
    with patch("orchestrator.clients.simplemdm.httpx.request", return_value=_resp(404)) as req, \
         patch("orchestrator.clients.simplemdm.time.sleep") as slept, \
         patch("orchestrator.clients.simplemdm._auth"), \
         pytest.raises(httpx.HTTPStatusError):
        simplemdm.find_device_by_name(HOST)
    assert req.call_count == 1
    slept.assert_not_called()


def test_every_client_call_goes_through_the_retrying_wrapper():
    """Guard against a future endpoint being added with a bare httpx.get and no backoff."""
    import inspect

    src = inspect.getsource(simplemdm)
    body = src.split("def _request", 1)[1].split("\ndef ", 1)[1]  # everything after _request
    for verb in ("httpx.get", "httpx.post", "httpx.put", "httpx.delete", "httpx.request"):
        assert verb not in body, f"{verb} used outside _request — it would skip 429 backoff"


# --- device resolution: hostname is NOT a usable key on fresh hardware ---


def test_resolves_by_serial_from_the_host_not_by_hostname():
    """The bug that made wave 1's first batch skip all four hosts.

    A DEP arrival enrolls as `Mac mini`; the hostname is DHCP-assigned and never written into the
    SimpleMDM record. Searching by hostname returns a clean zero hits, so all four hosts reported
    "has it finished DEP enrolling?" while being enrolled, on 15.3, and answering SSH.
    """
    ctx = workflow.resolve_offline(HOST)
    rec = {"id": 1962219, "attributes": {"name": "Mac mini", "serial_number": "W4LT930Y9Q"}}
    with patch("orchestrator.workflow.ssh.platform_serial", return_value="W4LT930Y9Q"), \
         patch("orchestrator.workflow.simplemdm.find_device_by_serial", return_value=rec) as by_serial, \
         patch("orchestrator.workflow.simplemdm.find_device_by_name") as by_name:
        assert workflow._resolve_mdm_device(ctx) is rec
    by_serial.assert_called_once_with("W4LT930Y9Q")
    by_name.assert_not_called()  # name lookup must not be the primary path


def test_falls_back_to_name_when_ssh_is_unavailable():
    """Already-provisioned hosts DO carry their hostname (every r8 shows name='macmini-r8-118'),
    so a re-run against the existing fleet still resolves with no SSH.
    """
    ctx = workflow.resolve_offline(HOST)
    rec = {"id": 477149, "attributes": {"name": HOST}}
    with patch("orchestrator.workflow.ssh.platform_serial", return_value=""), \
         patch("orchestrator.workflow.simplemdm.find_device_by_name", return_value=rec):
        assert workflow._resolve_mdm_device(ctx) is rec


def test_a_reachable_host_with_an_unenrolled_serial_says_so():
    ctx = workflow.resolve_offline(HOST)
    with patch("orchestrator.workflow.ssh.platform_serial", return_value="NOTENROLLED1"), \
         patch("orchestrator.workflow.simplemdm.find_device_by_serial", return_value=None), \
         pytest.raises(NotReadyError, match="isn't in SimpleMDM"):
        workflow._resolve_mdm_device(ctx)


def test_no_ssh_and_no_name_match_explains_why_hostname_cannot_work():
    ctx = workflow.resolve_offline(HOST)
    with patch("orchestrator.workflow.ssh.platform_serial", return_value=""), \
         patch("orchestrator.workflow.simplemdm.find_device_by_name", return_value=None), \
         pytest.raises(NotReadyError, match="not a usable lookup key"):
        workflow._resolve_mdm_device(ctx)


def test_find_device_by_serial_requires_an_exact_match():
    """`search` is fuzzy — searching a serial could return neighbours. Only an exact
    serial_number match may be returned, or we'd add the wrong physical machine to a group.
    """
    other = {"id": 1, "attributes": {"serial_number": "W4LT930Y9QXX"}}
    want = {"id": 2, "attributes": {"serial_number": "W4LT930Y9Q"}}
    resp = httpx.Response(200, json={"data": [other, want]},
                          request=httpx.Request("GET", "https://a.simplemdm.com/api/v1/devices"))
    with patch("orchestrator.clients.simplemdm._request", return_value=resp):
        assert simplemdm.find_device_by_serial("W4LT930Y9Q") == want
        assert simplemdm.find_device_by_serial("NOPE") is None


# --- quarantine-on-register must be driven from HERE, not from a later provision ---


def _patched(member: bool, pkg: bool = True):
    """Context managers for a step_add_to_group call with no real IO."""
    ids = (555,) if member else (999,)
    return [
        patch("orchestrator.workflow.simplemdm.get_assignment_group", return_value=_group(device_ids=ids)),
        patch("orchestrator.workflow.ssh.platform_serial", return_value="W4LT930Y9Q"),
        patch("orchestrator.workflow.ssh.file_exists", return_value=pkg),
        patch("orchestrator.workflow.simplemdm.find_device_by_serial", return_value={"id": 555}),
        patch("orchestrator.workflow.simplemdm.add_device_to_assignment_group"),
        patch("orchestrator.workflow.simplemdm.push_apps"),
    ]


def _run(member: bool, *, qor: bool, pkg: bool = True):
    import contextlib

    ctx = workflow.resolve_offline(HOST)
    with contextlib.ExitStack() as st:
        for cm in _patched(member, pkg):
            st.enter_context(cm)
        watch = st.enter_context(patch("orchestrator.workflow.step_quarantine_on_register"))
        workflow.step_add_to_group(ctx, group_id=BOOTSTRAP_GID, quarantine_on_register=qor)
    return watch


def test_starts_the_registration_watch_after_adding():
    assert _run(member=False, qor=True).call_count == 1


def test_starts_the_watch_even_when_the_host_was_already_a_member():
    """An already-member host may still be mid-bootstrap and about to register. Returning early
    without watching is how a host goes live unheld.
    """
    assert _run(member=True, qor=True).call_count == 1


def test_no_watch_unless_asked():
    assert _run(member=False, qor=False).call_count == 0


def test_watch_budget_spans_the_whole_bootstrap_not_just_registration():
    """The default budget (900s) is sized for a watch started AFTER bootstrap. From here the watch
    also covers pkg install, puppet, reboots and sentinel — ~30 min on wave 1 — so a default-sized
    budget would expire before there was anything to quarantine, and the host would go live unheld.
    """
    from orchestrator.config import get_settings

    s = get_settings()
    watch = _run(member=False, qor=True)
    budget = watch.call_args.kwargs["max_wait_seconds"]
    assert budget >= s.bootstrap_max_wait_seconds
    assert budget > s.quarantine_on_register_max_wait_seconds


def test_warns_when_a_member_never_received_the_pkg():
    """Membership does not prove push_apps ever ran; this path skips it."""
    import contextlib

    ctx = workflow.resolve_offline(HOST)
    with contextlib.ExitStack() as st:
        for cm in _patched(True, pkg=False):
            st.enter_context(cm)
        warn = st.enter_context(patch("orchestrator.workflow.ui.warn"))
        workflow.step_add_to_group(ctx, group_id=BOOTSTRAP_GID)
    assert warn.call_count == 1
    assert "never ran" in warn.call_args.args[0]


def test_batch_forwards_the_flag_and_extends_the_timeout():
    """A child killed by the batch timeout mid-watch leaves the host live and unheld."""
    from orchestrator import batch as b

    cmd = b._child_cmd("macmini-m4-241", "add-to-group", expected_os="15.3",
                       allow_sip_enabled=True, wait=True, quarantine_on_register=True)
    assert "--quarantine-on-register" in cmd
    plain = b._child_cmd("macmini-m4-241", "add-to-group", expected_os="15.3",
                         allow_sip_enabled=True, wait=True, quarantine_on_register=False)
    assert "--quarantine-on-register" not in plain
