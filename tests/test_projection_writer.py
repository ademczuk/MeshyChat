"""A request path cannot write a projection: the DATABASE refuses it. Divergence 022.

THE GAP THIS CLOSES. "No request path ever writes a projection table" was a convention.
Every derived table in sql_meshychat/ (004 mesh_edges and its watermark, 006 the vectors
and their state row, 007 the inferred edges and their ledger) carried ONE policy,
`FOR ALL ... app.server_internal = '1'`, and the mesh read views already run inside
`dbconn.request_server_internal`. So any view that ran an INSERT inside the context it
already holds would have written a projection, and the database would have agreed.

sql_meshychat/008 splits each of those policies: SELECT stays under server_internal;
INSERT, UPDATE and DELETE additionally need `app.projection_writer = '1'`, which only
`apps.core.rls.projection_writer()` sets and only the background jobs and their CLIs
enter. These tests hold that three ways:

  * the database refuses an INSERT from the request context, and accepts the same row
    from the job context (the positive control that the refusal is the policy, not a bad
    row), for every projection table;
  * UPDATE and DELETE from the request context touch no row, while the job context
    touches exactly one; the writer flag WITHOUT server_internal writes nothing either;
  * the flag is set in exactly one place in the code, entered only by the job modules,
    and every table sql_meshychat/ creates is classified, so a new derived table cannot
    copy the old FOR ALL shape without this file noticing.

House rules (docs/V1-DESIGN-RULES.md C1): every refusal sits beside its positive control,
differing in the one variable under test, the context.

Run:  pytest tests/test_projection_writer.py -q
"""
from __future__ import annotations

import ast
import datetime as _dt
import os
import re
import sys
import uuid
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

from apps.core import dbconn, rls  # noqa: E402
from tests.fixtures import meshsem_db as db  # noqa: E402

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")

#: Every table a request must never write: derived from shared_memory, rebuilt by a job.
PROJECTION_TABLES = ("mesh_edges", "mesh_edges_projection", "mesh_embeddings",
                     "mesh_embeddings_projection", "mesh_inferred_edges",
                     "mesh_inferred_audits", "mesh_lexical", "mesh_related_edges",
                     "mesh_related_runs", "mesh_related_projection",
                     # sql_meshychat/047, v1.1 step 6: the transcript-context index, rebuilt by a re-walk from seq 0.
                     "ctx_index_state", "mesh_ctx_chunks", "mesh_ctx_vectors", "ctx_dead_letter",
                     "ctx_source_gen",
                     # sql_meshychat/048: the context index's lexical terms.
                     "mesh_ctx_lexical")

#: Tables sql_meshychat/ creates that are NOT projections, each with the reason. A new
#: table must land in one of the two lists: the classification test refuses a silent one.
NOT_PROJECTIONS = {
    "shared_memory_edit": "the edit RECORDS (sql_meshychat/029, divergence 059): a PRIMARY record of who replaced which version and "
                          "whom to tell, append-only for every role, written only by memory.edit, holding no text; derived from "
                          "nothing re-buildable",
    "shared_memory_redaction": "the redaction RECORDS (sql_meshychat/027, divergence 053): a PRIMARY record of who "
                               "withdrew which row and when, append-only for every role, written only by memory.redact; "
                               "derived from nothing re-buildable",
    "invocation_chain_retention": "D08's retirement LEDGER (sql_meshychat/026, divergence 052): a "
                                  "PRIMARY record, one row per retired chain with its anchor copied "
                                  "byte for byte, written only by the SECURITY DEFINER function "
                                  "retire_invocation_chain; derived from nothing re-buildable",
    "invocation_frame_retention_policy": "D08's one-row retention POLICY (sql_meshychat/026, "
                                         "divergence 052): configuration only the owner role writes; "
                                         "derived from nothing",
    "background_job_leases": "the job runner's own lease row (sql_meshychat/002); no RLS, "
                             "written by the runner, derived from nothing",
    "mesh_route_usage": "a PRIMARY record of requests (sql_meshychat/005): the request "
                        "is the thing it counts, so the request path is its writer",
    "mesh_summary_requests": "a PRIMARY record of summary requests (sql_meshychat/017, "
                             "divergence 037): admission counts and outcomes, written by "
                             "the request it records; no message text, key or thread id, "
                             "so nothing in it is derived from shared_memory",
    "shared_memory_legacy_verdict": "APPEND-ONLY verdicts on historical shared_memory rows "
                                    "(sql_meshychat/014, divergence 039): 008's SELECT and "
                                    "INSERT split, but no UPDATE or DELETE policy at all and "
                                    "both revoked from meshy_app, so it cannot carry 008's "
                                    "four-policy shape",
    "hub_connections": "which sockets each worker holds (sql_meshychat/016, divergence 041 "
                       "S2): derived from no table; every command server_internal only, "
                       "which a request can also enter, so its writers are held to an "
                       "allowlist in tests/test_hub_presence.py (empty until S3 wires the "
                       "CallBridge and the consumer's disconnect)",
    "peer_invocation_webhooks": "a PRIMARY record of each peer's doorbell registration "
                                "(sql_meshychat/021, divergence 041 S9): written by the "
                                "peer's PUT and DELETE and by run_call_worker's verification; "
                                "derived from no table; every command server_internal only",
    "peer_webhook_attempts": "a PRIMARY record of each doorbell (sql_meshychat/021, divergence "
                             "041 S9): inserted by step 9, claimed and finished by "
                             "run_call_worker; derived from no table; every command "
                             "server_internal only",
    "peer_webhook_verifications": "a PRIMARY record of each verification POST "
                                  "(sql_meshychat/021, divergence 041 S9, the review's F1 "
                                  "and F3): claimed and finished by run_call_worker; derived "
                                  "from no table; every command server_internal only",
    "capability_revocation": "a PRIMARY record: the revoked capability block ids "
                             "(sql_meshychat/019, divergence 040), written by a revoke, "
                             "derived from nothing, append-only and never pruned (Q8)",
    "capability_event": "a PRIMARY record: the append-only issuance and revocation log "
                        "whose issue rows are the capability issuance register "
                        "(sql_meshychat/019, divergence 040); derived from nothing",
    "ctx_commit_order": "the commit order of transcript-context MANIFESTS (sql_meshychat/047, v1.1 step 6): "
                        "written ONLY by the SECURITY DEFINER constraint trigger meshy_ctx_commit_stamp at each "
                        "writer's commit, as mesh_commit_order is; meshychat_app holds no INSERT, UPDATE or DELETE",
    "ctx_chunk_changes": "the per-space change counter of mesh_ctx_chunks, the shard plan cache's freshness stamp "
                         "(sql_meshychat/049): written ONLY by the SECURITY DEFINER constraint trigger "
                         "meshy_ctx_chunk_changed at each chunk writer's commit, as ctx_commit_order is; meshychat_app "
                         "holds no INSERT, UPDATE or DELETE",
    "ctx_shard_plan": "the stored shard plan of mesh_ctx_chunks (sql_meshychat/053, v1.1 step 6b): derived from the "
                      "chunks, but written ONLY by its SECURITY DEFINER helpers from statement triggers on every chunk "
                      "write, cascades included, as ctx_chunk_changes is; meshychat_app holds no INSERT, UPDATE or DELETE",
    "ctx_shard_setting": "the one-row shard size the 053 triggers cut with (sql_meshychat/053): set by the owner role, "
                         "derived from nothing, no request or job writes it",
    "ctx_index_config": "the one-row configuration naming the context namespace the commit trigger stamps "
                        "(sql_meshychat/047): set by the owner role, derived from nothing, no request or job writes it",
    "mesh_commit_order": "the commit order of shared/mesh-chat rows (sql_meshychat/023, "
                         "STATUS D19): written ONLY by a SECURITY DEFINER constraint "
                         "trigger at each writer's commit, never by a job or a request; "
                         "meshy_app holds no INSERT, UPDATE or DELETE on it at all, so it "
                         "cannot carry 008's four-policy shape",
    "background_job_cursors": "the job runner's own cursor rows (sql_meshychat/023): the "
                              "frame audit's and the chain-head reconcile's positions, like "
                              "background_job_leases derived from nothing and written only "
                              "by the job bodies under their lease; no RLS",
    "skill_call_heartbeats": "a PRIMARY record of when a running call's target was last seen "
                             "(sql_meshychat/023, STATUS F09): written by the heartbeat job "
                             "from presence reads, not derived from shared_memory; every "
                             "command server_internal only, the shape of hub_connections",
    "file_message_grants": "a PRIMARY record of who a mail message granted its files to (sql_meshychat/043, divergence "
                           "098): recipients resolved from to/cc AT WRITE TIME, which a later roster cannot reproduce, so "
                           "it cannot be rebuilt by a job; written in memory.write's own transaction and deleted by "
                           "memory.redact, every command server_internal only",
    "file_downloads": "a PRIMARY record of presigned download URLs (sql_meshychat/043, divergence 098): the request "
                      "is the thing it records, so the presign path is its writer; derived from nothing",
    "card_heads": "the narrow record of each card/ head row that GET /v1/cards filters, orders and walks edges on "
                  "(sql_meshychat/057, divergence 110): derived from shared_memory, but written ONLY by the SECURITY "
                  "DEFINER row trigger meshy_card_head_track in the writer's own statement (and removed by the foreign "
                  "key's cascade), never by a job or a request; meshychat_app holds no INSERT, UPDATE or DELETE",
}

#: The only modules that may enter rls.projection_writer(): the job bodies, which the
#: CLIs call. Each is named with the functions that enter it.
WRITER_MODULES = {
    "apps.mesh.projection": {"catch_up", "rebuild", "backfill"},
    "apps.meshsem.embedjob": {"catch_up"},
    "apps.meshaudit.derive": {"_txn"},
    # divergence 032: the related_to job; `_record_status` commits a refusal's status in
    # a transaction of its own, so a failed tick still says why.
    "apps.meshsem.related": {"catch_up", "_record_status"},
    # divergence 039: the legacy verdict job appends verdicts, one transaction per tick.
    "apps.memoryskills.legacy_verdict": {"catch_up"},
    # divergence 053, the ONE request path: memory.redact deletes the derived rows of the version it blanks in the same
    # transaction as the tombstone (a job could not do that atomically, and the text would stay findable until it ran).
    "apps.memoryskills.redact": {"handle_redact"},
    # divergence 059, the SECOND request path: memory.edit deletes the derived rows of the version it replaces in the transaction that
    # replaces it, by redact.delete_derived, for the same reason.
    "apps.memoryskills.edit": {"handle_edit"},
    # divergence 060: the graph walker's job, the vectors and terms of the namespaces
    # MESHY_GRAPH_NAMESPACES lists besides the mail (docs/design/wiki-in-graph.md).
    "apps.meshsem.graphwalk": {"catch_up_namespace"},
    # v1.1 step 6 (sql_meshychat/047): the transcript-context index job, its replay loop and its reference recording.
    "apps.meshsem.ctxjob": {"catch_up", "_replay", "_reference", "_keep_vectors"},
    # v1.1 step 6b: the context vectors' retention job deletes chunk occurrences older than 180 days.
    "apps.meshsem.ctxindex": {"run_retention_job", "run_shard_plan_check_job"},
}


@pytest.fixture()
def owner():
    with psycopg.connect(DSN, autocommit=True) as c:
        db.ensure_namespace(c)
        yield c


@pytest.fixture()
def app():
    """meshychat_app, verified, autocommit: the connection a request is served on."""
    c = dbconn.connect(DSN)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture()
def src(owner):
    """A real shared_memory row, so every FK below is satisfied and only a policy can
    refuse the insert."""
    p = db.peer(owner, "pw")
    older = db.row(owner, p, {"body": "projection writer fixture"})
    # A second, NEWER row: mesh_related_edges runs newer -> older between two real rows.
    newer = db.row(owner, p, {"body": "projection writer fixture, newer"},
                   created_at=_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(microseconds=1))
    return {**older, "newer_id": newer["id"], "newer_key": newer["key"]}


def _case(table: str, src: dict) -> dict:
    """The insert, its params, a predicate naming only that row, an UPDATE of one column."""
    tag = f"pw-{uuid.uuid4().hex[:12]}"
    sid, skey = src["id"], src["key"]
    case = {
        "mesh_edges": dict(
            insert="INSERT INTO mesh_edges (src_id, src_key, src_version, edge_type, dst, "
                   "src_field, recorded_at) VALUES (%s, %s, 1, 'authored_by', %s, "
                   "'author_label', now())",
            params=(sid, skey, tag), where="dst = %s", set_="src_field = 'x'"),
        "mesh_edges_projection": dict(
            insert="INSERT INTO mesh_edges_projection (projection) VALUES (%s)",
            params=(tag,), where="projection = %s", set_="rows_projected = 1"),
        "mesh_embeddings": dict(
            insert="INSERT INTO mesh_embeddings (src_id, src_key, src_version, space, dims, "
                   "text_sha256, vec, provider_id, recorded_at) VALUES (%s, %s, 1, %s, 1, "
                   "repeat('a', 64), decode('00000000', 'hex'), 'x', now())",
            params=(sid, skey, tag), where="space = %s", set_="provider_id = 'y'"),
        "mesh_embeddings_projection": dict(
            insert="INSERT INTO mesh_embeddings_projection (space) VALUES (%s)",
            params=(tag,), where="space = %s", set_="rows_seen = 1"),
        "mesh_inferred_edges": dict(
            insert="INSERT INTO mesh_inferred_edges (src_id, src_key, src_version, "
                   "edge_type, dst, model, provider_id, prompt_version, recorded_at) "
                   "VALUES (%s, %s, 1, 'addressed_to', %s, 'm', 'p', 'v', now())",
            params=(sid, skey, tag), where="dst = %s", set_="model = 'm2'"),
        "mesh_inferred_audits": dict(
            insert="INSERT INTO mesh_inferred_audits (src_id, prompt_version, outcome) "
                   "VALUES (%s, %s, 'no_body')",
            params=(sid, tag), where="prompt_version = %s", set_="accepted = 1"),
        # sql_meshychat/010, divergence 026: the lexical arm's lexemes.
        "mesh_lexical": dict(
            insert="INSERT INTO mesh_lexical (src_id, src_key, src_version, space, "
                   "derivation, text_sha256, lexemes, tfs, recorded_at) VALUES (%s, %s, 1, "
                   "%s, 'word2-lower/v1', repeat('a', 64), '{xx}', '{1}', now())",
            params=(sid, skey, tag), where="space = %s", set_="derivation = 'y'"),
        # sql_meshychat/013, divergence 032: persisted related_to edges. The destination
        # is a second real row (the edge runs newer -> older), so only a policy refuses.
        "mesh_related_edges": dict(
            insert="INSERT INTO mesh_related_edges (src_id, src_key, src_version, dst_id, "
                   "dst_key, dst_version, space, embed_provider_id, rerank_provider_id, "
                   "rerank_model, rerank_reference, threshold_version, score_fwd, "
                   "score_rev, pair_score, threshold, rank, fused_rank, recorded_at, "
                   "dst_recorded_at) SELECT %s, %s, 1, o.id, o.key, 1, %s, 'e', 'r', 'm', "
                   "repeat('a', 64), repeat('b', 16), 2, 2, 2, 1, 1, 1, "
                   "s.created_at, o.created_at FROM shared_memory s, shared_memory o "
                   "WHERE s.id = %s AND o.id = %s",
            params=(src.get("newer_id", sid), src.get("newer_key", skey), tag,
                    src.get("newer_id", sid), sid),
            where="space = %s", set_="rank = 2"),
        "mesh_related_runs": dict(
            insert="INSERT INTO mesh_related_runs (src_id, space, threshold_version, "
                   "outcome, pool_size, reverse_scored, edges, rerank_provider_id) "
                   "VALUES (%s, %s, 'v', 'no_candidates', 0, 0, 0, 'r')",
            params=(sid, tag), where="space = %s", set_="pool_size = 1"),
        "mesh_related_projection": dict(
            insert="INSERT INTO mesh_related_projection (space) VALUES (%s)",
            params=(tag,), where="space = %s", set_="rows_seen = 1"),
        # sql_meshychat/047, v1.1 step 6: the transcript-context index.
        "ctx_index_state": dict(
            insert="INSERT INTO ctx_index_state (space) VALUES (%s)",
            params=(tag,), where="space = %s", set_="stuck_attempts = 1"),
        "mesh_ctx_chunks": dict(
            insert="INSERT INTO mesh_ctx_chunks (src_id, space, namespace, chunk_no, seat, src, gen, batch, "
                   "first_ord, last_ord, char_start, char_end, time_first, time_last, text_sha256) VALUES (%s, %s, "
                   "'shared/pw', 0, 's', 'x', 1, 1, 0, 0, 0, 1, now(), now(), repeat('a', 64))",
            params=(sid, tag), where="space = %s", set_="project = 'p'"),
        "mesh_ctx_vectors": dict(
            insert="INSERT INTO mesh_ctx_vectors (space, text_sha256, dim, vector) VALUES (%s, repeat('a', 64), 1, "
                   "decode('00000000', 'hex'))",
            params=(tag,), where="space = %s", set_="dim = 2"),
        "mesh_ctx_lexical": dict(
            insert="INSERT INTO mesh_ctx_lexical (space, text_sha256, derivation, lexemes, tfs) VALUES (%s, "
                   "repeat('a', 64), 'word2-lower/v1', '{xx}', '{1}')",
            params=(tag,), where="space = %s", set_="derivation = 'y'"),
        "ctx_source_gen": dict(
            insert="INSERT INTO ctx_source_gen (space, namespace, seat, src, gen) VALUES ('sp', %s, 's', 'x', 1)",
            params=(tag,), where="namespace = %s", set_="gen = 2"),
        "ctx_dead_letter": dict(
            insert="INSERT INTO ctx_dead_letter (seq, memory_id, space, reason, attempts) VALUES "
                   "((random() * 1e12)::bigint, %s, %s, 'r', 5)",
            params=(sid, tag), where="space = %s", set_="state = 'replay'"),
    }[table]
    case["tag"] = tag
    return case


def _count(conn, table: str, case: dict) -> int:
    """Counted on the OWNER connection, which bypasses RLS: the truth, not a view of it."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table} WHERE {case['where']}", (case["tag"],))
        return cur.fetchone()[0]


# =============================================================== the refusal, per table

@pytest.mark.parametrize("table", PROJECTION_TABLES)
def test_a_request_context_insert_raises_and_the_job_context_lands(owner, app, src, table):
    case = _case(table, src)

    # THE REFUSAL. The request's own context, as every mesh read view holds it.
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as exc:
        with dbconn.request_server_internal(app) as c, c.cursor() as cur:
            cur.execute(case["insert"], case["params"])
    assert "row-level security" in str(exc.value), exc.value
    assert _count(owner, table, case) == 0

    # POSITIVE CONTROL: the identical row, the job's context, lands.
    with rls.projection_writer(app) as c, c.cursor() as cur:
        cur.execute(case["insert"], case["params"])
    assert _count(owner, table, case) == 1

    # And reads are NOT narrowed: the request context still sees what the job wrote.
    with dbconn.request_server_internal(app) as c, c.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table} WHERE {case['where']}", (case["tag"],))
        assert cur.fetchone()[0] == 1, f"{table}: the request context lost its read"


@pytest.mark.parametrize("table", PROJECTION_TABLES)
def test_update_and_delete_from_a_request_touch_nothing(owner, app, src, table):
    """RLS refuses an UPDATE or DELETE by hiding the rows, so these do not raise: they
    match zero rows. Fail closed, asserted as a rowcount beside the job context's 1."""
    case = _case(table, src)
    with rls.projection_writer(app) as c, c.cursor() as cur:
        cur.execute(case["insert"], case["params"])

    with dbconn.request_server_internal(app) as c, c.cursor() as cur:
        cur.execute(f"UPDATE {table} SET {case['set_']} WHERE {case['where']}",
                    (case["tag"],))
        assert cur.rowcount == 0, f"{table}: a request UPDATED a projection row"
        cur.execute(f"DELETE FROM {table} WHERE {case['where']}", (case["tag"],))
        assert cur.rowcount == 0, f"{table}: a request DELETED a projection row"
    assert _count(owner, table, case) == 1

    with rls.projection_writer(app) as c, c.cursor() as cur:
        cur.execute(f"UPDATE {table} SET {case['set_']} WHERE {case['where']}",
                    (case["tag"],))
        assert cur.rowcount == 1, "positive control: the job context updates"
        cur.execute(f"DELETE FROM {table} WHERE {case['where']}", (case["tag"],))
        assert cur.rowcount == 1, "positive control: the job context deletes"
    assert _count(owner, table, case) == 0


def test_the_writer_flag_without_server_internal_writes_nothing(owner, app, src):
    """The policy is AND, not OR: the job flag alone is not a context."""
    case = _case("mesh_edges", src)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with app.transaction(), app.cursor() as cur:
            cur.execute("SELECT set_config('app.projection_writer', '1', true)")
            cur.execute(case["insert"], case["params"])
    assert _count(owner, "mesh_edges", case) == 0
    with rls.projection_writer(app) as c, c.cursor() as cur:        # control
        cur.execute(case["insert"], case["params"])
    assert _count(owner, "mesh_edges", case) == 1


def test_the_writer_context_is_released_with_its_transaction(app, src):
    """SET LOCAL semantics: a pooled connection that ran a job must not carry the flag
    into whatever it serves next."""
    with rls.projection_writer(app) as c, c.cursor() as cur:
        cur.execute("SELECT current_setting('app.projection_writer', true)")
        assert cur.fetchone()[0] == "1", "control: the flag is set inside the block"
    with app.cursor() as cur:
        cur.execute("SELECT coalesce(current_setting('app.projection_writer', true), '')")
        assert cur.fetchone()[0] == "", "the writer flag outlived its transaction"


def test_a_cascade_from_shared_memory_still_removes_projection_rows(owner, app, src):
    """sql_meshychat/008 says RLS does not apply to referential actions, which is why it
    uses policies and not a refusing trigger. Measured, as meshychat_app in the REQUEST
    context: deleting the source message still removes its projection rows, so retention
    is not broken by the writer rule."""
    cases = [(t, _case(t, src)) for t in ("mesh_edges", "mesh_embeddings",
                                          "mesh_inferred_edges", "mesh_lexical",
                                          "mesh_related_edges", "mesh_related_runs")]
    with rls.projection_writer(app) as c, c.cursor() as cur:
        for _t, case in cases:
            cur.execute(case["insert"], case["params"])
    assert all(_count(owner, t, case) == 1 for t, case in cases), "control: rows exist"
    with dbconn.request_server_internal(app) as c, c.cursor() as cur:
        cur.execute("DELETE FROM shared_memory WHERE id = %s", (src["id"],))
        assert cur.rowcount == 1, "control: the request context may delete the message"
    assert [_count(owner, t, case) for t, case in cases] == [0] * len(cases)


# ================================================================ the schema, as data

def test_every_projection_table_has_the_split_policy_and_no_for_all(owner):
    with owner.cursor() as cur:
        cur.execute("SELECT tablename, cmd, coalesce(qual, ''), coalesce(with_check, '') "
                    "FROM pg_policies WHERE schemaname = 'public' "
                    "AND tablename = ANY(%s)", (list(PROJECTION_TABLES),))
        rows = cur.fetchall()
    assert {r[0] for r in rows} == set(PROJECTION_TABLES), "a projection table has no policy"
    for table in PROJECTION_TABLES:
        mine = {r[1]: (r[2], r[3]) for r in rows if r[0] == table}
        assert set(mine) == {"SELECT", "INSERT", "UPDATE", "DELETE"}, (table, sorted(mine))
        assert "projection_writer" not in "".join(mine["SELECT"]), (
            f"{table}: reads must stay under server_internal alone")
        assert "server_internal" in mine["SELECT"][0]
        for cmd in ("INSERT", "UPDATE", "DELETE"):
            text = "".join(mine[cmd])
            assert "projection_writer" in text and "server_internal" in text, (table, cmd)


def _tables_created_by_sql_meshychat() -> set[str]:
    out = set()
    for f in sorted((ROOT / "sql_meshychat").glob("*.sql")):
        out |= set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", f.read_text("utf-8")))
    return out


def test_every_table_sql_meshychat_creates_is_classified():
    """A new table must be declared a projection (and get the split policy) or be named
    here as something else, with the reason. Four tables copied 004's FOR ALL shape
    without anyone deciding it; this makes it a decision."""
    created = _tables_created_by_sql_meshychat()
    assert "mesh_edges" in created, "control: the scan reads the migration files"
    unclassified = created - set(PROJECTION_TABLES) - set(NOT_PROJECTIONS)
    assert not unclassified, (
        f"{sorted(unclassified)}: classify each as a projection (PROJECTION_TABLES, and "
        "give it the split policy) or add it to NOT_PROJECTIONS with the reason")
    stale = (set(PROJECTION_TABLES) | set(NOT_PROJECTIONS)) - created
    assert not stale, f"classified tables no migration creates any more: {sorted(stale)}"


# ================================================================== the code, as data

def _python_files():
    for sub in ("apps", "meshychat", "tools"):
        for p in sorted((ROOT / sub).rglob("*.py")):
            if "__pycache__" not in p.parts:
                yield p


def test_the_writer_flag_is_set_in_exactly_one_place():
    """If the GUC's name appears anywhere else, something can set it without going
    through rls.projection_writer(), and the allowlist below would not see it."""
    where = [str(p.relative_to(ROOT)).replace("\\", "/") for p in _python_files()
             if "app.projection_writer" in p.read_text("utf-8")]
    assert where == ["apps/core/rls.py"], where


def _writer_call_sites() -> dict[str, set[str]]:
    """{module: {enclosing function}} for every call of `<x>.projection_writer(...)`."""
    found: dict[str, set[str]] = {}
    for p in _python_files():
        tree = ast.parse(p.read_text("utf-8"))
        module = ".".join(p.relative_to(ROOT).with_suffix("").parts)
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "projection_writer"):
                    found.setdefault(module, set()).add(fn.name)
    return found


def test_only_the_jobs_enter_the_writer_context():
    sites = _writer_call_sites()
    assert sites, "control: the scan finds the job modules' calls at all"
    assert set(sites) == set(WRITER_MODULES), sorted(sites)
    for module, fns in sites.items():
        assert fns == WRITER_MODULES[module], (module, sorted(fns))
    assert not any(m.endswith(".views") for m in sites), "a view enters the writer context"


def test_a_dry_run_of_the_auditor_holds_no_writer_flag(app):
    """apps/meshaudit/derive._txn: a dry run is READ ONLY and ALSO lacks the flag the
    write policies need, so it is dry twice over; a --write run holds the flag (control)."""
    from apps.meshaudit import derive

    def flag(write: bool):
        with derive._txn(app, write=write) as c, c.cursor() as cur:
            cur.execute("SELECT coalesce(current_setting('app.projection_writer', true), ''),"
                        " current_setting('transaction_read_only')")
            return cur.fetchone()

    assert flag(False) == ("", "on")
    assert flag(True) == ("1", "off")
