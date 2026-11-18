"""Telling the seats that hold a message that was EDITED (divergence 059; docs/design/edit.md section 4 and tests 12, 13 and 17; the operator's
ruling ASDLC D-2026-09-29-U14, extended to edits by memory-bxxxxx's review: a seat that holds the old text must hear that it changed).

An edit is a new signed version, and it is announced the way a redaction is (tests/test_redaction_delivery.py is the model): the record
(`shared_memory_edit`) draws its own position in the commit order (`mesh_commit_order.kind = 'edit'`, sql_meshychat/029), so an addressed wait on
the commit order finds it after whatever cursor a seat saved, a listener that was down included, exactly once. The frame is

    {"type": "mesh_edit", "key": K, "version": V, "edited_at": T, "edited_by": L, "role": R}

with no text. Unlike a redaction's, it carries the RECIPIENT's role: `to`, `cc`, `broadcast`, or `replied` for the author of a direct reply that
was not among the row's recipients. The same wait also returns the ordinary message frame for the new head (its own `write` position), which now
says `version`, `edited_by` and `edited_at`, all three or none, from the record.

The edits here go through the handler (`memory.edit`, signed, as meshychat_app under row-level security): what is under test is the delivery, and the
handler is covered by tests/test_edit.py. Peers come from two families of helpers (the wait tests' readers, the authorship tests' signing seats);
they share one run database and one namespace.

Run:  .venv313/Scripts/python.exe -m pytest tests/test_edit_delivery.py -q -p no:cacheprovider
"""
from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path

import pytest
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_edit import _edit, _redact  # noqa: E402
from tests.test_mesh_authorship import (  # noqa: E402,F401
    MESH, _admit, _key, _msg, _peer as _apeer, _value, _write, app_conn, owner_conn,
)
from tests.test_mesh_wait_addressed import (  # noqa: E402,F401
    NS, _anchor, _grant, _pair, _peer, _row, client, mesh_settings, no_roster, owner,
)
from tests.test_redaction_delivery import _frames  # noqa: E402

EVENT_FIELDS = {"type", "key", "version", "edited_at", "edited_by", "role"}
EDIT_FIELDS = {"version", "edited_by", "edited_at"}


def _events(frames, type_="mesh_edit", key=None):
    return [f for f in frames if f.get("type") == type_ and (key is None or f["key"] == key)]


def _world(owner, owner_conn, app_conn, tag, *, to=None, cc=None):
    """(the mesh's owner, a reader, an author, a sibling seat of the author's owner, the key, the value): the message is NOT written yet. The
    reader may read the namespace and, unless `to` says otherwise, is who the message is for."""
    mesh, _sender, reader = _pair(owner)
    author = _apeer(owner_conn, tag + "a")
    _admit(owner_conn, MESH, author)
    sibling = _apeer(owner_conn, tag + "s", same_owner_as=author)
    _admit(owner_conn, MESH, sibling)
    key = _key(author["name"])
    value = dict(_value(author["name"], "the original body"), to=[reader["name"]] if to is None else to)
    if cc is not None:
        value["cc"] = cc
    return mesh, reader, author, sibling, key, value


def _record_time(owner, table, key, version, column):
    return owner.execute(
        f"SELECT to_char(r.{column} AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') FROM {table} r "
        "JOIN shared_memory sm ON sm.id = r.memory_id WHERE sm.key = %s AND sm.version = %s", (key, version)).fetchone()[0]


def test_12_a_recipient_is_told_of_an_edit_once_after_the_new_head_and_the_event_holds_no_text(client, owner, owner_conn, app_conn):
    mesh, reader, author, sibling, key, value = _world(owner, owner_conn, app_conn, "d1")
    since = _anchor(client, reader)
    _write(app_conn, author, _msg(MESH, key, value))
    got, at = _frames(client, reader, since)
    assert [(f["type"], f["key"]) for f in got] == [("mesh_message", key)], got
    assert not EDIT_FIELDS & set(got[0]), "control: a message that was never edited says nothing of edits"

    marker = f"zq-edit-delivery-{uuid.uuid4().hex}"
    _edit(app_conn, sibling, key, dict(value, body=f"the corrected body {marker}"), 1)
    after, cursor = _frames(client, reader, at)
    assert [f["type"] for f in after] == ["mesh_message", "mesh_edit"], after          # the new head, then the edit, in commit order
    head, event = after
    assert (head["key"], head["version"], head["edited_by"]) == (key, 2, sibling["fallback"]) and head["edited_at"].endswith("Z"), head
    assert set(event) == EVENT_FIELDS, event
    assert (event["key"], event["version"], event["edited_by"], event["role"]) == (key, 2, sibling["fallback"], "to"), event
    assert event["edited_at"] == head["edited_at"] == _record_time(owner, "shared_memory_edit", key, 2, "edited_at")
    assert marker not in json.dumps(event), "the event carried the text"
    again, _ = _frames(client, reader, cursor)
    assert again == [], "the edit was delivered twice"


def test_12_a_cc_and_a_broadcast_reader_are_told_each_in_their_own_role(client, owner, owner_conn, app_conn):
    mesh, reader, author, sibling, key, value = _world(owner, owner_conn, app_conn, "d2", to=["all"])
    cc = _peer(owner, "cc")
    _grant(owner, NS, cc, mesh, permission="read")
    value["cc"] = [cc["name"]]
    since = {"cc": _anchor(client, cc), "reader": _anchor(client, reader)}
    _write(app_conn, author, _msg(MESH, key, value))
    _edit(app_conn, sibling, key, dict(value, body="an edit"), 1)
    for who, peer, role in (("cc", cc, "cc"), ("reader", reader, "broadcast")):
        frames, _ = _frames(client, peer, since[who])
        (event,) = _events(frames, key=key)
        assert event["role"] == role and event["version"] == 2, (who, event)


def test_12_the_editor_and_a_seat_the_row_was_not_for_are_not_told(client, owner, owner_conn, app_conn):
    """The row is a broadcast, so the editor (a reader of it) WOULD be told if the rule did not exclude it; the author's other seat is told, as a
    redaction's author is (only the seat that acted knows). A reader the row was addressed away from is not told. CONTROLS: the same events reach
    the seats they are for."""
    mesh, reader, author, sibling, key, value = _world(owner, owner_conn, app_conn, "d3", to=["all"])
    away = _peer(owner, "away")
    _grant(owner, NS, away, mesh, permission="read")
    since = {n: _anchor(client, p) for n, p in (("author", author), ("sibling", sibling), ("reader", reader))}
    _write(app_conn, author, _msg(MESH, key, value))
    _edit(app_conn, sibling, key, dict(value, body="an edit"), 1)
    reader_frames, _ = _frames(client, reader, since["reader"])
    assert len(_events(reader_frames, key=key)) == 1, "control: a broadcast reader is told"
    author_frames, _ = _frames(client, author, since["author"])
    assert len(_events(author_frames, key=key)) == 1, "the author's other seat is a reader of its own broadcast and is told"
    sibling_frames, _ = _frames(client, sibling, since["sibling"])
    assert _events(sibling_frames, key=key) == [], "the seat that edited was told of its own act"

    # a row addressed to one reader, edited: a reader it was addressed away from hears nothing
    key2 = _key(author["name"])
    value2 = dict(_value(author["name"], "for one seat"), to=[reader["name"]])
    since_away, since_reader = _anchor(client, away), _anchor(client, reader)
    _write(app_conn, author, _msg(MESH, key2, value2))
    _edit(app_conn, sibling, key2, dict(value2, body="edited, still for one seat"), 1)
    away_frames, _ = _frames(client, away, since_away)
    assert _events(away_frames) == [], f"a seat the row was not for was told: {away_frames}"
    assert len(_events(_frames(client, reader, since_reader)[0], key=key2)) == 1, "control: the addressed seat is told"


def test_22_the_author_of_a_reply_is_told_in_the_role_replied_though_not_a_recipient(client, owner, owner_conn, app_conn):
    mesh, reader, author, sibling, key, value = _world(owner, owner_conn, app_conn, "d4")
    replier = _peer(owner, "reply")
    _grant(owner, NS, replier, mesh, permission="write")
    bystander = _peer(owner, "by")
    _grant(owner, NS, bystander, mesh, permission="read")
    since = {n: _anchor(client, p) for n, p in (("replier", replier), ("bystander", bystander))}
    _write(app_conn, author, _msg(MESH, key, value))
    _row(owner, replier, {"to": reader["name"], "in_reply_to": key, "body": "a reply"})
    seen, _ = _frames(client, replier, since["replier"])
    assert key not in {f.get("key") for f in seen if f["type"] == "mesh_message"}, "control: the replier was not a recipient of the row"

    _edit(app_conn, sibling, key, dict(value, body="an edit the replier should hear of"), 1)
    told, cursor = _frames(client, replier, since["replier"])
    (event,) = _events(told, key=key)
    assert event["role"] == "replied" and event["version"] == 2, event
    assert _events(_frames(client, replier, cursor)[0], key=key) == [], "exactly once"
    assert _events(_frames(client, bystander, since["bystander"])[0]) == [], "a reader with no reply and no address was told"


def test_13_a_listener_that_was_down_receives_the_edit_exactly_once_from_its_saved_cursor(client, owner, owner_conn, app_conn):
    mesh, reader, author, sibling, key, value = _world(owner, owner_conn, app_conn, "d5")
    since = _anchor(client, reader)
    _write(app_conn, author, _msg(MESH, key, value))
    got, saved = _frames(client, reader, since)               # the listener read the message, saved its cursor, went down
    assert [f["key"] for f in got] == [key]
    _edit(app_conn, sibling, key, dict(value, body="edited while it was down"), 1)
    back, cursor = _frames(client, reader, saved)              # it comes back from the saved cursor
    assert [f["key"] for f in _events(back)] == [key], back
    assert _events(_frames(client, reader, cursor)[0], key=key) == [], "delivered twice"
    replay, _ = _frames(client, reader, saved)                 # a client that lost the newer cursor sees it again: at-least-once
    assert len(_events(replay, key=key)) == 1, "the position is stable: the same cursor answers the same event"


def test_an_edit_of_a_version_that_is_no_longer_the_head_is_still_announced_with_its_own_time(client, owner, owner_conn, app_conn):
    """Two edits, then the first edit's version is redacted. The listener that was down is owed the event of EACH: the edit of version 2 (a row
    that is no longer the head, so it is not a message frame any more), the edit of version 3, and the redaction of version 2, in commit order.
    The edit of version 2 keeps ITS record's time and seats although a redaction record of the same row now exists (each column takes its record
    by the position's kind, not by which join answered). CONTROL: the one message frame is the head."""
    mesh, reader, author, sibling, key, value = _world(owner, owner_conn, app_conn, "d6")
    since = _anchor(client, reader)
    _write(app_conn, author, _msg(MESH, key, value))
    _edit(app_conn, sibling, key, dict(value, body="second"), 1)
    time.sleep(1.2)                                            # the seconds of the two records must differ for the check below to mean something
    _edit(app_conn, author, key, dict(value, body="third"), 2)
    time.sleep(1.2)
    _redact(app_conn, author, key, version=2)
    frames, _ = _frames(client, reader, since)
    kinds = [(f["type"], f.get("version")) for f in frames]
    assert kinds == [("mesh_edit", 2), ("mesh_message", 3), ("mesh_edit", 3), ("mesh_redaction", 2)], kinds
    e2, m3, e3, r2 = frames
    assert e2["edited_at"] == _record_time(owner, "shared_memory_edit", key, 2, "edited_at") and e2["edited_by"] == sibling["fallback"], e2
    assert e3["edited_at"] == _record_time(owner, "shared_memory_edit", key, 3, "edited_at") and e3["edited_by"] == author["fallback"], e3
    assert r2["redacted_at"] == _record_time(owner, "shared_memory_redaction", key, 2, "redacted_at"), r2
    assert e2["edited_at"] != r2["redacted_at"], "control: the edit's time and the redaction's are different seconds"
    assert (m3["version"], m3["edited_by"]) == (3, author["fallback"]), m3


def test_the_embed_walk_reads_the_write_of_an_edited_version_and_never_its_edit_position(owner, owner_conn, app_conn):
    """The embed walk counts every row it reads and moves its watermark past it (apps/meshsem/embedjob.py). An edit's new row has its `write`
    position, walked once; the record's `edit` position is a second visit to the same row, and the walk's `kind = 'write'` skips it."""
    from apps.meshsem import embedjob

    mesh, reader, author, sibling, key, value = _world(owner, owner_conn, app_conn, "d7", to=["all"])
    _write(app_conn, author, _msg(MESH, key, value))
    _edit(app_conn, sibling, key, dict(value, body="an edit the walk must count once"), 1)
    seq = dict(owner.execute(
        "SELECT sm.version, o.seq FROM mesh_commit_order o JOIN shared_memory sm ON sm.id = o.memory_id "
        "WHERE sm.key = %s AND o.kind = 'write'", (key,)).fetchall())
    edit_seq = owner.execute("SELECT o.seq FROM mesh_commit_order o JOIN shared_memory sm ON sm.id = o.memory_id "
                             "WHERE sm.key = %s AND o.kind = 'edit'", (key,)).fetchone()[0]
    assert edit_seq > seq[2] > seq[1], (seq, edit_seq)

    def walked(after):
        with owner.cursor(row_factory=dict_row) as cur:
            cur.execute(embedjob._SELECT_ROWS, {"ns": NS, "after_seq": after, "limit": 100})
            return [(r["key"], r["version"]) for r in cur.fetchall()]

    assert walked(seq[1]) == [(key, 2)], "control: the write of the head (version 2) is walked, once"
    assert walked(seq[2]) == [], "the walk visited the edit position"


def test_a_parked_addressed_wait_is_woken_by_the_edit_notification_and_by_the_redaction_notification(client, owner, owner_conn, app_conn,
                                                                                                        monkeypatch):
    """The event is a row of the commit order that only an addressed wait reads, so the record's own NOTIFY (`meshy_mesh_edit`,
    `meshy_mesh_redaction`) must wake a parked one (apps/mesh/wake.py). The poll interval is made longer than the wait (30 s against 10),
    so only a wake can answer early. Each case is built so that NOTHING ELSE wakes the waiter: the edit's new row is addressed to someone
    else, so its own mesh NOTIFY (which wakes the seats a row is for) does not wake the reply author; and a redaction is an UPDATE, which sql/036
    never fires on. (The redaction's wake had no test of its own before this one; the two share the mechanism, so it is proved here.)"""
    from tests.test_mesh_wait_addressed import WAIT
    from tests.test_mesh_wait_nudge import _park_in_thread, _subscriber_if_any, _until

    wake = _subscriber_if_any()
    monkeypatch.setattr(WAIT, "POLL_SECS", 30.0)
    mesh, reader, author, sibling, key, value = _world(owner, owner_conn, app_conn, "d8")
    replier = _peer(owner, "wake")
    _grant(owner, NS, replier, mesh, permission="write")
    since_reply, since_reader = _anchor(client, replier), _anchor(client, reader)
    _write(app_conn, author, _msg(MESH, key, value))
    _row(owner, replier, {"to": reader["name"], "in_reply_to": key, "body": "a reply"})
    at_reply = _frames(client, replier, since_reply)[1]                 # the replier's cursor is past everything so far
    at_reader = _frames(client, reader, since_reader)[1]

    # the edit: only the replier is parked, and the row it wrote is not addressed to it
    t, result = _park_in_thread(replier["peer_id"], since=at_reply, timeout=10)
    time.sleep(0.8)                                                      # parked: its first read found nothing
    _edit(app_conn, sibling, key, dict(value, body="an edit that must wake a parked wait"), 1)
    t.join(15)
    assert not t.is_alive(), "the parked wait never returned"
    r = result["r"]
    assert r.status_code == 200, (r.status_code, r.content[:300])
    got = json.loads(r.content)["messages"]
    assert [(f["type"], f.get("role")) for f in got] == [("mesh_edit", "replied")], got
    assert result["took"] < 5.0, f"answered at {result['took']:.2f}s: the edit notification did not wake it"
    assert _until(lambda: wake.REGISTRY.count() == 0, 5.0), wake.REGISTRY.count()

    # the redaction: the reader is parked; a redaction is an UPDATE, so only its own notification can wake it
    at_reader = _frames(client, reader, at_reader)[1]                    # past the edit's frames
    t, result = _park_in_thread(reader["peer_id"], since=at_reader, timeout=10)
    time.sleep(0.8)
    _redact(app_conn, author, key, version=2)
    t.join(15)
    assert not t.is_alive(), "the parked wait never returned"
    r = result["r"]
    assert r.status_code == 200, (r.status_code, r.content[:300])
    assert [f["type"] for f in json.loads(r.content)["messages"]] == ["mesh_redaction"], r.content[:300]
    assert result["took"] < 5.0, f"answered at {result['took']:.2f}s: the redaction notification did not wake it"
    assert _until(lambda: wake.REGISTRY.count() == 0, 5.0), wake.REGISTRY.count()
