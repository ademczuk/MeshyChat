"""Lesson-mode publish validation.

A `mode = "lesson"` skill carries a LessonPayload in `examples`, and that payload is
persisted verbatim and FANNED OUT over WebSocket to every subscribed mentee. Two
consequences drive everything here:

  1. SECRETS. A credential in a fragment or an inline file body is republished to every
     subscriber. The scan covers `claude_md_fragments[].content` AND `files[].content`,
     because scanning only the fragments let an author smuggle credentials past the
     control by moving them into `files[]`. A base64 body is
     DECODED first, and a decode failure is a hard error rather than a silent skip -
     otherwise the gate is evaded by mislabelling `encoding`.
  2. RE-ATTRIBUTION. The signature only proves that `mentor_peer_id` signed those bytes,
     NOT that the peer presenting them is that mentor. Because payloads are fanned out,
     any subscriber can observe a respected mentor's validly-signed payload and re-POST
     it as its own skill; verification would still pass and the row would persist owned
     by the attacker while carrying the victim's attribution. So the publisher/mentor
     mismatch is rejected BEFORE the placeholder escape hatch, so dev mode cannot
     re-attribute either.

Errors carry the offending INDEX, never the matched bytes: the secret is never echoed
back into a response or a log.
"""
from __future__ import annotations

import base64
import binascii
import logging
import re
import uuid

import rfc8785

from apps.crypto import primitives as P
from apps.core.env import lookup

log = logging.getLogger("meshychat.skillsreg")

#: Dev-mode escape hatch; default unset means verification is mandatory. Read at CALL time
#: so a test can set it without re-importing.
PLACEHOLDER_ENV_VAR = "MESHYCHAT_LESSON_INSECURE_PLACEHOLDER"
PLACEHOLDER_SIGNATURE = "placeholder"

#: AWS access keys, PEM private-key headers, LLM key prefixes.
#: `sk-` is gated to >=20 payload chars so it does not match "sk-test" in prose.
SECRET_RE = re.compile(
    r"(AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|\bsk-(ant-|proj-)?[A-Za-z0-9_-]{20,})", re.IGNORECASE)


class LessonError(ValueError):
    """Every `LessonError` becomes a 400 E_VALIDATION.

    Its message is the only part of the failure a client can branch on beyond the status.
    """


def _placeholder_allowed() -> bool:
    """Return true only for the literal `"true"`, case-insensitive."""
    return (lookup(PLACEHOLDER_ENV_VAR) or "").lower() == "true"


def _as_object(examples):
    if not isinstance(examples, dict):
        raise LessonError("lesson examples failed to deserialize as LessonPayload: "
                          "invalid type: expected a LessonPayload object")
    return examples


def validate_lesson_consistency(examples, uploaded_attachment_ids=None) -> None:
    """Validate structure, harness, attachment existence, and secret scanning."""
    if examples is None:
        raise LessonError("lesson skill requires an examples payload (LessonPayload)")
    payload = _as_object(examples)

    harness = payload.get("target_harness")
    if not isinstance(harness, str):
        raise LessonError("lesson examples failed to deserialize as LessonPayload: "
                          "missing field `target_harness`")
    if harness != "claude_code":
        raise LessonError(
            f"lesson target_harness {harness} unsupported; v1 only accepts 'claude_code'")

    fragments = payload.get("claude_md_fragments") or []
    files = payload.get("files") or []
    if not isinstance(fragments, list) or not isinstance(files, list):
        raise LessonError("lesson examples failed to deserialize as LessonPayload: "
                          "claude_md_fragments and files must be arrays")

    # Every file sha256 must resolve to an attachment the publisher uploaded. Skipped
    # when the caller has not plumbed the attachment set in - which is the case on this
    # route, where the HTTP handlers pass None.
    if uploaded_attachment_ids is not None:
        for f in files:
            sha = (f or {}).get("sha256")
            if sha not in uploaded_attachment_ids:
                raise LessonError(
                    f"lesson references attachment sha256 {sha} which has not been uploaded")

    for idx, frag in enumerate(fragments):
        if not isinstance(frag, dict):
            raise LessonError("lesson examples failed to deserialize as LessonPayload: "
                              "claude_md_fragments entries must be objects")
        # The first present field, `content` or `body`, wins.
        content = frag.get("content")
        if content is None:
            content = frag.get("body")
        if not isinstance(content, str):
            raise LessonError("lesson examples failed to deserialize as LessonPayload: "
                              "missing field `content`")
        if SECRET_RE.search(content):
            raise LessonError(
                f"lesson claude_md_fragments[{idx}] contains a credential-shaped string; "
                "redact before publishing")

    for idx, f in enumerate(files):
        if not isinstance(f, dict) or not isinstance(f.get("sha256"), str):
            raise LessonError("lesson examples failed to deserialize as LessonPayload: "
                              "missing field `sha256`")
        content = f.get("content", "")
        if not isinstance(content, str):
            raise LessonError("lesson examples failed to deserialize as LessonPayload: "
                              "files[].content must be a string")
        encoding = f.get("encoding", "utf8")
        if not isinstance(encoding, str):
            raise LessonError("lesson examples failed to deserialize as LessonPayload: "
                              "files[].encoding must be a string")
        if encoding.lower() == "base64":
            decoded = None
            for altchars in (b"+/", b"-_"):
                try:
                    decoded = base64.b64decode(content + "=" * (-len(content) % 4),
                                               altchars=altchars, validate=True)
                    break
                except (binascii.Error, ValueError):
                    continue
            if decoded is None:
                raise LessonError(
                    "lesson examples failed to deserialize as LessonPayload: "
                    f"files[{idx}] declares encoding=base64 but content is not valid base64")
            text = decoded.decode("utf-8", errors="replace")
        else:
            text = content
        if SECRET_RE.search(text):
            raise LessonError(
                f"lesson files[{idx}] contains a credential-shaped string; "
                "redact before publishing")


def canonical_signing_bytes(payload) -> bytes:
    """Return the payload's canonical signing bytes.

    `signature` is REPLACED WITH NULL, not removed: a missing field and a null field
    produce different canonical bytes, so a producer that deletes the key signs a
    different message than this verifier hashes.
    """
    if not isinstance(payload, dict):
        raise LessonError("lesson examples failed to deserialize as LessonPayload: "
                          "payload is not a JSON object")
    clone = dict(payload)
    clone["signature"] = None
    try:
        return rfc8785.dumps(clone)
    except Exception as exc:                       # rfc8785 raises its own types
        raise LessonError("lesson examples failed to deserialize as LessonPayload: "
                          f"canonicalize failed: {exc}")


def find_active_peer_key(conn, peer_id) -> str | None:
    """Return the newest active key for a peer.

    ORDER BY created_at DESC, id DESC + LIMIT 1. Migration 030 dropped the DB-level
    "one active key per peer" unique index so a rotation can hold two active rows
    transiently, which makes that invariant purely application-level; without the
    ORDER BY, this security-relevant key resolution would pick whichever row Postgres
    streamed first whenever the invariant is violated.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT public_key FROM peer_keys "
            "WHERE peer_id = %s AND active = TRUE AND revoked_at IS NULL "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (str(peer_id),))
        row = cur.fetchone()
    return None if row is None else row[0]


def validate_lesson_signature(conn, examples, publisher_peer_id) -> None:
    """Validate a lesson signature when examples are supplied.

    The structural validator owns the `examples is None` case for lesson skills, and
    `update_metadata` calls this with None legitimately.
    """
    if examples is None:
        return
    payload = _as_object(examples)

    raw_mentor = payload.get("mentor_peer_id")
    signature = payload.get("signature")
    if not isinstance(signature, str) or not isinstance(raw_mentor, str):
        raise LessonError("lesson examples failed to deserialize as LessonPayload: "
                          "missing field `signature` or `mentor_peer_id`")
    try:
        mentor_peer_id = uuid.UUID(raw_mentor)
    except ValueError:
        raise LessonError("lesson examples failed to deserialize as LessonPayload: "
                          "mentor_peer_id is not a uuid")

    # BEFORE the placeholder hatch, deliberately - see the module docstring.
    if mentor_peer_id != uuid.UUID(str(publisher_peer_id)):
        raise LessonError("lesson signature invalid: mentor_peer_id does not match the "
                          "authenticated publishing peer")

    if signature == PLACEHOLDER_SIGNATURE:
        if _placeholder_allowed():
            log.warning("accepting lesson with signature='placeholder' - "
                        "MESHYCHAT_LESSON_INSECURE_PLACEHOLDER=true (dev mode, insecure): "
                        "mentor_peer_id=%s", mentor_peer_id)
            return
        raise LessonError(
            "lesson signature invalid: placeholder signature rejected "
            "(set MESHYCHAT_LESSON_INSECURE_PLACEHOLDER=true for dev mode)")

    try:
        sig_bytes = P.b64u_decode(signature)
    except Exception:
        raise LessonError("lesson signature invalid: signature is not valid base64url-no-pad")
    if len(sig_bytes) != 64:
        raise LessonError("lesson signature invalid: signature must decode to 64 bytes")

    public_key_b64u = find_active_peer_key(conn, mentor_peer_id)
    if public_key_b64u is None:
        raise LessonError(
            f"mentor peer {mentor_peer_id} has no active signing key in peer_keys")
    try:
        pk = P.b64u_decode(public_key_b64u)
    except Exception:
        raise LessonError("lesson signature invalid: mentor public key is not valid "
                          "base64url-no-pad")
    if len(pk) != 32:
        raise LessonError("lesson signature invalid: mentor public key must decode to 32 bytes")

    canonical = canonical_signing_bytes(payload)
    # `verify_strict`, not a plain verify: it rejects the weak-key / point-malleability
    # edge cases, matching the envelope verification policy.
    if not P.verify_strict(pk, canonical, sig_bytes):
        raise LessonError("lesson signature invalid: lesson signature verification failed")
