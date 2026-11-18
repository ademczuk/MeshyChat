"""The rerank pool (apps/meshsem/search._pool): which fused candidates the reranker sees,
and which the no-rerank path returns. Divergence 026, as amended by the pool guarantee.
No database and no model: `_pool` runs against a stand-in connection that answers its one
query (search._CANDIDATES) from a dict, the way shared_memory would.

THE DEFECT (measured on the node-test server at 388f28e, 128 messages): the embedding
arm's first place for a paraphrase query, ranked by no lexical term, fused at rank 42.
Reciprocal-rank fusion scores by rank alone, so every row BOTH arms rank (1/(60+i) +
1/(60+j)) outranks a row one arm ranks first (1/61) however weak its other match; the
pool was the first POOL rows of the fused order, so the reranker never saw the embedder's
best answer, and k is capped at POOL, so no request could return it.

THE RULE these tests hold `_pool` to: at most POOL live candidates, in fused order, that
always include the first ARM_GUARANTEE live candidates of EACH arm's own ranking (a row
the database does not return, the anchor, or a row of the anchor's thread is skipped,
not counted); the other slots are filled in fused order. So the no-rerank path's first
k rows are unchanged for every k up to the unchanged prefix (never shorter than
POOL - 2 * ARM_GUARANTEE), every row after the first change is a reserved row, and with
nothing added the pool is exactly what it was.

Run:  pytest tests/test_meshsem_pool.py -q
"""
from __future__ import annotations

import datetime as _dt
import os
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

from apps.memoryskills.validation import MESH_CHAT_NAMESPACE  # noqa: E402
from apps.meshsem import fusion, search  # noqa: E402

L, E = fusion.LEXICAL, fusion.EMBED
ROOT = Path(__file__).resolve().parent.parent
#: The design's value, written here and NOT read from the code, so a test that uses it is
#: a check on the code rather than a restatement of it (test_the_guarantee_is_ten_per_arm).
GUARANTEE = 10
T0 = _dt.datetime(2026, 9, 23, tzinfo=_dt.timezone.utc)


def _r(arm, ids):
    ids = list(ids)
    return fusion.Ranking(arm, ids, [1.0 - i / 10_000 for i in range(len(ids))])


class FakeDB:
    """shared_memory as search._CANDIDATES sees it. `rows` maps a src_id to its thread id
    (None: no thread); a src_id NOT in `rows` is one the query does not return: deleted,
    superseded (not the head) or behind the embed_policy gate. Every query is recorded."""

    def __init__(self, rows: dict[str, str | None]):
        self.rows = rows
        self.asked: list[list[str]] = []

    def cursor(self, row_factory=None):
        return _Cursor(self)


class _Cursor:
    def __init__(self, db: FakeDB):
        self.db, self.result = db, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params):
        assert sql == search._CANDIDATES, "the pool asked something other than its query"
        assert params["ns"] == MESH_CHAT_NAMESPACE and "open" in params
        ids = list(params["ids"])
        self.db.asked.append(ids)
        self.result = [{"id": sid, "key": f"msg/{sid}", "version": 1, "created_at": T0,
                        "author_label": "a", "body": f"body of {sid}",
                        "thread_id": self.db.rows[sid]}
                       for sid in ids if sid in self.db.rows]

    def fetchall(self):
        return self.result


def _call(fused, db, *, exclude_ids=(), exclude_thread=None):
    return search._pool(db, fused, gate=False, exclude_ids=set(exclude_ids),
                        exclude_thread=exclude_thread)


def _keys(fused, db, **kw) -> list[str]:
    return [c.src_id for c in _call(fused, db, **kw)[0]]


def _everyone(fused, thread=None) -> FakeDB:
    return FakeDB({f.src_id: thread for f in fused})


# ============================================================ the defect, reproduced

def test_the_guarantee_is_ten_per_arm_and_leaves_most_of_the_pool_to_the_fusion():
    assert search.ARM_GUARANTEE == GUARANTEE
    assert search.POOL == 40
    assert len(fusion.ARMS) * search.ARM_GUARANTEE <= search.POOL - search.DEFAULT_K
    # One arm's whole ranking lies inside the fused walk, so with one arm every reserved
    # row is already among the first POOL live rows: one arm adds nothing.
    assert search.ARM_DEPTH <= search.EXAMINE_MAX


def test_an_embed_only_first_place_enters_the_pool_however_many_rows_both_arms_ranked():
    """45 rows ranked by BOTH arms, and a target the embedding arm ranks FIRST that no
    lexical term matched. Every both-ranked row fuses above 1/61, so the target's fused
    rank is 46, outside POOL: on origin/main it was not in the pool. Control: that fused
    rank, computed by the real fusion. The guarantee adds it without an extra round trip
    to the database, and the pool is still POOL rows in fused order."""
    both = [f"b{i:02d}" for i in range(45)]
    fused = fusion.rrf([_r(L, both), _r(E, ["target"] + both)])
    order = [f.src_id for f in fused]
    assert order.index("target") + 1 == 46 > search.POOL, "control: crowded out by fusion"
    db = _everyone(fused)
    pool = _keys(fused, db)
    assert "target" in pool, "the embedding arm's first place never reaches the reranker"
    assert len(pool) == search.POOL
    assert pool == sorted(pool, key=order.index), "the pool is in fused order"
    assert pool == both[:search.POOL - 1] + ["target"], "only the tail changed"
    assert len(db.asked) == 1, "the guarantee cost a round trip it did not need"


def test_the_measured_shape_fused_rank_42_of_128_is_in_the_pool():
    """The live measurement's shape: 128 messages, the lexical arm ranks 42 of them (41 of
    them also near the top of the embedding arm, one only weakly), the embedding arm ranks
    all 128 with the target first, and the target fuses at rank 42."""
    d = [f"d{i:02d}" for i in range(1, 43)]
    others = [f"o{i:02d}" for i in range(85)]
    lexical = _r(L, d)
    embed = _r(E, ["target"] + d[:41] + others + [d[41]])
    assert len(set(embed.ids)) == 128
    fused = fusion.rrf([lexical, embed])
    assert [f.src_id for f in fused].index("target") + 1 == 42, "control: the measured rank"
    assert "target" in _keys(fused, _everyone(fused))


def test_the_context_pool_keeps_the_embed_arms_best_row_past_the_anchors_thread():
    """The context route's pool excludes the anchor and its thread, which are exactly what
    both arms rank first for an anchor's own text. Past them, the embedding arm's first
    LIVE row is `target`, and 45 other-thread rows ranked by both arms fuse above it."""
    thread = ["anchor"] + [f"m{i}" for i in range(5)]
    both = [f"b{i:02d}" for i in range(45)]
    fused = fusion.rrf([_r(L, thread + both), _r(E, thread + ["target"] + both)])
    db = FakeDB({**{s: "T" for s in thread}, **{b: f"t-{b}" for b in both},
                 "target": "t-target"})
    kw = {"exclude_ids": {"anchor"}, "exclude_thread": "T"}
    live_order = [f.src_id for f in fused if f.src_id not in thread]
    assert live_order.index("target") + 1 == 46, "control: outside the pool by fusion"
    pool = _keys(fused, db, **kw)
    assert "target" in pool
    assert not set(thread) & set(pool), "the anchor's thread entered the context pool"
    assert pool == both[:search.POOL - 1] + ["target"]
    assert _call(fused, db, **kw)[2] == {L: 0, E: 1}


# ======================================================= non-live rows are not counted

def _crowded_with(arm: str, head: list[str], tail: list[str]):
    """45 rows both arms rank, and `head` then `tail` ranked by `arm` ALONE, first. Every
    both-ranked row fuses above every single-arm row, so the single-arm rows are all
    outside the fused top POOL."""
    both = [f"b{i:02d}" for i in range(45)]
    other = L if arm == E else E
    single = head + tail
    fused = fusion.rrf([_r(arm, single + both), _r(other, both)])
    assert all(f.src_id in both for f in fused[:45]), "control: the crowding"
    return fused, both


@pytest.mark.parametrize("arm", [L, E])
def test_non_live_rows_in_an_arms_top_ten_are_skipped_not_counted_and_never_pooled(arm):
    """The arm's first five rows are each not live: s01 and s02 are rows the database
    does not return (deleted, superseded or behind the gate), s03 is the anchor, and s04
    and s05 are in the anchor's thread. The guarantee walks past them to the arm's first
    TEN live rows, s06..s15, and stops there: s16 is live and not reserved. Kills an
    off-by-one either way, a non-live row counted toward the ten, and a non-live row
    placed in the pool."""
    dead = ["s01", "s02", "s03", "s04", "s05"]
    tail = [f"s{i:02d}" for i in range(6, 17)]
    fused, both = _crowded_with(arm, dead, tail)
    rows = {s: None for s in tail + both}
    rows.update({"s03": None, "s04": "T", "s05": "T"})       # s01, s02: not returned
    db = FakeDB(rows)
    got = _call(fused, db, exclude_ids={"s03"}, exclude_thread="T")
    pool = [c.src_id for c in got[0]]
    assert not set(dead) & set(pool), "a non-live row entered the pool"
    assert set(tail[:GUARANTEE]) <= set(pool), "the arm's first ten live rows are reserved"
    assert "s16" not in pool, "the eleventh live row was reserved too"
    assert len(pool) == search.POOL
    assert pool == both[:search.POOL - GUARANTEE] + tail[:GUARANTEE]
    assert got[2] == {arm: GUARANTEE, (L if arm == E else E): 0}


@pytest.mark.parametrize("arm", [L, E])
def test_the_anchor_and_its_thread_are_excluded_where_only_the_arm_walk_reaches_them(arm):
    """The context route at the real constants, with the anchor and its thread reached
    ONLY by the arm walk: `arm` ranks 400 rows that are not live, then the anchor and
    five rows of its thread, then other-thread rows; the other arm ranks 60 live rows,
    which the fused walk finds in its first EXAMINE_FIRST candidates. The anchor's row
    has no thread (not yet projected), so only `exclude_ids` keeps it out, and only
    `exclude_thread` keeps out the five. Controls: the fused walk never looked up the
    anchor or its thread, and the arm walk did. The arm's first ten LIVE rows are then
    o000..o009, and neither the anchor nor a row of its thread may enter the pool.
    Kills: the arm walk's lookups skipping either exclusion (review mutant A7 skips
    both), which every hand-worked case missed because none had more than
    EXAMINE_FIRST fused rows."""
    other = E if arm == L else L
    live_other = [f"b{i:02d}" for i in range(60)]
    dead = [f"dead{i:03d}" for i in range(400)]
    thread = ["anchor"] + [f"m{i}" for i in range(5)]
    tail = [f"o{i:03d}" for i in range(15)]
    fused = fusion.rrf([_r(other, live_other), _r(arm, dead + thread + tail)])
    db = FakeDB({**{s: None for s in live_other}, **{s: "T" for s in thread[1:]},
                 "anchor": None, **{s: f"t-{s}" for s in tail}})
    pool, examined, guaranteed = _call(fused, db, exclude_ids={"anchor"}, exclude_thread="T")
    assert not set(thread) & set(db.asked[0]), "control: the fused walk looked them up"
    assert set(thread) <= {s for q in db.asked[1:] for s in q}, "control: the arm walk did not"
    keys = [c.src_id for c in pool]
    assert not set(thread) & set(keys), "the anchor's thread entered the context pool"
    assert keys == live_other[:search.POOL - GUARANTEE] + tail[:GUARANTEE]
    assert guaranteed == {other: 0, arm: GUARANTEE}


# ======================================================== pool.guaranteed is exact

def test_guaranteed_counts_only_rows_the_fused_top_pool_did_not_already_hold():
    """Five cases, each count worked by hand: nothing added; one row added by one arm;
    ten by each arm; a reservation partly inside the fused top POOL (only the rows
    outside it count); and one arm alone, which the fused order already is."""
    both = [f"b{i:02d}" for i in range(45)]

    small = fusion.rrf([_r(L, "abc"), _r(E, "cba")])
    assert _call(small, _everyone(small))[2] == {L: 0, E: 0}

    one = fusion.rrf([_r(L, both), _r(E, ["t"] + both)])
    assert _call(one, _everyone(one))[2] == {L: 0, E: 1}

    lex_only = [f"l{i:02d}" for i in range(12)]
    emb_only = [f"e{i:02d}" for i in range(12)]
    two = fusion.rrf([_r(L, lex_only + both), _r(E, emb_only + both)])
    got = _call(two, _everyone(two))
    pool, guaranteed = [c.src_id for c in got[0]], got[2]
    assert guaranteed == {L: 10, E: 10}
    assert pool == both[:20] + sorted(lex_only[:10] + emb_only[:10],
                                      key=[f.src_id for f in two].index)

    # The embedding arm's first ten are three rows the fused top 40 holds (b00..b02, which
    # it ranks first) and seven it does not (e00..e06).
    part = fusion.rrf([_r(L, both), _r(E, both[:3] + emb_only[:7] + both[3:])])
    got = _call(part, _everyone(part))
    pool, guaranteed = [c.src_id for c in got[0]], got[2]
    assert guaranteed == {L: 0, E: 7}
    assert set(emb_only[:7]) <= set(pool) and set(both[:3]) <= set(pool)

    alone = fusion.rrf([_r(E, ["t"] + both)])
    got = _call(alone, _everyone(alone))
    pool, guaranteed = [c.src_id for c in got[0]], got[2]
    assert guaranteed == {L: 0, E: 0} and pool == (["t"] + both)[:search.POOL]


# ======================================================= what the reservation costs

def test_the_reservation_costs_no_query_when_the_fused_walk_already_looked_its_rows():
    """The usual shape: every row live, each arm ranking 2000 of 3000 documents (the
    serving depth), so the fusion ranks thousands. Each arm's first ten rows fuse well
    inside the first EXAMINE_FIRST candidates, which the fused walk looks up in ONE
    query, so the reservation must add no query and no lookup. Control: both arms rank
    rows past the first EXAMINE_FIRST fused candidates, so a reservation that looked up
    more of its ranking than it needs would have something to look up. Kills: the arm
    walk looking up its arm's whole ranking on every request (review mutant A3)."""
    rng = random.Random(20260923)
    docs = [f"d{i:04d}" for i in range(3000)]
    lex, emb = rng.sample(docs, 2000), rng.sample(docs, 2000)
    fused = fusion.rrf([_r(L, lex), _r(E, emb)])
    first = {f.src_id for f in fused[:search.EXAMINE_FIRST]}
    assert len(fused) > search.EXAMINE_FIRST, "control: more than one chunk fused"
    assert set(lex) - first and set(emb) - first, "control: the arms reach past it"
    db = _everyone(fused)
    pool, examined, guaranteed = _call(fused, db)
    assert db.asked == [[f.src_id for f in fused[:search.EXAMINE_FIRST]]], \
        "the reservation cost a query the fused walk had already made"
    assert examined == search.EXAMINE_FIRST
    assert guaranteed == {L: 0, E: 0} and len(pool) == search.POOL


def test_an_arm_walk_past_the_fused_walk_reads_its_ranking_in_arm_chunk_lookups():
    """The embedding arm's first 700 rows are not live and its next 500 are; the lexical
    arm ranks 60 live rows. The fused walk finds its 40 in the first EXAMINE_FIRST
    candidates, which hold only 340 of the dead rows, so the embedding arm's reservation
    must walk 370 rows the fused walk never looked up (the other 360 dead rows, then its
    first ten live ones). It must read them IN THE ARM'S ORDER, from the first row it had
    not looked up, at most ARM_CHUNK rows a query and at most ceil(370 / ARM_CHUNK)
    queries, and stop reading once it has ten. Kills: looking rows up one at a time
    (review mutant A2), looking up the arm's whole ranking (A3), and a chunk that skips
    the row being examined (A11)."""
    live_lex = [f"b{i:02d}" for i in range(60)]
    dead = [f"dead{i:03d}" for i in range(700)]
    live_emb = [f"o{i:03d}" for i in range(500)]
    emb = dead + live_emb
    fused = fusion.rrf([_r(L, live_lex), _r(E, emb)])
    db = FakeDB({s: None for s in live_lex + live_emb})
    pool, examined, guaranteed = _call(fused, db)
    fused_q, arm_q = db.asked[0], db.asked[1:]
    assert fused_q == [f.src_id for f in fused[:search.EXAMINE_FIRST]]
    walked = emb[emb.index(next(s for s in emb if s not in fused_q)):emb.index("o009") + 1]
    assert len(walked) == 370 and not set(walked) & set(fused_q), "control: the walk"
    assert 1 <= len(arm_q) <= -(-len(walked) // search.ARM_CHUNK), \
        f"{len(arm_q)} queries for {len(walked)} rows"
    assert all(len(q) <= search.ARM_CHUNK for q in arm_q), [len(q) for q in arm_q]
    read = [s for q in arm_q for s in q]
    assert read[:len(walked)] == walked, "the arm walk read out of the arm's order"
    assert examined == len(fused_q) + len(read) < len(fused_q) + len(walked) + search.ARM_CHUNK
    assert guaranteed == {L: 0, E: GUARANTEE}
    assert [c.src_id for c in pool] == live_lex[:search.POOL - GUARANTEE] + live_emb[:GUARANTEE]


def test_the_worst_case_cost_is_bounded_and_buys_rows_the_old_pool_could_not_find():
    """The worst case, at the real constants: a context anchor whose own thread fills
    both arms' rankings (ARM_DEPTH rows each, disjoint), with a few other-thread rows at
    the bottom of each. Nothing live lies in the fused walk's reach, so the old pool was
    EMPTY (control), and each arm's walk must read the rest of its ranking. The cost is
    bounded, not a cliff: every lookup at most ARM_CHUNK rows, so at most
    2 + 2 * ceil(ARM_DEPTH / ARM_CHUNK) queries over at most 2 * ARM_DEPTH candidates
    (`examined` then exceeds EXAMINE_MAX, as its comment says). On this shape exactly:
    the fused walk's two chunks cover each arm's first 1000 rows (the arms interleave
    by rank), and each arm's other 1000 take ceil(1000 / ARM_CHUNK) lookups. For that,
    the pool holds ten other-thread rows of each arm where the old pool held none."""
    n, few = search.ARM_DEPTH, 12
    lex = [f"t{i:04d}" for i in range(n - few)] + [f"x{i:02d}" for i in range(few)]
    emb = [f"u{i:04d}" for i in range(n - few)] + [f"y{i:02d}" for i in range(few)]
    fused = fusion.rrf([_r(L, lex), _r(E, emb)])
    rows = {s: "T" for s in lex[:-few] + emb[:-few]}
    rows.update({s: f"t-{s}" for s in lex[-few:] + emb[-few:]})
    db = FakeDB(rows)
    pool, examined, guaranteed = _call(fused, db, exclude_ids={lex[0]}, exclude_thread="T")
    live = {s for s, t in rows.items() if t != "T"}
    assert _old_pool(fused, live) == [], "control: the old pool found nothing"
    ceil = lambda a, b: -(-a // b)                                     # noqa: E731
    assert len(db.asked) <= 2 + 2 * ceil(search.ARM_DEPTH, search.ARM_CHUNK)
    assert examined <= len(fused) <= 2 * search.ARM_DEPTH
    assert len(db.asked) == 2 + 2 * ceil(n // 2, search.ARM_CHUNK), [len(q) for q in db.asked]
    assert examined == len(fused) == 2 * n
    keys = [c.src_id for c in pool]
    assert keys == sorted(lex[-few:][:GUARANTEE] + emb[-few:][:GUARANTEE],
                          key=[f.src_id for f in fused].index)
    assert guaranteed == {L: GUARANTEE, E: GUARANTEE}


# ========================================================= the rule, where it is written

def _flat(text: str) -> str:
    return " ".join(text.split())


def test_the_docs_state_the_pool_rule_and_its_evidence():
    """The rule is written where the ranking is (divergence 026's amendment, the meshsem
    README, search.py's docstring), with the measurement behind it, what it cannot
    promise, and the field it adds to the envelope. The README's old sentence, "the
    reranker over the fused top 40", must be GONE, not merely joined by the new one, and
    so must the amendment's retracted claim that "unchanged for every k" cannot hold.
    Kills: any of them reverted, or the amendment losing its evidence or its caveat."""
    doc = (ROOT / "docs" / "DIVERGENCES.md").read_text(encoding="utf-8")
    entry = _flat(doc[doc.index("\n## 026,"):doc.index("\n## 027,")])
    for phrase in ("the rerank pool reserves each arm's first 10 live rows",
                   "(cosine 0.631)", "The fusion put it at rank 42, outside the pool of 40",
                   "31 of the 40 pool members had a lexical cosine under 0.05",
                   "never before position 20", "`guaranteed`",
                   "that is a CHOICE, not a necessity",
                   "would meet it literally", "Which one to keep is the designer's call",
                   "this is one query on one small corpus",
                   "Mutation: 10 mutants of the new code, 10 killed",
                   "at most 2 + 2 x ceil(`ARM_DEPTH` / `ARM_CHUNK`) = 22 queries over at most "
                   "`fused` <= 2 x `ARM_DEPTH` = 4000 candidates",
                   "the walks made 12 queries over 4000 candidates",
                   "An adversarial review then applied 11 mutants of its own",
                   "13 mutants, 13 killed by `tests/test_meshsem_pool.py` alone"):
        assert phrase in entry, phrase
    # The review showed the literal property CAN hold (serve rerank=0 from the fused top
    # POOL): the claim that it cannot must be gone, not merely contradicted.
    assert "cannot hold literally" not in entry, "the retracted impossibility claim"
    readme = _flat((ROOT / "apps" / "meshsem" / "README.md").read_text(encoding="utf-8"))
    assert "Then the reranker over the fused top 40," not in readme, "the old rule"
    assert "it always holds each arm's first 10 LIVE rows" in readme
    assert "`guaranteed` (`{\"lexical\": n, \"embed\": n}`" in readme
    # Where the envelope's fields are listed, a caller must learn the counts are per arm.
    assert "a row both arms reserve counts under each, so the two can sum to more" in readme
    code = _flat(search.__doc__)
    assert "ALWAYS include each arm's first ARM_GUARANTEE = 10 live rows" in code
    assert "`pool.guaranteed`" in code


# ========================================================== the property, randomised

def _old_pool(fused, live: set[str]) -> list[str]:
    """origin/main's pool, written again: the first POOL live candidates in fused order,
    looking no further than EXAMINE_MAX."""
    out = []
    for f in fused[:search.EXAMINE_MAX]:
        if f.src_id in live:
            out.append(f.src_id)
            if len(out) == search.POOL:
                break
    return out


def _reserved(ranking: fusion.Ranking, live: set[str]) -> list[str]:
    """An arm's first GUARANTEE live rows, read from the arm's OWN ranking (not from the
    fused list the code derives it from)."""
    return [s for s in ranking.ids if s in live][:GUARANTEE]


def _case(rng: random.Random):
    n = rng.randint(1, 160)
    docs = [f"d{i:03d}" for i in range(n)]
    shape = rng.random()
    arms = [L, E] if shape < 0.8 else ([L] if shape < 0.9 else [E])
    ids = {arm: rng.sample(docs, rng.randint(0, n)) for arm in arms}
    if len(arms) == 2:
        for arm, other in ((L, E), (E, L)):
            if rng.random() < 0.5:
                # Crowding on purpose: rows only this arm ranks, put first, where the
                # rows both arms rank can push them out of the fused top POOL.
                theirs = set(ids[other])
                head = [d for d in ids[arm] if d not in theirs][:rng.randint(0, 15)]
                ids[arm] = head + [d for d in ids[arm] if d not in head]
    rankings = [_r(arm, ids[arm]) for arm in arms]
    p_live = rng.choice([1.0, 0.9, 0.6, 0.3])
    rows = {d: (None if rng.random() > 0.2 else "T") for d in docs if rng.random() < p_live}
    use_thread = rng.random() < 0.3
    exclude_ids = {rng.choice(docs)} if use_thread else set()
    return rankings, rows, exclude_ids, ("T" if use_thread else None)


def test_the_pool_property_on_seeded_random_rankings(monkeypatch):
    """600 seeded cases (random corpus size, arm coverage, order, liveness, an anchor and
    its thread excluded or not, and in a third of them small EXAMINE_FIRST/EXAMINE_MAX so
    the walk's limits bite). For each, against an independent re-implementation of the
    old pool and of each arm's reservation:
      * at most POOL rows, distinct, all live, in fused order, and exactly
        min(POOL, |old pool U reserved|) of them (the free slots are filled);
      * every arm's first GUARANTEE live rows are present;
      * THE NO-RERANK ORDER: the new pool equals the old one up to its first difference,
        and every row from there on is a reserved row; so the first k rows are identical
        for every k <= min(len(old), POOL - 2 * GUARANTEE), and for EVERY k when no arm
        added a row (then the pool is exactly the old pool);
      * with one arm, nothing is added and the pool is the old pool, wherever the
        fused walk reaches the arm's whole ranking (always, unpatched: ARM_DEPTH <=
        EXAMINE_MAX; with a patched EXAMINE_MAX the reservation may reach past it);
      * `guaranteed` counts, per arm, its reserved rows the old pool did not hold;
      * no candidate is looked up twice, and `examined` is how many were looked up;
      * the cost: the fused walk's chunks first, then arm-walk lookups of at most
        ARM_CHUNK rows each and at most ceil(len(ranking) / ARM_CHUNK) per arm.
    Controls: enough cases add rows, skip non-live reserved rows, and fill the free slots
    that the property is not vacuous."""
    rng = random.Random(20260923)
    seen = {L: 0, E: 0, "both_arms": 0, "skipped_in_top": 0, "one_arm": 0,
            "walk_limited": 0, "arm_queries": 0}
    for n in range(600):
        with monkeypatch.context() as m:
            if n % 3 == 0:
                first = rng.randint(1, 25)
                m.setattr(search, "EXAMINE_FIRST", first)
                m.setattr(search, "EXAMINE_MAX", first + rng.randint(0, 40))
                m.setattr(search, "ARM_CHUNK", rng.randint(1, 30), raising=False)
                seen["walk_limited"] += 1
            rankings, rows, exclude_ids, thread = _case(rng)
            fused = fusion.rrf(rankings)
            db = FakeDB(rows)
            got = _call(fused, db, exclude_ids=exclude_ids, exclude_thread=thread)
            pool, examined = got[0], got[1]
            new = [c.src_id for c in pool]
            live = {s for s, t in rows.items()
                    if s not in exclude_ids and not (thread is not None and t == thread)}
            old = _old_pool(fused, live)
            reserved = {r.arm: _reserved(r, live) for r in rankings}
            must = set().union(*reserved.values())
            pos = {f.src_id: i for i, f in enumerate(fused)}
            ctx = (n, len(fused), len(old), sorted(must - set(old)))

            assert len(new) <= search.POOL and len(set(new)) == len(new), ctx
            assert set(new) <= live, ctx
            assert [pos[s] for s in new] == sorted(pos[s] for s in new), ctx
            assert must <= set(new), ctx
            assert len(new) == min(search.POOL, len(set(old) | must)), ctx

            d = next((i for i, (a, b) in enumerate(zip(new, old)) if a != b),
                     min(len(new), len(old)))
            assert new[:d] == old[:d] and set(new[d:]) <= must, ctx
            floor = min(len(old), search.POOL - 2 * GUARANTEE)
            ranked, _ = search._rerank("q", pool, False)       # the no-rerank path
            for k in range(1, floor + 1):
                assert [c.src_id for c in ranked[:k]] == old[:k], (ctx, k)

            added = {arm: sum(1 for s in reserved.get(arm, ()) if s not in set(old))
                     for arm in fusion.ARMS}
            if not any(added.values()):
                assert new == old, ctx
            if len(rankings) == 1 and len(fused) <= search.EXAMINE_MAX:
                assert new == old, ctx
                seen["one_arm"] += 1

            flat = [s for q in db.asked for s in q]
            assert len(flat) == len(set(flat)), (ctx, "a candidate was looked up twice")
            assert examined == len(flat), ctx
            # THE COST: the fused walk's one or two chunks, and after them only the arm
            # walks' lookups, each at most ARM_CHUNK rows and at most
            # ceil(len(ranking) / ARM_CHUNK) of them per arm.
            first = fused[:search.EXAMINE_FIRST]
            walks = [first] if first else []
            if first and sum(f.src_id in live for f in first) < search.POOL:
                rest = fused[search.EXAMINE_FIRST:search.EXAMINE_MAX]
                walks += [rest] if rest else []
            assert db.asked[:len(walks)] == [[f.src_id for f in w] for w in walks], ctx
            arm_q = db.asked[len(walks):]
            assert all(len(q) <= search.ARM_CHUNK for q in arm_q), (ctx, "an arm chunk")
            assert len(arm_q) <= sum(-(-len(r.ids) // search.ARM_CHUNK) for r in rankings), \
                (ctx, "an arm walk looked its rows up in more queries than chunks")
            seen["arm_queries"] += bool(arm_q)
            assert got[2] == added, (ctx, got[2], added)

            for arm in fusion.ARMS:
                seen[arm] += added[arm] > 0
            seen["both_arms"] += all(added.values())
            for r in rankings:           # a non-live row the reservation had to walk past
                last = r.ids.index(reserved[r.arm][-1]) if reserved[r.arm] else -1
                if any(s not in live for s in r.ids[:last + 1]):
                    seen["skipped_in_top"] += 1
                    break
    assert seen[L] >= 60 and seen[E] >= 60 and seen["both_arms"] >= 40, seen
    assert seen["skipped_in_top"] >= 150, seen
    assert seen["one_arm"] >= 40 and seen["walk_limited"] == 200, seen
    assert seen["arm_queries"] >= 60, seen
