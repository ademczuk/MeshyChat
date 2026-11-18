"""The write side of row-level security (MeshyChat divergence 001).

Before this divergence row-level security guarded reads only: with no context a SELECT returned
nothing, but an INSERT succeeded and could attribute a row to another peer, so an application bug
on a write path crossed tenants and raised nothing.

Every negative below is paired with the positive control that proves the write path still
WORKS, because "insert refused" is also what a completely broken policy looks like.
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

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")


@pytest.fixture()
def owner_conn():
    with psycopg.connect(DSN) as conn:
        yield conn


@pytest.fixture()
def app_conn():
    with psycopg.connect(DSN, autocommit=True) as conn:
        rls.set_role(conn)
        yield conn


def _peer(conn, tag: str) -> dict:
    owner_id, peer_id = uuid.uuid4(), uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO dashboard_users (id, email) VALUES (%s,%s)",
                    (owner_id, f"wso-{tag}-{owner_id.hex[:8]}@example.test"))
        cur.execute("INSERT INTO peers (id, owner_id, name) VALUES (%s,%s,%s)",
                    (peer_id, owner_id, f"wso-{tag}-{peer_id.hex[:8]}"))
    conn.commit()
    return {"owner_id": owner_id, "peer_id": peer_id}


def _insert(conn, peer_id, key):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO local_memory (peer_id, key, value) VALUES (%s,%s,%s)",
                    (peer_id, key, '{"v":1}'))


def test_a_bound_peer_can_write_its_own_row(owner_conn, app_conn):
    """POSITIVE CONTROL. Without this, every refusal below could be a broken policy."""
    a = _peer(owner_conn, "self")
    key = f"wso/{uuid.uuid4()}"
    with rls.peer_context(app_conn, peer_id=a["peer_id"], owner_id=a["owner_id"]):
        _insert(app_conn, a["peer_id"], key)
    with rls.peer_context(app_conn, peer_id=a["peer_id"], owner_id=a["owner_id"]):
        with app_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM local_memory WHERE key=%s", (key,))
            assert cur.fetchone()[0] == 1, "a peer could not write its OWN row"


def test_a_bound_peer_cannot_write_a_row_for_someone_else(owner_conn, app_conn):
    a, b = _peer(owner_conn, "a"), _peer(owner_conn, "b")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with rls.peer_context(app_conn, peer_id=a["peer_id"], owner_id=a["owner_id"]):
            _insert(app_conn, b["peer_id"], f"wso/{uuid.uuid4()}")   # attributed to B


def test_an_unbound_connection_cannot_write_at_all(owner_conn, app_conn):
    a = _peer(owner_conn, "unbound")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _insert(app_conn, a["peer_id"], f"wso/{uuid.uuid4()}")       # no context


def test_the_server_internal_path_still_writes(owner_conn, app_conn):
    """The server legitimately writes rows it does not own. That escape must survive,
    or the divergence breaks the server while looking like a security win."""
    a = _peer(owner_conn, "internal")
    key = f"wso/{uuid.uuid4()}"
    with rls.server_internal(app_conn):
        _insert(app_conn, a["peer_id"], key)
    # Read back as the OWNING PEER, not under server_internal. local_memory has exactly one
    # SELECT policy and it is peer-scoped (local_memory_peer_select), so a server-internal
    # writer cannot read back its own row there. The first version of this test asserted the
    # read-back under server_internal and failed,
    # which looked like a broken write and was a broken assertion.
    with rls.peer_context(app_conn, peer_id=a["peer_id"], owner_id=a["owner_id"]):
        with app_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM local_memory WHERE key=%s", (key,))
            assert cur.fetchone()[0] == 1, "the server-internal write path was broken"


def test_skill_calls_insert_is_bound_to_the_caller(owner_conn, app_conn):
    """The same rule on the call table: a caller cannot file a call as somebody else."""
    a, b = _peer(owner_conn, "caller"), _peer(owner_conn, "other")
    skill_id = uuid.uuid4()
    with owner_conn.cursor() as cur:
        cur.execute("INSERT INTO skills (id, peer_id, name, version, mode, share_mode, "
                    "input_schema) VALUES (%s,%s,'t.s','1','sync','private','{}')",
                    (skill_id, b["peer_id"]))
    owner_conn.commit()

    def insert_as(caller):
        with app_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO skill_calls (call_id, caller_peer_id, caller_owner_id, "
                "target_peer_id, target_owner_id, skill_id, skill_name, skill_version, mode, "
                "transport, status, args, deadline_at) VALUES (%s,%s,%s,%s,%s,%s,'t.s','1',"
                "'sync','ws','pending','{}', now() + interval '1 hour')",
                (f"call-{uuid.uuid4()}", caller["peer_id"], caller["owner_id"],
                 b["peer_id"], b["owner_id"], skill_id))

    with rls.peer_context(app_conn, peer_id=a["peer_id"], owner_id=a["owner_id"]):
        insert_as(a)                                   # positive control: own call is fine
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with rls.peer_context(app_conn, peer_id=a["peer_id"], owner_id=a["owner_id"]):
            insert_as(b)                               # filing a call as B must fail


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--no-header", "-p", "no:cacheprovider"]))
