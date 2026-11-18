"""`MESHY_CAPABILITIES`, the object-capability tokens flag (B07, docs/DIVERGENCES.md 040).

meshychat/settings.py parses it while it loads. It lives here, not in apps/memoryskills,
because importing any module of that package first runs the package's __init__, which
imports handlers, permissions, validation and psycopg: settings would load them half
configured, and a module-level settings read anywhere in that chain would become a circular
import at every startup (slice 4's review F6). This module imports nothing of the project.
apps/memoryskills/capability_keys.py re-exports every name here.
"""
from __future__ import annotations

ENABLE_ENV = "MESHY_CAPABILITIES"

#: MESHYCHAT_DPOP_ENFORCE's vocabulary (apps/authapi/config.py), so one spelling means one thing.
FLAG_TRUE = ("1", "true", "yes", "on")
FLAG_FALSE = ("0", "false", "no", "off")


class CapabilityConfigError(ValueError):
    """MESHY_CAPABILITIES is not a boolean. Raised while settings.py loads."""


def parse_flag(raw) -> bool | None:
    """One strict boolean parse for the project's environment switches: `raw` is the
    variable's value, or None when it is unset. Unset or blank is False; a FLAG_TRUE spelling
    is True and a FLAG_FALSE spelling is False, stripped and in any case; anything else is
    None, and the caller refuses with its own error class and its own message.

    It replaces the parse that `enabled_from_env` below, apps/core/mesh_unversioned.py's
    `accept_from_env` and apps/core/egress.py's `allow_loopback_from_env` each spelled out;
    each caller's verdicts are what they were. The variable's NAME stays with its caller: a
    census (tests/test_egress_hatch.py) holds that only the hatch's own module and settings.py
    name the hatch."""
    if raw is None or not raw.strip():
        return False
    value = raw.strip().lower()
    if value in FLAG_TRUE:
        return True
    if value in FLAG_FALSE:
        return False
    return None


def enabled_from_env(env) -> bool:
    """`MESHY_CAPABILITIES`, parsed. Unset or blank is OFF; the true and false spellings of
    MESHYCHAT_DPOP_ENFORCE; anything else refuses to load. meshychat/settings.py calls this once."""
    raw = env.get(ENABLE_ENV)
    value = parse_flag(raw)
    if value is None:
        raise CapabilityConfigError(
            f"{ENABLE_ENV}={raw!r} is not a boolean. Capability tokens are off unless it is "
            f"one of {', '.join(FLAG_TRUE)}; to leave them off, unset it or set it to 'off'.")
    return value


__all__ = ["CapabilityConfigError", "ENABLE_ENV", "FLAG_FALSE", "FLAG_TRUE", "enabled_from_env",
           "parse_flag"]
