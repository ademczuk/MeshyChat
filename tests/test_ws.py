"""The WebSocket hub, proved against a real socket and a real database.

WHAT THESE ARE FOR. Four of them decide whether the hub can be put in front of
traffic: an unauthenticated upgrade must be REFUSED, a valid one ACCEPTED, the
per-peer connection cap ENFORCED, and the send queue must drop the OLDEST frame
when it overflows. The rest pin contracts that are easy to drift on (the wire
tag spelling, the aud rule, DPoP replay, credential expiry).

EVERY NEGATIVE HAS A POSITIVE CONTROL IN THE SAME TEST. That is not ceremony
here - it is the only thing separating these assertions from vacuous ones:

  * "the upgrade was refused" and "the consumer crashed on import" produce the
    same (False, code) from the communicator;
  * "the cap rejected the 9th connection" and "every connection was rejected"
    look identical if you only assert the 9th;
  * "the queue dropped the oldest" and "the queue dropped the newest" leave the
    SAME LENGTH behind, so the length assertion is worthless on its own and the
    test asserts WHICH frames survived;
  * "DPoP refused a replay" and "DPoP refuses everything" are the same
    observation unless the first use is asserted to succeed.

No mocks: the peers rows, the dpop_jti_seen ledger and the agent_cards row are
real, in the same database apps/core/sqlmigrate.py built.

Run:  .venv/Scripts/python.exe -m pytest tests/test_ws.py -q
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import os
import sys
import time
import uuid
from pathlib import Path

import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import jwt as pyjwt

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")

import django  # noqa: E402

django.setup()

from channels.testing import WebsocketCommunicator  # noqa: E402

from apps.crypto import primitives as P  # noqa: E402
from apps.wsgw import auth, didkey, envelope, frame_handlers  # noqa: E402
from apps.wsgw.connection_manager import (  # noqa: E402
    ConnectionHandle,
    ConnectionManager,
    TooManyConnections,
)
from apps.wsgw.constants import (  # noqa: E402
    CLOSE_UNIMPLEMENTED,
    MAX_CONNS_PER_PEER,
    MAX_PRESENCE_SUBSCRIPTIONS_PER_CONN,
    NEGOTIATED_PROTOCOL,
    SEND_QUEUE_CAPACITY,
    WS_PATH,
)
from apps.wsgw.consumer import HubConfig, MeshyWebSocketConsumer  # noqa: E402
from apps.wsgw.presence import RecordingPresenceBackend  # noqa: E402
from apps.wsgw.send_queue import SendQueue  # noqa: E402

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")

ISSUER = "meshy-test-ws"


# --------------------------------------------------------------------------
# fixtures: real keys, real rows
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def keys() -> tuple[bytes, bytes]:
    """An Ed25519 signing keypair in PKCS#8/SPKI PEM."""
    sk = Ed25519PrivateKey.generate()
    sk_pem = sk.private_bytes(serialization.Encoding.PEM,
                              serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption())
    pk_pem = sk.public_key().public_bytes(serialization.Encoding.PEM,
                                          serialization.PublicFormat.SubjectPublicKeyInfo)
    return sk_pem, pk_pem


@pytest.fixture(scope="module")
def owner_conn():
    """Owner role, used ONLY to seed fixtures (same posture as test_rls_isolation)."""
    with psycopg.connect(DSN) as conn:
        yield conn


def _seed_peer(conn, tag: str, status: str = "active") -> uuid.UUID:
    """A dashboard_users owner + a peers row. Column names and the owner FK
    target (`dashboard_users`, NOT `owners`) read off the live schema."""
    owner_id, peer_id = uuid.uuid4(), uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO dashboard_users (id, email, plane) VALUES (%s,%s,'product')",
                    (owner_id, f"ws-{tag}-{owner_id.hex[:8]}@example.test"))
        cur.execute("INSERT INTO peers (id, owner_id, name, display_name, status) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    (peer_id, owner_id, f"ws-{tag}-{peer_id.hex[:8]}", f"ws {tag}", status))
    conn.commit()
    return peer_id


@pytest.fixture(scope="module")
def active_peer(owner_conn) -> uuid.UUID:
    return _seed_peer(owner_conn, "active", status="active")


@pytest.fixture(scope="module")
def second_peer(owner_conn) -> uuid.UUID:
    return _seed_peer(owner_conn, "second", status="active")


@pytest.fixture(scope="module")
def inactive_peer(owner_conn) -> uuid.UUID:
    """An inactive peer is refused with 403.

    'suspended', not 'revoked': `peers_status_check` on the live schema is
    CHECK (status = ANY (ARRAY['active','suspended','deleted'])). Read off
    pg_constraint after the first guess was rejected by the database.
    """
    return _seed_peer(owner_conn, "inactive", status="suspended")


def mint(sk_pem: bytes, *, sub: uuid.UUID, aud: str | None = "peer", ttl: int = 60,
         cnf: str | None = None, act: uuid.UUID | None = None,
         iss: str = ISSUER) -> str:
    """A peer JWT with the claims the gateway validates."""
    now = int(time.time())
    claims: dict = {"sub": str(sub), "iss": iss, "iat": now, "exp": now + ttl, "scopes": []}
    if aud is not None:
        claims["aud"] = aud
    if cnf is not None:
        claims["cnf"] = cnf
    if act is not None:
        claims["act"] = {"sub": str(act)}
    return pyjwt.encode(claims, sk_pem, algorithm="EdDSA")


def hub(pk_pem: bytes, manager: ConnectionManager, presence=None, **overrides):
    """One ASGI app instance shares `manager` across every communicator.

    DPoP is OFF here unless a test asks for it.
    HubConfig itself defaults it ON (divergence 015); these tests mint bearer tokens."""
    overrides.setdefault("dpop_enforce", False)
    config = HubConfig(jwt_public_key_pem=pk_pem, jwt_issuer=ISSUER, **overrides)
    return MeshyWebSocketConsumer.as_asgi(
        config=config, manager=manager, presence=presence or RecordingPresenceBackend()
    )


def protocols(token: str | None, *extra: str) -> list[str]:
    """Build the negotiated protocol and optional JWT protocol entry."""
    entries = [NEGOTIATED_PROTOCOL]
    if token is not None:
        entries.append(f"jwt.{token}")
    entries.extend(extra)
    return entries


async def _open(app, subprotocols, path: str = WS_PATH, headers=None):
    return WebsocketCommunicator(app, path, headers=headers or [], subprotocols=subprotocols)


# ==========================================================================
# 1. the upgrade: refused without credentials, accepted with them
# ==========================================================================

@pytest.mark.asyncio
async def test_unauthenticated_upgrade_is_refused_and_an_authenticated_one_is_not(
    keys, active_peer
):
    """THE CORE NEGATIVE, with its control in the same test.

    Four unauthenticated shapes must all be refused. On their own that proves
    nothing: a consumer that refuses EVERY upgrade - or that raises on import -
    passes all four identically. The last block is the control: the same app,
    the same path, a real token, accepted.
    """
    sk_pem, pk_pem = keys
    manager = ConnectionManager()
    app = hub(pk_pem, manager)

    refused = {}

    # (a) no Sec-WebSocket-Protocol at all -> MissingProtocol.
    comm = await _open(app, [])
    refused["no subprotocol"] = await comm.connect(timeout=5)
    await comm.disconnect()

    # (b) the negotiated protocol but no jwt. entry -> NoJwtEntry.
    comm = await _open(app, protocols(None))
    refused["no jwt entry"] = await comm.connect(timeout=5)
    await comm.disconnect()

    # (c) a token signed by the WRONG key -> TokenInvalid.
    other_sk = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption())
    comm = await _open(app, protocols(mint(other_sk, sub=active_peer)))
    refused["foreign signing key"] = await comm.connect(timeout=5)
    await comm.disconnect()

    # (d) a syntactically valid but expired token. 90 s past `exp` is beyond the 60 s
    # leeway: -30 s is inside that leeway and would be accepted. This test uses -90 s (STATUS B13;
    # the leeway itself is pinned at the upgrade by tests/test_jwt_one_verifier.py).
    expired = mint(sk_pem, sub=active_peer, ttl=-90)
    comm = await _open(app, protocols(expired))
    refused["expired token"] = await comm.connect(timeout=5)
    await comm.disconnect()

    for label, (accepted, code) in refused.items():
        assert accepted is False, f"{label}: the hub ACCEPTED an unauthenticated upgrade"
        assert code == 4401, f"{label}: expected the 401-equivalent close, got {code}"

    # POSITIVE CONTROL: the identical app accepts a properly credentialed peer,
    # so the four refusals above are the auth gate deciding and not the hub
    # being broken.
    comm = await _open(app, protocols(mint(sk_pem, sub=active_peer)))
    accepted, subprotocol = await comm.connect(timeout=5)
    assert accepted is True, "control failed: a VALID upgrade was also refused"
    assert subprotocol == NEGOTIATED_PROTOCOL, (
        "the handshake must answer with meshy.v1 and only meshy.v1; "
        f"got {subprotocol!r}"
    )
    await comm.disconnect()


@pytest.mark.asyncio
async def test_accepted_upgrade_registers_the_connection_and_sends_welcome(keys, active_peer):
    """The accept path end to end: registry, presence, welcome frame."""
    sk_pem, pk_pem = keys
    manager = ConnectionManager()
    presence = RecordingPresenceBackend()
    app = hub(pk_pem, manager, presence)

    # Control: nothing is registered before the socket opens, so the assertions
    # after it cannot be reading a pre-existing state.
    assert await manager.is_online(active_peer) is False

    comm = await _open(app, protocols(mint(sk_pem, sub=active_peer)))
    accepted, _ = await comm.connect(timeout=5)
    assert accepted is True

    welcome = json.loads(await comm.receive_from(timeout=5))
    assert welcome["type"] == "welcome", f"first frame must be welcome, got {welcome}"
    # Both fields are present and typed.
    uuid.UUID(welcome["connection_id"])
    _dt.datetime.fromisoformat(welcome["server_time"].replace("Z", "+00:00"))

    assert await manager.is_online(active_peer) is True
    assert ("online", active_peer) == presence.events[0][:2]

    await comm.disconnect()
    await asyncio.sleep(0)
    assert await manager.is_online(active_peer) is False, "deregister did not run"
    assert presence.events[-1][0] == "offline", (
        "the peer's last socket closing must flip presence offline"
    )


@pytest.mark.asyncio
async def test_a_non_peer_audience_and_an_inactive_peer_are_both_refused(
    keys, active_peer, inactive_peer
):
    """Audience and peer status are each checked beside an accepted case."""
    sk_pem, pk_pem = keys
    app = hub(pk_pem, ConnectionManager())

    for aud, label in [("dashboard", "dashboard token"), ("smoke-test", "smoke-test token"),
                       (None, "no aud claim")]:
        comm = await _open(app, protocols(mint(sk_pem, sub=active_peer, aud=aud)))
        accepted, code = await comm.connect(timeout=5)
        await comm.disconnect()
        assert accepted is False, f"{label} was accepted on the peer socket"
        assert code == 4401

    # A real token for a peer whose row says status != 'active'.
    comm = await _open(app, protocols(mint(sk_pem, sub=inactive_peer)))
    accepted, code = await comm.connect(timeout=5)
    await comm.disconnect()
    assert accepted is False, "an inactive peer was allowed to connect"
    assert code == 4403, f"inactive peer must map to the 403-equivalent, got {code}"

    # POSITIVE CONTROL: aud=peer AND status='active' -> accepted. Without this,
    # the four refusals above are also what a hub that refuses everything does.
    comm = await _open(app, protocols(mint(sk_pem, sub=active_peer, aud="peer")))
    accepted, _ = await comm.connect(timeout=5)
    assert accepted is True, "control failed: aud=peer on an active peer was refused"
    await comm.disconnect()


@pytest.mark.asyncio
async def test_duplicate_jwt_entries_are_refused(keys, active_peer, second_peer):
    """Two DIFFERING jwt. entries would let the socket
    authenticate on one token while the expiry deadline tracked another."""
    sk_pem, pk_pem = keys
    app = hub(pk_pem, ConnectionManager())

    a = mint(sk_pem, sub=active_peer, ttl=60)
    b = mint(sk_pem, sub=second_peer, ttl=3600)
    comm = await _open(app, [NEGOTIATED_PROTOCOL, f"jwt.{a}", f"jwt.{b}"])
    accepted, code = await comm.connect(timeout=5)
    await comm.disconnect()
    assert accepted is False, "two jwt. entries were accepted; which token bounds the socket?"
    assert code == 4401

    # POSITIVE CONTROL: either token ALONE is accepted, so the refusal is the
    # duplicate rule firing and not either token being bad.
    for token, label in [(a, "first token"), (b, "second token")]:
        comm = await _open(app, protocols(token))
        accepted, _ = await comm.connect(timeout=5)
        assert accepted is True, f"control failed: {label} alone was refused"
        await comm.disconnect()


# ==========================================================================
# 2. the per-peer connection cap
# ==========================================================================

@pytest.mark.asyncio
async def test_per_peer_connection_cap_is_enforced_over_real_sockets(
    keys, active_peer, second_peer
):
    """MAX_CONNS_PER_PEER = 8, enforced end to end.

    The controls are what make the 9th refusal meaningful:
      * connections 1..8 are each asserted ACCEPTED (so the cap is not "refuse
        everything");
      * a DIFFERENT peer connects fine while the first is at its cap (so the cap
        is per-PEER, not global);
      * closing one socket frees exactly one slot (so it is a live count, not a
        one-way counter).
    """
    sk_pem, pk_pem = keys
    manager = ConnectionManager()
    app = hub(pk_pem, manager)
    token = mint(sk_pem, sub=active_peer, ttl=300)

    opened = []
    for i in range(MAX_CONNS_PER_PEER):
        comm = await _open(app, protocols(token))
        accepted, _ = await comm.connect(timeout=5)
        assert accepted is True, f"connection {i + 1} of {MAX_CONNS_PER_PEER} was refused"
        assert json.loads(await comm.receive_from(timeout=5))["type"] == "welcome"
        opened.append(comm)

    assert await manager.connection_count(active_peer) == MAX_CONNS_PER_PEER

    # The one over the cap upgrades, then sends
    # a TOO_MANY_CONNECTIONS error frame and closes - so "accepted" is
    # True here and the refusal is in the frame. Asserting only on `accepted`
    # would have this test pass against a hub with no cap at all.
    over = await _open(app, protocols(token))
    accepted, _ = await over.connect(timeout=5)
    assert accepted is True, "the connection opens before the refusal frame is sent"
    frame = json.loads(await over.receive_from(timeout=5))
    assert frame == {"type": "error",
                     "code": "TOO_MANY_CONNECTIONS",
                     "message": "per-peer connection limit reached"}, frame
    assert (await over.receive_output(timeout=5))["type"] == "websocket.close"
    await over.disconnect()

    assert await manager.connection_count(active_peer) == MAX_CONNS_PER_PEER, (
        "the refused connection must not have been registered"
    )

    # CONTROL A: the cap is per-peer. A different peer is unaffected.
    other = await _open(app, protocols(mint(sk_pem, sub=second_peer, ttl=300)))
    accepted, _ = await other.connect(timeout=5)
    assert accepted is True, "another peer was blocked by the first peer's cap"
    assert json.loads(await other.receive_from(timeout=5))["type"] == "welcome"
    await other.disconnect()

    # CONTROL B: closing one socket frees exactly one slot.
    await opened.pop().disconnect()
    await asyncio.sleep(0)
    assert await manager.connection_count(active_peer) == MAX_CONNS_PER_PEER - 1
    replacement = await _open(app, protocols(token))
    accepted, _ = await replacement.connect(timeout=5)
    assert accepted is True, "a freed slot was not reusable"
    assert json.loads(await replacement.receive_from(timeout=5))["type"] == "welcome"
    opened.append(replacement)

    for comm in opened:
        await comm.disconnect()


@pytest.mark.asyncio
async def test_connection_manager_cap_at_the_registry_level():
    """The same cap without a socket, so a failure localises to the registry.

    `register` is the TOCTOU-sensitive step: the
    length check and the append must not be separable.
    """
    manager = ConnectionManager()
    peer = uuid.uuid4()

    def handle(p=peer):
        return ConnectionHandle(connection_id=uuid.uuid4(), peer_id=p, queue=SendQueue())

    for _ in range(MAX_CONNS_PER_PEER):
        await manager.register(handle())
    assert await manager.connection_count(peer) == MAX_CONNS_PER_PEER

    with pytest.raises(TooManyConnections):
        await manager.register(handle())

    # CONTROL: a different peer still registers, and the over-cap attempt left
    # the first peer's count untouched.
    await manager.register(handle(uuid.uuid4()))
    assert await manager.connection_count(peer) == MAX_CONNS_PER_PEER
    assert await manager.peer_count() == 2


# ==========================================================================
# 3. the send queue: drop-OLDEST
# ==========================================================================

@pytest.mark.asyncio
async def test_send_queue_drops_the_oldest_frame_when_full():
    """THE ASSERTION IS WHICH FRAME SURVIVED.

    Length alone cannot tell drop-oldest from drop-newest: both leave exactly
    `capacity` frames. So the queue is filled with individually identifiable
    frames and the survivors are compared by identity.
    """
    queue = SendQueue(capacity=3)
    for i in range(3):
        await queue.push(envelope.subscribed([f"ch-{i}"]))

    # CONTROL, before any overflow: nothing was dropped and the order is FIFO.
    assert len(queue) == 3
    assert queue.dropped == 0
    assert [f["channels"][0] for f in queue.snapshot()] == ["ch-0", "ch-1", "ch-2"]

    # Overflow by two.
    await queue.push(envelope.subscribed(["ch-3"]))
    await queue.push(envelope.subscribed(["ch-4"]))

    assert len(queue) == 3, "the queue grew past its capacity"
    assert queue.dropped == 2
    survivors = [f["channels"][0] for f in queue.snapshot()]
    assert survivors == ["ch-2", "ch-3", "ch-4"], (
        f"drop-OLDEST must keep the newest frames in order; survivors were {survivors}. "
        "['ch-0','ch-1','ch-2'] would mean drop-newest, which is the opposite policy."
    )
    # And the survivors come OUT oldest-first.
    assert (await queue.pop())["channels"] == ["ch-2"]
    assert (await queue.pop())["channels"] == ["ch-3"]
    assert (await queue.pop())["channels"] == ["ch-4"]


@pytest.mark.asyncio
async def test_send_queue_drop_oldest_at_the_production_capacity():
    """The same policy at SEND_QUEUE_CAPACITY (256), the production socket capacity.

    This is not only true of a toy size.
    """
    queue = SendQueue()
    assert queue.capacity == SEND_QUEUE_CAPACITY

    for i in range(SEND_QUEUE_CAPACITY):
        await queue.push(envelope.subscribed([f"ch-{i}"]))
    # CONTROL: exactly at capacity, nothing is dropped and the head is frame 0.
    assert queue.dropped == 0
    assert queue.snapshot()[0]["channels"] == ["ch-0"]

    await queue.push(envelope.pong())
    assert queue.dropped == 1
    assert len(queue) == SEND_QUEUE_CAPACITY
    assert queue.snapshot()[0]["channels"] == ["ch-1"], "the HEAD frame should be gone"
    assert queue.snapshot()[-1] == {"type": "pong"}, "the newest frame must have survived"


@pytest.mark.asyncio
async def test_send_queue_blocks_then_wakes_and_refuses_zero_capacity():
    """Pop waits for push, and capacity must be positive."""
    queue = SendQueue()
    task = asyncio.create_task(queue.pop())
    await asyncio.sleep(0)
    assert not task.done(), "pop returned on an empty queue"
    await queue.push(envelope.pong())
    assert await asyncio.wait_for(task, timeout=2) == {"type": "pong"}

    with pytest.raises(ValueError):
        SendQueue(capacity=0)
    # CONTROL: capacity 1 is fine, so the raise is about zero and not about
    # small queues in general.
    assert SendQueue(capacity=1).capacity == 1


# ==========================================================================
# 4. frames: envelope shape, ping, subscribe/unsubscribe, skill_* refusal
# ==========================================================================

def test_frame_type_tags_are_snake_case_not_dotted():
    """Top-level frame `type` tags are snake_case, not dotted.

    The signed envelope inside a skill frame uses dotted
    kinds (`"kind": "skill.call"`), so the two spellings sit in one JSON object
    and an incorrect builder can easily emit `"type": "skill.call"`.
    """
    assert envelope.SKILL_CALL == "skill_call"
    assert envelope.SKILL_RESUME == "skill_resume"
    assert "." not in envelope.SKILL_CALL

    # The parser must therefore treat the dotted spelling as UNKNOWN...
    assert envelope.parse_client_frame({"type": "skill.call"}).type == envelope.UNKNOWN
    # ...and the snake_case spelling as the real variant. Without this control
    # the line above also passes for a parser that recognises nothing.
    assert envelope.parse_client_frame({"type": "skill_call"}).type == envelope.SKILL_CALL

    assert envelope.pong() == {"type": "pong"}, "a unit variant is the tag ALONE"
    assert envelope.subscribed(["a"]) == {"type": "subscribed", "channels": ["a"]}
    assert envelope.error("PARSE", "x") == {"type": "error", "code": "PARSE", "message": "x"}


@pytest.mark.asyncio
async def test_ping_subscribe_and_unsubscribe_over_a_live_socket(keys, active_peer):
    """Ping, subscribe, and unsubscribe work over a live socket."""
    sk_pem, pk_pem = keys
    manager = ConnectionManager()
    presence = RecordingPresenceBackend()
    app = hub(pk_pem, manager, presence)

    comm = await _open(app, protocols(mint(sk_pem, sub=active_peer, ttl=300)))
    assert (await comm.connect(timeout=5))[0] is True
    assert json.loads(await comm.receive_from(timeout=5))["type"] == "welcome"

    await comm.send_json_to({"type": "ping"})
    assert json.loads(await comm.receive_from(timeout=5)) == {"type": "pong"}
    assert any(e[0] == "refresh" for e in presence.events), (
        "ping must also refresh the presence TTL"
    )

    watched = uuid.uuid4()
    await comm.send_json_to({"type": "subscribe", "channels": [f"presence:{watched}"]})
    assert json.loads(await comm.receive_from(timeout=5)) == {
        "type": "subscribed", "channels": [f"presence:{watched}"]}

    # The subscription is real: a presence publish reaches this socket.
    assert await manager.publish_presence(watched, envelope.peer_online(watched)) == 1
    assert json.loads(await comm.receive_from(timeout=5)) == {
        "type": "peer_online", "peer_id": str(watched)}

    await comm.send_json_to({"type": "unsubscribe", "channels": [f"presence:{watched}"]})
    assert json.loads(await comm.receive_from(timeout=5)) == {
        "type": "unsubscribed", "channels": [f"presence:{watched}"]}
    # CONTROL for the unsubscribe: the SAME publish now reaches nobody. Without
    # the fan-out above, a zero here would also be what a broken publish returns.
    assert await manager.publish_presence(watched, envelope.peer_online(watched)) == 0

    # Malformed JSON and an unknown tag both get an Error frame, not silence.
    await comm.send_to(text_data="{not json")
    assert json.loads(await comm.receive_from(timeout=5))["code"] == "PARSE"
    await comm.send_json_to({"type": "no_such_frame"})
    assert json.loads(await comm.receive_from(timeout=5))["code"] == "UNKNOWN_FRAME"

    await comm.disconnect()


#: Divergence 041 S4 builds the TARGET's four frames, S6 the CALLER's two and S7 skill_chunk;
#: this is the skill frame still without a handler (skill_resume stays refused, design 2.10).
NOT_YET_BUILT = frozenset({"skill_resume"})
#: The frames a target answers a call with, handled since 041 S4, and skill_chunk since S7
#: (apps/wsgw/target_frames.py).
TARGET_FRAMES = frozenset({"skill_ack", "skill_running", "skill_chunk", "skill_result",
                           "skill_error"})
#: The frames a caller sends, handled since 041 S6 (apps/wsgw/caller_frames.py).
CALLER_FRAMES = frozenset({"skill_call", "skill_cancel"})


@pytest.mark.asyncio
async def test_skill_frames_raise_rather_than_being_silently_accepted(keys, active_peer):
    """The skill_* frames without a handler are an explicit stub. A handler that accepted and
    did nothing would be indistinguishable, client-side, from a working one until the call
    timed out.

    Since 041 S4 the four target frames have handlers, and since S6 the caller's two: a bare
    tag is a SHAPE failure there (a ValueError, which the consumer answers PARSE), never the
    stub's raise. And since S6 a `call:` subscribe is handled: acked, and silent for a call
    this context cannot read."""
    assert NOT_YET_BUILT | TARGET_FRAMES | CALLER_FRAMES == envelope.SKILL_CLIENT_FRAMES
    assert not NOT_YET_BUILT & (TARGET_FRAMES | CALLER_FRAMES)
    ctx = frame_handlers.FrameContext(
        manager=ConnectionManager(), presence=RecordingPresenceBackend(),
        peer_id=uuid.uuid4(), connection_id=uuid.uuid4(), queue=SendQueue(),
    )
    for tag in sorted(NOT_YET_BUILT):
        with pytest.raises(frame_handlers.SkillFrameNotImplemented):
            await frame_handlers.dispatch(envelope.ClientFrame(type=tag, data={"type": tag}), ctx)
    for tag in sorted(TARGET_FRAMES | CALLER_FRAMES):
        with pytest.raises(ValueError):
            await frame_handlers.dispatch(envelope.ClientFrame(type=tag, data={"type": tag}), ctx)
    # `subscribe` also handles `call:<id>` channels (041 S6): with no database in this
    # context the ownership read fails, so nothing is registered, and the ack still echoes.
    await frame_handlers.dispatch(
        envelope.ClientFrame(type=envelope.SUBSCRIBE,
                             data={"type": "subscribe", "channels": ["call:abc"]}), ctx)
    assert queue_tail(ctx.queue) == {"type": "subscribed", "channels": ["call:abc"]}

    # CONTROL: the implemented frames do NOT raise, so the raises above are
    # about the skill family and not about dispatch being broken.
    await frame_handlers.dispatch(envelope.ClientFrame(type=envelope.PING, data={}), ctx)
    assert queue_tail(ctx.queue) == {"type": "pong"}

    # And over a real socket: refused in a frame, then the socket is closed.
    sk_pem, pk_pem = keys
    comm = await _open(hub(pk_pem, ConnectionManager()),
                       protocols(mint(sk_pem, sub=active_peer, ttl=300)))
    assert (await comm.connect(timeout=5))[0] is True
    assert json.loads(await comm.receive_from(timeout=5))["type"] == "welcome"
    await comm.send_json_to({"type": "skill_resume", "call_id": "c-1", "context_id": "x",
                             "action": "accept", "continuation_token": "t"})
    refusal = json.loads(await comm.receive_from(timeout=5))
    assert refusal["type"] == "error" and refusal["code"] == "UNIMPLEMENTED", refusal
    assert (await comm.receive_output(timeout=5))["type"] == "websocket.close"
    await comm.disconnect()


#: One WELL-SHAPED instance of every skill client frame, with the field types each variant
#: requires. Typed parsing of these exists since divergence 041's first slice; the
#: four target frames have handlers since S4, and the rest are still refused.
_ENV = {"payload": {"v": 1, "kind": "skill.call"}, "signature": "AAAA", "signing_kid": "kid-1"}
WELL_SHAPED_SKILL_FRAMES = {
    "skill_call": {"type": "skill_call", "envelope": _ENV, "mode_override": "sync"},
    "skill_ack": {"type": "skill_ack", "call_id": "c-1"},
    "skill_running": {"type": "skill_running", "call_id": "c-1"},
    "skill_chunk": {"type": "skill_chunk", "call_id": "c-1", "sequence": 0,
                    "chunk_type": "text", "envelope": _ENV},
    "skill_result": {"type": "skill_result", "call_id": "c-1", "envelope": _ENV},
    "skill_error": {"type": "skill_error", "call_id": "c-1", "error_code": "E_X",
                    "error_message": "m"},
    "skill_cancel": {"type": "skill_cancel", "call_id": "c-1", "reason": "r"},
    "skill_resume": {"type": "skill_resume", "call_id": "c-1", "context_id": "x",
                     "action": "accept", "continuation_token": "t"},
}
#: Frames the typed parser refuses by value (SKILL_CALL_BAD, SKILL_CHUNK_BAD, SKILL_ERROR_BAD).
#: Until a handler parses them they must meet the SAME refusal as a well-shaped frame: a
#: parser wired in front of the refusal would answer these with a _BAD code and leave the
#: socket open, which is a handler.
OUT_OF_DOMAIN_SKILL_FRAMES = [
    {"type": "skill_call", "envelope": _ENV, "mode_override": "lesson"},
    {"type": "skill_chunk", "call_id": "c-1", "sequence": 2147483648,
     "chunk_type": "text", "envelope": _ENV},
    {"type": "skill_error", "call_id": "c-1", "error_code": "E" * 129, "error_message": "m"},
]
#: Frames the typed parser refuses by SHAPE (a ValueError, which the consumer answers PARSE):
#: each tag with none of its fields, and a call_id that is not a call id. Until a handler
#: parses them they too must meet the same refusal. A parser wired in front of the refusal
#: would answer these PARSE and leave the socket open (S1 review F4: that mutant passed a
#: control that sent only well-typed frames).
MALFORMED_SKILL_FRAMES = ([{"type": tag} for tag in sorted(envelope.SKILL_CLIENT_FRAMES)]
                          + [{"type": "skill_ack", "call_id": "a\nforged log line"}])


@pytest.mark.asyncio
async def test_every_skill_frame_is_still_refused_unimplemented_and_closed_1011(
        keys, active_peer):
    """POSITIVE CONTROL for divergence 041, narrowed by S4 and S6 to the frames still without
    a handler (NOT_YET_BUILT). Each of them, well shaped, out of its domain or malformed, must
    still get the `UNIMPLEMENTED` error frame and then a close with 1011 (frame_handlers.py,
    consumer.py's SkillFrameNotImplemented branch), over a real socket. The older test above
    sends one frame and checks only that a close happens. The four target frames are the next
    test's, the caller's two and a `call:` subscribe the one after it.

    Control in the same test (C1): pings on the same kind of socket get their pongs and the
    socket stays open, so the closes below are about the frames, not the harness."""
    sk_pem, pk_pem = keys
    assert set(WELL_SHAPED_SKILL_FRAMES) == envelope.SKILL_CLIENT_FRAMES, (
        "every skill tag must be exercised; a new tag would otherwise escape this control")

    async def fresh():
        comm = await _open(hub(pk_pem, ConnectionManager()),
                           protocols(mint(sk_pem, sub=active_peer, ttl=300)))
        assert (await comm.connect(timeout=5))[0] is True
        assert json.loads(await comm.receive_from(timeout=5))["type"] == "welcome"
        return comm

    # CONTROL: the harness keeps a socket open across frames it implements.
    comm = await fresh()
    for _ in range(2):
        await comm.send_json_to({"type": "ping"})
        assert json.loads(await comm.receive_from(timeout=5)) == {"type": "pong"}
    await comm.disconnect()

    # The inputs are what they claim: each malformed frame fails the parser's SHAPE layer.
    for frame in MALFORMED_SKILL_FRAMES:
        with pytest.raises(ValueError) as caught:
            envelope.parse_skill_frame(envelope.parse_client_frame(frame))
        assert not isinstance(caught.value, envelope.SkillFrameBad), frame
    assert {f["type"] for f in MALFORMED_SKILL_FRAMES} == envelope.SKILL_CLIENT_FRAMES

    def unbuilt(frames):
        return [f for f in frames if f["type"] in NOT_YET_BUILT]

    cases = ([(tag, frame) for tag, frame in sorted(WELL_SHAPED_SKILL_FRAMES.items())
              if tag in NOT_YET_BUILT]
             + [(f"{f['type']} (out of domain)", f) for f in unbuilt(OUT_OF_DOMAIN_SKILL_FRAMES)]
             + [(f"{f['type']} (malformed)", f) for f in unbuilt(MALFORMED_SKILL_FRAMES)])
    refused = []
    for label, frame in cases:
        comm = await fresh()
        await comm.send_json_to(frame)
        reply = json.loads(await comm.receive_from(timeout=5))
        assert reply["type"] == "error" and reply["code"] == envelope.CODE_UNIMPLEMENTED, (
            label, reply)
        closing = await comm.receive_output(timeout=5)
        assert closing.get("type") == "websocket.close", (label, closing)
        assert closing.get("code") == CLOSE_UNIMPLEMENTED == 1011, (label, closing)
        await comm.disconnect()
        refused.append(label)
    assert len(refused) == 2, f"C6: every case must have run, ran {refused}"


@pytest.mark.asyncio
async def test_the_target_frames_are_answered_on_the_queue_and_the_socket_stays_open(
        keys, active_peer):
    """Divergence 041 S4: the four target frames have handlers, and since S7 skill_chunk. Every
    case the control above gave them now gets an error frame THROUGH THE SEND QUEUE, and the
    socket stays open: the pong to a ping sent after each is the next frame. Well shaped, for a
    call nobody has, the frame's own `_FAILED` code with the uniform text (the result's and the
    chunk's envelopes here are signed for `skill.call`, so each fails verification first, with
    its E_SIG code); out of its domain, its `_BAD` code; malformed, PARSE. All on ONE
    socket."""
    sk_pem, pk_pem = keys
    comm = await _open(hub(pk_pem, ConnectionManager()),
                       protocols(mint(sk_pem, sub=active_peer, ttl=300)))
    assert (await comm.connect(timeout=5))[0] is True
    assert json.loads(await comm.receive_from(timeout=5))["type"] == "welcome"
    uniform = "call is not routed to this peer"
    expected = {
        "skill_ack": ("SKILL_ACK_FAILED", uniform),
        "skill_running": ("SKILL_RUNNING_FAILED", uniform),
        "skill_result": ("E_SIG_KIND_MISMATCH", None),
        "skill_chunk": ("E_SIG_KIND_MISMATCH", None),
        "skill_error": ("SKILL_ERROR_FAILED", uniform),
    }
    out_of_domain = {"skill_error": "SKILL_ERROR_BAD", "skill_chunk": "SKILL_CHUNK_BAD"}
    cases = ([(tag, WELL_SHAPED_SKILL_FRAMES[tag], *expected[tag])
              for tag in sorted(TARGET_FRAMES)]
             + [(f"{f['type']} (out of domain)", f, out_of_domain[f["type"]], None)
                for f in OUT_OF_DOMAIN_SKILL_FRAMES if f["type"] in TARGET_FRAMES]
             + [(f"{f['type']} (malformed)", f, "PARSE", None)
                for f in MALFORMED_SKILL_FRAMES if f["type"] in TARGET_FRAMES])
    answered = []
    try:
        for label, frame, code, message in cases:
            await comm.send_json_to(frame)
            await comm.send_json_to({"type": "ping"})
            reply = await comm.receive_output(timeout=5)
            assert reply.get("type") == "websocket.send", (label, reply)
            reply = json.loads(reply["text"])
            assert reply["type"] == "error" and reply["code"] == code, (label, reply)
            if message is not None:
                assert reply["message"] == message, (label, reply)
            assert json.loads(await comm.receive_from(timeout=5)) == {"type": "pong"}, label
            answered.append(label)
    finally:
        await comm.disconnect()
    assert len(answered) == 13, f"C6: every case must have run, ran {answered}"


@pytest.mark.asyncio
async def test_the_caller_frames_are_answered_on_the_queue_and_the_socket_stays_open(
        keys, active_peer):
    """Divergence 041 S6: skill_call, skill_cancel and a `call:` subscribe have handlers. Out of
    its domain, skill_call is SKILL_CALL_BAD (a `lesson` override, 2.12.7); malformed, either
    frame is PARSE; a well-shaped skill_cancel for a call nobody has is SKILL_CANCEL_FAILED
    with the one refusal text (Q7); a `call:` subscribe and unsubscribe for such a call are
    acked with their channels and nothing else. All on ONE socket: the pong to a ping sent
    after each is the next frame, so the socket stayed open."""
    sk_pem, pk_pem = keys
    comm = await _open(hub(pk_pem, ConnectionManager()),
                       protocols(mint(sk_pem, sub=active_peer, ttl=300)))
    assert (await comm.connect(timeout=5))[0] is True
    assert json.loads(await comm.receive_from(timeout=5))["type"] == "welcome"
    cases = ([(f"{f['type']} (out of domain)", f,
               {"type": "error", "code": "SKILL_CALL_BAD"})
              for f in OUT_OF_DOMAIN_SKILL_FRAMES if f["type"] in CALLER_FRAMES]
             + [(f"{f['type']} (malformed)", f, {"type": "error", "code": "PARSE"})
                for f in MALFORMED_SKILL_FRAMES if f["type"] in CALLER_FRAMES]
             + [("skill_cancel, nobody's call", WELL_SHAPED_SKILL_FRAMES["skill_cancel"],
                 {"type": "error", "code": "SKILL_CANCEL_FAILED",
                  "message": "call was not made by this peer"}),
                ("subscribe call:", {"type": "subscribe", "channels": ["call:c-1"]},
                 {"type": "subscribed", "channels": ["call:c-1"]}),
                ("unsubscribe call:", {"type": "unsubscribe", "channels": ["call:c-1"]},
                 {"type": "unsubscribed", "channels": ["call:c-1"]})])
    answered = []
    try:
        for label, frame, expected in cases:
            await comm.send_json_to(frame)
            await comm.send_json_to({"type": "ping"})
            reply = await comm.receive_output(timeout=5)
            assert reply.get("type") == "websocket.send", (label, reply)
            reply = json.loads(reply["text"])
            assert {k: reply.get(k) for k in expected} == expected, (label, reply)
            assert json.loads(await comm.receive_from(timeout=5)) == {"type": "pong"}, label
            answered.append(label)
    finally:
        await comm.disconnect()
    assert len(answered) == 6, f"C6: every case must have run, ran {answered}"


def queue_tail(queue: SendQueue):
    return queue.snapshot()[-1]


@pytest.mark.asyncio
async def test_presence_subscription_cap_per_connection():
    """MAX_PRESENCE_SUBSCRIPTIONS_PER_CONN = 1000.

    Subscribe channels are caller-supplied UUIDs accepted without checking the
    peer exists, and one frame can carry hundreds of thousands of them.
    """
    manager = ConnectionManager()
    peer, conn_id = uuid.uuid4(), uuid.uuid4()
    await manager.register(ConnectionHandle(connection_id=conn_id, peer_id=peer,
                                            queue=SendQueue()))

    channels = [f"presence:{uuid.uuid4()}" for _ in range(MAX_PRESENCE_SUBSCRIPTIONS_PER_CONN + 50)]
    inserted = await manager.subscribe_presence(conn_id, channels)
    assert inserted == MAX_PRESENCE_SUBSCRIPTIONS_PER_CONN, inserted

    # A further frame inserts NOTHING: the cap is on the set, not on one frame.
    assert await manager.subscribe_presence(conn_id, [f"presence:{uuid.uuid4()}"]) == 0

    # CONTROL: under the cap, subscribing works and is not a no-op - and after
    # freeing a slot the next insert succeeds, so the zero above is the cap and
    # not a broken insert path.
    assert await manager.unsubscribe_presence(conn_id, channels[:1]) == 1
    assert await manager.subscribe_presence(conn_id, [f"presence:{uuid.uuid4()}"]) == 1

    # Non-presence and malformed channels are ignored rather than counted.
    assert await manager.subscribe_presence(conn_id, ["call:abc", "presence:not-a-uuid"]) == 0


# ==========================================================================
# 5. credential expiry and revocation
# ==========================================================================

@pytest.mark.asyncio
async def test_the_socket_is_force_closed_when_the_credential_expires(keys, active_peer):
    """A session may not outlive its token.

    The control is the long-lived token: it proves the close below is the
    DEADLINE firing rather than the hub dropping idle sockets.
    """
    sk_pem, pk_pem = keys
    app = hub(pk_pem, ConnectionManager())

    short = await _open(app, protocols(mint(sk_pem, sub=active_peer, ttl=2)))
    assert (await short.connect(timeout=5))[0] is True
    assert json.loads(await short.receive_from(timeout=5))["type"] == "welcome"

    long = await _open(app, protocols(mint(sk_pem, sub=active_peer, ttl=3600)))
    assert (await long.connect(timeout=5))[0] is True
    assert json.loads(await long.receive_from(timeout=5))["type"] == "welcome"

    event = await short.receive_output(timeout=6)
    assert event["type"] == "websocket.close", (
        f"the expired socket stayed open; got {event}")
    assert event.get("code") == 4401

    # CONTROL: over the same wall-clock window the long-lived socket is alive
    # and still serving frames.
    await long.send_json_to({"type": "ping"})
    assert json.loads(await long.receive_from(timeout=5)) == {"type": "pong"}

    await short.disconnect()
    await long.disconnect()


@pytest.mark.asyncio
async def test_drop_peer_tears_down_every_socket_that_peer_holds(keys, active_peer, second_peer):
    """Revocation tears down every socket held by a peer."""
    sk_pem, pk_pem = keys
    manager = ConnectionManager()
    app = hub(pk_pem, manager)

    a = await _open(app, protocols(mint(sk_pem, sub=active_peer, ttl=300)))
    b = await _open(app, protocols(mint(sk_pem, sub=active_peer, ttl=300)))
    survivor = await _open(app, protocols(mint(sk_pem, sub=second_peer, ttl=300)))
    for comm in (a, b, survivor):
        assert (await comm.connect(timeout=5))[0] is True
        assert json.loads(await comm.receive_from(timeout=5))["type"] == "welcome"

    assert await manager.drop_peer(active_peer) == 2
    for comm, label in [(a, "first"), (b, "second")]:
        event = await comm.receive_output(timeout=5)
        assert event["type"] == "websocket.close", f"{label} socket survived revocation"

    # CONTROL: the other peer's socket is untouched and still answers.
    await survivor.send_json_to({"type": "ping"})
    assert json.loads(await survivor.receive_from(timeout=5)) == {"type": "pong"}

    for comm in (a, b, survivor):
        await comm.disconnect()


# ==========================================================================
# 6. DPoP-bound upgrade (TASK-26)
# ==========================================================================

@pytest.mark.asyncio
async def test_dpop_enforce_accepts_one_proof_and_refuses_its_replay(keys, active_peer):
    """DPoP enforcement checks the real dpop_jti_seen ledger.

    Three negatives (bearer under enforce, missing proof, replay) and, in the
    middle, the accepted first use - without which "refuses a replay" is
    indistinguishable from "refuses every proof".
    """
    sk_pem, pk_pem = keys
    dpop_seed = bytes(range(32))
    cnf = didkey.to_did_key(P.public_key_of(dpop_seed))
    app = hub(pk_pem, ConnectionManager(), dpop_enforce=True)

    # (a) bearer (cnf absent) under enforce is DENIED, not fallen through
    # (TASK-46).
    comm = await _open(app, protocols(mint(sk_pem, sub=active_peer, ttl=300)))
    accepted, code = await comm.connect(timeout=5)
    await comm.disconnect()
    assert accepted is False and code == 4401, "a bearer upgrade slipped through enforce"

    # (b) cnf present but no dpop. entry.
    bound = mint(sk_pem, sub=active_peer, ttl=300, cnf=cnf)
    comm = await _open(app, protocols(bound))
    accepted, code = await comm.connect(timeout=5)
    await comm.disconnect()
    assert accepted is False and code == 4401, "a cnf-bound token connected with no proof"

    # (c) THE POSITIVE: a proof bound to WS_UPGRADE + this path + this token.
    wire = P.dpop_wire(dpop_seed, "WS_UPGRADE", WS_PATH, bound)
    comm = await _open(app, protocols(bound, f"dpop.{wire}"))
    accepted, code = await comm.connect(timeout=5)
    assert accepted is True, f"a VALID DPoP upgrade was refused (code {code})"
    assert json.loads(await comm.receive_from(timeout=5))["type"] == "welcome"
    await comm.disconnect()

    # (d) the SAME proof again: its jti is spent, so this is a replay.
    comm = await _open(app, protocols(bound, f"dpop.{wire}"))
    accepted, code = await comm.connect(timeout=5)
    await comm.disconnect()
    assert accepted is False and code == 4401, "a replayed DPoP proof was accepted"

    # (e) CONTROL for (d): a FRESH proof for the same token still connects, so
    # the replay refusal is the jti ledger and not the hub having gone sour.
    fresh = P.dpop_wire(dpop_seed, "WS_UPGRADE", WS_PATH, bound)
    comm = await _open(app, protocols(bound, f"dpop.{fresh}"))
    accepted, code = await comm.connect(timeout=5)
    assert accepted is True, f"a fresh proof was refused after a replay (code {code})"
    await comm.disconnect()


@pytest.mark.asyncio
async def test_a_proof_bound_to_another_request_is_refused(keys, active_peer):
    """The binding is the point: method, target and token."""
    sk_pem, pk_pem = keys
    dpop_seed = bytes([9] * 32)
    cnf = didkey.to_did_key(P.public_key_of(dpop_seed))
    token = mint(sk_pem, sub=active_peer, ttl=300, cnf=cnf)
    entries = protocols(token)

    for wire, label in [
        (P.dpop_wire(dpop_seed, "POST", WS_PATH, token), "wrong method"),
        (P.dpop_wire(dpop_seed, "WS_UPGRADE", "/v1/other", token), "wrong target"),
        (P.dpop_wire(dpop_seed, "WS_UPGRADE", WS_PATH, "some.other.jwt"), "wrong token"),
    ]:
        ok, why, jti = auth.verify_ws_dpop_proof(entries + [f"dpop.{wire}"], cnf, token, WS_PATH)
        assert ok is False, f"a proof bound to a {label} verified: it is replayable"
        assert jti is None

    # CONTROL: the correctly bound proof verifies and yields a jti to spend.
    good = P.dpop_wire(dpop_seed, "WS_UPGRADE", WS_PATH, token)
    ok, why, jti = auth.verify_ws_dpop_proof(entries + [f"dpop.{good}"], cnf, token, WS_PATH)
    assert ok is True, why
    assert isinstance(jti, bytes) and len(jti) == 16


def test_did_key_round_trips_and_rejects_the_wrong_multicodec():
    """A did:key round trip preserves an Ed25519 public key."""
    pub = P.public_key_of(bytes([3] * 32))
    did = didkey.to_did_key(pub)
    assert did.startswith("did:key:z")
    assert didkey.from_did_key(did) == pub

    for bad, label in [("z" + did, "no did:key prefix"),
                       ("did:key:z0OIl", "non-base58 characters"),
                       ("did:key:z" + didkey.b58_encode(b"\x12\x01" + pub), "wrong multicodec")]:
        with pytest.raises(didkey.DidKeyError):
            didkey.from_did_key(bad)


def test_did_key_has_one_implementation_and_the_gateway_reasons_kept_their_text():
    """The gateway and the HTTP side parse a did:key with the SAME code: two copies of a
    parser of a security-relevant input are how one starts accepting what the other
    refuses. The shared code keeps the gateway's error class and text, the only refusal
    text that reaches anything (consumer.py logs the reason); the HTTP callers catch
    ValueError, which DidKeyError still is."""
    from apps.authapi import didkey as shared

    for name in ("DidKeyError", "ED25519_MULTICODEC", "b58_encode", "b58_decode",
                 "to_did_key", "from_did_key"):
        assert getattr(didkey, name) is getattr(shared, name), name
    assert issubclass(shared.DidKeyError, ValueError)

    pub = P.public_key_of(bytes([3] * 32))
    did = shared.to_did_key(pub)
    for bad, text in [
        ("z" + did, "not a did:key (must start with 'did:key:z')"),
        ("did:key:z0OIl", "invalid base58btc encoding"),
        ("did:key:z" + shared.b58_encode(b"\x12\x01" + pub),
         "not an Ed25519 did:key (wrong multicodec prefix or length)"),
        ("did:key:z" + shared.b58_encode(shared.ED25519_MULTICODEC + pub[:31]),
         "not an Ed25519 did:key (wrong multicodec prefix or length)"),
    ]:
        ok, reason, jti = auth.verify_ws_dpop_proof(["dpop.x"], bad, "t", WS_PATH)
        assert (ok, jti) == (False, None)
        assert reason == f"cnf claim is not a resolvable did:key: {text}"
        with pytest.raises(ValueError):
            shared.from_did_key(bad)
    with pytest.raises(ValueError):
        shared.to_did_key(pub[:31])
    # Leading zero bytes encode as leading '1's both ways (bs58's rule).
    assert shared.b58_encode(b"\x00\x00\x01") == "112"
    assert shared.b58_decode("112") == b"\x00\x00\x01"


# ==========================================================================
# 7. the Origin gate
# ==========================================================================

def test_origin_gate_allows_absent_and_same_origin_and_refuses_a_stranger():
    """An ABSENT Origin passing is deliberate: mesh nodes, curl and CLI daemons never send one."""
    allow = ["https://dash.example"]

    # Absent -> allowed. Alone this is also what "allow everything" looks like,
    # hence the refusal below.
    assert auth.origin_allowed({}, allow) is True
    # Same-origin by Host match, with an EMPTY allow-list (the prod posture).
    assert auth.origin_allowed({b"origin": b"https://api.example",
                                b"host": b"api.example"}, []) is True
    # Allow-listed cross-origin.
    assert auth.origin_allowed({b"origin": b"https://dash.example",
                                b"host": b"api.example"}, allow) is True
    # THE NEGATIVE: present, not same-origin, not allow-listed.
    assert auth.origin_allowed({b"origin": b"https://evil.example",
                                b"host": b"api.example"}, allow) is False
    # A port mismatch is not a same-origin match.
    assert auth.origin_allowed({b"origin": b"https://api.example:8443",
                                b"host": b"api.example"}, []) is False
    # A malformed Origin never matches.
    assert auth.origin_allowed({b"origin": b"api.example", b"host": b"api.example"}, []) is False


@pytest.mark.asyncio
async def test_a_disallowed_origin_is_refused_before_the_credential_is_read(keys, active_peer):
    """Ordering matters: a cross-origin forger must not be
    able to tell 'bad Origin' from 'bad Origin AND inactive peer'."""
    sk_pem, pk_pem = keys
    app = hub(pk_pem, ConnectionManager(), dashboard_cors_origins=["https://dash.example"])
    token = mint(sk_pem, sub=active_peer, ttl=300)

    comm = await _open(app, protocols(token),
                       headers=[(b"origin", b"https://evil.example"), (b"host", b"api.example")])
    accepted, code = await comm.connect(timeout=5)
    await comm.disconnect()
    assert accepted is False, "a disallowed Origin was upgraded"
    assert code == 4403, f"Origin refusal must map to the 403-equivalent, got {code}"

    # CONTROL: the identical request from an allow-listed Origin is accepted, so
    # the refusal is the Origin gate and not the token.
    comm = await _open(app, protocols(token),
                       headers=[(b"origin", b"https://dash.example"), (b"host", b"api.example")])
    accepted, _ = await comm.connect(timeout=5)
    assert accepted is True, "control failed: an allow-listed Origin was refused"
    await comm.disconnect()


# ==========================================================================
# 8. 0RTT agent-card negotiation
# ==========================================================================

@pytest.mark.asyncio
async def test_a_stale_used_protocol_hash_is_refused_and_a_current_one_is_not(
    keys, owner_conn, active_peer
):
    """The used protocol hash is hex SHA-256 over the JCS card."""
    card = {"name": "ws-test", "protocolVersion": "0.3.0", "skills": []}
    with owner_conn.cursor() as cur:
        cur.execute("INSERT INTO agent_cards (peer_id, card) VALUES (%s, %s) "
                    "ON CONFLICT (peer_id) DO UPDATE SET card = EXCLUDED.card",
                    (active_peer, json.dumps(card)))
    owner_conn.commit()

    sk_pem, pk_pem = keys
    app = hub(pk_pem, ConnectionManager())
    token = mint(sk_pem, sub=active_peer, ttl=300)

    stale = "0" * 64
    comm = await _open(app, protocols(token, f"usedProtocolHash.{stale}"))
    accepted, code = await comm.connect(timeout=5)
    await comm.disconnect()
    assert accepted is False, "a stale usedProtocolHash was allowed to 0RTT"
    assert code == 4426, f"a 0RTT miss maps to the 426-equivalent, got {code}"

    # CONTROL: the CURRENT hash proceeds (0RTT, no re-fetch).
    current = auth.protocol_hash(card)
    assert len(current) == 64 and current == current.lower()
    comm = await _open(app, protocols(token, f"usedProtocolHash.{current}"))
    accepted, _ = await comm.connect(timeout=5)
    assert accepted is True, "control failed: the current card hash was refused"
    await comm.disconnect()

    # CONTROL 2: omitting the hash entirely is a normal upgrade (opt-in).
    comm = await _open(app, protocols(token))
    accepted, _ = await comm.connect(timeout=5)
    assert accepted is True
    await comm.disconnect()


# ==========================================================================
# 9. multi-connection presence semantics
# ==========================================================================

@pytest.mark.asyncio
async def test_presence_goes_offline_only_when_the_last_socket_closes(keys, active_peer):
    """A peer with several live sockets must not be marked
    offline every time any one of them closes."""
    sk_pem, pk_pem = keys
    manager = ConnectionManager()
    presence = RecordingPresenceBackend()
    app = hub(pk_pem, manager, presence)
    token = mint(sk_pem, sub=active_peer, ttl=300)

    a = await _open(app, protocols(token))
    b = await _open(app, protocols(token))
    for comm in (a, b):
        assert (await comm.connect(timeout=5))[0] is True
        assert json.loads(await comm.receive_from(timeout=5))["type"] == "welcome"

    await a.disconnect()
    await asyncio.sleep(0)
    assert [e for e in presence.events if e[0] == "offline"] == [], (
        "presence flipped offline while another socket was still live"
    )
    assert await manager.is_online(active_peer) is True

    # CONTROL: closing the LAST one does flip it, so the empty list above is the
    # last-connection rule and not a backend that never records offline.
    await b.disconnect()
    await asyncio.sleep(0)
    assert [e for e in presence.events if e[0] == "offline"], "the last close did not flip offline"
    assert await manager.is_online(active_peer) is False
