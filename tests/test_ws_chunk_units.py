"""Divergence 041 S7, the pure halves: the CallRegistry's chunk delivery and the chunk frame.

Design record (fin/design-skill-frames, 2e229a6): 4.6 (the chunk
arm, "read the chunk row by (call_id, sequence); build skill_chunk; deliver", and the recovery
sweep's part b, "replay the chunks after the highest sequence this registration has already
delivered"), 4.7 ("replay rebuilds the frame's envelope.payload by parsing the stored bytes")
and 5.1 (chunk frames only to sockets that proved caller ownership).

  live      `deliver_chunk`: every PROVEN registration of the call gets the frame, whatever its
            high-water mark (4.9 is at-least-once, and a target that sends out of order is not
            dropped), and each mark moves forward only; a provisional registration gets nothing
            (its subscribe reads the chunks once it is proven).
  recovery  `chunk_floors` names each call with a proven registration and the lowest mark among
            them; `deliver_chunks_after` sends each proven registration only the chunks past
            ITS mark, in order, and advances it.
  frame     `caller_frames.chunk_frame(row)`: the stored RFC 8785 bytes parsed back into
            `envelope.payload`, the stored signature and kid beside them; a row it cannot
            rebuild (no content, not JSON, not an object) gives no frame.

RED: none of these exists before S7 (AttributeError).

Run:  .venv/Scripts/python.exe -m pytest tests/test_ws_chunk_units.py -q -p no:cacheprovider
"""
from __future__ import annotations

import importlib
import uuid

import pytest

from apps.crypto import primitives as P
from apps.wsgw.send_queue import SendQueue

PAYLOAD = {"v": 1, "kind": "skill.chunk", "call_id": "c-1", "sequence": 4, "chunk_type": "text",
           "target_peer_id": "8b0b2b2e-1d1e-4d51-9d7f-2b9b0f4b7f11",
           "content": {"delta": "café \U0001f600", "n": 1.5},
           "issued_at": "2026-09-25T12:00:00Z"}


def _registry():
    return importlib.import_module("apps.wsgw.call_registry").CallRegistry()


def _frames():
    return importlib.import_module("apps.wsgw.caller_frames")


def chunk(seq: int) -> dict:
    return {"type": "skill_chunk", "call_id": "c-1", "sequence": seq}


def _row(**fields) -> dict:
    base = {"sequence": 4, "chunk_type": "text", "content": P.jcs(PAYLOAD),
            "chunk_signature": "c2ln", "chunk_signing_kid": "kid-t"}
    base.update(fields)
    return base


@pytest.mark.asyncio
async def test_a_live_chunk_reaches_every_proven_registration_and_no_provisional_one():
    """5.1: a proven registration gets the frame and its mark moves; a provisional one gets
    nothing and keeps -1. A chunk BELOW a registration's mark is still delivered (at-least-once;
    nothing is dropped for arriving late) and the mark does not move back."""
    reg = _registry()
    proven, waiting = uuid.uuid4(), uuid.uuid4()
    q_proven, q_waiting = SendQueue(), SendQueue()
    await reg.register("c-1", proven, q_proven, proven=True)
    await reg.register("c-1", waiting, q_waiting, proven=False)
    assert await reg.deliver_chunk("c-1", 5, chunk(5)) == 1
    assert await reg.deliver_chunk("c-1", 3, chunk(3)) == 1
    assert q_proven.snapshot() == [chunk(5), chunk(3)] and q_waiting.snapshot() == []
    assert await reg.chunk_high_water("c-1", proven) == 5
    assert await reg.chunk_high_water("c-1", waiting) == -1
    assert await reg.deliver_chunk("c-9", 0, chunk(0)) == 0
    assert await reg.is_registered("c-1"), "a chunk ends no registration"


@pytest.mark.asyncio
async def test_recovery_sends_each_registration_the_chunks_past_its_own_mark():
    """4.6 part b: `chunk_floors` gives each call's lowest PROVEN mark (a call registered only
    provisionally is not named); `deliver_chunks_after` sends each proven registration the
    chunks past its own mark, in order, advancing it, and a provisional one nothing."""
    reg = _registry()
    ahead, behind, waiting, other = (uuid.uuid4() for _ in range(4))
    queues = {k: SendQueue() for k in (ahead, behind, waiting, other)}
    await reg.register("c-1", ahead, queues[ahead], proven=True)
    await reg.register("c-1", behind, queues[behind], proven=True)
    await reg.register("c-1", waiting, queues[waiting], proven=False)
    await reg.register("c-2", other, queues[other], proven=False)
    await reg.advance_chunk("c-1", ahead, 1)
    assert await reg.chunk_floors() == {"c-1": -1}
    items = [(0, chunk(0)), (1, chunk(1)), (2, chunk(2))]
    assert await reg.deliver_chunks_after("c-1", items) == 4
    assert queues[ahead].snapshot() == [chunk(2)]
    assert queues[behind].snapshot() == [chunk(0), chunk(1), chunk(2)]
    assert queues[waiting].snapshot() == [] and queues[other].snapshot() == []
    assert (await reg.chunk_high_water("c-1", ahead),
            await reg.chunk_high_water("c-1", behind)) == (2, 2)
    assert await reg.chunk_floors() == {"c-1": 2}


def test_a_stored_chunk_row_becomes_the_frame_with_its_payload_parsed_from_the_bytes():
    """4.7: the frame's envelope.payload is the stored RFC 8785 bytes parsed (non-ASCII text
    and a float included), next to the stored signature and kid; `memoryview`, which psycopg
    returns for bytea, reads as bytes. The frame is S1's builder's shape."""
    frames = _frames()
    for content in (P.jcs(PAYLOAD), memoryview(P.jcs(PAYLOAD))):
        got = frames.chunk_frame("c-1", _row(content=content))
        assert got == {"type": "skill_chunk", "call_id": "c-1", "sequence": 4,
                       "chunk_type": "text",
                       "envelope": {"payload": PAYLOAD, "signature": "c2ln",
                                    "signing_kid": "kid-t"}}, got


@pytest.mark.parametrize("content", [None, b"not json", b"[1, 2]", b"\xff\xfe"])
def test_a_row_that_cannot_make_a_chunk_frame_gives_none(content):
    """No content (a file_ref row another writer left), bytes that are not JSON or not UTF-8,
    or JSON that is not an object (every signed payload is one): no frame, never a raise."""
    assert _frames().chunk_frame("c-1", _row(content=content)) is None


@pytest.mark.asyncio
async def test_review_m1_a_terminal_during_a_replay_is_held_and_pushed_after_it():
    """S7 review M1: while a replay to a PROVEN registration runs (`begin_replay` to
    `end_replay`, nested replays counted), the bridge's terminal is HELD: not pushed, the
    registration kept, `replay:terminal_held` counted. The last `end_replay` pushes it, after
    whatever the replay pushed, and ends the registration. A registration with no replay
    running gets the terminal at once, as before; a provisional one is marked as before; a
    replay is not begun for a registration that is absent or not proven."""
    reg = _registry()
    replaying, idle, waiting = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    q = {k: SendQueue() for k in (replaying, idle, waiting)}
    await reg.register("c-1", replaying, q[replaying], proven=True)
    await reg.register("c-1", idle, q[idle], proven=True)
    await reg.register("c-1", waiting, q[waiting], proven=False)
    assert await reg.begin_replay("c-1", waiting) is False
    assert await reg.begin_replay("c-9", idle) is False
    assert await reg.begin_replay("c-1", replaying) is True
    assert await reg.begin_replay("c-1", replaying) is True        # a second, overlapping
    term = {"type": "skill_timeout", "call_id": "c-1"}
    assert await reg.deliver_terminal("c-1", term) == 1
    assert q[idle].snapshot() == [term] and q[replaying].snapshot() == []
    assert reg.stats().get("replay:terminal_held") == 1
    await q[replaying].push(chunk(0))
    assert await reg.end_replay("c-1", replaying) is False         # one replay still runs
    assert q[replaying].snapshot() == [chunk(0)]
    await q[replaying].push(chunk(1))
    assert await reg.end_replay("c-1", replaying) is True
    assert q[replaying].snapshot() == [chunk(0), chunk(1), term]
    assert await reg.chunk_high_water("c-1", replaying) is None, "the registration ended"
    assert await reg.end_replay("c-1", replaying) is False
    assert await reg.prove("c-1", waiting) is True, "the provisional one was marked"
