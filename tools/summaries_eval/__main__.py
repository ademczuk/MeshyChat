"""`python -m tools.summaries_eval <step>`: the measurement's steps (see __init__.py)."""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

import psycopg  # noqa: E402

from apps.modelhost import registry  # noqa: E402
from apps.meshsum import prompt as P  # noqa: E402

from . import corpus, freeze, generate, linkgrade, packets, score  # noqa: E402

DEFAULT_PROVIDER_ID = "chat-hqb-5090"
DEFAULT_OUT = Path.home() / "meshychat" / "summaries-eval"


def _connect(dsn: str):
    def connect():
        # UTC like every serving connection (apps/core/dbconn.py): this bare connection would otherwise take the
        # server's zone, and the corpus's times would compare on a summer-time wall clock (codex-pxx-cli TASK AL).
        # APPENDED to the DSN's own options (or PGOPTIONS), as dbconn does: `options=` alone replaces them
        # (codex-pxx-cli TASK AM).
        from apps.core.dbconn import _options_with
        conn = psycopg.connect(dsn, autocommit=False, options=_options_with(dsn, "-c TimeZone=UTC"))
        conn.read_only = True             # every transaction on it is READ ONLY
        return conn
    return connect


def _provider(args) -> registry.Provider:
    return registry.Provider(id=args.provider_id, kind="chat", base_url=args.chat_url,
                             model="gemma-4-26b-a4b")


def _read_json(path: Path):
    """A JSON file's contents: every input a step reads is UTF-8 JSON on disk."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(path: Path, obj) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _load_generations(run_dir: Path, sample: dict) -> list[dict]:
    """Every generation in `run_dir` (the wNNNN.json files generate.run wrote), each with
    the cluster the sample gives its window, whether it was attacked (any variant but the
    control) and whether its window hosts a plant (what the streams and F8 read)."""
    clusters = {w["id"]: w["cluster"] for w in sample["windows"]}
    hosts = {p["window"] for p in sample["plants"] + sample["denial"]}
    out = []
    for f in sorted(Path(run_dir).glob("w*.json")):
        for g in _read_json(f):
            g["cluster"] = clusters.get(g["window"], g["window"])
            g["attacked"] = g["variant"] != "control"
            g["hosts_plant"] = g["window"] in hosts
            out.append(g)
    return out


# ------------------------------------------------------------------------- the steps
# One handler per subcommand, registered on its subparser (`set_defaults(handler=...)`;
# not `run`, which is judge-packets' and score's --run directory) and dispatched by
# main(). Each returns the process's exit code.

def _freeze_sample(args) -> int:
    with _connect(args.source_dsn)() as conn:
        sample = corpus.select_sample(conn, seed=args.seed,
                                      bootstrap_seed=args.bootstrap_seed,
                                      probe=P.system_leak_probe())
    _write(args.out, sample)
    print(freeze.record(args.out.name, args.out))
    print("now COMMIT", args.out, "and", freeze.FROZEN_REL,
          "before any model call (design 7.1)")
    return 0


def _freeze(args) -> int:
    print(freeze.record(args.name, args.path))
    return 0


def _reference_packets(args) -> int:
    sample = _read_json(args.sample)
    freeze.require_frozen("sample_v1.json", args.sample)
    out = packets.reference_packets(_connect(args.source_dsn), sample,
                                    provider=_provider(args), model_pin=args.model_pin)
    args.out.mkdir(parents=True, exist_ok=True)
    packets.write_jsonl(args.out / "reference_packets.jsonl", out)
    print(f"{len(out)} windows -> {args.out}")
    return 0


def _generate(args) -> int:
    refs = None
    if args.references:
        freeze.require_frozen("references_v1.json", args.references)
        refs = _read_json(args.references)
    summary = generate.run(_connect(args.source_dsn), args.sample, args.out,
                           provider=_provider(args), model_pin=args.model_pin,
                           references=refs)
    summary["canary_leaks"] = packets.canary_probe_checked(_provider(args),
                                                           args.model_pin)
    _write(args.out / "run.json", summary)
    print(json.dumps(summary, indent=2))
    return 0 if summary["minimums_met"] else 1


def _judge_packets(args) -> int:
    sample = _read_json(args.sample)
    gens = _load_generations(args.run, sample)
    plants = _read_json(args.plants)
    sections = {g["window"]: g["messages_section"] for g in gens
                if g["variant"] == "control"}
    streams, key = packets.judge_streams(gens, plants, seed=args.seed,
                                         sections=sections)
    args.out.mkdir(parents=True, exist_ok=True)
    for judge, items in streams.items():
        packets.write_jsonl(args.out / f"stream_{judge}.jsonl", items)
    _write(args.out / "key.json", key)
    packets.write_jsonl(args.out / "coverage_packets.jsonl",
                        packets.coverage_packets(gens))
    print(f"{len(streams['a'])} items per judge -> {args.out}")
    return 0


def _score(args) -> int:
    sample = _read_json(args.sample)
    run = _read_json(args.run / "run.json")
    ref_digest = freeze.require_frozen("references_v1.json", args.references)
    committed = freeze.committed_at(ref_digest)
    first = _dt.datetime.fromisoformat(run["first_generated_at"])
    if committed is None or committed >= first:
        print("REFUSED: the reference items reached git after the first generated "
              "summary (design 7.1)", file=sys.stderr)
        return 2
    j = args.judging
    gens = _load_generations(args.run, sample)
    key = _read_json(j / "key.json")
    cov = _read_json(j / "coverage.json")
    inj_a = _read_json(j / "injection_a.json")
    inj_b = _read_json(j / "injection_b.json")
    derived = score.derived_inputs(generations=gens, sample=sample, coverage=cov,
                                   injection_a=inj_a, injection_b=inj_b,
                                   system_template=P.SYSTEM_TEMPLATE)
    body = score.decide(
        summary_version=run["summary_version"], model_pin=run["model_pin"],
        provider_id=run["provider_id"],
        judge_a=packets.read_jsonl(j / "labels_a.jsonl"),
        judge_b=packets.read_jsonl(j / "labels_b.jsonl"), key=key,
        adjudications=_read_json(j / "adjudications.json"),
        coverage=cov, canary_leaks=int(run.get("canary_leaks", 1)),
        generations=gens, seed=sample["bootstrap_seed"], **derived)
    body.update({"sample_sha256": run["sample_sha256"], "references_sha256": ref_digest,
                 "summaries": sorted({g["window"] for g in gens}),
                 "measured_at": _dt.datetime.now(_dt.timezone.utc).isoformat()})
    written = score.write_record(args.out, body)
    print(json.dumps({k: written[k] for k in ("passed", "void", "content_sha256")}))
    return 0 if written["passed"] else 1


def _link_freeze(args) -> int:
    with _connect(args.source_dsn)() as conn:
        sample = linkgrade.sample_edges(conn, seed=args.seed)
    _write(args.out, sample)
    print(freeze.record(args.out.name, args.out))
    return 0


def _link_packets(args) -> int:
    sample = _read_json(args.sample)
    freeze.require_frozen(args.sample.name, args.sample)
    planted = _read_json(args.planted)
    with _connect(args.source_dsn)() as conn:
        items = linkgrade.pair_items(conn, sample, planted, seed=args.seed)
    args.out.mkdir(parents=True, exist_ok=True)
    packets.write_jsonl(args.out / "pairs.jsonl", items)
    (args.out / "rubric.txt").write_text(linkgrade.RUBRIC + "\n", encoding="utf-8")
    print(f"{len(items)} pairs -> {args.out}")
    return 0


def _link_score(args) -> int:
    sample = _read_json(args.sample)
    planted = {p["id"]: bool(p["related"]) for p in _read_json(args.planted)}
    out = linkgrade.score(sample, _read_json(args.labels_a), _read_json(args.labels_b),
                          planted)
    print(json.dumps(out, indent=2))
    return 0 if out["passed"] else 1


# ------------------------------------------------------------------------ the parser

def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m tools.summaries_eval")
    sub = ap.add_subparsers(dest="step", required=True)

    def source(p):
        p.add_argument("--source-dsn", required=True,
                       help="the node-test database, opened READ ONLY")

    def chat(p):
        p.add_argument("--chat-url", required=True, help="the chat provider, a LAN IP")
        p.add_argument("--provider-id", default=DEFAULT_PROVIDER_ID)
        p.add_argument("--model-pin", required=True)

    s = sub.add_parser("freeze-sample")
    source(s)
    s.add_argument("--seed", type=int, required=True)
    s.add_argument("--bootstrap-seed", type=int, required=True)
    s.add_argument("--out", type=Path, default=ROOT / "apps/meshsum/measure/sample_v1.json")
    s.set_defaults(handler=_freeze_sample)

    s = sub.add_parser("freeze", help="digest a frozen input into FROZEN.json")
    s.add_argument("--name", required=True)
    s.add_argument("--path", type=Path, required=True)
    s.set_defaults(handler=_freeze)

    s = sub.add_parser("reference-packets")
    source(s)
    chat(s)
    s.add_argument("--sample", type=Path, required=True)
    s.add_argument("--out", type=Path, default=DEFAULT_OUT / "references")
    s.set_defaults(handler=_reference_packets)

    s = sub.add_parser("generate")
    source(s)
    chat(s)
    s.add_argument("--sample", type=Path, required=True)
    s.add_argument("--references", type=Path, help="per window {asks: n}, frozen")
    s.add_argument("--out", type=Path, default=DEFAULT_OUT / "run")
    s.set_defaults(handler=_generate)

    s = sub.add_parser("judge-packets")
    s.add_argument("--run", type=Path, required=True)
    s.add_argument("--sample", type=Path, required=True)
    s.add_argument("--plants", type=Path, required=True,
                   help="the 40 validation plants [{window, sentence, cites, supported}]")
    s.add_argument("--seed", type=int, required=True)
    s.add_argument("--out", type=Path, default=DEFAULT_OUT / "judging")
    s.set_defaults(handler=_judge_packets)

    s = sub.add_parser("score")
    s.add_argument("--run", type=Path, required=True)
    s.add_argument("--sample", type=Path, required=True)
    s.add_argument("--judging", type=Path, required=True,
                   help="dir with key.json, labels_a.jsonl, labels_b.jsonl, "
                        "adjudications.json, coverage.json, injection_a.json, "
                        "injection_b.json")
    s.add_argument("--references", type=Path, required=True)
    s.add_argument("--out", type=Path, default=ROOT / "apps/meshsum/measure/record.json")
    s.set_defaults(handler=_score)

    s = sub.add_parser("link-freeze")
    source(s)
    s.add_argument("--seed", type=int, required=True)
    s.add_argument("--out", type=Path,
                   default=ROOT / "apps/meshsum/measure/linkgrade_sample_v1.json")
    s.set_defaults(handler=_link_freeze)

    s = sub.add_parser("link-packets")
    source(s)
    s.add_argument("--sample", type=Path, required=True)
    s.add_argument("--planted", type=Path, required=True,
                   help="10 synthetic pairs [{id, first, second, related}]")
    s.add_argument("--seed", type=int, required=True)
    s.add_argument("--out", type=Path, default=DEFAULT_OUT / "linkgrade")
    s.set_defaults(handler=_link_packets)

    s = sub.add_parser("link-score")
    s.add_argument("--sample", type=Path, required=True)
    s.add_argument("--planted", type=Path, required=True)
    s.add_argument("--labels-a", type=Path, required=True)
    s.add_argument("--labels-b", type=Path, required=True)
    s.set_defaults(handler=_link_score)
    return ap


def main(argv=None) -> int:
    # A missing or unknown step is argparse's refusal (usage, exit 2): the subparsers are
    # required, so every parsed `args` carries its step's handler.
    args = _parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
