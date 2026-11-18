"""Pre-send semantic collision hints (`POST /v1/mesh/hints`): multi-source pre-cut drops, receipt
filtering, and chunked bidirectional cross-encoder scoring with zero prose returned on the wire."""
from __future__ import annotations
import dataclasses, datetime as _dt, json, math, threading, time
from psycopg.rows import dict_row
from apps.cards import store as card_store, validation as card_rules
from apps.context import validation as ctx_validation
from apps.core import dbconn, env
from apps.memoryskills.errors import HandlerError
from apps.memoryskills.permissions import NAMESPACE_WIDE, check_permission
from apps.memoryskills.validation import MESH_CHAT_NAMESPACE
from apps.mesh import views as mesh_views
from apps.peers import errors
from . import ctxindex, ctxjob, ctxsearch, embedjob, graphsearch, graphwalk, providers, search as S, spaces, store
from .hint_rules import (ANCHOR_BONUS, BROADCAST_LABELS, DEFAULT_CARD_MIN_SCORE, DEFAULT_CONTEXT_MIN_SCORE,
                         DEFAULT_MESH_MIN_SCORE, DEFAULT_MIN_DIRECTION_SCORE, FRESH_DAYS, MIN_CANDIDATE_CHARS, MIN_DRAFT_CHARS,
                         RERANK_CHUNK, RERANK_TEXT_CHARS, REVERSE_DEPTH, extract_technical_anchors,
                         is_non_actionable_receipt, norm_labels as _norm_labels, normalize_rerank_text, seat_stem)

MAX_HINTS, MAX_DRAFT_CHARS, MAX_RECIPIENTS, MAX_THREAD_CHARS, MAX_LABEL_CHARS = 3, 4000, 64, 256, 64
RERANK_BUDGET_S, CARD_POOL_K, MESH_POOL_K, CTX_POOL_K = 12.0, 8, 20, 6
CARD_HINT_TYPES = frozenset({"Ruling", "Decision", "Constraint"})

def enabled() -> bool:
    return env.env_flag("MESHY_SEND_HINTS_ENABLED", "MESHY_SEND_HINTS_ENABLED")

def include_context_enabled() -> bool:
    raw = env.setting("MESHY_SEND_HINTS_INCLUDE_CONTEXT", "MESHY_SEND_HINTS_INCLUDE_CONTEXT", default=None)
    return True if raw is None else env.env_flag("MESHY_SEND_HINTS_INCLUDE_CONTEXT", "MESHY_SEND_HINTS_INCLUDE_CONTEXT")

def _float_setting(name: str, default: float) -> float:
    try:
        val = float(env.setting(name, name, default=default))
    except (TypeError, ValueError):
        return default
    return val if math.isfinite(val) else default

def card_min_score() -> float: return _float_setting("MESHY_SEND_HINTS_CARD_MIN_SCORE", DEFAULT_CARD_MIN_SCORE)
def mesh_min_score() -> float: return _float_setting("MESHY_SEND_HINTS_MESH_MIN_SCORE", DEFAULT_MESH_MIN_SCORE)
def context_min_score() -> float: return _float_setting("MESHY_SEND_HINTS_CONTEXT_MIN_SCORE", DEFAULT_CONTEXT_MIN_SCORE)
def min_direction_score() -> float: return _float_setting("MESHY_SEND_HINTS_MIN_DIRECTION_SCORE", DEFAULT_MIN_DIRECTION_SCORE)

class DailyHintLedger:
    """Bounded in-memory ledger enforcing <= 1 showing per `(sender_stem, target_key, utc_day)`."""
    def __init__(self, max_entries: int = 16_384):
        self.max_entries, self._shown, self._lock = max_entries, {}, threading.Lock()
    def seen(self, sender_id: str, target_key: str, utc_day: str) -> bool:
        with self._lock: return (sender_id, target_key, utc_day) in self._shown
    def _has_room_locked(self, utc_day: str) -> bool:
        if len(self._shown) >= self.max_entries:
            for k in [k for k in self._shown if k[2] < utc_day]: del self._shown[k]
        return len(self._shown) < self.max_entries
    def can_claim(self, sender_id: str, target_key: str, utc_day: str) -> bool:
        with self._lock:
            return (sender_id, target_key, utc_day) not in self._shown and self._has_room_locked(utc_day)
    def claim(self, sender_id: str, target_key: str, utc_day: str, *, now_ts: float = 0.0) -> bool:
        entry = (sender_id, target_key, utc_day)
        with self._lock:
            if entry in self._shown or not self._has_room_locked(utc_day): return False
            self._shown[entry] = now_ts
            return True
    def record(self, sender_id: str, target_key: str, utc_day: str, *, now_ts: float = 0.0) -> bool:
        return self.claim(sender_id, target_key, utc_day, now_ts=now_ts)
    def forget(self) -> None:
        with self._lock: self._shown.clear()

_DAILY_LEDGER = DailyHintLedger()
forget = _DAILY_LEDGER.forget

HintRequest = dataclasses.make_dataclass("HintRequest", [
    ("body", str), ("thread_id", str | None, None), ("in_reply_to", str | None, None),
    ("closes", tuple[str, ...], ()), ("to", tuple[str, ...], ()), ("cc", tuple[str, ...], ()),
    ("sender_label", str | None, None), ("msg_type", str | None, None), ("ref", str | None, None)], frozen=True)
_Candidate = dataclasses.make_dataclass("_Candidate", [
    "source", "namespace", "key", "kind", "author", "author_peer", "author_labels", "rcpt_labels",
    "thread_id", "in_reply_to", "title", "raw_text", "rerank_text", "min_score"])

def _opt_str(data: dict, key: str, *, max_chars: int) -> str | None:
    if (val := data.get(key)) is None: return None
    if not isinstance(val, str) or "\x00" in val:
        raise errors.ApiError(422, errors.E_VALIDATION, f"{key} must be a NUL-free string")
    if len(s := val.strip()) > max_chars:
        raise errors.ApiError(422, errors.E_VALIDATION, f"{key} must be at most {max_chars} characters")
    return s or None

def _opt_str_list(data: dict, key: str, *, max_items: int = MAX_RECIPIENTS, item_max: int = 128) -> tuple[str, ...]:
    if (val := data.get(key)) is None: return ()
    if not isinstance(val, list) or any(not isinstance(x, str) or "\x00" in x for x in val):
        raise errors.ApiError(422, errors.E_VALIDATION, f"{key} must be a list of NUL-free strings")
    if len(val) > max_items:
        raise errors.ApiError(422, errors.E_VALIDATION, f"{key} must have at most {max_items} items")
    out = [item.strip() for item in val if item.strip()]
    if any(len(s) > item_max for s in out):
        raise errors.ApiError(422, errors.E_VALIDATION, f"{key} entries must be at most {item_max} characters")
    return tuple(out)

def parse_request(raw_body: bytes) -> HintRequest:
    try: parsed = json.loads((raw_body or b"").decode("utf-8"))
    except (UnicodeDecodeError, ValueError): raise errors.ApiError(422, errors.E_VALIDATION, "request body must be valid JSON")
    if not isinstance(parsed, dict): raise errors.ApiError(422, errors.E_VALIDATION, "request body must be a JSON object")
    raw_text = parsed.get("body") if "body" in parsed else parsed.get("query")
    if not isinstance(raw_text, str) or "\x00" in raw_text:
        raise errors.ApiError(422, errors.E_VALIDATION, "body is required and must be a NUL-free string")
    if not (body := raw_text.strip()) or len(body) > MAX_DRAFT_CHARS:
        raise errors.ApiError(422, errors.E_VALIDATION, "body must not be empty" if not body else f"body must be at most {MAX_DRAFT_CHARS} characters")
    thread_id = _opt_str(parsed, "thread_id", max_chars=MAX_THREAD_CHARS) or (
        _opt_str(parsed, "thread", max_chars=MAX_THREAD_CHARS) if "thread" in parsed else None)
    return HintRequest(
        body=body, thread_id=thread_id, in_reply_to=_opt_str(parsed, "in_reply_to", max_chars=256),
        closes=_opt_str_list(parsed, "closes", max_items=32, item_max=256),
        to=_opt_str_list(parsed, "to"), cc=_opt_str_list(parsed, "cc"),
        sender_label=_opt_str(parsed, "sender_label", max_chars=MAX_LABEL_CHARS),
        msg_type=_opt_str(parsed, "msg_type", max_chars=32), ref=_opt_str(parsed, "ref", max_chars=256))

def _may_read(c, namespace: str, peer_id) -> bool:
    try: check_permission(namespace, peer_id, "read", c, resource=NAMESPACE_WIDE); return True
    except HandlerError: return False

def _card_title(card_type: str, seat: str | None, owner: str | None, refs) -> str:
    parts = [card_type, *([f"owner:{owner}"] if owner else []), *([f"seat:{seat}"] if seat else [])]
    if isinstance(refs, dict) and isinstance(refs.get("prs"), list) and refs["prs"]:
        parts.append(",".join(str(x) for x in refs["prs"][:3]))
    return " ".join(parts)[:160]

def _gather_card_candidates(conn, space: spaces.Space, peer_id, query: str) -> tuple[list[_Candidate], dict]:
    card_ns = card_rules.namespace()
    with dbconn.request_server_internal(conn) as c:
        if not _may_read(c, card_ns, peer_id): return [], {"ran": False, "reason": "permission_denied"}
        if card_ns not in graphwalk.walked_namespaces(): return [], {"ran": False, "reason": "not_walked"}
        superseded, replacing = card_store.superseders(c, card_ns), card_store.replacers(c, card_ns, CARD_HINT_TYPES)
    meta_by_key: dict[str, dict] = {}
    def keep(key: str, value: dict) -> bool:
        ok = card_store.matches(value, key, types=CARD_HINT_TYPES, status="active", superseded=superseded, replacing=replacing)
        if ok and isinstance(value, dict):
            meta_by_key[key] = {"type": str(value.get("type") or "Decision"), "seat": str(value.get("seat") or "") or None,
                                "owner": str(value.get("owner") or "") or None, "refs": value.get("refs") if isinstance(value.get("refs"), dict) else None,
                                "thread_id": value.get("thread_id") if isinstance(value.get("thread_id"), str) else None}
        return ok
    try: res = graphsearch.search(conn, space, card_ns, query, k=CARD_POOL_K, rerank=False, keep=keep)
    except S.SemanticUnavailable as exc: return [], {"ran": False, "reason": exc.code}
    min_sc, out = card_min_score(), []
    for h in res.get("hits", ()):
        key, meta = h["key"], meta_by_key.get(h["key"], {})
        ctype, card_seat = meta.get("type") or "Decision", meta.get("seat")
        seat, body_txt = card_seat or h.get("author"), (h.get("body") or "").strip()
        if norm_txt := normalize_rerank_text(body_txt):
            out.append(_Candidate("decisions", card_ns, key, ctype, seat, None,
                                  tuple(_norm_labels([card_seat] if card_seat else [h.get("author")])), (),
                                  meta.get("thread_id") or h.get("thread_id"), None,
                                  _card_title(ctype, seat, meta.get("owner"), meta.get("refs")),
                                  body_txt[:embedjob.MAX_CHARS], norm_txt, min_sc))
    return out, {"ran": True, "candidates": len(out)}

def _gather_mesh_candidates(conn, space: spaces.Space, query: str, *, now: _dt.datetime) -> tuple[list[_Candidate], dict]:
    try: res = S.search(conn, space, query, k=MESH_POOL_K, rerank=False)
    except S.SemanticUnavailable as exc: return [], {"ran": False, "reason": exc.code}
    hits = res.get("hits", ())
    if not (keys := [h["key"] for h in hits if isinstance(h.get("key"), str)]): return [], {"ran": True, "candidates": 0}
    peer_labels_cache: dict[str, list[str]] = {}
    with dbconn.request_server_internal(conn) as c:
        with c.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT key, created_by_peer_id::text AS author_peer, created_at, value "
                        "FROM shared_memory WHERE namespace = %(ns)s AND key = ANY(%(keys)s::text[]) AND is_head",
                        {"ns": MESH_CHAT_NAMESPACE, "keys": keys})
            row_by_key = {r["key"]: r for r in cur.fetchall()}
        for r in row_by_key.values():
            if (ap := r.get("author_peer")) and ap not in peer_labels_cache:
                peer_labels_cache[ap] = [x["label"].lower() for x in mesh_views.labels_for(c, ap)]
    oldest, min_sc, out, stale_dropped = now - _dt.timedelta(days=FRESH_DAYS), mesh_min_score(), [], 0
    for h in hits:
        if (row := row_by_key.get(h["key"])) is None: continue
        if row["created_at"] < oldest: stale_dropped += 1; continue
        val = row["value"] if isinstance(row["value"], dict) else {}
        mtype, body_txt = str(val.get("type") or "").strip().lower(), (h.get("body") or "").strip()
        norm_txt = normalize_rerank_text(body_txt)
        if mtype == "ack" or len(body_txt) < MIN_CANDIDATE_CHARS or not norm_txt or is_non_actionable_receipt(body_txt): continue
        ap = row.get("author_peer")
        subj = val.get("subject").strip() if isinstance(val.get("subject"), str) and val.get("subject").strip() else None
        irt = val.get("in_reply_to").strip() if isinstance(val.get("in_reply_to"), str) and val.get("in_reply_to").strip() else None
        out.append(_Candidate(
            "mesh_chat", MESH_CHAT_NAMESPACE, h["key"], mtype or "message", h.get("author"), ap,
            tuple(_norm_labels([h.get("author"), val.get("from"), peer_labels_cache.get(ap, ())])),
            tuple(_norm_labels([val.get("to"), val.get("cc")])),
            h.get("thread_id") or (val.get("thread_id") if isinstance(val.get("thread_id"), str) else None),
            irt, subj[:160] if subj else None, body_txt[:embedjob.MAX_CHARS], norm_txt, min_sc))
    return out, {"ran": True, "candidates": len(out), "stale_dropped": stale_dropped}

def _gather_context_candidates(conn, space: spaces.Space, peer_id, query: str, *, now: _dt.datetime) -> tuple[list[_Candidate], dict]:
    ctx_ns = ctx_validation.namespace()
    with dbconn.request_server_internal(conn) as c:
        if not _may_read(c, ctx_ns, peer_id): return [], {"ran": False, "reason": "permission_denied"}
    if not include_context_enabled(): return [], {"ran": False, "reason": "disabled"}
    if not ctxjob.enabled(): return [], {"ran": False, "reason": "index_off"}
    if not store.forbidden_rows_open(): return [], {"ran": False, "reason": "gate_closed"}
    flt = ctxindex.Filters(seat=None, src=None, project=None, since=now - _dt.timedelta(days=FRESH_DAYS), until=now)
    try: res = ctxsearch.search(conn, space, ctx_ns, query, flt, k=CTX_POOL_K, rerank=False, collapse="session")
    except (errors.ApiError, S.SemanticUnavailable) as exc: return [], {"ran": False, "reason": getattr(exc, "code", "unavailable")}
    min_sc, out = context_min_score(), []
    for h in res.get("hits", ()):
        txt = (h.get("text") or "").strip()
        if len(txt) < MIN_CANDIDATE_CHARS or not (norm_txt := normalize_rerank_text(txt)): continue
        seat, src = str(h.get("seat") or "").strip(), str(h.get("src") or "").strip()
        gen, batch, first_ord, last_ord = int(h.get("gen") or 0), int(h.get("batch") or 0), int(h.get("first_ord") or 0), int(h.get("last_ord") or 0)
        out.append(_Candidate("transcript_context", ctx_ns, f"ctx/{seat}/{src}/{gen:04d}/{batch:06d}/{first_ord:010d}",
                              "transcript_session", seat or None, None, tuple(_norm_labels([seat])), (), None, None,
                              f"{seat} session {src[:12]} ({first_ord}..{last_ord})", txt[:embedjob.MAX_CHARS], norm_txt, min_sc))
    return out, {"ran": True, "candidates": len(out)}

def _chunked_rerank(query: str, docs: list[str], *, deadline: float, pinned_id: str | None = None) -> tuple[list[float] | None, str | None, list[dict]]:
    scores_out, attempts_out, pid = [], [], pinned_id
    for i in range(0, len(docs), RERANK_CHUNK):
        chunk = docs[i:i + RERANK_CHUNK]
        rr = providers.rerank(query, chunk, budget_s=max(0.1, deadline - time.monotonic()), provider_ids={pid} if pid else None)
        attempts_out.extend(rr.attempts)
        if rr.ran and rr.scores is not None:
            pid = pid or rr.provider_id; scores_out.extend(rr.scores); continue
        if len(chunk) > 1 and rr.attempts and str(rr.attempts[-1].get("reason") or "") == "HTTP 500":
            for doc in chunk:
                sub = providers.rerank(query, [doc], budget_s=max(0.1, deadline - time.monotonic()), provider_ids={pid} if pid else None)
                attempts_out.extend(sub.attempts)
                if not sub.ran or sub.scores is None: return None, pid, attempts_out
                pid = pid or sub.provider_id; scores_out.extend(sub.scores)
            continue
        return None, pid, attempts_out
    return scores_out, pid, attempts_out

def apply_precut_drops(
    candidates: list[_Candidate], *, req: HintRequest, sender_peer: str, sender_labels: set[str],
    sender_stems: set[str], addressed_labels: set[str], addressed_stems: set[str],
    primary_sender_id: str, utc_day: str, ledger: DailyHintLedger = _DAILY_LEDGER,
) -> tuple[list[_Candidate], dict[str, int]]:
    dropped = {"same_thread_or_reply": 0, "same_author_or_addressed": 0, "daily_cap": 0}
    closes_set, seen_keys, kept = set(req.closes), set(), []
    excl_labels, excl_stems = sender_labels | addressed_labels, sender_stems | addressed_stems
    for cand in candidates:
        if cand.key in seen_keys: continue
        seen_keys.add(cand.key)
        if ((req.thread_id and cand.thread_id and cand.thread_id == req.thread_id)
                or (req.in_reply_to and cand.key == req.in_reply_to)
                or cand.key in closes_set or cand.key in req.body or (req.ref and cand.key == req.ref)):
            dropped["same_thread_or_reply"] += 1; continue
        c_labels, r_labels = set(cand.author_labels), set(cand.rcpt_labels)
        c_stems, r_stems = {seat_stem(x) for x in c_labels if seat_stem(x)}, {seat_stem(x) for x in r_labels if seat_stem(x)}
        if ((cand.author_peer is not None and cand.author_peer == sender_peer)
                or bool(c_labels & excl_labels) or bool(c_stems & excl_stems)
                or bool(r_labels & sender_labels) or bool(r_stems & sender_stems)):
            dropped["same_author_or_addressed"] += 1; continue
        if ledger.seen(primary_sender_id, cand.key, utc_day):
            dropped["daily_cap"] += 1; continue
        kept.append(cand)
    return kept, dropped

def _rerank_unavailable(space_name: str, dropped: dict, attempts: list[dict], sources: dict) -> dict:
    return {"enabled": True, "space": space_name, "hints": [], "dropped": dropped, "reranked": False,
            "abstained_reason": "rerank_unavailable", "providers": {"rerank": None},
            "attempts": {"rerank": S.wire_attempts(attempts)}, "sources": sources}

def evaluate_hints(conn, space: spaces.Space, peer_id, req: HintRequest, *,
                   now: _dt.datetime | None = None, ledger: DailyHintLedger = _DAILY_LEDGER) -> dict:
    if not enabled():
        return {"enabled": False, "hints": [], "dropped": {}, "reranked": False, "abstained_reason": "disabled"}
    if (req.msg_type or "").lower() == "ack" or len(req.body) < MIN_DRAFT_CHARS or is_non_actionable_receipt(req.body):
        return {"enabled": True, "hints": [], "dropped": {}, "reranked": False, "abstained_reason": "ack_or_short_draft"}
    when = now or _dt.datetime.now(_dt.timezone.utc)
    utc_day, sender_peer = when.astimezone(_dt.timezone.utc).strftime("%Y-%m-%d"), str(peer_id)
    with dbconn.request_server_internal(conn) as c:
        srv_labels = [x["label"].lower() for x in mesh_views.labels_for(c, peer_id)]
    sender_labels, addressed_labels = set(_norm_labels([srv_labels, req.sender_label])), set(_norm_labels([req.to, req.cc]))
    sender_stems, addressed_stems = {seat_stem(x) for x in sender_labels if seat_stem(x)}, {seat_stem(x) for x in addressed_labels if seat_stem(x)}
    primary_sender_id = (seat_stem(srv_labels[0]) if srv_labels and seat_stem(srv_labels[0]) else (seat_stem(req.sender_label) or sender_peer))

    query = req.body[:S.MAX_QUERY_CHARS]
    card_cands, card_src = _gather_card_candidates(conn, space, peer_id, query)
    mesh_cands, mesh_src = _gather_mesh_candidates(conn, space, query, now=when)
    ctx_cands, ctx_src = _gather_context_candidates(conn, space, peer_id, query, now=when)
    sources = {"decisions": card_src, "mesh_chat": mesh_src, "transcript_context": ctx_src}
    survivors, dropped = apply_precut_drops(
        [*card_cands, *mesh_cands, *ctx_cands], req=req, sender_peer=sender_peer,
        sender_labels=sender_labels, sender_stems=sender_stems, addressed_labels=addressed_labels,
        addressed_stems=addressed_stems, primary_sender_id=primary_sender_id, utc_day=utc_day, ledger=ledger)
    dropped["below_threshold"] = 0
    if not survivors:
        return {"enabled": True, "space": space.name, "hints": [], "dropped": dropped, "reranked": False, "sources": sources}

    norm_draft, deadline = normalize_rerank_text(req.body) or req.body[:RERANK_TEXT_CHARS], time.monotonic() + RERANK_BUDGET_S
    fwd_scores, pid, attempts = _chunked_rerank(norm_draft, [c.rerank_text for c in survivors], deadline=deadline)
    if fwd_scores is None or pid is None: return _rerank_unavailable(space.name, dropped, attempts, sources)

    dir_floor, verify_set = min_direction_score(), set()
    for idx in sorted(range(len(survivors)), key=lambda i: -fwd_scores[i]):
        if fwd_scores[idx] >= (0.0 if survivors[idx].source == "decisions" else dir_floor) and len(verify_set) < REVERSE_DEPTH:
            verify_set.add(idx)

    draft_excl = sender_stems | addressed_stems
    draft_anchors, scored = extract_technical_anchors(req.body, exclude_stems=draft_excl), []
    for idx, (cand, fwd) in enumerate(zip(survivors, fwd_scores)):
        if idx not in verify_set: dropped["below_threshold"] += 1; continue
        rev_rr = providers.rerank(cand.rerank_text, [norm_draft], budget_s=max(0.1, deadline - time.monotonic()), provider_ids={pid})
        attempts.extend(rev_rr.attempts)
        if not rev_rr.ran or rev_rr.scores is None: return _rerank_unavailable(space.name, dropped, attempts, sources)
        rev = float(rev_rr.scores[0])
        if min(fwd, rev) < (0.0 if cand.source == "decisions" else dir_floor):
            dropped["below_threshold"] += 1; continue
        eff = 0.5 * (float(fwd) + rev)
        if cand.source != "decisions" and draft_anchors:
            c_excl = draft_excl | {seat_stem(x) for x in (*cand.author_labels, *cand.rcpt_labels) if seat_stem(x)}
            if (draft_anchors - {f"id:{x}" for x in c_excl}) & extract_technical_anchors(cand.raw_text, exclude_stems=c_excl):
                eff += ANCHOR_BONUS
        if eff < cand.min_score: dropped["below_threshold"] += 1
        else: scored.append((eff, idx, cand))

    scored.sort(key=lambda item: (-item[0], 0 if item[2].source == "decisions" else 1, item[1]))
    now_ts, hints_out, overflow = when.timestamp(), [], False
    for score, _idx, cand in scored:
        if len(hints_out) >= MAX_HINTS:
            if ledger.can_claim(primary_sender_id, cand.key, utc_day):
                overflow = True
                break
            continue
        if not ledger.claim(primary_sender_id, cand.key, utc_day, now_ts=now_ts):
            dropped["daily_cap"] += 1
            continue
        hints_out.append({"source": cand.source, "namespace": cand.namespace, "key": cand.key, "kind": cand.kind,
                          "author": cand.author, "thread_id": cand.thread_id, "title": cand.title, "rerank_score": round(score, 6)})
    return {"enabled": True, "space": space.name, "hints": hints_out, "truncated": overflow,
            "dropped": dropped, "reranked": True,
            "thresholds": {"decisions": card_min_score(), "mesh_chat": mesh_min_score(),
                           "transcript_context": context_min_score(), "min_direction": dir_floor},
            "providers": {"rerank": pid}, "attempts": {"rerank": S.wire_attempts(attempts)}, "sources": sources}

__all__ = ["ANCHOR_BONUS", "BROADCAST_LABELS", "CARD_HINT_TYPES", "DEFAULT_CARD_MIN_SCORE", "DEFAULT_CONTEXT_MIN_SCORE",
           "DEFAULT_MESH_MIN_SCORE", "DEFAULT_MIN_DIRECTION_SCORE", "DailyHintLedger", "HintRequest", "MAX_HINTS",
           "apply_precut_drops", "enabled", "evaluate_hints", "extract_technical_anchors", "forget",
           "is_non_actionable_receipt", "normalize_rerank_text", "parse_request", "seat_stem"]
