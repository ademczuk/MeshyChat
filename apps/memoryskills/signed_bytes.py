"""What memory.write accepts at its seam, so that a row's derived columns can be held to the
bytes its writer signed. Divergence 039; design docs/design/signed-bytes-status.md
(fin/design-signed-bytes), sections 2.3 and 5 (Q3, Q4).

The database holds the same rules in sql_meshychat/014 (`sm_derive_*`, and the agree-trigger
that refuses a row whose columns disagree with its `signed_bytes`). The two are meant to be
TWO implementations: a drift between them fails the write instead of storing either answer.
This module is the Python side of the argument types; handle_write keeps its own derivation
of the columns, which the trigger then checks.

STRICT TYPES (Q3). Before A1 the handler read `pre_discipline` by Python truthiness (the
STRING "false" made a custody row for a namespace admin), a list or object `status` raised
TypeError (E_INTERNAL over W1), and a list of non-strings in `tags` stored Python reprs
(`{1,True,None,"{'a': 1}"}`). None of those can be reproduced in SQL, so they are refused:

    pre_discipline   absent, null, or a JSON boolean          else E_INVALID_ARGS
    status           absent, null, or a JSON string           else E_INVALID_STATUS
    claimed_status   absent, null, or a JSON string           else E_INVALID_ARGS
    author           absent, null, or a JSON string           else E_INVALID_ARGS
    author_label     absent, null, or a JSON string           else E_INVALID_ARGS
    tags             a list must hold only strings            else E_INVALID_ARGS

A comma-string `tags` is still accepted and normalised as before (Q3 keeps it). The trigger
does not check it (design 2.3 leaves it to an offline verifier): the legacy classifier
compares it on a row without bytes, and on a row with bytes nothing does yet. A `tags` value
that is neither a list nor a string keeps its existing refusal, E_BAD_REQUEST from
validation.norm_tags.

NUMBERS (Q4). A number beyond 2**53 - 1 anywhere in the payload is refused with
E_NUMBER_OUT_OF_RANGE: see apps/crypto/ijson.py for why no such row could be read back.
"""
from __future__ import annotations

from apps.crypto.ijson import MAX_SAFE_INTEGER, unsafe_number_path

from .errors import HandlerError

#: The args whose JSON type is fixed at the seam, and the code each refusal carries.
STRING_ARGS = ("claimed_status", "author", "author_label")


def _json_type(value) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def check_arg_types(args: dict) -> None:
    """Refuse a memory.write arg whose JSON type design 2.3 does not derive from. Q3."""
    pre = args.get("pre_discipline")
    if pre is not None and not isinstance(pre, bool):
        raise HandlerError(
            "E_INVALID_ARGS",
            f"pre_discipline must be a JSON boolean, not a {_json_type(pre)} "
            "(the string \"false\" used to make a custody row; divergence 039)")
    status = args.get("status")
    if status is not None and not isinstance(status, str):
        raise HandlerError(
            "E_INVALID_STATUS", f"status must be a string, not a {_json_type(status)}")
    for name in STRING_ARGS:
        value = args.get(name)
        if value is not None and not isinstance(value, str):
            raise HandlerError(
                "E_INVALID_ARGS", f"{name} must be a string, not a {_json_type(value)}")
    tags = args.get("tags")
    if isinstance(tags, list):
        for i, tag in enumerate(tags):
            if not isinstance(tag, str):
                raise HandlerError(
                    "E_INVALID_ARGS",
                    f"tags[{i}] must be a string, not a {_json_type(tag)} (a list of "
                    "strings, or one comma-separated string)")


def refuse_unsafe_numbers(payload) -> None:
    """Q4: refuse a payload holding a number no stored copy of it could be re-canonicalised
    from. The whole payload, not only the value: all of it is stored in provenance_jsonb."""
    path = unsafe_number_path(payload)
    if path is not None:
        raise HandlerError(
            "E_NUMBER_OUT_OF_RANGE",
            f"{path} is a number beyond +/-{MAX_SAFE_INTEGER} (2^53-1): RFC 8785 cannot "
            "carry it once it is stored, so the row could never be read back (I-JSON, "
            "RFC 7493; divergence 039)")


# --------------------------------------------------------------------------- derivation
# design 2.3 in Python, the twin of sql_meshychat/014's sm_derive_* functions. The handler
# does not call this (it keeps its own derivation, which the trigger checks); the legacy
# classifier does, to split a historical row's disagreements into signed and defaulted, and
# tests/test_signed_bytes_classifier.py holds it to the SQL over one corpus.

class Derived:
    """One derived column. `value` is the column the bytes derive (None is NULL; for
    author_label None means the writer signed none and the server stamped it; for tags None
    means a comma string, which the trigger does not check). `signed` is whether
    the writer signed the arg that decides it. `refused` names the arg a refusal names,
    exactly as the SQL raises it, or is None."""

    __slots__ = ("value", "signed", "refused")

    def __init__(self, value, signed: bool, refused: str | None = None):
        self.value, self.signed, self.refused = value, signed, refused

    def __repr__(self) -> str:
        return f"Derived({self.value!r}, signed={self.signed}, refused={self.refused!r})"


def _pre(args) -> Derived:
    v = args.get("pre_discipline")
    if v is None:
        return Derived(False, False)
    if isinstance(v, bool):
        return Derived(v, True)
    return Derived(None, True, "pre_discipline")


def _status(args, pre: Derived) -> Derived:
    v = args.get("status")
    if v is None:
        st, signed = None, False
    elif isinstance(v, str):
        st, signed = v, True
    else:
        return Derived(None, True, "status")
    if pre.refused:
        return Derived(None, signed, pre.refused)
    if pre.value:
        if st is not None:
            return Derived(None, True, "status")
        return Derived(None, False)
    if st is None and args.get("namespace") == "shared/wiki":
        return Derived("OPEN", False)
    return Derived(st, signed)


def _claimed(args, pre: Derived) -> Derived:
    if pre.refused:
        return Derived(None, False, pre.refused)
    if not pre.value:
        return Derived(None, False)
    v = args.get("claimed_status")
    if v is None:
        return Derived(None, False)
    if isinstance(v, str):
        return Derived(v, True)
    return Derived(None, True, "claimed_status")


def _author(args) -> Derived:
    author, label = args.get("author"), args.get("author_label")
    for v in (author, label):
        if v is not None and not isinstance(v, str):
            return Derived(None, True, "author_label")
    for v in (author, label):
        if isinstance(v, str) and v:
            return Derived(v, True)
    return Derived(None, False)


def _tags(args) -> Derived:
    t = args.get("tags")
    if t is None:
        return Derived([], False)
    if isinstance(t, str):
        return Derived(None, False)
    if not isinstance(t, list) or any(not isinstance(x, str) for x in t):
        return Derived(None, True, "tags")
    return Derived(list(t), True)


def derive_fields(payload) -> dict[str, Derived]:
    """Every derived column of design 2.3 from one payload, refusals included."""
    args = payload.get("args") if isinstance(payload, dict) else None
    args = args if isinstance(args, dict) else {}
    pre = _pre(args)
    return {
        "pre_discipline": pre,
        "status": _status(args, pre),
        "claimed_status": _claimed(args, pre),
        "author_label": _author(args),
        "tags": _tags(args),
        "value": Derived(args["value"] if "value" in args else args.get("body"), True),
    }
