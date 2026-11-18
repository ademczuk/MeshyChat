"""Deterministic extractive digest for /v1/mesh/summaries/{thread,backlog}?mode=deterministic.

Runs strictly in-process with zero LLM, provider, or tokenizer calls so voice/external seats
(such as `ixxxxxx`) and cold-start orientation checks always get declared facts plus cited
extractive sentences even when no chat provider or measurement pin is configured. Skips
`mesh_summary_requests` model-call admission (zero HQB GPU/provider cost) while still crossing
`peer_limit.admit`, `check_permission`, `namespace_body_withheld`, and `mesh_route_usage`.
"""
from __future__ import annotations

import unicodedata

DETERMINISTIC_SUMMARY_VERSION = "deterministic-v1"
DETERMINISTIC_LABEL = (
    "Deterministic extractive digest of the messages in `covered` and declared `facts` "
    "(no language model used). Unverified: read the cited messages before acting."
)
MAX_EXTRACTIVE_SENTENCES = 12
MAX_EXCERPT_CHARS = 180
HIGH_SIGNAL_TYPES = frozenset({
    "decision", "verdict", "blocker", "handoff", "request", "question", "proposal",
})


def _clean_single_line(text: str, *, max_chars: int = MAX_EXCERPT_CHARS) -> str:
    """Collapse whitespace/newlines and escape any remaining control or format codepoints."""
    collapsed = " ".join(str(text or "").split())
    safe = "".join(
        f"[U+{ord(ch):04X}]" if unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") else ch
        for ch in collapsed
    ).strip()
    if len(safe) > max_chars:
        return safe[: max_chars - 3].rstrip() + "..."
    return safe


def _first_meaningful_line(body: str) -> str:
    for raw_line in str(body or "").splitlines():
        cleaned = _clean_single_line(raw_line)
        if cleaned:
            return cleaned
    return _clean_single_line(body)


def _is_high_signal(row) -> bool:
    val = row.value if isinstance(getattr(row, "value", None), dict) else {}
    msg_type = str(val.get("type") or "").strip().lower()
    if msg_type in HIGH_SIGNAL_TYPES:
        return True
    if val.get("requires_ack") is True:
        return True
    if val.get("closes") or val.get("declines"):
        return True
    return False


def extract_sentences(rows: list) -> list[dict]:
    """Select up to `MAX_EXTRACTIVE_SENTENCES` representative rows from `rows` (already in
    oldest-first presentation order) and emit one single-line cited sentence per selected row."""
    if not rows:
        return []
    n_rows = len(rows)
    selected_indices: set[int] = set()

    # 1. Always include high-signal asks, decisions, verdicts, closes, and declines (newest first if capped).
    high_signal = [idx for idx, r in enumerate(rows) if _is_high_signal(r)]
    for idx in reversed(high_signal):
        if len(selected_indices) >= MAX_EXTRACTIVE_SENTENCES:
            break
        selected_indices.add(idx)

    # 2. Include the latest message from each distinct author if budget remains.
    latest_by_author: dict[str, int] = {}
    for idx, r in enumerate(rows):
        author_key = str(getattr(r, "author", "") or "unknown").lower()
        latest_by_author[author_key] = idx
    for idx in sorted(latest_by_author.values(), reverse=True):
        if len(selected_indices) >= MAX_EXTRACTIVE_SENTENCES:
            break
        selected_indices.add(idx)

    # 3. Ensure first and last chronological messages are represented when budget permits.
    for boundary in (n_rows - 1, 0):
        if len(selected_indices) < MAX_EXTRACTIVE_SENTENCES:
            selected_indices.add(boundary)

    # 4. Fill remaining slots with most recent messages.
    for idx in range(n_rows - 1, -1, -1):
        if len(selected_indices) >= MAX_EXTRACTIVE_SENTENCES:
            break
        selected_indices.add(idx)

    sentences: list[dict] = []
    for idx in sorted(selected_indices):
        r = rows[idx]
        val = r.value if isinstance(getattr(r, "value", None), dict) else {}
        author = _clean_single_line(getattr(r, "author", None) or val.get("from") or "unknown", max_chars=48)
        msg_type = _clean_single_line(val.get("type") or "note", max_chars=24)
        markers: list[str] = [msg_type]
        if val.get("requires_ack") is True:
            markers.append("ack")
        if val.get("closes"):
            markers.append("closes")
        if val.get("declines"):
            markers.append("declines")
        excerpt = _first_meaningful_line(getattr(r, "body", ""))
        tag = ",".join(markers)
        text = f"{author} [{tag}]: {excerpt}" if excerpt else f"{author} [{tag}]"
        sentences.append({"text": text, "cites": [r.key]})
    return sentences
