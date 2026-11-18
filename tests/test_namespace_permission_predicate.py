"""The four-edge table test (design 3.3): ONE namespace predicate,
`meshy_namespace_permission(namespace, peer)`, called by every layer and re-expressed by none
(divergence 040, slice 3). Divergence 040 and the slice's commits call this file T3, the
slice's number: it is not the design's T3 (6.1, holder binding), which slice 4 writes.

WHY (`docs/V1-DESIGN-RULES.md` A2; design `docs/design/biscuit-capabilities.md` 3.3 and 3.5;
critique C13). The namespace decision was written three times: `check_permission` in Python,
`memory.list`'s enumeration (`handlers._list_visible_namespaces`) and `/v1/mesh/wait`'s
visibility (`wait._PEER_VIS_CTE`, which the plain wait and 031's addressed scan both read).
The slice moves it into one SQL function that all three call, in two forms:
`meshy_namespace_permission(peer)`, every namespace the peer owns or holds a live grant on
with its answer (the predicate, written once), and `meshy_namespace_permission(namespace,
peer)`, one namespace's answer read from it. "Behaviour identical" is a PREDICTION about code
not yet written (C13) until a test covers the predicate's edges, so the table below is
written first:

  owner      the namespace's owner holds implicit admin, reported via='owner', even when it
             also holds a lower grant row of its own
  newest     otherwise the NEWEST live grant row decides (ORDER BY created_at DESC LIMIT 1),
             so a newer lower grant beats an older higher one; a revoked row is never it
  expired    a row whose expires_at has passed is ignored; one that expires later is live
  unclaimed  an unclaimed namespace is E_NAMESPACE_UNCLAIMED, never E_PERMISSION_DENIED

The predicate is keyed on the PEER. `owner_owner_id` and `grantee_owner_id`, the owner
ACCOUNT, sit on the same rows, and the dashboard's `tenant_scope` keys on them by design (an
owner session, not a peer). So the owner and newest edges each have rows where two peers
share one account: a sibling of the claimant is not the owner, and a sibling of a grantee
does not hold its grant. Without them no row could tell a peer from its account (040 slice
3's review, F1).

Each row is asked for `read`, `write` and `admin` of `check_permission` (the exact return
dict, or the exact code AND message), and for visibility of `_list_visible_namespaces`
(present, and `is_owner`) and the wait's `visible_namespaces`, all on a `meshychat_app`
connection under `rls.server_internal`, the way the server asks.

THE NEWEST-ROW EDGE CANNOT HAPPEN ON THIS SCHEMA. The partial unique index
`shared_memory_grant_active_unique (namespace, grantee_peer_id) WHERE revoked = FALSE`
(sql/028) allows one unrevoked row per pair, so two LIVE rows never exist and `ORDER BY`
decides nothing today (`test_two_live_rows_are_refused_by_the_schema` is the measurement).
The rows marked `relaxed` build that state anyway, on the owner connection, inside ONE
transaction that drops the index and is rolled back, so nothing outlives the test. They hold
the function to the design's rule, and they are the only rows that can tell `DESC` from
`ASC`. `_list_visible_namespaces` cannot be asked there: it opens its own server_internal
transaction and refuses a connection with one already open (apps/core/rls.py).

THE TABLE IS PROVED AGAINST WRONG FUNCTIONS IN EVERY RUN: each plant below is the real
function's own definition (`pg_get_functiondef`) with one clause changed, installed under the
real name, and the table must find it wrong on the edge the plant breaks; the real definition
is restored byte for byte after each. A plant whose needle is not in the definition fails
loudly, so a rewritten function cannot silently disarm them.

THE COST. The two queries that ask about every namespace a peer can read must not call a
function once per claimed namespace: measured on the dev Postgres, that made the wait's
visibility 5.5 times slower at 100 claimed namespaces and 209 times at 20,000 (divergence
040). They read the set form, which the planner inlines;
`test_the_set_form_is_inlined_into_both_queries` holds that by EXPLAIN, with a plant the
planner cannot inline as its control.

THE FOLLOW TEST is the behavioural proof that each surface CALLS the function rather than a
copy of it: under a planted function that admits everyone, and one that admits no one, every
surface (check_permission, memory.list's enumeration, the wait's visibility, the plain wait's
rows and the addressed scan's page) answers what the plant says. A surface on its own copy
would keep answering the old way.

ONLY IN A TEST DATABASE. The plants rewrite the live function (the follow test's first lets
every peer read every claimed namespace) and are restored in a `finally`, which a killed
process never reaches. The owner connection, which every write here goes through, and each
plant refuse any database but a session's `meshy_test_*`, so this module does not run with
MESHY_NO_TEST_DB=1 against the shared database (040 slice 3's review, F3).

RED on the unchanged tree: the function does not exist, and the three sites read the grant
table themselves (the static census).

Run:  pytest tests/test_namespace_permission_predicate.py -q -p no:cacheprovider
"""
from __future__ import annotations

import ast
import contextlib
import dataclasses
import datetime as _dt
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
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
os.environ.setdefault("MESHY_ALLOWED_HOSTS", "testserver,127.0.0.1,localhost")

import django  # noqa: E402
from django.conf import settings as dj_settings  # noqa: E402

if not dj_settings.configured:  # pragma: no cover
    django.setup()

FUNCTION = "meshychat_namespace_permission"
SIGNATURE = "meshychat_namespace_permission(text, uuid)"        # one namespace's answer
SET_SIGNATURE = "meshychat_namespace_permission(uuid)"          # the predicate: every namespace
MIGRATION = ROOT / "sql_meshychat" / "015_namespace_permission_predicate.sql"
NEEDS = ("read", "write", "admin")
UNIQUE_INDEX = "shared_memory_grant_active_unique"

NOW = _dt.datetime.now(_dt.timezone.utc)
#: Generous margins: the database's clock is the one that compares, and it is another host's.
PAST = NOW - _dt.timedelta(days=1)
LATER = NOW + _dt.timedelta(days=1)


# ======================================================================= the table

@dataclasses.dataclass(frozen=True)
class Grant:
    """One grant row for the asker, in the order the table lists them: the later one is
    NEWER (created_at one minute apart). `state`: live, revoked, expired or future."""
    permission: str
    state: str = "live"
    to: str = "asker"            # "asker", the "owner" itself, "other" (a third peer), or
    #                              "asker's sibling" (another peer of the asker's account)


@dataclasses.dataclass(frozen=True)
class Row:
    edge: str                    # owner | newest | expired | unclaimed
    name: str
    grants: tuple = ()
    asker: str = "grantee"       # "grantee" (a fresh peer), "owner" (the claimant), or
    #                              "claimant's sibling" (a fresh peer of the claimant's account)
    claimed: bool = True
    relaxed: bool = False        # two LIVE rows: only with the unique index dropped
    answer: str | None = None    # what the function returns
    read: str = ""               # check_permission's outcome for each need, spelled below
    write: str = ""
    admin: str = ""
    visible: bool = False        # memory.list's enumeration and the wait's `vis`
    owns_another: bool = False   # the asker also owns a second namespace


#: Outcome spellings: "owner" (admin via owner), "grant:P" (P via grant), "none" (denied: no
#: active grant), "low:P" (denied: holds P, needs more), "unclaimed" (E_NAMESPACE_UNCLAIMED).
TABLE = (
    Row("owner", "the owner, no grant row", asker="owner",
        answer="owner", read="owner", write="owner", admin="owner", visible=True),
    Row("owner", "the owner also holding a live read row of its own", asker="owner",
        grants=(Grant("read", to="owner"),),
        answer="owner", read="owner", write="owner", admin="owner", visible=True),
    Row("owner", "a live admin grantee is not the owner", grants=(Grant("admin"),),
        answer="admin", read="grant:admin", write="grant:admin", admin="grant:admin",
        visible=True),
    Row("owner", "a sibling of the claimant (another peer of its account) is not the owner",
        asker="claimant's sibling",
        answer=None, read="none", write="none", admin="none"),
    Row("owner", "a sibling of the claimant holding a live read row reads by its grant",
        asker="claimant's sibling", grants=(Grant("read"),),
        answer="read", read="grant:read", write="low:read", admin="low:read", visible=True),

    Row("newest", "one live read row", grants=(Grant("read"),),
        answer="read", read="grant:read", write="low:read", admin="low:read", visible=True),
    Row("newest", "one live read row, and the asker owns another namespace",
        grants=(Grant("read"),), owns_another=True,
        answer="read", read="grant:read", write="low:read", admin="low:read", visible=True),
    Row("newest", "one live write row", grants=(Grant("write"),),
        answer="write", read="grant:write", write="grant:write", admin="low:write",
        visible=True),
    Row("newest", "an older live read row, then a newer revoked admin row",
        grants=(Grant("read"), Grant("admin", "revoked")),
        answer="read", read="grant:read", write="low:read", admin="low:read", visible=True),
    Row("newest", "an older revoked admin row, then a newer live read row",
        grants=(Grant("admin", "revoked"), Grant("read")),
        answer="read", read="grant:read", write="low:read", admin="low:read", visible=True),
    Row("newest", "a revoked write row alone", grants=(Grant("write", "revoked"),),
        answer=None, read="none", write="none", admin="none"),
    Row("newest", "a stranger where another peer holds admin",
        grants=(Grant("admin", to="other"),),
        answer=None, read="none", write="none", admin="none"),
    Row("newest", "a peer whose sibling (another peer of its account) holds live admin",
        grants=(Grant("admin", to="asker's sibling"),),
        answer=None, read="none", write="none", admin="none"),
    Row("newest", "an older live read row, then a newer live admin row held by the sibling",
        grants=(Grant("read"), Grant("admin", to="asker's sibling")),
        answer="read", read="grant:read", write="low:read", admin="low:read", visible=True),
    Row("newest", "an older live admin row, then a newer live read row", relaxed=True,
        grants=(Grant("admin"), Grant("read")),
        answer="read", read="grant:read", write="low:read", admin="low:read", visible=True),
    Row("newest", "an older live read row, then a newer live admin row", relaxed=True,
        grants=(Grant("read"), Grant("admin")),
        answer="admin", read="grant:admin", write="grant:admin", admin="grant:admin",
        visible=True),

    Row("expired", "an expired admin row alone", grants=(Grant("admin", "expired"),),
        answer=None, read="none", write="none", admin="none"),
    Row("expired", "a write row that expires tomorrow", grants=(Grant("write", "future"),),
        answer="write", read="grant:write", write="grant:write", admin="low:write",
        visible=True),
    Row("expired", "an older live read row, then a newer expired admin row", relaxed=True,
        grants=(Grant("read"), Grant("admin", "expired")),
        answer="read", read="grant:read", write="low:read", admin="low:read", visible=True),

    Row("unclaimed", "an unclaimed namespace", claimed=False,
        answer="unclaimed", read="unclaimed", write="unclaimed", admin="unclaimed"),
    Row("unclaimed", "an unclaimed namespace, asked by the owner of another one",
        claimed=False, asker="owner",
        answer="unclaimed", read="unclaimed", write="unclaimed", admin="unclaimed"),
)


def test_the_table_covers_four_edges_three_needs_and_both_answers():
    """The instrument's own control: every edge has rows, every need is spelled on every row,
    and each edge has a row that admits and one that refuses (unclaimed only refuses)."""
    assert {r.edge for r in TABLE} == {"owner", "newest", "expired", "unclaimed"}
    for r in TABLE:
        for need in NEEDS:
            assert getattr(r, need), (r.name, need)
    for edge in ("owner", "newest", "expired"):
        outcomes = {getattr(r, n) for r in TABLE if r.edge == edge for n in NEEDS}
        assert any(o.startswith(("owner", "grant:")) for o in outcomes), edge
        assert any(o.startswith(("none", "low:")) for o in outcomes) or edge == "owner", edge
    assert any(r.relaxed for r in TABLE if r.edge == "newest")
    assert any(not r.relaxed for r in TABLE if r.edge == "newest")
    # two peers on one account: a refusal each way, and a sibling that holds a grant itself
    assert any(r.asker == "claimant's sibling" and r.answer is None for r in TABLE)
    assert any(r.asker == "claimant's sibling" and r.answer is not None for r in TABLE)
    assert any(g.to == "asker's sibling" for r in TABLE if r.answer is None for g in r.grants)


# ======================================================================= fixtures

@pytest.fixture()
def owner():
    with _owner_connection(DSN) as c:
        yield c


@pytest.fixture()
def app():
    from apps.memoryskills import db
    conn = db.connect(DSN)
    try:
        yield conn
    finally:
        conn.close()


# ======================================================================= only in a test database

class NotATestDatabase(RuntimeError):
    """This module's owner connection, or a plant, was pointed at a database that is not a
    session's `meshy_test_*` database."""


TEST_DATABASE_PREFIX = "meshy_test_"


def _refuse_unless_test_database(conn) -> str:
    """The database `conn` is in, if it is a session's `meshy_test_*` (tests/conftest.py
    builds one per run and drops it); otherwise NotATestDatabase. Asked of the server, not
    read from the DSN: the database is what a statement lands in."""
    with conn.cursor() as cur:
        cur.execute("SELECT current_database()")
        name = cur.fetchone()[0]
    if not name.startswith(TEST_DATABASE_PREFIX):
        raise NotATestDatabase(
            f"this module plants wrong versions of meshychat_namespace_permission and runs only in"
            f" a session's {TEST_DATABASE_PREFIX}* database; this connection is in {name!r}")
    return name


def _owner_connection(dsn: str):
    """The owner (autocommit) connection every write here goes through, refused and closed
    on a database that is not a test database."""
    conn = psycopg.connect(dsn, autocommit=True)
    try:
        _refuse_unless_test_database(conn)
    except BaseException:
        conn.close()
        raise
    return conn


class _Named:
    """A stand-in connection whose database is `name`: it answers `current_database()` and
    records every statement, so a refusal is shown without connecting anywhere else."""

    def __init__(self, name: str):
        self.name, self.sql, self.closed = name, [], False

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.sql.append(sql)

    def fetchone(self):
        return (self.name,)

    def close(self):
        self.closed = True


def test_the_owner_fixture_refuses_a_database_that_is_not_a_test_database(monkeypatch,
                                                                           request):
    """040 slice 3's review, F3: this module rewrites the live authorisation function (the
    plants; the follow test's first is "every peer reads every claimed namespace") and drops
    the grant table's unique index in a rolled-back transaction. A plant is restored in a
    `finally`, which a killed process never reaches. So the owner connection, which every
    write here goes through, is refused on any database but a session's `meshy_test_*`, the
    check `tests/test_which_db.py` makes; the shared database under MESHY_NO_TEST_DB=1 is
    refused too. The refused connection is closed."""
    stand_in = _Named("meshy_django")
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: stand_in)
    with pytest.raises(NotATestDatabase, match="meshy_django"):
        request.getfixturevalue("owner")
    assert stand_in.closed


def test_a_plant_refuses_before_it_reads_a_database_that_is_not_a_test_database():
    """The plants take any connection, so `planted` checks too, before its first read: the
    only statement the stand-in sees is the check."""
    stand_in = _Named("meshy_django")
    with pytest.raises(NotATestDatabase, match="meshy_django"):
        with planted(stand_in, "x", "y"):
            pass
    with pytest.raises(NotATestDatabase, match="meshy_django"):
        with planted_body(stand_in, EVERYONE_READS):
            pass
    assert stand_in.sql == ["SELECT current_database()"] * 2


def test_only_a_session_test_database_passes_the_check():
    """The names: a session's database passes; the shared one, the node-test server's, the
    maintenance database, and near-misses of the prefix do not. Control: a real connection
    to this session's database passes, so the check is not refusing everything."""
    with psycopg.connect(DSN) as conn:
        assert _refuse_unless_test_database(conn).startswith("meshy_test_")
    assert _refuse_unless_test_database(_Named("meshy_test_4242_abcdef")) == (
        "meshy_test_4242_abcdef")
    for name in ("meshy_django", "meshy_nodetest", "postgres", "meshy_testx",
                 "not_meshy_test_1", "meshy_tes_1"):
        with pytest.raises(NotATestDatabase, match=name):
            _refuse_unless_test_database(_Named(name))


def _peer(conn, tag: str, account=None) -> dict:
    """A peer on its own new owner account, or on `account` (an existing one's owner_id)."""
    owner_id, peer_id = account or uuid.uuid4(), uuid.uuid4()
    with conn.cursor() as cur:
        if account is None:
            cur.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id)"
                        " VALUES (%s,%s,'dev',%s)",
                        (owner_id, f"t3-{tag}-{owner_id.hex[:8]}@example.test", uuid.uuid4()))
        cur.execute("INSERT INTO peers (id, owner_id, name, display_name) VALUES (%s,%s,%s,%s)",
                    (peer_id, owner_id, f"t3-{tag}-{peer_id.hex[:8]}", f"t3 {tag}"))
    return {"peer_id": peer_id, "owner_id": owner_id, "label": f"t3-{tag}-{peer_id.hex[:8]}"}


def _claim(conn, p: dict, ns: str | None = None) -> str:
    ns = ns or f"t3test/{uuid.uuid4().hex[:12]}"
    with conn.cursor() as cur:
        cur.execute("INSERT INTO shared_memory_namespace (namespace, owner_peer_id,"
                    " owner_owner_id, claimant_plane, ephemeral, retention_policy)"
                    " VALUES (%s,%s,%s,'dev',FALSE,'keep_all')",
                    (ns, p["peer_id"], p["owner_id"]))
    return ns


def _grant(conn, ns: str, grantee: dict, granter: dict, permission: str, state: str,
           created_at: _dt.datetime) -> None:
    revoked = state == "revoked"
    expires = {"expired": PAST, "future": LATER}.get(state)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO shared_memory_grant (namespace, grantee_peer_id,"
                    " grantee_owner_id, permission, granted_by_peer_id, granted_by_owner_id,"
                    " revoked, revoked_at, expires_at, created_at)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (ns, grantee["peer_id"], grantee["owner_id"], permission,
                     granter["peer_id"], granter["owner_id"], revoked,
                     NOW if revoked else None, expires, created_at))


@dataclasses.dataclass
class Built:
    row: Row
    ns: str
    asker: dict


def _build(owner, row: Row) -> tuple[Built, list]:
    """The row's namespace, claimant and asker; returns the grant writes still to do (a
    relaxed row's go inside the transaction that drops the index)."""
    claimant = _peer(owner, "claimant")
    if row.claimed:
        ns = _claim(owner, claimant)
    else:
        ns = f"t3unclaimed/{uuid.uuid4().hex[:12]}"
        _claim(owner, claimant)                # the asker owns something, just not this
    if row.asker == "owner":
        asker = claimant
    elif row.asker == "claimant's sibling":
        asker = _peer(owner, "sibling", account=claimant["owner_id"])
    else:
        assert row.asker == "grantee", row.asker
        asker = _peer(owner, "asker")
    if row.owns_another:
        _claim(owner, asker)
    other = _peer(owner, "other")
    sibling = _peer(owner, "asker-sibling", account=asker["owner_id"])
    base = NOW - _dt.timedelta(hours=1)
    writes = []
    for i, g in enumerate(row.grants):
        to = {"asker": asker, "owner": claimant, "other": other,
              "asker's sibling": sibling}[g.to]
        writes.append((ns, to, claimant, g.permission, g.state,
                       base + _dt.timedelta(minutes=i)))
    return Built(row, ns, asker), writes


def _expected(b: Built, need: str):
    spec = getattr(b.row, need)
    peer = str(b.asker["peer_id"])
    if spec == "owner":
        return ("allowed", {"namespace": b.ns, "peer_id": peer, "permission": "admin",
                            "via": "owner"})
    if spec.startswith("grant:"):
        return ("allowed", {"namespace": b.ns, "peer_id": peer, "permission": spec[6:],
                            "via": "grant"})
    if spec == "none":
        return ("refused", "E_PERMISSION_DENIED",
                f"peer {peer} has no active grant on namespace {b.ns!r}")
    if spec.startswith("low:"):
        return ("refused", "E_PERMISSION_DENIED",
                f"peer {peer} has {spec[4:]!r} on {b.ns!r}, needs {need!r}")
    assert spec == "unclaimed", spec
    return ("refused", "E_NAMESPACE_UNCLAIMED", f"namespace {b.ns!r} is not claimed")


def _probe(conn, fn):
    """`fn()`, inside a savepoint when `conn` is in a transaction, so a wrong function that
    makes the database raise is recorded as an answer (and the transaction goes on) rather
    than ending the evaluation."""
    try:
        if conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE:
            return fn()
        with conn.transaction():
            return fn()
    except psycopg.Error as exc:
        return ("database error", type(exc).__name__)


def _asked(conn, ns, peer_id, need):
    from apps.memoryskills.errors import HandlerError
    from apps.memoryskills.permissions import NAMESPACE_WIDE, check_permission

    def ask():
        try:
            got = check_permission(ns, peer_id, need, conn, resource=NAMESPACE_WIDE)
        except HandlerError as exc:
            return ("refused", exc.code, exc.message)
        return ("allowed", got)
    return _probe(conn, ask)


def _answer(conn, ns, peer_id):
    def ask():
        with conn.cursor() as cur:
            cur.execute(f"SELECT {FUNCTION}(%s, %s)", (ns, str(peer_id)))
            return cur.fetchone()[0]
    return _probe(conn, ask)


def _waits(conn, peer_id) -> set:
    from apps.mesh import wait
    got = _probe(conn, lambda: set(wait.visible_namespaces(conn, peer_id)))
    return got if isinstance(got, set) else {got}


def _listed(app, peer_id) -> dict:
    from apps.memoryskills import handlers
    out = _probe(app, lambda: handlers._list_visible_namespaces(peer_id, app))
    if isinstance(out, tuple):
        return {out: None}
    return {n["namespace"]: n["is_owner"] for n in out["namespaces"]}


def _judge(b: Built, surface: str, need: str, want, got, out: list) -> None:
    if want != got:
        out.append(f"{b.row.edge} | {b.row.name} | {surface} {need}: want {want!r}, "
                   f"got {got!r}")


def _ask_on(conn, b: Built, out: list) -> None:
    """The function, check_permission for each need, and the wait's `vis`, on `conn`, which
    is already inside a server_internal transaction."""
    peer = b.asker["peer_id"]
    _judge(b, "function", "", b.row.answer, _answer(conn, b.ns, peer), out)
    for need in NEEDS:
        _judge(b, "check_permission", need, _expected(b, need),
               _asked(conn, b.ns, peer, need), out)
    _judge(b, "wait.visible_namespaces", "read", b.row.visible,
           b.ns in _waits(conn, peer), out)


def evaluate(owner, app, rows=TABLE) -> list[str]:
    """Every mismatch between the table and the database, as readable lines. Empty means the
    function and all three surfaces agree with the table."""
    from apps.core import rls

    out: list[str] = []
    plain, relaxed = [], []
    for row in rows:
        b, writes = _build(owner, row)
        (relaxed if row.relaxed else plain).append((b, writes))
    for _b, writes in plain:
        for w in writes:
            _grant(owner, *w)
    for b, _w in plain:
        with rls.server_internal(app):
            _ask_on(app, b, out)
        listed = _listed(app, b.asker["peer_id"])
        _judge(b, "memory.list", "read", b.row.visible, b.ns in listed, out)
        if b.row.visible and b.ns in listed:
            _judge(b, "memory.list is_owner", "", b.row.asker == "owner" and b.row.claimed,
                   listed[b.ns], out)
    if relaxed:
        with owner.transaction() as tx:
            owner.execute(f"DROP INDEX {UNIQUE_INDEX}")
            owner.execute("SELECT set_config('app.server_internal', '1', true)")
            for _b, writes in relaxed:
                for w in writes:
                    _grant(owner, *w)
            for b, _w in relaxed:
                _ask_on(owner, b, out)
            raise psycopg.Rollback(tx)
    return out


# ======================================================================= the table, green

def test_the_four_edge_table_on_every_surface(owner, app):
    """C13: the four edges, three needs, and the three surfaces agree with the table, the
    function itself too. The control that it can fail is the next test."""
    assert evaluate(owner, app) == []
    assert _index_definition(owner) is not None, "the relaxed rows' rollback kept the index"


# ======================================================================= plants

def _definition(conn, signature: str = SIGNATURE) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_get_functiondef(%s::regprocedure)", (signature,))
        return cur.fetchone()[0]


def _acl(conn, signature: str = SIGNATURE) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT proacl::text FROM pg_proc WHERE oid = %s::regprocedure",
                    (signature,))
        return cur.fetchone()[0]


def _index_definition(conn) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_get_indexdef(to_regclass(%s))", (UNIQUE_INDEX,))
        return cur.fetchone()[0]


@contextlib.contextmanager
def planted(owner, needle: str, replacement: str, signature: str = SIGNATURE):
    """The real function with ONE change, under the real name, restored byte for byte. Only
    in a test database: a process killed inside never reaches the restore."""
    _refuse_unless_test_database(owner)
    real, acl = _definition(owner, signature), _acl(owner, signature)
    assert real.count(needle) == 1, f"the plant's needle is not in the function once: {needle!r}"
    owner.execute(real.replace(needle, replacement))
    try:
        assert _definition(owner, signature) != real, "the plant did not take"
        yield
    finally:
        owner.execute(real)
        assert (_definition(owner, signature) == real
                and _acl(owner, signature) == acl), "the restore is not exact"


@contextlib.contextmanager
def planted_body(owner, body: str, signature: str = SET_SIGNATURE):
    """The real function with its whole body replaced, restored byte for byte."""
    _refuse_unless_test_database(owner)
    real = _definition(owner, signature)
    head, old, tail = real.split("$function$")
    with planted(owner, "$function$" + old + "$function$",
                 "$function$" + body + "$function$", signature):
        yield


OWNER_BRANCH = ("    SELECT n.namespace, 'owner'::text\n"
                "      FROM shared_memory_namespace n\n"
                "     WHERE n.owner_peer_id = meshychat_namespace_permission.peer\n"
                "    UNION ALL\n")

#: name -> (the form it changes, needle, replacement, the edge the table must find it on)
PLANTS = {
    "ASC order": (SET_SIGNATURE, "g.namespace, g.created_at DESC",
                  "g.namespace, g.created_at ASC", "newest"),
    "expires_at dropped": (SET_SIGNATURE,
                           "\n        AND (g.expires_at IS NULL OR g.expires_at > NOW())", "",
                           "expired"),
    "expiry compared the wrong way": (SET_SIGNATURE, "g.expires_at > NOW()",
                                      "g.expires_at < NOW()", "expired"),
    "revoked dropped": (SET_SIGNATURE, "\n        AND g.revoked = FALSE", "", "newest"),
    "owner branch removed": (SET_SIGNATURE, OWNER_BRANCH, "", "owner"),
    "owner precedence dropped (the grant branch reads owned namespaces)": (
        SET_SIGNATURE, "\n        AND n.owner_peer_id <> meshychat_namespace_permission.peer", "",
        "owner"),
    "the owner answered as an admin grant": (SET_SIGNATURE, "'owner'::text", "'admin'::text",
                                             "owner"),
    "unclaimed collapsed into denied": (SIGNATURE, "RETURN 'unclaimed';", "RETURN NULL;",
                                        "unclaimed"),
    "any peer's grant row": (SET_SIGNATURE,
                             "WHERE g.grantee_peer_id = meshychat_namespace_permission.peer\n"
                             "        AND n.owner_peer_id", "WHERE n.owner_peer_id", "newest"),
    "one namespace's answer read from any of the peer's": (
        SIGNATURE, "\n             WHERE a.namespace = meshychat_namespace_permission.namespace",
        "\n             LIMIT 1", "newest"),
    # Keyed on the ACCOUNT, not the peer: shared_memory_namespace.owner_owner_id and
    # shared_memory_grant.grantee_owner_id sit on the same rows, and the dashboard's
    # tenant_scope keys on them by design (an owner session). Here every other peer of the
    # same account would own the namespace, or hold its sibling's grant (040 slice 3's
    # review, F1).
    "ownership keyed on the owner account": (
        SET_SIGNATURE, "     WHERE n.owner_peer_id = meshychat_namespace_permission.peer\n"
                       "    UNION ALL",
        "     WHERE n.owner_owner_id = (SELECT p.owner_id FROM peers p"
        " WHERE p.id = meshychat_namespace_permission.peer)\n    UNION ALL", "owner"),
    "a grant keyed on the grantee's owner account": (
        SET_SIGNATURE, "WHERE g.grantee_peer_id = meshychat_namespace_permission.peer",
        "WHERE g.grantee_owner_id = (SELECT p.owner_id FROM peers p"
        " WHERE p.id = meshychat_namespace_permission.peer)", "newest"),
}


@pytest.mark.parametrize("plant", sorted(PLANTS))
def test_the_table_finds_each_wrong_function(owner, app, plant):
    """The table's RED, kept: with one clause of the function wrong, the table reports it,
    on the edge that clause is for. The real function passes the same table (the test above),
    so the difference is the plant."""
    signature, needle, replacement, edge = PLANTS[plant]
    with planted(owner, needle, replacement, signature):
        wrong = evaluate(owner, app)
    assert wrong, f"the table passed a wrong function: {plant}"
    assert any(line.startswith(edge + " |") for line in wrong), (plant, wrong)
    assert evaluate(owner, app) == [], "control: the restored function passes again"


def test_two_live_rows_are_refused_by_the_schema(owner):
    """Why the newest-row rows are `relaxed`: the unique index refuses a second unrevoked row
    for one (namespace, grantee), so ORDER BY decides nothing on this schema. Control: a
    second row for ANOTHER grantee, and a revoked one for the same, are accepted."""
    claimant, asker, other = (_peer(owner, t) for t in ("idx-owner", "idx-asker", "idx-other"))
    ns = _claim(owner, claimant)
    _grant(owner, ns, asker, claimant, "read", "live", NOW - _dt.timedelta(minutes=2))
    _grant(owner, ns, other, claimant, "admin", "live", NOW - _dt.timedelta(minutes=1))
    _grant(owner, ns, asker, claimant, "admin", "revoked", NOW)
    with pytest.raises(psycopg.errors.UniqueViolation, match=UNIQUE_INDEX):
        _grant(owner, ns, asker, claimant, "admin", "live", NOW)
    with pytest.raises(psycopg.errors.UniqueViolation, match=UNIQUE_INDEX):
        _grant(owner, ns, asker, claimant, "admin", "expired", NOW)
    assert "WHERE (revoked = false)" in _index_definition(owner)


# ======================================================================= the function

def test_the_function_runs_as_its_caller_under_the_callers_rls(owner, app):
    """SECURITY INVOKER: as meshychat_app outside `server_internal`, the sql/028 policies hide the
    claim row, so the function answers 'unclaimed', and check_permission refuses with
    E_NAMESPACE_UNCLAIMED, which is what its own SELECT saw before this slice. Inside, the
    owner is the owner (control). A SECURITY DEFINER function would read as its owner, a
    superuser that bypasses RLS, and answer 'owner' outside: the plant shows the probe
    tells the two apart."""
    from apps.core import rls
    claimant = _peer(owner, "invoker")
    ns = _claim(owner, claimant)
    peer = claimant["peer_id"]

    def rows_of_set_form(conn):
        with conn.cursor() as cur:
            cur.execute(f"SELECT namespace, permission FROM {FUNCTION}(%s)", (str(peer),))
            return cur.fetchall()

    assert _answer(app, ns, peer) == "unclaimed"
    assert rows_of_set_form(app) == []
    assert _asked(app, ns, peer, "read")[:2] == ("refused", "E_NAMESPACE_UNCLAIMED")
    with rls.server_internal(app):
        assert _answer(app, ns, peer) == "owner"
        assert rows_of_set_form(app) == [(ns, "owner")]
    with planted(owner, " LANGUAGE plpgsql\n", " LANGUAGE plpgsql\n SECURITY DEFINER\n"):
        assert _answer(app, ns, peer) == "owner", "control: a definer reads across RLS"
    with planted(owner, " LANGUAGE sql\n", " LANGUAGE sql\n SECURITY DEFINER\n",
                 SET_SIGNATURE):
        assert rows_of_set_form(app) == [(ns, "owner")], "control: the set form's definer"
    assert _answer(app, ns, peer) == "unclaimed"
    assert rows_of_set_form(app) == []


def _attributes(conn, signature: str) -> tuple:
    with conn.cursor() as cur:
        cur.execute("SELECT l.lanname, p.provolatile, p.proparallel, p.prosecdef, "
                    "       p.proisstrict, p.proconfig, p.proretset, "
                    "       pg_get_function_result(p.oid), "
                    "       has_function_privilege('meshychat_app', p.oid, 'EXECUTE'), "
                    "       EXISTS (SELECT 1 FROM aclexplode(p.proacl) a "
                    "               WHERE a.grantee = 0 AND a.privilege_type = 'EXECUTE'), "
                    "       p.proacl IS NULL "
                    "FROM pg_proc p JOIN pg_language l ON l.oid = p.prolang "
                    "WHERE p.oid = %s::regprocedure", (signature,))
        return cur.fetchone()


def test_the_functions_stated_attributes(owner):
    """What 040 says about the two forms: STABLE, PARALLEL SAFE, SECURITY INVOKER, not
    STRICT, no SET clause; the set form LANGUAGE sql (what lets the planner inline it), the
    one-namespace form plpgsql (its plans are kept for the session); EXECUTE for meshychat_app and
    not for PUBLIC. Not STRICT is also behaviour: a NULL namespace finds no claim and is
    'unclaimed', as `namespace = NULL` found none. And there are exactly these two forms."""
    for signature, lang, retset, result in (
            (SIGNATURE, "plpgsql", False, "text"),
            (SET_SIGNATURE, "sql", True, "TABLE(namespace text, permission text)")):
        row = _attributes(owner, signature)
        (got_lang, volatile, parallel, definer, strict, config, got_retset, got_result,
         app_may, public_may, dflt) = row
        assert (got_lang, volatile, parallel, definer, strict, config, got_retset,
                got_result) == (lang, "s", "s", False, False, None, retset, result), signature
        assert app_may is True, signature
        assert dflt is False and public_may is False, (
            f"PUBLIC holds EXECUTE on {signature} (the CREATE default)")
    with owner.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_proc WHERE proname = %s", (FUNCTION,))
        assert cur.fetchone()[0] == 2
    assert _answer(owner, None, uuid.uuid4()) == "unclaimed"


def _plan(conn, sql: str, params) -> str:
    from apps.core import rls
    with rls.server_internal(conn):
        with conn.cursor() as cur:
            cur.execute("EXPLAIN " + sql, params)
            return "\n".join(r[0] for r in cur.fetchall())


def test_the_set_form_is_inlined_into_both_queries(owner, app):
    """The cost, held structurally: in the plans of the wait's `vis` and of memory.list's
    enumeration the function's name does not appear, because the planner inlined the set
    form into each query as a subquery that reads the tables itself, as the copies did. The
    first build called a one-namespace function
    per claimed namespace, which the plan shows as a Filter naming it on every row. Control:
    with the set form given a SET clause, which the planner cannot inline, both plans name
    it (a Function Scan), so the probe can see a call that is not inlined."""
    from apps.memoryskills import handlers
    from apps.mesh import wait
    peer = _peer(owner, "plan")
    queries = {
        "wait vis": (wait._PEER_VIS_CTE + "SELECT namespace FROM vis",
                     wait._vis_params(peer["peer_id"])),
        "memory.list": (handlers._VISIBLE_NAMESPACES_SQL,
                        handlers._visible_namespaces_params(peer["peer_id"])),
    }
    for label, (sql, params) in queries.items():
        plan = _plan(app, sql, params)
        assert FUNCTION not in plan, (label, plan)
        assert "shared_memory_grant" in plan, (label, plan)
    with planted(owner, " LANGUAGE sql\n", " LANGUAGE sql\n SET search_path TO 'public'\n",
                 SET_SIGNATURE):
        for label, (sql, params) in queries.items():
            plan = _plan(app, sql, params)
            assert f"Function Scan on {FUNCTION}" in plan, ("control", label, plan)
            assert "shared_memory_grant" not in plan, ("control", label, plan)


def test_the_migration_is_lf_and_numbered_015():
    """.gitattributes pins sql_meshychat/*.sql to LF and discover() refuses CRLF."""
    from apps.core import sqlmigrate
    raw = MIGRATION.read_bytes()
    assert b"\r\n" not in raw and raw.endswith(b"\n")
    plan = {v: p.name for v, _d, p, _c in sqlmigrate.discover(MIGRATION.parent)}
    assert plan[15] == MIGRATION.name


# ======================================================================= the follow test

def _mesh_reader_setup(owner):
    from tests.fixtures import meshsem_db as db
    db.ensure_namespace(owner)
    author = db.peer(owner, "t3-author")
    reader = db.peer(owner, "t3-meshreader")
    db.grant(owner, reader, "read")
    mesh_row = db.row(owner, author, {"from": author["label"], "to": "t3-nobody",
                                      "body": "t3 follow"})
    with owner.cursor() as cur:
        cur.execute("SELECT created_at FROM shared_memory WHERE id = %s", (mesh_row["id"],))
        mesh_row["created_at"] = cur.fetchone()[0]
    return reader, mesh_row


def _private_row(owner, author: dict, ns: str) -> dict:
    from tests.fixtures import signed_rows as sr
    rid = uuid.uuid4()
    key = f"msg/{int(NOW.timestamp() * 1000)}-{rid.hex}"
    # With the bytes a writer would have signed (divergence 051: A2 refuses a row without).
    sr.insert(owner, sr.row(author["peer_id"], author["owner_id"], ns, key,
                            {"body": "t3 private"}, rid=rid, call_id=f"call-{rid}",
                            author_label=author["label"]))
    created = owner.execute("SELECT created_at FROM shared_memory WHERE id = %s",
                            (rid,)).fetchone()[0]
    return {"key": key, "id": rid, "created_at": created}


def _surfaces(app, ns: str, peer_id, private_row: dict, mesh_row: dict) -> dict:
    """What each surface answers for `peer_id` about `ns` (and, for the two wait queries
    that return rows, about one row in `ns` and one in shared/mesh-chat)."""
    from apps.core import rls
    from apps.memoryskills.validation import MESH_CHAT_NAMESPACE
    from apps.mesh import wait

    def just_before(r):
        return wait.Cursor(wait.micros_of(r["created_at"]) - 1, uuid.UUID(int=0))

    with rls.server_internal(app):
        vis = set(wait.visible_namespaces(app, peer_id))
        plain = {r["key"] for r in wait.matching_rows(app, peer_id, just_before(private_row))}
        page = {r[0] for r in wait._addressed_page(app, peer_id, just_before(mesh_row))}
        read = _asked(app, ns, peer_id, "read")[0]
    return {
        "check_permission read": read == "allowed",
        "memory.list": ns in _listed(app, peer_id),
        "wait vis (namespace)": ns in vis,
        "wait vis (mesh-chat)": MESH_CHAT_NAMESPACE in vis,
        "plain wait row": private_row["key"] in plain,
        "addressed page row": mesh_row["key"] in page,
    }


#: Two bodies for the set form, the predicate: every peer reads every claimed namespace,
#: and no peer holds anything anywhere.
EVERYONE_READS = "\n    SELECT n.namespace, 'read'::text FROM shared_memory_namespace n\n"
NO_ONE_READS = ("\n    SELECT n.namespace, 'read'::text FROM shared_memory_namespace n"
                " WHERE FALSE\n")


def test_every_surface_follows_the_function(owner, app):
    """The behavioural proof that no surface kept its own copy: plant a predicate that lets
    every peer read every claimed namespace, then one that lets no one read anything, and
    each surface's answer moves with it; check_permission through the one-namespace form,
    which reads the planted set form. Controls: under the real function the stranger sees
    nothing and the readers see everything, which is the opposite of the first plant for the
    stranger and of the second for the readers."""
    claimant = _peer(owner, "follow-owner")
    ns = _claim(owner, claimant)
    stranger = _peer(owner, "follow-stranger")
    private_row = _private_row(owner, claimant, ns)
    mesh_reader, mesh_row = _mesh_reader_setup(owner)
    grantee = _peer(owner, "follow-grantee")
    _grant(owner, ns, grantee, claimant, "read", "live", NOW)

    def all_of(peer, private_expected: bool, mesh_expected: bool) -> dict:
        return {"check_permission read": private_expected, "memory.list": private_expected,
                "wait vis (namespace)": private_expected, "plain wait row": private_expected,
                "wait vis (mesh-chat)": mesh_expected, "addressed page row": mesh_expected}

    def seen(p):
        return _surfaces(app, ns, p["peer_id"], private_row, mesh_row)

    # the real function
    assert seen(stranger) == all_of(stranger, False, False)
    assert seen(grantee) == all_of(grantee, True, False)
    assert seen(mesh_reader) == all_of(mesh_reader, False, True)
    # everyone reads: the stranger sees all of it on every surface
    with planted_body(owner, EVERYONE_READS):
        assert seen(stranger) == all_of(stranger, True, True)
    # no one reads: the grantee and the mesh reader lose it on every surface
    with planted_body(owner, NO_ONE_READS):
        assert seen(grantee) == all_of(grantee, False, False)
        assert seen(mesh_reader) == all_of(mesh_reader, False, False)
    # restored
    assert seen(stranger) == all_of(stranger, False, False)
    assert seen(grantee) == all_of(grantee, True, False)


# ======================================================================= re-expressed by none

PRODUCTION_DIRS = ("apps", "meshychat", "tools")
#: A read of the grant table: a FROM or JOIN item, or a later item of a comma list, naming it
#: bare, schema-qualified or quoted, with or without ONLY (040 slice 3's review, F2: the first
#: pattern saw only `FROM|JOIN shared_memory_grant`).
GRANT_READ = re.compile(
    r'(?:\b(?:FROM|JOIN)\s+|,\s*)(?:ONLY\s+)?(?:(?:"[^"]+"|[A-Za-z_]\w*)\s*\.\s*)?'
    r'(?:"shared_memory_grant"|shared_memory_grant\b)', re.I)
CALLS_FUNCTION = re.compile(rf"\b{FUNCTION}\s*\(")

#: The grant table's readers that stay their own, by stated decision (design 3.5): the
#: dashboard's OWNER visibility, keyed on grantee_owner_id and an owner session, not a peer
#: capability surface. file -> the number of string constants that read the table.
OUT_OF_SCOPE_READERS = {
    "apps/dashboard/queries.py": 1,
    "apps/dashboard/tenant_scope.py": 4,
}
#: Every production caller of the function, file -> constants.
CALLERS = {
    "apps/memoryskills/permissions.py": 1,     # check_permission
    "apps/memoryskills/handlers.py": 1,        # _VISIBLE_NAMESPACES_SQL (memory.list)
    "apps/mesh/wait.py": 1,                    # _PEER_VIS_CTE, every wait query
    # Since 040 slice 4: the capability liveness statement's issuer clause (design 2.5 (b)
    # clause 3) asks THE predicate whether a token's issuer still holds admin.
    "apps/memoryskills/capability.py": 1,      # LIVENESS_SQL
    # Since 040 slice 5: the mint asks THE predicate whether its issuer holds admin on the
    # token's one namespace (design 2.2, 3.1; Q2), in the statement that reads its status;
    # the revoke asks it whether the caller is a live admin of the token's namespace (2.5 (a)).
    "apps/memoryskills/capability_issue.py": 2,  # MINT_SQL, REVOKE_SQL
}


def _production_files() -> list[Path]:
    files = [ROOT / "manage.py"]
    for d in PRODUCTION_DIRS:
        files += sorted((ROOT / d).rglob("*.py"))
    return [f for f in files if f.exists() and "__pycache__" not in f.parts]


def _docstrings(tree: ast.Module) -> set[int]:
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                out.add(id(first.value))
    return out


def string_census(source: str, pattern: re.Pattern) -> int:
    """How many string constants in `source` match, docstrings aside (prose, not SQL).
    Adjacent literals are one constant to the parser, so a query written as implicit
    concatenation counts once. Not seen: a match split across a `+` or across an f-string's
    parts; the follow test is the instrument for that."""
    tree = ast.parse(source)
    prose = _docstrings(tree)
    return sum(1 for node in ast.walk(tree)
               if isinstance(node, ast.Constant) and isinstance(node.value, str)
               and id(node) not in prose and pattern.search(node.value))


def _census(pattern: re.Pattern) -> dict[str, int]:
    out = {}
    for f in _production_files():
        n = string_census(f.read_text(encoding="utf-8"), pattern)
        if n:
            out[f.relative_to(ROOT).as_posix()] = n
    return out


OLD_WAIT_CTE = '''
_PEER_VIS_CTE = (
    "WITH peer AS ( "
    "    SELECT id FROM peers WHERE id = %(peer)s AND status = 'active' "
    "), "
    "vis AS ( "
    "    SELECT n.namespace FROM shared_memory_namespace n "
    "    JOIN peer p ON p.id = n.owner_peer_id "
    "    UNION "
    "    SELECT g.namespace FROM shared_memory_grant g "
    "    JOIN peer p ON p.id = g.grantee_peer_id "
    "    WHERE g.revoked = FALSE "
    ") "
)
'''


def test_no_production_sql_reads_the_grant_table_but_the_stated_exceptions():
    """A2's "re-expressed by none", measured: the only production strings that read
    shared_memory_grant are the dashboard's, which design 3.5 leaves out by name. Controls:
    the census finds the wait's old copy in a planted source, counts implicit concatenation
    once, and does not take a write (`UPDATE shared_memory_grant`) for a read. And it sees the
    table however a FROM item can name it (040 slice 3's review, F2): schema-qualified,
    quoted, ONLY, and as a later item of a comma list; a longer name that begins with the
    table's is another table."""
    assert string_census(OLD_WAIT_CTE, GRANT_READ) == 1
    assert string_census('q = "UPDATE shared_memory_grant SET revoked = TRUE"', GRANT_READ) == 0
    assert string_census('q = ("SELECT 1 " "from  shared_memory_grant g")', GRANT_READ) == 1
    for form in ('FROM public.shared_memory_grant g', 'FROM "shared_memory_grant" g',
                 'FROM "public"."shared_memory_grant" g', 'JOIN public . shared_memory_grant g',
                 'FROM ONLY shared_memory_grant g', 'FROM ONLY public.shared_memory_grant g',
                 'FROM peers p, shared_memory_grant g', 'FROM peers p,public.shared_memory_grant'):
        assert string_census(f"q = 'SELECT 1 {form} WHERE TRUE'", GRANT_READ) == 1, form
    for other in ('FROM shared_memory_grant_history g', 'FROM "shared_memory_grant_x" g',
                  'FROM public.shared_memory_grants g'):
        assert string_census(f"q = 'SELECT 1 {other}'", GRANT_READ) == 0, other
    assert _census(GRANT_READ) == OUT_OF_SCOPE_READERS


def test_the_three_sites_call_the_function():
    """Each of the three sites asks the function, once, in production code, and nothing else
    does but the capability liveness statement, which asks it about a token's issuer (040
    slice 4), and the mint's and the revoke's statements, which ask it about the issuer before
    it signs and about the caller before it revokes (040 slice 5). The census's own control is
    the planted old CTE, which does not call it."""
    assert string_census(OLD_WAIT_CTE, CALLS_FUNCTION) == 0
    assert _census(CALLS_FUNCTION) == CALLERS


def test_only_the_migration_reads_the_grant_table_among_the_divergences():
    """Across sql/ and sql_meshychat/, only 015 reads shared_memory_grant. sql/028 creates the
    table and its policies merely name it in an ON clause."""
    readers = {p.name for d in ("sql", "sql_meshychat")
               for p in sorted((ROOT / d).glob("*.sql"))
               if GRANT_READ.search(p.read_text(encoding="utf-8"))}
    assert readers == {MIGRATION.name}
