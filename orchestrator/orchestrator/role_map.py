"""
Hostname -> puppet role mapping.

For v1 we use simple prefix matching. Override by loading a JSON/YAML file via
REPROVISION_ROLE_MAP_PATH if a host doesn't follow the convention.
"""

from __future__ import annotations

# Tried in order; first match wins.
DEFAULT_PATTERNS = (
    ("macmini-m4-81", "gecko_t_osx_1500_m4_no_sip"),
    ("macmini-m4-", "gecko_t_osx_1500_m4"),
    ("macmini-r8-140-", "gecko_t_osx_1400_r8"),
    ("macmini-r8-", "gecko_t_osx_1400_r8"),
)


def role_for_hostname(hostname: str) -> str:
    base = hostname.split(".", 1)[0]
    for prefix, role in DEFAULT_PATTERNS:
        if base.startswith(prefix):
            return role
    raise ValueError(f"no role mapping for hostname '{hostname}'")
