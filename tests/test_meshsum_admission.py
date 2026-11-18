"""Admission for summaries: one chokepoint in two stages, counted in Postgres.

Divergence 037, design 5.3 and 6.2 (critique C4, C12, C15, C20, C21).

  * REQUEST admission runs before anything reaches HQB (the tokenizer included) and
    inserts the request's ledger row; a refused request writes NO row, so a peer past its
    limit cannot grow the table.
  * MODEL admission runs after a cache miss: the fleet in-flight cap and the model-call
    budgets per peer (hourly and five-minute), per owner (the bootstrap owner excepted)
    and fleet-wide. It marks the row `calling` with a lease.
  * A call the client abandoned keeps counting until its LEASE passes, because HQB keeps
    generating; the cap counts HQB's work, not the client's patience.
  * Both stages take a transaction-scoped advisory lock in the TWO-integer form, so two
    requests racing for the last slot cannot both get it.
  * The ledger is server_internal only (RLS ENABLE and FORCE, every command bound), holds
    no message text, key or thread id, and the retention prune is the only DELETE.

Run:  pytest tests/test_meshsum_admission.py -q
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import sys
import threading
import uuid
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

from apps.core import dbconn, rls  # noqa: E402
from apps.meshsum import admission as A  # noqa: E402
from tests.fixtures import meshsem_db as db  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")
VERSION = "sum-v1+test00000000"


@pytest.fixture()
def owner():
    with psycopg.connect(DSN, autocommit=True) as c:
        yield c


@pytest.fixture()
def app():
    c = dbconn.connect(DSN)
    try:
        yield c
    finally:
        c.close()


def _limits(**kw):
    return {**A.LIMITS, **kw}


def _release(owner) -> None:
    """End every live lease in this run's database: the in-flight cap is FLEET-wide, so a
    test that measures it starts from none in flight, whatever an earlier test left."""
    with owner.cursor() as cur:
        cur.execute("UPDATE mesh_summary_requests SET state = 'done' "
                    "WHERE state = 'calling'")


def _rows(owner, peer_id) -> list[dict]:
    with owner.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("SELECT * FROM mesh_summary_requests WHERE peer_id = %s "
                    "ORDER BY requested_at", (peer_id,))
        return cur.fetchall()


def _admit(app, p, **kw):
    return A.admit_request(app, peer_id=p["peer_id"], scope_kind="thread",
                           summary_version=VERSION, **kw)


def _call(app, rid, p, **kw):
    return A.admit_model_call(app, rid, peer_id=p["peer_id"], owner_id=p["owner_id"], **kw)


# ------------------------------------------------------------------ the design's values

def test_the_limits_are_the_designs_table():
    assert A.LIMITS == {"inflight": 2, "peer_calls_per_hour": 12,
                        "peer_calls_per_5min": 3, "owner_calls_per_hour": 30,
                        "fleet_calls_per_hour": 120, "peer_requests_per_hour": 60}
    assert A.LEASE_S == 60


# ------------------------------------------------------------------ request admission

def test_request_admission_writes_a_row_and_refuses_past_the_budget_without_one(owner, app):
    p = db.peer(owner, "req")
    limits = _limits(peer_requests_per_hour=2)
    a = _admit(app, p, limits=limits)
    b = _admit(app, p, limits=limits)
    assert a.request_id != b.request_id and a.owner_id == p["owner_id"]
    with pytest.raises(A.Refused) as exc:
        _admit(app, p, limits=limits)
    r = exc.value
    assert (r.status, r.code, r.window) == (429, "E_RATE_LIMITED", "per hour")
    assert 0 < r.retry_after <= 3600
    rows = _rows(owner, p["peer_id"])
    assert len(rows) == 2, "a refused request wrote a row"
    assert {x["state"] for x in rows} == {"admitted"}
    assert all(x["summary_version"] == VERSION and x["scope_kind"] == "thread" for x in rows)
    # Control: another peer's budget is its own.
    q = db.peer(owner, "req-other")
    _admit(app, q, limits=limits)


def test_requests_older_than_an_hour_do_not_count(owner, app):
    p = db.peer(owner, "old")
    limits = _limits(peer_requests_per_hour=1)
    rid = _admit(app, p, limits=limits).request_id
    with owner.cursor() as cur:
        cur.execute("UPDATE mesh_summary_requests SET requested_at = now() - "
                    "interval '61 minutes' WHERE id = %s", (rid,))
    _admit(app, p, limits=limits)


# -------------------------------------------------------------------- model admission

def test_the_fleet_in_flight_cap_answers_busy_and_a_passed_lease_frees_it(owner, app):
    _release(owner)
    ps = [db.peer(owner, f"fly{i}") for i in range(3)]
    rids = [_admit(app, p).request_id for p in ps]
    limits = _limits(inflight=2)
    _call(app, rids[0], ps[0], limits=limits)
    _call(app, rids[1], ps[1], limits=limits)
    with pytest.raises(A.Refused) as exc:
        _call(app, rids[2], ps[2], limits=limits)
    assert (exc.value.status, exc.value.code, exc.value.outcome) == (503, "E_SUMMARY_BUSY",
                                                                     "busy")
    assert exc.value.retry_after >= 1
    assert _rows(owner, ps[2]["peer_id"])[0]["state"] == "done"
    assert _rows(owner, ps[2]["peer_id"])[0]["outcome"] == "busy"
    with owner.cursor() as cur:
        cur.execute("UPDATE mesh_summary_requests SET lease_until = now() - interval '1 s' "
                    "WHERE id = %s", (rids[0],))
    rid = _admit(app, ps[2]).request_id
    _call(app, rid, ps[2], limits=limits)
    row = [x for x in _rows(owner, ps[2]["peer_id"]) if x["id"] == rid][0]
    assert row["state"] == "calling" and row["called_at"] is not None
    utc = _dt.timezone.utc    # same-zone datetimes compare as wall-clock times
    assert row["lease_until"].astimezone(utc) > row["called_at"].astimezone(utc)
    _release(owner)


def test_an_abandoned_call_counts_until_its_lease_passes(owner, app):
    """C15: a call the client gave up on at its budget keeps HQB busy to max_tokens, so
    settling it `unavailable` keeps the row `calling` under its lease."""
    _release(owner)
    p, q = db.peer(owner, "abandon"), db.peer(owner, "abandon-2")
    limits = _limits(inflight=1)
    rid = _admit(app, p).request_id
    _call(app, rid, p, limits=limits)
    A.settle(app, rid, outcome="unavailable", keep_lease=True)
    row = _rows(owner, p["peer_id"])[0]
    assert row["state"] == "calling" and row["outcome"] == "unavailable"
    with pytest.raises(A.Refused):
        _call(app, _admit(app, q).request_id, q, limits=limits)
    # Control: a call that FINISHED is done and frees its slot at once.
    A.settle(app, rid, outcome="generated")
    assert _rows(owner, p["peer_id"])[0]["state"] == "done"
    _call(app, _admit(app, q).request_id, q, limits=limits)
    _release(owner)


def test_per_peer_model_budgets_hourly_and_five_minute(owner, app):
    _release(owner)
    p = db.peer(owner, "peerbudget")
    limits = _limits(inflight=100, peer_calls_per_5min=2, peer_calls_per_hour=3)
    for _ in range(2):
        rid = _admit(app, p).request_id
        _call(app, rid, p, limits=limits)
        A.settle(app, rid, outcome="generated")
    rid = _admit(app, p).request_id
    with pytest.raises(A.Refused) as exc:
        _call(app, rid, p, limits=limits)
    assert (exc.value.status, exc.value.window, exc.value.outcome) == (
        429, "per 5 minutes", "rate_limited")
    with owner.cursor() as cur:
        cur.execute("UPDATE mesh_summary_requests SET called_at = called_at - "
                    "interval '6 minutes', requested_at = requested_at - interval '6 minutes'"
                    " WHERE peer_id = %s AND called_at IS NOT NULL", (p["peer_id"],))
    rid = _admit(app, p).request_id
    _call(app, rid, p, limits=limits)
    A.settle(app, rid, outcome="generated")
    rid = _admit(app, p).request_id
    with pytest.raises(A.Refused) as exc:
        _call(app, rid, p, limits=limits)
    assert exc.value.window == "per hour" and exc.value.status == 429


def test_the_owner_budget_binds_except_for_the_bootstrap_owner(owner, app, monkeypatch):
    """C12: one owner holds 10 of the test server's 11 peers; a flat per-owner limit would
    be the whole server's budget. The owner limit exists against an owner minting nodes to
    multiply a per-peer budget; the operator's own fleet is not that case."""
    _release(owner)
    a = db.peer(owner, "own-a")
    with owner.cursor() as cur:        # a second peer under the SAME owner
        b_id = uuid.uuid4()
        cur.execute("INSERT INTO peers (id, owner_id, name, display_name) VALUES "
                    "(%s,%s,%s,%s)", (b_id, a["owner_id"], f"own-b-{b_id.hex[:8]}", "b"))
    b = {**a, "peer_id": b_id}
    limits = _limits(inflight=100, owner_calls_per_hour=2)
    for p in (a, b):
        rid = _admit(app, p).request_id
        _call(app, rid, p, limits=limits)
        A.settle(app, rid, outcome="generated")
    rid = _admit(app, b).request_id
    with pytest.raises(A.Refused) as exc:
        _call(app, rid, b, limits=limits)
    assert exc.value.window == "per hour" and "owner" in exc.value.message
    # The same owner as the bootstrap authority's: excepted.
    monkeypatch.setattr(A, "_bootstrap_authority_peer_id", lambda: str(a["peer_id"]))
    rid = _admit(app, b).request_id
    _call(app, rid, b, limits=limits)
    A.settle(app, rid, outcome="generated")


def test_the_fleet_hourly_budget_is_a_busy_not_a_rate_limit(owner, app):
    _release(owner)
    p = db.peer(owner, "fleet")
    with owner.cursor() as cur:
        cur.execute("SELECT count(*) FROM mesh_summary_requests "
                    "WHERE called_at > now() - interval '1 hour'")
        already = cur.fetchone()[0]
    limits = _limits(inflight=100, fleet_calls_per_hour=already + 1)
    rid = _admit(app, p).request_id
    _call(app, rid, p, limits=limits)
    A.settle(app, rid, outcome="generated")
    rid = _admit(app, p).request_id
    with pytest.raises(A.Refused) as exc:
        _call(app, rid, p, limits=limits)
    assert (exc.value.status, exc.value.code) == (503, "E_SUMMARY_BUSY")


def test_two_racing_requests_cannot_both_take_the_last_slot(owner):
    """The advisory lock: 8 threads, each on its own connection, race for a cap of 1."""
    _release(owner)
    ps = [db.peer(owner, f"race{i}") for i in range(8)]
    rids = []
    with dbconn.serving_connection(DSN) as c:
        for p in ps:
            rids.append(_admit(c, p).request_id)
    results: list = []
    barrier = threading.Barrier(len(ps))

    def go(p, rid):
        with dbconn.serving_connection(DSN) as c:
            barrier.wait()
            try:
                _call(c, rid, p, limits=_limits(inflight=1))
                results.append("admitted")
            except A.Refused as exc:
                results.append(exc.outcome)

    threads = [threading.Thread(target=go, args=(p, r)) for p, r in zip(ps, rids)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert results.count("admitted") == 1, results
    _release(owner)


def test_the_lock_is_the_two_integer_form():
    """C21: the single-bigint key space is `hashtext(call_id)`'s (apps/calls/pipeline.py)
    and bootstrap's; the two-integer form cannot collide with either."""
    src = (ROOT / "apps" / "meshsum" / "admission.py").read_text(encoding="utf-8")
    calls = re.findall(r"pg_advisory_xact_lock\(([^)]*)\)", src)
    assert calls and all(c.count(",") == 1 for c in calls), calls


# ------------------------------------------------------------------------ settling

def test_settle_records_counts_and_never_text(owner, app):
    p = db.peer(owner, "settle")
    rid = _admit(app, p).request_id
    assert A.settle(app, rid, outcome="generated", covered_digest="sha256:" + "0" * 64,
                    covered_count=5, excluded={"embed_forbidden": 1, "no_text": 2,
                                               "cut_to_fit": 0, "source_changed": 1},
                    sentences_kept=3, sentences_dropped=1,
                    drop_reasons={"addresses_reader": 1}, model_pin="sha256:x",
                    provider_id="chat-fake", latency_ms=12, prompt_tokens=100,
                    completion_tokens=20, finish_reason="stop") is True
    row = _rows(owner, p["peer_id"])[0]
    assert row["state"] == "done" and row["outcome"] == "generated"
    assert row["excluded_no_text"] == 2 and row["excluded_source_changed"] == 1
    assert row["drop_reasons"] == {"addresses_reader": 1}
    with owner.cursor() as cur:
        cur.execute("SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'mesh_summary_requests'")
        cols = {r[0] for r in cur.fetchall()}
    assert not cols & {"key", "keys", "thread_id", "body", "text", "summary", "sentences"}
    assert "covered_digest" in cols, "control: the census reads real columns"


def test_an_unknown_outcome_is_refused_by_the_database(owner, app):
    p = db.peer(owner, "badoutcome")
    rid = _admit(app, p).request_id
    assert A.settle(app, rid, outcome="disabled") is False, \
        "a disabled summary is refused before admission and has no outcome here"


# ------------------------------------------------------------------------ the table

def test_the_ledger_is_server_internal_only(owner, app):
    p = db.peer(owner, "rls")
    rid = _admit(app, p).request_id
    with app.cursor() as cur:
        cur.execute("SELECT count(*) FROM mesh_summary_requests WHERE id = %s", (rid,))
        assert cur.fetchone()[0] == 0, "visible with no context"
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("INSERT INTO mesh_summary_requests (peer_id, owner_id, scope_kind,"
                        " summary_version) VALUES (%s, %s, 'thread', 'x')",
                        (p["peer_id"], p["owner_id"]))
    with rls.server_internal(app):
        with app.cursor() as cur:
            cur.execute("SELECT count(*) FROM mesh_summary_requests WHERE id = %s", (rid,))
            assert cur.fetchone()[0] == 1, "control: server_internal sees it"
    with owner.cursor() as cur:
        cur.execute("SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = 'mesh_summary_requests'")
        assert cur.fetchone() == (True, True)
        cur.execute("SELECT cmd, qual, with_check FROM pg_policies "
                    "WHERE tablename = 'mesh_summary_requests'")
        pols = cur.fetchall()
    assert sorted(c for c, _q, _w in pols) == ["DELETE", "INSERT", "SELECT", "UPDATE"]
    for cmd, qual, check in pols:
        for expr in (qual, check):
            if expr is not None:
                assert "app.server_internal" in expr, (cmd, expr)


def test_the_retention_prune_is_the_only_delete_on_the_ledger():
    hits = []
    for path in (ROOT / "apps" / "meshsum").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"DELETE\s+FROM\s+mesh_summary_requests", text, re.I):
            hits.append(path.name)
    assert hits == ["admission.py"], hits
    src = (ROOT / "apps" / "meshsum" / "admission.py").read_text(encoding="utf-8")
    body = src[src.index("def prune("):]
    body = body[:body.index("\ndef ", 1)]
    assert "DELETE FROM mesh_summary_requests" in body, "the DELETE is prune's"


def test_prune_deletes_by_age_only_and_in_bounded_batches(owner, app):
    p = db.peer(owner, "prune")
    old = [_admit(app, p).request_id for _ in range(3)]
    keep = _admit(app, p).request_id
    with owner.cursor() as cur:
        cur.execute("UPDATE mesh_summary_requests SET requested_at = now() - "
                    "interval '31 days' WHERE id = ANY(%s)", (old,))
    assert A.prune(app, keep_days=30, limit=2) == 2
    assert A.prune(app, keep_days=30, limit=2) >= 1
    left = {r["id"] for r in _rows(owner, p["peer_id"])}
    assert left == {keep}


def test_the_retention_job_is_registered():
    from apps.jobs import registry
    job = registry.by_name("mesh_summary_ledger_retention")
    assert job.max_batch > 0 and job.run is A.run_retention_job


@pytest.mark.parametrize("query,index", [
    ("SELECT count(*) FROM mesh_summary_requests WHERE peer_id = %(p)s "
     "AND requested_at > now() - interval '1 hour'", "mesh_summary_requests_peer_idx"),
    ("SELECT count(*) FROM mesh_summary_requests WHERE owner_id = %(p)s "
     "AND requested_at > now() - interval '61 minutes'", "mesh_summary_requests_owner_idx"),
    ("SELECT count(*) FROM mesh_summary_requests WHERE state = 'calling' "
     "AND lease_until > now()", "mesh_summary_requests_calling_idx"),
    ("SELECT count(*) FROM mesh_summary_requests WHERE called_at IS NOT NULL "
     "AND called_at > now() - interval '1 hour'", "mesh_summary_requests_called_idx"),
    ("SELECT id FROM mesh_summary_requests WHERE requested_at < now() - interval '30 days'"
     " LIMIT 10", "mesh_summary_requests_at_idx"),
])
def test_every_admission_count_is_an_index_range(owner, query, index):
    """B5: with sequential scans priced out, each count the admission and retention run
    is served by its own index, so none of them is a per-request full-table aggregate."""
    with owner.transaction():
        with owner.cursor() as cur:
            cur.execute("SET LOCAL enable_seqscan = off")
            cur.execute("EXPLAIN " + query, {"p": str(uuid.uuid4())})
            plan = "\n".join(r[0] for r in cur.fetchall())
    assert index in plan, plan
