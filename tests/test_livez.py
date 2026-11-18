"""`/v1/livez`: the load balancer's route. Database only, so a node without Redis stays in.

THE OPERATOR'S REASON FOR THE ROUTE (/v1/livez is divergence 035; status item E06).
`/v1/health` is the deep probe: 200 only when db AND redis AND blob pass, otherwise 503
"degraded". A deployment without Redis or blob storage configured therefore answers 503 on
every health check, forever, and a load balancer pointed at it evicts a node that is serving
requests. `/v1/health` remains unchanged; this route answers the narrower question a load
balancer asks: can this process reach its database?

The `livez` name is conventional.

What is pinned, each negative with a positive control in the same test (V1 rule C1):

  1. 200 {"status":"ok"} with the database up while /v1/health is 503 in the SAME
     configuration - the reason the route exists.
  2. 503 when the database is unreachable, for GET and for HEAD, as JSON, naming nothing.
  3. a hung probe is answered within ONE probe timeout, not health's three, and not early;
     the deadline is the module's constant, not a number written into the view.
  3b. a probe that raises is a 503, not a 500, and the log names its type, never its text.
  4. it does not share health's probe pool, so a wedged Redis probe there cannot starve it.
  4b. probes abandoned at their deadline do not fail later checks once the database is
      back - measured with a real mid-reply stall, not only a monkeypatched one.
  4c. at most LIVEZ_MAX_PROBES probe threads, and so database connections, exist at once,
      however many checks arrive; at the bound a check answers 503 without probing.
  4d. concurrent checks share ONE probe and a finished answer is reused for about a second,
      so a flood of unauthenticated checks does not become a flood of database logins.
  4e. a probe thread cannot hold up process exit.
  4f. the probe's connection carries TCP keepalives, so an abandoned probe whose reply the
      network lost returns and frees its slot.
  5. GET and HEAD only, like /v1/health.
  6. no credential is needed, and a bad one does not turn a probe into a 401; nor does a
     Host header outside ALLOWED_HOSTS (a load balancer probes by IP).
  7. the documents say what the route is: a readiness check, not a restart trigger; not the
     request path's RLS guard; and every citation of its divergence names its entry.

Run:  pytest tests/test_livez.py -q
Needs the dev stack database (conftest clones one per run).
"""
from __future__ import annotations

import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
os.environ.setdefault("MESHY_ALLOWED_HOSTS", "testserver,127.0.0.1,localhost")

import django  # noqa: E402

django.setup()

from django.test import Client, override_settings  # noqa: E402

import meshychat.urls as u  # noqa: E402
from apps.core import dbconn  # noqa: E402

#: The bound a hung probe must be answered within: ONE probe timeout (2 s)
#: plus the same 1.5 s of runner slack. Hardcoded rather than read from the module under
#: test, so a mutant that raises the module's constant cannot move the goalpost with it.
#: /v1/health's whole-set budget is 3 x 2 s = 6 s; this must stay well under it.
LIVEZ_BUDGET_S = 2.0 + 1.5

#: The reuse window pinned from both sides: an answer is served again at 0.6 s, and not at
#: 1.15 s. Hardcoded for the same reason as the budget above.
REUSED_AT_S, REPROBED_AT_S = 0.6, 1.15

#: The ceiling on probe threads alive at once. Each can hold a database connection opened
#: for an unauthenticated caller.
MAX_PROBES_CEILING = 4


@pytest.fixture(autouse=True)
def fresh_livez_state(monkeypatch):
    """Each test starts with no cached answer and no probe in flight, as a new process does.

    Probes a test leaves running belong to the replaced instance and cannot touch the next
    test's state.
    """
    fresh = getattr(u, "_Livez", None)
    if fresh is not None:
        monkeypatch.setattr(u, "_LIVEZ", fresh())
    yield


def _forget():
    """A fresh route state: no cached answer and no probes counted, as in a new process.

    Not for a test about probes that outlive their check - it forgets those too. Such a test
    waits out the reuse window instead (see the mid-reply stall test).
    """
    fresh = getattr(u, "_Livez", None)
    if fresh is not None:
        u._LIVEZ = fresh()


def _dead_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                   # nothing listens there now
    return port


def _gated_ping(calls: list, gate: threading.Event, answer: bool = True, wait_s: float = 30):
    """A stand-in for dbconn.ping that counts its calls and returns only when `gate` opens."""
    lock = threading.Lock()

    def ping(*_a, **_k):
        with lock:
            calls.append(threading.current_thread().name)
        gate.wait(wait_s)
        return answer

    return ping


@pytest.fixture()
def unconfigured_env(monkeypatch):
    """Redis and blob absent, the database real: the deployment E06 is about."""
    for var in ("MESHY_REDIS_URL", "MESHYCHAT_BLOB_BACKEND", "MESHYCHAT_BLOB_DIR"):
        monkeypatch.delenv(var, raising=False)
    # The blob probe asks the store built at startup, not the environment (B11): a
    # process that started without MESHYCHAT_BLOB_BACKEND has none.
    from apps.files import blob as blobmod
    monkeypatch.setattr(blobmod, "_ACTIVE", None)


# ------------------------------------------------------------------ 1. the reason
def test_livez_is_200_while_health_is_503_in_the_same_configuration(unconfigured_env):
    client = Client()
    health = client.get("/v1/health")
    hbody = json.loads(health.content.decode())
    # Control: the configuration really is the one a load balancer would evict, and the
    # database really is up. Without both, the 200 below proves nothing about E06.
    assert health.status_code == 503 and hbody["status"] == "degraded", hbody
    assert hbody["db"] is True, ("the database probe failed, so this test would pass for "
                                 "the wrong reason - is the dev stack up on 5434?")
    assert (hbody["redis"], hbody["blob"]) == (False, False), hbody

    live = client.get("/v1/livez")
    assert live.status_code == 200, (
        f"GET /v1/livez answered {live.status_code} with the database up and Redis and blob "
        "unconfigured. That is the configuration in which /v1/health is 503, and a load "
        "balancer needs a route that stays 200 in it (STATUS E06)")
    assert live["Content-Type"] == "application/json"
    assert live.content == b'{"status":"ok"}', live.content


# ------------------------------------------------------------ 2. database down
def test_livez_is_503_when_the_database_is_unreachable_and_says_nothing_about_it():
    client = Client()
    up = client.get("/v1/livez")                          # positive control
    assert up.status_code == 200, f"the real database is not reachable: {up.status_code}"

    marker = "livez-secret-marker-7f3a"
    dead_dsn = f"postgresql://meshychat_owner:{marker}@127.0.0.1:{_dead_port()}/nowhere"
    _forget()
    with override_settings(MESHY_DSN=dead_dsn):
        down = client.get("/v1/livez")

    assert down.status_code == 503, (
        f"/v1/livez answered {down.status_code} with the database unreachable; a 200 here "
        "keeps a node that cannot serve anything in the load balancer's pool")
    assert down["Content-Type"] == "application/json", (
        f"the 503 is served as {down['Content-Type']!r}; the route's contract is JSON on "
        "both answers, and a checker that parses the body must not get HTML on the one "
        "that matters")
    body = json.loads(down.content.decode())
    assert body == {"status": "unavailable"}, body
    wire = down.content.decode() + "".join(f"{k}:{v}" for k, v in down.items())
    for secret in (marker, dead_dsn, "meshy:", "nowhere"):
        assert secret not in wire, f"/v1/livez leaked {secret!r} from the DSN into the response"


def test_head_probes_the_database_too(monkeypatch):
    """A load balancer that checks with HEAD must not keep a node whose database is dead.

    The body cannot be asserted here: Django's test client strips HEAD bodies itself, so
    `content == b""` holds whatever the view returns. uvicorn and daphne were measured
    sending 0 body bytes for HEAD in review. What CAN go wrong is the status, and a HEAD
    answered without probing is the way it goes wrong.
    """
    client = Client()
    calls = []
    real_ping = dbconn.ping

    def counting(*a, **k):
        calls.append(1)
        return real_ping(*a, **k)

    monkeypatch.setattr(u.dbconn, "ping", counting)

    up = client.head("/v1/livez")                          # positive control
    assert up.status_code == 200 and len(calls) == 1, (up.status_code, len(calls))

    _forget()
    dead_dsn = f"postgresql://meshychat_owner:x@127.0.0.1:{_dead_port()}/nowhere"
    with override_settings(MESHY_DSN=dead_dsn):
        down = client.head("/v1/livez")
    assert len(calls) == 2, (
        f"HEAD /v1/livez ran the database probe {len(calls) - 1} times for the second "
        "check; a HEAD answered without probing reports a dead database as live")
    assert down.status_code == 503, (
        f"HEAD /v1/livez answered {down.status_code} with the database unreachable")


# ---------------------------------------------------------------- 3. the deadline
def test_livez_answers_a_hung_probe_within_one_probe_timeout(monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def hung(*_a, **_k):
        entered.set()
        release.wait(30)            # a black-holed or stalled database, released below
        return True

    monkeypatch.setattr(u.dbconn, "ping", hung)
    try:
        started = time.monotonic()
        resp = Client().get("/v1/livez")
        elapsed = time.monotonic() - started
    finally:
        release.set()               # free the worker so no later test inherits the stall

    assert entered.is_set(), (
        f"the stalled probe was never reached (status {resp.status_code}), so this test "
        "measured something other than the deadline")
    assert resp.status_code == 503, "a probe that never answered was reported live"
    assert resp["Content-Type"] == "application/json", resp["Content-Type"]
    assert elapsed < LIVEZ_BUDGET_S, (
        f"/v1/livez took {elapsed:.1f}s with a hung database probe; its budget is one "
        f"probe timeout ({LIVEZ_BUDGET_S:.1f}s with slack). A load balancer's own probe "
        "timeout is typically 2-5 s, and a route that outlives it reads as a hang")
    assert elapsed >= 0.9 * 2.0, (
        f"/v1/livez gave up on the probe after {elapsed:.2f}s; the deadline is one probe "
        "timeout (2 s), and giving up early reports a slow but live database as down")
    assert u.LIVEZ_DEADLINE == u.PROBE_TIMEOUT == 2.0, (
        "the liveness deadline is one probe timeout, not a new number")


def test_the_deadline_is_the_modules_constant_not_a_number_in_the_view(monkeypatch):
    """The test above pins the constant; this one pins that the view obeys it. A deadline
    written into the view as a literal passes the first and fails here."""
    release = threading.Event()
    monkeypatch.setattr(u.dbconn, "ping", _gated_ping([], release))
    monkeypatch.setattr(u, "LIVEZ_DEADLINE", 0.5)
    try:
        started = time.monotonic()
        resp = Client().get("/v1/livez")
        elapsed = time.monotonic() - started
    finally:
        release.set()
    assert resp.status_code == 503, resp.status_code
    assert 0.45 <= elapsed < 1.3, (
        f"with LIVEZ_DEADLINE at 0.5 s the hung check answered after {elapsed:.2f}s: the "
        "view is not using the constant")


def test_a_probe_that_raises_is_a_503_not_a_500_and_its_message_is_not_logged(
        monkeypatch, caplog):
    """dbconn.ping swallows its own errors, so this branch guards the next probe that does
    not. A 500 would still evict, but it would read as a bug in the route, not a down
    database, and the contract is 200 or 503. The message is kept out of the log because a
    connection error's message can carry the host and the user."""
    ok = Client().get("/v1/livez")                        # positive control
    assert ok.status_code == 200, ok.status_code

    marker = "livez-raise-marker-51c9"

    def boom(*_a, **_k):
        raise RuntimeError(f"probe exploded: postgresql://meshychat_owner:{marker}@db/x")

    monkeypatch.setattr(u.dbconn, "ping", boom)
    _forget()
    with caplog.at_level(logging.DEBUG):
        resp = Client().get("/v1/livez")
    assert resp.status_code == 503, (
        f"a probe that raised answered {resp.status_code}; the route must report the "
        "database unavailable, not fail itself")
    assert resp.content == b'{"status":"unavailable"}', resp.content

    logged = [r.getMessage() for r in caplog.records]
    # Control: the probe's failure WAS logged and captured - from the probe's own thread -
    # so the absence below is not an empty capture.
    assert any("RuntimeError" in m for m in logged), (
        f"no log line names the failure's type; captured: {logged}")
    leaked = [m for m in logged if marker in m]
    assert not leaked, f"the exception's message reached the log: {leaked}"


# ------------------------------------------------- 4. not health's probe pool
def test_livez_does_not_queue_behind_a_saturated_health_probe_pool(monkeypatch):
    """A slow-drip Redis held a /v1/health worker for 21 s in review. With every health
    worker held, the liveness answer must still come from the database, not the queue.

    The database probe is stubbed healthy (issue #451): on a loaded gate box the real SELECT 1
    overran livez's own 2 s deadline and gave 503 for an unrelated reason (mg446d, 2.3 s). The
    stub keeps what is tested: were livez to submit its probe to the saturated pool, the stub
    would queue behind the blockers like the real probe and the answer would still be 503."""
    monkeypatch.setattr(u, "_livez_probe", lambda: True)
    release = threading.Event()
    blockers = [u._PROBES.submit(release.wait, 30) for _ in range(u._PROBES._max_workers)]
    try:
        # Control: the pool really is saturated - a trivial task cannot start.
        probe = u._PROBES.submit(lambda: True)
        time.sleep(0.3)
        assert not probe.done(), "health's pool was not saturated; the check below is vacuous"

        started = time.monotonic()
        resp = Client().get("/v1/livez")
        elapsed = time.monotonic() - started
    finally:
        release.set()
        for b in blockers:
            b.result(timeout=5)

    assert resp.status_code == 200, (
        f"/v1/livez answered {resp.status_code} while /v1/health's probe pool was full; the "
        "liveness probe queued behind the dependencies it exists to ignore")
    assert elapsed < 1.5, f"/v1/livez took {elapsed:.1f}s behind a saturated health pool"


class _StallProxy:
    """A TCP proxy to the real database that goes silent mid-reply.

    It forwards both ways until the client sends `trigger`, then drops every byte the
    server sends on that connection: a database whose reply the network lost after the
    query went out. The kernel still ACKs, so nothing below the application notices.
    Closing the proxy ends each stalled connection, which is how its probe returns.
    """

    def __init__(self, upstream, trigger=b"SELECT 1 AS ping"):
        self.upstream = upstream
        self.trigger = trigger
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(16)
        self.port = self.listener.getsockname()[1]
        self.socks: list[socket.socket] = []
        self.stalled = 0
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return
            server = socket.create_connection(self.upstream)
            self.socks += [client, server]
            state = {"stalled": False}
            threading.Thread(target=self._up, args=(client, server, state), daemon=True).start()
            threading.Thread(target=self._down, args=(server, client, state),
                             daemon=True).start()

    def _up(self, client, server, state):
        tail = b""
        try:
            while True:
                data = client.recv(65536)
                if not data:
                    return
                tail = (tail + data)[-4096:]
                if self.trigger in tail and not state["stalled"]:
                    state["stalled"] = True
                    self.stalled += 1
                server.sendall(data)
        except OSError:
            return

    def _down(self, server, client, state):
        try:
            while True:
                data = server.recv(65536)
                if not data:
                    return
                if not state["stalled"]:
                    client.sendall(data)
        except OSError:
            return

    def close(self):
        for s in [self.listener] + self.socks:
            try:
                s.close()
            except OSError:
                pass


def test_probes_stalled_mid_reply_do_not_fail_checks_after_the_database_recovers():
    """Review F1, measured the way review measured it: a real connection to the real
    database whose reply is lost after `SELECT 1 AS ping` goes out. dbconn.ping's connect
    and statement timeouts do not cover that, so each stalled probe's thread stays in recv
    until the connection ends. Two of them held both workers of the old fixed pool, and
    every later check answered 503 with the database healthy again: a load balancer evicts
    a serving node, which is the defect the route exists to prevent."""
    real = dbconn.dsn()
    parts = urlsplit(real)
    proxy = _StallProxy((parts.hostname, parts.port or 5432))
    host = parts.netloc.rsplit(":", 1)[0] if parts.port else parts.netloc
    stalled_dsn = urlunsplit(parts._replace(netloc=f"{host}:{proxy.port}"))
    try:
        stalled = []
        with override_settings(MESHY_DSN=stalled_dsn):
            for _ in range(2):
                t0 = time.monotonic()
                stalled.append((Client().get("/v1/livez").status_code,
                                round(time.monotonic() - t0, 1)))
                # Wait out the reuse window rather than resetting the route's state: the
                # stalled probes must stay counted, exactly as they would in production.
                time.sleep(REPROBED_AT_S)
        # Control: both probes really stalled mid-reply, on the ping itself.
        assert proxy.stalled == 2, f"the proxy saw {proxy.stalled} stalled pings, not 2"
        assert [s for s, _ in stalled] == [503, 503], stalled

        t0 = time.monotonic()
        after = Client().get("/v1/livez")                 # the database is healthy again
        elapsed = time.monotonic() - t0
    finally:
        proxy.close()                                     # ends the stalled connections

    assert after.status_code == 200, (
        f"with the database healthy, /v1/livez answered {after.status_code} after two "
        f"probes stalled mid-reply ({stalled}); the stalled probes are holding every slot")
    assert elapsed < 1.5, f"the recovered check took {elapsed:.1f}s"


def test_wedged_probes_below_the_bound_do_not_fail_the_next_check(monkeypatch):
    """The same property without a network, at the bound: every probe slot but one wedged,
    and once the database answers again the next check must still see 200. The database
    that is back answers at once: a real ping under a loaded gate missed the 0.3 s deadline
    once and answered 503, which is the database's latency, not a wedged slot."""
    release = threading.Event()
    wedged_calls = []
    back_calls = []
    monkeypatch.setattr(u, "LIVEZ_DEADLINE", 0.3)
    monkeypatch.setattr(u, "LIVEZ_REUSE_S", 0.0, raising=False)
    limit = getattr(u, "LIVEZ_MAX_PROBES", MAX_PROBES_CEILING)
    try:
        monkeypatch.setattr(u.dbconn, "ping", _gated_ping(wedged_calls, release, answer=False))
        firsts = [Client().get("/v1/livez").status_code for _ in range(limit - 1)]
        assert firsts == [503] * (limit - 1), firsts
        assert len(wedged_calls) == limit - 1, (
            f"control: {len(wedged_calls)} probes wedged, expected {limit - 1}")

        back = threading.Event()
        back.set()
        monkeypatch.setattr(u.dbconn, "ping", _gated_ping(back_calls, back))   # the database is back
        after = Client().get("/v1/livez")
    finally:
        release.set()

    assert len(back_calls) == 1, f"control: the next check ran {len(back_calls)} probes, expected 1"
    assert after.status_code == 200, (
        f"with {limit - 1} probes wedged and the database back, the next check answered "
        f"{after.status_code}; it waited behind stuck probes instead of probing")


def test_probe_threads_are_bounded_and_the_bound_frees_when_they_return(monkeypatch):
    """However many checks arrive while probes hang, at most LIVEZ_MAX_PROBES exist - each
    one a database connection an unauthenticated caller made this process open. At the
    bound a check answers 503 at once, and its probe never runs late."""
    release = threading.Event()
    calls = []
    monkeypatch.setattr(u, "LIVEZ_DEADLINE", 0.2)
    monkeypatch.setattr(u, "LIVEZ_REUSE_S", 0.0, raising=False)
    monkeypatch.setattr(u.dbconn, "ping", _gated_ping(calls, release))
    try:
        answers = []
        for _ in range(10):
            t0 = time.monotonic()
            answers.append((Client().get("/v1/livez").status_code, time.monotonic() - t0))
        started_while_hung = len(calls)
    finally:
        release.set()

    limit = u.LIVEZ_MAX_PROBES
    assert limit <= MAX_PROBES_CEILING, (
        f"LIVEZ_MAX_PROBES is {limit}; every one is a database connection an anonymous "
        f"caller can hold open, and the ceiling is {MAX_PROBES_CEILING}")
    assert started_while_hung == limit, (
        f"{started_while_hung} probes started for 10 checks while every probe hung; "
        f"the bound is {limit}")
    refused = answers[limit:]
    assert all(s == 503 and t < 0.15 for s, t in refused), (
        f"checks past the bound must answer 503 at once, without probing: {refused}")

    # The slots come back when the hung probes return, and nothing queued runs late.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and u._LIVEZ.alive() > 0:
        time.sleep(0.02)
    assert u._LIVEZ.alive() == 0, "the released probes never gave their slots back"
    assert len(calls) == limit, f"{len(calls) - limit} refused checks' probes ran late"
    again = Client().get("/v1/livez")
    assert again.status_code == 200 and len(calls) == limit + 1, (again.status_code, len(calls))


def test_concurrent_checks_share_one_probe_and_reuse_its_answer_briefly(monkeypatch):
    """Review F2: every check opened its own database connection, two at a time, so ~60
    unauthenticated requests a second queued the load balancer's own check past its
    deadline. Checks now share the probe in flight, and a finished answer is served again
    for about a second, so the database sees at most about one login a second from this
    route whatever the request rate."""
    gate = threading.Event()
    calls = []
    monkeypatch.setattr(u.dbconn, "ping", _gated_ping(calls, gate))

    def burst(n):
        out = []
        lock = threading.Lock()

        def one():
            r = Client().get("/v1/livez")
            with lock:
                out.append(r.status_code)
        threads = [threading.Thread(target=one) for _ in range(n)]
        for t in threads:
            t.start()
        return threads, out

    threads, first = burst(16)
    time.sleep(0.3)                  # every check is now waiting on the probe in flight
    gate.set()
    for t in threads:
        t.join(10)
    answered_at = time.monotonic()
    assert first == [200] * 16, first
    assert len(calls) == 1, f"16 concurrent checks ran {len(calls)} probes; they share one"

    time.sleep(max(0.0, answered_at + REUSED_AT_S - time.monotonic()))
    threads, second = burst(16)
    for t in threads:
        t.join(10)
    assert second == [200] * 16, second
    assert len(calls) == 1, (
        f"checks {REUSED_AT_S}s after an answer ran {len(calls) - 1} new probes; the "
        "answer is reused for about a second")

    time.sleep(max(0.0, answered_at + REPROBED_AT_S - time.monotonic()))
    third = Client().get("/v1/livez")
    assert third.status_code == 200 and len(calls) == 2, (
        f"a check {REPROBED_AT_S}s after the answer ran {len(calls) - 1} new probes; an "
        "answer older than the window must not be served, or a dead database reads live")


def test_a_hung_probe_does_not_hold_up_process_exit(tmp_path):
    """Review F8: probe workers were non-daemon pool threads, and Python joins those at
    exit, so a probe stuck mid-reply turned a graceful restart into a wait for SIGKILL."""
    script = tmp_path / "exit_probe.py"
    script.write_text(
        "import os, sys, time\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'meshychat.settings')\n"
        "os.environ.setdefault('MESHY_ALLOWED_HOSTS', 'testserver')\n"
        "import django\n"
        "django.setup()\n"
        "from django.test import RequestFactory\n"
        "import meshychat.urls as u\n"
        "from apps.core import dbconn\n"
        "dbconn.ping = lambda *a, **k: time.sleep(40) or True\n"
        "resp = u.livez(RequestFactory().get('/v1/livez'))\n"
        "print('ANSWERED', resp.status_code, flush=True)\n",
        encoding="utf-8")
    errlog = tmp_path / "exit_probe.err"
    with open(errlog, "w", encoding="utf-8") as err:
        proc = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE,
                                stderr=err, text=True, cwd=str(ROOT))
    try:
        line = ""
        for line in iter(proc.stdout.readline, ""):     # the child logs to stdout too
            if line.startswith("ANSWERED"):
                break
        answered = time.monotonic()
        try:
            rc = proc.wait(timeout=12)
        except subprocess.TimeoutExpired:
            rc = None
        exited_after = time.monotonic() - answered
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)
    assert line.split() == ["ANSWERED", "503"], (
        f"control: the child answered {line!r}; stderr tail: "
        f"{errlog.read_text(encoding='utf-8', errors='replace')[-800:]}")
    assert rc == 0, (
        f"a process whose /v1/livez probe was hung did not exit within 12 s of its last "
        f"answer (rc={rc}, {exited_after:.1f}s); it is waiting for the probe thread")


def test_the_probe_connection_carries_tcp_keepalives(monkeypatch):
    """An ABANDONED probe returns only when its connection ends. When the network lost the
    database's reply, keepalives are what end it, and so what gives the slot back. The
    answer to the check never depends on them: the deadline decides that.

    Structural by necessity: dropping packets to prove it needs a firewall change on the
    host. What is proved here is that the probe's connection asks libpq for them, on the
    same host, port, database and user as every other connection."""
    from psycopg.conninfo import conninfo_to_dict

    seen = []
    monkeypatch.setattr(u.dbconn, "ping", lambda target=None, **k: seen.append(target) or True)
    assert Client().get("/v1/livez").status_code == 200
    assert len(seen) == 1 and seen[0], f"the probe was not given a conninfo: {seen}"
    got = conninfo_to_dict(seen[0])
    want = conninfo_to_dict(dbconn.dsn())
    for key in ("host", "port", "dbname", "user"):
        assert got.get(key) == want.get(key), (key, got.get(key), want.get(key))
    assert got.get("keepalives") == "1", got
    for key in ("keepalives_idle", "keepalives_interval", "keepalives_count"):
        assert key in got and 1 <= int(got[key]) <= 10, (key, got.get(key))


# ---------------------------------------------------------------- 5. methods
@pytest.mark.parametrize("method", ["post", "put", "delete", "patch", "options"])
def test_livez_refuses_methods_other_than_get_and_head(method):
    resp = getattr(Client(), method)("/v1/livez")
    assert resp.status_code == 405, (
        f"{method.upper()} /v1/livez returned {resp.status_code}; the route is GET/HEAD "
        "only, exactly like /v1/health")
    allowed = {m.strip() for m in resp["Allow"].split(",")}
    assert allowed == {"GET", "HEAD"}, resp["Allow"]


def test_livez_answers_get_and_head():
    """POSITIVE CONTROL for the refusals above: a handler that 405s everything passes them."""
    client = Client()
    get = client.get("/v1/livez")
    head = client.head("/v1/livez")
    assert get.status_code == 200, get.status_code
    assert head.status_code == 200, head.status_code


def test_livez_is_exact_path_no_trailing_slash_redirect():
    from django.urls import resolve, reverse

    assert resolve("/v1/livez").url_name == "livez"
    assert reverse("livez") == "/v1/livez"
    assert Client().get("/v1/livez/").status_code == 404, (
        "APPEND_SLASH is off; /v1/livez/ must 404 like /v1/health/, not redirect")


# ---------------------------------------------------------------- 6. no auth
def test_livez_needs_no_credential_and_ignores_a_bad_one():
    client = Client()
    # Control: this anonymous client is refused by a peer-authenticated route, so the 200s
    # below are not an ambient identity leaking in from somewhere.
    guarded = client.get("/v1/peers/presence")
    assert guarded.status_code == 401, (
        f"control failed: an anonymous GET /v1/peers/presence answered {guarded.status_code}")

    anonymous = client.get("/v1/livez")
    assert anonymous.status_code == 200, (
        f"GET /v1/livez without a credential answered {anonymous.status_code}; a load "
        "balancer holds no peer key and no owner session")

    _forget()
    garbage = client.get("/v1/livez", HTTP_AUTHORIZATION="Bearer not-a-token",
                         HTTP_DPOP="not-a-proof")
    assert garbage.status_code == 200, (
        f"a bad credential turned /v1/livez into {garbage.status_code}; the route must not "
        "read credentials at all")


def test_livez_answers_a_load_balancer_probing_by_ip(monkeypatch):
    """A load balancer sends the node's IP as Host, which ALLOWED_HOSTS need not list.
    Nothing on this route validates Host today; this keeps a later `request.get_host()`
    from evicting every node probed by address.

    The database probe answers healthy here (28 Oct 2026, issue #451): this test is about the Host header, and a real
    probe that ran past LIVEZ_DEADLINE on a loaded gate box answered 503 (mg446b) for a reason unrelated to it. The
    probe's own deadline has its tests above."""
    from django.conf import settings
    from django.http.request import validate_host

    monkeypatch.setattr(u, "_livez_probe", lambda: True)
    host = "10.9.9.9:8765"
    # Control: the host really is outside ALLOWED_HOSTS, so get_host() would refuse it.
    assert not validate_host("10.9.9.9", settings.ALLOWED_HOSTS), settings.ALLOWED_HOSTS
    resp = Client().get("/v1/livez", HTTP_HOST=host)
    assert resp.status_code == 200, (
        f"GET /v1/livez with Host {host} answered {resp.status_code}")


# ---------------------------------------------------------------- 7. the documents
README = ROOT / "README.md"
DIVERGENCES = ROOT / "docs" / "DIVERGENCES.md"
_HEADING = re.compile(r"^## (?P<number>\d{3}), (?P<title>.*)$", re.M)
_CITATION = re.compile(r"(?:docs/DIVERGENCES\.md|[Dd]ivergence) (?P<number>\d{3})\b")


def _section(text: str, start: str) -> str:
    i = text.index(start)
    j = text.find("\n## ", i + len(start))
    return text[i:] if j < 0 else text[i:j]


def _livez_entry(text: str) -> tuple[int, str]:
    hits = [m for m in _HEADING.finditer(text) if "/v1/livez" in m["title"]]
    assert len(hits) == 1, f"expected one divergence heading about /v1/livez, found {len(hits)}"
    return int(hits[0]["number"]), _section(text, hits[0].group(0))


def test_readme_offers_livez_for_readiness_and_never_for_restarts():
    """Review F5. Kubernetes restarts a container whose liveness check fails; a restart
    cannot fix a database, so pointing one at this route restarts every pod in a loop for
    the length of a database outage. The README must say readiness and say not restarts."""
    section = _section(README.read_text(encoding="utf-8"),
                       "## Which health route a load balancer uses")
    assert "orchestrator's liveness check" not in section, (
        "the README still points a container orchestrator's liveness check at /v1/livez")
    assert "readiness" in section, "the README does not name the check /v1/livez is for"
    assert re.search(r"restart", section, re.I), (
        "the README does not say /v1/livez must not trigger restarts")


def test_divergence_entry_does_not_claim_the_request_paths_rls_guard():
    """Review F7. A request connection also passes `assert_serving_connection` (the RLS
    control on the request path); the probe does not. The entry must not say otherwise."""
    _, entry = _livez_entry(DIVERGENCES.read_text(encoding="utf-8"))
    assert "opened the way request connections are opened" not in entry, (
        "the livez entry still says the probe's connection is opened like a request's")
    assert "assert_serving_connection" in entry, (
        "the livez entry does not say which request-path check the probe skips")


def _stale_livez_citations(div_text: str, sources: dict[str, str]):
    """[(file, line, cited)] for livez citations that do not name the livez entry."""
    number, _ = _livez_entry(div_text)
    seen: dict[str, int] = {}
    stale = []
    for name, text in sources.items():
        whole_file = name.endswith("test_livez.py")      # about nothing but this route
        for i, line in enumerate(text.splitlines(), 1):
            if not whole_file and "livez" not in line.lower():
                continue
            for m in _CITATION.finditer(line):
                seen[name] = seen.get(name, 0) + 1
                if int(m["number"]) != number:
                    stale.append((name, i, int(m["number"])))
    return number, seen, stale


def test_every_livez_divergence_citation_names_the_livez_entry():
    """Review F4. This entry will be renumbered when it lands (another entry took its number
    on main). tests/test_numbering_is_unique.py checks that a citation names SOME heading,
    so a citation left at the old number passes it by naming the entry that took the number.
    Here each citation of this route's entry, in the files that make one, must name the
    heading whose title is /v1/livez; so renumbering the heading alone turns this red."""
    div_text = DIVERGENCES.read_text(encoding="utf-8")
    sources = {p: (ROOT / p).read_text(encoding="utf-8")
               for p in ("meshychat/urls.py", "tests/test_livez.py", "README.md")}
    number, seen, stale = _stale_livez_citations(div_text, sources)
    # Control: the scanner sees a citation in every file that makes one.
    assert set(seen) == set(sources), (
        f"no livez citation found in {sorted(set(sources) - set(seen))}; a citation of this "
        "entry must sit on a line that names /v1/livez so this check can see it")
    assert not stale, f"citations that do not name the /v1/livez entry ({number:03d}): {stale}"

    # C2: the check refuses the renumbering it exists for - the heading moved, nothing else.
    moved = div_text.replace(f"## {number:03d}, ", f"## {number + 2:03d}, ", 1)
    assert moved != div_text
    _, _, stale_after = _stale_livez_citations(moved, sources)
    assert {name for name, _, _ in stale_after} == set(sources), stale_after


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--no-header", "-p", "no:cacheprovider"]))
