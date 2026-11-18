"""A FULL in-process index load runs its one statement under fullload.FULL_LOAD_TIMEOUT_MS, then restores the
transaction's own timeout; the incremental read keeps the request's (apps/meshsem/fullload.py).

7 Nov 2026: on a HQD short of memory the mail index's full load hit the request's 30 s statement timeout three times
(QueryCanceled on /v1/mesh/search and /v1/mesh/context), so the index never loaded and mail search stayed down.
No database here: a cursor that records statements stands in for Postgres."""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from apps.meshsem import fullload, lexical, search, spaces  # noqa: E402

T0 = dt.datetime(2026, 11, 7, 9, 0, tzinfo=dt.timezone.utc)


class _Cur:
    """Answers current_setting with the request's timeout, the full load with `full`, anything else with []."""

    def __init__(self, full):
        self.full, self.sql, self.params = full, [], []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.sql.append(sql)
        self.params.append(params)

    def fetchone(self):
        assert "current_setting('statement_timeout')" in self.sql[-1]
        return {"t": "30s"}

    def fetchall(self):
        return list(self.full) if "DESC LIMIT" in self.sql[-1] else []


class _Conn:
    def __init__(self, cur):
        self.cur = cur

    def cursor(self, row_factory=None):
        return self.cur


class _Space:
    name, dims = "test-space/4", 4


def _timeouts(cur):
    return [p[0] for s, p in zip(cur.sql, cur.params) if "set_config('statement_timeout'" in s]


def _vec(*xs):
    return spaces.encode(np.asarray(xs, dtype=np.float32))


def test_a_full_vector_load_runs_under_the_long_timeout_and_restores_the_requests():
    rows = [{"src_id": "b", "vec": _vec(0, 1, 0, 0), "embedded_at": T0 + dt.timedelta(seconds=2)},
            {"src_id": "a", "vec": _vec(1, 0, 0, 0), "embedded_at": T0 + dt.timedelta(seconds=1)}]
    cur = _Cur(rows)
    idx = search.VectorIndex(_Space, "shared/mesh-chat")
    idx.refresh(_Conn(cur), ceiling=10)
    assert idx.n == 2 and idx.ids == ["a", "b"], "oldest first in the buffer, as before"
    assert _timeouts(cur) == [f"{fullload.FULL_LOAD_TIMEOUT_MS}ms", "30s"]
    sets = [i for i, s in enumerate(cur.sql) if "set_config('statement_timeout'" in s]
    load = next(i for i, s in enumerate(cur.sql) if "DESC LIMIT" in s)
    assert sets[0] < load < sets[1] == len(cur.sql) - 1, "set just before the load, restored right after it"

    # control: the next refresh is incremental and leaves the request's timeout alone
    cur2 = _Cur([])
    idx.refresh(_Conn(cur2), ceiling=10)
    assert _timeouts(cur2) == [] and not any("DESC LIMIT" in s for s in cur2.sql)


def test_a_full_lexical_load_runs_under_the_long_timeout_and_restores_the_requests():
    rows = [{"src_id": "b", "text_sha256": "2" * 64, "indexed_at": T0 + dt.timedelta(seconds=2),
             "lexemes": ["beta"], "tfs": [1]},
            {"src_id": "a", "text_sha256": "1" * 64, "indexed_at": T0 + dt.timedelta(seconds=1),
             "lexemes": ["alpha"], "tfs": [1]}]
    cur = _Cur(rows)
    idx = lexical.LexicalIndex(_Space.name, "shared/mesh-chat")
    idx.refresh(_Conn(cur), ceiling=10)
    assert idx.n == 2
    assert _timeouts(cur) == [f"{fullload.FULL_LOAD_TIMEOUT_MS}ms", "30s"]
    cur2 = _Cur([])
    idx.refresh(_Conn(cur2), ceiling=10)
    assert _timeouts(cur2) == []


def test_the_full_load_timeout_comes_from_the_env_and_nonsense_keeps_the_default(monkeypatch):
    monkeypatch.delenv("MESHY_SEMANTIC_FULL_LOAD_TIMEOUT_MS", raising=False)
    assert fullload.timeout_ms() == fullload.FULL_LOAD_TIMEOUT_MS == 180_000
    monkeypatch.setenv("MESHY_SEMANTIC_FULL_LOAD_TIMEOUT_MS", "60000")
    assert fullload.timeout_ms() == 60_000
    for bad in ("0", "999", "-5", "two minutes", "1.5", " ", str(2 ** 31)):
        monkeypatch.setenv("MESHY_SEMANTIC_FULL_LOAD_TIMEOUT_MS", bad)
        assert fullload.timeout_ms() == 180_000, bad
