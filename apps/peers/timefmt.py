"""Timestamp formatting for RFC 3339 UTC values.

WHY THIS IS ITS OWN MODULE AND NOT AN `isoformat()` CALL: the key-publish proof is an
Ed25519 signature over `JCS({peer_id, new_pubkey, nonce, issued_at})`.
The client signs a string it formatted; the server parses that string and re-serializes
it to produce the canonical bytes. If the two formatters disagree by one character the signature
does not verify, and the failure looks like a bad key rather than a bad clock format.

The formatter emits RFC 3339 with no fractional part
when the sub-second component is zero, otherwise the shortest of 3/6/9 digits that
still shows every non-zero digit, and a literal `Z` for UTC.

Python's `datetime.isoformat()` does NOT match: it emits `+00:00` rather than `Z`, and
it emits 6 fractional digits whenever microsecond is non-zero (never 3).

The zero-fraction case is `"%Y-%m-%dT%H:%M:%SZ"`; the deployed production client uses
the same format for `issued_at`.

ONE RULE, FOUR SURFACES. `apps/grants/wire.py` and `apps/calls/wire.py` each carried a
copy of the formatter and the parser below (byte-identical bodies; only the ValueError
texts their routes put on the wire differed), and `apps/mesh/wait.py` a fourth copy of the
formatter. Each now binds the functions here under the name its callers import, keeping
its own message texts, so the one-character drift the paragraph above describes cannot
open between copies. `tests/test_rfc3339_one_rule.py` pins the binding and the texts.
"""
from __future__ import annotations

import datetime as _dt
import re

_RFC3339 = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})[Tt ](?P<time>\d{2}:\d{2}:\d{2})"
    r"(?P<frac>\.\d+)?(?P<off>[Zz]|[+-]\d{2}:?\d{2})$"
)


def format_rfc3339(value: _dt.datetime) -> str:
    """Format as RFC 3339 UTC with an auto-sized fractional second and `Z`.

    Python's clock resolution is microseconds, so a 9-digit branch is unreachable here;
    the 3- and 6-digit cases make the supported precision explicit.
    """
    if value.tzinfo is None:
        # psycopg returns aware datetimes for `timestamptz`; a naive one means the
        # column was read as `timestamp` or constructed by hand, and guessing its zone
        # would move the value by hours.
        raise ValueError("refusing to format a naive datetime: UTC must be explicit")
    utc = value.astimezone(_dt.timezone.utc)
    stem = utc.strftime("%Y-%m-%dT%H:%M:%S")
    micro = utc.microsecond
    if micro == 0:
        return stem + "Z"
    if micro % 1000 == 0:                      # whole milliseconds -> 3 digits
        return f"{stem}.{micro // 1000:03d}Z"
    return f"{stem}.{micro:06d}Z"              # -> 6 digits


def format_optional_rfc3339(value: _dt.datetime | None) -> str | None:
    """`format_rfc3339` for an optional datetime: None is null.

    The one body behind `apps.grants.wire.optional_rfc3339` and `apps.calls.wire.optional_rfc3339`,
    which were copies of it; each keeps its name and emits the bytes it always did.
    """
    if value is None:
        return None
    return format_rfc3339(value)


def match_rfc3339(text: str) -> _dt.datetime | None:
    """The shape match and offset arithmetic behind the three `parse_rfc3339`s (this
    module's, `apps.grants.wire`'s and `apps.calls.wire`'s): an aware UTC datetime, or
    None when `text` is not RFC 3339.

    None rather than a raise because the three callers differ ONLY in their ValueError
    texts, and those texts are wire: calls' goes out after "invalid `since`: " (400) and
    grants' after "invalid value for `expires_at`: " (422). Each caller raises its own,
    unchanged. Anything else this raises (a `+99:99` offset, a month 13) propagates as it
    did from every copy.
    """
    m = _RFC3339.match(text.strip())
    if not m:
        return None
    frac = m.group("frac") or ""
    micro = 0
    if frac:
        digits = (frac[1:] + "000000")[:6]
        micro = int(digits)
    off = m.group("off")
    if off in ("Z", "z"):
        tz = _dt.timezone.utc
    else:
        sign = 1 if off[0] == "+" else -1
        body = off[1:].replace(":", "")
        tz = _dt.timezone(sign * _dt.timedelta(hours=int(body[:2]), minutes=int(body[2:])))
    base = _dt.datetime.strptime(f"{m.group('date')}T{m.group('time')}", "%Y-%m-%dT%H:%M:%S")
    return base.replace(microsecond=micro, tzinfo=tz).astimezone(_dt.timezone.utc)


def parse_rfc3339(text: str) -> _dt.datetime:
    """Parse the `issued_at` a client sent. Returns an aware UTC datetime.

    Deliberately strict about SHAPE but tolerant about OFFSET, accepting any RFC 3339
    offset and converting it. The
    signature check downstream is what makes a non-`Z` offset fail, and it fails as
    E_SIG_BAD rather than as a parse error.
    """
    if not isinstance(text, str):
        raise ValueError("issued_at must be a string")
    parsed = match_rfc3339(text)
    if parsed is None:
        raise ValueError(f"issued_at is not RFC 3339: {text!r}")
    return parsed


def now_utc() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)
