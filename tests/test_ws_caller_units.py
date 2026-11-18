"""Divergence 041 S6, the pure halves: the ONE terminal mapping and the CallRegistry.

Design (fin/design-skill-frames, 2e229a6): 4.7 "One terminal mapping", 4.6's terminal arm, and
5.1 (delivery of terminal frames only to sockets that proved caller ownership).

  mapping   every terminal row shape: completed with its canonical payload; completed from before
            migration 107 (#596, reconstructed and
            MARKED); failed and SIGNED (#916, a skill_result, the signature is the
            discriminator); failed and unsigned (skill_error, `FAILED` when no code); timeout;
            abandoned; and a row that is not terminal (nothing).
  one       the WS frame and the HTTP terminal body (`views._terminal_body`) read status, result
            and the error pair from the same function, so a WS caller and an HTTP poller cannot
            be told two outcomes for one row (rule 031's one-decision, applied to terminals).
  registry  idempotent per (call, connection); a provisional registration receives no frame but
            remembers that a terminal came; teardown removes every registration of a socket;
            the per-registration chunk high-water mark S7 reads.

RED: `apps/calls/terminal.py`, `apps/wsgw/call_registry.py` and `apps/wsgw/caller_frames.py` do
not exist before S6; each test imports what it tests.

Run:  .venv/Scripts/python.exe -m pytest tests/test_ws_caller_units.py -q -p no:cacheprovider
"""
from __future__ import annotations

import datetime as _dt
import importlib
import types
import uuid

import pytest

from apps.wsgw import envelope
from apps.wsgw.send_queue import SendQueue

WHEN = _dt.datetime(2026, 9, 25, 12, 0, 0, tzinfo=_dt.timezone.utc)
SIGNED = {"v": 1, "kind": "skill.result", "call_id": "c-1", "status": "completed",
          "result": {"ok": True}}


def _terminal():
    return importlib.import_module("apps.calls.terminal")


def _frames():
    return importlib.import_module("apps.wsgw.caller_frames")


def _registry():
    return importlib.import_module("apps.wsgw.call_registry").CallRegistry()


def row(status: str, **fields) -> dict:
    base = {"call_id": "c-1", "status": status, "result": None, "result_payload": None,
            "result_signature": None, "result_signing_kid": None, "error_code": None,
            "error_message": None, "completed_at": WHEN}
    base.update(fields)
    return base


ROWS = {
    "completed": row("completed", result={"ok": True}, result_payload=SIGNED,
                     result_signature="sig", result_signing_kid="kid"),
    "completed before 107": row("completed", result=["legacy", 1]),
    "completed before 107, no result, no signature": row("completed"),
    "failed, signed": row("failed", result={"ok": False}, error_code="E_X",
                          error_message="boom",
                          result_payload=dict(SIGNED, status="failed"),
                          result_signature="fsig", result_signing_kid="fkid"),
    "failed, unsigned": row("failed", error_code="E_Y", error_message="nope"),
    "failed, unsigned, no code": row("failed"),
    "failed, a payload but no signature": row("failed", error_code="E_Z",
                                              error_message="half",
                                              result_payload={"v": 1}),
    "timeout": row("timeout", error_code="E_DEADLINE_EXCEEDED", error_message="late"),
    "abandoned": row("abandoned"),
}

FRAMES = {
    "completed": {"type": "skill_result", "call_id": "c-1", "status": "completed",
                  "envelope": {"payload": SIGNED, "signature": "sig", "signing_kid": "kid"}},
    "completed before 107": {"type": "skill_result", "call_id": "c-1", "status": "completed",
                             "envelope": {"payload": ["legacy", 1], "signature": "",
                                          "signing_kid": ""},
                             "payload_reconstructed": True},
    "completed before 107, no result, no signature": {
        "type": "skill_result", "call_id": "c-1", "status": "completed",
        "envelope": {"payload": None, "signature": "", "signing_kid": ""},
        "payload_reconstructed": True},
    "failed, signed": {"type": "skill_result", "call_id": "c-1", "status": "failed",
                       "envelope": {"payload": dict(SIGNED, status="failed"),
                                    "signature": "fsig", "signing_kid": "fkid"}},
    "failed, unsigned": {"type": "skill_error", "call_id": "c-1", "error_code": "E_Y",
                         "error_message": "nope"},
    "failed, unsigned, no code": {"type": "skill_error", "call_id": "c-1",
                                  "error_code": "FAILED", "error_message": ""},
    "failed, a payload but no signature": {"type": "skill_error", "call_id": "c-1",
                                           "error_code": "E_Z", "error_message": "half"},
    "timeout": {"type": "skill_timeout", "call_id": "c-1"},
    "abandoned": {"type": "skill_error", "call_id": "c-1", "error_code": "ABANDONED",
                  "error_message": "call was cancelled"},
}


# ============================================================================ the mapping

@pytest.mark.parametrize("name", sorted(ROWS))
def test_each_terminal_row_maps_to_the_expected_frame(name):
    """Every terminal arm is pinned, including #596 (a completed row with no canonical payload
    is reconstructed from `result` and MARKED) and #916 (a failure with a payload AND a
    signature is a skill_result with status failed; without the signature it is a skill_error)."""
    assert _frames().terminal_frame(ROWS[name]) == FRAMES[name]


@pytest.mark.parametrize("status", ["pending", "routed", "queued", "running",
                                    "input_required", "auth_required"])
def test_a_row_that_is_not_terminal_maps_to_no_frame(status):
    """A spurious or early nudge sends nothing."""
    assert _frames().terminal_frame(row(status)) is None
    assert _terminal().of_row(row(status)) is None


def test_the_frames_are_the_s1_builders_shapes():
    """The mapping builds through apps/wsgw/envelope.py's builders (S1), not by hand: the
    completed frame equals `envelope.skill_result`'s, the abandoned one `skill_error`'s."""
    from apps.calls.envelope import SignedEnvelope
    assert FRAMES["completed"] == envelope.skill_result(
        "c-1", "completed", SignedEnvelope(payload=SIGNED, signature="sig", signing_kid="kid"))
    assert FRAMES["abandoned"] == envelope.skill_error("c-1", "ABANDONED", "call was cancelled")
    assert FRAMES["timeout"] == envelope.skill_timeout("c-1")


def _http_body(r: dict) -> dict:
    from apps.calls import views
    result = types.SimpleNamespace(call_id="c-1", skill_version="1.0.0", mode="async")
    return views._terminal_body(result, r)


@pytest.mark.parametrize("name", sorted(ROWS))
def test_the_http_terminal_body_and_the_ws_frame_tell_one_outcome(name):
    """4.7: status, result and the error pair come from `terminal.of_row` for BOTH. The HTTP
    body keeps its own wire (completed carries result, signature, kid and
    completed_at; failed carries the error pair too, null or not, #916; timeout and abandoned
    carry the status alone), and each of its values is the one the mapping gives."""
    r = ROWS[name]
    outcome = _terminal().of_row(r)
    body = _http_body(r)
    assert body["status"] == outcome.status == r["status"]
    if outcome.status in ("completed", "failed"):
        assert body["result"] == outcome.result == r["result"]
        assert body["signature"] == outcome.signature
        assert body["signing_kid"] == outcome.signing_kid
    if outcome.status == "failed":
        assert (body["error_code"], body["error_message"]) == (outcome.error_code,
                                                                outcome.error_message)
    if outcome.status in ("timeout", "abandoned"):
        assert set(body) == {"call_id", "skill_version", "mode", "status"}, body


def test_both_surfaces_read_the_one_function(monkeypatch):
    """The property, not an agreement that two copies could also reach: replace `of_row` and
    both the HTTP body and the WS frame change with it."""
    terminal = _terminal()
    original = terminal.of_row
    asked: list = []

    def spy(r):
        asked.append(r["status"])
        outcome = original(r)
        return None if outcome is None else outcome._replace(error_code="SPY")

    monkeypatch.setattr(terminal, "of_row", spy)
    failed = ROWS["failed, unsigned"]
    assert _http_body(failed)["error_code"] == "SPY"
    assert _frames().terminal_frame(failed)["error_code"] == "SPY"
    assert asked == ["failed", "failed"]


# ============================================================================ the registry

@pytest.mark.asyncio
async def test_a_registration_is_idempotent_per_call_and_connection():
    """A repeat subscribe from the same socket does not double-register,
    so one terminal reaches it once."""
    reg = _registry()
    conn, queue = uuid.uuid4(), SendQueue()
    assert await reg.register("c-1", conn, queue, proven=True) is True
    assert await reg.register("c-1", conn, queue, proven=True) is False
    assert await reg.count_for(conn) == 1
    assert await reg.deliver_terminal("c-1", {"type": "skill_timeout", "call_id": "c-1"}) == 1
    assert queue.snapshot() == [{"type": "skill_timeout", "call_id": "c-1"}]
    assert not await reg.is_registered("c-1"), "the terminal ends the registration"


@pytest.mark.asyncio
async def test_a_provisional_registration_gets_no_frame_and_remembers_the_terminal():
    """5.1: terminal frames go only to sockets that PROVED caller ownership. A subscribe
    registers first (4.7) as provisional; the terminal meanwhile is withheld from it and
    remembered, and `prove` reports it once. A proven registration beside it gets the frame."""
    reg = _registry()
    waiting, proven = uuid.uuid4(), uuid.uuid4()
    q_waiting, q_proven = SendQueue(), SendQueue()
    await reg.register("c-1", waiting, q_waiting, proven=False)
    await reg.register("c-1", proven, q_proven, proven=True)
    frame = {"type": "skill_timeout", "call_id": "c-1"}
    assert await reg.deliver_terminal("c-1", frame) == 1
    assert q_proven.snapshot() == [frame] and q_waiting.snapshot() == []
    assert await reg.is_registered("c-1"), "the provisional one stays until it is decided"
    assert await reg.prove("c-1", waiting) is True
    assert await reg.prove("c-1", waiting) is False, "reported once"
    assert await reg.prove("c-1", uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_drop_unproven_never_removes_a_proven_registration():
    reg = _registry()
    conn, queue = uuid.uuid4(), SendQueue()
    await reg.register("c-1", conn, queue, proven=True)
    await reg.register("c-1", conn, queue, proven=False)          # a later subscribe
    assert await reg.drop_unproven("c-1", conn) is False
    assert await reg.is_registered("c-1")
    await reg.register("c-2", conn, queue, proven=False)
    assert await reg.drop_unproven("c-2", conn) is True
    assert not await reg.is_registered("c-2")


@pytest.mark.asyncio
async def test_teardown_removes_every_registration_of_the_socket_and_no_other():
    """`unregister_connection` runs on socket teardown."""
    reg = _registry()
    gone, stays = uuid.uuid4(), uuid.uuid4()
    for call_id in ("c-1", "c-2"):
        await reg.register(call_id, gone, SendQueue(), proven=True)
    await reg.register("c-1", stays, SendQueue(), proven=True)
    assert await reg.unregister_connection(gone) == 2
    assert await reg.count_for(gone) == 0
    assert sorted(await reg.call_ids()) == ["c-1"]
    assert await reg.unregister("c-1", stays) is True
    assert await reg.call_ids() == []


@pytest.mark.asyncio
async def test_each_registration_keeps_its_own_chunk_high_water_mark():
    """The mark S7's replay and the recovery sweep read: per (call, connection), -1 before any
    chunk, and it only moves forward."""
    reg = _registry()
    a, b = uuid.uuid4(), uuid.uuid4()
    await reg.register("c-1", a, SendQueue(), proven=True)
    await reg.register("c-1", b, SendQueue(), proven=True)
    assert await reg.chunk_high_water("c-1", a) == -1
    assert await reg.advance_chunk("c-1", a, 3) is True
    assert await reg.advance_chunk("c-1", a, 2) is False
    assert (await reg.chunk_high_water("c-1", a), await reg.chunk_high_water("c-1", b)) == (3, -1)
    assert await reg.chunk_high_water("c-9", a) is None



# ============================================================ the S6 review (13fcdcc), F7

@pytest.mark.asyncio
async def test_a_dispatch_cancelled_before_its_first_step_gives_both_slots_back(monkeypatch):
    """S6 review F7. `handle_skill_call` takes the socket's slot and a process slot, then
    spawns the dispatch, whose try/finally gives them back. A task cancelled before its first
    step never enters that try, so both slots stayed taken for good (each such leak lowering
    the process cap of 32 permanently). Now a dispatch that never ran gives them back when its
    task ends. CONTROL: a dispatch that runs (SKILL_CALL_BAD, no target) gives both back, ONCE:
    another call holds a process slot throughout, so a second release would show as a value
    above 2 (a full BoundedSemaphore would refuse it inside the callback and hide it), and a
    second socket-slot release as `held` below 0."""
    import asyncio
    import threading
    frames = _frames()
    slots = threading.BoundedSemaphore(3)
    assert slots.acquire(blocking=False)                 # another call's slot, held throughout
    monkeypatch.setattr(frames, "PIPELINE_SLOTS", slots)
    spawned: list = []

    def spawn(coro):
        task = asyncio.ensure_future(coro)
        spawned.append(task)
        return task

    ctx = types.SimpleNamespace(call_slots=frames.CallSlots(), queue=SendQueue(), spawn=spawn,
                                peer_id=uuid.uuid4(), connection_id=uuid.uuid4())
    parsed = types.SimpleNamespace(envelope=types.SimpleNamespace(payload={}),
                                   mode_override=None)
    await frames.handle_skill_call(parsed, ctx)
    await asyncio.gather(*spawned, return_exceptions=True)
    assert (slots._value, ctx.call_slots.held) == (2, 0), "control"
    assert [f["code"] for f in ctx.queue.snapshot()] == ["SKILL_CALL_BAD"]
    spawned.clear()
    await frames.handle_skill_call(parsed, ctx)
    assert (slots._value, ctx.call_slots.held) == (1, 1), "control: both taken"
    spawned[0].cancel()                                  # before the task's first step
    await asyncio.gather(*spawned, return_exceptions=True)
    await asyncio.sleep(0)
    assert (slots._value, ctx.call_slots.held) == (2, 0)
