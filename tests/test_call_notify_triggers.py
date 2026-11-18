"""The two NOTIFY triggers of divergence 041 S3, proved alone (design 4.5).

Design `docs/design/skill-frames.md` (fin/design-skill-frames, 2e229a6), section 4.5:

    AFTER UPDATE OF status ON skill_calls, when NEW.status IS DISTINCT FROM OLD.status:
        pg_notify('meshy_call', {call_id, status, target})
    AFTER INSERT ON skill_call_chunks:
        pg_notify('meshy_call_chunk', {call_id, sequence})

"Why triggers and not pg_notify calls in application code": status changes are written by
at least five code paths in three processes, and the janitor is the one most likely to be
forgotten. So one test times a call out with the janitor's own function on a connection of
its own, and the notification still arrives.

WHAT IS PROVED, each on a real database through a LISTEN connection opened as meshychat_app:
  * one notification per status EDGE, none for a same-status update, an update that does
    not name `status`, or a rolled-back edge;
  * one notification per committed chunk insert, none for a rolled-back or refused one;
  * each payload carries exactly the pointer keys, and nothing the caller or target wrote
    beyond the call_id (the args, the signature and the chunk content carry a marker that
    must not appear in any payload);
  * the call_id rides only while it is a call id by the application's own bound (at most
    200 bytes, `apps/calls/repository.validate_call_id`), so a payload is never more than a
    few hundred bytes; a row with a longer call_id (no writer in this server can make one)
    commits its edge and notifies nothing.

HOW A NEGATIVE IS OBSERVED. Notifications reach a listener in commit order. Each test ends
with a SENTINEL edge on a second call, and collects until the sentinel's notification
arrives. Everything committed before the sentinel has then been delivered, so "not among
them" is a fact about the queue, not a timing guess (rule C1: the sentinel is the positive
control in every test).

Run:  .venv/Scripts/python.exe -m pytest tests/test_call_notify_triggers.py -q -p no:cacheprovider
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import time
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from apps.calls import janitor  # noqa: E402
from apps.core import dbconn, rls  # noqa: E402
from tests.fixtures.call_rows import (ARGS_MARKER, seed_call, seed_peer,  # noqa: E402
                                      seed_skill)

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")

CALL_CHANNEL = "meshy_call"
CHUNK_CHANNEL = "meshy_call_chunk"
CALL_KEYS = {"call_id", "status", "target"}
CHUNK_KEYS = {"call_id", "sequence"}

#: How long a test waits for its sentinel before calling the queue empty of it.
SENTINEL_TIMEOUT_SECS = 10.0


# ------------------------------------------------------------------------- fixtures

@pytest.fixture()
def owner():
    """The bootstrap role, autocommit: seeds rows and reads them back. Bypasses RLS."""
    with psycopg.connect(DSN, autocommit=True) as conn:
        yield conn


@pytest.fixture()
def listener():
    """A meshychat_app connection LISTENing on both channels, as the CallBridge will."""
    conn = dbconn.connect(DSN)
    try:
        conn.execute(f"LISTEN {CALL_CHANNEL}")
        conn.execute(f"LISTEN {CHUNK_CHANNEL}")
        yield conn
    finally:
        conn.close()


@pytest.fixture()
def writer():
    """The application's write path: a meshychat_app connection, writes under server_internal
    (sql_meshychat/012 binds skill_calls' UPDATE to it)."""
    conn = dbconn.connect(DSN)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture()
def parties(owner):
    caller, target = seed_peer(owner, "caller"), seed_peer(owner, "target")
    return {"caller": caller, "target": target, "skill": seed_skill(owner, target)}


# -------------------------------------------------------------------------- helpers

def _call(owner, parties, **kw) -> str:
    return seed_call(owner, parties["caller"], parties["target"], parties["skill"], **kw)


def _set_status(writer, call_id: str, status: str) -> None:
    """One committed edge, written the way the application writes one."""
    with rls.server_internal(writer):
        cur = writer.execute("UPDATE skill_calls SET status = %s WHERE call_id = %s",
                             (status, call_id))
        assert cur.rowcount == 1, f"no row {call_id!r} to update"


def _collect(listener, sentinel_call_id: str) -> list[tuple[str, str]]:
    """(channel, payload) of every notification up to and including the sentinel's, which
    must arrive: it is the positive control that the queue was read to its end."""
    got: list[tuple[str, str]] = []
    deadline = time.monotonic() + SENTINEL_TIMEOUT_SECS
    while time.monotonic() < deadline:
        for note in listener.notifies(timeout=max(0.05, deadline - time.monotonic()),
                                      stop_after=1):
            got.append((note.channel, note.payload))
            if note.channel == CALL_CHANNEL and _names(note.payload, sentinel_call_id):
                return got
    raise AssertionError(
        f"the sentinel's notification never arrived within {SENTINEL_TIMEOUT_SECS}s: no "
        f"trigger notifies {CALL_CHANNEL!r} on a status edge. Received instead: {got[:5]}")


def _names(payload: str, call_id: str) -> bool:
    try:
        obj = json.loads(payload)
    except ValueError:
        return False
    return isinstance(obj, dict) and obj.get("call_id") == call_id


def _sentinel_edge(owner, writer, parties) -> str:
    call_id = _call(owner, parties)
    _set_status(writer, call_id, "routed")
    return call_id


def _for(got, channel: str, call_id: str) -> list[dict]:
    """The decoded payloads on `channel` naming `call_id`, in arrival order."""
    return [json.loads(p) for ch, p in got if ch == channel and _names(p, call_id)]


def _raw_for(got, call_id: str) -> list[str]:
    return [p for _, p in got if _names(p, call_id)]


# ----------------------------------------------------------- the status trigger

def test_each_status_edge_is_one_notification_carrying_only_the_pointer(
        owner, listener, writer, parties):
    """pending -> routed -> running -> completed, each committed alone: three notifications,
    in order, each exactly {call_id, status, target}. Between them, four writes that are NOT
    edges and must notify nothing: the same status again, an update that does not name
    `status`, an update that names it with its current value alongside another column, and
    an edge that is rolled back."""
    call_id = _call(owner, parties)
    _set_status(writer, call_id, "routed")
    _set_status(writer, call_id, "routed")                       # same status: no edge
    with rls.server_internal(writer):                            # status not named
        writer.execute("UPDATE skill_calls SET error_message = 'x' WHERE call_id = %s",
                       (call_id,))
    with rls.server_internal(writer):                            # named, unchanged
        writer.execute("UPDATE skill_calls SET status = status, error_code = NULL "
                       "WHERE call_id = %s", (call_id,))
    try:                                                         # an edge rolled back
        with rls.server_internal(writer):
            writer.execute("UPDATE skill_calls SET status = 'failed' "
                           "WHERE call_id = %s", (call_id,))
            raise RuntimeError("roll back")
    except RuntimeError:
        pass
    _set_status(writer, call_id, "running")
    _set_status(writer, call_id, "completed")
    sentinel = _sentinel_edge(owner, writer, parties)

    got = _collect(listener, sentinel)
    mine = _for(got, CALL_CHANNEL, call_id)
    assert [n["status"] for n in mine] == ["routed", "running", "completed"], mine
    for n in mine:
        assert set(n) == CALL_KEYS, f"the payload carries more or less than the pointer: {n}"
        assert n["call_id"] == call_id
        assert n["target"] == str(parties["target"]["peer_id"])
    for raw in _raw_for(got, call_id):
        assert ARGS_MARKER not in raw, f"caller data rode the notification: {raw}"
        assert str(parties["caller"]["peer_id"]) not in raw, raw
    assert _for(got, CHUNK_CHANNEL, call_id) == []
    row = owner.execute("SELECT status FROM skill_calls WHERE call_id = %s",
                        (call_id,)).fetchone()
    assert row == ("completed",), "control: the edges were written"


def test_two_edges_in_one_transaction_are_two_notifications_at_its_commit(
        owner, listener, writer, parties):
    """A NOTIFY is sent at COMMIT (pg_notify is transactional), so a target can never read
    the pointer before the row it points at is committed. Two edges in one transaction are
    two distinct payloads, delivered in order when it commits; before the commit, the
    listener holds nothing for the call."""
    call_id = _call(owner, parties)
    with rls.server_internal(writer):
        writer.execute("UPDATE skill_calls SET status = 'routed' WHERE call_id = %s",
                       (call_id,))
        writer.execute("UPDATE skill_calls SET status = 'running' WHERE call_id = %s",
                       (call_id,))
        early = list(listener.notifies(timeout=0.3))
    assert [n for n in early if call_id in n.payload] == [], "a pointer arrived before COMMIT"
    sentinel = _sentinel_edge(owner, writer, parties)
    got = _collect(listener, sentinel)
    assert [n["status"] for n in _for(got, CALL_CHANNEL, call_id)] == ["routed", "running"]


def test_a_timeout_the_janitor_writes_on_another_connection_notifies_the_listener(
        owner, listener, parties):
    """T12's trigger half: the deadline janitor's own function (`apps/calls/janitor.
    sweep_expired_deadlines`), on a connection of its own, times the call out, and the
    listener hears `timeout`. No application code in the janitor sends a notification;
    only a trigger can make this pass."""
    past = _dt.datetime(1971, 1, 1, tzinfo=_dt.timezone.utc)
    call_id = _call(owner, parties, status="running", deadline=past)
    swept: list[str] = []
    janitor_conn = dbconn.connect(DSN)
    try:
        for _ in range(50):                  # the per-run database may hold older expiries
            swept += janitor.sweep_expired_deadlines(janitor_conn, limit=500)
            if call_id in swept:
                break
    finally:
        janitor_conn.close()
    assert call_id in swept, "control: the janitor timed the call out"
    writer = dbconn.connect(DSN)
    try:
        sentinel = _sentinel_edge(owner, writer, parties)
    finally:
        writer.close()
    got = _collect(listener, sentinel)
    mine = _for(got, CALL_CHANNEL, call_id)
    assert mine == [{"call_id": call_id, "status": "timeout",
                     "target": str(parties["target"]["peer_id"])}], mine


# ------------------------------------------------------------ the chunk trigger

def _chunk(conn, call_id: str, sequence: int) -> None:
    conn.execute(
        "INSERT INTO skill_call_chunks (call_id, sequence, chunk_type, content, "
        "  chunk_signature, chunk_signing_kid) VALUES (%s,%s,'text',%s,%s,'kid-s3')",
        (call_id, sequence, ARGS_MARKER.encode(), f"sig-{ARGS_MARKER}"))


def test_each_committed_chunk_is_one_notification_carrying_only_call_id_and_sequence(
        owner, listener, writer, parties):
    """Chunks 0, 1 and 2 committed: three notifications, in order, each exactly
    {call_id, sequence}. A rolled-back chunk and a duplicate sequence (refused by the
    UNIQUE) notify nothing. The content and the signature, which carry the marker, never
    ride a notification."""
    call_id = _call(owner, parties, status="running")
    for seq in (0, 1, 2):
        _chunk(owner, call_id, seq)
    try:
        with owner.transaction():
            _chunk(owner, call_id, 3)
            raise RuntimeError("roll back")
    except RuntimeError:
        pass
    with pytest.raises(psycopg.errors.UniqueViolation):
        _chunk(owner, call_id, 1)
    sentinel = _sentinel_edge(owner, writer, parties)

    got = _collect(listener, sentinel)
    mine = _for(got, CHUNK_CHANNEL, call_id)
    assert [n["sequence"] for n in mine] == [0, 1, 2], mine
    for n in mine:
        assert set(n) == CHUNK_KEYS, f"the payload carries more or less than the pointer: {n}"
        assert n["call_id"] == call_id and type(n["sequence"]) is int
    for raw in _raw_for(got, call_id):
        assert ARGS_MARKER not in raw, f"chunk content rode the notification: {raw}"
    assert _for(got, CALL_CHANNEL, call_id) == [], "a chunk insert is not a status edge"
    stored = owner.execute("SELECT count(*) FROM skill_call_chunks WHERE call_id = %s",
                           (call_id,)).fetchone()[0]
    assert stored == 3, "control: three chunks were written"


# --------------------------------------------------------------- the bound

def test_the_call_id_rides_only_within_its_200_byte_bound(owner, listener, writer, parties):
    """A call_id of exactly 200 bytes (100 two-byte characters) rides whole, and the whole
    payload stays small. One of 201 bytes cannot come from this server (validate_call_id
    refuses it before anything is stored), so only another writer could store it: its edge
    and its chunk COMMIT, and notify nothing, so a payload never carries more than 200
    bytes of anyone's text and pg_notify's 8000-byte refusal can never fail a write."""
    at_bound = "é" * 100
    over = at_bound + "x"
    assert len(at_bound.encode()) == 200 and len(over.encode()) == 201
    ok = _call(owner, parties, call_id=at_bound)
    long = _call(owner, parties, call_id=over)
    _set_status(writer, ok, "routed")
    _set_status(writer, long, "routed")
    _chunk(owner, ok, 0)
    _chunk(owner, long, 0)
    sentinel = _sentinel_edge(owner, writer, parties)

    got = _collect(listener, sentinel)
    assert [n["status"] for n in _for(got, CALL_CHANNEL, ok)] == ["routed"]
    assert [n["sequence"] for n in _for(got, CHUNK_CHANNEL, ok)] == [0]
    assert all(len(raw.encode()) < 400 for raw in _raw_for(got, ok)), _raw_for(got, ok)
    assert _raw_for(got, long) == [], "a call_id over the bound rode a notification"
    status = owner.execute("SELECT status FROM skill_calls WHERE call_id = %s",
                           (long,)).fetchone()
    assert status == ("routed",), "control: the over-bound edge still committed"


# --------------------------------------------------------------- the catalog

TRIGGERS = """
    SELECT c.relname, t.tgname, t.tgtype, pg_get_triggerdef(t.oid), p.proname
    FROM pg_trigger t
    JOIN pg_proc p ON p.oid = t.tgfoid
    JOIN pg_class c ON c.oid = t.tgrelid
    WHERE NOT t.tgisinternal AND p.prosrc ILIKE %s
    ORDER BY 1, 2
"""


def test_exactly_two_triggers_notify_the_call_channels_and_they_are_the_designs(owner):
    """Every non-internal trigger in the database whose function names a call channel:
    exactly one AFTER UPDATE OF status FOR EACH ROW on skill_calls, with the IS DISTINCT
    FROM clause, and one AFTER INSERT FOR EACH ROW on skill_call_chunks. The detector's
    positive control: the same query finds sql/036's mesh trigger by its channel.

    Since 041 S9 the prefix also matches a third channel, `meshy_call_webhook`
    (sql_meshychat/021): its two triggers, on the doorbell's tables, are named here, are
    proved in tests/test_invoke_webhook.py, and must name that channel and not the call
    channels. Anything else on the prefix still fails the census."""
    rows = owner.execute(TRIGGERS, ("%meshy_call%",)).fetchall()
    doorbell = [r for r in rows if r[0] in ("peer_invocation_webhooks", "peer_webhook_attempts")]
    assert sorted((r[0], r[4]) for r in doorbell) == [
        ("peer_invocation_webhooks", "meshy_webhook_verify_notify"),
        ("peer_webhook_attempts", "meshy_webhook_attempt_notify")], doorbell
    for (source,) in owner.execute(
            "SELECT prosrc FROM pg_proc WHERE proname IN "
            "('meshy_webhook_verify_notify', 'meshy_webhook_attempt_notify')").fetchall():
        assert ("'meshy_call_webhook'" in source and "'meshy_call'" not in source
                and "'meshy_call_chunk'" not in source), source
    rows = [r for r in rows if r not in doorbell]
    tables = sorted(r[0] for r in rows)
    assert tables == ["skill_call_chunks", "skill_calls"], rows
    by_table = {r[0]: r for r in rows}
    # pg_trigger.tgtype bits: 1 ROW, 2 BEFORE, 4 INSERT, 8 DELETE, 16 UPDATE, 32 TRUNCATE.
    status_type, status_def = by_table["skill_calls"][2], by_table["skill_calls"][3]
    assert status_type & 1 and not status_type & 2 and status_type & 16, status_type
    assert not status_type & (4 | 8 | 32), status_type
    assert "UPDATE OF status" in status_def, status_def
    assert "IS DISTINCT FROM" in status_def, status_def
    chunk_type = by_table["skill_call_chunks"][2]
    assert chunk_type & 1 and not chunk_type & 2 and chunk_type & 4, chunk_type
    assert not chunk_type & (8 | 16 | 32), chunk_type
    control = owner.execute(TRIGGERS, ("%mesh_msg%",)).fetchall()
    assert [r[0] for r in control] == ["shared_memory"], control
