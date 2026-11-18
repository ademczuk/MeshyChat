"""Seeding helpers for the apps/meshsem database tests.

Rows are written on the OWNER connection (the bootstrap role) as handle_write stores them,
their signed bytes included (tests/fixtures/signed_rows.row): since A2 (divergence 051) a
row without them is refused from every session. One deliberate difference is available:
`embed_policy`. Every production
writer hard-codes 'forbidden' (apps/memoryskills/handlers.py:792-796); a test that needs
an embeddable row passes 'allowed', and one that needs a real row passes nothing.

Each test gets its OWN embedding space (`make_space`), registered in
apps/meshsem/spaces.SPACES for the test's duration, so its reference, watermark and
vectors are isolated from every other test's SPACE even though the namespace is shared.
That is not isolation from the other tests' ROWS: the embed job walks the whole namespace
into a new space from the start of the commit order, so every embeddable message an
earlier test left is embedded and lexed into this one, and both search arms rank it. A
test whose ranking must see only its own rows takes `commit_floor` before it writes
anything and applies `keep_only_rows_after` after every walk (OI-05).
"""
from __future__ import annotations

import datetime as _dt
import os
import uuid

from apps.meshsem import spaces
from apps.memoryskills.validation import MESH_CHAT_NAMESPACE as NS
from tests.fixtures import history_rows
from tests.fixtures import signed_rows as sr

DIMS = 64


def make_space(tag: str, *, raw: bool = False) -> spaces.Space:
    """A test-only space. `raw=True` has the raw space's prompt style (none; divergence
    026's default until 2026-09-26); otherwise the retrieval prompts of 019, the default
    again since 026's amendment of 2026-09-26."""
    style = spaces.EMBEDDINGGEMMA_300M_RAW if raw else spaces.EMBEDDINGGEMMA_300M
    return spaces.Space(name=f"fake-{DIMS}/{DIMS}/{tag}-{uuid.uuid4().hex[:8]}",
                        family="fake", dims=DIMS,
                        document_prompt=style.document_prompt,
                        query_prompt=style.query_prompt,
                        canaries=style.canaries)


def commit_floor(conn) -> int:
    """The newest seq in shared/mesh-chat's commit order (0 when it is empty). A row that
    commits after this call is stamped above it.

    The commit order, not `created_at`, because sql_meshychat/023 guarantees exactly this:
    a reader that sees seq N sees every committed seq below N, and nothing that commits
    later takes a seq below one already seen. `created_at` is a writer's transaction start,
    and `row()` stores whatever created_at a test passes, so a cut on it would drop a
    back-dated row the test wrote and keep a forward-dated one an earlier test wrote."""
    with conn.cursor() as cur:
        cur.execute("SELECT coalesce(max(seq), 0) FROM mesh_commit_order")
        return int(cur.fetchone()[0])


def keep_only_rows_after(conn, space: spaces.Space, floor: int) -> int:
    """Delete, from `space`'s own vectors and lexical terms, every row committed at or
    before `floor` (a `commit_floor` taken before the test wrote anything). Returns how many
    vectors went.

    WHY (OI-05). tests/test_meshsem_routes.py's `rerank=0` control asserts that the row
    matching all three query words is the embed arm's rank 1. It refused r16's release gate
    once, `{'lexical': 1, 'embed': 2}`, and passed 20 of 20 alone. The job had walked every
    earlier message into the test's new space, including the four the context test leaves,
    which share two of those three words. With the fake embedder's 64-d hashed-word vectors
    one of them outranks the exact row for some pairs of the random words the two tests
    draw: 128 of 20,000 draws over the rows the file writes before it, 0 of 20,000 over
    the test's two rows alone (the fake's own embed_text, checked against the vectors a
    real run stored). Forcing such a pair reproduces the gate's assertion exactly; the
    same word without the context test passes. Unforced, 40 runs of the context test and
    then 40 of the control in one session failed the control 5 times (embed rank 2, 2, 2,
    3, 16) and the context test 33 times (its `related` hits lost its own other-thread
    row); with this cut, 80 of 80 passed.

    AFTER the walk, not instead of it: the job still walks past those rows (its watermark
    moves), and only then are its vectors and terms of them removed, from this test's own
    space, which no other test reads (`make_space` names it uniquely). It must follow EVERY
    walk, because opening or closing the gate restarts the walk from the beginning
    (apps/meshsem/embedjob._restart_walk) and embeds them again. The space's coverage then
    counts those rows as `pending`: eligible, with no vector here."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM mesh_embeddings me USING mesh_commit_order o "
                    "WHERE me.space = %s AND o.memory_id = me.src_id AND o.seq <= %s",
                    (space.name, floor))
        vectors = cur.rowcount
        cur.execute("DELETE FROM mesh_lexical ml USING mesh_commit_order o "
                    "WHERE ml.space = %s AND o.memory_id = ml.src_id AND o.seq <= %s",
                    (space.name, floor))
    return vectors


def peer(conn, tag: str, *, group: uuid.UUID | None = None, key: bool = False) -> dict:
    """A peer under its own new dev-plane owner. `group` puts that owner in an existing
    trust group (a new one otherwise); `key=True` registers an ACTIVE Ed25519 signing
    key for it, the one memory.read's invoke path verifies envelopes under, and returns
    its `seed` and `kid`."""
    owner_id, peer_id = uuid.uuid4(), uuid.uuid4()
    group = group or uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id)"
                    " VALUES (%s,%s,'dev',%s)",
                    (owner_id, f"sem-{tag}-{owner_id.hex[:8]}@example.test", group))
        cur.execute("INSERT INTO peers (id, owner_id, name, display_name)"
                    " VALUES (%s,%s,%s,%s)",
                    (peer_id, owner_id, f"sem-{tag}-{peer_id.hex[:8]}", f"sem {tag}"))
    out = {"peer_id": peer_id, "owner_id": owner_id, "label": tag, "group": group}
    if key:
        out.update(add_key(conn, peer_id))
    return out


def add_key(conn, peer_id, *, revoked_at=None, active: bool = True) -> dict:
    """Register an Ed25519 key for `peer_id` (stored base64url, no pad, as verify reads
    it). Returns {seed, kid}."""
    from apps.crypto import primitives as P

    seed = os.urandom(32)
    public = P.public_key_of(seed)
    kid = P.kid_for_peer_key(public)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO peer_keys (peer_id, kid, public_key, active, revoked_at)"
                    " VALUES (%s,%s,%s,%s,%s)",
                    (peer_id, kid, P.b64u(public), active, revoked_at))
    return {"seed": seed, "kid": kid}


def memory_authority(conn, *, group: uuid.UUID | None = None) -> dict:
    """A memory authority as `manage.py bootstrap_memory_peer --trust-group` makes one:
    the peer, a dev-plane owner in `group` (a new group otherwise), and the manifest's
    skills. The route reads its id from settings.MESHY_MEMORY_PEER_ID, so a test points
    that setting at it."""
    from apps.calls.bootstrap import bootstrap_memory_peer

    group = group or uuid.uuid4()
    report = bootstrap_memory_peer(conn, peer_id=uuid.uuid4(), trust_group=group)
    return {"peer_id": report.peer_id, "owner_id": report.owner_id, "group": group}


def skill_grant(conn, memory: dict, grantee: dict, *, version_pin: str | None = None
                ) -> None:
    """An active memory.read skill grant from the memory authority's owner to
    `grantee`'s owner: what a caller outside the authority's tenant and trust group needs
    for memory.read's invoke to authorize it (apps/calls/authorize.py gate 2)."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO skill_grants (grantor_owner_id, grantee_owner_id,"
                    " granter_peer_id, skill_name, version_pin, source)"
                    " VALUES (%s,%s,%s,'memory.read',%s,'direct')",
                    (memory["owner_id"], grantee["owner_id"], memory["peer_id"],
                     version_pin))


def namespace_owner(conn) -> tuple | None:
    with conn.cursor() as cur:
        cur.execute("SELECT owner_peer_id, owner_owner_id FROM shared_memory_namespace "
                    "WHERE namespace = %s", (NS,))
        return cur.fetchone()


def ensure_namespace(conn) -> tuple:
    """The namespace claim, creating it with a fresh owner when the run has none yet."""
    holder = namespace_owner(conn)
    if holder is None:
        p = peer(conn, "nsowner")
        with conn.cursor() as cur:
            cur.execute("INSERT INTO shared_memory_namespace (namespace, owner_peer_id,"
                        " owner_owner_id, claimant_plane, ephemeral, retention_policy)"
                        " VALUES (%s,%s,%s,'dev',FALSE,'keep_all') ON CONFLICT DO NOTHING",
                        (NS, p["peer_id"], p["owner_id"]))
        holder = namespace_owner(conn)
    return holder


def grant(conn, p: dict, permission: str = "read") -> None:
    holder = ensure_namespace(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO shared_memory_grant (namespace, grantee_peer_id,"
                    " grantee_owner_id, permission, granted_by_peer_id, granted_by_owner_id)"
                    " VALUES (%s,%s,%s,%s,%s,%s)",
                    (NS, p["peer_id"], p["owner_id"], permission, holder[0], holder[1]))


def row(conn, p: dict, value: dict, *, embed_policy: str | None = None,
        created_at=None, key: str | None = None, version: int = 1,
        is_head: bool = True, rid=None, parent_id=None) -> dict:
    """One mesh row. Returns {id, key}. embed_policy None = the column default, which is
    what every production writer produces ('forbidden')."""
    rid = rid or uuid.uuid4()
    when = created_at or _dt.datetime.now(_dt.timezone.utc)
    key = key or f"msg/{int(when.timestamp() * 1000)}-{rid.hex}"
    extra = {"embed_policy": embed_policy} if embed_policy is not None else {}
    # A later version of a mail key is seeded as history (divergence 059: tests/fixtures/history_rows.py).
    history_rows.insert_seed(conn, sr.row(p["peer_id"], p["owner_id"], NS, key, value, rid=rid,
                                          call_id=f"call-{rid}", version=version, parent_id=parent_id,
                                          is_head=is_head, author_label=p["label"], created_at=when,
                                          **extra))
    return {"id": rid, "key": key}
