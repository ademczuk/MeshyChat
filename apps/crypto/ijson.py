"""The I-JSON number boundary (RFC 7493 section 2.2), and the loader the legacy classifier
parses stored rows with. Divergence 039.

WHY 2**53 - 1. It is the largest integer an IEEE 754 double holds exactly, and the largest
`rfc8785` (apps.crypto.primitives.jcs) will canonicalise as an integer: it raises
IntegerDomainError beyond it. A FLOAT at or beyond 2**53 is always integral, and Python
writes 1e16 as `1e+16`, which Postgres JSONB stores as the numeric 10000000000000000 and
psycopg reads back as the Python int 10**16. So a payload carrying 1e16 verifies (jcs of
the float is fine), is stored, and then every re-canonicalisation of what was stored fails:
the memory.read result cannot be signed, and a W1 retry dies re-hashing skill_calls.args
(design docs/design/signed-bytes-status.md, G1 and probe P1). Refusing at the write seam is
design Q4.

`loads_ijson` is the OTHER half of Q4, and is used ONLY by the legacy classifier
(apps/memoryskills/legacy_verdict.py): it reads an integer beyond 2**53 - 1 back as a
float, which is how a row this build or a JSON float wrote can be rebuilt. It is never used
on the write or read path.
"""
from __future__ import annotations

import json

#: The largest magnitude an I-JSON number may have and still round-trip as an integer.
MAX_SAFE_INTEGER = 2**53 - 1


def unsafe_number_path(obj, path: str = "$") -> str | None:
    """The JSONPath of the first number in `obj` whose magnitude exceeds 2**53 - 1, or None.

    Ints and floats alike; `bool` is excluded (it is an int in Python, never a JSON number).
    NaN and the infinities are not compared: nothing canonicalises them, and the envelope
    is refused before it gets here (rfc8785 raises FloatDomainError).
    """
    if isinstance(obj, bool):
        return None
    if isinstance(obj, (int, float)):
        try:
            return path if abs(obj) > MAX_SAFE_INTEGER else None
        except (TypeError, ValueError):
            return None
    if isinstance(obj, dict):
        for k, v in obj.items():
            found = unsafe_number_path(v, f"{path}.{k}")
            if found:
                return found
        return None
    if isinstance(obj, list):
        for i, v in enumerate(obj):
            found = unsafe_number_path(v, f"{path}[{i}]")
            if found:
                return found
    return None


def untextable_string_path(obj, path: str = "$") -> str | None:
    """The JSONPath of the first string in `obj`, member name or value, that holds U+0000 or a
    lone surrogate, or None.

    I-JSON (RFC 7493 section 2.1) forbids both. A JSON escape spells either one and json.loads
    keeps it, but neither can be carried further: `rfc8785` refuses a lone surrogate (it has
    no UTF-8 form), psycopg cannot encode one, and Postgres text and JSONB hold no U+0000. So
    a call carrying one reached the idempotency hash or the call row and answered HTTP 500
    (#252). The path names the place; callers never echo the string itself.
    """
    if isinstance(obj, str):
        return path if _untextable(obj) else None
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and _untextable(k):
                return f"{path} (a member name)"
            found = untextable_string_path(v, f"{path}.{echo_text(k)}")
            if found:
                return found
        return None
    if isinstance(obj, list):
        for i, v in enumerate(obj):
            found = untextable_string_path(v, f"{path}[{i}]")
            if found:
                return found
    return None


def echo_text(value) -> str:
    """A client value as it may appear in an error message or a log line: control, non-ASCII
    and unpaired-surrogate characters escaped (json.dumps writes a lone surrogate as its
    escape, so the result always encodes), at most 64 characters (pxx's and memory-bxxxxx's
    #275 reviews). Used for echoed member names and for the `unknown variant` messages, whose
    raw echo of a lone surrogate made the 422 response itself unencodable: a 500."""
    text = json.dumps(str(value))[1:-1]
    return text if len(text) <= 64 else text[:61] + "..."


def _untextable(value: str) -> bool:
    if "\x00" in value:
        return True
    try:
        value.encode("utf-8")          # strict UTF-8 fails on exactly the unpaired surrogates
    except UnicodeEncodeError:
        return True
    return False


def _parse_int_ijson(text: str):
    value = int(text)
    return float(value) if abs(value) > MAX_SAFE_INTEGER else value


def loads_ijson(text: str):
    """json.loads, except that an integer beyond 2**53 - 1 becomes a float."""
    return json.loads(text, parse_int=_parse_int_ijson)
