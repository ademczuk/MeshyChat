"""The A1 down script, applied. Divergence 039; design docs/design/signed-bytes-status.md
(fin/design-signed-bytes) section 4.3 and test 13 of section 4.4.

There were no down-migrations in the runner when A1 landed (apps/core/sqlmigrate.py had no
down step, and checksums every applied file; since the rollback gate `--rollback-to` runs
this script), so A1 ships a named script OUTSIDE the migrate set,
sql_meshychat/down/014_shared_memory_signed_bytes_down.sql, and this test applies it:

  * on a database with rows A1 wrote (bytes stored, derived columns held to them);
  * after it, the schema is the pre-A1 one: no column, no trigger or functions, no verdict
    table, no skill_calls CHECK, 063's immutability function byte for byte, and no ledger
    row for 014, so the migration can be applied again;
  * the pre-A1 handler (apps/memoryskills/handlers.py at PRE_A1, loaded from git) writes;
  * the classifier recovers every A1 row's bytes from provenance_jsonb with the default
    loader and RFC 8785, because the seam admitted only I-JSON numbers (design 4.3);
  * 014 applies again over the result (the round trip).

It builds and drops a database of its own: the down script changes the schema, and the
run's shared database must keep A1 for every other test.

SINCE A2 (divergence 051, sql_meshychat/025) the way back from A1 starts with A2's down
script: 025's trigger pins the signed_bytes column, so 014's down refuses while it is live
(tests/test_signed_bytes_a2.py holds that refusal). Every down here runs 025's first, the
order design 4.3 gives and `sqlmigrate.py --rollback-to` follows (newest first).

Run:  .venv/Scripts/python.exe -m pytest tests/test_signed_bytes_down.py -q
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from apps.core import rls, schema_sets  # noqa: E402
from apps.crypto import primitives as P  # noqa: E402
from apps.memoryskills import db, handlers  # noqa: E402

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")
DOWN = ROOT / "sql_meshychat" / "down" / "014_shared_memory_signed_bytes_down.sql"
DOWN_A2 = ROOT / "sql_meshychat" / "down" / "025_shared_memory_signed_bytes_required_down.sql"
UP_A1 = ROOT / "sql_meshychat" / "014_shared_memory_signed_bytes.sql"
HISTORICAL_A1_ROLE = "meshy_" + "app"

#: origin/main when A1 branched: the handler every rollback returns to.
PRE_A1 = "3fff44212b084e5ce2fbcd1c745414f32aafd2c1"


def _admin(dsn: str) -> str:
    return dsn.rsplit("/", 1)[0] + "/postgres"


@pytest.fixture(scope="module")
def down_dsn():
    name = f"meshy_down_{os.getpid()}_{uuid.uuid4().hex[:6]}"
    dsn = DSN.rsplit("/", 1)[0] + "/" + name
    with psycopg.connect(_admin(DSN), autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}"')
    try:
        with psycopg.connect(dsn) as conn:
            schema_sets.apply_all(conn, verbose=False)
            schema_sets.assert_schema_complete(conn)
        yield dsn
    finally:
        with psycopg.connect(_admin(DSN), autocommit=True) as c:
            c.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                      "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
            c.execute(f'DROP DATABASE IF EXISTS "{name}"')


def _pre_a1_handlers():
    """apps/memoryskills/handlers.py as it was at PRE_A1, imported as a sibling module so
    its relative imports resolve against today's package (it imports nothing A1 removed)."""
    src = subprocess.run(["git", "show", f"{PRE_A1}:apps/memoryskills/handlers.py"],
                         cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    assert src.returncode == 0, (
        f"cannot read the pre-A1 handler from git ({PRE_A1}): {src.stderr.strip()}. This "
        "test needs the repository history; it does not skip, because a rollback that was "
        "never exercised is the thing it exists to prevent")
    assert "signed_bytes" not in src.stdout, "control: that is the pre-A1 handler"
    # Its permissions module too, from the same commit (integ/r11). Divergence 040 slice 2
    # made check_permission's `resource=` required, and the pre-A1 handler, bound to today's,
    # raised TypeError on its first write: a failure of the loader, not of the schema this
    # test is about. Pre-A1 code is the handler AND the permissions it shipped with, so the
    # handler's `from .permissions import ...` resolves to PRE_A1's module while it loads.
    perm_src = subprocess.run(["git", "show", f"{PRE_A1}:apps/memoryskills/permissions.py"],
                              cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    assert perm_src.returncode == 0, perm_src.stderr
    assert "resource" not in perm_src.stdout, "control: the pre-040 permissions module"
    perm_spec = importlib.util.spec_from_loader("apps.memoryskills._permissions_pre_a1",
                                                loader=None)
    perm = importlib.util.module_from_spec(perm_spec)
    perm.__package__ = "apps.memoryskills"
    exec(compile(perm_src.stdout, "permissions_pre_a1.py", "exec"), perm.__dict__)
    spec = importlib.util.spec_from_loader("apps.memoryskills._handlers_pre_a1", loader=None)
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "apps.memoryskills"
    import apps.memoryskills.permissions  # noqa: F401 - today's, restored below
    today = sys.modules["apps.memoryskills.permissions"]
    sys.modules["apps.memoryskills.permissions"] = perm
    try:
        exec(compile(src.stdout, "handlers_pre_a1.py", "exec"), module.__dict__)
    finally:
        sys.modules["apps.memoryskills.permissions"] = today
    assert module.check_permission is perm.check_permission, "control: PRE_A1's is bound"
    return module


def _peer(conn, tag: str) -> dict:
    owner_id, peer_id = uuid.uuid4(), uuid.uuid4()
    seed = os.urandom(32)
    pub = P.public_key_of(seed)
    kid = P.kid_for_peer_key(pub)
    conn.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id) "
                 "VALUES (%s,%s,'dev',%s)",
                 (owner_id, f"down-{tag}-{owner_id.hex[:8]}@example.test", uuid.uuid4()))
    conn.execute("INSERT INTO peers (id, owner_id, name, display_name) VALUES (%s,%s,%s,%s)",
                 (peer_id, owner_id, f"down-{tag}-{peer_id.hex[:8]}", tag))
    conn.execute("INSERT INTO peer_keys (peer_id, kid, public_key, active) VALUES (%s,%s,%s,TRUE)",
                 (peer_id, kid, P.b64u(pub)))
    conn.commit()
    return {"peer_id": peer_id, "owner_id": owner_id, "seed": seed, "kid": kid}


def _env(peer: dict, skill: str, args: dict) -> dict:
    from tests.test_memory_skills import _envelope
    return _envelope(peer, skill, args)


A1_VALUES = [{"text": "plain"}, {"n": 1.0, "tiny": 1e-7, "f": 0.1, "big": 1e15},
             {"s": "caf\u00e9 \U0001F600", "\U0001F600": 1, "\uff41": 2},
             {"deep": [1, {"b": None, "a": True}]}]


def _function_body(sql: str) -> str:
    m = re.search(r"CREATE OR REPLACE FUNCTION shared_memory_enforce_immutability\(\) "
                  r"RETURNS TRIGGER AS \$\$(.*?)\$\$ LANGUAGE plpgsql;", sql, re.S)
    assert m, "no immutability function in that file"
    return m.group(1)


def test_13_the_a1_down_script_restores_pre_a1_and_the_classifier_recovers_the_bytes(
        down_dsn):
    owner = psycopg.connect(down_dsn)
    app = db.connect(down_dsn)
    try:
        peer = _peer(owner, "a1")
        handlers.set_bootstrap_authority(peer["peer_id"])
        ns = f"downtest/{uuid.uuid4().hex[:8]}"
        handlers.dispatch("namespace.claim", _env(peer, "namespace.claim", {"namespace": ns}),
                          peer["peer_id"], app)
        written = {}
        for i, value in enumerate(A1_VALUES):
            key = f"k{i}"
            handlers.dispatch("memory.write", _env(peer, "memory.write", {
                "namespace": ns, "key": key, "value": value, "tags": ["t"]}),
                peer["peer_id"], app)
            written[key] = bytes(owner.execute(
                "SELECT signed_bytes FROM shared_memory WHERE namespace = %s AND key = %s",
                (ns, key)).fetchone()[0])
        owner.commit()
        assert all(written.values()), "control: A1 stored bytes for every row"

        # --- the down scripts, A2's then A1's, in one transaction, as the owner role
        _apply_down(owner)

        cols = {r[0] for r in owner.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'shared_memory'").fetchall()}
        assert "signed_bytes" not in cols and "value" in cols
        assert owner.execute("SELECT count(*) FROM pg_trigger WHERE tgname IN "
                             "('shared_memory_signed_bytes_agree_trg', "
                             " 'shared_memory_legacy_verdict_bind_trg')").fetchone()[0] == 0
        assert owner.execute("SELECT count(*) FROM pg_proc WHERE proname LIKE 'sm\\_%%' "
                             "OR proname IN ('shared_memory_signed_bytes_agree', "
                             "'shared_memory_legacy_verdict_bind')").fetchone()[0] == 0
        assert owner.execute("SELECT to_regclass('shared_memory_legacy_verdict')"
                             ).fetchone()[0] is None
        assert owner.execute("SELECT count(*) FROM pg_constraint WHERE conname = "
                             "'skill_calls_status_matches_signed_result'").fetchone()[0] == 0
        assert owner.execute("SELECT count(*) FROM meshychat_migrations WHERE version = 14"
                             ).fetchone()[0] == 0
        live = owner.execute("SELECT prosrc FROM pg_proc WHERE proname = "
                             "'shared_memory_enforce_immutability'").fetchone()[0]
        owner.commit()
        assert live == _function_body(
            (ROOT / "sql" / "063_high_churn_parent_null_exemption.sql").read_text("utf-8"))

        # --- the pre-A1 handler writes; today's cannot (the runbook's order, named)
        old = _pre_a1_handlers()
        old.set_bootstrap_authority(peer["peer_id"])
        old.dispatch("memory.write", _env(peer, "memory.write", {
            "namespace": ns, "key": "after-down", "value": {"x": 1}}), peer["peer_id"], app)
        with pytest.raises(psycopg.errors.UndefinedColumn):
            handlers.dispatch("memory.write", _env(peer, "memory.write", {
                "namespace": ns, "key": "today", "value": {"x": 1}}), peer["peer_id"], app)

        # --- the classifier recovers A1's bytes from provenance_jsonb
        from apps.memoryskills import legacy_verdict
        ids = [r[0] for r in owner.execute(
            "SELECT id FROM shared_memory WHERE namespace = %s AND key = ANY(%s) ORDER BY key",
            (ns, list(written))).fetchall()]
        owner.commit()
        verdicts = legacy_verdict.classify_ids(app, ids)
        for key, v in zip(sorted(written), (verdicts[i] for i in ids)):
            assert (v.verdict, v.loader, v.canonicalizer) == (
                "verified", "json_default", "rfc8785"), (key, v)
            assert v.verified_bytes == written[key], key

        # --- and 014 applies again over what the rollback left
        schema_sets.apply_all(owner, verbose=False)
        schema_sets.assert_schema_complete(owner)
        assert owner.execute("SELECT count(*) FROM shared_memory WHERE namespace = %s AND "
                             "signed_bytes IS NULL", (ns,)).fetchone()[0] == len(written) + 1
        owner.commit()
        handlers.dispatch("memory.write", _env(peer, "memory.write", {
            "namespace": ns, "key": "again", "value": {"x": 1}}), peer["peer_id"], app)
    finally:
        app.close()
        owner.close()



# ============================================== adversarial review of fin/b02-a1, 5, 6 and 8

def _apply_down(owner) -> None:
    """A2's down script, then A1's (design 4.3; 025's trigger pins the column)."""
    with owner.transaction():
        owner.execute(DOWN_A2.read_text("utf-8"))
        owner.execute(DOWN.read_text("utf-8"))


def _undo_a2(owner) -> None:
    """Back to the A1 schema: 025's down script alone."""
    with owner.transaction():
        owner.execute(DOWN_A2.read_text("utf-8"))


def _reapply(owner) -> None:
    """Whatever an earlier test in this module left, end at the A1 schema."""
    schema_sets.apply_all(owner, verbose=False)
    schema_sets.assert_schema_complete(owner)
    owner.commit()


def _pre_a1_session(dsn: str):
    """A session opened the way pre-A1 code opens one: apps/core/rls.set_role at PRE_A1 ran
    `SET ROLE "meshy_app"` and nothing else (checked against git here)."""
    src = subprocess.run(["git", "show", f"{PRE_A1}:apps/core/rls.py"], cwd=ROOT,
                         capture_output=True, text=True, encoding="utf-8")
    assert src.returncode == 0, src.stderr
    body = src.stdout.split("def set_role(", 1)[1].split("\ndef ", 1)[0]
    assert re.findall(r"cur\.execute\((.*)\)", body) == ["f'SET ROLE \"{role}\"'"], (
        "control: the pre-A1 session setup is SET ROLE alone")
    conn = psycopg.connect(dsn, autocommit=True)
    conn.execute(f'SET ROLE "{HISTORICAL_A1_ROLE}"')
    return conn


def _give_historical_role_its_grants(owner) -> None:
    """Pre-A1 code runs as meshy_app, and 046 retires meshy_app's privileges in a database built
    from the history (meshychat_app keeps the copy). This test simulates the overlap in which
    that old code still serves, so restore meshy_app to exactly what meshychat_app holds, the
    same copy 046's down script makes. Production after the role rename has no meshy_app at
    all, so this state exists only for this simulation."""
    rows = owner.execute(
        "SELECT 'SCHEMA public', a.privilege_type, a.is_grantable FROM pg_namespace n "
        "CROSS JOIN LATERAL aclexplode(n.nspacl) a "
        "WHERE n.nspname = 'public' AND a.grantee = to_regrole('meshychat_app') "
        "UNION ALL "
        "SELECT CASE WHEN c.relkind = 'S' THEN 'SEQUENCE ' ELSE 'TABLE ' END || "
        "quote_ident(c.relname), a.privilege_type, a.is_grantable FROM pg_class c "
        "CROSS JOIN LATERAL aclexplode(c.relacl) a "
        "WHERE c.relnamespace = 'public'::regnamespace "
        "AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f') AND a.grantee = to_regrole('meshychat_app') "
        "UNION ALL "
        "SELECT 'FUNCTION ' || quote_ident(p.proname) || '(' || "
        "pg_get_function_identity_arguments(p.oid) || ')', a.privilege_type, a.is_grantable "
        "FROM pg_proc p CROSS JOIN LATERAL aclexplode(p.proacl) a "
        "WHERE p.pronamespace = 'public'::regnamespace AND a.grantee = to_regrole('meshychat_app')"
    ).fetchall()
    assert rows, "control: meshychat_app holds privileges to copy"
    for target, privilege, grantable in rows:
        owner.execute(f"GRANT {privilege} ON {target} TO {HISTORICAL_A1_ROLE}"
                      + (" WITH GRANT OPTION" if grantable else ""))


def _install_historical_a1_guard(owner) -> None:
    """Set the exact 014 guard this A1-era compatibility test is about.

    Earlier file tests can reapply 014 after 046 remains in the ledger, which
    restores this old function text without rerunning 046's textual rewrite.
    Make that intended A1 state explicit rather than depending on test order.
    """
    source = UP_A1.read_text(encoding="utf-8")
    start = source.index("CREATE OR REPLACE FUNCTION shared_memory_signed_bytes_agree()")
    end = source.index("\n$$;", start) + len("\n$$;")
    owner.execute(source[start:end])
    _give_historical_role_its_grants(owner)
    owner.commit()
    body = owner.execute(
        "SELECT pg_get_functiondef('shared_memory_signed_bytes_agree()'::regprocedure)"
    ).fetchone()[0]
    versions = {row[0] for row in owner.execute(
        "SELECT version FROM meshychat_migrations WHERE version IN (14, 46)"
    ).fetchall()}
    assert "current_user = 'meshy_app'" in body and versions == {14, 46}


def test_13_pre_a1_code_still_writes_on_the_a1_schema_so_a_deploy_can_overlap(down_dsn):
    """Review finding 6. Keyed to the role alone, A1 refused every write pre-A1 code made on
    the A1 schema (E_SIGNED_BYTES_REQUIRED), and A1 code fails on the pre-A1 schema
    (UndefinedColumn, test_13): no order of a rolling deploy kept writes working. Keyed to
    this build's session marker (finding 1), a pre-A1 instance's session carries none, so
    its writes land without bytes and are classified as verified. Forward: migrate,
    then roll the code. Back: roll the code back, then apply the down script.

    That is A1's schema. With A2 live (divergence 051) the same pre-A1 write is refused:
    design 4.3, "Code rolled back past A1 while A2 is live fails EVERY write, closed and
    loudly", which is why A2's down script runs before any code rollback past A1."""
    owner = psycopg.connect(down_dsn)
    app = db.connect(down_dsn)
    old_session = _pre_a1_session(down_dsn)
    try:
        _reapply(owner)
        _install_historical_a1_guard(owner)
        rls.set_role(app, HISTORICAL_A1_ROLE)
        _undo_a2(owner)
        peer = _peer(owner, "overlap")
        handlers.set_bootstrap_authority(peer["peer_id"])
        ns = f"downtest/{uuid.uuid4().hex[:8]}"
        handlers.dispatch("namespace.claim", _env(peer, "namespace.claim", {"namespace": ns}),
                          peer["peer_id"], app)
        old = _pre_a1_handlers()
        old.set_bootstrap_authority(peer["peer_id"])

        old.dispatch("memory.write", _env(peer, "memory.write", {
            "namespace": ns, "key": "old", "value": {"x": 1}, "tags": ["t"]}),
            peer["peer_id"], old_session)
        handlers.dispatch("memory.write", _env(peer, "memory.write", {
            "namespace": ns, "key": "new", "value": {"x": 1}, "tags": ["t"]}),
            peer["peer_id"], app)
        rows = dict(owner.execute("SELECT key, signed_bytes IS NOT NULL FROM shared_memory "
                                  "WHERE namespace = %s", (ns,)).fetchall())
        rid = owner.execute("SELECT id FROM shared_memory WHERE namespace = %s AND key = 'old'",
                            (ns,)).fetchone()[0]
        owner.commit()
        assert rows == {"old": False, "new": True}

        from apps.memoryskills import legacy_verdict
        v = legacy_verdict.classify_ids(app, [rid])[rid]
        assert (v.verdict, v.binding_failures, v.disagreeing_signed,
                v.disagreeing_defaulted) == ("verified", [], [], [])

        # One variable apart: the same pre-A1 handler on a session this build opened.
        with pytest.raises(psycopg.errors.CheckViolation, match=r"E_SIGNED_BYTES_REQUIRED"):
            old.dispatch("memory.write", _env(peer, "memory.write", {
                "namespace": ns, "key": "old-on-a-new-session", "value": {"x": 1}}),
                peer["peer_id"], app)

        # And one variable apart the other way: A2 applied again, the pre-A1 write that
        # landed above is refused, by 025's trigger.
        _reapply(owner)
        with pytest.raises(psycopg.errors.CheckViolation, match=r"divergence 051"):
            old.dispatch("memory.write", _env(peer, "memory.write", {
                "namespace": ns, "key": "old-under-a2", "value": {"x": 1}}),
                peer["peer_id"], old_session)
    finally:
        old_session.close()
        app.close()
        owner.close()


def test_13_the_down_script_keeps_the_verdicts_the_job_could_not_rebuild(down_dsn):
    """Review finding 8, design Q14. The down script dropped the verdict table, saying the
    job rebuilds it. It rebuilds only the verdicts of rows that still exist; a row an
    ephemeral rewrite or a prune deleted takes its verdict for good, which is the evidence
    Q14 keeps. The down script moves the verdicts to a retained table the pre-A1 code never
    reads, owner-only, and a second round trip adds to it without duplicating."""
    from apps.memoryskills import legacy_verdict
    from tests.test_signed_bytes_classifier import _namespace, _signer, legacy

    owner = psycopg.connect(down_dsn)
    app = db.connect(down_dsn)
    try:
        _reapply(owner)
        signer = _signer(owner, "keep")
        ns = _namespace(owner, signer)
        kept = legacy(owner, signer, ns)
        gone = legacy(owner, signer, ns)
        while legacy_verdict.catch_up(app, max_batch=500).more:
            pass
        owner.execute("DELETE FROM shared_memory WHERE id = %s", (gone,))
        owner.commit()

        def retained() -> dict:
            got = owner.execute(
                "SELECT row_id, verdict, row_namespace, count(*) OVER (PARTITION BY row_id) "
                "FROM shared_memory_legacy_verdict_retired WHERE row_id = ANY(%s)",
                ([kept, gone],)).fetchall()
            owner.commit()
            return {r[0]: (r[1], r[2], r[3]) for r in got}

        _apply_down(owner)
        assert owner.execute("SELECT to_regclass('shared_memory_legacy_verdict')"
                             ).fetchone()[0] is None, "control: the live table is gone"
        owner.commit()
        assert retained() == {kept: ("verified", ns, 1), gone: ("verified", ns, 1)}
        privileges = owner.execute(
            "SELECT p FROM unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE']) p "
            "WHERE has_table_privilege('meshy_app', 'shared_memory_legacy_verdict_retired', p)"
        ).fetchall()
        owner.commit()
        assert privileges == [], "the retained verdicts are the owner's alone"

        # The round trip again: 014, the job (kept is classified a second time), the down.
        _reapply(owner)
        while legacy_verdict.catch_up(app, max_batch=500).more:
            pass
        _apply_down(owner)
        assert retained() == {kept: ("verified", ns, 1), gone: ("verified", ns, 1)}
    finally:
        app.close()
        owner.close()
        with psycopg.connect(down_dsn) as c:
            _reapply(c)


def test_13_014_and_its_down_script_give_up_on_a_lock_instead_of_queueing(down_dsn):
    """Review finding 5. 014 and its down script take ACCESS EXCLUSIVE on shared_memory.
    Behind a long reader that request waits, and every later reader and writer of the table
    queues behind it: the migration becomes an outage for as long as the reader runs.
    The wait is bounded by `SET lock_timeout = '5s'`; it is SET LOCAL, so it ends with the
    migration's own transaction."""
    owner = psycopg.connect(down_dsn)
    reader = psycopg.connect(down_dsn)
    try:
        _reapply(owner)
        owner.execute("SET statement_timeout = '25s'")   # bounds this test's RED run
        owner.commit()
        default = reader.execute("SHOW lock_timeout").fetchone()[0]
        reader.commit()

        for step in ("down", "014"):
            reader.execute("SELECT count(*) FROM shared_memory")   # holds ACCESS SHARE
            started = time.monotonic()
            with pytest.raises(psycopg.errors.LockNotAvailable):
                if step == "down":
                    _apply_down(owner)
                else:
                    schema_sets.apply_all(owner, verbose=False)
            waited = time.monotonic() - started
            assert waited < 15, (step, waited)
            owner.rollback()
            # Nothing of the step landed: the column is still there after the refused down,
            # and still absent after the refused 014.
            has_col = owner.execute(
                "SELECT count(*) FROM information_schema.columns WHERE table_name = "
                "'shared_memory' AND column_name = 'signed_bytes'").fetchone()[0]
            owner.commit()
            assert has_col == (1 if step == "down" else 0), step
            reader.rollback()                                     # the reader finishes
            if step == "down":
                _apply_down(owner)                                # and the step applies
            else:
                _reapply(owner)
            assert owner.execute("SHOW lock_timeout").fetchone()[0] == default, (
                "the timeout outlived the migration's transaction")
            owner.commit()
    finally:
        reader.close()
        owner.close()
        with psycopg.connect(down_dsn) as c:
            _reapply(c)
