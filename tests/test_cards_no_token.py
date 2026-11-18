"""GET /v1/cards takes no CALLS token and bounds itself (divergence 091, amended).

The line a client prints before a send reads this route beside the send. The first build charged the
read to the same per-peer CALLS bucket memory.write draws on, so a read begun before a send could take
the last token and the send was refused (pxx's Codex review of the client, msg/<key>).
The recipient check met the same defect on #145; this is its remedy, on one shared class.

Run:  .venv/Scripts/python.exe -m pytest tests/test_cards_no_token.py -q -p no:cacheprovider
"""
from __future__ import annotations

import sys
import threading
import time as _time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.test_cards_routes import (  # noqa: E402,F401  (fixtures)
    _bearer, _card, _list, client, deck, graph, owner, world)

from apps.cards import views as card_views  # noqa: E402
from apps.core import peer_limit  # noqa: E402
from apps.core.minute_bound import MinuteBound  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_bound():
    card_views._BOUND.forget()
    yield
    card_views._BOUND.forget()


def test_the_card_list_spends_no_calls_token_and_search_does(client, owner, deck, monkeypatch):
    """The list refuses when the bucket is empty (check) and takes nothing (no admit). Control: the
    search over the same namespace is not advisory, embeds and reranks, and does take a token."""
    ns, holder, reader = deck
    taken, checked = [], []
    real_admit, real_check = peer_limit.admit, peer_limit.check
    monkeypatch.setattr(peer_limit, "admit", lambda pid, **kw: taken.append(str(pid)) or real_admit(pid, **kw))
    monkeypatch.setattr(peer_limit, "check", lambda pid, **kw: checked.append(str(pid)) or real_check(pid, **kw))
    assert _list(client, reader).status_code == 200
    assert taken == [] and checked == [str(reader["peer_id"])]
    r = client.get(f"/v1/mesh/search?q=anything&ns={ns}&rerank=0", **_bearer(reader["peer_id"]))
    assert r.status_code in (200, 503)
    assert taken == [str(reader["peer_id"])], "control: the search route does take a token"


def test_an_empty_bucket_still_refuses_the_list(client, owner, deck, monkeypatch):
    """Tokenless is not unmetered against the bucket: a peer already out of calls is refused here too."""
    ns, holder, reader = deck

    def empty(pid, **kw):
        raise peer_limit.PeerRateLimited(str(pid), "mesh", peer_limit.CALLS, 7)
    monkeypatch.setattr(peer_limit, "check", empty)
    r = _list(client, reader)
    assert r.status_code == 429 and r.headers.get("Retry-After") == "7"


def test_the_lists_own_bound_refuses_with_429(client, owner, deck, monkeypatch):
    ns, holder, reader = deck
    monkeypatch.setattr(card_views._BOUND, "per_minute", 2)
    assert _list(client, reader).status_code == 200
    assert _list(client, reader).status_code == 200
    third = _list(client, reader)
    assert third.status_code == 429 and int(third.headers["Retry-After"]) >= 1


def test_the_bound_is_per_peer_and_admits_exactly_its_limit_under_concurrency():
    """As the recipient check's (Codex on #145): the read, the check and the write are one critical
    section. Sixteen threads race for the one place left; exactly one gets it."""

    class SlowDict(dict):
        def get(self, *a):
            value = super().get(*a)
            _time.sleep(0.003)                 # a read that yields before the check and write
            return value

    bound = MinuteBound("test", 30)
    bound._seen = SlowDict()
    peer, other = str(uuid.uuid4()), str(uuid.uuid4())
    for _ in range(29):
        bound.admit(peer)
    admitted, refused = [], []
    barrier = threading.Barrier(16)

    def go():
        barrier.wait()
        try:
            bound.admit(peer)
            admitted.append(1)
        except peer_limit.PeerRateLimited:
            refused.append(1)

    threads = [threading.Thread(target=go) for _ in range(16)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert len(admitted) == 1 and len(refused) == 15, (len(admitted), len(refused))
    assert len(bound._seen[peer]) == 30
    bound.admit(other)                                   # control: another peer is not held by the first one's limit


def test_a_full_table_refuses_new_peers_and_forgets_no_one():
    """Three tracked, all active, one at its limit. A fourth peer is refused, and the limited peer is
    STILL refused afterwards: clearing the table would have let it straight back in."""
    bound = MinuteBound("test", 2, tracked_max=3)
    a, b, c, d = (str(uuid.uuid4()) for _ in range(4))
    bound.admit(a)
    bound.admit(a)
    with pytest.raises(peer_limit.PeerRateLimited):
        bound.admit(a)
    bound.admit(b)
    bound.admit(c)
    with pytest.raises(peer_limit.PeerRateLimited):
        bound.admit(d)
    with pytest.raises(peer_limit.PeerRateLimited):
        bound.admit(a)
    assert set(bound._seen) == {a, b, c}


def test_a_peer_with_no_grant_is_bounded_too(client, owner, deck, monkeypatch):
    """The bound is counted BEFORE the read check (pxx, reading #179): a peer with no grant on the
    namespace gets its 403 twice and then the 429, and takes no token for any of them. Control: a
    granted peer's own count is separate, it still reads."""
    from tests.test_cards_routes import _member
    ns, holder, reader = deck
    stranger = _member(owner, "nogrant")
    taken = []
    real_admit = peer_limit.admit
    monkeypatch.setattr(peer_limit, "admit", lambda pid, **kw: taken.append(str(pid)) or real_admit(pid, **kw))
    monkeypatch.setattr(card_views._BOUND, "per_minute", 2)
    assert [_list(client, stranger).status_code for _ in range(3)] == [403, 403, 429]
    assert taken == []
    assert _list(client, reader).status_code == 200


def test_a_call_is_forgotten_after_a_minute(monkeypatch):
    """The clock moved by hand (memory-bxxxxx on #179: nothing moved it, so a bound that never
    forgot passed). Two a minute: the third is refused at 59 s and admitted at 61 s."""
    from apps.core import minute_bound
    now = [1000.0]
    monkeypatch.setattr(minute_bound.time, "monotonic", lambda: now[0])
    bound = MinuteBound("test", 2)
    peer = str(uuid.uuid4())
    bound.admit(peer)
    bound.admit(peer)
    now[0] = 1059.0
    with pytest.raises(peer_limit.PeerRateLimited) as refused:
        bound.admit(peer)
    assert refused.value.retry_after == 2
    now[0] = 1061.0
    bound.admit(peer)
    bound.admit(peer)
    with pytest.raises(peer_limit.PeerRateLimited):
        bound.admit(peer)


def test_the_number_is_120_a_minute():
    """Pinned: divergence 091 and the client's reading of the route both name it."""
    assert card_views.CARDS_PER_MINUTE == 120 and card_views._BOUND.per_minute == 120
