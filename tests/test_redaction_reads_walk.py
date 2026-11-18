"""A redaction reads the stored read results by the index, a page at a time (apps/memoryskills/redact.py).

The first build tested the key inside one UPDATE over `skill_calls`. On a copy of production
(3,007,573 calls, 146 unpruned reads) the planner read the whole table for it, 25 s, and the redaction
died on the 30 s statement timeout (rehearsal of 2026-10-08). No test saw it, because a test database
holds a few hundred calls: the result was right and only the cost was wrong. So these tests look at
the STATEMENTS a redaction sends, which is where the defect was, and at the walk across pages.

Run:  .venv/Scripts/python.exe -m pytest tests/test_redaction_reads_walk.py -q -p no:cacheprovider
"""
from __future__ import annotations

import re
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from apps.memoryskills import redact  # noqa: E402
from apps.memoryskills.errors import HandlerError  # noqa: E402
from tests.test_memory_skills import app_conn, owner_conn  # noqa: E402,F401
from tests.test_redaction import (  # noqa: E402
    VALUE, _call_row, _redact, _stored_call, _world, _write, census)

INDEX = "skill_calls_result_prune_v2_idx"


def _reads_naming(owner_conn, peer, ns, key, marker, n):
    ids = []
    for _ in range(n):
        call_id = f"call-{uuid.uuid4()}"
        _stored_call(owner_conn, peer, "memory.read", call_id, args={"args": {"namespace": ns, "key": key}},
                     result={"op": "memory.read", "memo": {"key": key, "value": {"body": marker}}},
                     payload={"result": {"memo": {"key": key, "value": {"body": marker}}}})
        ids.append(call_id)
    return ids


def test_every_stored_read_is_blanked_when_they_span_several_pages(app_conn, owner_conn, monkeypatch):
    """Seven reads name the key and three name another, with two rows a page: the walk has to cross
    pages whose rows it has just pruned out of the index. Control: the other key's reads are whole."""
    monkeypatch.setattr(redact, "READS_PAGE", 2)
    marker = f"zq-walk-{uuid.uuid4().hex}"
    author, sibling, stranger, ns = _world(app_conn, owner_conn, "rw1")
    value = dict(VALUE, body=marker, subject="s", ref="r", session_label="l", surprise=1)
    key, other_key = _write(app_conn, author, ns, value=value), _write(app_conn, author, ns, value=value)
    mine = _reads_naming(owner_conn, stranger, ns, key, marker, 4)
    others = _reads_naming(owner_conn, stranger, ns, other_key, marker, 3)
    mine += _reads_naming(owner_conn, stranger, ns, key, marker, 3)

    out = _redact(app_conn, author, ns, key)
    assert out["stored_calls_blanked"]["read_results"] == 7
    for call_id in mine:
        row = _call_row(owner_conn, call_id)
        assert row["result"]["pruned_reason"] == "redaction" and row["result_payload"] is None
    for call_id in others:
        assert _call_row(owner_conn, call_id)["result"]["memo"]["value"]["body"] == marker
    _redact(app_conn, author, ns, other_key)
    assert census(owner_conn, marker) == []


def test_the_walk_returns_every_unpruned_read_once(app_conn, owner_conn, monkeypatch):
    """The walk by itself, pages of 2 against pages of 500: the same ids, none twice. A read that is
    already pruned, and a write, are not in it."""
    author, sibling, stranger, ns = _world(app_conn, owner_conn, "rw2")
    key = _write(app_conn, author, ns)
    _reads_naming(owner_conn, stranger, ns, key, "m", 5)
    pruned, written = f"call-{uuid.uuid4()}", f"call-{uuid.uuid4()}"
    _stored_call(owner_conn, stranger, "memory.read", pruned, args={"args": {}}, result={"ok": True, "pruned": True})
    _stored_call(owner_conn, stranger, "memory.write", written, args={"args": {}}, result={"op": "memory.write"})
    with owner_conn.cursor() as cur:
        whole = [i for page in redact._unpruned_reads(cur) for i in page]
        monkeypatch.setattr(redact, "READS_PAGE", 2)
        paged = [i for page in redact._unpruned_reads(cur) for i in page]
        cur.execute("SELECT id FROM skill_calls WHERE call_id = ANY(%s)", ([pruned, written],))
        outside = {r[0] for r in cur.fetchall()}
    owner_conn.commit()
    assert len(whole) >= 5 and len(set(whole)) == len(whole)
    assert paged == whole
    assert len(outside) == 2 and not outside & set(whole)


class _Recording:
    """A cursor that keeps every statement sent through it."""

    def __init__(self, cur, sent):
        self._cur, self._sent = cur, sent

    def execute(self, query, params=None):
        self._sent.append(" ".join(str(query).split()))
        return self._cur.execute(query, params)

    def __getattr__(self, name):
        return getattr(self._cur, name)


def test_no_statement_tests_the_key_against_the_whole_table(app_conn, owner_conn, monkeypatch):
    """THE DEFECT, where it was. Every statement of the stored-calls step that looks into a call's
    result for the key names its rows by id; and every statement that selects the unpruned reads is
    bounded and ordered as the index is. A statement with the key test beside the read predicate, as
    the first build had, fails the first assertion."""
    monkeypatch.setattr(redact, "READS_PAGE", 1)      # so the NEXT-page statement is sent too (memory-bxxxxx on #178)
    author, sibling, stranger, ns = _world(app_conn, owner_conn, "rw3")
    key = _write(app_conn, author, ns)
    _reads_naming(owner_conn, stranger, ns, key, "m", 2)
    sent: list[str] = []
    with owner_conn.cursor() as cur:
        out = redact._blank_stored_calls(_Recording(cur, sent), ns, key, [])
    owner_conn.rollback()
    assert out["read_results"] == 2, "control: the step did its work through the recording cursor"
    looks = [q for q in sent if "position(" in q]
    walks = [q for q in sent if "'memory.query'" in q]
    assert looks and walks, sent
    for q in looks:
        assert "id = ANY(" in q and "'memory.query'" not in q, q
    for q in walks:
        assert q.endswith("ORDER BY completed_at, id LIMIT %s") and "position(" not in q, q
    assert sum("(completed_at, id) > (%s, %s)" in q for q in walks) >= 2, "control: next-page statements were sent"


def test_the_walks_statements_are_planned_on_the_index(app_conn, owner_conn):
    """Both statements of the walk, as Postgres plans them here. `enable_seqscan` is left on: a
    predicate that stopped matching the index's would fall back to the table and fail this."""
    with owner_conn.cursor() as cur:
        first = "\n".join(r[0] for r in cur.execute("EXPLAIN " + redact._READS_FIRST, (500,)).fetchall())
        nxt = "\n".join(r[0] for r in cur.execute(
            "EXPLAIN " + redact._READS_NEXT, ("2026-01-01T00:00:00Z", str(uuid.uuid4()), 500)).fetchall())
        cur.execute("SELECT indexdef FROM pg_indexes WHERE indexname = %s", (INDEX,))
        assert cur.fetchone() is not None, "control: the index exists in this database"
    owner_conn.rollback()
    assert INDEX in first and "Seq Scan" not in first, first
    assert INDEX in nxt and "Seq Scan" not in nxt, nxt
    assert re.search(r"Index Cond: \(ROW\(completed_at, id\) >", nxt), nxt


def test_a_read_still_naming_the_key_refuses_the_redaction(app_conn, owner_conn, monkeypatch):
    """The postcondition walks too. With the blanking made to do nothing, the redaction is refused
    and the row keeps its text. Control: the same redaction with the blanking in place succeeds."""
    marker = f"zq-post-{uuid.uuid4().hex}"
    author, sibling, stranger, ns = _world(app_conn, owner_conn, "rw4")
    key = _write(app_conn, author, ns, value=dict(VALUE, body=marker, subject="s", ref="r",
                                                  session_label="l", surprise=1))
    # One row a page, and an older read that names another key first: the leftover is never on the
    # check's first page, so a check that read one page only would pass it (memory-bxxxxx on #178).
    monkeypatch.setattr(redact, "READS_PAGE", 1)
    _reads_naming(owner_conn, stranger, ns, "k-older-other", "other", 1)
    _reads_naming(owner_conn, stranger, ns, key, marker, 3)
    real = redact._unpruned_reads
    calls = []

    def first_walk_empty(cur):
        calls.append(1)
        return iter(()) if len(calls) == 1 else real(cur)

    monkeypatch.setattr(redact, "_unpruned_reads", first_walk_empty)
    with pytest.raises(HandlerError) as err:
        _redact(app_conn, author, ns, key)
    app_conn.rollback()
    assert "still there after the blanking" in str(err.value)
    assert "shared_memory.value" in census(owner_conn, marker)
    monkeypatch.setattr(redact, "_unpruned_reads", real)
    assert _redact(app_conn, author, ns, key)["stored_calls_blanked"]["read_results"] == 3
    assert census(owner_conn, marker) == []


def test_a_read_that_names_the_key_only_in_its_signed_payload_is_blanked(app_conn, owner_conn):
    """`result` and `result_payload` are two copies and either can be the one that names the key.
    Control: a read whose two copies name another key is whole."""
    marker = f"zq-pay-{uuid.uuid4().hex}"
    author, sibling, stranger, ns = _world(app_conn, owner_conn, "rw5")
    key = _write(app_conn, author, ns, value=dict(VALUE, body=marker, subject="s", ref="r",
                                                  session_label="l", surprise=1))
    only_payload, other = f"call-{uuid.uuid4()}", f"call-{uuid.uuid4()}"
    _stored_call(owner_conn, stranger, "memory.read", only_payload, args={"args": {"namespace": ns}},
                 result={"op": "memory.read"}, payload={"result": {"memo": {"key": key, "value": {"body": marker}}}})
    _stored_call(owner_conn, stranger, "memory.read", other, args={"args": {"namespace": ns}},
                 result={"op": "memory.read", "memo": {"key": "k-other"}}, payload={"result": {"memo": {"key": "k-other"}}})
    assert "skill_calls.result_payload" in census(owner_conn, marker)
    assert _redact(app_conn, author, ns, key)["stored_calls_blanked"]["read_results"] == 1
    assert _call_row(owner_conn, only_payload)["result_payload"] is None
    assert _call_row(owner_conn, other)["result_payload"]["result"]["memo"]["key"] == "k-other"
    assert census(owner_conn, marker) == []
