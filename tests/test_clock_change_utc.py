"""Database times compared in Python across a summer-time change (codex-pxx-cli TASK AL, the 25 Oct gates).

Python adds, subtracts and compares two datetimes of ONE zone on their wall clock, ignoring fold and offset. A session
whose zone has summer time returns timestamptz values in that zone, so in the repeated hour (02:00 to 03:00 local on
25 Oct 2026, 00:00Z to 01:00Z) two instants an hour apart compare equal and a later one can sort first. Serving
connections are pinned to UTC (apps/core/dbconn.py), so these are the paths that are not: an owner or migration
connection, a tool's bare psycopg connection, a test. Each test below is database-free, takes the instants around the
change as Europe/Berlin datetimes, and has a CONTROL showing the wall-clock reading the old code made.

Run:  pytest tests/test_clock_change_utc.py -q -p no:cacheprovider
"""
from __future__ import annotations

import datetime as _dt
import os
import sys
import types
import uuid
import zoneinfo
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

from apps.jobs import explain as E  # noqa: E402
from apps.meshsum import window as W  # noqa: E402
from tools.summaries_eval import corpus  # noqa: E402

BERLIN = zoneinfo.ZoneInfo("Europe/Berlin")
UTC = _dt.timezone.utc


def _at(h: int, m: int) -> _dt.datetime:
    """An instant on 25 Oct 2026 (UTC h:m), as a Europe/Berlin session returns it."""
    return _dt.datetime(2026, 10, 25, h, m, tzinfo=UTC).astimezone(BERLIN)


def test_explain_calls_a_lease_an_hour_past_now_held_across_the_change(monkeypatch):
    """now 00:30Z (02:30 CEST), leased_until 01:30Z (02:30 CET): a live lease, held_by_another. CONTROL: on the wall
    clock the two are equal, so `leased_until > now` was False and the lease read as expired (due_now)."""
    now, until = _at(0, 30), _at(1, 30)
    assert not until > now, "control: the wall-clock comparison calls the live lease expired"
    state = {"holder": "worker-b", "leased_until": until, "next_run_at": _at(0, 0), "consecutive_failures": 0,
             "last_result": None, "last_error": None, "last_started_at": _at(0, 0)}
    monkeypatch.setattr(E.leases, "read_state", lambda conn, name: state)
    got = E.explain(None, "job", now=now)
    assert got.reason == E.HELD_BY_ANOTHER and got.runnable is False, got


def test_explain_waits_for_a_next_run_an_hour_away_across_the_change(monkeypatch):
    """next_run_at an hour after now, across the change, is NOT due, and the wait is an hour, not zero."""
    now, nxt = _at(0, 30), _at(1, 30)
    state = {"holder": None, "leased_until": None, "next_run_at": nxt, "consecutive_failures": 0,
             "last_result": None, "last_error": None, "last_started_at": _at(0, 0)}
    monkeypatch.setattr(E.leases, "read_state", lambda conn, name: state)
    got = E.explain(None, "job", now=now)
    assert got.reason == E.NOT_DUE and "not due for 3600s" in got.detail, got


def test_present_orders_rows_by_instant_across_the_change():
    """00:50Z reads 02:50 CEST and 01:10Z reads 02:10 CET. CONTROL: sorted on the wall clock, the later row comes
    first."""
    early = types.SimpleNamespace(id=uuid.uuid4(), created_at=_at(0, 50))
    late = types.SimpleNamespace(id=uuid.uuid4(), created_at=_at(1, 10))
    assert sorted([early, late], key=lambda r: r.created_at)[0] is late, "control: the wall clock puts it first"
    assert W._present([late, early]) == [early, late]


def test_a_plant_lands_after_its_anchor_in_the_repeated_hour():
    """after = 01:10Z, 02:10 CET (fold=1). CONTROL: + 1 ms on the wall clock resets the fold and lands at 00:10Z, an
    hour BEFORE its anchor."""
    after = _at(1, 10)
    assert after.fold == 1
    assert (after + _dt.timedelta(milliseconds=1)).timestamp() < after.timestamp(), "control: the fold is lost"
    plant = {"id": "p1", "author": "a", "author_peer_id": str(uuid.uuid4()), "body": "planted"}
    row = corpus.plant_row(plant, after)
    assert 0 < row.created_at.timestamp() - after.timestamp() < 0.01, row.created_at


def test_the_eval_tool_connects_in_utc(monkeypatch):
    """tools/summaries_eval opens bare psycopg connections: they are pinned to UTC like the serving ones."""
    from tools.summaries_eval import __main__ as M
    seen = {}

    class Conn:
        read_only = False

    monkeypatch.delenv("PGOPTIONS", raising=False)
    monkeypatch.setattr(M.psycopg, "connect", lambda dsn, **kw: (seen.update(kw), Conn())[1])
    M._connect("postgresql://x")()
    assert seen.get("options") == "-c TimeZone=UTC", seen


def test_the_eval_tools_utc_pin_keeps_the_dsns_own_options(monkeypatch):
    """codex-pxx-cli TASK AM: `options=` alone REPLACES the DSN's options, so a timeout and a search_path it carries
    were lost. They are kept, with TimeZone=UTC after them; PGOPTIONS is kept the same way when the DSN has none."""
    from tools.summaries_eval import __main__ as M
    seen = {}

    class Conn:
        read_only = False

    monkeypatch.delenv("PGOPTIONS", raising=False)
    monkeypatch.setattr(M.psycopg, "connect", lambda dsn, **kw: (seen.update(kw), Conn())[1])
    M._connect("dbname=synthetic options='-c statement_timeout=1000 -c search_path=eval,public'")()
    assert seen["options"] == "-c statement_timeout=1000 -c search_path=eval,public -c TimeZone=UTC", seen
    monkeypatch.setenv("PGOPTIONS", "-c search_path=eval")
    M._connect("dbname=synthetic")()
    assert seen["options"] == "-c search_path=eval -c TimeZone=UTC", seen
