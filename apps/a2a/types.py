"""A2A v1.0 wire types and the projection from a skill call.

Key order, enum spellings, and omission rules are part of the wire.

THREE WIRE RULES THAT ARE INVISIBLE IN A HANDLER AND WRONG BY DEFAULT IN PYTHON:

 1. ENUM SPELLING. A2A v1.0 uses ProtoJSON, so lifecycle states use the
    SCREAMING_SNAKE names (`TASK_STATE_WORKING`), not v0.3.x kebab-case (`working`).
 2. FIELD ORDER. The key order below is part of the wire. Python dicts preserve
    insertion order, so each builder inserts keys in its listed wire order.
 3. OMISSION. An absent optional field is ABSENT, not `null`; an empty list is absent;
    and `append`/`lastChunk` are emitted only when true, matching proto3's omission of
    default-valued scalars. `{"append": false}` and `{}` are different bytes and a
    conformance parser can tell.

NO `kind` DISCRIMINATOR ANYWHERE. v1.0 removed it (breaking change A.2.1); emitting one
is the single most likely way to fail an external v1.0 client.

NO BARE DEFAULTS. `task_state_for` raises `UnknownCallStatus` on an unknown status
instead of falling back to `TASK_STATE_UNSPECIFIED`, and `part_for_chunk` raises
`UnknownChunkType` rather than emitting an empty part. These mappings deliberately
fail when their vocabulary changes: a Python `.get(x, default)` would project a new
status as "unspecified", indistinguishable from a task that really is unspecified.
"""
from __future__ import annotations

import json
import uuid as _uuid

# --------------------------------------------------------------------- TaskState
# A2A v1.0 TaskState wire values.

TASK_STATE_SUBMITTED = "TASK_STATE_SUBMITTED"
TASK_STATE_WORKING = "TASK_STATE_WORKING"
TASK_STATE_INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
TASK_STATE_AUTH_REQUIRED = "TASK_STATE_AUTH_REQUIRED"
TASK_STATE_COMPLETED = "TASK_STATE_COMPLETED"
TASK_STATE_CANCELED = "TASK_STATE_CANCELED"
TASK_STATE_FAILED = "TASK_STATE_FAILED"
TASK_STATE_REJECTED = "TASK_STATE_REJECTED"
TASK_STATE_UNSPECIFIED = "TASK_STATE_UNSPECIFIED"

#: Terminal A2A TaskState values.
TERMINAL_STATES = frozenset({
    TASK_STATE_COMPLETED, TASK_STATE_CANCELED, TASK_STATE_FAILED, TASK_STATE_REJECTED,
})

#: Maps `skill_calls.status` TEXT to a TaskState. The keys are the ten values the live
#: `skill_calls_status_check` CHECK permits (read off pg_constraint, not guessed).
#:
#: The three pre-execution states collapse to `submitted`; `timeout` and
#: `abandoned` are terminal non-successes and surface as `failed`. This mapping is
#: ONE-DIRECTIONAL by design: the call row is the source of truth and the A2A view is a
#: projection, so there is deliberately no inverse.
CALL_STATUS_TO_TASK_STATE = {
    "pending": TASK_STATE_SUBMITTED,
    "routed": TASK_STATE_SUBMITTED,
    "queued": TASK_STATE_SUBMITTED,
    "running": TASK_STATE_WORKING,
    "input_required": TASK_STATE_INPUT_REQUIRED,
    "auth_required": TASK_STATE_AUTH_REQUIRED,
    "completed": TASK_STATE_COMPLETED,
    "failed": TASK_STATE_FAILED,
    "timeout": TASK_STATE_FAILED,
    "abandoned": TASK_STATE_FAILED,
}

# --------------------------------------------------------------------------- Role
# A2A Role wire values.

ROLE_UNSPECIFIED = "ROLE_UNSPECIFIED"
ROLE_USER = "ROLE_USER"
ROLE_AGENT = "ROLE_AGENT"
ROLES = (ROLE_UNSPECIFIED, ROLE_USER, ROLE_AGENT)

#: Canonical TEXT spellings, also the three values the live
#: `skill_call_chunks_chunk_type_check` CHECK admits.
CHUNK_TYPE_TEXT = "text"
CHUNK_TYPE_JSON = "json"
CHUNK_TYPE_FILE_REF = "file_ref"


class UnknownCallStatus(ValueError):
    """A `skill_calls.status` with no A2A projection.

    Raised rather than defaulted: an unrecognized status must fail when the vocabulary
    drifts. A default would render that drift as a valid-looking task.
    """


class UnknownChunkType(ValueError):
    """A `skill_call_chunks.chunk_type` with no A2A Part projection."""


def task_state_for(call_status: str) -> str:
    """Project a stored call status to an A2A TaskState."""
    try:
        return CALL_STATUS_TO_TASK_STATE[call_status]
    except KeyError:
        raise UnknownCallStatus(
            f"no A2A TaskState projection for skill_calls.status {call_status!r}; "
            f"known: {sorted(CALL_STATUS_TO_TASK_STATE)}") from None


#: Every A2A TaskState value. Used to REFUSE a string that is not a TaskState at all,
#: so `is_terminal("TASK_STATE_COMPELTED")` raises
#: instead of quietly answering False - a typo that answers False keeps a finished
#: stream open forever rather than failing.
TASK_STATES = frozenset({
    TASK_STATE_SUBMITTED, TASK_STATE_WORKING, TASK_STATE_INPUT_REQUIRED,
    TASK_STATE_AUTH_REQUIRED, TASK_STATE_COMPLETED, TASK_STATE_CANCELED,
    TASK_STATE_FAILED, TASK_STATE_REJECTED, TASK_STATE_UNSPECIFIED,
})


def is_terminal(task_state: str) -> bool:
    """Return whether a validated A2A TaskState is terminal."""
    if task_state not in TASK_STATES:
        raise UnknownCallStatus(f"not an A2A TaskState: {task_state!r}")
    return task_state in TERMINAL_STATES


# --------------------------------------------------------------------------- Part
# Wire key order: text, raw, url, data, mediaType, filename,
# metadata. A OneOf with NO `kind` tag; exactly one content field is set.

_PART_FIELDS = (
    ("text", "text"), ("raw", "raw"), ("url", "url"), ("data", "data"),
    ("media_type", "mediaType"), ("filename", "filename"), ("metadata", "metadata"),
)


def part(**fields) -> dict:
    """Build a `Part`, omitting every unset optional field.

    `data` is the one field where `None` is ambiguous: a JSON `null` payload is legal.
    This signature treats `None` as unset, so callers that need a null data part must
    use `part_data` below, which sets it directly. `single_data_args` likewise treats a
    null data part as no data part.
    """
    unknown = set(fields) - {name for name, _ in _PART_FIELDS}
    if unknown:
        raise TypeError(f"not Part fields: {sorted(unknown)}")
    out = {}
    for name, wire_name in _PART_FIELDS:
        value = fields.get(name)
        if value is not None:
            out[wire_name] = value
    return out


def part_text(text: str) -> dict:
    """Build a text Part.

    An empty string is a VALUE, not an absence, so it is emitted as `{"text": ""}`.
    """
    return {"text": text}


def part_data(value) -> dict:
    """Build a data Part.

    `value` may be any JSON value, including null. An unparseable JSON chunk produces
    null, so this field is set directly rather than through the generic Part builder.
    """
    return {"data": value}


# ------------------------------------------------------------------------ Message
# Wire key order: messageId, contextId, taskId, role, parts,
# metadata, extensions, and task links.

def message(*, message_id: str, role: str, parts: list, context_id=None, task_id=None,
            metadata=None, extensions=None, reference_task_ids=None) -> dict:
    if role not in ROLES:
        raise ValueError(f"not an A2A Role: {role!r} (expected one of {ROLES})")
    out = {"messageId": message_id}
    if context_id is not None:
        out["contextId"] = context_id
    if task_id is not None:
        out["taskId"] = task_id
    out["role"] = role
    out["parts"] = list(parts)
    if metadata is not None:
        out["metadata"] = metadata
    if extensions:
        out["extensions"] = list(extensions)
    if reference_task_ids:
        out["referenceTaskIds"] = list(reference_task_ids)
    return out


class NoSingleDataPart(ValueError):
    """Raised when a message does not carry exactly one non-null data part.

    Typed rather than returned as `None`, because the caller must distinguish "zero
    data parts" from "a data part whose value happens to be falsy" - `{}` and `0` are
    both legal args payloads and both are falsy in Python.
    """


def single_data_args(msg: dict):
    """Return the one `data` Part's value.

    Zero data parts, or more than one, is an error and the route turns it into a 400.
    A missing `parts` array is also an error; this function runs after the view's shape
    check and must not read a missing array as "zero parts, fine".
    """
    parts = msg.get("parts")
    if not isinstance(parts, list):
        raise NoSingleDataPart("A2A message has no `parts` array")
    values = [p.get("data") for p in parts
              if isinstance(p, dict) and p.get("data") is not None]
    if len(values) != 1:
        raise NoSingleDataPart(
            f"A2A message must carry exactly one data part (found {len(values)})")
    return values[0]


# ----------------------------------------------------------------------- Artifact
# Wire key order: artifactId, parts, name, description,
# extensions, metadata.

def artifact(*, artifact_id: str, parts: list, name=None, description=None,
             extensions=None, metadata=None) -> dict:
    out = {"artifactId": artifact_id, "parts": list(parts)}
    if name is not None:
        out["name"] = name
    if description is not None:
        out["description"] = description
    if extensions:
        out["extensions"] = list(extensions)
    if metadata is not None:
        out["metadata"] = metadata
    return out


# --------------------------------------------------------------- TaskStatus / Task
# TaskStatus and Task field order.

def task_status(*, state: str, timestamp=None, msg=None) -> dict:
    out = {"state": state}
    if timestamp is not None:
        out["timestamp"] = timestamp
    if msg is not None:
        out["message"] = msg
    return out


def task_from_meshy_call(call_id: str, context_id, status: str, result) -> dict:
    """Project a stored skill call to an A2A Task.

    THE ARTIFACT RULE IS A CONJUNCTION, and getting it wrong in either direction is a
    observable wire behavior: the result artifact is emitted ONLY when
    the projected state is COMPLETED **and** a result is present. A failed call with a
    stored `result` carries no artifact, and a completed call with a NULL result carries
    none either. The artifact id is `{call_id}-result` and its single data part is the
    stored result verbatim.
    """
    state = task_state_for(status)
    artifacts = []
    if state == TASK_STATE_COMPLETED and result is not None:
        artifacts.append(artifact(
            artifact_id=f"{call_id}-result",
            parts=[part_data(result)],
            name="result",
        ))
    out = {"id": call_id}
    if context_id is not None:
        out["contextId"] = context_id
    out["status"] = task_status(state=state)
    if artifacts:
        out["artifacts"] = artifacts
    # `history` and `metadata` are never populated by this projection; both are omitted
    # when empty or absent rather than emitted as [] or null.
    return out


# --------------------------------------------------------------- streaming events
# TaskStatusUpdateEvent and TaskArtifactUpdateEvent field order.

def task_status_update_event(*, task_id: str, status: dict, context_id=None,
                             metadata=None) -> dict:
    """NOTE THE FIELD THAT IS NOT HERE: v1.0 DROPPED the v0.3.x `final` bool.
    Terminality is read from `status.state`, so emitting `final` would be the same
    drift class as the kebab-case enum defect."""
    out = {"taskId": task_id}
    if context_id is not None:
        out["contextId"] = context_id
    out["status"] = status
    if metadata is not None:
        out["metadata"] = metadata
    return out


def task_artifact_update_event(*, task_id: str, artifact_: dict, context_id=None,
                               append: bool = False, last_chunk: bool = False,
                               metadata=None) -> dict:
    """`append` and `lastChunk` are omitted when false, matching ProtoJSON's omission
    of default-valued scalars. The first artifact update has no `append` key; a later
    appended update has `"append": true`."""
    out = {"taskId": task_id}
    if context_id is not None:
        out["contextId"] = context_id
    out["artifact"] = artifact_
    if append:
        out["append"] = True
    if last_chunk:
        out["lastChunk"] = True
    if metadata is not None:
        out["metadata"] = metadata
    return out


def part_for_chunk(chunk_type: str, content, file_artifact_id) -> dict:
    """Build the per-chunk Part for `chunk_to_artifact_event`.

    text      -> UTF-8 text with invalid sequences replaced; absent content is b"",
                 which renders as `{"text": ""}` so it does not become an absent part.
    json      -> the parsed value, or JSON `null` when it does not parse.
    file_ref  -> `{"fileArtifactId": <uuid or null>}`.

    `content` is `bytea`, which psycopg hands back as `bytes` (or `memoryview` on some
    paths), so it is coerced rather than assumed.
    """
    if content is None:
        raw = b""
    elif isinstance(content, memoryview):
        raw = content.tobytes()
    elif isinstance(content, (bytes, bytearray)):
        raw = bytes(content)
    else:
        raise TypeError(f"chunk content must be bytes-like, got {type(content).__name__}")

    if chunk_type == CHUNK_TYPE_TEXT:
        # Invalid UTF-8 sequences become U+FFFD rather than raising an error.
        return part_text(raw.decode("utf-8", errors="replace"))
    if chunk_type == CHUNK_TYPE_JSON:
        try:
            return part_data(json.loads(raw.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return part_data(None)
    if chunk_type == CHUNK_TYPE_FILE_REF:
        fid = None if file_artifact_id is None else str(_uuid.UUID(str(file_artifact_id)))
        return part_data({"fileArtifactId": fid})
    raise UnknownChunkType(
        f"no A2A Part projection for chunk_type {chunk_type!r}; known: "
        f"{[CHUNK_TYPE_TEXT, CHUNK_TYPE_JSON, CHUNK_TYPE_FILE_REF]}")


def chunk_to_artifact_event(call_id: str, chunk: dict, append: bool) -> dict:
    """Build a TaskArtifactUpdateEvent for one stored chunk.

    EVERY chunk of a call shares ONE artifact id, `{call_id}-stream`, so a consumer
    appends them into a single growing artifact: `append` is false on the first and
    true after. `last_chunk` is never set because the terminal status event closes the
    stream instead.
    """
    return task_artifact_update_event(
        task_id=call_id,
        artifact_=artifact(
            artifact_id=f"{call_id}-stream",
            parts=[part_for_chunk(chunk["chunk_type"], chunk["content"],
                                  chunk["file_artifact_id"])],
        ),
        append=append,
        last_chunk=False,
    )


__all__ = [
    "CALL_STATUS_TO_TASK_STATE", "CHUNK_TYPE_FILE_REF", "CHUNK_TYPE_JSON",
    "CHUNK_TYPE_TEXT", "NoSingleDataPart", "ROLES", "ROLE_AGENT", "ROLE_UNSPECIFIED",
    "ROLE_USER", "TASK_STATE_AUTH_REQUIRED", "TASK_STATE_CANCELED",
    "TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_INPUT_REQUIRED",
    "TASK_STATE_REJECTED", "TASK_STATE_SUBMITTED", "TASK_STATE_UNSPECIFIED",
    "TASK_STATE_WORKING", "TASK_STATES", "TERMINAL_STATES", "UnknownCallStatus",
    "UnknownChunkType",
    "artifact", "chunk_to_artifact_event", "is_terminal", "message", "part",
    "part_data", "part_for_chunk", "part_text", "single_data_args",
    "task_artifact_update_event", "task_from_meshy_call", "task_state_for",
    "task_status", "task_status_update_event",
]
