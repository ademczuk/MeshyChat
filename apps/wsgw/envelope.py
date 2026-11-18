"""WebSocket wire format.

THE TAG IS `type` AND THE VALUES ARE snake_case, so a skill_call frame goes on the
wire as `"skill_call"`. `test_ws.py::test_frame_type_tags_are_snake_case_not_dotted`
pins this distinction.

That is easy to get wrong because the SIGNED ENVELOPE inside a skill frame uses
dotted `kind` values - `"kind": "skill.call"` - in the very same JSON object.
Two different spellings, one document.

Unknown client-frame tags become UNKNOWN and are answered with
`UNKNOWN_FRAME`.
"""
from __future__ import annotations

import datetime as _dt
import uuid
from dataclasses import dataclass, field
from typing import Any

from apps.calls.envelope import SignedEnvelope
from apps.calls.repository import validate_call_id

# --- ClientFrame tags --------------------------------------------------------
PING = "ping"
SUBSCRIBE = "subscribe"
UNSUBSCRIBE = "unsubscribe"
SKILL_CALL = "skill_call"
SKILL_ACK = "skill_ack"
SKILL_RUNNING = "skill_running"
SKILL_CHUNK = "skill_chunk"
SKILL_RESULT = "skill_result"
SKILL_ERROR = "skill_error"
SKILL_CANCEL = "skill_cancel"
SKILL_RESUME = "skill_resume"
UNKNOWN = "__unknown__"  # not a wire value; the local unknown-tag marker

#: Every skill_* client frame. Divergence 041 implements them in slices: S1 types
#: them (parse_skill_frame), S4 handles
#: the four a target sends (apps/wsgw/target_frames.py), S6 the caller's skill_call and
#: skill_cancel (apps/wsgw/caller_frames.py), S7 skill_chunk (target_frames.py), and
#: skill_resume is still refused (see frame_handlers.SkillFrameNotImplemented).
SKILL_CLIENT_FRAMES = frozenset({
    SKILL_CALL, SKILL_ACK, SKILL_RUNNING, SKILL_CHUNK,
    SKILL_RESULT, SKILL_ERROR, SKILL_CANCEL, SKILL_RESUME,
})

CLIENT_FRAME_TAGS = frozenset({PING, SUBSCRIBE, UNSUBSCRIBE}) | SKILL_CLIENT_FRAMES


@dataclass
class ClientFrame:
    """A parsed client->server frame. `data` is the raw object minus nothing:
    handlers read named fields out of it."""

    type: str
    data: dict = field(default_factory=dict)

    def channels(self) -> list[str]:
        """`channels` for subscribe/unsubscribe.

        A non-list, or a list with a non-string in it, is rejected rather than
        being silently coerced.
        """
        raw = self.data.get("channels")
        if not isinstance(raw, list) or not all(isinstance(c, str) for c in raw):
            raise ValueError("`channels` must be an array of strings")
        return raw


def parse_client_frame(obj: Any) -> ClientFrame:
    """Deserialize one client frame. Raises ValueError when the JSON is not a
    tagged object at all; an object whose tag is merely unrecognised becomes
    UNKNOWN."""
    if not isinstance(obj, dict):
        raise ValueError("frame must be a JSON object")
    tag = obj.get("type")
    if not isinstance(tag, str):
        raise ValueError("frame is missing its `type` tag")
    if tag not in CLIENT_FRAME_TAGS:
        return ClientFrame(type=UNKNOWN, data=obj)
    return ClientFrame(type=tag, data=obj)


# --- typed skill client frames -----------------------------------------------
# Design docs/design/skill-frames.md (fin/design-skill-frames), section 7 slice S1, with
# the parse-level checks of 4.7, recorded as divergence 041, S1. Since S4 the handlers of the
# four target frames (skill_ack, skill_running, skill_result, skill_error) parse through here,
# since S6 the caller's skill_call and skill_cancel, and since S7 skill_chunk; skill_resume is
# still refused (frame_handlers.SkillFrameNotImplemented).
#
# TWO LAYERS, each answering differently:
#
#   SHAPE. A required field missing, or a field of the wrong JSON type, fails
#   parsing of the whole frame. Here that is a ValueError, which the consumer answers with PARSE
#   (the path `ClientFrame.channels()` takes).
#   DOMAIN, the handlers' layer. A well-typed value outside its set or bound is
#   refused with the frame's own code: SkillFrameBad. Not a ValueError, so the
#   consumer's PARSE branch can never swallow it.
#
# Shape is checked for the whole frame before any domain check, so a handler
# reads only a fully parsed frame.
#
# The parser's bounds and their reasons:
#   * mode_override "lesson" is SKILL_CALL_BAD because the live
#     skill_calls_mode_check refuses the insert (design 2.12.7).
#   * sequence outside 0..=2147483647 is SKILL_CHUNK_BAD at parse, so it never
#     overflows the database int4 column (design 4.7, T22).
#   * error_code over 128 and error_message over 4096 characters are
#     SKILL_ERROR_BAD (design 4.7).
#   * envelope.payload must be a JSON object, as the HTTP routes' parsers already
#     require (apps/calls/views.py and apps/a2a/views.py `_envelope_from`).
#     Every signed payload is an object with `v` and `kind`.
#   * a frame's call_id is a call id, by the rule the HTTP path already applies
#     (apps/calls/repository.py validate_call_id): not
#     empty, at most 200 UTF-8 bytes, no Unicode Cc character. A shape failure, so
#     PARSE. It may be logged (design 4.7 keeps "no
#     such call" and "not your call" apart in the log), so a newline in it would
#     forge a log line. S1 review F1.
#   * no string field holds U+0000 or a lone surrogate. PostgreSQL text cannot
#     hold U+0000. S1 review F1.
# Not checked here: a key that appears twice in one frame. json.loads, in the
# consumer, keeps the last one.

#: The call modes: `"sync" | "async" | "stream"`, exactly what the live
#: `skill_calls_mode_check` admits. That CHECK refuses "lesson" (design 2.12.7).
SKILL_MODES = frozenset({"sync", "async", "stream"})
#: `file_ref` parses; its handler refuses it SKILL_CHUNK_BAD until its slice
#: (divergence 041 S7, design Q11, apps/wsgw/target_frames.chunk).
CHUNK_TYPES = frozenset({"text", "json", "file_ref"})
#: Resume actions are snake_case. An unknown value is a shape error.
RESUME_ACTIONS = frozenset({"accept", "decline", "cancel"})
#: `skill_call_chunks.sequence` is int4.
SEQUENCE_MAX = 2_147_483_647
#: Design 4.7. Characters (code points), not bytes.
MAX_ERROR_CODE_CHARS = 128
MAX_ERROR_MESSAGE_CHARS = 4096

#: A refused skill call.
CODE_SKILL_CALL_BAD = "SKILL_CALL_BAD"
#: A refused skill chunk.
CODE_SKILL_CHUNK_BAD = "SKILL_CHUNK_BAD"
#: The chunk was refused. Since divergence 041 S7 an unknown call and a call
#: that is not the socket's get this code with ONE text (design Q7), avoiding an existence oracle.
CODE_SKILL_CHUNK_FAILED = "SKILL_CHUNK_FAILED"
#: The call is the socket's and is not `running`. Told only once ownership is proven.
CODE_SKILL_CHUNK_NOT_RUNNING = "SKILL_CHUNK_NOT_RUNNING"
#: error_code and error_message have local bounds. Named as the two above are,
#: a value this frame's handler will never take.
CODE_SKILL_ERROR_BAD = "SKILL_ERROR_BAD"
#: The target frame was refused. Since divergence 041 S4 an unknown call and a call that is not the
#: socket's both get its code with ONE text (apps/wsgw/target_frames.py, design Q7).
CODE_SKILL_ACK_FAILED = "SKILL_ACK_FAILED"
CODE_SKILL_RUNNING_FAILED = "SKILL_RUNNING_FAILED"
CODE_SKILL_RESULT_FAILED = "SKILL_RESULT_FAILED"
CODE_SKILL_ERROR_FAILED = "SKILL_ERROR_FAILED"
#: A signed skill.result is not stored when its signed
#: call_id is not the frame's (design 2.12.2) or its signed status is neither completed nor
#: failed (Q6).
CODE_SKILL_RESULT_BAD = "SKILL_RESULT_BAD"
#: The pipeline refused or failed the caller's call. Since
#: divergence 041 S6 its message is the HTTP route's rendering of the same error, `<code>:
#: <message>` (apps/wsgw/caller_frames.py).
CODE_SKILL_CALL_FAILED = "SKILL_CALL_FAILED"
#: The caller's cancel was refused. Since 041 S6 a call that does not exist
#: and a call this peer did not make get its code with ONE text (design Q7).
CODE_SKILL_CANCEL_FAILED = "SKILL_CANCEL_FAILED"


class SkillFrameBad(Exception):
    """A skill frame whose fields have the right JSON types and a value outside
    their domain. Carries the error frame's code and message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message

    def frame(self) -> dict:
        return error(self.code, self.message)


@dataclass(frozen=True)
class SkillCallFrame:
    """A parsed skill_call frame."""
    envelope: SignedEnvelope
    mode_override: str | None


@dataclass(frozen=True)
class SkillAckFrame:
    """A parsed skill_ack frame."""
    call_id: str


@dataclass(frozen=True)
class SkillRunningFrame:
    """A parsed skill_running frame."""
    call_id: str


@dataclass(frozen=True)
class SkillChunkFrame:
    """A parsed skill_chunk frame."""
    call_id: str
    sequence: int
    chunk_type: str
    envelope: SignedEnvelope


@dataclass(frozen=True)
class SkillResultFrame:
    """A parsed skill_result frame."""
    call_id: str
    envelope: SignedEnvelope


@dataclass(frozen=True)
class SkillErrorFrame:
    """A parsed skill_error frame."""
    call_id: str
    error_code: str
    error_message: str


@dataclass(frozen=True)
class SkillCancelFrame:
    """A parsed skill_cancel frame."""
    call_id: str
    reason: str | None


@dataclass(frozen=True)
class SkillResumeFrame:
    """A parsed skill_resume frame, refused because calls cannot be paused (design 2.10)."""
    call_id: str
    context_id: str
    action: str
    supplied_input: Any
    continuation_token: str


def _required(data: dict, tag: str, name: str) -> Any:
    if name not in data:
        raise ValueError(f"`{tag}` is missing field `{name}`")
    return data[name]


def _text(value: str, tag: str, name: str) -> str:
    """A string a database column or a log line can hold: no U+0000, no lone surrogate.

    json.loads keeps a lone surrogate escape, and psycopg would raise UnicodeEncodeError at the first
    statement, a ValueError the consumer would call PARSE only by accident. PostgreSQL text
    cannot hold U+0000, and psycopg raises DataError for it, which is not a ValueError at
    all. Both are shape failures here. The message names the field and never the value.
    """
    if "\x00" in value:
        raise ValueError(f"`{tag}.{name}` must not contain U+0000")
    try:
        value.encode("utf-8")      # strict UTF-8 fails on exactly the unpaired surrogates
    except UnicodeEncodeError:
        raise ValueError(f"`{tag}.{name}` must not contain a lone surrogate") from None
    return value


def _string(data: dict, tag: str, name: str) -> str:
    value = _required(data, tag, name)
    if not isinstance(value, str):
        raise ValueError(f"`{tag}.{name}` must be a string")
    return _text(value, tag, name)


def _optional_string(data: dict, tag: str, name: str) -> str | None:
    value = data.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"`{tag}.{name}` must be a string or null")
    return _text(value, tag, name)


def _call_id(data: dict, tag: str) -> str:
    """The frame's call_id, typed as the HTTP path types one (validate_call_id). Its own
    messages never echo the value, and neither does this one."""
    value = _string(data, tag, "call_id")
    try:
        return validate_call_id(value)
    except ValueError as exc:
        raise ValueError(f"`{tag}.call_id` is not a call id: {exc}") from None


def _integer(data: dict, tag: str, name: str) -> int:
    value = _required(data, tag, name)
    # bool is an int in Python and never a JSON integer; 3.0 is a float.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"`{tag}.{name}` must be an integer")
    return value


def _envelope(data: dict, tag: str) -> SignedEnvelope:
    raw = _required(data, tag, "envelope")
    if not isinstance(raw, dict):
        raise ValueError(f"`{tag}.envelope` must be an object")
    payload = _required(raw, f"{tag}.envelope", "payload")
    if not isinstance(payload, dict):
        raise ValueError(f"`{tag}.envelope.payload` must be an object")
    return SignedEnvelope(payload=payload,
                          signature=_string(raw, f"{tag}.envelope", "signature"),
                          signing_kid=_string(raw, f"{tag}.envelope", "signing_kid"))


def _parse_skill_call(data: dict) -> SkillCallFrame:
    frame = SkillCallFrame(envelope=_envelope(data, SKILL_CALL),
                           mode_override=_optional_string(data, SKILL_CALL, "mode_override"))
    if frame.mode_override is not None and frame.mode_override not in SKILL_MODES:
        raise SkillFrameBad(CODE_SKILL_CALL_BAD,
                            f"unknown mode_override: {frame.mode_override!r}; expected one "
                            f"of {', '.join(sorted(SKILL_MODES))}")
    return frame


def _parse_skill_ack(data: dict) -> SkillAckFrame:
    return SkillAckFrame(call_id=_call_id(data, SKILL_ACK))


def _parse_skill_running(data: dict) -> SkillRunningFrame:
    return SkillRunningFrame(call_id=_call_id(data, SKILL_RUNNING))


def _parse_skill_chunk(data: dict) -> SkillChunkFrame:
    frame = SkillChunkFrame(call_id=_call_id(data, SKILL_CHUNK),
                            sequence=_integer(data, SKILL_CHUNK, "sequence"),
                            chunk_type=_string(data, SKILL_CHUNK, "chunk_type"),
                            envelope=_envelope(data, SKILL_CHUNK))
    # Check chunk_type before sequence.
    if frame.chunk_type not in CHUNK_TYPES:
        raise SkillFrameBad(CODE_SKILL_CHUNK_BAD,
                            f"unknown chunk_type: {frame.chunk_type!r}; expected one of "
                            f"{', '.join(sorted(CHUNK_TYPES))}")
    if not 0 <= frame.sequence <= SEQUENCE_MAX:
        raise SkillFrameBad(CODE_SKILL_CHUNK_BAD,
                            f"sequence must be between 0 and {SEQUENCE_MAX}, "
                            f"got {frame.sequence}")
    return frame


def _parse_skill_result(data: dict) -> SkillResultFrame:
    return SkillResultFrame(call_id=_call_id(data, SKILL_RESULT),
                            envelope=_envelope(data, SKILL_RESULT))


def _parse_skill_error(data: dict) -> SkillErrorFrame:
    frame = SkillErrorFrame(call_id=_call_id(data, SKILL_ERROR),
                            error_code=_string(data, SKILL_ERROR, "error_code"),
                            error_message=_string(data, SKILL_ERROR, "error_message"))
    if len(frame.error_code) > MAX_ERROR_CODE_CHARS:
        raise SkillFrameBad(CODE_SKILL_ERROR_BAD,
                            f"error_code is {len(frame.error_code)} characters; the most "
                            f"is {MAX_ERROR_CODE_CHARS}")
    if len(frame.error_message) > MAX_ERROR_MESSAGE_CHARS:
        raise SkillFrameBad(CODE_SKILL_ERROR_BAD,
                            f"error_message is {len(frame.error_message)} characters; the "
                            f"most is {MAX_ERROR_MESSAGE_CHARS}")
    return frame


def _parse_skill_cancel(data: dict) -> SkillCancelFrame:
    return SkillCancelFrame(call_id=_call_id(data, SKILL_CANCEL),
                            reason=_optional_string(data, SKILL_CANCEL, "reason"))


def _parse_skill_resume(data: dict) -> SkillResumeFrame:
    action = _string(data, SKILL_RESUME, "action")
    if action not in RESUME_ACTIONS:
        raise ValueError(f"`{SKILL_RESUME}.action` {action!r} is not one of "
                         f"{', '.join(sorted(RESUME_ACTIONS))}")
    return SkillResumeFrame(call_id=_call_id(data, SKILL_RESUME),
                            context_id=_string(data, SKILL_RESUME, "context_id"),
                            action=action,
                            supplied_input=data.get("supplied_input"),
                            continuation_token=_string(data, SKILL_RESUME,
                                                       "continuation_token"))


_SKILL_PARSERS = {
    SKILL_CALL: _parse_skill_call,
    SKILL_ACK: _parse_skill_ack,
    SKILL_RUNNING: _parse_skill_running,
    SKILL_CHUNK: _parse_skill_chunk,
    SKILL_RESULT: _parse_skill_result,
    SKILL_ERROR: _parse_skill_error,
    SKILL_CANCEL: _parse_skill_cancel,
    SKILL_RESUME: _parse_skill_resume,
}


def parse_skill_frame(frame: ClientFrame):
    """The typed fields of one skill client frame.

    Raises ValueError for a shape failure (PARSE) and SkillFrameBad for a value
    outside its domain (the frame's own `_BAD` code). The target frames' handlers call
    this (divergence 041 S4, and skill_chunk's since S7), and the caller's (S6); skill_resume
    has no handler.
    """
    parser = _SKILL_PARSERS.get(frame.type)
    if parser is None:
        raise ValueError(f"`{frame.type}` is not a skill frame")
    return parser(frame.data)


# --- ServerFrame builders ----------------------------------------------------
# Returned as plain dicts: the dict IS the wire object, so a test can assert on
# it without a serialization layer in between that could hide a mismatch.


def _rfc3339(when: _dt.datetime) -> str:
    """Serialize an RFC 3339 timestamp with a `Z` offset."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.timezone.utc)
    return when.astimezone(_dt.timezone.utc).isoformat().replace("+00:00", "Z")


def welcome(server_time: _dt.datetime, connection_id: uuid.UUID) -> dict:
    """Queue the welcome frame immediately on connect."""
    return {"type": "welcome",
            "server_time": _rfc3339(server_time),
            "connection_id": str(connection_id)}


def pong() -> dict:
    """Build a pong frame whose object carries the tag alone."""
    return {"type": "pong"}


def subscribed(channels: list[str]) -> dict:
    """Build a subscribed frame."""
    return {"type": "subscribed", "channels": list(channels)}


def unsubscribed(channels: list[str]) -> dict:
    """Build an unsubscribed frame."""
    return {"type": "unsubscribed", "channels": list(channels)}


def error(code: str, message: str) -> dict:
    """Build an error frame."""
    return {"type": "error", "code": code, "message": message}


def peer_online(peer_id: uuid.UUID) -> dict:
    """Build a peer_online frame for `presence:<peer_id>` subscribers."""
    return {"type": "peer_online", "peer_id": str(peer_id)}


def peer_offline(peer_id: uuid.UUID) -> dict:
    """Build a peer_offline frame."""
    return {"type": "peer_offline", "peer_id": str(peer_id)}


def peer_presence(peer_id: uuid.UUID, state: str, blocked_on: uuid.UUID | None,
                  note: str | None, live: bool) -> dict:
    """Build the row-presence update the beat route fans out to
    `presence:<peer_id>` subscribers.

    `blocked_on` and `note` are OMITTED when None, not sent as null.
    `tools/memory/kxxx_presence_guard.py:132-137` reads
    `type`, `peer_id`, `state` and `note` off this frame.
    """
    frame: dict = {"type": "peer_presence", "peer_id": str(peer_id), "state": state}
    if blocked_on is not None:
        frame["blocked_on"] = str(blocked_on)
    if note is not None:
        frame["note"] = note
    frame["live"] = bool(live)
    return frame


def mesh_message(key: str, from_peer_id: uuid.UUID, to: list[str] | None,
                 created_at: str, *, role: str, to_truncated: bool = False,
                 cc: list[str] | None = None, cc_truncated: bool = False,
                 preview: tuple[str, bool] | None = None,
                 edit: tuple[int, str, str] | None = None,
                 sender: str | None = None,
                 requires_ack: bool | None = None,
                 directive: bool = False, urgent: bool = False, urgent_declined: bool = False) -> dict:
    """Build a mesh-message notification. Its base fields are `type`, `key`, `from_peer_id`,
    `created_at`, and `role`; divergence 033 adds the recipient-specific fields below. A NUDGE,
    never a transport: the client reads the row over its signed path.

    Fields are emitted in this order, and each recipient-specific field is for ONE recipient (the
    frame is built per recipient, apps/wsgw/mesh_notify.py):
      key, from_peer_id   the message identity
      from                the sender's ALIAS (divergence 063): the row's `author_label` ONLY when it is the label the fleet ROSTER gives
                          that peer (a peer that merely NAMED itself like a seat gets none: 028 accepts any label in the caller's own set,
                          `peers.name` included); the frame never reads the body's `from`. Present only when the row is a first
                          version: on a row an EDIT wrote `author_label` is the EDITOR's, and `edited_by` carries that.
                          A person reading a notice cannot tell a peer from its id, so the frame names the seat.
      to                  a LIST of labels as written, never JSON text; omitted when the row
                          has no `to` (033)
      to_truncated        true, present only when the list left labels off (033)
      cc, cc_truncated    the same for `cc` (033),
                          and only for a recipient memory.read without a capability
                          would show the row to (040), as
                          the preview (review F3 of 44640f2)
      created_at          `Z`-suffixed second-precision UTC text
      role                "to" | "cc" | "broadcast": why THIS recipient got it (033)
      requires_ack        true or false: whether the message asks for an answer, as the mesh
                          graph reads the row (#199); omitted only for a row that is not an
                          object. A client's wake rule may hold a cc copy that asks nothing
      directive           true, present only on a `to` frame of a row holding a validated seat
                          directive (the directive rule): the postbox's priority-lane hint, never authority
      preview_unverified  the first 160 characters of the body, and whether that cut it,
      preview_truncated   both present or both absent, and present only for a recipient
                          memory.read without a capability would show the body to (033,
                          040). NOT signature-checked,
                          as the name says: the signed read stays the authority
      version, edited_by, edited_at
                          all three or none (divergence 059): present only when this row is a
                          version an EDIT wrote (`shared_memory_edit` holds a record for it),
                          because `from` stays the original sender and a reader would otherwise
                          credit that sender with words the editor wrote. `version` is the row's,
                          `edited_by` the editor's label (the row's `author_label`), `edited_at`
                          the record's time as the `Z` second-precision text `created_at` is. A
                          message frame of version 1 has none of them, so a frame from before
                          this change is the frame of a row that was never edited

    The consumers read `type` (lower-cased), `key`, `from_peer_id` and `to`:
    tools/mesh_client/agentmeshy_mesh_client/reactor.py:216-233 and
    tools/memory/mesh_ws_monitor.py:813-825. Both parse a LIST `to` as well as a string
    (the list branch of reactor.py:34-63 `_to_labels`, copied verbatim into
    tests/test_mesh_live.py), and neither reads a field it does not know.
    """
    frame: dict = {"type": "mesh_message", "key": key, "from_peer_id": str(from_peer_id)}
    if isinstance(sender, str) and sender:
        frame["from"] = sender
    if to is not None:
        frame["to"] = list(to)
        if to_truncated:
            frame["to_truncated"] = True
    if cc is not None:
        frame["cc"] = list(cc)
        if cc_truncated:
            frame["cc_truncated"] = True
    frame["created_at"] = created_at
    frame["role"] = role
    if requires_ack is not None:
        frame["requires_ack"] = bool(requires_ack)
    # `directive` (Tue 10 Nov 2026, oxxxxxx item 1): true, present only on a `to` frame of a row that holds a
    # seat directive. memory.write stores a `directive` only after validate_directive (a roster seat, its target on the
    # to line), and memory.edit freezes everything but `body`, so the key's presence is the server's own word. A
    # ROUTING hint for the postbox priority lane (deliver next): the seat still does the signed read, which alone
    # decides what the directive may authorise. Absent on cc and broadcast frames, and on every other row.
    if directive and role == "to":
        frame["directive"] = True
    # The urgent lane (kxxx): delivery ORDER only, on a `to` frame only; a cc copy of the same row is ordinary.
    if urgent and role == "to":
        frame["urgent"] = True
    if urgent_declined and role == "to":
        frame["urgent_declined"] = True
    if preview is not None:
        frame["preview_unverified"], frame["preview_truncated"] = preview
    if edit is not None:
        frame["version"], frame["edited_by"], frame["edited_at"] = int(edit[0]), edit[1], edit[2]
    return frame


def mesh_edit(key: str, version: int, edited_at: str, edited_by: str, role: str) -> dict:
    """The event that says a mail row was EDITED (divergence 059; docs/design/edit.md section 4).
    A pointer and a record, never text: the key and the new version, when, by whom (the
    editor's label) and why THIS recipient is told. `role` is a message frame's (`to`, `cc`,
    `broadcast`) or `replied`: a seat that wrote a direct reply to the row and was not among its
    recipients. The role stays in the frame because the server already decides it for the message
    frame, and re-deriving it in a client from a line that carries no routing would be a second rule.

    Field order: type, key, version, edited_at, edited_by, role. Built PER RECIPIENT (the role is the
    recipient's). Unlike a redaction's event, an edit WAKES the seat that gets it: a request whose text
    changed after the seat acted on it is a silent divergence. The monitor prints it as
    `{"event": "edit", ...}` (tools/meshy_node/monitor.py)."""
    return {"type": "mesh_edit", "key": key, "version": int(version), "edited_at": edited_at,
            "edited_by": edited_by, "role": role}


def mesh_redaction(key: str, version: int, redacted_at: str, reason_code: str) -> dict:
    """The event that says a mail row was redacted (divergence 053; docs/design/redaction.md
    section 3). A pointer and a
    record, never text: the key and version of the row, when it was redacted, and one of the
    closed reason codes. A client blanks whatever it kept of that row (the postbox's
    preview line) and never asks the server what the row said.

    Field order: type, key, version, redacted_at, reason_code. `redacted_at` is the
    record's time as `Z`-suffixed second-precision UTC text, as a message frame's
    `created_at` is. It is built for the same recipients as the message it withdraws (the
    row's to, cc or broadcast) and for the authors of its direct replies; the monitor prints
    it as `{"event": "redaction", ...}` (tools/meshy_node/monitor.py)."""
    return {"type": "mesh_redaction", "key": key, "version": int(version),
            "redacted_at": redacted_at, "reason_code": reason_code}


# --- skill server frames -----------------------------------------------------
# Built here (divergence 041 S1) and sent since S5 (skill_invoke, apps/wsgw/call_notify.py), S6
# (skill_acked and the terminal frames, apps/wsgw/caller_frames.py) and S7 (skill_chunk, built
# from a stored row, caller_frames.chunk_frame).
# Their design is docs/design/skill-frames.md (fin/design-skill-frames).
# Field order is the wire order.

#: `"completed" | "failed"`. A timeout goes out as skill_timeout and an
#: abandoned call as skill_error (`caller_frames.terminal_frame`, `apps/calls/terminal.py`).
SKILL_RESULT_STATUSES = frozenset({"completed", "failed"})


def _signed_envelope(env: SignedEnvelope, *, any_payload: bool = False) -> dict:
    """Serialize a signed envelope.

    Its payload, signature, and signing_kid fields are all required. A dashboard_test row has a
    NULL args_signature and args_signing_kid (sql/006_skill_calls.sql:19-20); writing those
    as null would make an invalid frame, so it is refused (S1 review F2).

    `any_payload` admits a payload that is not an object for the
    one frame that carries one: a skill_result whose payload was RECONSTRUCTED from a row's
    `result` (#596, `caller_frames.terminal_frame`, `apps/calls/terminal.py`), which is marked
    and was never signed as sent.
    """
    if not any_payload and not isinstance(env.payload, dict):
        raise ValueError("a signed envelope's payload must be an object")
    for name, value in (("signature", env.signature), ("signing_kid", env.signing_kid)):
        if not isinstance(value, str):
            raise ValueError(f"a signed envelope needs its {name}; got {type(value).__name__}")
    return {"payload": env.payload, "signature": env.signature,
            "signing_kid": env.signing_kid}


def skill_invoke(*, call_id: str, caller_peer_id: uuid.UUID, target_peer_id: uuid.UUID,
                 skill_name: str, skill_version: str, mode: str,
                 envelope: SignedEnvelope, deadline_at: _dt.datetime) -> dict:
    """Build a TARGET frame that says "run this call".

    Built from the committed row (design 4.6): `envelope` is the caller's whole signed
    payload with its signature and kid (`args`, `args_signature`, `args_signing_kid`), so
    the target can verify the caller itself.

    `continuation_nonce` and `continuation_token` are NEVER written. Nothing here mints
    them (apps/calls/pipeline.py:50-54), so the keys are absent, not null.

    `deadline_at` is RFC 3339 UTC with a `Z`. `_rfc3339` writes six fractional digits.
    """
    if mode not in SKILL_MODES:
        raise ValueError(f"skill_invoke mode {mode!r} is not one of {sorted(SKILL_MODES)}")
    # The caller column is NULL for a dashboard_test row (sql/006:7), and `str(None)` is "None".
    for name, value in (("caller_peer_id", caller_peer_id), ("target_peer_id", target_peer_id)):
        if not isinstance(value, uuid.UUID):
            raise ValueError(f"skill_invoke {name} must be a UUID; got {type(value).__name__}")
    for name, value in (("call_id", call_id), ("skill_name", skill_name),
                        ("skill_version", skill_version)):
        if not isinstance(value, str):
            raise ValueError(f"skill_invoke {name} must be a string; got {type(value).__name__}")
    return {"type": "skill_invoke",
            "call_id": call_id,
            "caller_peer_id": str(caller_peer_id),
            "target_peer_id": str(target_peer_id),
            "skill_name": skill_name,
            "skill_version": skill_version,
            "mode": mode,
            "envelope": _signed_envelope(envelope),
            "deadline_at": _rfc3339(deadline_at)}


def skill_acked(call_id: str) -> dict:
    """Build a CALLER frame saying "your skill_call was accepted", not the target's ack."""
    return {"type": "skill_acked", "call_id": call_id}


def skill_chunk(call_id: str, sequence: int, chunk_type: str,
                envelope: SignedEnvelope) -> dict:
    """Build a caller frame for one persisted stream chunk."""
    return {"type": "skill_chunk", "call_id": call_id, "sequence": sequence,
            "chunk_type": chunk_type, "envelope": _signed_envelope(envelope)}


def skill_result(call_id: str, status: str, envelope: SignedEnvelope, *,
                 payload_reconstructed: bool = False) -> dict:
    """Build a caller frame for a completed call or a SIGNED failure.

    `payload_reconstructed` is absent unless true. A reconstructed payload is
    the row's `result`, any JSON value, null included (#596; divergence 041 S6's terminal
    mapping, apps/calls/terminal.py).
    """
    if status not in SKILL_RESULT_STATUSES:
        raise ValueError(
            f"skill_result status {status!r} is not one of {sorted(SKILL_RESULT_STATUSES)}")
    frame: dict = {"type": "skill_result", "call_id": call_id, "status": status,
                   "envelope": _signed_envelope(envelope, any_payload=payload_reconstructed)}
    if payload_reconstructed:
        frame["payload_reconstructed"] = True
    return frame


def skill_error(call_id: str, error_code: str, error_message: str) -> dict:
    """Build a caller frame for an unsigned failure or `abandoned`."""
    return {"type": "skill_error", "call_id": call_id, "error_code": error_code,
            "error_message": error_message}


def skill_timeout(call_id: str) -> dict:
    """Build a caller frame saying the deadline passed."""
    return {"type": "skill_timeout", "call_id": call_id}


# --- error codes emitted by the hub -----------------------------------------
#: Malformed JSON on the socket.
CODE_PARSE = "PARSE"
#: Binary frames are not supported.
CODE_UNSUPPORTED = "UNSUPPORTED"
#: The tag is not supported.
CODE_UNKNOWN_FRAME = "UNKNOWN_FRAME"
#: The per-peer connection cap was reached.
CODE_TOO_MANY_CONNECTIONS = "TOO_MANY_CONNECTIONS"
#: The per-connection in-flight skill.call cap was reached.
CODE_SKILL_CALL_BUSY = "SKILL_CALL_BUSY"
#: The skill frame with no handler (skill_resume, design 2.10; skill_chunk had none
#: until divergence 041 S7) is refused, never acked; see frame_handlers.SkillFrameNotImplemented.
CODE_UNIMPLEMENTED = "UNIMPLEMENTED"
