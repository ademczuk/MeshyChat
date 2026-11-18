"""ASDLC OI-51, divergence 050: the two background walks of `invocation_frames` read the chain
index, as `meshychat_app`, with nothing disabled by the test.

Production, 2026-09-27: the chain-head reconcile's walk past an anchored head
(`apps/calls/chain_head.py`) and the frame audit's root read (`apps/calls/frame_audit.py`)
were planned as an Index Scan on `invocation_frames_pkey` with the chain as a Filter, and ran
IO-bound for 3.5 to 18.7 s, one of them holding an anchor row FOR UPDATE. The chain index
`invocation_frames_chain_idx (chain_id, id DESC)` was there all along: a planner choice, not a
missing index. The planner costs the primary-key walk from the chain's share of the table,
spread evenly over the ids, and a chain whose frames stopped long ago has none of them there.

THE PLANT. The misestimate needs statistics shaped like production's, which a few hundred test
frames are not, so this module plants that shape once and removes it afterwards: six chains
whose frames interleave in 12000 ids (each about 4.8% of the table, and the chain column's
correlation low, as production's is, 0.15), then 30000 standalone frames after every one of
them, the way production's anchored chains sit behind 1.9M later ids. `ANALYZE` reads every row
(`default_statistics_target` 1000 on the planting session), so the statistics do not depend on
a sample. Frame hashes are synthetic (the links and roots are real, which is all the walks
read); the audit therefore reports the one or two planted frames it reads as HashMismatch, and
that is not what these tests look at.

POSITIVE CONTROLS, in each plan test: the statement as it was at c4d2125, EXPLAINed on the
same connection with nothing disabled, IS the production plan (the primary key with a
chain_id Filter), so the plant reproduces the misestimate and the instrument can see it; the
absence asserted for the real function is a reading, not a blind spot.

WHAT THE PLANT CANNOT MATCH. Production's walk had a LIMIT of 5000 against an estimate of 49k
frames past the head. Here a chain's estimate past its head is about 1.4k, below 5000, and
there the unfixed planner reads the chain with a bitmap and sorts it (another plan the fix
excludes), so the walk is driven at LIMIT 100, the same LIMIT-to-estimate proportion (about
7%, production's is 10%). The walk is the real function with a smaller budget, as the tick
gives it when its budget runs low.

Run:  .venv313/Scripts/python.exe -m pytest tests/test_frame_walk_plans.py -q
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

from apps.calls import chain_head as CH  # noqa: E402
from apps.calls import frame_audit as FA  # noqa: E402
from apps.calls.result_signing import ResultSigner  # noqa: E402
from apps.core import dbconn  # noqa: E402
from tests.fixtures import frames as F  # noqa: E402

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")

SIGNER = ResultSigner(private_key=Ed25519PrivateKey.from_private_bytes(F.SEED), kid=F.KID)

CHAIN_INDEX = "invocation_frames_chain_idx"
PKEY = "invocation_frames_pkey"
CHAINS, INTERLEAVED, STANDALONE = 6, 12000, 30000
MARK = "oi51-"

#: The three statements as they were at c4d2125 (origin/main before this change): the
#: positive controls, and the statements the result test compares with the new statements.
OLD_WALK = ("SELECT id, frame_hash, previous_frame_hash FROM invocation_frames "
            "WHERE chain_id = %s AND id > %s ORDER BY id LIMIT %s")
OLD_ROOT = ("SELECT id, frame_hash FROM invocation_frames WHERE chain_id = %s "
            "ORDER BY id ASC LIMIT 1")
OLD_PREDECESSOR = ("SELECT frame_hash FROM invocation_frames WHERE chain_id = %s "
                   "AND id < %s ORDER BY id DESC LIMIT 1")

#: How the interceptor recognizes each walk: the select list, the same before and after.
WALK_PREFIX = "SELECT id, frame_hash, previous_frame_hash FROM invocation_frames"
ROOT_PREFIX = "SELECT id, frame_hash FROM invocation_frames"
PREDECESSOR_PREFIX = "SELECT frame_hash FROM invocation_frames"


@pytest.fixture(scope="module")
def plant():
    """Production's shape, planted once for the module and removed after it (see the module
    docstring). Yields the planted chains, each as its frames' ids in order."""
    salt = uuid.uuid4().hex
    chains = [str(uuid.uuid4()) for _ in range(CHAINS)]
    with psycopg.connect(DSN, autocommit=True) as owner:
        floor = owner.execute("SELECT coalesce(max(id), 0) FROM invocation_frames").fetchone()[0]
        try:
            owner.execute(
                "INSERT INTO invocation_frames (chain_id, previous_frame_hash, parent_hash, "
                "frame_hash, decision, caller_owner_id, caller_peer_id, target_owner_id, "
                "target_peer_id, skill_name, call_id) "
                "SELECT c, lag(h) OVER w, CASE WHEN lag(h) OVER w IS NULL THEN NULL "
                "ELSE first_value(h) OVER w END, h, 'allow', o, o, o, o, 'memory.query', "
                "%s || g FROM (SELECT g, (%s::uuid[])[1 + (hashint4(g) & 2147483647) %% %s] "
                "AS c, md5(%s || g) || md5(g || %s) AS h "
                "FROM generate_series(1, %s) g) s, (SELECT gen_random_uuid() AS o) oo "
                "WINDOW w AS (PARTITION BY c ORDER BY g) ORDER BY g",
                (MARK, chains, CHAINS, salt, salt, INTERLEAVED))
            owner.execute(
                "INSERT INTO invocation_frames (frame_hash, decision, caller_owner_id, "
                "caller_peer_id, target_owner_id, target_peer_id, skill_name, call_id) "
                "SELECT md5(%s || g) || md5(g || %s), 'allow', o, o, o, o, 'memory.query', "
                "%s || 's' || g FROM generate_series(1, %s) g, "
                "(SELECT gen_random_uuid() AS o) oo ORDER BY g",
                ("s" + salt, salt, MARK, STANDALONE))
            owner.execute("SET default_statistics_target = 1000")
            owner.execute("ANALYZE invocation_frames")
            owner.execute("RESET default_statistics_target")
            ids = {c: [r[0] for r in owner.execute(
                "SELECT id FROM invocation_frames WHERE chain_id = %s AND id > %s ORDER BY id",
                (c, floor)).fetchall()] for c in chains}
            assert all(len(v) > 1000 for v in ids.values()), "control: every chain planted"
            yield ids
        finally:
            owner.execute("DELETE FROM invocation_chain_heads WHERE chain_id = ANY(%s::uuid[])",
                          (chains,))
            owner.execute("DELETE FROM invocation_frames WHERE id > %s AND call_id LIKE %s",
                          (floor, MARK + "%"))
            owner.execute("DELETE FROM background_job_cursors WHERE cursor_name LIKE 'oi51.%'")
            owner.execute("ANALYZE invocation_frames")


@pytest.fixture()
def app():
    c = dbconn.connect(DSN)
    try:
        yield c
    finally:
        c.close()


def _plan_nodes(plan: dict):
    yield plan
    for child in plan.get("Plans", []):
        yield from _plan_nodes(child)


def _explain(conn, query: str, params) -> dict:
    """The plan of `query` as `conn` would run it now: its role, its transaction's settings."""
    with psycopg.Cursor(conn) as cur:
        cur.execute("EXPLAIN (FORMAT JSON) " + query, params)
        return cur.fetchone()[0][0]["Plan"]


def _capture(conn, prefixes: dict[str, str]) -> dict[str, list[dict]]:
    """Make every cursor of `conn` record, before running it, each statement that starts with
    one of `prefixes` (label -> prefix): its plan, its text and parameters, and the
    `enable_sort` in force for it; then run it as it was given. The real function runs as it
    does in production, and what is recorded is read inside its own transaction."""
    seen: dict[str, list[dict]] = {label: [] for label in prefixes}

    class _Explain(psycopg.Cursor):
        def execute(self, query, params=None, **kw):
            if isinstance(query, str):
                for label, prefix in prefixes.items():
                    if query.startswith(prefix):
                        with psycopg.Cursor(self.connection) as cur:
                            cur.execute("SELECT current_setting('enable_sort')")
                            sort = cur.fetchone()[0]
                        seen[label].append({"plan": _explain(self.connection, query, params),
                                            "query": query, "params": tuple(params or ()),
                                            "enable_sort": sort})
            return super().execute(query, params, **kw)

    conn.cursor_factory = _Explain
    return seen


def _shape(plan: dict) -> list[tuple[str, str | None, str]]:
    return [(n["Node Type"], n.get("Index Name"), n.get("Filter", "")) for n in _plan_nodes(plan)]


def _is_production_failure(plan: dict) -> bool:
    """The plan production ran: the primary key, with the chain as a Filter."""
    return any(i == PKEY and "chain_id" in f for _, i, f in _shape(plan))


def _assert_chain_index_only(seen: dict, what: str) -> None:
    """An ordered Index Scan of the chain index; no primary key, no Seq Scan, no bitmap, no
    Sort: a read that starts at its bound and stops at its LIMIT. And sorts were off for the
    statement: the pin, in force where the statement runs, is what makes this plan the only
    one left rather than the cheapest one today."""
    shape = _shape(seen["plan"])
    assert ("Index Scan", CHAIN_INDEX) in [(t, i) for t, i, _ in shape], f"{what}: {shape}"
    assert not any(i == PKEY for _, i, _ in shape), f"{what}: the primary key: {shape}"
    assert not any(t in ("Seq Scan", "Bitmap Heap Scan", "Bitmap Index Scan", "Sort",
                         "Incremental Sort") for t, _, _ in shape), f"{what}: {shape}"
    assert seen["enable_sort"] == "off", (
        f"{what}: the sort pin is not in force: enable_sort={seen['enable_sort']}")


def test_oi51_the_chain_head_walk_reads_the_chain_index_as_meshy_app_with_nothing_disabled(
        plant, app):
    """The real `reconcile` on the application connection, twice.

    PAST AN ANCHORED HEAD, production's case: the chain is anchored at its last frame first
    (the real function again, from its root), so the measured walk has 30000 later ids and no
    frame of the chain among them. It is an ordered read of the chain index; it reads nothing,
    the verdict is CURRENT and the anchor does not move. CONTROL: the statement as it was, on
    the same connection, is planned as the primary key with a chain_id Filter.

    FROM THE ROOT at the production budget (5000), a chain the planner believes smaller than
    the LIMIT: the same ordered read, and the whole chain anchored. CONTROL: the function's own
    statement, planned outside its transaction (so without its sort pin), reads the chain with
    a bitmap and sorts it, a read of every frame the chain holds whatever the LIMIT: the pin,
    not the planner's estimate, is what keeps the walk to its LIMIT."""
    chain, ids = next(iter(plant.items()))
    first = CH.reconcile(app, chain, signer=SIGNER, keys=F.KEYS, limit=len(ids) + 1)
    assert (first.verdict, first.head_frame_id, first.frame_count) == (
        CH.ANCHORED, ids[-1], len(ids)), first

    control = _explain(app, OLD_WALK, (chain, ids[-1], 100))
    assert _is_production_failure(control), (
        f"control: the plant no longer reproduces production's plan: {_shape(control)}")

    plans = _capture(app, {"walk": WALK_PREFIX})
    o = CH.reconcile(app, chain, signer=SIGNER, keys=F.KEYS, limit=100)
    assert (o.verdict, o.frames_read, o.head_frame_id, o.frame_count) == (
        CH.CURRENT, 0, ids[-1], len(ids)), o
    assert len(plans["walk"]) == 1, plans
    _assert_chain_index_only(plans["walk"][0], "the walk past the head")

    fresh, fresh_ids = list(plant.items())[2]
    o = CH.reconcile(app, fresh, signer=SIGNER, keys=F.KEYS, limit=CH.FRAMES_PER_TICK)
    assert (o.verdict, o.frames_read, o.head_frame_id, o.frame_count) == (
        CH.ANCHORED, len(fresh_ids), fresh_ids[-1], len(fresh_ids)), o
    assert len(plans["walk"]) == 2, plans
    _assert_chain_index_only(plans["walk"][1], "the walk from the root")
    unpinned = _shape(_explain(app, plans["walk"][1]["query"], plans["walk"][1]["params"]))
    assert any(t in ("Sort", "Bitmap Heap Scan") for t, _, _ in unpinned), (
        f"control: without the pin the planner must pick the sorted read here: {unpinned}")


def test_oi51_the_audit_root_and_predecessor_reads_use_the_chain_index_as_meshy_app(plant, app):
    """The real `audit_tick` on the application connection, its cursors set so that each pass
    reads exactly one frame: the last frame of a planted chain, whose predecessor is not in the
    batch (one predecessor probe per pass) and whose chain has a root (one root probe, cached).
    Both probes are ordered reads of the chain index, with sorts off. CONTROL: the root read as
    it was is planned as the primary key, from the table's first id, with a chain_id Filter.

    Here the new statements' shape alone is enough: a LIMIT 1 probe gives a bitmap-and-sort
    read no cost advantage, and neither this table nor production's planner (the chains
    sampled for divergence 050) picked one unpinned. The pin makes the choice structural rather
    than a cost comparison, so it is held directly: `enable_sort` is off where each probe runs."""
    chain, ids = list(plant.items())[1]
    last = ids[-1]
    with psycopg.connect(DSN, autocommit=True) as owner:
        for name in (FA.CURSOR_NEW, FA.CURSOR_RECHECK):
            owner.execute("INSERT INTO background_job_cursors (cursor_name, position, laps) "
                          "VALUES (%s, %s, 0)", ("oi51.audit." + name, last - 1))

    control = _explain(app, OLD_ROOT, (chain,))
    assert _is_production_failure(control), (
        f"control: the plant no longer reproduces production's plan: {_shape(control)}")

    plans = _capture(app, {"root": ROOT_PREFIX, "predecessor": PREDECESSOR_PREFIX})
    r = FA.audit_tick(app, new_per_tick=1, recheck_per_tick=1, keys=F.KEYS,
                      cursor_prefix="oi51.audit.")
    assert (r.frames_new, r.frames_rechecked, r.new_position) == (1, 1, last), r
    assert len(plans["root"]) == 1 and len(plans["predecessor"]) == 2, plans
    _assert_chain_index_only(plans["root"][0], "the root read")
    for p in plans["predecessor"]:
        _assert_chain_index_only(p, "the predecessor read")


def _old(cur, query, params) -> list[dict]:
    cur.execute(query, params)
    return cur.fetchall()


def test_oi51_the_walks_return_the_same_rows_in_the_same_order_as_before(plant, app):
    """Byte for byte the rows and order of the statements as they were, on the planted chains
    (interleaved with each other and with standalone frames), on two chains whose ids are the
    neighbours of a planted one (uuid + 1 and - 1: the range `chain_id >= %s AND chain_id <= %s`
    is that chain and no other), and on a chain with no frame. Every head from before the root
    to past the last frame, every LIMIT from 1 to more than the chain holds."""
    chains = list(plant)
    x = uuid.UUID(chains[0])
    neighbours = [str(uuid.UUID(int=x.int + 1)), str(uuid.UUID(int=x.int - 1))]
    with psycopg.connect(DSN, autocommit=True) as owner:
        for n in neighbours:
            F.chain(owner, 3, chain_id=n, call_id=MARK + "neighbour")
        near = {n: [r[0] for r in owner.execute(
            "SELECT id FROM invocation_frames WHERE chain_id = %s ORDER BY id", (n,))]
            for n in neighbours}
    cases = {c: plant[c] for c in chains[:2]} | near | {str(uuid.uuid4()): []}
    compared = 0
    with app.transaction(), app.cursor(row_factory=dict_row) as cur:
        chains_probe = FA._Chains(cur)
        for chain, ids in cases.items():
            heads = sorted({0, *(ids[i] for i in (0, 1, len(ids) // 2, -2, -1) if ids),
                            *(i + 1 for i in ids[-1:])})
            for head in heads:
                for limit in (1, 7, 100, len(ids) + 5):
                    want = _old(cur, OLD_WALK, (chain, head, limit))
                    assert CH.frames_past(cur, chain, head, limit) == want, (chain, head, limit)
                    compared += bool(want)
            for fid in ids[:3] + ids[len(ids) // 2:len(ids) // 2 + 3] + ids[-3:]:
                want = _old(cur, OLD_PREDECESSOR, (chain, fid))
                got = chains_probe.predecessor_hash(chain, fid)
                assert got == (want[0]["frame_hash"] if want else None), (chain, fid)
            if ids:
                (want,) = _old(cur, OLD_ROOT, (chain,))
                assert chains_probe.root(chain) == (want["id"], want["frame_hash"]) == (
                    ids[0], want["frame_hash"])
    assert compared > 40, "control: the comparison saw rows, not only empty answers"
