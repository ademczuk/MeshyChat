"""The CALLER's frames: skill_call, skill_cancel and `call:` subscriptions (divergence 041 S6), and
the chunk replay (S7).

Design `docs/design/skill-frames.md` (fin/design-skill-frames, 2e229a6): section 7 row S6,
4.7 (skill_call, the registration order, one terminal mapping, cancel, subscribe), 4.6 (the
terminal arm, apps/wsgw/call_notify.py), 5.1 and 5.3, the approved Q7, Q12 and Q13, critique F1
and F2, risk R1, and defects 2.12.4 to 2.12.6 and 2.12.13.

SKILL_CALL (`handle_skill_call`, called by the reader; `_run_skill_call`, spawned):

  * SPAWNED, never awaited by the reader: the pipeline can wait on a lock or a
    slow statement, and an inline await would stall the socket's every later frame, a
    skill_cancel for this very call included. Two caps are taken WITHOUT WAITING before the
    spawn, and either one refused is SKILL_CALL_BUSY at once: the socket's own
    MAX_INFLIGHT_SKILL_CALLS (16), and PIPELINE_SLOTS, one process-wide semaphore of
    MAX_CONCURRENT_WS_PIPELINE_CALLS (R1). The pipeline runs on
    `_PIPELINE_EXECUTOR`, whose workers the process cap never exceeds, so a call never queues
    there unseen, and no pipeline runs on the default executor. The caller's short keyed reads
    do (the re-read after the ack, each subscribe read), as the target frames' statements do:
    one at a time per socket, since each is awaited in wire order (041 S6 review, item 11).
    The process slot is given back when the pipeline thread ENDS (a done-callback), not when
    the socket goes: the thread holds a database connection until then. A dispatch cancelled
    before its first step never enters its try/finally; its task's done-callback gives both
    slots back then (041 S6 review F7).
  * `target_peer_id`, `skill_name`, `skill_version` (the version pin) and `call_id` are read from
    the SIGNED payload; a target that is not a UUID is
    SKILL_CALL_BAD. `mode_override` is {sync, async, stream}, refused at parse otherwise
    (apps/wsgw/envelope.py, 2.12.7).
  * `pipeline.start_call` runs on a FRESH role-bound connection (`dbconn.serving_connection`,
    the guard every HTTP request passes), with `transport` hard-coded to TRANSPORT_WS: no frame
    field chooses a transport, so no WS frame can claim `dashboard_test`. A call terminal at
    return whose row this thread then fails to read is not a failed call: it takes the
    registration path below (041 S6 review F9; it was SKILL_CALL_FAILED E_INTERNAL, for a
    call that had completed). The CallsContext is
    `CallsContext(envelope_nonce_enforce=nonce_ledger.enforce_from_settings(),
    memory_peer_id=route.memory_peer_id_from_settings())`: 036's one reader (5.3, Q13) and the
    one memory-authority reader (the design writes `routing.`; on main it is
    apps/calls/route.py). No quota is applied here: `apps/calls/views.py` names `meshy_tier`, but
    this path does not. No capability token: Biscuit (040, on
    fin/biscuit, not on main) has put no WS
    carrier on main, and `StartCallInput` has no such field on this base.
  * A refusal is SKILL_CALL_FAILED with the HTTP route's rendering of the SAME error
    (`wire.api_error_from_rpc`), as `<code>: <message>`. The HTTP route flattens NotAuthorized
    to `forbidden` because its reason is an oracle (a missing grant, a replayed nonce), so the
    WS door does too.
  * REGISTRATION ORDER (4.7). A call that is terminal when start_call returns (the memory
    authority runs in-process, apps/calls/route.py; or a replay of a finished call) has already
    fired its terminal trigger, so nothing will ever deliver it: push skill_acked, then the
    terminal frame, and do NOT register. Otherwise: register (PROVEN: the pipeline has
    verified the caller's signature as this socket's peer), push skill_acked, then RE-READ the
    row; if it became terminal meanwhile, push the terminal frame and end the registration. A
    terminal committed before the registration is caught by the re-read, one committed after
    it by the CallBridge's terminal arm; one committed in between reaches the socket twice,
    which is harmless (the caller keys on call_id), where a missing one would not be. Since S7
    the call's CHUNKS are replayed between the ack and the re-read, for the same window: a
    chunk a fast target stored before the registration found nobody to deliver it to. A
    terminal the bridge delivers while that replay reads is held and pushed after the chunks,
    and then there is no re-read (the S7 review's M1). Not in the terminal-at-return branch:
    that is a memory call, which has no chunks, or a retry that step 2 answered from a
    finished call's row, whose chunks a `subscribe` replays;
    the one read would be paid by every memory call over WS.

SKILL_CANCEL (`handle_skill_cancel`), on the socket's own connection (apps/wsgw/target_frames.
SocketConnection), in wire order, not spawned: `cancel_by_caller`, ONE guarded statement,

    UPDATE skill_calls SET status = 'abandoned', completed_at = NOW()
     WHERE call_id = $1 AND caller_peer_id = $me AND status = ANY(in flight)
       AND <the peer is active> [AND <the socket's bound key is live>]

with the `caller_disconnected` event (`source = 'caller'`, the reason in `details`) in the same
transaction. NEVER `reads.cancel_to_abandoned`: it has no caller predicate and relies on the HTTP
view's own check (critique F1), so a frame handler calling it would let any peer cancel any call.
Zero rows: the call is re-read under the same caller and standing clauses; nothing found is
SKILL_CANCEL_FAILED with ONE text for "no such call" and "not yours" (Q7: the WS door closed; the
server log says which), and a call of this peer's that is not in flight is `call not in flight
    (status: <Debug>)`, the HTTP route's 409 text. Success sends nothing; the
status trigger's pointer brings the caller's registered sockets skill_error ABANDONED. Nothing
    tells the target (Q12); its next frame is refused. skill_resume stays
UNIMPLEMENTED with its close (2.10).

SUBSCRIBE `call:<id>` (`subscribe_calls`), at most MAX_CALL_SUBSCRIPTIONS_PER_CONN per socket,
and at most that many distinct `call:` channels READ per frame, a repeated one read once (041 S6
review F3: the live-registration cap alone bounded no work, since an unknown call registers
nothing and a finished one registers and unregisters in one step): REGISTER FIRST, provisional,
    then read the row (closing 2.12.5: a terminal between the two is lost). A provisional
    registration receives no frame
(apps/wsgw/call_registry.py): a stranger's socket, registered first, would otherwise hear another
caller's result for as long as its read took. Then: no such call, or a peer that is not the
call's caller (its target included), is unregistered and hears nothing, the reason counted
    (`subscribe:unknown_call`, `subscribe:not_caller`) and logged, never sent.
The caller's registration is proven, and then (S7) its CHUNKS are replayed: `read_chunks`, from
sequence 0 and at most the newest 200 (design 4.7, F9; not `reads.list_chunks`, whose `sequence >
since` skips 0, 2.12.11), each pushed and the registration's high-water mark advanced. Read
AFTER the proof, so a chunk committed after the read finds the registration proven and reaches
it live; one committed while it was provisional is in the read. If the row is terminal, or a
terminal came while it was provisional (re-read then), the terminal frame follows the chunks
    and the registration ends (2.12.4: a new subscriber to a finished call would otherwise
    never hear its terminal). A terminal the bridge delivers while the replay reads is
    held on the registration
and pushed after the replayed chunks (the S7 review's M1; apps/wsgw/call_registry.py). The ack
    echoes the channels as sent, after any such frame.
UNSUBSCRIBE `call:<id>` removes this socket's registration (2.12.6: otherwise later frames
would still reach an unsubscribed socket).

THE ONE TERMINAL MAPPING (`terminal_frame`): `apps/calls/terminal.of_row`, which the HTTP
terminal body reads too (apps/calls/views.py `_terminal_body`), including every terminal arm.

THE ONE CHUNK FRAME (`chunk_frame`, S7): a stored row, its `content` (the RFC 8785 bytes the
target signed) parsed back into `envelope.payload`, beside the stored
signature and kid. The replay here and the CallBridge's chunk arm and recovery sweep build every
skill_chunk a caller hears through it.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import json
import logging
import threading
import uuid

from psycopg.types.json import Jsonb

from apps.calls import chunks as call_chunks
from apps.calls import nonce_ledger, pipeline, reads, terminal, wire
from apps.calls import route as routing
from apps.calls.envelope import SignedEnvelope
from apps.calls.errors import RateLimited, RpcError
from apps.calls.repository import validate_call_id
from apps.core import dbconn, peer_limit, rls
from apps.crypto import primitives as P

from . import envelope, revocation, target_frames
from .constants import (
    MAX_CALL_SUBSCRIPTIONS_PER_CONN,
    MAX_CONCURRENT_WS_PIPELINE_CALLS,
    MAX_INFLIGHT_SKILL_CALLS,
)

log = logging.getLogger(__name__)

#: Prefix for caller subscription channels.
CALL_PREFIX = "call:"

#: The texts a caller's frames are answered with. Literals, pinned by the tests.
BUSY_CONNECTION = (f"too many concurrent skill calls on this connection "
                   f"(max {MAX_INFLIGHT_SKILL_CALLS}); retry shortly")
BUSY_SERVER = (f"too many concurrent skill calls on this server "
               f"(max {MAX_CONCURRENT_WS_PIPELINE_CALLS}); retry shortly")
BAD_TARGET = "missing or invalid target_peer_id in payload"
NOT_MADE_BY_THIS_PEER = "call was not made by this peer"
INTERNAL = f"{wire.E_INTERNAL}: internal error"

#: The one semaphore every socket's skill_call takes a slot of before its pipeline thread
#: starts (R1). Taken without waiting; given back when that thread ends.
PIPELINE_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_WS_PIPELINE_CALLS)

#: The pipeline threads' own pool: never more work in it than PIPELINE_SLOTS admits.
_PIPELINE_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=MAX_CONCURRENT_WS_PIPELINE_CALLS, thread_name_prefix="meshy-ws-skill-call")

#: pg_stat_activity's application_name for a pipeline thread's connection: this prefix and the
#: socket's connection_id. 50 characters, under Postgres's 63.
APPLICATION_NAME_PREFIX = "meshy-ws-call-"


class CallSlots:
    """One socket's MAX_INFLIGHT_SKILL_CALLS, taken without waiting and given back when the
    spawned dispatch ends. Used on the socket's event loop only."""

    def __init__(self, limit: int = MAX_INFLIGHT_SKILL_CALLS) -> None:
        self.limit = limit
        self.held = 0

    def try_acquire(self) -> bool:
        if self.held >= self.limit:
            return False
        self.held += 1
        return True

    def release(self) -> None:
        self.held -= 1


class _BadCall(Exception):
    """A skill_call whose signed payload names no usable target: SKILL_CALL_BAD."""


class _Dispatch:
    """Whether a spawned dispatch has begun (041 S6 review F7)."""

    __slots__ = ("began",)

    def __init__(self) -> None:
        self.began = False


# --------------------------------------------------------------------- the one mapping

def terminal_frame(row: dict) -> dict | None:
    """The frame a caller hears for a terminal row, or None for a row that is not terminal.
    Every value comes from `terminal.of_row`, the function the HTTP terminal body reads
    (design 4.7). Absent strings render as a signature or kid of "" and an unsigned failure
    code of `FAILED`."""
    outcome = terminal.of_row(row)
    if outcome is None:
        return None
    if outcome.status in (terminal.STATUS_COMPLETED, terminal.STATUS_FAILED) and outcome.signed:
        return envelope.skill_result(
            outcome.call_id, outcome.status,
            SignedEnvelope(payload=outcome.payload, signature=outcome.signature or "",
                           signing_kid=outcome.signing_kid or ""),
            payload_reconstructed=outcome.payload_reconstructed)
    if outcome.status == terminal.STATUS_FAILED:
        return envelope.skill_error(outcome.call_id, outcome.error_code or "FAILED",
                                    outcome.error_message or "")
    if outcome.status == terminal.STATUS_TIMEOUT:
        return envelope.skill_timeout(outcome.call_id)
    return envelope.skill_error(outcome.call_id, "ABANDONED", "call was cancelled")


def chunk_frame(call_id: str, row: dict) -> dict | None:
    """The skill_chunk a caller hears for a stored chunk row, or None for a row that cannot
    make one: no content (a file_ref row another writer left), or bytes that are not a JSON
    object in UTF-8 (every payload this server stores is one)."""
    content = row["content"]
    if content is None:
        return None
    try:
        payload = json.loads(bytes(content).decode("utf-8"))
        return envelope.skill_chunk(
            call_id, row["sequence"], row["chunk_type"],
            SignedEnvelope(payload=payload, signature=row["chunk_signature"],
                           signing_kid=row["chunk_signing_kid"]))
    except ValueError:      # not UTF-8, not JSON, or (the builder) not an object
        return None


def read_chunks(conn, call_id: str) -> list[dict]:
    """The call's stored chunks for a replay to this socket: `apps/calls/chunks.replay`, from
    sequence 0, at most the newest 200, oldest first (design 4.7, F9). The replay's one read,
    on the socket's own connection; the CallBridge reads through apps/calls/chunks itself."""
    return call_chunks.replay(conn, call_id)


def read_call(conn, call_id: str) -> dict | None:
    """The call's row by its UNIQUE call_id (server_internal, `reads.load_call`), or None.
    The one read the caller's frames make: skill_call's re-read, subscribe's ownership read."""
    found = reads.load_call(conn, call_id)
    return found.row if isinstance(found, reads.Found) else None


# --------------------------------------------------------------------------- skill_call

async def handle_skill_call(parsed: envelope.SkillCallFrame, ctx) -> None:
    """On the READER: refuse a caller whose CALLS bucket is empty (B12), take both slots
    without waiting, or answer SKILL_CALL_BUSY; then spawn the dispatch and return. `ctx` is
    the frame's FrameContext.

    The bucket is READ here and TAKEN by start_call, as on every transport: a refusal here
    costs no pipeline thread, no guarded connection and no warning line (review a65ab61f
    LOW 3). A call that passes this read and meets an empty bucket in start_call is refused
    there, with the same answer."""
    try:
        peer_limit.check(ctx.peer_id, transport=pipeline.TRANSPORT_WS)
    except peer_limit.PeerRateLimited as exc:
        await ctx.queue.push(envelope.error(envelope.CODE_SKILL_CALL_FAILED,
                                            f"{exc.code}: {exc.message}"))
        return
    if not ctx.call_slots.try_acquire():
        await ctx.queue.push(envelope.error(envelope.CODE_SKILL_CALL_BUSY, BUSY_CONNECTION))
        return
    slot = PIPELINE_SLOTS
    if not slot.acquire(blocking=False):
        ctx.call_slots.release()
        await ctx.queue.push(envelope.error(envelope.CODE_SKILL_CALL_BUSY, BUSY_SERVER))
        return
    dispatch = _Dispatch()
    try:
        task = ctx.spawn(_run_skill_call(parsed, ctx, slot, dispatch))
    except BaseException:
        slot.release()
        ctx.call_slots.release()
        raise
    task.add_done_callback(functools.partial(_if_never_began, dispatch, slot, ctx.call_slots))


def _if_never_began(dispatch: _Dispatch, slot, call_slots: CallSlots, _task) -> None:
    """The dispatch task's done-callback, on the loop. A task cancelled before its first step
    never ran its coroutine, so its try/finally never gave the two slots back: this does."""
    if not dispatch.began:
        slot.release()
        call_slots.release()


async def _run_skill_call(parsed: envelope.SkillCallFrame, ctx, slot,
                          dispatch: _Dispatch) -> None:
    """The spawned dispatch. Answers every outcome on the socket's
    queue; the socket slot is given back when it ends, the process slot when the pipeline
    thread does."""
    dispatch.began = True
    submitted = False
    try:
        try:
            target, skill_name, version_pin, call_id = _payload_fields(parsed.envelope.payload)
            future = _PIPELINE_EXECUTOR.submit(
                _start_in_thread, ctx.peer_id, ctx.connection_id, parsed.envelope,
                parsed.mode_override, target, skill_name, version_pin, call_id)
            submitted = True
            future.add_done_callback(lambda _f: slot.release())
            # Shielded: a disconnect cancels THIS task, not the pipeline. A call the thread
            # has begun is finished, and only what
            # follows here, the registration, is skipped.
            result, row = await asyncio.shield(asyncio.wrap_future(future))
        except _BadCall:
            await ctx.queue.push(envelope.error(envelope.CODE_SKILL_CALL_BAD, BAD_TARGET))
            return
        except RpcError as exc:
            api = wire.api_error_from_rpc(exc)
            if not isinstance(exc, RateLimited):   # peer_limit logs those, damped
                log.warning("skill_call from peer %s refused: %s %s", ctx.peer_id,
                            type(exc).__name__, exc.message)
            await ctx.queue.push(envelope.error(envelope.CODE_SKILL_CALL_FAILED,
                                                f"{api.code}: {api.message}"))
            return
        except Exception:  # noqa: BLE001 - answered, and logged in full
            log.exception("skill_call from peer %s failed", ctx.peer_id)
            await ctx.queue.push(envelope.error(envelope.CODE_SKILL_CALL_FAILED, INTERNAL))
            return
        await _after_start(result, row, ctx)
    finally:
        if not submitted:
            slot.release()
        ctx.call_slots.release()


def _payload_fields(payload: dict):
    """Read from the SIGNED payload: the target (a UUID, else
    SKILL_CALL_BAD), the skill name ("" when absent: step 3 then finds no skill), the version
    pin and the call_id (None when absent: step 1 draws one, and step 4b refuses it)."""
    raw = payload.get("target_peer_id")
    try:
        target = uuid.UUID(raw) if isinstance(raw, str) else None
    except ValueError:
        target = None
    if target is None:
        raise _BadCall()
    name = payload.get("skill_name")
    version = payload.get("skill_version")
    call_id = payload.get("call_id")
    return (target, name if isinstance(name, str) else "",
            version if isinstance(version, str) else None,
            call_id if isinstance(call_id, str) else None)


def _start_in_thread(peer_id, connection_id, env, mode_override, target, skill_name,
                     version_pin, call_id):
    """The one pipeline, on a fresh guarded connection of its own, transport `ws`. Returns the
    StartCallResult and, for a call terminal at return, its row: None when that read failed
    (the call stands; `_after_start` registers it and re-reads)."""
    with dbconn.serving_connection(
            application_name=APPLICATION_NAME_PREFIX + str(connection_id)) as conn:
        ctx = pipeline.CallsContext(
            conn=conn,
            envelope_nonce_enforce=nonce_ledger.enforce_from_settings(),
            memory_peer_id=routing.memory_peer_id_from_settings())
        result = pipeline.start_call(ctx, pipeline.StartCallInput(
            caller_peer_id=peer_id,          # the socket's AUTHENTICATED peer
            target_peer_id=target,
            skill_name=skill_name,
            envelope=env,
            transport=pipeline.TRANSPORT_WS,
            call_id=call_id,
            version_pin=version_pin,
            mode_override=mode_override,
            capability_token=None,
            args_attachments=[],
            webhook_url=None,
            webhook_secret_hash=None))
        row = None
        if result.status in terminal.TERMINAL_STATUSES:
            try:
                row = read_call(conn, result.call_id)
            except Exception:  # noqa: BLE001 - the call is made; the registration path reads
                log.warning("skill_call %s is %s but its row could not be read here; it is "
                            "registered and read again", result.call_id, result.status,
                            exc_info=True)
        return result, row


async def _after_start(result, row, ctx) -> None:
    """4.7's registration order (module docstring). A call terminal at return whose row did
    not read terminal here (the read failed) takes the registration path: registered, acked
    and re-read, and if that read fails too, the re-offer sweep's terminal re-check delivers
    it (041 S6 review F9)."""
    call_id = result.call_id
    if result.status in terminal.TERMINAL_STATUSES:
        frame = terminal_frame(row) if row is not None else None
        if frame is not None:
            await ctx.queue.push(envelope.skill_acked(call_id))
            await ctx.queue.push(frame)
            return
        log.warning("skill_call %s returned %s and its row did not read terminal here; "
                    "registered and read again", call_id, result.status)
    await ctx.registry.register(call_id, ctx.connection_id, ctx.queue, proven=True)
    await ctx.queue.push(envelope.skill_acked(call_id))
    if await _replay_chunks(ctx, call_id):
        return          # a terminal held during the replay followed its chunks (S7 review M1)
    try:
        row = await _read(ctx, call_id)
    except Exception:  # noqa: BLE001 - the registration stands; the bridge delivers
        log.warning("skill_call %s: the re-read after registering failed; the terminal "
                    "arm and the sweeps deliver it", call_id, exc_info=True)
        return
    frame = terminal_frame(row) if row is not None else None
    if frame is not None:
        await ctx.queue.push(frame)
        await ctx.registry.unregister(call_id, ctx.connection_id)


async def _read(ctx, call_id: str) -> dict | None:
    """`read_call` on the socket's own connection, off the event loop."""
    if ctx.db is None:
        raise RuntimeError("this socket has no connection for its frames")
    return await asyncio.to_thread(ctx.db.run, read_call, call_id=call_id)


async def _replay_chunks(ctx, call_id: str) -> bool:
    """The call's stored chunks to THIS socket, whose registration is proven (design 4.7): read
    on the socket's own connection, each pushed through the one chunk frame, and the
    registration's high-water mark advanced. A failed read is logged and counted, never
    raised: the registration stands, and GET /v1/calls/{id}/chunks has the rows.

    THE REPLAY IS BRACKETED (S7 review M1): `begin_replay` before the read, with no await
    between the registration's proof and it, and `end_replay` after the last push, so a
    terminal the bridge delivers meanwhile is held and pushed after the chunks. Returns True
    when that happened: the terminal is sent and the registration has ended."""
    registry = ctx.registry
    if not await registry.begin_replay(call_id, ctx.connection_id):
        registry.count("replay:no_registration")
        return False
    sent = 0
    try:
        if ctx.db is None:
            raise RuntimeError("this socket has no connection for its frames")
        rows = await asyncio.to_thread(ctx.db.run, read_chunks, call_id=call_id)
        for row in rows:
            frame = chunk_frame(call_id, row)
            if frame is None:
                registry.count("replay:unbuildable")
                continue
            await ctx.queue.push(frame)
            await registry.advance_chunk(call_id, ctx.connection_id, row["sequence"])
            sent += 1
    except Exception:  # noqa: BLE001 - the registration stands; the rows stay readable
        registry.count("replay:read_failed")
        log.warning("chunk replay for call %r to peer %s FAILED; the registration stands",
                    call_id, ctx.peer_id, exc_info=True)
    finally:
        ended = await registry.end_replay(call_id, ctx.connection_id)
    if sent:
        registry.count("replay:sent", sent)
    if ended:
        registry.count("replay:terminal_after")
    return ended


# ------------------------------------------------------------------------- skill_cancel

async def handle_skill_cancel(parsed: envelope.SkillCancelFrame, ctx) -> None:
    """Cancel through `cancel_by_caller` on the socket's own connection."""
    try:
        if ctx.db is None:
            raise RuntimeError("this socket has no connection for its frames")
        refusal = await asyncio.to_thread(
            ctx.db.run, cancel_by_caller, call_id=parsed.call_id, me=ctx.peer_id,
            bound_key=ctx.bound_key, reason=parsed.reason)
    except Exception:  # noqa: BLE001 - answered, and logged in full; the text names nothing
        log.exception("skill_cancel for call %r from peer %s failed", parsed.call_id,
                      ctx.peer_id)
        refusal = target_frames.Refusal(envelope.CODE_SKILL_CANCEL_FAILED,
                                        target_frames.INTERNAL_ERROR)
    if refusal is not None:
        await ctx.queue.push(refusal.frame())


def cancel_by_caller(conn, *, call_id: str, me: uuid.UUID, bound_key: bytes | None,
                     reason: str | None) -> target_frames.Refusal | None:
    """The caller's cancel: ONE guarded statement with its event (module docstring). None when
    it landed; else the refusal to send."""
    code = envelope.CODE_SKILL_CANCEL_FAILED
    params = {"call_id": call_id, "me": me,
              "bound_key": None if bound_key is None else P.b64u(bound_key),
              "in_flight": list(reads.IN_FLIGHT_STATUSES),
              "details": None if reason is None else Jsonb({"reason": reason})}
    mine = "c.caller_peer_id = %(me)s AND " + target_frames.peer_standing(bound_key)
    with rls.server_internal(conn):
        with conn.cursor() as cur:
            cur.execute(
                "WITH abandoned AS ("
                " UPDATE skill_calls AS c SET status = 'abandoned', completed_at = NOW()"
                "  WHERE c.call_id = %(call_id)s AND c.status = ANY(%(in_flight)s::text[])"
                "    AND " + mine + " RETURNING c.call_id) "
                "INSERT INTO skill_call_events (call_id, event_type, details, source, "
                "  source_peer_id) "
                "SELECT call_id, 'caller_disconnected', %(details)s, 'caller', %(me)s "
                "  FROM abandoned RETURNING call_id", params)
            if cur.fetchone() is not None:
                call_chunks.trim(cur, call_id)                       # 041 S7, design 5.2
                return None
    with rls.server_internal(conn):
        with conn.cursor() as cur:
            cur.execute("SELECT c.status FROM skill_calls c WHERE c.call_id = %(call_id)s AND "
                        + mine, params)
            row = cur.fetchone()
    if row is None:
        log.warning("skill_cancel refused for call %r from peer %s: %s", call_id, me,
                    _why_not_cancelled(conn, params, bound_key))
        return target_frames.Refusal(code, NOT_MADE_BY_THIS_PEER)
    return target_frames.Refusal(
        code, f"call not in flight (status: {wire.debug_status(row[0])})")


def _why_not_cancelled(conn, params: dict, bound_key: bytes | None) -> str:
    """For the SERVER LOG ONLY: which clause refused. The wire answer never depends on it."""
    try:
        with rls.server_internal(conn):
            with conn.cursor() as cur:
                cur.execute("SELECT (SELECT c.caller_peer_id FROM skill_calls c "
                            "         WHERE c.call_id = %(call_id)s), "
                            "       (SELECT count(*) FROM skill_calls c "
                            "         WHERE c.call_id = %(call_id)s), "
                            "       (SELECT p.status FROM peers p WHERE p.id = %(me)s)",
                            params)
                caller, exists, peer_status = cur.fetchone()
        me = params["me"]
        if not exists:
            return "no such call"
        if caller != me:
            return "the call was made by another peer"
        if peer_status != "active":
            return f"this peer is not active (status {peer_status})"
        if bound_key is not None:
            state = revocation.key_states(conn, [(me, bound_key)])[(me, bound_key)]
            if state != revocation.KEY_LIVE:
                return f"the key this socket's token is bound to is {state}"
        return "not explained by the call or the peer: the row moved between the statements"
    except Exception as exc:  # noqa: BLE001 - a diagnosis must never change the answer
        return f"not diagnosed ({type(exc).__name__})"


# --------------------------------------------------------------- subscribe, unsubscribe

async def subscribe_calls(ctx, channels: list[str]) -> None:
    """The `call:` channels of one subscribe frame, in order, each registered FIRST
    (provisional), then read, then proven or dropped (module docstring). A call named again in
    the same frame is not read again (`subscribe:duplicate`). At the cap, of this socket's
    registrations or of the calls this frame has read, the rest are dropped, as the presence
    cap drops them."""
    seen: set[str] = set()
    for channel in channels:
        if not channel.startswith(CALL_PREFIX):
            continue
        try:
            call_id = validate_call_id(channel[len(CALL_PREFIX):])
        except ValueError:
            ctx.registry.count("subscribe:not_a_call_id")
            continue
        if call_id in seen:
            ctx.registry.count("subscribe:duplicate")
            continue
        if (len(seen) >= MAX_CALL_SUBSCRIPTIONS_PER_CONN
                or await ctx.registry.count_for(ctx.connection_id)
                >= MAX_CALL_SUBSCRIPTIONS_PER_CONN):
            ctx.registry.count("subscribe:capped")
            break
        seen.add(call_id)
        await _subscribe_one(ctx, call_id)


async def _subscribe_one(ctx, call_id: str) -> None:
    registry = ctx.registry
    await registry.register(call_id, ctx.connection_id, ctx.queue, proven=False)
    try:
        row = await _read(ctx, call_id)
    except Exception:  # noqa: BLE001 - not registered, and loud in the log only
        await registry.drop_unproven(call_id, ctx.connection_id)
        registry.count("subscribe:lookup_failed")
        log.warning("subscribe: call %r lookup FAILED for peer %s, not registered; this is "
                    "not an absent call", call_id, ctx.peer_id, exc_info=True)
        return
    if row is None:
        await registry.drop_unproven(call_id, ctx.connection_id)
        registry.count("subscribe:unknown_call")
        log.info("subscribe: unknown call %r from peer %s, not registered", call_id,
                 ctx.peer_id)
        return
    caller = row["caller_peer_id"]
    if caller is None or uuid.UUID(str(caller)) != uuid.UUID(str(ctx.peer_id)):
        await registry.drop_unproven(call_id, ctx.connection_id)
        registry.count("subscribe:not_caller")
        log.warning("subscribe: REJECTED, peer %s is not the caller of call %r", ctx.peer_id,
                    call_id)
        return
    seen = await registry.prove(call_id, ctx.connection_id)
    registry.count("subscribe:registered")
    log.info("subscribe: caller %s registered for call %r", ctx.peer_id, call_id)
    if await _replay_chunks(ctx, call_id):
        registry.count("subscribe:terminal_pushed")   # held during the replay (S7 review M1)
        return
    frame = terminal_frame(row)
    if frame is None and seen:
        try:
            row = await _read(ctx, call_id)
        except Exception:  # noqa: BLE001 - the registration stands; the sweeps deliver
            log.warning("subscribe: call %r re-read failed; the sweeps deliver its terminal",
                        call_id, exc_info=True)
            return
        frame = terminal_frame(row) if row is not None else None
    if frame is not None:
        await ctx.queue.push(frame)
        await registry.unregister(call_id, ctx.connection_id)
        registry.count("subscribe:terminal_pushed")


async def unsubscribe_calls(ctx, channels: list[str]) -> None:
    """2.12.6: each `call:` channel removes this socket's registration for that call."""
    for channel in channels:
        if channel.startswith(CALL_PREFIX):
            await ctx.registry.unregister(channel[len(CALL_PREFIX):], ctx.connection_id)


__all__ = ["BUSY_CONNECTION", "BUSY_SERVER", "CallSlots", "PIPELINE_SLOTS",
           "cancel_by_caller", "chunk_frame", "handle_skill_call", "handle_skill_cancel",
           "read_call", "read_chunks", "subscribe_calls", "terminal_frame",
           "unsubscribe_calls"]
