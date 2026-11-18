"""One RFC 3339 rule behind three wire surfaces (apps/peers/timefmt.py).

`apps/grants/wire.py` and `apps/calls/wire.py` each carried a copy of timefmt's
formatter and parser (byte-identical bodies; only the ValueError texts differed), and
`apps/mesh/wait.py` a copy of the formatter. The copies are gone and each module binds
timefmt's functions under the name its callers import. grants/wire.py's docstring used to
say `test_grants.py` pinned its copy to timefmt's; no such test existed. This file is that
pin. It proves:

  * the binding itself (the same function object), so a copy cannot quietly come back;
  * the bytes each name emits, through each name: no fraction, 3 digits,
    6 digits), a literal `Z`, an offset converted rather than echoed, null for None, and
    the refusal of a naive datetime with its exact text;
  * the datetime each parser returns for the same inputs, and the ValueError text each
    one keeps. The texts are wire: `apps/calls/views.py` answers
    `invalid `since`: {exc}` (400) and `apps/grants/views.py`
    `invalid value for `expires_at`: {exc}` (422), so each is asserted character for
    character, and the routes are checked end to end in test_calls_http.py
    (`test_the_list_validates_role_status_and_since`) and test_grants.py
    (`test_a_malformed_expires_at_is_422_with_the_parsers_text`).

The mesh wait's binding is pinned beside its body test in tests/test_mesh_graphread.py
(`test_the_wait_timestamp_is_timefmts_formatter`), which already loads that module.

No database and no `django.setup()`: the three modules import on their own, and keeping
this file light is what lets it run first when the rule is in doubt.
"""
from __future__ import annotations

import datetime as _dt
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")

from apps.calls import wire as calls_wire  # noqa: E402
from apps.grants import wire as grants_wire  # noqa: E402
from apps.peers import timefmt  # noqa: E402

UTC = _dt.timezone.utc
T = _dt.datetime(2026, 9, 21, 15, 45, 26, tzinfo=UTC)

FORMATTERS = [
    pytest.param(timefmt.format_optional_rfc3339, id="timefmt"),
    pytest.param(grants_wire.optional_rfc3339, id="grants"),
    pytest.param(calls_wire.optional_rfc3339, id="calls"),
]

PARSERS = [
    pytest.param(timefmt.parse_rfc3339, id="timefmt"),
    pytest.param(grants_wire.parse_rfc3339, id="grants"),
    pytest.param(calls_wire.parse_rfc3339, id="calls"),
]


# ==================================================================== formatter

def test_the_three_formatter_names_are_one_function():
    """Identity, not equality of outputs: a re-derived copy with the same outputs today is
    exactly what drifted into three files before, and what this file exists to refuse."""
    assert grants_wire.optional_rfc3339 is timefmt.format_optional_rfc3339
    assert calls_wire.optional_rfc3339 is timefmt.format_optional_rfc3339


@pytest.mark.parametrize("fmt", FORMATTERS)
def test_each_formatter_name_emits_canonical_timestamp_bytes(fmt):
    """The known answers test_peers.py pins for the signature contract, through every
    name a wire module exports, plus their None rule: None is null."""
    assert fmt(T) == "2026-09-21T15:45:26Z"
    assert fmt(T.replace(microsecond=500000)) == "2026-09-21T15:45:26.500Z"
    assert fmt(T.replace(microsecond=500001)) == "2026-09-21T15:45:26.500001Z"
    assert fmt(None) is None
    # An offset is converted, not echoed: the formatter holds UTC, so the output of the same
    # instant is the same bytes whatever zone the Python value wore.
    assert fmt(T.astimezone(_dt.timezone(_dt.timedelta(hours=2)))) == "2026-09-21T15:45:26Z"


@pytest.mark.parametrize("fmt", FORMATTERS + [pytest.param(timefmt.format_rfc3339, id="strict")])
def test_each_formatter_name_refuses_a_naive_datetime_with_the_same_text(fmt):
    """A naive datetime means a column read as `timestamp` or a value built by hand;
    guessing its zone would move it by hours, so every name raises, with one text."""
    with pytest.raises(ValueError) as exc:
        fmt(T.replace(tzinfo=None))
    assert str(exc.value) == "refusing to format a naive datetime: UTC must be explicit"


def test_the_strict_formatter_still_takes_no_none():
    """`timefmt.format_rfc3339` is the signature-contract formatter: a None reaching it is
    a caller's bug, not an `Option`, and it keeps failing the way it always did rather
    than quietly returning null."""
    with pytest.raises(AttributeError):
        timefmt.format_rfc3339(None)


def test_the_plus_zero_spelling_still_derives_from_the_shared_bytes():
    """calls' `format_rfc3339_offset` (receipt `completedAt`, `E_APPROVAL_PENDING.expires_at`)
    rewrites the shared `Z` form to `+00:00` and renders None as "" - both unchanged."""
    assert calls_wire.format_rfc3339_offset(T) == "2026-09-21T15:45:26+00:00"
    assert calls_wire.format_rfc3339_offset(T.replace(microsecond=500000)) == \
        "2026-09-21T15:45:26.500+00:00"
    assert calls_wire.format_rfc3339_offset(None) == ""


# ======================================================================= parser

ACCEPTED = [
    ("2026-09-21T15:45:26Z", T),
    ("2026-09-21t15:45:26z", T),
    ("2026-09-21 15:45:26Z", T),
    # Surrounding whitespace is stripped, as every copy did.
    ("  2026-09-21T15:45:26Z\n", T),
    ("2026-09-21T15:45:26.5Z", T.replace(microsecond=500000)),
    ("2026-09-21T15:45:26.500001Z", T.replace(microsecond=500001)),
    # A seventh fraction digit is dropped, not rounded.
    ("2026-09-21T15:45:26.1234567Z", T.replace(microsecond=123456)),
    ("2026-09-21T17:45:26+02:00", T),
    # This parser accepts a colon-less offset; the signature door's parser in
    # apps/calls/envelope.py deliberately uses a different rule.
    ("2026-09-21T17:45:26+0200", T),
    ("2026-09-21T10:15:26-05:30", T),
]


@pytest.mark.parametrize("text,expected", ACCEPTED)
def test_the_three_parsers_return_the_same_aware_utc_instant(text, expected):
    for parse in (timefmt.parse_rfc3339, grants_wire.parse_rfc3339, calls_wire.parse_rfc3339):
        got = parse(text)
        assert got == expected, (parse.__module__, text)
        assert got.tzinfo == UTC, (parse.__module__, text)
    assert timefmt.match_rfc3339(text) == expected


REFUSED = ["yesterday", "", "2026-09-21T15:45:26", "2026-09-21", "20260921T154526Z",
           "2026-09-21T15:45Z", "2026-09-21T15:45:26.Z", "2026-09-21T15:45:26+2:00"]


@pytest.mark.parametrize("text", REFUSED)
def test_the_three_parsers_refuse_the_same_shapes_and_keep_their_own_texts(text):
    """One grammar, three messages. The shared helper answers None; each surface turns that
    into the ValueError text its route has always put on the wire, with the raw input
    repr'd after it, unstripped."""
    assert timefmt.match_rfc3339(text) is None
    with pytest.raises(ValueError) as exc:
        timefmt.parse_rfc3339(text)
    assert str(exc.value) == f"issued_at is not RFC 3339: {text!r}"
    with pytest.raises(ValueError) as exc:
        grants_wire.parse_rfc3339(text)
    assert str(exc.value) == f"not an RFC 3339 timestamp: {text!r}"
    with pytest.raises(ValueError) as exc:
        calls_wire.parse_rfc3339(text)
    assert str(exc.value) == f"not an RFC 3339 timestamp: {text!r}"


@pytest.mark.parametrize("not_text", [None, 5, 1.5, b"2026-09-21T15:45:26Z",
                                      ["2026-09-21T15:45:26Z"], {"since": "x"}])
def test_a_non_string_is_refused_before_matching_with_each_surfaces_text(not_text):
    """The isinstance check stays in each wrapper, ahead of the shared `.strip()`: a JSON
    number or list in `expires_at` is a ValueError with the surface's own words, never an
    AttributeError out of the helper."""
    with pytest.raises(ValueError) as exc:
        timefmt.parse_rfc3339(not_text)
    assert str(exc.value) == "issued_at must be a string"
    with pytest.raises(ValueError) as exc:
        grants_wire.parse_rfc3339(not_text)
    assert str(exc.value) == "expected an RFC 3339 timestamp string"
    with pytest.raises(ValueError) as exc:
        calls_wire.parse_rfc3339(not_text)
    assert str(exc.value) == "timestamp must be a string"


@pytest.mark.parametrize("parse", PARSERS)
def test_an_in_grammar_value_python_cannot_hold_raises_as_every_copy_did(parse):
    """The grammar admits a `+99:99` offset and a 13th month; what refuses them is Python
    (`timezone()` and `strptime`), with Python's own ValueError text, from inside the
    shared body exactly as from inside each copy. Pinned so the helper is not later
    'tidied' into swallowing them as a not-RFC-3339 message or letting them through."""
    with pytest.raises(ValueError) as exc:
        parse("2026-09-21T15:45:26+99:99")
    assert "offset must be a timedelta" in str(exc.value)
    with pytest.raises(ValueError) as exc:
        parse("2026-13-21T15:45:26Z")
    assert "does not match format" in str(exc.value)


@pytest.mark.parametrize("parse", PARSERS)
def test_each_parser_round_trips_through_the_shared_formatter(parse):
    """What test_peers.py proves for the signature surface, for every surface: the server
    rebuilds exactly a client-formatted string: no fraction, 3 digits for whole milliseconds,
    else 6."""
    for text in ("2026-09-21T15:45:26Z", "2026-09-21T15:45:26.500Z",
                 "2026-09-21T15:45:26.500001Z"):
        assert timefmt.format_optional_rfc3339(parse(text)) == text
