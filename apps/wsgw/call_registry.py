"""Which sockets hear a call's caller-side frames: the CallRegistry (divergence 041 S6).

It maps a call_id to its registered sockets, is idempotent per (call, connection), and clears
registrations on the call's terminal frame and on socket teardown. Design 4.6, 4.7 and 5.1.

TWO KINDS OF REGISTRATION:

  * PROVEN: the socket's peer is the call's caller. A `skill_call` registers its own socket
    proven (the pipeline has just verified the caller's signature as the socket's peer), and a
    `subscribe call:<id>` becomes proven once its read of the row finds the socket's peer in
    `caller_peer_id`. Terminal frames and (since S7) chunk frames go to proven registrations
    only (5.1: "sockets that proved caller ownership").
  * PROVISIONAL: a `subscribe` registers first and reads the row after (design 4.7), so a
    terminal published between the two is not lost. Delivering before that read would let a
    stranger's socket hear another caller's result for as long as
    its ownership read took. Until the
    read proves it, the registration receives NOTHING; a terminal that arrives meanwhile is
    remembered on it (`terminal_seen`), and `prove` reports that once, so the subscribe handler
    re-reads and pushes the terminal itself. A registration that the read does not prove is
    dropped.

Every method runs on the event loop that owns the sockets, as ConnectionManager's do; the
CallBridge's thread reaches it through `run_coroutine_threadsafe`. None of them awaits anything
but a SendQueue push, and `deliver_terminal` works on a snapshot, so a registration changing
under a push is not iterated.

`chunk_high_water` is each registration's highest delivered chunk sequence, -1 before any
(design 4.6). Since S7 a live chunk (`deliver_chunk`) and a subscriber's replay advance it, and
the recovery sweep's part b sends each registration only the chunks past it
(`chunk_floors`, `deliver_chunks_after`). A live chunk is delivered WHATEVER the mark: 4.9 is
at-least-once, and a chunk that arrives below the mark (a target that sends out of order, or
a replay racing a live frame) is sent, not dropped; the client orders and dedupes on sequence.

A TERMINAL DURING A REPLAY IS HELD (the S7 review's M1). A replay (`begin_replay` to
`end_replay`) awaits a database read with its registration already proven, so the bridge's
terminal arm could write the terminal to it FIRST and the replayed chunks after it, where 4.7
has the terminal follow the chunks and 4.9 tells a client to treat the first terminal as final.
While a replay runs, `deliver_terminal` keeps that frame on the registration instead (the first
one, since the first terminal is final) and keeps the registration; the last `end_replay` pushes
it after the replay's chunks and ends the registration. Live chunks still go out during a
replay (4.9: the client orders on sequence).
"""
from __future__ import annotations

import collections
import dataclasses
import threading
import uuid

from .send_queue import SendQueue


@dataclasses.dataclass
class Registration:
    """One socket's registration for one call."""

    connection_id: uuid.UUID
    queue: SendQueue
    #: The socket's peer is the call's caller. False: registered by a subscribe whose read has
    #: not decided yet; it receives no frame.
    proven: bool
    #: A terminal was delivered for the call while this registration was provisional.
    terminal_seen: bool = False
    #: The highest chunk sequence delivered to this registration; -1 before any (S7). Only
    #: ever moves forward.
    chunk_high_water: int = -1
    #: Replays of stored chunks running to this registration (S7 review M1).
    replays: int = 0
    #: A terminal frame delivered while a replay ran, pushed when the last one ends.
    held_terminal: dict | None = None


class CallRegistry:
    """call_id -> {connection_id: Registration}, and connection_id -> its call_ids."""

    def __init__(self) -> None:
        self._by_call: dict[str, dict[uuid.UUID, Registration]] = {}
        self._by_connection: dict[uuid.UUID, set[str]] = {}
        self._stats: collections.Counter = collections.Counter()
        self._stats_lock = threading.Lock()

    # --- counters (read from any thread) -------------------------------------------------

    def stats(self) -> dict:
        with self._stats_lock:
            return dict(self._stats)

    def count(self, name: str, n: int = 1) -> None:
        with self._stats_lock:
            self._stats[name] += n

    # --- registration --------------------------------------------------------------------

    async def register(self, call_id: str, connection_id: uuid.UUID, queue: SendQueue, *,
                       proven: bool) -> bool:
        """Register the socket for the call. True when a registration was made; False when it
        already had one. A proven registration upgrades it and a provisional one never
        downgrades it."""
        regs = self._by_call.setdefault(call_id, {})
        existing = regs.get(connection_id)
        if existing is not None:
            if proven:
                existing.proven = True
            return False
        regs[connection_id] = Registration(connection_id, queue, proven)
        self._by_connection.setdefault(connection_id, set()).add(call_id)
        return True

    async def prove(self, call_id: str, connection_id: uuid.UUID) -> bool | None:
        """Mark the registration proven. Returns whether a terminal arrived while it was not
        (and clears that), or None when the socket holds no registration for the call."""
        reg = self._by_call.get(call_id, {}).get(connection_id)
        if reg is None:
            return None
        reg.proven = True
        seen, reg.terminal_seen = reg.terminal_seen, False
        return seen

    async def drop_unproven(self, call_id: str, connection_id: uuid.UUID) -> bool:
        """Remove the registration if it is still provisional. A proven one is never removed
        here: it was proven by something other than the read that just failed."""
        reg = self._by_call.get(call_id, {}).get(connection_id)
        if reg is None or reg.proven:
            return False
        return self._remove(call_id, connection_id)

    async def unregister(self, call_id: str, connection_id: uuid.UUID) -> bool:
        """Remove this socket's registration for the call (a terminal pushed to it directly,
        or an `unsubscribe`, design 2.12.6)."""
        return self._remove(call_id, connection_id)

    async def unregister_connection(self, connection_id: uuid.UUID) -> int:
        """Socket teardown: every registration of the socket goes."""
        calls = self._by_connection.pop(connection_id, set())
        for call_id in calls:
            regs = self._by_call.get(call_id)
            if regs is not None:
                regs.pop(connection_id, None)
                if not regs:
                    del self._by_call[call_id]
        return len(calls)

    def _remove(self, call_id: str, connection_id: uuid.UUID) -> bool:
        regs = self._by_call.get(call_id)
        if regs is None or regs.pop(connection_id, None) is None:
            return False
        if not regs:
            del self._by_call[call_id]
        calls = self._by_connection.get(connection_id)
        if calls is not None:
            calls.discard(call_id)
            if not calls:
                del self._by_connection[connection_id]
        return True

    # --- reads -----------------------------------------------------------------------------

    async def is_registered(self, call_id: str) -> bool:
        return bool(self._by_call.get(call_id))

    async def call_ids(self) -> list[str]:
        return list(self._by_call)

    async def count_for(self, connection_id: uuid.UUID) -> int:
        return len(self._by_connection.get(connection_id, ()))

    async def chunk_high_water(self, call_id: str, connection_id: uuid.UUID) -> int | None:
        reg = self._by_call.get(call_id, {}).get(connection_id)
        return None if reg is None else reg.chunk_high_water

    async def advance_chunk(self, call_id: str, connection_id: uuid.UUID,
                            sequence: int) -> bool:
        """Record that `sequence` reached this registration; only ever forward."""
        reg = self._by_call.get(call_id, {}).get(connection_id)
        if reg is None or sequence <= reg.chunk_high_water:
            return False
        reg.chunk_high_water = sequence
        return True

    # --- delivery --------------------------------------------------------------------------

    async def deliver_terminal(self, call_id: str, frame: dict) -> int:
        """The terminal frame to every PROVEN registration of the call, each then removed. A
        provisional registration is kept and marked `terminal_seen`. Returns the sockets
        written to."""
        sent = 0
        for connection_id, reg in list(self._by_call.get(call_id, {}).items()):
            if not reg.proven:
                reg.terminal_seen = True
                continue
            if reg.replays:
                # S7 review M1: after the replay's chunks, not before them (module docstring).
                if reg.held_terminal is None:
                    reg.held_terminal = frame
                self.count("replay:terminal_held")
                continue
            await reg.queue.push(frame)
            self._remove(call_id, connection_id)
            sent += 1
        return sent

    async def begin_replay(self, call_id: str, connection_id: uuid.UUID) -> bool:
        """A replay of stored chunks to this PROVEN registration starts: until the matching
        `end_replay`, a terminal for the call is held on it (S7 review M1). False, and nothing
        begun, when the socket holds no proven registration for the call."""
        reg = self._by_call.get(call_id, {}).get(connection_id)
        if reg is None or not reg.proven:
            return False
        reg.replays += 1
        return True

    async def end_replay(self, call_id: str, connection_id: uuid.UUID) -> bool:
        """A replay ends. When it was the last one and a terminal was held meanwhile, push that
        terminal (after whatever the replay pushed) and end the registration, as
        `deliver_terminal` would have; returns True then, else False."""
        reg = self._by_call.get(call_id, {}).get(connection_id)
        if reg is None or reg.replays == 0:
            return False
        reg.replays -= 1
        if reg.replays or reg.held_terminal is None:
            return False
        await reg.queue.push(reg.held_terminal)
        self._remove(call_id, connection_id)
        return True

    async def deliver_chunk(self, call_id: str, sequence: int, frame: dict) -> int:
        """A live chunk frame (the CallBridge's chunk arm, design 4.6) to every PROVEN
        registration of the call, each one's mark advanced (forward only); the registration
        stays. A provisional one gets nothing: its subscribe reads the chunks once it is
        proven (apps/wsgw/caller_frames.py). Returns the sockets written to."""
        sent = 0
        for reg in list(self._by_call.get(call_id, {}).values()):
            if not reg.proven:
                continue
            await reg.queue.push(frame)
            if sequence > reg.chunk_high_water:
                reg.chunk_high_water = sequence
            sent += 1
        return sent

    async def chunk_floors(self) -> dict[str, int]:
        """Each call with a PROVEN registration, and the lowest high-water mark among them: the
        recovery sweep reads each call's chunks past it, once (design 4.6, part b)."""
        floors: dict[str, int] = {}
        for call_id, regs in self._by_call.items():
            marks = [reg.chunk_high_water for reg in regs.values() if reg.proven]
            if marks:
                floors[call_id] = min(marks)
        return floors

    async def deliver_chunks_after(self, call_id: str, items: list) -> int:
        """The recovery sweep's replay: `items` are (sequence, frame), oldest first; each PROVEN
        registration of the call gets the ones past ITS mark, in order, and its mark advances.
        Returns the frames written."""
        sent = 0
        for reg in list(self._by_call.get(call_id, {}).values()):
            if not reg.proven:
                continue
            for sequence, frame in items:
                if sequence > reg.chunk_high_water:
                    await reg.queue.push(frame)
                    reg.chunk_high_water = sequence
                    sent += 1
        return sent


#: The hub's process-wide registry, beside connection_manager.MANAGER: the production
#: CallBridge delivers from it and the default consumer registers into it.
CALL_REGISTRY = CallRegistry()
