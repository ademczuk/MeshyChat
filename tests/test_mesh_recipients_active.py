"""MEASURED activity on GET /v1/mesh/recipients (oxxxxxx f38d28 item 5, Tue 10 Nov 2026).

oxxxxxx's roster, built on `fleet`, showed memory-bxxxxx DOWN ("no live socket") while it was writing to oxxxxxx,
and Bxxxxx counted five seats idle that had each sent a result in the previous 15 minutes. The dashboard has had
the measured rule since #575; the recipients route now answers it too, with ONE grouped statement per call (pxx
9059b9 P2-1: the route runs before every send) on the database's clock (P3). Pure functions with a fake connection;
the SQL against Postgres is the DB half (tests/test_query_cost_bounds.py, tests/test_mesh_recipients.py).
"""
import contextlib
import datetime as dt

import pytest

from apps.dashboard import queries as dash
from apps.mesh import work

SENT = dt.datetime(2026, 11, 10, 7, 41, tzinfo=dt.timezone.utc)
LIVE = {"effective_state": "active", "beat_age_secs": 600, "state_expires_at": "2026-11-10T08:00:00Z"}
SAID_NOTHING = {"effective_state": "unknown", "beat_age_secs": None, "state_expires_at": None}


class _Conn:
    def __init__(self, rows=(), fail=False):
        self.rows, self.fail, self.statements, self.savepoints = list(rows), fail, [], 0

    @contextlib.contextmanager
    def transaction(self):
        self.savepoints += 1
        yield

    @contextlib.contextmanager
    def cursor(self):
        conn = self

        class _Cur:
            def execute(self, sql, params):
                conn.statements.append((sql, params))
                if conn.fail:
                    raise RuntimeError("canceling statement due to statement timeout")

            def fetchall(self):
                return conn.rows
        yield _Cur()


def test_the_rule_is_the_dashboards():
    assert work.ACTIVE_WINDOW_SECS == dash.ACTIVE_WINDOW_SECS
    assert work.BEAT_WINDOW_SECS == dash.BEAT_WINDOW_SECS
    assert tuple(work.DAEMON_THREADS) == tuple(dash.DAEMON_THREADS)


def test_one_statement_for_every_peer_of_a_call_in_its_own_savepoint():
    conn = _Conn(rows=[("p1", SENT)])
    sent = work.active_sent(conn, ["p1", "p2", "p3", "p1"])
    assert sent == {"p1": SENT}
    assert len(conn.statements) == 1 and conn.savepoints == 1           # never one read per peer
    sql, params = conn.statements[0]
    assert params[2] == ["p1", "p2", "p3"]                               # each peer once
    assert "GROUP BY created_by_peer_id" in sql and "ANY (%s::uuid[])" in sql


def test_the_window_is_the_databases_clock_on_both_bounds():
    sql = work.ACTIVE_SENT_SQL
    assert "now() - make_interval(secs => %s)) * 1000" in sql            # the key bound, from now()
    assert "created_at >= now() - make_interval(secs => %s)" in sql      # the time bound, from now()
    conn = _Conn()
    work.active_sent(conn, ["p"])
    params = conn.statements[0][1]
    assert params[1] == params[3] == work.ACTIVE_WINDOW_SECS and not any(isinstance(p, str) and p.startswith("msg/1")
                                                                         for p in params)


def test_no_peers_no_statement():
    conn = _Conn()
    assert work.active_sent(conn, []) == {} and conn.statements == []


def test_a_failed_read_gives_none_not_an_error():
    conn = _Conn(fail=True)
    assert work.active_sent(conn, ["p"]) is None


def test_a_seat_that_sent_is_active_whatever_it_published():
    assert work.measured_active({"p": SENT}, "p", SAID_NOTHING) == (True, SENT)


@pytest.mark.parametrize("presence, want", [
    (LIVE, True),
    ({**LIVE, "beat_age_secs": work.BEAT_WINDOW_SECS + 1}, False),       # beat too old
    ({**LIVE, "state_expires_at": None}, False),                          # no ttl: says nothing
    ({**LIVE, "effective_state": "idle"}, False),
    (SAID_NOTHING, False),
])
def test_without_a_send_only_a_live_published_active_counts(presence, want):
    assert work.measured_active({}, "p", presence) == (want, None)


def test_when_the_read_failed_only_a_live_beat_decides_else_unknown():
    assert work.measured_active(None, "p", LIVE) == (True, None)
    assert work.measured_active(None, "p", SAID_NOTHING) == (None, None)   # null, never a fabricated false
