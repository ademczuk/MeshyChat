"""A summary shows only what the requester can read, and the cache cannot widen it.

Divergence 037, design 2 (2.2, 2.4, 2.7, 2.8; critique C3). As 027's tests did: for each
gate, the summary's answer BESIDE THE REAL HANDLER'S for the same caller and key set, on
one install, each beside its control. The real handler's answer is the context route's
anchor `body_withheld` for a key of the thread: 027's gate, memory.read's invoke path.

  * a second owner's node with a namespace grant and no skill grant; a caller with no
    usable signing key; a bearer token with no proof under DPoP; a soft-deleted
    memory.read: each is 403 E_SUMMARY_WITHHELD naming the gate the anchor names;
  * a grant revoked DURING the model call gives the 403, not the summary (2.4);
  * a grant revoked while the request prepared gives the 403 (the gate is asked in the
    transaction that reads the rows);
  * two callers with the same covered set and different owed acks each get their own
    `facts`, the second from the cache (C3);
  * a single-flight waiter whose grant is revoked during the first caller's model call
    gets the 403, and the first caller still gets its summary (2.7);
  * a forbidden row with the gate closed reaches neither the prompt nor the tokenizer.

Run:  pytest tests/test_meshsum_authz.py -q
"""
from __future__ import annotations

import datetime as _dt
import os
import sys
import threading
import time
import uuid
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
os.environ.setdefault("MESHY_ALLOWED_HOSTS", "testserver,127.0.0.1,localhost")

import django  # noqa: E402
from django.conf import settings as dj_settings  # noqa: E402

if not dj_settings.configured:  # pragma: no cover
    django.setup()

from django.test import Client, override_settings  # noqa: E402

from apps.meshsum import admission, cache  # noqa: E402
from tests.fixtures import meshsem_db as db  # noqa: E402
from tests.fixtures.meshsum_world import SummaryWorld, bearer, project  # noqa: E402

THREAD = "/v1/mesh/summaries/thread"


@pytest.fixture()
def owner():
    with psycopg.connect(DSN, autocommit=True) as c:
        db.ensure_namespace(c)
        yield c


@pytest.fixture()
def world(owner, tmp_path, monkeypatch):
    w = SummaryWorld(DSN, tmp_path, owner, monkeypatch)
    yield w
    w.close()


def _summary(caller, t, **headers):
    return Client().get(THREAD, {"thread_id": t}, **{**bearer(caller["peer_id"]),
                                                     **headers})


def _anchor(caller, key, **headers):
    """The REAL handler's answer for the same caller: context's anchor gate (027)."""
    r = Client().get("/v1/mesh/context", {"key": key, "rerank": 0},
                     **{**bearer(caller["peer_id"]), **headers})
    assert r.status_code == 200, r.content[:200]
    return r.json()["anchor"]["body_withheld"]


def _withheld(r):
    if r.status_code == 200:
        return None
    assert r.status_code == 403 and r.json()["error"] == "E_SUMMARY_WITHHELD", r.content
    return r.json()["body_withheld"]


def _marker_absent(r, text):
    assert text.encode() not in r.content


# ================================================================ beside the handler

def test_a_second_owners_node_without_a_skill_grant(owner, world):
    t, rows = world.thread(2, bodies=["secret one zq1", "secret two zq2"])
    outsider = db.peer(owner, "outsider", key=True)
    db.grant(owner, outsider, "read")
    r = _summary(outsider, t)
    assert _withheld(r) == "not_authorized"
    _marker_absent(r, "secret one")
    db.skill_grant(owner, world.memory, outsider)
    assert _withheld(_summary(outsider, t)) is None, "control: with the skill grant"


def test_the_summary_and_the_handler_agree_for_every_gate(owner, world):
    t, rows = world.thread(2)
    key = rows[0]["key"]
    cases = []
    outsider = db.peer(owner, "outsider", key=True)
    db.grant(owner, outsider, "read")
    cases.append(outsider)
    keyless = db.peer(owner, "keyless", group=world.memory["group"])
    db.grant(owner, keyless, "read")
    cases.append(keyless)
    cases.append(world.reader("control"))
    seen = []
    for caller in cases:
        summary = _withheld(_summary(caller, t))
        handler = _anchor(caller, key)
        assert summary == handler, (caller["label"], summary, handler)
        seen.append(summary)
    assert seen == ["not_authorized", "no_usable_signing_key", None]


def test_with_dpop_enforced_the_summary_needs_a_proof_for_this_get(owner, world):
    from apps.authapi.didkey import to_did_key
    from tools.meshy_node import wire

    t, rows = world.thread(2)
    reader = world.reader("dpop")
    auth = bearer(reader["peer_id"], cnf=to_did_key(wire.public_key_of(reader["seed"])))
    token = auth["HTTP_AUTHORIZATION"][len("Bearer "):]

    def get(**headers):
        return Client().get(THREAD, {"thread_id": t}, **{**auth, **headers})

    with override_settings(MESHY_DPOP_ENFORCE=True):
        assert _withheld(get()) == "no_proof_of_possession"
        wrong = wire.dpop_proof(reader["seed"], "GET", "/v1/mesh/context", token)
        assert _withheld(get(HTTP_DPOP=wrong)) == "no_proof_of_possession"
        fresh = wire.dpop_proof(reader["seed"], "GET", THREAD, token)
        assert _withheld(get(HTTP_DPOP=fresh)) is None, "control: a proof for this GET"
        assert _withheld(get(HTTP_DPOP=fresh)) == "no_proof_of_possession", "spent"


def test_a_soft_deleted_memory_read_withholds(owner, world):
    own = db.memory_authority(owner)
    world.declare(memory_peer_id=own["peer_id"])
    reader = db.peer(owner, "own-reader", group=own["group"], key=True)
    db.grant(owner, reader, "read")
    t, _rows = world.thread(2)
    assert _withheld(_summary(reader, t)) is None, "control"
    with owner.cursor() as cur:
        cur.execute("UPDATE skills SET deleted_at = now() WHERE peer_id = %s "
                    "AND name = 'memory.read'", (own["peer_id"],))
    assert _withheld(_summary(reader, t)) == "skill_not_found"


# ============================================================ revoked mid-request

def _revoke(owner, p):
    with owner.cursor() as cur:
        cur.execute("UPDATE shared_memory_grant SET revoked = TRUE, revoked_at = now() "
                    "WHERE grantee_peer_id = %s", (p["peer_id"],))


def test_a_grant_revoked_during_the_model_call_gets_the_403(owner, world):
    t, _rows = world.thread(2, bodies=["during one zd1", "during two zd2"])
    reader = world.reader("mid-call")
    world.model.during_call = lambda payload: _revoke(owner, reader)
    r = _summary(reader, t)
    assert _withheld(r) == "namespace_permission"
    _marker_absent(r, "during one")
    assert world.model.chats() == 1, "the call was made; its answer was not shown"


def test_a_grant_revoked_while_the_request_prepared_gets_the_403(owner, world,
                                                                 monkeypatch):
    t, _rows = world.thread(2)
    reader = world.reader("mid-prep")
    real = admission.admit_request

    def revoke_then_admit(*a, **kw):
        _revoke(owner, reader)
        return real(*a, **kw)

    monkeypatch.setattr(admission, "admit_request", revoke_then_admit)
    assert _withheld(_summary(reader, t)) == "namespace_permission"
    assert world.model.chats() == 0 and world.model.tokenized == []


# ======================================================================== the cache

def test_two_callers_with_one_prompt_each_get_their_own_facts(owner, world):
    a, b = world.reader("owes-a"), world.reader("owes-b")
    name_a = world.name_of(a)
    otter = db.peer(owner, "otter")
    t = f"test/sum-{uuid.uuid4().hex[:10]}"
    ask = world.msg(otter, t, "please ack this", to=[name_a], requires_ack=True)
    world.msg(otter, t, "and this is context")
    project(owner)
    ra, rb = _summary(a, t), _summary(b, t)
    assert ra.status_code == rb.status_code == 200
    assert rb.json()["provenance"]["cache"] == "hit"
    assert [o["key"] for o in ra.json()["facts"]["owed_by_you"]] == [ask["key"]]
    assert rb.json()["facts"]["owed_by_you"] == [], "B was handed A's owed ack"
    assert ra.json()["summary"] == rb.json()["summary"]


def test_a_cache_entry_is_not_served_to_a_caller_the_gate_refuses(owner, world):
    t, _rows = world.thread(2, bodies=["cached one zc1", "cached two zc2"])
    assert _summary(world.reader("first"), t).status_code == 200
    outsider = db.peer(owner, "outsider", key=True)
    db.grant(owner, outsider, "read")
    chats = world.model.chats()
    r = _summary(outsider, t)
    assert _withheld(r) == "not_authorized" and world.model.chats() == chats
    _marker_absent(r, "cached one")
    _marker_absent(r, "otter wrote")


def test_a_new_version_of_a_covered_row_is_a_new_prompt(owner, world):
    otter = db.peer(owner, "otter")
    t = f"test/sum-{uuid.uuid4().hex[:10]}"
    first = world.msg(otter, t, "version one")
    world.msg(otter, t, "another")
    project(owner)
    reader = world.reader("versions")
    assert _summary(reader, t).json()["provenance"]["cache"] == "miss"
    with owner.cursor() as cur:
        cur.execute("UPDATE shared_memory SET is_head = FALSE WHERE id = %s",
                    (first["id"],))
    db.row(owner, otter, {"thread_id": t, "body": "version two"}, key=first["key"],
           version=2, parent_id=first["id"], embed_policy="allowed")
    project(owner)
    again = _summary(reader, t).json()
    assert again["provenance"]["cache"] == "miss"
    assert [c["version"] for c in again["covered"] if c["key"] == first["key"]] == [2]


def _changed_during_the_call(owner, world, change, *, first_policy="allowed"):
    """A two-message thread whose FIRST row `change` alters while the model generates."""
    otter, heron = db.peer(owner, "otter"), db.peer(owner, "heron")
    t = f"test/sum-{uuid.uuid4().hex[:10]}"
    first = world.msg(otter, t, "the note that changes", policy=first_policy)
    second = world.msg(heron, t, "an answer that stays")
    project(owner)
    world.model.during_call = lambda payload: change(first, otter, t)
    r = _summary(world.reader(f"changed-{uuid.uuid4().hex[:6]}"), t)
    assert r.status_code == 200, r.content[:300]
    body = r.json()
    assert [d["reason"] for d in body["summary"]["dropped"]] == ["source_changed"]
    assert body["summary"]["dropped"][0]["cites"] == [first["key"]]
    assert body["excluded"]["source_changed"] == 1
    assert [s["cites"] for s in body["summary"]["sentences"]] == [[second["key"]]]
    assert world.model.chats() == 1


def test_a_covered_row_superseded_during_the_model_call_is_source_changed(owner, world):
    """Design 2.4 at the route (adversarial review of 037: mutants of `_changed` survived,
    no route test reached `source_changed`). A sentence citing a row that stopped being
    the head while the model generated is dropped, counted, and its text not served."""
    def supersede(first, otter, t):
        with owner.cursor() as cur:
            cur.execute("UPDATE shared_memory SET is_head = FALSE WHERE id = %s",
                        (first["id"],))
        db.row(owner, otter, {"thread_id": t, "body": "version two"}, key=first["key"],
               version=2, parent_id=first["id"], embed_policy="allowed")

    _changed_during_the_call(owner, world, supersede)


def test_a_covered_row_the_gate_closes_on_during_the_model_call_is_source_changed(
        owner, world):
    """A row's embed_policy cannot change (shared_memory rows are immutable, sql/028), but
    the GATE can: a forbidden row covered while it was open is one the model may not be
    shown once the operator closes it during the call, so its sentence is source_changed."""
    world.declare(gate=True)
    _changed_during_the_call(owner, world, lambda *_: world.declare(gate=False),
                             first_policy=None)


def test_a_single_flight_waiter_revoked_during_the_leaders_call_gets_the_403(owner, world):
    t, _rows = world.thread(2, bodies=["flight one zf1", "flight two zf2"])
    leader, waiter = world.reader("leader"), world.reader("waiter")
    results: dict = {}

    def during(payload):
        # Hold the leader's call until the waiter is parked on its flight: a waiter that
        # arrived after the answer was cached would be a plain hit, a different path.
        assert _wait(lambda: cache.CACHE.waiting() >= 1, timeout=20)
        _revoke(owner, waiter)

    world.model.during_call = during

    def run(name, caller):
        results[name] = _summary(caller, t)

    lt = threading.Thread(target=run, args=("leader", leader))
    lt.start()
    assert _wait(lambda: world.model.chats() == 1)
    wt = threading.Thread(target=run, args=("waiter", waiter))
    wt.start()
    lt.join(30)
    wt.join(30)
    assert results["leader"].status_code == 200
    assert results["leader"].json()["provenance"]["cache"] == "miss"
    assert _withheld(results["waiter"]) == "namespace_permission"
    _marker_absent(results["waiter"], "otter wrote")
    assert world.model.chats() == 1, "the waiter made a call of its own"


def test_a_waiter_takes_only_the_answer_and_runs_its_own_recheck(owner, world):
    """Control for the test above: a waiter NOT revoked gets the leader's sentences with
    `cache: shared`, and no call of its own."""
    t, _rows = world.thread(2)
    leader, waiter = world.reader("leader2"), world.reader("waiter2")
    go = threading.Event()
    world.model.during_call = lambda payload: go.wait(10)
    results: dict = {}
    lt = threading.Thread(target=lambda: results.__setitem__("l", _summary(leader, t)))
    lt.start()
    assert _wait(lambda: world.model.chats() == 1)
    wt = threading.Thread(target=lambda: results.__setitem__("w", _summary(waiter, t)))
    wt.start()
    # Measured in the full suite under load: a fixed 0.5 s let the leader finish first,
    # and the waiter answered `hit`. Release the leader only once the waiter is parked.
    assert _wait(lambda: cache.CACHE.waiting() >= 1, timeout=20)
    go.set()
    lt.join(30)
    wt.join(30)
    assert results["w"].status_code == 200
    assert results["w"].json()["provenance"]["cache"] == "shared"
    assert results["w"].json()["summary"] == results["l"].json()["summary"]
    assert world.model.chats() == 1


def _wait(pred, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


# ==================================================================== the embed gate

def test_a_forbidden_row_never_reaches_the_prompt_or_the_tokenizer(owner, world):
    world.declare(gate=False)
    otter = db.peer(owner, "otter")
    t = f"test/sum-{uuid.uuid4().hex[:10]}"
    canary = f"zk{uuid.uuid4().hex[:10]}"
    world.msg(otter, t, f"forbidden {canary}", policy=None)
    world.msg(otter, t, "allowed text")
    project(owner)
    r = _summary(world.reader("gate"), t)
    assert r.status_code == 200 and r.json()["excluded"]["embed_forbidden"] == 1
    assert world.model.prompts and world.model.tokenized
    assert not any(canary in p for p in world.model.prompts)
    assert not any(canary in x for x in world.model.tokenized)
    _marker_absent(r, canary)
    world.declare(gate=True)
    from apps.meshsum import cache
    cache.CACHE.clear()
    r2 = _summary(world.reader("gate-open"), t)
    assert r2.json()["excluded"]["embed_forbidden"] == 0
    assert any(canary in p for p in world.model.prompts), "control: the gate open sends it"
