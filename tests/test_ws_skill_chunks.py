"""Divergence 041 S7: skill_chunk (text, json), live delivery, replay, and the trim at terminal.

Design `docs/design/skill-frames.md` (fin/design-skill-frames, 2e229a6): section 7 row S7,
4.7 (the skill_chunk row, subscribe replay, stored chunk content), 4.6 (the CallBridge's chunk
arm, and the recovery sweep's part b), 4.9, 5.2 (chunk injection), the approved Q5 (no nonce
ledger on skill.chunk), Q11 (file_ref deferred) and Q15 (GET chunks keeps since=0), critique F9,
and defect 2.12.11 (a replay that skips sequence 0).

  T4   a stream: T's chunks reach C's registered socket live, in order, each one T's signed
       payload; a second socket of C subscribing after chunk 3 gets 1-3 REPLAYED before its ack,
       then 4-5 live, then the terminal frame. The stored content is the RFC 8785 bytes T
       signed, and the stored signature verifies over them with no re-canonicalisation. And the
       order: a chunk committed right after a subscriber's replay read still reaches it live,
       because the registration is proven BEFORE that read.
  T7   a duplicate sequence is refused SKILL_CHUNK_BAD `duplicate sequence`; the first stays.
  T22  a late subscriber's replay includes sequence 0 (not `reads.list_chunks`, whose
       `sequence > since` skips it); a 300-chunk stream replays at most the newest 200; a
       sequence outside the 32-bit signed range (and a negative one) is SKILL_CHUNK_BAD at
       parse, and its maximum lands.
  T5   (its chunk arm) a stranger's skill_chunk for T's running call gets the SAME code and
       message as one for a call_id nobody has; nothing is stored; the log tells them apart;
       T's own chunk lands. A call that is T's but not running is SKILL_CHUNK_NOT_RUNNING, and
       a call routed to the memory authority gets the uniform refusal.
  and  the binding (the signed call_id, sequence and chunk_type are the frame's); Q11 (file_ref
       refused); the 512 KiB content bound, at its edge; every terminal writer trims the call's
       chunks to the newest 200 in its own transaction (skill_result, skill_error, the WS
       cancel, the HTTP cancel, the deadline janitor), and a call left running keeps its own;
       the chunk arm delivers to PROVEN registrations only, from the ROW; the recovery sweep
       replays each registration's chunks after its high-water mark; a WS skill_call replays a
       chunk committed before its registration; Q15, GET chunks keeps its since=0 default.
  review  the S7 review (divergence 041, its S7 review section). H1: a running call holds at
       most MAX_CHUNKS_PER_CALL chunks, the limit told to its running target only, and a
       janitor trim that fails undoes no timeout and no other call's trim. A terminal whose
       trim fails does not commit (MX5). A suspended target's chunk, and one on a socket whose
       bound key is revoked, get the uniform refusal (MX4). A signed sequence of `true` or 1.0
       is refused (MX8). A skill_call reads its replay after registering (MX7). M1: a terminal
       committed during a replay follows the replayed chunks, on subscribe and on skill_call.
       Recovery replays the missed chunks before it delivers the terminal (MX9).

The harness is S5's and S6's (tests/test_call_delivery.py, tests/test_ws_caller_frames.py): the
production consumer, registry and bridges, DPoP ENFORCED with real proofs, HTTP writes with
proofs too. The new codes are literals.

RED: every test that sends a skill_chunk meets the `UNIMPLEMENTED` error frame and the 1011
close that frame_handlers.py answers it with before S7; a subscriber's replay and the trims are
simply absent (a subscriber hears no replay; 205 rows stay 205).

Run:  .venv/Scripts/python.exe -m pytest tests/test_ws_skill_chunks.py -q -p no:cacheprovider
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import threading
import time
import uuid

import psycopg
import pytest
from django.test import Client, override_settings

from apps.calls import chunks as call_chunks
from apps.calls import janitor
from apps.core import dbconn
from apps.crypto import primitives as P
from tests.fixtures.call_rows import call_row, seed_call
from tests.fixtures.hub_presence_db import hub_connections_clean  # noqa: F401  (autouse)
# The S5 harness. Its autouse fixtures (server_config, bridges_stopped) apply here too.
from tests.test_call_delivery import (  # noqa: F401  (fixtures are used by name)
    DSN,
    NEVER,
    UNIFORM,
    _connect,
    _delta,
    _events,
    _get,
    _invoke_frame,
    _key,
    _next,
    _pong,
    _send,
    _skill_id,
    _status,
    _token,
    _until,
    bridges_stopped,
    owner,
    server_config,
    signed_call,
    signed_result,
    tuned,
    verifies_under,
    world,
)
# The S6 harness. `registry_cleared` is autouse there and here.
from tests.test_ws_caller_frames import (  # noqa: F401
    _caller_frames,
    _commit_terminal,
    _janitor_body,
    _named,
    _registry_module,
    acked,
    call_over_ws,
    registry_cleared,
    subscribe,
)

#: The wire, as literals.
CHUNK_BAD = "SKILL_CHUNK_BAD"
CHUNK_FAILED = "SKILL_CHUNK_FAILED"
CHUNK_NOT_RUNNING = "SKILL_CHUNK_NOT_RUNNING"
MAX_CONTENT = 512 * 1024
#: The newest chunks a replay sends and a terminal keeps.
KEEP = 200
CHUNK_CHANNEL = "meshy_call_chunk"

_ABSENT = object()


# ------------------------------------------------------------------------------ chunks

def _now_z() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")


def signed_chunk(peer: dict, call_id: str, sequence: int, *, chunk_type: str = "text",
                 content=None, frame: dict | None = None, **fields) -> dict:
    """Build a signed skill_chunk frame: `{v, kind,
    call_id, target_peer_id, sequence, chunk_type, <body>, issued_at}`, no nonce, signed
    with the peer's key. `fields` set (or, as `_ABSENT`, drop) payload fields; `frame` sets the
    frame's own fields, for a frame that disagrees with what was signed."""
    key = peer["keys"][0]
    payload = {"v": 1, "kind": "skill.chunk", "call_id": call_id,
               "target_peer_id": str(peer["peer_id"]), "sequence": sequence,
               "chunk_type": chunk_type,
               "content": {"delta": f"token {sequence}"} if content is None else content,
               "issued_at": _now_z()}
    if content is _ABSENT:
        payload.pop("content")
    for name, value in fields.items():
        if value is _ABSENT:
            payload.pop(name, None)
        else:
            payload[name] = value
    out = {"type": "skill_chunk", "call_id": call_id, "sequence": sequence,
           "chunk_type": chunk_type,
           "envelope": {"payload": payload,
                        "signature": P.b64u(P.sign(key["seed"], P.jcs(payload))),
                        "signing_kid": key["kid"]}}
    out.update(frame or {})
    return out


def to_caller(frame: dict) -> dict:
    """Return the skill_chunk a caller hears for a chunk that landed."""
    return {"type": "skill_chunk", "call_id": frame["call_id"], "sequence": frame["sequence"],
            "chunk_type": frame["chunk_type"], "envelope": frame["envelope"]}


def _store(conn, frames: list[dict]) -> None:
    """Chunk rows written as another writer would write them (the bootstrap role): the
    RFC 8785 bytes of the signed payload as content. The insert trigger fires for any
    writer."""
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO skill_call_chunks (call_id, sequence, chunk_type, content, "
            "  chunk_signature, chunk_signing_kid) VALUES (%s,%s,%s,%s,%s,%s)",
            [(f["call_id"], f["sequence"], f["chunk_type"], P.jcs(f["envelope"]["payload"]),
              f["envelope"]["signature"], f["envelope"]["signing_kid"]) for f in frames])


def _seed_chunks(peer: dict, call_id: str, sequences) -> list[dict]:
    """Signed chunks of `peer` for the call, stored on a connection of their own. Returns the
    frames, in `sequences` order."""
    frames = [signed_chunk(peer, call_id, s) for s in sequences]
    with psycopg.connect(DSN, autocommit=True) as conn:
        _store(conn, frames)
    return frames


def _chunk_rows(owner, call_id: str) -> list[tuple]:
    return owner.execute("SELECT sequence, chunk_type, content, chunk_signature, "
                         "chunk_signing_kid FROM skill_call_chunks WHERE call_id = %s "
                         "ORDER BY sequence", (call_id,)).fetchall()


def _sequences(owner, call_id: str) -> list[int]:
    return [r[0] for r in _chunk_rows(owner, call_id)]


def _refusal(code: str, message: str = UNIFORM) -> dict:
    return {"type": "error", "code": code, "message": message}


def _call(owner, world: dict, status: str = "running", *, target: dict | None = None,
          deadline: _dt.datetime | None = None) -> str:
    """A call from C to `target` (T by default), written as `status` by the bootstrap role."""
    target = target or world["target"]
    return seed_call(owner, world["caller"], target, _skill_id(owner, target), status=status,
                     call_id=f"s7-{uuid.uuid4()}", deadline=deadline)


def _stream_skill(owner, target: dict, name: str = "s7.stream") -> str:
    owner.execute("INSERT INTO skills (peer_id, name, version, mode, share_mode, input_schema, "
                  "is_current) VALUES (%s,%s,'1.0.0','stream','grants_only','{}',TRUE)",
                  (target["peer_id"], name))
    return name


async def _target(peer: dict):
    """S5's `_connect`, and the invokes its connect-time redelivery queued read off: a seeded
    `routed` or `running` call is offered to its target on connect (design 4.8)."""
    comm = await _connect(peer)
    await comm.send_json_to({"type": "ping"})
    comm.redelivered = []
    while (frame := await _next(comm)) != {"type": "pong"}:
        assert frame.get("type") == "skill_invoke", f"expected a redelivered invoke: {frame}"
        comm.redelivered.append(frame["call_id"])
    return comm


async def _frames(comm, n: int) -> list[dict]:
    return [await _next(comm) for _ in range(n)]


async def _heard(bridge, before: dict, n: int) -> None:
    """The bridge has decided `n` more chunk pointers since `before`: a subscriber registered
    after this cannot also hear those chunks live (a pointer still queued would find it)."""
    await _until(lambda: bridge.stats().get("chunk:notifications", 0)
                 - before.get("chunk:notifications", 0) >= n,
                 f"{n} chunk pointers decided", timeout=30)


def _chunk_delivery_recorder(bridge, monkeypatch) -> list:
    """(call_id, sequence) of each live chunk delivery pass of `bridge`, appended when the pass
    has RETURNED (its frames are queued by then). Absent before S7, where nothing is recorded
    and the test fails on the wire."""
    passes: list = []
    original = getattr(bridge, "_deliver_chunk", None)

    async def recorded(call_id, sequence, frame):
        try:
            return await original(call_id, sequence, frame)
        finally:
            passes.append((call_id, sequence))

    if original is not None:
        monkeypatch.setattr(bridge, "_deliver_chunk", recorded)
    return passes


def _wait_for_chunk_handled(bridge, before: dict, passes: list, call_id: str, sequence: int,
                            timeout: float = 10.0) -> None:
    """Block (on a worker thread) until the bridge FINISHED with the chunk's pointer: its
    delivery pass returned, or it decided nothing here is registered for the call."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (call_id, sequence) in passes:
            return
        if bridge.stats().get("chunk:no_registration", 0) > before.get(
                "chunk:no_registration", 0):
            return
        time.sleep(0.02)
    raise AssertionError(f"the bridge never finished with chunk {sequence} of {call_id}")


# ============================================================================== T4

@pytest.mark.asyncio
async def test_t4_a_stream_reaches_its_caller_live_and_a_late_subscriber_by_replay_then_live(
        owner, world, tuned):
    """C's WS skill_call to T's stream skill is acked; T acks, runs and sends chunks 1-3, each
    landing with no reply; C's registered socket hears each as skill_chunk, in order, whose
    envelope is T's signed payload. A second socket of C subscribes: chunks 1-3 are REPLAYED
    before its ack. T sends 4 and 5: both sockets hear them live; T's result: both hear the
    terminal. The rows hold the RFC 8785 bytes T signed (A3: the stored signature verifies over
    the stored content as it is)."""
    tuned(reoffer_secs=NEVER)
    caller, target = world["caller"], world["target"]
    skill = _stream_skill(owner, target)
    t = await _connect(target)
    c1 = await _connect(caller)
    c2 = None
    try:
        env = signed_call(caller, target, skill, {"prompt": "count to five"})
        call_id = await call_over_ws(c1, env, mode="stream")
        await acked(c1, call_id)
        assert (await _invoke_frame(t))["call_id"] == call_id
        assert await _send(t, {"type": "skill_ack", "call_id": call_id}) is None
        assert await _send(t, {"type": "skill_running", "call_id": call_id}) is None
        sent = {}
        for seq in (1, 2, 3):
            sent[seq] = signed_chunk(target, call_id, seq)
            assert await _send(t, sent[seq]) is None, f"chunk {seq} was refused"
        live = await _frames(c1, 3)
        assert live == [to_caller(sent[s]) for s in (1, 2, 3)], live
        assert all(verifies_under(target, f["envelope"]) for f in live)

        c2 = await _connect(caller)
        replayed = await subscribe(c2, f"call:{call_id}")
        assert replayed == [to_caller(sent[s]) for s in (1, 2, 3)], replayed

        for seq in (4, 5):
            sent[seq] = signed_chunk(target, call_id, seq)
            assert await _send(t, sent[seq]) is None, f"chunk {seq} was refused"
        for comm in (c1, c2):
            assert await _frames(comm, 2) == [to_caller(sent[4]), to_caller(sent[5])]
        result = signed_result(target, call_id, {"counted": 5})
        assert await _send(t, result) is None
        for comm in (c1, c2):
            got = await _next(comm)
            assert (got["type"], got["call_id"], got["status"]) == (
                "skill_result", call_id, "completed"), got
            assert got["envelope"]["payload"] == result["envelope"]["payload"]
            await _pong(comm)
    finally:
        for comm in (t, c1, c2):
            if comm is not None:
                await comm.disconnect()
    rows = _chunk_rows(owner, call_id)
    assert [r[0] for r in rows] == [1, 2, 3, 4, 5]
    for seq, chunk_type, content, signature, kid in rows:
        payload = sent[seq]["envelope"]["payload"]
        assert chunk_type == "text" and kid == target["keys"][0]["kid"]
        assert bytes(content) == P.jcs(payload), f"chunk {seq}: not the bytes T signed"
        assert P.verify_strict(target["keys"][0]["public"], bytes(content),
                               P.b64u_decode(signature)), f"chunk {seq}"


@pytest.mark.asyncio
async def test_t4_a_chunk_committed_right_after_a_subscribers_replay_read_reaches_it_live(
        owner, world, tuned, monkeypatch):
    """4.7's order for the replay: register, prove the caller, THEN read the chunks. A hook on
    the subscriber's replay read (`caller_frames.read_chunks`) commits chunk 2 right after that
    read returns and waits until the bridge has finished with its pointer. The registration was
    proven before the read, so the bridge delivers chunk 2 live: C hears 0, 1 (replayed) and 2
    (live) before its ack, in whatever order the loop interleaved them (4.9: the client orders
    on sequence), each once. Read, then register (the order swapped), chunk 2 is lost."""
    bridge = tuned(reoffer_secs=NEVER)
    caller, target = world["caller"], world["target"]
    call_id = _call(owner, world)
    early = _seed_chunks(target, call_id, [0, 1])
    c = await _connect(caller)
    try:
        frames = _caller_frames()
        original = getattr(frames, "read_chunks", None)
        hooked = threading.Event()
        passes = _chunk_delivery_recorder(bridge, monkeypatch)
        late: list = []

        def hook(conn, *args, **kwargs):
            rows = original(conn, *args, **kwargs)
            if not hooked.is_set():
                before = bridge.stats()
                late.extend(_seed_chunks(target, call_id, [2]))
                _wait_for_chunk_handled(bridge, before, passes, call_id, 2)
                hooked.set()
            return rows

        if original is not None:
            monkeypatch.setattr(frames, "read_chunks", hook)
        got = await subscribe(c, f"call:{call_id}")
        assert hooked.is_set(), "the subscriber's replay never read the chunks"
        expected = [to_caller(f) for f in early + late]
        assert sorted(got, key=lambda f: f["sequence"]) == expected, got
        await _pong(c)
    finally:
        await c.disconnect()


# ============================================================================== T7

@pytest.mark.asyncio
async def test_t7_a_duplicate_sequence_is_refused_and_the_first_chunk_stays(owner, world, tuned):
    """5.2: `UNIQUE (call_id, sequence)` (sql/008). A second chunk at sequence 1 with other
    content is SKILL_CHUNK_BAD `duplicate sequence`, told to the call's own target only (a
    stranger's insert selects no row, so it never meets the constraint); the first row stays,
    byte for byte. CONTROL: sequence 2 lands after it, on the same socket connection."""
    tuned(reoffer_secs=NEVER)
    target = world["target"]
    call_id = _call(owner, world)
    t = await _target(target)
    try:
        first = signed_chunk(target, call_id, 1, content={"delta": "first"})
        assert await _send(t, first) is None
        again = signed_chunk(target, call_id, 1, content={"delta": "second"})
        assert await _send(t, again) == _refusal(CHUNK_BAD, "duplicate sequence")
        assert await _send(t, signed_chunk(target, call_id, 2)) is None       # CONTROL
    finally:
        await t.disconnect()
    rows = _chunk_rows(owner, call_id)
    assert [r[0] for r in rows] == [1, 2]
    assert bytes(rows[0][2]) == P.jcs(first["envelope"]["payload"])
    assert rows[0][3] == first["envelope"]["signature"]


# ============================================================================= T22

@pytest.mark.asyncio
async def test_t22_a_late_subscribers_replay_includes_sequence_zero(owner, world, tuned):
    """2.12.11: replay must include sequence 0, which report_chunk accepts. T's chunks 0, 1, 2 land; C subscribes late and hears all three,
    0 first, before its ack."""
    bridge = tuned(reoffer_secs=NEVER)
    caller, target = world["caller"], world["target"]
    call_id = _call(owner, world)
    t = await _target(target)
    c = await _connect(caller)
    try:
        sent = [signed_chunk(target, call_id, s) for s in (0, 1, 2)]
        before = bridge.stats()
        for frame in sent:
            assert await _send(t, frame) is None
        await _heard(bridge, before, 3)
        assert await subscribe(c, f"call:{call_id}") == [to_caller(f) for f in sent]
    finally:
        await t.disconnect()
        await c.disconnect()


@pytest.mark.asyncio
async def test_t22_a_300_chunk_stream_replays_the_newest_200_and_is_trimmed_to_them(
        owner, world, tuned):
    """F9 and 5.4: a replay is at most 200 rows, for a running call as for a terminal one. T
    sends 300 chunks (0-299), none refused; a late subscriber hears exactly 100-299, in order,
    then its ack. T's result then trims the call's rows to those 200 (5.2, in its own
    transaction), and a subscriber after the terminal hears the same 200, then the terminal."""
    bridge = tuned(reoffer_secs=NEVER)
    caller, target = world["caller"], world["target"]
    call_id = _call(owner, world)
    t = await _target(target)
    c = await _connect(caller)
    c2 = None
    try:
        sent = [signed_chunk(target, call_id, s) for s in range(300)]
        before = bridge.stats()
        for frame in sent:
            await t.send_json_to(frame)
        await _pong(t)                                       # none of the 300 was refused
        assert _sequences(owner, call_id) == list(range(300))
        await _heard(bridge, before, 300)
        got = await subscribe(c, f"call:{call_id}")
        assert [f["sequence"] for f in got] == list(range(300 - KEEP, 300))
        assert got == [to_caller(f) for f in sent[300 - KEEP:]]
        assert await _send(t, signed_result(target, call_id)) is None
        assert (await _next(c))["type"] == "skill_result"
        assert _sequences(owner, call_id) == list(range(100, 300))
        c2 = await _connect(caller)
        got = await subscribe(c2, f"call:{call_id}")
        assert [f.get("sequence") for f in got] == list(range(100, 300)) + [None], [
            f.get("sequence") for f in got]
        assert got[-1]["type"] == "skill_result"
    finally:
        for comm in (t, c, c2):
            if comm is not None:
                await comm.disconnect()


@pytest.mark.asyncio
async def test_t22_a_sequence_outside_i32_is_refused_at_parse_and_i32s_max_lands(
        owner, world, tuned):
    """The signed sequence and INTEGER column are limited to a 32-bit signed integer:
    2147483648 is SKILL_CHUNK_BAD at parse, not a database error; so is -1. CONTROL:
    2147483647 lands."""
    tuned(reoffer_secs=NEVER)
    target = world["target"]
    call_id = _call(owner, world)
    t = await _target(target)
    try:
        for bad in (2147483648, -1):
            assert await _send(t, signed_chunk(target, call_id, bad)) == _refusal(
                CHUNK_BAD, f"sequence must be between 0 and 2147483647, got {bad}")
        assert await _send(t, signed_chunk(target, call_id, 2147483647)) is None
    finally:
        await t.disconnect()
    assert _sequences(owner, call_id) == [2147483647]


# ============================================================================== T5

@pytest.mark.asyncio
async def test_t5_a_strangers_chunk_gets_the_unknown_calls_refusal_and_stores_nothing(
        owner, world, tuned, caplog):
    """2.12.1 and 5.2: X signs a chunk for T's running call (as X: it verifies, and it binds to
    the frame), and one for a call_id nobody has. Both get ONE refusal, byte for byte; only the
    INSERT's `target_peer_id = me` stops the first. Nothing is stored, the call row does not
    move, and only the server log tells the two apart. CONTROL: T's own chunk lands."""
    caplog.set_level(logging.INFO)
    tuned(reoffer_secs=NEVER)
    target, stranger = world["target"], world["other"]
    call_id = _call(owner, world)
    unknown = f"s7-unknown-{uuid.uuid4()}"
    before = call_row(owner, call_id)
    t = await _target(target)
    x = await _connect(stranger)
    try:
        on_theirs = await _send(x, signed_chunk(stranger, call_id, 0))
        on_nobodys = await _send(x, signed_chunk(stranger, unknown, 0))
        assert on_theirs == _refusal(CHUNK_FAILED), on_theirs
        assert on_nobodys == on_theirs, "a stranger could tell T's call from no call"
        assert _sequences(owner, call_id) == [] and call_row(owner, call_id) == before
        logged = [r.getMessage() for r in caplog.records]
        mine = [m for m in logged if "skill_chunk" in m and unknown in m]
        theirs = [m for m in logged if "skill_chunk" in m and call_id in m]
        assert mine and "no such call" in mine[-1], mine
        assert theirs and "another peer" in theirs[-1], theirs
        assert await _send(t, signed_chunk(target, call_id, 0)) is None          # CONTROL
    finally:
        await t.disconnect()
        await x.disconnect()
    assert _sequences(owner, call_id) == [0]


@pytest.mark.asyncio
async def test_a_chunk_for_its_targets_call_that_is_not_running_is_refused_not_running(
        owner, world, tuned):
    """Once ownership is proven, T's chunk for its `routed` call and for its `completed`
    call is SKILL_CHUNK_NOT_RUNNING with the state-transition wording asserted below, and stores
    nothing. CONTROL: its running call's chunk lands."""
    tuned(reoffer_secs=NEVER)
    target = world["target"]
    routed, done, on_run = (_call(owner, world, s) for s in ("routed", "completed", "running"))
    t = await _target(target)
    try:
        assert await _send(t, signed_chunk(target, routed, 0)) == _refusal(
            CHUNK_NOT_RUNNING, "state transition: cannot move from Routed to one of [Running]")
        assert await _send(t, signed_chunk(target, done, 0)) == _refusal(
            CHUNK_NOT_RUNNING,
            "state transition: cannot move from Completed to one of [Running]")
        assert await _send(t, signed_chunk(target, on_run, 0)) is None            # CONTROL
    finally:
        await t.disconnect()
    assert (_sequences(owner, routed), _sequences(owner, done)) == ([], [])
    assert _sequences(owner, on_run) == [0]


@pytest.mark.asyncio
async def test_a_chunk_for_a_call_routed_to_the_memory_authority_gets_the_uniform_refusal(
        owner, world, tuned):
    """4.4: a memory call runs in-process; no socket writes to it, chunks included. With the
    setting naming U as the authority, U's chunk for its running call is the uniform refusal
    and stores nothing. CONTROL: with the setting naming another peer, the same socket's chunk
    lands."""
    tuned(reoffer_secs=NEVER)
    u = world["other"]
    call_id = _call(owner, world, target=u)
    a = await _target(u)
    try:
        with override_settings(MESHY_MEMORY_PEER_ID=str(u["peer_id"])):
            assert await _send(a, signed_chunk(u, call_id, 0)) == _refusal(CHUNK_FAILED)
            assert _sequences(owner, call_id) == []
        with override_settings(MESHY_MEMORY_PEER_ID=str(uuid.uuid4())):
            assert await _send(a, signed_chunk(u, call_id, 0)) is None            # CONTROL
    finally:
        await a.disconnect()
    assert _sequences(owner, call_id) == [0]


# ================================================================== the binding, Q11, 512 KiB

@pytest.mark.asyncio
async def test_a_chunk_is_bound_to_its_frames_call_id_sequence_and_chunk_type(
        owner, world, tuned):
    """5.2, "the target itself cannot place a chunk signed for call A or sequence N into call B
    or sequence M" (2.12.2's chunk half): a frame whose call_id, sequence or chunk_type is not
    the signed one is SKILL_CHUNK_BAD, and neither call gets a row. CONTROL: each same
    envelope, sent under the frame fields it was signed with, lands."""
    tuned(reoffer_secs=NEVER)
    target = world["target"]
    a_call, b_call = _call(owner, world), _call(owner, world)
    t = await _target(target)
    try:
        cases = [
            (signed_chunk(target, a_call, 0), {"call_id": b_call},
             "the signed call_id is not this frame's call_id"),
            (signed_chunk(target, a_call, 1), {"sequence": 2},
             "the signed sequence is not this frame's sequence"),
            (signed_chunk(target, a_call, 2, chunk_type="json"), {"chunk_type": "text"},
             "the signed chunk_type is not this frame's chunk_type"),
        ]
        for signed, lie, message in cases:
            forged = dict(signed, **lie)
            assert await _send(t, forged) == _refusal(CHUNK_BAD, message), lie
            assert (_sequences(owner, a_call), _sequences(owner, b_call)) == ([], []), lie
        for signed, _lie, _message in cases:                                     # CONTROL
            assert await _send(t, signed) is None
    finally:
        await t.disconnect()
    assert _sequences(owner, a_call) == [0, 1, 2] and _sequences(owner, b_call) == []


@pytest.mark.asyncio
async def test_q11_a_file_ref_chunk_is_refused_bad_and_stores_nothing(owner, world, tuned):
    """Q11: file_ref chunks are deferred because they need apps/files' ownership and `ready`
    checks. A signed file_ref chunk is SKILL_CHUNK_BAD and stores nothing.
    CONTROL: a text chunk and a json chunk land."""
    tuned(reoffer_secs=NEVER)
    target = world["target"]
    call_id = _call(owner, world)
    t = await _target(target)
    try:
        ref = signed_chunk(target, call_id, 0, chunk_type="file_ref", content=_ABSENT,
                           chunk={"file_artifact_id": str(uuid.uuid4())})
        assert await _send(t, ref) == _refusal(
            CHUNK_BAD, "file_ref chunks are not accepted by this server yet (design Q11)")
        assert _sequences(owner, call_id) == []
        assert await _send(t, signed_chunk(target, call_id, 0)) is None          # CONTROL
        assert await _send(t, signed_chunk(target, call_id, 1, chunk_type="json",
                                           content={"k": [1, 2]})) is None
    finally:
        await t.disconnect()
    assert [r[:2] for r in _chunk_rows(owner, call_id)] == [(0, "text"), (1, "json")]


@pytest.mark.asyncio
async def test_chunk_content_is_bounded_at_512_kib_of_its_stored_bytes(owner, world, tuned):
    """Inline content is at most 512 KiB. The bound is on the bytes stored, the payload's
    RFC 8785 form: exactly 524288 lands, 524289 is SKILL_CHUNK_BAD with the asserted wire
    wording and stores nothing."""
    tuned(reoffer_secs=NEVER)
    target = world["target"]
    call_id = _call(owner, world)

    def sized(sequence: int, size: int) -> dict:
        at = _now_z()
        empty = len(P.jcs(signed_chunk(target, call_id, sequence, content="",
                                       issued_at=at)["envelope"]["payload"]))
        frame = signed_chunk(target, call_id, sequence, content="x" * (size - empty),
                             issued_at=at)
        assert len(P.jcs(frame["envelope"]["payload"])) == size
        return frame

    t = await _target(target)
    try:
        assert await _send(t, sized(0, MAX_CONTENT + 1)) == _refusal(
            CHUNK_BAD, f"content {MAX_CONTENT + 1} bytes exceeds max {MAX_CONTENT}")
        assert _sequences(owner, call_id) == []
        assert await _send(t, sized(1, MAX_CONTENT)) is None                     # CONTROL
    finally:
        await t.disconnect()
    assert _sequences(owner, call_id) == [1]


# ===================================================================== the trim at terminal

def _http_cancel(caller: dict, call_id: str):
    path = f"/v1/calls/{call_id}/cancel"
    token = _token(caller)
    return Client().post(path, data=json.dumps({"reason": "s7"}),
                         content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {token}",
                         HTTP_DPOP=P.dpop_wire(caller["keys"][0]["seed"], "POST", path, token))


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ["skill_result", "skill_error", "skill_cancel",
                                    "http_cancel", "janitor"])
async def test_every_terminal_writer_trims_the_calls_chunks_to_the_newest_200(
        owner, world, tuned, writer):
    """5.2: retention keeps the newest 200 chunks once a call is terminal. The trim runs IN
    the terminal transaction with sql/008's `trim_chunks_for_call(call_id, 200)`. Each terminal writer of a running
    call: the target's result and error, the caller's WS cancel (`cancel_by_caller`), the HTTP
    cancel and the deadline janitor. 205 rows (0-204) become the newest 200 (5-204).
    CONTROL: before the terminal the call holds 205, and a second running call's 205 stay."""
    tuned(reoffer_secs=NEVER)
    caller, target = world["caller"], world["target"]
    past = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=1)
    call_id = _call(owner, world, deadline=past if writer == "janitor" else None)
    other = _call(owner, world)
    with psycopg.connect(DSN, autocommit=True) as conn:
        _store(conn, [signed_chunk(target, c, s) for c in (call_id, other) for s in range(205)])
    assert _sequences(owner, call_id) == list(range(205))                        # CONTROL
    if writer in ("skill_result", "skill_error"):
        t = await _target(target)
        try:
            frame = (signed_result(target, call_id) if writer == "skill_result" else
                     {"type": "skill_error", "call_id": call_id, "error_code": "E_X",
                      "error_message": "stopped"})
            assert await _send(t, frame) is None
        finally:
            await t.disconnect()
    elif writer == "skill_cancel":
        c = await _connect(caller)
        try:
            assert await _send(c, {"type": "skill_cancel", "call_id": call_id}) is None
        finally:
            await c.disconnect()
    elif writer == "http_cancel":
        response = await asyncio.to_thread(_http_cancel, caller, call_id)
        assert response.status_code == 200, (response.status_code, response.content[:300])
    else:
        await asyncio.to_thread(_janitor_body)
    assert _status(owner, call_id) in ("completed", "failed", "abandoned", "timeout")
    assert _sequences(owner, call_id) == list(range(5, 205)), writer
    assert _sequences(owner, other) == list(range(205)), "control: a running call keeps its own"


# ================================================================ the CallBridge's chunk arm

@pytest.mark.asyncio
async def test_a_chunk_reaches_a_proven_registration_and_never_a_provisional_one(
        owner, world, tuned, monkeypatch):
    """5.1: chunk frames go to sockets that PROVED caller ownership. A stranger's subscribe
    registers first, provisionally, then reads; a chunk committed while that read runs finds
    the call registered (C's socket is, proven), and the hook waits until the bridge's delivery
    pass has RETURNED, so the stranger's registration was provisional when it ran. C hears the
    chunk; the stranger hears nothing, then its ack."""
    bridge = tuned(reoffer_secs=NEVER)
    caller, target, stranger = world["caller"], world["target"], world["other"]
    call_id = _call(owner, world)
    c = await _connect(caller)
    x = await _connect(stranger)
    try:
        assert await subscribe(c, f"call:{call_id}") == []
        frames = _caller_frames()
        original = frames.read_call
        hooked = threading.Event()
        passes = _chunk_delivery_recorder(bridge, monkeypatch)
        late: list = []

        def hook(conn, *args, **kwargs):
            row = original(conn, *args, **kwargs)
            if _named(args, kwargs) == call_id and not hooked.is_set():
                before = bridge.stats()
                late.extend(_seed_chunks(target, call_id, [0]))
                _wait_for_chunk_handled(bridge, before, passes, call_id, 0)
                hooked.set()
            return row

        monkeypatch.setattr(frames, "read_call", hook)
        assert await subscribe(x, f"call:{call_id}") == []
        assert hooked.is_set()
        assert await _next(c) == to_caller(late[0])
        await _pong(x)
    finally:
        await c.disconnect()
        await x.disconnect()


@pytest.mark.asyncio
async def test_a_forged_chunk_pointer_delivers_only_a_real_row_to_its_caller(
        owner, world, tuned):
    """Pointers only (4.6, 5.4): the chunk arm reads the ROW by (call_id, sequence). A forged
    pointer naming C's registered call at a sequence with no row sends nothing
    (`chunk:row_missing`); one naming another caller's call is not read
    (`chunk:no_registration`); one naming a real row sends that row again, to C (at most a
    duplicate, 4.9)."""
    bridge = tuned(reoffer_secs=NEVER)
    caller, target = world["caller"], world["target"]
    call_id = _call(owner, world)
    theirs = seed_call(owner, world["other"], target, _skill_id(owner, target),
                       status="running", call_id=f"s7-{uuid.uuid4()}")
    stored = _seed_chunks(target, call_id, [0])
    _seed_chunks(target, theirs, [0])
    c = await _connect(caller)
    try:
        assert await subscribe(c, f"call:{call_id}") == [to_caller(stored[0])]
        before = bridge.stats()
        for pointer in ({"call_id": call_id, "sequence": 7},
                        {"call_id": theirs, "sequence": 0}):
            owner.execute("SELECT pg_notify(%s, %s)", (CHUNK_CHANNEL, json.dumps(pointer)))
        expected = {"chunk:notifications": 2, "chunk:registered": 1, "chunk:row_missing": 1,
                    "chunk:no_registration": 1}
        await _until(lambda: _delta(before, bridge.stats(), "chunk:") == expected,
                     "both forgeries decided, and the row read for the registered one")
        await _pong(c)
        assert _delta(before, bridge.stats(), "chunk:") == expected
        owner.execute("SELECT pg_notify(%s, %s)",
                      (CHUNK_CHANNEL, json.dumps({"call_id": call_id, "sequence": 0})))
        assert await _next(c) == to_caller(stored[0])
    finally:
        await c.disconnect()


@pytest.mark.asyncio
async def test_recovery_replays_each_registrations_chunks_after_its_high_water_mark(
        owner, world, tuned, monkeypatch):
    """4.6, the recovery sweep's part b: a chunk committed while the bridge's LISTEN connection
    was down was never heard. C's registration heard chunk 0 by its replay and chunk 1 live, so
    its high-water mark is 1; chunk 2 commits while LISTEN is down and reaches nobody. Released,
    the sweep runs before listening is reported and sends C chunk 2 alone, not 0 or 1 again."""
    bridge = tuned(reoffer_secs=NEVER, beat_secs=NEVER)
    caller, target = world["caller"], world["target"]
    call_id = _call(owner, world)
    first = _seed_chunks(target, call_id, [0])
    c = await _connect(caller)
    down, entered, release = threading.Event(), threading.Event(), threading.Event()
    original = bridge._run_once

    def held(*args, **kwargs):
        if down.is_set():
            entered.set()
            release.wait(20)
        return original(*args, **kwargs)

    monkeypatch.setattr(bridge, "_run_once", held)
    try:
        assert await subscribe(c, f"call:{call_id}") == [to_caller(first[0])]
        live = _seed_chunks(target, call_id, [1])
        assert await _next(c) == to_caller(live[0])
        down.set()
        pids = owner.execute("SELECT pid FROM pg_stat_activity WHERE datname = "
                             "current_database() AND query = 'LISTEN meshy_call_chunk'"
                             ).fetchall()
        assert len(pids) == 1, pids
        owner.execute("SELECT pg_terminate_backend(%s)", (pids[0][0],))
        assert await asyncio.to_thread(entered.wait, 15), "the bridge did not reconnect"
        assert not bridge.is_listening()
        missed = _seed_chunks(target, call_id, [2])
        await asyncio.sleep(0.5)
        await _pong(c)

        before = bridge.stats()
        down.clear()
        release.set()
        assert await bridge.wait_listening(15)
        assert await _next(c) == to_caller(missed[0])
        await _pong(c)
        assert _delta(before, bridge.stats(), "recovery_chunk:") == {
            "recovery_chunk:sent": 1}, "the frame came from the recovery sweep"
    finally:
        release.set()
        await c.disconnect()


@pytest.mark.asyncio
async def test_a_ws_skill_call_replays_a_chunk_committed_before_its_registration(
        owner, world, tuned, monkeypatch):
    """The registration path of a WS skill_call (4.7: register, ack, re-read) replays the
    call's chunks too. A chunk committed after start_call returned the call in flight and before
    the handler registered it (a hook on `caller_frames._start_in_thread`, standing in for a
    fast target: the row made running and chunk 0 stored) finds no registration
    (`chunk:no_registration`); only the handler's replay can deliver it: C gets skill_acked,
    then skill_chunk 0. CONTROL: a plain call is acked and nothing follows."""
    bridge = tuned(reoffer_secs=NEVER)
    caller, target = world["caller"], world["target"]
    frames = _caller_frames()
    original = frames._start_in_thread
    stored: list = []

    def then_chunk(*args, **kwargs):
        result, row = original(*args, **kwargs)
        before = bridge.stats()
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("UPDATE skill_calls SET status = 'running', started_at = now() "
                         "WHERE call_id = %s", (result.call_id,))
        stored.extend(_seed_chunks(target, result.call_id, [0]))
        _wait_for_chunk_handled(bridge, before, [], result.call_id, 0)
        return result, row

    c = await _connect(caller)
    try:
        await acked(c, await call_over_ws(c, signed_call(caller, target, "s5.echo")))
        await _pong(c)                                                           # CONTROL
        monkeypatch.setattr(frames, "_start_in_thread", then_chunk)
        before = bridge.stats()
        call_id = await call_over_ws(c, signed_call(caller, target, "s5.echo"))
        await acked(c, call_id)
        assert await _next(c) == to_caller(stored[0])
        assert _delta(before, bridge.stats(), "chunk:no_registration") == {
            "chunk:no_registration": 1}
        await _pong(c)
    finally:
        await c.disconnect()


# ============================================================================== Q15

@pytest.mark.asyncio
async def test_q15_get_chunks_keeps_its_since_0_default_and_since_minus_1_includes_0(
        owner, world):
    """Q15: `GET /v1/calls/{id}/chunks` keeps its wire default, `since=0`, which skips a
    chunk at sequence 0 (2.12.11); `since=-1` includes it. A pin: the default is a contract,
    and the WS replay (not this route) is what S7 fixes."""
    caller, target = world["caller"], world["target"]
    call_id = _call(owner, world)
    _seed_chunks(target, call_id, [0, 1, 2])
    path = f"/v1/calls/{call_id}/chunks"
    default = await asyncio.to_thread(_get, path, caller)
    assert [c["sequence"] for c in default] == [1, 2]
    everything = await asyncio.to_thread(_get, path + "?since=-1", caller)
    assert [c["sequence"] for c in everything] == [0, 1, 2]


# ================================================== the S7 review: bounds, atomicity, orders

def _decided(bridge, before: dict, n: int, timeout: float = 10.0) -> None:
    """Block (on a worker thread) until the bridge has decided `n` more chunk pointers."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (bridge.stats().get("chunk:notifications", 0)
                - before.get("chunk:notifications", 0)) >= n:
            return
        time.sleep(0.02)
    raise AssertionError(f"the bridge never decided {n} chunk pointers")


def _terminal_handled(bridge, before: dict, held_before: dict, timeout: float = 10.0) -> None:
    """Block (on a worker thread) until the bridge's terminal arm has handled the pointer: it
    wrote the terminal to a registration (`terminal:sent`), or held it for one whose replay is
    running (the registry's `replay:terminal_held`, S7 review M1). Either ends the wait, so the
    test reads the order the caller hears on the tree before the fix and after it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (bridge.stats().get("terminal:sent", 0) > before.get("terminal:sent", 0)
                or bridge.registry.stats().get("replay:terminal_held", 0)
                > held_before.get("replay:terminal_held", 0)):
            return
        time.sleep(0.02)
    raise AssertionError("the bridge never handled the terminal pointer")


def _terminal_type(frame: dict) -> tuple:
    return frame.get("type"), frame.get("call_id")


@pytest.mark.asyncio
async def test_review_h1_a_running_call_holds_at_most_the_chunk_cap_told_to_its_target_only(
        owner, world, tuned, monkeypatch):
    """S7 review H1: nothing bounded how many chunks one running call holds (5.2 bounds each
    chunk's size, not their count), so the trim at terminal, inside its writer's transaction
    and under that connection's statement timeout, had no bound either: 400k rows took 5.9 to
    10.1 s on HQD, past the socket's 5 s and the janitor's 10 s. `MAX_CHUNKS_PER_CALL` (10000 in
    production, 3 here) bounds it at the insert. T's call holds 2; T's chunk 2 lands (the
    third); T's chunk 3 is SKILL_CHUNK_BAD with the limit text and stores nothing. The limit
    is told to the call's RUNNING TARGET only: a stranger's chunk for the same full call gets
    the uniform refusal, and T's chunk for its own full, completed call is
    SKILL_CHUNK_NOT_RUNNING. CONTROL: T's chunk for its other running call lands."""
    monkeypatch.setattr(call_chunks, "MAX_CHUNKS_PER_CALL", 3, raising=False)
    tuned(reoffer_secs=NEVER)
    target, stranger = world["target"], world["other"]
    call_id, other = _call(owner, world), _call(owner, world)
    done = _call(owner, world, "completed")
    _seed_chunks(target, call_id, [0, 1])
    _seed_chunks(target, done, [0, 1, 2])
    t = await _target(target)
    x = await _connect(stranger)
    try:
        assert await _send(t, signed_chunk(target, call_id, 2)) is None          # CONTROL
        assert await _send(t, signed_chunk(target, call_id, 3)) == _refusal(
            CHUNK_BAD, "chunk limit reached: a call holds at most 3 chunks")
        assert await _send(x, signed_chunk(stranger, call_id, 3)) == _refusal(CHUNK_FAILED)
        assert await _send(t, signed_chunk(target, done, 3)) == _refusal(
            CHUNK_NOT_RUNNING,
            "state transition: cannot move from Completed to one of [Running]")
        assert await _send(t, signed_chunk(target, other, 0)) is None            # CONTROL
    finally:
        await t.disconnect()
        await x.disconnect()
    assert _sequences(owner, call_id) == [0, 1, 2]
    assert (_sequences(owner, done), _sequences(owner, other)) == ([0, 1, 2], [0])


def test_review_h1_the_production_chunk_cap_is_10000():
    """A pin on the number the H1 test runs at 3: at it, a terminal's trim deletes at most 9800
    rows, measured at 97 to 112 ms on HQD's dev Postgres (divergence 041, the S7 review)."""
    assert getattr(call_chunks, "MAX_CHUNKS_PER_CALL", None) == 10_000


def test_review_h1_a_janitor_trim_that_fails_undoes_no_timeout_and_no_other_calls_trim(
        owner, world, caplog):
    """S7 review H1: the janitor trimmed its whole batch in ONE statement, in the transaction of
    the batch's timeouts, under the job's statement timeout, so one call whose trim could not
    finish rolled back every timeout of the batch; the batch is taken oldest deadline first, so
    the same call headed every tick and no call timed out again (the review's probe I). Now
    each swept call is trimmed in a statement of its own under a SAVEPOINT: a trim that fails is
    rolled back to it and logged, and the timeouts and the other calls' trims commit. Three
    calls past their deadline hold 205 chunks each; another connection holds a row lock on the
    oldest chunk of the middle one, so its trim waits past the sweep connection's statement
    timeout (1 s here) and is cancelled, a deterministic stand-in for a trim too long to
    finish. All three are swept and `timeout`; the other two hold their newest 200; the
    blocked one keeps its 205, and the log names it. CONTROL: all three ran with 205 rows
    before the sweep, and once the lock is gone the blocked call trims."""
    caplog.set_level(logging.WARNING)
    target = world["target"]
    past = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=5)
    calls = [_call(owner, world, deadline=past + _dt.timedelta(seconds=i)) for i in range(3)]
    with psycopg.connect(DSN, autocommit=True) as conn:
        _store(conn, [signed_chunk(target, c, s) for c in calls for s in range(205)])
    assert [_sequences(owner, c) for c in calls] == [list(range(205))] * 3        # CONTROL
    blocked = calls[1]
    blocker = psycopg.connect(DSN)
    try:
        assert blocker.execute("SELECT 1 FROM skill_call_chunks WHERE call_id = %s AND "
                               "sequence = 0 FOR UPDATE", (blocked,)).fetchall() == [(1,)]
        conn = dbconn.connect(statement_timeout_ms=1000)
        try:
            swept = janitor.sweep_expired_deadlines(conn, limit=1000)
        finally:
            conn.close()
    finally:
        blocker.rollback()
        blocker.close()
    assert set(calls) <= set(swept), swept
    assert [_status(owner, c) for c in calls] == ["timeout"] * 3
    assert _sequences(owner, calls[0]) == list(range(5, 205))
    assert _sequences(owner, calls[2]) == list(range(5, 205))
    assert _sequences(owner, blocked) == list(range(205)), "its trim was rolled back, alone"
    assert any(blocked in r.getMessage() and "trim" in r.getMessage()
               for r in caplog.records if r.levelno >= logging.WARNING), \
        [r.getMessage() for r in caplog.records]
    with psycopg.connect(DSN, autocommit=True) as conn:                          # CONTROL
        conn.execute("SELECT trim_chunks_for_call(%s, 200)", (blocked,))
    assert _sequences(owner, blocked) == list(range(5, 205))


def _raise_trim(*args, **kwargs):
    raise RuntimeError("the trim failed (a test stand-in)")


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ["skill_result", "skill_error", "skill_cancel",
                                    "http_cancel"])
async def test_review_a_terminal_whose_trim_fails_does_not_commit(
        owner, world, tuned, monkeypatch, writer):
    """5.2 as the build states it: each of these writers trims INSIDE the transaction of its
    terminal edge, so a terminal never commits without its trim. The review found it untested:
    skill_result's trim moved after its commit survived (MX5). The trim made to fail (it
    raises): the writer answers its internal error, and the call is still `running`, its row
    and events unchanged, its 205 chunks all there. CONTROL: the trim restored, the same writer
    ends the call and it holds its newest 200."""
    tuned(reoffer_secs=NEVER)
    caller, target = world["caller"], world["target"]
    call_id = _call(owner, world)
    with psycopg.connect(DSN, autocommit=True) as conn:
        _store(conn, [signed_chunk(target, call_id, s) for s in range(205)])
    before = (call_row(owner, call_id), _events(owner, call_id))

    async def end_it():
        if writer in ("skill_result", "skill_error"):
            t = await _target(target)
            try:
                return await _send(t, signed_result(target, call_id)
                                   if writer == "skill_result" else
                                   {"type": "skill_error", "call_id": call_id,
                                    "error_code": "E_X", "error_message": "stopped"})
            finally:
                await t.disconnect()
        if writer == "skill_cancel":
            c = await _connect(caller)
            try:
                return await _send(c, {"type": "skill_cancel", "call_id": call_id})
            finally:
                await c.disconnect()
        return (await asyncio.to_thread(_http_cancel, caller, call_id)).status_code

    monkeypatch.setattr(call_chunks, "trim", _raise_trim)
    refused = await end_it()
    if writer == "http_cancel":
        assert refused == 500, refused
    else:
        assert (refused["type"], refused["message"]) == ("error", "internal error"), refused
    assert (call_row(owner, call_id), _events(owner, call_id)) == before, writer
    assert _sequences(owner, call_id) == list(range(205))
    monkeypatch.undo()                                                           # CONTROL
    tuned(reoffer_secs=NEVER)
    assert await end_it() in (None, 200)
    assert _status(owner, call_id) in ("completed", "failed", "abandoned")
    assert _sequences(owner, call_id) == list(range(5, 205))


@pytest.mark.asyncio
@pytest.mark.parametrize("standing", ["suspended", "deleted"])
async def test_review_a_suspended_targets_chunk_gets_the_uniform_refusal(
        owner, world, tuned, standing):
    """R12 and S4's T9 for skill_chunk (the review's MX4: the chunk statement without its active
    and live-key clauses survived). A suspension is plain SQL, and the socket stays open until
    030's sweep; in that window the statement's active clause refuses T's chunk for its running
    call with the uniform refusal, and nothing is stored. CONTROL: reactivated, the same
    socket's chunk lands."""
    tuned(reoffer_secs=NEVER)
    target = world["target"]
    call_id = _call(owner, world)
    t = await _target(target)
    try:
        owner.execute("UPDATE peers SET status = %s WHERE id = %s",
                      (standing, target["peer_id"]))
        try:
            refused = await _send(t, signed_chunk(target, call_id, 0))
        finally:
            owner.execute("UPDATE peers SET status = 'active' WHERE id = %s",
                          (target["peer_id"],))
        assert refused == _refusal(CHUNK_FAILED), refused
        assert _sequences(owner, call_id) == []
        assert await _send(t, signed_chunk(target, call_id, 1)) is None          # CONTROL
    finally:
        await t.disconnect()
    assert _sequences(owner, call_id) == [1]


@pytest.mark.asyncio
async def test_review_a_chunk_on_a_socket_whose_bound_key_is_revoked_gets_the_uniform_refusal(
        owner, world, tuned):
    """Critique F4 and R12 for skill_chunk (MX4's other half): T's socket is bound to its first
    key, which is revoked by SQL; the chunk is signed with T's SECOND, live key, so
    verification passes and only the live-key clause refuses it: the uniform refusal, nothing
    stored. CONTROL: the bound key live again, the same chunk lands on the same socket."""
    tuned(reoffer_secs=NEVER)
    target = world["target"]
    bound = target["keys"][0]
    second = dict(target, keys=[_key(owner, target["peer_id"])])
    call_id = _call(owner, world)
    t = await _target(target)
    try:
        owner.execute("UPDATE peer_keys SET active = FALSE, revoked_at = NOW() "
                      "WHERE peer_id = %s AND kid = %s", (target["peer_id"], bound["kid"]))
        frame = signed_chunk(second, call_id, 0)
        try:
            refused = await _send(t, frame)
        finally:
            owner.execute("UPDATE peer_keys SET active = TRUE, revoked_at = NULL "
                          "WHERE peer_id = %s AND kid = %s", (target["peer_id"], bound["kid"]))
        assert refused == _refusal(CHUNK_FAILED), refused
        assert _sequences(owner, call_id) == []
        assert await _send(t, frame) is None                                     # CONTROL
    finally:
        await t.disconnect()
    assert _sequences(owner, call_id) == [0]


@pytest.mark.asyncio
async def test_review_a_signed_sequence_that_only_compares_equal_is_refused(
        owner, world, tuned):
    """The binding compares the signed sequence as an integer (the review's MX8: without the
    type check it survived; its probe E): a frame at sequence 1 whose envelope signs `true`, or
    `1.0`, is SKILL_CHUNK_BAD, and nothing is stored. CONTROL: the envelope signing 1 lands."""
    tuned(reoffer_secs=NEVER)
    target = world["target"]
    key = target["keys"][0]
    call_id = _call(owner, world)
    t = await _target(target)
    try:
        for lie in (True, 1.0):
            frame = signed_chunk(target, call_id, 1)
            payload = dict(frame["envelope"]["payload"], sequence=lie)
            frame["envelope"] = {"payload": payload,
                                 "signature": P.b64u(P.sign(key["seed"], P.jcs(payload))),
                                 "signing_kid": key["kid"]}
            assert await _send(t, frame) == _refusal(
                CHUNK_BAD, "the signed sequence is not this frame's sequence"), lie
            assert _sequences(owner, call_id) == [], lie
        assert await _send(t, signed_chunk(target, call_id, 1)) is None          # CONTROL
    finally:
        await t.disconnect()
    assert _sequences(owner, call_id) == [1]


@pytest.mark.asyncio
async def test_review_a_ws_skill_call_reads_its_replay_after_registering(
        owner, world, tuned, monkeypatch):
    """4.7's order for a skill_call's registration path: register, ack, THEN read the chunks
    (the review's MX7, the read moved before the registration, survived). A hook on
    `_start_in_thread` makes the call running; a hook on the replay read
    (`caller_frames.read_chunks`) commits chunk 0 right AFTER that read and waits until the
    bridge has finished with its pointer. Registered before the read, C hears skill_acked, then
    chunk 0 live, then nothing. Read before registering, the pointer finds no registration and
    the chunk is lost."""
    bridge = tuned(reoffer_secs=NEVER)
    caller, target = world["caller"], world["target"]
    frames = _caller_frames()
    original_start, original_read = frames._start_in_thread, frames.read_chunks
    passes = _chunk_delivery_recorder(bridge, monkeypatch)
    hooked = threading.Event()
    late: list = []

    def running(*args, **kwargs):
        result, row = original_start(*args, **kwargs)
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("UPDATE skill_calls SET status = 'running', started_at = now() "
                         "WHERE call_id = %s", (result.call_id,))
        return result, row

    def hook(conn, *args, **kwargs):
        rows = original_read(conn, *args, **kwargs)
        call_id = kwargs.get("call_id", args[0] if args else None)
        if not hooked.is_set():
            # Set before the commit: the live frame can reach C before this hook returns.
            hooked.set()
            before = bridge.stats()
            late.append(signed_chunk(target, call_id, 0))
            with psycopg.connect(DSN, autocommit=True) as conn:
                _store(conn, late)
            _wait_for_chunk_handled(bridge, before, passes, call_id, 0)
        return rows

    monkeypatch.setattr(frames, "_start_in_thread", running)
    monkeypatch.setattr(frames, "read_chunks", hook)
    c = await _connect(caller)
    try:
        call_id = await call_over_ws(c, signed_call(caller, target, "s5.echo"))
        await acked(c, call_id)
        assert await _next(c) == to_caller(late[0])
        assert hooked.is_set()
        await _pong(c)
    finally:
        await c.disconnect()


@pytest.mark.asyncio
async def test_review_m1_a_terminal_during_a_subscribers_replay_follows_its_chunks(
        owner, world, tuned, monkeypatch):
    """S7 review M1 (its probe A): subscribe proves the caller, then awaits the replay's read;
    a terminal committed during that read reached the now-proven registration through the
    bridge's terminal arm FIRST, and the replayed chunks followed it, where 4.7 has the terminal
    frame follow the chunks and 4.9 tells a client to treat the first terminal as final. C's
    running call holds chunks 0 and 1; a hook on the replay read commits a timeout and waits
    until the bridge has handled it. C hears chunk 0, chunk 1, then skill_timeout, once, then
    its ack, and nothing after it."""
    bridge = tuned(reoffer_secs=NEVER)
    caller, target = world["caller"], world["target"]
    call_id = _call(owner, world)
    early = _seed_chunks(target, call_id, [0, 1])
    frames = _caller_frames()
    original = frames.read_chunks
    hooked = threading.Event()

    def hook(conn, *args, **kwargs):
        rows = original(conn, *args, **kwargs)
        if not hooked.is_set():
            before, held = bridge.stats(), bridge.registry.stats()
            _commit_terminal(call_id, "timeout")
            _terminal_handled(bridge, before, held)
            hooked.set()
        return rows

    monkeypatch.setattr(frames, "read_chunks", hook)
    c = await _connect(caller)
    try:
        got = await subscribe(c, f"call:{call_id}")
        assert hooked.is_set(), "the replay never read the chunks"
        assert [f.get("type") for f in got] == ["skill_chunk", "skill_chunk", "skill_timeout"], got
        assert got[:2] == [to_caller(f) for f in early], got
        assert _terminal_type(got[2]) == ("skill_timeout", call_id), got
        await _pong(c)
    finally:
        await c.disconnect()


@pytest.mark.asyncio
async def test_review_m1_a_terminal_during_a_skill_calls_replay_follows_its_chunks(
        owner, world, tuned, monkeypatch):
    """S7 review M1 on the skill_call path (read there from the code, run here): the handler
    registers the socket proven, acks, then awaits the replay's read; a terminal committed
    during it reached the socket first, the chunks followed, and the re-read pushed the terminal
    again. A hook on `_start_in_thread` makes the call running and stores chunks 0 and 1 before
    the registration (both pointers find none); a hook on the replay read commits a timeout and
    waits until the bridge has handled it. C hears skill_acked, chunk 0, chunk 1, skill_timeout
    once, and nothing after it, read once the handler's registration path has RETURNED (a
    wrapper on `_after_start`): a re-read after the held terminal would push it a second time.
    """
    bridge = tuned(reoffer_secs=NEVER)
    caller, target = world["caller"], world["target"]
    frames = _caller_frames()
    original_start, original_read = frames._start_in_thread, frames.read_chunks
    original_after = frames._after_start
    stored: list = []
    finished: list = []
    hooked = threading.Event()

    async def after_start(*args, **kwargs):
        try:
            return await original_after(*args, **kwargs)
        finally:
            finished.append(True)

    def running_with_chunks(*args, **kwargs):
        result, row = original_start(*args, **kwargs)
        before = bridge.stats()
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("UPDATE skill_calls SET status = 'running', started_at = now() "
                         "WHERE call_id = %s", (result.call_id,))
        stored.extend(_seed_chunks(target, result.call_id, [0, 1]))
        _decided(bridge, before, 2)
        return result, row

    def hook(conn, *args, **kwargs):
        rows = original_read(conn, *args, **kwargs)
        call_id = kwargs.get("call_id", args[0] if args else None)
        if not hooked.is_set():
            before, held = bridge.stats(), bridge.registry.stats()
            _commit_terminal(call_id, "timeout")
            _terminal_handled(bridge, before, held)
            hooked.set()
        return rows

    monkeypatch.setattr(frames, "_start_in_thread", running_with_chunks)
    monkeypatch.setattr(frames, "read_chunks", hook)
    monkeypatch.setattr(frames, "_after_start", after_start)
    c = await _connect(caller)
    try:
        call_id = await call_over_ws(c, signed_call(caller, target, "s5.echo"))
        await acked(c, call_id)
        got = [await _next(c) for _ in range(3)]
        assert hooked.is_set(), "the replay never read the chunks"
        assert [f.get("type") for f in got] == ["skill_chunk", "skill_chunk", "skill_timeout"], got
        assert got[:2] == [to_caller(f) for f in stored], got
        assert _terminal_type(got[2]) == ("skill_timeout", call_id), got
        await _until(lambda: finished, "the skill_call's registration path to return")
        await _pong(c)
    finally:
        await c.disconnect()


@pytest.mark.asyncio
async def test_review_recovery_replays_the_missed_chunks_before_the_terminal(
        owner, world, tuned, monkeypatch):
    """4.6 part b: on a (re)LISTEN the recovery sweep replays each registration's chunks past
    its mark, THEN delivers the terminal of a row that is terminal (the review's MX9: the
    terminal re-check run first survived; it ends the registration the chunks go to). C's
    registration heard chunk 0; while the bridge's LISTEN connection is down, chunk 1 commits
    and the call times out, and neither pointer is heard. Once LISTEN is back C hears chunk 1,
    then skill_timeout, then nothing."""
    bridge = tuned(reoffer_secs=NEVER, beat_secs=NEVER)
    caller, target = world["caller"], world["target"]
    call_id = _call(owner, world)
    first = _seed_chunks(target, call_id, [0])
    c = await _connect(caller)
    down, entered, release = threading.Event(), threading.Event(), threading.Event()
    original = bridge._run_once

    def held(*args, **kwargs):
        if down.is_set():
            entered.set()
            release.wait(20)
        return original(*args, **kwargs)

    monkeypatch.setattr(bridge, "_run_once", held)
    try:
        assert await subscribe(c, f"call:{call_id}") == [to_caller(first[0])]
        down.set()
        pids = owner.execute("SELECT pid FROM pg_stat_activity WHERE datname = "
                             "current_database() AND query = 'LISTEN meshy_call_chunk'"
                             ).fetchall()
        assert len(pids) == 1, pids
        owner.execute("SELECT pg_terminate_backend(%s)", (pids[0][0],))
        assert await asyncio.to_thread(entered.wait, 15), "the bridge did not reconnect"
        assert not bridge.is_listening()
        missed = _seed_chunks(target, call_id, [1])
        _commit_terminal(call_id, "timeout")
        await asyncio.sleep(0.5)
        await _pong(c)
        down.clear()
        release.set()
        assert await bridge.wait_listening(15)
        assert await _next(c) == to_caller(missed[0])
        assert _terminal_type(await _next(c)) == ("skill_timeout", call_id)
        await _pong(c)
    finally:
        release.set()
        await c.disconnect()
