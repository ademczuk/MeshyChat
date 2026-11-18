"""Revocation reaches an open WebSocket. STATUS B15; divergence 030.

THE DEFECT, measured on c5457ac. `ConnectionManager.drop_peer` (apps/wsgw/
connection_manager.py:169) was the whole revocation mechanism and nothing called it: a grep
of apps/, meshychat/ and tools/ found only docstrings. A peer suspended, stripped of its
read on shared/mesh-chat, or whose key was revoked kept its open socket until its JWT
expired: it could still ping, subscribe to presence and receive presence frames, and the
only thing that stopped was the mesh push.

WHAT IS PROVED HERE, every trigger end to end against `meshychat.asgi.application` with a
connected client, so the consumer, the registry (`MANAGER`) and the bridge (`BRIDGE`) are
the ones a deployed process runs:

  * suspension (`peers.status` no longer 'active'), by the operator's plain UPDATE: the
    bridge's standing sweep closes every socket the peer holds with 4410;
  * suspension with a NOTIFY in the same transaction closes it at once, and a FORGED
    notification naming a peer in good standing closes nothing;
  * a `memory.grant` revoke that removes the peer's read on shared/mesh-chat, through the
    signed invoke route, closes it with 4411; the same revoke on another namespace does not;
  * a key revocation through `DELETE /v1/peers/me/keys/{kid}`, DPoP enforced, closes the
    sockets bound to that key with 4412, leaves the peer's socket bound to another key
    open, and refuses a new upgrade presenting the revoked key;
  * a dropped socket sends nothing more and dispatches nothing more;
  * (sections 5-7, the adversarial review of c766b76) only a TRANSITION closes: a
    notification naming shared/mesh-chat, forged or from an admin revoking a grant that had
    already expired, closes no socket that never had read, while a revoke reaches a socket
    opened after the last sweep, by notification or by the next sweep, because the upgrade
    records the read the socket connected with; a rotation's grace-window key keeps its
    socket until the window ends; with DPoP off a revoked key still cannot come back; a
    deleted peer row is inactive; and the sweep interval is the setting.

HOW A NEGATIVE IS OBSERVED (the rule tests/test_mesh_live.py states): the bridge handles
notifications ONE AT A TIME in commit order, so "the unrelated revoke dropped nothing" is
proved by a mesh message committed AFTER it arriving on the socket as its next frame, never
by waiting N seconds for silence.

The close codes are literals here on purpose: this file had to run red on the tree that
had no revocation constants, and a literal also pins the wire value against a constant that
drifts. `test_the_close_codes_are_the_constants` ties the two together.

Run:  .venv/Scripts/python.exe -m pytest tests/test_ws_revocation.py -q -p no:cacheprovider
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import io
import json
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
from django.core.management import call_command  # noqa: E402
from django.test import Client, override_settings  # noqa: E402

from apps.authapi import config as authconfig  # noqa: E402
from apps.authapi.didkey import to_did_key  # noqa: E402
from apps.crypto import primitives as P  # noqa: E402
from apps.memoryskills.validation import MESH_CHAT_NAMESPACE as NS  # noqa: E402
from apps.wsgw import mesh_notify  # noqa: E402
from apps.wsgw.connection_manager import MANAGER, ConnectionHandle, ConnectionManager  # noqa: E402
from apps.wsgw.constants import NEGOTIATED_PROTOCOL, WS_PATH  # noqa: E402
from apps.wsgw.consumer import HubConfig, MeshyWebSocketConsumer  # noqa: E402
from apps.wsgw.presence import RecordingPresenceBackend  # noqa: E402
from apps.wsgw.send_queue import SendQueue  # noqa: E402
from meshychat.asgi import application  # noqa: E402
from tests.fixtures import signed_rows as sr  # noqa: E402

ISSUER = "meshy-revocation-test"

#: The wire values. See the module docstring on why these are literals.
CLOSE_PEER_INACTIVE = 4410
CLOSE_GRANT_REVOKED = 4411
CLOSE_KEY_REVOKED = 4412
#: The NOTIFY channel an operator (or a route) names to revoke at once.
REVOCATION_CHANNEL = "meshy_peer_revoked"

_server_key = ed25519.Ed25519PrivateKey.generate()
SERVER_PRIVATE_PEM = _server_key.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption())
SERVER_PUBLIC_PEM = _server_key.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)

#: A sweep that never fires inside a test: isolates the NOTIFY path from the sweep.
NO_SWEEP = 3600.0
#: A sweep fast enough to test, slow enough not to spin.
FAST_SWEEP = 0.5


# ---------------------------------------------------------------------------- fixtures

@pytest.fixture(autouse=True)
def server_config():
    with override_settings(
        ALLOWED_HOSTS=["*"],
        MESHY_JWT_PUBLIC_KEY_PEM=SERVER_PUBLIC_PEM.decode("ascii"),
        MESHY_JWT_ISSUER=ISSUER,
        # MESHY_ENVELOPE_NONCE_ENFORCE is deliberately NOT pinned: the nonce gate runs
        # at its shipped default, ON (divergence 036).
        MESHY_SYNC_TIMEOUT_SECONDS=0.0,
        MESHY_WS_REVOCATION_SWEEP_SECS=NO_SWEEP,
    ), authconfig.override_config(jwt_signing_key_pem=SERVER_PRIVATE_PEM,
                                  jwt_public_key_pem=SERVER_PUBLIC_PEM,
                                  jwt_issuer=ISSUER):
        yield


@pytest.fixture()
def owner():
    with psycopg.connect(DSN, autocommit=True) as conn:
        yield conn


@pytest.fixture()
def bridge():
    yield mesh_notify.BRIDGE
    mesh_notify.BRIDGE.stop()


@pytest.fixture(autouse=True)
def registry_left_clean():
    """See tests/test_mesh_live.py: a cancelled communicator never runs disconnect()."""
    yield
    MANAGER._by_peer.clear()


# ----------------------------------------------------------------------------- seeding

def _uniq(tag: str) -> str:
    return f"{tag}-{uuid.uuid4().hex[:8]}"


def _key(conn, peer_id) -> dict:
    seed = os.urandom(32)
    public = P.public_key_of(seed)
    kid = P.kid_for_peer_key(public)
    conn.execute("INSERT INTO peer_keys (peer_id, kid, public_key, active) "
                 "VALUES (%s,%s,%s,TRUE)", (peer_id, kid, P.b64u(public)))
    return {"seed": seed, "kid": kid, "did": to_did_key(public)}


def _peer(conn, tag: str, *, group: uuid.UUID | None = None, key: bool = False) -> dict:
    group = group or uuid.uuid4()
    owner_id, peer_id, name = uuid.uuid4(), uuid.uuid4(), _uniq(tag)
    conn.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id) "
                 "VALUES (%s,%s,'dev',%s)",
                 (owner_id, f"revoke-{tag}-{owner_id.hex[:8]}@example.test", group))
    conn.execute("INSERT INTO peers (id, owner_id, name, display_name) VALUES (%s,%s,%s,%s)",
                 (peer_id, owner_id, name, tag))
    out = {"peer_id": peer_id, "owner_id": owner_id, "name": name}
    if key:
        out.update(_key(conn, peer_id))
    return out


def _mesh_chat_owner(conn) -> tuple[uuid.UUID, uuid.UUID]:
    row = conn.execute("SELECT owner_peer_id, owner_owner_id FROM shared_memory_namespace "
                       "WHERE namespace = %s", (NS,)).fetchone()
    if row is None:
        p = _peer(conn, "nsowner")
        conn.execute("INSERT INTO shared_memory_namespace (namespace, owner_peer_id, "
                     "  owner_owner_id, claimant_plane, ephemeral, retention_policy) "
                     "VALUES (%s,%s,%s,'dev',FALSE,'keep_all') ON CONFLICT DO NOTHING",
                     (NS, p["peer_id"], p["owner_id"]))
        row = conn.execute("SELECT owner_peer_id, owner_owner_id FROM shared_memory_namespace "
                           "WHERE namespace = %s", (NS,)).fetchone()
    return row[0], row[1]


def _grant(conn, peer: dict, permission: str, namespace: str = NS) -> None:
    ns_peer, ns_owner = conn.execute(
        "SELECT owner_peer_id, owner_owner_id FROM shared_memory_namespace "
        "WHERE namespace = %s", (namespace,)).fetchone()
    conn.execute("INSERT INTO shared_memory_grant (namespace, grantee_peer_id, "
                 "  grantee_owner_id, permission, granted_by_peer_id, granted_by_owner_id) "
                 "VALUES (%s,%s,%s,%s,%s,%s)",
                 (namespace, peer["peer_id"], peer["owner_id"], permission, ns_peer, ns_owner))


def _msg_key() -> str:
    return f"msg/{int(time.time() * 1000)}-{uuid.uuid4().hex[:10]}"


def _insert_msg(conn, author: dict, to: list[str]) -> str:
    """One committed mesh row, straight to the table; sql/036 notifies on it."""
    _mesh_chat_owner(conn)
    rid, key = uuid.uuid4(), _msg_key()
    value = {"schema_version": 1, "to": to, "from": "revocation-test", "type": "note",
             "thread_id": "revocation", "ts": "2026-09-23T10:00:00Z", "body": "ordering probe",
             "author_family": "claude"}
    # With the bytes a writer would have signed (divergence 051: A2 refuses a row without).
    sr.insert(conn, sr.row(author["peer_id"], author["owner_id"], NS, key, value, rid=rid,
                           call_id=f"rev-{rid}", author_label=author["name"]))
    return key


# ------------------------------------------------------------------------------ sockets

def _token(peer: dict, *, cnf: str | None = None) -> str:
    now = int(time.time())
    claims = {"sub": str(peer["peer_id"]), "aud": "peer", "iss": ISSUER, "iat": now,
              "exp": now + 600, "scopes": []}
    if cnf is not None:
        claims["cnf"] = cnf
    return pyjwt.encode(claims, SERVER_PRIVATE_PEM, algorithm="EdDSA")


async def _open(peer: dict, *, key: dict | None = None) -> tuple[WebsocketCommunicator, tuple]:
    """A socket on the REAL application. With `key`, the token is bound to it (`cnf`) and
    carries a fresh DPoP proof signed by it, as a node sends under enforcement."""
    token = _token(peer, cnf=key["did"] if key else None)
    protocols = [NEGOTIATED_PROTOCOL, f"jwt.{token}"]
    if key:
        protocols.append(f"dpop.{P.dpop_wire(key['seed'], 'WS_UPGRADE', WS_PATH, token)}")
    comm = WebsocketCommunicator(application, WS_PATH, subprotocols=protocols)
    return comm, await comm.connect(timeout=15)


async def _connect(peer: dict, *, key: dict | None = None) -> WebsocketCommunicator:
    comm, (accepted, code) = await _open(peer, key=key)
    assert accepted is True, f"the hub refused {peer['name']} ({code})"
    first = json.loads(await comm.receive_from(timeout=15))
    assert first["type"] == "welcome", first
    return comm


async def _mesh_key(comm, timeout: float = 15.0) -> str:
    frame = json.loads(await comm.receive_from(timeout=timeout))
    assert frame["type"] == "mesh_message", f"expected a mesh_message frame, got {frame}"
    return frame["key"]


async def _closed_with(comm, code: int, timeout: float = 15.0) -> dict:
    """The NEXT thing the socket emits is the close, with this code. Not a frame first:
    a frame between the revocation and the close is a frame that should not exist."""
    event = await comm.receive_output(timeout=timeout)
    assert event.get("type") == "websocket.close", f"expected a close, got {event}"
    assert event.get("code") == code, f"closed with {event.get('code')}, expected {code}"
    return event


async def _pong(comm) -> None:
    await comm.send_json_to({"type": "ping"})
    assert json.loads(await comm.receive_from(timeout=15)) == {"type": "pong"}


def _delta(before: dict, after: dict) -> dict:
    keys = set(before) | set(after)
    return {k: after.get(k, 0) - before.get(k, 0)
            for k in keys if after.get(k, 0) != before.get(k, 0) and k.startswith("revocation:")}


# ======================================================================= 1. suspension

@pytest.mark.asyncio
async def test_an_operator_suspension_closes_every_socket_the_peer_holds(owner, bridge):
    """The operator suspends with a plain UPDATE and no NOTIFY, so the standing sweep must
    find the changed `peers.status`. Both of
    the peer's sockets close with 4410; the control peer, evaluated by the SAME sweep,
    still answers; and the suspended peer cannot come back (upgrade gate 3, 4403)."""
    sus, ctl = _peer(owner, "suspended"), _peer(owner, "control")
    socks = []
    with override_settings(MESHY_WS_REVOCATION_SWEEP_SECS=FAST_SWEEP):
        try:
            first, second, control = (await _connect(sus), await _connect(sus),
                                      await _connect(ctl))
            socks = [first, second, control]
            assert await bridge.wait_listening(10), "the bridge never LISTENed"
            before = bridge.stats()
            owner.execute("UPDATE peers SET status = 'suspended' WHERE id = %s",
                          (sus["peer_id"],))
            for comm in (first, second):
                event = await _closed_with(comm, CLOSE_PEER_INACTIVE)
                assert "suspended" in event.get("reason", "") or "active" in event.get(
                    "reason", ""), event
            await _pong(control)
            assert _delta(before, bridge.stats()).get("revocation:dropped:peer_inactive") == 2

            again, (accepted, code) = await _open(sus)
            await again.disconnect()
            assert (accepted, code) == (False, 4403), "a suspended peer reconnected"
        finally:
            for comm in socks:
                await comm.disconnect()


@pytest.mark.asyncio
async def test_a_notify_closes_at_once_and_a_forged_one_closes_nothing(owner, bridge):
    """The sweep is off here (NO_SWEEP), so only the notification can act.

    NOTIFY is unauthenticated: any session on the database may send one. So the payload is
    a POINTER and the bridge re-reads `peers.status` before it drops anything. A forged
    notification naming an ACTIVE peer is followed by a mesh message to that peer, which
    must be its next frame; then the real suspension, UPDATE and NOTIFY in one transaction,
    closes the socket with no sweep running."""
    author, target = _peer(owner, "author"), _peer(owner, "target")
    _mesh_chat_owner(owner)
    _grant(owner, target, "read")
    comm = await _connect(target)
    try:
        assert await bridge.wait_listening(10)
        before = bridge.stats()
        owner.execute("SELECT pg_notify(%s, %s)",
                      (REVOCATION_CHANNEL, json.dumps({"peer_id": str(target["peer_id"])})))
        probe = _insert_msg(owner, author, [target["name"]])
        assert await _mesh_key(comm) == probe, "the forged revocation dropped the socket"

        with psycopg.connect(DSN) as tx:                  # ONE transaction, as a route does
            tx.execute("UPDATE peers SET status = 'suspended' WHERE id = %s",
                       (target["peer_id"],))
            tx.execute("SELECT pg_notify(%s, %s)",
                       (REVOCATION_CHANNEL, json.dumps({"peer_id": str(target["peer_id"])})))
        await _closed_with(comm, CLOSE_PEER_INACTIVE, timeout=10)
        delta = _delta(before, bridge.stats())
        assert delta.get("revocation:notifications") == 2, delta
        assert delta.get("revocation:kept") == 1, delta
        assert delta.get("revocation:dropped:peer_inactive") == 1, delta
        assert "revocation:sweeps" not in delta, f"a sweep ran with the sweep off: {delta}"
    finally:
        await comm.disconnect()


# ================================================= 2. read on shared/mesh-chat revoked

@pytest.fixture()
def grant_world(owner):
    """A memory authority made by THE COMMAND, an ADMIN of shared/mesh-chat who can sign a
    memory.grant through it, a target with read, a control with read, and a second
    namespace the target also reads (the unrelated revoke)."""
    group = uuid.uuid4()
    memory_peer = uuid.uuid4()
    out = io.StringIO()
    with override_settings(MESHY_MEMORY_PEER_ID=str(memory_peer)):
        call_command("bootstrap_memory_peer", "--trust-group", str(group),
                     "--owner-email", f"revoke-mem-{memory_peer.hex[:8]}@example.test",
                     stdout=out, stderr=io.StringIO())
    assert "CREATED" in out.getvalue(), out.getvalue()
    _mesh_chat_owner(owner)
    admin = _peer(owner, "admin", group=group, key=True)
    target, control, author = _peer(owner, "target"), _peer(owner, "control"), _peer(owner, "a")
    _grant(owner, admin, "admin")
    _grant(owner, target, "read")
    _grant(owner, control, "read")
    other_ns = f"dev/revocation-{uuid.uuid4().hex[:8]}"
    owner.execute("INSERT INTO shared_memory_namespace (namespace, owner_peer_id, "
                  "  owner_owner_id, claimant_plane, ephemeral, retention_policy) "
                  "VALUES (%s,%s,%s,'dev',FALSE,'keep_all')",
                  (other_ns, admin["peer_id"], admin["owner_id"]))
    _grant(owner, target, "read", namespace=other_ns)
    with override_settings(MESHY_MEMORY_PEER_ID=str(memory_peer)):
        yield {"memory_peer": memory_peer, "admin": admin, "target": target,
               "control": control, "author": author, "other_ns": other_ns}


def _memory_grant_revoke(world: dict, grantee: dict, namespace: str):
    """A signed memory.grant {revoke: true} to the memory authority, as mem_client sends it."""
    admin, target = world["admin"], world["memory_peer"]
    payload = {
        "v": 1, "kind": "skill.call", "call_id": str(uuid.uuid4()),
        "caller_peer_id": str(admin["peer_id"]), "target_peer_id": str(target),
        "skill_name": "memory.grant", "skill_version": "0.2.0",
        "args": {"namespace": namespace, "grantee_peer_id": str(grantee["peer_id"]),
                 "revoke": True},
        "args_attachments": [],
        "issued_at": _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "nonce": uuid.uuid4().hex,
    }
    envelope = {"payload": payload, "signature": P.b64u(P.sign(admin["seed"], P.jcs(payload))),
                "signing_kid": admin["kid"]}
    body = {"envelope": envelope, "mode_override": "async", "call_id": payload["call_id"]}
    response = Client().post(f"/v1/agents/{target}/skills/memory.grant/invoke",
                             data=json.dumps(body), content_type="application/json",
                             headers={"authorization": f"Bearer {_token(admin)}"})
    assert response.status_code == 202, response.content
    call = json.loads(response.content)
    assert call["status"] == "completed", call
    return call


@pytest.mark.asyncio
async def test_revoking_read_on_mesh_chat_closes_the_socket_and_nothing_else(
        owner, grant_world, bridge):
    w = grant_world
    target, control, author = w["target"], w["control"], w["author"]
    socks = {}
    try:
        socks["target"], socks["control"] = await _connect(target), await _connect(control)
        assert await bridge.wait_listening(10)
        before = bridge.stats()

        # UNRELATED 1: the same peer's grant on ANOTHER namespace. Its next frame is M1.
        await asyncio.to_thread(_memory_grant_revoke, w, target, w["other_ns"])
        m1 = _insert_msg(owner, author, [target["name"], control["name"]])
        assert await _mesh_key(socks["target"]) == m1, "a revoke on another namespace dropped it"
        assert await _mesh_key(socks["control"]) == m1

        # UNRELATED 2: a FORGED grant notification for the control, who still has read.
        owner.execute("SELECT pg_notify(%s, %s)", (REVOCATION_CHANNEL, json.dumps(
            {"peer_id": str(control["peer_id"]), "namespace": NS})))

        # THE REVOKE: the target's read on shared/mesh-chat, through the signed route.
        await asyncio.to_thread(_memory_grant_revoke, w, target, NS)
        await _closed_with(socks["target"], CLOSE_GRANT_REVOKED)

        m2 = _insert_msg(owner, author, [control["name"]])
        assert await _mesh_key(socks["control"]) == m2, "the control lost its socket"
        delta = _delta(before, bridge.stats())
        assert delta.get("revocation:dropped:grant_revoked") == 1, delta
        assert delta.get("revocation:skipped:namespace") == 1, delta

        # By design a peer without read may still HOLD a socket (it gets no mesh push; see
        # divergence 020). The revocation is the close, not a ban: it reconnects.
        back = await _connect(target)
        await _pong(back)
        await back.disconnect()
    finally:
        for comm in socks.values():
            await comm.disconnect()


@pytest.mark.asyncio
async def test_a_grant_revoked_behind_the_routes_back_is_found_by_the_sweep(owner, bridge):
    """An operator's `UPDATE shared_memory_grant SET revoked = TRUE` sends no NOTIFY. The
    sweep closes a socket whose peer it has SEEN holding read and no longer does. The
    control is a peer that connected with no read at all: the same sweeps leave it open,
    because a peer that never had read has nothing revoked (divergence 020 lets it hold a
    socket without the push)."""
    reader, never = _peer(owner, "reader"), _peer(owner, "never-read")
    _mesh_chat_owner(owner)
    _grant(owner, reader, "read")
    with override_settings(MESHY_WS_REVOCATION_SWEEP_SECS=FAST_SWEEP):
        socks = []
        try:
            r, n = await _connect(reader), await _connect(never)
            socks = [r, n]
            assert await bridge.wait_listening(10)
            before = bridge.stats()
            # Two sweeps have run once the counter moves by two: the reader was SEEN.
            for _ in range(100):
                if _delta(before, bridge.stats()).get("revocation:sweeps", 0) >= 2:
                    break
                await asyncio.sleep(0.1)
            owner.execute("UPDATE shared_memory_grant SET revoked = TRUE, revoked_at = NOW() "
                          "WHERE namespace = %s AND grantee_peer_id = %s",
                          (NS, reader["peer_id"]))
            await _closed_with(r, CLOSE_GRANT_REVOKED)
            await _pong(n)
        finally:
            for comm in socks:
                await comm.disconnect()


# ====================================================================== 3. key revoked

def _revoke_key(peer: dict, auth_key: dict, kid: str):
    """DELETE /v1/peers/me/keys/{kid} under DPoP enforcement, authenticated by a token
    bound to `auth_key` and a proof it signed."""
    path = f"/v1/peers/me/keys/{kid}"
    token = _token(peer, cnf=auth_key["did"])
    proof = P.dpop_wire(auth_key["seed"], "DELETE", path, token)
    response = Client().delete(path, headers={"authorization": f"Bearer {token}",
                                              "DPoP": proof})
    assert response.status_code == 204, (response.status_code, response.content)


@pytest.mark.asyncio
async def test_revoking_a_key_closes_the_sockets_bound_to_it_and_refuses_its_return(
        owner, bridge):
    """One peer, three keys, DPoP enforced as in production. S1 is bound to K1, S3 to K3.

      revoke K2 (nothing bound to it)   -> both sockets receive M1: nothing dropped
      revoke K1                          -> S1 closes 4412; S3 receives M2
      upgrade again presenting K1        -> refused 4412
      upgrade presenting K3              -> accepted (the control)
    """
    author, peer = _peer(owner, "author"), _peer(owner, "keyed")
    _mesh_chat_owner(owner)
    _grant(owner, peer, "read")
    k1, k2, k3 = _key(owner, peer["peer_id"]), _key(owner, peer["peer_id"]), _key(
        owner, peer["peer_id"])
    socks = {}
    with override_settings(MESHY_DPOP_ENFORCE=True):
        try:
            socks["s1"] = await _connect(peer, key=k1)
            socks["s3"] = await _connect(peer, key=k3)
            assert await bridge.wait_listening(10)
            before = bridge.stats()

            await asyncio.to_thread(_revoke_key, peer, k3, k2["kid"])
            m1 = _insert_msg(owner, author, [peer["name"]])
            assert await _mesh_key(socks["s1"]) == m1, "revoking K2 dropped the K1 socket"
            assert await _mesh_key(socks["s3"]) == m1

            await asyncio.to_thread(_revoke_key, peer, k3, k1["kid"])
            await _closed_with(socks["s1"], CLOSE_KEY_REVOKED)
            m2 = _insert_msg(owner, author, [peer["name"]])
            assert await _mesh_key(socks["s3"]) == m2, "revoking K1 dropped the K3 socket"
            delta = _delta(before, bridge.stats())
            assert delta.get("revocation:dropped:key_revoked") == 1, delta

            again, (accepted, code) = await _open(peer, key=k1)
            await again.disconnect()
            assert (accepted, code) == (False, CLOSE_KEY_REVOKED), (accepted, code)
            control = await _connect(peer, key=k3)
            await _pong(control)
            await control.disconnect()
        finally:
            for comm in socks.values():
                await comm.disconnect()


# ============================================== 4. a dropped socket goes quiet at once

def _consumer(manager: ConnectionManager) -> tuple[MeshyWebSocketConsumer, ConnectionHandle]:
    config = HubConfig(jwt_public_key_pem=SERVER_PUBLIC_PEM, jwt_issuer=ISSUER,
                       dpop_enforce=False, check_peer_active=False, zero_rtt=False)
    consumer = MeshyWebSocketConsumer(config=config, manager=manager,
                                     presence=RecordingPresenceBackend())
    consumer.peer_id = uuid.uuid4()
    handle = ConnectionHandle(connection_id=consumer.connection_id, peer_id=consumer.peer_id,
                              queue=consumer.queue)
    consumer.handle = handle
    return consumer, handle


@pytest.mark.asyncio
async def test_a_revoked_socket_dispatches_no_further_frame():
    """Between the revocation and the close there is a window; nothing a client sends in
    it is acted on. CONTROL first: before the drop, the same ping is answered."""
    manager = ConnectionManager()
    consumer, handle = _consumer(manager)
    await manager.register(handle)
    await consumer.receive(text_data=json.dumps({"type": "ping"}))
    assert (await asyncio.wait_for(consumer.queue.pop(), 5)) == {"type": "pong"}

    assert await manager.drop_peer(consumer.peer_id) == 1
    await consumer.receive(text_data=json.dumps({"type": "ping"}))
    assert len(consumer.queue) == 0, "a frame from a revoked socket was dispatched"


@pytest.mark.asyncio
async def test_a_revoked_socket_writes_no_further_frame():
    """A frame already queued when the revocation lands is not written. CONTROL first: the
    writer does write a queued frame on a live socket."""
    manager = ConnectionManager()
    consumer, handle = _consumer(manager)
    await manager.register(handle)
    sent: list = []

    async def record(text_data=None, bytes_data=None, close=False):
        sent.append(text_data)

    consumer.send = record
    writer = asyncio.create_task(consumer._writer_loop())
    await consumer.queue.push({"type": "pong"})
    for _ in range(50):
        if sent:
            break
        await asyncio.sleep(0.02)
    assert sent == [json.dumps({"type": "pong"})], "control: a live writer wrote nothing"

    await manager.drop_peer(consumer.peer_id)
    await consumer.queue.push({"type": "mesh_message", "key": "msg/after-revocation"})
    try:
        await asyncio.wait_for(writer, 2)
    except asyncio.TimeoutError:
        pass
    finally:
        writer.cancel()
    assert sent == [json.dumps({"type": "pong"})], f"written after revocation: {sent[1:]}"


def test_the_close_codes_are_the_constants():
    from apps.wsgw import constants
    assert (constants.CLOSE_REVOKED_PEER_INACTIVE, constants.CLOSE_REVOKED_GRANT,
            constants.CLOSE_REVOKED_KEY) == (CLOSE_PEER_INACTIVE, CLOSE_GRANT_REVOKED,
                                             CLOSE_KEY_REVOKED)
    assert constants.REVOCATION_NOTIFY_CHANNEL == REVOCATION_CHANNEL
    codes = {constants.CLOSE_UNAUTHORIZED, constants.CLOSE_FORBIDDEN,
             constants.CLOSE_UPGRADE_REQUIRED, constants.CLOSE_INTERNAL_ERROR,
             constants.CLOSE_TOO_MANY_CONNECTIONS, constants.CLOSE_UNIMPLEMENTED}
    assert not codes & {CLOSE_PEER_INACTIVE, CLOSE_GRANT_REVOKED, CLOSE_KEY_REVOKED}, (
        "a revocation code collides with another close reason")



# ========================================= 5. only a TRANSITION closes (review of c766b76)
#
# The adversarial review of c766b76 found two paths nothing pinned and one the code got
# wrong. Each test below names the mutant it kills.

async def _still_open(comm, why: str) -> None:
    """The NEXT thing the socket emits answers a ping. A close in its place is the failure,
    and the message shows it (a bare `receive_from` would only say the type was wrong)."""
    await comm.send_json_to({"type": "ping"})
    event = await comm.receive_output(timeout=10)
    assert event.get("type") == "websocket.send", f"{why}: {event}"
    assert json.loads(event["text"]) == {"type": "pong"}, f"{why}: {event}"


def _notify(conn, payload: dict) -> None:
    conn.execute("SELECT pg_notify(%s, %s)", (REVOCATION_CHANNEL, json.dumps(payload)))


@pytest.mark.asyncio
async def test_a_notification_naming_mesh_chat_closes_no_socket_that_never_had_read(
        owner, bridge):
    """Divergence 020 lets a peer with NO read on shared/mesh-chat hold a socket: no push,
    and it still pings and watches presence. A notification is a POINTER, and one that
    merely NAMES shared/mesh-chat is not evidence the peer lost anything; any database
    session can send it. It must close nothing.

    RED on c766b76: the payload's `namespace` alone put the peer on the grant watch, so this
    socket was closed with 4411 (review finding F3). The barrier is a reader's mesh frame
    committed AFTER the notification: the bridge handles notifications one at a time in
    commit order, so once the frame arrives the notification has been judged."""
    never, reader, author = _peer(owner, "never-read"), _peer(owner, "reader"), _peer(
        owner, "author")
    _mesh_chat_owner(owner)
    _grant(owner, reader, "read")
    n, r = await _connect(never), await _connect(reader)
    try:
        assert await bridge.wait_listening(10)
        await _still_open(n, "control: the no-read socket was not live to begin with")
        before = bridge.stats()
        _notify(owner, {"peer_id": str(never["peer_id"]), "namespace": NS})
        probe = _insert_msg(owner, author, [reader["name"]])
        assert await _mesh_key(r) == probe
        await _still_open(n, "a notification naming shared/mesh-chat closed a peer "
                             "that never had read")
        delta = _delta(before, bridge.stats())
        assert delta.get("revocation:notifications") == 1, delta
        assert delta.get("revocation:kept") == 1, delta
        assert "revocation:dropped:grant_revoked" not in delta, delta
    finally:
        await n.disconnect()
        await r.disconnect()


@pytest.mark.asyncio
async def test_revoking_a_grant_that_had_already_expired_closes_nothing(
        owner, grant_world, bridge):
    """The same wrong close by a REAL route. The target's read EXPIRED before it connected,
    so its socket is a legitimate no-read one. An admin tidying the stale row with
    `memory.grant {revoke}` changes nothing about the target's standing, but the row is
    still `revoked = FALSE`, so the route counts 1 and notifies with the namespace.

    CONTROL, through the same route in the same test: revoking the control's LIVE read
    closes the control's socket with 4411. That close is also the barrier: it is judged
    after the target's notification, which was committed first.

    RED on c766b76: the target was closed with 4411 (review finding F3)."""
    w = grant_world
    target, control = w["target"], w["control"]
    owner.execute("UPDATE shared_memory_grant SET expires_at = NOW() - INTERVAL '1 minute' "
                  "WHERE namespace = %s AND grantee_peer_id = %s AND revoked = FALSE",
                  (NS, target["peer_id"]))
    t, c = await _connect(target), await _connect(control)
    try:
        assert await bridge.wait_listening(10)
        await _still_open(t, "control: the target's socket was not live to begin with")
        await asyncio.to_thread(_memory_grant_revoke, w, target, NS)
        await asyncio.to_thread(_memory_grant_revoke, w, control, NS)
        await _closed_with(c, CLOSE_GRANT_REVOKED)
        await _still_open(t, "revoking an already-expired grant closed a no-read socket")
    finally:
        await t.disconnect()
        await c.disconnect()


@pytest.mark.asyncio
async def test_a_revoke_reaches_a_socket_opened_after_the_last_sweep(
        owner, grant_world, bridge):
    """The production shape: the bridge is ALREADY listening (an earlier socket started it
    and its startup sweep ran) when the target connects, and no sweep runs after (NO_SWEEP).
    No sweep has seen the target read, so what puts it on the grant watch is the read its
    socket held WHEN IT CONNECTED. The control peer stays open and still receives.

    Review finding F1: on c766b76 only the payload's namespace put the target on the watch,
    and no test built this shape. Mutants killed: A1 on c766b76 (the namespace ignored,
    `watch = {peer} if peer in self._readers`), and after the fix the connect-time read
    dropped from the notification's watch."""
    w = grant_world
    target, control, author = w["target"], w["control"], w["author"]
    c = await _connect(control)
    t = None
    try:
        assert await bridge.wait_listening(10)
        base = bridge.stats().get("revocation:sweeps", 0)
        t = await _connect(target)
        await _still_open(t, "control: the target's socket was not live to begin with")
        assert bridge.stats().get("revocation:sweeps", 0) == base, "a sweep ran after connect"
        await asyncio.to_thread(_memory_grant_revoke, w, target, NS)
        await _closed_with(t, CLOSE_GRANT_REVOKED, timeout=10)
        m = _insert_msg(owner, author, [control["name"]])
        assert await _mesh_key(c) == m, "the control lost its socket"
    finally:
        await c.disconnect()
        if t is not None:
            await t.disconnect()


@pytest.mark.asyncio
async def test_the_next_sweep_finds_a_sql_revoke_on_a_socket_no_sweep_has_seen(owner, bridge):
    """An operator's `UPDATE shared_memory_grant SET revoked = TRUE` sends no NOTIFY, and
    the reader connected on a warm bridge, so no sweep ever SAW it read. The read its socket
    held at connect is the evidence of the transition, so the next sweep closes it with
    4411. The next sweep is forced by a re-LISTEN, which sweeps before it reports ready
    (divergence 030), so the test does not race a timer. CONTROL: a peer that connected
    with no read at all, judged by the same sweep, stays open.

    RED on c766b76: the sweep watched only peers a sweep had seen, so the reader kept its
    socket until it reconnected (divergence 030 listed this as a known gap). Mutant killed:
    the connect-time read dropped from the sweep's watch."""
    reader, never = _peer(owner, "reader"), _peer(owner, "never-read")
    _mesh_chat_owner(owner)
    _grant(owner, reader, "read")
    n = await _connect(never)
    r = None
    try:
        assert await bridge.wait_listening(10)
        base = bridge.stats().get("revocation:sweeps", 0)
        r = await _connect(reader)
        await _still_open(r, "control: the reader's socket was not live to begin with")
        assert bridge.stats().get("revocation:sweeps", 0) == base, "a sweep ran after connect"
        owner.execute("UPDATE shared_memory_grant SET revoked = TRUE, revoked_at = NOW() "
                      "WHERE namespace = %s AND grantee_peer_id = %s", (NS, reader["peer_id"]))
        await asyncio.to_thread(bridge.stop)
        assert await bridge.ensure_started(), "the bridge did not LISTEN again"
        assert bridge.stats().get("revocation:sweeps", 0) == base + 1
        await _closed_with(r, CLOSE_GRANT_REVOKED, timeout=10)
        await _still_open(n, "the sweep closed a peer that never had read")
    finally:
        await n.disconnect()
        if r is not None:
            await r.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("lookup, recorded", [("holds read", True), ("no read", False),
                                              ("lookup fails", True)])
async def test_the_upgrade_records_the_read_the_socket_connected_with(monkeypatch, lookup,
                                                                      recorded):
    """Gate 4c records whether the peer held read on shared/mesh-chat at connect, and never
    refuses on it. A lookup that FAILS records True: the socket is then re-judged from the
    tables at the next notification or sweep, and closed only if it holds no read then;
    recording False would drop it from every grant watch on a transient error.

    Mutants killed: the lookup's result ignored (`mesh_read=False` on the handle; the
    "holds read" case), and a failed lookup recorded as False (the "lookup fails" case)."""
    from apps.wsgw import db as ws_db

    async def active(_peer):
        return True

    async def read(_peer):
        if lookup == "lookup fails":
            raise psycopg.OperationalError("the database went away")
        return lookup == "holds read"

    monkeypatch.setattr(ws_db, "peer_is_active", active)
    monkeypatch.setattr(ws_db, "mesh_read", read)
    manager = ConnectionManager()
    config = HubConfig(jwt_public_key_pem=SERVER_PUBLIC_PEM, jwt_issuer=ISSUER,
                       dpop_enforce=False, check_peer_active=True, zero_rtt=False)
    app = MeshyWebSocketConsumer.as_asgi(config=config, manager=manager,
                                        presence=RecordingPresenceBackend())
    peer = {"peer_id": uuid.uuid4(), "name": "recorded"}
    comm = WebsocketCommunicator(app, WS_PATH,
                                 subprotocols=[NEGOTIATED_PROTOCOL, f"jwt.{_token(peer)}"])
    accepted, code = await comm.connect(timeout=10)
    try:
        assert accepted is True, f"{lookup}: the upgrade refused ({code}); 4c is not a gate"
        assert json.loads(await comm.receive_from(timeout=10))["type"] == "welcome"
        got = await manager.mesh_readers()
        assert got == ({peer["peer_id"]} if recorded else set()), f"{lookup}: {got}"
    finally:
        await comm.disconnect()


# ============================================= 6. the legitimate key paths stay open

@pytest.mark.asyncio
async def test_a_rotation_grace_key_keeps_its_socket_until_the_window_ends(owner, bridge):
    """A rotation puts the old key in its 24 h grace window (`revoke_old_active`:
    active = FALSE, revoked_at = NOW() + 24 h), and `find_by_kid` still verifies with it. So
    a socket bound to it survives the sweep and a new upgrade presenting it is admitted.
    THEN the window ends (revoked_at in the past) and the next sweep closes the socket with
    4412: the refusal half, which proves the sweeps above were judging this key.

    Review finding F2. Mutant killed: A2, `OR revoked_at > NOW()` dropped from
    `revocation.key_states` (the grace window read as dead)."""
    author, peer = _peer(owner, "author"), _peer(owner, "rotating")
    _mesh_chat_owner(owner)
    _grant(owner, peer, "read")
    old = _key(owner, peer["peer_id"])
    with override_settings(MESHY_DPOP_ENFORCE=True, MESHY_WS_REVOCATION_SWEEP_SECS=FAST_SWEEP):
        s = await _connect(peer, key=old)
        try:
            assert await bridge.wait_listening(10)
            _key(owner, peer["peer_id"])                       # the rotation's new key
            owner.execute("UPDATE peer_keys SET active = FALSE, "
                          "revoked_at = NOW() + make_interval(hours => 24) "
                          "WHERE peer_id = %s AND kid = %s", (peer["peer_id"], old["kid"]))
            before = bridge.stats().get("revocation:sweeps", 0)
            for _ in range(100):
                if bridge.stats().get("revocation:sweeps", 0) >= before + 2:
                    break
                await asyncio.sleep(0.1)
            assert bridge.stats().get("revocation:sweeps", 0) >= before + 2, "no sweep ran"
            m = _insert_msg(owner, author, [peer["name"]])
            assert await _mesh_key(s) == m, "a grace-window key's socket was closed"
            again = await _connect(peer, key=old)          # the upgrade on it: admitted
            await _still_open(again, "a grace-window key's new socket was closed")
            await again.disconnect()

            owner.execute("UPDATE peer_keys SET revoked_at = NOW() - INTERVAL '1 second' "
                          "WHERE peer_id = %s AND kid = %s", (peer["peer_id"], old["kid"]))
            await _closed_with(s, CLOSE_KEY_REVOKED, timeout=10)
        finally:
            await s.disconnect()


@pytest.mark.asyncio
async def test_with_dpop_off_a_revoked_key_still_cannot_come_back(owner, bridge):
    """`MESHYCHAT_DPOP_ENFORCE=false` disables DPoP enforcement, and a token still carries the
    `cnf` it was minted with. A key revoked while DPoP is off closes the socket bound to it
    AND refuses the next upgrade presenting it (4412), or the close would be undone by the
    same unexpired token. CONTROL: the same peer's upgrade bound to its live key is admitted.

    Review finding F6. Mutant killed: A9, the upgrade's key gate taken only under DPoP
    enforcement (`... and self.config.dpop_enforce`)."""
    peer = _peer(owner, "dpop-off")
    k1, k2 = _key(owner, peer["peer_id"]), _key(owner, peer["peer_id"])
    with override_settings(MESHY_DPOP_ENFORCE=False):
        s1 = await _connect(peer, key=k1)
        try:
            assert await bridge.wait_listening(10)
            with psycopg.connect(DSN) as tx:                  # the route's shape: one tx
                tx.execute("UPDATE peer_keys SET active = FALSE, revoked_at = NOW() "
                           "WHERE peer_id = %s AND kid = %s", (peer["peer_id"], k1["kid"]))
                tx.execute("SELECT pg_notify(%s, %s)", (REVOCATION_CHANNEL, json.dumps(
                    {"peer_id": str(peer["peer_id"]), "kid": k1["kid"]})))
            await _closed_with(s1, CLOSE_KEY_REVOKED, timeout=10)
            again, (accepted, code) = await _open(peer, key=k1)
            await again.disconnect()
            assert (accepted, code) == (False, CLOSE_KEY_REVOKED), (accepted, code)
            live = await _connect(peer, key=k2)
            await _still_open(live, "control: the live key's socket was closed")
            await live.disconnect()
        finally:
            await s1.disconnect()


# ======================================== 7. what divergence 030 states, each pinned

def test_a_peer_row_that_is_gone_has_no_standing(owner):
    """Divergence 030: `peers.status` not 'active', OR THE ROW GONE, is 4410. The upgrade
    admits only `EXISTS(... status = 'active')`, so a socket whose row was deleted has no
    standing either. CONTROL: an active peer in the same call is not returned.

    Review finding F7. Mutant killed: A3, `inactive_peers` selecting `status <> 'active'`
    (which a missing row never matches)."""
    from apps.wsgw import revocation

    active, suspended = _peer(owner, "active"), _peer(owner, "suspended")
    owner.execute("UPDATE peers SET status = 'suspended' WHERE id = %s",
                  (suspended["peer_id"],))
    gone = uuid.uuid4()
    got = revocation.inactive_peers(owner, [active["peer_id"], suspended["peer_id"], gone])
    assert got == {suspended["peer_id"], gone}, got
    drops, _ = revocation.judge(owner, {active["peer_id"], gone}, [], set())
    assert [(d.peer_id, d.cause) for d in drops] == [(gone, revocation.PEER_INACTIVE)]


def test_the_sweep_interval_is_the_setting_or_the_variable(monkeypatch):
    """Divergence 030: the sweep runs every `MESHY_WS_REVOCATION_SWEEP_SECS` (a setting or
    the environment variable), default 10 s, never below the floor. Every test above that
    turns the sweep OFF (NO_SWEEP) relies on this read; before this test they passed
    because they finish inside the 10 s default, whether or not the setting was read.

    Review finding F7. Mutant killed: A4, the setting never read (`raw = None`)."""
    from apps.wsgw import constants

    monkeypatch.delenv("MESHY_WS_REVOCATION_SWEEP_SECS", raising=False)
    with override_settings(MESHY_WS_REVOCATION_SWEEP_SECS=None):
        assert mesh_notify.revocation_sweep_secs() == constants.REVOCATION_SWEEP_SECS == 10.0
        monkeypatch.setenv("MESHY_WS_REVOCATION_SWEEP_SECS", "2.5")
        assert mesh_notify.revocation_sweep_secs() == 2.5
    with override_settings(MESHY_WS_REVOCATION_SWEEP_SECS=NO_SWEEP):
        assert mesh_notify.revocation_sweep_secs() == NO_SWEEP
    with override_settings(MESHY_WS_REVOCATION_SWEEP_SECS=0.0):
        assert mesh_notify.revocation_sweep_secs() == constants.REVOCATION_SWEEP_MIN_SECS
    with override_settings(MESHY_WS_REVOCATION_SWEEP_SECS="not a number"):
        assert mesh_notify.revocation_sweep_secs() == constants.REVOCATION_SWEEP_SECS
