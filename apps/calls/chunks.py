"""Stream chunks: the bounds, the replay read and the retention trim (divergence 041 S7).

Design: docs/design/skill-frames.md, sections 4.7 (subscribe replay, stored chunk content),
4.6 (the chunk arm, the recovery sweep's part b), 5.2, critique F9, and the approved Q15. The table and
`trim_chunks_for_call` are sql/008, not sql_meshychat.

THE REPLAY READ (`replay`) IS NOT `reads.list_chunks`. `reads.list_chunks` uses
`sequence > since`, which never returns a chunk stored at sequence 0 when called with since=0
(the wire default). `report_chunk` accepts sequence 0 and a valid target may send it (the SDK
leaves numbering to the target). `GET /v1/calls/{id}/chunks` keeps since=0 as its default (Q15;
since=-1 includes 0). `replay` reads `sequence >= 0` past a high-water mark, and at most the NEWEST
`CHUNK_KEEP_LAST` of them, returned oldest first: the same bound as the trim at terminal, so a
replay is at most 200 rows for a running call as for a finished one (5.4). A keyed range scan on
`UNIQUE (call_id, sequence)`, backwards, then re-sorted: at most 200 rows either way.

THE TRIM (`trim`) runs INSIDE each terminal writer's own transaction (5.2), not after the commit
in a background task whose failure would only be logged. A terminal edge never commits without
its trim; the deadline janitor is the one exception, on a trim that FAILS (apps/calls/janitor.py,
the S7 review's H1).

THE CAP (`MAX_CHUNKS_PER_CALL`, the S7 review's H1). 5.2 bounds each chunk's size, not how many a
running call holds. The trim deletes all but 200 of them
inside a transaction that runs under a statement timeout (the socket's 5 s, the janitor job's
10 s): the review measured 400k rows at 5.9 to 10.1 s on HQD, a flood one peer reaches in about
10 minutes, and a trim past its timeout took its terminal edge with it (every timeout of the
janitor's batch, and the target's result, error and the caller's cancel). So the chunk statement
(apps/wsgw/target_frames.chunk) stores a chunk only while the call holds fewer than the cap, and
a terminal's trim deletes at most `MAX_CHUNKS_PER_CALL - CHUNK_KEEP_LAST` rows: 9800, measured at
97 to 112 ms. The refusal, SKILL_CHUNK_BAD, is told only to the call's running target. The count
is read in the chunk statement's snapshot, so concurrent inserts for one call
(more than one socket of its target) can pass it together: at most one row per such statement.

skill_call_chunks has row-level security DISABLED (measured, apps/calls/reads.py), so nothing
here binds a context: the callers bind whatever their statement needs, and authorize by the
PARENT call before they read.
"""
from __future__ import annotations

#: The chunks a terminal call keeps, and (design 4.7, F9) the most a replay sends.
CHUNK_KEEP_LAST = 200

#: Inline content, text or json. The bound is on the bytes STORED, the verified payload's
#: RFC 8785 form (design 4.7).
MAX_CHUNK_CONTENT_BYTES = 512 * 1024

#: The most chunks one call holds (the S7 review's H1). Read at each chunk statement, so a test
#: can lower it.
MAX_CHUNKS_PER_CALL = 10_000

#: What a chunk frame is rebuilt from (apps/wsgw/caller_frames.chunk_frame).
REPLAY_COLUMNS = ("sequence", "chunk_type", "content", "chunk_signature", "chunk_signing_kid")


def _rows(cur) -> list[dict]:
    return [dict(zip(REPLAY_COLUMNS, r)) for r in cur.fetchall()]


def replay(conn, call_id: str, *, after: int = -1, limit: int = CHUNK_KEEP_LAST) -> list[dict]:
    """The call's chunks with `sequence >= 0` and `> after`, at most the newest `limit`, oldest
    first. `after=-1` is a subscriber's whole replay; a registration's high-water mark is the
    recovery sweep's (design 4.6, part b)."""
    cols = ", ".join(REPLAY_COLUMNS)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {cols} FROM ("
            f"  SELECT {cols} FROM skill_call_chunks"
            "    WHERE call_id = %s AND sequence >= 0 AND sequence > %s"
            "    ORDER BY sequence DESC LIMIT %s"
            ") AS newest ORDER BY sequence ASC",
            (call_id, after, limit))
        return _rows(cur)


def read_one(conn, call_id: str, sequence: int) -> dict | None:
    """One chunk by its UNIQUE (call_id, sequence): the chunk arm's read (design 4.6). None when
    there is no such row (a forged pointer, or a row the trim at terminal already removed)."""
    cols = ", ".join(REPLAY_COLUMNS)
    with conn.cursor() as cur:
        cur.execute(f"SELECT {cols} FROM skill_call_chunks WHERE call_id = %s AND sequence = %s",
                    (call_id, sequence))
        rows = _rows(cur)
    return rows[0] if rows else None


def trim(cur, call_id: str) -> int:
    """`trim_chunks_for_call(call_id, 200)` (sql/008) on the caller's cursor, so inside its
    transaction: the newest CHUNK_KEEP_LAST chunks of the call stay. Returns the rows deleted."""
    cur.execute("SELECT trim_chunks_for_call(%s, %s)", (call_id, CHUNK_KEEP_LAST))
    return cur.fetchone()[0]


__all__ = ["CHUNK_KEEP_LAST", "MAX_CHUNKS_PER_CALL", "MAX_CHUNK_CONTENT_BYTES",
           "REPLAY_COLUMNS", "read_one", "replay", "trim"]
