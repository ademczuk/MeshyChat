"""The revocation table and the issuance/revocation event log (B07 slice 4; design
docs/design/biscuit-capabilities.md 2.5 (a) and (c), Q8, critique C18; divergence 040;
`sql_meshychat/019`).

WHAT IS PINNED:

  * `capability_revocation`, exactly the design's columns: revocation_id BYTEA PRIMARY KEY (one
    block's 64-byte id), revoked_at, revoked_by_peer (a peer), reason, not_after (the token's own
    expiry, kept for a LATER prune decision; there is no prune, Q8).
  * `capability_event`, the append-only issuance/revocation event log. Its `issue` rows ARE the
    issuance register of 2.5 (c): issuer, subject, namespace, rights, depth cap, expiry, kid and
    the authority revocation id, one row per authority id. Never the token: no column can hold
    it. A `revoke` row records who revoked which block id and why.
  * Row-level security like the memory tables (apps/memoryskills/db.py): ENABLE and FORCE, a
    SELECT policy and an INSERT policy bound to `app.server_internal = '1'`, and NO UPDATE or
    DELETE policy (029's write shape, sql_meshychat/012), so append-only is the database's
    property. The app role also holds no UPDATE or DELETE privilege on either table, so an
    attempt is an error rather than a silent nothing.
  * Nothing prunes either table: no job, and no DELETE of them anywhere under apps/.

Every refusal is paired with the write that succeeds beside it (docs/V1-DESIGN-RULES.md C1).

Run:  pytest tests/test_capability_tables.py -q
Needs the dev database (tests/conftest.py builds and drops its own).
"""
from __future__ import annotations

import ast
import datetime as _dt
import json
import os
import re
import sys
import uuid
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")
MIGRATION = ROOT / "sql_meshychat" / "019_capability_revocation.sql"
TABLES = ("capability_revocation", "capability_event")
NOW = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)


@pytest.fixture()
def owner():
    with psycopg.connect(DSN, autocommit=True) as conn:
        yield conn


@pytest.fixture()
def app():
    from apps.core import dbconn
    conn = dbconn.connect(DSN)
    try:
        yield conn
    finally:
        conn.close()


def _peer(owner) -> uuid.UUID:
    owner_id, peer_id = uuid.uuid4(), uuid.uuid4()
    with owner.cursor() as cur:
        cur.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id) "
                    "VALUES (%s,%s,'dev',%s)",
                    (owner_id, f"cap19-{owner_id.hex[:8]}@example.test", uuid.uuid4()))
        cur.execute("INSERT INTO peers (id, owner_id, name) VALUES (%s,%s,%s)",
                    (peer_id, owner_id, f"cap19-{peer_id.hex[:8]}"))
    return peer_id


def _rid() -> bytes:
    return os.urandom(64)


def _revocation(peer, rid=None) -> dict:
    return {"revocation_id": rid or _rid(), "revoked_by_peer": peer, "reason": "cap19 test",
            "not_after": NOW + _dt.timedelta(hours=1)}


def _issue(peer, aid=None, **over) -> dict:
    """An issue row. A value in `over` may be a function of the time now (a margin of a
    minute each way keeps it clear of the database clock's skew and the test's own age)."""
    now = _dt.datetime.now(_dt.timezone.utc)
    row = {"event": "issue", "actor_peer_id": peer, "authority_revocation_id": aid or _rid(),
           "issuer_peer_id": peer, "subject_peer_id": peer, "namespace": "cap19/ns",
           "rights": json.dumps([{"prefix": "", "action": "read"}]), "depth_cap": 2,
           "expires_at": now + _dt.timedelta(hours=1), "kid": 7}
    row.update({k: v(now) if callable(v) else v for k, v in over.items()})
    return row


def _revoke_event(peer, aid=None, **over) -> dict:
    row = {"event": "revoke", "actor_peer_id": peer, "authority_revocation_id": aid or _rid(),
           "revocation_id": _rid(), "reason": "cap19 test"}
    row.update(over)
    return row


def _insert(cur, table: str, row: dict) -> None:
    cols = list(row)
    marks = ", ".join("%s::jsonb" if c == "rights" else "%s" for c in cols)
    cur.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({marks})",
                [row[c] for c in cols])


def _count(owner, table: str, where: str, args) -> int:
    with owner.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table} WHERE {where}", args)
        return cur.fetchone()[0]


# ============================================================================ the file

def test_the_migration_is_lf_and_numbered_019():
    """sql_meshychat/*.sql are LF (.gitattributes; discover() refuses CRLF). 019, not 017:
    017 is on main (divergence 037's summaries), 016 and 018 are the skill frames'."""
    from apps.core import sqlmigrate
    raw = MIGRATION.read_bytes()
    assert b"\r\n" not in raw and raw.endswith(b"\n")
    plan = {v: p.name for v, _d, p, _c in sqlmigrate.discover(MIGRATION.parent)}
    assert plan[19] == MIGRATION.name
    assert plan[15] == "015_namespace_permission_predicate.sql", "control: 015 is slice 3's"


# ============================================================================ the columns

def _columns(owner, table: str) -> dict:
    with owner.cursor() as cur:
        cur.execute("SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = %s", (table,))
        return {r[0]: (r[1], r[2]) for r in cur.fetchall()}


def test_the_revocation_table_is_the_designs(owner):
    assert _columns(owner, "capability_revocation") == {
        "revocation_id": ("bytea", "NO"),
        "revoked_at": ("timestamp with time zone", "NO"),
        "revoked_by_peer": ("uuid", "NO"),
        "reason": ("text", "NO"),
        "not_after": ("timestamp with time zone", "NO"),
    }
    with owner.cursor() as cur:
        cur.execute("SELECT a.attname FROM pg_index i JOIN pg_attribute a ON "
                    "a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
                    "WHERE i.indrelid = 'capability_revocation'::regclass AND i.indisprimary")
        assert [r[0] for r in cur.fetchall()] == ["revocation_id"]
        cur.execute("SELECT confrelid::regclass::text FROM pg_constraint "
                    "WHERE conrelid = 'capability_revocation'::regclass AND contype = 'f'")
        assert [r[0] for r in cur.fetchall()] == ["peers"]


def test_the_event_log_holds_the_register_and_never_the_token(owner):
    cols = _columns(owner, "capability_event")
    assert {c: t for c, (t, _n) in cols.items()} == {
        "id": "bigint", "event": "text", "at": "timestamp with time zone",
        "actor_peer_id": "uuid", "authority_revocation_id": "bytea",
        "issuer_peer_id": "uuid", "subject_peer_id": "uuid", "namespace": "text",
        "rights": "jsonb", "depth_cap": "smallint", "expires_at": "timestamp with time zone",
        "kid": "bigint", "revocation_id": "bytea", "reason": "text",
    }
    for table in TABLES:
        assert not [c for c in _columns(owner, table) if "token" in c or "biscuit" in c], table


# ============================================================================ the checks

def test_the_register_takes_one_issue_row_per_authority_id(owner):
    peer = _peer(owner)
    aid = _rid()
    with owner.cursor() as cur:
        _insert(cur, "capability_event", _issue(peer, aid))
        with pytest.raises(psycopg.errors.UniqueViolation):
            _insert(cur, "capability_event", _issue(peer, aid))
        # CONTROL: revoke rows name the same authority id as often as blocks are revoked.
        _insert(cur, "capability_event", _revoke_event(peer, aid))
        _insert(cur, "capability_event", _revoke_event(peer, aid))
    assert _count(owner, "capability_event", "authority_revocation_id = %s", (aid,)) == 3


@pytest.mark.parametrize("over,why", [
    ({"authority_revocation_id": b"\x01" * 63}, "a 63-byte id"),
    ({"depth_cap": 5}, "depth cap past 4"),
    ({"depth_cap": -1}, "negative depth cap"),
    ({"expires_at": lambda now: now + _dt.timedelta(hours=24, seconds=60)},
     "past 24 hours (Q3)"),
    ({"expires_at": lambda now: now - _dt.timedelta(seconds=60)}, "already expired"),
    ({"kid": -1}, "negative kid"),
    ({"kid": 2 ** 32}, "kid past u32"),
    ({"rights": json.dumps([{"prefix": "", "action": "admin"}])}, "admin in a token"),
    ({"rights": json.dumps([])}, "no rights"),
    ({"rights": json.dumps({"prefix": "", "action": "read"})}, "rights not an array"),
    ({"rights": json.dumps([{"prefix": 7, "action": "read"}])}, "a prefix not a string"),
    ({"issuer_peer_id": None}, "no issuer"),
    ({"subject_peer_id": None}, "no subject"),
    ({"namespace": None}, "no namespace"),
    ({"kid": None}, "no kid"),
    ({"reason": "why"}, "a reason on an issue row"),
    ({"revocation_id": b"\x02" * 64}, "a revoked id on an issue row"),
    ({"actor_peer_id": uuid.uuid4()}, "an actor who is not the issuer"),
    ({"event": "update"}, "an unknown event"),
], ids=lambda v: v if isinstance(v, str) else None)
def test_an_issue_row_outside_the_register_shape_is_refused(owner, over, why):
    peer = _peer(owner)
    with owner.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation):
            _insert(cur, "capability_event", _issue(peer, **over))
        _insert(cur, "capability_event", _issue(peer))         # CONTROL: the shape itself


@pytest.mark.parametrize("over,why", [
    ({"revocation_id": None}, "no revoked id"),
    ({"revocation_id": b"\x03" * 65}, "a 65-byte revoked id"),
    ({"reason": None}, "no reason"),
    ({"issuer_peer_id": uuid.uuid4()}, "a register column on a revoke row"),
    ({"rights": json.dumps([{"prefix": "", "action": "read"}])}, "rights on a revoke row"),
], ids=lambda v: v if isinstance(v, str) else None)
def test_a_revoke_row_outside_its_shape_is_refused(owner, over, why):
    peer = _peer(owner)
    with owner.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation):
            _insert(cur, "capability_event", _revoke_event(peer, **over))
        _insert(cur, "capability_event", _revoke_event(peer))  # CONTROL


def test_a_revocation_is_one_64_byte_block_id_by_a_real_peer(owner):
    peer = _peer(owner)
    with owner.cursor() as cur:
        for bad in (b"\x04" * 32, b"\x04" * 65):
            with pytest.raises(psycopg.errors.CheckViolation):
                _insert(cur, "capability_revocation", _revocation(peer, bad))
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            _insert(cur, "capability_revocation", _revocation(uuid.uuid4()))
        row = _revocation(peer)
        _insert(cur, "capability_revocation", row)              # CONTROL
        with pytest.raises(psycopg.errors.UniqueViolation):
            _insert(cur, "capability_revocation", row)
    assert _count(owner, "capability_revocation", "revocation_id = %s",
                  (row["revocation_id"],)) == 1


# ============================================================================ RLS

def test_rls_is_forced_with_select_and_insert_policies_only_bound_to_server_internal(owner):
    with owner.cursor() as cur:
        cur.execute("SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = ANY(%s)", (list(TABLES),))
        assert sorted(cur.fetchall()) == sorted((t, True, True) for t in TABLES)
        cur.execute("SELECT tablename, cmd, coalesce(qual, ''), coalesce(with_check, '') "
                    "FROM pg_policies WHERE schemaname = 'public' AND tablename = ANY(%s)",
                    (list(TABLES),))
        rows = cur.fetchall()
    for table in TABLES:
        mine = {cmd: (qual, check) for t, cmd, qual, check in rows if t == table}
        assert set(mine) == {"SELECT", "INSERT"}, (table, sorted(mine))
        assert "app.server_internal" in mine["SELECT"][0], mine
        assert "app.server_internal" in mine["INSERT"][1], mine


def test_the_app_role_holds_no_update_or_delete_privilege(owner):
    with owner.cursor() as cur:
        for table in TABLES:
            cur.execute("SELECT has_table_privilege('meshychat_app', %s, 'UPDATE'), "
                        "has_table_privilege('meshychat_app', %s, 'DELETE'), "
                        "has_table_privilege('meshychat_app', %s, 'TRUNCATE'), "
                        "has_table_privilege('meshychat_app', %s, 'INSERT'), "
                        "has_table_privilege('meshychat_app', %s, 'SELECT')", (table,) * 5)
            assert cur.fetchone() == (False, False, False, True, True), table


def test_the_server_writes_and_reads_and_can_never_rewrite_or_remove(owner, app):
    """As meshychat_app under server_internal: the INSERT and the read land (the positive
    control); an UPDATE and a DELETE of the same rows are refused, and the rows are intact."""
    from apps.core import rls

    peer = _peer(owner)
    rev, event = _revocation(peer), _issue(peer)
    with rls.server_internal(app), app.cursor() as cur:
        _insert(cur, "capability_revocation", rev)
        _insert(cur, "capability_event", event)
        cur.execute("SELECT count(*) FROM capability_revocation WHERE revocation_id = %s",
                    (rev["revocation_id"],))
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT issuer_peer_id FROM capability_event "
                    "WHERE authority_revocation_id = %s", (event["authority_revocation_id"],))
        assert cur.fetchone()[0] == peer

    for sql, args in (
            ("UPDATE capability_revocation SET reason = 'x' WHERE revocation_id = %s",
             (rev["revocation_id"],)),
            ("DELETE FROM capability_revocation WHERE revocation_id = %s",
             (rev["revocation_id"],)),
            ("DELETE FROM capability_revocation", ()),
            ("UPDATE capability_event SET expires_at = now() + interval '1 day' "
             "WHERE authority_revocation_id = %s", (event["authority_revocation_id"],)),
            ("DELETE FROM capability_event", ()),
            ("TRUNCATE capability_revocation", ()),
            ("TRUNCATE capability_event", ())):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with rls.server_internal(app), app.cursor() as cur:
                cur.execute(sql, args)
    assert _count(owner, "capability_revocation", "revocation_id = %s AND reason = %s",
                  (rev["revocation_id"], rev["reason"])) == 1
    assert _count(owner, "capability_event", "authority_revocation_id = %s AND expires_at = %s",
                  (event["authority_revocation_id"], event["expires_at"])) == 1


def test_outside_server_internal_the_app_role_neither_reads_nor_writes(owner, app):
    """Contextless, and bound to a peer as a request is: no row is visible and an INSERT is
    refused by the policy. CONTROL: the owner sees the row the app role cannot."""
    from apps.core import rls

    peer = _peer(owner)
    rev = _revocation(peer)
    with owner.cursor() as cur:
        _insert(cur, "capability_revocation", rev)
    for bind in ("none", "peer"):
        ctx = (rls.peer_context(app, peer_id=peer, owner_id=None) if bind == "peer"
               else app.transaction())
        with ctx, app.cursor() as cur:
            cur.execute("SELECT count(*) FROM capability_revocation WHERE revocation_id = %s",
                        (rev["revocation_id"],))
            assert cur.fetchone()[0] == 0, bind
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            ctx = (rls.peer_context(app, peer_id=peer, owner_id=None) if bind == "peer"
                   else app.transaction())
            with ctx, app.cursor() as cur:
                _insert(cur, "capability_revocation", _revocation(peer))
    assert _count(owner, "capability_revocation", "revocation_id = %s",
                  (rev["revocation_id"],)) == 1


# ============================================================================ no prune (Q8)

def test_nothing_under_apps_deletes_from_or_prunes_the_capability_tables():
    """Q8: revocations are never pruned in v1; the event log is an audit (V1 rule A1). No
    DELETE or TRUNCATE naming either table in any string under apps/ (the job registry
    included), and no job named for them. CONTROL: the scan finds a DELETE it should."""
    pattern = re.compile(r"\b(DELETE\s+FROM|TRUNCATE)\s+(ONLY\s+)?\"?(\w+)", re.I)
    found, control = [], False
    for p in (ROOT / "apps").rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for m in pattern.finditer(node.value):
                    if m.group(3) == "shared_memory":
                        control = True
                    if m.group(3).startswith("capability_"):
                        found.append((str(p.relative_to(ROOT)), m.group(0)))
    assert control, "control: the scan finds handle_prune's DELETE FROM shared_memory"
    assert not found, found
    registry = (ROOT / "apps" / "jobs" / "registry.py").read_text(encoding="utf-8")
    assert "capability" not in registry.lower()
