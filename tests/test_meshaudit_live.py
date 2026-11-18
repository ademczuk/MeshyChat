"""ONE live test of the auditor against the real LAN chat model. Skipped unless
MESHY_LIVE_MODELS=1.

    MESHY_LIVE_MODELS=1 MESHY_TEST_DSN=postgresql://... MESHY_LIVE_CHAT=http://<hqb>:7872
        pytest tests/test_meshaudit_live.py -q -s

Chat endpoint(s): MESHY_LIVE_CHAT, comma-separated, REQUIRED (on this LAN HQB serves
gemma-4-26b-a4b at port 7872). The database: MESHY_TEST_DSN, REQUIRED. Neither has a
default, and the run is REFUSED without them (tests/live_env.py).

SYNTHETIC TEXT ONLY: every row here is invented by this test, so nothing read from the mesh
is sent anywhere.

What it proves that the fake cannot: the real reasoning model answers the real prompt
inside its context window (n_ctx 2816) and budget, with a reply the STRICT parser accepts
(it wraps its JSON in one ``` fence, which is allowed), and the sweep runs end to end
through the MUST-8 chokepoint as a dry run that writes nothing.
"""
from __future__ import annotations

import datetime as _dt
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

from apps.core import rls  # noqa: E402
from apps.mesh import projection as MP  # noqa: E402
from apps.meshaudit import derive as D  # noqa: E402
from apps.meshaudit import validate as V  # noqa: E402
from tests import live_env  # noqa: E402
from tests.fixtures import meshsem_db as db  # noqa: E402


def test_the_real_model_answers_strictly_and_selects_the_only_thread():
    # Named, or REFUSED, before any connection.
    chats = live_env.urls("MESHY_LIVE_CHAT", "chat model")
    DSN = live_env.dsn()
    with psycopg.connect(DSN, autocommit=True) as owner:
        db.ensure_namespace(owner)
        tag = uuid.uuid4().hex[:6]
        otter, wren = db.peer(owner, f"otter-{tag}"), db.peer(owner, f"wren-{tag}")
        base = (_dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)
                - _dt.timedelta(days=3650))
        thread = f"t-lease-renewal-{tag}"
        ask = db.row(owner, otter, {"to": wren["label"], "thread_id": thread,
                                    "body": "can someone take the lease renewal for the "
                                            "staging db? it expires friday"},
                     created_at=base - _dt.timedelta(minutes=20))
        db.row(owner, wren, {"to": otter["label"], "thread_id": thread,
                             "body": "I will look at it after lunch"},
               created_at=base - _dt.timedelta(minutes=10))
        subject = db.row(owner, wren, {"body": f"@{otter['label']} done, the staging lease "
                                               "is renewed until next month."},
                         created_at=base)
        MP.rebuild(owner)

        with override_settings(MESHY_MODEL_PROVIDERS=[
                {"id": f"chat-live-{i}", "kind": "chat", "base_url": u,
                 "model": "gemma-4-26b-a4b"} for i, u in enumerate(chats)]):
            with psycopg.connect(DSN, autocommit=True) as app:
                rls.set_role(app)
                report = D.derive(app, keys=[subject["key"]])

        print(f"\nlive: request {ask['key']}, accepted={report.accepted} "
              f"refused={report.refused} rows={[(r.key, r.accepted) for r in report.rows]}")
        assert report.rows_asked == 1 and not report.provider_failed, report.stopped
        assert V.ANSWER_NOT_STRICT_JSON not in report.refused, report.refused
        assert V.ANSWER_TRUNCATED not in report.refused, report.refused
        assert V.ANSWER_EMPTY not in report.refused, "thinking was not turned off"
        # The ONE thread candidate is the only quality claim asserted: which earlier
        # message a "done" answers (2026-09-23: the request `ask`) is the model's
        # judgment, printed above, not tested.
        assert V.Proposal("in_thread", thread) in report.rows[0].accepted, report.rows[0]
        with rls.server_internal(owner):
            with owner.cursor() as cur:
                cur.execute("SELECT count(*) FROM mesh_inferred_edges WHERE src_id = %s",
                            (subject["id"],))
                assert cur.fetchone()[0] == 0, "a dry run wrote"
