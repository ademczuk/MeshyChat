"""The board as a graph: who acted on what, at which version.

`projection.py` answers "what is the state of subject X". This module answers the
questions that need more than one subject at once:

    what has this actor declared, across everything?        acts_by_actor
    the whole edge list, so a caller can render or walk it    edges / adjacency

An EDGE is one declared act read as a relation: actor --verb@ref--> subject. The actor is
the AUTHENTICATED peer on the row, never a name in the body, for the reason stated in
`projection.py`. Nothing here interprets prose; a row with no `subject` is not an edge.

WHY THIS IS STILL BOUNDED. Every query filters on `value ? 'subject'`, which is the
predicate of the partial index in `sql_meshychat/003`. The population it walks is the set
of DECLARED ACTS, not the set of messages, and measured adoption puts the first at well
under one percent of the second. So an unfiltered `edges()` reads the small set, not the
namespace. A `limit` is still accepted so a caller can cap what it renders; it is a cap on
output, not a window on truth, and the caller is told when it bit.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
from collections import defaultdict
from typing import Iterable

from apps.memoryskills.validation import MESH_CHAT_NAMESPACE


@dataclasses.dataclass(frozen=True)
class Edge:
    actor_peer_id: str
    actor_label: str | None
    verb: str | None
    subject: str
    ref: str | None
    recorded_at: _dt.datetime
    key: str


@dataclasses.dataclass(frozen=True)
class EdgeList:
    edges: tuple[Edge, ...]
    #: True when `limit` cut the result. A caller that ignores this is showing a prefix
    #: and calling it the graph, which is the shape the board exists to prevent.
    truncated: bool


def _row_to_edge(row) -> Edge:
    key, value, label, peer_id, created_at = row
    return Edge(
        actor_peer_id=str(peer_id), actor_label=label, verb=value.get("type"),
        subject=value["subject"], ref=value.get("ref"), recorded_at=created_at, key=key,
    )


def edges(conn, *, namespace: str = MESH_CHAT_NAMESPACE, subject: str | None = None,
          actor_peer_id: str | None = None, ref: str | None = None,
          limit: int | None = None) -> EdgeList:
    """Declared acts as edges, oldest first, optionally filtered. See module docstring."""
    where = ["namespace = %s", "value ? 'subject'", "is_head = TRUE"]
    params: list = [namespace]
    if subject is not None:
        where.append("value ->> 'subject' = %s"); params.append(subject)
    if actor_peer_id is not None:
        where.append("created_by_peer_id = %s"); params.append(str(actor_peer_id))
    if ref is not None:
        where.append("value ->> 'ref' = %s"); params.append(ref)
    sql = ("SELECT key, value, author_label, created_by_peer_id, created_at "
           "FROM shared_memory WHERE " + " AND ".join(where) +
           " ORDER BY created_at ASC, id ASC")
    if limit is not None:
        sql += " LIMIT %s"; params.append(int(limit) + 1)   # one extra: detect truncation
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    truncated = limit is not None and len(rows) > limit
    if truncated:
        rows = rows[:limit]
    return EdgeList(edges=tuple(_row_to_edge(r) for r in rows), truncated=truncated)


def acts_by_actor(conn, peer_id, **kw) -> EdgeList:
    """Everything one authenticated peer has declared. The actor is the ROW's peer."""
    return edges(conn, actor_peer_id=str(peer_id), **kw)


def adjacency(items: Iterable[Edge]) -> dict:
    """Two views of the same edges, keyed both ways.

    by_subject: subject -> actor -> [verb@ref, ...]   "who has touched this"
    by_actor:   actor   -> subject -> [verb@ref, ...] "what has this seat touched"

    Verbs are kept in recorded order and never collapsed to a set, because "reviewed,
    reviewed, reviewed" and "reviewed" are different facts about a subject.
    """
    by_subject: dict = defaultdict(lambda: defaultdict(list))
    by_actor: dict = defaultdict(lambda: defaultdict(list))
    for e in items:
        actor = e.actor_label or e.actor_peer_id[:8]
        label = f"{e.verb or '?'}@{e.ref or '-'}"
        by_subject[e.subject][actor].append(label)
        by_actor[actor][e.subject].append(label)
    return {"by_subject": {s: dict(a) for s, a in by_subject.items()},
            "by_actor": {a: dict(s) for a, s in by_actor.items()}}
