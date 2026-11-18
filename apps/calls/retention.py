"""`skill_calls` retention: the read-result prune and the row retention (STATUS D14).

Before these jobs, nothing removed a `skill_calls` row, so the table, its events and its
chunks grew for ever (#407), and D17's rule "a spent flow nonce is kept
while its consuming call exists" (divergence 036) kept every spent nonce for ever too: the
call it waited on never went.

RULE (i): BOTH ARE KEYED ON THE CALL'S OWN TERMINAL AGE, and on nothing else. A row is
eligible only in a terminal status (`completed`, `failed`, `timeout`, `abandoned`) with a
`completed_at` older than the window. The grant that authorised the call, its caller and its
target are never read, so no live object's identity decides what goes. A row in flight has
no terminal age and is never eligible, however old; a terminal row with no `completed_at`
is not either: a call whose terminal end cannot be established is not eligible for deletion.

THE READ-RESULT PRUNE (`prune_read_result_payloads`). A `memory.query`, `memory.read` or, since
053, `memory.list` returns its
data through the call row, so a caller can poll it; an hour after the call ends the result
is replaced by an audit summary (`pruned`, the op, the original size, signature and kid, the
result count), the signature columns and the canonical payload are cleared (#596: the
payload is a superset of the result, so leaving it would keep exactly the bytes the prune
exists to delete, served under a NULL signature), and the attachments emptied. The row stays.
The prune is a job of its own at 60 s, not on the 5 s deadline-sweep tick, because its
window is an hour and nothing is gained by asking every 5 s. The victim query matches
the partial index sql_meshychat/028 ships (`skill_calls_result_prune_v2_idx`: sql/092's with
`memory.list` added, divergence 053 part 2) exactly.

THE ROW RETENTION (`prune_completed_calls`). A call terminal for longer than
`MESHYCHAT_CALL_RETENTION_DAYS` (default 90, at least 1) is deleted; its
events, chunks and webhook attempts go by ON DELETE CASCADE (sql/007, sql/008,
sql_meshychat/021). It runs every 15 minutes: a bounded delete does not need the deadline
sweep's 5 s tick, and on a table carrying its whole history the first sweeps are a backfill that
should not fire 720 times an hour.

BOTH ARE BOUNDED (B5) and take their rows `FOR UPDATE SKIP LOCKED` so a row held by an
in-flight write is skipped rather than waited on. The read-result prune goes oldest
completion first, on sql/092's partial index. The row retention goes OLDEST CREATION first,
on sql/101's `skill_calls_created_at_idx` (review finding 1, 2026-09-26): no index is keyed
on `completed_at` for every terminal row, so an `ORDER BY completed_at` reads and sorts the
whole terminal table on every tick, and the LIMIT bounds only what is deleted.
Under the job's statement_timeout (30 s for the row retention, 20 s for the read prune) that
is the failure 092's header records for the read prune: every tick times out and nothing is
deleted. `created_at < cutoff` is added
beside `completed_at < cutoff`; a call is created before it completes, so the added
predicate is implied and removes no victim (a row whose clock says otherwise is only
deleted later, never wrongly), and the index is then walked in order and stops at the
LIMIT. What a tick still reads beyond its victims is the residue created before the cutoff
that is not eligible: calls still in flight (the deadline sweep ends those) and terminal
rows with no `completed_at`. A partial index on `completed_at` for the terminal statuses built
CONCURRENTLY needs a `-- no-transaction` migration outside this batch's one number; see
docs/DIVERGENCES.md. Both run under
`server_internal`: `skill_calls` is FORCE-RLS, its SELECT policy is peer-scoped, and its
UPDATE and DELETE policies admit only server_internal (sql_meshychat/012), so without the
context both would match nothing and report a clean, empty sweep.

WHAT THE ROW RETENTION ENDS FOR OTHER TABLES. A receipt (`GET /v1/calls/{id}/receipt`) and a
poll of the call answer not-found once the row is gone. A spent flow nonce
consumed by the call becomes reclaimable by `flow_nonce_sweep` on its next tick (D17). Rows
that name the call without a foreign key (`push_notification_configs`,
`work_pool.materialized_call_id`, `invocation_frames.call_id`) keep the name.
"""
from __future__ import annotations

import logging
import os

from apps.calls import terminal
from apps.core import rls
from apps.core.env import lookup

log = logging.getLogger("meshychat.calls.retention")

#: Read results are retained for one hour.
READ_RESULT_RETENTION_SECS = 60 * 60
#: A batch of 100 (divergence 046): on production a retained memory.query result averages about
#: 516 kB stored, and the statement de-TOASTs
#: each one several times and deletes its TOAST chunks, so 1000 rows is measured at roughly
#: ten times the job's 20 s statement_timeout, while 100 fits it. A full batch reports more
#: work pending, so the runner comes back for the rest on its next pass.
READ_RESULT_PRUNE_BATCH = 100
#: Default call retention is 90 days.
CALL_RETENTION_DAYS_DEFAULT = 90
CALL_PRUNE_BATCH = 5_000
#: The row-retention cadence.
RETENTION_TICK_SECS = 15 * 60

#: The statuses that end a call (sql/006's CHECK): the ONE terminal mapping's set
#: (apps/calls/terminal.py, divergence 041 S6), not a copy of it. The
#: two statements below spell them as literals, the predicate sql/092's partial index matches.
TERMINAL_STATUSES = terminal.TERMINAL_STATUSES
#: The skills whose results the read prune clears; the tuple is the predicate of sql/092's
#: partial index. memory.list joined it with divergence 053 part 2 (sql_meshychat/028): it
#: answers with a preview of each row, which is row text like a read's.
READ_SKILLS = ("memory.query", "memory.read", "memory.list")


def call_retention_secs(env=None) -> int:
    """Read MESHYCHAT_CALL_RETENTION_DAYS (the retired MESHY_ spelling is honored by lookup)
    when it is an integer of at least 1, else use the 90-day default. Read on every tick, so a
    changed value takes effect without a restart of the loop."""
    env = os.environ if env is None else env
    raw = (lookup("MESHYCHAT_CALL_RETENTION_DAYS", source=env) or "").strip()
    try:
        days = int(raw)
    except ValueError:
        days = CALL_RETENTION_DAYS_DEFAULT
    if days < 1:
        days = CALL_RETENTION_DAYS_DEFAULT
    return days * 24 * 60 * 60


#: The memo's version as a number, read only inside a CASE that has checked it IS a JSON number.
_MV = "jsonb_extract_path_text(sc.result, 'memo', 'version')::numeric"


def prune_read_result_payloads(conn, *, older_than_secs: int, limit: int) -> int:
    """Prune retained read-result payloads and return the rows pruned.

    The statuses and skill names are SQL literals, not parameters, so the predicate is
    textually sql/092's index predicate and the planner can prove the partial index applies
    (092's header: without it the victim query seq-scans and de-TOASTs the whole table).
    `conn` must be an autocommit connection with no open transaction (rls.py). The summary
    reads the OLD row: in an UPDATE's SET list a column reference is the prior value, so
    `sc.result` and `sc.result_signature` are the ones being replaced.

    A memory.read's summary keeps `memo_version`, the version the read RETURNED, and only as an
    integer from 1 to 2**31-1: anything else under memo.version (an object, an array, a string, a
    fraction, a huge number) is dropped, because a pruned row is never pruned again and would keep
    it for good (Codex on #111). GET /v1/mesh/unread (divergence 074) counts a read by the version it
    fetched, and without it a read older than an hour could only be judged by when it was
    made, which credits a read that raced an edit with the edit. Other skills have no memo, so
    jsonb_strip_nulls drops the key.

    THE PLAN IS PINNED (divergence 046, OI-45 of 2026-09-27). The job runs as `meshychat_app`
    under FORCE RLS, and the victim query takes `FOR UPDATE`, so Postgres adds the UPDATE
    policy's qual beside the SELECT policy's. That qual has no column in it, and as a
    security qual it is estimated at the default selectivity (0.005) instead of being
    treated as a one-time filter, so the planner expects about a hundred victims, below the
    LIMIT. With the LIMIT no longer cutting the cost, it prefers a Seq Scan plus Sort over
    the ordered walk of 092's partial index, and on production that read the 5.7 GB heap and
    de-TOASTed results until the 20 s statement_timeout cancelled every tick. An EXPLAIN as
    the compatibility database role `meshy` (now `meshychat_owner`) bypasses RLS and shows the index plan, which is why
    check 092's header asks
    for did not catch it. Seq scans and sorts are switched off for THIS transaction only
    (`set_config(..., true)`, the same mechanism as `app.server_internal`): the only plan
    left that yields `ORDER BY completed_at` is the ordered range of the partial index,
    which stops at the LIMIT. If 092's index were ever missing or invalid, the planner
    would fall back to the penalised seq scan, which is today's failure and no worse."""
    if limit <= 0:
        raise ValueError("limit must be > 0; a zero bound would be a silent no-op")
    with rls.server_internal(conn):
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('enable_seqscan', 'off', true), "
                        "set_config('enable_sort', 'off', true)")
            cur.execute(
                "WITH victims AS ("
                "  SELECT id FROM skill_calls "
                "   WHERE skill_name IN ('memory.query', 'memory.read', 'memory.list') "
                "     AND status IN ('completed', 'failed', 'timeout', 'abandoned') "
                "     AND result IS NOT NULL "
                "     AND completed_at IS NOT NULL "
                "     AND completed_at < now() - make_interval(secs => %s::double precision) "
                "     AND COALESCE(jsonb_extract_path_text(result, 'pruned'), 'false') <> 'true' "
                "   ORDER BY completed_at ASC "
                "   LIMIT %s "
                "   FOR UPDATE SKIP LOCKED"
                ") "
                "UPDATE skill_calls AS sc "
                "   SET result = jsonb_strip_nulls(jsonb_build_object("
                "         'ok', true, "
                "         'pruned', true, "
                "         'pruned_reason', 'read_result_retention', "
                "         'pruned_at', to_jsonb(now()), "
                "         'skill_name', sc.skill_name, "
                "         'op', jsonb_extract_path_text(sc.result, 'op'), "
                "         'original_result_bytes', pg_column_size(sc.result), "
                "         'original_result_signature', sc.result_signature, "
                "         'original_result_signing_kid', sc.result_signing_kid, "
                "         'memo_version', CASE WHEN jsonb_typeof(jsonb_extract_path(sc.result, 'memo', "
                "           'version')) = 'number' THEN CASE WHEN " + _MV + " BETWEEN 1 AND 2147483647 "
                "           AND " + _MV + " = trunc(" + _MV + ") THEN to_jsonb(" + _MV + "::bigint) END END, "
                "         'result_count', CASE "
                "             WHEN jsonb_typeof(jsonb_extract_path(sc.result, 'results')) "
                "                  = 'array' "
                "             THEN jsonb_array_length(jsonb_extract_path(sc.result, 'results')) "
                "             ELSE NULL END)), "
                "       result_attachments = ARRAY[]::uuid[], "
                "       result_signature = NULL, "
                "       result_signing_kid = NULL, "
                "       result_payload = NULL "
                "  FROM victims WHERE sc.id = victims.id",
                (int(older_than_secs), int(limit)))
            return cur.rowcount


def _cutoff(cur, older_than_secs: int):
    """now() - the window, read from the server, as a timestamptz the planner can compare
    with an index bound (a parameter, not an expression per row)."""
    cur.execute("SELECT now() - make_interval(secs => %s::double precision)",
                (int(older_than_secs),))
    return cur.fetchone()[0]


def prune_completed_calls(conn, *, older_than_secs: int, limit: int) -> int:
    """Delete up to `limit` calls terminal for longer than `older_than_secs`, oldest
    CREATION first. Return rows deleted; cascaded events, chunks and webhook attempts are not
    counted. The cutoff is computed once, so the two predicates compare against the same instant."""
    if limit <= 0:
        raise ValueError("limit must be > 0; a zero bound would be a silent no-op")
    with rls.server_internal(conn):
        with conn.cursor() as cur:
            cur.execute(
                "WITH victims AS ("
                "  SELECT id FROM skill_calls "
                "   WHERE status IN ('completed', 'failed', 'timeout', 'abandoned') "
                "     AND completed_at IS NOT NULL "
                "     AND completed_at < %(cutoff)s "
                "     AND created_at < %(cutoff)s "
                "   ORDER BY created_at ASC "
                "   LIMIT %(limit)s "
                "   FOR UPDATE SKIP LOCKED"
                ") "
                "DELETE FROM skill_calls WHERE id IN (SELECT id FROM victims)",
                {"cutoff": _cutoff(cur, older_than_secs), "limit": int(limit)})
            return cur.rowcount


def run_read_result_prune_job(conn, job) -> int:
    """The registered body of `call_read_result_prune` (apps/jobs/registry.py)."""
    n = prune_read_result_payloads(conn, older_than_secs=READ_RESULT_RETENTION_SECS,
                                   limit=job.max_batch)
    if n:
        log.info("pruned %d retained read result payload(s)", n)
    return n


def run_call_retention_job(conn, job) -> int:
    """The registered body of `call_retention` (apps/jobs/registry.py)."""
    n = prune_completed_calls(conn, older_than_secs=call_retention_secs(), limit=job.max_batch)
    if n:
        log.info("pruned %d completed skill_calls row(s)", n)
    return n


__all__ = ["CALL_PRUNE_BATCH", "CALL_RETENTION_DAYS_DEFAULT", "READ_RESULT_PRUNE_BATCH",
           "READ_RESULT_RETENTION_SECS", "RETENTION_TICK_SECS", "call_retention_secs",
           "prune_completed_calls", "prune_read_result_payloads", "run_call_retention_job",
           "run_read_result_prune_job"]
