"""What memory.write refuses in the transcript-context namespace (docs/design/context-v1.md, sections 3 to 7).

A row there is one dialogue ITEM (`ctx/...`) or one batch MANIFEST (`ctxm/...`), written once. Two halves, as the
cards' rules are split (apps/cards/validation.py):

  `validate_context`   no database: the key, every field, its type and limit, the serialized budget, the digest.
                       NO SCREENS here: memory.write runs this before the permission check, and a screen code
                       returned to a caller without a grant would let anyone probe the client-name denylist
                       (memory-bxxxxx, msg/<key>, item 1).
  `enforce_context`    beside the row, after check_permission and the product-plane ban: the seat is the caller's,
                       the key is new (strictly version 1, no exception), both screens on the WHOLE text, and for
                       a manifest its items, its counts and its place after the previous batch's manifest.

Redaction of this namespace is NOT here and not yet built (design section 8): memory.redact refuses the namespace
until it is, because today's in-place tombstone would leave the item's digest in its manifest.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

from apps.core import env
from apps.memoryskills.errors import HandlerError

from . import screens

DEFAULT_NAMESPACE = "shared/transcript-context"
SCHEMA = "context.v1"
#: The serialized value (the cards' serializer), and the mesh's own one-write ceiling (MESH_MAX_BODY_BYTES).
MAX_VALUE_BYTES = 65_536
MAX_ITEMS_PER_BATCH = 256
MAX_STEP_CHARS = 1_000
#: F3 (ingest plan D1, step 3 item 8; the parser side is #429). A tool call and its result are one tool_exchange item,
#: or ordered parts of one when the exchange is too large for an item; `thinking` is plain reasoning text.
MAX_EXCHANGE_PARTS = 128
MAX_TOOL_NAME_CHARS = 256
#: Clock skew, no more (the cards' rule): a later `time` is refused.
MAX_AHEAD = timedelta(minutes=5)

_SEAT = r"([a-z0-9][a-z0-9_.-]{0,63})"
_SRC = r"([0-9a-f]{32})"
ITEM_KEY_RE = re.compile(r"^ctx/" + _SEAT + "/" + _SRC + r"/(\d{4})/(\d{6})/(\d{10})$")
MANIFEST_KEY_RE = re.compile(r"^ctxm/" + _SEAT + "/" + _SRC + r"/(\d{4})/(\d{6})$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
#: native_ref and agent_id are OPAQUE, as `src` is: the first 32 hex of the sha256 of the native id, never the id
#: itself. A raw id cannot carry text past the screens (a reviewer, #346), and it is not screened either: the loader's
#: secret detectors match real id shapes (toolu_01..., msg_01..., call_...), so screening raw ids refused real items and,
#: through their omitted replacements, blocked a whole generation (memory-bxxxxx, P1-2). A reader maps an id to
#: its record locally by hashing it the same way.
OPAQUE_RE = re.compile(r"^[0-9a-f]{32}$")
#: The loader's version: digits and dots (1.23, 1.23.4).
VERSION_RE = re.compile(r"^[0-9]{1,4}(?:\.[0-9]{1,4}){0,3}$")   # [0-9], not \d: \d also takes non-ASCII digits
#: The free text an item stores, each screened whole in enforce_context. Every other string is a fixed shape above.
SCREENED_FIELDS = ("text", "step")
TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

TEXT_ITEMS = {"user_text": "user", "assistant_text": "assistant", "thinking": "assistant"}
ITEM_KINDS = frozenset({"user_text", "assistant_text", "task", "artifact", "omitted", "tool_exchange", "thinking"})
DIALECTS = frozenset({"claude", "codex", "kimi", "antigravity"})
OMITTED_REASONS = frozenset({"secret", "client_name", "too_large"})
TASK_EVENTS = frozenset({"added", "completed", "removed", "ambiguous"})
COUNT_KEYS = ("pending", "in_progress", "completed")
MANIFEST_COUNTS = ("written", "omitted_secret", "omitted_client_name", "omitted_too_large")
#: The F3 classes a manifest counts, present only in a batch that holds an F3 item (the parser adds them then, so an F1
#: manifest is unchanged). Each is a subset of `written`, so they are not part of the sum that must equal the items.
F3_COUNTS = ("tool_exchange", "thinking", "call_missing", "result_missing", "parts_extra")
CALL_STATUSES = frozenset({"ok", "missing"})
RESULT_STATUSES = frozenset({"ok", "error", "missing"})
EXCHANGE_FIELDS = frozenset({"tool_ref", "tool", "call_status", "result_status", "exchange_sha256", "part", "parts"})

_COMMON = {"schema", "kind", "seat", "src", "gen", "batch", "dialect", "adapter_version", "time"}
ITEM_FIELDS = _COMMON | {"ordinal", "item", "role", "native_ref", "text", "text_sha256", "omitted_reason",
                         "omitted_bytes", "agent_id", "step", "event", "counts"} | EXCHANGE_FIELDS
#: `redacted` is a KNOWN manifest field (the server's redaction writes it, design 8), so the unknown-field rule does not
#: refuse it: validate_context refuses it by name, on every memory.write (memory-bxxxxx, item 4).
MANIFEST_FIELDS = {"schema", "kind", "seat", "src", "gen", "batch", "first", "last", "items", "counts", "redacted"}


def namespace() -> str:
    """The context namespace: MESHY_CONTEXT_NAMESPACE, else shared/transcript-context."""
    return str(env.setting("MESHY_CONTEXT_NAMESPACE", "MESHY_CONTEXT_NAMESPACE", default=DEFAULT_NAMESPACE))


def is_context_namespace(ns) -> bool:
    return ns == namespace()


def screens_apply(ns) -> bool:
    """Whether items in `ns` pass the admission screens (apps/context/screens.py). NEITHER applies to
    shared/transcript-context: the operator ruled out the client-name check (23 Oct 2026, kxxx-8136bc; v1.1 design
    step 2) and then the secret check (24 Oct 2026, on record in kxxx's session: "Store everything, secrets
    included"), so a seat's whole dialogue is stored as written. Bound to that NAME, not to whichever namespace is
    configured, so a scratch or test namespace (rows 6-7 run on shared/scratch-transcript-context) keeps the screens
    it was pinned with. Nothing is loaded for the production namespace: no detectors, no denylist."""
    return ns != DEFAULT_NAMESPACE


def _refuse(message: str, code: str = "E_CONTEXT_SCHEMA"):
    raise HandlerError(code, message)


def _int(value, name: str, lo: int = 0) -> int:
    # bool is an int in Python: True would pass as 1
    if type(value) is not int or value < lo:
        _refuse(f"{name} must be an integer >= {lo}")
    return value


def _str(value, name: str, limit: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or (not empty and not value):
        _refuse(f"{name} must be a {'' if empty else 'non-empty '}string of at most {limit} characters")
    return value


def _shaped(value, name: str, rx, shape: str) -> str:
    if not isinstance(value, str) or not rx.fullmatch(value):
        _refuse(f"{name} must be {shape}")
    return value


def _parse_time(raw, now: datetime) -> None:
    if not isinstance(raw, str) or not TIME_RE.fullmatch(raw):
        _refuse("time must be a UTC instant written YYYY-MM-DDTHH:MM:SS.mmmZ", "E_CONTEXT_TIME")
    try:
        t = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError:
        _refuse(f"time {raw!r} is not a real instant", "E_CONTEXT_TIME")
    if t > now + MAX_AHEAD:
        _refuse("time is more than 5 minutes ahead of the server", "E_CONTEXT_TIME")


def _serialized(value) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except UnicodeEncodeError:
        _refuse("the value holds a lone UTF-16 surrogate; clean it (U+FFFD) before hashing and writing",
                "E_CONTEXT_TEXT_ENCODING")


def key_kind(key) -> tuple[str, tuple] | None:
    """('item', (seat, src, gen, batch, ordinal)) or ('manifest', (seat, src, gen, batch)), or None."""
    m = ITEM_KEY_RE.fullmatch(str(key))
    if m:
        return "item", (m.group(1), m.group(2), int(m.group(3)), int(m.group(4)), int(m.group(5)))
    m = MANIFEST_KEY_RE.fullmatch(str(key))
    if m:
        return "manifest", (m.group(1), m.group(2), int(m.group(3)), int(m.group(4)))
    return None


def _check_key_fields(value, parts, names) -> None:
    for name, expected in zip(names, parts):
        got = value.get(name)
        if type(got) is not type(expected) or got != expected:
            _refuse(f"{name} {got!r} does not match the key's {expected!r}")
    if parts[2] < 1 or parts[3] < 1:
        _refuse("gen and batch start at 1")


def _check_item(value) -> None:
    item = value.get("item")
    if item not in ITEM_KINDS:
        _refuse("item must be one of " + ", ".join(sorted(ITEM_KINDS)))
    fields = set(value)
    if item == "omitted":
        extra = fields & ({"role", "text", "text_sha256", "agent_id", "step", "event", "counts"} | EXCHANGE_FIELDS)
        if extra:
            _refuse(f"an omitted item carries nothing derived from what it omits: drop {', '.join(sorted(extra))}")
        reason = value.get("omitted_reason")
        if reason not in OMITTED_REASONS:
            _refuse("omitted_reason must be one of " + ", ".join(sorted(OMITTED_REASONS)))
        if reason == "too_large":
            _int(value.get("omitted_bytes"), "omitted_bytes", 1)
        elif "omitted_bytes" in fields:
            _refuse("omitted_bytes is only for reason too_large: a screened item keeps no size either")
        return
    if fields & {"omitted_reason", "omitted_bytes"}:
        _refuse("omitted_reason and omitted_bytes belong to an omitted item only")
    text = value.get("text")
    if not isinstance(text, str) or not text:
        _refuse(f"a {item} item carries its whole text")
    digest = value.get("text_sha256")
    if not isinstance(digest, str) or not _HEX64.fullmatch(digest):
        _refuse("text_sha256 must be 64 lower-case hex")
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != digest:
        _refuse("text_sha256 is not the sha256 of text", "E_CONTEXT_DIGEST")
    if item in TEXT_ITEMS:
        if value.get("role") != TEXT_ITEMS[item]:
            _refuse(f"a {item} item has role {TEXT_ITEMS[item]!r}")
    elif "role" in fields:
        _refuse(f"a {item} item carries no role")
    if "agent_id" in fields:
        if item != "artifact":
            _refuse("agent_id is only on artifact items (a subagent's prose is not dialogue)")
        _shaped(value["agent_id"], "agent_id", OPAQUE_RE, "32 lower-case hex (the sha256 prefix of the subagent id)")
    exchange = fields & EXCHANGE_FIELDS
    if item == "tool_exchange":
        _check_exchange(value, text, digest)
    elif exchange:
        _refuse(f"{', '.join(sorted(exchange))} belong to a tool_exchange item only")
    task = fields & {"step", "event", "counts"}
    if item == "task":
        _str(value.get("step"), "step", MAX_STEP_CHARS)
        event = value.get("event")
        if event not in TASK_EVENTS:
            _refuse("event must be one of " + ", ".join(sorted(TASK_EVENTS)))
        counts = value.get("counts")
        if event == "ambiguous":
            if not isinstance(counts, dict) or set(counts) != set(COUNT_KEYS):
                _refuse("an ambiguous task carries counts {pending, in_progress, completed}")
            for k in COUNT_KEYS:
                _int(counts[k], f"counts.{k}")
        elif "counts" in fields:
            _refuse("counts belong to an ambiguous task only")
    elif task:
        _refuse(f"{', '.join(sorted(task))} belong to a task item only")


def _no_duplicates(pairs):
    keys = [k for k, _v in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate member")
    return dict(pairs)


def _decode_exchange(text):
    """The exchange JSON, or None. A duplicate member is refused, not resolved: json.loads keeps the last one, so
    {"call": <secret>, "call": "x"} would decode to "x" and the secret would pass the screen that reads the decoded
    fields (nxxxxx on #433)."""
    try:
        return json.loads(text, object_pairs_hook=_no_duplicates)
    except ValueError:
        return None


def _exchange_shaped(body) -> bool:
    """{tool, call, result}, call and result each a string or null: the parser only ever writes those (_tool_text).
    An object or a list would hide from the screens, which read the decoded STRING fields (txxxx, GitHub Codex on
    #433 validation.py:279: {"call": {"note": "\\u005a..."}} decoded to an object no screen input held)."""
    return (isinstance(body, dict) and set(body) == {"tool", "call", "result"}
            and all(body[k] is None or isinstance(body[k], str) for k in ("call", "result")))


def _check_exchange(value, text, digest) -> None:
    """A tool_exchange: the call and its result, paired by tool-use id, or part i of n of one (the parser's _exchange).
    The text of a whole exchange, or the parts joined in order, is the JSON {tool, call, result}."""
    _shaped(value.get("tool_ref"), "tool_ref", OPAQUE_RE, "32 lower-case hex (the sha256 prefix of the tool-use id)")
    tool = value.get("tool")
    if tool is not None:
        _str(tool, "tool", MAX_TOOL_NAME_CHARS)
    if value.get("call_status") not in CALL_STATUSES:
        _refuse("call_status must be one of " + ", ".join(sorted(CALL_STATUSES)))
    if value.get("result_status") not in RESULT_STATUSES:
        _refuse("result_status must be one of " + ", ".join(sorted(RESULT_STATUSES)))
    if value["call_status"] == "missing" and value["result_status"] == "missing":
        _refuse("a tool_exchange has its call, its result or both")
    whole = value.get("exchange_sha256")
    if not isinstance(whole, str) or not _HEX64.fullmatch(whole):
        _refuse("exchange_sha256 must be 64 lower-case hex")
    parts = _int(value.get("parts"), "parts", 1)
    part = _int(value.get("part"), "part", 1)
    if parts > MAX_EXCHANGE_PARTS or part > parts:
        _refuse(f"part i of n, 1 <= i <= n <= {MAX_EXCHANGE_PARTS}")
    if parts == 1:
        if whole != digest:
            _refuse("a one-part exchange's exchange_sha256 is its text_sha256", "E_CONTEXT_DIGEST")
        body = _decode_exchange(text)
        if not _exchange_shaped(body):
            _refuse("a one-part exchange's text is the JSON {tool, call, result}, call and result strings or null")
        if body["tool"] != tool:
            _refuse("tool must be the tool its text names")


def _exchange_screen_text(value) -> list:
    """What the screens read for a tool_exchange. Its text is JSON, and json.dumps escapes every quote inside the call
    and the result, so a quoted secret ({"password":"..."}, KEY="...") would not match a detector in the text as stored
    (GitHub Codex on #429, context_parser.py:888). A one-part exchange is screened on its decoded fields as well; a
    part, a slice of the JSON that may not decode, on its text with the JSON string escapes undone as well."""
    text = value.get("text") or ""
    # The top-level tool name is stored outside the text: screened on every exchange, one part or many (Codex on #433).
    tool = [value["tool"]] if isinstance(value.get("tool"), str) else []
    if value.get("parts") == 1:
        body = _decode_exchange(text)
        if isinstance(body, dict):
            # _unescape(text) too: whatever the decode yields, every escape in the stored text is undone once.
            return tool + [text, _unescape(text)] + _exchange_fields(body)
    return tool + [text, _unescape(text)]


def _exchange_fields(body) -> list:
    """A decoded exchange's tool, call and result, plus every string inside a call or result that is itself JSON. A call
    that is JSON holds its strings escaped one layer down, so a secret in it ({"note": "{\\"password\\":\\"...\\"}"})
    matched no detector on the decoded field alone (GitHub Codex on #433, validation.py:289). The parser screens the
    same strings (context_parser._json_strings); this keeps the server's screen as strong as the client's."""
    fields = [body[k] for k in ("tool", "call", "result") if isinstance(body.get(k), str)]
    return fields + [s for k in ("call", "result") for s in _json_strings(body.get(k))]


def _json_strings(text) -> list:
    """Every string inside `text` when `text` is a JSON array or object, keys included; [] for anything else. The same
    walk as context_parser._json_strings, one parse deep: a server screen stricter than the client's would refuse a batch
    the client had already frozen, and the upload would stall on it."""
    if not isinstance(text, str) or text.lstrip()[:1] not in ("[", "{"):
        return []
    try:
        stack, out = [json.loads(text)], []
    except ValueError:
        return []
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            out.append(_no_lone_surrogates(item))
        elif isinstance(item, dict):
            out.extend(_no_lone_surrogates(k) for k in item if isinstance(k, str))
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return out


def _no_lone_surrogates(s: str) -> str:
    """Each unpaired surrogate (from a JSON \\uD800 escape) as U+FFFD, as transcript_cards._no_lone_surrogates does, so
    a screen never meets a string that cannot encode."""
    return "".join("�" if 0xD800 <= ord(c) <= 0xDFFF else c for c in s)


_JSON_ESCAPE = re.compile(r'\\(["\\/bfnrt]|u[0-9a-fA-F]{4})')
_UNESCAPE = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


def _unescape(text: str) -> str:
    """A JSON string slice with its escapes undone, \\uXXXX included (json.dumps writes U+0000..U+001F that way even
    with ensure_ascii=False). Tolerant: a slice may end inside an escape, which stays as it is."""
    return _JSON_ESCAPE.sub(lambda m: chr(int(m.group(1)[1:], 16)) if m.group(1)[0] == "u"
                            else _UNESCAPE[m.group(1)], text)


def _check_manifest(value, batch_parts) -> None:
    first = _int(value.get("first"), "first")
    last = _int(value.get("last"), "last")
    if last < first or last - first + 1 > MAX_ITEMS_PER_BATCH:
        _refuse(f"a batch covers first..last, contiguous, at most {MAX_ITEMS_PER_BATCH} items")
    if batch_parts[3] == 1 and first != 0:
        _refuse("batch 000001 starts at ordinal 0", "E_CONTEXT_MANIFEST_ORDER")
    items = value.get("items")
    if not isinstance(items, list) or len(items) != last - first + 1:
        _refuse("items has one entry per ordinal first..last")
    for i, entry in enumerate(items):
        if (not isinstance(entry, list) or len(entry) != 2 or type(entry[0]) is not int or entry[0] != first + i
                or not (entry[1] is None or (isinstance(entry[1], str) and _HEX64.fullmatch(entry[1])))):
            _refuse(f"items[{i}] must be [{first + i}, <text_sha256 or null>]")
    counts = value.get("counts")
    if not isinstance(counts, dict) or set(counts) not in (set(MANIFEST_COUNTS), set(MANIFEST_COUNTS + F3_COUNTS)):
        _refuse("counts must be {" + ", ".join(MANIFEST_COUNTS) + "}, plus {" + ", ".join(F3_COUNTS)
                + "} in a batch with F3 items")
    for k in counts:
        _int(counts[k], f"counts.{k}")
    if sum(counts[k] for k in MANIFEST_COUNTS) != len(items):
        _refuse("counts must add up to the number of items")
    if "tool_exchange" in counts and (counts["tool_exchange"] + counts["thinking"] > counts["written"]
                                      or max(counts["call_missing"], counts["result_missing"], counts["parts_extra"])
                                      > counts["tool_exchange"]):
        _refuse("the F3 counts are subsets of written, and the missing halves and extra parts of tool_exchange")


def validate_context(ns, key, value, *, now: datetime | None = None) -> str | None:
    """'item' or 'manifest' when (ns, key) is a context row and `value` passed every rule that needs no database.
    None when `ns` is not the context namespace (nothing is checked). Raises HandlerError otherwise."""
    if not is_context_namespace(ns):
        return None
    kind = key_kind(key)
    if kind is None:
        _refuse(f"{ns} holds context items (ctx/<seat>/<src>/<gen>/<batch>/<ordinal>) and manifests "
                f"(ctxm/<seat>/<src>/<gen>/<batch>) only, not {str(key)[:80]!r}", "E_CONTEXT_KEY")
    kind_name, parts = kind
    if not isinstance(value, dict):
        _refuse("a context value must be an object")
    if len(_serialized(value)) > MAX_VALUE_BYTES:
        _refuse(f"the serialized value is at most {MAX_VALUE_BYTES} bytes; write a larger item as omitted, "
                "reason too_large", "E_CONTEXT_TOO_LARGE")
    if value.get("schema") != SCHEMA or value.get("kind") != kind_name:
        _refuse(f"schema must be {SCHEMA!r} and kind {kind_name!r} for this key")
    if "redacted" in value:
        _refuse("redacted is set only by memory.redact, never through memory.write")
    allowed = ITEM_FIELDS if kind_name == "item" else MANIFEST_FIELDS
    unknown = sorted(set(value) - allowed)
    if unknown:
        _refuse(f"unknown fields: {', '.join(unknown)} (a new field is a reviewed server change)")
    if kind_name == "item":
        _check_key_fields(value, parts, ("seat", "src", "gen", "batch", "ordinal"))
        if value.get("dialect") not in DIALECTS:
            _refuse("dialect must be one of " + ", ".join(sorted(DIALECTS)))
        _shaped(value.get("adapter_version"), "adapter_version", VERSION_RE, "a version such as 1.23")
        _parse_time(value.get("time"), now or datetime.now(timezone.utc))
        if "native_ref" in value:
            _shaped(value["native_ref"], "native_ref", OPAQUE_RE, "32 lower-case hex (the sha256 prefix of the record id)")
        _check_item(value)
    else:
        _check_key_fields(value, parts, ("seat", "src", "gen", "batch"))
        _check_manifest(value, parts)
    return kind_name


def _item_key(seat, src, gen, batch, ordinal) -> str:
    return f"ctx/{seat}/{src}/{gen:04d}/{batch:06d}/{ordinal:010d}"


def _manifest_key(seat, src, gen, batch) -> str:
    return f"ctxm/{seat}/{src}/{gen:04d}/{batch:06d}"


def enforce_context(conn, ns, key, value, kind: str, caller_peer_id, *, caller_label: str) -> None:
    """The rules that need the database. Called by memory.write after check_permission and the plane ban, with the
    ONE label the server stamps for the caller (handlers.stamped_label)."""
    seat = value["seat"]
    if seat != caller_label:
        _refuse(f"seat {seat!r} is not the caller's label {caller_label!r}: a seat writes its own context",
                "E_CONTEXT_SEAT_MISMATCH")
    if kind == "item" and screens_apply(ns):
        # The screens first, holding no lock and reading nothing: they can take time on a long item, and nothing they
        # decide depends on the database (memory-bxxxxx). Every other string is a fixed shape (validate_context).
        texts = [value[f] for f in SCREENED_FIELDS if isinstance(value.get(f), str)]
        if value.get("item") == "tool_exchange":
            texts = _exchange_screen_text(value)
        screens.screen(*texts)
    with conn.cursor() as cur:
        # Write-once has TWO guards, each tested with the other turned off (memory-bxxxxx). First: one writer at a
        # time per source generation, until the transaction ends, so a racer waits here and then sees the first row. The
        # lock is taken after the screens, so a slow screen never holds it.
        serialize(cur, ns, seat, value["src"], value["gen"])
        cur.execute("SELECT 1 FROM shared_memory WHERE namespace = %s AND key = %s LIMIT 1", (ns, key))
        refuse_existing(key, cur.fetchone())
    if kind == "manifest":
        _enforce_manifest(conn, ns, value)


def serialize(cur, ns, seat, src, gen) -> None:
    """The per-source-generation lock (transaction-scoped)."""
    cur.execute("SELECT pg_advisory_xact_lock(hashtext('context:' || %s), hashtext(%s))", (ns, f"{seat}/{src}/{gen}"))


def refuse_existing(key, row) -> None:
    """Refuse when any version of `key` exists. Also called at memory.write's own head read, the second guard: without
    it, a racer committing between the check above and that read would be written as version 2 (memory-bxxxxx)."""
    if row is not None:
        _refuse(f"{key} exists: every context key is written once. Read it back and compare the bytes",
                "E_CONTEXT_WRITE_ONCE")


def refuse_existing_head(key, head) -> None:
    """memory.write's own call of the second guard (its own name, so a test can turn this guard off alone)."""
    refuse_existing(key, head)


def _enforce_manifest(conn, ns, value) -> None:
    seat, src, gen, batch = value["seat"], value["src"], value["gen"], value["batch"]
    first, last = value["first"], value["last"]
    with conn.cursor() as cur:
        if batch > 1:
            # FOR UPDATE: the predecessor's manifest is the lock two adjacent manifests serialize on (design 6).
            cur.execute("SELECT value FROM shared_memory WHERE namespace = %s AND key = %s AND is_head FOR UPDATE",
                        (ns, _manifest_key(seat, src, gen, batch - 1)))
            prev = cur.fetchone()
            if prev is None:
                # Retryable: the uploader commits batch n-1's manifest, then sends this one again unchanged.
                _refuse(f"batch {batch - 1:06d}'s manifest is not committed: manifests commit in batch order",
                        "E_CONTEXT_MANIFEST_PREDECESSOR")
            prev_last = prev[0].get("last") if isinstance(prev[0], dict) else None
            if type(prev_last) is not int or first != prev_last + 1:
                _refuse(f"first must be {prev_last!r} + 1: ordinals are contiguous across batches",
                        "E_CONTEXT_MANIFEST_ORDER")
        keys = [_item_key(seat, src, gen, batch, o) for o in range(first, last + 1)]
        cur.execute("SELECT key, version, is_head, value FROM shared_memory WHERE namespace = %s AND key = ANY(%s)",
                    (ns, keys))
        rows = {}
        for k, version, is_head, row_value in cur.fetchall():
            if version != 1 or not is_head:
                _refuse(f"{k} is not a single version-1 item", "E_CONTEXT_MANIFEST")
            rows[k] = row_value
    tally = dict.fromkeys(MANIFEST_COUNTS + (F3_COUNTS if "tool_exchange" in value["counts"] else ()), 0)
    for (ordinal, digest), k in zip(value["items"], keys):
        stored = rows.get(k)
        if stored is None:
            # Retryable: the item's own write has not committed yet; the manifest goes again after it does.
            _refuse(f"ordinal {ordinal} has no item yet: a manifest follows its items",
                    "E_CONTEXT_MANIFEST_ITEM_MISSING")
        if not isinstance(stored, dict) or stored.get("kind") != "item":
            _refuse(f"ordinal {ordinal} has no item: a batch holds whole items only", "E_CONTEXT_MANIFEST")
        if stored.get("item") == "omitted":
            if digest is not None:
                _refuse(f"ordinal {ordinal} is omitted; its entry digest is null", "E_CONTEXT_MANIFEST")
            tally["omitted_" + stored["omitted_reason"]] += 1
        else:
            if digest != stored.get("text_sha256"):
                _refuse(f"ordinal {ordinal}'s digest is not the stored item's", "E_CONTEXT_MANIFEST")
            tally["written"] += 1
            if stored.get("item") in ("tool_exchange", "thinking"):
                if "tool_exchange" not in tally:
                    _refuse(f"ordinal {ordinal} is an F3 item; the manifest counts the F3 classes", "E_CONTEXT_MANIFEST")
                tally[stored["item"]] += 1
                if stored["item"] == "tool_exchange":
                    tally["call_missing"] += stored.get("call_status") == "missing"
                    tally["result_missing"] += stored.get("result_status") == "missing"
                    tally["parts_extra"] += stored.get("part", 1) > 1
    if "tool_exchange" in tally and tally["tool_exchange"] + tally["thinking"] == 0:
        # The F3 counts are written only beside F3 items, even all zero (nxxxxx on #433): one manifest shape per batch.
        _refuse("the F3 counts belong to a batch with F3 items only", "E_CONTEXT_MANIFEST")
    if tally != value["counts"]:
        _refuse(f"counts {value['counts']} do not match the stored items {tally}", "E_CONTEXT_MANIFEST")
    _check_split_exchanges(ns, [(ordinal, rows[k]) for (ordinal, _d), k in zip(value["items"], keys)])


def _check_split_exchanges(ns, stored) -> None:
    """Every split tool_exchange in the batch is whole (txxxx on #433): parts 1..n on consecutive ordinals, one
    tool_ref, exchange_sha256, tool and statuses across them, the joined texts hashing to exchange_sha256 and decoding to
    {tool, call, result}. The parser keeps all of one exchange's parts in one batch, so the manifest is where the whole
    is visible; it is also where the screens see a secret that spans two parts, which no per-part screen can. A refusal
    here leaves the batch without its manifest, so it is never indexed or searched."""
    runs: list[list[tuple[int, dict]]] = []
    for ordinal, v in stored:
        if v.get("item") != "tool_exchange" or v.get("parts", 1) == 1:
            continue
        if v["part"] == 1:
            runs.append([(ordinal, v)])
        elif not runs or runs[-1][-1][0] != ordinal - 1 or runs[-1][-1][1]["part"] != v["part"] - 1:
            _refuse(f"ordinal {ordinal} is part {v['part']} of an exchange whose earlier parts are not before it",
                    "E_CONTEXT_MANIFEST")
        else:
            runs[-1].append((ordinal, v))
    same = ("tool_ref", "exchange_sha256", "tool", "call_status", "result_status", "parts")
    for run in runs:
        head = run[0][1]
        if len(run) != head["parts"] or any(any(v.get(f) != head.get(f) for f in same) for _o, v in run):
            _refuse(f"the exchange starting at ordinal {run[0][0]} is not whole in this batch", "E_CONTEXT_MANIFEST")
        joined = "".join(v["text"] for _o, v in run)
        if hashlib.sha256(joined.encode("utf-8")).hexdigest() != head["exchange_sha256"]:
            _refuse(f"the parts from ordinal {run[0][0]} do not join to exchange_sha256", "E_CONTEXT_MANIFEST")
        body = _decode_exchange(joined)
        if not _exchange_shaped(body) or body["tool"] != head.get("tool"):
            _refuse(f"the parts from ordinal {run[0][0]} do not join to the JSON {{tool, call, result}}",
                    "E_CONTEXT_MANIFEST")
        if screens_apply(ns):
            screens.screen(*_exchange_fields(body))


__all__ = ["DEFAULT_NAMESPACE", "MAX_VALUE_BYTES", "SCHEMA", "enforce_context", "is_context_namespace", "key_kind",
           "namespace", "screens_apply", "validate_context"]
