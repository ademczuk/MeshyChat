"""Per-socket bounded send queue with DROP-OLDEST backpressure.

The policy is the whole point and is the opposite of `asyncio.Queue`'s (which blocks the producer) and of
`asyncio.Queue(maxsize=n).put_nowait` (which raises): when full, push discards
the HEAD and appends, so a slow consumer loses OLD frames and keeps NEW ones.

A stale `peer.online` notification is less useful than a fresh `skill.invoke`.

LOSS IS NOW OBSERVABLE. Drop-oldest is the right policy and it was silent: `dropped` was incremented and, measured 2026-09-21, read by
nothing outside tests/test_ws.py. So a slow socket lost frames and neither end found
out, which is docs/V1-DESIGN-RULES.md E1 ("a failure and a pass looked identical").

Borrowed from herdr (herdrdev/herdr, Apache-2.0), whose `EventHub` pairs a bounded ring
with a MONOTONIC SEQUENCE, and whose socket API specifies that a subscriber which falls
behind the retained history is told `events_lost` and disconnected, so it re-subscribes
and reconciles against an authoritative snapshot rather than silently continuing with a
hole. Their own note is worth keeping: "Snapshots and events have no shared sequence
boundary... do not unconditionally replay buffered event payloads onto the snapshot."

What is taken here is the SEQUENCE AND THE LOSS RECORD, not the disconnect. A sequence
makes the gap nameable ("you are missing 5..9") instead of merely countable, and it is
internal, so the wire format is untouched. Telling the CLIENT, which is the half that
would actually let it resync, is a wire change and is proposed in docs/DIVERGENCES.md
rather than taken unilaterally here.

LOST TERMINALS ARE NAMED, AND RE-PUSHED (F04, operator decision 60, 2026-09-27; ASDLC OI-30;
docs/DIVERGENCES.md 048). The drop stays silent on the wire. But a TERMINAL frame (skill_result,
skill_error, skill_timeout: the one terminal mapping's three types, caller_frames.terminal_frame)
is the one frame a caller cannot do without, and its registration ended when it was queued, so
nothing else would ever send it again. So the loss record also names the CALL of every terminal
frame an overflow evicted (`FrameLoss.terminal_call_ids`), and the writer hands those names to the
CallBridge's loss pass, which re-reads each row and pushes its terminal again
(apps/wsgw/call_notify.py). A terminal is named only when no copy of it is still queued and no
copy was taken by the writer (`has_terminal`), because a copy can be queued twice (the window in
caller_frames' registration order) and a caller must not be told twice. A non-terminal frame is
counted and never named: it stays dropped. Both memories are bounded by
TERMINAL_MEMORY (rule B8); a lost terminal past it is counted as unnamed, and its caller recovers
by `GET /v1/calls/{id}` or a subscribe, as before F04.

The deque operations here are already atomic with respect to the event loop (no await between
the length check and the append), so no lock is needed. Adding one would only introduce a
suspension point between "is it full" and "append" - i.e. it would create the race it looked
like it was preventing.
"""
from __future__ import annotations

import asyncio
import collections
import dataclasses
from typing import Any

from .constants import SEND_QUEUE_CAPACITY

#: The frame types that END a call on its caller's socket (F04): the three the one terminal
#: mapping builds (apps/wsgw/caller_frames.terminal_frame, through envelope.skill_result,
#: skill_error and skill_timeout). The hub
#: builds these types nowhere else: `error` is a different type, and a skill_result or
#: skill_error a TARGET sends is read by the hub, never queued to a socket.
TERMINAL_FRAME_TYPES = frozenset({"skill_result", "skill_error", "skill_timeout"})

#: How many call ids one queue remembers as LOST (a terminal evicted with no copy queued or
#: taken) and as TAKEN (a terminal the writer took), each (rule B8). The queue's capacity: one
#: burst between two of the writer's takes cannot name more terminals than it evicted, and a
#: socket's own calls are fewer (MAX_INFLIGHT_SKILL_CALLS, plus its subscriptions).
TERMINAL_MEMORY = SEND_QUEUE_CAPACITY


def terminal_call_id(frame: Any) -> str | None:
    """The call a terminal frame ends, or None for any other frame (a non-dict included)."""
    if isinstance(frame, dict) and frame.get("type") in TERMINAL_FRAME_TYPES:
        call_id = frame.get("call_id")
        if isinstance(call_id, str):
            return call_id
    return None


@dataclasses.dataclass(frozen=True)
class FrameLoss:
    """Frames this socket discarded, as a value the caller must handle.

    A COUNT alone cannot answer the only question a client actually has, which is
    "what did I miss". A sequence range can, because the client knows which
    sequence numbers it received.

    Typed rather than a tuple so `take_loss()` returning None ("nothing was lost")
    is distinguishable from a zero-valued record, which is B3 applied here.
    """

    count: int
    first_seq: int  #: sequence of the OLDEST frame lost in this burst
    last_seq: int  #: sequence of the NEWEST frame lost in this burst
    #: F04: the calls whose TERMINAL frame is among the lost, oldest first, each once, and only
    #: those no copy of which is still queued or was taken by the writer. At most
    #: TERMINAL_MEMORY. Every other lost frame is in `count` only: it stays dropped.
    terminal_call_ids: tuple[str, ...] = ()
    #: Lost terminals past TERMINAL_MEMORY, counted and not named.
    terminals_unnamed: int = 0


class SendQueue:
    """A bounded FIFO of server frames with drop-oldest overflow."""

    def __init__(self, capacity: int = SEND_QUEUE_CAPACITY) -> None:
        # A queue with no capacity is invalid.
        if capacity <= 0:
            raise ValueError("SendQueue capacity must be > 0")
        self._capacity = capacity
        #: (sequence, frame) pairs. The sequence is internal: `pop`, `pop_nowait`
        #: and `snapshot` all still hand back bare frames, so nothing outside this
        #: module sees the pairing and the wire format is unchanged.
        self._deque: collections.deque[tuple[int, Any]] = collections.deque()
        self._notify = asyncio.Event()
        #: Count of frames discarded by the drop-oldest policy. Without it a
        #: drop is invisible, and a test that only checks length cannot tell a
        #: drop from a pop.
        self.dropped = 0
        #: Monotonic, assigned at push. Never reset, never reused.
        self._next_seq = 0
        #: Loss not yet reported to anyone. Consumed by `take_loss()`.
        self._pending_loss: FrameLoss | None = None
        #: F04. Terminal frames now in the deque, by call (a call can have two copies queued).
        #: Never more keys than the deque has frames.
        self._terminals_queued: collections.Counter[str] = collections.Counter()
        #: Calls whose terminal the writer took (`pop`), oldest first, at most TERMINAL_MEMORY.
        self._terminals_taken: collections.OrderedDict[str, None] = collections.OrderedDict()
        #: Calls whose terminal was evicted with no copy queued or taken, not yet reported:
        #: consumed with the loss by `take_loss()`. At most TERMINAL_MEMORY.
        self._terminals_lost: dict[str, None] = {}
        self._terminals_unnamed = 0

    @property
    def capacity(self) -> int:
        return self._capacity

    def push_nowait(self, frame: Any) -> None:
        """Enqueue, evicting the OLDEST frame first when at capacity.

        Synchronous because nothing here can block; `push` is the async spelling
        that calls `push_nowait`.
        """
        if len(self._deque) >= self._capacity:
            lost_seq, lost_frame = self._deque.popleft()
            self.dropped += 1
            lost_call = terminal_call_id(lost_frame)
            if lost_call is not None:
                self._unqueue_terminal(lost_call)
                self._note_lost_terminal(lost_call)
            # Accumulate into one burst rather than one record per frame: an
            # overflowing socket drops continuously, and a notice per frame would
            # be the same silent-loss problem wearing a log line.
            if self._pending_loss is None:
                self._pending_loss = FrameLoss(1, lost_seq, lost_seq)
            else:
                self._pending_loss = FrameLoss(
                    self._pending_loss.count + 1, self._pending_loss.first_seq, lost_seq
                )
        call_id = terminal_call_id(frame)
        if call_id is not None:
            self._terminals_queued[call_id] += 1
        self._deque.append((self._next_seq, frame))
        self._next_seq += 1
        self._notify.set()

    def take_loss(self) -> FrameLoss | None:
        """Report and CLEAR the accumulated loss. None when nothing was lost.

        Consume-once on purpose. A caller that reports the same burst on every
        pass would make a single overflow look like continuing loss, and a caller
        that never clears it would report the first burst forever. The lost
        terminals' names (F04) are consumed with it.
        """
        loss, self._pending_loss = self._pending_loss, None
        if loss is not None and (self._terminals_lost or self._terminals_unnamed):
            loss = dataclasses.replace(loss, terminal_call_ids=tuple(self._terminals_lost),
                                       terminals_unnamed=self._terminals_unnamed)
        self._terminals_lost = {}
        self._terminals_unnamed = 0
        return loss

    def has_terminal(self, call_id: str) -> bool:
        """Whether a terminal frame for the call is queued here now, or was taken by the writer
        (within the last TERMINAL_MEMORY taken). The re-push asks this on the event loop, in
        the turn it would push, so a caller that already has its terminal is not sent another
        (F04)."""
        return call_id in self._terminals_queued or call_id in self._terminals_taken

    def _unqueue_terminal(self, call_id: str) -> None:
        left = self._terminals_queued[call_id] - 1
        if left > 0:
            self._terminals_queued[call_id] = left
        else:
            self._terminals_queued.pop(call_id, None)

    def _note_lost_terminal(self, call_id: str) -> None:
        """An evicted terminal is LOST only if no copy of it is still queued or was taken."""
        if self.has_terminal(call_id) or call_id in self._terminals_lost:
            return
        if len(self._terminals_lost) >= TERMINAL_MEMORY:
            self._terminals_unnamed += 1
            return
        self._terminals_lost[call_id] = None

    def _taken(self, frame: Any) -> None:
        """The writer takes `frame`. A terminal is remembered as taken, and a copy that was
        named lost is no longer lost: this copy reaches the socket."""
        call_id = terminal_call_id(frame)
        if call_id is None:
            return
        self._unqueue_terminal(call_id)
        self._terminals_lost.pop(call_id, None)
        self._terminals_taken[call_id] = None
        self._terminals_taken.move_to_end(call_id)
        if len(self._terminals_taken) > TERMINAL_MEMORY:
            self._terminals_taken.popitem(last=False)

    @property
    def next_seq(self) -> int:
        """The sequence the NEXT pushed frame will carry. Test/observability only."""
        return self._next_seq

    async def push(self, frame: Any) -> None:
        self.push_nowait(frame)

    async def pop(self) -> Any:
        """Await the next frame."""
        while True:
            if self._deque:
                _, frame = self._deque.popleft()
                if not self._deque:
                    self._notify.clear()
                self._taken(frame)
                return frame
            # Empty: re-arm and park. Nothing can interleave between the clear
            # and the wait (no await in between), so a push cannot be lost here.
            self._notify.clear()
            await self._notify.wait()

    def pop_nowait(self) -> Any:
        """Non-blocking pop; raises IndexError when empty. Test/drain helper."""
        _, frame = self._deque.popleft()
        if not self._deque:
            self._notify.clear()
        self._taken(frame)
        return frame

    def __len__(self) -> int:
        """Snapshot length."""
        return len(self._deque)

    def snapshot(self) -> list:
        """A copy of the queue contents, oldest first. Test helper: asserting
        WHICH frames survived an overflow is the only assertion that can tell
        drop-oldest apart from drop-newest - both leave the same length."""
        return [frame for _, frame in self._deque]

    def snapshot_sequenced(self) -> list[tuple[int, Any]]:
        """`snapshot()` with each frame's sequence. Proves a gap is contiguous."""
        return list(self._deque)
