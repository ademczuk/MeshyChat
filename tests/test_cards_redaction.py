"""Divergence 092: a redacted decision card keeps what it replaced.

Through the REAL memory.redact (apps/memoryskills/redact.py) and the database's own trigger
(sql_meshychat/027, its key list replaced by 040), then read with the card list's own query.

The failure this closes (memory-bxxxxx on the design, msg/<key>): B
supersedes A; B is redacted for a credential; the tombstone kept no `supersedes`, so A read as the
current ruling again, in every seat's default read, with nothing saying so.

Run:  .venv/Scripts/python.exe -m pytest tests/test_cards_redaction.py -q -p no:cacheprovider
"""
from __future__ import annotations

import hashlib
import uuid

import pytest

from tests.test_memory_skills import (  # noqa: F401 - fixtures are used by name; sets up Django
    _call,
    _claim,
    _peer,
    app_conn,
    owner_conn,
)
from apps.cards import store  # noqa: E402
from apps.cards import validation as V  # noqa: E402
from apps.memoryskills import handlers, redact  # noqa: E402
from apps.memoryskills.errors import HandlerError  # noqa: E402

SECRET = "the answer held a credential: zq-credential-marker"


def _card(seat, text, time, *, card_type="Ruling", **more) -> tuple[str, dict]:
    card_id = uuid.uuid4().hex
    value = {"schema": "card.v1", "id": card_id, "type": card_type, "owner": "operator", "seat": seat,
             "time": time, "text": text, "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
             "source": {"record_uuid": str(uuid.uuid4()), "block_index": 0, "kind": "answered_question",
                        "first_hand": True}}
    value.update(more)
    return f"card/{seat}/{card_id}", value


@pytest.fixture()
def cards(app_conn, owner_conn, monkeypatch):
    ns = f"shared/cards-{uuid.uuid4().hex[:12]}"
    seat = _peer(owner_conn, "seat")
    _claim(app_conn, seat, ns)
    monkeypatch.setenv("MESHY_CARDS_NAMESPACE", ns)
    monkeypatch.setattr(handlers, "_ROSTER_LABELS", {})
    return ns, seat, f"peer-{str(seat['peer_id'])[:8]}"


def _write(conn, peer, ns, key, value):
    return _call(conn, peer, "memory.write", {"namespace": ns, "key": key, "value": value})


def _redact(conn, peer, ns, key):
    return _call(conn, peer, "memory.redact", {"namespace": ns, "key": key, "reason_code": "credential"})


def _head(conn, ns, key) -> dict:
    row = conn.execute("SELECT value FROM shared_memory WHERE namespace = %s AND key = %s AND is_head",
                       (ns, key)).fetchone()
    conn.commit()
    return row[0]


def _listed(conn, ns, **flt) -> dict[str, dict]:
    page = store.list_cards(conn, ns, **flt)
    conn.commit()
    return {c["key"]: c for c in page["cards"]}


def test_a_redacted_card_still_supersedes_what_it_replaced(app_conn, owner_conn, cards):
    ns, seat, label = cards
    a_key, a = _card(label, "Ruling | hold for the fix", "2026-10-08T07:32:13.000Z")
    b_key, b = _card(label, f"Ruling | deploy now. {SECRET}", "2026-10-08T07:37:17.834Z", supersedes=[a_key],
                     question="Deploy or hold?", answer=f"Deploy. {SECRET}", notes=SECRET)
    _write(app_conn, seat, ns, a_key, a)
    _write(app_conn, seat, ns, b_key, b)
    assert list(_listed(owner_conn, ns)) == [b_key]                           # control: before the redaction

    _redact(app_conn, seat, ns, b_key)

    tomb = _head(owner_conn, ns, b_key)
    assert tomb["redacted"] is True and tomb["reason_code"] == "credential"
    assert {k: tomb[k] for k in redact.CARD_FIELDS} == {
        "schema": "card.v1", "id": b["id"], "seat": label, "time": b["time"], "supersedes": [a_key]}
    assert set(tomb) == {"redacted", "reason_code", "redacted_at", "type", *redact.CARD_FIELDS}, sorted(tomb)
    assert SECRET not in str(tomb), "no prose survives: text, question, answer and notes are gone"

    assert list(_listed(owner_conn, ns)) == [], "A is NOT current again, and the tombstone is not a card"
    superseded = _listed(owner_conn, ns, status="superseded")
    assert list(superseded) == [a_key]
    assert superseded[a_key]["superseded_by"] == [b_key]
    assert superseded[a_key]["named_by_redacted"] == [b_key]
    assert store.superseders(owner_conn, ns) == {a_key: [b_key]}


def test_a_card_superseded_by_a_live_card_says_nothing_about_redaction(app_conn, owner_conn, cards):
    """Control for the marker: it appears only when a naming card IS redacted."""
    ns, seat, label = cards
    a_key, a = _card(label, "Ruling | a", "2026-10-08T07:00:00.000Z")
    b_key, b = _card(label, "Ruling | b", "2026-10-08T08:00:00.000Z", supersedes=[a_key])
    _write(app_conn, seat, ns, a_key, a)
    _write(app_conn, seat, ns, b_key, b)
    card = _listed(owner_conn, ns, status="superseded")[a_key]
    assert card["superseded_by"] == [b_key] and "named_by_redacted" not in card


def test_a_redacted_card_is_not_written_again_and_its_tombstone_is_not_a_target(app_conn, owner_conn, cards):
    ns, seat, label = cards
    a_key, a = _card(label, "Ruling | a", "2026-10-08T07:00:00.000Z")
    _write(app_conn, seat, ns, a_key, a)
    _redact(app_conn, seat, ns, a_key)
    with pytest.raises(HandlerError) as exc:
        _write(app_conn, seat, ns, a_key, a)
    assert exc.value.code == "E_CARD_REDACTED"
    c_key, c = _card(label, "Ruling | c", "2026-10-08T09:00:00.000Z", supersedes=[a_key])
    with pytest.raises(HandlerError) as exc:
        _write(app_conn, seat, ns, c_key, c)
    assert exc.value.code == "E_CARD_SUPERSEDES", "the tombstone keeps `time`, and is still not a card to supersede"
    assert store.superseders(owner_conn, ns) == {}


def test_a_row_that_is_not_a_card_keeps_none_of_the_card_fields(app_conn, owner_conn, cards):
    """The five fields are kept for a card key of the card namespace ONLY. Elsewhere a field named `id`,
    `time` or `supersedes` can hold anything, and the database's list merely permits them."""
    _, seat, _ = cards
    other = f"memtest/other-{uuid.uuid4().hex[:10]}"
    _claim(app_conn, seat, other)
    value = {"schema": "note", "id": "free text id", "seat": "anything", "time": "whenever",
             "supersedes": ["a pointer of this namespace's own"], "type": "note", "body": SECRET}
    _write(app_conn, seat, other, "note/x", value)
    _redact(app_conn, seat, other, "note/x")
    tomb = _head(owner_conn, other, "note/x")
    assert set(tomb) == {"redacted", "reason_code", "redacted_at", "type"}, sorted(tomb)


def test_the_pure_tombstone_keeps_card_fields_only_when_told_the_row_is_a_card():
    old = {"schema": "card.v1", "id": "0" * 32, "seat": "kxxx", "time": "2026-10-08T07:37:17.834Z",
           "supersedes": ["card/kxxx/" + "1" * 32], "type": "Ruling", "text": SECRET, "answer": SECRET,
           "source": {"record_uuid": "r", "block_index": 0}}
    plain = redact.tombstone(old, reason_code="credential", redacted_at="t")
    card = redact.tombstone(old, reason_code="credential", redacted_at="t", card=True)
    assert set(plain) == {"redacted", "reason_code", "redacted_at", "type"}
    assert set(card) == set(plain) | set(redact.CARD_FIELDS)
    assert SECRET not in str(card) and "source" not in card
    bad = dict(old, supersedes=[{"not": "a key"}], time={"nested": SECRET})
    kept = redact.tombstone(bad, reason_code="credential", redacted_at="t", card=True)
    assert "supersedes" not in kept and "time" not in kept, "a field whose shape is not an identifier is dropped"


def test_the_card_fields_hold_no_prose_field_of_a_card():
    """If a prose field is ever added to CARD_FIELDS, a redaction would keep the text it exists to remove."""
    prose = {"text", "question", "answer", "notes", "options", "done_when", "owner"}
    assert not set(redact.CARD_FIELDS) & prose
    assert set(redact.CARD_FIELDS) <= V.REQUIRED_FIELDS | V.OPTIONAL_FIELDS


def test_redacting_a_middle_correction_keeps_the_last_one_in_the_seat_start_read(app_conn, owner_conn, cards):
    """memory-bxxxxx on #170, finding 1. Ruling A, a typed B over A, a typed C over B; B is redacted. C
    named B, and if a tombstone could not be the TARGET of an edge, C lost its link to a Ruling and the
    seat-start read was empty, with a readable current card sitting there."""
    ns, seat, label = cards
    a_key, a = _card(label, "Ruling | hold", "2026-10-08T07:00:00.000Z")
    b_key, b = _card(label, f"Candidate | no, deploy. {SECRET}", "2026-10-08T08:00:00.000Z", card_type="Candidate",
                     supersedes=[a_key])
    c_key, c = _card(label, "Candidate | deploy, after the backup", "2026-10-08T09:00:00.000Z", card_type="Candidate",
                     supersedes=[b_key])
    for key, value in ((a_key, a), (b_key, b), (c_key, c)):
        _write(app_conn, seat, ns, key, value)
    seat_start = ("Ruling", "Constraint", "Commitment", "OpenItem")
    assert list(_listed(owner_conn, ns, types=seat_start)) == [c_key]         # control: before the redaction
    _redact(app_conn, seat, ns, b_key)
    after = _listed(owner_conn, ns, types=seat_start)
    assert list(after) == [c_key], "the readable correction still stands in for the Ruling"
    assert after[c_key]["replaces_requested"] is True
    assert list(_listed(owner_conn, ns)) == [c_key]
    assert store.replacers(owner_conn, ns, ("Ruling",)) == {b_key, c_key}
    owner_conn.commit()


def test_redacting_the_ruling_keeps_its_typed_replacement_in_the_seat_start_read(app_conn, owner_conn, cards):
    """memory-bxxxxx on #170, finding 2. Ruling A, a typed B over A; A is redacted. The tombstone keeps
    `type` and `time`, so B still replaces a Ruling."""
    ns, seat, label = cards
    a_key, a = _card(label, f"Ruling | hold. {SECRET}", "2026-10-08T07:00:00.000Z")
    b_key, b = _card(label, "Candidate | no, deploy", "2026-10-08T08:00:00.000Z", card_type="Candidate",
                     supersedes=[a_key])
    _write(app_conn, seat, ns, a_key, a)
    _write(app_conn, seat, ns, b_key, b)
    assert list(_listed(owner_conn, ns, types=("Ruling",))) == [b_key]        # control
    _redact(app_conn, seat, ns, a_key)
    assert list(_listed(owner_conn, ns, types=("Ruling",))) == [b_key]
    assert list(_listed(owner_conn, ns, status="any")) == [b_key], "the redacted Ruling itself is never listed"
    owner_conn.commit()


def test_a_tombstone_is_a_target_on_the_read_side_and_never_on_the_write_side(app_conn, owner_conn, cards):
    """Both halves pinned, because they differ on purpose. READ: an edge to a tombstone stays live, so
    what stood on a redacted card keeps its place. WRITE: a new card may not name a tombstone."""
    ns, seat, label = cards
    a_key, a = _card(label, "Ruling | a", "2026-10-08T07:00:00.000Z")
    b_key, b = _card(label, "Ruling | b", "2026-10-08T08:00:00.000Z", supersedes=[a_key])
    _write(app_conn, seat, ns, a_key, a)
    _write(app_conn, seat, ns, b_key, b)
    _redact(app_conn, seat, ns, a_key)
    assert store.superseders(owner_conn, ns) == {a_key: [b_key]}, "the edge to the tombstone is still read"
    owner_conn.commit()
    c_key, c = _card(label, "Ruling | c", "2026-10-08T09:00:00.000Z", supersedes=[a_key])
    with pytest.raises(HandlerError) as exc:
        _write(app_conn, seat, ns, c_key, c)
    assert exc.value.code == "E_CARD_SUPERSEDES"
