"""Audit finding 9a (a reviewer 55836e, design confirmed 0ae861): GET /v1/mesh/threads/<id> pages with ?after= and
carries message content with ?envelope=1. Round 2 after pxx e7748b.

Before: a 512-message thread was cut at the route's limit with no way on, and a reader had to make one signed read per
key (or re-read from row 1) to follow a thread. Now:
  * a page cut by ?limit names `next_after`, an exact POSITION (pos:<micros>:<src_id>), and walking it returns every
    row once, in order. An edit between pages cannot move it (P2-2): an edit adds a version at a later position;
  * ?after=<msg key> also works and starts at that key's EARLIEST place, so it can repeat rows, never skip them;
    anything else is a 422, never an empty page that would read as "nothing new";
  * ?after with include_inferred=1 is a 422 (P2-3): inferred rows are not in the declared order;
  * ?envelope=1 attaches each message the caller may read, gated as memory.read is (P2-1: shown with a reader a signed
    read would admit, a signing key and the memory.read skill grant, beside a control without the grant);
  * with DPoP enforced and no proof, no envelopes and `envelope_withheld` "no_proof";
  * an envelope is not a read: the caller's unread answer is unchanged.
Each test has its own thread (pxx e7748b: a shared thread made two tests depend on order).

Run: python -m pytest tests/test_mesh_thread_paging.py -q -p no:cacheprovider
"""
from __future__ import annotations

import datetime as _dt
import uuid

import pytest
from django.test import override_settings

from tests.test_mesh_graphread import (NS, P, _auth, _grant, _mesh_claimant, _peer, _row, client,  # noqa: F401
                                       mesh_settings, no_roster, owner)


@pytest.fixture()
def memory_authority(owner):
    """The memory authority the preview gate checks skill grants against (as tests/test_mesh_wait_addressed.py)."""
    from tests.fixtures import meshsem_db as db
    mem = db.memory_authority(owner)
    with override_settings(MESHY_MEMORY_PEER_ID=str(mem["peer_id"])):
        yield mem


def _thread():
    return "t-paging-%s" % uuid.uuid4().hex[:10]


def _seed(owner, n, thread):
    claimant = _mesh_claimant(owner)
    me = _peer(owner, "pg")
    _grant(owner, NS, me, claimant)
    base = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=10)
    keys = []
    for i in range(n):
        keys.append(_row(owner, claimant | {"label": "author"},
                         {"to": me["name"], "thread_id": thread, "type": "status", "body": "message %d" % i,
                          "from": "author"},
                         created_at=base + _dt.timedelta(seconds=i))["key"])
    P.rebuild(owner)
    return me, keys, claimant


def _get(client, me, thread, **params):
    q = "&".join("%s=%s" % kv for kv in params.items())
    return client.get("/v1/mesh/threads/%s%s" % (thread, ("?" + q) if q else ""), **_auth(me["peer_id"]))


def _walk(client, me, thread, limit):
    seen, after, pages = [], None, 0
    while True:
        params = {"limit": limit}
        if after:
            params["after"] = after
        body = _get(client, me, thread, **params).json()
        seen += [h["key"] for h in body["hits"]]
        pages += 1
        if not body["truncated"]:
            assert "next_after" not in body
            return seen, pages
        after = body["next_after"]
        assert after.startswith("pos:")
        assert pages < 50


def test_after_a_key_returns_only_what_follows_it(client, owner):
    """RED before the fix: `after` was ignored, so the whole thread came back."""
    t = _thread()
    me, keys, _ = _seed(owner, 5, t)
    assert [h["key"] for h in _get(client, me, t, after=keys[1]).json()["hits"]] == keys[2:]
    assert [h["key"] for h in _get(client, me, t).json()["hits"]] == keys            # control: no after, all of it


def test_a_cut_page_names_next_after_and_the_walk_returns_every_row_once(client, owner):
    t = _thread()
    me, keys, _ = _seed(owner, 7, t)
    seen, pages = _walk(client, me, t, 3)
    assert seen == keys and pages == 3


@pytest.fixture()
def write_conn():
    """The role memory.write and memory.edit run as: meshychat_app under FORCE RLS."""
    from apps.memoryskills import db as memdb
    from tests.test_mesh_graphread import DSN
    conn = memdb.connect(DSN)
    try:
        yield conn
    finally:
        conn.close()


def test_an_edit_between_pages_skips_nothing(client, owner, write_conn, memory_authority):
    """pxx e7748b P2-2: the last message of page 1 is edited before page 2 is asked for. The edit's new version sits
    at a later position; a cursor that followed the key would skip every row in between. The position cannot.
    Written and edited the supported way: memory.write, then memory.edit (divergence 059's single writer)."""
    import time

    from apps.memoryskills import handlers
    from tests.test_mesh_authorship import _admit, _envelope, _key, _peer as _author_peer

    t = _thread()
    author = _author_peer(owner, "pgw")
    _admit(owner, NS, author)
    me = _peer(owner, "pgr")
    _grant(owner, NS, me, _mesh_claimant(owner))

    def value(body):
        return {"schema_version": 1, "to": [me["name"]], "from": author["name"], "type": "status",
                "thread_id": t, "ts": "2026-11-08T18:00:00Z", "body": body, "author_family": "claude"}

    keys = []
    for i in range(6):
        k = _key(author["name"])
        handlers.dispatch("memory.write", _envelope(author, "memory.write",
                                                    {"namespace": NS, "key": k, "value": value("message %d" % i)}),
                          author["peer_id"], write_conn)
        keys.append(k)
        time.sleep(0.01)                                # distinct created_at, so the order is the write order
    P.rebuild(owner)
    first = _get(client, me, t, limit=2).json()
    assert [h["key"] for h in first["hits"]] == keys[:2], first
    handlers.dispatch("memory.edit", _envelope(author, "memory.edit",
                                               {"namespace": NS, "key": keys[1], "expected_head_version": 1,
                                                "value": value("message 1, edited")}),
                      author["peer_id"], write_conn)
    P.rebuild(owner)
    rest = _get(client, me, t, after=first["next_after"]).json()
    got = [h["key"] for h in rest["hits"]]
    assert got[:4] == keys[2:], got                     # nothing between the old and the new place is skipped
    assert keys[1] in got[4:], got                      # the edited message shows again, at its new place
    # pxx bd1374 P2-2: the envelope says which message was edited (the edit record is outside the value)
    from tests.fixtures import meshsem_db as db
    me.update(db.add_key(owner, me["peer_id"]))
    db.skill_grant(owner, memory_authority, me)
    envs = {h["key"]: h["envelope"] for h in _get(client, me, t, envelope=1).json()["hits"]}
    assert envs[keys[1]]["edited"] is True and envs[keys[1]]["body"] == "message 1, edited"
    assert envs[keys[1]]["version"] == 2
    assert all(envs[k]["edited"] is False for k in keys if k != keys[1])


def test_after_must_name_this_thread_and_not_mix_with_inferred(client, owner):
    t = _thread()
    me, keys, claimant = _seed(owner, 2, t)
    other = _row(owner, claimant | {"label": "author"}, {"to": me["name"], "thread_id": _thread(), "body": "x"})["key"]
    P.rebuild(owner)
    assert _get(client, me, t, after=other).status_code == 422
    assert _get(client, me, t, after="not-a-key").status_code == 422
    assert _get(client, me, t, after="pos:12:not-a-uuid").status_code == 422
    # GitHub Codex at graph.py:145: a number datetime cannot hold is a 422, never a 500
    assert _get(client, me, t, after="pos:99999999999999999999:0e8c2f3a-1111-4222-8333-444455556666").status_code == 422
    far = _get(client, me, t, after="pos:9999999999999999:0e8c2f3a-1111-4222-8333-444455556666")
    assert far.status_code == 200 and far.json()["hits"] == []      # 16 digits: a real (year 2286) position, past it
    assert _get(client, me, t, after=keys[0], include_inferred=1).status_code == 422
    assert _get(client, me, t, after=keys[0]).status_code == 200                      # control


def test_envelopes_go_to_a_reader_memory_read_admits_and_are_not_a_read(client, owner, memory_authority):
    """pxx e7748b P2-1: the gate is memory.read's own, so the envelope goes to a caller a signed read would admit (a
    signing key and the memory.read skill grant). CONTROL, one variable apart: the same caller before the skill grant
    gets the plain hits, no envelope, and the reason. RED before the fix: no envelope field at all."""
    from tests.fixtures import meshsem_db as db
    t = _thread()
    me, keys, _ = _seed(owner, 3, t)
    me.update(db.add_key(owner, me["peer_id"]))
    refused = _get(client, me, t, envelope=1).json()
    assert all("envelope" not in h for h in refused["hits"]) and refused["envelope_withheld"]
    db.skill_grant(owner, memory_authority, me)
    before = client.get("/v1/mesh/unread", **_auth(me["peer_id"]))
    body = _get(client, me, t, envelope=1).json()
    envs = [h.get("envelope") for h in body["hits"]]
    assert [e["body"] for e in envs] == ["message 0", "message 1", "message 2"], body
    assert all(e["thread_id"] == t and e["to"] == me["name"] and "version" in e for e in envs)
    assert body["envelope_withheld"] is None
    after = client.get("/v1/mesh/unread", **_auth(me["peer_id"]))
    assert before.status_code == after.status_code == 200, (before.content, after.content)
    assert before.json()["unread"] == after.json()["unread"]                 # the envelope read marked nothing
    assert "envelope" not in _get(client, me, t).json()["hits"][0]          # control: not asked, not sent


def test_with_dpop_enforced_and_no_proof_there_are_no_envelopes(client, owner, memory_authority):
    from tests.fixtures import meshsem_db as db
    t = _thread()
    me, _keys, _ = _seed(owner, 2, t)
    me.update(db.add_key(owner, me["peer_id"]))
    db.skill_grant(owner, memory_authority, me)
    with override_settings(MESHY_DPOP_ENFORCE=True):
        body = _get(client, me, t, envelope=1).json()
    assert body["envelope_withheld"] == "no_proof"
    assert all("envelope" not in h for h in body["hits"]) and len(body["hits"]) == 2
