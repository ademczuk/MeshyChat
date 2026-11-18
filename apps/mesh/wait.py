"""GET /v1/mesh/wait: the long-poll a node parks on until a readable mesh row lands.

Divergence 018 (c). The fleet's node client calls this route
(`tools/memory/mesh_poll_api.py:797-850`, `tools/memory/mesh_poll.py:64-90`). Its constants,
visibility query and cursor wire format let a node park here unchanged.

THE CONTRACT:

  request   GET /v1/mesh/wait?since=<cursor>&timeout=<secs>, `Authorization: Bearer <jwt>`
            No DPoP: it is a read (divergence 015).
  cursor    base64url, NO padding, of the ASCII text `<created_at_micros>:<uuid>`
            Missing or MALFORMED is not an
            error: the wait starts at the newest row the caller can already see, "so a
            fresh idle CLI only receives messages created after it begins waiting".
  timeout   default 25 s, capped at 60 s, and further capped by the token's remaining life
            minus 5 s. An effective timeout of 0 answers 204 at once, before any row is read.
  200       `{"messages":[{"key","from","to","created_at"}],"cursor":"<next>"}`, at most
            50 rows, oldest first, the cursor naming the last row. `from`/`to` are
            `value->>'from'`/`value->>'to'`, verbatim text.
  204       nothing readable landed before the deadline.
  503       more than 512 concurrent waiters. Taken BEFORE authentication.

WHAT "READABLE" MEANS, and why this route does not answer 403. The authorisation here is
the row filter itself: a head `msg/` row, in a namespace the caller owns or holds an active
read/write/admin grant on, that the caller did not author. It is ADDRESSING-AGNOSTIC: the
server never parses `to`, the client filters. A caller with no readable namespace therefore
waits and gets 204. The four graph routes 403 instead (apps/mesh/views.py); the difference is
recorded in divergence 018. The visibility clause re-expressed
`permissions.check_permission` in SQL until divergence 040 slice 3. It now reads the predicate's
set form, `meshychat_namespace_permission(peer)`, which check_permission reads too (see
_PEER_VIS_CTE). tests/test_mesh_graphread.py pins the two to the same answer over every grant
state, and tests/test_namespace_permission_predicate.py plants the function and shows the
wait's visibility, the plain wait's rows and the addressed scan follow it.

THE NUDGE (STATUS F12; divergence 018, amended 2026-09-26). A parked wait registers with the
process's registry, re-reads once to close the race between its first read and registration,
and waits on its event (apps/mesh/wake.py). The process's ONE subscriber LISTENs on the
NOTIFY sql/036 already sends for every committed `msg/` insert into shared/mesh-chat and
wakes every waiter but the row's author; a woken waiter re-reads. A waiter whose whole set
the nudge covers (the addressed mode; a plain wait whose readable namespaces are
shared/mesh-chat alone) does not poll while the subscriber is listening; a plain waiter that
can read another namespace keeps the 2 s re-query for it, since no trigger announces a row
there; every waiter polls while the subscriber is not listening and is woken when it is back.
The deadline rules are unchanged.

REQUEST HANDLING:
  * An ASGI worker being stopped ends the request its own way.
  * The status codes use the shared verifier (apps/peers/auth.py); the body is
    this project's JSON error envelope.

ADDED, and additive only: `X-Meshy-Visible-Namespaces` on every 200 and 204, the number of
namespaces the caller can read. A 204 for "idle" and a 204 for "you can read nothing" are
one status; V1-DESIGN-RULES B3 says absence is typed, and a header is the only
place a 204 can carry the type without changing the wire a node parses.

ADDED, and opt-in only: `addressed=1` (divergence 031). The plain route is addressing-
agnostic and its body carries `to` but never `cc`, so a client cannot recompute from it the
set the live push delivers (apps/wsgw/mesh_notify.py, divergence 020: shared/mesh-chat
only, addressed by `to` or `cc` or a broadcast). `tools/meshy_node listen --monitor`
catches up here and then goes live on the socket, and needs the two to be one set. With
`addressed=1` the route:
  * refuses, 403 and typed, a caller that cannot read shared/mesh-chat
    (`permissions.check_permission`, the graph routes' gate), where the plain route
    answers 204;
  * scans shared/mesh-chat only, keeps a row exactly when the push would nudge the caller
    for it (`mesh_notify.addressing_of`, `peer_labels`, `role_for`: the push's own
    functions, not a re-expression), and answers each kept row as the push's own frame
    for this caller (`mesh_notify.nudge_from_columns(...).frame(role, readable=...)`,
    divergence 033: the caller's `role`, `to` as a list, and `cc` as a list and the body's
    first 160 characters when memory.read without a capability would show THIS caller
    the row (the wait takes no token, divergence 040): `mesh_notify.
    preview_gates`, divergence 027's gate, fail-closed, with the caller as the context
    route reads it, a DPoP proof for this GET included, asked about each row's own key, the
    reader's half once for the page, and run in the transaction that reads the rows);
  * pages by the SERVER'S COMMIT ORDER, not by `(created_at, id)` (STATUS E13, below), and
    names in `cursor` the last row it SCANNED, which can be past the last row it
    returned, and on a 204 names that position in `X-Meshy-Cursor`, so rows for other
    peers are passed over once, not re-read on every call;
  * answers 200 with an EMPTY `messages` when a read stopped on its page budget among
    rows for other peers, so the caller goes on from `cursor` rather than taking a 204
    for "nothing left" while rows for it may still lie ahead;
  * resolves a missing or malformed cursor as the plain route does, to the newest row: the
    current end of the commit order (0 when the mesh holds no row yet, so the first row
    that ever lands is after it);
  * answers `timeout=0` by READING and answering at once (the plain route answers 204
    without reading), so a catch-up never parks;
  * marks every 200 and 204 with `X-Meshy-Addressed: 1`, so a client can tell a server
    that applied the filter from one that ignored an unknown parameter;
  * names on every 200 and 204, in `X-Meshy-Resume-Cursor`, where a caller that stops
    here should RESUME. Since E13 that is the scan position itself (below); until then it
    was held ADDRESSED_SETTLE_SECS behind the database clock, and a write open longer than
    that could still be passed over.
Nothing on the plain route changes: no new header, the same body, the same timeouts.

THE ADDRESSED CURSOR IS A SERVER SEQUENCE (STATUS E13; roadmap 6; divergence 031, amended
2026-09-26). `created_at` is assigned at the writing TRANSACTION's start,
so a row can commit AFTER a later-stamped row has been read; a walk keyed on it steps over
that row. The addressed mode walks `mesh_commit_order.seq` instead (sql_meshychat/023, fin/
w2-jobs's D19): drawn when the row's transaction COMMITS, under a lock that makes seq order the
order in which rows became visible, so a reader that sees seq N sees every seq below it and no
row can later take a seq below one already read. Its cursor is the text
`seq:<n>:<created_at_micros>:<uuid>`, n and the row at it (`seq:0` before any row), base64url
without padding (`encode_seq_cursor`), which a client stores and returns and never needs to
read: opaque, and never a legal plain cursor, so the plain decoder refuses it (and answers
"no cursor") rather than misreading it. The row is there so a read can check
that n is still its place: after 023 is rolled back and applied again the backfill renumbers,
and a cursor whose row is not at n is placed again from the row (`resolve_seq`, review
finding 6). The resume point is the
scan position: nothing to hold back, nothing to re-read, nothing to dedupe. A plain cursor
(what this mode answered before E13, and what a node saved then) is still taken, once:
`seq_after_legacy` starts just before the first row, in commit order, that lies after it by
`(created_at, id)`, so no row after it is skipped (a few before it may be answered again, for
the caller's recorded keys to dedupe). The server assigns the sequence and the CURSOR stays
the recipient's (the monitor's sidecar). THE PLAIN MODE KEEPS `(created_at, id)` over EVERY
readable namespace, and the commit order is kept for shared/mesh-chat only.

NO CONNECTION IS HELD WHILE WAITING. Each re-read opens a guarded connection, reads, and
closes it; a waiter holding a Postgres backend for 60 s would exhaust `max_connections` long
before 512 waiters. The process's subscriber holds ONE connection, for LISTEN, whatever the
number of waiters.
"""
from __future__ import annotations

import base64
import binascii
import dataclasses
import datetime as _dt
import json
import logging
import re
import threading
import time
import uuid

import psycopg
from psycopg.rows import dict_row

from apps.core import dbconn
from apps.memoryskills.permissions import answers_admitting
from apps.memoryskills.validation import MESH_CHAT_NAMESPACE
from apps.peers import errors
from apps.peers import names as peer_names
from apps.peers.timefmt import format_rfc3339
from apps.wsgw import envelope, mesh_notify

from . import wake

log = logging.getLogger("meshychat.mesh.wait")

#: Wait defaults and bounds.
DEFAULT_TIMEOUT_SECS = 25
MAX_TIMEOUT_SECS = 60
JWT_EXPIRY_SKEW_SECS = 5
ROW_LIMIT = 50

#: A module attribute so a test can shorten it; production never changes it.
POLL_SECS = 2.0

#: Bound concurrent waiters.
MAX_WAITERS = 512

#: The header added by divergence 018. Additive: no node reads it, B3 is why it exists.
VISIBLE_NAMESPACES_HEADER = "X-Meshy-Visible-Namespaces"

_waiters = threading.BoundedSemaphore(MAX_WAITERS)


def try_acquire_waiter() -> bool:
    """Acquire a waiter permit without blocking."""
    return _waiters.acquire(blocking=False)


def release_waiter() -> None:
    _waiters.release()


# --------------------------------------------------------------------------- #
# The cursor: base64url-nopad('<micros>:<uuid>')
# --------------------------------------------------------------------------- #

_B64URL = re.compile(r"[A-Za-z0-9_-]*")
_I64 = re.compile(r"[+-]?[0-9]+")
_HEX = re.compile(r"[0-9A-Fa-f]{32}")
_HYPHENATED = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
                         r"[0-9A-Fa-f]{12}")
_I64_MIN, _I64_MAX = -(2 ** 63), 2 ** 63 - 1


def _days_from_civil(y: int, m: int, d: int) -> int:
    """Days since 1970-01-01 for any proleptic Gregorian date (H. Hinnant's algorithm).
    Python's `date` stops at year 1; the accepted cursor range does not."""
    y -= m <= 2
    era = (y if y >= 0 else y - 399) // 400
    yoe = y - era * 400
    doy = (153 * (m + (-3 if m > 2 else 9)) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


#: Compatibility-named `_CHRONO_*` constants hold the accepted cursor-datetime range,
#: -262143-01-01 through 262142-12-31. Values outside it are malformed cursors.
_MIN_INSTANT_MICROS = _days_from_civil(-262143, 1, 1) * 86_400 * 1_000_000
_MAX_INSTANT_MICROS = (_days_from_civil(262142, 12, 31) + 1) * 86_400 * 1_000_000 - 1


def _b64url_nopad_encode(raw: bytes) -> str:
    """Encode URL-safe Base64 without padding. Both cursor formats (the plain one and E13's
    seq one) are written by this."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_nopad_decode(raw: str) -> bytes | None:
    """Decode strict URL-safe Base64 with the four refusals `decode_cursor` lists; None for
    each. One copy behind both cursor decoders, so the plain and seq cursor cannot drift apart
    in how strictly their Base64 is read."""
    if not _B64URL.fullmatch(raw) or len(raw) % 4 == 1:
        return None
    try:
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (binascii.Error, ValueError):
        return None
    if _b64url_nopad_encode(decoded) != raw:
        return None
    return decoded


@dataclasses.dataclass(frozen=True)
class Cursor:
    """A plain cursor's timestamp and UUID."""

    created_at_micros: int
    id: uuid.UUID


def encode_cursor(created_at_micros: int, row_id) -> str:
    """Encode a cursor with a hyphenated lowercase UUID."""
    raw = f"{int(created_at_micros)}:{uuid.UUID(str(row_id))}".encode("ascii")
    return _b64url_nopad_encode(raw)


def _parse_uuid(text: str) -> uuid.UUID | None:
    """Accept simple, hyphenated, braced or urn:uuid: UUID forms and nothing else.
    Python's `uuid.UUID` is more lenient (it drops hyphens anywhere), so malformed cursors
    must be refused here too."""
    if len(text) == 32 and _HEX.fullmatch(text):
        return uuid.UUID(hex=text)
    if len(text) == 38 and text[0] == "{" and text[-1] == "}":
        text = text[1:-1]
    elif len(text) == 45 and text.startswith("urn:uuid:"):
        text = text[9:]
    if len(text) == 36 and _HYPHENATED.fullmatch(text):
        return uuid.UUID(text)
    return None


def decode_cursor(raw: str | None) -> Cursor | None:
    """Decode a cursor strictly. None means MALFORMED, which the caller treats as "no cursor",
    never as an error.

    Cursor decoding refuses padding, characters outside the URL-safe alphabet, an impossible
    length and non-zero trailing bits. Python's decoder accepts all four, so each is checked
    here, the last by requiring the input to be the canonical encoding of what it decodes to.
    """
    if raw is None:
        return None
    decoded = _b64url_nopad_decode(raw)
    if decoded is None:
        return None
    try:
        text = decoded.decode("utf-8")
    except UnicodeDecodeError:
        return None
    created, sep, id_text = text.partition(":")
    if not sep or not _I64.fullmatch(created):
        return None
    micros = int(created)
    if not (_I64_MIN <= micros <= _I64_MAX):
        return None
    if not (_MIN_INSTANT_MICROS <= micros <= _MAX_INSTANT_MICROS):
        return None
    row_id = _parse_uuid(id_text)
    if row_id is None:
        return None
    return Cursor(micros, row_id)


# --------------------------------------------------------------------------- #
# The addressed mode's cursor, STATUS E13:
#   base64url-nopad('seq:<n>:<created_at_micros>:<uuid>'), or 'seq:0' before any row
# --------------------------------------------------------------------------- #

@dataclasses.dataclass(frozen=True)
class SeqCursor:
    """A position in the commit order (`mesh_commit_order.seq`): every row with a seq at or
    below it has been read.

    `anchor`: the row whose seq IS `seq`, as its `(created_at, id)`; None only at 0, before
    any row. A number alone does not say which numbering it belongs to, and 023's backfill,
    applied again after its down script, numbers the surviving rows densely from 1: a bare
    `seq:K` saved before would then lie past rows that landed after it (review finding 6 of
    E13). The anchor is how a read checks that the number is still that row's place
    (`resolve_seq`) and, when it is not, places the reader again from the row itself. Not
    compared: two positions with one seq are one position in one numbering."""

    seq: int
    anchor: Cursor | None = dataclasses.field(default=None, compare=False)


_SEQ_TEXT = re.compile(r"seq:(?:(0)|([1-9][0-9]{0,18}):(0|-?[1-9][0-9]{0,18}):"
                       r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}))")


def encode_seq_cursor(seq: int, anchor: Cursor | None = None) -> str:
    """`seq:0`, or `seq:<n>:<micros>:<uuid>` naming the row at n (`anchor`), which every
    position past 0 has (a position is always a row's: `_row_position`)."""
    if int(seq) == 0:
        text = "seq:0"
    elif anchor is None:
        raise ValueError(f"seq position {seq} without the row at it (review finding 6)")
    else:
        text = f"seq:{int(seq)}:{int(anchor.created_at_micros)}:{uuid.UUID(str(anchor.id))}"
    return _b64url_nopad_encode(text.encode("ascii"))


def decode_seq_cursor(raw: str | None) -> SeqCursor | None:
    """The canonical encoding of `seq:0`, or of `seq:<n>:<micros>:<uuid>` with n a positive
    63-bit integer and micros a 64-bit one, both written without a plus sign or leading zero,
    and the uuid hyphenated and lower case, as `encode_seq_cursor` writes them; None
    otherwise (read as "no cursor", the rule a malformed plain cursor follows). A bare
    `seq:<n>` past 0 names no row, cannot be checked against the numbering, and is refused.
    As strict as `decode_cursor`: no padding, only the url-safe alphabet, and the input must
    be the canonical encoding of what it decodes to."""
    if not raw:
        return None
    decoded = _b64url_nopad_decode(raw)
    if decoded is None:
        return None
    match = _SEQ_TEXT.fullmatch(decoded.decode("ascii", "replace"))
    if match is None:
        return None
    if match.group(1) is not None:
        return SeqCursor(0)
    seq, micros = int(match.group(2)), int(match.group(3))
    if seq > _I64_MAX or not (_I64_MIN <= micros <= _I64_MAX):
        return None
    return SeqCursor(seq, Cursor(micros, uuid.UUID(match.group(4))))


_EPOCH = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)
_ONE_US = _dt.timedelta(microseconds=1)
_PY_MIN_MICROS = (_dt.datetime(1, 1, 1, tzinfo=_dt.timezone.utc) - _EPOCH) // _ONE_US
_PY_MAX_MICROS = (_dt.datetime(9999, 12, 31, 23, 59, 59, 999999,
                               tzinfo=_dt.timezone.utc) - _EPOCH) // _ONE_US


def micros_of(ts: _dt.datetime) -> int:
    """Use integer arithmetic, not float, so a cursor names an instant to the microsecond."""
    return (ts - _EPOCH) // _ONE_US


def _bind_instant(micros: int):
    """The cursor instant as a value Postgres compares against.

    A decoded cursor can name an instant Python's datetime cannot hold (the accepted range
    permits years to +/-262142). Past year 9999 nothing can be newer than it, so it binds as
    'infinity'; before year 1 everything is, so '-infinity'. This includes instants below
    Postgres's own floor (4714 BC), which are answered normally; divergence 018 records that
    edge.
    """
    if micros > _PY_MAX_MICROS:
        return "infinity"
    if micros < _PY_MIN_MICROS:
        return "-infinity"
    return _EPOCH + _dt.timedelta(microseconds=micros)


# This wire format uses a `Z`; the fraction is omitted, or 3 or 6 digits, whichever is exact
# (Postgres keeps microseconds, so the 9-digit case cannot arise). `rfc3339` keeps the established
# helper name and delegates to `format_rfc3339` in apps/peers/timefmt.py. The only value reaching
# it is `shared_memory.created_at`, a TIMESTAMPTZ, so it is always aware and the shared formatter's
# naive refusal never fires.
rfc3339 = format_rfc3339


# --------------------------------------------------------------------------- #
# Parameters and the effective timeout
# --------------------------------------------------------------------------- #

@dataclasses.dataclass(frozen=True)
class WaitParams:
    """Parsed wait query parameters."""

    since: str | None
    timeout: int | None
    #: Divergence 031: `addressed=1` selects addressed mode.
    addressed: bool = False
    #: STATUS E13: `seq=1`, the addressed mode's opt-in to the commit order's cursor. A
    #: client that neither sends it nor sends a seq cursor keeps the walk it had before E13.
    seq: bool = False


_U64 = re.compile(r"\+?[0-9]+")
_U64_MAX = 2 ** 64 - 1


def parse_params(query) -> WaitParams:
    """Unknown keys are ignored; a repeated key or a `timeout` that is not a u64 is a 400
    before the handler runs, waiter permit acquisition and authentication.

    `+` in a query string is a space, so `timeout=+5` fails and `timeout=%2B5` (a literal
    plus accepted by the integer parser) passes.
    """
    for key in ("since", "timeout", "addressed", "seq"):
        if len(query.getlist(key)) > 1:
            raise errors.validation(
                f"Failed to deserialize query string: duplicate field `{key}`")
    addressed = query.get("addressed")
    if addressed not in (None, "0", "1"):
        raise errors.validation("addressed must be 0 or 1 (divergence 031)")
    seq = query.get("seq")
    if seq not in (None, "0", "1"):
        raise errors.validation("seq must be 0 or 1 (divergence 031, STATUS E13)")
    since = query.get("since")
    raw_timeout = query.get("timeout")
    timeout = None
    if raw_timeout is not None:
        if not _U64.fullmatch(raw_timeout) or int(raw_timeout) > _U64_MAX:
            raise errors.validation(
                "Failed to deserialize query string: timeout: invalid digit found in string")
        timeout = int(raw_timeout)
    return WaitParams(since=since, timeout=timeout, addressed=addressed == "1",
                      seq=seq == "1")


def effective_timeout(requested: int | None, exp: int, now: int) -> int:
    """The token budget floors at zero, the request defaults to 25 and caps at 60, and the
    smaller of the two wins."""
    token_budget = max(0, int(exp) - int(now) - JWT_EXPIRY_SKEW_SECS)
    wanted = min(DEFAULT_TIMEOUT_SECS if requested is None else requested, MAX_TIMEOUT_SECS)
    return min(wanted, token_budget)


# --------------------------------------------------------------------------- #
# The visibility query
# --------------------------------------------------------------------------- #

#: `peer_vis_cte`: an ACTIVE caller (else nothing is visible) and its `vis` set. Since
#: divergence 040 slice 3, `vis` reads memory.read's predicate set form,
#: `meshychat_namespace_permission(peer)`, which check_permission's
#: one-namespace form reads too, and keeps the namespaces whose answer admits a read
#: (`%(readable)s`, from `answers_admitting("read")`). The planner inlines the set form, so
#: this reads the two tables, not a function call per namespace. Every permission a live grant
#: can hold admits a read, so the set form (newest live row) and a plain any-live-row check agree
#: (and sql/028's unique index allows one live row per grantee anyway).
_PEER_VIS_CTE = (
    "WITH peer AS ( "
    "    SELECT id FROM peers WHERE id = %(peer)s AND status = 'active' "
    "), "
    "vis AS ( "
    "    SELECT a.namespace FROM peer p, LATERAL meshychat_namespace_permission(p.id) a "
    "    WHERE a.permission = ANY(%(readable)s) "
    ") "
)

#: The answers of `meshychat_namespace_permission` that put a namespace in `vis`.
_READABLE = answers_admitting("read")


def _vis_params(peer_id) -> dict:
    """The parameters `_PEER_VIS_CTE` itself reads (a fresh list: psycopg adapts a list,
    not a tuple, to an array)."""
    return {"peer": str(peer_id), "readable": list(_READABLE)}


def _base_where(with_cursor: bool) -> str:
    """Build the row filter. The cursor comparison is STRICT (`>`), so the row a cursor names
    is never returned again. `%%` is psycopg's escape for `%`."""
    cursor = ("AND (sm.created_at, sm.id) > (%(after_at)s::timestamptz, %(after_id)s::uuid) "
              if with_cursor else "")
    return ("sm.is_head "
            "AND sm.key LIKE 'msg/%%' "
            f"{cursor}"
            "AND sm.namespace IN (SELECT namespace FROM vis) "
            "AND sm.created_by_peer_id <> p.id")


def _params(peer_id, since: Cursor | None) -> dict:
    params = _vis_params(peer_id)
    if since is not None:
        params["after_at"] = _bind_instant(since.created_at_micros)
        params["after_id"] = str(since.id)
    return params


def visible_namespaces(conn, peer_id) -> list[str]:
    """The `vis` set itself: what the caller can read. Under the caller's
    `server_internal` context, like every function below (shared_memory,
    shared_memory_namespace and shared_memory_grant all key on it, sql/028)."""
    with conn.cursor() as cur:
        cur.execute(_PEER_VIS_CTE + "SELECT namespace FROM vis ORDER BY namespace",
                    _vis_params(peer_id))
        return [r[0] for r in cur.fetchall()]


def current_cursor(conn, peer_id, namespace: str | None = None) -> Cursor | None:
    """Return the newest row the caller could be woken by, or None when it can see none (the
    wait then returns the first row that becomes visible). `namespace`
    narrows it to one namespace (the addressed mode's, divergence 031)."""
    sql = (_PEER_VIS_CTE +
           "SELECT sm.created_at, sm.id FROM shared_memory sm JOIN peer p ON TRUE "
           "WHERE " + _base_where(False) +
           (" AND sm.namespace = %(ns)s" if namespace is not None else "") +
           " ORDER BY sm.created_at DESC, sm.id DESC LIMIT 1")
    params = _params(peer_id, None)
    if namespace is not None:
        params["ns"] = namespace
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return None if row is None else Cursor(micros_of(row[0]), row[1])


def matching_rows(conn, peer_id, since: Cursor | None) -> list[dict]:
    """Return up to 50 readable rows after `since`, oldest first."""
    sql = (_PEER_VIS_CTE +
           "SELECT sm.key, sm.value->>'from' AS from_label, sm.value->>'to' AS to_label, "
           "       sm.created_at, sm.id "
           "FROM shared_memory sm JOIN peer p ON TRUE "
           "WHERE " + _base_where(since is not None) +
           f" ORDER BY sm.created_at ASC, sm.id ASC LIMIT {ROW_LIMIT}")
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, _params(peer_id, since))
        return cur.fetchall()


def effective_since(conn, peer_id, raw_since: str | None) -> Cursor | None:
    """Return the valid cursor, or the caller's current cursor."""
    cursor = decode_cursor(raw_since)
    if cursor is not None:
        return cursor
    return current_cursor(conn, peer_id)


# --------------------------------------------------------------------------- #
# Responses
# --------------------------------------------------------------------------- #

def ok_body(rows: list[dict]) -> bytes:
    """Build a compact UTF-8 response with fields in declaration order (`key`, `from`, `to`,
    `created_at`; then `messages`, `cursor`)."""
    last = rows[-1]
    body = {
        "messages": [
            {"key": r["key"], "from": r["from_label"], "to": r["to_label"],
             "created_at": rfc3339(r["created_at"])}
            for r in rows
        ],
        "cursor": encode_cursor(micros_of(last["created_at"]), last["id"]),
    }
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


# --------------------------------------------------------------------------- #
# The loop
# --------------------------------------------------------------------------- #

@dataclasses.dataclass(frozen=True)
class FirstRead:
    """The reads a wait does on the request's own connection, before it parks."""

    #: The cursor the wait is keyed on: the caller's, or the newest visible row.
    since: Cursor | None
    #: Readable rows already after `since`. Non-empty means answer 200 without parking.
    rows: list
    #: len(vis), for the X-Meshy-Visible-Namespaces header.
    visible: int
    #: STATUS F12: every namespace the caller can read is one the nudge covers
    #: (NUDGED_NAMESPACES), so a park may wait for a nudge alone.
    nudged: bool = False


#: The namespaces whose `msg/` inserts sql/036's trigger announces (`mesh_msg`).
NUDGED_NAMESPACES = frozenset({MESH_CHAT_NAMESPACE})


def first_read(conn, peer_id, raw_since: str | None) -> FirstRead:
    """Resolve the cursor, then run the fast query in one `server_internal` transaction on the
    request's guarded connection, which the caller
    closes BEFORE parking so no Postgres backend is held while the wait sleeps."""
    with dbconn.request_server_internal(conn) as c:
        names = visible_namespaces(c, peer_id)
        since = effective_since(c, peer_id, raw_since)
        rows = matching_rows(c, peer_id, since)
    return FirstRead(since=since, rows=rows, visible=len(names),
                     nudged=set(names) <= NUDGED_NAMESPACES)


def count_visible(conn, peer_id) -> int:
    """Return len(vis) alone for the zero-timeout 204 that reads no rows."""
    with dbconn.request_server_internal(conn) as c:
        return len(visible_namespaces(c, peer_id))


def _poll_once(peer_id, since: Cursor | None) -> list:
    """One re-query on a connection of its own, opened and closed around the read."""
    with dbconn.serving_connection() as conn:
        with dbconn.request_server_internal(conn) as c:
            return matching_rows(c, peer_id, since)


#: Re-reads of a parked wait the database refused (review finding 4 of F12), this process.
REREAD_FAILURES = 0


def _reread(read):
    """One re-read of a parked wait, or None when the database refused it (a
    psycopg.Error: past max_connections, a dropped connection). The waiter then waits on and
    polls: a failed re-read loses nothing (a 204 at the deadline names the position the
    caller already holds), where letting it escape answered the caller 500."""
    global REREAD_FAILURES
    try:
        return read(), True
    except psycopg.Error as exc:
        REREAD_FAILURES += 1
        log.warning("mesh wait: a re-read failed, the waiter polls on: %s: %s",
                    type(exc).__name__, exc)
        return None, False


def _parked(peer_id, eff_timeout: float, read, *, nudged: bool, labels=None,
            clock=time.monotonic):
    """The park itself (STATUS F12): register with the process's waiter registry, start its
    subscriber if it is not running, re-read once because a row committed between the first
    read and registration was announced to nobody, then wait on the waiter's event and
    re-read on every wake, until `read()` answers something other than None (returned) or
    the deadline passes (None). `nudged`: the nudge covers every row `read` can answer, so
    while the subscriber is listening the waiter waits for a nudge alone; otherwise it also
    re-reads every POLL_SECS. The event is cleared BEFORE each re-read, so a nudge that
    lands during one is not lost. `labels`: an addressed wait's (`caller_labels`), so the
    subscriber wakes it only for a row the push would send it (review finding 4); None for
    a plain wait. A re-read the database refuses (`_reread`) is retried at the poll
    interval, never raised.

    The deadline is checked after each wait and wins over a re-read due at the same moment,
    returning 204 without re-querying. The deadline starts here, after the first read, not
    when the request arrived.

    A re-read follows ONLY a nudge (the event set) or a poll tick (a full POLL_SECS wait). A wait
    that returns with neither, cut
    short by the deadline and returning a hair before it by `clock` (a timer coarser than
    the clock), loops back to wait out the rest without a read (STATUS E21: it re-read, one
    wasted scan per timed-out waiter). The event is cleared only when it WAS set, so a nudge
    that lands after such a wait returned is kept for the next one.
    """
    waiter = wake.REGISTRY.register(peer_id, labels)
    try:
        deadline = clock() + eff_timeout
        if deadline - clock() <= 0:
            return None
        wake.SUBSCRIBER.ensure_started()
        found, ok = _reread(read)
        if found is not None:
            return found
        while True:
            remaining = deadline - clock()
            if remaining <= 0:
                return None
            alone = ok and nudged and wake.SUBSCRIBER.is_listening()
            tick = not alone and POLL_SECS < remaining
            nudge = waiter.event.wait(POLL_SECS if tick else remaining)
            if nudge:
                waiter.event.clear()
            if clock() >= deadline:
                return None
            if not nudge and not tick:
                continue
            found, ok = _reread(read)
            if found is not None:
                return found
    finally:
        wake.REGISTRY.unregister(waiter)


def park(peer_id, since: Cursor | None, eff_timeout: float, *, nudged: bool = False,
         clock=time.monotonic) -> list:
    """The plain mode's park: `_parked` over `_poll_once`, the rows (or [] at the
    deadline). `nudged`: `FirstRead.nudged`, the caller reads only what the nudge covers."""
    rows = _parked(peer_id, eff_timeout, lambda: _poll_once(peer_id, since) or None,
                   nudged=nudged, clock=clock)
    return rows or []


# --------------------------------------------------------------------------- #
# The addressed mode, divergence 031: the push's set, read from the log
# --------------------------------------------------------------------------- #

#: On every 200 and 204 of an addressed wait. A server that ignores the parameter answers
#: without it, and with the whole mesh.
ADDRESSED_HEADER = "X-Meshy-Addressed"
#: On an addressed 204: the position the wait read up to, a cursor in the wire format.
CURSOR_HEADER = "X-Meshy-Cursor"
#: How many ROW_LIMIT pages one read scans for a row addressed to the caller before it
#: answers with its position alone. Bounds one request's work on a mesh busy with other
#: peers' traffic; the caller resumes from the position.
ADDRESSED_SCAN_PAGES = 20
#: The plain-cursor position before any row (0 us, the nil uuid): `since=start` on the
#: backlog summary (apps/meshsum). The addressed mode's own beginning is seq 0.
BEGINNING = Cursor(0, uuid.UUID(int=0))
#: On every addressed 200 and 204: where to RESUME, a cursor in the wire format (below).
RESUME_HEADER = "X-Meshy-Resume-Cursor"
#: How far behind the database clock a `(created_at, id)` walk holds its resume point. A
#: row's `created_at` is its writing transaction's START (sql/028, `DEFAULT NOW()`), so a
#: write still open when a read runs lands, once committed, BEHIND rows that read already
#: passed. Since E13 the addressed wait walks the commit order and needs no hold; the backlog
#: summary's read (apps/meshsum/window.py) still walks `(created_at, id)` and holds this.
ADDRESSED_SETTLE_SECS = 10


def caller_labels(conn, peer_id) -> frozenset[str]:
    """The labels the push answers for this peer (`mesh_notify.peer_labels`: roster label,
    `peers.name`, `peer-<id8>`, lower-cased), from the same `peers` column it reads."""
    name = peer_names.name_of(conn, peer_id)                    # divergence 066
    return mesh_notify.peer_labels(uuid.UUID(str(peer_id)), name)


def _addressed_page(conn, peer_id, since: Cursor) -> list[tuple]:
    """One page of `matching_rows`' rows, narrowed to shared/mesh-chat (the only namespace
    the push serves), with the columns the push builds its nudge from."""
    sql = (_PEER_VIS_CTE +
           "SELECT sm.key, sm.created_by_peer_id, sm.created_at, sm.id, " +
           mesh_notify.nudge_columns("sm.") +
           " FROM shared_memory sm JOIN peer p ON TRUE "
           "WHERE " + _base_where(True) + " AND sm.namespace = %(ns)s"
           f" ORDER BY sm.created_at ASC, sm.id ASC LIMIT {ROW_LIMIT}")
    params = _params(peer_id, since)
    params["ns"] = MESH_CHAT_NAMESPACE
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


#: `_addressed_page`'s public name, for the one other reader of exactly this set: a node's
#: backlog summary (divergence 037, apps/meshsum/window.py), which must summarise the rows
#: this wait would answer, read by this query and never re-expressed.
addressed_page = _addressed_page


#: The columns after the nudge's that a commit-order row adds (divergence 053 and 059): `kind`
#: ('write', 'redaction' or 'edit'), and, for a redaction or an edit only, the record's version,
#: its time as the frame's own `Z` text, the reason code (a redaction's; NULL for an edit), the
#: seats besides the row's recipients that are to be told (the authors of its replies), the seat
#: that acted (who already knows), and the row's `author_label` (an edit's `edited_by`). A row
#: can have BOTH records (an edited version redacted later), so each column takes its record by
#: the position's `kind`, never by which join happened to answer: an edit position of a version
#: that was redacted since is still an EDIT event, with the edit's own time and seats.
_EVENT_COLUMNS = (
    "o.kind, CASE o.kind WHEN 'redaction' THEN r.version WHEN 'edit' THEN e.version END, "
    "to_char((CASE o.kind WHEN 'redaction' THEN r.redacted_at WHEN 'edit' THEN e.edited_at END) "
    "AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'), "
    "CASE WHEN o.kind = 'redaction' THEN r.reason_code END, "
    "CASE o.kind WHEN 'redaction' THEN r.notify_peer_ids WHEN 'edit' THEN e.notify_peer_ids END, "
    "CASE o.kind WHEN 'redaction' THEN r.redacted_by_peer_id WHEN 'edit' THEN e.edited_by_peer_id END, "
    "sm.author_label")


def _addressed_seq_page(conn, peer_id, after: int) -> list[tuple]:
    """`_addressed_page` in COMMIT order (STATUS E13): up to ROW_LIMIT positions of the commit
    order of shared/mesh-chat past `after`, lowest seq first, each as (seq, key, author,
    created_at, id, *the push's nudge columns, *_EVENT_COLUMNS).

    A `write` position is a readable head `msg/` row the caller did not author, by
    `_base_where` without its cursor clause: the same visibility filter as ever. A
    `redaction` position (sql_meshychat/027) is the row's REDACTION, and is not a head
    (`version: all` redacts old versions too) and is not filtered by author: whether the
    caller is told is decided per caller by `scan_addressed` from the tombstone's own `to` and
    `cc`, the record's reply authors and the redacting seat. An `edit` position
    (sql_meshychat/029) is the row's EDIT, with the same shape: not a head (a second edit
    retires the first version, and a listener that was down is still owed its event) and not
    filtered by author, decided per caller by `edit_event`. Each still needs the namespace
    to be readable by the caller. The seq is the primary key's range."""
    sql = (_PEER_VIS_CTE +
           "SELECT o.seq, sm.key, sm.created_by_peer_id, sm.created_at, sm.id, " +
           mesh_notify.nudge_columns("sm.") + ", " + _EVENT_COLUMNS +
           " FROM mesh_commit_order o JOIN shared_memory sm ON sm.id = o.memory_id"
           " JOIN peer p ON TRUE"
           " LEFT JOIN shared_memory_redaction r ON r.memory_id = o.memory_id"
           " LEFT JOIN shared_memory_edit e ON e.memory_id = o.memory_id"
           " WHERE o.seq > %(after_seq)s AND sm.namespace = %(ns)s AND ("
           "   (o.kind = 'write' AND " + _base_where(False) + ")"
           "   OR (o.kind = 'redaction' AND r.id IS NOT NULL AND sm.key LIKE 'msg/%%'"
           "       AND sm.namespace IN (SELECT namespace FROM vis))"
           "   OR (o.kind = 'edit' AND e.id IS NOT NULL AND sm.key LIKE 'msg/%%'"
           "       AND sm.namespace IN (SELECT namespace FROM vis)))"
           f" ORDER BY o.seq ASC LIMIT {ROW_LIMIT}")
    params = _params(peer_id, None)
    params["after_seq"] = int(after)
    params["ns"] = MESH_CHAT_NAMESPACE
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _row_position(conn, below: int | None = None) -> SeqCursor:
    """The row with the highest seq (below `below`, when given) as a position naming it, or
    SeqCursor(0) when there is none. Every position this module answers is a row's, so its
    cursor can carry the row (`SeqCursor.anchor`); with no row between it and `below`, the
    row just below `below` is the same position as `below - 1`. The primary key read
    backwards, one entry, and one primary-key probe of shared_memory.

    EVERY KIND of position counts, on purpose (divergence 053): the end of the order includes a redaction position, so a seat
    whose first call comes after a redaction starts after it, and a cursor saved at a redaction event is the redaction's."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT o.seq, sm.created_at, sm.id FROM mesh_commit_order o "
            "JOIN shared_memory sm ON sm.id = o.memory_id "
            + ("WHERE o.seq < %s " if below is not None else "")
            + "ORDER BY o.seq DESC LIMIT 1",
            (int(below),) if below is not None else ())
        row = cur.fetchone()
    if row is None:
        return SeqCursor(0)
    return SeqCursor(int(row[0]), Cursor(micros_of(row[1]), row[2]))


def end_position(conn) -> SeqCursor:
    """The end of the commit order as this statement sees it (SeqCursor(0) when nothing is
    stamped). Every seq at or below it is visible here too: the stamping lock releases only
    after its transaction is visible."""
    return _row_position(conn)


#: Rows per page of `seq_after_legacy`'s backward walk.
LEGACY_WALK_PAGE = 100


def _commit_order_below(conn, below: int | None) -> list[tuple]:
    """Up to LEGACY_WALK_PAGE rows of the commit order below `below` (from its end when
    None), highest seq first, as (seq, created_at, id, kind): the primary key read backwards, then
    one primary-key probe of shared_memory per row. The probe is a LATERAL subquery with
    `LIMIT 1` (the id is the key, so it changes no answer) because a plain LATERAL is
    flattened into a join the planner may answer by reading shared_memory whole (measured:
    a hash join over all 601 rows for a 100-row page).

    NO `kind` PREDICATE, on purpose (divergence 053): one lets the planner answer from UNIQUE (memory_id, kind) and read every
    position (measured with the first form: 703 tuples, then 1303, for the same 100-row page). The page carries `kind` and a
    caller that wants writes only skips a redaction position itself."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT o.seq, sm.created_at, sm.id, o.kind FROM ("
            "  SELECT seq, memory_id, kind FROM mesh_commit_order "
            + ("WHERE seq < %s " if below is not None else "")
            + f"  ORDER BY seq DESC LIMIT {LEGACY_WALK_PAGE}) o "
            "CROSS JOIN LATERAL (SELECT created_at, id FROM shared_memory "
            "                    WHERE id = o.memory_id LIMIT 1) sm "
            "ORDER BY o.seq DESC",
            (int(below),) if below is not None else ())
        return cur.fetchall()


def seq_after_legacy(conn, cursor: Cursor) -> SeqCursor:
    """A plain `(created_at, id)` cursor (what the addressed mode answered before E13, or a
    seq cursor's own row when its number no longer names it, `resolve_seq`) as a position
    that skips no row after it: just before the lowest seq of any mesh row after it by
    `(created_at, id)`; the current end when there is none. Rows before the cursor with a
    higher seq (they committed late) are answered again; the caller dedupes.

    BOUNDED (B5, review finding 2 of E13): not a `min()` over the whole commit order, which
    read every row on each call, but a walk BACKWARDS from its end, page by page, that stops
    at the first row stamped ADDRESSED_SETTLE_SECS or more before the cursor's instant. It
    reads the rows committed since about then: what lies after the cursor (the backlog the
    caller is about to page through anyway), never the history before it. Why stopping
    there skips nothing: a row S stamped (its writing transaction began) that long before
    the cursor committed before the cursor's instant, and every row with a lower seq
    committed before S did, so began before that instant too and is not after the cursor.
    That holds for a write shorter than ADDRESSED_SETTLE_SECS, the bound the walk the cursor
    came from already stated (divergence 031)."""
    after = (cursor.created_at_micros, cursor.id.int)
    floor = cursor.created_at_micros - ADDRESSED_SETTLE_SECS * 1_000_000
    low = None
    below = None
    while True:
        rows = _commit_order_below(conn, below)
        for seq, created, row_id, kind in rows:
            if kind != "write":
                continue        # a redaction position is not a new row after the cursor (divergence 053)
            at = micros_of(created)
            if (at, row_id.int) > after:
                low = int(seq)
            elif at < floor:
                rows = []
                break
        else:
            if len(rows) == LEGACY_WALK_PAGE:
                below = int(rows[-1][0])
                continue
        break
    return end_position(conn) if low is None else _row_position(conn, low)


def resolve_seq(conn, since: SeqCursor) -> SeqCursor:
    """`since` as a position in the CURRENT numbering (review finding 6 of E13): as it is
    when its seq is 0 or is still its row's; otherwise placed again from its row
    (`seq_after_legacy` on the anchor), which is what happens after 023 was rolled back and
    applied again (the backfill renumbers) and after a retention sweep removed the row (its
    stamp goes with it). One primary-key probe."""
    if since.seq == 0 or since.anchor is None:
        # 0 is before every row in any numbering. No anchor: a position built in code, not
        # read from the wire (the decoder refuses a bare seq past 0), taken as it is.
        return since
    with conn.cursor() as cur:
        cur.execute("SELECT memory_id FROM mesh_commit_order WHERE seq = %s", (int(since.seq),))
        row = cur.fetchone()
    if row is not None and row[0] == since.anchor.id:
        return since
    return seq_after_legacy(conn, since.anchor)


def legacy_position(conn, since: SeqCursor) -> Cursor:
    """A seq cursor as a `(created_at, id)` position for a walk still keyed on that order
    (the backlog summary, apps/meshsum/window.py): just before the lowest `(created_at, id)`
    of any row past `since` in commit order, so the walk covers every one of them (and may
    cover rows already delivered, which only widens a summary). With no row past it, the
    row AT it (or BEGINNING when there is none). `since` is first read in the current
    numbering (`resolve_seq`)."""
    since = resolve_seq(conn, since)
    with conn.cursor() as cur:
        # A redaction position is not a row past `since` (divergence 053), and it is not a WHERE: a predicate on `kind` lets the
        # planner leave the primary key's range for UNIQUE (memory_id, kind) (see _commit_order_below). A redaction position
        # sorts LAST instead, so the first row is a write when any write is past `since`, and a redaction means there is none.
        cur.execute(
            "SELECT sm.created_at, sm.id, o.kind FROM mesh_commit_order o JOIN shared_memory sm "
            "ON sm.id = o.memory_id WHERE o.seq > %s "
            "ORDER BY (o.kind <> 'write'), sm.created_at, sm.id LIMIT 1",
            (int(since.seq),))
        row = cur.fetchone()
    if row is not None and row[2] == "write":
        return Cursor(micros_of(row[0]) - 1, uuid.UUID(int=(1 << 128) - 1))
    at = _last_write_at_or_below(conn, int(since.seq))
    return BEGINNING if at is None else Cursor(micros_of(at[0]), at[1])


def _last_write_at_or_below(conn, seq: int) -> tuple | None:
    """(created_at, id) of the highest WRITE position at or below `seq`, or None. It reads pages of `_commit_order_below`
    and skips a redaction position, so the reads stay bounded by the primary key (the row a cursor names is the last row that
    arrived, and a row redacted later is an old `created_at` at a new `seq`)."""
    below = seq + 1
    while True:
        rows = _commit_order_below(conn, below)
        for _seq, created, row_id, kind in rows:
            if kind == "write":
                return created, row_id
        if len(rows) < LEGACY_WALK_PAGE:
            return None
        below = int(rows[-1][0])


def redaction_event(nudge, extra, peer_id, labels) -> dict | None:
    """The `mesh_redaction` frame for one redaction position, or None when this caller is not to
    be told (divergence 053; the operator's rulings, ASDLC D-2026-09-29-U13). Told: the row's
    own recipients, by the very decision a message frame uses (`nudge.role` over the
    tombstone's `to` and `cc`, which a redaction keeps, and a broadcast), and the authors of
    its direct replies (`notify_peer_ids`, fixed on the record when it was written). Not told:
    the seat that redacted. The event names the row and says why, never what it said."""
    _kind, version, redacted_at, reason_code, notify, redactor, _label = extra
    if str(redactor) == str(peer_id):
        return None
    if nudge.role(labels) is None and str(peer_id) not in {str(p) for p in (notify or ())}:
        return None
    return envelope.mesh_redaction(nudge.key, version, redacted_at, reason_code)


def edit_event(nudge, extra, peer_id, labels) -> dict | None:
    """The `mesh_edit` frame for one edit position, or None when this caller is not to be told (divergence
    059; docs/design/edit.md section 4). Told: the row's own recipients, by the very decision a message frame
    uses (`nudge.role` over the head's `to` and `cc`, which an edit cannot change, and a broadcast), each with
    the role that admitted it, and the authors of its direct replies (`notify_peer_ids`, fixed on the record
    when it was written) in the role `replied`. Not told: the seat that edited. The event names the row, the
    version and the editor, never what it says now. `apps/wsgw/mesh_notify.py resolve_edit_recipients`
    decides the same way for the push, so the two paths cannot disagree."""
    _kind, version, edited_at, _reason, notify, editor, edited_by = extra
    if str(editor) == str(peer_id):
        return None
    role = nudge.role(labels)
    if role is None:
        if str(peer_id) not in {str(p) for p in (notify or ())}:
            return None
        role = mesh_notify.ROLE_REPLIED
    return envelope.mesh_edit(nudge.key, version, edited_at, edited_by, role)


def scan_addressed(conn, peer_id, since: SeqCursor | Cursor, labels,
                   reader) -> tuple[list[dict], SeqCursor | Cursor, bool]:
    """(frames, position, more): the push's frames for the rows after `since` that the push
    would nudge this caller for, in the order `since` names: IN COMMIT ORDER for a
    SeqCursor (STATUS E13), lowest seq first; by `(created_at, id)` for a plain Cursor (the
    walk a client that did not opt in keeps, review finding 1 of E13). Then the last row
    scanned, as a position of the same kind; and whether the scan stopped on its page
    budget (ADDRESSED_SCAN_PAGES full pages, none holding a frame) with rows perhaps left to
    read. Page by page until a page holds a frame, the rows run out, or the budget is
    spent. At most ROW_LIMIT frames: they all come from the one page that held any.

    `reader` (apps/meshsem/search.Reader): who asks, for the preview's gate
    (`mesh_notify.preview_gates`, divergence 033: 027's gate, fail-closed, in a savepoint,
    answering as the push's `preview_gate` does). It is asked once for the page that holds
    the frames, about EACH gated row's own key (divergence 040, slice 2): the half that
    depends on the reader alone once, the namespace predicate once per key (the slice-2
    review's F2). Until slice 2 the whole gate was asked once per call and the first row's
    answer stood for every row; an answer about one key must not stand for another. It is
    asked AFTER the rows it covers are read, on `conn`: the same transaction, as the push
    asks it beside its row. A gate that fails withholds the page's previews and never fails
    the answer."""
    frames: list[dict] = []
    position = since
    by_seq = isinstance(since, SeqCursor)
    for _ in range(ADDRESSED_SCAN_PAGES):
        if by_seq:
            rows = _addressed_seq_page(conn, peer_id, position.seq)
        else:
            rows = [(None, *row) for row in _addressed_page(conn, peer_id, position)]
        if not rows:
            return frames, position, False
        mine = []
        for _seq, key, author, _created, _id, *cols in rows:
            nudge = mesh_notify.nudge_from_columns(key, author, cols[:mesh_notify.NUDGE_COLUMNS])
            extra = cols[mesh_notify.NUDGE_COLUMNS:]
            if extra and extra[0] in ("redaction", "edit"):
                event = (redaction_event if extra[0] == "redaction" else edit_event)(nudge, extra, peer_id, labels)
                if event is not None:
                    mine.append((None, event, None))
                continue
            role = nudge.role(labels)
            if role is not None:
                mine.append((key, nudge, role))
        gated = [key for key, nudge, _role in mine if key is not None and nudge.gated]
        gate = mesh_notify.preview_gates(conn, reader, gated) if gated else {}
        for key, nudge, role in mine:
            if key is None:                        # a redaction event, already a frame, in its place in the order
                frames.append(nudge)
                continue
            frames.append(nudge.frame(role, readable=key in gate and gate[key] is None))
        last = rows[-1]
        position = (SeqCursor(int(last[0]), Cursor(micros_of(last[3]), last[4])) if by_seq
                    else Cursor(micros_of(last[3]), last[4]))
        if frames or len(rows) < ROW_LIMIT:
            return frames, position, False
    return frames, position, True


def settle_horizon(conn) -> Cursor:
    """ADDRESSED_SETTLE_SECS before the database's NOW(), as a cursor (the nil uuid, so a row
    stamped exactly then is after it). NOW() is the reading transaction's start, which is
    no later than the snapshot of any read in it, and it is the clock `created_at` is read
    from."""
    with conn.cursor() as cur:
        cur.execute("SELECT NOW() - make_interval(secs => %s)", (float(ADDRESSED_SETTLE_SECS),))
        (at,) = cur.fetchone()
    return Cursor(micros_of(at), uuid.UUID(int=0))


def resume_point(position: Cursor, horizon: Cursor) -> Cursor:
    """The earlier of a `(created_at, id)` scan position and the settle horizon, in the
    order Postgres compares `(created_at, id)` (a uuid compares as its 16 bytes, big-endian:
    `.int`). The resume point of the walk a client that did not opt in to the commit order
    keeps (divergence 031 as it stood before E13)."""
    def order(c: Cursor):
        return c.created_at_micros, c.id.int
    return position if order(position) <= order(horizon) else horizon


@dataclasses.dataclass(frozen=True)
class AddressedRead:
    """An addressed wait's first read, done on the request's connection before it parks."""

    frames: list
    #: The last row scanned: in commit order (a SeqCursor, also where a caller that stops
    #: here resumes), or by `(created_at, id)` for a client that did not opt in.
    position: SeqCursor | Cursor
    labels: frozenset
    visible: int
    #: The scan spent its budget on rows for others: answer at once, do not park.
    more: bool = False
    #: Who asked, for the preview's gate (`scan_addressed`); a park asks it again.
    reader: object = None
    #: The `(created_at, id)` walk only: `settle_horizon` as of this first read. A park keeps
    #: it: an earlier horizon only re-reads more, never less. None on the commit order.
    horizon: Cursor | None = None

    def resume(self, position: SeqCursor | Cursor) -> SeqCursor | Cursor:
        """Where a caller that stops at `position` resumes: the position itself on the
        commit order; held back to the settle horizon on the `(created_at, id)` walk."""
        if isinstance(position, SeqCursor) or self.horizon is None:
            return position
        return resume_point(position, self.horizon)


def addressed_since(conn, raw_since: str | None) -> SeqCursor:
    """The addressed mode's starting position (STATUS E13): a seq cursor read in the current
    numbering (`resolve_seq`: as it is while it still names its row); a plain cursor, from
    before E13, translated so no row after it is skipped (`seq_after_legacy`); a missing or
    malformed one, the current end of the commit order (the plain route's "newest row" rule;
    0, before any row, when nothing is stamped yet)."""
    seq = decode_seq_cursor(raw_since)
    if seq is not None:
        return resolve_seq(conn, seq)
    legacy = decode_cursor(raw_since)
    if legacy is not None:
        return seq_after_legacy(conn, legacy)
    return end_position(conn)


def legacy_since(conn, peer_id, raw_since: str | None) -> Cursor:
    """The `(created_at, id)` walk's starting position, exactly as before E13: the cursor as
    `effective_since` resolves it (a missing or malformed one: the newest mesh row the
    caller can see), or BEGINNING when it can see none."""
    since = decode_cursor(raw_since)
    if since is None:
        since = current_cursor(conn, peer_id, MESH_CHAT_NAMESPACE)
    return BEGINNING if since is None else since


def first_read_addressed(conn, peer_id, raw_since: str | None, reader, *,
                         seq: bool = False) -> AddressedRead:
    """The position, then the scan, in one transaction. `reader`: `scan_addressed`'s.

    ON THE COMMIT ORDER (`addressed_since`) when the caller asked for it (`seq`, the
    request's `seq=1`) or sent a seq cursor, which only a client that reads one has. ELSE
    ON THE `(created_at, id)` WALK a client from before E13 reads (review finding 1: its
    `cursor_order` refuses a seq cursor and it ends with `server_unsupported`), with the
    settle horizon read FIRST, in the same transaction, so it is no later than the scan."""
    with dbconn.request_server_internal(conn) as c:
        by_seq = seq or decode_seq_cursor(raw_since) is not None
        horizon = None if by_seq else settle_horizon(c)
        visible = len(visible_namespaces(c, peer_id))
        since = (addressed_since(c, raw_since) if by_seq
                 else legacy_since(c, peer_id, raw_since))
        labels = caller_labels(c, peer_id)
        frames, position, more = scan_addressed(c, peer_id, since, labels, reader)
    return AddressedRead(frames=frames, position=position, labels=labels, visible=visible,
                         more=more, reader=reader, horizon=horizon)


def park_addressed(peer_id, position: SeqCursor | Cursor, labels, eff_timeout: float, *,
                   reader, clock=time.monotonic) -> tuple[list, SeqCursor | Cursor, bool]:
    """`park` over the addressed scan: the same deadline rules, a connection of its own per
    re-read, and the position carried forward so a 204 can name it. Returns as soon as a
    re-read finds a frame or spends its budget (`more`). The addressed set is shared/mesh-
    chat's alone, which the nudge covers, so it waits for a nudge alone while the subscriber
    listens (STATUS F12). `reader`: `scan_addressed`'s, decided once for the request (a DPoP
    proof is spent when it is checked)."""
    at = {"position": position}

    def read():
        with dbconn.serving_connection() as conn:
            with dbconn.request_server_internal(conn) as c:
                frames, at["position"], more = scan_addressed(c, peer_id, at["position"],
                                                              labels, reader)
        return (frames, more) if frames or more else None

    found = _parked(peer_id, eff_timeout, read, nudged=True, labels=labels, clock=clock)
    if found is None:
        return [], at["position"], False
    return found[0], at["position"], found[1]


def cursor_wire(position: Cursor | SeqCursor) -> str:
    """A position in its wire form: a seq cursor (the addressed mode's, E13) or a plain one
    (the plain mode's, and the backlog summary's `through`)."""
    if isinstance(position, SeqCursor):
        return encode_seq_cursor(position.seq, position.anchor)
    return encode_cursor(position.created_at_micros, position.id)


def addressed_body(frames: list[dict], position: Cursor | SeqCursor) -> bytes:
    """`ok_body`'s shape with the push's frames as `messages` and the scan position as
    `cursor`. Compact, like it."""
    return json.dumps({"messages": frames, "cursor": cursor_wire(position)},
                      separators=(",", ":"), ensure_ascii=False).encode("utf-8")
