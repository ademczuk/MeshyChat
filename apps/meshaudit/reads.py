"""Inferred edges on the graph reads, OPT-IN, and never mistakable for declared ones.

`GET /v1/mesh/inbox` and `GET /v1/mesh/threads/<id>` answer from mesh_edges exactly as
before unless the caller passes `?include_inferred=1`. Then, and only then, rows from
mesh_inferred_edges are merged in. EVERY hit names its tier: `"tier": "declared"` on what a
sender wrote, `"tier": "inferred"` plus the model, provider and prompt version on what the
auditor inferred, so a reader never has to guess which kind a hit is from the absence of a
field. The response's `inferred` block says whether inferences were included and, when
they were, the denominators (apps/meshaudit/store.coverage): an inbox that gained two
inferred hits is a fact about how much of the backlog has been audited before it is a
fact about the mail.

The flag is parsed strictly: `0`/`false` or absent is off, `1`/`true` is on, and anything
else is a 422, so a typo neither silently includes nor silently hides.

READ ONLY, under the route's `server_internal` context, head rows only (joined back to
shared_memory.is_head, as apps/mesh/graph.py does). Truncation is reported: each tier is
fetched to the route's limit, the merge is cut to it, and `truncated` is true when either
tier or the cut dropped anything.
"""
from __future__ import annotations

import dataclasses
from typing import Iterable

from psycopg.rows import dict_row

from apps.peers import errors

from . import prompt as P
from . import store

TIER_DECLARED = "declared"
TIER_INFERRED = "inferred"

_ON = ("1", "true")
_OFF = ("0", "false")


def include_inferred(request) -> bool:
    raw = request.GET.get("include_inferred")
    if raw is None or raw in _OFF:
        return False
    if raw in _ON:
        return True
    raise errors.ApiError(422, "E_VALIDATION", "include_inferred must be 0 or 1")


@dataclasses.dataclass(frozen=True)
class Tiered:
    hits: list[dict]
    truncated: bool
    inferred: dict


_SELECT = (
    "SELECT i.src_key AS key, i.src_version AS version, i.edge_type, i.dst, "
    "       'inferred' AS src_field, i.recorded_at, a.dst AS author, i.src_id, "
    "       i.model, i.provider_id, i.prompt_version, i.inferred_at "
    "FROM mesh_inferred_edges i "
    "JOIN shared_memory sm ON sm.id = i.src_id AND sm.is_head "
    "LEFT JOIN mesh_edges a ON a.src_id = i.src_id AND a.edge_type = 'authored_by' "
)


def _fetch(conn, where: str, params: list, *, newest_first: bool,
           limit: int | None) -> tuple[list[dict], bool]:
    order = "DESC" if newest_first else "ASC"
    sql = _SELECT + "WHERE " + where + f" ORDER BY i.recorded_at {order}, i.src_id {order}"
    if limit is not None:
        sql += " LIMIT %s"
        params = params + [int(limit) + 1]
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    truncated = limit is not None and len(rows) > limit
    rows = rows[:limit] if truncated else rows
    out = []
    for r in rows:
        out.append({
            "key": r["key"], "version": r["version"], "edge_type": r["edge_type"],
            "dst": r["dst"], "src_field": r["src_field"], "recorded_at": r["recorded_at"],
            "author": r["author"], "tier": TIER_INFERRED,
            "inferred": {"model": r["model"], "provider_id": r["provider_id"],
                         "prompt_version": r["prompt_version"],
                         "inferred_at": r["inferred_at"]},
            "_order": (r["recorded_at"], str(r["src_id"])),
        })
    return out, truncated


def _declared(hits) -> list[dict]:
    out = []
    for h in hits.hits:
        d = dataclasses.asdict(h)
        d["tier"] = TIER_DECLARED
        d["_order"] = (h.recorded_at, "")
        out.append(d)
    return out


def _merge(declared: list[dict], inferred: list[dict], *, newest_first: bool,
           limit: int | None) -> tuple[list[dict], bool]:
    merged = sorted(declared + inferred, key=lambda d: d["_order"], reverse=newest_first)
    cut = limit is not None and len(merged) > limit
    merged = merged[:limit] if cut else merged
    for d in merged:
        d.pop("_order", None)
    return merged, cut


def _tiered(conn, request, declared_hits, *, where: str, params: list, newest_first: bool,
            limit: int | None) -> Tiered:
    declared = _declared(declared_hits)
    if not include_inferred(request):
        for d in declared:
            d.pop("_order", None)
        return Tiered(hits=declared, truncated=declared_hits.truncated,
                      inferred={"included": False})
    inferred, inferred_truncated = _fetch(conn, where, params, newest_first=newest_first,
                                          limit=limit)
    merged, cut = _merge(declared, inferred, newest_first=newest_first, limit=limit)
    return Tiered(
        hits=merged, truncated=declared_hits.truncated or inferred_truncated or cut,
        inferred={"included": True,
                  "count": sum(1 for d in merged if d["tier"] == TIER_INFERRED),
                  "coverage": store.coverage(conn, prompt_version=P.PROMPT_VERSION)})


def for_inbox(request, conn, declared_hits, *, labels: Iterable[str],
              limit: int | None) -> Tiered:
    """Only `addressed_to` is ever inferred; `copied_to` is not, so `cc=0` changes nothing
    on the inferred tier."""
    return _tiered(conn, request, declared_hits,
                   where="i.edge_type = 'addressed_to' AND i.dst = ANY(%s)",
                   params=[list(labels)], newest_first=True, limit=limit)


def for_thread(request, conn, declared_hits, *, thread_id: str,
               limit: int | None) -> Tiered:
    return _tiered(conn, request, declared_hits,
                   where="i.edge_type = 'in_thread' AND i.dst = %s",
                   params=[thread_id], newest_first=False, limit=limit)


__all__ = ["TIER_DECLARED", "TIER_INFERRED", "Tiered", "for_inbox", "for_thread",
           "include_inferred"]
