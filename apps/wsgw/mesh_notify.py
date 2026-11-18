"""Live delivery without Redis: Postgres LISTEN/NOTIFY into this process's sockets.

Divergence 020 records the delivery design. The trigger `shared_memory_mesh_notify`
(sql/036, the only trigger that notifies `mesh_msg`) runs `pg_notify('mesh_msg', ...)` for
every committed insert of a `msg/` key into `shared/mesh-chat`.

DELIVERY DESIGN:

  * No Redis. One LISTEN connection per serving process, and delivery straight into
    `ConnectionManager.publish_to_peer`. Each process delivers only to ITS OWN sockets,
    so N processes on one database each deliver once, never N times.
  * The payload is a POINTER, not data. NOTIFY is unauthenticated: any session on the
    database can send one on any channel. So the bridge reads the committed row back by
    (key, author) and builds the frame from the ROW, with the same expressions sql/036
    uses. A forged or stale notification can at worst re-nudge a row that exists; it
    cannot put text the row does not hold in front of a peer.
  * RECIPIENTS ARE FILTERED. A frame goes to a connected peer only when all four hold:
      1. it is not the author
      2. its `peers.status` is 'active'
      3. the row addresses it, or is a broadcast
      4. it may READ shared/mesh-chat, by the one predicate
         `memoryskills.permissions.check_permission`
    The filter is fail-closed: peers that hold no grant on the namespace receive no frame.

ADDRESSING, and why it is WIDER than the client's filter rather than equal to it. The
node clients decide on a frame by its `to` alone, lower-cased, with `both`/`all` or an
absent/empty `to` meaning everyone (reactor.py:34-74, mesh_ws_monitor.py:165-212). The
push must never be NARROWER than that, or a nudge a node would have acted on never
arrives. So `addressing_of` reads `to` AND `cc` through apps/mesh/normalize (the one
place the string/array/null/absent spellings are known), applies the client's own split
to the string shape (a comma list, or a JSON array held in a string), lower-cases for
matching, and calls it a broadcast exactly when the client would. A cc'd peer therefore
receives a frame that reactor may then ignore (it reads `to` only, mesh_ws_monitor.py:
125-129); that is extra latency spent, never a message lost.

THE ROLE (divergence 033). A frame is built per RECIPIENT and says in `role` why that
recipient got it: `to`, `cc` or `broadcast`, by `role_for`, the one decision both the
push and the addressed long-poll (apps/mesh/wait.py) call, so they cannot disagree. `to`
and `cc` travel as lists of the labels as written (`frame_labels`), never as the JSON
  text sql/036's `value ->> 'to'` renders an array as. `to` is the frame field and
goes to every recipient; `cc` goes only where the preview does (below), because nothing
else shows a reader who ELSE was copied (review F3 of 44640f2).

THE PREVIEW (divergence 033). A frame may carry the first PREVIEW_CHARS characters of the
body (`preview_unverified`, `preview_truncated`), and its `cc`, and only to a reader
memory.read without a capability would show the body to (`readable`; the push takes no token,
divergence 040): `body_withheld`, which is divergence
027's gate for the context anchor's body (search._anchor_body_withheld), asked about the
row's own key, run in the transaction that read the row, fail-closed (`preview_gate`).
The reader is a SOCKET (`socket_reader`): its peer, and the key its token is bound to,
which the upgrade proved it holds when DPoP is enforced. A peer's sockets can be bound to
different keys, so the push builds one frame per key and every other socket of the peer
gets the frame without a preview (`ConnectionManager.publish_to_peer(by_key=)`). The
field name says the text is NOT signature-checked: the signed read stays the authority.

The server's labels for a peer are the three the graph routes answer for (roster label,
`peers.name`, `peer-<id8>`). Mesh `to` and `from` labels are free-form client labels and may
not equal `peers.name`. A node addressed by an unknown label gets no push and learns of the
message on its next poll. The nudge is an optimisation and the signed read stays authoritative,
so the cost of a wrong label is latency, which buys not telling every active peer who wrote to
whom. Divergence 020 records the trade.

REVOCATION (STATUS B15, divergence 030). The same listener thread also LISTENs on
`meshy_peer_revoked` and, every `REVOCATION_SWEEP_SECS` and on every (re)LISTEN, re-reads
the standing of every connected peer; a socket that lost it is closed through
`ConnectionManager.drop_peer`/`drop_connections` with a typed code. apps/wsgw/revocation.py
holds the rules; this file only schedules them and hands the drops to the loop.

THREADING. psycopg's async connection refuses Windows' default Proactor loop, and a LISTEN
connection cannot also run queries. So one daemon thread per bridge owns two sync connections, LISTEN and
query, both role-bound through apps/core/dbconn, and hands work to the event loop that
owns the sockets with `run_coroutine_threadsafe`. No request connection is ever touched
(docs/V1-DESIGN-RULES.md E7).
"""
from __future__ import annotations

import asyncio
import collections
import dataclasses
import json
import logging
import threading
import time
import uuid
from typing import Any

from apps.core import dbconn, env, rls
from apps.memoryskills.errors import HandlerError
from apps.memoryskills.handlers import roster_label_of
from apps.memoryskills.permissions import Key, check_permission
from apps.memoryskills.validation import MESH_CHAT_NAMESPACE, MESH_MAX_RECIPIENTS
from apps.mesh import normalize

from . import envelope, revocation
from .connection_manager import MANAGER, ConnectionManager
from .constants import (
    EDIT_NOTIFY_CHANNEL,
    LISTEN_BACKOFF_INITIAL_SECS,
    LISTEN_BACKOFF_MAX_SECS,
    LISTEN_CLEAN_RESTART_FLOOR_SECS,
    MESH_NOTIFY_CHANNEL,
    PRESENCE_ROW_NOTIFY_CHANNEL,
    REDACTION_NOTIFY_CHANNEL,
    REVOCATION_NOTIFY_CHANNEL,
    REVOCATION_SWEEP_MIN_SECS,
    REVOCATION_SWEEP_SECS,
)

log = logging.getLogger(__name__)

#: The labels the node clients read as "everyone" (reactor.py:74, mesh_ws_monitor.py:208).
BROADCAST_LABELS = frozenset({"all", "both"})

#: How long a socket's connect waits for the FIRST LISTEN of a fresh bridge. A message
#: committed before LISTEN is live is never nudged (the client's poll catches it), so the
#: first connect waits for it rather than racing it. Bounded, and non-fatal on expiry.
READY_TIMEOUT_SECS = 5.0

#: How long the listener thread blocks in one `notifies()` call before it re-checks its
#: stop flag. Only bounds shutdown latency; delivery is immediate.
NOTIFY_POLL_SECS = 1.0

#: Bound on one hop from the listener thread into the event loop.
LOOP_CALL_TIMEOUT_SECS = 5.0


def revocation_sweep_secs() -> float:
    """`MESHY_WS_REVOCATION_SWEEP_SECS` (a Django setting or the environment variable of
    the same name), else `REVOCATION_SWEEP_SECS`. Read on every loop, so a change applies
    at the next sweep; never below `REVOCATION_SWEEP_MIN_SECS`."""
    raw = env.setting("MESHY_WS_REVOCATION_SWEEP_SECS", "MESHY_WS_REVOCATION_SWEEP_SECS")
    try:
        value = float(raw) if raw is not None else REVOCATION_SWEEP_SECS
    except (TypeError, ValueError):
        value = REVOCATION_SWEEP_SECS
    return max(REVOCATION_SWEEP_MIN_SECS, value)


# --------------------------------------------------------------------------- #
# Addressing: pure, no I/O
# --------------------------------------------------------------------------- #

#: A recipient's role on its frame (divergence 033): why THIS peer received the message.
ROLE_TO = "to"
ROLE_CC = "cc"
ROLE_BROADCAST = "broadcast"

#: The most labels a frame's `to` or `cc` carries, and the longest label it shows (033).
#: The v1 schema caps a new write at MESH_MAX_RECIPIENTS labels of 64 characters, so only a
#: legacy row reaches either bound. The ROLE is decided on every label regardless.
FRAME_MAX_LABELS = MESH_MAX_RECIPIENTS
FRAME_LABEL_CHARS = 256

#: How much of the body a frame may carry (divergence 033), in characters: code points,
#: as Python and Postgres's `left()` count them, so an astral character is never split
#: into half a surrogate pair.
PREVIEW_CHARS = 160


def _split_string(text: str) -> list[str]:
    """The node client's parse of ONE string-shaped recipient field, order and case kept.

    The string branch of `reactor.py:_to_labels` (:47-63), which is also
    `mesh_ws_monitor.py:to_labels` (:171-185), without its lower-casing (`addressing_of`
    lower-cases for matching; a frame shows what was written): a leading `[` is a JSON
    array held in a string, falling back to a quote-stripping comma split; anything else is
    a comma list.
    """
    s = text.strip()
    if not s:
        return []
    if s.startswith("["):
        try:
            arr = json.loads(s)
            return [x.strip() for x in arr if isinstance(x, str) and x.strip()]
        except Exception:
            inner = s.strip("[]")
            return [t for t in (t.strip().strip("\"'") for t in inner.split(",")) if t]
    return [p for p in (p.strip() for p in s.split(",")) if p]


def _written(value: dict, field: str) -> tuple[str, ...] | None:
    """`to` or `cc` as the writer wrote it: labels in order, case kept, an exact repeat
    dropped. None when the row does not carry the field (absent, or JSON null). The SHAPE
    comes from apps/mesh/normalize; only its string shape is split further, because only
    there does the client split."""
    labels, shape, _skipped = normalize.recipients(value, field)
    if shape in (f"{field}:absent", f"{field}:null"):
        return None
    out: list[str] = []
    seen: set[str] = set()
    for label in labels:
        for one in (_split_string(label) if shape == f"{field}:string" else (label,)):
            if one not in seen:
                seen.add(one)
                out.append(one)
    return tuple(out)


@dataclasses.dataclass(frozen=True)
class Addressing:
    """Who one row is for. Built by `addressing_of`, read by `role_for` and by the frame."""

    #: `to` and `cc` as written (`_written`), or None when the row does not carry the field.
    to: tuple[str, ...] | None
    cc: tuple[str, ...] | None
    #: The same labels lower-cased, for matching `peer_labels` (the node clients lower-case
    #: both sides, reactor.py:44-74).
    to_keys: frozenset[str]
    cc_keys: frozenset[str]
    #: Addressed to everyone: `to` yields no label (absent, null, empty, only non-strings, a
    #: value that is not an object), or `all`/`both` is in `to` or `cc`. Exactly when the
    #: node client's own filter reads the row as for everyone (reactor.py:66-74).
    broadcast: bool


def addressing_of(value: Any) -> Addressing:
    """`Addressing` of one row's value. A value that is not an object has no `to` (its
    `value ->> 'to'` is SQL NULL, sql/036), so it is a broadcast with no lists."""
    if not isinstance(value, dict):
        return Addressing(None, None, frozenset(), frozenset(), True)
    to, cc = _written(value, "to"), _written(value, "cc")
    to_keys = frozenset(x.lower() for x in to or ())
    cc_keys = frozenset(x.lower() for x in cc or ())
    broadcast = not to_keys or bool((to_keys | cc_keys) & BROADCAST_LABELS)
    return Addressing(to, cc, to_keys, cc_keys, broadcast)


def role_for(addressing: Addressing, labels: frozenset[str]) -> str | None:
    """THE addressing decision, and the only one (divergence 033): this peer's role on the
    row's frame, or None when the row is not for it.

    Named in `to`, then named in `cc`, then a broadcast. A peer named in both is `to`; a
    peer named in `cc` of a broadcast is `cc`, because being named says more than being
    everyone. The push (`resolve_recipients`) and the addressed wait (apps/mesh/wait.py
    `scan_addressed`, divergence 031) both decide by it, so the set each answers and the
    role each states cannot disagree. `labels` are the peer's own (`peer_labels`).

    Not None exactly when the pre-033 rule delivered (the union of `to` and `cc` against
    the peer's labels, or a broadcast), so the role never changed who receives a frame;
    tests/test_mesh_live.py holds that rule verbatim and compares the two."""
    if addressing.to_keys & labels:
        return ROLE_TO
    if addressing.cc_keys & labels:
        return ROLE_CC
    if addressing.broadcast:
        return ROLE_BROADCAST
    return None


def recipient_labels(value: Any) -> frozenset[str] | None:
    """Every label a row names, lower-cased, or None when `to` names nobody (a broadcast by
    absence). A VIEW of `addressing_of`, not a decision: `role_for` decides. Kept because
    tests/test_mesh_live.py pins the parse of every spelling through it."""
    a = addressing_of(value)
    return None if not a.to_keys else a.to_keys | a.cc_keys


def frame_labels(labels: tuple[str, ...]) -> tuple[list[str], bool]:
    """A frame's copy of `to` or `cc` (divergence 033): the first FRAME_MAX_LABELS labels no
    longer than FRAME_LABEL_CHARS, and whether any label was left off. An over-long label
    is left off rather than cut, because a cut label is one nobody wrote."""
    shown = [x for x in labels if len(x) <= FRAME_LABEL_CHARS][:FRAME_MAX_LABELS]
    return shown, len(shown) < len(labels)


def _candidate_rows(conn, candidates: list) -> dict:
    """`{peer: (name, status)}` for the connected peers a push may go to, read once for all three
    resolvers (message, redaction, edit). A CONTESTED `peers.name` comes back as None (divergence
    066, `apps/peers/names.py uncontested`): the push does not answer two peers for one name."""
    from apps.peers import names as peer_names
    with conn.cursor() as cur:
        cur.execute("SELECT id, name, status FROM peers WHERE id = ANY(%s)", (candidates,))
        rows = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    kept = peer_names.uncontested(conn, {peer: found[0] for peer, found in rows.items()})
    return {peer: (kept[peer], found[1]) for peer, found in rows.items()}


def peer_labels(peer_id: uuid.UUID, name: str | None) -> frozenset[str]:
    """What the server knows this peer is called: roster label, `peers.name`, and the
    `peer-<id8>` fallback the write path stamps (handlers.py:614). `name` is passed in by the
    caller, which gives None for a CONTESTED name (divergence 066: `_candidate_rows` here,
    `apps/mesh/wait.py caller_labels`).

    The same three sources the mesh graph routes answer a caller for
    (`apps/mesh/views.py:labels_for`, divergence 018 (b)), where `peers.name` was added
    after measuring seats addressed by it and not by the roster. Lower-cased, unlike
    there, because the node clients lower-case both sides (reactor.py:44-74) and the push
    must not be narrower than their filter. Both now live on one branch and are still two
    functions; merging them is owed (divergence 020)."""
    out = {f"peer-{str(peer_id)[:8]}"}
    roster = roster_label_of(peer_id)
    if roster and roster.strip():
        out.add(roster.strip().lower())
    if isinstance(name, str) and name.strip():
        out.add(name.strip().lower())
    return frozenset(out)


# --------------------------------------------------------------------------- #
# Reading a nudge back from the database
# --------------------------------------------------------------------------- #

@dataclasses.dataclass(frozen=True)
class Nudge:
    """One committed mesh row, reduced to what the frame and the filter need."""

    key: str
    from_peer_id: uuid.UUID
    #: sql/036's `to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')`.
    created_at: str
    #: `addressing_of` the row's value: who it is for, and its `to`/`cc` as written.
    addressing: Addressing
    #: The first PREVIEW_CHARS + 1 characters of the body when it is a JSON string (one
    #: more than a preview shows, so a cut can be told from a body of exactly that length),
    #: else None. Only ever put on a frame for a reader `body_withheld` passes.
    body_head: str | None = None
    #: (version, edited_by, edited_at) when an EDIT wrote this row (`shared_memory_edit` holds a record
    #: for it, divergence 059), else None. `edited_by` is the row's `author_label` (the editor's),
    #: `edited_at` the record's time as `Z` text. Read from the record, never from the row's value.
    edit: tuple[int, str, str] | None = None
    #: The sender's alias (divergence 063): the row's `author_label` when it is the label the fleet ROSTER gives this peer (`_roster_alias`), else None.
    #: None on a row an EDIT wrote, where that label is the editor's (`edit` carries it).
    sender: str | None = None
    #: Whether the message asks its recipients for an answer (#199), as the mesh graph reads it
    #: (`normalize.requires_ack`): True or False for a row that is an object, None otherwise.
    requires_ack: bool | None = None
    #: Whether the row holds a seat directive (`value ? 'directive'`; memory.write stores one only after
    #: validate_directive). The frame shows it on a `to` frame only (envelope.mesh_message).
    directive: bool = False
    #: The urgent lane (kxxx): `value -> 'urgent' = true` and `value ? 'urgent_declined'`. memory.write stores
    #: either only after validate_urgent and the hourly allowance. Shown on a `to` frame only.
    urgent: bool = False
    urgent_declined: bool = False

    @property
    def gated(self) -> bool:
        """The row holds something a frame shows only to a `readable` reader: a string
        body to preview, or a `cc` (review F3 of 44640f2). Only then is the gate asked."""
        return self.body_head is not None or self.addressing.cc is not None

    def role(self, labels: frozenset[str]) -> str | None:
        """`role_for` this row and a peer's labels."""
        return role_for(self.addressing, labels)

    def frame(self, role: str, *, readable: bool = False) -> dict:
        """The frame for ONE recipient, whose role the caller decided (`role`). `to` is a
        list (`frame_labels`), omitted when the row has no such field.
        `readable`: the caller has found memory.read without a capability would show THIS
        recipient the row
        (`preview_gate`). Only then does the frame carry the preview (a string body's
        head) and `cc` (a list, omitted when the row has none): nothing else on the server
        shows a reader who ELSE was cc'd (review F3 of 44640f2), and memory.read, which
        returns the whole value, is the gate that does."""
        to = cc = None
        to_cut = cc_cut = False
        if self.addressing.to is not None:
            to, to_cut = frame_labels(self.addressing.to)
        if readable and self.addressing.cc is not None:
            cc, cc_cut = frame_labels(self.addressing.cc)
        shown = preview_of(self.body_head) if readable else None
        return envelope.mesh_message(self.key, self.from_peer_id, to, self.created_at,
                                     role=role, to_truncated=to_cut, cc=cc,
                                     cc_truncated=cc_cut, preview=shown, edit=self.edit,
                                     sender=self.sender, requires_ack=self.requires_ack,
                                     directive=self.directive, urgent=self.urgent,
                                     urgent_declined=self.urgent_declined)


def preview_of(head: str | None) -> tuple[str, bool] | None:
    """(the first PREVIEW_CHARS characters, whether that cut anything) of a body head, or
    None when there is no string body. Characters are code points: a Python str holds an
    astral character as ONE, so the cut never leaves half a surrogate pair."""
    if head is None:
        return None
    return head[:PREVIEW_CHARS], len(head) > PREVIEW_CHARS


def parse_mesh_payload(raw: str) -> tuple[str, uuid.UUID] | None:
    """(key, author) from sql/036's payload, or None when it is not one.

    Only `key` and `from_peer_id` are read. They locate the row; everything the frame
    carries is then read from the row itself (see the module docstring). A malformed
    payload is skipped, not fatal."""
    try:
        obj = json.loads(raw)
        key = obj["key"]
        author = uuid.UUID(str(obj["from_peer_id"]))
    except (ValueError, TypeError, KeyError):
        return None
    if not isinstance(key, str) or not key.startswith("msg/"):
        return None
    return key, author


#: How many columns `nudge_columns` selects, so a query that adds its own after them can slice them off.
NUDGE_COLUMNS = 14


def nudge_columns(alias: str = "") -> str:
    """The twelve columns a nudge is built from: sql/036's own expression for the frame's
    `created_at`, the row's `to` and `cc` as jsonb with whether each key is present, the
    head of a string body (PREVIEW_CHARS + 1 characters: Postgres's `left()` counts
    characters, not bytes, and never reads the rest of a long body into the frame's
    reader), and the three an EDIT adds (divergence 059): the row's `version` and
    `author_label` and, when `shared_memory_edit` holds a record for the row, the record's
    time as `Z` text (NULL otherwise, and then the other two are not used), and the row's
    `requires_ack` as jsonb (#199: the frame says whether the message asks for an answer), and whether
    the value holds a `directive` (the postbox priority lane, 10 Nov 2026). `alias`
    qualifies them (`"sm."`) for a query that joins.

    ONE definition for two readers: `load_nudge` below, and the addressed long-poll
    (apps/mesh/wait.py, divergence 031), so the frame a catch-up answers is the frame the
    push sends, built by `nudge_from_columns` from the same columns.

    `to` is read as jsonb, not as sql/036's `left(value ->> 'to', 256)`: `->>` renders a
    jsonb ARRAY as its JSON text, which is how a frame's `to` reached a node as the string
    "[\"desktop-5e6e5b2b\"]" (round 1, 2026-09-23). The frame's lists are built from the
    jsonb by `addressing_of` instead (divergence 033)."""
    a = alias
    # The record's subquery names the row's id QUALIFIED: inside it a bare `id` is the record's own column.
    row_id = f"{a}id" if a else "shared_memory.id"
    return (f"to_char({a}created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'), "
            f"jsonb_typeof({a}value) = 'object', "
            f"{a}value ? 'to', {a}value -> 'to', {a}value ? 'cc', {a}value -> 'cc', "
            f"CASE WHEN jsonb_typeof({a}value -> 'body') = 'string' "
            f"THEN left({a}value ->> 'body', {PREVIEW_CHARS + 1}) END, "
            f"{a}version, {a}author_label, "
            f"(SELECT to_char(e.edited_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') "
            f"   FROM shared_memory_edit e WHERE e.memory_id = {row_id}), "
            f"{a}value -> 'requires_ack', "
            f"({a}value ? 'directive'), "
            f"(({a}value -> 'urgent') = 'true'::jsonb), "
            f"({a}value ? 'urgent_declined')")


def _roster_alias(author: uuid.UUID, author_label: str | None) -> str | None:
    """The alias a frame may show: the row's `author_label` ONLY when it is the label the fleet roster (the operator's own list) gives this very peer.
    Divergence 028 holds a mail message's label to one of the caller's own labels, and that set includes `peers.name`, which is free text and not unique:
    a peer of another owner can be NAMED like a seat and pass. So the roster decides. A peer with no roster label, or whose stored label is not its roster
    label, gets no alias, and the notice shows its id (review of #42, F1)."""
    return author_label if roster_label_of(author) == author_label else None


def nudge_from_columns(key: str, author: uuid.UUID, cols) -> Nudge:
    """A Nudge from the twelve `nudge_columns` of one row. Only `to` and `cc` are rebuilt
    into a value for `addressing_of`; of the body only its head is held; the edit's three
    are held only when the record's time is there. `requires_ack` is the mesh graph's own
    reading of the field (apps/mesh/normalize.requires_ack: the bool, or the legacy strings
    'true' / 'True'), so the frame and the owed edge never disagree; None for a row that is
    not an object."""
    (created_at, is_object, has_to, to, has_cc, cc, body_head, version, author_label, edited_at,
     requires_ack_raw, has_directive, is_urgent, has_declined) = cols
    value: Any = None
    if is_object:
        value = {}
        if has_to:
            value["to"] = to
        if has_cc:
            value["cc"] = cc
    edit = (int(version), author_label, edited_at) if edited_at is not None else None
    asks = None
    if is_object:
        from apps.mesh.normalize import requires_ack
        asks = requires_ack({"requires_ack": requires_ack_raw})
    return Nudge(key=key, from_peer_id=author, created_at=created_at,
                 addressing=addressing_of(value), body_head=body_head, edit=edit,
                 sender=_roster_alias(author, author_label) if edit is None else None,
                 requires_ack=asks, directive=bool(is_object and has_directive is True),
                 urgent=bool(is_object and is_urgent is True), urgent_declined=bool(is_object and has_declined is True))


def load_nudge(conn, key: str, author: uuid.UUID) -> Nudge | None:
    """The newest version of `key` written by `author`, or None.

    Must run inside `rls.server_internal`: shared_memory is FORCE-RLS and admits only
    that context (apps/core/rls.py). Uses `shared_memory_nkv_unique (namespace, key,
    version)`. Selecting by author as well as key means a notification can only ever
    name a row its own author wrote.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT " + nudge_columns() + " "
            "FROM shared_memory "
            "WHERE namespace = %s AND key = %s AND created_by_peer_id = %s "
            "ORDER BY version DESC LIMIT 1",
            (MESH_CHAT_NAMESPACE, key, author))
        row = cur.fetchone()
    if row is None:
        return None
    return nudge_from_columns(key, author, row)


def _candidates(conn, connected: list[uuid.UUID], *, exclude: uuid.UUID, reason: str
                ) -> tuple[collections.Counter, list[tuple[uuid.UUID, tuple | None]]]:
    """The first step of each of the three resolvers (message, redaction, edit), which was
    written out in each: the ONE connected peer a push never goes to is left out and counted
    under `reason` (`author`, `redactor`, `editor`), and the others' rows are read at once
    (`_candidate_rows`). Returns (the counts so far, [(peer, (name, status) or None)] in
    `connected`'s order); the list is empty, and nothing was read, when every connected peer
    was the excluded one. Each resolver's result is unchanged: the `inactive` check and its
    count stay in the resolver's own loop, so every count lands in the order it did."""
    skipped: collections.Counter = collections.Counter()
    candidates = []
    for peer in connected:
        if peer == exclude:
            skipped[reason] += 1
        else:
            candidates.append(peer)
    if not candidates:
        return skipped, []
    rows = _candidate_rows(conn, candidates)
    return skipped, [(peer, rows.get(peer)) for peer in candidates]


def resolve_recipients(conn, nudge: Nudge, connected: list[uuid.UUID]
                       ) -> tuple[tuple[tuple[uuid.UUID, str], ...], dict]:
    """((peer, role) for each peer to nudge, {reason: count} for every connected peer that
    was not). The role is `role_for`'s, the decision that admitted the peer (033).

    Must run inside `rls.server_internal`, which `check_permission` requires. The four
    conditions are the module docstring's, checked in that order, and each refusal is
    COUNTED by reason so a missing frame can be explained from the bridge's stats.
    """
    skipped, candidates = _candidates(conn, connected, exclude=nudge.from_peer_id, reason="author")
    if not candidates:
        return (), dict(skipped)

    out: list[tuple[uuid.UUID, str]] = []
    for peer, found in candidates:
        if found is None or found[1] != "active":
            skipped["inactive"] += 1
            continue
        role = nudge.role(peer_labels(peer, found[0]))
        if role is None:
            skipped["not_addressed"] += 1
            continue
        try:
            check_permission(MESH_CHAT_NAMESPACE, peer, "read", conn,
                             resource=Key(nudge.key))
        except HandlerError as exc:
            skipped[f"no_read:{exc.code}"] += 1
            continue
        out.append((peer, role))
    return tuple(out), dict(skipped)


# --------------------------------------------------------------------------- #
# A redaction, announced (divergence 053; docs/design/redaction.md)
# --------------------------------------------------------------------------- #

@dataclasses.dataclass(frozen=True)
class Redaction:
    """One redaction record, read back with the row it redacted, for the push."""

    #: The tombstone's routing (`to`, `cc`, and so a broadcast) as a Nudge: a redaction keeps
    #: exactly the fields that say who a row was for, and drops the body, so `role` decides who
    #: held the row by the rule a message frame uses. `body_head` is always None.
    nudge: Nudge
    #: The `mesh_redaction` frame: key, version, when, and one of the closed reason codes.
    event: dict
    #: The authors of the row's direct replies, named on the record when it was written (the
    #: operator's ruling, ASDLC D-2026-09-29-U13). Told though the row was not for them.
    notify: frozenset
    #: The seat that redacted. It knows.
    redactor: uuid.UUID


def _pointer_uuid(raw: str, field: str) -> uuid.UUID | None:
    """The uuid under `field` in a trigger's JSON payload, or None when the payload is not JSON,
    not an object, lacks the field or holds something that is not a uuid. The one parse behind
    `parse_redaction_payload`, `parse_edit_payload` and `parse_presence_payload`, which read a
    pointer and nothing else (the module docstring: the payload is unauthenticated); each
    answers as it did."""
    try:
        return uuid.UUID(str(json.loads(raw)[field]))
    except (ValueError, TypeError, KeyError):
        return None


def parse_redaction_payload(raw: str) -> uuid.UUID | None:
    """The record id from the trigger's payload, or None when it is not one. Only the id is
    read, as `parse_mesh_payload` reads only a key: everything the frame carries is read back
    from the record and the row, so a forged notification can at worst re-announce a redaction
    that exists."""
    return _pointer_uuid(raw, "id")


def _load_record(conn, record_id: uuid.UUID, *, columns: str, table: str, alias: str
                 ) -> tuple[str, uuid.UUID, list, list] | None:
    """One record (a redaction's or an edit's) read back WITH the mail row it is about, by the
    record's id: (the row's key, its author, the row's `nudge_columns`, the record's own
    `columns`), or None when there is no such record or its row is not a mail row. The one
    statement `load_redaction` and `load_edit` ran separately; each builds the same object from
    the same columns as before. `columns` are the record's, qualified by `alias`, the alias of
    `table` in the join. Must run inside `rls.server_internal`, like `load_nudge`."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT sm.key, sm.created_by_peer_id, " + nudge_columns("sm.") + ", " + columns + " "
            "FROM " + table + " " + alias + " JOIN shared_memory sm ON sm.id = " + alias + ".memory_id "
            "WHERE " + alias + ".id = %s AND " + alias + ".namespace = %s AND sm.key LIKE 'msg/%%'",
            (record_id, MESH_CHAT_NAMESPACE))
        row = cur.fetchone()
    if row is None:
        return None
    key, author, *rest = row
    return key, author, rest[:NUDGE_COLUMNS], rest[NUDGE_COLUMNS:]


def load_redaction(conn, record_id: uuid.UUID) -> Redaction | None:
    """The record and its row, or None when there is none (or it is not a mail row). Must run
    inside `rls.server_internal`, like `load_nudge`."""
    found = _load_record(
        conn, record_id,
        columns="  r.version, to_char(r.redacted_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'), "
                "  r.reason_code, r.notify_peer_ids, r.redacted_by_peer_id",
        table="shared_memory_redaction", alias="r")
    if found is None:
        return None
    key, author, cols, (version, redacted_at, reason_code, notify, redactor) = found
    return Redaction(nudge=nudge_from_columns(key, author, cols),
                     event=envelope.mesh_redaction(key, version, redacted_at, reason_code),
                     notify=frozenset(notify or ()), redactor=redactor)


def resolve_redaction_recipients(conn, red: Redaction, connected: list[uuid.UUID]
                                 ) -> tuple[tuple[uuid.UUID, ...], dict]:
    """(the connected peers to tell, {reason: count} for every one that was not). The same
    conditions as `resolve_recipients`, in the same order, with the two that differ: the author
    is NOT excluded (a redaction by another seat of the author's owner is news to the author
    seat too, if the row was for it) but the seat that redacted IS, and a reply author on the
    record is told though the row did not address it. `apps/mesh/wait.py redaction_event` decides
    the same way for the addressed wait, so the two paths cannot disagree.

    Must run inside `rls.server_internal`, which `check_permission` requires."""
    skipped, candidates = _candidates(conn, connected, exclude=red.redactor, reason="redactor")
    if not candidates:
        return (), dict(skipped)
    out: list[uuid.UUID] = []
    for peer, found in candidates:
        if found is None or found[1] != "active":
            skipped["inactive"] += 1
            continue
        if red.nudge.role(peer_labels(peer, found[0])) is None and peer not in red.notify:
            skipped["not_addressed"] += 1
            continue
        try:
            check_permission(MESH_CHAT_NAMESPACE, peer, "read", conn, resource=Key(red.nudge.key))
        except HandlerError as exc:
            skipped[f"no_read:{exc.code}"] += 1
            continue
        out.append(peer)
    return tuple(out), dict(skipped)


# --------------------------------------------------------------------------- #
# An edit, announced (divergence 059; docs/design/edit.md section 4)
# --------------------------------------------------------------------------- #

#: The role a seat is told an edit in when it was not among the row's recipients: it wrote a direct reply.
ROLE_REPLIED = "replied"


@dataclasses.dataclass(frozen=True)
class Edit:
    """One edit record, read back with the row it wrote, for the push."""

    #: The new head's routing (`to`, `cc`, and so a broadcast) as a Nudge. An edit cannot change any
    #: of it (docs/design/edit.md check 5), so it is the routing of the row that was edited: `role`
    #: decides who held the row by the rule a message frame uses. `body_head` is never used.
    nudge: Nudge
    version: int
    #: The record's time as `Z` text, second precision, as a message frame's `created_at` is.
    edited_at: str
    #: The editor's label: the new row's `author_label`.
    edited_by: str
    #: The authors of the row's direct replies, named on the record when it was written.
    notify: frozenset
    #: The seat that edited. It knows.
    editor: uuid.UUID

    def event(self, role: str) -> dict:
        """The `mesh_edit` frame for ONE recipient, whose role the caller decided."""
        return envelope.mesh_edit(self.nudge.key, self.version, self.edited_at, self.edited_by, role)


def parse_edit_payload(raw: str) -> uuid.UUID | None:
    """The record id from the trigger's payload, or None when it is not one. Only the id is read, as
    `parse_redaction_payload` reads only an id: everything the frame carries is read back from the record
    and the row, so a forged notification can at worst re-announce an edit that exists."""
    return parse_redaction_payload(raw)


def load_edit(conn, record_id: uuid.UUID) -> Edit | None:
    """The record and its row, or None when there is none (or it is not a mail row). Must run inside
    `rls.server_internal`, like `load_nudge`."""
    found = _load_record(
        conn, record_id,
        columns="  e.version, to_char(e.edited_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'), "
                "  sm.author_label, e.notify_peer_ids, e.edited_by_peer_id",
        table="shared_memory_edit", alias="e")
    if found is None:
        return None
    key, author, cols, (version, edited_at, edited_by, notify, editor) = found
    return Edit(nudge=nudge_from_columns(key, author, cols), version=int(version), edited_at=edited_at,
                edited_by=edited_by, notify=frozenset(notify or ()), editor=editor)


def resolve_edit_recipients(conn, ed: Edit, connected: list[uuid.UUID]
                            ) -> tuple[tuple[tuple[uuid.UUID, str], ...], dict]:
    """((peer, role) for each connected peer to tell, {reason: count} for every one that was not). The same
    conditions as `resolve_recipients`, in the same order, with the two that differ: the seat that EDITED is
    excluded (it knows), not the row's author (another seat of the author's owner may be the editor, and the
    author seat is a recipient like any other when the row was for it); and a reply author on the record is
    told though the row did not address it, in the role `replied`. `apps/mesh/wait.py edit_event` decides the
    same way for the addressed wait, so the two paths cannot disagree.

    Must run inside `rls.server_internal`, which `check_permission` requires."""
    skipped, candidates = _candidates(conn, connected, exclude=ed.editor, reason="editor")
    if not candidates:
        return (), dict(skipped)
    out: list[tuple[uuid.UUID, str]] = []
    for peer, found in candidates:
        if found is None or found[1] != "active":
            skipped["inactive"] += 1
            continue
        role = ed.nudge.role(peer_labels(peer, found[0]))
        if role is None:
            if peer not in ed.notify:
                skipped["not_addressed"] += 1
                continue
            role = ROLE_REPLIED
        try:
            check_permission(MESH_CHAT_NAMESPACE, peer, "read", conn, resource=Key(ed.nudge.key))
        except HandlerError as exc:
            skipped[f"no_read:{exc.code}"] += 1
            continue
        out.append((peer, role))
    return tuple(out), dict(skipped)


def body_withheld(conn, reader, key: str) -> str | None:
    """None when memory.read without a capability would show `reader` the body of the
    shared/mesh-chat message at `key`, else the first of its gates that refuses. Since
    divergence 040 slice 6 memory.read also takes a token; this gate takes none (design 3.4,
    site 11, tokenless by decision), so a reader whose only authority is a token is withheld
    the preview, the fail-closed direction (T21). Divergence 027's ONE function for
    that question (apps/meshsem/search.py `body_withheld`, the context anchor's gate), not
    a re-expression: DPoP possession, the memory.read skill resolving, a usable signing
    key, the tenancy or skill-grant authorization, the namespace grant, asked about `key`
    (divergence 040, slice 2: the row's own key, never the namespace as a whole). Run it in
    the transaction that read the row (`rls.server_internal`), as the anchor's is."""
    from apps.meshsem import search
    return search.body_withheld(conn, reader, key)


def body_withheld_many(conn, reader, keys) -> dict[str, str | None]:
    """`body_withheld` for each of `keys`, by key (memory.read without a capability, as
    `body_withheld`): the same ONE function's answers
    (apps/meshsem/search.py `body_withheld_many`), with the half that depends on the reader
    alone asked once (divergence 040, slice 2 review F2). For the addressed wait, which asks
    about a page of rows for one reader."""
    from apps.meshsem import search
    return search.body_withheld_many(conn, reader, keys)


#: Why a preview was withheld when 027's gate itself FAILED (raised) rather than refused.
WITHHELD_ERROR = "error"


def _gate_failed(reader, exc: Exception) -> None:
    log.warning("preview gate failed for %s: %s: %s",
                getattr(reader, "peer_id", None), type(exc).__name__, exc)


def preview_gate(conn, reader, key: str) -> str | None:
    """`body_withheld` for the row at `key` (memory.read without a capability, divergence
    040 slice 6), FAIL-CLOSED: the push's way to ask it, and
    `preview_gates` the addressed wait's, so they fail alike (review F2 of 44640f2). A gate
    that raises, for any reason, withholds the preview (WITHHELD_ERROR) and is logged; it
    never shows the preview and never fails the frame or the answer. It runs in a
    SAVEPOINT, so a database error inside it rolls back to it and the transaction that read
    the row, and every later gate asked in that transaction, carry on."""
    try:
        with conn.transaction():
            return body_withheld(conn, reader, key)
    except Exception as exc:
        _gate_failed(reader, exc)
        return WITHHELD_ERROR


def preview_gates(conn, reader, keys) -> dict[str, str | None]:
    """`preview_gate` for the rows at `keys` at once (the addressed wait's page), by key,
    each answered as memory.read without a capability would answer it:
    `body_withheld_many` in one SAVEPOINT, FAIL-CLOSED as `preview_gate` is. A gate that
    raises withholds every preview it was asked about (WITHHELD_ERROR each) and is logged
    once; the rows' frames and the answer are never failed."""
    keys = tuple(dict.fromkeys(keys))
    try:
        with conn.transaction():
            return body_withheld_many(conn, reader, keys)
    except Exception as exc:
        _gate_failed(reader, exc)
        return dict.fromkeys(keys, WITHHELD_ERROR)


def socket_reader(peer_id: uuid.UUID, bound_key: bytes | None):
    """The reader one socket is, for `body_withheld`. With DPoP enforced the upgrade
    checked a proof by the key the socket's token is bound to (apps/wsgw/consumer.py), so
    that key is PROVEN and is the one memory.read must verify under; a socket bound to no
    key proved nothing. With DPoP off, memory.read's POST needs no proof and names no key,
    so neither does the socket (027's rule for a GET)."""
    from apps.meshsem.search import Reader
    from apps.peers.auth import dpop_enforced

    if dpop_enforced():
        return Reader(peer_id=peer_id, possession=bound_key is not None, key=bound_key)
    return Reader(peer_id=peer_id, possession=True, key=None)


def push_frame(conn, nudge: Nudge, peer_id: uuid.UUID, role: str,
               bound_key: bytes | None) -> tuple[dict, str | None]:
    """The frame the push sends ONE socket of `peer_id`, whose token is bound to
    `bound_key` (None for a bearer token), and why its preview and `cc` were withheld:
    None when the gate passed, or when the row has neither a string body nor a `cc`
    (`Nudge.gated`: no gate is asked). The gate is `preview_gate`: one that fails
    withholds (WITHHELD_ERROR)."""
    if not nudge.gated:
        return nudge.frame(role), None
    withheld = preview_gate(conn, socket_reader(peer_id, bound_key), nudge.key)
    return nudge.frame(role, readable=withheld is None), withheld


def parse_presence_payload(raw: str) -> uuid.UUID | None:
    return _pointer_uuid(raw, "peer_id")


def load_presence_frame(conn, peer_id: uuid.UUID) -> dict | None:
    """The `peer_presence` frame for the peer's CURRENT row, or None.

    Built from the row rather than the notification for the reason the mesh path is:
    the payload is a pointer. `live` is true because the frame is sent after a beat lands.
    `mesh_presence` carries no RLS (apps/peers/db.py), so no context is entered."""
    with conn.cursor() as cur:
        cur.execute("SELECT state, blocked_on, note FROM mesh_presence WHERE peer_id = %s",
                    (peer_id,))
        row = cur.fetchone()
    if row is None:
        return None
    state, blocked_on, note = row
    return envelope.peer_presence(peer_id, state, blocked_on, note, live=True)


# --------------------------------------------------------------------------- #
# The bridge
# --------------------------------------------------------------------------- #

class _QueryConn:
    """The listener thread's second connection: opened lazily, dropped on any error."""

    def __init__(self, target) -> None:
        self._target = target
        self._conn = None

    def get(self):
        if self._conn is None or self._conn.closed:
            conn = dbconn.connect(self._target())
            try:
                # The same guard a request connection crosses: autocommit, and RLS proven
                # in force by a probe, not assumed from the SET ROLE that preceded it.
                dbconn.assert_serving_connection(conn)
            except Exception:
                conn.close()
                raise
            self._conn = conn
        return self._conn

    def drop(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None


class LiveBridge:
    """One LISTEN loop feeding one ConnectionManager.

    Started by the first socket that connects (`ensure_started`), bound to the event loop
    that socket runs on, because that loop owns every SendQueue the bridge writes to.
    There is no lifespan hook on the ASGI router, and a bridge with no socket has nobody to tell.
    """

    def __init__(self, manager: ConnectionManager, *, dsn=None) -> None:
        self.manager = manager
        self._dsn = dsn
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop = threading.Event()
        self._listening = threading.Event()
        self._stats: collections.Counter = collections.Counter()
        self._stats_lock = threading.Lock()
        #: Peers a sweep or a notification last SAW holding read on shared/mesh-chat. With
        #: the peers whose socket HELD read when it connected (`ConnectionManager.
        #: mesh_readers`), only these can LOSE it (apps/wsgw/revocation.py). Owned by the
        #: listener thread.
        self._readers: set[uuid.UUID] = set()

    # --- lifecycle ------------------------------------------------------------

    def _target(self) -> str:
        return self._dsn or dbconn.dsn()

    async def ensure_started(self, *, ready_timeout: float = READY_TIMEOUT_SECS) -> bool:
        """Start the listener for the running loop if it is not already. True once LISTEN
        is live. Never raises for a database problem: the socket is still useful without
        a push, and the client's poll is authoritative."""
        loop = asyncio.get_running_loop()
        stale = None
        with self._lock:
            alive = self._thread is not None and self._thread.is_alive()
            if not (alive and self._loop is loop):
                if alive:
                    stale = self._stop
                self._loop = loop
                self._stop = threading.Event()
                self._listening = threading.Event()
                self._thread = threading.Thread(
                    target=self._run, args=(loop, self._stop, self._listening),
                    name="meshy-live-bridge", daemon=True)
                self._thread.start()
            listening = self._listening
        if stale is not None:
            # A bridge from a loop that is gone. It exits at its next poll; joining it
            # here would block the loop this socket is being served on.
            stale.set()
        if listening.is_set():
            return True
        ready = await asyncio.to_thread(listening.wait, ready_timeout)
        if not ready:
            log.warning("live bridge: LISTEN not established within %.1fs; this socket "
                        "gets no push until it is (the client poll still delivers)",
                        ready_timeout)
        return ready

    async def wait_listening(self, timeout: float = READY_TIMEOUT_SECS) -> bool:
        return await asyncio.to_thread(self._listening.wait, timeout)

    def is_listening(self) -> bool:
        return self._listening.is_set()

    def stop(self, timeout: float = 5.0) -> None:
        with self._lock:
            thread, stop = self._thread, self._stop
            self._thread = None
            self._loop = None
        stop.set()
        if thread is not None:
            thread.join(timeout)

    def stats(self) -> dict:
        with self._stats_lock:
            return dict(self._stats)

    def _count(self, name: str, n: int = 1) -> None:
        with self._stats_lock:
            self._stats[name] += n

    # --- the listener thread --------------------------------------------------

    def _run(self, loop, stop: threading.Event, listening: threading.Event) -> None:
        """Reconnect forever with capped exponential backoff."""
        delay = LISTEN_BACKOFF_INITIAL_SECS
        while not stop.is_set() and not loop.is_closed():
            try:
                self._run_once(loop, stop, listening)
                delay = LISTEN_BACKOFF_INITIAL_SECS
                stop.wait(LISTEN_CLEAN_RESTART_FLOOR_SECS)
            except Exception as exc:
                listening.clear()
                self._count("listen_errors")
                log.warning("live bridge: %s: %s; retrying in %.1fs",
                            type(exc).__name__, exc, delay)
                stop.wait(delay)
                delay = min(delay * 2, LISTEN_BACKOFF_MAX_SECS)
        listening.clear()

    def _run_once(self, loop, stop: threading.Event, listening: threading.Event) -> None:
        listen = dbconn.connect(self._target(), pool=False)     # a LISTEN connection is this thread's own, never pooled
        query = _QueryConn(self._target)
        try:
            with listen.cursor() as cur:
                cur.execute(f"LISTEN {MESH_NOTIFY_CHANNEL}")
                cur.execute(f"LISTEN {REDACTION_NOTIFY_CHANNEL}")
                cur.execute(f"LISTEN {EDIT_NOTIFY_CHANNEL}")
                cur.execute(f"LISTEN {PRESENCE_ROW_NOTIFY_CHANNEL}")
                cur.execute(f"LISTEN {REVOCATION_NOTIFY_CHANNEL}")
            # Anything revoked while this connection was not listening was never heard, so
            # Re-read every connected peer's standing before reporting ready.
            self._sweep_safely(loop, query)
            listening.set()
            self._count("listens")
            next_sweep = time.monotonic() + revocation_sweep_secs()
            while not stop.is_set():
                if loop.is_closed():
                    return
                wait = min(NOTIFY_POLL_SECS, max(0.0, next_sweep - time.monotonic()))
                for note in listen.notifies(timeout=wait):
                    if stop.is_set() or loop.is_closed():
                        return
                    self._handle(note.channel, note.payload, loop, query)
                if time.monotonic() >= next_sweep:
                    if stop.is_set() or loop.is_closed():
                        return
                    self._sweep_safely(loop, query)
                    next_sweep = time.monotonic() + revocation_sweep_secs()
        finally:
            listening.clear()
            query.drop()
            listen.close()

    def _handle(self, channel: str, payload: str, loop, query: _QueryConn) -> None:
        """One notification. Nothing a single notification does can end the listener."""
        try:
            if channel == MESH_NOTIFY_CHANNEL:
                self._on_mesh(payload, loop, query)
            elif channel == REDACTION_NOTIFY_CHANNEL:
                self._on_redaction(payload, loop, query)
            elif channel == EDIT_NOTIFY_CHANNEL:
                self._on_edit(payload, loop, query)
            elif channel == PRESENCE_ROW_NOTIFY_CHANNEL:
                self._on_presence_row(payload, loop, query)
            elif channel == REVOCATION_NOTIFY_CHANNEL:
                self._on_revocation(payload, loop, query)
            else:
                self._count("unknown_channel")
        except Exception as exc:
            query.drop()
            self._count(f"{channel}:errors")
            log.warning("live bridge: %s notification failed: %s: %s",
                        channel, type(exc).__name__, exc)

    def _on_loop(self, loop, coro):
        try:
            future = asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError:
            coro.close()
            raise
        return future.result(LOOP_CALL_TIMEOUT_SECS)

    def _on_mesh(self, payload: str, loop, query: _QueryConn) -> None:
        self._count("mesh:notifications")
        parsed = parse_mesh_payload(payload)
        if parsed is None:
            self._count("mesh:undecodable")
            return
        key, author = parsed
        sockets = self._on_loop(loop, self.manager.socket_keys())
        connected = list(sockets)
        if not any(peer != author for peer in connected):
            self._count("mesh:nobody_connected")
            return
        conn = query.get()
        with rls.server_internal(conn):
            nudge = load_nudge(conn, key, author)
            if nudge is None:
                self._count("mesh:row_not_found")
                return
            recipients, skipped = resolve_recipients(conn, nudge, connected)
            deliveries = [(peer, nudge.frame(role),
                           self._previewed(conn, nudge, peer, role, sockets.get(peer, ())))
                          for peer, role in recipients]
        for reason, n in skipped.items():
            self._count(f"mesh:skipped:{reason}", n)
        if not deliveries:
            return
        self._on_loop(loop, self._deliver(deliveries))

    def _on_redaction(self, payload: str, loop, query: _QueryConn) -> None:
        """One redaction NOTIFY: read the record and its row back, and send the event to each
        connected peer it is for (`resolve_redaction_recipients`). The event has no preview
        and no per-key variant: it carries a key and a reason and no text, so every socket of a
        recipient gets the same frame."""
        self._on_record(payload, loop, query, kind="redaction", parse=parse_redaction_payload,
                        load=load_redaction, resolve=resolve_redaction_recipients,
                        frames=lambda red, recipients: [(peer, red.event) for peer in recipients])

    def _on_edit(self, payload: str, loop, query: _QueryConn) -> None:
        """One edit NOTIFY: read the record and its row back, and send the event to each connected peer it
        is for (`resolve_edit_recipients`), each with ITS role. The event has no preview and no per-key
        variant: it carries a key, a version and a label and no text, so every socket of a recipient gets
        the same frame."""
        self._on_record(payload, loop, query, kind="edit", parse=parse_edit_payload,
                        load=load_edit, resolve=resolve_edit_recipients,
                        frames=lambda ed, recipients: [(peer, ed.event(role))
                                                       for peer, role in recipients])

    def _on_record(self, payload: str, loop, query: _QueryConn, *, kind: str, parse, load,
                   resolve, frames) -> None:
        """The one path `_on_redaction` and `_on_edit` each wrote out: parse the pointer, read the
        record and its row back and resolve the recipients inside `rls.server_internal`, count
        every step under `kind` (`<kind>:notifications`, `undecodable`, `nobody_connected`,
        `record_not_found`, `skipped:<reason>`), and hand `frames(record, recipients)`, built
        after the read, to `_deliver_events`."""
        self._count(f"{kind}:notifications")
        record_id = parse(payload)
        if record_id is None:
            self._count(f"{kind}:undecodable")
            return
        sockets = self._on_loop(loop, self.manager.socket_keys())
        connected = list(sockets)
        if not connected:
            self._count(f"{kind}:nobody_connected")
            return
        conn = query.get()
        with rls.server_internal(conn):
            record = load(conn, record_id)
            if record is None:
                self._count(f"{kind}:record_not_found")
                return
            recipients, skipped = resolve(conn, record, connected)
        for reason, n in skipped.items():
            self._count(f"{kind}:skipped:{reason}", n)
        if not recipients:
            return
        self._on_loop(loop, self._deliver_events(frames(record, recipients), f"{kind}:frames"))

    async def _deliver_events(self, deliveries, counter: str) -> int:
        """`_deliver` for a redaction's or an edit's events: once per recipient peer, every socket
        it holds, counted under `counter` on the loop as each frame is queued (for the reason
        `_deliver` counts there)."""
        total = 0
        for peer, frame in deliveries:
            n = await self.manager.publish_to_peer(peer, frame, by_key={})
            self._count(counter, n)
            total += n
        return total

    def _previewed(self, conn, nudge: Nudge, peer: uuid.UUID, role: str, keys) -> dict:
        """{bound key: the frame WITH its preview and `cc`} for each key this peer's sockets
        were bound to at the snapshot whose reader `body_withheld` passes (divergence 033).
        Each refusal is counted by reason. A gate that FAILS withholds (`preview_gate`,
        counted `error`) inside a savepoint, so the row read and the frame itself survive
        it. A row with nothing gated (`Nudge.gated`) asks no gate."""
        if not nudge.gated:
            return {}
        shown = {}
        for bound_key in keys:
            frame, withheld = push_frame(conn, nudge, peer, role, bound_key)
            if withheld is None:
                shown[bound_key] = frame
            else:
                self._count(f"mesh:preview_withheld:{withheld}")
        return shown

    async def _deliver(self, deliveries) -> int:
        """Publish once per recipient peer, each with its frame (the role is the recipient's,
        divergence 033): every socket that peer holds
        gets it, with the preview when that socket's key passed the gate (`by_key`). A
        peer that disconnected since the snapshot simply has no sockets left, which is 0,
        not an error.

        Counted HERE, on the loop, as each frame is queued, not by the listener thread
        after this returns: a client can read the frame before the thread resumes, and
        a count that lags the wire misreports what was sent at the moment it is read."""
        total = 0
        for peer, frame, by_key in deliveries:
            n = await self.manager.publish_to_peer(peer, frame, by_key=by_key)
            self._count("mesh:frames", n)
            total += n
        return total

    async def _deliver_presence(self, peer_id: uuid.UUID, frame: dict) -> int:
        """Deliver only to sockets subscribed to presence:<peer_id>.
        Counted on the loop for the reason `_deliver` is."""
        n = await self.manager.publish_presence(peer_id, frame)
        self._count("presence_row:frames", n)
        return n

    def _on_presence_row(self, payload: str, loop, query: _QueryConn) -> None:
        self._count("presence_row:notifications")
        peer_id = parse_presence_payload(payload)
        if peer_id is None:
            self._count("presence_row:undecodable")
            return
        frame = load_presence_frame(query.get(), peer_id)
        if frame is None:
            self._count("presence_row:row_not_found")
            return
        self._on_loop(loop, self._deliver_presence(peer_id, frame))


    # --- revocation (STATUS B15, divergence 030) -------------------------------

    async def _drop(self, drops) -> int:
        """Close the sockets `drops` names, on the loop that owns them. Counted HERE, as
        each drop lands, for the reason `_deliver` is: the close reaches a client only after
        this coroutine yields, so the count never lags the wire."""
        total = 0
        for drop in drops:
            code, reason = revocation.CLOSES[drop.cause]
            if drop.connections is None:
                n = await self.manager.drop_peer(drop.peer_id, code=code, reason=reason)
            else:
                n = await self.manager.drop_connections(drop.peer_id, drop.connections,
                                                        code=code, reason=reason)
            self._count(f"revocation:dropped:{drop.cause}", n)
            total += n
        return total

    def _sweep_safely(self, loop, query: _QueryConn) -> None:
        """One sweep; a failure is counted and logged, never fatal to the listener."""
        try:
            self._sweep(loop, query)
        except Exception as exc:
            query.drop()
            self._count("revocation:sweep_errors")
            log.warning("live bridge: revocation sweep failed: %s: %s", type(exc).__name__, exc)

    def _sweep(self, loop, query: _QueryConn) -> None:
        """Re-read the standing of every peer with a socket here, and close what lost it."""
        self._count("revocation:sweeps")
        connected = set(self._on_loop(loop, self.manager.connected_peers()))
        if not connected:
            self._readers = set()
            return
        bound = self._on_loop(loop, self.manager.bound_keys())
        at_connect = self._on_loop(loop, self.manager.mesh_readers())
        conn = query.get()
        watch = (self._readers | at_connect) & connected
        drops, still = revocation.judge(conn, connected, bound, watch)
        gone = {d.peer_id for d in drops if d.connections is None}
        self._readers = still | revocation.readers_among(conn, connected - gone - watch)
        if drops:
            self._on_loop(loop, self._drop(drops))

    def _on_revocation(self, payload: str, loop, query: _QueryConn) -> None:
        """One notification: re-read ONE peer's standing (the payload is a pointer)."""
        self._count("revocation:notifications")
        notice = revocation.parse_notice(payload)
        if notice is None:
            self._count("revocation:undecodable")
            return
        peer = notice.peer_id
        connected = self._on_loop(loop, self.manager.connected_peers())
        if peer not in connected:
            self._count("revocation:not_connected")
            return
        if notice.namespace is not None and notice.namespace != MESH_CHAT_NAMESPACE:
            self._count("revocation:skipped:namespace")
        # The payload's namespace decides NOTHING: it is a pointer any database session can
        # send (review of c766b76, finding F3). The evidence of a transition is this
        # process's own: a sweep SAW the peer hold read, or one of its sockets held read
        # when it connected (see apps/wsgw/revocation.py).
        at_connect = self._on_loop(loop, self.manager.mesh_readers())
        watch = {peer} if (peer in self._readers or peer in at_connect) else set()
        bound = [b for b in self._on_loop(loop, self.manager.bound_keys()) if b[0] == peer]
        drops, still = revocation.judge(query.get(), {peer}, bound, watch)
        if peer in still:
            self._readers.add(peer)
        elif any(d.connections is None for d in drops):
            self._readers.discard(peer)
        if not drops:
            self._count("revocation:kept")
            return
        self._on_loop(loop, self._drop(drops))


#: The production bridge, bound to the registry the default consumer registers into.
BRIDGE = LiveBridge(MANAGER)
