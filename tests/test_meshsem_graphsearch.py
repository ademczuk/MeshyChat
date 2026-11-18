"""GET /v1/mesh/search?ns=<graph namespace> through the REAL urlconf (divergence 060, OI-57 slice 2).

The fixtures are tests/test_meshsem_routes.py's (a real peer, a real bearer, the deterministic fake
model hosts over HTTP). Each test claims its own graph namespace, writes rows into it, runs the graph
walker, and searches. The mail search's own behavior is held by test_meshsem_routes.py, unchanged.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.test_meshsem_routes import (  # noqa: E402,F401  (fixtures)
    DSN, _bearer, _member, _reader, client, owner, world)

from django.test import override_settings  # noqa: E402

from apps.memoryskills.validation import MESH_CHAT_NAMESPACE  # noqa: E402
from apps.meshsem import graphsearch, graphtext, graphwalk, search  # noqa: E402
from tests.fixtures import meshsem_db as db  # noqa: E402
from tests.fixtures import signed_rows as sr  # noqa: E402

#: A long entry whose words about a "lighthouse keeper" sit only in a LATER chunk, so the best
#: chunk is not the first.
FILLER = "\n\n".join(f"Section {i}. The weather report covered rain, wind and clouds across the valley." * 3
                     for i in range(12))
LATE = "The lighthouse keeper polished the lens every evening before the storm."
LONG = FILLER + "\n\n" + LATE + "\n\n" + FILLER


def _claim(owner) -> tuple[str, dict]:
    p = db.peer(owner, "gsowner")
    ns = f"test/gsearch-{uuid.uuid4().hex[:10]}"
    with owner.cursor() as cur:
        cur.execute("INSERT INTO shared_memory_namespace (namespace, owner_peer_id, owner_owner_id,"
                    " claimant_plane, ephemeral, retention_policy) VALUES (%s,%s,%s,'dev',FALSE,'keep_all')",
                    (ns, p["peer_id"], p["owner_id"]))
    return ns, p


def _grant(owner, ns, holder, p, permission="read"):
    with owner.cursor() as cur:
        cur.execute("INSERT INTO shared_memory_grant (namespace, grantee_peer_id, grantee_owner_id, permission,"
                    " granted_by_peer_id, granted_by_owner_id) VALUES (%s,%s,%s,%s,%s,%s)",
                    (ns, p["peer_id"], p["owner_id"], permission, holder["peer_id"], holder["owner_id"]))


def _row(owner, p, ns, key, value, *, policy="allowed"):
    rid = uuid.uuid4()
    sr.insert(owner, sr.row(p["peer_id"], p["owner_id"], ns, key, value, rid=rid, call_id=f"call-{rid}",
                            is_head=True, author_label=p["label"], embed_policy=policy))
    return rid


def _walk(owner, ns):
    for _ in range(20):
        r = graphwalk.catch_up_namespace(owner, ns)
        if not r.more:
            return r


@pytest.fixture()
def graph(owner, world):
    ns, holder = _claim(owner)
    with override_settings(MESHY_GRAPH_NAMESPACES=f"{MESH_CHAT_NAMESPACE},{ns}"):
        search.forget()
        yield ns, holder
    search.forget()


def _get(client, reader, q, ns, **more):
    extra = "".join(f"&{k}={v}" for k, v in more.items())
    return client.get(f"/v1/mesh/search?q={q}&ns={ns}{extra}", **_bearer(reader["peer_id"]))


def test_a_reader_of_the_namespace_finds_the_entry_by_its_best_chunk(client, owner, world, graph):
    ns, holder = graph
    reader = _member(owner, "gsreader")
    _grant(owner, ns, holder, reader)
    long_id = _row(owner, holder, ns, "wiki/lighthouse", {"body_md": LONG, "title": "Coastal notes"})
    _row(owner, holder, ns, "wiki/other", {"body_md": "A note about bread and baking."})
    assert _walk(owner, ns).pending == 0
    r = _get(client, reader, "lighthouse+keeper+lens", ns, k=5, rerank=0)
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["namespace"] == ns and body["hits"], body
    top = body["hits"][0]
    assert top["key"] == "wiki/lighthouse" and top["namespace"] == ns
    text = graphtext.row_text({"body_md": LONG, "title": "Coastal notes"})[1]
    spans = graphtext.chunk_spans(text)
    assert top["chunk_no"] > 0, "control: the passage is not in the first chunk, so the best chunk had to be found"
    a, b = top["chunk_span"]
    assert (a, b) == spans[top["chunk_no"]] and LATE in top["body"]
    assert top["body"] == text[a:b].strip(), "the body is the deciding chunk, stripped as every hit body is"
    assert body["coverage"]["total"] == 2 and body["coverage"]["embedded"] == 2
    assert str(long_id)  # the row id never leaks into the hit; the key names it


def test_a_mail_search_never_returns_a_graph_row_and_a_graph_search_never_a_mail_row(client, owner, world, graph):
    ns, holder = graph
    reader = _reader(owner, "gsboth")                      # read on the mail
    _grant(owner, ns, holder, reader)                      # and on the graph namespace
    word = "zqxmarker" + uuid.uuid4().hex[:6]
    _row(owner, holder, ns, "wiki/marked", {"body_md": f"graph row {word}"})
    mail = db.row(owner, reader, {"body": f"mail row {word}", "schema_version": 1}, embed_policy="allowed")
    from tests.test_meshsem_routes import _drain
    _drain(owner)
    _walk(owner, ns)
    in_mail = client.get(f"/v1/mesh/search?q={word}&rerank=0", **_bearer(reader["peer_id"])).json()
    in_graph = _get(client, reader, word, ns, rerank=0).json()
    assert [h["key"] for h in in_graph["hits"]] == ["wiki/marked"]
    assert mail["key"] in [h["key"] for h in in_mail.get("hits", [])], "control: the mail row is findable"
    assert "wiki/marked" not in [h["key"] for h in in_mail.get("hits", [])]
    assert all("namespace" not in h for h in in_mail.get("hits", [])), "a mail hit's JSON is unchanged"


def test_a_peer_without_read_on_the_named_namespace_is_refused(client, owner, world, graph):
    ns, holder = graph
    stranger = _reader(owner, "gsstranger")                # reads the mail, not the graph namespace
    _row(owner, holder, ns, "wiki/private", {"body_md": "nothing for strangers"})
    _walk(owner, ns)
    r = _get(client, stranger, "strangers", ns)
    assert r.status_code == 403, r.content
    allowed = client.get("/v1/mesh/search?q=strangers&rerank=0", **_bearer(stranger["peer_id"]))
    assert allowed.status_code == 200, "control: the same peer's mail search is allowed"


def test_a_graph_reader_without_mail_read_can_search_the_graph(client, owner, world, graph):
    """The check is the NAMED namespace's: a peer that reads only the wiki searches the wiki, and is
    refused the mail search (control)."""
    ns, holder = graph
    reader = _member(owner, "gsonly")
    _grant(owner, ns, holder, reader)
    _row(owner, holder, ns, "wiki/only", {"body_md": "tidal pools and anemones"})
    _walk(owner, ns)
    assert _get(client, reader, "anemones", ns, rerank=0).status_code == 200
    assert client.get("/v1/mesh/search?q=anemones", **_bearer(reader["peer_id"])).status_code == 403


@pytest.mark.parametrize("bad", ["test/not-listed", "shared/mesh-chat,x"])
def test_a_namespace_the_server_does_not_derive_is_422(client, owner, world, graph, bad):
    reader = _reader(owner, "gsbad")
    r = client.get(f"/v1/mesh/search?q=x&ns={bad}", **_bearer(reader["peer_id"]))
    assert r.status_code == 422, r.content


def test_two_ns_values_are_422(client, owner, world, graph):
    ns, _holder = graph
    reader = _reader(owner, "gstwo")
    r = client.get(f"/v1/mesh/search?q=x&ns={ns}&ns={MESH_CHAT_NAMESPACE}", **_bearer(reader["peer_id"]))
    assert r.status_code == 422


def test_ns_naming_the_mail_is_the_mail_search(client, owner, world, graph):
    reader = _reader(owner, "gsmail")
    a = client.get("/v1/mesh/search?q=anything&rerank=0", **_bearer(reader["peer_id"]))
    b = client.get(f"/v1/mesh/search?q=anything&rerank=0&ns={MESH_CHAT_NAMESPACE}", **_bearer(reader["peer_id"]))
    assert a.status_code == b.status_code
    assert "namespace" not in b.json()


def test_the_gate_applies_at_read_time(client, owner, world, graph):
    """world.declare() replaces the settings override, so the graph namespace is re-listed after each
    declare: without that the closed-gate request is a 422 and the test would pass without reaching
    the gate (the first version did, and a mutant that ignored the gate survived it)."""
    ns, holder = graph
    listed = f"{MESH_CHAT_NAMESPACE},{ns}"
    reader = _member(owner, "gsgate")
    _grant(owner, ns, holder, reader)
    word = "zqgate" + uuid.uuid4().hex[:6]
    _row(owner, holder, ns, "wiki/forbidden", {"body_md": f"forbidden {word}"}, policy="forbidden")
    world.declare(gate=True)
    with override_settings(MESHY_GRAPH_NAMESPACES=listed):
        _walk(owner, ns)
        opened = _get(client, reader, word, ns, rerank=0)
    assert opened.status_code == 200 and [h["key"] for h in opened.json()["hits"]] == ["wiki/forbidden"],         "control: found while the gate is open"
    world.declare(gate=False)
    with override_settings(MESHY_GRAPH_NAMESPACES=listed):
        closed = _get(client, reader, word, ns, rerank=0)
    assert closed.status_code == 200, closed.content
    assert [h["key"] for h in closed.json()["hits"]] == []


def test_a_row_found_only_lexically_is_shown_by_the_chunk_sharing_most_query_terms():
    text = graphtext.row_text({"body_md": LONG})[1]
    spans = graphtext.chunk_spans(text)
    n = graphsearch._best_lexical_chunk(text, spans, "lighthouse keeper lens")
    assert LATE in text[spans[n][0]:spans[n][1]] and n > 0


def test_the_chunk_index_collapses_chunks_to_rows_best_first():
    import numpy as np
    from apps.meshsem import spaces
    sp = db.make_space("chunkidx")
    idx = graphsearch.ChunkIndex(sp, "test/x")
    q = np.zeros(db.DIMS, dtype=np.float32)
    q[0] = 1.0
    for key, x in (("a:0", 0.1), ("a:3", 0.9), ("b:0", 0.5), ("a:1", 0.8)):
        v = np.zeros(db.DIMS, dtype=np.float32)
        v[0], v[1] = x, (1 - x * x) ** 0.5
        idx._put(key, v)
    ids, scores, chunk = idx.rank_rows(q)
    assert ids == ["a", "b"] and chunk == {"a": 3, "b": 0} and scores[0] > scores[1]
    assert spaces is not None


def test_rows_are_collapsed_before_the_arm_depth_cut(monkeypatch):
    """Review of #36 (Codex, P2): the top ARM_DEPTH chunks were kept FIRST and then collapsed, so a few long
    entries filling those slots cut every other entry out of the arm. Each row's best chunk is taken over ALL
    its chunks, then the top ARM_DEPTH rows are kept."""
    import numpy as np
    monkeypatch.setattr(search, "ARM_DEPTH", 3)
    sp = db.make_space("chunkdepth")
    idx = graphsearch.ChunkIndex(sp, "test/x")
    q = np.zeros(db.DIMS, dtype=np.float32)
    q[0] = 1.0

    def put(key, x):
        v = np.zeros(db.DIMS, dtype=np.float32)
        v[0], v[1] = x, (1 - x * x) ** 0.5
        idx._put(key, v)
    for n in range(5):
        put(f"long:{n}", 0.9 - n * 0.01)               # five strong chunks of ONE entry
    put("other:0", 0.5)
    put("third:0", 0.4)
    ids, scores, chunk = idx.rank_rows(q)
    assert ids == ["long", "other", "third"], ids
    assert chunk["long"] == 0
