"""Build, then swap: a refresh of the in-process search indexes never makes a search wait (13 Nov 2026).

On HQD on 12-13 Nov a search's refresh held the index lock across its database read and its rebuild; every
other search queued on that lock, the queued searches filled the server's thread pool and /v1/livez missed (stack
dumps 23:43, 23:45, (time withheld); pxx 1c64e7). Now a refresh is single-flight, reads and builds with no serving
lock, and swaps; a request that finds a refresh running serves the last load, or gets a temporary 503
(E_INDEX_WARMING, Retry-After) when there is none yet.

No database: a cursor that records statements stands in for Postgres, as in test_meshsem_full_load.py. The
full load's fetch blocks on an Event so a second thread can act while it runs."""
from __future__ import annotations

import datetime as dt
import os
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# Django set up here, as tests/test_db_connect_bound.py does, so the views import also works when this file runs alone.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

from apps.meshsem import lexical, search, spaces  # noqa: E402

T0 = dt.datetime(2026, 11, 13, 0, 0, tzinfo=dt.timezone.utc)
WAIT_S = 10            # a hang backstop only; every step below is expected within milliseconds


class _Cur:
    """current_setting answers the request's timeout; the full load answers `full`, after `gate` if one is set;
    a load that `fail`s raises; anything else answers []."""

    def __init__(self, full, gate=None, entered=None, fail=False):
        self.full, self.gate, self.entered, self.fail, self.sql = full, gate, entered, fail, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.sql.append(sql)

    def fetchone(self):
        return {"t": "30s"}

    def fetchall(self):
        if "DESC LIMIT" not in self.sql[-1]:
            return []
        if self.entered is not None:
            self.entered.set()
        if self.gate is not None:
            assert self.gate.wait(WAIT_S), "the test never released the full load"
        if self.fail:
            raise RuntimeError("database went away during the full load")
        return list(self.full)


class _Conn:
    def __init__(self, cur):
        self.cur = cur

    def cursor(self, row_factory=None):
        return self.cur


class _Space:
    name, dims = "test-space/4", 4


def _vec_row(src_id, *xs, s=1):
    return {"src_id": src_id, "vec": spaces.encode(np.asarray(xs, dtype=np.float32)),
            "embedded_at": T0 + dt.timedelta(seconds=s)}


def _lex_row(src_id, word, s=1):
    return {"src_id": src_id, "text_sha256": src_id * 64, "indexed_at": T0 + dt.timedelta(seconds=s),
            "lexemes": [word], "tfs": [1]}


def _vector_rank(idx):
    return idx.rank(np.asarray([1, 0, 0, 0], dtype=np.float32))[0]


def _lexical_rank(idx, word):
    return idx.rank([word], [1], limit=5)[0]


def _force_full_load(idx):
    """The next refresh does a full load: the reconcile is due, and over the ceiling it refuses."""
    idx.loaded_mono, idx.over_ceiling = -1e12, True


def _in_thread(fn):
    out = {}

    def run():
        try:
            out["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - asserted on by the caller
            out["error"] = exc
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, out


@pytest.mark.parametrize("kind", ["vector", "lexical"])
def test_a_search_during_a_slow_full_load_serves_the_old_load_and_never_waits(kind):
    if kind == "vector":
        idx = search.VectorIndex(_Space, "shared/mesh-chat")
        old, new = [_vec_row("a", 1, 0, 0, 0)], [_vec_row("c", 1, 0, 0, 0, s=5)]
        ranked = lambda: _vector_rank(idx)                                  # noqa: E731
    else:
        idx = lexical.LexicalIndex(_Space.name, "shared/mesh-chat")
        old, new = [_lex_row("a", "alpha")], [_lex_row("c", "alpha", s=5)]
        ranked = lambda: _lexical_rank(idx, "alpha")                        # noqa: E731
    idx.refresh(_Conn(_Cur(old)), ceiling=10)
    assert ranked() == ["a"]                                                # control: the first load serves

    _force_full_load(idx)
    gate, entered = threading.Event(), threading.Event()
    worker, out = _in_thread(lambda: idx.refresh(_Conn(_Cur(new, gate, entered)), ceiling=10))
    assert entered.wait(WAIT_S), "the full load never started"
    try:
        started = time.monotonic()
        assert ranked() == ["a"], "a search during the full load ranks the OLD load"
        idx.refresh(_Conn(_Cur([])), ceiling=10)                            # a second request: returns at once
        assert time.monotonic() - started < WAIT_S / 2, "a search waited for the running refresh"
        assert worker.is_alive(), "control: the full load really was still running"
    finally:
        gate.set()
    worker.join(WAIT_S)
    assert not worker.is_alive() and "error" not in out, out
    assert ranked() == ["c"], "after the swap the new load serves"


@pytest.mark.parametrize("kind", ["vector", "lexical"])
def test_with_no_load_yet_a_second_request_gets_index_warming_not_a_wait(kind):
    idx = (search.VectorIndex(_Space, "shared/mesh-chat") if kind == "vector"
           else lexical.LexicalIndex(_Space.name, "shared/mesh-chat"))
    gate, entered = threading.Event(), threading.Event()
    rows = [_vec_row("a", 1, 0, 0, 0)] if kind == "vector" else [_lex_row("a", "alpha")]
    worker, out = _in_thread(lambda: idx.refresh(_Conn(_Cur(rows, gate, entered)), ceiling=10))
    assert entered.wait(WAIT_S)
    try:
        with pytest.raises(lexical.IndexWarming):
            idx.refresh(_Conn(_Cur([])), ceiling=10)
    finally:
        gate.set()
    worker.join(WAIT_S)
    assert not worker.is_alive() and "error" not in out, out
    idx.refresh(_Conn(_Cur([])), ceiling=10)                                # control: once built, no refusal


def test_index_warming_becomes_a_temporary_503_with_retry_after():
    class Warming:
        def refresh(self, conn, *, ceiling):
            raise lexical.IndexWarming("building")

    with pytest.raises(search.SemanticUnavailable) as caught:
        search.refresh_or_warming(Warming(), Warming(), object(), 10, lambda: {"space": "s"})
    exc = caught.value
    assert exc.status == 503 and exc.code == search.E_INDEX_WARMING
    assert exc.retry_after == lexical.WARMING_RETRY_AFTER_S and exc.body()["space"] == "s"

    from apps.meshsem import views
    resp = views._unavailable(exc)
    assert resp.status_code == 503 and resp["Retry-After"] == str(lexical.WARMING_RETRY_AFTER_S)
    other = search.SemanticUnavailable(503, "E_SPACE_MISMATCH", "m", {})
    assert not views._unavailable(other).has_header("Retry-After")          # control: only a temporary refusal


@pytest.mark.parametrize("kind", ["vector", "lexical"])
def test_a_full_load_that_fails_leaves_the_old_load_serving(kind):
    """Before, the full load reset the index first, so a failed read left it empty until the next refresh."""
    if kind == "vector":
        idx = search.VectorIndex(_Space, "shared/mesh-chat")
        idx.refresh(_Conn(_Cur([_vec_row("a", 1, 0, 0, 0)])), ceiling=10)
        ranked = lambda: _vector_rank(idx)                                  # noqa: E731
    else:
        idx = lexical.LexicalIndex(_Space.name, "shared/mesh-chat")
        idx.refresh(_Conn(_Cur([_lex_row("a", "alpha")])), ceiling=10)
        ranked = lambda: _lexical_rank(idx, "alpha")                        # noqa: E731
    _force_full_load(idx)
    with pytest.raises(RuntimeError):
        idx.refresh(_Conn(_Cur([], fail=True)), ceiling=10)
    assert ranked() == ["a"]
    idx.refresh(_Conn(_Cur([])), ceiling=10)                                # control: the lock was released


class _IncCur(_Cur):
    """Answers the incremental read (not a full load) with `inc`."""

    def __init__(self, inc):
        super().__init__([])
        self.inc = inc

    def fetchall(self):
        return list(self.inc) if "DESC LIMIT" not in self.sql[-1] else []


def test_a_lexical_refresh_that_changes_nothing_copies_nothing(monkeypatch):
    """pxx b9d64b P3-2: the overlap window re-reads the last minute's rows on every search; when none of them
    changes the index, no copy is made. Control: a changed row is copied in and served."""
    idx = lexical.LexicalIndex(_Space.name, "shared/mesh-chat")
    row = _lex_row("a", "alpha")
    idx.refresh(_Conn(_Cur([row])), ceiling=10)
    clones = []
    real = lexical.LexicalIndex._clone
    monkeypatch.setattr(lexical.LexicalIndex, "_clone", lambda self: clones.append(1) or real(self))

    idx.refresh(_Conn(_IncCur([row])), ceiling=10)                      # same id, same digest
    assert clones == [] and _lexical_rank(idx, "alpha") == ["a"]

    changed = dict(_lex_row("a", "beta", s=2), text_sha256="f" * 64)
    idx.refresh(_Conn(_IncCur([changed])), ceiling=10)                  # control: a changed digest
    assert clones == [1] and _lexical_rank(idx, "beta") == ["a"] and _lexical_rank(idx, "alpha") == []


def test_a_reindexed_row_with_the_same_text_still_moves_the_watermark(monkeypatch):
    """pxx 35bb76 P2-1: the no-copy shortcut must not skip a row that only moves the watermark (same id, same
    digest, newer indexed_at); main moved it, so the overlap window stays bounded. Control: the same row at its old
    time, inside the overlap and not newer, still copies nothing."""
    idx = lexical.LexicalIndex(_Space.name, "shared/mesh-chat")
    row = _lex_row("a", "alpha", s=0)
    idx.refresh(_Conn(_Cur([row])), ceiling=10)
    assert idx.watermark == (T0, "a")
    clones = []
    real = lexical.LexicalIndex._clone
    monkeypatch.setattr(lexical.LexicalIndex, "_clone", lambda self: clones.append(1) or real(self))

    idx.refresh(_Conn(_IncCur([row])), ceiling=10)                      # control: not newer, nothing copied
    assert clones == [] and idx.watermark == (T0, "a")

    later = dict(row, indexed_at=T0 + dt.timedelta(minutes=5))          # same text, re-indexed later
    idx.refresh(_Conn(_IncCur([later])), ceiling=10)
    assert idx.watermark == (T0 + dt.timedelta(minutes=5), "a")
    assert _lexical_rank(idx, "alpha") == ["a"]
