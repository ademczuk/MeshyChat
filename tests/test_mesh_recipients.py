"""GET /v1/mesh/recipients (divergence 083, issue #140): for each label a sender is about to write to, the peers it
reaches, each with its presence and its unread count. The send path WARNS from it and never refuses.

What is under test, each beside its control:
  * the unread count is the number that peer's own GET /v1/mesh/unread reports (one read clears one);
  * effective_state follows divergence 082: published asleep reads asleep; expired and never-beat read unknown,
    and never-beat has no beat age;
  * a label resolves exactly as labels_for answers (peers.name and the peer-<id8> fallback); an unknown label
    reaches no peer;
  * COUNTS ONLY: no message key, sender or body in the answer;
  * input bounds (1..10 labels) and the read gate (a peer without read on shared/mesh-chat is refused);
  * the call is counted under its own route name, which 039's CHECK admits.

Run:  pytest tests/test_mesh_recipients.py -q
Mutants (apps/mesh/views.py unless named; each seen to fail a test here):
  R1  unread counted for the CALLER, not the recipient          test_the_count_is_the_recipients_own_unread_total
  R2  the label resolves by peers.name only (fallback dropped)  test_the_fallback_label_reaches_its_peer
  R3  never-beat reads as the column default (presence.py)      test_presence_follows_082
  R4  the bound on labels is not applied                        test_input_bounds
  R5  the route spends a CALLS token                            test_the_route_spends_no_calls_token
  R6  the route's own per-minute bound is not applied           test_the_routes_own_bound_refuses_with_429
  R7  the bound's check and write are not one critical section   test_concurrent_admissions_never_exceed_the_bound
  R8  a full table is cleared (refused peers forgotten)          test_a_full_table_refuses_new_peers_and_forgets_no_one
  R9  `online` from the published state, or without the window  test_online_is_the_measured_socket_not_the_published_state
  U1  (graph.py) any message from the recipient resets it        test_unanswered_follows_the_scored_definition
  U2  (graph.py) the caller's cc sends count as sends            test_unanswered_follows_the_scored_definition
"""
from __future__ import annotations

import json
import uuid

import pytest

from apps.core import peer_limit
from apps.mesh import views

from tests.test_mesh_routes import NS, P, _auth, _peer, client, mesh_settings, owner  # noqa: F401
from tests.test_mesh_unread import _mail, _read, _target, _unread


def _recipients(client, caller, labels) -> dict:
    r = client.get("/v1/mesh/recipients?to=" + ",".join(labels), **_auth(caller["peer_id"]))
    assert r.status_code == 200, r.content[:400]
    return r.json()


def _only_peer(body, label) -> dict:
    (rec,) = [x for x in body["recipients"] if x["label"] == label]
    (peer,) = rec["peers"]
    return peer


def _name(owner, peer) -> str:
    return owner.execute("SELECT name FROM peers WHERE id = %s", (peer["peer_id"],)).fetchone()[0]


def test_the_count_is_the_recipients_own_unread_total(client, owner):
    """MUTANT R1. Three messages to R; R's own unread says 3 and so does the sender's check. One completed read
    by R makes both 2 (control: the numbers move together, so they are the same quantity)."""
    sender, rcpt = _peer(owner, "snd"), _peer(owner, "rcv")
    label = _name(owner, rcpt)
    target = _target(owner)
    keys = [_mail(owner, sender, to=[label]) for _ in range(3)]
    P.rebuild(owner)
    assert _unread(client, rcpt)["unread"]["total"] == 3
    peer = _only_peer(_recipients(client, sender, [label]), label)
    assert peer["peer_id"] == str(rcpt["peer_id"]) and peer["unread_count"] == 3
    assert isinstance(peer["unread_oldest_age_secs"], int)
    _read(owner, rcpt, target, keys[0])
    assert _unread(client, rcpt)["unread"]["total"] == 2
    assert _only_peer(_recipients(client, sender, [label]), label)["unread_count"] == 2


def test_presence_follows_082(client, owner):
    """MUTANT R3. Never beat: unknown, no beat age. Published asleep with a live expiry: asleep. Expired: unknown,
    with a beat age (the seat's own word ran out)."""
    sender, rcpt = _peer(owner, "snd"), _peer(owner, "rcv")
    label = _name(owner, rcpt)
    peer = _only_peer(_recipients(client, sender, [label]), label)
    assert peer["effective_state"] == "unknown" and peer["beat_age_secs"] is None    # no row at all
    from apps.calls import reads
    reads.touch_last_activity(owner, rcpt["peer_id"])     # the row any call makes: state 'active', no beat
    peer = _only_peer(_recipients(client, sender, [label]), label)
    assert peer["effective_state"] == "unknown" and peer["beat_age_secs"] is None, peer
    owner.execute("INSERT INTO mesh_presence (peer_id, state, note, last_beat, state_expires_at) "
                  "VALUES (%s, 'asleep', 'compacting', NOW(), NOW() + interval '10 minutes') "
                  "ON CONFLICT (peer_id) DO UPDATE SET state = EXCLUDED.state, note = EXCLUDED.note, "
                  "last_beat = EXCLUDED.last_beat, state_expires_at = EXCLUDED.state_expires_at",
                  (rcpt["peer_id"],))
    peer = _only_peer(_recipients(client, sender, [label]), label)
    assert peer["effective_state"] == "asleep" and peer["note"] == "compacting" and peer["beat_age_secs"] >= 0
    owner.execute("UPDATE mesh_presence SET state_expires_at = NOW() - interval '1 second' WHERE peer_id = %s",
                  (rcpt["peer_id"],))
    peer = _only_peer(_recipients(client, sender, [label]), label)
    assert peer["effective_state"] == "unknown" and isinstance(peer["beat_age_secs"], int)


def test_online_is_the_measured_socket_not_the_published_state(client, owner):
    """MUTANT R9. `online` is hub_presence's rule (a socket beaten within 30 s), independent of the published state:
    a seat published `active` without a ttl, with no socket, reads online False; a live socket reads True even with
    no presence row; a socket not beaten inside the window reads False again."""
    from apps.core import dbconn
    from apps.wsgw import hub_presence

    sender, rcpt = _peer(owner, "snd"), _peer(owner, "rcv")
    label = _name(owner, rcpt)
    owner.execute("INSERT INTO mesh_presence (peer_id, state, last_beat, state_expires_at) "
                  "VALUES (%s, 'active', NOW() - interval '75 days', NULL) "
                  "ON CONFLICT (peer_id) DO UPDATE SET state = 'active', last_beat = EXCLUDED.last_beat, "
                  "state_expires_at = NULL", (rcpt["peer_id"],))
    peer = _only_peer(_recipients(client, sender, [label]), label)
    assert peer["effective_state"] == "active" and peer["online"] is False, peer   # what it SAID vs what is there

    other = _peer(owner, "live")
    other_label = _name(owner, other)
    app = dbconn.connect()
    conn_id = uuid.uuid4()
    try:
        assert hub_presence.insert(app, conn_id, other["peer_id"], uuid.uuid4())
        assert hub_presence.is_online(app, other["peer_id"]), "control: the store itself reads it online"
        body = _recipients(client, sender, [label, other_label])
        assert _only_peer(body, other_label)["online"] is True
        assert _only_peer(body, other_label)["effective_state"] == "unknown"   # no presence row, still online
        assert _only_peer(body, label)["online"] is False
        # past the window: the same row, not beaten for 31 s, reads offline
        owner.execute("UPDATE hub_connections SET seen_at = now() - make_interval(secs => %s) "
                      "WHERE connection_id = %s", (hub_presence.ONLINE_WINDOW_SECS + 1, conn_id))
        assert _only_peer(_recipients(client, sender, [other_label]), other_label)["online"] is False
    finally:
        hub_presence.delete(app, conn_id)
        app.close()


def test_the_fallback_label_reaches_its_peer(client, owner):
    """MUTANT R2. peer-<id8> is a label labels_for always answers for, so it must reach that peer."""
    sender, rcpt = _peer(owner, "snd"), _peer(owner, "rcv")
    fallback = f"peer-{str(rcpt['peer_id'])[:8]}"
    peer = _only_peer(_recipients(client, sender, [fallback]), fallback)
    assert peer["peer_id"] == str(rcpt["peer_id"])


def test_an_unknown_label_reaches_no_peer(client, owner):
    sender = _peer(owner, "snd")
    body = _recipients(client, sender, ["nobody-" + uuid.uuid4().hex[:8]])
    assert body["recipients"][0]["peers"] == []


def test_the_answer_carries_no_keys_senders_or_text(client, owner):
    sender, rcpt = _peer(owner, "snd"), _peer(owner, "rcv")
    label = _name(owner, rcpt)
    key = _mail(owner, sender, to=[label])
    P.rebuild(owner)
    raw = json.dumps(_recipients(client, sender, [label]))
    assert key not in raw and "synthetic" not in raw and _name(owner, sender) not in raw


def test_input_bounds(client, owner):
    """MUTANT R4. 1..10 labels; none, eleven, or one longer than 200 characters is 422."""
    sender = _peer(owner, "snd")
    for query in ("", "?to=", "?to=" + ",".join(f"l{i}" for i in range(11)), "?to=" + "x" * 201):
        r = client.get("/v1/mesh/recipients" + query, **_auth(sender["peer_id"]))
        assert r.status_code == 422, (query[:40], r.status_code)
    assert client.get("/v1/mesh/recipients?to=" + ",".join(f"l{i}" for i in range(10)),
                      **_auth(sender["peer_id"])).status_code == 200


def test_the_read_gate_applies(client, owner):
    _peer(owner, "seed")
    outsider = _peer(owner, "nogrant", grant=False)
    r = client.get("/v1/mesh/recipients?to=anyone", **_auth(outsider["peer_id"]))
    assert r.status_code == 403, r.status_code
    assert client.get("/v1/mesh/recipients?to=anyone").status_code == 401


def test_the_call_is_counted_under_its_own_route(client, owner):
    sender = _peer(owner, "snd")
    before = owner.execute("SELECT COALESCE(sum(calls), 0) FROM mesh_route_usage WHERE route = 'recipients'").fetchone()[0]
    _recipients(client, sender, ["anyone"])
    after = owner.execute("SELECT COALESCE(sum(calls), 0) FROM mesh_route_usage WHERE route = 'recipients'").fetchone()[0]
    assert after == before + 1


@pytest.fixture(autouse=True)
def _fresh_route_bound():
    views._recipients_seen.clear()
    yield
    views._recipients_seen.clear()


def test_the_route_spends_no_calls_token(client, owner, monkeypatch):
    """MUTANT R5 (Codex on #145). The advisory check must not take the token the send after it needs: it
    refuses when the bucket is empty (check) but takes nothing (no admit). Control: inbox does admit."""
    taken, checked = [], []
    real_admit, real_check = peer_limit.admit, peer_limit.check
    monkeypatch.setattr(peer_limit, "admit", lambda pid, **kw: taken.append(str(pid)) or real_admit(pid, **kw))
    monkeypatch.setattr(peer_limit, "check", lambda pid, **kw: checked.append(str(pid)) or real_check(pid, **kw))
    sender = _peer(owner, "snd")
    _recipients(client, sender, ["anyone"])
    assert taken == [] and checked == [str(sender["peer_id"])]
    assert client.get("/v1/mesh/inbox", **_auth(sender["peer_id"])).status_code == 200
    assert taken == [str(sender["peer_id"])], "control: an ordinary mesh route does take a token"


def test_the_routes_own_bound_refuses_with_429(client, owner, monkeypatch):
    """MUTANT R6. Tokenless, so it bounds itself: past RECIPIENTS_PER_MINUTE in a minute, 429 + Retry-After."""
    monkeypatch.setattr(views, "RECIPIENTS_PER_MINUTE", 2)
    sender = _peer(owner, "snd")
    for _ in range(2):
        _recipients(client, sender, ["anyone"])
    r = client.get("/v1/mesh/recipients?to=anyone", **_auth(sender["peer_id"]))
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1, (r.status_code, r.content[:200])
    other = _peer(owner, "other")
    _recipients(client, other, ["anyone"])          # the bound is per peer


def test_concurrent_admissions_never_exceed_the_bound(monkeypatch):
    """MUTANT R7 (Codex on #145). Many threads admitting at once for one peer: exactly the bound gets in,
    the rest are refused. The shared dict's read yields after it returns, so without the lock every thread
    would read the same stale list and all would get in; with it, they are serialized."""
    import threading
    import time as _time

    class SlowDict(dict):
        def get(self, *a):
            value = super().get(*a)
            _time.sleep(0.003)                 # a read that yields before the check and write
            return value

    monkeypatch.setattr(views, "RECIPIENTS_PER_MINUTE", 30)
    monkeypatch.setattr(views, "_recipients_seen", SlowDict())
    peer = str(uuid.uuid4())
    for _ in range(29):
        views._recipients_admit(peer)
    admitted, refused = [], []
    barrier = threading.Barrier(16)

    def go():
        barrier.wait()
        try:
            views._recipients_admit(peer)
            admitted.append(1)
        except peer_limit.PeerRateLimited:
            refused.append(1)

    threads = [threading.Thread(target=go) for _ in range(16)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert len(admitted) == 1 and len(refused) == 15, (len(admitted), len(refused))
    assert len(views._recipients_seen[peer]) == 30



def test_a_full_table_refuses_new_peers_and_forgets_no_one(monkeypatch):
    """MUTANT R8 (Codex on #145). Three tracked, all active, one of them at its limit. A fourth peer is refused,
    and the limited peer is STILL refused afterwards: clearing the table would have let it straight back in."""
    monkeypatch.setattr(views, "RECIPIENTS_TRACKED_MAX", 3)
    monkeypatch.setattr(views, "RECIPIENTS_PER_MINUTE", 2)
    a, b, c, d = (str(uuid.uuid4()) for _ in range(4))
    views._recipients_admit(a)
    views._recipients_admit(a)
    with pytest.raises(peer_limit.PeerRateLimited):
        views._recipients_admit(a)                     # a is at its limit
    views._recipients_admit(b)
    views._recipients_admit(c)
    with pytest.raises(peer_limit.PeerRateLimited):
        views._recipients_admit(d)                     # full of active peers: the new one waits
    with pytest.raises(peer_limit.PeerRateLimited):
        views._recipients_admit(a)                     # and a is not forgotten
    assert d not in views._recipients_seen and len(views._recipients_seen) == 3


def test_unanswered_follows_the_scored_definition(client, owner):
    """Divergence 084 (#141), export.sql's `ub`: the caller's sends ADDRESSED (to) to the recipient since the
    recipient last wrote a message ADDRESSED (to) to the caller. MUTANTS U1 (any message from the recipient
    resets it: a reply to someone else, or one that only copies the caller, must not) and U2 (a cc send counts)."""
    def seat(t):
        # In production a message's author label is one of the author's own labels (roster, peers.name or
        # the fallback); the fixture stamps the TAG, so tag and name are made the same here.
        tag = f"{t}-{uuid.uuid4().hex[:6]}"
        return _peer(owner, tag, name=tag)

    a, b, c = seat("ua"), seat("ub"), seat("uc")
    la, lb, lc = a["name"], b["name"], c["name"]

    def count():
        P.rebuild(owner)
        return _only_peer(_recipients(client, a, [lb]), lb)["unanswered_from_you"]

    assert count() == 0
    _mail(owner, a, to=[lb], minutes_ago=50)
    _mail(owner, a, to=[lb], minutes_ago=40)
    _mail(owner, a, to=[lc], cc=[lb], minutes_ago=39)          # a copy to b is not a send to b (U2)
    assert count() == 2
    _mail(owner, b, to=[lc], minutes_ago=30)                   # b wrote, but not to a (U1)
    _mail(owner, b, to=[lc], cc=[la], minutes_ago=29)          # b copied a: not an answer (U1)
    assert count() == 2
    _mail(owner, b, to=[la], minutes_ago=20)                   # b answered a: the count resets
    assert count() == 0
    _mail(owner, a, to=[lb], minutes_ago=10)
    assert count() == 1
    assert _only_peer(_recipients(client, b, [la]), la)["unanswered_from_you"] == 0, "b owes nothing after its answer"
