"""CLI for the persisted related_to edges (divergence 032).

    python -m apps.meshsem.related_cli status            # calibration, walk state, coverage
    python -m apps.meshsem.related_cli catch-up [--until-done] [--batch N]
                                                         # one bounded tick, what the job does
    python -m apps.meshsem.related_cli show --key msg/...   # one message's edges, both ways
    python -m apps.meshsem.related_cli calibrate [--write-calibration] [--typing] ...
                                                         # tools/related_calibration.py

Kept out of apps/meshsem/__main__.py on purpose (a parallel branch edits that file); the
integrator can fold it in as `python -m apps.meshsem related ...`.

Runs through dbconn.serving_connection(), as the RLS-bound app role, exactly as the job
does. Exit codes: 0 done, 1 a tick was refused (the job would report an error), 2 could
not run. `catch-up --until-done` stops on the first tick that makes no progress, and says
why. `calibrate` is the one command that talks to a live model on purpose: it runs only
when invoked by hand, against a throwaway database, on the SYNTHETIC fixture.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

from apps.core import dbconn  # noqa: E402
from apps.meshsem import _cli  # noqa: E402


#: The walk state `status` prints, read and labelled from this one list so the two cannot
#: drift apart.
_STATUS_COLUMNS = ("threshold_version", "forbidden_rows_open", "watermark_at",
                   "rows_seen", "sources_related", "sources_none_above",
                   "sources_no_candidates", "sources_targets_gone",
                   "sources_rerank_refused", "pairs_refused",
                   "rows_skipped_forbidden",
                   "rows_skipped_no_vector", "edges_written", "last_status",
                   "last_status_at", "last_detail")


def _print_report(r) -> None:
    print(f"catch-up [{r.space}]: {r.status}"
          + (f" ({r.detail})" if r.detail else "")
          + f"; read {r.rows_read}, processed {r.rows_processed}: related "
            f"{r.sources_related}, none above threshold {r.sources_none_above}, "
            f"no candidates {r.sources_no_candidates}, targets gone "
            f"{r.sources_targets_gone}, rerank refused {r.sources_rerank_refused} "
            f"({r.pairs_refused} pair(s)), skipped_forbidden "
            f"{r.skipped_forbidden}, skipped_no_vector {r.skipped_no_vector}; edges written "
            f"{r.edges_written}" + (", MORE PENDING" if r.more else ""))
    if r.gate_changed or r.threshold_changed:
        print(f"  walk RESTARTED: gate changed {r.gate_changed} (open {r.gate_open}, "
              f"{r.purged} edge(s) purged), threshold changed {r.threshold_changed}")
    if r.purged and not r.gate_changed:
        print(f"  gate closed: {r.purged} edge(s) touching a forbidden row purged")
    if r.rerank_provider_id:
        print(f"  reranker pinned: {r.rerank_provider_id}")
    _cli.print_attempts(r.attempts)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dsn", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="calibration, walk state and coverage")
    cu = sub.add_parser("catch-up", help="one bounded tick")
    cu.add_argument("--batch", type=int, default=None)
    cu.add_argument("--until-done", action="store_true")
    sh = sub.add_parser("show", help="one message's edges")
    sh.add_argument("--key", required=True)
    sub.add_parser("calibrate", add_help=False,
                   help="run the calibration against the LAN models (by hand only)")
    a, rest = ap.parse_known_args(argv)
    if a.cmd == "calibrate":
        # Imported here: the harness opens a throwaway database as the bootstrap owner,
        # which no apps/ module may do (tests/test_every_connection_is_role_bound.py).
        return importlib.import_module("tools.related_calibration").main(rest)
    if rest:
        ap.error(f"unrecognized arguments: {' '.join(rest)}")

    from apps.meshsem import related, related_reads, relatedcal, store
    try:
        space = store.configured_space()
        with dbconn.serving_connection(a.dsn) as conn:
            if a.cmd == "catch-up":
                batch = a.batch or related.DEFAULT_BATCH
                return _cli.drain(lambda: related.catch_up(conn, space=space, max_batch=batch),
                                  _print_report, lambda r: r.status in related.ERROR_STATUSES,
                                  a.until_done)
            gate = store.forbidden_rows_open()
            try:
                cal = relatedcal.load()
                why = None if cal else f"none enabled at {relatedcal.configured_path()}"
            except relatedcal.CalibrationInvalid as exc:
                cal, why = None, f"INVALID: {exc}"
            if cal is not None and cal.space != space.name:
                why = f"calibrated for space {cal.space!r}, configured {space.name!r}"
                cal = None
            if a.cmd == "status":
                print(f"space {space.name}; gate (MESHY_EMBED_FORBIDDEN_ROWS) open: {gate}")
                if cal:
                    print(f"  calibration {cal.threshold_version}: threshold {cal.threshold} "
                          f"logits, k {cal.k}, reverse depth {cal.reverse_depth}, "
                          f"aggregator {cal.aggregator}, canary tolerance {cal.tolerance}")
                else:
                    print(f"  calibration: {why}. The job records not_calibrated and "
                          "writes nothing.")
                with dbconn.request_server_internal(conn) as c:
                    with c.cursor() as cur:
                        cur.execute(f"SELECT {', '.join(_STATUS_COLUMNS)} "
                                    "FROM mesh_related_projection WHERE space = %s",
                                    (space.name,))
                        row = cur.fetchone()
                    cov = related_reads.coverage(c, space, gate=gate, cal=cal)
                if row is None:
                    print("  walk: never run")
                else:
                    print("  walk: " + json.dumps(dict(zip(_STATUS_COLUMNS, row)), default=str))
                print(f"  coverage: {json.dumps(cov)}")
                return 0
            if a.cmd == "show":
                if cal is None:
                    print(f"no calibration to show edges under: {why}")
                    return 2
                with dbconn.request_server_internal(conn) as c:
                    with c.cursor() as cur:
                        cur.execute("SELECT id FROM shared_memory WHERE key = %s "
                                    "AND is_head AND namespace = 'shared/mesh-chat'",
                                    (a.key,))
                        row = cur.fetchone()
                    if row is None:
                        print(f"no head message {a.key!r}")
                        return 2
                    hits, truncated, hidden = related_reads.edges_of(
                        c, space, row[0], cal, gate=gate, limit=related_reads.LIMIT_MAX,
                        with_body=False)
                print(f"{a.key} under calibration {cal.threshold_version} "
                      f"(threshold {cal.threshold}): {len(hits)} edge(s)"
                      + (" (truncated)" if truncated else "")
                      + (f", {hidden} hidden as now declared" if hidden else ""))
                for h in hits:
                    s = h["scores"]
                    print(f"  {h['direction']:3} pair {s['pair']:+.3f} (fwd {s['forward']:+.3f}"
                          f", rev {s['reverse']:+.3f}) rank {s['rank']}  {h['key']}  "
                          f"thread {h['thread_id']}")
                return 0
    except (OSError, RuntimeError, KeyError) as exc:
        print(f"COULD NOT RUN: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
