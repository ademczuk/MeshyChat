"""The live half of telling the seats about a redaction (divergence 053; docs/design/redaction.md section 5a,
test 10; tests/test_redaction_delivery.py is the durable half through the addressed wait).

The trigger on `shared_memory_redaction` NOTIFYs `meshy_mesh_redaction` with the record's id at COMMIT. The
production bridge (`mesh_notify.BRIDGE`, one LISTEN thread) reads the record and its row back and sends
each connected peer it is for one `mesh_redaction` frame, through the production application and the
production connection registry, as tests/test_mesh_live.py does for messages. Who it is for is the
DURABLE rule (apps/mesh/wait.py `redaction_event`): the row's own recipients over the tombstone's `to`/`cc`
(or a broadcast), and the authors of its replies named on the record; never the seat that redacted.

An ABSENCE is proved the way test_mesh_live.py proves one: a message written afterwards to everyone is the
next frame of a peer that was not told, so the peer that received the event received it BEFORE that message
(one LISTEN thread handles the two notifications in commit order) and the peer that did not, did not.

Run:  .venv313/Scripts/python.exe -m pytest tests/test_redaction_push.py -q -p no:cacheprovider
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_mesh_live import (  # noqa: E402,F401
    DSN, NS, _connect, _frame, _frames_delta, _grant, _insert_msg, _mesh, _peer, _v1, bridge, owner,
    registry_left_clean, roster, server_config,
)
from apps.wsgw.constants import REDACTION_NOTIFY_CHANNEL  # noqa: E402

MARKER = "zq-push-marker-77aa"
EVENT_FIELDS = {"type", "key", "version", "redacted_at", "reason_code"}


def _row_id(conn, key):
    return conn.execute("SELECT id FROM shared_memory WHERE key = %s", (key,)).fetchone()[0]


def _redact_message(conn, key, redactor, *, reason="client_name", notify=()) -> dict:
    """The handler's own `_redact_one`, on the committed mail row `key`, in the caller's transaction (which commits on exit
    of `conn.transaction()` for an autocommit `conn`, and is the caller's to roll back for a non-autocommit one)."""
    from apps.memoryskills import redact
    cur = conn.cursor(row_factory=dict_row)
    cur.execute("SELECT id, version, value, signed_bytes, payload_jcs_sha256, created_by_peer_id "
                "FROM shared_memory WHERE key = %s AND namespace = %s", (key, NS))
    found = cur.fetchone()
    return redact._redact_one(cur, found, NS, key, redactor["peer_id"], redactor["owner_id"], reason, False,
                              b"the request as signed", f"call-{uuid.uuid4()}", [str(p) for p in notify])


def _committed_redaction(owner, key, redactor, **kw) -> dict:
    with owner.transaction():
        return _redact_message(owner, key, redactor, **kw)


@pytest.mark.asyncio
async def test_a_connected_recipient_and_a_reply_author_receive_the_event_and_nobody_else_does(owner, bridge, roster):
    author, bob, dave, erin, zed = (_peer(owner, t) for t in ("author", "bob", "dave", "erin", "zed"))
    for p in (author, bob, dave, erin):
        _grant(owner, p, "read")
    socks = {}
    try:
        for name, p in (("author", author), ("bob", bob), ("dave", dave), ("erin", erin)):
            socks[name] = await _connect(p)
        assert await bridge.wait_listening(10)
        m1 = _insert_msg(owner, author, _v1([bob["name"]], body=f"about {MARKER}"))
        assert (await _mesh(socks["bob"]))["key"] == m1, "control: bob received the message"
        before = bridge.stats()

        record = _committed_redaction(owner, m1, author, notify=[erin["peer_id"]])
        for who in ("bob", "erin"):
            frame = await _frame(socks[who])
            assert set(frame) == EVENT_FIELDS, (who, frame)
            assert (frame["type"], frame["key"], frame["version"], frame["reason_code"]) == (
                "mesh_redaction", m1, 1, "client_name"), (who, frame)
            assert frame["redacted_at"] == record["redacted_at"][:19] + "Z"
            assert MARKER not in json.dumps(frame)

        # A message to everyone written AFTER the redaction is the first frame of every peer the event was not for.
        after = _insert_msg(owner, zed, _v1(["all"]))
        for who in ("author", "dave"):
            assert (await _mesh(socks[who]))["key"] == after, f"{who} was sent the event"
        for who in ("bob", "erin"):
            assert (await _mesh(socks[who]))["key"] == after, "control: the marker reached the recipients too, after the event"

        assert _frames_delta(before, bridge.stats()) == {
            "redaction:notifications": 1,
            "redaction:frames": 2,                     # bob (to) and erin (a reply author on the record)
            "redaction:skipped:redactor": 1,           # the author redacted its own row
            "redaction:skipped:not_addressed": 1,      # dave: may read the namespace, not addressed, no reply
            "mesh:notifications": 1,
            "mesh:frames": 4,                          # the marker to author, bob, dave and erin
        }
    finally:
        for comm in socks.values():
            await comm.disconnect()


@pytest.mark.asyncio
async def test_a_rolled_back_redaction_is_never_announced(owner, bridge, roster):
    author, bob = _peer(owner, "author"), _peer(owner, "bob")
    _grant(owner, bob, "read")
    comm = await _connect(bob)
    try:
        assert await bridge.wait_listening(10)
        m1 = _insert_msg(owner, author, _v1([bob["name"]]))
        assert (await _mesh(comm))["key"] == m1
        before = bridge.stats()
        with psycopg.connect(DSN) as tx:                 # NOT autocommit: one transaction
            _redact_message(tx, m1, author)
            tx.rollback()
        marker = _insert_msg(owner, author, _v1([bob["name"]]))
        assert (await _mesh(comm))["key"] == marker, "the rolled-back redaction produced a frame"
        assert "redaction:notifications" not in _frames_delta(before, bridge.stats())
        assert owner.execute("SELECT count(*) FROM shared_memory_redaction r JOIN shared_memory sm ON sm.id = r.memory_id "
                             "WHERE sm.key = %s", (m1,)).fetchone()[0] == 0
    finally:
        await comm.disconnect()


@pytest.mark.asyncio
async def test_a_forged_notification_cannot_put_text_on_the_wire(owner, bridge, roster):
    """NOTIFY is unauthenticated. A forged payload naming no record, a payload that is not JSON, and one that carries text of
    its own produce no frame; one naming a REAL record re-announces exactly what the record and its row hold, never the payload's
    fields. The control is the message written after them, which is the next frame."""
    author, bob = _peer(owner, "author"), _peer(owner, "bob")
    _grant(owner, bob, "read")
    comm = await _connect(bob)
    try:
        assert await bridge.wait_listening(10)
        m1 = _insert_msg(owner, author, _v1([bob["name"]], body=MARKER))
        assert (await _mesh(comm))["key"] == m1
        record = _committed_redaction(owner, m1, author)
        genuine = await _frame(comm)
        assert genuine["type"] == "mesh_redaction" and genuine["key"] == m1

        before = bridge.stats()
        forged = {"id": record["redaction_id"], "key": "msg/forged", "version": 99, "reason_code": "credential",
                  "body": "forged-text"}
        owner.execute("SELECT pg_notify(%s, %s)", (REDACTION_NOTIFY_CHANNEL, json.dumps(forged)))
        renudge = await _frame(comm)
        assert renudge == genuine, f"the frame took the forged payload's fields: {renudge}"

        owner.execute("SELECT pg_notify(%s, %s)", (REDACTION_NOTIFY_CHANNEL, json.dumps(dict(forged, id=str(uuid.uuid4())))))
        owner.execute("SELECT pg_notify(%s, %s)", (REDACTION_NOTIFY_CHANNEL, "this is not json"))
        marker = _insert_msg(owner, author, _v1([bob["name"]]))
        assert (await _mesh(comm))["key"] == marker, "a notification for no such record made a frame"
        delta = _frames_delta(before, bridge.stats())
        assert delta["redaction:notifications"] == 3 and delta["redaction:record_not_found"] == 1
        assert delta["redaction:undecodable"] == 1 and delta["redaction:frames"] == 1, delta
    finally:
        await comm.disconnect()


@pytest.mark.asyncio
async def test_a_suspended_recipient_and_a_recipient_without_read_are_not_told(owner, bridge, roster):
    """The two refusals a message frame has (mesh_notify.resolve_recipients) hold for the event too. bob is addressed, active and
    may read: told (the control). gus is addressed and may read but was suspended after connecting: not told. hal is addressed and
    connected but holds no read on the namespace: not told. The bridge counts each refusal by its reason."""
    from django.test import override_settings

    author, bob, gus, hal = (_peer(owner, t) for t in ("author", "bob", "gus", "hal"))
    for p in (bob, gus):
        _grant(owner, p, "read")
    socks = {}
    held_off = override_settings(MESHY_WS_REVOCATION_SWEEP_SECS=3600.0)      # the standing sweep would close gus's socket first
    held_off.enable()
    try:
        for name, p in (("bob", bob), ("gus", gus), ("hal", hal)):
            socks[name] = await _connect(p)
        assert await bridge.wait_listening(10)
        m1 = _insert_msg(owner, author, _v1([bob["name"], gus["name"], hal["name"]], body=MARKER))
        assert (await _mesh(socks["bob"]))["key"] == m1, "control: bob received the message"
        assert (await _mesh(socks["gus"]))["key"] == m1, "control: gus was addressed and could read it"
        owner.execute("UPDATE peers SET status = 'suspended' WHERE id = %s", (gus["peer_id"],))
        before = bridge.stats()

        _committed_redaction(owner, m1, author)
        frame = await _frame(socks["bob"])
        assert (frame["type"], frame["key"]) == ("mesh_redaction", m1), frame
        for who in ("gus", "hal"):
            assert await socks[who].receive_nothing(timeout=1.0), f"{who} was sent the event"
        delta = _frames_delta(before, bridge.stats())
        assert delta.get("redaction:notifications") == 1, delta
        assert delta.get("redaction:skipped:inactive") == 1, delta
        assert delta.get("redaction:skipped:no_read:E_PERMISSION_DENIED") == 1, delta
        assert delta.get("redaction:frames") == 1, delta
    finally:
        for comm in socks.values():
            await comm.disconnect()
        held_off.disable()
