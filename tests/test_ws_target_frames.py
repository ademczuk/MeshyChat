"""Divergence 041 S4: the TARGET's frames, skill_ack, skill_running, skill_result and
skill_error, each one guarded statement with the active and live-key clauses.

Design decisions (fin/design-skill-frames, 2e229a6): section 7 row
S4, 4.7 (the frame table and its common rules), 5.1, 4.4 (the memory authority is never
answered over a socket), Q5, Q6 and Q7, critique F4 and F7, R12, 2.12.1 (the existence oracle
on target frames), and 2.12.2 (a result not bound to its frame).

THERE IS NO ROUTING YET (S5). Every call here is a row the bootstrap role inserts as `routed`
or `running`, as the target would find it once S5 delivers it. What is proved:

  T5   a stranger's ack, running, result and error for the target's call get the SAME code
       and the SAME message as the same frame for a call_id nobody has; the row and its
       events do not move; the server log tells the two apart; the target's own frames land.
  T6   a result signed for one of the target's calls, filed under another: refused, and
       neither row moves. The same envelope filed under its own call lands (control).
  T8   a result straight after the ack, with no running, is an InvalidTransition naming the
       status (the target owns the call, so it may be told); after running it lands. A
       duplicate is an idempotent success that writes nothing.
  T9   a target suspended by SQL, or whose bound key is revoked by SQL, sends nothing that
       lands in the window before the sweep closes its socket (4410, 4412); reconnected in
       good standing, it is accepted (control). A key inside its rotation grace window is
       live, by the rule divergence 030 applies, until the window ends.
  T19  (its target-frame arm) a call routed to the MEMORY AUTHORITY gets the uniform refusal
       on the authority's own socket; the same socket's frame lands once the setting names
       another peer (control).
  T21  a result's row carries result, status, error_code and error_message exactly as the
       target signed them, NULL when absent, checked with the fleet's own bound-field list
       (`tools/meshy_node/wire.RESULT_BOUND_FIELDS`) against the target's PUBLISHED key.
  Q6   a signed status other than completed or failed is refused SKILL_RESULT_BAD.
  and  the frames' parse and domain failures answered on the queue with the socket open;
       the socket's own connection, opened at the first target frame, reused, closed with
       the socket; and a frame stuck on the database answered within its bound.

Every socket runs against `meshychat.asgi.application` (the production consumer, registry
and bridges) with DPoP ENFORCED and real proofs, because the WS upgrade is the write boundary
(design section 6). The new error codes are literals: this file had to run red on the tree
that had no target frames.

HOW "NO REPLY" IS OBSERVED (rule C1). A target frame that succeeds produces no reply.
The consumer dispatches one frame at a time, so a ping sent after the frame is
answered after it: the pong as the NEXT frame means the frame produced no reply.

Run:  .venv/Scripts/python.exe -m pytest tests/test_ws_target_frames.py -q -p no:cacheprovider
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path

import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")
os.environ.setdefault("MESHY_TEST_DSN", DSN)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
os.environ.setdefault("MESHY_ALLOWED_HOSTS", "testserver,127.0.0.1,localhost")
os.environ.setdefault("MESHY_DSN", DSN)

import django  # noqa: E402
from django.conf import settings as dj_settings  # noqa: E402

if not dj_settings.configured:  # pragma: no cover - depends on collection order
    django.setup()

import jwt as pyjwt  # noqa: E402
from channels.testing import WebsocketCommunicator  # noqa: E402
from django.test import Client, override_settings  # noqa: E402

from apps.authapi import config as authconfig  # noqa: E402
from apps.authapi.didkey import to_did_key  # noqa: E402
from apps.crypto import primitives as P  # noqa: E402
from apps.wsgw import call_notify, mesh_notify  # noqa: E402
from apps.wsgw.connection_manager import MANAGER  # noqa: E402
from apps.wsgw.constants import NEGOTIATED_PROTOCOL, WS_PATH  # noqa: E402
from meshychat.asgi import application  # noqa: E402
from tests.fixtures.call_rows import call_row, seed_call, seed_peer, seed_skill  # noqa: E402
from tools.meshy_node import wire  # noqa: E402

ISSUER = "meshy-target-frames-test"
_server_key = ed25519.Ed25519PrivateKey.generate()
SERVER_PRIVATE_PEM = _server_key.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption())
SERVER_PUBLIC_PEM = _server_key.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)

#: The wire, as literals (see the module docstring).
UNIFORM = "call is not routed to this peer"
ACK_FAILED = "SKILL_ACK_FAILED"
RUNNING_FAILED = "SKILL_RUNNING_FAILED"
RESULT_FAILED = "SKILL_RESULT_FAILED"
ERROR_FAILED = "SKILL_ERROR_FAILED"
RESULT_BAD = "SKILL_RESULT_BAD"
ERROR_BAD = "SKILL_ERROR_BAD"
CLOSE_PEER_INACTIVE = 4410
CLOSE_KEY_REVOKED = 4412

#: A revocation sweep that never fires inside a test: the window R12 names stays open until
#: the test itself makes the bridge sweep.
NO_SWEEP = 3600.0
#: The application_name the socket's own connection carries (see the lifecycle test).
APP_NAME_PREFIX = "meshy-ws-frames-"

_ABSENT = object()


# ------------------------------------------------------------------------- fixtures

@pytest.fixture(autouse=True)
def server_config():
    """One server key mints every token here and verifies it, over WS and HTTP. DPoP is
    ENFORCED (conftest runs the suite with it off; section 6 wants it on here)."""
    with override_settings(
        ALLOWED_HOSTS=["*"],
        MESHY_JWT_PUBLIC_KEY_PEM=SERVER_PUBLIC_PEM.decode("ascii"),
        MESHY_JWT_ISSUER=ISSUER,
        MESHY_DPOP_ENFORCE=True,
        MESHY_WS_REVOCATION_SWEEP_SECS=NO_SWEEP,
    ), authconfig.override_config(jwt_signing_key_pem=SERVER_PRIVATE_PEM,
                                  jwt_public_key_pem=SERVER_PUBLIC_PEM,
                                  jwt_issuer=ISSUER):
        yield


@pytest.fixture(autouse=True)
def bridges_stopped():
    """The production bridges are bound to the loop that started them, and pytest-asyncio
    gives each test its own; the registry is process-wide (tests/test_call_bridge.py)."""
    yield
    call_notify.CALL_BRIDGE.stop()
    mesh_notify.BRIDGE.stop()
    MANAGER._by_peer.clear()


@pytest.fixture()
def owner():
    """The bootstrap role, autocommit: seeds, suspends, revokes and measures. Bypasses RLS."""
    with psycopg.connect(DSN, autocommit=True) as conn:
        yield conn


def _key(conn, peer_id) -> dict:
    seed = os.urandom(32)
    public = P.public_key_of(seed)
    kid = P.kid_for_peer_key(public)
    conn.execute("INSERT INTO peer_keys (peer_id, kid, public_key, active) "
                 "VALUES (%s,%s,%s,TRUE)", (peer_id, kid, P.b64u(public)))
    return {"seed": seed, "kid": kid, "did": to_did_key(public)}


def _peer(conn, tag: str, *, keys: int = 1) -> dict:
    peer = seed_peer(conn, tag)
    peer["keys"] = [_key(conn, peer["peer_id"]) for _ in range(keys)]
    return peer


@pytest.fixture()
def world(owner):
    """A caller C, a target T with two keys (the second signs when the first is revoked),
    a stranger X, and a skill of T's."""
    target = _peer(owner, "s4-target", keys=2)
    return {"caller": _peer(owner, "s4-caller"), "target": target,
            "stranger": _peer(owner, "s4-stranger"), "skill": seed_skill(owner, target)}


def _call(owner, w: dict, status: str, *, target: dict | None = None) -> str:
    return seed_call(owner, w["caller"], target or w["target"], w["skill"], status=status,
                     call_id=f"s4-{uuid.uuid4()}")


def _events(owner, call_id: str) -> list:
    return owner.execute("SELECT event_type, source, source_peer_id, details "
                         "FROM skill_call_events WHERE call_id = %s ORDER BY id",
                         (call_id,)).fetchall()


def _col(owner, call_id: str, *names: str) -> tuple:
    return owner.execute(f"SELECT {', '.join(names)} FROM skill_calls WHERE call_id = %s",
                         (call_id,)).fetchone()


# -------------------------------------------------------------------------- sockets

def _token(peer: dict, *, cnf: str | None = None, ttl: int = 600) -> str:
    now = int(time.time())
    claims = {"sub": str(peer["peer_id"]), "aud": "peer", "iss": ISSUER, "iat": now,
              "exp": now + ttl, "scopes": []}
    if cnf is not None:
        claims["cnf"] = cnf
    return pyjwt.encode(claims, SERVER_PRIVATE_PEM, algorithm="EdDSA")


async def _upgrade(peer: dict, key: dict | None = None, *,
                   ttl: int = 600) -> WebsocketCommunicator:
    """A socket on the REAL application, its token bound to `key` (the peer's first by
    default) with a fresh DPoP proof signed by it, as a node sends under enforcement.
    Accepted; nothing read after the handshake."""
    key = key or peer["keys"][0]
    token = _token(peer, cnf=key["did"], ttl=ttl)
    protocols = [NEGOTIATED_PROTOCOL, f"jwt.{token}",
                 f"dpop.{P.dpop_wire(key['seed'], 'WS_UPGRADE', WS_PATH, token)}"]
    comm = WebsocketCommunicator(application, WS_PATH, subprotocols=protocols)
    accepted, code = await comm.connect(timeout=15)
    assert accepted is True, f"the hub refused the socket ({code})"
    return comm


async def _connect(peer: dict, key: dict | None = None, *,
                   ttl: int = 600) -> WebsocketCommunicator:
    """`_upgrade`, and its `welcome` read: a registered socket.

    Since 041 S5 a target's connect redelivers its in-flight calls (design 4.8), so the
    `routed` and `running` rows a test seeded before connecting are offered to the socket as
    skill_invoke frames. They are read here, once the socket's redelivery has run, and kept
    in `comm.redelivered`, so every test's own frames start after them."""
    runs = call_notify.CALL_BRIDGE.stats().get("redelivery:runs", 0)
    comm = await _upgrade(peer, key, ttl=ttl)
    welcome = json.loads(await comm.receive_from(timeout=15))
    assert welcome["type"] == "welcome", welcome
    comm.connection_id = welcome["connection_id"]
    deadline = time.monotonic() + 15
    while call_notify.CALL_BRIDGE.stats().get("redelivery:runs", 0) <= runs:
        assert time.monotonic() < deadline, "the socket's connect-time redelivery never ran"
        await asyncio.sleep(0.02)
    await comm.send_json_to({"type": "ping"})
    comm.redelivered = []
    while (frame := json.loads(await comm.receive_from(timeout=15))) != {"type": "pong"}:
        assert frame.get("type") == "skill_invoke", f"expected a redelivered invoke: {frame}"
        comm.redelivered.append(frame["call_id"])
    return comm


async def _next(comm) -> dict:
    """The next frame, or a failure that says the socket CLOSED instead (the unchanged tree
    answers every skill frame UNIMPLEMENTED and closes with 1011)."""
    event = await comm.receive_output(timeout=15)
    if event.get("type") == "websocket.close":
        pytest.fail(f"the socket closed with {event.get('code')} instead of answering")
    assert event.get("type") == "websocket.send", event
    return json.loads(event["text"])


async def _send(comm, frame: dict) -> dict | None:
    """Send `frame`, then a ping. None when the pong comes first (the frame got no reply);
    else the reply, after which the pong must follow: the socket stays open."""
    await comm.send_json_to(frame)
    await comm.send_json_to({"type": "ping"})
    first = await _next(comm)
    if first == {"type": "pong"}:
        return None
    second = await _next(comm)
    assert second == {"type": "pong"}, f"after the reply {first}, expected the pong: {second}"
    return first


async def _send_past_offers(comm, frame: dict, call_id: str | tuple[str, ...]) -> dict | None:
    """`_send`, skipping skill_invoke offers of `call_id` itself (or of each id in a tuple). With
    several sockets held for one target, a live offer of a call already in flight can reach a
    socket after its connect-time redelivery was drained; on a loaded gate box one did, and `_send`
    took it for the reply (mg446c, issue #451). Offers of any other call, and any other frame,
    still fail."""
    skip = {call_id} if isinstance(call_id, str) else set(call_id)
    await comm.send_json_to(frame)
    await comm.send_json_to({"type": "ping"})
    replies = []
    while True:
        got = await _next(comm)
        if got.get("type") == "skill_invoke" and got.get("call_id") in skip:
            continue
        if got == {"type": "pong"}:
            break
        replies.append(got)
    assert len(replies) <= 1, f"more than one reply before the pong: {replies}"
    return replies[0] if replies else None


async def _frame(comm) -> dict:
    """The next frame the socket was sent, as JSON; a close first is a failure."""
    event = await comm.receive_output(timeout=15)
    assert event.get("type") == "websocket.send", event
    return json.loads(event["text"])


async def _settled(comm) -> None:
    """Disconnect and wait for the consumer to finish. Channels hands a consumer its messages
    one at a time, so every frame sent before this has been handled when it returns: a frame
    that got no reply, on a socket that no longer answers, is judged by the rows after it."""
    await comm.disconnect(timeout=15)


async def _closed_with(comm, code: int, timeout: float = 15.0) -> None:
    event = await comm.receive_output(timeout=timeout)
    assert event.get("type") == "websocket.close", f"expected a close, got {event}"
    assert event.get("code") == code, f"closed with {event.get('code')}, expected {code}"


async def _sweep_now() -> None:
    """Make the live bridge run its revocation sweep now: a (re)LISTEN sweeps every connected
    peer's standing BEFORE it reports listening (mesh_notify.py _run_once), so a restart is a
    sweep, with the timed sweep still held off by NO_SWEEP."""
    await asyncio.to_thread(mesh_notify.BRIDGE.stop)
    assert await mesh_notify.BRIDGE.ensure_started(), "the live bridge did not LISTEN again"


# --------------------------------------------------------------------------- frames

def ack(call_id: str) -> dict:
    return {"type": "skill_ack", "call_id": call_id}


def running(call_id: str) -> dict:
    return {"type": "skill_running", "call_id": call_id}


def error(call_id: str, code: str = "E_TARGET_FAILED",
          message: str = "the target could not run it") -> dict:
    return {"type": "skill_error", "call_id": call_id, "error_code": code,
            "error_message": message}


def signed_result(peer: dict, call_id: str, *, key: dict | None = None,
                  frame_call_id: str | None = None, signed_call_id=None, **fields) -> dict:
    """Build a `skill.result` frame a target sends: a signed payload
    (`{v, kind, call_id, target_peer_id, status, result, issued_at, nonce}`), signed with
    the peer's key. A field given as `_ABSENT` is left out of the payload; `signed_call_id`
    is `_ABSENT` for a payload that names no call at all."""
    key = key or peer["keys"][0]
    payload = {"v": 1, "kind": "skill.result", "call_id": call_id,
               "target_peer_id": str(peer["peer_id"]), "status": "completed",
               "result": {"ok": True, "answer": 42},
               "issued_at": _dt.datetime.now(_dt.timezone.utc).isoformat().replace(
                   "+00:00", "Z"),
               "nonce": str(uuid.uuid4())}
    if signed_call_id is _ABSENT:
        payload.pop("call_id")
    for name, value in fields.items():
        if value is _ABSENT:
            payload.pop(name, None)
        else:
            payload[name] = value
    return {"type": "skill_result", "call_id": frame_call_id or call_id,
            "envelope": {"payload": payload,
                         "signature": P.b64u(P.sign(key["seed"], P.jcs(payload))),
                         "signing_kid": key["kid"]}}


def _refusal(code: str, message: str = UNIFORM) -> dict:
    return {"type": "error", "code": code, "message": message}


# ============================================================================== T5

@pytest.mark.asyncio
async def test_t5_a_strangers_frame_gets_the_unknown_calls_refusal_and_moves_nothing(
        owner, world, caplog):
    """An unknown call or another peer's call must return one refusal per frame: otherwise any
    peer can probe a call_id. Only the server log
    tells them apart (Q7, the WS door). X's result is signed by X for T's call_id, so it
    passes verification and the binding: only the statement's `target_peer_id = me` stops
    it. CONTROL: T's own frames land on the same rows."""
    caplog.set_level(logging.INFO)
    w = world
    routed, on_run = _call(owner, w, "routed"), _call(owner, w, "running")
    unknown = f"s4-unknown-{uuid.uuid4()}"
    target, stranger = w["target"], w["stranger"]
    t, x = await _connect(target), await _connect(stranger)
    try:
        before = {c: (call_row(owner, c), _events(owner, c)) for c in (routed, on_run)}
        cases = [
            (ack(routed), ack(unknown), ACK_FAILED),
            (running(routed), running(unknown), RUNNING_FAILED),
            (signed_result(stranger, on_run), signed_result(stranger, unknown), RESULT_FAILED),
            (error(on_run), error(unknown), ERROR_FAILED),
        ]
        answered = []
        for theirs, nobodys, code in cases:
            on_theirs, on_nobodys = await _send(x, theirs), await _send(x, nobodys)
            assert on_theirs == _refusal(code), (theirs["type"], on_theirs)
            assert on_nobodys == on_theirs, (
                f"{theirs['type']}: a stranger could tell T's call from no call: "
                f"{on_theirs} vs {on_nobodys}")
            answered.append(theirs["type"])
        assert answered == ["skill_ack", "skill_running", "skill_result", "skill_error"]
        for call_id, (row, events) in before.items():
            assert call_row(owner, call_id) == row, f"a stranger moved {call_id}"
            assert _events(owner, call_id) == events, f"a stranger wrote an event on {call_id}"

        # Only the log tells them apart.
        logged = [r.getMessage() for r in caplog.records]
        for kind in ("skill_ack", "skill_running", "skill_result", "skill_error"):
            mine = [m for m in logged if kind in m and unknown in m]
            theirs = [m for m in logged if kind in m and (routed in m or on_run in m)]
            assert mine and "no such call" in mine[-1], (kind, mine)
            assert theirs and "another peer" in theirs[-1], (kind, theirs)

        # CONTROL: the target's own frames land, on the same rows.
        assert await _send(t, ack(routed)) is None
        assert await _send(t, running(routed)) is None
        assert await _send(t, signed_result(target, routed)) is None
        assert await _send(t, error(on_run)) is None
        assert _col(owner, routed, "status") == ("completed",)
        assert _col(owner, on_run, "status", "error_code", "error_message") == (
            "failed", "E_TARGET_FAILED", "the target could not run it")
        # Both terminals stamp completed_at, which the terminal body and the receipt carry
        # (review M41: skill_error without it survived).
        assert _col(owner, routed, "completed_at IS NOT NULL") == (True,)
        assert _col(owner, on_run, "completed_at IS NOT NULL") == (True,)
        tid = target["peer_id"]
        assert [e[:3] for e in _events(owner, routed)] == [
            ("target_acked", "target", tid), ("running", "target", tid),
            ("completed", "target", tid)]
        assert _events(owner, on_run) == [
            ("failed", "target", tid,
             {"code": "E_TARGET_FAILED", "message": "the target could not run it"})]
    finally:
        await t.disconnect()
        await x.disconnect()


# ============================================================================== T6

@pytest.mark.asyncio
async def test_t6_a_result_signed_for_one_call_and_filed_under_another_is_refused(
        owner, world):
    """2.12.2: binding only the signer to the socket would let a target file its signed result
    for B as the result of A, and A's row would then carry a signature
    over a different call (rule A3). The signed call_id must be the frame's. CONTROL: the same
    envelope, filed under its own call, lands; so the refusal was the binding, not the
    envelope."""
    w = world
    a, b = _call(owner, w, "running"), _call(owner, w, "running")
    t = await _connect(w["target"])
    try:
        rows = {c: call_row(owner, c) for c in (a, b)}
        for_b = signed_result(w["target"], b)
        filed_as_a = dict(for_b, call_id=a)
        reply = await _send(t, filed_as_a)
        assert reply is not None and reply["type"] == "error" and reply["code"] == RESULT_BAD, (
            reply)
        assert "call_id" in reply["message"], reply
        assert {c: call_row(owner, c) for c in (a, b)} == rows, "a mis-filed result moved a row"
        assert _events(owner, a) == [] and _events(owner, b) == []
        # A signed result that names NO call is bound to none: an optional binding would file
        # it under whichever running call of T's the frame names (review M13).
        reply = await _send(t, signed_result(w["target"], a, signed_call_id=_ABSENT))
        assert reply is not None and reply["code"] == RESULT_BAD, reply
        assert "call_id" in reply["message"], reply
        assert {c: call_row(owner, c) for c in (a, b)} == rows, "an unbound result moved a row"
        assert _events(owner, a) == [] and _events(owner, b) == []

        assert await _send(t, for_b) is None                   # CONTROL: its own call
        assert await _send(t, signed_result(w["target"], a)) is None
        assert _col(owner, b, "status", "result_payload")[0] == "completed"
        assert _col(owner, b, "result_payload")[0]["call_id"] == b
        assert _col(owner, a, "result_payload")[0]["call_id"] == a
    finally:
        await t.disconnect()


# ============================================================================== T8

@pytest.mark.asyncio
async def test_t8_a_result_before_running_is_an_invalid_transition_and_lands_after_it(
        owner, world):
    """The contract is ack, running, result. A result on a `routed` call
    is refused with the invalid-transition text, which names the status: the peer owns the
    call, so it may be told (Q7). After running it lands. A frame repeated after its edge is
    an idempotent success that writes nothing; a frame that
    names a different edge is an invalid transition."""
    w, target = world, world["target"]
    c, d = _call(owner, w, "routed"), _call(owner, w, "routed")
    t = await _connect(target)
    try:
        assert await _send(t, ack(c)) is None
        before = call_row(owner, c)
        reply = await _send(t, signed_result(target, c))
        assert reply == _refusal(RESULT_FAILED,
                                 "state transition: cannot move from routed to completed"), reply
        assert call_row(owner, c) == before, "a result landed on a routed call"
        assert await _send(t, error(c)) == _refusal(
            ERROR_FAILED, "state transition: cannot move from routed to failed")

        assert await _send(t, running(c)) is None
        assert await _send(t, signed_result(target, c, result={"n": 1})) is None
        landed = call_row(owner, c)
        assert _col(owner, c, "status", "result") == ("completed", {"n": 1})

        # Idempotent: the same edge again answers nothing and writes nothing.
        assert await _send(t, signed_result(target, c, result={"n": 2})) is None
        assert call_row(owner, c) == landed, "a duplicate result overwrote the first"
        # A different edge after the terminal: named, refused.
        assert await _send(t, running(c)) == _refusal(
            RUNNING_FAILED, "state transition: cannot move from completed to running")
        assert await _send(t, ack(c)) == _refusal(
            ACK_FAILED, "state transition: cannot move from completed to acked")
        assert await _send(t, error(c)) == _refusal(
            ERROR_FAILED, "state transition: cannot move from completed to failed")
        assert call_row(owner, c) == landed

        # running twice: the second is idempotent and moves nothing (started_at included).
        assert _col(owner, d, "started_at") == (None,)
        assert await _send(t, running(d)) is None
        assert _col(owner, d, "started_at IS NOT NULL") == (True,), "running set no started_at"
        started = call_row(owner, d)
        assert await _send(t, running(d)) is None
        assert call_row(owner, d) == started
        assert [e[0] for e in _events(owner, d)] == ["running"]
    finally:
        await t.disconnect()


# ============================================================================== T9

@pytest.mark.asyncio
@pytest.mark.parametrize("standing", ["suspended", "deleted"])
async def test_t9_a_suspended_target_is_refused_until_the_sweep_closes_it_4410(
        owner, world, standing):
    """R12: a suspension is plain SQL and sends no notification, so divergence 030 closes the
    socket only at the next sweep (10 s in production; held off here). In that window the
    statement's active clause refuses every frame, with the uniform refusal, and the rows do
    not move. Then the sweep closes the socket with 4410. CONTROL: reactivated and
    reconnected, T is accepted on the same rows.

    All four frames, skill_error included: it is one of the unsigned frames critique F4 names,
    and before the S4 review round only ack, running and result were sent here, so the active
    clause could be dropped from skill_error alone (review M1). And both standings that are not
    `active` (`sql/001:24`), since `status <> 'suspended'` passed `suspended` alone (M14)."""
    w, target = world, world["target"]
    routed, on_run = _call(owner, w, "routed"), _call(owner, w, "running")
    on_run_err = _call(owner, w, "running")
    t = await _connect(target)
    back = None
    try:
        assert await mesh_notify.BRIDGE.wait_listening(10), "the live bridge never LISTENed"
        owner.execute("UPDATE peers SET status = %s WHERE id = %s",
                      (standing, target["peer_id"]))
        calls = (routed, on_run, on_run_err)
        rows = {c: (call_row(owner, c), _events(owner, c)) for c in calls}
        assert await _send(t, ack(routed)) == _refusal(ACK_FAILED)
        assert await _send(t, running(routed)) == _refusal(RUNNING_FAILED)
        assert await _send(t, signed_result(target, on_run)) == _refusal(RESULT_FAILED)
        assert await _send(t, error(on_run_err)) == _refusal(ERROR_FAILED)
        assert {c: (call_row(owner, c), _events(owner, c)) for c in rows} == rows

        await _sweep_now()
        await _closed_with(t, CLOSE_PEER_INACTIVE)

        owner.execute("UPDATE peers SET status = 'active' WHERE id = %s", (target["peer_id"],))
        back = await _connect(target)
        assert await _send(back, ack(routed)) is None
        assert await _send(back, running(routed)) is None
        assert await _send(back, signed_result(target, on_run)) is None
        assert await _send(back, error(on_run_err)) is None
        assert _col(owner, routed, "status") == ("running",)
        assert _col(owner, on_run, "status") == ("completed",)
        assert _col(owner, on_run_err, "status") == ("failed",)
    finally:
        await t.disconnect()
        if back is not None:
            await back.disconnect()


@pytest.mark.asyncio
async def test_t9_a_target_whose_bound_key_is_revoked_is_refused_until_the_4412_close(
        owner, world):
    """Critique F4: skill_ack, skill_running and skill_error carry no signature, so nothing
    but the statement stops a socket whose token's key was revoked, in the window before 030's
    4412 close. The result here is signed with T's OTHER, live key: verification passes, and
    only the live-key clause refuses it. CONTROL: T reconnects bound to that live key and is
    accepted on the same rows."""
    w, target = world, world["target"]
    bound, other = target["keys"]
    routed, on_run = _call(owner, w, "routed"), _call(owner, w, "running")
    t = await _connect(target, bound)
    back = None
    try:
        assert await mesh_notify.BRIDGE.wait_listening(10), "the live bridge never LISTENed"
        owner.execute("UPDATE peer_keys SET active = FALSE, revoked_at = NOW() "
                      "WHERE peer_id = %s AND kid = %s", (target["peer_id"], bound["kid"]))
        rows = {c: (call_row(owner, c), _events(owner, c)) for c in (routed, on_run)}
        assert await _send(t, ack(routed)) == _refusal(ACK_FAILED)
        assert await _send(t, running(routed)) == _refusal(RUNNING_FAILED)
        assert await _send(t, signed_result(target, on_run, key=other)) == _refusal(
            RESULT_FAILED)
        assert await _send(t, error(on_run)) == _refusal(ERROR_FAILED)
        assert {c: (call_row(owner, c), _events(owner, c)) for c in rows} == rows

        await _sweep_now()
        await _closed_with(t, CLOSE_KEY_REVOKED)

        back = await _connect(target, other)
        assert await _send(back, ack(routed)) is None
        assert await _send(back, running(routed)) is None
        assert await _send(back, signed_result(target, on_run, key=other)) is None
        assert _col(owner, routed, "status") == ("running",)
        assert _col(owner, on_run, "status") == ("completed",)
    finally:
        await t.disconnect()
        if back is not None:
            await back.disconnect()


@pytest.mark.asyncio
async def test_t9_a_bound_key_inside_its_rotation_grace_window_still_answers(owner, world):
    """The live-key clause is `revocation.key_states`' rule, the grace window included: a key
    rotated out with `revoked_at` in the future is live until then, and divergence 030 keeps
    its socket open meanwhile. So its frames land. CONTROL: once the window has ended, the
    same socket's next frame is refused, before any sweep."""
    w, target = world, world["target"]
    bound = target["keys"][0]
    routed = _call(owner, w, "routed")
    t = await _connect(target, bound)
    try:
        owner.execute("UPDATE peer_keys SET active = FALSE, revoked_at = NOW() + interval "
                      "'1 hour' WHERE peer_id = %s AND kid = %s",
                      (target["peer_id"], bound["kid"]))
        assert await _send(t, ack(routed)) is None
        owner.execute("UPDATE peer_keys SET revoked_at = NOW() - interval '1 second' "
                      "WHERE peer_id = %s AND kid = %s", (target["peer_id"], bound["kid"]))
        assert await _send(t, running(routed)) == _refusal(RUNNING_FAILED)
        assert _col(owner, routed, "status") == ("routed",)
    finally:
        await t.disconnect()


@pytest.mark.asyncio
async def test_t9_the_bound_key_live_under_another_peer_does_not_keep_the_socket_live(
        owner, world):
    """`peer_keys` is unique only on (peer_id, kid) (`sql/002`), so the SAME public key can be
    live under a second peer: whoever holds the private half of a revoked key can publish it
    again elsewhere. The live-key clause asks for a live row of THIS peer's; without its
    `k.peer_id = me` the stranger's row would keep T's revoked socket answering (review M3).
    CONTROL: once T's own row is live again, the same socket's frame lands."""
    w, target, stranger = world, world["target"], world["stranger"]
    bound = target["keys"][0]
    public = P.public_key_of(bound["seed"])
    routed = _call(owner, w, "routed")
    t = await _connect(target, bound)
    try:
        assert await mesh_notify.BRIDGE.wait_listening(10), "the live bridge never LISTENed"
        owner.execute("UPDATE peer_keys SET active = FALSE, revoked_at = NOW() "
                      "WHERE peer_id = %s AND kid = %s", (target["peer_id"], bound["kid"]))
        owner.execute("INSERT INTO peer_keys (peer_id, kid, public_key, active) "
                      "VALUES (%s,%s,%s,TRUE)",
                      (stranger["peer_id"], bound["kid"], P.b64u(public)))
        live = owner.execute(
            "SELECT peer_id FROM peer_keys WHERE public_key = %s AND active "
            "AND revoked_at IS NULL", (P.b64u(public),)).fetchall()
        assert live == [(stranger["peer_id"],)], f"the premise: live under the stranger only {live}"
        assert await _send(t, running(routed)) == _refusal(RUNNING_FAILED)
        assert _col(owner, routed, "status") == ("routed",)
        assert _events(owner, routed) == []

        owner.execute("UPDATE peer_keys SET active = TRUE, revoked_at = NULL "
                      "WHERE peer_id = %s AND kid = %s", (target["peer_id"], bound["kid"]))
        assert await _send(t, running(routed)) is None                         # CONTROL
        assert _col(owner, routed, "status") == ("running",)
    finally:
        await t.disconnect()


# ============================================================================= T19

@pytest.mark.asyncio
async def test_t19_the_memory_authority_gets_the_uniform_refusal_for_a_call_routed_to_it(
        owner, world):
    """4.4: a memory call runs IN-PROCESS between pending and its terminal. If the memory
    daemon ever connected here, its frames could race that run (a result could win the
    running-to-terminal edge). So every target statement excludes the authority, and its
    socket gets the uniform refusal. CONTROL: the same socket's frames land once the setting
    names another peer, so the refusal was the authority predicate."""
    w = world
    authority = _peer(owner, "s4-authority")
    routed = _call(owner, w, "routed", target=authority)
    on_run = _call(owner, w, "running", target=authority)
    a = await _connect(authority)
    try:
        with override_settings(MESHY_MEMORY_PEER_ID=str(authority["peer_id"])):
            rows = {c: (call_row(owner, c), _events(owner, c)) for c in (routed, on_run)}
            assert await _send(a, ack(routed)) == _refusal(ACK_FAILED)
            assert await _send(a, running(routed)) == _refusal(RUNNING_FAILED)
            assert await _send(a, signed_result(authority, on_run)) == _refusal(RESULT_FAILED)
            assert await _send(a, error(on_run)) == _refusal(ERROR_FAILED)
            assert {c: (call_row(owner, c), _events(owner, c)) for c in rows} == rows
        with override_settings(MESHY_MEMORY_PEER_ID=str(uuid.uuid4())):
            assert await _send(a, ack(routed)) is None
            assert await _send(a, running(routed)) is None
            assert await _send(a, signed_result(authority, on_run)) is None
        assert _col(owner, routed, "status") == ("running",)
        assert _col(owner, on_run, "status") == ("completed",)
    finally:
        await a.disconnect()


# ============================================================================= T21

def _get(path: str, peer: dict | None = None):
    headers = {"authorization": f"Bearer {_token(peer)}"} if peer else {}
    response = Client().get(path, headers=headers)
    assert response.status_code == 200, (path, response.status_code, response.content[:300])
    return json.loads(response.content)


@pytest.mark.asyncio
async def test_t21_the_row_carries_exactly_what_the_target_signed(owner, world):
    """Critique F7. The fleet's verifier binds status, call_id, result, error_code and
    error_message to the row (`wire.RESULT_BOUND_FIELDS`). Each row field is copied from the
    SIGNED payload, NULL when the payload has none, including a failed row's error pair. The
    completed status retains its signed values rather than silently replacing them with NULL.
    Checked through the caller's own read (GET /v1/calls/{id}) against
    T's PUBLISHED key (GET /agents/{T}/jwks.json), with `wire.result_signature_ok`.
    CONTROL: an error_message changed by SQL afterwards is caught by the same check."""
    caller = world["caller"]
    target = _peer(owner, "s4-solo")        # ONE key: the one its JWKS publishes
    w = dict(world, target=target, skill=seed_skill(owner, target))
    cases = {
        "failed, with the error pair": dict(
            status="failed", result={"ok": False}, error_code="E_REMOTE",
            error_message="the remote skill failed"),
        "failed, without the error pair": dict(status="failed", result={"ok": False}),
        "completed, with an error pair signed": dict(
            status="completed", error_code="W_PARTIAL", error_message="two rows skipped"),
        "completed, with no result at all": dict(status="completed", result=_ABSENT),
    }
    calls = {label: _call(owner, w, "running") for label in cases}
    t = await _connect(target)
    try:
        signed = {}
        for label, fields in cases.items():
            frame = signed_result(target, calls[label], **fields)
            signed[label] = frame["envelope"]["payload"]
            assert await _send(t, frame) is None, label
    finally:
        await t.disconnect()

    jwks = await asyncio.to_thread(_get, f"/agents/{target['peer_id']}/jwks.json")
    assert [k.get("kid") for k in jwks.get("keys", [])] == [target["keys"][0]["kid"]], jwks
    for label, call_id in calls.items():
        call = await asyncio.to_thread(_get, f"/v1/calls/{call_id}", caller)
        payload = signed[label]
        assert call["result_payload"] == payload, label
        for name in wire.RESULT_BOUND_FIELDS:
            assert call[name] == payload.get(name), (label, name, call[name])
        assert call["result_signing_kid"] == target["keys"][0]["kid"], label
        assert wire.result_signature_ok(call, jwks) == (True, "ok"), label

    assert _col(owner, calls["completed, with no result at all"], "result IS NULL") == (True,)
    # CONTROL: the check bites on a row that no longer says what was signed.
    tampered = calls["failed, with the error pair"]
    owner.execute("UPDATE skill_calls SET error_message = 'rewritten' WHERE call_id = %s",
                  (tampered,))
    call = await asyncio.to_thread(_get, f"/v1/calls/{tampered}", caller)
    assert wire.result_signature_ok(call, jwks) == (
        False, "signed error_message differs from the call's error_message")


# ============================================================================== Q6

@pytest.mark.asyncio
async def test_q6_a_signed_status_other_than_completed_or_failed_is_refused(owner, world):
    """2.12.8: an unrecognised signed status must not become `completed`, which would create a
    row that contradicts the stored signature (rule E2). Q6 requires SKILL_RESULT_BAD, and the
    call stays running. CONTROLS: an absent status is `completed`, and `failed` is failed."""
    w, target = world, world["target"]
    stays = _call(owner, w, "running")
    absent, failed = _call(owner, w, "running"), _call(owner, w, "running")
    t = await _connect(target)
    try:
        row = call_row(owner, stays)
        refused = []
        for status in ("succeeded", "timeout", "COMPLETED", None, 1, True, ["completed"]):
            reply = await _send(t, signed_result(target, stays, status=status))
            assert reply is not None and reply["code"] == RESULT_BAD, (status, reply)
            refused.append(status)
        assert len(refused) == 7
        assert call_row(owner, stays) == row, "an unknown signed status moved the call"
        assert _events(owner, stays) == []

        assert await _send(t, signed_result(target, absent, status=_ABSENT)) is None
        assert await _send(t, signed_result(target, failed, status="failed")) is None
        assert _col(owner, absent, "status") == ("completed",)
        assert _col(owner, failed, "status") == ("failed",)
        # The event names the terminal the row took: a signed failure's event is `failed`.
        tid = target["peer_id"]
        assert _events(owner, failed) == [("failed", "target", tid, None)]
        assert _events(owner, absent) == [("completed", "target", tid, None)]
    finally:
        await t.disconnect()


# ============================================================= parse and domain, on the queue

@pytest.mark.asyncio
async def test_a_bad_target_frame_is_answered_on_the_queue_and_the_socket_stays_open(
        owner, world):
    """A target frame is no longer refused UNIMPLEMENTED with a close: a shape failure is
    PARSE, a value out of its domain its frame's `_BAD` code, a signature failure its E_SIG
    code, each through the send queue with the socket open (the pong follows each). A signed
    payload holding U+0000 is refused before the database, which cannot store it in jsonb."""
    w, target = world, world["target"]
    on_run = _call(owner, w, "running")
    t = await _connect(target)
    try:
        row = call_row(owner, on_run)
        answers = {}
        for label, frame in {
            "ack, no call_id": {"type": "skill_ack"},
            "running, a call_id with a newline": running("a\nforged log line"),
            "result, no envelope": {"type": "skill_result", "call_id": on_run},
            "error, no message": {"type": "skill_error", "call_id": on_run,
                                  "error_code": "E_X"},
        }.items():
            reply = await _send(t, frame)
            assert reply is not None and reply["code"] == "PARSE", (label, reply)
            answers[label] = reply["code"]
        reply = await _send(t, error(on_run, code="E" * 129))
        assert reply is not None and reply["code"] == ERROR_BAD, reply
        reply = await _send(t, error(on_run, message="m" * 4097))
        assert reply is not None and reply["code"] == ERROR_BAD, reply
        stranger_signed = signed_result(target, on_run, key=w["stranger"]["keys"][0])
        reply = await _send(t, stranger_signed)
        assert reply is not None and reply["code"] == "E_SIG_BAD", reply
        # The stranger's OWN valid result (its key, its peer id) replayed on T's socket for T's
        # call: verified against the SOCKET's peer, it is not T's to file.
        reply = await _send(t, signed_result(w["stranger"], on_run))
        assert reply is not None and reply["code"] == "E_SIG_IDENTITY_MISMATCH", reply
        reply = await _send(t, signed_result(target, on_run, result={"text": "a\x00b"}))
        assert reply is not None and reply["code"] == RESULT_BAD, reply
        # A signed error field that is not a string: the column is text, and a number would be
        # stored as its decimal spelling, a row that no longer says what was signed (review
        # M12).
        for name, value in (("error_code", 7), ("error_message", ["m"])):
            reply = await _send(t, signed_result(target, on_run, status="failed",
                                                 **{name: value}))
            assert reply is not None and reply["code"] == RESULT_BAD, (name, reply)
            assert name in reply["message"], (name, reply)
        assert len(answers) == 4
        assert call_row(owner, on_run) == row
        # CONTROL: the same socket, a good frame.
        assert await _send(t, error(on_run, code="E" * 128, message="m" * 4096)) is None
        assert _col(owner, on_run, "status") == ("failed",)
    finally:
        await t.disconnect()


# ================================================================ the socket's own connection

def _sessions(owner, connection_id: str) -> list:
    return owner.execute("SELECT pid, backend_start FROM pg_stat_activity "
                         "WHERE application_name = %s",
                         (APP_NAME_PREFIX + connection_id,)).fetchall()


@pytest.mark.asyncio
async def test_the_socket_opens_one_connection_at_its_first_target_frame_and_closes_it(
        owner, world):
    """Design 4.7: each socket lazily opens ONE role-bound autocommit connection for its
    target frames and closes it on disconnect. Observed in pg_stat_activity by the
    application_name that names the socket. None before the first target frame (a ping
    opens none), the SAME session across four frames, and none once the socket is gone."""
    w, target = world, world["target"]
    routed = _call(owner, w, "routed")
    t = await _connect(target)
    closed = False
    try:
        await t.send_json_to({"type": "ping"})
        assert await _next(t) == {"type": "pong"}
        assert _sessions(owner, t.connection_id) == [], "a connection before any target frame"
        assert await _send(t, ack(routed)) is None
        first = _sessions(owner, t.connection_id)
        assert len(first) == 1, first
        assert await _send(t, running(routed)) is None
        assert await _send(t, ack(f"s4-unknown-{uuid.uuid4()}")) == _refusal(ACK_FAILED)
        assert await _send(t, signed_result(target, routed)) is None
        assert _sessions(owner, t.connection_id) == first, "the socket opened a second session"
        await t.disconnect()
        closed = True
        # 20 s, not 5: the close runs after the disconnect, and on a loaded gate box it had
        # not finished at 5 s (mg446c, issue #451). The poll stays 0.05 s, so a fast close
        # still passes at once.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 20.0
        while loop.time() < deadline:
            if not _sessions(owner, t.connection_id):
                break
            await asyncio.sleep(0.05)
        assert _sessions(owner, t.connection_id) == [], "the connection outlived its socket"
    finally:
        if not closed:
            await t.disconnect()


@pytest.mark.asyncio
async def test_a_frame_stuck_on_a_locked_row_is_answered_within_its_bound(
        owner, world, monkeypatch):
    """S3 review F1's lesson, applied to the socket's connection: a statement that waits on a
    row another transaction holds gives up at its statement bound and is answered with the
    frame's code and a message that says nothing about the call; the next frame opens a fresh
    connection and lands. The bound is shortened here; the production value is pinned."""
    from apps.wsgw import target_frames
    assert target_frames.STATEMENT_TIMEOUT_MS == 5000
    monkeypatch.setattr(target_frames, "STATEMENT_TIMEOUT_MS", 300)
    w, target = world, world["target"]
    routed = _call(owner, w, "routed")
    t = await _connect(target)
    try:
        assert await _send(t, ack(routed)) is None
        before = _sessions(owner, t.connection_id)
        assert len(before) == 1, before
        with psycopg.connect(DSN) as holder:
            holder.execute("SELECT 1 FROM skill_calls WHERE call_id = %s FOR UPDATE", (routed,))
            started = time.monotonic()
            reply = await _send(t, running(routed))
            waited = time.monotonic() - started
            holder.rollback()
        assert reply == _refusal(RUNNING_FAILED, "internal error"), reply
        assert waited < 3.0, f"the frame waited {waited:.2f}s on the locked row"
        assert _col(owner, routed, "status") == ("routed",)
        # The connection that raised is not trusted again: it is closed at once (review M17).
        for _ in range(100):
            if not _sessions(owner, t.connection_id):
                break
            await asyncio.sleep(0.05)
        assert _sessions(owner, t.connection_id) == [], "the failed connection was kept"
        assert await _send(t, running(routed)) is None
        assert _col(owner, routed, "status") == ("running",)
        after = _sessions(owner, t.connection_id)
        assert len(after) == 1 and after[0][0] != before[0][0], (before, after)
    finally:
        await t.disconnect()


# ======================================================== the S4 review round (code findings)

@pytest.mark.asyncio
async def test_a_socket_refused_at_the_per_peer_cap_dispatches_nothing(owner, world):
    """Review finding 1. The per-peer cap is checked AFTER the upgrade, so
    the refused socket is accepted, told TOO_MANY_CONNECTIONS and closed 1013, and it never
    gets a handle. Its frames still ran: with no handle the bound key read as None, so the
    live-key clause was left out, and a frame whose token's key had been revoked landed. A
    socket the hub refused is not a socket: it dispatches nothing. CONTROLS: a REGISTERED
    socket bound to the same revoked key is refused by the clause, and once the key is live
    again the same registered socket's frame lands on the same row."""
    from apps.wsgw.constants import CLOSE_TOO_MANY_CONNECTIONS, MAX_CONNS_PER_PEER
    w, target = world, world["target"]
    bound = target["keys"][0]
    routed = _call(owner, w, "routed")
    held = [await _connect(target, bound) for _ in range(MAX_CONNS_PER_PEER)]
    try:
        capped = await _upgrade(target, bound)
        refusal = await _frame(capped)
        assert refusal["code"] == "TOO_MANY_CONNECTIONS", refusal
        await _closed_with(capped, CLOSE_TOO_MANY_CONNECTIONS)
        assert await mesh_notify.BRIDGE.wait_listening(10), "the live bridge never LISTENed"
        owner.execute("UPDATE peer_keys SET active = FALSE, revoked_at = NOW() "
                      "WHERE peer_id = %s AND kid = %s", (target["peer_id"], bound["kid"]))
        await capped.send_json_to(ack(routed))
        await capped.send_json_to(running(routed))
        await _settled(capped)
        assert _col(owner, routed, "status", "started_at") == ("routed", None), (
            "a socket refused at the cap moved the call")
        assert _events(owner, routed) == [], "a socket refused at the cap wrote an event"

        assert await _send_past_offers(held[0], running(routed), routed) == _refusal(
            RUNNING_FAILED)                                                       # CONTROL
        owner.execute("UPDATE peer_keys SET active = TRUE, revoked_at = NULL "
                      "WHERE peer_id = %s AND kid = %s", (target["peer_id"], bound["kid"]))
        assert await _send_past_offers(held[0], running(routed), routed) is None  # CONTROL
        assert _col(owner, routed, "status") == ("running",)
    finally:
        for comm in held:
            await comm.disconnect()


@pytest.mark.asyncio
async def test_a_socket_the_server_closed_dispatches_nothing_after_its_close(owner, world):
    """Review finding 1, the other half. The server's close was on its way, but the socket
    still read and ran frames until the client's close came back: after the 1011 close of a
    frame that is not built yet, and after the 4401 close at the token's expiry, for a session
    that may not outlive its token. Every close the server starts stops the reader.
    CONTROL: a fresh socket's frames land on the same rows."""
    from apps.wsgw.constants import CLOSE_CREDENTIAL_EXPIRED, CLOSE_UNIMPLEMENTED
    w, target = world, world["target"]
    after_1011, after_4401 = _call(owner, w, "routed"), _call(owner, w, "routed")

    t = await _connect(target)
    # skill_resume: a frame that stays unbuilt (design 2.10); skill_cancel was used here until
    # divergence 041 S6 gave it a handler.
    await t.send_json_to({"type": "skill_resume", "call_id": after_1011, "context_id": "x",
                          "action": "accept", "continuation_token": "t"})
    reply = await _frame(t)
    assert reply["code"] == "UNIMPLEMENTED", reply
    await _closed_with(t, CLOSE_UNIMPLEMENTED)
    await t.send_json_to(running(after_1011))
    await _settled(t)

    # The token must outlive _connect's welcome, redelivery wait and ping/pong. ttl=2 did not on
    # gate mg613's loaded box (12 Nov): the 4401 close came inside _connect.
    expiring = await _connect(target, ttl=8)
    # In those 8 s the re-offer sweep may offer this test's own routed calls to the live socket before the expiry
    # closes it (gate mg615, 12 Nov: "expected a close, got ... skill_invoke"). Offers BEFORE the close are allowed;
    # what this test pins is that nothing the client sends AFTER the close moves a call. So skip re-offers of the two
    # calls, and nothing else, until the close.
    while True:
        event = await expiring.receive_output(timeout=15)
        if event.get("type") == "websocket.send":
            frame = json.loads(event["text"])
            assert frame.get("type") == "skill_invoke" and frame.get("call_id") in (after_1011, after_4401), frame
            continue
        assert event.get("type") == "websocket.close", f"expected a close, got {event}"
        assert event.get("code") == CLOSE_CREDENTIAL_EXPIRED, event
        break
    await expiring.send_json_to(running(after_4401))
    await _settled(expiring)

    after = {"after 1011": _col(owner, after_1011, "status")[0],
             "after 4401": _col(owner, after_4401, "status")[0]}
    assert after == {"after 1011": "routed", "after 4401": "routed"}, (
        f"a socket the server closed moved a call: {after}")
    assert _events(owner, after_1011) == [] and _events(owner, after_4401) == []

    # CONTROL. The re-offer sweep may offer the two still-routed calls to this fresh socket before
    # its frames are read, as it may to `expiring` above; `_send` took such an offer for the reply
    # (gate mgu12, 17 Nov: "after the reply {skill_invoke ...}, expected the pong"). So skip offers of
    # these two calls, and nothing else: any other frame before the pong still fails.
    fresh = await _connect(target)
    both = (after_1011, after_4401)
    try:
        assert await _send_past_offers(fresh, running(after_1011), both) is None
        assert await _send_past_offers(fresh, running(after_4401), both) is None
        assert _col(owner, after_1011, "status") == ("running",)
        assert _col(owner, after_4401, "status") == ("running",)
    finally:
        await fresh.disconnect()


@pytest.mark.asyncio
async def test_a_consumer_with_no_handle_dispatches_nothing_even_before_a_close():
    """Review finding 1, the handle gate on its own. Today every path that leaves an accepted
    socket without a handle closes it first, so the closing flag alone already stops its
    reader, and the socket tests above cannot tell the two gates apart. This one can: a
    consumer never registered and never closed answers nothing, not even a ping, so a later
    path that refuses registration and closes afterwards (or not at all) still dispatches
    nothing, and never with the bound key read as None. CONTROL: given its handle, the same
    consumer answers the same ping."""
    from apps.wsgw.connection_manager import ConnectionHandle, ConnectionManager
    from apps.wsgw.consumer import HubConfig, MeshyWebSocketConsumer
    from apps.wsgw.presence import RecordingPresenceBackend
    config = HubConfig(jwt_public_key_pem=SERVER_PUBLIC_PEM, jwt_issuer=ISSUER,
                       dpop_enforce=False, check_peer_active=False, zero_rtt=False)
    manager = ConnectionManager()
    consumer = MeshyWebSocketConsumer(config=config, manager=manager,
                                     presence=RecordingPresenceBackend())
    consumer.peer_id = uuid.uuid4()
    try:
        await consumer.receive(text_data=json.dumps({"type": "ping"}))
        await consumer.receive(text_data=json.dumps(ack(f"s4-unknown-{uuid.uuid4()}")))
        assert len(consumer.queue) == 0, "a consumer with no handle dispatched a frame"
        assert consumer.frames_db._conn is None, "a consumer with no handle opened a connection"

        handle = ConnectionHandle(connection_id=consumer.connection_id,
                                  peer_id=consumer.peer_id, queue=consumer.queue)
        await manager.register(handle)
        consumer.handle = handle
        await consumer.receive(text_data=json.dumps({"type": "ping"}))          # CONTROL
        assert (await asyncio.wait_for(consumer.queue.pop(), 5)) == {"type": "pong"}
        await manager.deregister(consumer.peer_id, consumer.connection_id)
    finally:
        await asyncio.to_thread(consumer.frames_db.close)


@pytest.mark.asyncio
async def test_a_signed_result_with_attachments_is_refused_not_dropped(owner, world):
    """Review finding 5. `result_attachments` is not validated against the skill's
    `returns_attachments`, the target's owner, and the signed `_attachments` manifest. The
    row's `result_attachments` decides who may read a file, so
    storing the list unvalidated would let a target hand the caller any file id. S4 stored
    nothing and said nothing: the call completed with `{}` while its signed payload named
    files. It is refused with SKILL_RESULT_BAD, like any signed field that cannot be stored
    under Q6's rule, and the call stays running. A value that is not a list is refused too.
    CONTROL: an empty list asks for nothing and lands."""
    w, target = world, world["target"]
    stays, empty = _call(owner, w, "running"), _call(owner, w, "running")
    t = await _connect(target)
    try:
        row = call_row(owner, stays)
        for value in ([str(uuid.uuid4())], "not-a-list", [None], {}, None):
            reply = await _send(t, signed_result(target, stays, result_attachments=value))
            assert reply is not None and reply["code"] == RESULT_BAD, (value, reply)
            assert "result_attachments" in reply["message"], (value, reply)
        assert call_row(owner, stays) == row, "a result with attachments moved the call"
        assert _events(owner, stays) == []

        assert await _send(t, signed_result(target, empty, result_attachments=[])) is None
        assert _col(owner, empty, "status", "result_attachments") == ("completed", [])
    finally:
        await t.disconnect()


def test_the_live_key_rule_is_one_parenthesised_expression(owner):
    """Review finding 8. `revocation.key_live_sql` is an OR; returned bare, a caller that wrote
    `... AND <it>` would read `(... AND a) OR b`, and a key inside its grace window would
    satisfy a clause meant to be false. It is one parenthesised expression. CONTROL: the rule
    alone, on the same three rows."""
    from apps.wsgw import revocation
    rows = ("(VALUES (1, NULL::timestamptz, TRUE), (2, NOW() + interval '1 hour', FALSE), "
            "        (3, NOW() - interval '1 hour', FALSE)) AS k(n, revoked_at, active)")
    rule = revocation.key_live_sql("k")
    alone = owner.execute(f"SELECT {rule} FROM {rows} ORDER BY n").fetchall()
    assert alone == [(True,), (True,), (False,)], alone                         # CONTROL
    composed = owner.execute(f"SELECT FALSE AND {rule} FROM {rows} ORDER BY n").fetchall()
    assert composed == [(False,), (False,), (False,)], composed
