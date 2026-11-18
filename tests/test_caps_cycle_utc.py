"""The caps snapshot reads usage from the SAME cycle the counter is written under (UTC).

`apps.files.quota._cycle_start` counts a free owner's usage under the first of the UTC month. The
caps snapshot (`apps.dashboard.queries.CAPS_SNAPSHOT_SQL`, read by the dashboard's tier-info route and
by `owner_tier.effective_caps`) used to compute its own month from `CURRENT_DATE`, the database
session's LOCAL date, so for the hours between local and UTC midnight at the start of every month it
read an empty cycle and reported 0. test_owner_tier failed that way on 2026-09-30 23:48Z (01:48 on
1 October at UTC+2), on main as on a branch. The snapshot now takes the cycle from `_cycle_start`.

The clock is FIXED, so the case does not depend on when it runs: 2020-01-31 23:30 UTC, with the database
session at UTC+14, where it is already 1 February.

Run:  .venv/Scripts/python.exe -m pytest tests/test_caps_cycle_utc.py -q -p no:cacheprovider
"""
from __future__ import annotations

import datetime as dt
import types
import uuid

import psycopg
import pytest

from tests.test_onboard import DSN, owner_conn  # noqa: F401 - fixture used by name; sets up Django
from apps.authapi import owner_tier  # noqa: E402
from apps.files import quota  # noqa: E402

FIXED_UTC = dt.datetime(2020, 1, 31, 23, 30, tzinfo=dt.timezone.utc)


class _FixedDatetime(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return FIXED_UTC if tz is None else FIXED_UTC.astimezone(tz)


@pytest.fixture()
def fixed_clock(monkeypatch):
    shim = types.SimpleNamespace(**{k: getattr(dt, k) for k in dir(dt) if not k.startswith("__")})
    shim.datetime = _FixedDatetime
    monkeypatch.setattr(quota, "_dt", shim)
    assert quota._cycle_start.__globals__["_dt"].datetime.now(dt.timezone.utc) == FIXED_UTC


def _free_owner_with_usage(conn, **counters) -> uuid.UUID:
    owner = uuid.uuid4()
    conn.execute("INSERT INTO dashboard_users (id, email) VALUES (%s, %s)",
                 (owner, f"cycle-{owner.hex[:8]}@example.test"))
    for cycle, n in counters.items():
        conn.execute("INSERT INTO owner_usage (owner_id, capability, cycle_start, counter) "
                     "VALUES (%s, 'peers_per_owner', %s, %s)", (owner, dt.date.fromisoformat(cycle), n))
    return owner


def test_the_snapshot_reads_the_utc_cycle_when_the_session_is_already_in_the_next_month(
        owner_conn, fixed_clock):
    """January (UTC) holds 3, February holds 7. At 23:30 UTC on 31 January the counter's cycle is
    January, and the snapshot must say 3 even though the session's local date is 1 February.
    CONTROL: the session's own date really is February here, so a snapshot keyed on it would read 7
    (or 0, with no February row), which is the defect.
    (Mutant: restore `date_trunc('month', CURRENT_DATE)` in CAPS_SNAPSHOT_SQL: this fails.)"""
    owner = _free_owner_with_usage(owner_conn, **{"2020-01-01": 3, "2020-02-01": 7})
    with psycopg.connect(DSN) as conn:
        conn.execute("SET TIME ZONE 'Etc/GMT-14'")                       # UTC+14
        local = conn.execute("SELECT (timestamptz '2020-01-31 23:30:00+00')::date").fetchone()[0]
        assert local == dt.date(2020, 2, 1), "control: the session is not a day ahead"
        assert quota._cycle_start(conn, owner) == dt.date(2020, 1, 1)
        caps = owner_tier.effective_caps(conn, owner)["caps"]
    assert caps["peers_per_owner"]["current"] == 3, caps["peers_per_owner"]


def test_a_live_subscription_period_still_decides_the_cycle(owner_conn, fixed_clock):
    """A pro owner's cycle is its subscription's `current_period_start`, not the calendar month, in
    the snapshot as in the counter. Control for the test above: the change routes BOTH through
    `_cycle_start`, and this is its other branch."""
    owner = _free_owner_with_usage(owner_conn, **{"2020-01-01": 3, "2020-01-15": 5})
    owner_conn.execute("INSERT INTO owner_subscriptions (owner_id, stripe_customer_id, status, "
                       "current_period_start) VALUES (%s, %s, 'active', %s)",
                       (owner, f"cus_test_{owner.hex[:12]}",
                        dt.datetime(2020, 1, 15, 12, 0, tzinfo=dt.timezone.utc)))
    with psycopg.connect(DSN) as conn:
        assert quota._cycle_start(conn, owner) == dt.date(2020, 1, 15)
        caps = owner_tier.effective_caps(conn, owner)["caps"]
    assert caps["peers_per_owner"]["current"] == 5, caps["peers_per_owner"]


from tests.test_dashboard import dashboard_settings as _dashboard_settings  # noqa: E402 - the route's JWT settings


def test_the_tier_info_route_reads_the_counters_cycle_too(fixed_clock, _dashboard_settings):
    """The other reader of the snapshot: GET /v1/dashboard/api/tier-info (apps/dashboard/views.py). With the
    clock fixed in January 2020, an owner whose January counter holds 4 is reported `current` 4.
    CONTROL: the owner's February row holds 9 and the real date is years later, so a route that passed
    any cycle but the counter's own would report 9 or 0.
    (Mutant from kxxx's review of #44, V1: the route passes a wrong cycle. This fails; the two cases above
    go through `owner_tier.effective_caps` only and do not notice.)"""
    from tests.test_dashboard import _insert_owner, _sql_conn, ok

    with _sql_conn() as conn, conn.cursor() as cur:
        owner = _insert_owner(cur, f"cycle-route-{uuid.uuid4().hex[:8]}")
        for cycle, n in (("2020-01-01", 4), ("2020-02-01", 9)):
            cur.execute("INSERT INTO owner_usage (owner_id, capability, cycle_start, counter) "
                        "VALUES (%s, 'peers_per_owner', %s, %s)", (owner, dt.date.fromisoformat(cycle), n))
    try:
        payload = ok(owner, "/v1/dashboard/api/tier-info")
        assert payload["capabilities"]["peers_per_owner"]["current"] == 4, payload["capabilities"]
    finally:
        with _sql_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM owner_usage WHERE owner_id = %s", (owner,))
            cur.execute("DELETE FROM dashboard_users WHERE id = %s", (owner,))
