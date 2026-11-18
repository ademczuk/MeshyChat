"""Live delivery without Redis, proved on real sockets against a real database. Divergence 020.

THE GAP THIS FILE IS PROVED AGAINST, measured on the pre-change code (slice/routing,
c91013a): `/v1/ws` accepted a socket and then never sent it anything but `welcome`,
`pong` and acks. `ConnectionManager.publish_to_peer` and `publish_presence` had no
caller anywhere under apps/ (code graph trace and grep agree), presence was a
`RecordingPresenceBackend` that appended to a list, and the `peer_online` /
`peer_offline` builders in apps/wsgw/envelope.py were never called. sql/036's trigger was
already NOTIFYing `mesh_msg` on every committed message, and nothing LISTENed.

HOUSE RULES (docs/V1-DESIGN-RULES.md). Every negative sits beside its positive control in
the SAME test, differing in the one variable under test (C1).

HOW A NEGATIVE IS OBSERVED, because "nothing arrived within N seconds" is a timing guess
that a slow box turns green. The listener handles notifications ONE AT A TIME in commit
order and pushes every recipient of one before it reads the next. So a peer that must NOT
get message 1 is sent message 2 afterwards, and its FIRST frame must be message 2: had
message 1 reached it, message 1 would be first. Frames are matched by `key`, a structured
field, never by substring (C4).

THROUGH THE PRODUCTION WIRING: the socket tests connect to `meshychat.asgi.application`,
so the consumer, the registry (`MANAGER`), the presence backend and the bridge (`BRIDGE`)
are the ones a deployed process uses, not test doubles handed to a consumer. A route test
that supplies its own wiring proves the view, never the wiring (see meshychat/urls.py).

Run:  .venv/Scripts/python.exe -m pytest tests/test_mesh_live.py -q -p no:cacheprovider
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import io
import json
import os
import re
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
from apps.crypto import primitives as P  # noqa: E402
from apps.memoryskills import handlers  # noqa: E402
from apps.memoryskills.validation import MESH_CHAT_NAMESPACE as NS  # noqa: E402
from apps.wsgw import mesh_notify, presence  # noqa: E402
from apps.wsgw.connection_manager import MANAGER, ConnectionManager  # noqa: E402
from apps.wsgw.constants import MESH_NOTIFY_CHANNEL, NEGOTIATED_PROTOCOL, WS_PATH  # noqa: E402
from apps.wsgw.consumer import MeshyWebSocketConsumer  # noqa: E402
from meshychat.asgi import application  # noqa: E402
from tests.fixtures import signed_rows as sr  # noqa: E402

ISSUER = "meshy-live-test"

_server_key = ed25519.Ed25519PrivateKey.generate()
SERVER_PRIVATE_PEM = _server_key.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption())
SERVER_PUBLIC_PEM = _server_key.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)

#: sql/036's created_at rendering: second precision, Z-suffixed UTC.
CREATED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

#: The frame has the tag plus exactly four fields, plus the recipient's `role`
#: (divergence 033). No body, no value; `cc` only when the row has one AND memory.read would
#: show this reader the row (review F3 of 44640f2).
FRAME_FIELDS = {"type", "key", "from_peer_id", "to", "created_at", "role", "requires_ack"}


# ---------------------------------------------------------------------------- fixtures

@pytest.fixture(autouse=True)
def server_config():
    """One server key: it mints the peer JWTs presented here, and the WS gateway and the
    HTTP routes verify with it, read from settings exactly as production reads them."""
    with override_settings(
        ALLOWED_HOSTS=["*"],
        MESHY_JWT_PUBLIC_KEY_PEM=SERVER_PUBLIC_PEM.decode("ascii"),
        MESHY_JWT_ISSUER=ISSUER,
        # MESHY_ENVELOPE_NONCE_ENFORCE is deliberately NOT pinned: the nonce gate runs
        # at its shipped default, ON (divergence 036).
        MESHY_SYNC_TIMEOUT_SECONDS=0.0,
    ), authconfig.override_config(jwt_signing_key_pem=SERVER_PRIVATE_PEM,
                                  jwt_public_key_pem=SERVER_PUBLIC_PEM,
                                  jwt_issuer=ISSUER):
        yield


@pytest.fixture()
def owner():
    """The bootstrap role, AUTOCOMMIT: seeding and independent read-back only. It bypasses
    RLS, so "the row is there" is a fact about the table, not about a context."""
    with psycopg.connect(DSN, autocommit=True) as conn:
        yield conn


@pytest.fixture()
def bridge():
    """The PRODUCTION bridge. Stopped after the test because pytest-asyncio gives every
    test its own event loop, and a bridge is bound to the loop that started it."""
    yield mesh_notify.BRIDGE
    mesh_notify.BRIDGE.stop()


@pytest.fixture(autouse=True)
def registry_left_clean():
    """A socket whose `receive_from` times out has its application CANCELLED by the test
    communicator, so its consumer never runs `disconnect()` and its registration stays
    in the process-wide registry. One failing test would then leave peers "connected"
    for every test after it and turn their exact counts red for a reason that is not
    theirs. A real server always delivers the disconnect; this is the harness's gap."""
    yield
    MANAGER._by_peer.clear()


@pytest.fixture()
def roster():
    """Restore the process-wide roster cache after a test primes it."""
    saved = dict(handlers._ROSTER_LABELS)
    yield
    handlers._ROSTER_LABELS = saved


# ----------------------------------------------------------------------------- seeding

def _uniq(tag: str) -> str:
    return f"{tag}-{uuid.uuid4().hex[:8]}"


def _owner_row(conn, tag: str, group: uuid.UUID) -> uuid.UUID:
    owner_id = uuid.uuid4()
    conn.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id) "
                 "VALUES (%s,%s,'dev',%s)",
                 (owner_id, f"live-{tag}-{owner_id.hex[:8]}@example.test", group))
    return owner_id


def _peer(conn, tag: str, *, group: uuid.UUID | None = None, key: bool = False) -> dict:
    """A dev-plane peer with a lower-case `peers.name` that is a valid mesh label."""
    group = group or uuid.uuid4()
    owner_id = _owner_row(conn, tag, group)
    peer_id, name = uuid.uuid4(), _uniq(tag)
    conn.execute("INSERT INTO peers (id, owner_id, name, display_name) VALUES (%s,%s,%s,%s)",
                 (peer_id, owner_id, name, tag))
    out = {"peer_id": peer_id, "owner_id": owner_id, "name": name,
           "fallback": f"peer-{str(peer_id)[:8]}"}
    if key:
        seed = os.urandom(32)
        public = P.public_key_of(seed)
        kid = P.kid_for_peer_key(public)
        conn.execute("INSERT INTO peer_keys (peer_id, kid, public_key, active) "
                     "VALUES (%s,%s,%s,TRUE)", (peer_id, kid, P.b64u(public)))
        out.update(seed=seed, kid=kid)
    return out


def _mesh_chat_owner(conn) -> tuple[uuid.UUID, uuid.UUID]:
    """(owner peer, owner owner) of shared/mesh-chat, claiming it if nobody has. The
    per-run database is shared by every module in the run, so it may already be held."""
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


def _grant(conn, peer: dict, permission: str) -> None:
    ns_peer, ns_owner = _mesh_chat_owner(conn)
    conn.execute("INSERT INTO shared_memory_grant (namespace, grantee_peer_id, "
                 "  grantee_owner_id, permission, granted_by_peer_id, granted_by_owner_id) "
                 "VALUES (%s,%s,%s,%s,%s,%s)",
                 (NS, peer["peer_id"], peer["owner_id"], permission, ns_peer, ns_owner))


def _msg_key(sender: str | None = None) -> str:
    """A mesh key; with `sender`, the `msg/<ms>-<sender>-<tail>` shape a memory.write must
    use, since divergence 028 binds the key's sender label to the signer."""
    head = f"msg/{int(time.time() * 1000)}"
    tail = uuid.uuid4().hex[:10]
    return f"{head}-{sender}-{tail}" if sender else f"{head}-{tail}"


def _insert_msg(conn, author: dict, value: dict, *, key: str | None = None) -> str:
    """One mesh row written straight to the table, as handle_write stores it, with the
    bytes a writer would have signed (divergence 051: A2 refuses a row without them). sql/036
    fires on this exactly as on a memory.write: its WHEN clause reads only namespace and key.
    Committed or not is the CALLER's transaction's business."""
    ns_peer, _ = _mesh_chat_owner(conn)
    rid, key = uuid.uuid4(), key or _msg_key()
    sr.insert(conn, sr.row(author["peer_id"], author["owner_id"], NS, key, value, rid=rid,
                           call_id=f"live-{rid}", author_label=author["name"]))
    return key


def _v1(to: list[str], *, cc: list[str] | None = None, body: str = "hello",
        sender: str = "live-test") -> dict:
    value = {"schema_version": 1, "to": to, "from": sender, "type": "note",
             "thread_id": "live-thread", "ts": "2026-09-23T10:00:00Z", "body": body,
             "author_family": "claude"}
    if cc is not None:
        value["cc"] = cc
    return value


# ------------------------------------------------------------------------------ sockets

def _token(peer: dict) -> str:
    now = int(time.time())
    return pyjwt.encode({"sub": str(peer["peer_id"]), "aud": "peer", "iss": ISSUER,
                         "iat": now, "exp": now + 600, "scopes": []},
                        SERVER_PRIVATE_PEM, algorithm="EdDSA")


async def _connect(peer: dict) -> WebsocketCommunicator:
    """A socket on the REAL application, past the welcome frame."""
    comm = WebsocketCommunicator(application, WS_PATH,
                                 subprotocols=[NEGOTIATED_PROTOCOL, f"jwt.{_token(peer)}"])
    accepted, _ = await comm.connect(timeout=15)
    assert accepted is True, f"the hub refused {peer['name']}"
    first = json.loads(await comm.receive_from(timeout=15))
    assert first["type"] == "welcome", first
    return comm


async def _frame(comm, timeout: float = 10.0) -> dict:
    return json.loads(await comm.receive_from(timeout=timeout))


async def _mesh(comm, timeout: float = 10.0) -> dict:
    frame = await _frame(comm, timeout)
    assert frame["type"] == "mesh_message", f"expected a mesh_message frame, got {frame}"
    return frame


def _delta(before: dict, after: dict) -> dict:
    keys = set(before) | set(after)
    return {k: after.get(k, 0) - before.get(k, 0) for k in keys if after.get(k, 0) != before.get(k, 0)}


def _frames_delta(before: dict, after: dict) -> dict:
    """`_delta` without the preview gate's counts (divergence 033). The tests that use it
    are about who gets a FRAME; whether the frame previews the body depends on the memory
    authority this run's database holds, and section 10 counts it exactly."""
    return {k: v for k, v in _delta(before, after).items()
            if not k.startswith("mesh:preview_withheld:")}


# ============================================================ 1. one trigger, verbatim

TRIGGERS_NOTIFYING_MESH_MSG = """
    SELECT c.relname, t.tgname, p.proname
    FROM pg_trigger t
    JOIN pg_proc p ON p.oid = t.tgfoid
    JOIN pg_class c ON c.oid = t.tgrelid
    WHERE NOT t.tgisinternal AND p.prosrc ILIKE '%%mesh_msg%%'
    ORDER BY 1, 2
"""


def test_exactly_one_trigger_notifies_mesh_msg_and_it_is_the_sql_036_one():
    """No second trigger was added, and the one there is sql/036's, byte for byte.

    The detector scans EVERY non-internal trigger in the database, not only those on
    shared_memory, for a function whose source names the channel. Its positive control
    plants a second notifying trigger inside a transaction, sees the count become 2, and
    rolls it back: a detector that could not see a second trigger would pass vacuously.
    Stated limit: a trigger that reaches the channel through a helper function, or through
    dynamic SQL that assembles the name, is not seen by a source scan.
    """
    with psycopg.connect(DSN) as conn:
        found = conn.execute(TRIGGERS_NOTIFYING_MESH_MSG).fetchall()
        assert found == [("shared_memory", "shared_memory_mesh_notify", "mesh_chat_notify")], found

        # Verbatim: the live function body is the text between the dollar quotes in sql/036.
        sql = (ROOT / "sql" / "036_mesh_notify_trigger.sql").read_text(encoding="utf-8")
        body = re.search(r"AS \$\$(.*?)\$\$;", sql, re.S).group(1)
        live = conn.execute("SELECT prosrc FROM pg_proc WHERE proname = 'mesh_chat_notify'"
                            ).fetchone()[0]
        assert live == body, "mesh_chat_notify is not sql/036's function body"
        when = conn.execute("SELECT pg_get_triggerdef(oid) FROM pg_trigger "
                            "WHERE tgname = 'shared_memory_mesh_notify'").fetchone()[0]
        assert "AFTER INSERT" in when and "msg/%" in when and "shared/mesh-chat" in when, when

        # POSITIVE CONTROL: plant a second one, count 2, roll back.
        conn.execute("CREATE FUNCTION live_test_second_notify() RETURNS trigger "
                     "LANGUAGE plpgsql AS $f$ BEGIN PERFORM pg_notify('mesh_msg', NEW.key); "
                     "RETURN NEW; END; $f$")
        conn.execute("CREATE TRIGGER live_test_second_notify AFTER INSERT ON shared_memory "
                     "FOR EACH ROW EXECUTE FUNCTION live_test_second_notify()")
        planted = conn.execute(TRIGGERS_NOTIFYING_MESH_MSG).fetchall()
        conn.rollback()
        assert len(planted) == 2, f"the detector did not see a planted second trigger: {planted}"
        assert conn.execute(TRIGGERS_NOTIFYING_MESH_MSG).fetchall() == found


def test_one_committed_message_is_exactly_one_notification(owner):
    """The behavioural half of "one trigger": one insert, one NOTIFY. Two triggers, or a
    trigger plus an application-side NOTIFY, would deliver two here.

    Control: a row outside `msg/` in the same namespace produces NONE, so the count of
    one is the trigger's WHEN clause deciding, not every insert notifying."""
    author = _peer(owner, "one-author")
    with psycopg.connect(DSN, autocommit=True) as listener:
        listener.execute(f"LISTEN {MESH_NOTIFY_CHANNEL}")
        key = _insert_msg(owner, author, _v1(["x"]))
        not_a_message = f"cursor/{uuid.uuid4().hex}"
        _insert_msg(owner, author, {"cursor": 1}, key=not_a_message)
        seen = [json.loads(n.payload) for n in listener.notifies(timeout=2.0)]
    mine = [n for n in seen if n.get("key") == key]
    assert len(mine) == 1, f"one committed message produced {len(mine)} notifications"
    assert set(mine[0]) == {"key", "from_peer_id", "to", "created_at"}, mine[0]
    assert not [n for n in seen if n.get("key") == not_a_message], "a non-msg/ key notified"


# ======================================================== 2. the end-to-end delivery

@pytest.fixture()
def writer_world(owner):
    """A memory authority made by THE COMMAND, an author who may write shared/mesh-chat
    through it, and a reader. Same shape as tests/test_call_routing.py's `world`."""
    group = uuid.uuid4()
    memory_peer = uuid.uuid4()
    out = io.StringIO()
    with override_settings(MESHY_MEMORY_PEER_ID=str(memory_peer)):
        call_command("bootstrap_memory_peer", "--trust-group", str(group),
                     "--owner-email", f"live-mem-{memory_peer.hex[:8]}@example.test",
                     stdout=out, stderr=io.StringIO())
    assert "CREATED" in out.getvalue(), out.getvalue()
    author = _peer(owner, "author", group=group, key=True)
    reader = _peer(owner, "reader")
    _grant(owner, author, "write")
    _grant(owner, reader, "read")
    with override_settings(MESHY_MEMORY_PEER_ID=str(memory_peer)):
        yield {"memory_peer": memory_peer, "author": author, "reader": reader}


def _memory_write(world: dict, key: str, value: dict):
    """A signed memory.write to the memory authority, as mem_client sends it
    (mem_client.py:552-581 builds the envelope, :839-849 posts it)."""
    author, target = world["author"], world["memory_peer"]
    payload = {
        "v": 1, "kind": "skill.call", "call_id": str(uuid.uuid4()),
        "caller_peer_id": str(author["peer_id"]), "target_peer_id": str(target),
        "skill_name": "memory.write", "skill_version": "0.2.0",
        "args": {"namespace": NS, "key": key, "value": value},
        "args_attachments": [],
        "issued_at": _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "nonce": uuid.uuid4().hex,
    }
    envelope = {"payload": payload, "signature": P.b64u(P.sign(author["seed"], P.jcs(payload))),
                "signing_kid": author["kid"]}
    body = {"envelope": envelope, "mode_override": "async", "call_id": payload["call_id"]}
    return Client().post(f"/v1/agents/{target}/skills/memory.write/invoke",
                         data=json.dumps(body), content_type="application/json",
                         headers={"authorization": f"Bearer {_token(author)}"})


@pytest.mark.asyncio
async def test_a_connected_peer_receives_a_mesh_message_after_another_peers_memory_write_commits(
        owner, writer_world, bridge):
    """THE GAP, closed through the production path: a socket on the real application,
    the real registry and bridge, and a signed memory.write through the invoke route.

    The frame carries NO BODY for this reader. Its field set is exactly FRAME_FIELDS (type,
    key, from_peer_id, to, created_at, role, requires_ack), and a marker unique to this
    message's body is absent from the raw text the socket received. The positive control for
    that absence is the same marker read
    back from the committed row: the body exists, it simply is not on the wire. (This
    reader has no signing key, so memory.read would refuse it and the frame carries no
    preview either, divergence 033; section 10 is the reader that is shown one.)
    """
    reader, author = writer_world["reader"], writer_world["author"]
    comm = await _connect(reader)
    try:
        assert await bridge.wait_listening(10), "the bridge never LISTENed"
        marker = f"body-marker-{uuid.uuid4().hex}"
        # Through memory.write, so the sender is the author's own name (divergence 028).
        key = _msg_key(author["name"])
        response = await asyncio.to_thread(
            _memory_write, writer_world, key,
            _v1([reader["name"]], body=marker, sender=author["name"]))
        assert response.status_code == 202, response.content
        call = json.loads(response.content)
        assert call["status"] == "completed", call

        raw = await comm.receive_from(timeout=10)
        frame = json.loads(raw)
        assert set(frame) == FRAME_FIELDS, f"frame fields {sorted(frame)}"
        assert frame["type"] == "mesh_message"
        assert frame["key"] == key
        assert frame["from_peer_id"] == str(author["peer_id"])
        # A list, not sql/036's `value ->> 'to'` text of the array (divergence 033).
        assert frame["to"] == [reader["name"]], frame["to"]
        assert frame["role"] == "to", frame
        assert CREATED_AT_RE.fullmatch(frame["created_at"]), frame["created_at"]

        assert marker not in raw, "the message body reached the socket"
        stored = owner.execute("SELECT value->>'body', to_char(created_at AT TIME ZONE 'UTC', "
                               "'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') FROM shared_memory "
                               "WHERE namespace = %s AND key = %s", (NS, key)).fetchall()
        assert stored == [(marker, frame["created_at"])], stored
    finally:
        await comm.disconnect()


# ================================================================ 3. who gets a frame

@pytest.mark.asyncio
async def test_only_addressed_readable_peers_other_than_the_author_receive_the_frame(
        owner, bridge, roster):
    """The recipient filter, every branch beside its control, in one message sequence.

      M1 author -> to [bob by peers.name, frank by peer-<id8>, erin by name],
                   cc [carol by ROSTER label]
      M2 author -> to ["all"]                       (broadcast)
         then erin is granted read
      M3 author -> to [erin]
      M4 bob    -> to [author by name]

    First frames, and what each proves:
      bob, carol, frank  M1 then M2   to by name, cc by roster, to by fallback; broadcast
      dave               M2           not addressed: M1 skipped (a read grant is not
                                      addressing), and broadcast still reaches him
      erin               M3           addressed but NO read grant: M1 and M2 skipped;
                                      with the grant, the same peer receives
      author             M4           never nudged about its own M1..M3
    """
    author = _peer(owner, "author")
    bob, carol, frank = _peer(owner, "bob"), _peer(owner, "carol"), _peer(owner, "frank")
    dave, erin = _peer(owner, "dave"), _peer(owner, "erin")
    for p in (author, bob, carol, frank, dave):
        _grant(owner, p, "read")
    carol_roster = _uniq("carol-roster")
    handlers.prime_roster_labels(json.dumps(
        {"peers": {str(carol["peer_id"]): carol_roster}}).encode())
    assert carol_roster != carol["name"]

    socks = {}
    try:
        for name, p in (("author", author), ("bob", bob), ("carol", carol), ("frank", frank),
                        ("dave", dave), ("erin", erin)):
            socks[name] = await _connect(p)
        assert await bridge.wait_listening(10)
        before = bridge.stats()

        m1 = _insert_msg(owner, author, _v1([bob["name"], frank["fallback"], erin["name"]],
                                            cc=[carol_roster]))
        m2 = _insert_msg(owner, author, _v1(["all"]))
        for who in ("bob", "carol", "frank"):
            got = [(await _mesh(socks[who]))["key"] for _ in range(2)]
            assert got == [m1, m2], f"{who} received {got}"
        assert (await _mesh(socks["dave"]))["key"] == m2, "dave received M1, not addressed"

        _grant(owner, erin, "read")
        m3 = _insert_msg(owner, author, _v1([erin["name"]]))
        assert (await _mesh(socks["erin"]))["key"] == m3, "erin received M1 or M2 unreadable"

        m4 = _insert_msg(owner, bob, _v1([author["name"]]))
        assert (await _mesh(socks["author"]))["key"] == m4, "the author was nudged about M1..M3"

        # The listener RAN every branch, counted by reason (rule C6).
        assert _frames_delta(before, bridge.stats()) == {
            "mesh:notifications": 4,
            "mesh:frames": 3 + 4 + 1 + 1,
            "mesh:skipped:author": 4,
            "mesh:skipped:not_addressed": 1 + 0 + 4 + 4,
            "mesh:skipped:no_read:E_PERMISSION_DENIED": 1 + 1,
        }
    finally:
        for comm in socks.values():
            await comm.disconnect()


@pytest.mark.asyncio
async def test_a_peer_suspended_after_connecting_gets_no_frame(owner, bridge):
    """`peers.status` is re-read per message, so a suspension lands on the next message even
    though the socket was admitted while active. Controls: bob, addressed by
    the same message, receives it; and gus, reactivated, receives the next one first.

    Since STATUS B15 the bridge's standing sweep also CLOSES a suspended peer's socket
    (tests/test_ws_revocation.py). This test is about the per-message re-read in the window
    before a sweep, so the sweep is held off here; otherwise the close could land first."""
    author, bob, gus = _peer(owner, "author"), _peer(owner, "bob"), _peer(owner, "gus")
    for p in (bob, gus):
        _grant(owner, p, "read")
    socks = {}
    held_off = override_settings(MESHY_WS_REVOCATION_SWEEP_SECS=3600.0)
    held_off.enable()
    try:
        socks["bob"], socks["gus"] = await _connect(bob), await _connect(gus)
        assert await bridge.wait_listening(10)
        owner.execute("UPDATE peers SET status = 'suspended' WHERE id = %s", (gus["peer_id"],))
        m1 = _insert_msg(owner, author, _v1([bob["name"], gus["name"]]))
        assert (await _mesh(socks["bob"]))["key"] == m1, "control: bob did not receive M1"
        owner.execute("UPDATE peers SET status = 'active' WHERE id = %s", (gus["peer_id"],))
        m2 = _insert_msg(owner, author, _v1([gus["name"]]))
        assert (await _mesh(socks["gus"]))["key"] == m2, "a suspended peer received M1"
    finally:
        for comm in socks.values():
            await comm.disconnect()
        held_off.disable()


# ============================================================ 4. transactional delivery

@pytest.mark.asyncio
async def test_a_rolled_back_write_produces_no_frame(owner, bridge):
    """pg_notify is transactional (sql/036's header): a rolled-back insert never reaches
    the listener. The rolled-back row R is addressed to bob exactly as the committed row C
    is, and written FIRST, so bob's first frame being C is the proof R produced none.
    The listener's own count agrees: one notification, not two."""
    author, bob = _peer(owner, "author"), _peer(owner, "bob")
    _grant(owner, bob, "read")
    comm = await _connect(bob)
    try:
        assert await bridge.wait_listening(10)
        before = bridge.stats()
        with psycopg.connect(DSN) as tx:            # NOT autocommit: one transaction
            rolled_back = _insert_msg(tx, author, _v1([bob["name"]]))
            tx.rollback()
        committed = _insert_msg(owner, author, _v1([bob["name"]]))
        frame = await _mesh(comm)
        assert frame["key"] == committed, f"first frame was {frame['key']}, R={rolled_back}"
        assert _frames_delta(before, bridge.stats()) == {"mesh:notifications": 1,
                                                          "mesh:frames": 1}
        assert owner.execute("SELECT count(*) FROM shared_memory WHERE key = %s",
                             (rolled_back,)).fetchone()[0] == 0
    finally:
        await comm.disconnect()


# ====================================================== 5. the payload is a pointer

@pytest.mark.asyncio
async def test_a_forged_notification_cannot_put_text_on_the_wire(owner, bridge):
    """NOTIFY is unauthenticated: any session on the database can send one. A forged
    `mesh_msg` naming a real row re-nudges that row with the ROW's `to` and `created_at`,
    never the payload's. One naming a key that does not exist, and one naming a real key
    under an author who did not write it, produce no frame; their control is the real
    message written after them, which is bob's next frame."""
    author, bob = _peer(owner, "author"), _peer(owner, "bob")
    _grant(owner, bob, "read")
    comm = await _connect(bob)
    try:
        assert await bridge.wait_listening(10)
        real = _insert_msg(owner, author, _v1([bob["name"]]))
        genuine = await _mesh(comm)
        assert genuine["key"] == real

        forged = {"key": real, "from_peer_id": str(author["peer_id"]),
                  "to": "forged-recipient-text", "created_at": "1999-01-01T00:00:00Z"}
        owner.execute("SELECT pg_notify(%s, %s)", (MESH_NOTIFY_CHANNEL, json.dumps(forged)))
        renudge = await _mesh(comm)
        assert renudge == genuine, f"the frame took the forged payload's fields: {renudge}"

        before = bridge.stats()
        missing = dict(forged, key=_msg_key())
        owner.execute("SELECT pg_notify(%s, %s)", (MESH_NOTIFY_CHANNEL, json.dumps(missing)))
        # An author that is neither bob (whom the author rule would skip anyway, which
        # would make this case vacuous) nor the row's writer.
        misattributed = dict(forged, from_peer_id=str(uuid.uuid4()))
        owner.execute("SELECT pg_notify(%s, %s)",
                      (MESH_NOTIFY_CHANNEL, json.dumps(misattributed)))
        after = _insert_msg(owner, author, _v1([bob["name"]]))
        assert (await _mesh(comm))["key"] == after, "a notification for no such row made a frame"
        assert _frames_delta(before, bridge.stats()) == {
            "mesh:notifications": 3, "mesh:row_not_found": 2, "mesh:frames": 1}
    finally:
        await comm.disconnect()


# ======================================================================= 6. presence

async def _subscribe(comm, peer: dict) -> None:
    channel = f"presence:{peer['peer_id']}"
    await comm.send_to(text_data=json.dumps({"type": "subscribe", "channels": [channel]}))
    assert await _frame(comm) == {"type": "subscribed", "channels": [channel]}


async def _pong(comm) -> None:
    await comm.send_to(text_data=json.dumps({"type": "ping"}))
    assert await _frame(comm) == {"type": "pong"}


@pytest.fixture()
def short_presence_windows(monkeypatch):
    """The deployed backend with short windows (divergence 105), so these tests wait for the held
    frames in a fraction of a second rather than 3 s and 5 s. tests/test_presence_debounce.py pins
    the production values and the hold itself without a wall clock."""
    monkeypatch.setattr(presence.BACKEND, "arrival_secs", 0.2)
    monkeypatch.setattr(presence.BACKEND, "departure_grace_secs", 0.2)
    return presence.BACKEND


@pytest.mark.asyncio
async def test_online_and_offline_reach_presence_subscribers_and_only_the_last_close_is_offline(
        owner, short_presence_windows):
    """Every accepted socket of a peer already shown marks it online; only the peer's LAST closed
    socket marks it offline. The frames go to sockets subscribed to `presence:<peer>`. A `ping` is
    the ordering fence: the frame after it must be `pong`, so any presence frame that should not
    exist would have arrived first."""
    watcher, x = _peer(owner, "watcher"), _peer(owner, "x")
    w = await _connect(watcher)
    x1 = x2 = None
    try:
        await _subscribe(w, x)
        x1 = await _connect(x)
        assert await _frame(w) == {"type": "peer_online", "peer_id": str(x["peer_id"])}
        x2 = await _connect(x)
        assert await _frame(w) == {"type": "peer_online", "peer_id": str(x["peer_id"])}

        await x1.disconnect()
        x1 = None
        await _pong(w)          # not offline: x still holds a socket

        await x2.disconnect()
        x2 = None
        assert await _frame(w) == {"type": "peer_offline", "peer_id": str(x["peer_id"])}
        await _pong(w)          # and exactly one offline
    finally:
        for comm in (x1, x2, w):
            if comm is not None:
                await comm.disconnect()


@pytest.mark.asyncio
async def test_a_reconnect_inside_the_grace_reaches_no_subscriber(owner, short_presence_windows):
    """Divergence 105 on the real hub: x is shown online, its only socket closes and a new one
    opens inside the departure grace. The watcher gets neither an offline nor a second online.
    The grace is 30 s for that step, so a slow reconnect on a loaded box still lands inside it.
    The `pong` is the fence, and the held departure is shown cancelled, not merely late. Control:
    the same close with a short grace does reach the watcher as `peer_offline`."""
    watcher, x = _peer(owner, "watcher"), _peer(owner, "x")
    w = await _connect(watcher)
    x1 = x2 = None
    try:
        await _subscribe(w, x)
        x1 = await _connect(x)
        assert await _frame(w) == {"type": "peer_online", "peer_id": str(x["peer_id"])}

        short_presence_windows.departure_grace_secs = 30.0
        await x1.disconnect()
        x1 = None
        x2 = await _connect(x)
        assert short_presence_windows._pending(x["peer_id"]) is None, "the departure is still held"
        await _pong(w)          # no offline and no second online reached the watcher

        short_presence_windows.departure_grace_secs = 0.2
        await x2.disconnect()
        x2 = None
        assert await _frame(w) == {"type": "peer_offline", "peer_id": str(x["peer_id"])}
        await _pong(w)
    finally:
        for comm in (x1, x2, w):
            if comm is not None:
                await comm.disconnect()


@pytest.mark.asyncio
async def test_a_late_subscriber_gets_the_shown_peers_online_after_its_ack(owner,
                                                                           short_presence_windows):
    """Divergence 106 on the real hub: x is shown online before the watcher subscribes. The
    subscribe is answered with the ack, then `peer_online` for x, and nothing more (the `pong`
    fence). Control: a peer with no socket (y) in the same frame gets no snapshot."""
    watcher, x, y = _peer(owner, "watcher"), _peer(owner, "x"), _peer(owner, "y")
    x1 = await _connect(x)
    w = None
    try:
        for _ in range(100):                   # x's arrival is held for 0.2 s
            if short_presence_windows.is_shown(x["peer_id"]):
                break
            await asyncio.sleep(0.05)
        assert short_presence_windows.is_shown(x["peer_id"]), "x was never shown"
        w = await _connect(watcher)
        channels = [f"presence:{x['peer_id']}", f"presence:{y['peer_id']}"]
        await w.send_to(text_data=json.dumps({"type": "subscribe", "channels": channels}))
        assert await _frame(w) == {"type": "subscribed", "channels": channels}
        assert await _frame(w) == {"type": "peer_online", "peer_id": str(x["peer_id"])}
        await _pong(w)          # no snapshot for y, and only one for x
    finally:
        for comm in (x1, w):
            if comm is not None:
                await comm.disconnect()


def _beat(peer: dict, body: dict):
    return Client().post("/v1/peers/me/presence", data=json.dumps(body),
                         content_type="application/json",
                         headers={"authorization": f"Bearer {_token(peer)}"})


@pytest.mark.asyncio
async def test_a_presence_beat_reaches_subscribers_of_that_peer_and_no_one_else(owner, bridge):
    """POST /v1/peers/me/presence sends `peer_presence` to `presence:<peer>` subscribers.
    A node's beat request writes the peer's row; the bridge loads that row to decide the frame.

    `u` watches y, not x. x beats, then y beats: u's first frame is y's, so x's never
    reached it; w's next frame after x's first beat is x's SECOND beat, so y's never
    reached w. The second beat also pins the omission of null `note`/`blocked_on`."""
    watcher_w, watcher_u = _peer(owner, "w"), _peer(owner, "u")
    x, y = _peer(owner, "x"), _peer(owner, "y")
    w, u = await _connect(watcher_w), await _connect(watcher_u)
    try:
        assert await bridge.wait_listening(10)
        await _subscribe(w, x)
        await _subscribe(u, y)
        note = _uniq("note")

        assert (await asyncio.to_thread(_beat, x, {"state": "idle", "note": note})
                ).status_code == 200
        assert await _frame(w) == {"type": "peer_presence", "peer_id": str(x["peer_id"]),
                                   "state": "idle", "note": note, "live": True}
        assert (await asyncio.to_thread(_beat, y, {"state": "asleep"})).status_code == 200
        assert await _frame(u) == {"type": "peer_presence", "peer_id": str(y["peer_id"]),
                                   "state": "asleep", "live": True}, "u received x's beat"
        assert (await asyncio.to_thread(_beat, x, {"state": "active"})).status_code == 200
        assert await _frame(w) == {"type": "peer_presence", "peer_id": str(x["peer_id"]),
                                   "state": "active", "live": True}, "w received y's beat"
        # The row is the durable fact; the frame reported it.
        assert owner.execute("SELECT state, note FROM mesh_presence WHERE peer_id = %s",
                             (x["peer_id"],)).fetchone() == ("active", None)
    finally:
        await w.disconnect()
        await u.disconnect()


# ================================================================ 7. addressing, pure

def _client_to_labels(to):
    """VERBATIM from the node client, tools/mesh_client/agentmeshy_mesh_client/reactor.py
    :34-63 at b578b5c3a (the same function as mesh_ws_monitor.py:165-185). Copied, not
    imported, so this test is a second instrument that does not move when the server's
    parse does (rule C5)."""
    if to is None:
        return None
    if isinstance(to, (list, tuple)):
        labels = {x.strip().lower() for x in to if isinstance(x, str) and x.strip()}
        return labels or None
    if not isinstance(to, str):
        return None
    s = to.strip()
    if not s:
        return None
    if s.startswith("["):
        try:
            arr = json.loads(s)
            labels = {x.strip().lower() for x in arr if isinstance(x, str) and x.strip()}
            return labels or None
        except Exception:
            inner = s.strip("[]")
            labels = {t.strip().strip("\"'").lower() for t in inner.split(",")}
            labels.discard("")
            return labels or None
    labels = {p.strip().lower() for p in s.split(",")}
    labels.discard("")
    return labels or None


def _client_addressed_to_me(to, me_label):
    """VERBATIM, reactor.py:66-74 at b578b5c3a."""
    labels = _client_to_labels(to)
    if labels is None:
        return True
    return bool({"both", "all", str(me_label).strip().lower()} & labels)


def _frame_to(value):
    """The `to` the push's frame hands the client, for a value held in Python: a list since
    divergence 033 (before it, `left(value ->> 'to', 256)`), None when the frame has none."""
    return _frame_of(value).get("to")


SPELLINGS = [
    # (value, the labels the server must derive; None = broadcast)
    ({"to": "bob"}, {"bob"}),
    ({"to": " Bob "}, {"bob"}),
    ({"to": "bob, Carol"}, {"bob", "carol"}),
    ({"to": '["bob", "carol"]'}, {"bob", "carol"}),
    ({"to": "[bob, 'carol']"}, {"bob", "carol"}),
    ({"to": ["Bob", " carol "]}, {"bob", "carol"}),
    ({"to": ["bob", 7, None]}, {"bob"}),
    ({"to": ["bob"], "cc": ["dave"]}, {"bob", "dave"}),
    ({"to": ["bob"], "cc": "dave, erin"}, {"bob", "dave", "erin"}),
    ({"to": "all"}, {"all"}),
    ({"to": ["both"]}, {"both"}),
    ({}, None),
    ({"to": None}, None),
    ({"to": ""}, None),
    ({"to": "  "}, None),
    ({"to": []}, None),
    ({"to": [1, 2]}, None),
    ({"to": 5}, None),
    ({"to": {"bob": 1}}, None),
    ({"cc": ["dave"]}, None),                  # no `to`: broadcast, as the client reads it
    ({"to": None, "cc": ["dave"]}, None),
    ("not an object", None),
    (["bob"], None),
]


@pytest.mark.parametrize("value,expected", SPELLINGS)
def test_recipient_labels_read_every_spelling(value, expected):
    got = mesh_notify.recipient_labels(value)
    assert got == (None if expected is None else frozenset(expected)), (value, got)


def test_the_push_is_never_narrower_than_the_clients_own_filter():
    """For every spelling and every candidate label: if the client's reactor would act on
    the frame the push sends (its `to` a list since divergence 033), the server must have
    sent it. The reverse is allowed (a cc'd peer gets a frame its reactor ignores; its
    `role` says `cc`). Its CONTROL is that the server is not simply sending everything: it
    refuses the unaddressed peer on every addressed spelling."""
    candidates = ["bob", "carol", "dave", "erin", "zed"]
    refused = 0
    for value, _ in SPELLINGS:
        addressing = mesh_notify.addressing_of(value)
        for me in candidates:
            server = mesh_notify.role_for(addressing, frozenset({me})) is not None
            client = _client_addressed_to_me(_frame_to(value), me)
            assert server or not client, f"client acts on {value!r} as {me!r}; server skipped"
            refused += not server
    assert refused > 0, "the server addressed every peer on every spelling"
    bob = mesh_notify.addressing_of({"to": ["bob"]})
    assert mesh_notify.role_for(bob, frozenset({"zed"})) is None
    assert mesh_notify.role_for(bob, frozenset({"bob"})) == "to"


def test_peer_labels_are_the_roster_name_and_fallback(roster):
    peer = uuid.uuid4()
    assert mesh_notify.peer_labels(peer, "Kxxx") == frozenset({"kxxx", f"peer-{str(peer)[:8]}"})
    handlers.prime_roster_labels(json.dumps({"peers": {str(peer): "kxxx-cc"}}).encode())
    assert mesh_notify.peer_labels(peer, None) == frozenset({"kxxx-cc", f"peer-{str(peer)[:8]}"})


# ===================================================================== 8. the wiring

def test_production_wiring_shares_one_registry():
    """The bridge and the presence backend a deployed consumer uses publish into the SAME
    registry it registers sockets in. A consumer handed its own registry and no bridge
    gets NO bridge, rather than one feeding a registry its socket is not in."""
    assert mesh_notify.BRIDGE.manager is MANAGER
    assert isinstance(presence.BACKEND, presence.LocalPresenceBackend)
    assert presence.BACKEND.manager is MANAGER

    default = MeshyWebSocketConsumer()
    assert default.manager is MANAGER and default.bridge is mesh_notify.BRIDGE
    assert default.presence is presence.BACKEND

    own = ConnectionManager()
    assert MeshyWebSocketConsumer(manager=own).bridge is None
    mine = mesh_notify.LiveBridge(own)
    assert MeshyWebSocketConsumer(manager=own, bridge=mine).bridge is mine


# ================================================ 9. the recipient's role, and list-shaped
#                                                     recipients (divergence 033)
#
# MEASURED BEFORE THE CHANGE (round 1, txxxx, live, 2026-09-23): a `listen --monitor` line
# carried the frame's `to` only, so a peer that was cc'd could not tell it had been; and on
# HQD `to` arrived as a JSON-ENCODED STRING ("[\"desktop-5e6e5b2b\"]"), which is sql/036's
# `left(value ->> 'to', 256)` applied to a jsonb ARRAY, not a list.

def _raw_mesh(raw: str) -> dict:
    frame = json.loads(raw)
    assert frame["type"] == "mesh_message", frame
    return frame


@pytest.mark.asyncio
async def test_each_recipient_is_told_its_role_and_to_and_cc_are_lists_on_the_wire(
        owner, bridge, memory_authority):
    """One role per RECIPIENT, decided by the rule that decided it gets the message.

      M1 author -> to [bob, pat], cc [carol, pat]   bob: to, carol: cc, pat: to (to wins)
      M2 author -> to ["all"], cc [carol]           dave, bob, pat: broadcast; carol: cc
                                                    (named beats broadcast)
      M3 author -> to '["<bob>"]', a JSON array HELD IN A STRING (the measured legacy shape)
                                                    bob: to, and `to` is the list [bob]

    dave's FIRST frame is M2, so M1 (which does not name him) never reached him: the role
    is per recipient, not the message's. Every frame is parsed from the raw text the socket
    received, so `to` and `cc` being arrays is a fact about the wire.

    `cc` is shown only to a reader memory.read would show the row to (review F3 of
    44640f2): nothing else lets a reader see who ELSE was cc'd. bob, carol, pat and dave
    are such readers. `outsider` (another trust group, a key, read, no skill grant) gets M2,
    the broadcast, with its role and `to` and WITHOUT `cc`, and carol's label is absent
    from its raw frame; dave, on the same message, is the control."""
    group = memory_authority["group"]
    author = _peer(owner, "author")
    bob, carol, pat, dave = (_peer(owner, t, group=group, key=True)
                             for t in ("bob", "carol", "pat", "dave"))
    outsider = _peer(owner, "outsider", key=True)
    for p in (bob, carol, pat, dave, outsider):
        _grant(owner, p, "read")
    socks = {}
    try:
        for name, p in (("bob", bob), ("carol", carol), ("pat", pat), ("dave", dave),
                        ("outsider", outsider)):
            socks[name] = await _connect(p)
        assert await bridge.wait_listening(10)

        m1 = _insert_msg(owner, author, _v1([bob["name"], pat["name"]],
                                            cc=[carol["name"], pat["name"]]))
        m2 = _insert_msg(owner, author, _v1(["all"], cc=[carol["name"]]))
        m3 = _insert_msg(owner, author, dict(_v1(["x"]), to=json.dumps([bob["name"]])))

        expect = {"bob": 3, "carol": 2, "pat": 2, "dave": 1, "outsider": 1}
        raws = {name: [await socks[name].receive_from(timeout=10) for _ in range(n)]
                for name, n in expect.items()}
        got = {name: [_raw_mesh(r) for r in rs] for name, rs in raws.items()}
        assert [f["key"] for f in got["bob"]] == [m1, m2, m3]
        assert [f["key"] for f in got["carol"]] == [m1, m2]
        assert [f["key"] for f in got["pat"]] == [m1, m2]
        assert [f["key"] for f in got["dave"]] == [m2], "M1 reached a peer it does not name"

        roles = {name: [f.get("role") for f in frames] for name, frames in got.items()}
        assert roles == {"bob": ["to", "broadcast", "to"], "carol": ["cc", "cc"],
                         "pat": ["to", "broadcast"], "dave": ["broadcast"],
                         "outsider": ["broadcast"]}, roles

        for frame in (got["bob"][0], got["carol"][0], got["pat"][0]):
            assert frame["to"] == [bob["name"], pat["name"]], frame
            assert frame["cc"] == [carol["name"], pat["name"]], frame
        assert got["dave"][0]["to"] == ["all"] and got["dave"][0]["cc"] == [carol["name"]]
        legacy = got["bob"][2]
        assert legacy["to"] == [bob["name"]] and "cc" not in legacy, legacy
        out = got["outsider"][0]
        assert out["key"] == m2 and out["to"] == ["all"], out
        assert "cc" not in out and "cc_truncated" not in out, out
        assert carol["name"] not in raws["outsider"][0], "cc reached a reader without read"
    finally:
        for comm in socks.values():
            await comm.disconnect()


ROLES = [
    # (value, one of the peer's labels, the role it must be told; None = not addressed)
    ({"to": ["bob"]}, "bob", "to"),
    ({"to": ["x"], "cc": ["bob"]}, "bob", "cc"),
    ({"to": ["bob"], "cc": ["bob"]}, "bob", "to"),             # both: `to` wins
    ({"to": ["Bob"], "cc": "bob"}, "bob", "to"),                # both, other spellings
    ({"to": ["all"]}, "bob", "broadcast"),
    ({"to": ["both"]}, "bob", "broadcast"),
    ({"to": ["x"], "cc": ["all"]}, "bob", "broadcast"),
    ({"to": ["all"], "cc": ["bob"]}, "bob", "cc"),              # named beats broadcast
    ({"to": ["all", "bob"]}, "bob", "to"),
    ({}, "bob", "broadcast"),
    ({"cc": ["bob"]}, "bob", "cc"),                             # no `to`: everyone, and
    ({"cc": ["bob"]}, "zed", "broadcast"),                      # bob is named in cc
    ({"to": "Bob, carol"}, "bob", "to"),
    ({"to": '["x"]', "cc": "BOB"}, "bob", "cc"),
    ({"to": '["bob"]'}, "bob", "to"),
    ({"to": ["x"]}, "bob", None),
    ({"to": ["x"], "cc": ["y"]}, "bob", None),
    ("not an object", "bob", "broadcast"),
]


@pytest.mark.parametrize("value,label,role", ROLES)
def test_the_role_is_to_then_cc_then_broadcast(value, label, role):
    got = mesh_notify.role_for(mesh_notify.addressing_of(value), frozenset({label}))
    assert got == role, (value, label, got)


def _pre_033_is_addressed(recipients, labels):
    """VERBATIM, mesh_notify.is_addressed at 77274c2 (the rule before the role existed).
    Copied, so this test does not move when the server's code does (rule C5)."""
    if recipients is None:
        return True
    if recipients & frozenset({"all", "both"}):
        return True
    return bool(recipients & labels)


def test_the_role_changes_who_is_told_why_never_who_receives():
    """A role is given exactly when the rule before 033 delivered: for every spelling and
    every candidate label, `role_for` is not None iff the old predicate was true. So the
    role is information added to the frames that already went out, and never a new
    recipient set. Control: some pairs are refused, so the equivalence is not vacuous."""
    refused = 0
    for value in [v for v, _ in SPELLINGS] + [v for v, _, _ in ROLES]:
        a = mesh_notify.addressing_of(value)
        for me in ("bob", "carol", "dave", "erin", "zed", "x", "y"):
            role = mesh_notify.role_for(a, frozenset({me}))
            old = _pre_033_is_addressed(mesh_notify.recipient_labels(value), frozenset({me}))
            assert (role is not None) == old, (value, me, role, old)
            refused += role is None
    assert refused > 0


ABSENT = object()

FRAME_LISTS = [
    # (value, the frame's `to`, the frame's `cc`); ABSENT = the field is not on the frame
    ({"to": ["Bob", " carol "]}, ["Bob", "carol"], ABSENT),
    ({"to": "Bob, carol"}, ["Bob", "carol"], ABSENT),
    ({"to": '["desktop-5e6e5b2b"]'}, ["desktop-5e6e5b2b"], ABSENT),
    ({"to": "[bob, 'carol']"}, ["bob", "carol"], ABSENT),
    ({"to": ["bob", 7, None, "bob"]}, ["bob"], ABSENT),
    ({"to": ["bob"], "cc": "dave, erin"}, ["bob"], ["dave", "erin"]),
    ({"to": ["bob"], "cc": ["dave"]}, ["bob"], ["dave"]),
    ({"to": "all"}, ["all"], ABSENT),
    ({"to": ""}, [], ABSENT),
    ({"to": 5}, [], ABSENT),
    ({"to": None}, ABSENT, ABSENT),
    ({"cc": ["dave"]}, ABSENT, ["dave"]),
    ({}, ABSENT, ABSENT),
    ("not an object", ABSENT, ABSENT),
]


def _frame_of(value, role="to", *, readable=True):
    """The push's frame for a value held in Python: the Nudge the database read builds. By
    default for a reader memory.read would show the row to, so `cc` is on it (review F3)."""
    return mesh_notify.Nudge(key="msg/1-x", from_peer_id=uuid.UUID(int=1),
                             created_at="2026-09-24T00:00:00Z",
                             addressing=mesh_notify.addressing_of(value)).frame(
                                 role, readable=readable)


@pytest.mark.parametrize("value,to,cc", FRAME_LISTS)
def test_to_and_cc_on_a_frame_are_lists_of_labels_never_encoded_strings(value, to, cc):
    frame = _frame_of(value)
    for field, want in (("to", to), ("cc", cc)):
        if want is ABSENT:
            assert field not in frame, (value, frame)
        else:
            assert frame[field] == want and isinstance(frame[field], list), (value, frame)
    assert "to_truncated" not in frame and "cc_truncated" not in frame, frame
    # Review F3 of 44640f2: for a reader memory.read would NOT show the row to, no `cc`
    # (who else was copied is the row's, not the nudge's); `to` is a frame field and is shown
    # at the frame's own gate, as the plain wait shows it.
    plain = _frame_of(value, readable=False)
    assert "cc" not in plain and "cc_truncated" not in plain, (value, plain)
    assert plain.get("to", ABSENT) == frame.get("to", ABSENT), (value, plain)


def test_a_frames_label_lists_are_bounded_and_say_so_while_the_role_reads_every_label():
    """A legacy row can name any number of labels of any length (the v1 schema caps new
    writes at 32 labels of 64 characters; nothing caps what was written before it). The
    frame carries at most 32 labels, none longer than 256 characters (an over-long label
    is LEFT OFF, not cut: a cut label is one nobody wrote), and says when it left any off;
    the ROLE is decided on every label. Control: a list at the bound is not marked."""
    many = [f"n{i}" for i in range(40)]
    value = {"to": ["x" * 300] + many, "cc": ["y" * 300]}
    frame = _frame_of(value)
    assert frame["to"] == many[:32] and frame["to_truncated"] is True, frame
    assert frame["cc"] == [] and frame["cc_truncated"] is True, frame
    assert mesh_notify.role_for(mesh_notify.addressing_of(value), frozenset({"n39"})) == "to"
    at_bound = _frame_of({"to": many[:32], "cc": ["z" * 256]})
    assert at_bound["to"] == many[:32] and at_bound["cc"] == ["z" * 256]
    assert "to_truncated" not in at_bound and "cc_truncated" not in at_bound, at_bound


# ====================================================== 10. the body preview (divergence 033)
#
# A frame to a reader memory.read would show the body to carries its first 160 characters
# (whole code points) as `preview_unverified`, with `preview_truncated`; a frame to any
# other reader carries neither. The gate is divergence 027's (`search._anchor_body_withheld`),
# the one function that decides whether memory.read would show this reader a body.

#: A body of 161 code points whose 160th is astral: the cut must keep it WHOLE.
ASTRAL = "\U0001F600"
BODY_161 = "p" * 159 + ASTRAL + "q"


@pytest.fixture()
def memory_authority(owner):
    """A memory authority made by the bootstrap command's own function, with
    MESHY_MEMORY_PEER_ID pointed at it for the test (the bridge thread reads the same
    settings object)."""
    from tests.fixtures import meshsem_db as db
    mem = db.memory_authority(owner)
    with override_settings(MESHY_MEMORY_PEER_ID=str(mem["peer_id"])):
        yield mem


def _no_lone_surrogate(text: str) -> bool:
    return not any(0xD800 <= ord(c) <= 0xDFFF for c in text)


@pytest.mark.asyncio
async def test_a_frame_previews_the_body_only_to_a_reader_memory_read_would_show_it(
        owner, bridge, memory_authority):
    """M1 author -> to [inside, outsider, keyless, noread], a 161-character body whose
    160th character is astral.

      inside    the memory authority's trust group, an active key, read: the preview is
                the first 160 code points, the astral one WHOLE, and truncated is true
      outsider  another trust group, an active key, read, no skill grant: memory.read
                refuses it (not_authorized), so its frame carries NO preview
      keyless   the trust group, read, no key: memory.read refuses it
                (no_usable_signing_key), so no preview
      noread    no read grant: no frame at all (the push's own gate)

    Then noread is granted read and M2 goes to it alone: its FIRST frame is M2 (so M1 never
    reached it) and M2 carries the preview, the control that the same peer, once it may
    read, is shown one. The body text is absent from the raw frames that carry none."""
    group = memory_authority["group"]
    author = _peer(owner, "author")
    inside = _peer(owner, "inside", group=group, key=True)
    outsider = _peer(owner, "outsider", key=True)
    keyless = _peer(owner, "keyless", group=group)
    noread = _peer(owner, "noread", group=group, key=True)
    for p in (inside, outsider, keyless):
        _grant(owner, p, "read")
    socks = {}
    try:
        for name, p in (("inside", inside), ("outsider", outsider), ("keyless", keyless),
                        ("noread", noread)):
            socks[name] = await _connect(p)
        assert await bridge.wait_listening(10)
        before = bridge.stats()

        m1 = _insert_msg(owner, author, _v1([p["name"] for p in (inside, outsider, keyless,
                                                                  noread)], body=BODY_161,
                                            cc=["cc-observer"]))
        raw = {name: await socks[name].receive_from(timeout=10)
               for name in ("inside", "outsider", "keyless")}
        frames = {name: _raw_mesh(text) for name, text in raw.items()}
        assert {f["key"] for f in frames.values()} == {m1}

        shown = frames["inside"]
        assert shown.get("preview_unverified") == "p" * 159 + ASTRAL, shown
        assert shown["preview_truncated"] is True, shown
        assert len(shown["preview_unverified"]) == 160
        assert _no_lone_surrogate(shown["preview_unverified"])
        assert shown["cc"] == ["cc-observer"], shown
        for name in ("outsider", "keyless"):
            assert "preview_unverified" not in frames[name], (name, frames[name])
            assert "preview_truncated" not in frames[name], (name, frames[name])
            assert "p" * 40 not in raw[name], f"the body reached {name}"
            assert "cc" not in frames[name], (name, frames[name])
            assert "cc-observer" not in raw[name], f"cc reached {name} (review F3)"

        _grant(owner, noread, "read")
        m2 = _insert_msg(owner, author, _v1([noread["name"]], body="short"))
        first = _raw_mesh(await socks["noread"].receive_from(timeout=10))
        assert first["key"] == m2, "a peer without read received M1"
        assert first["preview_unverified"] == "short" and first["preview_truncated"] is False

        assert _delta(before, bridge.stats()) == {
            "mesh:notifications": 2,
            "mesh:frames": 3 + 1,
            "mesh:skipped:no_read:E_PERMISSION_DENIED": 1,
            "mesh:skipped:not_addressed": 3,
            "mesh:preview_withheld:not_authorized": 1,
            "mesh:preview_withheld:no_usable_signing_key": 1,
        }
    finally:
        for comm in socks.values():
            await comm.disconnect()


def test_the_push_previews_per_socket_key_and_withholds_from_a_reader_without_read(
        owner, memory_authority):
    """`push_frame`, the push's frame for ONE socket, with DPoP enforced: a socket's token
    is bound to a key (the upgrade proved it holds it), and memory.read would verify only
    under a USABLE one. So a socket bound to the peer's active key is shown the preview, one
    bound to a key revoked past its grace window is not (no_usable_signing_key), and a
    socket bound to no key is not (no_proof_of_possession). With DPoP off, as memory.read's
    POST then needs no proof, the bearer socket is shown it. A peer WITHOUT read is never
    shown it (namespace_permission): the gate re-checks the namespace grant beside the row."""
    from apps.core import rls
    from apps.crypto import primitives as P
    from tests.fixtures import meshsem_db as db

    group = memory_authority["group"]
    author = _peer(owner, "author")
    reader = _peer(owner, "reader", group=group, key=True)
    _grant(owner, reader, "read")
    past = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=1)
    old = db.add_key(owner, reader["peer_id"], active=False, revoked_at=past)
    stranger = _peer(owner, "stranger", group=group, key=True)
    key = _insert_msg(owner, author, _v1([reader["name"], stranger["name"]], body="hi there",
                                         cc=["cc-watcher"]))

    active = P.public_key_of(reader["seed"])
    revoked = P.public_key_of(old["seed"])
    with rls.server_internal(owner):
        nudge = mesh_notify.load_nudge(owner, key, author["peer_id"])
        with override_settings(MESHY_DPOP_ENFORCE=True):
            got = {name: mesh_notify.push_frame(owner, nudge, reader["peer_id"], "to", k)
                   for name, k in (("active", active), ("revoked", revoked), ("none", None))}
        off = mesh_notify.push_frame(owner, nudge, reader["peer_id"], "to", None)
        nope = mesh_notify.push_frame(owner, nudge, stranger["peer_id"], "to", None)
    assert got["active"][1] is None and got["active"][0]["preview_unverified"] == "hi there"
    assert got["revoked"][1] == "no_usable_signing_key", got["revoked"]
    assert got["none"][1] == "no_proof_of_possession", got["none"]
    for name in ("revoked", "none"):
        assert "preview_unverified" not in got[name][0], got[name]
    assert off[1] is None and off[0]["preview_unverified"] == "hi there", off
    assert nope[1] == "namespace_permission" and "preview_unverified" not in nope[0], nope
    # Review F3 of 44640f2: `cc` travels with the preview, per socket key.
    for name, frame in (("active", got["active"][0]), ("off", off[0])):
        assert frame["cc"] == ["cc-watcher"], (name, frame)
    for name, frame in (("revoked", got["revoked"][0]), ("none", got["none"][0]),
                        ("nope", nope[0])):
        assert "cc" not in frame, (name, frame)


@pytest.mark.parametrize("body,preview,truncated", [
    ("a" * 159, "a" * 159, False),
    ("a" * 160, "a" * 160, False),
    ("a" * 161, "a" * 160, True),
    ("a" * 159 + ASTRAL, "a" * 159 + ASTRAL, False),          # 160 code points, not cut
    ("a" * 159 + ASTRAL + "b", "a" * 159 + ASTRAL, True),     # cut after it, kept whole
    ("a" * 160 + ASTRAL, "a" * 160, True),                    # cut before it, left whole
    (ASTRAL * 161, ASTRAL * 160, True),
    ("", "", False),
])
def test_the_preview_is_160_whole_code_points(body, preview, truncated):
    """The bound, on the frame the push and the wait both build: 159, 160 and 161
    characters, and an astral character on each side of the cut. The count is in code
    points, so a surrogate pair is never split; the frame's JSON text then encodes each
    astral character whole."""
    nudge = mesh_notify.Nudge(key="msg/1-x", from_peer_id=uuid.UUID(int=1),
                              created_at="2026-09-24T00:00:00Z",
                              addressing=mesh_notify.addressing_of({"to": ["bob"]}),
                              body_head=body[:mesh_notify.PREVIEW_CHARS + 1])
    frame = nudge.frame("to", readable=True)
    assert frame["preview_unverified"] == preview, frame
    assert frame["preview_truncated"] is truncated, frame
    assert _no_lone_surrogate(frame["preview_unverified"])
    assert json.loads(json.dumps(frame))["preview_unverified"] == preview
    assert "preview_unverified" not in nudge.frame("to"), "the default is no preview"


@pytest.mark.asyncio
async def test_each_socket_is_sent_the_frame_for_its_own_key_and_never_another_keys():
    """`ConnectionManager.publish_to_peer(by_key=)`, the push's per-key fan-out (033). One
    peer holds three sockets, bound to key A, to key B and to no key; the previewed frame
    is keyed to A alone (B's gate refused, or B connected after the push looked). A's
    socket gets it; B's and the bearer socket get the frame WITHOUT a preview, never
    another key's. Control: with no map, and with an empty one, every socket gets the
    plain frame.
    Kills: a socket whose key is not in the map sent some other key's previewed frame
    (mutant M13 of the 033 mutation run, which every socket test let live: each of their
    readers held one socket, so a refused reader's map was empty)."""
    from apps.wsgw.connection_manager import ConnectionHandle
    from apps.wsgw.send_queue import SendQueue

    manager = ConnectionManager()
    peer = uuid.uuid4()
    key_a, key_b = b"A" * 32, b"B" * 32
    handles = {}
    for name, key in (("a", key_a), ("b", key_b), ("bearer", None)):
        handles[name] = ConnectionHandle(connection_id=uuid.uuid4(), peer_id=peer,
                                         queue=SendQueue(), bound_key=key)
        await manager.register(handles[name])
    plain = {"type": "mesh_message", "key": "msg/1-x", "role": "to"}
    shown = dict(plain, preview_unverified="hi", preview_truncated=False)

    assert await manager.publish_to_peer(peer, plain, by_key={key_a: shown}) == 3
    got = {name: h.queue.pop_nowait() for name, h in handles.items()}
    assert got == {"a": shown, "b": plain, "bearer": plain}, got
    for by_key in (None, {}):
        assert await manager.publish_to_peer(peer, plain, by_key=by_key) == 3
        got = {name: h.queue.pop_nowait() for name, h in handles.items()}
        assert got == {"a": plain, "b": plain, "bearer": plain}, (by_key, got)


# ================================================= 11. the review of 44640f2, as applied
#
# F1: the push's fail-closed path when 027's gate RAISES was untested. The code was right,
# and two mutants of it lived through every socket test: a gate error sending the
# previewed frame (fail-open), and the savepoint around the gate removed. These two tests
# pin it: the reader whose gate failed gets the frame without a preview (counted `error`),
# and a database error inside one recipient's gate costs the NEXT recipient nothing.

@pytest.mark.asyncio
async def test_a_preview_gate_that_raises_withholds_the_preview_never_the_frame(
        owner, bridge, memory_authority, monkeypatch):
    """027's gate raises while the push asks it about a reader it would otherwise pass: the
    frame still goes out, with its role, and carries NO preview; the body text is absent
    from the raw frame; the refusal is counted `mesh:preview_withheld:error`. Control: the
    same reader, the gate whole again, is shown the next message's preview."""
    from apps.meshsem import search

    author = _peer(owner, "author")
    reader = _peer(owner, "gatefault", group=memory_authority["group"], key=True)
    _grant(owner, reader, "read")
    real = search.body_withheld

    def broken(conn, who, key):
        raise RuntimeError("injected: the preview gate failed")

    comm = await _connect(reader)
    try:
        assert await bridge.wait_listening(10)
        monkeypatch.setattr(search, "body_withheld", broken)
        before = bridge.stats()
        m1 = _insert_msg(owner, author, _v1([reader["name"]], body="withheld by a fault"))
        raw = await comm.receive_from(timeout=10)
        frame = _raw_mesh(raw)
        assert frame["key"] == m1 and frame["role"] == "to", frame
        assert "preview_unverified" not in frame and "preview_truncated" not in frame, frame
        assert "withheld by a fault" not in raw, "a failed gate put the body on the wire"
        assert _delta(before, bridge.stats()) == {
            "mesh:notifications": 1, "mesh:frames": 1, "mesh:preview_withheld:error": 1}

        monkeypatch.setattr(search, "body_withheld", real)
        m2 = _insert_msg(owner, author, _v1([reader["name"]], body="shown once whole"))
        shown = _raw_mesh(await comm.receive_from(timeout=10))
        assert shown["key"] == m2, shown
        assert shown["preview_unverified"] == "shown once whole", "control"
    finally:
        await comm.disconnect()


@pytest.mark.asyncio
async def test_a_database_error_in_one_recipients_gate_costs_the_next_recipient_nothing(
        owner, bridge, memory_authority, monkeypatch):
    """Two readers memory.read admits, one message to both. The FIRST gate the push asks
    runs a statement that fails in the database (which aborts the transaction it runs in)
    and raises. Its reader gets the frame without a preview; the other reader, asked next
    in the same transaction, still gets its frame WITH the preview. That is the savepoint
    around the gate: without it the second gate, or the row read's own transaction, fails
    on the aborted transaction and the second reader loses its preview or its frame."""
    from apps.meshsem import search

    group = memory_authority["group"]
    author = _peer(owner, "author")
    readers = {tag: _peer(owner, tag, group=group, key=True) for tag in ("first", "second")}
    for p in readers.values():
        _grant(owner, p, "read")
    real = search.body_withheld
    faulted: list = []

    def fails_once(conn, who, key):
        if not faulted:
            faulted.append(who.peer_id)
            conn.execute("SELECT 1 / 0")
        return real(conn, who, key)

    socks = {}
    try:
        for tag, p in readers.items():
            socks[tag] = await _connect(p)
        assert await bridge.wait_listening(10)
        monkeypatch.setattr(search, "body_withheld", fails_once)
        before = bridge.stats()
        m = _insert_msg(owner, author, _v1([p["name"] for p in readers.values()],
                                           body="both may read this"))
        frames = {tag: _raw_mesh(await socks[tag].receive_from(timeout=10)) for tag in socks}
        assert {f["key"] for f in frames.values()} == {m}
        assert len(faulted) == 1, faulted
        victim = next(t for t, p in readers.items() if p["peer_id"] == faulted[0])
        other = next(t for t in readers if t != victim)
        assert "preview_unverified" not in frames[victim], frames[victim]
        assert frames[other]["preview_unverified"] == "both may read this", frames[other]
        assert _delta(before, bridge.stats()) == {
            "mesh:notifications": 1, "mesh:frames": 2, "mesh:preview_withheld:error": 1}
    finally:
        for comm in socks.values():
            await comm.disconnect()


def test_a_row_with_cc_and_no_body_still_asks_the_gate_for_its_cc(owner, memory_authority):
    """Review F3 of 44640f2. The preview's gate decides `cc` too, so a row with a `cc` and
    no string body (nothing to preview) must still ask it: a reader memory.read admits gets
    `cc`, one of another trust group without a skill grant (not_authorized) gets the frame,
    with `to` and its role, and no `cc`. Control: a row with neither asks no gate."""
    from apps.core import rls

    author = _peer(owner, "author")
    reader = _peer(owner, "reader", group=memory_authority["group"], key=True)
    outsider = _peer(owner, "outsider", key=True)
    for p in (reader, outsider):
        _grant(owner, p, "read")
    to = [reader["name"], outsider["name"]]
    with_cc = _insert_msg(owner, author, {"schema_version": 1, "to": to, "cc": ["cc-only"]})
    neither = _insert_msg(owner, author, {"schema_version": 1, "to": to})
    with rls.server_internal(owner):
        nudge = mesh_notify.load_nudge(owner, with_cc, author["peer_id"])
        bare = mesh_notify.load_nudge(owner, neither, author["peer_id"])
        shown = mesh_notify.push_frame(owner, nudge, reader["peer_id"], "to", None)
        refused = mesh_notify.push_frame(owner, nudge, outsider["peer_id"], "to", None)
        plain = mesh_notify.push_frame(owner, bare, outsider["peer_id"], "to", None)
    assert nudge.body_head is None
    assert shown[1] is None and shown[0]["cc"] == ["cc-only"], shown
    assert "preview_unverified" not in shown[0], shown
    assert refused[1] == "not_authorized", refused
    assert "cc" not in refused[0] and refused[0]["to"] == to, refused
    assert plain[1] is None and "cc" not in plain[0], plain


# F6: every socket above connects with a bearer token and DPoP off, so no socket test had a
# socket BOUND to a key, and two mutants of the per-key push (the previewed frame always
# role `to`; `socket_keys` dropping the bound keys) died only in the monitor's heavy
# end-to-end test. This one runs the production upgrade with DPoP enforced.

def _bound_token(peer: dict) -> str:
    """A peer JWT bound (`cnf`) to the peer's active key, as the minter issues one."""
    from apps.authapi.didkey import to_did_key
    now = int(time.time())
    return pyjwt.encode({"sub": str(peer["peer_id"]), "aud": "peer", "iss": ISSUER,
                         "iat": now, "exp": now + 600, "scopes": [],
                         "cnf": to_did_key(P.public_key_of(peer["seed"]))},
                        SERVER_PRIVATE_PEM, algorithm="EdDSA")


async def _connect_bound(peer: dict) -> WebsocketCommunicator:
    """A socket on the REAL application whose upgrade PROVES the key its token is bound to
    (a DPoP proof for WS_UPGRADE on the socket's path, as `listen` sends it)."""
    from tools.meshy_node import wire
    token = _bound_token(peer)
    proof = wire.dpop_proof(peer["seed"], wire.WS_UPGRADE, WS_PATH, token)
    comm = WebsocketCommunicator(application, WS_PATH, subprotocols=[
        NEGOTIATED_PROTOCOL, f"jwt.{token}", f"dpop.{proof}"])
    accepted, _ = await comm.connect(timeout=15)
    assert accepted is True, f"the hub refused {peer['name']}'s bound upgrade"
    first = json.loads(await comm.receive_from(timeout=15))
    assert first["type"] == "welcome", first
    return comm


@pytest.mark.asyncio
async def test_with_dpop_enforced_a_key_bound_socket_gets_the_frame_for_its_own_key(
        owner, bridge, memory_authority):
    """DPoP ENFORCED; every socket's token is bound to its peer's active key, proven at
    the upgrade. M1 author -> to [inside, outsider], cc [copied], a body.

      inside    memory.read admits it: role `to`, the preview, `cc`
      copied    memory.read admits it: role `cc` (its OWN role, on the previewed frame),
                the preview, `cc`
      outsider  another trust group, no skill grant: role `to`, no preview, no `cc`

    M2 is the same addressing with NO body: nothing to preview, but a `cc`, so the push
    still asks the gate per socket key (`Nudge.gated`): inside and copied get `cc`, the
    outsider does not.

    Kills, at the socket: the previewed frame built with another recipient's role, the
    bridge's snapshot losing the sockets' bound keys (each would reach inside and copied
    as a frame keyed to no socket, so without the preview), and the push's per-key frames
    built only for a row with a body (R9 of the review's mutation run: M2's `cc` would
    reach no socket)."""
    group = memory_authority["group"]
    author = _peer(owner, "author")
    inside = _peer(owner, "inside", group=group, key=True)
    copied = _peer(owner, "copied", group=group, key=True)
    outsider = _peer(owner, "outsider", key=True)
    for p in (inside, copied, outsider):
        _grant(owner, p, "read")
    socks = {}
    with override_settings(MESHY_DPOP_ENFORCE=True):
        try:
            for name, p in (("inside", inside), ("copied", copied), ("outsider", outsider)):
                socks[name] = await _connect_bound(p)
            assert await bridge.wait_listening(10)
            before = bridge.stats()
            m1 = _insert_msg(owner, author, _v1([inside["name"], outsider["name"]],
                                                cc=[copied["name"]], body="bound and shown"))
            raw = {name: await socks[name].receive_from(timeout=10) for name in socks}
            frames = {name: _raw_mesh(text) for name, text in raw.items()}
            assert {f["key"] for f in frames.values()} == {m1}
            assert {n: f["role"] for n, f in frames.items()} == {
                "inside": "to", "copied": "cc", "outsider": "to"}, frames
            for name in ("inside", "copied"):
                assert frames[name]["preview_unverified"] == "bound and shown", frames[name]
                assert frames[name]["cc"] == [copied["name"]], frames[name]
            out = frames["outsider"]
            assert "preview_unverified" not in out and "cc" not in out, out
            assert "bound and shown" not in raw["outsider"]

            m2 = _insert_msg(owner, author, {"schema_version": 1, "type": "note",
                                             "to": [inside["name"], outsider["name"]],
                                             "cc": [copied["name"]]})
            raw2 = {name: await socks[name].receive_from(timeout=10) for name in socks}
            second = {name: _raw_mesh(text) for name, text in raw2.items()}
            assert {f["key"] for f in second.values()} == {m2}
            for name in ("inside", "copied"):
                assert second[name]["cc"] == [copied["name"]], (name, second[name])
                assert "preview_unverified" not in second[name], second[name]
            assert "cc" not in second["outsider"], second["outsider"]
            assert copied["name"] not in raw2["outsider"], "cc reached the outsider"
            assert _delta(before, bridge.stats()) == {
                "mesh:notifications": 2, "mesh:frames": 6,
                "mesh:preview_withheld:not_authorized": 2}
        finally:
            for comm in socks.values():
                await comm.disconnect()
