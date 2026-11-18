"""Inferred edges on the mesh graph routes: OPT-IN, and tiered on every hit.

Through the REAL urlconf with a REAL EdDSA bearer, as tests/test_mesh_routes.py. The
inferred rows are seeded straight into mesh_inferred_edges (the auditor's own tests prove
how they get there), so these tests are about the READ contract only:

  * without `include_inferred`, or with `=0`, no inferred hit appears and every hit says
    `tier: "declared"`; `=1` is the positive control that shows the inferred one, with
    `tier: "inferred"` and its provenance, and the coverage denominators;
  * a superseded version's inference is hidden like a declared edge would be;
  * the flag is parsed strictly (a typo is a 422, never a silent default);
  * a read writes nothing;
  * the inferred tier sits behind the SAME mesh-chat read gate as the declared one
    (divergence 018 (a)), and the inbox's inferred tier answers for every label the
    declared tier does, not only the primary one. Both are integration properties: this
    file was written on a base without the gate, and the two met in apps/mesh/views.py.

Run:  pytest tests/test_meshaudit_routes.py -v
"""
from __future__ import annotations

import datetime as _dt
import json
import os
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
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
os.environ.setdefault("MESHY_ALLOWED_HOSTS", "testserver,127.0.0.1,localhost")

ISSUER = "meshy-meshaudit-test-issuer"
_signing_key = ed25519.Ed25519PrivateKey.generate()
JWT_PRIVATE_PEM = _signing_key.private_bytes(
    encoding=serialization.Encoding.PEM, format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption())
JWT_PUBLIC_PEM = _signing_key.public_key().public_bytes(
    encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo)

import django  # noqa: E402
from django.conf import settings as dj_settings  # noqa: E402

if not dj_settings.configured:  # pragma: no cover
    django.setup()

import jwt as pyjwt  # noqa: E402
from django.test import Client, override_settings  # noqa: E402

from apps.core import rls  # noqa: E402
from apps.memoryskills import handlers  # noqa: E402
from apps.memoryskills.validation import MESH_CHAT_NAMESPACE as NS  # noqa: E402
from apps.mesh import projection as MP  # noqa: E402
from apps.meshaudit import prompt as PR  # noqa: E402
from tests.fixtures import history_rows  # noqa: E402
from tests.fixtures import signed_rows as sr  # noqa: E402


@pytest.fixture(autouse=True)
def mesh_settings():
    with override_settings(ROOT_URLCONF="meshychat.urls", ALLOWED_HOSTS=["*"],
                           MESHY_JWT_PUBLIC_KEY_PEM=JWT_PUBLIC_PEM, MESHY_JWT_ISSUER=ISSUER,
                           MESHY_DSN=DSN):
        yield


@pytest.fixture()
def client():
    return Client()


@pytest.fixture()
def owner():
    with psycopg.connect(DSN, autocommit=True) as c:
        yield c


def _auth(peer_id) -> dict:
    now = int(_dt.datetime.now(_dt.timezone.utc).timestamp())
    tok = pyjwt.encode({"sub": str(peer_id), "iss": ISSUER, "iat": now, "exp": now + 300,
                        "aud": "peer", "scopes": []}, JWT_PRIVATE_PEM, algorithm="EdDSA")
    return {"HTTP_AUTHORIZATION": f"Bearer {tok}"}


def _peer(conn, tag: str) -> dict:
    owner_id, peer_id, group = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id)"
                    " VALUES (%s,%s,'dev',%s)",
                    (owner_id, f"aud-{tag}-{owner_id.hex[:8]}@example.test", group))
        cur.execute("INSERT INTO peers (id, owner_id, name, display_name) VALUES (%s,%s,%s,%s)",
                    (peer_id, owner_id, f"aud-{tag}-{peer_id.hex[:8]}", f"aud {tag}"))
        cur.execute("INSERT INTO shared_memory_namespace (namespace, owner_peer_id,"
                    " owner_owner_id, claimant_plane, ephemeral, retention_policy)"
                    " VALUES (%s,%s,%s,'dev',FALSE,'keep_all') ON CONFLICT DO NOTHING",
                    (NS, peer_id, owner_id))
    return {"peer_id": peer_id, "owner_id": owner_id, "label": tag}


def _grant_read(conn, peer: dict) -> None:
    """Read on shared/mesh-chat, which every graph route requires since divergence 018 (a).
    Granted by whoever holds the namespace claim (the first `_peer` of the run, or another
    file's claimant); the claimant itself needs no grant row, as check_permission says."""
    with conn.cursor() as cur:
        cur.execute("SELECT owner_peer_id, owner_owner_id FROM shared_memory_namespace"
                    " WHERE namespace = %s", (NS,))
        ns_peer, ns_owner = cur.fetchone()
        if ns_peer == peer["peer_id"]:
            return
        cur.execute("INSERT INTO shared_memory_grant (namespace, grantee_peer_id,"
                    " grantee_owner_id, permission, granted_by_peer_id, granted_by_owner_id)"
                    " VALUES (%s,%s,%s,'read',%s,%s)",
                    (NS, peer["peer_id"], peer["owner_id"], ns_peer, ns_owner))


def _row(conn, peer, value: dict, *, version: int = 1, parent_id=None, key=None,
         is_head: bool = True) -> dict:
    rid = uuid.uuid4()
    key = key or f"msg/{int(_dt.datetime.now().timestamp() * 1000)}-{rid.hex}"
    # With the bytes a writer would have signed (divergence 051: A2 refuses a row without). A later version of a mail key is
    # seeded as history (divergence 059: tests/fixtures/history_rows.py).
    history_rows.insert_seed(conn, sr.row(peer["peer_id"], peer["owner_id"], NS, key, value, rid=rid,
                                          call_id=f"call-{rid}", version=version, parent_id=parent_id,
                                          is_head=is_head, author_label=peer["label"]))
    return {"id": rid, "key": key}


def _infer(conn, row: dict, edge_type: str, dst: str) -> None:
    """What `python -m apps.meshaudit derive --write` would have written."""
    with rls.server_internal(conn):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO mesh_inferred_edges (src_id, src_key, src_version, edge_type,"
                " dst, model, provider_id, prompt_version, recorded_at)"
                " SELECT id, key, version, %s, %s, 'fake-gemma', 'chat-fake', %s, created_at"
                " FROM shared_memory WHERE id = %s",
                (edge_type, dst, PR.PROMPT_VERSION, row["id"]))


def _table_counts(conn) -> tuple[int, int, int]:
    with rls.server_internal(conn):
        with conn.cursor() as cur:
            cur.execute("SELECT (SELECT count(*) FROM mesh_inferred_edges),"
                        " (SELECT count(*) FROM mesh_inferred_audits),"
                        " (SELECT count(*) FROM mesh_edges)")
            return tuple(cur.fetchone())


@pytest.fixture()
def scene(owner):
    """`me` has one DECLARED message and one legacy message INFERRED as addressed to me;
    a thread has one declared member and one inferred member."""
    p = _peer(owner, "sender")
    me_peer = _peer(owner, "me")
    _grant_read(owner, me_peer)          # a fleet node holds read; see _grant_read
    me = f"me-{uuid.uuid4().hex[:6]}"
    thread = f"t-{uuid.uuid4().hex[:6]}"
    handlers.prime_roster_labels(json.dumps({"peers": {str(me_peer["peer_id"]): me}}).encode())
    declared = _row(owner, p, {"to": me, "thread_id": thread, "body": "declared"})
    legacy = _row(owner, p, {"body": "legacy, says nothing structured"})
    MP.rebuild(owner)
    _infer(owner, legacy, "addressed_to", me)
    _infer(owner, legacy, "in_thread", thread)
    yield {"me_peer": me_peer, "me": me, "thread": thread, "declared": declared,
           "legacy": legacy, "sender": p}
    handlers.prime_roster_labels(b"{}")


def _keys_by_tier(body) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for h in body["hits"]:
        out.setdefault(h["tier"], set()).add(h["key"])
    return out


def test_the_inbox_hides_inferred_edges_unless_asked(client, scene):
    auth = _auth(scene["me_peer"]["peer_id"])
    for url in ("/v1/mesh/inbox", "/v1/mesh/inbox?include_inferred=0",
                "/v1/mesh/inbox?include_inferred=false"):
        r = client.get(url, **auth)
        assert r.status_code == 200, (url, r.content[:300])
        body = r.json()
        assert _keys_by_tier(body) == {"declared": {scene["declared"]["key"]}}, url
        assert body["inferred"] == {"included": False}, url

    r = client.get("/v1/mesh/inbox?include_inferred=1", **auth)
    assert r.status_code == 200, r.content[:300]
    body = r.json()
    assert _keys_by_tier(body) == {"declared": {scene["declared"]["key"]},
                                   "inferred": {scene["legacy"]["key"]}}, (
        "positive control: =1 shows the inferred hit, beside the declared one")
    hit = next(h for h in body["hits"] if h["tier"] == "inferred")
    assert hit["edge_type"] == "addressed_to" and hit["dst"] == scene["me"]
    assert hit["author"] == scene["sender"]["label"], "the author is the DECLARED stamp"
    assert hit["inferred"]["model"] == "fake-gemma"
    assert hit["inferred"]["provider_id"] == "chat-fake"
    assert hit["inferred"]["prompt_version"] == PR.PROMPT_VERSION
    assert body["inferred"]["included"] is True and body["inferred"]["count"] == 1
    cov = body["inferred"]["coverage"]
    assert cov["eligible"] >= 1 and cov["edges"]["addressed_to"] >= 1
    assert cov["prompt_version"] == PR.PROMPT_VERSION
    assert "coverage" in body, "the declared coverage is still there"


def test_the_thread_route_tiers_the_same_way(client, scene):
    auth = _auth(scene["me_peer"]["peer_id"])
    url = f"/v1/mesh/threads/{scene['thread']}"
    off = client.get(url, **auth).json()
    assert _keys_by_tier(off) == {"declared": {scene["declared"]["key"]}}
    assert off["inferred"] == {"included": False}
    on = client.get(url + "?include_inferred=1", **auth).json()
    assert _keys_by_tier(on) == {"declared": {scene["declared"]["key"]},
                                 "inferred": {scene["legacy"]["key"]}}
    assert [h["tier"] for h in on["hits"]] == ["declared", "inferred"], (
        "oldest first across both tiers: the declared row was written first")


def test_a_superseded_version_is_hidden_on_the_inferred_tier_too(client, owner, scene):
    auth = _auth(scene["me_peer"]["peer_id"])
    before = client.get("/v1/mesh/inbox?include_inferred=1", **auth).json()
    assert scene["legacy"]["key"] in _keys_by_tier(before).get("inferred", set()), (
        "positive control")
    with owner.cursor() as cur:
        cur.execute("UPDATE shared_memory SET is_head = FALSE WHERE id = %s",
                    (scene["legacy"]["id"],))
    _row(owner, scene["sender"], {"body": "v2"}, version=2, parent_id=scene["legacy"]["id"],
         key=scene["legacy"]["key"])
    after = client.get("/v1/mesh/inbox?include_inferred=1", **auth).json()
    assert "inferred" not in _keys_by_tier(after), after["hits"]


def test_the_flag_is_parsed_strictly(client, scene):
    auth = _auth(scene["me_peer"]["peer_id"])
    for bad in ("yes", "2", "True", ""):
        r = client.get(f"/v1/mesh/inbox?include_inferred={bad}", **auth)
        assert r.status_code == 422, (bad, r.status_code)
    assert client.get("/v1/mesh/inbox?include_inferred=true", **auth).status_code == 200


def test_the_truncation_flag_covers_the_merge(client, scene):
    auth = _auth(scene["me_peer"]["peer_id"])
    one = client.get("/v1/mesh/inbox?include_inferred=1&limit=1", **auth).json()
    assert len(one["hits"]) == 1 and one["truncated"] is True, (
        "two hits cut to one: each tier fit the limit alone, only the merge dropped one")
    two = client.get("/v1/mesh/inbox?include_inferred=1&limit=2", **auth).json()
    assert len(two["hits"]) == 2 and two["truncated"] is False, "positive control"


def test_a_read_writes_nothing(client, owner, scene):
    auth = _auth(scene["me_peer"]["peer_id"])
    before = _table_counts(owner)
    for url in ("/v1/mesh/inbox?include_inferred=1",
                f"/v1/mesh/threads/{scene['thread']}?include_inferred=1"):
        assert client.get(url, **auth).status_code == 200
    assert _table_counts(owner) == before
    assert before[0] >= 2, "positive control: there were inferred rows to read"


def test_the_inferred_tier_is_behind_the_same_read_gate(client, owner, scene):
    """Integration of 018 (a) with 021: a peer without read on shared/mesh-chat is refused
    BEFORE either tier is read, flag or no flag, with the gate's own typed code; the same
    peer and token, once granted, reads the inferred thread member (positive control)."""
    outsider = _peer(owner, "outsider")
    auth = _auth(outsider["peer_id"])
    thread_url = f"/v1/mesh/threads/{scene['thread']}?include_inferred=1"
    for url in ("/v1/mesh/inbox?include_inferred=1", thread_url):
        r = client.get(url, **auth)
        assert r.status_code == 403, (url, r.content[:300])
        assert r.json()["error"] == "E_PERMISSION_DENIED", url
        assert scene["legacy"]["key"].encode() not in r.content, url
    _grant_read(owner, outsider)
    r = client.get(thread_url, **auth)
    assert r.status_code == 200, r.content[:300]
    assert scene["legacy"]["key"] in _keys_by_tier(r.json()).get("inferred", set()), (
        "positive control: the grant, and only the grant, opened the inferred tier")


def test_the_inferred_inbox_answers_for_every_label_of_the_caller(client, owner, scene):
    """graphread answers a caller for its roster label AND its peers.name (018 (b)); the
    inferred tier must match the same set, or a legacy row inferred as addressed to the
    peers.name spelling would be visible on the declared tier's terms and not on its own.
    The roster-label inference already in the scene is the positive control."""
    with owner.cursor() as cur:
        cur.execute("SELECT name FROM peers WHERE id = %s", (scene["me_peer"]["peer_id"],))
        name = cur.fetchone()[0]
    assert name != scene["me"], "the scene must give `me` two distinct labels"
    by_name = _row(owner, scene["sender"], {"body": "legacy, for the peers.name spelling"})
    MP.rebuild(owner)
    _infer(owner, by_name, "addressed_to", name)
    body = client.get("/v1/mesh/inbox?include_inferred=1",
                      **_auth(scene["me_peer"]["peer_id"])).json()
    inferred = _keys_by_tier(body).get("inferred", set())
    assert scene["legacy"]["key"] in inferred, "positive control: the roster label"
    assert by_name["key"] in inferred, (
        "the inferred tier matched only the primary label; peers.name was dropped")
    hit = next(h for h in body["hits"] if h["key"] == by_name["key"])
    assert hit["dst"] == name and hit["tier"] == "inferred"


def test_the_inferred_tables_are_closed_without_server_internal(owner, scene):
    """ENABLE + FORCE RLS: the app role with no context sees and writes nothing; the same
    role under server_internal sees the rows."""
    with rls.server_internal(owner):
        with owner.cursor() as cur:
            cur.execute("INSERT INTO mesh_inferred_audits (src_id, prompt_version, outcome)"
                        " VALUES (%s, 'rls-test', 'no_body')", (scene["legacy"]["id"],))
    with psycopg.connect(DSN, autocommit=True) as app:
        rls.set_role(app)
        with app.cursor() as cur:
            for table in ("mesh_inferred_edges", "mesh_inferred_audits"):
                cur.execute(f"SELECT count(*) FROM {table}")
                assert cur.fetchone()[0] == 0, f"{table} leaked to a contextless read"
            with pytest.raises(psycopg.Error):
                cur.execute("INSERT INTO mesh_inferred_edges (src_id, src_key, src_version,"
                            " edge_type, dst, model, provider_id, prompt_version,"
                            " recorded_at) VALUES (%s, 'k', 1, 'in_thread', 't', 'm', 'p',"
                            " 'v', now())", (scene["legacy"]["id"],))
        with rls.server_internal(app):
            with app.cursor() as cur:
                cur.execute("SELECT count(*) FROM mesh_inferred_edges WHERE src_id = %s",
                            (scene["legacy"]["id"],))
                assert cur.fetchone()[0] == 2, "positive control"
                cur.execute("SELECT count(*) FROM mesh_inferred_audits WHERE src_id = %s",
                            (scene["legacy"]["id"],))
                assert cur.fetchone()[0] == 1, "positive control"
        rls.assert_rls_active(app, table="mesh_inferred_edges")
        rls.assert_rls_active(app, table="mesh_inferred_audits")


def test_the_schema_refuses_a_second_thread_and_a_bad_reply(owner, scene):
    """One thread per message, and replies_to must be another message's msg/ key, at the
    storage boundary. Positive control: a first thread for a fresh row is accepted."""
    fresh = _row(owner, scene["sender"], {"body": "another legacy row"})
    _infer(owner, fresh, "in_thread", scene["thread"])
    with pytest.raises(psycopg.errors.UniqueViolation):
        _infer(owner, fresh, "in_thread", "t-second")
    with pytest.raises(psycopg.errors.CheckViolation):
        _infer(owner, fresh, "replies_to", "wiki/not-a-message")
    with pytest.raises(psycopg.errors.CheckViolation):
        _infer(owner, fresh, "replies_to", fresh["key"])
    with pytest.raises(psycopg.errors.CheckViolation):
        _infer(owner, fresh, "owes_ack", scene["me"])
    _infer(owner, fresh, "replies_to", scene["declared"]["key"])       # positive control


def test_retention_deletes_the_inference_with_its_message(owner, scene):
    fresh = _row(owner, scene["sender"], {"body": "to be pruned"})
    _infer(owner, fresh, "addressed_to", scene["me"])
    with rls.server_internal(owner):
        with owner.cursor() as cur:
            cur.execute("SELECT count(*) FROM mesh_inferred_edges WHERE src_id = %s",
                        (fresh["id"],))
            assert cur.fetchone()[0] == 1, "positive control"
            cur.execute("DELETE FROM shared_memory WHERE id = %s", (fresh["id"],))
            cur.execute("SELECT count(*) FROM mesh_inferred_edges WHERE src_id = %s",
                        (fresh["id"],))
            assert cur.fetchone()[0] == 0, "ON DELETE CASCADE"
