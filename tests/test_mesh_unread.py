"""GET /v1/mesh/unread (divergence 074): a seat's fetch backlog, read from the server's own
record of who fetched what.

WHY. meshy-mod counted a read only when it could parse the shell line that ran it, and on
2026-10-05 2,170 of 2,363 reads in six HQB transcripts were in a form it could not see: seats
that had read everything showed 55, 10 and 4 unread, and the operator could not tell a busy seat
from one that was behind. Every `tools.meshy_node read` is a signed memory.read call, and
skill_calls keeps the caller, the args and the status for the call retention (90 days by
default). So the backlog is a server query, and it counts every way of reading.

kxxx's conditions (msg/<key>): addressed mail only (to, not cc); a COMPLETED
memory.read by THIS peer of that key at that version or later; the same peer context and read
gate as owed; a filters echo; `since` capped below the call retention and the answer says so;
a failed or refused read clears nothing.

The harness is tests/test_mesh_routes.py's: the Django test client against the dev database,
peers and signed rows written straight to the tables, the projection rebuilt. Read calls are
planted as skill_calls rows (the bootstrap role bypasses RLS), so each test sets exactly the
reads it is about. The route itself reads them in server_internal, as it does in production.

Run:  bash ../runt-wt.sh <worktree> tests/test_mesh_unread.py -q
Mutants (each a one-line break, each seen to fail a test here; apps/mesh/graph.py unless named):
  U1  any status clears, not only completed            test_a_completed_read_by_this_peer_clears_it_and_nothing_else_does
  U2  another peer's read clears this peer's mail       test_a_completed_read_by_this_peer_clears_it_and_nothing_else_does
  U3  cc mail counts as backlog                         test_a_completed_read_by_this_peer_clears_it_and_nothing_else_does
  U4  a read of an older version clears a newer one     test_an_edit_makes_a_fetched_message_pending_again
  U5  a fallback head read clears whenever it was made test_a_fallback_head_read_made_before_the_edit_does_not_clear_it
  U6  the caller's own messages count                   test_the_callers_own_messages_and_broadcasts_are_not_backlog
  U7  total and oldest_at count only the returned page  test_the_window_reports_its_total_and_oldest_beyond_the_limit
  U8  `since` is not capped below the call retention    test_a_window_older_than_the_call_retention_is_capped_and_says_so (views.py)
  U9  a message to two of the peer's labels is two rows test_a_message_to_two_of_the_callers_labels_is_one_row
  U10 the down script keeps the unread rows             test_036_rollback_removes_unread_usage_rows_and_restores_035_check (down/036)
  U11 the reads come from the bare args, not args.args  test_a_completed_read_by_this_peer_clears_it_and_nothing_else_does
      (the first version of this file and of graph.unread both did this; see _read)
  U12 the prune summary drops memo_version (retention.py)  test_d14_a_pruned_read_keeps_the_version_it_returned_and_no_text
      (tests/test_call_retention.py)
  U13 the returned version is ignored                 test_a_read_that_raced_an_edit_counts_by_the_version_it_returned
  U14 a string version is not read in the fallback     test_a_read_whose_result_no_longer_says_falls_back_to_what_it_asked
  U15 a null version is not the head in the fallback   test_a_read_whose_result_no_longer_says_falls_back_to_what_it_asked
  U16 the default window is not clamped (views.py)    test_the_default_window_fits_a_short_retention
  U17 the prune keeps any memo.version (retention.py) test_d14_a_pruned_read_keeps_the_version_it_returned_and_no_text
  U18 a given but unreadable version takes the time rule  test_a_read_whose_result_no_longer_says_falls_back_to_what_it_asked
  U19 whitespace and a sign are not stripped          test_a_read_whose_result_no_longer_says_falls_back_to_what_it_asked
  U20 `reads` is not MATERIALIZED (graph.py)        test_the_reads_cte_is_materialized
  U21 the reads are matched by the old NOT EXISTS   test_the_reads_and_cleared_are_scanned_once_in_the_routes_own_context
      against `reads` (the statement before 3 Nov 2026)
  U22 `cleared` is joined as a CTE, not read once   test_the_reads_and_cleared_are_scanned_once_in_the_routes_own_context
  U23 a read whose args carry no key is not left    test_a_read_whose_args_carry_no_key_neither_fails_nor_clears
      out of `cleared` (graph.py)
  U24 `cleared` keeps the SMALLEST version asked    test_the_largest_version_read_decides_and_a_later_smaller_read_does_not_undo_it
  U25 the time arm compares with `>`, not `>=`      test_a_head_read_at_the_heads_own_microsecond_clears_it_and_one_before_does_not
  U26 the time arm keeps only milliseconds          test_a_head_read_at_the_heads_own_microsecond_clears_it_and_one_before_does_not
  U27 the query unbounded, or QueryCanceled not caught (views.py: R6)
                                                    test_an_89_day_window_on_a_busy_seat_answers_timed_out_never_500
      (and tests/test_mesh_unread_bound.py, which needs no database)
"""
from __future__ import annotations

import datetime as _dt
import os
import uuid

import psycopg
import pytest
from psycopg.types.json import Jsonb

from tests.test_mesh_routes import (DSN, NS, P, _auth, _edit_row_at, _peer, _row_at,  # noqa: F401
                                    _sqlmigrate, client, mesh_settings, owner)
from apps.memoryskills import handlers
from tools.meshy_node import wire

NOW = lambda: _dt.datetime.now(_dt.timezone.utc)  # noqa: E731


@pytest.fixture(autouse=True)
def _no_roster():
    handlers.prime_roster_labels(b"{}")
    yield
    handlers.prime_roster_labels(b"{}")


def _target(owner) -> tuple[dict, uuid.UUID]:
    """The peer a memory.read call is addressed to, and its memory.read skill row (FK)."""
    target = _peer(owner, "memd")
    sid = uuid.uuid4()
    owner.execute("INSERT INTO skills (id, peer_id, name, version, mode, share_mode, input_schema) "
                  "VALUES (%s,%s,'memory.read','1','sync','private','{}')", (sid, target["peer_id"]))
    return target, sid


#: Leave `version` out of the call's args (a read of the head).
HEAD = object()


def _read(owner, reader: dict, target, key: str | None, *, version=HEAD, returned: int | None = 1,
          status: str = "completed", at: _dt.datetime | None = None, pruned: bool = False,
          namespace: str = NS) -> None:
    """A memory.read call by `reader`, stored as the invoke pipeline stores it: `args` is the
    WHOLE signed payload (apps/calls/repository.insert_call), built here by the real client's
    own `wire.call_envelope`, so this fixture cannot drift from what production holds. The first
    version of this file planted the bare memory.read args instead; every test and every mutant
    passed against that shape, and the query matched no real row.

    `version` is what the call ASKED for (HEAD leaves it out; None sends a JSON null; a string is
    sent as a string). `returned` is the version its result says it fetched, as handle_read
    answers {op, memo}; None writes no result, as on a row whose result no longer says. `pruned`
    writes the read-result prune's summary instead, which keeps only `memo_version`. A `key` of
    None leaves the key out of the args, as no real client sends but a stored row could hold."""
    t, sid = target
    margs = {"namespace": namespace, "include_provenance": True}
    if key is not None:
        margs["key"] = key
    if version is not HEAD:
        margs["version"] = version
    result = None
    if status == "completed" and returned is not None:
        result = ({"ok": True, "pruned": True, "pruned_reason": "read_result_retention",
                   "skill_name": "memory.read", "op": "memory.read", "memo_version": returned}
                  if pruned else
                  {"op": "memory.read", "memo": {"key": key, "version": returned,
                                                 "value": {"body": "synthetic"}}})
    args = wire.call_envelope(seed=os.urandom(32), kid="kid", caller_peer_id=str(reader["peer_id"]),
                              target_peer_id=str(t["peer_id"]), skill="memory.read",
                              args=margs)["payload"]
    owner.execute(
        "INSERT INTO skill_calls (call_id, caller_peer_id, caller_owner_id, target_peer_id, "
        "target_owner_id, skill_id, skill_name, skill_version, mode, transport, status, args, "
        "args_signature, args_signing_kid, deadline_at, created_at, result) "
        "VALUES (%s,%s,%s,%s,%s,%s,'memory.read','1','sync','http',%s,%s,'sig','kid',%s,%s,%s)",
        (f"unread-{uuid.uuid4()}", reader["peer_id"], reader["owner_id"], t["peer_id"],
         t["owner_id"], sid, status, Jsonb(args), (at or NOW()) + _dt.timedelta(hours=1),
         at or NOW(), None if result is None else Jsonb(result)))


def _unread(client, peer, query: str = "") -> dict:
    r = client.get(f"/v1/mesh/unread{query}", **_auth(peer["peer_id"]))
    assert r.status_code == 200, r.content[:400]
    return r.json()


def _keys(body) -> list[str]:
    return [item["key"] for item in body["unread"]["items"]]


def _mail(owner, sender, to=None, cc=None, *, minutes_ago: float = 5) -> str:
    value = {"body": "synthetic", "thread": f"t-{uuid.uuid4().hex[:6]}"}
    if to is not None:
        value["to"] = to
    if cc is not None:
        value["cc"] = cc
    key, _ = _row_at(owner, sender, value, NOW() - _dt.timedelta(minutes=minutes_ago))
    return key


def test_a_completed_read_by_this_peer_clears_it_and_nothing_else_does(client, owner):
    """Addressed mail is backlog until THIS peer completes a read of it. CONTROLS, each beside the
    case it guards: a failed read, a read by another peer, and cc mail (never backlog) leave the
    answer as it was, while the completed read clears exactly its own key.
    MUTANTS U1 (any status clears), U2 (another peer's read clears), U3 (cc counts)."""
    a, b, c = _peer(owner, "ua"), _peer(owner, "ub"), _peer(owner, "uc")
    target = _target(owner)
    k_read = _mail(owner, a, to=[b["name"]], minutes_ago=30)
    k_failed = _mail(owner, a, to=[b["name"]], minutes_ago=20)
    k_other = _mail(owner, a, to=[b["name"], c["name"]], minutes_ago=10)
    k_cc = _mail(owner, a, to=[c["name"]], cc=[b["name"]], minutes_ago=5)
    P.rebuild(owner)
    before = _unread(client, b)
    assert set(_keys(before)) == {k_read, k_failed, k_other}, "cc mail is never backlog (U3)"
    _read(owner, b, target, k_read)
    _read(owner, b, target, k_failed, status="failed")
    _read(owner, c, target, k_other)
    after = _unread(client, b)
    assert set(_keys(after)) == {k_failed, k_other}
    assert after["unread"]["total"] == 2
    assert k_other not in _keys(_unread(client, c)), "c's own read cleared c's copy"


def test_an_edit_makes_a_fetched_message_pending_again(client, owner):
    """An edit is a new head version: a read of version 1 no longer covers it, and a read that
    named no version counts only when it was made at or after the edit, since it fetched the head
    of its moment. Reading version 2, or reading the head after the edit, clears it.
    MUTANTS U4 (an older version clears) and U5 (a versionless read clears whenever made)."""
    a, b = _peer(owner, "ea"), _peer(owner, "eb")
    target = _target(owner)
    t0 = NOW() - _dt.timedelta(minutes=30)
    key, rid = _row_at(owner, a, {"to": [b["name"]], "body": "v1"}, t0)
    _read(owner, b, target, key, version=1, returned=1, at=t0 + _dt.timedelta(minutes=1))
    _read(owner, b, target, key, returned=1, at=t0 + _dt.timedelta(minutes=2))   # the head of then
    P.rebuild(owner)
    assert key not in _keys(_unread(client, b)), "both reads covered version 1"
    edited_at = t0 + _dt.timedelta(minutes=10)
    _edit_row_at(owner, a, key, {"to": [b["name"]], "body": "v2"}, rid, edited_at)
    P.rebuild(owner)
    pending = _unread(client, b)
    assert key in _keys(pending), "an edit makes a fetched message pending again (U4, U5)"
    item = next(i for i in pending["unread"]["items"] if i["key"] == key)
    assert item["version"] == 2
    assert _dt.datetime.fromisoformat(item["version_at"]) == edited_at, "age starts at the edit"
    _read(owner, b, target, key, returned=2, at=edited_at + _dt.timedelta(minutes=1))  # head of now
    assert key not in _keys(_unread(client, b))


def test_a_read_of_the_new_version_by_number_clears_it(client, owner):
    """The versioned half of the rule, on its own: version 2 asked for by number clears the
    version 2 head, and version 1 asked for after the edit does not. MUTANT U4."""
    a, b = _peer(owner, "va"), _peer(owner, "vb")
    target = _target(owner)
    t0 = NOW() - _dt.timedelta(minutes=30)
    key, rid = _row_at(owner, a, {"to": [b["name"]], "body": "v1"}, t0)
    _edit_row_at(owner, a, key, {"to": [b["name"]], "body": "v2"}, rid, t0 + _dt.timedelta(minutes=5))
    P.rebuild(owner)
    _read(owner, b, target, key, version=1, returned=1, at=t0 + _dt.timedelta(minutes=6))
    assert key in _keys(_unread(client, b))
    _read(owner, b, target, key, version=2, returned=2, at=t0 + _dt.timedelta(minutes=7))
    assert key not in _keys(_unread(client, b))


def test_the_callers_own_messages_and_broadcasts_are_not_backlog(client, owner):
    """A message the caller signed is never its backlog, even addressed to itself; a broadcast to
    `all` is not addressed to the caller's labels (as in inbox). CONTROL: the same peer's mail from
    someone else is listed. MUTANT U6."""
    a, b = _peer(owner, "oa"), _peer(owner, "ob")
    own = _mail(owner, b, to=[b["name"]])
    broadcast = _mail(owner, a, to=["all"])
    theirs = _mail(owner, a, to=[b["name"]])
    P.rebuild(owner)
    keys = _keys(_unread(client, b))
    assert theirs in keys and own not in keys and broadcast not in keys


def test_a_read_that_raced_an_edit_counts_by_the_version_it_returned(client, owner):
    """Codex on #111: an edit's version 2 is stamped with its transaction's START, so a read of the
    head that ran inside that transaction is made AFTER version 2's time yet fetched version 1.
    Judged by time it cleared version 2 and the edit never showed as backlog. Judged by the version
    it returned, it does not. CONTROL: a read returning version 2 at the same moment clears it.
    MUTANT U13: the returned version is ignored and the time rule decides."""
    a, b = _peer(owner, "ra"), _peer(owner, "rb2")
    target = _target(owner)
    t0 = NOW() - _dt.timedelta(minutes=30)
    key, rid = _row_at(owner, a, {"to": [b["name"]], "body": "v1"}, t0)
    edited_at = t0 + _dt.timedelta(minutes=10)
    _edit_row_at(owner, a, key, {"to": [b["name"]], "body": "v2"}, rid, edited_at)
    P.rebuild(owner)
    _read(owner, b, target, key, returned=1, at=edited_at + _dt.timedelta(milliseconds=200))
    assert key in _keys(_unread(client, b)), "a read that fetched version 1 does not clear version 2"
    _read(owner, b, target, key, returned=2, at=edited_at + _dt.timedelta(milliseconds=200))
    assert key not in _keys(_unread(client, b))


def test_a_pruned_read_still_counts_by_the_version_it_returned(client, owner):
    """An hour after a read the prune replaces its result with a summary that keeps only
    `memo_version`. That still decides: version 1 does not clear an edited message, version 2 does.
    MUTANT U12 (retention.py) is the prune side, tested in tests/test_call_retention.py."""
    a, b = _peer(owner, "pa"), _peer(owner, "pb")
    target = _target(owner)
    t0 = NOW() - _dt.timedelta(minutes=30)
    key, rid = _row_at(owner, a, {"to": [b["name"]], "body": "v1"}, t0)
    edited_at = t0 + _dt.timedelta(minutes=10)
    _edit_row_at(owner, a, key, {"to": [b["name"]], "body": "v2"}, rid, edited_at)
    P.rebuild(owner)
    _read(owner, b, target, key, returned=1, pruned=True, at=edited_at + _dt.timedelta(minutes=1))
    assert key in _keys(_unread(client, b))
    _read(owner, b, target, key, returned=2, pruned=True, at=edited_at + _dt.timedelta(minutes=2))
    assert key not in _keys(_unread(client, b))


@pytest.mark.parametrize("asked,clears", [("2", True), (2, True), ("1", False), (None, True), (HEAD, True),
                                          (" 1 ", False), ("+1", False), (" 2 ", True), ("+2", True),
                                          (2.0, True), (1.9, False), (True, False), ("x", False)])
def test_a_read_whose_result_no_longer_says_falls_back_to_what_it_asked(client, owner, asked, clears):
    """A read pruned before the summary kept the version (or redacted) has no returned version.
    Then what it ASKED for decides, normalized as handle_read does: a number or a string of digits
    is that version, and null or absent is the head, which counts when the read was made after the
    head was written. Codex on #111: a null or a numeric string never cleared anything, and then
    ' 1 ' and '+1', which int() reads as 1, fell to the time rule and cleared version 2. A version
    that was given but cannot be read (here 'x') clears nothing; 1.9 is 1, as int() truncates.
    MUTANTS U14 (a string version is not read), U15 (a null version is not the head), U18 (a given
    but unreadable version takes the time rule) and U19 (whitespace and a sign are not stripped)."""
    a, b = _peer(owner, "fa"), _peer(owner, "fb")
    target = _target(owner)
    t0 = NOW() - _dt.timedelta(minutes=30)
    key, rid = _row_at(owner, a, {"to": [b["name"]], "body": "v1"}, t0)
    edited_at = t0 + _dt.timedelta(minutes=10)
    _edit_row_at(owner, a, key, {"to": [b["name"]], "body": "v2"}, rid, edited_at)
    P.rebuild(owner)
    _read(owner, b, target, key, version=asked, returned=None, at=edited_at + _dt.timedelta(minutes=1))
    assert (key not in _keys(_unread(client, b))) is clears


def test_a_fallback_head_read_made_before_the_edit_does_not_clear_it(client, owner):
    """In the fallback, a read of the head counts only when it was made at or after the head version
    was written: one made before the edit fetched version 1. CONTROL: the same read made after the
    edit clears it. MUTANT U5: a fallback head read clears whenever it was made."""
    a, b = _peer(owner, "ha"), _peer(owner, "hb")
    target = _target(owner)
    t0 = NOW() - _dt.timedelta(minutes=30)
    key, rid = _row_at(owner, a, {"to": [b["name"]], "body": "v1"}, t0)
    edited_at = t0 + _dt.timedelta(minutes=10)
    _edit_row_at(owner, a, key, {"to": [b["name"]], "body": "v2"}, rid, edited_at)
    P.rebuild(owner)
    _read(owner, b, target, key, returned=None, at=t0 + _dt.timedelta(minutes=5))
    assert key in _keys(_unread(client, b))
    _read(owner, b, target, key, returned=None, at=edited_at + _dt.timedelta(minutes=1))
    assert key not in _keys(_unread(client, b))


def test_the_default_window_fits_a_short_retention(client, owner, monkeypatch):
    """Codex on #111: at MESHYCHAT_CALL_RETENTION_DAYS=7 the default 7 days exceeded the maximum of 6,
    so every plain call was a 422. The DEFAULT is clamped; an explicit days past it is still
    refused. MUTANT U16 (views.py): the default is not clamped."""
    monkeypatch.setenv("MESHYCHAT_CALL_RETENTION_DAYS", "7")
    b = _peer(owner, "short")
    f = _unread(client, b)["filters"]
    assert f["days"] == 6 and f["retention_days"] == 7 and f["capped"] is False
    assert client.get("/v1/mesh/unread?days=7", **_auth(b["peer_id"])).status_code == 422


def test_the_window_reports_its_total_and_oldest_beyond_the_limit(client, owner):
    """`limit` caps the items, newest first, and says so; `total` and `oldest_at` describe the
    whole window, so a seat with a long backlog is not read as a short one. MUTANT U7."""
    a, b = _peer(owner, "wa"), _peer(owner, "wb")
    keys = [_mail(owner, a, to=[b["name"]], minutes_ago=m) for m in (50, 40, 30)]
    P.rebuild(owner)
    body = _unread(client, b, "?limit=2")
    assert _keys(body) == [keys[2], keys[1]]
    assert body["unread"]["truncated"] is True and body["unread"]["total"] == 3
    oldest = _dt.datetime.fromisoformat(body["unread"]["oldest_at"])
    assert abs((oldest - (NOW() - _dt.timedelta(minutes=50))).total_seconds()) < 60
    assert body["filters"]["limit"] == 2 and body["filters"]["days"] == 7
    assert body["filters"]["capped"] is False and "coverage" in body and "as_of" in body


def test_a_message_older_than_the_window_is_not_listed(client, owner):
    """`days` bounds the window by the head version's write time. CONTROL: the newer message is."""
    a, b = _peer(owner, "da"), _peer(owner, "db")
    old = _mail(owner, a, to=[b["name"]], minutes_ago=3 * 24 * 60)
    new = _mail(owner, a, to=[b["name"]], minutes_ago=5)
    P.rebuild(owner)
    assert _keys(_unread(client, b, "?days=2")) == [new]
    assert set(_keys(_unread(client, b, "?days=4"))) == {old, new}


def test_a_window_older_than_the_call_retention_is_capped_and_says_so(client, owner, monkeypatch):
    """kxxx's condition: a read the retention deleted would turn fetched mail back into backlog,
    so the window never opens before the retention less a day. An explicit `since` past that is
    moved up and the answer says so; `days` past it is refused. CONTROL: a `since` inside the window
    is used as given. MUTANT U8 (views.py: no cap)."""
    monkeypatch.setenv("MESHYCHAT_CALL_RETENTION_DAYS", "10")
    b = _peer(owner, "rb")
    asked = (NOW() - _dt.timedelta(days=30)).isoformat().replace("+00:00", "Z")
    body = _unread(client, b, f"?since={asked}")
    f = body["filters"]
    assert f["capped"] is True and f["requested_since"] == asked and f["retention_days"] == 10
    since = _dt.datetime.fromisoformat(f["since"])
    assert abs((since - (NOW() - _dt.timedelta(days=9))).total_seconds()) < 60
    inside = (NOW() - _dt.timedelta(days=2)).isoformat().replace("+00:00", "Z")
    g = _unread(client, b, f"?since={inside}")["filters"]
    assert g["capped"] is False and "requested_since" not in g
    assert client.get("/v1/mesh/unread?days=10", **_auth(b["peer_id"])).status_code == 422
    assert client.get("/v1/mesh/unread?days=9", **_auth(b["peer_id"])).status_code == 200


def test_an_89_day_window_on_a_busy_seat_answers_timed_out_never_500(client, owner, monkeypatch):
    """R6 (oxxxxxx ab9f28): on 8 Nov 2026 an 89-day window for a busy seat ran into the server's 30 s statement
    timeout twice, and the route answered 500. The query now has its own bound in a savepoint: past it the answer is
    a 200 that says `timed_out`, with no items and a null total, and the request goes on to its coverage. The busy
    seat is a sleep in front of the real query, in the route's own transaction. CONTROL: the same window without the
    sleep answers its list, `timed_out` false. MUTANT U27 (views.py: the query unbounded, or QueryCanceled not caught)."""
    from apps.mesh import graph, views
    monkeypatch.setenv("MESHYCHAT_CALL_RETENTION_DAYS", "90")
    b, a = _peer(owner, "rbusy"), _peer(owner, "rsend")
    k1 = _mail(owner, a, to=[b["name"]])
    P.rebuild(owner)
    real = graph.unread

    def busy(conn, *args, **kwargs):
        with conn.cursor() as cur:
            cur.execute("SELECT pg_sleep(1)")
        return real(conn, *args, **kwargs)
    monkeypatch.setattr(views, "UNREAD_TIMEOUT_MS", 200)
    monkeypatch.setattr(graph, "unread", busy)
    r = client.get("/v1/mesh/unread?days=89", **_auth(b["peer_id"]))
    assert r.status_code == 200, r.content[:400]
    body = r.json()
    assert body["filters"]["days"] == 89 and body["coverage"]["total"] is not None
    u = body["unread"]
    assert u["timed_out"] is True and u["truncated"] is True and u["items"] == [] and u["total"] is None
    assert "shorter" in u["reason"]
    monkeypatch.setattr(graph, "unread", real)
    ok = _unread(client, b, "?days=89")["unread"]
    assert ok["timed_out"] is False and ok["total"] == 1 and [i["key"] for i in ok["items"]] == [k1]


def test_a_message_to_two_of_the_callers_labels_is_one_row(client, owner):
    """A peer is answered for under several labels (views.labels_for); one message addressed to
    two of them is one item and counts once. MUTANT U9."""
    a, b = _peer(owner, "la"), _peer(owner, "lb")
    fallback = f"peer-{str(b['peer_id'])[:8]}"
    key = _mail(owner, a, to=[b["name"], fallback])
    P.rebuild(owner)
    body = _unread(client, b)
    assert _keys(body).count(key) == 1 and body["unread"]["total"] == 1


@pytest.mark.parametrize("query", ("since=2026-10-03T10:00:00", "since=bogus", "days=0", "days=x",
                                   "limit=0", "limit=501", "limit=x",
                                   "since=2026-10-03T10:00:00Z&days=2"))
def test_unread_refuses_bad_parameters_with_422(client, owner, query):
    """A malformed bound is refused, never silently widened or narrowed."""
    b = _peer(owner, "bad")
    r = client.get(f"/v1/mesh/unread?{query}", **_auth(b["peer_id"]))
    assert r.status_code == 422 and r.json()["error"] == "E_VALIDATION", r.content


def test_unread_is_behind_the_read_gate(client, owner):
    """The same gate as every graph route: a peer without read on shared/mesh-chat is refused,
    and without a bearer the route is 401. CONTROL: a granted peer is answered."""
    ungranted, granted = _peer(owner, "ng", grant=False), _peer(owner, "g")
    assert client.get("/v1/mesh/unread").status_code == 401
    refused = client.get("/v1/mesh/unread", **_auth(ungranted["peer_id"]))
    assert refused.status_code == 403
    assert client.get("/v1/mesh/unread", **_auth(granted["peer_id"])).status_code == 200


def test_036_rollback_removes_unread_usage_rows_and_restores_035_check(owner):
    """The down script deletes the counts of the route it added before restoring 035's CHECK.
    MUTANT U10: leave `unread` rows behind; the restored CHECK refuses the rollback."""
    peer = _peer(owner, "unread-rollback")
    owner.execute("INSERT INTO mesh_route_usage (route, day, peer_id, outcome, calls) "
                  "VALUES ('unread', current_date, %s, 'ok', 1)", (peer["peer_id"],))
    try:
        rolled = _sqlmigrate("--rollback-to", "meshychat:35")
        assert rolled.returncode == 0, rolled.stderr
        left = owner.execute("SELECT count(*) FROM mesh_route_usage WHERE route = 'unread'").fetchone()[0]
        check = owner.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE "
                              "conname = 'mesh_route_usage_route_check'").fetchone()[0]
        assert left == 0 and "'unread'" not in check and "'brief'" in check, check
    finally:
        restored = _sqlmigrate()
        assert restored.returncode == 0, restored.stderr


def test_the_reads_cte_is_materialized():
    """Divergence 081 (kxxx's timing on a production copy, msg/<key>): when a NOT
    EXISTS read `reads`, Postgres inlined it and re-ran the skill_calls scan per pending message (16.2 s
    median for the fleet counts at days=7, 1.9 s materialized). Since 3 Nov 2026 only `cleared` reads it,
    once, and it stays MATERIALIZED so that a second reference can never re-run the scan. A string check;
    the plan itself is held by test_the_reads_and_cleared_are_scanned_once_in_the_routes_own_context.
    MUTANT U20: drop MATERIALIZED."""
    from apps.mesh import graph
    assert "WITH reads AS MATERIALIZED (" in graph._UNREAD_SQL


def _plan_nodes(node: dict):
    yield node
    for child in node.get("Plans", ()):
        yield from _plan_nodes(child)


def test_the_reads_and_cleared_are_scanned_once_in_the_routes_own_context(owner):
    """3 Nov 2026 (kxxx, production, read-only EXPLAIN ANALYZE): as meshychat_app in server_internal, the
    context every caller of graph.unread runs in, skill_calls' SELECT policies made the planner expect ONE
    read where a heavy reader had 21,288, and it chose a Nested Loop Anti Join that rescanned `reads` once
    per pending message: 1,622 loops, 19.4 M rows through the join filter, 8.3 s for one recipient label
    (the /fleet pane timed out on it). The owner role, which bypasses RLS, got a hash and 0.89 s, which is
    why the earlier timing (divergence 081) never saw it. The same mechanism as OI-45 above in
    tests/test_call_retention.py: a policy qual in the estimate.

    So this runs the REAL graph.unread on an application connection inside request_server_internal, with
    NOTHING disabled, and turns the statement into EXPLAIN ANALYZE before running it for real. Held: every
    scan of `reads` and of `cleared` ran ONCE, so the work is linear in the reads and the mail whatever the
    estimates say. POSITIVE CONTROLS: the reads scan really returned every planted read (a once-run empty
    scan would hold vacuously), and the answer is exactly the six messages not read, from twelve.
    MUTANTS U21 (the pre-fix NOT EXISTS against `reads`: rescanned per message) and U22 (`cleared` joined
    as a one-row CTE: rescanned per message, copying the whole object each time)."""
    from apps.core import dbconn
    from apps.mesh import graph

    a, b = _peer(owner, "sa"), _peer(owner, "sb")
    target = _target(owner)
    keys = [_mail(owner, a, to=[b["name"]], minutes_ago=60 - i) for i in range(12)]
    P.rebuild(owner)
    for k in keys[:6]:
        _read(owner, b, target, k)
    for i in range(14):                                    # repeated reads, as a polling seat makes them
        _read(owner, b, target, keys[i % 6], returned=1)
    planted_reads = 6 + 14

    plans: list[dict] = []

    class _ExplainThenRun(psycopg.Cursor):
        def execute(self, query, params=None, **kw):
            if isinstance(query, str) and query.lstrip().startswith("WITH reads AS"):
                super().execute("EXPLAIN (ANALYZE, FORMAT JSON) " + query, params, **kw)
                row = self.fetchone()                      # graph.unread asks for dict rows
                plans.append((row["QUERY PLAN"] if isinstance(row, dict) else row[0])[0]["Plan"])
            return super().execute(query, params, **kw)

    conn = dbconn.connect(DSN)
    try:
        with dbconn.request_server_internal(conn) as c:
            assert c.execute("SELECT current_user").fetchone()[0] == "meshychat_app"
            c.cursor_factory = _ExplainThenRun
            result = graph.unread(c, [b["name"]], peer_id=b["peer_id"],
                                  since=NOW() - _dt.timedelta(days=7), limit=50)
    finally:
        conn.close()
    assert len(plans) == 1, plans
    assert result.total == 6 and {i["key"] for i in result.items} == set(keys[6:]), result
    scans = [(n["CTE Name"], n["Actual Loops"], n["Actual Rows"]) for n in _plan_nodes(plans[0])
             if n["Node Type"] == "CTE Scan"]
    # At most once: an InitPlan whose arm is never reached (OR stops early) reports 0 loops, and that is fine.
    assert all(loops <= 1 for _, loops, _ in scans), f"a CTE rescanned per message: {scans}"
    assert [loops for name, loops, _ in scans if name == "reads"] == [1], scans
    assert any(name == "reads" and rows == planted_reads for name, _, rows in scans), scans
    assert any(name == "cleared" and loops == 1 for name, loops, _ in scans), scans


def test_a_read_whose_args_carry_no_key_neither_fails_nor_clears(client, owner):
    """`cleared` keys a jsonb object by the read's key, and a NULL object key is an ERROR, not a
    skipped row: one stored read with no key in its args would make every call of this peer's backlog a
    500. Before 3 Nov 2026 such a read simply matched nothing (NULL = key is never true). Held: the route
    answers, and the keyless read clears nothing. CONTROL: a read of the same message WITH its key clears
    it, so the first answer is a reading. MUTANT U23: drop `WHERE key IS NOT NULL` from `cleared`."""
    a, b = _peer(owner, "nk"), _peer(owner, "nkb")
    target = _target(owner)
    key = _mail(owner, a, to=[b["name"]])
    P.rebuild(owner)
    _read(owner, b, target, None)
    assert _keys(_unread(client, b)) == [key], "a read with no key clears nothing and fails nothing"
    _read(owner, b, target, key)
    assert _keys(_unread(client, b)) == []


def test_one_key_read_every_way_is_cleared_only_by_a_read_that_covers_its_head(client, owner):
    """`cleared` keeps, per key, the maximum of each of the three deciding fields, each over its OWN kind
    of read only (returned; asked, when nothing was returned and a version was given; the read's time,
    when nothing was returned and the head was asked for). The NOT EXISTS it replaces asked whether ANY
    one read cleared the message; the maxima answer the same only while the kinds stay apart. Here one
    edited message is read every way that does NOT cover version 2: one returned version 1 after the edit
    (a race), one asked for version 1 with nothing returned, one asked for the head before the edit with
    nothing returned. Each field's maximum is then one that clears nothing, and the message is pending.
    CONTROL: a head read after the edit with nothing returned clears it (the time rule, on its own)."""
    a, b = _peer(owner, "mx"), _peer(owner, "mxb")
    target = _target(owner)
    t0 = NOW() - _dt.timedelta(minutes=30)
    key, rid = _row_at(owner, a, {"to": [b["name"]], "body": "v1"}, t0)
    edited_at = t0 + _dt.timedelta(minutes=10)
    _edit_row_at(owner, a, key, {"to": [b["name"]], "body": "v2"}, rid, edited_at)
    P.rebuild(owner)
    _read(owner, b, target, key, returned=1, at=edited_at + _dt.timedelta(minutes=2))
    _read(owner, b, target, key, version=1, returned=None, at=edited_at + _dt.timedelta(minutes=3))
    _read(owner, b, target, key, returned=None, at=t0 + _dt.timedelta(minutes=5))
    assert key in _keys(_unread(client, b)), "no read covers version 2"
    _read(owner, b, target, key, returned=None, at=edited_at + _dt.timedelta(minutes=4))
    assert key not in _keys(_unread(client, b))


def test_the_largest_version_read_decides_and_a_later_smaller_read_does_not_undo_it(client, owner):
    """`cleared` keeps each field's MAXIMUM, whatever order the reads came in. Two edited messages, each read
    first at version 2 and LATER at version 1: by the version asked for (nothing returned), and by the version
    returned. Each was covered by its first read, so each stays cleared: a later read that covers less never
    makes a message pending again (the old NOT EXISTS asked whether ANY read cleared it). CONTROL, beside it:
    a third message read only at version 1 is pending, so the two answers are readings.
    MUTANT U24: min(asked), which no other test catches (a key never had two reads on the asked arm)."""
    a, b = _peer(owner, "lg"), _peer(owner, "lgb")
    target = _target(owner)
    t0 = NOW() - _dt.timedelta(minutes=30)
    edited_at = t0 + _dt.timedelta(minutes=10)
    keys = []
    for body in ("asked", "returned", "control"):
        key, rid = _row_at(owner, a, {"to": [b["name"]], "body": body}, t0)
        _edit_row_at(owner, a, key, {"to": [b["name"]], "body": body + " v2"}, rid, edited_at)
        keys.append(key)
    k_asked, k_returned, k_control = keys
    P.rebuild(owner)
    _read(owner, b, target, k_asked, version=2, returned=None, at=edited_at + _dt.timedelta(minutes=1))
    _read(owner, b, target, k_asked, version=1, returned=None, at=edited_at + _dt.timedelta(minutes=2))
    _read(owner, b, target, k_returned, version=2, returned=2, at=edited_at + _dt.timedelta(minutes=1))
    _read(owner, b, target, k_returned, version=1, returned=1, at=edited_at + _dt.timedelta(minutes=2))
    _read(owner, b, target, k_control, version=1, returned=None, at=edited_at + _dt.timedelta(minutes=1))
    assert _keys(_unread(client, b)) == [k_control]


def test_a_head_read_at_the_heads_own_microsecond_clears_it_and_one_before_does_not(client, owner):
    """The time arm is `>=`, to the microsecond: a head read (nothing returned) made at exactly the moment the
    head version was written fetched it, and one made a microsecond earlier did not. `cleared` carries the
    read's time as an epoch numeric, so nothing may round on the way: the edit is placed at a time with all
    six fractional digits set. MUTANTS U25 (`>`) and U26 (the read's time kept to the millisecond)."""
    a, b = _peer(owner, "us"), _peer(owner, "usb")
    target = _target(owner)
    t0 = (NOW() - _dt.timedelta(minutes=30)).replace(microsecond=0)
    key, rid = _row_at(owner, a, {"to": [b["name"]], "body": "v1"}, t0)
    edited_at = t0 + _dt.timedelta(minutes=10, microseconds=123_457)
    _edit_row_at(owner, a, key, {"to": [b["name"]], "body": "v2"}, rid, edited_at)
    P.rebuild(owner)
    _read(owner, b, target, key, returned=None, at=edited_at - _dt.timedelta(microseconds=1))
    assert key in _keys(_unread(client, b)), "a head read one microsecond before the edit fetched version 1"
    _read(owner, b, target, key, returned=None, at=edited_at)
    assert key not in _keys(_unread(client, b)), "a head read at the edit's own microsecond fetched version 2"
