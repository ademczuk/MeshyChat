"""The text of a graph row and its chunks (divergence 060; docs/design/wiki-in-graph.md 4.2).

Pure functions, no database. The graph walker (apps/meshsem/graphwalk.py) embeds what `chunks`
returns and lexes what `row_text` returns; nothing else decides either.

WHICH FIELD. A mail row has one text field, `body`. A wiki row does not: production's 998 heads
(measured 2026-09-30) carry `body_md` (978), `body` (18), `summary` (4) and `lesson` (3), and 21
carry none of the class. The rule is the LONGEST non-blank string among CONTENT_CLASS, the names
only a tiebreak in their order. Longest over ALL fields is wrong: on the graph the wiki came from,
an appended review field outgrew its entry (928 bytes against an 819-byte `body_md`) and would
have been embedded as the body. A row with no content-class string is data, not prose: `None`,
and the walker counts it as skipped_no_content rather than embedding an empty string.

THE TITLE. A string `title` is prefixed to the text ("title", a blank line, then the text), so it
lands in the first chunk and nowhere else, and the lexical terms carry it too.

CHUNKS. Windows of at most MAX_CHARS characters, each ending at the last paragraph break, else the
last sentence end, else the last space inside the window's back half, else at MAX_CHARS; the next
window starts OVERLAP characters before the previous one ended, so a sentence cut at a boundary is
whole in one of the two. A text of MAX_CHARS or fewer is one chunk, the whole of it.
"""
from __future__ import annotations

import re

#: The fields that may hold a graph row's prose, in tiebreak order (design 4.2).
CONTENT_CLASS = ("body_md", "body", "text", "content", "lesson", "summary")
#: The embed job's window (apps/meshsem/embedjob.MAX_CHARS): one limit for mail and graph.
MAX_CHARS = 1500
OVERLAP = 200
_SENTENCE_END = re.compile(r"[.!?][)\"']?\s")


def row_text(value) -> tuple[str, str] | None:
    """(field, text) for a graph row's value, the title prefixed; None when no content-class field
    holds a non-blank string."""
    if not isinstance(value, dict):
        return None
    best: tuple[str, str] | None = None
    for field in CONTENT_CLASS:
        v = value.get(field)
        if isinstance(v, str) and v.strip() and (best is None or len(v) > len(best[1])):
            best = (field, v)
    if best is None:
        return None
    field, text = best
    title = value.get("title")
    if isinstance(title, str) and title.strip():
        text = f"{title.strip()}\n\n{text}"
    return field, text


def _break_at(text: str, start: int, end: int) -> int:
    """The end of the chunk that starts at `start` with `end` as its hard limit: after the last
    paragraph break, else the last sentence end, else the last space. A break is looked for only in
    the window's back half, so no chunk but the last is shorter than half a window."""
    lo = start + (end - start) // 2
    para = text.rfind("\n\n", lo, end)
    if para >= 0:
        return para + 2
    last = None
    for m in _SENTENCE_END.finditer(text, lo, end):
        last = m
    if last is not None:
        return last.end()
    space = max(text.rfind(" ", lo, end), text.rfind("\n", lo, end))
    if space >= 0:
        return space + 1
    return end


def chunk_spans(text: str, *, max_chars: int = MAX_CHARS, overlap: int = OVERLAP) -> list[tuple[int, int]]:
    """[(start, end)] character spans covering `text`, each at most `max_chars` long."""
    if max_chars <= overlap:
        raise ValueError("max_chars must exceed overlap, or the walk could not advance")
    n = len(text)
    if n <= max_chars:
        return [(0, n)]
    spans = []
    start = 0
    while True:
        hard = start + max_chars
        if hard >= n:
            spans.append((start, n))
            return spans
        end = _break_at(text, start, hard)
        spans.append((start, end))
        start = max(end - overlap, start + 1)


def chunks(text: str) -> list[str]:
    """The chunk texts, in order."""
    return [text[a:b] for a, b in chunk_spans(text)]


__all__ = ["CONTENT_CLASS", "MAX_CHARS", "OVERLAP", "chunk_spans", "chunks", "row_text"]
