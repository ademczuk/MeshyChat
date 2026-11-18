"""Per-seat daily series and change points (divergence 086, issue #143). Synthetic series with a KNOWN change
point, each beside the control that must not alarm.

Run:  pytest tests/test_mesh_drift.py -q
Mutants (apps/mesh/drift.py; each seen to fail a test here):
  D1  the baseline SD floor is dropped (a flat baseline divides by 0)   test_a_constant_series_never_alarms_and_a_step_from_it_does
  D2  only upward shifts are detected                                   test_a_step_is_found_in_its_direction_near_its_day
  D4  the type-mix JSD is never compared                                test_a_type_mix_shift_is_found_and_a_stable_mix_is_not
  D5  an alarm need not persist (a recovered dip is reported)           test_a_dip_that_recovers_is_not_a_shift, calibration
  D6  no terminal confirmation window                                   test_a_late_dip_that_recovers_is_not_a_shift
  D7  the SD floor uses later values (one outlier hides a step)         test_one_outlier_does_not_hide_a_sustained_step
  D8  the baseline size ignores missing values                          test_a_sparse_metric_is_judged_on_its_own_points
EQUIVALENT, not listed as a kill: dropping the early minimum-baseline return in shifts_for_seat changes nothing,
because each series is skipped anyway when it has no more points than the baseline (seen: it survives).
"""
from __future__ import annotations

import datetime as _dt
import random

import pytest

from apps.mesh import drift


def _days(values, metric="messages", start=_dt.date(2026, 9, 1), mix=None):
    out = []
    for i, v in enumerate(values):
        d = {"seat": "otter", "day": start + _dt.timedelta(days=i)}
        for m in drift.METRICS:
            d[m] = None
        d[metric] = v
        d["type_mix"] = (mix[i] if mix else {"request": 3, "reply": 3})
        out.append(d)
    return out


def test_a_step_is_found_in_its_direction_near_its_day():
    """MUTANT D2. A noisy series steps up at day 20, another steps down at day 20."""
    rng = random.Random(7)
    up = [10 + rng.gauss(0, 1) for _ in range(20)] + [16 + rng.gauss(0, 1) for _ in range(10)]
    down = [10 + rng.gauss(0, 1) for _ in range(20)] + [4 + rng.gauss(0, 1) for _ in range(10)]
    (s_up,) = [s for s in drift.shifts_for_seat("otter", _days(up)) if s.metric == "messages"]
    (s_down,) = [s for s in drift.shifts_for_seat("otter", _days(down)) if s.metric == "messages"]
    assert s_up.direction == "up" and s_down.direction == "down"
    for s in (s_up, s_down):
        day = (_dt.date.fromisoformat(s.day) - _dt.date(2026, 9, 1)).days
        assert 20 <= day <= 23, f"alarmed on day {day}, the change was at 20"
    assert s_up.after > s_up.before and s_down.after < s_down.before


def test_a_stable_noisy_series_does_not_alarm():
    rng = random.Random(11)
    flat = [10 + rng.gauss(0, 1) for _ in range(30)]
    assert [s for s in drift.shifts_for_seat("otter", _days(flat)) if s.metric == "messages"] == []


def test_a_constant_series_never_alarms_and_a_step_from_it_does():
    """MUTANT D1: a baseline with SD 0 needs a floor, or a constant series divides by zero and a step from it
    cannot be scored."""
    const = [5.0] * 30
    assert drift.shifts_for_seat("otter", _days(const)) == []
    step = [5.0] * 20 + [9.0] * 10
    assert [s.metric for s in drift.shifts_for_seat("otter", _days(step))] == ["messages"]


def test_a_seat_with_too_few_days_is_not_judged():
    """MUTANT D3."""
    few = [1, 1, 1, 50, 50, 50, 50]                  # 7 active days: under MIN_BASELINE_DAYS + 1
    assert drift.shifts_for_seat("otter", _days(few)) == []
    out = drift.report_from_rows(_days(few))
    assert out["seats"] == 1 and out["seats_judged"] == 0 and out["shifts"] == []


def test_a_type_mix_shift_is_found_and_a_stable_mix_is_not():
    """MUTANT D4. 21 days of requests and replies, then 7 days that are almost all status notes."""
    vals = [10] * 28
    stable = drift.shifts_for_seat("otter", _days(vals))
    assert [s for s in stable if s.metric == "type_mix"] == []
    mix = [{"request": 3, "reply": 3}] * 21 + [{"status": 6}] * 7
    shifted = [s for s in drift.shifts_for_seat("otter", _days(vals, mix=mix)) if s.metric == "type_mix"]
    assert len(shifted) == 1 and shifted[0].score >= drift.JSD_ALARM


def test_jsd_bounds():
    assert drift.jsd({"a": 1}, {"a": 5}) == 0.0
    assert abs(drift.jsd({"a": 1}, {"b": 1}) - 1.0) < 1e-9
    assert drift.jsd({}, {"a": 1}) == 0.0


def test_the_report_carries_labels_days_and_numbers_and_names_what_it_does_not_measure():
    rng = random.Random(3)
    rows = _days([10 + rng.gauss(0, 1) for _ in range(20)] + [20] * 10)
    out = drift.report_from_rows(rows)
    assert out["seats_judged"] == 1 and out["shifts"] and out["shifts"][0]["seat"] == "otter"
    assert set(out["not_measured"]) == {"owed_ack_rate", "embedding_centroid_shift"}


# --------------------------------------------------------------------------- the query, on a real database

from tests.test_mesh_routes import NS, _peer, _row_at, client, mesh_settings, owner  # noqa: E402,F401


def test_the_series_query_aggregates_per_seat_and_day(owner):
    now = _dt.datetime.now(_dt.timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
    a = _peer(owner, "dr")
    k1, _ = _row_at(owner, a, {"body": "x" * 100, "type": "request", "thread_id": "t1"}, now - _dt.timedelta(days=2))
    _row_at(owner, a, {"body": "y" * 300, "type": "reply", "thread_id": "t2", "in_reply_to": k1},
            now - _dt.timedelta(days=2) + _dt.timedelta(minutes=30))
    with owner.transaction():
        owner.execute("SET LOCAL row_security = off")
        with owner.cursor() as cur:
            cur.execute(drift.SERIES_SQL, {"days": 30})
            cols = [c.name for c in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall() if r[0] == a["label"]]
    (row,) = rows
    assert row["messages"] == 2 and row["threads"] == 2 and row["reply_share"] == 0.5
    assert row["len_p50"] == 200 and abs(row["reply_latency_p50_s"] - 1800) < 1
    assert row["type_mix"] == {"request": 1, "reply": 1}


def test_a_dip_that_recovers_is_not_a_shift():
    """MUTANT D5. Two days 2.5 SD down, then back to the baseline: noise, not a shift."""
    vals = [10.0, 10.4, 9.7, 10.2, 9.9, 10.3, 9.8, 10.1, 10.0, 9.9, 10.2, 10.1, 9.8, 10.0, 10.3,
            8.2, 8.3, 10.1, 9.9, 10.2, 10.0, 9.8, 10.1, 10.3, 9.9, 10.0, 10.2, 9.8, 10.1, 10.0]
    assert [s for s in drift.shifts_for_seat("otter", _days(vals)) if s.metric == "messages"] == []


def test_calibration_over_many_seeds():
    """Deterministic seeds: on 30 flat noisy days a series alarms in under 5% of 200 seeds; a 6 SD step at
    day 20 is found within 3 days in at least 95%."""
    false_alarms = caught = 0
    for seed in range(200):
        rng = random.Random(1000 + seed)
        flat = [10 + rng.gauss(0, 1) for _ in range(30)]
        false_alarms += bool([s for s in drift.shifts_for_seat("o", _days(flat)) if s.metric == "messages"])
        step = [10 + rng.gauss(0, 1) for _ in range(20)] + [16 + rng.gauss(0, 1) for _ in range(10)]
        hit = [s for s in drift.shifts_for_seat("o", _days(step)) if s.metric == "messages"]
        if hit and hit[0].direction == "up" and 20 <= (_dt.date.fromisoformat(hit[0].day) - _dt.date(2026, 9, 1)).days <= 23:
            caught += 1
    assert false_alarms < 10, f"{false_alarms} of 200 flat series alarmed"
    assert caught >= 190, f"only {caught} of 200 steps caught within 3 days"



def test_a_late_dip_that_recovers_is_not_a_shift():
    """MUTANT D6 (Codex on #148): two dip days near the end, then recovery. The suffix median is 5.5, so only
    the terminal window keeps it out."""
    vals = [10.0] * 26 + [1.0] * 2 + [10.0] * 2
    assert [s for s in drift.shifts_for_seat("otter", _days(vals)) if s.metric == "messages"] == []


def test_an_alarm_with_too_few_days_after_it_is_unconfirmed():
    vals = [10.0] * 28 + [30.0] * 2                 # a jump on the last two days only
    assert [s for s in drift.shifts_for_seat("otter", _days(vals)) if s.metric == "messages"] == []


def test_one_outlier_does_not_hide_a_sustained_step():
    """MUTANT D7 (Codex on #148): [10]*20 + [1000] + [20]*9 is a sustained step with one wild day in it."""
    vals = [10.0] * 20 + [1000.0] + [20.0] * 9
    hit = [s for s in drift.shifts_for_seat("otter", _days(vals)) if s.metric == "messages"]
    assert len(hit) == 1 and hit[0].direction == "up"


def test_a_sparse_metric_is_judged_on_its_own_points():
    """MUTANT D8 (Codex on #148): 30 active days, reply latency on 14 of them, 7 at 60 s then 7 at 600 s."""
    days = _days([10.0] * 30)
    lat = [60.0] * 7 + [600.0] * 7
    for i, d in enumerate(days):
        d["reply_latency_p50_s"] = lat[i // 2] if i % 2 == 0 and i // 2 < len(lat) else None
    hit = [s for s in drift.shifts_for_seat("otter", days) if s.metric == "reply_latency_p50_s"]
    assert len(hit) == 1 and hit[0].direction == "up"


def test_the_series_query_excludes_the_unfinished_day(owner):
    """Codex on #148: today's first message must not read as a collapsed day."""
    now = _dt.datetime.now(_dt.timezone.utc)
    a = _peer(owner, "today")
    _row_at(owner, a, {"body": "x", "type": "note"}, now - _dt.timedelta(minutes=1))
    with owner.transaction():
        owner.execute("SET LOCAL row_security = off")
        with owner.cursor() as cur:
            cur.execute(drift.SERIES_SQL, {"days": 30})
            assert [r for r in cur.fetchall() if r[0] == a["label"]] == [], "the unfinished UTC day was included"
