"""Retention sweeps: every table that grows by a row a request gets a registered job.

STATUS D10 (`dpop_jti_seen`), D11 (`pairing_codes`, `auth_challenges`), D12
(`mesh_route_usage`), D13 (the files janitor), D15 (`owner_sessions`) and D17
(`flow_continuation_nonces`). Each was a table with no scheduled delete, or a delete with no
scheduler (D12's CLI verb, D13's `FileService.sweep_*`).

RULE (i), THE ONE EVERY TEST HERE HOLDS: a sweep is keyed on the row's own EXPIRY or
terminal age, never on a live object's identity and never on a row that is still in force.
So every test plants a row the sweep must delete AND, beside it, the row closest to the
boundary that it must keep (the positive control): an expired row gone says nothing on its
own, because a sweep that deletes everything passes that half too.

ISOLATION. The sweeps are table-wide by design, and other tests in the run leave rows in
the same tables. Every assertion is scoped to this test's own rows, and the bound tests
plant rows older than anything else in the table so the oldest-first order makes each
tick's share of them exact (the pattern of tests/test_jobs.py's bound test). The file
janitor's cap test cannot scope that way, because `more_work_pending` counts the whole
batch, so its ticks run on a connection that sees only its own owner's files
(`_seeing_only_files_of`, issue #164).

Each job runs through `runner.run_due` as the application role (`dbconn.connect`), under a
lease name unique to the test, so a sweep that needed a privilege or an RLS context the
work connection does not have fails here and not on the test server's 30 s loop.
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

from apps.core import dbconn  # noqa: E402
from apps.jobs import registry, runner  # noqa: E402

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")

ANCIENT = "timestamptz '2000-01-01 00:00:00+00'"
#: Two ages older than ANCIENT, for the oldest-first test: no other test plants either.
OLDEST = "timestamptz '1990-01-01 00:00:00+00'"
LATER = "timestamptz '1995-01-01 00:00:00+00'"


@pytest.fixture()
def owner():
    """Role `meshy`: seeding and reading back only, never the connection under test."""
    with psycopg.connect(DSN, autocommit=True) as c:
        yield c


def _connect(**kw):
    return dbconn.connect(DSN, **kw)


def _on_a_heap_scan(body):
    """`body`, on a work connection whose planner may not use an index. An index scan on
    `expires_at` hands rows over oldest first whether or not the sweep says ORDER BY, so a
    bound test planned onto one cannot tell the two apart: measured on the flow sweep, whose
    ORDER BY mutant survived once its predicate planned an index scan. A heap scan hands
    them over in insertion order, which the bound tests plant newest first."""
    def run(conn, job):
        conn.execute("SET enable_indexscan = off")
        conn.execute("SET enable_bitmapscan = off")
        return body(conn, job)
    return run


def _seeing_only_files_of(owner_id, body):
    """`body`, on a work connection whose `file_artifacts` holds only `owner_id`'s rows.

    For a claim about a whole janitor batch (`more_work_pending`), where scoping the
    assertion to the test's own rows is not enough: the sweeps are table-wide, so a row
    another test left that crosses its window during the test joins the batch and moves
    the count (issue #164). A TEMP view of that name, on this session only, shadows the
    table for the janitor's unqualified statements (the temporary schema is searched first
    for relations); its writes go through to the table and commit as before, and the
    statements themselves are unchanged. The check below fails the tick if the name does
    not resolve to the view."""
    def run(conn, job):
        conn.execute(sql.SQL("CREATE TEMP VIEW file_artifacts AS SELECT * FROM "
                             "public.file_artifacts WHERE owner_id = {}::uuid").format(
                                 sql.Literal(str(owner_id))))
        shadowed = conn.execute("SELECT to_regclass('file_artifacts') = "
                                "to_regclass('pg_temp.file_artifacts')").fetchone()[0]
        assert shadowed, "the janitor's file_artifacts is not this test's one-owner view"
        return body(conn, job)
    return run


def _run(name: str, *, max_batch: int | None = None,
         heap_scan: bool = False, only_files_of=None) -> runner.Outcome:
    """Run the REGISTERED job `name` once, under a lease row nobody else uses.

    The body, interval and timeout are the registry's; only the lease name (and, for the
    bound tests, max_batch and the heap scan; for the file janitor's cap test, the view of
    one owner's files) differ, so this exercises exactly what `run_jobs --loop` runs.
    """
    base = registry.by_name(name)
    body = _on_a_heap_scan(base.run) if heap_scan else base.run
    if only_files_of is not None:
        body = _seeing_only_files_of(only_files_of, body)
    job = registry.Job(name=f"t_{name}_{uuid.uuid4().hex[:8]}",
                       interval_secs=base.interval_secs, timeout_secs=base.timeout_secs,
                       jitter_frac=base.jitter_frac,
                       max_batch=base.max_batch if max_batch is None else max_batch,
                       run=body)
    try:
        (out,) = runner.run_due(_connect, jobs=(job,), holder="retention-test")
    finally:
        with psycopg.connect(DSN, autocommit=True) as c:
            c.execute("DELETE FROM background_job_leases WHERE job_name = %s", (job.name,))
    assert out.claimed is True, out
    assert out.result == "ok", out
    return out


def _owner_and_peer(conn, tag: str) -> tuple[uuid.UUID, uuid.UUID]:
    owner_id, peer_id = uuid.uuid4(), uuid.uuid4()
    conn.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id) "
                 "VALUES (%s,%s,'product',NULL)",
                 (owner_id, f"sweep-{tag}-{owner_id.hex[:10]}@example.test"))
    conn.execute("INSERT INTO peers (id, owner_id, name) VALUES (%s,%s,%s)",
                 (peer_id, owner_id, f"sweep-{tag}-{peer_id.hex[:10]}"))
    return owner_id, peer_id


def _col(conn, sql: str, params) -> list:
    return [r[0] for r in conn.execute(sql, params).fetchall()]


# ------------------------------------------------------------------- D10 dpop_jti_seen

def _jti(tag: str) -> bytes:
    return (tag.encode() + uuid.uuid4().bytes)[:16].ljust(16, b"\0")


def test_d10_the_dpop_sweep_deletes_spent_proofs_past_their_window_and_a_margin(owner):
    """`expires_at` is the proof's timestamp plus the 120 s window, judged on the APP
    clock by verify_dpop and swept on the DATABASE clock. A row deleted the moment it
    expires can go while the proof is still acceptable to an app clock a little behind,
    and a replay of it then spends it afresh (the finding divergence 036 recorded for the
    envelope ledger). So the sweep keeps one more window: the row that expired 60 s ago
    must survive, the one an hour ago must go."""
    _, peer = _owner_and_peer(owner, "dpop")
    rows = {"old": "now() - interval '1 hour'", "in_margin": "now() - interval '60 seconds'",
            "nearly": "now() + interval '5 seconds'", "live": "now() + interval '1 hour'"}
    jtis = {}
    for tag, expr in rows.items():
        jtis[tag] = _jti(tag)
        owner.execute(f"INSERT INTO dpop_jti_seen (jti, peer_id, expires_at) "
                      f"VALUES (%s, %s, {expr})", (jtis[tag], peer))

    out = _run("dpop_jti_sweep")

    left = {bytes(j) for j in _col(owner, "SELECT jti FROM dpop_jti_seen WHERE peer_id = %s",
                                   (peer,))}
    assert jtis["old"] not in left, "a proof spent an hour ago is still on the ledger"
    assert out.rows >= 1
    assert left == {jtis["in_margin"], jtis["nearly"], jtis["live"]}, (
        "the sweep deleted a jti inside its window or its margin: a replay of that proof "
        "would now be accepted as a first presentation")


# ---------------------------------------------------- D11 pairing_codes, auth_challenges

def test_d11_the_pairing_code_sweep_keeps_a_day_past_expiry_then_deletes(owner):
    """A code lives 60 s. Deleting it changes what a late presentation is told, from 410
    "expired" or "already used" to 404 "unknown pairing code", so a day past expiry is
    kept. Used or not makes no difference to the
    sweep: the key is expiry (rule i)."""
    owner_id, _ = _owner_and_peer(owner, "pair")
    codes = {
        "old_unused": ("now() - interval '2 days'", None),
        "old_used": ("now() - interval '2 days'", "now() - interval '2 days'"),
        "recent": ("now() - interval '1 hour'", None),
        "recent_used": ("now() - interval '1 hour'", "now() - interval '61 minutes'"),
        "live": ("now() + interval '60 seconds'", None),
    }
    names = {}
    for tag, (exp, used) in codes.items():
        names[tag] = f"sweep-{tag}-{uuid.uuid4().hex}"
        owner.execute(f"INSERT INTO pairing_codes (code, owner_id, expires_at, used_at) "
                      f"VALUES (%s, %s, {exp}, {used or 'NULL'})", (names[tag], owner_id))

    _run("pairing_code_sweep")

    left = set(_col(owner, "SELECT code FROM pairing_codes WHERE owner_id = %s", (owner_id,)))
    assert names["old_unused"] not in left and names["old_used"] not in left, left
    assert left == {names["recent"], names["recent_used"], names["live"]}, (
        "a code inside its day of grace, or a live one, was deleted")


def test_d11_the_challenge_sweep_deletes_expired_challenges_consumed_or_not(owner):
    """The sign-in consume matches `expires_at > NOW()`, so an expired challenge is dead
    whether or not it was consumed; the sweep's predicate is the opportunistic prune's
    (apps/authapi/views.py). A consumed challenge that has not
    expired is kept: the key is expiry, not use."""
    peer = uuid.uuid4()     # no FK to peers, by design (sql/032)
    rows = {
        "expired": ("now() - interval '1 minute'", None),
        "expired_consumed": ("now() - interval '1 minute'", "now() - interval '2 minutes'"),
        "nearly": ("now() + interval '5 seconds'", None),
        "consumed_live": ("now() + interval '1 minute'", "now()"),
    }
    for tag, (exp, consumed) in rows.items():
        owner.execute(f"INSERT INTO auth_challenges (peer_id, nonce, expires_at, consumed_at) "
                      f"VALUES (%s, %s, {exp}, {consumed or 'NULL'})", (peer, tag))

    _run("auth_challenge_sweep")

    left = set(_col(owner, "SELECT nonce FROM auth_challenges WHERE peer_id = %s", (peer,)))
    assert "expired" not in left and "expired_consumed" not in left, left
    assert left == {"nearly", "consumed_live"}, "an unexpired challenge was deleted"


# ------------------------------------------------------------------ D15 owner_sessions

def test_d15_the_session_janitor_deletes_expired_sessions_only(owner):
    """The session janitor deletes expired `owner_sessions` every 5 min.
    Every lookup here already rejects an expired row, so an expired row authenticates nothing;
    the one 5 s from expiry still does and must stay."""
    owner_id, _ = _owner_and_peer(owner, "sess")
    rows = {"expired": "now() - interval '1 minute'",
            "nearly": "now() + interval '5 seconds'",
            "live": "now() + interval '30 days'"}
    for tag, exp in rows.items():
        owner.execute(f"INSERT INTO owner_sessions (owner_id, expires_at, token_hash, "
                      f"user_agent) VALUES (%s, {exp}, %s, %s)",
                      (owner_id, uuid.uuid4().hex, tag))

    _run("owner_session_sweep")

    left = set(_col(owner, "SELECT user_agent FROM owner_sessions WHERE owner_id = %s",
                    (owner_id,)))
    assert "expired" not in left
    assert left == {"nearly", "live"}, "a session still in force was deleted"


# -------------------------------------------------------- D17 flow_continuation_nonces

#: Expired long past the margin a spent nonce with no call row is kept for.
LONG_AGO = "now() - interval '1 hour'"
#: Two ages older than OLDEST, for the batch test below: no other test plants either.
EARLIEST = "timestamptz '1985-01-01 00:00:00+00'"
EARLY = "timestamptz '1986-01-01 00:00:00+00'"


def _call_row(owner, call_id: str) -> uuid.UUID:
    """A `skill_calls` row under this `call_id`. That table is where a receipt comes from
    (apps/calls/views.get_receipt loads the call through reads.load_call, keyed on the
    UNIQUE `skill_calls.call_id`), and `consumed_by_call_id` holds the same key. The consume
    binds the pipeline's `call_id`, which pipeline step 8 uses to insert the call. Returns the
    row's `id`, which is NOT it."""
    owner_id, peer_id = _owner_and_peer(owner, "flowcall")
    skill_id, row_id = uuid.uuid4(), uuid.uuid4()
    owner.execute("INSERT INTO skills (id, peer_id, name, version, mode, share_mode, "
                  "input_schema) VALUES (%s,%s,%s,'1','sync','private','{}')",
                  (skill_id, peer_id, f"sweep.flow{skill_id.hex[:8]}"))
    owner.execute("INSERT INTO skill_calls (id, call_id, caller_peer_id, caller_owner_id, "
                  "target_peer_id, target_owner_id, skill_id, skill_name, skill_version, "
                  "mode, transport, args, deadline_at) VALUES (%s,%s,%s,%s,%s,%s,%s,"
                  "'sweep.flow','1','sync','ws','{}', now() + interval '1 hour')",
                  (row_id, call_id, peer_id, owner_id, peer_id, owner_id, skill_id))
    return row_id


def _flow_nonce(owner, parent: str, tag: str, expires: str, by: str | None = None) -> str:
    """One flow_continuation_nonces row; spent by the call `by` when one is named, a minute
    before it expired (the consume matches `expires_at >= NOW()`)."""
    nonce = f"flowc-{tag}-{uuid.uuid4().hex}"
    consumed = f"({expires}) - interval '1 minute'" if by else "NULL"
    owner.execute(
        f"INSERT INTO flow_continuation_nonces (nonce, parent_call_id, caller_owner_id, "
        f"caller_peer_id, target_owner_id, target_peer_id, expires_at, consumed_at, "
        f"consumed_by_call_id) VALUES (%s,%s,%s,%s,%s,%s,{expires},{consumed},%s)",
        (nonce, parent, uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), by))
    return nonce


def _flow_left(owner, parent: str) -> set[str]:
    return set(_col(owner, "SELECT nonce FROM flow_continuation_nonces "
                           "WHERE parent_call_id = %s", (parent,)))


def test_d17_a_spent_nonce_is_kept_while_its_consuming_call_exists_then_deleted(owner):
    """Operator decision, 2026-09-26. A spent nonce is the only record of its consuming
    call's parent: the receipt reads `parent_call_id` from it (apps/calls/reads.
    receipt_provenance). So it is kept for as long as that call exists, and deleted once
    the call is gone: removed by a call retention, or never written. An unspent nonce keeps
    the rule it had, deleted at expiry with no margin (`expired`, a minute past, goes).

    The key is `skill_calls.call_id`, not `skill_calls.id`: `by_row_id` names a call that
    EXISTS, by its row id, and is deleted. The sweep runs as meshychat_app, under which
    skill_calls is FORCE-RLS and reads empty without server_internal, so a sweep that
    dropped that context would delete `spent_live` here (the positive control)."""
    parent = f"parent-{uuid.uuid4().hex}"
    child = f"child-{uuid.uuid4().hex}"
    _call_row(owner, child)
    other_row_id = _call_row(owner, f"other-{uuid.uuid4().hex}")
    n = {
        "spent_live": _flow_nonce(owner, parent, "live", LONG_AGO, child),
        "spent_never": _flow_nonce(owner, parent, "never", LONG_AGO,
                                   f"never-{uuid.uuid4().hex}"),
        "by_row_id": _flow_nonce(owner, parent, "rowid", LONG_AGO, str(other_row_id)),
        "expired": _flow_nonce(owner, parent, "expired", "now() - interval '1 minute'"),
        "nearly": _flow_nonce(owner, parent, "nearly", "now() + interval '5 seconds'"),
    }

    first = _run("flow_nonce_sweep")

    assert _flow_left(owner, parent) == {n["spent_live"], n["nearly"]}, (
        "kept must be exactly the nonce whose consuming call exists and the unexpired one")
    assert first.rows >= 3, first
    from apps.calls import reads
    assert reads.receipt_provenance(owner, child, None)["parent_call_id"] == parent, (
        "the spent nonce was kept but the receipt no longer finds its parent")

    # The call is removed, as a call retention would remove it; its nonce goes next tick.
    assert owner.execute("DELETE FROM skill_calls WHERE call_id = %s",
                         (child,)).rowcount == 1
    _run("flow_nonce_sweep")
    assert _flow_left(owner, parent) == {n["nearly"]}, (
        "the consuming call is gone and its spent nonce is still stored")


def test_d17_a_spent_nonce_with_no_call_row_is_kept_through_a_margin_past_expiry(owner):
    """The consume and the call row are two commits: the consume commits first, and pipeline
    step 8 inserts the call after it. In
    between, a spent nonce names a call that does not exist YET, so "never existed" can
    only be judged once that request has had time to write its row. A spent nonce whose
    call is absent is therefore kept a margin past its expiry (the consume was at or before
    expiry, so that is at least the margin after the consume): 60 s past, kept; an hour
    past, deleted. An unspent nonce 60 s past expiry goes, as before: it can never be
    consumed, so it has no call to wait for."""
    from apps.calls import nonce_ledger
    parent = f"parent-{uuid.uuid4().hex}"
    inside = "now() - interval '60 seconds'"
    n = {
        "inside": _flow_nonce(owner, parent, "inside", inside, f"pending-{uuid.uuid4().hex}"),
        "past": _flow_nonce(owner, parent, "past", LONG_AGO, f"gone-{uuid.uuid4().hex}"),
        "unspent": _flow_nonce(owner, parent, "unspent", inside),
    }

    _run("flow_nonce_sweep")

    assert _flow_left(owner, parent) == {n["inside"]}, (
        "a spent nonce with no call row goes only past the margin; an unspent one at expiry")
    assert 60 < nonce_ledger.FLOW_SPENT_MARGIN_SECS < 3600, (
        "the planted ages no longer straddle the margin")


# ------------------------------------------------------------------- the bounds (B5)

def _seed_old_dpop(owner, n, at=ANCIENT):
    _, peer = _owner_and_peer(owner, "dpopb")
    for i in range(n):
        owner.execute(f"INSERT INTO dpop_jti_seen (jti, peer_id, expires_at) "
                      f"VALUES (%s, %s, {at})", (_jti(f"b{i}"), peer))
    return lambda: owner.execute("SELECT count(*) FROM dpop_jti_seen WHERE peer_id = %s",
                                 (peer,)).fetchone()[0]


def _seed_old_pairing(owner, n, at=ANCIENT):
    owner_id, _ = _owner_and_peer(owner, "pairb")
    for i in range(n):
        owner.execute(f"INSERT INTO pairing_codes (code, owner_id, expires_at) "
                      f"VALUES (%s, %s, {at})", (f"b{i}-{uuid.uuid4().hex}", owner_id))
    return lambda: owner.execute("SELECT count(*) FROM pairing_codes WHERE owner_id = %s",
                                 (owner_id,)).fetchone()[0]


def _seed_old_challenges(owner, n, at=ANCIENT):
    peer = uuid.uuid4()
    for i in range(n):
        owner.execute(f"INSERT INTO auth_challenges (peer_id, nonce, expires_at) "
                      f"VALUES (%s, %s, {at})", (peer, f"b{i}"))
    return lambda: owner.execute("SELECT count(*) FROM auth_challenges WHERE peer_id = %s",
                                 (peer,)).fetchone()[0]


def _seed_old_sessions(owner, n, at=ANCIENT):
    owner_id, _ = _owner_and_peer(owner, "sessb")
    for _ in range(n):
        owner.execute(f"INSERT INTO owner_sessions (owner_id, expires_at, token_hash) "
                      f"VALUES (%s, {at}, %s)", (owner_id, uuid.uuid4().hex))
    return lambda: owner.execute("SELECT count(*) FROM owner_sessions WHERE owner_id = %s",
                                 (owner_id,)).fetchone()[0]


def _seed_old_flow(owner, n, at=ANCIENT):
    parent = f"parentb-{uuid.uuid4().hex}"
    for i in range(n):
        owner.execute(
            f"INSERT INTO flow_continuation_nonces (nonce, parent_call_id, caller_owner_id, "
            f"caller_peer_id, target_owner_id, target_peer_id, expires_at) "
            f"VALUES (%s,%s,%s,%s,%s,%s,{at})",
            (f"flowc-b{i}-{uuid.uuid4().hex}", parent, uuid.uuid4(), uuid.uuid4(),
             uuid.uuid4(), uuid.uuid4()))
    return lambda: owner.execute(
        "SELECT count(*) FROM flow_continuation_nonces WHERE parent_call_id = %s",
        (parent,)).fetchone()[0]


def _seed_old_flow_spent(owner, n, at=ANCIENT):
    """Spent nonces whose consuming call never existed: the sweep's other deletable kind."""
    parent = f"parentbs-{uuid.uuid4().hex}"
    for i in range(n):
        _flow_nonce(owner, parent, f"bs{i}", at, f"never-{uuid.uuid4().hex}")
    return lambda: owner.execute(
        "SELECT count(*) FROM flow_continuation_nonces WHERE parent_call_id = %s",
        (parent,)).fetchone()[0]


@pytest.mark.parametrize("job,seed", [
    ("dpop_jti_sweep", _seed_old_dpop),
    ("pairing_code_sweep", _seed_old_pairing),
    ("auth_challenge_sweep", _seed_old_challenges),
    ("owner_session_sweep", _seed_old_sessions),
    ("flow_nonce_sweep", _seed_old_flow),
    ("flow_nonce_sweep", _seed_old_flow_spent),
])
def test_each_sweep_deletes_at_most_max_batch_a_tick_oldest_first(owner, job, seed):
    """B5: a backlog drains over ticks, never in one long DELETE, and the oldest rows go
    first (the `ORDER BY expires_at` each sweep's docstring states). Two tiers, both older
    than anything else in the table: three rows expired in 1995 planted FIRST, then two
    expired in 1990, so neither insertion order nor a sweep that ignores age can hand the
    1990 pair to the first tick; only the ordering does. A tick that reached its bound
    says more work is pending (review of f6923d6, finding 3: before, every planted row
    shared one age, and a sweep without its ORDER BY passed). Every tick runs on a heap
    scan (`_on_a_heap_scan`), so an index cannot supply the order in the ORDER BY's place."""
    later = seed(owner, 3, LATER)
    oldest = seed(owner, 2, OLDEST)
    assert (oldest(), later()) == (2, 3)
    first = _run(job, max_batch=2, heap_scan=True)
    assert (first.rows, first.more_work_pending) == (2, True), first
    assert (oldest(), later()) == (0, 3), (
        "the first tick did not take the two oldest rows: the sweep is not oldest first")
    second = _run(job, max_batch=2, heap_scan=True)
    assert (second.rows, later()) == (2, 1), second
    _run(job, max_batch=2, heap_scan=True)
    assert later() == 0, "the backlog did not drain"


def test_d17_kept_spent_nonces_do_not_use_up_a_batch(owner):
    """The bound applies to the rows the sweep may delete, not to every expired row. A
    spent nonce whose call exists is kept for as long as the call is, so if kept rows
    counted toward the LIMIT, a batch's worth of them at the old end of the expiry index
    would stall the sweep for good. Three kept rows are planted older than anything else
    in the table (1985), two deletable ones after them (1986): a tick of two deletes those
    two and says more may be pending."""
    parent = f"parent-{uuid.uuid4().hex}"
    calls = [f"kept{i}-{uuid.uuid4().hex}" for i in range(3)]
    for call_id in calls:
        _call_row(owner, call_id)
    kept = {_flow_nonce(owner, parent, f"k{i}", EARLIEST, c) for i, c in enumerate(calls)}
    for i in range(2):
        _flow_nonce(owner, parent, f"g{i}", EARLY, f"never-{uuid.uuid4().hex}")

    out = _run("flow_nonce_sweep", max_batch=2, heap_scan=True)

    assert (out.rows, out.more_work_pending) == (2, True), out
    assert _flow_left(owner, parent) == kept, (
        "the kept nonces took the batch: the deletable ones behind them are still stored")
    owner.execute("DELETE FROM flow_continuation_nonces WHERE parent_call_id = %s", (parent,))
    owner.execute("DELETE FROM skill_calls WHERE call_id = ANY(%s)", (calls,))


# ------------------------------------------------------------ D12 mesh_route_usage

def test_d12_the_usage_retention_prunes_one_day_a_tick_past_90_days(owner):
    """`apps/mesh/usage.prune` had one caller, a CLI verb nothing scheduled. The job calls
    it, keyed by DAY (the retention stated in sql_meshychat/005: 90 days), and bounds a
    tick to the oldest day in the table, so a first run over a long backlog is many small
    deletes rather than one. Day 90 goes (`day <= today - 90`, the prune's own predicate);
    day 89 stays."""
    from apps.mesh import usage
    _, peer = _owner_and_peer(owner, "usage")
    ages = (3000, 2999, 91, 90, 89, 0)
    for age in ages:
        owner.execute("INSERT INTO mesh_route_usage (route, day, peer_id, outcome, calls) "
                      "VALUES ('inbox', (now() AT TIME ZONE 'UTC')::date - %s, %s, 'ok', 1)",
                      (age, peer))

    def mine() -> set[int]:
        return set(_col(owner, "SELECT (now() AT TIME ZONE 'UTC')::date - day "
                               "FROM mesh_route_usage WHERE peer_id = %s", (peer,)))

    first = _run("mesh_route_usage_retention")
    assert mine() == set(ages) - {3000}, "a tick must delete the oldest day and only it"
    assert first.more_work_pending is True, first
    for _ in range(4000):
        out = _run("mesh_route_usage_retention")
        if not out.more_work_pending:
            break
    assert mine() == {89, 0}, (
        f"retention is {usage.RETENTION_DAYS} days: day 90 and older go, day 89 stays")
    assert _run("mesh_route_usage_retention").rows == 0


# ------------------------------------------------------------------ D13 files janitor

@pytest.fixture()
def blob_store(tmp_path, monkeypatch):
    """An fs blob store installed the way apps/files/apps.py installs it at startup."""
    from apps.files import blob as blobmod
    from apps.files import config as cfgmod
    blob_dir = tmp_path / "blobs"
    blob_dir.mkdir()
    monkeypatch.setenv("MESHYCHAT_BLOB_BACKEND", "fs")
    monkeypatch.setenv("MESHYCHAT_BLOB_DIR", str(blob_dir))
    monkeypatch.setenv("MESHYCHAT_BLOB_TOKEN_SECRET", "00" * 32)
    monkeypatch.setenv("MESHYCHAT_PUBLIC_BASE_URL", "https://meshy.test")
    monkeypatch.delenv("MESHYCHAT_UPLOAD_WINDOW_SECS", raising=False)
    monkeypatch.setattr(blobmod, "_ACTIVE", blobmod._ACTIVE)
    blobmod.activate(cfgmod.build_blob_config())
    return blobmod.active()[1]


def _artifact(owner, store, owner_id, *, status, created="now()", grace="NULL") -> str:
    fid = uuid.uuid4()
    key = f"{owner_id}/{fid}"
    owner.execute(f"INSERT INTO file_artifacts (id, owner_id, r2_key, content_type, status, "
                  f"created_at, grace_until) VALUES (%s,%s,%s,'text/plain',%s,{created},{grace})",
                  (fid, owner_id, key, status))
    store.put_bytes(key, b"abc")
    return str(fid)


def test_d13_the_files_janitor_is_scheduled_and_honours_both_windows(owner, blob_store):
    """The two `FileService.sweep_*` methods must have a caller: `file_janitor` runs both every
    5 s. Pending past the upload window becomes `failed` and loses its object; ready past its
    grace is deleted with its object; each has a control on
    the near side of its window. The two controls are removed after: left behind, each
    would cross its window an hour later, inside whatever test ran a janitor then
    (issue #164)."""
    owner_id, _ = _owner_and_peer(owner, "files")
    try:
        stale = _artifact(owner, blob_store, owner_id, status="pending",
                          created="now() - interval '2 hours'")
        fresh = _artifact(owner, blob_store, owner_id, status="pending")
        graced = _artifact(owner, blob_store, owner_id, status="ready",
                           grace="now() - interval '1 minute'")
        held = _artifact(owner, blob_store, owner_id, status="ready",
                         grace="now() + interval '1 hour'")

        out = _run("file_janitor")

        status = dict(owner.execute("SELECT id::text, status FROM file_artifacts "
                                    "WHERE owner_id = %s", (owner_id,)).fetchall())
        assert status.get(stale) == "failed" and graced not in status, status
        assert status.get(fresh) == "pending" and status.get(held) == "ready", (
            "a pending upload inside its window, or a ready file inside its grace, was swept")
        assert out.rows >= 2
        blob_dir = Path(os.environ["MESHYCHAT_BLOB_DIR"])
        on_disk = {f: (blob_dir / str(owner_id) / f).exists()
                   for f in (stale, fresh, graced, held)}
        assert on_disk == {stale: False, fresh: True, graced: False, held: True}, on_disk
    finally:
        owner.execute("DELETE FROM file_artifacts WHERE owner_id = %s", (owner_id,))


def _drain_file_janitor() -> None:
    """Run the janitor until it reports nothing more pending, so the table holds no row it
    would sweep (other tests leave stale artifacts; the sweep is table-wide)."""
    for _ in range(100):
        if not _run("file_janitor").more_work_pending:
            return
    raise AssertionError("the file janitor never caught up")


def test_d13_without_a_blob_backend_the_janitor_sweeps_nothing_and_says_why(
        owner, blob_store, monkeypatch, caplog):
    """A server started without MESHYCHAT_BLOB_BACKEND serves no file route, so it creates no
    artifact; a janitor that failed every tick there would be the fresh-install runbook's
    `run_jobs` printing FAIL forever. It must not mark a row it cannot delete the object
    of, either: failing a pending row without deleting its blob orphans the blob.

    But the jobs process can lack the backend while the web process has it (their
    environments differ), and then eligible rows pile up with every tick `ok rows=0`
    (review of f6923d6, finding 4). So when rows ARE past their window and there is no
    backend, the tick says so in a warning, counts only. Control: the same no-backend
    tick over a table with nothing eligible is silent.

    The sweep is table-wide, and the no-backend count uses the upload window from the
    ENVIRONMENT while the drain sweeps with the blob_store fixture's configured window, so a
    row another test left can be past one and inside the other: the drain cannot clear it
    and a no-backend tick rightly counts it (the r13 gate, 2026-09-26: tests/test_files*.py
    leave one). So this test compares two no-backend ticks around ONE planted row: the
    warning's count rises by exactly one, and the first tick is silent whenever nothing was
    eligible (always, when this test runs alone)."""
    import logging
    import re
    from apps.files import blob as blobmod
    _drain_file_janitor()
    monkeypatch.setattr(blobmod, "_ACTIVE", None)

    def warned_count(said):
        """The row count a no-backend tick's warning names; 0 when it said nothing."""
        if not said:
            return 0
        assert len(said) == 1 and "no blob backend" in said[0], said
        m = re.search(r"and (\d+)\+? file row\(s\) are past their window", said[0])
        assert m, said
        n = int(m.group(1))
        assert n >= 1, ("a no-backend warning over nothing eligible is noise", said)
        return n

    with caplog.at_level(logging.WARNING, logger="apps.jobs.sweeps"):
        quiet = _run("file_janitor")
    said = [r.getMessage() for r in caplog.records if r.name == "apps.jobs.sweeps"]
    assert (quiet.rows, quiet.more_work_pending) == (0, False), (quiet, said)
    before = warned_count(said)

    owner_id, _ = _owner_and_peer(owner, "files0")
    fid = uuid.uuid4()
    owner.execute("INSERT INTO file_artifacts (id, owner_id, r2_key, content_type, status, "
                  "created_at) VALUES (%s,%s,%s,'text/plain','pending', "
                  "now() - interval '2 hours')", (fid, owner_id, f"{owner_id}/{fid}"))
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="apps.jobs.sweeps"):
        out = _run("file_janitor")

    assert out.rows == 0 and out.more_work_pending is False, out
    assert owner.execute("SELECT status FROM file_artifacts WHERE id = %s",
                         (fid,)).fetchone()[0] == "pending"
    said = [r.getMessage() for r in caplog.records if r.name == "apps.jobs.sweeps"]
    assert len(said) == 1 and warned_count(said) == before + 1, (before, said)
    assert str(fid) not in said[0] and str(owner_id) not in said[0], "counts only"
    owner.execute("DELETE FROM file_artifacts WHERE id = %s", (fid,))


@pytest.mark.parametrize("status", ["pending", "ready"])
def test_d13_a_full_janitor_batch_says_more_is_pending(owner, blob_store, status):
    """`more_work_pending` at the repository statements' cap (review of f6923d6, finding
    2: a janitor that always said False passed every test). Planted without objects: the
    fs delete of a missing object is success (spec 11.1). Control: one row under the cap
    is swept whole and says nothing is pending.

    ISOLATED, NOT DRAINED (issue #164). This used to drain the table-wide janitor first,
    and a row another test left, still inside its window at the drain, crossed it before
    the control's tick: the control swept 100 and said more was pending, `(100, True, 0)`
    in a gate. Now every tick sees only this test's owner's files (`_seeing_only_files_of`),
    and a row of a second owner, past its window from the start, shows the isolation holds:
    no tick sweeps it."""
    from apps.jobs import sweeps
    owner_id, _ = _owner_and_peer(owner, f"filecap{status[0]}")
    stranger, _ = _owner_and_peer(owner, f"filecap-other{status[0]}")
    when = (("now() - interval '2 hours'", "NULL") if status == "pending"
            else ("now()", "now() - interval '1 minute'"))

    def plant(who, n: int) -> None:
        owner.execute(
            f"INSERT INTO file_artifacts (id, owner_id, r2_key, content_type, status, "
            f"created_at, grace_until) SELECT gen_random_uuid(), %s, %s || g::text, "
            f"'text/plain', %s, {when[0]}, {when[1]} FROM generate_series(1, %s) g",
            (who, f"{who}/{uuid.uuid4().hex}-", status, n))

    def left(who=owner_id) -> int:
        return owner.execute("SELECT count(*) FROM file_artifacts WHERE owner_id = %s "
                             "AND status = %s", (who, status)).fetchone()[0]

    def tick() -> runner.Outcome:
        out = _run("file_janitor", only_files_of=owner_id)
        assert left(stranger) == 1, (
            "a tick swept another owner's file: the one-owner view did not hold", out)
        return out

    try:
        plant(stranger, 1)
        plant(owner_id, sweeps.FILE_SWEEP_CAP - 1)
        under = tick()
        assert (under.rows, under.more_work_pending, left()) == (
            sweeps.FILE_SWEEP_CAP - 1, False, 0), under
        plant(owner_id, sweeps.FILE_SWEEP_CAP)
        full = tick()
        assert (full.rows, left()) == (sweeps.FILE_SWEEP_CAP, 0), full
        assert full.more_work_pending is True, (
            "a tick that swept a full batch of 100 said nothing more is pending")
    finally:
        owner.execute("DELETE FROM file_artifacts WHERE owner_id IN (%s, %s)",
                      (owner_id, stranger))


# ------------------------------------------------------- every sweep, registered once

def test_every_retention_sweep_is_registered_and_bounded():
    names = {j.name for j in registry.JOBS}
    wanted = {"dpop_jti_sweep", "pairing_code_sweep", "auth_challenge_sweep",
              "owner_session_sweep", "flow_nonce_sweep", "file_janitor",
              "mesh_route_usage_retention"}
    assert "envelope_nonce_sweep" in names       # control: the registry is the real one
    assert wanted <= names, sorted(wanted - names)
    assert len([j.name for j in registry.JOBS]) == len(names), "a job name is registered twice"
    for name in wanted:
        assert registry.by_name(name).max_batch > 0


def test_d13_the_janitor_reads_more_pending_at_the_repository_statements_own_cap():
    """`more_work_pending` for the file janitor is `swept == FILE_SWEEP_CAP`. The cap is a
    literal inside the two repository statements, so a change there without a change here
    would make a full batch read as caught up; this holds the two equal."""
    import inspect
    import re
    from apps.files import repository as repo
    from apps.jobs import sweeps
    for fn in (repo.sweep_pending_past_window, repo.sweep_ready_past_grace):
        limits = re.findall(r"LIMIT (\d+)", inspect.getsource(fn))
        assert limits == [str(sweeps.FILE_SWEEP_CAP)], (fn.__name__, limits)
