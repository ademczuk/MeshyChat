"""The resident view of the mail index (pxx's design A, option (b), on <thread>: msg e00a09 and
d4bf7c, 7 Nov 2026). DB-free: a fake connection answers each statement from a table held in the test and records the
SQL, so a test can say which reads ran.

What these pin:
- UNSET (the default) is today's path: the same statements, the same ranking, and no read of a stored vector
  (pxx's condition 4);
- a view holds `resident` floats per row, sliced in SQL, so a 256-d view of 768-d rows reads a third of the bytes;
- over a view, the embed arm's first N are rescored at full width in ONE read by primary key. Their order and scores
  are the full-width index's, a missing or malformed stored row drops out, and ties keep the view's order;
- a pool member past N gets its full-width cosine from at most one more read (condition 2);
- vector_of is None on a view, an undeclared width is refused, and only the mail index takes the setting.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import os
import re
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")

import django  # noqa: E402

django.setup()

from apps.meshsem import fusion, graphsearch, search, spaces  # noqa: E402

SPACE = spaces.get(spaces.DEFAULT_SPACE)
T0 = dt.datetime(2026, 11, 7, 12, 0, tzinfo=dt.timezone.utc)
ID = "00000000-0000-0000-0000-{:012d}".format
STORED = "SELECT e.src_id::text AS src_id, e.vec FROM unnest("


class _Cur:
    def __init__(self, db):
        self.db, self._rows = db, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        flat = " ".join(sql.split())
        self.db.log.append(flat)
        rows = self.db.rows
        if "current_setting(" in flat:                         # fullload saving the request's timeout
            self._rows = [{"t": "30s"}]
            return
        if "set_config(" in flat:                              # fullload setting or restoring it
            self._rows = [{"set_config": params[0] if params else ""}]
            return
        if flat.startswith(STORED):                            # search.stored_vectors, by primary key
            want = set(params[0])
            self._rows = [{"src_id": r["src_id"], "vec": r["vec"]} for r in rows if r["src_id"] in want]
            return
        if flat.startswith("SELECT vec FROM mesh_embeddings WHERE src_id = %s"):   # related's own fallback, a tuple row
            self._rows = [(r["vec"],) for r in rows if r["src_id"] == str(params[0])]
            return
        if "AS src_id, embedded_at FROM" in flat:              # the reconcile's live pairs
            self._rows = [{"src_id": r["src_id"], "embedded_at": r["at"]} for r in rows]
            return
        cut = re.search(r"substring\(vec from 1 for (\d+)\) AS vec", flat)
        if "= ANY(" in flat:                                   # the reconcile's load by id
            want = set(params[3])
            got = sorted([r for r in rows if r["src_id"] in want], key=lambda r: (r["at"], r["src_id"]))
        elif "DESC" in flat:                                   # the full load, newest first
            got = sorted(rows, key=lambda r: (r["at"], r["src_id"]), reverse=True)[:params[-1]]
        else:                                                  # the incremental window
            got = sorted(rows, key=lambda r: (r["at"], r["src_id"]))
        self._rows = [{"src_id": r["src_id"], "embedded_at": r["at"],
                       "vec": r["vec"][:int(cut.group(1))] if cut else r["vec"]} for r in got]

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _DB:
    def __init__(self):
        self.rows: list[dict] = []
        self.log: list[str] = []

    def cursor(self, row_factory=None):
        return _Cur(self)

    def add(self, i: int, vec: np.ndarray, *, raw: bytes | None = None):
        self.rows.append({"src_id": ID(i), "at": T0 + dt.timedelta(seconds=i),
                          "vec": raw if raw is not None else spaces.encode(vec)})

    def stored_reads(self) -> list[str]:
        return [s for s in self.log if s.startswith(STORED)]


@pytest.fixture()
def world():
    """60 rows of random 768-d vectors, and a query."""
    rng = np.random.default_rng(11)
    db = _DB()
    for i in range(60):
        db.add(i, rng.standard_normal(SPACE.dims).astype(np.float32))
    return db, rng.standard_normal(SPACE.dims).astype(np.float32)


def _loaded(db, resident=None):
    idx = search.VectorIndex(SPACE, resident=resident)
    idx.refresh(db, ceiling=1000)
    return idx


def _read(db, idx):
    calls = []

    def read(ids):
        calls.append(list(ids))
        return search.stored_vectors(db, idx, ids)
    return read, calls


def _boom(ids):
    raise AssertionError("the unset path read a stored vector")


def _cand(src_id, cosine, embed_rank, lexical_rank=1):
    """A pool member as _pool builds it: fused_score is fusion.rrf's sum, 1/(RRF_K + rank) for each arm ranking it."""
    ranks = {fusion.LEXICAL: lexical_rank, fusion.EMBED: embed_rank}
    return search.Candidate(src_id=src_id, key="msg/" + src_id[-4:], version=1, recorded_at=T0, author="pxx",
                            thread_id=None, body="b", body_truncated=False,
                            fused_score=sum(1.0 / (fusion.RRF_K + r) for r in ranks.values() if r is not None),
                            ranks=ranks, cosine=cosine, lexical_score=0.5 if lexical_rank is not None else None)


def test_unset_is_todays_path_and_reads_no_stored_vector(world, monkeypatch):
    monkeypatch.delenv("MESHY_SEMANTIC_RESIDENT_DIMS", raising=False)
    db, q = world
    assert search.resident_dims(SPACE) is None
    idx = _loaded(db)
    assert not idx.view and idx.resident == SPACE.dims and idx.buf.shape[1] == SPACE.dims
    assert all("substring(" not in s for s in db.log)
    assert any("AS src_id, vec, embedded_at FROM mesh_embeddings" in s for s in db.log)   # today's text
    arm = search._embed_arm(idx, q, source="embedded", read=_boom)
    assert arm.detail == {"query_vector": "embedded"} and arm.exact is None
    assert arm.as_json() == {"ran": True, "ranked": 60, "query_vector": "embedded"}
    unit = (q / np.linalg.norm(q)).astype(np.float32)
    want = sorted(range(60), key=lambda i: -float(idx.buf[idx.pos[ID(i)]] @ unit))
    assert arm.ranking.ids == [ID(i) for i in want]
    pool = [_cand(ID(1), 0.3, 900)]
    assert search._exact_pool(pool, arm, _boom) is pool                 # no read, the same list
    assert db.stored_reads() == []
    assert "resident_dims" not in idx.describe(1000)
    assert idx.vector_of(ID(3)) is not None
    assert search._view_read(db, idx) == {}                             # callers keep today's call shape


def test_a_view_slices_in_sql_and_holds_resident_floats(world, monkeypatch):
    db, _q = world
    idx = _loaded(db, resident=256)
    assert idx.view and idx.buf.shape[1] == 256 and idx.n == 60
    full = [s for s in db.log if "DESC" in s]
    assert len(full) == 1 and "substring(vec from 1 for 1024) AS vec" in full[0]
    assert np.allclose(np.linalg.norm(idx.buf[:idx.n], axis=1), 1.0, atol=1e-5)
    first = spaces.decode(db.rows[0]["vec"], SPACE.dims)[:256]
    assert np.allclose(idx.buf[idx.pos[ID(0)]], first / np.linalg.norm(first), atol=1e-6)
    # a row's own full-width vector, as a query, finds that row first in the view: the query is sliced the same way
    own = spaces.decode(db.rows[0]["vec"], SPACE.dims).astype(np.float64)
    ids, sc = idx.rank((own / np.linalg.norm(own)).astype(np.float32))
    assert ids[0] == ID(0) and sc[0] == pytest.approx(1.0, abs=1e-5)
    # the incremental window and the reconcile slice too
    db.add(60, np.ones(SPACE.dims, dtype=np.float32))
    idx.refresh(db, ceiling=1000)
    monkeypatch.setattr(search.time, "monotonic", lambda: 10 ** 9)
    db.add(61, np.ones(SPACE.dims, dtype=np.float32))
    idx.refresh(db, ceiling=1000)
    loads = [s for s in db.log if "FROM mesh_embeddings" in s and " vec" in s and not s.startswith(STORED)]
    assert len(loads) >= 3 and all("substring(vec from 1 for 1024) AS vec" in s for s in loads)
    assert idx.n == 62 and idx.buf.shape[1] == 256


def test_the_rescored_head_is_the_full_width_ranking_of_those_rows(world, monkeypatch):
    monkeypatch.setenv("MESHY_SEMANTIC_RESCORE_DEPTH", "20")
    db, q = world
    full, view = _loaded(db), _loaded(db, resident=256)
    read, calls = _read(db, view)
    arm = search._embed_arm(view, q, source="embedded", read=read)
    assert len(calls) == 1 and len(calls[0]) == 20                    # ONE read, of the first N
    assert arm.detail == {"query_vector": "embedded", "resident_dims": 256, "rescore_depth": 20, "rescored": 20}
    head = arm.ranking.ids[:20]
    assert set(head) == set(calls[0]) and arm.exact == frozenset(head)
    ref = search._embed_arm(full, q, source="embedded")
    full_score = dict(zip(ref.ranking.ids, ref.ranking.scores))
    assert head == sorted(head, key=lambda s: -full_score[s])         # the full width's order
    assert np.allclose(arm.ranking.scores[:20], [full_score[s] for s in head], rtol=0, atol=1e-6)
    unit = (q.astype(np.float64) / np.linalg.norm(q.astype(np.float64))).astype(np.float32)
    assert len(arm.ranking.ids) == 60 and arm.ranking.ids[20:] == view.rank(unit)[0][20:]   # past N: the view's order


def test_a_missing_or_malformed_stored_row_drops_out_and_ties_keep_the_views_order(monkeypatch):
    monkeypatch.setenv("MESHY_SEMANTIC_RESCORE_DEPTH", "12")                # every row is in the head
    db = _DB()
    rng = np.random.default_rng(5)
    twin = rng.standard_normal(SPACE.dims).astype(np.float32)
    db.add(0, twin)
    db.add(1, twin)                                                    # an exact tie at every width
    for i in range(2, 12):
        db.add(i, rng.standard_normal(SPACE.dims).astype(np.float32))
    view = _loaded(db, resident=256)
    db.rows[5]["vec"] = db.rows[5]["vec"][:400]                        # malformed at the full width
    db.rows = [r for r in db.rows if r["src_id"] != ID(7)]             # gone since the load
    read, _calls = _read(db, view)
    arm = search._embed_arm(view, twin, source="stored", read=read)
    head = arm.ranking.ids[:arm.detail["rescored"]]
    assert ID(5) not in head and ID(7) not in head and arm.detail["rescored"] == 10
    order = view.rank((twin / np.linalg.norm(twin)).astype(np.float32))[0]
    assert head.index(ID(0)) < head.index(ID(1)) if order.index(ID(0)) < order.index(ID(1)) else \
        head.index(ID(1)) < head.index(ID(0))


def test_a_pool_member_past_the_head_gets_its_full_width_cosine(world, monkeypatch):
    monkeypatch.setenv("MESHY_SEMANTIC_RESCORE_DEPTH", "10")
    db, q = world
    full, view = _loaded(db), _loaded(db, resident=256)
    read, calls = _read(db, view)
    arm = search._embed_arm(view, q, source="embedded", read=read)
    ids, scores = arm.ranking.ids, arm.ranking.scores
    ref = search._embed_arm(full, q, source="embedded")
    full_score = dict(zip(ref.ranking.ids, ref.ranking.scores))
    gone = ids[30]
    db.rows = [r for r in db.rows if r["src_id"] != gone]
    pool = [_cand(ids[0], scores[0], 1), _cand(ids[25], scores[25], 26), _cand(gone, scores[30], 31),
            _cand(ID(99), None, None)]
    out = search._exact_pool(pool, arm, read)
    assert len(calls) == 2 and sorted(calls[1]) == sorted([ids[25], gone])   # at most one more read
    assert out[0] is pool[0]                                           # the head was already exact
    assert out[1].cosine == pytest.approx(full_score[ids[25]], abs=1e-6)
    assert out[1].cosine != pytest.approx(scores[25], abs=1e-9)        # it was the view's score before
    assert out[2].cosine is None and out[2].ranks[fusion.EMBED] is None  # dropped out of the embed arm
    assert out[3] is pool[3]                                           # the lexical-only member: untouched
    assert arm.detail["rescored_pool"] == 2 and arm.as_json()["rescored_pool"] == 2


def test_a_pool_member_with_no_full_width_vector_loses_its_embed_share_or_leaves_the_pool(world, monkeypatch):
    """GitHub Codex P2 on #543 (search.py:586), confirmed by pxx (6bf45e): a member past the rescored head whose stored
    row is malformed lost its embed rank but KEPT its embed share of fused_score, so an embed-only one could still be
    returned. Now, as the head does before fusion (_embed_arm): the share leaves the score, a member no other arm
    ranks leaves the pool, and the order without a rerank follows the corrected scores.
    CONTROL: the same pool with every stored row whole is returned in its own order, one score changed by nothing.
    MUTANT: keep the embed share; the two-arm member keeps its place. MUTANT: keep the embed-only member; it is in
    the pool. MUTANT: skip the re-sort; the two-arm member stays second. All three pass at bd1c6e24's code."""
    monkeypatch.setenv("MESHY_SEMANTIC_RESCORE_DEPTH", "10")           # smaller than the pool
    db, q = world
    view = _loaded(db, resident=256)
    read, _calls = _read(db, view)
    arm = search._embed_arm(view, q, source="embedded", read=read)
    ids, scores = arm.ranking.ids, arm.ranking.scores
    both, only = ids[15], ids[20]                                      # both past the head of 10
    pool = [_cand(ids[12], scores[12], 13, lexical_rank=5),          # 1/73 + 1/65
            _cand(both, scores[15], 16, lexical_rank=3),             # 1/76 + 1/63
            _cand(ids[0], scores[0], 1, lexical_rank=None),          # 1/61, in the head
            _cand(ID(98), None, None, lexical_rank=1),               # 1/61, lexical only
            _cand(only, scores[20], 21, lexical_rank=None)]          # 1/81, embed only
    assert [c.fused_score for c in pool] == sorted((c.fused_score for c in pool), reverse=True), "fused order"
    whole = search._exact_pool(list(pool), arm, read)
    assert [c.src_id for c in whole] == [c.src_id for c in pool]
    assert [c.fused_score for c in whole] == [c.fused_score for c in pool], "the control: no row was bad"
    for sid in (both, only):                                           # malformed at the full width since the load
        row = next(r for r in db.rows if r["src_id"] == sid)
        row["vec"] = row["vec"][:400]
    arm = search._embed_arm(view, q, source="embedded", read=read)
    out = search._exact_pool(list(pool), arm, read)
    assert only not in [c.src_id for c in out], "an embed-only member with no full-width vector is gone"
    got = next(c for c in out if c.src_id == both)
    assert got.cosine is None and got.ranks[fusion.EMBED] is None
    assert got.fused_score == pytest.approx(1.0 / (fusion.RRF_K + 3), abs=1e-12), "only its lexical share is left"
    assert [c.src_id for c in out] == [ids[12], ids[0], ID(98), both], "it moves below the 1/61 rows"
    assert arm.detail["dropped_pool"] == 1 and arm.as_json()["dropped_pool"] == 1


def test_related_leaves_out_an_embed_only_member_with_no_full_width_vector(world, monkeypatch):
    """The same P2 on the context route: related.build_pool returns _exact_pool's pool, so an embed-only member past
    the head with a malformed stored row is not in it, and no member is left with no arm ranking it.
    MUTANT: keep the embed-only member in _exact_pool; it is in related's pool."""
    from apps.meshsem import lexical, related
    monkeypatch.setenv("MESHY_SEMANTIC_RESCORE_DEPTH", "10")
    db, _q = world
    idx = _loaded(db, resident=256)
    src_vec = spaces.decode(db.rows[0]["vec"], SPACE.dims)
    order = search._embed_arm(idx, src_vec, source="stored", read=_read(db, idx)[0]).ranking.ids   # related's arm
    monkeypatch.setattr(related, "_older", lambda conn, fused, src: fused)
    # the pool as _pool builds it from the real fused list: the lexical index is empty, so every member is embed-only
    monkeypatch.setattr(search, "_pool", lambda conn, fused, **kw: (
        [_cand(f.src_id, f.arm_scores[fusion.EMBED], f.ranks.get(fusion.EMBED), lexical_rank=None)
         for f in fused[:40]], 40, {}))
    bad = order[25]                                                    # past the head of 10, inside the pool of 40
    row = next(r for r in db.rows if r["src_id"] == bad)
    row["vec"] = row["vec"][:400]                                      # malformed at the full width since the load
    src = {"id": ID(0), "body": "alpha", "created_at": T0, "thread_id": None}
    pool = related.build_pool(db, SPACE, src, gate=False, idx=idx, lex=lexical.LexicalIndex(SPACE.name))
    assert pool and bad not in [c.src_id for c in pool]
    assert all(any(r is not None for r in c.ranks.values()) for c in pool), "every member is ranked by some arm"


def test_the_view_needs_a_reader_and_returns_no_vector(world):
    db, q = world
    view = _loaded(db, resident=256)
    with pytest.raises(RuntimeError, match="needs `read`"):
        search._embed_arm(view, q, source="embedded")
    assert view.vector_of(ID(3)) is None
    assert view.describe(1000)["resident_dims"] == 256
    assert set(search._view_read(db, view)) == {"read"}


def test_only_declared_widths_and_only_the_mail_index(monkeypatch):
    with pytest.raises(ValueError, match="declares no 300-d view"):
        search.VectorIndex(SPACE, resident=300)
    for raw, want in (("256", 256), ("512", 512), ("768", None), ("300", None), ("256.5", None), ("", None),
                      ("x", None)):
        monkeypatch.setenv("MESHY_SEMANTIC_RESIDENT_DIMS", raw)
        assert search.resident_dims(SPACE) == want, raw
    for raw, want in (("128", 128), ("5", search.DEFAULT_RESCORE_DEPTH), ("99999", search.DEFAULT_RESCORE_DEPTH),
                      ("nope", search.DEFAULT_RESCORE_DEPTH)):
        monkeypatch.setenv("MESHY_SEMANTIC_RESCORE_DEPTH", raw)
        assert search.rescore_depth() == want, raw
    monkeypatch.setenv("MESHY_SEMANTIC_RESIDENT_DIMS", "256")
    monkeypatch.setattr(search, "_indexes", {})
    monkeypatch.setattr(search, "_graph_indexes", {})
    assert search.index_for(SPACE).resident == 256
    assert graphsearch.index_for(SPACE, "shared/decisions").resident == SPACE.dims
    # a view is not a new space: its identity is unchanged
    assert dataclasses.replace(SPACE, views=()).canary_sha256() == SPACE.canary_sha256()


@pytest.fixture()
def no_txn(monkeypatch):
    """dbconn.request_server_internal over the fake: the fake connection itself, and a count of the transactions."""
    import contextlib
    opened = []

    @contextlib.contextmanager
    def fake(conn):
        opened.append(conn)
        yield conn
    monkeypatch.setattr(search.dbconn, "request_server_internal", fake)
    return opened


def test_the_context_query_vector_is_read_by_primary_key_only_on_a_view(world, no_txn):
    db, _q = world
    full, view = _loaded(db), _loaded(db, resident=256)
    assert search._query_vector_of(db, full, ID(4)) is not None and no_txn == []     # loaded: no read
    assert search._query_vector_of(db, full, ID(999)) is None and no_txn == []       # not held: the embedder's case
    got = search._query_vector_of(db, view, ID(4))
    assert len(no_txn) == 1 and db.stored_reads()[-1].startswith(STORED)
    assert got.shape == (SPACE.dims,) and np.array_equal(got, spaces.decode(db.rows[4]["vec"], SPACE.dims))
    read = search._reader(db, view)                                                  # its own short transaction
    assert set(read([ID(1), ID(2)])) == {ID(1), ID(2)} and len(no_txn) == 2


@pytest.mark.parametrize("resident", [None, 256])
def test_related_pools_carry_the_full_width_cosine(world, monkeypatch, resident):
    """related stores each edge's cosine (related.py:662): on a view it must be the full-width one, and unset must
    read no stored vector beyond today's."""
    from apps.meshsem import lexical, related
    monkeypatch.setenv("MESHY_SEMANTIC_RESCORE_DEPTH", "10")
    db, _q = world
    full, idx = _loaded(db), _loaded(db, resident=resident)
    r = search._embed_arm(full, spaces.decode(db.rows[0]["vec"], SPACE.dims), source="stored").ranking
    ref = dict(zip(r.ids, r.scores))                                   # the full-width cosine of every row
    monkeypatch.setattr(related, "_older", lambda conn, fused, src: fused)
    # the pool: the fused order's first 40, as _pool would build it from the arms' scores
    monkeypatch.setattr(search, "_pool", lambda conn, fused, **kw: (
        [_cand(f.src_id, f.arm_scores[fusion.EMBED], f.ranks.get(fusion.EMBED)) for f in fused[:40]], 40, {}))
    before = len(db.stored_reads())
    src = {"id": ID(0), "body": "alpha", "created_at": T0, "thread_id": None}
    pool = related.build_pool(db, SPACE, src, gate=False, idx=idx, lex=lexical.LexicalIndex(SPACE.name))
    assert pool and all(c.cosine == pytest.approx(ref[c.src_id], abs=1e-6) for c in pool if c.cosine is not None)
    reads = len(db.stored_reads()) - before
    assert reads == (0 if resident is None else 2)            # the head, then the pool members past it
