"""Divergence 041 S6: the caller's side over WebSocket.

Design `docs/design/skill-frames.md` (fin/design-skill-frames, 2e229a6): section 7 row S6,
4.7 (skill_call, its registration order, the one terminal mapping, skill_cancel through a new
`cancel_by_caller`, `subscribe`/`unsubscribe call:`), 4.6 (the CallBridge's terminal arm), 5.3
(replay: one reader, divergence 036), the approved Q7 (the cancel and subscribe doors), Q12 (no
cancel or timeout frame to the target) and Q13, critique F1 and F2, and defects
2.12.4 to 2.12.6.

  T2   a WS skill_call to a connected T: C gets skill_acked, then T's signed skill_result, once;
       and a terminal committed right after the handler's re-read still reaches C, because the
       registration comes BEFORE that read.
  T3   a WS skill_call to the memory authority is terminal when start_call returns: C gets
       skill_acked and the terminal frame, and the call is never registered.
  T10  C cancels over WS while T runs: C gets skill_error ABANDONED through the trigger; a
       stranger's cancel, the target's own cancel and a cancel of an unknown call get ONE
       refusal and move nothing; T's later result is refused. A suspended caller, or one whose
       bound key was revoked, cannot cancel from its open socket.
  T16  the WS path reads 036's one switch: ON (the default), a USED nonce re-signed under a NEW
       call_id is refused over WS and over HTTP, and a payload with no nonce is refused; OFF,
       both are accepted; a byte-identical resend is a replay in both modes.
  T17  17 concurrent skill_calls on one socket: exactly one SKILL_CALL_BUSY, and a skill_cancel
       and a ping sent meanwhile are answered while the 16 are held. The process-wide cap on
       pipeline threads (R1) answers SKILL_CALL_BUSY too, and gives its slot back.
  T19  the memory authority's socket, connected while C's WS call to it runs in-process, is
       offered nothing and its result is refused; C gets the in-process result.
  T12  (S5's caller-socket half) the janitor's job body times a queued call out on a connection
       of its own: the WS caller and a subscribed socket each get skill_timeout.
  T23  (S5's caller-socket half) run_call_worker's loop times the call out while the shared
       loop is held: the WS caller gets skill_timeout.
  T13  (S5's caller-socket half) C on worker B, T on worker A: C's skill_call reaches T, and
       T's result reaches C through B's bridge.
  T15  (S5's caller-socket half) a terminal committed while the bridge's LISTEN was down
       reaches the registered caller by the recovery sweep.
  and  the re-offer sweep's terminal re-check; a forged terminal pointer delivers nothing; the
       subscribe door (register first, prove ownership, silence for a non-caller, the terminal
       of a finished call, the 1000 cap) and unsubscribe (2.12.6).

The harness is S5's (tests/test_call_delivery.py): the production consumer, registry and
bridges, DPoP ENFORCED with real proofs, HTTP writes with proofs too, and every envelope with a
fresh nonce; nothing here pins a nonce switch except T16, which sets it explicitly.

RED: every test that sends a caller frame meets the `UNIMPLEMENTED` error frame and the 1011
close that frame_handlers.py answers skill_call, skill_cancel and a `call:` subscribe with
before S6. The new modules are imported per use (`_registry_module`, `_caller_frames`), so
that is where each of those tests fails, not at collection.

Run:  .venv/Scripts/python.exe -m pytest tests/test_ws_caller_frames.py -q -p no:cacheprovider
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import importlib
import threading
import time
import uuid

import psycopg
import pytest
from django.test import override_settings

from apps.calls import janitor, nonce_ledger, worker
from apps.core import dbconn
from apps.crypto import primitives as P
from apps.jobs import leases, registry, runner
from apps.wsgw import call_notify
from apps.wsgw.connection_manager import MANAGER, ConnectionManager
from apps.wsgw.consumer import MeshyWebSocketConsumer
from tests.fixtures.call_rows import seed_call
from tests.fixtures.hub_presence_db import hub_connections_clean  # noqa: F401  (autouse)
# The S5 harness. Its autouse fixtures (server_config, bridges_stopped) apply here too.
from tests.test_call_delivery import (  # noqa: F401  (fixtures are used by name)
    DSN,
    NEVER,
    UNIFORM,
    _connect,
    _delta,
    _invoke_frame,
    _kinds,
    _next,
    _notify,
    _pong,
    _send,
    _skill_id,
    _status,
    _until,
    answer,
    authority,
    bridges_stopped,
    invoke,
    owner,
    server_config,
    signed_call,
    signed_result,
    tuned,
    verifies_under,
    world,
)

#: The refusal of a cancel for a call this peer did not make, the same for a call that does
#: not exist (Q7: the WS door closed). A literal, so a change of text is a test failure.
NOT_YOURS = "call was not made by this peer"
BUSY_HERE = ("too many concurrent skill calls on this connection (max 16); retry shortly")
TERMINAL_DECISIONS = ("call:terminal:registered", "call:terminal:no_registration")


def _registry_module():
    """apps/wsgw/call_registry.py (041 S6), imported per use: on the tree before S6 each test
    then fails where it meets the refusal on the wire, not at collection."""
    return importlib.import_module("apps.wsgw.call_registry")


def _caller_frames():
    return importlib.import_module("apps.wsgw.caller_frames")


@pytest.fixture(autouse=True)
def registry_cleared():
    """The production CallRegistry is process-wide, like MANAGER (bridges_stopped clears that)."""
    yield
    try:
        reg = _registry_module().CALL_REGISTRY
    except ImportError:
        return
    reg._by_call.clear()
    reg._by_connection.clear()


# --------------------------------------------------------------------------- the caller

async def call_over_ws(comm, envelope: dict, *, mode: str | None = None) -> str:
    """Send a skill_call frame carrying `envelope`; returns its signed call_id."""
    frame = {"type": "skill_call", "envelope": envelope}
    if mode is not None:
        frame["mode_override"] = mode
    await comm.send_json_to(frame)
    return envelope["payload"]["call_id"]


async def acked(comm, call_id: str, timeout: float = 15) -> None:
    frame = await _next(comm, timeout)
    assert frame == {"type": "skill_acked", "call_id": call_id}, frame


async def subscribe(comm, *channels: str) -> list[dict]:
    """Send a subscribe; returns the frames that came before its ack, which must echo."""
    await comm.send_json_to({"type": "subscribe", "channels": list(channels)})
    before = []
    while (frame := await _next(comm))["type"] != "subscribed":
        before.append(frame)
    assert frame == {"type": "subscribed", "channels": list(channels)}, frame
    return before


def abandoned_frame(call_id: str) -> dict:
    return {"type": "skill_error", "call_id": call_id, "error_code": "ABANDONED",
            "error_message": "call was cancelled"}


def _result_columns(owner, call_id: str) -> tuple:
    return owner.execute("SELECT result_payload, result_signature, result_signing_kid "
                         "FROM skill_calls WHERE call_id = %s", (call_id,)).fetchone()


def _commit_terminal(call_id: str, status: str = "timeout") -> None:
    """A terminal written by a writer that is not this server's (the bootstrap role, its own
    connection): the trigger fires for any writer."""
    with psycopg.connect(DSN, autocommit=True) as conn:
        moved = conn.execute("UPDATE skill_calls SET status = %s, completed_at = now() "
                             "WHERE call_id = %s AND status NOT IN "
                             "('completed', 'failed', 'timeout', 'abandoned') RETURNING 1",
                             (status, call_id)).fetchall()
    assert moved, f"control: {call_id} was in flight"


def _wait_for_terminal_decision(bridge, before: dict, timeout: float = 10.0) -> None:
    """Block (on a worker thread) until `bridge` has decided a terminal pointer since
    `before`: the decision is counted on the loop, in the turn that read the registry."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        now = bridge.stats()
        if any(now.get(k, 0) > before.get(k, 0) for k in TERMINAL_DECISIONS):
            return
        time.sleep(0.02)
    raise AssertionError("the bridge never decided the terminal pointer")


def _delivery_recorder(bridge, monkeypatch) -> list:
    """The call_ids of the bridge's delivery passes (`_deliver_terminals`), each appended when
    its pass has RETURNED. A proven registration is written to, and a provisional one marked
    `terminal_seen`, inside that pass, on the loop. S6 review F2: a hook that waited for the
    DECISION only (`_wait_for_terminal_decision`) let a subscribe's `prove` run before the
    mark, and the terminal then reached the socket after the ack instead of before it."""
    passes: list = []
    original = bridge._deliver_terminals

    async def recorded(frames, kind):
        try:
            return await original(frames, kind)
        finally:
            passes.extend(call_id for call_id, _frame in frames)

    monkeypatch.setattr(bridge, "_deliver_terminals", recorded)
    return passes


def _wait_for_terminal_handled(bridge, before: dict, passes: list, call_id: str,
                               timeout: float = 10.0) -> None:
    """Block (on a worker thread) until the bridge has FINISHED with the call's terminal
    pointer: its delivery pass for the call returned (`passes`, from `_delivery_recorder`), or
    it decided, since `before`, that nothing here is registered (then nothing follows)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if call_id in passes:
            return
        if bridge.stats().get("call:terminal:no_registration", 0) > before.get(
                "call:terminal:no_registration", 0):
            return
        time.sleep(0.02)
    raise AssertionError("the bridge never finished with the terminal pointer")


def _spy_registrations(monkeypatch) -> list | None:
    """Every call_id the production CallRegistry is asked to register, in order. None on a
    tree without the registry, where the test fails on the wire first."""
    try:
        reg = _registry_module().CALL_REGISTRY
    except ImportError:
        return None
    seen: list = []
    original = reg.register

    async def spy(call_id, *args, **kwargs):
        seen.append(call_id)
        return await original(call_id, *args, **kwargs)

    monkeypatch.setattr(reg, "register", spy)
    return seen


async def _registered(call_id: str) -> bool:
    return await _registry_module().CALL_REGISTRY.is_registered(call_id)


def _slow_skill(owner, target: dict, name: str = "s6.slow", runtime_ms: int = 1000) -> str:
    """An async skill whose runtime is `runtime_ms` (one second by default), so a call's deadline
    passes in a test. A test whose CONTROL runs before the deadline passes a longer one: gate
    mg613 (12 Nov) swept t12's call before its control on a loaded box."""
    owner.execute("INSERT INTO skills (peer_id, name, version, mode, share_mode, input_schema, "
                  "is_current, max_runtime_ms) VALUES (%s,%s,'1.0.0','async','grants_only',"
                  "'{}',TRUE,%s)", (target["peer_id"], name, runtime_ms))
    return name


async def _past_deadline(owner, call_id: str) -> None:
    deadline = owner.execute("SELECT deadline_at FROM skill_calls WHERE call_id = %s",
                             (call_id,)).fetchone()[0]
    wait = (deadline - _dt.datetime.now(_dt.timezone.utc)).total_seconds()
    await asyncio.sleep(max(0.0, wait) + 0.3)


# ============================================================================== T2

@pytest.mark.asyncio
async def test_t2_a_ws_caller_gets_skill_acked_then_the_signed_result_once(owner, world, tuned):
    """C's skill_call runs the one pipeline (transport `ws`, hard-coded) and C gets
    skill_acked; T gets the invoke, verifies C's signature and answers; the terminal pointer
    reaches this worker's bridge, which finds C's socket registered, re-reads the row and
    sends the one terminal mapping's frame: skill_result, the SIGNED payload T sent with its
    signature and kid (#596). Once, and the call is then unregistered. A `sync` override does
    not make the WS path wait: the ack comes first either way."""
    tuned(reoffer_secs=NEVER)
    caller, target = world["caller"], world["target"]
    t = await _connect(target)
    c = await _connect(caller)
    try:
        env = signed_call(caller, target, "s5.echo", {"text": "over ws"})
        call_id = await call_over_ws(c, env, mode="sync")
        await acked(c, call_id)
        frame = await _invoke_frame(t)
        assert frame["envelope"] == env and verifies_under(caller, frame["envelope"])
        assert frame["mode"] == "sync"
        signed = await answer(t, target, call_id, {"echo": "over ws"})

        got = await _next(c)
        payload, signature, kid = _result_columns(owner, call_id)
        assert payload == signed
        assert got == {"type": "skill_result", "call_id": call_id, "status": "completed",
                       "envelope": {"payload": signed, "signature": signature,
                                    "signing_kid": kid}}, got
        assert verifies_under(target, got["envelope"])
        await _pong(c)
        assert not await _registered(call_id), "the terminal frame ends the registration"
    finally:
        await c.disconnect()
        await t.disconnect()
    transport, mode, caller_col = owner.execute(
        "SELECT transport, mode, caller_peer_id FROM skill_calls WHERE call_id = %s",
        (call_id,)).fetchone()
    assert (transport, mode, caller_col) == ("ws", "sync", caller["peer_id"])
    assert _kinds(owner, call_id) == [("created", "meshy"), ("routed", "meshy"),
                                      ("target_acked", "target"), ("running", "target"),
                                      ("completed", "target")]


@pytest.mark.asyncio
async def test_t2_a_terminal_committed_just_after_the_handlers_reread_reaches_the_caller(
        owner, world, tuned, monkeypatch):
    """4.7's registration order: register, push skill_acked, then RE-READ. The terminal is
    committed right after that re-read returns (a hook on `caller_frames.read_call`), and the
    hook waits until the bridge has decided its pointer. The registration came first, so the
    bridge finds C's socket and delivers. CONTROL, before the hook: a plain skill_call is
    acked (on the tree before S6: UNIMPLEMENTED and 1011)."""
    bridge = tuned(reoffer_secs=NEVER)
    caller, target = world["caller"], world["target"]           # T offline: `queued`
    c = await _connect(caller)
    try:
        control = signed_call(caller, target, "s5.echo")
        await acked(c, await call_over_ws(c, control))

        frames = _caller_frames()
        original = frames.read_call
        env = signed_call(caller, target, "s5.echo")
        call_id = env["payload"]["call_id"]
        hooked = threading.Event()

        def hook(conn, *args, **kwargs):
            row = original(conn, *args, **kwargs)
            if _named(args, kwargs) == call_id and not hooked.is_set():
                before = bridge.stats()
                _commit_terminal(call_id)
                _wait_for_terminal_decision(bridge, before)
                hooked.set()
            return row

        monkeypatch.setattr(frames, "read_call", hook)
        before = bridge.stats()
        await call_over_ws(c, env)
        await acked(c, call_id)
        assert await asyncio.to_thread(hooked.wait, 15), "the handler never re-read the row"
        assert await _next(c) == {"type": "skill_timeout", "call_id": call_id}
        assert _delta(before, bridge.stats(), "call:terminal") == {
            "call:terminal:registered": 1}
    finally:
        await c.disconnect()


def _named(args, kwargs) -> str | None:
    """The call_id a `read_call(conn, call_id)` hook was asked for, positional or keyword."""
    return kwargs.get("call_id", args[0] if args else None)


# ============================================================================== T3

@pytest.mark.asyncio
async def test_t3_a_ws_call_to_the_memory_authority_is_terminal_at_once_and_never_registered(
        owner, world, authority, tuned, monkeypatch):
    """4.7's Django-specific hazard (R6): a call to the memory authority is TERMINAL when
    start_call returns, and its terminal trigger has already fired. The handler pushes
    skill_acked and the terminal frame (the server-signed result, whose kid the served JWKS
    carries) and does NOT register: nothing will ever deliver for it again."""
    tuned(reoffer_secs=NEVER)
    caller = world["caller"]
    registered = _spy_registrations(monkeypatch)
    c = await _connect(caller)
    try:
        env = signed_call(caller, authority, "memory.write",
                          {"namespace": authority["namespace"], "key": "t3", "value": {"n": 3}},
                          version="0.2.0")
        call_id = await call_over_ws(c, env)
        await acked(c, call_id)
        got = await _next(c)
        payload, signature, kid = _result_columns(owner, call_id)
        assert got == {"type": "skill_result", "call_id": call_id, "status": "completed",
                       "envelope": {"payload": payload, "signature": signature,
                                    "signing_kid": kid}}, got
        assert payload["target_peer_id"] == str(authority["peer_id"])
        await _pong(c)
    finally:
        await c.disconnect()
    assert _status(owner, call_id) == "completed"
    assert registered is not None, "the registry exists"
    assert call_id not in registered, f"a terminal call was registered: {registered}"


# ============================================================================= T10

@pytest.mark.asyncio
async def test_t10_the_caller_cancels_and_nobody_else_can(owner, world, tuned):
    """F1: skill_cancel runs `cancel_by_caller`, whose UPDATE carries `caller_peer_id = $me`.
    A stranger's cancel, the TARGET's cancel (it may read the call, not end it) and a cancel of
    a call that does not exist get the same code and text (Q7), and the row stays `running`.
    The caller's cancel lands with no reply of its own; the trigger's pointer brings it
    skill_error ABANDONED. T's result afterwards is refused with the transition text (T owns
    the call). A second cancel is `not in flight`, as the HTTP route's 409 says."""
    tuned(reoffer_secs=NEVER)
    caller, target, stranger = world["caller"], world["target"], world["other"]
    t = await _connect(target)
    c = await _connect(caller)
    x = await _connect(stranger)
    try:
        env = signed_call(caller, target, "s5.echo")
        call_id = await call_over_ws(c, env)
        await acked(c, call_id)
        assert (await _invoke_frame(t))["call_id"] == call_id
        assert await _send(t, {"type": "skill_ack", "call_id": call_id}) is None
        assert await _send(t, {"type": "skill_running", "call_id": call_id}) is None

        refusal = {"type": "error", "code": "SKILL_CANCEL_FAILED", "message": NOT_YOURS}
        assert await _send(x, {"type": "skill_cancel", "call_id": call_id,
                               "reason": "not mine"}) == refusal
        assert await _send(t, {"type": "skill_cancel", "call_id": call_id}) == refusal
        assert await _send(x, {"type": "skill_cancel",
                               "call_id": f"s6-{uuid.uuid4()}"}) == refusal
        assert _status(owner, call_id) == "running", "a refused cancel moved the call"

        await c.send_json_to({"type": "skill_cancel", "call_id": call_id,
                              "reason": "user abort"})
        assert await _next(c) == abandoned_frame(call_id)
        await _pong(c)
        assert _status(owner, call_id) == "abandoned"
        assert await _send(t, signed_result(target, call_id)) == {
            "type": "error", "code": "SKILL_RESULT_FAILED",
            "message": "state transition: cannot move from abandoned to completed"}
        assert await _send(c, {"type": "skill_cancel", "call_id": call_id}) == {
            "type": "error", "code": "SKILL_CANCEL_FAILED",
            "message": "call not in flight (status: Abandoned)"}
    finally:
        for s in (x, c, t):
            await s.disconnect()
    events = owner.execute("SELECT event_type, source, source_peer_id, details "
                           "FROM skill_call_events WHERE call_id = %s ORDER BY id",
                           (call_id,)).fetchall()
    assert events[-1] == ("caller_disconnected", "caller", caller["peer_id"],
                          {"reason": "user abort"}), events
    assert [e[0] for e in events].count("caller_disconnected") == 1


@pytest.mark.asyncio
async def test_a_suspended_caller_or_a_revoked_bound_key_cannot_cancel_from_an_open_socket(
        owner, world, tuned):
    """The active and live-key clauses in `cancel_by_caller`'s WHERE (4.7, R12): a suspension
    is SQL and sends no notification, and the revocation sweep is an hour away here, so the
    socket stays open; its cancel gets the uniform refusal and the call stays `queued`. The
    same with the key its token is bound to revoked. CONTROL: restored, the same socket's
    cancel lands."""
    tuned(reoffer_secs=NEVER)
    caller, target = world["caller"], world["target"]
    key = caller["keys"][0]
    c = await _connect(caller)
    try:
        env = signed_call(caller, target, "s5.echo")
        call_id = await call_over_ws(c, env)
        await acked(c, call_id)
        refusal = {"type": "error", "code": "SKILL_CANCEL_FAILED", "message": NOT_YOURS}
        cancel = {"type": "skill_cancel", "call_id": call_id}

        owner.execute("UPDATE peers SET status = 'suspended' WHERE id = %s",
                      (caller["peer_id"],))
        assert await _send(c, cancel) == refusal
        owner.execute("UPDATE peers SET status = 'active' WHERE id = %s", (caller["peer_id"],))
        owner.execute("UPDATE peer_keys SET active = FALSE, revoked_at = now() "
                      "WHERE peer_id = %s AND kid = %s", (caller["peer_id"], key["kid"]))
        assert await _send(c, cancel) == refusal
        assert _status(owner, call_id) == "queued"

        owner.execute("UPDATE peer_keys SET active = TRUE, revoked_at = NULL "
                      "WHERE peer_id = %s AND kid = %s", (caller["peer_id"], key["kid"]))
        await c.send_json_to(cancel)                                            # CONTROL
        assert await _next(c) == abandoned_frame(call_id)
    finally:
        owner.execute("UPDATE peers SET status = 'active' WHERE id = %s", (caller["peer_id"],))
        owner.execute("UPDATE peer_keys SET active = TRUE, revoked_at = NULL "
                      "WHERE peer_id = %s AND kid = %s", (caller["peer_id"], key["kid"]))
        await c.disconnect()
    assert _status(owner, call_id) == "abandoned"


# ============================================================================= T16

@pytest.mark.asyncio
async def test_t16_the_ws_path_reads_the_one_replay_switch(owner, world, tuned, monkeypatch,
                                                            caplog):
    """5.3 and critique F2, derived from `apps/calls/nonce_ledger.py`'s account as landed
    (036): step 2 answers a byte-identical resend of an envelope that CREATED a call from its
    row, so that case is a replay whether the switch is on or off; the ledger decides a nonce
    re-signed under a NEW call_id and a payload with no nonce. ON (unset: the default), both
    are refused over WS, and the re-signed nonce over HTTP too (the same gate); gate 8's log
    line says why. OFF (`MESHY_ENVELOPE_NONCE_ENFORCE=False`, the setting the one reader
    reads), both are accepted. Every CallsContext the WS path builds reads
    `nonce_ledger.enforce_from_settings()`: the spy counts one reading per skill_call and per
    HTTP invoke, in order.

    S6 review F8, the docstring's other two cases, over WS. ON: a skill_call REFUSED after step
    4c (a `sync` override on a skill whose runtime passes the sync ceiling, refused at step 7)
    burns its nonce, so the same bytes sent again without the override are a replay; a
    skill_call that FAILED before its row committed (the insert raises) recorded nothing, so
    the same bytes then run. OFF: the refused-then-resent bytes are accepted."""
    tuned(reoffer_secs=NEVER)
    caller, target = world["caller"], world["target"]               # T offline: `queued`
    readings: list = []
    original = nonce_ledger.enforce_from_settings

    def spy():
        value = original()
        readings.append(value)
        return value

    monkeypatch.setattr(nonce_ledger, "enforce_from_settings", spy)
    caplog.set_level("WARNING", logger="meshychat.calls")
    failed = {"type": "error", "code": "SKILL_CALL_FAILED", "message": "E_FORBIDDEN: forbidden"}
    owner.execute("INSERT INTO skills (peer_id, name, version, mode, share_mode, input_schema, "
                  "is_current, max_runtime_ms) VALUES (%s,'s6.long','1.0.0','async',"
                  "'grants_only','{}',TRUE,60000)", (target["peer_id"],))
    step7 = {"type": "error", "code": "SKILL_CALL_FAILED",
             "message": "E_VALIDATION: invalid mode override: sync not allowed for skill mode "
                        "async"}
    from apps.calls import repository
    armed: list = []
    original_insert = repository.insert_call

    def insert_or_fail(*args, **kwargs):
        if armed:
            armed.clear()
            raise RuntimeError("injected: the call row never committed")
        return original_insert(*args, **kwargs)

    monkeypatch.setattr(repository, "insert_call", insert_or_fail)

    def resigned(first: dict, *, nonce=...) -> dict:
        """`first`'s payload under a NEW call_id (and `nonce`, or none), signed again."""
        payload = dict(first["payload"], call_id=f"s6-{uuid.uuid4()}")
        if nonce is None:
            payload.pop("nonce")
        elif nonce is not ...:
            payload["nonce"] = nonce
        key = caller["keys"][0]
        return {"payload": payload, "signature": P.b64u(P.sign(key["seed"], P.jcs(payload))),
                "signing_kid": key["kid"]}

    def created(call_id: str) -> int:
        return owner.execute("SELECT count(*) FROM skill_call_events WHERE call_id = %s AND "
                             "event_type = 'created'", (call_id,)).fetchone()[0]

    c = await _connect(caller)
    try:
        # ---- ON, the default (neither switch pinned) ----
        first = signed_call(caller, target, "s5.echo")
        await acked(c, await call_over_ws(c, first))
        reused = resigned(first)
        await call_over_ws(c, reused)
        assert await _next(c) == failed
        assert _status_or_none(owner, reused["payload"]["call_id"]) is None
        over_http = resigned(first)
        body = await invoke(caller, target, "s5.echo", over_http, status=403)
        assert body["error"] == "E_FORBIDDEN", body
        assert _status_or_none(owner, over_http["payload"]["call_id"]) is None
        bare = resigned(first, nonce=None)
        await call_over_ws(c, bare)
        assert await _next(c) == failed
        assert _status_or_none(owner, bare["payload"]["call_id"]) is None
        await acked(c, await call_over_ws(c, first))                 # the replay
        assert created(first["payload"]["call_id"]) == 1
        burned = signed_call(caller, target, "s6.long")
        await call_over_ws(c, burned, mode="sync")
        assert await _next(c) == step7                               # refused after 4c
        await call_over_ws(c, burned)
        assert await _next(c) == failed                              # the burned nonce
        assert _status_or_none(owner, burned["payload"]["call_id"]) is None
        retried = signed_call(caller, target, "s5.echo")
        armed.append(True)
        await call_over_ws(c, retried)
        assert await _next(c) == {"type": "error", "code": "SKILL_CALL_FAILED",
                                  "message": "E_INTERNAL: internal error"}
        assert not armed, "control: the injected failure fired"
        assert _status_or_none(owner, retried["payload"]["call_id"]) is None
        await acked(c, await call_over_ws(c, retried))               # recorded nothing: runs
        assert created(retried["payload"]["call_id"]) == 1
        reasons = [r.getMessage() for r in caplog.records if "gate 8 refused" in r.getMessage()]
        assert any(burned["payload"]["call_id"] in m and "envelope replay" in m
                   for m in reasons), reasons
        assert any(reused["payload"]["call_id"] in m and "envelope replay" in m
                   for m in reasons), reasons
        assert any(bare["payload"]["call_id"] in m and "missing envelope nonce" in m
                   for m in reasons), reasons

        # ---- OFF, spelled as the one reader reads it ----
        with override_settings(MESHY_ENVELOPE_NONCE_ENFORCE=False):
            second = signed_call(caller, target, "s5.echo")
            await acked(c, await call_over_ws(c, second))
            reused_off = resigned(second)
            await acked(c, await call_over_ws(c, reused_off))
            bare_off = resigned(second, nonce=None)
            await acked(c, await call_over_ws(c, bare_off))
            await acked(c, await call_over_ws(c, second))            # the replay
            burned_off = signed_call(caller, target, "s6.long")
            await call_over_ws(c, burned_off, mode="sync")
            assert await _next(c) == step7
            await acked(c, await call_over_ws(c, burned_off))        # nothing was burned
        assert _status(owner, reused_off["payload"]["call_id"]) == "queued"
        assert _status(owner, bare_off["payload"]["call_id"]) == "queued"
        assert created(second["payload"]["call_id"]) == 1
    finally:
        await c.disconnect()
    assert readings == [True] * 9 + [False] * 6, readings


def _status_or_none(owner, call_id: str) -> str | None:
    row = owner.execute("SELECT status FROM skill_calls WHERE call_id = %s",
                        (call_id,)).fetchone()
    return None if row is None else row[0]


# ============================================================================= T17

def _hold(owner, call_ids) -> None:
    """Hold the pipeline's call_id lock (pipeline._call_id_lock) for each call, from the
    bootstrap role's session: a start_call for one of them waits at step 1 until released."""
    for call_id in call_ids:
        owner.execute("SELECT pg_advisory_lock(hashtext(%s))", (call_id,))


async def _collect(comm, until, timeout: float = 5.0) -> list[dict]:
    """Frames read until `until(frames)` holds; fails after `timeout` seconds."""
    frames: list = []
    deadline = time.monotonic() + timeout
    while not until(frames):
        remaining = deadline - time.monotonic()
        assert remaining > 0, f"timed out; read so far: {frames}"
        frames.append(await _next(comm, remaining))
    return frames


@pytest.mark.asyncio
async def test_t17_seventeen_calls_on_one_socket_get_one_busy_and_the_reader_stays_free(
        owner, world, tuned):
    """R1: the dispatch is SPAWNED under a
    per-socket semaphore of 16, and over it SKILL_CALL_BUSY is answered without blocking the
    reader. Each of the 17 calls' step-1 lock is held, so the 16 that spawn wait inside the
    pipeline. Meanwhile a skill_cancel of C's own subscribed call and a ping are answered at
    once. Released, the 16 are acked, and the 17th never made a row."""
    tuned(reoffer_secs=NEVER)
    caller, target = world["caller"], world["target"]
    other_call = seed_call(owner, caller, target, _skill_id(owner, target), status="queued",
                           call_id=f"s6-{uuid.uuid4()}")
    envs = [signed_call(caller, target, "s5.echo") for _ in range(17)]
    ids = [e["payload"]["call_id"] for e in envs]
    c = await _connect(caller)
    try:
        assert await subscribe(c, f"call:{other_call}") == []
        _hold(owner, ids)
        for env in envs:
            await call_over_ws(c, env)
        await c.send_json_to({"type": "skill_cancel", "call_id": other_call})
        await c.send_json_to({"type": "ping"})

        def answered(frames):
            return ({"type": "pong"} in frames and abandoned_frame(other_call) in frames
                    and any(f.get("code") == "SKILL_CALL_BUSY" for f in frames))

        early = await _collect(c, answered, timeout=5.0)
        busy = [f for f in early if f.get("code") == "SKILL_CALL_BUSY"]
        assert busy == [{"type": "error", "code": "SKILL_CALL_BUSY", "message": BUSY_HERE}]
        assert not [f for f in early if f["type"] == "skill_acked"], early
        owner.execute("SELECT pg_advisory_unlock_all()")
        later = await _collect(
            c, lambda fs: len([f for f in fs if f["type"] == "skill_acked"]) == 16, 30)
        assert [f for f in later if f["type"] != "skill_acked"] == [], later
        assert {f["call_id"] for f in later} == set(ids[:16])
        await _pong(c)
    finally:
        owner.execute("SELECT pg_advisory_unlock_all()")
        await c.disconnect()
    assert _status_or_none(owner, ids[16]) is None, "the refused 17th made a row"
    assert all(_status(owner, i) == "queued" for i in ids[:16])


@pytest.mark.asyncio
async def test_the_process_wide_pipeline_cap_answers_busy_and_returns_its_slot(
        owner, world, tuned, monkeypatch):
    """R1's process-wide cap (proposal 32): every socket's skill_call takes a slot of ONE
    semaphore before its pipeline thread starts, and over it the call is SKILL_CALL_BUSY at
    once, with a message that names the server, not the socket. Two sockets, the cap set to
    2: two calls held in the pipeline, the third refused. Released, the slots come back: a
    fourth call is acked. CONTROL, before the cap is lowered: a call is acked."""
    tuned(reoffer_secs=NEVER)
    caller, target = world["caller"], world["target"]
    envs = [signed_call(caller, target, "s5.echo") for _ in range(4)]
    ids = [e["payload"]["call_id"] for e in envs]
    c1, c2 = await _connect(caller), await _connect(caller)
    try:
        await acked(c1, await call_over_ws(c1, signed_call(caller, target, "s5.echo")))
        frames = _caller_frames()
        monkeypatch.setattr(frames, "PIPELINE_SLOTS", threading.BoundedSemaphore(2))
        _hold(owner, ids[:3])
        await call_over_ws(c1, envs[0])
        await call_over_ws(c2, envs[1])
        await asyncio.sleep(0.5)
        await call_over_ws(c1, envs[2])
        refused = await _next(c1)
        assert refused == {"type": "error", "code": "SKILL_CALL_BUSY",
                           "message": frames.BUSY_SERVER}, refused
        owner.execute("SELECT pg_advisory_unlock_all()")
        await acked(c1, ids[0])
        await acked(c2, ids[1])
        await acked(c1, await call_over_ws(c1, envs[3]))
    finally:
        owner.execute("SELECT pg_advisory_unlock_all()")
        await c1.disconnect()
        await c2.disconnect()
    assert _status_or_none(owner, ids[2]) is None


# ============================================================================= T19

@pytest.mark.asyncio
async def test_t19_the_authority_is_offered_nothing_of_a_ws_callers_in_process_call(
        owner, world, authority, monkeypatch, tuned):
    """S5's T19 with the in-process call started by C's WS skill_call. While the call runs
    (its handler held), the memory authority's own socket connects: its redelivery offers it
    nothing, and a signed result it sends for the call gets the uniform refusal. Released, C
    gets skill_acked and the server-signed terminal frame; the authority's result never
    landed. CONTROL: the call was mid-run when the authority connected."""
    bridge = tuned(reoffer_secs=NEVER)
    from apps.memoryskills import handlers
    entered, release = threading.Event(), threading.Event()
    original = handlers.dispatch

    def held(*args, **kwargs):
        entered.set()
        release.wait(20)
        return original(*args, **kwargs)

    monkeypatch.setattr(handlers, "dispatch", held)
    caller = world["caller"]
    env = signed_call(caller, authority, "memory.write",
                      {"namespace": authority["namespace"], "key": "t19ws", "value": {"n": 1}},
                      version="0.2.0")
    c = await _connect(caller)
    socks = [c]
    try:
        call_id = await call_over_ws(c, env)
        assert await asyncio.to_thread(entered.wait, 15), "the in-process call never started"
        assert _status(owner, call_id) == "running", "control: the call is mid-run"
        before = bridge.stats()
        socks.append(a := await _connect(authority))
        await _pong(a)
        refusal = await _send(a, signed_result(authority, call_id, {"forged": True}))
        assert refusal == {"type": "error", "code": "SKILL_RESULT_FAILED", "message": UNIFORM}
        release.set()
        await acked(c, call_id)
        got = await _next(c)
        assert got["type"] == "skill_result" and got["status"] == "completed", got
        assert got["envelope"]["payload"]["target_peer_id"] == str(authority["peer_id"])
        assert got["envelope"]["payload"]["result"] != {"forged": True}
    finally:
        release.set()
        for s in socks:
            await s.disconnect()
    assert _delta(before, bridge.stats(), "redelivery:sent") == {}


# ======================================================================= T12 and T23

@pytest.mark.asyncio
async def test_t12_the_janitor_times_out_a_queued_call_and_the_ws_caller_and_subscriber_hear(
        owner, world, tuned):
    """S5's T12, caller-socket half: C's WS call to an offline T is `queued`; a second socket
    of C subscribes to it. Past its deadline the janitor's job body, on a connection of its
    own, times it out; its edge's pointer reaches this worker's bridge, and both sockets get
    skill_timeout. CONTROL: before the deadline the same body leaves it alone."""
    tuned(reoffer_secs=NEVER)
    caller, target = world["caller"], world["target"]
    skill = _slow_skill(owner, target, runtime_ms=6000)     # the control must run inside the deadline
    c, c2 = await _connect(caller), await _connect(caller)
    try:
        env = signed_call(caller, target, skill)
        call_id = await call_over_ws(c, env)
        await acked(c, call_id)
        assert await subscribe(c2, f"call:{call_id}") == []
        await asyncio.to_thread(_janitor_body)
        assert _status(owner, call_id) == "queued", "control: swept before its deadline"
        await _pong(c)
        await _past_deadline(owner, call_id)
        await asyncio.to_thread(_janitor_body)
        assert _status(owner, call_id) == "timeout"
        assert await _next(c) == {"type": "skill_timeout", "call_id": call_id}
        assert await _next(c2) == {"type": "skill_timeout", "call_id": call_id}
        await _pong(c)
        await _pong(c2)
    finally:
        await c.disconnect()
        await c2.disconnect()


def _janitor_body() -> None:
    conn = dbconn.connect()
    try:
        janitor.sweep_expired_deadlines(conn, limit=1000)
    finally:
        conn.close()


def _sweep_due_now() -> None:
    conn = dbconn.connect()
    try:
        leases.register(conn, "call_deadline_sweep")
        assert leases.force_due(conn, "call_deadline_sweep")
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_t23_the_workers_loop_times_the_call_out_and_the_ws_caller_hears(
        owner, world, tuned):
    """S5's T23, caller-socket half: the shared loop is held by a slow job body that sorts
    before the sweep; run_call_worker's own loop times C's queued call out within one of its
    5 s ticks, and C gets skill_timeout while the shared loop is still held."""
    tuned(reoffer_secs=NEVER)
    caller, target = world["caller"], world["target"]
    skill = _slow_skill(owner, target, "s6.slow23")
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def slow(conn, job) -> int:
        started.set()
        release.wait(60)
        return 0

    fake = registry.Job(name=f"t23ws_slow_{uuid.uuid4().hex[:8]}", interval_secs=60,
                        timeout_secs=90, jitter_frac=0.2, max_batch=1, run=slow)

    def shared_loop():
        runner.run_due(dbconn.connect, jobs=(fake, registry.by_name("call_deadline_sweep")))
        finished.set()

    await asyncio.to_thread(_sweep_due_now)
    shared = threading.Thread(target=shared_loop, name="t23ws-shared", daemon=True)
    shared.start()
    stop = threading.Event()
    loop = threading.Thread(target=worker.run_loop, args=(dbconn.connect,),
                            kwargs={"stop": stop}, name="t23ws-worker", daemon=True)
    c = await _connect(caller)
    try:
        assert await asyncio.to_thread(started.wait, 15), "the shared loop never started"
        loop.start()
        call_id = await call_over_ws(c, signed_call(caller, target, skill))
        await acked(c, call_id)
        assert await _next(c, timeout=15) == {"type": "skill_timeout", "call_id": call_id}
        assert not finished.is_set(), "control: the shared loop was still held"
    finally:
        stop.set()
        release.set()
        await c.disconnect()
        await asyncio.to_thread(loop.join, 15)
        await asyncio.to_thread(shared.join, 30)
    assert _status(owner, call_id) == "timeout"


# ============================================================================= T13

@pytest.mark.asyncio
async def test_t13_a_ws_caller_on_one_worker_reaches_a_target_on_another(owner, world):
    """S5's T13, caller-socket half. Worker A (its own registry, CallRegistry and bridge) holds
    T; worker B (the production ones) holds C. C's skill_call runs on B: step 9 reads T in the
    presence table (`routed`), A's bridge delivers the invoke, T answers on A, and the
    terminal pointer reaches B's bridge, where C's socket is registered: C gets the result.
    A's bridge has no registration for the call."""
    caller, target = world["caller"], world["target"]
    manager_a = ConnectionManager()
    bridge_a = call_notify.CallBridge(manager_a, reoffer_secs=NEVER)
    app_a = MeshyWebSocketConsumer.as_asgi(manager=manager_a, call_bridge=bridge_a)
    socks = []
    try:
        socks.append(t := await _connect(target, app=app_a, bridge=bridge_a))
        socks.append(c := await _connect(caller))
        assert not await MANAGER.is_online(target["peer_id"]), "control: T is not on B"
        before_a, before_b = bridge_a.stats(), call_notify.CALL_BRIDGE.stats()
        env = signed_call(caller, target, "s5.echo")
        call_id = await call_over_ws(c, env)
        await acked(c, call_id)
        assert (await _invoke_frame(t))["envelope"] == env
        signed = await answer(t, target, call_id)
        got = await _next(c)
        assert (got["type"], got["status"], got["envelope"]["payload"]) == (
            "skill_result", "completed", signed)
    finally:
        for s in socks:
            await s.disconnect()
        bridge_a.stop()
    assert _delta(before_b, call_notify.CALL_BRIDGE.stats(), "call:terminal") == {
        "call:terminal:registered": 1}
    assert _delta(before_a, bridge_a.stats(), "call:terminal") == {
        "call:terminal:no_registration": 1}


# ============================================================================= T15

@pytest.mark.asyncio
async def test_t15_a_terminal_committed_while_listen_was_down_reaches_the_registered_caller(
        owner, world, tuned, monkeypatch):
    """4.6's recovery sweep, part b: for every call registered in this worker, re-read the row
    and deliver its terminal. C's call is queued and registered; the bridge's LISTEN
    connection is killed and its reconnect held before it LISTENs; the call is timed out
    meanwhile and its pointer reaches nobody (C gets nothing). Released, the sweep runs
    before listening is reported, and C gets skill_timeout. The re-offer sweep is an hour
    away."""
    bridge = tuned(reoffer_secs=NEVER, beat_secs=NEVER)
    caller, target = world["caller"], world["target"]
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
        # Registered by a subscribe, whose ownership read is done before its ack, so no read
        # of the handler's is still running when the terminal commits (a skill_call's re-read
        # after its ack could see the terminal itself, and deliver it with no bridge at all).
        env = signed_call(caller, target, "s5.echo")
        call_id = env["payload"]["call_id"]
        await invoke(caller, target, "s5.echo", env)
        assert await subscribe(c, f"call:{call_id}") == []
        down.set()
        pids = owner.execute("SELECT pid FROM pg_stat_activity WHERE datname = "
                             "current_database() AND query = 'LISTEN meshy_call_chunk'"
                             ).fetchall()
        assert len(pids) == 1, pids
        owner.execute("SELECT pg_terminate_backend(%s)", (pids[0][0],))
        assert await asyncio.to_thread(entered.wait, 15), "the bridge did not reconnect"
        assert not bridge.is_listening()
        _commit_terminal(call_id)
        await asyncio.sleep(0.5)
        await _pong(c)

        before = bridge.stats()
        down.clear()
        release.set()
        assert await bridge.wait_listening(15)
        assert await _next(c) == {"type": "skill_timeout", "call_id": call_id}
        assert _delta(before, bridge.stats(), "recovery_terminal:") == {
            "recovery_terminal:sent": 1}, "the frame came from the recovery sweep"
    finally:
        release.set()
        await c.disconnect()


# ================================================== the re-offer re-check, forged pointers

@pytest.mark.asyncio
async def test_the_reoffer_sweep_rechecks_registered_calls_for_a_lost_terminal(
        owner, world, tuned, monkeypatch):
    """4.6's re-offer sweep, for registered calls: a terminal re-check. The terminal pointer
    for C's call is lost without any LISTEN outage (dropped here, standing in for a loop hop
    that timed out, mesh_notify.LOOP_CALL_TIMEOUT_SECS): C gets nothing. The next periodic
    sweep re-reads the registered call and delivers skill_timeout. S6 review F6 (ME): the
    re-check reads in batches of RECHECK_BATCH, set to ONE here, and the lost call is
    registered SECOND, behind a call that stays in flight, so it is read in the second batch;
    CONTROL, the first call stays registered."""
    bridge = tuned(reoffer_secs=NEVER)
    caller, target = world["caller"], world["target"]
    c = await _connect(caller)
    original = bridge._on_call
    dropped: list = []

    def lossy(payload, loop, query):
        pointer = call_notify.parse_call_payload(payload)
        if pointer is not None and pointer.call_id in lost and pointer.status == "timeout":
            dropped.append(pointer.call_id)
            return None
        return original(payload, loop, query)

    lost: set = set()
    monkeypatch.setattr(bridge, "_on_call", lossy)
    monkeypatch.setattr(call_notify, "RECHECK_BATCH", 1)
    try:
        # Registered by a subscribe, whose read is done before its ack (see T15 above): only
        # the bridge can deliver the terminal from here on.
        first = signed_call(caller, target, "s5.echo")
        await invoke(caller, target, "s5.echo", first)
        assert await subscribe(c, f"call:{first['payload']['call_id']}") == []
        env = signed_call(caller, target, "s5.echo")
        call_id = env["payload"]["call_id"]
        await invoke(caller, target, "s5.echo", env)
        assert await subscribe(c, f"call:{call_id}") == []
        lost.add(call_id)
        _commit_terminal(call_id)
        await _until(lambda: dropped, "the pointer to be dropped")
        await _pong(c)
        before = bridge.stats()
        tuned(reoffer_secs=0.3)
        assert await _next(c) == {"type": "skill_timeout", "call_id": call_id}
        assert not await _registered(call_id)
        assert _delta(before, bridge.stats(), "reoffer_terminal:") == {
            "reoffer_terminal:sent": 1}, "the frame came from the re-check"
        assert await _registered(first["payload"]["call_id"]), "control: still in flight"
    finally:
        await c.disconnect()


@pytest.mark.asyncio
async def test_a_forged_terminal_pointer_delivers_nothing_the_row_does_not_say(
        owner, world, tuned):
    """Pointers only (4.1, 4.6): a forged `completed` pointer naming C's registered call,
    which is `queued`, makes the bridge re-read the row, find it not terminal, and send
    nothing; the registration stays. A forged terminal pointer naming another caller's
    finished call, which nobody registered, is not even read. CONTROL: the real terminal
    then reaches C."""
    bridge = tuned(reoffer_secs=NEVER)
    caller, target, other = world["caller"], world["target"], world["other"]
    c = await _connect(caller)
    try:
        call_id = await call_over_ws(c, signed_call(caller, target, "s5.echo"))
        await acked(c, call_id)
        theirs = seed_call(owner, other, target, _skill_id(owner, target),
                           status="completed", call_id=f"s6-{uuid.uuid4()}")
        before = bridge.stats()
        t = str(target["peer_id"])
        _notify(owner, {"call_id": call_id, "status": "completed", "target": t})
        _notify(owner, {"call_id": theirs, "status": "completed", "target": t})
        await _until(lambda: _delta(before, bridge.stats(), "call:terminal").get(
            "call:terminal:no_registration") == 1 and _delta(
            before, bridge.stats(), "terminal:").get("terminal:not_terminal") == 1,
            "both forgeries decided")
        await _pong(c)
        assert await _registered(call_id)
        assert _status(owner, call_id) == "queued"
        _commit_terminal(call_id, "abandoned")                                  # CONTROL
        assert await _next(c) == abandoned_frame(call_id)
    finally:
        await c.disconnect()


# ======================================================================== subscribe

@pytest.mark.asyncio
async def test_subscribe_registers_the_caller_and_its_terminal_reaches_it(owner, world, tuned):
    """4.7: `subscribe` with `call:<id>` for a call C made over HTTP: the ack echoes the
    channels as sent, and the call's terminal
    reaches the subscribed socket. Registration never reads `transport`."""
    tuned(reoffer_secs=NEVER)
    caller, target = world["caller"], world["target"]
    env = signed_call(caller, target, "s5.echo")
    call_id = env["payload"]["call_id"]
    assert (await invoke(caller, target, "s5.echo", env))["status"] == "queued"
    c = await _connect(caller)
    try:
        channels = (f"presence:{target['peer_id']}", f"call:{call_id}")
        assert await subscribe(c, *channels) == []
        _commit_terminal(call_id, "abandoned")
        assert await _next(c) == abandoned_frame(call_id)
    finally:
        await c.disconnect()


@pytest.mark.asyncio
async def test_subscribing_to_a_finished_call_pushes_its_terminal_frame(owner, world, tuned):
    """2.12.4: a subscriber to an already-terminal call must receive its terminal frame.
    Here the ownership read finds the row
    terminal and pushes the one mapping's frame before the ack, and leaves nothing
    registered."""
    tuned(reoffer_secs=NEVER)
    caller, target = world["caller"], world["target"]
    env = signed_call(caller, target, "s5.echo")
    call_id = env["payload"]["call_id"]
    await invoke(caller, target, "s5.echo", env)
    _commit_terminal(call_id, "timeout")
    c = await _connect(caller)
    try:
        assert await subscribe(c, f"call:{call_id}") == [
            {"type": "skill_timeout", "call_id": call_id}]
        assert not await _registered(call_id)
        await _pong(c)
    finally:
        await c.disconnect()


@pytest.mark.asyncio
async def test_a_non_callers_subscription_is_silent_and_counted(owner, world, tuned):
    """Q7, the subscribe door: a stranger, the call's TARGET and a subscription to a call that
    does not exist are all acked with their channels and hear NOTHING, and the reason is
    counted server-side and kept off the wire. CONTROL: the caller's own
    subscription hears the terminal."""
    tuned(reoffer_secs=NEVER)
    caller, target, stranger = world["caller"], world["target"], world["other"]
    env = signed_call(caller, target, "s5.echo")
    call_id = env["payload"]["call_id"]
    await invoke(caller, target, "s5.echo", env)
    reg_stats = None
    socks = []
    try:
        socks.append(x := await _connect(stranger))
        socks.append(t := await _connect(target))
        socks.append(c := await _connect(caller))
        assert (await _invoke_frame(t))["call_id"] == call_id     # T is connected: routed
        assert await subscribe(c, f"call:{call_id}") == []
        before = _registry_module().CALL_REGISTRY.stats()
        assert await subscribe(x, f"call:{call_id}", f"call:s6-{uuid.uuid4()}") == []
        assert await subscribe(t, f"call:{call_id}") == []
        reg_stats = _delta(before, _registry_module().CALL_REGISTRY.stats(), "subscribe:")
        # S6 review F6 (MC): silent is not enough, the provisional registrations are GONE; C's
        # proven one is the call's only registration.
        regs = _registry_module().CALL_REGISTRY._by_call.get(call_id, {})
        assert len(regs) == 1 and all(r.proven for r in regs.values()), (
            f"a non-caller's registration outlived its read: {regs}")
        _commit_terminal(call_id, "abandoned")
        assert await _next(c) == abandoned_frame(call_id)                       # CONTROL
        await _pong(x)
        await _pong(t)
    finally:
        for s in socks:
            await s.disconnect()
    assert reg_stats == {"subscribe:not_caller": 2, "subscribe:unknown_call": 1}, reg_stats


@pytest.mark.asyncio
async def test_a_non_caller_registered_first_hears_nothing_while_its_ownership_is_read(
        owner, world, tuned, monkeypatch):
    """Register FIRST, then read (4.7), without a window in which a non-caller's socket is a
    subscriber: the registration is PROVISIONAL until the read proves the caller, and the
    bridge delivers to proven registrations only. The terminal is committed while the
    stranger's ownership read runs, and the hook waits for the bridge's decision (it finds a
    registration) and until its delivery pass has RETURNED, so the stranger's registration is
    provisional when the pass meets it (S6 review F2). The stranger hears nothing; C,
    subscribed before, hears the terminal."""
    bridge = tuned(reoffer_secs=NEVER)
    caller, target, stranger = world["caller"], world["target"], world["other"]
    env = signed_call(caller, target, "s5.echo")
    call_id = env["payload"]["call_id"]
    await invoke(caller, target, "s5.echo", env)
    c = await _connect(caller)
    x = await _connect(stranger)
    try:
        assert await subscribe(c, f"call:{call_id}") == []                # CONTROL, and RED
        frames = _caller_frames()
        original = frames.read_call
        hooked = threading.Event()

        passes = _delivery_recorder(bridge, monkeypatch)

        def hook(conn, *args, **kwargs):
            row = original(conn, *args, **kwargs)
            if _named(args, kwargs) == call_id and not hooked.is_set():
                before = bridge.stats()
                _commit_terminal(call_id, "abandoned")
                _wait_for_terminal_handled(bridge, before, passes, call_id)
                hooked.set()
            return row

        monkeypatch.setattr(frames, "read_call", hook)
        assert await subscribe(x, f"call:{call_id}") == []
        assert hooked.is_set()
        assert await _next(c) == abandoned_frame(call_id)
        await _pong(x)
    finally:
        await c.disconnect()
        await x.disconnect()


@pytest.mark.asyncio
async def test_a_terminal_committed_while_the_callers_ownership_is_read_reaches_it(
        owner, world, tuned, monkeypatch):
    """2.12.5, registration racing delivery: registration comes FIRST (provisional), so a
    terminal published during the ownership read is not lost. The terminal is committed during
    the ownership read, and the hook waits until the bridge's
    delivery pass for it has RETURNED, so the provisional registration is marked before the
    read proves C the caller (S6 review F2: waiting for the decision alone left that order to
    the scheduler, and the frame then came after the ack in 2 runs of 8); the handler then
    re-reads and pushes the terminal before the ack. CONTROL, before the hook: C's plain
    subscription is acked."""
    bridge = tuned(reoffer_secs=NEVER)
    caller, target = world["caller"], world["target"]
    envs = [signed_call(caller, target, "s5.echo") for _ in range(2)]
    control, call_id = (e["payload"]["call_id"] for e in envs)
    for env in envs:
        await invoke(caller, target, "s5.echo", env)
    c = await _connect(caller)
    try:
        assert await subscribe(c, f"call:{control}") == []
        frames = _caller_frames()
        original = frames.read_call
        hooked = threading.Event()

        passes = _delivery_recorder(bridge, monkeypatch)

        def hook(conn, *args, **kwargs):
            row = original(conn, *args, **kwargs)
            if _named(args, kwargs) == call_id and not hooked.is_set():
                before = bridge.stats()
                _commit_terminal(call_id, "timeout")
                _wait_for_terminal_handled(bridge, before, passes, call_id)
                hooked.set()
            return row

        monkeypatch.setattr(frames, "read_call", hook)
        before = bridge.stats()
        got = await subscribe(c, f"call:{call_id}")
        assert got == [{"type": "skill_timeout", "call_id": call_id}], got
        assert _delta(before, bridge.stats(), "call:terminal") == {
            "call:terminal:registered": 1}
        assert not await _registered(call_id)
    finally:
        await c.disconnect()


@pytest.mark.asyncio
async def test_unsubscribe_removes_the_call_registration(owner, world, tuned):
    """2.12.6: unsubscribe removes a `call:` registration. Of C's two subscribed sockets, the
    one that unsubscribed (acked with its channels) hears
    nothing; the other hears the terminal."""
    tuned(reoffer_secs=NEVER)
    caller, target = world["caller"], world["target"]
    env = signed_call(caller, target, "s5.echo")
    call_id = env["payload"]["call_id"]
    await invoke(caller, target, "s5.echo", env)
    c1, c2 = await _connect(caller), await _connect(caller)
    try:
        assert await subscribe(c1, f"call:{call_id}") == []
        assert await subscribe(c2, f"call:{call_id}") == []
        await c1.send_json_to({"type": "unsubscribe", "channels": [f"call:{call_id}"]})
        assert await _next(c1) == {"type": "unsubscribed", "channels": [f"call:{call_id}"]}
        _commit_terminal(call_id, "abandoned")
        assert await _next(c2) == abandoned_frame(call_id)
        await _pong(c1)
    finally:
        await c1.disconnect()
        await c2.disconnect()


@pytest.mark.asyncio
async def test_call_subscriptions_per_socket_are_capped(owner, world, tuned, monkeypatch):
    """4.7 and 5.4: at most MAX_CALL_SUBSCRIPTIONS_PER_CONN (the presence cap, 1000) call
    registrations per socket, applied as the presence cap is: the channels past it are
    dropped and the ack still echoes all of them. The cap is
    set to 2 here, after one subscription: of the next frame's two calls the second is not
    registered and its terminal is not heard; CONTROL, the first subscription's is."""
    tuned(reoffer_secs=NEVER)
    caller, target = world["caller"], world["target"]
    envs = [signed_call(caller, target, "s5.echo") for _ in range(3)]
    ids = [e["payload"]["call_id"] for e in envs]
    for env in envs:
        await invoke(caller, target, "s5.echo", env)
    c = await _connect(caller)
    try:
        assert await subscribe(c, f"call:{ids[0]}") == []
        monkeypatch.setattr(_caller_frames(), "MAX_CALL_SUBSCRIPTIONS_PER_CONN", 2)
        assert await subscribe(c, f"call:{ids[1]}", f"call:{ids[2]}") == []
        _commit_terminal(ids[2], "abandoned")
        await asyncio.sleep(0.5)
        await _pong(c)
        _commit_terminal(ids[0], "abandoned")
        assert await _next(c) == abandoned_frame(ids[0])
    finally:
        await c.disconnect()
    assert _registry_module().CALL_REGISTRY.stats().get("subscribe:capped", 0) >= 1


# ========================================================== the socket's skill_call errors

@pytest.mark.asyncio
async def test_a_skill_call_the_pipeline_refuses_is_answered_and_the_socket_stays_open(
        owner, world, tuned):
    """What a refused skill_call says. A payload with
    no usable target_peer_id: SKILL_CALL_BAD, before any database work. A pipeline refusal:
    SKILL_CALL_FAILED with the HTTP route's rendering of the same error
    (`wire.api_error_from_rpc`), `<code>: <message>`, so a missing grant or a replayed nonce
    reads `E_FORBIDDEN: forbidden` and is no oracle; a bad signature keeps its code and text.
    The socket stays open throughout: the pong follows each answer."""
    tuned(reoffer_secs=NEVER)
    caller, target = world["caller"], world["target"]
    c = await _connect(caller)
    try:
        no_target = signed_call(caller, target, "s5.echo")
        no_target["payload"].pop("target_peer_id")
        await call_over_ws(c, no_target)
        assert await _next(c) == {"type": "error", "code": "SKILL_CALL_BAD",
                                  "message": "missing or invalid target_peer_id in payload"}
        await _pong(c)
        await call_over_ws(c, signed_call(caller, target, "s6.no-such-skill"))
        assert await _next(c) == {"type": "error", "code": "SKILL_CALL_FAILED",
                                  "message": "E_NOT_FOUND: not found"}
        await _pong(c)
        forged = signed_call(caller, target, "s5.echo")
        forged["signature"] = P.b64u(bytes(64))
        await call_over_ws(c, forged)
        reply = await _next(c)
        assert reply["type"] == "error" and reply["code"] == "SKILL_CALL_FAILED", reply
        assert reply["message"].startswith("E_SIG_BAD: "), reply
        await _pong(c)
        assert _status_or_none(owner, forged["payload"]["call_id"]) is None
    finally:
        await c.disconnect()


@pytest.mark.asyncio
async def test_another_peers_signed_call_replayed_on_a_socket_is_refused(owner, world, tuned):
    """5.1: the caller is the socket's AUTHENTICATED peer, and the signed `caller_peer_id` must
    be that peer's (verify_payload, inside the one pipeline). A stranger that captured C's
    signed skill_call and sends it on its own socket is refused, makes no row and is registered
    for nothing; C's own socket, sending the same bytes, is acked (CONTROL)."""
    tuned(reoffer_secs=NEVER)
    caller, target, stranger = world["caller"], world["target"], world["other"]
    x = await _connect(stranger)
    c = await _connect(caller)
    try:
        env = signed_call(caller, target, "s5.echo")
        call_id = await call_over_ws(x, env)
        reply = await _next(x)
        assert reply["type"] == "error" and reply["code"] == "SKILL_CALL_FAILED", reply
        assert reply["message"].startswith("E_SIG_"), reply
        await _pong(x)
        assert _status_or_none(owner, call_id) is None
        assert not await _registered(call_id)
        await acked(c, await call_over_ws(c, env))                               # CONTROL
        assert _status(owner, call_id) == "queued"
        owner_of = owner.execute("SELECT caller_peer_id FROM skill_calls WHERE call_id = %s",
                                 (call_id,)).fetchone()[0]
        assert owner_of == caller["peer_id"]
    finally:
        await x.disconnect()
        await c.disconnect()


@pytest.mark.asyncio
async def test_a_sockets_registrations_go_with_it(owner, world, tuned):
    """A socket's
    registrations end when it closes, and another socket's for the same call do not. C's first
    socket calls (registered by its skill_call) and its second subscribes; the first closes and
    the call is still registered, for the second; the second closes and nothing is left."""
    tuned(reoffer_secs=NEVER)
    caller, target = world["caller"], world["target"]
    c1, c2 = await _connect(caller), await _connect(caller)
    call_id = await call_over_ws(c1, signed_call(caller, target, "s5.echo"))
    await acked(c1, call_id)
    assert await subscribe(c2, f"call:{call_id}") == []
    reg = _registry_module().CALL_REGISTRY
    await c1.disconnect()                    # returns once the consumer's disconnect ran
    assert await reg.is_registered(call_id), "the other socket's registration stays"
    await c2.disconnect()
    assert not await reg.is_registered(call_id), "a closed socket left a registration"
    assert await reg.call_ids() == []



# ================================================================ the S6 review (13fcdcc)

class _DaphneReplies:
    """What daphne does with the application's `websocket.close` after the accept, run through
    daphne's OWN code: `daphne.ws_protocol.WebSocketProtocol.handle_reply`, whose `serverClose`
    calls autobahn's OWN `sendClose`, the method that refuses a code that is not 1000 or
    3000-4999 (autobahn/websocket/protocol.py). Only the frame writer, `sendCloseFrame`, is a
    recorder. Then daphne's application checker: an application that raised is closed by
    `handle_exception`, which writes 1011 through `sendCloseFrame`, past that rule."""

    def __init__(self) -> None:
        from daphne.ws_protocol import WebSocketProtocol
        self.protocol = WebSocketProtocol.__new__(WebSocketProtocol)
        self.protocol.state = WebSocketProtocol.STATE_OPEN
        self.written: list = []
        self.raised: list = []
        self.protocol.sendCloseFrame = (
            lambda code=None, reasonUtf8=None, isReply=False: self.written.append(code))

    def serve(self, app):
        async def served(scope, receive, send):
            accepted = False

            async def reply(message):
                nonlocal accepted
                if message["type"] == "websocket.accept":
                    accepted = True
                elif message["type"] == "websocket.close" and accepted:
                    self.protocol.handle_reply(message)          # raises where daphne raises
                await send(message)

            try:
                await app(scope, receive, reply)
            except Exception as exc:  # noqa: BLE001 - daphne's application checker
                self.raised.append(exc)
                self.protocol.handle_exception(exc)
                await send({"type": "websocket.close", "code": self.written[-1]})
        return served


@pytest.mark.asyncio
async def test_a_close_daphne_refuses_still_tears_the_socket_down(owner, world, tuned,
                                                                  monkeypatch):
    """S6 review F1 (HIGH; the close is 041 S1's). Under daphne the consumer's close with 1011
    (a frame not built yet) RAISES: the application dies, daphne closes the socket with 1011
    itself, and Channels never dispatches `disconnect`. The socket stayed in the
    ConnectionManager and in `hub_connections`, and after MAX_CONNS_PER_PEER such closes the
    peer could not connect to that worker again. Served through daphne's own reply code
    (`_DaphneReplies`) with the per-peer cap set to 2, three sockets in turn each send
    skill_resume (still UNIMPLEMENTED), get the refusal and daphne's 1011; after each, the peer
    has no socket registered and no presence row, and the third is admitted. CONTROL of the
    instrument: it refuses 1011 with autobahn's own text and passes 4401."""
    tuned(reoffer_secs=NEVER)
    from apps.wsgw import connection_manager
    from meshychat.asgi import application
    from tests.test_call_delivery import _online_rows
    monkeypatch.setattr(connection_manager, "MAX_CONNS_PER_PEER", 2)
    caller = world["caller"]
    daphne = _DaphneReplies()
    refused = "invalid close code 1011 (must be 1000 or from [3000, 4999])"
    with pytest.raises(Exception) as caught:
        daphne.protocol.handle_reply({"type": "websocket.close", "code": 1011})
    assert str(caught.value) == refused
    daphne.protocol.handle_reply({"type": "websocket.close", "code": 4401})
    assert daphne.written == [4401], "control: the instrument passes a private-use code"
    daphne.written.clear()
    app = daphne.serve(application)
    for n in range(3):
        c = await _connect(caller, app=app)
        await c.send_json_to({"type": "skill_resume", "call_id": "c-1", "context_id": "x",
                              "action": "accept", "continuation_token": "t"})
        refusal = await _next(c)
        assert (refusal["type"], refusal["code"]) == ("error", "UNIMPLEMENTED"), refusal
        assert await c.receive_output(timeout=10) == {"type": "websocket.close", "code": 1011}
        assert await MANAGER.connection_count(caller["peer_id"]) == 0, (
            f"close {n + 1} left its socket registered")
        await _until(lambda: _online_rows(owner, caller) == [], f"close {n + 1}'s presence row")
        await c.disconnect()
    assert [str(e) for e in daphne.raised] == [refused] * 3


@pytest.mark.asyncio
async def test_one_subscribe_frame_reads_at_most_the_cap_and_each_call_once(
        owner, world, tuned, monkeypatch):
    """S6 review F3: MAX_CALL_SUBSCRIPTIONS_PER_CONN bounded LIVE registrations, not work. An
    unknown call registers nothing, so a frame of 1200 unknown ids was read 1200 times; a
    finished call registers and unregisters in one step, so C's finished call named 1200 times
    was read and pushed 1200 times (and S7 replays chunks per proven subscription). Now one
    frame's `call:` channels are de-duplicated and at most the cap of them are read. The cap
    set to 2: five unknown ids, two reads and `subscribe:capped`; C's finished call named
    three times, one read and one terminal frame. The ack echoes every channel as sent."""
    tuned(reoffer_secs=NEVER)
    caller, target = world["caller"], world["target"]
    env = signed_call(caller, target, "s5.echo")
    call_id = env["payload"]["call_id"]
    await invoke(caller, target, "s5.echo", env)
    _commit_terminal(call_id, "timeout")
    frames = _caller_frames()
    read: list = []
    original = frames.read_call

    def counting(conn, *args, **kwargs):
        read.append(_named(args, kwargs))
        return original(conn, *args, **kwargs)

    monkeypatch.setattr(frames, "read_call", counting)
    monkeypatch.setattr(frames, "MAX_CALL_SUBSCRIPTIONS_PER_CONN", 2)
    c = await _connect(caller)
    try:
        before = _registry_module().CALL_REGISTRY.stats()
        assert await subscribe(c, *(f"call:s6-{uuid.uuid4()}" for _ in range(5))) == []
        assert len(read) == 2, read
        assert _delta(before, _registry_module().CALL_REGISTRY.stats(), "subscribe:") == {
            "subscribe:unknown_call": 2, "subscribe:capped": 1}
        read.clear()
        assert await subscribe(c, *[f"call:{call_id}"] * 3) == [
            {"type": "skill_timeout", "call_id": call_id}]
        assert read == [call_id], read
        await _pong(c)
    finally:
        await c.disconnect()


@pytest.mark.asyncio
async def test_a_terminal_committed_before_the_registration_is_pushed_by_the_reread(
        owner, world, tuned, monkeypatch):
    """S6 review F4, the window 4.7's re-read exists for: the terminal commits AFTER start_call
    returned the call in flight and BEFORE the handler registers it. A hook on
    `caller_frames._start_in_thread` commits it and waits until the bridge has decided its
    pointer, which finds no registration (`call:terminal:no_registration`): no delivery will
    come from the bridge, and the re-offer sweep is an hour away. Only the handler's re-read
    can deliver it: C gets skill_acked, then skill_timeout. CONTROL: a plain call is acked."""
    bridge = tuned(reoffer_secs=NEVER)
    caller, target = world["caller"], world["target"]
    frames = _caller_frames()
    original = frames._start_in_thread

    def then_terminal(*args, **kwargs):
        result, row = original(*args, **kwargs)
        before = bridge.stats()
        _commit_terminal(result.call_id, "timeout")
        _wait_for_terminal_decision(bridge, before)
        return result, row

    c = await _connect(caller)
    try:
        await acked(c, await call_over_ws(c, signed_call(caller, target, "s5.echo")))
        monkeypatch.setattr(frames, "_start_in_thread", then_terminal)
        before = bridge.stats()
        call_id = await call_over_ws(c, signed_call(caller, target, "s5.echo"))
        await acked(c, call_id)
        assert await _next(c) == {"type": "skill_timeout", "call_id": call_id}
        assert _delta(before, bridge.stats(), "call:terminal") == {
            "call:terminal:no_registration": 1}
        await _pong(c)
        assert not await _registered(call_id)
    finally:
        await c.disconnect()


def _waiting_on_a_lock(owner) -> bool:
    """A skill_call pipeline thread's connection is waiting on a lock (step 1's, held)."""
    return owner.execute(
        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND "
        "application_name LIKE 'meshy-ws-call-%' AND wait_event_type = 'Lock'"
    ).fetchone()[0] > 0


@pytest.mark.asyncio
async def test_a_socket_closed_mid_pipeline_registers_nothing_and_its_slot_waits_for_the_thread(
        owner, world, tuned, monkeypatch):
    """S6 review F5 and item 11 (MB). The consumer cancels a socket's spawned dispatches at
    disconnect, so a call whose pipeline is still running when its socket closes registers
    nothing for the dead socket; and the process slot is held until the pipeline THREAD ends,
    not the dispatch, since the thread holds a database connection until then. The process cap
    set to 1: C's call waits at step 1 (its lock held); C's socket closes; C's other socket's
    call is SKILL_CALL_BUSY (the server's text). Released: the first call is made (`queued`),
    the slot comes back, the other socket's next call is acked, and nothing is registered for
    the first call. CONTROL: a plain call is acked before the cap is lowered."""
    tuned(reoffer_secs=NEVER)
    caller, target = world["caller"], world["target"]
    frames = _caller_frames()
    env, later, last = (signed_call(caller, target, "s5.echo") for _ in range(3))
    call_id = env["payload"]["call_id"]
    c, c2 = await _connect(caller), await _connect(caller)
    closed = False
    try:
        await acked(c, await call_over_ws(c, signed_call(caller, target, "s5.echo")))
        slots = threading.BoundedSemaphore(1)
        monkeypatch.setattr(frames, "PIPELINE_SLOTS", slots)
        _hold(owner, [call_id])
        await call_over_ws(c, env)
        await _until(lambda: _waiting_on_a_lock(owner), "the pipeline thread to wait at step 1")
        await c.disconnect()                   # returns once the consumer's disconnect ran
        closed = True
        await call_over_ws(c2, later)
        assert await _next(c2) == {"type": "error", "code": "SKILL_CALL_BUSY",
                                   "message": frames.BUSY_SERVER}
        owner.execute("SELECT pg_advisory_unlock_all()")
        await _until(lambda: _status_or_none(owner, call_id) == "queued", "the call to be made")
        await _until(lambda: slots._value == 1, "the thread to end and give its slot back")
        await acked(c2, await call_over_ws(c2, last))
        await _pong(c2)
        assert not await _registered(call_id), "a closed socket's dispatch registered afterwards"
    finally:
        owner.execute("SELECT pg_advisory_unlock_all()")
        if not closed:
            await c.disconnect()
        await c2.disconnect()
    assert _status_or_none(owner, later["payload"]["call_id"]) is None


@pytest.mark.asyncio
async def test_a_call_terminal_at_return_whose_row_read_fails_is_acked_and_delivered(
        owner, world, authority, tuned, monkeypatch):
    """S6 review F9: a call TERMINAL when start_call returns (the memory authority runs it
    in-process) whose row the pipeline thread then fails to read was answered SKILL_CALL_FAILED
    E_INTERNAL, for a call that completed (its write applied). Now the call takes the
    registration path: registered, skill_acked, re-read on the socket's own connection, and
    its terminal pushed; nothing stays registered. CONTROL: the injected failure fired once,
    in the pipeline thread, and the row is `completed`."""
    tuned(reoffer_secs=NEVER)
    caller = world["caller"]
    frames = _caller_frames()
    original = frames.read_call
    failed: list = []

    def flaky(conn, *args, **kwargs):
        if threading.current_thread().name.startswith("meshy-ws-skill-call") and not failed:
            failed.append(_named(args, kwargs))
            raise psycopg.OperationalError("injected: the read after start_call failed")
        return original(conn, *args, **kwargs)

    monkeypatch.setattr(frames, "read_call", flaky)
    c = await _connect(caller)
    try:
        env = signed_call(caller, authority, "memory.write",
                          {"namespace": authority["namespace"], "key": "f9", "value": {"n": 9}},
                          version="0.2.0")
        call_id = await call_over_ws(c, env)
        await acked(c, call_id)
        got = await _next(c)
        payload, signature, kid = _result_columns(owner, call_id)
        assert got == {"type": "skill_result", "call_id": call_id, "status": "completed",
                       "envelope": {"payload": payload, "signature": signature,
                                    "signing_kid": kid}}, got
        await _pong(c)
        assert not await _registered(call_id)
    finally:
        await c.disconnect()
    assert failed == [call_id]
    assert _status(owner, call_id) == "completed"
