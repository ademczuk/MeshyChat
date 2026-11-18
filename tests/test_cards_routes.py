"""Divergence 091: reading decision cards. GET /v1/cards (the filter and the derived status) and the
card filter on GET /v1/mesh/search?ns=<card namespace>, through the REAL urlconf.

The fixtures are tests/test_meshsem_graphsearch.py's (a real peer and bearer, the deterministic fake
model hosts, a claimed graph namespace the walker derives). Cards are inserted as signed rows, as
memory.write would have stored them; the write rules have their own file (tests/test_cards_write.py).

Run:  .venv/Scripts/python.exe -m pytest tests/test_cards_routes.py -q -p no:cacheprovider
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.test_meshsem_graphsearch import (  # noqa: E402,F401  (fixtures)
    _bearer, _claim, _grant, _member, _row, _walk, client, graph, owner, world)

from django.test import override_settings  # noqa: E402

from apps.cards import store, validation  # noqa: E402
from apps.memoryskills.validation import MESH_CHAT_NAMESPACE  # noqa: E402
from tools.meshy_node import cards as client_cards  # noqa: E402
from tools.meshy_node import node as client_node  # noqa: E402


@pytest.fixture()
def deck(owner, world, graph):
    """The graph namespace, named as the card namespace, with a reader that holds its read grant."""
    ns, holder = graph
    reader = _member(owner, "cardreader")
    _grant(owner, ns, holder, reader)
    with override_settings(MESHY_CARDS_NAMESPACE=ns):
        yield ns, holder, reader


def _card(owner, holder, ns, text, *, time, card_type="Ruling", seat="kxxx", supersedes=None, own="operator"):
    card_id = uuid.uuid4().hex
    key = f"card/{seat}/{card_id}"
    value = {"schema": "card.v1", "id": card_id, "type": card_type, "owner": own, "seat": seat, "time": time,
             "text": text, "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
             "source": {"record_uuid": str(uuid.uuid4()), "block_index": 0, "kind": "answered_question",
                        "first_hand": True}}
    if supersedes is not None:
        value["supersedes"] = supersedes
    _row(owner, holder, ns, key, value)
    return key


def _list(client, reader, **params):
    query = "&".join(f"{k}={v}" for k, vs in params.items() for v in (vs if isinstance(vs, list) else [vs]))
    return client.get(f"/v1/cards?{query}", **_bearer(reader["peer_id"]))


def _keys(response) -> list[str]:
    assert response.status_code == 200, response.content
    return [c["key"] for c in response.json()["cards"]]


def _tombstone(owner, key):
    """A tombstone that kept NONE of a card's fields: what a redaction written before divergence 092 would hold. The
    real memory.redact, which keeps the card's pointers, is exercised in tests/test_cards_redaction.py."""
    # The immutability trigger only, never USER: the card head record's trigger (sql_meshychat/057) must
    # still see this write, as it sees every real one, or the record keeps the old value.
    owner.execute("ALTER TABLE shared_memory DISABLE TRIGGER shared_memory_immutability")
    try:
        owner.execute("UPDATE shared_memory SET value = %s::jsonb WHERE key = %s AND is_head",
                      ('{"redacted": true, "reason_code": "test"}', key))
    finally:
        owner.execute("ALTER TABLE shared_memory ENABLE TRIGGER shared_memory_immutability")


# --------------------------------------------------------------------------- the list

def test_the_list_is_newest_first_and_carries_each_cards_fields(client, owner, deck):
    ns, holder, reader = deck
    old = _card(owner, holder, ns, "Ruling | hold the deploy", time="2026-10-08T07:32:13.000Z")
    new = _card(owner, holder, ns, "Ruling | deploy now", time="2026-10-08T07:37:17.834Z")
    r = _list(client, reader)
    assert _keys(r) == [new, old]
    body = r.json()
    assert body["namespace"] == ns and body["next"] is None
    top = body["cards"][0]
    assert top["text"] == "Ruling | deploy now" and top["version"] == 1
    assert top["status"] == "active" and top["superseded_by"] == []


def test_a_peer_without_read_on_the_card_namespace_is_refused(client, owner, deck):
    ns, holder, reader = deck
    _card(owner, holder, ns, "Ruling | private to readers", time="2026-10-08T07:00:00.000Z")
    stranger = _member(owner, "cardstranger")
    assert _list(client, stranger).status_code == 403
    assert _list(client, reader).status_code == 200                          # control


def test_the_filters_select_by_type_seat_owner_and_time(client, owner, deck):
    ns, holder, reader = deck
    ruling = _card(owner, holder, ns, "Ruling | r", time="2026-10-08T07:00:00.000Z")
    cand = _card(owner, holder, ns, "Candidate | c", time="2026-10-08T08:00:00.000Z", card_type="Candidate")
    other_seat = _card(owner, holder, ns, "Ruling | txxxx's", time="2026-10-08T09:00:00.000Z", seat="txxxx")
    seat_owned = _card(owner, holder, ns, "Decision | a seat's", time="2026-10-08T10:00:00.000Z",
                       card_type="Decision", own="kxxx")
    assert _keys(_list(client, reader)) == [seat_owned, other_seat, cand, ruling]          # control: no filter
    assert _keys(_list(client, reader, type="Ruling")) == [other_seat, ruling]
    assert _keys(_list(client, reader, type=["Ruling", "Candidate"])) == [other_seat, cand, ruling]
    assert _keys(_list(client, reader, seat="txxxx")) == [other_seat]
    assert _keys(_list(client, reader, owner="kxxx")) == [seat_owned]
    assert _keys(_list(client, reader, since="2026-10-08T08:00:00.000Z")) == [seat_owned, other_seat, cand]
    assert _keys(_list(client, reader, until="2026-10-08T08:00:00.000Z")) == [ruling]      # until is exclusive
    for bad in ({"type": "Opinion"}, {"status": "gone"}, {"limit": "0"}, {"limit": "201"}, {"since": "2026-10-08"},
                {"after": "not-a-cursor"},
                # the shape of an instant, and not one: month 13, hour 25. A typed 422, never the database's error
                {"since": "2026-13-08T07:00:00.000Z"}, {"until": "2026-10-08T25:00:00.000Z"},
                {"recorded_since": "2026-13-08T07:00:00.000Z"}):
        assert _list(client, reader, **bad).status_code == 422, bad


def test_paging_by_cursor_returns_every_card_once_when_times_are_equal(client, owner, deck):
    """Seven cards, five of them at ONE instant: the cursor is (time, key), so a page boundary inside
    the tie neither repeats nor skips. A cursor of the time alone would lose the rest of the tie."""
    ns, holder, reader = deck
    keys = [_card(owner, holder, ns, f"Ruling | tie {i}", time="2026-10-08T07:00:00.000Z") for i in range(5)]
    keys.append(_card(owner, holder, ns, "Ruling | later", time="2026-10-08T08:00:00.000Z"))
    keys.append(_card(owner, holder, ns, "Ruling | earlier", time="2026-10-08T06:00:00.000Z"))
    seen, after, pages = [], None, 0
    while True:
        params = {"limit": "2"} if after is None else {"limit": "2", "after": after}
        r = _list(client, reader, **params)
        seen.extend(_keys(r))
        after = r.json()["next"]
        pages += 1
        if after is None:
            break
    assert pages == 4 and len(seen) == 7 and sorted(seen) == sorted(keys)
    assert seen == _keys(_list(client, reader, limit="200")), "the pages are the one list, in its order"


# --------------------------------------------------------------------------- derived status

def test_superseded_is_derived_from_the_head_cards_of_its_own_seat_that_name_a_key(client, owner, deck):
    ns, holder, reader = deck
    first = _card(owner, holder, ns, "Ruling | deploy now, clean later", time="2026-10-08T07:35:56.780Z")
    final = _card(owner, holder, ns, "Ruling | deploy ea0a130 now", time="2026-10-08T07:37:17.834Z",
                  supersedes=[first])
    assert _keys(_list(client, reader)) == [final]                                          # active is the default
    assert _keys(_list(client, reader, status="superseded")) == [first]
    everything = _list(client, reader, status="any")
    assert _keys(everything) == [final, first]
    by_key = {c["key"]: c for c in everything.json()["cards"]}
    assert by_key[first]["status"] == "superseded" and by_key[first]["superseded_by"] == [final]
    assert by_key[first]["overruled_by"] == []
    assert by_key[final]["status"] == "active" and by_key[final]["superseded_by"] == []
    assert store.superseders(owner, ns) == {first: [final]}


def test_another_seats_card_overrules_and_never_hides(client, owner, deck):
    """Whether a card a recorded decision is the writing seat's own word. So a later Ruling of seat B that names
    seat A's Ruling does NOT take it out of any seat's default read: A's card stays active and says
    `overruled_by`. Only A's own later card hides it."""
    ns, holder, reader = deck
    hold = _card(owner, holder, ns, "Ruling | hold for the fix", time="2026-10-08T07:32:13.000Z", seat="pxx")
    final = _card(owner, holder, ns, "Ruling | deploy ea0a130 now", time="2026-10-08T07:37:17.834Z", seat="kxxx",
                  supersedes=[hold])
    active = _list(client, reader)
    assert _keys(active) == [final, hold], "both are shown: nobody hid pxx's ruling"
    by_key = {c["key"]: c for c in active.json()["cards"]}
    assert by_key[hold]["status"] == "active" and by_key[hold]["overruled_by"] == [final]
    assert by_key[hold]["superseded_by"] == []
    assert _keys(_list(client, reader, status="superseded")) == []
    assert store.superseders(owner, ns) == {}
    agreed = _card(owner, holder, ns, "Ruling | superseded here too", time="2026-10-08T08:00:00.000Z", seat="pxx",
                   supersedes=[hold])                                                       # control: its OWN seat
    assert _keys(_list(client, reader)) == [agreed, final]
    assert _keys(_list(client, reader, status="superseded")) == [hold]
    assert store.superseders(owner, ns) == {hold: [agreed]}


def test_there_is_no_cascade_and_a_redacted_superseder_stops_superseding(client, owner, deck):
    """A named by B named by C: A and B are superseded, C is active. Then C is redacted: its tombstone
    has no text and no `supersedes`, so B reads as active again, and A is still superseded by B."""
    ns, holder, reader = deck
    a = _card(owner, holder, ns, "Ruling | a", time="2026-10-08T07:00:00.000Z")
    b = _card(owner, holder, ns, "Ruling | b", time="2026-10-08T08:00:00.000Z", supersedes=[a])
    c = _card(owner, holder, ns, "Ruling | c", time="2026-10-08T09:00:00.000Z", supersedes=[b])
    assert _keys(_list(client, reader)) == [c]
    assert _keys(_list(client, reader, status="superseded")) == [b, a]
    _tombstone(owner, c)
    assert _keys(_list(client, reader)) == [b], "the tombstone is not a card, and no longer supersedes b"
    assert _keys(_list(client, reader, status="any")) == [b, a]
    assert store.superseders(owner, ns) == {a: [b]}
    _tombstone(owner, a)
    assert store.superseders(owner, ns) == {}, "a redacted TARGET is not a card either: nothing is superseded"


# --------------------------------------------------------------------------- search over cards

def _search(client, reader, q, ns, **params):
    extra = "".join(f"&{k}={v}" for k, vs in params.items() for v in (vs if isinstance(vs, list) else [vs]))
    return client.get(f"/v1/mesh/search?q={q}&ns={ns}&rerank=0{extra}", **_bearer(reader["peer_id"]))


def _hits(response) -> list[str]:
    assert response.status_code == 200, response.content
    return [h["key"] for h in response.json()["hits"]]


def test_the_search_filter_drops_cards_before_they_take_a_place_in_the_answer(client, owner, deck):
    """One word, four cards. Unfiltered, all four are hits. With type=Ruling the Candidate is not a
    hit; with the default status the superseded Ruling is not either. The filter is echoed, so a
    caller can tell a filtered answer from an unfiltered one."""
    ns, holder, reader = deck
    word = "zqxcard" + uuid.uuid4().hex[:6]
    old = _card(owner, holder, ns, f"Ruling | {word} hold", time="2026-10-08T07:00:00.000Z")
    new = _card(owner, holder, ns, f"Ruling | {word} deploy", time="2026-10-08T08:00:00.000Z", supersedes=[old])
    cand = _card(owner, holder, ns, f"Candidate | {word} maybe", time="2026-10-08T09:00:00.000Z", card_type="Candidate")
    theirs = _card(owner, holder, ns, f"Ruling | {word} elsewhere", time="2026-10-08T10:00:00.000Z", seat="txxxx")
    assert _walk(owner, ns).pending == 0
    everything = _search(client, reader, word, ns, status="any")
    assert sorted(_hits(everything)) == sorted([old, new, cand, theirs])                    # control: all four match
    plain = _search(client, reader, word, ns)
    assert sorted(_hits(plain)) == sorted([new, cand, theirs]), "with no parameter the superseded ruling is not a hit"
    assert plain.json()["filter"]["status"] == "active", "and the answer says it was filtered"
    rulings = _search(client, reader, word, ns, type="Ruling")
    assert sorted(_hits(rulings)) == sorted([new, theirs])
    assert rulings.json()["filter"] == {"types": ["Ruling"], "status": "active", "seat": None, "owner": None,
                                        "thread": None}
    assert sorted(_hits(_search(client, reader, word, ns, type="Ruling", status="any"))) == sorted([old, new, theirs])
    assert _hits(_search(client, reader, word, ns, status="superseded")) == [old]
    assert _hits(_search(client, reader, word, ns, seat="txxxx")) == [theirs]
    assert _search(client, reader, word, ns, type="Opinion").status_code == 422


def test_the_search_window_selects_by_the_cards_own_time_as_the_list_does(client, owner, deck):
    """Two decisions about one subject on different days are told apart by the day: `since` is inclusive and
    `until` exclusive on the card's own time, in the search as in the list, and a request without them is
    answered as before (the echo has no `since` or `until` key)."""
    ns, holder, reader = deck
    word = "zqxwin" + uuid.uuid4().hex[:6]
    mon = _card(owner, holder, ns, f"Ruling | {word} deploy monday", time="2026-10-12T09:00:00.000Z")
    tue = _card(owner, holder, ns, f"Ruling | {word} deploy tuesday", time="2026-10-13T09:00:00.000Z")
    tue_late = _card(owner, holder, ns, f"Ruling | {word} deploy tuesday night", time="2026-10-13T23:59:59.999Z",
                     seat="txxxx")
    wed = _card(owner, holder, ns, f"Ruling | {word} deploy wednesday", time="2026-10-14T00:00:00.000Z")
    assert _walk(owner, ns).pending == 0

    def found(**window):
        return sorted(_hits(_search(client, reader, word, ns, **window)))
    day = {"since": "2026-10-13T00:00:00.000Z", "until": "2026-10-14T00:00:00.000Z"}
    assert found() == sorted([mon, tue, tue_late, wed])                                     # control: no window
    assert found(**day) == sorted([tue, tue_late])                    # the card AT `until` is out, the day's last ms in
    assert found(since="2026-10-13T09:00:00.000Z") == sorted([tue, tue_late, wed])         # since is inclusive
    assert found(until="2026-10-13T09:00:00.000Z") == [mon]                                 # until is exclusive
    assert found(since="2026-10-13T09:00:00.000Z", until="2026-10-13T09:00:00.000Z") == []  # equal bounds: nothing
    assert found(since="2026-10-14T00:00:00.000Z", until="2026-10-13T00:00:00.000Z") == []  # reversed: empty, no error
    assert found(seat="txxxx", **day) == [tue_late]                                         # with the other filters
    assert found(type="Candidate", **day) == []
    # The list is asked the same window and answers with the same cards.
    assert sorted(_keys(_list(client, reader, **day))) == found(**day)
    assert _keys(_list(client, reader, **{**day, "since": day["until"], "until": day["since"]})) == []
    # The echo names the window only when one was asked.
    both = _search(client, reader, word, ns, **day).json()["filter"]
    assert both == {"types": [], "status": "active", "seat": None, "owner": None, "thread": None, **day}
    only = _search(client, reader, word, ns, since=day["since"]).json()["filter"]
    assert only["since"] == day["since"] and "until" not in only
    assert "since" not in _search(client, reader, word, ns).json()["filter"]
    for bad in ({"since": "2026-10-08"}, {"since": "yesterday"}, {"until": "2026-10-13T09:00:00Z"},
                {"since": "2026-13-08T07:00:00.000Z"}, {"until": "2026-10-08T25:00:00.000Z"}):
        assert _search(client, reader, word, ns, **bad).status_code == 422, bad


def test_matches_keeps_an_instant_only_when_its_time_has_the_one_spelling():
    """The window compares `time` as text, which is the order of instants only for the one spelling. A card
    with any other spelling (the writer refuses one) is never kept by a window, and without a window nothing
    changed."""
    base = {"type": "Ruling", "seat": "kxxx", "owner": "operator", "text": "Ruling | t"}
    key = "card/kxxx/" + "a" * 32

    def kept(at, **window):
        return store.matches({**base, "time": at}, key, **window)
    mid = "2026-10-13T09:00:00.000Z"
    assert kept(mid, since=mid) and not kept(mid, until=mid)                                # inclusive, exclusive
    assert kept(mid, since="2026-10-13T08:59:59.999Z", until="2026-10-13T09:00:00.001Z")
    assert not kept(mid, since="2026-10-13T09:00:00.001Z")
    for odd in ("2026-10-13T09:00:00Z", "2026-10-13T09:00:00.000+00:00", "2026-10-13 09:00:00.000Z",
                "2026-10-13T09:00:00.0Z", None, 7):
        assert kept(odd) is True, odd                                                       # no window: as before
        assert kept(odd, since="2026-10-01T00:00:00.000Z") is False, odd
        assert kept(odd, until="2027-01-01T00:00:00.000Z") is False, odd
    assert store.matches({"type": "Ruling", "text": "t"}, key, since="2026-10-01T00:00:00.000Z") is False   # no time


def test_the_card_filter_is_refused_on_any_other_namespace(client, owner, world, graph):
    """Refused, not ignored: a caller that filtered would read an unfiltered answer as filtered."""
    ns, holder = graph
    reader = _member(owner, "cardother")
    _grant(owner, ns, holder, reader)
    _row(owner, holder, ns, "wiki/entry", {"body_md": "an ordinary graph row"})
    _walk(owner, ns)
    assert _search(client, reader, "ordinary", ns, type="Ruling").status_code == 422
    for window in ({"since": "2026-10-08T07:00:00.000Z"}, {"until": "2026-10-08T07:00:00.000Z"}):
        assert _search(client, reader, "ordinary", ns, **window).status_code == 422, window
    assert _hits(_search(client, reader, "ordinary", ns)) == ["wiki/entry"]                 # control


# --------------------------------------------------------------------------- who is doing what, and what is new

def test_the_thread_filter_answers_who_is_building_what_in_this_thread(client, owner, deck):
    """The read a seat makes before it starts work on something a thread is discussing."""
    ns, holder, reader = deck
    thread = "transcripts-to-mesh-graph-20261008"

    def commitment(text, time, seat, thread_id):
        card_id = uuid.uuid4().hex
        key = f"card/{seat}/{card_id}"
        value = {"schema": "card.v1", "id": card_id, "type": "Commitment", "owner": seat, "seat": seat,
                 "time": time, "text": text, "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                 "thread_id": thread_id, "done_when": "PR merged",
                 "source": {"record_uuid": str(uuid.uuid4()), "block_index": 0, "kind": "claim", "first_hand": True}}
        _row(owner, holder, ns, key, value)
        return key
    mine = commitment("Commitment | the card server PR", "2026-10-08T09:05:00.000Z", "kxxx", thread)
    theirs = commitment("Commitment | the loader", "2026-10-08T08:24:00.000Z", "txxxx", thread)
    elsewhere = commitment("Commitment | the hints route", "2026-10-08T07:27:00.000Z", "uxxx", "node-awareness-20261007")
    assert _keys(_list(client, reader, type="Commitment")) == [mine, theirs, elsewhere]      # control
    assert _keys(_list(client, reader, type="Commitment", thread=thread)) == [mine, theirs]
    assert _keys(_list(client, reader, thread="node-awareness-20261007")) == [elsewhere]
    assert _list(client, reader, thread="has a space").status_code == 422


def test_recorded_since_is_the_servers_write_time_not_the_cards_own_time(client, owner, deck):
    """A card loaded today from a June transcript has a June `time`. A seat asking what is new since
    its last read must get it; `since`, which reads the card's time, does not return it."""
    ns, holder, reader = deck
    old = _card(owner, holder, ns, "Ruling | from a June transcript", time="2026-06-14T13:01:37.642Z")
    # The immutability trigger only, never USER: the card head record's trigger (sql_meshychat/057) must
    # still see this write, as it sees every real one, or the record keeps the old value.
    owner.execute("ALTER TABLE shared_memory DISABLE TRIGGER shared_memory_immutability")
    owner.execute("UPDATE shared_memory SET created_at = '2026-10-01T00:00:00Z' WHERE key = %s", (old,))
    owner.execute("ALTER TABLE shared_memory ENABLE TRIGGER shared_memory_immutability")
    late = _card(owner, holder, ns, "Ruling | loaded just now, also from June", time="2026-06-15T09:00:00.000Z")
    assert _keys(_list(client, reader, recorded_since="2026-10-05T00:00:00.000Z")) == [late]
    assert _keys(_list(client, reader, since="2026-10-05T00:00:00.000Z")) == []            # the wrong question
    assert _keys(_list(client, reader, recorded_since="2026-09-01T00:00:00.000Z")) == [late, old]   # control
    assert _list(client, reader, recorded_since="yesterday").status_code == 422


def test_a_type_filter_also_returns_the_card_that_replaced_one_of_that_type(client, owner, deck):
    """The operator overrules an answered question by typing. The Ruling is superseded and the typed
    line is a Candidate: a read of Rulings alone would show nothing for the topic."""
    ns, holder, reader = deck
    ruling = _card(owner, holder, ns, "Ruling | hold for the fix", time="2026-10-08T07:32:13.000Z")
    typed = _card(owner, holder, ns, "Candidate | no, deploy it now", time="2026-10-08T07:40:00.000Z",
                  card_type="Candidate", supersedes=[ruling])
    plain = _card(owner, holder, ns, "Candidate | an unrelated prompt", time="2026-10-08T07:50:00.000Z",
                  card_type="Candidate")
    r = _list(client, reader, type="Ruling")
    assert _keys(r) == [typed], "the replacement stands in for the Ruling it replaced; the unrelated Candidate does not"
    assert r.json()["cards"][0]["replaces_requested"] is True
    assert _keys(_list(client, reader, type="Candidate")) == [plain, typed]
    assert "replaces_requested" not in _list(client, reader, type="Candidate").json()["cards"][1]
    assert _keys(_list(client, reader, type="Ruling", status="superseded")) == [ruling]


def test_an_edge_counts_only_when_the_naming_card_is_strictly_later(client, owner, deck):
    """The order is in the read, whatever was written. Two rows that name each other (written by a
    path that skipped the write rules) give ONE live edge, later over earlier; and a card naming one
    at its own instant supersedes nothing."""
    ns, holder, reader = deck
    id_a, id_b = uuid.uuid4().hex, uuid.uuid4().hex
    key_a, key_b = f"card/kxxx/{id_a}", f"card/kxxx/{id_b}"

    def raw(card_id, key, time, text, supersedes):
        _row(owner, holder, ns, key, {
            "schema": "card.v1", "id": card_id, "type": "Ruling", "owner": "operator", "seat": "kxxx", "time": time,
            "text": text, "text_sha256": hashlib.sha256(text.encode()).hexdigest(), "supersedes": supersedes,
            "source": {"record_uuid": str(uuid.uuid4()), "block_index": 0, "kind": "answered_question",
                       "first_hand": True}})
    raw(id_a, key_a, "2026-10-08T07:00:00.000Z", "Ruling | a", [key_b])
    raw(id_b, key_b, "2026-10-08T08:00:00.000Z", "Ruling | b", [key_a])
    assert store.superseders(owner, ns) == {key_a: [key_b]}
    assert _keys(_list(client, reader)) == [key_b], "a loop would leave the topic with no active card"
    same = _card(owner, holder, ns, "Ruling | same instant as b", time="2026-10-08T08:00:00.000Z", supersedes=[key_b])
    assert sorted(_keys(_list(client, reader))) == sorted([key_b, same])


def test_the_card_filter_is_refused_on_the_mail_search_named_or_by_default(client, owner, world, graph):
    """codex-bxxxxx-cli on #169: the filter was checked only on the graph branch, so a mail search with
    `type=Ruling` answered with unfiltered mail and no `filter` echo."""
    ns, holder = graph
    from tests.test_meshsem_routes import _reader
    reader = _reader(owner, "cardmail")                                                     # reads the mail
    for extra in ("type=Ruling", "status=active", "seat=kxxx", "owner=operator", "thread=some-thread",
                  "since=2026-10-08T07:00:00.000Z", "until=2026-10-08T07:00:00.000Z"):
        assert client.get(f"/v1/mesh/search?q=anything&rerank=0&{extra}",
                          **_bearer(reader["peer_id"])).status_code == 422, extra
        assert client.get(f"/v1/mesh/search?q=anything&rerank=0&ns={MESH_CHAT_NAMESPACE}&{extra}",
                          **_bearer(reader["peer_id"])).status_code == 422, extra
    plain = client.get("/v1/mesh/search?q=anything&rerank=0", **_bearer(reader["peer_id"]))
    assert plain.status_code in (200, 503) and "filter" not in plain.json()                 # control: unchanged


def test_a_second_typed_correction_still_stands_in_for_the_ruling_in_the_list_and_in_search(client, owner, deck):
    """memory-bxxxxx on #169, C and D. Ruling A; the operator types B over it; then types C over B. With
    a one-step rule the seat-start read was empty for the topic after C, and the search under
    type=Ruling was empty after B. The replacement walks every live edge, and the search uses the
    same rule as the list."""
    ns, holder, reader = deck
    word = "zqxchain" + uuid.uuid4().hex[:6]
    a = _card(owner, holder, ns, f"Ruling | {word} hold", time="2026-10-08T07:00:00.000Z")
    b = _card(owner, holder, ns, f"Candidate | {word} no, deploy", time="2026-10-08T08:00:00.000Z",
              card_type="Candidate", supersedes=[a])
    assert _keys(_list(client, reader, type="Ruling")) == [b]                               # one step, as before
    c = _card(owner, holder, ns, f"Candidate | {word} deploy, after the backup", time="2026-10-08T09:00:00.000Z",
              card_type="Candidate", supersedes=[b])
    unrelated = _card(owner, holder, ns, f"Candidate | {word} unrelated", time="2026-10-08T10:00:00.000Z",
                      card_type="Candidate")
    seat_start = _list(client, reader, type=["Ruling", "Constraint", "Commitment", "OpenItem"])
    assert _keys(seat_start) == [c], "the second correction stands in for the Ruling; the unrelated prompt does not"
    assert seat_start.json()["cards"][0]["replaces_requested"] is True
    assert store.replacers(owner, ns, ("Ruling",)) == {b, c}
    assert store.replacers(owner, ns, ("Commitment",)) == set() and store.replacers(owner, ns, ()) == set()
    assert _walk(owner, ns).pending == 0
    assert _hits(_search(client, reader, word, ns, type="Ruling")) == [c], "the list and the search agree"
    assert sorted(_hits(_search(client, reader, word, ns, type="Candidate"))) == sorted([c, unrelated])


# --------------------------------------------------------------------------- the client's session-start read

def test_the_four_reads_of_card_recent_are_ones_this_route_answers(client, owner, deck, monkeypatch, tmp_path):
    """`card recent` (tools/meshy_node/cards.py, the briefing at a session start) against this route and the real
    store: each of its four reads is answered, `recorded_since` in milliseconds included (the route refuses any
    other spelling with a 422), and the briefing is built from the fields the route returns: `time`, `question`,
    `answer`, the seat in the key, `type` with `replaces_requested`, and `overruled_by`. node.cards is the one
    thing stood in for: it asks this route through the test client, as this peer's reader."""
    ns, holder, reader = deck

    def card(seat, hours_ago, card_type="Ruling", *, question=None, answer=None, words="", supersedes=None):
        card_id = uuid.uuid4().hex
        key = f"card/{seat}/{card_id}"
        at = client_cards.now_time(datetime.now(timezone.utc) - timedelta(hours=hours_ago))
        text = f"{card_type} | owner: operator | seat: {seat} | {at[:16]}\n{words or question}"
        value = {"schema": "card.v1", "id": card_id, "type": card_type, "owner": "operator", "seat": seat,
                 "time": at, "text": text, "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                 "source": {"record_uuid": str(uuid.uuid4()), "block_index": 0, "kind": "answered_question",
                            "first_hand": True}}
        value.update({k: v for k, v in (("question", question), ("answer", answer), ("supersedes", supersedes))
                      if v is not None})
        assert validation.validate_card(ns, key, value)
        _row(owner, holder, ns, key, value)
        return key, at

    old, _ = card("kxxx", 30, question="Hold the deploy?", answer="Hold")
    typed, typed_at = card("kxxx", 20, "Candidate", words="no, deploy it now", supersedes=[old])
    mine, mine_at = card("kxxx", 2, question="Which namespace?", answer="shared/decisions")
    txxxx, txxxx_at = card("txxxx", 1, question="Overrule kxxx?", answer="Yes", supersedes=[mine])
    asked = []

    def via_route(path, state, query, *, timeout=None, max_bytes=None, purpose=None):
        assert purpose == "briefing", purpose               # issue #238 C3: counted as a briefing, not as use
        asked.append(query)
        r = client.get("/v1/cards?" + urllib.parse.urlencode([*query, ("purpose", purpose)]),
                       **_bearer(reader["peer_id"]))
        assert r.status_code == 200, r.content
        assert max_bytes == client_cards.RECENT_MAX_ANSWER_BYTES and len(r.content) <= max_bytes
        return r.json()

    monkeypatch.setattr(client_node, "cards", via_route)
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"schema": client_node.STATE_SCHEMA, "base_url": "http://testserver",
                                      "status": "minted", "peer_id": str(reader["peer_id"]), "jwt": "x",
                                      "jwt_expires_at": "2099-01-01T00:00:00Z"}), encoding="utf-8")
    lines = client_cards.recent(state_file, "http://testserver", seat="kxxx", n=10, others=8, days=3,
                                load_result=None, deadline=time.monotonic() + 60)
    assert len(asked) == 4
    def minute(at):
        return at[:16].replace("T", " ")

    assert lines == [
        client_cards.RECENT_FIRST_LINE,
        "Newest active Rulings recorded for seat kxxx:",
        f"  {minute(mine_at)} | Q: Which namespace? | A: shared/decisions | overruled by a later card of txxxx",
        f"  {minute(typed_at)} | Candidate in place of a Ruling: no, deploy it now",
        f"  freshness: newest card of any kind for this seat {minute(mine_at)} (2.0 h ago); newest card of any seat "
        f"{minute(txxxx_at)}",
        "Rulings other seats recorded in the last 3 days (1 shown; context, not applicable by default):",
        f"  {minute(txxxx_at)} txxxx         | Q: Overrule kxxx? | A: Yes",
    ], "kxxx's first Ruling is superseded by kxxx's own typed line, which stands in for it; txxxx's overrules, "\
       "and hides nothing"


def test_every_read_says_whether_a_cards_thread_was_declared_or_inferred_by_a_hook(client, owner, deck):
    """pxx, node-awareness item 8: the question hook INFERS a card's thread_id from the session, and the thread
    filter accepts that card like a declared one. Codex's case: an inferred and a declared Question in the same
    thread come back from one thread-filtered read, the list and the search alike, and each says which it is."""
    ns, holder, reader = deck
    thread = "node-awareness-20261007"
    word = "zqxtier" + uuid.uuid4().hex[:6]

    def question(text, time, kind, thread_id=None):
        card_id = uuid.uuid4().hex
        key = f"card/kxxx/{card_id}"
        value = {"schema": "card.v1", "id": card_id, "type": "Question", "owner": "operator", "seat": "kxxx",
                 "time": time, "text": text, "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                 "source": {"record_uuid": str(uuid.uuid4()), "block_index": 0, "kind": kind, "first_hand": True}}
        if thread_id is not None:
            value["thread_id"] = thread_id
        _row(owner, holder, ns, key, value)
        return key
    inferred = question(f"Question | {word} asked in a session", "2026-10-10T09:00:00.000Z", "question_hook", thread)
    declared = question(f"Question | {word} put in the thread", "2026-10-10T08:00:00.000Z", "claim", thread)
    answered = question(f"Question | {word} answered", "2026-10-10T07:00:00.000Z", "answered_question_hook", thread)
    loose = question(f"Question | {word} with no thread", "2026-10-10T06:00:00.000Z", "question_hook")
    expected = {inferred: "inferred", declared: "declared", answered: "inferred"}

    listed = _list(client, reader, thread=thread)
    assert _keys(listed) == [inferred, declared, answered]
    assert {c["key"]: c["thread_tier"] for c in listed.json()["cards"]} == expected
    unfiltered = {c["key"]: c for c in _list(client, reader).json()["cards"]}
    assert loose in unfiltered and "thread_tier" not in unfiltered[loose], "no thread, no tier"

    assert _walk(owner, ns).pending == 0
    found = _search(client, reader, word, ns, thread=thread)
    assert sorted(_hits(found)) == sorted([inferred, declared, answered])
    assert {h["key"]: h["thread_tier"] for h in found.json()["hits"]} == expected
    assert {h["key"]: h["thread_id"] for h in found.json()["hits"]} == dict.fromkeys(expected, thread)
    every = {h["key"]: h for h in _search(client, reader, word, ns).json()["hits"]}
    assert set(every) == {inferred, declared, answered, loose}                             # control: all four match
    assert "thread_tier" not in every[loose] and every[loose]["thread_id"] is None


def test_a_card_shaped_key_in_another_namespace_gets_no_tier_and_cannot_fail_the_search(client, owner, world, graph):
    """Codex on #221: the lookup took any `card/` key for a card, in every graph namespace. Outside the card namespace
    memory.write does not validate cards, so a row there with a list for `source.kind` made thread_tier raise
    (unhashable) and the whole search answered 500. Only the card namespace's rows get a tier."""
    ns, holder = graph                                     # NOT the card namespace: no override here
    reader = _member(owner, "cardshape")
    _grant(owner, ns, holder, reader)
    word = "zqxshape" + uuid.uuid4().hex[:6]
    _row(owner, holder, ns, "card/looks/like", {"text": f"{word} card shaped", "thread_id": "some-thread-20261010",
                                                "source": {"kind": ["question_hook"]}})
    _row(owner, holder, ns, "wiki/plain", {"body_md": f"{word} an ordinary row"})
    _walk(owner, ns)
    r = _search(client, reader, word, ns)
    assert sorted(_hits(r)) == ["card/looks/like", "wiki/plain"]
    by_key = {h["key"]: h for h in r.json()["hits"]}
    assert "thread_tier" not in by_key["card/looks/like"] and by_key["card/looks/like"]["thread_id"] is None


def test_thread_tier_reads_a_string_kind_only():
    """An unhashable kind is 'declared', never an exception: `in` a frozenset raises on a list or a dict."""
    for kind in (["question_hook"], {"k": 1}, None, 7):
        assert store.thread_tier({"thread_id": "t-20261010", "source": {"kind": kind}}) == "declared"
    assert store.thread_tier({"thread_id": "t-20261010", "source": {"kind": "question_hook"}}) == "inferred"
    assert store.thread_tier({"thread_id": "t-20261010", "source": ["not", "a", "dict"]}) == "declared"
