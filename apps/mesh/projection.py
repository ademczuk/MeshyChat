"""Write the projection, and prove it can be thrown away.

THREE DISCIPLINES, from the event-sourcing literature, each made a function rather than a
sentence:

  1. THE PROJECTION IS DISCARDABLE. `rebuild()` is TRUNCATE plus a full replay of the log,
     in one transaction. "If you cannot delete this projection and rebuild it with zero
     data loss, you are not doing event sourcing correctly." tests/test_mesh_projection.py
     rebuilds twice and asserts the edge sets are identical, so the property is measured.
  2. NO REQUEST PATH WRITES IT. This module is the only writer. Both tables are readable
     under `app.server_internal = '1'` and WRITABLE only when the job-only projection-writer
     flag is also set (sql_meshychat/008, divergence 022), which `rls.projection_writer()` sets
     and only `catch_up` and `rebuild` below enter. A request holds server_internal for
     its reads, so before 008 this discipline was a convention; the database now refuses
     a request's INSERT. A second writer would be a second truth, and the dual-write
     problem event sourcing exists to remove.
  3. REBUILD IS EXERCISED, NOT THEORETICAL. The catch-up runs as a registered background
     job (apps/jobs/registry.py) and `python -m apps.mesh rebuild` is a first-class verb.
     A rebuild nobody runs is a rebuild nobody can claim works.

SCALE, so the choice of full replay is a measurement and not a hope. The namespace holds
23,456 rows (2026-09-21). The literature's failure mode, the "replay storm", is cited at
2 million events across 4 projections. This is two to three orders of magnitude below
that, so full sequential replay on demand is the correct default and partitioned rebuild
is deliberately not built.

CATCH-UP IS A KEYSET WALK IN COMMIT ORDER (STATUS D19, sql_meshychat/023). The walk key is
`mesh_commit_order.seq`, drawn when the row's transaction COMMITS, under a lock that makes
seq order the order in which rows became visible. It used to be (created_at, id), and
`created_at` is the writer's transaction START: a transaction that started first and
committed last wrote a row below a watermark the walk had already passed, and that row was
never projected. `watermark_seq` is the walk's position. `watermark_at` / `watermark_id` are
still written, as the GREATEST (created_at, id) walked, because readers compare rows with
them by that key (apps/meshaudit/store.py, apps/meshsem/related.py, apps/meshsum/window.py);
they never move backwards when a late-committing row with an older created_at is walked.
THEY NO LONGER IMPLY COVERAGE (review finding 7, 2026-09-26): they mean "the greatest key
walked", not "every row at or below it is walked". A row that commits late with a
created_at below them, and that the walk has not reached yet, sits below them unwalked until
the walk reaches it. A reader that treats the key as coverage is wrong about that row for
that window; related.py's walk, still keyed on (created_at, id) and bounded by the lesser
of this key and the embed walk's, passes it for good (skipped_no_vector). Before 023 the
walk itself lost such a row for good, so no reader is worse off; related.py's walk is not
in this batch.

TWO PASSES NEVER SET THE WATERMARK BACK (STATUS D21). The job lease keeps two JOB ticks
apart, and nothing else: `python -m apps.mesh catch-up` and `rebuild` take no lease, and a
lease is taken over from a holder whose `leased_until` passed while it may still be running
(the tick's statements are each bounded by statement_timeout, the tick as a whole is not).
The watermark is read without a lock, so two passes can read the same position; before
D21 the one that wrote second wrote its own position outright, and a pass that read fewer
rows set the walk BACK and counted rows the other had already counted. `_advance` is now a
compare-and-set in SQL: the position is written only WHERE the row still holds the position
this pass read, and every row a pass walks has a seq above that position, so the saved seq
only ever moves forward. A pass that loses raises `ConcurrentWalk` and its transaction
(edges and counters) rolls back; the next pass starts from wherever the winner left it.
The embed and related walks already compared their read position under FOR UPDATE before
writing (apps/meshsem/embedjob.py, apps/meshsem/related.py); this walk was the one that
did not.

WHAT CATCH-UP CANNOT SEE, stated because it is the projection's one blind spot. The only
legal mutation on shared_memory is `is_head` flipping TRUE to FALSE, and it is an UPDATE:
no new row, so no watermark movement. The projection therefore does NOT store head-ness.
`apps/mesh/graph.py` joins back to `shared_memory.is_head` at read time, which is always
current and costs one join at 0.3 QPS. A projection that copied the flag would go stale on
every version bump and nothing would tell it.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import uuid

from psycopg.rows import dict_row

from apps.core import rls
from apps.memoryskills.validation import MESH_CHAT_NAMESPACE

from . import normalize

#: The one row in mesh_edges_projection.
PROJECTION_NAME = "mesh_edges"

#: Rows read per catch-up tick. Also the job's declared cost bound (B5): the job returns
#: exactly this when the log had at least this many unread rows, and the runner reports
#: `more_work_pending` so the next tick follows immediately rather than waiting an interval.
DEFAULT_BATCH = 2000

_INSERT = (
    "INSERT INTO mesh_edges "
    "(src_id, src_key, src_version, edge_type, dst, src_field, recorded_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (src_id, edge_type, dst) DO NOTHING"
)

#: The walk, in commit order (D19). `seq` is the position; the join is the row's PRIMARY
#: KEY. Only shared/mesh-chat rows are stamped (sql_meshychat/023's trigger), so the
#: namespace is fixed: `catch_up` and `rebuild` refuse any other.
#:
#: EVERY KIND of position is read, on purpose (divergence 053): a row is met again at its `redaction` position, as the
#: tombstone it is now, and re-projected from that. The insert below is ON CONFLICT DO NOTHING, so the routing edges are
#: what they were, and a row whose thread id was blanked gets its `in_thread` edge to `redacted/<record id>` there, after
#: redact.delete_derived removed the edge to the old thread id.
_SELECT_ROWS = (
    "SELECT o.seq, sm.id, sm.key, sm.version, sm.value, sm.author_label, sm.embed_policy, sm.created_at, sm.is_head "
    "FROM mesh_commit_order o JOIN shared_memory sm ON sm.id = o.memory_id "
    "WHERE o.seq > %(after_seq)s AND sm.namespace = %(ns)s AND sm.key LIKE 'msg/%%' "
    "ORDER BY o.seq ASC "
    "LIMIT %(limit)s "
    # FOR SHARE: mentions are derived from the BODY, and a redaction (apps/memoryskills/redact.py, which
    # locks the row FOR UPDATE) deletes them. Without the lock a pass could read a body, a redaction could
    # tombstone the row and delete its mentions and commit, and the pass could then insert mentions from
    # the body it read, so references came back after a redaction (codex on #89). With it the redaction
    # waits for this pass and deletes what it inserted, or this pass waits and reads the tombstone.
    "FOR SHARE OF sm"
)


#: THE LOCK BETWEEN A PASS AND A BLANKING REDACTION (divergence 073; Codex on #106, via kxxx). An inherited edge copies
#: the thread a parent's edge names, read through mesh_edges with the parent's row unlocked, and a parent in a chain
#: inherited its own in an earlier pass. So a redaction with blank_thread_id could commit between a pass's read and its
#: insert, and the reply would keep a copy of the withdrawn thread for good (it has an edge, so it is never a candidate
#: again). A row lock cannot close that: for a chain the redaction locks the grandparent's rows while the pass reads the
#: parent's edge, and a pass that waits on a locked row rechecks it against an edge row it already read. So every pass
#: holds this lock SHARED for its transaction, and a blanking redaction takes it EXCLUSIVE, each BEFORE any row lock, so
#: neither ever waits for the other while holding a row the other wants. Passes still run beside one another; a blanking
#: redaction waits for the pass in flight (a catch-up tick, or a whole rebuild), and the next pass waits for it.
#: Two-integer form ('medg', 73), a key space no one-integer lock shares, as apps/meshsum/admission.py does.
BLANKING_LOCK = (0x6D656467, 73)


def hold_against_blanking(cur) -> None:
    """A pass's side of BLANKING_LOCK: shared, until the pass's transaction ends. The pass's first statement."""
    cur.execute("SELECT pg_advisory_xact_lock_shared(%s, %s)", BLANKING_LOCK)


def exclude_passes(cur) -> None:
    """A blanking redaction's side of BLANKING_LOCK: exclusive, until its transaction ends. Taken before its row locks."""
    cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", BLANKING_LOCK)


#: A REPLY THAT DECLARES NO THREAD INHERITS ITS PARENT'S (divergence 073). Here and not in normalize, which sees
#: one row: the thread is the parent's, so it is a join. ONE edge, src_field 'inherited', because graph.owed_acks
#: LEFT JOINs in_thread and a second edge would be a second owed row.
#:
#: WHO INHERITS is decided from the reply's VALUE (normalize.thread_of finds no thread), and only for a message with
#: no in_thread edge yet. Never from the missing edge alone: a redaction with blank_thread_id deletes the row's edge
#: at once, and its tombstone's own edge to redacted/<id> is written only when the walk reaches the redaction's
#: position, so between the two an edge-only test would hand the blanked thread back (memory-bxxxxx on the design).
#: The candidate rows are read FOR SHARE, as _SELECT_ROWS reads the batch: a redaction waits for this pass, or this
#: pass reads the tombstone.
#:
#: WHAT IS INHERITED is the in_thread of the parent KEY's lowest version that has one not under `redacted/`: it can be
#: the parent's own inherited edge, so a chain of threadless replies inherits down the chain. Lowest version, not "the
#: version the reply answered", because when the parent commits after the reply that version does not exist, and a
#: reply keeps the parent's first thread if the parent is later edited into another one. Candidates are the replies in
#: the batch and the replies to a key in the batch, so a parent projected AFTER its reply still gives the reply its
#: edge, and a rebuild in commit order gives the edge an incremental walk gave. recorded_at is the reply's own (the
#: edge row's), never the pass's, so an old reply never reads as fresh activity.
_INHERIT_CANDIDATES = (
    "SELECT r.src_id, r.src_key, r.src_version, r.recorded_at, pt.dst, s.value "
    "FROM mesh_edges r "
    "JOIN shared_memory s ON s.id = r.src_id "
    "CROSS JOIN LATERAL (SELECT t.dst FROM mesh_edges t "
    "                     WHERE t.src_key = r.dst AND t.edge_type = 'in_thread' "
    "                       AND t.dst NOT LIKE 'redacted/%%' "
    "                     ORDER BY t.src_version, t.dst LIMIT 1) pt "
    "WHERE r.edge_type = 'replies_to' "
    "  AND (r.src_id = ANY(%(ids)s::uuid[]) OR r.dst = ANY(%(keys)s::text[])) "
    "  AND NOT EXISTS (SELECT 1 FROM mesh_edges x "
    "                   WHERE x.src_id = r.src_id AND x.edge_type = 'in_thread') "
    "ORDER BY r.src_id "
    "FOR SHARE OF s"
)

#: A row whose own in_thread edge the pass just wrote keeps that one only (belt and braces beside the value test).
_DROP_SHADOWED_INHERITED = (
    "DELETE FROM mesh_edges i "
    "WHERE i.src_id = ANY(%(ids)s::uuid[]) AND i.edge_type = 'in_thread' AND i.src_field = 'inherited' "
    "  AND EXISTS (SELECT 1 FROM mesh_edges o WHERE o.src_id = i.src_id AND o.edge_type = 'in_thread' "
    "              AND o.src_field <> 'inherited')"
)


class ConcurrentWalk(RuntimeError):
    """The watermark moved between this pass's read and its write: another pass ran (the
    CLI beside the job, or a job whose lease was taken over). Nothing this pass did is
    committed; the next pass starts from wherever the other one left it (STATUS D21)."""


def _require_mesh_chat(namespace: str) -> None:
    if namespace != MESH_CHAT_NAMESPACE:
        raise ValueError(f"the commit order is kept for {MESH_CHAT_NAMESPACE!r} only; "
                         f"{namespace!r} has none to walk (sql_meshychat/023)")


def greatest_key(current: tuple | None, rows: list[dict]) -> tuple | None:
    """The greatest (created_at, id) among `current` and `rows`: the reader-facing watermark,
    which only ever moves forward. uuid.UUID orders by its 128-bit value, which is the
    database's uuid order (bytewise), so the comparison agrees with Postgres'."""
    best = current if current and current[0] is not None else None
    for r in rows:
        key = (r["created_at"], uuid.UUID(str(r["id"])))
        if best is None or key > (best[0], uuid.UUID(str(best[1]))):
            best = key
    return best


@dataclasses.dataclass(frozen=True)
class Report:
    """What one projection pass did. Every count is from this pass only."""

    rows_read: int
    edges_written: int
    #: reason -> count of values present but unusable, summed over the pass.
    skipped: dict[str, int]
    #: One line per row that carried two disagreeing spellings of one relation.
    conflicts: tuple[str, ...]
    watermark_at: _dt.datetime | None
    watermark_id: str | None
    #: True when `rows_read` hit the batch bound, so the log may hold more.
    more: bool


#: What `backfill` can replay: edge kinds (034, 060) and the inherited thread (073).
BACKFILL_TYPES = ("closes", "declines", "mentions", "in_thread_inherited")


@dataclasses.dataclass(frozen=True)
class BackfillReport:
    """Rows read and new edges written by a non-destructive targeted replay."""
    rows_read: int
    edges_written: int


def _ensure_state_row(cur) -> None:
    cur.execute(
        "INSERT INTO mesh_edges_projection (projection) VALUES (%s) "
        "ON CONFLICT (projection) DO NOTHING",
        (PROJECTION_NAME,),
    )


def _read_watermark(cur) -> tuple[int, _dt.datetime | None, uuid.UUID | None]:
    """(watermark_seq, watermark_at, watermark_id); seq 0 when the walk has not started."""
    cur.execute("SELECT watermark_seq, watermark_at, watermark_id FROM mesh_edges_projection "
                "WHERE projection = %s", (PROJECTION_NAME,))
    # The callers open this cursor with row_factory=dict_row, so the row is a dict. The
    # first version indexed `row[0]` and raised KeyError on every catch-up while every
    # rebuild passed, because rebuild never reads the watermark. Named access cannot
    # silently agree with the wrong column either.
    row = cur.fetchone()
    if row is None:
        return 0, None, None
    return (row["watermark_seq"] or 0), row["watermark_at"], row["watermark_id"]


def _project(cur, rows: list[dict]) -> tuple[int, dict[str, int], list[str]]:
    """Insert the edges for `rows`. Returns (edges written, skipped, conflicts)."""
    # Mentions are body-derived content, so they use the same deployment gate as vectors and
    # lexical terms. Normalization remains pure; only this projection knows policy.
    from apps.meshsem import store

    gate_open = store.forbidden_rows_open()
    written = 0
    skipped: dict[str, int] = {}
    conflicts: list[str] = []
    for r in rows:
        # Mentions only from a head: a version already retired when the pass reads it (an edit before catch-up,
        # or any rebuild after an edit) had its mentions deleted by the edit, and its routing edges are all a
        # retired version keeps (codex on #93). FOR SHARE keeps is_head as read until this pass commits.
        re = normalize.project_row(r, mentions_allowed=(gate_open or r.get("embed_policy") == "allowed")
                                   and r.get("is_head") is not False)
        for k, n in re.projected.skipped.items():
            skipped[k] = skipped.get(k, 0) + n
        for c in re.projected.conflicts:
            conflicts.append(f"{re.src_key} v{re.src_version}: {c}")
        tuples = re.rows()
        if tuples:
            cur.executemany(_INSERT, tuples)
            written += cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else len(tuples)
    ids = [str(r["id"]) for r in rows]
    cur.execute(_DROP_SHADOWED_INHERITED, {"ids": ids})
    written += _inherit_threads(cur, ids=ids, keys=sorted({str(r["key"]) for r in rows}))
    return written, skipped, conflicts


def _inherit_threads(cur, *, ids: list[str], keys: list[str]) -> int:
    """Give each threadless reply among the candidates its parent's thread (`_INHERIT_CANDIDATES`), then the replies
    to the keys that just inherited, until a round inherits nothing. Returns the edges written.

    It ends: every round that goes on wrote an `in_thread` edge for a message that had none, and a message with
    one is never a candidate again, so there are at most as many rounds as threadless replies. A cycle of replies
    with no thread anywhere writes nothing in its first round."""
    written = 0
    while ids or keys:
        cur.execute(_INHERIT_CANDIDATES, {"ids": ids, "keys": keys})
        tuples = [(c["src_id"], c["src_key"], c["src_version"], "in_thread", c["dst"], "inherited", c["recorded_at"])
                  for c in cur.fetchall()
                  if isinstance(c["value"], dict) and normalize.thread_of(c["value"])[0] is None]
        if not tuples:
            return written
        cur.executemany(_INSERT, tuples)
        inserted = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else len(tuples)
        if inserted == 0:
            # Nothing landed, so another round would read the same candidates again: stop rather than spin.
            return written
        written += inserted
        ids, keys = [], sorted({str(t[1]) for t in tuples})
    return written


def _advance(cur, rows: list[dict], edges_written: int, *, rebuilt: bool,
             key: tuple | None, from_seq: int) -> tuple:
    """Move the walk from `from_seq`, the position this pass read, past `rows`: the seq of
    the last one, and the reader-facing key to the greatest (created_at, id) walked so far.
    Returns that key.

    A COMPARE-AND-SET (STATUS D21): the row is written only while it still holds
    `from_seq`, so a pass that read an older position than the one now saved changes
    nothing and raises. Every row in `rows` has seq > `from_seq` (the walk's WHERE), so a
    write that lands only ever moves the position forward. rebuild's reset writes NULL,
    which COALESCE reads as 0, the position its walk starts from."""
    key = greatest_key(key, rows)
    cur.execute(
        "UPDATE mesh_edges_projection SET "
        "  watermark_seq = %s, watermark_at = %s, watermark_id = %s, "
        "  rows_projected = rows_projected + %s, edges_projected = edges_projected + %s, "
        "  rebuilt_at = CASE WHEN %s THEN now() ELSE rebuilt_at END, updated_at = now() "
        "WHERE projection = %s AND COALESCE(watermark_seq, 0) = %s",
        (rows[-1]["seq"], key[0], key[1], len(rows), edges_written, rebuilt,
         PROJECTION_NAME, int(from_seq)),
    )
    if cur.rowcount != 1:
        raise ConcurrentWalk(f"the {PROJECTION_NAME} watermark moved from {from_seq} during "
                             "this pass; another pass is running")
    return key


def catch_up(conn, *, max_batch: int = DEFAULT_BATCH,
             namespace: str = MESH_CHAT_NAMESPACE) -> Report:
    """Project every log row after the watermark, up to `max_batch`. Idempotent.

    Runs under `rls.projection_writer` (server_internal plus the job-only write flag, both
    of which sql_meshychat/008 requires to write either table; divergence 022). `conn`
    must be an autocommit connection with no open transaction (rls.py refuses otherwise),
    so the whole pass is one transaction that either lands with its watermark or not at all.
    """
    _require_mesh_chat(namespace)
    with rls.projection_writer(conn):
        with conn.cursor(row_factory=dict_row) as cur:
            hold_against_blanking(cur)
            _ensure_state_row(cur)
            after_seq, after_at, after_id = _read_watermark(cur)
            cur.execute(_SELECT_ROWS, {"ns": namespace, "after_seq": int(after_seq),
                                       "limit": int(max_batch)})
            rows = cur.fetchall()
            if not rows:
                return Report(0, 0, {}, (), after_at,
                              str(after_id) if after_id is not None else None, more=False)
            written, skipped, conflicts = _project(cur, rows)
            key = _advance(cur, rows, written, rebuilt=False, key=(after_at, after_id),
                           from_seq=after_seq)
    return Report(rows_read=len(rows), edges_written=written, skipped=skipped,
                  conflicts=tuple(conflicts), watermark_at=key[0],
                  watermark_id=str(key[1]), more=(len(rows) >= max_batch))


def rebuild(conn, *, namespace: str = MESH_CHAT_NAMESPACE,
            batch: int = DEFAULT_BATCH) -> Report:
    """Empty the projection and replay the whole log. One transaction.

    Discipline 1. The truncate and every insert commit together, so a reader never sees
    a half-rebuilt projection: it sees the old one until the new one is complete.
    """
    _require_mesh_chat(namespace)
    total_rows = 0
    total_edges = 0
    skipped: dict[str, int] = {}
    conflicts: list[str] = []
    key = None
    with rls.projection_writer(conn):             # divergence 022: the job-only writer
        with conn.cursor(row_factory=dict_row) as cur:
            hold_against_blanking(cur)
            # DELETE, NOT TRUNCATE, and this is a privilege fact rather than a
            # preference. TRUNCATE is a SEPARATE Postgres privilege that `meshychat_app`
            # does not hold on any table here - the default privileges in sql/010
            # grant SELECT/INSERT/UPDATE/DELETE and stop there. So `TRUNCATE` worked
            # in the tests, which connect as the owner role (`meshychat_owner`), and failed
            # the moment the CLI ran it: `python -m apps.mesh rebuild` goes through
            # dbconn.serving_connection(), which does SET ROLE meshychat_app, and died
            # with "permission denied for table mesh_edges".
            #
            # Granting TRUNCATE to meshychat_app would have been the other fix and is the
            # wrong one: it widens the app role for every table to make one
            # maintenance command faster. DELETE is transactional here exactly as
            # TRUNCATE was, takes a weaker lock, and keeps the role uniform.
            cur.execute("DELETE FROM mesh_edges")
            _ensure_state_row(cur)
            cur.execute("UPDATE mesh_edges_projection SET watermark_seq = NULL, "
                        "watermark_at = NULL, watermark_id = NULL, rows_projected = 0, "
                        "edges_projected = 0 WHERE projection = %s", (PROJECTION_NAME,))
            after_seq = 0
            while True:
                cur.execute(_SELECT_ROWS, {"ns": namespace, "after_seq": after_seq,
                                           "limit": int(batch)})
                rows = cur.fetchall()
                if not rows:
                    break
                written, sk, cf = _project(cur, rows)
                for k, n in sk.items():
                    skipped[k] = skipped.get(k, 0) + n
                conflicts.extend(cf)
                key = _advance(cur, rows, written, rebuilt=True, key=key,
                               from_seq=after_seq)
                total_rows += len(rows)
                total_edges += written
                after_seq = rows[-1]["seq"]
                if len(rows) < batch:
                    break
            if total_rows == 0:
                cur.execute("UPDATE mesh_edges_projection SET rebuilt_at = now(), "
                            "updated_at = now() WHERE projection = %s", (PROJECTION_NAME,))
    return Report(rows_read=total_rows, edges_written=total_edges, skipped=skipped,
                  conflicts=tuple(conflicts), watermark_at=key[0] if key else None,
                  watermark_id=str(key[1]) if key else None, more=False)


def backfill(conn, *, types: tuple[str, ...] = ("closes", "mentions"),
             batch: int = DEFAULT_BATCH, namespace: str = MESH_CHAT_NAMESPACE) -> BackfillReport:
    """Insert only new named edge kinds from the covered head-msg prefix, without moving the watermark.

    Run after the migration and code deploy. It is safe beside catch-up: the mesh_edges
    primary key and `_INSERT`'s ON CONFLICT DO NOTHING make either writer's duplicate a no-op.
    The watermark is read once before the replay; rows committed after it are catch-up's
    responsibility, so a backfill never reports coverage it did not establish.

    `in_thread_inherited` (divergence 073) gives the replies already in the log the inherited
    thread a rebuild would give them, without the rebuild: each batch runs `_inherit_threads`
    over its rows and the replies to its keys. Every key has one head row, so walking the heads
    reaches every reply, a non-head version included, through its parent's key; and every edge
    it reads from is already projected (the walk stays below the watermark), so the lowest
    version rule picks what a rebuild picks, in whichever order the batches meet the chain.
    """
    _require_mesh_chat(namespace)
    wanted = tuple(types)
    if not wanted or set(wanted) - set(BACKFILL_TYPES) or batch < 1:
        raise ValueError(f"backfill requires a subset of {', '.join(BACKFILL_TYPES)} and a positive batch")
    with rls.projection_writer(conn):
        with conn.cursor(row_factory=dict_row) as cur:
            _ensure_state_row(cur)
            through_seq, _, _ = _read_watermark(cur)
    if not through_seq:
        return BackfillReport(0, 0)
    after = total_rows = total_edges = 0
    select = (
        "SELECT o.seq, sm.id, sm.key, sm.version, sm.value, sm.author_label, sm.embed_policy, sm.created_at "
        "FROM mesh_commit_order o JOIN shared_memory sm ON sm.id = o.memory_id "
        "WHERE o.seq > %(after)s AND o.seq <= %(through)s AND sm.namespace = %(ns)s "
        "AND sm.is_head AND sm.key LIKE 'msg/%%' "
        "ORDER BY o.seq ASC LIMIT %(limit)s "
        "FOR SHARE OF sm"  # as _SELECT_ROWS: a redaction waits for this batch, or this batch reads its tombstone
    )
    while True:
        with rls.projection_writer(conn):
            with conn.cursor(row_factory=dict_row) as cur:
                hold_against_blanking(cur)
                cur.execute(select, {"after": after, "through": int(through_seq), "ns": namespace,
                                     "limit": int(batch)})
                rows = cur.fetchall()
                if not rows:
                    break
                from apps.meshsem import store
                gate_open = store.forbidden_rows_open()
                for row in rows:
                    tuples = [t for t in normalize.project_row(
                        row, mentions_allowed=gate_open or row.get("embed_policy") == "allowed"
                    ).rows() if t[3] in wanted]
                    if tuples:
                        cur.executemany(_INSERT, tuples)
                        total_edges += max(0, cur.rowcount or 0)
                if "in_thread_inherited" in wanted:
                    total_edges += _inherit_threads(cur, ids=[str(r["id"]) for r in rows],
                                                    keys=sorted({str(r["key"]) for r in rows}))
                after = int(rows[-1]["seq"])
                total_rows += len(rows)
        if len(rows) < batch:
            break
    return BackfillReport(total_rows, total_edges)


def run_catch_up_job(conn, job) -> int:
    """The registered job body (apps/jobs/registry.py). Returns rows read, which the runner
    compares to `job.max_batch` to decide whether more work is pending."""
    return catch_up(conn, max_batch=job.max_batch).rows_read


def state(conn) -> dict:
    """The projection row, or an empty dict when it has never been written.

    Like everything in graph.py, this READS under a context the caller already holds: the
    route wrapper enters `server_internal` before the handler runs, and entering it again
    here raised `server_internal() needs a connection with NO open transaction` on the
    first request to /v1/mesh/drift. `rebuild` and `catch_up` are the opposite case on
    purpose: they are called from outside any context and enter their own.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM mesh_edges_projection WHERE projection = %s",
                    (PROJECTION_NAME,))
        row = cur.fetchone()
    return dict(row) if row else {}
