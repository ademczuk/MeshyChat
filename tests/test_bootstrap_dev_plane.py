"""`bootstrap_owner --plane dev --trust-group G`: a fresh install whose nodes can write the mesh.

THE BLOCKER, found by the end-to-end run of 2026-09-23 (divergence 024). On a fresh
install the only way to create an owner is `manage.py bootstrap_owner`, and it always
created a PRODUCT-plane owner. The plane cannot be changed afterwards
(`dashboard_users_freeze_plane_group_trg`, sql/040), and every shared/* namespace refuses a
product tenant twice: `memory.grant` refuses it as a grantee (I2,
apps/memoryskills/handlers.py `E_PRODUCT_NO_SHARED_GRANT`) and `memory.write` refuses it as
a writer (`E_PRODUCT_NO_SHARED_WRITE`). A product owner also cannot CLAIM a namespace at
all (`ns_dev_only_trigger`). So every node minted from a fresh install could read the mesh
and never write to it, and the only way past was operator SQL.

The owner's plane must be chosen when the owner is created, so the command now takes it.
This file drives the path the e2e run could not: the dev-plane operator claims a shared
namespace as the bootstrap authority, grants its node write through `memory.grant`, and the
node writes through `memory.write`, all through the handlers the call route dispatches to,
with no SQL beyond seeding the node's peer row (which onboarding creates). The control is
the same flow for a product-plane owner, refused where the e2e run was refused.

Run:  pytest tests/test_bootstrap_dev_plane.py -q
"""
from __future__ import annotations

import io
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

from django.core.management import call_command  # noqa: E402
from django.core.management.base import CommandError  # noqa: E402

from apps.crypto import primitives as P  # noqa: E402
from apps.memoryskills import db, handlers  # noqa: E402
from apps.memoryskills.errors import HandlerError  # noqa: E402

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")


@pytest.fixture()
def owner_conn():
    with psycopg.connect(DSN, autocommit=True) as conn:
        yield conn


@pytest.fixture()
def app_conn():
    conn = db.connect(DSN)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _restore_bootstrap_authority():
    before = handlers.BOOTSTRAP_AUTHORITY_PEER_ID
    yield
    handlers.set_bootstrap_authority(before)


def _bootstrap(tmp_path, **opts) -> tuple[dict, bytes]:
    """Run the command as an operator would, with a public key file; return what it
    printed and the private seed the test (standing in for the operator) keeps."""
    seed = os.urandom(32)
    pub = tmp_path / f"op-{uuid.uuid4().hex[:6]}.pub"
    pub.write_text(P.b64u(P.public_key_of(seed)) + "\n", encoding="ascii")
    out = io.StringIO()
    opts.setdefault("peer_name", f"op-{uuid.uuid4().hex[:8]}")   # divergence 065: a name is an address
    call_command("bootstrap_owner", email=f"op-{uuid.uuid4().hex[:8]}@example.test",
                 public_key_file=str(pub), dsn=DSN, additional_owner=True, stdout=out,
                 **opts)
    return json.loads(out.getvalue()), seed


def _node(owner_conn, owner_id) -> dict:
    """A node under `owner_id`, as POST /v1/onboard/peers creates one: a peer row and its
    active key, attached to the owner who minted the pairing code."""
    seed = os.urandom(32)
    pub = P.public_key_of(seed)
    peer_id = uuid.uuid4()
    with owner_conn.cursor() as cur:
        cur.execute("INSERT INTO peers (id, owner_id, name, display_name) VALUES (%s,%s,%s,%s)",
                    (peer_id, owner_id, f"desktop-{peer_id.hex[:8]}", "Desktop"))
        cur.execute("INSERT INTO peer_keys (peer_id, kid, public_key, active) "
                    "VALUES (%s,%s,%s,TRUE)", (peer_id, P.kid_for_peer_key(pub), P.b64u(pub)))
    return {"peer_id": peer_id, "seed": seed, "kid": P.kid_for_peer_key(pub)}


def _call(conn, peer: dict, skill: str, args: dict):
    payload = {"v": 1, "kind": "skill.call", "call_id": f"call-{uuid.uuid4()}",
               "caller_peer_id": str(peer["peer_id"]), "target_peer_id": str(peer["peer_id"]),
               "skill_name": skill, "skill_version": "1", "args": args,
               "args_attachments": [],
               "issued_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
    env = {"payload": payload, "signature": P.b64u(P.sign(peer["seed"], P.jcs(payload))),
           "signing_kid": peer["kid"],
           "verified_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
    return handlers.dispatch(skill, env, peer["peer_id"], conn)


def _operator(made: dict, seed: bytes) -> dict:
    return {"peer_id": uuid.UUID(made["peer_id"]), "seed": seed, "kid": made["peer_kid"]}


def test_a_dev_plane_owner_is_created_in_the_named_trust_group(tmp_path, owner_conn):
    group = uuid.uuid4()
    made, _ = _bootstrap(tmp_path, plane="dev", trust_group=str(group))
    row = owner_conn.execute("SELECT plane, trust_group_id FROM dashboard_users WHERE id = %s",
                             (made["owner_id"],)).fetchone()
    assert row == ("dev", group)
    # CONTROL, one variable apart: the default is unchanged, a product owner, no group.
    made, _ = _bootstrap(tmp_path)
    row = owner_conn.execute("SELECT plane, trust_group_id FROM dashboard_users WHERE id = %s",
                             (made["owner_id"],)).fetchone()
    assert row == ("product", None)


@pytest.mark.parametrize("opts,why", [
    ({"plane": "dev"}, "needs --trust-group"),
    ({"plane": "product", "trust_group": str(uuid.uuid4())}, "only for --plane dev"),
    ({"plane": "dev", "trust_group": "not-a-uuid"}, "must be a UUID"),
])
def test_a_plane_and_group_that_disagree_are_refused_and_nothing_is_written(
        tmp_path, owner_conn, opts, why):
    """The CHECK behind this (`(plane = 'dev') = (trust_group_id IS NOT NULL)`) would refuse
    the INSERT anyway; the command says why first, and writes nothing."""
    before = owner_conn.execute("SELECT count(*) FROM dashboard_users").fetchone()[0]
    with pytest.raises(CommandError, match=why):
        _bootstrap(tmp_path, **opts)
    assert owner_conn.execute("SELECT count(*) FROM dashboard_users").fetchone()[0] == before


def test_a_dev_plane_install_writes_the_mesh_and_a_product_one_is_refused(
        tmp_path, owner_conn, app_conn):
    ns = f"shared/e2e-{uuid.uuid4().hex[:12]}"
    made, seed = _bootstrap(tmp_path, plane="dev", trust_group=str(uuid.uuid4()))
    operator = _operator(made, seed)
    node = _node(owner_conn, made["owner_id"])

    # The operator's signer is the bootstrap authority (MESHYCHAT_BOOTSTRAP_AUTHORITY_PEER_ID in
    # a deploy), so the claim needs no SQL; it owns the namespace and grants its node.
    handlers.set_bootstrap_authority(operator["peer_id"])
    claimed = _call(app_conn, operator, "namespace.claim", {"namespace": ns})
    assert claimed["owner_peer_id"] == str(operator["peer_id"])
    _call(app_conn, operator, "memory.grant",
          {"namespace": ns, "grantee_peer_id": str(node["peer_id"]), "permission": "write"})
    key = f"msg/{int(datetime.now(timezone.utc).timestamp() * 1000)}-{uuid.uuid4().hex}"
    wrote = _call(app_conn, node, "memory.write",
                  {"namespace": ns, "key": key, "value": {"body": "hello from a node"}})
    assert wrote.get("version") == 1, wrote
    got = owner_conn.execute("SELECT created_by_peer_id FROM shared_memory WHERE namespace = %s"
                             " AND key = %s AND is_head", (ns, key)).fetchone()
    assert got == (node["peer_id"],)

    # CONTROL: the default, product-plane install, as the e2e run met it. Its node cannot
    # be granted write on a shared namespace, and cannot write to one.
    product, _pseed = _bootstrap(tmp_path)
    pnode = _node(owner_conn, product["owner_id"])
    with pytest.raises(HandlerError) as refused:
        _call(app_conn, operator, "memory.grant",
              {"namespace": ns, "grantee_peer_id": str(pnode["peer_id"]),
               "permission": "write", "expires_at": "2099-01-01T00:00:00Z"})
    assert refused.value.code == "E_PRODUCT_NO_SHARED_GRANT"
    write = {"namespace": ns, "key": key + "-p", "value": {"body": "hello from product"}}
    with pytest.raises(HandlerError) as refused:
        _call(app_conn, pnode, "memory.write", write)
    assert refused.value.code == "E_PERMISSION_DENIED", refused.value.code
    # And the SQL the e2e run used to force a grant past I2 does not open the write
    # either: the write side refuses a product tenant on its own (its step P1.5).
    owner_conn.execute(
        "INSERT INTO shared_memory_grant (namespace, grantee_peer_id, grantee_owner_id, "
        "permission, granted_by_peer_id, granted_by_owner_id) VALUES (%s,%s,%s,'write',%s,%s)",
        (ns, pnode["peer_id"], product["owner_id"], operator["peer_id"], made["owner_id"]))
    with pytest.raises(HandlerError) as refused:
        _call(app_conn, pnode, "memory.write", write)
    assert refused.value.code == "E_PRODUCT_NO_SHARED_WRITE", refused.value.code
    assert owner_conn.execute("SELECT count(*) FROM shared_memory WHERE namespace = %s"
                              " AND key = %s", (ns, key + "-p")).fetchone()[0] == 0
