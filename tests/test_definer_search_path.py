"""Every SECURITY DEFINER function runs with search_path = pg_catalog, public, pg_temp and names its relations
schema-qualified (divergence 096, sql_meshychat/042; kxxx's decision, msg/<key>).

THE CLASS. A function's `SET search_path` that does not name pg_temp still searches the session's temporary schema, and
searches it FIRST, for relation and type names. Every definer function here had `search_path = pg_catalog, public`, and
seven of the eight named their relations unqualified, so an unqualified name inside one could resolve to a temporary table
of the CALLER's session while the body ran with the owner's rights. meshychat_app may create temporary tables (PUBLIC holds
TEMPORARY on the database; test_the_app_role_can_make_the_shadow_tables is the control that it can).

What this file holds, each part against a control:
  * THE CENSUS of pg_proc: every definer function's path ends in pg_temp (the rule refuses 041's path, a missing path and
    pg_temp first); the census below is every definer function the database has, so a new one is added here with its
    relations and a shadow test; each body names every relation it uses as `public.<name>` (a lexical scan, whose control
    is the pre-042 bodies in the down script, where it finds exactly the census's relations unqualified).
  * PER FUNCTION, in a fresh session of the role that calls it: a temporary table of EVERY name the function uses, filled so
    that a function reading or writing it would answer or change something else, changes nothing. The answer is what the
    real tables give, the real rows changed are the ones the function changes, and every shadow table is as it was. The
    session's control: in it, an unqualified name resolves to the shadow, so the instrument can see a shadowed read.
  * 042 CHANGES NOTHING ELSE: on a database at meshychat 41, applying 42 keeps each function's oid, owner, ACL, comment,
    flags and triggers, and its new body is its old body with `public.` added; the down script's copies are each defining
    file's text verbatim (its catalog round trip is tests/test_rollback_gate.py's, parametrised over every down script).

Seen RED, as the divergence records: with 042 not applied, every shadow test of the seven re-created functions fails, and
the census fails on all eight. file_referencing_calls (041) was already qualified, so its shadow test passes either way and
only the census sees its path; its own mutant (a relation unqualified, pg_temp first) is caught by its shadow test.

Run:  .venv/bin/python -m pytest tests/test_definer_search_path.py -q -p no:cacheprovider
"""
from __future__ import annotations

import contextlib
import os
import re
import sys
import uuid
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")

from apps.core import rls, schema_sets, sqlmigrate  # noqa: E402
from apps.memoryskills import db as memdb  # noqa: E402
from tests.fixtures import frames as F  # noqa: E402
from tests.fixtures import signed_rows as sr  # noqa: E402
from tests import test_mesh_authorship as mesh  # noqa: E402
from tests.test_edit_db_rule import _author_and_sibling, _direct  # noqa: E402
from tests.test_files import _seed_call, _seed_peer, _seed_skill  # noqa: E402
from tests.test_frame_retention import _anchor, _chain, _grant, _retire  # noqa: E402
from apps.calls import frame_retention as FR  # noqa: E402  (after test_frame_retention, which sets Django up)

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")
DOWN = ROOT / "sql_meshychat" / "down" / "042_definer_search_path_down.sql"

#: THE CENSUS (sql_meshychat/042's header): every SECURITY DEFINER function, by regprocedure, with every relation its body
#: names. test_the_census_is_every_definer_function_and_each_has_the_one_path holds this to pg_proc; a new definer function
#: is added here, with its relations and a shadow test, and its path must end in pg_temp.
CENSUS = {
    "meshychat_same_tenant_or_devgroup(uuid,uuid)": ("dashboard_users",),
    "prune_invocation_frames(integer,integer)": ("invocation_frames",),
    "retire_invocation_chain(uuid,bigint,bigint,integer)": (
        "invocation_frame_retention_policy", "skill_grants", "invocation_chain_retention", "invocation_chain_heads",
        "invocation_frames"),
    "prune_standalone_invocation_frames(integer)": ("invocation_frame_retention_policy", "invocation_frames"),
    "meshy_mesh_commit_stamp()": ("mesh_commit_order", "shared_memory"),
    "meshy_mesh_redaction_stamp()": ("mesh_commit_order", "shared_memory"),
    "meshy_mesh_edit_stamp()": ("mesh_commit_order", "shared_memory"),
    "meshy_ctx_commit_stamp()": ("ctx_index_config", "ctx_commit_order", "shared_memory"),
    "meshy_ctx_chunk_changed()": ("ctx_chunk_changes",),
    "meshy_ctx_shard_plan_group(text,text,text,timestamp with time zone)": (
        "ctx_shard_setting", "ctx_shard_plan", "mesh_ctx_chunks"),
    "meshy_ctx_shard_plan_groups(text[],text[],text[],timestamp with time zone[])": (),
    "meshy_ctx_shard_plan_inserted(bigint[])": ("ctx_shard_setting", "mesh_ctx_chunks", "ctx_shard_plan"),
    "meshy_ctx_shard_plan_truncate()": ("ctx_shard_plan",),
    "meshy_ctx_shard_plan_rebuild()": ("ctx_shard_setting", "ctx_shard_plan", "mesh_ctx_chunks"),
    "file_referencing_calls(uuid)": ("skill_calls",),
    "meshy_card_head_track()": ("card_heads",),
}
#: Definer functions created AFTER 042, in its hardened shape from the start: in the census, not in 042's history.
AFTER_042 = {"meshy_ctx_commit_stamp()",      # sql_meshychat/047, v1.1 step 6
             "meshy_ctx_chunk_changed()",     # sql_meshychat/049, the shard plan's change counter
             # sql_meshychat/053, the stored shard plan's writers
             "meshy_ctx_shard_plan_group(text,text,text,timestamp with time zone)",
             "meshy_ctx_shard_plan_groups(text[],text[],text[],timestamp with time zone[])",
             "meshy_ctx_shard_plan_inserted(bigint[])", "meshy_ctx_shard_plan_truncate()",
             "meshy_ctx_shard_plan_rebuild()",
             # sql_meshychat/057, the card head record's trigger (divergence 110)
             "meshy_card_head_track()"}
HISTORIC_CENSUS = {
    sig.replace("meshychat_same_tenant_or_devgroup", "meshy_same_tenant_or_devgroup"): relations
    for sig, relations in CENSUS.items() if sig not in AFTER_042
}
#: The one path 042 gives every definer function.
PATH = ["pg_catalog", "public", "pg_temp"]

DEFINERS = ("SELECT p.oid::regprocedure::text, p.proconfig, p.prosrc FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE p.prosecdef AND n.nspname NOT IN ('pg_catalog', 'information_schema') ORDER BY 1")


@pytest.fixture()
def owner():
    """The migrating compatibility role (`meshy`, bypasses RLS), AUTOCOMMIT: seeds and observes. Never the session under test."""
    with psycopg.connect(DSN, autocommit=True) as conn:
        yield conn


@pytest.fixture()
def app_conn():
    """A request's connection (meshychat_app), for the setup helpers that write through the handlers."""
    conn = memdb.connect(DSN)
    try:
        yield conn
    finally:
        conn.close()


@contextlib.contextmanager
def _fresh_app_session():
    """A NEW meshychat_app session, the caller whose temporary schema is searched. New per test, so no plan a definer function
    cached in this backend earlier can stand between the shadow and the function."""
    conn = memdb.connect(DSN)
    try:
        assert conn.execute("SELECT current_user").fetchone()[0] == "meshychat_app"
        yield conn
    finally:
        conn.close()


# ----------------------------------------------------------------------------------------------------------------------
# the census

def search_path_of(proconfig) -> list[str] | None:
    """The search_path entries of a pg_proc.proconfig, unquoted, or None when the function sets none."""
    for entry in proconfig or []:
        name, _, value = entry.partition("=")
        if name.strip().lower() == "search_path":
            return [part.strip().strip('"') for part in value.split(",")]
    return None


def pg_temp_last(path) -> bool:
    return bool(path) and path[-1] == "pg_temp" and path.count("pg_temp") == 1


def test_every_definer_function_searches_pg_temp_last(owner):
    """THE RULE, for any definer function now or later: its proconfig names a search_path whose LAST entry is pg_temp.
    Not vacuous: the census's eight are among the functions read. CONTROL (C1): the rule refuses 041's path, a function
    with no path, pg_temp first and pg_temp twice, and accepts 042's."""
    rows = owner.execute(DEFINERS).fetchall()
    found = {sig for sig, _cfg, _src in rows}
    assert set(CENSUS) <= found, f"the census's functions were not all read: missing {sorted(set(CENSUS) - found)}"
    bad = {sig: search_path_of(cfg) for sig, cfg, _src in rows if not pg_temp_last(search_path_of(cfg))}
    assert bad == {}, f"SECURITY DEFINER functions whose search_path does not end in pg_temp: {bad}"
    assert search_path_of(["search_path=pg_catalog, public"]) == ["pg_catalog", "public"]
    assert not pg_temp_last(search_path_of(["search_path=pg_catalog, public"]))
    assert not pg_temp_last(search_path_of(None)) and not pg_temp_last(search_path_of(["work_mem=64MB"]))
    assert not pg_temp_last(search_path_of(["search_path=pg_temp, pg_catalog, public"]))
    assert not pg_temp_last(search_path_of(["search_path=pg_temp, pg_catalog, pg_temp"]))
    assert pg_temp_last(search_path_of(['search_path="pg_catalog", "public", "pg_temp"']))
    assert pg_temp_last(search_path_of(["search_path=pg_catalog, public, pg_temp"]))


def test_the_census_is_every_definer_function_and_each_has_the_one_path(owner):
    """pg_proc is the ground truth: the definer functions in the database are exactly the census's, and each has 042's
    path. A new definer function fails here until it is added to CENSUS with its relations and a shadow test."""
    rows = owner.execute(DEFINERS).fetchall()
    assert {sig for sig, _c, _s in rows} == set(CENSUS), sorted(sig for sig, _c, _s in rows)
    assert {sig: search_path_of(cfg) for sig, cfg, _s in rows} == {sig: PATH for sig in CENSUS}


_COMMENT = re.compile(r"--[^\n]*")
_STRING = re.compile(r"'(?:[^']|'')*'")
_CTE = re.compile(r"\bWITH\s+([A-Za-z_]\w*)\s+AS\b", re.I)
#: A relation named after FROM, JOIN, INSERT INTO, UPDATE or DELETE FROM, unqualified, and not a function call (a name
#: with `(` right after it) or a schema (a name with `.` right after it). The lookbehinds leave out `IS DISTINCT FROM` and
#: the row locks `FOR UPDATE`, `FOR NO KEY UPDATE`, and `DO UPDATE`.
_UNQUALIFIED_REF = re.compile(
    r"(?:(?<!DISTINCT )\bFROM|\bJOIN|\bINSERT\s+INTO|(?<!FOR )(?<!KEY )(?<!DO )\bUPDATE)\s+"
    r"(?!public\.)([A-Za-z_]\w*)\b(?![.(])", re.I)


def _code(body: str) -> str:
    """A function body without its comments and string literals (comments first: a comment may hold an apostrophe)."""
    return _STRING.sub("''", _COMMENT.sub("", body))


def unqualified_relations(body: str, relations=()) -> set[str]:
    """Relations the body names without `public.`: every FROM/JOIN/INSERT INTO/UPDATE target that is not a CTE, and
    every bare mention of a name in `relations` (the second form of a FROM list, `FROM a x, b y`, has no keyword). A
    name followed by a dot is a column's qualifier, not a relation."""
    code = _code(body)
    ctes = {m.lower() for m in _CTE.findall(code)}
    found = {m.lower() for m in _UNQUALIFIED_REF.findall(code)} - ctes
    for rel in relations:
        if re.search(r"(?<![\w.])" + rel + r"(?![\w.])", code):
            found.add(rel)
    return found


def _down_blocks() -> dict[str, tuple[str, str]]:
    """The down script's verbatim copies: name -> (source 'path:first-last', block text)."""
    text = DOWN.read_text(encoding="utf-8")
    out = {}
    for m in re.finditer(r"^-- (\S+):(\d+)-(\d+), verbatim\.\n(CREATE OR REPLACE FUNCTION (\w+)\(.*?^\$\$;\n)",
                         text, re.S | re.M):
        out[m.group(5)] = (f"{m.group(1)}:{m.group(2)}-{m.group(3)}", m.group(4))
    return out


def test_every_definer_function_names_its_relations_schema_qualified(owner):
    """Each census function's body names every relation as `public.<name>`, and names nothing the census does not list.
    CONTROL: the same scan over the pre-042 bodies (the down script's verbatim copies) finds exactly each function's census
    relations unqualified, so the scan can see an unqualified relation and the census lists every one it found."""
    bodies = {sig.split("(")[0]: src for sig, _cfg, src in owner.execute(DEFINERS).fetchall()}
    for sig, relations in CENSUS.items():
        name = sig.split("(")[0]
        assert unqualified_relations(bodies[name], relations) == set(), (sig, unqualified_relations(bodies[name], relations))
        code = _code(bodies[name])
        missing = [r for r in relations if not re.search(r"\bpublic\." + r + r"(?!\w)", code)]
        assert missing == [], f"{sig}: the census names {missing}, which its body does not use"
    old = _down_blocks()
    assert set(old) == {sig.split("(")[0] for sig in HISTORIC_CENSUS} - {"file_referencing_calls"}, sorted(old)
    for name, (_src, block) in old.items():
        body = block.split("$$", 1)[1]
        relations = next(rels for sig, rels in HISTORIC_CENSUS.items() if sig.startswith(name + "("))
        assert unqualified_relations(body) == set(relations), (name, unqualified_relations(body))
        assert unqualified_relations(body, relations) == set(relations), name


def test_the_down_script_copies_each_function_from_its_defining_file_verbatim():
    """The down script restores the pre-042 definitions by CREATE OR REPLACE from text; each copy is the defining file's
    lines, byte for byte, so the restore cannot drift from what was there. (The catalog round trip is test_rollback_gate's.)"""
    blocks = _down_blocks()
    assert len(blocks) == 7, sorted(blocks)
    for name, (where, block) in blocks.items():
        path, _, span = where.partition(":")
        first, last = (int(x) for x in span.split("-"))
        lines = (ROOT / path).read_text(encoding="utf-8").split("\n")
        assert "\n".join(lines[first - 1:last]) + "\n" == block, f"{name}: not {where} verbatim"
    assert "ALTER FUNCTION public.file_referencing_calls(uuid) SET search_path = pg_catalog, public;" in DOWN.read_text(
        encoding="utf-8")


# ----------------------------------------------------------------------------------------------------------------------
# 042 changes the path and the qualification, and nothing else

_PROC = ("SELECT p.oid, p.oid::regprocedure::text, p.proowner::regrole::text, coalesce(p.proacl::text, ''), "
         "coalesce(obj_description(p.oid, 'pg_proc'), ''), p.prolang, p.prosecdef, p.proleakproof, p.proisstrict, "
         "p.proretset, p.provolatile, p.proparallel, p.pronargs, p.prorettype, p.proargtypes::text, "
         "coalesce(p.proallargtypes::text, ''), coalesce(p.proargmodes::text, ''), coalesce(p.proargnames::text, ''), "
         "p.procost, p.prorows, "
         "coalesce((SELECT string_agg(t.tgrelid::regclass::text || ':' || t.tgname, ',' ORDER BY 1) FROM pg_trigger t "
         "          WHERE t.tgfoid = p.oid), ''), "
         "p.proconfig, p.prosrc "
         "FROM pg_proc p WHERE p.prosecdef")


@pytest.fixture(scope="module")
def at_041():
    """A database of its own migrated through meshychat 041, dropped afterwards."""
    name = f"meshy_test_dpath_{os.getpid()}_{uuid.uuid4().hex[:6]}"
    admin = DSN.rsplit("/", 1)[0] + "/postgres"
    dsn = DSN.rsplit("/", 1)[0] + "/" + name
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}"')
    try:
        with psycopg.connect(dsn) as conn:
            for s in schema_sets.SETS:
                sqlmigrate.migrate(conn, sql_dir=s.sql_dir, ledger=s.ledger, verbose=False,
                                   upto=41 if s.name == "meshychat" else None)
        yield dsn
    finally:
        with psycopg.connect(admin, autocommit=True) as c:
            c.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s "
                      "AND pid <> pg_backend_pid()", (name,))
            c.execute(f'DROP DATABASE IF EXISTS "{name}"')


def test_042_changes_only_the_path_and_the_qualification(at_041):
    """On a database at 041: applying 042 keeps every definer function's oid, owner, ACL, comment, language, flags,
    signature, cost and triggers; its path gains pg_temp at the end; and its new body with `public.` removed is its old
    body with `public.` removed (041's, already qualified, is unchanged). CONTROL: 042 is the version applied, and the
    seven re-created bodies did change."""
    meshychat = next(s for s in schema_sets.SETS if s.name == "meshychat")
    with psycopg.connect(at_041) as conn:
        before = {r[1]: r for r in conn.execute(_PROC).fetchall()}
        assert set(before) == set(HISTORIC_CENSUS), sorted(before)
        summary = sqlmigrate.migrate(conn, sql_dir=meshychat.sql_dir, ledger=meshychat.ledger, verbose=False, upto=42)
        assert summary["versions"] == [42]
        after = {r[1]: r for r in conn.execute(_PROC).fetchall()}
    assert set(after) == set(HISTORIC_CENSUS)
    changed = []
    for sig in HISTORIC_CENSUS:
        b, a = before[sig], after[sig]
        assert a[:-2] == b[:-2], f"{sig}: something besides the path and the body changed:\n{b[:-2]}\n{a[:-2]}"
        assert search_path_of(b[-2]) == ["pg_catalog", "public"] and search_path_of(a[-2]) == PATH, (sig, b[-2], a[-2])
        assert a[-1].replace("public.", "") == b[-1].replace("public.", ""), f"{sig}: the body changed beyond `public.`"
        if a[-1] != b[-1]:
            changed.append(sig)
    assert sorted(changed) == sorted(set(HISTORIC_CENSUS) - {"file_referencing_calls(uuid)"}), changed


# ----------------------------------------------------------------------------------------------------------------------
# per function: a temporary table of each name the function uses, in the caller's session, changes nothing

def _shadow(conn, name: str, rows: list[dict] = (), *, unique: tuple[str, ...] = ()) -> None:
    """A temporary table named `name` in `conn`'s session, with the real table's columns (no constraints), holding `rows`.
    CONTROL, every time: in this session the unqualified name now resolves to it."""
    conn.execute(f"CREATE TEMP TABLE {name} AS SELECT * FROM public.{name} WHERE false")
    if unique:
        conn.execute(f"CREATE UNIQUE INDEX ON pg_temp.{name} ({', '.join(unique)})")
    for row in rows:
        cols = list(row)
        conn.execute(f"INSERT INTO pg_temp.{name} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
                     [row[c] for c in cols])
    assert conn.execute("SELECT c.relnamespace = pg_my_temp_schema() FROM pg_class c WHERE c.oid = to_regclass(%s)",
                        (name,)).fetchone()[0] is True, f"control: {name} does not resolve to the shadow in this session"


def _snapshot(conn, name: str) -> list[str]:
    return sorted(repr(r) for r in conn.execute(f"SELECT * FROM pg_temp.{name}").fetchall())


def test_the_app_role_can_make_the_shadow_tables(owner):
    """The precondition the class rests on, and the one these tests use: meshychat_app may create temporary tables and may not
    create anything in public. CONTROL: a temporary table it makes is in its own temporary schema."""
    assert owner.execute("SELECT has_database_privilege('meshychat_app', current_database(), 'TEMPORARY'), "
                         "has_schema_privilege('meshychat_app', 'public', 'CREATE')").fetchone() == (True, False)
    with _fresh_app_session() as s:
        _shadow(s, "dashboard_users")


def test_shadowing_dashboard_users_does_not_change_the_tenant_predicate(owner):
    """meshy_same_tenant_or_devgroup (sql/039; the RLS predicate of meshy_memory and the dashboards). Two dev-plane owners
    in DIFFERENT trust groups: the answer is false. The shadow puts each in the other's group and both in a third, so a
    body reading the shadow through EITHER of its two references, or both, would answer true. CONTROL: the predicate says
    true for two owners of one group, so a true answer is reachable."""
    g1, g2, g3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    c, t, peer_of_c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    for oid, group in ((c, g1), (t, g2), (peer_of_c, g1)):
        owner.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id) VALUES (%s, %s, 'dev', %s)",
                      (oid, f"dpath-{oid.hex[:10]}@example.test", group))
    ask = "SELECT public.meshychat_same_tenant_or_devgroup(%s, %s)"
    assert owner.execute(ask, (c, t)).fetchone()[0] is False
    assert owner.execute(ask, (c, peer_of_c)).fetchone()[0] is True, "control: one group answers true"
    with _fresh_app_session() as s:
        _shadow(s, "dashboard_users", [
            {"id": c, "plane": "dev", "trust_group_id": g2}, {"id": c, "plane": "dev", "trust_group_id": g3},
            {"id": t, "plane": "dev", "trust_group_id": g1}, {"id": t, "plane": "dev", "trust_group_id": g3}])
        before = _snapshot(s, "dashboard_users")
        assert s.execute(ask, (c, t)).fetchone()[0] is False, "the predicate read the caller's temporary dashboard_users"
        assert _snapshot(s, "dashboard_users") == before


def test_shadowing_invocation_frames_does_not_change_the_standalone_prune_of_012(owner):
    """prune_invocation_frames (sql_meshychat/012). meshychat_app may not execute it (control below), so the caller is a session
    of its owner, as the operator's is. The real table has a standalone frame 4,000 days old; the shadow has two more, at
    ids no real frame has, so a body selecting or deleting through the shadow would return 2 or 0 and leave the real one.
    In one transaction, rolled back."""
    assert owner.execute("SELECT has_function_privilege('meshychat_app', 'prune_invocation_frames(integer, integer)', "
                         "'EXECUTE')").fetchone()[0] is False, "control: the app role cannot call it, so its owner does"
    with psycopg.connect(DSN, autocommit=True) as s, s.transaction(force_rollback=True):
        frame = F.standalone(s)["id"]                    # before the shadow: the fixture's INSERT is unqualified
        s.execute("UPDATE public.invocation_frames SET created_at = now() - interval '4000 days' WHERE id = %s",
                  (frame,))
        expected = s.execute("SELECT count(*) FROM public.invocation_frames WHERE chain_id IS NULL "
                             "AND created_at < now() - interval '3650 days'").fetchone()[0]
        assert expected >= 1
        old = s.execute("SELECT now() - interval '4000 days'").fetchone()[0]
        _shadow(s, "invocation_frames", [{"id": -1, "chain_id": None, "created_at": old},
                                         {"id": -2, "chain_id": None, "created_at": old}])
        before = _snapshot(s, "invocation_frames")
        assert s.execute("SELECT public.prune_invocation_frames(3650, 50000)").fetchone()[0] == expected
        assert s.execute("SELECT count(*) FROM public.invocation_frames WHERE id = %s", (frame,)).fetchone()[0] == 0
        assert _snapshot(s, "invocation_frames") == before


@pytest.fixture()
def standalone_window(owner):
    """The policy's standalone window set to 3,650 days for one test, and a real standalone frame 4,000 days old; both
    put back afterwards."""
    saved = owner.execute("SELECT chain_window_days, standalone_retain_days FROM invocation_frame_retention_policy"
                          ).fetchone()
    owner.execute("UPDATE invocation_frame_retention_policy SET standalone_retain_days = 3650")
    frame = F.standalone(owner)["id"]
    owner.execute("UPDATE invocation_frames SET created_at = now() - interval '4000 days' WHERE id = %s", (frame,))
    try:
        yield frame
    finally:
        owner.execute("DELETE FROM invocation_frames WHERE id = %s", (frame,))
        owner.execute("UPDATE invocation_frame_retention_policy SET chain_window_days = %s, "
                      "standalone_retain_days = %s", saved)


def test_shadowing_the_policy_and_the_frames_does_not_change_the_standalone_prune_of_026(standalone_window):
    """prune_standalone_invocation_frames (sql_meshychat/026), called by meshychat_app. The shadow policy turns standalone
    retention off (a body reading it returns NULL); the shadow frames are two old standalone ones at ids no real frame has
    (a body selecting or deleting through them returns 2 or 0). The answer is the real count, the real old frame goes, and
    neither shadow changes. In one transaction, rolled back."""
    frame = standalone_window
    with _fresh_app_session() as s, s.transaction(force_rollback=True):
        expected = s.execute("SELECT count(*) FROM public.invocation_frames WHERE chain_id IS NULL "
                             "AND created_at < now() - interval '3650 days'").fetchone()[0]
        assert expected >= 1
        old = s.execute("SELECT now() - interval '4000 days'").fetchone()[0]
        _shadow(s, "invocation_frame_retention_policy",
                [{"singleton": True, "chain_window_days": 90, "standalone_retain_days": None}])
        _shadow(s, "invocation_frames", [{"id": -1, "chain_id": None, "created_at": old},
                                         {"id": -2, "chain_id": None, "created_at": old}])
        before = {n: _snapshot(s, n) for n in ("invocation_frame_retention_policy", "invocation_frames")}
        assert s.execute("SELECT public.prune_standalone_invocation_frames(50000)").fetchone()[0] == expected
        assert s.execute("SELECT count(*) FROM public.invocation_frames WHERE id = %s", (frame,)).fetchone()[0] == 0
        assert {n: _snapshot(s, n) for n in before} == before


@pytest.mark.parametrize("scenario", ["complete", "grant_live_again", "frame_past_retired_head"])
def test_shadowing_every_table_of_the_chain_retirement_changes_nothing(owner, app_conn, scenario):
    """retire_invocation_chain (sql_meshychat/026), called by meshychat_app, on the three paths that write, each as
    test_frame_retention shows it:
      * `complete`: a chain whose root grant is 200 days revoked, three frames 400 days old, anchored: a new retirement
        that takes it whole, ('complete', NULL, 3);
      * `grant_live_again`: a five-frame chain one batch of two into its prune, its grant then revived: it stops;
      * `frame_past_retired_head`: the same, but a frame added past the retired head instead: it stops.
    Each shadow answers differently for EVERY reference to its name on that path:
      * the policy has chain retention off ('off');
      * skill_grants holds the root grant with the opposite liveness ('grant_live', or not stopping);
      * invocation_chain_retention holds a finished ledger row for the chain ('finished'), so a write through it changes it;
      * invocation_chain_heads holds a different anchor ('anchor_changed'), so a delete through it removes it;
      * invocation_frames holds frames of the chain at id -1 (at or below any head) and, except where the real chain has a
        frame past the head, at 9e18 (past it), so each read and the delete would see a different chain.
    The answer is the real one, the real frames, anchor and ledger are what it leaves, and no shadow changed. In one
    transaction, rolled back. (The two stop paths are here because an occurrence sweep found their UPDATEs unexercised.)"""
    if scenario == "complete":
        gid = _grant(owner, revoked_days_ago=200)
        _chain(owner, app_conn, gid, 3)
        head, count = _anchor(owner, gid)[1], _anchor(owner, gid)[3]
        retired, live, left, expected = head, False, 0, [("complete", None, 3)]
        ledger = [("complete", None, 3, head)]
    else:
        gid = _grant(owner, expires_days=-200)
        frames = _chain(owner, app_conn, gid, 5)
        retired = _anchor(owner, gid)[1]
        assert _retire(app_conn, gid, batch=2).outcome == FR.STARTED
        if scenario == "grant_live_again":
            owner.execute("UPDATE skill_grants SET expires_at = now() + interval '30 days' WHERE id = %s", (gid,))
            live, left = True, 3
        else:
            F.extend(owner, frames[:3], chain_id=gid, sign=True)
            live, left = False, 4
        head = count = 0                      # what the job passes once the anchor is retired (_retire's default)
        expected, ledger = [("stopped", scenario, 0)], [("stopped", scenario, 2, retired)]
    with _fresh_app_session() as s, s.transaction(force_rollback=True):
        old = s.execute("SELECT now() - interval '400 days'").fetchone()[0]
        _shadow(s, "invocation_frame_retention_policy",
                [{"singleton": True, "chain_window_days": None, "standalone_retain_days": None}])
        _shadow(s, "skill_grants", [{"id": gid, "revoked_at": None if not live else old, "expires_at": None}])
        _shadow(s, "invocation_chain_retention", [{"chain_id": gid, "status": "complete", "frames_deleted": 0,
                                                   "anchor_head_frame_id": retired, "finished_at": None}])
        _shadow(s, "invocation_chain_heads", [{"chain_id": gid, "head_frame_id": -1, "frame_count": -1,
                                               "head_hash": "shadow"}])
        shadow_frames = [{"id": -1, "chain_id": gid, "frame_hash": "shadow-low", "created_at": old}]
        if scenario != "frame_past_retired_head":
            shadow_frames.append({"id": 9_000_000_000_000_000_000, "chain_id": gid, "frame_hash": "shadow-high",
                                  "created_at": old})
        _shadow(s, "invocation_frames", shadow_frames)
        names = ("invocation_frame_retention_policy", "skill_grants", "invocation_chain_retention",
                 "invocation_chain_heads", "invocation_frames")
        before = {n: _snapshot(s, n) for n in names}
        got = s.execute("SELECT * FROM public.retire_invocation_chain(%s, %s, %s, 1000)", (gid, head, count)).fetchall()
        assert got == expected, got
        assert s.execute("SELECT count(*) FROM public.invocation_frames WHERE chain_id = %s",
                         (gid,)).fetchone()[0] == left
        assert s.execute("SELECT count(*) FROM public.invocation_chain_heads WHERE chain_id = %s",
                         (gid,)).fetchone()[0] == 0
        assert s.execute("SELECT status, stop_reason, frames_deleted, anchor_head_frame_id "
                         "FROM public.invocation_chain_retention WHERE chain_id = %s", (gid,)).fetchall() == ledger
        assert {n: _snapshot(s, n) for n in names} == before


def test_shadowing_skill_calls_does_not_change_the_referencing_calls(owner):
    """file_referencing_calls (sql_meshychat/041), called by meshychat_app inside the server_internal binding. One real call
    references the file; the shadow holds two other calls referencing it, one per array, so a body reading the shadow
    through either branch would return them. The answer is the real call alone."""
    with psycopg.connect(DSN) as seed:
        p = _seed_peer(seed, "dpath")
        skill = _seed_skill(seed, p["peer_id"])
        fid = uuid.uuid4()
        call_id = _seed_call(seed, caller=p, target=p, file_id=fid, status="completed", skill_id=skill)
    real = owner.execute("SELECT id, status, caller_peer_id, target_peer_id FROM skill_calls WHERE call_id = %s",
                         (call_id,)).fetchall()
    assert len(real) == 1
    stranger = uuid.uuid4()
    with _fresh_app_session() as s:
        _shadow(s, "skill_calls", [
            {"id": uuid.uuid4(), "status": "shadow", "caller_peer_id": stranger, "target_peer_id": stranger,
             "args_attachments": [fid], "result_attachments": []},
            {"id": uuid.uuid4(), "status": "shadow", "caller_peer_id": stranger, "target_peer_id": stranger,
             "args_attachments": [], "result_attachments": [fid]}])
        before = _snapshot(s, "skill_calls")
        with rls.server_internal(s):
            got = s.execute("SELECT * FROM public.file_referencing_calls(%s)", (fid,)).fetchall()
        assert got == real, got
        assert _snapshot(s, "skill_calls") == before


def _stamps(owner, memory_id) -> list[str]:
    return [r[0] for r in owner.execute("SELECT kind FROM mesh_commit_order WHERE memory_id = %s ORDER BY kind",
                                        (memory_id,)).fetchall()]


def _shadow_the_commit_order(s) -> dict:
    """The stampers' two names, shadowed: shared_memory EMPTY (a stamper reading it finds no row and stamps nothing) and
    mesh_commit_order empty with the real table's unique key (a stamper writing it stamps there instead)."""
    _shadow(s, "shared_memory")
    _shadow(s, "mesh_commit_order", unique=("memory_id", "kind"))
    return {n: _snapshot(s, n) for n in ("shared_memory", "mesh_commit_order")}


def test_shadowing_the_commit_order_does_not_change_a_writes_stamp(owner):
    """meshy_mesh_commit_stamp (sql_meshychat/027), the deferred trigger on a mail row's INSERT, runs at COMMIT in the
    writer's session. A meshychat_app session inserts a mail row as the server-internal write path does, THEN shadows both
    names in the same transaction, then commits: the row gets its one 'write' position in the real order, and both
    shadows are still empty."""
    author = mesh._peer(owner, "dpw")
    mesh._admit(owner, mesh.MESH, author)
    key = mesh._key(author["name"])
    cols = sr.row(author["peer_id"], author["owner_id"], mesh.MESH, key, mesh._value(author["name"], "a message"),
                  author_label=author["fallback"])
    with _fresh_app_session() as s:
        with rls.server_internal(s):
            rid = sr.insert(s, cols)                     # before the shadow: the fixture's INSERT is unqualified
            before = _shadow_the_commit_order(s)
        assert {n: _snapshot(s, n) for n in before} == before
    assert _stamps(owner, rid) == ["write"], "the write's stamp went somewhere else"


def test_shadowing_the_ctx_commit_order_does_not_change_a_manifests_stamp(owner):
    """meshy_ctx_commit_stamp (sql_meshychat/047, v1.1 step 6), the deferred trigger on a context MANIFEST's INSERT. A
    meshychat_app session inserts a manifest row in the configured namespace, THEN shadows its three names in the same
    transaction (the config naming ANOTHER namespace, so a shadowed read would stamp nothing), then commits: the manifest
    gets its one 'write' position in the real ctx_commit_order, and every shadow is as it was."""
    ns = f"shared/ctxidx-{uuid.uuid4().hex[:10]}"
    # The config is one shared row: hold tests/test_ctx_commit_order.py's lock across the change and its restore.
    owner.execute("SELECT pg_advisory_lock(1835233133, 990)")
    prior = owner.execute("SELECT namespace FROM ctx_index_config WHERE id = 1").fetchone()[0]
    owner.execute("UPDATE ctx_index_config SET namespace = %s WHERE id = 1", (ns,))
    try:
        author = mesh._peer(owner, "dpc")
        mesh._admit(owner, ns, author)
        key = f"ctxm/{author['fallback']}/{uuid.uuid4().hex}/0001/000001"
        cols = sr.row(author["peer_id"], author["owner_id"], ns, key, {"schema": "context.v1", "kind": "manifest"},
                      author_label=author["fallback"])
        with _fresh_app_session() as s:
            with rls.server_internal(s):
                rid = sr.insert(s, cols)
                _shadow(s, "shared_memory")
                _shadow(s, "ctx_commit_order", unique=("memory_id", "kind"))
                _shadow(s, "ctx_index_config", [{"id": 1, "namespace": "shared/elsewhere", "updated_at": "now"}])
                before = {n: _snapshot(s, n) for n in ("shared_memory", "ctx_commit_order", "ctx_index_config")}
            assert {n: _snapshot(s, n) for n in before} == before
        stamps = [r[0] for r in owner.execute("SELECT kind FROM ctx_commit_order WHERE memory_id = %s",
                                              (rid,)).fetchall()]
        assert stamps == ["write"], "the manifest's stamp went somewhere else"
    finally:
        owner.execute("UPDATE ctx_index_config SET namespace = %s WHERE id = 1", (prior,))
        owner.execute("SELECT pg_advisory_unlock(1835233133, 990)")


def test_shadowing_the_chunk_counter_does_not_change_a_writes_bump(owner):
    """meshy_ctx_chunk_changed (sql_meshychat/049), the deferred trigger on a context CHUNK's insert, update or delete. A
    meshychat_app session writes a chunk as the walk does, THEN shadows ctx_chunk_changes in the same transaction (holding
    a row for the space, so a shadowed write would land there), then commits: the real counter has the one bump, and the
    shadow is as it was."""
    author = mesh._peer(owner, "dpk")
    ns = f"shared/ctxcnt-{uuid.uuid4().hex[:10]}"
    mesh._admit(owner, ns, author)
    src = sr.insert(owner, sr.row(author["peer_id"], author["owner_id"], ns, f"ctxm/x/{uuid.uuid4().hex}/0001/000001",
                                  {"schema": "context.v1", "kind": "manifest"}, author_label=author["fallback"]))
    space = f"fake-8/8/ctxcnt-{uuid.uuid4().hex[:8]}"
    try:
        with _fresh_app_session() as s:
            with rls.projection_writer(s):
                s.execute("INSERT INTO mesh_ctx_chunks (src_id, space, namespace, chunk_no, seat, src, gen, batch, "
                          "first_ord, last_ord, char_start, char_end, time_first, time_last, text_sha256) VALUES "
                          "(%s, %s, %s, 0, 's', 'x', 1, 1, 0, 0, 0, 1, now(), now(), repeat('a', 64))", (src, space, ns))
                _shadow(s, "ctx_chunk_changes", [{"space": space, "changes": 100, "changed_at": "now"}])
                before = _snapshot(s, "ctx_chunk_changes")
            assert _snapshot(s, "ctx_chunk_changes") == before
        got = owner.execute("SELECT changes FROM ctx_chunk_changes WHERE space = %s", (space,)).fetchone()
        assert got == (1,), f"the chunk's bump went somewhere else: {got}"
    finally:
        owner.execute("DELETE FROM mesh_ctx_chunks WHERE space = %s", (space,))
        owner.execute("DELETE FROM ctx_chunk_changes WHERE space = %s", (space,))


def test_shadowing_the_shard_plan_does_not_change_where_a_chunk_is_planned(owner):
    """The sql_meshychat/053 helpers, reached from the (non-definer) triggers on a context chunk's insert and delete. A
    meshychat_app session shadows ctx_shard_plan and ctx_shard_setting (a shard_max of 1, so a shadowed read would cut
    differently), then inserts and deletes a chunk as the walk does: the real plan gets the chunk's shard and then loses it,
    and both shadows are as they were."""
    author = mesh._peer(owner, "dsp")
    ns = f"shared/ctxplan-{uuid.uuid4().hex[:10]}"
    mesh._admit(owner, ns, author)
    src = sr.insert(owner, sr.row(author["peer_id"], author["owner_id"], ns, f"ctxm/x/{uuid.uuid4().hex}/0001/000001",
                                  {"schema": "context.v1", "kind": "manifest"}, author_label=author["fallback"]))
    space = f"fake-8/8/ctxplan-{uuid.uuid4().hex[:8]}"
    planned = "SELECT count(*), coalesce(sum(n), 0) FROM ctx_shard_plan WHERE space = %s"
    try:
        with _fresh_app_session() as s:
            with rls.projection_writer(s):
                _shadow(s, "ctx_shard_plan")
                _shadow(s, "ctx_shard_setting", [{"one": True, "shard_max": 1}])
                before = (_snapshot(s, "ctx_shard_plan"), _snapshot(s, "ctx_shard_setting"))
                for k in range(2):
                    s.execute("INSERT INTO mesh_ctx_chunks (src_id, space, namespace, chunk_no, seat, src, gen, batch, "
                              "first_ord, last_ord, char_start, char_end, time_first, time_last, text_sha256) VALUES "
                              "(%s, %s, %s, %s, 's', 'x', 1, 1, 0, 0, 0, 1, now(), now(), repeat('a', 64))",
                              (src, space, ns, k))
            assert (_snapshot(s, "ctx_shard_plan"), _snapshot(s, "ctx_shard_setting")) == before
        assert owner.execute(planned, (space,)).fetchone() == (1, 2), "one shard of two rows, cut at the real 50000"
        with _fresh_app_session() as s:
            with rls.projection_writer(s):
                _shadow(s, "ctx_shard_plan")
                s.execute("DELETE FROM mesh_ctx_chunks WHERE space = %s", (space,))
        assert owner.execute(planned, (space,)).fetchone() == (0, 0), "the delete emptied the real plan"
    finally:
        owner.execute("DELETE FROM mesh_ctx_chunks WHERE space = %s", (space,))
        owner.execute("DELETE FROM ctx_chunk_changes WHERE space = %s", (space,))


def test_shadowing_the_commit_order_does_not_change_an_edits_stamp(owner, app_conn):
    """meshy_mesh_edit_stamp (sql_meshychat/029), the deferred trigger on an edit record's INSERT. The edited version is
    written first (its own 'write' stamp is drawn then); a meshychat_app session inserts the record the handler writes, then
    shadows both names, then commits: the version gets its one 'edit' position, and both shadows are still empty."""
    author, sibling, key, value = _author_and_sibling(owner, app_conn, "dpe")
    head = mesh._head(owner, mesh.MESH, key)
    _direct(app_conn, head, peer=sibling, value=dict(value, body="an edit"), mark=author["owner_id"])
    new = mesh._head(owner, mesh.MESH, key)
    assert _stamps(owner, new["id"]) == ["write"]
    with _fresh_app_session() as s:
        with rls.server_internal(s):
            s.execute("INSERT INTO public.shared_memory_edit (memory_id, namespace, key, version, prior_version, "
                      "edited_by_peer_id, edited_by_owner_id) VALUES (%s, %s, %s, 2, 1, %s, %s)",
                      (new["id"], mesh.MESH, key, sibling["peer_id"], sibling["owner_id"]))
            before = _shadow_the_commit_order(s)
        assert {n: _snapshot(s, n) for n in before} == before
    assert _stamps(owner, new["id"]) == ["edit", "write"], "the edit's stamp went somewhere else"


def test_shadowing_the_commit_order_does_not_change_a_redactions_stamp(owner, app_conn):
    """meshy_mesh_redaction_stamp (sql_meshychat/027), the deferred trigger on a redaction record's INSERT. A meshychat_app
    session inserts the record for a real mail row, then shadows both names, then commits: the row gets its one
    'redaction' position, and both shadows are still empty."""
    author = mesh._peer(owner, "dpr")
    mesh._admit(owner, mesh.MESH, author)
    key = mesh._key(author["name"])
    mesh._write(app_conn, author, mesh._msg(mesh.MESH, key, mesh._value(author["name"], "a message")))
    head = mesh._head(owner, mesh.MESH, key)
    assert _stamps(owner, head["id"]) == ["write"]
    with _fresh_app_session() as s:
        with rls.server_internal(s):
            s.execute("INSERT INTO public.shared_memory_redaction (memory_id, namespace, key, version, "
                      "redacted_by_peer_id, redacted_by_owner_id, acted_as, reason_code, request_signed_bytes) "
                      "VALUES (%s, %s, %s, 1, %s, %s, 'author', 'sent_in_error', %s)",
                      (head["id"], mesh.MESH, key, author["peer_id"], author["owner_id"], b"request"))
            before = _shadow_the_commit_order(s)
        assert {n: _snapshot(s, n) for n in before} == before
    assert _stamps(owner, head["id"]) == ["redaction", "write"], "the redaction's stamp went somewhere else"


def test_shadowing_the_card_head_record_does_not_change_where_a_cards_record_is_kept(owner):
    """meshy_card_head_track (sql_meshychat/057, divergence 110), the AFTER row trigger on a card/ row's INSERT or UPDATE.
    A meshychat_app session shadows card_heads FIRST (the trigger runs inside the INSERT, not at commit), then inserts a
    card row as the server-internal write path does: the real record holds the row, and the shadow is still empty."""
    author = mesh._peer(owner, "dph")
    ns = f"shared/cardrec-{uuid.uuid4().hex[:10]}"
    mesh._admit(owner, ns, author)
    key = f"card/{author['fallback']}/{uuid.uuid4().hex}"
    cols = sr.row(author["peer_id"], author["owner_id"], ns, key,
                  {"schema": "card.v1", "type": "Ruling", "seat": author["fallback"], "time": "2026-10-08T07:00:00.000Z",
                   "text": "Ruling | a"}, author_label=author["fallback"])
    with _fresh_app_session() as s:
        with rls.server_internal(s):
            _shadow(s, "card_heads")
            before = _snapshot(s, "card_heads")
            rid = sr.insert(s, cols)
            assert _snapshot(s, "card_heads") == before
    got = owner.execute("SELECT memory_id, has_text, seat FROM card_heads WHERE namespace = %s AND key = %s",
                        (ns, key)).fetchone()
    assert got == (uuid.UUID(str(rid)), True, author["fallback"]), f"the card's record went somewhere else: {got}"
