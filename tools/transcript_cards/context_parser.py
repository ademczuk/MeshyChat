"""context_parser.py - context.v1 items and manifests from transcript records (txxxx, parser owner).

Builds the dialogue items of docs/design/context-v1.md (r4, as tightened by #346) from a seat's transcript
records, locally and deterministically. It sends nothing: the uploader (codex-kxxx-cli's queue) freezes the bytes
this produces. Dialects: codex, claude, antigravity and kimi.

Rules it implements, each pinned by tests/test_transcript_cards_context_parser.py:
  * One item per TEXT BLOCK. User text and assistant text only; Codex `analysis` phase, Codex `event_msg`
    duplicates, Claude tool results, sidechains and isMeta records, and Kimi loop events (step markers, tool
    calls and results, thinking), machine-origin user messages, `agent.message.appended` copies and bookkeeping
    records are not dialogue.
  * Both screens run on the WHOLE text (the loader's own DETECTORS, then the client-name list). A hit makes the
    item `omitted` with only its reason. Nothing derived from the text is kept.
  * EXCEPT for the namespace shared/transcript-context, where NEITHER screen runs and text is kept as written,
    secrets and client names included (Bxxxxx, 24 Oct 2026 about (time withheld), "Store everything, secrets
    included", relayed by kxxx in msg/<key>; the server half is #380's screens_apply). The
    parser is told the namespace; with none, or any other one, both screens run as before.
  * An item whose complete serialized value is over 65,536 bytes is `omitted`, reason too_large, carrying only
    the original text's UTF-8 size. v1 never keeps a prefix.
  * update_plan / TodoWrite become task items by txxxx's b2d1a0 rule (a multiset of statuses per step text).
  * Lone surrogates become U+FFFD before screening, hashing and serializing.
  * A subagent source gives ONE artifact item: its last non-empty assistant text block, after finish().
Batches are the uploader's cut: it calls close_batch() at each local commit (the design allocates batch numbers
at commit), and a full batch closes itself. The parser never decides where a batch ends beyond the 256 cap.
Checkpoint size: state() holds at most one pending final text, only if its whole artifact value fits the 65,536 byte
budget. The state's own JSON encoding can escape that text again (a control character is up to 6 bytes), so the
worst case is about 400 KB; no server limit applies to a local checkpoint.
Choices the design leaves open are listed in ASSUMPTIONS and were decided with kxxx.
"""

import argparse
import datetime
import hashlib
import json
import os
import sys
import unicodedata

try:
    import antigravity_dialect
    import kimi_dialect
    import transcript_cards as tc
except ImportError:                                            # imported as a package module
    from . import antigravity_dialect
    from . import kimi_dialect
    from . import transcript_cards as tc

preferred_antigravity_path = antigravity_dialect.preferred_antigravity_path

CONTEXT_ADAPTER_VERSION = "1.0"
SCHEMA = "context.v1"
MAX_VALUE_BYTES = 65_536
MAX_STEP_CHARS = 1_000
MAX_PLAN_STEPS = 256
DEFAULT_BATCH_ITEMS = 256
DIALECTS = ("codex", "claude", "antigravity", "kimi")
STATE_VERSION = 8              # 8: F3 tool_exchange pairing state (27 Oct 2026); 7: antigravity rewind refs (26 Oct).
                             # The kimi dialect adds no checkpoint field. A v6 codex/claude/kimi state upgrades to v7
                             # with ag_hwm=None, and a v7 state of any dialect to v8 with F3 off (_upgrade_state).
GENERATION = 1
UNSCREENED_NAMESPACE = "shared/transcript-context"
# F3 (ingest plan D1, step 3 item 8, step 9b; spec agreed with txxxx on <thread>). Off by default.
F3_PENDING_BYTES = 1 << 20     # calls waiting for their result, in the checkpoint (the uploader's limit is 4 MiB)
F3_SEEN_IDS = 2048             # recent tool-use ids, for duplicate results; older ones are not remembered
F3_FLUSH_AGE_S = 300           # a call this much older (by data time) than the newest record goes out result-missing
F3_IDLE_FLUSH_S = 24 * 3600    # by the caller's clock, only after this long: a long tool run (a gate, an operator
                               # question) also leaves the file idle, and must stay one exchange (txxxx)
F3_MAX_PARTS = 128             # an exchange needing more parts than this is omitted too_large (a batch holds 256)
F3_DIALECT_BYTES = 65_536     # a dialect adapter's own F3 checkpoint slot (antigravity: step pairing + early results)
F3_EXCLUDED = ("encrypted_reasoning", "is_meta", "compact_summary", "tool_duplicate_result", "tool_duplicate_call",
               "truncated_fields")
_AG_TOOL_RESULTS = ("GENERIC", "TOOL_RESPONSE", "RUN_COMMAND", "VIEW_FILE", "CODE_ACTION", "GREP_SEARCH",
                    "LIST_DIRECTORY", "MCP_TOOL", "READ_URL_CONTENT", "SEARCH_WEB")
_AG_COMPACT_TYPES = ("CHECKPOINT", "CONVERSATION_HISTORY")
_AG_META_TYPES = ("SYSTEM_MESSAGE", "ERROR_MESSAGE")
_AG_RES_SEEN = 128


def screens_apply(namespace):
    """False only for the NAME shared/transcript-context; True for every other namespace and for none."""
    return namespace != UNSCREENED_NAMESPACE

ASSUMPTIONS = (
    "omitted_bytes is the UTF-8 size of the original text (a task: of its step)",
    "a task item carries text_sha256 of its one-line text, like any item with text",
    "one `added` item per added occurrence of a step text, so multiplicity survives",
    "machine-typed user prompts (postbox deliveries, launcher text) are dropped as the cards drop them",
    "an unknown plan status counts as pending",
    "a Claude sidechain record with no agentId quarantines its source (skipped: no_agent_id), never the parent id",
    "an Antigravity dialogue record with no session_id or conversation_id quarantines its source (skipped: no_session_id)",
    "F3 (off by default): a tool call and its result are one tool_exchange item, paired by tool-use id; a call with no "
    "result by the age cut goes out result-missing, a late result call-missing; a too-large exchange is screened whole "
    "and then split losslessly into parts; empty or encrypted thinking is reported as encrypted_reasoning",
    "F3: a call waits in the local checkpoint, unscreened, until its result arrives (at most F3_PENDING_BYTES); both "
    "screens run on the whole exchange when it is emitted, and nothing of it leaves the machine before then",
    "Antigravity step_index rewinds append created_at to native_ref when an earlier step_index reappears with a later timestamp; same-or-earlier-timestamp replays are skipped (skipped: replay)",
    "F3 Antigravity pairs tool_calls at planner step P with MODEL tool-result step P + 1 + idx via f3_dialect (buffering early results that precede their planner line under F3_DIALECT_BYTES, or pairing by explicit call id when present), flushes open calls and unclaimed early results on turn boundaries or age cuts, tracks tool-result step rewinds separately from dialogue ag_hwm, and reports compact-log truncated_fields instead of emitting chopped text",
    "a Kimi source takes its identity from the wire path (session_<id>/agents/<agent>), never from a record; a "
    "content record with no derived or given session id quarantines the source (skipped: no_session_id). A wire "
    "is a subagent's only under a literal agents/<dir> with a dir other than main (any other layout is a main "
    "wire), and a subagent wire's src is the session id - given or derived - plus the path's agents/<agent> "
    "suffix, so it never collides with the parent",
    "a Kimi context.undo or context.clear is reported (skipped: kimi_undo, kimi_clear) but retracts nothing: the "
    "wire is append-only, the v6 checkpoint holds no pending-message buffer, and an item already frozen cannot be "
    "rewritten, so a retracted message keeps its item (as every dialect keeps pre-compaction dialogue)",
    "Kimi agent.message.appended and turn.prompt records repeat the append_message and loop-event content and "
    "are bookkeeping (skipped: kimi_bookkeeping), as are llm.*, usage.*, token_counting.* and the other harness "
    "record families",
)

_REPLAY = object()


class PlanStateFull(ValueError):
    """The plan table holds MAX_PLAN_STEPS distinct step texts. It refuses; it never drops."""


def _is_int(v, low=0, high=None):
    return type(v) is int and v >= low and (high is None or v <= high)


def _ag_utc_ns(ts):
    """Canonical UTC instant at nanosecond precision (`YYYY-MM-DDTHH:MM:SS.nnnnnnnnnZ`), or `""` when unparseable.
    Converts offset timestamps (`+HH:MM` / `-HH:MM`) to UTC so distinct ISO spellings of the same instant compare
    equal and string order equals chronological order."""
    m = tc._INSTANT.match(ts) if isinstance(ts, str) else None
    if not m:
        return ""
    y, mo, d, h, mi, s, frac, zone = m.groups()
    try:
        t = datetime.datetime(int(y), int(mo), int(d), int(h), int(mi), int(s))
        if zone != "Z":
            offset = datetime.timedelta(hours=int(zone[1:3]), minutes=int(zone[4:6]))
            t = t - offset if zone[0] == "+" else t + offset
    except (ValueError, OverflowError):
        return ""
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + ((frac or "") + "000000000")[:9] + "Z"


def _is_ag_ns(s):
    return isinstance(s, str) and len(s) == 30 and _ag_utc_ns(s) == s


def _ag_clean_id(raw):
    return tc._no_lone_surrogates(raw).strip() if isinstance(raw, str) and raw.strip() else None


def _ag_truncated(rec):
    """Fields compact transcript.jsonl chopped on `rec`; a malformed `truncated_fields` fails closed on all three."""
    tf = rec.get("truncated_fields")
    if tf is None:
        return frozenset()
    if isinstance(tf, list) and all(isinstance(x, str) for x in tf):
        return frozenset(tf)
    return frozenset(("content", "thinking", "tool_calls"))


def _valid_ag_f3(d):
    return (set(d) <= {"steps", "early", "res_seen", "res_hwm", "anon"}
            and ("steps" not in d or (isinstance(d["steps"], list)
                                      and all(isinstance(x, list) and len(x) == 3
                                              and (x[0] is None or _is_int(x[0], 0))
                                              and isinstance(x[1], str) and bool(x[1])
                                              and (x[2] == "" or _is_ag_ns(x[2])) for x in d["steps"])))
            and ("early" not in d or (isinstance(d["early"], list)
                                      and all(isinstance(w, list) and len(w) == 6
                                              and isinstance(w[0], str) and len(w[0]) == 32
                                              and all(c in "0123456789abcdef" for c in w[0])
                                              and (_is_int(w[1], 1) or (isinstance(w[1], str) and bool(w[1])))
                                              and (w[2] is None or isinstance(w[2], str))
                                              and type(w[3]) is bool and (w[4] == "" or _is_ag_ns(w[4]))
                                              and (w[5] is None or isinstance(w[5], str)) for w in d["early"])
                                      and len({w[0] for w in d["early"]}) == len(d["early"])))
            and ("res_seen" not in d or (isinstance(d["res_seen"], list) and len(d["res_seen"]) <= _AG_RES_SEEN
                                         and all(isinstance(x, list) and len(x) == 3 and _is_int(x[0], 0)
                                                 and isinstance(x[1], str)
                                                 and (x[1] == "" or (len(x[1]) == 32
                                                                     and all(c in "0123456789abcdef" for c in x[1])))
                                                 and _is_ag_ns(x[2]) for x in d["res_seen"])
                                         and len({(x[0], x[1]) for x in d["res_seen"]}) == len(d["res_seen"])))
            and ("res_hwm" not in d or (isinstance(d["res_hwm"], list) and len(d["res_hwm"]) == 4
                                        and _is_int(d["res_hwm"][0], 0) and _is_ag_ns(d["res_hwm"][1])
                                        and _is_int(d["res_hwm"][2], 0, d["res_hwm"][0])
                                        and (d["res_hwm"][3] == "" or _is_ag_ns(d["res_hwm"][3]))))
            and ("anon" not in d or _is_int(d["anon"], 1)))


def _upgrade_v6_state(state):
    """Migrate a v6 checkpoint for `codex`, `claude` or `kimi` to v7 (`ag_hwm=None`) without mutating the caller's
    dict, so sources enrolled before #421 resume without re-uploading (kxxx). A kimi v6 state migrates the
    same way: the kimi dialect has no antigravity rewind state, so `ag_hwm=None` is its only possible value. A v6
    state for `antigravity` could never exist and stays refused."""
    if (isinstance(state, dict) and state.get("v") == 6
            and state.get("dialect") in ("codex", "claude", "kimi") and "ag_hwm" not in state):
        upgraded = dict(state)
        upgraded["v"] = 7
        upgraded["ag_hwm"] = None
        return upgraded
    return state


def _upgrade_v7_state(state):
    """Migrate a v7 checkpoint of any dialect to v8 with F3 off and nothing pending, without mutating the caller's
    dict. F3 is off for every source enrolled before it, so its items are exactly the v7 parser's."""
    if (isinstance(state, dict) and state.get("v") == 7
            and state.get("dialect") in ("codex", "claude", "antigravity", "kimi")
            and not any(k in state for k in ("f3", "tool_pending", "tool_seen", "newest_time", "f3_dialect"))):
        upgraded = dict(state)
        upgraded.update(v=8, f3=False, tool_pending=[], tool_seen=[], newest_time="", f3_dialect={})
        return upgraded
    return state


def _upgrade_state(state):
    return _upgrade_v7_state(_upgrade_v6_state(state))


def _utc_seconds(t):
    """Seconds of a utc_instant string, or None."""
    try:
        return datetime.datetime.strptime(t, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=datetime.timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def validate_state(state):
    """Refuse an unusable checkpoint with ValueError, never KeyError, and never accept an impossible number."""
    state = _upgrade_state(state)
    keys = ("v", "seat", "dialect", "gen", "batch_items", "adapter_version", "denylist_sha256", "drop_machine_typed",
            "src", "subagent", "ordinal", "plan", "oversize", "final", "finished", "batch", "batch_count", "quarantined",
            "ag_hwm", "screens", "f3", "tool_pending", "tool_seen", "newest_time", "f3_dialect")
    if not isinstance(state, dict) or state.get("v") != STATE_VERSION or state.get("adapter_version") != CONTEXT_ADAPTER_VERSION:
        # Older versions lacked fields or tagged plan keys differently. The parser is deterministic, so a rebuild
        # from the start of the source gives the same bytes; resuming an old state would not.
        raise ValueError("state was written by another parser version: rebuild the source")
    missing = [k for k in keys if k not in state]
    if missing:
        raise ValueError("state is incomplete (%s): rebuild the source" % ", ".join(missing))
    bad = []
    if not (isinstance(state["seat"], str) and state["seat"]):
        bad.append("seat")
    if state["dialect"] not in DIALECTS:
        bad.append("dialect")
    if not _is_int(state["gen"], 1):
        bad.append("gen")
    if not _is_int(state["batch_items"], 1, 256):
        bad.append("batch_items")
    if not isinstance(state["denylist_sha256"], str):
        bad.append("denylist_sha256")
    if not all(type(state[k]) is bool for k in ("drop_machine_typed", "subagent", "finished", "quarantined", "screens",
                                                 "f3")):
        bad.append("a flag")
    pending = state["tool_pending"]
    if not (isinstance(pending, list) and all(
            isinstance(e, list) and len(e) == 5 and isinstance(e[0], str) and len(e[0]) == 32
            and (e[1] is None or isinstance(e[1], str)) and isinstance(e[2], str) and isinstance(e[3], str)
            and (e[4] is None or isinstance(e[4], str)) for e in pending)
            and len(serialize(pending)) <= F3_PENDING_BYTES + MAX_VALUE_BYTES
            and len({e[0] for e in pending}) == len(pending)):
        bad.append("tool_pending")
    seen = state["tool_seen"]
    if not (isinstance(seen, list) and len(seen) <= F3_SEEN_IDS and all(
            isinstance(e, list) and len(e) == 2 and isinstance(e[0], str) and len(e[0]) == 32
            and e[1] in ("p", "f") for e in seen)):
        bad.append("tool_seen")
    if not isinstance(state["newest_time"], str):
        bad.append("newest_time")
    if not (isinstance(state["f3_dialect"], dict) and len(serialize(state["f3_dialect"])) <= F3_DIALECT_BYTES):
        bad.append("f3_dialect")
    elif state["dialect"] == "antigravity" and not _valid_ag_f3(state["f3_dialect"]):
        bad.append("f3_dialect")
    if state["f3"] is False and (state["tool_pending"] or state["tool_seen"] or state["f3_dialect"]):
        bad.append("F3 pairing state on a source with F3 off")
    src = state["src"]
    if src is not None and not (isinstance(src, str) and len(src) == 32 and all(c in "0123456789abcdef" for c in src)):
        bad.append("src")
    if not _is_int(state["ordinal"]):
        bad.append("ordinal")
    if not _is_int(state["batch"], 1):
        bad.append("batch")
    if _is_int(state["batch_items"], 1, 256) and not _is_int(state["batch_count"], 0, state["batch_items"] - 1):
        bad.append("batch_count")
    plan = state["plan"]
    if not (isinstance(plan, list) and len(plan) <= MAX_PLAN_STEPS and all(
            isinstance(r, list) and len(r) == 2 and isinstance(r[0], str) and r[0][:2] in ("t:", "h:")
            and isinstance(r[1], list) and len(r[1]) == 3 and all(_is_int(n) for n in r[1]) for r in plan)):
        bad.append("plan")
    over = state["oversize"]
    if not (isinstance(over, dict) and all(
            isinstance(k, str) and k.startswith("h:") and isinstance(v, dict) and "bytes" in v and "reason" in v
            and _is_int(v["bytes"]) and v["reason"] in (None, "secret", "client_name") for k, v in over.items())):
        bad.append("oversize")
    elif isinstance(plan, list) and any(isinstance(r, list) and r and isinstance(r[0], str) and r[0].startswith("h:")
                                        and r[0] not in over for r in plan):
        bad.append("an active plan step has no oversize entry")
    final = state["final"]
    if final is not None and not (
            isinstance(final, dict) and all(k in final for k in ("time", "ref", "bytes", "reason", "text"))
            and isinstance(final["time"], str) and _is_int(final["bytes"]) and final["reason"] in (None, "secret", "client_name")
            and (final["ref"] is None or isinstance(final["ref"], str))
            and (final["text"] is None or isinstance(final["text"], str))):
        bad.append("final")
    ag_hwm = state["ag_hwm"]
    if ag_hwm is not None and not (
            isinstance(ag_hwm, list) and len(ag_hwm) == 4
            and _is_int(ag_hwm[0], 0) and _is_ag_ns(ag_hwm[1])
            and _is_int(ag_hwm[2], 0, ag_hwm[0]) and isinstance(ag_hwm[3], str)
            and ((ag_hwm[3] == "" and ag_hwm[2] == ag_hwm[0])
                 or (_is_ag_ns(ag_hwm[3]) and ag_hwm[3] < ag_hwm[1]))):
        bad.append("ag_hwm")
    if bad:
        raise ValueError("state is not usable (%s): rebuild the source" % ", ".join(bad))


def serialize(value):
    """The server's serialization: the cards' json.dumps, UTF-8."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _tool_text(value):
    """A tool call's input or a result as text: a string as it is, anything else as compact JSON (serialize()'s form).
    Never encoded here: a lone surrogate inside a transcript's structure would make serialize() raise
    UnicodeEncodeError, a ValueError the uploader turns into parser_refused, stalling the source at that record for
    good (GitHub Codex on 1cf18567, context_parser.py:572). The parser replaces lone surrogates before it screens or
    hashes the text."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_strings(text):
    """Every string inside `text` when `text` is a JSON array or object, keys included: what a structured tool input
    or result held before _tool_text encoded it. [] for anything else."""
    if not isinstance(text, str) or not text.lstrip()[:1] in ("[", "{"):
        return []
    try:
        stack, out = [json.loads(text)], []
    except ValueError:
        return []
    while stack:
        value = stack.pop()
        if isinstance(value, str):
            out.append(tc._no_lone_surrogates(value))
        elif isinstance(value, dict):
            out.extend(tc._no_lone_surrogates(k) for k in value if isinstance(k, str))
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    return out


def _json_text_bytes(text):
    """The UTF-8 bytes `text` takes as a JSON string value under serialize(), quotes excluded."""
    return len(json.dumps(text, ensure_ascii=False).encode("utf-8")) - 2


def _split_text(text, budget):
    """Cut `text` into consecutive pieces, each within `budget` bytes once escaped as a JSON string. Their
    concatenation is `text` exactly. None if the budget cannot hold a single character."""
    if budget < 12:                          # the longest escape of one character is 6 bytes; keep a margin
        return None
    pieces, start = [], 0
    while start < len(text):
        low, high = 1, min(len(text) - start, budget)
        while low < high:                    # the longest prefix that fits
            mid = (low + high + 1) // 2
            if _json_text_bytes(text[start:start + mid]) <= budget:
                low = mid
            else:
                high = mid - 1
        pieces.append(text[start:start + low])
        start += low
    return pieces


def _hex32(text):
    return _sha(tc._no_lone_surrogates(text))[:32]


def _normalize_name(text):
    return "".join(c for c in unicodedata.normalize("NFKC", text).casefold() if unicodedata.category(c)[0] in "LN")


class ClientNames:
    """The client-name list: NFKC, case folding, letters and digits only; a marker under 4 such characters is
    refused for the whole list (the server's rule)."""

    def __init__(self, markers=()):
        self.markers = []
        for marker in markers:
            norm = _normalize_name(marker)
            if len(norm) < 4:
                raise ValueError("a client-name marker needs at least 4 letters or digits")
            self.markers.append(norm)
        self.markers = sorted(set(self.markers))

    def digest(self):
        return _sha(json.dumps(self.markers)) if self.markers else ""

    def hits(self, text):
        if not self.markers:
            return False
        norm = _normalize_name(text)
        return any(m in norm for m in self.markers)


def item_key(item):
    return "ctx/%s/%s/%04d/%06d/%010d" % (item["seat"], item["src"], item["gen"], item["batch"], item["ordinal"])


def manifest_key(item):
    return "ctxm/%s/%s/%04d/%06d" % (item["seat"], item["src"], item["gen"], item["batch"])


def manifests(items, batch_items=DEFAULT_BATCH_ITEMS):
    """One manifest per batch of items, in batch order. `items` are this parser's values, in ordinal order."""
    out, by_batch = [], {}
    for item in items:
        by_batch.setdefault(item["batch"], []).append(item)
    for batch in sorted(by_batch):
        rows = by_batch[batch]
        head = rows[0]
        counts = {"written": 0, "omitted_secret": 0, "omitted_client_name": 0, "omitted_too_large": 0}
        if any(row["item"] in ("tool_exchange", "thinking") for row in rows):
            # F3 classes, only in a batch that has F3 items, so an F1 manifest's bytes do not change.
            counts.update(tool_exchange=0, thinking=0, call_missing=0, result_missing=0, parts_extra=0)
        entries = []
        for row in rows:
            if row["item"] == "omitted":
                counts["omitted_" + row["omitted_reason"]] += 1
                entries.append([row["ordinal"], None])
            else:
                counts["written"] += 1
                entries.append([row["ordinal"], row["text_sha256"]])
                if row["item"] == "thinking":
                    counts["thinking"] += 1
                elif row["item"] == "tool_exchange":
                    counts["tool_exchange"] += 1
                    counts["call_missing"] += row["call_status"] == "missing"
                    counts["result_missing"] += row["result_status"] == "missing"
                    counts["parts_extra"] += row["part"] > 1
        out.append({"schema": SCHEMA, "kind": "manifest", "seat": head["seat"], "src": head["src"],
                    "gen": head["gen"], "batch": batch, "first": rows[0]["ordinal"], "last": rows[-1]["ordinal"],
                    "items": entries, "counts": counts})
    return out


class ContextParser:
    def __init__(self, seat, dialect, denylist=(), batch_items=DEFAULT_BATCH_ITEMS, gen=GENERATION,
                 drop_machine_typed=True, _state=None, namespace=None, session_id=None, subagent=False,
                 source_path=None, f3=False):
        if dialect not in DIALECTS:
            raise ValueError("dialect must be one of %s" % (DIALECTS,))
        if not 1 <= batch_items <= 256:
            raise ValueError("a batch holds 1 to 256 items")
        if type(subagent) is not bool:
            raise ValueError("subagent must be a bool")
        if type(f3) is not bool:
            raise ValueError("f3 must be a bool")
        self.f3 = f3                 # F3 item kinds; part of the enrollment identity (the uploader's metadata)
        self.tool_pending = []       # [tool_ref, name, call_text, time, native_ref]: calls waiting, in call order
        self.tool_seen = []          # [tool_ref, "p"|"f"]: recent ids, paired or flushed result-missing
        self.newest_time = ""        # the newest record time seen (utc_instant), the flush's data clock
        self.f3_dialect = {}         # a dialect adapter's own F3 state, JSON only, at most F3_DIALECT_BYTES
        self._unit = None            # [first item index, F1 items reserved] of the record being parsed (_f3_room)
        self.seat, self.dialect, self.gen = seat, dialect, gen
        self.batch_items, self.drop_machine_typed = batch_items, drop_machine_typed
        self.names = ClientNames(denylist)
        self.screens = screens_apply(namespace)   # False only for shared/transcript-context
        self.items = []              # items emitted by THIS instance (a revived parser starts empty)
        self.skipped = []            # (reason, ref): records that could not become an item
        self.src = None
        self.subagent = subagent
        self.ag_hwm = None           # antigravity (max_step_index, first_created_at_at_max) rewind guard (O(1))
        if _state is None and dialect == "antigravity":
            raw_sp = os.fspath(source_path) if isinstance(source_path, (str, os.PathLike)) else None
            if isinstance(raw_sp, str) and raw_sp.strip():
                if session_id is None:
                    derived = antigravity_dialect._session_from_path(raw_sp)
                    if derived:
                        session_id = derived
                if antigravity_dialect.is_subagent_path(raw_sp):
                    self.subagent = True
            if isinstance(session_id, str) and session_id.strip():
                self.src = _hex32(session_id.strip())
        if _state is None and dialect == "kimi":
            # A Kimi record carries no session id: the wire path is the identity (kimi_dialect's matchers).
            raw_sp = os.fspath(source_path) if isinstance(source_path, (str, os.PathLike)) else None
            if isinstance(raw_sp, str) and raw_sp.strip():
                k_session, k_sub, k_agent = kimi_dialect.wire_identity(raw_sp)
                if k_sub:
                    # A subagent wire is never keyed by the bare session id, whether the id was derived from
                    # the path or given explicitly: the agents/<agent> suffix keeps its src distinct from the
                    # parent's main wire (txxxx's step-9 review, P2 #1).
                    self.subagent = True
                    base = session_id if isinstance(session_id, str) and session_id.strip() else k_session
                    if base:
                        session_id = base + "/agents/" + k_agent
                elif session_id is None and k_session:
                    session_id = k_session
            if isinstance(session_id, str) and session_id.strip():
                self.src = _hex32(session_id.strip())
        self.ordinal = 0
        self.plan = {}               # step key: [pending, in_progress, completed]
        self.oversize = {}           # "h:" key of an over-long step: its size and screen verdict
        self.quarantined = False     # a sidechain-first source with no agentId: it gives nothing, ever
        self.batch = 1               # the batch the next item belongs to; the uploader closes it with close_batch()
        self.batch_count = 0         # items in the current batch; a full batch closes itself
        self.final = None            # a subagent source: the last non-empty assistant block seen so far
        self.finished = False
        if _state is not None:
            self.src, self.subagent, self.ordinal = _state["src"], _state["subagent"], _state["ordinal"]
            self.plan = {step: list(counts) for step, counts in _state["plan"]}
            self.oversize = dict(_state["oversize"])
            self.final, self.finished = _state["final"], _state["finished"]
            self.batch, self.batch_count = _state["batch"], _state["batch_count"]
            self.quarantined = _state["quarantined"]
            self.ag_hwm = tuple(_state["ag_hwm"]) if _state["ag_hwm"] is not None else None
            self.screens = _state["screens"]
            if _state["f3"] != f3:
                raise ValueError("the state was written with F3 %s: switching it needs a new generation"
                                 % ("on" if _state["f3"] else "off"))
            self.tool_pending = [list(e) for e in _state["tool_pending"]]
            self.tool_seen = [list(e) for e in _state["tool_seen"]]
            self.newest_time = _state["newest_time"]
            self.f3_dialect = json.loads(json.dumps(_state["f3_dialect"]))

    # ---- checkpoint state: advances only through feed(), so a caller commits it with its offset -------------

    def state(self):
        return {"v": STATE_VERSION, "seat": self.seat, "dialect": self.dialect, "gen": self.gen,
                "batch_items": self.batch_items, "adapter_version": CONTEXT_ADAPTER_VERSION,
                "denylist_sha256": self.names.digest(), "drop_machine_typed": self.drop_machine_typed,
                "src": self.src, "subagent": self.subagent,
                "ordinal": self.ordinal, "plan": [[s, list(c)] for s, c in self.plan.items()],
                "oversize": dict(self.oversize), "final": self.final, "finished": self.finished,
                "batch": self.batch, "batch_count": self.batch_count, "quarantined": self.quarantined,
                "ag_hwm": list(self.ag_hwm) if self.ag_hwm is not None else None,
                "screens": self.screens, "f3": self.f3, "tool_pending": [list(e) for e in self.tool_pending],
                "tool_seen": [list(e) for e in self.tool_seen], "newest_time": self.newest_time,
                "f3_dialect": json.loads(json.dumps(self.f3_dialect))}

    @classmethod
    def from_state(cls, state, denylist=(), namespace=None, f3=None):
        """Resume a checkpoint. The screens come from the state, and `namespace`, when given, must agree with them:
        a checkpoint written unscreened is never resumed for a screened namespace, nor the other way round. `f3`,
        when given, must agree with the state's too: one source never mixes F1 and F3 items."""
        validate_state(state)
        state = _upgrade_state(state)
        parser = cls(state["seat"], state["dialect"], denylist, state["batch_items"], state["gen"],
                     state["drop_machine_typed"], _state=state, f3=state["f3"] if f3 is None else f3)
        if parser.names.digest() != state["denylist_sha256"]:
            raise ValueError("the client-name list differs from the one the state was written with")
        if namespace is not None and screens_apply(namespace) != state["screens"]:
            raise ValueError("the state was written %s, but namespace %s is %s: rebuild the source" % (
                "screened" if state["screens"] else "unscreened", namespace,
                "screened" if screens_apply(namespace) else "unscreened"))
        return parser

    # ---- input ----------------------------------------------------------------------------------------------

    def feed(self, record):
        """Parse one transcript record. Returns the items it produced (also appended to self.items)."""
        if self.finished:
            raise ValueError("this source is finished: it takes no more records")
        before = len(self.items)
        if not isinstance(record, dict):
            self.skipped.append(("not_an_object", None))
            return []
        self._unit = [before, 0]
        try:
            if self.dialect == "codex":
                self._codex(record)
            elif self.dialect == "antigravity":
                self._antigravity(record)
            elif self.dialect == "kimi":
                self._kimi(record)        # F3 adds tool_exchange and thinking only on main wires; off is byte-identical
            else:
                self._claude(record)
        finally:
            self._unit = None
        return self.items[before:]

    def _codex(self, rec):
        kind, payload = rec.get("type"), rec.get("payload")
        if not isinstance(payload, dict):
            return
        if kind == "session_meta":
            if self.src is None and isinstance(payload.get("id"), str):
                self.src = _hex32(payload["id"])
            source = payload.get("source")
            if isinstance(source, dict) and "subagent" in source:
                self.subagent = True
            return
        if self.f3 and kind == "compacted" and not self.subagent:
            self._saw_time(tc.utc_instant(rec.get("timestamp")))
            return self._excluded("compact_summary", None)
        if kind != "response_item":
            return                                    # event_msg repeats the response_item
        time = tc.utc_instant(rec.get("timestamp"))
        if self.f3 and not self.subagent:
            self._saw_time(time)
            # The item's dialogue and plan items are emitted after its F3 items (a pressure flush among them): keep a
            # slot for each (_f3_room; GitHub Codex on 7d96b08f, txxxx).
            self._unit[1] = self._codex_f1_reserve(payload, time)
            self._codex_f3(payload, time)
        if payload.get("type") == "message":
            role = payload.get("role")
            if role not in ("user", "assistant") or payload.get("phase") == "analysis":
                return
            if self.subagent:                         # a subagent source: only its last assistant block matters
                if role == "assistant":
                    for part in payload.get("content") if isinstance(payload.get("content"), list) else []:
                        if isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str):
                            self._final_candidate(part["text"], time, payload.get("id"))
                return
            content = payload.get("content")
            for part in content if isinstance(content, list) else []:      # one item per text block, in order
                if not (isinstance(part, dict) and part.get("type") in ("output_text", "input_text")
                        and isinstance(part.get("text"), str)):
                    continue
                if role == "user" and self.drop_machine_typed and tc._CODEX_MACHINE_TYPED.search(part["text"]):
                    continue
                self._text(role, part["text"], time, payload.get("id"))
        elif payload.get("type") == "function_call" and payload.get("name") == "update_plan" and not self.subagent:
            try:
                steps = json.loads(payload.get("arguments") or "{}").get("plan")
            except (ValueError, AttributeError, TypeError):
                steps = None
            if not isinstance(steps, list):
                self.skipped.append(("plan_unreadable", payload.get("call_id")))
                return
            self._plan([(s.get("step"), s.get("status")) for s in steps if isinstance(s, dict)],
                       time, payload.get("call_id"))

    def _codex_f1_reserve(self, payload, time):
        """The F1 items a Codex response_item emits after its F3 items: one per event of an update_plan, diffed from the
        current plan without changing it (_plan_diff). Its call goes to the pending calls first, which can flush older
        calls. A dialogue message emits no F3 item, so it needs no slot kept."""
        if payload.get("type") == "function_call" and payload.get("name") == "update_plan" and time:
            try:
                steps = json.loads(payload.get("arguments") or "{}").get("plan")
            except (ValueError, AttributeError, TypeError):
                return 0
            if isinstance(steps, list):
                return len(self._plan_diff(self.plan, [(s.get("step"), s.get("status")) for s in steps
                                                       if isinstance(s, dict)])[1])
        return 0

    def _codex_f3(self, payload, time):
        """F3 records of a Codex response_item: tool calls and their outputs, analysis-phase messages and reasoning."""
        ptype, ref = payload.get("type"), payload.get("id") or payload.get("call_id")
        name = payload.get("name") if isinstance(payload.get("name"), str) else None
        if ptype in ("function_call", "custom_tool_call") and isinstance(payload.get("call_id"), str):
            arg = payload.get("arguments") if ptype == "function_call" else payload.get("input")
            self._tool_call(payload["call_id"], name, _tool_text(arg),
                            time, ref)
        elif ptype in ("function_call_output", "custom_tool_call_output") and isinstance(payload.get("call_id"), str):
            out = payload.get("output")
            self._tool_result(payload["call_id"], _tool_text(out),
                              False, time, ref)
        elif ptype == "message" and payload.get("phase") == "analysis" and payload.get("role") == "assistant":
            for part in payload.get("content") if isinstance(payload.get("content"), list) else []:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    self._thinking(part["text"], time, ref)
        elif ptype == "reasoning":
            texts = [p.get("text") for key in ("summary", "content") for p in (
                payload.get(key) if isinstance(payload.get(key), list) else []) if isinstance(p, dict)
                and isinstance(p.get("text"), str) and p["text"].strip()]
            for text in texts:
                self._thinking(text, time, ref)
            if not texts:                             # encrypted_content only: nothing readable to keep
                self._excluded("encrypted_reasoning", ref)

    def _claude(self, rec):
        kind = rec.get("type")
        if self.f3 and kind in ("user", "assistant") and (rec.get("isMeta") or rec.get("isCompactSummary")):
            self._saw_time(tc.utc_instant(rec.get("timestamp")))
            return self._excluded("is_meta" if rec.get("isMeta") else "compact_summary", rec.get("uuid"))
        if kind not in ("user", "assistant") or rec.get("isMeta") or rec.get("isCompactSummary"):
            return
        if self.quarantined:
            return                                    # decided at the first record, kept in the checkpoint
        side = bool(rec.get("isSidechain"))
        if self.src is None:
            if side:                                  # a sidechain-first file is a subagent source of its own
                self.subagent = True
                native = rec.get("agentId")           # never the parent's sessionId: it would collide with the parent
                if not isinstance(native, str) or not native:
                    self.quarantined = True           # never assign an identity, whatever record comes next
                    self.skipped.append(("no_agent_id", None))
                    return
            else:
                native = rec.get("sessionId")
            if isinstance(native, str):
                self.src = _hex32(native)
        if side != self.subagent:
            return                                    # a main source skips sidechain records
        message = rec.get("message")
        if not isinstance(message, dict):
            return
        content = message.get("content")
        time, ref = tc.utc_instant(rec.get("timestamp")), rec.get("uuid")
        if self.f3 and not self.subagent:
            self._saw_time(time)
            # The record's dialogue and plan items are emitted after its F3 items: keep a slot for each (_f3_room).
            self._unit[1] = self._f1_reserve(kind, content, time)
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if kind == "assistant" and block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                    self._tool_call(block["id"], block.get("name") if isinstance(block.get("name"), str) else None,
                                    _tool_text(block.get("input")), time, ref)
                elif kind == "user" and block.get("type") == "tool_result" and isinstance(block.get("tool_use_id"), str):
                    body = block.get("content")
                    self._tool_result(block["tool_use_id"],
                                      _tool_text(body),
                                      block.get("is_error") is True, time, ref)
                elif kind == "assistant" and block.get("type") == "thinking":
                    if isinstance(block.get("thinking"), str) and block["thinking"].strip():
                        self._thinking(block["thinking"], time, ref)
                    else:                                 # signature only: the text is not in the transcript
                        self._excluded("encrypted_reasoning", ref)
                elif kind == "assistant" and block.get("type") == "redacted_thinking":
                    self._excluded("encrypted_reasoning", ref)
        if self.subagent:
            if kind == "assistant":
                for block in content if isinstance(content, list) else []:
                    if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                        self._final_candidate(block["text"], time, ref)
            return
        if kind == "user":
            blocks = [content] if isinstance(content, str) else [
                b.get("text") for b in (content if isinstance(content, list) else [])
                if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)]
            for text in blocks:                                    # a tool_result block is not dialogue
                if self.drop_machine_typed and tc._MACHINE_TYPED.search(text):
                    continue
                self._text("user", text, time, ref)
            return
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                if isinstance(block.get("text"), str):
                    self._text("assistant", block["text"], time, ref)
            elif block.get("type") == "tool_use" and block.get("name") == "TodoWrite":
                inp = block.get("input")
                todos = inp.get("todos") if isinstance(inp, dict) else None
                if isinstance(todos, list):
                    self._plan([(t.get("content"), t.get("status")) for t in todos if isinstance(t, dict)], time, ref)

    def _ag_fold_res_hwm(self, step_index, ns):
        hwm = self.f3_dialect.get("res_hwm")
        if hwm is None:
            self.f3_dialect["res_hwm"] = [step_index, ns, step_index, ""]
            return
        hi, hi_ns, cur, fl = hwm
        if step_index > hi:
            self.f3_dialect["res_hwm"] = [step_index, max(ns, hi_ns), step_index, ""]
        elif ns > (hi_ns if step_index <= cur else fl):
            self.f3_dialect["res_hwm"] = [hi, max(ns, hi_ns), step_index, hi_ns if step_index <= cur else fl]

    def _ag_open_call_ns(self, step_index, res_id):
        for st, tid, cns in self.f3_dialect.get("steps") or ():
            if (tid == res_id) if res_id is not None else (st != 0 and st == step_index):
                return cns
        return ""

    def _ag_evict_res_seen(self, seen):
        ev = min(seen, key=lambda x: x[0])
        seen.remove(ev)
        if not seen:
            self.f3_dialect.pop("res_seen", None)
        self._ag_fold_res_hwm(ev[0], ev[2])

    def _ag_record_res(self, seen, step_index, rid, ns):
        seen.append([step_index, rid, ns])
        self.f3_dialect["res_seen"] = seen
        while len(seen) > _AG_RES_SEEN:
            self._ag_evict_res_seen(seen)
        self._ag_cap_dialect()

    def _ag_ref(self, step_index, created_at, res=False, res_id=None):
        base_ref = self._ag_plain_ref(step_index)
        ns = _ag_utc_ns(created_at)
        if not ns:
            return base_ref
        call_ns = self._ag_open_call_ns(step_index, res_id) if res else ""
        if base_ref is None and not (call_ns and ns < call_ns):
            return None
        stamp = created_at if isinstance(created_at, str) else ""
        if res:
            rid = _hex32(res_id) if res_id else ""
            seen = self.f3_dialect.get("res_seen") or []
            hit = next((x for x in seen if x[0] == step_index and x[1] == rid), None)
            if call_ns and ns < call_ns:
                bound_ns = call_ns
            elif hit is not None:
                bound_ns = hit[2]
            else:
                hwm = self.f3_dialect.get("res_hwm")
                ev_bound = (hwm[1] if step_index <= hwm[2] else hwm[3]) if (hwm and step_index <= hwm[0]) else None
                if ev_bound is None or bool(call_ns):
                    self._ag_record_res(seen, step_index, rid, ns)
                    rew = ev_bound is not None or bool(self.ag_hwm and step_index <= self.ag_hwm[0] and self.ag_hwm[3])
                    return base_ref if not rew else (base_ref + ":" + stamp)
                bound_ns = ev_bound
        else:
            hwm = self.ag_hwm
            if hwm is None:
                self.ag_hwm = (step_index, ns, step_index, "")
                return base_ref
            max_step, max_ns, cur_step, floor_ns = hwm
            if step_index > max_step:
                self.ag_hwm = (step_index, max(ns, max_ns), step_index, "")
                return base_ref
            bound_ns = max_ns if step_index <= cur_step else floor_ns
        if ns <= bound_ns:
            self.skipped.append(("replay", base_ref))
            return _REPLAY
        if res:
            if hit is not None:
                hit[2] = ns
            else:
                self._ag_record_res(seen, step_index, rid, ns)
        else:
            next_floor = max_ns if step_index <= cur_step else floor_ns
            self.ag_hwm = (max_step, max(ns, max_ns), step_index, next_floor)
        return "%s:%s" % (base_ref, stamp)

    def _ag_plain_ref(self, step_index):
        if not _is_int(step_index, 0):
            return None
        return "%s:%d" % (self.src, step_index) if self.src else str(step_index)

    def _ag_anon_base(self):
        n = self.f3_dialect.get("anon", 0) + 1
        self.f3_dialect["anon"] = n
        return "%s:anon:%d" % (self.src, n)

    def _ag_emit_early(self, w):
        if w[2] is not None:
            self._seen(w[0], "p")
            self._exchange(w[0], None, None, w[2], "missing", "error" if w[3] else "ok", tc.utc_instant(w[4]), w[5])

    def _ag_cap_dialect(self):
        while len(serialize(self.f3_dialect)) > F3_DIALECT_BYTES:
            seen = self.f3_dialect.get("res_seen")
            early = self.f3_dialect.get("early")
            steps = self.f3_dialect.get("steps")
            if seen:
                self._ag_evict_res_seen(seen)
            elif early:
                w = early.pop(0)
                if not early:
                    self.f3_dialect.pop("early", None)
                self._ag_emit_early(w)
            elif steps:
                _st, tid, _cns = steps.pop(0)
                if not steps:
                    self.f3_dialect.pop("steps", None)
                self.flush_pending(everything=True, only=_hex32(tid))
            else:
                break

    def _ag_drop_step_ref(self, tool_ref):
        steps = self.f3_dialect.get("steps")
        if steps:
            kept = [x for x in steps if _hex32(x[1]) != tool_ref]
            if kept:
                self.f3_dialect["steps"] = kept
            else:
                self.f3_dialect.pop("steps", None)

    def _ag_step_push(self, target_step, tid, cns):
        steps = self.f3_dialect.setdefault("steps", [])
        steps.append([target_step, tid, cns])
        self._ag_cap_dialect()

    def _ag_step_pop(self, step_index, explicit_id):
        steps = self.f3_dialect.get("steps")
        if explicit_id is not None:
            if any(e[0] == _hex32(explicit_id) for e in self.tool_pending):
                if steps:
                    kept = [x for x in steps if x[1] != explicit_id]
                    if kept:
                        self.f3_dialect["steps"] = kept
                    else:
                        self.f3_dialect.pop("steps", None)
                return explicit_id
            return None
        if not steps:
            return None
        key = step_index if _is_int(step_index, 1) else (None if step_index is None else -1)
        if key == -1:
            return None
        for i, (st, tid, _cns) in enumerate(steps):
            if st == key:
                steps.pop(i)
                if not steps:
                    self.f3_dialect.pop("steps", None)
                return tid
        return None

    def _ag_early_pop(self, key):
        early = self.f3_dialect.get("early")
        if not early or not (_is_int(key, 1) or (isinstance(key, str) and bool(key))):
            return None
        for i, w in enumerate(early):
            if type(w[1]) is type(key) and w[1] == key:
                early.pop(i)
                if not early:
                    self.f3_dialect.pop("early", None)
                return w
        return None

    def _ag_early_push(self, key, body, is_error, ns, ref):
        prev = self._ag_early_pop(key)
        if prev is not None:
            self._ag_emit_early(prev)
        eref = _hex32(key if isinstance(key, str) else ("ag:orphan:%s" % (ref or self._ag_anon_base())))
        if any(w[0] == eref for w in self.f3_dialect.get("early", ())):
            eref = _hex32("ag:orphan:%s" % self._ag_anon_base())
        early = self.f3_dialect.setdefault("early", [])
        early.append([eref, key, None if body is None else tc._no_lone_surrogates(body),
                      bool(is_error), ns or "", ref if isinstance(ref, str) else None])
        self._ag_cap_dialect()

    def _ag_flush_open(self, keep_steps=(), min_time=""):
        for _st, tid, _cns in self.f3_dialect.pop("steps", None) or ():
            self.flush_pending(everything=True, only=_hex32(tid))
        early = self.f3_dialect.get("early")
        if early:
            kept = []
            for w in early:
                if w[1] in keep_steps and (not min_time or not w[4] or min_time <= w[4]):
                    kept.append(w)
                else:
                    self._ag_emit_early(w)
            if kept:
                self.f3_dialect["early"] = kept
            else:
                self.f3_dialect.pop("early", None)

    def _ag_user_f3(self, rec, text, step_index, raw_created, time):
        if text.lstrip().startswith("<CONTEXT_SUMMARY>"):
            ref = self._ag_ref(step_index, raw_created) if time else self._ag_plain_ref(step_index)
            if ref is _REPLAY:
                return
            self._saw_time(time)
            self._ag_flush_open()
            return self._excluded("compact_summary", ref)
        trunc = "content" in _ag_truncated(rec)
        if not trunc and (not text.strip() or (self.drop_machine_typed and tc._MACHINE_TYPED.search(text))):
            return
        if self.src is None:
            self.quarantined = True
            return self.skipped.append(("no_session_id", None))
        ref = self._ag_ref(step_index, raw_created) if time else self._ag_plain_ref(step_index)
        if ref is _REPLAY:
            return
        self._saw_time(time)
        if trunc:
            self._ag_flush_open()
            return self._excluded("truncated_fields", ref)
        self._unit[1] = 1 if time else 0
        self._ag_flush_open()
        self._text("user", text, time, ref)

    def _ag_planner_f3(self, rec, prose, step_index, raw_created, time):
        trunc = _ag_truncated(rec)
        has_prose = isinstance(prose, str) and bool(tc._no_lone_surrogates(prose).strip())
        thinking = rec.get("thinking")
        has_thinking = isinstance(thinking, str) and bool(tc._no_lone_surrogates(thinking).strip())
        raw_calls = rec.get("tool_calls")
        calls = [c for c in raw_calls if isinstance(c, dict)] if isinstance(raw_calls, list) else []
        if not (has_prose or has_thinking or calls or trunc):
            return
        if self.src is None:
            self.quarantined = True
            self.skipped.append(("no_session_id", None))
            return
        ref = self._ag_ref(step_index, raw_created)
        if ref is _REPLAY:
            return
        self._saw_time(time)
        self._unit[1] = 1 if (has_prose and "content" not in trunc and time) else 0
        owned = ({_ag_clean_id(c.get("id")) or (step_index + 1 + idx if _is_int(step_index, 0) else None)
                  for idx, c in enumerate(calls)} - {None}) if "tool_calls" not in trunc else ()
        p_ns = _ag_utc_ns(raw_created)
        self._ag_flush_open(owned, p_ns)
        if "thinking" in trunc:
            self._excluded("truncated_fields", ref)
        elif has_thinking:
            self._thinking(thinking, time, ref)
        if "tool_calls" in trunc:
            self._excluded("truncated_fields", ref)
        elif calls:
            id_base, seen_c = ref or self._ag_anon_base(), set()
            for idx, c in enumerate(calls):
                explicit_c_id = _ag_clean_id(c.get("id"))
                tid = explicit_c_id or ("ag:%s:%d" % (id_base, idx))
                raw_name = c.get("name")
                name = tc._no_lone_surrogates(raw_name).strip() if isinstance(raw_name, str) else None
                args = c["args"] if "args" in c else {}
                tref = _hex32(tid)
                dup = tref in seen_c or any(e[0] == tref for e in self.tool_pending)
                target_step = (step_index + 1 + idx if _is_int(step_index, 0) else None) if explicit_c_id is None else 0
                ekey = explicit_c_id if explicit_c_id is not None else target_step
                if tref in seen_c and not any(e[0] == tref for e in self.tool_pending):
                    self.skipped.append(("tool_duplicate_call", ref))
                else:
                    self._tool_call(tid, name, _tool_text(args), time, ref)
                seen_c.add(tref)
                if not dup:
                    w = self._ag_early_pop(ekey)
                    if w is not None:
                        if w[2] is None:
                            self.flush_pending(everything=True, only=tref)
                        else:
                            self._tool_result(tid, w[2], w[3], tc.utc_instant(w[4]) or None, w[5])
                    elif any(e[0] == tref for e in self.tool_pending):
                        self._ag_step_push(target_step, tid, p_ns)
        if "content" in trunc:
            self._excluded("truncated_fields", ref)
        elif has_prose:
            self._text("assistant", prose, time, ref)

    def _ag_other_f3(self, rec, step_index, source, rtype, raw_created, time):
        if source in ("SYSTEM", "SYSTEM_SDK") or rtype in (_AG_COMPACT_TYPES + _AG_META_TYPES):
            self._saw_time(time)
            cls = "compact_summary" if rtype in _AG_COMPACT_TYPES else "is_meta"
            return self._excluded(cls, self._ag_plain_ref(step_index))
        if source != "MODEL" or rtype not in _AG_TOOL_RESULTS:
            return
        if self.src is None:
            self.quarantined = True
            self.skipped.append(("no_session_id", None))
            return
        explicit_id = _ag_clean_id(rec.get("tool_call_id"))
        ref = self._ag_ref(step_index, raw_created, res=True, res_id=explicit_id)
        if ref is _REPLAY:
            return
        self._saw_time(time)
        tid = self._ag_step_pop(step_index, explicit_id)
        r_ns = _ag_utc_ns(raw_created)
        in_early = explicit_id is not None and any(type(w[1]) is str and w[1] == explicit_id
                                                   for w in self.f3_dialect.get("early", ()))
        ekey = ((explicit_id if not in_early and not any(e[0] == _hex32(explicit_id) for e in self.tool_seen) else None)
                if explicit_id is not None else (step_index if _is_int(step_index, 1) else None))
        if "content" in _ag_truncated(rec):
            self._excluded("truncated_fields", ref)
            if tid:
                self.flush_pending(everything=True, only=_hex32(tid))
            elif ekey is not None:
                self._ag_early_push(ekey, None, False, r_ns, ref)
            return
        res_body = rec.get("content")
        body = res_body if res_body is not None else (rec["error"] if isinstance(rec.get("error"), str) else "")
        is_error = (rec.get("status") in ("ERROR", "INVALID") or bool(rec.get("error"))
                    or (rtype == "RUN_COMMAND" and type(rec.get("exit_code")) is int and rec.get("exit_code") != 0))
        if tid is None and in_early:
            return self.skipped.append(("tool_duplicate_result", ref))
        if tid is None and ekey is not None:
            return self._ag_early_push(ekey, _tool_text(body), is_error, r_ns, ref)
        call_id = tid or explicit_id or ("ag:orphan:%s" % (ref or self._ag_anon_base()))
        self._tool_result(call_id, _tool_text(body), is_error, time, ref)

    def _antigravity(self, rec):
        if self.quarantined:
            return
        if self.src is None:
            conv = rec.get("conversation_id")
            if isinstance(conv, str) and conv.strip():
                self.src = _hex32(conv.strip())
        step_index = rec.get("step_index")
        source = rec.get("source")
        rtype = rec.get("type")
        if self.ordinal == 0 and self.final is None and (step_index is None or (type(step_index) is int and step_index == 0)):
            content0 = rec.get("content")
            if source == "SYSTEM" and isinstance(content0, str) and "sender=" in content0:
                self.subagent = True
        raw_created = rec.get("created_at")
        time = tc.utc_instant(raw_created)
        if source == "USER_EXPLICIT" and rtype == "USER_INPUT":
            text = tc._no_lone_surrogates(antigravity_dialect.extract_user_text(rec.get("content")))
            if self.f3 and not self.subagent:
                return self._ag_user_f3(rec, text, step_index, raw_created, time)
            if not text.strip():
                return
            if text.lstrip().startswith("<CONTEXT_SUMMARY>"):
                return
            if self.drop_machine_typed and tc._MACHINE_TYPED.search(text):
                return
            if self.src is None:
                self.quarantined = True
                self.skipped.append(("no_session_id", None))
                return
            if self.subagent:
                return
            ref = self._ag_ref(step_index, raw_created) if time else (
                "%s:%d" % (self.src, step_index) if _is_int(step_index, 0) else None)
            if ref is _REPLAY:
                return
            self._text("user", text, time, ref)
        elif source == "MODEL" and rtype == "PLANNER_RESPONSE":
            content = rec.get("content")
            if self.f3 and not self.subagent:
                return self._ag_planner_f3(rec, content, step_index, raw_created, time)
            if not isinstance(content, str):
                return
            if not tc._no_lone_surrogates(content).strip():
                return
            if self.src is None:
                self.quarantined = True
                self.skipped.append(("no_session_id", None))
                return
            ref = self._ag_ref(step_index, raw_created) if time else (
                "%s:%d" % (self.src, step_index) if _is_int(step_index, 0) else None)
            if ref is _REPLAY:
                return
            if self.subagent:
                self._final_candidate(content, time, ref)
            else:
                self._text("assistant", content, time, ref)
        elif self.f3 and not self.subagent:
            self._ag_other_f3(rec, step_index, source, rtype, raw_created, time)

    def _kimi(self, rec):
        if self.quarantined:
            return                                    # the first content record settled it; the checkpoint keeps it
        rtype = rec.get("type")
        if rtype == "context.append_message":
            msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
            ref = msg.get("id") if isinstance(msg.get("id"), str) and msg.get("id") else None
            role = msg.get("role")
            if role != "user":
                self.skipped.append(("kimi_message_" + str(role), ref))
                return
            if self.subagent:                         # the parent's dispatch prompt is not the subagent's report
                self.skipped.append(("kimi_subagent_prompt", ref))
                return
            origin = msg.get("origin") if isinstance(msg.get("origin"), dict) else {}
            kind = origin.get("kind")                 # missing: the older wire format, still typed (kimi_dialect)
            if kind != "user" and kind is not None and self.drop_machine_typed:
                # a machine rendering in a user-role record: injections, cron and system triggers, task text
                if kind == "injection":
                    cls = "kimi_injection_" + str(origin.get("variant"))
                elif kind in ("system_trigger", "cron_job"):
                    cls = "kimi_" + kind
                else:
                    cls = "kimi_origin_" + str(kind)
                self.skipped.append((cls, ref))
                return
            content = msg.get("content")
            raw_blocks = content if isinstance(content, list) else [content]
            blocks = [c.get("text") if isinstance(c, dict) else c for c in raw_blocks
                      if isinstance(c, str)
                      or (isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str))]
            kept = []
            for text in blocks:                       # one item per text block, in order
                if text.lstrip().startswith("<system-reminder"):
                    continue                          # a harness reminder part is machine text (kimi_dialect)
                if self.drop_machine_typed and tc._KIMI_MACHINE_TYPED.search(text):
                    self.skipped.append(("kimi_machine_typed", ref))
                    continue
                kept.append(text)
            if not any(tc._no_lone_surrogates(t).strip() for t in kept):
                return                                # an empty or attachment-only message is not dialogue
            if self.src is None:
                self.quarantined = True               # never assign an identity the path did not give
                self.skipped.append(("no_session_id", None))
                return
            time = kimi_dialect._time(rec.get("time"))
            if self.f3 and not self.subagent:
                self._saw_time(time)
            for text in kept:
                self._text("user", text, time, ref)
            return
        if rtype == "context.append_loop_event":
            ev = rec.get("event") if isinstance(rec.get("event"), dict) else {}
            ref = ev.get("uuid") if isinstance(ev.get("uuid"), str) and ev.get("uuid") else None
            part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
            ptype = part.get("type")
            etype = ev.get("type")
            if self.f3 and not self.subagent and etype in ("tool.call", "tool.result"):
                tool_id = ev.get("toolCallId") if isinstance(ev.get("toolCallId"), str) and ev.get("toolCallId") else None
                if tool_id is None:
                    self.skipped.append(("kimi_loop_event", ref))
                    return
                if self.src is None:
                    self.quarantined = True
                    self.skipped.append(("no_session_id", None))
                    return
                time = kimi_dialect._time(rec.get("time"))
                if self.f3 and not self.subagent:
                    self._saw_time(time)
                if etype == "tool.call":
                    return self._tool_call(tool_id, ev.get("name"), _tool_text(ev.get("args")), time, ref)
                result = ev.get("result") if isinstance(ev.get("result"), dict) else {}
                result_ref = ev.get("parentUuid") if isinstance(ev.get("parentUuid"), str) and ev.get("parentUuid") else tool_id
                return self._tool_result(tool_id, _tool_text(result.get("output")), result.get("isError") is True,
                                         time, result_ref)
            if self.f3 and not self.subagent and etype == "content.part" and ptype == "think":
                thought = part.get("think")
                if isinstance(thought, str) and thought.strip():
                    if self.src is None:
                        self.quarantined = True
                        self.skipped.append(("no_session_id", None))
                        return
                    time = kimi_dialect._time(rec.get("time"))
                    if self.f3 and not self.subagent:
                        self._saw_time(time)
                    return self._thinking(thought, time, ref)
            if etype == "content.part" and ptype == "think":
                self.skipped.append(("kimi_thinking", ref))
                return
            if etype != "content.part" or ptype != "text" or not isinstance(part.get("text"), str):
                # step.begin/step.end, non-F3 tool.call/tool.result, image parts, unknown events
                self.skipped.append(("kimi_loop_event", ref))
                return
            text = part["text"]
            if not tc._no_lone_surrogates(text).strip():
                return                                # an empty text part is not dialogue
            if self.src is None:
                self.quarantined = True
                self.skipped.append(("no_session_id", None))
                return
            time = kimi_dialect._time(rec.get("time"))
            if self.f3 and not self.subagent:
                self._saw_time(time)
            if self.subagent:                         # a kimi subagent wire: only its last text part matters
                self._final_candidate(text, time, ref)
            else:
                self._text("assistant", text, time, ref)
            return
        if rtype == "context.apply_compaction":
            # The compacted summary is model-written and near-duplicates earlier dialogue (step 9b).
            if self.f3 and not self.subagent:
                return self._excluded("compact_summary", None)
            self.skipped.append(("kimi_compaction", None))
            return
        if rtype == "context.undo":
            # Reported, but nothing is retracted: the wire is append-only, the v6 checkpoint holds no
            # pending-message buffer, and an item already frozen cannot be rewritten (see ASSUMPTIONS).
            self.skipped.append(("kimi_undo", None))
            return
        if rtype == "context.clear":
            self.skipped.append(("kimi_clear", None))
            return
        # Everything else is harness bookkeeping: metadata, profile.bind, llm.request, usage.record,
        # token_counting.*, turn.* (turn.prompt repeats the append_message text, turn.steer the steered
        # message), agent.message.appended (the loop's copy of the same message), goal.*, cron.*, task.*,
        # agent.*, subagent.*, file_history.*, prompt.*, permission.*, interaction.*, swarm_mode.*,
        # plan_mode.*, mcp.*, config.update, full_compaction.*, and any type not yet known.
        self.skipped.append(("kimi_bookkeeping", None))

    # ---- the artifact of a subagent source (kxxx) ---------------------------------------------------

    def _final_candidate(self, text, time, native_ref):
        """Remember the last non-empty assistant text block of a subagent source. Only its verdict, size and, when
        the WHOLE artifact value fits the 65,536 byte item budget, its text are kept. That bounds the artifact, not
        the checkpoint: the checkpoint's own JSON encoding can escape the text again (see the module header)."""
        text = tc._no_lone_surrogates(text)
        if not text.strip():
            return
        if not time:
            self.skipped.append(("no_time", native_ref))
            return
        size = len(text.encode("utf-8"))
        reason = self._screen(text)
        ref = native_ref if isinstance(native_ref, str) else None
        fits = size <= MAX_VALUE_BYTES
        if fits and not reason and self.src is not None:
            # the real envelope, so the checkpoint never holds a text finish() would have to refuse
            probe = self._base("artifact", time, ref)
            probe["text"], probe["text_sha256"], probe["agent_id"] = text, _sha(text), self.src
            fits = len(serialize(probe)) <= MAX_VALUE_BYTES
        self.final = {"time": time, "ref": ref, "bytes": size, "reason": reason,
                      "text": None if reason or not fits else text}

    def finish(self):
        """Close the source. A subagent source then yields ONE artifact item: its final report, whole and screened
        like any text. A source with no final text yields none and says so under skipped. Others yield nothing."""
        if not self.subagent or self.finished:
            return []
        self.finished = True
        before, final = len(self.items), self.final
        if final is None:
            self.skipped.append(("no_final_text", None))
        elif final["reason"]:
            self._omit(final["time"], final["ref"], final["reason"])
        elif final["text"] is None:
            self._omit(final["time"], final["ref"], "too_large", final["bytes"])
        else:
            value = self._base("artifact", final["time"], final["ref"])
            value["text"], value["text_sha256"], value["agent_id"] = final["text"], _sha(final["text"]), self.src
            if len(serialize(value)) > MAX_VALUE_BYTES:
                self._omit(final["time"], final["ref"], "too_large", final["bytes"])
            else:
                self._push(value)
        return self.items[before:]

    # ---- emission -------------------------------------------------------------------------------------------

    def _base(self, item, time, native_ref, role=None):
        if self.src is None:
            raise ValueError("a record arrived before the source had a session id")
        ordinal = self.ordinal
        value = {"schema": SCHEMA, "kind": "item", "seat": self.seat, "src": self.src, "gen": self.gen,
                 "batch": self.batch, "ordinal": ordinal, "item": item}
        if role:
            value["role"] = role
        value.update({"dialect": self.dialect, "adapter_version": CONTEXT_ADAPTER_VERSION, "time": time})
        if isinstance(native_ref, str) and native_ref:
            value["native_ref"] = _hex32(native_ref)
        return value

    def _screen(self, *texts):
        if not self.screens:
            return None                          # shared/transcript-context: kept as written, see the docstring
        for text in texts:
            if tc.detect(text):
                return "secret"
        for text in texts:
            if self.names.hits(text):
                return "client_name"
        return None

    def _omit(self, time, native_ref, reason, nbytes=None):
        value = self._base("omitted", time, native_ref)
        value["omitted_reason"] = reason
        if nbytes is not None:
            value["omitted_bytes"] = nbytes
        self._push(value)

    def _push(self, value):
        self.items.append(value)
        self.ordinal += 1
        self.batch_count += 1
        if self.batch_count >= self.batch_items:
            self.close_batch()

    def close_batch(self):
        """End the current batch: the next item opens the next one. Batches are the uploader's cut (a local atomic
        commit), so the caller decides where; a batch also closes by itself at batch_items items. An empty batch is
        never closed, so batch numbers have no hole."""
        if self.batch_count:
            self.batch += 1
            self.batch_count = 0

    def _text(self, role, text, time, native_ref, kind=None):
        text = tc._no_lone_surrogates(text)
        if not text.strip():
            return
        if not time:
            self.skipped.append(("no_time", native_ref))
            return
        reason = self._screen(text)
        if reason:
            return self._omit(time, native_ref, reason)
        value = self._base(kind or role + "_text", time, native_ref, role)
        value["text"], value["text_sha256"] = text, _sha(text)
        if len(serialize(value)) > MAX_VALUE_BYTES:
            return self._omit(time, native_ref, "too_large", len(text.encode("utf-8")))
        self._push(value)

    # ---- F3: the five calls a dialect makes (txxxx, kxxx); every one is a no-op with F3 off ------

    def _saw_time(self, time):
        if self.f3 and isinstance(time, str) and time > self.newest_time:
            self.newest_time = time

    def _excluded(self, cls, native_ref):
        """A record F3 reports and does not keep (a class in F3_EXCLUDED)."""
        if self.f3:
            self.skipped.append((cls, native_ref))

    def _f3_room(self):
        """How many more F3 items the record being parsed may emit. The uploader holds one record's items in one batch,
        and stops at a record that produced more than a batch holds, for good (GitHub Codex on c2263808: two parallel
        results of 2 parts each at batch_items=2). So the record's F3 items together, plus a slot for each of its text
        blocks, stay within batch_items. Outside feed (flush_pending, which the uploader calls one ref at a time) each
        exchange has the whole batch."""
        if self._unit is None:
            return self.batch_items
        return self.batch_items - (len(self.items) - self._unit[0]) - self._unit[1]

    def _thinking(self, text, time, native_ref):
        if self.f3:
            if self._f3_room() < 1:
                return self.skipped.append(("record_full", native_ref))
            self._text("assistant", text, time, native_ref, kind="thinking")

    def _seen(self, tool_ref, how):
        self.tool_seen = [e for e in self.tool_seen if e[0] != tool_ref] + [[tool_ref, how]]
        del self.tool_seen[:-F3_SEEN_IDS]

    def _tool_call(self, tool_id, name, call_text, time, native_ref):
        """A call waits in the checkpoint for its result, paired by id, never by position.
        Tool names outside the server's 1..256-character string contract are anonymous (None)."""
        if not self.f3:
            return
        tool_ref = _hex32(tool_id)
        if any(e[0] == tool_ref for e in self.tool_pending):
            return self.skipped.append(("tool_duplicate_call", native_ref))
        if not isinstance(name, str) or not name or len(name) > 256:
            name = None
        self.tool_pending.append([tool_ref, None if name is None else tc._no_lone_surrogates(name),
                                  tc._no_lone_surrogates(call_text), time or "",
                                  native_ref if isinstance(native_ref, str) else None])
        while self.tool_pending and len(serialize(self.tool_pending)) > F3_PENDING_BYTES:
            self._flush_one(self.tool_pending.pop(0))         # the oldest waits no longer: result missing

    def _tool_result(self, tool_id, result_text, is_error, time, native_ref):
        if not self.f3:
            return
        tool_ref = _hex32(tool_id)
        result_text = tc._no_lone_surrogates(result_text)
        status = "error" if is_error else "ok"
        for i, e in enumerate(self.tool_pending):
            if e[0] == tool_ref:
                del self.tool_pending[i]
                self._seen(tool_ref, "p")
                return self._exchange(tool_ref, e[1], e[2], result_text, "ok", status, e[3] or time, e[4] or native_ref)
        if any(e[0] == tool_ref and e[1] == "p" for e in self.tool_seen):
            return self.skipped.append(("tool_duplicate_result", native_ref))
        # A late result (its call already went out result-missing), or one whose call is older than the window.
        self._seen(tool_ref, "p")
        self._exchange(tool_ref, None, None, result_text, "missing", status, time, native_ref)

    def _due(self, e, now, everything):
        """A waiting call is due: at least F3_FLUSH_AGE_S older than the newest record (data clock), or at least
        F3_IDLE_FLUSH_S older than `now` (the caller's clock, for a source that ended with a call open), or either
        time is unusable, or `everything`."""
        newest, called = _utc_seconds(self.newest_time), _utc_seconds(e[3])
        if everything or newest is None or called is None or newest - called >= F3_FLUSH_AGE_S:
            return True
        wall = _utc_seconds(now) if isinstance(now, str) and now else None
        return wall is not None and wall - called >= F3_IDLE_FLUSH_S

    def due_pending(self, now=None, everything=False):
        """The tool refs flush_pending would send now, in call order (the uploader flushes them one at a time, so
        it can freeze each batch a flush fills, the way it does for a record)."""
        due = [e[0] for e in self.tool_pending if self.f3 and self._due(e, now, everything)]
        return due + [w[0] for w in self.f3_dialect.get("early", ())
                      if self.f3 and self._due((None, None, None, tc.utc_instant(w[4]), None), now, everything)]

    def flush_pending(self, now=None, everything=False, only=None):
        """The uploader calls this just before it cuts a batch by age. Each due call (see _due) goes out with its
        result missing. The parser keeps no wall clock of its own: `now` (a utc_instant) is the caller's, given only
        when the file has been idle. `everything` flushes every waiting call (parse_file, at the end of a whole file);
        `only` flushes that one tool ref if it is due."""
        if not self.f3:
            return []
        before = len(self.items)
        for e in list(self.tool_pending):
            if (only is None or e[0] == only) and self._due(e, now, everything):
                self.tool_pending.remove(e)
                self._flush_one(e)
        early = self.f3_dialect.get("early")
        if early:
            for w in list(early):
                if (only is None or w[0] == only) and self._due((None, None, None, tc.utc_instant(w[4]), None), now, everything):
                    early.remove(w)
                    self._ag_emit_early(w)
            if not early:
                self.f3_dialect.pop("early", None)
        return self.items[before:]

    def _flush_one(self, e):
        self._seen(e[0], "f")
        self._ag_drop_step_ref(e[0])
        self._exchange(e[0], e[1], e[2], None, "ok", "missing", e[3], e[4])

    def _exchange(self, tool_ref, name, call_text, result_text, call_status, result_status, time, native_ref):
        """One call and its result as one item, or as ordered parts when it is too large. Both screens run on the
        WHOLE exchange before any split, so a secret across a part boundary is still caught."""
        whole = json.dumps({"tool": name, "call": call_text, "result": result_text},
                           ensure_ascii=False, separators=(",", ":"))
        if not time:
            return self.skipped.append(("no_time", native_ref))
        room = self._f3_room()
        if room < 1:                       # not even an omission fits: reported, never a stalled source
            return self.skipped.append(("record_full", native_ref))
        # The raw fields, never only `whole`: json.dumps escapes every quote inside them, so a quoted secret
        # ({"password":"..."}, KEY="...") does not match a detector in `whole` (GitHub Codex on 0a7517dc,
        # context_parser.py:888). They are the exchange's whole content, unsplit, so a secret across a part boundary
        # is caught too; screening the escaped `whole` as well caught nothing they miss (its mutant survived).
        reason = self._screen(*[t for t in (name, call_text, result_text) if t])
        if not reason:
            # A structured call or result reached here as _tool_text's JSON, which escapes every quote inside it, so a
            # quoted secret in a text block ({"password":"..."}) did not match. Its strings are screened as they were
            # (GitHub Codex on 899aca19, context_parser.py:611).
            reason = self._screen(*_json_strings(call_text), *_json_strings(result_text))
        if reason:
            return self._omit(time, native_ref, reason)
        extra = {"tool_ref": tool_ref, "tool": name, "call_status": call_status, "result_status": result_status,
                 "exchange_sha256": _sha(whole)}
        value = self._base("tool_exchange", time, native_ref)
        value.update(extra, part=1, parts=1, text=whole, text_sha256=_sha(whole))
        if len(serialize(value)) <= MAX_VALUE_BYTES:
            return self._push(value)
        probe = dict(value, ordinal=self.ordinal + F3_MAX_PARTS, batch=self.batch + F3_MAX_PARTS,
                     part=F3_MAX_PARTS, parts=F3_MAX_PARTS, text="", text_sha256="0" * 64)
        texts = _split_text(whole, MAX_VALUE_BYTES - len(serialize(probe)))
        # One record's parts must fit one batch, or the uploader stops at that record for good ("one record produced
        # more items than a batch holds"); batch_items can be configured below F3_MAX_PARTS (GitHub Codex on a7a9ed7a),
        # and the record's other exchanges share the batch with these parts (Codex on c2263808): `room`.
        if texts is None or len(texts) > min(F3_MAX_PARTS, room):
            return self._omit(time, native_ref, "too_large", len(whole.encode("utf-8")))
        for n, text in enumerate(texts, 1):
            value = self._base("tool_exchange", time, native_ref)
            value.update(extra, part=n, parts=len(texts), text=text, text_sha256=_sha(text))
            if len(serialize(value)) > MAX_VALUE_BYTES:            # the budget above makes this impossible
                raise ValueError("a tool_exchange part exceeds the item size")
            self._push(value)

    def _task_item(self, event, step, time, native_ref, counts=None):
        text = "plan step %s: %s" % (event, step)
        if event == "ambiguous":
            text = ("plan step ambiguous (removed or completed): %s (pending %d, in_progress %d, completed %d)"
                    % (step, counts["pending"], counts["in_progress"], counts["completed"]))
        if len(step) > MAX_STEP_CHARS:
            return self._omit(time, native_ref, "too_large", len(step.encode("utf-8")))
        reason = self._screen(step, text)
        if reason:
            return self._omit(time, native_ref, reason)
        value = self._base("task", time, native_ref)
        value["step"], value["event"] = step, event
        if counts is not None:
            value["counts"] = counts
        value["text"], value["text_sha256"] = text, _sha(text)
        if len(serialize(value)) > MAX_VALUE_BYTES:
            return self._omit(time, native_ref, "too_large", len(step.encode("utf-8")))
        self._push(value)

    # ---- the update_plan rule (b2d1a0) ----------------------------------------------------------------------

    @staticmethod
    def _step_key(step):
        """Keys are tagged: "t:" + the text, or "h:" + sha256 for a text over the step limit, so a short step
        can never collide with a hashed long one."""
        step = tc._no_lone_surrogates(step)
        return "t:" + step if len(step) <= MAX_STEP_CHARS else "h:" + _sha(step)

    def _f1_reserve(self, kind, content, time):
        """The F1 items a Claude record emits after its F3 items: one per text block, and one per event of each
        TodoWrite plan in it, diffed in order from the current plan without changing it (txxxx, GitHub Codex on
        56d830a8: a thinking block and a TodoWrite adding two tasks made three items at batch_items=2)."""
        if isinstance(content, str):
            return 1
        blocks = [b for b in (content if isinstance(content, list) else []) if isinstance(b, dict)]
        reserve = sum(b.get("type") == "text" for b in blocks)
        if kind != "assistant" or not time:
            return reserve
        plan = self.plan
        for b in blocks:
            if b.get("type") == "tool_use" and b.get("name") == "TodoWrite":
                todos = b.get("input").get("todos") if isinstance(b.get("input"), dict) else None
                if isinstance(todos, list):
                    plan, events = self._plan_diff(plan, [(t.get("content"), t.get("status")) for t in todos
                                                          if isinstance(t, dict)])
                    reserve += len(events)
        return reserve

    def _plan_diff(self, plan, steps):
        """(the new plan, its events against `plan`): pure, so _f1_reserve can count what _plan will emit."""
        new = {}
        for step, status in steps:
            if not isinstance(step, str) or not step.strip():
                continue
            counts = new.setdefault(self._step_key(step), [0, 0, 0])
            counts[{"in_progress": 1, "completed": 2}.get(status if isinstance(status, str) else "", 0)] += 1
        if len(new) > MAX_PLAN_STEPS:
            raise PlanStateFull("a plan holds more than %d distinct step texts" % MAX_PLAN_STEPS)
        events = []                  # (event, key, counts)
        for key, before in plan.items():
            after = new.get(key, [0, 0, 0])
            tb, ta = sum(before), sum(after)
            step = key
            if ta < tb:
                if after[2] > before[2]:
                    events.append(("ambiguous", key, {"pending": after[0], "in_progress": after[1],
                                                       "completed": after[2]}))
                else:
                    events += [("removed", key, None)] * (tb - ta)
                continue
            events += [("completed", key, None)] * max(0, after[2] - before[2])
            events += [("added", key, None)] * (ta - tb)
        for key, after in new.items():
            if key not in plan:
                events += [("added", key, None)] * sum(after)
        return new, events

    def _plan(self, steps, time, native_ref):
        for step, _status in steps:
            if not isinstance(step, str) or not step.strip():
                continue
            key = self._step_key(step)
            if key.startswith("h:") and key not in self.oversize:
                # an oversize step is screened now, whole, and only its verdict and size are remembered
                clean = tc._no_lone_surrogates(step)
                self.oversize[key] = {"bytes": len(clean.encode("utf-8")), "reason": self._screen(clean)}
        new, events = self._plan_diff(self.plan, steps)
        if not time:
            self.skipped.append(("no_time", native_ref))
            self.plan = new
            return
        self.plan = new
        for event, key, counts in events:
            if key.startswith("h:"):
                big = self.oversize[key]
                if big["reason"]:
                    self._omit(time, native_ref, big["reason"])
                else:
                    self._omit(time, native_ref, "too_large", big["bytes"])
            else:
                self._task_item(event, key[2:], time, native_ref, counts)
        self.oversize = {k: v for k, v in self.oversize.items() if k in new}


# ---- command line: one transcript file in, frozen items and manifests out ---------------------------------------

def parse_file(path, seat, dialect, denylist=(), batch_items=DEFAULT_BATCH_ITEMS, cut_after_lines=(), namespace=None,
               session_id=None, f3=False):
    """Parse a whole JSONL transcript. Returns (parser, report). A line that is not JSON is counted, not guessed at."""
    read_path = preferred_antigravity_path(path) if dialect == "antigravity" else path
    parser = ContextParser(seat, dialect, denylist, batch_items, namespace=namespace, session_id=session_id,
                           source_path=path, f3=f3)
    raw = open(read_path, "rb").read()
    bad = 0
    records = 0
    cuts = set(cut_after_lines)

    def whole(step):
        """Run `step` on the parser; if it left a split exchange's parts in two batches, roll back, close the batch
        and run it again, as the uploader does (GitHub Codex on ddc306c1, context_parser.py:964): the server checks a
        split exchange whole within one batch. F1 output never takes this path."""
        nonlocal parser
        if not parser.f3:
            return step(parser)
        state, n_items, n_skipped = parser.state(), len(parser.items), len(parser.skipped)
        step(parser)
        spans = {}
        for item in parser.items[n_items:]:
            if item["item"] == "tool_exchange" and item.get("parts", 1) > 1:
                spans.setdefault(item["exchange_sha256"], set()).add(item["batch"])
        if any(len(batches) > 1 for batches in spans.values()):
            items, skipped = parser.items[:n_items], parser.skipped[:n_skipped]
            parser = ContextParser.from_state(state, denylist, namespace=namespace)
            parser.items, parser.skipped = items, skipped
            parser.close_batch()
            step(parser)

    for number, line in enumerate(raw.decode("utf-8", "replace").splitlines()):
        if number - 1 in cuts:                       # the cut comes after line number-1 was parsed
            parser.close_batch()
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            bad += 1
            continue
        records += 1
        whole(lambda p, r=record: p.feed(r))
    for ref in parser.due_pending(everything=True):  # end of the whole file: nothing more can pair, one at a time
        whole(lambda p, r=ref: p.flush_pending(everything=True, only=r))
    parser.finish()
    kinds = {}
    for item in parser.items:
        kind = item["item"] if item["item"] != "omitted" else "omitted_" + item["omitted_reason"]
        kinds[kind] = kinds.get(kind, 0) + 1
    report = {"adapter_version": CONTEXT_ADAPTER_VERSION, "parser_sha256": hashlib.sha256(
        open(__file__, "rb").read()).hexdigest(), "input_sha256": hashlib.sha256(raw).hexdigest(),
        "input_bytes": len(raw), "dialect": dialect, "seat": seat, "records": records, "unreadable_lines": bad,
        "items": len(parser.items), "by_kind": dict(sorted(kinds.items())),
        "skipped": [list(s) for s in parser.skipped], "batch_items": batch_items,
        "client_name_list_sha256": parser.names.digest(), "cut_after_lines": sorted(cuts),
        "namespace": namespace, "screens": parser.screens, "f3": f3,
        "assumptions": list(ASSUMPTIONS)}
    return parser, report


def main(argv=None):
    ap = argparse.ArgumentParser(description="context.v1 items and manifests from one transcript file (local only)")
    ap.add_argument("source")
    ap.add_argument("--seat", required=True)
    ap.add_argument("--dialect", required=True, choices=DIALECTS)
    ap.add_argument("--denylist", help="JSON file: a list of client-name markers")
    ap.add_argument("--batch-items", type=int, default=DEFAULT_BATCH_ITEMS)
    ap.add_argument("--cut-after-line", type=int, action="append", default=[],
                    help="close the batch after this 0-based line of the file (repeat for several cuts)")
    ap.add_argument("--namespace", help="the context namespace the items are for; only shared/transcript-context "
                    "turns the secret and client-name screens off")
    ap.add_argument("--session-id", default=None, help="explicit session/conversation id for antigravity or kimi "
                    "transcripts (kimi default: derive it from the wire path)")
    ap.add_argument("--f3", action="store_true", help="also tool_exchange and thinking items (ingest plan D1; off "
                    "until the server accepts them, step 2b and step 6b)")
    ap.add_argument("--out", required=True, help="a new directory")
    args = ap.parse_args(argv)
    denylist = json.load(open(args.denylist, encoding="utf-8")) if args.denylist else []
    os.makedirs(args.out, exist_ok=False)
    parser, report = parse_file(args.source, args.seat, args.dialect, denylist, args.batch_items, args.cut_after_line,
                                session_id=args.session_id, namespace=args.namespace, f3=args.f3)
    with open(os.path.join(args.out, "items.jsonl"), "wb") as f:
        for item in parser.items:
            f.write(json.dumps({"key": item_key(item), "value": item}, ensure_ascii=False,
                               separators=(",", ":")).encode("utf-8") + b"\n")
    with open(os.path.join(args.out, "manifests.jsonl"), "wb") as f:
        for m in manifests(parser.items, args.batch_items):
            head = {"seat": m["seat"], "src": m["src"], "gen": m["gen"], "batch": m["batch"]}
            f.write(json.dumps({"key": "ctxm/%s/%s/%04d/%06d" % (head["seat"], head["src"], head["gen"], head["batch"]),
                                "value": m}, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
    with open(os.path.join(args.out, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(json.dumps({k: report[k] for k in ("adapter_version", "input_sha256", "items", "by_kind")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
