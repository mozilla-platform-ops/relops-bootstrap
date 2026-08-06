"""Shared exception type for expected, operator-actionable failures.

Anything that raises `ReprovisionError` is a known operational condition with a human-readable,
fix-it message (not signed in to 1Password, off the VPN, host unreachable, BST missing, …).
The CLI turns these into a single clean red line instead of a Python traceback; genuine bugs
(anything that is *not* a ReprovisionError) still traceback so we notice them.
"""

from __future__ import annotations


class ReprovisionError(RuntimeError):
    pass


class NotReadyError(ReprovisionError):
    """The host isn't in a state we're willing to provision from — but nothing is broken.

    Distinct from ReprovisionError so a batch can tell "come back later / go fix the host"
    (wrong OS version, SIP still on, box not up yet) apart from "this attempt failed".
    The CLI exits 2 for these and 1 for real failures, which is what lets `reprovision batch`
    report skipped-vs-failed instead of lumping 55 hosts into one number.
    """
