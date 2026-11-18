"""The board over HTTP, through the REAL urlconf, with a REAL peer bearer.

Three things this proves that `tests/test_board_projection.py` cannot:

  1. WIRING. Requests resolve through `meshychat.urls`, not an app-local ROOT_URLCONF, so
     a passing test here means the route is actually reachable. This repository has
     already shipped modules whose route tests passed while the server served 404, which
     is why `ROOT_URLCONF` below is the project's and not the app's.
  2. AUTH. Every board route refuses without a bearer and answers with one. The bearer is
     a genuine EdDSA peer JWT verified against `MESHY_JWT_PUBLIC_KEY_PEM`, minted the same
     way `tests/test_peers.py` mints one.
  3. THE WIRE SHAPE OF ABSENCE. A subject with no acts is `"state": "no_declared_acts"`,
     HTTP 200, with the adoption pair attached. Not a 404, because a 404 is also what an
     unmounted route returns and a caller could not tell the two apart.

DSN NOTE, because it is the exact skew that produced a 44-failure wall in a sibling
checkout on 2026-09-21. `tests/conftest.py` builds a per-run database and publishes it
ONLY as `MESHY_TEST_DSN`. The board's routes open their connection through
`apps.core.dbconn`, whose `dsn()` reads `settings.MESHY_DSN` first and has never heard of
`MESHY_TEST_DSN`. So without the `override_settings` below, fixtures would seed the clone
while every request ran against the shared database, and every authenticated route would
look up a peer that database has never seen. The fixture points settings at the clone.

SEEDS ARE COMMITTED, unlike the projection tests, because the route opens its own
connection and cannot see another connection's uncommitted rows. The per-run database
makes that safe: it is dropped when the session ends.

Run:  pytest tests/test_board_routes.py -v
"""
from __future__ import annotations

import datetime as _dt
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

ISSUER = "meshy-board-test-issuer"
_signing_key = ed25519.Ed25519PrivateKey.generate()
JWT_PRIVATE_PEM = _signing_key.private_bytes(
    encoding=serialization.Encoding.PEM, format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption())
JWT_PUBLIC_PEM = _signing_key.public_key().public_bytes(
    encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo)

import django  # noqa: E402
from django.conf import settings as dj_settings  # noqa: E402

if not dj_settings.configured:  # pragma: no cover - whichever module imports first wins
    django.setup()

import jwt as pyjwt  # noqa: E402
from django.test import Client, override_settings  # noqa: E402

from apps.board import projection as B  # noqa: E402
from tests.fixtures import signed_rows as sr  # noqa: E402

NS = B.MESH_CHAT_NAMESPACE


@pytest.fixture(autouse=True)
def board_settings():
    """The project urlconf, the JWT key the routes verify against, and the DSN the routes
    open. See the module docstring for why the DSN override is not optional."""
    with override_settings(
        ROOT_URLCONF="meshychat.urls",
        ALLOWED_HOSTS=["*"],
        MESHY_JWT_PUBLIC_KEY_PEM=JWT_PUBLIC_PEM,
        MESHY_JWT_ISSUER=ISSUER,
        MESHY_DSN=DSN,
    ):
        yield


@pytest.fixture()
def client():
    return Client()


def _mint(peer_id, ttl=300) -> str:
    now = int(_dt.datetime.now(_dt.timezone.utc).timestamp())
    return pyjwt.encode({"sub": str(peer_id), "iss": ISSUER, "iat": now, "exp": now + ttl,
                         "aud": "peer", "scopes": []}, JWT_PRIVATE_PEM, algorithm="EdDSA")


def _auth(peer_id) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {_mint(peer_id)}"}


@pytest.fixture()
def owner():
    """Owner connection, AUTOCOMMIT: seeds must be visible to the route's own connection."""
    with psycopg.connect(DSN, autocommit=True) as c:
        yield c


def _peer(conn, tag: str) -> dict:
    owner_id, peer_id, group = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id)"
                    " VALUES (%s,%s,'dev',%s)",
                    (owner_id, f"brd-{tag}-{owner_id.hex[:8]}@example.test", group))
        cur.execute("INSERT INTO peers (id, owner_id, name, display_name) VALUES (%s,%s,%s,%s)",
                    (peer_id, owner_id, f"brd-{tag}-{peer_id.hex[:8]}", f"brd {tag}"))
        cur.execute("INSERT INTO shared_memory_namespace (namespace, owner_peer_id,"
                    " owner_owner_id, claimant_plane, ephemeral, retention_policy)"
                    " VALUES (%s,%s,%s,'dev',FALSE,'keep_all') ON CONFLICT DO NOTHING",
                    (NS, peer_id, owner_id))
    return {"peer_id": peer_id, "owner_id": owner_id, "label": tag}


def _act(conn, peer, subject, verb, ref="abc123"):
    rid = uuid.uuid4()
    # `ts` is the actor's CLAIMED clock and the projection flags it when it disagrees
    # with the server's `created_at` by more than five minutes. A fixed literal here
    # made every act look six hours skewed and failed the clock_suspect assertion; the
    # claim has to be "now", in the exact shape MESH_TIMESTAMP_RE admits.
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    value = {"schema_version": 1, "to": ["all"], "from": peer["label"], "type": verb,
             "thread_id": "t1", "ts": ts, "body": f"{verb} {subject}",
             "author_family": "claude", "subject": subject, "ref": ref}
    # With the bytes a writer would have signed: since A2 (divergence 051) a row without
    # them is refused from every session.
    sr.insert(conn, sr.row(
        peer["peer_id"], peer["owner_id"], NS,
        f"msg/{int(_dt.datetime.now().timestamp()*1000)}-{rid.hex}", value, rid=rid,
        call_id=f"call-{rid}", author_label=peer["label"]))


def _subj(tag: str) -> str:
    return f"pr/{tag}-{uuid.uuid4().hex[:8]}"


# ------------------------------------------------------------------- wiring + auth

def test_the_settings_override_is_in_effect():
    """Positive control for the autouse fixture. Without it, every 401/404 below would
    have two explanations."""
    assert dj_settings.ROOT_URLCONF == "meshychat.urls"
    assert dj_settings.MESHY_DSN == DSN
    assert dj_settings.MESHY_JWT_ISSUER == ISSUER


@pytest.mark.parametrize("path", ["/v1/board/subjects", "/v1/board/subjects/pr/1",
                                  "/v1/board/graph", "/v1/board/me"])
def test_every_board_route_refuses_without_a_bearer(client, path):
    r = client.get(path)
    assert r.status_code == 401, f"{path}: {r.status_code} {r.content[:120]!r}"


def test_a_board_route_is_not_a_404_through_the_project_urlconf(client, owner):
    """The wiring assertion. A 404 here means the module exists and is not mounted."""
    p = _peer(owner, "wire")
    r = client.get("/v1/board/subjects", **_auth(p["peer_id"]))
    assert r.status_code == 200, r.content[:200]


def test_post_is_refused_the_board_is_read_only(client, owner):
    p = _peer(owner, "ro")
    r = client.post("/v1/board/subjects", **_auth(p["peer_id"]))
    assert r.status_code == 405
    assert r.headers.get("Allow") == "GET"


# ------------------------------------------------------------------- the wire shape

def test_absence_is_a_named_state_with_the_denominator_not_a_404(client, owner):
    p = _peer(owner, "absent")
    s = _subj("nothing")
    r = client.get(f"/v1/board/subjects/{s}", **_auth(p["peer_id"]))
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "no_declared_acts"
    assert "acts" not in body, "absence must not carry an empty acts list (B3)"
    assert set(body["adoption"]) == {"declared", "total"}
    assert any("adoption" in line.lower() for line in body["prose"])


def test_a_held_subject_comes_back_as_acts_with_the_authenticated_actor(client, owner):
    uxxx = _peer(owner, "uxxx")
    s = _subj("held")
    _act(owner, uxxx, s, "reviewed", ref="d4abcb7")
    r = client.get(f"/v1/board/subjects/{s}", **_auth(uxxx["peer_id"]))
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "acts"
    assert body["latest_by_verb"]["reviewed"]["actor_peer_id"] == str(uxxx["peer_id"])
    assert body["latest_by_verb"]["reviewed"]["ref"] == "d4abcb7"
    assert "merged" not in body["latest_by_verb"]
    assert body["clock_suspect"] is False
    assert any("reviewed" in line for line in body["prose"])


def test_subject_with_slashes_survives_the_url(client, owner):
    """`pr/933` has a slash; a `<str:>` converter would 404 it. `<path:>` is load-bearing."""
    p = _peer(owner, "slash")
    s = _subj("a/b")                     # two slashes once the prefix is counted
    _act(owner, p, s, "opened")
    r = client.get(f"/v1/board/subjects/{s}", **_auth(p["peer_id"]))
    assert r.status_code == 200 and r.json()["state"] == "acts", r.content[:200]


# ------------------------------------------------------------------------ the graph

def test_graph_returns_edges_and_both_adjacencies(client, owner):
    a, b = _peer(owner, "ga"), _peer(owner, "gb")
    s1, s2 = _subj("g1"), _subj("g2")
    _act(owner, a, s1, "opened", ref="r1")
    _act(owner, b, s1, "reviewed", ref="r1")
    _act(owner, a, s2, "opened", ref="r2")
    r = client.get("/v1/board/graph", **_auth(a["peer_id"]))
    assert r.status_code == 200
    body = r.json()
    mine = [e for e in body["edges"] if e["subject"] in (s1, s2)]
    assert len(mine) == 3
    assert body["truncated"] is False
    adj = body["adjacency"]
    assert set(adj["by_subject"][s1]) == {"ga", "gb"}, "who touched s1"
    assert adj["by_actor"]["ga"][s1] == ["opened@r1"] and adj["by_actor"]["ga"][s2] == ["opened@r2"]


def test_graph_filters_by_ref_and_reports_truncation(client, owner):
    p = _peer(owner, "gr")
    ref = f"sha-{uuid.uuid4().hex[:10]}"
    for i in range(3):
        _act(owner, p, _subj(f"r{i}"), "reviewed", ref=ref)
    r = client.get(f"/v1/board/graph?ref={ref}", **_auth(p["peer_id"]))
    assert len(r.json()["edges"]) == 3 and r.json()["truncated"] is False
    r = client.get(f"/v1/board/graph?ref={ref}&limit=2", **_auth(p["peer_id"]))
    assert len(r.json()["edges"]) == 2 and r.json()["truncated"] is True, (
        "a cap that bit must say so, or the caller shows a prefix and calls it the graph"
    )


def test_me_returns_only_the_calling_peers_acts(client, owner):
    me, other = _peer(owner, "me"), _peer(owner, "other")
    s = _subj("mine")
    _act(owner, me, s, "opened")
    _act(owner, other, _subj("theirs"), "opened")
    r = client.get("/v1/board/me", **_auth(me["peer_id"]))
    assert r.status_code == 200
    body = r.json()
    assert body["actor_peer_id"] == str(me["peer_id"])
    subjects = {e["subject"] for e in body["edges"]}
    assert s in subjects
    assert all(e["actor_peer_id"] == str(me["peer_id"]) for e in body["edges"])
