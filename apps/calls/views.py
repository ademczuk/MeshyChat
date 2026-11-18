"""HTTP surface for `POST /v1/agents/{peer_id}/skills/{name}/invoke` and `/v1/calls/*`.

This module is ROUTING, AUTHORIZATION-ROUTING AND SHAPE. It does not re-implement
verification: `apps/calls/pipeline.py` owns the gate order (verify -> rebind -> nonce
-> authorize -> insert) and is called, not copied.

WHAT EACH ROUTE ANSWERS:

  POST /v1/agents/{peer_id}/skills/{name}/invoke
  GET  /v1/calls
  GET  /v1/calls/{call_id}
  GET  /v1/calls/{call_id}/receipt
  GET  /v1/calls/{call_id}/events
  GET  /v1/calls/{call_id}/chunks
  POST /v1/calls/{call_id}/cancel

THE AUTHORIZATION SHAPE, which is the same on five of the six reads and is worth
stating once: resolve the call by its UNIQUE `call_id` under the server-internal
context, THEN check the authenticated peer is its caller or its target
(`assert_peer_can_see`). A 404 is returned for an unknown id before
that check, so the two are distinguishable: `404 = no such call`,
`403 = not yours`. Collapsing them into one code would be defensible privacy design
and would NOT be this server, so it is not done here.

ONE EXTRACTOR, NOT TWO. `authenticate_peer` is imported from `apps.peers.auth`; a second
copy in this package would duplicate the audience / actor-claim / peer-status policy and
could drift from the peers routes. Nothing here edits that module - it reuses a shared
contract.

ROUTE DESIGN LIMITS:

  * Quota: there is no tier service, so no `calls_per_cycle` reservation is taken and 402
    E_QUOTA_EXCEEDED is unreachable on this route.
  * A SYNC call uses a bounded row poll. A memory-authority call is terminal when `start_call`
    returns (step 9,
    apps/calls/route.py, docs/DIVERGENCES.md 017), so `_await_terminal`'s first read
    returns it; a call to any other peer is `routed` or `queued` (041 S5), and at the
    default wait (0 s, design Q1) the route answers 202 + Location and the client polls.
    See `_await_terminal`.
  * Cancel does not publish a terminal notification because this HTTP route has no subscription
    mechanism for it.
"""
from __future__ import annotations

import logging
import time
import uuid

from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt

from apps.calls import http, nonce_ledger, object_capability, pipeline, reads, terminal, wire
from apps.calls import route as routing
from apps.calls.errors import RpcError
from apps.crypto.ijson import echo_text
# The SHARED extractor and the SHARED error enum - see the module docstring.
from apps.peers.auth import authenticate_peer
from apps.peers.errors import ApiError

log = logging.getLogger("meshychat.calls")

#: The ceiling on `_await_terminal`'s poll; the configured wait defaults to 0.
SYNC_TIMEOUT_SECONDS = 30.0

def _sync_poll_seconds() -> float:
    """How long a sync invoke waits for its call to reach a terminal state.

    DEFAULT 0.0. A memory-authority call is finished before
    `start_call` returns (docs/DIVERGENCES.md 017), so the one read `_await_terminal` makes
    sees its terminal row. A call to another peer is finished by that peer, over its
    socket, in another request or worker (041 S5); the operator's design answer Q1 keeps
    the wait at 0 for it, so a sync invoke answers 202 + Location and the client polls.
    `MESHY_SYNC_TIMEOUT_SECONDS` restores a polled wait of up to 30 s. The response
    shapes are identical either way, only the latency differs.
    """
    return http.sync_poll_seconds(SYNC_TIMEOUT_SECONDS)


# ------------------------------------------------------------------ plumbing

def _route(*allowed):
    """Answer the METHOD, then open ONE guarded connection, run the handler, render an
    ApiError. Used as `@_route("GET")`.

    THE METHOD FIRST (#129), as `apps/a2a/views.py` does (STATUS C73): each handler used to
    answer it on its first line, after this had opened the connection, so every 405 paid a
    full connect and a database outage turned it into a 500.

    `dbconn.serving_connection` is used rather than a bare psycopg connect because it
    REFUSES a connection that could bypass row-level security or that is not in
    autocommit. Both failure modes are silent in the direction of disclosure, and both
    are things a route cannot detect on its own.
    """
    def decorate(handler):
        def wrapper(request, *args, **kwargs):
            bad = _method(request, *allowed)
            if bad:
                return bad
            return http.serve(log, handler, request, *args, **kwargs)
        wrapper.__name__ = getattr(handler, "__name__", "route")
        return csrf_exempt(wrapper)
    return decorate


def _method(request, *allowed) -> HttpResponse | None:
    """Answer a known path with an unknown method as 405 + Allow."""
    if request.method not in allowed:
        return HttpResponse(status=405, headers={"Allow": ", ".join(allowed)})
    return None


# The request helpers both transports share (apps/calls/http.py), bound under the names
# this module's handlers and tests have always used.
_json_body = http.json_body
_unprocessable = http.unprocessable
_peer_uuid = http.peer_uuid


def _query_since(request, param: str = "since"):
    raw = request.GET.get(param)
    if raw is None:
        return None
    try:
        return wire.parse_rfc3339(raw)
    except ValueError as exc:
        raise ApiError(400, wire.E_VALIDATION, f"invalid `{param}`: {exc}")


def _query_int(request, param: str, default: int) -> int:
    raw = request.GET.get(param)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ApiError(400, wire.E_VALIDATION, f"invalid `{param}`: {raw!r}")


def _listing_rows(listing, what: str) -> list:
    """Read a `reads.Rows | reads.Empty` WITHOUT discarding which one it is.

    Every list route here used to spell this `listing.rows if isinstance(listing,
    reads.Rows) else []`, and that `else` is the whole reason `reads.py` was written:
    an `Empty` is a query that PROVED its context was bound before it ran, and it
    carries the SEARCH SPACE it proved it searched (`Empty.scope`). Collapsing it to a
    bare `[]` at the boundary threw that away, so the typed absence the module exists to
    provide was made by NO caller - the routes were back to "an empty list means
    something, probably".

    Two things change. The scope is RECORDED, so an operator reading the log of a 200
    `[]` sees what was actually queried instead of inferring it. And anything that is
    neither arm RAISES instead of rendering as "no data": a reader that grew a third
    outcome, or that returned a bare list again, must not be able to reach the wire as
    an empty array.
    """
    if isinstance(listing, reads.Rows):
        return listing.rows
    if isinstance(listing, reads.Empty):
        # The scope, not just "empty": "no rows" and "no rows FOR THIS SEARCH" are
        # different statements and only the second one is actionable.
        log.info("%s: no rows, scope=%s", what, listing.scope)
        return []
    raise TypeError(
        f"{what}: expected reads.Rows or reads.Empty, got "
        f"{type(listing).__name__} - an untyped absence cannot be told apart from an "
        "unbound RLS context, which is the failure reads.py exists to prevent")


_resolve_visible_call = http.resolve_visible_call


# -------------------------------------------------------------------- invoke

_envelope_from = http.envelope_from


def _optional_str(body: dict, key: str) -> str | None:
    value = body.get(key)
    if value is None or isinstance(value, str):
        return value
    raise _unprocessable(f"`{key}` must be a string or null")


def _mode_override(body: dict) -> str | None:
    """An unknown mode is a deserialization failure (422),
    which is NOT the same as the 400 E_VALIDATION the PIPELINE raises for a mode that
    deserializes but is not allowed on this skill (`sync` on a >30s skill). Two
    different rejections, two different statuses, and a client can tell them apart."""
    value = body.get("mode_override")
    if value is None:
        return None
    if value not in ("sync", "async", "stream", "lesson"):
        raise _unprocessable(f"unknown variant `{echo_text(value)}`, expected one of "
                             "`sync`, `async`, `stream`, `lesson`")
    return value


#: The most file ids one call may attach (divergence 088, issue #156). Since #151 admission runs the file
#: read check per id, one or two queries each, so an uncapped list let one body near the upload limit cost
#: tens of thousands of queries. A mesh message may cite 8 files (divergence 079); calls get room beyond that.
MAX_ARGS_ATTACHMENTS = 32


def _args_attachments(body: dict) -> list[uuid.UUID]:
    """Absent attachments mean empty. At most
    MAX_ARGS_ATTACHMENTS, refused before any element is parsed or any database work."""
    raw = body.get("args_attachments")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise _unprocessable("`args_attachments` must be an array")
    if len(raw) > MAX_ARGS_ATTACHMENTS:
        raise _unprocessable(f"`args_attachments` has {len(raw)} entries; at most {MAX_ARGS_ATTACHMENTS} are allowed")
    out = []
    for item in raw:
        try:
            out.append(uuid.UUID(str(item)))
        except (ValueError, AttributeError, TypeError):
            raise _unprocessable(f"`args_attachments` contains a non-uuid: {item!r}")
    return out


def _accepted_body(result) -> dict:
    """Build the accepted response in its stable key order."""
    body = {
        "call_id": result.call_id,
        "status": result.status,
        "skill_version": result.skill_version,
        "mode": result.mode,
        "deadline_at": wire.optional_rfc3339(result.deadline_at),
        # A stream call is consumed over the WS frame protocol or by polling chunks;
        # there is no webhook delivery for chunks, which is why the list differs.
        "return_via": (["ws", "poll-chunks"] if result.mode == "stream"
                       else ["ws", "webhook", "poll"]),
    }
    if result.mode == "stream":
        body["chunks_url"] = f"/v1/calls/{result.call_id}/chunks?since=0"
    # Appended LAST, after every key a client already knew, so the earlier body is a prefix of this one and a client
    # that compares the known keys in order and ignores unknown trailing keys keeps working (kxxx, msg 3c5e5a).
    body["replayed"] = result.replayed
    return body


def _poll_sleep(seconds: float) -> None:
    """The one sleep this module's polls take. A module-local name, so a test hooks THIS
    view's loop (`monkeypatch.setattr(views, "_poll_sleep", ...)`) instead of the
    process-wide `time.sleep`, which every thread in the process shares and a count of
    which would measure the process, not the loop (review of 129e325, finding 2)."""
    time.sleep(seconds)


def _await_terminal(conn, call_id: str, timeout: float) -> dict | None:
    """Poll the call row until it is terminal or the wait expires.

    This poll reaches the answer whenever another
    process completes the call, and reaches it immediately when the call is already
    terminal (an idempotent replay of a finished call). `timeout=0.0`, the default,
    still performs ONE read - "already terminal" is a real and common case and skipping
    the read would turn a 200 into a 202.

    Returns the terminal row, or None on timeout (-> 202 + Location). Terminal is what the ONE
    terminal mapping says it is (`terminal.TERMINAL_STATUSES`, divergence 041 S6), the set
    `_terminal_body` then reads the row through.
    """
    deadline = time.monotonic() + timeout
    while True:
        found = reads.load_call(conn, call_id)
        if isinstance(found, reads.Found) and found.row["status"] in terminal.TERMINAL_STATUSES:
            return found.row
        if time.monotonic() >= deadline:
            return None
        _poll_sleep(0.05)


def _terminal_body(result, row: dict) -> dict:
    """Render a sync call that ended during the wait, or a FRESH call step 9 ended
    (divergence 017). A REPLAY of an already-terminal call is `_already_terminal_body`.

    The failed branch emits `result`, `signature`, `signing_kid` and `completed_at`
    unconditionally: they are present when the call has a SIGNED
    failure and null on the unsigned skill_error path, and emitting them either way is
    what lets a caller read one field instead of guessing from which key exists.

    Every value is read through `terminal.of_row`, the ONE terminal mapping the WebSocket
    terminal frame reads too (divergence 041 S6, design 4.7), so a poller and a WS caller
    cannot be told two outcomes for one row. `_await_terminal` hands only terminal rows here.
    """
    outcome = terminal.of_row(row)
    if outcome is None:
        raise ValueError(f"call {result.call_id} is not terminal ({row['status']})")
    body = {
        "call_id": result.call_id,
        "skill_version": result.skill_version,
        "mode": result.mode,
        "status": outcome.status,
    }
    if outcome.status == terminal.STATUS_COMPLETED:
        body["result"] = outcome.result
        body["signature"] = outcome.signature
        body["signing_kid"] = outcome.signing_kid
        body["completed_at"] = wire.optional_rfc3339(outcome.completed_at)
    elif outcome.status == terminal.STATUS_FAILED:
        body["error_code"] = outcome.error_code
        body["error_message"] = outcome.error_message
        body["result"] = outcome.result
        body["signature"] = outcome.signature
        body["signing_kid"] = outcome.signing_kid
        body["completed_at"] = wire.optional_rfc3339(outcome.completed_at)
    # timeout / abandoned carry a status and nothing else.
    return body


def _already_terminal_body(result, row: dict) -> dict:
    """Render a replayed call that was NOT IN FLIGHT when `start_call` returned it.

    This includes terminal or paused calls, which are re-fetched and rendered with this
    response shape rather than `_terminal_body`.

    Its keys, in this order: `call_id`, `status` (the re-fetched row's),
    `skill_version`, `mode` (the literal "sync"), `deadline_at`, `result`, `error_code`,
    `error_message`. The last three are emitted even when null. There is no `signature`,
    `signing_kid` or `completed_at`: a caller that wants them polls `GET /v1/calls/{id}`.
    STATUS C07; until it, this route answered this branch with `_terminal_body`.

    WHEN IT APPLIES HERE: `result.replayed` and a status outside
    `Routed | Queued | Pending | Running`, i.e. an idempotent replay of a finished call
    OR of a paused one (`input_required` / `auth_required`). Until the review of
    129e325 this route gated on a TERMINAL status, and a replayed paused call fell into
    the wait (202, or a block of up to 30 s). A FRESH call that step 9 finished before
    `start_call` returned (a memory-authority call, divergence 017) keeps
    `_terminal_body`: the node client's inline read wants the signature it carries
    (docs/DIVERGENCES.md 017).
    """
    return {
        "call_id": result.call_id,
        "status": row["status"],
        "skill_version": result.skill_version,
        "mode": "sync",
        "deadline_at": wire.optional_rfc3339(result.deadline_at),
        "result": row["result"],
        "error_code": row["error_code"],
        "error_message": row["error_message"],
    }


@_route("POST")
def invoke(request, conn, peer_id, name):
    """POST /v1/agents/{peer_id}/skills/{name}/invoke.

    A CLIENT NOTE THAT IS EASY TO READ AS A BUG AND IS NOT ONE: on this transport the
    caller MUST echo the signed `call_id` in the body. `InvokeBody.call_id` is
    optional, and when it is absent step 1 of the pipeline GENERATES a fresh UUID -
    which step 4b then compares against the call_id the envelope committed to, and
    refuses the mismatch with 403. The WS path derives the key from the payload, so only HTTP has this
    obligation. It is not softened here: the alternative - defaulting the effective
    call_id to the signed one - would let the envelope choose its own idempotency key
    and turn the rebind guard into a tautology.
    """
    auth = authenticate_peer(conn, request)
    body = _json_body(request)
    target_peer_id = _peer_uuid(peer_id)

    envelope = _envelope_from(body)
    memory_peer_id = routing.memory_peer_id_from_settings()
    # An OBJECT capability in Meshychat-Capability (the project's design log (not part of this release) 040, slice 6): read and
    # pre-checked HERE, before start_call, so a token that does not parse or a caller that
    # does not hold it is a 403 that starts no call and burns no nonce. None without the
    # header, which is every call before slice 6.
    principal = object_capability.presented_for_call(
        request, conn, caller_peer_id=auth.peer_id, target_peer_id=target_peer_id,
        skill_name=name, memory_peer_id=memory_peer_id)
    inp = pipeline.StartCallInput(
        # THE AUTHENTICATED IDENTITY, never a body field. The whole invariant in
        # pipeline.py rests on this argument coming from the JWT subject.
        caller_peer_id=auth.peer_id,
        target_peer_id=target_peer_id,
        skill_name=name,
        # version_pin and capability_token are None on the HTTP path. The route does not
        # accept them; the object capability rides in its own field.
        version_pin=None,
        capability_token=None,
        object_capability=principal,
        envelope=envelope,
        mode_override=_mode_override(body),
        transport=pipeline.TRANSPORT_HTTP,
        call_id=_optional_str(body, "call_id"),
        webhook_url=_optional_str(body, "webhook_url"),
        webhook_secret_hash=_optional_str(body, "webhook_secret_hash"),
        args_attachments=_args_attachments(body),
    )

    # Best effort: a failure is a warning that does not fail
    # the invoke. A presence side effect must never be able to refuse a call.
    try:
        reads.touch_last_activity(conn, auth.peer_id)
    except Exception as exc:  # noqa: BLE001 - deliberate: logs and continues
        log.warning("presence activity touch failed; continuing invoke: %s: %s",
                    type(exc).__name__, exc)

    # Gate 8 is ON unless the operator opted out (the project's design log (not part of this release) 036), through the
    # reader every transport shares, so the two routes cannot disagree about it.
    ctx = pipeline.CallsContext(conn=conn,
                                envelope_nonce_enforce=nonce_ledger.enforce_from_settings(),
                                memory_peer_id=memory_peer_id)
    try:
        result = pipeline.start_call(ctx, inp)
    except RpcError as exc:
        raise wire.api_error_from_rpc(exc)

    if result.mode != "sync":
        # Async / Stream / Lesson: 202 immediately.
        return wire.json_response(
            _accepted_body(result), status=202,
            headers={"Location": f"/v1/calls/{result.call_id}"})

    if result.replayed and result.status not in reads.IN_FLIGHT_STATUSES:
        # Re-fetch and use this branch's response body. `Routed | Queued | Pending | Running`
        # are the only statuses that wait, so every other status - terminal AND the paused
        # `input_required` / `auth_required` - lands here (review of 129e325, finding 1).
        found = reads.load_call(conn, result.call_id)
        if isinstance(found, reads.NotFound):
            raise ApiError(404, wire.E_NOT_FOUND, "not found")
        return wire.json_response(_already_terminal_body(result, found.row), status=200)

    row = _await_terminal(conn, result.call_id, _sync_poll_seconds())
    if row is None:
        return wire.json_response(
            _accepted_body(result), status=202,
            headers={"Location": f"/v1/calls/{result.call_id}"})
    return wire.json_response(_terminal_body(result, row), status=200)


# --------------------------------------------------------------------- reads

@_route("GET")
def list_mine(request, conn):
    """GET /v1/calls?role=caller|target.

    `role` is REQUIRED and has no default, so a missing one is a 400 rather than a silent
    "both sides".
    """
    auth = authenticate_peer(conn, request)

    role = request.GET.get("role")
    if role is None:
        raise ApiError(400, wire.E_VALIDATION,
                       "Failed to deserialize query string: missing field `role`")
    if role not in (reads.ROLE_CALLER, reads.ROLE_TARGET):
        raise ApiError(400, wire.E_VALIDATION, "role must be 'caller' or 'target'")

    status = request.GET.get("status")
    if status is not None and status not in reads.CALL_STATUSES:
        raise ApiError(400, wire.E_VALIDATION, f"unknown status: {status}")

    since = _query_since(request)
    limit = _query_int(request, "limit", reads.DEFAULT_LIST_LIMIT)
    limit = max(1, min(limit, reads.MAX_LIST_LIMIT))   # `.clamp(1, 500)`

    listing = reads.list_for_peer(conn, auth.peer_id, role, status, since, limit)
    rows = _listing_rows(listing, "GET /v1/calls")
    return wire.json_response([wire.call_json(r, auth.peer_id) for r in rows])


@_route("GET")
def get_call(request, conn, call_id):
    """GET /v1/calls/{call_id}."""
    auth = authenticate_peer(conn, request)
    row = _resolve_visible_call(conn, call_id, auth.peer_id)
    return wire.json_response(wire.call_json(row, auth.peer_id))


@_route("GET")
def get_receipt(request, conn, call_id):
    """GET /v1/calls/{call_id}/receipt.

    The receipt is DERIVED and UNSIGNED at the receipt level, and that is a decision
    rather than a gap: the server holds only peer PUBLIC keys, so it cannot sign as the
    executor. Non-repudiation comes from the per-field signatures returned alongside -
    `args_signature` (caller-signed args) and `result_signature` (executor-signed
    result) - each verifiable against that peer's published key.

    EXCEPT for a memory call (docs/DIVERGENCES.md 017): this server executed it, so
    `result_signature` is the SERVER's and its kid resolves in /.well-known/jwks.json.
    Since operator decision 38 (2026-09-26, F07) the same key is also registered under
    the memory peer the receipt names as `issuer` (apps/calls/result_signing.
    register_under_memory_peer), so (issuer, kid) resolves there too; the receipt shape is
    unchanged.
    """
    auth = authenticate_peer(conn, request)
    row = _resolve_visible_call(conn, call_id, auth.peer_id)
    provenance = reads.receipt_provenance(conn, call_id, row["grant_id"])
    body = {"receipt": wire.receipt_json(row, provenance)}
    # Omit each of these four fields when it is None.
    for key in ("args_signature", "args_signing_kid",
                "result_signature", "result_signing_kid"):
        if row[key] is not None:
            body[key] = row[key]
    return wire.json_response(body)


@_route("GET")
def get_events(request, conn, call_id):
    """GET /v1/calls/{call_id}/events."""
    auth = authenticate_peer(conn, request)
    _resolve_visible_call(conn, call_id, auth.peer_id)
    listing = reads.list_events(conn, call_id, _query_since(request))
    rows = _listing_rows(listing, f"GET /v1/calls/{call_id}/events")
    return wire.json_response([wire.event_json(r) for r in rows])


@_route("GET")
def get_chunks(request, conn, call_id):
    """GET /v1/calls/{call_id}/chunks. `since` defaults to 0.

    The chunk query is EXCLUSIVE (`sequence > since`), so the default never returns a chunk
    stored at sequence 0, which a target may send (design 2.12.11). The default (0, which
    skips sequence 0) is the existing wire contract and is kept (divergence 041 S7, the
    approved Q15); `?since=-1`
    returns every stored chunk, 0 included. The WS replay does not read through here: it reads
    from sequence 0 (apps/calls/chunks.replay). A call keeps its newest 200 chunks once it is
    terminal (the trim at terminal, 041 S7), so older ones are gone from this route too."""
    auth = authenticate_peer(conn, request)
    _resolve_visible_call(conn, call_id, auth.peer_id)
    listing = reads.list_chunks(conn, call_id, _query_int(request, "since", 0))
    rows = _listing_rows(listing, f"GET /v1/calls/{call_id}/chunks")
    return wire.json_response([wire.chunk_json(r) for r in rows])


# -------------------------------------------------------------------- cancel

def _not_in_flight(status: str) -> ApiError:
    """Map a non-in-flight call to its conflict response.

    The message uses the title-cased status spelling, so the wire text is
    `call not in flight (status: Completed)`, not `... completed`.
    """
    return ApiError(409, wire.E_CONFLICT,
                    f"call not in flight (status: {wire.debug_status(status)})")


@_route("POST")
def cancel(request, conn, call_id):
    """POST /v1/calls/{call_id}/cancel.

    TWO DIFFERENT 403s, in this order:
      * `assert_peer_can_see` first - a stranger gets 403 before learning anything else;
      * then `cancel_call`'s own `source == caller` check - the TARGET can READ the call
        but may not cancel it, and also gets 403.
    Both render E_FORBIDDEN.

    409 E_CONFLICT is the answer for a call that is not in flight, except that a second
    cancel of an ALREADY-ABANDONED call is idempotent success - a retried cancel
    must not become an error.
    """
    auth = authenticate_peer(conn, request)
    body = _json_body(request)
    reason = _optional_str(body, "reason")

    existing = _resolve_visible_call(conn, call_id, auth.peer_id)

    caller = existing["caller_peer_id"]
    if caller is None or uuid.UUID(str(caller)) != uuid.UUID(str(auth.peer_id)):
        raise ApiError(403, wire.E_FORBIDDEN, "forbidden")
    # This route returns the call JSON without viewer-specific fields. `call_json` is
    # therefore called WITHOUT a viewer on every exit below, so no `continuation_nonce`
    # is ever attached - a distinction visible only on a self-call.
    if existing["status"] not in reads.IN_FLIGHT_STATUSES:
        # 409 ON EVERY TERMINAL STATUS, INCLUDING `abandoned`. A call outside
        # {Pending, Routed, Queued, Running} is a conflict. Returning 200 for an
        # already-abandoned call would make a SECOND cancel look like it had done something.
        #
        # The idempotent-200 below is NOT the same case: there,
        # the call was in flight when it was read and lost a race to a concurrent
        # abandon. "Someone else abandoned it while I asked"
        # is success; "it was already terminal when I asked" is a conflict.
        raise _not_in_flight(existing["status"])

    # ONE transaction for the state change AND its audit row - see
    # reads.cancel_to_abandoned. These were two autocommit statements, and the window
    # between them could leave a call abandoned with nothing recording who did it.
    updated = reads.cancel_to_abandoned(conn, call_id, auth.peer_id, reason)
    if updated is None:
        # Lost the race to a concurrent terminal transition. Re-read to find out to
        # WHAT: already-abandoned is idempotent success, anything else is the conflict.
        current = reads.load_call(conn, call_id)
        if isinstance(current, reads.NotFound):
            raise ApiError(404, wire.E_NOT_FOUND, "not found")
        if current.row["status"] == "abandoned":
            return wire.json_response(wire.call_json(current.row))
        raise _not_in_flight(current.row["status"])

    return wire.json_response(wire.call_json(updated))


__all__ = ["invoke", "list_mine", "get_call", "get_receipt", "get_events",
           "get_chunks", "cancel"]
