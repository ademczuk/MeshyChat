"""Divergence 041, S1, the skill frames' first slice: the server's skill frame builders and
typed parsing of every skill client frame, while every skill frame is still refused on the
socket.

Design: `docs/design/skill-frames.md` (fin/design-skill-frames, 2e229a6), section 7 row S1,
with 2.1, 2.2, 4.6 (the skill_invoke shape), 4.7 (the parse-level checks) and 2.12.7. Every field
list is pinned to those frame definitions in wire key order.

PURE. No socket and no database: the builders return the wire dict and the parsers take one.
That the frames are STILL REFUSED on a real socket is proved in tests/test_ws.py
(test_every_skill_frame_is_still_refused_unimplemented_and_closed_1011), because it needs
the consumer.

House rules (docs/V1-DESIGN-RULES.md): every refusal sits beside the value that is admitted
(C1), and structured fields are compared, never substrings of a serialized frame (C4).

Run:  .venv/Scripts/python.exe -m pytest tests/test_ws_skill_frames.py -q -p no:cacheprovider
"""
from __future__ import annotations

import datetime as _dt
import json
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from apps.calls.envelope import SignedEnvelope  # noqa: E402
from apps.wsgw import envelope as E  # noqa: E402

CALLER = uuid.UUID("11111111-2222-4333-8444-555555555555")
TARGET = uuid.UUID("66666666-7777-4888-9999-aaaaaaaaaaaa")
#: Microseconds that are not a whole number of milliseconds, so ISO formatting writes six
#: digits (see the skill_invoke test).
DEADLINE = _dt.datetime(2026, 9, 24, 12, 0, 30, 250123, tzinfo=_dt.timezone.utc)
ENV = SignedEnvelope(
    payload={"v": 1, "kind": "skill.call", "call_id": "c-1", "args": {"q": "x"}},
    signature="c2lnbmF0dXJl",
    signing_kid="kid-1",
)
ENV_WIRE = {"payload": ENV.payload, "signature": "c2lnbmF0dXJl", "signing_kid": "kid-1"}


def _wire(frame: dict) -> dict:
    """The frame as a client reads it: through JSON. A builder that put a non-JSON value in
    (a UUID object, a datetime) fails here rather than at the socket."""
    return json.loads(json.dumps(frame))


# ==========================================================================
# 1. server frame builders
# ==========================================================================

def _invoke(**over) -> dict:
    kw = dict(call_id="c-1", caller_peer_id=CALLER, target_peer_id=TARGET,
              skill_name="translate", skill_version="1.0.0", mode="sync", envelope=ENV,
              deadline_at=DEADLINE)
    kw.update(over)
    return E.skill_invoke(**kw)


def test_skill_invoke_is_envelope_rs_shape_and_omits_both_continuation_fields():
    """`continuation_nonce` and `continuation_token` are absent because this build mints neither:
    the flow guard is not implemented (`apps/calls/pipeline.py:50-54`).

    They are not null: a present null is a different wire object. The bridge serializes the frame
    before any target reads it, so absent keys stay absent.
    """
    frame = _invoke()
    assert list(frame) == ["type", "call_id", "caller_peer_id", "target_peer_id", "skill_name",
                           "skill_version", "mode", "envelope", "deadline_at"]
    assert frame == {
        "type": "skill_invoke",
        "call_id": "c-1",
        "caller_peer_id": str(CALLER),
        "target_peer_id": str(TARGET),
        "skill_name": "translate",
        "skill_version": "1.0.0",
        "mode": "sync",
        "envelope": ENV_WIRE,
        "deadline_at": "2026-09-24T12:00:30.250123Z",
    }
    assert "continuation_nonce" not in frame and "continuation_token" not in frame
    assert list(frame["envelope"]) == ["payload", "signature", "signing_kid"]
    assert _wire(frame) == frame
    # The deadline is the same instant, in UTC with a Z, from any aware input.
    later = DEADLINE.astimezone(_dt.timezone(_dt.timedelta(hours=-4)))
    assert _invoke(deadline_at=later)["deadline_at"] == "2026-09-24T12:00:30.250123Z"
    assert _dt.datetime.fromisoformat(frame["deadline_at"]) == DEADLINE
    # A whole second has no fraction.
    whole = DEADLINE.replace(microsecond=0)
    assert _invoke(deadline_at=whole)["deadline_at"] == "2026-09-24T12:00:30Z"


def test_skill_invoke_mode_is_the_live_mode_check_and_never_lesson():
    """The live skill_calls_mode_check admits exactly `"sync" | "async" | "stream"` (design 3.2).

    There is no lesson mode (2.12.7), so the builder refuses to put one on the wire.
    """
    for mode in ("sync", "async", "stream"):
        assert _invoke(mode=mode)["mode"] == mode
    for bad in ("lesson", "SYNC", ""):
        with pytest.raises(ValueError, match="mode"):
            _invoke(mode=bad)


def test_skill_acked_and_skill_timeout_carry_the_call_id_alone():
    """skill_acked and skill_timeout carry the call ID alone."""
    assert E.skill_acked("c-1") == {"type": "skill_acked", "call_id": "c-1"}
    assert E.skill_timeout("c-1") == {"type": "skill_timeout", "call_id": "c-1"}
    assert list(E.skill_acked("c-1")) == ["type", "call_id"]
    assert list(E.skill_timeout("c-1")) == ["type", "call_id"]


def test_skill_chunk_is_envelope_rs_shape():
    """`sequence` stays a JSON integer."""
    frame = E.skill_chunk("c-1", 0, "json", ENV)
    assert list(frame) == ["type", "call_id", "sequence", "chunk_type", "envelope"]
    assert frame == {"type": "skill_chunk", "call_id": "c-1", "sequence": 0,
                     "chunk_type": "json", "envelope": ENV_WIRE}
    assert _wire(E.skill_chunk("c-1", 2147483647, "text", ENV))["sequence"] == 2147483647


def test_skill_result_omits_payload_reconstructed_unless_true():
    """`payload_reconstructed` is absent on the happy path and `true` when set."""
    frame = E.skill_result("c-1", "completed", ENV)
    assert list(frame) == ["type", "call_id", "status", "envelope"]
    assert frame == {"type": "skill_result", "call_id": "c-1", "status": "completed",
                     "envelope": ENV_WIRE}
    assert "payload_reconstructed" not in E.skill_result("c-1", "failed", ENV,
                                                         payload_reconstructed=False)
    flagged = E.skill_result("c-1", "completed", ENV, payload_reconstructed=True)
    assert list(flagged) == ["type", "call_id", "status", "envelope", "payload_reconstructed"]
    assert flagged["payload_reconstructed"] is True
    assert _wire(flagged) == flagged


def test_skill_result_status_is_completed_or_failed_only():
    """`"completed" | "failed"`. A timeout is skill_timeout and an abandoned call is skill_error,
    so no other status goes in this frame."""
    for status in ("completed", "failed"):
        assert E.skill_result("c-1", status, ENV)["status"] == status
    for bad in ("timeout", "abandoned", "running", "Completed", ""):
        with pytest.raises(ValueError, match="status"):
            E.skill_result("c-1", bad, ENV)


def test_skill_error_is_envelope_rs_shape():
    """skill_error carries its call ID, error code, and message."""
    frame = E.skill_error("c-1", "ABANDONED", "the caller cancelled")
    assert list(frame) == ["type", "call_id", "error_code", "error_message"]
    assert frame == {"type": "skill_error", "call_id": "c-1", "error_code": "ABANDONED",
                     "error_message": "the caller cancelled"}


# ==========================================================================
# 2. typed parsing of the skill client frames
# ==========================================================================
#
# Two layers. SHAPE requires every field to have the expected JSON type and answers PARSE. Here
# that is a ValueError, which the consumer already answers with PARSE through `channels`.
# DOMAIN is the handlers': a well-typed value outside its set or bound is refused with the frame's
# own code (SkillFrameBad). Shape is checked in full before domain because the frame is parsed
# before any handler reads it.

ENV_IN = {"payload": {"v": 1, "kind": "skill.chunk", "call_id": "c-1"},
          "signature": "c2ln", "signing_kid": "kid-1"}
ENV_PARSED = SignedEnvelope(payload=ENV_IN["payload"], signature="c2ln", signing_kid="kid-1")

GOOD = {
    "skill_call": {"type": "skill_call", "envelope": ENV_IN, "mode_override": "stream"},
    "skill_ack": {"type": "skill_ack", "call_id": "c-1"},
    "skill_running": {"type": "skill_running", "call_id": "c-1"},
    "skill_chunk": {"type": "skill_chunk", "call_id": "c-1", "sequence": 3,
                    "chunk_type": "json", "envelope": ENV_IN},
    "skill_result": {"type": "skill_result", "call_id": "c-1", "envelope": ENV_IN},
    "skill_error": {"type": "skill_error", "call_id": "c-1", "error_code": "E_TOOL",
                    "error_message": "the tool failed"},
    "skill_cancel": {"type": "skill_cancel", "call_id": "c-1", "reason": "user abort"},
    "skill_resume": {"type": "skill_resume", "call_id": "c-1", "context_id": "ctx-1",
                     "action": "accept", "supplied_input": {"city": "Paris"},
                     "continuation_token": "rtok"},
}


def _parse(obj: dict):
    """The path a handler will take: the consumer's parse_client_frame, then the typed parse."""
    return E.parse_skill_frame(E.parse_client_frame(obj))


def _with(tag: str, **over) -> dict:
    obj = dict(GOOD[tag])
    for key, value in over.items():
        if value is _DROP:
            obj.pop(key, None)
        else:
            obj[key] = value
    return obj


_DROP = object()


def _env(**over) -> dict:
    env = dict(ENV_IN)
    for key, value in over.items():
        if value is _DROP:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def _bad(obj: dict) -> E.SkillFrameBad:
    with pytest.raises(E.SkillFrameBad) as caught:
        _parse(obj)
    return caught.value


def test_every_skill_client_frame_parses_to_its_typed_fields():
    """One typed value per variant, field for field."""
    assert set(GOOD) == E.SKILL_CLIENT_FRAMES, "one GOOD frame per skill tag"
    assert _parse(GOOD["skill_call"]) == E.SkillCallFrame(envelope=ENV_PARSED,
                                                          mode_override="stream")
    assert _parse(GOOD["skill_ack"]) == E.SkillAckFrame(call_id="c-1")
    assert _parse(GOOD["skill_running"]) == E.SkillRunningFrame(call_id="c-1")
    assert _parse(GOOD["skill_chunk"]) == E.SkillChunkFrame(
        call_id="c-1", sequence=3, chunk_type="json", envelope=ENV_PARSED)
    assert _parse(GOOD["skill_result"]) == E.SkillResultFrame(call_id="c-1",
                                                              envelope=ENV_PARSED)
    assert _parse(GOOD["skill_error"]) == E.SkillErrorFrame(
        call_id="c-1", error_code="E_TOOL", error_message="the tool failed")
    assert _parse(GOOD["skill_cancel"]) == E.SkillCancelFrame(call_id="c-1",
                                                              reason="user abort")
    assert _parse(GOOD["skill_resume"]) == E.SkillResumeFrame(
        call_id="c-1", context_id="ctx-1", action="accept",
        supplied_input={"city": "Paris"}, continuation_token="rtok")
    assert len({type(_parse(GOOD[t])) for t in GOOD}) == 8, "one type per variant"


def test_optional_fields_default_to_none_and_unknown_fields_are_ignored():
    """Optional fields are None when absent or null, and an extra key is ignored."""
    for absent in (_DROP, None):
        assert _parse(_with("skill_call", mode_override=absent)).mode_override is None
        assert _parse(_with("skill_cancel", reason=absent)).reason is None
        assert _parse(_with("skill_resume", supplied_input=absent)).supplied_input is None
    # supplied_input is any JSON value, kept as sent.
    for value in (5, "x", [1, 2], {"k": None}, False):
        assert _parse(_with("skill_resume", supplied_input=value)).supplied_input == value
    for action in ("accept", "decline", "cancel"):
        assert _parse(_with("skill_resume", action=action)).action == action
    assert _parse(_with("skill_ack", extra="ignored", call_id="c-9")) == E.SkillAckFrame("c-9")
    assert _parse(_with("skill_chunk", envelope=_env(extra=1))).envelope == ENV_PARSED


def test_a_non_skill_frame_is_not_parsed_here():
    """parse_skill_frame covers exactly SKILL_CLIENT_FRAMES; a ping or subscribe is the
    dispatcher's, and an UNKNOWN tag has no fields to type."""
    for obj in ({"type": "ping"}, {"type": "subscribe", "channels": []},
                {"type": "skill.call"}):
        with pytest.raises(ValueError, match="not a skill frame"):
            _parse(obj)
    assert isinstance(_parse(GOOD["skill_ack"]), E.SkillAckFrame)   # control


SHAPE_ERRORS = [
    ("call_id missing", _with("skill_ack", call_id=_DROP)),
    ("call_id an integer", _with("skill_ack", call_id=7)),
    ("call_id null", _with("skill_running", call_id=None)),
    ("call_id a list", _with("skill_cancel", call_id=["c-1"])),
    ("envelope missing", _with("skill_call", envelope=_DROP)),
    ("envelope null", _with("skill_result", envelope=None)),
    ("envelope a string", _with("skill_call", envelope="e")),
    ("payload missing", _with("skill_call", envelope=_env(payload=_DROP))),
    ("payload an array", _with("skill_result", envelope=_env(payload=[1]))),
    ("payload a string", _with("skill_chunk", envelope=_env(payload="{}"))),
    ("signature an integer", _with("skill_call", envelope=_env(signature=5))),
    ("signing_kid missing", _with("skill_result", envelope=_env(signing_kid=_DROP))),
    ("mode_override an integer", _with("skill_call", mode_override=1)),
    ("sequence missing", _with("skill_chunk", sequence=_DROP)),
    ("sequence a string", _with("skill_chunk", sequence="3")),
    ("sequence a float", _with("skill_chunk", sequence=3.0)),
    ("sequence a boolean", _with("skill_chunk", sequence=True)),
    ("sequence null", _with("skill_chunk", sequence=None)),
    ("chunk_type an integer", _with("skill_chunk", chunk_type=1)),
    ("error_code null", _with("skill_error", error_code=None)),
    ("error_message missing", _with("skill_error", error_message=_DROP)),
    ("reason an integer", _with("skill_cancel", reason=5)),
    ("context_id an integer", _with("skill_resume", context_id=1)),
    ("action unknown", _with("skill_resume", action="resume")),
    ("action missing", _with("skill_resume", action=_DROP)),
    ("continuation_token missing", _with("skill_resume", continuation_token=_DROP)),
]


def test_a_field_of_the_wrong_json_type_is_a_parse_error_not_a_domain_refusal():
    """Each of these is a PARSE failure here, a ValueError rather than SkillFrameBad.

    SkillFrameBad would carry a _BAD code instead.
    """
    ran = []
    for label, obj in SHAPE_ERRORS:
        with pytest.raises(ValueError) as caught:
            _parse(obj)
        assert not isinstance(caught.value, E.SkillFrameBad), label
        ran.append(label)
    assert len(ran) == len(SHAPE_ERRORS) == 26, "C6"
    # CONTROL: every frame those were derived from parses.
    for tag, obj in GOOD.items():
        _parse(obj)


def test_shape_is_checked_before_domain():
    """A missing envelope and a sequence past the 32-bit signed maximum are a PARSE failure.

    Shape checking never reaches the handler, so this is ValueError, not SKILL_CHUNK_BAD.
    """
    both = _with("skill_chunk", envelope=_DROP, sequence=2147483648)
    with pytest.raises(ValueError) as caught:
        _parse(both)
    assert not isinstance(caught.value, E.SkillFrameBad)
    # CONTROL: with the envelope back, the same sequence is the domain refusal.
    assert _bad(_with("skill_chunk", sequence=2147483648)).code == "SKILL_CHUNK_BAD"
    both = _with("skill_call", envelope=_env(signature=None), mode_override="lesson")
    with pytest.raises(ValueError) as caught:
        _parse(both)
    assert not isinstance(caught.value, E.SkillFrameBad)
    assert _bad(_with("skill_call", mode_override="lesson")).code == "SKILL_CALL_BAD"


def test_mode_override_is_sync_async_or_stream_and_lesson_is_skill_call_bad():
    """Design 4.7 and 2.12.7: the live skill_calls_mode_check refuses "lesson".

    It is refused at parse with SKILL_CALL_BAD, the code used for an unknown mode.
    """
    for mode in ("sync", "async", "stream"):
        assert _parse(_with("skill_call", mode_override=mode)).mode_override == mode
    assert E.SKILL_MODES == frozenset({"sync", "async", "stream"})
    for bad in ("lesson", "Sync", "SYNC", "batch", ""):
        refused = _bad(_with("skill_call", mode_override=bad))
        assert refused.code == E.CODE_SKILL_CALL_BAD == "SKILL_CALL_BAD", bad
        assert refused.frame() == {"type": "error", "code": "SKILL_CALL_BAD",
                                   "message": refused.message}
        assert "mode_override" in refused.message, refused.message


def test_sequence_is_an_i32_from_0_to_2147483647_and_outside_is_skill_chunk_bad():
    """Design 4.7, T22's parse half: `sequence` is a 32-bit signed integer and report_chunk
    requires `sequence >= 0`.

    2147483648 must be SKILL_CHUNK_BAD at parse, never an
    int4 overflow in the database."""
    assert E.SEQUENCE_MAX == 2147483647 == 2**31 - 1
    for ok in (0, 1, 2147483647):
        parsed = _parse(_with("skill_chunk", sequence=ok))
        assert parsed.sequence == ok and type(parsed.sequence) is int
    for bad in (2147483648, -1, -2147483648, 2**63, 10**30):
        refused = _bad(_with("skill_chunk", sequence=bad))
        assert refused.code == E.CODE_SKILL_CHUNK_BAD == "SKILL_CHUNK_BAD", bad
        assert "sequence" in refused.message, refused.message
    # The same through JSON text, the way the consumer reads a frame.
    text = json.dumps(_with("skill_chunk", sequence=0)).replace('"sequence": 0',
                                                                '"sequence": 2147483648')
    with pytest.raises(E.SkillFrameBad):
        E.parse_skill_frame(E.parse_client_frame(json.loads(text)))


def test_chunk_type_is_text_json_or_file_ref_and_anything_else_is_skill_chunk_bad():
    """`file_ref` parses; its handler refuses it until its slice
    (design 4.7, Q11), which is a handler's decision, not the parser's."""
    assert E.CHUNK_TYPES == frozenset({"text", "json", "file_ref"})
    for ok in ("text", "json", "file_ref"):
        assert _parse(_with("skill_chunk", chunk_type=ok)).chunk_type == ok
    for bad in ("TEXT", "binary", "", "file-ref"):
        refused = _bad(_with("skill_chunk", chunk_type=bad))
        assert refused.code == "SKILL_CHUNK_BAD", bad
        assert "chunk_type" in refused.message, refused.message


def test_skill_error_caps_error_code_at_128_and_error_message_at_4096_characters():
    """Design 4.7: values beyond the cap are refused with SKILL_ERROR_BAD.

    The cap is in CHARACTERS, not bytes: a two-byte character counts once.
    """
    assert (E.MAX_ERROR_CODE_CHARS, E.MAX_ERROR_MESSAGE_CHARS) == (128, 4096)
    for code in ("", "E" * 128, "é" * 128):
        assert _parse(_with("skill_error", error_code=code)).error_code == code
    for message in ("", "m" * 4096, "é" * 4096):
        assert _parse(_with("skill_error", error_message=message)).error_message == message
    for over in (dict(error_code="E" * 129), dict(error_code="é" * 129),
                 dict(error_message="m" * 4097), dict(error_message="é" * 4097)):
        refused = _bad(_with("skill_error", **over))
        assert refused.code == E.CODE_SKILL_ERROR_BAD == "SKILL_ERROR_BAD", over.keys()
        assert next(iter(over)) in refused.message, refused.message


def test_a_domain_refusal_is_not_a_value_error():
    """The consumer answers ValueError with PARSE (consumer.py's `except ValueError`). A
    domain refusal carries its own code, so it must not fall into that branch once a
    handler parses frames."""
    assert not issubclass(E.SkillFrameBad, ValueError)
    refused = E.SkillFrameBad("SKILL_CHUNK_BAD", "m")
    assert (refused.code, refused.message) == ("SKILL_CHUNK_BAD", "m")
    assert refused.frame() == E.error("SKILL_CHUNK_BAD", "m")


# ==========================================================================
# 3. the S1 adversarial review (a020e90): F1 and F3
# ==========================================================================

#: Every skill client frame that carries a frame-level `call_id` (skill_call's is inside its
#: signed payload, which the pipeline types).
CALL_ID_TAGS = ("skill_ack", "skill_running", "skill_chunk", "skill_result", "skill_error",
                "skill_cancel", "skill_resume")
#: Each is refused by apps/calls/repository.py validate_call_id, which the HTTP path already
#: runs before a call_id is
#: logged or stored. The second would forge a line in this repo's plain-text log format
#: (meshychat/settings.py, "%(asctime)s %(levelname)s %(name)s %(message)s").
BAD_CALL_IDS = [
    ("empty", ""),
    ("a newline", "a\n2026-09-24 12:00:00,000 INFO apps.wsgw forged"),
    ("a carriage return", "a\rb"),
    ("a NUL", "a\x00b"),
    ("DEL", "a\x7fb"),
    ("the C1 control NEL", "a\x85b"),
    ("a lone high surrogate", "a\ud800"),
    ("a lone low surrogate", "\udfffa"),
    ("201 bytes", "x" * 201),
    ("202 bytes in 101 characters", "é" * 101),
    ("a million characters", "x" * 1_000_000),
]
#: CONTROL (C1): what the same validator admits, at and under its 200-byte cap.
GOOD_CALL_IDS = ["c-1", "x" * 200, "é" * 100, "a call id with spaces",
                 "café-\U0001F600"]


def _shape_error(obj: dict) -> str:
    """The frame fails SHAPE (a ValueError, answered PARSE), and NOT the domain layer."""
    with pytest.raises(ValueError) as caught:
        _parse(obj)
    assert not isinstance(caught.value, E.SkillFrameBad)
    return str(caught.value)


def test_every_frame_call_id_is_a_call_id_and_anything_else_is_a_parse_error():
    """Review F1. A frame's call_id is typed as the HTTP path types one (validate_call_id:
    not empty, at most 200 UTF-8 bytes, no Unicode Cc character), so none of these ever
    reaches a log line, a statement or a channel name. The refusal does not echo the value:
    its message is one short printable line."""
    assert set(CALL_ID_TAGS) == {tag for tag, obj in GOOD.items() if "call_id" in obj}
    for tag in CALL_ID_TAGS:
        for ok in GOOD_CALL_IDS:
            assert _parse(_with(tag, call_id=ok)).call_id == ok, (tag, ok[:20])
        for label, bad in BAD_CALL_IDS:
            text = _shape_error(_with(tag, call_id=bad))
            assert f"`{tag}.call_id`" in text, (tag, label, text)
            assert text.isprintable() and "forged" not in text, (tag, label, text)
            assert len(text) < 200, (tag, label, len(text))
    # The same through JSON text, the way the consumer reads a frame: json.loads keeps a
    # lone surrogate escape, so shape validation must refuse it.
    _shape_error(json.loads('{"type": "skill_ack", "call_id": "a\\ud800"}'))
    # A call_id failure is shape, so it wins over a domain failure in the same frame.
    for obj in (_with("skill_chunk", call_id="", sequence=2147483648),
                _with("skill_error", call_id="a\nb", error_code="E" * 129)):
        _shape_error(obj)
    # CONTROL: with a good call_id, the same frames are the domain refusals.
    assert _bad(_with("skill_chunk", sequence=2147483648)).code == "SKILL_CHUNK_BAD"
    assert _bad(_with("skill_error", error_code="E" * 129)).code == "SKILL_ERROR_BAD"


def _set(tag: str, path: str, value) -> dict:
    """GOOD[tag] with one field set; `envelope.x` sets a field of the signed envelope."""
    if path.startswith("envelope."):
        return _with(tag, envelope=_env(**{path.split(".", 1)[1]: value}))
    return _with(tag, **{path: value})


#: Every typed string of every skill frame other than call_id, with an admitted value.
TEXT_FIELDS = [
    ("skill_call", "mode_override", "sync"),
    ("skill_call", "envelope.signature", "c2ln"),
    ("skill_call", "envelope.signing_kid", "kid-1"),
    ("skill_chunk", "chunk_type", "text"),
    ("skill_chunk", "envelope.signature", "c2ln"),
    ("skill_chunk", "envelope.signing_kid", "kid-1"),
    ("skill_result", "envelope.signature", "c2ln"),
    ("skill_result", "envelope.signing_kid", "kid-1"),
    ("skill_error", "error_code", "E_TOOL"),
    ("skill_error", "error_message", "the tool failed"),
    ("skill_cancel", "reason", "user abort"),
    ("skill_resume", "context_id", "ctx-1"),
    ("skill_resume", "action", "accept"),
    ("skill_resume", "continuation_token", "rtok"),
]


def test_no_string_field_holds_a_nul_or_a_lone_surrogate():
    """Review F1, the other half. A lone surrogate survives json.loads, and PostgreSQL text
    cannot hold U+0000: psycopg raises
    DataError for a NUL, which is not a ValueError, and UnicodeEncodeError for a surrogate,
    which the consumer's `except ValueError` would mislabel. Both are refused at shape, in
    every string field. Only NUL: a newline or a tab is text, and an error_message may carry
    a stack trace (the log-injection rule is call_id's, above)."""
    assert len(TEXT_FIELDS) == 14, "C6"
    for tag, path, ok in TEXT_FIELDS:
        for bad in (ok + "\x00", "\x00", ok + "\ud800", "\udfff" + ok, ok + "\ud83d"):
            text = _shape_error(_set(tag, path, bad))
            assert f"{path.split('.')[-1]}`" in text, (tag, path, text)
            text.encode("utf-8")      # the message itself holds no surrogate
            assert "\x00" not in text
        assert _parse(_set(tag, path, ok)), (tag, path)          # CONTROL
    # CONTROL (C1): what IS admitted in free text. A non-BMP character is one code point
    # once json.loads has joined its escaped surrogate PAIR.
    frame = json.loads('{"type": "skill_error", "call_id": "c-1", "error_code": "E",'
                       ' "error_message": "line one\\nline two\\t\\ud83d\\ude00 \\u00e9"}')
    assert _parse(frame).error_message == "line one\nline two\t\U0001F600 é"
    assert _parse(_with("skill_cancel", reason="a\nb\x01c")).reason == "a\nb\x01c"


def test_shape_is_checked_before_domain_in_the_chunk_and_error_frames():
    """Review F3. test_shape_is_checked_before_domain pins the rule for `sequence` and
    `mode_override` only; a chunk_type or error-cap check moved ahead of a later field's
    shape survived it. Each pair: the frame with a shape failure AND a domain failure is a
    ValueError, and the same frame with the shape repaired is the domain refusal."""
    pairs = [
        (_with("skill_chunk", envelope=_DROP, chunk_type="bogus"),
         _with("skill_chunk", chunk_type="bogus"), "SKILL_CHUNK_BAD"),
        (_with("skill_chunk", envelope=_env(signing_kid=_DROP), chunk_type="bogus"),
         _with("skill_chunk", chunk_type="bogus"), "SKILL_CHUNK_BAD"),
        (_with("skill_chunk", call_id=_DROP, sequence=-1),
         _with("skill_chunk", sequence=-1), "SKILL_CHUNK_BAD"),
        (_with("skill_error", error_code="E" * 129, error_message=_DROP),
         _with("skill_error", error_code="E" * 129), "SKILL_ERROR_BAD"),
        (_with("skill_error", error_code="E" * 129, error_message=5),
         _with("skill_error", error_code="E" * 129), "SKILL_ERROR_BAD"),
        (_with("skill_error", error_message="m" * 4097, call_id=_DROP),
         _with("skill_error", error_message="m" * 4097), "SKILL_ERROR_BAD"),
    ]
    for both, domain_only, code in pairs:
        _shape_error(both)
        assert _bad(domain_only).code == code       # CONTROL


# ==========================================================================
# 4. the builders refuse a value the frame type cannot hold (review F2)
# ==========================================================================

def test_skill_invoke_refuses_a_row_without_a_caller_or_a_signature():
    """Review F2. caller_peer_id, args_signature and args_signing_kid are NULL for a
    dashboard_test row (sql/006_skill_calls.sql:7, :19-20). The frame requires non-null
    identifiers and strings, so such a row cannot become a skill_invoke. Here `str(None)` wrote "None" and a None
    signature went out as null."""
    unsigned = SignedEnvelope(payload=ENV.payload, signature=None, signing_kid=None)
    no_sig = SignedEnvelope(payload=ENV.payload, signature=None, signing_kid="kid-1")
    no_kid = SignedEnvelope(payload=ENV.payload, signature="c2ln", signing_kid=None)
    no_payload = SignedEnvelope(payload=None, signature="c2ln", signing_kid="kid-1")
    refused = [dict(caller_peer_id=None), dict(target_peer_id=None),
               dict(caller_peer_id=str(CALLER)), dict(target_peer_id=TARGET.int),
               dict(call_id=None), dict(skill_name=None), dict(skill_version=None),
               dict(envelope=unsigned), dict(envelope=no_sig), dict(envelope=no_kid),
               dict(envelope=no_payload)]
    for over in refused:
        with pytest.raises(ValueError):
            _invoke(**over)
    # The same envelope guard in the two other frames that carry one.
    for env in (unsigned, no_sig, no_kid, no_payload):
        with pytest.raises(ValueError):
            E.skill_chunk("c-1", 0, "text", env)
        with pytest.raises(ValueError):
            E.skill_result("c-1", "completed", env)
    # CONTROL: the well-typed row builds, and so do the other two.
    assert _invoke()["caller_peer_id"] == str(CALLER)
    assert E.skill_chunk("c-1", 0, "text", ENV)["envelope"] == ENV_WIRE
    assert E.skill_result("c-1", "failed", ENV)["envelope"] == ENV_WIRE
