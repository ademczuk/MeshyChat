"""HTTP surface for the A2A shim: the Task read, SSE stream, and `message:send`.

The agent-card discovery routes are served by `apps/peers` and are not duplicated here;
see `urls.py`.

THE AUTHORIZATION SHAPE, identical on all three and identical to `/v1/calls/{id}`:
resolve the call by its UNIQUE `call_id` under the SERVER-INTERNAL context, then check
the authenticated peer is its caller or its target. 404 for an unknown id comes FIRST,
so `404 = no such call` and `403 = not yours` stay distinguishable - collapsing them
would be defensible privacy design and would not be this server.

SHARED COMPONENTS.
  * `apps.calls.reads` owns the typed call/chunk reads (`Found | NotFound | Denied`,
    `Rows | Empty`) and the RLS context each one runs in. This module calls them.
  * `apps.calls.pipeline.start_call` owns the invoke gate order. `message:send` is a
    boundary translator onto that same seam. A second copy of the pipeline here would be
    a second copy of the authorization policy, free to drift.
  * `apps.peers.auth.authenticate_peer` is the single `AuthenticatedPeer` extractor and
    `apps.peers.errors.ApiError` the single error enum for every router.

NO PER-REQUEST AGGREGATE anywhere in this module. The Task read is one keyed lookup on
`skill_calls.call_id` (UNIQUE); the stream is a `sequence > last_seq` RANGE scan on
`skill_call_chunks (call_id, sequence)` - which is the UNIQUE index - read in pages of
`STREAM_CHUNK_PAGE`, plus one keyed TWO-COLUMN status read per poll
(`reads.load_call_status`). There is no COUNT(*) and no chain walk. STATUS C73, 2026-09-26:
the scan used to be unbounded and the status read used to fetch the whole 34-column row.

CONNECTIONS, measured by C73 and NOT changed here: one unpooled connection per request,
and a second for a stream's generator, held for as long as the stream is open (up to
`STREAM_MAX_POLLS` idle polls, ~3 min). A pool belongs in `apps/core/dbconn`, under every
route at once; it is not this module's to add.

KNOWN LIMITS OF THIS SHIM, stated plainly because a silent one is the worst outcome:

  1. NO TIER QUOTA on `message:send`: there is no tier service in this build, so no
     `calls_per_cycle` reservation is taken and `402 E_QUOTA_EXCEEDED` is unreachable. What
     charges a call here is B12's per-peer bucket, taken INSIDE `start_call` (pipeline
     step 0, docs/DIVERGENCES.md 003), once per call on every transport, answering 429.
  1b. An INACTIVE TARGET is 403 E_FORBIDDEN, not 404. The peer extractor refuses an inactive
     caller with 403, and `start_call` step 5 checks the target's `status = 'active'`
     predicate through `lookup_owner_for_peer`, which raises `NotAuthorized` -> 403.
     Disclosed on 2026-09-26 (STATUS C72) and pinned by
     `tests/test_a2a.py` test_message_send_to_an_inactive_target_is_403_and_writes_no_call.
  2. NO BLOCKING WAIT FOR A TERMINAL RESULT: there is no result channel here, so a sync
     call cannot block until the target answers. A memory-authority call ends inside
     `start_call` (docs/DIVERGENCES.md 017) and a call to another peer is `routed` or
     `queued` (041 S5), so this POLLS the row for `MESHY_SYNC_TIMEOUT_SECONDS` (default
     0.0, one read). The Task is projected from the row as it stands, and an
     already-terminal replay still returns terminal.
  3. NO `202 E_APPROVAL_PENDING` on `message:send`. The call pipeline has no
     human-approval flow, so no `RpcError` in `apps/calls/errors.py` carries it. When
     an approval flow lands in the pipeline, this route gets its 202 with no edit,
     because the status comes from `wire.api_error_from_rpc`.
  4. NO SSE KEEP-ALIVE comment frame; see `stream_task`.

  The SSE generator catches a failed read at the GENERATOR BOUNDARY (`_stream_body`) and
  ends the stream with a terminal `TASK_STATE_FAILED` frame. A transient read error on a
  still-running call therefore closes this stream. The reasoning is at `_stream_body`.

  The SSE stream is a POLL LOOP over the persisted chunk buffer, so it needs no Redis or
  WebSocket coupling.
"""
from __future__ import annotations

import json
import logging
import time

from django.http import HttpResponse, StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt

from apps.a2a import types
from apps.calls import http, nonce_ledger, object_capability, pipeline, reads, terminal, wire
from apps.calls import route as routing
from apps.calls.errors import RpcError
from apps.core import dbconn
from apps.crypto.ijson import echo_text
from apps.peers.auth import authenticate_peer
from apps.peers.errors import ApiError

log = logging.getLogger("meshychat.a2a")

#: The CEILING on the poll, not its default; see divergence 2 in the module docstring.
A2A_SYNC_TIMEOUT_SECONDS = 30.0

#: Poll cadence for an idle A2A stream.
STREAM_POLL_SECONDS = 0.2
STREAM_MAX_POLLS = 900          # ~3 min of idle polling before forcing a close

#: The stream's chunk reads are PAGES of this many rows so that a reconnect to a long
#: stream never reads every stored chunk in one unbounded statement. The stream continues
#: from the last emitted sequence until a page comes back short, so every chunk leaves once,
#: in order.
STREAM_CHUNK_PAGE = 100

#: The key `args` in the signed payload can be absent or hold JSON null; these are different
#: values, and both fail equality against the A2A data part.
_MISSING = object()


# ------------------------------------------------------------------------ plumbing

def _route(*allowed):
    """Answer the METHOD, then open ONE guarded connection, run the handler, render an
    ApiError. Used as `@_route("GET")`.

    THE METHOD FIRST (STATUS C73). An unknown method returns 405 + Allow before a
    connection is opened; otherwise unauthenticated 405 requests would pay a full connect
    and the guard's round trips.

    `dbconn.serving_connection` rather than a bare connect: it REFUSES a connection
    that could bypass row-level security or that is not in autocommit, and both failure
    modes are silent in the direction of disclosure.
    """
    def decorate(handler):
        guarded = _guarded(handler)

        def by_method(request, *args, **kwargs):
            if request.method not in allowed:
                return HttpResponse(status=405, headers={"Allow": ", ".join(allowed)})
            return guarded(request, *args, **kwargs)
        by_method.__name__ = getattr(handler, "__name__", "route")
        return csrf_exempt(by_method)
    return decorate


def _guarded(handler):
    """The connection and error half of `_route`: apps/calls/http.py `serve`, the same
    body the native routes run, logging under this module's logger."""
    def wrapper(request, *args, **kwargs):
        return http.serve(log, handler, request, *args, **kwargs)
    return wrapper


# The request helpers in apps/calls/http.py, bound under the names this module's handlers
# and tests use.
_peer_uuid = http.peer_uuid
_json_body = http.json_body
_unprocessable = http.unprocessable
_resolve_visible_call = http.resolve_visible_call


# ------------------------------------------------------------- GET /a2a/tasks/{id}

@_route("GET")
def get_task(request, conn, call_id):
    """Get an A2A Task by call id.

    `context_id` is `None` on this route, so `contextId` is absent from the body.
    `skill_calls` has no A2A context column, and deriving one from e.g.
    `pause_context_id` would put an unrelated value on the wire.
    """
    auth = authenticate_peer(conn, request)
    row = _resolve_visible_call(conn, call_id, auth.peer_id)
    task = types.task_from_meshy_call(row["call_id"], None, row["status"], row["result"])
    return wire.json_response(task)


# ------------------------------------------------------ GET /a2a/tasks/{id}/stream

def _sse_frame(event_name: str, payload: dict) -> bytes:
    """One SSE frame with event before data and a blank-line terminator.

    `json.dumps` defaults to `', '` and `': '` separators and `ensure_ascii=True`, which
    would escape non-ASCII as `\\uXXXX` and alter the bytes of the frame.
    """
    data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return f"event: {event_name}\ndata: {data}\n\n".encode("utf-8")


def _drain_page(conn, call_id: str, last_seq: int, append: bool):
    """ONE page of the chunk buffer past `last_seq`, yielded as `TaskArtifactUpdateEvent`
    frames. Returns `(rows, last_seq, append)`: `rows` is the page's row count, or None
    when the read did not come back as `reads.Rows`, and the cursor pair is carried back
    so the next page, at either call site in `_stream_frames`, continues it (the first
    chunk of the stream is the only one with `append` false). `STREAM_CHUNK_PAGE` is read
    here, per page, so a test that narrows the module's page narrows both sites."""
    page = reads.list_chunks(conn, call_id, last_seq, limit=STREAM_CHUNK_PAGE)
    if not isinstance(page, reads.Rows):
        return None, last_seq, append
    for chunk in page.rows:
        last_seq = chunk["sequence"]
        yield _sse_frame("TaskArtifactUpdateEvent",
                         types.chunk_to_artifact_event(call_id, chunk, append))
        append = True
    return len(page.rows), last_seq, append


def _terminal_frame(call_id: str, state: str) -> bytes:
    """The `TaskStatusUpdateEvent` a stream closes with, in whichever terminal state."""
    return _sse_frame("TaskStatusUpdateEvent", types.task_status_update_event(
        task_id=call_id, status=types.task_status(state=state)))


def _stream_frames(call_id: str):
    """Generator-side poll loop for a call's persisted chunks.

    ITS OWN CONNECTION, opened and closed by the `with` below. A streaming response's
    generator runs AFTER the view returns, so the request-scoped connection this app
    opened in `_route` is already closed by then; borrowing it would read from a closed
    handle on the first poll. The `with` also closes it when the client disconnects and
    the generator is garbage-collected mid-stream.

    THE FINAL DRAIN BEFORE CLOSE IS LOAD-BEARING. A chunk can
    commit in the window between the `list_since` at the top of a poll and the terminal
    status read below it - `report_chunk` commits before the call's terminal transition
    - so the buffer is re-drained after the status is found terminal. Without it a live
    stream silently truncates its tail, which looks like a client bug.

    The authorization has ALREADY happened in the view, before this generator was
    handed to the response: a 403 must be a JSON body with a 403 status, not an SSE
    stream that opens 200 and then says nothing.
    """
    with dbconn.serving_connection() as conn:
        last_seq = 0
        append = False
        polls = 0
        while True:
            rows, last_seq, append = yield from _drain_page(conn, call_id, last_seq, append)
            emitted = bool(rows)
            if rows is not None and rows >= STREAM_CHUNK_PAGE:
                # A FULL page may have more stored chunks. Drain before reading the status.
                continue

            # The STATUS only (STATUS C73): `load_call` would re-read all 34 columns,
            # `args` and `result` jsonb included, on every poll. The view has already
            # authorized this viewer against the full row.
            found = reads.load_call_status(conn, call_id)
            # A terminal call or a row that vanished mid-stream closes the stream. A vanished
            # row has no status to project, so it closes as FAILED.
            if isinstance(found, reads.NotFound):
                state = types.TASK_STATE_FAILED
            else:
                state = types.task_state_for(found.row["status"])
                if not types.is_terminal(state):
                    # Not terminal. Back off only when the poll produced NOTHING, so a chatty
                    # call re-drains immediately and does not spend a poll from the ceiling; the
                    # ceiling exists to bound an IDLE stream, and charging it for
                    # productive polls would cut a long, healthy stream short.
                    if emitted:
                        continue
                    polls += 1
                    if polls >= STREAM_MAX_POLLS:
                        return
                    _poll_sleep(STREAM_POLL_SECONDS)
                    continue

            while True:
                rows, last_seq, append = yield from _drain_page(conn, call_id, last_seq,
                                                                append)
                if rows is None or rows < STREAM_CHUNK_PAGE:
                    break
            yield _terminal_frame(call_id, state)
            return


def _stream_body(call_id: str):
    """`_stream_frames`, with the guarantee that it ENDS rather than escapes.

    `_route` CANNOT cover this generator. It runs after the view returned, so the `200
    text/event-stream` status line and headers are already on the wire: there is no
    status left to turn into a 500, and an exception raised here propagates straight
    into the WSGI server, which drops the connection mid-body. An SSE consumer cannot
    tell that drop from a stream that simply finished - the framing carries no
    end-of-stream marker of its own - so the failure would be INVISIBLE to the client
    and to anything reading only the access log.

    A read error produces a terminal `TaskStatusUpdateEvent` carrying
    `TASK_STATE_FAILED`, the same frame used for a vanished row rather than a new event
    type. Catching at the boundary also covers `types.UnknownChunkType` and
    `UnknownCallStatus`. The narrow cost is that a failing read closes a still-running
    stream; a terminal close is recoverable by re-issuing GET, while a silent close is not.

    NOT A SWALLOW: the cause is logged at ERROR with its traceback, because a generator
    that hides a psycopg failure behind a clean-looking close is worse than one that
    crashes loudly. `GeneratorExit` is a `BaseException` and is deliberately NOT caught
    - a client disconnect must unwind into `_stream_frames` so its `with` closes the
    connection, and yielding during that unwind is a `RuntimeError`.
    """
    try:
        yield from _stream_frames(call_id)
    except Exception:  # noqa: BLE001 - logged in full immediately below
        log.exception(
            "GET /a2a/tasks/%s/stream: the SSE generator failed after the response "
            "opened; closing the stream with a terminal %s", call_id,
            types.TASK_STATE_FAILED)
        yield _terminal_frame(call_id, types.TASK_STATE_FAILED)


@_route("GET")
def stream_task(request, conn, call_id):
    """Stream an A2A Task's persisted chunks.

    One `TaskArtifactUpdateEvent` per persisted stream chunk, then a terminal
    `TaskStatusUpdateEvent` when the call reaches a terminal status. Same
    caller-or-target gate as the task read, applied BEFORE the stream is opened.

    No SSE keep-alive comment frame is emitted. A blocking generator cannot emit one while
    it sleeps without restructuring the poll into a timer, so an idle stream behind an
    aggressive intermediary may be dropped sooner.
    """
    auth = authenticate_peer(conn, request)
    _resolve_visible_call(conn, call_id, auth.peer_id)

    response = StreamingHttpResponse(
        _stream_body(call_id),
        # SSE content type; Cache-Control: no-cache is set below.
        content_type="text/event-stream",
    )
    response["Cache-Control"] = "no-cache"
    return response


# --------------------------------------- POST /a2a/agents/{id}/skills/{name}/message:send

def _message_from(body: dict) -> dict:
    """Validate the required A2A message object.

    The required fields are `messageId`, `role` and `parts`; `contextId`, `taskId`, and
    `metadata` may be null. A missing required field is a 422: the request was JSON, but
    not this type.
    """
    raw = body.get("message")
    if raw is None:
        raise _unprocessable("missing field `message`")
    if not isinstance(raw, dict):
        raise _unprocessable("`message` must be an object")
    if not isinstance(raw.get("messageId"), str):
        raise _unprocessable("`message.messageId` must be a string")
    if raw.get("role") not in types.ROLES:
        raise _unprocessable(
            f"unknown variant `{echo_text(raw.get('role'))}`, expected one of {list(types.ROLES)}")
    parts = raw.get("parts")
    if not isinstance(parts, list) or any(not isinstance(p, dict) for p in parts):
        raise _unprocessable("`message.parts` must be an array of objects")
    ctx = raw.get("contextId")
    if ctx is not None and not isinstance(ctx, str):
        raise _unprocessable("`message.contextId` must be a string or null")
    return raw


_envelope_from = http.envelope_from


def _sync_poll_seconds() -> float:
    """See divergence 2 in the module docstring.

    Shares `MESHY_SYNC_TIMEOUT_SECONDS` with the native invoke route so the two transports
    cannot disagree about how long a sync call waits.
    """
    return http.sync_poll_seconds(A2A_SYNC_TIMEOUT_SECONDS)


def _poll_sleep(seconds: float) -> None:
    """The one sleep this module's polls take. A module-local name, so a test hooks THIS
    view's loop (`monkeypatch.setattr(views, "_poll_sleep", ...)`) instead of the
    process-wide `time.sleep`, which every thread in the process shares and a count of
    which would measure the process, not the loop (review of 129e325, finding 2)."""
    time.sleep(seconds)


def _await_terminal(conn, call_id: str, timeout: float) -> None:
    """Wait briefly for a terminal call state, then let the caller re-read the row.

    This returns nothing; its only job is to give a concurrent completion a chance to
    land. `timeout=0.0` performs no read because the caller re-reads the row
    unconditionally straight after.
    Terminal is what the ONE terminal mapping says it is (`terminal.TERMINAL_STATUSES`,
    divergence 041 S6): the statuses `types.CALL_STATUS_TO_TASK_STATE` projects to a
    terminal TaskState, which tests/test_a2a.py holds equal.
    """
    if timeout <= 0.0:
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = reads.load_call(conn, call_id)
        if isinstance(found, reads.Found) and found.row["status"] in terminal.TERMINAL_STATUSES:
            return
        _poll_sleep(0.05)


@_route("POST")
def message_send(request, conn, peer_id, skill_name):
    """Start a call from `POST /a2a/agents/{peer_id}/skills/{skill_name}/message:send`.

    AC#4 "A2A-FORMAT IN" IS THE WHOLE POINT OF THE TWO CHECKS AT THE TOP. The A2A
    message carries the args as exactly one data Part, and that Part MUST equal the
    signed envelope's `args`. Without the equality the A2A payload would be decorative:
    a caller could sign one call and present a different one in A2A clothing, and the
    shim would have translated a request nobody signed.

    AC#6: the caller identity comes from the AUTHENTICATED CHANNEL (`auth.peer_id`) and
    the target from the ROUTE PATH - never from the message body or the token payload.
    The pipeline derives the whole `GrantRequest` from those two, which is what keeps
    the channel-identity invariant true on this transport as well as the native one.
    """
    auth = authenticate_peer(conn, request)
    body = _json_body(request)
    target_peer_id = _peer_uuid(peer_id)

    msg = _message_from(body)
    envelope = _envelope_from(body)

    try:
        a2a_args = types.single_data_args(msg)
    except types.NoSingleDataPart as exc:
        raise ApiError(400, wire.E_VALIDATION,
                       "A2A message must carry exactly one data part (the skill args)"
                       ) from exc
    signed_args = envelope.payload["args"] if "args" in envelope.payload else _MISSING
    if signed_args is _MISSING or a2a_args != signed_args:
        raise ApiError(400, wire.E_VALIDATION,
                       "A2A message data part does not match the signed envelope args")

    context_id = msg.get("contextId")

    call_id = body.get("call_id")
    if call_id is not None and not isinstance(call_id, str):
        raise _unprocessable("`call_id` must be a string or null")

    memory_peer_id = routing.memory_peer_id_from_settings()
    # The OBJECT capability in Meshychat-Capability, pre-checked before the call starts, exactly
    # as the native invoke route does (apps/calls/object_capability.py; the project's design log (not part of this release)
    # 040, slice 6). None without the header.
    principal = object_capability.presented_for_call(
        request, conn, caller_peer_id=auth.peer_id, target_peer_id=target_peer_id,
        skill_name=skill_name, memory_peer_id=memory_peer_id)
    inp = pipeline.StartCallInput(
        caller_peer_id=auth.peer_id,
        target_peer_id=target_peer_id,
        skill_name=skill_name,
        # version_pin and capability_token are None here: this route authenticates by peer JWT
        # only. capability_token is the skill-grant gate 1 token; the object capability rides in
        # its own field.
        version_pin=None,
        capability_token=None,
        object_capability=principal,
        envelope=envelope,
        mode_override=None,
        transport=pipeline.TRANSPORT_HTTP,
        call_id=call_id,
        webhook_url=None,
        webhook_secret_hash=None,
        args_attachments=[],
    )

    ctx = pipeline.CallsContext(
        conn=conn,
        # Gate 8, ON unless opted out (the project's design log (not part of this release) 036); the same reader as invoke.
        envelope_nonce_enforce=nonce_ledger.enforce_from_settings(),
        # Step 9 routes on this (the project's design log (not part of this release) 017); the same reader as invoke, read
        # once above for the capability's pre-check too.
        memory_peer_id=memory_peer_id)
    try:
        result = pipeline.start_call(ctx, inp)
    except RpcError as exc:
        raise wire.api_error_from_rpc(exc)

    if result.mode == "sync":
        _await_terminal(conn, result.call_id, _sync_poll_seconds())

    # Re-read the row and project it. A row that has vanished between the insert and this
    # read is a 404, not a 500 or a fabricated in-flight Task.
    found = reads.load_call(conn, result.call_id)
    if isinstance(found, reads.NotFound):
        raise ApiError(404, wire.E_NOT_FOUND, "not found")
    row = found.row
    task = types.task_from_meshy_call(row["call_id"], context_id, row["status"],
                                     row["result"])
    return wire.json_response(task)


__all__ = ["get_task", "message_send", "stream_task"]
