"""Gate mode resolution.

=============================================================================
GATE DEFAULTS CLOSED. READ THIS BEFORE CHANGING ANYTHING HERE.
=============================================================================

`MESHYCHAT_POLICY_REGISTRY_ENFORCE` is read at request time. Gates here default
CLOSED:

  * unset / empty / unparseable  -> ENFORCE
  * "observe"                    -> OBSERVE   (the only spellings that relax)
  * "0" "false" "no" "off"       -> OBSERVE
  * "enforce" "1" "true" "yes" "on" -> ENFORCE

So the relaxation requires an EXPLICIT value, and a typo'd, truncated or dropped
environment variable fails towards enforcement rather than towards admission. An
operator who wants observational behavior sets
`MESHYCHAT_POLICY_REGISTRY_ENFORCE=observe`; it cannot be enabled by accident.

WHY. A default that is both "off" and "what you get when the variable is missing" cannot
be distinguished from a misconfiguration, and the failure is silent and in the direction
of admission - the gate records what it WOULD have denied and admits the call. That is
the same silent-towards-disclosure shape this project already refuses elsewhere:
`apps/core/dbconn.py` makes RLS role adoption unconditional, and `meshychat/urls.py`
reports an unconfigured health dependency as `false`, never healthy.

WHAT IT COSTS, stated plainly so the trade is visible rather than argued away: a
deployment that carries attachments written expecting observe-mode will start DENYING on
them the moment it runs this server. Every `GateMode` below carries the `reason` it
resolved that way, which the hook route logs per request, so an operator can tell
"enforcing because nobody set the flag" from "enforcing because someone set it to
enforce". The wire shape
is unchanged - `mode` is still the string "observe" or "enforce" in every response and
every `meshy_guard_policy_decisions.mode` row (`rgpd_mode_check` admits exactly those two
values), so the default does not change the vocabulary.

The two related flags are named here for completeness, but this package implements
neither gate: `MESHYCHAT_FLOW_GUARD_ENFORCE` lives on the invoke/authorize path and
`MESHYCHAT_TAG_POLICY_ENFORCE` on the tag gate. The same fail-closed resolver is
exported so that callers can reuse it, but nothing in this package calls them.
"""
from __future__ import annotations

from typing import NamedTuple
from apps.core.env import lookup

#: meshy_guard_policy_decisions.mode CHECK constraint `rgpd_mode_check`, measured:
#: mode = ANY (ARRAY['observe','enforce']). The wire vocabulary has two values.
OBSERVE = "observe"
ENFORCE = "enforce"

#: Read through apps.core.env.lookup.
POLICY_REGISTRY_FLAG = "MESHYCHAT_POLICY_REGISTRY_ENFORCE"
#: Named for the reader; this package implements neither gate (see module docstring).
FLOW_GUARD_FLAG = "MESHYCHAT_FLOW_GUARD_ENFORCE"
TAG_POLICY_FLAG = "MESHYCHAT_TAG_POLICY_ENFORCE"

#: The ONLY values that relax the gate. Anything else - including absent, empty and
#: misspelled - enforces.
_RELAXING = frozenset({OBSERVE, "0", "false", "no", "off"})
_ENFORCING = frozenset({ENFORCE, "1", "true", "yes", "on"})


class GateMode(NamedTuple):
    """The resolved mode plus the reason it resolved that way.

    `reason` is not decoration: with a fail-closed default, "enforcing" is the answer to
    two very different questions ("an operator asked for it" and "nobody configured
    anything"), and an operator debugging a deny needs to know which. It is logged, never
    put on the wire.
    """

    mode: str
    reason: str

    @property
    def is_enforce(self) -> bool:
        """Whether this mode enforces."""
        return self.mode == ENFORCE


def resolve(flag: str = POLICY_REGISTRY_FLAG, env=None) -> GateMode:
    """Resolve a gate flag, failing CLOSED. `env` is injectable so tests need no monkeypatch.

    Read at call time. Reading at import time would freeze whatever value existed when
    the first module imported this one, and would make the flag untestable without a reload.
    """
    raw = lookup(flag, source=env)
    if raw is None:
        return GateMode(ENFORCE, f"{flag} is unset; the server defaults CLOSED")
    stripped = raw.strip().lower()
    if stripped == "":
        return GateMode(ENFORCE, f"{flag} is empty; the server defaults CLOSED")
    if stripped in _RELAXING:
        return GateMode(OBSERVE, f"{flag}={raw!r} explicitly relaxes the gate")
    if stripped in _ENFORCING:
        return GateMode(ENFORCE, f"{flag}={raw!r} explicitly enforces the gate")
    # Unrecognised values matter most: a typo ("obsrve", "observe " with a stray quote,
    # or a value from a templating system that rendered empty) must not silently open the
    # gate. This is the arm where treating an unknown value as observe would open it.
    return GateMode(
        ENFORCE,
        f"{flag}={raw!r} is not a recognised value; the server defaults CLOSED "
        f"(set it to {OBSERVE!r} to relax)",
    )


def policy_registry_mode(env=None) -> GateMode:
    """The mode the meshguard policy gate runs in for this request."""
    return resolve(POLICY_REGISTRY_FLAG, env=env)
