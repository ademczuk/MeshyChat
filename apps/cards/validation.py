"""What memory.write refuses in the decision-card namespace (divergence 091; docs/design/decision-cards.md 4).

A card is a recorded decision operator decision a seat's loader (tools/transcript_cards) took from that
seat's own transcripts. Every seat reads every card (the operator, 2026-10-08T08:20:36Z), so what a
card SAYS about who decided, and which earlier card it replaces, is read as fact by every other seat.
These rules are what keeps that true.

Two halves, as the mail's own rules are split:

  `validate_card`      no database: the key, the fields, their types, the text and its digest.
  `enforce_card`       beside the row: the seat is the caller's, a later version is its first writer's,
                       and every `supersedes` target is an earlier head card.

`status` is never stored as anything but "active". Whether a card is superseded is DERIVED when it is
read (apps/cards/store.py): a seat cannot write into another seat's card, so it could not mark it.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

from apps.core import env
from apps.memoryskills.errors import HandlerError
from apps.memoryskills.validation import MESH_LABEL_RE, MESH_THREAD_RE
from apps.meshsem import graphtext

#: The namespace cards live in. A setting so a test can claim its own.
DEFAULT_NAMESPACE = "shared/decisions"
SCHEMA = "card.v1"
OPERATOR = "operator"
KEY_RE = re.compile(r"^card/([a-z0-9][a-z0-9_.-]{0,63})/([0-9a-f]{32})$")
#: Ruling: the operator's answer to a question. Question: asked, not yet answered. Candidate: an
#: operator-typed prompt. Commitment: a seat's "I am doing X, done when Y". Claim: model-written.
TYPES = frozenset({"Ruling", "Question", "Candidate", "Decision", "Commitment", "OpenItem", "Constraint",
                   "Claim", "Context"})
#: The loader's cut (tools/transcript_cards), and the walker's one-chunk window (graphtext.MAX_CHARS).
MAX_TEXT_CHARS = 1500
MAX_SUPERSEDES = 16
#: The largest card of the first measured run (1,276 cards) is 3,576 bytes.
MAX_VALUE_BYTES = 8_192
#: How far ahead of the server a card's `time` may be: clock skew, no more. A later `time` would
#: sort a card above every real one and let it supersede cards not yet written.
MAX_AHEAD = timedelta(minutes=5)

REQUIRED_FIELDS = frozenset({"schema", "id", "type", "owner", "seat", "time", "text", "text_sha256", "source"})
#: The loader's other fields at version 1.2, and `supersedes`. An unknown field is refused, so a new
#: loader field is a server change someone reviews.
OPTIONAL_FIELDS = frozenset({
    "date", "status", "question", "options", "answer", "notes", "free_text", "long_free_text",
    "multi_select", "truncated", "short", "has_attachment", "legacy_origin", "prompt_source",
    "slash_command", "parent_uuid", "loader_version", "supersedes", "thread_id", "done_when",
    # Loader 1.4: the earliest card of a text an operator typed again counts its repeats. A later
    # copy carries `repeat_of` and is NOT written: that field is unknown here on purpose.
    "repeats", "repeat_last_time",
    # Loader 1.19 (gap register item 7, txxxx's field list, msg/<key>): who said a card's text,
    # whose words it quotes, how it was extracted and whether the operator confirmed it. Checked by _check_attribution.
    "attribution",
    # approvals_cards.py (<thread>, kxxx): a Ruling read from a seat's approvals table keeps
    # that seat's reading of the answer here, apart from `answer`, which holds the operator's words only.
    "seat_effect",
    # Issue #297 (kxxx ruling msg/<key>): bounded structured references on a card (branch, prs,
    # threads, repo-relative files). Checked by _check_refs.
    "refs",
})
#: `refs`'s keys and bounds (#297).
REFS_KEYS = frozenset({"branch", "prs", "threads", "files"})
MAX_REF_BRANCH_CHARS = 120
MAX_REF_PRS = 8
MAX_REF_PR_NUMBER = 999_999
MAX_REF_THREADS = 8
MAX_REF_FILES = 16
MAX_REF_FILE_CHARS = 200
_PR_REF_RE = re.compile(r"^#[1-9]\d{0,5}$")
#: `attribution`'s keys and values. Exactly these keys; `quoted_author` alone may be null ("nothing quoted"), which is
#: not "unknown" ("quoted, author not given"). "promoted" is never written: item 6's promotion is the server's or a
#: later card's word, never a writer's.
ATTRIBUTION_KEYS = frozenset({"speaker", "quoted_author", "extraction", "confirmation"})
SPEAKERS = frozenset({"operator", "agent"})
CONFIRMATIONS = frozenset({"answered", "typed", "asked"})
_EXTRACTION_RE = re.compile(r"^[a-z_]{1,31}/[a-z_]{1,32}$")
MAX_DONE_WHEN_CHARS = 500
SOURCE_REQUIRED = frozenset({"record_uuid", "block_index", "kind", "first_hand"})
#: `transcript_uuid`: the transcript record a Ruling was mined from, when `record_uuid` is the
#: question call's id (loader 1.7). Never frozen: the hook that writes a Ruling at answer time has none.
SOURCE_OPTIONAL = frozenset({"source_sha256", "sessions", "files", "other_content", "transcript_uuid"})
#: The loader versions whose ids are stable. Before 1.4 the id held a hash of the kept fields, so a card
#: cut differently became a second card; before 1.7 a Ruling was keyed on the transcript record, not on
#: the question call and index the answer-time hook also knows, so a hook-written Ruling and the same
#: answer mined later were two cards. (txxxx, msg/<key>.)
MIN_LOADER_VERSION = (1, 4)
MIN_LOADER_VERSION_RULING = (1, 7)
_LOADER_VERSION_RE = re.compile(r"^(\d{1,4})\.(\d{1,4})$")
#: The walker embeds the LONGEST content-class string of a row and prefixes `title`
#: (graphtext.row_text). A card's prose is `text` alone, so no other such field may exist: a longer
#: one would be embedded in its place, and search would rank the card by text no reader is shown.
FORBIDDEN_PROSE_FIELDS = frozenset(graphtext.CONTENT_CLASS) - {"text"} | {"title"}

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
_BOOL_FIELDS = ("free_text", "long_free_text", "multi_select", "truncated", "short", "has_attachment",
                "legacy_origin")
_STR_FIELDS = ("date", "question", "answer", "notes", "prompt_source", "slash_command", "loader_version",
               "owner", "seat_effect")


def namespace() -> str:
    """The card namespace: MESHY_CARDS_NAMESPACE, else shared/decisions."""
    return str(env.setting("MESHY_CARDS_NAMESPACE", "MESHY_CARDS_NAMESPACE", default=DEFAULT_NAMESPACE))


def is_card_namespace(ns) -> bool:
    return ns == namespace()


def _refuse(message: str, code: str = "E_CARD_SCHEMA"):
    raise HandlerError(code, message)


def parse_time(raw) -> datetime:
    """A card's `time`: exactly `YYYY-MM-DDTHH:MM:SS.mmmZ`, as an aware UTC datetime.

    ONE spelling, the loader's own (all 1,276 cards of the first measured run carry it), because the
    read route orders and pages by the STRING: with a second spelling allowed ("...:17Z" beside
    "...:17.834Z", or an offset), string order stops being time order and a card sorts out of place."""
    if not isinstance(raw, str) or not TIME_RE.fullmatch(raw):
        _refuse("time must be a UTC instant written YYYY-MM-DDTHH:MM:SS.mmmZ, for example "
                "2026-10-08T07:37:17.834Z", "E_CARD_TIME")
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError:
        _refuse(f"time {raw!r} is not a real instant", "E_CARD_TIME")


def _is_option(option) -> bool:
    """An offered answer: its label, or the loader's {label, description}."""
    if isinstance(option, str):
        return True
    return (isinstance(option, dict) and set(option) <= {"label", "description"} and "label" in option
            and all(isinstance(v, str) for v in option.values()))


def key_parts(key) -> tuple[str, str] | None:
    """(seat label, id) of a card key, or None."""
    m = KEY_RE.fullmatch(str(key))
    return (m.group(1), m.group(2)) if m else None


def validate_card(ns, key, value, *, now: datetime | None = None) -> bool:
    """True when (ns, key) is a card and `value` passed every rule that needs no database. False when
    `ns` is not the card namespace (nothing is checked). Raises HandlerError otherwise."""
    if not is_card_namespace(ns):
        return False
    parts = key_parts(key)
    if parts is None:
        _refuse(f"{ns} holds cards only: the key must be card/<seat>/<32 lower-case hex>, not {str(key)[:80]!r}",
                "E_CARD_KEY")
    seat, card_id = parts
    if not isinstance(value, dict):
        _refuse("a card's value must be an object")
    try:
        raw_bytes = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except UnicodeEncodeError:
        _refuse("a card's value must be valid UTF-8 (no lone surrogates)")
    if len(raw_bytes) > MAX_VALUE_BYTES:
        _refuse(f"a card's value is at most {MAX_VALUE_BYTES} bytes of JSON")

    fields = set(value)
    banned = sorted(fields & FORBIDDEN_PROSE_FIELDS)
    if banned:
        _refuse(f"a card's prose is `text` alone; {', '.join(banned)} would be embedded in its place")
    missing = sorted(REQUIRED_FIELDS - fields)
    if missing:
        _refuse(f"missing required fields: {', '.join(missing)}")
    unknown = sorted(fields - REQUIRED_FIELDS - OPTIONAL_FIELDS)
    if unknown:
        _refuse("unknown fields: " + ", ".join(repr(f) for f in unknown))

    if value["schema"] != SCHEMA:
        _refuse(f"schema must be {SCHEMA!r}")
    if value["id"] != card_id:
        _refuse("id must equal the key's last segment", "E_CARD_KEY")
    if value["seat"] != seat:
        _refuse(f"seat {value['seat']!r} is not the key's seat {seat!r}: a seat writes its own cards",
                "E_CARD_SEAT_MISMATCH")
    if not isinstance(value["seat"], str) or not MESH_LABEL_RE.fullmatch(value["seat"]):
        _refuse("seat must be a normalized lowercase label")
    if not isinstance(value["type"], str) or value["type"] not in TYPES:
        _refuse(f"type must be one of {', '.join(sorted(TYPES))}")
    for name in _STR_FIELDS:
        if name in value and not isinstance(value[name], str):
            _refuse(f"{name} must be a string")
    if value.get("parent_uuid") is not None and not isinstance(value["parent_uuid"], str):
        _refuse("parent_uuid must be a string or null")
    # WHO DECIDED. "operator" is the seat's own attestation that the operator typed or answered it
    # (the loader's and the hook's value; the server cannot check it, and the signature says which
    # seat attests). Otherwise the seat itself. Never ANOTHER seat: a card cannot put a decision in
    # a seat's mouth that did not sign it. A Context card is the seat's own working-context snapshot
    # (#297), never the operator's decision.
    if value["owner"] not in (OPERATOR, value["seat"]):
        _refuse(f"owner is {OPERATOR!r} or the card's own seat, not {str(value['owner'])[:64]!r}")
    if value["type"] == "Context" and value["owner"] != value["seat"]:
        _refuse(f"a Context card's owner is its seat ({value['seat']!r}), not {OPERATOR!r}")
    for name in _BOOL_FIELDS:
        if name in value and type(value[name]) is not bool:
            _refuse(f"{name} must be true or false")
    if "status" in value and value["status"] != "active":
        _refuse("status, when present, is 'active': superseded is derived from the cards that name "
                "this one in `supersedes`, never stored")
    if "loader_version" in value:
        m = _LOADER_VERSION_RE.fullmatch(value["loader_version"])
        if m is None:
            _refuse("loader_version must be two numbers, for example 1.7")
        # Compared as NUMBERS: as text, "1.10" sorts below "1.4".
        floor = MIN_LOADER_VERSION_RULING if value["type"] == "Ruling" else MIN_LOADER_VERSION
        if (int(m.group(1)), int(m.group(2))) < floor:
            _refuse(f"a {value['type']} from loader {value['loader_version']} is not written: its id is not "
                    f"stable before {floor[0]}.{floor[1]}; rebuild the store with the current loader",
                    "E_CARD_LOADER")
    if "thread_id" in value and (not isinstance(value["thread_id"], str)
                                 or not MESH_THREAD_RE.fullmatch(value["thread_id"])):
        _refuse("thread_id must be a mesh thread id")
    if "done_when" in value and (not isinstance(value["done_when"], str) or not value["done_when"].strip()
                                 or len(value["done_when"]) > MAX_DONE_WHEN_CHARS):
        _refuse(f"done_when must be a non-blank string of at most {MAX_DONE_WHEN_CHARS} characters")
    if "options" in value:
        opts = value["options"]
        if not isinstance(opts, list) or len(opts) > 32 or not all(_is_option(o) for o in opts):
            _refuse("options must be an array of at most 32 options, each a string or "
                    "{label, description} with string values")
    if "repeats" in value and (type(value["repeats"]) is not int or value["repeats"] < 1):
        _refuse("repeats must be a positive integer")
    if "repeat_last_time" in value:
        parse_time(value["repeat_last_time"])

    when = parse_time(value["time"])
    current = now or datetime.now(timezone.utc)
    if when > current + MAX_AHEAD:
        _refuse(f"time {value['time']!r} is ahead of the server's clock; a card records a decision "
                "already made", "E_CARD_TIME")

    text = value["text"]
    if not isinstance(text, str) or not text.strip():
        _refuse("text must be a non-blank string")
    if len(text) > MAX_TEXT_CHARS:
        _refuse(f"text is at most {MAX_TEXT_CHARS} characters")
    digest = value["text_sha256"]
    if not isinstance(digest, str) or not _HEX64.fullmatch(digest):
        _refuse("text_sha256 must be 64 lower-case hex characters")
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != digest:
        _refuse("text_sha256 is not the SHA-256 of text", "E_CARD_DIGEST")

    source = value["source"]
    if not isinstance(source, dict):
        _refuse("source must be an object")
    s_missing = sorted(SOURCE_REQUIRED - set(source))
    if s_missing:
        _refuse(f"source is missing: {', '.join(s_missing)}")
    s_unknown = sorted(set(source) - SOURCE_REQUIRED - SOURCE_OPTIONAL)
    if s_unknown:
        _refuse("source has unknown fields: " + ", ".join(repr(f) for f in s_unknown))
    if not isinstance(source["record_uuid"], str) or not isinstance(source["kind"], str):
        _refuse("source.record_uuid and source.kind must be strings")
    if type(source["block_index"]) is not int or source["block_index"] < 0:
        _refuse("source.block_index must be a non-negative integer")
    if type(source["first_hand"]) is not bool:
        _refuse("source.first_hand must be true or false")
    if value["type"] == "Context" and source["first_hand"] is not False:
        _refuse("a Context card is the seat's working-context snapshot, not a first-hand operator decision: "
                "source.first_hand must be false")
    if "attribution" in value:
        _check_attribution(value)
    if "refs" in value:
        _check_refs(value["refs"])

    targets = supersedes_of(value, own_key=str(key))
    if value["type"] == "Context" and len(targets) > 1:
        _refuse("a Context card supersedes at most 1 card (its seat's previous Context head)", "E_CARD_SUPERSEDES")
    return True


def _check_attribution(value) -> None:
    """`attribution`'s shape, and its agreement with the fields every card already has. a recorded decision agreement is
    NOT checked here: it holds only for a card born with attribution (enforce_card), because every card written before
    loader 1.19 says first_hand true and that field is frozen."""
    att = value["attribution"]
    if not isinstance(att, dict) or set(att) != ATTRIBUTION_KEYS:
        _refuse("attribution is an object with exactly speaker, quoted_author, extraction and confirmation")
    if att["speaker"] not in SPEAKERS:
        _refuse(f"attribution.speaker is one of {', '.join(sorted(SPEAKERS))}")
    if att["confirmation"] not in CONFIRMATIONS:
        _refuse(f"attribution.confirmation is one of {', '.join(sorted(CONFIRMATIONS))}; a promotion is never "
                "written by a seat")
    qa = att["quoted_author"]
    if qa is not None and qa != "unknown" and not (isinstance(qa, str) and MESH_LABEL_RE.fullmatch(qa)):
        _refuse("attribution.quoted_author is null, \"unknown\" or a mesh label")
    if not isinstance(att["extraction"], str) or not _EXTRACTION_RE.fullmatch(att["extraction"]):
        _refuse("attribution.extraction is <dialect>/<kind>, lower-case letters and underscores")
    # Agreement with the card's type: two fields that could say opposite things are refused, not explained.
    if value["type"] == "Ruling" and (att["confirmation"] != "answered" or att["speaker"] != "operator"):
        _refuse("a Ruling is the operator's answer: attribution.confirmation \"answered\", speaker \"operator\"")
    if att["confirmation"] == "asked" and value["type"] != "Question":
        _refuse("attribution.confirmation \"asked\" is a Question's: an unanswered card of another type would count "
                "as first-hand without anyone's answer")
    if value["type"] == "Question" and (att["confirmation"] != "asked" or att["speaker"] != "agent"):
        _refuse("a Question is the seat's own model asking: attribution.confirmation \"asked\", speaker \"agent\"")
    if value["type"] == "Context" and att["speaker"] != "agent":
        _refuse("a Context card is the seat's own working-context snapshot: attribution.speaker \"agent\"")


def _is_repo_relative_file(path) -> bool:
    """A normalized repo-relative POSIX path: 1..MAX_REF_FILE_CHARS chars, no leading '/', no '\\' or ':', no control
    or whitespace characters, and no empty, '.' or '..' segments (#297)."""
    if not isinstance(path, str) or not (1 <= len(path) <= MAX_REF_FILE_CHARS):
        return False
    if path.startswith("/") or "\\" in path or ":" in path:
        return False
    if any(ord(ch) <= 32 or ord(ch) == 127 or ch.isspace() for ch in path):
        return False
    parts = path.split("/")
    return all(part and part not in (".", "..") for part in parts)


def _is_pr_ref(item) -> bool:
    if type(item) is int:
        return 1 <= item <= MAX_REF_PR_NUMBER
    return isinstance(item, str) and _PR_REF_RE.fullmatch(item) is not None


def _check_refs(refs) -> None:
    """`refs`'s shape and bounds (#297): optional object with keys in {branch, prs, threads, files}."""
    if not isinstance(refs, dict):
        _refuse("refs must be an object")
    unknown = sorted(set(refs) - REFS_KEYS)
    if unknown:
        _refuse("refs has unknown fields: " + ", ".join(repr(f) for f in unknown))
    if "branch" in refs and refs["branch"] is not None:
        branch = refs["branch"]
        if (not isinstance(branch, str) or not (1 <= len(branch) <= MAX_REF_BRANCH_CHARS)
                or any(ord(ch) <= 32 or ord(ch) == 127 or ch.isspace() for ch in branch)):
            _refuse(f"refs.branch must be null or a non-blank string of at most {MAX_REF_BRANCH_CHARS} characters "
                    "with no whitespace or control characters")
    if "prs" in refs:
        prs = refs["prs"]
        if not isinstance(prs, list) or len(prs) > MAX_REF_PRS or not all(_is_pr_ref(p) for p in prs):
            _refuse(f"refs.prs must be an array of at most {MAX_REF_PRS} PR references (positive integers 1.."
                    f"{MAX_REF_PR_NUMBER} or '#<number>')")
        seen_prs: set[int] = set()
        for p in prs:
            num = p if type(p) is int else int(p[1:])
            if num in seen_prs:
                _refuse("refs.prs must not repeat a PR number")
            seen_prs.add(num)
    if "threads" in refs:
        threads = refs["threads"]
        if (not isinstance(threads, list) or len(threads) > MAX_REF_THREADS
                or not all(isinstance(t, str) and MESH_THREAD_RE.fullmatch(t) for t in threads)):
            _refuse(f"refs.threads must be an array of at most {MAX_REF_THREADS} mesh thread ids")
        if len(set(threads)) != len(threads):
            _refuse("refs.threads must not repeat a thread id")
    if "files" in refs:
        files = refs["files"]
        if (not isinstance(files, list) or len(files) > MAX_REF_FILES
                or not all(_is_repo_relative_file(f) for f in files)):
            _refuse(f"refs.files must be an array of at most {MAX_REF_FILES} repo-relative POSIX paths (at most "
                    f"{MAX_REF_FILE_CHARS} characters each, with no leading '/', '\\\\', ':' or '..')")
        if len(set(files)) != len(files):
            _refuse("refs.files must not repeat a path")


def attributed_first_hand(att) -> bool:
    """First-hand by attribution: the operator said it, and it quotes nobody."""
    return att.get("speaker") == "operator" and att.get("quoted_author") is None


def supersedes_of(value, *, own_key: str | None = None) -> list[str]:
    """The card keys a value's `supersedes` names, validated for shape. [] when ABSENT. A present
    null is refused: it would read as "absent" to the carry rule and as "cleared" to the store, and
    clearing takes an explicit []."""
    if not isinstance(value, dict) or "supersedes" not in value:
        return []
    raw = value["supersedes"]
    if not isinstance(raw, list) or len(raw) > MAX_SUPERSEDES:
        _refuse(f"supersedes must be an array of at most {MAX_SUPERSEDES} card keys", "E_CARD_SUPERSEDES")
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str) or key_parts(item) is None:
            _refuse("supersedes holds card keys (card/<seat>/<32 hex>)", "E_CARD_SUPERSEDES")
        if item == own_key:
            _refuse("a card cannot supersede itself", "E_CARD_SUPERSEDES")
        if item in out:
            _refuse("supersedes must not repeat a key", "E_CARD_SUPERSEDES")
        out.append(item)
    return out


def _is_card(value) -> bool:
    """A stored value that is still a card: a redacted one is a tombstone with no `text`."""
    return isinstance(value, dict) and isinstance(value.get("text"), str)


def _first_hand(value) -> bool:
    """Whether a card counts a recorded decision for the supersession rules. ONE authority per card: `attribution` when the
    card carries it, else the legacy `source.first_hand` (kxxx, msg/<key>). Without this, a
    re-versioned Question would say "agent" in attribution and still count a recorded decision here."""
    if not isinstance(value, dict):
        return False
    att = value.get("attribution")
    if isinstance(att, dict):
        return attributed_first_hand(att)
    source = value.get("source")
    return isinstance(source, dict) and source.get("first_hand") is True


def enforce_card(conn, ns, key, value, caller_peer_id, *, caller_label: str) -> None:
    """The rules that need the database. Called by memory.write after the plane ban, with the ONE
    label the server stamps for the caller (handlers.stamped_label: the roster label, else
    peer-<id8>). One label, not every label the caller answers to: a peer with three labels would
    otherwise hold one record under three keys, and superseding one would leave two active.

    1. The card's seat is the caller's label: a seat writes as itself.
    2. A later version is written only by the peer that wrote version 1, never over a redacted
       head (the loader's next run would write the redacted text back), and never with another
       `time`, `type`, `seat`, source record, `source.first_hand` or `source.kind`: those decide
       every order and every rule below.
    3. A later version that drops `supersedes` while the head names targets is refused; clearing
       takes an explicit []. The loader does not know the list, so an omission is a lost one.
    4. Each target this version ADDS is a head card strictly earlier than this one. A Claim
       supersedes only a Claim, a relayed card never supersedes a recorded decision one, and another
       seat's card is superseded only by a recorded decision Ruling.
    """
    seat = value["seat"]
    if seat != caller_label:
        _refuse(f"seat {seat!r} is not the caller's label {caller_label!r}: a seat writes its own cards, "
                "under the one label the server stamps for it", "E_CARD_SEAT_MISMATCH")
    with conn.cursor() as cur:
        # Serialize all card writes for (namespace, seat) BEFORE reading root or head_row.
        # Under READ COMMITTED, a bare `SELECT ... WHERE key = %s AND is_head FOR UPDATE` that waits on an
        # in-flight re-version re-evaluates `is_head` on the old row after the first writer commits, sees
        # `is_head = false`, and returns `head_row = None` (skipping frozen-field and supersedes checks),
        # and taking row locks before the advisory lock on re-versions vs after on new Context cards would
        # invert lock order (pxx review on #302).
        cur.execute("SELECT pg_advisory_xact_lock(hashtext('cards:' || %s), hashtext(%s))",
                    (ns, seat))
        cur.execute("SELECT created_by_peer_id, version, value FROM shared_memory "
                    "WHERE namespace = %s AND key = %s ORDER BY version ASC LIMIT 1", (ns, key))
        root = cur.fetchone()
        # FOR UPDATE: memory.redact rewrites this row in place. Without the lock a redaction could
        # commit between this read and the insert of the next version, and that version would carry
        # the redacted text back. With it, one waits for the other: a redaction that came first is
        # seen here (E_CARD_REDACTED), and one that comes second redacts the new version too.
        cur.execute("SELECT value FROM shared_memory WHERE namespace = %s AND key = %s AND is_head "
                    "FOR UPDATE", (ns, key))
        head_row = cur.fetchone()
    if root is not None and str(root[0]) != str(caller_peer_id):
        _refuse(f"{ns}/{key} was created (version {root[1]}) by another peer; only that peer writes a "
                "later version of its card", "E_CARD_NOT_AUTHOR")

    # ATTRIBUTION (loader 1.19). first_hand must agree with it only on a card BORN with it: every earlier card says
    # first_hand true, that field is frozen, and a Question re-versioned with speaker "agent" would otherwise be refused
    # for good (txxxx, msg/<key>). A redacted root keeps no fields and so never counts as born.
    root_value = root[2] if root is not None else value
    born_attributed = isinstance(root_value, dict) and isinstance(root_value.get("attribution"), dict)
    att = value.get("attribution") if isinstance(value.get("attribution"), dict) else None
    if born_attributed and att is not None and value["source"]["first_hand"] is not attributed_first_hand(att):
        _refuse("source.first_hand must equal attribution (speaker \"operator\" and quoted_author null) on a card "
                "first written with attribution")

    targets = supersedes_of(value, own_key=str(key))
    already: list[str] = []
    if head_row is not None:
        head = head_row[0]
        if not _is_card(head):
            _refuse(f"{ns}/{key} was redacted; a redacted card is not written again", "E_CARD_REDACTED")
        frozen = [("time", head.get("time"), value["time"]), ("type", head.get("type"), value["type"]),
                  ("seat", head.get("seat"), value["seat"]),
                  # WHO DECIDED is read as a fact once written: a later version that turned the operator's
                  # decision into the seat's, or back, would rewrite it without a new card (#253).
                  ("owner", head.get("owner"), value["owner"])]
        old_source = head.get("source") if isinstance(head.get("source"), dict) else {}
        # `first_hand` with them: it decides what this card may supersede, and those edges are
        # checked once, when they are added. a recorded decision Ruling that turned relayed in version 2
        # would keep an edge no relayed card may hold.
        for name in ("record_uuid", "block_index", "first_hand", "kind"):
            frozen.append((f"source.{name}", old_source.get(name), value["source"][name]))
        # Once a head carries attribution it is the card's authority (_first_hand), so it is carried, and what it says
        # a recorded decision is frozen as source.first_hand is: the edges it allowed were checked once, when added.
        head_att = head.get("attribution") if isinstance(head.get("attribution"), dict) else None
        if head_att is not None:
            if att is None:
                _refuse("the head of this card carries attribution, so every later version carries it",
                        "E_CARD_FROZEN")
            frozen.append(("first-hand by attribution", attributed_first_hand(head_att), attributed_first_hand(att)))
        changed = [name for name, old, new in frozen if old != new]
        if changed:
            _refuse(f"a later version of a card keeps its {', '.join(changed)}: write a new card that "
                    "supersedes this one instead", "E_CARD_FROZEN")
        already = [t for t in (head.get("supersedes") or []) if isinstance(t, str)]
        if already and "supersedes" not in value:
            _refuse(f"the head of {key} supersedes {len(already)} card(s) and this version does not say: "
                    "carry the list, or clear it with an explicit []", "E_CARD_SUPERSEDES")

    if value["type"] == "Context":
        if any(t not in targets for t in already):
            _refuse(f"a later version of a Context card keeps its supersedes edge ({already!r}); "
                    "clearing it would leave more than one active Context head", "E_CARD_SUPERSEDES")
        # Issue #297: one active Context head per seat, and an idle hour with unchanged content writes nothing.
        with conn.cursor() as cur:
            # The seat's heads are read ONCE: their supersedes edges become (target, latest superseding time), and a
            # Context head is active unless a later head supersedes it. The earlier form tested every Context head
            # against every head of the seat (a correlated NOT EXISTS), quadratic in the seat's cards: at 2,410 heads
            # (memory-bxxxxx, 1 Nov) it passed the statement timeout on every Context write, which the route answers
            # as E_INTERNAL (46 of the 50 QueryCanceled in prod's log that day). Same rows, checked read-only on prod
            # against the old form (antigravity 187 superseded, nxxxxx 44). It locks the active Context heads, as
            # before. On the aggregate ledger (tests/test_query_cost_bounds.py).
            cur.execute("WITH namers AS ("
                        "  SELECT value->'supersedes' AS sup, value->>'time' AS t FROM shared_memory "
                        "  WHERE namespace = %s AND is_head AND key <> %s AND key LIKE %s AND value->>'seat' = %s "
                        "    AND (jsonb_typeof(value->'text') = 'string' OR value->'redacted' = 'true'::jsonb)"
                        "), superseded AS ("
                        "  SELECT e.k, max(n.t) AS t FROM namers n "
                        # a non-array supersedes names nothing, and never reaches the array function as a scalar
                        "  CROSS JOIN LATERAL jsonb_array_elements_text(CASE WHEN jsonb_typeof(n.sup) = 'array' "
                        "    THEN n.sup ELSE '[]'::jsonb END) AS e(k) "
                        "  GROUP BY e.k"
                        ") "
                        "SELECT sm.key, sm.value FROM shared_memory sm WHERE sm.namespace = %s AND sm.is_head "
                        "AND sm.key <> %s AND sm.key LIKE %s AND sm.value->>'seat' = %s "
                        "AND sm.value->>'type' = 'Context' "
                        "AND jsonb_typeof(sm.value->'text') = 'string' "
                        "AND NOT EXISTS (SELECT 1 FROM superseded x WHERE x.k = sm.key AND x.t > sm.value->>'time') "
                        "FOR UPDATE OF sm",
                        (ns, key, f"card/{seat}/%", seat, ns, key, f"card/{seat}/%", seat))
            active_heads = cur.fetchall()
            self_superseded = False
            if head_row is not None:
                cur.execute("SELECT 1 FROM shared_memory s2 "
                            "WHERE s2.namespace = %s AND s2.is_head AND s2.key <> %s AND s2.key LIKE %s "
                            "  AND s2.value->>'seat' = %s "
                            "  AND (jsonb_typeof(s2.value->'text') = 'string' OR s2.value->'redacted' = 'true'::jsonb) "
                            "  AND jsonb_typeof(s2.value->'supersedes') = 'array' "
                            "  AND s2.value->'supersedes' ? %s "
                            "  AND s2.value->>'time' > %s LIMIT 1",
                            (ns, key, f"card/{seat}/%", seat, key, value["time"]))
                self_superseded = cur.fetchone() is not None
        if not self_superseded:
            for active_key, active_val in active_heads:
                if active_key not in targets:
                    _refuse(f"seat {seat!r} already has an active Context head ({active_key!r}); a Context card "
                            "must supersede it so the seat holds at most one active Context head",
                            "E_CARD_SUPERSEDES")
                if (head_row is None
                        and isinstance(active_val, dict)
                        and active_val.get("text_sha256") == value["text_sha256"]
                        and active_val.get("refs") == value.get("refs")):
                    _refuse(f"Context card for seat {seat!r} is unchanged since {active_key!r} (same text_sha256 "
                            "and refs); nothing written", "E_CARD_UNCHANGED")

    added = [t for t in targets if t not in already]
    if not added:
        return
    when = parse_time(value["time"])
    with conn.cursor() as cur:
        cur.execute("SELECT key, value FROM shared_memory WHERE namespace = %s AND is_head "
                    "AND key = ANY(%s)", (ns, added))
        found = {row[0]: row[1] for row in cur.fetchall()}
    for target in added:
        old = found.get(target)
        if not _is_card(old) or not isinstance(old.get("time"), str):
            _refuse(f"supersedes names {target!r}, which is not a readable head card of {ns}",
                    "E_CARD_SUPERSEDES")
        if not TIME_RE.fullmatch(old["time"]) or not parse_time(old["time"]) < when:
            _refuse(f"supersedes names {target!r}, which is not strictly earlier than this card",
                    "E_CARD_SUPERSEDES")
        if value["type"] == "Claim" and old.get("type") != "Claim":
            _refuse(f"a Claim supersedes only a Claim; {target!r} is a {old.get('type')!r}",
                    "E_CARD_SUPERSEDES")
        if value["type"] == "Context" and (old.get("type") != "Context" or old.get("seat") != seat):
            _refuse(f"a Context card supersedes only its own seat's Context card; {target!r} is "
                    f"{old.get('seat')!r}/{old.get('type')!r}", "E_CARD_SUPERSEDES")
        if value["type"] != "Context" and old.get("type") == "Context":
            _refuse(f"{target!r} is a Context card: only a Context card of the same seat supersedes it",
                    "E_CARD_SUPERSEDES")
        if _first_hand(old) and not _first_hand(value):
            _refuse(f"a relayed card does not supersede a first-hand one ({target!r})", "E_CARD_SUPERSEDES")
        if old.get("seat") != seat and not (value["type"] == "Ruling" and _first_hand(value)):
            _refuse(f"{target!r} is another seat's card: only a first-hand Ruling supersedes it",
                    "E_CARD_SUPERSEDES")


__all__ = ["DEFAULT_NAMESPACE", "FORBIDDEN_PROSE_FIELDS", "KEY_RE", "MAX_REF_BRANCH_CHARS", "MAX_REF_FILES",
           "MAX_REF_FILE_CHARS", "MAX_REF_PRS", "MAX_REF_PR_NUMBER", "MAX_REF_THREADS", "MAX_SUPERSEDES",
           "MAX_TEXT_CHARS", "MAX_VALUE_BYTES", "OPTIONAL_FIELDS", "REFS_KEYS", "REQUIRED_FIELDS", "SCHEMA",
           "TIME_RE", "TYPES", "enforce_card", "is_card_namespace", "key_parts", "namespace", "parse_time",
           "supersedes_of", "validate_card"]
