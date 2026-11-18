"""Checks 1-4 of a signed envelope.

`apps.calls.envelope.check_claims` is the one definition of checks 1-4: HTTP invoke runs it
inside `verify_payload`, and the board door and the in-process path run it alone (divergence
038). These tests pin known answers for the claim checks. The review of 038 found four places
where nothing pinned the behavior. Each test below names the mutant it kills.

No database: checks 1-4 need no key. Every refusal is paired with a payload that passes.

Run:  .venv/Scripts/python.exe -m pytest tests/test_envelope_claims.py -q
"""
from __future__ import annotations

import datetime as _dt
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from apps.calls.envelope import (  # noqa: E402
    KIND_SKILL_CALL, KIND_SKILL_RESULT, REPLAY_WINDOW_SECS, EnvelopeClaims, SignedEnvelope,
    check_claims, verify_payload,
)
from apps.calls.errors import SignatureFailed  # noqa: E402

UTC = _dt.timezone.utc
NOW = _dt.datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC)
ME = uuid.UUID("5d0c9d0e-6b7a-4a57-9a51-2f3a4b5c6d7e")
OTHER = uuid.UUID("0f1e2d3c-4b5a-4968-8776-655443322110")
_ABSENT = object()


def _payload(**edits) -> dict:
    """A complete skill.call payload issued at NOW by ME, with `edits` applied."""
    payload = {"v": 1, "kind": KIND_SKILL_CALL, "call_id": "c-1", "caller_peer_id": str(ME),
               "target_peer_id": str(OTHER), "skill_name": "memory.write",
               "skill_version": "1", "args": {}, "args_attachments": [],
               "issued_at": "2026-09-24T12:00:00Z"}
    for name, value in edits.items():
        if value is _ABSENT:
            payload.pop(name, None)
        else:
            payload[name] = value
    return payload


def _refused(payload) -> SignatureFailed:
    with pytest.raises(SignatureFailed) as exc:
        check_claims(payload, ME, KIND_SKILL_CALL, NOW)
    return exc.value


def _rfc3339(at: _dt.datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def test_a_complete_payload_passes_and_names_its_signer():
    """The positive control for every refusal in this file."""
    assert check_claims(_payload(), ME, KIND_SKILL_CALL, NOW) == EnvelopeClaims(
        kind=KIND_SKILL_CALL, peer_id=ME, issued_at=NOW)


# --------------------------------------------------------------------------- #
# The order (review finding F2)
# --------------------------------------------------------------------------- #

def test_the_checks_answer_in_verify_rs_order():
    """THE ORDER OF THE CHECKS IS THE CONTRACT (the module's own header): a client tells
    "your clock is wrong" from "you signed for someone else" by which check answers.

    The payload starts wrong on all four counts. Check 1 answers. Mend the version and
    check 2 answers; mend the kind and check 3 answers; mend the time and check 4 answers;
    mend the caller and it passes. Swapping any two neighbouring checks changes one of
    these answers. Mutant killed: checks 3 and 4 swapped (the review's M1, which survived
    the whole suite), and likewise 1 with 2 and 2 with 3."""
    payload = _payload(v=2, kind=KIND_SKILL_RESULT, issued_at="2026-09-23T12:00:00Z",
                       caller_peer_id=str(OTHER))
    steps = [("v", 1, "E_SIG_VERSION_UNSUPPORTED", 400),
             ("kind", KIND_SKILL_CALL, "E_SIG_KIND_MISMATCH", 400),
             ("issued_at", "2026-09-24T12:00:00Z", "E_SIG_REPLAY", 401),
             ("caller_peer_id", str(ME), "E_SIG_IDENTITY_MISMATCH", 401)]
    for field, mended, code, status in steps:
        err = _refused(payload)
        assert (err.code, err.http_status) == (code, status), (field, err.code, err.message)
        payload[field] = mended
    assert check_claims(payload, ME, KIND_SKILL_CALL, NOW).peer_id == ME


# --------------------------------------------------------------------------- #
# Check 1, the version (review finding F4)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("v", [True, False, 1.0, -1, 2 ** 64, "1", None, _ABSENT],
                         ids=["true", "false", "float-1.0", "negative", "past-u64", "string",
                              "json-null", "absent"])
def test_the_version_is_read_as_a_json_integer(v):
    """The version is an unsigned 64-bit integer, else Malformed. A boolean, a float (1.0
    included), a negative number, or one past that range is not a valid version. Each is 400
    E_SIG_BAD "payload.v missing or not a
    number". None of them is read as version 1, and none is VersionUnsupported.

    Mutants killed: the boolean guard removed (`true` then passes as version 1: the
    review's M11, which survived the whole suite); the u64 range dropped (-1 and 2**64 are
    then VersionUnsupported, as they were before this test)."""
    err = _refused(_payload(v=v))
    assert (err.code, err.http_status) == ("E_SIG_BAD", 400), (err.code, err.message)
    assert "payload.v missing or not a number" in err.message


def test_a_version_that_is_a_u64_is_compared_with_1():
    """The controls for the test above: 1 passes. 0, 2 and u64's largest value are numbers,
    so they are compared with 1 and refused as VersionUnsupported, not as Malformed."""
    assert check_claims(_payload(v=1), ME, KIND_SKILL_CALL, NOW).kind == KIND_SKILL_CALL
    for v in (0, 2, 2 ** 64 - 1):
        err = _refused(_payload(v=v))
        assert (err.code, err.http_status) == ("E_SIG_VERSION_UNSUPPORTED", 400), v


# --------------------------------------------------------------------------- #
# Check 3, the window (review finding F3)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("seconds_ago,inside", [
    (0, True), (120, True), (-120, True), (120.999999, True), (-120.999999, True),
    (121, False), (-121, False), (86400, False)],
    ids=["now", "120-past", "120-ahead", "120.999999-past", "120.999999-ahead", "121-past",
         "121-ahead", "a-day-past"])
def test_the_window_counts_whole_seconds_as_verify_rs_does(seconds_ago, inside):
    """The absolute whole-second skew is compared with REPLAY_WINDOW_SECS. It truncates
    toward zero, so an envelope 120.999999 s old, or that far ahead, is inside the window, and
    121 s is the first second outside it, on either side.

    Comparing fractional seconds would refuse 120.5 s. Mutants killed: a 117 s window (the review's M2, which survived the whole
    suite, since the tests stopped at 115 and 121.5 s); `>=` in place of `>`; fractional
    seconds; a 121 s window; flooring the signed skew instead of truncating it (the
    120.999999-ahead case)."""
    assert REPLAY_WINDOW_SECS == 120
    issued = NOW - _dt.timedelta(seconds=seconds_ago)
    payload = _payload(issued_at=_rfc3339(issued))
    if inside:
        assert check_claims(payload, ME, KIND_SKILL_CALL, NOW).issued_at == issued
    else:
        err = _refused(payload)
        assert (err.code, err.http_status) == ("E_SIG_REPLAY", 401), err.message


# --------------------------------------------------------------------------- #
# Check 3, instants outside Python's datetime range (review finding F1)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("issued_at", [
    "0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00", "0000-01-01T00:00:00Z",
    "0000-02-29T00:00:00Z", "0001-01-01T00:00:00Z", "9999-12-31T23:59:59.999999999Z"])
def test_an_instant_far_outside_the_window_is_a_replay_whatever_its_year(issued_at):
    """`issued_at` is attacker-controlled, so extreme values must answer Replay without
    error. Python's datetime stops at year 1 and year 9999. The first two values are inside Python's range
    as written and outside it in UTC. `astimezone` raised OverflowError on them: an
    unhandled 500 on the board door and an exception out of invoke. Year 0 is a valid date in
    the proleptic Gregorian calendar, and a leap year (hence 0000-02-29), and it was E_SIG_BAD
    here.

    All six are Replay. So are they when the payload also names another caller (check 3
    answers before check 4), and so is invoke's `verify_payload`, which answers before it
    needs a key. Mutant killed: the old `fromisoformat(...).astimezone(utc)` parse."""
    err = _refused(_payload(issued_at=issued_at))
    assert (err.code, err.http_status) == ("E_SIG_REPLAY", 401), (err.code, err.message)
    assert _refused(_payload(issued_at=issued_at, caller_peer_id=str(OTHER))).code == \
        "E_SIG_REPLAY"
    with pytest.raises(SignatureFailed) as exc:
        verify_payload(None, SignedEnvelope(payload=_payload(issued_at=issued_at),
                                            signature="", signing_kid=""),
                       ME, KIND_SKILL_CALL, NOW)
    assert exc.value.code == "E_SIG_REPLAY"


# --------------------------------------------------------------------------- #
# Check 3, the grammar (review finding F8)
# --------------------------------------------------------------------------- #

#: Forms the parser refuses as Malformed: 400 E_SIG_BAD. The first ten are ISO 8601 forms
#: Python's `fromisoformat` accepted, so this parser has to reject them explicitly.
_REFUSED_FORMS = [
    ("basic", "20260924T120000Z"),
    ("week-date", "2026-W39-4T12:00:00Z"),
    ("no-seconds", "2026-09-24T12:00Z"),
    ("hour-only", "2026-09-24T12Z"),
    ("other-separator", "2026-09-24X12:00:00Z"),
    ("empty-fraction", "2026-09-24T12:00:00.Z"),
    ("comma-fraction", "2026-09-24T12:00:00,5Z"),
    ("offset-without-colon", "2026-09-24T12:00:00+0000"),
    ("offset-hours-only", "2026-09-24T12:00:00+00"),
    ("offset-with-seconds", "2026-09-24T12:00:00+00:00:00"),
    ("ordinal-date", "2026-267T12:00:00Z"),
    ("date-only", "2026-09-24"),
    ("no-offset", "2026-09-24T12:00:00"),
    ("trailing-newline", "2026-09-24T12:00:00Z\n"),
    ("leading-space", " 2026-09-24T12:00:00Z"),
    ("non-ascii-digits", "٢٠٢٦-09-24T12:00:00Z"),
    ("offset-24-hours", "2026-09-24T12:00:00+24:00"),
    ("offset-minute-60", "2026-09-24T12:00:00+00:60"),
    ("hour-24", "2026-09-24T24:00:00Z"),
    ("minute-60", "2026-09-24T12:60:00Z"),
    ("second-61", "2026-09-24T12:00:61Z"),
    ("september-31", "2026-09-31T12:00:00Z"),
    ("february-29-not-leap", "2026-02-29T12:00:00Z"),
    ("february-30-year-0", "0000-02-30T00:00:00Z"),
]


@pytest.mark.parametrize("issued_at", [c[1] for c in _REFUSED_FORMS],
                         ids=[c[0] for c in _REFUSED_FORMS])
def test_an_malformed_issued_at_value_is_refused(issued_at):
    """Mutants killed: the old `fromisoformat` parse (the first ten cases); `\\d` in place
    of `[0-9]` (non-ascii-digits); `$` in place of a full match (trailing-newline); the
    optional offset colon that the repository's other RFC 3339 parsers allow
    (offset-without-colon)."""
    err = _refused(_payload(issued_at=issued_at))
    assert (err.code, err.http_status) == ("E_SIG_BAD", 400), (err.code, err.message)
    assert "payload.issued_at not RFC 3339" in err.message


#: Accepted forms, each naming NOW (12:00:00 UTC). The lowercase `z` and the U+2212 minus
#: sign were once refused here although RFC 3339 parsers accept them; they are accepted now.
_ACCEPTED_FORMS = [
    ("Z", "2026-09-24T12:00:00Z"),
    ("lowercase-t", "2026-09-24t12:00:00Z"),
    ("space", "2026-09-24 12:00:00Z"),
    ("lowercase-z", "2026-09-24T12:00:00z"),
    ("plus-zero", "2026-09-24T12:00:00+00:00"),
    ("minus-zero", "2026-09-24T12:00:00-00:00"),
    ("east", "2026-09-24T17:30:00+05:30"),
    ("west", "2026-09-24T06:00:00-06:00"),
    ("unicode-minus", "2026-09-24T06:00:00−06:00"),
    ("widest-offset", "2026-09-25T11:59:00+23:59"),
    ("one-fraction-digit", "2026-09-24T12:00:00.0Z"),
    ("twelve-fraction-digits", "2026-09-24T12:00:00.000000000999Z"),
]


@pytest.mark.parametrize("issued_at", [c[1] for c in _ACCEPTED_FORMS],
                         ids=[c[0] for c in _ACCEPTED_FORMS])
def test_an_issued_at_value_is_read_as_the_same_instant(issued_at):
    """The positive controls for the grammar, and the offset's sign: each names NOW. A
    mutant that adds the offset instead of subtracting it moves `east` and `west` by hours
    and is refused as a replay."""
    assert check_claims(_payload(issued_at=issued_at), ME, KIND_SKILL_CALL,
                        NOW).issued_at == NOW


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--no-header", "-p", "no:cacheprovider"]))
