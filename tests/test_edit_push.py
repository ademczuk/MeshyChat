"""The live half of telling the seats about an edit (divergence 059; docs/design/edit.md section 4 and tests 12, 14 and 17;
tests/test_edit_delivery.py is the durable half through the addressed wait, and tests/test_redaction_push.py is the model).

The trigger on `shared_memory_edit` NOTIFYs `meshy_mesh_edit` with the record's id at COMMIT. The production bridge (`mesh_notify.BRIDGE`, one LISTEN
thread) reads the record and its row back and sends each connected peer it is for one `mesh_edit` frame WITH THAT PEER'S ROLE, through the production
application and the production connection registry, as tests/test_mesh_live.py does for messages. Who it is for is the DURABLE rule
(apps/mesh/wait.py `edit_event`): the row's own recipients over the head's `to`/`cc` (or a broadcast), and the authors of its replies named on the
record in the role `replied`; never the seat that edited. The row the edit wrote is ALSO an ordinary message to the bridge (sql/036 fires on it), so a
recipient's first frame is that message, now carrying `version`, `edited_by` and `edited_at`, and its second is the event.

An ABSENCE is proved the way test_mesh_live.py proves one: a message written afterwards to everyone is the next frame of a peer that was not told, so the
peer that received the event received it BEFORE that message (one LISTEN thread handles the notifications in commit order) and the peer that did not,
did not.

Run:  .venv313/Scripts/python.exe -m pytest tests/test_edit_push.py -q -p no:cacheprovider
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from apps.wsgw.constants import EDIT_NOTIFY_CHANNEL  # noqa: E402
from tests.fixtures import signed_rows as sr  # noqa: E402
from tests.test_edit import _edit  # noqa: E402
from tests.test_mesh_authorship import (  # noqa: E402,F401
    MESH, _admit, _head, _key, _msg, _peer as _apeer, _value, _write, app_conn, owner_conn,
)
from tests.test_mesh_live import (  # noqa: E402,F401
    DSN, NS, _connect, _frame, _frames_delta, _grant, _insert_msg, _mesh, _peer, _v1, bridge, owner,
    registry_left_clean, roster, server_config,
)

MARKER = "zq-edit-push-marker-91cc"
EVENT_FIELDS = {"type", "key", "version", "edited_at", "edited_by", "role"}


def _writers(owner_conn, tag):
    """An author and a sibling seat of the same owner, both admitted to write (signing seats: the handler verifies a signature)."""
    author = _apeer(owner_conn, tag + "a")
    _admit(owner_conn, MESH, author)
    sibling = _apeer(owner_conn, tag + "s", same_owner_as=author)
    _admit(owner_conn, MESH, sibling)
    return author, sibling


def _record_id(owner, key, version=2):
    return owner.execute("SELECT r.id FROM shared_memory_edit r JOIN shared_memory sm ON sm.id = r.memory_id "
                         "WHERE sm.key = %s AND sm.version = %s", (key, version)).fetchone()[0]


@pytest.mark.asyncio
async def test_a_connected_recipient_and_a_reply_author_are_told_each_in_its_role_and_nobody_else_is(owner, owner_conn, app_conn, bridge, roster):
    author, sibling = _writers(owner_conn, "pa")
    bob, dave, erin, zed = (_peer(owner, t) for t in ("bob", "dave", "erin", "zed"))
    for p in (bob, dave, erin):
        _grant(owner, p, "read")
    socks = {}
    try:
        for name, p in (("bob", bob), ("dave", dave), ("erin", erin)):
            socks[name] = await _connect(p)
        assert await bridge.wait_listening(10)
        key = _key(author["name"])
        value = dict(_value(author["name"], f"about {MARKER}"), to=[bob["name"]])
        # erin answered the message but was not one of its recipients (her reply is addressed to nobody who is connected). It is written
        # FIRST, so the bridge has handled its notification by the time bob's frame for the message arrives, and the counts below are exact.
        _insert_msg(owner, erin, dict(_v1(["nobody-connected"], sender=erin["name"]), in_reply_to=key))
        _write(app_conn, author, _msg(MESH, key, value))
        m1 = await _mesh(socks["bob"])
        assert m1["key"] == key and not {"version", "edited_by", "edited_at"} & set(m1), "control: an unedited message says nothing of edits"
        before = bridge.stats()

        _edit(app_conn, sibling, key, dict(value, body=f"the corrected text {MARKER}"), 1)
        head = await _mesh(socks["bob"])                      # the new head, as an ordinary message frame ...
        assert (head["key"], head["version"], head["edited_by"]) == (key, 2, sibling["fallback"]) and head["edited_at"].endswith("Z"), head
        events = {"bob": await _frame(socks["bob"]), "erin": await _frame(socks["erin"])}     # ... then the event, to bob and to erin
        for who, role in (("bob", "to"), ("erin", "replied")):
            event = events[who]
            assert set(event) == EVENT_FIELDS, (who, event)
            assert (event["type"], event["key"], event["version"], event["edited_by"], event["role"]) == (
                "mesh_edit", key, 2, sibling["fallback"], role), (who, event)
            assert event["edited_at"] == head["edited_at"]
            assert MARKER not in json.dumps(event)

        # A message to everyone written AFTER the edit is the first frame of every peer the event was not for.
        after = _insert_msg(owner, zed, _v1(["all"]))
        assert (await _mesh(socks["dave"]))["key"] == after, "dave was sent the event"
        for who in ("bob", "erin"):
            assert (await _mesh(socks[who]))["key"] == after, "control: the marker reached the recipients too, after the event"

        assert _frames_delta(before, bridge.stats()) == {
            "edit:notifications": 1,
            "edit:frames": 2,                          # bob (to) and erin (a reply author on the record)
            "edit:skipped:not_addressed": 1,           # dave: may read the namespace, not addressed, no reply
            "mesh:notifications": 2,                   # the edited row, and the marker
            "mesh:frames": 4,                          # the new head to bob, and the marker to bob, dave and erin
            "mesh:skipped:not_addressed": 2,           # the new head is not for dave or erin (the reply author is told by the EVENT)
        }
    finally:
        for comm in socks.values():
            await comm.disconnect()


@pytest.mark.asyncio
async def test_a_rolled_back_edit_is_never_announced(owner, owner_conn, app_conn, bridge, roster):
    """The record's insert and the NOTIFY are in the edit's own transaction, so a rollback leaves no record and no frame. The edit's rows are
    made the way the handler makes them (retire the head, insert the next version with the path's mark, then the record) on a connection that
    is not autocommit, and the transaction is rolled back. CONTROL: a message written afterwards is the next frame."""
    author, sibling = _writers(owner_conn, "pr")
    bob = _peer(owner, "bob")
    _grant(owner, bob, "read")
    comm = await _connect(bob)
    try:
        assert await bridge.wait_listening(10)
        key = _key(author["name"])
        value = dict(_value(author["name"], "for bob"), to=[bob["name"]])
        _write(app_conn, author, _msg(MESH, key, value))
        assert (await _mesh(comm))["key"] == key
        head = _head(owner_conn, MESH, key)
        before = bridge.stats()
        # The edit's rows, made as the handler makes them, in ONE transaction that is rolled back. `rls.server_internal` cannot be used for
        # this: it opens (and COMMITS) a transaction of its own, so the settings it would set are set here, transaction-locally.
        p = sr.payload(sibling["peer_id"], head["namespace"], head["key"], dict(value, body="an edit that will not stand"),
                       skill_name="memory.edit", extra_args={"expected_head_version": head["version"]})
        cols = sr.columns(p, peer_id=sibling["peer_id"], owner_id=sibling["owner_id"], version=head["version"] + 1,
                          parent_id=head["id"], author_label=sibling["fallback"])
        with psycopg.connect(DSN) as tx:                 # NOT autocommit: one transaction
            with tx.cursor() as cur:
                cur.execute("SELECT set_config('app.server_internal', '1', true)")
                cur.execute("SELECT set_config('app.mesh_edit_owner', %s, true)", (str(author["owner_id"]),))
                cur.execute("UPDATE shared_memory SET is_head = FALSE WHERE id = %s", (head["id"],))
                sr.insert(tx, cols)
                cur.execute("INSERT INTO shared_memory_edit (memory_id, namespace, key, version, prior_version, edited_by_peer_id, "
                            "edited_by_owner_id) SELECT id, namespace, key, version, version - 1, %s, %s FROM shared_memory "
                            "WHERE namespace = %s AND key = %s AND version = 2",
                            (sibling["peer_id"], sibling["owner_id"], MESH, key))
                assert cur.rowcount == 1, "control: the record was written inside the transaction"
            tx.rollback()
        assert owner.execute("SELECT count(*) FROM shared_memory WHERE key = %s", (key,)).fetchone()[0] == 1, "the new version stayed"
        marker = _insert_msg(owner, author, _v1([bob["name"]], sender=author["name"]))
        assert (await _mesh(comm))["key"] == marker, "the rolled-back edit produced a frame"
        assert "edit:notifications" not in _frames_delta(before, bridge.stats())
        assert owner.execute("SELECT count(*) FROM shared_memory_edit r JOIN shared_memory sm ON sm.id = r.memory_id "
                             "WHERE sm.key = %s", (key,)).fetchone()[0] == 0
    finally:
        await comm.disconnect()


@pytest.mark.asyncio
async def test_a_forged_notification_cannot_put_text_on_the_wire(owner, owner_conn, app_conn, bridge, roster):
    """NOTIFY is unauthenticated. A forged payload naming no record, a payload that is not JSON, and one that carries fields of its own produce no
    frame; one naming a REAL record re-announces exactly what the record and its row hold, never the payload's fields. The control is the message
    written after them, which is the next frame."""
    author, sibling = _writers(owner_conn, "pf")
    bob = _peer(owner, "bob")
    _grant(owner, bob, "read")
    comm = await _connect(bob)
    try:
        assert await bridge.wait_listening(10)
        key = _key(author["name"])
        value = dict(_value(author["name"], MARKER), to=[bob["name"]])
        _write(app_conn, author, _msg(MESH, key, value))
        assert (await _mesh(comm))["key"] == key
        _edit(app_conn, sibling, key, dict(value, body="an edit"), 1)
        assert (await _mesh(comm))["key"] == key                       # the new head
        genuine = await _frame(comm)
        assert genuine["type"] == "mesh_edit" and genuine["key"] == key

        before = bridge.stats()
        forged = {"id": str(_record_id(owner, key)), "key": "msg/forged", "version": 99, "role": "cc", "edited_by": "forged-label",
                  "body": "forged-text"}
        owner.execute("SELECT pg_notify(%s, %s)", (EDIT_NOTIFY_CHANNEL, json.dumps(forged)))
        renudge = await _frame(comm)
        assert renudge == genuine, f"the frame took the forged payload's fields: {renudge}"

        owner.execute("SELECT pg_notify(%s, %s)", (EDIT_NOTIFY_CHANNEL, json.dumps(dict(forged, id=str(uuid.uuid4())))))
        owner.execute("SELECT pg_notify(%s, %s)", (EDIT_NOTIFY_CHANNEL, "this is not json"))
        marker = _insert_msg(owner, author, _v1([bob["name"]], sender=author["name"]))
        assert (await _mesh(comm))["key"] == marker, "a notification for no such record made a frame"
        delta = _frames_delta(before, bridge.stats())
        assert delta["edit:notifications"] == 3 and delta["edit:record_not_found"] == 1
        assert delta["edit:undecodable"] == 1 and delta["edit:frames"] == 1, delta
    finally:
        await comm.disconnect()


@pytest.mark.asyncio
async def test_a_suspended_recipient_and_a_recipient_without_read_are_not_told(owner, owner_conn, app_conn, bridge, roster):
    """The two refusals a message frame has (mesh_notify.resolve_recipients) hold for the event too. bob is addressed, active and may read: told
    (the control). gus is addressed and may read but was suspended after connecting: not told. hal is addressed and connected but holds no read on
    the namespace: not told. The bridge counts each refusal by its reason."""
    from django.test import override_settings

    author, sibling = _writers(owner_conn, "ps")
    bob, gus, hal = (_peer(owner, t) for t in ("bob", "gus", "hal"))
    for p in (bob, gus):
        _grant(owner, p, "read")
    socks = {}
    held_off = override_settings(MESHY_WS_REVOCATION_SWEEP_SECS=3600.0)      # the standing sweep would close gus's socket first
    held_off.enable()
    try:
        for name, p in (("bob", bob), ("gus", gus), ("hal", hal)):
            socks[name] = await _connect(p)
        assert await bridge.wait_listening(10)
        key = _key(author["name"])
        value = dict(_value(author["name"], MARKER), to=[bob["name"], gus["name"], hal["name"]])
        _write(app_conn, author, _msg(MESH, key, value))
        assert (await _mesh(socks["bob"]))["key"] == key, "control: bob received the message"
        assert (await _mesh(socks["gus"]))["key"] == key, "control: gus was addressed and could read it"
        owner.execute("UPDATE peers SET status = 'suspended' WHERE id = %s", (gus["peer_id"],))
        before = bridge.stats()

        _edit(app_conn, sibling, key, dict(value, body="an edit"), 1)
        assert (await _mesh(socks["bob"]))["key"] == key                       # the new head
        frame = await _frame(socks["bob"])
        assert (frame["type"], frame["key"], frame["role"]) == ("mesh_edit", key, "to"), frame
        for who in ("gus", "hal"):
            assert await socks[who].receive_nothing(timeout=1.0), f"{who} was sent the event"
        delta = _frames_delta(before, bridge.stats())
        assert delta.get("edit:notifications") == 1, delta
        assert delta.get("edit:skipped:inactive") == 1, delta
        assert delta.get("edit:skipped:no_read:E_PERMISSION_DENIED") == 1, delta
        assert delta.get("edit:frames") == 1, delta
    finally:
        for comm in socks.values():
            await comm.disconnect()
        held_off.disable()


@pytest.mark.asyncio
async def test_the_seat_that_edited_is_not_told_of_its_own_edit_though_the_row_was_addressed_to_it(owner, owner_conn, app_conn, bridge, roster):
    """`resolve_edit_recipients` skips the seat that EDITED, not the row's author (another seat of the author's owner may be the editor). Nothing
    else keeps the event from it when the row names it: `to` holds both bob and the editor's own label, so the editor is a recipient by the rule
    a message frame uses, and only that skip stops the event. CONTROL: bob, the other recipient of the same edit, is told, and the editor's seat
    did receive the message before (it is one of its recipients). The editor's silence is proved by the message written afterwards being the
    next frame of its socket."""
    author, sibling = _writers(owner_conn, "pe")
    bob = _peer(owner, "bob")
    _grant(owner, bob, "read")
    socks = {}
    try:
        socks["bob"] = await _connect(bob)
        socks["sibling"] = await _connect(sibling)
        assert await bridge.wait_listening(10)
        key = _key(author["name"])
        value = dict(_value(author["name"], f"for bob and for the seat that will edit it {MARKER}"), to=[bob["name"], sibling["name"]])
        _write(app_conn, author, _msg(MESH, key, value))
        assert (await _mesh(socks["bob"]))["key"] == key
        assert (await _mesh(socks["sibling"]))["key"] == key, "control: the seat that will edit is one of the row's recipients"
        before = bridge.stats()

        _edit(app_conn, sibling, key, dict(value, body=f"corrected by the sibling {MARKER}"), 1)
        head = await _mesh(socks["bob"])                                   # the new head, then the event, to bob
        assert (head["key"], head["version"], head["edited_by"]) == (key, 2, sibling["fallback"]), head
        told = await _frame(socks["bob"])
        assert (told["type"], told["key"], told["role"], told["edited_by"]) == ("mesh_edit", key, "to", sibling["fallback"]), told

        # A message to everyone written AFTER the edit is the first frame of a peer the event was not for.
        after = _insert_msg(owner, author, _v1(["all"], sender=author["name"]))
        assert (await _mesh(socks["sibling"]))["key"] == after, "the seat that edited was sent the event"
        assert (await _mesh(socks["bob"]))["key"] == after, "control: bob's next frame after the event is the marker"

        delta = {k: v for k, v in _frames_delta(before, bridge.stats()).items() if k.startswith("edit:")}
        assert delta == {"edit:notifications": 1, "edit:frames": 1, "edit:skipped:editor": 1}, delta
    finally:
        for comm in socks.values():
            await comm.disconnect()


def test_the_three_pointer_payloads_parse_the_same_way_each_under_its_own_field():
    """A redaction's, an edit's and a presence row's NOTIFY carry one uuid and nothing else (the payload is
    unauthenticated, so only a pointer is read): each parse answers that uuid under ITS field, and None for anything
    that is not one, never raising, so a forged payload is counted `undecodable` and the listener goes on."""
    from apps.wsgw import mesh_notify

    rid = uuid.uuid4()
    parses = {"id": (mesh_notify.parse_redaction_payload, mesh_notify.parse_edit_payload),
              "peer_id": (mesh_notify.parse_presence_payload,)}
    for field, funcs in parses.items():
        other = "peer_id" if field == "id" else "id"
        for parse in funcs:
            assert parse(json.dumps({field: str(rid)})) == rid, parse
            assert parse(json.dumps({field: str(rid).upper(), "extra": 1})) == rid, parse
            for raw in ("not json", "null", "[]", '"%s"' % rid, json.dumps({other: str(rid)}),
                        json.dumps({field: None}), json.dumps({field: 5}), json.dumps({field: [str(rid)]}),
                        json.dumps({field: "not-a-uuid"})):
                assert parse(raw) is None, (parse, raw)
