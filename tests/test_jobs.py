"""The background-job scheduler: leases, jitter, and the first real job.

WHAT THIS CLOSES. The scheduler's first real job is the nonce janitor:
`sweep_expired_nonces` had existed with ZERO callers (the graph reports in=0, measured
2026-09-21 against controls of 119 and 28 on neighbouring symbols), and sql/049 already ships
the index for it, describing a "TASK-33 #3 janitor" that was never built.

ISOLATION. Every test that touches the lease table uses a job name unique to the run,
because the dev database is shared with other checkouts and a fixed name would make two
suites fight over one row and fail in a way neither of them names.

House rules: C1 (every negative carries a positive control in the same test), C2/C3 (the
gates here are mutation-proved in the commit that adds them), B5 (a job with no declared
cost bound is refused), E7 (jitter is mandatory, background work never shares the
request pool).
"""
from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from apps.calls import nonce_ledger                       # noqa: E402
from apps.jobs import leases, registry, runner, schedule   # noqa: E402

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")


@pytest.fixture()
def conn():
    with psycopg.connect(DSN, autocommit=True) as c:
        yield c


@pytest.fixture()
def job_name(conn):
    """A lease row nobody else can collide with, removed afterwards."""
    name = "test_job_" + uuid.uuid4().hex[:12]
    yield name
    with conn.cursor() as cur:
        cur.execute("DELETE FROM background_job_leases WHERE job_name = %s", (name,))


def _connect_factory():
    """What run_due takes instead of a connection. See E7 in apps/jobs/runner.py."""
    def connect(**kwargs):
        return psycopg.connect(DSN, autocommit=True)
    return connect


def _seed_peer(conn):
    """An owner plus a peer, matching tests/test_peers.py::_seed_peer.

    The owner FK targets dashboard_users, and a product-plane user must carry a NULL
    trust_group_id per the CHECK constraint.
    """
    owner_id, peer_id = uuid.uuid4(), uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id) "
                    "VALUES (%s,%s,'product',NULL)",
                    (owner_id, "jobs-" + owner_id.hex[:8] + "@example.test"))
        cur.execute("INSERT INTO peers (id, owner_id, name, display_name) "
                    "VALUES (%s,%s,%s,%s)",
                    (peer_id, owner_id, "jobs-" + peer_id.hex[:8], "jobs test"))
    return owner_id, peer_id


# ------------------------------------------------------------------- the lease

def test_only_one_of_two_racing_holders_gets_the_job(conn, job_name):
    """The primary key IS the mutual exclusion. Both claims are legal; one must lose."""
    leases.register(conn, job_name)
    first = leases.claim(conn, job_name, "worker-a", lease_secs=60)
    second = leases.claim(conn, job_name, "worker-b", lease_secs=60)

    assert first.acquired is True, "positive control: the first claim must succeed"
    assert second.acquired is False, "a live lease must not be claimable by anyone else"
    assert leases.read_state(conn, job_name)["holder"] == "worker-a"


def test_an_expired_lease_is_reclaimable_and_names_the_dead_holder(conn, job_name):
    """Crash recovery is the claim condition itself. The takeover must not be silent."""
    leases.register(conn, job_name)
    assert leases.claim(conn, job_name, "crashed", lease_secs=1).acquired is True

    # CONTROL, before expiry: the lease is live and must NOT be claimable.
    assert leases.claim(conn, job_name, "rescuer", lease_secs=60).acquired is False

    time.sleep(1.2)  # the lease expires on the database clock, not a Python one

    taken = leases.claim(conn, job_name, "rescuer", lease_secs=60)
    assert taken.acquired is True
    assert taken.took_over_from == "crashed", (
        "a takeover must report whose lease it displaced; a job taken over on every "
        "tick is a job crashing on every tick and must not read as healthy scheduling"
    )


def test_a_first_claim_reports_no_takeover(conn, job_name):
    """The other direction: took_over_from must not fire on a clean first claim."""
    leases.register(conn, job_name)
    claim = leases.claim(conn, job_name, "worker-a", lease_secs=60)
    assert claim.acquired is True
    assert claim.took_over_from is None


def test_heartbeat_is_scoped_to_the_holder(conn, job_name):
    leases.register(conn, job_name)
    leases.claim(conn, job_name, "mine", lease_secs=60)
    assert leases.heartbeat(conn, job_name, "mine", 60) is True          # control
    assert leases.heartbeat(conn, job_name, "not-mine", 60) is False, (
        "a displaced worker must not be able to extend a lease someone else now owns"
    )


def test_release_is_holder_scoped_and_schedules_forward(conn, job_name):
    leases.register(conn, job_name)
    leases.claim(conn, job_name, "mine", lease_secs=60)

    assert leases.release(conn, job_name, "not-mine", delay_secs=60, result="ok") is False
    assert leases.release(conn, job_name, "mine", delay_secs=60, result="ok") is True

    state = leases.read_state(conn, job_name)
    assert state["holder"] is None and state["leased_until"] is None
    assert state["last_result"] == "ok"
    assert leases.claim(conn, job_name, "anyone", lease_secs=60).acquired is False, (
        "a released job must not be immediately due again, or one worker loops on it"
    )


def test_failure_counts_up_and_success_resets_it(conn, job_name):
    leases.register(conn, job_name)
    for _ in range(2):
        leases.claim(conn, job_name, "w", lease_secs=60)
        leases.release(conn, job_name, "w", delay_secs=0, result="error", error="boom")
    assert leases.read_state(conn, job_name)["consecutive_failures"] == 2

    leases.claim(conn, job_name, "w", lease_secs=60)
    leases.release(conn, job_name, "w", delay_secs=60, result="ok")
    assert leases.read_state(conn, job_name)["consecutive_failures"] == 0


def test_claiming_an_unregistered_job_is_false_not_an_error(conn):
    missing = "never_registered_" + uuid.uuid4().hex
    assert leases.claim(conn, missing, "w", 60).acquired is False


def test_register_does_not_drag_a_live_schedule_back_to_now(conn, job_name):
    """A restart loop must not become a hot loop over every registered job."""
    leases.register(conn, job_name)
    leases.claim(conn, job_name, "w", lease_secs=60)
    leases.release(conn, job_name, "w", delay_secs=3600, result="ok")
    before = leases.read_state(conn, job_name)["next_run_at"]

    leases.register(conn, job_name)  # re-registration, as happens at every boot
    assert leases.read_state(conn, job_name)["next_run_at"] == before


def test_a_non_positive_lease_is_refused(conn, job_name):
    leases.register(conn, job_name)
    assert leases.claim(conn, job_name, "w", lease_secs=1).acquired is True  # control
    with pytest.raises(ValueError):
        leases.claim(conn, job_name, "w", lease_secs=0)


# ---------------------------------------------------------------- the schedule

def test_zero_jitter_is_refused():
    """E7: a fixed-offset schedule synchronizes every instance onto the same minute."""
    assert schedule.next_delay_secs(300, 0.2) > 0        # positive control
    with pytest.raises(ValueError, match="jitter"):
        schedule.next_delay_secs(300, 0.0)
    with pytest.raises(ValueError, match="jitter"):
        schedule.next_delay_secs(300, 0.9)


def test_jitter_actually_spreads_runs():
    """The property, not the parameter. Identical draws would be the #385 shape."""
    draws = {round(schedule.next_delay_secs(300, 0.2), 4) for _ in range(200)}
    assert len(draws) > 100, "jitter produced only %d distinct delays in 200" % len(draws)
    assert all(240 <= d <= 360 for d in draws), "draws must stay within the declared spread"


def test_backoff_grows_but_is_capped():
    base = schedule.next_delay_secs(60, 0.2)
    assert 48 <= base <= 72                                     # control
    assert schedule.backoff_delay_secs(60, 3, 0.2) > base
    huge = schedule.backoff_delay_secs(60, 50, 0.2)
    assert huge <= 60 * 32 * 1.2 + 1, (
        "an uncapped backoff schedules the retry past any window anyone watches, and "
        "the job becomes silently retired rather than loudly broken"
    )


# ---------------------------------------------------------------- the registry

def test_a_job_without_a_cost_bound_is_refused():
    """B5, enforced at import rather than discovered in production."""
    ok = registry.Job(name="x", interval_secs=1, timeout_secs=1, jitter_frac=0.1,
                      max_batch=1, run=lambda c, j: 0)
    assert ok.max_batch == 1                                    # positive control
    with pytest.raises(ValueError, match="max_batch"):
        registry.Job(name="x", interval_secs=1, timeout_secs=1, jitter_frac=0.1,
                     max_batch=0, run=lambda c, j: 0)


def test_the_derived_lease_always_outlives_the_timeout():
    """The invariant the removed vacuous check pretended to hold, tested where it can fail."""
    for timeout in (0.1, 1, 5, 10, 60, 600):
        job = registry.Job(name="t", interval_secs=timeout * 10, timeout_secs=timeout,
                           jitter_frac=0.1, max_batch=1, run=lambda c, j: 0)
        assert job.lease_secs > timeout, (
            "timeout=%s: a lease that lapses mid-run lets a second worker start a "
            "concurrent copy" % timeout
        )


def test_the_nonce_sweep_is_registered():
    names = [j.name for j in registry.JOBS]
    assert "envelope_nonce_sweep" in names
    assert registry.by_name("envelope_nonce_sweep").max_batch > 0
    with pytest.raises(KeyError):
        registry.by_name("no_such_job")


# -------------------------------------------------------- the job, end to end

def test_the_sweep_deletes_expired_rows_and_spares_live_ones(conn):
    """The whole point: a real row really deleted, and a live one really kept, by the
    bounded DELETE the scheduler uses (the unbounded default went in #130).

    `nearly-expired` has 5 s left. A row an hour from expiry could not tell a correct
    sweep from one that deletes early (`expires_at < now() + interval '100 seconds'`
    survived every test, review mutant M2); this one can."""
    _, peer_id = _seed_peer(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO envelope_nonce_seen (peer_id, nonce, expires_at) "
                    "VALUES (%s,'expired-1', now() - interval '1 hour'), "
                    "       (%s,'expired-2', now() - interval '1 hour'), "
                    "       (%s,'nearly-expired', now() + interval '5 seconds'), "
                    "       (%s,'still-live', now() + interval '1 hour')",
                    (peer_id, peer_id, peer_id, peer_id))

    # >= 2, not == 2. The sweep is TABLE-WIDE by design, so other rows legitimately
    # expired by other tests or a previous run are swept in the same call. Asserting an
    # exact global count would make this test fail for a reason that has nothing to do
    # with what it is testing. The scoped assertion below is the real check.
    deleted = nonce_ledger.sweep_expired_nonces(conn, limit=1000)
    assert deleted >= 2, "both of this test's expired rows must go"

    with conn.cursor() as cur:
        cur.execute("SELECT nonce FROM envelope_nonce_seen WHERE peer_id = %s "
                    "ORDER BY nonce", (peer_id,))
        remaining = [r[0] for r in cur.fetchall()]
    assert remaining == ["nearly-expired", "still-live"], (
        "positive control: a nonce inside its window must survive, or the sweep is "
        "deleting the replay protection it exists to bound"
    )


def test_the_sweep_respects_its_bound(conn):
    """B5: one call must not delete an unbounded backlog in a single transaction.

    Counted on THIS test's rows, not the table (review finding 6). The sweep is
    table-wide, and since divergence 036 every HTTP test in the run leaves a ledger row
    that expires minutes later, so exact global counts held only while none of those
    expired between the previous sweep and this one. A foreign expired row is planted
    here on purpose, so the assertions below are shown to hold with one present (the
    global form, 2/2/1/0, fails with it). This test's rows are the oldest in the table,
    and the sweep takes the oldest first, so each tick's share of them is exact."""
    _, peer_id = _seed_peer(conn)
    _, foreign_peer = _seed_peer(conn)
    with conn.cursor() as cur:
        for i in range(5):
            cur.execute("INSERT INTO envelope_nonce_seen (peer_id, nonce, expires_at) "
                        "VALUES (%s,%s, timestamptz '2000-01-01 00:00:00+00')",
                        (peer_id, "old-" + str(i)))
        cur.execute("INSERT INTO envelope_nonce_seen (peer_id, nonce, expires_at) "
                    "VALUES (%s,'foreign', now() - interval '1 minute')", (foreign_peer,))

    def mine() -> int:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM envelope_nonce_seen WHERE peer_id = %s",
                        (peer_id,))
            return cur.fetchone()[0]

    assert nonce_ledger.sweep_expired_nonces(conn, limit=2) == 2
    assert mine() == 3
    assert nonce_ledger.sweep_expired_nonces(conn, limit=2) == 2
    assert mine() == 1
    # The last of this test's rows and the foreign one: still two, never more.
    assert nonce_ledger.sweep_expired_nonces(conn, limit=2) == 2
    assert mine() == 0, "backlog drains over ticks"
    assert nonce_ledger.sweep_expired_nonces(conn, limit=2) <= 2

    with pytest.raises(ValueError):
        nonce_ledger.sweep_expired_nonces(conn, limit=0)
    with pytest.raises(TypeError):                   # #130: no unbounded default left
        nonce_ledger.sweep_expired_nonces(conn)
    with pytest.raises(ValueError):                  # nor an unbounded value passed by name
        nonce_ledger.sweep_expired_nonces(conn, limit=None)


def test_run_due_claims_runs_and_reschedules(conn):
    """run_due takes a FACTORY, never a connection, so a request connection cannot be
    handed to background work (E7). This exercises that path end to end."""
    _, peer_id = _seed_peer(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO envelope_nonce_seen (peer_id, nonce, expires_at) "
                    "VALUES (%s,'sweep-me', now() - interval '1 hour')", (peer_id,))

    name = "test_sweep_" + uuid.uuid4().hex[:10]
    job = registry.Job(name=name, interval_secs=300, timeout_secs=10, jitter_frac=0.2,
                       max_batch=1000, run=registry._sweep_envelope_nonces)
    try:
        outcomes = runner.run_due(_connect_factory(), jobs=(job,), holder="test-holder")
        assert len(outcomes) == 1
        out = outcomes[0]
        assert out.claimed is True and out.result == "ok", out
        assert out.rows is not None and out.rows >= 1
        assert out.more_work_pending is False

        state = leases.read_state(conn, name)
        assert state["holder"] is None, "the runner must give the lease back"
        assert state["last_result"] == "ok"

        # A second tick must NOT re-run it: the schedule was written forward.
        again = runner.run_due(_connect_factory(), jobs=(job,), holder="test-holder")
        assert again[0].claimed is False
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM background_job_leases WHERE job_name = %s", (name,))


def test_run_due_reports_a_failing_job_instead_of_raising(conn):
    """A job that throws must be recorded and released, not take the tick down with it."""
    name = "test_boom_" + uuid.uuid4().hex[:10]

    def explode(c, j):
        raise RuntimeError("deliberate")

    job = registry.Job(name=name, interval_secs=300, timeout_secs=10, jitter_frac=0.2,
                       max_batch=10, run=explode)
    try:
        out = runner.run_due(_connect_factory(), jobs=(job,), holder="test-holder")[0]
        assert out.claimed is True
        assert out.result == "error"
        assert "RuntimeError" in out.error and "deliberate" in out.error, (
            "the recorded error must name the exception TYPE, not only its message"
        )
        state = leases.read_state(conn, name)
        assert state["holder"] is None, "a failing job must still release its lease"
        assert state["consecutive_failures"] == 1
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM background_job_leases WHERE job_name = %s", (name,))


def test_run_due_reports_unclaimed_jobs_rather_than_omitting_them(conn, job_name):
    """B3: "checked, not due" must be distinguishable from "never looked at it"."""
    leases.register(conn, job_name)
    leases.claim(conn, job_name, "someone-else", lease_secs=300)  # held elsewhere

    job = registry.Job(name=job_name, interval_secs=300, timeout_secs=10,
                       jitter_frac=0.2, max_batch=10, run=lambda c, j: 0)

    outcomes = runner.run_due(_connect_factory(), jobs=(job,), holder="me")
    assert len(outcomes) == 1, "an unclaimed job must still appear in the result"
    assert outcomes[0].claimed is False
    assert outcomes[0].result is None


# ------------------------------------------------------ D06 the per-tick timeout, driven

def _app_factory():
    """The factory `run_jobs` passes (apps.core.dbconn.connect), which HONOURS
    statement_timeout_ms; `_connect_factory` above ignores it, so a timeout test on it
    would pass without any timeout at all."""
    from apps.core import dbconn

    def connect(**kwargs):
        return dbconn.connect(DSN, **kwargs)
    return connect


def test_d06_a_body_that_overruns_its_timeout_is_cancelled_and_recorded_as_error(conn):
    """#385's per-tick timeout (E7), driven rather than read off the source: a body asleep
    in the database past `timeout_secs` is cancelled BY THE SERVER, the tick reports
    `error` naming the cancellation, and the tick returns long before the body would
    have. The control is the same job and timeout with a body inside it: `ok`."""
    def sleeper(secs):
        def body(c, j):
            c.execute("SELECT pg_sleep(%s)", (secs,))
            return 0
        return body

    slow = registry.Job(name="test_slow_" + uuid.uuid4().hex[:10], interval_secs=300,
                        timeout_secs=0.5, jitter_frac=0.2, max_batch=10, run=sleeper(30))
    quick = registry.Job(name="test_quick_" + uuid.uuid4().hex[:10], interval_secs=300,
                         timeout_secs=0.5, jitter_frac=0.2, max_batch=10, run=sleeper(0.05))
    try:
        started = time.monotonic()
        out_slow, out_quick = runner.run_due(_app_factory(), jobs=(slow, quick),
                                             holder="test-holder")
        took = time.monotonic() - started
        assert out_quick.claimed and out_quick.result == "ok", out_quick   # control
        assert out_slow.claimed and out_slow.result == "error", out_slow
        assert "QueryCanceled" in out_slow.error and "statement timeout" in out_slow.error, (
            out_slow.error)
        # The body sleeps 30 s and the bound is 15 s, so an uncancelled body still fails by a
        # wide margin, while a loaded gate box has room: a 6 s body against a 4 s bound took
        # 4.3 s in mg446d (issue #451).
        assert took < 15, f"the tick waited {took:.1f}s: the body was not cancelled at 0.5 s"
        state = leases.read_state(conn, slow.name)
        assert state["holder"] is None and state["consecutive_failures"] == 1
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM background_job_leases WHERE job_name = ANY(%s)",
                        ([slow.name, quick.name],))


# --------------------------------------------- D18 a backlog is followed up, not waited on

def test_d18_a_tick_with_more_work_pending_is_due_again_at_once(conn):
    """`more_work_pending` was reported and then ignored: the runner scheduled the next
    run a full interval later either way, so a backlog drained one batch per interval
    while apps/mesh/projection.py said "the next tick follows immediately". A tick that
    reports more pending is now due again within `FOLLOW_UP_SECS` (jittered); the control,
    a tick under its bound, still waits its interval."""
    full = registry.Job(name="test_full_" + uuid.uuid4().hex[:10], interval_secs=300,
                        timeout_secs=10, jitter_frac=0.2, max_batch=3, run=lambda c, j: 3)
    short = registry.Job(name="test_short_" + uuid.uuid4().hex[:10], interval_secs=300,
                         timeout_secs=10, jitter_frac=0.2, max_batch=3, run=lambda c, j: 2)
    try:
        out_full, out_short = runner.run_due(_connect_factory(), jobs=(full, short),
                                             holder="test-holder")
        assert out_full.more_work_pending is True and out_short.more_work_pending is False
        with conn.cursor() as cur:
            cur.execute("SELECT job_name, extract(epoch FROM next_run_at - now()) "
                        "FROM background_job_leases WHERE job_name = ANY(%s)",
                        ([full.name, short.name],))
            due_in = dict(cur.fetchall())
        assert due_in[short.name] > 200, due_in                              # control
        assert due_in[full.name] < 5, (
            f"a tick that hit its bound is next due in {due_in[full.name]:.0f}s, a full "
            "interval: the backlog drains one batch per interval")
        time.sleep(2)
        again = runner.run_due(_connect_factory(), jobs=(full, short), holder="test-holder")
        assert [o.claimed for o in again] == [True, False], again
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM background_job_leases WHERE job_name = ANY(%s)",
                        ([full.name, short.name],))


@pytest.mark.parametrize("module,job_name,report", [
    ("apps.meshsem.embedjob", "mesh_embed_catch_up",
     {"space": "s", "rows_processed": 5, "stopped": "embed_bound", "more": True,
      "attempts": ()}),
    ("apps.meshsem.related", "mesh_related_edges",
     {"space": "s", "rows_processed": 5, "stopped": "deadline", "more": True,
      "status": "ok", "detail": None}),
], ids=["embed", "related"])
def test_d18_a_body_that_stops_early_says_so(conn, monkeypatch, module, job_name, report):
    """The embed tick stops on its embed bound or its deadline with rows READ well under
    max_batch, so `rows >= max_batch` read false and the backlog waited an interval (the
    limit divergence 019 recorded). The body now hands the runner the report's own `more`.
    The related job has the same two stops (divergence 032). Control: the same body on a
    report that did not stop early says nothing is pending."""
    import importlib
    from types import SimpleNamespace
    mod = importlib.import_module(module)
    base = registry.by_name(job_name)
    reports = iter([SimpleNamespace(**report),
                    SimpleNamespace(**{**report, "stopped": None, "more": False})])
    monkeypatch.setattr(mod, "catch_up", lambda *a, **kw: next(reports))
    outs = []
    for _ in range(2):
        job = registry.Job(name="test_early_" + uuid.uuid4().hex[:10],
                           interval_secs=base.interval_secs, timeout_secs=base.timeout_secs,
                           jitter_frac=base.jitter_frac, max_batch=base.max_batch,
                           run=base.run)
        try:
            outs.append(runner.run_due(_connect_factory(), jobs=(job,), holder="t")[0])
        finally:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM background_job_leases WHERE job_name = %s",
                            (job.name,))
    stopped, finished = outs
    assert stopped.result == "ok" and stopped.rows == 5, stopped
    assert finished.result == "ok" and finished.more_work_pending is False, finished
    assert stopped.more_work_pending is True, (
        "a tick that stopped on its bound with 5 of 2000 rows read reported no more work")


# ------------------------- review of f6923d6, finding 1: a stalled janitor holds no other job

def test_f1_a_stalled_file_janitor_does_not_hold_the_shared_loop(monkeypatch):
    """The file janitor's tick deletes up to 200 objects in Python, one blob call each, and
    the R2 client bounds each CALL at 60 s (apps/files/r2.py REQUEST_TIMEOUT_SECS), not the
    tick. In the shared serial `run_jobs --loop` a degraded store therefore held every
    other job for hours. The janitor ticks on a thread of its own inside the same
    `run_jobs --loop`, so a stalled tick does not hold the shared loop.

    Driven through the command, with `run_due` replaced so no job body runs: the janitor's
    tick is stalled until the test ends, and the other job must still complete three ticks
    meanwhile. Control: the janitor's tick is asserted to have STARTED, so "the other job
    ticked" is not the trivial result of a janitor that never ran."""
    import contextlib
    import threading
    from types import SimpleNamespace

    import django
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
    django.setup()
    from django.core.management import call_command
    from apps.jobs.management.commands import run_jobs as cmd

    janitor = registry.by_name("file_janitor")
    other = registry.by_name("envelope_nonce_sweep")
    monkeypatch.setattr(registry, "JOBS", (janitor, other))
    monkeypatch.setattr(cmd.dbconn, "serving_connection", lambda: contextlib.nullcontext())
    monkeypatch.setattr(cmd.schema_sets, "assert_schema_complete", lambda c: None)

    release = threading.Event()
    janitor_started = threading.Event()
    lock = threading.Lock()
    during_stall = []                   # other-job ticks that finished while it was stalled

    def fake_run_due(connect, *, jobs=None, holder=None):
        names = [j.name for j in jobs]
        if janitor.name in names:
            janitor_started.set()
            release.wait(timeout=5)     # a store answering nothing, one call after another
        elif janitor_started.is_set() and not release.is_set():
            with lock:
                during_stall.append(names)
        return [runner.Outcome(job=n, claimed=False) for n in names]

    monkeypatch.setattr(runner, "run_due", fake_run_due)
    deadline = time.monotonic() + 3

    def fake_sleep(secs):
        time.sleep(0.01)
        with lock:
            done = len(during_stall) >= 3
        if done or time.monotonic() > deadline:
            release.set()
            raise KeyboardInterrupt

    monkeypatch.setattr(cmd, "time", SimpleNamespace(sleep=fake_sleep))
    try:
        with pytest.raises(SystemExit):
            call_command("run_jobs", "--loop", "--interval", "0.01")
    finally:
        release.set()
    assert janitor_started.is_set(), "control: the janitor's tick never started"
    assert len(during_stall) >= 3, (
        f"{len(during_stall)} tick(s) of another job while the janitor's tick was stalled: "
        "a slow blob store holds the whole loop")
    assert all(janitor.name not in names for names in during_stall)
