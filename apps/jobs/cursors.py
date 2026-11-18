"""A background job's cursor: one row of `background_job_cursors` (sql_meshychat/023), read
and saved by the job bodies that walk a table in bounded ticks.

`read` and `save` replace the private pairs apps/calls/frame_audit.py, frame_retention.py,
chain_head.py and heartbeat.py each hand-rolled over the table: the same SELECT and the same
INSERT ... ON CONFLICT (cursor_name) DO UPDATE in four spellings, each naming the columns its
job used. `save` writes every column, as chain_head's spelling did. The narrower spellings
left the columns they omitted at their defaults (a frame-audit cursor's `position_key` is
NULL, a retention cursor's `position` is 0, the heartbeat cursor's `position` and `laps` are
0), and no cursor name has two writers, so each caller's rows are the rows it wrote before,
column for column (tests/test_job_cursors.py holds the three narrow spellings against it).

Both run on the caller's connection, in whatever transaction it holds, and open none of their
own: the frame audit reads and saves inside its REPEATABLE READ tick transaction, so its two
cursors move with what the tick read or not at all; the chain-head reconcile and frame
retention call between per-chain transactions on an autocommit connection; the heartbeat
inside its server_internal transaction. The table carries no row-level security (023: the
runner's own, like background_job_leases), so no context is needed.
"""
from __future__ import annotations

from psycopg.rows import dict_row


def read(conn, name: str) -> dict | None:
    """The cursor's row, `{position, position_key, laps}`, or None when the job has never
    saved it (its first tick; each job decides what that means)."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT position, position_key, laps FROM background_job_cursors "
                    "WHERE cursor_name = %s", (name,))
        return cur.fetchone()


def read_or_start(conn, name: str) -> dict:
    """`read`, or the row a cursor starts from (position 0, no key, no laps) when none is
    saved: where a rolling pass begins its first lap."""
    return read(conn, name) or {"position": 0, "position_key": None, "laps": 0}


def save(conn, name: str, *, position: int = 0, key: str | None = None, laps: int = 0) -> None:
    """Write the cursor whole: insert it, or replace every column and stamp `updated_at`."""
    conn.execute("INSERT INTO background_job_cursors (cursor_name, position, position_key, "
                 "laps) VALUES (%s, %s, %s, %s) ON CONFLICT (cursor_name) DO UPDATE SET "
                 "position = EXCLUDED.position, position_key = EXCLUDED.position_key, "
                 "laps = EXCLUDED.laps, updated_at = now()",
                 (name, int(position), key, int(laps)))


__all__ = ["read", "read_or_start", "save"]
