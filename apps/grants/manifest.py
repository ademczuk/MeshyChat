"""Skill-manifest pinning: the 32 bytes a version-pinned grant approves.

The hash is

    SHA-256( JCS( view ) )

over exactly the behavior-defining fields. What is excluded is as
load-bearing as what is included: `max_runtime_ms`, `price_cents`, `currency`,
`share_mode`, `display_name`, `examples`, `runtime_status`, `is_current`, the
timestamps and the signature fields are all out, because they can change without
changing what the skill DOES and pinning them would force needless re-approval.
`tags` is SORTED, so tag reordering is not drift.

The 64-hex value `GOLDEN_LEGACY_HEX = 1b7afe0f…c8fa` was produced by an independent
implementation of this hash. `test_grants.py` recomputes the named fixture through the
function below and asserts the same 64 hex characters. That is the only evidence the
canonical bytes agree across implementations; a correct-looking but different hash would
silently brick every pinned grant at the invoke gate.

BOUNDED GAP, STATED: manifest hash rule v1.1 conditionally includes a NORMALIZED
`runtime_profile` (sorted+deduped `policy_tags` and `tool_interfaces`, all-absent
sub-objects dropped, a wholly-absent profile omitted so legacy bytes are unchanged).
The absence law is implemented here exactly. The normalization is
not: a skill row that declares a non-empty `runtime_profile` raises
`ManifestUnsupported` and the caller fails CLOSED (no grant is written), rather than
storing a hash computed from an unsupported normalization. A wrong-but-stored
hash is worse than a refused issuance: it would pass every check here and mismatch at
the call-time pin gate, which is a bricked grant with no visible cause.
"""
from __future__ import annotations

import hashlib

from apps.crypto.primitives import jcs


class ManifestError(ValueError):
    """Raised when canonical manifest serialization fails."""


class ManifestUnsupported(ManifestError):
    """Raised for a declared runtime_profile that this module does not normalize."""


def _profile_is_absent(profile) -> bool:
    """A NULL column or an object that normalizes to empty is absent.

    Only the two ends of the rule are decided here: NULL / `{}` / an object whose every
    value is null or an empty container is ABSENT (byte-identical to legacy); anything
    else is a real declaration this module refuses rather than guesses.
    """
    if profile is None:
        return True
    if not isinstance(profile, dict):
        # A non-object runtime_profile is a real value; refuse it rather than silently
        # ignoring it.
        return False
    for value in profile.values():
        if value is None:
            continue
        if isinstance(value, (dict, list, str)) and len(value) == 0:
            continue
        return False
    return True


def manifest_hash_for_fields(*, peer_id, name: str, version: str, mode: str,
                             description, input_schema, output_schema,
                             attachments_allowed: bool, returns_attachments: bool,
                             requires_human_auth: bool, tags, runtime_profile) -> bytes:
    """Return the 32 raw bytes for the supplied manifest fields."""
    if not _profile_is_absent(runtime_profile):
        raise ManifestUnsupported(
            "skill declares a runtime_profile; the server does not implement the "
            "v1.1 normalization and refuses to pin a hash it cannot reproduce")
    view = {
        "peer_id": str(peer_id),
        "name": name,
        "version": version,
        "mode": mode,
        "description": description,
        "input_schema": input_schema,
        "output_schema": output_schema,
        "attachments_allowed": bool(attachments_allowed),
        "returns_attachments": bool(returns_attachments),
        "requires_human_auth": bool(requires_human_auth),
        # Sorted by code point. For ASCII tags that equals byte order; a non-ASCII tag
        # also sorts by code point, which equals UTF-8 byte order. The sort order is
        # part of the hashed bytes.
        "tags": sorted(str(t) for t in (tags or [])),
    }
    # The absence law: the key is NOT emitted at all when the profile is absent, which
    # is what keeps the byte stream identical to the pre-v1.1 output.
    try:
        canonical = jcs(view)
    except (TypeError, ValueError) as exc:
        raise ManifestError(f"canonical manifest serialization failed: {exc}") from exc
    return hashlib.sha256(canonical).digest()


def manifest_hash(skill_row) -> bytes:
    """Return the manifest hash for a `skills` row read from Postgres."""
    return manifest_hash_for_fields(
        peer_id=skill_row["peer_id"],
        name=skill_row["name"],
        version=skill_row["version"],
        mode=skill_row["mode"],
        description=skill_row["description"],
        input_schema=skill_row["input_schema"],
        output_schema=skill_row["output_schema"],
        attachments_allowed=skill_row["attachments_allowed"],
        returns_attachments=skill_row["returns_attachments"],
        requires_human_auth=skill_row["requires_human_auth"],
        tags=skill_row["tags"],
        runtime_profile=skill_row["runtime_profile"],
    )
