"""GET /v1/cards cuts a card's three key lists at 32, in SQL, with the true counts (divergence 091, amended
2026-10-10).

`superseded_by`, `overruled_by` and `named_by_redacted` were whole lists: a key is named by as many later cards
as name it, so a page of cards each named by thousands was larger than any client cap, and `card recent` (#224)
reads at most 8 MiB and then fails closed (kxxx, msg/<key>). Each list now holds at most
`store.NAMED_CAP` keys, the first by key; `*_count` is the true number and `truncated_lists: true` says a list
was cut. The cut is an array slice in `store._AGG`, so the row the server reads is bounded too, and the status
is read from the count of ALL a card's own-seat namers, never from a cut list.

The mutants (NC1 to NC19, of apps/cards/store.py) are named in the docstring of each test that kills them, and
listed with their results in the amendment.

Run:  .venv/Scripts/python.exe -m pytest tests/test_cards_named_cap.py -q -p no:cacheprovider
"""
from __future__ import annotations

import hashlib
import json
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.test_cards_routes import (  # noqa: E402,F401  (fixtures)
    _bearer, _card, _hits, _keys, _list, _row, _search, _walk, client, deck, graph, owner, world)

from django.core.serializers.json import DjangoJSONEncoder  # noqa: E402

from apps.cards import store, validation  # noqa: E402

#: The number the tests are written against, as a literal: a test that read store.NAMED_CAP would move with a
#: mutant of it (NC2, NC3) and pass.
CAP = 32


def _namers(owner, holder, ns, target, n, *, seat="kxxx", hour=8):
    """`n` cards of `seat`, each later than `target` and naming it; their keys."""
    return [_card(owner, holder, ns, f"Ruling | namer {i}", seat=seat, supersedes=[target],
                  time=f"2026-10-08T{hour:02d}:{i // 60:02d}:{i % 60:02d}.000Z") for i in range(n)]


def _redact(owner, ns, keys):
    """Turn head cards into the tombstones memory.redact writes for a card since divergence 092: no prose, and
    `schema`, `id`, `seat`, `time`, `supersedes` and `type` kept (the real redaction is
    tests/test_cards_redaction.py's). One statement, the immutability trigger off for it."""
    # The immutability trigger only, never USER: the card head record's trigger (sql_meshychat/057) must
    # still see this write, as it sees every real one, or the record keeps the old value.
    owner.execute("ALTER TABLE shared_memory DISABLE TRIGGER shared_memory_immutability")
    try:
        owner.execute(
            "UPDATE shared_memory SET value = jsonb_build_object('redacted', true, 'reason_code', 'test', "
            "  'redacted_at', '2026-10-10T00:00:00Z', 'type', value->'type', 'schema', value->'schema', "
            "  'id', value->'id', 'seat', value->'seat', 'time', value->'time', 'supersedes', value->'supersedes') "
            "WHERE namespace = %s AND key = ANY(%s) AND is_head", (ns, list(keys)))
    finally:
        owner.execute("ALTER TABLE shared_memory ENABLE TRIGGER shared_memory_immutability")


def _one(client, reader, key, **params) -> dict:
    """The card `key` as GET /v1/cards?status=any returns it."""
    r = _list(client, reader, status="any", limit="200", **params)
    assert r.status_code == 200, r.content
    found = [c for c in r.json()["cards"] if c["key"] == key]
    assert len(found) == 1, f"{key} not listed once"
    return found[0]


def test_the_cap_is_32():
    """The constant the SQL is bound with. MUTANTS NC2 and NC3 (31, 33) fail here and in the route tests below."""
    assert store.NAMED_CAP == CAP


# --------------------------------------------------------------------------- superseded_by

def test_superseded_by_is_cut_at_32_of_33_with_the_true_count(client, owner, deck):
    """33 cards of the card's own seat name it: 32 keys, the first by key, a count of 33, `truncated_lists`.
    MUTANTS NC1 (no slice: 33 keys), NC2 (cap 31), NC3 (cap 33: 33 keys, nothing cut), NC4 and NC4b (the
    count read from the cut list, in SQL and in Python: 32), NC7 (`truncated_lists` never set), NC8
    (`truncated_lists` blind to superseded_by), NC16 (the kept keys by key DESCENDING: another 32)."""
    ns, holder, reader = deck
    target = _card(owner, holder, ns, "Ruling | named 33 times", time="2026-10-08T07:00:00.000Z")
    namers = _namers(owner, holder, ns, target, CAP + 1)
    card = _one(client, reader, target)
    assert card["status"] == "superseded"
    assert card["superseded_by"] == sorted(namers)[:CAP]
    assert card["superseded_by_count"] == CAP + 1
    assert card["overruled_by"] == [] and card["overruled_by_count"] == 0
    assert card["truncated_lists"] is True
    assert "named_by_redacted" not in card and "named_by_redacted_count" not in card


def test_superseded_by_at_exactly_32_is_whole_and_says_nothing_was_cut(client, owner, deck):
    """CONTROL at the cap: 32 namers are 32 keys, a count of 32, and no `truncated_lists` key at all (the
    route leaves an optional field out when there is nothing to say, as `replaces_requested`).
    MUTANTS NC2 (cap 31: 31 keys and a cut), NC17 (`truncated_lists: false` sent when nothing was cut)."""
    ns, holder, reader = deck
    target = _card(owner, holder, ns, "Ruling | named 32 times", time="2026-10-08T07:00:00.000Z")
    namers = _namers(owner, holder, ns, target, CAP)
    card = _one(client, reader, target)
    assert card["status"] == "superseded"
    assert card["superseded_by"] == sorted(namers) and card["superseded_by_count"] == CAP
    assert "truncated_lists" not in card


# --------------------------------------------------------------------------- overruled_by

def test_overruled_by_is_cut_at_32_of_33_with_the_true_count(client, owner, deck):
    """33 on record Rulings of ANOTHER seat name it: it stays active (only its own seat hides it), with 32 of
    the 33 keys in `overruled_by`. MUTANTS NC1, NC2, NC3, NC5 and NC5b (the count read from the cut list), NC7, NC9
    (`truncated_lists` blind to overruled_by)."""
    ns, holder, reader = deck
    target = _card(owner, holder, ns, "Ruling | overruled 33 times", time="2026-10-08T07:00:00.000Z", seat="pxx")
    namers = _namers(owner, holder, ns, target, CAP + 1, seat="kxxx")
    card = _one(client, reader, target)
    assert card["status"] == "active"
    assert card["overruled_by"] == sorted(namers)[:CAP] and card["overruled_by_count"] == CAP + 1
    assert card["superseded_by"] == [] and card["superseded_by_count"] == 0
    assert card["truncated_lists"] is True
    assert target in _keys(_list(client, reader, limit="200")), "overruled is not hidden, however many overrule it"


def test_overruled_by_at_exactly_32_is_whole(client, owner, deck):
    """CONTROL at the cap for overruled_by. MUTANT NC2 (cap 31)."""
    ns, holder, reader = deck
    target = _card(owner, holder, ns, "Ruling | overruled 32 times", time="2026-10-08T07:00:00.000Z", seat="pxx")
    namers = _namers(owner, holder, ns, target, CAP, seat="kxxx")
    card = _one(client, reader, target)
    assert card["overruled_by"] == sorted(namers) and card["overruled_by_count"] == CAP
    assert "truncated_lists" not in card


# --------------------------------------------------------------------------- named_by_redacted

def test_named_by_redacted_is_cut_at_32_of_33_and_only_it(client, owner, deck):
    """33 redacted cards name it: 17 of its own seat and 16 of another. So `superseded_by` (17) and
    `overruled_by` (16) are whole and ONLY `named_by_redacted` is cut, and it alone sets `truncated_lists`.
    MUTANTS NC1, NC2, NC3, NC6 and NC6b (its count read from the cut list), NC7, NC10 (`truncated_lists` blind to
    named_by_redacted)."""
    ns, holder, reader = deck
    target = _card(owner, holder, ns, "Ruling | named by 33 redacted", time="2026-10-08T07:00:00.000Z", seat="pxx")
    own = _namers(owner, holder, ns, target, 17, seat="pxx")
    other = _namers(owner, holder, ns, target, 16, seat="kxxx", hour=9)
    _redact(owner, ns, own + other)
    card = _one(client, reader, target)
    assert card["status"] == "superseded", "a redacted card of its own seat still supersedes it (divergence 092)"
    assert card["superseded_by"] == sorted(own) and card["superseded_by_count"] == 17
    assert card["overruled_by"] == sorted(other) and card["overruled_by_count"] == 16
    assert card["named_by_redacted"] == sorted(own + other)[:CAP]
    assert card["named_by_redacted_count"] == CAP + 1
    assert card["truncated_lists"] is True


def test_named_by_redacted_at_exactly_32_is_whole(client, owner, deck):
    """CONTROL at the cap for named_by_redacted (16 and 16). MUTANT NC2 (cap 31)."""
    ns, holder, reader = deck
    target = _card(owner, holder, ns, "Ruling | named by 32 redacted", time="2026-10-08T07:00:00.000Z", seat="pxx")
    own = _namers(owner, holder, ns, target, 16, seat="pxx")
    other = _namers(owner, holder, ns, target, 16, seat="kxxx", hour=9)
    _redact(owner, ns, own + other)
    card = _one(client, reader, target)
    assert card["named_by_redacted"] == sorted(own + other) and card["named_by_redacted_count"] == CAP
    assert "truncated_lists" not in card


# --------------------------------------------------------------------------- the status is the full set's

def test_the_status_is_decided_by_the_33rd_namer_which_the_cut_drops(client, owner, deck):
    """32 on record Rulings of seat amy and ONE card of the card's own seat zed name it. By key the 32 amy
    cards come first and zed's is the 33rd, so a cut of all 33 namers taken before they are split by seat
    drops the one card that supersedes it. The status, the status filters, `superseders` and the search filter
    all still say superseded, and its own lists are whole (32 and 1).
    MUTANT NC11 (the 32 kept of ALL namers before the seat split: it reads as active, in the list and in
    `superseders`). NC12 (status from the cut `superseded_by` in Python), NC13 (the SQL status filter on the
    cut array) and NC15 (`superseders` on the cut list) survive, as equivalent: a slice [1:32] of a non-empty
    array is never empty, so a status read from the cut own-seat list is the full set's."""
    ns, holder, reader = deck
    target = _card(owner, holder, ns, "Ruling | zed's own", time="2026-10-08T07:00:00.000Z", seat="zed")
    others = _namers(owner, holder, ns, target, CAP, seat="amy")
    mine = _card(owner, holder, ns, "Ruling | zed changes its mind", time="2026-10-08T10:00:00.000Z", seat="zed",
                 supersedes=[target])
    assert sorted(others + [mine]).index(mine) == CAP, "zed's card is the 33rd namer by key"
    card = _one(client, reader, target)
    assert card["status"] == "superseded"
    assert card["superseded_by"] == [mine] and card["superseded_by_count"] == 1
    assert card["overruled_by"] == sorted(others) and card["overruled_by_count"] == CAP
    assert "truncated_lists" not in card
    assert target in _keys(_list(client, reader, status="superseded", limit="200"))
    assert target not in _keys(_list(client, reader, limit="200"))
    assert store.superseders(owner, ns)[target] == [mine]


def test_a_card_superseded_by_33_is_superseded_in_every_read(client, owner, deck):
    """The status filters and the search's key set are read from the count of all 33: the card is in
    status=superseded and not in the default read, and `superseders` holds it with the first 32 keys.
    MUTANTS NC1, NC2, NC3 and NC16 (`superseders` holds another list). It is also the CONTROL of the test above:
    it passes under NC11 (33 own-seat namers cut to 32 before the split still leave the card superseded), so
    that test fails under NC11 for the seat split alone."""
    ns, holder, reader = deck
    target = _card(owner, holder, ns, "Ruling | superseded 33 times", time="2026-10-08T07:00:00.000Z")
    namers = _namers(owner, holder, ns, target, CAP + 1)
    assert target in _keys(_list(client, reader, status="superseded", limit="200"))
    assert target not in _keys(_list(client, reader, limit="200"))
    assert store.superseders(owner, ns)[target] == sorted(namers)[:CAP]


def test_the_search_filter_agrees_with_the_list_on_a_cut_card(client, owner, deck):
    """GET /v1/mesh/search?ns=<card namespace> filters on `store.superseders`, the same `_AGG`: a card named by
    33 of its own seat, and one decided by its 33rd namer, are superseded there as on the list.
    Search hits carry no key lists (graphsearch._lookup builds a hit from the row's chunk), so there is no list
    there to cut. MUTANT NC11 (the 33rd-namer card turns into an active hit)."""
    ns, holder, reader = deck
    word = "zqxcap" + uuid.uuid4().hex[:6]
    many = _card(owner, holder, ns, f"Ruling | {word} named by 33", time="2026-10-08T07:00:00.000Z")
    _namers(owner, holder, ns, many, CAP + 1)
    late = _card(owner, holder, ns, f"Ruling | {word} decided by the 33rd", time="2026-10-08T07:00:00.000Z",
                 seat="zed")
    _namers(owner, holder, ns, late, CAP, seat="amy", hour=9)
    _card(owner, holder, ns, "Ruling | zed changes its mind", time="2026-10-08T11:00:00.000Z", seat="zed",
          supersedes=[late])
    control = _card(owner, holder, ns, f"Ruling | {word} nobody names", time="2026-10-08T07:30:00.000Z", seat="txxxx")
    assert _walk(owner, ns).pending == 0
    active = _hits(_search(client, reader, word, ns))
    assert active[0] == control and many not in active and late not in active, active
    assert sorted(_hits(_search(client, reader, word, ns, status="superseded"))) == sorted([many, late])
    assert {many, late, control} <= set(_hits(_search(client, reader, word, ns, status="any")))       # control


# --------------------------------------------------------------------------- the read is bounded in SQL

def test_the_rows_read_from_the_database_hold_32_keys_not_33(client, owner, deck, monkeypatch):
    """The cut is in the SQL, so the row the server fetches is already short: each row list_cards reads, as
    psycopg hands it over, holds 32 keys and the count 33; and `_BY_TARGET`'s own result has cardinality 32.
    A cut in Python after the fetch gives the same JSON and reads every key. MUTANT NC14 (the SQL slice
    removed and the lists cut in `_named_fields`: the JSON passes, these rows hold 33), NC1."""
    ns, holder, reader = deck
    target = _card(owner, holder, ns, "Ruling | named 33 times", time="2026-10-08T07:00:00.000Z", seat="pxx")
    own = _namers(owner, holder, ns, target, CAP + 1, seat="pxx")
    other = _namers(owner, holder, ns, target, CAP + 1, seat="kxxx", hour=9)
    redacted = _namers(owner, holder, ns, target, CAP + 1, seat="pxx", hour=10)
    _redact(owner, ns, redacted)
    fetched = []
    real = store.dict_row

    def recording(cursor):
        make = real(cursor)

        def row(values):
            got = make(values)
            fetched.append(got)
            return got
        return row
    monkeypatch.setattr(store, "dict_row", recording)
    card = _one(client, reader, target)
    assert card["superseded_by_count"] == 2 * (CAP + 1) and card["overruled_by_count"] == CAP + 1
    assert card["named_by_redacted_count"] == CAP + 1 and card["truncated_lists"] is True
    raw = [r for r in fetched if r.get("key") == target]
    assert len(raw) == 1
    assert [len(raw[0][c]) for c in ("by_keys", "overruled_by", "by_redacted")] == [CAP, CAP, CAP]
    assert [raw[0][c] for c in ("by_count", "overruled_count", "redacted_count")] == [2 * (CAP + 1), CAP + 1, CAP + 1]
    assert len(own + redacted) == 2 * (CAP + 1) and len(other) == CAP + 1
    with owner.cursor() as cur:
        cur.execute(f"SELECT cardinality(x.by_keys), cardinality(x.overruled_by), cardinality(x.by_redacted), "
                    f"x.by_count FROM ({store._BY_TARGET}) x WHERE x.target = %(target)s",
                    {"ns": ns, "named_cap": store.NAMED_CAP, "target": target})
        assert cur.fetchall() == [(CAP, CAP, CAP, 2 * (CAP + 1))]


def test_a_key_repeated_in_one_supersedes_is_one_namer(client, owner, deck):
    """A write refuses a repeated key in `supersedes` (validation.supersedes_of); a row that has one anyway (a
    path that skipped the write rules) is ONE card naming the target: one key, a count of 1, so the count and
    the list cannot disagree. MUTANT NC18 (`DISTINCT` dropped: two keys and a count of 2)."""
    ns, holder, reader = deck
    target = _card(owner, holder, ns, "Ruling | named twice by one", time="2026-10-08T07:00:00.000Z")
    card_id = uuid.uuid4().hex
    key = f"card/kxxx/{card_id}"
    text = "Ruling | names it twice"
    _row(owner, holder, ns, key, {
        "schema": "card.v1", "id": card_id, "type": "Ruling", "owner": "operator", "seat": "kxxx",
        "time": "2026-10-08T08:00:00.000Z", "text": text, "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "supersedes": [target, target], "source": {"record_uuid": str(uuid.uuid4()), "block_index": 0,
                                                    "kind": "answered_question", "first_hand": True}})
    card = _one(client, reader, target)
    assert card["superseded_by"] == [key] and card["superseded_by_count"] == 1
    assert "truncated_lists" not in card


# --------------------------------------------------------------------------- why 32

def test_a_page_of_200_cards_at_every_cap_stays_under_card_recents_8_mib():
    """The reason for the cut, as arithmetic on the real encoder. `card recent` (#224, cards.RECENT_MAX_ANSWER_BYTES)
    reads at most 8 MiB of one answer. The largest page: MAX_LIMIT cards, each a value of MAX_VALUE_BYTES as the
    write measures it (ensure_ascii off), which JsonResponse writes with ensure_ascii on, at most 3 times longer
    (a 4-byte character becomes a 12-byte surrogate pair); and the derived fields with all three lists full of
    the longest keys a card may have (a 64-character seat). CONTROL: the same page with the lists uncut at 200
    namers each (a card can be named by any number) is over the cap. A cap of 64 would not fit."""
    longest = "card/" + "s" * 64 + "/" + "f" * 32
    assert validation.key_parts(longest) is not None

    def page(n_keys):
        lists = [longest] * n_keys
        derived = {"key": longest, "version": 10 ** 6, "written_at": "2026-10-10T12:00:00.123456+00:00",
                   "status": "superseded", "superseded_by": lists, "superseded_by_count": 10 ** 6,
                   "overruled_by": lists, "overruled_by_count": 10 ** 6, "named_by_redacted": lists,
                   "named_by_redacted_count": 10 ** 6, "truncated_lists": True, "replaces_requested": True,
                   "thread_tier": "declared"}
        one = len(json.dumps(derived, cls=DjangoJSONEncoder)) + 3 * validation.MAX_VALUE_BYTES
        envelope = len(json.dumps({"namespace": "s" * 64, "filter": {"types": sorted(validation.TYPES),
                                   "status": "superseded", "seat": "s" * 64, "owner": "s" * 64, "thread": "t" * 128},
                                   "cards": [], "next": "c" * 200}))
        return store.MAX_LIMIT * (one + 2) + envelope
    cap = 8 * 1024 * 1024
    assert page(store.NAMED_CAP) < cap
    assert page(200) > cap, "control: uncut lists overflow the same page"
    assert page(64) > cap, "a cap of 64 keys would not fit"
