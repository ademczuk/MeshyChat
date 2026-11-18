"""Divergence 110: GET /v1/cards reads the narrow head record `card_heads` (sql_meshychat/057), never a value, to
decide what to list, and the record is kept by the database on every write.

THREE CLAIMS, each with its test and each RED-proved here against a broken variant:

  (a) EQUIVALENCE. On a seeded namespace that holds supersession chains, overrules (one of a card nothing else names), a redacted namer and a redacted
      target written through the REAL memory.write and memory.redact, a tombstone written before divergence 092, a
      namer earlier than its target, a `supersedes` that is a string, holds a missing key, a number and a null, cards
      with no seat, a number for a seat, a number for a time and no time, a `text` that is not a string, ties on
      `time`, a second version that moved a card's thread, a card recorded long after its `time`, fan-in past the
      NAMED_CAP cut of both kinds, and a second namespace whose cards name ours, `store.list_cards`,
      `store.superseders` and `store.replacers` return exactly what the code before 110 returned. That code's SQL is
      kept below, verbatim from origin/main ab2da2de, as the oracle. Five mutants of the new SQL are each caught.
      A walk across a tie on `time` matches the Python order with the page index dropped too, so the page's own
      ORDER BY must carry the key tie-break (the index supplies it under the chosen plan); its mutant is caught.
  (b) MAINTENANCE. Through each real write path (memory.write of a first and a later version, a superseding card,
      memory.redact, a delete that cascades, a write rolled back) the record is exactly what an independent
      derivation from the head rows says. Two broken triggers are each caught. A head retired with nothing after
      it loses its record (the trigger's other branch, which every real append hides).
  (c) COST. With 20,000 card heads of about 1.4 KB beside 40,000 other rows, a page at limit 1 and at limit 100 costs
      about what it does at 1,000 heads, and no plan node reads more shared_memory rows than the page holds.

Run:  .venv/Scripts/python.exe -m pytest tests/test_cards_head_record.py -q -p no:cacheprovider
"""
from __future__ import annotations

import hashlib
import inspect
import json
import statistics
import time as clock
import uuid
from pathlib import Path

import pytest
from psycopg.rows import dict_row

from tests.test_memory_skills import (  # noqa: F401 - fixtures are used by name; sets up Django
    _call,
    _claim,
    _peer,
    app_conn,
    owner_conn,
)
from apps.cards import store  # noqa: E402
from apps.core import rls  # noqa: E402
from apps.memoryskills import handlers  # noqa: E402
from tests.fixtures import history_rows  # noqa: E402
from tests.fixtures import signed_rows as sr  # noqa: E402


# ============================================================================ the oracle: the code before 110
# Verbatim from apps/cards/store.py at origin/main ab2da2de (comments dropped). Do not "fix" it: it is the
# definition of what the list returned.

OLD_EDGES = (
    "SELECT sm.key AS namer, tgt.key AS target, tgt.value->>'type' AS target_type, "
    "       (sm.value->>'seat' IS NOT DISTINCT FROM tgt.value->>'seat') AS same_seat, "
    "       (jsonb_typeof(sm.value->'text') IS DISTINCT FROM 'string') AS namer_redacted "
    "FROM shared_memory sm "
    "CROSS JOIN LATERAL jsonb_array_elements_text("
    "  CASE WHEN jsonb_typeof(sm.value->'supersedes') = 'array' THEN sm.value->'supersedes' "
    "       ELSE '[]'::jsonb END) AS t(target) "
    "JOIN shared_memory tgt ON tgt.namespace = sm.namespace AND tgt.key = t.target AND tgt.is_head "
    "  AND (jsonb_typeof(tgt.value->'text') = 'string' OR tgt.value->'redacted' = 'true'::jsonb) "
    "  AND tgt.value->>'time' < sm.value->>'time' "
    "WHERE sm.namespace = %(ns)s AND sm.is_head AND sm.key LIKE 'card/%%' "
    "  AND (jsonb_typeof(sm.value->'text') = 'string' OR sm.value->'redacted' = 'true'::jsonb)")
OLD_AGG = ("SELECT e.target, "
           "(array_agg(DISTINCT e.namer ORDER BY e.namer) FILTER (WHERE e.same_seat))[1:%(named_cap)s::int] AS by_keys, "
           "COALESCE(cardinality(array_agg(DISTINCT e.namer ORDER BY e.namer) FILTER (WHERE e.same_seat)), 0) "
           "  AS by_count, "
           "(array_agg(DISTINCT e.namer ORDER BY e.namer) FILTER (WHERE NOT e.same_seat))[1:%(named_cap)s::int] "
           "  AS overruled_by, "
           "COALESCE(cardinality(array_agg(DISTINCT e.namer ORDER BY e.namer) FILTER (WHERE NOT e.same_seat)), 0) "
           "  AS overruled_count, "
           "(array_agg(DISTINCT e.namer ORDER BY e.namer) FILTER (WHERE e.namer_redacted))[1:%(named_cap)s::int] "
           "  AS by_redacted, "
           "COALESCE(cardinality(array_agg(DISTINCT e.namer ORDER BY e.namer) FILTER (WHERE e.namer_redacted)), 0) "
           "  AS redacted_count "
           "FROM e GROUP BY e.target")
OLD_BY_TARGET = f"WITH e AS ({OLD_EDGES}) {OLD_AGG}"
OLD_NAMED = "s.by_keys, s.by_count, s.overruled_by, s.overruled_count, s.by_redacted, s.redacted_count"
OLD_REACH = ("SELECT e.namer FROM e WHERE e.target_type = ANY(%(types)s) "
             "UNION SELECT e.namer FROM e JOIN reach r ON e.target = r.key")


def old_list_sql(ns, *, types=(), seat=None, owner=None, thread=None, status="active", since=None, until=None,
                 recorded_since=None, limit=store.DEFAULT_LIMIT, after=None):
    where = ["sm.namespace = %(ns)s", "sm.is_head", "sm.key LIKE 'card/%%'",
             "jsonb_typeof(sm.value->'text') = 'string'", "jsonb_typeof(sm.value->'time') = 'string'"]
    params: dict = {"ns": ns, "limit": int(limit) + 1, "named_cap": store.NAMED_CAP}
    replaces = "FALSE"
    if types:
        params["types"] = list(types)
        replaces = "(sm.key IN (SELECT r.key FROM reach r))"
        where.append(f"(sm.value->>'type' = ANY(%(types)s) OR {replaces})")
    if seat is not None:
        where.append("sm.value->>'seat' = %(seat)s")
        params["seat"] = seat
    if owner is not None:
        where.append("sm.value->>'owner' = %(owner)s")
        params["owner"] = owner
    if thread is not None:
        where.append("sm.value->>'thread_id' = %(thread)s")
        params["thread"] = thread
    if since is not None:
        where.append("sm.value->>'time' >= %(since)s")
        params["since"] = since
    if until is not None:
        where.append("sm.value->>'time' < %(until)s")
        params["until"] = until
    if recorded_since is not None:
        where.append("sm.created_at >= %(recorded_since)s::timestamptz")
        params["recorded_since"] = recorded_since
    if after is not None:
        where.append("(sm.value->>'time', sm.key) < (%(a_time)s, %(a_key)s)")
        params["a_time"], params["a_key"] = after
    if status == "active":
        where.append("sm.key NOT IN (SELECT s.target FROM s WHERE s.by_count > 0)")
    elif status == "superseded":
        where.append("sm.key IN (SELECT s.target FROM s WHERE s.by_count > 0)")
    reach = f", reach(key) AS ({OLD_REACH}) " if types else " "
    sql = (f"WITH RECURSIVE e AS MATERIALIZED ({OLD_EDGES}), "
           f"s AS MATERIALIZED ({OLD_AGG})"
           f"{reach}"
           f", page AS (SELECT sm.key, sm.version, sm.value, sm.created_at, {replaces} AS replaces_requested "
           f"           FROM shared_memory sm WHERE {' AND '.join(where)} "
           "           ORDER BY sm.value->>'time' DESC, sm.key DESC LIMIT %(limit)s) "
           f"SELECT page.key, page.version, page.value, page.created_at, page.replaces_requested, {OLD_NAMED} "
           "FROM page LEFT JOIN s ON s.target = page.key "
           "ORDER BY page.value->>'time' DESC, page.key DESC")
    return sql, params


def old_list_cards(conn, ns, *, limit=store.DEFAULT_LIMIT, **flt) -> dict:
    """The page as the code before 110 built it: its SQL, and the module's unchanged post-processing."""
    sql, params = old_list_sql(ns, limit=limit, **flt)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    cards = []
    for r in rows:
        card = dict(r["value"])
        card.update({"key": r["key"], "version": r["version"], "written_at": r["created_at"].isoformat()})
        card.update(store._named_fields(r))
        if r["replaces_requested"]:
            card["replaces_requested"] = True
        tier = store.thread_tier(r["value"])
        if tier is not None:
            card["thread_tier"] = tier
        cards.append(card)
    return {"cards": cards,
            "next": store.encode_cursor(rows[-1]["value"]["time"], rows[-1]["key"]) if more and rows else None}


def old_superseders(conn, ns):
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(OLD_BY_TARGET, {"ns": ns, "named_cap": store.NAMED_CAP})
        return {row["target"]: list(row["by_keys"] or []) for row in cur.fetchall() if row["by_count"]}


def old_replacers(conn, ns, types):
    if not types:
        return set()
    with conn.cursor() as cur:
        cur.execute(f"WITH RECURSIVE e AS MATERIALIZED ({OLD_EDGES}), reach(key) AS ({OLD_REACH}) SELECT key FROM reach",
                    {"ns": ns, "types": list(types)})
        return {row[0] for row in cur.fetchall()}


# ============================================================================ seeding

def T(hh, mm=0, ss=0, ms=0, day=8) -> str:
    return f"2026-10-{day:02d}T{hh:02d}:{mm:02d}:{ss:02d}.{ms:03d}Z"


def _value(seat, text, time, *, card_type="Ruling", **more) -> dict:
    card_id = uuid.uuid4().hex
    value = {"schema": "card.v1", "id": card_id, "type": card_type, "owner": "operator", "seat": seat,
             "time": time, "text": text, "text_sha256": hashlib.sha256(str(text).encode()).hexdigest(),
             "source": {"record_uuid": str(uuid.uuid4()), "block_index": 0, "kind": "answered_question",
                        "first_hand": True}}
    value.update(more)
    return value


def _without(value: dict, *fields) -> dict:
    return {k: v for k, v in value.items() if k not in fields}


def _ckey(seat) -> str:
    return f"card/{seat if isinstance(seat, str) else 'x'}/{uuid.uuid4().hex}"


def _raw(owner_conn, peer, ns, key, value, **override):
    """A head row inserted as a test sets up a world (tests/fixtures/signed_rows), past the card write rules: the
    shapes memory.write refuses today but a row of the namespace can still hold."""
    rid = sr.insert(owner_conn, sr.row(peer["peer_id"], peer["owner_id"], ns, key, value, **override))
    owner_conn.commit()
    return rid


def _raw_next(owner_conn, peer, ns, key, value):
    """A later version of `key`, written as memory.write appends: retire the head, insert version+1."""
    vid, ver = owner_conn.execute("SELECT id, version FROM shared_memory WHERE namespace = %s AND key = %s AND is_head",
                                  (ns, key)).fetchone()
    owner_conn.execute("UPDATE shared_memory SET is_head = FALSE WHERE id = %s", (vid,))
    rid = sr.insert(owner_conn, sr.row(peer["peer_id"], peer["owner_id"], ns, key, value, version=ver + 1,
                                       parent_id=vid))
    owner_conn.commit()
    return rid


def _write(app_conn, peer, ns, key, value):
    return _call(app_conn, peer, "memory.write", {"namespace": ns, "key": key, "value": value})


def _redact(app_conn, peer, ns, key):
    return _call(app_conn, peer, "memory.redact", {"namespace": ns, "key": key, "reason_code": "credential"})


@pytest.fixture()
def cards(app_conn, owner_conn, monkeypatch):
    ns = f"shared/cards-{uuid.uuid4().hex[:12]}"
    seat = _peer(owner_conn, "rec")
    _claim(app_conn, seat, ns)
    monkeypatch.setenv("MESHY_CARDS_NAMESPACE", ns)
    monkeypatch.setattr(handlers, "_ROSTER_LABELS", {})
    return ns, seat, f"peer-{str(seat['peer_id'])[:8]}"


@pytest.fixture()
def world(app_conn, owner_conn, cards):
    """The seeded namespace (see the module docstring); returns (ns, names) where names maps a role to a key."""
    ns, seat, label = cards
    k = {}

    def real(name, value):
        key = f"card/{label}/{value['id']}"
        _write(app_conn, seat, ns, key, value)
        k[name] = key
        return key

    def raw(name, value, key=None, **override):
        key = key or _ckey(value.get("seat") if isinstance(value, dict) else None)
        _raw(owner_conn, seat, ns, key, value, **override)
        k[name] = key
        return key

    # Real writes, through memory.write and memory.redact: a chain, a later version, redacted namer and target.
    real("r1", _value(label, "Ruling | r1", T(7)))
    real("r2", _value(label, "Ruling | r2 replaces r1", T(8), supersedes=[k["r1"]]))
    real("r3", _value(label, "Candidate | r3 corrects r2", T(9), card_type="Candidate", supersedes=[k["r2"]]))
    real("r5", _value(label, "Ruling | r5 first", T(10)))
    v2 = _value(label, "Ruling | r5 second version", T(10), supersedes=[])
    v1 = json.loads(json.dumps(owner_conn.execute(
        "SELECT value FROM shared_memory WHERE namespace = %s AND key = %s AND is_head", (ns, k["r5"])).fetchone()[0]))
    owner_conn.commit()
    v2.update({"id": v1["id"], "source": v1["source"]})
    _write(app_conn, seat, ns, k["r5"], v2)
    real("r6", _value(label, "Ruling | r6 replaces r5, then redacted", T(11), supersedes=[k["r5"]]))
    _redact(app_conn, seat, ns, k["r6"])
    real("r7", _value(label, "Ruling | r7, later redacted", T(6)))
    real("r8", _value(label, "Ruling | r8 replaces r7", T(6, 30), supersedes=[k["r7"]]))
    _redact(app_conn, seat, ns, k["r7"])

    # Raw rows: other seats, and the shapes the write rules refuse.
    raw("j1", _value("kxxx", "Ruling | j1", T(7), thread_id="T1"))
    raw("j2", _value("kxxx", "Commitment | j2 same time as j1", T(7), card_type="Commitment", owner="kxxx",
                     thread_id="T1", source={"record_uuid": str(uuid.uuid4()), "block_index": 0,
                                             "kind": "question_hook", "first_hand": True}))
    raw("j3", _value("kxxx", "OpenItem | j3", T(7), card_type="OpenItem", thread_id="T2"))
    raw("j4", _value("kxxx", "Ruling | j4 replaces j1 with odd elements", T(9),
                     supersedes=[k["j1"], "card/kxxx/" + "0" * 32, 5, None]))
    raw("n1", _value("pxx", "Ruling | n1 overrules j1", T(9, 30), supersedes=[k["j1"]]))
    raw("n2", _value("pxx", "Ruling | n2 earlier than its target", T(6), supersedes=[k["j3"]]))
    raw("n3", _value("pxx", "Constraint | n3 supersedes is a string", T(8), card_type="Constraint",
                     supersedes=k["j3"]))
    raw("n4", _value("pxx", "Ruling | n4 numeric time", 1234, supersedes=[k["j1"]]))
    raw("n5", _without(_value("pxx", "Ruling | n5 no time", None), "time"))
    raw("x2", _without(_value(None, "Ruling | x2 no seat", T(10)), "seat"))
    raw("x1", _without(_value(None, "Ruling | x1 no seat replaces x2", T(10, 30), supersedes=[k["x2"]]), "seat"))
    raw("e1", _value("kxxx", "Ruling | e1", T(14)))
    raw("e2", _value("kxxx", "Ruling | e2 names e1 at the same instant", T(14), supersedes=[k["e1"]]))
    raw("o1", _value("kxxx", "Ruling | o1, only overruled", T(15)))
    raw("o2", _value("pxx", "Ruling | o2 overrules o1", T(16), supersedes=[k["o1"]]))
    raw("x3", _value(7, "Decision | x3 numeric seat", T(5), card_type="Decision"))
    raw("x4", _value("pxx", 5, T(12)))                                           # text that is not a string
    raw("t1", {"redacted": True, "reason_code": "test"}, key=_ckey("kxxx"))      # a tombstone before 092
    raw("t2", {"redacted": True, "reason_code": "test", "type": "Ruling", "seat": "kxxx", "time": T(12),
               "supersedes": [k["j4"]]}, key=_ckey("kxxx"))
    raw("c1", _value("kxxx", "Ruling | c1 June transcript, recorded 1 Oct", T(13, day=1)[:5] + "06-14T13:01:37.642Z"),
        created_at="2026-10-01T00:00:00Z")
    raw("c2", _value("kxxx", "Ruling | c2 June transcript, recorded now", "2026-06-15T09:00:00.000Z"))
    # A second version of j2 that moved its thread: the filters read the head.
    _raw_next(owner_conn, seat, ns, k["j2"], _value("kxxx", "Commitment | j2 moved to T2", T(7),
                                                    card_type="Commitment", owner="kxxx", thread_id="T2"))
    # Fan-in past the cut: 34 own-seat namers and 33 of another seat name f0.
    raw("f0", _value("kxxx", "Ruling | f0 named 67 times", T(3)))
    for i in range(34):
        raw(f"fa{i}", _value("kxxx", f"Ruling | fa{i}", T(4, i // 60, i % 60), supersedes=[k["f0"]]))
    for i in range(33):
        raw(f"fb{i}", _value("pxx", f"Ruling | fb{i}", T(4, 30, i), supersedes=[k["f0"]]))

    # A second namespace whose card names one of ours, later: it must not reach this namespace.
    ns2 = f"shared/cards2-{uuid.uuid4().hex[:12]}"
    _claim(app_conn, seat, ns2)
    _raw(owner_conn, seat, ns2, _ckey("kxxx"), _value("kxxx", "Ruling | elsewhere", T(23), supersedes=[k["j3"]]))
    _raw(owner_conn, seat, ns2, k["j3"], _value("kxxx", "Ruling | same key elsewhere", T(1)))
    return ns, k, label


# ============================================================================ (a) equivalence

FILTERS = []
for _st in store.STATUSES:
    for _ty in [(), ("Ruling",), ("Ruling", "Constraint", "Commitment", "OpenItem"), ("Candidate",), ("Decision",)]:
        for _seat in [None, "kxxx", "pxx", "LABEL"]:
            FILTERS.append({"status": _st, "types": _ty, "seat": _seat})
    for _extra in [{"owner": "operator"}, {"owner": "kxxx"}, {"thread": "T1"}, {"thread": "T2"},
                   {"since": T(7)}, {"until": T(9)}, {"since": T(6, 30), "until": T(10)},
                   {"recorded_since": "2026-10-05T00:00:00.000Z"}, {"recorded_since": "2026-09-01T00:00:00.000Z"},
                   {"types": ("Ruling",), "thread": "T1", "owner": "operator"}]:
        FILTERS.append({"status": _st, **_extra})


def _resolve(flt, label):
    return {k: (label if v == "LABEL" else v) for k, v in flt.items()}


def _walk(fn, conn, ns, limit, **flt):
    """Every page of one filter, following `next`: a list of pages."""
    pages, after = [], None
    for _ in range(500):
        page = fn(conn, ns, limit=limit, after=after, **flt)
        pages.append(page)
        if page["next"] is None:
            return pages
        after = store.decode_cursor(page["next"])
    raise AssertionError("no end to the pages")


def differences(conn, ns, label, new_list=None) -> list[str]:
    """Every filter of FILTERS on which the new list (store.list_cards, or `new_list`) and the oracle disagree, plus
    the paged walks at limits 1 and 3 of the plain filters, and superseders and replacers. Empty when equal. A new
    list that raises counts as a difference."""
    new_list = new_list or store.list_cards
    out = []

    def run(fn, **kw):
        with rls.server_internal(conn):
            return fn(conn, ns, **kw)

    for flt in FILTERS:
        flt = _resolve(flt, label)
        want = run(old_list_cards, limit=200, **flt)
        try:
            got = run(new_list, limit=200, **flt)
        except Exception as exc:        # a mutant may break the SQL; that is a difference too
            out.append(f"{flt}: raised {type(exc).__name__}")
            continue
        if got != want:
            out.append(f"{flt}: {[c['key'] for c in got['cards']]} != {[c['key'] for c in want['cards']]}")
    for status in store.STATUSES:
        for limit in (1, 3):
            for seat in (None, "kxxx"):
                with rls.server_internal(conn):
                    want = _walk(old_list_cards, conn, ns, limit, status=status, seat=seat)
                try:
                    with rls.server_internal(conn):
                        got = _walk(new_list, conn, ns, limit, status=status, seat=seat)
                except Exception as exc:
                    out.append(f"walk {status} {limit} {seat}: raised {type(exc).__name__}")
                    continue
                if got != want:
                    out.append(f"walk {status} {limit} {seat}: pages differ")
    with rls.server_internal(conn):
        if store.superseders(conn, ns) != old_superseders(conn, ns):
            out.append("superseders differ")
        for types in [("Ruling",), ("Candidate",), ("Ruling", "Constraint")]:
            if store.replacers(conn, ns, types) != old_replacers(conn, ns, types):
                out.append(f"replacers {types} differ")
    return out


def test_the_seed_exercises_what_it_claims(app_conn, owner_conn, world):
    """CONTROL for the equivalence test: the seeded world holds each case its docstring names, as the oracle reads it,
    so equal answers are not equal empties."""
    ns, k, label = world
    with rls.server_internal(app_conn):
        anyp = {c["key"]: c for c in old_list_cards(app_conn, ns, limit=200, status="any")["cards"]}
        sup = old_superseders(app_conn, ns)
    assert anyp[k["r1"]]["status"] == "superseded" and anyp[k["r2"]]["status"] == "superseded"   # a chain
    assert anyp[k["r5"]]["named_by_redacted"] == [k["r6"]] and anyp[k["r5"]]["version"] == 2     # redacted namer, v2
    assert k["r7"] not in anyp and anyp[k["r8"]]["status"] == "active"                          # redacted target
    assert anyp[k["j1"]]["overruled_by"] == sorted([k["n1"]])                                    # an overrule
    assert anyp[k["j1"]]["status"] == "superseded" and k["j3"] not in sup                        # n2, n3: no edge
    assert anyp[k["x2"]]["status"] == "superseded"                                               # no seat on either
    assert k["n4"] not in anyp and k["n5"] not in anyp and k["x4"] not in anyp                   # time, text shapes
    assert anyp[k["j4"]]["status"] == "superseded" and anyp[k["j4"]]["named_by_redacted"] == [k["t2"]]
    assert anyp[k["f0"]]["truncated_lists"] is True and anyp[k["f0"]]["superseded_by_count"] == 34
    assert anyp[k["f0"]]["overruled_by_count"] == 33
    assert anyp[k["j2"]]["thread_id"] == "T2" and anyp[k["j2"]]["thread_tier"] == "declared"
    assert anyp[k["j3"]]["status"] == "active"
    assert anyp[k["e1"]]["status"] == "active", "an edge needs a STRICTLY later namer"
    assert anyp[k["o1"]]["status"] == "active" and anyp[k["o1"]]["overruled_by"] == [k["o2"]]
    assert len(anyp) >= 80, len(anyp)


def test_the_list_superseders_and_replacers_answer_as_the_code_before_110(app_conn, owner_conn, world):
    """(a) Every filter in FILTERS (three statuses by five type sets by four seats, and owner, thread, both windows,
    `recorded_since` and a combination, under each status), each read at limit 200 and the plain ones walked page by
    page at limits 1 and 3: the new list is the oracle's, card for card and field for field, cursors included."""
    ns, k, label = world
    assert differences(app_conn, ns, label) == []


def _mutant_list(old: str, new: str):
    """store.list_cards with one piece of its SQL text replaced (the RED proofs of (a))."""
    src = inspect.getsource(store.list_cards)
    assert old in src, f"the mutant's target is not in list_cards: {old!r}"
    scope = dict(vars(store))
    exec(compile(src.replace(old, new, 1), "<mutant>", "exec"), scope)
    return scope["list_cards"]


@pytest.mark.parametrize("name,target,old,new", [
    ("M1 a tombstone is no target", "edges", "(t.has_text OR t.redacted)", "t.has_text"),
    ("M2 an edge at the same instant", "edges", "t.card_time < n.card_time", "t.card_time <= n.card_time"),
    ("M3 two cards with no seat are not one seat", "edges", "(n.seat IS NOT DISTINCT FROM t.seat)", "(n.seat = t.seat)"),
    ("M4 a time that is not a string is listed", "list", '"h.time_is_string"', '"TRUE"'),
    ("M5 another seat's card hides", "list", "WHERE e.same_seat)", "WHERE TRUE)"),
])
def test_RED_each_broken_variant_of_the_new_sql_is_caught(app_conn, owner_conn, world, monkeypatch,
                                                         name, target, old, new):
    """(a) RED-proved: each mutant of the new SQL makes `differences` non-empty on this seed. M1 to M3 break the edge
    pass (which list_cards, superseders and replacers share); M4 and M5 the page."""
    ns, k, label = world
    if target == "edges":
        assert old in store._EDGES
        monkeypatch.setattr(store, "_EDGES", store._EDGES.replace(old, new))
        found = differences(app_conn, ns, label)
    else:
        found = differences(app_conn, ns, label, new_list=_mutant_list(old, new))
    assert found, f"{name}: the equivalence test did not see it"


# ---------------------------------------------------------------------------- (a) the tie-break, whatever the plan

def _without_page_index(owner_conn, fn):
    """fn(owner_conn) in ONE transaction of the owner with card_heads_page_idx dropped, rolled back after. Setting the
    index paths off is not enough: list_cards turns sorts off, so a scan and a sort is as disabled as the index walk
    and the planner still picks the index once the table holds a few hundred records. Without the index the page's
    order can only come from its own ORDER BY."""
    owner_conn.rollback()
    try:
        owner_conn.execute("DROP INDEX card_heads_page_idx")
        owner_conn.execute("SELECT set_config('app.server_internal', '1', true)")
        return fn(owner_conn)
    finally:
        owner_conn.rollback()


def _ties(owner_conn, cards) -> tuple[str, list[str]]:
    """Ten cards, eight of them at ONE instant, inserted in ascending key order (so a scan reads the tie group
    smallest key first); returns the namespace and the keys in the page order (time DESC, key DESC) worked out in
    Python, not by any SQL."""
    ns, seat, label = cards
    times = [T(8)] + [T(9)] * 8 + [T(10)]
    keys = sorted(f"card/{label}/{uuid.uuid4().hex}" for _ in times)    # a cursor's key must be a card key
    for key, time in zip(keys, times):
        _raw(owner_conn, seat, ns, key, _value(label, f"Ruling | {key[-10:]}", time))
    return ns, [k for _, k in sorted(zip(times, keys), reverse=True)]


def _tie_walks(app_conn, owner_conn, ns, list_fn=None) -> dict:
    """The keys of every page of the active list, walked at limits 1, 3 and 4 (each puts a page boundary inside the
    tie group): under the plan the code chooses, as the request role, and with the page index gone."""
    list_fn = list_fn or store.list_cards
    out = {}
    for limit in (1, 3, 4):
        with rls.server_internal(app_conn):
            pages = _walk(list_fn, app_conn, ns, limit)
        out[("chosen plan", limit)] = [[c["key"] for c in p["cards"]] for p in pages]
        pages = _without_page_index(owner_conn, lambda c, lim=limit: _walk(list_fn, c, ns, lim))
        out[("no page index", limit)] = [[c["key"] for c in p["cards"]] for p in pages]
    return out


def _tie_differences(app_conn, owner_conn, ns, want, list_fn=None) -> list[str]:
    out = []
    for (path, limit), pages in _tie_walks(app_conn, owner_conn, ns, list_fn).items():
        expect = [want[i:i + limit] for i in range(0, len(want), limit)]
        if pages != expect:
            out.append(f"{path}, limit {limit}: {pages} != {expect}")
    return out


def test_pages_split_a_tie_by_key_whatever_the_plan(app_conn, owner_conn, cards):
    """(a) The page's ORDER BY carries the key tie-break itself. Under the chosen plan the page walks
    card_heads_page_idx, which supplies (time DESC, key DESC) on its own, so a page ORDER BY without `h.key DESC`
    still passed every other test here. With that index gone the order comes from the sort alone: every page of a
    walk whose boundaries fall inside an eight-card tie must be the Python slice of (time DESC, key DESC)."""
    ns, want = _ties(owner_conn, cards)
    seen = {}
    _without_page_index(owner_conn, lambda c: store.list_cards(_Spy(c, seen), ns, limit=3))
    used = [n.get("Index Name") for n in _nodes(seen["plan"]) if n.get("Relation Name") == "card_heads"]
    assert used and "card_heads_page_idx" not in used,         f"control: the page index is gone for the second walk: {seen['text']}"
    assert _tie_differences(app_conn, owner_conn, ns, want) == []


def test_RED_a_page_order_without_the_key_tie_break_is_caught(app_conn, owner_conn, cards):
    """RED for the test above: drop `h.key DESC` from the page's ORDER BY. With the page index gone a bounded sort on
    the time alone keeps whichever tied cards it read first, so pages and their cursors skip cards."""
    ns, want = _ties(owner_conn, cards)
    mutant = _mutant_list("h.card_time DESC, h.key DESC LIMIT", "h.card_time DESC LIMIT")
    found = _tie_differences(app_conn, owner_conn, ns, want, list_fn=mutant)
    assert any(f.startswith("no page index") for f in found), found


# ============================================================================ (b) maintenance

#: The record as derived INDEPENDENTLY of meshy_card_head_put, from the head rows: what every write must leave.
EXPECTED = (
    "SELECT sm.namespace, sm.key, sm.id, sm.version, sm.created_at, "
    "       coalesce(jsonb_typeof(sm.value->'text') = 'string', false), "
    "       coalesce(sm.value->'redacted' = 'true'::jsonb, false), sm.value->>'time', "
    "       coalesce(jsonb_typeof(sm.value->'time') = 'string', false), "
    "       sm.value->>'type', sm.value->>'seat', sm.value->>'owner', sm.value->>'thread_id', "
    "       CASE WHEN jsonb_typeof(sm.value->'supersedes') = 'array' "
    "            THEN ARRAY(SELECT jsonb_array_elements_text(sm.value->'supersedes')) ELSE '{}'::text[] END "
    "FROM shared_memory sm WHERE sm.namespace = %s AND sm.is_head AND sm.key LIKE 'card/%%' ORDER BY 2")
RECORDS = ("SELECT namespace, key, memory_id, version, created_at, has_text, redacted, card_time, time_is_string, "
           "card_type, seat, owner, thread_id, supersedes FROM card_heads WHERE namespace = %s ORDER BY 2")


def in_sync(owner_conn, ns) -> bool:
    want = owner_conn.execute(EXPECTED, (ns,)).fetchall()
    got = owner_conn.execute(RECORDS, (ns,)).fetchall()
    owner_conn.commit()
    return want == got


def _maintenance_steps(app_conn, owner_conn, ns, seat, label) -> list[tuple[str, bool]]:
    """Each real write path in turn, and whether the record matched the head rows after it."""
    out = []
    a = _value(label, "Ruling | a", T(7))
    a_key = f"card/{label}/{a['id']}"
    _write(app_conn, seat, ns, a_key, a)
    out.append(("first version", in_sync(owner_conn, ns)))
    a2 = dict(a, text="Ruling | a, second version", text_sha256=hashlib.sha256(b"Ruling | a, second version").hexdigest(),
              thread_id="T9")
    _write(app_conn, seat, ns, a_key, a2)
    out.append(("later version", in_sync(owner_conn, ns)))
    b = _value(label, "Ruling | b replaces a", T(8), supersedes=[a_key])
    b_key = f"card/{label}/{b['id']}"
    _write(app_conn, seat, ns, b_key, b)
    out.append(("superseding card", in_sync(owner_conn, ns)))
    _redact(app_conn, seat, ns, b_key)
    out.append(("redaction", in_sync(owner_conn, ns)))
    c = _value(label, "Ruling | c, rolled back", T(9))
    c_key = f"card/{label}/{c['id']}"
    sr.insert(owner_conn, sr.row(seat["peer_id"], seat["owner_id"], ns, c_key, c))
    inside = owner_conn.execute("SELECT count(*) FROM card_heads WHERE namespace = %s AND key = %s",
                                (ns, c_key)).fetchone()[0]
    owner_conn.rollback()
    out.append(("in the writer's transaction", inside == 1))
    out.append(("rolled back write", in_sync(owner_conn, ns)))
    d_key = f"card/{label}/{uuid.uuid4().hex}"
    rid = _raw(owner_conn, seat, ns, d_key, _value(label, "Ruling | d, then deleted", T(10)))
    owner_conn.execute("DELETE FROM shared_memory WHERE id = %s", (rid,))
    owner_conn.commit()
    out.append(("delete (cascade)", in_sync(owner_conn, ns)))
    return out


def test_every_write_path_keeps_the_record_in_sync(app_conn, owner_conn, cards):
    """(b) After each real write (memory.write of a first and a later version, a superseding card, memory.redact, a
    write rolled back, a delete that cascades) the record is exactly the independent derivation from the head rows;
    the redaction's record says redacted with no text and keeps what it replaced."""
    ns, seat, label = cards
    steps = _maintenance_steps(app_conn, owner_conn, ns, seat, label)
    assert steps == [(name, True) for name, _ in steps], steps
    recs = owner_conn.execute("SELECT has_text, redacted, supersedes FROM card_heads WHERE namespace = %s "
                              "AND cardinality(supersedes) > 0", (ns,)).fetchall()
    owner_conn.commit()
    assert len(recs) == 1 and recs[0][0] is False and recs[0][1] is True and len(recs[0][2]) == 1, recs


def test_the_record_is_not_writable_by_the_request_role(app_conn):
    """The request role may read the record in the server-internal context and may not write it at all."""
    with rls.server_internal(app_conn):
        app_conn.execute("SELECT count(*) FROM card_heads").fetchone()
    with pytest.raises(Exception) as exc:
        with rls.server_internal(app_conn):
            app_conn.execute("DELETE FROM card_heads")
    assert "permission denied" in str(exc.value)


@pytest.mark.parametrize("variant", ["trigger disabled", "trigger on INSERT only"])
def test_RED_a_broken_trigger_is_caught_by_the_maintenance_test(app_conn, owner_conn, cards, variant):
    """(b) RED-proved: with no trigger, the first write leaves no record at all; with a trigger that
    fires on INSERT only, the redaction (an UPDATE of the value in place) leaves the record readable."""
    ns, seat, label = cards
    if variant == "trigger disabled":
        owner_conn.execute("ALTER TABLE shared_memory DISABLE TRIGGER shared_memory_card_head")
        expect_first_bad = "first version"
    else:
        owner_conn.execute("DROP TRIGGER shared_memory_card_head ON shared_memory")
        owner_conn.execute("CREATE TRIGGER shared_memory_card_head AFTER INSERT ON shared_memory FOR EACH ROW "
                           "WHEN (NEW.key LIKE 'card/%') EXECUTE FUNCTION public.meshy_card_head_track()")
        expect_first_bad = "redaction"
    owner_conn.commit()
    try:
        steps = _maintenance_steps(app_conn, owner_conn, ns, seat, label)
    finally:
        owner_conn.rollback()
        owner_conn.execute("DROP TRIGGER IF EXISTS shared_memory_card_head ON shared_memory")
        owner_conn.execute("CREATE TRIGGER shared_memory_card_head AFTER INSERT OR UPDATE ON shared_memory "
                           "FOR EACH ROW WHEN (NEW.key LIKE 'card/%' AND octet_length(NEW.key) <= 1024) "
                           "EXECUTE FUNCTION public.meshy_card_head_track()")
        owner_conn.commit()
    first_bad = next((name for name, ok in steps if not ok), None)
    assert first_bad == expect_first_bad, steps


def test_the_sync_function_repairs_a_key_written_past_the_trigger(owner_conn, cards):
    """meshy_card_head_sync (the repair 057 ships) makes a stale record right again."""
    ns, seat, label = cards
    key = f"card/{label}/{uuid.uuid4().hex}"
    _raw(owner_conn, seat, ns, key, _value(label, "Ruling | s", T(7)))
    owner_conn.execute("ALTER TABLE shared_memory DISABLE TRIGGER USER")
    owner_conn.execute("UPDATE shared_memory SET value = %s::jsonb WHERE namespace = %s AND key = %s",
                       ('{"redacted": true, "reason_code": "test"}', ns, key))
    owner_conn.execute("ALTER TABLE shared_memory ENABLE TRIGGER USER")
    owner_conn.commit()
    assert not in_sync(owner_conn, ns), "control: a write past every user trigger leaves the record stale"
    owner_conn.execute("SELECT public.meshy_card_head_sync(%s, %s)", (ns, key))
    owner_conn.commit()
    assert in_sync(owner_conn, ns)


def test_a_head_retired_with_no_successor_loses_its_record(owner_conn, cards):
    """(b) The trigger's other branch: a row that stops being the head removes the record that names it. Every real
    retirement (memory.write's append) is followed by the next version's insert, which puts the key's record again, so
    the maintenance test never sees this branch on its own. Here the head is retired and nothing follows."""
    ns, seat, label = cards
    key = f"card/{label}/{uuid.uuid4().hex}"
    rid = _raw(owner_conn, seat, ns, key, _value(label, "Ruling | retired, no successor", T(7)))
    before = owner_conn.execute("SELECT count(*) FROM card_heads WHERE memory_id = %s", (rid,)).fetchone()[0]
    owner_conn.commit()
    assert before == 1, "control: the head has its record before it is retired"
    owner_conn.execute("UPDATE shared_memory SET is_head = FALSE WHERE id = %s", (rid,))
    owner_conn.commit()
    left = owner_conn.execute("SELECT count(*) FROM card_heads WHERE namespace = %s AND key = %s",
                              (ns, key)).fetchone()[0]
    owner_conn.commit()
    assert left == 0, "a retired head kept its record"
    assert in_sync(owner_conn, ns)


# ============================================================================ (c) cost

#: Nine seats; seat 0 is "kxxx", about 11% of the cards (production: 2,389 of 21,073). One card in four names the
#: card nine before it, of its own seat (production: 5,601 edges over 21,073 heads). Values about 1.4 KB.
_SEATS = "ARRAY['kxxx','pxx','uxxx','txxxx','dxxxxx','nxxxxx','exxxx','oxxxxxx','bxxxxx']"
_SEED = f"""
INSERT INTO shared_memory (namespace, key, version, is_head, value, tags, author_label, provenance_jsonb,
                           payload_jcs_sha256, created_by_peer_id, created_by_owner_id)
SELECT %(ns)s, c.key, 1, TRUE,
       jsonb_build_object('schema', 'card.v1', 'id', md5(%(ns)s || c.i), 'type',
                          (ARRAY['Ruling','Candidate','Commitment','OpenItem','Constraint'])[1 + c.i %% 5],
                          'owner', 'operator', 'seat', c.seat,
                          'time', to_char(timestamptz '2026-06-01 00:00:00+00' + make_interval(secs => c.i),
                                          'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'),
                          'text', (SELECT string_agg(md5(c.i || '-' || j), ' ') FROM generate_series(1, 30) j),
                          'text_sha256', encode(sha256(convert_to(%(ns)s || c.i, 'UTF8')), 'hex'),
                          'source', jsonb_build_object('record_uuid', md5('r' || c.i), 'block_index', 0,
                                                       'kind', 'answered_question', 'first_hand', true))
       || CASE WHEN c.i %% 4 = 0 AND c.i >= 9
               THEN jsonb_build_object('supersedes', jsonb_build_array(
                        'card/' || c.seat || '/' || md5(%(ns)s || (c.i - 9))))
               ELSE '{{}}'::jsonb END,
       '{{}}', c.seat,
       jsonb_build_object('payload', jsonb_build_object('call_id', 'cost-' || %(ns)s || '-' || c.i)),
       encode(sha256(convert_to('cost-' || %(ns)s || '-' || c.i, 'UTF8')), 'hex'),
       %(peer)s, %(owner)s
  FROM (SELECT i, ({_SEATS})[1 + i %% 9] AS seat,
               'card/' || ({_SEATS})[1 + i %% 9] || '/' || md5(%(ns)s || i) AS key
          FROM generate_series(0, %(n)s - 1) AS i) AS c
"""
_SEED_OTHER = """
INSERT INTO shared_memory (namespace, key, version, is_head, value, tags, author_label, provenance_jsonb,
                           payload_jcs_sha256, created_by_peer_id, created_by_owner_id)
SELECT %(ns)s, 'note/' || i, 1, TRUE,
       jsonb_build_object('text', (SELECT string_agg(md5(i || '+' || j), ' ') FROM generate_series(1, 40) j)),
       '{}', 'other', jsonb_build_object('payload', jsonb_build_object('call_id', 'cost-' || %(ns)s || '-' || i)),
       encode(sha256(convert_to('cost-' || %(ns)s || '-' || i, 'UTF8')), 'hex'), %(peer)s, %(owner)s
  FROM generate_series(0, %(n)s - 1) AS i
"""

SMALL, LARGE, OTHER = 1_000, 21_000, 40_000
#: A page still grows with the namespace: the edge pass reads every namer record (DIVERGENCES 110), so a bound on
#: new-at-21k against new-at-1k measures the box's floor, not the code (it fired on a 2 ms floor at x14.9). The bound
#: is on GROWTH (21k minus 1k), against the growth of the oracle (the code before 110) in the same run on the same box:
#: new growth <= GROWTH_SHARE x oracle growth + SLACK_MS. Measured on Sun 8 Nov 2026: the HQB gate's worst row is
#: seat-start types limit 1, 35.2 ms against 128.9 ms (0.27, bound 79.5 ms); three scratch Postgres 16 runs, worst
#: 28.8 ms against 80.2 ms (0.36, bound 55.1 ms). The oracle's own growth fails it by 47 to 101 ms on every gate row.
GROWTH_SHARE = 0.5
SLACK_MS = 15.0


def _ms(conn, fn, runs):
    times = []
    for _ in range(runs):
        t0 = clock.perf_counter()
        with rls.server_internal(conn):
            fn()
        times.append((clock.perf_counter() - t0) * 1000)
    return statistics.median(times)


class _Spy:
    """A connection that EXPLAINs list_cards' page statement where list_cards runs it: in the same transaction, after
    the same settings (the sort pin), then runs it."""

    def __init__(self, inner, seen):
        self.inner, self.seen = inner, seen

    @property
    def info(self):  # list_cards reads the transaction status in its finally (8c9c8678)
        return self.inner.info

    def cursor(self, **kw):
        return _SpyCursor(self.inner, self.inner.cursor(**kw), self.seen)


class _SpyCursor:
    def __init__(self, conn, cur, seen):
        self.conn, self.cur, self.seen = conn, cur, seen

    def __enter__(self):
        self.cur.__enter__()
        return self

    def __exit__(self, *a):
        return self.cur.__exit__(*a)

    def execute(self, sql, params=None):
        if sql.startswith("WITH"):
            text = self.conn.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) " + sql, params).fetchall()
            js = self.conn.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, params).fetchone()[0]
            self.seen["text"], self.seen["plan"] = "\n".join(r[0] for r in text), js[0]["Plan"]
        return self.cur.execute(sql, params)

    def fetchone(self):
        return self.cur.fetchone()

    def fetchall(self):
        return self.cur.fetchall()


def _plan(conn, ns, **flt):
    """EXPLAIN (ANALYZE, BUFFERS) of the new list's own statement, as text and as JSON."""
    seen = {}
    with rls.server_internal(conn):
        store.list_cards(_Spy(conn, seen), ns, **flt)
    return seen["text"], seen["plan"]


def _nodes(plan):
    yield plan
    for child in plan.get("Plans", []):
        yield from _nodes(child)


@pytest.fixture()
def big(app_conn, owner_conn):
    peer = _peer(owner_conn, "cost")
    small, large, other = (f"shared/cost{n}-{uuid.uuid4().hex[:8]}" for n in ("s", "l", "o"))
    for ns in (small, large, other):
        _claim(app_conn, peer, ns)
    with history_rows.seeding_history(owner_conn):
        for ns, n, sql in ((small, SMALL, _SEED), (large, LARGE, _SEED), (other, OTHER, _SEED_OTHER)):
            owner_conn.execute(sql, {"ns": ns, "n": n, "peer": peer["peer_id"], "owner": peer["owner_id"]})
    owner_conn.execute("ANALYZE shared_memory")
    owner_conn.execute("ANALYZE card_heads")
    owner_conn.commit()
    try:
        yield small, large
    finally:
        owner_conn.rollback()
        owner_conn.execute("DELETE FROM shared_memory WHERE namespace = ANY(%s)", ([small, large, other],))
        owner_conn.commit()


#: (name, filter). Every case reads the records of every card that names something (the edge pass) and the page's
#: own rows, so every case grows with the namespace; a type filter adds the walk over every live edge (`_REACH`, by
#: design: a card that replaces one of the requested type is listed however deep the corrections go).
CASES = [("default page", {}),
         ("seat kxxx, status any", {"seat": "kxxx", "status": "any"}),
         ("seat-start types", {"types": ("Ruling", "Constraint", "Commitment", "OpenItem")})]


def test_a_page_grows_with_the_namespace_at_a_fraction_of_the_old_rate(app_conn, owner_conn, big):
    """(c) The new list at 21,000 production-like heads (beside 40,000 other rows) against 1,000, at limit 1 and 100,
    warm medians, with the oracle (the code before 110) measured the same way. A page still grows with the namespace
    (the edge pass), so the bound is on growth against the oracle's growth, never on a ratio to the 1,000-head floor.
    Then the plan at 21,000: every node on shared_memory is an index scan that reads at most the page's rows."""
    small, large = big
    meta = owner_conn.execute(
        "SELECT count(*), avg(pg_column_size(value))::int, max(pg_column_size(value)) FROM shared_memory "
        "WHERE namespace = %s AND is_head", (large,)).fetchone()
    namers = owner_conn.execute("SELECT count(*) FROM card_heads WHERE namespace = %s AND supersedes <> '{}'",
                                (large,)).fetchone()[0]
    owner_conn.commit()
    print(f"\n[cost] large: {meta[0]} heads, value bytes avg {meta[1]} max {meta[2]}, {namers} namers; "
          f"small: {SMALL} heads; other rows: {OTHER}")
    assert meta[0] == LARGE and 1300 <= meta[1] <= 1550, meta
    rows = []
    for name, flt in CASES:
        for limit in (1, 100):
            for ns in (small, large):       # warm both lists on both sizes
                _ms(app_conn, lambda: store.list_cards(app_conn, ns, limit=limit, **flt), 2)
                _ms(app_conn, lambda: old_list_cards(app_conn, ns, limit=limit, **flt), 1)
            new_s = _ms(app_conn, lambda: store.list_cards(app_conn, small, limit=limit, **flt), 9)
            new_l = _ms(app_conn, lambda: store.list_cards(app_conn, large, limit=limit, **flt), 9)
            old_s = _ms(app_conn, lambda: old_list_cards(app_conn, small, limit=limit, **flt), 3)
            old_l = _ms(app_conn, lambda: old_list_cards(app_conn, large, limit=limit, **flt), 3)
            rows.append((name, limit, new_s, new_l, old_s, old_l))
            print(f"[cost] {name:22s} limit {limit:3d}: new {new_s:7.1f} ms at 1k, {new_l:7.1f} ms at 21k "
                  f"(x{new_l / new_s:4.1f}) | before 110 {old_s:7.1f} ms at 1k, {old_l:7.1f} ms at 21k "
                  f"(x{old_l / old_s:4.1f})")
    plans = {}
    for name, flt in CASES:                  # the plans first, so a failed bound below has them in the output
        for limit in (1, 100):
            plans[name, limit] = _plan(app_conn, large, limit=limit, **flt)
            if limit == 100:
                print(f"[cost] EXPLAIN (ANALYZE, BUFFERS), {name}, limit {limit}, 21k heads:\n{plans[name, limit][0]}")

    for name, limit, new_s, new_l, old_s, old_l in rows:
        assert new_l - new_s <= GROWTH_SHARE * (old_l - old_s) + SLACK_MS, (name, limit, new_s, new_l, old_s, old_l)
        assert new_l < old_l, (name, limit, new_l, old_l)

    for (name, limit), (text, plan) in plans.items():
        for node in _nodes(plan):
            if node.get("Relation Name") == "shared_memory":
                read = node["Actual Rows"] * node["Actual Loops"]
                assert read <= limit + 1, (name, limit, node["Node Type"], read, text)
                assert node["Node Type"] in ("Index Scan", "Index Only Scan"), (name, limit, text)


def _settings(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT current_setting('enable_sort'), current_setting('jit')")
        return cur.fetchone()


def _list_with_a_failing_row_reader(app_conn, ns, monkeypatch):
    """list_cards whose rows fail in Python, not in SQL: the transaction stays usable, so a caller can catch the
    error and carry on in the same transaction."""
    def broken(cursor):
        raise RuntimeError("row reader failed")
    monkeypatch.setattr(store, "dict_row", broken)
    with pytest.raises(RuntimeError):
        store.list_cards(app_conn, ns)
    monkeypatch.setattr(store, "dict_row", dict_row)


def test_sorts_and_jit_come_back_when_the_page_read_fails_and_the_caller_carries_on(app_conn, cards, monkeypatch):
    """kxxx's P3 on 2e18c7: list_cards turns enable_sort and jit off for its one statement. A caller that catches a
    failure inside the same transaction must get them back, or the rest of its transaction plans without sorts.
    An SQL error aborts the transaction (and a savepoint rollback drops the local settings), so the path that
    matters is a Python error after the statement ran: the transaction is still fine and nothing undid the change."""
    ns, seat, label = cards
    one = _value(label, "one card", T(9)); _write(app_conn, seat, ns, f"card/{label}/{one['id']}", one)
    before = _settings(app_conn)
    with app_conn.transaction():
        _list_with_a_failing_row_reader(app_conn, ns, monkeypatch)
        assert _settings(app_conn) == before
        store.list_cards(app_conn, ns)
        assert _settings(app_conn) == before
    assert _settings(app_conn) == before


def test_RED_without_the_finally_a_caught_failure_leaves_sorts_off(app_conn, cards, monkeypatch):
    """RED for the test above: the list as it was before the try/finally (restore only after a clean read)."""
    ns, seat, label = cards
    one = _value(label, "one card", T(9)); _write(app_conn, seat, ns, f"card/{label}/{one['id']}", one)
    src = inspect.getsource(store.list_cards)
    assert "finally:" in src, "control: the code under test has the finally"
    before = _settings(app_conn)

    def old_style(conn, ns_):
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('enable_sort', 'off', true), set_config('jit', 'off', true)")
        raise RuntimeError("row reader failed")   # the read fails; the old code never reached its restore
    with app_conn.transaction():
        with pytest.raises(RuntimeError):
            old_style(app_conn, ns)
        assert _settings(app_conn) != before, "the old shape leaves the settings off for the rest of the transaction"


# --- pxx, round 1: C1 (the request role and the definer trigger) and C2 (a card/ key in another namespace) ---------

MIGRATION_057 = (Path(__file__).resolve().parent.parent / "sql_meshychat" / "057_card_heads.sql").read_text(encoding="utf-8")
FORGE_TABLE = ("CREATE TEMP TABLE forge (id uuid, namespace text, key text, version integer, created_at timestamptz, "
               "value jsonb, is_head boolean) ON COMMIT DROP")
FORGE_TRIGGER = ("CREATE TRIGGER forge_card_head AFTER INSERT ON forge FOR EACH ROW "
                 "EXECUTE FUNCTION public.meshy_card_head_track()")
FORGE_ROW = "INSERT INTO forge VALUES (%s, %s, %s, %s, now(), %s::jsonb, true)"


def _definition(name: str) -> str:
    """One CREATE OR REPLACE FUNCTION statement of 057, as the file has it."""
    start = MIGRATION_057.index(f"CREATE OR REPLACE FUNCTION public.{name}(")
    return MIGRATION_057[start:MIGRATION_057.index("$$;", start) + 3]


def _track_without_the_table_check() -> str:
    body = _definition("meshy_card_head_track")
    start = body.index("    -- The definer writes card_heads")
    end = body.index("END IF;", start) + len("END IF;\n")
    return body[:start] + body[end:]


def _put_before_the_bound() -> str:
    """meshy_card_head_put as round 1 had it: card_time and time_is_string taken from any string time."""
    body = _definition("meshy_card_head_put")
    start = body.index("            CASE WHEN jsonb_typeof(p_value -> 'time')")
    end = body.index("p_value ->> 'type'", start)
    return body[:start] + ("            p_value ->> 'time',\n"
                           "            coalesce(jsonb_typeof(p_value -> 'time') = 'string', false),\n"
                           "            ") + body[end:]


def _incompressible(n: int) -> str:
    """n characters that pglz cannot shrink: a btree entry is compressed before its size is checked, so a repeated
    character would fit the index and prove nothing."""
    return "".join(uuid.uuid4().hex for _ in range(n // 32 + 1))[:n]


def _real_card(app_conn, owner_conn, cards):
    ns, seat, label = cards
    value = _value(label, "Ruling | the real one", T(7))
    key = f"card/{label}/{value['id']}"
    _write(app_conn, seat, ns, key, value)
    row = owner_conn.execute("SELECT memory_id, version, seat FROM card_heads WHERE namespace = %s AND key = %s",
                             (ns, key)).fetchone()
    owner_conn.commit()
    assert row is not None and row[2] == label, "control: the real card has its record"
    return ns, key, row[0], row[1]


def _record_seat(owner_conn, ns, key):
    got = owner_conn.execute("SELECT seat FROM card_heads WHERE namespace = %s AND key = %s", (ns, key)).fetchone()
    owner_conn.commit()
    return got[0] if got else None


def test_the_request_role_holds_no_execute_on_the_record_functions(owner_conn):
    """C1: 010's default privileges grant the app role EXECUTE on every new function directly, so 057 revokes it from
    meshychat_app by name, not only from PUBLIC."""
    got = owner_conn.execute(
        "SELECT has_function_privilege('meshychat_app', 'public.meshy_card_head_track()', 'EXECUTE'), "
        "has_function_privilege('meshychat_app', 'public.meshy_card_head_sync(text, text)', 'EXECUTE'), "
        "has_function_privilege('meshychat_app', "
        "'public.meshy_card_head_put(uuid, text, text, integer, timestamptz, jsonb)', 'EXECUTE')").fetchone()
    owner_conn.commit()
    assert got == (False, False, False), got


def test_the_request_role_cannot_attach_the_definer_trigger_to_its_own_table(app_conn, owner_conn, cards):
    """C1: meshychat_app may create temporary tables, so with EXECUTE it could attach the SECURITY DEFINER trigger to
    one and write any record as the definer. CREATE TRIGGER needs EXECUTE on the function: refused."""
    ns, key, mid, version = _real_card(app_conn, owner_conn, cards)
    with pytest.raises(Exception) as exc:
        with app_conn.transaction():
            app_conn.execute(FORGE_TABLE)
            app_conn.execute(FORGE_TRIGGER)
    assert "permission denied" in str(exc.value), exc.value
    assert _record_seat(owner_conn, ns, key) == cards[2]


def test_the_trigger_function_refuses_any_table_but_shared_memory(app_conn, owner_conn, cards):
    """C1, the second layer: even a role that may execute it gets nothing from the function on another table."""
    ns, key, mid, version = _real_card(app_conn, owner_conn, cards)
    with pytest.raises(Exception) as exc:
        with owner_conn.transaction():
            owner_conn.execute(FORGE_TABLE)
            owner_conn.execute(FORGE_TRIGGER)
            owner_conn.execute(FORGE_ROW, (mid, ns, key, version, json.dumps({"text": "t", "seat": "forged"})))
    assert "runs on public.shared_memory only" in str(exc.value), exc.value
    assert _record_seat(owner_conn, ns, key) == cards[2]


def test_RED_without_both_layers_the_request_role_rewrites_a_record(app_conn, owner_conn, cards):
    """RED for C1: with EXECUTE granted to meshychat_app (what 010's default privileges leave) and the table check
    gone, the request role rewrites a real card's record through its own temporary table."""
    ns, key, mid, version = _real_card(app_conn, owner_conn, cards)
    owner_conn.execute(_track_without_the_table_check())
    owner_conn.execute("GRANT EXECUTE ON FUNCTION public.meshy_card_head_track() TO meshychat_app")
    owner_conn.commit()
    try:
        with app_conn.transaction():
            app_conn.execute(FORGE_TABLE)
            app_conn.execute(FORGE_TRIGGER)
            app_conn.execute(FORGE_ROW, (mid, ns, key, version, json.dumps({"text": "t", "seat": "forged"})))
        forged = _record_seat(owner_conn, ns, key)
    finally:
        owner_conn.rollback()
        owner_conn.execute(_definition("meshy_card_head_track"))
        owner_conn.execute("REVOKE EXECUTE ON FUNCTION public.meshy_card_head_track() FROM meshychat_app")
        owner_conn.execute("SELECT set_config('app.server_internal', '1', true)")
        owner_conn.execute("SELECT public.meshy_card_head_sync(%s, %s)", (ns, key))
        owner_conn.commit()
    assert forged == "forged", forged
    assert _record_seat(owner_conn, ns, key) == cards[2]


def test_a_card_key_with_a_long_time_in_another_namespace_is_written(app_conn, owner_conn, cards):
    """C2: card validation checks only the card namespace, so another namespace may hold card/x with any value. A
    16 KB "time" string is written, and its record holds no card time (the page index caps an entry near 2.7 KB)."""
    ns, seat, label = cards
    other = f"shared/notes-{uuid.uuid4().hex[:12]}"
    _claim(app_conn, seat, other)
    _write(app_conn, seat, other, "card/x", {"text": "not a card", "time": _incompressible(16384)})
    got = owner_conn.execute("SELECT card_time, time_is_string FROM card_heads WHERE namespace = %s AND key = 'card/x'",
                             (other,)).fetchone()
    owner_conn.commit()
    assert got == (None, False), got


def test_a_card_key_over_1024_bytes_gets_no_record(app_conn, owner_conn, cards):
    """C2: a key long enough to overflow the page index with the time beside it gets no record at all."""
    ns, seat, label = cards
    other = f"shared/notes-{uuid.uuid4().hex[:12]}"
    _claim(app_conn, seat, other)
    _raw(owner_conn, seat, other, "card/" + _incompressible(1100), {"text": "long key", "time": T(7)})
    got = owner_conn.execute("SELECT count(*) FROM card_heads WHERE namespace = %s", (other,)).fetchone()[0]
    owner_conn.commit()
    assert got == 0, got


def test_RED_the_unbounded_time_aborts_the_write(app_conn, owner_conn, cards):
    """RED for C2: with round 1's derivation (any string time kept as card_time) the same row is refused."""
    ns, seat, label = cards
    other = f"shared/notes-{uuid.uuid4().hex[:12]}"
    _claim(app_conn, seat, other)
    owner_conn.execute(_put_before_the_bound())
    try:
        with pytest.raises(Exception) as exc:
            sr.insert(owner_conn, sr.row(seat["peer_id"], seat["owner_id"], other, "card/x",
                                         {"text": "not a card", "time": _incompressible(16384)}))
    finally:
        owner_conn.rollback()
    assert "index row" in str(exc.value), exc.value
