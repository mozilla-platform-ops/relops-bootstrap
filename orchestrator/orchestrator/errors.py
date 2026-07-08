"""Shared exception type for expected, operator-actionable failures.

Anything that raises `ReprovisionError` is a known operational condition with a human-readable,
fix-it message (not signed in to 1Password, off the VPN, host unreachable, BST missing, …).
The CLI turns these into a single clean red line instead of a Python traceback; genuine bugs
(anything that is *not* a ReprovisionError) still traceback so we notice them.
"""

from __future__ import annotations


class ReprovisionError(RuntimeError):
    pass
