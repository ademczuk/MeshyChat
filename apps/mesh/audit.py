"""Read-only checks of the artifact mentions (`hash:` and `path:`, divergence 089) against the extractor as it is.

The projection only inserts (`ON CONFLICT DO NOTHING`), so what an older extractor wrote stays when the extractor is
fixed, and what a backfill writes cannot be taken back without a rebuild. Two reports for whoever runs one:

  stale        the artifact edges in mesh_edges that the extractor no longer writes for their row. It lists them
               and deletes nothing: a delete is a separate, deliberate step.
  frequency    what the extractor reads as a digest in the head rows, by length and most frequent first, each
               with the text in front of it. It reads bodies, not edges, so it shows what a backfill WOULD write.
               Beside it, the same counts for the hex tokens the identifier rule keeps out: a digest skipped in
               error leaves no edge, so only this side of the report can show it.

Both run under a context the caller holds, as graph.py does. `frequency` prints message text (the characters before
a digest): it is a report for the operator of the database, not something to post.
"""
from __future__ import annotations

import collections
import datetime as _dt
import re
from typing import Iterable

from psycopg.rows import dict_row

from apps.memoryskills.validation import MESH_CHAT_NAMESPACE

from . import normalize

ARTIFACT_PREFIXES = ("hash:", "path:")
#: Rows and edges read per query, and the most either report reads in all. A report that stops at the cap says so.
PAGE = 2000
DEFAULT_CAP = 2_000_000
_SPACE = re.compile(r"\s+")


def stale_artifacts(row: dict, dsts: Iterable[str]) -> list[str]:
    """The `hash:` and `path:` dsts among `dsts` that the extractor does not write for this row, sorted.

    Extractor against extractor: the embed gate is not asked, since it decides whether a row has mentions at all,
    not which.
    """
    current = {t[4] for t in normalize.project_row(row, mentions_allowed=True).rows() if t[3] == "mentions"}
    return sorted({d for d in dsts if d.startswith(ARTIFACT_PREFIXES)} - current)


def digest_contexts(body: object, *, width: int = 24) -> list[tuple[str, str, bool]]:
    """For each digest the extractor reads in a body: (hex token, the `width` characters before it, whether the
    row also gets `commit:<its first 7>` from the older kind). Runs of whitespace count as one space, so a wrapped
    line and an aligned column read alike.

    The third field answers kxxx's question after the first deploy: a bare SHA such as `ea0a130b8e64` is written
    both as `commit:ea0a130` and as `hash:ea0a130b8e64`, by design (089), and a length class that is mostly such
    twins is a class of commit citations rather than of file digests.
    """
    return _contexts(body, normalize.digest_positions(body), width)


def identifier_contexts(body: object, *, width: int = 24) -> list[tuple[str, str, bool]]:
    """The same three fields for each hex token the identifier rule keeps out of the digests. A report of what is
    read cannot show a digest skipped in error, for example one on the line after `...from the peer:`; this one can."""
    return _contexts(body, normalize.identifier_positions(body), width)


def _contexts(body: object, found: list[tuple[int, str]], width: int) -> list[tuple[str, str, bool]]:
    if not found:
        return []
    commits = {ref for ref, _ in normalize.mention_ref_fields(body) if ref.startswith("commit:")}
    return [(value, _SPACE.sub(" ", body[max(0, start - 4 * width):start])[-width:], f"commit:{value[:7]}" in commits)
            for start, value in found]


class Tally:
    """The digests of `digest_contexts`, counted as they are read: per length, how many, how many the `commit:` kind
    takes too, the most frequent values, and the most frequent texts in front of them. It keeps a count per distinct
    value and context, never the occurrences, so a row that names one digest a hundred times costs one entry (codex)."""

    def __init__(self) -> None:
        self.total = 0
        self._count: collections.Counter = collections.Counter()
        self._commit: collections.Counter = collections.Counter()
        self._values: dict[int, collections.Counter] = collections.defaultdict(collections.Counter)
        self._before: dict[int, collections.Counter] = collections.defaultdict(collections.Counter)

    def add(self, found: Iterable[tuple[str, str, bool]]) -> None:
        for value, before, also_commit in found:
            length = len(value)
            self.total += 1
            self._count[length] += 1
            self._commit[length] += bool(also_commit)
            self._values[length][value] += 1
            self._before[length][before] += 1

    def report(self, *, top: int = 15) -> dict:
        return {str(length): {
            "count": self._count[length],
            "distinct": len(self._values[length]),
            "also_commit": self._commit[length],
            "top_values": [{"value": v, "count": n} for v, n in _most_common(self._values[length], top)],
            "top_before": [{"before": b, "count": n} for b, n in _most_common(self._before[length], top)],
        } for length in sorted(self._count, key=lambda n: (-self._count[n], n))}


def summarize(found: Iterable[tuple[str, str, bool]], *, top: int = 15) -> dict:
    """`Tally` over one iterable."""
    tally = Tally()
    tally.add(found)
    return tally.report(top=top)


def _most_common(counter: collections.Counter, top: int) -> list[tuple[str, int]]:
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:max(0, top)]


def frequency_report(read: Tally, skipped: Tally, *, rows_read: int, rows_with: int, truncated: bool,
                     top: int = 15) -> dict:
    """What `frequency` returns, from its two tallies. Each total stands beside its own breakdown: `digests` with
    `by_length` (what the extractor reads), `skipped_as_identifier` with `skipped_by_length` (what the identifier
    rule keeps out). A function of its own so that a test without a database can hold the two apart
    (memory-bxxxxx on #167)."""
    return {"rows_read": rows_read, "rows_with_a_digest": rows_with, "truncated": truncated,
            "digests": read.total, "by_length": read.report(top=top),
            "skipped_as_identifier": skipped.total, "skipped_by_length": skipped.report(top=top)}


def stale(conn, *, since: _dt.datetime | None = None, cap: int = DEFAULT_CAP) -> dict:
    """Every artifact edge the extractor would not write today, oldest row first. Read-only."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT e.src_id::text AS src_id, e.dst, e.src_field, e.recorded_at FROM mesh_edges e "
            "WHERE e.edge_type = 'mentions' AND (e.dst LIKE 'hash:%%' OR e.dst LIKE 'path:%%') "
            "AND (%(since)s::timestamptz IS NULL OR e.recorded_at >= %(since)s::timestamptz) "
            "ORDER BY e.recorded_at, e.src_id, e.dst LIMIT %(cap)s", {"since": since, "cap": int(cap) + 1})
        edges = cur.fetchall()
        truncated = len(edges) > cap
        edges = edges[:cap]
        by_row: dict[str, list[dict]] = collections.defaultdict(list)
        for edge in edges:
            by_row[edge["src_id"]].append(edge)
        found: list[dict] = []
        ids = list(by_row)
        rows_read = 0
        for i in range(0, len(ids), PAGE):
            cur.execute("SELECT sm.id::text AS id, sm.key, sm.version, sm.value, sm.author_label, sm.created_at "
                        "FROM shared_memory sm WHERE sm.id = ANY(%s::uuid[])", (ids[i:i + PAGE],))
            for row in cur.fetchall():
                rows_read += 1
                gone = set(stale_artifacts(row, [e["dst"] for e in by_row[row["id"]]]))
                found += [{"src_id": row["id"], "src_key": row["key"], "src_version": row["version"],
                           "dst": e["dst"], "src_field": e["src_field"], "recorded_at": e["recorded_at"].isoformat()}
                          for e in by_row[row["id"]] if e["dst"] in gone]
    found.sort(key=lambda f: (f["recorded_at"], f["src_id"], f["dst"]))
    kinds = collections.Counter(f["dst"].split(":", 1)[0] for f in found)
    return {"edges_read": len(edges), "rows_read": rows_read, "truncated": truncated,
            "stale_count": len(found), "stale_by_kind": dict(sorted(kinds.items())), "stale": found}


def frequency(conn, *, since: _dt.datetime | None = None, top: int = 15, width: int = 24,
              cap: int = DEFAULT_CAP, namespace: str = MESH_CHAT_NAMESPACE) -> dict:
    """The digests the extractor reads in the head rows a backfill would walk, counted. Read-only.

    The embed gate applies as it does to the backfill: a row whose mentions are not allowed is not read for them.
    """
    from apps.meshsem import store
    gate_open = store.forbidden_rows_open()
    tally, skipped = Tally(), Tally()
    rows_read = rows_with = 0
    after = "00000000-0000-0000-0000-000000000000"
    truncated = False
    with conn.cursor(row_factory=dict_row) as cur:
        while True:
            cur.execute(
                "SELECT sm.id::text AS id, sm.value, sm.embed_policy FROM shared_memory sm "
                "WHERE sm.namespace = %(ns)s AND sm.is_head AND sm.key LIKE 'msg/%%' AND sm.id > %(after)s::uuid "
                "AND (%(since)s::timestamptz IS NULL OR sm.created_at >= %(since)s::timestamptz) "
                "ORDER BY sm.id LIMIT %(limit)s", {"ns": namespace, "after": after, "since": since, "limit": PAGE})
            rows = cur.fetchall()
            for row in rows:
                if rows_read >= cap:
                    truncated = True
                    break
                rows_read += 1
                value = row["value"]
                if not isinstance(value, dict) or not (gate_open or row.get("embed_policy") == "allowed"):
                    continue
                here = digest_contexts(value.get("body"), width=width)
                rows_with += bool(here)
                tally.add(here)
                skipped.add(identifier_contexts(value.get("body"), width=width))
            if truncated or len(rows) < PAGE:
                break
            after = rows[-1]["id"]
    return frequency_report(tally, skipped, rows_read=rows_read, rows_with=rows_with, truncated=truncated, top=top)
