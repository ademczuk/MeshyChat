"""STATUS F12: `/v1/mesh/wait` parks on ONE subscriber per process and an in-process waiter
registry, not a poll per waiter.

WHAT WAS WRONG, on the code before this change. Every parked waiter re-queried the database
every POLL_SECS (2 s) until a row arrived or its deadline passed (apps/mesh/wait.py `park`,
`park_addressed`). Divergence 018 first shipped poll-only (no Redis nudge) and marked
per-waiter polling for replacement by one subscriber and an in-process waiter registry. That
needs no Redis: the Postgres NOTIFY the live push listens to (sql/036's `mesh_msg`, every
committed `msg/` insert into shared/mesh-chat) is the nudge.

WHAT IT IS NOW (apps/mesh/wake.py). One daemon thread per process holds one LISTEN connection
on `mesh_msg` and wakes every registered waiter but the row's author; a woken waiter re-reads
with the query it always ran. A waiter whose set the nudge covers (the addressed wait, which
reads shared/mesh-chat only; a plain wait whose readable namespaces are shared/mesh-chat
alone) does not poll while the subscriber is listening. A plain waiter that can read another
namespace, whose rows no trigger announces, keeps its 2 s re-query beside the nudge.
When the subscriber is not listening (not yet up, or its connection lost) every waiter polls,
and it is woken when LISTEN comes back, since a row committed while nobody listened was never
heard.

Helpers are tests/test_mesh_wait_addressed.py's (its autouse fixtures apply here too).

Run:  .venv313/Scripts/python.exe -m pytest tests/test_mesh_wait_nudge.py -q
"""
from __future__ import annotations

import contextlib
import importlib
import json
import sys
import threading
import time
import uuid
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_mesh_wait_addressed import (  # noqa: E402,F401
    DSN, NS, WAIT, _anchor, _claim, _grant, _mesh_owner, _pair, _peer, _row, _wait, client,
    mesh_settings, no_roster, owner,
)
from django.test import Client  # noqa: E402
from tests.fixtures import history_rows  # noqa: E402
from tests.fixtures import signed_rows as sr  # noqa: E402


def _wake():
    return importlib.import_module("apps.mesh.wake")


def _park_in_thread(peer_id, **kw) -> tuple[threading.Thread, dict]:
    result: dict = {}

    def run():
        result["r"], result["took"] = _wait(Client(), peer_id, **kw)
        result["ended"] = time.monotonic()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, result


#: How far before a waiter's own deadline an answer must come to count as a wake (pxx 861d6d).
WAKE_MARGIN_S = 2.0


def _woken_within(answered_at: float, event_at: float, start: float, deadline_s: float, bound_s: float) -> bool:
    """An answer came soon enough after `event_at` to be a wake, never a deadline answer: within `bound_s` of the event,
    and at least WAKE_MARGIN_S before the waiter's own deadline (`start` + `deadline_s`). A waiter that was never woken
    answers at its deadline, where its last read finds the row, so a bound on time-from-the-event alone passes it
    whenever the event itself lands late (a slow box). The bound shrinks with the time left; it never passes a
    deadline answer, and never fails a prompt one just because the event was late, while 2 s are left."""
    return answered_at - event_at < min(bound_s, start + deadline_s - event_at - WAKE_MARGIN_S)


def test_the_wake_guard_never_passes_a_deadline_answer_and_passes_a_prompt_one():
    """pxx 861d6d: the guard behind the two wake bounds below, pinned without a database. A deadline answer (what W2,
    a nudge ignored, produces) fails it at every event time; a prompt answer passes while the margin is left."""
    start, deadline = 100.0, 20.0
    for tenths in range(0, 200):
        event_at = start + tenths / 10
        assert not _woken_within(start + deadline, event_at, start, deadline, 6.0), event_at
        left = start + deadline - event_at - WAKE_MARGIN_S
        if abs(left - 0.2) < 0.05:
            continue  # the exact boundary is float rounding, not behaviour
        assert _woken_within(event_at + 0.2, event_at, start, deadline, 6.0) == (left > 0.2), event_at


def _until(predicate, timeout: float = 10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _subscriber_if_any():
    """The process's subscriber, LISTENing; None on the code before F12, which has none (so
    the two tests that use this fail there on their own assertion, not on an import)."""
    try:
        wake = _wake()
    except ModuleNotFoundError:
        return None
    assert wake.SUBSCRIBER.ensure_started(ready_timeout=15.0), "no LISTEN within 15 s"
    _fresh_probe_schedule(wake)
    return wake


def _fresh_probe_schedule(wake) -> None:
    """The process's subscriber outlives a test, and so did its probe schedule: a probe heard
    in an EARLIER test set `_probe_next` PROBE_EVERY_SECS (5 s) ahead, so a test that lowers
    PROBE_EVERY_SECS sent no probe until that had passed (review LOW-1 of integ/r14: `heard`
    0 or 1 of 3, 2 of 9 whole-file runs). Wait until no waiter is parked and no probe is in
    flight (no probe goes out while nobody waits, so none can start after this), then make
    the next probe due at once: the test's own first park sends it, and each one heard after
    it is scheduled by the PROBE_EVERY_SECS the test set."""
    sub = wake.SUBSCRIBER
    assert _until(lambda: wake.REGISTRY.count() == 0 and sub._probe_token is None, 15.0), (
        wake.REGISTRY.count(), sub._probe_token)
    sub._probe_next = 0.0


@pytest.fixture()
def listening():
    """The process's subscriber, started and LISTENing (the first park starts it too), with a
    fresh probe schedule (`_fresh_probe_schedule`)."""
    wake = _wake()
    assert wake.SUBSCRIBER.ensure_started(ready_timeout=15.0), "no LISTEN within 15 s"
    _fresh_probe_schedule(wake)
    yield wake


def test_a_parked_addressed_wait_is_woken_by_the_nudge_not_a_poll(client, owner,
                                                                  monkeypatch):
    """The poll interval made longer than the wait (30 s against 10), so only a nudge can
    wake it: a row for the receiver written at 0.5 s is answered well before the deadline.
    Before F12 the waiter slept its poll interval and answered 204 at 10 s. CONTROL: the
    waiter registry is empty again once the wait has answered (no leaked registration)."""
    wake = _subscriber_if_any()
    monkeypatch.setattr(WAIT, "POLL_SECS", 30.0)
    mesh, sender, receiver = _pair(owner)
    since = _anchor(client, receiver)
    t, result = _park_in_thread(receiver["peer_id"], since=since, timeout=10)
    time.sleep(0.8)                              # parked (its first read found nothing)
    row = _row(owner, sender, {"to": receiver["name"]})
    t.join(15)
    assert not t.is_alive(), "the waiter never returned"
    r = result["r"]
    assert r.status_code == 200, (r.status_code, r.content[:300])
    assert [m["key"] for m in json.loads(r.content)["messages"]] == [row["key"]]
    assert result["took"] < 5.0, f"answered at {result['took']:.2f}s: not the nudge"
    assert _until(lambda: wake.REGISTRY.count() == 0, 5.0), wake.REGISTRY.count()


def test_a_plain_wait_that_reads_only_the_mesh_is_woken_by_the_nudge(client, owner,
                                                                     monkeypatch):
    """The plain route, for a peer whose only readable namespace is shared/mesh-chat: the
    nudge covers its whole set, so it parks without polling and a row wakes it at once
    (poll interval 30 s, wait 10 s). Before F12: 204 at 10 s."""
    _subscriber_if_any()
    monkeypatch.setattr(WAIT, "POLL_SECS", 30.0)
    mesh, sender, receiver = _pair(owner)
    t, result = _park_in_thread(receiver["peer_id"], timeout=10, addressed=None)
    time.sleep(0.8)                              # parked (its first read found nothing)
    row = _row(owner, sender, {"to": "anyone"})
    t.join(15)
    assert not t.is_alive(), "the waiter never returned"
    r = result["r"]
    assert r.status_code == 200, (r.status_code, r.content[:300])
    assert row["key"] in [m["key"] for m in json.loads(r.content)["messages"]]
    assert result["took"] < 5.0, f"answered at {result['took']:.2f}s: not the nudge"


def test_a_plain_wait_that_reads_another_namespace_keeps_polling_for_it(client, owner,
                                                                        monkeypatch,
                                                                        listening):
    """No trigger announces a row outside shared/mesh-chat, so a plain waiter that can read
    another namespace keeps the poll (0.2 s here): a row there, which no nudge will ever
    report, still wakes it. The negative beside it: with the poll at 30 s the same row is
    not seen before a 3 s deadline (204), which is why the poll is kept."""
    ns2 = f"shared/mesh-chat-nudge-{uuid.uuid4().hex[:8]}"
    mesh, sender, receiver = _pair(owner)
    _claim(owner, ns2, sender)
    _grant(owner, ns2, receiver, sender)

    monkeypatch.setattr(WAIT, "POLL_SECS", 30.0)
    # 6 s park and 1.5 s before the row (were 3 s and 0.5 s): on a loaded gate box the park's own gap re-read ran later
    # than 0.5 s after registering and read the row, 200 where 204 (mg446b, issue #451). The poll stays 30 s, so the
    # row is still unseen before the deadline unless something but the poll wakes the waiter.
    t, result = _park_in_thread(receiver["peer_id"], timeout=6, addressed=None)
    assert _until(lambda: listening.REGISTRY.count() >= 1), "the waiter never registered"
    time.sleep(1.5)                              # past the park's own gap re-read
    _row(owner, sender, {"to": "anyone"}, ns=ns2)
    t.join(15)
    assert result["r"].status_code == 204, "a row no nudge reports woke a 30 s poll"

    monkeypatch.setattr(WAIT, "POLL_SECS", 0.2)
    # A 25 s park and a 15 s bound (were 10 and 5): on a loaded gate box the 0.2 s poll's
    # answer took 7.4 s (mg446d, issue #451). A waiter the poll never woke still ends in a 204
    # at 25 s, which the status check below catches.
    t, result = _park_in_thread(receiver["peer_id"], timeout=25, addressed=None)
    assert _until(lambda: listening.REGISTRY.count() >= 1), "the waiter never registered"
    time.sleep(0.3)
    row = _row(owner, sender, {"to": "anyone"}, ns=ns2)
    t.join(35)
    r = result["r"]
    assert r.status_code == 200, (r.status_code, r.content[:300])
    assert [m["key"] for m in json.loads(r.content)["messages"]] == [row["key"]]
    assert result["took"] < 15.0, result["took"]


def test_many_waiters_share_one_listen_connection(client, owner, monkeypatch, listening):
    """The no-poll resolution uses one LISTENing connection for four parked waiters.

    Each waiter is in the registry while it is parked, and none is once it has answered.
    """
    monkeypatch.setattr(WAIT, "POLL_SECS", 30.0)
    mesh, sender, _receiver = _pair(owner)
    receivers = [_peer(owner, f"many-{i}") for i in range(4)]
    threads = []
    for p in receivers:
        _grant(owner, NS, p, mesh)
        threads.append(_park_in_thread(p["peer_id"], since=_anchor(client, p), timeout=10))
    assert _until(lambda: listening.REGISTRY.count() >= 4), listening.REGISTRY.count()
    with psycopg.connect(DSN, autocommit=True) as c:
        n = c.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                      " AND application_name = %s",
                      (listening.APPLICATION_NAME,)).fetchone()[0]
    assert n == 1, f"{n} subscriber connections for 4 waiters"
    _row(owner, sender, {"to": "all"})
    for t, result in threads:
        t.join(15)
        assert result["r"].status_code == 200, result["r"].content[:300]
    assert _until(lambda: listening.REGISTRY.count() == 0, 5.0)


def test_a_lost_subscriber_drops_its_waiters_to_the_poll_and_wakes_them(client, owner,
                                                                         monkeypatch,
                                                                         listening):
    """The subscriber's LISTEN connection is killed by the database while a waiter is parked
    without polling. The waiter is woken, polls (0.2 s) while nobody listens, and a row
    committed in that gap, which no NOTIFY reaches, is still answered. Then the subscriber
    comes back by itself. CONTROL: before the kill it was listening."""
    monkeypatch.setattr(WAIT, "POLL_SECS", 0.2)
    # The subscriber stays down 10 s: long enough that only the LOSS can have woken the
    # waiter into polling before the answer (< 8 s), not the reconnect's own wake.
    monkeypatch.setattr(listening, "RESTART_DELAY_SECS", 10.0)
    mesh, sender, receiver = _pair(owner)
    since = _anchor(client, receiver)
    assert listening.SUBSCRIBER.is_listening()
    t, result = _park_in_thread(receiver["peer_id"], since=since, timeout=15)
    assert _until(lambda: listening.REGISTRY.count() >= 1), "the waiter never registered"
    time.sleep(0.5)                              # past the park's own gap re-read
    with psycopg.connect(DSN, autocommit=True) as c:
        killed = c.execute("SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity "
                           "WHERE datname = current_database() AND application_name = %s",
                           (listening.APPLICATION_NAME,)).fetchone()[0]
    assert killed == 1, killed
    assert _until(lambda: not listening.SUBSCRIBER.is_listening(), 5.0), "never noticed"
    row = _row(owner, sender, {"to": receiver["name"]})
    t.join(20)
    r = result["r"]
    assert r.status_code == 200, (r.status_code, r.content[:300])
    assert [m["key"] for m in json.loads(r.content)["messages"]] == [row["key"]]
    assert result["took"] < 8.0, result["took"]
    assert _until(listening.SUBSCRIBER.is_listening, 25.0), "the subscriber never came back"


def test_a_nudged_wait_is_charged_once(client, owner, monkeypatch, listening):
    """B12 (r13) charges each /v1/mesh/* request against the peer's CALLS bucket once. A wait
    that parks and is woken by a nudge, and re-reads, is still ONE charge: the park never
    re-admits. CONTROL: a second request is a second charge."""
    from apps.core import peer_limit
    charged: list = []
    real = peer_limit.admit

    def counting(peer_id, *a, **k):
        charged.append(str(peer_id))
        return real(peer_id, *a, **k)

    monkeypatch.setattr(peer_limit, "admit", counting)
    monkeypatch.setattr(WAIT, "POLL_SECS", 30.0)
    mesh, sender, receiver = _pair(owner)
    since = _anchor(client, receiver)
    me = str(receiver["peer_id"])
    before = charged.count(me)
    t, result = _park_in_thread(receiver["peer_id"], since=since, timeout=10)
    assert _until(lambda: listening.REGISTRY.count() >= 1)
    _row(owner, sender, {"to": "somebody-else"})       # a nudge that finds nothing for it
    time.sleep(0.5)
    _row(owner, sender, {"to": receiver["name"]})
    t.join(15)
    assert result["r"].status_code == 200, result["r"].content[:300]
    assert charged.count(me) - before == 1, charged.count(me) - before
    _wait(client, receiver["peer_id"], since=since, timeout=0)
    assert charged.count(me) - before == 2


def test_the_registry_wakes_everyone_but_the_author():
    """Unit: a nudge for a row by peer A sets the event of every registered waiter except
    A's (its own row is never answered to it); `wake_all` sets every one; unregistering
    removes it. CONTROL: an unrelated waiter registered beside them is woken."""
    wake = _wake()
    reg = wake.Registry()
    a, b = uuid.uuid4(), uuid.uuid4()
    wa, wb, wb2 = reg.register(a), reg.register(b), reg.register(b)
    assert reg.count() == 3
    assert reg.wake(a) == 2
    assert not wa.event.is_set() and wb.event.is_set() and wb2.event.is_set()
    wb.event.clear()
    wb2.event.clear()
    assert reg.wake_all() == 3 and wa.event.is_set()
    reg.unregister(wb2)
    reg.unregister(wb2)                       # twice is harmless
    assert reg.count() == 2


def test_a_covered_waiter_does_not_poll_while_the_subscriber_listens(client, owner,
                                                                     monkeypatch, listening):
    """The no-poll property is measured by counting re-reads. A plain waiter that reads only
    shared/mesh-chat, idle for 5 s with the poll interval at 0.1 s, re-reads once (the gap
    re-read after it registers) and then only on a nudge: here, none. CONTROL: the same wait
    for a waiter that can also read another namespace re-reads every 0.1 s. Its floor is twice
    the covered waiter's ceiling, not 5 s / 0.1 s: a loaded gate box measured about 400 ms a
    round trip, and the control only has to show that polling happens. The waits were 2 s, where
    that box made 5 re-reads, and once 3 (mg463m, 29 Oct): 5 s keeps the floor at a quarter of
    what the slow box reads."""
    monkeypatch.setattr(WAIT, "POLL_SECS", 0.1)
    reads: dict = {}
    real = WAIT._poll_once

    def counting(peer_id, since):
        reads[str(peer_id)] = reads.get(str(peer_id), 0) + 1
        return real(peer_id, since)

    monkeypatch.setattr(WAIT, "_poll_once", counting)
    mesh, sender, covered = _pair(owner)
    other = _peer(owner, "other-ns")
    _grant(owner, NS, other, mesh)
    ns2 = f"shared/mesh-chat-nudge-{uuid.uuid4().hex[:8]}"
    _claim(owner, ns2, sender)
    _grant(owner, ns2, other, sender)
    for p in (covered, other):
        r, _ = _wait(client, p["peer_id"], timeout=5, addressed=None)
        assert r.status_code == 204, (r.status_code, r.content[:200])
    assert reads.get(str(covered["peer_id"]), 0) <= 2, reads
    assert reads.get(str(other["peer_id"]), 0) >= 4, reads


def test_a_row_committed_between_the_first_read_and_the_registration_is_answered_at_once(
        client, owner, monkeypatch, listening):
    """Review finding 5 of F12: the park's gap re-read, the one thing that closes the race
    between the first read and the registration. The row is written in exactly that window
    (inside `register`, before the waiter is added) and its NOTIFY is HEARD by the
    subscriber before the waiter exists, so no nudge can ever reach it; the poll interval
    is 30 s against a 6 s wait. It is answered at once, by the gap re-read. Without it the
    waiter waits for a nudge that already went by and answers 204 at its deadline. CONTROL:
    the row was announced and heard before the waiter registered."""
    monkeypatch.setattr(WAIT, "POLL_SECS", 30.0)
    mesh, sender, receiver = _pair(owner)
    since = _anchor(client, receiver)
    real = listening.REGISTRY.register
    planted: dict = {}

    def register(peer_id, *args, **kwargs):
        if str(peer_id) == str(receiver["peer_id"]) and "row" not in planted:
            heard = listening.SUBSCRIBER.stats().get("notifications", 0)
            planted["row"] = _row(owner, sender, {"to": receiver["name"]})
            planted["heard"] = _until(
                lambda: listening.SUBSCRIBER.stats().get("notifications", 0) > heard, 5.0)
            planted["waiters"] = listening.REGISTRY.count()
            planted["at"] = time.monotonic()
        return real(peer_id, *args, **kwargs)

    monkeypatch.setattr(listening.REGISTRY, "register", register)
    # Timed from the plant, not from the request: `took` also counts the wait above for the NOTIFY to be heard, which
    # under load took most of it (5.68 s against 3.0 on main's gate 675924fb, 29 Oct). The wait is 20 s; without the
    # gap re-read the answer comes at the deadline, which _woken_within never passes, however late the plant landed.
    started = time.monotonic()
    r, took = _wait(client, receiver["peer_id"], since=since, timeout=20)
    answered = time.monotonic()
    assert planted.get("heard"), "the row's NOTIFY was not heard before the registration"
    assert r.status_code == 200, (r.status_code, r.content[:300], took)
    assert [m["key"] for m in json.loads(r.content)["messages"]] == [planted["row"]["key"]]
    since_plant = answered - planted["at"]
    assert _woken_within(answered, planted["at"], started, 20.0, 5.0), (
        f"answered {since_plant:.2f}s after the plant ({took:.2f}s in all, plant at "
        f"{planted['at'] - started:.2f}s): not by the gap re-read")


# ===================================================================== review finding 3
#
# A LISTEN connection whose TCP path stalls without closing (a NAT timeout, a VM network
# hiccup) raises nothing: `notifies()` just returns empty, `is_listening()` stays True, and
# every waiter that waits for a nudge alone waits to its deadline. The subscriber now proves
# the path end to end: while waiters are parked it sends itself a NOTIFY on a channel of its
# own, and one not heard back in time means LISTEN is lost.

class _FreezableProxy:
    """A TCP proxy on 127.0.0.1 to the test database that can be FROZEN: no byte moves in
    either direction and nothing is closed (a new connection is accepted and then gets no
    answer either), the half-open path the finding describes."""

    def __init__(self, host: str, port: int):
        import socket
        self.upstream = (host, port)
        self.frozen = threading.Event()
        self.closed = threading.Event()
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(16)
        self.port = self.server.getsockname()[1]
        self.sockets: list = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        import socket
        while not self.closed.is_set():
            try:
                client, _ = self.server.accept()
            except OSError:
                return
            upstream = socket.create_connection(self.upstream)
            self.sockets += [client, upstream]
            for a, b in ((client, upstream), (upstream, client)):
                threading.Thread(target=self._pump, args=(a, b), daemon=True).start()

    def _pump(self, src, dst):
        import select
        while not self.closed.is_set():
            if self.frozen.is_set():
                time.sleep(0.05)
                continue
            try:
                ready, _, _ = select.select([src], [], [], 0.05)
                if not ready:
                    continue
                data = src.recv(65536)
                if not data:
                    return
                dst.sendall(data)
            except OSError:
                return

    def close(self):
        self.closed.set()
        for s in [self.server, *self.sockets]:
            try:
                s.close()
            except OSError:
                pass


def test_a_listen_connection_that_stalls_without_closing_is_found_and_waiters_poll(
        client, owner, monkeypatch):
    """The reviewer's repro as a test. A subscriber of its own LISTENs through a proxy that
    is then frozen; an addressed waiter parks (a 12 s wait, the poll at 2 s) and a row for it
    lands at 0.8 s, which no NOTIFY can bring through the frozen path. The subscriber finds
    its LISTEN deaf (its own probe does not come back), drops its waiters to the poll, and
    the row is answered in seconds. Before this change: 204 at the deadline, the subscriber
    still reporting it was listening. CONTROL: before the freeze it was listening, and
    through the same proxy unfrozen a nudge arrives."""
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    wake = _wake()
    target = conninfo_to_dict(DSN)
    proxy = _FreezableProxy(target.get("host", "127.0.0.1"), int(target.get("port", 5432)))
    registry = wake.Registry()
    sub = wake.Subscriber(registry, dsn=make_conninfo(DSN, host="127.0.0.1",
                                                      port=str(proxy.port)))
    monkeypatch.setattr(wake, "REGISTRY", registry)
    monkeypatch.setattr(wake, "SUBSCRIBER", sub)
    monkeypatch.setattr(wake, "PROBE_EVERY_SECS", 0.5, raising=False)
    monkeypatch.setattr(wake, "PROBE_TIMEOUT_SECS", 1.0, raising=False)
    monkeypatch.setattr(wake, "RESTART_DELAY_SECS", 30.0)
    monkeypatch.setattr(WAIT, "POLL_SECS", 2.0)
    try:
        assert sub.ensure_started(ready_timeout=15.0), "no LISTEN through the proxy"
        mesh, sender, receiver = _pair(owner)
        since = _anchor(client, receiver)
        heard = sub.stats().get("notifications", 0)
        _row(owner, sender, {"to": "somebody-else"})
        assert _until(lambda: sub.stats().get("notifications", 0) > heard, 5.0), \
            "control: no nudge through the unfrozen proxy"

        proxy.frozen.set()
        t, result = _park_in_thread(receiver["peer_id"], since=since, timeout=12)
        time.sleep(0.8)
        row = _row(owner, sender, {"to": receiver["name"]})
        t.join(20)
        assert not t.is_alive(), "the waiter never returned"
        r = result["r"]
        assert r.status_code == 200, (r.status_code, r.content[:300], result["took"],
                                      sub.is_listening(), sub.stats())
        assert [m["key"] for m in json.loads(r.content)["messages"]] == [row["key"]]
        assert result["took"] < 6.0, (result["took"], sub.stats())
        # Found deaf within 10 s, not at this instant: the waiter can be answered (by the 2 s
        # poll) before the 1 s probe has timed out, and on a loaded gate box it was, with the
        # subscriber still reporting listening (mg446h, issue #451). One that never notices
        # the frozen path still fails here.
        assert _until(lambda: not sub.is_listening(), 10.0), sub.stats()
    finally:
        sub.stop(timeout=5.0)
        proxy.close()


def test_a_listen_connection_that_hears_is_never_taken_for_deaf(client, owner, monkeypatch,
                                                                listening):
    """The positive control beside the one above: the process's own subscriber, healthy,
    probing every 0.2 s (1 s to come back) while a waiter is parked for 3 s. Every probe is
    heard, none is overdue, LISTEN is never dropped, and the waiter answers 204 at its
    deadline as an idle wait should, having waited on the nudge alone.

    The subscriber sends a probe only between two `notifies()` windows of NOTIFY_POLL_SECS
    (1 s), so at the default window "every 0.2 s" was one probe a second: three windows in
    the 3 s park, the third probe heard only when the first window closed early enough in
    it (review LOW-1 of integ/r14: `heard` 2 with no earlier probe in the process). The
    window is 0.2 s here too, so the probes go at the 0.2 s this test names.

    The probe's time to come back is 2.5 s and the park 6 s (28 Oct 2026). At 1 s and 3 s this healthy control failed
    under a full gate's load with deaf 1 (mg446b and mg440a, landing gates of two PRs that touch neither this file nor
    the subscriber) and passed alone on a quiet box (48 of 48 in 91 s): one probe answered in over 1 s on a loaded box
    is load, not a deaf LISTEN. The timeout stays shorter than the park, so a probe that is really lost is still
    overdue inside it and the deaf counter would move; the test above this one proves that it does."""
    monkeypatch.setattr(listening, "NOTIFY_POLL_SECS", 0.2)
    monkeypatch.setattr(listening, "PROBE_EVERY_SECS", 0.2)
    monkeypatch.setattr(listening, "PROBE_TIMEOUT_SECS", 2.5)
    monkeypatch.setattr(WAIT, "POLL_SECS", 30.0)
    mesh, sender, receiver = _pair(owner)
    since = _anchor(client, receiver)
    before = listening.SUBSCRIBER.stats()
    r, took = _wait(client, receiver["peer_id"], since=since, timeout=6)
    after = listening.SUBSCRIBER.stats()
    assert r.status_code == 204, (r.status_code, r.content[:300])
    heard = after.get("probes_heard", 0) - before.get("probes_heard", 0)
    assert heard >= 3, (before, after)
    assert after.get("deaf", 0) == before.get("deaf", 0), (before, after)
    assert after.get("listens", 0) == before.get("listens", 0), "LISTEN was re-made"
    assert listening.SUBSCRIBER.is_listening()


# ===================================================================== review finding 4
#
# One message woke every parked waiter but its author, and each opened a connection and
# re-scanned at the same instant: up to MAX_WAITERS - 1 connections for one row, and past
# max_connections the re-reads raised inside the park, a 500. The addressed waiters are now
# woken only for a row the push would send them (the subscriber reads the row's addressing
# once, as the push does), and a re-read that fails keeps the waiter waiting.

def _counting_scans(monkeypatch, at: dict | None = None) -> dict:
    """Each peer's number of addressed scans; `at`, when given, collects when each began."""
    counts: dict = {}
    real = WAIT.scan_addressed

    def counting(conn, peer_id, *args, **kwargs):
        counts[str(peer_id)] = counts.get(str(peer_id), 0) + 1
        if at is not None:
            at.setdefault(str(peer_id), []).append(time.monotonic())
        return real(conn, peer_id, *args, **kwargs)

    monkeypatch.setattr(WAIT, "scan_addressed", counting)
    return counts


def test_a_row_for_one_addressed_waiter_wakes_that_waiter_alone(client, owner, monkeypatch,
                                                                listening):
    """The reviewer's repro: 8 addressed receivers parked (poll 30 s, wait 4 s) and one row
    addressed to receiver 0. Receiver 0 answers 200 with it at once; the other 7 scan only
    their first read and their gap re-read, and answer 204 at their deadline: the row did
    not wake them. Before this change each of the 7 was woken and re-scanned too (3 scans
    each). CONTROL: a broadcast wakes, and is answered to, every one of them."""
    monkeypatch.setattr(WAIT, "POLL_SECS", 30.0)
    mesh, sender, _ = _pair(owner)
    receivers = [_peer(owner, f"herd-{i}") for i in range(8)]
    for p in receivers:
        _grant(owner, NS, p, mesh)
    anchors = [_anchor(client, p) for p in receivers]
    at: dict = {}
    counts = _counting_scans(monkeypatch, at)
    parking = listening.SUBSCRIBER.stats()
    # 12 s parks and 20 s to register (were 4 s and 10 s): on a loaded gate box the first parks reached their 4 s
    # deadline before all 8 had registered, so the count never reached 8 (mg446b, mg433a; issue #451). The poll stays
    # 30 s, so the 7 unaddressed waiters still scan exactly twice and answer 204 at their deadline.
    # 20 s parks (were 12 s): woken is measured from the row's write below, and the deadline must stay far beyond that
    # bound, so a waiter that was never woken (answered at its deadline, where the last read finds the row) still fails.
    parked_at = time.monotonic()
    threads = [_park_in_thread(p["peer_id"], since=a, timeout=20)
               for p, a in zip(receivers, anchors)]
    assert _until(lambda: listening.REGISTRY.count() >= 8, 20.0), listening.REGISTRY.count()
    assert _until(lambda: all(counts.get(str(p["peer_id"]), 0) >= 2 for p in receivers), 20.0)
    before = listening.SUBSCRIBER.stats()
    row_at = time.monotonic()
    row = _row(owner, sender, {"to": receivers[0]["name"]})
    for t, _ in threads:
        t.join(30)
    after = listening.SUBSCRIBER.stats()
    # What the subscriber did while they were parked, for a failure's message: a wake of
    # every addressed waiter comes from an unread addressing (`addressing_errors`,
    # `addressing_unknown`), a LISTEN lost or made again (`deaf`, `listen_errors`,
    # `listens`), an undecodable payload, or a notification besides this row's.
    moved = {k: after.get(k, 0) - before.get(k, 0) for k in set(before) | set(after)
             if after.get(k, 0) != before.get(k, 0)}
    statuses = [result["r"].status_code for _, result in threads]
    assert statuses == [200] + [204] * 7, (statuses, moved)
    first = json.loads(threads[0][1]["r"].content)
    assert [m["key"] for m in first["messages"]] == [row["key"]]
    # Woken by the row, not by its deadline: answered within 6 s of the row's write. Timed from the row: `took` runs
    # from the park's start and also counts the wait for all 8 to register, so a bound on it failed under load ("< 3.0"
    # at 3.2 and 3.9 s, mg433c, mg446j, issue #451; "< 11.0" at 12.2 s on main's gate 675924fb, 29 Oct, where the
    # 12 s park was so close that a deadline answer and a late wake looked alike). A waiter never woken answers at
    # its 20 s deadline, which _woken_within never passes, however late the row landed (pxx 861d6d).
    woke = threads[0][1]
    after_row = woke["ended"] - row_at
    assert _woken_within(woke["ended"], row_at, parked_at, 20.0, 6.0), (after_row, row_at - parked_at, woke["took"], moved)
    assert woke["ended"] - row_at < 6.0, (woke["ended"] - row_at, moved)
    others = [counts.get(str(p["peer_id"]), 0) for p in receivers[1:]]
    # For a failure: each extra scan's start relative to the row's write (a wake by the row
    # scans at about +0; a re-read at the deadline at about +12 less the parking), and what
    # the subscriber did from before the parks began.
    extra = {i + 1: [round(t - row_at, 3) for t in at.get(str(p["peer_id"]), [])]
             for i, p in enumerate(receivers[1:]) if counts.get(str(p["peer_id"]), 0) != 2}
    since_parking = {k: after.get(k, 0) - parking.get(k, 0) for k in set(parking) | set(after)
                     if after.get(k, 0) != parking.get(k, 0)}
    assert others == [2] * 7, (others, counts.get(str(receivers[0]["peer_id"])), moved,
                               extra, since_parking)

    anchors = [_anchor(client, p) for p in receivers]
    broadcast_parked_at = time.monotonic()
    threads = [_park_in_thread(p["peer_id"], since=a, timeout=20)
               for p, a in zip(receivers, anchors)]
    assert _until(lambda: listening.REGISTRY.count() >= 8, 20.0), listening.REGISTRY.count()
    time.sleep(0.3)
    everyone_at = time.monotonic()
    everyone = _row(owner, sender, {"to": "all"})
    for t, result in threads:
        t.join(30)
        assert result["r"].status_code == 200, result["r"].status_code
        assert everyone["key"] in [m["key"] for m in json.loads(result["r"].content)["messages"]]
        assert _woken_within(result["ended"], everyone_at, broadcast_parked_at, 20.0, 6.0), (
            result["ended"] - everyone_at, everyone_at - broadcast_parked_at, result["took"])
        assert result["ended"] - everyone_at < 6.0, result["ended"] - everyone_at


def test_a_plain_waiter_is_still_woken_by_every_row(client, owner, monkeypatch, listening):
    """The plain wait is addressing-agnostic: every readable row is its, so the
    narrowing above never applies to it. A row addressed to somebody else wakes and answers
    a plain waiter that reads only the mesh (poll 30 s, wait 10 s)."""
    monkeypatch.setattr(WAIT, "POLL_SECS", 30.0)
    mesh, sender, receiver = _pair(owner)
    t, result = _park_in_thread(receiver["peer_id"], timeout=10, addressed=None)
    assert _until(lambda: listening.REGISTRY.count() >= 1)
    time.sleep(0.3)
    row = _row(owner, sender, {"to": "somebody-else"})
    t.join(15)
    r = result["r"]
    assert r.status_code == 200, (r.status_code, r.content[:300])
    assert row["key"] in [m["key"] for m in json.loads(r.content)["messages"]]
    assert result["took"] < 5.0, result["took"]


def test_a_re_read_the_database_refuses_keeps_the_waiter_waiting(owner, monkeypatch,
                                                                 listening):
    """Past max_connections a woken waiter's re-read raises (psycopg's OperationalError).
    The park no longer lets that escape as a 500: the waiter polls on, and a row that lands
    once the database answers again is answered. Here the park's first two re-reads raise.
    Before this change: 500. CONTROL: the first read (on the request's own connection) was
    not touched, and the failing re-reads really ran."""
    monkeypatch.setattr(WAIT, "POLL_SECS", 0.2)
    mesh, sender, receiver = _pair(owner)
    raising = Client(raise_request_exception=False)
    since = _anchor(raising, receiver)
    calls = {"n": 0}
    real = WAIT.scan_addressed

    def flaky(conn, peer_id, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] in (2, 3):                   # the gap re-read and the next one
            raise psycopg.OperationalError("sorry, too many clients already")
        return real(conn, peer_id, *args, **kwargs)

    monkeypatch.setattr(WAIT, "scan_addressed", flaky)
    result: dict = {}

    def run():
        result["r"], result["took"] = _wait(raising, receiver["peer_id"], since=since,
                                            timeout=8)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert _until(lambda: calls["n"] >= 4, 5.0), calls
    row = _row(owner, sender, {"to": receiver["name"]})
    t.join(15)
    r = result["r"]
    assert r.status_code == 200, (r.status_code, r.content[:300])
    assert [m["key"] for m in json.loads(r.content)["messages"]] == [row["key"]]


# ===================================================================== E21: the timed-out park
#
# When a waiter waits on the nudge alone, `event.wait(remaining)` can return a hair BEFORE the
# deadline by the park's own clock without the event set (a timer tick coarser than the
# clock; about 1 in 20 runs of the herd test above on HQD). The park then re-read anyway:
# one wasted scan per timed-out waiter, and the herd test's "2 scans each" saw 3. A waiter
# re-queries only on a poll tick or a nudge; an elapsed deadline answers 204 with no read. So a
# wait that returns with no nudge and no poll interval spent loops back without a read,
# and the event is cleared only when it WAS set, so a nudge that lands just after a timed-out
# wait returns is still there for the next one. These drive `_parked` on ONE scripted clock
# that only the scripted event moves: the early return is forced, not hoped for.

class _Clock:
    """The park's clock; only `_ScriptedEvent.wait` moves it."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _ScriptedEvent:
    """A waiter's event whose `wait(timeout)` follows a script, one step per call:
    (seconds the clock moves during the wait, a nudge lands DURING it, a nudge lands just
    AFTER it returned). It returns whether the event is set, as `threading.Event.wait` does.
    A call past the script's end fails the test (the park waited more often than scripted)."""

    def __init__(self, clock: _Clock, steps) -> None:
        self.clock, self.steps = clock, list(steps)
        self.flag = False
        self.timeouts: list = []

    def wait(self, timeout=None) -> bool:
        assert self.steps, f"an unscripted wait({timeout}) at {self.clock.now}"
        self.timeouts.append(timeout)
        advance, during, after = self.steps.pop(0)
        self.clock.now += advance
        if during:
            self.flag = True
        woke = self.flag
        if after:
            self.flag = True
        return woke

    def set(self) -> None:
        self.flag = True

    def clear(self) -> None:
        self.flag = False

    def is_set(self) -> bool:
        return self.flag


def _scripted_park(monkeypatch, steps, *, eff_timeout: float, nudged: bool = True,
                   listening: bool = True, answers=(), every_step: bool = True):
    """Run `_parked` with the scripted event and clock, the subscriber stubbed (listening or
    not) and a `read` that answers `answers` in order, then None. Returns (what the park
    returned, the number of reads, the event). `every_step`: the park must have waited
    through the whole script."""
    wake = _wake()
    clock = _Clock()
    event = _ScriptedEvent(clock, steps)
    real_register = wake.REGISTRY.register

    def register(peer_id, labels=None):
        waiter = real_register(peer_id, labels)
        waiter.event = event
        return waiter

    monkeypatch.setattr(wake.REGISTRY, "register", register)
    monkeypatch.setattr(wake.SUBSCRIBER, "ensure_started", lambda **kw: listening)
    monkeypatch.setattr(wake.SUBSCRIBER, "is_listening", lambda: listening)
    reads = {"n": 0}
    queue = list(answers)

    def read():
        reads["n"] += 1
        return queue.pop(0) if queue else None

    found = WAIT._parked(uuid.uuid4(), eff_timeout, read, nudged=nudged, clock=clock)
    assert not every_step or not event.steps, f"scripted waits left over: {event.steps}"
    return found, reads["n"], event


def test_a_nudge_only_wait_that_times_out_just_before_its_deadline_does_not_re_read(
        monkeypatch):
    """E21. A nudge-only waiter (4 s) whose wait returns 1 ms before the deadline with no
    nudge: the park waits out the last millisecond and answers None, having read ONCE (the
    gap re-read). On the code before E21 it read twice: the timed-out wait re-read."""
    found, reads, event = _scripted_park(
        monkeypatch, [(4.0 - 0.001, False, False), (0.001, False, False)], eff_timeout=4.0)
    assert found is None
    assert reads == 1, f"{reads} reads: a timed-out wait re-read"
    assert event.timeouts[0] == pytest.approx(4.0), event.timeouts   # waited for the nudge alone


def test_a_nudge_that_lands_as_a_timed_out_wait_returns_is_not_lost(monkeypatch):
    """E21's other half: the event is cleared only when it WAS set. The wait times out with no
    nudge and one lands just after it returned; the next wait sees it at once and the re-read
    answers the row. (A park that cleared unconditionally and skipped the re-read would lose
    that nudge and wait on past it: an unscripted wait.)"""
    found, reads, _ = _scripted_park(
        monkeypatch, [(1.0, False, True), (0.0, False, False)], eff_timeout=4.0,
        answers=[None, ["row"]], every_step=False)
    assert found == ["row"]
    assert reads == 2, reads


def test_a_nudge_during_the_wait_still_re_reads_control(monkeypatch):
    """CONTROL for E21: a nudge that lands DURING the wait is re-read at once and answered."""
    found, reads, event = _scripted_park(
        monkeypatch, [(0.5, True, False)], eff_timeout=4.0, answers=[None, ["row"]])
    assert found == ["row"]
    assert reads == 2, reads
    assert not event.is_set(), "the nudge re-read was not cleared"


def test_a_polling_wait_re_reads_each_interval_but_not_a_truncated_last_one(monkeypatch):
    """CONTROL for E21, the polling park (a subscriber not listening): each full POLL_SECS
    wait is a tick and re-reads; the last wait, cut short by the deadline (1 s left of a 2 s
    interval) and returning 1 ms early, is no tick because its next tick falls after the
    deadline, and does not re-read.
    Reads: the gap re-read and two ticks."""
    monkeypatch.setattr(WAIT, "POLL_SECS", 2.0)
    found, reads, event = _scripted_park(
        monkeypatch,
        [(2.0, False, False), (2.0, False, False), (1.0 - 0.001, False, False),
         (0.001, False, False)],
        eff_timeout=5.0, listening=False)
    assert found is None
    assert reads == 3, f"{reads} reads: the gap re-read and two ticks expected"
    assert event.timeouts[:3] == [2.0, 2.0, pytest.approx(1.0)], event.timeouts


# ===================================================================== E22: the addressing read
#
# Review MEDIUM-1 of integ/r14 (the project's design log (not part of this release), 018's finding-4 amendment): while any
# addressed waiter was parked, the subscriber read each NOTIFY's addressing SERIALLY on its
# one thread, a fresh guarded connection per row (about 67 ms each on HQD), so 150 unrelated
# rows held an addressed waiter's wake about 10 s, every plain waiter's wake queued behind the
# same reads, and a slow database could stall the LISTEN loop into a false deaf verdict. The
# subscriber now only QUEUES (key, author) and wakes the plain waiters at once; a thread of
# its own drains the queue and reads a whole group's addressing in ONE `key = ANY(...)` read
# on a persistent role-bound connection. An addressing it cannot read still wakes every
# addressed waiter.

_SUBSCRIBER_THREADS = ("meshy-mesh-wait-subscriber", "meshy-mesh-wait-addressing")


def _on_subscriber_side() -> bool:
    return threading.current_thread().name in _SUBSCRIBER_THREADS


def _gate_addressing_reads(monkeypatch, gate: threading.Event | None = None,
                           refuse: bool = False) -> dict:
    """Hold (until `gate` is set) or refuse every server_internal context the subscriber's
    side enters: the addressing read's, before and after E22 alike (a waiter's own re-read
    runs on its request's thread and is untouched). Returns {"entered": n}."""
    from apps.core import dbconn
    real = dbconn.request_server_internal
    seen = {"entered": 0}

    @contextlib.contextmanager
    def gated(conn):
        if _on_subscriber_side():
            seen["entered"] += 1
            if refuse:
                raise psycopg.OperationalError("addressing read refused (test)")
            if gate is not None:
                gate.wait(30.0)
        with real(conn) as c:
            yield c

    monkeypatch.setattr(dbconn, "request_server_internal", gated)
    return seen


def _count_subscriber_connects(monkeypatch) -> list:
    """Every connection the subscriber's side opens other than its LISTEN connection: the
    addressing reads' connections (the probe's are opened on a thread of their own)."""
    from apps.core import dbconn
    wake = _wake()
    real = dbconn.connect
    opened: list = []

    def counting(*args, **kwargs):
        if _on_subscriber_side() and kwargs.get("application_name") != wake.APPLICATION_NAME:
            opened.append(threading.current_thread().name)
        return real(*args, **kwargs)

    monkeypatch.setattr(dbconn, "connect", counting)
    return opened


def _park_timed(peer_id, **kw) -> tuple[threading.Thread, dict]:
    """`_park_in_thread`, also recording WHEN (time.monotonic) the wait answered."""
    result: dict = {}

    def run():
        result["r"], result["took"] = _wait(Client(), peer_id, **kw)
        result["at"] = time.monotonic()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, result


def test_an_addressed_waiter_is_not_queued_behind_a_burst_of_unrelated_rows(
        client, owner, monkeypatch, listening):
    """E22, the reviewer's repro: an addressed waiter parked (poll 30 s, wait 30 s), 150 rows
    for somebody else committed at once, then one row for it. It answers that row in well
    under 2 s of the row's commit; the subscriber's side opened at most ONE connection for
    the 151 rows' addressing (the persistent one, if an earlier test had not opened it) and
    read it in fewer reads than rows. Before E22: one fresh connection and one read per
    row, serially, so the wake waited behind 150 reads (the reviewer measured about 10 s)."""
    monkeypatch.setattr(WAIT, "POLL_SECS", 30.0)
    mesh, sender, receiver = _pair(owner)
    since = _anchor(client, receiver)
    counts = _counting_scans(monkeypatch)
    t, result = _park_timed(receiver["peer_id"], since=since, timeout=30)
    me = str(receiver["peer_id"])
    assert _until(lambda: counts.get(me, 0) >= 2), counts      # first read and gap re-read
    opened = _count_subscriber_connects(monkeypatch)
    before = listening.SUBSCRIBER.stats()
    with owner.transaction():
        for _ in range(150):
            _row(owner, sender, {"to": "somebody-else"})
    row_at = time.monotonic()
    row = _row(owner, sender, {"to": receiver["name"]})
    t.join(40)
    after = listening.SUBSCRIBER.stats()
    moved = {k: after.get(k, 0) - before.get(k, 0) for k in set(before) | set(after)
             if after.get(k, 0) != before.get(k, 0)}
    r = result["r"]
    assert r.status_code == 200, (r.status_code, r.content[:300], moved)
    assert [m["key"] for m in json.loads(r.content)["messages"]] == [row["key"]]
    assert result["at"] - row_at < 2.0, (
        f"answered {result['at'] - row_at:.2f}s after the row: behind the burst", moved)
    assert moved.get("notifications", 0) >= 151, moved                 # CONTROL: all heard
    assert len(opened) <= 1, (f"{len(opened)} addressing connections for 151 rows", moved)
    # Fewer reads than rows: a group is read at once (2 reads for the 151 on HQD), not one
    # read per row. (The code before E22 counts no reads; its latency above fails it.)
    assert moved.get("addressing_reads", 0) < 151, moved


def test_a_plain_waiter_is_never_queued_behind_an_addressing_read(client, owner, monkeypatch,
                                                                  listening):
    """E22: the addressing read HELD (a gate on the subscriber side's server_internal
    context) while an addressed waiter and a plain waiter are parked, and a row for somebody
    else committed. The plain waiter, which every row wakes, answers it while the read is
    still held. Before E22 its wake came after the read on the same thread, so it could not
    answer until the gate opened. CONTROL: the held read was really entered, and the
    addressed waiter, the row not being its, answers 204."""
    monkeypatch.setattr(WAIT, "POLL_SECS", 30.0)
    mesh, sender, addressed = _pair(owner)
    plain = _peer(owner, "plain")
    _grant(owner, NS, plain, mesh)
    since = _anchor(client, addressed)
    counts = _counting_scans(monkeypatch)
    reads: dict = {}
    real_poll = WAIT._poll_once

    def counting_poll(peer_id, since_):
        # Counted when the read has RETURNED: a row committed after that is one this read
        # cannot have seen, so only a wake can answer it (counted at the call, the read could
        # still run after the row's commit and answer it itself, without any wake).
        rows = real_poll(peer_id, since_)
        reads[str(peer_id)] = reads.get(str(peer_id), 0) + 1
        return rows

    monkeypatch.setattr(WAIT, "_poll_once", counting_poll)
    gate = threading.Event()
    seen = _gate_addressing_reads(monkeypatch, gate)
    try:
        ta, ra = _park_timed(addressed["peer_id"], since=since, timeout=12)
        tp, rp = _park_timed(plain["peer_id"], timeout=12, addressed=None)
        assert _until(lambda: counts.get(str(addressed["peer_id"]), 0) >= 2
                      and reads.get(str(plain["peer_id"]), 0) >= 1), (counts, reads)
        before = listening.SUBSCRIBER.stats()
        row_at = time.monotonic()
        row = _row(owner, sender, {"to": "somebody-else"})
        assert _until(lambda: seen["entered"] >= 1, 5.0), "the addressing read was never entered"
        tp.join(8)
        answered_while_held = not tp.is_alive()
        after = listening.SUBSCRIBER.stats()
    finally:
        gate.set()
    tp.join(15)
    ta.join(20)
    moved = {k: after.get(k, 0) - before.get(k, 0) for k in set(before) | set(after)
             if after.get(k, 0) != before.get(k, 0)}
    assert answered_while_held, ("the plain waiter waited behind the held addressing read",
                                 moved)
    # Woken by the row's nudge, not by a LISTEN re-made (which wakes every waiter).
    assert not moved.get("listens"), moved
    assert rp["at"] - row_at < 2.0, (rp["at"] - row_at, moved)
    r = rp["r"]
    assert r.status_code == 200, (r.status_code, r.content[:300])
    assert row["key"] in [m["key"] for m in json.loads(r.content)["messages"]]
    assert ra["r"].status_code == 204, ra["r"].status_code


def test_an_addressing_read_that_fails_still_wakes_every_addressed_waiter(
        client, owner, monkeypatch, listening):
    """The safe side, kept by E22: the addressing read REFUSED (the subscriber side's
    server_internal context raises), a row for somebody else wakes the parked addressed
    waiter anyway, which re-scans (a third scan after its first read and gap re-read) and
    answers 204 at its deadline, the row not being its. CONTROL: `addressing_errors`
    counted it."""
    monkeypatch.setattr(WAIT, "POLL_SECS", 30.0)
    mesh, sender, receiver = _pair(owner)
    since = _anchor(client, receiver)
    counts = _counting_scans(monkeypatch)
    me = str(receiver["peer_id"])
    t, result = _park_timed(receiver["peer_id"], since=since, timeout=6)
    assert _until(lambda: counts.get(me, 0) >= 2), counts
    _gate_addressing_reads(monkeypatch, refuse=True)
    errors = listening.SUBSCRIBER.stats().get("addressing_errors", 0)
    _row(owner, sender, {"to": "somebody-else"})
    assert _until(lambda: counts.get(me, 0) >= 3, 5.0), counts
    t.join(15)
    assert result["r"].status_code == 204, result["r"].status_code
    assert listening.SUBSCRIBER.stats().get("addressing_errors", 0) > errors


def test_the_batched_addressing_read_is_load_nudge_for_every_pair(owner):
    """E22's one read answers, for each (key, author), exactly what `mesh_notify.load_nudge`
    answers: the newest version by that author, and nothing for a key that author did not
    write or a key that does not exist. CONTROL: every row compared is really there, and
    the second version of one is what both read."""
    from psycopg.types.json import Jsonb

    from apps.wsgw import mesh_notify
    wake = _wake()
    mesh, sender, receiver = _pair(owner)
    other = _peer(owner, "other-author")
    _grant(owner, NS, other, mesh, permission="write")
    a = _row(owner, sender, {"to": receiver["name"], "cc": ["x", "y"], "body": "hello"})
    b = _row(owner, other, {"to": "all"})
    c = _row(owner, sender, {"body": 7})
    # A second version of c: the newest. History, not an edit (its `to` and body differ from version 1's, which the edit path
    # refuses; divergence 059: tests/fixtures/history_rows.py), by c's author, with the bytes a writer would have signed (051).
    with history_rows.seeding_mail_history(owner):
        owner.execute("UPDATE shared_memory SET is_head = FALSE"
                      " WHERE namespace = %s AND key = %s AND version = 1", (NS, c["key"]))
        sr.insert(owner, sr.row(sender["peer_id"], sender["owner_id"], NS, c["key"],
                                {"to": "v2"}, version=2, parent_id=c["id"],
                                author_label=sender["label"]))
    pairs = [(a["key"], sender["peer_id"]), (b["key"], other["peer_id"]),
             (c["key"], sender["peer_id"]),
             (a["key"], other["peer_id"]),                           # not that author's row
             (f"msg/0-{uuid.uuid4().hex}", sender["peer_id"])]   # no such row
    got = wake.load_nudges(owner, pairs)
    want = {p: mesh_notify.load_nudge(owner, *p) for p in pairs}
    assert all(want[p] is not None for p in pairs[:3]), want
    assert want[pairs[2]].addressing.to == ("v2",), want[pairs[2]]
    assert {p: got.get(p) for p in pairs} == want


def test_the_addressing_backlog_is_bounded_and_overflow_wakes_the_addressed(monkeypatch):
    """E22's queue is a process-lifetime container a burst grows, so it is bounded
    (tests/test_bounded_caches.py): past ADDRESSING_BACKLOG_MAX queued rows the backlog is
    dropped and every addressed waiter is woken, the same safe side as an unread addressing.
    Unit, on a subscriber that is not running (nothing drains its queue). CONTROL: queueing
    up to the bound wakes nobody, and a plain waiter is woken by every row at once."""
    wake = _wake()
    monkeypatch.setattr(wake, "ADDRESSING_BACKLOG_MAX", 3)
    reg = wake.Registry()
    sub = wake.Subscriber(reg)
    addressed = reg.register(uuid.uuid4(), labels={"somebody"})
    plain = reg.register(uuid.uuid4())
    author = uuid.uuid4()

    def note():
        return json.dumps({"key": f"msg/1-{uuid.uuid4().hex}", "from_peer_id": str(author)})

    for _ in range(3):
        sub._on_notify(note())
        assert plain.event.is_set()
        plain.event.clear()
    assert not addressed.event.is_set() and len(sub._pending) == 3, len(sub._pending)
    sub._on_notify(note())
    assert addressed.event.is_set(), "an overflowing backlog woke no addressed waiter"
    assert len(sub._pending) <= 3, len(sub._pending)
    assert sub.stats().get("addressing_overflow") == 1, sub.stats()
