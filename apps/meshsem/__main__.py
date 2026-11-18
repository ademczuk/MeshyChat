"""CLI for semantic mesh retrieval (divergence 019).

    python -m apps.meshsem status                 # the space's reference, gate and coverage
    python -m apps.meshsem catch-up [--until-done] [--batch N]
                                                  # one bounded embed tick, what the job does
    python -m apps.meshsem graph-catch-up [--until-done]
                                                  # one tick of the graph walker over every
                                                  # namespace MESHY_GRAPH_NAMESPACES lists
                                                  # besides the mail (divergence 060)
    python -m apps.meshsem graph-pilot --ns NS --out FILE [--entries 20] [--k 3] [--seed 1]
                                                  # the related-entries value pilot page for a
                                                  # graph namespace (read only; OI-57)
    python -m apps.meshsem search --q "..." [--k 10] [--no-rerank]
                                                  # fused lexical + embedding ranking,
                                                  # then reranked unless --no-rerank
                                                  # (divergence 026, as amended)

Runs through dbconn.serving_connection(), so as the RLS-bound app role, exactly as the
job does. `catch-up --until-done` drains a backlog tick after tick and stops on the first
tick that makes no progress, printing why. Exit codes: 0 done, 1 a tick stopped with no
admitted provider, 2 could not run.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# The gate, the space and the provider list are read through Django settings
# (apps/core/env.setting), and reading an unconfigured settings object raises rather than
# falling back to the environment. apps/mesh's CLI never reads a setting; this one does.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

from apps.core import dbconn  # noqa: E402
from apps.meshsem import _cli  # noqa: E402


def _print_report(r) -> None:
    print(f"catch-up [{r.space}]: read {r.rows_read}, processed {r.rows_processed}: "
          f"embedded {r.embedded}, unchanged {r.unchanged}, skipped_forbidden "
          f"{r.skipped_forbidden}, skipped_empty {r.skipped_empty}, "
          f"skipped_no_schema_version {r.skipped_no_schema_version}"
          + (f", STOPPED: {r.stopped}" if r.stopped else "")
          + (", MORE PENDING" if r.more else ""))
    print(f"  gate open: {r.gate_open}" + (" (CHANGED this tick; walk restarted"
                                          f", {r.purged} vector(s) purged)"
                                          if r.gate_changed else ""))
    print(f"  lexical rows written: {r.lexed}" + (" (derivation CHANGED this tick; walk "
                                                  "restarted)" if r.derivation_changed
                                                  else ""))
    if r.reference_recorded:
        print("  reference RECORDED this tick")
    if r.providers_used:
        print(f"  providers used: {', '.join(r.providers_used)}")
    _cli.print_attempts(r.attempts)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dsn", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="reference, gate and coverage for the configured space")
    cu = sub.add_parser("catch-up", help="one bounded embed tick")
    cu.add_argument("--batch", type=int, default=None)
    cu.add_argument("--until-done", action="store_true")
    gc = sub.add_parser("graph-catch-up", help="one tick of the graph walker (divergence 060)")
    gc.add_argument("--until-done", action="store_true")
    gp = sub.add_parser("graph-pilot", help="the related-entries value pilot page (read only)")
    gp.add_argument("--ns", required=True)
    gp.add_argument("--out", required=True)
    gp.add_argument("--entries", type=int, default=20)
    gp.add_argument("--k", type=int, default=3)
    gp.add_argument("--seed", type=int, default=1)
    se = sub.add_parser("search", help="semantic search, as the route answers it")
    se.add_argument("--q", required=True)
    se.add_argument("--k", type=int, default=10)
    se.add_argument("--no-rerank", dest="rerank", action="store_false",
                    help="do not rerank the fused top 40 (reranked by default, as the "
                         "route is: divergence 026, as amended)")
    # The default it asked for is the default now; accepted so an old command line runs.
    se.add_argument("--rerank", dest="rerank", action="store_true", help=argparse.SUPPRESS)
    se.set_defaults(rerank=True)
    a = ap.parse_args(argv)

    from apps.meshsem import embedjob, graphpilot, graphwalk, search, store
    try:
        space = store.configured_space()
        with dbconn.serving_connection(a.dsn) as conn:
            if a.cmd == "catch-up":
                batch = a.batch or embedjob.DEFAULT_BATCH
                return _cli.drain(lambda: embedjob.catch_up(conn, space=space, max_batch=batch),
                                  _print_report, lambda r: r.stopped == "provider_unavailable",
                                  a.until_done)
            if a.cmd == "graph-catch-up":
                if not graphwalk.walked_namespaces():
                    print("graph-catch-up: MESHY_GRAPH_NAMESPACES lists no namespace besides the mail; nothing to do")
                    return 0
                while True:
                    reports = graphwalk.catch_up(conn, space=space)
                    for r in reports:
                        print(f"graph-catch-up [{r.namespace} in {r.space}]: heads {r.heads}: embedded {r.embedded_rows} "
                              f"row(s) in {r.embedded_chunks} chunk(s), lexed {r.lexed}, unchanged {r.unchanged}, "
                              f"skipped_forbidden {r.skipped_forbidden}, skipped_no_content {r.skipped_no_content}, "
                              f"deleted {r.deleted_rows}, pending {r.pending}"
                              + (f", STOPPED: {r.stopped}" if r.stopped else ""))
                    if any(r.stopped == "provider_unavailable" and r.pending for r in reports):
                        return 1
                    if not a.until_done or not any(r.more for r in reports):
                        return 0
            if a.cmd == "graph-pilot":
                with dbconn.request_server_internal(conn) as c:
                    # Read only by the session, not only by the module: a later edit that wrote would fail here.
                    c.execute("SET TRANSACTION READ ONLY")
                    page = graphpilot.build(c, space, a.ns, entries_n=a.entries, k=a.k, seed=a.seed)
                with open(a.out, "w", encoding="utf-8") as f:
                    f.write(page)
                print(f"graph-pilot: wrote {a.out} ({page.count(chr(10) + '## ')} entries)")
                return 0
            if a.cmd == "status":
                gate = store.forbidden_rows_open()
                with dbconn.request_server_internal(conn) as c:
                    with c.cursor() as cur:
                        state = store.read_state(cur, space) or {}
                    cov = store.coverage(c, space, gate_open=gate)
                print(f"space {space.name}")
                print(f"  reference: {state.get('reference_provider') or 'NOT RECORDED'}"
                      + (f" at {state['reference_recorded_at'].isoformat()}"
                         if state.get("reference_recorded_at") else ""))
                print(f"  gate (MESHY_EMBED_FORBIDDEN_ROWS) open: {gate}; walk ran under "
                      f"{state.get('forbidden_rows_open')}")
                print(f"  coverage: {json.dumps(cov.as_json())}")
                return 0
            if a.cmd == "search":
                body = search.search(conn, space, a.q, k=a.k, rerank=a.rerank)
                print(json.dumps({k: body[k] for k in ("space", "method", "degraded",
                                                        "providers", "reranked",
                                                        "coverage", "truncated")}))
                for h in body["hits"]:
                    print(f"  {h['fused_score']:.5f} {h['ranks']} {h['rerank_score']}  "
                          f"{h['key']}  {(h['body'] or '')[:100]!r}")
                return 0
    except search.SemanticUnavailable as exc:
        print(f"UNAVAILABLE: {exc.code}: {exc.message}", file=sys.stderr)
        return 2
    except (OSError, RuntimeError, KeyError) as exc:
        print(f"COULD NOT RUN: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
