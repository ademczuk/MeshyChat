"""Cross-worker presence: which process holds a socket for a peer (divergence 041 S2).

Design `docs/design/skill-frames.md` 4.3, Q8, Q14 and critique F13. The table is
`hub_connections` (sql_meshychat/016). "Is the target online" cannot be answered by the
in-process `ConnectionManager`, which knows only sockets in this process, while an HTTP invoke
is usually served by a different process from the target's socket once more than one worker
runs. So every worker writes a row per socket it holds, and any worker reads the table.

THE CONTRACT, per row:
  * `insert` when a socket is accepted, `delete` when it closes, both by connection_id;
  * `upsert_beat` every `BEAT_INTERVAL_SECS` with every socket the worker holds, as
    (connection_id, peer_id) pairs. NOTE THE ORDER: the registry's `bound_keys()` yields
    (peer_id, connection_id, key). It is a real UPSERT, not an update of rows that exist,
    so an insert that failed once is repaired by the next beat;
  * `is_online(peer)` is a row with `seen_at` inside `ONLINE_WINDOW_SECS` (three beats),
    compared on the DATABASE clock, so two workers never compare two clocks.

A SOCKET WHOSE PEER NO LONGER EXISTS is skipped, not written, and named in a warning; the
write counts only what it wrote. A peer deleted while its socket is open (the revocation
sweep closes such a socket within about 10 s, but only while the LiveBridge that drives it
runs) would otherwise fail the beat's one statement on the foreign key, leave every OTHER
socket on the worker unrefreshed, and after 30 s read all of them offline. A beat whose
pairs are reversed names no existing peer, so it writes nothing and warns about every
socket. A peer deleted in the instant between the statement's check and its write still
fails that one beat on the foreign key; the next beat skips it.

A crashed worker's rows stop being refreshed and read offline within 30 s. `prune` deletes
the ones nobody has seen for PRUNE_AFTER_SECS; every CallBridge beat runs it, so any live
worker clears any dead worker's rows (041 S3). Until then `connected_at` and
`worker_token` say whose they were.

EVERY FUNCTION ENTERS `rls.server_internal` ITSELF, on the connection it is given, which
must be an autocommit meshychat_app connection with no open transaction (`apps.core.dbconn
.connect()`, as `apps/mesh/usage.record` takes one). The table refuses every command outside
that context (F13): a peer-bound, owner-bound or contextless connection cannot write a row
that makes an offline peer look online and strands its calls in `routed`, or delete one and
send an online peer's calls to `queued`. A function that forgot the context would read
"offline" and write nothing, and the reads would not raise. Each call is its own
transaction and none nests in another (a connection with a transaction open is refused), so
S5's online check and the routed/queued write that follows it are two transactions; the
`queued` safety net covers the gap between them.

SERVER_INTERNAL IS NOT "SERVER THREADS ONLY". Request handlers enter it too
(`rls.projection_writer`'s docstring: "A request holds `server_internal` for its reads";
`apps/calls/route.py`). So the RLS alone does not stop server code on a request path from
calling a write here. tests/test_hub_presence.py does: only this module may name the table,
and only the functions its PRESENCE_WRITE_SITES names may call the writes: the CallBridge's
`_insert_row`, `_delete_row`, `_beat` and `_drain_departed` (041 S3). `is_online` is
importable by name anywhere. The bridge's own methods are write surfaces too, so the same
file lets only the consumer hold the bridge, and only for `attach` and `detach` (S3 review F6).

WIRING (041 S3, apps/wsgw/call_notify.py). The consumer's connect calls the bridge's
`attach`, which inserts only once this worker's CallBridge reports LISTENING; its disconnect
calls `detach`, which deletes. The beat runs on the CallBridge thread only while its LISTEN
connection is up, and reads the registry through the event loop, so a worker that cannot
deliver, or whose loop is wedged, stops beating and its rows age out. Step 9 reads
`is_online` from S5; the dashboard's `ws_online` stays as it is until a later slice.

S2'S REVIEW OWED S3 THREE THINGS (F3, F6, I1), and each is done there:
  * THE DELETE WAITS FOR THE BEAT. The beat is a real UPSERT, so a beat whose registry
    snapshot was taken before a socket closed would RE-CREATE that socket's row after
    `delete` removed it. The bridge holds one lock from the beat's snapshot to its write,
    and the delete takes the same lock after the socket is deregistered. For at most 1 s
    (S3 review F1): a beat that holds it longer is stuck on the database, and it deletes the
    departed socket's row itself, after its write.
  * THE PRUNE (`prune`, below), run by every beat.
  * THE TOKEN IS DRAWN WHERE THE WORKER STARTS: the CallBridge draws it when its thread
    starts. `WORKER_TOKEN` below is drawn at import and no longer written by anything; it
    is kept for S2's test of it.
"""
from __future__ import annotations

import logging
import uuid
from typing import Iterable

from apps.core import rls

log = logging.getLogger(__name__)

#: Seconds between two beats of one worker (design 4.3).
BEAT_INTERVAL_SECS = 10

#: A peer is online while one of its rows was seen within this many seconds: three beats.
#: Deliberately shorter than the 120 s `constants.PRESENCE_TTL_SECS`:
#: with 120 s a crashed worker's rows would read online for four times the 30 s default
#: call deadline, and a call routed in that window gets no doorbell (design 4.3).
ONLINE_WINDOW_SECS = 3 * BEAT_INTERVAL_SECS

#: A row not seen for this long is deleted by `prune`. Ten online windows: a row this old
#: has read offline for four and a half minutes, so a prune never changes a routing answer
#: (`prune` refuses any age inside the window); it only stops a crashed worker's rows from
#: accumulating until their peer is deleted.
PRUNE_AFTER_SECS = 10 * ONLINE_WINDOW_SECS

#: A token drawn at import. NOT the token rows are written with: the CallBridge draws its own
#: when its thread starts (S2 review I1), so a server that forks after import gives each
#: child its own. Kept because S2's test of a per-process token reads it.
WORKER_TOKEN: uuid.UUID = uuid.uuid4()

# ON CONFLICT refreshes rather than refuses. The beat reads the registry, so it can write a
# new socket's row before that socket's own insert runs, and the insert must not then fail.
# peer_id and worker_token take the writer's values: the worker holding the socket is the
# authority on both. connected_at keeps the first write's value. Only sockets whose peer
# still exists are written (the module docstring says why); RETURNING names the written ones.
_UPSERT = (
    "INSERT INTO hub_connections (connection_id, peer_id, worker_token) "
    "SELECT s.connection_id, s.peer_id, s.worker_token "
    "FROM unnest(%s::uuid[], %s::uuid[], %s::uuid[]) AS s(connection_id, peer_id, worker_token) "
    "WHERE EXISTS (SELECT 1 FROM peers p WHERE p.id = s.peer_id) "
    "ON CONFLICT (connection_id) DO UPDATE SET "
    "peer_id = EXCLUDED.peer_id, worker_token = EXCLUDED.worker_token, seen_at = now() "
    "RETURNING connection_id"
)

#: How many unwritten connection ids one warning names.
_WARN_IDS = 5

def online_condition(peer_sql: str) -> str:
    """The online rule as a SQL condition, for a statement that must apply it ITSELF, in the same
    statement as the rest of its guard (a caller may not name `hub_connections`: this store is the
    only module that does, tests/test_hub_presence.py). `peer_sql` is the SQL expression of the
    peer id; the one placeholder is the window, `ONLINE_WINDOW_SECS` seconds. `is_online` runs
    exactly this, so the two cannot disagree about who is online."""
    return ("EXISTS (SELECT 1 FROM hub_connections WHERE peer_id = " + peer_sql +
            " AND seen_at > now() - make_interval(secs => %s))")


_ONLINE = "SELECT " + online_condition("%s")

_PRUNE = "DELETE FROM hub_connections WHERE seen_at < now() - make_interval(secs => %s)"


def _uuid(value, what: str) -> uuid.UUID:
    """A UUID, or its canonical string form; anything else is refused before any SQL."""
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str):
        try:
            return uuid.UUID(value)
        except ValueError:
            raise ValueError(f"{what} is not a UUID") from None
    raise TypeError(f"{what} must be a uuid.UUID or its string form, not {type(value).__name__}")


def _upsert(conn, rows: list[tuple[uuid.UUID, uuid.UUID]], worker_token: uuid.UUID) -> int:
    """Write `rows`; returns how many were written. A row whose peer no longer exists is
    not written, and a warning names it."""
    if not rows:
        return 0
    with rls.server_internal(conn):
        with conn.cursor() as cur:
            cur.execute(_UPSERT, ([c for c, _ in rows], [p for _, p in rows],
                                  [worker_token] * len(rows)))
            written = {r[0] for r in cur.fetchall()}
    skipped = [str(c) for c, _ in rows if c not in written]
    if skipped:
        log.warning("hub_connections: %d socket(s) not written because their peer no longer "
                    "exists (or the pair is reversed): %s%s", len(skipped),
                    ", ".join(skipped[:_WARN_IDS]), " ..." if len(skipped) > _WARN_IDS else "")
    return len(written)


def insert(conn, connection_id, peer_id, worker_token) -> bool:
    """Record that this worker holds socket `connection_id` for `peer_id`. False when no row
    was written because the peer no longer exists."""
    row = (_uuid(connection_id, "connection_id"), _uuid(peer_id, "peer_id"))
    return _upsert(conn, [row], _uuid(worker_token, "worker_token")) == 1


def delete(conn, connection_id) -> bool:
    """Forget socket `connection_id`. True when a row was deleted."""
    cid = _uuid(connection_id, "connection_id")
    with rls.server_internal(conn):
        with conn.cursor() as cur:
            cur.execute("DELETE FROM hub_connections WHERE connection_id = %s", (cid,))
            return cur.rowcount == 1


def upsert_beat(conn, rows: Iterable[tuple], worker_token) -> int:
    """One beat: write or refresh a row for every (connection_id, peer_id) this worker
    holds, in one statement. Every pair is checked before any is written. Returns the rows
    written or refreshed; fewer than the distinct sockets given means some peer no longer
    exists (a warning names the sockets)."""
    checked = [(_uuid(c, "connection_id"), _uuid(p, "peer_id")) for c, p in rows]
    # One statement cannot upsert one key twice (Postgres refuses "ON CONFLICT DO UPDATE
    # command cannot affect row a second time"), so a repeated pair is written once. A
    # connection_id names ONE socket bound to one peer: a beat pairing it with two peers is
    # a caller bug, and picking either would put a routing input on a guess, so it is refused
    # before anything is written.
    unique: dict[uuid.UUID, uuid.UUID] = {}
    for cid, pid in checked:
        if unique.setdefault(cid, pid) != pid:
            raise ValueError(f"connection_id {cid} is paired with two peers in one beat")
    return _upsert(conn, list(unique.items()), _uuid(worker_token, "worker_token"))


def is_online(conn, peer_id) -> bool:
    """Whether ANY worker holds a socket for `peer_id` that it beat for within the window."""
    pid = _uuid(peer_id, "peer_id")
    with rls.server_internal(conn):
        with conn.cursor() as cur:
            cur.execute(_ONLINE, (pid, ONLINE_WINDOW_SECS))
            return bool(cur.fetchone()[0])


def prune(conn, older_than_secs: float = PRUNE_AFTER_SECS) -> int:
    """Delete every row, whichever worker wrote it, not seen for `older_than_secs` on the
    database clock. Returns how many. An age inside ONLINE_WINDOW_SECS is refused before
    any SQL: it would delete rows that still read online."""
    if not older_than_secs >= ONLINE_WINDOW_SECS:
        raise ValueError(f"a prune of rows younger than the {ONLINE_WINDOW_SECS} s online "
                         f"window would delete online rows: {older_than_secs!r}")
    with rls.server_internal(conn):
        with conn.cursor() as cur:
            cur.execute(_PRUNE, (older_than_secs,))
            return cur.rowcount
