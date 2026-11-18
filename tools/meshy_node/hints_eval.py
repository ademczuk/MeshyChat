"""Offline and live read-only calibration harness for pre-send semantic collision hints (zero prose output)."""
from __future__ import annotations
import argparse, csv, io, json, math, re, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from apps.meshsem.hint_rules import (
    ANCHOR_BONUS, DEFAULT_CARD_MIN_SCORE, DEFAULT_MESH_MIN_SCORE, DEFAULT_MIN_DIRECTION_SCORE as DEFAULT_MIN_DIR_SCORE,
    FRESH_DAYS, MIN_CANDIDATE_CHARS, MIN_DRAFT_CHARS, RERANK_CHUNK, RERANK_TEXT_CHARS, REVERSE_DEPTH,
    extract_technical_anchors, is_non_actionable_receipt, norm_labels, normalize_rerank_text, seat_stem)
from apps.modelhost import locality
from . import cards, node

_KEY_TS_RE = re.compile(r"^msg/(\d+)-")

class ContentRefused(RuntimeError):
    """MUST-8: reranker destination may not receive mesh content."""

class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def _content_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _RefuseRedirects())

_OPENER = _content_opener()

def require_rerank_url_ok(rerank_url: str) -> str:
    where = locality.classify(rerank_url)
    if not where.content_ok:
        raise ContentRefused(f"--rerank-url {rerank_url!r} refused for mesh content (MUST-8): {where.reason}")
    parts = urllib.parse.urlsplit(rerank_url.strip())
    if parts.query or parts.fragment:
        raise ContentRefused(f"--rerank-url {rerank_url!r} refused for mesh content (MUST-8): query or fragment not allowed")
    url = rerank_url.strip().rstrip("/")
    return url if url.endswith("/rerank") else f"{url}/rerank"

def _is_fresh_mesh_hit(mh: dict[str, Any], *, oldest: datetime) -> bool:
    raw = mh.get("recorded_at") if mh.get("recorded_at") is not None else mh.get("created_at")
    if not isinstance(raw, str) or not raw.strip(): return False
    try: ts = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except (ValueError, OverflowError): return False
    return False if ts.tzinfo is None or ts.utcoffset() is None else ts.astimezone(timezone.utc) >= oldest

def wilson_95(successes: int, total: int) -> tuple[float, float]:
    if total <= 0: return (0.0, 0.0)
    z, p = 1.959963984540054, successes / total
    denom = 1.0 + (z * z) / total
    center, half = (p + (z * z) / (2.0 * total)) / denom, (z * math.sqrt((p * (1.0 - p) + (z * z) / (4.0 * total)) / total)) / denom
    return (round(max(0.0, center - half), 4), round(min(1.0, center + half), 4))

def _retry_call(fn: Callable[[], Any], *, attempts: int = 3) -> Any:
    last_exc: Exception | None = None
    for i in range(attempts):
        try: return fn()
        except (ContentRefused, node.Refused): raise
        except node.ServerSaid as exc:
            if exc.status < 500 and exc.status != 429: raise
            last_exc = exc
        except urllib.error.HTTPError as exc:
            if exc.code == 500 or (exc.code < 500 and exc.code != 429): raise
            last_exc = exc
        except Exception as exc: last_exc = exc  # noqa: BLE001
        if i + 1 < attempts: time.sleep(0.4 * (i + 1))
    assert last_exc is not None
    raise last_exc

def _rerank_chunk(url: str, query: str, docs: list[str], *, timeout: float) -> list[float]:
    raw_req = json.dumps({"model": "rerank", "query": query[:RERANK_TEXT_CHARS],
                          "documents": [d[:RERANK_TEXT_CHARS] for d in docs], "top_n": len(docs)}).encode("utf-8")
    def _do() -> list[float]:
        req = urllib.request.Request(url, data=raw_req, headers={"Content-Type": "application/json"}, method="POST")
        with _OPENER.open(req, timeout=max(0.1, timeout)) as resp:
            parsed = json.loads(resp.read().decode("utf-8"))
        scores = [0.0] * len(docs)
        for item in parsed["results"]: scores[int(item["index"])] = float(item["relevance_score"])
        return scores
    return _retry_call(_do)

def http_rerank(rerank_url: str, query: str, docs: list[str], *, timeout: float = 20.0) -> list[float]:
    url = require_rerank_url_ok(rerank_url)
    if not docs: return []
    scores_out: list[float] = []
    for i in range(0, len(docs), RERANK_CHUNK):
        chunk = docs[i:i + RERANK_CHUNK]
        try: scores_out.extend(_rerank_chunk(url, query, chunk, timeout=timeout))
        except urllib.error.HTTPError as exc:
            if exc.code == 500 and len(chunk) > 1:
                for doc in chunk: scores_out.extend(_rerank_chunk(url, query, [doc], timeout=timeout))
            else: raise
    return scores_out

def extract_graded_csv(text: str) -> list[dict[str, Any]]:
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.strip() == "send_key,hint_key,grade")
    clean = ["send_key,hint_key,grade", *[ln.strip() for ln in lines[start + 1:] if ln.strip().startswith("msg/") and ln.strip().count(",") == 2]]
    return [{"send_key": r["send_key"], "hint_key": r["hint_key"], "grade": int(r["grade"])} for r in csv.DictReader(io.StringIO("\n".join(clean)))]

def evaluate_graded_pairs(
    pairs: list[dict[str, Any]], messages_by_key: dict[str, dict[str, Any]],
    rerank_fn: Callable[[str, list[str]], list[float]], *,
    thresholds: tuple[float, ...] = (1.0, 1.15, 1.3, 1.5, 2.0, 3.0), min_dir: float = DEFAULT_MIN_DIR_SCORE,
) -> dict[str, Any]:
    total, raw_useful = len(pairs), sum(1 for p in pairs if int(p["grade"]) >= 1)
    indexed = sorted(((idx, p, int(m.group(1)) if (m := _KEY_TS_RE.match(p["send_key"] or "")) else 0)
                      for idx, p in enumerate(pairs)), key=lambda x: (x[2], x[0]))
    scored_rows, pre_drops = [], {"ack_or_short": 0, "same_thread_or_reply": 0, "same_author_or_addressed": 0}
    for idx, p, ts_ms in indexed:
        sk, hk, grade = p["send_key"], p["hint_key"], int(p["grade"])
        sd, hd = messages_by_key[sk], messages_by_key[hk]
        s_body, h_body = (sd.get("body") or "").strip(), (hd.get("body") or "").strip()
        s_th, h_th = str(sd.get("thread_id") or "").strip(), str(hd.get("thread_id") or "").strip()
        s_irt, h_irt = str(sd.get("in_reply_to") or "").strip(), str(hd.get("in_reply_to") or "").strip()
        if ((s_th and h_th and s_th == h_th) or (s_irt and hk == s_irt) or (h_irt and sk == h_irt)
                or hk in set(sd.get("closes") or ()) or hk in s_body):
            pre_drops["same_thread_or_reply"] += 1; continue
        s_labels, h_labels = set(norm_labels([sd.get("author"), sd.get("from")])), set(norm_labels([hd.get("author"), hd.get("from")]))
        s_stems, h_stems = {seat_stem(x) for x in s_labels if seat_stem(x)}, {seat_stem(x) for x in h_labels if seat_stem(x)}
        s_rcpt, h_rcpt = set(norm_labels([sd.get("to"), sd.get("cc")])), set(norm_labels([hd.get("to"), hd.get("cc")]))
        s_rcpt_stems, h_rcpt_stems = {seat_stem(x) for x in s_rcpt if seat_stem(x)}, {seat_stem(x) for x in h_rcpt if seat_stem(x)}
        if ((sd.get("signed_by") and sd.get("signed_by") == hd.get("signed_by"))
                or bool(h_labels & (s_labels | s_rcpt)) or bool(h_stems & (s_stems | s_rcpt_stems))
                or bool(h_rcpt & s_labels) or bool(h_rcpt_stems & s_stems)):
            pre_drops["same_author_or_addressed"] += 1; continue
        if (str(sd.get("type") or "").lower() == "ack" or str(hd.get("type") or "").lower() == "ack"
                or len(s_body) < MIN_DRAFT_CHARS or len(h_body) < MIN_CANDIDATE_CHARS
                or is_non_actionable_receipt(s_body) or is_non_actionable_receipt(h_body)):
            pre_drops["ack_or_short"] += 1; continue
        ns, nh = normalize_rerank_text(s_body), normalize_rerank_text(h_body)
        fwd, rev = rerank_fn(ns, [nh])[0], rerank_fn(nh, [ns])[0]
        excl = s_stems | h_stems | s_rcpt_stems | h_rcpt_stems
        shared = bool(extract_technical_anchors(s_body, exclude_stems=excl) & extract_technical_anchors(h_body, exclude_stems=excl))
        eff = 0.5 * (fwd + rev) + (ANCHOR_BONUS if shared else 0.0)
        utc_day = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d") if ts_ms else "unknown"
        sender_id = next(iter(sorted(s_stems)), next(iter(sorted(s_labels)), "unknown"))
        scored_rows.append({"idx": idx, "send_key": sk, "hint_key": hk, "grade": grade, "sender_id": sender_id, "utc_day": utc_day,
                            "forward_score": round(fwd, 4), "min_dir": round(min(fwd, rev), 4), "effective_score": round(eff, 4)})

    def _eval_at(thr: float, score_key: str, apply_dir: bool) -> dict[str, Any]:
        ledger, daily_dropped, below, kept = set(), 0, 0, []
        for row in scored_rows:
            cap_key = (row["sender_id"], row["hint_key"], row["utc_day"])
            if cap_key in ledger: daily_dropped += 1
            elif (apply_dir and row["min_dir"] < min_dir) or row[score_key] < thr: below += 1
            else: ledger.add(cap_key); kept.append(row)
        useful = sum(1 for r in kept if r["grade"] >= 1)
        return {"threshold": thr, "shown": len(kept), "useful": useful, "precision": round(useful / len(kept), 4) if kept else None,
                "wilson_95": list(wilson_95(useful, len(kept))), "daily_cap_dropped": daily_dropped, "below_threshold": below}

    precut = _eval_at(-math.inf, "forward_score", False)
    return {
        "total_pairs": total,
        "raw_baseline": {"shown": total, "useful": raw_useful, "precision": round(raw_useful / total, 4) if total else 0.0, "wilson_95": list(wilson_95(raw_useful, total))},
        "precut_drops": {**pre_drops, "daily_cap": precut["daily_cap_dropped"]},
        "after_precut_only": {k: precut[k] for k in ("shown", "useful", "precision", "wilson_95")},
        "forward_sweep": [_eval_at(t, "forward_score", False) for t in thresholds],
        "calibrated_sweep": [_eval_at(t, "effective_score", True) for t in thresholds],
    }

DEFAULT_BENCHMARK_DRAFTS: tuple[dict[str, Any], ...] = (
    {"id": "P01", "positive": True, "sender": "antigravity", "thread_id": "eval/p01", "to": ["vxxxxxxxxxx"], "msg_type": "note", "match_tokens": ("directive-check", "directive-done", "seat_directive"), "body": "When reading a message carrying seat_directive, run meshy_node directive-check --key K once to claim it and directive-done with outcome done or refused afterwards."},
    {"id": "P02", "positive": True, "sender": "antigravity", "thread_id": "eval/p02", "to": ["vxxxxxxxxxx"], "msg_type": "question", "match_tokens": ("hold_cc_without_ack", "bank", "wait_next", "requires_ack"), "body": "Under hold_cc_without_ack in postbox wait_next, incoming cc messages with requires_ack=false are banked into wait-session.offset.bank.json without waking the session."},
    {"id": "P03", "positive": True, "sender": "antigravity", "thread_id": "eval/p03", "to": ["vxxxxxxxxxx"], "msg_type": "note", "match_tokens": ("meshy_ctx_index_budget_bytes", "shardcache", "ctxindex", "budget"), "body": "MESHY_CTX_INDEX_BUDGET_BYTES caps ShardCache resident vectors at 1 GiB on HQD so ctxindex evicts shards instead of paging Postgres."},
    {"id": "N01", "positive": False, "sender": "antigravity", "thread_id": "eval/n01", "to": ["memory-bxxxxx"], "msg_type": "ack", "match_tokens": (), "body": "ACK, received and queued."},
    {"id": "N02", "positive": False, "sender": "antigravity", "thread_id": "eval/n02", "to": ["memory-bxxxxx"], "msg_type": "status", "match_tokens": (), "body": "Local pytest passed 85 tests in 2.1s, git diff --check is clean, ready when you are."},
    {"id": "N03", "positive": False, "sender": "antigravity", "thread_id": "eval/n03", "to": ["memory-bxxxxx"], "msg_type": "note", "match_tokens": (), "body": "Going idle now with wait_next armed on both spool logs; ping me if anything comes up."},
    {"id": "N04", "positive": False, "sender": "antigravity", "thread_id": "directive-rollout-example", "to": ["txxxx", "pxx", "kxxx"], "msg_type": "answer", "match_tokens": (), "body": "When reading a message carrying seat_directive, run meshy_node directive-check --key K once to claim it and directive-done afterwards."},
)

def evaluate_live_benchmark(state_file: Path, base_url: str, rerank_url: str, *, drafts: tuple[dict[str, Any], ...] = DEFAULT_BENCHMARK_DRAFTS,
                            card_min_score: float = DEFAULT_CARD_MIN_SCORE, mesh_min_score: float = DEFAULT_MESH_MIN_SCORE,
                            min_dir: float = DEFAULT_MIN_DIR_SCORE, now: datetime | None = None) -> dict[str, Any]:
    require_rerank_url_ok(rerank_url)
    state, ledger, read_cache = node.load_state(state_file, base_url), set(), {}
    when = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    utc_day, oldest, shown_hints, pos_fired, neg_fired = when.strftime("%Y-%m-%d"), when - timedelta(days=FRESH_DAYS), [], 0, 0
    st_peer = state.get("peer_id") if isinstance(state, dict) else None
    st_label = state.get("label") if isinstance(state, dict) else None
    for d in drafts:
        body = d["body"].strip()
        if str(d.get("msg_type") or "").lower() == "ack" or len(body) < MIN_DRAFT_CHARS or is_non_actionable_receipt(body): continue
        s_labels = set(norm_labels([d.get("sender")]))
        s_stems = {seat_stem(x) for x in s_labels if seat_stem(x)}
        s_id = seat_stem(d.get("sender")) or next(iter(sorted(s_labels)), "unknown")
        s_peer = d.get("sender_peer") or (st_peer if st_label and seat_stem(st_label) in s_stems else None)
        s_th, s_irt, s_ref = str(d.get("thread_id") or "").strip(), str(d.get("in_reply_to") or "").strip(), str(d.get("ref") or "").strip()
        s_closes = set(norm_labels([d.get("closes")])) | {str(x).strip() for x in (d.get("closes") or ()) if str(x).strip()}
        s_rcpt = set(norm_labels([d.get("to"), d.get("cc")]))
        s_rcpt_stems = {seat_stem(x) for x in s_rcpt if seat_stem(x)}
        excl_labels, excl_stems, seen_keys, cands = s_labels | s_rcpt, s_stems | s_rcpt_stems, set(), []
        cs = _retry_call(lambda: cards.search(state_file, state, body[:2000], ns=None, k=8, rerank=False, types=("Ruling", "Decision", "Constraint"),
                                              thread=None, seat=None, owner=None, status="active", since=None, until=None))
        for ch in cs.get("hits", ()):
            ck = ch["key"]
            if ck in seen_keys: continue
            seen_keys.add(ck)
            key_seat = ck.split("/")[1] if ck.startswith("card/") and ck.count("/") >= 2 else ch.get("author")
            c_labels = set(norm_labels([key_seat, ch.get("author")]))
            c_stems = {seat_stem(x) for x in c_labels if seat_stem(x)}
            c_th, c_body = str(ch.get("thread_id") or "").strip(), str(ch.get("body") or "").strip()[:1500]
            norm_c = normalize_rerank_text(c_body)
            if (c_body and norm_c and not ((s_th and c_th and s_th == c_th) or (s_irt and ck == s_irt)
                    or ck in s_closes or ck in body or (s_ref and ck == s_ref)
                    or bool(c_labels & excl_labels) or bool(c_stems & excl_stems) or (s_id, ck, utc_day) in ledger)):
                cands.append({"source": "decisions", "key": ck, "author": key_seat, "thread_id": c_th or None,
                              "min_score": card_min_score, "raw_body": c_body, "norm_body": norm_c, "stems": c_stems})
        ms = _retry_call(lambda: node.search(state_file, state, q=body[:2000], k=20, rerank=False))
        for mh in ms.get("hits", ()):
            mk = mh["key"]
            if mk in seen_keys or not _is_fresh_mesh_hit(mh, oldest=oldest): continue
            seen_keys.add(mk)
            if mk not in read_cache:
                try: read_cache[mk] = _retry_call(lambda kk=mk: node.read(state_file, state, key=kk))
                except node.ServerSaid as exc:
                    if exc.status in (403, 404): read_cache[mk] = None
                    else: raise
            mdoc = read_cache[mk]
            if not isinstance(mdoc, dict): continue
            ts_src = mdoc if (mdoc.get("recorded_at") is not None or mdoc.get("created_at") is not None) else mh
            if not _is_fresh_mesh_hit(ts_src, oldest=oldest) or str(mdoc.get("type") or "").strip().lower() == "ack": continue
            m_labels = set(norm_labels([mdoc.get("author") or mh.get("author"), mdoc.get("from")]))
            m_stems = {seat_stem(x) for x in m_labels if seat_stem(x)}
            m_rcpt = set(norm_labels([mdoc.get("to"), mdoc.get("cc")]))
            m_rcpt_stems = {seat_stem(x) for x in m_rcpt if seat_stem(x)}
            m_th = str(mdoc.get("thread_id") if mdoc.get("thread_id") is not None else mh.get("thread_id") or "").strip()
            raw_m = mdoc.get("body") if isinstance(mdoc.get("body"), str) else (mh.get("body") or "")
            m_body = str(raw_m).strip()[:1500]
            norm_m = normalize_rerank_text(m_body)
            if (len(m_body) >= MIN_CANDIDATE_CHARS and norm_m and not is_non_actionable_receipt(m_body)
                    and not ((s_th and m_th and s_th == m_th) or (s_irt and mk == s_irt)
                             or mk in s_closes or mk in body or (s_ref and mk == s_ref)
                             or (s_peer and mdoc.get("signed_by") and mdoc.get("signed_by") == s_peer)
                             or bool(m_labels & excl_labels) or bool(m_stems & excl_stems)
                             or bool(m_rcpt & s_labels) or bool(m_rcpt_stems & s_stems)
                             or (s_id, mk, utc_day) in ledger)):
                cands.append({"source": "mesh_chat", "key": mk, "author": mdoc.get("author") or mh.get("author"),
                              "thread_id": m_th or None, "min_score": mesh_min_score, "raw_body": m_body,
                              "norm_body": norm_m, "stems": m_stems | m_rcpt_stems})
        if not cands: continue
        norm_q = normalize_rerank_text(body) or body[:RERANK_TEXT_CHARS]
        fwds, kept = http_rerank(rerank_url, norm_q, [c["norm_body"] for c in cands]), []
        verify_idxs = [i for i in sorted(range(len(cands)), key=lambda i: -fwds[i])
                       if fwds[i] >= (0.0 if cands[i]["source"] == "decisions" else min_dir)][:REVERSE_DEPTH]
        for idx in verify_idxs:
            c, fwd = cands[idx], fwds[idx]
            req_floor = 0.0 if c["source"] == "decisions" else min_dir
            rev = http_rerank(rerank_url, c["norm_body"], [norm_q])[0]
            if min(fwd, rev) < req_floor: continue
            excl = excl_stems | c["stems"]
            shared = bool(c["source"] != "decisions" and (extract_technical_anchors(body, exclude_stems=excl) & extract_technical_anchors(c["raw_body"], exclude_stems=excl)))
            if (eff := 0.5 * (fwd + rev) + (ANCHOR_BONUS if shared else 0.0)) >= c["min_score"]:
                kept.append((eff, idx, c))
        kept = sorted(kept, key=lambda item: (-item[0], 0 if item[2]["source"] == "decisions" else 1, item[1]))[:3]
        emitted = []
        for sc, _idx, c in kept:
            if (s_id, c["key"], utc_day) in ledger or len(ledger) >= 16_384: continue
            ledger.add((s_id, c["key"], utc_day))
            emitted.append((sc, c))
        if emitted: pos_fired += int(bool(d["positive"])); neg_fired += int(not d["positive"])
        tokens = tuple(t.lower() for t in d.get("match_tokens", ()))
        for sc, c in emitted:
            shown_hints.append({"draft_id": d["id"], "positive_draft": d["positive"], "source": c["source"], "key": c["key"], "author": c["author"],
                                "thread_id": c["thread_id"], "rerank_score": round(sc, 4), "true_positive": bool(d["positive"] and any(tok in c["raw_body"].lower() for tok in tokens))})
    tp, total_shown = sum(1 for h in shown_hints if h["true_positive"]), len(shown_hints)
    return {"drafts_total": len(drafts), "positive_drafts": sum(1 for d in drafts if d["positive"]), "negative_drafts": sum(1 for d in drafts if not d["positive"]),
            "positive_drafts_fired": pos_fired, "negative_drafts_fired": neg_fired, "hints_shown": total_shown, "true_positives": tp,
            "precision": round(tp / total_shown, 4) if total_shown else None, "wilson_95": list(wilson_95(tp, total_shown)),
            "thresholds": {"decisions": card_min_score, "mesh_chat": mesh_min_score, "min_direction": min_dir}, "items": shown_hints}

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Evaluate pre-send collision hints on graded45 and live benchmark.")
    for flag, kw in (("--base-url", {"required": True}), ("--state-file", {"type": Path, "required": True}),
                     ("--rerank-url", {"default": "http://127.0.0.1:7876"}), ("--graded-key", {"default": "msg/1791467655849-uxxx-3938a9"}),
                     ("--skip-graded", {"action": "store_true"}), ("--skip-benchmark", {"action": "store_true"})):
        ap.add_argument(flag, **kw)
    args = ap.parse_args(argv)
    try: require_rerank_url_ok(args.rerank_url)
    except ContentRefused as exc:
        print(f"NOT RUN: {exc}", file=sys.stderr)
        return 2
    base_url = node.normalize_base_url(args.base_url)
    state, out = node.load_state(args.state_file, base_url), {}
    if not args.skip_graded:
        pairs = extract_graded_csv(_retry_call(lambda: node.read(args.state_file, state, key=args.graded_key))["body"])
        msgs = {k: _retry_call(lambda kk=k: node.read(args.state_file, state, key=kk)) for k in sorted({p["send_key"] for p in pairs} | {p["hint_key"] for p in pairs})}
        out["graded45"] = evaluate_graded_pairs(pairs, msgs, lambda q, docs: http_rerank(args.rerank_url, q, docs))
    if not args.skip_benchmark:
        out["live_benchmark"] = evaluate_live_benchmark(args.state_file, base_url, args.rerank_url)
    print(json.dumps(out, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
