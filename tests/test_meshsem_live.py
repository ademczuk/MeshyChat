"""The live tests against the real LAN models. Skipped unless MESHY_LIVE_MODELS=1.

    MESHY_LIVE_MODELS=1 MESHY_TEST_DSN=postgresql://... MESHY_LIVE_EMBED=http://<a>,http://<b>
        MESHY_LIVE_RERANK=http://<r> MESHY_LIVE_CHAT=http://<c> pytest tests/test_meshsem_live.py -q

Every endpoint is REQUIRED, comma-separated base URLs in preference order, and so is the
database (tests/live_env.py). There are no defaults: on this LAN HQB serves embed at 7875,
rerank at 7876 and chat at 7872, and HQD runs no model workloads, so the HQD embedder the
old default named (127.0.0.1:8103) no longer exists and the one-space test failed as
shipped (vxxxxxxxxxx F19, 2026-09-26). A run that has not named them is REFUSED.

SYNTHETIC TEXT ONLY. Every row this test writes is invented here; nothing read from the
mesh is sent anywhere. It runs the real DEFAULT space (embeddinggemma-300m/768/retrieval-v1
since divergence 026's amendment of 2026-09-26; raw-v1 before it) under its own test-only
name so it never touches a deployment's reference.

What it proves that the fakes cannot, in two tests:
  * `test_the_named_embedders_are_one_space`: every named embedder is admitted against a
    reference recorded by EVERY one of them. It needs two or more; with ONE named it is
    SKIPPED with a reason saying so, never run as one embedder compared with itself,
    which passes and proves nothing about two.
  * `test_the_live_models_answer_a_search`: the real reranker runs, the real chat model
    answers through the MUST-8 chokepoint, and a search over real vectors puts the relevant
    synthetic message first.
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest

pytestmark = pytest.mark.skipif(os.environ.get("MESHY_LIVE_MODELS") != "1",
                                reason="live LAN models: set MESHY_LIVE_MODELS=1")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

from django.test import override_settings  # noqa: E402

from apps.core import dbconn  # noqa: E402
from apps.meshsem import embedjob, providers, search, spaces  # noqa: E402
from tests import live_env  # noqa: E402
from tests.fixtures import meshsem_db as db  # noqa: E402


def _test_space(monkeypatch) -> spaces.Space:
    real = spaces.get(spaces.DEFAULT_SPACE)
    space = spaces.Space(**{**real.__dict__,
                            "name": f"{real.name}/livetest-{uuid.uuid4().hex[:8]}"})
    monkeypatch.setitem(spaces.SPACES, space.name, space)
    return space


def _embeds(space: spaces.Space) -> list[dict]:
    return [{"id": f"embed-{i}", "kind": "embed", "base_url": u, "space": space.name}
            for i, u in enumerate(live_env.urls("MESHY_LIVE_EMBED", "embedder"))]


def test_the_named_embedders_are_one_space(monkeypatch):
    space = _test_space(monkeypatch)
    embeds = _embeds(space)
    if len(embeds) < 2:
        pytest.skip(f"ONE-SPACE CHECK NOT RUN: MESHY_LIVE_EMBED names {len(embeds)} embedder "
                    f"({embeds[0]['base_url']}). That two embedders are one space needs two; "
                    "one compared with its own reference proves nothing about another. Name "
                    "a second embedder to run it.")
    # Each embedder admitted against a reference recorded by EACH named one.
    refs = {}
    for e in embeds:
        with override_settings(MESHY_MODEL_PROVIDERS=[e]):
            refs[e["id"]] = providers.embed_in_space(space, [], None,
                                                     record_if_unreferenced=True).canary_vectors
    for e in embeds:
        for ref_id, ref in refs.items():
            with override_settings(MESHY_MODEL_PROVIDERS=[e]):
                got = providers.embed_in_space(space, [space.query("probe")], ref)
            assert got.canary_min_cosine >= spaces.ADMISSION_COSINE, (e["id"], ref_id)
            print(f"{e['id']} vs reference from {ref_id}: {got.canary_min_cosine:.6f}")


def test_the_live_models_answer_a_search(monkeypatch):
    space = _test_space(monkeypatch)
    # Named, or REFUSED, before any connection.
    embeds = _embeds(space)
    reranks = [{"id": f"rerank-{i}", "kind": "rerank", "base_url": u}
               for i, u in enumerate(live_env.urls("MESHY_LIVE_RERANK", "reranker"))]
    chats = [{"id": f"chat-{i}", "kind": "chat", "base_url": u}
             for i, u in enumerate(live_env.urls("MESHY_LIVE_CHAT", "chat model"))]
    DSN = live_env.dsn()

    # End to end over synthetic rows: job, search, rerank, chat.
    with psycopg.connect(DSN, autocommit=True) as owner:
        db.ensure_namespace(owner)
        p = db.peer(owner, "live")
        tag = uuid.uuid4().hex[:8]
        rows = {
            "rls": db.row(owner, p, {"body": f"[{tag}] Row level security makes each "
                                             "tenant see only its own rows."},
                          embed_policy="allowed"),
            "uxxx": db.row(owner, p, {"body": f"[{tag}] The uxxx comes in twice a day."},
                           embed_policy="allowed"),
            "key": db.row(owner, p, {"body": f"[{tag}] A peer rotates its signing key with "
                                             "a co-signed handover."},
                          embed_policy="allowed"),
        }
        with override_settings(MESHY_MODEL_PROVIDERS=embeds + reranks + chats,
                               MESHY_SEMANTIC_SPACE=space.name):
            with dbconn.serving_connection(DSN) as conn:
                for _ in range(200):
                    r = embedjob.catch_up(conn)
                    assert r.stopped != "provider_unavailable", r.attempts
                    if not r.more or r.rows_processed == 0:
                        break
                search.forget()
                body = search.search(conn, space, "which tenant can read which rows", k=3,
                                     rerank=True)
            assert body["reranked"] is True, body["attempts"]
            assert body["method"] == "rrf(lexical,embed)+rerank", body["degraded"]
            assert body["providers"]["embed"] and body["providers"]["rerank"]
            ours = [h for h in body["hits"] if h["body"].startswith(f"[{tag}]")]
            assert ours and ours[0]["key"] == rows["rls"]["key"], body["hits"]

            reply = providers.chat([{"role": "user",
                                     "content": "Reply with the single word: ready"}])
            assert "ready" in reply.text.lower()
            print(f"search via {body['providers']}, chat via {reply.provider_id} "
                  f"({reply.reasoning_chars} reasoning chars)")
