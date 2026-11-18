"""Cross-node transcript-context search, bounded item read, and fleet consensus over MeshyChat.

Built strictly over MeshyChat's signed HTTP/memory routes (no local disk transcript dependency,
and zero imports from `apps/`), so HQB, HQD, HZ, and laptop seats observe identical fleet state.

Security invariant (kxxx ruling, 26 Oct 2026):
`shared/transcript-context` stores session turns as written, live secrets included. Every output
that carries raw transcript text (`search_transcripts`, `read_transcript_items`, and their human
formatters) stamps an explicit untrusted sentinel (`UNTRUSTED_BANNER`, per-item
`[QUOTED UNTRUSTED TRANSCRIPT EVIDENCE <key>]` lines directly above each item's text, and
`"_untrusted_transcript_evidence": True`) that `sendguard.refuse_untrusted_transcript_paste`
blocks non-overridably on outbound `meshy_node send` and `edit`. That sentinel check is a
backstop against accidental pastes (whole outputs or single copied item blocks with their header),
not a barrier against a seat that deliberately strips the marker. `fleet_transcript_consensus`
omits raw transcript excerpts altogether, emitting pointer metadata (`seat`, `src`, `gen`, `batch`,
`first_ord`, `last_ord`, `read_cmd`) only.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
import os
from pathlib import Path
import re
import shlex
import time
from typing import Any

from . import cards, node, sendguard, wire

DEFAULT_TRANSCRIPT_NAMESPACE = "shared/transcript-context"
DEFAULT_CARD_NAMESPACE = cards.DEFAULT_NAMESPACE
UNTRUSTED_BANNER = sendguard.UNTRUSTED_TRANSCRIPT_BANNER

#: Printed with every non-empty search. Value test set A (vxxxxxxxxxx, kxxx): both misses chose between
#: two similar hits from a 200-character quote that did not hold the deciding fact; the full item did. Neutral: it
#: names no record and hints at no answer.
READ_HINT = ("Before relying on a hit, read its full item (the verify command); when several hits look alike, read "
             "each and cite the one whose text states the answer.")

COLLAPSE_MODES = ("chunk", "item", "session")
MAX_SEARCH_K = 50
MAX_READ_ORDINALS = 32
CONSENSUS_MAX_PAGES = 4
RECENT_ACTIVITY_MAX_PAGES = 2
MAX_SEAT_FRESHNESS_PROBES = 32
DEFAULT_STALE_INGEST_HOURS = 36.0
DEFAULT_STALE_TRANSCRIPT_HOURS = 6.0
DEFAULT_INDEX_PROBE_HOURS = 12.0
MAX_FLEET_INDEX_PROBE_HOURS = 24.0
DEFAULT_SEAT_PROBE_HOURS = 72.0
MAX_SEAT_PROBE_HOURS = 168.0
CONSENSUS_CALL_TIMEOUT_S = 20.0
VALID_UPLOAD_MODES = frozenset({"continuous", "one_off"})
_DEGRADABLE_CLIENT_STATUSES = frozenset({403, 404, 422, 429})
E_AUTHOR_UNVERIFIED = "E_AUTHOR_UNVERIFIED"
SEAT_PROBE_QUERY = "the a to of in status"
CONTEXT_SUMMARY_MAX_CHARS = 160

CONSENSUS_QUERY_TYPES = ("Context", "Commitment", "Question", "OpenItem", "Constraint")
CONSENSUS_ACCEPTED_TYPES = frozenset({"Context", "Commitment", "Question", "OpenItem", "Constraint", "Antibody"})

SEAT_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
SRC_RE = re.compile(r"^[0-9a-f]{32}$")
ITEM_KEY_RE = re.compile(r"^ctx/([a-z0-9][a-z0-9_.-]{0,63})/([0-9a-f]{32})/(\d{4})/(\d{6})/(\d{10})$")
MANIFEST_KEY_RE = re.compile(r"^ctxm/([a-z0-9][a-z0-9_.-]{0,63})/([0-9a-f]{32})/(\d{4})/(\d{6})$")

_LINE_UNSAFE_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]")
_CARD_HEADER_RE = re.compile(r"^[A-Za-z]+ \| owner: [^\n]+\n?")


def named_transcript_namespace(given: str | None) -> str:
    """Resolve the transcript-context namespace, refusing blank or control-character values."""
    if given is None:
        env_val = (os.environ.get("MESHY_CONTEXT_NAMESPACE") or "").strip()
        if env_val and not any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in env_val):
            return env_val
        return DEFAULT_TRANSCRIPT_NAMESPACE
    ns = str(given).strip()
    if not ns or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in ns):
        raise node.Refused("--namespace may not be blank or hold whitespace/control characters")
    return ns


def _clean_opt(raw: str | None, flag: str) -> str | None:
    if raw is None:
        return None
    val = str(raw).strip()
    if not val:
        return None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in val):
        raise node.Refused(f"{flag} may not hold control characters")
    return val


def _clean_seat(raw: str | None) -> str | None:
    val = _clean_opt(raw, "--seat")
    if val is not None and not SEAT_RE.fullmatch(val):
        raise node.Refused(f"--seat {cards.short(val, 64)!r} is not a valid lowercase seat label")
    return val


def _sanitize_multiline(text: str) -> list[str]:
    cleaned = _LINE_UNSAFE_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    return cleaned.split("\n")


def _build_read_cmd(hit: dict[str, Any], *, ns: str) -> str | None:
    seat = hit.get("seat")
    src = hit.get("src")
    gen = hit.get("gen")
    batch = hit.get("batch")
    first_ord = hit.get("first_ord")
    last_ord = hit.get("last_ord")
    if (
        isinstance(seat, str)
        and SEAT_RE.fullmatch(seat)
        and isinstance(src, str)
        and SRC_RE.fullmatch(src)
        and type(gen) is int
        and gen >= 1
        and type(batch) is int
        and batch >= 1
        and type(first_ord) is int
        and first_ord >= 0
        and type(last_ord) is int
        and last_ord >= first_ord
    ):
        capped_last = min(last_ord, first_ord + MAX_READ_ORDINALS - 1)
        ns_arg = f" --namespace {shlex.quote(ns)}"
        return (
            f"meshy_node transcript-read --seat {seat} --src {src} --gen {gen} "
            f"--batch {batch} --first-ord {first_ord} --last-ord {capped_last}{ns_arg}"
        )
    return None


def search_transcripts(
    state_path: Path,
    state: dict,
    *,
    q: str,
    k: int = 10,
    rerank: bool = True,
    ns: str | None = DEFAULT_TRANSCRIPT_NAMESPACE,
    seat: str | None = None,
    src: str | None = None,
    since: str | None = None,
    until: str | None = None,
    collapse: str = "chunk",
    purpose: str | None = None,
) -> dict:
    """Search `shared/transcript-context` via `GET /v1/mesh/search` with strict namespace verification."""
    ns_clean = named_transcript_namespace(ns)
    if not isinstance(q, str) or not q.strip():
        raise node.Refused("--q must be a non-empty query string")
    if type(k) is not int or not (1 <= k <= MAX_SEARCH_K):
        raise node.Refused(f"--k must be an integer within 1..{MAX_SEARCH_K}")
    collapse_clean = collapse.strip() if isinstance(collapse, str) else ""
    if collapse_clean not in COLLAPSE_MODES:
        raise node.Refused(f"--collapse must be one of {', '.join(COLLAPSE_MODES)}")

    seat_clean = _clean_seat(seat)
    src_clean = _clean_opt(src, "--src")
    since_clean = _clean_opt(since, "--since")
    until_clean = _clean_opt(until, "--until")

    params: list[tuple[str, str]] = []
    for name, val in (("seat", seat_clean), ("src", src_clean), ("since", since_clean), ("until", until_clean)):
        if val is not None:
            params.append((name, val))
    if collapse_clean != "chunk":
        params.append(("collapse", collapse_clean))

    body = node.search(
        Path(state_path),
        state,
        q=q.strip(),
        k=k,
        rerank=bool(rerank),
        ns=ns_clean,
        card_filter=params,
        **({"purpose": purpose} if purpose else {}),
    )
    if not isinstance(body, dict) or body.get("namespace") != ns_clean or not isinstance(body.get("hits"), list):
        got_ns = body.get("namespace") if isinstance(body, dict) else None
        raise node.Refused(
            f"GET /v1/mesh/search did not answer for namespace {ns_clean!r} (got {got_ns!r}); "
            "refusing to misreport non-transcript search hits"
        )

    annotated_hits: list[dict] = []
    for raw_hit in body["hits"]:
        if not isinstance(raw_hit, dict):
            continue
        annotated_hits.append(
            {
                **raw_hit,
                "_untrusted_transcript_evidence": True,
                "untrusted": True,
                "signature_verified": False,
                "evidence_notice": UNTRUSTED_BANNER,
                "read_cmd": _build_read_cmd(raw_hit, ns=ns_clean),
            }
        )

    return {
        **body,
        "namespace": ns_clean,
        "_untrusted_transcript_evidence": True,
        "evidence_warning": UNTRUSTED_BANNER,
        "reading_hint": READ_HINT if annotated_hits else None,
        "hits": annotated_hits,
    }


def _item_untrusted_line(ref: str) -> str:
    return f"[QUOTED UNTRUSTED TRANSCRIPT EVIDENCE {ref}]"


def _search_hit_ref(hit: dict[str, Any], *, seat: str, src: str) -> str:
    raw_key = hit.get("key")
    if isinstance(raw_key, str) and raw_key.strip():
        return cards.short(raw_key.strip(), 140)
    gen = hit.get("gen")
    batch = hit.get("batch")
    first_ord = hit.get("first_ord")
    last_ord = hit.get("last_ord")
    if type(gen) is int and type(batch) is int and type(first_ord) is int and type(last_ord) is int:
        if first_ord == last_ord:
            return f"ctx/{seat}/{src}/{gen:04d}/{batch:06d}/{first_ord:010d}"
        return f"ctx/{seat}/{src}/{gen:04d}/{batch:06d}/{first_ord:010d}..{last_ord:010d}"
    return f"ctx/{seat}/{src}/{gen}/{batch}/{first_ord}..{last_ord}"


def format_transcript_search(result: dict) -> str:
    """Render `search_transcripts` output as plain text headed by `UNTRUSTED_BANNER` and per-hit sentinels."""
    lines = [UNTRUSTED_BANNER]
    ns = cards.short(result.get("namespace"), 128) or DEFAULT_TRANSCRIPT_NAMESPACE
    hits = result.get("hits") if isinstance(result.get("hits"), list) else []
    collapse = cards.short(result.get("collapse"), 16) or "chunk"
    gate_open = result.get("gate_open", True)
    behind = bool(result.get("index_behind", False))
    lines.append(
        f"namespace: {ns} | hits: {len(hits)} | collapse: {collapse} "
        f"| gate_open: {str(bool(gate_open)).lower()} | index_behind: {str(behind).lower()}"
    )
    if gate_open is False and result.get("reason"):
        lines.append(f"  gate_reason: {cards.short(result.get('reason'), 160)}")
    if not hits:
        lines.append("  (no matching transcript chunks)")
        return "\n".join(lines)
    lines.append("  " + READ_HINT)
    for idx, hit in enumerate(hits, 1):
        seat = cards.short(hit.get("seat"), 64) or "?"
        src = cards.short(hit.get("src"), 32) or "?"
        gen = hit.get("gen")
        batch = hit.get("batch")
        first_ord = hit.get("first_ord")
        last_ord = hit.get("last_ord")
        score = hit.get("rerank_score") if hit.get("rerank_score") is not None else hit.get("score")
        t_first = cards.short(hit.get("time_first"), 32) or "?"
        lines.append(
            f"[{idx}] seat={seat} src={src[:12]} gen={gen} batch={batch} "
            f"ord={first_ord}..{last_ord} score={score} time={t_first}"
        )
        lines.append(f"    {_item_untrusted_line(_search_hit_ref(hit, seat=seat, src=src))}")
        if hit.get("text_available") is False or not isinstance(hit.get("text"), str):
            lines.append("    quote (unverified): <text unavailable>")
        else:
            lines.append(f"    quote (unverified): {cards.short(hit['text'], 200)!r}")
        if hit.get("read_cmd"):
            lines.append(f"    verify: {hit['read_cmd']}")
    return "\n".join(lines)


def _resolve_read_keys(
    *,
    key: str | None,
    seat: str | None,
    src: str | None,
    gen: int | None,
    batch: int | None,
    first_ord: int | None,
    last_ord: int | None,
) -> list[str]:
    has_coords = any(v is not None for v in (seat, src, gen, batch, first_ord, last_ord))
    if key is not None and has_coords:
        raise node.Refused("pass either --key or (--seat, --src, --gen, --batch, --first-ord [--last-ord]), not both")
    if key is not None:
        k = str(key).strip()
        m_item = ITEM_KEY_RE.fullmatch(k)
        m_man = MANIFEST_KEY_RE.fullmatch(k) if m_item is None else None
        match = m_item or m_man
        if match is None:
            raise node.Refused(
                f"--key {cards.short(k, 96)!r} must match ctx/<seat>/<32-hex>/<gen:04d>/<batch:06d>/<ord:010d> "
                "or ctxm/<seat>/<32-hex>/<gen:04d>/<batch:06d>"
            )
        if int(match.group(3)) < 1 or int(match.group(4)) < 1:
            raise node.Refused("--key gen and batch segments must be >= 1")
        return [k]

    if seat is None or src is None or gen is None or batch is None or first_ord is None:
        raise node.Refused(
            "transcript-read requires either --key or all of --seat, --src, --gen, --batch, --first-ord"
        )
    seat_clean = _clean_seat(seat)
    src_clean = _clean_opt(src, "--src")
    if src_clean is None or not SRC_RE.fullmatch(src_clean):
        raise node.Refused("--src must be 32 lowercase hex characters")
    if type(gen) is not int or not (1 <= gen <= 9999):
        raise node.Refused("--gen must be an integer within 1..9999")
    if type(batch) is not int or not (1 <= batch <= 999_999):
        raise node.Refused("--batch must be an integer within 1..999999")
    if type(first_ord) is not int or not (0 <= first_ord <= 9_999_999_999):
        raise node.Refused("--first-ord must be an integer >= 0")
    end_ord = first_ord if last_ord is None else last_ord
    if type(end_ord) is not int or not (0 <= end_ord <= 9_999_999_999):
        raise node.Refused("--last-ord must be an integer >= 0")
    if end_ord < first_ord:
        raise node.Refused("--last-ord must be >= --first-ord")
    span = end_ord - first_ord + 1
    if span > MAX_READ_ORDINALS:
        raise node.Refused(
            f"ordinal span {span} ({first_ord}..{end_ord}) exceeds max {MAX_READ_ORDINALS} items per invocation"
        )
    return [f"ctx/{seat_clean}/{src_clean}/{gen:04d}/{batch:06d}/{o:010d}" for o in range(first_ord, end_ord + 1)]


def read_transcript_items(
    state_path: Path,
    state: dict,
    *,
    key: str | None = None,
    seat: str | None = None,
    src: str | None = None,
    gen: int | None = None,
    batch: int | None = None,
    first_ord: int | None = None,
    last_ord: int | None = None,
    ns: str | None = DEFAULT_TRANSCRIPT_NAMESPACE,
    require_signature: bool = False,
) -> dict:
    """Read one exact `ctx/`/`ctxm/` key or a bounded (`<= 32`) contiguous ordinal span via `memory.read`."""
    ns_clean = named_transcript_namespace(ns)
    keys_to_read = _resolve_read_keys(
        key=key,
        seat=seat,
        src=src,
        gen=gen,
        batch=batch,
        first_ord=first_ord,
        last_ord=last_ord,
    )
    path_obj = Path(state_path)
    node.require_peer(path_obj, state)

    items: list[dict] = []
    for target_key in keys_to_read:
        raw = node.invoke(
            path_obj,
            state,
            skill="memory.read",
            args={"namespace": ns_clean, "key": target_key, "include_provenance": True},
        )
        status = raw.get("status")
        if status not in node.TERMINAL:
            raise node.ReadFailed(
                {"error": node.E_READ_POLL_TIMEOUT, "key": target_key, "call_id": raw.get("call_id"), "status": status}
            )
        if raw.get("result_signature_verified") is not True:
            raise node.ReadFailed(
                {
                    "error": node.E_RESULT_UNVERIFIED,
                    "key": target_key,
                    "call_id": raw.get("call_id"),
                    "status": status,
                    "result_signature_verified": False,
                    "detail": raw.get("result_signature_detail"),
                }
            )
        body = raw.get("result")
        if status != "completed" or not isinstance(body, dict) or body.get("ok", True) is False:
            if raw.get("error_code") == "E_UNKNOWN_KEY":
                items.append(
                    {
                        "_untrusted_transcript_evidence": True,
                        "key": target_key,
                        "status": "missing",
                        "author_signature_verified": False,
                        "signed_by": None,
                        "author_signature_error": "E_UNKNOWN_KEY: key not found",
                    }
                )
                continue
            raise node.ReadFailed(
                {
                    "error": node.E_READ_REFUSED,
                    "key": target_key,
                    "call_id": raw.get("call_id"),
                    "status": status,
                    "error_code": raw.get("error_code"),
                    "error_message": raw.get("error_message"),
                    "result_signature_verified": True,
                }
            )

        memo = body.get("memo")
        if (
            not isinstance(memo, dict)
            or memo.get("namespace") != ns_clean
            or memo.get("key") != target_key
            or memo.get("is_head") is not True
        ):
            raise node.ReadFailed(
                {
                    "error": node.E_RESULT_MISMATCH,
                    "key": target_key,
                    "call_id": raw.get("call_id"),
                    "detail": f"memo did not match head of {ns_clean}/{target_key}",
                }
            )

        verified, sig_err = wire.author_signature_ok(memo, lambda peer: node.author_keys(state["base_url"], peer))
        value = memo.get("value") if isinstance(memo.get("value"), dict) else {}
        entry: dict[str, Any] = {
            "_untrusted_transcript_evidence": True,
            "key": target_key,
            "status": "present",
            "version": memo.get("version"),
            "recorded_at": node._iso(memo.get("created_at")),
            "seat": value.get("seat"),
            "kind": value.get("kind"),
            "item": value.get("item"),
            "role": value.get("role"),
            "time": value.get("time"),
            "text": value.get("text"),
            "omitted_reason": value.get("omitted_reason"),
            "value": value,
            "author_signature_verified": bool(verified),
            "signed_by": memo.get("created_by_peer_id") if verified else None,
        }
        if not verified:
            entry["author_signature_error"] = sig_err
        items.append(entry)

    if require_signature:
        bad_rows = [
            row for row in items
            if row.get("status") != "present" or row.get("author_signature_verified") is not True
        ]
        if bad_rows:
            bad = bad_rows[0]
            raise node.ReadFailed(
                {
                    "error": E_AUTHOR_UNVERIFIED,
                    "key": bad["key"],
                    "status": bad["status"],
                    "author_signature_verified": False,
                    "detail": bad.get("author_signature_error") or "author signature not verified",
                    "unverified_keys": [r["key"] for r in bad_rows],
                }
            )

    present_count = sum(1 for row in items if row["status"] == "present")
    missing_count = sum(1 for row in items if row["status"] == "missing")
    return {
        "namespace": ns_clean,
        "_untrusted_transcript_evidence": True,
        "evidence_warning": UNTRUSTED_BANNER,
        "requested_keys": keys_to_read,
        "present_count": present_count,
        "missing_count": missing_count,
        "items": items,
    }


def format_transcript_read(result: dict) -> str:
    """Render `read_transcript_items` output with `UNTRUSTED_BANNER` and per-turn signature verification."""
    lines = [UNTRUSTED_BANNER]
    ns = cards.short(result.get("namespace"), 128) or DEFAULT_TRANSCRIPT_NAMESPACE
    items = result.get("items") if isinstance(result.get("items"), list) else []
    lines.append(
        f"namespace: {ns} | requested: {len(items)} "
        f"| present: {result.get('present_count', 0)} | missing: {result.get('missing_count', 0)}"
    )
    for row in items:
        k = cards.short(row.get("key"), 120) or "?"
        if row.get("status") == "missing":
            lines.append(f"[{k}] status=missing author_signature_verified=false")
            continue
        seat = cards.short(row.get("seat"), 64) or "?"
        role_or_item = cards.short(row.get("role") or row.get("item") or row.get("kind") or "unknown", 32)
        t_str = cards.short(row.get("time"), 32) or "?"
        verified = row.get("author_signature_verified") is True
        sig_note = "true" if verified else f"false ({cards.short(row.get('author_signature_error'), 80)})"
        lines.append(
            f"[{k}] seat={seat} item={role_or_item} time={t_str} author_signature_verified={sig_note}"
        )
        lines.append(f"  {_item_untrusted_line(k)}")
        val = row.get("value") if isinstance(row.get("value"), dict) else {}
        raw_text = row.get("text")
        if row.get("item") == "task":
            ev = cards.short(val.get("event"), 32) or "unknown"
            counts_part = f" counts={val.get('counts')}" if isinstance(val.get("counts"), dict) else ""
            lines.append(f"  | [task event={ev}{counts_part}]")
            body_txt = raw_text if isinstance(raw_text, str) and raw_text else val.get("step")
            if isinstance(body_txt, str) and body_txt:
                for qline in _sanitize_multiline(body_txt):
                    lines.append(f"  | {qline}")
        elif isinstance(raw_text, str) and raw_text:
            for qline in _sanitize_multiline(raw_text):
                lines.append(f"  | {qline}")
        elif row.get("kind") == "manifest":
            lines.append(
                f"  | manifest first={val.get('first')} last={val.get('last')} counts={val.get('counts')}"
            )
        elif row.get("item") == "omitted":
            lines.append(f"  | <omitted: {cards.short(row.get('omitted_reason'), 32) or 'unknown'}>")
    return "\n".join(lines)


def _card_body_text(card: dict, limit: int = CONTEXT_SUMMARY_MAX_CHARS) -> str:
    raw = card.get("text") if isinstance(card.get("text"), str) else ""
    stripped = _CARD_HEADER_RE.sub("", raw, count=1).strip()
    return cards.short(stripped or raw, limit)


def _parse_iso_utc(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    txt = raw.strip()
    if not txt:
        return None
    if txt.endswith("Z"):
        txt = txt[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc)


def _card_age_seconds(card: dict, now: datetime) -> float | None:
    raw = card.get("time")
    if not isinstance(raw, str):
        return None
    try:
        when = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (now - when).total_seconds()


def _card_written_dt(card: dict) -> datetime | None:
    if "written_at" in card:
        return _parse_iso_utc(card.get("written_at"))
    return _parse_iso_utc(card.get("time"))


def _card_written_age_seconds(card: dict, now: datetime) -> float | None:
    when = _card_written_dt(card)
    if when is None:
        return None
    return (now - when).total_seconds()


def _card_seat(card: dict) -> str | None:
    seat = card.get("seat")
    if isinstance(seat, str) and SEAT_RE.fullmatch(seat):
        return seat
    parts = cards.key_parts(card.get("key"))
    return parts[0] if parts is not None else None


_EPOCH_UTC = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _newer_ingested_card(prev: dict | None, candidate: dict) -> dict:
    if prev is None:
        return candidate
    cand_dt = _card_written_dt(candidate)
    prev_dt = _card_written_dt(prev)
    cur_sort = (
        cand_dt is not None,
        cand_dt if cand_dt is not None else _EPOCH_UTC,
        str(candidate.get("id") or ""),
        str(candidate.get("key") or ""),
    )
    prev_sort = (
        prev_dt is not None,
        prev_dt if prev_dt is not None else _EPOCH_UTC,
        str(prev.get("id") or ""),
        str(prev.get("key") or ""),
    )
    return candidate if cur_sort > prev_sort else prev


def _degradable_http_status(status: int) -> bool:
    return status in _DEGRADABLE_CLIENT_STATUSES or status >= 500


def _clean_error_text(raw: Any, fallback: str, *, limit: int = 160) -> str:
    cleaned = cards.short(raw, limit).replace("\u2014", "-").replace("\u2013", "-")
    return cleaned or fallback


def _server_error_text(exc: node.ServerSaid) -> str:
    if isinstance(exc.body, dict):
        msg = exc.body.get("message") or exc.body.get("error") or exc.body.get("code")
    else:
        msg = exc.body
    return _clean_error_text(msg, f"HTTP {exc.status}")


def _transport_error_text(exc: BaseException) -> str:
    detail = _clean_error_text(str(exc), "")
    name = type(exc).__name__
    return _clean_error_text(f"{name}: {detail}" if detail else name, name)


def _consensus_call(fn, *args, **kwargs):
    with node.finishing_by(time.monotonic() + CONSENSUS_CALL_TIMEOUT_S):
        return fn(*args, **kwargs)


def _seat_probe_window_s(ingest_stale_threshold_s: float, transcript_stale_threshold_s: float) -> float:
    return min(
        max(DEFAULT_SEAT_PROBE_HOURS * 3600.0, ingest_stale_threshold_s, transcript_stale_threshold_s * 2.0),
        MAX_SEAT_PROBE_HOURS * 3600.0,
    )


def _seat_probe_since_iso(
    current: datetime, *, ingest_stale_threshold_s: float, transcript_stale_threshold_s: float
) -> str:
    window_s = _seat_probe_window_s(ingest_stale_threshold_s, transcript_stale_threshold_s)
    return cards.now_time(current - timedelta(seconds=window_s))


def _index_probe_since_iso(
    current: datetime,
    *,
    seat_filter: str | None,
    ingest_stale_threshold_s: float,
    transcript_stale_threshold_s: float,
) -> str:
    if seat_filter is not None:
        return _seat_probe_since_iso(
            current,
            ingest_stale_threshold_s=ingest_stale_threshold_s,
            transcript_stale_threshold_s=transcript_stale_threshold_s,
        )
    window_s = min(
        max(DEFAULT_INDEX_PROBE_HOURS * 3600.0, transcript_stale_threshold_s),
        MAX_FLEET_INDEX_PROBE_HOURS * 3600.0,
    )
    return cards.now_time(current - timedelta(seconds=window_s))


def _fetch_consensus_cards(
    state_path: Path,
    state: dict,
    *,
    card_ns: str,
    seat_filter: str | None,
) -> tuple[list[dict], set[str], dict[str, dict], bool, int]:
    """Bounded `GET /v1/cards` walk (`<= CONSENSUS_MAX_PAGES` pages of 200) verifying `namespace == card_ns`."""
    raw_rows: list[dict] = []
    raw_seats: set[str] = set()
    latest_by_seat: dict[str, dict] = {}
    superseded_keys: set[str] = set()
    seen_cursors: set[str] = set()
    after: str | None = None
    truncated = False

    for _ in range(CONSENSUS_MAX_PAGES):
        page = _consensus_call(
            node.cards,
            Path(state_path),
            state,
            cards.list_query(
                types=CONSENSUS_QUERY_TYPES,
                seat=seat_filter,
                status="active",
                limit=cards.THREAD_LIMIT,
                after=after,
            ),
        )
        if not isinstance(page, dict) or page.get("namespace") != card_ns or not isinstance(page.get("cards"), list):
            got_ns = page.get("namespace") if isinstance(page, dict) else None
            raise node.Refused(
                f"GET /v1/cards did not answer with a card list for namespace {card_ns!r} (got {got_ns!r})"
            )
        nxt = page.get("next")
        if nxt is not None and (not isinstance(nxt, str) or not nxt.strip()):
            raise node.Refused("GET /v1/cards answered with an invalid next cursor")
        batch = [c for c in page["cards"] if isinstance(c, dict)]
        raw_rows.extend(batch)
        for c in batch:
            c_seat = _card_seat(c)
            if c_seat is not None and (seat_filter is None or c_seat == seat_filter):
                raw_seats.add(c_seat)
                latest_by_seat[c_seat] = _newer_ingested_card(latest_by_seat.get(c_seat), c)
            sup = c.get("supersedes")
            if c_seat is not None and isinstance(sup, list):
                prefix = f"card/{c_seat}/"
                for sk in sup:
                    if isinstance(sk, str) and sk.startswith(prefix):
                        superseded_keys.add(sk)
        if not nxt:
            after = None
            break
        if nxt in seen_cursors:
            truncated = True
            after = None
            break
        seen_cursors.add(nxt)
        after = nxt
    else:
        truncated = True

    active_heads: list[dict] = []
    for c in raw_rows:
        c_seat = _card_seat(c)
        if c_seat is None:
            continue
        if seat_filter is not None and c_seat != seat_filter:
            continue
        if c.get("type") not in CONSENSUS_ACCEPTED_TYPES:
            continue
        if c.get("status", "active") != "active":
            continue
        if c.get("superseded_by") or (type(c.get("superseded_by_count")) is int and c["superseded_by_count"] > 0):
            continue
        if c.get("key") in superseded_keys:
            continue
        active_heads.append(c)
    return active_heads, raw_seats, latest_by_seat, truncated, len(raw_rows)


def _fetch_recent_card_activity(
    state_path: Path,
    state: dict,
    *,
    card_ns: str,
    seat_filter: str | None,
    recorded_since_iso: str,
) -> tuple[dict[str, dict], bool]:
    """Bounded `status=any` activity check measuring server ingestion (`written_at`) freshness.

    Walks `recorded_since=recorded_since_iso` first so historical backfills ingested recently
    (old event `time`, fresh server `written_at`) are never buried behind `ORDER BY time DESC`,
    then walks general `status=any` pages to discover seats whose last ingestion is stale.
    Returns `(latest_by_seat, recent_truncated)` so `fleet_transcript_consensus` can verify
    individual candidate stale seats when the `recorded_since` sweep hits its page cap.
    """
    latest_by_seat: dict[str, dict] = {}
    recent_truncated = False
    sweeps: tuple[tuple[str | None, int], ...] = (
        (recorded_since_iso, RECENT_ACTIVITY_MAX_PAGES),
        (None, 1),
    )

    for rec_since, max_pages in sweeps:
        seen_cursors: set[str] = set()
        after: str | None = None
        page_limit = cards.THREAD_LIMIT
        for page_idx in range(max_pages):
            page = _consensus_call(
                node.cards,
                Path(state_path),
                state,
                cards.list_query(
                    seat=seat_filter,
                    status="any",
                    recorded_since=rec_since,
                    limit=page_limit,
                    after=after,
                ),
            )
            if not isinstance(page, dict) or page.get("namespace") != card_ns or not isinstance(page.get("cards"), list):
                got_ns = page.get("namespace") if isinstance(page, dict) else None
                raise node.Refused(
                    f"GET /v1/cards did not answer with a card list for namespace {card_ns!r} (got {got_ns!r})"
                )
            nxt = page.get("next")
            if nxt is not None and (not isinstance(nxt, str) or not nxt.strip()):
                raise node.Refused("GET /v1/cards answered with an invalid next cursor")
            for c in page["cards"]:
                if not isinstance(c, dict):
                    continue
                c_seat = _card_seat(c)
                if c_seat is not None and (seat_filter is None or c_seat == seat_filter):
                    latest_by_seat[c_seat] = _newer_ingested_card(latest_by_seat.get(c_seat), c)
            if not nxt or nxt in seen_cursors:
                break
            if rec_since is not None and page_idx + 1 >= max_pages:
                recent_truncated = True
            seen_cursors.add(nxt)
            after = nxt
    return latest_by_seat, recent_truncated


def _probe_seat_recent_ingestion(
    state_path: Path,
    state: dict,
    *,
    card_ns: str,
    seat_name: str,
    recorded_since_iso: str,
) -> dict | None:
    """Exact 1-row `seat=<seat_name>&status=any&recorded_since=<cutoff>&limit=1` probe.

    Because every card matching `recorded_since` was ingested at or after `<cutoff>`, any single
    returned row proves fresh ingestion regardless of `ORDER BY time DESC`, while `[]` proves zero
    cards were ingested since `<cutoff>`.
    """
    page = _consensus_call(
        node.cards,
        Path(state_path),
        state,
        cards.list_query(
            seat=seat_name,
            status="any",
            recorded_since=recorded_since_iso,
            limit=1,
        ),
    )
    if not isinstance(page, dict) or page.get("namespace") != card_ns or not isinstance(page.get("cards"), list):
        got_ns = page.get("namespace") if isinstance(page, dict) else None
        raise node.Refused(
            f"GET /v1/cards did not answer with a card list for namespace {card_ns!r} (got {got_ns!r})"
        )
    for c in page["cards"]:
        if isinstance(c, dict) and _card_seat(c) == seat_name:
            return c
    return None


def _condense_hit(h: dict[str, Any]) -> dict[str, Any]:
    return {
        "seat": h.get("seat"),
        "src": h.get("src"),
        "gen": h.get("gen"),
        "batch": h.get("batch"),
        "first_ord": h.get("first_ord"),
        "last_ord": h.get("last_ord"),
        "time_last": h.get("time_last"),
        "score": h.get("rerank_score") if h.get("rerank_score") is not None else h.get("score"),
        "read_cmd": h.get("read_cmd"),
    }


def _hit_recency_key(h: dict[str, Any]) -> tuple[bool, datetime, int, int, int]:
    dt = _parse_iso_utc(h.get("time_last"))
    gen = h.get("gen") if type(h.get("gen")) is int else -1
    batch = h.get("batch") if type(h.get("batch")) is int else -1
    last_ord = h.get("last_ord") if type(h.get("last_ord")) is int else -1
    return (dt is not None, dt if dt is not None else _EPOCH_UTC, gen, batch, last_ord)


def _probe_transcript_index(
    state_path: Path,
    state: dict,
    *,
    transcript_ns: str,
    query: str | None,
    seat_filter: str | None,
    since_iso: str | None = None,
) -> dict:
    probe_q = query if query is not None else SEAT_PROBE_QUERY
    probe_k = 10 if query is not None else 20
    eff_since = since_iso or _index_probe_since_iso(
        datetime.now(timezone.utc), seat_filter=seat_filter,
        ingest_stale_threshold_s=DEFAULT_STALE_INGEST_HOURS * 3600.0,
        transcript_stale_threshold_s=DEFAULT_STALE_TRANSCRIPT_HOURS * 3600.0,
    )
    degraded = {"available": False, "namespace": transcript_ns, "since": eff_since, "hits": []}
    try:
        res = _consensus_call(
            search_transcripts, state_path, state, q=probe_q, k=probe_k, rerank=bool(query),
            ns=transcript_ns, seat=seat_filter, since=eff_since, collapse="session",
            purpose=None if query is not None else "consensus",
        )
    except node.ServerSaid as exc:
        if _degradable_http_status(exc.status):
            return {**degraded, "http_status": exc.status, "error": _server_error_text(exc)}
        raise
    except (TimeoutError, OSError) as exc:
        return {**degraded, "http_status": None, "error": _transport_error_text(exc)}
    except node.Refused as exc:
        return {**degraded, "http_status": None, "error": _clean_error_text(str(exc), "refused")}

    condensed_hits = [_condense_hit(h) for h in (res.get("hits") or ()) if isinstance(h, dict)]
    return {
        "available": True,
        "namespace": transcript_ns,
        "since": eff_since,
        "gate_open": bool(res.get("gate_open", True)),
        "index_behind": bool(res.get("index_behind", False)),
        "truncated": bool(res.get("truncated", False)),
        "sources_with_unindexed_newer_gen": int(res.get("sources_with_unindexed_newer_gen") or 0),
        "coverage": res.get("coverage"),
        "method": res.get("method"),
        "reason": res.get("reason"),
        "hits": condensed_hits,
    }


def _probe_seat_latest_transcript(
    state_path: Path, state: dict, *, transcript_ns: str, seat_name: str, since_iso: str | None = None
) -> tuple[int, dict | None, bool, dict | None]:
    eff_since = since_iso or _seat_probe_since_iso(
        datetime.now(timezone.utc),
        ingest_stale_threshold_s=DEFAULT_STALE_INGEST_HOURS * 3600.0,
        transcript_stale_threshold_s=DEFAULT_STALE_TRANSCRIPT_HOURS * 3600.0,
    )
    probe_err: dict | None = None

    def _chunk_search(step_since_iso: str) -> dict | None:
        nonlocal probe_err
        try:
            return _consensus_call(
                search_transcripts, state_path, state, q=SEAT_PROBE_QUERY, k=20, rerank=False,
                ns=transcript_ns, seat=seat_name, since=step_since_iso, collapse="chunk",
                purpose="consensus",
            )
        except node.ServerSaid as exc:
            if _degradable_http_status(exc.status):
                probe_err = {"http_status": exc.status, "error": _server_error_text(exc)}
                return None
            raise
        except (TimeoutError, OSError) as exc:
            probe_err = {"http_status": None, "error": _transport_error_text(exc)}
            return None
        except node.Refused as exc:
            probe_err = {"http_status": None, "error": _clean_error_text(str(exc), "refused")}
            return None

    res = _chunk_search(eff_since)
    if res is None:
        return 0, None, False, probe_err
    raw_all = [h for h in (res.get("hits") or ()) if isinstance(h, dict)]
    raw_hits = [h for h in raw_all if h.get("seat") == seat_name]
    res_cov = res.get("coverage") if isinstance(res.get("coverage"), dict) else {}
    subset_chunks, _ = _coverage_summary(res_cov, len(raw_hits))
    if not raw_hits:
        return (subset_chunks or 0), None, (subset_chunks in (None, 0) and not res_cov.get("subset_at_least")), None

    total_chunks = subset_chunks if subset_chunks is not None else len(raw_hits)
    best = max(raw_hits, key=_hit_recency_key)
    best_dt = _parse_iso_utc(best.get("time_last"))
    complete = (
        best_dt is not None
        and subset_chunks is not None
        and len(raw_all) >= subset_chunks
        and not res_cov.get("subset_at_least")
        and not res_cov.get("truncated")
    )
    for _ in range(0 if complete else 4):
        if best_dt is None:
            break
        newer_res = _chunk_search(cards.now_time(best_dt + timedelta(milliseconds=1)))
        if newer_res is None:
            complete = False
            break
        newer_all = [h for h in (newer_res.get("hits") or ()) if isinstance(h, dict)]
        newer_hits = [h for h in newer_all if h.get("seat") == seat_name]
        newer_sub, _ = _coverage_summary(newer_res.get("coverage"), len(newer_hits))
        newer_cov = newer_res.get("coverage") if isinstance(newer_res.get("coverage"), dict) else {}
        if not newer_hits:
            complete = (newer_sub in (None, 0)) and not newer_cov.get("subset_at_least")
            break
        cand = max(newer_hits, key=_hit_recency_key)
        cand_dt = _parse_iso_utc(cand.get("time_last"))
        if cand_dt is None or cand_dt <= best_dt:
            complete = len(newer_all) < 20 and cand_dt is not None and not newer_cov.get("truncated")
            break
        best, best_dt = cand, cand_dt
    return total_chunks, _condense_hit(best), complete, probe_err


def fleet_transcript_consensus(
    state_path: Path,
    state: dict,
    *,
    card_ns: str | None = DEFAULT_CARD_NAMESPACE,
    transcript_ns: str | None = DEFAULT_TRANSCRIPT_NAMESPACE,
    query: str | None = None,
    seat_filter: str | None = None,
    stale_context_hours: float = 6.0,
    stale_ingest_hours: float = DEFAULT_STALE_INGEST_HOURS,
    stale_transcript_hours: float = DEFAULT_STALE_TRANSCRIPT_HOURS,
    upload_modes: dict[str, str] | None = None,
    now: datetime | None = None,
) -> dict:
    """Build the Shared Fleet Transcript & Diagnostic Consensus from MeshyChat graph stores."""
    c_ns = cards.named_namespace(card_ns)
    t_ns = named_transcript_namespace(transcript_ns)
    seat_clean = _clean_seat(seat_filter)
    query_clean = _clean_opt(query, "--q")
    clean_modes: dict[str, str] = {}
    for raw_s, raw_m in (upload_modes or {}).items():
        try:
            ms = _clean_seat(raw_s)
        except node.Refused as exc:
            raise node.Refused("--upload-mode must be <seat>=continuous or <seat>=one_off") from exc
        mm = str(raw_m or "").strip().lower()
        if ms is None or mm not in VALID_UPLOAD_MODES:
            raise node.Refused("--upload-mode must be <seat>=continuous or <seat>=one_off")
        clean_modes[ms] = mm
    for val, flag in (
        (stale_context_hours, "--stale-hours"),
        (stale_ingest_hours, "--stale-ingest-hours"),
        (stale_transcript_hours, "--stale-transcript-hours"),
    ):
        if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val) or val <= 0:
            raise node.Refused(f"{flag} must be a finite positive number")

    current = now if now is not None else datetime.now(timezone.utc)
    stale_threshold_s = float(stale_context_hours) * 3600.0
    ingest_stale_threshold_s = float(stale_ingest_hours) * 3600.0
    transcript_stale_threshold_s = float(stale_transcript_hours) * 3600.0
    seat_probe_window_s = _seat_probe_window_s(ingest_stale_threshold_s, transcript_stale_threshold_s)
    index_since_iso = _index_probe_since_iso(
        current, seat_filter=seat_clean,
        ingest_stale_threshold_s=ingest_stale_threshold_s,
        transcript_stale_threshold_s=transcript_stale_threshold_s,
    )
    seat_since_iso = cards.now_time(current - timedelta(seconds=seat_probe_window_s))

    cards_available, recent_truncated, recent_failed = True, False, False
    cards_http_status: int | None = None
    cards_error: str | None = None
    head_cards: list[dict] = []
    raw_seats: set[str] = set()
    latest_by_seat: dict[str, dict] = {}
    cards_truncated, cards_checked = False, 0

    try:
        head_cards, raw_seats, latest_by_seat, cards_truncated, cards_checked = _fetch_consensus_cards(
            state_path, state, card_ns=c_ns, seat_filter=seat_clean
        )
    except node.ServerSaid as exc:
        if not _degradable_http_status(exc.status):
            raise
        cards_available, cards_http_status, cards_error = False, exc.status, _server_error_text(exc)
    except (TimeoutError, OSError) as exc:
        cards_available, cards_error = False, _transport_error_text(exc)

    recorded_since_iso = cards.now_time(current - timedelta(seconds=ingest_stale_threshold_s))
    if cards_available:
        try:
            recent_by_seat, recent_truncated = _fetch_recent_card_activity(
                state_path, state, card_ns=c_ns, seat_filter=seat_clean, recorded_since_iso=recorded_since_iso
            )
            for s_name, c_row in recent_by_seat.items():
                latest_by_seat[s_name] = _newer_ingested_card(latest_by_seat.get(s_name), c_row)
        except node.ServerSaid as exc:
            if not _degradable_http_status(exc.status):
                raise
            cards_available, recent_failed = False, True
            cards_http_status, cards_error = exc.status, _server_error_text(exc)
        except (TimeoutError, OSError) as exc:
            cards_available, recent_failed, cards_error = False, True, _transport_error_text(exc)

    transcript_index = _probe_transcript_index(
        state_path, state, transcript_ns=t_ns, query=query_clean, seat_filter=seat_clean, since_iso=index_since_iso
    )

    ctx_by_seat: dict[str, dict] = {}
    commits_by_seat: dict[str, list[dict]] = {}
    questions_by_seat: dict[str, list[dict]] = {}
    open_items_by_seat: dict[str, list[dict]] = {}
    antibodies_list: list[dict] = []
    discovered_seats: set[str] = set(raw_seats) | set(latest_by_seat)

    for c in head_cards:
        s = _card_seat(c)
        if s is None:
            continue
        ctype = c.get("type")
        if ctype == "Context":
            discovered_seats.add(s)
            prev = ctx_by_seat.get(s)
            cur_sort = (str(c.get("time") or ""), str(c.get("id") or ""))
            prev_sort = (str(prev.get("time") or ""), str(prev.get("id") or "")) if prev is not None else ("", "")
            if prev is None or cur_sort > prev_sort:
                ctx_by_seat[s] = c
        elif ctype == "Commitment":
            discovered_seats.add(s)
            commits_by_seat.setdefault(s, []).append(
                {
                    "key": cards.short(c.get("key"), 80),
                    "thread": cards.short(c.get("thread_id"), 128) or None,
                    "what": _card_body_text(c, 140),
                    "time": cards.short(c.get("time"), 32),
                }
            )
        elif ctype == "Question":
            age_s = _card_age_seconds(c, current)
            if age_s is not None and age_s < cards.QUESTION_MAX_AGE_S:
                discovered_seats.add(s)
                q_text = c.get("question") if isinstance(c.get("question"), str) and c.get("question").strip() else None
                questions_by_seat.setdefault(s, []).append(
                    {
                        "key": cards.short(c.get("key"), 80),
                        "thread": cards.short(c.get("thread_id"), 128) or None,
                        "question": cards.short(q_text, 140) if q_text else _card_body_text(c, 140),
                        "time": cards.short(c.get("time"), 32),
                        "age_hours": round(max(0.0, age_s) / 3600.0, 2),
                    }
                )
        elif ctype == "OpenItem":
            discovered_seats.add(s)
            open_items_by_seat.setdefault(s, []).append(
                {
                    "key": cards.short(c.get("key"), 80),
                    "what": _card_body_text(c, 140),
                    "time": cards.short(c.get("time"), 32),
                }
            )
        elif ctype in ("Constraint", "Antibody"):
            discovered_seats.add(s)
            antibodies_list.append(
                {
                    "key": cards.short(c.get("key"), 80),
                    "seat": s,
                    "type": ctype,
                    "time": cards.short(c.get("time"), 32),
                    "summary": _card_body_text(c, 140),
                }
            )

    tx_hits_by_seat: dict[str, list[dict]] = {}
    for h in transcript_index.get("hits") or ():
        h_seat = h.get("seat")
        if isinstance(h_seat, str) and SEAT_RE.fullmatch(h_seat):
            if seat_clean is None or h_seat == seat_clean:
                discovered_seats.add(h_seat)
                tx_hits_by_seat.setdefault(h_seat, []).append(h)

    antibodies_list.sort(key=lambda item: (item.get("time") or "", item.get("key") or ""), reverse=True)

    seat_entries: list[dict] = []
    alerts: list[dict] = []
    freshness_probes_used = 0
    tx_probes_used = 0
    can_probe_tx = bool(transcript_index.get("available")) and transcript_index.get("gate_open") is not False
    prefetched_seat_tx: dict[str, tuple[int, dict | None, bool]] = {}
    if (
        seat_clean is not None
        and seat_clean not in discovered_seats
        and can_probe_tx
        and tx_probes_used < MAX_SEAT_FRESHNESS_PROBES
    ):
        tx_probes_used += 1
        p_cnt, p_lat, p_comp, p_err = _probe_seat_latest_transcript(
            state_path,
            state,
            transcript_ns=t_ns,
            seat_name=seat_clean,
            since_iso=seat_since_iso,
        )
        if p_err is not None:
            can_probe_tx = False
            transcript_index["available"] = False
            transcript_index["http_status"] = p_err["http_status"]
            transcript_index["error"] = p_err["error"]
        if p_lat is not None or clean_modes.get(seat_clean) == "continuous":
            discovered_seats.add(seat_clean)
            prefetched_seat_tx[seat_clean] = (p_cnt, p_lat, False if p_err is not None else p_comp)
    elif seat_clean is not None and seat_clean not in discovered_seats and clean_modes.get(seat_clean) == "continuous":
        discovered_seats.add(seat_clean)

    for s in sorted(discovered_seats):
        ctx_card = ctx_by_seat.get(s)
        latest_context: dict | None = None
        if ctx_card is not None:
            age_s = _card_age_seconds(ctx_card, current)
            age_hours = round(max(0.0, age_s) / 3600.0, 2) if age_s is not None else None
            is_stale = age_s is None or age_s > stale_threshold_s
            latest_context = {
                "key": cards.short(ctx_card.get("key"), 80),
                "time": cards.short(ctx_card.get("time"), 32),
                "age_hours": age_hours,
                "stale": is_stale,
                "summary": _card_body_text(ctx_card, CONTEXT_SUMMARY_MAX_CHARS),
            }

        newest_card = latest_by_seat.get(s)
        latest_card: dict | None = None
        c_age_s: float | None = None
        if newest_card is not None:
            c_age_s = _card_written_age_seconds(newest_card, current)
            c_age_hours = round(max(0.0, c_age_s) / 3600.0, 2) if c_age_s is not None else None
            c_stale: bool | None = c_age_s is None or c_age_s > ingest_stale_threshold_s
            if c_stale and recent_failed:
                c_stale = None
            if c_stale and recent_truncated:
                if cards_available and freshness_probes_used < MAX_SEAT_FRESHNESS_PROBES:
                    freshness_probes_used += 1
                    try:
                        probed = _probe_seat_recent_ingestion(
                            state_path, state, card_ns=c_ns, seat_name=s, recorded_since_iso=recorded_since_iso
                        )
                    except node.ServerSaid as exc:
                        if not _degradable_http_status(exc.status):
                            raise
                        cards_available, recent_failed = False, True
                        cards_http_status, cards_error, c_stale = exc.status, _server_error_text(exc), None
                    except (TimeoutError, OSError) as exc:
                        cards_available, recent_failed = False, True
                        cards_error, c_stale = _transport_error_text(exc), None
                    else:
                        if probed is not None:
                            newest_card = _newer_ingested_card(newest_card, probed)
                            latest_by_seat[s] = newest_card
                            c_age_s = _card_written_age_seconds(newest_card, current)
                            c_age_hours = round(max(0.0, c_age_s) / 3600.0, 2) if c_age_s is not None else None
                            c_stale = False
                else:
                    c_stale = None
            raw_written = newest_card.get("written_at") if "written_at" in newest_card else newest_card.get("time")
            latest_card = {
                "key": cards.short(newest_card.get("key"), 80),
                "type": cards.short(newest_card.get("type"), 32),
                "written_at": cards.short(raw_written, 40),
                "time": cards.short(newest_card.get("time"), 32),
                "age_hours": c_age_hours,
                "stale": c_stale,
            }

        s_commits = sorted(commits_by_seat.get(s, []), key=lambda x: x.get("time") or "", reverse=True)
        s_questions = sorted(questions_by_seat.get(s, []), key=lambda x: x.get("time") or "", reverse=True)
        s_open_items = sorted(open_items_by_seat.get(s, []), key=lambda x: x.get("time") or "", reverse=True)
        s_tx_hits = tx_hits_by_seat.get(s, [])
        is_continuous = clean_modes.get(s) == "continuous"
        seat_dormant = (
            seat_clean != s
            and not is_continuous
            and not s_tx_hits
            and newest_card is not None
            and c_age_s is not None
            and c_age_s > seat_probe_window_s
        )

        tx_chunk_count = 0
        tx_latest: dict | None = None
        tx_freshness_complete = False
        if s in prefetched_seat_tx:
            tx_chunk_count, tx_latest, tx_freshness_complete = prefetched_seat_tx[s]
        elif can_probe_tx and not seat_dormant and tx_probes_used < MAX_SEAT_FRESHNESS_PROBES:
            tx_probes_used += 1
            tx_chunk_count, tx_latest, tx_freshness_complete, tx_probe_err = _probe_seat_latest_transcript(
                state_path, state, transcript_ns=t_ns, seat_name=s, since_iso=seat_since_iso
            )
            if tx_probe_err is not None:
                can_probe_tx = False
                tx_freshness_complete = False
                transcript_index["available"] = False
                transcript_index["http_status"] = tx_probe_err["http_status"]
                transcript_index["error"] = tx_probe_err["error"]
        if s_tx_hits:
            tx_latest = max([tx_latest, *s_tx_hits] if tx_latest is not None else s_tx_hits, key=_hit_recency_key)
            tx_chunk_count = max(tx_chunk_count, len(s_tx_hits))

        latest_transcript: dict | None = None
        tx_stale: bool | None = False
        if tx_latest is not None:
            tx_dt = _parse_iso_utc(tx_latest.get("time_last"))
            tx_age_s = (current - tx_dt).total_seconds() if tx_dt is not None else None
            tx_age_hours = round(max(0.0, tx_age_s) / 3600.0, 2) if tx_age_s is not None else None
            card_dt = _card_written_dt(newest_card) if newest_card is not None else None
            lag_vs_card_s = (card_dt - tx_dt).total_seconds() if (card_dt is not None and tx_dt is not None) else None
            lag_vs_card_hours = round(max(0.0, lag_vs_card_s) / 3600.0, 2) if lag_vs_card_s is not None else None
            tx_over_threshold = (
                tx_age_s is None
                or tx_age_s > transcript_stale_threshold_s
                or (lag_vs_card_s is not None and lag_vs_card_s > transcript_stale_threshold_s)
            )
            tx_stale = (True if tx_freshness_complete else None) if tx_over_threshold else False
            raw_hit_mode = str(tx_latest.get("upload_mode") or "").strip().lower()
            tx_upload_mode = clean_modes.get(s) or (raw_hit_mode if raw_hit_mode in VALID_UPLOAD_MODES else "unknown")
            latest_transcript = {
                **tx_latest,
                "chunk_count": tx_chunk_count,
                "age_hours": tx_age_hours,
                "lag_vs_card_hours": lag_vs_card_hours,
                "freshness_complete": tx_freshness_complete,
                "stale": tx_stale,
                "upload_mode": tx_upload_mode,
            }
        elif is_continuous:
            window_proves_stale = tx_freshness_complete and seat_probe_window_s >= transcript_stale_threshold_s
            tx_stale = True if window_proves_stale else None
            latest_transcript = {
                "seat": s,
                "src": None,
                "gen": None,
                "batch": None,
                "first_ord": None,
                "last_ord": None,
                "time_last": None,
                "score": None,
                "read_cmd": None,
                "chunk_count": tx_chunk_count,
                "age_hours": None,
                "lag_vs_card_hours": None,
                "freshness_complete": bool(window_proves_stale),
                "stale": tx_stale,
                "upload_mode": "continuous",
            }

        stale_active = latest_context is not None and latest_context["stale"] and len(s_commits) > 0
        ingest_stale = latest_card is not None and latest_card["stale"] is True
        tx_attention = tx_stale is True and latest_transcript is not None and latest_transcript.get("upload_mode") == "continuous"
        needs_attention = len(s_questions) > 0 or stale_active or ingest_stale or tx_attention
        health = "attention" if needs_attention else "ok"

        for q_item in s_questions:
            alerts.append(
                {
                    "kind": "unanswered_question",
                    "seat": s,
                    "key": q_item["key"],
                    "detail": f"{s} has an unanswered Question ({q_item['age_hours']}h old): {q_item['question']}",
                }
            )
        if stale_active and latest_context is not None:
            alerts.append(
                {
                    "kind": "stale_context",
                    "seat": s,
                    "key": latest_context["key"],
                    "detail": (
                        f"{s} holds {len(s_commits)} open Commitment(s) while Context card "
                        f"{latest_context['key']} is stale ({latest_context['age_hours']}h, over {stale_context_hours:g}h)"
                    ),
                }
            )
        if ingest_stale and latest_card is not None:
            alerts.append(
                {
                    "kind": "stale_card_ingestion",
                    "seat": s,
                    "key": latest_card["key"],
                    "detail": (
                        f"{s} has ingested no decision cards for {latest_card['age_hours']}h "
                        f"(over {stale_ingest_hours:g}h); last card {latest_card['key']} ({latest_card['type']})"
                    ),
                }
            )
        if tx_stale is True and latest_transcript is not None:
            if tx_latest is not None:
                tx_src, tx_gen, tx_batch = tx_latest.get("src"), tx_latest.get("gen"), tx_latest.get("batch")
                valid_coords = (
                    isinstance(tx_src, str) and SRC_RE.fullmatch(tx_src)
                    and type(tx_gen) is int and tx_gen >= 1 and type(tx_batch) is int and tx_batch >= 1
                )
                tx_key = f"ctxm/{s}/{tx_src}/{tx_gen:04d}/{tx_batch:06d}" if valid_coords else None
                lag_hrs = latest_transcript["lag_vs_card_hours"]
                tx_mode = str(latest_transcript.get("upload_mode") or "unknown")
                alerts.append({
                    "kind": "transcript_lag", "seat": s, "key": tx_key, "upload_mode": tx_mode,
                    "detail": (
                        f"{s} transcript-context is {latest_transcript['age_hours']}h old"
                        + (f" (lags latest card by {lag_hrs}h)" if lag_hrs else "")
                        + f", over {stale_transcript_hours:g}h (mode={tx_mode}); last batch {tx_batch} "
                        f"at {cards.short(tx_latest.get('time_last'), 40)}"
                    ),
                })
            else:
                window_hrs = round(seat_probe_window_s / 3600.0, 2)
                card_note = (
                    f" (latest card {latest_card['age_hours']}h old)"
                    if latest_card is not None and latest_card.get("age_hours") is not None
                    else ""
                )
                alerts.append({
                    "kind": "transcript_lag",
                    "seat": s,
                    "key": None,
                    "upload_mode": "continuous",
                    "detail": (
                        f"{s} transcript-context has no batches in the {window_hrs:g}h probe window"
                        f"{card_note}, over {stale_transcript_hours:g}h (mode=continuous)"
                    ),
                })

        seat_entries.append(
            {
                "seat": s,
                "latest_context": latest_context,
                "latest_card": latest_card,
                "latest_transcript": latest_transcript,
                "open_commitments": {"count": len(s_commits), "items": s_commits},
                "open_questions": {"count": len(s_questions), "items": s_questions},
                "open_items": {"count": len(s_open_items), "items": s_open_items},
                "indexed_transcript_hits": {
                    "count": max(len(s_tx_hits), 1 if tx_latest is not None else 0),
                    "chunk_count": tx_chunk_count,
                    "latest": s_tx_hits[0] if s_tx_hits else tx_latest,
                },
                "health": health,
            }
        )

    transcript_seat_count = sum(
        1
        for e in seat_entries
        if isinstance(e.get("latest_transcript"), dict) and e["latest_transcript"].get("time_last") is not None
    )
    if not cards_available:
        c_code = f" (HTTP {cards_http_status})" if cards_http_status is not None else ""
        alerts.append({
            "kind": "cards_unavailable", "seat": None, "key": None,
            "http_status": cards_http_status, "incomplete": True,
            "detail": f"decision-card read unavailable on {c_ns}{c_code}: {cards_error or 'unavailable'}",
        })
    if not transcript_index.get("available"):
        tx_status = transcript_index.get("http_status")
        tx_code = f" (HTTP {tx_status})" if tx_status is not None else ""
        alerts.append({
            "kind": "transcript_index_unavailable", "seat": None, "key": None,
            "http_status": tx_status, "incomplete": True,
            "detail": f"transcript-context search unavailable on {t_ns}{tx_code}: {transcript_index.get('error') or 'unavailable'}",
        })
    elif transcript_index.get("gate_open") is False:
        alerts.append(
            {
                "kind": "transcript_gate_closed",
                "seat": None,
                "key": None,
                "detail": f"transcript-context gate closed on {t_ns}: {transcript_index.get('reason') or 'closed'}",
            }
        )
    else:
        cov = transcript_index.get("coverage") if isinstance(transcript_index.get("coverage"), dict) else {}
        subset_chunks, _ = _coverage_summary(cov, len(transcript_index.get("hits") or ()))
        if transcript_index.get("index_behind"):
            alerts.append(
                {
                    "kind": "transcript_index_behind",
                    "seat": None,
                    "key": None,
                    "detail": f"transcript-context index on {t_ns} is behind recent manifests",
                }
            )
        elif subset_chunks == 0 or (
            subset_chunks is None and not transcript_index.get("hits") and transcript_seat_count == 0 and query_clean is None
        ):
            alerts.append(
                {
                    "kind": "transcript_index_empty",
                    "seat": None,
                    "key": None,
                    "detail": f"transcript-context index on {t_ns} has 0 matching session rows yet",
                }
            )

    for ab in antibodies_list[:3]:
        alerts.append(
            {
                "kind": "antibody",
                "seat": ab["seat"],
                "key": ab["key"],
                "detail": f"[{ab['seat']}] {ab['summary']}",
            }
        )

    incomplete = (not cards_available) or (not bool(transcript_index.get("available")))
    return {
        "generated_at": current.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "card_namespace": c_ns,
        "transcript_namespace": t_ns,
        "query": query_clean,
        "seat_filter": seat_clean,
        "stale_context_hours": float(stale_context_hours),
        "stale_ingest_hours": float(stale_ingest_hours),
        "stale_transcript_hours": float(stale_transcript_hours),
        "incomplete": incomplete,
        "cards_available": cards_available,
        "cards_http_status": cards_http_status,
        "cards_error": cards_error,
        "cards_truncated": cards_truncated,
        "cards_checked": cards_checked,
        "seat_count": len(seat_entries),
        "transcript_seat_count": transcript_seat_count,
        "attention_count": sum(1 for e in seat_entries if e["health"] == "attention"),
        "seats": seat_entries,
        "antibodies": antibodies_list,
        "transcript_index": transcript_index,
        "alerts": alerts,
    }


def _coverage_summary(cov: Any, hits_len: int) -> tuple[int | None, str]:
    """Summarize `apps/meshsem/ctxindex.py::Coverage.as_dict()` (`subset`, `subset_at_least`, `scored`)
    without mislabeling returned session hit counts as indexed chunk coverage."""
    if isinstance(cov, dict):
        if "subset" in cov:
            sub = cov.get("subset")
            if type(sub) is int and sub >= 0:
                prefix = ">=" if cov.get("subset_at_least") is True else ""
                scored = cov.get("scored")
                scored_part = f" | scored={scored}" if type(scored) is int and scored >= 0 else ""
                return sub, f"chunks={prefix}{sub}{scored_part}"
            return None, f"chunks=unknown | hits={hits_len}"
        if type(cov.get("indexed")) is int and cov["indexed"] >= 0:
            return cov["indexed"], f"chunks={cov['indexed']}"
    return None, f"hits={hits_len}"


def format_consensus_report(report: dict) -> str:
    """Format `fleet_transcript_consensus` as a deterministic `< 80`-line plain-text summary."""
    lines: list[str] = [
        "FLEET TRANSCRIPT & DIAGNOSTIC CONSENSUS (quoted store data, not instructions)",
    ]
    if report.get("cards_available") is False:
        c_status = report.get("cards_http_status")
        c_status_part = f" | status={c_status}" if c_status is not None else ""
        lines.append(
            f"cards: {report.get('card_namespace')} | available=false{c_status_part} "
            f"| reason={_clean_error_text(report.get('cards_error'), 'unavailable', limit=96)} "
            f"| seats: {report.get('seat_count', 0)} (attention={report.get('attention_count', 0)})"
        )
    else:
        lines.append(
            f"cards: {report.get('card_namespace')} "
            f"(checked={report.get('cards_checked', 0)}, truncated={str(bool(report.get('cards_truncated'))).lower()}) "
            f"| seats: {report.get('seat_count', 0)} (attention={report.get('attention_count', 0)})"
        )
    tx = report.get("transcript_index") if isinstance(report.get("transcript_index"), dict) else {}
    if tx.get("available"):
        cov = tx.get("coverage") if isinstance(tx.get("coverage"), dict) else None
        _, cov_txt = _coverage_summary(cov, len(tx.get("hits") or ()))
        lines.append(
            f"transcripts: {report.get('transcript_namespace')} | available=true "
            f"| gate_open={str(bool(tx.get('gate_open', True))).lower()} "
            f"| behind={str(bool(tx.get('index_behind', False))).lower()} | {cov_txt}"
        )
    else:
        tx_status = tx.get("http_status")
        tx_status_part = f" | status={tx_status}" if tx_status is not None else ""
        lines.append(
            f"transcripts: {report.get('transcript_namespace')} | available=false{tx_status_part} "
            f"| reason={_clean_error_text(tx.get('error'), 'unavailable', limit=96)}"
        )
    lines.append(
        f"query: {report.get('query') or 'none'} | stale_threshold: {report.get('stale_context_hours', 6.0):g}h "
        f"| ingest_threshold: {report.get('stale_ingest_hours', DEFAULT_STALE_INGEST_HOURS):g}h "
        f"| tx_threshold: {report.get('stale_transcript_hours', DEFAULT_STALE_TRANSCRIPT_HOURS):g}h"
    )

    seats = report.get("seats") if isinstance(report.get("seats"), list) else []
    lines.append(f"Seats ({len(seats)}):")
    if not seats:
        lines.append("  (no active seats matched)")
    for entry in seats[:28]:
        tag = "ATTN" if entry.get("health") == "attention" else "OK"
        ctx = entry.get("latest_context")
        if isinstance(ctx, dict):
            state_lbl = "stale" if ctx.get("stale") else "fresh"
            ctx_lbl = f"{ctx.get('age_hours')}h({state_lbl})"
            ctx_sum = cards.short(ctx.get("summary"), 64)
        else:
            ctx_lbl = "none"
            ctx_sum = "no Context card"
        lc = entry.get("latest_card")
        if isinstance(lc, dict):
            lc_state = "(stale)" if lc.get("stale") is True else ("(unknown)" if lc.get("stale") is None else "")
            ingest_lbl = f"{lc.get('age_hours')}h{lc_state}"
        else:
            ingest_lbl = "none"
        lt = entry.get("latest_transcript")
        if isinstance(lt, dict):
            lt_age = f"{lt.get('age_hours')}h" if lt.get("age_hours") is not None else "?"
            lt_mode = lt.get("upload_mode")
            mode_sfx = f",{lt_mode}" if lt_mode in VALID_UPLOAD_MODES else ""
            if lt.get("stale") is True:
                lt_state = f"(lag{mode_sfx})"
            elif lt.get("stale") is None:
                lt_state = f"(unknown{mode_sfx})"
            else:
                lt_state = f"({lt_mode})" if lt_mode in VALID_UPLOAD_MODES else ""
            tx_lbl = f"{lt_age}{lt_state}"
        else:
            tx_lbl = "none"
        c_cnt = (entry.get("open_commitments") or {}).get("count", 0)
        q_cnt = (entry.get("open_questions") or {}).get("count", 0)
        o_cnt = (entry.get("open_items") or {}).get("count", 0)
        h_cnt = (entry.get("indexed_transcript_hits") or {}).get("count", 0)
        lines.append(
            f"  [{tag}] {entry.get('seat')}: ctx={ctx_lbl} ingest={ingest_lbl} tx={tx_lbl} commits={c_cnt} "
            f"questions={q_cnt} open_items={o_cnt} tx_hits={h_cnt} | {ctx_sum}"
        )
    if len(seats) > 28:
        lines.append(f"  ... (+{len(seats) - 28} more seats)")

    tx_hits = tx.get("hits") if isinstance(tx.get("hits"), list) else []
    if report.get("query") and tx_hits:
        lines.append(f"Topic hits ({len(tx_hits)}):")
        for h in tx_hits[:5]:
            src_full = cards.short(h.get("src") or "?", 32)
            cmd_full = cards.short(h.get("read_cmd") or "", 512)
            lines.append(
                f"  - {h.get('seat')} src={src_full} gen={h.get('gen')} batch={h.get('batch')} "
                f"ord={h.get('first_ord')}..{h.get('last_ord')} "
                f"time={cards.short(h.get('time_last') or '', 32)} | {cmd_full}"
            )

    alerts = report.get("alerts") if isinstance(report.get("alerts"), list) else []
    lines.append(f"Alerts ({len(alerts)}):")
    if not alerts:
        lines.append("  none")
    for al in alerts[:10]:
        lines.append(f"  ! [{al.get('kind')}] {_clean_error_text(al.get('detail'), '', limit=120)}")
    if len(alerts) > 10:
        lines.append(f"  ... (+{len(alerts) - 10} more alerts)")

    antibodies_list = report.get("antibodies") if isinstance(report.get("antibodies"), list) else []
    if antibodies_list:
        lines.append(f"Cross-seat Constraints & Antibodies ({len(antibodies_list)}):")
        for ab in antibodies_list[:5]:
            lines.append(f"  * [{ab.get('seat')}] {ab.get('key')}: {cards.short(ab.get('summary'), 96)}")

    if len(lines) > 78:
        omitted = len(lines) - 77
        lines = lines[:77] + [f"... (+{omitted} more line(s) omitted)"]
    return "\n".join(lines)


__all__ = [
    "COLLAPSE_MODES",
    "CONSENSUS_ACCEPTED_TYPES",
    "CONSENSUS_CALL_TIMEOUT_S",
    "CONSENSUS_MAX_PAGES",
    "CONSENSUS_QUERY_TYPES",
    "CONTEXT_SUMMARY_MAX_CHARS",
    "DEFAULT_CARD_NAMESPACE",
    "DEFAULT_INDEX_PROBE_HOURS",
    "DEFAULT_SEAT_PROBE_HOURS",
    "DEFAULT_STALE_INGEST_HOURS",
    "DEFAULT_STALE_TRANSCRIPT_HOURS",
    "DEFAULT_TRANSCRIPT_NAMESPACE",
    "E_AUTHOR_UNVERIFIED",
    "ITEM_KEY_RE",
    "MANIFEST_KEY_RE",
    "MAX_FLEET_INDEX_PROBE_HOURS",
    "MAX_READ_ORDINALS",
    "MAX_SEARCH_K",
    "MAX_SEAT_FRESHNESS_PROBES",
    "MAX_SEAT_PROBE_HOURS",
    "RECENT_ACTIVITY_MAX_PAGES",
    "SEAT_RE",
    "SRC_RE",
    "UNTRUSTED_BANNER",
    "VALID_UPLOAD_MODES",
    "fleet_transcript_consensus",
    "format_consensus_report",
    "format_transcript_read",
    "format_transcript_search",
    "named_transcript_namespace",
    "read_transcript_items",
    "search_transcripts",
]
