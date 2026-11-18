"""A consumer whose task is CANCELLED after it registered gives its ConnectionManager slot back.

HQD production, 27 Oct 2026: during a stall on :6310, daphne cancelled upgrade tasks after they had
registered (consumer.py, step 7). Neither `disconnect` nor the refused-close path ran, so each left a
slot with no socket behind it, and after MAX_CONNS_PER_PEER of them every later upgrade of the peer
was refused TOO_MANY_CONNECTIONS (all ten HQB identities, 7.5 h, cleared only by a restart). These
tests run the consumer's own `__call__` with Channels' dispatch loop replaced by "register, then
stall", and cancel it as daphne does. No database: the frame connection is never opened."""
from __future__ import annotations

import asyncio
import uuid

import pytest
from channels.generic.websocket import AsyncWebsocketConsumer

from apps.wsgw import consumer as C
from apps.wsgw.connection_manager import ConnectionHandle, ConnectionManager, TooManyConnections
from apps.wsgw.constants import MAX_CONNS_PER_PEER


class _Presence:
    def __init__(self):
        self.offline = []

    async def mark_offline(self, peer_id, connection_id):
        self.offline.append((peer_id, connection_id))


def _consumer(manager, presence):
    cfg = C.HubConfig(jwt_public_key_pem=None, jwt_issuer=None)
    return C.MeshyWebSocketConsumer(config=cfg, manager=manager, presence=presence)


async def _register_then_stall(self, scope, receive, send):
    """Stands in for Channels' dispatch loop: what `connect` does up to step 7, then a stall."""
    handle = ConnectionHandle(connection_id=self.connection_id, peer_id=scope["peer"], queue=self.queue)
    await self.manager.register(handle)
    self.handle, self.peer_id, self._registered = handle, scope["peer"], True
    scope["registered"].set()
    await asyncio.Event().wait()


async def _run_and_cancel(cons, peer):
    registered = asyncio.Event()
    task = asyncio.ensure_future(cons({"peer": peer, "registered": registered}, None, None))
    await asyncio.wait_for(registered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_a_cancelled_consumer_gives_its_slot_back(monkeypatch):
    monkeypatch.setattr(AsyncWebsocketConsumer, "__call__", _register_then_stall)
    mgr, presence, peer = ConnectionManager(), _Presence(), uuid.uuid4()
    cons = _consumer(mgr, presence)
    await _run_and_cancel(cons, peer)
    assert peer not in mgr._by_peer, "the slot was released"
    assert cons._torn_down and not cons._registered
    assert presence.offline == [(peer, cons.connection_id)], "its last socket: presence goes offline"


@pytest.mark.asyncio
async def test_more_cancellations_than_the_cap_still_leave_the_peer_able_to_connect(monkeypatch):
    monkeypatch.setattr(AsyncWebsocketConsumer, "__call__", _register_then_stall)
    mgr, peer = ConnectionManager(), uuid.uuid4()
    for _ in range(MAX_CONNS_PER_PEER + 1):
        await _run_and_cancel(_consumer(mgr, _Presence()), peer)
    await mgr.register(ConnectionHandle(connection_id=uuid.uuid4(), peer_id=peer, queue=C.SendQueue()))


@pytest.mark.asyncio
async def test_red_control_without_the_finally_the_cap_locks_the_peer_out(monkeypatch):
    """The same cancellations through Channels' __call__ alone (the code before this fix): every one
    leaks its slot, and the next upgrade is refused, exactly what production showed."""
    monkeypatch.setattr(AsyncWebsocketConsumer, "__call__", _register_then_stall)
    mgr, peer = ConnectionManager(), uuid.uuid4()
    for _ in range(MAX_CONNS_PER_PEER):
        cons = _consumer(mgr, _Presence())
        registered = asyncio.Event()
        task = asyncio.ensure_future(AsyncWebsocketConsumer.__call__(cons, {"peer": peer, "registered": registered},
                                                                     None, None))
        await asyncio.wait_for(registered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(mgr._by_peer[peer]) == MAX_CONNS_PER_PEER
    with pytest.raises(TooManyConnections):
        await mgr.register(ConnectionHandle(connection_id=uuid.uuid4(), peer_id=peer, queue=C.SendQueue()))


@pytest.mark.asyncio
async def test_a_normal_disconnect_tears_down_once_and_the_finally_adds_nothing(monkeypatch):
    calls = []

    async def register_then_disconnect(self, scope, receive, send):
        handle = ConnectionHandle(connection_id=self.connection_id, peer_id=scope["peer"], queue=self.queue)
        await self.manager.register(handle)
        self.handle, self.peer_id, self._registered = handle, scope["peer"], True
        await self.disconnect(1000)
    monkeypatch.setattr(AsyncWebsocketConsumer, "__call__", register_then_disconnect)
    mgr, presence, peer = ConnectionManager(), _Presence(), uuid.uuid4()
    cons = _consumer(mgr, presence)
    real = cons._teardown

    async def counted():
        calls.append(1)
        await real()
    cons._teardown = counted
    await cons({"peer": peer}, None, None)
    assert calls == [1] and peer not in mgr._by_peer and len(presence.offline) == 1


@pytest.mark.asyncio
async def test_a_second_cancel_during_the_teardown_cannot_leave_the_slot_half_released(monkeypatch):
    """The teardown is shielded and awaited to its end: cancelling the task again while it runs does not
    stop the deregister."""
    monkeypatch.setattr(AsyncWebsocketConsumer, "__call__", _register_then_stall)
    mgr, peer = ConnectionManager(), uuid.uuid4()
    gate = asyncio.Event()

    class SlowPresence(_Presence):
        async def mark_offline(self, peer_id, connection_id):
            await gate.wait()
            await super().mark_offline(peer_id, connection_id)
    presence = SlowPresence()
    cons = _consumer(mgr, presence)
    registered = asyncio.Event()
    task = asyncio.ensure_future(cons({"peer": peer, "registered": registered}, None, None))
    await asyncio.wait_for(registered.wait(), 5)
    at_end = []
    task.add_done_callback(lambda t: at_end.append(peer in mgr._by_peer or not presence.offline))
    task.cancel()
    await asyncio.sleep(0.05)                      # the teardown is now waiting inside mark_offline
    task.cancel()                                  # a second cancel
    asyncio.get_running_loop().call_later(0.1, gate.set)
    with pytest.raises(asyncio.CancelledError):
        await task
    # Checked AT the moment the task ended, not later: the task must not end before its teardown did.
    assert at_end == [False], "the task ended while its slot or its presence row was still held"
    assert peer not in mgr._by_peer and presence.offline == [(peer, cons.connection_id)]


@pytest.mark.asyncio
@pytest.mark.parametrize("cancels", [3, 5])
async def test_repeated_cancels_during_a_slow_teardown_never_cut_it_short(monkeypatch, cancels):
    """pxx's #441 review: daphne's application checker cancels again on every tick while the
    application is not done, and a stall is exactly when the teardown is slow. Every cancel after
    the first lands while the teardown waits in mark_offline; when the task ends, the slot and the
    presence row are released, and the task ends cancelled."""
    monkeypatch.setattr(AsyncWebsocketConsumer, "__call__", _register_then_stall)
    mgr, peer = ConnectionManager(), uuid.uuid4()
    gate = asyncio.Event()

    class SlowPresence(_Presence):
        async def mark_offline(self, peer_id, connection_id):
            await gate.wait()
            await super().mark_offline(peer_id, connection_id)
    presence = SlowPresence()
    cons = _consumer(mgr, presence)
    registered = asyncio.Event()
    task = asyncio.ensure_future(cons({"peer": peer, "registered": registered}, None, None))
    await asyncio.wait_for(registered.wait(), 5)
    at_end = []
    task.add_done_callback(lambda t: at_end.append((peer in mgr._by_peer, list(presence.offline), t.cancelled())))
    for _ in range(cancels):
        task.cancel()
        await asyncio.sleep(0.02)                  # each later cancel arrives inside the slow teardown
    asyncio.get_running_loop().call_later(0.1, gate.set)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert at_end == [(False, [(peer, cons.connection_id)], True)], at_end
