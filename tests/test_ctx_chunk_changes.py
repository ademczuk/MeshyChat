"""sql_meshychat/049: the per-space chunk change counter, the shard plan cache's freshness stamp (codex-pxx-cli on #396).

What this file holds, each part against a control:
  * ONE BUMP PER TRANSACTION AND SPACE, at commit: a transaction that writes three chunks moves the counter by one, and
    another connection does not see the bump before the commit (the control: it does after).
  * DELETES MOVE IT, cascades included: a chunk deleted directly, and one deleted by its manifest's shared_memory row
    going (ON DELETE CASCADE), each move the counter; the newest chunk id, the stamp it replaces, does not move for either.
  * A WARM PLAN DROPS A DELETED CHUNK (P2-1): after a delete the next search replans and does not return the chunk.
  * WHO WRITES IT: the request role cannot write the table (the definer trigger is its only writer).
  * THE DOWN SCRIPT names every object 049 creates.

Run:  pytest tests/test_ctx_chunk_changes.py -q -p no:cacheprovider
"""
from __future__ import annotations

import re
import uuid

import psycopg
import pytest

from apps.core import dbconn
from apps.meshsem import ctxindex
from tests import test_mesh_authorship as mesh
from tests.fixtures import signed_rows as sr
from tests.test_ctx_vector_store import DSN, NEEDLE, ROOT, _add, _away, owner, world  # noqa: F401 (fixtures)

UP = ROOT / "sql_meshychat" / "049_ctx_chunk_changes.sql"
DOWN = ROOT / "sql_meshychat" / "down" / "049_ctx_chunk_changes_down.sql"


def _changes(conn, space) -> int:
    row = conn.execute("SELECT changes FROM ctx_chunk_changes WHERE space = %s", (space.name,)).fetchone()
    return 0 if row is None else row[0]


def _newest(conn, space):
    return conn.execute("SELECT max(id) FROM mesh_ctx_chunks WHERE space = %s", (space.name,)).fetchone()[0]


@pytest.fixture()
def counted(world):
    """world's teardown deletes the space's counter row after its chunks (whose delete bumps it)."""
    return world


def test_a_transaction_bumps_once_per_space_and_only_at_its_commit(counted):
    w = counted
    before = _changes(w["owner"], w["space"])
    with psycopg.connect(DSN) as tx:                      # one transaction, three chunks
        inner = dict(w, owner=tx)
        for _ in range(3):
            _add(inner)
        assert _changes(w["owner"], w["space"]) == before, "another connection saw the bump before the commit"
        tx.commit()
    w["n"] = inner["n"]                                   # inner was a copy: carry its digest counter back (pxx 0aec5a)
    assert _changes(w["owner"], w["space"]) == before + 1, "three rows in one transaction must bump exactly once"
    _add(w)                                               # control: the next (autocommit) transaction bumps again
    assert _changes(w["owner"], w["space"]) == before + 2


def test_a_direct_delete_and_a_cascade_each_move_the_counter_but_not_the_newest_id(counted):
    w = counted
    keep = _add(w)
    gone = _add(w)
    newest = _newest(w["owner"], w["space"])
    n = _changes(w["owner"], w["space"])
    w["owner"].execute("DELETE FROM mesh_ctx_chunks WHERE id = %s", (keep,))
    assert _changes(w["owner"], w["space"]) == n + 1 and _newest(w["owner"], w["space"]) == newest, \
        "a direct delete must move the counter (and leaves the newest id where it was: the stamp 049 replaces)"

    author = mesh._peer(w["owner"], "ccc")
    mesh._admit(w["owner"], w["ns"], author)
    other = sr.insert(w["owner"], sr.row(author["peer_id"], author["owner_id"], w["ns"],
                                         f"ctxm/x/{uuid.uuid4().hex}/0001/000001",
                                         {"schema": "context.v1", "kind": "manifest"}, author_label=author["fallback"]))
    cascaded = _add(dict(w, src_id=other))
    m = _changes(w["owner"], w["space"])
    w["owner"].execute("DELETE FROM shared_memory WHERE id = %s", (other,))
    assert w["owner"].execute("SELECT count(*) FROM mesh_ctx_chunks WHERE id = %s", (cascaded,)).fetchone()[0] == 0, \
        "control: the manifest's delete cascaded to its chunk"
    assert _changes(w["owner"], w["space"]) == m + 1, "a cascaded delete must move the counter"
    assert gone in [r[0] for r in w["owner"].execute("SELECT id FROM mesh_ctx_chunks WHERE space = %s",
                                                     (w["space"].name,))]


def test_a_warm_plan_does_not_return_a_deleted_chunk(counted, monkeypatch):
    """P2-1: before 049 the plan's stamp was the newest chunk id, which a delete does not move."""
    w = counted
    monkeypatch.setattr(ctxindex, "EXACT_MAX", 0)               # the sharded path, which plans
    monkeypatch.setattr(ctxindex, "_plans", {})
    calls = []
    real = ctxindex._plan_rows
    monkeypatch.setattr(ctxindex, "_plan_rows", lambda *a, **kw: calls.append(1) or real(*a, **kw))
    _add(w, vec=_away(w))
    needle = _add(w, vec=NEEDLE)
    _add(w, vec=_away(w))                                      # the deleted chunk is NOT the newest id
    hits, _cov = ctxindex.search(w["owner"], w["space"], w["ns"], NEEDLE, k=5)
    assert hits and hits[0].chunk_id == needle and len(calls) == 1, "control: the needle is found on the warm plan"
    w["owner"].execute("DELETE FROM mesh_ctx_chunks WHERE id = %s", (needle,))
    hits, cov = ctxindex.search(w["owner"], w["space"], w["ns"], NEEDLE, k=5)
    assert len(calls) == 2, "a delete did not replan"
    assert needle not in [h.chunk_id for h in hits] and not cov.truncated


def test_only_an_update_moves_the_update_count(counted):
    """049's `updates`, which ctxindex folds into a resident shard's version: inserts and deletes leave it alone (the
    shard's row count already sees those), an UPDATE moves it, once per transaction."""
    w = counted
    upd = lambda: w["owner"].execute("SELECT updates FROM ctx_chunk_changes WHERE space = %s",  # noqa: E731
                                     (w["space"].name,)).fetchone()[0]
    a = _add(w)
    b = _add(w)
    assert upd() == 0, "an insert moved the update count"
    with psycopg.connect(DSN) as tx:
        tx.execute("UPDATE mesh_ctx_chunks SET project = 'p' WHERE id = ANY(%s)", ([a, b],))
        tx.commit()
    assert upd() == 1, "two updated rows in one transaction must count once"
    w["owner"].execute("DELETE FROM mesh_ctx_chunks WHERE id = %s", (a,))
    assert upd() == 1, "a delete moved the update count"


def test_the_request_role_cannot_write_the_counter(counted):
    w = counted
    _add(w)
    assert _changes(w["owner"], w["space"]) >= 1, "control: the owner reads the row the trigger wrote"
    app = dbconn.connect(DSN)
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app.execute("UPDATE ctx_chunk_changes SET changes = 0 WHERE space = %s", (w["space"].name,))
    finally:
        app.close()


def test_the_down_script_names_every_object_049_creates():
    up = UP.read_text(encoding="utf-8")
    down = DOWN.read_text(encoding="utf-8")
    made = re.findall(r"CREATE (?:OR REPLACE )?(?:CONSTRAINT )?(TABLE|FUNCTION|TRIGGER)(?: IF NOT EXISTS)? "
                      r"(?:public\.)?(\w+)", up)
    assert sorted(made) == [("FUNCTION", "meshy_ctx_chunk_changed"), ("TABLE", "ctx_chunk_changes"),
                            ("TRIGGER", "mesh_ctx_chunks_changed")], "control: the scan finds 049's three objects"
    for kind, name in made:
        assert re.search(rf"DROP {kind} IF EXISTS (?:public\.)?{name}\b", down), f"down/049 leaves {kind} {name}"
