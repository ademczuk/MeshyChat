"""The mesh graph read surface is safe and usable by real nodes (divergence 018).

Four measured gaps, one section each, every test through the REAL urlconf with a REAL
EdDSA bearer:

  (a) the graph routes answered any authenticated peer. Now they call memory.read's own
      permission check on shared/mesh-chat and refuse with a typed 403.
  (b) the roster is never loaded in production, so every label was `peer-<id8>`. Now the
      peers table names the caller; the roster still overrides the primary label.
  (c) /v1/mesh/wait, the long-poll the node client parks on, did not exist. It now
      returns newly visible rows to a waiting node.
  (d) nothing counted route usage. Now `mesh_route_usage` does, and /v1/mesh/drift says so.

House rules: every negative sits beside its positive control in the same test (C1), and
each new module is imported INSIDE the tests that need it, so that against the pre-change
tree each test fails on its own assertion or its own ImportError rather than the whole
file failing at collection. That is what makes the red-proof per test.

Run:  pytest tests/test_mesh_graphread.py -q
"""
from __future__ import annotations

import base64
import datetime as _dt
import importlib
import json
import os
import sys
import threading
import time
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

ISSUER = "meshy-mesh-graphread-issuer"
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
from apps.memoryskills.errors import HandlerError  # noqa: E402
from apps.memoryskills.permissions import NAMESPACE_WIDE, check_permission  # noqa: E402
from apps.memoryskills.validation import MESH_CHAT_NAMESPACE as NS  # noqa: E402
from apps.mesh import projection as P  # noqa: E402
from tests.fixtures import signed_rows as sr  # noqa: E402

GRAPH_ROUTES = ("/v1/mesh/inbox", "/v1/mesh/owed", "/v1/mesh/threads/t-graphread",
                "/v1/mesh/drift", "/v1/mesh/mentions?ref=%2373")


def _mod(name: str):
    """A module this slice adds, imported where it is used (see the module docstring)."""
    return importlib.import_module(f"apps.mesh.{name}")


@pytest.fixture(autouse=True)
def mesh_settings():
    with override_settings(ROOT_URLCONF="meshychat.urls", ALLOWED_HOSTS=["*"],
                           MESHY_JWT_PUBLIC_KEY_PEM=JWT_PUBLIC_PEM, MESHY_JWT_ISSUER=ISSUER,
                           MESHY_DSN=DSN):
        yield


@pytest.fixture(autouse=True)
def no_roster():
    handlers.prime_roster_labels(b"{}")
    yield
    handlers.prime_roster_labels(b"{}")


@pytest.fixture()
def client():
    return Client()


@pytest.fixture()
def owner():
    with psycopg.connect(DSN, autocommit=True) as c:
        yield c


def _mint(peer_id, ttl=300) -> str:
    now = int(_dt.datetime.now(_dt.timezone.utc).timestamp())
    return pyjwt.encode({"sub": str(peer_id), "iss": ISSUER, "iat": now, "exp": now + ttl,
                         "aud": "peer", "scopes": []}, JWT_PRIVATE_PEM, algorithm="EdDSA")


def _auth(peer_id, ttl=300) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {_mint(peer_id, ttl)}"}


def _peer(conn, tag: str, *, name: str | None = None) -> dict:
    owner_id, peer_id, group = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    name = name if name is not None else f"gr-{tag}-{peer_id.hex[:8]}"
    with conn.cursor() as cur:
        cur.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id)"
                    " VALUES (%s,%s,'dev',%s)",
                    (owner_id, f"gr-{tag}-{owner_id.hex[:8]}@example.test", group))
        cur.execute("INSERT INTO peers (id, owner_id, name, display_name) VALUES (%s,%s,%s,%s)",
                    (peer_id, owner_id, name, f"gr {tag}"))
    return {"peer_id": peer_id, "owner_id": owner_id, "name": name, "label": name}


def _claim(conn, ns: str, peer) -> None:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO shared_memory_namespace (namespace, owner_peer_id,"
                    " owner_owner_id, claimant_plane, ephemeral, retention_policy)"
                    " VALUES (%s,%s,%s,'dev',FALSE,'keep_all') ON CONFLICT DO NOTHING",
                    (ns, peer["peer_id"], peer["owner_id"]))


def _mesh_claimant(conn) -> dict:
    """Whoever owns shared/mesh-chat in this run's database, claiming it if nobody has."""
    _claim(conn, NS, _peer(conn, "claimant"))
    with conn.cursor() as cur:
        cur.execute("SELECT owner_peer_id, owner_owner_id FROM shared_memory_namespace"
                    " WHERE namespace = %s", (NS,))
        peer_id, owner_id = cur.fetchone()
    return {"peer_id": peer_id, "owner_id": owner_id}


def _grant(conn, ns: str, grantee, granter, *, permission="read", revoked=False,
           expires_at=None) -> None:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO shared_memory_grant (namespace, grantee_peer_id,"
                    " grantee_owner_id, permission, granted_by_peer_id, granted_by_owner_id,"
                    " revoked, revoked_at, expires_at)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (ns, grantee["peer_id"], grantee["owner_id"], permission,
                     granter["peer_id"], granter["owner_id"], revoked,
                     _dt.datetime.now(_dt.timezone.utc) if revoked else None, expires_at))


def _row(conn, peer, value: dict, *, ns: str = NS, created_at=None) -> dict:
    rid = uuid.uuid4()
    when = created_at or _dt.datetime.now(_dt.timezone.utc)
    key = f"msg/{int(when.timestamp() * 1000)}-{rid.hex}"
    # With the bytes a writer would have signed (divergence 051: A2 refuses a row without).
    sr.insert(conn, sr.row(peer["peer_id"], peer["owner_id"], ns, key, value, rid=rid,
                           call_id=f"call-{rid}", author_label=peer["label"], created_at=when))
    created = conn.execute("SELECT created_at FROM shared_memory WHERE id = %s",
                           (rid,)).fetchone()[0]
    return {"key": key, "id": rid, "created_at": created}


def _private_ns(conn, tag: str) -> tuple[str, dict]:
    """A namespace of this test's own, claimed by a fresh sender. /v1/mesh/wait reads
    EVERY namespace the caller can
    read, so a waiter granted only this one cannot be woken by a sibling test's rows."""
    ns = f"shared/mesh-chat-{tag}-{uuid.uuid4().hex[:8]}"
    sender = _peer(conn, f"{tag}-sender")
    _claim(conn, ns, sender)
    return ns, sender


def _usage_calls(conn, route: str, peer_id, outcome: str) -> int:
    with rls.server_internal(conn):
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(sum(calls), 0) FROM mesh_route_usage WHERE route = %s"
                        " AND peer_id = %s AND outcome = %s", (route, peer_id, outcome))
            return int(cur.fetchone()[0])


# ======================================================================= (a) the gate

def test_each_graph_route_refuses_a_peer_without_read_and_serves_one_with_it(client, owner):
    """THE gap. A peer with no grant on shared/mesh-chat gets a typed 403 from every graph
    route; the SAME peer, once granted read, gets 200 from every one. The control is the
    same token, so the 403 can only be the grant."""
    claimant = _mesh_claimant(owner)
    p = _peer(owner, "nogrant")
    hdrs = _auth(p["peer_id"])
    for url in GRAPH_ROUTES:
        r = client.get(url, **hdrs)
        assert r.status_code == 403, (url, r.status_code, r.content[:200])
        assert r.json()["error"] == "E_PERMISSION_DENIED", (url, r.json())
    _grant(owner, NS, p, claimant)
    for url in GRAPH_ROUTES:
        r = client.get(url, **hdrs)
        assert r.status_code == 200, (url, r.status_code, r.content[:200])


def test_a_revoked_or_expired_grant_is_refused_and_a_live_one_is_not(client, owner):
    """The grant predicate is memory.read's: `revoked = FALSE AND (expires_at IS NULL OR
    expires_at > now())`. Dropping either half would count history as capability."""
    claimant = _mesh_claimant(owner)
    revoked, expired, live = (_peer(owner, t) for t in ("revoked", "expired", "live"))
    _grant(owner, NS, revoked, claimant, revoked=True)
    _grant(owner, NS, expired, claimant,
           expires_at=_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=1))
    _grant(owner, NS, live, claimant,
           expires_at=_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=1))
    for who, want in ((revoked, 403), (expired, 403), (live, 200)):
        r = client.get("/v1/mesh/inbox", **_auth(who["peer_id"]))
        assert r.status_code == want, (who["name"], r.status_code, r.content[:200])


def test_the_namespace_owner_needs_no_grant(client, owner):
    """Owner holds implicit admin in check_permission; the gate must not demand a grant
    row from the claimant (that would lock the owner out of its own namespace)."""
    claimant = _mesh_claimant(owner)
    r = client.get("/v1/mesh/drift", **_auth(claimant["peer_id"]))
    assert r.status_code == 200, r.content[:200]


def test_an_unclaimed_namespace_is_a_typed_403_not_an_empty_answer(client, owner, monkeypatch):
    """Default-deny on an unclaimed namespace, with its own code. Pointed at a namespace
    nobody claimed; the positive control is the same peer on the claimed one."""
    views = _mod("views")
    claimant = _mesh_claimant(owner)
    monkeypatch.setattr(views, "MESH_CHAT_NAMESPACE", f"shared/unclaimed-{uuid.uuid4().hex}")
    r = client.get("/v1/mesh/inbox", **_auth(claimant["peer_id"]))
    assert r.status_code == 403 and r.json()["error"] == "E_NAMESPACE_UNCLAIMED", r.content
    monkeypatch.setattr(views, "MESH_CHAT_NAMESPACE", NS)
    assert client.get("/v1/mesh/inbox", **_auth(claimant["peer_id"])).status_code == 200


def test_the_gate_is_memory_reads_own_function(owner):
    """Structural, beside the behavioural tests above: the view gate IS check_permission
    with need='read' on the mesh namespace, not a re-expression of it. A copied predicate
    is how two gates drift (V1-DESIGN-RULES A2)."""
    views = _mod("views")
    claimant = _mesh_claimant(owner)
    stranger = _peer(owner, "stranger")
    with rls.server_internal(owner):
        views.require_mesh_read(owner, claimant["peer_id"])          # positive control
        with pytest.raises(HandlerError) as e:
            views.require_mesh_read(owner, stranger["peer_id"])
        with pytest.raises(HandlerError) as e2:
            check_permission(NS, stranger["peer_id"], "read", owner, resource=NAMESPACE_WIDE)
    assert e.value.code == e2.value.code == "E_PERMISSION_DENIED"


def test_the_graph_routes_and_the_addressed_wait_refuse_through_one_helper(client, owner,
                                                                          monkeypatch):
    """`_route` and the addressed wait both answer the gate's 403 from `_mesh_read_refusal`,
    and the codes it reads as a denial are the usage counter's own set, so the two doors and
    the `denied` outcome are one rule. Spied by calling through: the answers are the real
    ones. (Mutant: `_route` keeps its own copy of the try/except: the inbox call is unseen.)"""
    views, usage = _mod("views"), _mod("usage")
    assert views._DENIED is usage.DENIED_CODES
    _mesh_claimant(owner)
    stranger = _peer(owner, "one-gate")
    real, seen = views._mesh_read_refusal, []

    def spy(c, peer_id):
        answer = real(c, peer_id)
        seen.append((str(peer_id), None if answer is None else answer.status_code))
        return answer

    monkeypatch.setattr(views, "_mesh_read_refusal", spy)
    bodies = []
    for url in ("/v1/mesh/inbox", "/v1/mesh/wait?addressed=1&timeout=0"):
        r = client.get(url, **_auth(stranger["peer_id"]))
        assert r.status_code == 403, (url, r.status_code, r.content[:200])
        bodies.append(r.json())
    assert seen == [(str(stranger["peer_id"]), 403)] * 2, seen
    assert bodies[0] == bodies[1] and bodies[0]["error"] == "E_PERMISSION_DENIED", bodies


# ================================================================ (b) the caller's label

def test_the_label_comes_from_peers_name_when_no_roster(client, owner):
    """No roster is loaded (the production state here). The label is the peer's own
    peers.name, the response says so, and mail addressed to that name is found."""
    claimant = _mesh_claimant(owner)
    me = _peer(owner, "named")
    _grant(owner, NS, me, claimant)
    k = _row(owner, claimant | {"label": "someone"}, {"to": me["name"], "body": "hi"})["key"]
    _row(owner, claimant | {"label": "someone"}, {"to": "not-me", "body": "control"})
    P.rebuild(owner)
    body = client.get("/v1/mesh/inbox", **_auth(me["peer_id"])).json()
    assert body["label"] == me["name"] and body["label_source"] == "peers.name"
    fallback = f"peer-{str(me['peer_id'])[:8]}"
    assert body["labels"] == [{"label": me["name"], "source": "peers.name"},
                              {"label": fallback, "source": "fallback"}]
    assert [h["key"] for h in body["hits"]] == [k]
    assert body["label"] != f"peer-{str(me['peer_id'])[:8]}", "must not be the fallback"


def test_the_roster_overrides_the_primary_label_and_both_are_answered(client, owner):
    """Measured on the production standby: for `nxxxxx` the roster says `nxxxxx-cc` and
    every addressed message says `nxxxxx`; for `bxxxxx` the roster says `memory-bxxxxx`
    and nearly every message says that. So both labels are answered for, the roster's
    first, and each hit says which label reached it."""
    claimant = _mesh_claimant(owner)
    me = _peer(owner, "seat")
    _grant(owner, NS, me, claimant)
    roster_label = f"{me['name']}-cc"
    handlers.prime_roster_labels(json.dumps({"peers": {str(me["peer_id"]): roster_label}}).encode())
    sender = claimant | {"label": "someone"}
    by_name = _row(owner, sender, {"to": me["name"]})["key"]
    by_roster = _row(owner, sender, {"to": [roster_label]})["key"]
    _row(owner, sender, {"to": "neither"})
    P.rebuild(owner)
    body = client.get("/v1/mesh/inbox", **_auth(me["peer_id"])).json()
    assert body["label"] == roster_label and body["label_source"] == "roster"
    assert body["labels"] == [{"label": roster_label, "source": "roster"},
                              {"label": me["name"], "source": "peers.name"},
                              {"label": f"peer-{str(me['peer_id'])[:8]}",
                               "source": "fallback"}]
    assert {(h["key"], h["dst"]) for h in body["hits"]} == {
        (by_name, me["name"]), (by_roster, roster_label)}


def test_owed_answers_for_every_label_and_a_self_authored_ask_is_not_owed(client, owner):
    claimant = _mesh_claimant(owner)
    me = _peer(owner, "owes")
    _grant(owner, NS, me, claimant)
    roster_label = f"{me['name']}-cc"
    handlers.prime_roster_labels(json.dumps({"peers": {str(me["peer_id"]): roster_label}}).encode())
    t = f"t-{uuid.uuid4().hex[:6]}"
    asked = _row(owner, claimant | {"label": "asker"},
                 {"to": me["name"], "thread_id": t, "requires_ack": True})["key"]
    # One second later, authored under the ROSTER label and addressed to the name: P
    # asked itself, so it is not owed, and P has now spoken in thread t.
    _row(owner, me | {"label": roster_label},
         {"to": me["name"], "thread_id": t, "requires_ack": True},
         created_at=_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=1))
    P.rebuild(owner)
    body = client.get("/v1/mesh/owed", **_auth(me["peer_id"])).json()
    assert [o["hit"]["key"] for o in body["owed"]] == [asked], body["owed"]
    assert body["owed"][0]["answered_in_thread"] is True, (
        "P spoke in the thread under its roster label after the ask")


def test_owed_recognises_the_callers_own_write_under_the_fallback_author_label(client, owner):
    """With no roster loaded this server stamps the caller's writes `peer-<id8>`
    (apps/memoryskills/handlers.py:614). The caller is addressed by peers.name. An ask the
    caller sent itself must not be owed, and speaking under the fallback must count as
    speaking. The control is an ask from someone else in the same thread: owed."""
    claimant = _mesh_claimant(owner)
    me = _peer(owner, "fbauthor")
    _grant(owner, NS, me, claimant)
    fallback = f"peer-{str(me['peer_id'])[:8]}"
    t = f"t-{uuid.uuid4().hex[:6]}"
    theirs = _row(owner, claimant | {"label": "asker"},
                  {"to": me["name"], "thread_id": t, "requires_ack": True})["key"]
    _row(owner, me | {"label": fallback},
         {"to": me["name"], "thread_id": t, "requires_ack": True},
         created_at=_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=1))
    P.rebuild(owner)
    body = client.get("/v1/mesh/owed", **_auth(me["peer_id"])).json()
    assert [o["hit"]["key"] for o in body["owed"]] == [theirs], body["owed"]
    assert body["owed"][0]["answered_in_thread"] is True


def test_neighbours_declares_an_unprojected_anchors_routing_until_catch_up(client, owner, monkeypatch):
    """A live row has its own declared routing before the minute projection tick, then the
    same routing comes from edges. MUTANT: remove the value readers in related_reads.neighbours;
    the pre-catch-up answer loses its thread and reply. MUTANT: omit size_is_projected_count;
    the answer no longer says why its size excludes the anchor. The embed gate is OPEN here so the
    body's mention is projected; the closed case is the next test."""
    monkeypatch.setattr("apps.meshsem.store.forbidden_rows_open", lambda: True)
    claimant = _mesh_claimant(owner)
    headers = _auth(claimant["peer_id"])
    thread = f"ops/fresh-{uuid.uuid4().hex[:6]}"
    parent = _row(owner, claimant | {"label": "parent"}, {"thread_id": thread})["key"]
    P.rebuild(owner)
    key = _row(owner, claimant | {"label": "sender"}, {
        "to": ["otter", "badger"], "thread_id": thread, "in_reply_to": parent,
        "requires_ack": True, "done_when": "post the receipt", "closes": [parent],
        "body": "Please inspect #77.",
    })["key"]
    url = f"/v1/mesh/neighbours?key={key}"
    before = client.get(url, **headers).json()
    assert before["anchor"] == {
        "key": key, "version": 1, "recorded_at": before["anchor"]["recorded_at"],
        "author": "sender", "thread_id": thread, "projected": False,
            "thread_source": "value", "owes_ack": ["badger", "otter"],
        "done_when": "post the receipt",
    }
    assert before["declared"]["replies_to"]["key"] == parent
    assert before["declared"]["replies_to"]["tier"] == "declared_unprojected"
    assert before["declared"]["thread"] == {
        "thread_id": thread, "size": 1, "size_is_projected_count": True,
    }
    assert before["declared"]["mentions"] == ["gh:#77"]
    assert before["declared"]["closes"] == [parent]

    P.catch_up(owner)
    after = client.get(url, **headers).json()
    for field in ("thread_id", "owes_ack", "done_when"):
        assert after["anchor"][field] == before["anchor"][field]
    for field in ("key", "found", "version", "author", "recorded_at"):
        assert after["declared"]["replies_to"][field] == before["declared"]["replies_to"][field]
    assert after["declared"]["mentions"] == before["declared"]["mentions"]
    assert after["declared"]["closes"] == before["declared"]["closes"]
    assert after["anchor"]["projected"] is True and after["anchor"]["thread_source"] == "edges"
    assert after["declared"]["replies_to"]["tier"] == "declared"
    assert after["declared"]["thread"]["size"] == 2

    no_thread = _row(owner, claimant | {"label": "sender"}, {"to": "otter"})["key"]
    missing = client.get(f"/v1/mesh/neighbours?key={no_thread}", **headers).json()
    assert missing["anchor"]["thread_id"] is None and missing["anchor"]["projected"] is False
    assert missing["declared"]["thread"] is None
    # This suite shares one throwaway database. Remove the projected rows this live-mail
    # proof created, so a later drift test sees only its own direct inserts.
    with rls.projection_writer(owner):
        owner.execute("DELETE FROM mesh_edges WHERE src_key = ANY(%s)", ([parent, key, no_thread],))
    owner.execute("DELETE FROM shared_memory WHERE key = ANY(%s)", ([parent, key, no_thread],))


def test_neighbours_unprojected_fallback_applies_the_mention_embed_gate(client, owner, monkeypatch):
    """Mentions are derived from the body, and the projection derives them only when the embed gate is
    open or the row's embed_policy is 'allowed'. The unprojected fallback must apply the same gate, or a
    forbidden row's mentions are served for the minute before catch-up and vanish after it.

    MUTANT: call project_row without mentions_allowed in the fallback; the forbidden row shows gh:#78
    before catch-up."""
    monkeypatch.setattr("apps.meshsem.store.forbidden_rows_open", lambda: False)
    claimant = _mesh_claimant(owner)
    headers = _auth(claimant["peer_id"])
    key = _row(owner, claimant | {"label": "sender"}, {
        "to": ["otter"], "thread_id": f"ops/gated-{uuid.uuid4().hex[:6]}", "body": "Please inspect #78.",
    })["key"]
    policy = owner.execute("SELECT embed_policy FROM shared_memory WHERE key = %s", (key,)).fetchone()[0]
    assert policy != "allowed", policy
    url = f"/v1/mesh/neighbours?key={key}"
    before = client.get(url, **headers).json()
    assert before["anchor"]["projected"] is False
    assert before["declared"]["mentions"] == []
    P.catch_up(owner)
    after = client.get(url, **headers).json()
    assert after["anchor"]["projected"] is True
    assert after["declared"]["mentions"] == before["declared"]["mentions"] == []
    with rls.projection_writer(owner):
        owner.execute("DELETE FROM mesh_edges WHERE src_key = %s", (key,))
    owner.execute("DELETE FROM shared_memory WHERE key = %s", (key,))


@pytest.mark.parametrize("value", [None, "a scalar", 7, ["a", "list"]])
def test_neighbours_answers_for_a_head_whose_value_is_not_an_object(client, owner, value):
    """Codex on #93: a msg/ head whose value is JSON null, a scalar or an array never gets an edge (the
    projection skips it as data), so it stays in the unprojected branch for good, where the readers take an
    object: the route answered 500 where it used to answer. It has no thread, parent or edges.

    MUTANT: drop the guard; the route answers 500."""
    claimant = _mesh_claimant(owner)
    headers = _auth(claimant["peer_id"])
    key = _row(owner, claimant | {"label": "sender"}, value)["key"]
    try:
        r = client.get(f"/v1/mesh/neighbours?key={key}", **headers)
        assert r.status_code == 200, r.content
        body = r.json()
        assert body["anchor"]["projected"] is False and body["anchor"]["thread_id"] is None
        assert body["anchor"]["owes_ack"] == [] and body["declared"]["replies_to"] is None
        assert body["declared"]["thread"] is None and body["declared"]["mentions"] == []
    finally:
        owner.execute("DELETE FROM shared_memory WHERE key = %s", (key,))


def test_neighbours_declared_answer_does_not_need_the_semantic_space(client, owner, monkeypatch):
    """The configured space is read only for the inferred tier. With an unknown space the declared
    answer still serves, and include_inferred=1 is the 503 E_SPACE_UNKNOWN control.

    MUTANT: resolve the space before checking include_inferred; the declared read answers 503."""
    def unknown():
        raise KeyError("space nope/0/x is not configured")

    monkeypatch.setattr("apps.meshsem.store.configured_space", unknown)
    claimant = _mesh_claimant(owner)
    headers = _auth(claimant["peer_id"])
    key = _row(owner, claimant | {"label": "sender"}, {
        "to": ["otter"], "thread_id": f"ops/nospace-{uuid.uuid4().hex[:6]}",
    })["key"]
    declared = client.get(f"/v1/mesh/neighbours?key={key}", **headers)
    assert declared.status_code == 200, declared.content
    body = declared.json()
    assert body["anchor"]["key"] == key and body["inferred"] == {"included": False}
    inferred = client.get(f"/v1/mesh/neighbours?key={key}&include_inferred=1", **headers)
    assert inferred.status_code == 503 and inferred.json()["error"] == "E_SPACE_UNKNOWN", inferred.content
    owner.execute("DELETE FROM shared_memory WHERE key = %s", (key,))


# ============================================================ (c) /v1/mesh/wait

def _wait(client, peer_id, *, since=None, timeout=None, ttl=300):
    q = []
    if since is not None:
        q.append(f"since={since}")
    if timeout is not None:
        q.append(f"timeout={timeout}")
    url = "/v1/mesh/wait" + ("?" + "&".join(q) if q else "")
    started = time.monotonic()
    r = client.get(url, **_auth(peer_id, ttl))
    return r, time.monotonic() - started


def test_wait_is_204_when_idle_within_the_timeout_and_200_with_a_cursor_after_a_write(
        client, owner, monkeypatch):
    """The two outcomes a node's wait loop branches on. Idle: 204 after
    roughly `timeout` seconds, not before and not much after. Then a row written WHILE the
    waiter is parked wakes it with 200, the row, and a cursor naming exactly that row."""
    W = _mod("wait")
    monkeypatch.setattr(W, "POLL_SECS", 0.1)
    ns, sender = _private_ns(owner, "wake")
    receiver = _peer(owner, "wake-recv")
    _grant(owner, ns, receiver, sender)

    r, took = _wait(client, receiver["peer_id"], timeout=1)
    assert r.status_code == 204 and r.content == b"", (r.status_code, r.content[:200])
    # The lower bound is the claim (it WAITED); the upper one only has to exclude a hang.
    # It was 4 s and one mutation run on this 0.3 GiB-free box failed here on clean
    # behaviour, so it is loose on purpose: timing is not what this test is about.
    assert 0.9 <= took < 8.0, f"idle wait took {took:.2f}s for timeout=1"

    result = {}
    # The row must land AFTER the waiter took its starting cursor, or it is not newer and the wait rightly ends 204
    # (mg412c, 26 Oct: a fixed 0.5 s head start was not enough on a loaded gate box). The view parks through
    # mesh_wait.park only after first_read fixed that cursor, so the row is written once park is entered.
    parked = threading.Event()
    real_park = W.park

    def park_and_tell(*args, **kwargs):
        parked.set()
        return real_park(*args, **kwargs)

    monkeypatch.setattr(W, "park", park_and_tell)

    def park():
        result["r"], result["took"] = _wait(Client(), receiver["peer_id"], timeout=10)

    t = threading.Thread(target=park)
    t.start()
    assert parked.wait(15), "the waiter never parked"
    landed = _row(owner, sender, {"from": sender["name"], "to": "anyone"}, ns=ns)
    t.join(timeout=15)
    assert not t.is_alive(), "the waiter never returned"
    r = result["r"]
    assert r.status_code == 200, (r.status_code, r.content[:300])
    # 200 already proves the wake (an unwoken wait ends 204 at 10 s); this only
    # excludes a wake that happened to coincide with the deadline.
    assert result["took"] < 9.0, f"woke after {result['took']:.2f}s, too close to the 10 s deadline"
    body = json.loads(r.content)
    assert [m["key"] for m in body["messages"]] == [landed["key"]]
    assert body["messages"][0]["from"] == sender["name"]
    assert body["messages"][0]["to"] == "anyone"
    assert W.decode_cursor(body["cursor"]) == W.Cursor(W.micros_of(landed["created_at"]),
                                                       landed["id"])


def test_wait_returns_only_strictly_newer_readable_rows_it_did_not_author(client, owner):
    """The cursor's own row is excluded, a row to someone else is INCLUDED (the route is
    addressing-agnostic), the receiver's own row and a row in an ungranted namespace are
    excluded, and the order is oldest first."""
    W = _mod("wait")
    ns, sender = _private_ns(owner, "strict")
    receiver = _peer(owner, "strict-recv")
    _grant(owner, ns, receiver, sender)
    start = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=20)
    old = _row(owner, sender, {"to": "receiver"}, ns=ns, created_at=start)
    direct = _row(owner, sender, {"to": "receiver"}, ns=ns,
                  created_at=start + _dt.timedelta(seconds=1))
    broadcast = _row(owner, sender, {"body": "no to"}, ns=ns,
                     created_at=start + _dt.timedelta(seconds=2))
    other = _row(owner, sender, {"to": "other"}, ns=ns,
                 created_at=start + _dt.timedelta(seconds=3))
    _row(owner, receiver, {"to": "both"}, ns=ns, created_at=start + _dt.timedelta(seconds=4))
    ungranted, _ = _private_ns(owner, "strict-ungranted")
    _row(owner, sender, {"to": "receiver"}, ns=ungranted,
         created_at=start + _dt.timedelta(seconds=5))

    since = W.encode_cursor(W.micros_of(old["created_at"]), old["id"])
    r, _ = _wait(client, receiver["peer_id"], since=since, timeout=5)
    assert r.status_code == 200, r.content[:300]
    body = json.loads(r.content)
    assert [m["key"] for m in body["messages"]] == [direct["key"], broadcast["key"],
                                                    other["key"]]
    assert body["messages"][1]["to"] is None, "a row with no `to` has a null `to`"
    assert r[W.VISIBLE_NAMESPACES_HEADER] == "1"


def test_wait_under_a_revoked_grant_is_204_and_the_live_grant_is_the_control(client, owner):
    """The same row and cursor reach a second receiver whose grant is live, while a
    revoked grant receives no rows."""
    W = _mod("wait")
    ns, sender = _private_ns(owner, "revoked")
    revoked, live = _peer(owner, "rv-recv"), _peer(owner, "live-recv")
    _grant(owner, ns, revoked, sender, revoked=True)
    _grant(owner, ns, live, sender)
    start = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=20)
    hidden = _row(owner, sender, {"to": "receiver"}, ns=ns,
                  created_at=start + _dt.timedelta(seconds=1))
    since = W.encode_cursor(W.micros_of(start), uuid.UUID(int=0))
    r, _ = _wait(client, revoked["peer_id"], since=since, timeout=1)
    assert r.status_code == 204 and r[W.VISIBLE_NAMESPACES_HEADER] == "0"
    r, _ = _wait(client, live["peer_id"], since=since, timeout=1)
    assert r.status_code == 200
    assert [m["key"] for m in json.loads(r.content)["messages"]] == [hidden["key"]]


def test_wait_with_a_malformed_cursor_starts_at_now_not_at_the_beginning(client, owner):
    """A missing or malformed cursor is not an error; it is "start
    from the newest visible row". So a row that is ALREADY there is not returned (204),
    while the same row IS returned to a well-formed cursor from before it (control)."""
    W = _mod("wait")
    ns, sender = _private_ns(owner, "malformed")
    receiver = _peer(owner, "mal-recv")
    _grant(owner, ns, receiver, sender)
    past = _row(owner, sender, {"to": "x"}, ns=ns,
                created_at=_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=5))
    good = W.encode_cursor(W.micros_of(past["created_at"]) - 1, uuid.UUID(int=0))
    for bad in ("not-a-cursor", good + "=", base64.urlsafe_b64encode(
            b"123:not-a-uuid").decode().rstrip("=")):
        r, _ = _wait(client, receiver["peer_id"], since=bad, timeout=1)
        assert r.status_code == 204, (bad, r.status_code)
    r, _ = _wait(client, receiver["peer_id"], timeout=1)
    assert r.status_code == 204, "no cursor at all behaves like a malformed one"
    r, _ = _wait(client, receiver["peer_id"], since=good, timeout=1)
    assert r.status_code == 200 and [m["key"] for m in json.loads(r.content)["messages"]] == [
        past["key"]]


def test_wait_answers_204_at_once_when_the_token_is_about_to_expire(client, owner):
    """The effective timeout is capped by the token's life minus
    5 s, and a zero timeout is 204 BEFORE any row is read, even with a row pending. The
    control is the same row and cursor under a long-lived token: 200."""
    W = _mod("wait")
    ns, sender = _private_ns(owner, "expiring")
    receiver = _peer(owner, "exp-recv")
    _grant(owner, ns, receiver, sender)
    pending = _row(owner, sender, {"to": "x"}, ns=ns,
                   created_at=_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=5))
    since = W.encode_cursor(W.micros_of(pending["created_at"]) - 1, uuid.UUID(int=0))
    r, took = _wait(client, receiver["peer_id"], since=since, timeout=50, ttl=4)
    assert r.status_code == 204 and took < 1.5, (r.status_code, took)
    r, _ = _wait(client, receiver["peer_id"], since=since, timeout=50, ttl=300)
    assert r.status_code == 200


def test_the_effective_timeout_honors_the_configured_cap():
    """Pins the effective timeout as a pure function, including the cap a test cannot
    afford to wait out."""
    W = _mod("wait")
    now = 1_000_000
    assert W.effective_timeout(None, now + 300, now) == 25          # default
    assert W.effective_timeout(50, now + 300, now) == 50            # the node client's 50
    assert W.effective_timeout(10**6, now + 300, now) == 60         # capped at 60
    assert W.effective_timeout(50, now + 20, now) == 15             # token budget - 5
    assert W.effective_timeout(50, now + 5, now) == 0               # exhausted
    assert W.effective_timeout(50, now - 100, now) == 0             # floors at zero
    assert W.effective_timeout(0, now + 300, now) == 0


def test_parked_waiters_do_not_queue_behind_each_other_under_asgi(owner, monkeypatch):
    """Divergence 018 claims a parked waiter holds its own thread and does not block the
    next. That is a property of Django's ASGI handler (a thread-sensitive context per
    request), not of the WSGI test client every other test here uses, so it is measured
    through the real ASGI application: three waiters with timeout=2, started together,
    must all be 204 in well under the 6 s they would take in series."""
    import asyncio
    from asgiref.testing import ApplicationCommunicator
    from django.core.asgi import get_asgi_application
    app = get_asgi_application()
    ns, sender = _private_ns(owner, "asgi")
    waiters = [_peer(owner, f"asgi-{i}") for i in range(3)]
    for w in waiters:
        _grant(owner, ns, w, sender)

    # pxx's P3 on #612, Codex P2 on #620: the overlap is timed INSIDE the server, around the park itself, so time a
    # request spends queued in ASGI or its executor before the view runs is not counted as waiting.
    import threading
    from apps.mesh import wait as mesh_wait
    real_park, spans, spans_lock = mesh_wait.park, [], threading.Lock()

    def timed_park(*a, **k):
        t0 = time.monotonic()
        try:
            return real_park(*a, **k)
        finally:
            with spans_lock:
                spans.append((t0, time.monotonic()))
    monkeypatch.setattr(mesh_wait, "park", timed_park)

    async def one(peer) -> int:
        comm = ApplicationCommunicator(app, {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "GET", "scheme": "http", "path": "/v1/mesh/wait",
            "raw_path": b"/v1/mesh/wait", "query_string": b"timeout=2", "root_path": "",
            "headers": [(b"host", b"testserver"),
                        (b"authorization", f"Bearer {_mint(peer['peer_id'])}".encode())],
            "client": ("127.0.0.1", 1), "server": ("testserver", 80)})
        await comm.send_input({"type": "http.request", "body": b"", "more_body": False})
        start = await comm.receive_output(timeout=15)
        while True:
            body = await comm.receive_output(timeout=15)
            if not body.get("more_body"):
                break
        return start["status"]

    async def all_three():
        return await asyncio.gather(*(one(w) for w in waiters))

    started = time.monotonic()
    statuses = asyncio.run(all_three())
    took = time.monotonic() - started
    assert statuses == [204, 204, 204], statuses
    # Upper bound 5.5 s, not 4.5: a serial run needs at least 6 s (three timeout=2 waits), so 5.5 still fails any
    # serial implementation on any box. 4.5 refused gate mg608 (12 Nov 2026) at 4.74 s on a loaded run (2:02:17,
    # the slowest of five), the test's only failure in 437 gate logs on HQB.
    assert 1.9 <= took < 5.5, f"three timeout=2 waiters took {took:.2f}s together"
    # The property itself, independent of how loaded the box is: each waiter's park, timed inside the server; if the
    # three parked concurrently, every park began before any of them ended.
    assert len(spans) == 3, spans
    last_start, first_end = max(a for a, _ in spans), min(b for _, b in spans)
    assert last_start < first_end, (
        f"the waits did not overlap: the last started {last_start - min(a for a, _ in spans):.2f}s in, "
        f"after the first had ended at {first_end - min(a for a, _ in spans):.2f}s")


def test_wait_rejects_a_bad_timeout_before_authentication(client, owner):
    """A malformed or repeated `timeout` is 400 before the handler runs, even with no
    bearer, where a good one is 401 (control)."""
    for q in ("timeout=abc", "timeout=-1", "timeout=1.5", "timeout=", "timeout=1&timeout=2",
              "since=a&since=b"):
        assert client.get(f"/v1/mesh/wait?{q}").status_code == 400, q
    assert client.get("/v1/mesh/wait?timeout=1").status_code == 401


def test_wait_answers_503_when_every_waiter_permit_is_taken(client, owner, monkeypatch):
    """There are 512 waiter permits. With none left
    the answer is 503 before authentication; with one, the same request is 401 (control)."""
    W = _mod("wait")
    monkeypatch.setattr(W, "_waiters", threading.BoundedSemaphore(1))
    assert W._waiters.acquire(blocking=False)
    r = client.get("/v1/mesh/wait?timeout=1")
    assert r.status_code == 503 and r.content == b"too many waiters"
    W._waiters.release()
    assert client.get("/v1/mesh/wait?timeout=1").status_code == 401
    assert W.MAX_WAITERS == 512


def test_wait_releases_its_permit_on_every_exit(client, owner, monkeypatch):
    """A leaked permit is a slow outage: after 512 leaks every node gets 503. One permit,
    three different exits (401, 204, 200), then the permit must still be free."""
    W = _mod("wait")
    monkeypatch.setattr(W, "_waiters", threading.BoundedSemaphore(1))
    ns, sender = _private_ns(owner, "permit")
    receiver = _peer(owner, "permit-recv")
    _grant(owner, ns, receiver, sender)
    assert client.get("/v1/mesh/wait?timeout=1").status_code == 401
    assert _wait(client, receiver["peer_id"], timeout=1)[0].status_code == 204
    row = _row(owner, sender, {"to": "x"}, ns=ns,
               created_at=_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=2))
    since = W.encode_cursor(W.micros_of(row["created_at"]) - 1, uuid.UUID(int=0))
    assert _wait(client, receiver["peer_id"], since=since, timeout=1)[0].status_code == 200
    assert W._waiters.acquire(blocking=False), "a permit leaked"
    W._waiters.release()


def test_the_cursor_wire_format_is_canonical():
    """Fixed test values (`rid`, `1_781_423_999_999_123`) pin the cursor format separately
    from a cursor round trip: padding, trailing bits, non-canonical UUID forms, and the
    timestamp range remain strict beyond Python's decoder."""
    W = _mod("wait")
    rid = uuid.UUID("0f8fad5b-d9cb-469f-a165-70867728950e")
    enc = W.encode_cursor(1_781_423_999_999_123, rid)
    assert base64.urlsafe_b64decode(enc + "=" * (-len(enc) % 4)).decode() == (
        f"1781423999999123:{rid}")
    assert "=" not in enc
    assert W.decode_cursor(enc) == W.Cursor(1_781_423_999_999_123, rid)

    def b64(text: str) -> str:
        return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")

    # Accepted UUID text forms.
    for form in (rid.hex, "{" + str(rid) + "}", "urn:uuid:" + str(rid), str(rid).upper()):
        assert W.decode_cursor(b64(f"5:{form}")) == W.Cursor(5, rid), form
    # refused: padding, a stray character, a hyphen Python would drop, empty, no colon.
    padded = b64("5:" + str(rid)) + "=" * (-len(b64("5:" + str(rid))) % 4 or 4)
    for bad in (padded, enc + "!", b64("5:" + rid.hex[:8] + "-" + rid.hex[8:]), "",
                b64("5" + str(rid)), b64("x:" + str(rid)), b64(":" + str(rid))):
        assert W.decode_cursor(bad) is None, bad
    # Non-zero trailing bits: 53 input bytes leave 2 unused bits in the last character.
    # Flipping one is invisible to Python's decoder (asserted, the precondition). A strict
    # base64 decoder rejects it, so the cursor decoder must too.
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    tampered = enc[:-1] + alphabet[alphabet.index(enc[-1]) ^ 1]

    def py(x):
        return base64.urlsafe_b64decode(x + "=" * (-len(x) % 4))

    assert len(py(enc)) == 53 and py(tampered) == py(enc) and tampered != enc
    assert W.decode_cursor(tampered) is None
    # Timestamp range: inside decodes, one past is malformed.
    assert W.decode_cursor(b64(f"{W._MAX_INSTANT_MICROS}:{rid}")) is not None
    assert W.decode_cursor(b64(f"{W._MAX_INSTANT_MICROS + 1}:{rid}")) is None
    assert W.decode_cursor(b64(f"{W._MIN_INSTANT_MICROS}:{rid}")) is not None
    assert W.decode_cursor(b64(f"{W._MIN_INSTANT_MICROS - 1}:{rid}")) is None
    assert W.decode_cursor(b64(f"+5:{rid}")) == W.Cursor(5, rid), "i64::from_str takes '+'"


def test_the_node_clients_own_cursor_is_accepted():
    """What the node client's wait loop sends before its first 200: the conservative
    `<ms*1000>:<nil uuid>` cursor. It must decode, not fall back to now,
    because the fallback would skip the rows the client backdated the cursor to catch."""
    W = _mod("wait")
    raw = f"{1_781_423_999_000 * 1000}:00000000-0000-0000-0000-000000000000".encode()
    client_cursor = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    assert W.decode_cursor(client_cursor) == W.Cursor(1_781_423_999_000_000, uuid.UUID(int=0))


def test_the_200_body_has_canonical_bytes():
    """The 200 body is compact, with fields in fixed order, UTF-8 unescaped, and
    timestamps with a Z (no fraction, 3 digits, or 6, whichever is exact)."""
    W = _mod("wait")
    rid = uuid.uuid4()
    utc = _dt.timezone.utc
    rows = [
        {"key": "msg/1", "from_label": "txxxx", "to_label": '["a", "b"]',
         "created_at": _dt.datetime(2026, 9, 23, 1, 2, 3, tzinfo=utc), "id": uuid.uuid4()},
        {"key": "msg/2", "from_label": None, "to_label": "caf\u00e9",
         "created_at": _dt.datetime(2026, 9, 23, 1, 2, 3, 120000, tzinfo=utc), "id": uuid.uuid4()},
        {"key": "msg/3", "from_label": "x", "to_label": None,
         "created_at": _dt.datetime(2026, 9, 23, 1, 2, 3, 123456, tzinfo=utc), "id": rid},
    ]
    body = W.ok_body(rows)
    cursor = W.encode_cursor(W.micros_of(rows[-1]["created_at"]), rid)
    assert body == (
        '{"messages":['
        '{"key":"msg/1","from":"txxxx","to":"[\\"a\\", \\"b\\"]","created_at":"2026-09-23T01:02:03Z"},'
        '{"key":"msg/2","from":null,"to":"caf\u00e9","created_at":"2026-09-23T01:02:03.120Z"},'
        '{"key":"msg/3","from":"x","to":null,"created_at":"2026-09-23T01:02:03.123456Z"}],'
        f'"cursor":"{cursor}"}}').encode("utf-8")


def test_the_wait_timestamp_is_timefmts_formatter():
    """`rfc3339` was once a fourth copy of the timestamp-formatting rule. It is now
    apps/peers/timefmt.py's formatter, bound under the name this module always had. The
    identity check, rather than output checks alone, stops a copy from quietly returning;
    the bytes it emits are the test above. Safe because the only value reaching it is
    `shared_memory.created_at`, a
    TIMESTAMPTZ and so always aware - the shared formatter refuses a naive datetime."""
    from apps.peers.timefmt import format_rfc3339
    W = _mod("wait")
    assert W.rfc3339 is format_rfc3339


def test_the_visibility_query_agrees_with_check_permission_on_every_grant_state(owner):
    """The wait's `vis` CTE and memory.read's check_permission both read
    `meshy_namespace_permission` since divergence 040 slice 3; before it, `vis` re-expressed
    the predicate in SQL. The two must give the same answer on every state a grant can be in,
    or the long poll and memory.read disagree about who can read a namespace. Agreement cannot
    detect a matching SQL copy: this test passed while `vis` was one;
    tests/test_namespace_permission_predicate.py's follow test can."""
    W = _mod("wait")
    ns, claimant = _private_ns(owner, "agree")
    cases = {"owner": claimant}
    for tag in ("read", "write", "admin", "revoked", "expired", "future", "none"):
        cases[tag] = _peer(owner, f"agree-{tag}")
    for perm in ("read", "write", "admin"):
        _grant(owner, ns, cases[perm], claimant, permission=perm)
    _grant(owner, ns, cases["revoked"], claimant, revoked=True)
    _grant(owner, ns, cases["expired"], claimant,
           expires_at=_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=1))
    _grant(owner, ns, cases["future"], claimant,
           expires_at=_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=1))
    seen = {}
    with rls.server_internal(owner):
        for tag, p in cases.items():
            in_vis = ns in W.visible_namespaces(owner, p["peer_id"])
            try:
                check_permission(ns, p["peer_id"], "read", owner, resource=NAMESPACE_WIDE)
                allowed = True
            except HandlerError:
                allowed = False
            seen[tag] = (in_vis, allowed)
    assert all(v == a for v, a in seen.values()), seen
    assert {t for t, (v, _) in seen.items() if v} == {"owner", "read", "write", "admin",
                                                      "future"}, seen


# ======================================================================= (d) usage

def test_every_route_counts_per_peer_and_outcome_and_drift_reports_it(client, owner):
    """A request increments its (route, day, peer, outcome) row by exactly one; a refused
    request counts as `denied`, not as a use; the drift response carries the rollup."""
    claimant = _mesh_claimant(owner)
    p = _peer(owner, "counted")
    hdrs = _auth(p["peer_id"])
    assert client.get("/v1/mesh/inbox", **hdrs).status_code == 403
    assert _usage_calls(owner, "inbox", p["peer_id"], "denied") == 1
    assert _usage_calls(owner, "inbox", p["peer_id"], "ok") == 0
    _grant(owner, NS, p, claimant)
    for url, route in (("/v1/mesh/inbox", "inbox"), ("/v1/mesh/owed", "owed"),
                       ("/v1/mesh/threads/t-count", "thread"), ("/v1/mesh/drift", "drift")):
        before = _usage_calls(owner, route, p["peer_id"], "ok")
        assert client.get(url, **hdrs).status_code == 200
        assert client.get(url, **hdrs).status_code == 200
        assert _usage_calls(owner, route, p["peer_id"], "ok") == before + 2, route
    assert _wait(client, p["peer_id"], timeout=0)[0].status_code == 204
    assert _usage_calls(owner, "wait", p["peer_id"], "ok") == 1
    assert client.get("/v1/mesh/inbox?limit=x", **hdrs).status_code == 422
    assert _usage_calls(owner, "inbox", p["peer_id"], "error") == 1

    usage = client.get("/v1/mesh/drift", **hdrs).json()["usage"]
    # `neighbours` joined the set with divergence 032 (sql_meshychat/013 widens the CHECK),
    # and the two summary routes with divergence 037 (sql_meshychat/017). 035 adds brief and 036
    # unread; notification variants are counted but intentionally absent from the route summary.
    assert set(usage["routes"]) == {"inbox", "owed", "thread", "drift", "wait", "neighbours",
                                    "summary_thread", "summary_backlog", "mentions", "awaiting", "brief",
                                    "unread", "recipients"}           # 083 adds recipients (sql_meshychat/039)
    assert usage["routes"]["inbox"]["calls"] >= 2 and usage["routes"]["inbox"]["peers"] >= 1
    assert usage["routes"]["inbox"]["denied_calls"] >= 1
    assert usage["window_days"] == 7 and usage["counting_since"] is not None
    # STATUS F10: memory.query is counted where it runs (apps/calls/route.py), a count now,
    # not the typed null 018 (d) reported while it was not dispatched here.
    assert isinstance(usage["memory_query"]["calls"], int), usage["memory_query"]
    assert "memory_query" not in usage["routes"]
    assert isinstance(usage["write_failures_this_process"], int)


def test_the_route_set_in_code_is_the_check_constraint(owner):
    """usage.COUNTED (ROUTES and, since sql_meshychat/024, memory_query) and OUTCOMES against
    the CHECKs in sql_meshychat/005: a route added to one and not the other would fail every
    count for that route (logged, counted)."""
    U = _mod("usage")
    with owner.cursor() as cur:
        cur.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = 'mesh_route_usage'::regclass AND contype = 'c'")
        defs = " ".join(r[0] for r in cur.fetchall())
    for name in U.COUNTED + U.OUTCOMES:
        assert f"'{name}'" in defs, name
    assert defs.count("'::text") == len(U.COUNTED) + len(U.OUTCOMES), defs


def test_a_failed_count_never_fails_the_request_and_is_itself_counted(owner):
    """The counter's failure is observable (WRITE_FAILURES, an ERROR log) and returns
    False; it never raises into the route. The control is a good count returning True."""
    U = _mod("usage")
    p = _peer(owner, "failcount")
    with psycopg.connect(DSN, autocommit=True) as app:
        rls.set_role(app)
        before = U.WRITE_FAILURES
        assert U.record(app, "inbox", p["peer_id"], "ok") is True
        assert U.record(app, "not-a-route", p["peer_id"], "ok") is False
        assert U.WRITE_FAILURES == before + 1
        assert U.record(app, "inbox", p["peer_id"], "ok") is True, (
            "the connection must be usable after a failed count")
    assert _usage_calls(owner, "inbox", p["peer_id"], "ok") == 2


def test_the_usage_table_is_server_internal_only(owner):
    """RLS ENABLE+FORCE with the sql_meshychat/004 policy shape: as meshychat_app with no
    context a read sees nothing and an insert is refused; under server_internal both work."""
    p = _peer(owner, "rls")
    with psycopg.connect(DSN, autocommit=True) as app:
        rls.set_role(app)
        with rls.server_internal(app):
            with app.cursor() as cur:
                cur.execute("INSERT INTO mesh_route_usage (route, day, peer_id, outcome, calls)"
                            " VALUES ('drift', current_date, %s, 'ok', 1)", (p["peer_id"],))
                cur.execute("SELECT count(*) FROM mesh_route_usage WHERE peer_id = %s",
                            (p["peer_id"],))
                assert cur.fetchone()[0] == 1
        with app.cursor() as cur:
            cur.execute("SELECT count(*) FROM mesh_route_usage WHERE peer_id = %s",
                        (p["peer_id"],))
            assert cur.fetchone()[0] == 0
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("INSERT INTO mesh_route_usage (route, day, peer_id, outcome, calls)"
                            " VALUES ('owed', current_date, %s, 'ok', 1)", (p["peer_id"],))
    with owner.cursor() as cur:
        cur.execute("SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = 'mesh_route_usage'")
        assert cur.fetchone() == (True, True)


def test_prune_keeps_ninety_days_and_deletes_older(owner):
    U = _mod("usage")
    p = _peer(owner, "prune")
    with rls.server_internal(owner):
        with owner.cursor() as cur:
            for age in (0, U.RETENTION_DAYS - 1, U.RETENTION_DAYS, U.RETENTION_DAYS + 30):
                cur.execute("INSERT INTO mesh_route_usage (route, day, peer_id, outcome, calls)"
                            " VALUES ('inbox', (now() AT TIME ZONE 'UTC')::date - %s, %s,"
                            " 'ok', 1)", (age, p["peer_id"]))
    with psycopg.connect(DSN, autocommit=True) as app:
        rls.set_role(app)
        assert U.prune(app) >= 2
    with rls.server_internal(owner):
        with owner.cursor() as cur:
            cur.execute("SELECT (now() AT TIME ZONE 'UTC')::date - day FROM mesh_route_usage"
                        " WHERE peer_id = %s ORDER BY 1", (p["peer_id"],))
            assert [r[0] for r in cur.fetchall()] == [0, U.RETENTION_DAYS - 1]


def test_the_usage_verb_runs_as_the_app_role_and_prunes(owner):
    """`python -m apps.mesh usage --prune` is the retention mechanism sql_meshychat/005
    names, so it must run the way an operator runs it: a subprocess, through
    dbconn.serving_connection(), as meshychat_app. A row past retention is gone afterwards and
    a row inside it is not (the control)."""
    import subprocess
    U = _mod("usage")
    p = _peer(owner, "cli")
    with rls.server_internal(owner):
        with owner.cursor() as cur:
            for age in (1, U.RETENTION_DAYS + 5):
                cur.execute("INSERT INTO mesh_route_usage (route, day, peer_id, outcome, calls)"
                            " VALUES ('owed', (now() AT TIME ZONE 'UTC')::date - %s, %s,"
                            " 'ok', 3)", (age, p["peer_id"]))
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    out = subprocess.run([sys.executable, "-m", "apps.mesh", "--dsn", DSN, "usage", "--prune"],
                         cwd=str(Path(__file__).resolve().parent.parent),
                         capture_output=True, text=True, env=env, timeout=120)
    assert out.returncode == 0, f"rc={out.returncode} stderr={out.stderr[-600:]}"
    assert "pruned" in out.stdout and "owed" in out.stdout and "memory.query" in out.stdout
    with rls.server_internal(owner):
        with owner.cursor() as cur:
            cur.execute("SELECT (now() AT TIME ZONE 'UTC')::date - day FROM mesh_route_usage"
                        " WHERE peer_id = %s", (p["peer_id"],))
            assert [r[0] for r in cur.fetchall()] == [1]
