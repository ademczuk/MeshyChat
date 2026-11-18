"""Generate the measured summaries: the server's own code, the frozen sample, in its order.

Design 7.1, 7.2, 7.5, 7.6 and F10. For each window in the sample's frozen order:

  * the rows by id from the source (READ ONLY), the server's per-row rules, the server's
    token-counted packing and prompt (apps/meshsum/window.pack and compose), and the
    call exactly as the route makes it (providers.chat pinned to the record's provider,
    every sampler setting, the window's JSON schema), then the validator and the half rule;
  * attacked windows twice over: WITHOUT the plant (the control) and WITH it; suppression
    windows and their controls three times each (7.5: decoding is not shown to repeat);
  * the EXTRACTIVE floor (7.6): each covered message's first sentence, newest first,
    until as long as the kept sentences, presented oldest first, `<author> wrote: "..."`.

It STOPS at the first window at which every minimum is met, reading only counts the
validator produced and counts fixed before the run (never a judge's label), so it cannot
select for a result. It refuses to start unless the sample is frozen and committed
(freeze.require_frozen). Outputs go to a directory OUTSIDE the repository (the judged
   material stays out of git, as the auditor A/B's did); `run.json` records the first
   generation time, which `score` compares with reference items' commit time (they must
   not be later).

Two windows are generated at once, so the in-flight cap is full as F10 requires, and each
call records whether it met a cold model (`/props` `is_sleeping` before the call).
"""
from __future__ import annotations

import concurrent.futures
import dataclasses
import datetime as _dt
import json
import re
import time
from pathlib import Path

from apps.mesh import normalize
from apps.meshsem import providers
from apps.meshsum import pin, prompt as P, validate as V

from . import corpus, freeze

CONCURRENCY = 2


@dataclasses.dataclass
class Counts:
    thread_windows: int = 0
    backlog_windows: int = 0
    kept_sentences: int = 0
    asks: int = 0
    requires_ack: int = 0

    def met(self, minimums: dict) -> bool:
        return all(getattr(self, k) >= v for k, v in minimums.items())


def extractive(rows: list, kept_chars: int) -> list[dict]:
    """The model-free floor: first sentences, newest first, until as long as Gemma's kept
    sentences, presented oldest first, each citing its own message."""
    picked = []
    total = 0
    for n in range(len(rows), 0, -1):
        if total >= kept_chars:
            break
        row = rows[n - 1]
        first = re.split(r"(?<=[.!?])\s+", row.body.strip(), maxsplit=1)[0]
        text = f'{row.author} wrote: "{first}"'
        picked.append({"text": text, "cites": [n]})
        total += len(text)
    return picked[::-1]


def _context(composed, peer_names: frozenset) -> V.Context:
    known = set(peer_names)
    for s in composed.sources:
        known |= set(s.aliases) | {x.lower() for x in s.to} | {x.lower() for x in s.cc}
    return V.Context(sources=composed.sources, known_labels=frozenset(known),
                     system_text=composed.messages[0]["content"], canary=composed.canary,
                     nonce=composed.nonce)


def summarize_rows(rows: list, kind: str, *, provider, model_pin: str,
                   peer_names: frozenset, thread_id: str | None) -> dict:
    """One summary of `rows`, by the server's code. No text of it is stored in git."""
    packed, composed = corpus.pack_and_compose(rows, kind, provider=provider,
                                               model_pin=model_pin, thread_id=thread_id,
                                               deadline_s=45)
    ctx = _context(composed, peer_names)
    try:
        props = providers.provider_info(provider, "/props", timeout=5)
        cold = bool(props.get("is_sleeping"))
    except providers.ProviderError:
        cold = None
    t0 = time.monotonic()
    reply = providers.chat(composed.messages, provider_ids={provider.id},
                           max_tokens=P.REQUEST["max_tokens"], params=P.REQUEST,
                           json_schema=P.json_schema(len(packed.rows)), accept_empty=True,
                           budget_s=45.0, timeout_s=40.0)
    latency = int((time.monotonic() - t0) * 1000)
    verdict = V.validate(reply.text, reply.finish_reason, ctx)
    if verdict.refused is None:
        verdict = V.settle(verdict, changed=set())
    kept = [{"text": s.text, "cites": list(s.cites)} for s in verdict.kept]
    kept_chars = sum(len(s["text"]) for s in kept)
    return {
        "kind": kind, "covered": [{"n": n, "key": r.key, "version": r.version,
                                   "author": r.author}
                                  for n, r in enumerate(packed.rows, start=1)],
        "covered_digest": composed.covered_digest,
        "messages_section": composed.messages[1]["content"],
        "canary": composed.canary,
        "refused": verdict.refused, "kept": kept,
        "dropped": [{"position": d.position, "reason": d.reason, "cites": list(d.cites)}
                    for d in verdict.dropped],
        "finish_reason": reply.finish_reason, "latency_ms": latency, "cold": cold,
        "usage": reply.usage, "model_reported": reply.model,
        "extractive": extractive(packed.rows, kept_chars),
        "requires_ack": sum(1 for r in packed.rows if normalize.requires_ack(r.value)),
    }


def owed_acks_listed(conn, peer_id: str, rows: list) -> dict:
    """F5's mechanical half: every ask in the window that sets `requires_ack` and names
    the owner in `to` (an owes_ack edge, apps/mesh/normalize.py), and how many of them the
    route's `facts` would list: graph.owed_acks asked FOR those keys, as facts.for_backlog
    asks it, under the labels the route answers the owner for."""
    from apps.mesh import graph
    from apps.mesh import views as mesh_views

    expected = [r.key for r in rows if r.role == "to" and normalize.requires_ack(r.value)]
    if not expected:
        return {"owes_expected": 0, "owes_listed": 0}
    labels = [x["label"] for x in mesh_views.labels_for(conn, peer_id)]
    listed = {o.hit.key for o in graph.owed_acks(conn, labels, keys=expected)}
    return {"owes_expected": len(expected), "owes_listed": len(listed & set(expected))}


def _peer_names(conn) -> frozenset:
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM peers WHERE name IS NOT NULL LIMIT 10000")
        return frozenset(r[0].strip().lower() for r in cur.fetchall() if r[0])


def run(connect, sample_path: Path, out_dir: Path, *, provider, model_pin: str,
        references: dict | None = None, repo: Path = freeze.ROOT) -> dict:
    """Generate in the frozen order until every minimum is met. Returns the run's
    summary (also written to `out_dir/run.json`). `connect()` opens a READ ONLY connection
    to the source; each worker opens its own (a connection is not shared by threads).
    `references`: per window, the ask count recorded by reference items (annotated before any
    summary exists, 7.1)."""
    sample_digest = freeze.require_frozen("sample_v1.json", sample_path, repo=repo)
    sample = json.loads(Path(sample_path).read_text(encoding="utf-8"))
    live = pin.read_pin(provider)
    if live.digest != model_pin:
        raise RuntimeError(f"the provider reports pin {live.digest}, not {model_pin}")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    by_id = {w["id"]: w for w in sample["windows"]}
    plants_by_window: dict = {}
    for p in sample["plants"] + sample["denial"]:
        plants_by_window.setdefault(p["window"], []).append(p)
    with connect() as conn:
        names = _peer_names(conn)
    counts = Counts()
    first_at = None
    done: list[str] = []

    def one(wid: str) -> list[dict]:
        w = by_id[wid]
        owes = {}
        with connect() as conn:
            rows = corpus.load_rows(conn, w["row_ids"], roles=w.get("roles"))
            if w["kind"] == "backlog":
                owes = owed_acks_listed(conn, w["peer_id"], rows)
        tid = w.get("thread_id")
        results = []
        plants = plants_by_window.get(wid, [])
        runs = max([p.get("runs", 1) for p in plants] or [1])
        for r in range(runs):
            results.append({"window": wid, "variant": "control", "run": r + 1, **owes,
                            **summarize_rows(rows, w["kind"], provider=provider,
                                             model_pin=model_pin, peer_names=names,
                                             thread_id=tid)})
        for p in plants:
            planted = rows + [corpus.plant_row(p, rows[-1].created_at)]
            for r in range(p.get("runs", 1)):
                results.append({"window": wid, "variant": p["id"], "run": r + 1,
                                **summarize_rows(planted, w["kind"], provider=provider,
                                                 model_pin=model_pin, peer_names=names,
                                                 thread_id=tid)})
        return results

    order = list(sample["order"])
    with concurrent.futures.ThreadPoolExecutor(CONCURRENCY) as pool:
        i = 0
        while i < len(order) and not counts.met(sample["minimums"]):
            batch = order[i:i + CONCURRENCY]
            i += len(batch)
            if first_at is None:
                first_at = _dt.datetime.now(_dt.timezone.utc)
            for wid, results in zip(batch, pool.map(one, batch)):
                (out_dir / f"{wid}.json").write_text(json.dumps(results, indent=1),
                                                     encoding="utf-8")
                done.append(wid)
                control = results[0]
                kind = by_id[wid]["kind"]
                counts.thread_windows += kind == "thread"
                counts.backlog_windows += kind == "backlog"
                counts.kept_sentences += len(control["kept"])
                counts.requires_ack += control["requires_ack"] if kind == "backlog" else 0
                if references:
                    counts.asks += int(references.get(wid, {}).get("asks", 0))
    summary = {"sample_sha256": sample_digest, "model_pin": model_pin,
               "provider_id": provider.id, "summary_version": P.SUMMARY_VERSION,
               "first_generated_at": first_at.isoformat() if first_at else None,
               "windows": done, "counts": dataclasses.asdict(counts),
               "minimums_met": counts.met(sample["minimums"])}
    (out_dir / "run.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


__all__ = ["CONCURRENCY", "Counts", "extractive", "run", "summarize_rows"]
