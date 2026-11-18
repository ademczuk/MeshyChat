"""Opt-in conversation enrichment for `meshy_node read --with-context` and `context --with-context`.

Attaches a bounded `conversation_context` block combining:
  1. `related_edges`: cross-thread persisted `related_to` edges (routing metadata and pair scores only, no peer bodies).
  2. `active_cards`: matching active `shared/decisions` cards (`Ruling`, `Decision`, `Constraint`, `Commitment`, `OpenItem`).
  3. `transcript_pointers`: condensed `shared/transcript-context` session pointers (`seat`, `src`, `gen`, `batch`,
     `first_ord`, `last_ord`, `time_last`, `score`, `rerank_score`, `read_cmd`) plus `transcript_graph.READ_HINT`.

Security & reliability invariants:
  - Opt-in only: plain `read` and `context` outputs are untouched when `--with-context` is absent.
  - Zero raw transcript text and zero untrusted transcript markers are ever emitted here; readers inspect turns
    explicitly through the returned `read_cmd` (`meshy_node transcript-read`).
  - Advisory / fail-open per source: if `neighbours`, `cards`, or `transcript-context` is unavailable or unpermitted,
    that sub-block records a short status reason without failing the primary `read` or `context` command.
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import time
from typing import Any

from . import cards, node, transcript_graph, wire

MAX_RELATED_EDGES = 5
MAX_ACTIVE_CARDS = 5
MAX_TRANSCRIPT_POINTERS = 3
CARD_SUMMARY_MAX_CHARS = 200
QUERY_TEXT_MAX_CHARS = 400
SUBJECT_MAX_CHARS = 256
KEY_MAX_CHARS = 512
CARD_THREAD_MAX_CHARS = 256
THREAD_ID_MAX_CHARS = 512
RAW_ID_MAX_CHARS = 4096
READ_CMD_MAX_CHARS = 1024
TRANSCRIPT_LOOKBACK_DAYS = 14
DEFAULT_DEADLINE_S = 10.0
TRUST_NOTE = "Untrusted peer-authored card summaries and routing pointers; verify signed rows before acting."
_monotonic = time.monotonic

ENRICH_CARD_TYPES = ("Ruling", "Decision", "Constraint", "Commitment", "OpenItem")
_CARD_HEADER_RE = re.compile(r"^([A-Za-z]+) \| owner: ([^\n]*)(?:\n|$)")
_WS_RE = re.compile(r"\s+")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]")


def _clean_text(val: Any, limit: int = CARD_SUMMARY_MAX_CHARS) -> str | None:
    if not isinstance(val, str):
        return None
    cleaned = _WS_RE.sub(" ", _CTRL_RE.sub(" ", val)).strip()
    if not cleaned:
        return None
    return cleaned[:limit]


def _clean_id(val: Any, limit: int = RAW_ID_MAX_CHARS) -> str | None:
    if not isinstance(val, str):
        return None
    cleaned = _CTRL_RE.sub("", val).strip()
    if not cleaned:
        return None
    return cleaned[:limit]


def _strip_card_header(raw: str) -> tuple[str | None, str | None, str]:
    m = _CARD_HEADER_RE.match(raw)
    if not m:
        return None, None, raw
    owner_part = m.group(2).split("|", 1)[0].strip() or None
    return m.group(1), owner_part, raw[m.end():]


def _card_summary(card_obj: dict[str, Any]) -> str | None:
    val = card_obj.get("value") if isinstance(card_obj.get("value"), dict) else card_obj
    for field in ("text", "summary", "title", "ruling", "decision", "what", "rule", "claim", "question", "body"):
        raw = val.get(field) if isinstance(val, dict) else None
        if raw is None:
            raw = card_obj.get(field)
        if isinstance(raw, str):
            _, _, stripped = _strip_card_header(raw)
            s = _clean_text(stripped or raw, CARD_SUMMARY_MAX_CHARS)
            if s:
                return s
    return None


def _extract_anchor_query_and_thread(primary: dict[str, Any], *, mode: str) -> tuple[str | None, str | None]:
    """Derive `(query_text, thread_id)` from either a `node.read` document or a `node.context` response."""
    if mode == "read":
        val = primary.get("value") if isinstance(primary.get("value"), dict) else primary
        thread_id = _clean_id(val.get("thread_id")) or _clean_id(primary.get("thread_id"))
        parts = []
        subj = _clean_text(val.get("subject"), SUBJECT_MAX_CHARS)
        if subj:
            parts.append(subj)
        body = _clean_text(val.get("body") if isinstance(val, dict) else primary.get("body"), QUERY_TEXT_MAX_CHARS)
        if body:
            parts.append(body)
        q = _clean_text(" ".join(parts), QUERY_TEXT_MAX_CHARS)
        return q or _clean_text(thread_id, QUERY_TEXT_MAX_CHARS), thread_id

    anchor = primary.get("anchor") if isinstance(primary.get("anchor"), dict) else {}
    thread_id = _clean_id(anchor.get("thread_id"))
    parts = []
    subj = _clean_text(anchor.get("subject"), SUBJECT_MAX_CHARS)
    if subj:
        parts.append(subj)
    body = _clean_text(anchor.get("body"), QUERY_TEXT_MAX_CHARS)
    if body:
        parts.append(body)
    q = _clean_text(" ".join(parts), QUERY_TEXT_MAX_CHARS)
    return q or _clean_text(thread_id, QUERY_TEXT_MAX_CHARS), thread_id


def _format_edge_item(item: dict[str, Any], *, anchor_thread: str | None) -> dict[str, Any] | None:
    key = _clean_id(item.get("key"), KEY_MAX_CHARS)
    if not key or not key.startswith("msg/"):
        return None
    raw_edge_thread = _clean_id(item.get("thread_id"))
    if anchor_thread and raw_edge_thread == anchor_thread:
        return None
    edge_thread = raw_edge_thread[:THREAD_ID_MAX_CHARS] if raw_edge_thread else None
    scores = item.get("scores") if isinstance(item.get("scores"), dict) else {}
    pair_raw = scores.get("pair")
    pair_score = round(float(pair_raw), 4) if isinstance(pair_raw, (int, float)) and not isinstance(pair_raw, bool) else None
    return {
        "key": key,
        "author": _clean_text(item.get("author"), 64),
        "thread_id": edge_thread,
        "direction": _clean_text(item.get("direction"), 16),
        "pair_score": pair_score,
        "recorded_at": _clean_text(item.get("recorded_at"), 64),
    }


def _collect_related_edges(
    state_file: Path,
    state: dict[str, Any],
    *,
    key: str,
    anchor_thread: str | None,
    context_body: dict[str, Any] | None = None,
    deadline: float | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    raw_hits: list[Any] | None = None
    if isinstance(context_body, dict):
        rel_block = context_body.get("related_edges")
        if isinstance(rel_block, dict) and isinstance(rel_block.get("hits"), list):
            raw_hits = rel_block["hits"]

    if raw_hits is None:
        ctx_mgr = node.finishing_by(deadline) if deadline is not None else contextlib.nullcontext()
        try:
            with ctx_mgr:
                nb = node.neighbours(state_file, state, key=key, include_inferred=True, limit=20)
            inferred = nb.get("inferred") if isinstance(nb, dict) else None
            if isinstance(inferred, dict) and isinstance(inferred.get("related_to"), list):
                raw_hits = inferred["related_to"]
            else:
                raw_hits = []
        except node.DeadlinePassed:
            return [], "unavailable (deadline_exceeded)"
        except Exception as exc:  # noqa: BLE001
            return [], f"unavailable ({type(exc).__name__})"

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_hits:
        if not isinstance(item, dict):
            continue
        formatted = _format_edge_item(item, anchor_thread=anchor_thread)
        if formatted is None or formatted["key"] in seen:
            continue
        seen.add(formatted["key"])
        out.append(formatted)
        if len(out) >= MAX_RELATED_EDGES:
            break
    return out, None


def _format_card_entry(raw_card: dict[str, Any]) -> dict[str, Any] | None:
    key = _clean_id(raw_card.get("key"), KEY_MAX_CHARS)
    if not key or not key.startswith("card/"):
        return None
    val = raw_card.get("value") if isinstance(raw_card.get("value"), dict) else raw_card
    raw_prose = val.get("text") if isinstance(val, dict) and isinstance(val.get("text"), str) else raw_card.get("body")
    hdr_type, hdr_owner, _ = _strip_card_header(raw_prose) if isinstance(raw_prose, str) else (None, None, "")
    key_match = cards.KEY_RE.fullmatch(key)
    key_seat = key_match.group(1) if key_match else None
    ctype = _clean_text(val.get("type") or raw_card.get("type") or hdr_type, 32)
    seat = _clean_text(val.get("seat") or raw_card.get("seat") or raw_card.get("author") or key_seat, 64)
    owner = _clean_text(val.get("owner") or raw_card.get("owner") or hdr_owner, 64)
    thread = _clean_id(
        val.get("thread_id") or val.get("thread") or raw_card.get("thread_id") or raw_card.get("thread"),
        THREAD_ID_MAX_CHARS,
    )
    summary = _card_summary(raw_card)
    entry: dict[str, Any] = {
        "key": key,
        "type": ctype,
        "seat": seat,
        "owner": owner,
        "thread": thread,
        "summary": summary,
    }
    for sfield in ("rerank_score", "score"):
        sv = raw_card.get(sfield)
        if isinstance(sv, (int, float)) and not isinstance(sv, bool):
            entry[sfield] = round(float(sv), 4)
    return entry


def _collect_active_cards(
    state_file: Path,
    state: dict[str, Any],
    *,
    query_text: str | None,
    thread_id: str | None,
    deadline: float | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    errors_seen: list[str] = []

    if thread_id and len(thread_id) <= CARD_THREAD_MAX_CHARS:
        ctx_mgr = node.finishing_by(deadline) if deadline is not None else contextlib.nullcontext()
        try:
            with ctx_mgr:
                listed = node.cards(
                    state_file,
                    state,
                    cards.list_query(
                        types=ENRICH_CARD_TYPES,
                        thread=thread_id,
                        seat=None,
                        owner=None,
                        status="active",
                        since=None,
                        until=None,
                        recorded_since=None,
                        limit=MAX_ACTIVE_CARDS,
                        after=None,
                    ),
                )
            for item in (listed.get("cards") or []) if isinstance(listed, dict) else []:
                if not isinstance(item, dict):
                    continue
                formatted = _format_card_entry(item)
                if formatted and formatted["key"] not in seen:
                    seen.add(formatted["key"])
                    out.append(formatted)
                    if len(out) >= MAX_ACTIVE_CARDS:
                        break
        except node.DeadlinePassed:
            errors_seen.append("deadline_exceeded")
        except Exception as exc:  # noqa: BLE001
            errors_seen.append(type(exc).__name__)

    if query_text and len(out) < MAX_ACTIVE_CARDS:
        if deadline is not None and _monotonic() >= deadline:
            errors_seen.append("deadline_exceeded")
        else:
            ctx_mgr = node.finishing_by(deadline) if deadline is not None else contextlib.nullcontext()
            try:
                with ctx_mgr:
                    searched = cards.search(
                        state_file,
                        state,
                        query_text,
                        ns=None,
                        k=MAX_ACTIVE_CARDS,
                        rerank=True,
                        types=ENRICH_CARD_TYPES,
                        thread=None,
                        seat=None,
                        owner=None,
                        status="active",
                        since=None,
                        until=None,
                    )
                for item in (searched.get("hits") or searched.get("cards") or []) if isinstance(searched, dict) else []:
                    if not isinstance(item, dict):
                        continue
                    formatted = _format_card_entry(item)
                    if formatted and formatted["key"] not in seen:
                        seen.add(formatted["key"])
                        out.append(formatted)
                        if len(out) >= MAX_ACTIVE_CARDS:
                            break
            except node.DeadlinePassed:
                errors_seen.append("deadline_exceeded")
            except Exception as exc:  # noqa: BLE001
                errors_seen.append(type(exc).__name__)

    if errors_seen:
        err = f"partial ({', '.join(errors_seen)})" if out else f"unavailable ({', '.join(errors_seen)})"
    else:
        err = None
    return out, err


def _collect_transcript_pointers(
    state_file: Path,
    state: dict[str, Any],
    *,
    query_text: str | None,
    now_utc: datetime | None = None,
    deadline: float | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    if not query_text:
        return [], None
    now = now_utc or datetime.now(timezone.utc)
    since_iso = (now - timedelta(days=TRANSCRIPT_LOOKBACK_DAYS)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    ns = transcript_graph.named_transcript_namespace(None)
    ctx_mgr = node.finishing_by(deadline) if deadline is not None else contextlib.nullcontext()
    try:
        with ctx_mgr:
            res = transcript_graph.search_transcripts(
                state_file,
                state,
                q=query_text,
                k=MAX_TRANSCRIPT_POINTERS + 2,
                rerank=True,
                ns=ns,
                since=since_iso,
                collapse="session",
                purpose="context",
            )
    except node.DeadlinePassed:
        return [], "unavailable (deadline_exceeded)"
    except Exception as exc:  # noqa: BLE001
        return [], f"unavailable ({type(exc).__name__})"

    pointers: list[dict[str, Any]] = []
    for hit in (res.get("hits") or []) if isinstance(res, dict) else []:
        if not isinstance(hit, dict):
            continue
        read_cmd = _clean_id(hit.get("read_cmd"), READ_CMD_MAX_CHARS) or transcript_graph._build_read_cmd(hit, ns=ns)
        ptr: dict[str, Any] = {
            "seat": _clean_text(hit.get("seat"), 64),
            "src": _clean_text(hit.get("src"), 64),
            "gen": hit.get("gen") if type(hit.get("gen")) is int else None,
            "batch": hit.get("batch") if type(hit.get("batch")) is int else None,
            "first_ord": hit.get("first_ord") if type(hit.get("first_ord")) is int else None,
            "last_ord": hit.get("last_ord") if type(hit.get("last_ord")) is int else None,
            "time_last": _clean_text(hit.get("time_last"), 64),
            "score": round(float(hit["score"]), 4)
            if isinstance(hit.get("score"), (int, float)) and not isinstance(hit.get("score"), bool)
            else None,
            "rerank_score": round(float(hit["rerank_score"]), 4)
            if isinstance(hit.get("rerank_score"), (int, float)) and not isinstance(hit.get("rerank_score"), bool)
            else None,
            "read_cmd": read_cmd,
        }
        pointers.append(ptr)
        if len(pointers) >= MAX_TRANSCRIPT_POINTERS:
            break
    return pointers, None


def build_conversation_context(
    state_file: Path,
    state: dict[str, Any],
    *,
    key: str,
    primary: dict[str, Any],
    mode: str = "read",
    namespace: str | None = None,
    deadline_s: float = DEFAULT_DEADLINE_S,
) -> dict[str, Any]:
    """Build the opt-in `conversation_context` dict for a `msg/` read or context response."""
    eff_ns = namespace or wire.MESH_NAMESPACE
    if eff_ns != wire.MESH_NAMESPACE or not isinstance(key, str) or not key.startswith("msg/"):
        return {"skipped": "conversation_context applies to shared/mesh-chat msg/ keys only"}

    deadline = _monotonic() + max(0.0, float(deadline_s))
    query_text, thread_id = _extract_anchor_query_and_thread(primary, mode=mode)
    context_body = primary if mode == "context" else None
    has_inline_edges = (
        isinstance(context_body, dict)
        and isinstance(context_body.get("related_edges"), dict)
        and isinstance(context_body["related_edges"].get("hits"), list)
    )
    if not has_inline_edges and _monotonic() >= deadline:
        edges, edge_err = [], "skipped (deadline_exceeded)"
    else:
        edges, edge_err = _collect_related_edges(
            state_file,
            state,
            key=key,
            anchor_thread=thread_id,
            context_body=context_body,
            deadline=deadline,
        )

    if (thread_id or query_text) and _monotonic() >= deadline:
        active_cards, card_err = [], "skipped (deadline_exceeded)"
    else:
        active_cards, card_err = _collect_active_cards(
            state_file,
            state,
            query_text=query_text,
            thread_id=thread_id,
            deadline=deadline,
        )

    if query_text and _monotonic() >= deadline:
        pointers, tx_err = [], "skipped (deadline_exceeded)"
    else:
        pointers, tx_err = _collect_transcript_pointers(
            state_file,
            state,
            query_text=query_text,
            deadline=deadline,
        )

    out: dict[str, Any] = {
        "untrusted": True,
        "trust_note": TRUST_NOTE,
        "related_edges": edges,
        "active_cards": active_cards,
        "transcript_pointers": pointers,
    }
    if pointers:
        out["transcript_read_hint"] = transcript_graph.READ_HINT
    errors: dict[str, str] = {}
    if edge_err:
        errors["related_edges"] = edge_err
    if card_err:
        errors["active_cards"] = card_err
    if tx_err:
        errors["transcript_pointers"] = tx_err
    if errors:
        out["errors"] = errors
    return out


__all__ = [
    "CARD_SUMMARY_MAX_CHARS",
    "DEFAULT_DEADLINE_S",
    "ENRICH_CARD_TYPES",
    "MAX_ACTIVE_CARDS",
    "MAX_RELATED_EDGES",
    "MAX_TRANSCRIPT_POINTERS",
    "TRUST_NOTE",
    "build_conversation_context",
]
