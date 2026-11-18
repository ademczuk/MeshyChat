"""shared_memory rows WITHOUT signed bytes, seeded the way history reached this database.

Divergence 051 (sql_meshychat/025, B02 phase A2). From A2 on, no session can insert a row
whose `signed_bytes` is NULL. Production still holds every row written before 014 and every
row written by the earlier writer stack after it, all without bytes, and keeps them: A2 refuses new ones
and rewrites nothing. So a test of the verdict job, of a reader's `status_basis` for a legacy
row, or of a rollback still needs rows without bytes, and it gets them the way production got
them: written while the refusal was not in force. A full restore loads them the same way (it
creates the triggers after the data; design section 2.4).

A test that models a row THIS build writes does not come here: it builds the row from a
payload with the bytes (tests/fixtures/signed_rows.py), which A2 admits.

The second kind of history is a LATER VERSION OF A MAIL KEY (divergence 059, sql_meshychat/029). Since 029 the database
refuses one except through the edit path: the mark, the owner of version 1, only the body changed. The 53 later versions
production holds were written before that rule (8 of them with a changed `to` or `cc`), and a test of a projection, a read or a
walk over a SUPERSEDED version needs a chain as it stands there. `seeding_mail_history(conn)` is `seeding_history` for 011's
single-writer trigger, with the same guards; `insert_seed` is `signed_rows.insert` for the helpers that seed a world, which
seeds a later version of a mail key that way and anything else as one plain INSERT. A test of what the rule REFUSES or of what
the edit path ADMITS does not come here: it inserts through `signed_rows.insert` and meets the trigger.

`seeding_history(conn)` disables A2's trigger, and only that one, in ONE transaction on the
owner's connection, and enables it again before that transaction commits. ALTER TABLE takes
ACCESS EXCLUSIVE on shared_memory, so no other session can insert, or see the trigger
disabled, while it is off; 014's agree-trigger stays on throughout, so a marked meshychat_app
session is refused a NULL row as A1 refuses it. Nothing here can reach production: it needs
the table's owner, who could drop the trigger anyway (025's header says so).

Two guards, both because the lock is ACCESS EXCLUSIVE:
  * the connection must be IDLE (no open transaction), so the seeding commits, and releases
    the lock, when the block ends. Inside a caller's open transaction the lock would be held
    until the caller commits, and a test that then calls the app, whose session waits on the
    lock, would hang on its own idle connection with no deadlock for Postgres to detect;
  * `lock_timeout` 10 s: a session that holds a lock on shared_memory (an open transaction
    that read it) makes the seeding fail, named, instead of waiting for ever.
"""
from __future__ import annotations

import contextlib

from psycopg import pq

TRIGGER = "shared_memory_signed_bytes_required_trg"
MAIL_TRIGGER = "shared_memory_mesh_single_writer_trg"       # 011, replaced by 029


class NotIdle(AssertionError):
    """The connection has an open transaction; commit or roll it back first."""


@contextlib.contextmanager
def seeding_history(conn, trigger: str = TRIGGER):
    """Insert rows without bytes inside the block, on `conn` (the owner role) -- or, with
    `trigger`, past that one trigger. The block is one transaction; it commits when the block
    ends and rolls back if it raises."""
    status = conn.info.transaction_status
    if status != pq.TransactionStatus.IDLE:
        raise NotIdle(f"seeding_history needs an idle connection, this one is {status.name}: "
                      "commit or roll back first, or the ACCESS EXCLUSIVE lock outlives the "
                      "block (tests/fixtures/history_rows.py)")
    with conn.transaction():
        conn.execute("SET LOCAL lock_timeout = '10s'")
        conn.execute(f"ALTER TABLE shared_memory DISABLE TRIGGER {trigger}")
        yield conn
        # A mail row queues DEFERRED constraint-trigger events (its commit-order position), and ALTER TABLE refuses a table with
        # events pending: fire them now, inside this same transaction, where they would have fired at its commit.
        conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
        conn.execute(f"ALTER TABLE shared_memory ENABLE TRIGGER {trigger}")


def insert(conn, cols: dict):
    """One row without bytes, in its own seeding transaction. Returns its id."""
    from tests.fixtures import signed_rows as sr
    with seeding_history(conn):
        return sr.insert(conn, cols)


def seeding_mail_history(conn):
    """Later versions of mail keys inside the block, on `conn` (the owner role): 011's single-writer trigger is
    off for this one transaction and on again before it commits (divergence 059; the module docstring)."""
    return seeding_history(conn, MAIL_TRIGGER)


def insert_seed(conn, cols: dict):
    """`signed_rows.insert` for a helper that seeds a world. A later version of a mail key is seeded as history
    (`seeding_mail_history`, its own transaction on an idle connection); anything else is one plain INSERT in the
    caller's transaction, as before. Returns the id."""
    from tests.fixtures import signed_rows as sr
    if (cols.get("namespace") == "shared/mesh-chat" and str(cols.get("key", "")).startswith("msg/")
            and int(cols.get("version") or 1) > 1):
        with seeding_mail_history(conn):
            return sr.insert(conn, cols)
    return sr.insert(conn, cols)
