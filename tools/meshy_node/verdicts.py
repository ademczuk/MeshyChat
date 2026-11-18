"""Pullboard-inspired approval gates, verdict freshness projection, merge-tree conflict checks,
and longest-prefix lane ownership for `tools/meshy_node/`.

Cherrypicked mechanisms (adapted in pure Python stdlib from `pullboard-dev/pullboard` 0.9.1):
1. Canonical Verdict Header Parser & Validator (`parse_verdict_header`, `validate_verdict` from
   `src/machine.js:268-285` and `src/board.js:138-144,244-261,1544-1605`):
   - Enforces `EXIT_GUARDS`: `CODE GO` / `GO` requires `P1 == 0` and a concrete hex `HEAD` (>= 12 hex)
     or 64-hex `TAR_SHA256` / `ARTIFACT_SHA256` (never `"none"` or `"latest"`).
   - `FIX FIRST` / `NO-GO` / `REVISE` requires `(P1 + P2) >= 1` when defect counts are given,
     validates `REASON` against `REJECT_REASONS`, and validates typed `CAUSE` references.
2. Multi-Family Quorum, Spawn-Lineage Standing & `HEAD_NOT_NEW` Evaluator (`evaluate_quorum`,
   `check_verifier_standing`, `check_head_is_new` from `src/seats.js:184-219`, `src/board.js:1270-1280,1451-1466`):
   - Refuses `SELF_VERIFY`, same-family verification when cross-family is required (`O2_FAMILY_MATCH`),
     and builder-spawned subagents (`VERIFIER_STANDING` walking `creator_map` up to `coordinator`).
   - Classifies each seat's latest verdict against `target_sha` as `VERIFIED_GO`, `STALE_GO`,
     `BLOCKED_FIX_FIRST`, or `MISSING`, and refuses re-submission of a rejected `(head_sha, criterion_digest)`
     with `HEAD_NOT_NEW`.
3. Path-Scoped Stale-Verdict Projector (`capture_verdict_review`, `project_verdict_freshness` from
   `src/verdict-freshness.js:33-156`):
   - Seals `{item, commit, head, trunk, paths}` and evaluates trunk advancement by intersecting
     `main_touched_paths` with `review["paths"]` (`DISJOINT_PATHS` vs `STALE_VERDICT_PATH_OVERLAP`).
4. Pre-Review Merge-Tree Conflict Check (`check_merge_conflicts` from `src/trusted-policy.js:98-112`):
   - Runs read-only `git merge-tree --write-tree --name-only -z <trunk> <head>` without touching
     any worktree or index.
5. Longest-Prefix Lane Ownership (`file_lane`, `out_of_lane`, `check_lane_overlaps` from `src/lanes.js:49-95`).
"""
from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import defang

DECISIONS = ("CODE GO", "FIX FIRST", "GO", "NO-GO", "REVISE", "REJECT", "BLOCK", "HOLD")
POSITIVE_DECISIONS = frozenset({"CODE GO", "GO"})
NEGATIVE_DECISIONS = frozenset({"FIX FIRST", "NO-GO", "REVISE", "REJECT", "BLOCK"})

REJECT_REASONS = frozenset({
    "TEST_FAILURE",
    "BEHAVIOR_MISMATCH",
    "INSUFFICIENT_EVIDENCE",
    "STALE_HEAD",
    "SECURITY_FINDING",
    "OTHER",
})

FAMILIES = frozenset({"claude", "codex", "gemini", "kimi"})

_HEX_SHA_RE = re.compile(r"^[0-9a-fA-F]{12,64}$")
_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_UUID_PEER_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_INLINE_HEX_RE = re.compile(r"\b([0-9a-fA-F]{12,64})\b")
_FENCE_LINE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_VERDICT_LINE_RE = re.compile(
    r"^\s*(?:[-*#]+\s*|(?:\d+[.)]\s*))?(?:\*\*|__|\[)?VERDICT\b(?!-[A-Za-z0-9])"
    r"(?:\s*\([^)\n]{1,40}\))?"
    r"(?:\s*[:=\-\u2013\u2014>]+\s*(?:\*\*|__|\]|\))?|\s*(?:\*\*|__|\]|\))\s*[:=\-\u2013\u2014>]*\s*|\s+(?=(?:CODE\s+GO|FIX[- ]FIRST|NO[- ]?GO|NOT\s+A\s+GO|NOT\s+GO|NOT\s+OK|(?:A\s+)?VETO|REVISE|REJECT|BLOCK|HOLD|GO)\b))"
    r"(.+)$",
    re.IGNORECASE,
)
_MIDLINE_STRUCTURED_VERDICT_RE = re.compile(
    r"(?:^|;\s*)VERDICT\b(?!-[A-Za-z0-9])\s*[:=\-\u2013\u2014>]+\s*([^`\n]+;[^`\n]+)$",
    re.IGNORECASE,
)
_BARE_NEGATIVE_LINE_RE = re.compile(
    r"^\s*(?:[-*#]+\s*)?(?:\*\*|`)?((?:FIX[- ]FIRST|NO[- ]?GO|REVISE|REJECT|BLOCK)\b(?:\s*$|\s*[;.,:!?\-\u2013\u2014].*))$",
    re.IGNORECASE,
)
_PROSE_NEGATIVE_TOKEN_RE = re.compile(
    r"(?:^|(?<=\s))-1(?=$|\s|[.,;:!?)])|"
    r"\b(?:NO[\s\-\u2013\u2014]*GO|NOT\s+A\s+GO|NOT\s+GO|NOT\s+OK|NOT\s+READY|"
    r"FIX[\s\-\u2013\u2014]+FIRST|REVISE|REQUEST(?:S|ED|ING)?\s+CHANGES|CHANGES\s+REQUESTED|"
    r"REJECT(?:ED)?|BLOCK(?:ED|ING)?|VETO(?:ED)?|"
    r"FAIL(?:ED|S|ING|URE|URES)?|RETRACT(?:ED|ING|S)?|WITHDRAW(?:N|ING|S)?|"
    r"DO\s+NOT\s+(?:MERGE|LAND)|DON'T\s+(?:MERGE|LAND)|"
    r"NOPE|BROKEN|FOUND\s+A\s+P[12]|PLEASE\s+WAIT|HOLD)\b",
    re.IGNORECASE,
)
_QUOTED_ENDORSEMENT_RE = re.compile(
    r"(?:^|(?<=\s))\+1(?=$|\s|[.,;:!?)])|\b(?:agreed|seconded|same\s+here|concur)\b",
    re.IGNORECASE,
)
_EMBEDDED_FIELD_SPLIT_RE = re.compile(
    r"^(.+?)[.,!\s]+\s*((?:HEAD|COMMIT|TAR_SHA256|ARTIFACT_SHA256|SHA256|ITEM|P1|P2|REASON|CAUSE)\s*:.+)$",
    re.IGNORECASE,
)
_NEGATIVE_PREFIX_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^FIX[\s\-]+FIRST(?=$|[\s.,:;!?\-\u2013\u2014(/])", re.IGNORECASE), "FIX FIRST"),
    (re.compile(r"^NO(?:[\s\-]+)?GO(?=$|[\s.,:;!?\-\u2013\u2014(/])", re.IGNORECASE), "NO-GO"),
    (re.compile(r"^NOT\s+A\s+GO(?=$|[\s.,:;!?\-\u2013\u2014(/])", re.IGNORECASE), "NO-GO"),
    (re.compile(r"^NOT\s+GO(?=$|[\s.,:;!?\-\u2013\u2014(/])", re.IGNORECASE), "NO-GO"),
    (re.compile(r"^NOT\s+OK(?=$|[\s.,:;!?\-\u2013\u2014(/])", re.IGNORECASE), "NO-GO"),
    (re.compile(r"^(?:A\s+)?VETO(?=$|[\s.,:;!?\-\u2013\u2014(/])", re.IGNORECASE), "REJECT"),
    (re.compile(r"^REVISE(?=$|[\s.,:;!?\-\u2013\u2014(/])", re.IGNORECASE), "REVISE"),
    (re.compile(r"^REJECT(?:ED)?(?=$|[\s.,:;!?\-\u2013\u2014(/])", re.IGNORECASE), "REJECT"),
    (re.compile(r"^BLOCK(?:ED)?(?=$|[\s.,:;!?\-\u2013\u2014(/])", re.IGNORECASE), "BLOCK"),
    (re.compile(r"^FAIL(?:ED)?(?=$|[\s.,:;!?\-\u2013\u2014(/])", re.IGNORECASE), "REJECT"),
    (re.compile(r"^REQUEST\s+CHANGES(?=$|[\s.,:;!?\-\u2013\u2014(/])", re.IGNORECASE), "REVISE"),
    (re.compile(r"^HOLD(?=$|[\s.,:;!?\-\u2013\u2014(/])", re.IGNORECASE), "HOLD"),
)

_TYPED_CAUSE_PATTERNS = (
    re.compile(r"^test:[^\s:]+::\S+$"),
    re.compile(r"^file:[^\s:]+:\d+(?:-\d+)?$"),
    re.compile(r"^refusal:[A-Za-z0-9_.-]+$"),
    re.compile(r"^row:[A-Za-z0-9_.:/-]+$"),
    re.compile(r"^rule:[A-Za-z0-9_.:/-]+$"),
    re.compile(r"^doctrine:[A-Za-z0-9_.:/-]+$"),
    re.compile(r"^sentinel:[A-Za-z0-9_.:/-]+$"),
)

_FORBIDDEN_HEAD_TOKENS = frozenset({"none", "null", "latest", "head", "main", "master", "tbd", "unknown"})

_CLAUDE_SEAT_PREFIXES = (
    "claude",
    "bxxxxx",
    "exxxx",
    "txxxx",
    "dxxxxx",
    "uxxx",
    "vxxxxxxxxxx",
    "oxxxxx",
    "memory-",
    "pxx",
    "kxxx",
    "sxxxxxxxx",
)


def _seat_roster_family(seat: str | None) -> str:
    """Resolve model family strictly from the trusted seat roster prefix/name (`unknown` if unrecognized)."""
    if not isinstance(seat, str) or not seat.strip():
        return "unknown"
    s = seat.strip().lower()
    if s in FAMILIES:
        return s
    if "antigravity" in s or "gemini" in s or s.startswith("agy"):
        return "gemini"
    if s.startswith("codex") or "-codex" in s or "openai" in s:
        return "codex"
    if "nxxxxx" in s or "kimi" in s:
        return "kimi"
    if any(s == p or s.startswith(p) for p in _CLAUDE_SEAT_PREFIXES):
        return "claude"
    return "unknown"


def resolve_family(seat: str | None, explicit_family: str | None = None) -> str:
    """Resolve model family (`claude`, `codex`, `gemini`, `kimi`, or `unknown`).

    Known seats in the fleet roster ALWAYS resolve to their roster family so a self-asserted
    `FAMILY:` header tag can never spoof cross-family standing. `explicit_family` is used only
    when `seat` is not in the known seat roster.
    """
    roster_fam = _seat_roster_family(seat)
    if roster_fam != "unknown":
        return roster_fam
    if isinstance(explicit_family, str):
        fam = explicit_family.strip().lower()
        if fam in FAMILIES:
            return fam
    return "unknown"


def parse_rejection_cause(cause: str) -> dict | None:
    """Parse and validate a typed rejection cause (`test:<file>::<name>`, `file:<path>:<line>`,
    `refusal:<CODE>`, `row:<id>`, `rule:<id>`, `doctrine:<id>`, `sentinel:<name>`).
    """
    if not isinstance(cause, str):
        return None
    token = cause.strip()
    if not token:
        return None
    for pat in _TYPED_CAUSE_PATTERNS:
        if pat.match(token):
            kind, _, target = token.partition(":")
            return {"kind": kind, "target": target, "raw": token}
    return None


def _normalize_decision(raw: str) -> str:
    stripped = defang.strip_invisible_and_bidi(raw or "").strip().strip("*`_[]")
    cleaned = " ".join(stripped.upper().split())
    if not cleaned:
        return ""
    for pos in ("CODE GO", "GO"):
        if cleaned == pos or cleaned == pos + ".":
            return pos
    for pat, canonical in _NEGATIVE_PREFIX_PATTERNS:
        if pat.match(cleaned):
            return canonical
    return cleaned


def _unquoted_unfenced_lines(text: str) -> list[str]:
    """Return lines from `text` outside closed Markdown fenced code blocks (` ``` ` / `~~~`) and
    excluding Markdown blockquotes (`>`).

    If a fenced code block is left unclosed at EOF, any negative/non-GO verdict line inside the
    unclosed fence is still surfaced so an unclosed fence before `VERDICT: NO-GO` cannot hide a
    blocking verdict.
    """
    out: list[str] = []
    unclosed_fence_lines: list[str] = []
    active_fence_char: str | None = None
    active_fence_len = 0
    for line in text.splitlines():
        m_fence = _FENCE_LINE_RE.match(line)
        if m_fence:
            marker = m_fence.group(1)
            if active_fence_char is None:
                active_fence_char = marker[0]
                active_fence_len = len(marker)
                unclosed_fence_lines = []
                continue
            if marker[0] == active_fence_char and len(marker) >= active_fence_len:
                active_fence_char = None
                active_fence_len = 0
                unclosed_fence_lines = []
                continue
        if active_fence_char is not None:
            if not line.lstrip().startswith(">"):
                unclosed_fence_lines.append(line)
            continue
        if line.lstrip().startswith(">"):
            continue
        out.append(line)

    if active_fence_char is not None and unclosed_fence_lines:
        for fline in unclosed_fence_lines:
            m_v = (
                _VERDICT_LINE_RE.match(fline)
                or _MIDLINE_STRUCTURED_VERDICT_RE.search(fline)
                or _BARE_NEGATIVE_LINE_RE.match(fline)
            )
            if m_v:
                first_seg = m_v.group(1).split(";", 1)[0].strip()
                if _normalize_decision(first_seg) not in POSITIVE_DECISIONS:
                    out.append(fline)
            elif _PROSE_NEGATIVE_TOKEN_RE.search(fline):
                out.append(fline)
    return out


def _has_prose_negative_token(text: str) -> bool:
    """Return True if unquoted, unfenced `text` contains any negative/withdrawal token,
    or endorses a quoted negative verdict (`> VERDICT: NO-GO` + `+1`).
    """
    if not isinstance(text, str) or not text.strip():
        return False
    sanitized_text = defang.sanitize_untrusted_text(text)
    candidate_lines = _unquoted_unfenced_lines(sanitized_text)
    if not candidate_lines:
        return False
    unquoted = "\n".join(candidate_lines)
    if _PROSE_NEGATIVE_TOKEN_RE.search(unquoted):
        return True
    if _QUOTED_ENDORSEMENT_RE.search(unquoted):
        quoted_lines = [
            ln.lstrip()[1:].lstrip()
            for ln in sanitized_text.splitlines()
            if ln.lstrip().startswith(">")
        ]
        if quoted_lines and _PROSE_NEGATIVE_TOKEN_RE.search("\n".join(quoted_lines)):
            return True
    return False


def parse_verdict_header(text: str) -> dict | None:
    """Parse a `VERDICT: ...` header line from a mesh message body.

    Returns a dictionary containing normalized fields:
    `decision`, `structured` (bool), `item`, `head`, `tar_sha256`, `artifact_sha256`,
    `family`, `p1`, `p2`, `reason`, `cause`, `causes`, `next`, `criterion_digest`, `trunk`,
    `cited_shas`, `raw_fields`, `duplicate_fields`.
    """
    if not isinstance(text, str) or not text.strip():
        return None

    sanitized_text = defang.sanitize_untrusted_text(text)
    candidate_lines = _unquoted_unfenced_lines(sanitized_text)
    if not candidate_lines:
        return None
    unquoted_text = "\n".join(candidate_lines)

    header_payload: str | None = None
    for line in candidate_lines:
        m = _VERDICT_LINE_RE.match(line)
        if m:
            header_payload = m.group(1).strip()
            break
    if header_payload is None:
        for line in candidate_lines:
            m_mid = _MIDLINE_STRUCTURED_VERDICT_RE.search(line)
            if m_mid:
                header_payload = m_mid.group(1).strip()
                break
    if header_payload is None:
        for line in candidate_lines:
            m_bare = _BARE_NEGATIVE_LINE_RE.match(line)
            if m_bare:
                header_payload = m_bare.group(1).strip()
                break

    if header_payload is None:
        return None

    all_shas = [h.lower() for h in _INLINE_HEX_RE.findall(unquoted_text)]
    seen_shas: list[str] = []
    for h in all_shas:
        if h not in seen_shas:
            seen_shas.append(h)

    segments = [seg.strip() for seg in header_payload.split(";") if seg.strip()]
    if not segments:
        return None

    m_embed = _EMBEDDED_FIELD_SPLIT_RE.match(segments[0])
    if m_embed:
        segments = [m_embed.group(1).strip(), m_embed.group(2).strip(), *segments[1:]]

    structured = len(segments) > 1 or ";" in header_payload
    decision = _normalize_decision(segments[0])
    raw_fields: dict[str, str] = {}
    duplicate_fields: list[str] = []
    canonical_seen: set[str] = set()
    alias_map = {
        "COMMIT": "HEAD",
        "SHA256": "ARTIFACT_SHA256",
        "DIGEST": "CRITERION_DIGEST",
    }
    for seg in segments[1:]:
        if ":" not in seg:
            continue
        k, _, v = seg.partition(":")
        key_norm = k.strip().upper().replace("-", "_")
        canon_key = alias_map.get(key_norm, key_norm)
        if key_norm in raw_fields or canon_key in canonical_seen:
            if canon_key not in duplicate_fields:
                duplicate_fields.append(canon_key)
        canonical_seen.add(canon_key)
        raw_fields[key_norm] = v.strip()

    def _parse_int(val: str | None) -> int | str | None:
        if val is None:
            return None
        try:
            return int(val)
        except ValueError:
            return val

    head = raw_fields.get("HEAD") or raw_fields.get("COMMIT")
    tar_sha = raw_fields.get("TAR_SHA256")
    art_sha = raw_fields.get("ARTIFACT_SHA256") or raw_fields.get("SHA256") or tar_sha
    if not structured and head is None and seen_shas:
        head = seen_shas[0]

    for candidate in (head, tar_sha, art_sha):
        if isinstance(candidate, str) and _HEX_SHA_RE.match(candidate):
            low = candidate.lower()
            if low not in seen_shas:
                seen_shas.insert(0, low)

    cause_raw = raw_fields.get("CAUSE")
    causes = [c.strip() for c in cause_raw.split(",") if c.strip()] if cause_raw else []

    return {
        "decision": decision,
        "structured": structured,
        "item": raw_fields.get("ITEM"),
        "head": head,
        "tar_sha256": tar_sha,
        "artifact_sha256": art_sha,
        "family": raw_fields.get("FAMILY"),
        "p1": _parse_int(raw_fields.get("P1")),
        "p2": _parse_int(raw_fields.get("P2")),
        "reason": raw_fields.get("REASON"),
        "cause": cause_raw,
        "causes": causes,
        "next": raw_fields.get("NEXT"),
        "criterion_digest": raw_fields.get("CRITERION_DIGEST") or raw_fields.get("DIGEST"),
        "trunk": raw_fields.get("TRUNK"),
        "cited_shas": seen_shas,
        "raw_fields": raw_fields,
        "duplicate_fields": duplicate_fields,
    }


def validate_verdict(parsed_or_text: dict | str) -> list[str]:
    """Validate a parsed verdict dict (or raw message text) against Pullboard's `EXIT_GUARDS`.

    Returns a list of refusal strings (empty list means valid).
    """
    parsed = parse_verdict_header(parsed_or_text) if isinstance(parsed_or_text, str) else parsed_or_text
    if not isinstance(parsed, dict):
        return ["E_VERDICT_MISSING: no VERDICT header found"]

    problems: list[str] = []
    decision = parsed.get("decision")
    if decision not in DECISIONS:
        problems.append(f"E_VERDICT_DECISION: unknown decision {decision!r}; expected one of {DECISIONS}")
        return problems

    dup_fields = parsed.get("duplicate_fields") or []
    if dup_fields:
        problems.append(f"E_VERDICT_DUPLICATE_FIELD: duplicate header field(s) {dup_fields}")

    head = parsed.get("head")
    art_sha = parsed.get("artifact_sha256") or parsed.get("tar_sha256")
    p1 = parsed.get("p1")
    p2 = parsed.get("p2")

    if p1 is not None and (isinstance(p1, bool) or not isinstance(p1, int) or p1 < 0):
        problems.append(f"E_VERDICT_BAD_P1: P1 must be a non-negative integer, got {p1!r}")
    if p2 is not None and (isinstance(p2, bool) or not isinstance(p2, int) or p2 < 0):
        problems.append(f"E_VERDICT_BAD_P2: P2 must be a non-negative integer, got {p2!r}")

    has_valid_head = (
        isinstance(head, str)
        and head.strip().lower() not in _FORBIDDEN_HEAD_TOKENS
        and bool(_HEX_SHA_RE.match(head.strip()))
    )
    has_valid_artifact = (
        isinstance(art_sha, str)
        and art_sha.strip().lower() not in _FORBIDDEN_HEAD_TOKENS
        and bool(_HEX64_RE.match(art_sha.strip()))
    )

    if decision in POSITIVE_DECISIONS:
        if not parsed.get("structured"):
            problems.append("E_VERDICT_UNSTRUCTURED: CODE GO / GO requires a structured ';'-delimited header")
        if not (has_valid_head or has_valid_artifact):
            problems.append(
                "E_VERDICT_HEAD_REQUIRED: CODE GO / GO requires a concrete hex HEAD (>= 12 hex chars) "
                "or 64-hex TAR_SHA256 / ARTIFACT_SHA256 (never 'none' or 'latest')"
            )
        if p1 is None or isinstance(p1, bool) or not isinstance(p1, int):
            problems.append(f"E_VERDICT_P1_REQUIRED: {decision} requires explicit P1: 0")
        elif p1 > 0:
            problems.append(f"E_VERDICT_P1_NONZERO: {decision} requires P1 == 0, got P1={p1}")

    elif decision in NEGATIVE_DECISIONS:
        raw_fields = parsed.get("raw_fields") or {}
        if ("HEAD" in raw_fields or "COMMIT" in raw_fields) and not has_valid_head:
            problems.append(f"E_VERDICT_BAD_HEAD: invalid HEAD {head!r} in {decision} header")
        if isinstance(p1, int) and not isinstance(p1, bool) and isinstance(p2, int) and not isinstance(p2, bool):
            if (p1 + p2) < 1:
                problems.append(f"E_VERDICT_ZERO_DEFECTS: {decision} requires (P1 + P2) >= 1 when P1/P2 are given")
        elif isinstance(p1, int) and not isinstance(p1, bool) and p2 is None and p1 < 1:
            problems.append(f"E_VERDICT_ZERO_DEFECTS: {decision} requires P1 >= 1 when P1 is given without P2")

        reason = parsed.get("reason")
        if reason is not None:
            if not isinstance(reason, str) or reason.strip().upper() not in REJECT_REASONS:
                problems.append(
                    f"E_VERDICT_BAD_REASON: REASON {reason!r} must be one of {sorted(REJECT_REASONS)}"
                )

        causes = parsed.get("causes") or ([parsed["cause"]] if parsed.get("cause") else [])
        for c in causes:
            if parse_rejection_cause(c) is None:
                problems.append(
                    f"E_VERDICT_BAD_CAUSE: CAUSE {c!r} must match test:<path>::<name>, "
                    "file:<path>:<line>, refusal:<CODE>, row:<id>, rule:<id>, doctrine:<id>, or sentinel:<name>"
                )

    return problems


def check_verifier_standing(
    verifier: str,
    builder: str | None,
    *,
    creator_map: dict[str, str] | None = None,
    coordinator: str = "coordinator",
    require_cross_family: bool = False,
    verifier_family: str | None = None,
    builder_family: str | None = None,
) -> dict:
    """Check whether `verifier` has independent standing to verify work built by `builder`.

    Enforces:
    1. `SELF_VERIFY`: `verifier != builder`.
    2. `VERIFIER_STANDING` (`src/seats.js:184-219`): walks `creator_map` from `verifier` up to
       `coordinator`; refuses if `builder` is in `verifier`'s creator chain, if the creator chain
       loops, or if `builder == coordinator` and `verifier` descends from `coordinator`.
    3. `O2_FAMILY_MATCH` (`src/board.js:1458-1460`): when `require_cross_family` is True, refuses
       if `verifier` and `builder` resolve to the same model family.
    """
    v_norm = (verifier or "").strip()
    b_norm = (builder or "").strip()
    if not v_norm:
        return {"ok": False, "code": "VERIFIER_STANDING", "reason": "verifier seat label is empty"}

    if b_norm and v_norm.casefold() == b_norm.casefold():
        return {
            "ok": False,
            "code": "SELF_VERIFY",
            "reason": f"{v_norm} cannot verify its own build ({b_norm})",
        }

    if creator_map and b_norm:
        folded_map = {k.strip().casefold(): v.strip() for k, v in creator_map.items() if k and v}
        coord_folded = coordinator.strip().casefold()
        b_folded = b_norm.casefold()

        seen: set[str] = set()
        curr = v_norm.casefold()
        while curr in folded_map:
            if curr in seen:
                return {
                    "ok": False,
                    "code": "VERIFIER_STANDING",
                    "reason": f"{v_norm} creator lineage contains a cycle at {curr}",
                }
            seen.add(curr)
            creator = folded_map[curr]
            creator_folded = creator.casefold()
            if creator_folded == b_folded:
                return {
                    "ok": False,
                    "code": "VERIFIER_STANDING",
                    "reason": f"{v_norm} was spawned by builder {b_norm} and lacks independent standing",
                }
            if b_folded == coord_folded and creator_folded == coord_folded:
                return {
                    "ok": False,
                    "code": "VERIFIER_STANDING",
                    "reason": f"{v_norm} descends from {coordinator} and cannot verify {coordinator}'s build",
                }
            if creator_folded == coord_folded:
                break
            curr = creator_folded

    v_fam = resolve_family(v_norm, verifier_family)
    b_fam = resolve_family(b_norm, builder_family) if b_norm else "unknown"
    if require_cross_family:
        if v_fam == "unknown" or (b_norm and b_fam == "unknown"):
            return {
                "ok": False,
                "code": "O2_FAMILY_MATCH",
                "reason": (
                    f"cross-family verification requires known model families "
                    f"(verifier {v_norm}={v_fam}, builder {b_norm}={b_fam})"
                ),
            }
        if v_fam == b_fam:
            return {
                "ok": False,
                "code": "O2_FAMILY_MATCH",
                "reason": f"verifier {v_norm} ({v_fam}) shares model family with builder {b_norm} ({b_fam})",
            }

    return {"ok": True, "code": None, "reason": None, "verifier_family": v_fam, "builder_family": b_fam}


def _sha_matches(candidate: str | None, target_sha: str) -> bool:
    if not candidate or not target_sha:
        return False
    c = candidate.strip().lower()
    t = target_sha.strip().lower()
    if len(c) < 12 or len(t) < 12:
        return False
    if not (_HEX_SHA_RE.match(c) and _HEX_SHA_RE.match(t)):
        return False
    # 64-character SHA-256 archive/artifact digests require an exact 64-char match,
    # never a short commit abbreviation prefix match.
    if len(t) == 64 or len(c) == 64:
        return len(c) == 64 and len(t) == 64 and c == t
    if len(c) > 40 or len(t) > 40:
        return c == t
    return c == t or t.startswith(c) or c.startswith(t)


def _verdict_matches_target(parsed: dict, target_sha: str) -> bool:
    """Check whether a parsed verdict binds to `target_sha`.

    - When `target_sha` is a 64-char SHA-256 archive digest, requires an exact 64-char digest
      match (never accepting a shorter commit abbreviation in `HEAD`).
    - When `target_sha` is a git commit SHA-1 (`12..40` hex chars), matches against the commit
      `HEAD` (or unstructured `cited_shas` of commit length), never against `TAR_SHA256` /
      `ARTIFACT_SHA256`.
    """
    t = (target_sha or "").strip().lower()
    if len(t) < 12 or not _HEX_SHA_RE.match(t):
        return False

    head_val = parsed.get("head")
    head_norm = head_val.strip().lower() if isinstance(head_val, str) and head_val.strip() else None

    artifact_candidates: list[str] = []
    for field in ("tar_sha256", "artifact_sha256"):
        val = parsed.get(field)
        if isinstance(val, str) and val.strip():
            tok = val.strip().lower()
            if tok not in artifact_candidates:
                artifact_candidates.append(tok)

    structured = bool(parsed.get("structured") or head_norm or artifact_candidates)

    if len(t) == 64:
        if structured:
            candidates = list(artifact_candidates)
            if head_norm and len(head_norm) == 64 and head_norm not in candidates:
                candidates.append(head_norm)
            return any(len(c) == 64 and _HEX64_RE.match(c) and c == t for c in candidates)
        return any(len(c) == 64 and _HEX64_RE.match(c) and c == t for c in (parsed.get("cited_shas") or []))

    if structured:
        if not head_norm or len(head_norm) > 40:
            return False
        return _sha_matches(head_norm, t)

    return any(12 <= len(c) <= 40 and _sha_matches(c, t) for c in (parsed.get("cited_shas") or []))


def check_head_is_new(
    item_id: str,
    head_sha: str,
    criterion_digest: str | None,
    prior_verdicts: list[dict],
) -> dict:
    """Enforce Pullboard's `headIsNew` (`src/board.js:1270-1280`): if the latest verdict for `item_id`
    was a rejection (`FIX FIRST`, `NO-GO`, `REVISE`, `REJECT`) at the same `head_sha` and the
    `criterion_digest` has not changed, refuse resubmission with `HEAD_NOT_NEW`.
    """
    if not prior_verdicts:
        return {"ok": True, "code": None, "reason": None}

    relevant = [
        v for v in prior_verdicts
        if isinstance(v, dict) and (not item_id or str(v.get("item") or "") == str(item_id))
    ]
    if not relevant:
        return {"ok": True, "code": None, "reason": None}

    latest = relevant[-1]
    decision = _normalize_decision(str(latest.get("decision") or latest.get("verdict") or ""))
    if decision not in NEGATIVE_DECISIONS and decision != "REJECT":
        return {"ok": True, "code": None, "reason": None}

    prev_head = str(latest.get("head") or latest.get("commit") or "")
    prev_digest = latest.get("criterion_digest") or latest.get("digest")
    if _sha_matches(prev_head, head_sha):
        if criterion_digest is None or prev_digest is None or str(criterion_digest) == str(prev_digest):
            return {
                "ok": False,
                "code": "HEAD_NOT_NEW",
                "reason": (
                    f"item {item_id!r} was already rejected ({decision}) at head {head_sha}; "
                    "push a new fix commit or refreeze the criterion before requesting re-review"
                ),
            }
    return {"ok": True, "code": None, "reason": None}


def _verdict_target_shas(parsed: dict) -> list[str]:
    """Return the commit/artifact SHAs that a parsed verdict actually binds to.

    When a structured verdict specifies explicit `head`, `tar_sha256`, or `artifact_sha256` fields,
    ONLY those explicit valid hex header fields are returned (never arbitrary hex tokens mentioned in
    the body). Unstructured verdicts without explicit header fields return `[]` so an unstructured
    `NO-GO` mentioning random hex words in prose never gets misclassified as `STALE_FIX_FIRST`.
    """
    if not parsed.get("structured"):
        return []
    raw_fields = parsed.get("raw_fields") or {}
    explicit: list[str] = []
    for field in ("HEAD", "COMMIT", "TAR_SHA256", "ARTIFACT_SHA256", "SHA256"):
        val = raw_fields.get(field)
        if isinstance(val, str) and val.strip():
            tok = val.strip().lower()
            if tok not in _FORBIDDEN_HEAD_TOKENS and _HEX_SHA_RE.match(tok) and tok not in explicit:
                explicit.append(tok)
    return explicit


def _parse_timestamp_key(raw_ts: object) -> float | None:
    if isinstance(raw_ts, (int, float)) and not isinstance(raw_ts, bool):
        return float(raw_ts)
    if isinstance(raw_ts, str) and raw_ts.strip():
        s = raw_ts.strip()
        try:
            dt = datetime.fromisoformat(s[:-1] + "+00:00" if s.endswith("Z") else s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            return None
    return None


def _order_messages_chronologically(messages: list[dict]) -> list[dict]:
    """Order messages from oldest to newest when `recorded_at` / `created_at` timestamps are present.

    When timestamps are present on any message, untimestamped (or unparseable-timestamp) messages
    sort before timestamped messages so an untimestamped GO can never overwrite a timestamped verdict,
    and timestamped messages sort chronologically by `(ts, idx)`.
    """
    indexed: list[tuple[int, float | None, int, dict]] = []
    any_ts = False
    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        ts = _parse_timestamp_key(msg.get("recorded_at") or msg.get("created_at"))
        if ts is not None:
            any_ts = True
        indexed.append((1 if ts is not None else 0, ts, idx, msg))
    if not any_ts:
        return [m for _, _, _, m in indexed]
    indexed.sort(key=lambda item: (item[0], item[1] or 0.0, item[2]))
    return [m for _, _, _, m in indexed]


def evaluate_quorum(
    messages: list[dict],
    *,
    target_sha: str,
    builder: str | None = None,
    builder_family: str | None = None,
    required_seats: list[str] | None = None,
    required_families: set[str] | list[str] | None = None,
    min_distinct_families: int = 1,
    creator_map: dict[str, str] | None = None,
    require_cross_family: bool = False,
    ruling_cards: list[dict] | None = None,
    require_operator_ruling: bool = False,
    expected_peers: dict[str, str] | None = None,
) -> dict:
    """Evaluate signed mesh review messages on a thread against `target_sha`.

    Rules:
    1. A seat's standing for `target_sha` is determined by its latest structured `VERDICT:` header
       for that target (or explicit `VERDICT:` header line).
    2. Unstructured prose never creates a block on a third seat. One conservative exception: a later
       unstructured message from a seat that currently stands at `VERIFIED_GO`, containing any
       negative token anywhere (`NO-GO`, `NO GO`, `FIX FIRST`, `REJECT`, `BLOCK`, `VETO`,
       `"do not merge"`, `HOLD`), drops that seat to `HOLD` until it posts a new structured header.
    3. Quorum looks only at the seats that count (`required_seats` when specified, otherwise verifier
       seats that posted a verdict header; `builder` is excluded by `SELF_VERIFY`).
    4. In `--messages-file` inputs where any message carries a timestamp, untimestamped messages sort
       before timestamped ones; an untimestamped block latches across timestamped messages only when
       explicitly bound to `target_sha` (or when no message in the list has a timestamp).
    """
    target = target_sha.strip().lower()
    seat_latest: dict[str, dict] = {}
    seat_unverifiable_block: dict[str, dict] = {}
    seat_display: dict[str, str] = {}
    standing_refusals: list[dict] = []
    expected_peers_folded = {
        k.strip().casefold(): str(v).strip().casefold()
        for k, v in (expected_peers or {}).items()
        if k and v
    }

    ordered_messages = _order_messages_chronologically(messages)
    req_seats = [s.strip() for s in (required_seats or []) if s and s.strip()]
    req_folded = {s.casefold() for s in req_seats}
    any_ts = any(
        _parse_timestamp_key(m.get("recorded_at") or m.get("created_at")) is not None
        for m in ordered_messages
    )
    for msg in ordered_messages:
        author = str(msg.get("author") or msg.get("from") or msg.get("seat") or "").strip()
        body = str(msg.get("body") or msg.get("text") or "")
        if not author or not body:
            continue

        folded = author.casefold()
        parsed = parse_verdict_header(body)
        prose_withdrawal = False
        if parsed is None:
            if (
                (not req_folded or folded in req_folded)
                and folded in seat_latest
                and seat_latest[folded]["status"] == "VERIFIED_GO"
                and _has_prose_negative_token(body)
            ):
                prose_withdrawal = True
                parsed = {
                    "decision": "HOLD",
                    "structured": False,
                    "item": None,
                    "head": None,
                    "tar_sha256": None,
                    "artifact_sha256": None,
                    "family": None,
                    "p1": None,
                    "p2": None,
                    "reason": "PROSE_NEGATIVE_WITHDRAWAL",
                    "cause": None,
                    "causes": [],
                    "next": None,
                    "criterion_digest": None,
                    "trunk": None,
                    "cited_shas": [],
                    "raw_fields": {},
                    "duplicate_fields": [],
                }
            else:
                continue

        decision = parsed["decision"]
        roster_fam = _seat_roster_family(author)
        header_fam = parsed.get("family")
        msg_fam = msg.get("author_family")
        claimed_fam = (
            header_fam.strip()
            if isinstance(header_fam, str) and header_fam.strip()
            else (msg_fam.strip() if isinstance(msg_fam, str) and msg_fam.strip() else None)
        )
        if claimed_fam is not None and claimed_fam.lower() != roster_fam:
            standing_refusals.append({
                "seat": author,
                "code": "FAMILY_SPOOF",
                "reason": f"seat {author} ({roster_fam}) cannot claim family {claimed_fam!r}",
                "decision": decision,
                "key": msg.get("key"),
            })
            continue

        standing = check_verifier_standing(
            author,
            builder,
            creator_map=creator_map,
            require_cross_family=require_cross_family,
            verifier_family=None,
            builder_family=builder_family,
        )
        if not standing["ok"]:
            standing_refusals.append({
                "seat": author,
                "code": standing["code"],
                "reason": standing["reason"],
                "decision": decision,
                "key": msg.get("key"),
            })
            continue

        sig_ok = (
            msg.get("signature_verified") is True
            and ("status_verified" not in msg or msg.get("status_verified") is True)
        )
        signer = msg.get("signed_by")
        signer_ok = True
        if expected_peers_folded:
            if author.casefold() in expected_peers_folded:
                signer_ok = (
                    signer is not None
                    and str(signer).strip().casefold() == expected_peers_folded[author.casefold()]
                )
            elif signer is not None:
                signer_str = str(signer).strip()
                signer_ok = bool(_UUID_PEER_ID_RE.match(signer_str)) or signer_str.casefold() == author.casefold()
        elif signer is not None:
            signer_str = str(signer).strip()
            # `node.read` stamps `signed_by` with the signer's peer UUID (`node.py:1080-1086`);
            # a non-UUID `signed_by` string is a seat/label claim and must match `author`.
            signer_ok = bool(_UUID_PEER_ID_RE.match(signer_str)) or signer_str.casefold() == author.casefold()

        if not sig_ok or not signer_ok:
            standing_refusals.append({
                "seat": author,
                "code": "UNVERIFIED_SIGNATURE" if not sig_ok else "SIGNER_MISMATCH",
                "reason": (
                    "message signature_verified is not True"
                    if not sig_ok
                    else f"signed_by {signer!r} does not match author {author!r}"
                ),
                "decision": decision,
                "key": msg.get("key"),
            })

        problems = validate_verdict(parsed)
        bound_shas = _verdict_target_shas(parsed)
        cited = list(parsed.get("cited_shas") or [])
        matches_target = _verdict_matches_target(parsed, target)

        if decision in POSITIVE_DECISIONS:
            if problems:
                status = "INVALID_VERDICT"
            elif not (sig_ok and signer_ok):
                status = "UNVERIFIED_SIGNATURE"
            elif matches_target:
                status = "VERIFIED_GO"
            else:
                status = "STALE_GO"
        elif decision in NEGATIVE_DECISIONS:
            if problems and not matches_target and bound_shas:
                status = "BLOCKED_FIX_FIRST"
            elif matches_target or not bound_shas or problems:
                status = "BLOCKED_FIX_FIRST"
            else:
                status = "STALE_FIX_FIRST"
        elif decision == "HOLD":
            status = "HOLD"
        else:
            status = "INVALID_VERDICT"

        folded = author.casefold()
        msg_has_ts = _parse_timestamp_key(msg.get("recorded_at") or msg.get("created_at")) is not None
        # An unauthenticated message must never clear an existing blocking or authenticated verdict
        if not (sig_ok and signer_ok) and folded in seat_latest:
            prev_status = seat_latest[folded]["status"]
            if prev_status in ("BLOCKED_FIX_FIRST", "HOLD", "INVALID_VERDICT"):
                continue
            if (
                seat_latest[folded].get("authenticated") is True
                and status not in ("BLOCKED_FIX_FIRST", "HOLD", "INVALID_VERDICT")
            ):
                continue

        entry = {
            "seat": author,
            "family": standing["verifier_family"],
            "status": status,
            "decision": decision,
            "head": parsed.get("head"),
            "artifact_sha256": parsed.get("artifact_sha256"),
            "bound_shas": bound_shas,
            "cited_shas": cited,
            "matches_target": matches_target,
            "problems": problems,
            "reason": parsed.get("reason"),
            "causes": parsed.get("causes", []),
            "key": msg.get("key"),
            "signature_verified": msg.get("signature_verified"),
            "status_verified": msg.get("status_verified"),
            "signed_by": msg.get("signed_by"),
            "authenticated": bool(sig_ok and signer_ok),
            "has_timestamp": msg_has_ts,
            "prose_withdrawal": prose_withdrawal,
        }
        if (
            not prose_withdrawal
            and not msg_has_ts
            and status in ("BLOCKED_FIX_FIRST", "HOLD", "INVALID_VERDICT")
            and (matches_target or not any_ts)
        ):
            seat_unverifiable_block[folded] = entry

        # An untimestamped blocking verdict has unverifiable relative time order against every other message
        # from the same seat, so neither a later positive verdict nor an intermediate HOLD / STALE_FIX_FIRST
        # may clear it; and a verdict bound to a different SHA (STALE_GO / STALE_FIX_FIRST) must never clear
        # an active BLOCKED_FIX_FIRST, HOLD, or INVALID_VERDICT on the target.
        if status in ("VERIFIED_GO", "STALE_GO", "STALE_FIX_FIRST"):
            if folded in seat_unverifiable_block:
                seat_display[folded] = author
                seat_latest[folded] = seat_unverifiable_block[folded]
                continue
            if (
                status in ("STALE_GO", "STALE_FIX_FIRST")
                and folded in seat_latest
                and seat_latest[folded]["status"] in ("BLOCKED_FIX_FIRST", "HOLD", "INVALID_VERDICT")
            ):
                continue

        seat_display[folded] = author
        seat_latest[folded] = entry

    for req in req_seats:
        f_req = req.casefold()
        if f_req not in seat_latest:
            seat_display[f_req] = req
            seat_latest[f_req] = {
                "seat": req,
                "family": resolve_family(req),
                "status": "MISSING",
                "decision": None,
                "head": None,
                "artifact_sha256": None,
                "cited_shas": [],
                "matches_target": False,
                "problems": [],
                "reason": None,
                "causes": [],
                "key": None,
            }

    counted_infos = [
        info for k, info in seat_latest.items() if not req_folded or k in req_folded
    ]

    verified_go_seats = sorted(
        info["seat"] for info in counted_infos if info["status"] == "VERIFIED_GO"
    )
    stale_go_seats = sorted(
        info["seat"] for info in counted_infos if info["status"] == "STALE_GO"
    )
    blocked_seats = sorted(
        info["seat"]
        for k, info in seat_latest.items()
        if info["status"] in ("BLOCKED_FIX_FIRST", "HOLD", "INVALID_VERDICT")
        and (not req_folded or k in req_folded or not info.get("prose_withdrawal"))
    )
    missing_seats = sorted(
        info["seat"] for info in counted_infos if info["status"] == "MISSING"
    )

    verified_families_set = {
        info["family"]
        for info in counted_infos
        if info["status"] == "VERIFIED_GO" and info["family"] != "unknown"
    }
    b_fam_resolved = resolve_family(builder, builder_family) if builder else "unknown"
    all_participating_families = set(verified_families_set)
    if b_fam_resolved != "unknown" and verified_go_seats:
        all_participating_families.add(b_fam_resolved)

    req_fams = {f.strip().lower() for f in (required_families or []) if f and f.strip()}
    missing_families = sorted(req_fams - verified_families_set)

    operator_ruling_ok = True
    if require_operator_ruling:
        operator_ruling_ok = any(
            isinstance(c, dict)
            and c.get("type") == "Ruling"
            and c.get("owner") == "operator"
            and c.get("status", "active") == "active"
            for c in (ruling_cards or [])
        )

    seats_ok = all(
        seat_latest[s.casefold()]["status"] == "VERIFIED_GO" for s in req_seats
    )
    families_ok = (
        len(missing_families) == 0
        and len(all_participating_families if builder else verified_families_set) >= min_distinct_families
    )
    all_statuses_allowed = all(
        info["status"] in ("VERIFIED_GO", "STALE_GO", "STALE_FIX_FIRST")
        for info in counted_infos
    )
    quorum_met = (
        bool(verified_go_seats)
        and seats_ok
        and families_ok
        and len(blocked_seats) == 0
        and len(missing_seats) == 0
        and all_statuses_allowed
        and operator_ruling_ok
    )

    return {
        "quorum_met": quorum_met,
        "target_sha": target,
        "seats": {seat_display[k]: v for k, v in sorted(seat_latest.items())},
        "verified_go_seats": verified_go_seats,
        "verified_families": sorted(verified_families_set),
        "participating_families": sorted(all_participating_families),
        "missing_families": missing_families,
        "stale_go_seats": stale_go_seats,
        "blocked_seats": blocked_seats,
        "missing_seats": missing_seats,
        "standing_refusals": standing_refusals,
        "operator_ruling_ok": operator_ruling_ok,
    }


def _run_git(repo_path: Path | str, args: list[str]) -> subprocess.CompletedProcess:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("GIT_") or k in ("GIT_EXEC_PATH", "GIT_SSH_COMMAND")
    }
    return subprocess.run(
        ["git", "--no-replace-objects", "-c", "core.quotepath=false", *args],
        cwd=os.fspath(repo_path),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _norm_rel_path(path: str) -> str:
    """Normalize a repository-relative path: forward slashes, strip literal `./` prefixes
    and leading `/`, while preserving leading dots on dotfiles/dot-directories (e.g. `.env`,
    `.github/workflows/ci.yml`).
    """
    norm = str(path).strip().replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    norm = norm.lstrip("/")
    while norm.startswith("./"):
        norm = norm[2:]
    return norm


def capture_verdict_review(
    repo_path: Path | str,
    *,
    item_id: str,
    head_sha: str,
    trunk_sha: str | None = None,
    base_sha: str | None = None,
    changed_paths: list[str] | None = None,
) -> dict:
    """Seal a verdict review snapshot `{item, commit, head, trunk, paths}` (Pullboard
    `src/verdict-freshness.js:33-54`).
    """
    if changed_paths is not None and trunk_sha is not None:
        norm_paths = sorted({_norm_rel_path(p) for p in changed_paths if p and _norm_rel_path(p)})
        return {
            "item": item_id,
            "commit": head_sha,
            "head": head_sha,
            "trunk": trunk_sha,
            "paths": norm_paths,
        }

    if trunk_sha is None:
        for candidate_ref in ("refs/remotes/origin/main", "refs/heads/main", "HEAD"):
            res = _run_git(repo_path, ["rev-parse", "--verify", f"{candidate_ref}^{{commit}}"])
            if res.returncode == 0 and res.stdout.strip():
                trunk_sha = res.stdout.strip()
                break
        if not trunk_sha:
            raise ValueError("BAD_VERDICT_REVIEW: unable to resolve trunk commit in repository")

    head_res = _run_git(repo_path, ["rev-parse", "--verify", f"{head_sha}^{{commit}}"])
    if head_res.returncode != 0:
        raise ValueError(f"BAD_VERDICT_REVIEW: head commit {head_sha!r} is unavailable")
    resolved_head = head_res.stdout.strip()

    base = base_sha or trunk_sha
    mb_res = _run_git(repo_path, ["merge-base", base, resolved_head])
    if mb_res.returncode != 0 or not mb_res.stdout.strip():
        raise ValueError(f"BAD_VERDICT_REVIEW: merge-base between {base} and {resolved_head} is unavailable")
    ancestor = mb_res.stdout.strip()

    diff_res = _run_git(
        repo_path,
        ["diff", "--no-renames", "--name-only", "-z", ancestor, resolved_head, "--"],
    )
    if diff_res.returncode != 0:
        raise ValueError("BAD_VERDICT_REVIEW: unable to read changed paths")
    paths = sorted({_norm_rel_path(p) for p in diff_res.stdout.split("\0") if p and _norm_rel_path(p)})
    return {
        "item": item_id,
        "commit": resolved_head,
        "head": resolved_head,
        "trunk": trunk_sha,
        "paths": paths,
    }


def project_verdict_freshness(
    repo_path: Path | str | None,
    review: dict,
    *,
    tip_sha: str | None = None,
    current_head_sha: str | None = None,
    main_touched_paths: list[str] | set[str] | None = None,
) -> dict:
    """Project whether a sealed `CODE GO` review remains fresh after `main` advances
    (Pullboard `src/verdict-freshness.js:67-156`).

    - If `current_head_sha` differs from `review["head"]`, returns `{"stale": True, "reason": "HEAD_MOVED", "files": []}`.
    - If `tip_sha == review["trunk"]`, returns `{"stale": False, "reason": "TRUNK_UNCHANGED", "files": []}`.
    - Otherwise intersects `main_touched_paths` along `trunk..tip` with `review["paths"]`:
      - Empty overlap -> `{"stale": False, "reason": "DISJOINT_PATHS", "files": []}`.
      - Non-empty overlap -> `{"stale": True, "reason": "STALE_VERDICT_PATH_OVERLAP", "files": overlap}`.
    """
    if not isinstance(review, dict) or not review.get("head") or not review.get("trunk"):
        return {"stale": True, "reason": "BAD_VERDICT_REVIEW", "files": []}

    reviewed_head = str(review["head"])
    reviewed_trunk = str(review["trunk"])
    review_paths = [_norm_rel_path(str(p)) for p in review.get("paths", []) if p and _norm_rel_path(str(p))]

    if current_head_sha is not None and not (
        current_head_sha.strip() == reviewed_head.strip() or _sha_matches(current_head_sha, reviewed_head)
    ):
        return {"stale": True, "reason": "HEAD_MOVED", "files": []}

    if tip_sha is None and repo_path is not None:
        for candidate_ref in ("refs/remotes/origin/main", "refs/heads/main", "HEAD"):
            res = _run_git(repo_path, ["rev-parse", "--verify", f"{candidate_ref}^{{commit}}"])
            if res.returncode == 0 and res.stdout.strip():
                tip_sha = res.stdout.strip()
                break

    if tip_sha is not None and (
        tip_sha.strip() == reviewed_trunk.strip() or _sha_matches(tip_sha, reviewed_trunk)
    ):
        return {"stale": False, "reason": "TRUNK_UNCHANGED", "files": []}

    if main_touched_paths is None:
        if repo_path is None or tip_sha is None:
            return {"stale": True, "reason": "GIT_HISTORY_UNAVAILABLE", "files": []}
        anc = _run_git(repo_path, ["merge-base", "--is-ancestor", reviewed_trunk, tip_sha])
        if anc.returncode != 0:
            return {"stale": True, "reason": "TRUNK_NOT_ANCESTOR", "files": []}
        diff_res = _run_git(
            repo_path,
            ["diff", "--no-renames", "--name-only", "-z", reviewed_trunk, tip_sha, "--"],
        )
        if diff_res.returncode != 0:
            return {"stale": True, "reason": "GIT_HISTORY_UNAVAILABLE", "files": []}
        touched_set = {_norm_rel_path(p) for p in diff_res.stdout.split("\0") if p and _norm_rel_path(p)}
    else:
        touched_set = {_norm_rel_path(str(p)) for p in main_touched_paths if p and _norm_rel_path(str(p))}

    overlap = sorted(p for p in review_paths if p in touched_set)
    if not overlap:
        return {"stale": False, "reason": "DISJOINT_PATHS", "files": []}
    return {"stale": True, "reason": "STALE_VERDICT_PATH_OVERLAP", "files": overlap}


def check_merge_conflicts(repo_path: Path | str, trunk_sha: str, head_sha: str) -> dict:
    """Run in-memory `git merge-tree --write-tree --name-only -z <trunk> <head>` without touching
    the working tree or index (Pullboard `src/trusted-policy.js:98-112` and `src/next-review.js:19-37`).
    """
    res = _run_git(
        repo_path,
        ["merge-tree", "--write-tree", "--name-only", "-z", trunk_sha, head_sha],
    )
    if res.returncode == 0:
        tree_oid = res.stdout.split("\0", 1)[0].strip()
        return {"clean": True, "conflicting_files": [], "tree_oid": tree_oid}
    if res.returncode == 1:
        records = res.stdout.split("\0")[1:]
        if "" in records:
            end = records.index("")
            records = records[:end]
        files = sorted({r for r in records if r})
        return {"clean": False, "conflicting_files": files, "tree_oid": None}
    raise ValueError(f"MERGE_CHECK_FAILED: git merge-tree exited with {res.returncode}")


def normalize_lane_prefix(prefix: str) -> str:
    """Normalize a lane path prefix (forward slashes, no leading `./`, refuses `/` or `..`)."""
    cleaned = str(prefix).strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    if not cleaned or cleaned.startswith("/") or ".." in cleaned.split("/"):
        raise ValueError(f"BAD_LANE_PATH: {prefix!r} is not a valid relative lane prefix")
    return cleaned


def _lane_prefixes(lane_spec: list[str] | dict) -> list[str]:
    if isinstance(lane_spec, dict):
        raw = lane_spec.get("owns", [])
    elif isinstance(lane_spec, (list, tuple, set)):
        raw = list(lane_spec)
    else:
        raw = []
    return [normalize_lane_prefix(p) for p in raw if p]


def file_lane(
    rel_path: str,
    lanes: dict[str, list[str] | dict],
    *,
    default_owner: str = "coordinator",
) -> str:
    """Resolve the lane that owns `rel_path` using Pullboard's longest-matching-prefix rule
    (`src/lanes.js:49-61`).
    """
    norm = _norm_rel_path(rel_path)
    owner = default_owner
    longest = 0
    for name, spec in lanes.items():
        for prefix in _lane_prefixes(spec):
            # A prefix without trailing slash matches exact file or directory boundary
            matches = (
                norm.startswith(prefix)
                if prefix.endswith("/")
                else (norm == prefix or norm.startswith(prefix + "/"))
            )
            if matches and len(prefix) > longest:
                owner = name
                longest = len(prefix)
    return owner


def out_of_lane(
    rel_paths: list[str],
    lane: str,
    lanes: dict[str, list[str] | dict],
    *,
    shared: list[str] | None = None,
    coordinator: str = "coordinator",
) -> list[str]:
    """Return paths in `rel_paths` that `lane` is not permitted to modify (`src/lanes.js:72-78`)."""
    if lane == coordinator:
        return []
    shared_prefixes = [normalize_lane_prefix(s) for s in (shared or []) if s]

    def _is_shared(norm_p: str) -> bool:
        for sp in shared_prefixes:
            if sp.endswith("/") and norm_p.startswith(sp):
                return True
            if not sp.endswith("/") and (norm_p == sp or norm_p.startswith(sp + "/")):
                return True
        return False

    violations: list[str] = []
    for p in rel_paths:
        norm = _norm_rel_path(p)
        if _is_shared(norm):
            continue
        owner = file_lane(norm, lanes, default_owner=coordinator)
        if owner != lane:
            violations.append(f"{norm} ({owner}'s)")
    return violations


def check_lane_overlaps(
    branch_files: dict[str, list[str]],
    lanes: dict[str, list[str] | dict] | None = None,
    *,
    approved_overlaps: set[tuple[str, str]] | None = None,
    default_owner: str = "coordinator",
) -> dict:
    """Detect unapproved file and lane overlaps across open branches/PRs."""
    lanes = lanes or {}
    approved: set[tuple[str, str]] = set()
    for a, b in approved_overlaps or set():
        approved.add((a, b))
        approved.add((b, a))

    branches = sorted(branch_files)
    norm_branch_files: dict[str, set[str]] = {
        b: {_norm_rel_path(p) for p in branch_files[b] if p and _norm_rel_path(p)}
        for b in branches
    }

    file_overlaps: list[dict] = []
    lane_overlaps: list[dict] = []

    for i, b1 in enumerate(branches):
        for b2 in branches[i + 1 :]:
            if (b1, b2) in approved:
                continue
            shared_files = sorted(norm_branch_files[b1] & norm_branch_files[b2])
            if shared_files:
                file_overlaps.append({"branches": [b1, b2], "files": shared_files})
            if lanes:
                lanes_1 = {
                    file_lane(p, lanes, default_owner=default_owner)
                    for p in norm_branch_files[b1]
                } - {default_owner}
                lanes_2 = {
                    file_lane(p, lanes, default_owner=default_owner)
                    for p in norm_branch_files[b2]
                } - {default_owner}
                shared_lanes = sorted(lanes_1 & lanes_2)
                if shared_lanes:
                    lane_overlaps.append({"branches": [b1, b2], "lanes": shared_lanes})

    return {
        "clean": len(file_overlaps) == 0 and len(lane_overlaps) == 0,
        "file_overlaps": file_overlaps,
        "lane_overlaps": lane_overlaps,
    }
