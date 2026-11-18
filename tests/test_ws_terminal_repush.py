"""F04, a lost TERMINAL frame is re-pushed on the loss pass (ASDLC OI-30, operator decision 60,
D-2026-09-27-19; docs/DIVERGENCES.md 048).

THE GAP, as origin/main (c4d2125) has it. A caller's registration for a call ends when its
terminal frame is QUEUED (call_registry.deliver_terminal, caller_frames._after_start and
_subscribe_one). If the drop-oldest send queue then evicts
that frame, nothing sends it again: the periodic terminal re-check reads REGISTERED calls only,
and the loss pass re-offers skill_invoke to targets only ("a loss says nothing about a
   terminal", call_notify._run_once). The caller that waits on the socket waits until its own
   timeout; without a delivered terminal frame, it has no outcome over WS. The approved
design's 4.9 promised a re-offer "by the sweep the loss triggers"; 041 S6 recorded the
departure and raised it (divergence 041, the S6 entry's limits); decision 60 closes it.

WHAT IS PROVED HERE

  queue    the loss record NAMES the call of every terminal frame the overflow evicted, once,
           and only when no copy of it is still queued or was already taken by the writer; a
           non-terminal frame is counted and never named; both memories are bounded (B8).
  one      every frame the one terminal mapping builds is recognised as terminal, and no other
           frame the hub sends is.
  pass     the loss pass re-reads the named call's ROW and pushes its terminal frame to the
           socket that lost it, and to no other; CONTROL, a terminal the socket already has
           (queued, or taken by the writer) is not pushed again, and another finished call
           that was not named is not pushed at all; a gone socket and a record naming a call
           the socket's peer did not make push nothing; a throttled loss leaves the record for
           the periodic sweep.
  writer   through the real `_writer_loop`: a dropped chunk and ack stay dropped because chunks
           are lossy by design and acks are not re-sent; a dropped terminal is written again.
  wire     the production consumer, a real socket: a subscribed caller whose skill_timeout is
           evicted by a queue's worth of filler receives it after the filler, from the loss
           pass alone (the periodic sweep is an hour away and the registration has ended);
           CONTROL, a delivered terminal is not sent again when the same socket later drops
           frames and the loss pass runs.

HOW TERMINAL DELIVERY IS DEDUPED: a multi-call client keys outcomes on call_id and treats the
first terminal as final (design 4.9). The server side here does not lean on that behavior: it
re-pushes only a terminal no copy of which is queued or was taken by the writer.

RED on origin/main c4d2125, with this file: see docs/DIVERGENCES.md 048 (the queue and pass
tests fail on the missing record and the missing keyword; the wire test times out waiting for
the terminal, which is the defect itself).

Run:  MESHY_TEMPLATE_DSN=... .venv313/Scripts/python.exe -m pytest tests/test_ws_terminal_repush.py
      -q -p no:cacheprovider
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid

import pytest

from apps.wsgw import call_notify, envelope, send_queue
from apps.wsgw.connection_manager import MANAGER, ConnectionHandle, ConnectionManager
from apps.wsgw.send_queue import SendQueue
from apps.calls.envelope import SignedEnvelope
from tests.fixtures.call_rows import seed_call
from tests.fixtures.hub_presence_db import hub_connections_clean  # noqa: F401  (autouse)
# The S5 harness; its autouse fixtures (server_config, bridges_stopped) apply here by import.
from tests.test_call_delivery import (  # noqa: F401  (fixtures are used by name)
    NEVER,
    _connect,
    _delta,
    _next,
    _pong,
    _skill_id,
    _until,
    bridges_stopped,
    invoke,
    owner,
    server_config,
    signed_call,
    tuned,
    world,
)
from tests.test_ws_caller_frames import (  # noqa: F401  (registry_cleared is autouse)
    _commit_terminal,
    _registered,
    registry_cleared,
    subscribe,
)
from tests.test_ws_caller_units import ROWS

PONG = envelope.pong()

#: The three terminal frame types, as literals.
_TERMINAL_TYPES = ("skill_result", "skill_error", "skill_timeout")


def timeout_frame(call_id: str) -> dict:
    return envelope.skill_timeout(call_id)


def _chunk(call_id: str, sequence: int) -> dict:
    return envelope.skill_chunk(call_id, sequence, "data",
                                SignedEnvelope(payload={"n": sequence}, signature="s",
                                               signing_kid="k"))


# ============================================================ the queue names a lost terminal

def test_a_terminal_evicted_by_an_overflow_is_named_and_a_non_terminal_is_only_counted():
    """Four frames into a queue of two: the ack and the terminal of c-1 are evicted. The record
    counts both, as before (the range is unchanged), and NAMES c-1, once. CONTROL in the same
    test: a burst that evicts only non-terminal frames names nothing."""
    q = SendQueue(capacity=2)
    q.push_nowait(envelope.skill_acked("c-1"))
    q.push_nowait(timeout_frame("c-1"))
    q.push_nowait(PONG)
    q.push_nowait(PONG)
    loss = q.take_loss()
    assert (loss.count, loss.first_seq, loss.last_seq) == (2, 0, 1)
    assert loss.terminal_call_ids == ("c-1",)
    assert loss.terminals_unnamed == 0

    q = SendQueue(capacity=2)                                   # control: nothing terminal lost
    for frame in (envelope.skill_acked("c-2"), _chunk("c-2", 0), PONG, PONG):
        q.push_nowait(frame)
    loss = q.take_loss()
    assert loss.count == 2 and loss.terminal_call_ids == ()


@pytest.mark.parametrize("name", sorted(ROWS))
def test_every_frame_the_one_terminal_mapping_builds_is_recognised_as_terminal(name):
    """caller_frames.terminal_frame is the only builder of the three terminal types
    (skill_result, skill_error, skill_timeout); each row shape it maps must be recognised, or
    that terminal's loss would go unnamed and silently stay lost."""
    from apps.wsgw import caller_frames

    frame = caller_frames.terminal_frame(ROWS[name])
    assert send_queue.terminal_call_id(frame) == "c-1", frame


def test_no_other_frame_the_hub_sends_is_recognised_as_terminal():
    """Only a terminal is re-pushed, so nothing else may be named. CONTROL: the
    timeout frame, in the same list's terms, is."""
    others = [PONG, envelope.skill_acked("c-1"), _chunk("c-1", 3),
              envelope.error(envelope.CODE_SKILL_CALL_FAILED, "x"),
              envelope.subscribed(["call:c-1"]), envelope.peer_online(uuid.uuid4()),
              {"type": "skill_invoke", "call_id": "c-1"}, {"type": "filler", "n": 1},
              "not a dict", None]
    assert [send_queue.terminal_call_id(f) for f in others] == [None] * len(others)
    assert send_queue.terminal_call_id(timeout_frame("c-1")) == "c-1"


def test_a_terminal_with_a_copy_still_queued_or_taken_by_the_writer_is_not_named():
    """The idempotency half at the queue (a caller must not act twice): a copy of the same
    terminal can be queued twice (caller_frames: "one committed in between reaches the socket
    twice"). Losing ONE copy is not losing the terminal. CONTROL: once the surviving copy is
    evicted too, the terminal is lost, and named."""
    q = SendQueue(capacity=2)
    q.push_nowait(timeout_frame("c-1"))
    q.push_nowait(timeout_frame("c-1"))
    q.push_nowait(PONG)                               # evicts the first copy only
    loss = q.take_loss()
    assert loss.count == 1 and loss.terminal_call_ids == (), "a queued copy survives"
    assert q.has_terminal("c-1")
    q.push_nowait(PONG)                               # evicts the second copy
    assert q.take_loss().terminal_call_ids == ("c-1",), "control: now it is lost"
    assert not q.has_terminal("c-1")

    q = SendQueue(capacity=1)
    q.push_nowait(timeout_frame("c-2"))
    assert q.pop_nowait() == timeout_frame("c-2")     # the writer took it
    q.push_nowait(timeout_frame("c-2"))               # a second copy
    q.push_nowait(PONG)                               # evicted
    loss = q.take_loss()
    assert loss.count == 1 and loss.terminal_call_ids == (), "the writer already had it"
    assert q.has_terminal("c-2")


def test_a_named_terminal_whose_new_copy_reaches_the_writer_before_the_report_is_unnamed():
    """A copy pushed and taken after the loss (a subscribe that pushes a finished call's
    terminal) answers the loss before the writer reports it. CONTROL: the loss is still
    counted."""
    q = SendQueue(capacity=1)
    q.push_nowait(timeout_frame("c-3"))
    q.push_nowait(PONG)                               # c-3 lost
    assert q.pop_nowait() == PONG
    q.push_nowait(timeout_frame("c-3"))
    assert q.pop_nowait() == timeout_frame("c-3")
    loss = q.take_loss()
    assert loss.count == 1 and loss.terminal_call_ids == ()


def test_the_named_and_the_taken_terminals_are_bounded(monkeypatch):
    """Rule B8: both memories hold at most TERMINAL_MEMORY call ids. Past it, a lost terminal
    is counted as unnamed (its caller recovers by GET /v1/calls/{id} or a subscribe, as before
    F04), and the oldest taken one is forgotten. CONTROL: the ones inside the bound are kept."""
    monkeypatch.setattr(send_queue, "TERMINAL_MEMORY", 2)
    q = SendQueue(capacity=1)
    for i in range(4):
        q.push_nowait(timeout_frame(f"c-{i}"))        # c-0, c-1 and c-2 evicted
    loss = q.take_loss()
    assert loss.count == 3
    assert loss.terminal_call_ids == ("c-0", "c-1") and loss.terminals_unnamed == 1

    q = SendQueue(capacity=1)
    for i in range(3):
        q.push_nowait(timeout_frame(f"t-{i}"))
        q.pop_nowait()
    assert [q.has_terminal(f"t-{i}") for i in range(3)] == [False, True, True]


# ============================================================ the bridge's loss pass (database)

async def _socket(manager: ConnectionManager, peer: dict, capacity: int = 256):
    handle = ConnectionHandle(connection_id=uuid.uuid4(), peer_id=peer["peer_id"],
                              queue=SendQueue(capacity))
    await manager.register(handle)
    return handle


async def _bridge(manager: ConnectionManager, **timers) -> call_notify.CallBridge:
    bridge = call_notify.CallBridge(manager, **{"beat_secs": NEVER, "reoffer_secs": NEVER,
                                                **timers})
    assert await bridge.ensure_started() is True
    return bridge


def _finished(owner, world, tag: str, *, status: str = "timeout") -> str:
    """A call of the world's caller to its target, already terminal (`status`)."""
    target = world["target"]
    return seed_call(owner, world["caller"], target, _skill_id(owner, target), status=status,
                     call_id=f"f04-{tag}-{uuid.uuid4()}")


def _drain(queue: SendQueue) -> list:
    got = []
    while len(queue):
        got.append(queue.pop_nowait())
    return got


@pytest.mark.asyncio
async def test_a_dropped_terminal_is_repushed_to_the_socket_that_lost_it(owner, world):
    """The loss pass re-reads the named call's ROW and pushes the one mapping's frame to the
    socket whose queue lost it; the caller's second socket, which did not lose it, gets
    nothing. CONTROL: another finished call of the same caller, not named, is not pushed."""
    lost = _finished(owner, world, "lost")
    other = _finished(owner, world, "other", status="abandoned")
    manager = ConnectionManager()
    lossy = await _socket(manager, world["caller"])
    sibling = await _socket(manager, world["caller"])
    bridge = await _bridge(manager)
    try:
        before = bridge.stats()
        bridge.request_reoffer(world["caller"]["peer_id"], connection_id=lossy.connection_id,
                               terminal_call_ids=(lost,))
        await _until(lambda: len(lossy.queue) >= 1, "the lost terminal re-pushed")
        await asyncio.sleep(0.6)                                  # more polls: nothing else
    finally:
        bridge.stop()
    assert _drain(lossy.queue) == [timeout_frame(lost)]
    assert _drain(sibling.queue) == [], "the sibling socket did not lose it"
    assert owner.execute("SELECT status FROM skill_calls WHERE call_id = %s",
                         (other,)).fetchone()[0] == "abandoned", "control: a real finished call"
    assert _delta(before, bridge.stats(), "terminal_repush:") == {
        "terminal_repush:requested": 1, "terminal_repush:sent": 1}


@pytest.mark.asyncio
async def test_a_terminal_the_socket_already_has_is_not_repushed(owner, world):
    """A caller must not act twice. Three calls named lost on one socket: a copy of A's
    terminal was taken by the writer, a copy of B's is still queued, C's has no copy. Only C's
    is pushed; A and B are counted `already_delivered`. C is the positive control."""
    a, b, c = (_finished(owner, world, t) for t in ("had", "queued", "none"))
    manager = ConnectionManager()
    sock = await _socket(manager, world["caller"])
    sock.queue.push_nowait(timeout_frame(a))
    assert sock.queue.pop_nowait() == timeout_frame(a)            # the writer took A's
    sock.queue.push_nowait(timeout_frame(b))                      # B's is queued
    bridge = await _bridge(manager)
    try:
        before = bridge.stats()
        bridge.request_reoffer(world["caller"]["peer_id"], connection_id=sock.connection_id,
                               terminal_call_ids=(a, b, c))
        await _until(lambda: len(sock.queue) >= 2, "C's terminal re-pushed")
        await asyncio.sleep(0.6)
    finally:
        bridge.stop()
    assert _drain(sock.queue) == [timeout_frame(b), timeout_frame(c)]
    assert _delta(before, bridge.stats(), "terminal_repush:") == {
        "terminal_repush:requested": 3, "terminal_repush:already_delivered": 2,
        "terminal_repush:sent": 1}


@pytest.mark.asyncio
async def test_a_gone_socket_and_a_call_that_is_not_the_peers_get_nothing(owner, world):
    """Terminal frames go to the caller's sockets only (design 5.1). A record naming a call the
    socket's peer did not make (here the target's socket naming the caller's call) pushes
    nothing, counted `not_caller`; a record for a socket deregistered before the pass pushes
    nothing, counted `socket_gone`. CONTROL: the caller's live socket gets its own."""
    mine = _finished(owner, world, "mine")
    gone_call = _finished(owner, world, "gone")
    manager = ConnectionManager()
    caller_sock = await _socket(manager, world["caller"])
    target_sock = await _socket(manager, world["target"])
    gone = await _socket(manager, world["caller"])
    await manager.deregister(world["caller"]["peer_id"], gone.connection_id)
    bridge = await _bridge(manager)
    try:
        before = bridge.stats()
        bridge.request_reoffer(world["target"]["peer_id"],
                               connection_id=target_sock.connection_id,
                               terminal_call_ids=(mine,))
        bridge.request_reoffer(world["caller"]["peer_id"], connection_id=gone.connection_id,
                               terminal_call_ids=(gone_call,))
        bridge.request_reoffer(world["caller"]["peer_id"],
                               connection_id=caller_sock.connection_id,
                               terminal_call_ids=(mine,))
        await _until(lambda: len(caller_sock.queue) >= 1, "the caller's own terminal")
        await asyncio.sleep(0.6)
    finally:
        bridge.stop()
    assert _drain(caller_sock.queue) == [timeout_frame(mine)]
    assert _drain(target_sock.queue) == [], "the caller's terminal reached the target"
    assert _drain(gone.queue) == []
    delta = _delta(before, bridge.stats(), "terminal_repush:")
    assert delta == {"terminal_repush:requested": 3, "terminal_repush:not_caller": 1,
                     "terminal_repush:socket_gone": 1, "terminal_repush:sent": 1}, delta


@pytest.mark.asyncio
async def test_a_throttled_loss_leaves_its_terminal_to_the_periodic_sweep(owner, world):
    """The loss pass runs at most once per `loss_reoffer_interval_secs` per peer (S5 review
    F1), and covers the lossy peers only. A terminal named by C's throttled loss is held, not
    dropped: nothing is pushed while only losses arrive, and ANOTHER peer's loss pass (U's,
    which runs, counted) does not push it either; the periodic sweep does. CONTROL: C's first
    loss pass ran (counted), so the throttle, not a dead listener, held the second.

    The pass counter is taken as a pass STARTS, and its re-push runs after its re-offer, so
    `passes() == 1` can be seen while C's first pass is still running; a name held then is
    that pass's to push (the next pass for the peer), which failed this test about one run
    in three. Passes run one at a time on the bridge thread, so T's pass starting is the
    proof that C's has ended, and only then is the name held."""
    held = _finished(owner, world, "held")
    manager = ConnectionManager()
    sock = await _socket(manager, world["caller"])
    await _socket(manager, world["other"])
    bridge = await _bridge(manager)
    try:
        before = bridge.stats()

        def passes() -> int:
            return bridge.stats().get("sweep:reoffer_loss", 0) - before.get(
                "sweep:reoffer_loss", 0)

        bridge.request_reoffer(world["caller"]["peer_id"])                  # accepted
        await _until(lambda: passes() == 1, "C's first loss pass")
        bridge.request_reoffer(world["target"]["peer_id"])                  # T's, accepted
        await _until(lambda: passes() == 2, "T's loss pass, so C's has ended")
        bridge.request_reoffer(world["caller"]["peer_id"], connection_id=sock.connection_id,
                               terminal_call_ids=(held,))                  # throttled
        bridge.request_reoffer(world["other"]["peer_id"])                   # U's, accepted
        await _until(lambda: passes() == 3, "U's loss pass")
        await asyncio.sleep(0.8)
        assert len(sock.queue) == 0, "a throttled loss, or another peer's pass, pushed"
        assert bridge.stats().get("reoffer:loss_throttled", 0) - before.get(
            "reoffer:loss_throttled", 0) == 1
        bridge.reoffer_secs = 0.3
        await _until(lambda: len(sock.queue) >= 1, "the periodic sweep's re-push")
    finally:
        bridge.stop()
    assert _drain(sock.queue) == [timeout_frame(held)]


@pytest.mark.asyncio
async def test_a_failed_read_holds_the_names_for_the_next_pass(owner, world, monkeypatch):
    """The held names are the only record of a lost terminal. A pass whose row read fails
    (counted as the sweep's error) holds them again, and the next pass pushes the frame.
    CONTROL: the first pass did fail, and pushed nothing."""
    lost = _finished(owner, world, "retry")
    manager = ConnectionManager()
    sock = await _socket(manager, world["caller"])
    bridge = await _bridge(manager, loss_reoffer_interval_secs=0.0)
    original = call_notify.CallBridge._terminal_rows
    calls = {"n": 0}

    def flaky(conn, ids):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("the read failed")
        return original(conn, ids)

    monkeypatch.setattr(bridge, "_terminal_rows", flaky)
    try:
        before = bridge.stats()
        bridge.request_reoffer(world["caller"]["peer_id"], connection_id=sock.connection_id,
                               terminal_call_ids=(lost,))
        await _until(lambda: bridge.stats().get("sweep:reoffer_repush_errors", 0)
                     > before.get("sweep:reoffer_repush_errors", 0), "the failed pass")
        assert len(sock.queue) == 0
        bridge.request_reoffer(world["caller"]["peer_id"])                  # the next pass
        await _until(lambda: len(sock.queue) >= 1, "the re-push on the next pass")
    finally:
        bridge.stop()
    assert _drain(sock.queue) == [timeout_frame(lost)]
    assert calls["n"] == 2


# ================================================================ through the real writer

@pytest.mark.asyncio
async def test_through_the_writer_a_dropped_chunk_stays_dropped_and_a_dropped_terminal_is_not(
        owner, world, caplog):
    """The real `_writer_loop` (as tests/test_metrics.py drives it), wired to a CallBridge.
    A chunk and an ack evicted by an overflow are not written again: chunks are lossy by design
    and acks are not re-sent, though the loss
    pass ran for them (counted). Then a terminal evicted the same way IS written again, after
    the frames that survived it. The counted "WS send queue dropped" warning stays one per
    burst (two here), and the lost terminal is named on a line of its own."""
    from apps.wsgw.consumer import HubConfig, MeshyWebSocketConsumer
    from apps.wsgw.presence import RecordingPresenceBackend

    call_id = _finished(owner, world, "writer")
    manager = ConnectionManager()
    bridge = await _bridge(manager, loss_reoffer_interval_secs=0.0)
    config = HubConfig(jwt_public_key_pem=None, jwt_issuer=None, dpop_enforce=False,
                       check_peer_active=False, zero_rtt=False)
    consumer = MeshyWebSocketConsumer(config=config, manager=manager,
                                     presence=RecordingPresenceBackend(), call_bridge=bridge)
    consumer.peer_id = world["caller"]["peer_id"]
    consumer.queue = SendQueue(capacity=2)
    await manager.register(ConnectionHandle(connection_id=consumer.connection_id,
                                            peer_id=consumer.peer_id, queue=consumer.queue))
    written: list = []

    async def record(text_data=None, bytes_data=None, close=False):
        written.append(json.loads(text_data))

    consumer.send = record
    caplog.set_level(logging.WARNING, logger="apps.wsgw.consumer")
    for frame in (_chunk(call_id, 0), envelope.skill_acked(call_id),
                  {"type": "pong", "n": 1}, {"type": "pong", "n": 2}):
        consumer.queue.push_nowait(frame)                         # chunk and ack evicted
    before = bridge.stats()
    writer = asyncio.create_task(consumer._writer_loop())
    try:
        await _until(lambda: bridge.stats().get("sweep:reoffer_loss", 0)
                     > before.get("sweep:reoffer_loss", 0), "the loss pass for the chunk")
        await asyncio.sleep(0.6)
        assert written == [{"type": "pong", "n": 1}, {"type": "pong", "n": 2}], written

        # One loop turn: the terminal and then two frames, so it is evicted before the writer
        # can take it (the queue's capacity is two).
        consumer.queue.push_nowait(timeout_frame(call_id))
        consumer.queue.push_nowait({"type": "pong", "n": 3})
        consumer.queue.push_nowait({"type": "pong", "n": 4})
        await _until(lambda: len(written) >= 5, "the terminal written again")
        await asyncio.sleep(0.6)
    finally:
        writer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await writer
        bridge.stop()
    assert written[2:] == [{"type": "pong", "n": 3}, {"type": "pong", "n": 4},
                           timeout_frame(call_id)], written
    assert _delta(before, bridge.stats(), "terminal_repush:") == {
        "terminal_repush:requested": 1, "terminal_repush:sent": 1}
    lines = [r.getMessage() for r in caplog.records if r.name == "apps.wsgw.consumer"]
    assert sum("WS send queue dropped" in m for m in lines) == 2, lines
    named = [m for m in lines if "WS send queue loss took the terminal frame" in m]
    assert len(named) == 1 and call_id in named[0], lines


# ==================================================================== on the wire (end to end)

def _evict_first_terminal(handle):
    """Make the caller's send queue lose the first terminal frame pushed to it, as an overflow
    does: the frame and then a queue's worth of filler in one loop turn, so the writer never
    takes the frame and finds the loss record (tests/test_call_delivery's T18 pattern). The
    terminal is recognised by its type literals, not by send_queue.terminal_call_id, so this
    instrument runs unchanged on a tree without F04 and the wire test fails there on the
    defect itself (the frame never comes), not on a missing name."""
    queue = handle.queue
    original = queue.push
    state = {"done": False}

    async def push(frame):
        if frame.get("type") in _TERMINAL_TYPES and not state["done"]:
            state["done"] = True
            queue.push_nowait(frame)
            for i in range(queue.capacity):
                queue.push_nowait({"type": "filler", "n": i})
            return
        await original(frame)

    queue.push = push
    return state


async def _after_filler(comm, timeout: float = 8) -> dict:
    """The next frame that is not filler. None coming is an AssertionError that says how many
    filler frames the socket DID read first, so a red run shows a live socket that never got
    the frame, not a stalled one."""
    fillers = 0
    while True:
        try:
            frame = await _next(comm, timeout)
        except asyncio.TimeoutError:
            raise AssertionError(f"no frame after {fillers} filler frame(s) within "
                                 f"{timeout}s") from None
        if frame.get("type") != "filler":
            return frame
        fillers += 1


@pytest.mark.asyncio
async def test_f04_a_callers_evicted_terminal_reaches_it_on_the_loss_pass(owner, world, tuned):
    """C subscribes to its queued call; the call times out; the bridge's terminal arm queues
    skill_timeout to C's registration and ENDS the registration; the overflow evicts it. The
    writer's loss record names the call, the loss pass re-reads the row and pushes it again,
    and C reads it after the filler. Only the loss pass can have sent it: the periodic sweep
    (and its terminal re-check) is an hour away, and the registration is gone. CONTROL: the
    frame was evicted (the queue dropped), and exactly one copy follows (the pong is next)."""
    bridge = tuned(reoffer_secs=NEVER)
    caller, target = world["caller"], world["target"]
    c = await _connect(caller)
    try:
        handle = MANAGER._by_peer[caller["peer_id"]][0]
        env = signed_call(caller, target, "s5.echo")
        call_id = env["payload"]["call_id"]
        assert (await invoke(caller, target, "s5.echo", env))["status"] == "queued"
        assert await subscribe(c, f"call:{call_id}") == []
        state = _evict_first_terminal(handle)
        before = bridge.stats()
        _commit_terminal(call_id)
        frame = await _after_filler(c)
        assert state["done"] and handle.queue.dropped >= 1, "control: the terminal was evicted"
        assert frame == {"type": "skill_timeout", "call_id": call_id}, frame
        assert not await _registered(call_id), "the registration ended at the first queueing"
        delta = _delta(before, bridge.stats(), "terminal_repush:", "reoffer_terminal:")
        assert delta == {"terminal_repush:requested": 1, "terminal_repush:sent": 1}, delta
        await _pong(c)
    finally:
        await c.disconnect()


@pytest.mark.asyncio
async def test_f04_a_delivered_terminal_is_not_sent_again_when_the_socket_later_drops_frames(
        owner, world, tuned):
    """CONTROL for the above: C reads its skill_timeout; then its queue overflows with filler
    (non-terminal frames only). The loss pass runs (counted), and nothing is re-pushed: C reads
    the surviving filler and then the pong."""
    bridge = tuned(reoffer_secs=NEVER)
    caller, target = world["caller"], world["target"]
    c = await _connect(caller)
    try:
        handle = MANAGER._by_peer[caller["peer_id"]][0]
        env = signed_call(caller, target, "s5.echo")
        call_id = env["payload"]["call_id"]
        assert (await invoke(caller, target, "s5.echo", env))["status"] == "queued"
        assert await subscribe(c, f"call:{call_id}") == []
        _commit_terminal(call_id)
        assert await _next(c) == {"type": "skill_timeout", "call_id": call_id}
        before = bridge.stats()
        for i in range(handle.queue.capacity + 5):
            handle.queue.push_nowait({"type": "filler", "n": i})
        await _until(lambda: bridge.stats().get("sweep:reoffer_loss", 0)
                     > before.get("sweep:reoffer_loss", 0), "the loss pass")
        await asyncio.sleep(0.6)
        for _ in range(handle.queue.capacity):
            assert (await _next(c))["type"] == "filler"
        await _pong(c)
        assert _delta(before, bridge.stats(), "terminal_repush:") == {}
    finally:
        await c.disconnect()
