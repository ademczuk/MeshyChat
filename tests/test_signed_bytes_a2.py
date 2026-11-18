"""B02 phase A2: a shared_memory row without the bytes its writer signed is refused from every
session. Divergence 051, sql_meshychat/025; design docs/design/signed-bytes-status.md on
fin/design-signed-bytes (9aab90b), section 4.1's A2 row, 4.2, 4.3's A2 down and Q10.

  R   the refusal: a row with NULL `signed_bytes` is refused from this build's session (A1
      already refused it there), from an unmarked `meshychat_app` session, and from the
      owner role that the memory daemon uses; each beside the same row WITH its bytes, which
      lands from the same session;
  H   history is untouched: a new version written through handle_write over a head stored
      without bytes lands and retires that head (an UPDATE, which A2 does not bind), and the
      old row stays as it was, still classified by the verdict job;
  D   the down order is enforced: while 025 is live, 014's down script refuses (025's WHEN
      pins the column) and changes nothing; after 025's down script, 014's runs; and 025's
      down gives back A1's admission exactly (NULL lands from the owner, is still refused
      from this build's session);
  S   tests/fixtures/history_rows.seeding_history, the only way a test now writes a row
      without bytes, disables A2's trigger only inside its own transaction, leaves 014's on,
      and refuses a connection with an open transaction.

HOUSE RULES (docs/V1-DESIGN-RULES.md C1): every refusal sits beside its control, one variable
apart; refusals are matched on the SQLSTATE and on the code the trigger raises.

Run:  .venv/Scripts/python.exe -m pytest tests/test_signed_bytes_a2.py -q
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import psycopg
import psycopg.errors
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from apps.core import rls, rollback, schema_sets  # noqa: E402
from apps.memoryskills import db, handlers  # noqa: E402
from tests.fixtures import history_rows  # noqa: E402
from tests.fixtures import signed_rows as sr  # noqa: E402

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")
DOWN_DIR = ROOT / "sql_meshychat" / "down"
DOWN_014 = DOWN_DIR / "014_shared_memory_signed_bytes_down.sql"
A2_TRIGGER = "shared_memory_signed_bytes_required_trg"
A1_TRIGGER = "shared_memory_signed_bytes_agree_trg"


# --------------------------------------------------------------------------- fixtures

def _peer(conn, tag: str) -> dict:
    owner_id, peer_id = uuid.uuid4(), uuid.uuid4()
    conn.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id) "
                 "VALUES (%s,%s,'dev',%s)",
                 (owner_id, f"a2-{tag}-{owner_id.hex[:8]}@example.test", uuid.uuid4()))
    conn.execute("INSERT INTO peers (id, owner_id, name, display_name) VALUES (%s,%s,%s,%s)",
                 (peer_id, owner_id, f"a2-{tag}-{peer_id.hex[:8]}", tag))
    conn.commit()
    return {"peer_id": peer_id, "owner_id": owner_id}


def _namespace(conn, peer: dict) -> str:
    name = f"a2test/{uuid.uuid4().hex[:12]}"
    conn.execute("INSERT INTO shared_memory_namespace (namespace, owner_peer_id, "
                 " owner_owner_id, retention_policy) VALUES (%s,%s,%s,'keep_all')",
                 (name, peer["peer_id"], peer["owner_id"]))
    conn.commit()
    return name


@pytest.fixture()
def owner_conn():
    """The owner role `meshy` bypasses RLS; the memory daemon connects as this role."""
    with psycopg.connect(DSN) as conn:
        yield conn


@pytest.fixture()
def world(owner_conn):
    w = _peer(owner_conn, "writer")
    return {"writer": w, "ns": _namespace(owner_conn, w)}


def _this_builds_session(dsn: str):
    """meshychat_app with apps.core.rls.set_role's marker, verified by db.connect."""
    return db.connect(dsn)


def _historical_build_session(dsn: str):
    """The version-024 application session, used only after this test rolls back to 024."""
    conn = psycopg.connect(dsn, autocommit=True)
    old_role = "meshy_" + "app"
    rls.set_role(conn, old_role)
    return conn


def _direct_server_session(dsn: str):
    """An unmarked database-writer session: bootstrap login, then `SET ROLE meshychat_app`,
    without the signed-bytes writer marker."""
    conn = psycopg.connect(dsn, autocommit=True)
    conn.execute("SET ROLE meshychat_app")
    assert conn.execute("SELECT current_user").fetchone()[0] == "meshychat_app", "control"
    assert conn.execute("SELECT current_setting('meshy.signed_bytes_writer', true)"
                        ).fetchone()[0] in (None, ""), "control: no marker"
    return conn


def _insert_from(session: str, conn, cols: dict):
    """One INSERT from `session`, committed or rolled back as a unit."""
    if session == "this_build":
        with rls.server_internal(conn):
            return sr.insert(conn, cols)
    if session == "direct_server":
        with conn.transaction():
            conn.execute("SELECT set_config('app.server_internal', '1', true)")
            return sr.insert(conn, cols)
    try:
        rid = sr.insert(conn, cols)
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return rid


def _open(session: str, dsn: str):
    if session == "this_build":
        return _this_builds_session(dsn)
    if session == "direct_server":
        return _direct_server_session(dsn)
    return psycopg.connect(dsn)


def _exists(owner_conn, rid) -> bool:
    row = owner_conn.execute("SELECT 1 FROM shared_memory WHERE id = %s", (rid,)).fetchone()
    owner_conn.commit()
    return row is not None


SESSIONS = ("this_build", "direct_server", "owner")


# ======================================================================== R: the refusal

@pytest.mark.parametrize("session", SESSIONS)
def test_r_a_row_without_bytes_is_refused_from_every_session(session, owner_conn, world):
    """Design 4.1, A2: "the trigger's NULL admission removed for every role". On A1 alone
    (origin/main at c4d2125) only `this_build` is refused; the unmarked application session and
    the owner land the row, held to nothing. One variable apart: the same payload's row
    WITH its bytes lands from the same session, so the session is not refused, the NULL is."""
    w = world["writer"]
    conn = _open(session, DSN)
    try:
        p = sr.payload(w["peer_id"], world["ns"], f"k/{uuid.uuid4().hex[:8]}", {"x": 1})
        bare = sr.columns(p, peer_id=w["peer_id"], owner_id=w["owner_id"], with_bytes=False)
        with pytest.raises(psycopg.errors.CheckViolation, match=r"^E_SIGNED_BYTES_REQUIRED: "):
            _insert_from(session, conn, bare)
        assert not _exists(owner_conn, bare["id"])

        whole = sr.columns(p, peer_id=w["peer_id"], owner_id=w["owner_id"])
        assert whole["signed_bytes"] is not None, "control: the one variable"
        assert {k: v for k, v in whole.items() if k != "signed_bytes"}.keys() == bare.keys()
        assert _exists(owner_conn, _insert_from(session, conn, whole))
    finally:
        conn.close()


def test_r_the_refusal_names_the_row_and_the_divergence(owner_conn, world):
    """The owner's refusal comes from 025's trigger (014's admits the owner), and says which
    row and why, the way 011 and 014 name theirs."""
    w = world["writer"]
    key = f"k/{uuid.uuid4().hex[:8]}"
    p = sr.payload(w["peer_id"], world["ns"], key, {"x": 1})
    with pytest.raises(psycopg.errors.CheckViolation) as exc:
        _insert_from("owner", owner_conn,
                     sr.columns(p, peer_id=w["peer_id"], owner_id=w["owner_id"],
                                with_bytes=False))
    assert exc.value.diag.message_primary == (
        f"E_SIGNED_BYTES_REQUIRED: every insert must store the signed bytes of "
        f"{world['ns']}/{key} (divergence 051, B02 A2)")
    assert exc.value.diag.context and "shared_memory_signed_bytes_required" in (
        exc.value.diag.context)


# ================================================================ H: history is untouched

def test_h_a_new_version_over_a_head_stored_without_bytes_lands_and_retires_it(owner_conn):
    """Production holds every row written before 014 without bytes, and a write to one of
    those keys retires its head (an UPDATE of is_head). A2 binds INSERT only: the new version
    lands with its bytes, the old one keeps its NULL and is still a legacy row the verdict
    job classifies. Control: the historical row really has no bytes."""
    from apps.memoryskills import legacy_verdict
    from tests.test_memory_skills import _envelope
    from tests.test_signed_bytes_classifier import _namespace as _ns, _signer, legacy

    app = db.connect(DSN)
    try:
        signer = _signer(owner_conn, "a2hist")
        ns = _ns(owner_conn, signer)
        key = f"k/{uuid.uuid4().hex[:8]}"
        old = legacy(owner_conn, signer, ns, key=key)
        assert owner_conn.execute("SELECT signed_bytes IS NULL, is_head FROM shared_memory "
                                  "WHERE id = %s", (old,)).fetchone() == (True, True)
        owner_conn.commit()

        handlers.dispatch("memory.write", _envelope(signer, "memory.write", {
            "namespace": ns, "key": key, "value": {"x": 2}}), signer["peer_id"], app)

        rows = owner_conn.execute(
            "SELECT id, version, is_head, parent_id, signed_bytes IS NOT NULL "
            "FROM shared_memory WHERE namespace = %s AND key = %s ORDER BY version",
            (ns, key)).fetchall()
        owner_conn.commit()
        assert [(r[1], r[2], r[4]) for r in rows] == [(1, False, False), (2, True, True)]
        assert rows[1][3] == old
        assert legacy_verdict.classify_ids(app, [old])[old].verdict == "verified"
    finally:
        app.close()


# ================================================================ D: the order of the downs

@pytest.fixture(scope="module")
def a2_dsn():
    """A database of this test's own: the down scripts change the schema, and the run's
    shared database must keep A2 for every other test."""
    admin = DSN.rsplit("/", 1)[0] + "/postgres"
    name = f"meshy_a2_{os.getpid()}_{uuid.uuid4().hex[:6]}"
    dsn = DSN.rsplit("/", 1)[0] + "/" + name
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}"')
    try:
        with psycopg.connect(dsn) as conn:
            schema_sets.apply_all(conn, verbose=False)
            schema_sets.assert_schema_complete(conn)
        yield dsn
    finally:
        with psycopg.connect(admin, autocommit=True) as c:
            c.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                      "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
            c.execute(f'DROP DATABASE IF EXISTS "{name}"')


def _meshychat_set():
    return next(s for s in schema_sets.SETS if s.name == "meshychat")


def _above(conn, floor: int) -> list[int]:
    """The meshychat versions applied above `floor`, newest first: what a rollback to `floor` must
    run, in that order. Read from the ledger rather than written out, so a migration added above
    025 (027 and 028 for redaction, 030 for OI-57, 029 for edit) does not break these tests by
    being new: the property held is ORDER and COMPLETENESS, and 025 last."""
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM meshychat_migrations WHERE version > %s ORDER BY version DESC", (floor,))
        out = [int(r[0]) for r in cur.fetchall()]
    conn.commit()
    assert out[-1] == 25 and {25, 26, 27, 28} <= set(out), f"control: the fixture has A2 and what sits above it: {out}"
    return out


def _catalog_state(conn) -> dict:
    row = conn.execute(
        "SELECT (SELECT count(*) FROM information_schema.columns WHERE table_name = "
        "        'shared_memory' AND column_name = 'signed_bytes'),"
        "       (SELECT array_agg(tgname::text ORDER BY tgname) FROM pg_trigger WHERE "
        "        tgrelid = 'shared_memory'::regclass AND tgname LIKE 'shared_memory_signed%%'),"
        "       (SELECT array_agg(version ORDER BY version) FROM meshychat_migrations "
        "        WHERE version IN (14, 25))").fetchone()
    conn.commit()
    return {"column": row[0], "triggers": row[1] or [], "ledger": row[2] or []}


def test_d_a1s_down_script_refuses_while_a2_is_live_and_runs_after_a2s(a2_dsn):
    """Design 4.3: "The runbook applies A2's down script before any code rollback". 025's
    WHEN pins signed_bytes (pg_depend), so 014's down script, run first, fails on its DROP
    COLUMN and its transaction takes nothing with it. Without the pin it would drop the
    column and leave 025's function naming it, and every insert after that would fail.
    One variable apart: after the rollback gate runs 025's down script, 014's runs."""
    owner = psycopg.connect(a2_dsn)
    try:
        live = _catalog_state(owner)
        assert live == {"column": 1, "triggers": [A1_TRIGGER, A2_TRIGGER],
                        "ledger": [14, 25]}, "control: A1 and A2 are applied"

        with pytest.raises(psycopg.errors.DependentObjectsStillExist) as exc:
            with owner.transaction():
                owner.execute(DOWN_014.read_text("utf-8"))
        assert "signed_bytes" in exc.value.diag.message_primary
        assert A2_TRIGGER in (exc.value.diag.message_detail or "")
        assert _catalog_state(owner) == live, "the refused down script changed nothing"

        # Newest first: D08's 026 sits above A2 (integ/r20-maint), redaction's 027 and 028 above that (divergence 053), and
        # edit's 029 above those (divergence 059), so the way back to 24 runs their down scripts before 025's. 026 and 028
        # touch nothing of shared_memory; 027 restores 014's immutability function, which A2 does not read; 029 restores
        # 011's and 014's INSERT functions, and 014's agree function is the one A1 and A2 stand on.
        expected = _above(owner, 24)            # read BEFORE the rollback empties it
        assert rollback.rollback(owner, _meshychat_set(), 24) == expected
        assert _catalog_state(owner) == {"column": 1, "triggers": [A1_TRIGGER],
                                         "ledger": [14]}
        with owner.transaction():
            owner.execute(DOWN_014.read_text("utf-8"))
        assert _catalog_state(owner) == {"column": 0, "triggers": [], "ledger": []}
    finally:
        schema_sets.apply_all(owner, verbose=False)
        schema_sets.assert_schema_complete(owner)
        owner.commit()
        owner.close()


def test_d_a2s_down_script_gives_back_a1s_admission_exactly(a2_dsn):
    """Design 4.3, "A2 down: restore the NULL admission". After it, A1 is what it was: a
    row without bytes lands from the owner and is refused from this build's session, by
    014's trigger (divergence 039's message). Control first, on the live A2 schema: the
    owner's row is refused."""
    owner = psycopg.connect(a2_dsn)
    app = None
    try:
        w = _peer(owner, "a2down")
        ns = _namespace(owner, w)

        def bare():
            p = sr.payload(w["peer_id"], ns, f"k/{uuid.uuid4().hex[:8]}", {"x": 1})
            return sr.columns(p, peer_id=w["peer_id"], owner_id=w["owner_id"],
                              with_bytes=False)

        with pytest.raises(psycopg.errors.CheckViolation, match=r"divergence 051"):
            _insert_from("owner", owner, bare())

        expected = _above(owner, 24)            # read BEFORE the rollback empties it
        assert rollback.rollback(owner, _meshychat_set(), 24) == expected   # newest first, 025 last
        assert _exists(owner, _insert_from("owner", owner, bare()))
        app = _historical_build_session(a2_dsn)
        with pytest.raises(psycopg.errors.CheckViolation,
                           match=r"^E_SIGNED_BYTES_REQUIRED: meshy_app must store .*divergence 039"):
            _insert_from("this_build", app, bare())
    finally:
        if app is not None:
            app.close()
        schema_sets.apply_all(owner, verbose=False)
        schema_sets.assert_schema_complete(owner)
        owner.commit()
        owner.close()


# ======================================================== S: the test suite's history seeder

def _a2_enabled(conn) -> str:
    got = conn.execute("SELECT tgenabled FROM pg_trigger WHERE tgname = %s",
                       (A2_TRIGGER,)).fetchone()[0]
    conn.commit()
    return got


def test_s_the_history_seeder_disables_a2_only_inside_its_own_transaction(owner_conn, world):
    """Inside the block a row without bytes lands, while another session still reads the
    trigger as enabled ('O': the ALTER is not committed). After the block it is enabled on
    the committed catalog and the same kind of row is refused again (the control)."""
    w = world["writer"]
    other = psycopg.connect(DSN)
    try:
        def bare():
            p = sr.payload(w["peer_id"], world["ns"], f"k/{uuid.uuid4().hex[:8]}", {"x": 1})
            return sr.columns(p, peer_id=w["peer_id"], owner_id=w["owner_id"],
                              with_bytes=False)

        with history_rows.seeding_history(owner_conn):
            rid = sr.insert(owner_conn, bare())
            assert _a2_enabled(other) == "O"
        assert _exists(owner_conn, rid)
        assert _a2_enabled(other) == "O"
        with pytest.raises(psycopg.errors.CheckViolation, match=r"divergence 051"):
            _insert_from("owner", owner_conn, bare())
    finally:
        other.close()


def test_s_the_history_seeder_leaves_a1_on(owner_conn, world):
    """It disables 025's trigger and not 014's: inside the block, a session marked as this
    build's is still refused a row without bytes, by A1. Control, in the same block: the
    owner's row lands."""
    w = world["writer"]

    def bare():
        p = sr.payload(w["peer_id"], world["ns"], f"k/{uuid.uuid4().hex[:8]}", {"x": 1})
        return sr.columns(p, peer_id=w["peer_id"], owner_id=w["owner_id"], with_bytes=False)

    with history_rows.seeding_history(owner_conn):
        rid = sr.insert(owner_conn, bare())
        with pytest.raises(psycopg.errors.CheckViolation, match=r"divergence 039"):
            with owner_conn.transaction():
                owner_conn.execute("SET LOCAL ROLE meshychat_app")
                owner_conn.execute(f"SELECT set_config('{rls.SIGNED_BYTES_WRITER_SETTING}',"
                                   " 'on', true)")
                owner_conn.execute("SELECT set_config('app.server_internal', '1', true)")
                sr.insert(owner_conn, bare())
    assert _exists(owner_conn, rid)


def test_s_the_history_seeder_refuses_a_connection_with_an_open_transaction(owner_conn):
    """Inside a caller's open transaction the ACCESS EXCLUSIVE lock would outlive the block
    and a test that then called the app would wait on its own idle connection. Control: the
    same connection, idle, is accepted."""
    owner_conn.execute("SELECT 1")
    with pytest.raises(history_rows.NotIdle):
        with history_rows.seeding_history(owner_conn):
            pass
    owner_conn.rollback()
    with history_rows.seeding_history(owner_conn):
        pass
    assert _a2_enabled(owner_conn) == "O"


# ======================================================== T: the calibration tool's seeding

def test_t_the_calibration_tool_seeds_rows_a2_admits(a2_dsn):
    """tools/related_calibration.py seeds its throwaway database by direct INSERT, as the
    owner. It wrote rows without bytes, which 025 refuses, so the tool could no longer run.
    It now stores each synthetic payload's canonical bytes; 014's agree-trigger checks them
    against the row. Control: the rows landed as head rows of the mesh namespace, each with
    bytes that hash to its payload_jcs_sha256."""
    from tools import related_calibration as relcal

    fx = {"messages": [
        {"id": "m1", "author": f"a2cal-{uuid.uuid4().hex[:6]}", "t": 0,
         "text": "synthetic one", "thread": "th-a2"},
        {"id": "m2", "author": f"a2cal-{uuid.uuid4().hex[:6]}", "t": 1,
         "text": "synthetic two", "thread": "th-a2", "reply_to": "m1"}]}
    owner = psycopg.connect(a2_dsn)
    try:
        assert _catalog_state(owner)["triggers"] == [A1_TRIGGER, A2_TRIGGER], "control: A2"
        ids = relcal._seed(a2_dsn, fx)
        got = owner.execute(
            "SELECT count(*), count(*) FILTER (WHERE is_head AND namespace = %s AND "
            " encode(sha256(signed_bytes), 'hex') = payload_jcs_sha256) "
            "FROM shared_memory WHERE id = ANY(%s)",
            (relcal.NS, [uuid.UUID(v["id"]) for v in ids.values()])).fetchone()
        owner.commit()
        assert got == (2, 2)
    finally:
        owner.close()
