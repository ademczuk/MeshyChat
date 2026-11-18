"""Pure-stdlib normalization, receipt filtering, stem canonicalization, and anchor corroboration."""
from __future__ import annotations
import re
from typing import Any

DEFAULT_CARD_MIN_SCORE, DEFAULT_MESH_MIN_SCORE, DEFAULT_CONTEXT_MIN_SCORE, DEFAULT_MIN_DIRECTION_SCORE = 2.0, 1.3, 1.5, 0.65
MIN_DRAFT_CHARS, MIN_CANDIDATE_CHARS, RERANK_TEXT_CHARS, RERANK_CHUNK, REVERSE_DEPTH, ANCHOR_BONUS, FRESH_DAYS = 40, 80, 1500, 3, 6, 0.25, 14
BROADCAST_LABELS = frozenset({"all", "fleet", "everyone", "*"})
_STEM_PREFIXES, _STEM_SUFFIXES = ("codex-", "memory-"), ("-admin-cli", "-cli", "-prod", "-cc", "-node", "-session")
_GENERIC_STOP_ANCHORS = frozenset((
    "meshychat meshy_node agent agents operator session sessions thread threads message messages inbox postbox "
    "status review update request answer reply readme notes report commit branch main origin github python python3 "
    "pytest claude codex linux macos windows local server client prod production check checks read-only fail-closed "
    "non-blocking in-memory end-to-end pre-send up-to-date follow-up hand-off one-shot long-lived user-scope "
    "operator-authorized source-review re-review fourth-pass first-hand high-priority low-priority open-source "
    "short-lived machine-local host-side server-side client-side"
).split())
_CARD_HDR_RE = re.compile(r"^(?:Ruling|Decision|Constraint)\s*\|[^\n]*\n", re.I)
_GH_PR_URL_RE = re.compile(r"https?://github\.com/[^/\s]+/([^/\s#?]+)/(?:pull|issues)/(\d+)\S*", re.I)
_HEX_BLOB_RE = re.compile(r"\b(?:sha256[:=\s]+)?[0-9a-f]{32,64}\b", re.I)
_MSG_KEY_RE = re.compile(r"\bmsg/[A-Za-z0-9_.-]+\b")
_RECEIPT_OPEN_RE = re.compile(r"^\s*(?:done|ack|acknowledged|arrived\s+in\b|pong\b|confirmed\b|copied\b|noted\b)(?!\s*:)", re.I)
_TRANSPORT_RECEIPT_RE = re.compile(r"(?:arrived\s+in\s+this\s+\w+\s+thread|reply\s+sent\s+using\s+.*meshy_node|"
                                   r"signed\s+ack\s+reached\s+this\s+.*session|notice\s+without\s+an\s+inbox\s+poll)", re.I)
_ISSUE_PR_RE = re.compile(r"(?:(?<!\w)#(\d{2,6})\b|\b([A-Z]{2,8}-\d{1,5})\b)")
_FILE_RE = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z0-9_.-]+\.(?:py|js|ts|tsx|rs|go|sh|ps1|cmd|sql|toml|yaml|yml|plist|conf|gguf|service))(?![A-Za-z0-9_])", re.I)
_PORT_RE = re.compile(r"(?:127\.0\.0\.1|localhost|0\.0\.0\.0|\bport\s+):?(\d{4,5})\b", re.I)
_IDENT_RE = re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+|[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+|[a-z][a-z0-9]+(?:-[a-z0-9]{2,})+)\b")

def seat_stem(label: str | None) -> str:
    s = label.strip().lower() if isinstance(label, str) else ""
    if not s or s in BROADCAST_LABELS:
        return ""
    changed = True
    while changed:
        changed = False
        for pfx in _STEM_PREFIXES:
            if s.startswith(pfx) and len(s) > len(pfx):
                s, changed = s[len(pfx):], True
        for sfx in _STEM_SUFFIXES:
            if s.endswith(sfx) and len(s) > len(sfx):
                s, changed = s[:-len(sfx)], True
    return s

def norm_labels(val: Any) -> list[str]:
    stack, out = [val], []
    while stack:
        cur = stack.pop(0)
        if isinstance(cur, str):
            for raw in cur.split(","):
                if (s := raw.strip().lower()) and s not in BROADCAST_LABELS and s not in out:
                    out.append(s)
        elif isinstance(cur, (list, tuple, set)):
            stack[:0] = list(cur)
    return out

def normalize_rerank_text(text: str, *, max_chars: int = RERANK_TEXT_CHARS) -> str:
    s = _HEX_BLOB_RE.sub(" ", _GH_PR_URL_RE.sub(r"\1 #\2", _CARD_HDR_RE.sub("", text or "")))
    return re.sub(r"[ \t]{2,}", " ", s).strip()[:max_chars]

def extract_technical_anchors(text: str, *, exclude_stems: set[str] | frozenset[str] = frozenset()) -> set[str]:
    out, s = set(), _MSG_KEY_RE.sub(" ", _HEX_BLOB_RE.sub(" ", _GH_PR_URL_RE.sub(r"\1 #\2", text or "")))
    for m in _ISSUE_PR_RE.finditer(s):
        out.add(f"#{m.group(1)}" if m.group(1) else m.group(2).lower())
    for m in _FILE_RE.finditer(s):
        if (fn := m.group(1).lower()) not in {"env.json", "status.json", "state.json"} and not fn.endswith("-state.json"):
            out.add(f"file:{fn}")
    for m in _PORT_RE.finditer(s):
        if m.group(1) not in {"6310", "5432", "5434", "8080"}:
            out.add(f"port:{m.group(1)}")
    for m in _IDENT_RE.finditer(s):
        tok = m.group(1).lower()
        st = seat_stem(tok)
        if (len(tok) >= 5 and tok not in _GENERIC_STOP_ANCHORS and st == tok and st not in exclude_stems
                and not tok.endswith("-state") and not re.fullmatch(r"\d+.*", tok)):
            out.add(f"id:{tok}")
    return out

def is_non_actionable_receipt(text: str) -> bool:
    s = (text or "").strip()
    return True if not s else bool(len(s) <= 280 and "?" not in s and (
        _TRANSPORT_RECEIPT_RE.search(s) or (_RECEIPT_OPEN_RE.match(s) and not extract_technical_anchors(s))))
