"""sql_meshychat/023 applied to a POPULATED database, and taken back (STATUS D19).

tests/test_rollback_gate.py already holds 023's down script to the catalog on an empty
database. This file holds the parts the catalog cannot see, on a database that has mesh rows
and walks in progress when 023 arrives:

  * the backfill stamps every existing shared/mesh-chat row in the walks' OLD order,
    (created_at, id), and nothing outside the namespace;
  * each walk's saved (watermark_at, watermark_id) is translated to the seq of the last row
    at or below it, so the walk resumes exactly where it was: the rows it had walked sit at
    or below `watermark_seq`, the rows it had not sit above;
  * meshychat_app can read the order only under server_internal and cannot write it at all (the
    trigger function is its only writer), with the positive controls beside each refusal;
  * the down script removes 023's objects, leaves the walks' own (watermark_at, watermark_id)
    as the old code expects them, and 023 applies again with the same translation.

It builds and drops a database of its own: the down script changes the schema.

Run:  .venv313/Scripts/python.exe -m pytest tests/test_commit_order_migration.py -q
"""
from __future__ import annotations

import datetime as _dt
import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from apps.core import rls, rollback, schema_sets, sqlmigrate  # noqa: E402
from tests.fixtures import meshsem_db as db  # noqa: E402

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")


def _admin(dsn: str) -> str:
    return dsn.rsplit("/", 1)[0] + "/postgres"


@pytest.fixture(scope="module")
def at_022():
    """A database migrated through meshychat 022, and not 023."""
    name = f"meshy_co_{os.getpid()}_{uuid.uuid4().hex[:6]}"
    dsn = DSN.rsplit("/", 1)[0] + "/" + name
    with psycopg.connect(_admin(DSN), autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}"')
    try:
        with psycopg.connect(dsn) as conn:
            for s in schema_sets.SETS:
                sqlmigrate.migrate(conn, sql_dir=s.sql_dir, ledger=s.ledger, verbose=False,
                                   upto=22 if s.name == "meshychat" else None)
        yield dsn
    finally:
        with psycopg.connect(_admin(DSN), autocommit=True) as c:
            c.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                      "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
            c.execute(f'DROP DATABASE IF EXISTS "{name}"')


def _meshychat():
    return next(s for s in schema_sets.SETS if s.name == "meshychat")


def _order(conn) -> dict:
    """memory_id -> seq, read as the owner."""
    return {str(r[0]): r[1] for r in conn.execute(
        "SELECT memory_id, seq FROM mesh_commit_order").fetchall()}


def test_023_stamps_history_in_the_old_order_and_translates_the_saved_walks(at_022):
    base = _dt.datetime(2026, 9, 1, tzinfo=_dt.timezone.utc)
    with psycopg.connect(at_022, autocommit=True) as owner:
        db.ensure_namespace(owner)
        p = db.peer(owner, "co")
        # Inserted in an order that is NOT the (created_at, id) order, so a backfill that
        # followed insertion order or id order would be seen.
        rows = [db.row(owner, p, {"body": f"co {i}"},
                       created_at=base + _dt.timedelta(minutes=m))
                for i, m in enumerate((30, 10, 50, 20, 40))]
        ordered = [r for _m, r in sorted(zip((30, 10, 50, 20, 40), rows),
                                         key=lambda t: t[0])]
        other = uuid.uuid4()
        owner.execute("INSERT INTO shared_memory_namespace (namespace, owner_peer_id, "
                      "owner_owner_id, claimant_plane, ephemeral, retention_policy) "
                      "VALUES ('co/other', %s, %s, 'dev', FALSE, 'keep_all')",
                      (p["peer_id"], p["owner_id"]))
        owner.execute(
            "INSERT INTO shared_memory (id, namespace, key, version, is_head, value, "
            "provenance_jsonb, payload_jcs_sha256, created_by_peer_id, created_by_owner_id, "
            "author_label) VALUES (%s, 'co/other', 'k', 1, TRUE, '{}', "
            "'{\"payload\": {\"call_id\": \"call-co\"}}', %s, %s, %s, 'co')",
            (other, uuid.uuid4().hex * 2, p["peer_id"], p["owner_id"]))
        # Two walks in progress: the projection past the third row in time order, one embed
        # space past the first. A third space that never started stays NULL.
        mid, first = ordered[2], ordered[0]
        owner.execute("INSERT INTO mesh_edges_projection (projection, watermark_at, "
                      "watermark_id) SELECT 'mesh_edges', created_at, id FROM shared_memory "
                      "WHERE id = %s", (mid["id"],))
        owner.execute("INSERT INTO mesh_embeddings_projection (space, watermark_at, "
                      "watermark_id) SELECT 'co-space', created_at, id FROM shared_memory "
                      "WHERE id = %s", (first["id"],))
        owner.execute("INSERT INTO mesh_embeddings_projection (space) VALUES ('co-fresh')")

    with psycopg.connect(at_022) as conn:
        summary = sqlmigrate.migrate(conn, sql_dir=_meshychat().sql_dir,
                                     ledger=_meshychat().ledger, verbose=False, upto=23)
    assert summary["versions"] == [23]

    with psycopg.connect(at_022, autocommit=True) as owner:
        order = _order(owner)
        seqs = [order[str(r["id"])] for r in ordered]
        assert seqs == sorted(seqs), "history is stamped in (created_at, id) order"
        assert str(other) not in order, "a row outside shared/mesh-chat is not stamped"
        wm = dict(owner.execute("SELECT projection, watermark_seq FROM mesh_edges_projection"
                                ).fetchall())
        assert wm["mesh_edges"] == order[str(mid["id"])]
        ew = dict(owner.execute("SELECT space, watermark_seq FROM mesh_embeddings_projection"
                                ).fetchall())
        assert ew == {"co-space": order[str(first["id"])], "co-fresh": None}
        walked = {str(r["id"]) for r in ordered[:3]}
        assert {m for m, s in order.items() if s <= wm["mesh_edges"]} >= walked
        assert not ({m for m, s in order.items() if s > wm["mesh_edges"]} & walked)

        # A new row after 023: stamped by the trigger, above every backfilled seq.
        late = db.row(owner, p, {"body": "co late"}, created_at=base)
        assert _order(owner)[str(late["id"])] > max(seqs)

    # This is a version-023 schema, so the historical application role reads only under
    # server_internal and never writes.
    app = psycopg.connect(at_022, autocommit=True)
    try:
        rls.set_role(app, "meshy_" + "app")
        assert app.execute("SELECT count(*) FROM mesh_commit_order").fetchone()[0] == 0
        with rls.server_internal(app):
            n = app.execute("SELECT count(*) FROM mesh_commit_order").fetchone()[0]
        assert n == len(rows) + 1, "control: under server_internal meshy_app reads the order"
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with rls.server_internal(app):
                app.execute("INSERT INTO mesh_commit_order (memory_id) VALUES (%s)",
                            (rows[0]["id"],))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with rls.server_internal(app):
                app.execute("DELETE FROM mesh_commit_order")
    finally:
        app.close()

    # Down, then up again.
    with psycopg.connect(at_022) as conn:
        assert rollback.rollback(conn, _meshychat(), 22) == [23]
    with psycopg.connect(at_022, autocommit=True) as owner:
        gone = owner.execute("SELECT to_regclass('mesh_commit_order'), "
                             "to_regclass('background_job_cursors'), "
                             "to_regclass('skill_call_heartbeats')").fetchone()
        assert gone == (None, None, None)
        cols = {r[0] for r in owner.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'mesh_edges_projection'").fetchall()}
        assert "watermark_seq" not in cols and {"watermark_at", "watermark_id"} <= cols
        kept = owner.execute("SELECT watermark_id FROM mesh_edges_projection "
                             "WHERE projection = 'mesh_edges'").fetchone()[0]
        assert str(kept) == str(mid["id"]), "the old walk's own position survives the down"
        assert owner.execute("SELECT count(*) FROM pg_trigger WHERE tgname = "
                             "'shared_memory_mesh_commit_order'").fetchone()[0] == 0
    with psycopg.connect(at_022) as conn:
        again = sqlmigrate.migrate(conn, sql_dir=_meshychat().sql_dir,
                                   ledger=_meshychat().ledger, verbose=False, upto=23)
    assert again["versions"] == [23]
    with psycopg.connect(at_022, autocommit=True) as owner:
        order = _order(owner)
        wm = owner.execute("SELECT watermark_seq FROM mesh_edges_projection "
                           "WHERE projection = 'mesh_edges'").fetchone()[0]
        assert wm == order[str(mid["id"])]
