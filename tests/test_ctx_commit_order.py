"""v1.1 step 6, the schema (sql_meshychat/047): the commit order of transcript-context MANIFESTS, and the index's tables.

What this file holds, each part against a control:
  * COMMIT ORDER, not start order: a manifest whose transaction starts first and commits last draws the HIGHER seq, and a
    reader that sees it sees the other one too (023's protocol on slot 2 of the same advisory key).
  * WHAT IS STAMPED: a manifest (key ctxm/...) in the namespace ctx_index_config names, and nothing else. An item key in
    that namespace, and a manifest key in any other namespace, get no seq (pxx 241bfc: the configured namespace, read from
    the database, not a literal). Changing the config row moves what is stamped, which is the control that it decides.
  * REMOVAL BY REFERENCE (pxx 241bfc addition c): ctx_vectors_orphaned counts a vector only when no chunk occurrence in its
    space names its digest, so deleting one of two occurrences leaves the shared vector referenced.
  * THE DOWN SCRIPT names every object this file creates (the catalog round trip is tests/test_rollback_gate.py's).

Run:  pytest tests/test_ctx_commit_order.py -q -p no:cacheprovider
"""
from __future__ import annotations

import os
import re
import sys
import threading
import uuid
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")

from tests import test_mesh_authorship as mesh  # noqa: E402
from tests.fixtures import signed_rows as sr  # noqa: E402

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")
#: ctx_index_config is ONE row in the shared test database. Every test that changes it holds this session lock across
#: the change and its restore (pxx 2a94c2), so a parallel runner serializes here instead of reading the wrong namespace.
CONFIG_LOCK = (1835233133, 990)
UP = ROOT / "sql_meshychat" / "047_ctx_index_schema.sql"
DOWN = ROOT / "sql_meshychat" / "down" / "047_ctx_index_schema_down.sql"


@pytest.fixture()
def owner():
    with psycopg.connect(DSN, autocommit=True) as conn:
        yield conn


@pytest.fixture()
def ctx_ns(owner):
    """A fresh namespace made the configured context namespace for one test, restored afterwards, with a writer."""
    ns = f"shared/ctxidx-{uuid.uuid4().hex[:10]}"
    owner.execute("SELECT pg_advisory_lock(%s, %s)", CONFIG_LOCK)
    prior = owner.execute("SELECT namespace FROM ctx_index_config WHERE id = 1").fetchone()[0]
    try:
        owner.execute("UPDATE ctx_index_config SET namespace = %s WHERE id = 1", (ns,))
        author = mesh._peer(owner, "cco")
        mesh._admit(owner, ns, author)
        yield ns, author
    finally:
        owner.execute("UPDATE ctx_index_config SET namespace = %s WHERE id = 1", (prior,))
        owner.execute("SELECT pg_advisory_unlock(%s, %s)", CONFIG_LOCK)


def _cols(ns, author, key):
    return sr.row(author["peer_id"], author["owner_id"], ns, key, {"schema": "context.v1", "kind": "manifest"},
                  author_label=author["fallback"])


def _manifest_key(author, batch=1):
    return f"ctxm/{author['fallback']}/{uuid.uuid4().hex}/0001/{batch:06d}"


def _seq(owner, rid):
    row = owner.execute("SELECT seq FROM ctx_commit_order WHERE memory_id = %s AND kind = 'write'", (rid,)).fetchone()
    return None if row is None else row[0]


def test_a_manifest_that_starts_first_and_commits_last_draws_the_higher_seq(owner, ctx_ns):
    ns, author = ctx_ns
    first = psycopg.connect(DSN)            # transaction A: starts first, commits last
    try:
        rid_a = sr.insert(first, _cols(ns, author, _manifest_key(author, 1)))
        with psycopg.connect(DSN) as second:  # transaction B: starts second, commits first
            rid_b = sr.insert(second, _cols(ns, author, _manifest_key(author, 2)))
            second.commit()
        assert _seq(owner, rid_b) is not None and _seq(owner, rid_a) is None, "B is stamped at ITS commit, A not yet"
        first.commit()
    finally:
        first.close()
    assert _seq(owner, rid_a) > _seq(owner, rid_b), "the seq follows commit order, not start order"


def test_a_second_commit_waits_for_the_first_stampers_commit(owner, ctx_ns):
    """The advisory lock is held from the draw to the commit: a stamper that has drawn blocks the next one until it is
    visible, so no reader can see seq N+1 without seq N. CONTROL: the waiter finishes once the holder commits."""
    ns, author = ctx_ns
    holder = psycopg.connect(DSN)
    try:
        holder.execute("SELECT pg_advisory_xact_lock(1835233133, 2)")   # the lock a stamper holds to its commit
        done = threading.Event()

        def writer():
            with psycopg.connect(DSN) as w:
                sr.insert(w, _cols(ns, author, _manifest_key(author)))
                w.commit()
            done.set()

        t = threading.Thread(target=writer, daemon=True)
        t.start()
        assert not done.wait(1.0), "a manifest committed while another stamper held the lock"
        holder.commit()
        assert done.wait(10.0), "control: the waiting manifest commits once the lock is released"
        t.join(5)
    finally:
        holder.close()


def test_only_manifests_in_the_configured_namespace_are_stamped(owner, ctx_ns):
    ns, author = ctx_ns
    with psycopg.connect(DSN, autocommit=True) as c:
        manifest = sr.insert(c, _cols(ns, author, _manifest_key(author)))
        item = sr.insert(c, _cols(ns, author, f"ctx/{author['fallback']}/{uuid.uuid4().hex}/0001/000001/0000000000"))
    assert _seq(owner, manifest) is not None, "control: a manifest in the configured namespace is stamped"
    assert _seq(owner, item) is None, "an ITEM key is never stamped: a batch is walked at its manifest"
    other = f"shared/ctxidx-other-{uuid.uuid4().hex[:8]}"
    mesh._admit(owner, other, author)
    with psycopg.connect(DSN, autocommit=True) as c:
        elsewhere = sr.insert(c, _cols(other, author, _manifest_key(author)))
    assert _seq(owner, elsewhere) is None, "a manifest key in another namespace gets no seq"
    owner.execute("UPDATE ctx_index_config SET namespace = %s WHERE id = 1", (other,))
    with psycopg.connect(DSN, autocommit=True) as c:
        moved = sr.insert(c, _cols(other, author, _manifest_key(author)))
    assert _seq(owner, moved) is not None, "control: the config row is what decides, so moving it moves the stamping"


def test_a_shared_vector_is_orphaned_only_when_no_occurrence_names_it(owner, ctx_ns):
    ns, author = ctx_ns
    space = f"ctxidx-{uuid.uuid4().hex[:8]}"
    digest = "b" * 64
    with psycopg.connect(DSN, autocommit=True) as c:
        a = sr.insert(c, _cols(ns, author, _manifest_key(author, 1)))
        b = sr.insert(c, _cols(ns, author, _manifest_key(author, 2)))
    for rid in (a, b):
        owner.execute("INSERT INTO mesh_ctx_chunks (src_id, space, namespace, chunk_no, seat, src, gen, batch, "
                      "first_ord, last_ord, char_start, char_end, time_first, time_last, text_sha256) VALUES (%s, %s, "
                      "%s, 0, 's', 'x', 1, 1, 0, 0, 0, 1, now(), now(), %s)", (rid, space, ns, digest))
    owner.execute("INSERT INTO mesh_ctx_vectors (space, text_sha256, dim, vector) VALUES (%s, %s, 1, '\\x00000000')",
                  (space, digest))
    orphaned = lambda: owner.execute("SELECT ctx_vectors_orphaned(%s)", (space,)).fetchone()[0]  # noqa: E731
    assert orphaned() == 0
    owner.execute("DELETE FROM mesh_ctx_chunks WHERE src_id = %s AND space = %s", (a, space))
    assert orphaned() == 0, "one occurrence left: the shared vector is still referenced"
    owner.execute("DELETE FROM mesh_ctx_chunks WHERE src_id = %s AND space = %s", (b, space))
    assert orphaned() == 1, "control: with no occurrence left the vector is counted as orphaned"
    owner.execute("DELETE FROM mesh_ctx_vectors WHERE space = %s", (space,))


def test_the_down_script_drops_every_object_the_migration_creates():
    up, down = UP.read_text(encoding="utf-8"), DOWN.read_text(encoding="utf-8")
    created = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", up))
    created |= {m.split(".")[-1] for m in re.findall(r"CREATE OR REPLACE FUNCTION ([\w.]+)\(", up)}
    created |= set(re.findall(r"CREATE CONSTRAINT TRIGGER (\w+)", up))
    assert created == {"ctx_index_config", "ctx_commit_order", "ctx_index_state", "mesh_ctx_chunks", "mesh_ctx_vectors",
                       "ctx_dead_letter", "ctx_source_gen", "meshy_ctx_commit_stamp", "ctx_vectors_orphaned",
                       "shared_memory_ctx_commit_order"}, sorted(created)
    for name in created:
        assert re.search(r"DROP (TABLE|FUNCTION|TRIGGER) IF EXISTS (public\.)?" + name + r"\b", down), name
