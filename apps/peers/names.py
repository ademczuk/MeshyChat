"""A peer's name is a mail address, so a new peer may not take one already in use (divergence 065).

WHY. `peers.name` is one of a peer's LABELS (apps/mesh/views.py `labels_for`): mail addressed to it
reaches that peer, and 028 lets the peer write mail as it. The column is free text with no UNIQUE
constraint, and `POST /v1/peers` and `bootstrap_owner --peer-name` take any name, so a peer of another
owner could be created under a seat's name and both send as that seat and collect its mail (review of
#42, F1; proved there by test). This is the DOOR: it stops a NEW duplicate at creation. Duplicates that
already exist are for kxxx's read and write side change (its own divergence, not yet numbered here).

THE RULE. A new name is refused when its CANONICAL form (`canonical`: NFKC, then Unicode case folding)
equals the canonical form of
  - any roster label (apps/memoryskills/roster.py; the operator's list),
  - any existing peer's `peers.name`, of ANY owner and in ANY status, deleted and suspended included:
    stored mail carries labels as strings (`author_label`, `to`, `cc`), so a reused name would inherit
    another peer's history,
  - a broadcast address (`all`, `both`), or
or when it has the SHAPE of a name the server generates (`peer-<8 hex>`, `desktop-<8 hex>`,
`mentee-<12 hex>`) or is the in-process memory authority's fixed name. The server-generated paths pass
`generated=True`: they skip the shape rule (it is theirs) and keep every other one, and on a clash they
draw again.

GLOBAL, NOT PER OWNER, because label resolution is global: two owners' peers named `laptop` are one
address. Case-insensitive, because 034 lets the roster hold labels that differ only by case and a
reader cannot tell `Kxxx` from `kxxx`; stricter than 028's exact comparison, on purpose, at the door.

SERIALIZED. The check runs INSIDE the creating transaction after `pg_advisory_xact_lock` on the
canonical name, so two concurrent creates of one name are ordered: the second waits, then sees the
first's committed row and is refused. A UNIQUE index would be simpler and is not possible while a
database may already hold duplicates. Every creation path in this repository takes the lock through
`claim`; a direct INSERT elsewhere (a test fixture, a hand-written SQL session) does not.

THE REFUSAL IS GENERIC ("name unavailable"): it never says whether another owner, a deleted peer, the
roster or a reserved shape holds the name, so a caller cannot probe other owners' peer names beyond
learning that one is taken.

THE SCAN. Names are compared in Python over every `peers.name`, because Postgres `lower()` is not
Unicode case folding and an index cannot be built on a function the database does not have. Peer
creation is rare and the table is small; this is a statement of cost, measured nowhere else.
"""
from __future__ import annotations

import re
import unicodedata

from apps.memoryskills import handlers

#: Names only the server hands out. A user taking one could collide with a name the server draws later.
_GENERATED_SHAPES = (
    re.compile(r"^peer-[0-9a-f]{8}$"),        # the fallback label, apps/mesh/views.py labels_for
    re.compile(r"^desktop-[0-9a-f]{8}$"),     # onboarding, apps/authapi/onboard.py create_peer
    re.compile(r"^mentee-[0-9a-f]{12}$"),     # invitation redemption, apps/authapi/views.py
)
#: apps/calls/bootstrap.py PEER_NAME, the in-process memory authority. Named here, not imported:
#: apps.calls imports half the server, and this module is imported by the peer-creation paths.
#: Reserved against USERS only. `bootstrap_memory_peer` (operator-run) keeps inserting it without
#: `claim`: every memory authority carries this one fixed name by design, and a database used by
#: tests holds several of them.
_FIXED_NAMES = frozenset({"meshy-mem-daemon"})
_BROADCAST = frozenset({"all", "both"})

#: The lock namespace, so this advisory lock can never share a key with another feature's.
_LOCK_PREFIX = "meshychat.peer-name:"


class NameUnavailable(Exception):
    """The name is taken or reserved. Deliberately says nothing about by whom."""

    MESSAGE = "name unavailable: choose another peer name"

    def __init__(self):
        super().__init__(self.MESSAGE)


def canonical(name: str) -> str:
    """The one comparison form: NFKC (compatibility characters folded to their plain form), then
    `str.casefold` (Unicode case folding, which `lower()` is not: `ß` folds to `ss`)."""
    return unicodedata.normalize("NFKC", name).casefold()


def _reserved_shape(folded: str) -> bool:
    return folded in _FIXED_NAMES or any(p.match(folded) for p in _GENERATED_SHAPES)


def claim(cur, name: str, *, generated: bool = False) -> None:
    """Lock `name`'s canonical form for this transaction and refuse it if it is unavailable.

    MUST run inside the transaction that inserts the peer, before the INSERT: the lock is released at
    that transaction's end, which is what orders a concurrent create of the same name behind this one.
    Raises `NameUnavailable`; returns None when the name may be used.

    Every side is compared STRIPPED, as `uncontested` compares them (ASDLC OI-71 (a)): both creators
    strip the new name already, but a stored name or a roster label with outer whitespace would
    otherwise not contest the bare name that a recipient resolves to it."""
    folded = canonical(name.strip())
    cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (_LOCK_PREFIX + folded,))
    if folded in _BROADCAST:
        raise NameUnavailable()
    if not generated and _reserved_shape(folded):
        raise NameUnavailable()
    if any(canonical(label.strip()) == folded for label in handlers._ROSTER_LABELS.values()):
        raise NameUnavailable()
    cur.execute("SELECT name FROM peers")
    if any(canonical(row[0].strip()) == folded for row in cur.fetchall() if row[0] is not None):
        raise NameUnavailable()


#: How many fresh draws a server-generated name gets before the create fails. A clash at 32 random
#: bits is one in four billion per draw; eight in a row is not a thing that happens by chance.
_DRAWS = 8


def draw(cur, make) -> str:
    """A server-generated name: `make()` drawn afresh until `claim(..., generated=True)` accepts one.
    Raises `NameUnavailable` after `_DRAWS` clashes, which would mean the generator is broken."""
    for _ in range(_DRAWS):
        name = make()
        try:
            claim(cur, name, generated=True)
        except NameUnavailable:
            continue
        return name
    raise NameUnavailable()


def uncontested(conn, names: dict) -> dict:
    """`{peer id: peers.name}` with a CONTESTED name replaced by None (divergence 066).

    The door above stops a NEW duplicate. This is the other half: a duplicate that already exists, or
    one inserted by a path that does not take the door (a hand-written SQL session, a restore). A
    `peers.name` is one of a peer's labels wherever it is read (`apps/mesh/views.py labels_for`, the
    push's and the wait's `peer_labels`), so two peers holding one name each collect the other's mail
    and each may write as it. Neither can be shown to be the rightful one from the name alone, so
    NEITHER is answered for it: a name is a label only while exactly one peer holds it.

    A name is contested for peer P when its `canonical` form (the door's: NFKC, case folded; surrounding space
    stripped, as the push strips it) is
      - the canonical `peers.name` of ANOTHER peer row, in any status, or
      - the canonical form of a roster label the operator gave ANOTHER peer.
    The roster's own holder keeps the label through the roster, which this function does not touch;
    a peer whose name is its own roster label is not contested by it.

    Keys come back as given (uuid or str); a None or blank name stays None. One scan of `peers`
    per call, compared in Python for the reason `claim` gives."""
    wanted = {k: v for k, v in names.items() if isinstance(v, str) and v.strip()}
    out = {k: None for k in names}
    if not wanted:
        return out
    holders: dict[str, set[str]] = {}
    with conn.cursor() as cur:
        cur.execute("SELECT id, name FROM peers")
        for pid, name in cur.fetchall():
            if isinstance(name, str) and name.strip():
                holders.setdefault(canonical(name.strip()), set()).add(str(pid))
    for pid, label in handlers._ROSTER_LABELS.items():
        if isinstance(label, str) and label.strip():
            holders.setdefault(canonical(label.strip()), set()).add(str(pid))
    for key, name in wanted.items():
        if holders.get(canonical(name.strip()), set()) <= {str(key)}:
            out[key] = name
    return out


def names_of(conn, peer_ids) -> dict[str, str | None]:
    """Batch `name_of` for `peer_ids`: `{str(peer_id): uncontested peers.name or None}` in at most
    two reads of `peers` (one primary-key lookup of the requested ids, then one `uncontested` scan
    when any requested peer holds a non-blank name), rather than `2 * N` reads on fleet routes."""
    ids = list(dict.fromkeys(str(p) for p in peer_ids))
    if not ids:
        return {}
    raw: dict[str, str | None] = {pid: None for pid in ids}
    with conn.cursor() as cur:
        cur.execute("SELECT id::text, name FROM peers WHERE id = ANY(%s::uuid[])", (ids,))
        for pid, name in cur.fetchall():
            raw[str(pid)] = name
    return uncontested(conn, raw)


def name_of(conn, peer_id) -> str | None:
    """ONE peer's `peers.name` as a label: as written (a label is compared byte for byte with
    what senders wrote in `to`, so trimming or case-folding it here would invent one), or None
    when it is blank, the peer is unknown, or the name is contested (`uncontested` above). The
    graph routes' `labels_for` and the addressed wait's `caller_labels` both read it here, so
    the two cannot answer one peer for different names."""
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM peers WHERE id = %s", (str(peer_id),))
        row = cur.fetchone()
    name = row[0] if row else None
    if not (isinstance(name, str) and name.strip()):
        return None
    return uncontested(conn, {str(peer_id): name})[str(peer_id)]
