"""Hostname validation guards SSH/expect/SimpleMDM/VNC against untrusted input."""
from __future__ import annotations

import pytest

from orchestrator.hostnames import WORKER_DOMAIN, validate_fqdn, validate_short


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("macmini-m4-81", "macmini-m4-81"),
        ("MACMINI-M4-81", "macmini-m4-81"),   # normalised to lowercase
        ("  macmini-m4-9  ", "macmini-m4-9"),  # trimmed
        ("macmini-r8-140", "macmini-r8-140"),
        ("macmini-r8-140-2", "macmini-r8-140-2"),  # optional sub-index
    ],
)
def test_validate_short_accepts_fleet_names(raw, expected):
    assert validate_short(raw) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "foo; rm -rf /",
        "macmini-m4-81; touch x",
        "macmini-m4-$(id)",
        "macmini-m4-`id`",
        "macmini-m4-1\n[exec id]",   # Tcl/expect injection payload
        "macmini-m4-",               # no index
        "macmini-x9-1",              # unknown generation
        "evil.com",
        "macmini-m4-81.attacker.com",  # dotted — short form must have no domain
        "../../etc/passwd",
    ],
)
def test_validate_short_rejects_unsafe(bad):
    with pytest.raises(ValueError):
        validate_short(bad)


def test_validate_fqdn_accepts_worker_domain():
    fqdn = f"macmini-m4-81.{WORKER_DOMAIN}"
    assert validate_fqdn(fqdn) == fqdn


@pytest.mark.parametrize(
    "bad",
    [
        "macmini-m4-81",                       # short form, no domain
        "macmini-m4-81.attacker.com",          # off-domain (VNC creds exfil)
        f"evil.{WORKER_DOMAIN}",               # off-fleet base
        f"$(id).{WORKER_DOMAIN}",              # injection base
        f"macmini-m4-81.evil.{WORKER_DOMAIN}",  # domain not exactly the worker domain
    ],
)
def test_validate_fqdn_rejects_off_fleet_or_off_domain(bad):
    with pytest.raises(ValueError):
        validate_fqdn(bad)
