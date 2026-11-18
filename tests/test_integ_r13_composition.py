"""integ/r13: what fin/w1-sec (B12's per-peer buckets), fin/w1-jobs (the retention sweeps, the
janitor's own lane, D17) and fin/w1-boot (E14's boot check, E15's statement timeout, F17's
health deadline) must still mean on top of integ/r12 (B07's capability guard ahead of remote
routing, F8's caller clause, B9's code fingerprint). Every negative sits beside its positive
control in the same test.

  1. B12 charges a token-bearing call once, whatever path decides it. A token-bearing call to
     the memory authority takes exactly one CALLS token on invoke and on A2A; step 9's
     E_CAPABILITY_NOT_ACCEPTED (start_call handed a principal for a remote target) takes
     exactly one, the step-0 take, and nothing more on the refusal; the view's
     E_CAPABILITY_NOT_ACCEPTED and E_CAPABILITY_INVALID are refused before start_call and take
     none. CONTROL: a tokenless call to the same remote target is queued and takes one. On an
     empty bucket a token-bearing call to the memory authority is 429 with Retry-After and no
     row, no nonce, no write, and step 9's refusal cannot be reached (RateLimited at step 0);
     the view's guard still answers E_CAPABILITY_NOT_ACCEPTED ahead of the limit.
  2. No retention sweep deletes a row a live r12 feature still needs: the call worker's lease
     row keeps its code fingerprint (sql_meshychat/022), a suspended caller's queued call
     stays queued for the deadline sweep (F8), a spent flow nonce is kept while its call
     exists (D17, even once the call is terminal), and an unexpired pairing code is kept.
     CONTROLS: in the same pass the sweeps delete an expired pairing code, an unspent expired
     nonce and a spent nonce whose call is gone; the deadline sweep then times the queued
     call out; once the call row is deleted its nonce goes too.
  3. `run_jobs --loop` with the file janitor on its own lane still runs the r12 call worker's
     deadline sweep in the shared pass, and leaves the worker's recorded fingerprint alone.
     CONTROLS: the janitor's lane tick started (and is stalled throughout); a call whose
     deadline has not passed is left in flight.
  4. /v1/health keeps r12's three code-fingerprint fields after w1-boot's F17 deadline, in
     order after `blob`; a stalled worker-code read is null inside one probe timeout; and the
     served entrypoint boots under daphne with the node-test server's environment (DSN, JWT
     key paths, MESHY_ALLOWED_HOSTS, no redis, no blob, tokens off) and answers them.
     CONTROLS: a recorded foreign hash reads stale without changing the status code; an
     unstalled read returns the hash; the served token JWKS carries the kid of the key the
     environment names, and git_sha is the environment's.

Run:  pytest tests/test_integ_r13_composition.py -q
"""
from __future__ import annotations

import contextlib
import dataclasses
import http.client
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
os.environ.setdefault("MESHY_ALLOWED_HOSTS", "testserver,127.0.0.1,localhost")

import django  # noqa: E402

django.setup()

from django.test import Client  # noqa: E402

from apps.core import dbconn  # noqa: E402
from apps.jobs import registry, runner  # noqa: E402
from tests.capability_invoke import invoke_context, outcome  # noqa: E402

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")
SWEEP = "call_deadline_sweep"
#: r12's /v1/health body with tokens off: seven base fields, then B9's three.
HEALTH_KEYS = ["status", "version", "git_sha", "git_dirty", "db", "redis", "blob",
               "code_sha256", "worker_code_sha256", "worker_code_stale"]


@pytest.fixture()
def owner():
    """Role `meshy`, autocommit: seeding and reading back, never the connection under test."""
    with psycopg.connect(DSN, autocommit=True) as c:
        yield c


def _write(ns: str, key: str) -> dict:
    return {"namespace": ns, "key": key, "value": {"n": 1}}


# ================================================== 1. B12 buckets and the capability paths

def _spent(peer) -> int:
    """CALLS tokens the peer has spent in this process (refill is ~0 under these tests)."""
    from apps.core import peer_limit
    lim = peer_limit._limiter
    if lim is None:
        return 0
    entry = lim._buckets.get(f"{peer_limit.CALLS}:{uuid.UUID(str(peer.id))}")
    return 0 if entry is None else round(lim.burst - entry[0])


def _remote_target(inv, tag: str):
    other = inv.peer(tag)
    inv.w.sql("INSERT INTO skills (peer_id, name, version, mode, share_mode, input_schema, "
              "is_current) VALUES (%s, 'memory.write', '0.2.0', 'sync', 'grants_only', '{}', "
              "TRUE)", (other.id,))
    return other


def _start(inv, caller, target, key, ns, *, principal):
    """start_call as a transport that builds its StartCallInput itself (no view in front)."""
    from apps.calls import pipeline
    from apps.calls import route as routing
    from apps.calls.envelope import SignedEnvelope

    env = inv.envelope(caller, "memory.write", _write(ns, key),
                       token=principal.token if principal else None, target=target.id)
    inp = pipeline.StartCallInput(
        caller_peer_id=uuid.UUID(caller.id), target_peer_id=uuid.UUID(target.id),
        skill_name="memory.write", envelope=SignedEnvelope(**env),
        call_id=env["payload"]["call_id"], object_capability=principal)
    ctx = pipeline.CallsContext(conn=inv.w.app, envelope_nonce_enforce=True,
                                memory_peer_id=routing.memory_peer_id_from_settings())
    return env, pipeline.start_call(ctx, inp)


def _world(inv, tag):
    a, e = inv.peer("A"), inv.peer("E")
    other = _remote_target(inv, tag)
    ns = inv.w.claim(a)
    inv.w.grant(ns, e, a, "write")
    tok = inv.w.mint(issuer=a, subject=e, ns=ns, rights=(("", "write"),), depth_cap=0)
    return a, e, other, ns, tok


def test_1a_a_token_bearing_call_and_each_capability_refusal_is_charged_at_most_once():
    import biscuit_auth

    from apps.memoryskills.capability import PresentedCapability

    with invoke_context(MESHY_PEER_RATE_BURST=50, MESHY_PEER_RATE_PER_SEC=0.0001) as inv:
        a, e, other, ns, tok = _world(inv, "r13-b12a")
        assert _spent(e) == 0

        def delta(fn):
            before = _spent(e)
            out = fn()
            return _spent(e) - before, out

        # A token-bearing call to the memory authority: one token, invoke and A2A alike.
        n, (seen, _env) = delta(lambda: inv.call(e, "memory.write", _write(ns, "b/inv"),
                                                 token=tok))
        assert outcome(seen) == (200, "completed", None), seen.body
        assert n == 1, f"invoke with a token took {n} tokens"
        n, (seen, _env) = delta(lambda: inv.a2a(e, "memory.write", _write(ns, "b/a2a"),
                                                token=tok))
        assert seen.body["status"]["state"] == "TASK_STATE_COMPLETED", seen.body
        assert n == 1, f"A2A with a token took {n} tokens"

        # The view's guard (B07) refuses a token to a remote target before start_call: none.
        for transport in ("invoke", "a2a"):
            send = inv.call if transport == "invoke" else inv.a2a
            n, (seen, env) = delta(lambda: send(e, "memory.write", _write(ns, f"b/r/{transport}"),
                                                token=tok, target=other.id))
            assert outcome(seen) == (403, "E_CAPABILITY_NOT_ACCEPTED", None), seen.body
            assert inv.call_row(env["payload"]["call_id"]) is None
            assert n == 0, f"{transport}: the view's refusal took {n} tokens"

        # Step 9's refusal, behind the views: step 0's one take and nothing on the refusal.
        n, (_env, res) = delta(lambda: _start(
            inv, e, other, "b/step9", ns,
            principal=PresentedCapability(tok.to_base64(), "memory.write")))
        row = inv.call_row(res.call_id)
        assert (row["status"], row["error_code"]) == ("failed", "E_CAPABILITY_NOT_ACCEPTED")
        assert n == 1, f"step 9's E_CAPABILITY_NOT_ACCEPTED took {n} tokens"

        # A token under a foreign root is refused by the view's pre-check: none.
        foreign = inv.w.mint(issuer=a, subject=e, ns=ns, rights=(("", "write"),),
                             depth_cap=0, register=False, root=biscuit_auth.KeyPair())
        n, (seen, _env) = delta(lambda: inv.call(e, "memory.write", _write(ns, "b/foreign"),
                                                 token=foreign))
        assert outcome(seen) == (403, "E_CAPABILITY_INVALID", None), seen.body
        assert n == 0

        # CONTROL, one field apart: the same remote call without the token is queued, one token.
        n, (seen, env) = delta(lambda: inv.call(e, "memory.write", _write(ns, "b/plain"),
                                                target=other.id))
        assert seen.status == 202, seen.body
        assert inv.call_row(env["payload"]["call_id"])["status"] == "queued"
        assert n == 1
        assert _spent(e) == 4, _spent(e)
        assert _spent(a) == 0, "the issuer's bucket paid for its subject's calls"


def test_1b_an_empty_bucket_refuses_a_token_bearing_call_before_any_row():
    from apps.calls.errors import RateLimited
    from apps.memoryskills.capability import PresentedCapability

    with invoke_context(MESHY_PEER_RATE_BURST=2, MESHY_PEER_RATE_PER_SEC=0.0001) as inv:
        _a, e, other, ns, tok = _world(inv, "r13-b12b")
        # CONTROL: the burst's two token-bearing calls complete.
        for key in ("e/1", "e/2"):
            seen, _env = inv.call(e, "memory.write", _write(ns, key), token=tok)
            assert outcome(seen) == (200, "completed", None), (key, seen.body)
        assert _spent(e) == 2

        seen, env = inv.call(e, "memory.write", _write(ns, "e/3"), token=tok)
        assert outcome(seen) == (429, "E_RATE_LIMITED", None), seen.body
        assert int(seen.headers["Retry-After"]) >= 1, seen.headers
        assert inv.call_row(env["payload"]["call_id"]) is None
        assert inv.nonce_rows(env) == [], "a refused call burned its nonce"
        assert [r["version"] for r in inv.memory_rows(ns, "e/3")] == []
        seen, _env = inv.a2a(e, "memory.write", _write(ns, "e/3a"), token=tok)
        assert seen.status == 429, seen.body

        # Step 0 comes before step 9: the principal never reaches the guard, and no row lands.
        with pytest.raises(RateLimited):
            _start(inv, e, other, "e/step9", ns,
                   principal=PresentedCapability(tok.to_base64(), "memory.write"))
        inv.w.app.rollback()
        assert inv._rows("SELECT 1 FROM skill_calls WHERE caller_peer_id = %s "
                         "AND status = 'failed'", (e.id,)) == []

        # The view's guard answers ahead of the limit (it runs before start_call).
        seen, _env = inv.call(e, "memory.write", _write(ns, "e/r"), token=tok, target=other.id)
        assert outcome(seen) == (403, "E_CAPABILITY_NOT_ACCEPTED", None), seen.body
        # And the tokenless remote call, which the guard passes, meets the limit.
        seen, _env = inv.call(e, "memory.write", _write(ns, "e/rp"), target=other.id)
        assert outcome(seen) == (429, "E_RATE_LIMITED", None), seen.body


# ======================================================== 2. retention against live r12 rows

def _peer(owner, tag: str, *, status: str = "active") -> tuple[uuid.UUID, uuid.UUID]:
    owner_id, peer_id = uuid.uuid4(), uuid.uuid4()
    owner.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id) "
                  "VALUES (%s,%s,'product',NULL)",
                  (owner_id, f"r13-{tag}-{owner_id.hex[:10]}@example.test"))
    owner.execute("INSERT INTO peers (id, owner_id, name, status) VALUES (%s,%s,%s,%s)",
                  (peer_id, owner_id, f"r13-{tag}-{peer_id.hex[:10]}", status))
    return owner_id, peer_id


def _queued_call(owner, *, caller_status: str, deadline: str) -> str:
    """A `queued` call of a caller in `caller_status` to another peer (no doorbell)."""
    c_owner, caller = _peer(owner, "caller", status=caller_status)
    t_owner, target = _peer(owner, "target")
    skill_id, call_id = uuid.uuid4(), f"r13-{uuid.uuid4().hex}"
    owner.execute("INSERT INTO skills (id, peer_id, name, version, mode, share_mode, "
                  "input_schema) VALUES (%s,%s,%s,'1','sync','private','{}')",
                  (skill_id, target, f"r13.q{skill_id.hex[:8]}"))
    owner.execute("INSERT INTO skill_calls (id, call_id, caller_peer_id, caller_owner_id, "
                  "target_peer_id, target_owner_id, skill_id, skill_name, skill_version, "
                  "mode, transport, args, status, deadline_at) VALUES (%s,%s,%s,%s,%s,%s,%s,"
                  f"'r13.q','1','sync','http','{{}}','queued',{deadline})",
                  (uuid.uuid4(), call_id, caller, c_owner, target, t_owner, skill_id))
    return call_id


def _flow_nonce(owner, parent: str, tag: str, expires: str, by: str | None = None) -> str:
    nonce = f"r13-{tag}-{uuid.uuid4().hex}"
    consumed = f"({expires}) - interval '1 minute'" if by else "NULL"
    owner.execute(
        f"INSERT INTO flow_continuation_nonces (nonce, parent_call_id, caller_owner_id, "
        f"caller_peer_id, target_owner_id, target_peer_id, expires_at, consumed_at, "
        f"consumed_by_call_id) VALUES (%s,%s,%s,%s,%s,%s,{expires},{consumed},%s)",
        (nonce, parent, uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), by))
    return nonce


def _col(owner, sql: str, params) -> list:
    return [r[0] for r in owner.execute(sql, params).fetchall()]


def _run(name: str) -> runner.Outcome:
    """The REGISTERED job `name`, once, under a lease name nobody else uses (the body, bound
    and timeout are the registry's; tests/test_retention_sweeps.py's pattern)."""
    job = dataclasses.replace(registry.by_name(name), name=f"t13_{name}_{uuid.uuid4().hex[:8]}")
    try:
        (out,) = runner.run_due(lambda **kw: dbconn.connect(DSN, **kw), jobs=(job,),
                                holder="integ-r13")
    finally:
        with psycopg.connect(DSN, autocommit=True) as c:
            c.execute("DELETE FROM background_job_leases WHERE job_name = %s", (job.name,))
    assert out.claimed and out.result == "ok", out
    return out


#: Every retention job w1-jobs registered, plus the two that already existed.
RETENTION = ("envelope_nonce_sweep", "dpop_jti_sweep", "pairing_code_sweep",
             "auth_challenge_sweep", "owner_session_sweep", "flow_nonce_sweep", "file_janitor",
             "mesh_route_usage_retention", "mesh_summary_ledger_retention")


def _lease_code(owner):
    return owner.execute("SELECT worker_code_sha256, worker_code_at FROM background_job_leases "
                         "WHERE job_name = %s", (SWEEP,)).fetchone()


def _worker_ticked():
    """The r12 call worker's own tick: it registers the sweep's lease row and records B9."""
    from apps.calls import worker
    from apps.core import code_fingerprint
    worker.tick(lambda **kw: dbconn.connect(DSN, **kw), holder="integ-r13-worker")
    return code_fingerprint.running()


def test_2_no_retention_sweep_deletes_a_row_a_live_r12_feature_needs(owner):
    assert {j.name for j in registry.JOBS} >= set(RETENTION), "a retention job was renamed"
    running = _worker_ticked()
    before = _lease_code(owner)
    assert before is not None and before[0] == running, "control: the worker recorded B9"

    # F8: a suspended caller's queued call is left to the deadline sweep; D17: its nonce.
    queued = _queued_call(owner, caller_status="suspended", deadline="now() + interval '1 hour'")
    parent = f"r13-parent-{uuid.uuid4().hex}"
    long_ago = "now() - interval '1 hour'"
    kept_nonce = _flow_nonce(owner, parent, "spent-live", long_ago, by=queued)
    gone_spent = _flow_nonce(owner, parent, "spent-gone", long_ago, by=f"r13-none-{uuid.uuid4()}")
    gone_unspent = _flow_nonce(owner, parent, "unspent", long_ago)
    p_owner, _ = _peer(owner, "pair")
    codes = {}
    for tag, exp in (("live", "now() + interval '60 seconds'"),
                     ("grace", "now() - interval '1 hour'"),
                     ("old", "now() - interval '2 days'")):
        codes[tag] = f"r13-{tag}-{uuid.uuid4().hex}"
        owner.execute(f"INSERT INTO pairing_codes (code, owner_id, expires_at) "
                      f"VALUES (%s, %s, {exp})", (codes[tag], p_owner))

    for name in RETENTION:
        _run(name)

    assert _lease_code(owner) == before, "a sweep touched the call worker's B9 record"
    from apps.calls import worker
    with contextlib.closing(dbconn.connect(DSN)) as c:
        assert worker.read_worker_code(c)[0] == running
    assert _col(owner, "SELECT status FROM skill_calls WHERE call_id = %s", (queued,)) == [
        "queued"], "a suspended caller's queued call was removed before its deadline"
    left = set(_col(owner, "SELECT nonce FROM flow_continuation_nonces WHERE parent_call_id = %s",
                    (parent,)))
    assert left == {kept_nonce}, ("D17: a spent nonce whose call exists must stay, and the two "
                                  f"dead ones must go (control); left {left}")
    left = set(_col(owner, "SELECT code FROM pairing_codes WHERE owner_id = %s", (p_owner,)))
    assert left == {codes["live"], codes["grace"]}, left       # control: "old" is gone

    # CONTROL for F8: the deadline sweep is what ends it, once its deadline has passed.
    owner.execute("UPDATE skill_calls SET deadline_at = now() - interval '1 second' "
                  "WHERE call_id = %s", (queued,))
    _run(SWEEP)
    assert _col(owner, "SELECT status FROM skill_calls WHERE call_id = %s", (queued,)) == [
        "timeout"]
    # D17 holds for a terminal call too: the receipt still reads its parent from the nonce.
    _run("flow_nonce_sweep")
    assert _col(owner, "SELECT nonce FROM flow_continuation_nonces WHERE nonce = %s",
                (kept_nonce,)) == [kept_nonce]
    # CONTROL for D17: the call row gone, the nonce goes with the next sweep.
    owner.execute("DELETE FROM skill_call_events WHERE call_id = %s", (queued,))
    owner.execute("DELETE FROM skill_calls WHERE call_id = %s", (queued,))
    _run("flow_nonce_sweep")
    assert _col(owner, "SELECT nonce FROM flow_continuation_nonces WHERE nonce = %s",
                (kept_nonce,)) == []


# ================================================= 3. run_jobs --loop, the janitor's own lane

def test_3_the_loop_runs_the_call_workers_deadline_sweep_beside_a_stalled_janitor_lane(
        owner, monkeypatch):
    from types import SimpleNamespace

    from django.core.management import call_command

    from apps.jobs.management.commands import run_jobs as cmd

    running = _worker_ticked()
    recorded = _lease_code(owner)
    assert recorded[0] == running
    overdue = _queued_call(owner, caller_status="suspended",
                           deadline="now() - interval '1 second'")
    future = _queued_call(owner, caller_status="suspended",
                          deadline="now() + interval '1 hour'")
    # Due now and unheld, as a restarted loop finds it; B9's columns are not touched.
    owner.execute("UPDATE background_job_leases SET next_run_at = now(), holder = NULL, "
                  "leased_until = NULL WHERE job_name = %s", (SWEEP,))

    janitor = registry.by_name("file_janitor")
    assert janitor.own_lane is True
    release, started, stall_over = threading.Event(), threading.Event(), threading.Event()
    swept_during_stall = []

    def stalled(conn, job):
        started.set()
        release.wait(10)                 # a blob store answering nothing
        stall_over.set()
        return 0

    lane = dataclasses.replace(janitor, run=stalled)
    monkeypatch.setattr(registry, "JOBS", (lane, registry.by_name(SWEEP)))

    def status(call_id):
        return _col(owner, "SELECT status FROM skill_calls WHERE call_id = %s", (call_id,))[0]

    deadline = time.monotonic() + 30

    def fake_sleep(_secs):
        time.sleep(0.05)
        if status(overdue) == "timeout" and started.is_set():
            swept_during_stall.append(not stall_over.is_set())
            release.set()
            raise KeyboardInterrupt
        if time.monotonic() > deadline:
            release.set()
            raise KeyboardInterrupt

    monkeypatch.setattr(cmd, "time", SimpleNamespace(sleep=fake_sleep))
    try:
        with pytest.raises(SystemExit):
            call_command("run_jobs", "--loop", "--interval", "0.05")
    finally:
        release.set()
    assert started.is_set(), "control: the janitor's lane tick never started"
    assert status(overdue) == "timeout", "the shared pass never ran the deadline sweep"
    assert swept_during_stall == [True], (
        "the deadline sweep ran only once the janitor's stalled tick had ended: the janitor "
        "held the shared pass")
    assert "timeout" in _col(owner, "SELECT event_type FROM skill_call_events "
                                    "WHERE call_id = %s", (overdue,))
    assert status(future) == "queued", "control: a call inside its deadline was swept"
    assert _lease_code(owner) == recorded, "the loop's tick overwrote the worker's B9 record"
    state = owner.execute("SELECT last_finished_at FROM background_job_leases "
                          "WHERE job_name = %s", (SWEEP,)).fetchone()
    assert state[0] is not None
    owner.execute("UPDATE skill_calls SET deadline_at = now() - interval '1 second' "
                  "WHERE call_id = %s", (future,))   # leave nothing in flight behind


# ============================================================ 4. /v1/health and the boot

def _record(code: str):
    from apps.jobs import leases
    with contextlib.closing(dbconn.connect(DSN)) as c:
        assert leases.record_code_hash(c, SWEEP, code)


def test_4a_health_keeps_the_code_fingerprint_fields_after_w1_boot():
    from django.conf import settings

    import meshychat.urls as u

    assert not getattr(settings, "MESHY_CAPABILITIES", False), "tokens are off by default"
    assert u.HEALTH_DEADLINE == u.PROBE_TIMEOUT, "F17's deadline"
    running = _worker_ticked()
    resp = Client().get("/v1/health")
    body = resp.json()
    assert list(body) == HEALTH_KEYS, list(body)
    assert (body["code_sha256"], body["worker_code_sha256"], body["worker_code_stale"]) == (
        running, running, False), body
    code = resp.status_code
    try:
        # CONTROL: a worker on other code reads stale, and the status code does not move.
        _record("0" * 64)
        stale = Client().get("/v1/health")
        assert stale.status_code == code
        assert (stale.json()["worker_code_sha256"], stale.json()["worker_code_stale"]) == (
            "0" * 64, True)
    finally:
        _record(running)


def test_4b_a_stalled_worker_code_read_is_null_inside_one_probe_timeout(monkeypatch):
    import meshychat.urls as u

    running = _worker_ticked()
    for name in ("probe_db", "probe_blob", "probe_redis"):
        monkeypatch.setattr(u, name, lambda *a, **k: True)
    ok = Client().get("/v1/health")                          # CONTROL: unstalled
    assert ok.status_code == 200 and ok.json()["worker_code_sha256"] == running, ok.json()

    release = threading.Event()

    def stalled(*_a, **_k):
        release.wait(30)
        return running

    monkeypatch.setattr(u, "probe_worker_code", stalled)
    try:
        t0 = time.monotonic()
        resp = Client().get("/v1/health")
        elapsed = time.monotonic() - t0
    finally:
        release.set()
    body = resp.json()
    assert resp.status_code == 200, "a stale or unknown worker never changes the status"
    assert (body["worker_code_sha256"], body["worker_code_stale"]) == (None, None), body
    assert list(body) == HEALTH_KEYS
    assert u.PROBE_TIMEOUT - 0.1 <= elapsed < u.PROBE_TIMEOUT + 0.5, elapsed


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _keypair(tmp_path: Path) -> tuple[Path, Path]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    priv, pub = tmp_path / "jwt.pem", tmp_path / "jwt.pub.pem"
    priv.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                       serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    pub.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM,
                                                  serialization.PublicFormat.SubjectPublicKeyInfo))
    return priv, pub


def _get(port: int, host: str, path: str = "/v1/health") -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", path, headers={"Host": host})
        r = conn.getresponse()
        return r.status, r.read()
    finally:
        conn.close()


def test_4c_the_server_boots_under_the_test_servers_environment(tmp_path):
    """daphne on meshychat.asgi:application, as tools/start.ps1 runs it on :17070, here on a
    free loopback port, with the environment start.ps1 sets and nothing of the test run's."""
    import json

    from apps.core import code_fingerprint

    running = _worker_ticked()
    priv, pub = _keypair(tmp_path)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("MESHY_", "MESHYCHAT_", "MESHY_", "PYTEST_", "DJANGO_", "DATABASE_URL"))}
    env.update({
        "DJANGO_SETTINGS_MODULE": "meshychat.settings", "PYTHONPATH": str(ROOT),
        "MESHY_DSN": DSN, "MESHYCHAT_JWT_ISSUER": "meshychat-integ-r13",
        "MESHYCHAT_JWT_PRIVATE_KEY_PEM_PATH": str(priv), "MESHYCHAT_JWT_PUBLIC_KEY_PEM_PATH": str(pub),
        "MESHYCHAT_STATE_SECRET": secrets.token_hex(32), "MESHY_SECRET_KEY": secrets.token_hex(32),
        "MESHY_ALLOWED_HOSTS": "192.168.50.20",
        "MESHYCHAT_BOOTSTRAP_AUTHORITY_PEER_ID": str(uuid.uuid4()),
        "MESHY_EMBED_FORBIDDEN_ROWS": "true",
        "MESHYCHAT_GIT_SHA": "0123456789ab", "MESHYCHAT_GIT_DIRTY": "0",
    })
    for absent in ("MESHY_REDIS_URL", "MESHYCHAT_BLOB_BACKEND",
                   "MESHY_CAPABILITIES"):
        assert absent not in env
    port = _free_port()
    log = (tmp_path / "daphne.log").open("wb")
    proc = subprocess.Popen([sys.executable, "-m", "daphne", "-b", "127.0.0.1", "-p", str(port),
                             "meshychat.asgi:application"], cwd=str(ROOT), env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    try:
        status, raw = None, b""
        until = time.monotonic() + 120
        while time.monotonic() < until:
            if proc.poll() is not None:
                break
            try:
                status, raw = _get(port, "192.168.50.20")
                break
            except OSError:
                time.sleep(0.5)
        log.flush()
        out = (tmp_path / "daphne.log").read_text("utf-8", "replace")
        assert status is not None, f"daphne did not answer (exit {proc.poll()}):\n{out[-3000:]}"
        body = json.loads(raw)
        assert list(body) == HEALTH_KEYS, body
        # No redis and no blob, as on the test server: degraded, 503, and the db answers.
        assert (status, body["status"], body["db"], body["redis"], body["blob"]) == (
            503, "degraded", True, False, False), body
        assert (body["git_sha"], body["git_dirty"]) == ("0123456789ab", False)
        # The served tree is this one; the worker's record is read through the served process.
        assert body["code_sha256"] == code_fingerprint.running() == running
        assert (body["worker_code_sha256"], body["worker_code_stale"]) == (running, False), body
        # CONTROL: the JWT key paths are this environment's: the token JWKS publishes the kid
        # of the key generated above, so the answer above is this env's server.
        from apps.authapi import tokens
        from apps.crypto import primitives as P
        kid = P.server_kid(tokens.public_key_raw(pub.read_bytes()))
        jwks_status, jwks = _get(port, "192.168.50.20", "/.well-known/jwks.json")
        assert jwks_status == 200, jwks
        assert [k["kid"] for k in json.loads(jwks)["keys"]] == [kid], jwks
    finally:
        proc.terminate()
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(15)
        log.close()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "--no-header", "-p", "no:cacheprovider"]))
