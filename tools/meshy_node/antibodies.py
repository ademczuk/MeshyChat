"""Clean-room pure-stdlib error-immunity engine for MeshyChat fleet seats.

Implements five core algorithms adapted from Shyboy0499/antibody (MIT):
1. Ordered noise normalization, path collapsing, 12-hex fingerprinting, and pre-truncation redaction.
2. Three-tier error matching (Tier 1 exact signature, Tier 2 fuzzy Jaccard, Tier 3 short-message error code).
3. Display-safe failure and test-runner verdict classification without PreToolUse Bash DEBUG traps.
4. Per-seat FixTrust tracking (DOUBT_AFTER = 1, SUPPRESS_AFTER = 2) with fix_sig change reset.
5. Turn-scoped ResolutionTracker (succeeded + command-only moved_past) and box-local ClaimsStore (CLAIM_TTL_S = 600).

CLI usage (for PostToolUse hooks, seat wrappers, and transcript audits):
    python -m tools.meshy_node.antibodies fingerprint --category pytest --message "..."
    python -m tools.meshy_node.antibodies classify --command "git show missing | cat" --output "fatal: bad revision"
    python -m tools.meshy_node.antibodies scan-transcript --transcript session.jsonl --catalog antibodies.json
"""
from __future__ import annotations

import argparse
import contextlib
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Any, Iterable, Iterator

# ---- Constants

FUZZY_JACCARD_THRESHOLD = 0.60
SHORT_MESSAGE_MAX_CHARS = 80
DEFAULT_SAMPLE_MAX_CHARS = 280

DOUBT_AFTER = 1
SUPPRESS_AFTER = 2

RESOLUTION_WINDOW_TURNS = 1
CLAIM_TTL_S = 600

# ---- Normalization & Redaction Regexes

ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
ANSI_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
HEX_HASH_RE = re.compile(r"\b[0-9a-fA-F]{32,}\b")

ISO_TIMESTAMP_RE = re.compile(
    r"\b(?:19|20)\d{2}(?:-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?|"
    r"(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01]))\b|"
    r"(?=(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01]))(?:19|20)\d{6}"
)
DURATION_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:ms|s)\b")
PID_RE = re.compile(r"\bpid(?:\s+|[=:])\d+\b", re.IGNORECASE)
HOST_PORT_RE = re.compile(
    r"(?<![/\\A-Za-z0-9_.\-])\b(?:localhost|(?:\d{1,3}\.){3}\d{1,3}|"
    r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.(?!py|ts|js|mjs|cjs|jsx|tsx|rs|go|sh|ps1|rb|java|kt|c|cc|cpp|h|hpp|json|ya?ml|toml|md|txt|sql|xml|html|css|ini|cfg|lock\b)[A-Za-z]{2,10}"
    r")(?=:)"
    r":\d{2,5}\b(?!:\d)"
)
LINE_COL_RE = re.compile(r":\d+(?::\d+)?\b")
LINE_WORD_RE = re.compile(r"\bline\s+\d+(?:,\s*column\s+\d+)?\b", re.IGNORECASE)

WIN_TEMP_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s\"':<>|]*?AppData[\\/]Local[\\/]Temp(?:[\\/][^\s\"':<>|]+)?",
    re.IGNORECASE,
)
UNIX_TEMP_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_.\-:<])(?:/private)?(?:/tmp|/var/folders)(?:/[A-Za-z0-9_.\-]+)+"
)
TEMP_SEGMENT_RE = re.compile(r"\btmp-[A-Za-z0-9_.\-]+")

WIN_ABS_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]:[\\/](?:[^\s\"':<>|/\\]+[\\/])*([^\s\"':<>|/\\]+)"
)
UNIX_ABS_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_.\-:<])/(?:[A-Za-z0-9_.\-]+/)+([A-Za-z0-9_.\-]+)"
)

# Secret redaction patterns (applied after path collapsing and before length truncation)
URL_CRED_RE = re.compile(
    r"(?<![A-Za-z0-9+.\-])([A-Za-z][A-Za-z0-9+.\-]*://)"
    r"([^\s/?#:]*:[^\s/?#]+|(?!(?:git|anonymous)@)[A-Za-z0-9_.\-~%+]{4,})@"
)
BEARER_RE = re.compile(
    r"\b((?:Authorization\s*:\s*(?:Bearer|Basic|Token|Digest|Negotiate|AWS4-HMAC-SHA256)|Bearer|Basic)\s+)"
    r"(?!<redacted>\b|header\b|required\b|missing\b|invalid\b|auth(?:entication)?\b|token\b)"
    r"[A-Za-z0-9._\-+/=]+",
    re.IGNORECASE,
)
COOKIE_HEADER_RE = re.compile(r"(?i)\b((?:Set-)?Cookie[ \t]*:[ \t]*)([^\r\n]+)")
_COOKIE_PAIR_RE = re.compile(r"(?<![A-Za-z0-9_\-])([A-Za-z0-9_\-]++=)(?!<redacted>)([^\s;\"',]+)")
SECRET_PREFIX_RE = re.compile(
    r"\b(?:sk[-_][A-Za-z0-9_\-]{8,}|rk[-_][A-Za-z0-9_\-]{8,}|whsec_[A-Za-z0-9_\-]{8,}"
    r"|gh[pousr]_[A-Za-z0-9]{12,}|github_pat_[A-Za-z0-9_]{12,}|glpat-[A-Za-z0-9_\-]{8,}"
    r"|xox[baprs]-[A-Za-z0-9\-]{10,}|(?:AKIA|ASIA)[0-9A-Z]{16}|AIza[0-9A-Za-z_\-]{20,}"
    r"|eyJ[A-Za-z0-9_\-]{5,}\.eyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]+)\b"
)
CLI_FLAG_SECRET_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_\-])("
    r"--(?:[A-Za-z0-9_\-]*(?:password|passwd|passphrase|pwd|secret|token|key|cred(?:ential)?s?))(?:[ \t]+|=)"
    r"|(?:--user|-u)(?:[ \t]+|=)[\"']?[^\s:'\"]+:"
    r"|\blogin[ \t]+\S+[ \t]+(?:password|passwd|account)[ \t]+"
    r"|\bmysql\w*(?:[ \t]+\S+)*?[ \t]+-p(?![ \t]|=)"
    r")"
    r"(?!<redacted>)(?![\-])([^\s\"']+)"
)
_BENIGN_KEY_EXCLUSION = (
    r"(?!(?:primary|foreign|cache|sort|partition|index|routing|group|hash|lookup|"
    r"natural|surrogate|composite|candidate|unique|public|pub|ssh[_-]?public|host|"
    r"max|min|num|total)[_-]?keys?(?![A-Za-z0-9_\-]))"
    r"(?!(?:max|min|num|total|input|output|prompt|completion|cached|reasoning)[_-]?tokens?(?![A-Za-z0-9_\-]))"
    r"(?!token[_-]?(?:count|limit|length|type|id|ids|izer)(?![A-Za-z0-9_\-]))"
    r"(?!tokenizer(?![A-Za-z0-9_\-]))"
)
_BENIGN_KEY_SUFFIX_LOOKBEHIND = (
    r"(?<!_name)(?<!_names)(?<!_count)(?<!_counts)(?<!_len)(?<!_length)(?<!_size)"
    r"(?<!_type)(?<!_types)(?<!_format)(?<!_path)(?<!_paths)(?<!_file)(?<!_dir)"
    r"(?<!_url)(?<!_uri)(?<!_list)(?<!_map)(?<!_set)(?<!_enabled)(?<!_disabled)"
    r"(?<!_required)(?<!_status)(?<!_state)(?<!_mode)(?<!_level)(?<!_limit)"
    r"(?<!_max)(?<!_min)(?<!_ttl)(?<!_timeout)(?<!_age)"
)
_SECRET_CORE = (
    r"(?:password|passwd|passphrase|pgpassword|pgpwd|pwd|secret|"
    r"token(?!s(?![A-Za-z0-9])|izer|ize[d]?|izing)|"
    r"session[_-]?id|credential|credentials|creds|cookie|"
    r"apikey|passkey|secretkey|privkey|privatekey|authkey|accesskey|"
    r"masterkey|signingkey|clientkey|hmackey|webhookkey|servicekey|sessionkey|licensekey|"
    r"(?:(?<=[_-])key|(?-i:(?<=[a-z0-9])Key))(?![sS](?![A-Za-z0-9]))|"
    r"(?<=[_-])auth(?![A-Za-z0-9_\-])|"
    r"(?<![A-Za-z0-9_\-])(?:key|auth)(?![A-Za-z0-9_\-]))"
)
_SECRET_KEY_TOKEN = (
    r"(?<![A-Za-z0-9_\-])"
    r"(?=[A-Za-z0-9_\-]{1,64}+(?![A-Za-z0-9_\-])(?:\\?[\"'])?\s*[=:])"
    r"(?!(?:set-)?cookie[ \t]*:)(?!authorization[ \t]*:)"
    + _BENIGN_KEY_EXCLUSION
    + r"([A-Za-z0-9_\-]*?" + _SECRET_CORE + r"[A-Za-z0-9_\-]*)"
    + _BENIGN_KEY_SUFFIX_LOOKBEHIND
    + r"(?![A-Za-z0-9_\-])"
)
_BENIGN_UNQUOTED_VALUE_LOOKAHEAD = (
    r"(?!(?:true|false|none|null|nil|undefined|return|pass|break|continue|raise|"
    r"required|missing|invalid|expected|optional|header)(?:[\s,;)}\\<>]|$))"
    r"(?![A-Za-z_][A-Za-z0-9_.]*\()"
)
KV_QUOTED_SECRET_RE = re.compile(
    r"(?i)" + _SECRET_KEY_TOKEN
    + r"((?:\\?[\"'])?\s*[=:]\s*(?:[fFbBrRuU]{1,2})?(\\?[\"']))"
    + r"(?!<(?:redacted|uuid|hash|ts|pid|port|pos|tmp|path)>\3)"
    + r"([^\r\n\"']{1,})"
    + r"(\3)"
)
KV_EQ_SECRET_RE = re.compile(
    r"(?i)" + _SECRET_KEY_TOKEN
    + r"(\s*=\s*(?:\\?[\"'])?)(?!<(?:redacted|uuid|hash|ts|pid|port|pos|tmp|path)>)"
    + _BENIGN_UNQUOTED_VALUE_LOOKAHEAD
    + r"([^\s\"'<>]{1,})((?:\\?[\"'])?)"
)
KV_SECRET_RE = re.compile(
    r"(?i)" + _SECRET_KEY_TOKEN
    + r"((?:\\?[\"'])?\s*:\s*(?:\\?[\"'])?)(?!<(?:redacted|uuid|hash|ts|pid|port|pos|tmp|path)>)"
    + _BENIGN_UNQUOTED_VALUE_LOOKAHEAD
    + r"([^\s\"',;}\\<>]{1,})((?:\\?[\"'])?)"
)
LONG_BASE64_RE = re.compile(r"(?<![A-Za-z0-9+/=_\-./])[A-Za-z0-9+/=_\-]{40,}(?![A-Za-z0-9+/=_\-])")


def _to_str(val: Any) -> str:
    return "" if val is None else str(val).strip()


def _first_str(*vals: Any, default: str = "") -> str:
    for v in vals:
        s = _to_str(v)
        if s:
            return s
    return default


def _none_if_empty(text: str | None) -> str | None:
    if text:
        return text
    return None


def _replace_url_cred(match: re.Match[str]) -> str:
    scheme_prefix, userinfo = match.group(1), match.group(2)
    if ":" in userinfo:
        return f"{scheme_prefix}{userinfo.split(':', 1)[0]}:<redacted>@"
    return f"{scheme_prefix}<redacted>@"


def _replace_cookie_header(match: re.Match[str]) -> str:
    return match.group(1) + _COOKIE_PAIR_RE.sub(r"\1<redacted>", match.group(2))


def _replace_long_base64(match: re.Match[str]) -> str:
    tok = match.group(0)
    if any(c.isupper() for c in tok) and any(c.islower() for c in tok) and any(c.isdigit() for c in tok):
        return "<redacted>"
    return tok


PLACEHOLDER_RE = re.compile(r"<(?:uuid|hash|ts|pid|port|pos|tmp|path|redacted)>")
TOKEN_RE = re.compile(r"[A-Za-z0-9_.\-/]{2,}")

_ERROR_CODE_FORMATTERS = (
    (re.compile(r"\b(E_[A-Z0-9_]{2,})\b"), str),
    (re.compile(r"\b(ERR_[A-Z0-9_]{2,})\b"), str),
    (
        re.compile(
            r"\b(E(?:ACCES|ADDRINUSE|ADDRNOTAVAIL|AGAIN|BADF|BUSY|CHILD|CONNABORTED|"
            r"CONNREFUSED|CONNRESET|EXIST|FAULT|FBIG|HOSTUNREACH|INTR|INVAL|IO|ISDIR|"
            r"LOOP|MFILE|MLINK|NAMETOOLONG|NETDOWN|NETUNREACH|NFILE|NOBUFS|NODEV|NOENT|"
            r"NOEXEC|NOLCK|NOMEM|NOSPC|NOSYS|NOTCONN|NOTDIR|NOTEMPTY|NOTSOCK|NOTSUP|"
            r"NXIO|OPNOTSUPP|PERM|PIPE|RANGE|ROFS|SPIPE|SRCH|TIMEDOUT|TXTBSY|WOULDBLOCK|XDEV))\b"
        ),
        str,
    ),
    (re.compile(r"\b([A-Z][A-Za-z0-9_]*(?:Error|Exception))\b"), str),
    (re.compile(r"\b(0x[0-9a-fA-F]{2,})\b"), str.lower),
    (
        re.compile(r"\b(?:HTTP(?:/\d(?:\.\d)?)?\s+|status(?:\s+code)?\s*[:=]?\s*)([45]\d{2})\b", re.IGNORECASE),
        lambda v: f"HTTP_{v}",
    ),
)
ERROR_CODE_PATTERNS = tuple(p for p, _ in _ERROR_CODE_FORMATTERS)

# ---- Shell Command & Failure Classifier Constants

SHELL_STAGE_SCAN_RE = re.compile(
    r"""'[^']*'|"(?:\\.|[^"\\])*"|\\.|(?P<op>&&|\|\||\|&|;|\n|\||(?<![>&])&(?![>&\d]))"""
)
SHELL_CLAUSE_SCAN_RE = re.compile(
    r"""'[^']*'|"(?:\\.|[^"\\])*"|\\.|(?P<op>&&|\|\||;|\n)"""
)
_STAGE_PREFIX_RE = re.compile(
    r"^\s*(?:(?:[A-Za-z_][A-Za-z0-9_]*=\S+|sudo|env|command|time)\s+)*"
    r"(?!(?:sudo|env|command|time)(?:\s|$)|[A-Za-z_][A-Za-z0-9_]*=)"
    r"[\"']?(?:[^\s\"'/\\]+[/\\])*([^\s\"'/\\]+?)(?:\.exe)?[\"']?(?=\s|$)"
    r"(?:\s+(?:(?:-C|-c|--git-dir|--work-tree)\s+\S+|-[^\s]+))*"
    r"(?:\s+([^\s\"'-][^\s\"']*))?",
    re.IGNORECASE,
)
_FIND_SIDE_EFFECT_RE = re.compile(r"(?:^|\s)-(?:exec|execdir|delete|ok|okdir)\b")
_SUBSHELL_RE = re.compile(r"\$\(|`")

DISPLAY_COMMANDS = frozenset({
    "cat", "head", "tail", "grep", "rg", "find", "diff",
    "git log", "git show", "git diff", "git grep", "git blame", "git status",
    "get-content", "select-string", "less", "more", "wc", "echo", "printf",
})

BENIGN_EXITS = frozenset({
    "grep", "rg", "diff", "test", "[", "git diff", "git grep", "select-string",
})

OWN_ERROR_RE = re.compile(
    r"^\s*(?:cat|head|tail|grep|rg|find|diff|git|Get-Content|Select-String|dir|ls)\s*:\s+.*"
    r"(?:No such file or directory|Permission denied|invalid option|unknown option|"
    r"unrecognized option|fatal:|error:|cannot open|is a directory|bad revision|Cannot find path|cannot access)",
    re.IGNORECASE | re.MULTILINE,
)
GIT_FATAL_RE = re.compile(r"^\s*fatal:\s+\S+", re.IGNORECASE | re.MULTILINE)

RUNNER_VERDICT_PATTERNS = (
    re.compile(r"^=+\s*FAILURES\s*=+$", re.MULTILINE),
    re.compile(r"^=+\s*ERRORS\s*=+$", re.MULTILINE),
    re.compile(
        r"(?:^|(?<!=)=+\s+|\b\d+\s+passed,\s+|\b,\s*)[1-9]\d*\s+(?:failed|errors?)\b",
        re.IGNORECASE | re.MULTILINE,
    ),
    re.compile(r"\btest result:\s+FAILED\b"),
    re.compile(r"^not ok\b", re.MULTILINE),
    re.compile(r"^#\s*fail\s+[1-9]\d*\b", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^FAIL\s+\S+", re.MULTILINE),
)

FAILURE_LINE_PATTERNS = (
    re.compile(r"^Traceback \(most recent call last\):", re.MULTILINE),
    re.compile(r"^(?:E\s+)?[A-Z][A-Za-z0-9_.]*(?:Error|Exception|Fault):", re.MULTILINE),
    re.compile(r"^(?:fatal|error|panic):\s+\S+", re.IGNORECASE | re.MULTILINE),
    re.compile(r"\b(?:command not found|No such file or directory|Permission denied|Cannot find path)\b"),
)


# ---- 1. Ordered Noise Normalizer & Fingerprinter

def strip_ansi(text: str) -> str:
    """Strip ANSI CSI and OSC escape sequences from `text`."""
    cleaned = ANSI_OSC_RE.sub("", _to_str(text))
    return ANSI_CSI_RE.sub("", cleaned)


def collapse_paths(text: str) -> str:
    """Replace temp directories with `<tmp>` BEFORE collapsing absolute paths to `<path>/<final_segment>`."""
    out = WIN_TEMP_PATH_RE.sub("<tmp>", _to_str(text))
    out = UNIX_TEMP_PATH_RE.sub("<tmp>", out)
    out = TEMP_SEGMENT_RE.sub("<tmp>", out)
    out = WIN_ABS_PATH_RE.sub(r"<path>/\1", out)
    out = UNIX_ABS_PATH_RE.sub(r"<path>/\1", out)
    return out


def normalize_error(text: str) -> str:
    """Normalize dynamic noise in `text` while preserving strict ordering invariants:
    1. Strip ANSI CSI/OSC sequences.
    2. Replace UUIDs (`<uuid>`) and >=32-char hex hashes (`<hash>`) BEFORE ISO timestamps (`<ts>`).
    3. Replace durations (`<ts>`), PIDs (`<pid>`), and host:port (`<port>`) BEFORE `:line:col` (`<pos>`).
    4. Replace temp dir segments (`<tmp>`) BEFORE collapsing absolute paths (`<path>/<final_segment>`).
    """
    out = strip_ansi(text)
    out = UUID_RE.sub("<uuid>", out)
    out = HEX_HASH_RE.sub("<hash>", out)
    out = ISO_TIMESTAMP_RE.sub("<ts>", out)
    out = DURATION_RE.sub("<ts>", out)
    out = PID_RE.sub("<pid>", out)
    out = HOST_PORT_RE.sub("<port>", out)
    out = LINE_COL_RE.sub("<pos>", out)
    out = LINE_WORD_RE.sub("<pos>", out)
    out = collapse_paths(out)
    return " ".join(out.split())


def error_signature(category: str, message: str, *, is_normalized: bool = False) -> str:
    """Compute 12-hex SHA-256 fingerprint `sha256(f"{category}\\0{normalized}")[:12]`."""
    norm = message if is_normalized else normalize_error(message)
    cat = _first_str(category, default="general").lower()
    digest = hashlib.sha256(f"{cat}\0{norm}".encode("utf-8")).hexdigest()
    return digest[:12]


def redact_text(text: str) -> str:
    """Redact paths and secrets idempotently without length truncation.

    Runs `collapse_paths` BEFORE long-base64 / long-hex replacement so a long directory
    segment is collapsed to `<path>/<final_segment>` instead of partially matching `<redacted>`.
    """
    out = strip_ansi(text)
    out = URL_CRED_RE.sub(_replace_url_cred, out)
    out = collapse_paths(out)
    out = BEARER_RE.sub(r"\1<redacted>", out)
    out = COOKIE_HEADER_RE.sub(_replace_cookie_header, out)
    out = SECRET_PREFIX_RE.sub("<redacted>", out)
    out = CLI_FLAG_SECRET_RE.sub(r"\1<redacted>", out)
    out = KV_QUOTED_SECRET_RE.sub(r"\1\2<redacted>\5", out)
    out = KV_EQ_SECRET_RE.sub(r"\1\2<redacted>\4", out)
    out = KV_SECRET_RE.sub(r"\1\2<redacted>\4", out)
    out = UUID_RE.sub("<uuid>", out)
    out = HEX_HASH_RE.sub("<hash>", out)
    out = LONG_BASE64_RE.sub(_replace_long_base64, out)
    return " ".join(out.split())


def redact_sample(text: str, max_chars: int = DEFAULT_SAMPLE_MAX_CHARS) -> str:
    """Redact secrets and paths BEFORE length truncation so truncation never slices a secret token in half."""
    collapsed = " ".join(strip_ansi(text).split())
    bounded = collapsed[: max(max_chars * 8, 4096)]
    redacted = redact_text(bounded)
    if max_chars <= 3:
        return redacted[: max(0, max_chars)]
    if len(redacted) <= max_chars and len(collapsed) <= len(bounded):
        return redacted
    return redacted[: max_chars - 3] + "..."


# ---- 2. Three-Tier Error Matcher

@dataclass(frozen=True)
class AntibodyEntry:
    id: str
    category: str
    message: str = ""
    fix: str = ""
    sig: str = ""
    normalized: str = ""
    code: str | None = None

    @classmethod
    def from_any(cls, item: AntibodyEntry | dict[str, Any]) -> AntibodyEntry:
        if isinstance(item, AntibodyEntry):
            return item
        data = dict(item)
        entry_id = _first_str(data.get("id"), data.get("entry_id"))
        category = _first_str(data.get("category"), default="general").lower()
        message = _first_str(data.get("message"), data.get("sample"), data.get("title"))
        norm = _first_str(data.get("normalized"), normalize_error(message))
        sig = _first_str(data.get("sig"), error_signature(category, norm))
        code = _first_str(data.get("code"), extract_error_code(message), extract_error_code(norm))
        return cls(
            id=entry_id,
            category=category,
            message=message,
            fix=_first_str(data.get("fix")),
            sig=sig,
            normalized=norm,
            code=_none_if_empty(code),
        )


@dataclass(frozen=True)
class MatchResult:
    entry: Any
    entry_id: str
    tier: str
    similarity: float
    sig: str
    code: str | None = None

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


def tokenize_error(text: str) -> set[str]:
    """Tokenize normalized error text into >=2-char lowercase identifiers, ignoring canonical placeholders."""
    without_placeholders = PLACEHOLDER_RE.sub(" ", _to_str(text))
    return {tok.lower() for tok in TOKEN_RE.findall(without_placeholders)}


def jaccard_similarity(tokens_a: set[str], tokens_b: set[str]) -> float:
    """Compute set Jaccard similarity `|A & B| / |A | B|` (0.0 when either set is empty)."""
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


def extract_error_code(text: str) -> str | None:
    """Extract a structured error code (`E_...`, `ERR_...`, `OperationalError`, `0x...`, HTTP 4xx/5xx)."""
    s = _to_str(text)
    for pattern, fmt in _ERROR_CODE_FORMATTERS:
        m = pattern.search(s)
        if m is not None:
            return fmt(m.group(1))
    return None


def match_antibody(
    category: str,
    message: str,
    entries: Iterable[Any],
    *,
    fuzzy_threshold: float = FUZZY_JACCARD_THRESHOLD,
    short_max_chars: int = SHORT_MESSAGE_MAX_CHARS,
) -> MatchResult | None:
    """Match an error against `entries` across 3 tiers:
    - Tier 1 (`"exact"`): exact 12-hex signature match within the same category (`similarity = 1.0`).
    - Tier 2 (`"fuzzy"`): same-category token Jaccard similarity `>= fuzzy_threshold` (`0.60`).
    - Tier 3 (`"code"`): short message (`len(normalized) < 80`) sharing an extracted error code,
      using token Jaccard similarity as tie-breaker.
    """
    cat = _first_str(category, default="general").lower()
    norm = normalize_error(message)
    sig = error_signature(cat, norm)
    candidates = [(raw, AntibodyEntry.from_any(raw)) for raw in entries]
    same_cat = [(raw, ent) for raw, ent in candidates if ent.category == cat]

    for raw, ent in same_cat:
        if ent.sig == sig:
            return MatchResult(entry=raw, entry_id=ent.id, tier="exact", similarity=1.0, sig=sig, code=ent.code)

    query_tokens = tokenize_error(norm)
    fuzzy_hits = [
        (sim, -idx, raw, ent)
        for idx, (raw, ent) in enumerate(same_cat)
        for sim in (jaccard_similarity(query_tokens, tokenize_error(ent.normalized)),)
        if sim >= fuzzy_threshold
    ]
    if fuzzy_hits:
        sim, _, raw, ent = max(fuzzy_hits)
        return MatchResult(entry=raw, entry_id=ent.id, tier="fuzzy", similarity=sim, sig=sig, code=ent.code)

    if len(norm) < short_max_chars:
        query_code = _none_if_empty(_first_str(extract_error_code(message), extract_error_code(norm)))
        code_hits = [
            (jaccard_similarity(query_tokens, tokenize_error(ent.normalized)), -idx, raw, ent)
            for idx, (raw, ent) in enumerate(same_cat)
            if query_code and ent.code == query_code and len(ent.normalized) < short_max_chars
        ]
        if code_hits:
            sim, _, raw, ent = max(code_hits)
            return MatchResult(entry=raw, entry_id=ent.id, tier="code", similarity=sim, sig=sig, code=query_code)

    return None


# ---- 3. Display-Safe Failure & Test-Runner Verdict Classifier

def _split_shell_unquoted(command: str, scanner: re.Pattern[str]) -> list[str]:
    """Split `command` on unquoted, unescaped shell operators matched by `scanner` (`group('op')`)."""
    out: list[str] = []
    for a, b in _unquoted_segment_spans(command, scanner):
        seg = command[a:b].strip()
        if seg:
            out.append(seg)
    return out


def _unquoted_segment_spans(command: str, scanner: re.Pattern[str]) -> list[tuple[int, int]]:
    """Return `(start, end)` character spans separated by matches with `group('op') is not None`."""
    spans: list[tuple[int, int]] = []
    start = 0
    for match in scanner.finditer(command):
        if match.group("op"):
            spans.append((start, match.start()))
            start = match.end()
    spans.append((start, len(command)))
    return spans


def split_shell_stages(command: str) -> list[str]:
    """Split a shell command string on unquoted `&&`, `||`, `;`, and `|` into non-empty stages."""
    return _split_shell_unquoted(_to_str(command), SHELL_STAGE_SCAN_RE)


def _stage_command_head(stage: str) -> str:
    """Extract normalized command name (or `git <subcmd>`) from one shell pipeline stage."""
    m = _STAGE_PREFIX_RE.match(stage)
    if m is None:
        return ""
    base = m.group(1).lower()
    if base == "git":
        return f"git {_to_str(m.group(2)).lower()}".strip()
    return base


def is_display_only_command(command: str) -> bool:
    """Return True if `command` has >=1 stages and every stage is a pure display/search command."""
    cmd_str = _to_str(command)
    stages = split_shell_stages(cmd_str)
    if not stages or bool(_SUBSHELL_RE.search(cmd_str)):
        return False
    for stage in stages:
        if _stage_command_head(stage) not in DISPLAY_COMMANDS or bool(_FIND_SIDE_EFFECT_RE.search(stage)):
            return False
    return True


def benign_exit(command: str, exit_code: int | None) -> bool:
    """Return True when `exit_code == 1` and every `&&`/`||`/`;` clause ends in `BENIGN_EXITS`
    (or is a pure display command following a benign test/grep guard via `&&`)."""
    if exit_code != 1:
        return False
    clauses = _split_shell_unquoted(_to_str(command), SHELL_CLAUSE_SCAN_RE)
    saw_benign = False
    for clause in clauses:
        stages = split_shell_stages(clause)
        last_head = _stage_command_head(([""] + stages)[-1])
        if last_head in BENIGN_EXITS:
            saw_benign = True
        elif not (saw_benign and is_display_only_command(clause)):
            return False
    return saw_benign


def has_runner_verdict(output: str) -> bool:
    """Return True if `output` contains a test-runner failure verdict (`pytest`, `cargo test`, TAP)."""
    cleaned = strip_ansi(output)
    return any(pattern.search(cleaned) for pattern in RUNNER_VERDICT_PATTERNS)


def _has_own_display_error(command: str, cleaned_out: str) -> bool:
    """Return True if `cleaned_out` has a tool-prefixed error or a Git-stage `fatal:` diagnostic."""
    has_git_stage = any(
        _stage_command_head(stage).startswith("git")
        for stage in split_shell_stages(command)
    )
    return bool(OWN_ERROR_RE.search(cleaned_out) or (has_git_stage and GIT_FATAL_RE.search(cleaned_out)))


def looks_failed(command: str, output: str, exit_code: int | None = None) -> bool:
    """Classify whether a tool/command execution failed without PreToolUse Bash DEBUG wrapping."""
    cleaned_out = strip_ansi(output)
    if benign_exit(command, exit_code):
        return _has_own_display_error(command, cleaned_out)
    if exit_code not in (None, 0):
        return True
    if is_display_only_command(command):
        return _has_own_display_error(command, cleaned_out)
    return has_runner_verdict(cleaned_out) or any(p.search(cleaned_out) for p in FAILURE_LINE_PATTERNS)


# ---- 4. Per-Seat FixTrust (DOUBT_AFTER = 1, SUPPRESS_AFTER = 2)

def compute_fix_sig(fix: str) -> str:
    """Compute 12-hex SHA-256 signature of one-line normalized `fix` text."""
    one_line = " ".join(_to_str(fix).split())
    return hashlib.sha256(one_line.encode("utf-8")).hexdigest()[:12]


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.write("\n")
        for _ in range(19):
            with contextlib.suppress(PermissionError):
                os.replace(tmp_name, path)
                return
            time.sleep(0.005)
        os.replace(tmp_name, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _is_nonneg_int(val: Any) -> bool:
    return type(val) is int and val >= 0


def _is_pos_number(val: Any) -> bool:
    return type(val) in (int, float) and math.isfinite(val) and val > 0


def _is_nonempty_str(val: Any) -> bool:
    return isinstance(val, str) and bool(val.strip())


def _validate_fix_record(k: Any, v: Any, state_path: Path | None) -> dict[str, Any]:
    if not (
        isinstance(v, dict)
        and all(_is_nonneg_int(v.get(c)) for c in ("injected", "recurred_after_inject", "succeeded"))
        and isinstance(v.get("fix_sig", ""), str)
    ):
        raise ValueError(
            f"Invalid FixTrust record for {k!r} in {state_path}: "
            "expected dict with non-negative int injected, recurred_after_inject, succeeded and str fix_sig"
        )
    rec = dict(v)
    rec.setdefault("fix_sig", "")
    return rec


def _validate_turn_pair(item: Any, state_path: Path | None) -> tuple[str, str]:
    if not (isinstance(item, (list, tuple)) and len(item) == 2 and all(map(_is_nonempty_str, item))):
        raise ValueError(f"Invalid FixTrust injected_in_turn item in {state_path}: {item!r}")
    return (item[0], item[1])


class FixTrust:
    """Per-seat trust tracker for injected antibody fixes and decision cards."""

    def __init__(self, state_path: str | Path | None = None) -> None:
        self.state_path = Path(state_path) if state_path else None
        self.records: dict[str, dict[str, Any]] = {}
        self._injected_in_turn: set[tuple[str, str]] = set()
        with _file_lock(self.state_path):
            self._load()

    def _load(self) -> None:
        if self.state_path and self.state_path.is_file():
            p = self.state_path
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f"Corrupt or unreadable FixTrust state file {p}: {exc}") from exc
            if not (
                isinstance(data, dict)
                and isinstance(data.get("records"), dict)
                and isinstance(data.get("injected_in_turn", []), list)
            ):
                raise ValueError(f"Invalid FixTrust state schema in {p}: expected object with 'records' dict")
            self.records = {str(k): _validate_fix_record(k, v, p) for k, v in data["records"].items()}
            self._injected_in_turn = {_validate_turn_pair(item, p) for item in data.get("injected_in_turn", [])}

    def _save(self) -> None:
        if self.state_path:
            _atomic_write_json(
                self.state_path,
                {
                    "records": self.records,
                    "injected_in_turn": sorted([list(pair) for pair in self._injected_in_turn]),
                },
            )

    def _ensure_record(self, entry_id: str, fix: str | None = None) -> dict[str, Any]:
        sig = compute_fix_sig(fix) if fix else ""
        rec = self.records.get(entry_id)
        if rec is None:
            rec = {"injected": 0, "recurred_after_inject": 0, "succeeded": 0, "fix_sig": sig}
            self.records[entry_id] = rec
        elif sig and rec["fix_sig"] != sig:
            if rec["fix_sig"]:
                rec = {"injected": 0, "recurred_after_inject": 0, "succeeded": 0, "fix_sig": sig}
                self.records[entry_id] = rec
                self._injected_in_turn = {pair for pair in self._injected_in_turn if pair[1] != entry_id}
            else:
                rec["fix_sig"] = sig
        return rec

    def record_injected(self, session_id: str, entry_id: str, fix: str) -> dict[str, Any]:
        """Record that `entry_id` with `fix` was injected in `session_id` during the current turn."""
        with _file_lock(self.state_path):
            self._load()
            rec = self._ensure_record(entry_id, fix)
            rec["injected"] = int(rec.get("injected", 0)) + 1
            self._injected_in_turn.add((session_id, entry_id))
            self._save()
            return dict(rec)

    def record_seen(self, session_id: str, entry_id: str, fix: str | None = None) -> dict[str, Any]:
        """Record that `entry_id` occurred in `session_id`; increments `recurred_after_inject` if already injected this turn."""
        with _file_lock(self.state_path):
            self._load()
            rec = self._ensure_record(entry_id, fix)
            if (session_id, entry_id) in self._injected_in_turn:
                rec["recurred_after_inject"] = int(rec.get("recurred_after_inject", 0)) + 1
            self._save()
            return dict(rec)

    def record_succeeded(self, entry_id: str, fix: str | None = None) -> dict[str, Any]:
        """Record that `entry_id` resolved (`succeeded` or `moved_past`), protecting it from suppression."""
        with _file_lock(self.state_path):
            self._load()
            rec = self._ensure_record(entry_id, fix)
            rec["succeeded"] = int(rec.get("succeeded", 0)) + 1
            self._save()
            return dict(rec)

    def new_turn(self, session_id: str) -> None:
        """Clear turn-scoped injection tracking for `session_id`."""
        with _file_lock(self.state_path):
            self._load()
            self._injected_in_turn = {
                pair for pair in self._injected_in_turn if pair[0] != session_id
            }
            self._save()

    def status(self, entry_id: str, fix: str | None = None) -> str:
        """Return `"trusted"`, `"doubted"`, or `"suppressed"` for `entry_id`."""
        with _file_lock(self.state_path):
            self._load()
            rec = self._ensure_record(entry_id, fix)
            self._save()
            succeeded = int(rec.get("succeeded", 0))
            recurred = int(rec.get("recurred_after_inject", 0))
            if succeeded > 0 or recurred < DOUBT_AFTER:
                return "trusted"
            if recurred >= SUPPRESS_AFTER:
                return "suppressed"
            return "doubted"

    def should_inject(self, entry_id: str, fix: str | None = None) -> bool:
        """Return True unless `entry_id` is currently `"suppressed"`."""
        return self.status(entry_id, fix) != "suppressed"

    def format_notice(self, entry_id: str, fix: str) -> str | None:
        """Format notice text according to trust status, returning None when suppressed."""
        st = self.status(entry_id, fix)
        if st == "suppressed":
            return None
        if st == "doubted":
            return f"[VERIFY: this fix failed earlier on this machine] {fix}"
        return fix


# ---- 5. Turn-Scoped ResolutionTracker & Box-Local ClaimsStore

class ResolutionTracker:
    """Tracks failed `(entry_id, command_or_tool_key, turn)` across `RESOLUTION_WINDOW_TURNS = 1`."""

    def __init__(self, window_turns: int = RESOLUTION_WINDOW_TURNS) -> None:
        self.window_turns = int(window_turns)
        self.pending: dict[str, dict[str, Any]] = {}
        self.resolved: list[dict[str, Any]] = []

    def _prune(self, current_turn: int) -> None:
        self.pending = {
            key: item
            for key, item in self.pending.items()
            if (current_turn - int(item["turn"])) <= self.window_turns
        }

    def record_failure(self, entry_id: str, key: str, turn: int) -> dict[str, Any] | None:
        """Record a failure on `key` at `turn`."""
        self._prune(turn)
        prior = self.pending.get(key)
        resolution: dict[str, Any] | None = None
        if prior is not None and key.startswith("command:") and prior["entry_id"] != entry_id:
            resolution = {
                "entry_id": prior["entry_id"],
                "replaced_by": entry_id,
                "key": key,
                "failed_turn": prior["turn"],
                "turn": turn,
                "reason": "moved_past",
            }
            self.resolved.append(resolution)
        self.pending[key] = {"entry_id": entry_id, "key": key, "turn": int(turn)}
        return resolution

    def record_success(self, key: str, turn: int) -> dict[str, Any] | None:
        """Record a success on `key` at `turn`, resolving any unexpired failure with `reason="succeeded"`."""
        self._prune(turn)
        prior = self.pending.pop(key, None)
        if prior is None:
            return None
        resolution = {
            "entry_id": prior["entry_id"],
            "key": key,
            "failed_turn": prior["turn"],
            "turn": turn,
            "reason": "succeeded",
        }
        self.resolved.append(resolution)
        return resolution


def _lock_fd_nt(fh: Any, unlock: bool) -> None:
    import msvcrt
    fh.seek(0)
    mode = msvcrt.LK_UNLCK if unlock else msvcrt.LK_NBLCK
    msvcrt.locking(fh.fileno(), mode, 1)


def _lock_fd_posix(fh: Any, unlock: bool) -> None:
    import fcntl
    fcntl.flock(fh.fileno(), fcntl.LOCK_UN if unlock else fcntl.LOCK_EX | fcntl.LOCK_NB)


def _lock_fd(fh: Any, *, unlock: bool = False) -> None:
    """One exclusive lock per open handle: msvcrt.locking on byte 0 on Windows, fcntl.flock elsewhere (the
    board_claim._Lock and sendguard convention). A held lock raises OSError on both, which _file_lock retries."""
    if os.name == "nt":
        _lock_fd_nt(fh, unlock)
    else:
        _lock_fd_posix(fh, unlock)


@contextlib.contextmanager
def _file_lock(path: Path | None, timeout_s: float = 5.0) -> Iterator[None]:
    """Acquire an exclusive cross-process file lock on `<path>.lock` when `path` is set."""
    if path is None:
        yield
        return
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+b") as fh:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                _lock_fd(fh)
                break
            except OSError:
                time.sleep(0.01)
        else:
            _lock_fd(fh)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                _lock_fd(fh, unlock=True)


def _validate_claim_record(k: Any, v: Any, path: Path | None) -> dict[str, Any]:
    if not (isinstance(v, dict) and _is_nonempty_str(v.get("agent")) and _is_pos_number(v.get("claimed_at"))):
        raise ValueError(f"Invalid claim record for {k!r} in {path}: missing or invalid 'claimed_at' or 'agent'")
    return dict(v)


class ClaimsStore:
    """Box-local 10-minute diagnosis lease store (`CLAIM_TTL_S = 600`)."""

    def __init__(self, path: str | Path | None = None, ttl_s: float = CLAIM_TTL_S) -> None:
        self.path = Path(path) if path else None
        self.ttl_s = float(ttl_s)
        self.claims: dict[str, dict[str, Any]] = {}
        with _file_lock(self.path):
            self._load()

    def _load(self) -> None:
        if self.path and self.path.is_file():
            p = self.path
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f"Corrupt or unreadable ClaimsStore file {p}: {exc}") from exc
            if not (isinstance(data, dict) and isinstance(data.get("claims"), dict)):
                raise ValueError(f"Invalid ClaimsStore schema in {p}: expected object with 'claims' dict")
            self.claims = {str(k): _validate_claim_record(k, v, p) for k, v in data["claims"].items()}

    def _save(self) -> None:
        if self.path:
            _atomic_write_json(self.path, {"claims": self.claims})

    def _prune(self, now: float) -> bool:
        expired = [
            sig
            for sig, rec in self.claims.items()
            if (now - float(rec.get("claimed_at", 0.0))) >= self.ttl_s
        ]
        for sig in expired:
            del self.claims[sig]
        return bool(expired)

    def acquire_or_hint(
        self,
        sig: str,
        agent: str,
        now: float,
        entry_id: str | None = None,
    ) -> dict[str, Any]:
        """Acquire a diagnosis lease on `sig` for `agent`, or return a 1-line claim hint if held by another seat."""
        ts = float(now)
        with _file_lock(self.path):
            self._load()
            self._prune(ts)
            existing = self.claims.get(sig, {})
            label = _first_str(entry_id, existing.get("entry_id"), sig)
            if not existing or existing.get("agent") == agent:
                claimed_at = float(existing.get("claimed_at", ts))
                self.claims[sig] = {"sig": sig, "agent": agent, "claimed_at": claimed_at, "entry_id": label}
                self._save()
                return {"acquired": True, "sig": sig, "agent": agent, "elapsed_s": ts - claimed_at, "hint": None}

            self._save()
            holder = _to_str(existing["agent"])
            elapsed_s = max(0.0, ts - float(existing["claimed_at"]))
            elapsed_min = max(1, int(round(elapsed_s / 60.0)))
            hint = (
                f"[antibody] {label}: {holder} has been diagnosing this for {elapsed_min} min. "
                f"Its fix will be passed to you when it is recorded."
            )
            return {"acquired": False, "sig": sig, "agent": holder, "elapsed_s": elapsed_s, "hint": hint}

    def release(self, sig: str, agent: str) -> bool:
        """Release a diagnosis lease on `sig` verifying `agent` owns it."""
        with _file_lock(self.path):
            self._load()
            existing = self.claims.get(sig)
            if existing is None or existing.get("agent") != agent:
                return False
            del self.claims[sig]
            self._save()
            return True


# ---- 6. Transcript Scanner & CLI Entrypoint

def scan_transcript(
    transcript_path: str | Path,
    catalog: Iterable[Any] = (),
    *,
    default_category: str = "general",
) -> dict[str, Any]:
    """Scan a JSONL transcript for failed tool/command outputs and match against `catalog`."""
    entries = list(catalog)
    matched: list[dict[str, Any]] = []
    uncatalogued: dict[str, dict[str, Any]] = {}

    path = Path(transcript_path)
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        rec = json.loads(raw_line)
        cmd = _first_str(rec.get("command"), rec.get("cmd"))
        parts = [
            _to_str(rec.get(k))
            for k in ("output", "stdout", "content", "text", "stderr", "error")
        ]
        output = "\n".join(dict.fromkeys(p for p in parts if p))
        raw_ec = rec.get("exit_code")
        exit_code = None if raw_ec is None else int(raw_ec)
        cat = _first_str(rec.get("category"), default=default_category).lower()

        if not looks_failed(cmd, output, exit_code=exit_code):
            continue

        hit = match_antibody(cat, output, entries)
        if hit is not None:
            matched.append({
                "entry_id": hit.entry_id,
                "tier": hit.tier,
                "similarity": round(hit.similarity, 4),
                "sig": hit.sig,
                "code": hit.code,
            })
        else:
            norm = normalize_error(output)
            sig = error_signature(cat, norm)
            bucket = uncatalogued.get(sig)
            if bucket is None:
                code_val = _first_str(extract_error_code(output), extract_error_code(norm))
                uncatalogued[sig] = {
                    "sig": sig,
                    "category": cat,
                    "count": 1,
                    "code": _none_if_empty(code_val),
                    "sample": redact_sample(output),
                }
            else:
                bucket["count"] = int(bucket["count"]) + 1

    return {
        "matched": matched,
        "uncatalogued": list(uncatalogued.values()),
    }


def _load_catalog_json(catalog_path: str | None) -> list[Any]:
    if not catalog_path:
        return []
    return list(json.loads(Path(catalog_path).read_text(encoding="utf-8")))


def _cmd_fingerprint(args: argparse.Namespace) -> int:
    norm = normalize_error(args.message)
    sig = error_signature(args.category, norm)
    code_val = _first_str(extract_error_code(args.message), extract_error_code(norm))
    payload = {
        "category": args.category.strip().lower(),
        "sig": sig,
        "normalized": redact_text(norm),
        "redacted": redact_sample(args.message, max_chars=args.max_chars),
        "code": _none_if_empty(code_val),
    }
    print(json.dumps(payload, sort_keys=True))
    return 0


def _cmd_classify(args: argparse.Namespace) -> int:
    payload = {
        "command": args.command,
        "exit_code": args.exit_code,
        "display_only": is_display_only_command(args.command),
        "benign_exit": benign_exit(args.command, args.exit_code),
        "runner_verdict": has_runner_verdict(args.output),
        "looks_failed": looks_failed(args.command, args.output, exit_code=args.exit_code),
    }
    print(json.dumps(payload, sort_keys=True))
    return 0


def _cmd_scan_transcript(args: argparse.Namespace) -> int:
    report = scan_transcript(args.transcript, _load_catalog_json(args.catalog), default_category=args.category)
    print(json.dumps(report, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for fingerprinting, failure classification, and transcript scanning."""
    parser = argparse.ArgumentParser(
        prog="python -m tools.meshy_node.antibodies",
        description="MeshyChat fleet error-immunity fingerprinting, classification, and transcript audit CLI.",
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_fp = sub.add_parser("fingerprint", help="Normalize, fingerprint, and redact an error message")
    p_fp.add_argument("--category", default="general", help="Error category (default: general)")
    p_fp.add_argument("--message", required=True, help="Raw error text")
    p_fp.add_argument("--max-chars", type=int, default=DEFAULT_SAMPLE_MAX_CHARS, help="Max sample length")
    p_fp.set_defaults(_handler=_cmd_fingerprint)

    p_cls = sub.add_parser("classify", help="Classify whether a shell command and output indicate failure")
    p_cls.add_argument("--command", default="", help="Shell command string")
    p_cls.add_argument("--output", default="", help="Captured stdout/stderr text")
    p_cls.add_argument("--exit-code", type=int, default=None, help="Optional integer exit status")
    p_cls.set_defaults(_handler=_cmd_classify)

    p_scan = sub.add_parser("scan-transcript", help="Scan a JSONL transcript against an antibodies JSON catalog")
    p_scan.add_argument("--transcript", required=True, help="Path to JSONL transcript file")
    p_scan.add_argument("--catalog", default=None, help="Optional path to JSON file containing antibody entries")
    p_scan.add_argument("--category", default="general", help="Default category when record omits one")
    p_scan.set_defaults(_handler=_cmd_scan_transcript)

    args = parser.parse_args(argv)
    return int(args._handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
