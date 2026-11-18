"""The persisted related_to edges against HQB's REAL reranker. Skipped unless
MESHY_LIVE_MODELS=1. Divergence 032.

    MESHY_LIVE_MODELS=1 MESHY_LIVE_RERANK=http://<hqb>:7876
        pytest tests/test_meshsem_related_live.py -q -s

MESHY_LIVE_RERANK is REQUIRED and names exactly ONE reranker, the one the committed
calibration is for (on this LAN, HQB's at port 7876). No default: the run is REFUSED
without it, and a list is refused rather than narrowed to its first entry
(tests/live_env.py).

SYNTHETIC TEXT ONLY: the canary sets are fixed sentences in apps/meshsem/relatedcal.py and
the pair below is invented here. Nothing read from the mesh is sent anywhere.

What it proves that the fakes cannot:
  * HQB's reranker still answers the committed canary sets within RERANK_CANARY_TOL of the
    reference logits the calibration was run with (it is the model the threshold is FOR);
  * under the committed threshold, one synthetic related pair crosses it in both
    directions' mean and one unrelated pair does not.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("MESHY_LIVE_MODELS") != "1",
                                reason="live LAN models: set MESHY_LIVE_MODELS=1")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

from django.test import override_settings  # noqa: E402

from tests import live_env  # noqa: E402

RELATED = ("Compaction lost its lease because the renewal shared a connection with the job "
           "body and timed out behind a slow vacuum.",
           "Moved the lease heartbeat to its own control connection so slow statements no "
           "longer starve the renewal.")
UNRELATED = ("The bakery doubled the croissant batch and the queue at the counter shrank.",
             "Rotated the gateway certificate and re-enabled the renewal timer.")


def test_hqb_matches_the_committed_canaries_and_the_threshold_separates_a_pair():
    RERANK = live_env.url("MESHY_LIVE_RERANK", "reranker")
    rc = importlib.import_module("apps.meshsem.relatedcal")
    related = importlib.import_module("apps.meshsem.related")
    providers = importlib.import_module("apps.meshsem.providers")
    doc = rc.read_document(rc.CALIBRATION_PATH)
    with override_settings(MESHY_MODEL_PROVIDERS=[
            {"id": "rerank-hqb", "kind": "rerank", "base_url": RERANK,
             "model": doc.get("rerank_model") or "bge-reranker-v2-m3"}]):
        admitted, attempts = related.admit_reranker(rc.canary_sets_of(doc),
                                                    tolerance=doc["canaries"]["tolerance"])
        assert admitted == "rerank-hqb", attempts
        print("canary max |delta|:", [a.get("max_delta") for a in attempts])

        def pair_score(a, b):
            fwd = providers.rerank(a, [b], provider_ids={"rerank-hqb"})
            rev = providers.rerank(b, [a], provider_ids={"rerank-hqb"})
            assert fwd.ran and rev.ran
            return (fwd.scores[0] + rev.scores[0]) / 2.0

        yes, no = pair_score(*RELATED), pair_score(*UNRELATED)
        print(f"related pair {yes:.3f}, unrelated pair {no:.3f}, "
              f"threshold {doc['threshold']}")
        assert yes >= doc["threshold"] > no
