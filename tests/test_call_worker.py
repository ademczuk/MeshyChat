"""Divergence 041 S5: `manage.py run_call_worker`, one new process, runs the deadline sweep every
5 s in a loop of its own, and says when a call has stayed in flight past its deadline.

Design `docs/design/skill-frames.md`: 4.10 (a call to an offline target and the janitor's cadence), the approved
Q1 and Q4, critique F5 and F12, and rule B9. Once `queued` exists the deadline sweep is what
ends a call nobody will answer, and the shared `run_jobs` loop runs every job in one sequence
(apps/jobs/runner.py), divergence 032's link job included, whose tick may take 45 + 20 s. So
the sweep also runs in its own 5 s loop, and the lease decides which process runs a given tick.

  cadence  `call_deadline_sweep` is registered at 5 s; the worker runs that job and no other;
           the shared loop still has it, and every other job, unchanged.
  T12      an offline target's call is `queued`, and once its deadline passes a worker tick
           times it out: `E_DEADLINE_EXCEEDED`, "passed while the call was queued", read by
           the caller's poll. (The caller-socket half, `skill_timeout`, is S6's; the trigger
           that carries the janitor's edge is S3's, tests/test_call_notify_triggers.py.)
  T23      the shared loop is held by a slow job body; the worker's loop times the call out
           within one of its 5 s ticks while the shared loop is still held.
  health   the oldest in-flight call past its deadline, as a number and as the exit status of
           `run_call_worker --health`.

Run:  .venv/Scripts/python.exe -m pytest tests/test_call_worker.py -q -p no:cacheprovider
"""
from __future__ import annotations

import datetime as _dt
import io
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")
os.environ.setdefault("MESHY_TEST_DSN", DSN)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
os.environ.setdefault("MESHY_ALLOWED_HOSTS", "testserver,127.0.0.1,localhost")
os.environ.setdefault("MESHY_DSN", DSN)

import django  # noqa: E402
from django.conf import settings as dj_settings  # noqa: E402

if not dj_settings.configured:  # pragma: no cover - depends on collection order
    django.setup()

import jwt as pyjwt  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ed25519  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.test import Client, override_settings  # noqa: E402

from apps.calls import janitor  # noqa: E402
from apps.core import dbconn  # noqa: E402
from apps.crypto import primitives as P  # noqa: E402
from apps.jobs import leases, registry, runner  # noqa: E402
from tests.fixtures.call_rows import seed_call  # noqa: E402

ISSUER = "meshy-call-worker-test"
_server_key = ed25519.Ed25519PrivateKey.generate()
SERVER_PRIVATE_PEM = _server_key.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption())
SERVER_PUBLIC_PEM = _server_key.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)

SWEEP = "call_deadline_sweep"


@pytest.fixture(autouse=True)
def server_config():
    with override_settings(ALLOWED_HOSTS=["*"],
                           MESHY_JWT_PUBLIC_KEY_PEM=SERVER_PUBLIC_PEM.decode("ascii"),
                           MESHY_JWT_ISSUER=ISSUER, MESHY_DPOP_ENFORCE=False):
        yield


@pytest.fixture()
def owner():
    with psycopg.connect(DSN, autocommit=True) as conn:
        yield conn


def _peer(conn, tag: str, group: uuid.UUID) -> dict:
    owner_id, peer_id, seed = uuid.uuid4(), uuid.uuid4(), os.urandom(32)
    public = P.public_key_of(seed)
    conn.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id) "
                 "VALUES (%s,%s,'dev',%s)",
                 (owner_id, f"s5w-{tag}-{owner_id.hex[:8]}@example.test", group))
    conn.execute("INSERT INTO peers (id, owner_id, name) VALUES (%s,%s,%s)",
                 (peer_id, owner_id, f"s5w-{tag}-{peer_id.hex[:8]}"))
    kid = P.kid_for_peer_key(public)
    conn.execute("INSERT INTO peer_keys (peer_id, kid, public_key, active) "
                 "VALUES (%s,%s,%s,TRUE)", (peer_id, kid, P.b64u(public)))
    return {"peer_id": peer_id, "owner_id": owner_id, "seed": seed, "kid": kid}


@pytest.fixture()
def world(owner):
    """C calls T's skill, whose runtime is five seconds, so its deadline passes in a test.
    T never connects. Five, not one (issue #451): on a loaded gate box the invoke and the
    first tick took longer than one second, so T12's control found the call already swept
    (mg456b). The tests that wait for the deadline wait for the real one, so they only run longer."""
    group = uuid.uuid4()
    caller, target = _peer(owner, "caller", group), _peer(owner, "target", group)
    owner.execute("INSERT INTO skills (peer_id, name, version, mode, share_mode, input_schema, "
                  "is_current, max_runtime_ms) VALUES (%s,'s5w.slow','1.0.0','async',"
                  "'grants_only','{}',TRUE,5000)", (target["peer_id"],))
    return {"caller": caller, "target": target}


def _bearer(peer: dict) -> str:
    now = int(time.time())
    return "Bearer " + pyjwt.encode(
        {"sub": str(peer["peer_id"]), "aud": "peer", "iss": ISSUER, "iat": now,
         "exp": now + 600, "scopes": ["skills:invoke"]}, SERVER_PRIVATE_PEM, algorithm="EdDSA")


def queued_call(world: dict) -> tuple[str, _dt.datetime]:
    """A signed HTTP invoke to T, which has no socket: step 9 writes `queued`. Returns the
    call_id and its deadline (five seconds out). A fresh nonce; no nonce switch is pinned."""
    caller, target = world["caller"], world["target"]
    payload = {"v": 1, "kind": "skill.call", "call_id": f"s5w-{uuid.uuid4()}",
               "caller_peer_id": str(caller["peer_id"]),
               "target_peer_id": str(target["peer_id"]), "skill_name": "s5w.slow",
               "skill_version": "1.0.0", "args": {}, "args_attachments": [],
               "issued_at": _dt.datetime.now(_dt.timezone.utc).isoformat().replace(
                   "+00:00", "Z"),
               "nonce": uuid.uuid4().hex}
    envelope = {"payload": payload, "signature": P.b64u(P.sign(caller["seed"], P.jcs(payload))),
                "signing_kid": caller["kid"]}
    response = Client().post(
        f"/v1/agents/{target['peer_id']}/skills/s5w.slow/invoke",
        data=json.dumps({"envelope": envelope, "call_id": payload["call_id"]}),
        content_type="application/json", HTTP_AUTHORIZATION=_bearer(caller))
    assert response.status_code == 202, response.content
    body = json.loads(response.content)
    assert body["status"] == "queued", body
    deadline = _dt.datetime.fromisoformat(body["deadline_at"].replace("Z", "+00:00"))
    return payload["call_id"], deadline


def poll(world: dict, call_id: str) -> dict:
    response = Client().get(f"/v1/calls/{call_id}",
                            HTTP_AUTHORIZATION=_bearer(world["caller"]))
    assert response.status_code == 200, response.content
    return json.loads(response.content)


def _row(owner, call_id: str) -> tuple:
    return owner.execute("SELECT status, error_code, error_message FROM skill_calls "
                         "WHERE call_id = %s", (call_id,)).fetchone()


def _kinds(owner, call_id: str) -> list[tuple]:
    return owner.execute("SELECT event_type, source FROM skill_call_events WHERE call_id = %s "
                         "ORDER BY id", (call_id,)).fetchall()


def _sweep_due_now() -> None:
    conn = dbconn.connect()
    try:
        leases.register(conn, SWEEP)
        assert leases.force_due(conn, SWEEP)
    finally:
        conn.close()


#: How far ahead of now a leftover in-flight call's deadline is still brought forward by
#: `_clear_overdue`: longer than any test here runs.
CLEAR_HORIZON_SECS = 300


def _clear_overdue() -> None:
    """Every in-flight call past its deadline, whichever test left it, timed out now, so the
    health reading below is about this test's rows only. Also every one whose deadline
    falls within CLEAR_HORIZON_SECS: a call another file left 0.11 s short of its deadline
    was not yet past it at this sweep, crossed it before the health reading, and read as
    `oldest_overdue_secs` 0.1 where the test asserts None (review of integ/r14; 1 of 2
    combined runs on the untouched base). Such a call's deadline is moved just behind now,
    as the owner that `seed_call` writes calls as, and the sweep times it out with the rest."""
    with psycopg.connect(DSN, autocommit=True) as owner:
        owner.execute(
            "UPDATE skill_calls SET deadline_at = now() - interval '1 second' "
            "WHERE status = ANY(%s::text[]) "
            "AND deadline_at >= now() AND deadline_at < now() + make_interval(secs => %s)",
            (list(janitor.SWEPT_STATUSES), CLEAR_HORIZON_SECS))
    conn = dbconn.connect()
    try:
        while len(janitor.sweep_expired_deadlines(conn, limit=1000)) == 1000:
            pass
    finally:
        conn.close()


def _wait(predicate, what: str, timeout: float) -> float:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if predicate():
            return time.monotonic() - start
        time.sleep(0.05)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


# ========================================================================== cadence

def test_the_sweep_runs_every_five_seconds_in_the_workers_own_loop():
    """4.10 (a) and (b): the job's interval is 5 s; the worker's loop is 5 s and runs that
    job and nothing else. The shared loop is
    unchanged: it still has the sweep (the lease decides who runs a tick) and every other job,
    divergence 032's link job among them, at the intervals they had."""
    from apps.calls import worker
    job = registry.by_name(SWEEP)
    assert job.interval_secs == 5
    assert (job.timeout_secs, job.max_batch) == (10, 1000)
    assert job.run is janitor.run_deadline_sweep_job
    assert worker.WORKER_INTERVAL_SECS == 5
    assert worker.JOBS == (job,)
    assert job in registry.JOBS, "the shared loop keeps the sweep; the lease decides"
    unchanged = {"envelope_nonce_sweep": (300, 10), "mesh_edges_catch_up": (60, 20),
                 "mesh_embed_catch_up": (60, 30), "mesh_related_edges": (60, 45)}
    assert {n: (registry.by_name(n).interval_secs, registry.by_name(n).timeout_secs)
            for n in unchanged} == unchanged


# ============================================================================== T12

def test_t12_an_offline_targets_queued_call_times_out_and_says_it_was_queued(owner, world):
    """The call waits `queued` (Q1) until its deadline; then a worker tick, on connections of
    its own, times it out with the reason the caller reads. CONTROL: before the deadline the
    same tick leaves it queued."""
    from apps.calls import worker
    call_id, deadline = queued_call(world)
    _sweep_due_now()
    worker.tick(dbconn.connect)
    assert _row(owner, call_id)[0] == "queued", "control: swept before its deadline"
    time.sleep(max(0.0, (deadline - _dt.datetime.now(_dt.timezone.utc)).total_seconds()) + 0.3)
    _sweep_due_now()
    report = worker.tick(dbconn.connect)
    assert [o.job for o in report.outcomes] == [SWEEP] and report.outcomes[0].rows >= 1, report
    status, code, message = _row(owner, call_id)
    assert (status, code) == ("timeout", janitor.E_DEADLINE_EXCEEDED)
    assert message.endswith("passed while the call was queued"), message
    assert _kinds(owner, call_id) == [("created", "meshy"), ("queued", "meshy"),
                                      ("timeout", "meshy")]
    got = poll(world, call_id)
    assert (got["status"], got["error_code"], got["error_message"]) == (status, code, message)


# ============================================================================== T23

def test_t23_the_worker_times_a_call_out_while_the_shared_loop_is_held(owner, world):
    """F5: the shared loop is held by a slow job body (a fake that waits, standing in for
    the link job's 45 + 20 s tick) that sorts before the sweep, so that loop cannot reach the
    sweep. The worker's own loop times the call out within one of its 5 s ticks after the
    deadline, while the shared loop is still held. CONTROL: the shared loop really was held,
    and reached the sweep only after it was released."""
    from apps.calls import worker
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def slow(conn, job) -> int:
        started.set()
        release.wait(60)
        return 0

    fake = registry.Job(name=f"t23_slow_{uuid.uuid4().hex[:8]}", interval_secs=60,
                        timeout_secs=90, jitter_frac=0.2, max_batch=1, run=slow)
    shared_outcomes = []

    def shared_loop():
        shared_outcomes.extend(runner.run_due(dbconn.connect,
                                              jobs=(fake, registry.by_name(SWEEP))))
        finished.set()

    _sweep_due_now()
    shared = threading.Thread(target=shared_loop, name="t23-shared", daemon=True)
    shared.start()
    assert started.wait(15), "the shared loop never reached the slow job"
    stop = threading.Event()
    loop = threading.Thread(target=worker.run_loop, args=(dbconn.connect,),
                            kwargs={"stop": stop}, name="t23-worker", daemon=True)
    try:
        loop.start()
        call_id, deadline = queued_call(world)
        waited = _wait(lambda: _row(owner, call_id)[0] == "timeout",
                       "the worker to time the call out", timeout=20)
        late = (_dt.datetime.now(_dt.timezone.utc) - deadline).total_seconds()
        assert late <= worker.WORKER_INTERVAL_SECS * 1.2 + 1.5, (
            f"timed out {late:.2f}s after its deadline (waited {waited:.2f}s)")
        assert not finished.is_set(), "control: the shared loop was still held"
        assert _row(owner, call_id)[2].endswith("passed while the call was queued")
    finally:
        stop.set()
        release.set()
        loop.join(15)
        shared.join(30)
    assert not loop.is_alive(), "the worker's loop did not stop"
    assert finished.is_set() and [o.job for o in shared_outcomes] == [fake.name, SWEEP]


# =========================================================================== health

def test_the_health_signal_is_the_oldest_in_flight_call_past_its_deadline(owner, world):
    """Rule B9, 4.10 (c): health exercises the work path. The reading is the sweep's own
    predicate (the six in-flight statuses, `deadline_at < now()`), and `run_call_worker
    --health` exits 1 once the oldest such call is past its deadline by more than
    OVERDUE_ALERT_SECS, 0 otherwise. CONTROLS: nothing overdue reads None and exits 0; a call
    overdue by less than the threshold reads its age and exits 0; a worker tick clears it.
    Since the S5 review (F5) health also needs the sweep to have finished lately, so a tick
    runs first; tests below prove that half."""
    from apps.calls import worker
    _clear_overdue()
    _sweep_due_now()
    worker.tick(dbconn.connect)

    def health() -> tuple[int, dict]:
        out = io.StringIO()
        with pytest.raises(SystemExit) as exit_:
            call_command("run_call_worker", "--health", "--json", stdout=out)
        return exit_.value.code, json.loads(out.getvalue())

    code, report = health()
    assert (code, report["oldest_overdue_secs"], report["healthy"]) == (0, None, True), report
    assert report["alert_after_secs"] == janitor.OVERDUE_ALERT_SECS == 30

    target = world["target"]
    skill = owner.execute("SELECT id FROM skills WHERE peer_id = %s",
                          (target["peer_id"],)).fetchone()[0]
    now = _dt.datetime.now(_dt.timezone.utc)
    recent = seed_call(owner, world["caller"], target, skill, status="queued",
                       deadline=now - _dt.timedelta(seconds=5), call_id=f"s5w-{uuid.uuid4()}")
    code, report = health()
    assert code == 0 and report["healthy"] is True, report
    assert 5 <= report["oldest_overdue_secs"] < 30, report

    stale = seed_call(owner, world["caller"], target, skill, status="routed",
                      deadline=now - _dt.timedelta(seconds=120), call_id=f"s5w-{uuid.uuid4()}")
    conn = dbconn.connect()
    try:
        oldest = janitor.oldest_overdue_secs(conn)
    finally:
        conn.close()
    assert 120 <= oldest < 200, oldest
    code, report = health()
    assert code == 1 and report["healthy"] is False, report
    assert 120 <= report["oldest_overdue_secs"] < 200, report

    _sweep_due_now()
    with pytest.raises(SystemExit) as once:
        call_command("run_call_worker", "--once", stdout=io.StringIO())
    assert once.value.code == 0
    assert {c: _row(owner, c)[0] for c in (recent, stale)} == {recent: "timeout",
                                                                stale: "timeout"}
    code, report = health()
    assert (code, report["oldest_overdue_secs"]) == (0, None), report


# ================================================== the S5 review (3d3c5a8), pinned

def _health() -> tuple[int, dict]:
    out = io.StringIO()
    with pytest.raises(SystemExit) as exit_:
        call_command("run_call_worker", "--health", "--json", stdout=out)
    return exit_.value.code, json.loads(out.getvalue())


def _release_lease() -> None:
    conn = dbconn.connect()
    try:
        leases.force_release(conn, SWEEP, force=True)
    finally:
        conn.close()


def test_the_worker_does_not_spin_while_another_process_holds_the_sweep(monkeypatch):
    """F4 of the S5 review: while another process holds the sweep's lease (the shared loop
    mid-sweep, or a `run_jobs` that died holding it for its 35 s lease), the job is due but
    cannot be claimed. The loop must wait until the lease can be claimed, at most its 5 s,
    not tick every MIN_WAIT_SECS: before the review it ticked 9 times in 3 s, each tick two
    role-checked connections. CONTROL: the other holder really held the lease, and the loop's
    first tick found it held."""
    from apps.calls import worker
    _sweep_due_now()
    conn = dbconn.connect()
    try:
        assert leases.claim(conn, SWEEP, f"t-f4-other-{uuid.uuid4().hex[:8]}",
                            lease_secs=30).acquired, "control: the other process holds it"
    finally:
        conn.close()
    ticks = []
    original = worker.tick

    def counting(*args, **kwargs):
        report = original(*args, **kwargs)
        ticks.append(report)
        return report

    monkeypatch.setattr(worker, "tick", counting)
    stop = threading.Event()
    loop = threading.Thread(target=worker.run_loop, args=(dbconn.connect,),
                            kwargs={"stop": stop}, name="t-f4-worker", daemon=True)
    try:
        loop.start()
        time.sleep(2.5)
    finally:
        stop.set()
        loop.join(15)
        _release_lease()
    assert not loop.is_alive()
    assert len(ticks) == 1, f"{len(ticks)} ticks in 2.5 s while another process held the lease"
    assert [o.claimed for o in ticks[0].outcomes] == [False], ticks[0]
    assert ticks[0].next_due_secs > worker.WORKER_INTERVAL_SECS, ticks[0]


def test_the_worker_sleeps_until_the_job_can_run_bounded_to_its_interval(monkeypatch):
    """F3 MG of the S5 review: the loop sleeps for the tick's reading, bounded to
    [MIN_WAIT_SECS, WORKER_INTERVAL_SECS], and a flat 5 s sleep is wrong for a job due
    sooner (the job's interval is jittered to 4-6 s). A tick that raises waits the interval.
    Readings are fed in; the waits are recorded, no clock and no database."""
    from apps.calls import worker
    readings = iter([1.0, -3.0, None, 7.0, 0.05, RuntimeError("database down")])

    def fake_tick(connect, *, jobs, holder):
        reading = next(readings)
        if isinstance(reading, Exception):
            raise reading
        return worker.Tick(outcomes=[], oldest_overdue_secs=None, next_due_secs=reading)

    class Stop:
        def __init__(self) -> None:
            self.waits: list[float] = []

        def is_set(self) -> bool:
            return len(self.waits) >= 6

        def wait(self, secs: float) -> None:
            self.waits.append(secs)

    monkeypatch.setattr(worker, "tick", fake_tick)
    stop = Stop()
    worker.run_loop(None, stop=stop)
    assert stop.waits == [1.0, worker.MIN_WAIT_SECS, worker.WORKER_INTERVAL_SECS,
                          worker.WORKER_INTERVAL_SECS, worker.MIN_WAIT_SECS,
                          worker.WORKER_INTERVAL_SECS]


def test_health_alerts_when_the_deadline_sweep_has_not_run_lately(owner):
    """F5 of the S5 review, rule B9: with no call past its deadline the oldest-overdue reading
    is None whether or not anything is sweeping, so health could not see a dead
    `run_call_worker`. It now also reads how long ago the sweep last finished (its lease row's
    `last_finished_at`, on the database clock; either process may have run it), and is
    unhealthy past SWEEP_STALE_ALERT_SECS (30 s, six ticks) or when the sweep never ran.
    CONTROLS: just after a tick it is healthy; a tick makes it healthy again."""
    from apps.calls import worker
    _clear_overdue()
    _sweep_due_now()
    worker.tick(dbconn.connect)
    code, fresh = _health()
    assert code == 0 and fresh["healthy"] is True, fresh
    assert fresh["oldest_overdue_secs"] is None, fresh

    owner.execute("UPDATE background_job_leases SET last_finished_at = now() - "
                  "interval '120 seconds' WHERE job_name = %s", (SWEEP,))
    code, report = _health()
    assert code == 1, f"healthy while nothing had swept for 120 s: {report}"
    assert fresh["sweep_age_secs"] < 10, fresh
    assert report["healthy"] is False and report["oldest_overdue_secs"] is None, report
    assert 120 <= report["sweep_age_secs"] < 200, report
    assert report["sweep_alert_after_secs"] == worker.SWEEP_STALE_ALERT_SECS == 30

    owner.execute("UPDATE background_job_leases SET last_finished_at = NULL "
                  "WHERE job_name = %s", (SWEEP,))
    code, report = _health()
    assert (code, report["sweep_age_secs"], report["healthy"]) == (1, None, False), report

    _sweep_due_now()
    with pytest.raises(SystemExit) as once:
        call_command("run_call_worker", "--once", stdout=io.StringIO())
    assert once.value.code == 0
    code, report = _health()
    assert code == 0 and report["sweep_age_secs"] < 10, report



def test_a_call_left_just_short_of_its_deadline_does_not_reach_the_health_reading(owner, world):
    """Review of integ/r14: the two health tests above read `oldest_overdue_secs` None after
    `_clear_overdue`, and failed 1 of 2 combined runs on the untouched base because an
    earlier file left an in-flight call 0.11 s short of its deadline, which crossed it
    between that sweep and the reading. Planted here: a queued call due in 1.5 s, then
    `_clear_overdue` and a tick as those tests begin, then a reading once that deadline has
    passed. It reads None: the leftover was timed out with the rest. CONTROL: the call was
    in flight and not yet due when `_clear_overdue` ran."""
    from apps.calls import worker
    target = world["target"]
    skill = owner.execute("SELECT id FROM skills WHERE peer_id = %s",
                          (target["peer_id"],)).fetchone()[0]
    due = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=1.5)
    left = seed_call(owner, world["caller"], target, skill, status="queued", deadline=due,
                     call_id=f"s5w-left-{uuid.uuid4()}")
    assert _row(owner, left)[0] == "queued"
    assert owner.execute("SELECT deadline_at > now() FROM skill_calls WHERE call_id = %s",
                         (left,)).fetchone()[0], "control: not yet due"
    _clear_overdue()
    _sweep_due_now()
    worker.tick(dbconn.connect)
    _wait(lambda: owner.execute("SELECT now() > %s + interval '200 milliseconds'",
                                (due,)).fetchone()[0], "the planted deadline to pass", 10.0)
    code, report = _health()
    assert report["oldest_overdue_secs"] is None, report
    assert code == 0 and report["healthy"] is True, report


# ============================== B9: the hash of the worker's code (operator decision 2026-09-25)

def _recorded(owner) -> tuple:
    return owner.execute("SELECT worker_code_sha256, worker_code_at FROM background_job_leases "
                         "WHERE job_name = %s", (SWEEP,)).fetchone()


def test_b9_every_tick_records_the_hash_of_the_workers_code_and_health_names_a_stale_worker(
        owner):
    """Rule B9, the S5 review's F5 other half (operator decision 2026-09-25): every tick of
    `run_call_worker` writes a content hash of the code it is running (the served tree as the
    process first read it, `code_fingerprint.running()`) to the sweep's lease row
    (sql_meshychat/022), whether or not that tick claimed the sweep; `--health` compares it
    with the serving code and exits 1 on a stale worker. CONTROLS: the hash this process
    records is the serving code's (healthy); a hash never recorded is reported as None and is
    not, by itself, unhealthy; a tick makes a stale reading current again."""
    from apps.calls import worker
    from apps.core import code_fingerprint
    serving = code_fingerprint.running()
    assert len(serving) == 64 and serving == code_fingerprint.running()
    _clear_overdue()
    _sweep_due_now()
    worker.tick(dbconn.connect)
    owner.execute("UPDATE background_job_leases SET worker_code_sha256 = NULL, "
                  "worker_code_at = NULL WHERE job_name = %s", (SWEEP,))
    code, report = _health()
    assert (code, report["worker_code_sha256"], report["worker_code_stale"]) == (0, None, None), \
        report
    worker.tick(dbconn.connect)
    sha, at = _recorded(owner)
    assert sha == serving and at is not None, (sha, at)
    code, report = _health()
    assert (code, report["code_sha256"], report["worker_code_sha256"],
            report["worker_code_stale"], report["healthy"]) == (0, serving, serving, False,
                                                                True), report
    assert 0 <= report["worker_code_age_secs"] < 10, report

    # A worker still running code the tree no longer holds.
    owner.execute("UPDATE background_job_leases SET worker_code_sha256 = %s, "
                  "worker_code_at = now() WHERE job_name = %s", ("0" * 64, SWEEP))
    code, report = _health()
    assert (code, report["worker_code_stale"], report["healthy"]) == (1, True, False), report
    assert report["oldest_overdue_secs"] is None and report["sweep_age_secs"] < 30, report

    # A tick that does NOT claim the sweep (another process holds it) records all the same.
    _sweep_due_now()
    conn = dbconn.connect()
    try:
        assert leases.claim(conn, SWEEP, f"t-b9-other-{uuid.uuid4().hex[:8]}",
                            lease_secs=30).acquired
    finally:
        conn.close()
    try:
        tick = worker.tick(dbconn.connect)
    finally:
        _release_lease()
    assert [o.claimed for o in tick.outcomes] == [False], tick
    assert _recorded(owner)[0] == serving
    code, report = _health()
    assert (code, report["worker_code_stale"]) == (0, False), report


def test_status_names_the_call_worker_and_drops_the_stale_nothing_schedules_line():
    """F6 of the S5 review: docs/STATUS.md's background-jobs gap said NOTHING scheduled
    `run_jobs` (stale since the README runbook's step 5) and never named `run_call_worker`,
    while divergence 017's corrected line cites that very section."""
    text = (ROOT / "docs" / "STATUS.md").read_text(encoding="utf-8")
    assert "NOTHING SCHEDULES THAT COMMAND" not in text
    assert "run_call_worker" in text
