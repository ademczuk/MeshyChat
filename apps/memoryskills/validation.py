"""Pure validation + the vocabularies the six memory.* skills share.

Nothing in this module touches a database, so the handler layer can be read without
re-reading the vocabularies.

`apps.crypto.primitives.jcs` supplies RFC-8785 canonicalization because
`payload_jcs_sha256` is the column the SIGNED envelope is hashed into. `json.dumps` with
`sort_keys=True` and RFC 8785 agree on ASCII object keys and disagree on non-BMP/composed
ones. For the payloads the fleet writes today the two produce identical bytes; where they
differ, the primitive the server verifies with wins.
"""
from __future__ import annotations

import base64
import hashlib
import datetime as _dt
import json
import os
import re
import uuid as _uuid
from datetime import datetime, timezone

from apps.crypto.ijson import MAX_SAFE_INTEGER

from .errors import HandlerError

# --------------------------------------------------------------------------- #
# Vocabularies
# --------------------------------------------------------------------------- #

# Ordered permission lattice: read < write < admin.
PERM_LEVEL = {"read": 1, "write": 2, "admin": 3}

# The spelling is UNDERSCORED to match the live
# multiword value DURABLE_PATTERN; the CHECK constraint on shared_memory.status
# uses the same spelling (verified against pg_constraint:
# shared_memory_status_check lists exactly these ten).
LIVE_STATUSES = {"OPEN", "PARTIAL", "CONFIRMED", "REFUTED", "MITIGATED", "DURABLE_PATTERN"}
STATUS_PROPOSED_FIX = "PROPOSED_FIX"
STATUS_FIXED = "FIXED"
STATUS_PRE_DISCIPLINE = "PRE_DISCIPLINE"
STATUS_WONTFIX = "WONTFIX"
DISCIPLINE_STATUSES = {STATUS_PROPOSED_FIX, STATUS_FIXED, STATUS_PRE_DISCIPLINE, STATUS_WONTFIX}
ALL_STATUSES = LIVE_STATUSES | DISCIPLINE_STATUSES

# The same three values as the live CHECK constraint
# shared_memory_namespace_retention_policy_chk.
RETENTION_KEEP_ALL = "keep_all"
RETENTION_HIGH_CHURN = "high_churn"
RETENTION_EPHEMERAL = "ephemeral"
RETENTION_POLICIES = {RETENTION_KEEP_ALL, RETENTION_HIGH_CHURN, RETENTION_EPHEMERAL}

NEW_KEY_FORBIDDEN_STATUSES = {STATUS_PROPOSED_FIX, STATUS_FIXED}
CLOSURE_STATUSES = {STATUS_FIXED, STATUS_WONTFIX}

DEFAULT_LIMIT = 50
MAX_LIMIT = 500

FIELDS_MODES = ("full", "abstract", "structure")
ABSTRACT_CONTENT_FIELDS = ("body_md", "body", "text", "content")
ABSTRACT_FALLBACK_CHARS = 280

# --------------------------------------------------------------------------- #
# Mesh-chat key + message shape
# --------------------------------------------------------------------------- #

MIN_EPOCH_MS = 1_000_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000  # 2100-01-01T00:00:00Z

MESH_CHAT_NAMESPACE = "shared/mesh-chat"
MESH_MESSAGE_V1 = 1
MESH_MESSAGE_V1_REQUIRED_FIELDS = {
    "schema_version", "to", "from", "type", "thread_id", "ts", "body", "author_family",
}
MESH_MESSAGE_V1_OPTIONAL_FIELDS = {
    "cc", "requires_ack", "in_reply_to", "session_label", "subject", "ref", "done_when", "declines",
    "closes", "attachments", "operator_words", "directive", "urgent", "urgent_declined", "window_end",
}
#: The operator's voices (threat_model.md, amendment of 28 Oct 2026): the only peers whose message may carry
#: `operator_words`, the operator's own words relayed for him. Any other writer is refused, so the field cannot be
#: forged, and an edit cannot add it (an edit changes the body only).
OPERATOR_VOICE_PEERS = frozenset({
    "0447bf92-25fb-5230-b531-5a0d9c00d8c7",     # memory-bxxxxx (HQB)
    "610170d5-09fb-5385-9ea4-fc3df696b431",     # oxxxxxx (HZ)
    # kxxx (HQD): a recorded decision: "make kxxx a voice too"
    # (threat_model.md, amendment of 7 Nov 2026, the voice bullet). Keep equal to tools/meshy_node/node.OPERATOR_VOICES.
    "a2b5be8f-aa4f-5634-8b28-97de57204a14",
    # ixxxxxx (the voice relay): a recorded decision about (time withheld): "make
    # ixxxxxx a voice" (threat_model.md, amendment of 15 Nov 2026). Its words reach it as speech, not as
    # text typed in a window: see that amendment's residual risk.
    "d0ebe226-1a66-5089-9cf3-affdcaab9e53",
})
OPERATOR_WORDS_MAX_LINES = 20
OPERATOR_WORDS_MAX_CHARS = 4000
#: the directive rule (approvals, a recorded decision: "Any node, no human step"; "meshychat should allow for one node
#: to steer another, regardless of kimi, codex or claude"): the seats whose message may carry a `directive`, one seat's
#: instruction to ONE other seat that the reader treats as the operator typing in its own window
#: (threat_model.md, amendment of 2 Nov 2026). Pinned by PEER ID, read from GET /v1/mesh/recipients on 2 Nov 2026, never
#: by label. Adding a seat is a reviewed change here and in tools/meshy_node/node.SEAT_DIRECTIVE_PEERS.
SEAT_DIRECTIVE_PEERS = frozenset({
    "a2b5be8f-aa4f-5634-8b28-97de57204a14",     # kxxx (HQD, Claude)
    "5b23b084-b86b-5400-9db9-0abf5af9fbce",     # codex-kxxx-cli (HQD, Codex)
    "717734fb-b4a3-5125-95a0-e21bbb0f7e8c",     # pxx (Claude)
    "81f62ebb-b2c5-5325-a9b1-a76822cd1b71",     # codex-pxx-cli (Codex)
    "610170d5-09fb-5385-9ea4-fc3df696b431",     # oxxxxxx (HZ, Claude)
    "5a2ce360-6f34-5481-aa14-4bb2743d3214",     # codex-oxxxxxx-admin-cli (HZ, Codex)
    "0447bf92-25fb-5230-b531-5a0d9c00d8c7",     # memory-bxxxxx (HQB, Claude)
    "8fd74872-9d50-5932-9cac-106c49087a39",     # codex-bxxxxx-cli (HQB, Codex)
    "6b22cef7-ca52-5546-91ed-b4b3f3fcf516",     # nxxxxx (HQB)
    "2d6d1628-bdc0-5034-a975-e0ffe6e2e9c4",     # txxxx (HQB)
    "f123e60d-ed49-5131-8ac9-30f21d72f960",     # uxxx (HQB)
    "87f38c76-f096-5693-828c-e1bb5021cc83",     # dxxxxx (HQB)
    "ef973ad0-24d1-5403-b666-0b4fd049b2f7",     # antigravity (HQB)
    "9b9956f6-2a96-5362-822b-3e9f6cff830a",     # vxxxxxxxxxx (HQB)
    "2ea9214c-7fd2-5764-8e85-4ddd1ec94d07",     # codex-oxxxxxx-cli (HZ, Codex builder; oxxxxxx)
})
DIRECTIVE_KEYS = frozenset({"v", "words", "target", "target_peer", "expires_at"})
DIRECTIVE_MAX_LINES = 20
DIRECTIVE_MAX_CHARS = 4000
#: a recorded decision (the directive rule Q6): "24 hours, yes if unexpired".
DIRECTIVE_MAX_LIFE_S = 86400
#: A clock-skew allowance on the far bound only; the near bound (expired) is never stretched.
DIRECTIVE_SKEW_S = 120
#: `window_end` (kxxx, yes from oxxxxxx under a recorded decision, Tue 10 Nov 2026): the end of the window
#: a directive's or relay's words are for (a lab stop, a meeting, a deploy slot). A directive may not outlive it by more
#: than this margin; a relay that names one is read as expired after it (tools/meshy_node/node.relay_expiry).
WINDOW_MARGIN_S = 1800
#: A directive's msg/<epoch-ms> key must be within this of the server clock at write (pxx): the rate count reads
#: a key RANGE (handlers._enforce_directive_rate, B5), so a back- or forward-dated key would escape the hourly limit.
DIRECTIVE_KEY_SKEW_MS = 600_000
DIRECTIVE_KEY_STAMP_RE = re.compile(r"[1-9][0-9]{12}", re.ASCII)
#: One looping or misled seat cannot flood every other (pxx's review of plan v1, P2-4): at most this many directives
#: per signer in any rolling hour (E_RATE_LIMITED).
DIRECTIVE_RATE_PER_HOUR = 30
#: The kill switch. "1" turns seat directives on; anything else (and unset) refuses the field from everyone
#: (E_FEATURE_OFF) and the read never marks a directive addressed. Read at call time, so a restart is not needed to
#: read a changed process environment, and the test suite can flip it.
SEAT_DIRECTIVES_ENV = "MESHY_SEAT_DIRECTIVES"


def is_roster_seat(peer_id) -> bool:
    """Whether `peer_id` is on the seat roster NOW (read at call time, so the read path sees a changed roster)."""
    return peer_id is not None and str(peer_id) in SEAT_DIRECTIVE_PEERS


def seat_directives_enabled() -> bool:
    return os.environ.get(SEAT_DIRECTIVES_ENV, "").strip() == "1"
MESH_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
MESH_SUBJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
MESH_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
# Deliberately NOT MESH_LABEL_RE: the `#<n>` suffix is the form already written by
# forking seats, and the plain label rule would refuse exactly the rows the field
# exists to disambiguate.
MESH_SESSION_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}(?:#[0-9]{1,8})?$")
MESH_TYPE_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,31}$")
MESH_THREAD_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
MESH_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
MESH_MAX_RECIPIENTS = 32
MESH_MAX_BODY_BYTES = 65_536

# `attachments` (divergence 079): the files a message cites from the file service
# (apps/files), one reference per file. The reference is what the reader verifies the
# downloaded bytes against, so every part of it is checked here, and nothing more: whether
# the file EXISTS, and who may download it, is the file service's question (its access check,
# apps/files/service.authorize_read), asked at download time with the reader's own token. A
# reference to a file that does not exist, or that this reader may not read, is stored as
# written and fails at the download with E_FILE_NOT_FOUND, exactly as a guessed id does.
MESH_MAX_ATTACHMENTS = 8
MESH_ATTACHMENT_FIELDS = frozenset({"file_id", "name", "size_bytes", "sha256"})
MESH_ATTACHMENT_NAME_MAX_CHARS = 255
#: I-JSON's bound (RFC 7493; divergence 039 refuses a larger number anywhere in a payload).
#: Far above any MESHYCHAT_MAX_FILE_BYTES; it is the bound a stored size can still be read back at.
MESH_ATTACHMENT_MAX_SIZE_BYTES = MAX_SAFE_INTEGER
MESH_SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}")


def validate_mesh_key_ts(key):
    """Reject a msg/ key whose ts segment is not an in-band epoch-ms.

    Namespace-AGNOSTIC by design: the check keys on the `msg/` key shape, not on the
    namespace, so the poison cannot be laundered through a differently-named namespace. A
    non-msg/ key passes untouched.
    """
    if not str(key).startswith("msg/"):
        return
    ts_seg = str(key)[len("msg/"):].split("-", 1)[0]
    if not (ts_seg.isdigit() and MIN_EPOCH_MS <= int(ts_seg) <= MAX_EPOCH_MS):
        raise HandlerError(
            "E_BAD_KEY_TS",
            f"mesh key {key!r} ts segment {ts_seg!r} is not an epoch-ms in "
            f"[{MIN_EPOCH_MS}, {MAX_EPOCH_MS}]; a date-style key poisons the poll cursor",
        )


def is_mesh_message(namespace, key) -> bool:
    """The rows divergence 028 governs in full (R1 to R5): shared/mesh-chat, keys under
    msg/. Every other namespace and key keeps the plain shared key-value semantics: any key,
    any author, any writer, except for the one claim `claims_mesh_sender` names."""
    return namespace == MESH_CHAT_NAMESPACE and str(key).startswith("msg/")


def claims_mesh_sender(namespace, key, value) -> bool:
    """True for a mesh message, and for a msg/ row in ANY namespace whose value names a
    sender (a non-null `from`). R2 and R3 of divergence 028 bind that claim to the caller.

    Namespace-agnostic for the reason validate_mesh_key_ts is: GET /v1/mesh/wait
    (apps/mesh/wait.py) serves the head msg/ rows of EVERY namespace
    the caller can read and reports `value->>'from'` as the sender, so a `from` forged in
    shared/wiki was served exactly like one forged in shared/mesh-chat (review of 7084e88,
    finding 1). A msg/ row outside the mesh namespace that names no sender claims nothing
    and keeps the plain shared key-value semantics: any key, any author, any writer."""
    if is_mesh_message(namespace, key):
        return True
    return (str(key).startswith("msg/") and isinstance(value, dict)
            and value.get("from") is not None)


def key_embeds_sender(key, sender) -> bool:
    """True when the msg/ key is `msg/<epoch-ms>-<sender>` or `msg/<epoch-ms>-<sender>-<tail>`.

    The first shape is the client's; the second is the pre-cutover sender's, which adds no
    suffix. The `-` after the sender is required, so `alice` does not match a key minted for
    `alicex`.

    A LABEL MAY CONTAIN '-', so a key cannot be parsed back into a sender unambiguously:
    `msg/<ms>-desktop-5e6e5b2b-4aefe3` is `desktop` with tail `5e6e5b2b-4aefe3` as well as
    `desktop-5e6e5b2b` with tail `4aefe3`. This checks the prefix a writer CLAIMED against the
    `from` it signed, which R2 binds to the caller; nothing in this backend parses a sender
    out of a key, and a reader that wants the author reads created_by_peer_id."""
    ts_and_rest = str(key)[len("msg/"):]
    _, sep, rest = ts_and_rest.partition("-")
    sender = str(sender)
    return bool(sep) and bool(sender) and (rest == sender or rest.startswith(sender + "-"))


def validate_operator_words(namespace, key, value, caller_peer_id) -> None:
    """`operator_words` (threat_model.md, amendment of 28 Oct 2026): 1 to OPERATOR_WORDS_MAX_LINES non-empty strings,
    each at most OPERATOR_WORDS_MAX_CHARS, written only by an operator voice. Absent is fine for everyone."""
    if not isinstance(value, dict) or "operator_words" not in value:
        return
    # Any namespace, any key: a writer other than a voice never gets operator_words stored (threat_model.md, Critical;
    # pxx's review of #447, F1: mesh-chat grants cover the whole namespace, and other namespaces take any value).
    if str(caller_peer_id) not in OPERATOR_VOICE_PEERS:
        raise HandlerError("E_PERMISSION_DENIED", "only an operator voice may send operator_words")
    if namespace != MESH_CHAT_NAMESPACE or not str(key).startswith("msg/"):
        raise HandlerError("E_INVALID_ARGS", "operator_words belongs on a shared/mesh-chat msg/ key only")
    words = value["operator_words"]
    if (not isinstance(words, list) or not 1 <= len(words) <= OPERATOR_WORDS_MAX_LINES
            or not all(isinstance(w, str) and w.strip() and len(w) <= OPERATOR_WORDS_MAX_CHARS for w in words)):
        _mesh_schema_error(f"operator_words is a list of 1 to {OPERATOR_WORDS_MAX_LINES} non-empty strings of at most "
                           f"{OPERATOR_WORDS_MAX_CHARS} characters")


def _parse_utc_z(text):
    """An ISO 8601 instant ending in Z, else None. Z ONLY, the spelling the reader accepts (wire.directive_problem):
    a "+00:00" the server stored would be refused by every target as malformed, with no error to the sender
    (GitHub Codex on #490, wire.py:245; pxx). A naive time or any offset is refused at write."""
    if not isinstance(text, str) or not text.endswith("Z") or len(text) > 40:
        return None
    try:
        when = _dt.datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError:
        return None
    if when.tzinfo is None or when.utcoffset() != _dt.timedelta(0):
        return None
    return when


def check_directive_key_time(key, now=None) -> None:
    """A NEW directive's msg/<epoch-ms> key must be within DIRECTIVE_KEY_SKEW_MS of the server clock (strictly, to
    match the rate count's half-open range), so the count's key range sees every directive of the last hour
    (pxx). Admission only: handle_write runs it AFTER the replay dedup, so an identical replay of a stored
    directive, however late, still returns its stored write (pxx)."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    stamp = str(key)[len("msg/"):].split("-", 1)[0]
    # CANONICAL only: 13 ASCII digits, no leading zero (every epoch-ms from 2001 to 2286). The rate count compares
    # keys as TEXT, so a leading zero or non-ASCII digits (which isdigit() and int() accept) would sort outside the
    # counted range while reading as the current time here (pxx, GitHub Codex on 8f5b4813).
    if (not DIRECTIVE_KEY_STAMP_RE.fullmatch(stamp)
            or not abs(int(stamp) - int(now.timestamp() * 1000)) < DIRECTIVE_KEY_SKEW_MS):
        raise HandlerError("E_INVALID_ARGS", f"a directive's msg/ key must carry the current time (within "
                                             f"{DIRECTIVE_KEY_SKEW_MS // 60000} min of the server clock)")


def check_directive_not_expired(value, now=None) -> None:
    """A NEW directive's expires_at is in the future. Admission only, after handle_write's replay return."""
    when = _parse_utc_z((value.get("directive") or {}).get("expires_at") if isinstance(value, dict) else None)
    now = now or _dt.datetime.now(_dt.timezone.utc)
    if when is None or when <= now:
        raise HandlerError("E_INVALID_ARGS", "directive.expires_at is not in the future")


def validate_directive(namespace, key, value, caller_peer_id, now=None) -> None:
    """`directive` (the directive rule; threat_model.md, amendment of 2 Nov 2026): one roster seat's instruction to ONE other
    seat, `{"v": 1, "words": [1..20 strings], "target": "<label>", "expires_at": "<ISO UTC>"}`. The message key is its
    id (a msg/ key is written once). Absent is fine for everyone. Every refusal names the rule it broke."""
    if not isinstance(value, dict) or "directive" not in value:
        return
    if not seat_directives_enabled():
        raise HandlerError("E_FEATURE_OFF", f"seat directives are off on this server ({SEAT_DIRECTIVES_ENV} is not 1)")
    # Any namespace, any key: a writer outside the roster never gets a directive stored (as operator_words, F1 of #447).
    if str(caller_peer_id) not in SEAT_DIRECTIVE_PEERS:
        raise HandlerError("E_PERMISSION_DENIED", "only a roster seat may send a directive (SEAT_DIRECTIVE_PEERS)")
    if namespace != MESH_CHAT_NAMESPACE or not str(key).startswith("msg/"):
        raise HandlerError("E_INVALID_ARGS", "a directive belongs on a shared/mesh-chat msg/ key only")
    now = now or _dt.datetime.now(_dt.timezone.utc)
    d = value["directive"]
    if not isinstance(d, dict) or set(d) != DIRECTIVE_KEYS or d.get("v") != 1 or isinstance(d.get("v"), bool):
        _mesh_schema_error("directive is exactly {v: 1, words, target, target_peer, expires_at}")
    words = d["words"]
    if (not isinstance(words, list) or not 1 <= len(words) <= DIRECTIVE_MAX_LINES
            or not all(isinstance(w, str) and w.strip() and len(w) <= DIRECTIVE_MAX_CHARS for w in words)):
        _mesh_schema_error(f"directive.words is a list of 1 to {DIRECTIVE_MAX_LINES} non-empty strings of at most "
                           f"{DIRECTIVE_MAX_CHARS} characters")
    target = d["target"]
    raw_to = value.get("to")
    to = [x for x in raw_to if isinstance(x, str)] if isinstance(raw_to, list) else []
    if not isinstance(target, str) or not MESH_LABEL_RE.fullmatch(target):
        _mesh_schema_error("directive.target is one seat label")
    # One target, on the to line: a directive is never a cc or a broadcast, and one directive cannot run one shared
    # effect (a deploy) twice in two seats (codex-pxx-cli's pass on plan v1, P2-5). Fan-out is one message per seat.
    if target not in to:
        raise HandlerError("E_INVALID_ARGS", "directive.target must be on the message's to line")
    # The target's PEER, fixed at write: the read addresses the directive to this peer only, so moving the label to
    # another peer later cannot give one directive a second target (pxx, codex-pxx-cli F3). handle_write
    # checks, inside its transaction, that this peer holds `target` now.
    if not _is_canonical_uuid(d["target_peer"]):
        _mesh_schema_error("directive.target_peer is the target's peer id (a canonical UUID)")
    when = _parse_utc_z(d["expires_at"])
    if when is None:
        _mesh_schema_error("directive.expires_at is an ISO 8601 UTC instant (…Z)")
    # "Not in the future" is ADMISSION (check_directive_not_expired, after the replay check in handle_write): an
    # identical replay after its expiry must return its stored write (pxx). The far bound stays here: a later
    # replay only shrinks the distance, so it can never refuse one.
    if (when - now).total_seconds() > DIRECTIVE_MAX_LIFE_S + DIRECTIVE_SKEW_S:
        raise HandlerError("E_INVALID_ARGS", f"directive.expires_at is more than {DIRECTIVE_MAX_LIFE_S // 3600} h away")


def validate_window_end(namespace, key, value, now=None) -> None:
    """`window_end`: an ISO 8601 UTC instant (…Z) on a mesh message that carries a `directive` or `operator_words`, at
    most DIRECTIVE_MAX_LIFE_S ahead. With a directive, `directive.expires_at` is at most window_end + WINDOW_MARGIN_S,
    so a directive for "the lab until 12:45" stops at 13:15, not 24 h later. It is a top-level field, not a directive
    key, because every installed reader requires the directive's exact key set and would refuse a new one as malformed.
    Replay-safe: both bounds hold or fail the same way on a later replay. "Already past" is admission
    (check_window_not_past, after handle_write's replay return), as for a directive's expiry (pxx)."""
    if not isinstance(value, dict) or "window_end" not in value:
        return
    if namespace != MESH_CHAT_NAMESPACE or not str(key).startswith("msg/"):
        raise HandlerError("E_INVALID_ARGS", "window_end belongs on a shared/mesh-chat msg/ key only")
    if "directive" not in value and "operator_words" not in value:
        raise HandlerError("E_INVALID_ARGS", "window_end goes with a directive or operator_words")
    when = _parse_utc_z(value["window_end"])
    if when is None:
        _mesh_schema_error("window_end is an ISO 8601 UTC instant (…Z)")
    now = now or _dt.datetime.now(_dt.timezone.utc)
    if (when - now).total_seconds() > DIRECTIVE_MAX_LIFE_S + DIRECTIVE_SKEW_S:
        raise HandlerError("E_INVALID_ARGS", f"window_end is more than {DIRECTIVE_MAX_LIFE_S // 3600} h away")
    d = value.get("directive")
    expires = _parse_utc_z(d.get("expires_at")) if isinstance(d, dict) else None
    if expires is not None and expires > when + _dt.timedelta(seconds=WINDOW_MARGIN_S):
        raise HandlerError("E_INVALID_ARGS", f"directive.expires_at is more than {WINDOW_MARGIN_S // 60} min after "
                                             "window_end: a directive does not outlive the window it is for")


def check_window_not_past(value, now=None) -> None:
    """A NEW message's window_end + WINDOW_MARGIN_S is in the future: words for a window that has already closed are
    refused at write, not delivered dead. Admission only, after handle_write's replay return."""
    if not isinstance(value, dict) or "window_end" not in value:
        return
    when = _parse_utc_z(value.get("window_end"))
    now = now or _dt.datetime.now(_dt.timezone.utc)
    if when is None or when + _dt.timedelta(seconds=WINDOW_MARGIN_S) <= now:
        raise HandlerError("E_INVALID_ARGS", f"window_end plus {WINDOW_MARGIN_S // 60} min has already passed")
#: The urgent lane (kxxx, GO from oxxxxxx under SD 445, Tue 10 Nov 2026): a seat's message to ONE seat
#: marked `urgent: true` is delivered first. Order only, never authority. At most URGENT_RATE_PER_HOUR per signer in any
#: rolling hour (handlers._enforce_urgent); over it, the client resends the message as ordinary with
#: `urgent_declined: "rate_limited"`, the sender's own label (never checked against the count: a false one gains
#: nothing, and checking it dropped resends whenever a slot freed in between).
URGENT_RATE_PER_HOUR = 4
URGENT_DECLINED_VALUES = frozenset({"rate_limited"})
#: The switch: "1" turns the lane on; anything else refuses both fields (E_FEATURE_OFF). Read at call time. Stays OFF
#: until the coordinator says ON, after the client parts are on the seats that receive urgent mail (34bbcb point 5).
URGENT_LANE_ENV = "MESHY_URGENT_LANE"


def urgent_lane_enabled() -> bool:
    return os.environ.get(URGENT_LANE_ENV, "") == "1"


def validate_urgent(namespace, key, value) -> None:
    """`urgent` (exactly true) or `urgent_declined` (exactly "rate_limited"), never both: only on a shared/mesh-chat
    msg/ key, to exactly ONE label on the to line, and never beside a directive (which has its own lane). Absent is
    fine for everyone. The hourly allowance is admission (handlers._enforce_urgent, after the replay return)."""
    if not isinstance(value, dict) or not ("urgent" in value or "urgent_declined" in value):
        return
    if not urgent_lane_enabled():
        raise HandlerError("E_FEATURE_OFF", f"the urgent lane is off on this server ({URGENT_LANE_ENV} is not 1)")
    if namespace != MESH_CHAT_NAMESPACE or not str(key).startswith("msg/"):
        raise HandlerError("E_INVALID_ARGS", "urgent belongs on a shared/mesh-chat msg/ key only")
    if "urgent" in value and "urgent_declined" in value:
        _mesh_schema_error("a message is urgent or urgent_declined, never both")
    if "urgent" in value and value["urgent"] is not True:
        _mesh_schema_error("urgent is exactly true when present")
    if "urgent_declined" in value and (not isinstance(value["urgent_declined"], str)
                                       or value["urgent_declined"] not in URGENT_DECLINED_VALUES):
        _mesh_schema_error("urgent_declined is \"rate_limited\" when present")
    to = value.get("to")
    if not isinstance(to, list) or len(to) != 1 or not isinstance(to[0], str):
        raise HandlerError("E_INVALID_ARGS", "an urgent message goes to exactly one seat on its to line")
    if "directive" in value:
        raise HandlerError("E_INVALID_ARGS", "a directive has its own lane; it is not also urgent")


def validate_mesh_key_sender(namespace, key, value):
    """R3 of divergence 028: the sender label embedded in a mesh message's key is its
    value.from, in every row `claims_mesh_sender` names. Run after validate_mesh_message_v1,
    which has already required a v1 mesh message's `from` to be a normalized label. The live
    server accepted `msg/<ms>-desktop-5e6e5b2b-...` from a node that was not
    desktop-5e6e5b2b on 2026-09-23; nothing compared the two.

    AN UNVERSIONED MESSAGE accepted under MESHY_MESH_ACCEPT_UNVERSIONED (divergence 011,
    2026-09-27) has had no such check: its `from` may be absent or any JSON, and this compares
    its text with the key. R2 (handlers._enforce_mesh_sender) then requires `from` to be one
    of the caller's labels, which refuses an absent or non-string one."""
    if not claims_mesh_sender(namespace, key, value):
        return
    sender = value.get("from") if isinstance(value, dict) else None
    if not key_embeds_sender(key, sender):
        raise HandlerError(
            "E_KEY_LABEL_MISMATCH",
            f"mesh key {key!r} does not embed value.from {sender!r}: a message key is "
            f"msg/<epoch-ms>-{sender}[-<tail>] (divergence 028)")


def _mesh_schema_error(message):
    raise HandlerError("E_MESH_SCHEMA", message)


def _validate_mesh_label(value, field):
    if not isinstance(value, str) or not MESH_LABEL_RE.fullmatch(value):
        _mesh_schema_error(
            f"{field} must be a normalized lowercase label matching "
            "[a-z0-9][a-z0-9_.-]{0,63}"
        )


def _validate_mesh_session_label(raw):
    if not isinstance(raw, str) or not MESH_SESSION_LABEL_RE.fullmatch(raw):
        raise HandlerError(
            "E_MESH_SCHEMA",
            "session_label must be a mesh label with an optional #<n> session suffix",
        )


def _is_canonical_uuid(raw) -> bool:
    """The one spelling `str(uuid.UUID(...))` prints: lowercase, hyphenated, no braces and no
    urn: prefix. uuid.UUID itself accepts all of those, so a reference could otherwise name
    one file in several spellings and slip past the duplicate check."""
    if not isinstance(raw, str):
        return False
    try:
        return str(_uuid.UUID(raw)) == raw
    except ValueError:
        return False


def _is_plain_file_name(name) -> bool:
    """A name a reader can write in ONE directory as it stands: 1 to 255 characters, no path
    separator of either platform, no control character, and not `.` or `..`. The client's
    `fetch` writes the file under exactly this name, so a name with a separator in it would be
    a path the SENDER chose on the reader's disk."""
    return (isinstance(name, str) and 1 <= len(name) <= MESH_ATTACHMENT_NAME_MAX_CHARS
            and name not in (".", "..") and "/" not in name and "\\" not in name
            and not any(ord(char) < 32 or ord(char) == 127 for char in name))


def _validate_mesh_attachments(attachments):
    """Divergence 079. Every rule refuses with E_MESH_SCHEMA and names the entry by index;
    no message echoes a value, as no other message here does."""
    if (not isinstance(attachments, list)
            or not 1 <= len(attachments) <= MESH_MAX_ATTACHMENTS):
        _mesh_schema_error(
            f"attachments must be an array of 1 to {MESH_MAX_ATTACHMENTS} file references "
            "when present")
    for index, item in enumerate(attachments):
        where = f"attachments[{index}]"
        if not isinstance(item, dict) or set(item) != MESH_ATTACHMENT_FIELDS:
            _mesh_schema_error(
                f"{where} must be an object with exactly the keys "
                f"{', '.join(sorted(MESH_ATTACHMENT_FIELDS))}")
        if not _is_canonical_uuid(item["file_id"]):
            _mesh_schema_error(f"{where}.file_id must be a canonical lowercase UUID")
        if not _is_plain_file_name(item["name"]):
            _mesh_schema_error(
                f"{where}.name must be a plain file name of 1 to "
                f"{MESH_ATTACHMENT_NAME_MAX_CHARS} characters: no '/' or '\\', no control "
                "characters, not '.' or '..'")
        size = item["size_bytes"]
        # `type(...) is not int`, as for schema_version: a bool is an int to isinstance.
        if type(size) is not int or not 0 <= size <= MESH_ATTACHMENT_MAX_SIZE_BYTES:
            _mesh_schema_error(
                f"{where}.size_bytes must be a non-negative integer of at most 2^53-1")
        sha256 = item["sha256"]
        if not isinstance(sha256, str) or not MESH_SHA256_HEX_RE.fullmatch(sha256):
            _mesh_schema_error(f"{where}.sha256 must be exactly 64 lowercase hex characters")
    file_ids = [item["file_id"] for item in attachments]
    if len(set(file_ids)) != len(file_ids):
        _mesh_schema_error("attachments must not contain duplicate file_id values")


def validate_mesh_message_v1(namespace, key, value, *, accept_unversioned: bool = False) -> bool:
    """Validate a mesh message v1 before any DB work. schema_version is REQUIRED, unless
    `accept_unversioned` is the bool True (below). Returns True exactly when it accepted an
    unversioned message that way, so the caller can count it; False otherwise.

    A value with no schema_version must not be stored unchecked and then read like one that
    passed. Letting that through is how the production namespace came to hold four spellings
    of thread and a write-only reply_to. Measured on the 300 most recent production rows on
    2026-09-21, 215 carried no schema_version and 85 did. A migration that is optional at the
    write seam is finished by nobody, so this backend requires the version at the write seam.
    Divergence 011.

    THE TRANSITIONAL EXCEPTION (divergence 011, dated paragraph 2026-09-27). The fleet keeps
    its pre-cutover client until cutover, and it sends no schema_version, so memory.write
    passes `accept_unversioned` from MESHY_MESH_ACCEPT_UNVERSIONED (off by default). With it
    True, an OBJECT with NO schema_version key returns early, and nothing else changes: a
    value that declares schema_version is validated in full, a non-object is refused, and the
    key and authorship rules run after this as before. The board's door
    (apps/board/declare.py) never passes it.
    """
    if namespace != MESH_CHAT_NAMESPACE or not str(key).startswith("msg/"):
        return False
    if not isinstance(value, dict):
        _mesh_schema_error("mesh message value must be an object")
    if "schema_version" not in value:
        if accept_unversioned is True:
            return True
        _mesh_schema_error(
            "a shared/mesh-chat message must declare schema_version 1 (required fields: "
            "to, from, type, thread_id, ts, body, author_family). A row without it would be "
            "stored unchecked and later read as though the schema had accepted it, so this "
            "backend refuses it. On the fleet sender that is MESH_SCHEMA_V1=1.")

    # `type(...) is not int` rather than isinstance: bool is a subclass of int and
    # True == 1, so isinstance would admit schema_version=True.
    if type(value["schema_version"]) is not int or value["schema_version"] != MESH_MESSAGE_V1:
        _mesh_schema_error("schema_version must be the integer 1")

    fields = set(value)
    missing = sorted(MESH_MESSAGE_V1_REQUIRED_FIELDS - fields)
    if missing:
        _mesh_schema_error(f"missing required fields: {', '.join(missing)}")
    unknown = fields - MESH_MESSAGE_V1_REQUIRED_FIELDS - MESH_MESSAGE_V1_OPTIONAL_FIELDS
    if unknown:
        rendered = ", ".join(sorted(repr(field) for field in unknown))
        _mesh_schema_error(f"unknown fields: {rendered}")

    recipients = value["to"]
    if not isinstance(recipients, list):
        _mesh_schema_error("to must be a non-empty array of normalized recipient labels")
    if not 1 <= len(recipients) <= MESH_MAX_RECIPIENTS:
        _mesh_schema_error(f"to must contain between 1 and {MESH_MAX_RECIPIENTS} recipients")
    for recipient in recipients:
        _validate_mesh_label(recipient, "to recipient")
    if len(set(recipients)) != len(recipients):
        _mesh_schema_error("to must not contain duplicate recipients")

    if "cc" in value:
        cc_recipients = value["cc"]
        if not isinstance(cc_recipients, list):
            _mesh_schema_error("cc must be an array of normalized recipient labels")
        if not 1 <= len(cc_recipients) <= MESH_MAX_RECIPIENTS:
            _mesh_schema_error(
                f"cc must contain between 1 and {MESH_MAX_RECIPIENTS} recipients")
        for recipient in cc_recipients:
            _validate_mesh_label(recipient, "cc recipient")
        if len(set(cc_recipients)) != len(cc_recipients):
            _mesh_schema_error("cc must not contain duplicate recipients")

    _validate_mesh_label(value["from"], "from")
    _validate_mesh_label(value["author_family"], "author_family")

    # OPTIONAL is not UNVALIDATED.
    if "session_label" in value:
        _validate_mesh_session_label(value["session_label"])
    if "subject" in value:
        subject = value["subject"]
        if not isinstance(subject, str) or not MESH_SUBJECT_RE.fullmatch(subject):
            _mesh_schema_error(
                "subject must be 1 to 256 characters matching [A-Za-z0-9][A-Za-z0-9_.:/-]*")
    if "ref" in value:
        ref = value["ref"]
        if not isinstance(ref, str) or not MESH_REF_RE.fullmatch(ref):
            _mesh_schema_error(
                "ref must be 1 to 128 characters matching [A-Za-z0-9][A-Za-z0-9_.:/-]*")

    msg_type = value["type"]
    if not isinstance(msg_type, str) or not MESH_TYPE_RE.fullmatch(msg_type):
        _mesh_schema_error("type must match [a-z][a-z0-9_.-]{0,31}")

    thread_id = value["thread_id"]
    if not isinstance(thread_id, str) or not MESH_THREAD_RE.fullmatch(thread_id):
        _mesh_schema_error(
            "thread_id must be 1 to 256 characters matching [A-Za-z0-9][A-Za-z0-9_.:/-]*")

    timestamp = value["ts"]
    if not isinstance(timestamp, str) or not MESH_TIMESTAMP_RE.fullmatch(timestamp):
        _mesh_schema_error("ts must be a UTC timestamp formatted YYYY-MM-DDTHH:MM:SSZ")
    try:
        datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        _mesh_schema_error("ts must be a real UTC timestamp formatted YYYY-MM-DDTHH:MM:SSZ")

    body = value["body"]
    if not isinstance(body, str) or not body.strip():
        _mesh_schema_error("body must be a non-empty string")
    if len(body.encode("utf-8")) > MESH_MAX_BODY_BYTES:
        _mesh_schema_error(f"body must not exceed {MESH_MAX_BODY_BYTES} UTF-8 bytes")

    if "requires_ack" in value and type(value["requires_ack"]) is not bool:
        _mesh_schema_error("requires_ack must be a boolean when present")
    if "done_when" in value:
        done_when = value["done_when"]
        if (not isinstance(done_when, str) or not done_when.strip() or done_when != done_when.strip()
                or len(done_when) > 280
                or any(ord(char) < 32 or ord(char) == 127 for char in done_when)):
            _mesh_schema_error("done_when must be a non-empty stripped string of at most 280 characters without control characters")
        if value.get("requires_ack") is not True:
            _mesh_schema_error("done_when is allowed only when requires_ack is true")
    if "closes" in value:
        closes = value["closes"]
        if not isinstance(closes, list) or not closes or len(closes) > 16:
            _mesh_schema_error("closes must be a non-empty array of at most 16 msg/ keys")
        for close in closes:
            if (not isinstance(close, str) or not close.startswith("msg/")
                    or len(close) > 512):
                _mesh_schema_error("closes entries must be bounded msg/ keys")
            try:
                validate_mesh_key_ts(close)
            except HandlerError:
                _mesh_schema_error("closes entries must have an in-band epoch-ms msg/ key")
        if len(set(closes)) != len(closes):
            _mesh_schema_error("closes must not contain duplicate msg/ keys")
    if "declines" in value:
        declines = value["declines"]
        if not isinstance(declines, list) or not declines or len(declines) > 16:
            _mesh_schema_error("declines must be a non-empty array of at most 16 msg/ keys")
        for decline in declines:
            if (not isinstance(decline, str) or not decline.startswith("msg/")
                    or len(decline) > 512):
                _mesh_schema_error("declines entries must be bounded msg/ keys")
            try:
                validate_mesh_key_ts(decline)
            except HandlerError:
                _mesh_schema_error("declines entries must have an in-band epoch-ms msg/ key")
        if len(set(declines)) != len(declines):
            _mesh_schema_error("declines must not contain duplicate msg/ keys")
        if "closes" in value and (set(value["closes"]) & set(declines)):
            _mesh_schema_error("closes and declines must not contain the same msg/ key")
    if "attachments" in value:
        _validate_mesh_attachments(value["attachments"])
    if "in_reply_to" in value:
        in_reply_to = value["in_reply_to"]
        if (not isinstance(in_reply_to, str) or not in_reply_to.startswith("msg/")
                or len(in_reply_to) > 512):
            _mesh_schema_error("in_reply_to must be a bounded msg/ key when present")
    return False


# --------------------------------------------------------------------------- #
# Scalar validation
# --------------------------------------------------------------------------- #

def validate_retention_policy(policy):
    """None or '' -> keep_all (an unmarked namespace is NEVER pruned); a known
    policy passes through; anything else is E_BAD_REQUEST."""
    if not policy:
        return RETENTION_KEEP_ALL
    if policy not in RETENTION_POLICIES:
        raise HandlerError(
            "E_BAD_REQUEST",
            f"retention_policy {policy!r} not in {sorted(RETENTION_POLICIES)}",
        )
    return policy


def validate_status(status):
    """Return a known status or None, refusing every other value."""
    if status is None:
        return None
    if status not in ALL_STATUSES:
        raise HandlerError("E_INVALID_STATUS", f"status {status!r} not in {sorted(ALL_STATUSES)}")
    return status


def payload_of(verified_payload):
    """Return the verified payload object or refuse a missing or non-object payload."""
    payload = verified_payload.get("payload") if isinstance(verified_payload, dict) else None
    if not isinstance(payload, dict):
        raise HandlerError("E_BAD_REQUEST", "verified_payload.payload missing or not an object")
    return payload


def args_of(payload):
    """Return the payload args object, using an empty object when it is absent."""
    args = payload.get("args")
    if args is None:
        return {}
    if not isinstance(args, dict):
        raise HandlerError("E_BAD_REQUEST", "payload.args must be an object")
    return args


def require(args, name):
    """Note the empty STRING is treated as missing but an empty list/dict is not, because callers rely on
    `value: {}` being a writable (if useless) memo while `key: ""` is a bug."""
    v = args.get(name)
    if v is None or (isinstance(v, str) and v == ""):
        raise HandlerError("E_BAD_REQUEST", f"missing required arg {name!r}")
    return v


def norm_tags(raw):
    """Tag normalization.

    This is the WHOLE of it: a comma string splits and strips, a list is coerced
    element-wise to str, None is []. There is deliberately no lowercasing, no
    dedupe and no vocabulary check - shared/wiki's `_ontology` vocabulary grows by
    proposal-then-CONFIRM and graph_gen soft-warns on off-vocab tags instead. Adding
    normalization here would silently
    rewrite tags the corpus already carries.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        return [t.strip() for t in raw.split(",") if t.strip()]
    if isinstance(raw, list):
        return [str(t) for t in raw]
    raise HandlerError("E_BAD_REQUEST", "tags must be a list or comma string")


def clamp_limit(raw):
    """A non-positive limit becomes the DEFAULT, not an error, and MAX_LIMIT caps a single
    PAGE (the cursor pages past it)."""
    if raw is None:
        return DEFAULT_LIMIT
    try:
        n = int(raw)
    except (TypeError, ValueError):
        raise HandlerError("E_BAD_REQUEST", "limit must be an integer")
    if n <= 0:
        return DEFAULT_LIMIT
    return min(n, MAX_LIMIT)


def fields_mode_of(args):
    """Fails LOUD: a typo'd `fields` silently returning full bodies would defeat the one thing
    the caller asked for."""
    mode = args.get("fields")
    if mode not in (None,) + FIELDS_MODES:
        raise HandlerError("E_INVALID_FIELDS", "fields must be one of full|abstract|structure")
    return mode


# --------------------------------------------------------------------------- #
# Keyset cursors
# --------------------------------------------------------------------------- #

def encode_cursor(obj):
    raw = json.dumps(obj, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _cursor_is_uuid(v):
    if not isinstance(v, str):
        return False
    try:
        _uuid.UUID(v)
    except Exception:
        return False
    return True


def _cursor_is_timestamp(v):
    if not isinstance(v, str):
        return False
    try:
        datetime.fromisoformat(v.replace("Z", "+00:00"))
    except Exception:
        return False
    return True


def _cursor_is_text(v):
    return isinstance(v, str)


# Every cursor field declares the type it is interpolated as. An UNREGISTERED
# field name raises rather than defaulting to accept: a new cursor field must be
# declared here.
CURSOR_FIELD_TYPES = {
    "c": _cursor_is_timestamp,
    "i": _cursor_is_uuid,
    "k": _cursor_is_text,
}


def decode_cursor(token, keys):
    """Decode an opaque cursor to the tuple of `keys`, refusing anything that is
    not the declared type for that field."""
    try:
        raw = base64.urlsafe_b64decode(str(token) + "=" * (-len(str(token)) % 4))
        obj = json.loads(raw.decode())
    except Exception:
        raise HandlerError("E_BAD_REQUEST", "cursor is not a valid opaque cursor token")
    if not isinstance(obj, dict):
        raise HandlerError("E_BAD_REQUEST", "cursor is not a valid opaque cursor token")
    out = []
    for k in keys:
        if k not in obj:
            raise HandlerError("E_BAD_REQUEST", f"cursor is missing field {k!r}")
        checker = CURSOR_FIELD_TYPES.get(k)
        if checker is None:
            # Programming error, not a client error: a cursor field with no
            # declared type must never reach the SQL interpolation.
            raise RuntimeError(f"cursor field {k!r} has no declared type in CURSOR_FIELD_TYPES")
        if not checker(obj[k]):
            raise HandlerError("E_BAD_REQUEST", f"cursor field {k!r} has the wrong type")
        out.append(obj[k])
    return tuple(out)


# --------------------------------------------------------------------------- #
# Projections
# --------------------------------------------------------------------------- #

def structure_of(value):
    """Drop the heavy prose fields, keep links/status/citations. A non-dict passes
    through untouched - projecting a scalar would silently change its type."""
    if not isinstance(value, dict):
        return value
    return {k: v for k, v in value.items() if k not in ABSTRACT_CONTENT_FIELDS}


def abstract_of(value):
    """body_abstract is verbatim and never truncated; otherwise the LONGEST content-class
    field, truncated with a flag; a value with no content-class field is DATA and comes back
    MARKED (`data_row`), never silently as prose."""
    if not isinstance(value, dict):
        return {"abstract": None, "abstract_source": None,
                "abstract_truncated": False, "value_kind": type(value).__name__}
    ba = value.get("body_abstract")
    if isinstance(ba, str) and ba.strip():
        return {"title": value.get("title"), "abstract": ba,
                "abstract_source": "body_abstract", "abstract_truncated": False}
    best_field, best = None, ""
    for f in ABSTRACT_CONTENT_FIELDS:
        v = value.get(f)
        if isinstance(v, str) and len(v) > len(best):
            best_field, best = f, v
    if best_field is None:
        return {"title": value.get("title"), "abstract": None,
                "abstract_source": None, "abstract_truncated": False, "data_row": True}
    return {"title": value.get("title"),
            "abstract": best[:ABSTRACT_FALLBACK_CHARS],
            "abstract_source": best_field,
            "abstract_truncated": len(best) > ABSTRACT_FALLBACK_CHARS}


# --------------------------------------------------------------------------- #
# Crypto / time helpers
# --------------------------------------------------------------------------- #

# jcs_canonicalize and payload_jcs_sha256 have no caller in today's handlers (handle_write
# hashes the bytes _verify_provenance_signature verified), but they are NOT dead: the pre-A1
# handler, which the rollback path still runs on the A1 schema, imports payload_jcs_sha256
# from this module (tests/test_signed_bytes_down.py loads it from git and writes with it).

def jcs_canonicalize(obj):
    """See the module docstring for why this is `primitives.jcs` and not
    `json.dumps(sort_keys=True)`. Kept (#130): payload_jcs_sha256 below calls it."""
    from apps.crypto import primitives as P

    return P.jcs(obj)


def payload_jcs_sha256(payload):
    """Hex sha256 of the canonical payload bytes. `shared_memory.payload_jcs_sha256`
    carries CHECK (~ '^[0-9a-f]{64}$') and a UNIQUE index, so this value is both the
    integrity anchor and the replay key (measured off pg_constraint). Kept (#130): the pre-A1
    handler that tests/test_signed_bytes_down.py loads from git imports it."""
    return hashlib.sha256(jcs_canonicalize(payload)).hexdigest()


def b64url_decode(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
