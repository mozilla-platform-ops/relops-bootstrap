"""`reprovision group-parity` — do a group's hosts get the profiles a working prod host gets?

This is the check the m4-214 incident (2026-08-12) needed and nobody had. A host missing
"Skip Setup Assistant - All Screens" and the FDA "SSH Keygen Wrapper" came up on the Wi-Fi pane
at first boot, and because a modal Setup Assistant holds focus it presented as *Safari automation
is broken* — hours went into Safari versions, TCC grants and puppet ordering before the actual
cause surfaced. The postmortem's advice was to diff `profiles show -type configuration` against a
known-good host by hand, which needs SSH to a box that by definition may not be reachable.

The load-bearing design decision, pinned below: compare EFFECTIVE per-device profile sets, never
assignment groups to each other. Verified against the live account on 2026-08-19 — the bootstrap
group is not attached to either m4-214 profile, yet its devices hold both, because a DEP arrival
is still in the additive DEP Enrollment group. A group-level diff reports exactly those two as
missing when they are in fact delivered, which would train an operator to ignore this check.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from orchestrator import workflow
from orchestrator.errors import ReprovisionError

BOOTSTRAP_GID = 2417981
PROD_GID = 2017918

SKIP_SETUP = (197155, "System Settings - Skip Setup Assistant - All Screens")
FDA_KEYGEN = (197148, "System Settings - Full Disk Access - SSH Keygen Wrapper")
PPPC = (222765, "TCC - CI Worker Support Binaries (PPPC)")
COMMON = (161696, "System Settings - Desktop & Dock - Disable Click to Show Desktop")


_GROUP_NAMES = {
    PROD_GID: "gecko-t-osx-1500-m4",
    BOOTSTRAP_GID: "gecko-t-osx-1500-m4-bootstrap",
}


def _group(gid):
    return {"id": gid, "attributes": {"name": _GROUP_NAMES.get(gid, f"group-{gid}")}}


def _assignment_group(gid, name, device_ids):
    return {
        "id": gid,
        "attributes": {"name": name},
        "relationships": {"devices": {"data": [{"id": d} for d in device_ids]}},
    }


def _parity(*, ref_devices, target_devices, profiles, hostname=None, all_groups=None, **kw):
    """Drive step_group_parity against a fake fleet. `profiles` maps device id -> {pid: name}.

    `all_groups` is the account-wide assignment-group list the membership pass inverts; default
    empty, so profile-only tests stay about profiles.
    """
    def _ids(gid):
        return ref_devices if gid == PROD_GID else target_devices

    def _dev(did):
        return {"id": did, "attributes": {"name": "Mac mini", "serial_number": f"SER{did}"}}

    with patch("orchestrator.workflow.simplemdm.get_assignment_group", side_effect=_group), \
         patch("orchestrator.workflow.simplemdm.assignment_group_device_ids", side_effect=_ids), \
         patch("orchestrator.workflow.simplemdm.assignment_groups", return_value=all_groups or []), \
         patch("orchestrator.workflow.simplemdm.get_device", side_effect=_dev), \
         patch("orchestrator.workflow.simplemdm.device_profiles",
               side_effect=lambda did: dict(profiles[did])) as dp:
        workflow.step_group_parity(
            group_id=BOOTSTRAP_GID, reference_group_id=PROD_GID, hostname=hostname, **kw
        )
    return dp


# --- the baseline ---


def test_the_baseline_is_the_intersection_not_one_sampled_host():
    """One atypical prod box must not be able to drag a profile into the baseline.

    A union would flag every target host for a profile that only one reference host happens to
    carry; a single sample makes the whole check depend on which device the API listed first.
    """
    profiles = {
        1: dict([COMMON, SKIP_SETUP]),   # reference hosts...
        2: dict([COMMON]),               # ...disagree about SKIP_SETUP
        9: dict([COMMON]),               # target lacks it too — but it isn't in the baseline
    }
    _parity(ref_devices=[1, 2], target_devices=[9], profiles=profiles)  # must not raise


def test_a_profile_every_reference_host_has_and_the_target_lacks_is_a_gap():
    profiles = {
        1: dict([COMMON, PPPC]),
        2: dict([COMMON, PPPC]),
        9: dict([COMMON]),
    }
    with pytest.raises(ReprovisionError, match="CI Worker Support Binaries"):
        _parity(ref_devices=[1, 2], target_devices=[9], profiles=profiles)


def test_full_parity_passes_quietly():
    profiles = {1: dict([COMMON, PPPC]), 9: dict([COMMON, PPPC])}
    _parity(ref_devices=[1], target_devices=[9], profiles=profiles)


# --- the false-alarm this check cannot afford ---


def test_a_profile_delivered_by_another_additive_path_is_NOT_flagged():
    """The bootstrap group carries neither m4-214 profile; its devices get both from DEP Enrollment.

    Because the comparison is per-device and effective, that is correctly a non-event. If this ever
    starts failing, someone has reimplemented the check against assignment-group attachment and it
    will cry wolf on the exact pair from the postmortem.
    """
    profiles = {
        1: dict([COMMON, SKIP_SETUP, FDA_KEYGEN]),
        9: dict([COMMON, SKIP_SETUP, FDA_KEYGEN]),  # via DEP Enrollment, not the bootstrap group
    }
    _parity(ref_devices=[1], target_devices=[9], profiles=profiles)


def test_it_reads_the_effective_set_of_every_target_device():
    profiles = {1: dict([COMMON]), 9: dict([COMMON]), 10: dict([COMMON]), 11: dict([COMMON])}
    dp = _parity(ref_devices=[1], target_devices=[9, 10, 11], profiles=profiles)
    assert {c.args[0] for c in dp.call_args_list} == {1, 9, 10, 11}


# --- the report ---


def test_a_gap_reports_how_many_devices_are_affected():
    profiles = {
        1: dict([COMMON, PPPC]),
        9: dict([COMMON]),
        10: dict([COMMON, PPPC]),
        11: dict([COMMON]),
    }
    with pytest.raises(ReprovisionError, match=r"missing on 2/3 device"):
        _parity(ref_devices=[1], target_devices=[9, 10, 11], profiles=profiles)


@pytest.mark.parametrize(
    "profile,expected",
    [
        (SKIP_SETUP, "Wi-Fi network"),   # the symptom, not the cause — that's how you meet it
        (FDA_KEYGEN, "m4-214"),
        (PPPC, "SIP"),
    ],
)
def test_a_known_load_bearing_profile_carries_its_story(profile, expected):
    """A bare profile name doesn't tell an operator why they should care, or what it'll look like."""
    profiles = {1: dict([COMMON, profile]), 9: dict([COMMON])}
    with pytest.raises(ReprovisionError, match=expected):
        _parity(ref_devices=[1], target_devices=[9], profiles=profiles)


def test_the_fix_it_suggests_never_says_move():
    """A move strips the source group's profiles — the m4-214 root cause. It must not be advice."""
    profiles = {1: dict([COMMON, PPPC]), 9: dict([COMMON])}
    with pytest.raises(ReprovisionError) as e:
        _parity(ref_devices=[1], target_devices=[9], profiles=profiles)
    assert "ADDING" in str(e.value)
    assert "Never MOVE" in str(e.value)


# --- guards ---


def test_max_devices_caps_the_work():
    profiles = {1: dict([COMMON]), 9: dict([COMMON]), 10: dict([COMMON])}
    dp = _parity(ref_devices=[1], target_devices=[9, 10], profiles=profiles, max_devices=1)
    assert {c.args[0] for c in dp.call_args_list} == {1, 9}


def test_comparing_a_group_against_itself_is_refused():
    with pytest.raises(ReprovisionError, match="nothing to compare"):
        workflow.step_group_parity(group_id=PROD_GID, reference_group_id=PROD_GID)


def test_an_empty_reference_group_is_refused():
    with pytest.raises(ReprovisionError, match="no devices"):
        _parity(ref_devices=[], target_devices=[9], profiles={9: dict([COMMON])})


def test_a_reference_group_with_nothing_in_common_is_refused():
    """An intersection of zero isn't "parity achieved" — it means the baseline is meaningless."""
    profiles = {1: dict([COMMON]), 2: dict([PPPC]), 9: {}}
    with pytest.raises(ReprovisionError, match="share no profiles"):
        _parity(ref_devices=[1, 2], target_devices=[9], profiles=profiles)


def test_single_host_mode_resolves_the_device_by_serial():
    """A DEP arrival is named "Mac mini" in SimpleMDM, so the serial is the only join key."""
    profiles = {1: dict([COMMON, PPPC]), 555: dict([COMMON])}
    with patch("orchestrator.workflow.ssh.platform_serial", return_value="W4LT930Y9Q"), \
         patch("orchestrator.workflow.simplemdm.find_device_by_serial", return_value={"id": 555}), \
         pytest.raises(ReprovisionError, match="CI Worker Support Binaries"):
        _parity(
            ref_devices=[1], target_devices=[], profiles=profiles, hostname="macmini-m4-241"
        )


# --- group membership outliers: the mis-clicked MOVE, which a profile diff under-reports ---

DEP_GID = 2017921
SSHKEY_GID = 1514391


def _clean(pids=(COMMON,)):
    return dict(pids)


def test_a_device_missing_a_group_all_its_peers_are_in_is_flagged():
    """Found live 2026-08-19: one device sat in the bootstrap group ALONE.

    It had lost DEP Enrollment, Relops Public SSH Key, Sudoers and Enable SSH — so it was missing
    the admin key too, and every orchestrator step is SSH. A profile-only check reported two
    missing profiles and could not say the host was unreachable by design.
    """
    targets = [9, 10, 11, 12]
    profiles = {1: _clean(), **{d: _clean() for d in targets}}
    groups = [
        _assignment_group(DEP_GID, "DEP Enrollment", [9, 10, 11]),      # 12 is missing
        _assignment_group(SSHKEY_GID, "Relops Public SSH Key", [9, 10, 11]),
    ]
    with pytest.raises(ReprovisionError) as e:
        _parity(ref_devices=[1], target_devices=targets, profiles=profiles, all_groups=groups)
    msg = str(e.value)
    assert "DEP Enrollment" in msg and "Relops Public SSH Key" in msg
    assert "MOVED rather than ADDED" in msg
    assert "SER12" in msg, "the outlier must be identified by serial, not just an id"


def test_a_group_only_a_minority_share_is_not_an_outlier():
    """Half a wave earmarked for a different role is normal variation, not a mis-click."""
    targets = [9, 10, 11, 12]
    profiles = {1: _clean(), **{d: _clean() for d in targets}}
    groups = [_assignment_group(4242, "tart-vm-hosts", [9, 10])]  # 50% < quorum
    _parity(ref_devices=[1], target_devices=targets, profiles=profiles, all_groups=groups)


def test_the_group_being_checked_is_not_reported_against_itself():
    targets = [9, 10, 11]
    profiles = {1: _clean(), **{d: _clean() for d in targets}}
    groups = [_assignment_group(BOOTSTRAP_GID, "gecko-t-osx-1500-m4-bootstrap", targets)]
    _parity(ref_devices=[1], target_devices=targets, profiles=profiles, all_groups=groups)


def test_consistent_membership_and_full_profiles_passes():
    targets = [9, 10, 11]
    profiles = {1: _clean(), **{d: _clean() for d in targets}}
    groups = [_assignment_group(DEP_GID, "DEP Enrollment", targets)]
    _parity(ref_devices=[1], target_devices=targets, profiles=profiles, all_groups=groups)


def test_single_host_mode_skips_the_membership_pass():
    """One host has no peers to be an outlier against; the comparison would be meaningless."""
    profiles = {1: _clean(), 555: _clean()}
    with patch("orchestrator.workflow.ssh.platform_serial", return_value="W4LT930Y9Q"), \
         patch("orchestrator.workflow.simplemdm.find_device_by_serial", return_value={"id": 555}), \
         patch("orchestrator.workflow.simplemdm.assignment_groups") as ag:
        _parity(
            ref_devices=[1], target_devices=[], profiles=profiles, hostname="macmini-m4-241",
            all_groups=[_assignment_group(DEP_GID, "DEP Enrollment", [1])],
        )
    ag.assert_not_called()
