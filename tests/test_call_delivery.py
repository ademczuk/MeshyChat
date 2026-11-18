"""Divergence 041 S5: step 9 routes a remote call, and the CallBridge delivers it to the target.

Design `docs/design/skill-frames.md`, section 7 row S5: 4.4 (step 9 for a remote target), 4.6 (the CallBridge's
arms and sweeps), 4.8 (redelivery), 4.9 (the delivery guarantees), 4.10 (a call to an offline
target), the approved Q1, Q14 and Q17, critique F8 and F16, and defect 2.12.3 (redelivery
published the invoke before it advanced the row).

THE CALLER IS HTTP IN THIS SLICE. Section 7: "after S5, HTTP caller to WS target works end to
end by polling". The WS skill_call, the CallRegistry and the caller's terminal frames are S6's,
so T12, T13, T15 and T23, which the design writes with a WS caller socket, prove their row and
target halves here with a caller that polls GET /v1/calls/{id}; S6 adds the caller-socket
halves. T12 and T23 are in tests/test_call_worker.py.

  T1   an HTTP invoke from C to a connected T is `routed`; T gets exactly one skill_invoke,
       built from the row, whose envelope verifies under C's key; T acks, runs and answers;
       C's poll reads `completed` with T's signed payload, checked against T's published key.
  Q1   a SYNC invoke to a remote target answers 202 with Location at the default wait.
  4.4  an offline target's call is `queued` with its event, and nothing else is written.
  4.4  a pending edge lost to redelivery: step 9 reports the `routed` it found, no second event.
  T24  a dashboard_test call to a remote target fails E_UNVERIFIED_CALL at step 9; T, connected,
       gets no skill_invoke. CONTROL: the same call signed over HTTP is offered to T.
  T13  two workers in one process: T's socket on worker A, the production worker B beside it;
       step 9 reads presence from the TABLE (routed, never queued), only A's bridge delivers.
  T14  (tests/test_call_bridge.py, S3's no-op half made whole): a forged pointer reaches at
       most the real row's real target.
  T20  T connected with its presence row suppressed: the call is `queued`, T's worker's
       CallBridge routes it on the queued notification, and T answers. A forged queued pointer
       naming another peer's call, with T as its hint, moves nothing.
  T19  the memory authority's socket, connected while an in-process call to it runs, is
       offered nothing, its result is refused, and the in-process result stands; and rows
       that name the authority as a `routed` or `queued` target are offered by no path.
  T11  T offline: `queued`; T connects: the row is routed BEFORE its skill_invoke is queued
       (held on a row lock, nothing is sent; released, a fast target's running lands).
  T15  a `routed` edge committed while the bridge's LISTEN was down reaches T by the
       recovery sweep once LISTEN is back.
  T18  a skill_invoke evicted from T's send queue is offered again at once (the loss triggers
       the re-offer sweep), and one lost without a loss record is offered again by the
       periodic sweep; T answers once; an acknowledged call is never offered again.
  and  a call past its deadline is offered by none of the four paths.

Every socket runs against the production consumer with DPoP ENFORCED and real proofs (design
section 6); every HTTP write carries a DPoP proof too. NONCE: every envelope carries a fresh
nonce, and nothing here pins MESHY_ENVELOPE_NONCE_ENFORCE or MESHYCHAT_ENVELOPE_NONCE_ENFORCE, so
these pass with replay enforcement off (this branch's base) and on (036's one reader, on main).

HOW "NOTHING WAS SENT" IS OBSERVED (rule C1): a consumer handles one message at a time, so a
ping sent after the event under test is answered after it, and the pong as the NEXT frame
means nothing had been queued ahead of it.

Run:  .venv/Scripts/python.exe -m pytest tests/test_call_delivery.py -q -p no:cacheprovider
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import os
import sys
import threading
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
from apps.calls import bootstrap as memory_bootstrap  # noqa: E402
from apps.calls import pipeline  # noqa: E402
from apps.calls import route as routing  # noqa: E402
from apps.calls.envelope import SignedEnvelope, verify_payload  # noqa: E402
from apps.core import dbconn  # noqa: E402
from apps.crypto import primitives as P  # noqa: E402
from apps.wsgw import call_notify, mesh_notify  # noqa: E402
from apps.wsgw.connection_manager import (MANAGER, ConnectionHandle,  # noqa: E402
                                          ConnectionManager)
from apps.wsgw.constants import NEGOTIATED_PROTOCOL, WS_PATH  # noqa: E402
from apps.wsgw.consumer import MeshyWebSocketConsumer  # noqa: E402
from apps.wsgw.send_queue import SendQueue  # noqa: E402
from meshychat.asgi import application  # noqa: E402
from tests.fixtures.call_rows import seed_call  # noqa: E402
from tests.fixtures.hub_presence_db import hub_connections_clean  # noqa: E402,F401  (autouse)
from tools.meshy_node import wire  # noqa: E402

ISSUER = "meshy-call-delivery-test"
_server_key = ed25519.Ed25519PrivateKey.generate()
SERVER_PRIVATE_PEM = _server_key.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption())
SERVER_PUBLIC_PEM = _server_key.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)

#: A sweep or a beat that never fires inside a test unless the test shortens it.
NEVER = 3600.0
#: The uniform refusal of a target frame (divergence 041 S4), as a literal.
UNIFORM = "call is not routed to this peer"
CALL_CHANNEL = "meshy_call"


# ------------------------------------------------------------------------- fixtures

@pytest.fixture(autouse=True)
def server_config():
    """One server key mints every token and verifies it, over WS and HTTP; DPoP ENFORCED;
    the live bridge's revocation sweep held off. MESHY_SYNC_TIMEOUT_SECONDS is left at its
    default on purpose (Q1), and neither nonce switch is pinned (see the module docstring)."""
    with override_settings(
        ALLOWED_HOSTS=["*"],
        MESHY_JWT_PUBLIC_KEY_PEM=SERVER_PUBLIC_PEM.decode("ascii"),
        MESHY_JWT_ISSUER=ISSUER,
        MESHY_DPOP_ENFORCE=True,
        MESHY_WS_REVOCATION_SWEEP_SECS=NEVER,
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
    """The bootstrap role, autocommit: seeds, holds rows, forges pointers and measures."""
    with psycopg.connect(DSN, autocommit=True) as conn:
        yield conn


@pytest.fixture()
def tuned(monkeypatch):
    """The production CallBridge with its timers set for one test (restored after it)."""
    def tune(**attrs):
        for name, value in attrs.items():
            assert hasattr(call_notify.CALL_BRIDGE, name), name
            monkeypatch.setattr(call_notify.CALL_BRIDGE, name, value)
        return call_notify.CALL_BRIDGE
    return tune


def _key(conn, peer_id) -> dict:
    seed = os.urandom(32)
    public = P.public_key_of(seed)
    kid = P.kid_for_peer_key(public)
    conn.execute("INSERT INTO peer_keys (peer_id, kid, public_key, active) "
                 "VALUES (%s,%s,%s,TRUE)", (peer_id, kid, P.b64u(public)))
    return {"seed": seed, "kid": kid, "did": to_did_key(public), "public": public}


def _peer(conn, tag: str, trust_group: uuid.UUID) -> dict:
    """A dev-plane owner in `trust_group` and one peer with one key: step 5's same-group
    allow passes between any two of them with no grant (apps/calls/authorize.py)."""
    owner_id, peer_id = uuid.uuid4(), uuid.uuid4()
    conn.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id) "
                 "VALUES (%s,%s,'dev',%s)",
                 (owner_id, f"s5-{tag}-{owner_id.hex[:8]}@example.test", trust_group))
    conn.execute("INSERT INTO peers (id, owner_id, name) VALUES (%s,%s,%s)",
                 (peer_id, owner_id, f"s5-{tag}-{peer_id.hex[:8]}"))
    return {"peer_id": peer_id, "owner_id": owner_id, "keys": [_key(conn, peer_id)]}


def _skill(conn, peer: dict, name: str, *, mode: str = "async") -> str:
    conn.execute("INSERT INTO skills (peer_id, name, version, mode, share_mode, input_schema, "
                 "is_current) VALUES (%s,%s,'1.0.0',%s,'grants_only','{}',TRUE)",
                 (peer["peer_id"], name, mode))
    return name


@pytest.fixture()
def world(owner):
    """A caller C, a target T and another peer U, in one dev trust group; T and U each own
    an async skill `s5.echo`, and T a sync one."""
    group = uuid.uuid4()
    caller, target, other = (_peer(owner, t, group) for t in ("caller", "target", "other"))
    _skill(owner, target, "s5.echo")
    _skill(owner, target, "s5.echo.sync", mode="sync")
    _skill(owner, other, "s5.echo")
    return {"group": group, "caller": caller, "target": target, "other": other}


# -------------------------------------------------------------------------- the caller

def _token(peer: dict, *, ttl: int = 600) -> str:
    now = int(time.time())
    return pyjwt.encode({"sub": str(peer["peer_id"]), "aud": "peer", "iss": ISSUER,
                         "iat": now, "exp": now + ttl, "scopes": ["skills:invoke"],
                         "cnf": peer["keys"][0]["did"]},
                        SERVER_PRIVATE_PEM, algorithm="EdDSA")


def signed_call(caller: dict, target: dict, skill: str, args: dict | None = None, *,
                version: str = "1.0.0") -> dict:
    """A `skill.call` envelope as the node client builds it, with a FRESH nonce."""
    payload = {
        "v": 1, "kind": "skill.call", "call_id": f"s5-{uuid.uuid4()}",
        "caller_peer_id": str(caller["peer_id"]), "target_peer_id": str(target["peer_id"]),
        "skill_name": skill, "skill_version": version, "args": args or {"text": "hello"},
        "args_attachments": [],
        "issued_at": _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "nonce": uuid.uuid4().hex,
    }
    key = caller["keys"][0]
    return {"payload": payload, "signature": P.b64u(P.sign(key["seed"], P.jcs(payload))),
            "signing_kid": key["kid"]}


def _invoke(caller: dict, target: dict, skill: str, envelope: dict, *, mode: str | None = None):
    """POST the invoke as a node does under DPoP: its token bound to its key, a fresh proof
    for this method and path, the signed call_id echoed."""
    path = f"/v1/agents/{target['peer_id']}/skills/{skill}/invoke"
    token = _token(caller)
    body = {"envelope": envelope, "call_id": envelope["payload"]["call_id"]}
    if mode is not None:
        body["mode_override"] = mode
    return Client().post(path, data=json.dumps(body), content_type="application/json",
                         HTTP_AUTHORIZATION=f"Bearer {token}",
                         HTTP_DPOP=P.dpop_wire(caller["keys"][0]["seed"], "POST", path, token))


async def invoke(caller, target, skill, envelope, *, mode=None, status=202) -> dict:
    """The invoke, off the event loop (the bridges need the loop while it runs)."""
    response = await asyncio.to_thread(_invoke, caller, target, skill, envelope, mode=mode)
    assert response.status_code == status, (response.status_code, response.content[:400])
    body = json.loads(response.content)
    if status == 202:
        assert response["Location"] == f"/v1/calls/{envelope['payload']['call_id']}", body
    return body


def _get(path: str, peer: dict | None = None) -> dict:
    headers = {"HTTP_AUTHORIZATION": f"Bearer {_token(peer)}"} if peer else {}
    response = Client().get(path, **headers)
    assert response.status_code == 200, (path, response.status_code, response.content[:300])
    return json.loads(response.content)


async def poll(caller: dict, call_id: str) -> dict:
    return await asyncio.to_thread(_get, f"/v1/calls/{call_id}", caller)


# -------------------------------------------------------------------------- the target

async def _connect(peer: dict, *, app=None, bridge=None,
                   redelivered: bool = True) -> WebsocketCommunicator:
    """A socket on the production consumer (or `app`, whose bridge is `bridge`), its token
    bound to the peer's key with a fresh DPoP proof; its `welcome` read as its FIRST frame.
    Then, unless `redelivered` is False, this socket's connect-time redelivery has finished
    (the bridge counted it): `connect()`, and the presence row it writes, returned before
    that task was spawned, and any frame it sent is queued. Nothing else is read."""
    bridge = bridge or call_notify.CALL_BRIDGE
    runs = bridge.stats().get("redelivery:runs", 0)
    key = peer["keys"][0]
    token = _token(peer)
    protocols = [NEGOTIATED_PROTOCOL, f"jwt.{token}",
                 f"dpop.{P.dpop_wire(key['seed'], 'WS_UPGRADE', WS_PATH, token)}"]
    comm = WebsocketCommunicator(app or application, WS_PATH, subprotocols=protocols)
    accepted, code = await comm.connect(timeout=15)
    assert accepted is True, f"the hub refused the socket ({code})"
    welcome = await _next(comm)
    assert welcome["type"] == "welcome", f"the first frame was not the welcome: {welcome}"
    if redelivered:
        await _until(lambda: bridge.stats().get("redelivery:runs", 0) > runs,
                     "this socket's connect-time redelivery")
    return comm


async def _next(comm, timeout: float = 15) -> dict:
    event = await comm.receive_output(timeout=timeout)
    if event.get("type") == "websocket.close":
        pytest.fail(f"the socket closed with {event.get('code')}")
    assert event.get("type") == "websocket.send", event
    return json.loads(event["text"])


async def _pong(comm) -> None:
    """Nothing is queued ahead of a ping sent now (see the module docstring)."""
    await comm.send_json_to({"type": "ping"})
    frame = await _next(comm)
    assert frame == {"type": "pong"}, f"a frame reached the socket before its pong: {frame}"


async def _invoke_frame(comm, timeout: float = 15) -> dict:
    frame = await _next(comm, timeout)
    assert frame.get("type") == "skill_invoke", f"expected a skill_invoke, got {frame}"
    return frame


async def _send(comm, frame: dict) -> dict | None:
    """Send a target frame, then a ping: None when the pong came first (the frame landed,
    and nothing else had been queued), else the frame that came first."""
    await comm.send_json_to(frame)
    await comm.send_json_to({"type": "ping"})
    first = await _next(comm)
    if first == {"type": "pong"}:
        return None
    second = await _next(comm)
    assert second == {"type": "pong"}, (first, second)
    return first


def signed_result(peer: dict, call_id: str, result=None) -> dict:
    key = peer["keys"][0]
    payload = {"v": 1, "kind": "skill.result", "call_id": call_id,
               "target_peer_id": str(peer["peer_id"]), "status": "completed",
               "result": result if result is not None else {"ok": True},
               "issued_at": _dt.datetime.now(_dt.timezone.utc).isoformat().replace(
                   "+00:00", "Z"),
               "nonce": str(uuid.uuid4())}
    return {"type": "skill_result", "call_id": call_id,
            "envelope": {"payload": payload,
                         "signature": P.b64u(P.sign(key["seed"], P.jcs(payload))),
                         "signing_kid": key["kid"]}}


async def answer(comm, target: dict, call_id: str, result=None) -> dict:
    """What a target does with an invoke: ack, running, a signed result; each lands."""
    assert await _send(comm, {"type": "skill_ack", "call_id": call_id}) is None
    assert await _send(comm, {"type": "skill_running", "call_id": call_id}) is None
    frame = signed_result(target, call_id, result)
    assert await _send(comm, frame) is None
    return frame["envelope"]["payload"]


def verifies_under(peer: dict, envelope: dict) -> bool:
    return P.verify_strict(peer["keys"][0]["public"], P.jcs(envelope["payload"]),
                           P.b64u_decode(envelope["signature"]))


# ------------------------------------------------------------------------- the database

def _status(owner, call_id: str) -> str:
    return owner.execute("SELECT status FROM skill_calls WHERE call_id = %s",
                         (call_id,)).fetchone()[0]


def _events(owner, call_id: str) -> list[tuple]:
    return owner.execute("SELECT event_type, source, details FROM skill_call_events "
                         "WHERE call_id = %s ORDER BY id", (call_id,)).fetchall()


def _kinds(owner, call_id: str) -> list[tuple]:
    return [(e[0], e[1]) for e in _events(owner, call_id)]


def _online_rows(owner, peer: dict) -> list:
    return owner.execute("SELECT connection_id, worker_token FROM hub_connections "
                         "WHERE peer_id = %s", (peer["peer_id"],)).fetchall()


def _notify(owner, payload: dict) -> None:
    owner.execute("SELECT pg_notify(%s, %s)", (CALL_CHANNEL, json.dumps(payload)))


def _delta(before: dict, after: dict, *prefixes: str) -> dict:
    keys = {k for k in set(before) | set(after) if k.startswith(prefixes)}
    return {k: after.get(k, 0) - before.get(k, 0) for k in keys
            if after.get(k, 0) != before.get(k, 0)}


async def _until(predicate, what: str, timeout: float = 10.0, step: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(step)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


async def _invokes(comm, n: int, *, most: int = 12) -> set[str]:
    """The call_ids of the next skill_invokes until `n` distinct ones arrived. A duplicate is
    allowed (4.9: at-least-once, and targets dedupe on call_id); any other frame is not."""
    seen: set[str] = set()
    for _ in range(most):
        seen.add((await _invoke_frame(comm))["call_id"])
        if len(seen) >= n:
            return seen
    raise AssertionError(f"{most} invokes and only {len(seen)} distinct calls: {seen}")


async def _drained(comm, frame: dict, call_id: str) -> None:
    """Send a target frame, then a ping, and read to the pong: the frame landed (no reply),
    and anything queued ahead of the pong is a repeat of this call's invoke or filler."""
    await comm.send_json_to(frame)
    await comm.send_json_to({"type": "ping"})
    while (got := await _next(comm)) != {"type": "pong"}:
        assert got.get("type") in ("skill_invoke", "filler"), got
        assert got.get("type") == "filler" or got["call_id"] == call_id, got


def _counted(bridge, before: dict, name: str, n: int = 1):
    """A predicate: the bridge counted `name` at least n more times since `before`."""
    return lambda: bridge.stats().get(name, 0) - before.get(name, 0) >= n


INVOKE_KEYS = {"type", "call_id", "caller_peer_id", "target_peer_id", "skill_name",
               "skill_version", "mode", "envelope", "deadline_at"}


# ============================================================================== T1

@pytest.mark.asyncio
async def test_t1_an_http_call_reaches_a_connected_target_and_completes_by_polling(
        owner, world, tuned):
    """Step 9 finds T online in the presence table and writes `routed` with its event; the
    trigger's pointer reaches this worker's CallBridge, which re-reads the ROW and sends T one
    skill_invoke: the caller's whole signed payload with its signature and kid, which T
    verifies under C's key, and no continuation fields. T answers, and C's poll reads the
    signed result, checked against T's published key with the fleet's own verifier."""
    tuned(reoffer_secs=NEVER)
    caller, target = world["caller"], world["target"]
    t = await _connect(target)
    try:
        assert _online_rows(owner, target), "control: T's presence row"
        env = signed_call(caller, target, "s5.echo", {"text": "hello"})
        call_id = env["payload"]["call_id"]
        body = await invoke(caller, target, "s5.echo", env, mode="async")
        assert body["status"] == "routed", body

        frame = await _invoke_frame(t)
        assert set(frame) == INVOKE_KEYS, sorted(frame)
        row = owner.execute("SELECT deadline_at FROM skill_calls WHERE call_id = %s",
                            (call_id,)).fetchone()
        assert frame["call_id"] == call_id
        assert frame["caller_peer_id"] == str(caller["peer_id"])
        assert frame["target_peer_id"] == str(target["peer_id"])
        assert (frame["skill_name"], frame["skill_version"], frame["mode"]) == (
            "s5.echo", "1.0.0", "async")
        assert (_dt.datetime.fromisoformat(frame["deadline_at"].replace("Z", "+00:00"))
                == row[0].astimezone(_dt.timezone.utc))
        assert frame["envelope"] == env, "the invoke must carry the caller's signed envelope"
        assert verifies_under(caller, frame["envelope"])
        assert not verifies_under(target, frame["envelope"]), "control: another key fails"

        signed = await answer(t, target, call_id, {"echo": "hello"})
    finally:
        await t.disconnect()

    got = await poll(caller, call_id)
    assert got["status"] == "completed", got
    assert got["result_payload"] == signed
    jwks = await asyncio.to_thread(_get, f"/agents/{target['peer_id']}/jwks.json")
    assert wire.result_signature_ok(got, jwks) == (True, "ok")
    assert _kinds(owner, call_id) == [("created", "meshy"), ("routed", "meshy"),
                                      ("target_acked", "target"), ("running", "target"),
                                      ("completed", "target")]


# ======================================================== step 9: queued, Q1, the race

@pytest.mark.asyncio
async def test_q1_an_offline_target_is_queued_and_a_sync_invoke_answers_202_to_poll(
        owner, world):
    """4.10 and Q1: a call to a target with no socket anywhere waits for it, `queued`, with
    one `queued` event from the server, and nothing else: no webhook attempt, no failure.
    A SYNC invoke answers 202 with Location at the default wait (0 s), and the client polls.
    CONTROL: nothing about T is online."""
    caller, target = world["caller"], world["target"]
    assert _online_rows(owner, target) == []
    for skill, mode in (("s5.echo", None), ("s5.echo.sync", None), ("s5.echo", "sync")):
        env = signed_call(caller, target, skill)
        body = await invoke(caller, target, skill, env, mode=mode)
        call_id = env["payload"]["call_id"]
        assert body["status"] == "queued", (skill, mode, body)
        assert body["return_via"] == ["ws", "webhook", "poll"], "the 202 body is unchanged"
        assert _kinds(owner, call_id) == [("created", "meshy"), ("queued", "meshy")]
        assert _events(owner, call_id)[1][2] is None
        got = await poll(caller, call_id)
        assert (got["status"], got["error_code"], got["result"]) == ("queued", None, None)


def _pending_call(conn, world: dict, env: dict) -> None:
    """Steps 1 to 8 of a verified call, as the pipeline commits them: a `pending` row."""
    from apps.calls import repository
    p = env["payload"]
    target, caller = world["target"], world["caller"]
    skill_id = conn.execute("SELECT id FROM skills WHERE peer_id = %s AND name = %s",
                            (target["peer_id"], p["skill_name"])).fetchone()[0]
    repository.insert_call(
        conn, call_id=p["call_id"], caller_peer_id=caller["peer_id"],
        caller_owner_id=caller["owner_id"], target_peer_id=target["peer_id"],
        target_owner_id=target["owner_id"], skill_id=skill_id, skill_name=p["skill_name"],
        skill_version="1.0.0", mode="async", transport="http", payload=p,
        args_signature=env["signature"], args_signing_kid=env["signing_kid"],
        args_attachments=[], grant_id=None,
        deadline_at=_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(minutes=5))


def test_step9_that_loses_the_pending_edge_reports_the_routed_it_found(owner, world):
    """4.8: step 8 commits `pending` before step 9 runs, so a reconnecting target's
    redelivery can route the row first. Step 9's guarded UPDATE then matches nothing, and it
    reports what it re-reads: `routed`, with no second event. CONTROL: the same call still
    pending is `queued` (T is offline)."""
    caller, target = world["caller"], world["target"]
    outcomes = {}
    with dbconn.serving_connection() as conn:
        for label in ("still pending", "routed first"):
            env = signed_call(caller, target, "s5.echo")
            call_id = env["payload"]["call_id"]
            _pending_call(conn, world, env)
            if label == "routed first":
                owner.execute("UPDATE skill_calls SET status = 'routed' WHERE call_id = %s",
                              (call_id,))
                owner.execute("INSERT INTO skill_call_events (call_id, event_type, source) "
                              "VALUES (%s, 'routed', 'meshy')", (call_id,))
            verified = verify_payload(conn, SignedEnvelope(**env), caller["peer_id"],
                                      "skill.call")
            outcomes[label] = (routing.route(
                conn, memory_peer_id=routing.memory_peer_id_from_settings(),
                call_id=call_id, caller_peer_id=caller["peer_id"],
                target_peer_id=target["peer_id"], skill_name="s5.echo",
                envelope=SignedEnvelope(**env), verified=verified,
                verified_at=_dt.datetime.now(_dt.timezone.utc)), _kinds(owner, call_id))
    assert outcomes == {
        "still pending": ("queued", [("created", "meshy"), ("queued", "meshy")]),
        "routed first": ("routed", [("created", "meshy"), ("routed", "meshy")]),
    }, outcomes


# ============================================================================= T24

@pytest.mark.asyncio
async def test_t24_a_dashboard_test_call_to_a_remote_target_fails_unverified_at_step_9(
        owner, world, tuned):
    """F16: the dashboard_test transport skips verification, so its row stores no signature
    and its skill_invoke would carry `signature: null`, which a target that verifies refuses
    after a delivery. Step 9 refuses it first, typed, as it does
    for the memory authority. T is connected and gets nothing. CONTROL: the same skill,
    signed, over HTTP, is offered to T."""
    tuned(reoffer_secs=NEVER)
    caller, target = world["caller"], world["target"]
    t = await _connect(target)
    try:
        env = signed_call(caller, target, "s5.echo")
        call_id = env["payload"]["call_id"]
        with dbconn.serving_connection() as conn:
            ctx = pipeline.CallsContext(
                conn=conn, envelope_nonce_enforce=True,        # gate 4c: not run unsigned
                memory_peer_id=routing.memory_peer_id_from_settings())
            result = pipeline.start_call(ctx, pipeline.StartCallInput(
                caller_peer_id=caller["peer_id"], target_peer_id=target["peer_id"],
                skill_name="s5.echo",
                envelope=SignedEnvelope(payload=env["payload"], signature="", signing_kid=""),
                transport=pipeline.TRANSPORT_DASHBOARD_TEST, call_id=call_id))
        assert result.status == "failed", result
        row = owner.execute("SELECT error_code, args_signature, started_at FROM skill_calls "
                            "WHERE call_id = %s", (call_id,)).fetchone()
        assert row == (routing.E_UNVERIFIED_CALL, None, None), row
        assert _kinds(owner, call_id) == [("created", "meshy"), ("failed", "meshy")]
        await asyncio.sleep(0.3)
        await _pong(t)

        control = signed_call(caller, target, "s5.echo")
        assert (await invoke(caller, target, "s5.echo", control))["status"] == "routed"
        assert (await _invoke_frame(t))["call_id"] == control["payload"]["call_id"]
    finally:
        await t.disconnect()


# ============================================================================= T13

@pytest.mark.asyncio
async def test_t13_the_target_on_one_worker_is_reached_from_a_request_that_is_not_there(
        owner, world):
    """Two workers in one process (design section 6): worker A is a ConnectionManager and a
    CallBridge of its own, holding T's socket; worker B is the production registry and bridge,
    holding U's. The request is served by neither's registry. Step 9 must read presence from
    `hub_connections`, where A's bridge wrote T's row: `routed`, never `queued`. Both bridges
    hear the pointer; only A's holds T, so only A's reads the row and sends. T answers over
    its socket on A, and C's poll reads the result."""
    caller, target, other = world["caller"], world["target"], world["other"]
    manager_a = ConnectionManager()
    bridge_a = call_notify.CallBridge(manager_a, reoffer_secs=NEVER)
    app_a = MeshyWebSocketConsumer.as_asgi(manager=manager_a, call_bridge=bridge_a)
    socks = []
    try:
        socks.append(u := await _connect(other))                 # worker B, the production one
        socks.append(t := await _connect(target, app=app_a, bridge=bridge_a))  # worker A
        assert await manager_a.is_online(target["peer_id"])
        assert not await MANAGER.is_online(target["peer_id"]), "control: T is not on B"
        assert [r[1] for r in _online_rows(owner, target)] == [bridge_a.worker_token]
        before_a, before_b = bridge_a.stats(), call_notify.CALL_BRIDGE.stats()

        env = signed_call(caller, target, "s5.echo")
        call_id = env["payload"]["call_id"]
        assert (await invoke(caller, target, "s5.echo", env))["status"] == "routed"
        frame = await _invoke_frame(t)
        assert frame["envelope"] == env
        await _until(_counted(call_notify.CALL_BRIDGE, before_b, "call:notifications"),
                     "B to hear the pointer")
        signed = await answer(t, target, call_id)
        await _pong(u)
    finally:
        for s in socks:
            await s.disconnect()
        bridge_a.stop()
    assert _kinds(owner, call_id)[:2] == [("created", "meshy"), ("routed", "meshy")], \
        "step 9 read the local registry, not the presence table"
    assert "queued" not in [k for k, _ in _kinds(owner, call_id)]
    delta_a = _delta(before_a, bridge_a.stats(), "call:routed", "invoke:")
    delta_b = _delta(before_b, call_notify.CALL_BRIDGE.stats(), "call:routed", "invoke:")
    assert delta_a == {"call:routed:target_here": 1, "invoke:sent": 1}, delta_a
    assert delta_b == {"call:routed:target_not_here": 1}, delta_b
    got = await poll(caller, call_id)
    assert (got["status"], got["result_payload"]) == ("completed", signed)


# ============================================================================= T20

@pytest.mark.asyncio
async def test_t20_a_target_with_no_presence_row_is_routed_by_its_workers_safety_net(
        owner, world, tuned):
    """4.3's safety net (Q14): T is connected here, but its presence row is gone (a late beat,
    a failed insert), so step 9 writes `queued`. The queued pointer names T, which holds a
    socket in this worker, so the bridge runs the guarded queued-to-routed edge itself, with
    its event, and that edge's own pointer delivers. T answers. CONTROL: the row really was
    missing (no beat can write it back within the test)."""
    bridge = tuned(beat_secs=NEVER, reoffer_secs=NEVER)
    caller, target = world["caller"], world["target"]
    t = await _connect(target)
    try:
        owner.execute("DELETE FROM hub_connections WHERE peer_id = %s", (target["peer_id"],))
        assert _online_rows(owner, target) == []
        before = bridge.stats()
        env = signed_call(caller, target, "s5.echo")
        call_id = env["payload"]["call_id"]
        assert (await invoke(caller, target, "s5.echo", env))["status"] == "queued"
        frame = await _invoke_frame(t)
        assert frame["call_id"] == call_id and frame["envelope"] == env
        await answer(t, target, call_id)
        assert _online_rows(owner, target) == [], "control: nothing wrote the row back"
    finally:
        await t.disconnect()
    assert _kinds(owner, call_id)[:3] == [("created", "meshy"), ("queued", "meshy"),
                                          ("routed", "meshy")]
    assert _events(owner, call_id)[2][2] == {"via": "safety_net"}
    assert _delta(before, bridge.stats(), "safety_net:") == {"safety_net:routed": 1}
    assert (await poll(caller, call_id))["status"] == "completed"


@pytest.mark.asyncio
async def test_the_safety_net_routes_only_a_call_whose_row_target_is_connected_here(
        owner, world, tuned):
    """The ROW's target decides, never the pointer's hint (4.6). U is offline and its call is
    `queued`; a forged pointer names that call with T, which IS connected here, as its hint.
    The guarded edge requires the row's target to be one of this worker's peers, so nothing
    moves and nothing is sent. CONTROL: the same forgery naming T's own queued call routes it,
    and T gets it."""
    bridge = tuned(beat_secs=NEVER, reoffer_secs=NEVER)
    caller, target, other = world["caller"], world["target"], world["other"]
    others_env = signed_call(caller, other, "s5.echo")
    others = others_env["payload"]["call_id"]
    assert (await invoke(caller, other, "s5.echo", others_env))["status"] == "queued"
    t = await _connect(target)
    try:
        # seeded AFTER T connected, or T's connect-time redelivery would route it first
        mine = seed_call(owner, caller, target, _skill_id(owner, target), status="queued",
                         call_id=f"s5-{uuid.uuid4()}")
        before = bridge.stats()
        _notify(owner, {"call_id": others, "status": "queued", "target": str(target["peer_id"])})
        await _until(_counted(bridge, before, "call:queued:target_here"), "the forgery decided")
        await _until(_counted(bridge, before, "safety_net:no_row"), "the safety net to miss")
        await _pong(t)
        assert _status(owner, others) == "queued", "a forged hint routed another peer's call"
        assert _kinds(owner, others) == [("created", "meshy"), ("queued", "meshy")]

        _notify(owner, {"call_id": mine, "status": "queued", "target": str(target["peer_id"])})
        assert (await _invoke_frame(t))["call_id"] == mine
        assert _status(owner, mine) == "routed"
    finally:
        await t.disconnect()


def _skill_id(owner, peer: dict, name: str = "s5.echo"):
    return owner.execute("SELECT id FROM skills WHERE peer_id = %s AND name = %s",
                         (peer["peer_id"], name)).fetchone()[0]


# ============================================================================= T19

@pytest.fixture()
def authority(owner, world):
    """The memory authority, made by the bootstrap as an operator makes it, in the world's
    trust group, with a namespace C may write and a key its socket can prove."""
    peer_id = uuid.uuid4()
    conn = dbconn.connect()
    try:
        memory_bootstrap.bootstrap_memory_peer(
            conn, peer_id=peer_id, owner_email=f"s5-mem-{peer_id.hex[:8]}@example.test",
            trust_group=world["group"])
    finally:
        conn.close()
    owner_id = owner.execute("SELECT owner_id FROM peers WHERE id = %s",
                             (peer_id,)).fetchone()[0]
    namespace = f"s5test/{uuid.uuid4().hex[:12]}"
    caller = world["caller"]
    owner.execute("INSERT INTO shared_memory_namespace (namespace, owner_peer_id, "
                  "owner_owner_id) VALUES (%s,%s,%s)", (namespace, peer_id, owner_id))
    owner.execute("INSERT INTO shared_memory_grant (namespace, grantee_peer_id, "
                  "grantee_owner_id, permission, granted_by_peer_id, granted_by_owner_id) "
                  "VALUES (%s,%s,%s,'write',%s,%s)",
                  (namespace, caller["peer_id"], caller["owner_id"], peer_id, owner_id))
    peer = {"peer_id": peer_id, "owner_id": owner_id, "keys": [_key(owner, peer_id)],
            "namespace": namespace}
    with override_settings(MESHY_MEMORY_PEER_ID=str(peer_id)):
        yield peer


@pytest.mark.asyncio
async def test_t19_the_authoritys_socket_is_offered_none_of_its_in_process_calls(
        owner, world, authority, monkeypatch, tuned):
    """4.4: a call to the memory authority runs IN-PROCESS between pending and its terminal.
    Its peer connects over WS while one is running: redelivery (which selects `running`
    rows) must offer it nothing, a skill_result it sends for that call gets the uniform
    refusal, and the in-process result, signed by the server, stands. CONTROL: redelivery ran
    for that socket; the handler was really mid-run."""
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
                      {"namespace": authority["namespace"], "key": "t19", "value": {"n": 1}},
                      version="0.2.0")
    call_id = env["payload"]["call_id"]
    socks = [await _connect(world["other"])]                 # the bridge is up and listening
    running = asyncio.create_task(invoke(caller, authority, "memory.write", env, mode="async"))
    try:
        assert await asyncio.to_thread(entered.wait, 15), "the in-process call never started"
        assert _status(owner, call_id) == "running", "control: the call is mid-run"
        before = bridge.stats()
        socks.append(a := await _connect(authority))
        await _until(_counted(bridge, before, "redelivery:runs"), "the authority's redelivery")
        await asyncio.sleep(0.3)
        await _pong(a)
        refusal = await _send(a, signed_result(authority, call_id, {"forged": True}))
        assert refusal == {"type": "error", "code": "SKILL_RESULT_FAILED", "message": UNIFORM}
    finally:
        release.set()
        body = await running
        for s in socks:
            await s.disconnect()
    assert body["call_id"] == call_id
    got = await poll(caller, call_id)
    assert got["status"] == "completed", got
    assert got["result_payload"]["target_peer_id"] == str(authority["peer_id"])
    assert got["result"] != {"forged": True}
    jwks = await asyncio.to_thread(_get, "/.well-known/jwks.json")
    assert got["result_signing_kid"] in {k["kid"] for k in jwks["keys"]}, \
        "the result must be the server's in-process one"
    assert _delta(before, bridge.stats(), "redelivery:sent") == {}


@pytest.mark.asyncio
async def test_t19_rows_naming_the_authority_as_their_target_are_offered_by_no_path(
        owner, world, authority, tuned):
    """Another writer on the database (5.5) could leave a `routed` or `queued` row whose
    target is the memory authority. No path offers it: connect-time redelivery, the routed
    arm, the queued safety net and the re-offer sweep all exclude the authority. CONTROL:
    identical rows whose target is T are offered by redelivery, on T's connect."""
    bridge = tuned(reoffer_secs=0.2, reoffer_age_secs=0.0)
    caller, target = world["caller"], world["target"]
    a_skill = _skill_id(owner, authority, "memory.write")
    to_a = {s: seed_call(owner, caller, authority, a_skill, status=s,
                         call_id=f"s5-a-{s}-{uuid.uuid4()}") for s in ("routed", "queued")}
    to_t = {s: seed_call(owner, caller, target, _skill_id(owner, target), status=s,
                         call_id=f"s5-t-{s}-{uuid.uuid4()}") for s in ("routed", "queued")}
    socks = []
    try:
        socks.append(a := await _connect(authority))
        before = bridge.stats()
        for status, call_id in to_a.items():
            _notify(owner, {"call_id": call_id, "status": status,
                            "target": str(authority["peer_id"])})
        await _until(_counted(bridge, before, "call:notifications", 2), "the forgeries")
        await _until(_counted(bridge, before, "sweep:reoffer", 2), "two re-offer sweeps")
        await _pong(a)
        assert {s: _status(owner, c) for s, c in to_a.items()} == {
            "routed": "routed", "queued": "queued"}

        tuned(reoffer_secs=NEVER)          # from here only T's connect offers anything
        socks.append(t := await _connect(target))
        assert await _invokes(t, 2) == set(to_t.values()), \
            "control: T's identical rows are offered"
        await asyncio.sleep(0.5)
        await _pong(a)
    finally:
        for s in socks:
            await s.disconnect()


# ============================================================================= T11

@pytest.mark.asyncio
async def test_t11_a_queued_call_is_routed_before_its_invoke_is_queued_on_connect(
        owner, world, tuned):
    """2.12.3: a redelivery that publishes the invoke and THEN advances the row lets a fast
    target's skill_running meet `queued` and be refused. Here the edge commits first.
    The call's row is held by another transaction when T connects, so the edge must wait for
    it: nothing may reach T meanwhile (the welcome stays first, and the pong follows it). Once
    the row is released, the invoke arrives, and T's skill_running, sent the moment it does,
    lands. CONTROL: the call was `queued` while held."""
    tuned(reoffer_secs=NEVER)
    caller, target = world["caller"], world["target"]
    socks = [await _connect(world["other"])]                 # the bridge is up and listening
    env = signed_call(caller, target, "s5.echo")
    call_id = env["payload"]["call_id"]
    assert (await invoke(caller, target, "s5.echo", env))["status"] == "queued"
    holder = psycopg.connect(DSN)
    holder.execute("SELECT 1 FROM skill_calls WHERE call_id = %s FOR UPDATE", (call_id,))
    try:
        socks.append(t := await _connect(target, redelivered=False))
        await asyncio.sleep(0.8)
        await _pong(t)
        assert _status(owner, call_id) == "queued"
        holder.rollback()
        frame = await _invoke_frame(t)
        assert frame["call_id"] == call_id
        assert await _send(t, {"type": "skill_running", "call_id": call_id}) is None, \
            "the invoke reached the target before its row was routed"
        assert _status(owner, call_id) == "running"
    finally:
        holder.close()
        for s in socks:
            await s.disconnect()
    assert _kinds(owner, call_id) == [("created", "meshy"), ("queued", "meshy"),
                                      ("routed", "meshy"), ("running", "target")]
    assert _events(owner, call_id)[2][2] == {"via": "redelivery"}


@pytest.mark.asyncio
async def test_connect_redelivers_routed_and_running_calls_unchanged(owner, world, tuned):
    """4.8: a `routed` and a `running` call are sent again as they are, to the socket that
    connected, after its welcome; neither row moves and no event is written. A `completed`
    one is not. CONTROL: the rows' statuses after."""
    tuned(reoffer_secs=NEVER)
    caller, target = world["caller"], world["target"]
    skill = _skill_id(owner, target)
    calls = {s: seed_call(owner, caller, target, skill, status=s, call_id=f"s5-{s}-{uuid.uuid4()}")
             for s in ("routed", "running", "completed")}
    # The bridge is listening before T connects, so T's connect is the only redelivery (a
    # socket that starts the bridge is also offered its calls by the first LISTEN's sweep).
    socks = [await _connect(world["other"])]
    try:
        socks.append(t := await _connect(target))
        got = {(await _invoke_frame(t))["call_id"] for _ in range(2)}
        await _pong(t)
    finally:
        for s in socks:
            await s.disconnect()
    assert got == {calls["routed"], calls["running"]}
    assert {s: _status(owner, c) for s, c in calls.items()} == {
        "routed": "routed", "running": "running", "completed": "completed"}
    assert all(_events(owner, c) == [] for c in calls.values())


# ============================================================================= T15

@pytest.mark.asyncio
async def test_t15_a_routed_edge_committed_while_listen_was_down_reaches_the_target(
        owner, world, tuned, monkeypatch):
    """4.6's recovery sweep: NOTIFY has one loss window, a LISTEN connection that was down.
    T is connected; the bridge's LISTEN connection is killed and its reconnect held BEFORE it
    LISTENs again. C's call is routed meanwhile (T's presence row is still fresh), and its
    pointer reaches nobody: T gets nothing. Released, the bridge LISTENs, and its recovery
    sweep, which runs before it reports listening, offers the call; T answers. The re-offer
    sweep is an hour away, so only the recovery sweep can have sent it."""
    bridge = tuned(reoffer_secs=NEVER, beat_secs=NEVER)
    caller, target = world["caller"], world["target"]
    t = await _connect(target)
    down, entered, release = threading.Event(), threading.Event(), threading.Event()
    original = bridge._run_once

    def held(*args, **kwargs):
        if down.is_set():
            entered.set()
            release.wait(20)
        return original(*args, **kwargs)

    monkeypatch.setattr(bridge, "_run_once", held)
    try:
        down.set()
        pids = owner.execute("SELECT pid FROM pg_stat_activity WHERE datname = "
                             "current_database() AND query = 'LISTEN meshy_call_chunk'").fetchall()
        assert len(pids) == 1, pids
        owner.execute("SELECT pg_terminate_backend(%s)", (pids[0][0],))
        assert await asyncio.to_thread(entered.wait, 15), "the bridge did not reconnect"
        assert not bridge.is_listening()

        env = signed_call(caller, target, "s5.echo")
        call_id = env["payload"]["call_id"]
        assert (await invoke(caller, target, "s5.echo", env))["status"] == "routed"
        await asyncio.sleep(0.5)
        await _pong(t)

        down.clear()
        release.set()
        assert await bridge.wait_listening(15)
        frame = await _invoke_frame(t)
        assert frame["call_id"] == call_id
        await answer(t, target, call_id)
    finally:
        release.set()
        await t.disconnect()
    assert (await poll(caller, call_id))["status"] == "completed"


# ============================================================================= T18

def _evict_first_invoke(handle, *, record_loss: bool):
    """Make T's send queue lose the first skill_invoke pushed to it. With `record_loss`, as
    an overflow does it: the invoke and then a queue's worth of filler in one loop turn, so
    the writer never sees the invoke and finds the loss record. Without, as a loop hop that
    timed out does it: the invoke never reaches the queue, and nothing records a loss."""
    queue = handle.queue
    original = queue.push
    state = {"done": False}

    async def push(frame):
        if frame.get("type") == "skill_invoke" and not state["done"]:
            state["done"] = True
            if not record_loss:
                return
            queue.push_nowait(frame)
            for i in range(queue.capacity):
                queue.push_nowait({"type": "filler", "n": i})
            return
        await original(frame)

    queue.push = push
    return state


async def _invoke_after_filler(comm, timeout: float = 15) -> dict:
    """The next frame that is not filler."""
    while True:
        frame = await _next(comm, timeout)
        if frame.get("type") != "filler":
            assert frame.get("type") == "skill_invoke", frame
            return frame


@pytest.mark.asyncio
async def test_t18_an_invoke_evicted_from_the_send_queue_is_offered_again_at_once(
        owner, world, tuned):
    """F8 and Q17: drop-oldest can evict a skill_invoke before T reads it, and 4.9's
    at-least-once needs it offered again. The loss record the writer finds triggers the
    re-offer sweep at once, and for that peer it does not wait for the 10 s age floor. The
    periodic sweep is an hour away, so only the loss can have triggered it. T answers once."""
    bridge = tuned(reoffer_secs=NEVER)
    caller, target = world["caller"], world["target"]
    t = await _connect(target)
    try:
        handle = MANAGER._by_peer[target["peer_id"]][0]
        state = _evict_first_invoke(handle, record_loss=True)
        before = bridge.stats()
        env = signed_call(caller, target, "s5.echo")
        call_id = env["payload"]["call_id"]
        assert (await invoke(caller, target, "s5.echo", env))["status"] == "routed"
        frame = await _invoke_after_filler(t, timeout=5)
        assert state["done"] and handle.queue.dropped >= 1, "control: the invoke was evicted"
        assert frame["call_id"] == call_id and frame["envelope"] == env
        delta = _delta(before, bridge.stats(), "reoffer:")
        assert delta.get("reoffer:loss_requests", 0) >= 1 and delta.get("reoffer:sent", 0) >= 1, \
            delta
        await _drained(t, {"type": "skill_ack", "call_id": call_id}, call_id)
        await _drained(t, {"type": "skill_running", "call_id": call_id}, call_id)
        await _drained(t, signed_result(target, call_id), call_id)
        await asyncio.sleep(0.5)
        await _pong(t)
    finally:
        await t.disconnect()
    assert (await poll(caller, call_id))["status"] == "completed"
    assert [k for k, _ in _kinds(owner, call_id)].count("completed") == 1


@pytest.mark.asyncio
async def test_t18_an_invoke_lost_without_a_record_is_offered_again_by_the_periodic_sweep(
        owner, world, tuned):
    """The other loss: a loop hop that timed out never queues the frame, and nothing records
    it. The periodic re-offer sweep (every 10 s in production, here 0.2 s with the age floor
    at 0.5 s) offers `routed` calls nobody acknowledged. Once T acks, it is offered no more
    (CONTROL: several sweeps pass and the next frame is the pong)."""
    bridge = tuned(reoffer_secs=0.2, reoffer_age_secs=0.5)
    caller, target = world["caller"], world["target"]
    t = await _connect(target)
    try:
        handle = MANAGER._by_peer[target["peer_id"]][0]
        state = _evict_first_invoke(handle, record_loss=False)
        env = signed_call(caller, target, "s5.echo")
        call_id = env["payload"]["call_id"]
        assert (await invoke(caller, target, "s5.echo", env))["status"] == "routed"
        frame = await _invoke_frame(t, timeout=5)
        assert state["done"], "control: the first invoke was swallowed"
        assert frame["call_id"] == call_id and frame["envelope"] == env
        await _drained(t, {"type": "skill_ack", "call_id": call_id}, call_id)
        before = bridge.stats()
        await _until(_counted(bridge, before, "sweep:reoffer", 4), "four more sweeps")
        await _pong(t)
    finally:
        await t.disconnect()


# ======================================================================= the deadline

@pytest.mark.asyncio
async def test_a_call_past_its_deadline_is_offered_by_no_path(owner, world, tuned):
    """A call whose deadline passed and that the janitor has not swept yet is dead: none of
    connect-time redelivery, the routed arm, the queued safety net or the re-offer sweep
    offers it, and none routes it. CONTROL: the same two rows with a future deadline are
    offered on the same connect."""
    bridge = tuned(reoffer_secs=NEVER, reoffer_age_secs=0.0)
    caller, target = world["caller"], world["target"]
    skill = _skill_id(owner, target)
    past = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=30)
    dead = {s: seed_call(owner, caller, target, skill, status=s, deadline=past,
                         call_id=f"s5-dead-{s}-{uuid.uuid4()}") for s in ("routed", "queued")}
    live = {s: seed_call(owner, caller, target, skill, status=s,
                         call_id=f"s5-live-{s}-{uuid.uuid4()}") for s in ("routed", "queued")}
    socks = [await _connect(world["other"])]                 # the bridge is up and listening
    try:
        socks.append(t := await _connect(target))
        assert await _invokes(t, 2) == set(live.values()), "control: the live rows are offered"
        assert await _send(t, {"type": "skill_ack", "call_id": live["routed"]}) is None
        assert await _send(t, {"type": "skill_ack", "call_id": live["queued"]}) is None
        tuned(reoffer_secs=0.2)                 # the live ones are acknowledged: not re-offered
        before = bridge.stats()
        for status, call_id in dead.items():
            _notify(owner, {"call_id": call_id, "status": status,
                            "target": str(target["peer_id"])})
        await _until(_counted(bridge, before, "call:notifications", 2), "the pointers")
        await _until(_counted(bridge, before, "sweep:reoffer", 3), "three re-offer sweeps")
        await _pong(t)
    finally:
        for s in socks:
            await s.disconnect()
    assert {s: _status(owner, c) for s, c in dead.items()} == {
        "routed": "routed", "queued": "queued"}


# ================================================================== a suspended target

@pytest.mark.asyncio
async def test_a_suspended_targets_calls_are_offered_by_no_path(owner, world, tuned):
    """A suspension is plain SQL and sends no notification, so T's socket stays open until
    the revocation sweep (held off here) closes it (design R12). Meanwhile no path offers T a
    call: not the routed arm, not the queued safety net, not the re-offer sweep, and not a
    new socket's redelivery. CONTROL: reactivated, the same forged pointer delivers."""
    bridge = tuned(reoffer_secs=0.2, reoffer_age_secs=0.0)
    caller, target = world["caller"], world["target"]
    skill = _skill_id(owner, target)
    t = await _connect(target)
    try:
        owner.execute("UPDATE peers SET status = 'suspended' WHERE id = %s", (target["peer_id"],))
        calls = {s: seed_call(owner, caller, target, skill, status=s,
                              call_id=f"s5-susp-{s}-{uuid.uuid4()}") for s in ("routed", "queued")}
        before = bridge.stats()
        for status, call_id in calls.items():
            _notify(owner, {"call_id": call_id, "status": status,
                            "target": str(target["peer_id"])})
        await _until(_counted(bridge, before, "call:notifications", 2), "the pointers")
        await _until(_counted(bridge, before, "sweep:reoffer", 3), "three re-offer sweeps")
        assert await bridge.redeliver(MANAGER._by_peer[target["peer_id"]][0]) == 0
        await _pong(t)
        assert {s: _status(owner, c) for s, c in calls.items()} == {
            "routed": "routed", "queued": "queued"}

        tuned(reoffer_secs=NEVER)
        owner.execute("UPDATE peers SET status = 'active' WHERE id = %s", (target["peer_id"],))
        _notify(owner, {"call_id": calls["routed"], "status": "routed",
                        "target": str(target["peer_id"])})
        assert (await _invoke_frame(t))["call_id"] == calls["routed"]
    finally:
        owner.execute("UPDATE peers SET status = 'active' WHERE id = %s", (target["peer_id"],))
        await t.disconnect()


# ================================================================== a suspended caller

@pytest.mark.asyncio
async def test_a_suspended_callers_calls_are_offered_by_no_path_and_end_at_the_deadline_sweep(
        owner, world, tuned):
    """Operator decision 2026-09-25 (divergence 041, F23 F8): OFFERABLE also requires the
    CALLER to be `active`. A caller suspended after step 5 (which refuses a suspended caller's
    NEW calls) has its in-flight calls offered by no path: not the routed arm, not the queued
    safety net, not the re-offer sweep, and not a connecting target's redelivery, which
    routes none of its `pending` or `queued` calls. They stay where they are and the deadline
    sweep ends them `timeout`. CONTROL: the caller reactivated, the same forged pointer
    delivers."""
    bridge = tuned(reoffer_secs=0.2, reoffer_age_secs=0.0)
    caller, target = world["caller"], world["target"]
    skill = _skill_id(owner, target)
    t = await _connect(target)
    try:
        owner.execute("UPDATE peers SET status = 'suspended' WHERE id = %s", (caller["peer_id"],))
        calls = {s: seed_call(owner, caller, target, skill, status=s,
                              call_id=f"f8-csusp-{s}-{uuid.uuid4()}")
                 for s in ("routed", "queued", "pending")}
        before = bridge.stats()
        for status in ("routed", "queued"):
            _notify(owner, {"call_id": calls[status], "status": status,
                            "target": str(target["peer_id"])})
        await _until(_counted(bridge, before, "call:notifications", 2), "the pointers")
        await _until(_counted(bridge, before, "sweep:reoffer", 3), "three re-offer sweeps")
        assert await bridge.redeliver(MANAGER._by_peer[target["peer_id"]][0]) == 0
        await _pong(t)
        assert {s: _status(owner, c) for s, c in calls.items()} == {
            "routed": "routed", "queued": "queued", "pending": "pending"}

        # The deadline sweep ends them: each is in flight, so the sweep's own predicate.
        owner.execute("UPDATE skill_calls SET deadline_at = now() - interval '1 second' "
                      "WHERE call_id = ANY(%s)", (list(calls.values()),))
        from apps.calls import janitor
        with dbconn.serving_connection() as conn:
            swept = janitor.sweep_expired_deadlines(conn, limit=1000)
        assert set(calls.values()) <= set(swept), swept
        assert {s: _status(owner, c) for s, c in calls.items()} == {
            "routed": "timeout", "queued": "timeout", "pending": "timeout"}

        tuned(reoffer_secs=NEVER)
        owner.execute("UPDATE peers SET status = 'active' WHERE id = %s", (caller["peer_id"],))
        control = seed_call(owner, caller, target, skill, status="routed",
                            call_id=f"f8-csusp-control-{uuid.uuid4()}")
        _notify(owner, {"call_id": control, "status": "routed",
                        "target": str(target["peer_id"])})
        assert (await _invoke_frame(t))["call_id"] == control
    finally:
        owner.execute("UPDATE peers SET status = 'active' WHERE id = %s", (caller["peer_id"],))
        await t.disconnect()


@pytest.mark.asyncio
async def test_a_suspended_callers_routed_call_the_target_already_holds_may_still_be_finished(
        owner, world, tuned):
    """Review of fin/post-r10, finding 5 (divergence 041, F23 F8): the caller clause is on
    what is OFFERED, not on the target's own frames. skill_running's guard (`routed` ->
    `running`) and skill_result's check the TARGET's standing only (S4), so a `routed` call
    the target received before its caller was suspended (or reads through
    GET /v1/calls?role=target) can still be run and finished; 041 says `routed` as well as
    `running`. CONTROL: the call was routed and delivered while the caller was active."""
    tuned(reoffer_secs=NEVER)
    caller, target = world["caller"], world["target"]
    t = await _connect(target)
    try:
        env = signed_call(caller, target, "s5.echo", {"text": "held"})
        call_id = env["payload"]["call_id"]
        assert (await invoke(caller, target, "s5.echo", env, mode="async"))["status"] == "routed"
        assert (await _invoke_frame(t))["call_id"] == call_id, "control: delivered while active"
        owner.execute("UPDATE peers SET status = 'suspended' WHERE id = %s", (caller["peer_id"],))
        assert _status(owner, call_id) == "routed"
        await answer(t, target, call_id, {"echo": "held"})
        assert _status(owner, call_id) == "completed"
    finally:
        owner.execute("UPDATE peers SET status = 'active' WHERE id = %s", (caller["peer_id"],))
        await t.disconnect()


# ======================================================== the S5 review (3d3c5a8), pinned
#
# The adversarial review of 3d3c5a8 found guards that the tests above did not kill (F2's MA
# and MB, F3's MD and ME) and a re-offer triggered by a send-queue loss that re-sent OTHER
# peers' calls and could keep itself going (F1). These tests read a bridge's frames straight
# off its queues: a registry of its own holding one fake socket (a bare SendQueue) per peer,
# no consumer and no writer between the bridge and what it queued (the harness
# tests/test_call_bridge.py's re-offer test uses), or they call the redelivery read itself.

async def _harness(*peers: dict, **timers):
    """A started CallBridge over a registry of its own, one fake socket per peer. Every timer
    is an hour away unless the test sets it. Returns the bridge and each peer's queue."""
    manager = ConnectionManager()
    queues = {}
    for peer in peers:
        queue = SendQueue()
        await manager.register(ConnectionHandle(connection_id=uuid.uuid4(),
                                                peer_id=peer["peer_id"], queue=queue))
        queues[peer["peer_id"]] = queue
    bridge = call_notify.CallBridge(manager, **{"beat_secs": NEVER, "reoffer_secs": NEVER,
                                                **timers})
    assert await bridge.ensure_started() is True
    return bridge, queues


def _invoked(queue) -> list[str]:
    """Drain a fake socket's queue: the call_ids of its skill_invokes, in order."""
    got = []
    while len(queue):
        frame = queue.pop_nowait()
        assert frame["type"] == "skill_invoke", frame
        got.append(frame["call_id"])
    return got


def _blocked_by(owner, pid: int) -> bool:
    return owner.execute("SELECT count(*) FROM pg_stat_activity "
                         "WHERE %s = ANY(pg_blocking_pids(pid))", (pid,)).fetchone()[0] > 0


def test_redelivery_does_not_route_a_call_ended_while_its_update_waited(owner, world):
    """F2 MA: redelivery reads its rows, then routes the `pending` and `queued` ones in one
    guarded UPDATE. A call ended between the two (a cancel, the janitor) must stay ended. The
    row is held FOR UPDATE and abandoned in that transaction, which commits only once the
    redelivery's UPDATE is waiting on it: READ COMMITTED re-evaluates the UPDATE's WHERE on
    the committed row, and only the status clause refuses it. CONTROL: a second queued call
    in the same redelivery, not held, is routed with its event."""
    caller, target = world["caller"], world["target"]
    skill = _skill_id(owner, target)
    held = seed_call(owner, caller, target, skill, status="queued",
                     call_id=f"s5r-held-{uuid.uuid4()}")
    free = seed_call(owner, caller, target, skill, status="queued",
                     call_id=f"s5r-free-{uuid.uuid4()}")
    bridge = call_notify.CallBridge(ConnectionManager())
    holder = psycopg.connect(DSN)
    result: dict = {}
    try:
        holder.execute("SELECT 1 FROM skill_calls WHERE call_id = %s FOR UPDATE", (held,))
        holder.execute("UPDATE skill_calls SET status = 'abandoned', completed_at = now() "
                       "WHERE call_id = %s", (held,))
        pid = holder.info.backend_pid
        worker = threading.Thread(
            target=lambda: result.update(offers=bridge._redeliver_for(target["peer_id"])),
            name="s5r-redelivery", daemon=True)
        worker.start()
        deadline = time.monotonic() + 4.0
        while not _blocked_by(owner, pid):
            assert time.monotonic() < deadline, "the redelivery's UPDATE never waited"
            time.sleep(0.02)
        holder.commit()
        worker.join(15)
    finally:
        holder.close()
    assert result.get("offers") == [], result
    assert (_status(owner, held), _kinds(owner, held)) == ("abandoned", [])
    assert (_status(owner, free), _kinds(owner, free)) == ("routed", [("routed", "meshy")])


def test_a_dashboard_test_call_caught_by_redelivery_before_step_9_still_fails_unverified(
        owner, world):
    """F3 MD, F16: step 8 commits a dashboard_test call `pending` with no signature before
    step 9 refuses it, so a target's redelivery can select it in between. OFFERABLE's
    signature clause keeps it out: the row stays `pending` with no event, and step 9 then
    fails it `E_UNVERIFIED_CALL` as T24 does. Without the clause the redelivery routes it,
    step 9 finds it routed, and an invoke that cannot be built waits for its deadline.
    CONTROL: a signed `pending` call in the same redelivery is routed."""
    caller, target = world["caller"], world["target"]
    skill = _skill_id(owner, target)
    unsigned = seed_call(owner, caller, target, skill, call_id=f"s5r-dash-{uuid.uuid4()}")
    owner.execute("UPDATE skill_calls SET transport = 'dashboard_test', args_signature = NULL, "
                  "args_signing_kid = NULL WHERE call_id = %s", (unsigned,))
    signed = seed_call(owner, caller, target, skill, call_id=f"s5r-signed-{uuid.uuid4()}")
    offers = call_notify.CallBridge(ConnectionManager())._redeliver_for(target["peer_id"])
    assert offers == []
    assert (_status(owner, unsigned), _kinds(owner, unsigned)) == ("pending", [])
    assert (_status(owner, signed), _kinds(owner, signed)) == ("routed", [("routed", "meshy")])
    with dbconn.serving_connection() as conn:
        status = routing.route(
            conn, memory_peer_id=routing.memory_peer_id_from_settings(), call_id=unsigned,
            caller_peer_id=caller["peer_id"], target_peer_id=target["peer_id"],
            skill_name="s5.echo", envelope=None, verified=None,
            verified_at=_dt.datetime.now(_dt.timezone.utc))
    assert status == "failed"
    assert owner.execute("SELECT error_code FROM skill_calls WHERE call_id = %s",
                         (unsigned,)).fetchone()[0] == routing.E_UNVERIFIED_CALL


def test_one_redelivery_offers_at_most_its_limit_most_urgent_deadline_first(owner, world):
    """F3 ME, rule B5: one redelivery reads at most `offer_limit` calls, the most urgent
    deadlines first; a call past the limit is neither sent nor routed by it. CONTROL: with the
    limit raised, the same redelivery reads all four and routes the queued one."""
    caller, target = world["caller"], world["target"]
    skill = _skill_id(owner, target)
    now = _dt.datetime.now(_dt.timezone.utc)
    routed = [seed_call(owner, caller, target, skill, status="routed",
                        deadline=now + _dt.timedelta(minutes=10 + i),
                        call_id=f"s5r-lim-{i}-{uuid.uuid4()}") for i in range(3)]
    late = seed_call(owner, caller, target, skill, status="queued",
                     deadline=now + _dt.timedelta(minutes=30), call_id=f"s5r-late-{uuid.uuid4()}")
    bridge = call_notify.CallBridge(ConnectionManager())
    bridge.offer_limit = 2
    assert [o.call_id for o in bridge._redeliver_for(target["peer_id"])] == routed[:2]
    assert (_status(owner, late), _kinds(owner, late)) == ("queued", [])
    bridge.offer_limit = 4
    assert [o.call_id for o in bridge._redeliver_for(target["peer_id"])] == routed
    assert (_status(owner, late), _kinds(owner, late)) == ("routed", [("routed", "meshy")])


@pytest.mark.asyncio
async def test_the_reoffer_sweep_offers_only_routed_calls(owner, world):
    """F2 MB: the re-offer sweep sends `routed` calls again, and nothing else. A call ended
    with no target event and a future deadline (abandoned by its caller) is past the age
    floor like the routed one, and is never offered. CONTROL: the routed call is offered."""
    caller, target = world["caller"], world["target"]
    bridge, queues = await _harness(target, reoffer_age_secs=0.0)
    queue = queues[target["peer_id"]]
    try:
        skill = _skill_id(owner, target)
        routed = seed_call(owner, caller, target, skill, status="routed",
                           call_id=f"s5r-routed-{uuid.uuid4()}")
        ended = seed_call(owner, caller, target, skill, status="abandoned",
                          call_id=f"s5r-ended-{uuid.uuid4()}")
        before = bridge.stats()
        bridge.reoffer_secs = 0.2
        await _until(_counted(bridge, before, "sweep:reoffer", 3), "three re-offer sweeps")
        bridge.reoffer_secs = NEVER
        await asyncio.sleep(0.5)
        got = _invoked(queue)
    finally:
        bridge.stop()
    assert routed in got, "control: the routed call was offered again"
    assert set(got) == {routed}, f"the sweep offered a call that is not routed: {ended}"


@pytest.mark.asyncio
async def test_a_send_queue_loss_reoffers_that_peers_calls_and_no_other_peers(owner, world):
    """F1 (a): a loss on A's socket re-offers A's unanswered calls at once, with no age floor,
    and nobody else's. B has an unanswered `routed` call past the age floor too (the floor is
    0 here), but the periodic sweep is an hour away, so nothing may re-send it: before the
    review a loss ran the whole sweep, and A's loss re-sent B's call. CONTROL: A's call is
    offered again."""
    caller, a, b = world["caller"], world["target"], world["other"]
    bridge, queues = await _harness(a, b, reoffer_age_secs=0.0)
    try:
        calls = {p["peer_id"]: seed_call(owner, caller, p, _skill_id(owner, p), status="routed",
                                         call_id=f"s5r-loss-{uuid.uuid4()}") for p in (a, b)}
        bridge.request_reoffer(a["peer_id"])
        await _until(lambda: len(queues[a["peer_id"]]) >= 1, "A's call offered again")
        await asyncio.sleep(1.0)                        # four more polls of the listener
    finally:
        bridge.stop()
    assert _invoked(queues[a["peer_id"]]) == [calls[a["peer_id"]]]
    assert _invoked(queues[b["peer_id"]]) == [], "A's loss re-sent B's call"


@pytest.mark.asyncio
async def test_repeated_losses_on_one_peer_reoffer_its_calls_once_per_interval(owner, world):
    """F1 (b): a loss-triggered re-offer pushes up to `offer_limit` invokes into the socket
    that lost frames; into a full queue each push evicts a frame, and the writer reports that
    loss too. Unthrottled, a slow reader kept the sweep running every poll. Now one peer's
    losses re-offer its calls at most once per `loss_reoffer_interval_secs` (10 s): four
    losses, each given more than two polls, send A's call once, and the other three are
    counted as throttled. CONTROL: once the interval has passed, a loss re-offers again."""
    caller, a = world["caller"], world["target"]
    bridge, queues = await _harness(a, reoffer_age_secs=0.0)
    queue = queues[a["peer_id"]]
    try:
        call_id = seed_call(owner, caller, a, _skill_id(owner, a), status="routed",
                            call_id=f"s5r-burst-{uuid.uuid4()}")
        before = bridge.stats()
        for _ in range(4):
            bridge.request_reoffer(a["peer_id"])
            await asyncio.sleep(0.6)
        assert _invoked(queue) == [call_id], "a burst of losses re-offered more than once"
        assert _delta(before, bridge.stats(), "reoffer:loss_") == {
            "reoffer:loss_requests": 4, "reoffer:loss_throttled": 3}
        bridge.loss_reoffer_interval_secs = 0.5
        bridge.request_reoffer(a["peer_id"])
        await _until(lambda: len(queue) >= 1, "a re-offer once the interval had passed")
        assert _invoked(queue) == [call_id]
    finally:
        bridge.stop()


@pytest.mark.asyncio
async def test_loss_triggered_reoffers_do_not_postpone_the_periodic_sweep(owner, world):
    """F1's fix makes a loss-triggered pass cover the lossy peers only, so it must not count
    as the periodic sweep: A's losses, one every 0.3 s and none throttled, must not hold back
    the 1 s periodic sweep that re-offers B's unanswered call."""
    caller, a, b = world["caller"], world["target"], world["other"]
    bridge, queues = await _harness(a, b, reoffer_secs=1.0, reoffer_age_secs=0.0,
                                    loss_reoffer_interval_secs=0.0)
    try:
        call_id = seed_call(owner, caller, b, _skill_id(owner, b), status="routed",
                            call_id=f"s5r-periodic-{uuid.uuid4()}")
        end = time.monotonic() + 3.2
        while time.monotonic() < end:
            bridge.request_reoffer(a["peer_id"])
            await asyncio.sleep(0.3)
    finally:
        bridge.stop()
    got = _invoked(queues[b["peer_id"]])
    assert len(got) >= 2 and set(got) == {call_id}, got
