"""STATUS E13, delivery's server half: the addressed wait pages by a SERVER sequence in commit
order, behind an opaque cursor (roadmap 6).

WHAT WAS WRONG, on the code before this change. `/v1/mesh/wait?addressed=1` (divergence 031)
paged by `(created_at, id)`, and `created_at` is the writing transaction's START. A write that
began first and committed last landed BELOW rows a read had already passed. Divergence 031
held the resume point ADDRESSED_SETTLE_SECS (10 s) behind the database clock and stated the
bound it left: "a write open longer than this when a read runs can still be passed over".
`apps/mesh/graph.py` `inbox` took a `since` timestamp of the same kind (no caller used it).

WHAT IT IS NOW. The sequence is `mesh_commit_order.seq` (sql_meshychat/023, fin/w2-jobs's D19):
drawn when the row's transaction COMMITS, under a lock that makes seq order the order in which
rows became visible, so a reader that sees seq N sees every seq below it and nothing can later
take a seq below one already read. The addressed wait walks it, and its cursor is the text
`seq:<n>`, base64url without padding: opaque to a client (which stores and returns it), and
never a legal plain-route cursor, so the two cannot be confused. The resume point is the scan
position itself: no settle window, nothing held back. A cursor from before this change (a
`(created_at, id)` one) is still accepted and translated so that no row after it is skipped.
The plain wait keeps its cursor over every readable namespace; commit order is used for
shared/mesh-chat only, where addressed delivery needs a resume point in commit order.

Helpers are tests/test_mesh_wait_addressed.py's (its autouse settings and roster fixtures are
imported too, so they apply here).

Run:  .venv313/Scripts/python.exe -m pytest tests/test_mesh_delivery_sequence.py -q
"""
from __future__ import annotations

import base64
import datetime as _dt
import inspect
import json
import re
import sys
import time
import uuid
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_mesh_wait_addressed import (  # noqa: E402,F401
    DSN, NS, WAIT, _anchor, _drain, _grant, _mesh_owner, _pair, _peer, _row,
    _row_in_open_transaction, _wait, client, mesh_settings, no_roster, owner,
)
from tests.fixtures import signed_rows as sr  # noqa: E402


def _row_at(conn, peer, value: dict, when: _dt.datetime) -> dict:
    """`_row`, with `created_at` given: a committed head msg/ row stamped `when`."""
    rid = uuid.uuid4()
    key = f"msg/{int(when.timestamp() * 1000)}-{rid.hex}"
    # With the bytes a writer would have signed (divergence 051: A2 refuses a row without).
    sr.insert(conn, sr.row(peer["peer_id"], peer["owner_id"], NS, key, value, rid=rid,
                           call_id=f"call-{rid}", author_label=peer["label"], created_at=when))
    return {"key": key, "id": rid, "created_at": when}


def _seq_of(owner, row_id) -> int:
    with owner.cursor() as cur:
        cur.execute("SELECT seq FROM mesh_commit_order WHERE memory_id = %s", (row_id,))
        (seq,) = cur.fetchone()
    return int(seq)


def test_a_write_held_open_past_the_settle_window_is_delivered_from_the_cursor(
        client, owner, monkeypatch):
    """The defect, past the old bound. A write addressed to the receiver is begun and held
    open for longer than the settle window (made 1 s here, from 10 s, so the test is short:
    the same arithmetic at any window), a later write to someone else commits, and the
    addressed read passes it. Then the held write commits. Paging on from the answer's
    cursor, and from its resume cursor, the held row IS answered, once: its seq was drawn at
    its commit, after the row the read passed. Before this change both lost it (the resume
    point was held 1 s behind the clock, and the held row's created_at was older than that).
    CONTROL: a later row for the receiver, committed normally, is answered too, after it."""
    monkeypatch.setattr(WAIT, "ADDRESSED_SETTLE_SECS", 1)
    mesh, sender, receiver = _pair(owner)
    since = _anchor(client, receiver)
    with psycopg.connect(DSN) as slow:                      # a transaction, not autocommit
        held = _row_in_open_transaction(slow, sender, {"to": receiver["name"]})
        time.sleep(1.6)
        later = _row(owner, sender, {"to": "somebody-else"})
        r, _ = _wait(client, receiver["peer_id"], since=since, timeout=0)
        assert r.status_code in (200, 204), (r.status_code, r.content[:300])
        if r.status_code == 204:
            position, resume = r["X-Meshy-Cursor"], r["X-Meshy-Resume-Cursor"]
        else:
            position, resume = json.loads(r.content)["cursor"], r["X-Meshy-Resume-Cursor"]
        slow.commit()
    after = _row(owner, sender, {"to": receiver["name"]})
    ours = {held, later["key"], after["key"]}
    for start in (position, resume):
        got = [m["key"] for m in _drain(client, receiver, start) if m["key"] in ours]
        assert got == [held, after["key"]], (start, got)


def test_the_addressed_cursor_is_the_commit_sequence_and_the_plain_one_is_unchanged(
        client, owner):
    """The addressed answer's `cursor` (and both headers) name a position in the server's
    commit order: `seq:<n>`, base64url without padding, n the seq of the last row the read
    scanned. It is not a plain-route cursor (the plain decoder refuses it, so a node that
    sends it there gets a "no cursor" answer, never a misread position). CONTROL: the plain
    route over the same rows still uses a `<micros>:<uuid>` cursor."""
    mesh, sender, receiver = _pair(owner)
    since = _anchor(client, receiver)
    assert WAIT.decode_seq_cursor(since) is not None, since
    row = _row(owner, sender, {"to": receiver["name"]})
    r, _ = _wait(client, receiver["peer_id"], since=since, timeout=0)
    assert r.status_code == 200, (r.status_code, r.content[:300])
    body = json.loads(r.content)
    assert [m["key"] for m in body["messages"]] == [row["key"]]
    seq = _seq_of(owner, row["id"])
    raw = base64.urlsafe_b64decode(body["cursor"] + "=" * (-len(body["cursor"]) % 4))
    anchor = f"{WAIT.micros_of(row['created_at'])}:{row['id']}"
    assert raw == f"seq:{seq}:{anchor}".encode("ascii") and "=" not in body["cursor"], raw
    assert WAIT.decode_seq_cursor(body["cursor"]) == WAIT.SeqCursor(seq)
    assert r["X-Meshy-Resume-Cursor"] == body["cursor"], "the resume point is the position"
    assert WAIT.decode_cursor(body["cursor"]) is None, "a seq cursor must not decode as plain"

    plain_since = WAIT.encode_cursor(WAIT.micros_of(row["created_at"]) - 1,
                                     uuid.UUID(int=0))
    r, _ = _wait(client, receiver["peer_id"], since=plain_since, timeout=1, addressed=None)
    assert r.status_code == 200, (r.status_code, r.content[:300])
    plain = json.loads(r.content)
    assert WAIT.decode_cursor(plain["cursor"]) is not None, plain["cursor"]
    assert WAIT.decode_seq_cursor(plain["cursor"]) is None, plain["cursor"]


def test_the_seq_cursor_is_decoded_strictly(client, owner):
    """Only the canonical encoding of `seq:<n>:<micros>:<uuid>` with n a positive 63-bit
    integer (or `seq:0`) is a position; anything else is "no cursor" (the wait then starts
    at the newest row, as a malformed plain cursor does). CONTROL: the canonical form of the
    same n decodes."""
    enc = lambda text: base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")  # noqa
    row = f"1790000000000000:{uuid.UUID(int=9)}"
    anchor = WAIT.Cursor(1790000000000000, uuid.UUID(int=9))
    assert WAIT.decode_seq_cursor(enc(f"seq:42:{row}")) == WAIT.SeqCursor(42)
    assert WAIT.encode_seq_cursor(42, anchor) == enc(f"seq:42:{row}")
    assert WAIT.decode_seq_cursor(enc(f"seq:9223372036854775807:{row}")) is not None
    for bad in ("seq:-1", "seq:042", "seq:", "seq:4 2", "seq:9223372036854775808",
                "SEQ:42", "seq:42\n", "seq:+42", "seq:0x2a"):
        assert WAIT.decode_seq_cursor(enc(f"{bad}:{row}")) is None, bad
    assert WAIT.decode_seq_cursor(enc(f"seq:42:{row}") + "=") is None, "padding"
    assert WAIT.decode_seq_cursor(None) is None and WAIT.decode_seq_cursor("") is None


def test_both_cursor_decoders_read_base64_by_one_strict_rule(monkeypatch):
    """The plain and the seq cursor refuse the same four things `URL_SAFE_NO_PAD` does
    (padding, a character outside the url-safe alphabet, an impossible length, non-zero
    trailing bits), because both decode through `_b64url_nopad_decode`. CONTROL: the
    canonical form of each decodes. (Mutant: either decoder keeps its own copy: the patched
    helper below does not reach it.)"""
    plain = WAIT.encode_cursor(1790000000000000, uuid.UUID(int=9))
    seq = WAIT.encode_seq_cursor(420, WAIT.Cursor(1790000000000000, uuid.UUID(int=9)))
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    for decode, good in ((WAIT.decode_cursor, plain), (WAIT.decode_seq_cursor, seq)):
        assert decode(good) is not None, good                                    # control
        assert len(good) % 4 in (2, 3), good    # so the last character carries spare bits
        trailing = good[:-1] + alphabet[alphabet.index(good[-1]) ^ 1]    # same bytes, a spare bit set
        for bad in (good + "=", good[:-1] + "+", good[:-1] + "/", good + "A" * (5 - len(good) % 4),
                    trailing):
            assert decode(bad) is None, (decode.__name__, bad)
    monkeypatch.setattr(WAIT, "_b64url_nopad_decode", lambda raw: None)
    assert WAIT.decode_cursor(plain) is None and WAIT.decode_seq_cursor(seq) is None


def test_a_cursor_from_before_the_sequence_is_translated_and_skips_nothing(client, owner):
    """A node that saved a `(created_at, id)` cursor before this change sends it once more.
    It is translated to the position just before the first row, in commit order, that lies
    after it by `(created_at, id)`, so every such row is answered (and nothing before the
    cursor); the answer's cursor is then a seq cursor. CONTROL: the row the old cursor named
    is not answered again."""
    mesh, sender, receiver = _pair(owner)
    first = _row(owner, sender, {"to": receiver["name"]})
    old = WAIT.encode_cursor(WAIT.micros_of(first["created_at"]), first["id"])
    second = _row(owner, sender, {"to": receiver["name"]})
    third = _row(owner, sender, {"cc": [receiver["name"]], "to": "x"})
    ours = {first["key"], second["key"], third["key"]}
    got = [m["key"] for m in _drain(client, receiver, old) if m["key"] in ours]
    assert got == [second["key"], third["key"]], got
    r, _ = _wait(client, receiver["peer_id"], since=old, timeout=0)
    assert WAIT.decode_seq_cursor(json.loads(r.content)["cursor"]) is not None


def test_graph_inbox_takes_no_timestamp_cursor():
    """The projection's inbox answers "newest first, at most N"; it is not a delivery path.
    Its `since` (a created_at filter, with the start-versus-commit defect above) had no
    caller and is gone, so nothing can page delivery by a timestamp through it."""
    from apps.mesh import graph
    assert "since" not in inspect.signature(graph.inbox).parameters
    assert "limit" in inspect.signature(graph.inbox).parameters, "control"


def test_the_backlog_summary_takes_the_monitors_seq_cursor(owner):
    """`summarize --backlog --since-cursor-file` sends the monitor's saved cursor, which is now
    a seq cursor. The backlog read (apps/meshsum/window.read_backlog) takes it and covers
    every row after it in commit order (over-including rows already delivered rather than
    skipping one). CONTROL: a row before it is not covered."""
    from apps.core import dbconn
    from apps.meshsum import window as W

    mesh, sender, receiver = _pair(owner)
    # Both stamped a minute back, so they lie behind the walk's settle horizon now.
    ago = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=1)
    before = _row_at(owner, sender, {"to": receiver["name"], "body": "before the cursor"},
                     ago)
    cursor = WAIT.SeqCursor(_seq_of(owner, before["id"]),
                            WAIT.Cursor(WAIT.micros_of(before["created_at"]), before["id"]))
    after = _row_at(owner, sender, {"to": receiver["name"], "body": "after the cursor"},
                    ago + _dt.timedelta(milliseconds=1))
    with dbconn.serving_connection(DSN) as conn:
        with dbconn.request_server_internal(conn) as c:
            got = W.read_backlog(c, receiver["peer_id"], since=cursor, max_messages=50,
                                 gate_open=True)
    keys = [r.key for r in got.scanned]
    assert after["key"] in keys, keys
    assert before["key"] not in keys, keys


# ===================================================================== review finding 1
#
# A node still running the monitor from before E13 sends no opt-in. Its `cursor_order` reads
# only `(created_at, id)` cursors, and a `X-Meshy-Resume-Cursor` it cannot read ends it with
# a terminal `server_unsupported` line (dc829ad tools/meshy_node/monitor.py:1041-1047), which
# no re-arm recovers from. So the seq cursor goes only to a client that asks for it
# (`seq=1`) or that sent one; every other addressed wait keeps the walk and the cursor it
# answered before E13.

_PRE_E13_TEXT = re.compile(r"(-?[0-9]{1,18}):([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                           r"[0-9a-f]{4}-[0-9a-f]{12})")


def _pre_e13_client_reads(cursor) -> bool:
    """Whether the monitor from before E13 takes `cursor` as a cursor: its `cursor_order`
    (dc829ad tools/meshy_node/monitor.py:318-337), copied, the bounds being years 1 to 9999
    as its _MICROS_MIN/_MICROS_MAX are."""
    if not isinstance(cursor, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", cursor):
        return False
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
    except ValueError:
        return False
    if base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != cursor:
        return False
    match = _PRE_E13_TEXT.fullmatch(raw.decode("ascii", "replace"))
    if match is None:
        return False
    return -62135596800000000 <= int(match.group(1)) <= 253402300799999999


def test_a_client_that_does_not_ask_for_the_sequence_keeps_the_cursor_it_can_read(
        client, owner):
    """The pre-E13 monitor's first arm (no cursor), then its catch-up from the position it
    was given: without `seq=1` every cursor the addressed wait answers (the body's, the
    204's X-Meshy-Cursor, X-Meshy-Resume-Cursor) is one that client reads, and the resume
    point is held behind the settle horizon as it was, so that client's contract (review
    F1, divergence 031 before E13) is unchanged. CONTROL: the same calls with `seq=1`
    answer seq cursors, which that client does not read."""
    mesh, sender, receiver = _pair(owner)
    r, _ = _wait(client, receiver["peer_id"], timeout=0, seq=None)
    assert r.status_code in (200, 204), (r.status_code, r.content[:300])
    since = (r["X-Meshy-Cursor"] if r.status_code == 204
             else json.loads(r.content)["cursor"])
    assert _pre_e13_client_reads(since), since
    assert _pre_e13_client_reads(r["X-Meshy-Resume-Cursor"]), r["X-Meshy-Resume-Cursor"]
    row = _row(owner, sender, {"to": receiver["name"]})
    got = []
    for _ in range(50):
        r, _ = _wait(client, receiver["peer_id"], since=since, timeout=0, seq=None)
        if r.status_code == 204:
            assert _pre_e13_client_reads(r["X-Meshy-Cursor"]), r["X-Meshy-Cursor"]
            assert _pre_e13_client_reads(r["X-Meshy-Resume-Cursor"])
            break
        assert r.status_code == 200, (r.status_code, r.content[:300])
        body = json.loads(r.content)
        assert _pre_e13_client_reads(body["cursor"]), body["cursor"]
        resume = r["X-Meshy-Resume-Cursor"]
        assert _pre_e13_client_reads(resume), resume
        got += [m["key"] for m in body["messages"]]
        if row["key"] in got:
            held = WAIT.decode_cursor(resume)
            assert held.created_at_micros < WAIT.micros_of(row["created_at"]), (
                "the resume point is held behind the settle horizon, as before E13")
        since = body["cursor"]
    assert row["key"] in got, got

    r, _ = _wait(client, receiver["peer_id"], timeout=0)             # seq=1: the new client
    assert r.status_code in (200, 204), (r.status_code, r.content[:300])
    for header in ("X-Meshy-Resume-Cursor",) + (("X-Meshy-Cursor",)
                                                if r.status_code == 204 else ()):
        assert WAIT.decode_seq_cursor(r[header]) is not None, r[header]
        assert not _pre_e13_client_reads(r[header]), r[header]


def test_a_seq_cursor_is_answered_in_kind_without_the_parameter(client, owner):
    """A client that SENDS a seq cursor reads one, so the wait stays on the commit order for
    it with or without `seq=1`. CONTROL: `seq` other than 0 or 1 is refused, as `addressed`
    is."""
    mesh, sender, receiver = _pair(owner)
    since = _anchor(client, receiver)
    assert WAIT.decode_seq_cursor(since) is not None, since
    row = _row(owner, sender, {"to": receiver["name"]})
    r, _ = _wait(client, receiver["peer_id"], since=since, timeout=0, seq=None)
    assert r.status_code == 200, (r.status_code, r.content[:300])
    body = json.loads(r.content)
    assert row["key"] in [m["key"] for m in body["messages"]]
    assert WAIT.decode_seq_cursor(body["cursor"]) is not None, body["cursor"]
    assert WAIT.decode_seq_cursor(r["X-Meshy-Resume-Cursor"]) is not None
    r, _ = _wait(client, receiver["peer_id"], timeout=0, seq="2")
    assert r.status_code == 400, (r.status_code, r.content[:300])


# ===================================================================== review finding 6
#
# 023's backfill numbers the surviving rows densely from 1. A `seq:<n>` saved before 023 was
# rolled back and applied again then names a place in ANOTHER numbering, and read in the new
# one it lies past rows that landed after it: they were never delivered. So a seq cursor
# names the row AT its position too, and a cursor whose row is not at that place in the
# current numbering is placed again from that row, by `(created_at, id)`.

def _once_more_if_deadlocked(conn, step):
    """Run `step`; if Postgres chose it as a deadlock victim, roll back and run it once more.

    This test deletes shared_memory rows, which wakes autovacuum on that table, and a down script's
    ACCESS EXCLUSIVE lock can then deadlock with it (mg385j on 25 Oct; issue #367, #374's gate on 23 Oct).
    Each step is one transaction: 046's down wraps itself in BEGIN/COMMIT, the other downs run in the
    connection's implicit one, and the migrator commits each file with its ledger row and rolls back the one
    that failed. So the victim's work is gone and the rerun starts from the same point. A second deadlock, or
    any other error, still fails the test.
    """
    try:
        return step()
    except psycopg.errors.DeadlockDetected:
        conn.rollback()
        return step()


def _round_trip_023(owner) -> None:
    """sql_meshychat/down/023 committed, then 023 applied again through the migrator (as
    `sqlmigrate` would), on this run's own database.

    A rollback past 023 rolls back what stands on it first, in reverse order: 042 (divergence 096)
    re-creates 027's and 029's commit-order stampers with pg_temp last in their path and their
    tables schema-qualified, so re-applying 027 and 029 under it would put their old definitions
    back while the ledger still says 042 ran; 040 (divergence 092) replaces 034's
    shared_memory_tombstone_keys() (adding the card fields), so re-applying 034 under it would put
    034's list back while the ledger still says 040 ran, and every later card redaction would be
    refused as a change to an immutable row; 045, 044, 039 and 037 replace
    the route CHECK again (adding the card purpose routes, the card routes, `recipients`, then the fleet routes), 036 replaces
    035's route CHECK (adding `unread`), and 035 replaces
    034's route CHECK on mesh_route_usage (adding `brief` and the two notice routes), so
    rolling back 034 under it would put 034's list back while the ledger still says 035 ran
    and every later count of those routes would fail; 034 replaces
    027's shared_memory_tombstone_keys() (adding `closes` and `requires_ack`), so re-applying 027
    under it would put 027's list back while the ledger still says 034 ran; 031 binds
    records to rows on the redaction table that 027's down drops; 029 (divergence
    059, edit) widens `mesh_commit_order`'s kinds and its trigger stamps into it, 027 (divergence
    053, redaction) alters it and its trigger stamps into it too, and 023's down DROPS that
    table. 046 renames 042's tenant predicate, so its down runs first before 042 restores
    the historical function body. The downs run 046, 045, 044, 042, 040, 039, 037, 036, 035, 034, 031, 029, 027, 023, then the migrator restores those versions
    (the versions in between were never rolled back; 038 adds a column to mesh_presence, 041 a function over skill_calls
    and 043 grants on files, and none of them stands on any of these). Leaving 031 in the ledger while
    dropping its table would silently lose its trigger for the rest of the suite, and leaving
    034 there would silently lose its tombstone keys."""
    from apps.core import sqlmigrate
    root = Path(__file__).resolve().parent.parent
    downs = [root / "sql_meshychat" / "down" / "058_mesh_route_usage_transcript_search_down.sql",
             root / "sql_meshychat" / "down" / "046_p2_meshychat_names_down.sql",
             root / "sql_meshychat" / "down" / "045_mesh_route_usage_card_purpose_down.sql",
             root / "sql_meshychat" / "down" / "044_mesh_route_usage_cards_down.sql",
             root / "sql_meshychat" / "down" / "042_definer_search_path_down.sql",
             root / "sql_meshychat" / "down" / "040_card_tombstone_keys_down.sql",
             root / "sql_meshychat" / "down" / "039_mesh_route_usage_recipients_down.sql",
             root / "sql_meshychat" / "down" / "037_mesh_route_usage_fleet_down.sql",
             root / "sql_meshychat" / "down" / "036_mesh_route_usage_unread_down.sql",
             root / "sql_meshychat" / "down" / "035_mesh_route_usage_brief_down.sql",
             root / "sql_meshychat" / "down" / "034_mesh_edges_closes_mentions_down.sql",
             root / "sql_meshychat" / "down" / "031_redaction_record_bound_to_row_down.sql",
             root / "sql_meshychat" / "down" / "029_mesh_message_edit_down.sql",
             root / "sql_meshychat" / "down" / "027_shared_memory_redaction_down.sql",
             root / "sql_meshychat" / "down" / "023_commit_order_sequence_down.sql"]
    with psycopg.connect(DSN) as conn:
        for down in downs:
            body = down.read_text(encoding="utf-8")
            _once_more_if_deadlocked(conn, lambda body=body: (conn.execute(body), conn.commit()))
        # The rollback runner removes a migration's ledger entry after its down body.
        # This test runs 046's body directly so its object names are available to 042's
        # historical body, then mirrors that runner step before the forward reapply.
        conn.execute("DELETE FROM meshychat_migrations WHERE version = 46")
        conn.commit()
        before = set(sqlmigrate.applied(conn, "meshychat_migrations"))
        conn.commit()
        _once_more_if_deadlocked(conn, lambda: sqlmigrate.migrate(
            conn, sql_dir=root / "sql_meshychat", ledger="meshychat_migrations", upto=58, verbose=False))
        # From the ledger, not migrate()'s return: after a rerun, the return lists only what the rerun applied.
        ran = {"versions": sorted(set(sqlmigrate.applied(conn, "meshychat_migrations")) - before)}
        conn.commit()
        assert conn.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_trigger WHERE "
            "tgrelid = 'shared_memory_redaction'::regclass AND "
            "tgname = 'shared_memory_redaction_bound_trg' AND tgenabled = 'O')"
        ).fetchone()[0], "the rollback round trip did not restore redaction binding"
        keys = conn.execute("SELECT shared_memory_tombstone_keys()").fetchone()[0]
        assert {"closes", "requires_ack", "supersedes"} <= set(keys), keys
        check = conn.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                             "WHERE conname = 'mesh_route_usage_route_check'").fetchone()[0]
        assert "'brief'" in check, "the rollback round trip did not restore 035's route CHECK"
        assert "'unread'" in check, "the rollback round trip did not restore 036's route CHECK"
        assert "'fleet_unread'" in check, "the rollback round trip did not restore 037's route CHECK"
        assert "'recipients'" in check, "the rollback round trip did not restore 039's route CHECK"
        assert "'card_search'" in check, "the rollback round trip did not restore 044's route CHECK"
        assert "'cards_briefing'" in check, "the rollback round trip did not restore 045's route CHECK"
        assert "'transcript_search'" in check, "the rollback round trip did not restore 058's route CHECK"
        stampers = dict(conn.execute(
            "SELECT proname, proconfig FROM pg_proc WHERE proname IN ('meshy_mesh_commit_stamp', "
            "'meshy_mesh_redaction_stamp', 'meshy_mesh_edit_stamp')").fetchall())
        assert stampers == dict.fromkeys(stampers, ["search_path=pg_catalog, public, pg_temp"]) and len(
            stampers) == 3, f"the rollback round trip did not restore 042's stampers: {stampers}"
    assert ran["versions"] == [23, 27, 29, 31, 34, 35, 36, 37, 39, 40, 42, 44, 45, 46, 58], ran


def test_a_saved_seq_cursor_is_read_in_the_numbering_it_came_from(client, owner):
    """The reviewer's repro: 30 rows written and deleted (the gaps a retention sweep
    leaves), one more row, the receiver's position saved there, 023 rolled back and applied
    again (the backfill renumbers the survivors densely), then a row for the receiver. From
    the saved cursor that row IS answered. CONTROL: the round trip really moved the numbering
    under the saved position (the new row's seq is at or below it, so a bare number would
    have skipped it), and a cursor taken after the round trip reads on normally."""
    mesh, sender, receiver = _pair(owner)
    gone = [_row(owner, sender, {"to": "nobody"}) for _ in range(30)]
    with owner.cursor() as cur:
        cur.execute("DELETE FROM shared_memory WHERE id = ANY(%s)", ([g["id"] for g in gone],))
    _row(owner, sender, {"to": "nobody"})
    saved = _anchor(client, receiver)
    old = WAIT.decode_seq_cursor(saved)
    assert old is not None and old.seq > 0, saved
    _round_trip_023(owner)
    row = _row(owner, sender, {"to": receiver["name"]})
    assert _seq_of(owner, row["id"]) <= old.seq, "the round trip did not renumber"
    got = [m["key"] for m in _drain(client, receiver, saved)]
    assert row["key"] in got, got
    fresh = _anchor(client, receiver)
    later = _row(owner, sender, {"to": receiver["name"]})
    assert [m["key"] for m in _drain(client, receiver, fresh)] == [later["key"]]


def test_a_seq_cursor_names_the_row_at_its_position(client, owner):
    """The wire form: `seq:<n>:<created_at micros>:<row id>` for the row whose seq is n,
    `seq:0` before any row; nothing else decodes (a bare `seq:<n>` past 0 names no row and
    cannot be checked, so it is "no cursor"). CONTROL: the answer's cursor names the row it
    read, and encodes as the decoder reads it."""
    mesh, sender, receiver = _pair(owner)
    since = _anchor(client, receiver)
    row = _row(owner, sender, {"to": receiver["name"]})
    r, _ = _wait(client, receiver["peer_id"], since=since, timeout=0)
    body = json.loads(r.content)
    raw = base64.urlsafe_b64decode(body["cursor"] + "=" * (-len(body["cursor"]) % 4))
    seq = _seq_of(owner, row["id"])
    assert raw == f"seq:{seq}:{WAIT.micros_of(row['created_at'])}:{row['id']}".encode(), raw
    decoded = WAIT.decode_seq_cursor(body["cursor"])
    assert decoded.seq == seq and decoded.anchor == WAIT.Cursor(
        WAIT.micros_of(row["created_at"]), row["id"]), decoded
    assert WAIT.cursor_wire(decoded) == body["cursor"]
    enc = lambda text: base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")  # noqa
    assert WAIT.decode_seq_cursor(enc("seq:0")).seq == 0
    u = uuid.UUID("0000000a-0000-4000-8000-00000000000b")
    good = f"seq:7:1790000000000000:{u}"
    assert WAIT.decode_seq_cursor(enc(good)).seq == 7
    for bad in ("seq:7", f"seq:0:1790000000000000:{u}", f"seq:7:1790000000000000:{u.hex}",
                f"seq:7:1790000000000000:{str(u).upper()}", f"seq:7:01790000000000000:{u}",
                f"seq:7:+1790000000000000:{u}", f"seq:7::{u}", "seq:7:1790000000000000"):
        assert WAIT.decode_seq_cursor(enc(bad)) is None, bad


# ===================================================================== review finding 2
#
# B5 (tests/test_query_cost_bounds.py): no per-request aggregate without a declared bound.
# The addressed wait's reads of the commit order are primary-key probes, and translating a
# plain cursor (once per node from before E13, or for a seq cursor whose row moved) walks
# the commit order BACKWARDS from its end and stops ADDRESSED_SETTLE_SECS before the
# cursor's instant: it reads what lies after the cursor, not the history before it.

def _rows_read(conn) -> int:
    """Tuples this transaction has read from the two tables, by scan or index fetch."""
    return int(conn.execute(
        "SELECT COALESCE(sum(seq_tup_read + COALESCE(idx_tup_fetch, 0)), 0) "
        "FROM pg_stat_xact_user_tables "
        "WHERE relname IN ('shared_memory', 'mesh_commit_order')").fetchone()[0])


def _translation_reads(cursor):
    """(the position, the tuples read) of one translation, in a transaction of its own.

    Sequential and bitmap scans off: this measures the SHAPE of the reads, as the B5 tests
    do (tests/test_query_cost_bounds.py: on a table this small Postgres rightly prefers to
    read the whole table and sort, for ANY query, which would prove nothing; measured here,
    a bitmap scan read all 601 rows for a 100-row page). With them off, a query the index
    can bound is bounded, and one it cannot (a min() over every row after a `(created_at,
    id)` that no index orders) still reads them all."""
    with psycopg.connect(DSN) as conn:           # one transaction: the xact counters are its own
        conn.execute("SET LOCAL enable_seqscan = off")
        conn.execute("SET LOCAL enable_bitmapscan = off")
        before = _rows_read(conn)
        position = WAIT.seq_after_legacy(conn, cursor)
        read = _rows_read(conn) - before
        conn.rollback()
    return position, read


def test_translating_a_plain_cursor_reads_what_follows_it_not_the_history(owner):
    """A plain cursor on a recent row, and history planted twice, 600 rows each time
    (stamped an hour back, so before the cursor by `(created_at, id)` although they
    committed after it). The translation reads the same number of rows after the second 600
    as after the first, at most one walk page and its probes (the walk stops at the first
    row stamped ADDRESSED_SETTLE_SECS before the cursor). Before this change one `min()` over
    the whole commit order read every row each time, 600 more the second time. CONTROL: rows
    that DO lie after the cursor are found: the position lands just before the first."""
    mesh, sender, receiver = _pair(owner)
    mark = _row(owner, sender, {"to": "nobody"})
    cursor = WAIT.Cursor(WAIT.micros_of(mark["created_at"]), mark["id"])
    hour_ago = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=1)
    reads = []
    for batch in range(2):
        for i in range(600):
            _row_at(owner, sender, {"to": "nobody"},
                    hour_ago + _dt.timedelta(microseconds=batch * 600 + i))
        position, read = _translation_reads(cursor)
        assert position.seq >= _seq_of(owner, mark["id"]), position
        reads.append(read)
    assert reads[1] - reads[0] < 20, reads
    assert reads[1] <= 2 * WAIT.LEGACY_WALK_PAGE + 10, reads

    after = _row(owner, sender, {"to": receiver["name"]})
    position, _ = _translation_reads(cursor)
    assert position.seq < _seq_of(owner, after["id"]), (position, _seq_of(owner, after["id"]))
    assert position.seq >= _seq_of(owner, mark["id"]), position


def test_a_late_commit_stamped_before_the_cursor_does_not_end_the_walk_early(owner):
    """The walk steps over a row that is NOT after the cursor but was stamped inside the
    settle window: a write X begun before the cursor's row and committed last lies at the
    end of the commit order, and behind it lies L, after the cursor. From the cursor the
    translation lands before L. (A walk that stopped at the first row not after the cursor
    would land past L and skip it.) CONTROL: X really is last in the commit order."""
    mesh, sender, receiver = _pair(owner)
    with psycopg.connect(DSN) as slow:                      # a transaction, not autocommit
        held = _row_in_open_transaction(slow, sender, {"to": "nobody"})
        time.sleep(0.05)
        mark = _row(owner, sender, {"to": "nobody"})
        late = _row(owner, sender, {"to": receiver["name"]})
        slow.commit()
    with owner.cursor() as cur:
        cur.execute("SELECT o.seq FROM mesh_commit_order o JOIN shared_memory sm "
                    "ON sm.id = o.memory_id WHERE sm.key = %s", (held,))
        (held_seq,) = cur.fetchone()
    assert held_seq > _seq_of(owner, late["id"]), "X is not last"
    cursor = WAIT.Cursor(WAIT.micros_of(mark["created_at"]), mark["id"])
    position, _ = _translation_reads(cursor)
    assert position.seq < _seq_of(owner, late["id"]), (position, _seq_of(owner, late["id"]))


class _FakeConn:
    def __init__(self):
        self.rollbacks = 0

    def rollback(self):
        self.rollbacks += 1


def _steps(*outcomes):
    """A step that raises or returns each outcome in turn, and counts its calls."""
    calls = []

    def step():
        calls.append(1)
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome
    return step, calls


def test_one_deadlock_is_rolled_back_and_run_once_more():
    conn = _FakeConn()
    step, calls = _steps(psycopg.errors.DeadlockDetected("deadlock detected"), "second")
    assert _once_more_if_deadlocked(conn, step) == "second"
    assert (len(calls), conn.rollbacks) == (2, 1)


def test_a_second_deadlock_still_fails():
    conn = _FakeConn()
    step, calls = _steps(psycopg.errors.DeadlockDetected("first"), psycopg.errors.DeadlockDetected("second"), "never")
    with pytest.raises(psycopg.errors.DeadlockDetected, match="second"):
        _once_more_if_deadlocked(conn, step)
    assert len(calls) == 2


def test_any_other_error_fails_with_no_rerun():
    for error in (psycopg.errors.LockNotAvailable("lock timeout"), psycopg.OperationalError("gone"), AssertionError("x")):
        conn = _FakeConn()
        step, calls = _steps(error, "never")
        with pytest.raises(type(error)):
            _once_more_if_deadlocked(conn, step)
        assert (len(calls), conn.rollbacks) == (1, 0), error
