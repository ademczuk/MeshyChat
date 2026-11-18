"""CLI for the board: ASK what the state of a unit of work is, or DECLARE an act.

    python -m apps.board --subject pr/933
    python -m apps.board                          # every subject that has an act
    python -m apps.board declare --subject pr/933 --type reviewed --ref auto \
        --state-file C:/Users/me/.agentmeshy-uxxx-state.json

`declare` is the cheap path: three fields, and `--ref auto` is the HEAD of the checkout
you are standing in (refused, not defaulted, outside a repository). The act is signed
with the state file's key and written through `memory.write` in-process, crossing every
gate that handler has. `--body` overrides the prose restatement; the structured fields
carry the act either way.

Deliberately a module entry point (`-m`) rather than a script run by path. A script run
by path puts its own directory on sys.path instead of the repository root, which is how
`apps/core/sqlmigrate.py` broke on 2026-09-21 the moment it grew an `apps.` import.
"""
from __future__ import annotations

import argparse
import sys

from apps.board import declare, derive, projection
# Re-exported on purpose as well as used: tests import both names from this entry point.
from apps.board.modelclient import _http_generate, answer_text  # noqa: F401
from apps.calls.errors import SignatureFailed
from apps.core import dbconn
from apps.memoryskills.errors import HandlerError


def _read(args) -> int:
    with dbconn.serving_connection(args.dsn) as conn:
        with dbconn.request_server_internal(conn) as c:
            pair = projection.adoption(c)
            subjects = [args.subject] if args.subject else projection.subjects_seen(c)
            if not subjects:
                declared, total = pair
                print(f"No subject has a declared act. {declared} of {total} rows in the "
                      f"namespace carry one. That is adoption, not absence of work.")
                return 0
            for subject in subjects:
                for line in projection.render(projection.state_for(c, subject),
                                              adoption_pair=pair):
                    print(line)
                for line in projection.render_same_number(
                        subject, projection.same_number_acts(c, subject)):
                    print(line)
    return 0


def _refusal(exc) -> str:
    """`CODE: message` for a handler refusal and for an envelope-check refusal alike.
    `str()` of a `SignatureFailed` is its message alone, which would drop the code."""
    return f"{exc.code}: {exc.message}"


def _declare(args) -> int:
    try:
        identity = declare.Identity.from_state_file(args.state_file, label=args.label)
    except (OSError, ValueError) as exc:
        print(f"NOT DECLARED: {exc}", file=sys.stderr)
        return 2
    try:
        with dbconn.serving_connection(args.dsn) as conn:
            result = declare.declare_in_process(
                conn, identity, subject=args.subject, verb=args.type, ref=args.ref,
                body=args.body, to=tuple(args.to.split(",")), thread_id=args.thread,
                checkout=args.checkout)
            with dbconn.request_server_internal(conn) as c:
                state = projection.state_for(c, result["subject"])
                pair = projection.adoption(c)
    except declare.NoCheckout as exc:
        print(f"NOT DECLARED: {exc}", file=sys.stderr)
        return 2
    except (HandlerError, SignatureFailed) as exc:
        # Exit 1 is the daemon REFUSING the act, and the reason is its own message. It is
        # a different exit from 2 (could not even try) because the two need different
        # responses: fix the act, versus fix the environment. An envelope the checks of
        # divergence 038 refuse is a refusal of the act too.
        print(f"REFUSED: {_refusal(exc)}", file=sys.stderr)
        return 1
    print(f"DECLARED {result['key']} v{result['version']}"
          + (" (replay of an earlier identical envelope)" if result.get("replay") else ""))
    for line in projection.render(state, adoption_pair=pair):
        print(line)
    return 0


def _derive(args) -> int:
    generate = None
    if args.model_url:
        # Refused HERE, before a row is read, rather than per row inside the sweep: the
        # sweep turns a raising model into a refusal, so a refused host would otherwise
        # read as "the model declined everything" and the run would exit 0.
        from apps.modelhost import locality

        where = locality.classify(args.model_url)
        if not where.content_ok:
            print(f"NOT RUN: --model-url {args.model_url!r} may not receive mesh content "
                  f"(MUST-8, divergence 019): {where.reason}. Name the model host by an IP "
                  "literal on this LAN, e.g. http://192.168.50.10:7872.", file=sys.stderr)
            return 2
        generate = _http_generate(args.model_url, args.model, args.max_tokens)
    with dbconn.serving_connection(args.dsn) as conn:
        with dbconn.request_server_internal(conn) as c:
            rows = derive.recent_rows(c, limit=args.limit)
            candidates = derive.undeclared(rows)
    proposals, reasons = derive.sweep(candidates, generate=generate, model_id=args.model)
    print(f"read {len(rows)} rows, {len(candidates)} carry no declared act of their own")
    print(f"outcomes: {reasons}")
    for p in proposals:
        print(f"  {p.subject:14s} {p.verb:10s} {p.ref[:12]:12s} [{p.by}] <- {p.evidence[:88]}")
    if not args.write:
        print(f"\n{len(proposals)} proposal(s). DRY RUN: nothing was written. These are "
              f"PROPOSALS, and writing them puts model-read claims on a shared substrate, "
              f"so it is a separate decision. Re-run with --write --state-file to declare "
              f"them as inferred.<verb> acts.")
        return 0
    if not args.state_file:
        print("--write needs --state-file: an inferred act is still SIGNED by whoever "
              "proposed it, so the deriver needs an identity of its own.", file=sys.stderr)
        return 2
    try:
        identity = declare.Identity.from_state_file(args.state_file, label=args.label)
    except (OSError, ValueError) as exc:
        print(f"NOT WRITTEN: {exc}", file=sys.stderr)
        return 2
    written = 0
    with dbconn.serving_connection(args.dsn) as conn:
        for p in proposals:
            try:
                declare.declare_in_process(
                    conn, identity, subject=p.subject, verb=p.inferred_verb, ref=p.ref,
                    in_reply_to=p.source_key,
                    body=f"INFERRED, not declared: read from {p.source_key} -- {p.evidence}")
                written += 1
            except (HandlerError, SignatureFailed) as exc:
                print(f"  REFUSED {p.subject}: {_refusal(exc)}", file=sys.stderr)
    print(f"wrote {written} inferred act(s) of {len(proposals)} proposed")
    return 0 if written == len(proposals) else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--subject", help="restrict the read to one subject, e.g. pr/933")
    ap.add_argument("--dsn", default=None)
    sub = ap.add_subparsers(dest="cmd")

    d = sub.add_parser("declare", help="declare one act (three fields; everything else is derived)")
    d.add_argument("--subject", required=True, help="the object, e.g. pr/933 or issue/12")
    d.add_argument("--type", required=True, help="the verb, e.g. reviewed, merged, held")
    d.add_argument("--ref", default=declare.REF_AUTO,
                   help=f"the version, e.g. a commit sha; {declare.REF_AUTO!r} (default) = "
                        "HEAD of --checkout, refused outside a repository")
    d.add_argument("--state-file", required=True,
                   help="the .agentmeshy-<node>-state.json holding peer_id, kid and the seed")
    d.add_argument("--label", default=None, help="the from label; default roster or peer-<id8>")
    d.add_argument("--body", default=None, help="prose; default is the restatement of the act")
    d.add_argument("--to", default=",".join(declare.DEFAULT_TO), help="comma-separated labels")
    d.add_argument("--thread", default=declare.DEFAULT_THREAD)
    d.add_argument("--checkout", default=None, help="directory whose HEAD --ref auto resolves")
    # A separate dest: a subparser's default would otherwise overwrite a --dsn given
    # before the subcommand (argparse resolves the subparser namespace last).
    d.add_argument("--dsn", dest="declare_dsn", default=None)

    v = sub.add_parser("derive", help="PROPOSE acts from prose already on the mesh (dry run)")
    v.add_argument("--limit", type=int, default=300, help="rows to read, newest first")
    v.add_argument("--model-url", default=None,
                   help="OpenAI-compatible base url for the ambiguous rows the rules refuse, "
                        "e.g. http://192.168.50.10:7872 (HQB's chat model). An IP literal, "
                        "loopback or RFC1918 only (MUST-8). Omitted: rules only, no GPU.")
    v.add_argument("--model", default="local", help="model id recorded on each proposal")
    v.add_argument("--max-tokens", type=int, default=2000,
                   help="a thinking model needs room to finish: measured, 640 returned "
                        "nothing and 2000 answered in 5.3s")
    v.add_argument("--write", action="store_true",
                   help="actually declare the proposals as inferred.<verb> acts. OFF by "
                        "default: this publishes model-read claims to a shared substrate.")
    v.add_argument("--state-file", default=None, help="identity to sign inferred acts with")
    v.add_argument("--label", default=None)
    v.add_argument("--dsn", dest="derive_dsn", default=None)

    args = ap.parse_args(argv)
    if args.cmd == "declare":
        args.dsn = args.declare_dsn or args.dsn
        return _declare(args)
    if args.cmd == "derive":
        args.dsn = args.derive_dsn or args.dsn
        return _derive(args)
    return _read(args)


if __name__ == "__main__":
    raise SystemExit(main())
