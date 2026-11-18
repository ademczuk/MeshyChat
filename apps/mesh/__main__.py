"""CLI for the mesh graph.

    python -m apps.mesh rebuild                 # TRUNCATE and replay the whole log
    python -m apps.mesh catch-up [--batch N]    # one bounded pass, what the job does
    python -m apps.mesh backfill --types closes,mentions,in_thread_inherited [--batch N]
    python -m apps.mesh inbox --label txxxx     # what is addressed to a label
    python -m apps.mesh owed --label txxxx      # acks a label owes
    python -m apps.mesh thread --id <thread>    # everything in a thread
    python -m apps.mesh drift                   # the four spellings, as numbers
    python -m apps.mesh usage [--days 7]        # per-route calls and peers (divergence 018)
    python -m apps.mesh usage --prune           # apply the 90-day retention first
    python -m apps.mesh transcript-usage [--days 7]  # per-seat transcript searches (TKT-4629)
    python -m apps.mesh echo-report [--days 1]  # repeats of another seat's message without credit (085)
    python -m apps.mesh drift-report [--days 30] # per-seat daily series and their change points (086)
    python -m apps.mesh mentions-audit stale [--since ISO]      # hash:/path: edges the extractor no longer writes (089)
    python -m apps.mesh mentions-audit frequency [--since ISO]  # the digests a backfill would write, with their context

A module entry point (`-m`), not a script run by path, for the reason recorded in
apps/board/__main__.py: a script run by path puts its own directory on sys.path rather
than the repository root.

`rebuild` exists as a verb so that discipline 3 in apps/mesh/projection.py is a thing an
operator DOES rather than a thing the code could do. Exit codes: 0 done, 2 could not run.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys

from apps.core import dbconn
from apps.mesh import audit, drift as behaviour, echo, graph, projection, usage


def _report(r: projection.Report, verb: str) -> None:
    print(f"{verb}: read {r.rows_read} row(s), wrote {r.edges_written} edge(s)"
          + (", MORE PENDING" if r.more else ""))
    if r.skipped:
        print("  skipped: " + ", ".join(f"{k}={n}" for k, n in sorted(r.skipped.items())))
    for line in r.conflicts[:20]:
        print(f"  conflict: {line}")
    if len(r.conflicts) > 20:
        print(f"  ... {len(r.conflicts) - 20} more conflict(s)")
    if r.watermark_at is not None:
        print(f"  watermark: {r.watermark_at.isoformat()} / {r.watermark_id}")


def _iso(text: str) -> _dt.datetime:
    """An ISO time for --since; one without an offset is UTC."""
    when = _dt.datetime.fromisoformat(text)
    return when if when.tzinfo else when.replace(tzinfo=_dt.timezone.utc)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dsn", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("rebuild", help="TRUNCATE the projection and replay the log")
    cu = sub.add_parser("catch-up", help="one bounded catch-up pass")
    cu.add_argument("--batch", type=int, default=projection.DEFAULT_BATCH)
    bf = sub.add_parser("backfill", help="insert new kinds without a destructive rebuild")
    bf.add_argument("--types", required=True, help="comma-separated: " + ",".join(projection.BACKFILL_TYPES))
    bf.add_argument("--batch", type=int, default=projection.DEFAULT_BATCH)

    ib = sub.add_parser("inbox", help="messages addressed to a label")
    ib.add_argument("--label", required=True)
    ib.add_argument("--limit", type=int, default=50)
    ib.add_argument("--no-cc", action="store_true")

    ow = sub.add_parser("owed", help="acks a label owes")
    ow.add_argument("--label", required=True)

    th = sub.add_parser("thread", help="everything in one thread")
    th.add_argument("--id", required=True)

    sub.add_parser("drift", help="edge_type -> src_field -> count")

    us = sub.add_parser("usage", help="per-route calls and distinct peers, last N days")
    us.add_argument("--days", type=int, default=usage.WINDOW_DAYS)
    us.add_argument("--prune", action="store_true",
                    help=f"first delete rows older than {usage.RETENTION_DAYS} days "
                         "(the retention policy in sql_meshychat/005)")

    tu = sub.add_parser("transcript-usage", help="per-seat transcript searches, last N days (TKT-4629)")
    tu.add_argument("--days", type=int, default=usage.WINDOW_DAYS)

    er = sub.add_parser("echo-report", help="advisory: a seat repeating another's message without credit (085)")
    er.add_argument("--days", type=int, default=1, help="judge the messages of the last N days (default 1)")

    dr = sub.add_parser("drift-report", help="per-seat daily series and their change points; a report (086)")
    dr.add_argument("--days", type=int, default=30)

    ma = sub.add_parser("mentions-audit", help="read-only: stale hash:/path: edges, or the digests a backfill "
                                                 "would write (089). `frequency` prints message text")
    ma.add_argument("report", choices=("stale", "frequency"))
    ma.add_argument("--since", type=_iso, default=None, help="only rows recorded at or after this ISO time")
    ma.add_argument("--top", type=int, default=15, help="frequency: values and contexts listed per length")
    ma.add_argument("--width", type=int, default=24, help="frequency: characters shown before each digest")

    a = ap.parse_args(argv)
    try:
        with dbconn.serving_connection(a.dsn) as conn:
            if a.cmd == "rebuild":
                _report(projection.rebuild(conn), "rebuild")
                return 0
            if a.cmd == "catch-up":
                _report(projection.catch_up(conn, max_batch=a.batch), "catch-up")
                return 0
            if a.cmd == "backfill":
                types = tuple(x.strip() for x in a.types.split(",") if x.strip())
                r = projection.backfill(conn, types=types, batch=a.batch)
                print(f"backfill: read {r.rows_read} row(s), wrote {r.edges_written} edge(s)")
                return 0
            if a.cmd == "mentions-audit":
                with dbconn.request_server_internal(conn) as c:
                    r = (audit.stale(c, since=a.since) if a.report == "stale"
                         else audit.frequency(c, since=a.since, top=a.top, width=a.width))
                print(json.dumps(r, indent=1))
                return 0
            if a.cmd == "echo-report":
                until = _dt.datetime.now(_dt.timezone.utc)
                with dbconn.request_server_internal(conn) as c:
                    print(json.dumps(echo.report(c, since=until - _dt.timedelta(days=max(1, a.days)),
                                                 until=until), indent=1))
                return 0
            if a.cmd == "drift-report":
                with dbconn.request_server_internal(conn) as c:
                    print(json.dumps(behaviour.report(c, days=max(8, a.days)), indent=1, default=str))
                return 0
            if a.cmd == "usage":
                if a.prune:
                    print(f"pruned {usage.prune(conn)} row(s) older than "
                          f"{usage.RETENTION_DAYS} days")
                with dbconn.request_server_internal(conn) as c:
                    for line in usage.render(usage.summary(c, days=a.days)):
                        print(line)
                return 0
            if a.cmd == "transcript-usage":
                with dbconn.request_server_internal(conn) as c:
                    for line in usage.render_transcript_usage(usage.transcript_daily(c, days=a.days)):
                        print(line)
                return 0
            with dbconn.request_server_internal(conn) as c:
                pair = graph.coverage(c)
                if a.cmd == "inbox":
                    res = graph.inbox(c, a.label, include_cc=not a.no_cc, limit=a.limit)
                    for line in graph.render_hits(f"inbox for {a.label}", res):
                        print(line)
                elif a.cmd == "owed":
                    items = graph.owed_acks(c, a.label)
                    print(f"acks owed by {a.label}: {len(items)}")
                    for o in items:
                        spoke = "spoke since" if o.answered_in_thread else "silent since"
                        print(f"  {o.hit.recorded_at.isoformat()}  {o.hit.key}  "
                              f"thread {o.thread_id or '<none>'}  [{spoke}]")
                elif a.cmd == "thread":
                    for line in graph.render_hits(f"thread {a.id}", graph.thread(c, a.id)):
                        print(line)
                elif a.cmd == "drift":
                    for edge_type, fields in graph.drift(c).items():
                        if fields:
                            print(f"  {edge_type:13} " + ", ".join(
                                f"{f}={n}" for f, n in sorted(fields.items())))
                print(graph.render_coverage(pair))
        return 0
    except (OSError, RuntimeError) as exc:
        print(f"COULD NOT RUN: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
