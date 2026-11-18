"""Socket presence is debounced (divergence 105, `apps/wsgw/presence.py`).

`LocalPresenceBackend` holds `peer_online` until a peer has kept a socket for ARRIVAL_SECS and
`peer_offline` until it has had none for DEPARTURE_GRACE_SECS. A change undone inside its window
sends nothing.

NO WALL CLOCK. The backend's `sleep` is a seam: `Clock.sleep` records the delay it was asked
for and waits until the test releases it, so "inside the window" and "after the window" are
the test's choice, not a race with a loaded machine. No database: the registry is a fake that
answers `is_online` from a counter of sockets and records what was published. Each test is one
`asyncio.run`, so no event loop plugin is involved.

Every "nothing was sent" has its positive control: the arrival and departure tests show the
same steps without the undo DO send the frame.
"""
from __future__ import annotations

import asyncio
import logging

import pytest
import sys
import uuid
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from apps.wsgw import envelope, presence  # noqa: E402
from apps.wsgw.connection_manager import MANAGER  # noqa: E402


class Registry:
    """The two calls the backend makes on a ConnectionManager, from a socket count per peer."""

    def __init__(self, fail_publish: bool = False) -> None:
        self.sockets: Counter = Counter()
        self.frames: list[dict] = []
        self.fail_publish = fail_publish

    async def is_online(self, peer_id) -> bool:
        return self.sockets[peer_id] > 0

    async def publish_presence(self, peer_id, frame) -> int:
        if self.fail_publish:
            raise ConnectionError("planted publish failure")
        self.frames.append(frame)
        return 1


class Clock:
    """A sleep that returns when the test says so."""

    def __init__(self) -> None:
        self.waiting: list[tuple[float, asyncio.Event, asyncio.Task]] = []

    async def sleep(self, delay: float) -> None:
        event = asyncio.Event()
        self.waiting.append((delay, event, asyncio.current_task()))
        await event.wait()

    async def settle(self) -> None:
        """Let tasks created just now reach their sleep."""
        for _ in range(10):
            await asyncio.sleep(0)

    async def delays(self) -> list[float]:
        await self.settle()
        return [d for d, _, t in self.waiting if not t.done()]

    async def release(self) -> list[float]:
        """End every sleep now in progress, wait for its timer to finish, and return the
        delays that were still pending (a cancelled timer is not among them)."""
        await self.settle()
        waiting, self.waiting = self.waiting, []
        live = [(d, e, t) for d, e, t in waiting if not t.done()]
        for _, event, _ in live:
            event.set()
        await asyncio.gather(*(t for _, _, t in waiting), return_exceptions=True)
        return [d for d, _, _ in live]


def _backend(**kw):
    reg, clock = Registry(kw.pop("fail_publish", False)), Clock()
    kw.setdefault("arrival_secs", 3.0)
    kw.setdefault("departure_grace_secs", 5.0)
    return presence.LocalPresenceBackend(reg, sleep=clock.sleep, **kw), reg, clock


async def _open(backend, reg, peer) -> uuid.UUID:
    """What the consumer does: register the socket, then mark_online (step 9)."""
    reg.sockets[peer] += 1
    conn = uuid.uuid4()
    await backend.mark_online(peer, conn)
    return conn


async def _close(backend, reg, peer, conn) -> None:
    """What `_teardown` does: deregister, and mark_offline only for the LAST socket."""
    reg.sockets[peer] -= 1
    if reg.sockets[peer] == 0:
        await backend.mark_offline(peer, conn)


def _online(peer):
    return envelope.peer_online(peer)


def _offline(peer):
    return envelope.peer_offline(peer)


async def _shown(backend, reg, clock, peer) -> uuid.UUID:
    conn = await _open(backend, reg, peer)
    assert await clock.release() == [3.0]
    assert reg.frames == [_online(peer)]
    return conn


# ---------------------------------------------------------------------------- arrival

def test_an_arrival_is_sent_only_after_its_window():
    async def run():
        backend, reg, clock = _backend()
        peer = uuid.uuid4()
        await _open(backend, reg, peer)
        assert reg.frames == [], "peer_online went out before the arrival window"
        assert await clock.delays() == [3.0]
        assert await clock.release() == [3.0]
        assert reg.frames == [_online(peer)]
    asyncio.run(run())


def test_a_socket_that_closes_inside_the_arrival_window_sends_nothing():
    async def run():
        backend, reg, clock = _backend()
        peer = uuid.uuid4()
        conn = await _open(backend, reg, peer)
        await clock.settle()
        await _close(backend, reg, peer, conn)
        assert await clock.release() == [], "the arrival timer was not cancelled"
        assert reg.frames == [], "a peer that was never shown was announced, or announced gone"
    asyncio.run(run())


def test_a_second_socket_inside_the_arrival_window_sends_one_frame():
    async def run():
        backend, reg, clock = _backend()
        peer = uuid.uuid4()
        await _open(backend, reg, peer)
        await _open(backend, reg, peer)
        assert await clock.release() == [3.0], "a second timer was started"
        assert reg.frames == [_online(peer)]
    asyncio.run(run())


def test_a_further_socket_of_a_shown_peer_is_sent_at_once():
    async def run():
        backend, reg, clock = _backend()
        peer = uuid.uuid4()
        await _shown(backend, reg, clock, peer)
        await _open(backend, reg, peer)
        assert reg.frames == [_online(peer), _online(peer)], "020's every-socket rule was lost"
        assert await clock.delays() == []
    asyncio.run(run())


# -------------------------------------------------------------------------- departure

def test_a_departure_is_sent_only_after_its_grace():
    async def run():
        backend, reg, clock = _backend()
        peer = uuid.uuid4()
        conn = await _shown(backend, reg, clock, peer)
        await _close(backend, reg, peer, conn)
        assert reg.frames == [_online(peer)], "peer_offline went out before the grace"
        assert await clock.delays() == [5.0]
        assert await clock.release() == [5.0]
        assert reg.frames == [_online(peer), _offline(peer)]
    asyncio.run(run())


def test_a_return_inside_the_grace_sends_nothing():
    async def run():
        backend, reg, clock = _backend()
        peer = uuid.uuid4()
        conn = await _shown(backend, reg, clock, peer)
        await _close(backend, reg, peer, conn)
        await clock.settle()
        conn = await _open(backend, reg, peer)
        assert await clock.release() == [], "the departure timer was not cancelled"
        assert reg.frames == [_online(peer)], "a peer that never left was shown leaving or returning"
        # It is still shown: its next real departure is held as a departure, not re-announced.
        await _close(backend, reg, peer, conn)
        assert await clock.release() == [5.0]
        assert reg.frames == [_online(peer), _offline(peer)]
    asyncio.run(run())


def test_a_return_after_the_grace_is_a_new_arrival():
    async def run():
        backend, reg, clock = _backend()
        peer = uuid.uuid4()
        conn = await _shown(backend, reg, clock, peer)
        await _close(backend, reg, peer, conn)
        assert await clock.release() == [5.0]
        await _open(backend, reg, peer)
        assert reg.frames == [_online(peer), _offline(peer)], "a returning peer skipped its window"
        assert await clock.release() == [3.0]
        assert reg.frames == [_online(peer), _offline(peer), _online(peer)]
    asyncio.run(run())


def test_closing_one_of_two_sockets_holds_nothing():
    async def run():
        backend, reg, clock = _backend()
        peer = uuid.uuid4()
        first = await _shown(backend, reg, clock, peer)
        await _open(backend, reg, peer)
        await _close(backend, reg, peer, first)
        assert await clock.delays() == []
        assert reg.frames == [_online(peer), _online(peer)]
    asyncio.run(run())


# --------------------------------------------------------- the registry decides at fire

def test_an_arrival_whose_sockets_are_gone_when_it_fires_is_dropped():
    async def run():
        backend, reg, clock = _backend()
        peer = uuid.uuid4()
        await _open(backend, reg, peer)
        reg.sockets[peer] = 0                  # gone without a mark_offline reaching the backend
        assert await clock.release() == [3.0]
        assert reg.frames == []
        await _open(backend, reg, peer)        # not shown, so this is a fresh arrival
        assert reg.frames == []
        assert await clock.release() == [3.0]
        assert reg.frames == [_online(peer)]
    asyncio.run(run())


def test_a_departure_whose_peer_is_back_when_it_fires_is_dropped():
    async def run():
        backend, reg, clock = _backend()
        peer = uuid.uuid4()
        conn = await _shown(backend, reg, clock, peer)
        await _close(backend, reg, peer, conn)
        reg.sockets[peer] = 1                  # registered, its mark_online not yet run
        assert await clock.release() == [5.0]
        assert reg.frames == [_online(peer)], "an offline contradicted a socket the peer held"
        await backend.mark_online(peer, uuid.uuid4())     # still shown: 020's immediate rule
        assert reg.frames == [_online(peer), _online(peer)]
    asyncio.run(run())


def test_a_failed_publish_of_a_held_frame_is_logged_not_raised(caplog):
    async def run():
        backend, reg, clock = _backend(fail_publish=True)
        peer = uuid.uuid4()
        await _open(backend, reg, peer)
        with caplog.at_level(logging.ERROR, logger="meshychat.wsgw.presence"):
            assert await clock.release() == [3.0]
        assert any("held arrival" in r.getMessage() for r in caplog.records)
    asyncio.run(run())


def test_a_timer_left_by_a_closed_event_loop_holds_nothing_back():
    backend, reg, clock = _backend()
    peer = uuid.uuid4()

    async def first():
        await _open(backend, reg, peer)
        await clock.settle()                   # asyncio.run cancels the timer as it closes

    asyncio.run(first())
    clock.waiting.clear()

    async def second():
        await _open(backend, reg, peer)        # the dead timer must not swallow this arrival
        assert await clock.release() == [3.0]
        assert reg.frames == [_online(peer)]
    asyncio.run(second())


# ------------------------------------------------------------------ windows and wiring

def test_windows_of_zero_send_at_once_as_before():
    async def run():
        backend, reg, clock = _backend(arrival_secs=0, departure_grace_secs=0)
        peer = uuid.uuid4()
        first = await _open(backend, reg, peer)
        second = await _open(backend, reg, peer)
        await _close(backend, reg, peer, first)
        await _close(backend, reg, peer, second)
        assert await clock.delays() == []
        assert reg.frames == [_online(peer), _online(peer), _offline(peer)]
    asyncio.run(run())


def test_the_production_backend_holds_three_and_five_seconds_on_the_shared_registry():
    assert isinstance(presence.BACKEND, presence.LocalPresenceBackend)
    assert presence.BACKEND.manager is MANAGER
    assert (presence.BACKEND.arrival_secs, presence.BACKEND.departure_grace_secs) == (3.0, 5.0)
    assert (presence.ARRIVAL_SECS, presence.DEPARTURE_GRACE_SECS) == (3.0, 5.0)
    assert presence.BACKEND._sleep is asyncio.sleep


# ------------------------------------------------------- snapshot on subscribe (106)

from apps.wsgw import frame_handlers  # noqa: E402
from apps.wsgw.connection_manager import ConnectionHandle, ConnectionManager  # noqa: E402
from apps.wsgw.constants import MAX_PRESENCE_SUBSCRIPTIONS_PER_CONN  # noqa: E402
from apps.wsgw.send_queue import SendQueue  # noqa: E402


async def _hub(**kw):
    """A real registry with one watcher socket, a debounced backend on it, and the watcher's
    frame context, as the consumer builds it."""
    manager, clock = ConnectionManager(), Clock()
    kw.setdefault("arrival_secs", 3.0)
    kw.setdefault("departure_grace_secs", 5.0)
    backend = kw.pop("backend", None) or presence.LocalPresenceBackend(manager, sleep=clock.sleep, **kw)
    watcher, conn, queue = uuid.uuid4(), uuid.uuid4(), SendQueue()
    await manager.register(ConnectionHandle(connection_id=conn, peer_id=watcher, queue=queue))
    ctx = frame_handlers.FrameContext(manager=manager, presence=backend, peer_id=watcher,
                                      connection_id=conn, queue=queue)
    return manager, backend, clock, ctx, queue


async def _peer_socket(manager, backend, peer) -> uuid.UUID:
    conn = uuid.uuid4()
    await manager.register(ConnectionHandle(connection_id=conn, peer_id=peer, queue=SendQueue()))
    await backend.mark_online(peer, conn)
    return conn


async def _peer_close(manager, backend, peer, conn) -> None:
    if await manager.deregister(peer, conn):
        await backend.mark_offline(peer, conn)


def _sub(*peers):
    return [f"presence:{p}" for p in peers]


def test_a_late_subscriber_to_a_shown_peer_gets_its_online_after_the_ack():
    async def run():
        manager, backend, clock, ctx, queue = await _hub()
        x = uuid.uuid4()
        await _peer_socket(manager, backend, x)
        assert await clock.release() == [3.0]          # x is shown, to no one yet
        await frame_handlers.handle_subscribe(ctx, _sub(x))
        assert queue.snapshot() == [envelope.subscribed(_sub(x)), _online(x)]
    asyncio.run(run())


def test_a_peer_inside_its_arrival_window_is_not_in_the_snapshot_but_arrives_after():
    async def run():
        manager, backend, clock, ctx, queue = await _hub()
        x = uuid.uuid4()
        await _peer_socket(manager, backend, x)
        await frame_handlers.handle_subscribe(ctx, _sub(x))
        assert queue.snapshot() == [envelope.subscribed(_sub(x))], "an unshown peer was snapshotted"
        assert await clock.release() == [3.0]          # control: the held arrival still reaches it
        assert queue.snapshot() == [envelope.subscribed(_sub(x)), _online(x)]
    asyncio.run(run())


def test_a_peer_inside_its_departure_grace_is_still_snapshotted_online():
    async def run():
        manager, backend, clock, ctx, queue = await _hub()
        x = uuid.uuid4()
        conn = await _peer_socket(manager, backend, x)
        assert await clock.release() == [3.0]
        await _peer_close(manager, backend, x, conn)
        await frame_handlers.handle_subscribe(ctx, _sub(x))
        assert queue.snapshot() == [envelope.subscribed(_sub(x)), _online(x)]
        assert await clock.release() == [5.0]          # and its offline follows when the grace ends
        assert queue.snapshot()[-1] == _offline(x)
    asyncio.run(run())


def test_a_peer_shown_gone_is_not_snapshotted():
    async def run():
        manager, backend, clock, ctx, queue = await _hub()
        x = uuid.uuid4()
        conn = await _peer_socket(manager, backend, x)
        assert await clock.release() == [3.0]
        await _peer_close(manager, backend, x, conn)
        assert await clock.release() == [5.0]
        await frame_handlers.handle_subscribe(ctx, _sub(x))
        assert queue.snapshot() == [envelope.subscribed(_sub(x))]
    asyncio.run(run())


def test_the_snapshot_names_each_subscribed_peer_once_and_skips_the_rest():
    async def run():
        manager, backend, clock, ctx, queue = await _hub(arrival_secs=0)
        x, y, never = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        await _peer_socket(manager, backend, x)
        await _peer_socket(manager, backend, y)
        channels = _sub(x, never, x) + ["presence:not-a-uuid", "call:abc"] + _sub(y)
        await frame_handlers.handle_subscribe(ctx, channels)
        assert queue.snapshot() == [envelope.subscribed(channels), _online(x), _online(y)]
    asyncio.run(run())


def test_a_channel_the_cap_dropped_gets_no_snapshot():
    async def run():
        manager, backend, clock, ctx, queue = await _hub(arrival_secs=0)
        x = uuid.uuid4()
        await _peer_socket(manager, backend, x)
        filler = _sub(*(uuid.uuid4() for _ in range(MAX_PRESENCE_SUBSCRIPTIONS_PER_CONN)))
        await manager.subscribe_presence(ctx.connection_id, filler)
        await frame_handlers.handle_subscribe(ctx, _sub(x))
        assert queue.snapshot() == [envelope.subscribed(_sub(x))], "a dropped channel was answered"
        # Control: with a slot free, the same subscribe is answered.
        await manager.unsubscribe_presence(ctx.connection_id, filler[:1])
        await frame_handlers.handle_subscribe(ctx, _sub(x))
        assert queue.snapshot()[-1] == _online(x)
    asyncio.run(run())


def test_only_the_subscribing_socket_gets_the_snapshot():
    async def run():
        manager, backend, clock, ctx, queue = await _hub(arrival_secs=0)
        x = uuid.uuid4()
        other_q = SendQueue()
        other = uuid.uuid4()
        await manager.register(ConnectionHandle(connection_id=other, peer_id=uuid.uuid4(), queue=other_q))
        await manager.subscribe_presence(other, _sub(x))
        await _peer_socket(manager, backend, x)
        assert other_q.snapshot() == [_online(x)]      # the live frame, before ctx subscribed
        await frame_handlers.handle_subscribe(ctx, _sub(x))
        assert queue.snapshot() == [envelope.subscribed(_sub(x)), _online(x)]
        assert other_q.snapshot() == [_online(x)], "the snapshot was broadcast"
    asyncio.run(run())


def test_a_backend_that_sends_no_frames_snapshots_no_one():
    async def run():
        recording = presence.RecordingPresenceBackend()
        manager, backend, clock, ctx, queue = await _hub(backend=recording)
        x = uuid.uuid4()
        await _peer_socket(manager, recording, x)
        await frame_handlers.handle_subscribe(ctx, _sub(x))
        assert queue.snapshot() == [envelope.subscribed(_sub(x))]
    asyncio.run(run())


# --------------------------------------------- the snapshot never evicts (pxx 8ba808, GitHub Codex P2)

async def _shown_peers(manager, backend, n):
    peers = [uuid.uuid4() for _ in range(n)]
    for p in peers:
        await _peer_socket(manager, backend, p)
    return peers


async def _drain_into(queue, out, stop):
    while not stop.is_set():
        try:
            out.append(await asyncio.wait_for(queue.pop(), timeout=0.05))
        except asyncio.TimeoutError:
            continue


@pytest.mark.parametrize("n", [255, 256, 1000])
def test_a_large_snapshot_keeps_its_ack_first_and_every_online_exactly_once(n):
    async def run():
        manager, backend, clock, ctx, queue = await _hub(arrival_secs=0)
        peers = await _shown_peers(manager, backend, n)
        got, stop = [], asyncio.Event()
        writer = asyncio.create_task(_drain_into(queue, got, stop))
        await frame_handlers.handle_subscribe(ctx, _sub(*peers))
        while len(queue):
            await asyncio.sleep(0.01)
        stop.set()
        await writer
        assert queue.dropped == 0, "the snapshot evicted frames"
        assert got[0] == envelope.subscribed(_sub(*peers)), "the ack was not first"
        assert sorted(f["peer_id"] for f in got[1:]) == sorted(str(p) for p in peers)
        assert all(f == _online(uuid.UUID(f["peer_id"])) for f in got[1:])
    asyncio.run(run())


def test_a_snapshot_into_a_queue_near_capacity_loses_none_of_the_frames_already_queued():
    async def run():
        manager, backend, clock, ctx, queue = await _hub(arrival_secs=0)
        peers = await _shown_peers(manager, backend, 100)
        backlog = [{"type": "backlog", "n": i} for i in range(250)]
        for frame in backlog:
            queue.push_nowait(frame)
        got, stop = [], asyncio.Event()
        subscribe = asyncio.create_task(frame_handlers.handle_subscribe(ctx, _sub(*peers)))
        await asyncio.sleep(0.05)                     # the reader is slow: nothing drains yet
        assert queue.dropped == 0 and len(queue) <= queue.capacity
        writer = asyncio.create_task(_drain_into(queue, got, stop))
        await subscribe
        while len(queue):
            await asyncio.sleep(0.01)
        stop.set()
        await writer
        assert queue.dropped == 0
        assert got[:250] == backlog, "a frame queued before the snapshot was lost or reordered"
        assert got[250] == envelope.subscribed(_sub(*peers))
        assert len(got) == 251 + 100
    asyncio.run(run())


def test_a_reader_that_never_drains_stops_the_snapshot_without_evicting_anything(monkeypatch):
    async def run():
        manager, backend, clock, ctx, queue = await _hub(arrival_secs=0)
        peers = await _shown_peers(manager, backend, 300)
        ticks = iter(range(0, 10_000))
        monkeypatch.setattr(frame_handlers, "_clock", lambda: float(next(ticks)))   # one second per look
        async def no_wait(_s):
            await asyncio.sleep(0)
        monkeypatch.setattr(frame_handlers, "_sleep", no_wait)
        await frame_handlers.handle_subscribe(ctx, _sub(*peers))
        assert queue.dropped == 0
        assert len(queue) == queue.capacity - frame_handlers.SNAPSHOT_HEADROOM, "it pushed past its room"
        frames = queue.snapshot()
        assert frames[0] == envelope.subscribed(_sub(*peers))
    asyncio.run(run())


def test_a_departure_scheduled_at_the_check_lands_after_the_online_never_before_it():
    """pxx 8ba808's caution: the check and the push are one step. The departure is scheduled from inside is_shown
    for the peer, so it runs at the first await after the check. With none before the push, the online is queued
    first and the offline after it, which is the truth. An await in between would queue the offline first and then
    a stale online, leaving the watcher believing a departed peer is online."""
    async def run():
        manager, backend, clock, ctx, queue = await _hub(arrival_secs=0, departure_grace_secs=0)
        peer = (await _shown_peers(manager, backend, 1))[0]
        conn = next(h.connection_id for h in manager._by_peer[peer])
        real = backend.is_shown

        def is_shown(p):
            if p == peer:
                asyncio.get_running_loop().create_task(_peer_close(manager, backend, peer, conn))
            return real(p)

        backend.is_shown = is_shown
        await frame_handlers.handle_subscribe(ctx, _sub(peer))
        for _ in range(5):
            await asyncio.sleep(0)
        about = [f["type"] for f in queue.snapshot() if f.get("peer_id") == str(peer)]
        assert about == ["peer_online", "peer_offline"], about
    asyncio.run(run())


def test_a_departure_during_a_wait_for_room_never_leaves_a_stale_online():
    """The queue is at the snapshot's room when the peer's turn comes, so the snapshot waits, and the peer departs
    (grace 0) from inside is_shown. The watcher's last frame about the peer must be its offline."""
    async def run():
        manager, backend, clock, ctx, queue = await _hub(arrival_secs=0, departure_grace_secs=0)
        peer = (await _shown_peers(manager, backend, 1))[0]
        conn = next(h.connection_id for h in manager._by_peer[peer])
        for i in range(queue.capacity - frame_handlers.SNAPSHOT_HEADROOM):
            queue.push_nowait({"type": "backlog", "n": i})
        real = backend.is_shown

        def is_shown(p):
            if p == peer:
                asyncio.get_running_loop().create_task(_peer_close(manager, backend, peer, conn))
            return real(p)

        backend.is_shown = is_shown
        got, stop = [], asyncio.Event()
        subscribe = asyncio.create_task(frame_handlers.handle_subscribe(ctx, _sub(peer)))
        await asyncio.sleep(0.05)                      # the snapshot is waiting for room
        writer = asyncio.create_task(_drain_into(queue, got, stop))
        await subscribe
        for _ in range(5):
            await asyncio.sleep(0)
        while len(queue):
            await asyncio.sleep(0.01)
        stop.set()
        await writer
        about = [f["type"] for f in got if f.get("peer_id") == str(peer)]
        assert about and about[-1] == "peer_offline", about
        assert queue.dropped == 0
    asyncio.run(run())
