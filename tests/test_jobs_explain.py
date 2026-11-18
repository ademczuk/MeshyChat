"""Why a job is not running, and the guard that keeps the answer honest.

THE PATTERN IS BORROWED. herdr (herdrdev/herdr, Apache-2.0) attaches a NAMED reason to
every inferred agent state and ships `herdr agent explain <target>` to dump the reasoning
trail rather than only the verdict. A scheduler needs the same thing for the same reason:
"no recent run" is produced by four different causes (held by a live lease, not yet due,
in backoff, never registered) and the row does not distinguish them to anyone who has not
read the schema.

THE RISK THIS FILE EXISTS TO CONTROL. An explainer is a SECOND implementation of the
claim predicate. If the two drift, the explainer confidently describes a decision the
scheduler did not make, which is worse than having no explainer at all: it converts "I do
not know why" into a wrong answer that reads authoritative.

So the load-bearing test here is not that each reason string is right. It is
test_explain_agrees_with_the_claim_predicate, which drives both over the same states and
requires them to agree. Reason strings are checked too, because agreement alone would be
satisfied by two predicates that are both wrong in the same way.
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from apps.jobs import explain as ex   # noqa: E402
from apps.jobs import leases          # noqa: E402

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")


@pytest.fixture()
def conn():
    with psycopg.connect(DSN, autocommit=True) as c:
        yield c


@pytest.fixture()
def job_name(conn):
    name = "test_explain_" + uuid.uuid4().hex[:12]
    yield name
    with conn.cursor() as cur:
        cur.execute("DELETE FROM background_job_leases WHERE job_name = %s", (name,))


def _reset(conn, name, *, next_run_offset="-1 second", holder=None, lease_offset=None,
           failures=0):
    """Put the row into an exact state. Timestamps are computed by the DATABASE."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE background_job_leases "
            "   SET next_run_at = now() + %s::interval, "
            "       holder = %s, "
            "       leased_until = CASE WHEN %s::text IS NULL THEN NULL "
            "                           ELSE now() + %s::interval END, "
            "       consecutive_failures = %s "
            " WHERE job_name = %s",
            (next_run_offset, holder, lease_offset, lease_offset, failures, name))


# ------------------------------------------------------------------ the reasons

def test_an_unregistered_job_says_so(conn):
    r = ex.explain(conn, "never_registered_" + uuid.uuid4().hex[:8])
    assert r.runnable is False
    assert r.reason == ex.NOT_REGISTERED


def test_a_due_unheld_job_is_runnable(conn, job_name):
    """Positive control for every blocked case below."""
    leases.register(conn, job_name)
    _reset(conn, job_name, next_run_offset="-1 second")
    r = ex.explain(conn, job_name)
    assert r.runnable is True
    assert r.reason == ex.DUE_NOW


def test_a_live_lease_blocks_and_names_the_holder(conn, job_name):
    leases.register(conn, job_name)
    _reset(conn, job_name, next_run_offset="-1 second",
           holder="worker-7", lease_offset="5 minutes")
    r = ex.explain(conn, job_name)
    assert r.runnable is False
    assert r.reason == ex.HELD_BY_ANOTHER
    assert r.holder == "worker-7"
    assert "worker-7" in r.detail, "the detail must name the holder an operator has to chase"


def test_a_future_schedule_blocks_and_is_not_confused_with_backoff(conn, job_name):
    leases.register(conn, job_name)
    _reset(conn, job_name, next_run_offset="10 minutes", failures=0)
    r = ex.explain(conn, job_name)
    assert r.runnable is False
    assert r.reason == ex.NOT_DUE
    assert r.in_backoff is False, "no failures means this delay is the ordinary interval"


def test_backoff_is_reported_as_a_modifier_not_a_separate_reason(conn, job_name):
    """The claim predicate cannot tell backoff from an ordinary interval, so neither may
    the explainer. Inventing a reason the scheduler cannot act on is how it starts lying."""
    leases.register(conn, job_name)
    _reset(conn, job_name, next_run_offset="10 minutes", failures=4)
    r = ex.explain(conn, job_name)
    assert r.reason == ex.NOT_DUE, "backoff must NOT become a fifth reason"
    assert r.in_backoff is True
    assert r.consecutive_failures == 4


def test_an_expired_lease_is_runnable_and_the_takeover_is_visible(conn, job_name):
    leases.register(conn, job_name)
    _reset(conn, job_name, next_run_offset="-1 second",
           holder="dead-worker", lease_offset="-1 minute")
    r = ex.explain(conn, job_name)
    assert r.runnable is True
    assert r.reason == ex.DUE_NOW
    assert "dead-worker" in r.detail and "TAKE OVER" in r.detail, (
        "a takeover must be visible in the explanation, not just in the runner's log"
    )


# ----------------------------------------------------------------- the guard

@pytest.mark.parametrize(
    "label,kwargs",
    [
        ("due and free",      dict(next_run_offset="-1 second")),
        ("held, live lease",  dict(next_run_offset="-1 second", holder="w", lease_offset="5 minutes")),
        ("held, dead lease",  dict(next_run_offset="-1 second", holder="w", lease_offset="-5 minutes")),
        ("not due",           dict(next_run_offset="10 minutes")),
        ("not due, in backoff", dict(next_run_offset="10 minutes", failures=3)),
        ("due, just released", dict(next_run_offset="-1 millisecond")),
    ],
)
def test_explain_agrees_with_the_claim_predicate(conn, job_name, label, kwargs):
    """THE LOAD-BEARING TEST. Two implementations of one predicate must not drift.

    explain() reads the same row claim() filters on and reports which clause rejects. If
    they ever disagree, the explainer describes a decision the scheduler did not make.
    """
    leases.register(conn, job_name)
    _reset(conn, job_name, **kwargs)

    predicted = ex.explain(conn, job_name).runnable          # read-only, runs first
    actual = leases.claim(conn, job_name, "prober", lease_secs=30).acquired

    assert predicted == actual, (
        f"{label}: explain said runnable={predicted} but the claim {'succeeded' if actual else 'failed'}. "
        "The explainer and the scheduler disagree, so the explanation is wrong."
    )


def test_explain_all_reports_every_job_including_the_healthy_ones(conn, job_name):
    """B3 again: "checked and fine" must differ from "not examined"."""
    leases.register(conn, job_name)

    class _J:
        def __init__(self, name):
            self.name = name

    others = [_J(job_name), _J("absent_" + uuid.uuid4().hex[:8])]
    reports = ex.explain_all(conn, others)
    assert len(reports) == 2, "a filtered subset would hide the jobs that are fine"
    assert {r.job for r in reports} == {o.name for o in others}


def test_callers_branch_on_reason_not_on_detail():
    """Guards the contract, so improving the wording cannot break a caller."""
    for name in (ex.NOT_REGISTERED, ex.HELD_BY_ANOTHER, ex.NOT_DUE, ex.DUE_NOW):
        assert isinstance(name, str) and name.islower() and " " not in name
    assert len({ex.NOT_REGISTERED, ex.HELD_BY_ANOTHER, ex.NOT_DUE, ex.DUE_NOW}) == 4


# ------------------------------------------- herdr: explain keeps the losers

def test_explain_reports_every_clause_not_only_the_first_blocker(conn, job_name):
    """herdr's evaluated_rules, applied here.

    A first-blocker-only explanation says the schedule blocked the job and says nothing
    about whether the lease was ALSO a problem, so the next question needs another round
    trip. herdr builds the full list in its normal evaluation loop rather than in a verbose
    mode, so the losers are always there.
    """
    leases.register(conn, job_name)
    _reset(conn, job_name, next_run_offset="10 minutes",
           holder="w", lease_offset="5 minutes")
    r = ex.explain(conn, job_name)

    # Exact order, not "either of these". The order mirrors the WHERE clause in
    # leases.claim, and an assertion that accepts two orderings cannot catch a reordering
    # that breaks that mirroring.
    assert [c.name for c in r.evaluated] == ["registered", "lease_free", "due"]
    assert r.reason == ex.HELD_BY_ANOTHER, "first blocker is still the reason"

    by = {c.name: c for c in r.evaluated}
    assert by["lease_free"].blocks is True
    assert by["due"].blocks is True, (
        "the schedule ALSO blocks, and that must be visible without a second query"
    )


def test_a_passing_clause_is_kept_with_blocks_false(conn, job_name):
    """The non-blocking answer is the one a first-blocker report throws away."""
    leases.register(conn, job_name)
    _reset(conn, job_name, next_run_offset="10 minutes")   # not due, but NOT held
    r = ex.explain(conn, job_name)
    by = {c.name: c for c in r.evaluated}
    assert by["due"].blocks is True
    assert by["lease_free"].blocks is False, "nothing holds it, and that is an answer"
    assert "not held" in by["lease_free"].detail


# -------------------------------- a job due for a long time is a WORKER finding

def test_a_job_due_briefly_is_not_flagged_as_starved(conn, job_name):
    """Positive control for the starvation test below."""
    leases.register(conn, job_name)
    _reset(conn, job_name, next_run_offset="-2 seconds")
    r = ex.explain(conn, job_name)
    assert r.runnable is True
    assert r.nothing_is_ticking is False
    assert r.due_for_secs is not None and r.due_for_secs < 60


def test_a_long_overdue_job_says_the_worker_is_the_problem(conn, job_name):
    """A job claimable for an hour is not a job problem. Without this, an operator reads
    'due_now' as healthy and goes looking in the wrong place."""
    leases.register(conn, job_name)
    _reset(conn, job_name, next_run_offset="-1 hour")
    r = ex.explain(conn, job_name)
    assert r.runnable is True, "it IS still claimable; the verdict must not change"
    assert r.reason == ex.DUE_NOW, "starvation is a modifier, not a new reason"
    assert r.nothing_is_ticking is True
    assert r.due_for_secs > ex.STALE_DUE_SECS
    assert "WORKER finding" in r.detail


# ------------------------------------------- operator verbs, from DBOS's affordances

def test_force_due_moves_the_schedule_and_nothing_else(conn, job_name):
    """--run-now must NOT bypass the lease. It says 'you may run', not 'run now'."""
    leases.register(conn, job_name)
    _reset(conn, job_name, next_run_offset="1 hour",
           holder="worker-9", lease_offset="30 minutes")
    assert ex.explain(conn, job_name).reason == ex.HELD_BY_ANOTHER   # control

    assert leases.force_due(conn, job_name) is True
    after = ex.explain(conn, job_name)
    assert after.reason == ex.HELD_BY_ANOTHER, (
        "bringing the schedule forward must leave a live lease in charge, or an operator "
        "can start a second concurrent copy of a job that is still running"
    )
    assert after.holder == "worker-9"


def test_force_due_on_an_unregistered_job_is_false(conn):
    assert leases.force_due(conn, "absent_" + uuid.uuid4().hex[:8]) is False


def test_releasing_a_live_lease_is_refused_without_force(conn, job_name):
    """Fail closed. The refusal must name the holder and the expiry, because the operator
    has to decide whether that holder is really dead."""
    leases.register(conn, job_name)
    _reset(conn, job_name, next_run_offset="-1 second",
           holder="still-working", lease_offset="10 minutes")

    released, why = leases.force_release(conn, job_name)
    assert released is False
    assert "REFUSED" in why and "still-working" in why
    assert ex.explain(conn, job_name).holder == "still-working", "the lease must survive"

    # CONTROL: the same call with --force does release it, so the refusal above is the
    # guard working and not the function being broken.
    released, why = leases.force_release(conn, job_name, force=True)
    assert released is True and "forced" in why
    assert ex.explain(conn, job_name).holder is None


def test_releasing_an_expired_lease_needs_no_force(conn, job_name):
    """The next claim would take it over anyway, so clearing it changes nothing except
    making the row read honestly in the meantime."""
    leases.register(conn, job_name)
    _reset(conn, job_name, next_run_offset="-1 second",
           holder="dead", lease_offset="-5 minutes")
    released, why = leases.force_release(conn, job_name)
    assert released is True
    assert "expired" in why and "dead" in why


def test_releasing_when_nothing_is_held_is_reported_not_silently_ok(conn, job_name):
    leases.register(conn, job_name)
    _reset(conn, job_name, next_run_offset="-1 second")
    released, why = leases.force_release(conn, job_name)
    assert released is False
    assert "nothing to release" in why


def test_never_run_is_distinguishable_from_ran_and_waiting(conn, job_name):
    """Registered but never started is a different state from ran-and-now-waiting, and
    the timestamps alone do not separate them for a reader."""
    leases.register(conn, job_name)
    assert ex.explain(conn, job_name).never_run is True

    leases.claim(conn, job_name, "w", lease_secs=60)          # sets last_started_at
    leases.release(conn, job_name, "w", delay_secs=60, result="ok")
    assert ex.explain(conn, job_name).never_run is False      # control
