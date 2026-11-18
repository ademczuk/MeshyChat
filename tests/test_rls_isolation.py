"""Row-level-security isolation, proved against a real database.

These decide whether the schema is safe to put in front of data. Each asserts a
NEGATIVE (rows that must NOT be visible) and is paired with a positive control, because
"returns nothing" is what BOTH correct isolation and a broken connection look like.

The mechanisms tested are the ones the LIVE SCHEMA uses, not the ones I first assumed:
  peer-scoped     skill_calls      -> app.current_peer_id
  server-internal shared_memory    -> app.server_internal = '1'
My first draft asserted peer-scoping on shared_memory. It has no such policy; the
database said so and the tests were rewritten to match it.

Run:  pytest tests/test_rls_isolation.py -v
Needs MESHY_TEST_DSN (defaults to the dev stack database built by sqlmigrate.py).
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from apps.core import rls  # noqa: E402
from tests.fixtures import signed_rows as sr  # noqa: E402

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")


@pytest.fixture()
def owner_conn():
    """Owner role: bypasses RLS. Used ONLY to seed fixtures."""
    with psycopg.connect(DSN) as conn:
        yield conn


@pytest.fixture()
def app_conn():
    """meshychat_app: the role a request actually runs under."""
    # autocommit: SET LOCAL must bind to a REAL transaction, not a savepoint inside an
    # implicit one. Without this the context leaks past its block - see rls.py.
    with psycopg.connect(DSN, autocommit=True) as conn:
        rls.set_role(conn)
        yield conn


def _peer(conn, tag: str, plane: str = "product") -> dict:
    """An owner + peer. Column names and the owner FK target (`dashboard_users`, not
    `owners`) were read off the live schema after two wrong guesses."""
    owner_id, peer_id = uuid.uuid4(), uuid.uuid4()
    with conn.cursor() as cur:
        # plane matters: a trigger (enforce_ns_dev_only) refuses a shared/* namespace
        # claim from a 'product' owner. The dual-plane tenancy rule lives in the DATABASE,
        # which is one reason this test uses the schema rather than re-expressing its rule.
        # CHECK ((plane='dev') = (trust_group_id IS NOT NULL)): the two are one decision,
        # so a dev-plane owner without a trust group is rejected and vice versa.
        cur.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id) "
                    "VALUES (%s,%s,%s,%s)",
                    (owner_id, f"rls-{tag}-{owner_id.hex[:8]}@example.test", plane,
                     uuid.uuid4() if plane == "dev" else None))
        cur.execute("INSERT INTO peers (id, owner_id, name, display_name) VALUES (%s,%s,%s,%s)",
                    (peer_id, owner_id, f"rls-{tag}-{peer_id.hex[:8]}", f"rls {tag}"))
    conn.commit()
    return {"owner_id": owner_id, "peer_id": peer_id}


def _skill(conn, owner_peer: dict) -> uuid.UUID:
    skill_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO skills (id, peer_id, name, version, mode, share_mode, "
                    "input_schema) VALUES (%s,%s,'test.skill','1','sync','private','{}')",
                    (skill_id, owner_peer["peer_id"]))
    conn.commit()
    return skill_id


def _skill_call(conn, caller: dict, target: dict) -> str:
    """One skill_calls row between two peers. Returns its call_id."""
    call_id = f"call-{uuid.uuid4()}"
    skill_id = _skill(conn, target)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO skill_calls (call_id, caller_peer_id, caller_owner_id, target_peer_id, "
            "target_owner_id, skill_id, skill_name, skill_version, mode, transport, status, args, "
            "deadline_at) VALUES (%s,%s,%s,%s,%s,%s,'test.skill','1','sync','ws','pending','{}', "
            "now() + interval '1 hour')",
            (call_id, caller["peer_id"], caller["owner_id"], target["peer_id"],
             target["owner_id"], skill_id))
    conn.commit()
    return call_id


def test_app_role_is_actually_subject_to_rls(app_conn):
    """Positive control for every negative below."""
    rls.assert_rls_active(app_conn)
    assert rls.current_role(app_conn) == "meshychat_app"


def test_owner_role_bypasses_and_is_detected(owner_conn):
    """Serving requests as the owner role is the silent-leak mistake; it must be caught."""
    with pytest.raises(RuntimeError, match="BYPASSES"):
        rls.assert_rls_active(owner_conn)


def test_peer_scoped_row_hidden_without_context(owner_conn, app_conn):
    a, b = _peer(owner_conn, "a"), _peer(owner_conn, "b")
    call_id = _skill_call(owner_conn, a, b)

    with rls.peer_context(app_conn, peer_id=a["peer_id"], owner_id=a["owner_id"]):
        with app_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM skill_calls WHERE call_id=%s", (call_id,))
            with_context = cur.fetchone()[0]

    with app_conn.cursor() as cur:                       # no context at all
        cur.execute("SELECT count(*) FROM skill_calls WHERE call_id=%s", (call_id,))
        without_context = cur.fetchone()[0]

    assert with_context == 1, ("the caller could not see its OWN call even with context - "
                               "the negative below would pass vacuously")
    assert without_context == 0, "row leaked to a connection with no peer context"


def test_third_peer_cannot_see_a_call_between_two_others(owner_conn, app_conn):
    a, b, c = _peer(owner_conn, "a"), _peer(owner_conn, "b"), _peer(owner_conn, "c")
    call_id = _skill_call(owner_conn, a, b)

    seen = {}
    for tag, p in (("caller", a), ("target", b), ("stranger", c)):
        with rls.peer_context(app_conn, peer_id=p["peer_id"], owner_id=p["owner_id"]):
            with app_conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM skill_calls WHERE call_id=%s", (call_id,))
                seen[tag] = cur.fetchone()[0]

    assert seen["caller"] == 1 and seen["target"] == 1, f"policy hid the participants: {seen}"
    assert seen["stranger"] == 0, "an unrelated peer could see someone else's call"


def test_server_internal_is_a_separate_mechanism(owner_conn, app_conn):
    """shared_memory keys on app.server_internal, NOT on the peer id."""
    a = _peer(owner_conn, "si", plane="dev")
    key = f"rls/{uuid.uuid4()}"
    with owner_conn.cursor() as cur:
        # the namespace is a FK target: shared_memory rows cannot exist in a namespace
        # that was never claimed. That is the append-only design, not a test artefact.
        cur.execute("INSERT INTO shared_memory_namespace (namespace, owner_peer_id, owner_owner_id) "
                    "VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                    ("shared/rls-test", a["peer_id"], a["owner_id"]))
    # provenance_call_id is a GENERATED column (from provenance_jsonb -> payload ->
    # call_id), so it is supplied by writing the provenance, never directly. The row carries
    # the bytes a writer would have signed, whose hash is payload_jcs_sha256 (UNIQUE across
    # the table: the payload's call_id makes it unique per run); since A2 (divergence 051)
    # a row without them is refused from every session.
    sr.insert(owner_conn, sr.row(a["peer_id"], a["owner_id"], "shared/rls-test", key,
                                 {"x": key}, call_id="call-" + key[-8:], author_label=None))
    owner_conn.commit()

    with rls.peer_context(app_conn, peer_id=a["peer_id"], owner_id=a["owner_id"]):
        with app_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM shared_memory WHERE key=%s", (key,))
            with_peer_only = cur.fetchone()[0]

    with rls.server_internal(app_conn):
        with app_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM shared_memory WHERE key=%s", (key,))
            with_internal = cur.fetchone()[0]

    assert with_peer_only == 0, ("shared_memory was visible with only a peer context - "
                                 "the server_internal gate is not doing anything")
    assert with_internal == 1, "the server_internal context could not see the row it seeded"


def test_context_does_not_leak_to_the_next_transaction(owner_conn, app_conn):
    """A pooled connection must not inherit the previous request's visibility."""
    a = _peer(owner_conn, "leak")
    with rls.peer_context(app_conn, peer_id=a["peer_id"], owner_id=a["owner_id"]):
        assert rls.context_values(app_conn)["peer_id"] == str(a["peer_id"])
    assert rls.context_values(app_conn)["peer_id"] in (None, ""), "peer context survived"

    with rls.server_internal(app_conn):
        pass
    with app_conn.cursor() as cur:
        cur.execute("SELECT current_setting('app.server_internal', true)")
        assert cur.fetchone()[0] in (None, ""), "server_internal survived its transaction"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--no-header", "-p", "no:cacheprovider"]))
