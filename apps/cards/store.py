"""Reading cards: the filtered list and the derived status (divergence 091; docs/design/decision-cards.md 5).

A CARD here is a head row of the card namespace under `card/` whose value still has its `text`: a
redacted card is a tombstone with no `text`, and is neither listed nor able to supersede anything.

SUPERSEDED IS DERIVED. A card is superseded when at least one live head card OF ITS OWN SEAT names
its key in `supersedes`. A later card of ANOTHER seat that names it does not hide it: the card stays
in the active read and carries `overruled_by` (see `_AGG`). Nothing is stored on the older card, so:

  * a superseding card that is redacted, or replaced by a version that no longer names the target,
    stops superseding it, and the target reads as active again;
  * there is no cascade: A named by B named by C leaves A and B both superseded and C active;
  * a write is refused unless every target is a head card strictly earlier than the new one
    (validation.enforce_card), so two cards cannot name each other.

ORDER is `time` descending, then key descending. `time` has one spelling (validation.TIME_RE), so
the string order is the time order, and the cursor is that pair.
"""
from __future__ import annotations

import base64
import json

from psycopg import pq
from psycopg.rows import dict_row

from . import validation

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
STATUSES = ("active", "superseded", "any")

#: The live edges of a namespace: (namer, target, the target's type, are the two of ONE seat, is the namer
#: redacted). An edge is live when the namer is a head row under card/ that names the target, the target
#: is a head card with its text, and the namer's `time` is STRICTLY LATER than the target's.
#:
#: THE NAMER MAY BE A TOMBSTONE (divergence 092). A redacted card keeps `supersedes`, `time` and `seat`
#: (memoryskills/redact.CARD_FIELDS), so what it replaced STAYS replaced: redacting a ruling for a
#: credential must not bring the ruling it overruled back as current.
#:
#: THE TARGET MAY BE A TOMBSTONE TOO, on this read side only. A redacted card keeps `type` and `time`,
#: so a card that replaced it still replaces a card of that type: redacting a Ruling, or a correction in
#: the middle of a chain, must not drop the readable card that stands on it from the seat-start read
#: (memory-bxxxxx on #170). A tombstone is a key here and is never listed. The WRITE rule is unchanged:
#: a new card may not name a tombstone (validation.enforce_card).
#: The order is in the join, so whatever was written, no loop of live edges can exist (a card
#: cannot be both later and earlier than another); `time` has one fixed-width UTC spelling, so the text
#: order is the instant order.
#:
#: READ FROM THE HEAD RECORD, NEVER FROM A VALUE (divergence 110, sql_meshychat/057). `card_heads` holds, for each
#: head row under card/, the fields this pass reads, as typed columns the database derives from the value in the
#: writer's own statement: `has_text` is `jsonb_typeof(value->'text') = 'string'`, `redacted` is
#: `value->'redacted' = 'true'::jsonb`, `card_time` is `value->>'time'`, `card_type` and `seat` are `->>`, and
#: `supersedes` is the array's elements as text. So the pass reads the narrow records of the cards that name
#: something (an index holds just those), unnests each `supersedes` (at most validation.MAX_SUPERSEDES keys) and
#: finds each target by its record's key, and decompresses no value at all. Before it read every head's value:
#: 847 ms of a 1,257 ms page on production, 21,073 heads (divergence 110).
#: The record holds card/ keys only, and so does the card namespace: memory.write refuses any other key there
#: (E_CARD_KEY, divergence 091), so a target outside card/ cannot be a row of it.
_EDGES = (
    "SELECT n.key AS namer, t.key AS target, t.card_type AS target_type, "
    "       (n.seat IS NOT DISTINCT FROM t.seat) AS same_seat, "
    "       (NOT n.has_text) AS namer_redacted "
    "FROM card_heads n "
    "CROSS JOIN LATERAL unnest(n.supersedes) AS u(target) "
    "JOIN card_heads t ON t.namespace = n.namespace AND t.key = u.target "
    "  AND (t.has_text OR t.redacted) AND t.card_time < n.card_time "
    "WHERE n.namespace = %(ns)s AND n.supersedes <> '{}'::text[] AND (n.has_text OR n.redacted)")
#: Per target: the cards of ITS OWN seat naming it (`by_keys`: it is superseded), and the cards of OTHER
#: seats naming it (`overruled_by`: it is NOT hidden).
#:
#: WHY THE SPLIT. Whether a card a recorded decision is the writing seat's own word; the server cannot check
#: it. If another seat's card could remove a Ruling from every seat's default read, any seat could hide
#: any Ruling by signing one line. So only a seat's own later card takes its card out of the active
#: read. Another seat's later Ruling is SHOWN beside it, and the older card carries `overruled_by`: the
#: reader sees both, with their seats and times, and the owning seat supersedes its own card when it
#: agrees. (dxxxxx on #169, msg/<key>.)
#: ONE literal, read from a CTE named `named` (rows of `_EDGES`): the module's one aggregate statement, and written
#: whole so tests/test_query_cost_bounds.py counts it (its scan needs the aggregate and the SELECT in one
#: literal; split across f-string parts it would not be on the ledger at all). `superseders` gives it every live
#: edge; `list_cards` gives it the edges naming the page's own cards only (divergence 110), so the aggregate runs
#: over at most limit+1 targets whatever the namespace holds.
#:
#: EACH LIST IS CUT HERE, IN SQL, at NAMED_CAP keys (kxxx, msg/<key>). A key is named by
#: as many later cards as name it, so without the cut one card named by thousands made a page larger than any
#: client cap (`card recent` reads at most 8 MiB and then fails closed). The slice is on the aggregate itself,
#: so `s`, the row this module fetches and the JSON are all bounded; a cut in Python after the fetch would
#: leave the read as large as before. Each `*_count` is the cardinality of the SAME uncut aggregate the list
#: is the prefix of (one aggregate, computed once: Postgres shares identical aggregate calls of one level),
#: so a count cannot disagree with its list. `DISTINCT` makes it a count of cards, not of edges: a write
#: refuses a repeated key in `supersedes` (validation.supersedes_of), so it changes nothing that was written.
#: The ORDER is the key, as before the cut: an uncut list is byte for byte what it was, and the kept keys of
#: a cut one are the first NAMED_CAP by key, the same on every read.
#:
#: STATUS IS NOT READ FROM A LIST. `by_count` (all of a card's own-seat namers) decides it, in the status
#: filter below and in `list_cards` and `superseders`, so a card named by 10,000 is superseded whatever is
#: kept. The split by seat is in the FILTER, before any cut: a cut of all namers first could drop the one
#: own-seat card that decides the status.
_AGG = ("SELECT e.target, "
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
        "FROM named e GROUP BY e.target")
_BY_TARGET = f"WITH named AS ({_EDGES}) {_AGG}"
#: What the page reads of `s` for each returned card.
_NAMED = "s.by_keys, s.by_count, s.overruled_by, s.overruled_count, s.by_redacted, s.redacted_count"
#: The most keys any one of a card's three lists (`superseded_by`, `overruled_by`, `named_by_redacted`)
#: carries; each `*_count` beside it says how many there are. Bound into `_AGG` as `named_cap`.
NAMED_CAP = 32


def superseders(conn, ns: str) -> dict[str, list[str]]:
    """{superseded card key: [the keys of the live head cards OF ITS OWN SEAT naming it, at most NAMED_CAP,
    by key]}. A card only other seats name is not here: it is overruled, not superseded, and stays in the
    active read. Whether a card is here is decided by the count of ALL its own-seat namers, never by the cut
    list: the card search filters on this key set (apps/meshsem/views._card_filter, `matches`)."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_BY_TARGET, {"ns": ns, "named_cap": NAMED_CAP})
        return {row["target"]: list(row["by_keys"] or []) for row in cur.fetchall() if row["by_count"]}


def encode_cursor(time: str, key: str) -> str:
    return base64.urlsafe_b64encode(json.dumps([time, key]).encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(raw: str) -> tuple[str, str]:
    """(time, key) of a cursor this module issued. ValueError for anything else."""
    try:
        data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except Exception as exc:
        raise ValueError("not a cursor") from exc
    if (not isinstance(data, list) or len(data) != 2 or not all(isinstance(x, str) for x in data)
            or not validation.TIME_RE.fullmatch(data[0]) or validation.key_parts(data[1]) is None):
        raise ValueError("not a cursor")
    return data[0], data[1]


#: The cards that REPLACE a card of a requested type, however many corrections deep: the cards naming
#: one, the cards naming those, and so on. Edges only go back in time (`_EDGES`), so the walk ends, and
#: UNION drops a key reached twice.
#: It reads `er`, the edges once each (`_REACH_EDGES`): a DISTINCT over a CTE is estimated at 200 rows, so the walk
#: hashes its worktable against it. Read from `e` directly, whose estimate counts ten elements for every unnested
#: array, it sorted both sides on every step (divergence 110).
_REACH = ("SELECT e.namer FROM er e WHERE e.target_type = ANY(%(types)s) "
          "UNION SELECT e.namer FROM er e JOIN reach r ON e.target = r.key")
_REACH_EDGES = "er AS MATERIALIZED (SELECT DISTINCT e.namer, e.target, e.target_type FROM e)"


def replacers(conn, ns: str, types) -> set[str]:
    """The keys of the cards that replace a card of one of `types`, through any number of live edges."""
    if not types:
        return set()
    with conn.cursor() as cur:
        cur.execute(f"WITH RECURSIVE e AS MATERIALIZED ({_EDGES}), {_REACH_EDGES}, reach(key) AS ({_REACH}) "
                    "SELECT key FROM reach",
                    {"ns": ns, "types": list(types)})
        return {row[0] for row in cur.fetchall()}


#: The `source.kind` of a card a hook wrote: its `thread_id`, when it has one, is the hook's INFERENCE from the
#: session, not a seat's declaration (pxx, node-awareness item 8). A thread filter accepts both alike, so every
#: read that returns the card says which it is.
INFERRED_THREAD_KINDS = frozenset({"question_hook", "answered_question_hook"})


def thread_tier(value) -> str | None:
    """'inferred' for a card with a `thread_id` whose source kind is a hook's, 'declared' for any other card
    with one, None for a card without a `thread_id` (the field is then left out)."""
    if not isinstance(value, dict) or not isinstance(value.get("thread_id"), str):
        return None
    source = value.get("source")
    kind = source.get("kind") if isinstance(source, dict) else None
    # A string only: a list or an object is unhashable, and `in` a frozenset would raise inside a search (Codex on #221).
    return "inferred" if isinstance(kind, str) and kind in INFERRED_THREAD_KINDS else "declared"


def matches(value: dict, key: str, *, types=(), seat=None, owner=None, thread=None, status="active",
            superseded: dict[str, list[str]] | None = None, replacing: set[str] | None = None,
            since=None, until=None) -> bool:
    """The list's filter as a predicate on one row, for the search pool (graphsearch._lookup): the
    same rule decides both, so a card the list hides is never a search hit under the same filter, and
    a card the list returns in place of a requested type (`replacing`, from `replacers`) is one.
    `since` is inclusive and `until` exclusive on the card's own `time`, compared as the list's SQL compares
    it: as text, which orders instants because every `time` has the one fixed shape (validation.TIME_RE).
    A Context card (#297) is excluded from the default search pool unless `Context` is explicitly named in `types`."""
    if not isinstance(value, dict) or not isinstance(value.get("text"), str):
        return False
    if not types and value.get("type") == "Context":
        return False
    if types and value.get("type") not in types and key not in (replacing or ()):
        return False
    if seat is not None and value.get("seat") != seat:
        return False
    if owner is not None and value.get("owner") != owner:
        return False
    if thread is not None and value.get("thread_id") != thread:
        return False
    if since is not None or until is not None:
        at = value.get("time")
        # A time of any other spelling ("...:17Z", "+00:00", no time at all) has no place in a text order, so
        # a window never keeps it: fail closed. The writer refuses such a time (validation.TIME_RE), and the
        # production cards all have the one spelling (14 Oct 2026: 8,906 of 8,906).
        if not isinstance(at, str) or not validation.TIME_RE.fullmatch(at):
            return False
        if since is not None and at < since:
            return False
        if until is not None and at >= until:
            return False
    if status != "any":
        is_superseded = key in (superseded or {})
        if is_superseded != (status == "superseded"):
            return False
    return True


def _named_fields(r) -> dict:
    """A card's derived fields from its row of `s` (`_NAMED`; every column None when nothing names it).

    `status`, `superseded_by` and `overruled_by` are always there, as before, and so are their counts.
    `named_by_redacted` and its count are there only when a redacted card names this one, and
    `truncated_lists: true` only when a list was cut: the route's optional fields are left out when there is
    nothing to say (`named_by_redacted`, `replaces_requested`), never sent as false. A list is cut when its
    count is larger than its length, which the SQL makes NAMED_CAP. The status is read from the count."""
    by_count = r["by_count"] or 0
    by = list(r["by_keys"] or [])
    overruled = list(r["overruled_by"] or [])
    overruled_count = r["overruled_count"] or 0
    out = {"status": "superseded" if by_count > 0 else "active",
           "superseded_by": by, "superseded_by_count": by_count,
           "overruled_by": overruled, "overruled_by_count": overruled_count}
    cut = by_count > len(by) or overruled_count > len(overruled)
    if r["redacted_count"]:
        # Named so a reader does not go looking for a card it cannot read: a card that names this
        # one was redacted, and still counts.
        redacted = list(r["by_redacted"] or [])
        out["named_by_redacted"] = redacted
        out["named_by_redacted_count"] = r["redacted_count"]
        cut = cut or r["redacted_count"] > len(redacted)
    if cut:
        out["truncated_lists"] = True
    return out


def list_cards(conn, ns: str, *, types=(), seat=None, owner=None, thread=None, status="active", since=None,
               until=None, recorded_since=None, limit=DEFAULT_LIMIT, after: tuple[str, str] | None = None) -> dict:
    """One page, newest first by the card's own `time`.

    `since` is inclusive and `until` exclusive, both on `time`. `recorded_since` is on the SERVER's
    write time of the head version: a card loaded today from a June transcript has a June `time`, so a
    seat that asks "what is new since my last read" asks by this one, never by `since`.

    `types`, when given, also returns a card of any type that REPLACES a card of a requested type
    (`replaces_requested: true` on it), through any number of live edges. Without that, a Ruling the
    operator overruled by typing drops out as superseded while its replacement, a Candidate, is not
    of a requested type, and the read shows nothing for the topic; and a second typed correction
    would lose it again if the walk stopped at one step (memory-bxxxxx on #169).

    Each card's `superseded_by`, `overruled_by` and `named_by_redacted` hold at most NAMED_CAP keys, the
    first by key, beside `*_count`, the true number, and `truncated_lists: true` when one was cut (`_AGG`,
    `_named_fields`). Its `status` is read from the count of all its own-seat namers."""
    # Every filter, the order and the cursor read the head RECORD (`card_heads`, divergence 110), never the value:
    # each column is the `->>` or `jsonb_typeof` of the value the read before it filtered on (sql_meshychat/057).
    # The value is fetched for the page's own rows only, by the head row's id.
    where = ["h.namespace = %(ns)s", "h.has_text", "h.time_is_string"]
    params: dict = {"ns": ns, "limit": int(limit) + 1, "named_cap": NAMED_CAP}
    replaces = "FALSE"
    if types:
        params["types"] = list(types)
        replaces = "(h.key IN (SELECT rk.key FROM reached rk))"
        where.append(f"(h.card_type = ANY(%(types)s) OR {replaces})")
    if seat is not None:
        where.append("h.seat = %(seat)s")
        params["seat"] = seat
    if owner is not None:
        where.append("h.owner = %(owner)s")
        params["owner"] = owner
    if thread is not None:
        where.append("h.thread_id = %(thread)s")
        params["thread"] = thread
    if since is not None:
        where.append("h.card_time >= %(since)s")
        params["since"] = since
    if until is not None:
        where.append("h.card_time < %(until)s")
        params["until"] = until
    if recorded_since is not None:
        where.append("h.created_at >= %(recorded_since)s::timestamptz")
        params["recorded_since"] = recorded_since
    if after is not None:
        where.append("(h.card_time, h.key) < (%(a_time)s, %(a_key)s)")
        params["a_time"], params["a_key"] = after
    # THE STATUS FILTER IS A KEY SET, NOT A JOIN. A CTE has no index. Joined to every candidate card it was
    # scanned once per card: uxxx measured 18.7 million rows removed by the join filter for one page of 50 at
    # 10,000 heads (msg/<key>). As `key [NOT] IN (the superseded keys)` it is one hashed set,
    # probed once per card. The set is the targets of an own-seat edge, which is exactly the targets whose
    # `by_count` is above 0 (`_AGG`: the count of a card's own-seat namers, never its cut list).
    if status == "active":
        where.append("h.key NOT IN (SELECT sup.target FROM sup)")
    elif status == "superseded":
        where.append("h.key IN (SELECT sup.target FROM sup)")
    # MATERIALIZED: without it the planner inlines a CTE and runs it once per candidate card (uxxx,
    # msg/<key>: 4,591 ms for the default page as inlined). `page` picks the rows from the
    # records; `named` keeps the edges that name a page card, so `s` aggregates for those cards only; the value
    # is joined last, for the page's rows, by id.
    # `reached` is the walk's keys, once each. The page probes it as a HASHED set: the walk's row estimate grows
    # with every unnested array (the planner reads ten elements an array), and an `IN` over the walk itself was
    # planned unhashed once that estimate passed work_mem, a linear scan of the walk per candidate card (measured
    # on 21,000 heads: 560 ms, against 18 ms hashed). A DISTINCT over a CTE is estimated at 200 rows and hashed.
    reach = (f", {_REACH_EDGES}, reach(key) AS ({_REACH}), "
             "reached AS MATERIALIZED (SELECT DISTINCT r.key FROM reach r) ") if types else " "
    sql = (f"WITH RECURSIVE e AS MATERIALIZED ({_EDGES}), "
           "sup AS MATERIALIZED (SELECT DISTINCT e.target FROM e WHERE e.same_seat)"
           f"{reach}"
           f", page AS MATERIALIZED (SELECT h.key, h.memory_id, h.card_time, {replaces} AS replaces_requested "
           f"           FROM card_heads h WHERE {' AND '.join(where)} "
           "           ORDER BY h.card_time DESC, h.key DESC LIMIT %(limit)s), "
           "named AS MATERIALIZED (SELECT e.* FROM e WHERE e.target IN (SELECT page.key FROM page)), "
           f"s AS MATERIALIZED ({_AGG}) "
           f"SELECT page.key, sm.version, sm.value, sm.created_at, page.replaces_requested, {_NAMED} "
           "FROM page JOIN shared_memory sm ON sm.id = page.memory_id AND sm.is_head "
           "LEFT JOIN s ON s.target = page.key "
           "ORDER BY page.card_time DESC, page.key DESC")
    # SORTS OFF FOR THIS STATEMENT, as divergence 046 does for the same reason. Under the request role the read
    # policy's qual (`current_setting('app.server_internal', true) = '1'`, no column in it) is estimated at the
    # default selectivity, 0.005, so the planner expects about 50 of 21,000 cards to pass and reads them all and
    # sorts them, rather than walking card_heads_page_idx in the page's order and stopping at the limit: measured
    # as meshychat_app at 21,000 heads, 101 ms (a top-N sort of 15,753 rows) against 20 ms. With sorts off the
    # ordered index walk is the one plan left for the page. AND JIT OFF: the sorts no plan can avoid (the final
    # order of the page, the aggregate's input) still carry the disabled-node cost, so the plan's total crosses
    # jit_above_cost and the statement spent about 550 ms compiling (measured the same way); related_reads turns
    # JIT off for its own statement too. Both put back afterwards, and local to the transaction either way.
    with conn.cursor() as cur:
        cur.execute("SELECT current_setting('enable_sort'), current_setting('jit')")
        sort_was, jit_was = cur.fetchone()
        cur.execute("SELECT set_config('enable_sort', 'off', true), set_config('jit', 'off', true)")
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    finally:
        # Put them back on every path a caller can continue on. A failed statement leaves the transaction (or the
        # savepoint) aborted, and rolling that back drops the local settings with it; a restore there would raise
        # and hide the original error, so it is skipped.
        if conn.info.transaction_status != pq.TransactionStatus.INERROR:
            with conn.cursor() as cur:
                cur.execute("SELECT set_config('enable_sort', %s, true), set_config('jit', %s, true)",
                            (sort_was, jit_was))
    more = len(rows) > limit
    rows = rows[:limit]
    cards = []
    for r in rows:
        card = dict(r["value"])
        card.update({"key": r["key"], "version": r["version"], "written_at": r["created_at"].isoformat()})
        card.update(_named_fields(r))
        if r["replaces_requested"]:
            card["replaces_requested"] = True
        tier = thread_tier(r["value"])
        if tier is not None:
            card["thread_tier"] = tier
        cards.append(card)
    return {"cards": cards,
            "next": encode_cursor(rows[-1]["value"]["time"], rows[-1]["key"]) if more and rows else None}


__all__ = ["DEFAULT_LIMIT", "MAX_LIMIT", "NAMED_CAP", "STATUSES", "decode_cursor", "encode_cursor", "list_cards",
           "replacers", "matches", "superseders"]
