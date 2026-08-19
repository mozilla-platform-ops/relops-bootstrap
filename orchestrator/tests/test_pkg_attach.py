"""`reprovision pkg-audit` / `pkg-attach` — making an uploaded pkg actually reach hosts.

Uploading a pkg and attaching it to an assignment group are separate operations in SimpleMDM, and
an unattached app is completely inert with nothing surfacing that fact. On 2026-08-19
`p_role_tart_worker` was signed, uploaded, and left unattached while the eight hosts it was built
for carried no role file — caught only because someone queried the group's app list by hand.

The interesting cases here are the guards, not the happy path: a production group must be refused
(attaching an app there pushes it to every member mid-task), the push must stay opt-in, and success
must be proven by re-reading the group rather than by trusting the POST.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from orchestrator import workflow
from orchestrator.clients import simplemdm
from orchestrator.errors import ReprovisionError

BOOTSTRAP_GID = 2417981
TART_GID = 2105807
PROD_GID = 2017918
APP = 690299


def _app(app_id=APP, name="p_role_tart_worker-1.0-signed", bundle="com.github.munki.pkg.p_role_tart_worker"):
    return {"id": app_id, "attributes": {"name": name, "bundle_identifier": bundle}}


def _group(gid, name, app_ids=(), device_ids=()):
    return {
        "id": gid,
        "attributes": {"name": name},
        "relationships": {
            "apps": {"data": [{"id": a} for a in app_ids]},
            "devices": {"data": [{"id": d} for d in device_ids]},
        },
    }


# --- the production-group guard ---


def test_refuses_to_attach_an_app_to_a_production_group():
    """A new app on a live prod group is pushed to every member, mid-task. 2017918 had 130+."""
    with patch("orchestrator.clients.simplemdm._request") as req, \
         pytest.raises(ReprovisionError, match="refusing to attach"):
        simplemdm.add_app_to_assignment_group(PROD_GID, APP)
    req.assert_not_called()  # the guard fires BEFORE any HTTP call


# --- attach ---


def _attach(*, group_apps, catalog=None, push=False, gid=TART_GID, spec=str(APP)):
    group = _group(gid, "Tart", app_ids=group_apps, device_ids=(1, 2, 3))
    calls = {"add": 0, "push": 0}

    def _add(g, a):
        calls["add"] += 1
        group["relationships"]["apps"]["data"].append({"id": a})

    with patch("orchestrator.workflow.simplemdm.apps", return_value=catalog or [_app()]), \
         patch("orchestrator.workflow.simplemdm.get_assignment_group", return_value=group), \
         patch("orchestrator.workflow.simplemdm.assignment_group_device_ids", return_value=[1, 2, 3]), \
         patch("orchestrator.workflow.simplemdm.assignment_group_app_ids",
               side_effect=lambda g: [int(a["id"]) for a in group["relationships"]["apps"]["data"]]), \
         patch("orchestrator.workflow.simplemdm.add_app_to_assignment_group", side_effect=_add), \
         patch("orchestrator.workflow.simplemdm.push_apps", side_effect=lambda g: calls.__setitem__("push", calls["push"] + 1)):
        workflow.step_pkg_attach(spec, group_id=gid, push=push)
    return calls


def test_attaches_when_the_group_does_not_carry_it():
    calls = _attach(group_apps=(600264,))
    assert calls["add"] == 1


def test_already_attached_is_a_no_op():
    calls = _attach(group_apps=(600264, APP))
    assert calls == {"add": 0, "push": 0}


def test_push_is_opt_in():
    """push_apps re-pushes EVERY app to EVERY member, including postinstalls.

    Defaulting it on would mean an innocuous-looking attach re-runs the bootstrap pkg on every
    member of the bootstrap group, mid-task.
    """
    assert _attach(group_apps=(600264,), push=False)["push"] == 0
    assert _attach(group_apps=(600264,), push=True)["push"] == 1


def test_verification_rereads_the_group_and_fails_when_the_attach_did_not_stick():
    """A 2xx from the POST is not evidence the group changed."""
    group = _group(TART_GID, "Tart", app_ids=(600264,), device_ids=(1,))
    with patch("orchestrator.workflow.simplemdm.apps", return_value=[_app()]), \
         patch("orchestrator.workflow.simplemdm.get_assignment_group", return_value=group), \
         patch("orchestrator.workflow.simplemdm.assignment_group_device_ids", return_value=[1]), \
         patch("orchestrator.workflow.simplemdm.assignment_group_app_ids", return_value=[600264]), \
         patch("orchestrator.workflow.simplemdm.add_app_to_assignment_group"), \
         patch("orchestrator.workflow.simplemdm.push_apps"), \
         pytest.raises(ReprovisionError, match="still does not list app"):
        workflow.step_pkg_attach(str(APP), group_id=TART_GID)


# --- app resolution ---


def test_resolves_an_app_by_name_substring():
    calls = _attach(group_apps=(600264,), spec="tart_worker")
    assert calls["add"] == 1


def test_resolves_an_app_by_bundle_identifier():
    calls = _attach(group_apps=(600264,), spec="munki.pkg.p_role_tart_worker")
    assert calls["add"] == 1


def test_an_ambiguous_name_lists_the_candidates_instead_of_guessing():
    catalog = [_app(1, "p_role_a", "com.x.p_role_a"), _app(2, "p_role_b", "com.x.p_role_b")]
    with patch("orchestrator.workflow.simplemdm.apps", return_value=catalog), \
         pytest.raises(ReprovisionError, match="matches 2 apps"):
        workflow.step_pkg_attach("p_role", group_id=TART_GID)


def test_an_unknown_app_is_refused():
    with patch("orchestrator.workflow.simplemdm.apps", return_value=[_app()]), \
         pytest.raises(ReprovisionError, match="no app matching"):
        workflow.step_pkg_attach("nonexistent", group_id=TART_GID)


def test_an_unknown_numeric_id_is_refused_by_id_not_treated_as_a_substring():
    with patch("orchestrator.workflow.simplemdm.apps", return_value=[_app()]), \
         pytest.raises(ReprovisionError, match="no app with id 999"):
        workflow.step_pkg_attach("999", group_id=TART_GID)


# --- the orphan audit ---


def _audit(catalog, groups):
    warned: list[str] = []
    with patch("orchestrator.workflow.simplemdm.apps", return_value=catalog), \
         patch("orchestrator.workflow.simplemdm.assignment_groups", return_value=groups), \
         patch("orchestrator.workflow.ui.warn", side_effect=warned.append), \
         patch("orchestrator.workflow.ui.ok"), patch("orchestrator.workflow.ui.info"), \
         patch("orchestrator.workflow.ui.step"):
        workflow.step_pkg_audit()
    return "\n".join(warned)


def test_audit_names_an_app_no_group_carries():
    """The exact 2026-08-19 failure: uploaded, attached to nothing, silently inert."""
    out = _audit([_app(), _app(600264, "Sudoers", "com.mozilla.pkg.Sudoers")],
                 [_group(TART_GID, "Tart", app_ids=(600264,))])
    assert "p_role_tart_worker" in out
    assert "Sudoers" not in out  # attached, so not an orphan


def test_audit_is_quiet_when_everything_is_attached():
    out = _audit([_app()], [_group(TART_GID, "Tart", app_ids=(APP,))])
    assert out == ""


def test_audit_counts_an_app_attached_to_any_group_as_carried():
    out = _audit([_app()], [_group(BOOTSTRAP_GID, "bootstrap", app_ids=()),
                            _group(TART_GID, "Tart", app_ids=(APP,))])
    assert out == ""
