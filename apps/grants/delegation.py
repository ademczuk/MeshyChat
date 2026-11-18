"""Delegated-grant attenuation: a child may never be broader than its parent.

The error text reaches the 400 body verbatim. The checks are ordered so a more-specific
failure reports its own reason; the TTL ceiling is checked last:

  1. parent LIVE (not revoked, not expired at `now`)
  2. parent grantee == the child's grantor   - you re-grant the slice you HOLD
  3. parent granter_peer == the child's      - sec3c peer-constancy: a re-delegation
     stays on the ORIGINAL target peer, never a peer the issuer happens to own
  4. parent skill covers the child's (phase 1: equality)
  5. the child does not outlive a BOUNDED parent (an unbounded child under a bounded
     parent DOES outlive it and is rejected; an unbounded parent bounds nothing here)
  6. child scopes are a SUBSET of the parent's
  7. child execution cap <= the parent's REMAINING executions
  8. and finally the absolute 300s sub-delegation TTL, independent of the parent

Check 5's asymmetry: `None` (perpetual) is the widest value on the child side and the
narrowest constraint on the parent side, so the same `None` means opposite things
depending on which side it is on.
"""
from __future__ import annotations

import datetime as _dt
import math

from .scopes import scope_set_from_json

#: Absolute per-hop ceiling for a delegated grant lifetime.
MAX_SUB_DELEGATION_TTL_SECS = 300


class ProvenanceError(ValueError):
    """A delegated-grant validation failure; `str(e)` is the 400 message."""


def parent_not_live() -> ProvenanceError:
    return ProvenanceError("parent grant is not live (revoked or expired)")


def validate_delegation_provenance(
    parent,
    child_grantor,
    child_granter_peer,
    child_skill: str,
    child_expires_at: _dt.datetime | None,
    child_scopes,
    child_max_executions: int | None,
    now: _dt.datetime,
) -> None:
    """Raise ProvenanceError unless the child strictly attenuates `parent`."""
    live = parent.revoked_at is None and (parent.expires_at is None or parent.expires_at > now)
    if not live:
        raise parent_not_live()

    if parent.grantee_owner_id != child_grantor:
        raise ProvenanceError(
            f"parent grantee {parent.grantee_owner_id} is not the new grantor "
            f"{child_grantor}; a delegation re-grants the slice the issuer holds")

    if parent.granter_peer_id != child_granter_peer:
        raise ProvenanceError(
            f"child granter peer {child_granter_peer} differs from the parent's "
            f"{parent.granter_peer_id}; peer is constant down a chain (sec3c) - a "
            "re-delegation stays on the original target peer, never the issuer's own peer")

    if parent.skill_name != child_skill:
        raise ProvenanceError(
            f"parent skill '{parent.skill_name}' does not cover child skill '{child_skill}'")

    if parent.expires_at is not None:
        outlives = True if child_expires_at is None else child_expires_at > parent.expires_at
        if outlives:
            raise ProvenanceError(
                "child expiry outlives the bounded parent (parent ends at "
                f"{int(parent.expires_at.timestamp())})")

    parent_scopes = scope_set_from_json(parent.scopes)
    extra = sorted(scope_set_from_json(child_scopes) - parent_scopes)
    if extra:
        # Wire form: a bracketed, double-quoted list, e.g. ["a", "b"].
        rendered = "[" + ", ".join(f'"{s}"' for s in extra) + "]"
        raise ProvenanceError(f"child broadens scope beyond the parent (added: {rendered})")

    if parent.max_executions is not None:
        remaining = parent.max_executions - parent.used_executions
        if remaining <= 0:
            raise ProvenanceError(
                f"parent execution cap is exhausted ({parent.used_executions}/"
                f"{parent.max_executions})")
        if child_max_executions is None or child_max_executions > remaining:
            requested = "None" if child_max_executions is None else f"Some({child_max_executions})"
            raise ProvenanceError(
                "child execution cap must be bounded by parent remaining executions "
                f"({remaining}); requested {requested}")

    # An absolute per-hop ceiling, independent of the parent.
    # An unbounded child is rejected here even under a perpetual parent, because its
    # offline-revocation window would be unbounded.
    # Truncate toward zero, so a 300.5s child is within the 300s boundary.
    within_ttl = (
        child_expires_at is not None
        and math.trunc((child_expires_at - now).total_seconds()) <= MAX_SUB_DELEGATION_TTL_SECS
    )
    if not within_ttl:
        raise ProvenanceError(
            f"sub-delegation lifetime exceeds the {MAX_SUB_DELEGATION_TTL_SECS}s ceiling "
            "(or is unbounded)")
