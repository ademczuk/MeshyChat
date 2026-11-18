"""The in-process search indexes are RECONCILED, not fully reloaded, once older than FULL_RELOAD_S.

Until 29 Oct 2026 an index older than FULL_RELOAD_S (600 s) re-read every row inside the search request that found it
stale: about 150 MB of vectors (into a buffer regrown by doubling) and every document's terms, under the index lock and
inside one transaction. On HQD that coincided with production livez timeouts while the box was paging. Now one read of the
live (id, stamp) pairs drops deleted rows and loads missing or changed ones by id. The full reload also did two more jobs,
which the reconcile keeps (pxx c0fed9): a row committed more than OVERLAP_S late is loaded (P2-2), and an index over its
ceiling still gets the full load, so it refills after deletes (P2-1). The DB-backed probes are in
tests/test_meshsem_reconcile_db.py.

DB-free: a fake connection answers each SELECT from a table held in the test and records the SQL, so the tests can say
which reads ran. CONTROLS: a new index loads in full, and an index within FULL_RELOAD_S neither reconciles nor reloads.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")

import django  # noqa: E402

django.setup()

from apps.meshsem import lexical, search, spaces  # noqa: E402

T0 = dt.datetime(2026, 10, 29, 6, 0, tzinfo=dt.timezone.utc)
ID = "00000000-0000-0000-0000-00000000000{}".format


def _is_id_read(flat: str) -> bool:
    return (flat.startswith("SELECT src_id, text_sha256 FROM mesh_lexical")
            or (" AS src_id, embedded_at FROM mesh_embeddings" in flat))


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
        if "current_setting(" in flat:                         # fullload saving the request's timeout
            self._rows = [{"t": "30s"}]
            return
        if "set_config(" in flat:                              # fullload setting or restoring it
            self._rows = [{"set_config": params[0] if params else ""}]
            return
        table = "mesh_lexical" if "FROM mesh_lexical" in flat else "mesh_embeddings"
        rows = self.db.tables[table]
        if _is_id_read(flat):
            self._rows = [{"src_id": r["src_id"], "text_sha256": r["text_sha256"], "embedded_at": r["at"]}
                          for r in rows]
            return
        if "= ANY(" in flat:                                   # the reconcile's load by id
            want = set(params[3])
            got = sorted([r for r in rows if r["src_id"] in want], key=lambda r: (r["at"], r["src_id"]))
        elif "DESC" in flat:                                   # the full load, newest first
            got = sorted(rows, key=lambda r: (r["at"], r["src_id"]), reverse=True)
        else:                                                  # the incremental window
            wm = params[3]
            got = sorted([r for r in rows if wm is None or r["at"] >= wm - dt.timedelta(seconds=60)],
                         key=lambda r: (r["at"], r["src_id"]))
        self._rows = [dict(r, indexed_at=r["at"], embedded_at=r["at"]) for r in got]

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _DB:
    def __init__(self):
        self.tables = {"mesh_lexical": [], "mesh_embeddings": []}
        self.log: list[str] = []

    def cursor(self, row_factory=None):
        return _Cur(self)

    def add(self, src_id: str, words: str, at: dt.datetime, vec: np.ndarray):
        lx, tf = lexical.terms(words)
        self.tables["mesh_lexical"].append({"src_id": src_id, "text_sha256": src_id + "-" + words, "at": at,
                                            "lexemes": lx, "tfs": tf})
        self.tables["mesh_embeddings"].append({"src_id": src_id, "text_sha256": "", "at": at,
                                               "vec": spaces.encode(vec)})

    def delete(self, src_id: str):
        for t in self.tables.values():
            t[:] = [r for r in t if r["src_id"] != src_id]

    def full_loads(self) -> int:
        return sum(1 for s in self.log if "DESC" in s)

    def id_reads(self) -> int:
        return sum(1 for s in self.log if _is_id_read(s))


@pytest.fixture()
def world(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(lexical.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(search.time, "monotonic", lambda: clock["t"])
    space = spaces.get(spaces.DEFAULT_SPACE)
    db = _DB()
    rng = np.random.default_rng(7)
    for i, words in enumerate(["alpha beta", "beta gamma", "gamma delta", "delta alpha"]):
        db.add(ID(i), words, T0 + dt.timedelta(seconds=i), rng.standard_normal(space.dims).astype(np.float32))
    db.rng = rng
    return db, clock, space


def _both(space):
    return search.VectorIndex(space), lexical.LexicalIndex(space.name)


def _refresh(db, idxs, ceiling=100):
    for idx in idxs:
        idx.refresh(db, ceiling=ceiling)


def _stale(clock):
    clock["t"] += search.FULL_RELOAD_S + 1


def test_a_stale_index_drops_deleted_rows_without_a_full_reload(world):
    db, clock, space = world
    vec, lex = _both(space)
    _refresh(db, (vec, lex))
    assert db.full_loads() == 2, db.log                       # CONTROL: a new index loads in full
    kept_vec = vec.vector_of(ID(2))
    db.delete(ID(1))
    db.log.clear()
    _stale(clock)
    _refresh(db, (vec, lex))
    assert db.full_loads() == 0 and db.id_reads() == 2, db.log
    assert vec.ids == lex.ids == [ID(0), ID(2), ID(3)] and ID(1) not in vec.pos and ID(1) not in lex.pos
    assert np.array_equal(vec.vector_of(ID(2)), kept_vec) and [vec.pos[s] for s in vec.ids] == [0, 1, 2]
    assert lex.rank(*lexical.terms("beta"), limit=10)[0] == [ID(0)]     # "beta gamma" is gone
    top, _ = vec.rank(vec.vector_of(ID(3)))
    assert top[0] == ID(3) and ID(1) not in top


def test_a_fresh_index_neither_reconciles_nor_reloads(world):
    db, clock, space = world
    vec, lex = _both(space)
    _refresh(db, (vec, lex))
    db.log.clear()
    clock["t"] += search.FULL_RELOAD_S - 1
    _refresh(db, (vec, lex))
    assert db.full_loads() == 0 and db.id_reads() == 0, db.log


def test_a_row_committed_later_than_the_overlap_window_is_loaded_by_the_reconcile(world):
    """pxx c0fed9 P2-2: a row stamped 120 s behind the watermark, committed after it moved, is outside the incremental
    read's 60 s overlap. The full reload used to reach it; the reconcile loads it by id."""
    db, clock, space = world
    vec, lex = _both(space)
    _refresh(db, (vec, lex))
    db.add(ID(8), "zeta", T0 + dt.timedelta(seconds=240), np.ones(space.dims, dtype=np.float32))
    _refresh(db, (vec, lex))                                  # watermark moves to +240 s
    db.add(ID(9), "omega", T0 + dt.timedelta(seconds=120), np.full(space.dims, -1.0, dtype=np.float32))
    _refresh(db, (vec, lex))
    assert ID(9) not in vec.pos and ID(9) not in lex.pos, "control: the incremental read misses it"
    db.log.clear()
    _stale(clock)
    _refresh(db, (vec, lex))
    assert db.full_loads() == 0, db.log
    assert ID(9) in vec.pos and ID(9) in lex.pos
    assert lex.rank(*lexical.terms("omega"), limit=3)[0] == [ID(9)]


def test_a_changed_row_with_a_late_stamp_is_reloaded_by_the_reconcile(world):
    """A late-committed UPDATE: same id, new vector and new text, stamped behind the watermark."""
    db, clock, space = world
    vec, lex = _both(space)
    _refresh(db, (vec, lex))
    db.add(ID(8), "zeta", T0 + dt.timedelta(seconds=240), np.ones(space.dims, dtype=np.float32))
    _refresh(db, (vec, lex))
    db.delete(ID(2))
    db.add(ID(2), "theta", T0 + dt.timedelta(seconds=100), np.full(space.dims, -1.0, dtype=np.float32))
    _refresh(db, (vec, lex))
    assert lex.rank(*lexical.terms("theta"), limit=3)[0] == [], "control: the incremental read misses it"
    _stale(clock)
    _refresh(db, (vec, lex))
    assert lex.rank(*lexical.terms("theta"), limit=3)[0] == [ID(2)]
    assert np.allclose(vec.vector_of(ID(2)), np.full(space.dims, -1.0) / np.sqrt(space.dims), atol=1e-6)


def test_an_index_over_its_ceiling_refills_after_deletes_through_the_full_load(world):
    """pxx c0fed9 P2-1: ceiling 3, four rows, the newest deleted: the next periodic step brings the oldest back in."""
    db, clock, space = world
    vec, lex = _both(space)
    _refresh(db, (vec, lex), ceiling=3)
    assert vec.over_ceiling and lex.over_ceiling and ID(0) not in vec.pos
    db.delete(ID(3))
    db.log.clear()
    _stale(clock)
    _refresh(db, (vec, lex), ceiling=3)
    assert db.full_loads() == 2, db.log
    assert set(vec.ids) == set(lex.ids) == {ID(0), ID(1), ID(2)}


def test_new_rows_that_would_pass_the_ceiling_still_load_in_full_newest_kept(world):
    db, clock, space = world
    vec, lex = _both(space)
    _refresh(db, (vec, lex), ceiling=4)
    _stale(clock)
    db.add(ID(9), "epsilon", T0 + dt.timedelta(seconds=30), np.ones(space.dims, dtype=np.float32))
    db.log.clear()
    _refresh(db, (vec, lex), ceiling=4)
    assert db.full_loads() == 2, db.log
    assert vec.n == lex.n == 4 and ID(0) not in lex.pos and ID(9) in lex.pos
