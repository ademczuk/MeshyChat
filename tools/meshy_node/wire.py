"""The bytes a node puts on the wire. Pure functions: no sockets, no files.

Written from the wire contract, NOT imported from `apps/` (see the package docstring).
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import secrets
import struct
import time
import uuid
from datetime import datetime, timezone

try:
    import rfc8785 as _rfc8785
except ImportError:
    _rfc8785 = None

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (Ed25519PrivateKey,
                                                              Ed25519PublicKey)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

# --------------------------------------------------------------------------- encodings

_JCS_INT_MIN = -(2**53) + 1
_JCS_INT_MAX = 2**53 - 1
_JCS_ESCAPE_RE = re.compile(r'[\x00-\x1f"\\]')
_JCS_SHORT_ESCAPES = {
    '"': r'\"',
    "\\": r"\\",
    "\b": r"\b",
    "\f": r"\f",
    "\n": r"\n",
    "\r": r"\r",
    "\t": r"\t",
}


def b64u(raw: bytes) -> str:
    """Unpadded base64url: the form every key, signature and proof takes on this wire."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64u_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _jcs_escape_match(match: re.Match[str]) -> str:
    ch = match.group(0)
    short = _JCS_SHORT_ESCAPES.get(ch)
    return short if short is not None else f"\\u{ord(ch):04x}"


def _jcs_str_bytes(value: str) -> bytes:
    raw = str.__str__(value)
    escaped = _JCS_ESCAPE_RE.sub(_jcs_escape_match, raw)
    try:
        return b'"' + escaped.encode("utf-8") + b'"'
    except UnicodeEncodeError as exc:
        raise ValueError("lone surrogates are not valid UTF-8 in RFC 8785 strings") from exc


def _jcs_float_bytes(value: float) -> bytes:
    f = float(value)
    if math.isnan(f) or math.isinf(f):
        raise ValueError(f"non-finite float {f!r} is not valid in RFC 8785")
    if f == 0.0:
        return b"0"
    sign = ""
    if f < 0.0:
        sign = "-"
        f = -f
    es = str(f)
    exponent_str = ""
    exponent_value = 0
    e_idx = es.find("e")
    if e_idx != -1:
        exponent_value = int(es[e_idx + 1:])
        exponent_str = f"e{'+' if exponent_value >= 0 else '-'}{abs(exponent_value)}"
        es = es[:e_idx]
    dot_idx = es.find(".")
    if dot_idx != -1:
        first = es[:dot_idx]
        dot = "."
        last = es[dot_idx + 1:]
        if last == "0":
            dot = ""
            last = ""
    else:
        first = es
        dot = ""
        last = ""
    if 0 < exponent_value < 21:
        digits = first + last
        pos = exponent_value + 1
        if len(digits) <= pos:
            first = digits + ("0" * (pos - len(digits)))
            dot = ""
            last = ""
        else:
            first = digits[:pos]
            dot = "."
            last = digits[pos:]
        exponent_str = ""
    elif -7 < exponent_value < 0:
        last = ("0" * (-1 - exponent_value)) + first + last
        first = "0"
        dot = "."
        exponent_str = ""
    return (sign + first + dot + last + exponent_str).encode("ascii")


def _jcs_write(obj, parts: list[bytes]) -> None:
    if obj is None:
        parts.append(b"null")
    elif isinstance(obj, bool):
        parts.append(b"true" if obj else b"false")
    elif isinstance(obj, int):
        v = int(obj)
        if v < _JCS_INT_MIN or v > _JCS_INT_MAX:
            raise ValueError(f"integer {v} is outside the IEEE 754 safe range")
        parts.append(str(v).encode("ascii"))
    elif isinstance(obj, float):
        parts.append(_jcs_float_bytes(obj))
    elif isinstance(obj, str):
        parts.append(_jcs_str_bytes(obj))
    elif isinstance(obj, (list, tuple)):
        parts.append(b"[")
        for idx, item in enumerate(obj):
            if idx:
                parts.append(b",")
            _jcs_write(item, parts)
        parts.append(b"]")
    elif isinstance(obj, dict):
        items = []
        for k, v in obj.items():
            if not isinstance(k, str):
                raise ValueError(f"object keys must be strings, got {type(k).__name__}")
            raw_k = str.__str__(k)
            try:
                sort_key = raw_k.encode("utf-16-be")
            except UnicodeEncodeError as exc:
                raise ValueError("lone surrogates are not valid in RFC 8785 keys") from exc
            items.append((sort_key, raw_k, v))
        items.sort(key=lambda entry: entry[0])
        for i in range(1, len(items)):
            if items[i][0] == items[i - 1][0]:
                raise ValueError("duplicate object key is not valid in RFC 8785")
        parts.append(b"{")
        for idx, (_, raw_k, v) in enumerate(items):
            if idx:
                parts.append(b",")
            parts.append(_jcs_str_bytes(raw_k))
            parts.append(b":")
            _jcs_write(v, parts)
        parts.append(b"}")
    else:
        raise ValueError(f"unsupported type for RFC 8785 canonicalization: {type(obj).__name__}")


def _stdlib_jcs(obj) -> bytes:
    """Pure-stdlib RFC 8785 (JCS) serializer, byte-identical to `rfc8785.dumps`."""
    parts: list[bytes] = []
    _jcs_write(obj, parts)
    return b"".join(parts)


def jcs(obj) -> bytes:
    """RFC 8785 bytes used to verify envelopes over the payload as sent."""
    if _rfc8785 is not None:
        return _rfc8785.dumps(obj)
    return _stdlib_jcs(obj)


def utc_now_rfc3339() -> str:
    """`issued_at`, in the UTC RFC 3339 form the server parses; the trailing `Z` is required."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------- Ed25519


def new_seed() -> bytes:
    """32 random bytes: the node's private key. Generated on the node, never sent."""
    return os.urandom(32)


def public_key_of(seed: bytes) -> bytes:
    return Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw)


def sign(seed: bytes, message: bytes) -> bytes:
    return Ed25519PrivateKey.from_private_bytes(seed).sign(message)


def verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, message)
    except (InvalidSignature, ValueError):
        return False
    return True


# --------------------------------------------------------------------------- DPoP
# Two encodings of one proof use different byte orders. The asymmetry is load-bearing:
# the signed message uses u32/i64 LITTLE-endian and the transport form u16/i64 BIG-endian.

DPOP_DOMAIN = b"meshy/dpop/v1"                     # Compatibility domain value.
WS_UPGRADE = "WS_UPGRADE"                          # DPoP method for socket upgrades.


def _dpop_signed(method: str, target: str, ts: int, token_hash: bytes, jti: bytes) -> bytes:
    m, t = method.encode("utf-8"), target.encode("utf-8")
    return (DPOP_DOMAIN + struct.pack("<I", len(m)) + m + struct.pack("<I", len(t)) + t
            + struct.pack("<q", ts) + token_hash + jti)


def dpop_proof(seed: bytes, method: str, target: str, jwt: str, *,
               timestamp: int | None = None, jti: bytes | None = None) -> str:
    """One proof, bound to (method, target, this exact token). `target` is the request PATH
    as sent, no query; for the socket it is the upgrade path with method WS_UPGRADE."""
    ts = int(time.time()) if timestamp is None else int(timestamp)
    j = secrets.token_bytes(16) if jti is None else jti
    if len(j) != 16:
        raise ValueError("jti must be 16 bytes")
    token_hash = hashlib.sha256(jwt.encode("utf-8")).digest()
    signature = sign(seed, _dpop_signed(method, target, ts, token_hash, j))
    m, t = method.encode("utf-8"), target.encode("utf-8")
    return b64u(struct.pack(">H", len(m)) + m + struct.pack(">H", len(t)) + t
                + struct.pack(">q", ts) + token_hash + j + signature)


# --------------------------------------------------------------------------- sign-in

SIGNIN_PURPOSE = "meshy-signin-v1"                  # Compatibility purpose value.


def signin_message(peer_id: str, nonce: str) -> bytes:
    """The bytes a peer signs to sign in: JCS of {aud, nonce, peer_id, purpose}.
    `aud` is "peer": this is a node, not an owner."""
    return jcs({"aud": "peer", "nonce": nonce, "peer_id": str(uuid.UUID(peer_id)),
                "purpose": SIGNIN_PURPOSE})


OWNER_SIGNIN_PURPOSE = "meshy-owner-signin-v1"      # Compatibility purpose value.


def owner_signin_message(peer_id: str, nonce: str) -> bytes:
    """The bytes an OWNER's signer peer signs to mint a dashboard session: JCS of the same
    four fields with `aud` "dashboard" and its own purpose, so a
    node sign-in signature can never be replayed as an owner session and vice versa."""
    return jcs({"aud": "dashboard", "nonce": nonce, "peer_id": str(uuid.UUID(peer_id)),
                "purpose": OWNER_SIGNIN_PURPOSE})


def peer_kid(public_key: bytes) -> str:
    """The peer-key id rule: the first 16 characters of unpadded base64url(SHA-256(raw
    public key)). It is the kid `manage.py bootstrap_owner` stores for the operator's signer
    (and prints as `peer_kid`), so `bind` can derive it rather than ask for it. A node minted
    from a pairing code has a DIFFERENT kid (the onboard route's `desktop-...`), which is why
    `mint` stores the kid the server answered instead of deriving one."""
    return b64u(hashlib.sha256(public_key).digest())[:16]


# --------------------------------------------------------------------------- mesh message v1
# apps/memoryskills/validation.py: MESH_MESSAGE_V1_REQUIRED_FIELDS and
# MESH_MESSAGE_V1_OPTIONAL_FIELDS. Restated here, not imported: the client must not depend
# on the server package, and tests/test_meshy_node.py pins the two sets equal so a v1 field
# added on one side is caught on the other.

MESH_NAMESPACE = "shared/mesh-chat"
V1_REQUIRED = frozenset({"schema_version", "to", "from", "type", "thread_id", "ts", "body",
                         "author_family"})
V1_OPTIONAL = frozenset({"cc", "requires_ack", "in_reply_to", "session_label", "subject",
                         "ref", "done_when", "closes", "declines", "attachments", "operator_words",
                         "directive", "urgent", "urgent_declined", "window_end"})

#: `attachments` (divergence 079): validation.py's MESH_MAX_ATTACHMENTS,
#: MESH_ATTACHMENT_FIELDS, MESH_ATTACHMENT_NAME_MAX_CHARS and MESH_ATTACHMENT_MAX_SIZE_BYTES,
#: restated for the same reason as the field sets and pinned equal by
#: tests/test_mesh_attachments_schema.py, which also runs every case through both checks.
MAX_ATTACHMENTS = 8
ATTACHMENT_FIELDS = frozenset({"file_id", "name", "size_bytes", "sha256"})
MAX_ATTACHMENT_NAME_CHARS = 255
MAX_ATTACHMENT_SIZE_BYTES = 2**53 - 1
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")

#: `subject` and `ref`: validation.py's MESH_SUBJECT_RE and MESH_REF_RE, copied, not imported,
#: for the same reason as the field sets, and pinned equal by tests/test_meshy_node_subject_ref.py.
#: A slug, never prose: the server refuses anything else with E_MESH_SCHEMA, but only after the
#: value was signed and sent (backlog item 15; divergence 007's paragraph of 2026-10-10).
MESH_SUBJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
MESH_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
_SLUG_SHAPE = ("{field} must be 1 to {most} characters: a letter or digit, then only letters, digits and _ . : / - "
               "(no spaces), e.g. {example}; free text belongs in the body")


class NotV1(ValueError):
    """A value this client refuses to send because it is not a mesh message v1."""


def subject_problem(value) -> str | None:
    """Why `value` is not a v1 `subject`, naming the shape that is, or None. The server's rule
    (validation.validate_mesh_message_v1): a str that MESH_SUBJECT_RE fullmatches, so no
    trailing newline either."""
    if isinstance(value, str) and MESH_SUBJECT_RE.fullmatch(value):
        return None
    return _SLUG_SHAPE.format(field="subject", most=256, example="pr/933 or backlog-15")


def ref_problem(value) -> str | None:
    """Why `value` is not a v1 `ref`, naming the shape that is, or None. As `subject_problem`,
    with MESH_REF_RE: at most 128 characters."""
    if isinstance(value, str) and MESH_REF_RE.fullmatch(value):
        return None
    return _SLUG_SHAPE.format(field="ref", most=128, example="a commit sha, pr/933 or v1.2.0")


def attachment_name_problem(name) -> str | None:
    """Why `name` is not a plain file name the schema accepts, or None. `files.upload` asks it
    of a local file's name before anything is sent, and `files.download` of a received one
    before it is used as a name on this disk: a row the server stored unchecked (an
    unversioned one, divergence 011) carries whatever its writer put there."""
    if (not isinstance(name, str) or not 1 <= len(name) <= MAX_ATTACHMENT_NAME_CHARS
            or name in (".", "..") or "/" in name or "\\" in name
            or any(ord(char) < 32 or ord(char) == 127 for char in name)):
        return (f"an attachment name is a plain file name of 1 to {MAX_ATTACHMENT_NAME_CHARS} "
                "characters: no '/' or '\\', no control characters, not '.' or '..'")
    return None


def attachment_problem(item) -> str | None:
    """Why one attachment reference is not the v1 shape, or None. The server's rules
    (validation._validate_mesh_attachments), in the same order."""
    if not isinstance(item, dict) or set(item) != ATTACHMENT_FIELDS:
        return ("an attachment is an object with exactly the keys "
                f"{', '.join(sorted(ATTACHMENT_FIELDS))}")
    file_id = item["file_id"]
    try:
        canonical = isinstance(file_id, str) and str(uuid.UUID(file_id)) == file_id
    except ValueError:
        canonical = False
    if not canonical:
        return "an attachment's file_id is a canonical lowercase UUID"
    named = attachment_name_problem(item["name"])
    if named:
        return named
    size = item["size_bytes"]
    if type(size) is not int or not 0 <= size <= MAX_ATTACHMENT_SIZE_BYTES:
        return "an attachment's size_bytes is a non-negative integer of at most 2^53-1"
    sha256 = item["sha256"]
    if not isinstance(sha256, str) or not _SHA256_HEX.fullmatch(sha256):
        return "an attachment's sha256 is exactly 64 lowercase hex characters"
    return None


#: the directive rule: the directive's shape, as apps/memoryskills/validation.validate_directive checks it.
DIRECTIVE_KEYS = frozenset({"v", "words", "target", "target_peer", "expires_at"})
DIRECTIVE_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


def directive_problem(directive, to) -> str | None:
    """Why `directive` is not a v1 directive for a message to `to`, or None. Shape only: the server also checks the
    roster, the expiry window and the rate."""
    if not isinstance(directive, dict) or set(directive) != DIRECTIVE_KEYS or directive.get("v") != 1:
        return "directive is exactly {v: 1, words, target, target_peer, expires_at}"
    words = directive["words"]
    if (not isinstance(words, list) or not 1 <= len(words) <= 20
            or not all(isinstance(w, str) and w.strip() and len(w) <= 4000 for w in words)):
        return "directive.words is a list of 1 to 20 non-empty strings of at most 4000 characters"
    target = directive["target"]
    if not isinstance(target, str) or not DIRECTIVE_LABEL_RE.fullmatch(target):
        return "directive.target is one seat label"
    if target not in list(to or []):
        return "directive.target must be on the to line"
    tp = directive["target_peer"]
    try:
        if not isinstance(tp, str) or str(uuid.UUID(tp)) != tp:
            return "directive.target_peer is the target's peer id (a canonical UUID)"
    except ValueError:
        return "directive.target_peer is the target's peer id (a canonical UUID)"
    exp = directive["expires_at"]
    if not isinstance(exp, str) or not exp.endswith("Z"):
        return "directive.expires_at is an ISO 8601 UTC instant ending in Z"
    try:
        datetime.fromisoformat(exp[:-1] + "+00:00")
    except ValueError:
        return "directive.expires_at is an ISO 8601 UTC instant ending in Z"
    return None


def window_end_problem(window_end) -> str | None:
    """Why `window_end` is not an ISO 8601 UTC instant ending in Z, or None (validation.validate_window_end, kxxx
    ab614c): the end of the window a directive's or relay's words are for. The server also bounds it."""
    if not isinstance(window_end, str) or not window_end.endswith("Z") or len(window_end) > 40:
        return "window_end is an ISO 8601 UTC instant ending in Z"
    try:
        datetime.fromisoformat(window_end[:-1] + "+00:00")
    except ValueError:
        return "window_end is an ISO 8601 UTC instant ending in Z"
    return None


def attachments_problem(items) -> str | None:
    """Why an `attachments` value is not the v1 shape, or None: a list of 1 to MAX_ATTACHMENTS
    references, each `attachment_problem`-clean, no file_id twice."""
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ATTACHMENTS:
        return f"attachments is an array of 1 to {MAX_ATTACHMENTS} file references"
    for index, item in enumerate(items):
        problem = attachment_problem(item)
        if problem:
            return f"attachments[{index}]: {problem}"
    file_ids = [item["file_id"] for item in items]
    if len(set(file_ids)) != len(file_ids):
        return "attachments must not name one file_id twice"
    return None


def assert_v1(value: dict) -> dict:
    """Refuse, before anything is signed, a value carrying a field outside v1 or missing a
    required one. The server refuses the same (E_MESH_SCHEMA), but a client that relies on
    that has already signed and sent the field."""
    fields = set(value)
    extra = sorted(fields - V1_REQUIRED - V1_OPTIONAL)
    if extra:
        raise NotV1(f"field(s) outside mesh message v1: {', '.join(extra)}")
    missing = sorted(V1_REQUIRED - fields)
    if missing:
        raise NotV1(f"missing v1 field(s): {', '.join(missing)}")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise NotV1("schema_version must be the integer 1")
    return value


def utc_ts() -> str:
    """`ts`: YYYY-MM-DDTHH:MM:SSZ exactly (validation.py MESH_TIMESTAMP_RE). The fleet
    sender's microsecond form is refused by the v1 validator."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def mesh_message(*, to: list[str], sender: str, msg_type: str, thread_id: str, body: str,
                 author_family: str, ts: str | None = None, cc: list[str] | None = None,
                 in_reply_to: str | None = None, requires_ack: bool = False,
                 subject: str | None = None, ref: str | None = None,
                 session_label: str | None = None, done_when: str | None = None,
                 closes: list[str] | None = None, declines: list[str] | None = None,
                 attachments: list[dict] | None = None, operator_words: list[str] | None = None,
                 directive: dict | None = None, window_end: str | None = None, urgent: bool = False,
                 urgent_declined: str | None = None) -> dict:
    """A mesh message v1 value. Optional fields are OMITTED when unset, never sent as null:
    the validator reads a present key as a claim to check.

    `attachments` is added whenever it is given, an empty list included, and its shape is
    checked HERE (`attachments_problem`, NotV1), before anything is signed: the server refuses
    the same value with E_MESH_SCHEMA, but only after it was signed and sent. So are a `subject`
    and a `ref` that will be sent (`subject_problem`, `ref_problem`); an empty one is omitted."""
    value: dict = {"schema_version": 1, "to": list(to), "from": sender, "type": msg_type,
                   "thread_id": thread_id, "ts": ts or utc_ts(), "body": body,
                   "author_family": author_family}
    if cc:
        value["cc"] = list(cc)
    if in_reply_to:
        value["in_reply_to"] = in_reply_to
    if requires_ack:
        value["requires_ack"] = True
    if subject:
        problem = subject_problem(subject)
        if problem:
            raise NotV1(problem)
        value["subject"] = subject
    if ref:
        problem = ref_problem(ref)
        if problem:
            raise NotV1(problem)
        value["ref"] = ref
    if session_label:
        value["session_label"] = session_label
    if done_when is not None:
        value["done_when"] = done_when
    if closes:
        value["closes"] = list(closes)
    if declines:
        if closes and (set(closes) & set(declines)):
            raise NotV1("closes and declines must not contain the same msg/ key")
        value["declines"] = list(declines)
    if attachments is not None:
        problem = attachments_problem(attachments)
        if problem:
            raise NotV1(problem)
        value["attachments"] = [dict(item) for item in attachments]
    if operator_words is not None:
        # The operator's own words relayed by a voice (threat_model.md, amendment of 28 Oct 2026). The server refuses
        # the field from any other writer; this only checks the shape before anything is signed.
        if (not isinstance(operator_words, list) or not 1 <= len(operator_words) <= 20
                or not all(isinstance(w, str) and w.strip() and len(w) <= 4000 for w in operator_words)):
            raise NotV1("operator_words is a list of 1 to 20 non-empty strings of at most 4000 characters")
        value["operator_words"] = list(operator_words)
    if directive is not None:
        # the directive rule (threat_model.md, amendment of 2 Nov 2026): one roster seat's instruction to ONE seat on the to
        # line. The server checks the roster, the target and the expiry; this checks the shape before signing.
        problem = directive_problem(directive, to)
        if problem:
            raise NotV1(problem)
        value["directive"] = dict(directive)
    if window_end is not None:
        # The window the words are for (kxxx): only beside a directive or relayed words, as the server requires.
        problem = window_end_problem(window_end)
        if problem:
            raise NotV1(problem)
        if directive is None and operator_words is None:
            raise NotV1("window_end goes with a directive or operator_words")
        value["window_end"] = window_end
    if urgent or urgent_declined is not None:
        # The urgent lane (kxxx): delivery order only, to ONE seat, never beside a directive. The server checks
        # the same and counts the hourly allowance; this refuses before signing.
        if urgent and urgent_declined is not None:
            raise NotV1("a message is urgent or urgent_declined, never both")
        if urgent_declined is not None and urgent_declined != "rate_limited":
            raise NotV1('urgent_declined is "rate_limited" when present')
        if len(list(to or [])) != 1:
            raise NotV1("an urgent message goes to exactly one seat on its to line")
        if directive is not None:
            raise NotV1("a directive has its own lane; it is not also urgent")
        if urgent:
            value["urgent"] = True
        else:
            value["urgent_declined"] = urgent_declined
    return assert_v1(value)


def mesh_key(sender: str, *, now_ms: int | None = None) -> str:
    """`msg/<epoch-ms>-<sender>-<6 hex>`. The epoch-ms segment is what the write seam checks
    (validation.validate_mesh_key_ts); the random tail keeps two sends in one millisecond
    from being two versions of one key."""
    ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    return f"msg/{ms}-{sender}-{secrets.token_hex(3)}"


# --------------------------------------------------------------------------- invoke

MEMORY_SKILL_VERSION = "0.2.0"   # apps/calls/bootstrap.py SKILL_VERSION.

#: An object capability (the project's design log (not part of this release) 040, slice 6; design B07 3.1). The header that
#: carries it, the most bytes it may hold (the 8 KiB cap leaves room for the JWT and
#: the DPoP proof inside Twisted's 16 KiB header budget), and the name a server lists in
#: /v1/health's `capabilities` when it decides one. This client refuses when it is absent:
#: this server declines the header while tokens are off, and a server that ignores it may
#: authorize by the sender's database grants.
CAPABILITY_HEADER = "Meshychat-Capability"
MAX_CAPABILITY_BYTES = 8192
OBJECT_CAPABILITY_V1 = "object-capability-v1"


def capability_problem(token) -> str | None:
    """Why `token` cannot be sent as a `Meshychat-Capability` header, or None. It must be a
    non-empty string of printable ASCII (a biscuit is url-safe base64), so the hash signed
    over it is over the exact bytes sent, of at most MAX_CAPABILITY_BYTES. The reason never
    quotes the token."""
    if not isinstance(token, str) or not token:
        return "a capability token is a non-empty string"
    if not all(" " < ch < "\x7f" for ch in token):
        return "a capability token is printable ASCII text with no space (url-safe base64)"
    if len(token) > MAX_CAPABILITY_BYTES:
        return (f"the capability token is {len(token)} bytes; a {CAPABILITY_HEADER} header is "
                f"at most {MAX_CAPABILITY_BYTES}")
    return None


def capability_sha256(token: str) -> str:
    """The hex sha256 of the exact header string, as the server compares it (lower case)."""
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def call_envelope(*, seed: bytes, kid: str, caller_peer_id: str, target_peer_id: str,
                  skill: str, args: dict, call_id: str | None = None,
                  skill_version: str = MEMORY_SKILL_VERSION,
                  capability: str | None = None) -> dict:
    """A signed `skill.call` envelope: {payload, signature, signing_kid}. The payload uses the
    call fields plus the TASK-33 nonce that `mem_client.build_envelope` sends. With a `capability` (the
    token the call presents in Meshychat-Capability), `capability_sha256`, its hash, is signed
    in too, and the token itself never is (docs/DIVERGENCES.md 040, slice 6)."""
    payload = {
        "v": 1, "kind": "skill.call", "call_id": call_id or str(uuid.uuid4()),
        "caller_peer_id": str(uuid.UUID(caller_peer_id)),
        "target_peer_id": str(uuid.UUID(target_peer_id)),
        "skill_name": skill, "skill_version": skill_version, "args": args,
        "args_attachments": [], "issued_at": utc_now_rfc3339(), "nonce": str(uuid.uuid4()),
    }
    if capability is not None:
        payload["capability_sha256"] = capability_sha256(capability)
    return {"payload": payload, "signature": b64u(sign(seed, jcs(payload))),
            "signing_kid": kid}


#: The row fields a signed skill.result must agree with. The error pair is signed when
#: present (apps/calls/route._result_payload) and ABSENT from the payload when null, which
#: `.get` reads as None on both sides: a refusal's text is bound like its result.
RESULT_BOUND_FIELDS = ("status", "call_id", "result", "error_code", "error_message")
KIND_SKILL_RESULT = "skill.result"


def result_signature_ok(call: dict, jwks: dict) -> tuple[bool, str]:
    """Check a finished call's `result_signature` over JCS(`result_payload`) with the key its
    `result_signing_kid` names in the SERVER's JWKS (docs/DIVERGENCES.md 017: memory results
    are signed by the server key). Also checks the signed payload is a skill.result and
    agrees with the row it arrived beside - status, call_id, result and the error pair -
    since a valid signature over a DIFFERENT status is the #922 shape
    (docs/V1-DESIGN-RULES.md E2), and a refusal's printed text is part of its answer.

    NEVER RAISES on what the server sent. Every field here is the server's (or whoever
    answered in its place): a signature that is not base64url, a kid or key of the wrong
    type, a JWKS key with no `x`, a payload RFC 8785 cannot canonicalize. Any of those is
    "did not verify", never an exception that ends the command reading it."""
    try:
        return _result_signature_ok(call, jwks)
    except Exception as exc:  # noqa: BLE001 - a verifier fails closed on malformed input
        return False, f"result signature or key is malformed ({type(exc).__name__})"


def _result_signature_ok(call: dict, jwks: dict) -> tuple[bool, str]:
    kid, sig, payload = (call.get("result_signing_kid"), call.get("result_signature"),
                         call.get("result_payload"))
    if not kid or not sig or not isinstance(payload, dict):
        return False, "result is unsigned or has no result_payload"
    if not isinstance(kid, str) or not isinstance(sig, str):
        return False, "result_signing_kid and result_signature must be strings"
    keys = {k.get("kid"): k for k in jwks.get("keys", []) if isinstance(k, dict)}
    jwk = keys.get(kid)
    if jwk is None or jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519":
        return False, f"kid {kid!r} is not an Ed25519 key in the server JWKS"
    if not isinstance(jwk.get("x"), str):
        return False, f"kid {kid!r} in the server JWKS has no public key"
    if not verify(b64u_decode(jwk["x"]), jcs(payload), b64u_decode(sig)):
        return False, "result signature does not verify"
    if payload.get("kind") != KIND_SKILL_RESULT:
        return False, f"signed kind is {payload.get('kind')!r}, not {KIND_SKILL_RESULT!r}"
    for field in RESULT_BOUND_FIELDS:
        if payload.get(field) != call.get(field):
            return False, f"signed {field} differs from the call's {field}"
    return True, "ok"


# --------------------------------------------------------------------------- the author's signature
# `result_signature_ok` proves the SERVER said a memory.read answer. This proves, or says it
# could not prove, that the message in it is what its AUTHOR signed: memory.read with
# `include_provenance` returns the envelope that wrote the row (apps/memoryskills/handlers.py
# handle_write, step 7: the verbatim payload, its signature and signing_kid), and the key
# comes from the author's own published keys, not from the row.

#: Why a message's `signature_verified` is false: its `signature_error` is "<CODE>: <why>".
SIG_NO_ENVELOPE = "E_SIGNATURE_NO_ENVELOPE"          # the row carries no signed envelope
SIG_NOT_THE_MESSAGE = "E_SIGNATURE_NOT_THE_MESSAGE"  # the envelope did not write THIS
                                                     # message as THIS row's writer
SIG_KEY_UNAVAILABLE = "E_SIGNATURE_KEY_UNAVAILABLE"  # the author's published keys hold no
                                                     # key under the kid, or were unreadable
SIG_INVALID = "E_SIGNATURE_INVALID"                  # it does not verify under that key
SIG_NOT_CARRIED = "E_SIGNATURE_NOT_CARRIED"          # the route carries no envelope, so
                                                     # nothing was checked (search, context)
KIND_SKILL_CALL = "skill.call"
WRITE_SKILL = "memory.write"
#: The skill whose signed call is the row of a message an EDIT wrote (divergence 059): the editor signs the whole new value
#: as a `memory.edit` request, and the server stores it as the new version's envelope. The row says so itself, by the
#: `edited_at` of its edit record (`memory.read` reports it), and only then is this the skill the envelope must be.
EDIT_SKILL = "memory.edit"


def author_signature_ok(memo: dict, fetch_jwks) -> tuple[bool, str | None]:
    """(True, None) only when, in this call: the memo's provenance is a `skill.call` of
    memory.write (of memory.edit for a row whose memo carries `edited_at`, divergence 059:
    the editor signed the whole new value) signed as the memo's writer (`created_by_peer_id`), for this namespace and
    key, whose value is the stored value (compared as RFC 8785 bytes, so `true` is not `1`);
    and its signature over JCS(payload) verifies under the Ed25519 key that
    `fetch_jwks(writer)` returns under the envelope's kid. `fetch_jwks(peer_id)` answers
    (jwks, None) or (None, why): the author's PUBLISHED keys (GET
    /agents/{peer}/jwks.json, the active key only), never the key the row names, which is
    only as good as the row. So a message signed under a key since rotated out or revoked
    reads false (E_SIGNATURE_KEY_UNAVAILABLE): unverified, not forged.

    Covers the key and the value (`from`, `to`, `cc`, `thread_id`, `type`,
    `in_reply_to`, `body`, ...). Not the version or recorded_at, which the server assigns,
    nor author_label (the writer's signed label, else the server's stamp: `status_check`
    compares it where the writer signed one). True binds the value to the WRITER'S PEER ID,
    not to any label: a `from` or author_label naming another peer is only as good as the
    server's 028 check, which covers msg/ keys written since 028 (review F1 of
    fin/r1-small), so the caller prints the writer as `signed_by`. Verification is `verify`
    (the `cryptography` library), not the server's verify_strict. NEVER RAISES on what the
    server sent: anything malformed is (False, why)."""
    try:
        return _author_signature_ok(memo, fetch_jwks)
    except Exception as exc:  # noqa: BLE001 - a verifier fails closed on malformed input
        return False, (f"{SIG_INVALID}: the envelope or key could not be checked "
                       f"({type(exc).__name__})")


def _author_signature_ok(memo: dict, fetch_jwks) -> tuple[bool, str | None]:
    prov = memo.get("provenance") if isinstance(memo, dict) else None
    if not isinstance(prov, dict):
        return False, f"{SIG_NO_ENVELOPE}: the row carries no signed envelope"
    payload, sig, kid = prov.get("payload"), prov.get("signature"), prov.get("signing_kid")
    if not (isinstance(payload, dict) and isinstance(sig, str) and sig
            and isinstance(kid, str) and kid):
        return False, (f"{SIG_NO_ENVELOPE}: the row's envelope has no payload, signature "
                       "or signing_kid")
    writer, args = memo.get("created_by_peer_id"), payload.get("args")
    why = None
    # A row the server says an edit wrote (its `edited_at`) is a memory.edit call; any other row is a memory.write call.
    # The two are paired, so a row that claims to be an edit but carries a write's envelope, or the reverse, is not verified.
    skill = EDIT_SKILL if memo.get("edited_at") is not None else WRITE_SKILL
    if payload.get("kind") != KIND_SKILL_CALL or payload.get("skill_name") != skill:
        why = (f"the envelope is a {payload.get('kind')!r} of {payload.get('skill_name')!r}, "
               f"not a {skill} call")
    elif not isinstance(writer, str) or payload.get("caller_peer_id") != writer:
        why = (f"the envelope was signed as caller {payload.get('caller_peer_id')!r}; the "
               f"row's writer is {writer!r}")
    elif (not isinstance(args, dict) or args.get("namespace") != memo.get("namespace")
          or args.get("key") != memo.get("key")):
        where = (f"{args.get('namespace')}/{args.get('key')}" if isinstance(args, dict)
                 else "nothing")
        why = f"the envelope wrote {where}, not {memo.get('namespace')}/{memo.get('key')}"
    else:
        # handle_write stores args.get("value", args.get("body")).
        signed = args["value"] if "value" in args else args.get("body")
        if jcs(signed) != jcs(memo.get("value")):
            why = "the stored value is not the value the envelope wrote"
    if why:
        return False, f"{SIG_NOT_THE_MESSAGE}: {why}"
    try:
        jwks, unavailable = fetch_jwks(writer)
    except Exception as exc:  # noqa: BLE001 - no key was found, so nothing was checked
        return False, (f"{SIG_KEY_UNAVAILABLE}: the keys peer {writer} publishes could not "
                       f"be fetched ({type(exc).__name__})")
    if jwks is None:
        return False, f"{SIG_KEY_UNAVAILABLE}: {unavailable}"
    keys = jwks.get("keys") if isinstance(jwks, dict) else None
    found = [k for k in (keys if isinstance(keys, list) else [])
             if isinstance(k, dict) and k.get("kid") == kid]
    jwk = found[0] if len(found) == 1 else None
    if (jwk is None or jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519"
            or not isinstance(jwk.get("x"), str)):
        return False, (f"{SIG_KEY_UNAVAILABLE}: the keys peer {writer} publishes hold no "
                       f"Ed25519 key {kid!r} (a key rotated out or revoked since the write "
                       "cannot be checked here)")
    if not verify(b64u_decode(jwk["x"]), jcs(payload), b64u_decode(sig)):
        return False, (f"{SIG_INVALID}: the envelope's signature does not verify under key "
                       f"{kid!r} of peer {writer}")
    return True, None


# --------------------------------------------------------------------------- the row's status
# B02 phase 0 (docs/design/signed-bytes-status.md, 4.1 and Q13). `author_signature_ok`
# covers the namespace, key and value. A row also carries pre_discipline, status,
# claimed_status, tags and author_label, and each is either an arg the writer signed or a
# pure function of the signed args (design 2.3): memory.write's own rules, restated here
# from apps/memoryskills/handlers.py handle_write and validation.norm_tags, not imported.
# So once the payload has verified, the client derives them and compares, for any row
# whose payload it verified, historical rows included, and no server can downgrade it.

#: Why a message's `status_verified` is false: its `status_error` is "<CODE>: <why>", or
#: the signature's own code when the author's signature did not verify (nothing compared).
STATUS_CONTRADICTS = "E_STATUS_CONTRADICTS_SIGNATURE"  # a field whose arg the writer
                                                       # SIGNED is not what the row says
STATUS_DEFAULT_DIFFERS = "E_STATUS_DEFAULT_DIFFERS"    # the writer signed no such arg (or
                                                       # one the rules discard) and the row
                                                       # is not the rule's default
STATUS_UNDERIVABLE = "E_STATUS_UNDERIVABLE"            # a signed arg the rules refuse, or
                                                       # input that could not be compared
STATUS_NOT_SERVED = "E_STATUS_NOT_SERVED"              # the memo does not carry the field
#: Most serious first: the code `status_error` leads with, and the order its reasons take.
STATUS_CODES = (STATUS_CONTRADICTS, STATUS_DEFAULT_DIFFERS, STATUS_UNDERIVABLE,
                STATUS_NOT_SERVED)
#: The row's fields the signed args determine, in the order they are checked.
STATUS_FIELDS = ("pre_discipline", "status", "claimed_status", "tags", "author_label")
WIKI_NAMESPACE = "shared/wiki"
#: How a derived field came to be: the writer signed its arg; the writer signed none and
#: the rule gives a default; the writer signed one that the rule discards, so it gives the
#: default all the same (claimed_status off a custody write, the one arg discarded); or
#: (author_label) the server stamped it from state the client does not have (the roster,
#: else "peer-<id8>"), so nothing derives it.
SIGNED, DEFAULTED, DISCARDED, STAMPED = "signed", "defaulted", "discarded", "stamped"


def _shown(value) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= 120 else text[:117] + "..."


def derive_status_fields(args: dict) -> tuple[dict, dict]:
    """(derived, refused) from a verified memory.write payload's `args`, by design 2.3:
    derived[field] = (value, SIGNED | DEFAULTED | DISCARDED | STAMPED); refused[field] =
    why the rules refuse the signed arg (Q3's strict types), where memory.write today
    coerces instead (G3). The client does not reproduce a coercion: a refused field is
    underivable.

      pre_discipline  absent or null: false; a JSON boolean: itself; else refused
      status          a JSON string: itself; absent or null: "OPEN" on a non-custody
                      shared/wiki write, else null; else refused; refused on a custody
                      write (handle_write refuses a status there)
      claimed_status  on a custody write: a JSON string, or null when absent; else
                      refused. Off custody: null, whatever was signed (DISCARDED when
                      something was)
      tags            a list of strings: itself, in order; a comma string: split on ","
                      and each part stripped, empty parts dropped (norm_tags); absent or
                      null: []; else refused
      author_label    `author`, else `author_label` (the first that is truthy, as
                      handle_write reads them): a non-empty string is itself; none:
                      stamped by the server; anything else refused"""
    if not isinstance(args, dict):
        raise TypeError("the signed args are not an object")
    derived: dict = {}
    refused: dict = {}
    pre = args.get("pre_discipline")
    if pre is None:
        derived["pre_discipline"] = (False, DEFAULTED)
    elif isinstance(pre, bool):
        derived["pre_discipline"] = (pre, SIGNED)
    else:
        refused["pre_discipline"] = f"the signed pre_discipline {_shown(pre)} is not a boolean"

    if "pre_discipline" not in derived:
        for field in ("status", "claimed_status"):
            refused[field] = "it depends on pre_discipline, which is underivable"
    else:
        custody = derived["pre_discipline"][0]
        status = args.get("status")
        if status is None:
            default = ("OPEN" if args.get("namespace") == WIKI_NAMESPACE and not custody
                       else None)
            derived["status"] = (default, DEFAULTED)
        elif not isinstance(status, str):
            refused["status"] = f"the signed status {_shown(status)} is not a string"
        elif custody:
            refused["status"] = ("a custody write (pre_discipline) signed a status, which "
                                 "memory.write refuses")
        else:
            derived["status"] = (status, SIGNED)
        claimed = args.get("claimed_status")
        if claimed is None:
            derived["claimed_status"] = (None, DEFAULTED)
        elif not custody:
            derived["claimed_status"] = (None, DISCARDED)
        elif isinstance(claimed, str):
            derived["claimed_status"] = (claimed, SIGNED)
        else:
            refused["claimed_status"] = (f"the signed claimed_status {_shown(claimed)} is "
                                         "not a string")

    tags = args.get("tags")
    if tags is None:
        derived["tags"] = ([], DEFAULTED)
    elif isinstance(tags, str):
        derived["tags"] = ([t.strip() for t in tags.split(",") if t.strip()], SIGNED)
    elif isinstance(tags, list) and all(isinstance(t, str) for t in tags):
        derived["tags"] = (list(tags), SIGNED)
    else:
        refused["tags"] = (f"the signed tags {_shown(tags)} are not a list of strings or a "
                           "comma string")

    author = args.get("author") or args.get("author_label")
    if not author:
        derived["author_label"] = (None, STAMPED)
    elif isinstance(author, str):
        derived["author_label"] = (author, SIGNED)
    else:
        refused["author_label"] = f"the signed author {_shown(author)} is not a string"
    return derived, refused


def status_check(memo: dict, signature_verified: bool, signature_error: str | None) -> dict:
    """{"status_verified": bool[, "status_error": "<CODE>: <why>"], "author_signed": bool}
    for a memo `author_signature_ok` has just judged: `signature_verified` and
    `signature_error` are ITS verdict for this memo.

    `status_verified` is true only when that signature verified AND each of STATUS_FIELDS
    the memo serves equals the value `derive_status_fields` gives from the verified
    payload's args, compared as RFC 8785 bytes (so 1 is not true). author_label is compared
    only when the writer signed one; a stamped label is not derivable and is not a failure,
    which `author_signed: false` says. False names every field that failed, the most
    serious class leading (STATUS_CODES); a field the writer did not sign, or signed and
    the rule discards, is reported as a default that differs, never as a contradiction of
    the signature (design 3.3 step 6), and the reason says which of the two it was.
    When the signature did not verify, nothing is compared: the error is that signature's
    own code (a rotated key reads E_SIGNATURE_KEY_UNAVAILABLE, not a contradiction).

    `author_signed` is true only when the signature verified, the writer signed an author
    label, and it is the one the memo serves as `author_label`.

    NOT BOUND TO THE VERSION (review of 0b08f4e, finding 4): the signed args carry no
    version, and neither this nor `author_signature_ok` reads `version` or `is_head`, so a
    memo served with another version's envelope from the same writer over the same value
    reads true, with that envelope's fields. In shared/mesh-chat a sender may write later
    versions of its own msg/ key. The README says so beside `signature_verified`'s own.

    NEVER RAISES on what the server sent: anything malformed is false, E_STATUS_UNDERIVABLE."""
    try:
        return _status_check(memo, signature_verified, signature_error)
    except Exception as exc:  # noqa: BLE001 - a verifier fails closed on malformed input
        return {"status_verified": False,
                "status_error": (f"{STATUS_UNDERIVABLE}: the row's status fields could not "
                                 f"be compared ({type(exc).__name__})"),
                "author_signed": False}


def _status_check(memo: dict, signature_verified: bool, signature_error: str | None) -> dict:
    if signature_verified is not True:
        code = (signature_error or "").split(":", 1)[0].strip() or SIG_INVALID
        return {"status_verified": False,
                "status_error": (f"{code}: nothing was compared: the author's signature "
                                 "over the payload did not verify"),
                "author_signed": False}
    args = memo["provenance"]["payload"]["args"]
    derived, refused = derive_status_fields(args)
    found: dict = {code: [] for code in STATUS_CODES}
    for field in STATUS_FIELDS:
        if field not in memo:
            found[STATUS_NOT_SERVED].append(f"{field}: the memo does not carry it")
        elif field in refused:
            found[STATUS_UNDERIVABLE].append(f"{field}: {refused[field]}")
        else:
            value, basis = derived[field]
            served = memo[field]
            if basis == STAMPED or jcs(served) == jcs(value):
                continue
            if basis == SIGNED:
                found[STATUS_CONTRADICTS].append(
                    f"{field}: the writer signed {_shown(value)}, the row says "
                    f"{_shown(served)}")
            elif basis == DISCARDED:
                found[STATUS_DEFAULT_DIFFERS].append(
                    f"{field}: the writer signed {_shown(args[field])}, which "
                    "memory.write keeps on a custody write only, so its rule gives "
                    f"{_shown(value)}, the row says {_shown(served)}")
            else:
                found[STATUS_DEFAULT_DIFFERS].append(
                    f"{field}: the writer signed none, so memory.write's rule gives "
                    f"{_shown(value)}, the row says {_shown(served)}")
    author, basis = derived.get("author_label", (None, None))
    author_signed = (basis == SIGNED and "author_label" in memo
                     and jcs(memo["author_label"]) == jcs(author))
    codes = [code for code in STATUS_CODES if found[code]]
    if not codes:
        return {"status_verified": True, "author_signed": author_signed}
    reasons = [reason for code in codes for reason in found[code]]
    return {"status_verified": False, "status_error": f"{codes[0]}: " + "; ".join(reasons),
            "author_signed": author_signed}
