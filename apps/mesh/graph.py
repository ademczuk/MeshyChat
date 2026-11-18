"""The questions the projection exists to answer.

    what is addressed to me, newest first?                   inbox
    what is in this thread?                                  thread
    what answers this message?                               replies
    what acks do I owe, and have I said anything since?      owed_acks
    how much of the log is projected, and how drifted is it? coverage, drift

EVERY READ JOINS BACK TO shared_memory.is_head. The projection does not store head-ness
(apps/mesh/projection.py explains why: the flip is an UPDATE the catch-up cannot see), so
the join is what keeps a superseded version out of the answer. At 0.3 QPS that is the
cheapest correct option, and it means a version bump is visible the instant it commits.

TRUNCATION IS REPORTED, NEVER SILENT. Same rule as apps/board/graph.py: a `limit` is a
cap on output, the caller is told when it bit, and no query here is windowed in a way that
turns "older than N rows" into "does not exist".

COVERAGE TRAVELS WITH EVERY ANSWER. `coverage()` is the projected/total pair, and the
views attach it to every response for the reason apps/board attaches adoption: a reader
who sees an empty inbox must be able to tell "nothing addressed to you" apart from "the
projection is three days behind". The second is a fact about the projection, not about
the mail.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import logging
import re
import threading
import time
from typing import Iterable, Mapping, Sequence

import psycopg
from psycopg.rows import dict_row

from apps.memoryskills.validation import MESH_CHAT_NAMESPACE
from apps.wsgw.mesh_notify import BROADCAST_LABELS

from .normalize import EDGE_TYPES
from .normalize import (HASH_PREFIX_LEN, INDEX_ONLY_FIELDS, QUALIFIED_ONLY_FIELD,
                        canonical_mention_ref)

log = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class Hit:
    """One message reached through one edge."""

    key: str
    version: int
    edge_type: str
    dst: str
    src_field: str
    recorded_at: _dt.datetime
    #: The projected author label, from the authored_by edge, when one exists.
    author: str | None


@dataclasses.dataclass(frozen=True)
class Hits:
    hits: tuple[Hit, ...]
    #: True when `limit` cut the result. Ignoring this shows a prefix as the whole.
    truncated: bool


_HEAD_JOIN = (
    "FROM mesh_edges e "
    "JOIN shared_memory sm ON sm.id = e.src_id AND sm.is_head "
    "LEFT JOIN mesh_edges a ON a.src_id = e.src_id AND a.edge_type = 'authored_by' "
)
DEFAULT_OWED_STATES = ("open", "active", "answered")


def _query(conn, where: list[str], params: list, *, limit: int | None,
           order: str = "e.recorded_at DESC, e.src_id DESC") -> Hits:
    sql = ("SELECT e.src_key AS key, e.src_version AS version, e.edge_type, e.dst, "
           "e.src_field, e.recorded_at, a.dst AS author " + _HEAD_JOIN +
           "WHERE " + " AND ".join(where) + f" ORDER BY {order}")
    if limit is not None:
        sql += " LIMIT %s"
        params = params + [int(limit) + 1]     # one extra: detect truncation
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    truncated = limit is not None and len(rows) > limit
    if truncated:
        rows = rows[:limit]
    return Hits(hits=tuple(Hit(**r) for r in rows), truncated=truncated)


def _labels(label: str | Sequence[str]) -> list[str]:
    """One label or several, as a non-empty list. A peer can be addressed by more than one
    label (divergence 018: the roster label and peers.name disagree for real seats), and
    an EMPTY set would silently answer "nothing addressed to you" for every caller, so it
    is refused rather than queried."""
    labels = [label] if isinstance(label, str) else [str(x) for x in label]
    if not labels:
        raise ValueError("at least one label is required")
    return labels


def inbox(conn, label: str | Sequence[str], *, include_cc: bool = True,
          limit: int | None = 50) -> Hits:
    """Messages addressed to `label` (or to any of several), newest first. THE query this
    projection is for. Each hit carries the `dst` it was reached through.

    Before this existed the same question was answered by paging the whole namespace and
    parsing `to` and `cc` client-side, in between 10 and 68 separately written places.

    NOT A DELIVERY PATH (STATUS E13). It answers "newest first, at most `limit`"; until
    2026-09-26 it also took a `since` timestamp, which no caller passed and which had the
    defect a delivery walk must not (a row whose writer started first and committed last
    lands below a timestamp already passed). Delivery pages by the server's commit order,
    through `/v1/mesh/wait?addressed=1` (apps/mesh/wait.py).
    """
    types = ("addressed_to", "copied_to") if include_cc else ("addressed_to",)
    where = ["e.edge_type = ANY(%s)", "e.dst = ANY(%s)"]
    params: list = [list(types), _labels(label)]
    return _query(conn, where, params, limit=limit)


class UnknownAfter(LookupError):
    """`after` names no message of this thread, or is not a position this route issued."""


#: A position in a thread's order, as `thread_page` issues it in `next_after`: the row's recorded_at (microseconds
#: since the epoch, UTC) and src_id. An edit of a message adds a new version at a LATER position, so a cursor that
#: was a message key would move with the edit and skip the rows in between (pxx P2-2); this cannot move.
POSITION_RE = re.compile(r"^pos:(\d{1,16}):([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$")
_EPOCH = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)


def position_token(recorded_at: _dt.datetime, src_id) -> str:
    micros = (recorded_at - _EPOCH) // _dt.timedelta(microseconds=1)
    return "pos:%d:%s" % (micros, src_id)


def _after_point(conn, thread_id: str, after: str):
    """(recorded_at, src_id) to page after. A position token is taken as issued. A msg/ key is resolved to its
    EARLIEST edge in this thread, so an edit can only make a page repeat rows, never skip them."""
    m = POSITION_RE.match(after)
    if m:
        # 16 digits are past any real time, and a value datetime cannot hold is "not a position this route
        # issued", never a 500 (GitHub Codex at graph.py:145, pxx P2-1).
        try:
            return _EPOCH + _dt.timedelta(microseconds=int(m.group(1))), m.group(2)
        except (OverflowError, ValueError):
            raise UnknownAfter(after) from None
    with conn.cursor() as cur:
        cur.execute("SELECT e.recorded_at, e.src_id FROM mesh_edges e "
                    "WHERE e.edge_type = 'in_thread' AND e.dst = %s AND e.src_key = %s "
                    "ORDER BY e.recorded_at ASC, e.src_id ASC LIMIT 1", (thread_id, after))
        row = cur.fetchone()
    if row is None:
        raise UnknownAfter(after)
    return row[0], row[1]


def thread_page(conn, thread_id: str, *, limit: int | None = None,
                after: str | None = None) -> tuple[Hits, str | None]:
    """(the page, the position after its last row) for one thread, oldest first (audit finding 9a).

    `after`: a position token from an earlier page's `next_after`, or a msg/ key of this thread (its earliest
    place); only the rows strictly after it are returned. Anything else raises UnknownAfter, never an empty page
    that would read as "nothing new". The position is returned only when `limit` cut the page."""
    where = ["e.edge_type = 'in_thread'", "e.dst = %s"]
    params: list = [thread_id]
    if after is not None:
        at, sid = _after_point(conn, thread_id, after)
        where.append("(e.recorded_at, e.src_id) > (%s, %s)")
        params += [at, sid]
    sql = ("SELECT e.src_key AS key, e.src_version AS version, e.edge_type, e.dst, "
           "e.src_field, e.recorded_at, a.dst AS author, e.src_id " + _HEAD_JOIN +
           "WHERE " + " AND ".join(where) + " ORDER BY e.recorded_at ASC, e.src_id ASC")
    if limit is not None:
        sql += " LIMIT %s"
        params = params + [int(limit) + 1]
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    truncated = limit is not None and len(rows) > limit
    if truncated:
        rows = rows[:limit]
    nxt = position_token(rows[-1]["recorded_at"], rows[-1]["src_id"]) if truncated and rows else None
    hits = tuple(Hit(**{k: v for k, v in r.items() if k != "src_id"}) for r in rows)
    return Hits(hits=hits, truncated=truncated), nxt


def causal_order(conn, hits: list[dict]) -> tuple[list[dict], list[dict]]:
    """Order a bounded thread page (`hits`, already in chronological stream order) into a causal
    reply tree (`replies_to` edges on each hit's exact head `(key, version)`), returning
    `(ordered_hits, unresolved_refs)`.

    - Any `replies_to` target NOT present in `hits` is recorded in `unresolved_refs`
      (`{"child_key", "parent_key"}`) and treated as a local root (`causal_depth=0`,
      `parent_unresolved=True`) so paged or cross-thread replies are never dropped or hidden.
    - Cycles among messages on the page are broken deterministically at the earliest unvisited
      stream node (`causal_depth=0`, `cycle_broken=True`) so every hit on the page is emitted once.
    - Zero SQL aggregates are used (one indexed probe on `mesh_edges`).
    """
    if not hits:
        return [], []
    by_key: dict[str, dict] = {}
    head_versions: dict[str, int] = {}
    stream_keys: list[str] = []
    for h in hits:
        if isinstance(h, dict) and isinstance(h.get("key"), str):
            k = h["key"]
            if k not in by_key:
                by_key[k] = dict(h)
                head_versions[k] = int(h.get("version") or 1)
                stream_keys.append(k)
    if not stream_keys:
        return list(hits), []

    parent_of: dict[str, str] = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT e.src_key, e.src_version, e.dst FROM mesh_edges e "
            "WHERE e.edge_type = 'replies_to' AND e.src_key = ANY(%s) "
            "ORDER BY e.src_key ASC, e.dst ASC",
            (stream_keys,),
        )
        for src_key, src_ver, dst in cur.fetchall():
            if head_versions.get(src_key) == int(src_ver) and src_key not in parent_of and dst != src_key:
                parent_of[src_key] = dst

    children: dict[str, list[str]] = {k: [] for k in stream_keys}
    roots: list[str] = []
    unresolved_refs: list[dict] = []
    for k in stream_keys:
        p = parent_of.get(k)
        if p is None:
            roots.append(k)
        elif p not in by_key:
            unresolved_refs.append({"child_key": k, "parent_key": p})
            roots.append(k)
        else:
            children[p].append(k)

    visited: set[str] = set()
    ordered: list[dict] = []

    def _emit_subtree(root_key: str, *, cycle_broken: bool = False) -> None:
        stack: list[tuple[str, int, bool]] = [(root_key, 0, cycle_broken)]
        while stack:
            curr, depth, broke = stack.pop()
            if curr in visited:
                continue
            visited.add(curr)
            row = dict(by_key[curr])
            p = parent_of.get(curr)
            row["reply_to"] = p
            row["causal_depth"] = depth
            if p is not None and p not in by_key:
                row["parent_unresolved"] = True
            if broke:
                row["cycle_broken"] = True
            ordered.append(row)
            for child in reversed(children.get(curr, ())):
                if child not in visited:
                    stack.append((child, depth + 1, False))

    for r in roots:
        _emit_subtree(r)
    for k in stream_keys:
        if k not in visited:
            _emit_subtree(k, cycle_broken=True)
    return ordered, unresolved_refs


def thread(conn, thread_id: str, *, limit: int | None = None) -> Hits:
    """Everything in one thread, oldest first, regardless of which spelling filed it."""
    return _query(conn, ["e.edge_type = 'in_thread'", "e.dst = %s"], [thread_id],
                  limit=limit, order="e.recorded_at ASC, e.src_id ASC")


def replies(conn, key: str, *, limit: int | None = None) -> Hits:
    """Messages that answer `key`, one hop, oldest first."""
    return _query(conn, ["e.edge_type = 'replies_to'", "e.dst = %s"], [key],
                  limit=limit, order="e.recorded_at ASC, e.src_id ASC")


@dataclasses.dataclass(frozen=True)
class OwedAck:
    """A message that asked `label` for an ack, and how directly it was answered.

    `answered_in_thread` is a FACT ABOUT ACTIVITY, not about comprehension: it says the
    peer authored something in that thread after the ask. The fleet has measured that
    activity is not proof of reading (a reply unread for 3.5 hours on a live thread), so
    the direct-reply test is returned separately and `state` never calls either one an
    acknowledgement without showing the fact it was derived from.
    """

    hit: Hit
    thread_id: str | None
    answered_in_thread: bool
    answered_directly: bool
    state: str
    done_when: str | None
    edited: bool
    # The ask's routing type from its current head.  Older stored messages may not have one.
    msg_type: str | None = None
    # When the ask's authority ends (kxxx item 2): a directive's expires_at, or for relayed words the window
    # their voice named plus the margin (validation.WINDOW_MARGIN_S). None for every other ask, and for a relay with
    # no window, which never expires (threat_model.md, 28 Oct and 10 Nov 2026).
    expires_at: str | None = None


#: Seconds after a relay's `window_end` that its words stop counting. Keep equal to
#: apps/memoryskills/validation.WINDOW_MARGIN_S and tools/meshy_node/node.WINDOW_MARGIN_S.
OWED_WINDOW_MARGIN_S = 1800


OWED_EXPIRED_UNKNOWN = "0001-01-01T00:00:00Z"


def owed_expiry(directive_expires_at, window_end, relayed) -> str | None:
    """The instant an owed ask's authority ends, as an ISO Z string, or None. Read from the stored value only; the
    write path already validated both fields. An unreadable directive expiry is left out (None). Relayed words with a
    window_end that does not parse are EXPIRED (the earliest instant), as the reader treats them (node.relay_expiry,
    _EXPIRED_UNKNOWN): an unknowable end must never show as unlimited (pxx P3-2). Only an old or hand-made
    row can carry one; the server refuses it at write."""
    def parse(text):
        if not isinstance(text, str) or not text.endswith("Z"):
            return None
        try:
            return _dt.datetime.fromisoformat(text[:-1] + "+00:00")
        except ValueError:
            return None
    when = parse(directive_expires_at)
    if when is None and relayed:
        end = parse(window_end)
        if end is not None:
            when = end + _dt.timedelta(seconds=OWED_WINDOW_MARGIN_S)
        elif window_end is not None:
            return OWED_EXPIRED_UNKNOWN
    return when.strftime("%Y-%m-%dT%H:%M:%SZ") if when is not None else None


@dataclasses.dataclass(frozen=True)
class AwaitingAck:
    """One ask authored by the caller and the label still owing its reply."""
    hit: Hit
    owing_label: str
    state: str
    done_when: str | None


@dataclasses.dataclass(frozen=True)
class ToCloseItem:
    """One answered, unclosed ask authored by the caller whose direct reply has settled."""
    key: str
    version: int
    recorded_at: _dt.datetime
    owing_label: str
    thread_id: str | None
    done_when: str | None
    latest_reply_key: str
    latest_reply_version: int
    latest_reply_at: _dt.datetime
    latest_reply_author: str | None
    due: bool = False


@dataclasses.dataclass(frozen=True)
class MentionHit:
    hit: Hit
    ambiguous: bool


@dataclasses.dataclass(frozen=True)
class Mentions:
    ref: str
    hits: tuple[MentionHit, ...]
    truncated: bool
    also_matched: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class Holder:
    """One signing peer's citations of an artifact, inside the newest hits that `custody` read."""
    #: `shared_memory.created_by_peer_id`: the key that signed the rows, so two labels of one seat are one holder.
    #: None for a row with no recorded signer.
    peer_id: str | None
    #: The author labels those rows carry, sorted: more than one is a seat that writes under aliases.
    labels: tuple[str, ...]
    mentions: int
    #: How many of them named less than the query (graph.mentions' `ambiguous`): consistent, not proof.
    ambiguous: int
    first_at: _dt.datetime
    last_at: _dt.datetime
    #: The newest keys, at most HOLDER_KEYS, so a reader can open the signed source of each claim.
    keys: tuple[str, ...]
    #: Message type -> count (request, answer, status, ...); `untyped` for a row without one.
    types: dict


@dataclasses.dataclass(frozen=True)
class Custody:
    ref: str
    holders: tuple[Holder, ...]
    #: The hits grouped. When `truncated`, older citations exist beyond them.
    hits_read: int
    truncated: bool
    also_matched: tuple[str, ...]


HOLDER_KEYS = 5


@dataclasses.dataclass(frozen=True)
class ThreadBrief:
    thread_id: str
    new: int
    last_recorded_at: _dt.datetime
    last_author: str | None


def mentions(conn, ref: str, *, limit: int = 50, gate: bool = False) -> Mentions:
    """Head messages that name one canonical artifact, newest first and visibly bounded.

    A bare `#73` intentionally includes repo-qualified `gh:<repo>#73` references; the
    response lists those additional destinations. A qualified query includes a bare #73 the
    text really names as potentially ambiguous evidence, flagged per hit rather than silently
    asserting a repo, and never the bare key another repository's `#73` writes beside itself
    (normalize.QUALIFIED_ONLY_FIELD): that one is an index key, not a reference.

    A mention is body-derived, so it is served only while the embed_policy gate is open or
    its row is 'allowed', as every body derivative's read is gated (apps/meshsem). The
    projection applies the same gate when it writes the edge, but a row projected while the
    gate was open keeps its edges, and closing the gate must hide them at once (codex on #89).
    `gate` is store.forbidden_rows_open(); closed when not given.

    A `hash:` or `path:` ref (issue #159) is answered by _artifact_mentions, which reads a prefix or a file name the
    way this reads a bare #73.
    """
    canonical = canonical_mention_ref(ref)
    if canonical is None:
        raise ValueError("ref is not a canonicalizable artifact reference")
    if canonical.startswith(("hash:", "path:")):
        return _artifact_mentions(conn, canonical, limit=limit, gate=gate)
    where = ["e.edge_type = 'mentions'", "(%s OR sm.embed_policy = 'allowed')"]
    params: list = [bool(gate)]
    qualified = canonical.startswith("gh:") and canonical != "gh:#" and "/" in canonical
    if canonical.startswith("gh:#"):
        where.append("e.dst = %s")
        params.append(canonical)
    elif qualified:
        number = canonical.rsplit("#", 1)[1]
        where.append("(e.dst = %s OR (e.dst = %s AND e.src_field <> %s))")
        params.extend([canonical, f"gh:#{number}", QUALIFIED_ONLY_FIELD])
    elif canonical.startswith("commit:"):
        where.append("e.dst = %s")
        params.append(canonical)
    else:
        where.append("e.dst = %s")
        params.append(canonical)
    sql = ("WITH matched AS (SELECT DISTINCT ON (e.src_id) e.src_id, e.src_key AS key, "
           "e.src_version AS version, e.edge_type, e.dst, e.src_field, e.recorded_at, a.dst AS author "
           + _HEAD_JOIN + "WHERE " + " AND ".join(where) +
           " ORDER BY e.src_id, (e.dst = %s) DESC) "
           "SELECT src_id, key, version, edge_type, dst, src_field, recorded_at, author FROM matched "
           "ORDER BY recorded_at DESC, src_id DESC LIMIT %s")
    params.extend([canonical, int(limit) + 1])
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    truncated = len(rows) > limit
    rows = rows[:limit]
    src_ids = [r.pop("src_id") for r in rows]
    matched_dsts: set[str] = set()
    if src_ids:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT dst, src_field FROM mesh_edges WHERE edge_type = 'mentions' "
                        "AND src_id = ANY(%s::uuid[])", (src_ids,))
            fields = cur.fetchall()
            candidates = [r[0] for r in fields]
        if canonical.startswith("gh:#"):
            suffix = "#" + canonical.rsplit("#", 1)[1]
            matched_dsts = {dst for dst in candidates if dst == canonical or dst.endswith(suffix)}
        elif qualified:
            matched_dsts = {dst for dst, field in fields if dst == canonical
                            or (dst == f"gh:#{number}" and field != QUALIFIED_ONLY_FIELD)}
        else:
            matched_dsts = {dst for dst in candidates if dst == canonical}
    also = tuple(sorted(matched_dsts - {canonical}))
    return Mentions(canonical, tuple(MentionHit(Hit(**r), bool(qualified and r["dst"] != canonical))
                                     for r in rows), truncated, also)


def _artifact_mentions(conn, canonical: str, *, limit: int, gate: bool) -> Mentions:
    """Head messages naming a digest or a file consistently with `canonical`, newest first and visibly bounded.

    Every row that names a hex token carries `hash:<its first 8>`, and every row that names a path carries
    `path:<its file name>`, either as the reference itself or as an index-only key (normalize.HASH_PREFIX_FIELD,
    PATH_NAME_FIELD). So one equality on mesh_edges_dst_idx finds the candidates. A join to each candidate's own
    named references (never an index-only key) then keeps the rows that agree with the query:
    - a row that named as much as the query or more (a longer hash that starts with it, or a path that ends with
      `/` and the query) is a plain hit;
    - a row that named less (a shorter prefix of the hash, or a shorter tail of the path) is consistent but not
      proof, so it is a hit flagged `ambiguous`, as a bare #73 is in a repo-qualified search;
    - a row whose only references with that key disagree (another digest with the same first 8 characters,
      another directory's file of the same name) is not a hit.
    The hit reports the row's best reference: a plain one before an ambiguous one, then the longest.
    """
    kind, value = canonical.split(":", 1)
    if kind == "hash":
        key = f"hash:{value[:HASH_PREFIX_LEN]}"
        shorter = [f"hash:{value[:n]}" for n in range(HASH_PREFIX_LEN, len(value) + 1)]
        exact_sql = "left(h.dst, %s::int) = %s"
        exact_params: list = [len(canonical), canonical]
    else:
        parts = value.split("/")
        key = f"path:{parts[-1]}"
        shorter = [f"path:{'/'.join(parts[i:])}" for i in range(len(parts))]
        exact_sql = "(h.dst = %s OR (left(h.dst, 5) = 'path:' AND right(h.dst, %s::int) = %s))"
        exact_params = [canonical, len(value) + 1, "/" + value]
    sql = ("WITH matched AS (SELECT DISTINCT ON (e.src_id) e.src_id, e.src_key AS key, "
           "e.src_version AS version, e.edge_type, h.dst, h.src_field, e.recorded_at, a.dst AS author, "
           + exact_sql + " AS exact "
           + _HEAD_JOIN +
           "JOIN mesh_edges h ON h.src_id = e.src_id AND h.edge_type = 'mentions' AND h.src_field <> ALL(%s) "
           "AND (h.dst = ANY(%s) OR " + exact_sql + ") "
           "WHERE e.edge_type = 'mentions' AND e.dst = %s AND (%s OR sm.embed_policy = 'allowed') "
           "ORDER BY e.src_id, exact DESC, length(h.dst) DESC) "
           "SELECT src_id, key, version, edge_type, dst, src_field, recorded_at, author, exact FROM matched "
           "ORDER BY recorded_at DESC, src_id DESC LIMIT %s")
    params = [*exact_params, list(INDEX_ONLY_FIELDS), shorter, *exact_params, key, bool(gate), int(limit) + 1]
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    truncated = len(rows) > limit
    rows = rows[:limit]
    hits = []
    for r in rows:
        r.pop("src_id")
        exact = bool(r.pop("exact"))
        hits.append(MentionHit(Hit(**r), not exact))
    also = tuple(sorted({h.hit.dst for h in hits} - {canonical}))
    return Mentions(canonical, tuple(hits), truncated, also)

def custody(conn, ref: str, *, limit: int = 100, gate: bool = False) -> Custody:
    """Who cites one artifact: graph.mentions' newest hits for `ref`, grouped by the peer that SIGNED each row
    (issue #159, phase 2). A label is not an identity here, since one seat writes under several (kxxx's count: 44
    never-talked pairs by signer were 618 by label), so the holder is `created_by_peer_id` and its labels are listed.

    Advisory and bounded: it reads what mentions reads (head rows, the embed gate, `limit` newest hits, `truncated`
    when more exist), then one lookup of those exact row versions' signers and types. No body text; every holder carries
    keys to the signed rows its claim rests on. Grouping is in Python over at most `limit` hits (group_by_signer),
    so no aggregate runs in SQL."""
    found = mentions(conn, ref, limit=limit, gate=gate)
    signers = {key: (peer_id, kind) for key, peer_id, kind in _signer_rows(conn, found.hits)}
    return Custody(found.ref, group_by_signer(found.hits, signers), len(found.hits), found.truncated,
                   found.also_matched)


def _signer_rows(conn, hits: Sequence[MentionHit]) -> list[tuple[str, str | None, str | None]]:
    """(key, signing peer, message type) of the exact row version each hit came from: one row per hit.

    By the VERSION, not the key's head now (codex on #159 phase 2): the request runs no repeatable snapshot, so an
    edit committed between the two reads, which a sibling peer of the same owner may sign (sql_meshychat/029), would
    otherwise lend the citation to the replacement's signer and type. And in the mesh namespace only (memory-bxxxxx
    on #162): keys are unique only within a namespace, so without it a row with the same key and version anywhere
    else, signed by any peer with a writable namespace, could take the citation. Separate from custody so a test can
    see every row it returns, before custody's dict keeps one per key in whatever order they came."""
    if not hits:
        return []
    with conn.cursor() as cur:
        cur.execute("SELECT sm.key, sm.created_by_peer_id::text, sm.value->>'type' FROM shared_memory sm "
                    "JOIN unnest(%s::text[], %s::int[]) AS h(key, version) "
                    "ON sm.key = h.key AND sm.version = h.version WHERE sm.namespace = %s",
                    ([h.hit.key for h in hits], [h.hit.version for h in hits], MESH_CHAT_NAMESPACE))
        return [tuple(row) for row in cur.fetchall()]


def group_by_signer(hits: Sequence[MentionHit],
                    signers: Mapping[str, tuple[str | None, str | None]]) -> tuple[Holder, ...]:
    """Pure: mention hits (newest first, as graph.mentions returns them) and key -> (signing peer, message type),
    to one Holder per signing peer, the most recent citer first. A hit whose row has no recorded signer, or none in
    `signers`, falls under peer None: grouped, never dropped."""
    groups: dict[str | None, list[MentionHit]] = {}
    for h in hits:
        groups.setdefault(signers.get(h.hit.key, (None, None))[0], []).append(h)
    holders = []
    for peer_id, rows in groups.items():
        rows = sorted(rows, key=lambda h: (h.hit.recorded_at, h.hit.key), reverse=True)
        types: dict[str, int] = {}
        for h in rows:
            kind = signers.get(h.hit.key, (None, None))[1] or "untyped"
            types[kind] = types.get(kind, 0) + 1
        holders.append(Holder(
            peer_id=peer_id,
            labels=tuple(sorted({h.hit.author for h in rows if h.hit.author})),
            mentions=len(rows), ambiguous=sum(1 for h in rows if h.ambiguous),
            first_at=rows[-1].hit.recorded_at, last_at=rows[0].hit.recorded_at,
            keys=tuple(h.hit.key for h in rows[:HOLDER_KEYS]),
            types=dict(sorted(types.items()))))
    return tuple(sorted(holders, key=lambda x: (x.last_at, x.peer_id or ""), reverse=True))

#: The three owed-state predicates, shared by `owed_acks` and `owed_counts` so the list and its counts
#: cannot drift apart. Each takes the caller's labels as one %s parameter. Each starts from the edges that
#: POINT AT the ask (the dst index), checks the author, and joins the reply or close back to its head row:
#: a superseded version keeps its edges (sql 029 counted 53 author-rewritten versions on production, 8 of
#: them re-addressed), so without the join an old version could answer or close an ask its head no longer
#: does (kxxx's review of #79, 2026-10-03). `first_sm` and `t` are bound by the enclosing query.
#: A close is the owing peer's, or the ask's own author's (`a`, from _HEAD_JOIN): an asker withdraws a stale
#: ask with `--closes` (oxxxxxx's 75 obsolete asks, 2026-10-30), and nobody owes it after that.
#: A REDACTED reply answers nothing (a reviewer's cherry-pick report P1.1, 13 Nov): its tombstone keeps the routing
#: fields (apps/memoryskills/redact.py ROUTING_FIELDS, in_reply_to included), so without this filter an ask whose
#: only answer was redacted stays 'answered' forever. Applies to the direct reply and to in-thread activity; a close
#: stays a close (closing is routing, which redaction keeps by design).
_OWED_DIRECT = ("EXISTS (SELECT 1 FROM mesh_edges rr JOIN mesh_edges ra ON ra.src_id = rr.src_id "
                "AND ra.edge_type = 'authored_by' JOIN shared_memory sr ON sr.id = rr.src_id AND sr.is_head "
                "WHERE rr.edge_type = 'replies_to' AND rr.dst = e.src_key AND ra.dst = ANY(%s) "
                "AND NOT COALESCE((sr.value -> 'redacted') = 'true'::jsonb, false) )")
_OWED_CLOSED = ("EXISTS (SELECT 1 FROM mesh_edges cc JOIN mesh_edges ca ON ca.src_id = cc.src_id "
                "AND ca.edge_type = 'authored_by' JOIN shared_memory sc ON sc.id = cc.src_id AND sc.is_head "
                "WHERE cc.edge_type = 'closes' AND cc.dst = e.src_key AND (ca.dst = ANY(%s) OR ca.dst = a.dst))")
_OWED_DECLINED = ("EXISTS (SELECT 1 FROM mesh_edges dd JOIN mesh_edges da ON da.src_id = dd.src_id "
                  "AND da.edge_type = 'authored_by' JOIN shared_memory sd ON sd.id = dd.src_id AND sd.is_head "
                  "WHERE dd.edge_type = 'declines' AND dd.dst = e.src_key AND da.dst = ANY(%s))")
_OWED_ACTIVITY = ("EXISTS (SELECT 1 FROM mesh_edges rt JOIN mesh_edges r ON r.src_id = rt.src_id "
                  "AND r.edge_type = 'authored_by' JOIN shared_memory sr ON sr.id = rt.src_id AND sr.is_head "
                  "WHERE rt.edge_type = 'in_thread' AND rt.dst = t.dst AND r.dst = ANY(%s) "
                  "AND r.recorded_at > first_sm.created_at "
                  "AND NOT COALESCE((sr.value -> 'redacted') = 'true'::jsonb, false) )")

#: An ask's time is its FIRST version's, not its head's: an edit writes a new version whose edges carry the
#: edit's time (apps/memoryskills/edit.py writes no created_at; normalize.py `recorded_at=row["created_at"]`),
#: and owed ordered, filtered and judged activity by it (finding 18). The earliest version present, not
#: `version = 1` by equality, so an ask whose version 1 is not in this database (a partial restore, a
#: migrated row) keeps its place in owed instead of dropping out of an inner join.
_OWED_FIRST = ("CROSS JOIN LATERAL (SELECT min(f.created_at) AS created_at FROM shared_memory f "
               "WHERE f.namespace = sm.namespace AND f.key = sm.key) first_sm ")


def owed_acks(conn, label: str | Sequence[str], *,
              limit: int | None = 100, thread_id: str | None = None,
              keys: Sequence[str] | None = None, states: Sequence[str] | None = None,
              since: _dt.datetime | None = None) -> tuple[OwedAck, ...]:
    """The OPEN(P, M) invariant from the fleet's delivery protocol, computed from edges.

    OPEN(P, M) := addressed_to(P, M) AND requires_ack(M) AND NOT authored_by(P, M),
    reported alongside whether P has authored anything in M's thread since M. With
    several labels, P is all of them: a message P authored under ANY of its labels is
    not owed by P, and speaking under any of them counts as having spoken.

    `thread_id`, `keys`, `states` and `since` narrow the question IN the query, before
    `limit` applies. Filtering the global answer afterwards would let a caller owing more
    than `limit` acks elsewhere lose an in-scope ask to the limit. An empty `keys` asks
    about no message; an empty `states` is a caller error rather than an ambiguous "all".
    """
    labels = _labels(label)
    # A direct reply is observable activity, not a settlement; only an explicit close ends
    # the debt. Callers that inspect historical settlements ask for state=closed explicitly.
    if states is None:
        states = list(DEFAULT_OWED_STATES)
    else:
        states = list(states)
        if not states:
            raise ValueError("at least one owed state is required")
        unknown = set(states) - {"open", "active", "answered", "declined", "closed"}
        if unknown:
            raise ValueError(f"unknown owed state(s): {', '.join(sorted(unknown))}")
    # The predicates and the first-version time are shared with owed_counts (above).
    direct = _OWED_DIRECT
    closed = _OWED_CLOSED
    declined = _OWED_DECLINED
    activity = _OWED_ACTIVITY
    sql = (
        "SELECT e.src_key AS key, e.src_version AS version, e.edge_type, e.dst, "
        "       e.src_field, first_sm.created_at AS recorded_at, a.dst AS author, t.dst AS thread_id, "
        f"       {activity} AS answered_in_thread, {direct} AS answered_directly, "
        f"       CASE WHEN {closed} THEN 'closed' WHEN {declined} THEN 'declined' WHEN {direct} THEN 'answered' WHEN {activity} THEN 'active' "
        "            ELSE 'open' END AS state, sm.value->>'done_when' AS done_when, "
        "       (sm.version <> 1) AS edited, sm.value->>'type' AS msg_type, "
        "       sm.value->'directive'->>'expires_at' AS directive_expires_at, sm.value->>'window_end' AS window_end, "
        "       (sm.value->'operator_words') IS NOT NULL AS relayed "
        + _HEAD_JOIN + _OWED_FIRST +
        "LEFT JOIN mesh_edges t ON t.src_id = e.src_id AND t.edge_type = 'in_thread' "
        "WHERE e.edge_type = 'owes_ack' AND e.dst = ANY(%s) "
        "  AND (a.dst IS NULL OR a.dst <> ALL(%s)) "
        + ("  AND t.dst = %s " if thread_id is not None else "")
        + ("  AND e.src_key = ANY(%s) " if keys is not None else "")
        + ("  AND first_sm.created_at >= %s " if since is not None else "")
        + (f"  AND CASE WHEN {closed} THEN 'closed' WHEN {declined} THEN 'declined' WHEN {direct} THEN 'answered' WHEN {activity} THEN 'active' "
           "           ELSE 'open' END = ANY(%s) " if states is not None else "")
        + "ORDER BY first_sm.created_at DESC, e.src_id DESC"
    )
    # SELECT's activity/direct/state, then WHERE's existing predicates. The filter's
    # duplicated predicates are intentional: PostgreSQL cannot use a SELECT alias in
    # WHERE, and moving this test after LIMIT hides in-scope asks.
    params: list = [labels, labels, labels, labels, labels, labels, labels, labels]
    if thread_id is not None:
        params.append(thread_id)
    if keys is not None:
        params.append(list(keys))
    if since is not None:
        params.append(since)
    if states is not None:
        params.extend([labels, labels, labels, labels, states])
    if limit is not None:
        sql += " LIMIT %s"
        params.append(int(limit))
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    out = []
    for r in rows:
        thread_id = r.pop("thread_id")
        answered = bool(r.pop("answered_in_thread"))
        directly = bool(r.pop("answered_directly"))
        state = r.pop("state")
        done_when = r.pop("done_when")
        edited = bool(r.pop("edited"))
        msg_type = r.pop("msg_type")
        expires_at = owed_expiry(r.pop("directive_expires_at"), r.pop("window_end"), bool(r.pop("relayed")))
        out.append(OwedAck(hit=Hit(**r), thread_id=thread_id, answered_in_thread=answered,
                           answered_directly=directly, state=state, done_when=done_when, expires_at=expires_at,
                           edited=edited, msg_type=msg_type))
    return tuple(out)


def _owing_groups(conn, owing_labels: Sequence[str]) -> dict[str, list[str]]:
    """For each owing label, every label of the ONE peer it addresses, as apps/mesh/views.labels_for
    answers that peer (roster label, an uncontested peers.name, peer-<id8>): the set owed_acks reads for
    that peer as a caller, so owed and awaiting agree on every (ask, peer).

    A candidate peer is one whose roster label, peers.name or fallback equals the label; it OWNS the label
    only if labels_for still answers it for that label. So a peers.name another peer holds, or one the
    roster gives to another peer (divergence 066), owns nothing, and its peer's replies never answer an
    ask addressed to it (codex on #89, finding 2). A roster label resolves whether or not it is also some
    peer's name (finding 3). A label no peer owns, or one two peers would, stands alone."""
    from apps.memoryskills import handlers
    from apps.mesh import views          # labels_for; views imports this module, so not at the top

    candidates: dict[str, set[str]] = {label: set() for label in owing_labels}
    fallback = {label[len("peer-"):]: label for label in owing_labels
                if len(label) == 13 and label.startswith("peer-")}
    with conn.cursor() as cur:
        cur.execute("SELECT id, name FROM peers WHERE name = ANY(%s)", (list(owing_labels),))
        for peer_id, name in cur.fetchall():
            candidates[name].add(str(peer_id))
        if fallback:
            cur.execute("SELECT id FROM peers WHERE substr(id::text, 1, 8) = ANY(%s)", (list(fallback),))
            for (peer_id,) in cur.fetchall():
                candidates[fallback[str(peer_id)[:8]]].add(str(peer_id))
    for peer_id, roster in handlers._ROSTER_LABELS.items():
        if roster in candidates:
            candidates[roster].add(str(peer_id))
    groups: dict[str, list[str]] = {}
    for label, peer_ids in candidates.items():
        owners = []
        for peer_id in sorted(peer_ids):
            labels = [x["label"] for x in views.labels_for(conn, peer_id)]
            if label in labels:
                owners.append(labels)
        groups[label] = sorted(set(owners[0])) if len(owners) == 1 else [label]
    return groups


def owed_counts(conn, label: str | Sequence[str], *, since: _dt.datetime | None = None) -> dict[str, int]:
    """Count owed asks by the exact `owed_acks` predicates, without its output limit."""
    labels = _labels(label)
    direct = _OWED_DIRECT
    closed = _OWED_CLOSED
    declined = _OWED_DECLINED
    activity = _OWED_ACTIVITY
    state = ("CASE WHEN is_closed THEN 'closed' WHEN is_declined THEN 'declined' "
             "WHEN is_direct THEN 'answered' WHEN is_active THEN 'active' ELSE 'open' END")
    sql = ("WITH candidates AS MATERIALIZED ("
           "SELECT " + closed + " AS is_closed, " + declined + " AS is_declined, "
           + direct + " AS is_direct, " + activity + " AS is_active "
           + _HEAD_JOIN + _OWED_FIRST +
           "LEFT JOIN mesh_edges t ON t.src_id = e.src_id AND t.edge_type = 'in_thread' "
           "WHERE e.edge_type = 'owes_ack' AND e.dst = ANY(%s) "
           "AND (a.dst IS NULL OR a.dst <> ALL(%s)) "
           + ("AND first_sm.created_at >= %s " if since is not None else "") + ") "
           "SELECT " + state + " AS state, count(*) AS count FROM candidates GROUP BY 1")
    params: list = [labels, labels, labels, labels, labels, labels]
    if since is not None:
        params.append(since)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    counts = {state: 0 for state in ("open", "active", "answered", "declined", "closed")}
    counts.update({r["state"]: int(r["count"]) for r in rows})
    return counts


def awaiting_counts(conn, label: str | Sequence[str], *, since: _dt.datetime | None = None) -> dict[str, int]:
    """Count `awaiting`'s exact predicates in one unbounded GROUP BY."""
    return awaiting(conn, label, states=("open", "active", "answered", "declined", "closed"),
                    since=since, limit=None, count_states=True)


def thread_brief(conn, label: str | Sequence[str], *, since: _dt.datetime,
                 limit: int) -> tuple[ThreadBrief, ...]:
    """Unread other-party activity after the caller's latest message in each thread."""
    labels = _labels(label)
    sql = (
        "WITH caller_last AS ("
        " SELECT e.dst AS thread_id, max(e.recorded_at) AS caller_recorded_at "
        + _HEAD_JOIN + "WHERE e.edge_type = 'in_thread' AND a.dst = ANY(%s) GROUP BY e.dst"
        "), new_messages AS ("
        " SELECT cl.thread_id, m.src_id, m.recorded_at, ma.dst AS author "
        " FROM caller_last cl "
        " JOIN mesh_edges m ON m.edge_type = 'in_thread' AND m.dst = cl.thread_id "
        " JOIN shared_memory msm ON msm.id = m.src_id AND msm.is_head "
        " LEFT JOIN mesh_edges ma ON ma.src_id = m.src_id AND ma.edge_type = 'authored_by' "
        " WHERE m.recorded_at > cl.caller_recorded_at AND m.recorded_at >= %s "
        "   AND (ma.dst IS NULL OR ma.dst <> ALL(%s))"
        ") SELECT thread_id, count(*) AS new, max(recorded_at) AS last_recorded_at, "
        " (array_agg(author ORDER BY recorded_at DESC, src_id DESC))[1] AS last_author "
        " FROM new_messages GROUP BY thread_id ORDER BY max(recorded_at) DESC, thread_id ASC LIMIT %s"
    )
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, [labels, since, labels, int(limit)])
        rows = cur.fetchall()
    return tuple(ThreadBrief(thread_id=r["thread_id"], new=int(r["new"]),
                             last_recorded_at=r["last_recorded_at"], last_author=r["last_author"])
                 for r in rows)


def help_wanted(conn, label: str | Sequence[str], *, since: _dt.datetime,
                help_after: _dt.timedelta, limit: int) -> tuple[dict, ...]:
    """Old broadcast asks that no one has answered or closed, oldest first.

    An answer is a head reply by someone OTHER than the asker: an asker's own follow-up ("any
    takers?") is not one. "Other" is the SIGNER, `created_by_peer_id`, not the author label: one
    peer writes under its roster label, its name or its `peer-<id8>` fallback, so a follow-up under
    another of its labels compared unequal and hid the ask for good (Codex on #91). A close by
    anyone, the asker included, settles it.

    One item per ASK: an ask to both `all` and `both` has an owes_ack edge to each, so the rows are
    made distinct by message before the limit, or one ask would fill two slots and could report
    `truncated` alone (Codex on #91)."""
    labels = _labels(label)
    sql = (
        "SELECT key, author, thread_id, recorded_at, done_when FROM ("
        "SELECT DISTINCT ON (e.src_id) e.src_id, e.src_key AS key, a.dst AS author, t.dst AS thread_id, "
        "e.recorded_at, sm.value->>'done_when' AS done_when " + _HEAD_JOIN +
        "LEFT JOIN mesh_edges t ON t.src_id = e.src_id AND t.edge_type = 'in_thread' "
        "WHERE e.edge_type = 'owes_ack' AND e.dst = ANY(%s) "
        "AND (a.dst IS NULL OR a.dst <> ALL(%s)) "
        "AND e.recorded_at >= %s AND e.recorded_at <= now() - %s::interval "
        "AND NOT EXISTS (SELECT 1 FROM mesh_edges rr JOIN shared_memory sr "
        "ON sr.id = rr.src_id AND sr.is_head "
        "WHERE rr.edge_type = 'replies_to' AND rr.dst = e.src_key "
        "AND sr.created_by_peer_id <> sm.created_by_peer_id "
        "AND NOT COALESCE((sr.value -> 'redacted') = 'true'::jsonb, false)) "
        "AND NOT EXISTS (SELECT 1 FROM mesh_edges cc JOIN shared_memory sc "
        "ON sc.id = cc.src_id AND sc.is_head WHERE cc.edge_type = 'closes' "
        "AND cc.dst = e.src_key) "
        "ORDER BY e.src_id, t.dst) asks "
        "ORDER BY recorded_at ASC, src_id ASC LIMIT %s"
    )
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, [sorted(BROADCAST_LABELS), labels, since, help_after, int(limit)])
        return tuple(dict(r) for r in cur.fetchall())


@dataclasses.dataclass(frozen=True)
class Unread:
    """The caller's fetch backlog in a window: newest first, at most `limit`, with the
    whole window's size and oldest time beside it."""

    items: tuple[dict, ...]
    total: int
    oldest_at: _dt.datetime | None
    truncated: bool


#: The caller's completed reads of mesh mail since the window opened: ONE range of
#: skill_calls_caller_created_idx (caller_peer_id, created_at DESC), filtered on the row, never
#: a lookup per message. A read cannot predate the version it fetched, so reads before `since`
#: cannot clear mail written after it. A read counts by the version it RETURNED: `memo.version`
#: in its result, or `memo_version` once the read-result prune has replaced the result with its
#: summary (apps/calls/retention.py). Only a read whose result says neither (pruned before the
#: summary kept the version, or redacted) falls back to what it ASKED for, read as handle_read's
#: int() reads it: a number truncated, a boolean as 0 or 1, a string with surrounding whitespace,
#: a sign and `_` between digit groups. Only a version that is ABSENT or null is a read of the head,
#: and only that counts by time (made at or after the head was written). A version that was given
#: but cannot be read clears nothing (Codex on #111: ' 1 ' and '+1' fell to the time rule and a
#: read that asked for version 1 cleared version 2).
#: skill_calls.args is the WHOLE SIGNED PAYLOAD (apps/calls/repository.insert_call), so the
#: memory.read arguments are its `args` member: sc.args->'args'->>'key', never sc.args->>'key',
#: which is NULL on every real row and would leave all mail pending forever.
#: `reads` is MATERIALIZED (kxxx, msg/<key>, divergence 081). It was added when a
#: NOT EXISTS against it was inlined and re-ran the skill_calls scan once per pending message (loops=352,
#: 1,166 ms for one heavy reader on a production copy; 145 ms materialized). Today only `cleared` reads
#: it, once; it stays MATERIALIZED so that a second reference can never re-run the scan. It changes the
#: plan, never the rows.
#: `cleared` (3 Nov 2026): the reads are matched to the pending mail by a LOOKUP, never by a join. That
#: 145 ms was measured as the table owner, which bypasses row-level security. The route runs as
#: meshychat_app in server_internal, where skill_calls' SELECT policies (an OR of current_setting()
#: comparisons, each at the default selectivity) multiply into the estimate: `reads` was planned at
#: 1 row and held 21,288 for one heavy reader on production, so the planner chose a Nested Loop Anti
#: Join that rescanned the whole CTE once per pending message (1,622 loops, 19.4 M rows removed by
#: the join filter, 8.3 s; 0.89 s as the owner, which happened to get a hash). No estimate can be
#: trusted here, so the plan must not depend on one: `cleared` folds the reads into ONE row, a jsonb
#: object from each key to the three numbers that decide it, and each pending message makes one
#: lookup in it (a binary search over the object's sorted keys). The rule is the old NOT EXISTS's own:
#: a message is cleared when SOME read of its key satisfies one of three conditions, each a `>=` on a
#: single field of that read, so it is cleared exactly when the field's MAXIMUM over those reads
#: satisfies it (max ignores NULL, as the comparison would). Each maximum is taken over its OWN kind of
#: read only, the FILTERs being the old conjunctions. The time is kept as its epoch, an exact numeric
#: (microseconds), so the comparison never goes through a timestamp's text form and the session
#: TimeZone. A read whose args carry no key matched nothing before and is left out here, where a NULL
#: object key would be an error. Size: one entry per distinct key the peer read in the window (6,678
#: for the heaviest reader on production), far below jsonb's object limit.
#: The object is read through an uncorrelated scalar subquery, which Postgres runs ONCE (an InitPlan)
#: and then hands to every row by reference. Joined as a one-row CTE instead, it was rescanned per
#: pending message and the whole object copied each time (0.43 ms a row, 0.66 s of 0.9 s, measured).
_CLEARED_HIT = "((SELECT by_key FROM cleared)->e.src_key)"
_UNREAD_SQL = (
    "WITH reads AS MATERIALIZED ("
    "SELECT sc.args->'args'->>'key' AS key, "
    "COALESCE(CASE WHEN jsonb_typeof(sc.result->'memo'->'version') = 'number' "
    "THEN (sc.result->'memo'->>'version')::numeric END, "
    "CASE WHEN jsonb_typeof(sc.result->'memo_version') = 'number' "
    "THEN (sc.result->>'memo_version')::numeric END) AS returned, "
    "(NOT (sc.args->'args' ? 'version') OR jsonb_typeof(sc.args->'args'->'version') = 'null') AS head_read, "
    "CASE jsonb_typeof(sc.args->'args'->'version') "
    "WHEN 'number' THEN trunc((sc.args->'args'->>'version')::numeric) "
    "WHEN 'boolean' THEN CASE WHEN sc.args->'args'->>'version' = 'true' THEN 1 ELSE 0 END "
    "WHEN 'string' THEN CASE WHEN btrim(sc.args->'args'->>'version', ' ' || chr(9) || chr(10) || chr(11) || chr(12) || chr(13)) ~ '^[+-]?[0-9]+(_[0-9]+)*$' "
    "THEN replace(btrim(sc.args->'args'->>'version', ' ' || chr(9) || chr(10) || chr(11) || chr(12) || chr(13)), '_', '')::numeric END END AS asked, sc.created_at "
    "FROM skill_calls sc "
    "WHERE sc.caller_peer_id = %s::uuid AND sc.created_at >= %s "
    "AND sc.skill_name = 'memory.read' AND sc.status = 'completed' "
    "AND sc.args->'args'->>'namespace' = %s), "
    "cleared AS MATERIALIZED ("
    "SELECT COALESCE(jsonb_object_agg(key, jsonb_build_array(returned, asked, head_at)), '{}'::jsonb) AS by_key "
    "FROM (SELECT key, max(returned) AS returned, "
    "max(asked) FILTER (WHERE returned IS NULL AND NOT head_read) AS asked, "
    "max(extract(epoch FROM created_at)) FILTER (WHERE returned IS NULL AND head_read) AS head_at "
    "FROM reads WHERE key IS NOT NULL GROUP BY key) per_key), "
    "pending AS ("
    "SELECT DISTINCT ON (e.src_id) e.src_id, e.src_key AS key, e.src_version AS version, "
    "a.dst AS author, e.dst, sm.created_at AS version_at " + _HEAD_JOIN +
    "WHERE e.edge_type = 'addressed_to' AND e.dst = ANY(%s) AND sm.created_at >= %s "
    "AND sm.created_by_peer_id IS DISTINCT FROM %s::uuid "
    "AND NOT COALESCE((" + _CLEARED_HIT + "->>0)::numeric >= e.src_version "
    "OR (" + _CLEARED_HIT + "->>1)::numeric >= e.src_version "
    "OR (" + _CLEARED_HIT + "->>2)::numeric >= extract(epoch FROM sm.created_at), false) "
    "ORDER BY e.src_id, e.dst) "
    "SELECT key, version, author, dst, version_at, "
    "count(*) OVER () AS total, min(version_at) OVER () AS oldest_at "
    "FROM pending ORDER BY version_at DESC, src_id DESC LIMIT %s"
)


#: Divergence 084 (issue #141): how many of the sender's messages to the recipient are unanswered, exactly
#: as send-filter/2026-10-05 scored it (export.sql's `ub`): sends addressed (to) to the recipient since the
#: recipient last wrote a message addressed (to) to the sender. Both sides by label (authored_by,
#: addressed_to edges), both bounded below by `since`. The bound is put on each edge's `recorded_at` as well
#: as on the row's `created_at`: the index (edge_type, dst, recorded_at) serves only the former, and an edge
#: is recorded when its version is projected, never before it is written, so `recorded_at >= since` keeps
#: every edge of a row written in the window (Codex on #146: bounded on created_at alone, a busy label's
#: whole history was read and filtered through the joins).
_UNANSWERED_SQL = (
    "WITH last_reply AS ("
    "  SELECT max(sm.created_at) AS t FROM mesh_edges a "
    "  JOIN mesh_edges d ON d.src_id = a.src_id AND d.edge_type = 'addressed_to' AND d.dst = ANY(%(sender)s) "
    "  JOIN shared_memory sm ON sm.id = a.src_id AND sm.is_head "
    "  WHERE a.edge_type = 'authored_by' AND a.dst = ANY(%(recipient)s) AND sm.created_at >= %(since)s "
    "    AND a.recorded_at >= %(since)s AND d.recorded_at >= %(since)s) "
    "SELECT count(DISTINCT a.src_id) FROM mesh_edges a "
    "JOIN mesh_edges d ON d.src_id = a.src_id AND d.edge_type = 'addressed_to' AND d.dst = ANY(%(recipient)s) "
    "JOIN shared_memory sm ON sm.id = a.src_id AND sm.is_head "
    "WHERE a.edge_type = 'authored_by' AND a.dst = ANY(%(sender)s) AND sm.created_at >= %(since)s "
    "  AND a.recorded_at >= %(since)s AND d.recorded_at >= %(since)s "
    "  AND sm.created_at > COALESCE((SELECT t FROM last_reply), '-infinity'::timestamptz)"
)


def unanswered_from(conn, sender_labels: Sequence[str], recipient_labels: Sequence[str], *,
                    since: _dt.datetime) -> int:
    """The sender's messages to the recipient since the recipient last wrote to the sender (divergence 084).
    `to` only on both sides, as the scored definition; cc is neither a send nor a reply here."""
    with conn.cursor() as cur:
        cur.execute(_UNANSWERED_SQL, {"sender": list(sender_labels), "recipient": list(recipient_labels),
                                      "since": since})
        return int(cur.fetchone()[0])


def unread(conn, label: str | Sequence[str], *, peer_id, since: _dt.datetime,
           limit: int) -> Unread:
    """Mail addressed to `label` (any of several; to, not cc) whose HEAD version this peer has
    not fetched: no COMPLETED memory.read by `peer_id` in shared/mesh-chat of that key that
    RETURNED that version or a later one. The returned version is the read's own answer, so a
    read that ran during an edit's transaction and fetched version 1 never clears version 2
    (Codex on #111: judged by time, an edit's version is stamped with its transaction's start
    and such a read cleared it, so the edit never showed as backlog). See _UNREAD_SQL for the
    fallback a read whose result no longer says takes.

    A FETCH, NOT AN ACTION. A completed read means the client fetched the message; whether a
    person or a model then looked at it, or acted on it, is not something the server can see.
    A failed or refused read clears nothing.

    The window is the head version's write time, so an edit makes a fetched message pending
    again and its age starts at the edit (an edit cannot change `to` or `cc`, divergence 059,
    so the recipients are the same at every version). The caller's own messages are excluded
    (a message is never delivered to its own author). Broadcast labels are not this peer's labels, so a broadcast
    is not counted, as in `inbox`. One message addressed to two of the peer's labels is one row.

    The fallback keeps the old edge for the reads it covers: one that asked for the head inside
    an edit's transaction is credited to the edit. Only reads pruned before the summary kept
    `memo_version`, and reads of redacted rows, take it."""
    labels = _labels(label)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_UNREAD_SQL, [str(peer_id), since, MESH_CHAT_NAMESPACE, labels, since,
                                  str(peer_id), int(limit) + 1])
        rows = cur.fetchall()
    total = int(rows[0]["total"]) if rows else 0
    oldest = rows[0]["oldest_at"] if rows else None
    items = tuple({k: r[k] for k in ("key", "version", "author", "dst", "version_at")}
                  for r in rows[:limit])
    return Unread(items=items, total=total, oldest_at=oldest, truncated=len(rows) > limit)


@dataclasses.dataclass(frozen=True)
class FleetUnreadCount:
    """One peer's fetch backlog count and oldest pending message time for /v1/mesh/fleet/unread."""

    total: int
    oldest_at: _dt.datetime | None


#: Batch equivalent of `_UNREAD_SQL` for `/v1/mesh/fleet/unread` (up to `FLEET_PEER_CAP` candidate
#: peers in ONE statement instead of one `_UNREAD_SQL` round-trip per peer). Keeps the 3 Nov 2026
#: RLS-safe lookup plan: `reads` is `MATERIALIZED` from one range scan of
#: `skill_calls_caller_created_idx` (`caller_peer_id = ANY(%s::uuid[]) AND created_at >= %s`),
#: `cleared` folds all covered readers' per-key maxima into ONE nested `jsonb` object
#: `{peer_id: {key: [returned, asked, head_at]}}` read through a single uncorrelated scalar
#: subquery (`InitPlan`), and `addressed` walks `mesh_edges_dst_idx` (`edge_type = 'addressed_to'`,
#: `dst = ANY(%s::text[])`, `recorded_at >= %s`) once across all covered labels, deduplicating
#: `(peer_id, src_id)` before checking `by_peer->peer_id->key`.
_FLEET_UNREAD_SQL = (
    "WITH peer_labels AS MATERIALIZED ("
    "SELECT pl.peer_id, pl.label FROM unnest(%s::uuid[], %s::text[]) AS pl(peer_id, label)), "
    "reads AS MATERIALIZED ("
    "SELECT sc.caller_peer_id, sc.args->'args'->>'key' AS key, "
    "COALESCE(CASE WHEN jsonb_typeof(sc.result->'memo'->'version') = 'number' "
    "THEN (sc.result->'memo'->>'version')::numeric END, "
    "CASE WHEN jsonb_typeof(sc.result->'memo_version') = 'number' "
    "THEN (sc.result->>'memo_version')::numeric END) AS returned, "
    "(NOT (sc.args->'args' ? 'version') OR jsonb_typeof(sc.args->'args'->'version') = 'null') AS head_read, "
    "CASE jsonb_typeof(sc.args->'args'->'version') "
    "WHEN 'number' THEN trunc((sc.args->'args'->>'version')::numeric) "
    "WHEN 'boolean' THEN CASE WHEN sc.args->'args'->>'version' = 'true' THEN 1 ELSE 0 END "
    "WHEN 'string' THEN CASE WHEN btrim(sc.args->'args'->>'version', ' ' || chr(9) || chr(10) || chr(11) || chr(12) || chr(13)) ~ '^[+-]?[0-9]+(_[0-9]+)*$' "
    "THEN replace(btrim(sc.args->'args'->>'version', ' ' || chr(9) || chr(10) || chr(11) || chr(12) || chr(13)), '_', '')::numeric END END AS asked, sc.created_at "
    "FROM skill_calls sc "
    "WHERE sc.caller_peer_id = ANY(%s::uuid[]) AND sc.created_at >= %s "
    "AND sc.skill_name = 'memory.read' AND sc.status = 'completed' "
    "AND sc.args->'args'->>'namespace' = %s), "
    "cleared AS MATERIALIZED ("
    "SELECT COALESCE(jsonb_object_agg(caller_peer_id::text, by_key), '{}'::jsonb) AS by_peer "
    "FROM (SELECT caller_peer_id, "
    "jsonb_object_agg(key, jsonb_build_array(returned, asked, head_at)) AS by_key "
    "FROM (SELECT caller_peer_id, key, max(returned) AS returned, "
    "max(asked) FILTER (WHERE returned IS NULL AND NOT head_read) AS asked, "
    "max(extract(epoch FROM created_at)) FILTER (WHERE returned IS NULL AND head_read) AS head_at "
    "FROM reads WHERE key IS NOT NULL GROUP BY caller_peer_id, key) per_key "
    "GROUP BY caller_peer_id) per_peer), "
    "addressed AS ("
    "SELECT DISTINCT ON (pl.peer_id, e.src_id) "
    "pl.peer_id, e.src_key AS key, e.src_version AS version, sm.created_at AS version_at "
    "FROM mesh_edges e "
    "JOIN peer_labels pl ON pl.label = e.dst "
    "JOIN shared_memory sm ON sm.id = e.src_id AND sm.is_head "
    "WHERE e.edge_type = 'addressed_to' AND e.dst = ANY(%s::text[]) "
    "AND e.recorded_at >= %s AND sm.created_at >= %s "
    "AND sm.created_by_peer_id IS DISTINCT FROM pl.peer_id "
    "ORDER BY pl.peer_id, e.src_id), "
    "checked AS ("
    "SELECT a.peer_id, a.version, a.version_at, "
    "(((SELECT by_peer FROM cleared)->(a.peer_id::text))->a.key) AS hit "
    "FROM addressed a) "
    "SELECT peer_id::text AS peer_id, count(*) AS total, min(version_at) AS oldest_at "
    "FROM checked "
    "WHERE NOT COALESCE((hit->>0)::numeric >= version "
    "OR (hit->>1)::numeric >= version "
    "OR (hit->>2)::numeric >= extract(epoch FROM version_at), false) "
    "GROUP BY peer_id"
)


def fleet_unread_counts(conn, peer_labels: Sequence[tuple[str, Sequence[str]]], *,
                        since: _dt.datetime) -> dict[str, FleetUnreadCount]:
    """Batch fetch-backlog counts and oldest pending timestamps for `peer_labels`
    (`(peer_id, labels)` pairs), agreeing peer for peer with `unread(..., limit=1)`."""
    out: dict[str, FleetUnreadCount] = {}
    peer_ids: list[str] = []
    flat_peers: list[str] = []
    flat_labels: list[str] = []
    all_labels: set[str] = set()
    for peer_id, raw_labels in peer_labels:
        pid = str(peer_id)
        labels = _labels(raw_labels)
        if pid in out:
            continue
        out[pid] = FleetUnreadCount(total=0, oldest_at=None)
        peer_ids.append(pid)
        for lab in dict.fromkeys(labels):
            flat_peers.append(pid)
            flat_labels.append(lab)
            all_labels.add(lab)
    if not peer_ids:
        return out
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_FLEET_UNREAD_SQL, [
            flat_peers, flat_labels, peer_ids, since, MESH_CHAT_NAMESPACE,
            sorted(all_labels), since, since,
        ])
        for row in cur.fetchall():
            out[str(row["peer_id"])] = FleetUnreadCount(
                total=int(row["total"]), oldest_at=row["oldest_at"])
    return out


@dataclasses.dataclass(frozen=True)
class FleetCandidates:
    """The peers a fleet backlog answer covers (divergence 081), and why each is in it.

    `peers` is ordered: peers a label of the window's addressed mail can name first, since only
    they can have backlog, then the rest, each group by peer id. `addressed_labels` is every
    label addressed in the window, so the caller can say how many no covered peer answers for."""

    peers: tuple[str, ...]
    addressed: frozenset[str]
    senders: frozenset[str]
    roster: frozenset[str]
    addressed_labels: frozenset[str]


#: Peers that wrote mesh mail in the window: the shared_memory rows of shared/mesh-chat.
_FLEET_SENDERS_SQL = (
    "SELECT DISTINCT sm.created_by_peer_id::text AS peer_id FROM shared_memory sm "
    "WHERE sm.namespace = %s AND sm.created_at >= %s AND sm.created_by_peer_id IS NOT NULL"
)

#: Every label addressed (to, not cc) in the window. An edge is recorded when its version is
#: projected, never before the version is written, so `recorded_at >= since` keeps every edge
#: of a head written in the window (a superset is fine: these only name candidates).
_FLEET_ADDRESSED_SQL = (
    "SELECT DISTINCT e.dst FROM mesh_edges e "
    "WHERE e.edge_type = 'addressed_to' AND e.recorded_at >= %s"
)

#: The active peers among the senders, the roster and the peers an addressed label can name:
#: by `peers.name` or by the `peer-<id8>` fallback (views.labels_for). peers is small (about
#: 1,900 rows when last measured), so one pass over it.
_FLEET_PEERS_SQL = (
    "SELECT p.id::text AS peer_id, "
    "(p.name = ANY(%s) OR left(p.id::text, 8) = ANY(%s)) AS addressed "
    "FROM peers p WHERE p.status = 'active' AND ("
    "p.id = ANY(%s::uuid[]) OR p.name = ANY(%s) OR left(p.id::text, 8) = ANY(%s))"
)


#: The label `views.labels_for` answers a peer for when neither the roster nor peers.name does.
_FALLBACK_LABEL = re.compile(r"peer-[0-9a-f]{8}")


def fleet_candidates(conn, *, since: _dt.datetime, roster: Mapping[str, str]) -> FleetCandidates:
    """The peers worth a backlog count in a window, bounded by what happened in it rather than
    by `peers.status` (about 1,870 of 1,871 rows are active; kxxx, msg/<key>).

    A peer is a candidate when it wrote mesh mail in the window, when the server's roster names
    it, or when a label addressed in the window can be its label. Membership only bounds the
    set: each candidate's count comes from `unread()` with that peer's own labels, so a peer
    whose name is contested (views.labels_for drops it) is still counted exactly as its own
    /v1/mesh/unread would count it. A peer that neither wrote nor was addressed in the window
    and is not on the roster has nothing in the window to count, so it is left out."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_FLEET_SENDERS_SQL, [MESH_CHAT_NAMESPACE, since])
        senders = {r["peer_id"] for r in cur.fetchall()}
        cur.execute(_FLEET_ADDRESSED_SQL, [since])
        labels = {r["dst"] for r in cur.fetchall()}
        roster_ids = {str(k) for k in roster}
        prefixes = sorted({x[5:] for x in labels if _FALLBACK_LABEL.fullmatch(x)})
        names = sorted(labels)
        cur.execute(_FLEET_PEERS_SQL, [names, prefixes, sorted(senders | roster_ids), names, prefixes])
        rows = cur.fetchall()
    active = {r["peer_id"] for r in rows}
    # A roster peer is addressed when its roster label is: the SQL sees only names and ids.
    addressed = {r["peer_id"] for r in rows if r["addressed"]} | {
        str(k) for k, v in roster.items() if v in labels and str(k) in active}
    ordered = sorted(addressed) + sorted(active - addressed)
    return FleetCandidates(peers=tuple(ordered), addressed=frozenset(addressed),
                           senders=frozenset(senders & active), roster=frozenset(roster_ids & active),
                           addressed_labels=frozenset(labels))


def awaiting(conn, label: str | Sequence[str], *, states: Sequence[str] | None = None,
             since: _dt.datetime | None = None, limit: int | None = 100,
             count_states: bool = False) -> tuple[AwaitingAck, ...] | dict[str, int]:
    """Mirror owed_acks for asks the caller authored, one row per (ask, owing label).

    This repeats owed's correlated predicates because its recipient label is the selected
    `e.dst` per result row, not one static argument. Keeping it explicit prevents a helper
    from accidentally evaluating all owing labels as one composite peer.
    """
    labels = _labels(label)
    if states is None:
        states = list(DEFAULT_OWED_STATES)
    else:
        states = list(states)
        if not states or set(states) - {"open", "active", "answered", "declined", "closed"}:
            raise ValueError("unknown awaiting state")
    # The owing label is an address, not necessarily the label stamped on its peer's
    # reply: roster and peers.name are both live identity sources (views.labels_for).
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT e.dst " + _HEAD_JOIN +
                    "WHERE e.edge_type = 'owes_ack' AND a.dst = ANY(%s) AND e.dst <> ALL(%s)",
                    (labels, labels))
        owing_labels = [r[0] for r in cur.fetchall()]
    groups = _owing_groups(conn, owing_labels)
    if not groups:
        return ({state: 0 for state in ("open", "active", "answered", "declined", "closed")}
                if count_states else ())
    values_sql = ", ".join("(%s, %s::text[])" for _ in groups)
    group_params: list = []
    for owing_label, equivalent in groups.items():
        group_params.extend([owing_label, equivalent])
    # The same checks as owed_acks, head join included, so owed and awaiting agree on every
    # (ask, peer): a superseded reply or close no longer answers or closes the ask its head left. A close
    # by the asker itself (`a.dst`) withdraws the ask for every owing label, as in owed.
    direct = ("EXISTS (SELECT 1 FROM mesh_edges rr JOIN mesh_edges ra ON ra.src_id = rr.src_id "
              "AND ra.edge_type = 'authored_by' JOIN shared_memory sr ON sr.id = rr.src_id AND sr.is_head "
              "WHERE rr.edge_type = 'replies_to' AND rr.dst = e.src_key AND ra.dst = ANY(lg.labels) "
              "AND NOT COALESCE((sr.value -> 'redacted') = 'true'::jsonb, false) )")
    closed = ("EXISTS (SELECT 1 FROM mesh_edges cc JOIN mesh_edges ca ON ca.src_id = cc.src_id "
              "AND ca.edge_type = 'authored_by' JOIN shared_memory sc ON sc.id = cc.src_id AND sc.is_head "
              "WHERE cc.edge_type = 'closes' AND cc.dst = e.src_key AND (ca.dst = ANY(lg.labels) OR ca.dst = a.dst))")
    declined = ("EXISTS (SELECT 1 FROM mesh_edges dd JOIN mesh_edges da ON da.src_id = dd.src_id "
                "AND da.edge_type = 'authored_by' JOIN shared_memory sd ON sd.id = dd.src_id AND sd.is_head "
                "WHERE dd.edge_type = 'declines' AND dd.dst = e.src_key AND da.dst = ANY(lg.labels))")
    activity = ("EXISTS (SELECT 1 FROM mesh_edges rt JOIN mesh_edges r ON r.src_id = rt.src_id "
                "AND r.edge_type = 'authored_by' JOIN shared_memory sr ON sr.id = rt.src_id AND sr.is_head "
                "WHERE rt.edge_type = 'in_thread' AND rt.dst = t.dst AND r.dst = ANY(lg.labels) "
                "AND r.recorded_at > e.recorded_at "
                "AND NOT COALESCE((sr.value -> 'redacted') = 'true'::jsonb, false) )")
    state = ("CASE WHEN is_closed THEN 'closed' WHEN is_declined THEN 'declined' "
             "WHEN is_direct THEN 'answered' WHEN is_active THEN 'active' ELSE 'open' END")
    # State predicates used to be rendered once for SELECT and again for WHERE. In the
    # `state=open` workload that made the correlated checks walk twice per ask.
    # Materialize their booleans once, after each reply/close lookup has used its dst index.
    sql = ("WITH label_groups(label, labels) AS (VALUES " + values_sql + "), candidates AS MATERIALIZED ("
           "SELECT e.src_key AS key, e.src_version AS version, e.edge_type, e.dst, e.src_field, "
           "e.recorded_at, a.dst AS author, e.dst AS owing_label, sm.value->>'done_when' AS done_when, "
           + closed + " AS is_closed, " + declined + " AS is_declined, "
           + direct + " AS is_direct, " + activity + " AS is_active "
           + _HEAD_JOIN + "LEFT JOIN mesh_edges t ON t.src_id = e.src_id AND t.edge_type = 'in_thread' "
           "JOIN label_groups lg ON lg.label = e.dst "
           "WHERE e.edge_type = 'owes_ack' AND a.dst = ANY(%s) AND e.dst <> ALL(%s) " +
           ("AND e.recorded_at >= %s" if since is not None else "") + ") ")
    if count_states:
        sql += "SELECT " + state + " AS state, count(*) AS count FROM candidates GROUP BY 1"
    else:
        sql += ("SELECT key, version, edge_type, dst, src_field, recorded_at, author, owing_label, "
                + state + " AS state, done_when FROM candidates " +
                ("WHERE " + state + " = ANY(%s) " if states is not None else "") +
                "ORDER BY recorded_at DESC")
    params: list = group_params + [labels, labels]
    if since is not None:
        params.append(since)
    if states is not None and not count_states:
        params.append(list(states))
    if limit is not None and not count_states:
        sql += " LIMIT %s"
        params.append(int(limit))
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    if count_states:
        counts = {state: 0 for state in ("open", "active", "answered", "declined", "closed")}
        counts.update({r["state"]: int(r["count"]) for r in rows})
        return counts
    return tuple(AwaitingAck(hit=Hit(key=r["key"], version=r["version"], edge_type=r["edge_type"],
                                     dst=r["dst"], src_field=r["src_field"], recorded_at=r["recorded_at"],
                                     author=r["author"]), owing_label=r["owing_label"], state=r["state"],
                             done_when=r["done_when"]) for r in rows)


def to_close(conn, label: str | Sequence[str], *, since: _dt.datetime,
             settled_before: _dt.datetime, limit: int = 12) -> tuple[tuple[ToCloseItem, ...], bool]:
    """Answered, unclosed asks authored by the caller whose direct replies have settled.

    Non-paging (`due` is always False): items are listed only after every owing recipient on
    the ask has either settled (closed/declined) or posted a direct non-redacted reply at or
    before `settled_before`, so an asker-signed `--closes` never prematurely withdraws an ask
    from a co-recipient who has not yet answered.
    """
    labels = _labels(label)
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT e.dst " + _HEAD_JOIN +
                    "WHERE e.edge_type = 'owes_ack' AND a.dst = ANY(%s) AND e.dst <> ALL(%s)",
                    (labels, labels))
        owing_labels = [r[0] for r in cur.fetchall()]
    groups = _owing_groups(conn, owing_labels)
    if not groups:
        return (), False
    values_sql = ", ".join("(%s, %s::text[])" for _ in groups)
    group_params: list = []
    for owing_label, equivalent in groups.items():
        group_params.extend([owing_label, equivalent])
    closed = ("EXISTS (SELECT 1 FROM mesh_edges tc_c JOIN mesh_edges tc_ca ON tc_ca.src_id = tc_c.src_id "
              "AND tc_ca.edge_type = 'authored_by' JOIN shared_memory tc_sc ON tc_sc.id = tc_c.src_id AND tc_sc.is_head "
              "WHERE tc_c.edge_type = 'closes' AND tc_c.dst = e.src_key AND (tc_ca.dst = ANY(lg.labels) OR tc_ca.dst = a.dst))")
    declined = ("EXISTS (SELECT 1 FROM mesh_edges dd JOIN mesh_edges da ON da.src_id = dd.src_id "
                "AND da.edge_type = 'authored_by' JOIN shared_memory sd ON sd.id = dd.src_id AND sd.is_head "
                "WHERE dd.edge_type = 'declines' AND dd.dst = e.src_key AND da.dst = ANY(lg.labels))")
    latest_reply = (
        "LEFT JOIN LATERAL ("
        "  SELECT rr.src_key AS key, sr.version AS version, sr.created_at AS recorded_at, "
        "         ra.dst AS author, rr.src_id AS src_id "
        "  FROM mesh_edges rr "
        "  JOIN mesh_edges ra ON ra.src_id = rr.src_id AND ra.edge_type = 'authored_by' "
        "  JOIN shared_memory sr ON sr.id = rr.src_id AND sr.is_head "
        "  WHERE rr.edge_type = 'replies_to' AND rr.dst = e.src_key AND ra.dst = ANY(lg.labels) "
        "    AND NOT COALESCE((sr.value -> 'redacted') = 'true'::jsonb, false) "
        "  ORDER BY sr.created_at DESC, rr.src_id DESC LIMIT 1"
        ") lr ON TRUE "
    )
    sql = (
        "WITH label_groups(label, labels) AS (VALUES " + values_sql + "), candidates AS MATERIALIZED ("
        "SELECT e.src_id, e.src_key AS key, e.src_version AS version, e.recorded_at, "
        "e.dst AS owing_label, t.dst AS thread_id, sm.value->>'done_when' AS done_when, "
        + closed + " AS is_closed, " + declined + " AS is_declined, "
        "lr.key AS latest_reply_key, lr.version AS latest_reply_version, "
        "lr.recorded_at AS latest_reply_at, lr.author AS latest_reply_author "
        + _HEAD_JOIN +
        "LEFT JOIN mesh_edges t ON t.src_id = e.src_id AND t.edge_type = 'in_thread' "
        "JOIN label_groups lg ON lg.label = e.dst "
        + latest_reply +
        "WHERE e.edge_type = 'owes_ack' AND a.dst = ANY(%s) AND e.dst <> ALL(%s) "
        "  AND e.recorded_at >= %s"
        ") "
        "SELECT c.key, c.version, c.recorded_at, c.owing_label, c.thread_id, c.done_when, "
        "       c.latest_reply_key, c.latest_reply_version, c.latest_reply_at, c.latest_reply_author "
        "FROM candidates c "
        "WHERE NOT c.is_closed AND NOT c.is_declined "
        "  AND c.latest_reply_key IS NOT NULL AND c.latest_reply_at <= %s "
        "  AND NOT EXISTS ("
        "      SELECT 1 FROM candidates c2 "
        "      WHERE c2.src_id = c.src_id AND NOT c2.is_closed AND NOT c2.is_declined "
        "        AND (c2.latest_reply_key IS NULL OR c2.latest_reply_at > %s)"
        "  ) "
        "ORDER BY c.latest_reply_at ASC, c.recorded_at ASC, c.src_id ASC, c.owing_label ASC "
        "LIMIT %s"
    )
    params: list = group_params + [labels, labels, since, settled_before, settled_before, int(limit) + 1]
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    truncated = len(rows) > limit
    items = tuple(
        ToCloseItem(
            key=r["key"],
            version=int(r["version"]),
            recorded_at=r["recorded_at"],
            owing_label=r["owing_label"],
            thread_id=r["thread_id"],
            done_when=r["done_when"],
            latest_reply_key=r["latest_reply_key"],
            latest_reply_version=int(r["latest_reply_version"]),
            latest_reply_at=r["latest_reply_at"],
            latest_reply_author=r["latest_reply_author"],
            due=False,
        )
        for r in rows[:limit]
    )
    return items, truncated


#: coverage() rides on every inbox, owed, awaiting, brief, unread, fleet-unread and mentions answer
#: (views._coverage), so it ran once per request: 3,665 ms on production as meshychat_app on 3 Nov 2026, at
#: about 880 such answers an hour, near one core of database time for a pair that moves by a few rows a
#: minute. It exists to tell "nothing for you" from "the projection is days behind" (module docstring), so
#: each server process now holds it COVERAGE_TTL_S per (database, namespace), as
#: apps/meshsem/related_reads holds its own. The pair can be up to COVERAGE_TTL_S old: projected < total for
#: under a minute after a write is expected, not a stuck projection (divergence 107). Every caller reads it
#: inside server_internal, and the only row policies on shared_memory and mesh_edges test that flag and
#: nothing about the caller, so one held pair serves every caller. Two misses inside one expiry both compute
#: it; at a few requests a second per process that costs one extra count, never a wrong one.
#:
#: The count runs with no parallel workers, in a savepoint (divergence 107, amended 4 Nov 2026). Its parallel
#: hash join takes dynamic shared memory, and production's Postgres container has Docker's default 64 MB
#: /dev/shm: `could not resize shared memory segment` (DiskFull) made 33 mail reads answer 500 on 4 Nov, over a
#: number those reads only carry. Serial took 760-900 ms against 420-510, once a minute per process. When the
#: count fails anyway the read serves the pair already held and holds it one more TTL, so a stall costs one
#: failed count a minute, not one per read. With nothing held the error stands: there is no pair to serve.
COVERAGE_TTL_S = 60.0
_coverage_held: dict[tuple, tuple[float, tuple[int, int]]] = {}
_coverage_lock = threading.Lock()
_clock = time.monotonic


def _coverage_key(conn, namespace: str) -> tuple:
    """(host, port, database, namespace): one test run makes several databases in one process."""
    info = getattr(conn, "info", None)
    return (getattr(info, "host", None), getattr(info, "port", None), getattr(info, "dbname", None),
            namespace)


def forget_coverage() -> None:
    """Drop every held pair. The test suite's one database serves every test of a run."""
    with _coverage_lock:
        _coverage_held.clear()


def coverage(conn, *, namespace: str = MESH_CHAT_NAMESPACE) -> tuple[int, int]:
    """(head messages with at least one projected edge, head messages in the namespace).

    A PAIR, for the same reason apps/board/projection.adoption is: the numerator alone
    is a number this project has already been burned reporting without its denominator.

    Held COVERAGE_TTL_S per (database, namespace) in this process (the note above). The
    numerator counts the heads with an edge through EXISTS. That equals the
    count(DISTINCT e.src_id) over the join it replaced, because the join matched e.src_id
    against sm.id, the primary key: 1,253 ms against 3,665 ms on production, the same pair.
    The numerator still has no `key LIKE 'msg/%'` and the total still has it.
    """
    key = _coverage_key(conn, namespace)
    with _coverage_lock:
        held = _coverage_held.get(key)
        if held is not None and _clock() - held[0] < COVERAGE_TTL_S:
            return held[1]
    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("SELECT current_setting('max_parallel_workers_per_gather')")
            workers_were = cur.fetchone()[0]
            cur.execute("SET LOCAL max_parallel_workers_per_gather = 0")
            cur.execute(
                "SELECT "
                "  (SELECT count(*) FROM shared_memory sm "
                "    WHERE sm.namespace = %s AND sm.is_head "
                "      AND EXISTS (SELECT 1 FROM mesh_edges e WHERE e.src_id = sm.id)), "
                "  (SELECT count(*) FROM shared_memory "
                "    WHERE namespace = %s AND is_head AND key LIKE 'msg/%%')",
                (namespace, namespace),
            )
            projected, total = cur.fetchone()
            cur.execute("SELECT set_config('max_parallel_workers_per_gather', %s, true)", (workers_were,))
    except psycopg.Error as exc:
        if held is None:
            raise
        log.warning("coverage count failed (%s); serving the held pair", type(exc).__name__)
        with _coverage_lock:
            _coverage_held[key] = (_clock(), held[1])
        return held[1]
    pair = (int(projected), int(total))
    with _coverage_lock:
        _coverage_held[key] = (_clock(), pair)
    return pair


def drift(conn) -> dict[str, dict[str, int]]:
    """edge_type -> src_field -> count. The four spellings and two types, as numbers.

    This is what makes a migration finishing WATCHABLE: `in_thread.thread` and
    `replies_to.reply_to` and `addressed_to.to:string` should trend to zero for new rows
    once every writer is on v1, and this is the query that says whether they have.

    Head versions only, like every read here (module docstring). A superseded version keeps its
    edges, so without the join each rewritten message counted once per version, and a spelling
    its head no longer uses kept counting (finding 23 of the 2026-10-04 graph review).
    """
    with conn.cursor() as cur:
        cur.execute("SELECT e.edge_type, e.src_field, count(*) FROM mesh_edges e "
                    "JOIN shared_memory sm ON sm.id = e.src_id AND sm.is_head "
                    "GROUP BY 1, 2 ORDER BY 1, 2")
        rows = cur.fetchall()
    out: dict[str, dict[str, int]] = {t: {} for t in EDGE_TYPES}
    for edge_type, src_field, n in rows:
        out.setdefault(edge_type, {})[src_field] = int(n)
    return out


def render_hits(title: str, result: Hits) -> Iterable[str]:
    """Say it in words. A structured field alone is absent to a reader reading prose."""
    yield f"{title}: {len(result.hits)} message(s)" + (" (truncated)" if result.truncated else "")
    for h in result.hits:
        yield (f"  {h.recorded_at.isoformat()}  {h.key}  from {h.author or '<unattributed>'}"
               f"  via {h.edge_type} [{h.src_field}]")


def render_coverage(pair: tuple[int, int]) -> str:
    projected, total = pair
    pct = (100.0 * projected / total) if total else 0.0
    return (f"  Coverage: {projected} of {total} head messages carry a projected edge "
            f"({pct:.1f}%). An empty result is a fact about coverage before it is a fact "
            f"about the mail.")
