"""The request half both call transports share: the native invoke and `/v1/calls/*` routes
(`apps/calls/views.py`) and the A2A shim (`apps/a2a/views.py`).

Each of these was written twice, once per views module, with identical bodies. Two copies
of the 415/400/422 split, of the 404-before-403 order or of the envelope's field checks are
free to drift, and the A2A shim exists to answer the SAME call lifecycle in another
vocabulary, so a drift between them would be a wire difference between two transports of
one call. Each views module binds these under its old private names, so its handlers and
the tests that reach them read unchanged.

What is NOT here, on purpose:
  * the routes' decorators. `serve` is the connection-and-error body; each views module
    keeps its own wrapper around it, so its 405 is answered where it was (a2a before the
    connection, STATUS C73; calls inside the handler) and its log lines stay under its own
    logger (`meshychat.a2a` / `meshychat.calls`), which the caller passes in.
  * `_poll_sleep` and `_await_terminal`. The sleep is module-local so a test hooks ONE
    view's loop (review of 129e325, finding 2), and the two waits differ: calls always
    reads once and returns the row, a2a skips the read at a 0 s wait and returns nothing.
  * the sync ceiling's VALUE. Each transport keeps its own constant; `sync_poll_seconds`
    takes it.
"""
from __future__ import annotations

import json
import uuid

from django.conf import settings

from apps.calls import reads, wire
from apps.calls.envelope import SignedEnvelope
from apps.calls.errors import RpcError
from apps.core import dbconn
from apps.crypto.ijson import untextable_string_path
from apps.peers.errors import ApiError


def serve(log, handler, request, /, *args, **kwargs):
    """Open ONE guarded connection, run the handler, render an ApiError.

    `dbconn.serving_connection` is used rather than a bare psycopg connect because it
    REFUSES a connection that could bypass row-level security or that is not in
    autocommit. Both failure modes are silent in the direction of disclosure, and both
    are things a route cannot detect on its own.
    """
    try:
        with dbconn.serving_connection() as conn:
            return handler(request, conn, *args, **kwargs)
    except ApiError as exc:
        if exc.status >= 500:
            # `detail` is set only by `internal()`; the shared class defaults it to None, so
            # an error built without it logs its message, as before the classes were folded.
            log.error("%s %s -> %s: %s", request.method, request.path, exc.code,
                      exc.detail if exc.detail is not None else exc.message)
        return wire.error_response(exc)
    except RpcError as exc:
        # A pipeline error that escaped a handler's own mapping still has to leave
        # as a JSON error envelope, never as a Django 500 HTML page.
        return wire.error_response(wire.api_error_from_rpc(exc))
    except Exception as exc:  # noqa: BLE001 - see below; this is not a swallow
        # An unexpected exception LOGS the detail and returns the constant
        # "internal error". Django's default would be a 500
        # HTML page (or a re-raise under DEBUG), breaking clients that parse every
        # error as JSON on the one path they are least able to parse. So the envelope is produced
        # here - and the cause is logged at ERROR with its traceback, so this
        # cannot hide a defect the way a `return []` would.
        log.exception("%s %s -> unhandled %s", request.method, request.path,
                      type(exc).__name__)
        return wire.error_response(
            ApiError(500, wire.E_INTERNAL, "internal error"))


def json_body(request) -> dict:
    """Extract JSON at the level of status codes.

    This surface distinguishes three statuses rather than collapsing them into one 400:

        wrong/absent Content-Type   -> 415 Unsupported Media Type
        body is not JSON at all     -> 400 Bad Request
        JSON, but not this shape    -> 422 Unprocessable Entity

    This returns the JSON `{error, message}` envelope every other error on this surface uses.
    The status is the contract a client branches on; the body stays structured rather than
    exposing parser prose.
    """
    ctype = (request.content_type or "").split(";")[0].strip().lower()
    if ctype != "application/json" and not ctype.endswith("+json"):
        raise ApiError(415, wire.E_VALIDATION,
                       f"expected `application/json` content-type, got {ctype!r}")
    try:
        body = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiError(400, wire.E_VALIDATION, f"body is not valid JSON: {exc}")
    if not isinstance(body, dict):
        raise ApiError(422, wire.E_VALIDATION, "body must be a JSON object")
    return body


def unprocessable(message: str) -> ApiError:
    """A body that parsed as JSON but does not deserialize into the target type."""
    return ApiError(422, wire.E_VALIDATION, message)


def peer_uuid(raw) -> uuid.UUID:
    """A `{peer_id}` path segment.

    A malformed id is a 400, NOT a 404. The body is this app's JSON envelope. A 404
    would be the worse answer: it would tell a client the peer does not exist when the
    truth is the id was never well-formed.
    """
    try:
        return uuid.UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        raise ApiError(400, wire.E_VALIDATION, f"invalid peer id: {raw}")


def resolve_visible_call(conn, call_id: str, viewer: uuid.UUID) -> dict:
    """404 then 403, in that order: load the call, then assert the peer can see it.

    `reads.load_call` returns `Found | NotFound` and `reads.for_viewer` returns
    `Found | Denied` - typed absence, so neither branch can be reached by a read that
    silently returned nothing because its RLS context was unbound.
    """
    found = reads.load_call(conn, call_id)
    if isinstance(found, reads.NotFound):
        raise ApiError(404, wire.E_NOT_FOUND, "not found")
    seen = reads.for_viewer(found, viewer)
    if isinstance(seen, reads.Denied):
        raise ApiError(403, wire.E_FORBIDDEN, "forbidden")
    return seen.row


def envelope_from(body: dict) -> SignedEnvelope:
    """`InvokeBody.envelope` / `MessageSendBody.envelope: SignedEnvelope` are REQUIRED.

    A missing or mis-shaped envelope is a deserialization
    failure, so 422, not 400: the request WAS JSON, it just was not this type."""
    raw = body.get("envelope")
    if raw is None:
        raise unprocessable("missing field `envelope`")
    if not isinstance(raw, dict):
        raise unprocessable("`envelope` must be an object")
    payload = raw.get("payload")
    signature = raw.get("signature")
    signing_kid = raw.get("signing_kid")
    if not isinstance(payload, dict):
        raise unprocessable("`envelope.payload` must be an object")
    if not isinstance(signature, str):
        raise unprocessable("`envelope.signature` must be a string")
    if not isinstance(signing_kid, str):
        raise unprocessable("`envelope.signing_kid` must be a string")
    if untextable_string_path(signing_kid) is not None:
        # #252, pxx's #275 review: the kid is a Postgres parameter (fetch_peer_key), which
        # holds no U+0000 and cannot encode a lone surrogate, so either answered HTTP 500.
        # The signature needs no such line: its decode already answers 401 E_SIG_BAD.
        raise unprocessable("`envelope.signing_kid` must not hold U+0000 or a lone surrogate")
    return SignedEnvelope(payload=payload, signature=signature, signing_kid=signing_kid)


def sync_poll_seconds(ceiling: float) -> float:
    """`MESHY_SYNC_TIMEOUT_SECONDS`, bounded to `[0, ceiling]`; a value that cannot be a
    wait (not a number) is the 0 s default. The setting is shared by both transports, while
    each supplies its own ceiling. Read per call, so a settings override takes effect on the
    next request."""
    raw = getattr(settings, "MESHY_SYNC_TIMEOUT_SECONDS", 0.0)
    try:
        return max(0.0, min(float(raw), ceiling))
    except (TypeError, ValueError):
        return 0.0


__all__ = ["envelope_from", "json_body", "peer_uuid", "resolve_visible_call", "serve",
           "sync_poll_seconds", "unprocessable"]
