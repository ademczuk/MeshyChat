"""The WIRE for the call + invoke HTTP surface: JSON shapes, timestamp formats and
the error mapping. Nothing here talks to the database; `reads.py` does that and
`views.py` composes the two.

WHY THIS IS A SEPARATE MODULE. Three of the things below are byte-level contracts that
are invisible in a route handler and wrong by default in Django:

   1. `json.dumps` defaults to `ensure_ascii=True` and `", "` / `": "` separators.
      This surface writes raw UTF-8 with no spaces; a response containing one non-ASCII
      character would otherwise differ from the bytes existing clients and tests expect,
      in every such byte.
   2. This surface uses TWO RFC-3339 spellings. Serialized calls use `...Z`, while a
      receipt's `completed_at` writes `+00:00`. `completed_at` is covered by a receipt
      signature, so the spellings are not interchangeable; see `format_rfc3339_offset` below.
  3. the error envelope is `{"error": <CODE>, "message": <text>}` and the (status, code)
   pair is what clients branch on.

ONE ApiError, NOT TWO. `apps.peers.errors.ApiError` is imported rather than re-declared;
a second copy of the (status, code) policy could drift. The same argument applies to
`authenticate_peer` in views.py.
"""
from __future__ import annotations

import datetime as _dt
import json
import uuid

from django.http import HttpResponse

from apps.calls import errors as rpc_errors
from apps.crypto import primitives as P
from apps.peers import timefmt
# ApiError is shared by the whole API; import it rather than duplicating its policy.
from apps.peers.errors import (E_CONFLICT, E_FORBIDDEN, E_INTERNAL,  # noqa: F401
                               E_NOT_FOUND, E_VALIDATION, ApiError)

# Additional error-code strings for this surface.
E_QUOTA_EXCEEDED = "E_QUOTA_EXCEEDED"
E_APPROVAL_PENDING = "E_APPROVAL_PENDING"
E_FILE_ATTACHMENT_INVALID = "E_FILE_ATTACHMENT_INVALID"


# --------------------------------------------------------------------- responses

def json_response(payload, status: int = 200, headers: dict | None = None) -> HttpResponse:
    """Return a compact UTF-8 JSON response.

    `separators` and `ensure_ascii=False` are not style: see the module docstring.
    `HttpResponse` rather than `JsonResponse` because a top-level ARRAY is a legal body
    on four of these routes and `JsonResponse` needs `safe=False` to emit one.
    """
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return HttpResponse(body, content_type="application/json", status=status,
                        headers=headers or {})


def error_response(exc: ApiError) -> HttpResponse:
    """A plain error has exactly `error` and `message`; optional values add their keys."""
    body = {"error": exc.code, "message": exc.message}
    for extra in ("approval_id", "expires_at"):
        value = getattr(exc, extra, None)
        if value is not None:
            body[extra] = value
    # A 429 from the per-peer limit (B12) says when to come back; nothing else sets it.
    retry_after = getattr(exc, "retry_after", None)
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
    return json_response(body, status=exc.status, headers=headers)


# -------------------------------------------------------------------- timestamps

# RFC 3339 with a trailing `Z`, or null for None. The rule, including its 9-digit branch,
# is in apps/peers/timefmt.py; this surface keeps the name its callers import.
optional_rfc3339 = timefmt.format_optional_rfc3339


def format_rfc3339_offset(value: _dt.datetime | None) -> str:
    """RFC-3339 with `+00:00`, not `Z`.

    Used by `CallReceipt.completed_at` and `E_APPROVAL_PENDING.expires_at`. A null
    completed_at renders as `""`, not JSON null. The receipt binds every field, so an
    empty string is a real value there.
    """
    if value is None:
        return ""
    z = optional_rfc3339(value)
    return z[:-1] + "+00:00"


def parse_rfc3339(text: str) -> _dt.datetime:
    """Parse a `since=` query value, accepting any RFC-3339 offset and converting to UTC.

    Raises ValueError otherwise; the caller turns that into a 400 whose message is
    "invalid `since`: " followed by the ValueError text.
    """
    if not isinstance(text, str):
        raise ValueError("timestamp must be a string")
    parsed = timefmt.match_rfc3339(text)
    if parsed is None:
        raise ValueError(f"not an RFC 3339 timestamp: {text!r}")
    return parsed


# ------------------------------------------------------------------ error mapping

def api_error_from_rpc(exc: rpc_errors.RpcError) -> ApiError:
    """Map an RpcError through the codes and statuses in apps/calls/errors.py.

    THE TWO FLATTENINGS ARE DELIBERATE AND ARE THE EASIEST THING TO GET WRONG HERE.
    The forbidden and not-found error variants render FIXED messages ("forbidden" /
    "not found"); the `reason` the engine attached is a server-side diagnostic and is
    NOT returned. Passing `exc.message` through would leak why a grant lookup failed -
    an oracle. Every other variant does pass its message.
    """
    if isinstance(exc, rpc_errors.NotAuthorized):
        return ApiError(403, E_FORBIDDEN, "forbidden")
    if isinstance(exc, rpc_errors.SkillNotFound):
        return ApiError(404, E_NOT_FOUND, "not found")
    if isinstance(exc, rpc_errors.SignatureFailed):
        # Signature verification returns the code and message verbatim.
        return ApiError(exc.http_status, exc.code, exc.message)
    if type(exc) is rpc_errors.RpcError:
        # Internal errors log the detail and return the constant message.
        err = ApiError(500, E_INTERNAL, "internal error")
        err.detail = exc.message
        return err
    err = ApiError(exc.http_status, exc.code, exc.message)
    if isinstance(exc, rpc_errors.RateLimited):
        err.retry_after = exc.retry_after
    return err


# ------------------------------------------------------------------- JSON shapes

def _u(value) -> str | None:
    return None if value is None else str(value)


def _uuid_list(value) -> list[str]:
    """A list of UUID strings, never null. The column is `NOT NULL DEFAULT '{}'`, so an
    empty array here is the column's real value, not a missing one."""
    return [str(v) for v in (value or [])]


def debug_status(status: str) -> str:
    """Return the title-cased spelling used in a conflict message.

    The 409 message reads `call not in flight (status: Completed)` - CamelCase, and
    `input_required` becomes `InputRequired`. Clients may match on this text, so keep this
    spelling rather than writing a nicer message.
    """
    return "".join(part.capitalize() for part in status.split("_"))


def call_json(row: dict, viewer: uuid.UUID | None = None) -> dict:
    """Serialize a call for an optional viewer.

    `viewer=None` means "serialize the bare call JSON shape", which is what
    `POST /v1/calls/{id}/cancel` returns rather than the viewer-specific call response.
    The distinction is only visible on a SELF-CALL, where caller and target are the same
    peer: the viewer-specific response would attach the `continuation_nonce` and the bare
    call JSON never does.

    Keys are emitted in this exact insertion order (do not sort or reorder); the order is
    part of the response bytes.

    `continuation_nonce` and `resume_nonce` are omitted from the call JSON, so neither is
    ever part of the flattened body. The viewer-specific response then adds
    `continuation_nonce` as its OWN field, and only when the viewer is the TARGET - the
    nonce authorizes the target to continue a flow, so handing it to the caller (who is
    equally entitled to READ the call) would hand them the target's continuation
    authority. That asymmetry is why this function has an optional viewer.
    """
    body = {
        "id": _u(row["id"]),
        "call_id": row["call_id"],
        "caller_peer_id": _u(row["caller_peer_id"]),
        "caller_owner_id": _u(row["caller_owner_id"]),
        "target_peer_id": _u(row["target_peer_id"]),
        "target_owner_id": _u(row["target_owner_id"]),
        "skill_id": _u(row["skill_id"]),
        "skill_name": row["skill_name"],
        "skill_version": row["skill_version"],
        "mode": row["mode"],
        "transport": row["transport"],
        "status": row["status"],
        "args": row["args"],
        "args_signature": row["args_signature"],
        "args_signing_kid": row["args_signing_kid"],
        "args_attachments": _uuid_list(row["args_attachments"]),
        "result": row["result"],
        "result_payload": row["result_payload"],
        "result_attachments": _uuid_list(row["result_attachments"]),
        "result_signature": row["result_signature"],
        "result_signing_kid": row["result_signing_kid"],
        "error_code": row["error_code"],
        "error_message": row["error_message"],
        "grant_id": _u(row["grant_id"]),
        "webhook_url": row["webhook_url"],
        "webhook_secret_hash": row["webhook_secret_hash"],
        "deadline_at": optional_rfc3339(row["deadline_at"]),
        "started_at": optional_rfc3339(row["started_at"]),
        "completed_at": optional_rfc3339(row["completed_at"]),
        "created_at": optional_rfc3339(row["created_at"]),
        "pause_context_id": row["pause_context_id"],
        "pause_requested_schema": row["pause_requested_schema"],
        "resume_spent_at": optional_rfc3339(row["resume_spent_at"]),
    }
    if viewer is not None and row["continuation_nonce"] is not None and \
            uuid.UUID(str(row["target_peer_id"])) == uuid.UUID(str(viewer)):
        body["continuation_nonce"] = row["continuation_nonce"]
    return body


def event_json(row: dict) -> dict:
    """Serialize event JSON. Keys are emitted in this exact insertion order; the order is
    part of the response bytes."""
    return {
        "id": row["id"],
        "call_id": row["call_id"],
        "event_type": row["event_type"],
        "details": row["details"],
        "source": row["source"],
        "source_peer_id": _u(row["source_peer_id"]),
        "occurred_at": optional_rfc3339(row["occurred_at"]),
    }


def chunk_json(row: dict) -> dict:
    """Serialize chunk JSON. Keys are emitted in this exact insertion order; the order is
    part of the response bytes.

    `content` is a SEQUENCE OF NUMBERS, not base64. The wire value is `[104,105]`; a
    base64 string would silently change the response.
    """
    content = row["content"]
    return {
        "id": row["id"],
        "call_id": row["call_id"],
        "sequence": row["sequence"],
        "chunk_type": row["chunk_type"],
        "content": None if content is None else list(bytes(content)),
        "file_artifact_id": _u(row["file_artifact_id"]),
        "chunk_signature": row["chunk_signature"],
        "chunk_signing_kid": row["chunk_signing_kid"],
        "emitted_at": optional_rfc3339(row["emitted_at"]),
    }


def sha256_hex(data: bytes) -> str:
    return P.sha256(data).hex()


def receipt_json(row: dict, provenance: dict) -> dict:
    """Build a receipt response.

    The receipt has no `signature` field because it is signed by the EXECUTOR and the server
    holds only peer PUBLIC keys. So the key is absent, always, and
    non-repudiation comes from the per-field signatures returned beside it.

    Every absent metadata field is `""`, not null, and the receipt BINDS them: a null
    and an empty string canonicalize to different bytes.

    THE KEYS ARE camelCase, and that is not a style choice. The wire names are `callId`,
    `argsSha256` and so on. Snake_case would break two things at once: a detached signature
    covers the canonical bytes OF THESE KEYS, so a renamed field verifies against nothing;
    and the receipt conformance verifier rejects a receipt missing any required camelCase field.

    Response keys are emitted in this exact insertion order (do not sort or reorder); the order
    is part of the response bytes. Key order is irrelevant to the signed bytes because JCS sorts.
    """
    args_sha = sha256_hex(P.jcs(row["args"]))
    result_sha = "" if row["result"] is None else sha256_hex(P.jcs(row["result"]))
    return {
        "callId": row["call_id"],
        "callerPeerId": _u(row["caller_peer_id"]) or "",
        "targetPeerId": str(row["target_peer_id"]),
        "skillName": row["skill_name"],
        "skillVersion": row["skill_version"],
        "grantId": _u(row["grant_id"]) or "",
        "parentCallId": provenance["parent_call_id"],
        "rootMandateHash": provenance["root_mandate_hash"],
        "chainId": provenance["chain_id"],
        "argsSha256": args_sha,
        "resultSha256": result_sha,
        "completedAt": format_rfc3339_offset(row["completed_at"]),
        "issuer": str(row["target_peer_id"]),
        "kid": row["result_signing_kid"] or "",
    }


__all__ = [
    "ApiError", "api_error_from_rpc", "call_json", "optional_rfc3339", "format_rfc3339_offset",
    "chunk_json", "debug_status", "error_response", "event_json", "json_response",
    "parse_rfc3339",
    "receipt_json", "sha256_hex",
    "E_APPROVAL_PENDING", "E_CONFLICT", "E_FILE_ATTACHMENT_INVALID", "E_FORBIDDEN",
    "E_INTERNAL", "E_NOT_FOUND", "E_QUOTA_EXCEEDED", "E_VALIDATION",
]
