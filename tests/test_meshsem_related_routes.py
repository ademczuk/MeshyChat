"""Persisted related_to edges on the read side: /v1/mesh/context and /v1/mesh/neighbours.
Divergence 032.

Through the REAL urlconf with a real EdDSA bearer, a real database, and the fake model
hosts (tests/fixtures/meshsem_fake.py). The edges are written by the job itself
(apps/meshsem/related.py), never planted, except where a test needs a row the job could
never write (an edge into another namespace) to prove the read refuses it anyway.

What the reads are held to:
  * context serves the anchor's persisted edges in `related_edges`, BESIDE `related`,
    with NO model call for them (the fake hosts count), `tier: "inferred"`, the scores
    and the provenance on every hit and truncation reported; `related` stays context's
    own on-demand ranking, so its `k` and its recall are what they were (the review
    measured the edge list keeping 34 of 63 grade-2 relations where the reranked top 10 kept
    58);
  * a reader never sees an edge to a row it could not read otherwise: superseded, gated
    (the embed_policy gate closed), in another namespace, or deleted; no grant is a 403;
  * every hit of context's `related` says what its `rerank_score` is (`score_kind`:
    the ONE-WAY logit with the anchor as the query, or none, and why) and carries the
    stored edge between the anchor and it (`edge`: pair, forward, reverse, rank,
    direction, threshold_version) or null, read in ONE query for the whole list under
    the edge reads' own gates, so a hit is never compared with an edge in the wrong
    units (txxxx's live finding: related's score is forward, the edges decide on the
    pair's mean);
  * a request writes nothing (the three tables snapshotted around it);
  * neighbours is declared-only by default, adds the inferred tier only on
    `include_inferred=1`, refuses a bad flag or a limit over 100 with a 422 (never a
    clamp), carries each neighbour's thread so a client can walk across threads one hop
    per call, and is counted in mesh_route_usage.

Run:  pytest tests/test_meshsem_related_routes.py -q
"""
from __future__ import annotations

import datetime as _dt
import importlib
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

ISSUER = "meshy-meshsem-related-issuer"
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
from django.test import Client  # noqa: E402

from apps.core import dbconn  # noqa: E402
from tests.fixtures import meshsem_db as db  # noqa: E402
from tests.fixtures.meshsem_related import (  # noqa: E402
    Scene, World, edges, pair, prepare, relate, start_here)
from tests.fixtures import signed_rows as sr  # noqa: E402


def _bearer(peer_id) -> dict:
    now = int(_dt.datetime.now(_dt.timezone.utc).timestamp())
    tok = pyjwt.encode({"sub": str(peer_id), "iss": ISSUER, "iat": now, "exp": now + 300,
                        "aud": "peer", "scopes": []}, PRIV, algorithm="EdDSA")
    return {"HTTP_AUTHORIZATION": f"Bearer {tok}"}


@pytest.fixture()
def owner():
    with psycopg.connect(DSN, autocommit=True) as c:
        db.ensure_namespace(c)
        yield c


@pytest.fixture()
def app():
    c = dbconn.connect(DSN)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture()
def world(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, extra_settings=dict(
        ROOT_URLCONF="meshychat.urls", ALLOWED_HOSTS=["*"], MESHY_DSN=DSN,
        MESHY_JWT_PUBLIC_KEY_PEM=PUB, MESHY_JWT_ISSUER=ISSUER))
    yield w
    w.close()


@pytest.fixture()
def client():
    return Client()


def _reader(owner, tag="relreader") -> dict:
    p = db.peer(owner, tag)
    db.grant(owner, p, "read")
    return p


def _calls(world) -> int:
    return sum(len(h.requests) for h in world.hosts)


def _related_scene(owner, app, world, *, gate=None):
    """a (oldest), c, then b newest: b -> a (pair 2.0) and b -> c (pair 1.5)."""
    if gate is not None:
        world.declare(gate=gate)
    s = Scene(owner, "relroute")
    start_here(owner, world, app)
    s.add("a")
    s.add("c")
    s.add("b")
    pair(world.rr, s, "b", "a", 3.0, 1.0)
    pair(world.rr, s, "b", "c", 1.5, 1.5)
    prepare(app)
    assert relate(app)[-1].status == "ok"
    return s


# ================================================================= context, persisted

def test_context_serves_persisted_edges_beside_related_with_no_model_call(
        client, owner, app, world):
    reader = _reader(owner)
    s = _related_scene(owner, app, world)
    before = _calls(world)
    r = client.get(f"/v1/mesh/context?key={s.rows['b']['key']}&k=1&rerank=0",
                   **_bearer(reader["peer_id"]))
    assert r.status_code == 200, r.content[:400]
    body = r.json()
    assert _calls(world) == before, "the persisted edges cost a model call"
    rel = body["related_edges"]
    assert rel["absent"] is None, rel
    assert [h["key"] for h in rel["hits"]] == [s.rows["a"]["key"], s.rows["c"]["key"]]
    assert rel["truncated"] is False, "two edges, none cut: k is `related`'s, not theirs"
    h = rel["hits"][0]
    assert (h["tier"], h["edge_type"], h["direction"]) == ("inferred", "related_to", "out")
    assert h["scores"]["pair"] == 2.0 and h["scores"]["forward"] == 3.0
    assert h["scores"]["reverse"] == 1.0 and h["scores"]["rank"] == 1
    assert h["scores"]["fused_rank"] >= 1
    assert h["inferred"]["rerank_provider_id"] == "rerank-a"
    assert h["inferred"]["threshold_version"] == world.cal["threshold_version"]
    assert h["inferred"]["space"] == world.space.name
    assert h["body"] == s.text["a"] and h["body_truncated"] is False
    assert h["thread_id"] == s.rows["a"]["thread"]
    assert rel["edges"]["threshold"] == 1.0 and rel["edges"]["k"] == 3
    assert rel["edges"]["processed_at"] is not None
    # `related` is context's own ranking, as before: k=1 of a larger pool, cut and said.
    assert len(body["related"]["hits"]) == 1 and body["related"]["truncated"] is True
    assert all("tier" not in x for x in body["related"]["hits"])
    assert body["method"] == "rrf(lexical,embed)" and body["reranked"] is False

    # The OTHER end sees the same edge, pointing in.
    body = client.get(f"/v1/mesh/context?key={s.rows['a']['key']}&k=5&rerank=0",
                      **_bearer(reader["peer_id"])).json()
    assert [(x["key"], x["direction"]) for x in body["related_edges"]["hits"]] == [
        (s.rows["b"]["key"], "in")]


def test_context_for_an_anchor_without_an_edge(client, owner, app, world):
    reader = _reader(owner)
    s = _related_scene(owner, app, world)
    loner = s.add("loner")
    prepare(app)
    body = client.get(f"/v1/mesh/context?key={loner['key']}&k=5&rerank=0",
                      **_bearer(reader["peer_id"])).json()
    assert body["related_edges"]["hits"] == [] and body["related_edges"]["absent"] is None
    assert body["related_edges"]["edges"]["processed_at"] is None, "not decided yet"
    assert body["method"] == "rrf(lexical,embed)"
    assert all("tier" not in h for h in body["related"]["hits"])


def test_context_related_keeps_its_k_and_recall_when_the_anchor_has_edges(
        client, owner, app, world):
    """Review finding. Persisted edges used to REPLACE context's `related` whenever the
    anchor had one visible edge: a caller asking k=5 got at most the job's k (a list cut
    at a precision-first threshold) and the on-demand ranking was never computed. On the
    calibration's own HQB scores that kept 34 of 63 grade-2 relations where the reranked
    top-10 kept 58. `related` is now context's ranking whatever the edges are, and the
    edges are served beside it."""
    reader = _reader(owner)
    s = Scene(owner, "relk")
    start_here(owner, world, app)
    for n in ("p1", "p2", "p3", "p4", "a", "c", "b"):      # b's pool holds at least 6
        s.add(n)
    pair(world.rr, s, "b", "a", 3.0, 1.0)
    pair(world.rr, s, "b", "c", 1.5, 1.5)
    prepare(app)
    assert relate(app)[-1].status == "ok"
    body = client.get(f"/v1/mesh/context?key={s.rows['b']['key']}&k=5&rerank=0",
                      **_bearer(reader["peer_id"])).json()
    rel = body["related"]
    assert len(rel["hits"]) == 5 and rel.get("source") != "persisted", rel
    assert all("tier" not in h for h in rel["hits"])
    assert {h["key"] for h in body["related_edges"]["hits"]} == {s.rows["a"]["key"],
                                                                 s.rows["c"]["key"]}


# ============================== related hits: what their score is, and the edge beside it

def _units_scene(owner, app, world, *, extra=()):
    """_related_scene plus d, a message in every pool with NO edge (each pair of it scores
    the fake reranker's default -10): d (oldest), a, c, then b; b -> a (fwd 3.0, rev 1.0,
    pair 2.0, rank 1), b -> c (1.5, 1.5, 1.5, rank 2). `extra`: more edgeless messages,
    older than d, so a pool holds enough hits in a run of this test alone."""
    s = Scene(owner, "relunits")
    start_here(owner, world, app)
    for n in (*extra, "d", "a", "c", "b"):
        s.add(n)
    pair(world.rr, s, "b", "a", 3.0, 1.0)
    pair(world.rr, s, "b", "c", 1.5, 1.5)
    prepare(app)
    assert relate(app)[-1].status == "ok"
    return s


def _ctx(client, reader, s, name, *, k=40, rerank=0) -> dict:
    r = client.get(f"/v1/mesh/context?key={s.rows[name]['key']}&k={k}&rerank={rerank}",
                   **_bearer(reader["peer_id"]))
    assert r.status_code == 200, r.content[:400]
    return r.json()


def _hits(body) -> dict:
    return {h["key"]: h for h in body["related"]["hits"]}


def test_related_hits_say_their_score_is_forward_and_carry_the_stored_edge(
        client, owner, app, world):
    """Txxxx's live finding: `related`'s rerank_score is the ONE-WAY logit (the anchor as
    the query), the stored edges decide on pair = mean(forward, reverse), so a hit above
    the threshold with no edge looked like a contradiction. Each hit now names its score's
    kind and carries the edge, if one is stored, with all three numbers."""
    reader = _reader(owner)
    s = _units_scene(owner, app, world)
    tv = world.cal["threshold_version"]
    body = _ctx(client, reader, s, "b", rerank=1)
    assert body["reranked"] is True, body["attempts"]
    hits = _hits(body)
    assert {s.rows[n]["key"] for n in "acd"} <= set(hits), sorted(hits)
    for h in body["related"]["hits"]:
        assert (h["score_kind"], h["score_absent"]) == ("forward", None), h
        assert h["rerank_score"] is not None
    scores = [h["rerank_score"] for h in body["related"]["hits"]]
    assert scores == sorted(scores, reverse=True), "related's order is the forward logit's"
    a, c, d = (hits[s.rows[n]["key"]] for n in "acd")
    assert a["edge"] == {"pair": 2.0, "forward": 3.0, "reverse": 1.0, "rank": 1,
                         "direction": "out", "threshold_version": tv}, a["edge"]
    assert c["edge"] == {"pair": 1.5, "forward": 1.5, "reverse": 1.5, "rank": 2,
                         "direction": "out", "threshold_version": tv}, c["edge"]
    assert d["edge"] is None, "no edge is stored between b and d"
    # b is the edge's source: the hit's one-way logit IS the stored forward.
    assert a["rerank_score"] == a["edge"]["forward"] == 3.0
    assert d["rerank_score"] == -10.0

    # The other end: a's context sees the same edge pointing IN, and there the hit's
    # one-way logit (a as the query) is the stored REVERSE, not the forward.
    body = _ctx(client, reader, s, "a", rerank=1)
    b = _hits(body)[s.rows["b"]["key"]]
    assert b["edge"] == {"pair": 2.0, "forward": 3.0, "reverse": 1.0, "rank": 1,
                         "direction": "in", "threshold_version": tv}, b["edge"]
    assert b["rerank_score"] == b["edge"]["reverse"] == 1.0
    assert (b["score_kind"], b["score_absent"]) == ("forward", None)
    assert _hits(body)[s.rows["d"]["key"]]["edge"] is None


def test_a_hit_with_no_rerank_score_says_why_and_keeps_its_edge(client, owner, app, world):
    reader = _reader(owner)
    s = _units_scene(owner, app, world)
    body = _ctx(client, reader, s, "b", rerank=0)
    assert body["reranked"] is False and body["related"]["hits"]
    for h in body["related"]["hits"]:
        assert (h["rerank_score"], h["score_kind"], h["score_absent"]) == (
            None, "none", "rerank_not_requested"), h
    assert _hits(body)[s.rows["a"]["key"]]["edge"]["pair"] == 2.0, (
        "the stored edge does not depend on this request's reranker")

    world.declare(rerankers=[])                   # asked for, and no reranker to ask
    body = _ctx(client, reader, s, "b", rerank=1)
    assert body["reranked"] is False and body["related"]["hits"]
    for h in body["related"]["hits"]:
        assert (h["rerank_score"], h["score_kind"], h["score_absent"]) == (
            None, "none", "rerank_unavailable"), h
    assert _hits(body)[s.rows["a"]["key"]]["edge"]["pair"] == 2.0


def test_a_declared_pair_shows_no_edge_on_its_related_hit(client, owner, app, world):
    """DECLARED WINS here too. c stays in b's `related` (another thread), but once b is
    declared a reply to c the stored edge between them is the edge reads' to hide, and
    this field must not show it either."""
    reader = _reader(owner)
    s = _units_scene(owner, app, world)
    c_key = s.rows["c"]["key"]
    assert _hits(_ctx(client, reader, s, "b"))[c_key]["edge"] is not None, "control"
    with owner.cursor() as cur:                    # b now DECLARES it replies to c
        cur.execute("INSERT INTO mesh_edges (src_id, src_key, src_version, edge_type, dst, "
                    "src_field, recorded_at) VALUES (%s, %s, 1, 'replies_to', %s, "
                    "'in_reply_to', %s)", (s.rows["b"]["id"], s.rows["b"]["key"], c_key,
                                           s.rows["b"]["created_at"]))
    body = _ctx(client, reader, s, "b")
    hits = _hits(body)
    assert c_key in hits, "control: c is still ranked (it is in another thread)"
    assert hits[c_key]["edge"] is None, hits[c_key]["edge"]
    assert body["related_edges"]["excluded"]["hidden_declared"] == 1
    assert hits[s.rows["a"]["key"]]["edge"]["pair"] == 2.0
    body = _ctx(client, reader, s, "c")           # and from the other end
    assert _hits(body)[s.rows["b"]["key"]]["edge"] is None


def test_the_edge_field_hides_an_end_superseded_after_ranking(
        client, owner, app, world, monkeypatch):
    """`related` and the edge lookup read in different transactions, so a hit ranked as a
    head row can be superseded before its edge is read. The lookup applies the edge reads'
    own visibility to BOTH ends and shows no edge for it. Made deterministic by
    superseding a row just after ranking: first a hit, then the anchor itself."""
    reader = _reader(owner)
    s = _units_scene(owner, app, world)
    a_key, c_key = s.rows["a"]["key"], s.rows["c"]["key"]
    hits = _hits(_ctx(client, reader, s, "b"))
    assert hits[a_key]["edge"] is not None and hits[c_key]["edge"] is not None, "control"

    search = importlib.import_module("apps.meshsem.search")
    real = search._rerank
    retire: list = []

    def ranked_then_superseded(query, pool, requested):
        out = real(query, pool, requested)
        with owner.cursor() as cur:
            cur.execute("UPDATE shared_memory SET is_head = FALSE WHERE id = %s",
                        (retire.pop(),))
        return out

    monkeypatch.setattr(search, "_rerank", ranked_then_superseded)
    retire.append(s.rows["a"]["id"])                          # the hit a
    hits = _hits(_ctx(client, reader, s, "b"))
    assert a_key in hits, "control: a was ranked before it was superseded"
    assert hits[a_key]["edge"] is None, "an edge to a superseded hit was shown"
    assert hits[c_key]["edge"] is not None, "control: c's edge is untouched"
    retire.append(s.rows["b"]["id"])                          # the anchor b
    hits = _hits(_ctx(client, reader, s, "b"))
    assert c_key in hits, "control: c was ranked before the anchor was superseded"
    assert hits[c_key]["edge"] is None, "an edge of a superseded anchor was shown"


def test_the_edge_lookup_applies_the_gate_to_both_ends(client, owner, app, world):
    """Through the route the lookup's own gate never bites first: a request reads the gate
    once, a hit passed it to be ranked, a forbidden anchor ranks nothing, and embed_policy
    is immutable. It is still the edge reads' gate, held here on the function the route
    calls, for an edge with a forbidden TARGET and one with a forbidden SOURCE."""
    reader = _reader(owner)
    world.declare(gate=True)
    s = Scene(owner, "relunitgate")
    start_here(owner, world, app)
    s.add("f", policy="forbidden")            # a forbidden TARGET: src -> f
    s.add("src")
    s.add("old")                              # a forbidden SOURCE: f2 -> old
    s.add("f2", policy="forbidden")
    pair(world.rr, s, "src", "f", 3.0, 3.0)
    pair(world.rr, s, "f2", "old", 3.0, 3.0)
    prepare(app)
    relate(app)
    hit = _hits(_ctx(client, reader, s, "src"))[s.rows["f"]["key"]]
    assert hit["edge"] is not None and hit["edge"]["direction"] == "out", (
        "control: with the gate open the route shows it")
    reads = importlib.import_module("apps.meshsem.related_reads")
    for anchor, other, direction in (("src", "f", "out"), ("old", "f2", "in")):
        a, ids = {"id": s.rows[anchor]["id"]}, [s.id(other)]
        shown = reads.for_related_hits(app, world.space, a, ids, gate=True)
        assert shown[s.id(other)]["direction"] == direction, "control"
        assert reads.for_related_hits(app, world.space, a, ids, gate=False) == {}, (
            f"an edge with a forbidden {'target' if direction == 'out' else 'source'} "
            "was served with the gate closed")


def test_the_edge_field_serves_only_the_current_calibration_in_the_configured_space(
        client, owner, app, world):
    reader = _reader(owner)
    s = _units_scene(owner, app, world)
    a_key, d_key = s.rows["a"]["key"], s.rows["d"]["key"]
    # A b -> d edge in ANOTHER space, same calibration version: never this space's.
    with owner.cursor() as cur:
        cur.execute("INSERT INTO mesh_related_edges (src_id, src_key, src_version, dst_id, "
                    "dst_key, dst_version, space, embed_provider_id, rerank_provider_id, "
                    "rerank_model, rerank_reference, threshold_version, score_fwd, "
                    "score_rev, pair_score, threshold, rank, fused_rank, recorded_at, "
                    "dst_recorded_at) SELECT e.src_id, e.src_key, 1, %s, %s, 1, %s, "
                    "'e', 'r', 'm', e.rerank_reference, e.threshold_version, 9, 9, 9, 1, "
                    "1, 1, e.recorded_at, %s FROM mesh_related_edges e "
                    "WHERE e.space = %s AND e.src_id = %s AND e.rank = 1",
                    (s.rows["d"]["id"], d_key, world.space.name + "-other",
                     s.rows["d"]["created_at"], world.space.name, s.rows["b"]["id"]))
        assert cur.rowcount == 1, "control: the other space's edge exists"
    hits = _hits(_ctx(client, reader, s, "b"))
    assert hits[a_key]["edge"] is not None, "control"
    assert hits[d_key]["edge"] is None, "an edge of another space was shown"

    world.calibrate(threshold=2.5, k=2)            # a new version; no tick has run under it
    assert len(edges(owner, world.space, [s.rows["b"]["id"]])) == 2, "control: rows remain"
    body = _ctx(client, reader, s, "b")
    assert a_key in _hits(body), "control: a is still ranked"
    assert all(h["edge"] is None for h in body["related"]["hits"]), [
        (h["key"], h["edge"]) for h in body["related"]["hits"] if h["edge"]]


def test_the_edges_of_every_related_hit_are_read_in_one_query(
        client, owner, app, world, monkeypatch):
    """No N+1: the statements that read mesh_related_edges during a context request are
    as many for 6 hits as for 1. Reranked, so a (3.0) and c (1.5) lead whatever else the
    run's namespace holds."""
    reader = _reader(owner)
    s = _units_scene(owner, app, world, extra=("e1", "e2", "e3", "e4"))
    seen: list[str] = []
    real = psycopg.Cursor.execute

    def spy(self, query, *args, **kwargs):
        seen.append(query if isinstance(query, str) else repr(query))
        return real(self, query, *args, **kwargs)

    monkeypatch.setattr(psycopg.Cursor, "execute", spy)

    def reads(k) -> tuple[int, dict]:
        seen.clear()
        body = _ctx(client, reader, s, "b", k=k, rerank=1)
        return sum(1 for q in seen if "mesh_related_edges" in q), body

    one, body1 = reads(1)
    six, body6 = reads(6)
    assert len(body1["related"]["hits"]) == 1 and len(body6["related"]["hits"]) == 6
    assert [h["key"] for h in body6["related"]["hits"] if h["edge"]] == [
        s.rows["a"]["key"], s.rows["c"]["key"]], "control: a and c lead, with their edges"
    assert six == one, f"{six} edge reads for 6 hits, {one} for 1: a query per hit"


def test_the_docs_say_related_is_one_way_and_edge_is_the_pair():
    """The README and divergence 032 say what `score_kind` and `edge` mean, in the code's
    own values, and that `related` is ordered by the one-way logit. The stale claim that
    context's change to search.py modified nothing is gone (it held on f7a434b)."""
    search = importlib.import_module("apps.meshsem.search")
    root = Path(__file__).resolve().parent.parent
    values = (search.SCORE_FORWARD, search.SCORE_NONE,
              search._score_fields(None, False)["score_absent"],
              search._score_fields(None, True)["score_absent"])
    for rel in ("apps/meshsem/README.md", "docs/DIVERGENCES.md"):
        text = " ".join((root / rel).read_text(encoding="utf-8").split()).lower()
        for phrase in ("score_kind", "score_absent", "mean(forward, reverse)",
                       "is ordered by it", "edge.forward", "edge.reverse",
                       "judged below the threshold", *(f'"{v}"' for v in values)):
            assert phrase in text, (rel, phrase)
        assert "the change to `search.py` is four added lines" not in text, rel


# ============================================== a reader never sees what it cannot read

def test_an_edge_to_a_superseded_row_is_hidden(client, owner, app, world):
    reader = _reader(owner)
    s = _related_scene(owner, app, world)
    url = f"/v1/mesh/context?key={s.rows['b']['key']}&k=5&rerank=0"
    keys = lambda: [h["key"] for h in client.get(url, **_bearer(reader["peer_id"])  # noqa: E731
                                                 ).json()["related_edges"]["hits"]]
    assert set(keys()) == {s.rows["a"]["key"], s.rows["c"]["key"]}, "control"
    with owner.cursor() as cur:                    # a is superseded (the head flag flips)
        cur.execute("UPDATE shared_memory SET is_head = FALSE WHERE id = %s",
                    (s.rows["a"]["id"],))
    assert keys() == [s.rows["c"]["key"]]


def test_an_edge_to_a_forbidden_row_is_hidden_the_moment_the_gate_closes(
        client, owner, app, world):
    reader = _reader(owner)
    world.declare(gate=True)
    s = Scene(owner, "relgate")
    start_here(owner, world, app)
    s.add("f", policy="forbidden")            # a forbidden TARGET: src -> f
    s.add("src")
    s.add("old")                              # a forbidden SOURCE: f2 -> old
    s.add("f2", policy="forbidden")
    pair(world.rr, s, "src", "f", 3.0, 3.0)
    pair(world.rr, s, "f2", "old", 3.0, 3.0)
    prepare(app)
    relate(app)
    url = f"/v1/mesh/context?key={s.rows['src']['key']}&k=5&rerank=0"
    url_old = f"/v1/mesh/context?key={s.rows['old']['key']}&k=5&rerank=0"
    body = client.get(url, **_bearer(reader["peer_id"])).json()
    assert [h["key"] for h in body["related_edges"]["hits"]] == [s.rows["f"]["key"]], \
        "control"
    body = client.get(url_old, **_bearer(reader["peer_id"])).json()
    assert [(h["key"], h["direction"]) for h in body["related_edges"]["hits"]] == [
        (s.rows["f2"]["key"], "in")], "control"
    world.declare(gate=False)
    body = client.get(url, **_bearer(reader["peer_id"])).json()
    assert s.rows["f"]["key"] not in [h["key"] for h in body["related_edges"]["hits"]]
    body = client.get(url_old, **_bearer(reader["peer_id"])).json()
    assert s.rows["f2"]["key"] not in [h["key"] for h in body["related_edges"]["hits"]], (
        "an edge FROM a forbidden message was shown with the gate closed")
    assert len(edges(owner, world.space, [s.rows["src"]["id"], s.rows["f2"]["id"]])) == 2, (
        "the rows still exist; the READ hid them, before any job purged them")


def test_an_edge_into_another_namespace_is_never_returned(client, owner, app, world):
    reader = _reader(owner)
    s = _related_scene(owner, app, world)
    foreign_id = uuid.uuid4()
    other_ns = f"test/relother-{s.uid}"
    with owner.cursor() as cur:
        cur.execute("INSERT INTO shared_memory_namespace (namespace, owner_peer_id, "
                    "owner_owner_id, claimant_plane, ephemeral, retention_policy) "
                    "VALUES (%s, %s, %s, 'dev', FALSE, 'keep_all')",
                    (other_ns, s.p["peer_id"], s.p["owner_id"]))
        # With the bytes a writer would have signed (divergence 051).
        sr.insert(owner, sr.row(s.p["peer_id"], s.p["owner_id"], other_ns,
                                f"msg/0-{foreign_id.hex}", {"body": "elsewhere"},
                                rid=foreign_id, call_id=f"call-{foreign_id}",
                                author_label="x", created_at=s.base - _dt.timedelta(days=1),
                                embed_policy="allowed"))
        cur.execute("INSERT INTO mesh_related_edges (src_id, src_key, src_version, dst_id, "
                    "dst_key, dst_version, space, embed_provider_id, rerank_provider_id, "
                    "rerank_model, rerank_reference, threshold_version, score_fwd, "
                    "score_rev, pair_score, threshold, rank, fused_rank, recorded_at, "
                    "dst_recorded_at) SELECT e.src_id, e.src_key, 1, %s, %s, 1, e.space, "
                    "'e', 'r', 'm', e.rerank_reference, e.threshold_version, 9, 9, 9, 1, "
                    "20, 1, e.recorded_at, %s FROM mesh_related_edges e "
                    "WHERE e.src_id = %s LIMIT 1",
                    (foreign_id, f"msg/0-{foreign_id.hex}",
                     s.base - _dt.timedelta(days=1), s.rows["b"]["id"]))
        assert cur.rowcount == 1, "control: the foreign edge exists"
    body = client.get(f"/v1/mesh/context?key={s.rows['b']['key']}&k=10&rerank=0",
                      **_bearer(reader["peer_id"])).json()
    assert {h["key"] for h in body["related_edges"]["hits"]} == {s.rows["a"]["key"],
                                                                  s.rows["c"]["key"]}
    body = client.get(f"/v1/mesh/neighbours?key={s.rows['b']['key']}&include_inferred=1",
                      **_bearer(reader["peer_id"])).json()
    assert {h["key"] for h in body["inferred"]["related_to"]} == {s.rows["a"]["key"],
                                                                  s.rows["c"]["key"]}


def test_a_deleted_target_takes_its_edge_and_a_stranger_gets_403(client, owner, app, world):
    reader = _reader(owner)
    stranger = db.peer(owner, "relstranger")
    s = _related_scene(owner, app, world)
    for url in (f"/v1/mesh/context?key={s.rows['b']['key']}",
                f"/v1/mesh/neighbours?key={s.rows['b']['key']}&include_inferred=1"):
        denied = client.get(url, **_bearer(stranger["peer_id"]))
        assert denied.status_code == 403, (url, denied.content[:200])
        assert client.get(url, **_bearer(reader["peer_id"])).status_code == 200  # control
    with owner.cursor() as cur:
        cur.execute("DELETE FROM shared_memory WHERE id = %s", (s.rows["a"]["id"],))
    body = client.get(f"/v1/mesh/context?key={s.rows['b']['key']}&k=5&rerank=0",
                      **_bearer(reader["peer_id"])).json()
    assert [h["key"] for h in body["related_edges"]["hits"]] == [s.rows["c"]["key"]]


def test_an_edge_between_messages_now_declared_in_one_thread_is_hidden(
        client, owner, app, world):
    """DECLARED WINS. A later `apps.mesh rebuild` can place two messages in one thread
    (or link them by a reply); the inferred edge between them is then hidden, and
    counted, rather than shown beside the declaration it duplicates."""
    reader = _reader(owner)
    s = _related_scene(owner, app, world)
    url = f"/v1/mesh/context?key={s.rows['b']['key']}&k=5&rerank=0"
    body = client.get(url, **_bearer(reader["peer_id"])).json()
    assert {h["key"] for h in body["related_edges"]["hits"]} == {
        s.rows["a"]["key"], s.rows["c"]["key"]}, "control"
    with owner.cursor() as cur:        # c is now DECLARED in b's thread
        cur.execute("INSERT INTO mesh_edges (src_id, src_key, src_version, edge_type, dst, "
                    "src_field, recorded_at) VALUES (%s, %s, 1, 'in_thread', %s, "
                    "'thread_id', %s)", (s.rows["c"]["id"], s.rows["c"]["key"],
                                         s.rows["b"]["thread"], s.rows["c"]["created_at"]))
    body = client.get(url, **_bearer(reader["peer_id"])).json()
    assert [h["key"] for h in body["related_edges"]["hits"]] == [s.rows["a"]["key"]]
    assert body["related_edges"]["excluded"]["hidden_declared"] == 1
    nb = client.get(f"/v1/mesh/neighbours?key={s.rows['b']['key']}&include_inferred=1",
                    **_bearer(reader["peer_id"])).json()["inferred"]
    assert [h["key"] for h in nb["related_to"]] == [s.rows["a"]["key"]]
    assert nb["hidden_declared"] == 1


def test_an_edited_newer_end_is_hidden_from_the_older_ends_reads(client, owner, app, world):
    """The mirror of test_an_edge_to_a_superseded_row_is_hidden: supersede the NEWER end
    (the edge's source, an edited message) and the older end's reads stop showing it, so
    the old version's body can never come back as an "in" hit. Both ends join head rows."""
    reader = _reader(owner)
    s = _related_scene(owner, app, world)
    url = f"/v1/mesh/neighbours?key={s.rows['a']['key']}&include_inferred=1"
    keys = lambda: [h["key"] for h in client.get(url, **_bearer(reader["peer_id"])  # noqa: E731
                                                 ).json()["inferred"]["related_to"]]
    assert keys() == [s.rows["b"]["key"]], "control: a sees b's edge, pointing in"
    with owner.cursor() as cur:
        cur.execute("UPDATE shared_memory SET is_head = FALSE WHERE id = %s",
                    (s.rows["b"]["id"],))
    assert keys() == [], "a superseded source was served from the older end"


def test_reads_serve_only_the_current_calibrations_edges(client, owner, app, world):
    """A new calibration is committed and the job has not run under it yet: the old
    version's edges, which may sit below the new threshold, are not served."""
    reader = _reader(owner)
    s = _related_scene(owner, app, world)
    url = f"/v1/mesh/neighbours?key={s.rows['b']['key']}&include_inferred=1"
    body = client.get(url, **_bearer(reader["peer_id"])).json()
    assert {h["key"] for h in body["inferred"]["related_to"]} == {
        s.rows["a"]["key"], s.rows["c"]["key"]}, "control"
    world.calibrate(threshold=2.5, k=2)            # a new version; no tick has run under it
    assert len(edges(owner, world.space, [s.rows["b"]["id"]])) == 2, "control: rows remain"
    body = client.get(url, **_bearer(reader["peer_id"])).json()
    assert body["inferred"]["related_to"] == [], body["inferred"]["related_to"]


def test_coverage_counts_a_gated_edge_under_no_other_heading(client, owner, app, world):
    """`hidden_superseded` counts edges the reads hide because an end is no longer a head
    row. An edge to a FORBIDDEN row under a closed gate is the gate's to hide: every
    coverage figure filters both ends by namespace and gate, and this one did not."""
    reader = _reader(owner)
    world.declare(gate=True)
    s = Scene(owner, "relcov")
    start_here(owner, world, app)
    s.add("f", policy="forbidden")
    s.add("a")
    s.add("src")
    pair(world.rr, s, "src", "f", 3.0, 3.0)
    pair(world.rr, s, "src", "a", 2.0, 2.0)
    prepare(app)
    relate(app)
    with owner.cursor() as cur:                 # both targets edited: neither is a head
        cur.execute("UPDATE shared_memory SET is_head = FALSE WHERE id = ANY(%s::uuid[])",
                    ([str(s.rows["f"]["id"]), str(s.rows["a"]["id"])],))
    world.declare(gate=False)                   # closed, and no tick has purged yet
    importlib.import_module("apps.meshsem.related_reads").forget()
    assert len(edges(owner, world.space, [s.rows["src"]["id"]])) == 2, "control: unpurged"
    cov = client.get(f"/v1/mesh/neighbours?key={s.rows['src']['key']}&include_inferred=1",
                     **_bearer(reader["peer_id"])).json()["inferred"]["coverage"]
    assert cov["hidden_superseded"] == 1, cov   # a's edge; f's is behind the closed gate


# =============================================================== a request writes nothing

def _snapshot(owner, space) -> tuple:
    with owner.cursor() as cur:
        cur.execute("SELECT * FROM mesh_related_edges WHERE space = %s "
                    "ORDER BY src_id, dst_id", (space.name,))
        e = cur.fetchall()
        cur.execute("SELECT * FROM mesh_related_runs WHERE space = %s ORDER BY src_id",
                    (space.name,))
        r = cur.fetchall()
        cur.execute("SELECT * FROM mesh_related_projection WHERE space = %s", (space.name,))
        p = cur.fetchall()
    assert e and r and p, "control: the snapshot holds rows to compare"
    return e, r, p


def test_no_request_writes_a_related_table(client, owner, app, world):
    reader = _reader(owner)
    s = _related_scene(owner, app, world)
    before = _snapshot(owner, world.space)
    for url in (f"/v1/mesh/context?key={s.rows['b']['key']}",
                f"/v1/mesh/context?key={s.rows['a']['key']}&rerank=1",
                f"/v1/mesh/neighbours?key={s.rows['b']['key']}&include_inferred=1",
                f"/v1/mesh/neighbours?key={s.rows['c']['key']}"):
        assert client.get(url, **_bearer(reader["peer_id"])).status_code == 200, url
    assert _snapshot(owner, world.space) == before, "a request path wrote a related table"


# ============================================================================ neighbours

def test_neighbours_is_declared_only_unless_asked(client, owner, app, world):
    reader = _reader(owner)
    s = Scene(owner, "relnb")
    start_here(owner, world, app)
    t = f"t-{s.uid}-main"
    s.add("root", thread=t)
    s.add("other")
    s.add("mid", thread=t, reply_to="root")
    s.add("leaf", thread=t, reply_to="mid")
    pair(world.rr, s, "mid", "other", 3.0, 2.0)
    prepare(app)
    relate(app)
    with owner.cursor() as cur:        # the auditor's inference for mid, planted
        cur.execute("INSERT INTO mesh_inferred_edges (src_id, src_key, src_version, "
                    "edge_type, dst, model, provider_id, prompt_version, recorded_at) "
                    "VALUES (%s, %s, 1, 'replies_to', %s, 'gemma', 'chat-a', 'pv1', %s)",
                    (s.rows["mid"]["id"], s.rows["mid"]["key"], s.rows["other"]["key"],
                     s.rows["mid"]["created_at"]))

    url = f"/v1/mesh/neighbours?key={s.rows['mid']['key']}"
    r = client.get(url, **_bearer(reader["peer_id"]))
    assert r.status_code == 200, r.content[:300]
    body = r.json()
    d = body["declared"]
    assert d["replies_to"]["key"] == s.rows["root"]["key"] and d["replies_to"]["found"]
    assert d["replies_to"]["tier"] == "declared"
    assert [h["key"] for h in d["replied_by"]] == [s.rows["leaf"]["key"]]
    assert d["thread"] == {"thread_id": t, "size": 3, "size_is_projected_count": True}
    assert body["inferred"] == {"included": False}
    assert "body" not in str(body), "the graph routes return no message bodies"

    body = client.get(url + "&include_inferred=1", **_bearer(reader["peer_id"])).json()
    inf = body["inferred"]
    assert inf["included"] is True
    rel = inf["related_to"]
    assert [(h["key"], h["direction"], h["tier"]) for h in rel] == [
        (s.rows["other"]["key"], "out", "inferred")]
    assert rel[0]["thread_id"] == s.rows["other"]["thread"]
    assert rel[0]["scores"]["pair"] == 2.5 and "body" not in rel[0]
    assert [(x["dst"], x["tier"], x["inferred"]["model"]) for x in inf["replies_to"]] == [
        (s.rows["other"]["key"], "inferred", "gemma")]
    cov = inf["coverage"]
    for k in ("eligible", "processed", "with_edges", "edges", "hidden_superseded",
              "hidden_declared", "threshold_version", "last_status", "last_status_at"):
        assert k in cov, (k, cov)
    assert cov["threshold_version"] == world.cal["threshold_version"]
    assert inf["truncated"] is False


def test_neighbours_refuses_bad_parameters_and_counts_its_use(client, owner, app, world):
    reader = _reader(owner)
    s = _related_scene(owner, app, world)
    auth = _bearer(reader["peer_id"])
    base = f"/v1/mesh/neighbours?key={s.rows['b']['key']}"
    for bad in ("&include_inferred=yes", "&limit=101", "&limit=abc", "&limit=0"):
        r = client.get(base + bad, **auth)
        assert r.status_code == 422 and r.json()["error"] == "E_VALIDATION", bad
    assert client.get("/v1/mesh/neighbours?key=wiki/x", **auth).status_code == 422
    missing = client.get("/v1/mesh/neighbours?key=msg/0-nosuchkey", **auth)
    assert missing.status_code == 404 and missing.json()["error"] == "E_NOT_FOUND"
    ok = client.get(base + "&limit=100&include_inferred=1", **auth)
    assert ok.status_code == 200 and ok.json()["limit"] == 100
    one = client.get(base + "&limit=1&include_inferred=1", **auth).json()
    assert len(one["inferred"]["related_to"]) == 1 and one["inferred"]["truncated"] is True
    assert client.post(base, **auth).status_code == 405
    with owner.cursor() as cur:
        cur.execute("SELECT outcome, calls FROM mesh_route_usage WHERE route = 'neighbours' "
                    "AND peer_id = %s ORDER BY outcome", (reader["peer_id"],))
        counted = dict(cur.fetchall())
    assert counted.get("ok", 0) >= 2 and counted.get("error", 0) >= 5, counted


def test_two_hops_cross_a_thread_boundary(client, owner, app, world):
    """No server-side multi-hop: each call is one hop, and each neighbour carries its
    thread, so a client walks c -> b -> a across three threads in two calls."""
    reader = _reader(owner)
    s = Scene(owner, "relhop")
    start_here(owner, world, app)
    s.add("a")
    s.add("b")
    s.add("c")
    pair(world.rr, s, "b", "a", 3.0, 3.0)
    pair(world.rr, s, "c", "b", 3.0, 3.0)
    prepare(app)
    relate(app)
    auth = _bearer(reader["peer_id"])
    hop1 = client.get(f"/v1/mesh/neighbours?key={s.rows['c']['key']}&include_inferred=1",
                      **auth).json()["inferred"]["related_to"]
    assert [(h["key"], h["thread_id"]) for h in hop1] == [
        (s.rows["b"]["key"], s.rows["b"]["thread"])]
    hop2 = client.get(f"/v1/mesh/neighbours?key={hop1[0]['key']}&include_inferred=1",
                      **auth).json()["inferred"]["related_to"]
    assert {(h["key"], h["direction"], h["thread_id"]) for h in hop2} == {
        (s.rows["a"]["key"], "out", s.rows["a"]["thread"]),
        (s.rows["c"]["key"], "in", s.rows["c"]["thread"])}
    assert len({s.rows[n]["thread"] for n in "abc"}) == 3


def test_the_neighbours_route_is_in_the_usage_route_set():
    usage = importlib.import_module("apps.mesh.usage")
    assert "neighbours" in usage.ROUTES
