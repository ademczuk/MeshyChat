"""Read a configuration flag from a Django setting OR the environment.

WHY THIS IS ITS OWN MODULE. Two owner extractors in this project read the SAME flag two
different ways, and only one of them works:

  * apps/billing/owner_auth.py:69-84 reads the setting, then falls back to the environment.
  * apps/dashboard/auth.py:114-116 reads the setting ONLY - and meshychat/settings.py never
    defines MESHY_SSO_CONFIGURED.

The consequence is not a style difference. `bearer_jwt_allowed()` keys on that flag, so on
the dashboard surface it is permanently False, **any EdDSA `aud=dashboard` token from the
configured issuer is full owner authority, and setting the environment variable does
nothing at all.** The production posture the module documents cannot be switched on.

So the rule this module exists to enforce: A GATE WHOSE FLAG CANNOT BE SET IS NOT A GATE.
One reader, used by every surface, that consults both sources.

Truthiness follows the environment convention, not Python's: the string "0" is FALSE here,
where `bool("0")` is True. Every flag that reaches this function is a deployment switch
written by a human into a shell, and "0" means off in every such file ever written.
"""
from __future__ import annotations

import math
import os
import sys

#: What counts as ON. Anything else - including "0", "false", "" and nonsense - is OFF,
#: because a flag nobody can spell correctly must fail CLOSED.
TRUE_VALUES = ("1", "true", "yes", "on")


class _ImproperlyConfigured(Exception):
    """Fallback configuration error when Django is not installed in a client venv."""


def _improperly_configured() -> type[Exception]:
    try:
        from django.core.exceptions import ImproperlyConfigured
        return ImproperlyConfigured
    except ImportError:
        return _ImproperlyConfigured


def lookup(name: str, default=None, *, source=None):
    """`name` from the environment (or the `source` mapping), else `default`.

    An empty string is a value, as it is to `os.environ.get`.
    """
    source = os.environ if source is None else source
    value = source.get(name)
    return default if value is None else value


def setting(name: str, env_name: str | None = None, default=None):
    """The Django setting if it is set, else the environment variable (through `lookup`), else `default`.

    `None` from `getattr` means "not configured" and falls through; a setting explicitly
    set to a falsey value that is NOT None (0, "", False) is returned as configured,
    because "explicitly off" must beat "the environment says on".
    """
    value = None
    if "django.conf" in sys.modules or os.environ.get("DJANGO_SETTINGS_MODULE"):
        try:
            from django.conf import settings
            from django.core.exceptions import ImproperlyConfigured
        except ImportError:
            pass
        else:
            try:
                value = getattr(settings, name, None)
            except ImproperlyConfigured:
                # Maintenance commands such as `python -m apps.mesh` deliberately do not boot
                # Django; they still need the environment half of this setting-or-env contract.
                value = None
    if value is not None:
        return value
    if env_name:
        raw = lookup(env_name)
        if raw is not None:
            return raw
    return default


def env_flag(name: str, env_name: str, *, default: bool = False) -> bool:
    """A boolean deployment switch, read from the setting or the environment.

    DEFAULTS FALSE, and every caller should want that: these flags turn ENFORCEMENT on.
    A flag that defaults True is one a missing config silently disables.
    """
    value = setting(name, env_name, default=default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in TRUE_VALUES


def number(name: str, raw, kind):
    """`raw` (a setting's value, or the environment's string) as a finite `kind` above zero,
    else ImproperlyConfigured naming `name`, the setting, and the defect.

    Moved from apps/core/peer_limit.py, whose private `_parse` apps/core/redeem_limit.py
    imported across modules; both rate limiters read their burst and refill through it, and
    every refusal message is what it was.
    """
    err = _improperly_configured()
    if kind is int and isinstance(raw, float) and not raw.is_integer():
        # int(1.5) is 1: a fractional burst is refused, as the string "1.5" is, never
        # truncated (review INFO 7). A whole float (3.0) is a burst of 3.
        raise err(f"{name} must be a whole number, got {raw!r}")
    try:
        value = kind(raw)
    except (TypeError, ValueError, OverflowError):
        raise err(f"{name} must be a {kind.__name__}, got {raw!r}") from None
    if isinstance(raw, bool) or not math.isfinite(value) or value <= 0 or (
            kind is int and value < 1):
        raise err(f"{name} must be a finite number above zero, got {raw!r}")
    return value


def rate_config(burst_name: str, rate_name: str, default_burst: int,
                default_rate: float) -> tuple[int, float]:
    """(burst, refill per second) for a token-bucket limiter: each from the setting of that
    name, else the environment variable of that name, else its default, validated by
    `number`. The one shape `peer_limit.config` and `redeem_limit.config` each spelled out;
    each reads the names and defaults it did."""
    burst = number(burst_name, setting(burst_name, burst_name, default_burst), int)
    rate = number(rate_name, setting(rate_name, rate_name, default_rate), float)
    return burst, rate


__all__ = ["TRUE_VALUES", "env_flag", "lookup", "number", "rate_config", "setting"]
