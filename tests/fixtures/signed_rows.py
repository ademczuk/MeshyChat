"""shared_memory rows for DIRECT inserts, built so the B02 agree-trigger admits them.

Divergence 039 (sql_meshychat/014). From A1 on, an INSERT into shared_memory as `meshychat_app`
must carry `signed_bytes`, and the trigger holds the row's derived columns to those bytes.
A test that inserts a row by hand, to model a path that skips `handle_write`, therefore
builds the row from a payload, the way the handler does, instead of from loose columns.

Nothing here signs anything. The trigger cannot verify a signature (the database has no
Ed25519, design section 2.4); it checks that the bytes hash to `payload_jcs_sha256`, parse
to `provenance_jsonb->'payload'`, and agree with the derived columns. The columns below are
derived by hand from the args, NOT by apps/memoryskills, so a test that inserts them checks
the trigger against an independent statement of the rule (design section 2.3).

Since A2 (divergence 051, sql_meshychat/025) a row without bytes is refused from EVERY
session, the owner compatibility role (`meshy`, which bypasses RLS) included. A test that inserts a row by
hand to set up a world therefore builds it here, with its bytes (`row`); one that needs a row
WITHOUT bytes, as every row written before 014 is, seeds it as history
(tests/fixtures/history_rows.py).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import uuid

from psycopg.types.json import Jsonb

from apps.crypto import primitives as P


def payload(peer_id, namespace: str, key: str, value, *, extra_args: dict | None = None,
            call_id: str | None = None, drop: tuple[str, ...] = (), **fields) -> dict:
    """A memory.write skill.call payload, the field set tests/test_memory_skills.py signs.
    `drop` removes top-level fields (G2's payload drops caller_peer_id and kind); keyword
    `fields` replace top-level fields (a wrong kind or skill_name)."""
    args = {"namespace": namespace, "key": key, "value": value}
    args.update(extra_args or {})
    out = {
        "v": 1,
        "kind": "skill.call",
        "call_id": call_id or f"call-{uuid.uuid4()}",
        "caller_peer_id": str(peer_id),
        "target_peer_id": str(peer_id),
        "skill_name": "memory.write",
        "skill_version": "1",
        "args": args,
        "args_attachments": [],
        "issued_at": _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    out.update(fields)
    for name in drop:
        out.pop(name, None)
    return out


def columns(p: dict, *, peer_id, owner_id, version: int = 1, parent_id=None,
            is_head: bool = True, rid=None, with_bytes: bool = True, **override) -> dict:
    """Columns that agree with `p` under design 2.3, derived here by hand. `override`
    replaces any column after derivation: that is how a test makes one disagree."""
    args = p.get("args") or {}
    pre = args.get("pre_discipline") is True
    status = args.get("status")
    if status is None and not pre and args.get("namespace") == "shared/wiki":
        status = "OPEN"
    tags = args.get("tags")
    author = args.get("author") or args.get("author_label")
    try:
        raw = P.jcs(p)
    except ValueError:
        # A payload RFC 8785 cannot serialise (an integer beyond 2^53-1): only a historical
        # row, inserted without bytes, can carry one. The caller supplies its hash.
        if with_bytes:
            raise
        raw = None
    cols = {
        "id": rid or uuid.uuid4(),
        "namespace": args.get("namespace"),
        "key": args.get("key"),
        "version": version,
        "parent_id": parent_id,
        "is_head": is_head,
        "value": Jsonb(args.get("value", args.get("body"))),
        "status": status,
        "pre_discipline": pre,
        "claimed_status": args.get("claimed_status") if pre else None,
        "tags": list(tags) if isinstance(tags, list) else [],
        "author_label": author or f"peer-{str(peer_id)[:8]}",
        "provenance_jsonb": Jsonb({"payload": p}),
        "payload_jcs_sha256": hashlib.sha256(raw).hexdigest() if raw is not None else None,
        "created_by_peer_id": peer_id,
        "created_by_owner_id": owner_id,
    }
    if with_bytes:
        cols["signed_bytes"] = raw
    for name, val in override.items():
        cols[name] = Jsonb(val) if name in ("value", "provenance_jsonb") else val
    return cols


def row(peer_id, owner_id, namespace: str, key: str, value, *, rid=None,
        call_id: str | None = None, extra_args: dict | None = None, **override) -> dict:
    """Columns for one row as handle_write would store it, bytes included, so A2 admits it
    and 014's agree-trigger holds it. For a test that inserts a row by hand to set up a
    world, whoever is meant to have written it. `override` sets what the trigger does not
    derive (an unsigned `author_label`, `created_at`, `embed_policy`, `version`,
    `parent_id`, `is_head`); a status, custody or tags value goes in `extra_args`, signed."""
    p = payload(peer_id, namespace, key, value, extra_args=extra_args, call_id=call_id)
    return columns(p, peer_id=peer_id, owner_id=owner_id, rid=rid, **override)


def insert(conn, cols: dict):
    """One INSERT of exactly these columns; returns the new row's id. The caller owns the
    transaction and the role."""
    names = list(cols)
    with conn.cursor() as cur:
        cur.execute(f"INSERT INTO shared_memory ({', '.join(names)}) "
                    f"VALUES ({', '.join(['%s'] * len(names))}) RETURNING id",
                    [cols[n] for n in names])
        return cur.fetchone()[0]
