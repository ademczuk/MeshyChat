"""Step 6b: the per-shard match probe is skipped where it cannot change the answer (apps/meshsem/ctxindex._matching).

At 15M rows an unfiltered search probed about 330 shards to score MAX_SHARDS_PER_QUERY = 4 (warm 27.7 s, memory-bxxxxx
the recorded rule). With no src, project or until filter, a shard whose month starts at or after `since` (or with no since) matches
by construction: its plan row counts current rows only (ctx_shard_plan, 051/053). pxx's conditions (ef9660), one test
each: (2) the probe statements counted per filter shape, (3) parity with the probe on walker-written rows, (4) rows
written without the walker cost a slot and say so. Condition (5), the production count of current (seat, src) pairs
with no ctx_source_gen row, was 0 of 6 on 29 Oct 2026 (read-only query, kxxx).

Run:  pytest tests/test_ctx_probe_shortcut.py -q -p no:cacheprovider
"""
from __future__ import annotations

import datetime as _dt

import pytest

from apps.meshsem import ctxindex
from tests.test_ctx_vector_store import (  # noqa: F401  (fixtures)
    NEEDLE, NOW, OLD, _add, _aggregate_fallback, _away, owner, world)

PROBE_SQL = "WITH ORDINALITY AS p(seat, start_"


class _CountingConn:
    """The owner connection, with every probe statement's shard count recorded."""

    def __init__(self, conn):
        self.conn, self.probes = conn, []

    def cursor(self, *a, **kw):
        real, probes = self.conn.cursor(*a, **kw), self.probes

        class _Cur:
            def __enter__(self):
                real.__enter__()
                return self

            def __exit__(self, *exc):
                return real.__exit__(*exc)

            def execute(self, sql, params=None):
                if PROBE_SQL in sql:
                    probes.append(len(params[0]))
                return real.execute(sql, params)

            def __getattr__(self, name):
                return getattr(real, name)

        return _Cur()


def _search(w, f=None, k=5):
    ctxindex.forget()
    conn = _CountingConn(w["owner"])
    hits, cov = ctxindex.search(conn, w["space"], w["ns"], NEEDLE, f, k=k)
    return hits, cov, conn.probes


@pytest.fixture()
def shards(world, monkeypatch):
    """Two months, two seats, several shards each: SHARD_MAX 3, EXACT_MAX 1, ten shards a query."""
    monkeypatch.setattr(ctxindex, "EXACT_MAX", 1)
    monkeypatch.setattr(ctxindex, "SHARD_MAX", 3)
    monkeypatch.setattr(ctxindex, "MAX_SHARDS_PER_QUERY", 10)
    return world


def _walker_rows(w):
    """Rows as the walker writes them: one ctx_source_gen row per source, only the newest gen current."""
    rows = []
    for seat in ("seat-a", "seat-b"):
        for src, project in (("s1", "p1"), ("s2", "p2")):
            for when in (NOW, OLD):
                rows.append(_add(w, seat=seat, src=src, project=project, when=when, gen=1, current=False,
                                 vec=_away(w)))
                rows.append(_add(w, seat=seat, src=src, project=project, when=when, gen=2, vec=_away(w)))
                rows.append(_add(w, seat=seat, src=src, project=project, when=when, gen=2,
                                 vec=NEEDLE if (seat, src, when) == ("seat-b", "s2", OLD) else _away(w)))
            w["owner"].execute("INSERT INTO ctx_source_gen (space, namespace, seat, src, gen) VALUES "
                               "(%s, %s, %s, %s, 2) ON CONFLICT DO NOTHING", (w["space"].name, w["ns"], seat, src))
    return rows


def _cleanup_gen(w):
    w["owner"].execute("DELETE FROM ctx_source_gen WHERE namespace = %s", (w["ns"],))


# ------------------------------------------------------------------ (2) the probe statements, counted

def test_an_unfiltered_search_runs_no_probe_and_a_filtered_one_still_probes(shards, monkeypatch):
    _walker_rows(shards)
    try:
        hits, cov, probes = _search(shards)
        assert cov.path == "sharded" and probes == [], (probes, cov.as_dict())
        assert hits and hits[0].chunk_id is not None
        planned = len(cov.shards_searched) + len(cov.shards_skipped)

        _, cov, probes = _search(shards, ctxindex.Filters(seat="seat-a"))
        assert probes == [], "the seat is applied by the plan, so no probe"

        month_start = NOW.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        _, cov, probes = _search(shards, ctxindex.Filters(since=month_start))
        assert probes == [], "since at a month start: every remaining month starts at or after it"

        inside = month_start + _dt.timedelta(seconds=1)
        _, cov, probes = _search(shards, ctxindex.Filters(since=inside))
        boundary = [s for s in cov.shards_searched + cov.shards_skipped
                    if s[1] == NOW.strftime("%Y-%m")]
        assert len(probes) == 1 and 0 < probes[0] <= len(boundary) + 2, (probes, cov.as_dict())

        for f in (ctxindex.Filters(src="s1"), ctxindex.Filters(project="p1"),
                  ctxindex.Filters(until=NOW + _dt.timedelta(days=1))):
            _, cov, probes = _search(shards, f)
            assert len(probes) == 1 and probes[0] == planned, (f, probes, planned)   # the full probe, as before
    finally:
        _cleanup_gen(shards)


# ------------------------------------------------------------------ (3) parity on walker-written rows

def test_walker_written_rows_give_the_same_shards_and_hits_with_and_without_the_shortcut(shards, monkeypatch):
    _walker_rows(shards)
    month_start = NOW.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    filter_sets = [ctxindex.Filters(), ctxindex.Filters(seat="seat-b"), ctxindex.Filters(since=month_start),
                   ctxindex.Filters(since=month_start + _dt.timedelta(seconds=1)), ctxindex.Filters(since=OLD),
                   ctxindex.Filters(src="s2"), ctxindex.Filters(project="p1"),
                   ctxindex.Filters(until=OLD + _dt.timedelta(days=1))]
    try:
        for cap in (10, 2):
            monkeypatch.setattr(ctxindex, "MAX_SHARDS_PER_QUERY", cap)
            for f in filter_sets:
                monkeypatch.setattr(ctxindex, "PROBE_SHORTCUT", True)
                hits_on, cov_on, _ = _search(shards, f)
                monkeypatch.setattr(ctxindex, "PROBE_SHORTCUT", False)
                hits_off, cov_off, probes_off = _search(shards, f)
                assert probes_off, "control: with the shortcut off every search probes"
                assert cov_on.shards_searched == cov_off.shards_searched, (cap, f)
                assert cov_on.shards_skipped == cov_off.shards_skipped, (cap, f)
                assert cov_on.truncated == cov_off.truncated and cov_on.scored == cov_off.scored, (cap, f)
                assert [h.chunk_id for h in hits_on] == [h.chunk_id for h in hits_off], (cap, f)
    finally:
        _cleanup_gen(shards)


# ------------------------------------------------------------------ (4) rows written without the walker

def test_a_stale_current_shard_written_without_the_walker_spends_a_slot_and_says_so(world, monkeypatch):
    """gen 1 and gen 2 of one source both flagged current, with no ctx_source_gen row: the newest month holds only gen 1,
    which the latest-gen rule hides. Unprobed, that shard takes the one slot and scores 0, and the older month's gen 2
    shard, holding the needle, is named skipped and the result truncated. CONTROL: with the probe, the needle is found."""
    monkeypatch.setattr(ctxindex, "EXACT_MAX", 1)
    monkeypatch.setattr(ctxindex, "SHARD_MAX", 5)
    monkeypatch.setattr(ctxindex, "MAX_SHARDS_PER_QUERY", 1)
    for _ in range(3):
        _add(world, src="s1", gen=1, when=NOW, vec=_away(world))                 # current, but superseded by gen 2
    needle = _add(world, src="s1", gen=2, when=OLD, vec=NEEDLE)
    _add(world, src="s1", gen=2, when=OLD, vec=_away(world))

    hits, cov, probes = _search(world)
    assert probes == [] and cov.path == "sharded"
    assert needle not in [h.chunk_id for h in hits] and cov.scored == 0, cov.as_dict()
    assert cov.truncated and any(s[1] == OLD.strftime("%Y-%m") for s in cov.shards_skipped), cov.as_dict()

    monkeypatch.setattr(ctxindex, "PROBE_SHORTCUT", False)
    hits, cov, probes = _search(world)
    assert probes and hits and hits[0].chunk_id == needle and not cov.shards_skipped, cov.as_dict()
