"""GET /v1/mesh/search and /v1/mesh/context through the REAL urlconf, with a real peer
bearer, a real database and the deterministic fake model host. Divergence 019.

What every test here holds the routes to:
  * authentication (401) and the namespace's READ grant (403), each beside a 200;
  * coverage, the space, the provider ids actually used and `reranked` on EVERY
    response, 200 and 503 alike, and `reranked: true` only when a reranker ran;
  * the ranking (divergence 026): reciprocal-rank fusion of the lexical and embedding
    arms, then the reranker unless `rerank=0` (026, as amended), `method` and `arms` on
    every ranked response, and an unreachable embedder degrading to lexical-only WITH
    THAT STATED, never to an empty 200; a reranker that is down or hung never fails a
    request, and a hung one costs one request its timeout, not every request;
  * the embed_policy gate applied at READ time, not only by the job;
  * the request path writes nothing: the vectors, the lexical rows, their state row and
    mesh_edges are snapshotted around requests and must be unchanged.

ISOLATION. The namespace is shared by every test in the run, and the embed job walks all
of it into each test's new space, so `world` records the commit order's floor before the
test writes anything and `_drain` cuts the space back to the rows committed after it: both
arms rank this test's rows and no others (OI-05; tests/fixtures/meshsem_db.py
keep_only_rows_after has the measurement).

Run:  pytest tests/test_meshsem_routes.py -q
"""
from __future__ import annotations

import datetime as _dt
import os
import sys
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

ISSUER = "meshy-meshsem-test-issuer"
_key = ed25519.Ed25519PrivateKey.generate()
PRIV = _key.private_bytes(encoding=serialization.Encoding.PEM,
                          format=serialization.PrivateFormat.PKCS8,
                          encryption_algorithm=serialization.NoEncryption())
PUB = _key.public_key().public_bytes(encoding=serialization.Encoding.PEM,
                                     format=serialization.PublicFormat.SubjectPublicKeyInfo)

import django  # noqa: E402
from django.conf import settings as dj_settings  # noqa: E402

if not dj_settings.configured:  # pragma: no cover
    django.setup()

import jwt as pyjwt  # noqa: E402
from django.test import Client, override_settings  # noqa: E402

from apps.mesh import projection as mesh_projection  # noqa: E402
from apps.meshsem import embedjob, fusion, search, spaces, store  # noqa: E402
from tests.fixtures import meshsem_db as db  # noqa: E402
from tests.fixtures.meshsem_fake import FakeModelHost  # noqa: E402


def _bearer(peer_id, *, cnf: str | None = None) -> dict:
    now = int(_dt.datetime.now(_dt.timezone.utc).timestamp())
    claims = {"sub": str(peer_id), "iss": ISSUER, "iat": now, "exp": now + 300,
              "aud": "peer", "scopes": []}
    if cnf is not None:
        claims["cnf"] = cnf
    tok = pyjwt.encode(claims, PRIV, algorithm="EdDSA")
    return {"HTTP_AUTHORIZATION": f"Bearer {tok}"}


@pytest.fixture()
def owner():
    with psycopg.connect(DSN, autocommit=True) as c:
        db.ensure_namespace(c)
        yield c


#: The memory authority every test here points MESHY_MEMORY_PEER_ID at, made once per
#: run in its own trust group. `_reader` puts readers in that group with an active key, so
#: memory.read's invoke path would admit them, which the context anchor's body now asks
#: for (divergence 027). A test that changes the authority itself makes its own.
_MEMORY: dict = {}


def _memory_authority() -> dict:
    if not _MEMORY:
        with psycopg.connect(DSN, autocommit=True) as c:
            _MEMORY.update(db.memory_authority(c))
    return _MEMORY


class World:
    def __init__(self, space, embed_host, rerank_host):
        self.space, self.embed_host, self.rerank_host = space, embed_host, rerank_host
        self.memory_peer_id = _memory_authority()["peer_id"]
        self._ctx = None
        self.declare()

    def declare(self, *, embed=True, rerank=True, gate=None, embed_host=None):
        if self._ctx is not None:
            self._ctx.disable()
        providers = []
        if embed:
            providers.append({"id": "embed-hqb", "kind": "embed",
                              "base_url": (embed_host or self.embed_host).base_url,
                              "space": self.space.name})
        if rerank:
            providers.append({"id": "rerank-hqb", "kind": "rerank",
                              "base_url": self.rerank_host.base_url})
        self._ctx = override_settings(
            ROOT_URLCONF="meshychat.urls", ALLOWED_HOSTS=["*"], MESHY_DSN=DSN,
            MESHY_JWT_PUBLIC_KEY_PEM=PUB, MESHY_JWT_ISSUER=ISSUER,
            MESHY_SEMANTIC_SPACE=self.space.name, MESHY_MODEL_PROVIDERS=providers,
            MESHY_EMBED_FORBIDDEN_ROWS=gate, MESHY_MEMORY_PEER_ID=str(self.memory_peer_id))
        self._ctx.enable()
        search.forget()

    def close(self):
        if self._ctx is not None:
            self._ctx.disable()


#: Each live world's commit floor (tests/fixtures/meshsem_db.commit_floor), by its space's
#: name: `world` sets it, its monkeypatch removes it, `_drain` cuts the configured space at
#: it. Keyed like spaces.SPACES, so `_drain` needs no world passed in.
_FLOOR: dict[str, int] = {}


@pytest.fixture()
def world(monkeypatch):
    space = db.make_space("route")
    monkeypatch.setitem(spaces.SPACES, space.name, space)
    with psycopg.connect(DSN, autocommit=True) as c:        # before the test writes a row
        monkeypatch.setitem(_FLOOR, space.name, db.commit_floor(c))
    eh, rh = FakeModelHost(dims=db.DIMS), FakeModelHost(dims=db.DIMS)
    w = World(space, eh, rh)
    yield w
    w.close()
    eh.close()
    rh.close()
    search.forget()


@pytest.fixture()
def client():
    return Client()


def _member(owner, tag="member") -> dict:
    """A peer memory.read's invoke path admits (the memory authority's trust group, an
    active signing key) and that holds NO namespace grant."""
    return db.peer(owner, tag, group=_memory_authority()["group"], key=True)


def _reader(owner, tag="reader") -> dict:
    p = _member(owner, tag)
    db.grant(owner, p, "read")
    return p


def _drain(conn):
    """Walk the embed job to the end, then cut the configured space (the test's `world`)
    back to the rows committed after its floor. The job walks every message an earlier
    test left into a new space; uncut, they are ranked beside this test's rows, and that
    is how the rerank=0 control below refused a release gate (OI-05,
    tests/fixtures/meshsem_db.keep_only_rows_after). After every walk, because a gate
    change restarts the walk and embeds them again."""
    r = None
    for _ in range(200):
        r = embedjob.catch_up(conn)
        if not r.more or r.rows_processed == 0:
            break
    space = store.configured_space()
    db.keep_only_rows_after(conn, space, _FLOOR[space.name])
    return r


def _project_edges(conn):
    for _ in range(200):
        if not mesh_projection.catch_up(conn).more:
            return


#: On EVERY response, 200, 404 and 503 alike. The ranking fields (divergence 026) are
#: present even where nothing was ranked, null then, so a client never has to guess.
ENVELOPE = ("space", "coverage", "providers", "reranked", "method", "rrf_k", "arms",
            "degraded")


def _assert_envelope(body: dict, space) -> None:
    for k in ENVELOPE:
        assert k in body, f"response lacks {k!r}: {sorted(body)}"
    assert body["space"] == space.name
    for k in ("embedded", "total", "skipped_forbidden", "lexical_indexed"):
        assert isinstance(body["coverage"][k], int), body["coverage"]
    assert set(body["providers"]) == {"embed", "rerank"}


# ======================================================================= auth

def test_both_routes_refuse_without_a_bearer(client, world):
    for url in ("/v1/mesh/search?q=x", "/v1/mesh/context?key=msg/1-x"):
        r = client.get(url)
        assert r.status_code == 401 and r.json()["error"] == "E_AUTH_MISSING", url


def test_both_routes_refuse_a_peer_without_read_permission(client, owner, world):
    """403 with the namespace layer's own code; the positive control is the SAME request
    by a peer holding a read grant, which reaches the handler (it answers 200 or a typed
    503, never 403)."""
    stranger = db.peer(owner, "stranger")
    reader = _reader(owner)
    _drain(owner)
    for url in ("/v1/mesh/search?q=alpha", "/v1/mesh/context?key=msg/0-none"):
        denied = client.get(url, **_bearer(stranger["peer_id"]))
        assert denied.status_code == 403, (url, denied.content[:200])
        assert denied.json()["error"] == "E_PERMISSION_DENIED"
        allowed = client.get(url, **_bearer(reader["peer_id"]))
        assert allowed.status_code in (200, 404), (url, allowed.content[:300])
        _assert_envelope(allowed.json(), world.space)


def test_a_revoked_grant_is_no_grant(client, owner, world):
    p = _reader(owner, "revoked")
    _drain(owner)
    assert client.get("/v1/mesh/search?q=a", **_bearer(p["peer_id"])).status_code == 200
    with owner.cursor() as cur:
        cur.execute("UPDATE shared_memory_grant SET revoked = TRUE, revoked_at = now() "
                    "WHERE grantee_peer_id = %s", (p["peer_id"],))
    assert client.get("/v1/mesh/search?q=a", **_bearer(p["peer_id"])).status_code == 403


# ======================================================================= search

def test_search_ranks_reranks_and_carries_the_envelope(client, owner, world):
    reader = _reader(owner)
    near = db.row(owner, reader, {"body": "tenant row security policy chi"},
                  embed_policy="allowed")
    db.row(owner, reader, {"body": "bananas ripen beside apples psi"}, embed_policy="allowed")
    _drain(owner)

    r = client.get("/v1/mesh/search?q=tenant+row+security&k=1&rerank=1",
                   **_bearer(reader["peer_id"]))
    assert r.status_code == 200, r.content[:400]
    body = r.json()
    _assert_envelope(body, world.space)
    assert body["reranked"] is True and body["providers"] == {"embed": "embed-hqb",
                                                              "rerank": "rerank-hqb"}
    assert body["method"] == "rrf(lexical,embed)+rerank"
    assert [h["key"] for h in body["hits"]] == [near["key"]]
    assert body["hits"][0]["rerank_score"] is not None
    assert body["truncated"] is True, "k=1 of a larger pool must say it cut"
    assert body["coverage"]["embedded"] >= 2
    assert body["query_vector"]["canary_min_cosine"] >= spaces.ADMISSION_COSINE


def test_a_down_reranker_says_reranked_false_and_still_answers(client, owner, world):
    """The fleet trap this refuses: a tool claiming reranked after falling back."""
    reader = _reader(owner)
    db.row(owner, reader, {"body": "omega fallback words"}, embed_policy="allowed")
    _drain(owner)
    world.rerank_host.rerank_mode = "down"
    body = client.get("/v1/mesh/search?q=omega+fallback&rerank=1",
                      **_bearer(reader["peer_id"])).json()
    assert body["reranked"] is False and body["providers"]["rerank"] is None
    assert body["rerank_requested"] is True and body["hits"], "first-stage order still served"
    assert body["method"] == "rrf(lexical,embed)", "a fallback must not claim +rerank"
    assert all(h["rerank_score"] is None for h in body["hits"])
    assert body["attempts"]["rerank"] == [{"id": "rerank-hqb", "verdict": "failed",
                                           "reason": "HTTP 503",
                                           "ms": body["attempts"]["rerank"][0]["ms"]}]
    _assert_envelope(body, world.space)

    world.rerank_host.rerank_mode = "good"                    # positive control
    body = client.get("/v1/mesh/search?q=omega+fallback&rerank=1",
                      **_bearer(reader["peer_id"])).json()
    assert body["reranked"] is True

    calls = world.rerank_host.calls("/rerank")
    body = client.get("/v1/mesh/search?q=omega&rerank=0", **_bearer(reader["peer_id"])).json()
    assert body["reranked"] is False and body["rerank_requested"] is False
    assert world.rerank_host.calls("/rerank") == calls, "rerank=0 still called a reranker"


def test_an_unreferenced_space_is_a_typed_503_with_coverage(client, owner, world):
    reader = _reader(owner)
    r = client.get("/v1/mesh/search?q=anything", **_bearer(reader["peer_id"]))
    assert r.status_code == 503 and r.json()["error"] == "E_SPACE_UNREFERENCED"
    _assert_envelope(r.json(), world.space)
    assert world.embed_host.requests == [], "a request must not record the reference"


def test_an_unavailable_embedder_is_a_typed_503_with_coverage(client, owner, world):
    """With nothing the lexical arm can match either, the answer is the embed arm's typed
    503, never an empty 200 (divergence 026). The envelope says the lexical arm RAN and
    ranked nothing, so the 503 is not read as 'search is down'."""
    reader = _reader(owner)
    _drain(owner)
    world.embed_host.mode = "down"
    nothing = f"zn{uuid.uuid4().hex}"
    r = client.get(f"/v1/mesh/search?q={nothing}", **_bearer(reader["peer_id"]))
    assert r.status_code == 503 and r.json()["error"] == "E_EMBED_UNAVAILABLE"
    body = r.json()
    _assert_envelope(body, world.space)
    assert body["reranked"] is False and body["attempts"]["embed"][0]["verdict"] == "failed"
    assert body["arms"]["lexical"] == {"ran": True, "ranked": 0, "query_terms": 1}
    assert body["arms"]["embed"]["ran"] is False and body["method"] == "lexical"


def test_a_different_model_cannot_answer_a_query(client, owner, world):
    """Admission on the READ path: the job recorded the reference with model A; a query
    provider that is model B at the same width is refused, never compared."""
    reader = _reader(owner)
    _drain(owner)
    world.embed_host.mode = "other"
    nothing = f"zm{uuid.uuid4().hex}"
    r = client.get(f"/v1/mesh/search?q={nothing}", **_bearer(reader["peer_id"]))
    assert r.status_code == 503
    assert r.json()["attempts"]["embed"][0]["verdict"] == "not_admitted"
    world.embed_host.mode = "near"                              # same model, other GPU
    assert client.get(f"/v1/mesh/search?q={nothing}",
                      **_bearer(reader["peer_id"])).status_code == 200


def test_the_gate_applies_at_read_time_not_only_in_the_job(client, owner, world):
    """Embed a forbidden row with the gate OPEN, then close it WITHOUT a job tick: the
    vector is still in the table, and search must not return it."""
    reader = _reader(owner)
    word = f"zq{uuid.uuid4().hex[:10]}"
    f = db.row(owner, reader, {"body": f"{word} forbidden row"})
    world.declare(gate=True)
    _drain(owner)
    open_ = client.get(f"/v1/mesh/search?q={word}&k=5", **_bearer(reader["peer_id"])).json()
    assert f["key"] in [h["key"] for h in open_["hits"]], "positive control: visible open"

    world.declare(gate=False)                                  # no job tick after this
    closed = client.get(f"/v1/mesh/search?q={word}&k=5", **_bearer(reader["peer_id"])).json()
    assert f["key"] not in [h["key"] for h in closed["hits"]]
    assert closed["coverage"]["skipped_forbidden"] >= 1
    assert closed["coverage"]["forbidden_rows_open"] is False
    # The lexical denominator is gated at read time too (divergence 026): the job lexed
    # and embedded the same rows, so under either gate the two counts agree, and closing
    # the gate removes the forbidden rows from both.
    opened, shut = open_["coverage"], closed["coverage"]
    assert opened["lexical_indexed"] == opened["embedded"], ("control", opened)
    assert shut["lexical_indexed"] == shut["embedded"], (
        "coverage.lexical_indexed counts rows the closed gate hides", shut)
    assert shut["lexical_indexed"] < opened["lexical_indexed"], (opened, shut)


def test_a_superseded_version_is_never_returned(client, owner, world):
    reader = _reader(owner)
    word = f"zv{uuid.uuid4().hex[:10]}"
    v1 = db.row(owner, reader, {"body": f"{word} version one"}, embed_policy="allowed")
    _drain(owner)
    hits = client.get(f"/v1/mesh/search?q={word}", **_bearer(reader["peer_id"])).json()["hits"]
    assert v1["key"] in [h["key"] for h in hits], "positive control"
    with owner.cursor() as cur:
        cur.execute("UPDATE shared_memory SET is_head = FALSE WHERE id = %s", (v1["id"],))
    hits = client.get(f"/v1/mesh/search?q={word}", **_bearer(reader["peer_id"])).json()["hits"]
    assert v1["key"] not in [h["key"] for h in hits]


def test_bad_parameters_are_422_and_writes_are_405(client, owner, world):
    reader = _reader(owner)
    auth = _bearer(reader["peer_id"])
    for url in ("/v1/mesh/search", "/v1/mesh/search?q=", "/v1/mesh/search?q=a&k=0",
                "/v1/mesh/search?q=a&k=41", "/v1/mesh/search?q=a&k=x",
                "/v1/mesh/search?q=a&rerank=maybe", "/v1/mesh/search?q=" + "a" * 2001,
                "/v1/mesh/context", "/v1/mesh/context?key=wiki/x"):
        r = client.get(url, **auth)
        assert r.status_code == 422 and r.json()["error"] == "E_VALIDATION", url
    for url in ("/v1/mesh/search?q=a", "/v1/mesh/context?key=msg/1"):
        r = client.post(url, **auth)
        assert r.status_code == 405 and r.headers["Allow"] == "GET"


# ======================================================================= context

def test_context_packs_thread_reply_chain_and_other_thread_relations(client, owner, world):
    reader = _reader(owner)
    t = f"t-{uuid.uuid4().hex[:8]}"
    other_t = f"t-{uuid.uuid4().hex[:8]}"
    topic = f"zc{uuid.uuid4().hex[:8]} lease renewal"
    base = _dt.datetime.now(_dt.timezone.utc)
    m1 = db.row(owner, reader, {"body": f"{topic} question", "thread_id": t},
                embed_policy="allowed", created_at=base)
    # m2 answers m1; m3 answers m2. The reply pointer names the key, as writers do.
    m2 = db.row(owner, reader, {"body": f"{topic} answer", "thread_id": t,
                                "in_reply_to": m1["key"]}, embed_policy="allowed",
                created_at=base + _dt.timedelta(seconds=1))
    m3 = db.row(owner, reader, {"body": f"{topic} follow up", "thread_id": t,
                                "in_reply_to": m2["key"]}, embed_policy="allowed",
                created_at=base + _dt.timedelta(seconds=2))
    elsewhere = db.row(owner, reader, {"body": f"{topic} in another thread",
                                       "thread_id": other_t}, embed_policy="allowed",
                       created_at=base + _dt.timedelta(seconds=3))
    _project_edges(owner)
    _drain(owner)

    r = client.get(f"/v1/mesh/context?key={m2['key']}&k=5&rerank=1",
                   **_bearer(reader["peer_id"]))
    assert r.status_code == 200, r.content[:400]
    body = r.json()
    _assert_envelope(body, world.space)
    assert body["anchor"]["thread_id"] == t and body["anchor"]["edges_projected"] is True
    assert [h["key"] for h in body["thread"]["hits"]] == [m1["key"], m2["key"], m3["key"]]
    assert [a["key"] for a in body["reply_chain"]["ancestors"]] == [m1["key"]]
    assert body["reply_chain"]["ended"] == "root"
    assert [h["key"] for h in body["reply_chain"]["replies"]] == [m3["key"]]
    related = [h["key"] for h in body["related"]["hits"]]
    assert elsewhere["key"] in related, "the other-thread message is related"
    assert not {m1["key"], m2["key"], m3["key"]} & set(related), (
        "related must come from OTHER threads and never be the anchor")
    assert body["reranked"] is True and body["related"]["query_vector"]["source"] == "stored"
    assert body["providers"]["embed"] is None, "a stored vector needs no embed call"
    assert body["method"] == "rrf(lexical,embed)+rerank" and body["degraded"] is None


def test_context_says_why_related_is_absent(client, owner, world):
    reader = _reader(owner)
    forbidden = db.row(owner, reader, {"body": "zf forbidden anchor"})
    _drain(owner)
    body = client.get(f"/v1/mesh/context?key={forbidden['key']}",
                      **_bearer(reader["peer_id"])).json()
    assert body["related"]["absent"] == "anchor_embed_forbidden" and body["related"]["hits"] == []
    assert body["thread"] is None and body["thread_absent"] in ("anchor_not_projected",
                                                                "no_in_thread_edge")
    sent = world.embed_host.embed_inputs()
    assert not any("zf forbidden anchor" in s for s in sent), "the anchor's text was sent"

    missing = client.get("/v1/mesh/context?key=msg/0-doesnotexist",
                         **_bearer(reader["peer_id"]))
    assert missing.status_code == 404 and missing.json()["error"] == "E_NOT_FOUND"
    _assert_envelope(missing.json(), world.space)


# ======================================================================= read only

def _snapshot(owner, space) -> tuple:
    with owner.cursor() as cur:
        cur.execute("SELECT src_id, text_sha256, embedded_at FROM mesh_embeddings "
                    "WHERE space = %s ORDER BY src_id", (space.name,))
        vecs = cur.fetchall()
        cur.execute("SELECT * FROM mesh_embeddings_projection WHERE space = %s", (space.name,))
        state = cur.fetchall()
        cur.execute("SELECT count(*), max(recorded_at) FROM mesh_edges")
        edges = cur.fetchone()
        cur.execute("SELECT src_id, text_sha256, indexed_at, lexemes, tfs FROM mesh_lexical "
                    "WHERE space = %s ORDER BY src_id", (space.name,))
        lexemes = cur.fetchall()
    assert lexemes, "control: the snapshot holds lexical rows to compare"
    return vecs, state, edges, lexemes


def test_no_request_writes_a_projection(client, owner, world):
    reader = _reader(owner)
    a = db.row(owner, reader, {"body": "readonly words here", "thread_id": "t-ro"},
               embed_policy="allowed")
    pending = db.row(owner, reader, {"body": "not yet embedded words"},
                     embed_policy="allowed")
    _project_edges(owner)
    _drain(owner)
    with owner.cursor() as cur:        # make one row pending: context must not embed-and-store
        cur.execute("DELETE FROM mesh_embeddings WHERE src_id = %s AND space = %s",
                    (pending["id"], world.space.name))
    before = _snapshot(owner, world.space)
    auth = _bearer(reader["peer_id"])
    assert client.get("/v1/mesh/search?q=readonly+words", **auth).status_code == 200
    ctx = client.get(f"/v1/mesh/context?key={pending['key']}", **auth)
    assert ctx.status_code == 200
    assert ctx.json()["related"]["query_vector"]["source"] == "embedded", (
        "positive control: the request DID compute a vector for the pending row")
    assert client.get(f"/v1/mesh/context?key={a['key']}", **auth).status_code == 200
    assert _snapshot(owner, world.space) == before, "a request path wrote a projection"


# ================================================================ the ranking (026)

def _fused_by_hand(hit: dict) -> float:
    return sum(1.0 / (fusion.RRF_K + r) for r in hit["ranks"].values() if r is not None)


def test_the_default_fuses_both_arms_then_reranks_and_rerank_0_turns_it_off(client, owner,
                                                                            world):
    """No `rerank` parameter: reciprocal-rank fusion of the lexical and embedding arms, then
    the reranker over the fused pool, and the response says so (divergence 026, as
    amended). The control, one variable apart, is `rerank=0`: no reranker call, and the
    order is the fusion's. Kills: the default reverted to 0, and a method name that claims
    what did not run."""
    reader = _reader(owner)
    word = f"zd{uuid.uuid4().hex[:10]}"
    exact = db.row(owner, reader, {"body": f"{word} lease renewal handover"},
                   embed_policy="allowed")
    db.row(owner, reader, {"body": f"{word} unrelated uxxx tables"}, embed_policy="allowed")
    _drain(owner)
    calls = world.rerank_host.calls("/rerank")

    body = client.get(f"/v1/mesh/search?q={word}+lease+renewal&k=5",
                      **_bearer(reader["peer_id"])).json()
    _assert_envelope(body, world.space)
    assert world.rerank_host.calls("/rerank") == calls + 1, "the default did not rerank"
    assert body["rerank_requested"] is True and body["reranked"] is True
    assert body["method"] == "rrf(lexical,embed)+rerank" and body["rrf_k"] == 60
    assert body["providers"]["rerank"] == "rerank-hqb" and body["degraded"] is None
    assert body["arms"]["lexical"]["ran"] and body["arms"]["embed"]["ran"]
    assert body["hits"][0]["key"] == exact["key"]
    assert all(h["rerank_score"] is not None for h in body["hits"])

    off = client.get(f"/v1/mesh/search?q={word}+lease+renewal&k=5&rerank=0",
                     **_bearer(reader["peer_id"])).json()
    assert world.rerank_host.calls("/rerank") == calls + 1, "control: rerank=0 reranked"
    assert off["rerank_requested"] is False and off["reranked"] is False
    assert off["method"] == "rrf(lexical,embed)" and off["rrf_k"] == 60
    assert off["arms"]["lexical"]["ranked"] >= 2 and off["arms"]["embed"]["ranked"] >= 2
    hits = off["hits"]
    assert hits[0]["key"] == exact["key"]
    assert hits[0]["ranks"] == {"lexical": 1, "embed": 1}
    for h in hits:
        assert h["fused_score"] == pytest.approx(_fused_by_hand(h), abs=2e-6), h
        assert (h["cosine"] is None) == (h["ranks"]["embed"] is None)
        assert (h["lexical_score"] is None) == (h["ranks"]["lexical"] is None)
        assert h["rerank_score"] is None
    assert [h["fused_score"] for h in hits] == sorted((h["fused_score"] for h in hits),
                                                      reverse=True)


@pytest.fixture()
def earlier(owner):
    """A message committed BEFORE `world` takes its floor: what every earlier test in the
    run leaves in the shared namespace. Requested ahead of `world`; the test checks that it
    really committed first."""
    word = f"zb{uuid.uuid4().hex[:10]}"
    return word, db.row(owner, _reader(owner, "earlier"), {"body": f"{word} floor probe"},
                        embed_policy="allowed")


def test_a_message_from_before_the_test_is_in_neither_arm(client, owner, earlier, world):
    """OI-05, the isolation the rerank=0 control above depends on: a message committed
    before the test's space was made is in neither arm's ranking, nor in the space's
    vectors or lexical terms, although the job walks it. Control, one variable apart: the
    same text committed AFTER the space was made is ranked first by both arms. That copy is
    dated an hour EARLIER, so the cut must be the commit order, not created_at. Kills:
    `_drain` without the cut (the earlier copy is ranked too), and a cut on created_at (the
    later copy is dropped)."""
    word, before = earlier
    with owner.cursor() as cur:
        cur.execute("SELECT seq FROM mesh_commit_order WHERE memory_id = %s",
                    (before["id"],))
        (seq,) = cur.fetchone()
    assert seq <= _FLOOR[world.space.name], (
        "precondition: `earlier` committed before `world`")
    reader = _reader(owner)
    after = db.row(owner, reader, {"body": f"{word} floor probe"}, embed_policy="allowed",
                   created_at=_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=1))
    _drain(owner)

    body = client.get(f"/v1/mesh/search?q={word}+floor+probe&k=5&rerank=0",
                      **_bearer(reader["peer_id"])).json()
    assert [h["key"] for h in body["hits"]] == [after["key"]], body["hits"]
    assert body["hits"][0]["ranks"] == {"lexical": 1, "embed": 1}
    assert body["arms"]["lexical"]["ranked"] == 1 and body["arms"]["embed"]["ranked"] == 1
    with owner.cursor() as cur:
        for table in ("mesh_embeddings", "mesh_lexical"):
            cur.execute(f"SELECT src_id FROM {table} WHERE space = %s", (world.space.name,))
            assert {r[0] for r in cur.fetchall()} == {after["id"]}, table


def test_the_search_and_context_functions_themselves_default_to_rerank(owner, world):
    """The CLI and any in-process caller get the same default as the route: search() and
    context() rerank unless told rerank=False (the control)."""
    from apps.core import dbconn

    reader = _reader(owner)
    word = f"zf{uuid.uuid4().hex[:10]}"
    anchor = db.row(owner, reader, {"body": f"{word} words", "thread_id": f"t-{word}-a"},
                    embed_policy="allowed")
    db.row(owner, reader, {"body": f"{word} more words", "thread_id": f"t-{word}-b"},
           embed_policy="allowed")
    _project_edges(owner)
    _drain(owner)
    calls = world.rerank_host.calls("/rerank")
    with dbconn.serving_connection(DSN) as conn:
        body = search.search(conn, world.space, word)
        assert body["reranked"] is True and body["rerank_requested"] is True
        assert world.rerank_host.calls("/rerank") == calls + 1
        ctx = search.context(conn, world.space, anchor["key"])
        assert ctx["reranked"] is True and ctx["rerank_requested"] is True
        assert world.rerank_host.calls("/rerank") == calls + 2
        off = search.search(conn, world.space, word, rerank=False)
        ctx_off = search.context(conn, world.space, anchor["key"], rerank=False)
    assert off["reranked"] is False and ctx_off["reranked"] is False, "control: told not to"
    assert world.rerank_host.calls("/rerank") == calls + 2


def test_a_reranker_outage_under_the_default_degrades_honestly(client, owner, world,
                                                               monkeypatch):
    """Reranking by default must never make the reranker a way to fail a search. With the
    fake reranker DOWN (503), returning a SHORT result, UNREACHABLE, or not declared at
    all, both routes answer 200 in fused order with `reranked: false`, a `method` without
    "+rerank", `rerank_requested: true`, and (where a reranker was declared) the attempt
    recorded, typed. Control, one variable apart: the same requests with the reranker up
    say `reranked: true` and "+rerank"."""
    reader = _reader(owner)
    word = f"zo{uuid.uuid4().hex[:10]}"
    anchor = db.row(owner, reader, {"body": f"{word} outage anchor words",
                                    "thread_id": f"t-{word}-a"}, embed_policy="allowed")
    db.row(owner, reader, {"body": f"{word} outage other words", "thread_id": f"t-{word}-b"},
           embed_policy="allowed")
    _project_edges(owner)
    _drain(owner)
    auth = _bearer(reader["peer_id"])
    urls = (f"/v1/mesh/search?q={word}+outage", f"/v1/mesh/context?key={anchor['key']}")

    def ask():
        out = []
        for url in urls:
            r = client.get(url, **auth)
            assert r.status_code == 200, (url, r.content[:300])
            body = r.json()
            _assert_envelope(body, world.space)
            hits = body["hits"] if "hits" in body else body["related"]["hits"]
            assert hits, (url, "the fused order must still be served")
            assert body["rerank_requested"] is True
            out.append((body, hits))
        return out

    for body, hits in ask():                                   # control: the reranker up
        assert body["reranked"] is True and body["method"] == "rrf(lexical,embed)+rerank"

    for mode, reason in (("down", "HTTP 503"),
                         ("short", "rerank did not return one result per document")):
        world.rerank_host.rerank_mode = mode
        for body, hits in ask():
            assert body["reranked"] is False and body["method"] == "rrf(lexical,embed)", mode
            assert body["providers"]["rerank"] is None and body["degraded"] is None
            assert all(h["rerank_score"] is None for h in hits)
            assert [h["fused_score"] for h in hits] == sorted(
                (h["fused_score"] for h in hits), reverse=True), "not the fused order"
            (attempt,) = body["attempts"]["rerank"]
            assert (attempt["id"], attempt["verdict"], attempt["reason"]) == (
                "rerank-hqb", "failed", reason), attempt
    world.rerank_host.rerank_mode = "good"

    class _Dead:
        base_url = f"http://127.0.0.1:{_dead_port()}"

    monkeypatch.setattr(world, "rerank_host", _Dead)
    world.declare()
    (searched, _), (contexted, _) = ask()
    (attempt,) = searched["attempts"]["rerank"]
    assert attempt["verdict"] == "failed" and attempt["reason"].startswith(
        "unreachable"), attempt
    # A transport failure starts the provider's cooldown (providers.RERANK_COOLDOWN_S): the
    # next request does not wait on it again, and says so, typed.
    (attempt,) = contexted["attempts"]["rerank"]
    assert attempt["verdict"] == "not_tried" and attempt["reason"].startswith(
        "cooling down: unreachable"), attempt
    for body in (searched, contexted):
        assert body["reranked"] is False and body["method"] == "rrf(lexical,embed)"

    world.declare(rerank=False)
    for body, _hits in ask():
        assert body["reranked"] is False and body["method"] == "rrf(lexical,embed)"
        assert body["attempts"]["rerank"] == [], "no reranker declared, none tried"


def _dead_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_an_unreachable_embedder_degrades_to_lexical_and_says_so(client, owner, world):
    """The embedder is down: the lexical arm answers alone, and the response says so in
    `method`, `arms` and `degraded` rather than looking like a fused answer. Controls:
    the same query with the embedder up is fused and not degraded; a query neither arm
    can answer is the typed 503, never an empty 200."""
    reader = _reader(owner)
    word = f"zu{uuid.uuid4().hex[:10]}"
    row = db.row(owner, reader, {"body": f"{word} degraded search words"},
                 embed_policy="allowed")
    _drain(owner)
    auth = _bearer(reader["peer_id"])

    up = client.get(f"/v1/mesh/search?q={word}", **auth).json()
    # Reranked: the default (divergence 026, as amended). The arms are this test's subject.
    assert up["method"] == "rrf(lexical,embed)+rerank" and up["degraded"] is None, "control"

    world.embed_host.mode = "down"
    r = client.get(f"/v1/mesh/search?q={word}", **auth)
    assert r.status_code == 200, r.content[:300]
    body = r.json()
    _assert_envelope(body, world.space)
    assert body["method"] == "lexical+rerank" and body["rrf_k"] is None
    assert body["arms"]["embed"] == {"ran": False, "ranked": 0, "absent": "embed_unavailable",
                                     "error": "E_EMBED_UNAVAILABLE"}
    assert body["arms"]["lexical"]["ran"] is True and body["arms"]["lexical"]["ranked"] >= 1
    assert body["degraded"] == ("lexical only: the embed arm did not run "
                                "(E_EMBED_UNAVAILABLE: embed_unavailable)")
    assert body["providers"]["embed"] is None and body["query_vector"] is None
    assert body["attempts"]["embed"][0]["verdict"] == "failed"
    assert body["hits"][0]["key"] == row["key"]
    assert all(h["ranks"]["embed"] is None and h["cosine"] is None for h in body["hits"])

    empty = client.get(f"/v1/mesh/search?q=zz{uuid.uuid4().hex}", **auth)
    assert empty.status_code == 503 and empty.json()["error"] == "E_EMBED_UNAVAILABLE"
    assert empty.json()["degraded"].startswith("lexical only:")


def test_a_message_written_during_an_embedder_outage_is_found_lexically(client, owner,
                                                                         world):
    """The lexical rows do not wait for the embedder (divergence 026): a message written
    while the embedder is down is lexed by the job's FAILING tick, so the lexical-only
    answer finds it, says it is lexical-only, and coverage shows the lexical arm ahead of
    the vectors. Control: before that tick, the same query has nothing to find (the typed
    503, not an empty 200)."""
    reader = _reader(owner)
    _drain(owner)
    auth = _bearer(reader["peer_id"])
    world.embed_host.mode = "down"
    word = f"zw{uuid.uuid4().hex[:10]}"
    row = db.row(owner, reader, {"body": f"{word} written during the outage"},
                 embed_policy="allowed")

    before = client.get(f"/v1/mesh/search?q={word}", **auth)
    assert before.status_code == 503, "control: not lexed yet, nothing to find"

    r = embedjob.catch_up(owner)
    assert (r.stopped, r.embedded) == ("provider_unavailable", 0) and r.lexed >= 1, r
    search.forget()                                  # coverage is held COVERAGE_TTL_S
    got = client.get(f"/v1/mesh/search?q={word}", **auth)
    assert got.status_code == 200, got.content[:300]
    body = got.json()
    assert body["method"] == "lexical+rerank"
    assert body["degraded"].startswith("lexical only:")
    assert [h["key"] for h in body["hits"]] == [row["key"]]
    assert body["hits"][0]["ranks"] == {"lexical": 1, "embed": None}
    cov = body["coverage"]
    assert cov["lexical_indexed"] == cov["embedded"] + 1 and cov["pending"] >= 1, cov


def test_context_degrades_to_lexical_when_the_anchor_needs_an_embed_call(client, owner,
                                                                          world):
    """An anchor with no stored vector needs the embedder; with it down, related comes
    from the lexical arm alone, stated. Control: with it up the same anchor is fused."""
    reader = _reader(owner)
    topic = f"zk{uuid.uuid4().hex[:8]}"
    anchor = db.row(owner, reader, {"body": f"{topic} anchor words",
                                    "thread_id": f"t-{topic}-a"}, embed_policy="allowed")
    other = db.row(owner, reader, {"body": f"{topic} other thread",
                                   "thread_id": f"t-{topic}-b"}, embed_policy="allowed")
    _project_edges(owner)
    _drain(owner)
    with owner.cursor() as cur:        # the anchor loses its stored vector, not its lexemes
        cur.execute("DELETE FROM mesh_embeddings WHERE src_id = %s AND space = %s",
                    (anchor["id"], world.space.name))
    search.forget()
    auth = _bearer(reader["peer_id"])

    up = client.get(f"/v1/mesh/context?key={anchor['key']}", **auth).json()
    assert up["method"] == "rrf(lexical,embed)+rerank", "control: fused with the embedder up"
    assert up["related"]["query_vector"]["source"] == "embedded"

    world.embed_host.mode = "down"
    body = client.get(f"/v1/mesh/context?key={anchor['key']}", **auth).json()
    assert body["method"] == "lexical+rerank"
    assert body["degraded"].startswith("lexical only:")
    assert body["arms"]["embed"]["absent"] == "embed_unavailable"
    assert other["key"] in [h["key"] for h in body["related"]["hits"]]
    assert body["related"]["absent"] is None and body["related"]["query_vector"] == {
        "source": None}


def test_context_with_no_embedder_and_no_lexical_match_says_why_related_is_empty(
        client, owner, world):
    """The context route's counterpart of search's typed 503 (divergence 026): the embed
    arm cannot run AND the lexical arm matches nothing but the anchor itself (excluded), so
    `related` is empty and MUST say why, typed, rather than read as 'nothing is related'.
    Control, one variable apart: with the embedder up the embed arm ranks the other
    message, so related is non-empty and not absent."""
    reader = _reader(owner)
    lone = f"zx{uuid.uuid4().hex[:12]}"
    anchor = db.row(owner, reader, {"body": lone}, embed_policy="allowed")
    db.row(owner, reader, {"body": "an unrelated message about uxxx tables"},
           embed_policy="allowed")
    _drain(owner)
    with owner.cursor() as cur:        # the anchor needs an embed call to use the embed arm
        cur.execute("DELETE FROM mesh_embeddings WHERE src_id = %s AND space = %s",
                    (anchor["id"], world.space.name))
    search.forget()
    auth = _bearer(reader["peer_id"])

    up = client.get(f"/v1/mesh/context?key={anchor['key']}", **auth).json()
    assert up["method"] == "rrf(lexical,embed)+rerank" and up["related"]["hits"], "control"
    assert up["related"]["absent"] is None

    world.embed_host.mode = "down"
    body = client.get(f"/v1/mesh/context?key={anchor['key']}", **auth).json()
    assert body["arms"]["lexical"]["ran"] is True and body["method"] == "lexical"
    assert body["related"]["hits"] == []
    assert body["related"]["absent"] == "embed_unavailable", body["related"]
    assert body["degraded"].startswith("lexical only:")



# ============================================== the rerank pool's per-arm guarantee (026)
#
# Measured on the node-test server at 388f28e: a paraphrase query's target was the embed
# arm's rank 1 and matched no lexical term, so reciprocal-rank fusion put it at rank 42,
# outside the POOL of 40 the reranker scores, and `k` is capped at POOL, so no request
# could return it. The pool now always holds each arm's first ARM_GUARANTEE live rows.
# These two tests stand in for the arms' RANKINGS only (the rows, the fusion, the pool
# query, the exclusions, the reranker and the route are all real), which is the only way
# to put 45 rows ahead of the target in both arms deterministically.

#: The `pool` object's fields on each route: search adds the per-arm counts it ranked.
POOL_FIELDS = {"size", "limit", "examined", "fused", "guaranteed"}


def _stand_in_arms(monkeypatch, lexical_ids, embed_ids):
    def lexical_arm(_lex, _text):
        ids = [str(i) for i in lexical_ids]
        return search.Arm(fusion.LEXICAL, True,
                          fusion.Ranking(fusion.LEXICAL, ids, [0.01] * len(ids)),
                          detail={"query_terms": 1})

    def embed_arm(_idx, _qvec, *, source):
        ids = [str(i) for i in embed_ids]
        return search.Arm(fusion.EMBED, True,
                          fusion.Ranking(fusion.EMBED, ids,
                                         [0.6 - n / 1000 for n in range(len(ids))]),
                          detail={"query_vector": source})

    monkeypatch.setattr(search, "_lexical_arm", lexical_arm)
    monkeypatch.setattr(search, "_embed_arm", embed_arm)


def test_search_returns_the_embed_arms_first_place_that_fusion_crowded_out(
        client, owner, world, monkeypatch):
    """45 rows both arms rank, and a target the embed arm ranks FIRST and the lexical arm
    not at all: it fuses at rank 46. With `rerank=0` and k = POOL the answer is the fused
    top 39 and then the target (the tail changed, nothing before it), and
    `pool.guaranteed` says the embed arm's guarantee added one row. With the reranker
    (the default) the target, the only row sharing the query's words, is first."""
    reader = _reader(owner)
    word = f"zg{uuid.uuid4().hex[:10]}"
    both = [db.row(owner, reader, {"body": f"{word} filler row {i}"}, embed_policy="allowed")
            for i in range(45)]
    target = db.row(owner, reader, {"body": "certificate expired so every handshake failed"},
                    embed_policy="allowed")
    _drain(owner)
    _stand_in_arms(monkeypatch, [r["id"] for r in both],
                   [target["id"]] + [r["id"] for r in both])
    auth = _bearer(reader["peer_id"])
    q = "certificate+expired+handshake"

    off = client.get(f"/v1/mesh/search?q={q}&k={search.POOL}&rerank=0", **auth).json()
    _assert_envelope(off, world.space)
    assert off["method"] == "rrf(lexical,embed)"
    keys = [h["key"] for h in off["hits"]]
    assert target["key"] in keys, "the embed arm's first place was never returned"
    assert keys == [r["key"] for r in both[:search.POOL - 1]] + [target["key"]]
    assert off["hits"][-1]["ranks"] == {"lexical": None, "embed": 1}
    assert set(off["pool"]) == POOL_FIELDS | {"ranked_vectors", "ranked_lexical"}
    assert off["pool"]["guaranteed"] == {"lexical": 0, "embed": 1}
    assert off["pool"]["size"] == search.POOL and off["pool"]["fused"] == 46

    on = client.get(f"/v1/mesh/search?q={q}&k=1", **auth).json()
    assert on["reranked"] is True and on["method"] == "rrf(lexical,embed)+rerank"
    assert [h["key"] for h in on["hits"]] == [target["key"]], "the reranker never saw it"
    assert on["pool"]["guaranteed"] == {"lexical": 0, "embed": 1}


def test_context_returns_the_embed_arms_best_row_past_the_anchors_own_thread(
        client, owner, world, monkeypatch):
    """The context route's pool excludes the anchor and its thread, which both arms rank
    first for the anchor's own text; past them the embed arm's first LIVE row is the
    target, and 45 other-thread rows ranked by both arms fuse above it. The same guarantee
    must hold here: the route calls the same pool."""
    reader = _reader(owner)
    word = f"zh{uuid.uuid4().hex[:10]}"
    t = f"t-{word}"
    anchor = db.row(owner, reader, {"body": "certificate expired handshake failed",
                                    "thread_id": t}, embed_policy="allowed")
    mates = [db.row(owner, reader, {"body": f"{word} mate {i}", "thread_id": t},
                    embed_policy="allowed") for i in range(5)]
    both = [db.row(owner, reader, {"body": f"{word} filler {i}", "thread_id": f"{t}-o{i}"},
                   embed_policy="allowed") for i in range(45)]
    target = db.row(owner, reader, {"body": "the certificate expired so the handshake failed",
                                    "thread_id": f"{t}-x"}, embed_policy="allowed")
    _project_edges(owner)
    _drain(owner)
    own = [anchor] + mates
    _stand_in_arms(monkeypatch, [r["id"] for r in own + both],
                   [r["id"] for r in own] + [target["id"]] + [r["id"] for r in both])
    auth = _bearer(reader["peer_id"])

    off = client.get(f"/v1/mesh/context?key={anchor['key']}&k={search.POOL}&rerank=0",
                     **auth).json()
    _assert_envelope(off, world.space)
    assert off["anchor"]["thread_id"] == t, "control: the thread is known, so excluded"
    related = off["related"]
    keys = [h["key"] for h in related["hits"]]
    assert target["key"] in keys, "the embed arm's best other-thread row was never returned"
    assert not {r["key"] for r in own} & set(keys)
    assert keys == [r["key"] for r in both[:search.POOL - 1]] + [target["key"]]
    assert set(related["pool"]) == POOL_FIELDS
    assert related["pool"]["guaranteed"] == {"lexical": 0, "embed": 1}

    on = client.get(f"/v1/mesh/context?key={anchor['key']}&k=1", **auth).json()
    assert on["reranked"] is True
    assert [h["key"] for h in on["related"]["hits"]] == [target["key"]]


# ============================================================== the anchor's body (027)
#
# THE LIVE FINDING (2026-09-23): the anchor object had key, version, recorded_at, author,
# thread_id and edges_projected, and NO body, so the one message a caller named was the one
# it could not read here, while every related hit carried its body. The anchor now carries
# `body` and `body_truncated` by the related hits' rule, read from the anchor's own
# shared_memory row, so it is there before the edges job has projected the row.
#
# memory.read (apps/memoryskills/handlers.handle_read) is the authority on what a caller
# may read of one key, so the anchor's body must be hidden in exactly the cases it hides
# the memo. Its rules, in the order it applies them, each covered below:
#   1. the namespace is claimed                                  E_NAMESPACE_UNCLAIMED
#      (tests/test_meshy_node_read.py: no shared database can unclaim the mesh)
#   2. the caller owns it, or holds a grant that is not revoked, not expired, and at or
#      above read                                                E_PERMISSION_DENIED
#   3. a row exists for (namespace, key) - a retention delete removes it  E_UNKNOWN_KEY
#   4. with no version asked, the row is the HEAD (is_head); context names no version, so
#      its anchor is always the head, as memory.read's no-version answer is
#   5. RLS: both run under app.server_internal, whose policy has no row predicate
# embed_policy is NOT one of them: it gates what may reach a MODEL (MUST-8), and
# memory.read returns a forbidden row's body to a caller with read. So does the anchor.
#
# IN FRONT OF handle_read (the review of fin/read): memory.read is reached through the
# invoke pipeline, which also asks for a DPoP proof on the POST, a usable signing key, the
# skill resolved, and the tenancy or skill-grant gate. `_memory_read` below runs the
# handler alone, so every caller it is compared for is one the pipeline admits (`_reader`,
# `_member`); the pipeline's own gates have their tests after these, and
# tests/test_meshy_node_read.py compares them with the real `read` end to end.

def _memory_read(caller: dict, key: str, version: int | None = None) -> dict:
    """What memory.read answers THIS caller for THIS key: the real handler, through the
    real dispatch, on a meshychat_app connection - the call apps/calls/route.py makes once the
    envelope has verified. {"op", "memo"} or the handler's own {"error", "message"}."""
    from apps.memoryskills import db as memory_db
    from apps.memoryskills import handlers
    from apps.memoryskills.errors import HandlerError
    from apps.memoryskills.validation import MESH_CHAT_NAMESPACE

    args: dict = {"namespace": MESH_CHAT_NAMESPACE, "key": key}
    if version is not None:
        args["version"] = version
    # `kind` because a payload route.py hands the handler has passed verify_payload's check
    # 2, and the binding compares it since divergence 038.
    payload = {"kind": "skill.call", "caller_peer_id": str(caller["peer_id"]),
               "skill_name": "memory.read", "args": args}
    conn = memory_db.connect(DSN)
    try:
        return handlers.dispatch("memory.read", {"payload": payload}, caller["peer_id"], conn)
    except HandlerError as exc:
        return {"error": exc.code, "message": exc.message}
    finally:
        conn.close()


def _cut(text) -> tuple[str, bool]:
    """The related hits' rule, written out: the stripped string body, at most
    embedjob.MAX_CHARS characters, and whether it was cut. A non-string body is ''."""
    stripped = (text if isinstance(text, str) else "").strip()
    return stripped[:embedjob.MAX_CHARS], len(stripped) > embedjob.MAX_CHARS


def _agree(client, caller: dict, key: str, *, absent: tuple[str, ...] = ()) -> dict:
    """The anchor shows a body exactly when memory.read shows the memo, and the SAME body;
    when memory.read refuses, context refuses with no anchor. No text in `absent` appears
    anywhere in the context response. Returns memory.read's answer."""
    read = _memory_read(caller, key)
    r = client.get(f"/v1/mesh/context?key={key}", **_bearer(caller["peer_id"]))
    if "memo" in read:
        assert r.status_code == 200, (key, r.content[:300])
        anchor = r.json()["anchor"]
        assert anchor.get("body_withheld") is None, anchor
        assert (anchor["body"], anchor["body_truncated"]) == _cut(read["memo"]["value"]
                                                                 .get("body")), anchor
        assert anchor["version"] == read["memo"]["version"] and read["memo"]["is_head"]
    else:
        assert r.status_code in (403, 404), (key, read, r.content[:300])
        assert "anchor" not in r.json() and b'"body"' not in r.content, r.content[:300]
    for text in absent:
        assert text.encode() not in r.content, (text, r.content[:300])
    return read


def test_context_carries_the_anchor_body_before_the_edges_job_has_run(client, owner,
                                                                     world):
    """Right after a write, before any edges tick: the anchor's body is there, with
    `thread_absent: anchor_not_projected` beside it. The body comes from the ROW, so it
    does not wait for the projection. Control: after the tick the thread appears and the
    body is the same."""
    reader = _reader(owner)
    thread = f"t-{uuid.uuid4().hex[:8]}"
    text = f"zb{uuid.uuid4().hex[:8]} synthetic: nobody has projected this message yet"
    row = db.row(owner, reader, {"body": text, "thread_id": thread})
    auth = _bearer(reader["peer_id"])

    r = client.get(f"/v1/mesh/context?key={row['key']}", **auth)
    assert r.status_code == 200, r.content[:300]
    body = r.json()
    assert body["anchor"]["edges_projected"] is False
    assert body["thread_absent"] == "anchor_not_projected" and body["thread"] is None
    assert body["anchor"]["body"] == text and body["anchor"]["body_truncated"] is False

    _project_edges(owner)
    after = client.get(f"/v1/mesh/context?key={row['key']}", **auth).json()
    assert after["anchor"]["edges_projected"] is True and after["thread_absent"] is None
    assert after["thread"]["thread_id"] == thread, "control: the projection ran"
    assert after["anchor"]["body"] == text and after["anchor"]["body_truncated"] is False


def test_the_anchor_body_is_cut_by_the_related_hits_rule(client, owner, world):
    """Same rule, same field names: an anchor and a related hit holding the same over-long
    body (padded with whitespace) carry the same `body` and `body_truncated`, and both are
    the stripped text cut at embedjob.MAX_CHARS. A body that is not a string is '' and not
    truncated, as a related hit's would be."""
    reader = _reader(owner)
    word = f"zt{uuid.uuid4().hex[:8]}"
    text = "  \n" + " ".join([word, "synthetic long body words"] * 120) + "  \n"
    assert len(text.strip()) > embedjob.MAX_CHARS, "control: the text is over the limit"
    anchor = db.row(owner, reader, {"body": text, "thread_id": f"t-{word}-a"},
                    embed_policy="allowed")
    twin = db.row(owner, reader, {"body": text, "thread_id": f"t-{word}-b"},
                  embed_policy="allowed")
    _project_edges(owner)
    _drain(owner)
    body = client.get(f"/v1/mesh/context?key={anchor['key']}&k=10",
                      **_bearer(reader["peer_id"])).json()
    assert (body["anchor"]["body"], body["anchor"]["body_truncated"]) == _cut(text)
    assert len(body["anchor"]["body"]) == embedjob.MAX_CHARS
    (hit,) = [h for h in body["related"]["hits"] if h["key"] == twin["key"]]
    assert (hit["body"], hit["body_truncated"]) == (body["anchor"]["body"], True)

    textless = db.row(owner, reader, {"body": {"not": "a string"}}, embed_policy="allowed")
    got = client.get(f"/v1/mesh/context?key={textless['key']}",
                     **_bearer(reader["peer_id"])).json()
    assert (got["anchor"]["body"], got["anchor"]["body_truncated"]) == ("", False)
    assert got["related"]["absent"] == "anchor_has_no_text"


def test_a_caller_without_read_gets_the_same_refusal_and_no_body_anywhere(client, owner,
                                                                         world):
    """No grant: the same 403 as before this change, code AND message, which are
    memory.read's own refusal for the same key and caller; the body appears nowhere in
    the response. Control, one variable apart: a peer with read gets the body."""
    reader = _reader(owner)
    stranger = db.peer(owner, "nobody")
    secret = f"zs{uuid.uuid4().hex[:8]} synthetic: for peers with read only"
    row = db.row(owner, reader, {"body": secret, "thread_id": "t-secret"})

    denied = client.get(f"/v1/mesh/context?key={row['key']}", **_bearer(stranger["peer_id"]))
    assert denied.status_code == 403
    assert denied.json() == {
        "error": "E_PERMISSION_DENIED",
        "message": f"peer {stranger['peer_id']} has no active grant on namespace "
                   "'shared/mesh-chat'"}
    assert denied.json() == _memory_read(stranger, row["key"])
    assert secret.encode() not in denied.content and b'"body"' not in denied.content

    ok = client.get(f"/v1/mesh/context?key={row['key']}", **_bearer(reader["peer_id"]))
    assert ok.status_code == 200 and ok.json()["anchor"]["body"] == secret, "control"


def test_the_anchor_hides_its_body_exactly_when_memory_read_hides_the_memo(client, owner,
                                                                          world):
    """Rules 2-4 of the list above, each beside the control that differs in one variable,
    and embed_policy shown to be none of them."""
    reader = _reader(owner)
    tag = uuid.uuid4().hex[:8]

    # Rule 4, head-ness: version 1 superseded by version 2. memory.read with no version
    # answers v2, and so does the anchor; v1's text is nowhere in context. (memory.read
    # WITH a version still answers v1: context takes no version, so it never can.)
    old, new = f"zh{tag} synthetic: the superseded text", f"zh{tag} synthetic: the head"
    key = f"msg/{int(time.time() * 1000)}-consistency-{tag}"
    v1 = db.row(owner, reader, {"body": old, "thread_id": f"t-{tag}"}, key=key)
    assert _agree(client, reader, key)["memo"]["value"]["body"] == old, "control: v1 is head"
    with owner.cursor() as cur:
        cur.execute("UPDATE shared_memory SET is_head = FALSE WHERE id = %s", (v1["id"],))
    db.row(owner, reader, {"body": new, "thread_id": f"t-{tag}"}, key=key, version=2,
           parent_id=v1["id"])
    assert _agree(client, reader, key, absent=(old,))["memo"]["value"]["body"] == new
    assert _memory_read(reader, key, version=1)["memo"]["value"]["body"] == old

    # Rule 3, a retention delete: the row is gone, so memory.read is E_UNKNOWN_KEY and the
    # anchor is the 404, with the text nowhere. Control: before the delete, both show it.
    gone = f"zr{tag} synthetic: retention will delete this"
    doomed = db.row(owner, reader, {"body": gone})
    assert "memo" in _agree(client, reader, doomed["key"]), "control: before the delete"
    with owner.cursor() as cur:
        cur.execute("DELETE FROM shared_memory WHERE id = %s", (doomed["id"],))
    assert _agree(client, reader, doomed["key"], absent=(gone,))["error"] == "E_UNKNOWN_KEY"
    missing = client.get(f"/v1/mesh/context?key={doomed['key']}",
                         **_bearer(reader["peer_id"]))
    assert missing.status_code == 404 and missing.json()["error"] == "E_NOT_FOUND"

    # Rule 3, an ephemeral rewrite (handle_write DELETEs the prior rows and inserts a new
    # version 1 at the same key): both answer the new text and the old is nowhere.
    before, after = f"ze{tag} synthetic: before the rewrite", f"ze{tag} synthetic: after"
    first = db.row(owner, reader, {"body": before})
    with owner.cursor() as cur:
        cur.execute("DELETE FROM shared_memory WHERE id = %s", (first["id"],))
    db.row(owner, reader, {"body": after}, key=first["key"])
    assert _agree(client, reader, first["key"], absent=(before,))["memo"]["value"][
        "body"] == after

    # Rule 2, the grant: revoked and expired grants are no grant, for both. Controls: an
    # active write grant (above read) and the namespace owner (implicit admin) read both.
    text = f"zg{tag} synthetic: grant-gated text"
    gated = db.row(owner, reader, {"body": text})
    writer = _member(owner, "writer")
    db.grant(owner, writer, "write")
    assert "memo" in _agree(client, writer, gated["key"]), "control: write is above read"
    # The namespace owner was made by whichever test ran first, outside the memory
    # authority's group: give it what the invoke path asks of such a caller, an active key
    # and a memory.read skill grant (apps/calls/authorize.py gate 2).
    holder = db.namespace_owner(owner)
    holder_peer = {"peer_id": holder[0], "owner_id": holder[1]}
    with owner.cursor() as cur:
        cur.execute("SELECT 1 FROM peer_keys WHERE peer_id = %s AND active "
                    "AND revoked_at IS NULL", (holder[0],))
        if cur.fetchone() is None:
            db.add_key(owner, holder[0])
        cur.execute("SELECT 1 FROM skill_grants WHERE grantee_owner_id = %s "
                    "AND granter_peer_id = %s AND revoked_at IS NULL",
                    (holder[1], _memory_authority()["peer_id"]))
        if cur.fetchone() is None:
            db.skill_grant(owner, _memory_authority(), holder_peer)
    assert "memo" in _agree(client, holder_peer, gated["key"]), "control: owner"
    revoked = _member(owner, "revoked")
    db.grant(owner, revoked, "read")
    assert "memo" in _agree(client, revoked, gated["key"]), "control: before the revoke"
    expired = _member(owner, "expired")
    with owner.cursor() as cur:
        cur.execute("UPDATE shared_memory_grant SET revoked = TRUE, revoked_at = now() "
                    "WHERE grantee_peer_id = %s", (revoked["peer_id"],))
        cur.execute("INSERT INTO shared_memory_grant (namespace, grantee_peer_id, "
                    " grantee_owner_id, permission, granted_by_peer_id, granted_by_owner_id,"
                    " expires_at) VALUES ('shared/mesh-chat', %s, %s, 'read', %s, %s, "
                    " now() - interval '1 minute')",
                    (expired["peer_id"], expired["owner_id"], holder[0], holder[1]))
    for who in (revoked, expired):
        assert _agree(client, who, gated["key"], absent=(text,))["error"] == \
            "E_PERMISSION_DENIED", who["label"]

    # NOT a rule: embed_policy. memory.read returns a 'forbidden' row's body to a caller
    # with read, with the gate closed; so does the anchor (related stays empty: the gate
    # keeps the text from a MODEL, which is its job).
    forbidden = db.row(owner, reader, {"body": f"zf{tag} synthetic: forbidden to embed"})
    assert _agree(client, reader, forbidden["key"])["memo"]["value"]["body"].startswith("zf")
    shut = client.get(f"/v1/mesh/context?key={forbidden['key']}",
                      **_bearer(reader["peer_id"])).json()
    assert shut["related"]["absent"] == "anchor_embed_forbidden"


def test_a_peer_that_is_no_longer_active_reads_no_anchor(client, owner, world):
    """In front of memory.read the invoke route authenticates the caller with
    apps/peers/auth.authenticate_peer, which refuses a peer whose status is not 'active';
    the context route authenticates with the same function. So a suspended reader gets no
    anchor and no body. Control: the same bearer before the suspension."""
    reader = _reader(owner, "suspended")
    text = f"zp{uuid.uuid4().hex[:8]} synthetic: for active peers"
    row = db.row(owner, reader, {"body": text})
    auth = _bearer(reader["peer_id"])
    ok = client.get(f"/v1/mesh/context?key={row['key']}", **auth)
    assert ok.status_code == 200 and ok.json()["anchor"]["body"] == text, "control"
    with owner.cursor() as cur:
        cur.execute("UPDATE peers SET status = 'suspended' WHERE id = %s", (reader["peer_id"],))
    refused = client.get(f"/v1/mesh/context?key={row['key']}", **auth)
    assert refused.status_code == 403 and text.encode() not in refused.content


# ============================================== the anchor's body: memory.read's invoke path
#
# THE FINDING (review of fin/read, adversarial, reproduced on a fresh install): the anchor
# showed a body that memory.read REFUSED the same caller, because memory.read is reached
# through the invoke pipeline and the context route applied only handle_read's namespace
# predicate. The body now waits on every gate of that path that a GET can decide
# (search._anchor_body_withheld): a DPoP proof when DPoP is enforced, the memory.read skill
# resolving on the memory authority, a usable signing key, the tenancy or skill-grant
# authorization, then the namespace grant again in the transaction that reads the row.
# A withheld body is `body: null`, `body_truncated: null` and `body_withheld: <gate>`; the
# rest of the answer (thread, reply chain, related) is 019's and unchanged.

def _withheld(client, caller: dict, key: str, text: str, **headers) -> str | None:
    """The anchor's `body_withheld` for this caller, asserting the text is nowhere in the
    response when it is withheld and is the body when it is not."""
    r = client.get(f"/v1/mesh/context?key={key}", **{**_bearer(caller["peer_id"]),
                                                      **headers})
    assert r.status_code == 200, r.content[:300]
    anchor = r.json()["anchor"]
    withheld = anchor.get("body_withheld")        # .get: absent at 09b08e0, so RED is
    if withheld is None:                          # "the body was shown", not a KeyError
        assert anchor["body"] == text and anchor["body_truncated"] is False, anchor
    else:
        assert anchor["body"] is None and anchor["body_truncated"] is None, anchor
        assert text.encode() not in r.content, r.content[:300]
    return withheld


def test_a_caller_outside_the_memory_authoritys_group_needs_a_skill_grant(client, owner,
                                                                         world):
    """A peer of ANOTHER owner and trust group, with an active key and a read grant on
    shared/mesh-chat: memory.read's invoke refuses it (403 E_FORBIDDEN, no skill grant),
    so the anchor withholds its body (`not_authorized`). Controls, one variable apart: a
    memory.read skill grant to its owner shows it; one pinned to a version the authority
    does not serve does not.
    Kills: the authorize() step removed from pipeline.refusal_without_envelope."""
    outsider = db.peer(owner, "outsider", key=True)
    db.grant(owner, outsider, "read")
    text = f"zx{uuid.uuid4().hex[:8]} synthetic: for the memory authority's tenancy"
    row = db.row(owner, outsider, {"body": text})
    assert _withheld(client, outsider, row["key"], text) == "not_authorized"
    db.skill_grant(owner, _memory_authority(), outsider, version_pin="9.9.9")
    assert _withheld(client, outsider, row["key"], text) == "not_authorized", "pinned"
    with owner.cursor() as cur:
        cur.execute("UPDATE skill_grants SET revoked_at = now() WHERE grantee_owner_id = %s",
                    (outsider["owner_id"],))
    db.skill_grant(owner, _memory_authority(), outsider)
    assert _withheld(client, outsider, row["key"], text) is None, "control: a skill grant"


def test_a_caller_with_no_usable_signing_key_gets_no_anchor_body(client, owner, world):
    """memory.read's envelope must be signed under a key of the caller's that is active,
    or revoked with its grace window still open (apps/calls/envelope.py checks 5 and 6).
    With none, the body is withheld (`no_usable_signing_key`): no key at all, and a key
    revoked a minute ago. Controls: a key revoked with the window still open shows it,
    and so does an active key.
    Kills: the key check removed, and the grace window read the wrong way round."""
    keyless = db.peer(owner, "keyless", group=_memory_authority()["group"])
    db.grant(owner, keyless, "read")
    text = f"zk{uuid.uuid4().hex[:8]} synthetic: for peers that can sign"
    row = db.row(owner, keyless, {"body": text})
    assert _withheld(client, keyless, row["key"], text) == "no_usable_signing_key"
    past = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=1)
    future = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(minutes=10)
    db.add_key(owner, keyless["peer_id"], active=False, revoked_at=past)
    assert _withheld(client, keyless, row["key"], text) == "no_usable_signing_key"
    db.add_key(owner, keyless["peer_id"], active=False, revoked_at=future)
    assert _withheld(client, keyless, row["key"], text) is None, "control: grace window"
    with owner.cursor() as cur:
        cur.execute("DELETE FROM peer_keys WHERE peer_id = %s", (keyless["peer_id"],))
    db.add_key(owner, keyless["peer_id"])
    assert _withheld(client, keyless, row["key"], text) is None, "control: an active key"


def test_a_memory_read_skill_that_does_not_resolve_withholds_the_anchor_body(client, owner,
                                                                            world):
    """The memory authority's memory.read soft-deleted: invoke cannot resolve it (404), so
    the anchor withholds its body (`skill_not_found`). A memory authority of this test's
    own, so no other test sees the deleted skill. Control: before the delete it shows.
    Kills: the resolve step removed from pipeline.refusal_without_envelope."""
    own = db.memory_authority(owner)
    world.memory_peer_id = own["peer_id"]
    world.declare()
    reader = db.peer(owner, "own-reader", group=own["group"], key=True)
    db.grant(owner, reader, "read")
    text = f"zq{uuid.uuid4().hex[:8]} synthetic: for a skill that resolves"
    row = db.row(owner, reader, {"body": text})
    assert _withheld(client, reader, row["key"], text) is None, "control"
    with owner.cursor() as cur:
        cur.execute("UPDATE skills SET deleted_at = now() WHERE peer_id = %s "
                    "AND name = 'memory.read'", (own["peer_id"],))
    assert _withheld(client, reader, row["key"], text) == "skill_not_found"


def test_a_grant_revoked_while_the_request_prepared_shows_no_anchor_body(client, owner,
                                                                        world, monkeypatch):
    """The route checks the namespace grant in its own transaction, then `_prepare` can
    take seconds (026 measured 6 to 8 s on a reload) before the anchor is read. A grant
    revoked in that window must not see the body: the grant is checked again in the
    transaction that reads the row, as handle_read checks and reads in one. The revoke is
    placed in the window by wrapping `_prepare`. Control: the same request with no revoke.
    Kills: the namespace check dropped from _anchor_body_withheld."""
    reader = _reader(owner, "revoked-midway")
    text = f"zw{uuid.uuid4().hex[:8]} synthetic: read before the revoke only"
    row = db.row(owner, reader, {"body": text})
    assert _withheld(client, reader, row["key"], text) is None, "control"
    real_prepare = search._prepare

    def revoke_then_prepare(conn, space):
        with owner.cursor() as cur:
            cur.execute("UPDATE shared_memory_grant SET revoked = TRUE, revoked_at = now() "
                        "WHERE grantee_peer_id = %s", (reader["peer_id"],))
        return real_prepare(conn, space)

    monkeypatch.setattr(search, "_prepare", revoke_then_prepare)
    assert _withheld(client, reader, row["key"], text) == "namespace_permission"


def test_with_dpop_enforced_the_anchor_body_needs_a_proof_for_this_get(client, owner, world):
    """memory.read is a DPoP-bound POST when DPoP is enforced (divergence 015), so a
    bearer token alone must not read the body here either. With enforcement on and a
    token bound (`cnf`) to the reader's key: no proof, a proof for another path, and a
    proof already spent all withhold it (`no_proof_of_possession`); a fresh proof for
    exactly this GET shows it. The route itself stays a bearer read: every case is 200.
    Kills: the possession check dropped from the view, and a proof accepted without
    spending its jti."""
    from apps.authapi.didkey import to_did_key
    from tools.meshy_node import wire

    reader = _reader(owner, "dpop")
    text = f"zd{uuid.uuid4().hex[:8]} synthetic: for the key holder"
    row = db.row(owner, reader, {"body": text})
    auth = _bearer(reader["peer_id"], cnf=to_did_key(wire.public_key_of(reader["seed"])))
    token = auth["HTTP_AUTHORIZATION"][len("Bearer "):]

    def proof(target="/v1/mesh/context"):
        return {"HTTP_DPOP": wire.dpop_proof(reader["seed"], "GET", target, token)}

    with override_settings(MESHY_DPOP_ENFORCE=True):
        caller = {"peer_id": reader["peer_id"]}
        assert _withheld(client, caller, row["key"], text,
                         **auth) == "no_proof_of_possession"
        assert _withheld(client, caller, row["key"], text, **auth,
                         **proof("/v1/mesh/search")) == "no_proof_of_possession"
        fresh = proof()
        assert _withheld(client, caller, row["key"], text, **auth, **fresh) is None, \
            "control: a proof for this GET"
        assert _withheld(client, caller, row["key"], text, **auth,
                         **fresh) == "no_proof_of_possession", "a spent proof"
    assert _withheld(client, caller, row["key"], text, **auth) is None, \
        "control: DPoP not enforced, as memory.read's POST then needs none"

    # The proven key must itself be one memory.read verifies under. Rotate: the old key
    # revoked (grace passed), a new one active. A token still bound to the OLD key, with a
    # valid proof by it, is refused by memory.read (no envelope it can sign verifies), so
    # the body is withheld; the token bound to the new key, with its proof, shows it.
    past = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=1)
    with owner.cursor() as cur:
        cur.execute("UPDATE peer_keys SET active = FALSE, revoked_at = %s "
                    "WHERE peer_id = %s", (past, reader["peer_id"]))
    new = db.add_key(owner, reader["peer_id"])
    with override_settings(MESHY_DPOP_ENFORCE=True):
        assert _withheld(client, caller, row["key"], text, **auth,
                         **proof()) == "no_usable_signing_key", "a proof by the old key"
        auth_new = _bearer(reader["peer_id"], cnf=to_did_key(wire.public_key_of(new["seed"])))
        token = auth_new["HTTP_AUTHORIZATION"][len("Bearer "):]
        fresh_new = {"HTTP_DPOP": wire.dpop_proof(new["seed"], "GET", "/v1/mesh/context",
                                                  token)}
        assert _withheld(client, caller, row["key"], text, **auth_new, **fresh_new) is None, \
            "control: the new key's proof"


def test_in_process_context_has_no_caller_and_shows_no_anchor_body(owner, world):
    """`search.context` (no reader) has nobody to ask memory.read's gates about: the body
    is withheld (`no_caller`). Control: `context_for` with the same reader the route
    would build shows it.
    Kills: context() passing a reader it does not have."""
    from apps.core import dbconn

    reader = _reader(owner, "in-process")
    text = f"zn{uuid.uuid4().hex[:8]} synthetic: in-process"
    row = db.row(owner, reader, {"body": text})
    with dbconn.serving_connection(DSN) as conn:
        anon = search.context(conn, world.space, row["key"], rerank=False)
        assert anon["anchor"]["body"] is None, anon["anchor"]
        assert anon["anchor"]["body_withheld"] == "no_caller"
        known = search.context_for(search.Reader(peer_id=reader["peer_id"], possession=True),
                                   conn, world.space, row["key"], k=10, rerank=False)
    assert known["anchor"]["body"] == text and known["anchor"]["body_withheld"] is None


def test_an_anchor_body_of_exactly_max_chars_is_not_truncated(client, owner, world):
    """The truncation boundary: a body of exactly embedjob.MAX_CHARS characters is whole
    (`body_truncated: false`), one more is cut. At 09b08e0 no test had a body AT the
    boundary and `>` mutated to `>=` survived.
    Kills: body_fields' `len(text) > MAX_CHARS` changed to `>=`."""
    reader = _reader(owner, "boundary")
    tag = uuid.uuid4().hex[:8]
    for extra, cut in ((0, False), (1, True)):
        text = (f"zm{tag} " + "x" * embedjob.MAX_CHARS)[:embedjob.MAX_CHARS + extra]
        assert len(text) == embedjob.MAX_CHARS + extra
        row = db.row(owner, reader, {"body": text})
        anchor = client.get(f"/v1/mesh/context?key={row['key']}",
                            **_bearer(reader["peer_id"])).json()["anchor"]
        assert anchor["body_truncated"] is cut, (extra, anchor["body_truncated"])
        assert anchor["body"] == text[:embedjob.MAX_CHARS]


def test_a_key_with_no_head_row_is_no_anchor_as_memory_read_is_unknown_key(client, owner,
                                                                          world):
    """handle_read with no version reads the HEAD; a key whose rows are all superseded (no
    `is_head` row: the head deleted, an older version left) is E_UNKNOWN_KEY there and the
    404 here, with the older text nowhere. Control: while the head exists, both show it.
    Kills: _anchor selecting the highest version instead of the head (equivalent on every
    table memory.write produces; this constructs the one where it is not)."""
    reader = _reader(owner, "headless")
    tag = uuid.uuid4().hex[:8]
    old, new = f"zo{tag} synthetic: superseded", f"zo{tag} synthetic: the head"
    key = f"msg/{int(time.time() * 1000)}-headless-{tag}"
    v1 = db.row(owner, reader, {"body": old}, key=key, is_head=False)
    head = db.row(owner, reader, {"body": new}, key=key, version=2, parent_id=v1["id"])
    assert _agree(client, reader, key)["memo"]["value"]["body"] == new, "control"
    with owner.cursor() as cur:
        cur.execute("DELETE FROM shared_memory WHERE id = %s", (head["id"],))
    assert _agree(client, reader, key, absent=(old,))["error"] == "E_UNKNOWN_KEY"



def test_a_hung_reranker_costs_one_request_its_timeout_not_every_request(client, owner,
                                                                        world, monkeypatch):
    """Reranking is on by default, so a reranker that accepts connections and never answers
    added RERANK_TIMEOUT_S (15 s) to EVERY search and context request (the review of
    fin/read measured 15.2 s a request against 0.3 s with rerank=0). Now a transport
    failure starts a cooldown: the next requests skip that provider at once, with a typed
    `not_tried` attempt naming the failure, and it is asked again when the cooldown ends.
    The timeout is shortened to 3 s and the cooldown to 6 s so the test can show all three
    phases. Control, one variable apart: the same requests with the reranker up rerank.
    Kills: the cooldown not recorded (every request waits the timeout again)."""
    import socket

    from apps.meshsem import providers

    monkeypatch.setattr(providers, "RERANK_TIMEOUT_S", 3.0)
    # raising=False: at 09b08e0 the constant did not exist, and the test must then fail on
    # what the requests do, not on the setup.
    monkeypatch.setattr(providers, "RERANK_COOLDOWN_S", 6.0, raising=False)
    reader = _reader(owner, "hung")
    word = f"zh{uuid.uuid4().hex[:10]}"
    anchor = db.row(owner, reader, {"body": f"{word} hung anchor words",
                                    "thread_id": f"t-{word}-a"}, embed_policy="allowed")
    db.row(owner, reader, {"body": f"{word} hung other words", "thread_id": f"t-{word}-b"},
           embed_policy="allowed")
    _project_edges(owner)
    _drain(owner)
    auth = _bearer(reader["peer_id"])

    def timed(url):
        started = time.monotonic()
        r = client.get(url, **auth)
        assert r.status_code == 200, (url, r.content[:300])
        return r.json(), time.monotonic() - started

    up, _ = timed(f"/v1/mesh/search?q={word}+hung")
    assert up["reranked"] is True, "control: the reranker up reranks"

    hung = socket.socket()
    hung.bind(("127.0.0.1", 0))
    hung.listen(16)                     # accepts into the backlog, never answers
    try:
        class _Hung:
            base_url = f"http://127.0.0.1:{hung.getsockname()[1]}"

        monkeypatch.setattr(world, "rerank_host", _Hung)
        world.declare()
        first, first_s = timed(f"/v1/mesh/search?q={word}+hung")
        cooldown_ends = time.monotonic() + 6.2
        (attempt,) = first["attempts"]["rerank"]
        assert attempt["verdict"] == "failed" and attempt["reason"] == \
            "transport: TimeoutError", attempt
        assert first_s >= 2.8 and first["reranked"] is False, first_s

        for url in (f"/v1/mesh/search?q={word}+hung",
                    f"/v1/mesh/context?key={anchor['key']}"):
            body, secs = timed(url)
            (attempt,) = body["attempts"]["rerank"]
            assert attempt["verdict"] == "not_tried", (url, attempt, secs)
            assert attempt["reason"].startswith("cooling down: transport: TimeoutError")
            # Relative, with a 3 s timeout (#371): a skipped provider saves the whole 3 s
            # timeout that the first request paid, leaving >1.5 s for non-rerank work under
            # gate load (measured at 0.60 s on HQB when a 1 s timeout left only 0.5 s).
            assert secs < first_s - 1.5, (url, secs, first_s, "waited on the hung reranker")
            assert body["reranked"] is False and "+rerank" not in body["method"]

        time.sleep(max(0.0, cooldown_ends - time.monotonic()))  # cooldown ends: asked again
        again, again_s = timed(f"/v1/mesh/search?q={word}+hung")
        (attempt,) = again["attempts"]["rerank"]
        assert attempt["verdict"] == "failed" and again_s >= 2.8, (attempt, again_s)
    finally:
        hung.close()
