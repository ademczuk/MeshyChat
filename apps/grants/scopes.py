"""Convert `skill_grants.scopes` to the scope set used by attenuation rules.

The conversion fails closed, and each branch is load-bearing:

  * `None` -> the EMPTY set, the LEAST authority. "No scopes means all scopes" would
    invert the subset check and authorize widening rather than reject it.
  * a JSON array of strings -> those strings (non-string members are dropped).
  * a single JSON string -> a one-element set.
  * anything else (object, number, `null`, bool) -> the empty set.

The flow-scope family is the exception to "unrecognized is inert": a string in that
family must have the expected `accept-origin:owner:<uuid>` form or the whole value is
rejected, so a malformed stamp can never be mistaken for authority later. Non-flow
strings stay inert for backward compatibility. Other scope families are deliberately
not parsed here: parsing them would start rejecting scope strings that issuance accepts
today.
"""
from __future__ import annotations

import uuid

#: This text reaches the wire inside the 403 message.
BAD_FLOW = "`meshy:flow:` expects `accept-origin:owner:<uuid>`"


class ScopeError(ValueError):
    """A flow scope string that does not parse."""


def scope_set_from_json(scopes) -> set[str]:
    """Return the string members of a scopes JSON value."""
    if isinstance(scopes, list):
        return {v for v in scopes if isinstance(v, str)}
    if isinstance(scopes, str):
        return {scopes}
    return set()


def flow_accept_origin_owners_from_json(scopes) -> set[uuid.UUID]:
    """Return owners from flow scopes or raise ScopeError naming an invalid string."""
    owners: set[uuid.UUID] = set()
    for raw in scope_set_from_json(scopes):
        if not raw.startswith("meshy:flow:"):
            continue
        parts = raw.split(":")
        # Exactly five segments: namespace, flow, action, subject type, and owner UUID.
        if len(parts) != 5 or parts[2] != "accept-origin" or parts[3] != "owner":
            raise ScopeError(f"{raw}: {BAD_FLOW}")
        try:
            owners.add(uuid.UUID(parts[4]))
        except ValueError:
            raise ScopeError(f"{raw}: {BAD_FLOW}") from None
    return owners


def has_flow_accept_origin_scope(scopes) -> bool:
    """Return whether any valid flow scope is present."""
    return bool(flow_accept_origin_owners_from_json(scopes))
