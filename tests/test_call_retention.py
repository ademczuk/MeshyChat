"""STATUS D14: `skill_calls` read-result pruning and row retention, and the D14/D17 pair.

RULE (i), THE ONE EVERY TEST HERE HOLDS: a call is removed, or its read result pruned, keyed
on its own TERMINAL AGE (`completed_at`, terminal status only), never on a live object's
identity: not the grant that authorised it, not its caller or target. So every test plants
the rows the job must act on AND, beside them, the rows closest to the boundary that it must
leave alone (the positive controls): a call just inside the window, a call that is old but
still in flight, and a terminal row with no `completed_at`.

THE PAIR WITH D17. `flow_nonce_sweep` keeps a SPENT flow nonce while its consuming call exists
(operator decision 2026-09-26, divergence 036), because the receipt reads the call's parent
from it. Until D14 nothing ever removed a call, so a spent nonce was kept for ever. The pair
test runs both registered jobs in the order the loop runs them and shows the nonce reclaimed
once, and only once, the call retention has removed its call.

Each job runs through `runner.run_due` as the application role (`dbconn.connect`), under a
lease name unique to the test, so a job that needed a privilege or an RLS context the work
connection does not have fails here. Every assertion is scoped to this test's own rows.

Run:  .venv313/Scripts/python.exe -m pytest tests/test_call_retention.py -q
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

from apps.core import dbconn  # noqa: E402
from apps.jobs import registry, runner  # noqa: E402
from tests.fixtures.call_rows import seed_call, seed_peer, seed_skill  # noqa: E402

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")


@pytest.fixture()
def owner():
    """Compatibility role `meshy`: seeding and reading back only, never the connection under test."""
    with psycopg.connect(DSN, autocommit=True) as c:
        yield c


def _run(name: str, *, max_batch: int | None = None) -> runner.Outcome:
    """Run the REGISTERED job `name` once, under a lease row nobody else uses."""
    base = registry.by_name(name)
    job = registry.Job(name=f"t_{name}_{uuid.uuid4().hex[:8]}",
                       interval_secs=base.interval_secs, timeout_secs=base.timeout_secs,
                       jitter_frac=base.jitter_frac,
                       max_batch=base.max_batch if max_batch is None else max_batch,
                       run=base.run)
    try:
        (out,) = runner.run_due(lambda **kw: dbconn.connect(DSN, **kw), jobs=(job,),
                                holder="call-retention-test")
    finally:
        with psycopg.connect(DSN, autocommit=True) as c:
            c.execute("DELETE FROM background_job_leases WHERE job_name = %s", (job.name,))
    assert out.claimed is True, out
    assert out.result == "ok", out
    return out


@pytest.fixture()
def world(owner):
    caller, target = seed_peer(owner, "d14c"), seed_peer(owner, "d14t")
    return {"caller": caller, "target": target, "skill": seed_skill(owner, target)}


def _call(owner, world, tag: str, *, status: str, completed: str | None,
          created: str = "now()", skill_name: str = "s3.skill", result=None) -> str:
    """One call row, then its status, `completed_at` and `created_at` set as given."""
    call_id = seed_call(owner, world["caller"], world["target"], world["skill"],
                        call_id=f"d14-{tag}-{uuid.uuid4().hex}")
    owner.execute(
        f"UPDATE skill_calls SET status = %s, skill_name = %s, result = %s, "
        f"  result_payload = %s, result_signature = %s, result_signing_kid = %s, "
        f"  completed_at = {completed or 'NULL'}, created_at = {created} "
        f"WHERE call_id = %s",
        (status, skill_name, None if result is None else Jsonb(result),
         None if result is None else Jsonb({"canonical": result}),
         None if result is None else "sig-original", None if result is None else "kid-1",
         call_id))
    return call_id


def _present(owner, call_ids) -> set[str]:
    return {r[0] for r in owner.execute(
        "SELECT call_id FROM skill_calls WHERE call_id = ANY(%s)", (list(call_ids),))}


# ------------------------------------------------------------------ row retention

def test_d14_a_terminal_call_past_retention_goes_with_its_children_and_nothing_else(owner,
                                                                                   world):
    """A call terminal for longer than the retention (90 days
    by default) is deleted, and its events and chunks go with it (ON DELETE CASCADE). Kept:
    the same statuses 89 days after completion; a call CREATED 200 days ago that is still
    `running`, `input_required` or `queued` (in flight: no terminal age), one of them
    carrying a stale 91-day `completed_at`; and a terminal row with no `completed_at`, whose
    end this job cannot explain."""
    old = "now() - interval '91 days'"
    gone = {s: _call(owner, world, f"old-{s}", status=s, completed=old,
                     created="now() - interval '120 days'")
            for s in ("completed", "failed", "timeout", "abandoned")}
    kept = {
        "recent": _call(owner, world, "recent", status="completed",
                        completed="now() - interval '89 days'",
                        created="now() - interval '120 days'"),
        "running": _call(owner, world, "running", status="running", completed=None,
                         created="now() - interval '200 days'"),
        "input_required": _call(owner, world, "input", status="input_required",
                                completed=None, created="now() - interval '200 days'"),
        "queued": _call(owner, world, "queued", status="queued", completed=None,
                        created="now() - interval '200 days'"),
        "no_completed_at": _call(owner, world, "nocomp", status="failed", completed=None,
                                 created="now() - interval '200 days'"),
        # In flight with a stale completed_at another writer left: the status decides.
        "running_stamped": _call(owner, world, "runstamp", status="running", completed=old,
                                 created="now() - interval '200 days'"),
    }
    victim = gone["completed"]
    owner.execute("INSERT INTO skill_call_events (call_id, event_type, source) "
                  "VALUES (%s, 'completed', 'meshy')", (victim,))
    owner.execute("INSERT INTO skill_call_chunks (call_id, sequence, chunk_type, content, "
                  "chunk_signature, chunk_signing_kid) VALUES (%s, 0, 'text', %s, 's', 'k')",
                  (victim, b"chunk"))

    out = _run("call_retention")

    assert _present(owner, list(gone.values()) + list(kept.values())) == set(kept.values()), (
        "exactly the terminal calls past retention are deleted")
    assert out.rows >= len(gone), out
    for table in ("skill_call_events", "skill_call_chunks"):
        left = owner.execute(f"SELECT count(*) FROM {table} WHERE call_id = %s",
                             (victim,)).fetchone()[0]
        assert left == 0, f"{table} kept rows of a deleted call"


def _check_statuses(owner) -> set[str]:
    """Every status skill_calls' CHECK constraint admits, read from the catalog."""
    import re
    (defn,) = owner.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = 'skill_calls'::regclass AND conname = 'skill_calls_status_check'"
    ).fetchone()
    return set(re.findall(r"'([a-z_]+)'::text", defn))


def test_d14_no_status_but_a_terminal_one_is_ever_removed_whatever_its_completed_at(owner,
                                                                                   world):
    """Review finding 4 (LOW): the terminal filter is the rule, not `status <> 'running'`.
    Every status the CHECK constraint admits that is NOT terminal (pending, routed, queued,
    running, input_required, auth_required, read from the catalog so a status added later is
    covered) is planted created 120 days ago with a stale 91-day `completed_at` another
    writer left, and every one is kept. CONTROL, in the same tick: a `completed` row of the
    same ages is deleted, and the catalog read does contain the four terminal statuses."""
    from apps.calls import retention
    statuses = _check_statuses(owner)
    assert set(retention.TERMINAL_STATUSES) < statuses, statuses
    in_flight = sorted(statuses - set(retention.TERMINAL_STATUSES))
    assert {"pending", "queued", "input_required", "auth_required"} <= set(in_flight)
    old, created = "now() - interval '91 days'", "now() - interval '120 days'"
    kept = [_call(owner, world, f"nt-{s}", status=s, completed=old, created=created)
            for s in in_flight]
    control = _call(owner, world, "nt-control", status="completed", completed=old,
                    created=created)
    _run("call_retention")
    assert _present(owner, kept + [control]) == set(kept), (
        "only the terminal control goes; no in-flight status is removed")


def test_d14_the_retention_is_bounded_and_oldest_first(owner, world):
    """B5: one tick deletes at most max_batch calls, the OLDEST completion first. Three calls
    completed in 1990, 1991 and 1992 (older than anything else a test plants), max_batch 2:
    the first tick takes 1990 and 1991 and keeps 1992."""
    ids = [_call(owner, world, f"b{y}", status="completed",
                 completed=f"timestamptz '{y}-01-01 00:00:00+00'",
                 created=f"timestamptz '{y}-01-01 00:00:00+00'")
           for y in (1992, 1991, 1990)]
    out = _run("call_retention", max_batch=2)
    assert out.rows == 2, out
    assert _present(owner, ids) == {ids[0]}, "the newest of the three must be the one kept"


def _plan_nodes(plan: dict):
    yield plan
    for child in plan.get("Plans", ()):
        yield from _plan_nodes(child)


def test_d14_the_retention_reads_an_index_range_in_order_not_every_terminal_row(owner):
    """Review finding 1 (MEDIUM): the victim query of the row retention must be served by an
    index in the order it deletes, so the 5000-row LIMIT bounds what a tick READS and not
    only what it deletes. Keyed on `completed_at` alone, no index serves it (sql/092 is
    partial to the two read skills), and the plan is every terminal row read and sorted:
    under the runner's 30 s statement_timeout a production-size table would time out every
    tick and delete nothing, and D17's spent nonces would never be reclaimed either.

    The instrument runs the REAL `prune_completed_calls` on a connection whose cursor turns
    the victim statement into `EXPLAIN (FORMAT JSON)`, with seq scans and sorts disabled:
    the planner then takes a sort-free index plan whenever one EXISTS, which is the property
    at issue (on a few test rows the costs alone would pick a sort either way, and on a
    production table the LIMIT makes the ordered range the cheap plan). Held: an ordered
    Index Scan on sql/101's `skill_calls_created_at_idx` (a call is created before it
    completes, so `created_at < cutoff` is implied by `completed_at < cutoff` and adds no
    row) and no Sort node. POSITIVE CONTROL: the same instrument, on a statement no index
    can order, still sees a Sort node, so the absence below is a reading and not a blind
    spot."""
    from apps.calls import retention

    plans: list[dict] = []

    class _Explain(psycopg.Cursor):
        def execute(self, query, params=None, **kw):
            if isinstance(query, str) and query.lstrip().startswith("WITH victims"):
                super().execute("EXPLAIN (FORMAT JSON) " + query, params, **kw)
                plans.append(self.fetchone()[0][0]["Plan"])
                return self
            return super().execute(query, params, **kw)

    with dbconn.connect(DSN) as conn:
        conn.execute("SET enable_seqscan = off")
        conn.execute("SET enable_sort = off")
        conn.cursor_factory = _Explain
        retention.prune_completed_calls(conn, older_than_secs=90 * 86400, limit=5000)
        with conn.cursor() as cur:
            cur.execute("WITH victims AS (SELECT id FROM skill_calls "
                        "ORDER BY md5(call_id) LIMIT 1) SELECT id FROM victims")
    assert len(plans) == 2, plans
    prune, control = ([(n["Node Type"], n.get("Index Name")) for n in _plan_nodes(p)]
                      for p in plans)
    assert any(t == "Sort" for t, _ in control), f"control: a Sort must be seen: {control}"
    assert ("Index Scan", "skill_calls_created_at_idx") in prune, prune
    assert not any(t in ("Sort", "Incremental Sort") for t, _ in prune), prune
    # A RANGE, not the whole index in order: when a tick has fewer victims than its LIMIT
    # (the steady state), an unbounded ordered walk would read every row newer than them.
    (scan,) = [n for n in _plan_nodes(plans[0])
               if n.get("Index Name") == "skill_calls_created_at_idx"]
    assert "created_at <" in scan.get("Index Cond", ""), scan


def test_oi45_the_read_prune_walks_its_partial_index_as_meshy_app_with_nothing_disabled(owner):
    """OI-45 (production, 2026-09-27): `call_read_result_prune` timed out on every tick. The
    instrument above disables seq scans and sorts on the CONNECTION, which proves only that
    an index plan EXISTS, and that is exactly the switch that hid this failure: as `meshy_app` (now `meshychat_app`)
    under FORCE RLS the victim query's `FOR UPDATE` adds the UPDATE policy's qual, the
    planner expects fewer victims than the LIMIT, and it seq-scans and sorts the table.

    So this runs the REAL `prune_read_result_payloads` on the application connection with
    NOTHING disabled by the test: whatever keeps the plan on the partial index must be the
    function's own doing. Held: an ordered Index Scan on sql/092's
    `skill_calls_result_prune_v2_idx`, a RANGE on `completed_at <`, no Seq Scan on
    skill_calls and no Sort. The LIMIT is the production batch. POSITIVE CONTROL: the same
    instrument on a statement no index can order still sees a Sort node, so the absence
    below is a reading.

    WHAT THIS TABLE CAN AND CANNOT SHOW. Against the function without its plan pin, this test
    fails here too, but on the small test table the unpinned plan's Seq Scan is in the outer
    UPDATE join, not in the victim scan: production's estimate (about a hundred victims under
    a 4-million-row table) does not reproduce on a few rows. The production mechanism was
    proven on production's own planner as `meshy_app` (now `meshychat_app`; docs/DIVERGENCES.md 046): unpinned, a
    Seq Scan and Sort of skill_calls; pinned, the ordered range of the partial index. The pin
    makes the plan the same on both, which is what this test holds."""
    from apps.calls import retention

    plans: list[dict] = []

    class _Explain(psycopg.Cursor):
        def execute(self, query, params=None, **kw):
            if isinstance(query, str) and query.lstrip().startswith("WITH victims"):
                super().execute("EXPLAIN (FORMAT JSON) " + query, params, **kw)
                plans.append(self.fetchone()[0][0]["Plan"])
                return self
            return super().execute(query, params, **kw)

    with dbconn.connect(DSN) as conn:
        conn.cursor_factory = _Explain
        retention.prune_read_result_payloads(
            conn, older_than_secs=retention.READ_RESULT_RETENTION_SECS,
            limit=retention.READ_RESULT_PRUNE_BATCH)
        with conn.cursor() as cur:
            cur.execute("WITH victims AS (SELECT id FROM skill_calls "
                        "ORDER BY md5(call_id) LIMIT 1) SELECT id FROM victims")
    assert len(plans) == 2, plans
    prune, control = ([(n["Node Type"], n.get("Index Name"), n.get("Relation Name"))
                       for n in _plan_nodes(p)] for p in plans)
    assert any(t == "Sort" for t, _, _ in control), f"control: a Sort must be seen: {control}"
    assert ("Index Scan", "skill_calls_result_prune_v2_idx", "skill_calls") in prune, prune
    assert not any(t == "Seq Scan" and r == "skill_calls" for t, _, r in prune), prune
    assert not any(t in ("Sort", "Incremental Sort") for t, _, _ in prune), prune
    (scan,) = [n for n in _plan_nodes(plans[0])
               if n.get("Index Name") == "skill_calls_result_prune_v2_idx"]
    assert "completed_at <" in scan.get("Index Cond", ""), scan


def test_oi45_the_registered_prune_batch_fits_its_timeout_budget():
    """The registered job prunes READ_RESULT_PRUNE_BATCH rows a tick, and that is 100
    (divergence 046): at production's ~516 kB average retained result, 1000
    rows a tick is measured at about ten times the job's 20 s statement_timeout."""
    from apps.calls import retention
    job = registry.by_name("call_read_result_prune")
    assert retention.READ_RESULT_PRUNE_BATCH == 100
    assert job.max_batch == retention.READ_RESULT_PRUNE_BATCH
    assert job.timeout_secs == 20


def test_d14_the_retention_window_is_read_from_the_environment_at_one_day_or_more(
        monkeypatch):
    """The retention-window helper reads MESHYCHAT_CALL_RETENTION_DAYS only when it parses and is
    at least 1; anything else is the 90-day default."""
    from apps.calls import retention
    for raw, days in (("30", 30), ("1", 1), ("0", 90), ("-5", 90), ("abc", 90), ("", 90)):
        monkeypatch.setenv("MESHYCHAT_CALL_RETENTION_DAYS", raw)
        assert retention.call_retention_secs() == days * 86400, raw
    monkeypatch.delenv("MESHYCHAT_CALL_RETENTION_DAYS")
    assert retention.call_retention_secs() == 90 * 86400


# ------------------------------------------------------------------ the D14/D17 pair

def _spent_nonce(owner, parent: str, by: str) -> str:
    """A flow nonce spent by `by`, expired an hour ago (well past the sweep's margin)."""
    nonce = f"d14-flow-{uuid.uuid4().hex}"
    owner.execute(
        "INSERT INTO flow_continuation_nonces (nonce, parent_call_id, caller_owner_id, "
        "caller_peer_id, target_owner_id, target_peer_id, expires_at, consumed_at, "
        "consumed_by_call_id) VALUES (%s,%s,%s,%s,%s,%s, now() - interval '1 hour', "
        "now() - interval '61 minutes', %s)",
        (nonce, parent, uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), by))
    return nonce


def _nonces(owner, parent: str) -> set[str]:
    return {r[0] for r in owner.execute(
        "SELECT nonce FROM flow_continuation_nonces WHERE parent_call_id = %s", (parent,))}


def test_d14_removes_the_old_call_so_d17_reclaims_its_spent_nonce(owner, world):
    """The pair. Two spent nonces, each consumed by a call: one call terminal for 91 days,
    one for 10. The flow sweep alone keeps both (each call exists). The call retention then
    removes the 91-day call only, and the next flow sweep reclaims exactly that call's
    nonce. CONTROL: the 10-day call and its nonce are still there after both jobs."""
    parent = f"d14-parent-{uuid.uuid4().hex}"
    old_call = _call(owner, world, "pair-old", status="completed",
                     completed="now() - interval '91 days'",
                     created="now() - interval '91 days'")
    new_call = _call(owner, world, "pair-new", status="completed",
                     completed="now() - interval '10 days'",
                     created="now() - interval '10 days'")
    n_old, n_new = _spent_nonce(owner, parent, old_call), _spent_nonce(owner, parent, new_call)

    _run("flow_nonce_sweep")
    assert _nonces(owner, parent) == {n_old, n_new}, (
        "before the call retention, both consuming calls exist and both nonces are kept")

    _run("call_retention")
    assert _present(owner, [old_call, new_call]) == {new_call}

    _run("flow_nonce_sweep")
    assert _nonces(owner, parent) == {n_new}, (
        "the call retention removed the consuming call; its spent nonce must be reclaimed")


# ------------------------------------------------------------------ read-result prune

@pytest.mark.parametrize("given,kept", [(3, 3), (2.0, 2), (2147483647, 2147483647),
                                         ({"version": "row text"}, None), ([3], None), ("3", None),
                                         (2.5, None), (10 ** 12, None), (0, None), (-1, None),
                                         ("row text " * 50, None), (True, None)])
def test_d14_a_pruned_read_keeps_the_version_it_returned_and_no_text(owner, world, given, kept):
    """GET /v1/mesh/unread (divergence 074) counts a read by the version it RETURNED, so the
    summary that replaces a memory.read's result keeps `memo_version`, and none of the memo's text.
    Only a bounded integer is kept (Codex on #111): an object, array, string, fraction, huge or
    non-positive number under memo.version is dropped, because a pruned row is never pruned again
    and would carry it for good. CONTROL: a memory.query summary has no memo and so no memo_version.
    MUTANTS U12 (the summary drops memo_version) and U17 (it keeps any value)."""
    two_h = "now() - interval '2 hours'"
    read = _call(owner, world, "rv", status="completed", completed=two_h, skill_name="memory.read",
                 result={"op": "memory.read",
                         "memo": {"key": "msg/1-x", "version": given, "value": {"body": "row text"}}})
    query = _call(owner, world, "qv", status="completed", completed=two_h, skill_name="memory.query",
                  result={"op": "memory.query", "results": [{"k": 1}], "ok": True})
    _run("call_read_result_prune")
    tomb = owner.execute("SELECT result FROM skill_calls WHERE call_id = %s", (read,)).fetchone()[0]
    assert tomb["pruned"] is True and tomb.get("memo_version") == kept
    assert type(tomb.get("memo_version")) in (int, type(None))
    assert "memo" not in tomb and "row text" not in json.dumps(tomb)
    qtomb = owner.execute("SELECT result FROM skill_calls WHERE call_id = %s", (query,)).fetchone()[0]
    assert qtomb["pruned"] is True and "memo_version" not in qtomb


def test_d14_a_read_result_is_replaced_by_a_tombstone_an_hour_after_it_ends(owner, world):
    """A memory.query or memory.read call terminal for
    more than an hour keeps its row, but its result becomes an audit summary (`pruned`,
    the op, the original size and signature, the result count), its signature columns and
    its canonical payload are cleared, and its attachments emptied. Kept as they were: the
    same skill 30 minutes after completion, memory.write after two hours (not a read), and
    an old memory.query still `running`. A second tick leaves the tombstone alone."""
    two_h, half_h = "now() - interval '2 hours'", "now() - interval '30 minutes'"
    result = {"op": "memory.query", "results": [{"k": 1}, {"k": 2}, {"k": 3}], "ok": True}
    pruned = {
        "query": _call(owner, world, "rq", status="completed", completed=two_h,
                       skill_name="memory.query", result=result),
        "read": _call(owner, world, "rr", status="failed", completed=two_h,
                      skill_name="memory.read",
                      result={"op": "memory.read", "memo": {"x": 1}}),
    }
    kept = {
        "fresh": _call(owner, world, "rf", status="completed", completed=half_h,
                       skill_name="memory.query", result=result),
        "write": _call(owner, world, "rw", status="completed", completed=two_h,
                       skill_name="memory.write", result={"op": "memory.write"}),
        "running": _call(owner, world, "rn", status="running", completed=None,
                         created=two_h, skill_name="memory.query", result=result),
    }
    before = {k: owner.execute("SELECT result, result_signature, result_payload FROM "
                               "skill_calls WHERE call_id = %s", (v,)).fetchone()
              for k, v in kept.items()}

    out = _run("call_read_result_prune")
    assert out.rows >= 2, out

    row = owner.execute(
        "SELECT result, result_signature, result_signing_kid, result_payload, "
        "result_attachments FROM skill_calls WHERE call_id = %s",
        (pruned["query"],)).fetchone()
    tomb, sig, kid, payload, attachments = row
    assert tomb["pruned"] is True and tomb["ok"] is True
    assert tomb["pruned_reason"] == "read_result_retention"
    assert tomb["skill_name"] == "memory.query" and tomb["op"] == "memory.query"
    assert tomb["result_count"] == 3
    assert tomb["original_result_signature"] == "sig-original"
    assert tomb["original_result_signing_kid"] == "kid-1"
    assert tomb["original_result_bytes"] > 0 and "results" not in tomb
    assert (sig, kid, payload, attachments) == (None, None, None, [])
    read_tomb = owner.execute("SELECT result FROM skill_calls WHERE call_id = %s",
                              (pruned["read"],)).fetchone()[0]
    assert read_tomb["pruned"] is True and "memo" not in read_tomb
    assert "result_count" not in read_tomb, "no results array: the key is stripped, not null"

    for k, v in kept.items():
        now = owner.execute("SELECT result, result_signature, result_payload FROM "
                            "skill_calls WHERE call_id = %s", (v,)).fetchone()
        assert now == before[k], f"{k} must not be pruned"

    stamp = tomb["pruned_at"]
    _run("call_read_result_prune")
    again = owner.execute("SELECT result FROM skill_calls WHERE call_id = %s",
                          (pruned["query"],)).fetchone()[0]
    assert again["pruned_at"] == stamp, "a pruned result is never pruned again"


def test_d14_a_pruned_read_still_answers_as_a_terminal_its_caller_can_read(owner, world):
    """What a poller gets after the prune (apps/calls/terminal.of_row): the row is still
    terminal and `completed`; its payload is the tombstone, marked reconstructed because the
    canonical payload is gone, and it carries no signature so no caller mistakes it for a
    signature over the replacement summary."""
    from apps.calls import reads, terminal
    call_id = _call(owner, world, "rt", status="completed",
                    completed="now() - interval '2 hours'", skill_name="memory.query",
                    result={"op": "memory.query", "results": []})
    _run("call_read_result_prune")
    with dbconn.connect(DSN) as conn:
        found = reads.load_call(conn, call_id)
    t = terminal.of_row(found.row)
    assert t.status == "completed" and t.signature is None
    assert t.payload_reconstructed is True and t.payload["pruned"] is True
    assert t.payload["result_count"] == 0


def test_d14_both_jobs_are_registered_and_bounded():
    names = {j.name for j in registry.JOBS}
    assert "call_deadline_sweep" in names          # control: the registry is the real one
    rows = registry.by_name("call_retention")
    reads = registry.by_name("call_read_result_prune")
    # The row prune keeps its default 5000 batch; the read prune's is 100, not the 1000 it
    # would otherwise have (divergence 046, OI-45).
    assert rows.max_batch == 5000 and reads.max_batch == 100
    assert rows.interval_secs == 900, "the retention sweep interval is 15 minutes"
    assert reads.interval_secs <= 60


def test_d14_the_terminal_statuses_are_the_one_terminal_mappings_not_a_copy():
    """`retention.TERMINAL_STATUSES` was a tuple of the four statuses, a copy of
    `apps/calls/terminal.TERMINAL_STATUSES` (the ONE terminal mapping, divergence 041 S6) that
    test_d14_no_status_but_a_terminal_one_is_ever_removed reads as the rule. It IS the owner's
    set now, so the rule and the mapping cannot drift apart."""
    from apps.calls import retention, terminal
    assert retention.TERMINAL_STATUSES is terminal.TERMINAL_STATUSES
