"""Calibrate the related_to threshold against the LAN's REAL models, on the SYNTHETIC
fixture. Divergence 032. Run by hand only:

    python -m apps.meshsem.related_cli calibrate [--write-calibration] [--typing] \
        [--embed URL] [--rerank URL] [--chat URL] [--out report.json] [--keep-db]

(or `python -m tools.related_calibration ...`, the same thing).

WHAT IT DOES, in order:
  1. creates a THROWAWAY database on the dev server (never `meshy_nodetest`, refused by
     name) and applies every migration set into it (apps/core/schema_sets.apply_all);
  2. seeds the fixture's 96 synthetic messages as head rows of shared/mesh-chat with
     embed_policy 'allowed' (their text is invented; nothing read from the mesh is sent to
     any model), with their threads and reply pointers;
  3. runs the REAL mesh_edges catch-up and the REAL embed job against the LAN embedder;
  4. records the reranker's canary REFERENCE logits (and repeats them, reporting the
     largest difference), then, for every message, builds the job's own pool
     (apps/meshsem/related.build_pool) and scores it with the job's own scorer
     (related.score_source), reverse-scoring the WHOLE pool so the job's reverse depth
     can be measured against scoring everything (risk R3);
  5. offline: the selection rule (relatedcal.decide: development split, bootstrap,
     held-out ship gate against the lexical floor), plus the report's comparisons (min and
     forward-only aggregators, reverse depth, hubs, length buckets);
  6. with --typing: gemma relation typing (thinking off) judged by
     relatedcal.typing_verdict against the majority and heuristic floors. The GATE is
     over the held-out pairs the edge rule wrote; the same verdict over every labelled
     held-out pair is reported beside it and decides nothing. OFF unless the gate passes;
  7. writes the report (--out) and, with --write-calibration, the calibration document
     apps/meshsem/calibration/related_calibration.json; drops the database.

WHY THIS FILE IS UNDER tools/ AND NOT apps/: it opens the throwaway database as the
bootstrap owner (to create it, migrate it and seed it), which no apps/ module may do
(tests/test_every_connection_is_role_bound.py). The job functions it calls run on a
dbconn.connect() connection, as the app role, exactly as the registered job does.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

import psycopg  # noqa: E402
from django.test import override_settings  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402

from apps.core import dbconn, rls, schema_sets  # noqa: E402
from apps.crypto import primitives as P  # noqa: E402
from apps.mesh import projection as mesh_projection  # noqa: E402
from apps.memoryskills.validation import MESH_CHAT_NAMESPACE as NS  # noqa: E402
from apps.meshsem import (embedjob, providers, related, relatedcal, search,  # noqa: E402
                          spaces, store)

ADMIN_DSN = "postgresql://meshychat_owner:change-me@127.0.0.1:5434/postgres"
REFUSED_DATABASES = {"meshy_nodetest", "meshy_django", "meshy_template", "postgres"}
EMBED_URL = "http://192.168.50.10:7875"
RERANK_URL = "http://192.168.50.10:7876"
CHAT_URL = "http://192.168.50.10:7872"


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------- the database

def _create_db(admin_dsn: str) -> tuple[str, str]:
    name = f"meshy_relcal_{os.getpid()}_{uuid.uuid4().hex[:6]}"
    if name in REFUSED_DATABASES:            # pragma: no cover - the prefix makes it so
        raise SystemExit(f"refusing to use database {name}")
    with psycopg.connect(admin_dsn, autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}"')
    dsn = admin_dsn.rsplit("/", 1)[0] + "/" + name
    with psycopg.connect(dsn) as conn:
        schema_sets.apply_all(conn, verbose=False)
        schema_sets.assert_schema_complete(conn)
    return name, dsn


def _drop_db(admin_dsn: str, name: str) -> None:
    if name in REFUSED_DATABASES or not name.startswith("meshy_relcal_"):
        raise SystemExit(f"refusing to drop database {name}")
    with psycopg.connect(admin_dsn, autocommit=True) as c:
        c.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                  "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
        c.execute(f'DROP DATABASE IF EXISTS "{name}"')


def _seed(dsn: str, fx: dict) -> dict[str, dict]:
    """The fixture as head rows. Returns fixture id -> {id, key, created_at}."""
    epoch = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0) - _dt.timedelta(days=7)
    out: dict[str, dict] = {}
    with psycopg.connect(dsn, autocommit=True) as c, c.cursor() as cur:
        peers = {}
        for author in sorted({m["author"] for m in fx["messages"]} | {"nsowner"}):
            owner_id, peer_id = uuid.uuid4(), uuid.uuid4()
            cur.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id) "
                        "VALUES (%s, %s, 'dev', %s)",
                        (owner_id, f"relcal-{author}@example.test", uuid.uuid4()))
            cur.execute("INSERT INTO peers (id, owner_id, name, display_name) "
                        "VALUES (%s, %s, %s, %s)", (peer_id, owner_id, author, author))
            peers[author] = (peer_id, owner_id)
        cur.execute("INSERT INTO shared_memory_namespace (namespace, owner_peer_id, "
                    "owner_owner_id, claimant_plane, ephemeral, retention_policy) "
                    "VALUES (%s, %s, %s, 'dev', FALSE, 'keep_all')",
                    (NS, *peers["nsowner"]))
        for m in sorted(fx["messages"], key=lambda m: m["t"]):
            rid = uuid.uuid4()
            when = epoch + _dt.timedelta(minutes=m["t"])
            key = f"msg/{int(when.timestamp() * 1000)}-{rid.hex[:8]}"
            value = {"schema_version": 1, "body": m["text"], "thread_id": m["thread"],
                     "to": ["mesh"]}
            if m.get("reply_to"):
                value["in_reply_to"] = out[m["reply_to"]]["key"]
            pid, oid = peers[m["author"]]
            # WITH THE BYTES A WRITER WOULD HAVE SIGNED (divergence 051). Since
            # sql_meshychat/025 every INSERT into shared_memory must carry signed_bytes,
            # and 014's agree-trigger holds the row to them: the payload's caller is the
            # row's peer, its args name the namespace, key and value, and the hash is the
            # hash of the bytes. Nothing is signed (the database cannot verify a
            # signature); the bytes are the canonical form of this synthetic payload.
            payload = {"v": 1, "kind": "skill.call", "call_id": f"call-{rid}",
                       "caller_peer_id": str(pid), "target_peer_id": str(pid),
                       "skill_name": "memory.write", "skill_version": "1",
                       "args": {"namespace": NS, "key": key, "value": value},
                       "args_attachments": [],
                       "issued_at": when.isoformat().replace("+00:00", "Z")}
            raw = P.jcs(payload)
            cur.execute("INSERT INTO shared_memory (id, namespace, key, version, is_head, "
                        "value, provenance_jsonb, payload_jcs_sha256, signed_bytes, "
                        "created_by_peer_id, created_by_owner_id, author_label, created_at, "
                        "embed_policy) "
                        "VALUES (%s, %s, %s, 1, TRUE, %s, %s, %s, %s, %s, %s, %s, %s, 'allowed')",
                        (rid, NS, key, Jsonb(value), Jsonb({"payload": payload}),
                         hashlib.sha256(raw).hexdigest(), raw, pid, oid, m["author"], when))
            out[m["id"]] = {"id": str(rid), "key": key, "created_at": when}
    return out


# ------------------------------------------------------------------------- scoring

def _drain(conn) -> None:
    for _ in range(100):
        if not mesh_projection.catch_up(conn).more:
            break
    for _ in range(100):
        r = embedjob.catch_up(conn, deadline_s=120.0)
        if r.stopped == "provider_unavailable":
            raise SystemExit(f"embedder unavailable: {r.attempts}")
        if not r.more or r.rows_processed == 0:
            break


def _canaries(pinned: str, repeats: int = 2) -> tuple[list[list[float]], float]:
    refs, worst = [], 0.0
    for q, docs in relatedcal.CANARY_SETS:
        runs = []
        for _ in range(repeats):
            got = providers.rerank(q, list(docs), provider_ids={pinned}, budget_s=30.0)
            if not got.ran:
                raise SystemExit(f"reranker unavailable for the canaries: {got.attempts}")
            runs.append(got.scores)
        refs.append(runs[0])
        for r in runs[1:]:
            worst = max([worst] + [abs(a - b) for a, b in zip(r, runs[0])])
    return refs, worst


def _score_all(conn, space, ids: dict[str, dict], pinned: str) -> tuple[dict, dict]:
    """fixture id -> [candidate dicts] (the job's pool, fully reverse-scored), and timing."""
    mid_of = {v["id"]: k for k, v in ids.items()}
    idx, lex = search.index_for(space), search.lexical_index_for(space)
    with rls.server_internal(conn) as c:
        idx.refresh(c, ceiling=search._ceiling())
        lex.refresh(c, ceiling=search._ceiling())
        emb = store.read_state(c.cursor(), space)
        with c.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute("SELECT watermark_at, watermark_id FROM mesh_edges_projection "
                        "WHERE projection = 'mesh_edges'")
            ew = cur.fetchone()
            up = min((emb["watermark_at"], emb["watermark_id"]),
                     (ew["watermark_at"], ew["watermark_id"]))
            cur.execute(related._SELECT_ROWS, {"space": space.name, "ns": NS,
                                               "after_at": None, "after_id": None,
                                               "up_at": up[0], "up_id": up[1],
                                               "limit": 10000})
            rows = cur.fetchall()
        pools = {str(r["id"]): (r, related.build_pool(c, space, r, gate=False, idx=idx,
                                                      lex=lex)) for r in rows}
    scored, fwd_ms, rev_ms = {}, [], []
    for n, (sid, (r, pool)) in enumerate(pools.items(), start=1):
        text = embedjob.document_text(r["body"])
        if not pool:
            scored[mid_of[sid]] = []
            continue
        t0 = time.monotonic()
        # No `confirm`: here a refused pair fails the run, it is never left out.
        cands, _refused = related.score_source(text, pool, pinned=pinned,
                                               reverse_depth=len(pool), budget_s=300.0)
        dt = time.monotonic() - t0
        # One forward call and len(pool) reverse calls: attribute the time roughly.
        fwd_ms.append(dt * 1000.0)
        scored[mid_of[sid]] = [{"dst": mid_of[c["candidate"].src_id], "fwd": c["fwd"],
                                "rev": c["rev"], "fused_rank": c["fused_rank"],
                                "cosine": c["candidate"].cosine,
                                "lexical": c["candidate"].lexical_score}
                               for c in cands]
        if n % 16 == 0:
            _log(f"  scored {n}/{len(pools)} sources")
    timing = {"sources": len(pools), "mean_pool": (sum(len(p) for _r, p in pools.values())
                                                   / max(1, len(pools))),
              "per_source_full_reverse_ms_median": sorted(fwd_ms)[len(fwd_ms) // 2]
              if fwd_ms else None}
    return scored, timing


# -------------------------------------------------------------------------- typing

def _typing(fx: dict, written: list[tuple[str, str]], chat_id: str) -> dict:
    """Gemma relation typing, MEASURED, and off unless it clears the rule fixed in advance
    (relatedcal.typing_verdict: the incident-bootstrap lower bound over the best floor,
    the invalid-answer bar, the contradicts-precision bar).

    The GATE runs over the held-out pairs the edge rule WROTE, because typing would label
    an existing edge and never make one. REPORTED beside it, never deciding: the same
    verdict over EVERY labelled held-out pair, a larger sample of the same task. Each pair
    is asked once; the texts are the fixture's synthetic ones."""
    msgs = {m["id"]: m for m in fx["messages"]}
    rel = relatedcal.relations(fx)
    held = set(relatedcal.anchors_in(fx, "held_out"))
    everything = sorted(pair for pair in rel if pair[0] in held)
    answers: dict[tuple[str, str], tuple[str | None, str]] = {}
    t0 = time.monotonic()
    for a, d in everything:
        try:
            reply = providers.chat(
                relatedcal.typing_messages(msgs[a]["text"], msgs[d]["text"]),
                max_tokens=512, temperature=0.0, budget_s=60.0,
                template_kwargs={"enable_thinking": False})
            label = (None if reply.finish_reason == "length"
                     else relatedcal.parse_typing(reply.text))
            answers[(a, d)] = (label, str(reply.finish_reason))
        except providers.ChatUnavailable as exc:
            answers[(a, d)] = (None, f"unavailable: {exc}")

    def verdict(items: list[tuple[str, str]]) -> dict:
        out = relatedcal.typing_verdict(
            [rel[x] for x in items], [answers[x][0] for x in items],
            [relatedcal.heuristic_relation(msgs[a]["author"], msgs[d]["author"],
                                           msgs[d]["text"]) for a, d in items],
            [msgs[a]["incident"] for a, _d in items])
        finish = [answers[x][1] for x in items]
        out["finish_reasons"] = {k: finish.count(k) for k in sorted(set(finish))}
        return out

    gated = [pair for pair in written if pair in rel]
    report = verdict(gated)
    report.update({"seconds": round(time.monotonic() - t0, 1), "provider": chat_id,
                   "gate_over": "held-out pairs the edge rule wrote",
                   "all_labelled_held_out": verdict(everything)})
    return report


# ------------------------------------------------------------------------------ main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fixture", type=Path, default=relatedcal.FIXTURE_PATH)
    ap.add_argument("--admin-dsn", default=ADMIN_DSN)
    ap.add_argument("--embed", default=EMBED_URL)
    ap.add_argument("--rerank", default=RERANK_URL)
    ap.add_argument("--chat", default=CHAT_URL)
    ap.add_argument("--space", default=spaces.DEFAULT_SPACE)
    ap.add_argument("--out", type=Path, default=None, help="write the full report here")
    ap.add_argument("--scores-out", type=Path, default=None,
                    help="write the raw per-pair scores here (synthetic fixture only)")
    ap.add_argument("--write-calibration", action="store_true")
    ap.add_argument("--typing", action="store_true")
    ap.add_argument("--keep-db", action="store_true")
    a = ap.parse_args(argv)

    fx = relatedcal.load_fixture(a.fixture)
    frozen = relatedcal.LABELS_PATH.read_text(encoding="utf-8").strip()
    if relatedcal.labels_digest(fx) != frozen:
        raise SystemExit("the fixture's labels do not match the frozen digest; refusing")
    space = spaces.get(a.space)
    provs = [{"id": "embed-hqb", "kind": "embed", "base_url": a.embed,
              "model": "embeddinggemma-300m", "space": space.name},
             {"id": "rerank-hqb", "kind": "rerank", "base_url": a.rerank,
              "model": "bge-reranker-v2-m3"},
             {"id": "chat-hqb", "kind": "chat", "base_url": a.chat,
              "model": "gemma-4-26b-a4b"}]
    name, dsn = _create_db(a.admin_dsn)
    _log(f"throwaway database {name}")
    try:
        ids = _seed(dsn, fx)
        with override_settings(MESHY_MODEL_PROVIDERS=provs, MESHY_SEMANTIC_SPACE=space.name,
                               MESHY_EMBED_FORBIDDEN_ROWS=False, MESHY_DSN=dsn):
            search.forget()
            conn = dbconn.connect(dsn)
            try:
                _drain(conn)
                refs, repeat_delta = _canaries("rerank-hqb")
                _log(f"canaries recorded; repeat max |delta| {repeat_delta:.6f}")
                t0 = time.monotonic()
                scored, timing = _score_all(conn, space, ids, "rerank-hqb")
                timing["scoring_seconds"] = round(time.monotonic() - t0, 1)
                _log(f"scored {len(scored)} sources in {timing['scoring_seconds']} s")
                report = _analyse(fx, scored, refs)
                report["timing"] = timing
                report["canary_repeat_max_delta"] = repeat_delta
                if a.typing:
                    held = report["held_out_edges"]
                    _log(f"typing {len(held)} held-out written pair(s) with gemma")
                    report["typing"] = _typing(fx, [tuple(x) for x in held], "chat-hqb")
            finally:
                conn.close()
                search.forget()
    finally:
        if a.keep_db:
            _log(f"kept {name}")
        else:
            _drop_db(a.admin_dsn, name)
            _log(f"dropped {name}")

    decision = report["decision"]
    doc = relatedcal.build(space=space.name, threshold=decision["threshold"],
                           k=decision["k"], canary_references=refs,
                           rerank_model="bge-reranker-v2-m3",
                           reverse_depth=relatedcal.DEFAULT_REVERSE_DEPTH,
                           enabled=decision["enabled"], status=decision["status"],
                           report={k: v for k, v in report.items()
                                   if k not in ("held_out_edges",)})
    if a.scores_out:
        a.scores_out.write_text(json.dumps(scored, indent=1), encoding="utf-8")
    if a.out:
        a.out.write_text(json.dumps({"calibration": doc, "report": report}, indent=1,
                                    default=str), encoding="utf-8")
        _log(f"report written to {a.out}")
    if a.write_calibration:
        relatedcal.CALIBRATION_PATH.write_text(json.dumps(doc, indent=1, default=str) + "\n",
                                               encoding="utf-8", newline="\n")
        _log(f"calibration written to {relatedcal.CALIBRATION_PATH}")
    print(json.dumps({"decision": decision, "threshold_version": doc["threshold_version"],
                      "dev": _brief(report["dev"]), "held_out": _brief(report["held_out"]),
                      "lexical_floor": _brief(report["lexical_floor"]),
                      "bootstrap": report["bootstrap"],
                      "typing": report.get("typing", {}).get("enabled")}, indent=1))
    return 0


def _brief(m: dict) -> dict:
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in m.items()
            if k in ("threshold", "k", "written", "correct_ge1", "correct_g2",
                     "precision_ge1", "precision_ge1_lb", "recall_g2", "eligible_g2",
                     "zero_overlap_recall", "max_in_degree")}


def _analyse(fx: dict, scored: dict, refs) -> dict:
    """The selection rule (relatedcal.decide), and the report around it."""
    dev = relatedcal.anchors_in(fx, "dev")
    held = relatedcal.anchors_in(fx, "held_out")
    depth = relatedcal.DEFAULT_REVERSE_DEPTH
    out = relatedcal.decide(scored, fx, reverse_depth=depth)
    report: dict = {"split": out["split"],
                    "selection_rule": {"grid": [relatedcal.GRID[0], relatedcal.GRID[-1], 0.5],
                                       "min_lb_dev": relatedcal.MIN_LB_DEV,
                                       "k_for_threshold": relatedcal.K_MAX,
                                       "k_recall_keep": relatedcal.K_RECALL_KEEP,
                                       "min_lb_held_out": relatedcal.MIN_LB_HELD_OUT,
                                       "bootstrap_max_width": relatedcal.BOOTSTRAP_MAX_WIDTH,
                                       "reverse_depth": depth}}
    # A threshold sweep on dev, for the record.
    report["dev_sweep"] = [
        {"threshold": g, **_brief(relatedcal.evaluate(scored, fx, dev, threshold=g,
                                                      k=relatedcal.K_MAX,
                                                      reverse_depth=depth))}
        for g in relatedcal.GRID if -6.0 <= g <= 6.0]
    report["bootstrap"] = out["bootstrap"]
    for part in ("dev", "held_out", "lexical_floor", "decision", "held_out_edges"):
        report[part] = out[part]                # held_out_edges: the pairs typing labels
    if report["decision"]["status"] == "no_threshold_qualified":
        return report
    t, k, ho = report["decision"]["threshold"], report["decision"]["k"], out["held_out"]
    # Comparisons: reported, never shipped.
    report["aggregators_at_selected_threshold"] = {
        agg: _brief(relatedcal.evaluate(scored, fx, held, threshold=t, k=k, aggregator=agg,
                                        reverse_depth=depth))
        for agg in relatedcal.AGGREGATORS}
    report["aggregators_own_threshold_dev"] = {
        agg: relatedcal.select_threshold(scored, fx, dev, aggregator=agg,
                                         reverse_depth=depth)
        for agg in relatedcal.AGGREGATORS}
    report["reverse_depth_vs_full"] = {
        "depth": depth,
        "held_out_depth": _brief(ho),
        "held_out_full": _brief(relatedcal.evaluate(scored, fx, held, threshold=t, k=k))}
    everyone = sorted(scored)
    allm = relatedcal.evaluate(scored, fx, everyone, threshold=t, k=k, reverse_depth=depth)
    report["all_sources"] = {"sources": len(everyone),
                             "with_edges": sum(1 for v in allm["out_degree"].values() if v),
                             "edges": allm["written"],
                             "max_in_degree": allm["max_in_degree"]}
    report["held_out_by_length"] = ho["by_length"]
    return report


if __name__ == "__main__":
    raise SystemExit(main())
