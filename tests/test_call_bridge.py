"""The CallBridge skeleton and the presence wiring of divergence 041 S3 (design 4.6, 4.3).

Design `docs/design/skill-frames.md`, section 7 row S3: "the two triggers plus a CallBridge skeleton (LISTEN, pointer parsing, counters, recovery
and re-offer sweeps, no frames sent); T14's no-op half". The presence wiring S2 deferred lands
here too: a `hub_connections` row is written only after this worker's CallBridge reports
LISTENING, deleted in `disconnect`, and refreshed by a beat on the CallBridge thread that reads
the registry through the event loop.

WHAT IS PROVED:
  1. Parsing. A notification is parsed for its POINTER and nothing else, by the rule the HTTP
     path and the frames apply to a call_id; anything else is malformed. The statuses a
     pointer may carry are exactly the ones the skill_calls CHECK admits.
  2. T14's no-op half. Forged `meshy_call` and `meshy_call_chunk` notifications naming a real
     call routed to a connected target, and malformed ones, are each parsed and counted by
     reason, and NOTHING is written or sent: the call row and its events are unchanged, and
     the first frame each connected socket reads after them is the pong to its own ping.
  3. The skeleton's shape. Its own thread, LISTEN and query connections (not LiveBridge's);
     on every (re)LISTEN the recovery sweep runs BEFORE listening is reported; the re-offer
     sweep runs on its interval; both hooks select nothing and send nothing yet.
  4. Presence. A row appears only once the bridge is LISTENING, for a socket that starts the
     bridge and for one that joins it while it reconnects, and is gone after `disconnect`,
     whose delete follows the deregister; killing the LISTEN connection stops the beat, and so
     does an outage through several reconnect retries, and so does a wedged event loop, for a
     wedge shorter than the loop-call timeout and one longer; a beat that took its snapshot
     before a socket closed cannot bring the row back after the delete; a beat prunes rows
     nobody has seen for PRUNE_AFTER_SECS.
  5. Bounded against a stalled database (S3 review F1). A beat or an insert stuck on a peer
     row another transaction holds gives up within its statement bound, and the listener
     goes on; a disconnect never waits for a stuck beat past a short bound and holds no
     executor thread while it waits; the beat it could not wait for deletes its row after its
     own write; every connection the bridge writes or queries through is bounded.

HOW A NEGATIVE IS OBSERVED (rule C1). The bridge handles notifications one at a time in commit
order and counts each on the event loop once it is decided, so after the counts for a batch
arrive, every decision about the batch has been taken; the socket then sends a ping, and its
FIRST frame must be the pong: a frame the bridge had queued would be ahead of it.

Every socket test runs against `meshychat.asgi.application`, so the consumer, the registry
(`MANAGER`) and the bridge (`call_notify.CALL_BRIDGE`) are the production wiring.

Run:  .venv/Scripts/python.exe -m pytest tests/test_call_bridge.py -q -p no:cacheprovider
"""
from __future__ import annotations

import asyncio
import dataclasses
import datetime as _dt
import json
import os
import re
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
from django.test import override_settings  # noqa: E402

from apps.wsgw import call_notify, hub_presence, mesh_notify  # noqa: E402
from apps.wsgw.connection_manager import (MANAGER, ConnectionHandle,  # noqa: E402
                                          ConnectionManager)
from apps.wsgw.constants import NEGOTIATED_PROTOCOL, WS_PATH  # noqa: E402
from apps.wsgw.consumer import MeshyWebSocketConsumer  # noqa: E402
from apps.wsgw.send_queue import SendQueue  # noqa: E402
from meshychat.asgi import application  # noqa: E402
from tests.fixtures.call_rows import (ARGS_MARKER, call_row, seed_call,  # noqa: E402
                                      seed_peer, seed_skill)
from tests.fixtures.hub_presence_db import hub_connections_clean  # noqa: E402,F401  (autouse)

ISSUER = "meshy-call-bridge-test"
_server_key = ed25519.Ed25519PrivateKey.generate()
SERVER_PRIVATE_PEM = _server_key.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption())
SERVER_PUBLIC_PEM = _server_key.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)

CALL_CHANNEL = "meshy_call"
CHUNK_CHANNEL = "meshy_call_chunk"


# ------------------------------------------------------------------------- fixtures

@pytest.fixture(autouse=True)
def server_config():
    """One server key: it mints the tokens presented here and the gateway verifies with
    it, read from settings as production reads them."""
    with override_settings(ALLOWED_HOSTS=["*"],
                           MESHY_JWT_PUBLIC_KEY_PEM=SERVER_PUBLIC_PEM.decode("ascii"),
                           MESHY_JWT_ISSUER=ISSUER):
        yield


@pytest.fixture(autouse=True)
def bridges_stopped():
    """The production bridges are bound to the loop that started them, and pytest-asyncio
    gives each test its own loop; the registry is process-wide. Cleared after each test
    (conftest's own autouse fixture also stops the CallBridge and clears hub_connections)."""
    yield
    call_notify.CALL_BRIDGE.stop()
    mesh_notify.BRIDGE.stop()
    MANAGER._by_peer.clear()


@pytest.fixture()
def owner():
    """The bootstrap role, autocommit: seeds, forges notifications, measures. Bypasses RLS."""
    with psycopg.connect(DSN, autocommit=True) as conn:
        yield conn


@pytest.fixture()
def bridge_tuned(monkeypatch):
    """The production CallBridge with its timers set for a test. Attributes the listener
    reads on every pass, restored by monkeypatch."""
    def tune(**attrs):
        for name, value in attrs.items():
            monkeypatch.setattr(call_notify.CALL_BRIDGE, name, value)
        return call_notify.CALL_BRIDGE
    return tune


# -------------------------------------------------------------------------- helpers

def _token(peer_id: uuid.UUID) -> str:
    now = int(time.time())
    return pyjwt.encode({"sub": str(peer_id), "aud": "peer", "iss": ISSUER, "iat": now,
                         "exp": now + 600, "scopes": []},
                        SERVER_PRIVATE_PEM, algorithm="EdDSA")


async def _connect(peer_id: uuid.UUID) -> tuple[WebsocketCommunicator, uuid.UUID]:
    """A socket on the REAL application, past its welcome AND past the end of `connect()`:
    Channels dispatches a consumer's messages one at a time, so the pong to a ping sent now
    is only produced after `connect()` (and its presence write) returned."""
    comm = WebsocketCommunicator(application, WS_PATH,
                                 subprotocols=[NEGOTIATED_PROTOCOL, f"jwt.{_token(peer_id)}"])
    accepted, _ = await comm.connect(timeout=15)
    assert accepted is True
    welcome = json.loads(await comm.receive_from(timeout=15))
    assert welcome["type"] == "welcome", welcome
    await _pong_first(comm)
    return comm, uuid.UUID(welcome["connection_id"])


async def _pong_first(comm) -> None:
    await comm.send_to(text_data=json.dumps({"type": "ping"}))
    frame = json.loads(await comm.receive_from(timeout=15))
    assert frame == {"type": "pong"}, f"a frame reached the socket before its pong: {frame}"


def _rows(owner, *connection_ids) -> dict:
    rows = owner.execute("SELECT connection_id, peer_id, worker_token, seen_at "
                         "FROM hub_connections WHERE connection_id = ANY(%s)",
                         (list(connection_ids),)).fetchall()
    return {r[0]: r for r in rows}


def _seen_at(owner, connection_id):
    row = _rows(owner, connection_id).get(connection_id)
    return None if row is None else row[3].astimezone(_dt.timezone.utc)


def _db_now(owner):
    # In UTC: same-zone datetimes compare and subtract as wall-clock times, an hour off
    # across a clock change.
    return owner.execute("SELECT clock_timestamp()").fetchone()[0].astimezone(_dt.timezone.utc)


async def _until(predicate, what: str, timeout: float = 10.0, step: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(step)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


def _delta(before: dict, after: dict, *prefixes: str) -> dict:
    keys = {k for k in set(before) | set(after) if k.startswith(prefixes)}
    return {k: after.get(k, 0) - before.get(k, 0) for k in keys
            if after.get(k, 0) != before.get(k, 0)}


def _notify(owner, channel: str, payload: str) -> None:
    owner.execute("SELECT pg_notify(%s, %s)", (channel, payload))


def _listen_pids(owner) -> list[int]:
    """The backends of every CallBridge LISTEN connection on this run's database: the last
    statement such a connection runs is its second LISTEN."""
    return [r[0] for r in owner.execute(
        "SELECT pid FROM pg_stat_activity WHERE datname = current_database() "
        "AND query = %s AND pid <> pg_backend_pid()", (f"LISTEN {CHUNK_CHANNEL}",))]


def _kill_listen(owner) -> None:
    pids = _listen_pids(owner)
    assert len(pids) == 1, f"expected one CallBridge LISTEN connection, found {pids}"
    assert owner.execute("SELECT pg_terminate_backend(%s)", (pids[0],)).fetchone()[0]


class _Gate:
    """A hook that holds the listener thread at one point until released. `entered` is set
    when the thread arrives. Every wait is bounded, so a failing test cannot hang the run."""

    def __init__(self) -> None:
        self.armed = threading.Event()
        self.entered = threading.Event()
        self.release = threading.Event()

    def hold(self) -> None:
        if self.armed.is_set():
            self.entered.set()
            self.release.wait(15)

    async def wait_entered(self, timeout: float = 10.0) -> bool:
        return await asyncio.to_thread(self.entered.wait, timeout)


class _PeerRowLock:
    """An unrelated transaction holding a peer's row FOR UPDATE, as a key publish does
    (`apps/peers/keys.py:186`). A write to `hub_connections` that names this peer for the
    first time waits on it: the foreign key's check takes KEY SHARE on the same row. A timer
    thread releases it after `hold` seconds, so a test that is RED cannot hang on it."""

    def __init__(self, peer_id: uuid.UUID, hold: float) -> None:
        self._conn = psycopg.connect(DSN)
        self._conn.execute("SELECT 1 FROM peers WHERE id = %s FOR UPDATE", (peer_id,))
        self._mutex = threading.Lock()
        self._released = False
        self._timer = threading.Timer(hold, self.release)
        self._timer.daemon = True
        self._timer.start()

    def is_held(self) -> bool:
        with self._mutex:
            return not self._released

    def release(self) -> None:
        self._timer.cancel()
        with self._mutex:
            if self._released:
                return
            self._released = True
            try:
                self._conn.rollback()
            finally:
                self._conn.close()


def _waiting_on_a_lock(owner) -> int:
    """How many presence writes on this run's database wait on a lock right now."""
    return owner.execute(
        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
        "AND wait_event_type = 'Lock' AND query LIKE 'INSERT INTO hub_connections%%'"
    ).fetchone()[0]


def _default_executor_workers() -> int:
    """The default executor's size: `ThreadPoolExecutor()`'s own default on 3.13 and later."""
    count = getattr(os, "process_cpu_count", os.cpu_count)() or 1
    return min(32, count + 4)


# ======================================================== 1. parsing: the pointer only

#: A target whose canonical spelling has letters, so an upper-case spelling differs from it.
TARGET = uuid.UUID("abcdef01-2345-4678-9abc-def012345678")


def _call_payload(**over) -> str:
    base = {"call_id": "c-1", "status": "routed", "target": str(TARGET)}
    base.update(over)
    return json.dumps({k: v for k, v in base.items() if v is not _ABSENT})


_ABSENT = object()


def test_a_call_pointer_is_parsed_for_its_three_keys_and_nothing_else():
    """Extra keys a forger adds are never read: the pointer type has exactly three fields."""
    target = uuid.uuid4()
    raw = json.dumps({"call_id": "call-9", "status": "queued", "target": str(target),
                      "args": {"x": ARGS_MARKER}, "envelope": ARGS_MARKER})
    pointer = call_notify.parse_call_payload(raw)
    assert pointer == call_notify.CallPointer(call_id="call-9", status="queued", target=target)
    assert [f.name for f in dataclasses.fields(pointer)] == ["call_id", "status", "target"]
    assert ARGS_MARKER not in repr(pointer)
    assert isinstance(pointer.target, uuid.UUID)


MALFORMED_CALLS = {
    "not json": "not json",
    "empty": "",
    "an array": "[]",
    "a string": '"c-1"',
    "no call_id": _call_payload(call_id=_ABSENT),
    "no status": _call_payload(status=_ABSENT),
    "no target": _call_payload(target=_ABSENT),
    "call_id a number": _call_payload(call_id=5),
    "call_id null": _call_payload(call_id=None),
    "call_id empty": _call_payload(call_id=""),
    "call_id with a newline": _call_payload(call_id="a\nb"),
    "call_id with NUL": _call_payload(call_id="a\u0000b"),
    "call_id a lone surrogate": '{"call_id": "\\ud800", "status": "routed", '
                                f'"target": "{TARGET}"}}',
    "call_id 201 bytes": _call_payload(call_id="é" * 100 + "x"),
    "status unknown": _call_payload(status="lesson"),
    "status upper case": _call_payload(status="ROUTED"),
    "status a number": _call_payload(status=1),
    "target not a uuid": _call_payload(target="not-a-uuid"),
    "target a number": _call_payload(target=7),
    "target upper case": _call_payload(target=str(TARGET).upper()),
    "target in braces": _call_payload(target="{" + str(TARGET) + "}"),
    "target without hyphens": _call_payload(target=TARGET.hex),
}


@pytest.mark.parametrize("name", sorted(MALFORMED_CALLS))
def test_a_malformed_call_payload_is_refused(name):
    assert call_notify.parse_call_payload(MALFORMED_CALLS[name]) is None


def test_the_call_id_rule_is_the_http_paths_at_its_bound():
    """Control for the bound above: 200 bytes is a call id, 201 is not."""
    ok = call_notify.parse_call_payload(_call_payload(call_id="é" * 100))
    assert ok is not None and len(ok.call_id.encode()) == 200


def test_a_chunk_pointer_is_parsed_for_its_two_keys_and_nothing_else():
    raw = json.dumps({"call_id": "call-9", "sequence": 3, "content": ARGS_MARKER})
    pointer = call_notify.parse_chunk_payload(raw)
    assert pointer == call_notify.ChunkPointer(call_id="call-9", sequence=3)
    assert [f.name for f in dataclasses.fields(pointer)] == ["call_id", "sequence"]
    for seq in (0, -1, 2**31 - 1, -(2**31)):          # the INTEGER column's whole domain
        assert call_notify.parse_chunk_payload(
            json.dumps({"call_id": "c", "sequence": seq})).sequence == seq


MALFORMED_CHUNKS = {
    "not json": "{",
    "an array": "[1]",
    "no sequence": json.dumps({"call_id": "c"}),
    "no call_id": json.dumps({"sequence": 1}),
    "sequence true": json.dumps({"call_id": "c", "sequence": True}),
    "sequence a float": json.dumps({"call_id": "c", "sequence": 1.0}),
    "sequence a string": json.dumps({"call_id": "c", "sequence": "1"}),
    "sequence past i32": json.dumps({"call_id": "c", "sequence": 2**31}),
    "sequence below i32": json.dumps({"call_id": "c", "sequence": -(2**31) - 1}),
    "call_id with a control": json.dumps({"call_id": "c\u0007", "sequence": 1}),
    "call_id 201 bytes": json.dumps({"call_id": "x" * 201, "sequence": 1}),
}


@pytest.mark.parametrize("name", sorted(MALFORMED_CHUNKS))
def test_a_malformed_chunk_payload_is_refused(name):
    assert call_notify.parse_chunk_payload(MALFORMED_CHUNKS[name]) is None


def test_the_statuses_a_pointer_may_carry_are_the_ones_the_table_admits(owner):
    """Read off the live CHECK constraint, not a copy of it."""
    (definition,) = owner.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = 'skill_calls_status_check'").fetchone()
    admitted = set(re.findall(r"'([a-z_]+)'::text", definition))
    assert len(admitted) >= 8, definition
    assert call_notify.CALL_STATUSES == admitted
    for status in admitted:
        assert call_notify.parse_call_payload(_call_payload(status=status)) is not None


def test_the_bridges_status_sets_and_sequence_bound_are_the_one_definitions():
    """The bridge kept its own copies of the call-status set, the terminal set and the int4
    sequence bound; `_decide_call` read the local terminal copy while `_terminal_rows` in the
    same module read `apps/calls/terminal`'s. Each is now built from the one definition, so a
    status added there cannot be missed here."""
    from apps.calls import reads, terminal
    from apps.wsgw import envelope

    assert call_notify.TERMINAL_STATUSES is terminal.TERMINAL_STATUSES
    assert call_notify.CALL_STATUSES == frozenset(reads.CALL_STATUSES)
    assert isinstance(call_notify.CALL_STATUSES, frozenset)
    assert call_notify.SEQUENCE_MAX == envelope.SEQUENCE_MAX == 2 ** 31 - 1
    assert call_notify.SEQUENCE_MIN == -(2 ** 31)


# ================================================= 2. T14, forged pointers, on the real wiring

async def _frames_to_pong(comm) -> list[dict]:
    """Every frame queued ahead of the pong to a ping sent now."""
    await comm.send_to(text_data=json.dumps({"type": "ping"}))
    frames = []
    while (frame := json.loads(await comm.receive_from(timeout=15))) != {"type": "pong"}:
        frames.append(frame)
    return frames


@pytest.mark.asyncio
async def test_t14_a_forged_pointer_reaches_at_most_the_real_rows_real_target(owner):
    """Design T14, whole since S5 (S3 proved its no-op half). NOTIFY is unauthenticated, so
    the forgeries below name two real `routed` calls, one to T and one to U, both connected
    here, with every status class, a target hint that holds no socket, a hint that names the
    WRONG peer, and a `routed` status for a call that is `completed`; the malformed ones cover each way a payload can fail to name a row. Each
    is counted under the reason decided on the loop. Where the bridge acts (a `routed` hint
    held here), it re-reads the ROW and sends the row's invoke to the ROW's target: T gets
    its own call once and U its own once, a duplicate each (4.9: at-least-once), and nobody
    gets the other's. A `queued` pointer for a routed row moves nothing. Neither row nor its
    events change. Nothing is sent for a terminal, running or unheld pointer."""
    caller, target, other = (seed_peer(owner, t) for t in ("caller", "target", "other"))
    bystander = seed_peer(owner, "bystander")
    runs = call_notify.CALL_BRIDGE.stats().get("redelivery:runs", 0)
    t_sock, _ = await _connect(target["peer_id"])
    u_sock, _ = await _connect(other["peer_id"])
    # Seeded once both sockets' connect-time redelivery (041 S5) has run, so what follows
    # is the forgeries' doing and not a redelivery's.
    await _until(lambda: call_notify.CALL_BRIDGE.stats().get("redelivery:runs", 0) >= runs + 2,
                 "both sockets' redelivery")
    mine = seed_call(owner, caller, target, seed_skill(owner, target), status="routed")
    theirs = seed_call(owner, caller, other, seed_skill(owner, other), status="routed")
    done = seed_call(owner, caller, target, seed_skill(owner, target), status="completed")
    before_rows = {c: call_row(owner, c) for c in (mine, theirs, done)}
    try:
        assert call_notify.CALL_BRIDGE.is_listening(), "control: the bridge is LISTENING"
        before = call_notify.CALL_BRIDGE.stats()
        t, u = str(target["peer_id"]), str(other["peer_id"])
        nobody = str(bystander["peer_id"])
        good = [
            {"call_id": mine, "status": "routed", "target": t},
            {"call_id": theirs, "status": "routed", "target": t},      # the hint lies
            {"call_id": done, "status": "routed", "target": t},        # the status lies
            {"call_id": mine, "status": "queued", "target": t},
            {"call_id": mine, "status": "completed", "target": t},
            {"call_id": mine, "status": "routed", "target": nobody},
            {"call_id": mine, "status": "running", "target": t},
            {"call_id": mine, "status": "abandoned", "target": u},
        ]
        bad = ["not json", "", "[]",
               json.dumps({"call_id": 5, "status": "routed", "target": t}),
               json.dumps({"call_id": mine, "status": "routed"}),
               json.dumps({"call_id": "a\nb", "status": "routed", "target": t}),
               json.dumps({"call_id": "x" * 201, "status": "routed", "target": t}),
               json.dumps({"call_id": mine, "status": "lesson", "target": t}),
               json.dumps({"call_id": mine, "status": "routed", "target": "nope"})]
        chunks = [json.dumps({"call_id": mine, "sequence": 0}),
                  json.dumps({"call_id": mine, "sequence": True}), "{",
                  json.dumps({"call_id": mine, "sequence": 2**31})]
        for payload in good:
            _notify(owner, CALL_CHANNEL, json.dumps(payload))
        for payload in bad:
            _notify(owner, CALL_CHANNEL, payload)
        for payload in chunks:
            _notify(owner, CHUNK_CHANNEL, payload)

        def settled():
            d = _delta(before, call_notify.CALL_BRIDGE.stats(), "call:", "chunk:",
                       "invoke:", "safety_net:")
            return (d.get("call:notifications") == len(good) + len(bad)
                    and d.get("chunk:notifications") == len(chunks)
                    and d.get("invoke:sent") == 2 and d.get("invoke:not_offered") == 1
                    and d.get("safety_net:no_row") == 1) and d

        counted = await _until(settled, "every forged notification to be counted")
        assert counted == {
            "call:notifications": 17, "call:undecodable": 9,
            "call:routed:target_here": 3, "call:queued:target_here": 1,
            "call:routed:target_not_here": 1, "call:terminal:no_registration": 2,
            "call:status_ignored": 1,
            "chunk:notifications": 4, "chunk:undecodable": 3, "chunk:no_registration": 1,
            "invoke:sent": 2, "invoke:not_offered": 1, "safety_net:no_row": 1,
        }, counted
        to_t, to_u = await _frames_to_pong(t_sock), await _frames_to_pong(u_sock)
        assert [(f["type"], f["call_id"]) for f in to_t] == [("skill_invoke", mine)], to_t
        assert [(f["type"], f["call_id"]) for f in to_u] == [("skill_invoke", theirs)], to_u
        for frame, call_id in ((to_t[0], mine), (to_u[0], theirs)):
            args, signature, kid, row_target = _args(owner, call_id)
            assert frame["envelope"] == {"payload": args, "signature": signature,
                                         "signing_kid": kid}, frame
            assert frame["target_peer_id"] == str(row_target), frame
        assert {c: call_row(owner, c) for c in (mine, theirs, done)} == before_rows, \
            "a forged pointer changed a row"
        events = owner.execute("SELECT count(*) FROM skill_call_events WHERE call_id = ANY(%s)",
                               ([mine, theirs, done],)).fetchone()[0]
        assert events == 0, "a forged pointer wrote an event"
        errors = {k: v for k, v in call_notify.CALL_BRIDGE.stats().items() if "error" in k}
        assert errors == {k: v for k, v in before.items() if "error" in k}, errors
    finally:
        await t_sock.disconnect()
        await u_sock.disconnect()


def _args(owner, call_id: str) -> tuple:
    """What the invoke must be built from: the ROW's args, signature, kid and target."""
    return owner.execute("SELECT args, args_signature, args_signing_kid, target_peer_id "
                         "FROM skill_calls WHERE call_id = %s", (call_id,)).fetchone()


# ============================================================ 3. the skeleton's shape

def test_the_production_consumer_gets_the_call_bridge_and_an_own_registry_gets_none():
    assert call_notify.CALL_BRIDGE.manager is MANAGER
    assert not isinstance(call_notify.CALL_BRIDGE, mesh_notify.LiveBridge)
    assert call_notify.CALL_BRIDGE is not mesh_notify.BRIDGE
    default = MeshyWebSocketConsumer()
    assert default.call_bridge is call_notify.CALL_BRIDGE
    own = ConnectionManager()
    assert MeshyWebSocketConsumer(manager=own).call_bridge is None
    mine = call_notify.CallBridge(own)
    assert MeshyWebSocketConsumer(manager=own, call_bridge=mine).call_bridge is mine


def test_the_production_bridge_starts_with_the_designs_cadences():
    """Every test below shortens the timers on the running bridge, so none of them would
    notice a production default that is wrong. Design 4.3: a beat every 10 s, and a row
    online for three beats (30 s); a slower beat would read a connected peer offline between
    beats. Q17 (approved): the re-offer sweep every 10 s. The prune never reaches into the
    online window. A bridge built with no arguments starts with the same values."""
    bridge = call_notify.CALL_BRIDGE
    assert bridge.beat_secs == hub_presence.BEAT_INTERVAL_SECS == 10
    assert hub_presence.ONLINE_WINDOW_SECS == 3 * bridge.beat_secs
    assert bridge.reoffer_secs == 10.0
    assert bridge.prune_after_secs == hub_presence.PRUNE_AFTER_SECS
    assert bridge.prune_after_secs >= hub_presence.ONLINE_WINDOW_SECS
    fresh = call_notify.CallBridge(ConnectionManager())
    assert (fresh.beat_secs, fresh.reoffer_secs, fresh.prune_after_secs) == (
        bridge.beat_secs, bridge.reoffer_secs, bridge.prune_after_secs)


@pytest.mark.asyncio
async def test_the_call_bridge_runs_its_own_thread_and_listen_connection(owner):
    """Not added to LiveBridge (design 4.2): after the first socket, two bridge threads and
    two LISTEN connections, the CallBridge's on the call channels only."""
    peer = seed_peer(owner, "solo")
    sock, _ = await _connect(peer["peer_id"])
    try:
        names = {t.name for t in threading.enumerate()}
        assert {"meshy-call-bridge", "meshy-live-bridge"} <= names, names
        listens = dict(owner.execute(
            "SELECT pid, query FROM pg_stat_activity WHERE datname = current_database() "
            "AND query LIKE 'LISTEN %%'").fetchall())
        call_pids = _listen_pids(owner)
        assert len(call_pids) == 1, listens
        live = [p for p, q in listens.items() if q == "LISTEN meshy_peer_revoked"]
        assert live and live[0] != call_pids[0], listens
    finally:
        await sock.disconnect()


@pytest.mark.asyncio
async def test_every_listen_sweeps_before_it_reports_listening(owner, monkeypatch):
    """mesh_notify.py:703-707's order, for the call channels: whatever was committed while
    the LISTEN connection was down is recovered BEFORE the bridge says it is listening, so
    a presence row (written only after LISTENING) is only ever written by a worker that has
    swept. Proved at the first LISTEN and again after the LISTEN connection is killed."""
    bridge = call_notify.CallBridge(ConnectionManager(), beat_secs=3600, reoffer_secs=3600)
    seen: list[bool] = []
    original = bridge._recovery_sweep

    def recording(loop, query):
        seen.append(bridge.is_listening())
        return original(loop, query)

    monkeypatch.setattr(bridge, "_recovery_sweep", recording)
    try:
        assert await bridge.ensure_started() is True
        assert seen == [False], seen
        assert bridge.stats().get("sweep:recovery") == 1
        _kill_listen(owner)
        await _until(lambda: bridge.stats().get("listens", 0) >= 2 and bridge.is_listening(),
                     "the bridge to LISTEN again")
        assert seen == [False, False], seen
        assert bridge.stats().get("sweep:recovery") == 2
        assert bridge.stats().get("listen_errors", 0) >= 1, "control: the kill was seen"
    finally:
        bridge.stop()


@pytest.mark.asyncio
async def test_the_reoffer_sweep_runs_on_its_interval_and_sends_nothing(owner):
    """The re-offer sweep (Q17) runs every `reoffer_secs` for the sockets this worker holds.
    A `routed` call to a connected target, with its deadline ahead and no target event, is
    offered again only once it is older than the age floor (`reoffer_age_secs`, 10 s): a call
    routed a moment ago has an invoke in flight. So three quick sweeps send nothing here
    (041 S5; in S3 the hook sent nothing at all). tests/test_call_delivery.py's T18 proves the
    sweep sends once the floor is passed. The first LISTEN's recovery sweep offers the call
    once, as 4.8 says it must (the control: exactly that one frame)."""
    manager = ConnectionManager()
    caller, target = seed_peer(owner, "caller"), seed_peer(owner, "target")
    call_id = seed_call(owner, caller, target, seed_skill(owner, target), status="routed")
    queue = SendQueue()
    await manager.register(ConnectionHandle(connection_id=uuid.uuid4(),
                                            peer_id=target["peer_id"], queue=queue))
    bridge = call_notify.CallBridge(manager, beat_secs=3600, reoffer_secs=0.2)
    try:
        assert await bridge.ensure_started() is True
        await _until(lambda: bridge.stats().get("sweep:reoffer", 0) >= 3,
                     "three re-offer sweeps", timeout=5)
        assert queue.next_seq == 1, "the re-offer sweep offered a call inside its age floor"
        frame = queue.pop_nowait()
        assert (frame["type"], frame["call_id"]) == ("skill_invoke", call_id), frame
    finally:
        bridge.stop()
        owner.execute("DELETE FROM hub_connections")      # the beat wrote the fake socket


# ========================================================================= 4. presence

@pytest.mark.asyncio
async def test_a_presence_row_is_written_only_once_the_call_bridge_is_listening(
        owner, bridge_tuned, monkeypatch):
    """The bridge is held in its first recovery sweep, before it reports LISTENING (the
    order the previous test proves). A socket that connects meanwhile is accepted, its
    connect ends without the bridge, and NO row is written for it: a row from a worker that
    cannot deliver would route calls into a black hole. Released, the bridge reports
    LISTENING and its first beat writes the row; a socket that connects after that has its
    row as soon as its connect ends, written by the insert (the next beat is an hour away)."""
    bridge = bridge_tuned(ready_timeout=0.5, beat_secs=3600, reoffer_secs=3600)
    gate = _Gate()
    original = bridge._recovery_sweep

    def held(loop, query):
        gate.hold()
        return original(loop, query)

    monkeypatch.setattr(bridge, "_recovery_sweep", held)
    peer = seed_peer(owner, "early")
    gate.armed.set()
    socks = []
    try:
        early, early_id = await _connect(peer["peer_id"])
        socks.append(early)
        assert gate.entered.is_set(), "control: the bridge was started and is held"
        assert bridge.is_listening() is False
        assert _rows(owner, early_id) == {}, "a row was written before the bridge listened"

        gate.armed.clear()
        gate.release.set()
        assert await bridge.wait_listening(10)
        row = await _until(lambda: _rows(owner, early_id).get(early_id),
                           "the first beat to write the early socket's row")
        assert row[1] == peer["peer_id"] and row[2] == bridge.worker_token

        late, late_id = await _connect(peer["peer_id"])
        socks.append(late)
        late_row = _rows(owner, late_id).get(late_id)
        assert late_row is not None, "the insert after LISTENING did not write the row"
        assert late_row[2] == bridge.worker_token
    finally:
        gate.release.set()
        for s in socks:
            await s.disconnect()


@pytest.mark.asyncio
async def test_a_socket_that_joins_a_reconnecting_bridge_gets_no_row_until_it_listens(
        owner, bridge_tuned, monkeypatch):
    """The previous test proves "a row only after LISTENING" for the socket that STARTS the
    bridge. In production the bridge starts once per process, so the case that matters is a
    socket that connects while a RUNNING bridge is reconnecting: its thread is alive and it
    is not listening (S3 review F2). The LISTEN connection is killed and the reconnect held in
    its recovery sweep; a socket accepted meanwhile gets no row. Released, the bridge listens
    and its first beat writes the row (the control).

    The first socket starts the bridge and must get its row, so it waits as long as production
    does: with ready_timeout=0.5 a loaded gate box gave up on LISTEN after 0.5 s, accepted the
    socket without a row, and with beats an hour apart the control row never came (mg436c,
    "LISTEN not established within 0.5s"). Only the joiner, which is meant to give up, gets the
    short wait; the bridge reads ready_timeout on every connect."""
    bridge = bridge_tuned(ready_timeout=call_notify.READY_TIMEOUT_SECS, beat_secs=3600, reoffer_secs=3600)
    first_peer, late_peer = seed_peer(owner, "first"), seed_peer(owner, "joiner")
    first, first_id = await _connect(first_peer["peer_id"])
    socks = [first]
    monkeypatch.setattr(bridge, "ready_timeout", 0.5)
    gate = _Gate()
    original = bridge._recovery_sweep

    def held(loop, query):
        gate.hold()
        return original(loop, query)

    monkeypatch.setattr(bridge, "_recovery_sweep", held)
    try:
        assert _rows(owner, first_id), "control: the first socket's row"
        gate.armed.set()
        _kill_listen(owner)
        assert await gate.wait_entered(), "the bridge did not reconnect to the sweep"
        assert bridge.is_listening() is False
        assert bridge._thread is not None and bridge._thread.is_alive(), (
            "control: the bridge is running and reconnecting, not stopped")
        joiner, joiner_id = await _connect(late_peer["peer_id"])
        socks.append(joiner)
        assert _rows(owner, joiner_id) == {}, (
            "a socket that joined a reconnecting bridge got a row before the bridge listened")

        gate.armed.clear()
        gate.release.set()
        assert await bridge.wait_listening(10)
        row = await _until(lambda: _rows(owner, joiner_id).get(joiner_id),
                           "the first beat after the reconnect to write the joiner's row")
        assert row[1] == late_peer["peer_id"] and row[2] == bridge.worker_token
    finally:
        gate.release.set()
        for s in socks:
            await s.disconnect()


@pytest.mark.asyncio
async def test_the_row_goes_with_its_socket_and_the_peers_other_socket_keeps_its_own(owner):
    peer = seed_peer(owner, "two")
    one, one_id = await _connect(peer["peer_id"])
    two, two_id = await _connect(peer["peer_id"])
    try:
        assert set(_rows(owner, one_id, two_id)) == {one_id, two_id}
        await one.disconnect()
        assert set(_rows(owner, one_id, two_id)) == {two_id}, "delete took the wrong rows"
    finally:
        await two.disconnect()
    assert _rows(owner, one_id, two_id) == {}


@pytest.mark.asyncio
async def test_the_delete_follows_the_deregister_so_no_later_beat_writes_the_row_back(
        owner, bridge_tuned, monkeypatch):
    """`disconnect` deletes the row AFTER the socket leaves the registry (consumer.py's
    step order). Deleted first, a beat that snapshots the still-registered socket between the
    delete and the deregister writes the row back, and nothing deletes it again: a ghost that
    reads online for 30 s (S3 review F5). Two full beats are made to run right after the
    delete, inside the disconnect; after it, two more; the row stays gone. The control: the
    delete ran, once."""
    bridge = bridge_tuned(beat_secs=0.2, reoffer_secs=3600)
    peer = seed_peer(owner, "order")
    sock, cid = await _connect(peer["peer_id"])
    original = bridge.detach

    async def detach_then_two_beats(connection_id):
        deleted = await original(connection_id)
        start = bridge.stats().get("presence:beats", 0)
        await _until(lambda: bridge.stats().get("presence:beats", 0) >= start + 2,
                     "two beats after the delete")
        return deleted

    monkeypatch.setattr(bridge, "detach", detach_then_two_beats)
    await _until(lambda: _seen_at(owner, cid), "the row")
    deletes = bridge.stats().get("presence:deletes", 0)
    await sock.disconnect(timeout=15)
    start = bridge.stats().get("presence:beats", 0)
    await _until(lambda: bridge.stats().get("presence:beats", 0) >= start + 2,
                 "two beats after the disconnect")
    assert _rows(owner, cid) == {}, (
        "a beat wrote the row back: the delete ran while the socket was still registered")
    assert bridge.stats().get("presence:deletes", 0) == deletes + 1, "control: one delete"


@pytest.mark.asyncio
async def test_killing_the_listen_connection_stops_the_beat(owner, bridge_tuned, monkeypatch):
    """A beat every 0.2 s moves the row's seen_at (the control). The LISTEN connection is
    killed and the bridge is held in the recovery sweep of its reconnect, so it is not
    listening: seen_at does not move for a second, five beats' worth. A worker whose LISTEN
    connection is down cannot deliver, so its rows must age out. Released, the beat
    resumes (the second control)."""
    bridge = bridge_tuned(beat_secs=0.2, reoffer_secs=3600)
    peer = seed_peer(owner, "beat")
    sock, cid = await _connect(peer["peer_id"])
    gate = _Gate()
    original = bridge._recovery_sweep

    def held(loop, query):
        gate.hold()
        return original(loop, query)

    monkeypatch.setattr(bridge, "_recovery_sweep", held)
    try:
        first = await _until(lambda: _seen_at(owner, cid), "the row")
        await _until(lambda: (_seen_at(owner, cid) or first) > first, "a beat to move seen_at")
        gate.armed.set()
        _kill_listen(owner)
        assert await gate.wait_entered(), "the bridge did not reconnect to the sweep"
        assert bridge.is_listening() is False
        frozen = _seen_at(owner, cid)
        await asyncio.sleep(1.0)
        assert _seen_at(owner, cid) == frozen, "the beat ran with the LISTEN connection down"
        gate.release.set()
        await _until(lambda: _seen_at(owner, cid) > frozen, "the beat to resume")
    finally:
        gate.release.set()
        await sock.disconnect()



@pytest.mark.asyncio
async def test_a_listen_outage_through_several_retries_stops_the_beat(
        owner, bridge_tuned, monkeypatch):
    """The previous test holds the thread INSIDE the reconnect's recovery sweep, so a beat
    that ran in the reconnect BACKOFF, between failed attempts, is not seen (S3 review F4).
    Here the LISTEN connection cannot be made again for 1.5 s: every attempt fails, and the
    backoff (shortened to 0.1 s, doubling) retries several times. seen_at does not move. The
    controls: the retries were counted, and the beat resumes once LISTEN comes back."""
    bridge = bridge_tuned(beat_secs=0.2, reoffer_secs=3600)
    # Read by the thread when it starts, which is at the first socket below.
    monkeypatch.setattr(call_notify, "LISTEN_BACKOFF_INITIAL_SECS", 0.1)
    down = threading.Event()
    original = bridge._run_once

    def refused(*args):
        if down.is_set():
            raise psycopg.OperationalError("test: the LISTEN connection cannot be made")
        return original(*args)

    monkeypatch.setattr(bridge, "_run_once", refused)
    peer = seed_peer(owner, "outage")
    sock, cid = await _connect(peer["peer_id"])
    try:
        first = await _until(lambda: _seen_at(owner, cid), "the row")
        await _until(lambda: (_seen_at(owner, cid) or first) > first, "a beat to move seen_at")
        errors = bridge.stats().get("listen_errors", 0)
        down.set()
        _kill_listen(owner)
        await _until(lambda: not bridge.is_listening(), "the bridge to see the kill", timeout=5)
        frozen = _seen_at(owner, cid)
        await asyncio.sleep(1.5)
        moved = _seen_at(owner, cid)
        retries = bridge.stats().get("listen_errors", 0) - errors
        assert moved == frozen, (
            "a beat ran while the LISTEN connection was down, between reconnect attempts")
        assert retries >= 3, f"control: the outage lasted through several retries ({retries})"
        down.clear()
        await _until(lambda: _seen_at(owner, cid) > frozen, "the beat after LISTEN returns")
    finally:
        down.clear()
        await sock.disconnect()


@pytest.mark.asyncio
async def test_a_wedged_event_loop_stops_the_beat(owner, bridge_tuned):
    """The beat reads the registry THROUGH the loop (design 4.3), so a worker whose loop is
    wedged stops beating and its rows age out. With the loop wedged for 1.2 s (six beats) and
    nothing else asked of the listener thread, seen_at stays where it was when the wedge
    began, give or take one beat already past its snapshot. The beat resumes after it (the
    control; the first control is the beat moving seen_at before the wedge).

    NO NOTIFICATION IS SENT DURING THIS WEDGE. The listener thread would then sit in that
    notification's own loop hop, and a beat that read the registry OFF the loop would sit
    there with it, so the test could not tell the two apart. The first version of this test
    sent one, and the mutant "the beat reads the registry on its own thread" survived it
    (041 S3 mutation campaign, B2). The counts get a wedge of their own, below."""
    bridge = bridge_tuned(beat_secs=0.2, reoffer_secs=3600)
    peer = seed_peer(owner, "wedge")
    sock, cid = await _connect(peer["peer_id"])
    try:
        first = await _until(lambda: _seen_at(owner, cid), "the row")
        await _until(lambda: (_seen_at(owner, cid) or first) > first, "a beat to move seen_at")
        # --- the wedge: nothing below yields to the loop until time.sleep returns ---
        wedge_start = _db_now(owner)
        time.sleep(1.2)
        wedged_seen = _seen_at(owner, cid)
        # --- end of the wedge ---
        assert (wedged_seen - wedge_start).total_seconds() < 0.15, (
            f"a beat ran {(wedged_seen - wedge_start).total_seconds():.2f}s into a wedged "
            "loop: it did not read the registry through the loop")
        await _until(lambda: _seen_at(owner, cid) > wedged_seen, "the beat after the wedge")
    finally:
        await sock.disconnect()



@pytest.mark.asyncio
async def test_a_loop_wedged_past_the_loop_call_timeout_stops_the_beat(
        owner, bridge_tuned, monkeypatch):
    """The previous wedge (1.2 s) is shorter than the loop-call timeout (5 s), so it never
    reaches the path where the beat's registry read TIMES OUT (S3 review F3). A beat that
    wrote its last snapshot there would keep a worker whose loop is wedged for good "online"
    for ever. The timeout is shortened to 0.3 s and the loop wedged for 1.5 s: seen_at stays
    where it was, the timeouts were counted (the control), and the beat resumes after."""
    bridge = bridge_tuned(beat_secs=0.2, reoffer_secs=3600)
    monkeypatch.setattr(call_notify, "LOOP_CALL_TIMEOUT_SECS", 0.3)
    peer = seed_peer(owner, "long-wedge")
    sock, cid = await _connect(peer["peer_id"])
    try:
        first = await _until(lambda: _seen_at(owner, cid), "the row")
        await _until(lambda: (_seen_at(owner, cid) or first) > first, "a beat to move seen_at")
        errors = bridge.stats().get("presence:beat_errors", 0)
        # --- the wedge: five loop-call timeouts long, and on a loaded box as long as it takes the beat thread to
        # time out twice (mg412b, 26 Oct: a fixed 1.5 s once gave one timeout, not two). The loop stays wedged
        # throughout: time.sleep never yields, and the counter is written by the beat thread under its lock. ---
        wedge_start = _db_now(owner)
        time.sleep(1.5)
        deadline = time.monotonic() + 10.0
        while (bridge.stats().get("presence:beat_errors", 0) - errors < 2
               and time.monotonic() < deadline):
            time.sleep(0.05)
        wedged_seen = _seen_at(owner, cid)
        timed_out = bridge.stats().get("presence:beat_errors", 0) - errors
        # --- end of the wedge ---
        assert (wedged_seen - wedge_start).total_seconds() < 0.15, (
            f"a beat ran {(wedged_seen - wedge_start).total_seconds():.2f}s into a loop "
            "wedged past the loop-call timeout")
        assert timed_out >= 2, f"control: the beat's registry read timed out ({timed_out})"
        await _until(lambda: _seen_at(owner, cid) > wedged_seen, "the beat after the wedge")
    finally:
        await sock.disconnect()


@pytest.mark.asyncio
async def test_a_notification_during_a_wedged_loop_is_counted_only_once_the_loop_runs(
        owner, bridge_tuned):
    """Every reason is counted ON the loop (design 4.6; the counter race divergence 020 hit).
    No beat or sweep is due (both an hour apart), so the listener thread is free to read a
    malformed notification sent while the loop is wedged for 1.2 s: it is not counted during
    the wedge, and it is counted once the loop runs again (the control)."""
    bridge = bridge_tuned(beat_secs=3600, reoffer_secs=3600)
    peer = seed_peer(owner, "wedge-count")
    sock, _ = await _connect(peer["peer_id"])
    try:
        await _until(lambda: bridge.stats().get("presence:beats", 0) >= 1,
                     "the beat that follows LISTEN, so no hop of its own is pending")
        before = bridge.stats()
        # --- the wedge ---
        _notify(owner, CALL_CHANNEL, "wedged-garbage")
        time.sleep(1.2)
        wedged_counts = _delta(before, bridge.stats(), "call:")
        # --- end of the wedge ---
        assert wedged_counts == {}, f"counted off the loop while it was wedged: {wedged_counts}"
        counted = await _until(
            lambda: _delta(before, bridge.stats(), "call:").get("call:undecodable"),
            "the count after the wedge")
        assert counted == 1
    finally:
        await sock.disconnect()


@pytest.mark.asyncio
async def test_a_beat_that_snapshotted_a_socket_cannot_bring_its_row_back(
        owner, bridge_tuned, monkeypatch):
    """S2's owed ordering (review F3): the beat is a real UPSERT, so one whose registry
    snapshot still held a socket must not write after that socket's delete. The beat is
    held between its snapshot and its write while the socket disconnects; the delete waits
    for the beat, and when both are done the row is gone. A beat held this briefly (0.5 s) is
    waited out: the delete is the disconnect's own, not handed to the beat, which S3 review
    F1's hand-off keeps for a beat held past `detach_wait_secs` (1 s)."""
    bridge = bridge_tuned(beat_secs=0.2, reoffer_secs=3600)
    peer = seed_peer(owner, "ghost")
    sock, cid = await _connect(peer["peer_id"])
    gate = _Gate()
    original = hub_presence.upsert_beat

    def held(conn, rows, worker_token):
        rows = list(rows)
        if any(c == cid for c, _ in rows):
            gate.hold()
        return original(conn, rows, worker_token)

    monkeypatch.setattr(hub_presence, "upsert_beat", held)
    try:
        await _until(lambda: _seen_at(owner, cid), "the row")
        before = bridge.stats()
        gate.armed.set()
        assert await gate.wait_entered(), "no beat snapshotted the socket"
        closing = asyncio.create_task(sock.disconnect(timeout=15))
        await asyncio.sleep(0.5)
        assert _seen_at(owner, cid) is not None, "the delete did not wait for the held beat"
        gate.armed.clear()
        gate.release.set()
        await closing
        await asyncio.sleep(0.5)                      # two more beats, without the socket
        assert _rows(owner, cid) == {}, "a beat brought the row back after the delete"
        assert _delta(before, bridge.stats(), "presence:delete") == {"presence:deletes": 1}, (
            "a beat held for 0.5 s was not waited out: the delete was handed to it")
    finally:
        gate.release.set()


@pytest.mark.asyncio
async def test_a_beat_prunes_rows_nobody_has_seen_for_the_prune_window(owner, bridge_tuned):
    """A crashed worker's rows stop being refreshed; the next beat of ANY worker deletes the
    ones older than PRUNE_AFTER_SECS and keeps a row that is merely offline (S2 review F6)."""
    bridge = bridge_tuned(beat_secs=0.2, reoffer_secs=3600)
    dead = seed_peer(owner, "crashed")
    stale, offline = uuid.uuid4(), uuid.uuid4()
    for cid, age in ((stale, hub_presence.PRUNE_AFTER_SECS + 1),
                     (offline, hub_presence.ONLINE_WINDOW_SECS + 1)):
        owner.execute("INSERT INTO hub_connections (connection_id, peer_id, worker_token, "
                      "connected_at, seen_at) VALUES (%s,%s,%s, now() - make_interval("
                      "secs => %s), now() - make_interval(secs => %s))",
                      (cid, dead["peer_id"], uuid.uuid4(), age, age))
    peer = seed_peer(owner, "pruner")
    sock, cid = await _connect(peer["peer_id"])
    try:
        before = bridge.stats().get("presence:beats", 0)
        await _until(lambda: bridge.stats().get("presence:beats", 0) >= before + 2, "two beats")
        assert set(_rows(owner, stale, offline, cid)) == {offline, cid}
    finally:
        await sock.disconnect()


def test_the_worker_token_is_drawn_when_the_bridge_starts():
    """S2 review I1: drawn at start, not at import, so a server that forks after importing
    the module gives each child its own token."""
    bridge = call_notify.CallBridge(ConnectionManager(), beat_secs=3600, reoffer_secs=3600)
    assert bridge.worker_token is None

    async def start() -> uuid.UUID:
        assert await bridge.ensure_started() is True
        token = bridge.worker_token
        bridge.stop()
        return token

    first, second = asyncio.run(start()), asyncio.run(start())
    assert isinstance(first, uuid.UUID) and first.version == 4
    assert first != second
    assert first != hub_presence.WORKER_TOKEN


# ============================== 5. bounded against a stalled database (S3 review F1)
#
# A beat's UPSERT that names a peer for the first time checks the foreign key, which takes
# KEY SHARE on the peer's row, so it waits on any transaction holding that row FOR UPDATE (a
# key publish, apps/peers/keys.py:186). Before the review nothing bounded that wait. The beat
# held the presence lock and the bridge's thread through it: no notification was handled,
# and every disconnect in the process queued behind the lock, each on a thread of the
# default executor, until every `asyncio.to_thread` in the process queued behind them.

#: How long the first test below holds the peer row. The production bounds are shorter: a
#: beat's or an insert's statement gives up after 5 s, and a detach waits at most 1 s.
STALL_HOLD_SECS = 7.0


def test_the_bounds_the_stall_tests_rely_on():
    """The thresholds below are derived from these; the production bridge carries them."""
    assert call_notify.STATEMENT_TIMEOUT_MS == 5000
    assert call_notify.CONNECT_TIMEOUT_SECS == 5
    assert call_notify.DETACH_LOCK_WAIT_SECS == 1.0
    bridge = call_notify.CALL_BRIDGE
    assert bridge.statement_timeout_ms == call_notify.STATEMENT_TIMEOUT_MS
    assert bridge.detach_wait_secs == call_notify.DETACH_LOCK_WAIT_SECS
    # A stalled statement never outlasts a beat interval.
    assert call_notify.STATEMENT_TIMEOUT_MS < 1000 * hub_presence.BEAT_INTERVAL_SECS
    assert call_notify.CONNECT_TIMEOUT_SECS < hub_presence.BEAT_INTERVAL_SECS


@pytest.mark.asyncio
async def test_a_beat_and_an_insert_stuck_on_a_locked_peer_row_give_up_within_their_bound(
        owner, bridge_tuned):
    """Peer L's row is held FOR UPDATE for 7 s. A socket for L connects: its insert and every
    beat after its register wait on the row. Both give up within their 5 s bound: the socket's
    connect ends while the row is still held (its row is written by a beat later), a beat
    error is counted, and the listener, free again, counts a notification sent during the
    stall. Released, the next beat writes L's row (the control) and the errors stop."""
    bridge = bridge_tuned(beat_secs=0.2, reoffer_secs=3600)
    steady, locked = seed_peer(owner, "steady"), seed_peer(owner, "locked")
    sock, _ = await _connect(steady["peer_id"])
    held = None
    connecting = None
    try:
        await _until(lambda: bridge.stats().get("presence:beats", 0) >= 1, "a beat")
        before = bridge.stats()
        held = _PeerRowLock(locked["peer_id"], hold=STALL_HOLD_SECS)
        t0 = time.monotonic()
        connecting = asyncio.create_task(_connect(locked["peer_id"]))
        await _until(lambda: _waiting_on_a_lock(owner) >= 2,
                     "the insert and a beat to wait on the locked row", timeout=5)
        _notify(owner, CALL_CHANNEL, "stalled-garbage")
        await asyncio.sleep(max(0.0, t0 + STALL_HOLD_SECS - 0.4 - time.monotonic()))
        connected = connecting.done()
        counts = _delta(before, bridge.stats(), "presence:beat_errors", "call:")
        still_held = held.is_held()
    finally:
        if held is not None:
            held.release()
    late, late_id = await connecting
    try:
        assert connected, "the socket's connect waited on the locked row past the insert's bound"
        assert counts.get("presence:beat_errors", 0) >= 1, (
            f"no beat gave up while the row was held: {counts}")
        assert counts.get("call:undecodable") == 1, (
            f"the listener handled no notification while the row was held: {counts}")
        assert still_held, "control: observed while the row was still held"
        row = await _until(lambda: _rows(owner, late_id).get(late_id),
                           "a beat to write the locked peer's row once it is released")
        assert row[1] == locked["peer_id"]
        errors = bridge.stats().get("presence:beat_errors", 0)
        start = bridge.stats().get("presence:beats", 0)
        await _until(lambda: bridge.stats().get("presence:beats", 0) >= start + 3, "three beats")
        assert bridge.stats().get("presence:beat_errors", 0) == errors, "the beat still fails"
    finally:
        await late.disconnect()
        await sock.disconnect()


@pytest.mark.asyncio
async def test_a_disconnect_does_not_wait_for_a_beat_stuck_on_the_database(owner, bridge_tuned):
    """The review's scenario. A beat is stuck on a locked peer row (its snapshot holds two
    sockets and a registered socket of the locked peer that has no row yet). While it is:
    one of the two sockets disconnects, and returns within 2 s; then more detaches than the
    default executor has threads run at once, and a no-op sent through the executor returns
    within 0.5 s, so no detach holds a thread while it waits. Released, the stuck beat
    finishes its write, which brings the departed socket's row back, and then deletes it: the
    row is gone, the other socket's stays, and the locked peer's is written (the control).
    The next beat is put an hour away while the beat is stuck, so no later beat can be what
    deleted the row."""
    bridge = bridge_tuned(beat_secs=0.2, reoffer_secs=3600)
    leaving, staying, locked = (seed_peer(owner, t) for t in ("leaving", "staying", "locked"))
    sock1, cid1 = await _connect(leaving["peer_id"])
    sock2, cid2 = await _connect(staying["peer_id"])
    fake = ConnectionHandle(connection_id=uuid.uuid4(), peer_id=locked["peer_id"],
                            queue=SendQueue())
    held = None
    detaches: list = []
    try:
        await _until(lambda: set(_rows(owner, cid1, cid2)) == {cid1, cid2}, "both rows")
        held = _PeerRowLock(locked["peer_id"], hold=4.0)
        await MANAGER.register(fake)
        await _until(lambda: _waiting_on_a_lock(owner) >= 1,
                     "a beat to wait on the locked row", timeout=3)
        beats = bridge.stats().get("presence:beats", 0)
        bridge_tuned(beat_secs=3600)          # read when the stuck beat returns
        t = time.monotonic()
        await sock1.disconnect(timeout=15)
        leave_secs = time.monotonic() - t
        n = _default_executor_workers() + 4
        detaches = [asyncio.create_task(bridge.detach(uuid.uuid4())) for _ in range(n)]
        await asyncio.sleep(0.05)
        t = time.monotonic()
        probe = asyncio.create_task(asyncio.to_thread(lambda: None))
        done, _ = await asyncio.wait({probe}, timeout=2.0)
        probe_secs = time.monotonic() - t
        still_held = held.is_held()
    finally:
        if held is not None:
            held.release()
    try:
        await asyncio.gather(*detaches)
        assert leave_secs < 2.0, (
            f"the disconnect waited {leave_secs:.2f}s for a beat stuck on the database")
        assert done and probe_secs < 0.5, (
            f"a no-op through the default executor waited {probe_secs:.2f}s behind "
            f"{n} detaches: they hold its threads while they wait")
        assert still_held, "control: measured while the row was still held"
        # Counted after its write, its departed drain and its prune: the whole beat is done.
        await _until(lambda: bridge.stats().get("presence:beats", 0) >= beats + 1,
                     "the stuck beat to finish once the row is released", timeout=8)
        assert set(_rows(owner, cid1, cid2)) == {cid2}, (
            "the stuck beat left the departed socket's row it wrote back (a ghost), or "
            "deleted the wrong row")
        assert _rows(owner, fake.connection_id), "control: the stuck beat's write committed"
        assert bridge.stats().get("presence:beats", 0) == beats + 1, (
            "control: only the stuck beat ran")
    finally:
        await MANAGER.deregister(fake.peer_id, fake.connection_id)
        await sock2.disconnect()


@pytest.mark.asyncio
async def test_a_detach_that_outwaits_a_held_beat_is_finished_by_that_beat(
        owner, bridge_tuned, monkeypatch):
    """The snapshot test above holds a beat for 0.5 s, and the delete waits for it. Here the
    beat is held between its snapshot and its write for longer than a detach waits. The
    disconnect returns while the beat is still held (the delete is handed to the beat), and
    once released the beat writes the row it snapshotted and then deletes it: the row is gone
    and stays gone. Deleted before the beat's write, or not handed over at all, it would be a
    ghost that reads online for 30 s. While the beat is held the next one is put an hour away,
    so the row is gone because THAT beat deleted it after its write: a later beat deleting it
    would leave a ghost for a whole beat interval, and would pass a test with 0.2 s beats."""
    bridge = bridge_tuned(beat_secs=0.2, reoffer_secs=3600)
    peer = seed_peer(owner, "handed")
    sock, cid = await _connect(peer["peer_id"])
    gate = _Gate()
    original = hub_presence.upsert_beat

    def held(conn, rows, worker_token):
        rows = list(rows)
        if any(c == cid for c, _ in rows):
            gate.hold()
        return original(conn, rows, worker_token)

    monkeypatch.setattr(hub_presence, "upsert_beat", held)
    closing = None
    try:
        await _until(lambda: _seen_at(owner, cid), "the row")
        before = bridge.stats()
        gate.armed.set()
        assert await gate.wait_entered(), "no beat snapshotted the socket"
        beats = bridge.stats().get("presence:beats", 0)     # the held beat is not counted yet
        bridge_tuned(beat_secs=3600)          # read when the held beat returns
        t = time.monotonic()
        closing = asyncio.create_task(sock.disconnect(timeout=15))
        done, _ = await asyncio.wait({closing}, timeout=2.0)
        close_secs = time.monotonic() - t
        held_during = not gate.release.is_set()
    finally:
        gate.armed.clear()
        gate.release.set()
    if closing is not None:
        await closing
    assert done, f"the disconnect waited {close_secs:.2f}s for a beat held past the bound"
    assert held_during, "control: measured while the beat was held"
    assert _delta(before, bridge.stats(), "presence:delete_handed_to_beat") == {
        "presence:delete_handed_to_beat": 1}
    # Counted after its write, its departed drain and its prune: the whole beat is done.
    await _until(lambda: bridge.stats().get("presence:beats", 0) >= beats + 1,
                 "the held beat to finish", timeout=5)
    assert _rows(owner, cid) == {}, (
        "the held beat left the row it wrote after the detach: a ghost that reads online")
    assert bridge.stats().get("presence:beats", 0) == beats + 1, "control: only the held beat ran"


@pytest.mark.asyncio
async def test_every_connection_the_bridge_writes_or_queries_through_is_bounded(
        owner, bridge_tuned, monkeypatch):
    """Every connection the bridge opens for the insert, the delete and its query connection
    (the beat, the prune and, from S5, the row reads) carries a statement bound and a connect
    timeout, each under one beat interval. The LISTEN connection runs no statement that can
    wait and is LiveBridge's shape. The control: all four were opened, and recorded."""
    from apps.core import dbconn

    bridge = bridge_tuned(beat_secs=0.2, reoffer_secs=3600)
    real = dbconn.connect
    sites = ("_insert_row", "_delete_row", "get", "_run_once")
    opened: list[tuple[str, dict]] = []

    def recording(*args, **kwargs):
        frame = sys._getframe(1)
        if frame.f_globals.get("__name__") == call_notify.__name__:
            site, f = frame.f_code.co_name, frame
            for _ in range(4):
                if f is None:
                    break
                if (f.f_globals.get("__name__") == call_notify.__name__
                        and f.f_code.co_name in sites):
                    site = f.f_code.co_name
                    break
                f = f.f_back
            opened.append((site, dict(kwargs)))
        return real(*args, **kwargs)

    monkeypatch.setattr(dbconn, "connect", recording)
    peer = seed_peer(owner, "bounded")
    sock, _ = await _connect(peer["peer_id"])
    try:
        start = bridge.stats().get("presence:beats", 0)
        await _until(lambda: bridge.stats().get("presence:beats", 0) >= start + 1, "a beat")
    finally:
        await sock.disconnect()
    assert set(sites) <= {s for s, _ in opened}, opened
    limit_ms = 1000 * hub_presence.BEAT_INTERVAL_SECS
    for site, kwargs in opened:
        if site == "_run_once":
            continue
        assert 0 < kwargs.get("statement_timeout_ms", 0) < limit_ms, (site, kwargs)
        assert 0 < kwargs.get("connect_timeout", 0) < hub_presence.BEAT_INTERVAL_SECS, (
            site, kwargs)
