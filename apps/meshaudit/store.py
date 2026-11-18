"""Every SQL statement the auditor runs. Callers hold the `server_internal` context.

WHICH ROWS ARE AUDITED. A head `msg/` row of shared/mesh-chat that the DECLARED projection
cannot place, meaning mesh_edges holds no `in_thread` edge for it OR no `addressed_to`
edge, AND that the projection has already walked (at or before the mesh_edges watermark).
The second condition is what makes "mesh_edges has no edge for it" mean "its sender
declared none" rather than "the catch-up has not reached it yet"; a row past the watermark
is simply not a candidate yet. With no watermark at all nothing is eligible and the caller
is told why (ProjectionNotRun), never handed an empty success.

Rows already audited under the CURRENT prompt version are skipped
(`mesh_inferred_audits`), newest first, bounded by the caller's limit.

KNOWN PEER LABELS are the authenticated `authored_by` labels in mesh_edges (the label the
daemon stamps on every write) and every `peers.name` (each seat's own handle, which senders
also address it by). Both are facts in this database. The in-process roster is not read:
a roster label that has ever written is already an `authored_by` label, and one that never
wrote is not something a legacy message can have been addressed to by a model's say-so.

THE FACT CHECKS READ DECLARED EDGES ONLY. `thread_exists` asks mesh_edges and never
mesh_inferred_edges, so an inference cannot vouch for the next one.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from apps.memoryskills.validation import MESH_CHAT_NAMESPACE

from .validate import EDGE_TYPES, ContextMessage, Proposal

#: How far back the context reaches, and how many rows it holds.
CONTEXT_ROWS = 6
CONTEXT_WINDOW = _dt.timedelta(hours=6)


class ProjectionNotRun(RuntimeError):
    """mesh_edges has never been caught up, so "declares no edge" cannot be told apart
    from "not projected yet", and nothing is eligible."""


def watermark(conn) -> tuple[_dt.datetime, str] | None:
    with conn.cursor() as cur:
        cur.execute("SELECT watermark_at, watermark_id FROM mesh_edges_projection "
                    "WHERE projection = 'mesh_edges'")
        row = cur.fetchone()
    if row is None or row[0] is None:
        return None
    return row[0], str(row[1])


#: The eligibility predicate, shared by the walk and by coverage so the two can never
#: count different populations.
_ELIGIBLE = (
    "sm.namespace = %(ns)s AND sm.is_head AND sm.key LIKE 'msg/%%' "
    "AND (sm.created_at, sm.id) <= (%(wm_at)s::timestamptz, %(wm_id)s::uuid) "
    "AND (NOT EXISTS (SELECT 1 FROM mesh_edges e "
    "                 WHERE e.src_id = sm.id AND e.edge_type = 'in_thread') "
    "     OR NOT EXISTS (SELECT 1 FROM mesh_edges e "
    "                    WHERE e.src_id = sm.id AND e.edge_type = 'addressed_to')) "
)


def select_subjects(conn, *, prompt_version: str, limit: int, keys: list[str] | None = None,
                    namespace: str = MESH_CHAT_NAMESPACE) -> list[dict]:
    wm = watermark(conn)
    if wm is None:
        raise ProjectionNotRun("mesh_edges has no watermark: run `python -m apps.mesh "
                               "catch-up` (or the mesh_edges_catch_up job) first")
    params = {"ns": namespace, "wm_at": wm[0], "wm_id": wm[1], "pv": prompt_version,
              "limit": int(limit)}
    sql = ("SELECT sm.id, sm.key, sm.version, sm.value, sm.author_label, sm.created_at, "
           "       p.name AS author_name "
           "FROM shared_memory sm LEFT JOIN peers p ON p.id = sm.created_by_peer_id "
           "WHERE " + _ELIGIBLE +
           "AND NOT EXISTS (SELECT 1 FROM mesh_inferred_audits a "
           "                WHERE a.src_id = sm.id AND a.prompt_version = %(pv)s) ")
    if keys is not None:
        sql += "AND sm.key = ANY(%(keys)s) "
        params["keys"] = list(keys)
    sql += "ORDER BY sm.created_at DESC, sm.id DESC LIMIT %(limit)s"
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def known_labels(conn) -> frozenset[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT dst FROM mesh_edges WHERE edge_type = 'authored_by' "
                    "UNION SELECT name FROM peers WHERE name <> ''")
        return frozenset(r[0] for r in cur.fetchall() if isinstance(r[0], str) and r[0])


def context_for(conn, row: dict, *, namespace: str = MESH_CHAT_NAMESPACE,
                rows: int = CONTEXT_ROWS, window: _dt.timedelta = CONTEXT_WINDOW
                ) -> list[ContextMessage]:
    """The head messages sent just before `row`, oldest first, with their DECLARED thread.
    Every field is a fact: key, stamped author label, and a mesh_edges thread or None."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT sm.key, sm.author_label, "
            "       CASE WHEN jsonb_typeof(sm.value->'body') = 'string' "
            "            THEN left(sm.value->>'body', 400) ELSE '' END AS body, "
            "       (SELECT t.dst FROM mesh_edges t WHERE t.src_id = sm.id "
            "          AND t.edge_type = 'in_thread' LIMIT 1) AS thread "
            "FROM shared_memory sm "
            "WHERE sm.namespace = %s AND sm.is_head AND sm.key LIKE 'msg/%%' "
            "  AND sm.created_at < %s AND sm.created_at >= %s AND sm.id <> %s "
            "ORDER BY sm.created_at DESC, sm.id DESC LIMIT %s",
            (namespace, row["created_at"], row["created_at"] - window, row["id"], int(rows)))
        found = cur.fetchall()
    return [ContextMessage(key=r["key"], author=r["author_label"] or "", thread=r["thread"],
                           snippet=r["body"] or "") for r in reversed(found)]


def threads_of_keys(conn, keys) -> tuple[str, ...]:
    """The declared threads of the msg/ keys a body mentions: a message that cites another
    is plausibly in its thread, and that thread is a fact."""
    keys = [k for k in keys if isinstance(k, str) and k.startswith("msg/")]
    if not keys:
        return ()
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT dst FROM mesh_edges "
                    "WHERE edge_type = 'in_thread' AND src_key = ANY(%s) ORDER BY dst",
                    (keys,))
        return tuple(r[0] for r in cur.fetchall())


class DbFacts:
    """validate.Facts over the database, read inside the caller's transaction, so the
    facts are as fresh as the write they gate."""

    def __init__(self, conn, *, namespace: str = MESH_CHAT_NAMESPACE):
        self._conn, self._ns = conn, namespace

    def is_known_label(self, label: str) -> bool:
        with self._conn.cursor() as cur:
            cur.execute("SELECT EXISTS (SELECT 1 FROM mesh_edges "
                        "               WHERE edge_type = 'authored_by' AND dst = %s) "
                        "    OR EXISTS (SELECT 1 FROM peers WHERE name = %s)",
                        (label, label))
            return bool(cur.fetchone()[0])

    def thread_exists(self, thread_id: str) -> bool:
        with self._conn.cursor() as cur:
            cur.execute("SELECT EXISTS (SELECT 1 FROM mesh_edges "
                        "               WHERE edge_type = 'in_thread' AND dst = %s)",
                        (thread_id,))
            return bool(cur.fetchone()[0])

    def message_first_seen(self, key: str) -> _dt.datetime | None:
        with self._conn.cursor() as cur:
            cur.execute("SELECT min(created_at) FROM shared_memory "
                        "WHERE namespace = %s AND key = %s", (self._ns, key))
            return cur.fetchone()[0]


_INSERT_EDGE = (
    "INSERT INTO mesh_inferred_edges (src_id, src_key, src_version, edge_type, dst, model, "
    "  provider_id, prompt_version, recorded_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
    # No conflict target: the primary key AND the one-thread/one-reply partial index both
    # refuse a second row, and either refusal is a no-op counted by the caller.
    "ON CONFLICT DO NOTHING"
)


@dataclasses.dataclass(frozen=True)
class Written:
    inserted: int
    #: Accepted proposals the table already held, or that would give a message a second
    #: thread or reply target (an earlier prompt version's inference stands).
    already_held: int


def record(conn, row: dict, *, outcome: str, prompt_version: str,
           accepted: tuple[Proposal, ...] = (), refused: dict[str, int] | None = None,
           asked: tuple[str, ...] = (), model: str | None = None,
           provider_id: str | None = None) -> Written:
    """Write the accepted edges and the ledger row, in the caller's transaction."""
    inserted = 0
    with conn.cursor() as cur:
        for p in accepted:
            if p.edge_type not in EDGE_TYPES:            # validate.py already refused it
                raise ValueError(f"not an inferable edge type: {p.edge_type!r}")
            cur.execute(_INSERT_EDGE, (row["id"], row["key"], row["version"], p.edge_type,
                                       p.dst, model, provider_id, prompt_version,
                                       row["created_at"]))
            inserted += max(cur.rowcount, 0)
        cur.execute(
            "INSERT INTO mesh_inferred_audits (src_id, prompt_version, outcome, model, "
            "  provider_id, asked, accepted, refused) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (src_id, prompt_version) DO NOTHING",
            (row["id"], prompt_version, outcome, model, provider_id, list(asked), inserted,
             Jsonb(refused or {})))
    return Written(inserted=inserted, already_held=len(accepted) - inserted)


def coverage(conn, *, prompt_version: str, namespace: str = MESH_CHAT_NAMESPACE) -> dict:
    """The denominators for every inferred answer.

    eligible   head rows the declared projection cannot place (and has walked)
    audited    of those, how many the CURRENT prompt version has asked about
    edges      inferred edges on head rows, by type, across every prompt version
    """
    wm = watermark(conn)
    edges = {t: 0 for t in EDGE_TYPES}
    with conn.cursor() as cur:
        cur.execute("SELECT i.edge_type, count(*) FROM mesh_inferred_edges i "
                    "JOIN shared_memory sm ON sm.id = i.src_id AND sm.is_head "
                    "WHERE sm.namespace = %s GROUP BY 1", (namespace,))
        for t, n in cur.fetchall():
            edges[t] = int(n)
        if wm is None:
            return {"eligible": 0, "audited": 0, "edges": edges,
                    "prompt_version": prompt_version, "projection_not_run": True}
        cur.execute(
            "SELECT count(*), count(a.src_id) FROM shared_memory sm "
            "LEFT JOIN mesh_inferred_audits a ON a.src_id = sm.id "
            "  AND a.prompt_version = %(pv)s WHERE " + _ELIGIBLE,
            {"ns": namespace, "wm_at": wm[0], "wm_id": wm[1], "pv": prompt_version})
        eligible, audited = cur.fetchone()
    return {"eligible": int(eligible), "audited": int(audited), "edges": edges,
            "prompt_version": prompt_version, "projection_not_run": False}


__all__ = ["CONTEXT_ROWS", "CONTEXT_WINDOW", "DbFacts", "ProjectionNotRun", "Written",
           "context_for", "coverage", "known_labels", "record", "select_subjects",
           "threads_of_keys", "watermark"]
