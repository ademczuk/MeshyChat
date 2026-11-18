"""A default bound on ESTABLISHING a database connection, and the wait route's answer when the database does not
answer (12 Nov 2026: two production wedges with request threads piled on connects while HQD paged; memory-bxxxxx
2af0c3, 5652f6). Database-free: psycopg.connect is captured, or pointed at a local socket that never answers."""
from __future__ import annotations

import contextlib
import os
import socket
import threading
import time

import psycopg
import pytest

# Set Django up here, as test_mesh_owed_key.py does, so this file also runs alone and not only after a module that
# already did (pxx 3bc715, Codex on #621: "settings are not configured" in the two wait-route tests on a lone run).
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

from apps.core import dbconn  # noqa: E402

ENV = dbconn.CONNECT_TIMEOUT_ENV


class _Stop(Exception):
    pass


def _kwargs_of(monkeypatch, target="postgresql://u:p@127.0.0.1:1/db", **kw):
    seen = {}

    def fake_connect(t, **k):
        seen.update(k)
        raise _Stop
    monkeypatch.setattr(dbconn.psycopg, "connect", fake_connect)
    with pytest.raises(_Stop):
        dbconn.connect(target, **kw)
    return seen


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    monkeypatch.delenv("PGCONNECT_TIMEOUT", raising=False)


def test_unset_is_the_ten_second_default(monkeypatch):
    assert dbconn.default_connect_timeout_s() == 10
    assert _kwargs_of(monkeypatch)["connect_timeout"] == 10


def test_zero_switches_the_default_off(monkeypatch):
    monkeypatch.setenv(ENV, "0")
    assert "connect_timeout" not in _kwargs_of(monkeypatch)


@pytest.mark.parametrize("garbage", ["-1", "1.5", "ten", "", "3601", "５"])
def test_a_value_that_does_not_parse_is_the_default(monkeypatch, garbage):
    monkeypatch.setenv(ENV, garbage)
    assert dbconn.default_connect_timeout_s() == 10
    monkeypatch.setenv(ENV, " 7 ")
    assert dbconn.default_connect_timeout_s() == 7                 # control: a value is read


def test_an_explicit_caller_wins(monkeypatch):
    monkeypatch.setenv(ENV, "30")
    assert _kwargs_of(monkeypatch, connect_timeout=3)["connect_timeout"] == 3


def test_a_dsns_own_bound_and_pgconnect_timeout_are_not_overridden(monkeypatch):
    """A keyword argument would silently override both, so the default stays out when either is set."""
    assert "connect_timeout" not in _kwargs_of(monkeypatch, "postgresql://u:p@127.0.0.1:1/db?connect_timeout=1")
    monkeypatch.setenv("PGCONNECT_TIMEOUT", "4")
    assert "connect_timeout" not in _kwargs_of(monkeypatch)


def test_the_default_bound_binds_on_a_database_that_never_answers(monkeypatch):
    """A local listener that never accepts is a database that never answers. With the default at 2 s the connect
    gives up within the bound plus a margin; with the bound off (0) it is still waiting at that point. A hang
    backstop, not a tight wall-clock bound: the gate box is slow."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    silent = f"postgresql://meshychat_owner:unused@127.0.0.1:{listener.getsockname()[1]}/meshy_silent"

    def attempt(out):
        started = time.monotonic()
        try:
            with contextlib.closing(dbconn.connect(silent)):
                pass
        except BaseException as exc:  # noqa: BLE001 - recorded and asserted on below
            out["exc"] = exc
        out["elapsed"] = time.monotonic() - started

    try:
        monkeypatch.setenv(ENV, "2")
        bounded: dict = {}
        worker = threading.Thread(target=attempt, args=(bounded,), daemon=True)
        worker.start()
        # A wide hang backstop, not a tight wall-clock bound (memory-bxxxxx 534aac: a loaded gate box refused a 4.5 s
        # bound at 4.74 s); the bound-off case below is what shows the bound binds.
        worker.join(2 + 28)
        assert not worker.is_alive(), "the connect waited past its 2 s bound (and a 28 s margin) on a silent database"
        assert isinstance(bounded.get("exc"), psycopg.OperationalError), bounded
        assert bounded["elapsed"] >= 1.5, bounded                   # control: it really waited

        monkeypatch.setenv(ENV, "0")                                # control: no bound, still waiting at 10 s
        unbounded: dict = {}
        worker = threading.Thread(target=attempt, args=(unbounded,), daemon=True)
        worker.start()
        worker.join(2 + 8)
        assert worker.is_alive(), f"with the bound off the connect returned: {unbounded}"
    finally:
        listener.close()                                            # frees the unbounded connect


# ------------------------------------------------------------------------------------- the wait route's answer

def _wait_request():
    from django.test import RequestFactory
    return RequestFactory().get("/v1/mesh/wait", {"timeout": "2"})


def test_the_wait_route_answers_503_with_retry_after_when_the_connect_fails(monkeypatch):
    from apps.mesh import usage, views
    from apps.mesh import wait as mesh_wait

    @contextlib.contextmanager
    def no_db(*a, **k):
        raise psycopg.OperationalError("connection timeout expired")
        yield  # pragma: no cover

    monkeypatch.setattr(views.dbconn, "serving_connection", no_db)
    monkeypatch.setattr(usage, "record_on_own_connection",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("usage row written")))
    free_before = mesh_wait._waiters._value
    resp = views.wait(_wait_request())
    assert resp.status_code == 503 and resp["Retry-After"] == "5"
    assert mesh_wait._waiters._value == free_before                 # the waiter permit is given back


def test_a_database_lost_after_auth_skips_the_usage_rows_second_connect(monkeypatch):
    """The usage row opens its own connection; after a database failure that would cost the request a second full
    bound, so it is skipped (memory-bxxxxx 5652f6 point 2)."""
    from apps.mesh import usage, views
    from apps.mesh import wait as mesh_wait

    class Auth:
        peer_id, token = "00000000-0000-0000-0000-000000000001", "t"

    @contextlib.contextmanager
    def fake_conn(*a, **k):
        yield object()

    monkeypatch.setattr(views.dbconn, "serving_connection", fake_conn)
    monkeypatch.setattr(views, "authenticate_peer", lambda conn, request: Auth())
    monkeypatch.setattr(views.peer_limit, "admit", lambda *a, **k: None)
    monkeypatch.setattr(views, "verify_token", lambda token: {"exp": int(time.time()) + 600})

    def lost(*a, **k):
        raise psycopg.OperationalError("server closed the connection unexpectedly")
    monkeypatch.setattr(mesh_wait, "first_read", lost)
    recorded = []
    monkeypatch.setattr(usage, "record_on_own_connection", lambda *a, **k: recorded.append(a))
    resp = views.wait(_wait_request())
    assert resp.status_code == 503 and recorded == []


@pytest.mark.parametrize("took,logged", [(0.25, True), (0.05, False)])
def test_a_slow_connect_is_logged_with_the_thread_count(monkeypatch, caplog, took, logged):
    """Measurement for the pool decision (pxx 4b5517): a connect over SLOW_CONNECT_LOG_S logs its time and the
    process's thread count; a fast one logs nothing (control)."""
    class _Fake:
        def close(self):
            pass
    clock = iter([100.0, 100.0 + took])
    monkeypatch.setattr(dbconn.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(dbconn.psycopg, "connect", lambda target, **k: _Fake())
    caplog.set_level("WARNING", logger="meshychat.db")
    dbconn.connect("postgresql://u:p@127.0.0.1:1/db", role="")
    hits = [r.getMessage() for r in caplog.records if "slow database connect" in r.getMessage()]
    assert bool(hits) is logged, hits
    if logged:
        assert "threads " in hits[0] and f"{took:.3f}s" in hits[0]
