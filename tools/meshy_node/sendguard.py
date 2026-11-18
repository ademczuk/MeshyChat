"""A refusal at the send seam for volume and size (kxxx's ruling of 2026-10-01, thread meshychat-send-volume).

THE FAILURE, measured 2026-10-01: one seat sent at least 39 messages to one peer in about two hours, mostly bundles of 5 to
10 inline source files, one bundle per revision of a test adapter, each revision fixing the previous review before it had
been verified. Every message was valid on its own; the volume was the defect, and nothing in the client pushed back. A rule
written in the sender's instructions did not stop it, so the check lives where the message is written.

TWO REFUSALS, each before anything is sent, each naming its number and its override:
  VOLUME  this identity has already sent MAX_PER_RECIPIENT LARGE messages (a body of COUNTED_MIN_BYTES or more) naming the
          same recipient label (to or cc) in the last WINDOW_S seconds, and this one is large too. Counted from a small
          ledger beside the state file (recipient, time, key; never a body or any other content), written only after a
          large send the server accepted and trimmed past the window. A message under COUNTED_MIN_BYTES is never counted
          and never refused for volume (amended 2026-10-05, divergence 068): measured on production, counting every
          message refused 337 sends in one ordinary day (11 sender/recipient pairs, a co-located team peaking at 15 per
          30 minutes), while the 2026-10-01 burst it was written for was bundles: replayed with only bodies of 4 KB or
          more counted, the same rule refuses 30 sends of that burst (3 pairs) and 0 of the ordinary day.
  SIZE    a body over MAX_BODY_BYTES. Far under the inline cap docshare documents, on purpose: a bundle of source files
          belongs in a commit and goes as `owner/name@<sha>:<path>` through `share` (divergence 064), and a large text
          artifact (a serial log, a kit, a report) goes as ONE attachment, `send --attach FILE` (divergence 079), not as
          a run of body parts.
BUNDLE  a body that is mostly file content (BUNDLE_MIN_BYTES or more, at least BUNDLE_MIN_SHARE of it fenced or code-like
          lines) and names a path or file name this identity already named to the same recipient in the last
          WINDOW_S seconds: the files were announced, so the bytes go as a pointer (`share`) or one `--attach` file.
          Added 2026-10-05 (divergence 068, second amendment): on two labelled sets (the 2026-10-01 burst and an ordinary
          day, 60 sends hand-labelled blind) it refused only churn (precision 1.00, recall 0.36) and 0 of 224 ordinary
          sends. Counted from a second ledger beside the state file holding, per accepted send, the recipient, the time and
          16-hex prefixes of the SHA-256 of each named token; never a body, never a token itself. An unreadable token
          ledger refuses nothing (this rule is about quality, not volume).
OVERRIDE  `--bulk-ack "<reason>"`, non-empty, and the reason is written INTO THE MESSAGE (a first body line and, when no
          subject was given, the subject `bulk-ack`), so the cost lands on the sender's record and the recipient can say
          stop. No environment variable and no config switch: an override nobody has to type is not one.
A reply (`--in-reply-to`) to the recipient's own message is NOT exempt: a fast back-and-forth is how the 39 happened.

WHAT THIS IS NOT. A client check binds only seats that run this client honestly: it stops the mistake, not a hostile sender.
The enforcement is a per-sender per-recipient budget on the server, a later item with its own divergence.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import tempfile
import time
from pathlib import Path

WINDOW_S = 30 * 60
MAX_PER_RECIPIENT = 6
COUNTED_MIN_BYTES = 4 * 1024      # only a body this large counts toward, and can be refused by, the volume limit
MAX_BODY_BYTES = 16 * 1024
MAX_REASON_CHARS = 300
#: Divergence 084 (issue #141): the ADVISORY tier. A body this large to a recipient that has not answered an
#: earlier send WARNS, never refuses. Scored on send-filter/2026-10-05 (60 hand labels): P 0.64, R 1.00,
#: F1 0.78, the same as the local model's best at no cost. The bundle rule stays the only refusal.
ADVISORY_MIN_BYTES = 2048
ADVISORY_MIN_UNANSWERED = 1
LEDGER_SUFFIX = ".sendledger"
TOKEN_LEDGER_SUFFIX = ".sendtokens"
PAIR_LEDGER_SUFFIX = ".sendpairs"
LOCK_SUFFIX = ".sendlock"
LOCK_WAIT_S = 10.0                # how long record() waits for another send's ledger update; then it records nothing
BUNDLE_MIN_BYTES = 2 * 1024
BUNDLE_MIN_SHARE = 0.6
PAIR_LOOP_TWO_WAY_MIN = 6
PAIR_LOOP_ONE_WAY_CAP = 30
PAIR_LOOP_LIMIT = PAIR_LOOP_ONE_WAY_CAP
PAIR_LOOP_WINDOW_S = 600


def pair_ledger_path(state_file: Path) -> Path:
    return state_file.with_name(state_file.name + PAIR_LEDGER_SUFFIX)


def reset_ledgers(state_file: Path) -> None:
    """Remove all local send-guard sidecar ledgers for `state_file` (used by test harnesses that send in bursts)."""
    st = Path(state_file)
    for path_fn in (ledger_path, token_ledger_path, pair_ledger_path):
        path_fn(st).unlink(missing_ok=True)


def _pair_entries(path: Path, now: float, window_s: int = PAIR_LOOP_WINDOW_S) -> list[dict]:
    """The pair-loop ledger entries inside `window_s`.

    Missing or unreadable ledgers return `[]` (fails open on disk damage, unlike the volume ledger's
    `LedgerUnreadable`): the pair-loop check is a coordination safety net and must not brick ordinary
    short sends if its sidecar file is damaged.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    out = []
    for e in data:
        if not (isinstance(e, dict) and isinstance(e.get("t"), (int, float)) and not isinstance(e["t"], bool)):
            continue
        if not (now - e["t"] < window_s):
            continue
        key_val = e.get("key") if isinstance(e.get("key"), str) and e.get("key").strip() else None
        if isinstance(e.get("to"), str) and e["to"].strip():
            rec = {"to": e["to"].casefold(), "t": e["t"]}
            if key_val:
                rec["key"] = key_val
            out.append(rec)
        elif isinstance(e.get("from"), str) and e["from"].strip():
            rec = {"from": e["from"].casefold(), "t": e["t"]}
            if key_val:
                rec["key"] = key_val
            out.append(rec)
    return out


def check_pair_loop(
    entries: list[dict],
    recipient: str,
    now: float,
    *,
    limit: int = PAIR_LOOP_ONE_WAY_CAP,
    two_way_min: int = PAIR_LOOP_TWO_WAY_MIN,
    window_s: int = PAIR_LOOP_WINDOW_S,
) -> str | None:
    """Pairwise ping-pong loop guard and one-way burst backstop (divergence 068 / kxxx).

    Refuses when:
    1. Two-way ping-pong: this seat sent `>= two_way_min` (6) messages to `recipient` AND
       received `>= two_way_min` (6) replies from `recipient` within `window_s` (600s / 10 min).
    2. One-way burst cap: this seat sent `>= limit` (30) messages to `recipient` within
       `window_s` (600s / 10 min) regardless of message byte size.
    """
    if not isinstance(entries, list) or not isinstance(recipient, str):
        return None
    target = recipient.casefold()
    if not target:
        return None
    sent_count = 0
    reply_count = 0
    for e in entries:
        if not (isinstance(e, dict) and isinstance(e.get("t"), (int, float)) and not isinstance(e["t"], bool)):
            continue
        if not (math.isfinite(e["t"]) and 0 <= now - e["t"] < window_s):
            continue
        to_val = e.get("to")
        from_val = e.get("from")
        if isinstance(to_val, str) and to_val.casefold() == target:
            sent_count += 1
        elif isinstance(from_val, str) and from_val.casefold() == target:
            reply_count += 1
    minutes = window_s // 60
    if sent_count >= two_way_min and reply_count >= two_way_min:
        return (
            f"pair-loop guard: {sent_count} sends to {target} and {reply_count} replies from {target} "
            f"in {minutes} min; pass --bulk-ack \"<reason>\" to override."
        )
    if sent_count >= limit:
        return (
            f"{sent_count} sends to {target} in {minutes} min: pair-loop one-way cap "
            f"(limit {limit}); pass --bulk-ack \"<reason>\" to override."
        )
    return None


def record_inbound(state_file: Path, *, sender: str, key: str | None = None, now: float | None = None) -> str | None:
    """Record an inbound message from `sender` in the pair-loop ledger so two-way ping-pong
    loops (>= 6 sends and >= 6 replies in 10 min) are counted accurately.

    Deduplicates by message `key` inside the active 10-minute window so reading or fetching
    the same message more than once counts it at most once. Never raises.
    """
    if not isinstance(sender, str) or not sender.strip():
        return None
    now = time.time() if now is None else now
    label = sender.strip().casefold()
    clean_key = key.strip() if isinstance(key, str) and key.strip() else None
    try:
        with _ledger_lock(state_file) as owned:
            if not owned:
                return f"the send ledger lock was held past {LOCK_WAIT_S:.0f} s"
            ppath = pair_ledger_path(state_file)
            pentries = _pair_entries(ppath, now)
            if clean_key is not None and any(
                e.get("from") == label and e.get("key") == clean_key for e in pentries
            ):
                return None
            rec = {"from": label, "t": now}
            if clean_key is not None:
                rec["key"] = clean_key
            pentries.append(rec)
            _write_json(ppath, pentries)
    except Exception as err:  # noqa: BLE001
        return f"the pair ledger could not be written ({type(err).__name__})"
    return None

_HASH = re.compile(r"[0-9a-f]{16}")
_PATH = re.compile(r"(?<![\w/.-])(?:[\w.-]+/)+[\w.-]+\.[A-Za-z0-9]{1,6}\b")
_BASENAME = re.compile(r"\b[\w.-]+\.(?:py|ts|tsx|js|md|json|sql|sh|ps1|toml|yaml|yml|txt|log|rs|go|cfg|ini)\b")
_FENCE = re.compile(r"```.*?```", re.S)
_CODE_LINE = re.compile(r"^(\s{4,}\S|\t|[+-](?![+-])\S|\s*(def|class|import|from|return|if|for|while|function|const|let|var|"
                        r"#include|SELECT|INSERT)\b|.*[{};]\s*$)")


def ledger_path(state_file: Path) -> Path:
    return state_file.with_name(state_file.name + LEDGER_SUFFIX)


def advisory(body_bytes, unanswered) -> bool:
    """The advisory rule (divergence 084): warn when the body is ADVISORY_MIN_BYTES or more and the sender
    has ADVISORY_MIN_UNANSWERED or more unanswered sends to the recipient. False for anything malformed."""
    if isinstance(body_bytes, bool) or isinstance(unanswered, bool):
        return False
    if not isinstance(body_bytes, int) or not isinstance(unanswered, int):
        return False
    return body_bytes >= ADVISORY_MIN_BYTES and unanswered >= ADVISORY_MIN_UNANSWERED


def token_ledger_path(state_file: Path) -> Path:
    return state_file.with_name(state_file.name + TOKEN_LEDGER_SUFFIX)


def file_share(body: str) -> float:
    """How much of the body is file content: the fenced share, or the share of non-blank lines that look like code."""
    if not body:
        return 0.0
    fenced = sum(len(m) for m in _FENCE.findall(body)) / len(body)
    lines = [ln for ln in body.splitlines() if ln.strip()]
    coded = sum(1 for ln in lines if _CODE_LINE.match(ln)) / len(lines) if lines else 0.0
    return max(fenced, coded)


#: Words that tell the reader a file comes with the message (audit finding 5, oxxxxxx). With no --attach, the
#: reader looks for a file that is not there; six reviewer round trips went that way.
_ATTACHED_RE = re.compile(r"\b(attached|attaching|attachments?|enclosed)\b", re.IGNORECASE)


def says_attached(body: str) -> bool:
    """True when the body says a file is attached. The caller warns only when nothing is (no --attach)."""
    return bool(body) and _ATTACHED_RE.search(body) is not None


def is_file_bundle(body: str) -> bool:
    return len(body.encode("utf-8")) >= BUNDLE_MIN_BYTES and file_share(body) >= BUNDLE_MIN_SHARE


def token_hashes(body: str) -> set[str]:
    """16-hex prefixes of the SHA-256 of each path and file name the body names. Only these are ever stored.
    Not commit shas or message keys: a sha cited as context ("base main 79f8215") or the key a reply answers is no
    announcement of a file, and matching them refused excerpts that re-sent nothing (memory-bxxxxx, PR #128).
    Re-scored on the labelled sets without them: the same 5 caught, 0 added, 0 of 224 ordinary sends."""
    found = set(_PATH.findall(body)) | set(_BASENAME.findall(body))
    return {hashlib.sha256(t.encode("utf-8")).hexdigest()[:16] for t in found}


def _token_entries(path: Path, now: float) -> list[dict]:
    """The token-ledger entries inside the window. Missing or unreadable is empty: this rule never refuses on doubt."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    out = []
    for e in data:
        if not (isinstance(e, dict) and isinstance(e.get("to"), str) and isinstance(e.get("t"), (int, float))
                and not isinstance(e["t"], bool) and isinstance(e.get("h"), list)):
            continue
        if e["t"] > now - WINDOW_S:
            hashes = [h for h in e["h"] if isinstance(h, str) and _HASH.fullmatch(h)]   # anything else is damage, ignored
            if hashes:
                entry = {"to": e["to"], "t": e["t"], "h": hashes}
                if isinstance(e.get("key"), str) and e["key"]:
                    entry["key"] = e["key"]                # the send this came from, so a re-record can see it
                out.append(entry)
    return out


@contextlib.contextmanager
def _ledger_lock(state_file: Path):
    """Yield True when this process owns the ledgers' lock, False when it could not get it within LOCK_WAIT_S.
    An OS lock on one byte of a lock file beside the state file (msvcrt on Windows, flock elsewhere): the OS releases
    it when the holder's handle closes or the holder dies, so there is no stale-lock removal to race, and the file is
    never deleted. A caller that does not own it must not touch either ledger."""
    lock = state_file.with_name(state_file.name + LOCK_SUFFIX)
    f = open(lock, "a+b")
    owned = False
    try:
        deadline = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                owned = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.01)
        yield owned
    finally:
        try:
            if owned:
                if os.name == "nt":
                    import msvcrt
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        finally:
            f.close()


def _write_json(path: Path, data) -> None:
    """Replace `path` atomically through a temporary file of this call's own, never a shared name."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data))
        for attempt in range(20):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:                    # Windows: a reader has the target open for a moment
                if attempt == 19:
                    raise
                time.sleep(0.01)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


class LedgerUnreadable(Exception):
    """The ledger exists and is not a list of entries. The guard cannot count, so it does not guess."""


def _entries(path: Path, now: float) -> list[dict]:
    """The ledger entries still inside the window. A missing ledger is empty; an unreadable one raises."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise LedgerUnreadable(str(exc)) from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise LedgerUnreadable("not JSON") from exc
    if not isinstance(data, list):
        raise LedgerUnreadable("not a list")
    kept = []
    for e in data:
        if (isinstance(e, dict) and isinstance(e.get("to"), str) and isinstance(e.get("t"), (int, float))
                and not isinstance(e["t"], bool) and isinstance(e.get("key"), str)):
            if now - e["t"] < WINDOW_S:
                kept.append(e)
        else:
            raise LedgerUnreadable("an entry is not {to, t, key}")
    return kept


def recipients(to: list[str] | None, cc: list[str] | None) -> list[str]:
    """The distinct recipient labels of one message, compared without case, in order of first appearance."""
    seen: list[str] = []
    for label in (to or []) + (cc or []):
        folded = label.casefold()
        if folded and folded not in seen:
            seen.append(folded)
    return seen


def clean_reason(reason: str | None) -> str | None:
    """None when no override was given. A given override must carry a non-empty reason: refusing here is the point."""
    if reason is None:
        return None
    one_line = " ".join(reason.split())
    if not one_line:
        raise ValueError("--bulk-ack needs a non-empty reason: it is written into the message the recipient reads")
    return one_line[:MAX_REASON_CHARS]


_FENCE_UNCLOSED_RE = re.compile(r"(`{3,}|~{3,})[\s\S]*?(?:\1|\Z)")
_QUOTED_OR_CODE_RE = re.compile(r'`([^`\n]+)`|"([^"\n]*)"')
_EXAMPLE_CUE_RE = re.compile(r"\b(?:example|e\.g\.|for\s+instance|such\s+as|usage|syntax|output|error|prints|printed|matched|refused)\b", re.IGNORECASE)
_EXEC_PREFIX_RE = re.compile(r"\b(?:(?:please|pls|(?:can|could|would)\s+(?:you|(?:one|either)\s+of\s+you|someone|anyone)|go\s+ahead\s+and|i'?ll|i\s+will|i\s+am|i'?m)(?:\s+(?:please|kindly|just|now))*(?:\s+(?:run|execute|use|invoke|do))?|(?:run|execute|use|invoke|running|executing)|then(?:\s+(?:a|do\s+a|run|execute|use))?)\s*:?\s*$", re.IGNORECASE)
_VERDICT_LINE_RE = re.compile(r"^\s*VERDICT\s*:", re.IGNORECASE)
_SEAT_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_MSG_OR_CARD_KEY_RE = re.compile(r"\b(?:msg/[0-9]+-[a-z0-9_.-]+-[0-9a-f]+|card/[a-z0-9_.-]+/[0-9a-f]+)\b", re.IGNORECASE)
_SHA40_TOKEN_RE = re.compile(r"(?<![\w-])([0-9a-fA-F]{40})(?![\w-])")
_NON_HEAD_SHA_PREFIX_RE = re.compile(r"\b(?:origin/main|main|trunk|master|base(?:\s+(?:head|commit|sha|oid))?|merge-base|onto|parent|on\s+top\s+of|old(?:\s+(?:head|commit|sha|oid))?|prior(?:\s+(?:head|commit|sha|oid))?|prev(?:ious)?(?:\s+(?:head|commit|sha|oid))?|superseded|stale|was|from|before|sha256|tree)\s*(?:[:=@-]|is)?\s*(?:https?://\S*/commit/)?$", re.IGNORECASE)
_SHORT_HEAD_LABEL_RE = re.compile(r"(?<![\w-])(?:new\s+|current\s+|remote[_ -]?)?(?:head|commit)\s*[:=@]\s*([0-9a-fA-F]{7,39})(?![\w-])", re.IGNORECASE)
_MERGER_DECL_RE = re.compile(r"(?:^|[;(\n]|(?:#\d+|\bPR\s*#?\d+))\s*merger\s*:\s*((?:(?!(?<![\w-])merger\s*:)[^\n;)])*)", re.IGNORECASE)
_AMBIGUOUS_MERGERS = frozenset({"any", "anyone", "anybody", "either", "both", "all", "someone", "somebody", "whoever", "tbd", "todo", "none", "null", "nil", "na", "unassigned", "unknown", "open"})
_MERGE_TECH_TAIL = r"(?!\s*(?:-?base|-?tree|conflict|conflicts|commit(?!\s+(?:https?://\S*/commit/)?[0-9a-fA-F]{7,40}\b)|commits\b(?!\s+[0-9a-fA-F]{7,40}\b)|gate|gates|queue|train|window|check|checks|preflight|guard)\b)"
_MERGE_VERB = r"(?:pinned\s+squash(?:\s*[- ]?merge)?|squash[- ]?merge|squash\s+and\s+merge|merge|squash)"
_HOLD_MERGE_RE = re.compile(r"\b(?:do\s+not|don'?t|never)\s+" + _MERGE_VERB + r"\b|\bhold\s+(?:off\s+(?:on\s+)?(?:merging|merge)|(?:the\s+)?merge)\b|\bnot\s+(?:yet\s+)?ready\s+(?:to|for)\s+(?:a\s+)?" + _MERGE_VERB + r"\b|\bno\s+merge\s+yet\b|\bwait\s+to\s+merge\b", re.IGNORECASE)
_GH_PR_MERGE_ASK_RE = re.compile(r"(?:^|[.!?;,\n]\s*|\b(?:please|pls|(?:can|could|would)\s+(?:you|someone|anyone)|go\s+ahead\s+and|run|execute|use|then|and\s+then)\s+)`?gh\s+pr\s+merge(?:\s+#?\d+\b|\s+\S*/pull/\d+\b|\s+--)", re.IGNORECASE)
_PR_REF_RE = re.compile(r"(?:#\d+\b|\bPR\s*#?\d+\b|/pull/\d+\b|\b(?:this|that)\s+PR\b)", re.IGNORECASE)
_POLITE_MERGE_ASK_RE = re.compile(r"\b(?:please|pls|(?:can|could)\s+(?:you|(?:one|either)\s+of\s+you|someone|anyone)|would\s+you|go\s+ahead\s+and)\s+(?:please\s+|kindly\s+|just\s+|now\s+)?(?:(?:review|gate|check|verify|approve)\s+and\s+)?(?:do\s+a\s+)?" + _MERGE_VERB + r"\b" + _MERGE_TECH_TAIL, re.IGNORECASE)
_MULTI_PEER_MERGE_ASK_RE = re.compile(r"\b(?:someone|somebody|anyone|anybody|(?:either|one|both)\s+of\s+you|whoever(?:\s+is\s+\w+(?:\s+first)?)?)\s+(?:can\s+|could\s+|may\s+|please\s+|pls\s+)?(?:do\s+a\s+)?(?:non-author\s+|pinned\s+)*" + _MERGE_VERB + r"\b" + _MERGE_TECH_TAIL, re.IGNORECASE)
_IMPERATIVE_MERGE_PR_RE = re.compile(r"(?:^|[.!?;,\n]\s*|:(?!//|\d+/)\s*|\bthen\s+|\band\s+then\s+)(?:please\s+|pls\s+)?" + _MERGE_VERB + r"\s+(?:commit\s+(?:https?://\S*/commit/)?[0-9a-fA-F]{7,40}\s+(?:for|on|of|in)\s+)?(?:(?:PR\s*)?#\d+\b|\S*/pull/\d+\b)", re.IGNORECASE)
_PINNED_SQUASH_ASK_RE = re.compile(r"\bthen\s+(?:a\s+|do\s+a\s+)?(?:non-author\s+)?pinned\s+squash\b", re.IGNORECASE)
_READY_MERGE_RE = re.compile(r"\b(?:ready\s+(?:to|for)|ok(?:ay)?\s+to|good\s+to|clear\s+to|time\s+to)\s+(?:do\s+)?(?:(?:a|an|the|non-author|pinned|clean|row[- ]805)\s+)*" + _MERGE_VERB + r"\b" + _MERGE_TECH_TAIL, re.IGNORECASE)
_ASK_NEGATION_RE = re.compile(r"\b(?:(?:not|never|no|neither|nor|nobody|no\s+one|without|avoid|refrain\s+from|stop|don'?t|doesn'?t|didn'?t|won'?t|can'?t|cannot|shouldn'?t|mustn'?t)(?:\s+(?:ever|just|now|yet|to|a|any))?(?:\s+(?:ask(?:ing)?|tell(?:ing)?|let(?:ting)?|allow(?:ing)?|have|having|want(?:ing)?|need(?:ing)?|say(?:ing)?|suggest(?:ing)?|know(?:ing)?|check(?:ing)?)(?:\s+[\w@-]+){0,3}(?:\s+to)?)?|no\s+(?:need|reason)\s+to)\s*$", re.IGNORECASE)
_CLAUSE_NEG_OR_COND_RE = re.compile(r"\b(?:(?:not|never|isn'?t|aren'?t|wasn'?t|weren'?t)(?:\s+(?:yet|quite|even|really|actually|fully))?\s*$|(?<!with\s)(?:(?:no|neither)\s+(?:pr|pull\s+request|branch|item|change|one)|none(?:\s+of\s+\w+)?|nothing)\s+(?:is|are|was|were)(?:\s+yet)?\s*$|(?:when|once|if|until|unless|whether|before|after)\b(?![\s\S]*\b(?:so|then)\b))", re.IGNORECASE)
_CLAUSE_SPLIT_RE = re.compile(r"(?:[!?;,\n]+|\.(?=\s|$)|\b(?:but|however|though|although|while|whereas)\b)+", re.IGNORECASE)


def strip_fences_and_quotes(body: str) -> str:
    """Strip fenced/indented/quoted examples while preserving inline or fenced commands explicitly requested for execution."""
    if not isinstance(body, str) or not body:
        return ""
    exec_ok = lambda pre, c_pre: not _EXAMPLE_CUE_RE.search(pre) and not _ASK_NEGATION_RE.search(c_pre) and bool(_EXEC_PREFIX_RE.search(c_pre))
    def _fence(m: re.Match[str]) -> str:
        last = (body[: m.start()].rstrip(" \t").splitlines() or [""])[-1]
        cmds = [s for s in (x.strip() for x in m.group(0).splitlines()[1:]) if re.match(r"gh\s+pr\s+merge\b", s, re.I)] if exec_ok(last, _CLAUSE_SPLIT_RE.split(last)[-1]) else []
        return ("\n" + "\n".join(cmds) + "\n") if cmds else "\n"
    out, prev_ex, prev_exec = [], False, False
    for ln in _FENCE_UNCLOSED_RE.sub(_fence, body).splitlines():
        if ln.lstrip().startswith(">") or ln.startswith(("    ", "\t")):
            if ln.startswith(("    ", "\t")) and prev_exec and re.match(r"gh\s+pr\s+merge\b", ln.strip(), re.I):
                out.append(ln.strip())
            prev_ex = False
            continue
        def _span(m: re.Match[str]) -> str:
            inner, is_code = (m.group(1) if m.group(1) is not None else m.group(2)).strip(), m.group(1) is not None
            if is_code and not re.search(r"\s", inner):
                return inner
            pre, c_pre = ln[: m.start()], _CLAUSE_SPLIT_RE.split(ln[: m.start()])[-1]
            if not prev_ex and (exec_ok(pre, c_pre) or (is_code and re.match(r"gh\s+pr\s+merge\b", inner, re.I) and not _EXAMPLE_CUE_RE.search(pre) and not c_pre.strip())):
                return f" {inner} "
            return " "
        out.append(_QUOTED_OR_CODE_RE.sub(_span, ln))
        prev_ex, prev_exec = bool(_EXAMPLE_CUE_RE.search(ln)), exec_ok(ln, _CLAUSE_SPLIT_RE.split(ln)[-1])
    return "\n".join(out)


def extract_merger_declarations(clean: str) -> list[str]:
    """Extract raw `merger: <value>` declarations from cleaned text, trimming trailing sentence punctuation."""
    return [m.group(1).strip().rstrip(".,!?:").strip() for m in _MERGER_DECL_RE.finditer(clean)]


def _extract_candidate_head_shas(clean: str) -> tuple[list[str], list[str]]:
    scrubbed, full_shas, short_labeled = _MSG_OR_CARD_KEY_RE.sub(" ", clean), [], []
    non_head = lambda pos: _NON_HEAD_SHA_PREFIX_RE.search(re.split(r"(?:[!?;,\n]+|\.(?=\s|$))", scrubbed[:pos])[-1][-60:])
    for m in _SHA40_TOKEN_RE.finditer(scrubbed):
        if not non_head(m.start()) and (sha := m.group(1).lower()) not in full_shas:
            full_shas.append(sha)
    for m in _SHORT_HEAD_LABEL_RE.finditer(scrubbed):
        if not non_head(m.start()) and (s := m.group(1).lower()) not in short_labeled and not (len(full_shas) == 1 and full_shas[0].startswith(s)):
            short_labeled.append(s)
    return full_shas, short_labeled


def _is_merge_request(clean: str) -> bool:
    prose = "\n".join(ln for ln in clean.splitlines() if not _VERDICT_LINE_RE.match(ln))
    clauses = [cl.strip() for cl in _CLAUSE_SPLIT_RE.split(prose) if cl.strip() and not _HOLD_MERGE_RE.search(cl)]
    if not clauses:
        return False
    if any((m := _GH_PR_MERGE_ASK_RE.search(cl)) and not _ASK_NEGATION_RE.search(cl[: m.start()]) for cl in clauses):
        return True
    if not _PR_REF_RE.search("\n".join(clauses)):
        return False
    ask_rxs = (_POLITE_MERGE_ASK_RE, _MULTI_PEER_MERGE_ASK_RE, _IMPERATIVE_MERGE_PR_RE, _PINNED_SQUASH_ASK_RE)
    return (any((m := rx.search(cl)) and not _ASK_NEGATION_RE.search(cl[: m.start()]) for cl in clauses for rx in ask_rxs)
            or any((m := _READY_MERGE_RE.search(cl)) and not _CLAUSE_NEG_OR_COND_RE.search(cl[: m.start()]) for cl in clauses))


def check_merge_request(body: str, *, to: list[str] | None, cc: list[str] | None) -> str | None:
    """Require any explicit PR merge ask to name a single recipient `merger: <seat>` and one 40-hex head SHA."""
    clean = strip_fences_and_quotes(body)
    if not _is_merge_request(clean):
        return None
    decls = extract_merger_declarations(clean)
    if not decls:
        return "merge request requires a single `merger: <seat>` line naming one recipient in --to/--cc and a full 40-hex commit SHA."
    if len(decls) >= 2:
        return f"merge request names {len(decls)} `merger:` declarations; name exactly one `merger: <seat>`."
    raw_target = decls[0]
    target = raw_target.casefold()
    if not target or not _SEAT_LABEL_RE.fullmatch(target) or target in _AMBIGUOUS_MERGERS:
        return f"merge request has ambiguous or invalid `merger: {raw_target}`; name a single seat label in --to/--cc."
    rcpts = recipients(to, cc)
    if target not in rcpts:
        return f"merge request names `merger: {target}`, which is not among message recipients ({', '.join(rcpts) or 'none'}); include {target} in --to or --cc."
    full_shas, short_labeled = _extract_candidate_head_shas(clean)
    if not full_shas or short_labeled:
        return f"merge request naming `merger: {target}` must include the full 40-hex head commit SHA (for `gh pr merge --match-head-commit <40-hex>`) alongside `merger: {target}`."
    if len(full_shas) > 1:
        return f"merge request names multiple distinct 40-hex commit SHAs ({', '.join(full_shas)}); pin a single 40-hex head commit SHA."
    return None


def refusals(state_file: Path, *, to: list[str] | None, cc: list[str] | None, body: str,
             now: float | None = None, pair_loop: bool = True) -> list[str]:
    """The reasons this send is refused, each naming its number and its override. Empty means send.
    Raises LedgerUnreadable when the volume cannot be counted."""
    now = time.time() if now is None else now
    out: list[str] = []
    size = len(body.encode("utf-8"))
    if size > MAX_BODY_BYTES:
        out.append(f"the body is {size} bytes, over the {MAX_BODY_BYTES} byte limit. A bundle of source files belongs in a "
                   f"commit: use `share` (divergence 064) and send `owner/name@<sha>:<path>` instead of the bytes. A large "
                   f"text artifact (a log, a kit, a report) goes as one file: `send --attach FILE` with a short body "
                   f"(divergence 079), never split into parts.")
    if is_file_bundle(body):                           # an announced bundle (divergence 068, second amendment)
        new = token_hashes(body)
        seen = _token_entries(token_ledger_path(state_file), now)
        for label in recipients(to, cc):
            named = new & {h for e in seen if e["to"] == label for h in e["h"]}
            if named:
                out.append(f"this body is mostly file content and names {len(named)} path(s) or file name(s) "
                           f"this identity already named to {label} in the last {WINDOW_S // 60} minutes: the files were "
                           f"announced, so send a pointer (`share`, `owner/name@<sha>:<path>`) or one `--attach` file. If "
                           f"{label} asked for these bytes by name, say so in --bulk-ack.")
    pair_seen = _pair_entries(pair_ledger_path(state_file), now) if pair_loop else []
    for label in recipients(to, cc):
        pair_why = check_pair_loop(pair_seen, label, now)
        if pair_why:
            out.append(pair_why)
    merge_why = check_merge_request(body, to=to, cc=cc)
    if merge_why:
        out.append(merge_why)
    if size < COUNTED_MIN_BYTES:                       # a short message is never refused for volume (divergence 068)
        return out
    entries = _entries(ledger_path(state_file), now)
    for label in recipients(to, cc):
        n = sum(1 for e in entries if e["to"] == label)
        if n >= MAX_PER_RECIPIENT:
            out.append(f"this identity has already sent {n} messages of {COUNTED_MIN_BYTES} bytes or more naming {label} "
                       f"in the last {WINDOW_S // 60} minutes (limit {MAX_PER_RECIPIENT}), and this one is that large too. "
                       f"Fold the revisions into one message, or put the files in a commit and send it with `share`.")
    return out


UNTRUSTED_TRANSCRIPT_BANNER = (
    "[QUOTED UNTRUSTED TRANSCRIPT EVIDENCE - may hold live secrets; never paste past outbound guards]"
)
_UNTRUSTED_TRANSCRIPT_SENTINEL = "[QUOTED UNTRUSTED TRANSCRIPT EVIDENCE"
_UNTRUSTED_TRANSCRIPT_JSON_RE = re.compile(r'"_untrusted_transcript_evidence"\s*:\s*true\b', re.IGNORECASE)


def refuse_untrusted_transcript_paste(body: str, refused=ValueError) -> None:
    """Non-overridable outbound backstop against accidental pastes of transcript-search / transcript-read output.

    shared/transcript-context stores live session secrets as written, so neither human output
    (marked on the banner and on every rendered item by `[QUOTED UNTRUSTED TRANSCRIPT EVIDENCE`)
    nor `--json` output (`"_untrusted_transcript_evidence": true`) may be sent on MeshyChat, even
    with `--bulk-ack`. This is a client-side backstop for accidental pastes (whole outputs or
    copied item blocks with their header), not a cryptographic barrier against a seat that
    deliberately strips the marker before sending.
    """
    if _UNTRUSTED_TRANSCRIPT_SENTINEL in body or _UNTRUSTED_TRANSCRIPT_JSON_RE.search(body):
        raise refused(
            "refused: raw transcript-search / transcript-read output from shared/transcript-context "
            "may hold live secrets and cannot be sent on MeshyChat; cite ctx/... keys and seats instead"
        )


def admit(state_file: Path, *, to: list[str] | None, cc: list[str] | None, body: str, subject: str | None,
          bulk_ack: str | None, refused, now: float | None = None, pair_loop: bool = True) -> tuple[str, str | None]:
    """The (body, subject) to send, or `refused` (node.Refused) raised with nothing sent.

    With a reason the refusals are overridden and the reason is written into the message. A reason given when nothing
    was refused is written too: it is a claim the recipient should be able to read."""
    refuse_untrusted_transcript_paste(body, refused=refused)
    try:
        reason = clean_reason(bulk_ack)
    except ValueError as exc:
        raise refused(str(exc))
    try:
        why = refusals(state_file, to=to, cc=cc, body=body, now=now, pair_loop=pair_loop)
    except LedgerUnreadable as exc:
        if reason is None:
            raise refused(f"refused before sending: the send ledger {ledger_path(state_file)} is unreadable ({exc}), so the "
                          f"volume cannot be counted. Repair or move it, or pass --bulk-ack \"<reason>\".")
        why = []
    if why and reason is None:
        raise refused("refused before sending: " + " ".join(why) +
                      " To send anyway pass --bulk-ack \"<reason>\"; the reason is written into the message.")
    if reason is None:
        return body, subject
    return f"[bulk-ack] {reason}\n{body}", subject if subject else "bulk-ack"


def record(state_file: Path, *, to: list[str] | None, cc: list[str] | None, key: str,
           now: float | None = None, body_bytes: int | None = None, body: str | None = None,
           idempotent: bool = False) -> str | None:
    """Add one entry per recipient label for a LARGE send the server ACCEPTED, and trim past the window. Never a body.
    `body_bytes` under COUNTED_MIN_BYTES adds nothing (the send is not counted); None counts it, as before the amendment.
    An unreadable ledger is replaced by a fresh one holding this send.
    It runs AFTER the server accepted the message, so it never raises: a failure to record (the lock held past
    LOCK_WAIT_S, a lock file or ledger the OS refuses, a ledger that does not decode) returns the reason, and the caller reports it without failing the
    send. A raise here became exit 2 "unreachable", and a caller that retries an exit 2 sends the message twice.
    `idempotent` (the operation path) skips a recipient that already holds this non-empty key in the window.
    Returns None when recorded."""
    try:
        return _record(state_file, to=to, cc=cc, key=key, now=now, body_bytes=body_bytes, body=body,
                       idempotent=idempotent)
    except Exception as exc:                           # noqa: BLE001 - any bookkeeping failure, never the send's
        return f"the send ledger could not be written ({type(exc).__name__})"


def _record(state_file: Path, *, to, cc, key, now, body_bytes, body, idempotent=False) -> str | None:
    now = time.time() if now is None else now
    path = ledger_path(state_file)
    with _ledger_lock(state_file) as owned:
        if not owned:                                  # never write without the lock: this send goes unrecorded, said so
            return f"the send ledger lock was held past {LOCK_WAIT_S:.0f} s"
        try:
            entries = _entries(path, now)
        except LedgerUnreadable:
            entries = []
        # `idempotent` is the operation path's: a crash between this write and the journal's guard_counted flag makes
        # the next status, retry or drain record the same send again, so a recipient already holding this non-empty key
        # in the window is not charged twice (GitHub Codex r4366608260, pxx).
        stored_key = str(key or "")
        already = ({entry.get("to") for entry in entries if stored_key and entry.get("key") == stored_key}
                   if idempotent else set())
        if body_bytes is None or body_bytes >= COUNTED_MIN_BYTES:
            entries.extend({"to": label, "t": now, "key": stored_key} for label in recipients(to, cc)
                           if label not in already)
        _write_json(path, entries)
        if body is not None:                           # every accepted send, any size: an announcement is usually short
            try:                                       # the bundle rule fails open: its ledger never fails a send
                hashes = sorted(token_hashes(body))
                tpath = token_ledger_path(state_file)
                tentries = _token_entries(tpath, now)
                # Each ledger is deduplicated by its own entries: a crash between the volume write above and this one
                # must not make the retry skip the token entries the first try never wrote (GitHub Codex r4366959130).
                tokened = ({entry.get("to") for entry in tentries if stored_key and entry.get("key") == stored_key}
                           if idempotent else set())
                if hashes:
                    tentries.extend({"to": label, "t": now, "h": hashes, "key": stored_key}
                                    for label in recipients(to, cc) if label not in tokened)
                _write_json(tpath, tentries)
            except OSError:
                pass
        if not (body is None and body_bytes is None):
            try:                                       # pairwise loop ledger: every accepted send, with or without body
                ppath = pair_ledger_path(state_file)
                pentries = _pair_entries(ppath, now)
                paired = ({entry.get("to") for entry in pentries if stored_key and entry.get("key") == stored_key}
                          if idempotent else set())
                pentries.extend({"to": label, "t": now, **({"key": stored_key} if stored_key else {})}
                                for label in recipients(to, cc) if label not in paired)
                _write_json(ppath, pentries)
            except OSError:
                pass
    return None
