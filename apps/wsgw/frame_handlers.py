"""Dispatch table for ClientFrame variants.

Implemented:
  ping          -> Pong + presence TTL refresh
  subscribe     -> presence channels, and `call:` ones
                   registered for the caller, + ack
  unsubscribe   -> drop presence and `call:` channels
                   + ack
  skill_call    -> SPAWNED start_call; skill_acked,
                   the terminal frame when it comes
  skill_cancel  -> the caller's call to abandoned
  skill_ack     -> a target_acked event, no reply
  skill_running -> routed to running, no reply
  skill_chunk   -> a chunk of a running call, no reply
  skill_result  -> running to the SIGNED terminal
  skill_error   -> running to failed, unsigned
  <unknown>     -> Error{UNKNOWN_FRAME}

THE TARGET FRAMES (divergence 041 S4, and skill_chunk since S7) are each one guarded
statement on the socket's own connection, with the active, live-key and memory-authority
clauses, and one refusal for an unknown call and a call that is not this peer's. The
statements are in apps/wsgw/target_frames.py. A refusal goes through the send queue and the
socket stays open.

THE CALLER'S FRAMES (divergence 041 S6): skill_call, skill_cancel and the `call:` half of
subscribe and unsubscribe, in apps/wsgw/caller_frames.py. skill_call is SPAWNED under the
socket's 16 slots and one process-wide cap, and answers SKILL_CALL_BUSY without blocking the
reader; skill_cancel is one guarded statement on the socket's own connection.

DELIBERATELY NOT IMPLEMENTED: skill_resume. This hub does not implement the continuation-token
engine, so it stays refused (design 2.10): it RAISES
`SkillFrameNotImplemented`. (skill_chunk did too, until divergence 041 S7.)

That is the point. A handler that accepted `skill_cancel` and did nothing would
look identical, from the client's side, to a working one - and the failure would
surface later as a call that ran on until the deadline janitor timed it out.
An ack that moved nothing and a call stuck in `Routed` are a common footgun for
first-cut clients.
A raise cannot be mistaken for success.

subscribe ALSO registers `call:<call_id>` channels with the caller bridge after an ownership
check and replays persisted chunks. Since 041 S6 the registration is here (register first,
then prove the caller, apps/wsgw/caller_frames.py), and
since S7 the replay: from sequence 0, at most the newest 200, after the caller is proven.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from apps.calls import route
from apps.core import peer_limit

from . import caller_frames, envelope, target_frames
from .call_registry import CallRegistry
from .connection_manager import ConnectionManager
from .presence import PresenceBackend
from .send_queue import SendQueue

log = logging.getLogger(__name__)

#: The subscribe snapshot (divergence 106) never pushes into the last SNAPSHOT_HEADROOM slots of the socket's send
#: queue: it waits for the writer to drain below that, so it cannot evict its own ack, a frame queued before it, or
#: one of its own onlines. The same rule as call_notify.OFFER_LIMIT: one pass must not evict its own frames.
SNAPSHOT_HEADROOM = 64
#: How often a waiting snapshot looks at the queue again, and how long one wait may last before the snapshot stops.
#: A reader that has not drained for SNAPSHOT_STALL_S learns the rest by the peers' next changes, as before 106.
SNAPSHOT_POLL_S = 0.01
SNAPSHOT_STALL_S = 5.0
_sleep = asyncio.sleep           # seams for the tests
_clock = time.monotonic


class SkillFrameNotImplemented(NotImplementedError):
    """Raised for a frame type with no handler.

    Carries the frame tag so the caller can name it in the error it returns.
    """

    def __init__(self, frame_type: str, source_label: str) -> None:
        super().__init__(
            f"frame `{frame_type}` is not implemented by this hub because its required "
            "call-processing dependency is not available. Refused rather than acked so a "
            "client cannot mistake it for success."
        )
        self.frame_type = frame_type
        self.source_label = source_label


#: Frame tag -> label named in the UNIMPLEMENTED message.
_SKILL_FRAME_SOURCES = {
    envelope.SKILL_RESUME: "skill_resume",
}


@dataclass
class FrameContext:
    """Everything dispatch takes, minus fields its handlers do not read."""

    manager: ConnectionManager
    presence: PresenceBackend
    peer_id: uuid.UUID
    connection_id: uuid.UUID
    queue: SendQueue
    #: The raw key the socket's token is bound to (`ConnectionHandle.bound_key`), or None for
    #: a bearer token. The target frames' statements require it live (divergence 041 S4).
    bound_key: bytes | None = None
    #: The socket's own connection for its target frames, opened at the first one and closed
    #: by the consumer's disconnect (target_frames.SocketConnection). None: no database, and
    #: a well-formed target frame is answered with its code and "internal error".
    db: target_frames.SocketConnection | None = None
    #: The registry the socket's call registrations go in (divergence 041 S6): the one its
    #: worker's CallBridge delivers from. A context with none gets a private one, which no
    #: bridge delivers from.
    registry: CallRegistry = field(default_factory=CallRegistry)
    #: The socket's MAX_INFLIGHT_SKILL_CALLS (041 S6), the consumer's for the socket's life.
    call_slots: caller_frames.CallSlots = field(default_factory=caller_frames.CallSlots)
    #: Starts a skill_call's spawned dispatch; the consumer's tracks it and cancels it at
    #: disconnect.
    spawn: Callable = asyncio.ensure_future


async def handle_ping(ctx: FrameContext) -> None:
    """Pong first, then the TTL refresh. A failed refresh is logged and NOT
    fatal, so it cannot swallow the Pong."""
    await ctx.queue.push(envelope.pong())
    await ctx.presence.refresh(ctx.peer_id, ctx.connection_id)


async def handle_subscribe(ctx: FrameContext, channels: list[str]) -> None:
    """The ack echoes the channels the client SENT, not the ones that took effect.
    That is why
    `ConnectionManager.subscribe_presence` returning a count is not used here.
    The `call:` channels are registered for the call's caller only, and a finished
    call's terminal frame comes before the ack (divergence 041 S6, caller_frames).

    After the ack, the snapshot (divergence 106): `peer_online` to THIS socket for each
    subscribed peer the presence backend shows online, so a watcher that subscribes late is
    not left waiting for the peer's next change. A frame that crosses it (a held arrival firing
    in between) can make that peer's online arrive twice, which 020 already allows.
    """
    await ctx.manager.subscribe_presence(ctx.connection_id, channels)
    await caller_frames.subscribe_calls(ctx, channels)
    await ctx.queue.push(envelope.subscribed(channels))
    await _presence_snapshot(ctx, channels)


async def _presence_snapshot(ctx: FrameContext, channels: list[str]) -> None:
    """Paced by the send queue's room (SNAPSHOT_HEADROOM). Each peer's `is_shown` check and its push are one step
    with no await between them, so a departure that fires while the snapshot waits is either sent before the check
    (and the check then sees the peer gone) or after the push (and the offline lands after the online)."""
    room = ctx.queue.capacity - SNAPSHOT_HEADROOM
    for peer_id in await ctx.manager.presence_subscribed(ctx.connection_id, channels):
        waited_from = None
        while len(ctx.queue) >= room:
            now = _clock()
            waited_from = now if waited_from is None else waited_from
            if now - waited_from >= SNAPSHOT_STALL_S:
                log.warning("presence snapshot stopped: the send queue did not drain for %.0f s", SNAPSHOT_STALL_S)
                return
            await _sleep(SNAPSHOT_POLL_S)
        if ctx.presence.is_shown(peer_id):
            ctx.queue.push_nowait(envelope.peer_online(peer_id))


async def handle_unsubscribe(ctx: FrameContext, channels: list[str]) -> None:
    """A `call:` channel's registration is removed (divergence 041 S6, design 2.12.6)."""
    await ctx.manager.unsubscribe_presence(ctx.connection_id, channels)
    await caller_frames.unsubscribe_calls(ctx, channels)
    await ctx.queue.push(envelope.unsubscribed(channels))


async def _caller_frame(frame: envelope.ClientFrame, ctx: FrameContext, handler) -> None:
    """One caller frame: parse it (a ValueError goes up to the consumer, which answers PARSE;
    a value out of its domain is its `_BAD` frame), then hand it to its handler."""
    try:
        parsed = envelope.parse_skill_frame(frame)
    except envelope.SkillFrameBad as bad:
        await ctx.queue.push(bad.frame())
        return
    await handler(parsed, ctx)


async def _target_frame(frame: envelope.ClientFrame, ctx: FrameContext, code: str,
                        statement, **fields) -> None:
    """One target frame: parse it (a ValueError goes up to the consumer, which answers PARSE;
    a value out of its domain is its `_BAD` frame), run its one guarded statement on the
    socket's own connection off the event loop, and queue the refusal if there is one. A
    frame that landed gets no reply.

    B12 (apps/core/peer_limit, review a65ab61f MEDIUM 1): a frame that LANDS is free, since it
    answers a call charged to its caller and a stream must not be cut. A frame that is REFUSED
    (bad, not this peer's call, a failed transition) takes a FRAMES token, since it cost a
    statement, a diagnosis and a log line; and a frame that arrives with FRAMES empty is
    answered RATE_LIMITED before any of that. A server fault (INTERNAL_ERROR) is not the
    peer's doing and is not charged."""
    try:
        peer_limit.check(ctx.peer_id, transport="ws", bucket=peer_limit.FRAMES)
    except peer_limit.PeerRateLimited as exc:
        await ctx.queue.push(envelope.error(peer_limit.WS_RATE_LIMITED, exc.message))
        return
    try:
        parsed = envelope.parse_skill_frame(frame)
    except envelope.SkillFrameBad as bad:
        peer_limit.charge(ctx.peer_id, transport="ws", bucket=peer_limit.FRAMES)
        await ctx.queue.push(bad.frame())
        return
    faulted = False
    try:
        if ctx.db is None:
            raise RuntimeError("this socket has no connection for target frames")
        kwargs = {"call_id": parsed.call_id, "me": ctx.peer_id, "bound_key": ctx.bound_key,
                  "authority": route.memory_peer_id_from_settings()}
        kwargs.update({name: getattr(parsed, attr) for name, attr in fields.items()})
        refusal = await asyncio.to_thread(ctx.db.run, statement, **kwargs)
    except Exception:  # noqa: BLE001 - answered, and logged in full; the text names nothing
        log.exception("%s for call %r from peer %s failed", frame.type, parsed.call_id,
                      ctx.peer_id)
        refusal = target_frames.Refusal(code, target_frames.INTERNAL_ERROR)
        faulted = True
    if refusal is not None:
        if not faulted:
            peer_limit.charge(ctx.peer_id, transport="ws", bucket=peer_limit.FRAMES)
        await ctx.queue.push(refusal.frame())


async def handle_skill_ack(frame: envelope.ClientFrame, ctx: FrameContext) -> None:
    """Record that the target acknowledged its routed call."""
    await _target_frame(frame, ctx, envelope.CODE_SKILL_ACK_FAILED, target_frames.ack)


async def handle_skill_running(frame: envelope.ClientFrame, ctx: FrameContext) -> None:
    """Move this peer's routed call to running."""
    await _target_frame(frame, ctx, envelope.CODE_SKILL_RUNNING_FAILED, target_frames.running)


async def handle_skill_chunk(frame: envelope.ClientFrame, ctx: FrameContext) -> None:
    """Store a chunk for this peer's running call; its pointer reaches the
    caller (divergence 041 S7)."""
    await _target_frame(frame, ctx, envelope.CODE_SKILL_CHUNK_FAILED, target_frames.chunk,
                        env="envelope", sequence="sequence", chunk_type="chunk_type")


async def handle_skill_result(frame: envelope.ClientFrame, ctx: FrameContext) -> None:
    """Move a running call to its signed terminal."""
    await _target_frame(frame, ctx, envelope.CODE_SKILL_RESULT_FAILED, target_frames.result,
                        env="envelope")


async def handle_skill_error(frame: envelope.ClientFrame, ctx: FrameContext) -> None:
    """Move a running call to failed with an unsigned error."""
    await _target_frame(frame, ctx, envelope.CODE_SKILL_ERROR_FAILED, target_frames.error,
                        error_code="error_code", error_message="error_message")


_TARGET_FRAME_HANDLERS = {
    envelope.SKILL_ACK: handle_skill_ack,
    envelope.SKILL_RUNNING: handle_skill_running,
    envelope.SKILL_CHUNK: handle_skill_chunk,
    envelope.SKILL_RESULT: handle_skill_result,
    envelope.SKILL_ERROR: handle_skill_error,
}


async def dispatch(frame: envelope.ClientFrame, ctx: FrameContext) -> None:
    """Dispatch a client frame.

    Raises SkillFrameNotImplemented for the skill frame that has no handler (skill_resume);
    the consumer turns that into an Error frame plus a close, never into an ack.
    """
    if frame.type == envelope.PING:
        await handle_ping(ctx)
    elif frame.type == envelope.SUBSCRIBE:
        await handle_subscribe(ctx, frame.channels())
    elif frame.type == envelope.UNSUBSCRIBE:
        await handle_unsubscribe(ctx, frame.channels())
    elif frame.type in _TARGET_FRAME_HANDLERS:
        await _TARGET_FRAME_HANDLERS[frame.type](frame, ctx)
    elif frame.type == envelope.SKILL_CALL:
        await _caller_frame(frame, ctx, caller_frames.handle_skill_call)
    elif frame.type == envelope.SKILL_CANCEL:
        await _caller_frame(frame, ctx, caller_frames.handle_skill_cancel)
    elif frame.type in _SKILL_FRAME_SOURCES:
        raise SkillFrameNotImplemented(frame.type, _SKILL_FRAME_SOURCES[frame.type])
    else:
        # Unknown frame tag.
        await ctx.queue.push(
            envelope.error(envelope.CODE_UNKNOWN_FRAME, "frame type not recognized")
        )
