"""Per-seat daily behaviour series with change-point detection, model-free (issue #143, divergence 086).

A REPORT, never an action. For each seat and day over a window: messages sent, body length (median and p90),
the share that are replies (`in_reply_to`), the median reply latency (the reply's time less the replied-to
message's), threads touched, and the declared `type` mix. Each numeric series goes through a two-sided CUSUM
against the seat's own baseline, and the type mix through the Jensen-Shannon divergence of the last RECENT_DAYS
against the days before. A shift is reported with its numbers (mean before and after, the day it alarmed) for a
person to interpret: a change in traffic (a new project, a new seat) moves these series as much as a change in
behaviour does, and nothing here can tell them apart.

Following kydehq/behavioral-drift-detection's shape: deterministic statistics only in the path; no model. A
model's message-type label agreed with the declared type on 48% of 150 real messages (6 Oct), too noisy to
start from.

NOT YET: the owed-ack rate (needs a per-day reconstruction of who owed what) and an embedding-centroid shift
(the embedder runs on HQB, which production cannot reach). Stated so the report is not mistaken for them.

`SERIES_SQL` is ONE aggregate query, counts and lengths only, so it can be run read-only against production
and its rows fed to `report_from_rows`.
"""
from __future__ import annotations

import dataclasses
import math
import statistics
from typing import Iterable, Sequence

#: CUSUM in standard deviations of the seat's baseline: drift allowance K, alarm threshold H.
CUSUM_K = 0.5
CUSUM_H = 5.0
#: A seat needs this many active days in its baseline before any series of it is judged.
MIN_BASELINE_DAYS = 7
#: The type-mix comparison: the last RECENT_DAYS against everything before; alarm at JSD >= JSD_ALARM.
RECENT_DAYS = 7
JSD_ALARM = 0.15
METRICS = ("messages", "len_p50", "len_p90", "reply_share", "reply_latency_p50_s", "threads")

SERIES_SQL = (
    "WITH m AS ("
    "  SELECT sm.author_label AS seat, (sm.created_at AT TIME ZONE 'UTC')::date AS day, sm.created_at, "
    "         octet_length(coalesce(sm.value->>'body', '')) AS len, coalesce(sm.value->>'type', '') AS typ, "
    "         sm.value->>'in_reply_to' AS parent, coalesce(sm.value->>'thread_id', sm.value->>'thread') AS thread "
    "  FROM shared_memory sm WHERE sm.namespace = 'shared/mesh-chat' AND sm.is_head AND sm.key LIKE 'msg/%%' "
    "    AND sm.created_at >= (date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC') "
    "                          - make_interval(days => %(days)s) "
    "    AND sm.created_at < (date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')), "
    "lat AS ("
    "  SELECT m.seat, m.day, extract(epoch FROM m.created_at - p.created_at) AS secs FROM m "
    "  JOIN shared_memory p ON p.namespace = 'shared/mesh-chat' AND p.is_head AND p.key = m.parent "
    "  WHERE m.parent IS NOT NULL AND p.created_at <= m.created_at), "
    "types AS (SELECT seat, day, jsonb_object_agg(typ, n) AS mix FROM "
    "  (SELECT seat, day, typ, count(*) AS n FROM m GROUP BY 1, 2, 3) t GROUP BY 1, 2) "
    "SELECT m.seat, m.day, count(*) AS messages, "
    "       percentile_cont(0.5) WITHIN GROUP (ORDER BY m.len) AS len_p50, "
    "       percentile_cont(0.9) WITHIN GROUP (ORDER BY m.len) AS len_p90, "
    "       avg((m.parent IS NOT NULL)::int)::float AS reply_share, "
    "       (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY l.secs) FROM lat l "
    "         WHERE l.seat = m.seat AND l.day = m.day) AS reply_latency_p50_s, "
    "       count(DISTINCT m.thread) AS threads, "
    "       (SELECT t.mix FROM types t WHERE t.seat = m.seat AND t.day = m.day) AS type_mix "
    "FROM m GROUP BY m.seat, m.day ORDER BY m.seat, m.day"
)


@dataclasses.dataclass(frozen=True)
class Shift:
    seat: str
    metric: str
    day: str
    direction: str            # "up" or "down" ("changed" for the type mix)
    before: float
    after: float
    score: float              # CUSUM value at the alarm, in baseline SDs; JSD for the type mix


#: The alarm must also hold over the LAST CONFIRM_DAYS values (a terminal window), and an alarm with fewer than
#: CONFIRM_DAYS values from it on is unconfirmed, not reported: [10]*26 + [1]*2 + [10]*2 had passed the median
#: rule (Codex on #148).
CONFIRM_DAYS = 3
#: The smallest SD a metric's baseline is credited with, in the metric's own unit: a flat baseline has SD 0,
#: and the floor must come from the baseline and the metric, never from later values (one 1000 in a series
#: of 10s and 20s had inflated a range-based floor to 99 and hidden the step).
SD_FLOOR = {"messages": 1.0, "len_p50": 20.0, "len_p90": 20.0, "reply_share": 0.05,
            "reply_latency_p50_s": 30.0, "threads": 0.5}
#: An alarm is a SHIFT only when the MEDIAN of every value from the alarm on stays this many baseline SDs
#: beyond the baseline mean in the alarm's direction. A dip that recovers is noise: the detector resets and
#: scans on (a two-day dip of 2.5 SD before a real step had alarmed "down" ahead of the step). The median,
#: not the mean: two extreme dip days move a mean past a tight baseline's SD, never a median.
PERSIST_SD = 1.0


def cusum(values: Sequence[float], *, baseline_n: int, k: float | None = None, h: float | None = None,
          floor: float = 1e-9):
    """(index of the first PERSISTENT alarm, "up"/"down", score) after the first `baseline_n`, or None. The
    baseline's mean and sample SD standardize the series; a flat baseline (SD 0) uses a floor so a constant
    series that then moves still alarms, and a constant series never does."""
    k = CUSUM_K if k is None else k                # read at call time, so the module constants govern
    h = CUSUM_H if h is None else h
    if len(values) <= baseline_n or baseline_n < 2:
        return None
    base = values[:baseline_n]
    mu = statistics.fmean(base)
    sd = statistics.stdev(base)
    # The SD is never below the metric's floor or a tenth of the baseline mean: a flat baseline (SD 0) still
    # alarms on a step with a readable score, and nothing after the baseline can widen it.
    sd = max(sd, abs(mu) * 0.1, floor, 1e-9)
    hi = lo = 0.0
    for i in range(baseline_n, len(values)):
        z = (values[i] - mu) / sd
        hi = max(0.0, hi + z - k)
        lo = max(0.0, lo - z - k)
        for direction, score in (("up", hi), ("down", lo)):
            if score > h:
                sign = 1 if direction == "up" else -1
                tail = values[i:]
                if len(tail) < CONFIRM_DAYS:
                    return None                       # too few values after the alarm: unconfirmed
                held = ((statistics.median(tail) - mu) * sign >= PERSIST_SD * sd
                        and (statistics.median(values[-CONFIRM_DAYS:]) - mu) * sign >= PERSIST_SD * sd)
                if held:
                    return i, direction, score
                hi = lo = 0.0                     # a dip that recovered: reset and scan on
                break
    return None


def jsd(p: dict, q: dict) -> float:
    """Jensen-Shannon divergence (base 2, in [0, 1]) of two count dicts. Empty sides give 0."""
    sp, sq = sum(p.values()), sum(q.values())
    if not sp or not sq:
        return 0.0
    keys = set(p) | set(q)
    pp = {x: p.get(x, 0) / sp for x in keys}
    qq = {x: q.get(x, 0) / sq for x in keys}
    mm = {x: (pp[x] + qq[x]) / 2 for x in keys}

    def kl(a, b):
        return sum(a[x] * math.log2(a[x] / b[x]) for x in keys if a[x] > 0)
    return 0.5 * kl(pp, mm) + 0.5 * kl(qq, mm)


def shifts_for_seat(seat: str, days: Sequence[dict]) -> list[Shift]:
    """`days`: one dict per ACTIVE day, oldest first, with the METRICS keys and `type_mix`."""
    out: list[Shift] = []
    if len(days) < MIN_BASELINE_DAYS + 1:
        return out
    for metric in METRICS:
        series = [(d["day"], d.get(metric)) for d in days if isinstance(d.get(metric), (int, float))]
        # Per metric, after missing values are dropped: reply latency exists only on days with a reply, and a
        # baseline sized from all active days had left it never judged (Codex on #148).
        baseline_n = max(MIN_BASELINE_DAYS, len(series) // 2)
        if len(series) <= baseline_n:
            continue
        values = [float(v) for _, v in series]
        hit = cusum(values, baseline_n=baseline_n, floor=SD_FLOOR.get(metric, 1e-9))
        if hit is None:
            continue
        i, direction, score = hit
        out.append(Shift(seat, metric, str(series[i][0]), direction,
                         round(statistics.fmean(values[:baseline_n]), 3),
                         round(statistics.fmean(values[i:]), 3), round(score, 2)))
    if len(days) >= RECENT_DAYS + MIN_BASELINE_DAYS:
        before, after = {}, {}
        for d in days[:-RECENT_DAYS]:
            for t, n in (d.get("type_mix") or {}).items():
                before[t] = before.get(t, 0) + n
        for d in days[-RECENT_DAYS:]:
            for t, n in (d.get("type_mix") or {}).items():
                after[t] = after.get(t, 0) + n
        score = jsd(before, after)
        if score >= JSD_ALARM:
            out.append(Shift(seat, "type_mix", str(days[-RECENT_DAYS]["day"]), "changed", 0.0, 0.0, round(score, 3)))
    return out


def report_from_rows(rows: Iterable[dict]) -> dict:
    """Group SERIES_SQL rows by seat and judge each. Labels, days and numbers only."""
    by_seat: dict[str, list[dict]] = {}
    for r in rows:
        by_seat.setdefault(r["seat"] or "", []).append(r)
    shifts: list[Shift] = []
    judged = 0
    for seat, days in sorted(by_seat.items()):
        days = sorted(days, key=lambda d: str(d["day"]))
        if len(days) >= MIN_BASELINE_DAYS + 1:
            judged += 1
        shifts.extend(shifts_for_seat(seat, days))
    return {"seats": len(by_seat), "seats_judged": judged, "shifts": [dataclasses.asdict(s) for s in shifts],
            "not_measured": ["owed_ack_rate", "embedding_centroid_shift"]}


def report(conn, *, days: int) -> dict:
    with conn.cursor() as cur:
        cur.execute(SERIES_SQL, {"days": int(days)})
        cols = [c.name for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    out = report_from_rows(rows)
    out["window_days"] = int(days)
    return out
