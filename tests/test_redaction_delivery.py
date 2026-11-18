"""Telling the seats that hold a redacted mail row (divergence 053; docs/design/redaction.md sections
5a and 6, tests 10, 18 and 22; the operator's rulings ASDLC D-2026-09-29-U13: `to` and `cc` stay,
reply authors are notified).

A redaction blanks the server's copies. The copies OUTSIDE the server (each seat's postbox spool holds
a preview line of every mail it received) are reached only if the seat is told, so the redaction is
announced as an EVENT that carries the key and never the text:

    {"type": "mesh_redaction", "key": K, "version": V, "redacted_at": T, "reason_code": R}

It travels the way a message does. Durably: the redaction draws its own position in the commit order
(`mesh_commit_order.kind = 'redaction'`, sql_meshychat/027), so an addressed wait on the commit order
finds it after whatever cursor the seat saved, a listener that was down included, exactly once.
Live: a NOTIFY wakes the parked waits and the push bridge (tests/test_redaction_push.py).

WHO. The row's own recipients, decided by the rule a message frame uses over the tombstone's `to` and
`cc` (kept by the redaction) or a broadcast; and the authors of the row's direct replies, named on the
record at the time. Never the seat that redacted.

The DB effects of a redaction are made here by `redact._redact_one`, the handler's own function, on a
row this module writes the way tests/test_mesh_wait_addressed.py writes mail: what is under test is
the delivery, and the handler is covered by tests/test_redaction.py.

Run:  .venv313/Scripts/python.exe -m pytest tests/test_redaction_delivery.py -q -p no:cacheprovider
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_mesh_wait_addressed import (  # noqa: E402,F401
    DSN, NS, WAIT, _anchor, _pair, _peer, _grant, _row, _wait, client, mesh_settings, no_roster, owner,
)

MARKER = "zq-delivery-marker-51d0"
EVENT_FIELDS = {"type", "key", "version", "redacted_at", "reason_code"}


def _redact_in_db(owner, row, redactor, *, reason="client_name", notify=(), blank_thread=False) -> dict:
    """The redaction of `row` (as `_row` returns it), by `redactor`, with the handler's own `_redact_one`, committed."""
    from apps.memoryskills import redact
    with owner.transaction():
        cur = owner.cursor(row_factory=dict_row)
        cur.execute("SELECT id, version, value, signed_bytes, payload_jcs_sha256, created_by_peer_id "
                    "FROM shared_memory WHERE id = %s", (row["id"],))
        found = cur.fetchone()
        return redact._redact_one(cur, found, NS, row["key"], redactor["peer_id"], redactor["owner_id"],
                                  reason, blank_thread, b"the request as signed", f"call-{uuid.uuid4()}",
                                  [str(p) for p in notify])


def _frames(client, peer, since):
    """(every frame answered after `since`, the cursor to resume from), page by page as the monitor reads."""
    out, at = [], since
    for _ in range(50):
        r, _t = _wait(client, peer["peer_id"], since=at, timeout=0)
        if r.status_code == 204:
            return out, r["X-Meshy-Cursor"]
        assert r.status_code == 200, (r.status_code, r.content[:300])
        body = json.loads(r.content)
        out += body["messages"]
        at = body["cursor"]
    raise AssertionError("the addressed wait never answered 204")


def _events(frames, key=None):
    return [f for f in frames if f.get("type") == "mesh_redaction" and (key is None or f["key"] == key)]


def test_10_a_recipient_is_told_and_the_event_names_the_row_and_holds_no_text(client, owner):
    mesh, sender, receiver = _pair(owner)
    since = _anchor(client, receiver)
    row = _row(owner, sender, {"to": receiver["name"], "body": f"text {MARKER} text", "subject": MARKER})
    got, at = _frames(client, receiver, since)
    assert [f["key"] for f in got if f["type"] == "mesh_message"] == [row["key"]], "control: the message was delivered"

    record = _redact_in_db(owner, row, sender)
    after, _ = _frames(client, receiver, at)
    told = _events(after, row["key"])
    assert len(told) == 1, after
    event = told[0]
    assert set(event) == EVENT_FIELDS, event
    assert event["version"] == 1 and event["reason_code"] == "client_name"
    assert event["redacted_at"] == record["redacted_at"][:19] + "Z", (event, record["redacted_at"])   # the record's instant, whole seconds, Z
    assert MARKER not in json.dumps(after), "the event carried the text"
    assert [f for f in after if f.get("type") == "mesh_message"] == [], "the redaction re-announced the message"


def test_10_a_seat_the_row_was_not_for_is_not_told_and_neither_is_the_seat_that_redacted(client, owner):
    mesh, sender, receiver = _pair(owner)
    other = _peer(owner, "other")
    _grant(owner, NS, other, mesh, permission="read")
    since_recv, since_other, since_sender = (_anchor(client, p) for p in (receiver, other, sender))
    row = _row(owner, sender, {"to": receiver["name"], "body": MARKER})
    _redact_in_db(owner, row, sender)

    recv, _ = _frames(client, receiver, since_recv)
    assert len(_events(recv, row["key"])) == 1, "control: the addressed seat is told"
    stranger, _ = _frames(client, other, since_other)
    assert _events(stranger) == [] or row["key"] not in {e["key"] for e in _events(stranger)}, "a seat the row was not for was told"
    redactor, _ = _frames(client, sender, since_sender)
    assert row["key"] not in {e["key"] for e in _events(redactor)}, "the seat that redacted was told of its own act"


def test_10_a_cc_recipient_and_a_broadcast_reader_are_told(client, owner):
    mesh, sender, receiver = _pair(owner)
    cc, everyone = _peer(owner, "cc"), _peer(owner, "all")
    for p in (cc, everyone):
        _grant(owner, NS, p, mesh, permission="read")
    since = {p["name"]: _anchor(client, p) for p in (cc, everyone)}
    addressed = _row(owner, sender, {"to": receiver["name"], "cc": [cc["name"]], "body": MARKER})
    broadcast = _row(owner, sender, {"to": "all", "body": MARKER})
    _redact_in_db(owner, addressed, sender)
    _redact_in_db(owner, broadcast, sender)
    cc_frames, _ = _frames(client, cc, since[cc["name"]])
    assert addressed["key"] in {e["key"] for e in _events(cc_frames)}, "a cc'd seat is told"
    all_frames, _ = _frames(client, everyone, since[everyone["name"]])
    assert {addressed["key"]} & {e["key"] for e in _events(all_frames)} == set(), "control: not addressed to this seat"
    assert broadcast["key"] in {e["key"] for e in _events(all_frames)}, "a broadcast's reader is told"


def test_18_a_listener_that_was_down_receives_the_event_exactly_once(client, owner):
    mesh, sender, receiver = _pair(owner)
    since = _anchor(client, receiver)
    row = _row(owner, sender, {"to": receiver["name"], "body": MARKER})
    got, saved = _frames(client, receiver, since)                 # the listener read the message, saved its cursor, went down
    assert row["key"] in {f["key"] for f in got}
    _redact_in_db(owner, row, sender)                             # while it was down
    back, cursor = _frames(client, receiver, saved)               # it comes back from the saved cursor
    assert [e["key"] for e in _events(back)] == [row["key"]], back
    again, _ = _frames(client, receiver, cursor)                  # and from the cursor it saved after that
    assert _events(again, row["key"]) == [], "the event was delivered twice"
    replay, _ = _frames(client, receiver, saved)                  # a client that lost the newer cursor sees it again: at-least-once
    assert len(_events(replay, row["key"])) == 1, "the position is stable: the same cursor answers the same event"


def test_22_the_author_of_a_reply_is_told_though_not_a_recipient(client, owner):
    mesh, sender, receiver = _pair(owner)
    replier = _peer(owner, "reply")
    _grant(owner, NS, replier, mesh, permission="write")
    since = _anchor(client, replier)
    row = _row(owner, sender, {"to": receiver["name"], "body": MARKER})
    _row(owner, replier, {"to": receiver["name"], "in_reply_to": row["key"], "body": "a reply"})
    seen_before, _ = _frames(client, replier, since)
    assert row["key"] not in {f["key"] for f in seen_before if f["type"] == "mesh_message"}, "control: the replier was not a recipient of the row"

    _redact_in_db(owner, row, sender, notify=[replier["peer_id"]])
    told, cursor = _frames(client, replier, since)
    assert [e["key"] for e in _events(told)] == [row["key"]], told
    again, _ = _frames(client, replier, cursor)
    assert _events(again, row["key"]) == [], "exactly once"
    # CONTROL: the same redaction without the reply author on the record does not tell them.
    row2 = _row(owner, sender, {"to": receiver["name"], "body": MARKER})
    _row(owner, replier, {"to": receiver["name"], "in_reply_to": row2["key"], "body": "another reply"})
    _redact_in_db(owner, row2, sender, notify=[])
    silent, _ = _frames(client, replier, cursor)
    assert row2["key"] not in {e["key"] for e in _events(silent)}, "told without being named on the record"


def test_a_version_that_is_not_the_head_is_still_announced(client, owner):
    """`version: all` redacts old versions, which are not heads; their events must not be dropped by the head filter that a message needs."""
    mesh, sender, receiver = _pair(owner)
    since = _anchor(client, receiver)
    row = _row(owner, sender, {"to": receiver["name"], "body": MARKER})
    with owner.cursor() as cur:            # retire the head as a later version would, so the row is no longer a head
        cur.execute("UPDATE shared_memory SET is_head = FALSE WHERE id = %s", (row["id"],))
    got, at = _frames(client, receiver, since)
    _redact_in_db(owner, row, sender)
    after, _ = _frames(client, receiver, at)
    assert [e["key"] for e in _events(after)] == [row["key"]], after


def _sibling(owner, of, tag) -> dict:
    """A second seat of `of`'s owner: the seat that may redact a row its owner wrote (the operator's ruling, U13)."""
    peer_id = uuid.uuid4()
    name = f"wa-{tag}-{peer_id.hex[:8]}"
    with owner.cursor() as cur:
        cur.execute("INSERT INTO peers (id, owner_id, name, display_name) VALUES (%s,%s,%s,%s)",
                    (peer_id, of["owner_id"], name, f"wa {tag}"))
    return {"peer_id": peer_id, "owner_id": of["owner_id"], "name": name, "label": name}


def test_10_a_seat_that_redacts_a_broadcast_it_could_read_is_not_told_of_its_own_act(client, owner):
    """The redacting seat is excluded even when the row was for it: a sibling seat of the author reads a broadcast
    (a message frame reaches it), then redacts it. The other reader is told; it is not. CONTROL: before the redaction
    the same seat WAS sent the message, so the exclusion is about the redaction and not about the row never reaching it."""
    mesh, sender, receiver = _pair(owner)
    sibling = _sibling(owner, sender, "sib")
    _grant(owner, NS, sibling, mesh, permission="write")
    since_sib, since_recv = _anchor(client, sibling), _anchor(client, receiver)
    row = _row(owner, sender, {"to": "all", "body": MARKER})
    before, at_sib = _frames(client, sibling, since_sib)
    assert row["key"] in {f["key"] for f in before if f["type"] == "mesh_message"}, "control: the message reached the sibling"

    _redact_in_db(owner, row, sibling)
    recv, _ = _frames(client, receiver, since_recv)
    assert row["key"] in {e["key"] for e in _events(recv)}, "control: another reader of the broadcast is told"
    sib, _ = _frames(client, sibling, at_sib)
    assert row["key"] not in {e["key"] for e in _events(sib)}, "the seat that redacted was told of its own act"


# ------------------------------------------------------------------------- `kind` for a legacy cursor (review of #32, point 3)

def test_a_redaction_position_is_not_a_new_row_for_a_legacy_cursor(owner):
    """`seq_after_legacy` and `legacy_position` answer "the rows after this cursor", and a redaction position is not one: a row redacted
    long after it was written is an OLD created_at at a NEW seq. If the translation counted it, the backwards walk (which stops at
    the first row stamped a settle window before the cursor) would stop on it and skip the rows between, and a summary window would
    open at that old instant. CONTROL: the same rows before the redaction give the same positions."""
    import datetime as _dt

    import psycopg

    from tests.test_mesh_delivery_sequence import _row_at, _seq_of

    mesh, sender, receiver = _pair(owner)
    hour_ago = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=1)
    old = _row_at(owner, sender, {"to": "nobody", "body": "an old row"}, hour_ago)
    mark = _row(owner, sender, {"to": "nobody"})
    after = _row(owner, sender, {"to": receiver["name"]})
    cursor = WAIT.Cursor(WAIT.micros_of(mark["created_at"]), mark["id"])
    since = WAIT.SeqCursor(_seq_of(owner, mark["id"]), cursor)

    def positions():
        with psycopg.connect(DSN) as conn:
            return WAIT.seq_after_legacy(conn, cursor), WAIT.legacy_position(conn, since)

    before = positions()
    assert before[0].seq < _seq_of(owner, after["id"]), "control: the translation lands before the row after the cursor"
    _redact_in_db(owner, {"id": old["id"], "key": old["key"]}, sender)
    top = owner.execute("SELECT max(seq) FROM mesh_commit_order").fetchone()[0]
    assert top > _seq_of(owner, after["id"]), "control: the redaction drew the highest position"
    assert positions() == before, "a redaction position moved a legacy cursor's translation"


def test_a_blanked_thread_moves_the_projected_thread_edge_and_only_this_rows(owner):
    """Blocking 1 of the review, on the path production takes. The edge is the PROJECTION's own (a real rebuild wrote it, not a seeded
    row), the redaction with `blank_thread_id` deletes it with the text, and the walk's second visit (the row's `redaction` position,
    which the projection reads on purpose: apps/mesh/projection.py) writes the edge the tombstone now has, toward `redacted/<record id>`.
    CONTROLS: the projection wrote the old thread's edge for both rows before, the sibling's edge is untouched, and a redaction
    WITHOUT blank_thread keeps the row's edge to its own thread."""
    from apps.mesh import projection as P

    mesh, sender, receiver = _pair(owner)
    thread = f"t/zqproj{uuid.uuid4().hex[:8]}"
    a, b, c = (_row(owner, sender, {"to": receiver["name"], "thread_id": thread, "body": "text"}) for _ in range(3))
    P.rebuild(owner)

    def edges(row):
        return [r[0] for r in owner.execute("SELECT dst FROM mesh_edges WHERE src_id = %s AND edge_type = 'in_thread'",
                                            (row["id"],)).fetchall()]

    assert edges(a) == edges(b) == edges(c) == [thread], "control: the projection wrote the thread edge of each row"
    _redact_in_db(owner, c, sender)                                       # no blanking: the thread stays for grouping
    P.catch_up(owner)
    assert edges(c) == [thread], "a redaction without blank_thread took the thread edge"

    record = _redact_in_db(owner, a, sender, blank_thread=True)
    assert record["thread_id_blanked"] is True
    assert edges(a) == [], "the edge to the old thread outlived the redaction"
    assert edges(b) == [thread], "the sibling's edge went too"
    walked = P.catch_up(owner)
    assert walked.rows_read == 1, walked
    assert edges(a) == [f"redacted/{record['redaction_id']}"], edges(a)
    assert edges(b) == [thread]
    assert thread not in [r[0] for r in owner.execute("SELECT dst FROM mesh_edges WHERE src_id = %s", (a["id"],)).fetchall()]


def test_redaction_removes_body_mentions_so_incremental_and_rebuild_agree(owner, monkeypatch):
    """MUTANT: omit the `mentions` delete in redact.delete_derived.

    The re-walk at the redaction position reads the tombstone, whose body is absent, so
    an incremental projection must agree with a rebuild for body-derived references.
    """
    from apps.mesh import graph as G
    from apps.mesh import projection as P
    from apps.core import rls

    # the gate open for the projection, and for the reads, which take it as an argument (5967913)
    monkeypatch.setattr("apps.meshsem.store.forbidden_rows_open", lambda: True)
    mesh, sender, receiver = _pair(owner)
    number = "53999"
    row = _row(owner, sender, {"to": receiver["name"], "body":
                               f"landed acme/secret#{number} at c0ffee1"})
    P.rebuild(owner)
    with rls.server_internal(owner):
        assert row["key"] in {h.hit.key for h in G.mentions(owner, f"#{number}", gate=True).hits}
    _redact_in_db(owner, row, sender, reason="client_name")
    assert P.catch_up(owner).rows_read == 1
    with rls.server_internal(owner):
        incremental = {h.hit.key for h in G.mentions(owner, f"#{number}", gate=True).hits}
        edges = owner.execute("SELECT edge_type, dst FROM mesh_edges WHERE src_id = %s", (row["id"],)).fetchall()
    assert row["key"] not in incremental
    assert not [edge for edge in edges if edge[0] == "mentions"], edges
    P.rebuild(owner)
    with rls.server_internal(owner):
        rebuilt = {h.hit.key for h in G.mentions(owner, f"#{number}", gate=True).hits}
    assert incremental == rebuilt


def test_redaction_keeps_closes_routing_but_drops_done_when_text(owner):
    """MUTANT: remove `closes` from ROUTING_FIELDS; rebuild reopens the settled ask."""
    from apps.mesh import graph as G
    from apps.mesh import projection as P
    from apps.core import rls

    mesh, asker, owing = _pair(owner)
    ask = _row(owner, asker, {"to": owing["name"], "requires_ack": True,
                               "done_when": "publish a receipt", "body": "please settle"})
    close = _row(owner, owing, {"to": asker["name"], "body": "settled", "closes": [ask["key"]]})
    P.rebuild(owner)
    _redact_in_db(owner, close, owing, reason="sent_in_error")
    P.catch_up(owner)

    def owed():
        with rls.server_internal(owner):
            return {item.hit.key: (item.state, item.done_when) for item in G.owed_acks(
                owner, owing["name"], limit=None, states=["open", "active", "answered", "closed"])}

    def values():
        with rls.server_internal(owner):
            return [owner.execute("SELECT value FROM shared_memory WHERE id = %s", (r["id"],)).fetchone()[0]
                    for r in (close, ask)]

    # The CLOSE is redacted; the ask it settled stays closed, incrementally and rebuilt.
    assert owed()[ask["key"]][0] == "closed"
    P.rebuild(owner)
    assert owed()[ask["key"]][0] == "closed"
    assert values()[0]["closes"] == [ask["key"]]

    # Then the ASK is redacted: it leaves owed (no one owes an answer to withdrawn text), and its
    # done_when, which is text, is gone from the tombstone.
    _redact_in_db(owner, ask, asker, reason="client_name")
    P.catch_up(owner)
    assert ask["key"] not in owed()
    P.rebuild(owner)
    assert ask["key"] not in owed()
    assert "done_when" not in values()[1]


def test_a_redacted_ask_leaves_owed_and_awaiting_incrementally_and_on_rebuild(owner):
    """A redacted ask is no longer owed by its recipient nor awaited by its author, and a rebuild agrees.
    MUTANT A: drop `asks=True` from _redact_one's delete_derived call; the incremental owed still lists the ask.
    MUTANT B: drop normalize's `redacted` guard on owes_ack; the rebuilt owed lists it again."""
    from apps.mesh import graph as G
    from apps.mesh import projection as P
    from apps.core import rls

    mesh, asker, owing = _pair(owner)
    ask = _row(owner, asker, {"to": owing["name"], "requires_ack": True, "body": "please answer"})
    kept = _row(owner, asker, {"to": owing["name"], "requires_ack": True, "body": "a second ask, not redacted"})
    P.rebuild(owner)

    def state():
        with rls.server_internal(owner):
            every = ["open", "active", "answered", "closed"]
            owed = {item.hit.key for item in G.owed_acks(owner, owing["name"], limit=None, states=every)}
            awaiting = {item.hit.key for item in G.awaiting(owner, asker["name"], limit=None, states=every)}
            edges = owner.execute("SELECT count(*) FROM mesh_edges WHERE src_id = %s AND edge_type = 'owes_ack'",
                                  (ask["id"],)).fetchone()[0]
        return owed, awaiting, edges

    owed, awaiting, edges = state()
    assert {ask["key"], kept["key"]} <= owed and {ask["key"], kept["key"]} <= awaiting and edges == 1
    _redact_in_db(owner, ask, asker, reason="sent_in_error")
    P.catch_up(owner)
    incremental = state()
    P.rebuild(owner)
    rebuilt = state()
    for owed, awaiting, edges in (incremental, rebuilt):
        assert ask["key"] not in owed and ask["key"] not in awaiting and edges == 0
        assert kept["key"] in owed and kept["key"] in awaiting
    assert incremental == rebuilt

def test_a_listener_that_starts_after_a_redaction_is_not_told_of_it(client, owner):
    """`end_position` and `_row_position` read every kind on purpose (review of #32, point 3): the end of the commit order INCLUDES a
    redaction position, so a seat whose first call comes after the redaction starts after it. Filtered to writes, its first cursor
    would sit below the redaction and the event would arrive for a message it never held. CONTROL: a seat that anchored BEFORE the
    redaction is told."""
    mesh, sender, receiver = _pair(owner)
    late = _peer(owner, "late")
    _grant(owner, NS, late, mesh, permission="read")
    early = _anchor(client, receiver)
    row = _row(owner, sender, {"to": "all", "body": MARKER})
    _redact_in_db(owner, row, sender)
    told, _ = _frames(client, receiver, early)
    assert [e["key"] for e in _events(told)] == [row["key"]], "control: a seat that anchored before the redaction is told"
    started = _anchor(client, late)
    frames, _ = _frames(client, late, started)
    assert _events(frames) == [], "a seat that started after the redaction was told of it"


def test_legacy_position_names_the_last_write_when_the_end_of_the_order_is_a_redaction(owner):
    """A cursor saved at a redaction event is the redaction's position (the end of the order includes it). The row a legacy walk
    resumes from is the last row that ARRIVED, not the old row that was redacted later (an old `created_at` at a new `seq`).
    CONTROL: before the redaction the end IS the last write."""
    import datetime as _dt

    import psycopg

    from tests.test_mesh_delivery_sequence import _row_at

    mesh, sender, receiver = _pair(owner)
    old = _row_at(owner, sender, {"to": "nobody", "body": "an old row"}, _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=1))
    last = _row(owner, sender, {"to": receiver["name"]})
    expect = WAIT.Cursor(WAIT.micros_of(last["created_at"]), last["id"])
    with psycopg.connect(DSN) as conn:
        assert WAIT.legacy_position(conn, WAIT.end_position(conn)) == expect, "control: the end is the last write"
    _redact_in_db(owner, {"id": old["id"], "key": old["key"]}, sender)
    with psycopg.connect(DSN) as conn:
        end = WAIT.end_position(conn)
        assert end.anchor.id == old["id"], "control: the end of the order is now the redaction position"
        assert WAIT.legacy_position(conn, end) == expect, "the legacy walk resumed from the redacted row"


def test_legacy_position_with_only_a_redaction_past_the_cursor_is_the_row_at_the_cursor(owner):
    """`since` is on the last write, and the only thing past it is a redaction. That is no row past the cursor, so the answer is the
    row AT it. A window opened just before the redacted row (an hour old) would re-cover an hour of mail. CONTROL: with a write past
    the cursor the answer is the window just before that write."""
    import datetime as _dt

    import psycopg

    from tests.test_mesh_delivery_sequence import _row_at

    mesh, sender, receiver = _pair(owner)
    old = _row_at(owner, sender, {"to": "nobody", "body": "an old row"}, _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=1))
    at = _row(owner, sender, {"to": receiver["name"]})
    with psycopg.connect(DSN) as conn:
        since = WAIT.end_position(conn)
    assert since.anchor.id == at["id"], "control: the cursor is on the last write"
    after = _row(owner, sender, {"to": receiver["name"]})
    with psycopg.connect(DSN) as conn:
        assert WAIT.legacy_position(conn, since) == WAIT.Cursor(WAIT.micros_of(after["created_at"]) - 1, uuid.UUID(int=(1 << 128) - 1)), \
            "control: with a write past the cursor the answer is the window just before it"
        since_after = WAIT.end_position(conn)
    _redact_in_db(owner, {"id": old["id"], "key": old["key"]}, sender)
    with psycopg.connect(DSN) as conn:
        assert WAIT.legacy_position(conn, since_after) == WAIT.Cursor(WAIT.micros_of(after["created_at"]), after["id"]), \
            "a redaction past the cursor was taken for a row"


def test_legacy_position_at_the_end_of_a_long_order_reads_a_page_and_not_the_history(owner):
    """The SHAPE of the reads, measured as tests/test_mesh_delivery_sequence.py measures the translation (sequential and bitmap scans
    off, so a query the primary key can bound is bounded and one it cannot reads every position). The answer for a cursor at the
    end of the order is the last write, found in one page of the walk; planting 600 rows twice must not grow it."""
    import datetime as _dt

    import psycopg

    from tests.test_mesh_delivery_sequence import _row_at, _rows_read

    mesh, sender, receiver = _pair(owner)
    hour_ago = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=1)
    reads = []
    for batch in range(2):
        for i in range(600):
            _row_at(owner, sender, {"to": "nobody"}, hour_ago + _dt.timedelta(microseconds=batch * 600 + i))
        with psycopg.connect(DSN) as conn:
            since = WAIT.end_position(conn)
        with psycopg.connect(DSN) as conn:
            conn.execute("SET LOCAL enable_seqscan = off")
            conn.execute("SET LOCAL enable_bitmapscan = off")
            before = _rows_read(conn)
            WAIT.legacy_position(conn, since)
            reads.append(_rows_read(conn) - before)
            conn.rollback()
    assert reads[1] - reads[0] < 20, reads
    assert reads[1] <= 2 * WAIT.LEGACY_WALK_PAGE + 10, reads


def test_the_embed_walk_reads_the_tail_of_a_long_order_and_not_the_history(owner):
    """The shape of the embed walk's read, WITH STATISTICS (ANALYZE first, as production's tables always have them). Measured as
    tests/test_mesh_delivery_sequence.py measures the legacy translation, sequential and bitmap scans off. Without statistics a
    freshly built database reads every position for this query whatever its predicates say (measured: 1200 tuples, then 2400, with
    `AND o.kind = 'write'` and without it), so that condition asserts nothing about the predicate. With them the walk reads the tail
    of the primary key's range, and planting 600 rows twice must not grow it: a predicate that moved the plan to UNIQUE
    (memory_id, kind) would show here. It returns exactly the last three positions."""
    import datetime as _dt

    import psycopg

    from apps.meshsem import embedjob
    from tests.test_mesh_delivery_sequence import _row_at, _rows_read

    mesh, sender, receiver = _pair(owner)
    hour_ago = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=1)
    reads = []
    for batch in range(2):
        for i in range(600):
            _row_at(owner, sender, {"to": "nobody", "body": "text"}, hour_ago + _dt.timedelta(microseconds=batch * 600 + i))
        owner.execute("ANALYZE mesh_commit_order")
        owner.execute("ANALYZE shared_memory")
        after = owner.execute("SELECT max(seq) FROM mesh_commit_order").fetchone()[0] - 3
        with psycopg.connect(DSN) as conn:
            conn.execute("SET LOCAL enable_seqscan = off")
            conn.execute("SET LOCAL enable_bitmapscan = off")
            before = _rows_read(conn)
            with conn.cursor() as cur:
                cur.execute(embedjob._SELECT_ROWS, {"ns": NS, "after_seq": after, "limit": 100})
                got = cur.fetchall()
            reads.append(_rows_read(conn) - before)
            conn.rollback()
        assert len(got) == 3, len(got)
    assert reads[1] - reads[0] < 20, reads
    assert reads[1] < 60, reads



def _drain(conn):
    from apps.mesh import projection as P
    for _ in range(200):
        if not P.catch_up(conn).more:
            return
    raise AssertionError("catch-up never drained")


def _mentions_of(conn, row_id) -> int:
    from apps.core import rls
    with rls.server_internal(conn):
        return conn.execute("SELECT count(*) FROM mesh_edges WHERE src_id = %s AND edge_type = 'mentions'",
                            (row_id,)).fetchone()[0]


def _race(owner, row, redactor, walk):
    """A redaction of `row` held open in `owner`'s transaction while `walk(conn)` runs on its own
    connection in a thread, then committed; the walk is joined. The walk reads after the redaction has
    tombstoned the row and deleted its mentions, but before that commits: the window codex named."""
    import threading
    import time
    import psycopg
    from apps.memoryskills import redact

    tx = owner.transaction()
    tx.__enter__()
    failed = []
    try:
        cur = owner.cursor(row_factory=dict_row)
        cur.execute("SELECT id, version, value, signed_bytes, payload_jcs_sha256, created_by_peer_id "
                    "FROM shared_memory WHERE id = %s", (row["id"],))
        redact._redact_one(cur, cur.fetchone(), NS, row["key"], redactor["peer_id"], redactor["owner_id"],
                           "client_name", False, b"the request as signed", f"call-{uuid.uuid4()}", [])

        def run():
            try:
                with psycopg.connect(DSN, autocommit=True) as c:
                    walk(c)
            except Exception as exc:     # surfaced below, not lost in the thread
                failed.append(exc)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        time.sleep(1.5)                  # the walk has read (or is waiting to read) the row
    except BaseException:
        tx.__exit__(*sys.exc_info())
        raise
    tx.__exit__(None, None, None)        # the redaction commits
    t.join(30)
    assert not t.is_alive(), "the walk never finished"
    assert not failed, failed


def test_a_redaction_racing_catch_up_leaves_no_mentions(owner, monkeypatch):
    """codex on #89: catch-up read a body without locking the row. A redaction could tombstone it and
    delete its mentions while the pass held the body, and the pass then inserted mentions from it, so a
    redacted message's references came back. Catch-up now reads FOR SHARE: it waits for the redaction
    and reads the tombstone, which has no body and so no mentions.

    MUTANT: drop FOR SHARE from _SELECT_ROWS; the pass reads the body before the redaction commits and
    inserts its mentions after it (its insert waits on the row's key lock, then goes ahead)."""
    from apps.mesh import projection as P

    monkeypatch.setattr("apps.meshsem.store.forbidden_rows_open", lambda: True)
    mesh, sender, receiver = _pair(owner)
    _drain(owner)
    row = _row(owner, sender, {"to": receiver["name"], "body": "see issue #73 and sampleco/meshy#74"})
    _race(owner, row, sender, lambda c: P.catch_up(c, max_batch=500))
    assert _mentions_of(owner, row["id"]) == 0


def test_a_redaction_racing_the_backfill_leaves_no_mentions(owner, monkeypatch):
    """The same window for the backfill, on a row already projected: the redaction deletes its mentions,
    and a backfill holding the old body would put them back. The backfill reads FOR SHARE too.

    MUTANT: drop FOR SHARE from the backfill's select; the mentions come back."""
    from apps.mesh import projection as P

    monkeypatch.setattr("apps.meshsem.store.forbidden_rows_open", lambda: True)
    mesh, sender, receiver = _pair(owner)
    row = _row(owner, sender, {"to": receiver["name"], "body": "see issue #75 and sampleco/meshy#76"})
    _drain(owner)
    assert _mentions_of(owner, row["id"]) > 0, "control: the row's mentions were projected"
    _race(owner, row, sender, lambda c: P.backfill(c, types=("mentions",)))
    assert _mentions_of(owner, row["id"]) == 0
