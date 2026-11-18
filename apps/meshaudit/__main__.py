"""CLI for the GemmaSwarm auditor (divergence 021).

    python -m apps.meshaudit derive [--write] [--limit N] [--key msg/... ...]
                                    # dry run unless --write: what would be inferred
    python -m apps.meshaudit status # eligible / audited / inferred edges, by prompt version

`derive` without `--write` asks the model and prints every proposal it would record and
every refusal by reason, inside READ ONLY transactions, and writes nothing. `--write`
records what survived validation into mesh_inferred_edges and one ledger row per row
asked. `--key` audits exactly the named rows (a spot check), still only when eligible.

Chat providers are the modelhost registry's `chat` entries (MESHY_MODEL_PROVIDERS, in
preference order); apps/meshsem/README.md has this box's list. Mesh text reaches only an
IP-literal loopback or RFC1918 host (MUST-8, apps/meshsem/providers.py).

Exit codes: 0 done; 1 rows needed a model and no chat provider answered (nothing about
those rows was recorded, so the next run asks again); 2 could not run (no database, or the
mesh_edges projection has never been caught up).
"""
from __future__ import annotations

import argparse
import os
import sys

# Provider configuration is read through Django settings when they are configured
# (apps/modelhost/registry.declared), as apps/meshsem's CLI does.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

from apps.core import dbconn, rls  # noqa: E402
from apps.meshaudit import derive as D  # noqa: E402
from apps.meshaudit import prompt as P  # noqa: E402
from apps.meshaudit import store  # noqa: E402


def _print_report(r: D.Report) -> None:
    mode = "WRITE" if r.write else "DRY RUN"
    print(f"derive [{mode}] prompt {r.prompt_version}: selected {r.rows_selected}, "
          f"asked {r.rows_asked}, no_body {r.rows_no_body}, "
          f"nothing_to_ask {r.rows_nothing_to_ask}")
    for row in r.rows:
        if row.outcome != "asked":
            print(f"  {row.key}: {row.outcome}")
            continue
        print(f"  {row.key}: asked {', '.join(row.asked)} via {row.provider_id}")
        for p in row.accepted:
            print(f"    INFERRED {p.edge_type} -> {p.dst}")
        for x in row.refusals:
            what = f"{x.edge_type} {x.value!r}" if x.edge_type else repr(x.value)
            print(f"    refused {x.reason}: {what}")
    acc = ", ".join(f"{t}={n}" for t, n in r.accepted.items())
    print(f"  accepted: {acc}")
    if r.refused:
        print("  refused: " + ", ".join(f"{k}={n}" for k, n in sorted(r.refused.items())))
    if r.write:
        print(f"  written: {r.inserted} edge(s); already held: {r.already_held}")
    else:
        n = sum(r.accepted.values())
        print(f"  DRY RUN: nothing written. --write would record {n} inferred edge(s) "
              f"and {r.rows_asked + r.rows_no_body + r.rows_nothing_to_ask} ledger row(s).")
    if r.providers_used:
        print(f"  providers used: {', '.join(r.providers_used)}")
    if r.stopped:
        print(f"  STOPPED: {r.stopped}")
        for a in r.attempts:
            print(f"    provider {a.get('id')}: {a.get('verdict')} {a.get('reason', '')}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dsn", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    de = sub.add_parser("derive", help="propose edges for legacy rows (dry run by default)")
    de.add_argument("--write", action="store_true",
                    help="record what survives validation (default: dry run)")
    de.add_argument("--limit", type=int, default=D.DEFAULT_LIMIT)
    de.add_argument("--key", action="append", default=None,
                    help="audit only this msg/ key (repeatable)")
    de.add_argument("--budget-s", type=float, default=D.RUN_BUDGET_S)
    sub.add_parser("status", help="eligible, audited and inferred counts")

    a = ap.parse_args(argv)
    try:
        with dbconn.serving_connection(a.dsn) as conn:
            if a.cmd == "status":
                with rls.server_internal(conn):
                    cov = store.coverage(conn, prompt_version=P.PROMPT_VERSION)
                print(f"prompt {P.PROMPT_VERSION}")
                print(f"  coverage: {cov}")
                if cov.get("projection_not_run"):
                    print("  mesh_edges has never been caught up: nothing is eligible yet")
                return 0
            if a.limit <= 0:
                print("COULD NOT RUN: --limit must be positive", file=sys.stderr)
                return 2
            report = D.derive(conn, write=a.write, limit=a.limit, keys=a.key,
                              run_budget_s=a.budget_s)
            _print_report(report)
            return 1 if report.provider_failed else 0
    except (OSError, RuntimeError) as exc:
        # store.ProjectionNotRun is a RuntimeError: a never-caught-up mesh_edges is the
        # documented exit-2 case and lands here.
        print(f"COULD NOT RUN: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
