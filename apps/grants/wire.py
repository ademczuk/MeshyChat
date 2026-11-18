"""Wire shapes: timestamps, and the two row->JSON projections.

FIELD ORDER IS PART OF THE WIRE. Grant and access-request fields are emitted in the
stable order below. Python dicts preserve insertion order and `json.dumps` honours it,
so a client receives stable bytes.

THE THREE OPTIONAL-FIELD RULES, which are not uniform and cannot be guessed:

  * most optional fields serialize as `null`
    (`version_pin`, `source_ref`, `scopes`, `expires_at`, `revoked_at`, `revoked_by`,
    `revoke_reason`, `max_executions`, `max_spend_cents`, and every optional field on
    an access request);
  * `freshness_ceiling_secs` and `approved_manifest_hash` are absent
    rather than null when unset - which is the normal case for every row this app
    writes;
  * `biscuit_token` is likewise omitted when None, and it is always None here (see
    service.py: minting is deploy-dark).

Grant, owner, and peer IDs serialize as UUID strings.

`approved_manifest_hash` is a byte sequence rendered as a JSON array of numbers, not
base64 or hex. Rendering it as hex would change the field a pinned grant is checked against.

Timestamps use RFC 3339 with an automatic fractional second and literal `Z`.
`apps/peers/timefmt.py` establishes the rule for this project from the key-publish
signature, where a one-character difference fails verification.
It used to be re-derived here so this package did not depend on another agent's module,
on the claim that `test_grants.py` pinned the two copies to the same outputs; no such
test existed. The copy is gone: `optional_rfc3339` and `parse_rfc3339` below bind timefmt's
functions under their old names and keep their old ValueError texts, and
`tests/test_rfc3339_one_rule.py` is the pin the old claim named.
"""
from __future__ import annotations

import datetime as _dt
import re
import uuid as _uuid

from apps.peers import timefmt


# RFC 3339 with automatic fractional seconds and `Z`; null for None.
optional_rfc3339 = timefmt.format_optional_rfc3339


def parse_rfc3339(text: str) -> _dt.datetime:
    """Parse an `expires_at` from a request body.

    Strict about shape, tolerant about offset: any RFC 3339 offset is accepted and
    converted to UTC. Raises ValueError; the caller decides the status, and
    the two texts below are what views.py `_ts_field` puts inside its 422.
    """
    if not isinstance(text, str):
        raise ValueError("expected an RFC 3339 timestamp string")
    parsed = timefmt.match_rfc3339(text)
    if parsed is None:
        raise ValueError(f"not an RFC 3339 timestamp: {text!r}")
    return parsed


_UUID_SIMPLE = re.compile(r"[0-9A-Fa-f]{32}")
_UUID_HYPHENATED = re.compile(
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")


def parse_uuid_str(text) -> _uuid.UUID:
    """Parse 32 hex digits, the 36-character hyphenated form, that form in braces (38),
    or after a lowercase `urn:uuid:` (45), with hex in either case and nothing else.
    Raises ValueError; the caller decides the status.

    Python's `uuid.UUID` is a strict superset: it drops hyphens wherever they are, strips
    any `urn:`/`uuid:` prefix and unbalanced braces, and hands the rest to `int(..., 16)`,
    which takes `0x`, a sign and surrounding space. Each of those reached the database
    here as a silently rewritten ID instead of a 400/422 (review of 4cbbc9e5).
    apps/mesh/wait.py `_parse_uuid` holds the same rule for cursor ids.
    """
    if not isinstance(text, str):
        raise ValueError("expected a UUID string")
    if len(text) == 32 and _UUID_SIMPLE.fullmatch(text):
        return _uuid.UUID(hex=text)
    if len(text) == 38 and text[0] == "{" and text[-1] == "}":
        text = text[1:-1]
    elif len(text) == 45 and text.startswith("urn:uuid:"):
        text = text[9:]
    if len(text) == 36 and _UUID_HYPHENATED.fullmatch(text):
        return _uuid.UUID(text)
    raise ValueError(f"invalid UUID: {text!r}")


def _uuid_str(value) -> str | None:
    if value is None:
        return None
    return str(value) if isinstance(value, _uuid.UUID) else str(_uuid.UUID(str(value)))


def grant_to_json(g) -> dict:
    """Return `SkillGrant` in its stable field order."""
    out = {
        "id": _uuid_str(g.id),
        "grantor_owner_id": _uuid_str(g.grantor_owner_id),
        "grantee_owner_id": _uuid_str(g.grantee_owner_id),
        "granter_peer_id": _uuid_str(g.granter_peer_id),
        "skill_name": g.skill_name,
        "version_pin": g.version_pin,
        "source": g.source,
        "source_ref": g.source_ref,
        "scopes": g.scopes,
        "granted_at": optional_rfc3339(g.granted_at),
        "expires_at": optional_rfc3339(g.expires_at),
        "revoked_at": optional_rfc3339(g.revoked_at),
        "revoked_by": _uuid_str(g.revoked_by),
        "revoke_reason": g.revoke_reason,
        "max_executions": g.max_executions,
        "used_executions": g.used_executions,
        "max_spend_cents": g.max_spend_cents,
        "used_spend_cents": g.used_spend_cents,
    }
    # These two, and only these two, are skipped when absent.
    if g.freshness_ceiling_secs is not None:
        out["freshness_ceiling_secs"] = g.freshness_ceiling_secs
    if g.approved_manifest_hash is not None:
        out["approved_manifest_hash"] = list(bytes(g.approved_manifest_hash))
    out["approved_requires_human_auth"] = g.approved_requires_human_auth
    return out


def request_to_json(r) -> dict:
    """Return `SkillAccessRequest`. No field is skipped."""
    return {
        "id": _uuid_str(r.id),
        "requester_owner_id": _uuid_str(r.requester_owner_id),
        "granter_owner_id": _uuid_str(r.granter_owner_id),
        "granter_peer_id": _uuid_str(r.granter_peer_id),
        "skill_name": r.skill_name,
        "message": r.message,
        "status": r.status,
        "decided_at": optional_rfc3339(r.decided_at),
        "decided_by": _uuid_str(r.decided_by),
        "decision_note": r.decision_note,
        "resulting_grant_id": _uuid_str(r.resulting_grant_id),
        "created_at": optional_rfc3339(r.created_at),
        "expires_at": optional_rfc3339(r.expires_at),
    }
