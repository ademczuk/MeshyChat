"""What a summary states WITHOUT the model: the declared facts (design 4.4).

Divergence 037. Every figure here comes from `mesh_edges` and `shared_memory`, never
from the model, and is labelled `tier: "declared"`. It is computed in the transaction that
reads the rows, for THIS caller, on every request, and never served from the cache
(critique C3: a cache that stored `owed_by_you` would hand one reader's owed acks to the
next reader of the same thread).

OBLIGATIONS NEVER COME FROM THE MODEL. A summary that leaves out an ask carried by an
`owes_ack` edge cannot hide an unresolved ask: it is in `facts.owed_by_you` whatever the
model wrote. Explicitly closed asks are settled and omitted; directly answered asks remain,
because a reply is activity rather than proof of completion (apps/mesh/graph.py).
The owed acks are asked FOR THE SCOPE (`graph.owed_acks(thread_id=...)` or `keys=...`),
never as the global answer filtered afterwards: that query stops at 100 rows across every
thread, newest first, so a caller owing more than 100 would lose an in-scope ask to the
limit (critique C9).

VISIBILITY. Computed over the rows the caller may SEE: today every head row of
shared/mesh-chat. The embed_policy gate is not a visibility rule (027: memory.read returns
a forbidden row's body to a caller with read), so a forbidden row still counts here while
its body stays out of every prompt. If an object-level rule lands (design B07), it hides a
row here too, as 2.3 says.
"""
from __future__ import annotations

from psycopg.rows import dict_row

from apps.memoryskills.validation import MESH_CHAT_NAMESPACE
from apps.mesh import graph

OWED_LIMIT = 100
AUTHORS_LIMIT = 50


def _owed(items) -> list[dict]:
    return [{"key": o.hit.key, "version": o.hit.version, "thread_id": o.thread_id,
             "recorded_at": o.hit.recorded_at.isoformat(),
             "answered_in_thread": o.answered_in_thread} for o in items]


def for_thread(conn, thread_id: str, labels: list[str]) -> dict:
    """The thread's declared facts for a caller answering for `labels`."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT count(*) AS n, min(sm.created_at) AS first_at, max(sm.created_at) AS last_at "
            "FROM mesh_edges e JOIN shared_memory sm ON sm.id = e.src_id AND sm.is_head "
            "WHERE e.edge_type = 'in_thread' AND e.dst = %s AND sm.namespace = %s "
            "  AND sm.key LIKE 'msg/%%'", (thread_id, MESH_CHAT_NAMESPACE))
        agg = cur.fetchone()
        cur.execute(
            "SELECT sm.author_label AS label, count(*) AS messages "
            "FROM mesh_edges e JOIN shared_memory sm ON sm.id = e.src_id AND sm.is_head "
            "WHERE e.edge_type = 'in_thread' AND e.dst = %s AND sm.namespace = %s "
            "  AND sm.key LIKE 'msg/%%' "
            "GROUP BY sm.author_label ORDER BY count(*) DESC, sm.author_label LIMIT %s",
            (thread_id, MESH_CHAT_NAMESPACE, AUTHORS_LIMIT + 1))
        authors = cur.fetchall()
    owed = graph.owed_acks(conn, labels, limit=OWED_LIMIT + 1, thread_id=thread_id)
    return {
        "tier": "declared",
        "messages": int(agg["n"]),
        "authors": [{"label": a["label"], "messages": int(a["messages"])}
                    for a in authors[:AUTHORS_LIMIT]],
        "authors_truncated": len(authors) > AUTHORS_LIMIT,
        "first_at": agg["first_at"].isoformat() if agg["first_at"] else None,
        "last_at": agg["last_at"].isoformat() if agg["last_at"] else None,
        "owed_by_you": _owed(owed[:OWED_LIMIT]),
        "owed_truncated": len(owed) > OWED_LIMIT,
    }


def for_backlog(conn, scanned: list, labels: list[str]) -> dict:
    """The backlog window's declared facts: per thread and per role, over every addressed
    row the request read (`scanned`, rules aside), and the caller's owed acks among them."""
    threads: dict = {}
    roles = {"to": 0, "cc": 0, "broadcast": 0}
    first = last = None
    for row in scanned:
        threads[row.thread_id] = threads.get(row.thread_id, 0) + 1
        if row.role in roles:
            roles[row.role] += 1
        first = row.created_at if first is None or row.created_at < first else first
        last = row.created_at if last is None or row.created_at > last else last
    keys = [row.key for row in scanned]
    owed = graph.owed_acks(conn, labels, limit=OWED_LIMIT + 1, keys=keys) if keys else ()
    return {
        "tier": "declared",
        "messages": len(scanned),
        "roles": roles,
        "threads": [{"thread_id": t, "messages": n}
                    for t, n in sorted(threads.items(), key=lambda kv: (-kv[1], kv[0] or ""))],
        "first_at": first.isoformat() if first else None,
        "last_at": last.isoformat() if last else None,
        "owed_by_you": _owed(owed[:OWED_LIMIT]),
        "owed_truncated": len(owed) > OWED_LIMIT,
    }


__all__ = ["for_backlog", "for_thread"]
