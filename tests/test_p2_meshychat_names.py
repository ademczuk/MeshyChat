"""P2 catalog, ACL, concurrency, rollback, and static-name controls."""
from __future__ import annotations

import ast
import contextlib
import functools
import os
import re
import shutil
import subprocess
import threading
import uuid
from pathlib import Path

import psycopg
import pytest

from apps.core import schema_sets, sqlmigrate
from tests.fixtures import signed_rows as signed


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DSN = os.environ["MESHY_TEMPLATE_DSN"]
APP_ROLE = "meshychat_app"
OLD_APP_ROLE = "meshy_app"


def _set(name: str):
    return next(item for item in schema_sets.SETS if item.name == name)


BASE_SCHEMA_SET = _set("base")
MESHYCHAT = _set("meshychat")


#: The version these tests are about. They compare the database 046 leaves with the one 045 left (ACLs, names), so
#: they build THROUGH 046, not to the head: a later migration's new objects (047, the context index) are not 046's.
P2_VERSION = 46


@contextlib.contextmanager
def _database(*, meshy_dir: Path | None = None, upto: int | None = P2_VERSION):
    """A new scratch database, migrated through both declared sets (meshychat through `upto`, 046 by default) and
    always dropped."""
    name = f"p2_{uuid.uuid4().hex}"
    admin = TEMPLATE_DSN.rsplit("/", 1)[0] + "/postgres"
    dsn = TEMPLATE_DSN.rsplit("/", 1)[0] + "/" + name
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    try:
        with psycopg.connect(dsn) as conn:
            sqlmigrate.migrate(conn, sql_dir=BASE_SCHEMA_SET.sql_dir, ledger=BASE_SCHEMA_SET.ledger,
                               verbose=False)
            sqlmigrate.migrate(conn, sql_dir=meshy_dir or MESHYCHAT.sql_dir,
                               ledger=MESHYCHAT.ledger, verbose=False, upto=upto)
        yield dsn
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                         "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
            conn.execute(f'DROP DATABASE IF EXISTS "{name}"')


def _copy_meshychat(tmp_path: Path, mutate, name: str = "046_p2_meshychat_names.sql") -> Path:
    copied = tmp_path / "sql_meshychat"
    shutil.copytree(MESHYCHAT.sql_dir, copied)
    path = copied / name
    source = path.read_text(encoding="utf-8")
    changed = mutate(source)
    assert changed != source, "the RED control did not mutate 046"
    path.write_text(changed, encoding="utf-8", newline="\n")
    return copied


def _old_catalog_names(conn) -> dict[str, list[str]]:
    queries = {
        "relations": "SELECT c.relname FROM pg_class c WHERE c.relnamespace='public'::regnamespace",
        "columns": "SELECT a.attname FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid "
                   "WHERE c.relnamespace='public'::regnamespace AND a.attnum > 0 AND NOT a.attisdropped",
        "constraints": "SELECT conname FROM pg_constraint WHERE connamespace='public'::regnamespace",
        "indexes": "SELECT c.relname FROM pg_class c WHERE c.relnamespace='public'::regnamespace "
                   "AND c.relkind='i'",
        "policies": "SELECT p.polname FROM pg_policy p JOIN pg_class c ON c.oid=p.polrelid "
                    "WHERE c.relnamespace='public'::regnamespace",
        "triggers": "SELECT tgname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
                    "WHERE c.relnamespace='public'::regnamespace AND NOT t.tgisinternal",
        "functions": "SELECT proname FROM pg_proc WHERE pronamespace='public'::regnamespace",
        "sequences": "SELECT c.relname FROM pg_class c WHERE c.relnamespace='public'::regnamespace "
                     "AND c.relkind='S'",
    }
    return {kind: [row[0] for row in conn.execute(query).fetchall() if "meshy" in row[0].lower()]
            for kind, query in queries.items()}


def _assert_t1(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        found = {kind: names for kind, names in _old_catalog_names(conn).items() if names}
        assert found == {}, found
        conn.execute("SET ROLE meshychat_app")
        assert conn.execute("SELECT current_user").fetchone()[0] == APP_ROLE
        bodies = conn.execute(
            "SELECT proname, pg_get_functiondef(oid) FROM pg_proc "
            "WHERE pronamespace='public'::regnamespace").fetchall()
        old_literals = [name for name, body in bodies
                        if "meshy_app" in re.sub(r"--[^\n]*", "", body)]
        assert old_literals == [], old_literals


def _acl_rows(conn, role: str) -> dict[str, set[tuple]]:
    role_sql = "to_regrole(%s)"
    return {
        "schema": set(conn.execute(
            "SELECT n.nspname, a.privilege_type, a.is_grantable "
            "FROM pg_namespace n CROSS JOIN LATERAL aclexplode(coalesce(n.nspacl, "
            "acldefault('n'::\"char\", n.nspowner))) a WHERE n.nspname='public' AND a.grantee=" + role_sql,
            (role,)).fetchall()),
        "relations": set(conn.execute(
            "SELECT c.relname, c.relkind, a.privilege_type, a.is_grantable FROM pg_class c "
            "CROSS JOIN LATERAL aclexplode(coalesce(c.relacl, acldefault(CASE WHEN c.relkind='S' "
            "THEN 'S'::\"char\" ELSE 'r'::\"char\" END, c.relowner))) a "
            "WHERE c.relnamespace='public'::regnamespace AND c.relkind IN ('r','p','v','m','S') "
            "AND a.grantee=" + role_sql, (role,)).fetchall()),
        "functions": set(conn.execute(
            "SELECT p.oid::regprocedure::text, a.privilege_type, a.is_grantable FROM pg_proc p "
            "CROSS JOIN LATERAL aclexplode(coalesce(p.proacl, acldefault('f'::\"char\", p.proowner))) a "
            "WHERE p.pronamespace='public'::regnamespace AND a.grantee=" + role_sql,
            (role,)).fetchall()),
        "defaults": set(conn.execute(
            "SELECT d.defaclrole::regrole::text, d.defaclobjtype, a.privilege_type, a.is_grantable "
            "FROM pg_default_acl d JOIN pg_namespace n ON n.oid=d.defaclnamespace "
            "CROSS JOIN LATERAL aclexplode(d.defaclacl) a WHERE n.nspname='public' "
            "AND a.grantee=" + role_sql, (role,)).fetchall()),
    }


def _seed_shared_memory(dsn: str) -> None:
    peer_id, owner_id = uuid.uuid4(), uuid.uuid4()
    with psycopg.connect(dsn) as conn:
        conn.execute("INSERT INTO dashboard_users (id,email,plane,trust_group_id) VALUES (%s,%s,'dev',%s)",
                     (owner_id, f"p2-{owner_id.hex}@example.test", uuid.uuid4()))
        conn.execute("INSERT INTO peers (id,owner_id,name,display_name) VALUES (%s,%s,%s,%s)",
                     (peer_id, owner_id, "p2", "p2"))
        conn.execute("INSERT INTO shared_memory_namespace (namespace,owner_peer_id,owner_owner_id) "
                     "VALUES ('shared/p2',%s,%s)", (peer_id, owner_id))
        signed.insert(conn, signed.row(peer_id, owner_id, "shared/p2", "p2", {"ok": True}))
        conn.commit()


_EMPTY_ACL = {"schema": set(), "relations": set(), "functions": set(), "defaults": set()}


@functools.lru_cache(maxsize=1)
def _pre_046_reference() -> dict[str, dict[str, set[tuple]]]:
    """The `meshy_app` compatibility role's ACL at version 45, with object names carried through
    the rename. 046 empties meshy_app, so after it the only honest reference is this snapshot."""
    with _database(upto=45) as dsn:
        with psycopg.connect(dsn, autocommit=True) as conn:
            before = _acl_rows(conn, OLD_APP_ROLE)
    assert before["relations"] and before["functions"] and before["defaults"], before

    def renamed(rows: set[tuple]) -> set[tuple]:
        return {(row[0].replace("meshy_", "meshychat_"), *row[1:]) for row in rows}
    return {"raw": before,
            "renamed": {"schema": before["schema"], "relations": renamed(before["relations"]),
                        "functions": renamed(before["functions"]), "defaults": before["defaults"]}}


def _assert_t2(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as owner:
        assert _acl_rows(owner, APP_ROLE) == _pre_046_reference()["renamed"]
        # The history-built database keeps no meshy_app privilege at all: a dump of it names
        # only meshychat_app and the owner.
        assert _acl_rows(owner, OLD_APP_ROLE) == _EMPTY_ACL
        # Whole-database check, wider than the four ACL families above: no ACL, owner or policy
        # in this database depends on meshy_app at all (pg_shdepend), so nothing else can leak
        # into a dump (columns, types, large objects, languages).
        assert owner.execute(
            "SELECT count(*) FROM pg_shdepend WHERE refobjid = to_regrole(%s) "
            "AND dbid = (SELECT oid FROM pg_database WHERE datname = current_database())",
            (OLD_APP_ROLE,)).fetchone()[0] == 0
        for table in ("shared_memory_legacy_verdict", "capability_revocation", "capability_event",
                      "shared_memory_redaction", "shared_memory_edit", "invocation_frames",
                      "admin_audit_log"):
            for privilege in ("UPDATE", "DELETE"):
                assert not owner.execute("SELECT has_table_privilege(%s,%s,%s)",
                                         (APP_ROLE, table, privilege)).fetchone()[0], (table, privilege)
        for function in ("meshychat_namespace_permission(uuid)",
                         "prune_standalone_invocation_frames(integer)",
                         "file_referencing_calls(uuid)"):
            assert owner.execute("SELECT has_function_privilege(%s,%s::regprocedure,'EXECUTE')",
                                 (APP_ROLE, function)).fetchone()[0], function
        # sql_meshychat/012 intentionally revokes this after sql/111_call_and_frame_retention.sql
        # grants it.
        assert not owner.execute("SELECT has_function_privilege(%s,%s::regprocedure,'EXECUTE')",
                                 (APP_ROLE, "prune_invocation_frames(integer,integer)")).fetchone()[0]

    _seed_shared_memory(dsn)
    with psycopg.connect(dsn, autocommit=True) as app:
        app.execute("SET ROLE meshychat_app")
        assert app.execute("SELECT count(*) FROM shared_memory").fetchone()[0] == 0
        app.execute("SELECT set_config('app.server_internal','1',false)")
        assert app.execute("SELECT count(*) FROM shared_memory").fetchone()[0] == 1


def test_t1_fresh_chain_removes_old_catalog_names_and_role_literals(tmp_path):
    """Fresh chain control, including a copied migration with one final rename omitted."""
    with _database() as dsn:
        _assert_t1(dsn)

    copied = _copy_meshychat(tmp_path, lambda text: text.replace(
        "ALTER FUNCTION public.meshy_same_tenant_or_devgroup(caller uuid, target uuid) "
        "RENAME TO meshychat_same_tenant_or_devgroup;",
        "-- red control: final function rename omitted", 1))
    with _database(meshy_dir=copied) as dsn:
        with pytest.raises(AssertionError, match="functions"):
            _assert_t1(dsn)


def test_t2_acl_equivalence_and_rls_controls(tmp_path):
    """ACL equality, append-only denials, allowed calls, and server-internal RLS control."""
    with _database() as dsn:
        _assert_t2(dsn)

    copied = _copy_meshychat(tmp_path, lambda text: text.replace(
        "EXECUTE 'REVOKE ' || rec.privilege_type || ' ON ' || object_sql || ' FROM meshychat_app';",
        "PERFORM 1; -- red control: one relation REVOKE omitted", 1))
    # RED control for the retirement of meshy_app: one family of revokes omitted, so meshy_app
    # keeps table privileges and a dump of the database would still name it.
    kept = _copy_meshychat(tmp_path / "kept", lambda text: text.replace(
        "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM meshy_app;",
        "-- red control: meshy_app table revoke omitted", 1))
    with _database(meshy_dir=kept) as dsn:
        with pytest.raises(AssertionError):
            _assert_t2(dsn)

    with _database(meshy_dir=copied, upto=45) as dsn:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("GRANT UPDATE ON capability_event TO meshychat_app")
        with psycopg.connect(dsn) as conn:
            sqlmigrate.migrate(conn, sql_dir=copied, ledger=MESHYCHAT.ledger, verbose=False, upto=46)
        with pytest.raises(AssertionError):
            _assert_t2(dsn)


def test_t3_existing_roles_and_two_database_advisory_lock(tmp_path):
    """046 accepts a pre-existing app role and two concurrent database migrations."""
    databases = []
    try:
        for _ in range(2):
            context = _database(upto=45)
            dsn = context.__enter__()
            databases.append((context, dsn))
        errors: list[BaseException] = []

        def apply_046(dsn: str):
            try:
                with psycopg.connect(dsn) as conn:
                    sqlmigrate.migrate(conn, sql_dir=MESHYCHAT.sql_dir, ledger=MESHYCHAT.ledger,
                                       verbose=False, upto=46)
            except BaseException as exc:  # asserted below with both worker outcomes
                errors.append(exc)

        threads = [threading.Thread(target=apply_046, args=(dsn,)) for _context, dsn in databases]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == [], errors
        for _context, dsn in databases:
            _assert_t2(dsn)

        copied = _copy_meshychat(tmp_path, lambda text: text.replace(
            "IF new_role IS NULL THEN", "IF TRUE THEN", 1))
        with _database(meshy_dir=copied) as dsn:
            pytest.fail(f"RED control unexpectedly migrated {dsn}")
    except psycopg.errors.DuplicateObject:
        pass
    finally:
        for context, _dsn in reversed(databases):
            context.__exit__(None, None, None)


def test_t4_down_then_up_restores_catalog_and_acl_equality(tmp_path):
    """The existing rollback gate covers catalog restoration; this adds second-up ACL proof."""
    with _database() as dsn:
        down = (MESHYCHAT.sql_dir / "down" / "046_p2_meshychat_names_down.sql").read_text(encoding="utf-8")
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(down)
            conn.execute(f"DELETE FROM {MESHYCHAT.ledger} WHERE version = 46")
            # Rolled back: meshy_app holds exactly what it held before 046, meshychat_app nothing.
            assert _acl_rows(conn, OLD_APP_ROLE) == _pre_046_reference()["raw"]
            assert _acl_rows(conn, APP_ROLE) == _EMPTY_ACL
        with psycopg.connect(dsn) as conn:
            sqlmigrate.migrate(conn, sql_dir=MESHYCHAT.sql_dir, ledger=MESHYCHAT.ledger,
                               verbose=False, upto=46)
        _assert_t2(dsn)

    # RED control for the regrant: the privileges go back to the wrong role, so a rollback
    # would leave meshy_app (the role the pre-046 code binds) with nothing.
    forgot = _copy_meshychat(tmp_path / "forgot", lambda text: text.replace(
        "' TO meshy_app' ||", "' TO meshychat_app' ||", 1),
        "down/046_p2_meshychat_names_down.sql")
    with _database() as dsn:
        down = (forgot / "down" / "046_p2_meshychat_names_down.sql").read_text(encoding="utf-8")
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(down)
            with pytest.raises(AssertionError):
                assert _acl_rows(conn, OLD_APP_ROLE) == _pre_046_reference()["raw"]

    broken = _copy_meshychat(tmp_path, lambda text: text.replace(
        "AND to_regrole('meshy_app') <> to_regrole('meshychat_app') THEN",
        "AND FALSE THEN", 1), "down/046_p2_meshychat_names_down.sql")
    with _database() as dsn:
        down = (broken / "down" / "046_p2_meshychat_names_down.sql").read_text(encoding="utf-8")
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(down)
        with pytest.raises(AssertionError):
            with psycopg.connect(dsn, autocommit=True) as conn:
                assert _acl_rows(conn, APP_ROLE) == {"schema": set(), "relations": set(),
                                                      "functions": set(), "defaults": set()}


def _static_violations(root: Path) -> list[str]:
    paths = subprocess.run(["git", "-C", str(root), "ls-files"], check=True,
                           text=True, capture_output=True).stdout.splitlines()
    bad = []
    for rel in paths:
        if rel.startswith(("sql/", "sql_meshychat/", "data/")):
            continue
        path = root / rel
        if not path.is_file() or path.suffix not in {".py", ".ps1", ".yml", ".yaml"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        doc_lines = set()
        if path.suffix == ".py":
            tree = ast.parse(text, filename=str(path))
            for node in ast.walk(tree):
                body = getattr(node, "body", None)
                if (isinstance(body, list) and body and isinstance(body[0], ast.Expr)
                        and isinstance(getattr(body[0], "value", None), ast.Constant)
                        and isinstance(body[0].value.value, str)):
                    doc_lines.update(range(body[0].value.lineno, body[0].value.end_lineno + 1))
        for number, line in enumerate(text.splitlines(), 1):
            stripped = line.lstrip()
            if number in doc_lines or stripped.startswith(("#", "--")):
                continue
            if re.search(r"\bSET\s+(?:LOCAL\s+)?ROLE\s+['\"]?meshy_app\b", line, re.I):
                bad.append(f"{rel}:{number}: SET " + "ROLE meshy_app")
            if re.search(r"postgresql://meshy(?=[:@])", line, re.I):
                bad.append(f"{rel}:{number}: login meshy")
    return bad


def test_t5_no_executable_old_app_role_or_owner_login(tmp_path):
    assert _static_violations(ROOT) == []
    probe = tmp_path / "probe"
    probe.mkdir()
    subprocess.run(["git", "init", "-q", str(probe)], check=True)
    (probe / "probe.py").write_text('conn.execute("SET ROLE ' + 'meshy_app")\n', encoding="utf-8")
    subprocess.run(["git", "-C", str(probe), "add", "probe.py"], check=True)
    assert _static_violations(probe) == ["probe.py:1: SET " + "ROLE meshy_app"]
