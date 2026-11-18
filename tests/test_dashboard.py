"""The dashboard read API, proved against the real database and the REAL routing table.

WHAT MAKES A TEST HERE COUNT
----------------------------
1. NO ROOT_URLCONF OVERRIDE. Every request below goes through `meshychat.urls`, which
   already includes `apps.dashboard.urls`. The previous round of this project pinned a
   per-app urlconf in its tests, so every route test passed while the application served
   404. A route test that supplies its own URLconf proves the view and says nothing
   about the wiring. `test_every_route_resolves_through_the_project_urlconf` resolves
   against the project table, and `test_a_route_that_should_not_exist_is_a_404` is its
   positive control - `resolve()` must be capable of FAILING, or "it resolved" proves
   nothing.

2. EVERY NEGATIVE HAS A POSITIVE CONTROL IN THE SAME TEST. The central claim of this
   surface is "a second owner sees NONE of the first owner's rows", and an empty list is
   also what a broken fixture, a mistyped namespace, an unbound RLS context or a 500
   swallowed into `[]` produces. So every isolation test asserts owner A SEES the row
   first and only then asserts owner B does not. Where the control is the interesting
   half it is asserted first, so a failure reads as "the happy path broke" rather than
   "the guard fired".

   `test_the_rls_context_is_what_makes_the_read_return_rows` is the control for the
   controls: it runs the SAME query with and without `app.server_internal`, shows the
   unprivileged read returns zero rows and raises nothing, and shows the repository
   layer refuses to report that as absence.

3. NO DATABASE MOCKS. Every row is written to the migrated Postgres named by
   MESHY_TEST_DSN. The fixture owners are plane='dev' with DISTINCT trust_group_ids,
   because (a) `enforce_ns_dev_only` refuses a namespace claim by a product-plane owner,
   so a product tenant could not have a visibility set at all, and (b) two dev owners in
   the SAME trust group are same-tenant under `meshy_same_tenant_or_devgroup`, which
   would make the isolation tests pass for the wrong reason.

Run: .venv/Scripts/python.exe -m pytest tests/test_dashboard.py -q
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import secrets
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DSN = os.environ.get("MESHY_TEST_DSN",
                    "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")
os.environ.setdefault("MESHY_TEST_DSN", DSN)
os.environ.setdefault("MESHY_DSN", DSN)

ISSUER = "meshy-dashboard-test-issuer"

_signing_key = ed25519.Ed25519PrivateKey.generate()
JWT_PRIVATE_PEM = _signing_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode("ascii")
JWT_PUBLIC_PEM = _signing_key.public_key().public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo,
).decode("ascii")

# --- Django settings ---------------------------------------------------------
# DJANGO_SETTINGS_MODULE is set to the PROJECT settings, not a test-only module, because
# the point of this file is to exercise the project's own routing table. Other modules
# in this suite configure Django at import time and whichever is imported FIRST wins, so
# nothing here depends on winning that race: the settings these tests actually need are
# applied per-test by the autouse `dashboard_settings` fixture via `override_settings`,
# which LAYERS on top of whatever base configuration won.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")

from django.conf import settings as dj_settings  # noqa: E402

if not dj_settings.configured:  # pragma: no cover - only when a sibling module lost
    import django as _django  # noqa: F401

import django  # noqa: E402

django.setup()

import jwt as pyjwt  # noqa: E402
from django.test import Client, override_settings  # noqa: E402
from django.urls import resolve, reverse  # noqa: E402
from django.urls.exceptions import Resolver404  # noqa: E402

from apps.core import dbconn, rls  # noqa: E402
from apps.dashboard import auth as dash_auth  # noqa: E402
from apps.dashboard import errors, queries, repo  # noqa: E402
from apps.dashboard.tenant_scope import TenantScope  # noqa: E402
from tests.fixtures import signed_rows as sr  # noqa: E402


# ----------------------------------------------------------------- settings

@pytest.fixture(autouse=True)
def dashboard_settings():
    """Layered rather than a module-level `configure()`, so these tests behave the same
    alone and in a full-suite run. `ROOT_URLCONF` is deliberately NOT among them."""
    with override_settings(
        ALLOWED_HOSTS=["*"],
        MESHY_JWT_PUBLIC_KEY_PEM=JWT_PUBLIC_PEM,
        MESHY_JWT_ISSUER=ISSUER,
        MESHY_DSN=DSN,
        # Matches a server with SSO not configured; the
        # production posture (SSO on, dev JWT off) is exercised explicitly by
        # test_dev_jwt_is_refused_when_sso_is_configured.
        MESHY_SSO_CONFIGURED=False,
    ):
        yield


@pytest.fixture(autouse=True)
def _no_dev_fleet_mode(monkeypatch):
    """`MESHYCHAT_DEV_FLEET_MODE` swaps in a CTE that shows a dev caller the whole fleet.
    Both fixture owners are plane='dev', so a stray value in the environment would make
    every isolation test below pass or fail for a reason that has nothing to do with the
    code under test. Same for the usage global-mode flag."""
    monkeypatch.delenv("MESHYCHAT_DEV_FLEET_MODE", raising=False)
    monkeypatch.delenv("MESHYCHAT_ALLOW_GLOBAL_USAGE", raising=False)
    monkeypatch.delenv("MESHY_REDIS_URL", raising=False)
    # The dev-JWT switch the grants surface shares with this one (bearer_jwt_allowed):
    # a stray `MESHYCHAT_ALLOW_DEV_JWT=0` would turn every Bearer-JWT test here into a 401.
    monkeypatch.delenv("MESHYCHAT_ALLOW_DEV_JWT", raising=False)


# ------------------------------------------------------------------ fixtures

def _sql_conn():
    """A SUPERUSER connection, used for FIXTURES ONLY.

    `meshy` has rolbypassrls, so it can seed `shared_memory` without entering the
    server-internal context. Nothing under test ever uses it: `apps/core/dbconn.py`
    refuses to serve a request on a bypassing connection, and
    `test_the_app_refuses_to_serve_on_a_bypassing_connection` pins that refusal.
    """
    return psycopg.connect(DSN, autocommit=True)


def _insert_owner(cur, label: str, *, is_admin: bool = False, tier: str = "free",
                  trust_group=None) -> uuid.UUID:
    """A dev-plane owner.

    `dashboard_users_plane_group_ck` makes plane='dev' and a non-NULL trust_group_id
    equivalent (read off pg_constraint), so the group id is required. It is a PARAMETER
    because the trust group IS the tenancy boundary: `meshy_same_tenant_or_devgroup` is
    true for two dev owners that share one, and the grant arm of `vis` is gated on it.
    A fixture that gave every owner a fresh group could never exercise that arm; one
    that gave every owner the same group could never prove isolation. This file needs
    both, so it builds both.
    """
    owner_id = uuid.uuid4()
    cur.execute(
        "INSERT INTO dashboard_users (id, email, display_name, tier, is_admin, plane, "
        "                             trust_group_id) "
        "VALUES (%s, %s, %s, %s, %s, 'dev', %s)",
        (owner_id, f"{label}-{owner_id}@dash.test", label, tier, is_admin,
         trust_group or uuid.uuid4()),
    )
    return owner_id


def _insert_peer(cur, owner_id, name: str, display_name: str | None = None) -> uuid.UUID:
    peer_id = uuid.uuid4()
    cur.execute(
        "INSERT INTO peers (id, owner_id, name, display_name, tier, status, metadata) "
        "VALUES (%s, %s, %s, %s, 'free', 'active', %s)",
        (peer_id, owner_id, name, display_name, json.dumps({"agent_type": "test"})),
    )
    return peer_id


def _claim_namespace(cur, namespace: str, owner_id, peer_id) -> str:
    cur.execute(
        "INSERT INTO shared_memory_namespace (namespace, owner_peer_id, owner_owner_id) "
        "VALUES (%s, %s, %s) ON CONFLICT (namespace) DO NOTHING",
        (namespace, peer_id, owner_id),
    )
    return namespace


def _mem(cur, namespace, key, value, owner_id, peer_id, *, status="OPEN", tags=None):
    """One head row. `provenance_call_id` is a GENERATED column pulled out of
    `provenance_jsonb->'payload'->>'call_id'` and it is both NOT NULL and UNIQUE, so the
    provenance document is required and must be distinct per row - measured from
    information_schema.columns, not guessed.

    Since A2 (divergence 051) the row carries the bytes a writer would have signed, and the
    status and tags are in them: 014's agree-trigger holds the columns to the bytes."""
    row_id = uuid.uuid4()
    signed = {"status": status} if status is not None else {}
    if tags:
        signed["tags"] = list(tags)
    cols = sr.row(peer_id, owner_id, namespace, key, value, rid=row_id,
                  call_id=f"call/{row_id}", extra_args=signed, author_label="fixture")
    cur.execute(f"INSERT INTO shared_memory ({', '.join(cols)}) "
                f"VALUES ({', '.join(['%s'] * len(cols))})", list(cols.values()))
    return row_id


class World:
    """Two complete, disjoint tenants. Attribute access only - a dict would invite a
    `.get()` that defaults, and a defaulted fixture id is how an isolation test starts
    comparing None to None."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


USAGE_NS = "shared/agent-usage"


#: Every fixture owner's display_name matches this. It is the handle the stale-row
#: sweeper below uses, so it has to stay in step with `_insert_owner`'s labels.
FIXTURE_OWNER_RE = "^dash-(a|b|c|admin)-"


def _purge_fixture_owners():
    """Remove any fixture owner left behind by an EARLIER run.

    Not paranoia: this fixture seeds `shared/agent-usage`, whose name is a FIXED global
    string. If a previous run raised inside the setup block, pytest
    never reaches the `yield` and the teardown never runs, so that namespace survives
    owned by a dead run's owner - and the next run's usage assertions then fail for a
    reason that has nothing to do with the code. Deleting in FK order rather than
    relying on cascades: `dashboard_users` cascades to `peers` only, and `shared_memory`
    holds FKs to both.
    """
    with _sql_conn() as conn, conn.cursor() as cur:
        # `created_at` older than the grace window, so a CONCURRENT run of this file
        # is not swept out from under itself. Without the window the sweeper deletes
        # another run's live fixture mid-test, which is a failure mode that looks
        # exactly like a tenancy bug (31 tests failed that way once).
        cur.execute("SELECT id FROM dashboard_users "
                    "WHERE display_name ~ %s AND created_at < NOW() - INTERVAL '10 minutes'",
                    (FIXTURE_OWNER_RE,))
        ids = [row[0] for row in cur.fetchall()]
        if not ids:
            return
        cur.execute("SELECT id FROM peers WHERE owner_id = ANY(%s)", (ids,))
        peer_ids = [row[0] for row in cur.fetchall()]
        cur.execute("DELETE FROM shared_memory WHERE created_by_owner_id = ANY(%s)", (ids,))
        cur.execute("DELETE FROM shared_memory_grant "
                    "WHERE grantee_owner_id = ANY(%s) OR granted_by_owner_id = ANY(%s)",
                    (ids, ids))
        cur.execute("DELETE FROM dashboard_memory_bridges "
                    "WHERE created_by_owner_id = ANY(%s)", (ids,))
        cur.execute("DELETE FROM shared_memory_namespace WHERE owner_owner_id = ANY(%s)", (ids,))
        cur.execute("DELETE FROM dashboard_invoice_items WHERE invoice_id IN "
                    "(SELECT id FROM dashboard_invoices WHERE owner_id = ANY(%s))", (ids,))
        for table in ("dashboard_node_logs", "dashboard_log_settings", "dashboard_invoices",
                      "dashboard_documents", "dashboard_expenses", "dashboard_payouts",
                      "dashboard_clients", "owner_api_keys", "owner_sessions",
                      "dashboard_notification_state", "dashboard_message_state",
                      "accounts_entitlements"):
            cur.execute(f"DELETE FROM {table} WHERE owner_id = ANY(%s)", (ids,))
        cur.execute("DELETE FROM skill_calls WHERE caller_owner_id = ANY(%s)", (ids,))
        if peer_ids:
            cur.execute("DELETE FROM skills WHERE peer_id = ANY(%s)", (peer_ids,))
            cur.execute("DELETE FROM models WHERE peer_id = ANY(%s)", (peer_ids,))
            cur.execute("DELETE FROM mesh_presence WHERE peer_id = ANY(%s)", (peer_ids,))
        cur.execute("DELETE FROM dashboard_users WHERE id = ANY(%s)", (ids,))


@pytest.fixture(scope="module")
def world():
    _purge_fixture_owners()
    marker = uuid.uuid4().hex[:8]
    created_usage_ns = False
    with _sql_conn() as conn, conn.cursor() as cur:
        # THREE owners, TWO tenants. A and C share a trust group, so a grant from C to A
        # survives the `meshy_same_tenant_or_devgroup` gate and the grant arm of `vis` is
        # actually exercised. B is alone in its own group and is the subject of every
        # "a second owner sees NONE of this" assertion below.
        group_ac = uuid.uuid4()
        owner_a = _insert_owner(cur, f"dash-a-{marker}", trust_group=group_ac)
        owner_c = _insert_owner(cur, f"dash-c-{marker}", trust_group=group_ac)
        owner_b = _insert_owner(cur, f"dash-b-{marker}")
        peer_a = _insert_peer(cur, owner_a, f"peer-a-{marker}", f"Peer A {marker}")
        peer_b = _insert_peer(cur, owner_b, f"peer-b-{marker}", f"Peer B {marker}")
        peer_c = _insert_peer(cur, owner_c, f"peer-c-{marker}", f"Peer C {marker}")

        ns_a = _claim_namespace(cur, f"test/dash-a-{marker}", owner_a, peer_a)
        ns_b = _claim_namespace(cur, f"test/dash-b-{marker}", owner_b, peer_b)
        ns_c = _claim_namespace(cur, f"test/dash-c-{marker}", owner_c, peer_c)
        mem_c = _mem(cur, ns_c, f"note/c-{marker}",
                     {"title": f"C-title-{marker}", "content": "c-content"},
                     owner_c, peer_c, status="OPEN", tags=["gamma"])

        mem_a = _mem(cur, ns_a, f"note/a-{marker}", {"title": f"A-title-{marker}",
                                                     "content": "a-content"},
                     owner_a, peer_a, status="CONFIRMED", tags=["alpha"])
        mem_b = _mem(cur, ns_b, f"note/b-{marker}", {"title": f"B-title-{marker}",
                                                     "content": "b-content"},
                     owner_b, peer_b, status="OPEN", tags=["beta"])
        # Two rows with the SAME normalized title inside A, so the duplicates route has
        # something to find without depending on any other tenant's data.
        _mem(cur, ns_a, f"note/a-dup-{marker}", {"title": f"A-title-{marker}",
                                                 "content": "a-dup"},
             owner_a, peer_a, status="OPEN", tags=["alpha"])

        msg_a = _mem(cur, ns_a, f"msg/a-{marker}",
                     {"body": f"hello-from-a-{marker}", "thread_id": f"thr-{marker}",
                      "to": f"peer-a-{marker}"},
                     owner_a, peer_a)
        msg_b = _mem(cur, ns_b, f"msg/b-{marker}",
                     {"body": f"hello-from-b-{marker}", "to": f"peer-b-{marker}"},
                     owner_b, peer_b)
        # A requires-ack message addressed to A's peer, AUTHORED BY A's own peer inside
        # A's namespace. The notifications predicate excludes `created_by_owner_id = $1`,
        # so this row must NOT appear for A - it is the negative half. The positive half
        # is seeded below via a grant.
        _mem(cur, ns_a, f"msg/a-self-ack-{marker}",
             {"body": "self", "requires_ack": "true", "to": f"peer-a-{marker}"},
             owner_a, peer_a)
        # ... and one authored by C inside C's namespace, which A may read through the
        # grant seeded below, addressed to A's peer: the row A SHOULD see.
        notif = _mem(cur, ns_c, f"msg/c-ack-{marker}",
                     {"body": f"ack-me-{marker}", "requires_ack": "true",
                      "to": f"peer-a-{marker}"},
                     owner_c, peer_c)

        skill_a = uuid.uuid4()
        cur.execute(
            "INSERT INTO skills (id, peer_id, name, version, description, mode, share_mode, "
            "  input_schema, tags, is_current) "
            "VALUES (%s, %s, %s, '1.0.0', 'skill a', 'sync', 'public', '{}'::jsonb, %s, true)",
            (skill_a, peer_a, f"skill-a-{marker}", ["cat-a"]),
        )
        skill_b = uuid.uuid4()
        cur.execute(
            "INSERT INTO skills (id, peer_id, name, version, description, mode, share_mode, "
            "  input_schema, tags, is_current) "
            "VALUES (%s, %s, %s, '1.0.0', 'skill b', 'sync', 'public', '{}'::jsonb, %s, true)",
            (skill_b, peer_b, f"skill-b-{marker}", ["cat-b"]),
        )
        model_a = uuid.uuid4()
        cur.execute(
            "INSERT INTO models (id, peer_id, name, display_name, provider, capabilities, "
            "  context_window, port, is_current) "
            "VALUES (%s, %s, %s, %s, 'local', %s, 8192, 11434, true)",
            (model_a, peer_a, f"model-a-{marker}", f"Model A {marker}", ["chat"]),
        )
        model_b = uuid.uuid4()
        cur.execute(
            "INSERT INTO models (id, peer_id, name, display_name, provider, capabilities, "
            "  context_window, port, is_current) "
            "VALUES (%s, %s, %s, %s, 'local', %s, 8192, 11435, true)",
            (model_b, peer_b, f"model-b-{marker}", f"Model B {marker}", ["chat"]),
        )
        skill_c = uuid.uuid4()
        cur.execute(
            "INSERT INTO skills (id, peer_id, name, version, description, mode, share_mode, "
            "  input_schema, tags, is_current) "
            "VALUES (%s, %s, %s, '1.0.0', 'skill c', 'sync', 'public', '{}'::jsonb, %s, true)",
            (skill_c, peer_c, f"skill-c-{marker}", ["cat-c"]),
        )
        model_c = uuid.uuid4()
        cur.execute(
            "INSERT INTO models (id, peer_id, name, display_name, provider, capabilities, "
            "  context_window, port, is_current) "
            "VALUES (%s, %s, %s, %s, 'local', %s, 8192, 11436, true)",
            (model_c, peer_c, f"model-c-{marker}", f"Model C {marker}", ["chat"]),
        )
        cur.execute("INSERT INTO mesh_presence (peer_id, state, last_beat) "
                    "VALUES (%s, 'idle', NOW())", (peer_c,))

        cur.execute("INSERT INTO mesh_presence (peer_id, state, last_beat, note) "
                    "VALUES (%s, 'active', NOW(), 'q:hr=0.5,st=ok')", (peer_a,))
        cur.execute("INSERT INTO mesh_presence (peer_id, state, last_beat) "
                    "VALUES (%s, 'idle', NOW())", (peer_b,))

        # Grant A read on C's namespace, so the grant arm of `vis` and the co-author arm
        # of `vis_peers` are EXERCISED rather than merely present in the SQL. Without a
        # live grant somewhere, every assertion below would pass just as happily against
        # a `vis` whose grant arm contributed nothing.
        #
        # It comes from C and not from B ON PURPOSE: the grant arm is gated on
        # `meshy_same_tenant_or_devgroup($1, n.owner_owner_id)`, so a B->A grant is
        # filtered out by the tenant intersect and would have been dead weight dressed up
        # as coverage. That behaviour is itself worth pinning, so the B->A grant is
        # seeded too and test_a_cross_tenant_grant_does_not_widen_visibility asserts it.
        cur.execute(
            "INSERT INTO shared_memory_grant (namespace, grantee_peer_id, grantee_owner_id, "
            "  permission, granted_by_peer_id, granted_by_owner_id) "
            "VALUES (%s, %s, %s, 'read', %s, %s)",
            (ns_c, peer_a, owner_a, peer_c, owner_c),
        )
        cur.execute(
            "INSERT INTO shared_memory_grant (namespace, grantee_peer_id, grantee_owner_id, "
            "  permission, granted_by_peer_id, granted_by_owner_id) "
            "VALUES (%s, %s, %s, 'read', %s, %s)",
            (ns_b, peer_a, owner_a, peer_b, owner_b),
        )

        cur.execute("INSERT INTO dashboard_node_logs (owner_id, peer_id, level, source, message) "
                    "VALUES (%s, %s, 'info', 'fixture', %s)",
                    (owner_a, peer_a, f"log-line-a-{marker}"))
        cur.execute("INSERT INTO dashboard_node_logs (owner_id, peer_id, level, source, message) "
                    "VALUES (%s, %s, 'error', 'fixture', %s)",
                    (owner_b, peer_b, f"log-line-b-{marker}"))

        client_a = uuid.uuid4()
        cur.execute("INSERT INTO dashboard_clients (id, owner_id, name, country, region) "
                    "VALUES (%s, %s, %s, 'GB', 'europe')",
                    (client_a, owner_a, f"client-a-{marker}"))
        cur.execute("INSERT INTO dashboard_clients (id, owner_id, name, country, region) "
                    "VALUES (%s, %s, %s, 'US', 'americas')",
                    (uuid.uuid4(), owner_b, f"client-b-{marker}"))
        invoice_a = uuid.uuid4()
        cur.execute(
            "INSERT INTO dashboard_invoices (id, owner_id, number, client_id, status, "
            "  issue_date, due_date, subtotal_cents, tax_rate, tax_cents, total_cents, paid_at) "
            "VALUES (%s, %s, %s, %s, 'sent', CURRENT_DATE, CURRENT_DATE, 1000, '0', 0, 1000, NOW())",
            (invoice_a, owner_a, f"INV-A-{marker}", client_a))
        invoice_b = uuid.uuid4()
        cur.execute(
            "INSERT INTO dashboard_invoices (id, owner_id, number, status, issue_date, "
            "  due_date, subtotal_cents, tax_rate, tax_cents, total_cents) "
            "VALUES (%s, %s, %s, 'draft', CURRENT_DATE, CURRENT_DATE, 500, '0', 0, 500)",
            (invoice_b, owner_b, f"INV-B-{marker}"))
        cur.execute(
            "INSERT INTO dashboard_invoice_items (id, invoice_id, description, quantity, "
            "  unit_price_cents, amount_cents, sort_order) "
            "VALUES (%s, %s, %s, '1', 1000, 1000, 0)",
            (uuid.uuid4(), invoice_a, f"item-a-{marker}"))
        cur.execute(
            "INSERT INTO dashboard_invoice_items (id, invoice_id, description, quantity, "
            "  unit_price_cents, amount_cents, sort_order) "
            "VALUES (%s, %s, %s, '1', 500, 500, 0)",
            (uuid.uuid4(), invoice_b, f"item-b-{marker}"))
        cur.execute(
            "INSERT INTO dashboard_documents (id, owner_id, number, type, status, fields) "
            "VALUES (%s, %s, %s, 'contract', 'draft', '{}'::jsonb)",
            (uuid.uuid4(), owner_a, f"DOC-A-{marker}"))
        cur.execute(
            "INSERT INTO dashboard_documents (id, owner_id, number, type, status, fields) "
            "VALUES (%s, %s, %s, 'contract', 'draft', '{}'::jsonb)",
            (uuid.uuid4(), owner_b, f"DOC-B-{marker}"))
        cur.execute(
            "INSERT INTO dashboard_expenses (id, owner_id, date, vendor, description, category, "
            "  amount_cents, currency, is_deductible) "
            "VALUES (%s, %s, CURRENT_DATE, %s, 'd', 'software', 250, 'GBP', true)",
            (uuid.uuid4(), owner_a, f"vendor-a-{marker}"))
        cur.execute(
            "INSERT INTO dashboard_expenses (id, owner_id, date, vendor, description, category, "
            "  amount_cents, currency, is_deductible) "
            "VALUES (%s, %s, CURRENT_DATE, %s, 'd', 'software', 750, 'USD', true)",
            (uuid.uuid4(), owner_b, f"vendor-b-{marker}"))
        cur.execute(
            "INSERT INTO dashboard_payouts (id, owner_id, contractor, period_start, period_end, "
            "  status, total_owed_cents) "
            "VALUES (%s, %s, %s, CURRENT_DATE, CURRENT_DATE, 'pending', 4200)",
            (uuid.uuid4(), owner_a, f"contractor-a-{marker}"))
        cur.execute(
            "INSERT INTO dashboard_payouts (id, owner_id, contractor, period_start, period_end, "
            "  status, total_owed_cents) "
            "VALUES (%s, %s, %s, CURRENT_DATE, CURRENT_DATE, 'pending', 99)",
            (uuid.uuid4(), owner_b, f"contractor-b-{marker}"))

        cur.execute("INSERT INTO dashboard_memory_bridges (id, source_namespace_id, "
                    "  target_namespace_id, bridge_type, created_by_owner_id, created_by_peer_id) "
                    # bridge_type is CHECKed against ('read','write','bidirectional')
                    # - read off pg_constraint, not guessed from the word "mirror".
                    "VALUES (%s, %s, %s, 'read', %s, %s)",
                    (uuid.uuid4(), ns_a, ns_c, owner_a, peer_a))

        # The usage namespace is GLOBAL by name, so it is created only if
        # it does not already exist and removed again only if this fixture created it.
        cur.execute("SELECT 1 FROM shared_memory_namespace WHERE namespace = %s", (USAGE_NS,))
        if cur.fetchone() is None:
            _claim_namespace(cur, USAGE_NS, owner_a, peer_a)
            created_usage_ns = True
        # BOTH owners are granted on the usage namespace. `expected` in the usage query is
        # "the claiming owner + its active write grantees", and `shared/agent-usage` is a
        # FIXED, GLOBAL name - so whether this fixture owns it depends on what else is in
        # the database. Granting both makes the usage assertions independent of that,
        # which matters because the first version of this fixture passed on a fresh
        # database and failed on one where the namespace already existed.
        for _peer, _owner in ((peer_a, owner_a), (peer_b, owner_b)):
            cur.execute(
                "INSERT INTO shared_memory_grant (namespace, grantee_peer_id, "
                "  grantee_owner_id, permission, granted_by_peer_id, granted_by_owner_id) "
                "VALUES (%s, %s, %s, 'write', %s, %s)",
                (USAGE_NS, _peer, _owner, _peer, _owner))
        usage_a = _mem(cur, USAGE_NS, f"usage/peer-a-{marker}",
                       {"totals": {"est_cost_usd": 12.5, "output": 100, "input": 200,
                                   "turns": 3}},
                       owner_a, peer_a)
        usage_b = _mem(cur, USAGE_NS, f"usage/peer-b-{marker}",
                       {"totals": {"est_cost_usd": 99.0, "output": 1, "input": 2,
                                   "turns": 1}},
                       owner_b, peer_b)

        # Tokens. A PAT for owner A and a live session cookie for owner A, so the two
        # non-JWT credential classes are exercised against a real row rather than
        # asserted about in a comment.
        pat_raw = "meshypat_" + secrets.token_urlsafe(24)
        cur.execute(
            "INSERT INTO owner_api_keys (id, owner_id, name, token_hash, token_prefix, scopes) "
            "VALUES (%s, %s, 'test', %s, %s, %s)",
            (uuid.uuid4(), owner_a, hashlib.sha256(pat_raw.encode()).hexdigest(),
             pat_raw[:12], ["dashboard:read"]))
        revoked_raw = "meshypat_" + secrets.token_urlsafe(24)
        cur.execute(
            "INSERT INTO owner_api_keys (id, owner_id, name, token_hash, token_prefix, scopes, "
            "  revoked_at) "
            "VALUES (%s, %s, 'revoked', %s, %s, %s, NOW())",
            (uuid.uuid4(), owner_a, hashlib.sha256(revoked_raw.encode()).hexdigest(),
             revoked_raw[:12], ["dashboard:read"]))
        session_raw = secrets.token_urlsafe(32)
        cur.execute(
            "INSERT INTO owner_sessions (id, owner_id, created_at, expires_at, last_seen_at, "
            "  token_hash) VALUES (%s, %s, NOW(), NOW() + INTERVAL '1 hour', NOW(), %s)",
            (uuid.uuid4(), owner_a, hashlib.sha256(session_raw.encode()).hexdigest()))

    w = World(marker=marker, owner_a=owner_a, owner_b=owner_b, owner_c=owner_c,
              peer_a=peer_a, peer_b=peer_b, peer_c=peer_c,
              ns_a=ns_a, ns_b=ns_b, ns_c=ns_c, mem_a=mem_a, mem_b=mem_b, mem_c=mem_c,
              msg_a=msg_a, msg_b=msg_b,
              notif=notif, skill_a=skill_a, skill_b=skill_b, skill_c=skill_c,
              model_a=model_a, model_b=model_b, model_c=model_c,
              invoice_a=invoice_a, invoice_b=invoice_b,
              usage_a=usage_a, usage_b=usage_b, pat=pat_raw, revoked_pat=revoked_raw,
              session=session_raw)
    yield w

    with _sql_conn() as conn, conn.cursor() as cur:
        # This run's OWN rows, deleted by id rather than by the sweeper's pattern (the
        # sweeper only touches rows older than its grace window, so it would skip these). shared_memory has an FK to peers and
        # to the namespace, and dashboard_users cascades only to peers, so the order
        # here is not cosmetic.
        cur.execute("DELETE FROM shared_memory WHERE created_by_owner_id IN (%s, %s, %s)",
                    (owner_a, owner_b, owner_c))
        cur.execute("DELETE FROM shared_memory_grant WHERE grantee_owner_id IN (%s, %s, %s)",
                    (owner_a, owner_b, owner_c))
        cur.execute("DELETE FROM dashboard_memory_bridges WHERE created_by_owner_id IN (%s, %s, %s)",
                    (owner_a, owner_b, owner_c))
        cur.execute("DELETE FROM shared_memory_namespace WHERE owner_owner_id IN (%s, %s, %s)"
                    + ("" if created_usage_ns else " AND namespace <> 'shared/agent-usage'"),
                    (owner_a, owner_b, owner_c))
        for table in ("dashboard_node_logs", "dashboard_log_settings", "dashboard_invoice_items",
                      "dashboard_invoices", "dashboard_documents", "dashboard_expenses",
                      "dashboard_payouts", "dashboard_clients", "owner_api_keys",
                      "owner_sessions", "dashboard_notification_state",
                      "dashboard_message_state", "accounts_entitlements"):
            if table == "dashboard_invoice_items":
                cur.execute("DELETE FROM dashboard_invoice_items WHERE invoice_id IN "
                            "(SELECT id FROM dashboard_invoices WHERE owner_id IN (%s, %s, %s))",
                            (owner_a, owner_b, owner_c))
                continue
            cur.execute(f"DELETE FROM {table} WHERE owner_id IN (%s, %s, %s)", (owner_a, owner_b, owner_c))
        cur.execute("DELETE FROM mesh_presence WHERE peer_id IN (%s, %s, %s)", (peer_a, peer_b, peer_c))
        cur.execute("DELETE FROM skills WHERE peer_id IN (%s, %s, %s)", (peer_a, peer_b, peer_c))
        cur.execute("DELETE FROM models WHERE peer_id IN (%s, %s, %s)", (peer_a, peer_b, peer_c))
        cur.execute("DELETE FROM dashboard_users WHERE id IN (%s, %s, %s)", (owner_a, owner_b, owner_c))


# ------------------------------------------------------------------- clients

def mint(owner_id, *, aud="dashboard", issuer=ISSUER, ttl=3600) -> str:
    now = int(_dt.datetime.now(_dt.timezone.utc).timestamp())
    return pyjwt.encode(
        {"sub": str(owner_id), "iss": issuer, "iat": now, "exp": now + ttl,
         "scopes": [], "aud": aud},
        JWT_PRIVATE_PEM, algorithm="EdDSA")


def get(owner_id, path, **params):
    client = Client()
    return client.get(path, params or None,
                      HTTP_AUTHORIZATION=f"Bearer {mint(owner_id)}")


def body(response):
    assert response["Content-Type"] == "application/json", response["Content-Type"]
    return json.loads(response.content.decode("utf-8"))


def ok(owner_id, path, **params):
    response = get(owner_id, path, **params)
    assert response.status_code == 200, (path, response.status_code,
                                         response.content[:400])
    return body(response)


# =========================================================================
# 1. wiring
# =========================================================================

ROUTES = [
    ("/v1/dashboard/api/peers", "dashboard-peers"),
    ("/v1/dashboard/api/dispatch/nodes", "dashboard-dispatch-nodes"),
    ("/v1/dashboard/api/namespaces", "dashboard-namespaces"),
    ("/v1/dashboard/api/memories", "dashboard-memories"),
    ("/v1/dashboard/api/memories/peers", "dashboard-memory-peers"),
    ("/v1/dashboard/api/memories/search", "dashboard-memory-search"),
    ("/v1/dashboard/api/memories/export", "dashboard-memory-export"),
    ("/v1/dashboard/api/memories/duplicates", "dashboard-memory-duplicates"),
    ("/v1/dashboard/api/messages", "dashboard-messages"),
    ("/v1/dashboard/api/stats", "dashboard-stats"),
    ("/v1/dashboard/api/tier-info", "dashboard-tier-info"),
    ("/v1/dashboard/api/models", "dashboard-models"),
    ("/v1/dashboard/api/usage", "dashboard-usage"),
    ("/v1/dashboard/api/tools", "dashboard-tools"),
    ("/v1/dashboard/api/bridges", "dashboard-bridges"),
    ("/v1/dashboard/api/marketplace", "dashboard-marketplace"),
    ("/v1/dashboard/api/notifications", "dashboard-notifications"),
    ("/v1/dashboard/api/notifications/count", "dashboard-notifications-count"),
    ("/v1/dashboard/api/logs", "dashboard-logs"),
    ("/v1/dashboard/api/logs/settings", "dashboard-log-settings"),
    ("/v1/dashboard/agents", "dashboard-agents"),
    ("/v1/dashboard/api/clients", "dashboard-clients"),
    ("/v1/dashboard/api/invoices", "dashboard-invoices"),
    ("/v1/dashboard/api/invoices/11111111-1111-1111-1111-111111111111/items",
     "dashboard-invoice-items"),
    ("/v1/dashboard/api/documents", "dashboard-documents"),
    ("/v1/dashboard/api/expenses", "dashboard-expenses"),
    ("/v1/dashboard/api/payouts", "dashboard-payouts"),
    ("/v1/dashboard/api/tax-report", "dashboard-tax-report"),
]


@pytest.mark.parametrize("path,name", ROUTES)
def test_every_route_resolves_through_the_project_urlconf(path, name):
    """No ROOT_URLCONF override anywhere in this file - this resolves against
    meshychat.urls, which is what a real request resolves against."""
    match = resolve(path)
    assert match.url_name == name, f"{path} -> {match.url_name!r}, expected {name!r}"


def test_a_route_that_should_not_exist_is_a_404():
    """POSITIVE CONTROL for the parametrised test above: `resolve()` must be capable of
    failing here, or "it resolved" proves only that resolve() returns something."""
    with pytest.raises(Resolver404):
        resolve("/v1/dashboard/api/definitely-not-mounted")


def test_reverse_agrees_with_resolve():
    """A name that resolves but cannot be reversed means two different route tables."""
    assert reverse("dashboard-peers") == "/v1/dashboard/api/peers"
    assert reverse("dashboard-invoice-items", args=["abc"]) == \
        "/v1/dashboard/api/invoices/abc/items"


# =========================================================================
# 2. the control for the controls
# =========================================================================

def test_the_rls_context_is_what_makes_the_read_return_rows(world):
    """The hazard this whole surface is built around, demonstrated end to end.

    Same connection, same SQL, same owner. Without `app.server_internal='1'` the read
    returns ZERO rows and raises NOTHING - which on the wire is an empty dashboard
    panel. With it, the rows are there. So "empty" is not evidence of anything until
    the context is proven, and `repo.prove_context` is what proves it.
    """
    scope = TenantScope(world.owner_a)
    sql = queries.namespaces_sql(scope.vis_cte())
    with dbconn.serving_connection() as conn:
        # (a) unprivileged: silently empty.
        with conn.cursor() as cur:
            cur.execute(sql, scope.params())
            unprivileged = cur.fetchall()
        assert unprivileged == [], (
            "expected the unprivileged read to be silently empty; if it returned rows, "
            "this connection is bypassing RLS and every isolation test below is void")

        # (b) privileged, through the repository: the SAME query finds the namespace.
        outcome = repo.fetch_all_internal(conn, sql, scope.params(), "namespaces")
        names = [o["name"] for o in repo.objs(outcome)]
        assert world.ns_a in names, "POSITIVE CONTROL FAILED: owner A cannot see its own namespace"

        # (c) the repository refuses to produce (a)'s answer at all.
        with pytest.raises(errors.ApiError) as caught:
            with conn.cursor() as cur:
                repo.prove_context(cur)
        assert caught.value.status == 500
        assert caught.value.detail and "app.server_internal" in caught.value.detail


def test_typed_absence_maps_to_status_codes_not_to_empty_collections():
    """The vocabulary in `apps/dashboard/repo.py`, exercised rather than asserted about.

    `Empty` renders as `[]` because "nothing matched" really is an empty panel; anything
    that is not a named outcome is an internal error, never an empty collection.
    (`NotFound`/`Denied` were deleted on 2026-09-29: no production path built them, so
    their half of this test only tested itself.)
    """
    assert repo.rows_or_raise(repo.Rows([{"obj": 1}], "t")) == [{"obj": 1}]
    assert repo.rows_or_raise(repo.Empty("t")) == []          # the control

    # An unrecognised outcome is an internal error, not a silent empty list.
    with pytest.raises(errors.ApiError) as bogus:
        repo.rows_or_raise(object())
    assert bogus.value.status == 500


def test_a_projection_without_the_obj_column_raises_instead_of_rendering_nulls():
    """`repo.objs` reads the column by explicit key. A `.get("obj")` that defaulted to
    None would turn a mis-aliased projection into a list of nulls - which renders as an
    empty dashboard panel rather than an error. Control: the well-formed row works."""
    assert repo.objs(repo.Rows([{"obj": {"id": 1}}], "t")) == [{"id": 1}]
    with pytest.raises(errors.ApiError) as caught:
        repo.objs(repo.Rows([{"not_obj": 1}], "t"))
    assert caught.value.status == 500


def test_the_app_refuses_to_serve_on_a_bypassing_connection():
    """`meshychat_owner` has rolbypassrls (pg_roles.rolbypassrls: meshychat_owner=t, meshychat_app=f),
    so a request served on it would read every tenant's rows and raise nothing. The
    refusal is an exception, and the positive control is that the SAME guard passes on
    the connection the app actually opens."""
    with psycopg.connect(DSN, autocommit=True) as bypassing:
        with pytest.raises(dbconn.RlsBypassError):
            dbconn.assert_serving_connection(bypassing)
    with dbconn.serving_connection() as good:
        dbconn.assert_serving_connection(good)  # no raise: the control


# =========================================================================
# 3. authentication
# =========================================================================

def test_no_credential_is_401_and_a_credential_is_200(world):
    """Both halves in one test: the 401 proves the route is guarded, the 200 proves the
    401 was not simply a broken route."""
    unauth = Client().get("/v1/dashboard/api/peers")
    assert unauth.status_code == 401
    assert body(unauth)["error"] == "E_AUTH_MISSING"
    assert get(world.owner_a, "/v1/dashboard/api/peers").status_code == 200


def test_a_peer_audience_token_is_403_not_401(world):
    """The token is verified but is for another audience, so it is 403 E_FORBIDDEN. Control:
    the identical token with aud=dashboard is a 200."""
    peer_tok = mint(world.owner_a, aud="peer")
    denied = Client().get("/v1/dashboard/api/peers", HTTP_AUTHORIZATION=f"Bearer {peer_tok}")
    assert denied.status_code == 403
    assert body(denied)["error"] == "E_FORBIDDEN"
    assert get(world.owner_a, "/v1/dashboard/api/peers").status_code == 200


def test_a_forged_sub_is_401_unknown_owner(world):
    """The dashboard_users EXISTS check refuses a forged sub before it resolves to a
    non-existent owner and surfaces as a 500 downstream."""
    forged = mint(uuid.uuid4())
    response = Client().get("/v1/dashboard/api/peers", HTTP_AUTHORIZATION=f"Bearer {forged}")
    assert response.status_code == 401
    assert body(response)["error"] == "E_AUTH_INVALID"
    assert get(world.owner_a, "/v1/dashboard/api/peers").status_code == 200


def test_a_stale_session_cookie_does_not_fall_through_to_the_bearer_token(world):
    """A stale cookie must not escalate to another scheme.

    The request carries a VALID Bearer token AND a dead cookie. If the cookie path fell
    through, this would be a 200. The control is the same request without the cookie.
    """
    client = Client()
    client.cookies["meshy_session"] = "not-a-live-session-token"
    blocked = client.get("/v1/dashboard/api/peers",
                         HTTP_AUTHORIZATION=f"Bearer {mint(world.owner_a)}")
    assert blocked.status_code == 401
    assert body(blocked)["error"] == "E_SESSION_EXPIRED"
    assert get(world.owner_a, "/v1/dashboard/api/peers").status_code == 200


def test_a_live_session_cookie_authenticates(world):
    client = Client()
    client.cookies["meshy_session"] = world.session
    response = client.get("/v1/dashboard/agents")
    assert response.status_code == 200, response.content[:300]
    names = [a["name"] for a in body(response)["agents"]]
    assert f"peer-a-{world.marker}" in names


def test_a_pat_authenticates_and_a_revoked_one_does_not(world):
    """The control is asserted FIRST: if the live PAT did not work, the revoked one
    failing would prove nothing about revocation."""
    live = Client().get("/v1/dashboard/agents", HTTP_AUTHORIZATION=f"Bearer {world.pat}")
    assert live.status_code == 200, live.content[:300]
    dead = Client().get("/v1/dashboard/agents",
                        HTTP_AUTHORIZATION=f"Bearer {world.revoked_pat}")
    assert dead.status_code == 401
    assert body(dead)["error"] == "E_API_KEY_INVALID"


def test_an_expired_pat_is_refused(world):
    """`_from_pat` accepts a key whose `expires_at` is NULL or in the future. The fixture PATs never
    expire and the test above only REVOKES, so that
    predicate could be deleted with this file green and an expired key kept full
    `dashboard:read` access. tests/test_grants.py pins the same predicate on the grants
    extractor, which is a separate copy.

    ONE VARIABLE: this key's own `expires_at` (a fresh row, so the module-scoped
    `world.pat` other tests use is not touched). CONTROL: a 200 while it is in the
    future."""
    raw = "meshypat_" + secrets.token_urlsafe(24)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    with _sql_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO owner_api_keys (id, owner_id, name, token_hash, token_prefix, scopes, "
            "  expires_at) VALUES (%s, %s, 'expiring', %s, %s, %s, NOW() + INTERVAL '1 hour')",
            (uuid.uuid4(), world.owner_a, digest, raw[:12], ["dashboard:read"]))

    live = Client().get("/v1/dashboard/agents", HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert live.status_code == 200, live.content[:300]

    with _sql_conn() as conn, conn.cursor() as cur:
        cur.execute("UPDATE owner_api_keys SET expires_at = NOW() - INTERVAL '1 second' "
                    "WHERE token_hash = %s", (digest,))
        assert cur.rowcount == 1
    dead = Client().get("/v1/dashboard/agents", HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert dead.status_code == 401, dead.content[:300]
    assert body(dead)["error"] == "E_API_KEY_INVALID"


def test_dev_jwt_is_refused_when_sso_is_configured(world):
    """The production posture sets MESHY_SSO_CONFIGURED and disables MESHY_ALLOW_DEV_JWT, so
    every non-prefixed Bearer JWT is rejected as E_AUTH_MISSING, not E_FORBIDDEN. Control: the
    same token with the same settings but dev JWT allowed."""
    token = mint(world.owner_a)
    with override_settings(MESHY_SSO_CONFIGURED=True, MESHY_ALLOW_DEV_JWT=False):
        refused = Client().get("/v1/dashboard/agents", HTTP_AUTHORIZATION=f"Bearer {token}")
    assert refused.status_code == 401
    assert body(refused)["error"] == "E_AUTH_MISSING"
    with override_settings(MESHY_SSO_CONFIGURED=True, MESHY_ALLOW_DEV_JWT=True):
        allowed = Client().get("/v1/dashboard/agents", HTTP_AUTHORIZATION=f"Bearer {token}")
    assert allowed.status_code == 200, allowed.content[:300]


@pytest.fixture
def production_auth_posture():
    """SSO configured and the dev JWT off: the posture production runs in. The autouse
    settings fixture pins MESHY_SSO_CONFIGURED=False for every other test here, so without
    this the credentials production actually accepts were only ever tested with SSO off
    (review queue 269: 74 of the file's tests)."""
    with override_settings(MESHY_SSO_CONFIGURED=True, MESHY_ALLOW_DEV_JWT=False):
        yield


def test_in_the_production_posture_a_session_cookie_and_a_pat_still_authenticate(
        world, production_auth_posture):
    """With SSO on and the dev JWT off, the dashboard's real credentials must still work:
    a live session cookie and a live PAT answer 200, a revoked PAT and a dead cookie are
    refused with their own codes, and a stale cookie does not fall through to a Bearer
    header. The dev-JWT refusal is the control that the posture is really in force: the
    same minted token that every other test uses is refused here."""
    token = mint(world.owner_a)
    refused = Client().get("/v1/dashboard/agents", HTTP_AUTHORIZATION=f"Bearer {token}")
    assert refused.status_code == 401, (
        "POSITIVE CONTROL FAILED: a dev JWT was accepted, so the production posture is not "
        "in force and nothing below is measured under it")
    assert body(refused)["error"] == "E_AUTH_MISSING"

    cookie = Client()
    cookie.cookies["meshy_session"] = world.session
    live = cookie.get("/v1/dashboard/agents")
    assert live.status_code == 200, ("session cookie", live.content[:300])

    pat = Client().get("/v1/dashboard/agents", HTTP_AUTHORIZATION=f"Bearer {world.pat}")
    assert pat.status_code == 200, ("PAT", pat.content[:300])

    dead_pat = Client().get("/v1/dashboard/agents",
                            HTTP_AUTHORIZATION=f"Bearer {world.revoked_pat}")
    assert dead_pat.status_code == 401
    assert body(dead_pat)["error"] == "E_API_KEY_INVALID"

    stale = Client()
    stale.cookies["meshy_session"] = "not-a-live-session-token"
    blocked = stale.get("/v1/dashboard/agents", HTTP_AUTHORIZATION=f"Bearer {world.pat}")
    assert blocked.status_code == 401, (
        "a dead session cookie fell through to a valid PAT under the production posture")
    assert body(blocked)["error"] == "E_SESSION_EXPIRED"


def test_a_wrong_issuer_token_is_rejected(world):
    """`iss` is a required claim, not merely a compared one. Control: the
    same claims with the right issuer."""
    wrong = mint(world.owner_a, issuer="somebody-else")
    response = Client().get("/v1/dashboard/agents", HTTP_AUTHORIZATION=f"Bearer {wrong}")
    assert response.status_code == 401
    assert body(response)["error"] == "E_AUTH_INVALID"
    assert get(world.owner_a, "/v1/dashboard/agents").status_code == 200


def _claims(owner_id, **overrides) -> dict:
    """The claims `mint` signs, with any key set to None REMOVED rather than nulled, so a
    "missing claim" case is a token that really lacks the claim."""
    now = int(_dt.datetime.now(_dt.timezone.utc).timestamp())
    claims = {"sub": str(owner_id), "iss": ISSUER, "iat": now, "exp": now + 3600,
              "scopes": [], "aud": "dashboard"}
    for key, value in overrides.items():
        if value is None:
            claims.pop(key)
        else:
            claims[key] = value
    return claims


def _bearer_get(token: str, path: str = "/v1/dashboard/agents"):
    return Client().get(path, HTTP_AUTHORIZATION=f"Bearer {token}")


#: STATUS C27 / RFQ M13. Each case is one claim changed on an otherwise valid token, and
#: the reason PyJWT gives for it, so a 401 for some OTHER reason cannot pass the case.
#: The expiry is 90 s past, not an hour: a verifier with the common 60 s clock leeway still
#: refuses it with a 30 s margin, while an hour let a verifier with a 3500 s leeway pass this
#: file.
_EXP_ISS_CASES = {
    "expired 90 s ago": ({"exp": lambda now: now - 90}, "Signature has expired"),
    "no exp claim": ({"exp": None}, 'missing the "exp" claim'),
    "no iss claim": ({"iss": None}, 'missing the "iss" claim'),
}


@pytest.mark.parametrize("case", sorted(_EXP_ISS_CASES))
def test_a_dashboard_token_needs_a_live_exp_and_an_iss(world, case):
    """STATUS C27, the `exp`/`iss` half (RFQ M12/M13). Both claims are required and the
    expiry is verified; `verify_dashboard_jwt` says the same in `options`, and
    nothing held it to that: no dashboard test presented an expired token, or one
    missing either claim.

    CONTROL FIRST: the identical claims, fresh and complete, are a 200 on the same route
    with the same key, so each 401 below is the claim and not a broken route or key.

    Measured against mutants of `verify_dashboard_jwt`:
      * `"verify_exp": True` -> `False`: an expired token is a 200. Killed by the
        expired case - the one real escalation among these.
      * `"require": ["exp", "iss"]` -> `[]`: killed by the "no exp claim" case on the
        REASON only. The token is still a 401, because `require_claim_types` re-applies
        the presence rule; the reason changes from PyJWT's 'missing the "exp" claim' to
        'missing required claim: exp'. The function's docstring says the guarantee must
        come from THIS call site rather than a later layer, and the reason is how that
        is observable.
      * `require_claim_types(claims)` removed: survives here (the call-site `require`
        masks it) and is killed by tests/test_jwt_claim_types.py.
      * BOTH of the above removed: a token with no `exp` is a 200. Killed by the
        "no exp claim" case.
    A missing `iss` is refused three times over (`require`, PyJWT's `issuer=` check,
    `require_claim_types`), so no single deletion changes that case.
    """
    overrides, reason = _EXP_ISS_CASES[case]
    now = int(_dt.datetime.now(_dt.timezone.utc).timestamp())
    resolved = {k: (v(now) if callable(v) else v) for k, v in overrides.items()}

    control = _bearer_get(pyjwt.encode(_claims(world.owner_a), JWT_PRIVATE_PEM,
                                       algorithm="EdDSA"))
    assert control.status_code == 200, control.content[:300]

    variant = pyjwt.encode(_claims(world.owner_a, **resolved), JWT_PRIVATE_PEM,
                           algorithm="EdDSA")
    refused = _bearer_get(variant)
    assert refused.status_code == 401, refused.content[:300]
    assert body(refused)["error"] == "E_AUTH_INVALID"
    assert reason in body(refused)["message"], body(refused)


def _b64url(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def test_an_hs256_token_keyed_by_the_public_key_is_refused_by_the_algorithm_pin(world):
    """STATUS C27, the algorithm half (RFQ M12): the classic key-confusion token.

    An HS256 token whose HMAC key is the server's PUBLIC PEM - which anyone can hold - is
    exactly what `JWT_ALGORITHM = "EdDSA"` exists to refuse. Adding "HS256" to the
    `algorithms` list survived 83 of 83, because PyJWT 2.14 ALSO refuses a PEM-shaped
    HMAC key (InvalidKeyError). That library heuristic is not this module's control and
    has been bypassed before (PyJWT < 2.4.0, CVE-2022-29217), so the test pins WHICH
    check refused the token: the algorithm allowlist, reported by PyJWT as "The
    specified alg value is not allowed". With HS256 allowed, the refusal would come from
    the key heuristic instead and this assertion fails.

    THREE CONTROLS:
      1. the token is a genuine HS256 signature under the public PEM (verified with
         PyJWT's own HMAC primitive, which does not apply the key heuristic), so the
         refusal is not a malformed fixture;
      2. the SAME claims signed EdDSA are a 200 on the same route;
      3. the refusal is a 401 E_AUTH_INVALID, not a 500 from an unhandled key error.
    """
    import hashlib as _hashlib
    import hmac as _hmac

    from jwt.algorithms import HMACAlgorithm

    claims = _claims(world.owner_a)
    header = {"alg": "HS256", "typ": "JWT"}
    signing_input = (_b64url(json.dumps(header, separators=(",", ":")).encode()) + "."
                     + _b64url(json.dumps(claims, separators=(",", ":")).encode()))
    public_key = JWT_PUBLIC_PEM.encode("utf-8")
    signature = _hmac.new(public_key, signing_input.encode("ascii"), _hashlib.sha256).digest()
    forged = f"{signing_input}.{_b64url(signature)}"

    # CONTROL 1 - a correct HMAC-SHA256 under the public key, by PyJWT's own primitive.
    assert HMACAlgorithm(HMACAlgorithm.SHA256).verify(
        signing_input.encode("ascii"), public_key, signature)

    # CONTROL 2 - identical claims, the pinned algorithm: accepted.
    accepted = _bearer_get(pyjwt.encode(claims, JWT_PRIVATE_PEM, algorithm="EdDSA"))
    assert accepted.status_code == 200, accepted.content[:300]

    refused = _bearer_get(forged)
    assert refused.status_code == 401, refused.content[:300]
    assert body(refused)["error"] == "E_AUTH_INVALID"
    assert "alg value is not allowed" in body(refused)["message"], body(refused)


# =========================================================================
# 4. the tenancy claim, route by route, each with its positive control
# =========================================================================

def test_peers_roster_shape_and_presence(world):
    payload = ok(world.owner_a, "/v1/dashboard/api/peers")
    assert "peers" in payload
    mine = [p for p in payload["peers"] if p["id"] == str(world.peer_a)]
    assert len(mine) == 1, "POSITIVE CONTROL FAILED: owner A does not see its own peer"
    peer = mine[0]
    # The documented shape, key by key. A subset check would pass against a projection
    # that had silently lost half its columns.
    for key in ("id", "name", "display_name", "tier", "status", "registration_status",
                "trust_level", "last_seen_at", "presence_age_secs", "heartbeat_live",
                "monitor_state", "monitor_live", "agent_type", "tools", "models",
                "created_at", "updated_at", "ws_online", "connection_live",
                "reachable", "active", "last_sent_at", "liveness", "presence_stale", "self_reported"):
        assert key in peer, f"peers projection is missing {key!r}"
    assert peer["heartbeat_live"] is True       # last_beat = NOW()
    assert peer["monitor_state"] == "active"
    assert peer["tools"] == 1 and peer["models"] == 1
    # SD 356: the socket flags are the MEASURED record (hub_connections), not Redis. This fixture holds no
    # socket, so the seat is not reachable, and ws_online says the same thing by the same rule.
    assert peer["reachable"] is False and peer["ws_online"] is False and peer["connection_live"] is False
    assert peer["liveness"] in ("active", "offline")
    assert peer["self_reported"] is None or set(peer["self_reported"]) == {"state", "note", "expires_at", "published_at"}


def test_measured_liveness_reads_each_acceptance_case(world):
    """SD 356 acceptance, against the database (pxx d7c18d P2-1), one peer per case under an owner of its own:
    (a) a msg/ head it wrote 5 min ago: active, liveness 'active', last_sent_at set;
    (b) a socket seen now and no send: reachable, liveness 'reachable_idle';
    (c) a send 31 min old, one in the wake-alerts thread, and one another peer wrote: none makes it active;
    (d) no TTL and updated 7 h ago: presence_stale, not active;
    (e) a lapsed TTL: presence_stale, not active;
    (f) an idle beat with a live TTL, and an active beat 2 h old with a 24 h TTL: not active;
        CONTROL for (f): an active beat published now with a live TTL is active;
    (g) pxx b150a0 / Codex: every row's updated_at is fresh (an invoke touches it), and only last_beat ages it; a
        touch-only row (last_beat NULL) reads self_reported null and is not active; a beat 50 min old with a live
        1 h TTL is active (the hourly re-publish cadence)."""
    import time as _time
    marker = uuid.uuid4().hex[:8]
    now = _dt.datetime.now(_dt.timezone.utc)
    names = ("sent", "socket", "old", "wake", "other", "nottl", "lapsed", "idlebeat", "oldbeat", "beat",
             "touchonly", "beat50")
    with _sql_conn() as conn, conn.cursor() as cur:
        owner = _insert_owner(cur, f"dash-a-live-{marker}")
        p = {n: _insert_peer(cur, owner, f"live-{n}-{marker}") for n in names}
        stranger = _insert_peer(cur, owner, f"live-stranger-{marker}")
        ns = _claim_namespace(cur, "shared/mesh-chat", owner, p["sent"])

        def send(peer, minutes_ago, thread="t-live", writer=None):
            at = now - _dt.timedelta(minutes=minutes_ago)
            key = "msg/%d-live-%s" % (int(at.timestamp() * 1000), uuid.uuid4().hex[:6])
            rid = uuid.uuid4()
            cols = sr.row(writer or peer, owner, ns, key, {"v": 1, "thread_id": thread, "body": "x"}, rid=rid,
                          call_id=f"call/{rid}", author_label="fixture", created_at=at)
            cur.execute(f"INSERT INTO shared_memory ({', '.join(cols)}) "
                        f"VALUES ({', '.join(['%s'] * len(cols))})", list(cols.values()))
        send(p["sent"], 5)                                   # (a)
        send(p["old"], 31)                                   # (c) too old
        send(p["wake"], 5, thread="wake-alerts")            # (c) a daemon's thread
        send(p["other"], 5, writer=stranger)                # (c) written by another peer: it is the stranger's
        cur.execute("INSERT INTO hub_connections (connection_id, peer_id, worker_token, seen_at) "
                    "VALUES (%s, %s, %s, NOW())", (uuid.uuid4(), p["socket"], uuid.uuid4()))   # (b)

        def beat(peer, state, *, published, expires):
            # updated_at is NOW() for every row, as an invoke's touch leaves it: only last_beat may age a beat
            cur.execute("INSERT INTO mesh_presence (peer_id, state, last_beat, updated_at, state_expires_at) "
                        "VALUES (%s, %s, %s, NOW(), %s)", (peer, state, published, expires))
        beat(p["nottl"], "active", published=now - _dt.timedelta(hours=7), expires=None)          # (d)
        beat(p["lapsed"], "active", published=now - _dt.timedelta(minutes=10),
             expires=now - _dt.timedelta(minutes=1))                                               # (e)
        beat(p["idlebeat"], "idle", published=now, expires=now + _dt.timedelta(hours=1))            # (f)
        beat(p["oldbeat"], "active", published=now - _dt.timedelta(hours=2),
             expires=now + _dt.timedelta(hours=22))                                                # (f)
        beat(p["beat"], "active", published=now, expires=now + _dt.timedelta(hours=1))              # control
        beat(p["touchonly"], "active", published=None, expires=None)                                # (g)
        beat(p["beat50"], "active", published=now - _dt.timedelta(minutes=50),
             expires=now + _dt.timedelta(minutes=10))                                              # (g)
    try:
        rows = {r["id"]: r for r in ok(owner, "/v1/dashboard/api/peers")["peers"]}
        r = {n: rows[str(pid)] for n, pid in p.items()}
        assert r["sent"]["active"] is True and r["sent"]["liveness"] == "active" and r["sent"]["last_sent_at"], r["sent"]
        assert r["socket"]["reachable"] is True and r["socket"]["ws_online"] is True
        assert r["socket"]["active"] is False and r["socket"]["liveness"] == "reachable_idle", r["socket"]
        for n in ("old", "wake", "other"):
            assert r[n]["active"] is False and r[n]["last_sent_at"] is None, (n, r[n])
        assert r["nottl"]["presence_stale"] is True and r["nottl"]["active"] is False, r["nottl"]
        assert r["lapsed"]["presence_stale"] is True and r["lapsed"]["active"] is False, r["lapsed"]
        for n in ("idlebeat", "oldbeat"):
            assert r[n]["active"] is False and r[n]["presence_stale"] is False, (n, r[n])
        assert r["beat"]["active"] is True and r["beat"]["liveness"] == "active", r["beat"]
        assert r["touchonly"]["self_reported"] is None and r["touchonly"]["presence_stale"] is None, r["touchonly"]
        assert r["touchonly"]["active"] is False, r["touchonly"]
        assert r["beat50"]["active"] is True and r["beat50"]["presence_stale"] is False, r["beat50"]
        assert set(r["beat"]["self_reported"]) == {"state", "note", "expires_at", "published_at"}
        assert r["sent"]["reachable"] is False and r["sent"]["ws_online"] is False
    finally:
        with _sql_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM shared_memory WHERE created_by_owner_id = %s", (owner,))
            cur.execute("DELETE FROM shared_memory_namespace WHERE owner_owner_id = %s", (owner,))
            cur.execute("DELETE FROM mesh_presence WHERE peer_id = ANY(%s)", (list(p.values()) + [stranger],))
            cur.execute("DELETE FROM dashboard_users WHERE id = %s", (owner,))
    _ = _time


def test_a_second_owner_sees_none_of_the_first_owners_peers(world):
    a_ids = {p["id"] for p in ok(world.owner_a, "/v1/dashboard/api/peers")["peers"]}
    assert str(world.peer_a) in a_ids, "POSITIVE CONTROL FAILED"
    b_ids = {p["id"] for p in ok(world.owner_b, "/v1/dashboard/api/peers")["peers"]}
    assert str(world.peer_b) in b_ids, "POSITIVE CONTROL FAILED: B cannot see its own peer"
    assert str(world.peer_a) not in b_ids


def test_namespaces_isolation(world):
    a = {n["name"] for n in ok(world.owner_a, "/v1/dashboard/api/namespaces")["namespaces"]}
    assert world.ns_a in a, "POSITIVE CONTROL FAILED"
    # A holds a live read grant on C's namespace and shares C's trust group, so the
    # GRANT ARM of `vis` must contribute it. If this fails, `vis` is owner-only and its
    # grant arm is dead code that every other assertion in this file would tolerate.
    assert world.ns_c in a, "the grant arm of vis did not contribute C's namespace to A"
    b = {n["name"] for n in ok(world.owner_b, "/v1/dashboard/api/namespaces")["namespaces"]}
    assert world.ns_b in b, "POSITIVE CONTROL FAILED"
    assert world.ns_a not in b
    assert world.ns_c not in b


def test_a_cross_tenant_grant_does_not_widen_visibility(world):
    """The tenant INTERSECT, not just the grant.

    Two live, unrevoked, unexpired grants exist for owner A: one from C (same trust
    group) and one from B (a different tenant). `vis` gates its grant arm on
    `meshy_same_tenant_or_devgroup($1, n.owner_owner_id)`, so only the first may count.
    The C grant is the positive control - without it, "B's namespace is absent" would
    also be what a `vis` with no grant arm at all produces.
    """
    # On the FIXTURE connection, not a serving one: `shared_memory_grant` is
    # server-internal RLS, so the same SELECT on a meshychat_app connection without the
    # context returns an empty set - which would make this precondition "pass" as a
    # vacuous truth in the other direction. (It genuinely did, the first time.)
    with _sql_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT namespace FROM shared_memory_grant "
            "WHERE grantee_owner_id = %s AND revoked = FALSE "
            "  AND (expires_at IS NULL OR expires_at > NOW())",
            (world.owner_a,))
        live = {row[0] for row in cur.fetchall()}
    assert {world.ns_b, world.ns_c} <= live, (
        "both grants must be LIVE for this test to mean anything; if the B grant were "
        "revoked or expired, its absence from vis would prove nothing about tenancy")

    visible = {n["name"] for n in ok(world.owner_a, "/v1/dashboard/api/namespaces")["namespaces"]}
    assert world.ns_c in visible, "POSITIVE CONTROL FAILED: the same-tenant grant was ignored too"
    assert world.ns_b not in visible, (
        "a live cross-tenant grant widened visibility: the vis grant arm is not gated on "
        "meshychat_same_tenant_or_devgroup")


def test_a_cross_tenant_grant_does_not_widen_content_visibility(world):
    """The tenant INTERSECT on the CONTENT route, `/api/memories`.

    The neighbouring test pins it on `/api/namespaces`, which reads `vis_cte()`. Memory
    bodies are read through `content_vis_cte()`, a separate CTE text with its own grant
    arm and its own `meshy_same_tenant_or_devgroup` gate, so deleting the gate from that
    one alone left every test green (review queue M01). Two live grants exist for
    owner A: one from C (same trust group), one from B (a different tenant). The C
    memory is the positive control, so an absent B memory cannot be what a grant arm
    that contributes nothing would also produce.
    """
    with _sql_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT namespace FROM shared_memory_grant "
            "WHERE grantee_owner_id = %s AND revoked = FALSE "
            "  AND (expires_at IS NULL OR expires_at > NOW())",
            (world.owner_a,))
        live = {row[0] for row in cur.fetchall()}
    assert {world.ns_b, world.ns_c} <= live, (
        "both grants must be LIVE for this test to mean anything; a revoked or expired B "
        "grant would make its memory's absence prove nothing about tenancy")

    mem_ids = {m["id"] for m in ok(world.owner_a, "/v1/dashboard/api/memories")["memories"]}
    assert str(world.mem_c) in mem_ids, (
        "POSITIVE CONTROL FAILED: the same-tenant grant contributed no memory to "
        "content_vis, so B's absence below would pass for the wrong reason")
    assert str(world.mem_b) not in mem_ids, (
        "a live cross-tenant grant exposed another tenant's memory CONTENT: the grant arm "
        "of content_vis_cte() is not gated on meshychat_same_tenant_or_devgroup")


def test_a_revoked_grant_does_not_list_an_empty_namespace_in_dev_fleet_mode(world, monkeypatch):
    """`g.revoked = FALSE` in `DEV_FLEET_VIS_CTE`, which no test ever ran (the autouse
    fixture above deletes MESHYCHAT_DEV_FLEET_MODE, so the dev-fleet CTEs were never selected;
    kxxx's mutation sample of PR #2 found the filter removable with the suite green).

    In dev-fleet mode a dev caller already sees every namespace that HAS rows, through
    the dev arm, so the grant arm only decides for a namespace with NO rows, and that is
    the only shape in which the revoked filter can be observed. Two empty namespaces owned
    by C (A's trust group): one with a live grant to A, one with a revoked one.

    Controls: dev-fleet mode is really in force (B's namespace, another trust group, is
    listed to A, which only the dev arm can do), and the LIVE-granted empty namespace is
    listed, so the grant arm contributes and the revoked one's absence is the filter.

    The same filter in `DEV_FLEET_CONTENT_VIS_CTE` is an EQUIVALENT mutant, not a gap:
    content routes read rows, every namespace with rows is already in the dev arm for a
    dev caller, and for a product-plane caller the tenant gate admits only its own
    namespaces, which the ownership arm already covers.
    """
    monkeypatch.setenv("MESHYCHAT_DEV_FLEET_MODE", "1")
    tag = uuid.uuid4().hex[:6]
    live_ns = f"test/dash-devfleet-live-{world.marker}-{tag}"
    revoked_ns = f"test/dash-devfleet-revoked-{world.marker}-{tag}"
    with _sql_conn() as conn, conn.cursor() as cur:
        for ns, revoked in ((live_ns, False), (revoked_ns, True)):
            _claim_namespace(cur, ns, world.owner_c, world.peer_c)
            cur.execute(
                "INSERT INTO shared_memory_grant (namespace, grantee_peer_id, grantee_owner_id, "
                "  permission, granted_by_peer_id, granted_by_owner_id, revoked, revoked_at) "
                "VALUES (%s, %s, %s, 'read', %s, %s, %s, CASE WHEN %s THEN NOW() END)",
                (ns, world.peer_a, world.owner_a, world.peer_c, world.owner_c, revoked, revoked))
        cur.execute("SELECT count(*) FROM shared_memory WHERE namespace IN (%s, %s)",
                    (live_ns, revoked_ns))
        assert cur.fetchone()[0] == 0, "both namespaces must be EMPTY for this test to mean anything"
    try:
        names = {n["name"] for n in
                 ok(world.owner_a, "/v1/dashboard/api/namespaces")["namespaces"]}
        assert world.ns_b in names, (
            "POSITIVE CONTROL FAILED: dev-fleet mode is not in force (another trust group's "
            "namespace is not listed to a dev caller)")
        assert live_ns in names, (
            "POSITIVE CONTROL FAILED: an empty namespace with a LIVE grant is not listed, so "
            "the grant arm of DEV_FLEET_VIS_CTE contributes nothing")
        assert revoked_ns not in names, (
            "a REVOKED grant still listed a namespace in dev-fleet mode: `g.revoked = FALSE` "
            "is not enforced in DEV_FLEET_VIS_CTE")
    finally:
        with _sql_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM shared_memory_grant WHERE namespace IN (%s, %s)",
                        (live_ns, revoked_ns))
            cur.execute("DELETE FROM shared_memory_namespace WHERE namespace IN (%s, %s)",
                        (live_ns, revoked_ns))


def test_a_revoked_grant_does_not_grant(world):
    """`AND g.revoked = FALSE`, in BOTH vis CTEs, with a revoked grant that exists.

    Deleting that clause from `DEFAULT_VIS_CTE` and `DEFAULT_CONTENT_VIS_CTE` passed the
    whole suite, and the reason is the fixture rather than the assertions: it seeds two
    grants and both are LIVE, so no test could tell a query that checks the flag from one
    that ignores it. Revocation is the one grant state a dashboard has to honour
    immediately - it is what an owner clicks when access must stop NOW.

    The grant is seeded from owner C, the SAME trust group as A, so the tenant intersect
    cannot be what excludes it; the only thing that may is `revoked`. C's live grant on
    `ns_c` is the positive control in both halves - if the grant arm contributed nothing
    at all, a revoked grant's absence would prove nothing.

    Both CTEs, because they are two separate texts: `/api/namespaces` reads `vis_cte()`
    and `/api/memories` reads `content_vis_cte()`, and a clause deleted from one is a
    leak the other's coverage hides.
    """
    revoked_ns = f"test/dash-revoked-{world.marker}-{uuid.uuid4().hex[:6]}"
    with _sql_conn() as conn, conn.cursor() as cur:
        _claim_namespace(cur, revoked_ns, world.owner_c, world.peer_c)
        revoked_mem = _mem(cur, revoked_ns, f"note/revoked-{world.marker}",
                           {"title": f"Revoked-title-{world.marker}",
                            "content": "revoked-content"},
                           world.owner_c, world.peer_c)
        cur.execute(
            "INSERT INTO shared_memory_grant (namespace, grantee_peer_id, grantee_owner_id, "
            "  permission, granted_by_peer_id, granted_by_owner_id, revoked, revoked_at) "
            "VALUES (%s, %s, %s, 'read', %s, %s, TRUE, NOW())",
            (revoked_ns, world.peer_a, world.owner_a, world.peer_c, world.owner_c))
        # The grant ROW must exist and be revoked. Without this, a fixture that failed to
        # insert would produce exactly the same "A cannot see it" answer.
        cur.execute("SELECT revoked FROM shared_memory_grant "
                    "WHERE namespace = %s AND grantee_owner_id = %s",
                    (revoked_ns, world.owner_a))
        row = cur.fetchone()
    assert row is not None and row[0] is True, \
        "the revoked grant was not seeded; this test would pass vacuously"

    try:
        # vis_cte() - the co-author-broadened CTE, via /api/namespaces.
        names = {n["name"] for n in
                 ok(world.owner_a, "/v1/dashboard/api/namespaces")["namespaces"]}
        assert world.ns_c in names, (
            "POSITIVE CONTROL FAILED: the LIVE grant arm of vis contributed nothing, so "
            "the revoked-grant assertion below would pass for the wrong reason")
        assert revoked_ns not in names, (
            "a REVOKED grant still widened vis: `AND g.revoked = FALSE` is not enforced "
            "in vis_cte()")

        # content_vis_cte() - a different CTE text, via /api/memories.
        mem_ids = {m["id"] for m in
                   ok(world.owner_a, "/v1/dashboard/api/memories")["memories"]}
        assert str(world.mem_c) in mem_ids, (
            "POSITIVE CONTROL FAILED: the LIVE grant arm of content_vis contributed "
            "nothing")
        assert str(revoked_mem) not in mem_ids, (
            "a REVOKED grant still exposed another owner's memory CONTENT: "
            "`AND g.revoked = FALSE` is not enforced in content_vis_cte()")
    finally:
        with _sql_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM shared_memory WHERE id = %s", (revoked_mem,))
            cur.execute("DELETE FROM shared_memory_grant WHERE namespace = %s", (revoked_ns,))
            cur.execute("DELETE FROM shared_memory_namespace WHERE namespace = %s",
                        (revoked_ns,))


def test_the_content_routes_run_in_dev_fleet_mode(world, monkeypatch):
    """`DEV_FLEET_CONTENT_VIS_CTE` was never executed by any test: the autouse fixture
    deletes MESHYCHAT_DEV_FLEET_MODE, so a syntax error or a missing CTE name in it would reach
    production unseen. Every content route is run in that mode here, with its behavior:
    the fixture owners are dev-plane, so A now sees B's memory (another trust group), which
    only the dev arm can produce, and A's own memory stays visible.
    """
    monkeypatch.setenv("MESHYCHAT_DEV_FLEET_MODE", "1")
    ids = {m["id"] for m in ok(world.owner_a, "/v1/dashboard/api/memories")["memories"]}
    assert str(world.mem_a) in ids, "A's own memory is missing in dev-fleet mode"
    assert str(world.mem_b) in ids, (
        "dev-fleet mode is not in force on the content route: another trust group's memory "
        "is not visible to a dev caller")
    for path in ("/v1/dashboard/api/messages", "/v1/dashboard/api/notifications",
                 "/v1/dashboard/api/memories/peers", "/v1/dashboard/api/memories/export",
                 "/v1/dashboard/api/memories/duplicates",
                 "/v1/dashboard/api/memories/search?q=title"):
        response = get(world.owner_a, path)
        assert response.status_code == 200, (path, response.status_code, response.content[:300])


#: The tables `repo.fetch_all_plain` is documented to read (repo.py, its docstring). Their
#: authorization is the route's `owner_id = ...`, not row-level security. Kept in step with
#: that docstring by hand, because the docstring is the claim this test holds it to.
PLAIN_READ_TABLES = (
    "peers", "skills", "models", "mesh_presence", "dashboard_users", "owner_sessions",
    "owner_api_keys", "dashboard_clients", "dashboard_invoices", "dashboard_invoice_items",
    "dashboard_documents", "dashboard_expenses", "dashboard_payouts",
    # Read through fetch_all_plain by the tier and entitlement routes; missing from this
    # list until kxxx's review of #12 derived the list from the call sites (below).
    "owner_subscriptions", "tier_capabilities", "owner_tier_overrides", "owner_usage",
    "accounts_entitlements",
)


def _tables_read_by_fetch_all_plain(cur) -> set:
    """Every table a `repo.fetch_all_plain` call site in views.py reads, DERIVED rather
    than listed: each call passes a `queries.*_SQL` constant, whose FROM/JOIN targets are
    kept when Postgres knows them as a table (so `EXTRACT(YEAR FROM date)` and CTE names
    drop out)."""
    import ast
    import re
    src = (Path(__file__).resolve().parent.parent / "apps/dashboard/views.py").read_text(
        encoding="utf-8")
    names = set()
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "fetch_all_plain"):
            arg = node.args[1]
            assert (isinstance(arg, ast.Attribute) and isinstance(arg.value, ast.Name)
                    and arg.value.id == "queries"), (
                f"a fetch_all_plain call does not pass a queries.* constant: {ast.dump(arg)}")
            names.add(arg.attr)
    candidates = set()
    for name in names:
        sql = getattr(queries, name)
        candidates |= {t.lower() for t in re.findall(r"\b(?:FROM|JOIN)\s+([a-z_][a-z0-9_]*)",
                                                     sql, re.I)}
    cur.execute("SELECT relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') "
                "AND c.relname = ANY(%s)", (sorted(candidates),))
    return {row[0] for row in cur.fetchall()}


def test_the_tables_read_without_a_privileged_context_carry_no_row_level_security():
    """`fetch_all_plain` reads with no RLS context, which is right only while its tables
    have no row-level security (review queue: it "has no runtime control"). The day one of
    them gains a policy, a plain read on `meshychat_app` returns `[]` for every owner, silently,
    and `/api/clients` renders an empty panel: the failure the repo module exists to stop.
    This pins the measurement the docstring states, so that day turns the suite red instead.

    `relrowsecurity` AND the policy count, because policies can sit on a table with RLS
    off (inert) and enabling it later is a one-line migration. Control: `shared_memory`,
    which this module DOES read under RLS, reports RLS on, so the query can see it.
    """
    with _sql_conn() as conn, conn.cursor() as cur:
        def state(table):
            cur.execute(
                "SELECT c.relrowsecurity, (SELECT count(*) FROM pg_policies p "
                "  WHERE p.schemaname = 'public' AND p.tablename = c.relname) "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relname = %s", (table,))
            return cur.fetchone()
        control = state("shared_memory")
        assert control is not None and control[0] is True, (
            f"POSITIVE CONTROL FAILED: shared_memory should report RLS on, got {control!r}")
        derived = _tables_read_by_fetch_all_plain(cur)
        assert len(derived) >= 10, f"control failed: only {sorted(derived)} derived"
        unlisted = derived - set(PLAIN_READ_TABLES)
        assert not unlisted, (
            f"fetch_all_plain reads {sorted(unlisted)}, which PLAIN_READ_TABLES (and the "
            "repo docstring it mirrors) does not list, so this test would not see RLS on them")
        missing = [t for t in PLAIN_READ_TABLES if state(t) is None]
        assert not missing, f"tables named in fetch_all_plain's docstring do not exist: {missing}"
        bound = {t: state(t) for t in PLAIN_READ_TABLES if state(t) != (False, 0)}
        assert not bound, (
            "a table read by fetch_all_plain now carries row-level security or a policy, so "
            f"plain reads of it would come back empty: {bound}")


def test_memories_isolation_and_shape(world):
    a = ok(world.owner_a, "/v1/dashboard/api/memories")["memories"]
    a_ids = {m["id"] for m in a}
    assert str(world.mem_a) in a_ids, "POSITIVE CONTROL FAILED"
    mine = [m for m in a if m["id"] == str(world.mem_a)][0]
    for key in ("id", "tier", "category", "title", "content", "tags", "source_peer_id",
                "confidence", "access_count", "confirmed_by", "created_at"):
        assert key in mine, f"memory projection is missing {key!r}"
    assert mine["tier"] == 1 and mine["confidence"] == "0.9"   # status CONFIRMED
    assert mine["category"] == "alpha"

    b_ids = {m["id"] for m in ok(world.owner_b, "/v1/dashboard/api/memories")["memories"]}
    assert str(world.mem_b) in b_ids, "POSITIVE CONTROL FAILED"
    assert str(world.mem_a) not in b_ids


def test_memories_content_scope_is_not_admin_broadened(world):
    """`content_vis_cte()` has NO admin arm, so an ADMIN owner in another tenant still
    sees nothing of A's memories. Control: A itself still does."""
    with _sql_conn() as conn, conn.cursor() as cur:
        admin = _insert_owner(cur, f"dash-admin-{world.marker}", is_admin=True)
    try:
        assert str(world.mem_a) in {
            m["id"] for m in ok(world.owner_a, "/v1/dashboard/api/memories")["memories"]}
        seen = {m["id"] for m in ok(admin, "/v1/dashboard/api/memories")["memories"]}
        assert str(world.mem_a) not in seen, (
            "an admin saw another tenant's memory CONTENT: the content route is using "
            "vis_cte() instead of content_vis_cte()")
    finally:
        with _sql_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM dashboard_users WHERE id = %s", (admin,))


def test_messages_isolation_and_filters(world):
    a = ok(world.owner_a, "/v1/dashboard/api/messages")["messages"]
    assert str(world.msg_a) in {m["id"] for m in a}, "POSITIVE CONTROL FAILED"
    mine = [m for m in a if m["id"] == str(world.msg_a)][0]
    for key in ("id", "from_peer_id", "to_peer_id", "to_peer_ids", "title", "subject",
                "body", "content_type", "thread_id", "reply_to_id", "read_at",
                "archived_at", "created_at"):
        assert key in mine, f"message projection is missing {key!r}"
    assert mine["body"] == f"hello-from-a-{world.marker}"

    threaded = ok(world.owner_a, "/v1/dashboard/api/messages",
                  thread_id=f"thr-{world.marker}")["messages"]
    assert [m["id"] for m in threaded] == [str(world.msg_a)]

    b_ids = {m["id"] for m in ok(world.owner_b, "/v1/dashboard/api/messages")["messages"]}
    assert str(world.msg_b) in b_ids, "POSITIVE CONTROL FAILED"
    assert str(world.msg_a) not in b_ids


def _head_counts(namespaces):
    """Ground truth for the stats assertions, counted on the fixture connection.

    Deliberately NOT a re-implementation of the visibility CTE - it takes the namespace
    list as given and counts heads in it. WHICH namespaces are visible is what
    test_namespaces_isolation and test_a_cross_tenant_grant_does_not_widen_visibility
    assert; this only checks that `stats` counts the same rows a direct count does. A
    hard-coded total would be wrong the moment the shared, globally-named
    `shared/agent-usage` namespace holds a row this fixture did not write.
    """
    with _sql_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FILTER (WHERE is_head), "
                    "       COUNT(*) FILTER (WHERE is_head AND key LIKE 'msg/%%') "
                    "FROM shared_memory WHERE namespace = ANY(%s)", (list(namespaces),))
        memories, messages = cur.fetchone()
    return memories, messages


def test_stats_counts_the_visible_scope(world):
    a = ok(world.owner_a, "/v1/dashboard/api/stats")
    assert set(a) == {"peers", "memories", "messages", "models", "tools"}
    b = ok(world.owner_b, "/v1/dashboard/api/stats")

    a_names = {n["name"] for n in ok(world.owner_a, "/v1/dashboard/api/namespaces")["namespaces"]}
    b_names = {n["name"] for n in ok(world.owner_b, "/v1/dashboard/api/namespaces")["namespaces"]}
    assert world.ns_a in a_names and world.ns_c in a_names, "POSITIVE CONTROL FAILED"
    assert world.ns_a not in b_names and world.ns_c not in b_names

    a_mem, a_msg = _head_counts(a_names)
    b_mem, b_msg = _head_counts(b_names)
    assert a["memories"] == a_mem, (a, a_names)
    assert a["messages"] == a_msg, (a, a_names)
    assert b["memories"] == b_mem, (b, b_names)
    assert b["messages"] == b_msg, (b, b_names)
    assert a_mem > 0, "POSITIVE CONTROL FAILED: the visible namespaces hold no heads at all"

    # `peers` counts A itself plus the co-author C reached through vis_peers; B sees only
    # itself. A count of 1 for A would mean vis_peers contributed nothing and every
    # co-authorship assertion in this file would be vacuous.
    assert a["peers"] == 2, a
    assert b["peers"] == 1, b


def test_models_isolation_and_port_redaction(world):
    a = ok(world.owner_a, "/v1/dashboard/api/models")["models"]
    mine = [m for m in a if m["id"] == str(world.model_a)]
    assert mine, "POSITIVE CONTROL FAILED: owner A cannot see its own model"
    assert mine[0]["port"] == 11434, "the owner of the serving peer must see the port"
    for key in ("id", "name", "provider", "peer_id", "peer_name", "capabilities",
                "context_window", "cost_per_1k_input", "cost_per_1k_output", "vram_gb",
                "port", "runtime_profile", "runtime_status", "health", "created_at",
                "updated_at"):
        assert key in mine[0], f"model projection is missing {key!r}"

    # C's model reaches A through vis_peers (A holds a grant on C's namespace and C's
    # peer co-authored it), and its PORT must be redacted because A does not own it.
    foreign = [m for m in a if m["id"] == str(world.model_c)]
    assert foreign, ("vis_peers did not surface the co-author's model; without this the "
                     "redaction assertion below would be vacuous")
    assert foreign[0]["port"] is None, "a foreign peer's node-local port was disclosed"
    # ... and B, in another tenant entirely, is not in A's roster at all.
    assert str(world.model_b) not in {m["id"] for m in a}

    b_ids = {m["id"] for m in ok(world.owner_b, "/v1/dashboard/api/models")["models"]}
    assert str(world.model_b) in b_ids, "POSITIVE CONTROL FAILED"
    assert str(world.model_a) not in b_ids


def test_tools_isolation_and_call_stats(world):
    a = ok(world.owner_a, "/v1/dashboard/api/tools")["tools"]
    mine = [t for t in a if t["id"] == str(world.skill_a)]
    assert mine, "POSITIVE CONTROL FAILED"
    for key in ("id", "name", "description", "version", "peer_id", "peer_name",
                "category", "mode", "share_mode", "input_schema", "runtime_profile",
                "runtime_status", "total_calls", "success_rate", "avg_latency_ms",
                "created_at", "updated_at"):
        assert key in mine[0], f"tool projection is missing {key!r}"
    # A skill with no calls must report 0/0/0, not null, through the LEFT JOIN + COALESCE.
    assert mine[0]["total_calls"] == 0
    assert mine[0]["success_rate"] == 0
    assert mine[0]["avg_latency_ms"] == 0
    assert mine[0]["category"] == "cat-a"

    b_ids = {t["id"] for t in ok(world.owner_b, "/v1/dashboard/api/tools")["tools"]}
    assert str(world.skill_b) in b_ids, "POSITIVE CONTROL FAILED"
    assert str(world.skill_a) not in b_ids


def test_tool_call_stats_match_the_rows(world):
    """The rewritten aggregate, checked against real `skill_calls` rows rather than
    against itself: two completed calls and one failed give total 3 and 67%."""
    made = []
    with _sql_conn() as conn, conn.cursor() as cur:
        for i, (status, secs) in enumerate([("completed", 2), ("completed", 4),
                                            ("failed", None)]):
            call = uuid.uuid4()
            made.append(call)
            cur.execute(
                "INSERT INTO skill_calls (id, call_id, caller_peer_id, caller_owner_id, "
                "  target_peer_id, target_owner_id, skill_id, skill_name, skill_version, "
                # args and deadline_at are both NOT NULL on this table - read off
                # information_schema, one NotNullViolation at a time.
                "  mode, transport, status, args, deadline_at, started_at, completed_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, 'skill', '1.0.0', 'sync', 'http', %s, "
                "        '{}'::jsonb, NOW() + INTERVAL '1 hour', "
                "        NOW(), CASE WHEN %s::int IS NULL THEN NULL "
                "                    ELSE NOW() + make_interval(secs => %s::int) END)",
                (call, f"call-{call}", world.peer_a, world.owner_a, world.peer_a,
                 world.owner_a, world.skill_a, status, secs, secs))
    try:
        tools = ok(world.owner_a, "/v1/dashboard/api/tools")["tools"]
        mine = [t for t in tools if t["id"] == str(world.skill_a)][0]
        assert mine["total_calls"] == 3
        assert mine["success_rate"] == 67          # ROUND(100*2/3)
        assert mine["avg_latency_ms"] == 3000      # AVG(2s, 4s)
    finally:
        with _sql_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM skill_calls WHERE id = ANY(%s)", (made,))


def test_usage_is_tenant_scoped_by_default(world):
    """THE RECORDED LEAK. `/v1/dashboard/api/usage` once defaulted to network-global
    under an RLS-bypassing connection and exposed every owner's spend.

    Owner A reports 12.5 USD, owner B reports 99.0. Each must see its own figure and
    NOT the other's, and the network aggregate must not be the sum.
    """
    a = ok(world.owner_a, "/v1/dashboard/api/usage")
    assert set(a) == {"network", "peers"}
    a_peers = {p["peer_id"]: p for p in a["peers"]}
    assert str(world.peer_a) in a_peers, "POSITIVE CONTROL FAILED: A cannot see its own usage"
    assert str(world.peer_b) not in a_peers, "owner A read owner B's spend"
    assert float(a["network"]["est_cost_usd"]) == 12.5

    b = ok(world.owner_b, "/v1/dashboard/api/usage")
    b_peers = {p["peer_id"]: p for p in b["peers"]}
    assert str(world.peer_b) in b_peers, "POSITIVE CONTROL FAILED"
    assert str(world.peer_a) not in b_peers, "owner B read owner A's spend"
    assert float(b["network"]["est_cost_usd"]) == 99.0


def test_usage_global_mode_is_opt_in_and_off_by_default(monkeypatch, world):
    """The flag exists and works - which is what makes "off by default" a real default
    rather than an unreachable branch. Scoped first (the control), then global."""
    scoped = ok(world.owner_b, "/v1/dashboard/api/usage")
    assert str(world.peer_a) not in {p["peer_id"] for p in scoped["peers"]}
    monkeypatch.setenv("MESHYCHAT_ALLOW_GLOBAL_USAGE", "1")
    globalised = ok(world.owner_b, "/v1/dashboard/api/usage")
    assert str(world.peer_a) in {p["peer_id"] for p in globalised["peers"]}, (
        "the opt-in flag did not widen the scope, so 'tenant-scoped by default' is "
        "untested - the query may simply be tenant-scoped unconditionally")


def test_the_unread_count_drops_when_a_notification_is_read(world):
    """M24: `unread_only` adds `AND ns.read_at IS NULL` to the count's query; removing it
    left every test green, because nothing ever read a notification. Marking A's
    notification read must take it out of the count and leave it in the list, now
    carrying its `read_at`."""
    before = ok(world.owner_a, "/v1/dashboard/api/notifications/count")["unread"]
    assert before >= 1, "POSITIVE CONTROL FAILED: A has no unread notification to read"
    with _sql_conn() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO dashboard_notification_state (owner_id, message_id, read_at) "
                    "VALUES (%s, %s, NOW())", (world.owner_a, world.notif))
    try:
        after = ok(world.owner_a, "/v1/dashboard/api/notifications/count")["unread"]
        assert after == before - 1, (
            f"reading a notification did not lower the unread count ({before} to {after}): "
            "`ns.read_at IS NULL` is not applied to the count")
        listed = {n["id"]: n for n in
                  ok(world.owner_a, "/v1/dashboard/api/notifications")["notifications"]}
        assert str(world.notif) in listed, "a READ notification vanished from the full list"
        assert listed[str(world.notif)]["read_at"] is not None
    finally:
        with _sql_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM dashboard_notification_state "
                        "WHERE owner_id = %s AND message_id = %s", (world.owner_a, world.notif))


def test_a_log_search_matches_percent_and_underscore_literally(world):
    """M25: `_escape_like` makes `%` and `_` in `q` literal under `ESCAPE '\'`; removing it
    left every test green. Two lines differ only where the query has a wildcard
    character: without the escaping, `50%_off` would also match `50XYoff`."""
    literal, decoy = f"price 50%_off {world.marker}", f"price 50XYoff {world.marker}"
    # One decoy per wildcard, so escaping only `%` or only `_` is caught too (kxxx's
    # review of #14): `50%Yoff` differs only where `_` is, `50X_off` only where `%` is.
    only_underscore, only_percent = f"price 50%Yoff {world.marker}", f"price 50X_off {world.marker}"
    decoys = (decoy, only_underscore, only_percent)
    with _sql_conn() as conn, conn.cursor() as cur:
        for message in (literal,) + decoys:
            cur.execute("INSERT INTO dashboard_node_logs (owner_id, peer_id, level, source, "
                        "message) VALUES (%s, %s, 'info', 'fixture', %s)",
                        (world.owner_a, world.peer_a, message))
    try:
        found = [r["message"] for r in
                 ok(world.owner_a, "/v1/dashboard/api/logs", q="50%_off")["data"]]
        assert literal in found, "POSITIVE CONTROL FAILED: the literal line was not found"
        for d in decoys:
            assert d not in found, (
                f"{d!r} matched `50%_off`: a wildcard in the search was not escaped")
    finally:
        with _sql_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM dashboard_node_logs WHERE message = ANY(%s)",
                        ([literal, *decoys],))


def test_an_internal_error_carries_the_constant_message_not_its_detail(world, monkeypatch):
    """M26: `errors.internal` keeps its detail for the log and puts only the constant
    "internal error" on the wire; the detail is typically
    SQL that names tables. Replacing the constant with the detail left every test green.
    Control: the same route answers 200 before the failure is injected."""
    assert get(world.owner_a, "/v1/dashboard/api/namespaces").status_code == 200

    def failing(*_args, **_kwargs):
        raise errors.internal("SELECT secret_column FROM some_internal_table")

    monkeypatch.setattr(repo, "fetch_all_internal", failing)
    response = get(world.owner_a, "/v1/dashboard/api/namespaces")
    assert response.status_code == 500, response.content[:200]
    text = response.content.decode("utf-8")
    assert "internal error" in text, text
    assert "secret_column" not in text and "some_internal_table" not in text, (
        f"an internal error's detail reached the wire: {text}")


def test_notifications_and_count(world):
    a = ok(world.owner_a, "/v1/dashboard/api/notifications")["notifications"]
    ids = {n["id"] for n in a}
    assert str(world.notif) in ids, (
        "POSITIVE CONTROL FAILED: the requires-ack message addressed to A's peer, "
        "authored by B in a namespace A may read, is not in A's notifications")
    row = [n for n in a if n["id"] == str(world.notif)][0]
    for key in ("id", "message_id", "namespace", "key", "type", "from", "to", "body",
                "thread_id", "requires_ack", "read_at", "created_at"):
        assert key in row, f"notification projection is missing {key!r}"
    # Authored by the CALLER -> excluded (`created_by_owner_id <> $1`).
    assert not any(n["key"].endswith(f"a-self-ack-{world.marker}") for n in a), \
        "a caller's own requires-ack message was returned as a notification to itself"

    count = ok(world.owner_a, "/v1/dashboard/api/notifications/count")
    assert set(count) == {"unread"}
    assert count["unread"] >= 1

    # Owner B is in another tenant and cannot see C's namespace at all.
    b = ok(world.owner_b, "/v1/dashboard/api/notifications")["notifications"]
    assert str(world.notif) not in {n["id"] for n in b}
    # Owner C AUTHORED it, so C must not be notified by its own message even though the
    # row is in C's own namespace - the `created_by_owner_id <> $1` arm.
    c = ok(world.owner_c, "/v1/dashboard/api/notifications")["notifications"]
    assert str(world.notif) not in {n["id"] for n in c}, (
        "the author was notified of its own requires-ack message")


def test_memory_explorer_routes(world):
    peers_payload = ok(world.owner_a, "/v1/dashboard/api/memories/peers")
    assert str(world.peer_a) in {p["id"] for p in peers_payload["peers"]}, \
        "POSITIVE CONTROL FAILED"

    found = ok(world.owner_a, "/v1/dashboard/api/memories/search", q="a-content")
    assert set(found) == {"data", "search_time_ms"}
    assert str(world.mem_a) in {m["id"] for m in found["data"]}, "POSITIVE CONTROL FAILED"
    assert isinstance(found["search_time_ms"], int)
    missing = ok(world.owner_a, "/v1/dashboard/api/memories/search",
                 q=f"no-such-term-{uuid.uuid4().hex}")
    assert missing["data"] == [], "the search matched a term that appears nowhere"

    export = ok(world.owner_a, "/v1/dashboard/api/memories/export")
    assert isinstance(export, list), "export must be a BARE array, not a wrapper object"
    assert str(world.mem_a) in {m["id"] for m in export}

    dups = ok(world.owner_a, "/v1/dashboard/api/memories/duplicates")["data"]
    titles = [d["title"] for d in dups]
    assert titles.count(f"A-title-{world.marker}") == 2, \
        "the two rows sharing a normalized title were not reported as duplicates"

    # ... and none of it reaches owner B.
    b_export = ok(world.owner_b, "/v1/dashboard/api/memories/export")
    assert str(world.mem_a) not in {m["id"] for m in b_export}


def test_the_memory_explorer_queries_are_namespace_scoped(world):
    """THREE QUERIES, ONE PREDICATE, AND NOTHING ASSERTED IT.

    `AND sm.namespace IN (SELECT namespace FROM vis)` is the whole tenancy boundary of
    memory SEARCH (`MEMORY_SEARCH_SQL`), DUPLICATES (`MEMORY_DUPLICATES_SQL`) and the
    authoring-peer roster (`MEMORY_PEERS_SQL`). Deleting it from any of the three left
    the suite green while the route returned another tenant's memory BODIES - the export
    route was covered and these three were not, which is precisely the "right in seven
    places, wrong in the eighth" failure `tenant_scope.py`'s header warns about.

    Owner B is in its own trust group, so nothing of B's is reachable by A through the
    grant arm either. Each half below asserts B's OWN route finds the row first: an
    empty `data` is also what a mis-seeded fixture, an unbound RLS context or a
    swallowed 500 produces, so "A did not see it" means nothing until B has seen it.
    """
    # A second B-owned row sharing mem_b's normalized title, so B has a DUPLICATE PAIR
    # for the duplicates query to find. Without it, dropping the namespace predicate
    # would leak nothing from B through that route and the test would pass vacuously.
    #
    # And a SECOND B-owned peer whose ONLY authorship is inside ns_b. `world.peer_b`
    # itself is the wrong probe for the roster: both fixture owners hold a write grant on
    # the GLOBAL `shared/agent-usage` namespace, peer_b authored a row there, and that
    # namespace really is in owner A's `vis` - so peer_b appears in A's roster
    # LEGITIMATELY, through a namespace A may read. The roster includes peers that authored
    # accessible rows, so that is not a leak, and asserting against peer_b would be a false
    # positive.
    dup_key = f"note/b-dup-{uuid.uuid4().hex[:8]}"
    with _sql_conn() as conn, conn.cursor() as cur:
        b_dup = _mem(cur, world.ns_b, dup_key,
                     {"title": f"B-title-{world.marker}", "content": "b-content-dup"},
                     world.owner_b, world.peer_b, status="OPEN", tags=["beta"])
        peer_b2 = _insert_peer(cur, world.owner_b, f"peer-b2-{world.marker}",
                               f"Peer B2 {world.marker}")
        mem_b2 = _mem(cur, world.ns_b, f"note/b2-{uuid.uuid4().hex[:8]}",
                      {"title": f"B2-title-{world.marker}", "content": "b2-content"},
                      world.owner_b, peer_b2, status="OPEN")
    try:
        # --- SEARCH ---------------------------------------------------------
        b_hits = ok(world.owner_b, "/v1/dashboard/api/memories/search", q="b-content")
        assert str(world.mem_b) in {m["id"] for m in b_hits["data"]}, (
            "POSITIVE CONTROL FAILED: owner B cannot find its own memory by content, so "
            "the leak assertion below would pass against a broken query")
        a_hits = ok(world.owner_a, "/v1/dashboard/api/memories/search", q="b-content")
        leaked = {m["id"] for m in a_hits["data"]} & {str(world.mem_b), str(b_dup)}
        assert not leaked, (
            f"memory search returned owner B's rows to owner A: {leaked}")
        # A's own content still matches - the filter did not simply stop returning rows.
        assert str(world.mem_a) in {
            m["id"] for m in ok(world.owner_a, "/v1/dashboard/api/memories/search",
                                q="a-content")["data"]}, "POSITIVE CONTROL FAILED"

        # --- DUPLICATES -----------------------------------------------------
        b_dups = ok(world.owner_b, "/v1/dashboard/api/memories/duplicates")["data"]
        assert [d["title"] for d in b_dups].count(f"B-title-{world.marker}") == 2, (
            "POSITIVE CONTROL FAILED: owner B's own duplicate pair was not reported, so "
            "its absence from owner A's answer would prove nothing")
        a_dups = ok(world.owner_a, "/v1/dashboard/api/memories/duplicates")["data"]
        assert f"B-title-{world.marker}" not in {d["title"] for d in a_dups}, \
            "memory duplicates reported owner B's titles to owner A"
        assert f"A-title-{world.marker}" in {d["title"] for d in a_dups}, \
            "POSITIVE CONTROL FAILED: owner A's own duplicate pair vanished"

        # --- AUTHORING-PEER ROSTER ------------------------------------------
        b_roster = {p["id"] for p in
                    ok(world.owner_b, "/v1/dashboard/api/memories/peers")["peers"]}
        assert str(peer_b2) in b_roster, (
            "POSITIVE CONTROL FAILED: owner B's own roster is missing the peer that "
            "authored a row in B's namespace")
        a_roster = {p["id"] for p in
                    ok(world.owner_a, "/v1/dashboard/api/memories/peers")["peers"]}
        assert str(world.peer_a) in a_roster, "POSITIVE CONTROL FAILED"
        assert str(peer_b2) not in a_roster, (
            "the memory authoring-peer roster listed a peer whose only authorship is "
            "inside another tenant's namespace")
    finally:
        with _sql_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM shared_memory WHERE id IN (%s, %s)", (b_dup, mem_b2))
            cur.execute("DELETE FROM peers WHERE id = %s", (peer_b2,))


def test_logs_are_owner_scoped(world):
    a = ok(world.owner_a, "/v1/dashboard/api/logs")
    assert set(a) == {"data", "retention_days"}
    messages = [row["message"] for row in a["data"]]
    assert f"log-line-a-{world.marker}" in messages, "POSITIVE CONTROL FAILED"
    assert f"log-line-b-{world.marker}" not in messages
    assert a["retention_days"] == 7

    filtered = ok(world.owner_a, "/v1/dashboard/api/logs", level="error")["data"]
    assert f"log-line-a-{world.marker}" not in [r["message"] for r in filtered], \
        "the level filter did not apply"
    kept = ok(world.owner_a, "/v1/dashboard/api/logs", level="info")["data"]
    assert f"log-line-a-{world.marker}" in [r["message"] for r in kept], \
        "POSITIVE CONTROL FAILED: the level filter dropped a matching row"

    b = ok(world.owner_b, "/v1/dashboard/api/logs")["data"]
    assert f"log-line-b-{world.marker}" in [r["message"] for r in b], "POSITIVE CONTROL FAILED"
    assert f"log-line-a-{world.marker}" not in [r["message"] for r in b]


def test_the_lazy_log_expiry_deletes_only_the_reading_owners_rows(world):
    """THE ONLY DESTRUCTIVE STATEMENT ON THIS SURFACE, AND IT RUNS ON A **GET**.

    `GET /api/logs` enforces retention lazily: it DELETEs the
    caller's expired rows inside the same privileged transaction as the read, so there
    is no sweeper. `LOGS_EXPIRE_SQL` runs under `server_internal`, which bypasses RLS,
    so its `WHERE owner_id = %(owner)s` is the ONLY thing standing between one owner's
    dashboard refresh and every other owner's log history. Replacing that clause with a
    tautology left the whole suite green.

    The positive control here is not decoration and it is the harder half: if the DELETE
    never ran at all, "owner B's row survived" would be just as true. So this asserts,
    in order, that (a) both expired rows exist, (b) A's own expired row is GONE after
    A's read - the statement really executed - and (c) B's equally expired row, which
    only B's own read may remove, is untouched.
    """
    old = "NOW() - INTERVAL '90 days'"     # far past the 7-day free-tier window
    with _sql_conn() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO dashboard_node_logs (owner_id, peer_id, level, source, "
                    f"  message, created_at) VALUES (%s, %s, 'info', 'fixture', %s, {old}) "
                    "RETURNING id",
                    (world.owner_a, world.peer_a, f"expired-a-{world.marker}"))
        expired_a = cur.fetchone()[0]
        cur.execute("INSERT INTO dashboard_node_logs (owner_id, peer_id, level, source, "
                    f"  message, created_at) VALUES (%s, %s, 'info', 'fixture', %s, {old}) "
                    "RETURNING id",
                    (world.owner_b, world.peer_b, f"expired-b-{world.marker}"))
        expired_b = cur.fetchone()[0]

    def _alive(row_id) -> bool:
        with _sql_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM dashboard_node_logs WHERE id = %s", (row_id,))
            return cur.fetchone() is not None

    try:
        assert _alive(expired_a) and _alive(expired_b), "fixture rows were not written"

        ok(world.owner_a, "/v1/dashboard/api/logs")

        assert not _alive(expired_a), (
            "POSITIVE CONTROL FAILED: the lazy expiry did not delete the READING "
            "owner's expired row, so this test cannot tell a scoped DELETE from a "
            "DELETE that never ran")
        assert _alive(expired_b), (
            "owner A's log READ deleted owner B's expired log row - the expiry DELETE "
            "runs under server_internal and is no longer scoped to the caller")
    finally:
        with _sql_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM dashboard_node_logs WHERE id IN (%s, %s)",
                        (expired_a, expired_b))


def test_log_settings_reports_the_tier_ceiling(world):
    payload = ok(world.owner_a, "/v1/dashboard/api/logs/settings")
    assert set(payload) == {"retention_days", "max_retention_days"}
    # tier 'free' -> log_retention_days_max = 7 (measured in tier_capabilities).
    assert payload["max_retention_days"] == 7
    assert payload["retention_days"] == 7


def test_log_settings_reads_the_ceiling_from_the_owners_tier(world):
    """The test above cannot tell reading `tier_capabilities` from falling through: for the
    free tier the ceiling (7), the unknown-capability default (7) and the stored default
    (7) are all the same number (review queue). A pro-tier owner's ceiling is 30
    (sql/097), which only the capability read can produce; retention stays 7, the stored
    default, because the report is min(stored, ceiling)."""
    with _sql_conn() as conn, conn.cursor() as cur:
        pro = _insert_owner(cur, f"dash-pro-{world.marker}", tier="pro")
    try:
        payload = ok(pro, "/v1/dashboard/api/logs/settings")
        assert payload["max_retention_days"] == 30, (
            f"a pro owner's ceiling is not its tier's: {payload}")
        assert payload["retention_days"] == 7, payload
    finally:
        with _sql_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM dashboard_users WHERE id = %s", (pro,))


def test_tier_info_is_owner_scoped(world):
    payload = ok(world.owner_a, "/v1/dashboard/api/tier-info")
    assert set(payload) == {"effective_tier", "linked_agents", "max_linked_agents",
                            "capabilities"}
    assert payload["effective_tier"] == "free"
    assert payload["linked_agents"] == 1, \
        "linked_agents counted peers outside the caller's own owner_id"
    assert payload["max_linked_agents"] == 5          # free tier peers_per_owner
    caps = payload["capabilities"]
    assert "peers_per_owner" in caps
    assert set(caps["peers_per_owner"]) == {"value", "current", "percent"}


def test_the_entitlements_gate_fails_closed_with_402(world):
    """Three states, all three asserted, because they are three different rows and only
    the middle one is obvious:
      no row at all -> `NoRecord`, ALLOWED (an owner predating the Accounts cutover)
      active=false  -> 402 E_QUOTA_EXCEEDED
      recheck_at passed -> 402 as well, EVEN IF active=true. Collapsing that case into
                       the inactive one would let an expired cached verdict keep
                       granting access, which is the inversion the gate exists to stop.

    E_QUOTA_EXCEEDED and 402, not a bespoke code and not 401: 401 would log the user out of a
    session that is perfectly valid.
    """
    allowed = ok(world.owner_a, "/v1/dashboard/api/tier-info")   # POSITIVE CONTROL: no row
    assert allowed["effective_tier"] == "free"

    def _set(active, recheck_sql):
        with _sql_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO accounts_entitlements (owner_id, issuer, subject, active, "
                f"  recheck_at) VALUES (%s, 'test-issuer', %s, %s, {recheck_sql}) "
                "ON CONFLICT (owner_id) DO UPDATE SET active = EXCLUDED.active, "
                "  recheck_at = EXCLUDED.recheck_at",
                (world.owner_a, str(world.owner_a), active))

    try:
        _set(False, "NOW() + INTERVAL '1 hour'")
        denied = get(world.owner_a, "/v1/dashboard/api/tier-info")
        assert denied.status_code == 402, denied.content[:300]
        assert body(denied)["error"] == "E_QUOTA_EXCEEDED"

        _set(True, "NOW() - INTERVAL '1 hour'")
        stale = get(world.owner_a, "/v1/dashboard/api/tier-info")
        assert stale.status_code == 402, (
            "an ACTIVE but stale verdict was served: the gate collapsed the stale case "
            "into the inactive one and stopped failing closed")
        assert body(stale)["error"] == "E_QUOTA_EXCEEDED"

        _set(True, "NOW() + INTERVAL '1 hour'")
        assert get(world.owner_a, "/v1/dashboard/api/tier-info").status_code == 200,             "POSITIVE CONTROL FAILED: an active, fresh entitlement was refused"
    finally:
        with _sql_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM accounts_entitlements WHERE owner_id = %s",
                        (world.owner_a,))


def test_the_one_gate_reads_its_row_under_any_row_factory(world):
    """kxxx's review of #648, P3-2. `verdict` read its row by position from `conn.cursor()`, so it was right only
    while the caller's connection defaulted to tuple rows. On a connection that defaults to dict rows it raised
    KeyError, which no caller translates (they catch psycopg.Error). It names its own row factory now."""
    from psycopg.rows import dict_row
    from apps.core import entitlements as core_entitlements

    def _set(active, recheck_sql):
        with _sql_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO accounts_entitlements (owner_id, issuer, subject, active, "
                f"  recheck_at) VALUES (%s, 'test-issuer', %s, %s, {recheck_sql}) "
                "ON CONFLICT (owner_id) DO UPDATE SET active = EXCLUDED.active, "
                "  recheck_at = EXCLUDED.recheck_at",
                (world.owner_a, str(world.owner_a), active))

    try:
        _set(True, "NOW() + INTERVAL '1 hour'")
        with _sql_conn() as conn:
            assert core_entitlements.verdict(conn, world.owner_a) == (True, "active"), "CONTROL: the tuple default"
            conn.row_factory = dict_row
            assert conn.execute("SELECT 1 AS one").fetchone() == {"one": 1}, "CONTROL: dict rows are this connection's default"
            assert core_entitlements.verdict(conn, world.owner_a) == (True, "active")
        _set(False, "NOW() + INTERVAL '1 hour'")
        with _sql_conn() as conn:
            conn.row_factory = dict_row
            assert core_entitlements.verdict(conn, world.owner_a) == (False, "product access denied")
        _set(True, "NOW() - INTERVAL '1 hour'")
        with _sql_conn() as conn:
            conn.row_factory = dict_row
            assert core_entitlements.verdict(conn, world.owner_a) == (False, "entitlement verdict is stale")
            assert core_entitlements.verdict(conn, world.owner_b)[0] is True, "no row: an owner predating the cutover"
    finally:
        with _sql_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM accounts_entitlements WHERE owner_id = %s",
                        (world.owner_a,))


def test_the_dashboard_asks_the_one_entitlements_gate(world, monkeypatch):
    """REFACTOR-QUEUE-2026-11 B2. The dashboard kept its own copy of `apps/core/entitlements`' SQL and order; it now
    calls `entitlements.require`, the gate `apps/files/quota` calls, and only translates the refusal. Proved by
    answering through core alone: with no entitlements row, a `verdict` patched to say stale or denied must refuse,
    which the copy (reading the table itself) never did. The test above holds the three real states either way."""
    from apps.core import entitlements as core_entitlements
    assert ok(world.owner_a, "/v1/dashboard/api/tier-info")["effective_tier"] == "free"   # control: no row, allowed
    for reason, message in (("entitlement verdict is stale", "stale"),
                            ("product access denied", "denied")):
        monkeypatch.setattr(core_entitlements, "verdict", lambda conn, owner_id, r=reason: (False, r))
        refused = get(world.owner_a, "/v1/dashboard/api/tier-info")
        assert refused.status_code == 402, (reason, refused.content[:300])
        assert body(refused)["error"] == "E_QUOTA_EXCEEDED"
        assert message in body(refused)["message"], (reason, body(refused))

    def broken(conn, owner_id):
        raise psycopg.OperationalError("synthetic driver failure")
    monkeypatch.setattr(core_entitlements, "verdict", broken)
    failed = get(world.owner_a, "/v1/dashboard/api/tier-info")
    assert failed.status_code == 500, failed.content[:300]
    assert "synthetic driver failure" not in failed.content.decode("utf-8", "replace"), \
        "a driver error's text reached the wire"


def test_the_entitlements_question_is_spelled_once():
    """B2's census: one assignment of ENTITLEMENTS_SQL under apps/, in apps/core/entitlements.py."""
    import ast
    root = Path(__file__).resolve().parent.parent / "apps"
    found = []
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "ENTITLEMENTS_SQL" for t in node.targets):
                found.append(path.relative_to(root).as_posix())
    assert found == ["core/entitlements.py"], found


#: Every mounted route whose answer is decided by a capability cap. `tier-info` resolves
#: `peers_per_owner`; `logs/settings` resolves `log_retention_days_max`. Both go through
#: `views._resolve_cap`, which is where the gate lives.
CAPABILITY_ROUTES = (
    "/v1/dashboard/api/tier-info",
    "/v1/dashboard/api/logs/settings",
)


def test_the_entitlements_gate_holds_at_every_capability_route(world):
    """THE GATE BELONGS AT THE CHOKEPOINT, NOT ON ONE ROUTE.

    The gate belongs in `views._resolve_cap`: a gate added per-route can be omitted. A per-route
    helper was called from `tier_info` only, while `_max_retention_days` resolved a cap in raw
    SQL with no gate at all. MEASURED on a revoked owner in the same instant, before the fix:
    tier-info 402, `/logs/settings` **200**.

    So this test is deliberately a LOOP over every route that resolves a cap rather than
    a second copy of the tier-info assertion: the defect was not that one check was
    wrong, it was that one call site was missing, and only a test that enumerates the
    call sites can catch the next one.

    The positive control is the SAME loop with an active, fresh entitlement, because a
    402 is also what a broken fixture, a mis-seeded owner or a 500-turned-402 produces.
    """
    def _set_active(active: bool):
        with _sql_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO accounts_entitlements (owner_id, issuer, subject, active, "
                "  recheck_at) VALUES (%s, 'test-issuer', %s, %s, NOW() + INTERVAL '1 hour') "
                "ON CONFLICT (owner_id) DO UPDATE SET active = EXCLUDED.active, "
                "  recheck_at = EXCLUDED.recheck_at",
                (world.owner_a, str(world.owner_a), active))

    try:
        # POSITIVE CONTROL, asserted first: an entitled owner is served by both.
        _set_active(True)
        for path in CAPABILITY_ROUTES:
            response = get(world.owner_a, path)
            assert response.status_code == 200, (
                f"POSITIVE CONTROL FAILED: {path} refused an owner whose entitlement is "
                f"active and fresh ({response.status_code}, {response.content[:200]!r})")

        _set_active(False)
        for path in CAPABILITY_ROUTES:
            response = get(world.owner_a, path)
            assert response.status_code == 402, (
                f"{path} answered {response.status_code} for an owner Accounts has "
                f"revoked; the entitlements gate does not cover this call site")
            assert body(response)["error"] == "E_QUOTA_EXCEEDED", body(response)
    finally:
        with _sql_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM accounts_entitlements WHERE owner_id = %s",
                        (world.owner_a,))


def test_agents_isolation(world):
    a = ok(world.owner_a, "/v1/dashboard/agents")["agents"]
    assert str(world.peer_a) in {x["id"] for x in a}, "POSITIVE CONTROL FAILED"
    for key in ("id", "peer_id", "name", "display_name", "tier", "status", "linked_at"):
        assert key in a[0], f"agent projection is missing {key!r}"
    b_ids = {x["id"] for x in ok(world.owner_b, "/v1/dashboard/agents")["agents"]}
    assert str(world.peer_b) in b_ids, "POSITIVE CONTROL FAILED"
    assert str(world.peer_a) not in b_ids


def test_bridges_require_both_endpoints_visible(world):
    """The bridge spans ns_a -> ns_c. A sees both endpoints; C sees only one (it owns
    ns_c but holds no grant on ns_a), and B sees neither."""
    a = ok(world.owner_a, "/v1/dashboard/api/bridges")["data"]
    assert any(x["source_namespace_id"] == world.ns_a for x in a), \
        "POSITIVE CONTROL FAILED: A can see both endpoints and still got no bridge"
    c = ok(world.owner_c, "/v1/dashboard/api/bridges")["data"]
    assert not any(x["source_namespace_id"] == world.ns_a for x in c), \
        "a bridge was listed to an owner who can see only ONE of its two endpoints"
    b = ok(world.owner_b, "/v1/dashboard/api/bridges")["data"]
    assert not any(x["source_namespace_id"] == world.ns_a for x in b), \
        "a bridge was listed to an owner who cannot see its source namespace"


def test_a_bridge_is_hidden_when_only_its_target_is_invisible(world):
    """`BRIDGES_SQL` gates on the SOURCE and the TARGET; deleting the target predicate
    left every test green (review queue M31).

    The neighbouring test cannot see it: C is excluded there because the SOURCE ns_a is
    invisible to C, which a source-only predicate also does. This one builds the other
    shape, a bridge from A's own namespace (visible to A) to B's namespace, which A can
    NOT see even though B granted A access, because that grant is cross-tenant. So the
    source is visible, the target is not, and only the target predicate can exclude it.
    The ns_a to ns_c bridge is the positive control: A sees both of its endpoints.
    """
    bridge_id = uuid.uuid4()
    with _sql_conn() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO dashboard_memory_bridges (id, source_namespace_id, "
                    "  target_namespace_id, bridge_type, created_by_owner_id, created_by_peer_id) "
                    "VALUES (%s, %s, %s, 'read', %s, %s)",
                    (bridge_id, world.ns_a, world.ns_b, world.owner_a, world.peer_a))
    try:
        seen = ok(world.owner_a, "/v1/dashboard/api/bridges")["data"]
        assert any(x["target_namespace_id"] == world.ns_c for x in seen), (
            "POSITIVE CONTROL FAILED: A can see both endpoints of the ns_a to ns_c bridge "
            "and still got no bridge, so the absence below would prove nothing")
        assert str(bridge_id) not in {str(x["id"]) for x in seen}, (
            "a bridge was listed to an owner who can see its SOURCE but not its TARGET: "
            "the target_namespace_id predicate of BRIDGES_SQL is not enforced")
    finally:
        with _sql_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM dashboard_memory_bridges WHERE id = %s", (bridge_id,))


def test_dispatch_nodes(world):
    payload = ok(world.owner_a, "/v1/dashboard/api/dispatch/nodes")
    mine = [n for n in payload["nodes"] if n["peer_id"] == str(world.peer_a)]
    assert mine, "POSITIVE CONTROL FAILED"
    for key in ("label", "peer_id", "last_seen_secs", "state", "live", "dispatch"):
        assert key in mine[0], f"node projection is missing {key!r}"
    assert mine[0]["state"] == "active" and mine[0]["live"] is True
    assert mine[0]["dispatch"] is None       # no shared/monitor record for this peer
    b_ids = {n["peer_id"] for n in ok(world.owner_b, "/v1/dashboard/api/dispatch/nodes")["nodes"]}
    assert str(world.peer_a) not in b_ids


def test_a_monitor_record_fills_dispatch_for_its_peer_only(world):
    """The dispatch LATERAL against a MATCHING row (review queue: the test above only ever
    sees `dispatch is None`). A `shared/monitor` row keyed by peer A's name fills A's
    `dispatch`; peer C, visible to A with no row of its own, stays null, so a join that
    lost its key predicate would show up as C inheriting A's record.

    `shared/monitor` is a fixed global name, so it is claimed only if absent and removed
    again only if this test claimed it, as the fixture does for the usage namespace.
    """
    with _sql_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM shared_memory_namespace WHERE namespace = 'shared/monitor'")
        claimed_here = cur.fetchone() is None
        if claimed_here:
            _claim_namespace(cur, "shared/monitor", world.owner_a, world.peer_a)
        cur.execute("SELECT name FROM peers WHERE id = %s", (world.peer_a,))
        name_a = cur.fetchone()[0]
        row = _mem(cur, "shared/monitor", name_a,
                   {"state": "running", "dispatch_id": f"d-{world.marker}", "cli": "claude"},
                   world.owner_a, world.peer_a)
    try:
        nodes = {n["peer_id"]: n for n in
                 ok(world.owner_a, "/v1/dashboard/api/dispatch/nodes")["nodes"]}
        a = nodes.get(str(world.peer_a))
        assert a is not None, "POSITIVE CONTROL FAILED: A's own node is missing"
        assert a["dispatch"] is not None, (
            "a shared/monitor row keyed by A's peer name did not reach A's dispatch")
        assert a["dispatch"]["state"] == "running"
        assert a["dispatch"]["dispatch_id"] == f"d-{world.marker}"
        c = nodes.get(str(world.peer_c))
        assert c is not None, "POSITIVE CONTROL FAILED: C's node is not visible to A"
        assert c["dispatch"] is None, (
            "peer C picked up a monitor record keyed by another peer's name")
    finally:
        with _sql_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM shared_memory WHERE id = %s", (row,))
            if claimed_here:
                cur.execute("DELETE FROM shared_memory_namespace "
                            "WHERE namespace = 'shared/monitor'")


def test_marketplace_is_an_authenticated_stub(world):
    assert ok(world.owner_a, "/v1/dashboard/api/marketplace") == {"items": []}
    assert Client().get("/v1/dashboard/api/marketplace").status_code == 401, \
        "the stub answered without a credential"


# =========================================================================
# 5. MeshyDocs
# =========================================================================

def test_docs_routes_are_owner_scoped(world):
    m = world.marker
    checks = [
        ("/v1/dashboard/api/clients", "clients", "name", f"client-a-{m}", f"client-b-{m}"),
        ("/v1/dashboard/api/invoices", "invoices", "number", f"INV-A-{m}", f"INV-B-{m}"),
        ("/v1/dashboard/api/documents", "documents", "number", f"DOC-A-{m}", f"DOC-B-{m}"),
        ("/v1/dashboard/api/expenses", "expenses", "vendor", f"vendor-a-{m}", f"vendor-b-{m}"),
        ("/v1/dashboard/api/payouts", "payouts", "contractor",
         f"contractor-a-{m}", f"contractor-b-{m}"),
    ]
    for path, key, field, a_value, b_value in checks:
        a_rows = ok(world.owner_a, path)[key]
        a_values = {row[field] for row in a_rows}
        assert a_value in a_values, f"POSITIVE CONTROL FAILED for {path}"
        assert b_value not in a_values, f"{path} leaked owner B's row to owner A"
        b_values = {row[field] for row in ok(world.owner_b, path)[key]}
        assert b_value in b_values, f"POSITIVE CONTROL FAILED for {path} (owner B)"
        assert a_value not in b_values


def test_invoice_items_check_the_parent_invoices_owner(world):
    """`dashboard_invoice_items` has NO owner column, so `WHERE invoice_id = $1` alone
    would list any owner's line items for a guessed id."""
    mine = ok(world.owner_a, f"/v1/dashboard/api/invoices/{world.invoice_a}/items")["items"]
    assert [i["description"] for i in mine] == [f"item-a-{world.marker}"], \
        "POSITIVE CONTROL FAILED"
    theirs = ok(world.owner_a, f"/v1/dashboard/api/invoices/{world.invoice_b}/items")["items"]
    assert theirs == [], "owner A read the line items of owner B's invoice"


def test_tax_report(world):
    payload = ok(world.owner_a, "/v1/dashboard/api/tax-report")["report"]
    assert set(payload) == {"year", "income", "expenses_by_category", "total_expenses_cents",
                            "total_deductible_cents", "contractor_payouts_cents",
                            "estimated_taxable_cents"}
    assert payload["income"]["total_invoiced_cents"] == 1000, "POSITIVE CONTROL FAILED"
    assert payload["income"]["total_collected_cents"] == 1000
    assert payload["total_expenses_cents"] == 250
    assert payload["contractor_payouts_cents"] == 4200
    # Owner B's 500-cent invoice and 750-cent expense must be nowhere in A's report.
    b_report = ok(world.owner_b, "/v1/dashboard/api/tax-report")["report"]
    assert b_report["income"]["total_invoiced_cents"] == 500
    assert b_report["total_expenses_cents"] == 750


# =========================================================================
# 6. parameter handling and method routing
# =========================================================================

def test_a_non_numeric_limit_is_400_and_a_numeric_one_is_200(world):
    bad = get(world.owner_a, "/v1/dashboard/api/memories", limit="banana")
    assert bad.status_code == 400
    assert body(bad)["error"] == "E_VALIDATION"
    assert get(world.owner_a, "/v1/dashboard/api/memories", limit="5").status_code == 200


def test_the_limit_is_clamped_not_honoured_verbatim(world):
    """`clamp(1, 1000)`: a limit of 0 becomes 1, so a 0-limit request still returns a
    row. Control: a limit of 1 returns exactly one."""
    zero = ok(world.owner_a, "/v1/dashboard/api/memories", limit="0")["memories"]
    one = ok(world.owner_a, "/v1/dashboard/api/memories", limit="1")["memories"]
    assert len(one) == 1, "POSITIVE CONTROL FAILED"
    assert len(zero) == 1, "limit=0 was passed through instead of clamped to 1"


def test_a_bad_level_is_400_and_a_good_one_is_200(world):
    bad = get(world.owner_a, "/v1/dashboard/api/logs", level="shouting")
    assert bad.status_code == 400
    assert body(bad)["error"] == "E_VALIDATION"
    assert get(world.owner_a, "/v1/dashboard/api/logs", level="warn").status_code == 200


def test_a_bad_before_timestamp_is_400_and_a_good_one_is_200(world):
    bad = get(world.owner_a, "/v1/dashboard/api/logs", before="yesterday")
    assert bad.status_code == 400
    assert get(world.owner_a, "/v1/dashboard/api/logs",
               before="2020-01-01T00:00:00Z").status_code == 200


def test_an_empty_level_is_400(world):
    """`?level=` carries an empty value, which `normalize_level` rejects. This build used to
    treat "" as absent and
    answer 200 (review queue, the dashboard's unrecorded divergences). Control: omitting
    the parameter is still 200."""
    empty = get(world.owner_a, "/v1/dashboard/api/logs", level="")
    assert empty.status_code == 400, empty.content[:200]
    assert body(empty)["error"] == "E_VALIDATION"
    assert get(world.owner_a, "/v1/dashboard/api/logs").status_code == 200


@pytest.mark.parametrize("raw", [
    "",                         # empty values fail validation
    "2020-01-01",               # a date alone
    "2020-01-01T00:00:00",      # no offset: fromisoformat returns a NAIVE datetime, which
                                # Postgres would read in the server's TimeZone
    "2020-01-01T00:00",         # no seconds
    "2020-01-01T00:00Z",        # no seconds, WITH an offset: the case above is offsetless
    "2020-01-01T00:00+02:00",   # too, so an optional-seconds regex passed without these
    "20200101T000000Z",         # basic ISO 8601, not RFC3339
])
def test_a_before_that_is_not_rfc3339_is_400(world, raw):
    """`before` requires seconds and an offset. `fromisoformat` accepted all of these, and
    the offsetless ones
    moved the cutoff with the server's TimeZone setting (review queue)."""
    r = get(world.owner_a, "/v1/dashboard/api/logs", before=raw)
    assert r.status_code == 400, (raw, r.status_code, r.content[:200])
    assert body(r)["error"] == "E_VALIDATION"


@pytest.mark.parametrize("raw", [
    "2020-01-01T00:00:00Z", "2020-01-01T00:00:00.123456+02:00", "2020-01-01t00:00:00z",
    "2020-01-01 00:00:00-05:30",
])
def test_an_rfc3339_before_is_accepted(world, raw):
    """Accepted forms use `T`, `t`, or a space between date and time; `Z`, `z`, or a numeric
    offset; and an optional fraction."""
    assert get(world.owner_a, "/v1/dashboard/api/logs", before=raw).status_code == 200, raw


def test_a_leap_second_before_is_refused(world):
    """Divergence 058. Python's datetime has no second 60, so `2016-12-31T23:59:60Z` passes
    this build's RFC3339 shape check and then fails to parse, answering 400. Pinned so this
    departure from RFC 3339's leap-second form is a recorded choice rather than an accident.
    Control: the second before it is accepted."""
    assert get(world.owner_a, "/v1/dashboard/api/logs",
               before="2016-12-31T23:59:59Z").status_code == 200
    leap = get(world.owner_a, "/v1/dashboard/api/logs", before="2016-12-31T23:59:60Z")
    assert leap.status_code == 400, leap.content[:200]
    assert body(leap)["error"] == "E_VALIDATION"


def test_a_non_uuid_source_peer_id_is_ignored_not_rejected(world):
    """A non-UUID source peer value is ignored. The control is a real UUID, which must filter."""
    ignored = ok(world.owner_a, "/v1/dashboard/api/memories", source_peer_id="not-a-uuid")
    unfiltered = ok(world.owner_a, "/v1/dashboard/api/memories")
    assert len(ignored["memories"]) == len(unfiltered["memories"])
    # Peer C, not B: A cannot see any of B's memories (another tenant), so filtering on
    # B returned an EMPTY list and `all(...)` over it was true whatever the filter did.
    filtered = ok(world.owner_a, "/v1/dashboard/api/memories",
                  source_peer_id=str(world.peer_c))["memories"]
    assert str(world.mem_c) in {m["id"] for m in filtered}, (
        "POSITIVE CONTROL FAILED: filtering on C's peer did not return C's memory")
    assert all(m["source_peer_id"] == str(world.peer_c) for m in filtered), \
        "POSITIVE CONTROL FAILED: a real uuid did not filter, so 'ignored' proves nothing"
    assert len(filtered) < len(unfiltered["memories"]), (
        "the real-uuid filter returned as many rows as no filter, so it did not narrow")


def test_a_malformed_invoice_id_is_400(world):
    response = get(world.owner_a, "/v1/dashboard/api/invoices/not-a-uuid/items")
    assert response.status_code == 400
    assert body(response)["error"] == "E_VALIDATION"
    assert get(world.owner_a,
               f"/v1/dashboard/api/invoices/{world.invoice_a}/items").status_code == 200


def test_a_write_method_on_a_read_route_is_405_with_allow(world):
    """This slice implements the READ side only, so every write verb on these paths is
    405 + `Allow: GET`. Pinned here so it cannot silently become a 500 or a 404."""
    response = Client().post("/v1/dashboard/api/memories",
                             HTTP_AUTHORIZATION=f"Bearer {mint(world.owner_a)}")
    assert response.status_code == 405
    assert response["Allow"] == "GET"
    assert get(world.owner_a, "/v1/dashboard/api/memories").status_code == 200


def test_responses_are_compact_json(world):
    """Responses use no spaces. Django's JsonResponse defaults to ", "/": ", which would
    change every byte after the first key."""
    response = get(world.owner_a, "/v1/dashboard/api/stats")
    # A 500 body is compact too, so without these two the test passes on a crash.
    assert response.status_code == 200, response.content[:300]
    raw = response.content.decode()
    assert '":' in raw, "POSITIVE CONTROL FAILED: the body has no key, so compactness is unshown"
    assert '", "' not in raw and '": ' not in raw, raw


def test_csrf_is_an_hmac_bound_to_the_session_not_a_presence_check(monkeypatch):
    """`require_csrf` IS the CSRF control on this surface - there is no other.

    `meshychat/settings.py` leaves Django's `CsrfViewMiddleware` out on purpose (it
    changes the wire), and says so in the comment beside the middleware list. So when
    this method only checked that the header was PRESENT, any cross-site page able to
    make the browser send `X-CSRF-Token: x` passed it. Deleting the check entirely left
    all 76 tests green, because every route on this slice is a GET and nothing called it.

    This test therefore calls it directly rather than through a route. Each refusal below
    is paired with the acceptance that proves the check can still say yes, because a
    method that raised unconditionally would satisfy every negative here on its own.

    The non-ASCII case is not hypothetical politeness: `hmac.compare_digest` raises
    `TypeError` on two `str` with any codepoint above 0x7F, Django decodes request
    headers as latin-1, and `views._route` catches only `ApiError` - so a str comparison
    turns one attacker-chosen header byte into an unhandled HTML 500.
    """
    from django.test import RequestFactory

    from apps.core import websec
    from apps.dashboard.auth import AuthenticatedOwner

    rf = RequestFactory()
    session_id = uuid.uuid4()
    secret = "dashboard-csrf-secret-" + uuid.uuid4().hex
    owner = AuthenticatedOwner(owner_id=uuid.uuid4(), method="cookie",
                               claims={"session_id": session_id})
    pat_caller = AuthenticatedOwner(owner_id=owner.owner_id, method="pat",
                                    scopes=("dashboard:read",))

    def post(token=None):
        extra = {"HTTP_X_CSRF_TOKEN": token} if token is not None else {}
        return rf.post("/v1/dashboard/api/memories", **extra)

    def refused(caller, request, why):
        with pytest.raises(errors.ApiError) as caught:
            caller.require_csrf(request)
        assert (caught.value.status, caught.value.code) == (403, "E_CSRF_REQUIRED"), why
        return caught.value

    monkeypatch.delenv("MESHYCHAT_STATE_SECRET", raising=False)
    monkeypatch.delenv("MESHY_STATE_SECRET", raising=False)
    with override_settings(MESHY_STATE_SECRET=secret, DEBUG=False):
        good = websec.csrf_token_for(session_id, secret.encode("utf-8"))

        # POSITIVE CONTROL, first: the correct token is accepted.
        owner.require_csrf(post(good))

        # ...and a header that is merely PRESENT is not. This is the defect.
        refused(owner, post("x"), "a non-empty but wrong token was accepted: the check "
                                 "is counting the header, not comparing it")
        refused(owner, post(good[:-1]), "a truncated token was accepted")

        # BOUND TO THE SESSION: a token minted for another session is another session's.
        other = websec.csrf_token_for(uuid.uuid4(), secret.encode("utf-8"))
        refused(owner, post(other), "a token minted for a different session was accepted")

        # A hostile header byte is a REFUSAL, never a TypeError escaping as an HTML 500.
        for hostile in ("töken", "ÿ" * 43, good[:-1] + "é"):
            refused(owner, post(hostile), f"non-ASCII header {hostile!r} was accepted")

        # A missing header is still a refusal, and an empty one is not "present".
        refused(owner, post(), "a write with no token at all was accepted")
        refused(owner, post(""), "an empty token was accepted")

        # Exemptions, both asserted so a blanket refusal cannot pass this test:
        # safe methods carry no token...
        owner.require_csrf(rf.get("/v1/dashboard/api/memories"))
        # ...and a credential that must be attached by the client is not forgeable
        # cross-site, so PAT and dev-JWT callers are exempt.
        pat_caller.require_csrf(post())

        # A cookie identity with no session id has nothing to derive the token FROM.
        # Fail closed rather than raising out of the HMAC.
        sessionless = AuthenticatedOwner(owner_id=owner.owner_id, method="cookie")
        refused(sessionless, post(good),
                "a cookie caller with no session id was accepted")

    # FAIL CLOSED WITH NO SECRET. A release build refuses; a debug build
    # passes so a developer is not locked out. The production default here is DEBUG=False,
    # so the default is the refusing one.
    with override_settings(MESHY_STATE_SECRET=None, DEBUG=False):
        refused(owner, post(good),
                "with no state secret configured, a release build accepted a write - "
                "an operator who forgot one variable would be running with no CSRF "
                "control and no signal")
    with override_settings(MESHY_STATE_SECRET=None, DEBUG=True):
        owner.require_csrf(post(good))

    # A blank value is NOT a configured secret; it must not key an HMAC on "".
    with override_settings(MESHY_STATE_SECRET="   ", DEBUG=False):
        refused(owner, post(good), "a blank MESHY_STATE_SECRET was treated as configured")

    # REACHABILITY: the secret can come from MESHYCHAT_STATE_SECRET.
    with override_settings(MESHY_STATE_SECRET=None, DEBUG=False):
        monkeypatch.setenv("MESHYCHAT_STATE_SECRET", secret)
        owner.require_csrf(post(good))
        refused(owner, post("x"),
                "the environment-supplied secret accepted a wrong token")


def test_the_production_auth_posture_is_reachable_from_the_environment(monkeypatch):
    """THE GATE WAS UNREACHABLE, and that is a different defect from a wrong default.

    `bearer_jwt_allowed()` keys on `MESHY_SSO_CONFIGURED`. Both flags used to be read
    through `_setting`, which consults Django settings ONLY, and `meshychat/settings.py`
    defines neither - so the flag was pinned False for every deployment, and **any EdDSA
    `aud=dashboard` token from the configured issuer was full owner authority**. Exporting
    the variable did nothing at all.

    This test asserts REACHABILITY, not the default. It fails against the old
    settings-only reader, because there the environment could not move the answer.

    The sibling extractor in this same project read it correctly, which is exactly why the
    divergence survived: two owner extractors, one flag, different reachability - and the
    working one is the reason nobody noticed the broken one.
    """
    from django.test import override_settings

    from apps.dashboard import auth

    # CONTROL: with neither source set, the legacy Bearer path is allowed. That is the
    # documented default and it is deliberately unchanged.
    monkeypatch.delenv("MESHY_SSO_CONFIGURED", raising=False)
    monkeypatch.delenv("MESHY_ALLOW_DEV_JWT", raising=False)
    with override_settings(MESHY_SSO_CONFIGURED=None, MESHY_ALLOW_DEV_JWT=None):
        assert auth.bearer_jwt_allowed() is True

        # THE FIX: the ENVIRONMENT can now switch the production posture on.
        monkeypatch.setenv("MESHY_SSO_CONFIGURED", "1")
        assert auth.bearer_jwt_allowed() is False, (
            "MESHY_SSO_CONFIGURED had no effect from the environment - the production "
            "posture is still unreachable")

        # ...and the dev-JWT escape is reachable the same way, so a deployment that needs
        # it is not forced to leave SSO off entirely.
        monkeypatch.setenv("MESHY_ALLOW_DEV_JWT", "true")
        assert auth.bearer_jwt_allowed() is True

        # "0" MUST MEAN OFF. `bool("0")` is True, so a deployment exporting
        # MESHY_ALLOW_DEV_JWT=0 to disable the dev path would previously have ENABLED it.
        monkeypatch.setenv("MESHY_ALLOW_DEV_JWT", "0")
        assert auth.bearer_jwt_allowed() is False, (
            "MESHY_ALLOW_DEV_JWT=0 enabled the dev-JWT path")

    # A Django SETTING still wins over the environment, so an explicit deployment value is
    # not overridden by a stray export.
    monkeypatch.setenv("MESHY_SSO_CONFIGURED", "1")
    with override_settings(MESHY_SSO_CONFIGURED=False):
        assert auth.bearer_jwt_allowed() is True


def test_the_capability_route_list_is_derived_from_the_code_not_maintained_by_hand():
    """CAPABILITY_ROUTES must EQUAL the routes that can reach `_resolve_cap`.

    WHY THIS EXISTS. The gate test above iterates a hand-written tuple of two paths. It is a
    real test and it caught a real hole - but the property it protects is "every route that
    resolves a capability is gated", and a hand-written list cannot hold that property: a
    future THIRD route calling `_resolve_cap` would simply not appear in the tuple, the gate
    test would keep passing on its two paths, and the new route would be ungated. That is
    the same shape as the defect being fixed - a gate someone forgets to add - one level up,
    in the test.

    So the list is derived from the source instead, and this test fails when the two
    disagree. Adding a capability route without listing it turns this red.

    THE SCAN IS TRANSITIVE, and that is not incidental: `log_settings` does not call
    `_resolve_cap` directly - it calls `_max_retention_days`, which does. A direct-callers
    scan would have concluded that only `tier_info` resolves a cap, matched a one-element
    list, and been confidently wrong about the exact route the original defect was found on.
    """
    import ast
    from django.urls import get_resolver

    source = (Path(__file__).resolve().parent.parent
              / "apps" / "dashboard" / "views.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    # name -> the function names it calls, for every top-level def in the module.
    calls: dict[str, set[str]] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        called = set()
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call):
                fn = inner.func
                if isinstance(fn, ast.Name):
                    called.add(fn.id)
                elif isinstance(fn, ast.Attribute):
                    called.add(fn.attr)
        calls[node.name] = called
    assert "_resolve_cap" in calls, (
        "control: the scan did not find _resolve_cap defined in views.py - the instrument "
        "is broken, not the code")

    # Transitive closure: who can reach _resolve_cap at all.
    reaching = {"_resolve_cap"}
    changed = True
    while changed:
        changed = False
        for name, called in calls.items():
            if name not in reaching and (called & reaching):
                reaching.add(name)
                changed = True
    reaching.discard("_resolve_cap")
    assert reaching, "control: nothing reaches _resolve_cap, so the closure is broken"

    # Which of those are actually MOUNTED, and at what path.
    def mounted(resolver, prefix=""):
        for pattern in resolver.url_patterns:
            if hasattr(pattern, "url_patterns"):
                yield from mounted(pattern, prefix + str(pattern.pattern))
            else:
                yield "/" + prefix + str(pattern.pattern), pattern.callback

    derived = {path for path, cb in mounted(get_resolver())
               if getattr(cb, "__name__", None) in reaching}

    assert derived == set(CAPABILITY_ROUTES), (
        "CAPABILITY_ROUTES has drifted from the routes that can reach _resolve_cap. "
        f"derived from source: {sorted(derived)}; "
        f"listed in the test: {sorted(CAPABILITY_ROUTES)}. "
        "Add the missing route(s) to CAPABILITY_ROUTES - and make sure the gate covers "
        "them, which is what the list is for.")

    # And the indirect one is genuinely present, so this test cannot pass by finding only
    # the easy direct caller.
    assert "log_settings" in reaching, (
        "the transitive closure lost the INDIRECT caller - log_settings reaches the cap "
        "through _max_retention_days, and a direct-only scan would silently under-report")



@pytest.mark.parametrize("method", ["post", "put", "patch", "delete", "head"])
@pytest.mark.parametrize("path,name", ROUTES)
def test_every_route_answers_a_wrong_method_before_the_credential(world, path, name, method):
    """`_route("GET")` answers the method for every view, where each view used to answer
    it on its own first line. The order is the wire: a wrong method with NO credential is
    405 + `Allow: GET` with an empty body, not the extractor's 401, on all 28 routes, HEAD
    included (DIVERGENCES 055). Control: the same path with a credential and GET is not a
    405, so the 405 is the method's and not the path's."""
    response = getattr(Client(), method)(path)
    assert response.status_code == 405, (path, method, response.status_code)
    assert response["Allow"] == "GET"
    assert response.content == b""
    assert get(world.owner_a, path).status_code != 405


def test_the_logs_route_runs_in_the_shared_server_internal_scaffold(world, monkeypatch):
    """`/api/logs` runs its three statements through `repo.run_internal`, the scaffold
    `fetch_all_internal` is built on, rather than a copy of the transaction, the context
    proof and the except arms of its own. Pinned by wrapping the shared helper: the route
    must pass through it once, under its `logs.list` label, and still answer 200."""
    seen = []
    real = repo.run_internal

    def spy(conn, scope, fn):
        seen.append(scope)
        return real(conn, scope, fn)

    monkeypatch.setattr(repo, "run_internal", spy)
    payload = ok(world.owner_a, "/v1/dashboard/api/logs")
    assert set(payload) == {"data", "retention_days"}
    assert seen == ["logs.list"], seen


def test_both_tier_readers_share_one_owner_tier_lookup(world, monkeypatch):
    """`tier_info` and `_resolve_cap` read the owner's tier through ONE `_owner_tier`, each
    under its own label; `tier_info` hands its tier to `_resolve_cap`, so it is read once
    there, and `/api/logs/settings` (which reaches `_resolve_cap` with no tier) reads it
    once too."""
    from apps.dashboard import views

    seen = []
    real = views._owner_tier

    def spy(conn, scope, label):
        seen.append(label)
        return real(conn, scope, label)

    monkeypatch.setattr(views, "_owner_tier", spy)
    assert ok(world.owner_a, "/v1/dashboard/api/tier-info")["effective_tier"] == "free"
    assert seen == ["tier_info.tier"], seen
    seen.clear()
    ok(world.owner_a, "/v1/dashboard/api/logs/settings")
    assert seen == ["tier.resolve_tier"], seen


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q", "--no-header", "-p", "no:cacheprovider"]))
