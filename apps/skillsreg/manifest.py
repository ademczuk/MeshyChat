"""The skill manifest hash.

32 bytes of SHA-256 over the JCS (RFC 8785) serialization of exactly the
behaviour-defining fields a grant approves. Publish-time attestation uses this
implementation. `apps/grants/manifest.py` is a second, independent implementation used
for grant pinning and the authorize arms. The two are kept in agreement by one shared
golden vector.

THIS APP USES IT FOR ONE DECISION ONLY: in `update_metadata`, "would applying this
update change the hash?". If yes, the attestation gate re-runs and any stored signature
is either refreshed or NULLED - a signature that verifies a hash the update is about to
invalidate must not be left looking valid for the new content.

WHAT IS EXCLUDED IS AS LOAD-BEARING AS WHAT IS INCLUDED. `max_runtime_ms`, `price_cents`,
`currency`, `share_mode`, `display_name`, `examples`, `runtime_status`, the timestamps,
`is_current` and the signature fields are all OUT: they can change without changing what
the skill DOES, and pinning them would force needless re-approval.

ABSENCE LAW: when the normalized `runtime_profile` is None the key
does not serialize AT ALL, so the byte stream for a legacy skill is character-identical
to the pre-v1.1 output. `tests/test_skills_registry.py` and `tests/test_grants.py` both
pin the golden vector 1b7afe0f1858394e4b4a21d4041fbf6267091901b4c7de80cbae946487d4c8fa.
It is the only evidence that this implementation and `apps/grants/manifest.py` agree on
the canonical bytes, rather than both merely looking reasonable.
"""
from __future__ import annotations

import hashlib

import rfc8785

from apps.skillsreg import runtime as rt


def manifest_hash_for_fields(peer_id, name, version, mode, description, input_schema,
                             output_schema, attachments_allowed, returns_attachments,
                             requires_human_auth, tags, runtime_profile) -> bytes:
    """Return the raw 32-byte manifest hash.

    `tags` is sorted so tag reordering is not drift. JCS sorts object keys itself, so
    the order the view is built in below does not affect the hash.
    """
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
        "tags": sorted(tags or []),
    }
    normalized = rt.normalized_profile(runtime_profile)
    if normalized is not None:
        # A normalized profile is written only when present; absence keeps legacy bytes
        # identical.
        view["runtime_profile"] = rt.profile_to_wire(normalized)
    return hashlib.sha256(rfc8785.dumps(view)).digest()


def manifest_hash(skill: dict) -> bytes:
    """Hash a row as `repository.py` returns it."""
    return manifest_hash_for_fields(
        skill["peer_id"], skill["name"], skill["version"], skill["mode"],
        skill["description"], skill["input_schema"], skill["output_schema"],
        skill["attachments_allowed"], skill["returns_attachments"],
        skill["requires_human_auth"], skill["tags"], skill["runtime_profile"],
    )
