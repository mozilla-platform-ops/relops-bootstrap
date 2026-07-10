"""Strict hostname validation for reprovisionable workers.

The runner and CLI must never trust a hostname just because it arrived from the
control plane (Hangar) or an operator's shell. A hostname flows into SSH argv, an
`expect`/Tcl login script, SimpleMDM API calls, and — for the screen agent — a VNC
connect that carries the admin password. We therefore constrain it to the exact
fleet shape at every ingest boundary, so no shell/Tcl metacharacter, whitespace,
or off-fleet / off-domain target can ever reach those sinks. Defense in depth:
Hangar already only sends canonical DB hostnames, but the runner does not assume
its control plane is uncompromised.
"""
from __future__ import annotations

import re

# Fixed MDC1 test domain for CI worker minis; the runner never targets anything else.
WORKER_DOMAIN = "test.releng.mdc1.mozilla.com"

# macmini-m4-<n> / macmini-r8-<n>, with an optional -<n> sub-index (e.g. macmini-r8-140-2).
# Lowercase letters, digits and single hyphens ONLY — deliberately excludes ., :, /, space,
# newline, quotes, backticks, $, [, ], ; and every other metacharacter, which is what
# neutralises shell / Tcl(expect) / argv injection and off-host targeting.
_SHORT_RE = re.compile(r"^macmini-(m4|r8)-\d+(-\d+)?$")


def validate_short(hostname: str) -> str:
    """Return the normalised short hostname, or raise ValueError if it is not a
    well-formed reprovisionable worker name."""
    short = (hostname or "").strip().lower()
    if not _SHORT_RE.match(short):
        raise ValueError(f"refusing unrecognised/unsafe hostname: {hostname!r}")
    return short


def validate_fqdn(fqdn: str) -> str:
    """Validate a full worker FQDN: the base must be a valid worker short name and
    the domain must be the fixed MDC1 test domain — no off-fleet / off-domain target."""
    raw = (fqdn or "").strip().lower()
    base, dot, domain = raw.partition(".")
    if not dot or domain != WORKER_DOMAIN:
        raise ValueError(f"refusing hostname outside {WORKER_DOMAIN}: {fqdn!r}")
    validate_short(base)
    return raw
