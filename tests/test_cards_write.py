"""Divergence 091: what memory.write refuses in the decision-card namespace (apps/cards/validation.py).

Every refusal has its control beside it: the same card with the one fault removed is written. A test
that only showed refusals would pass on a namespace that refused everything.

Run:  .venv/Scripts/python.exe -m pytest tests/test_cards_write.py -q -p no:cacheprovider
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from tests.test_memory_skills import (  # noqa: F401 - fixtures are used by name; sets up Django
    _call,
    _claim,
    _peer,
    app_conn,
    owner_conn,
)
from apps.cards import validation as V  # noqa: E402
from apps.memoryskills import handlers  # noqa: E402
from apps.memoryskills.errors import HandlerError  # noqa: E402


def _label(peer) -> str:
    """The ONE label the server stamps for a peer with no roster entry (handlers.stamped_label)."""
    return f"peer-{str(peer['peer_id'])[:8]}"


def _card(seat: str, *, text="Ruling | owner: operator | seat: s | deploy now", time="2026-09-30T07:13:17.834Z",
          card_type="Ruling", card_id=None, first_hand=True, source=None, **more) -> tuple[str, dict]:
    card_id = card_id or uuid.uuid4().hex
    value = {"schema": "card.v1", "id": card_id, "type": card_type, "owner": "operator", "seat": seat,
             "time": time, "text": text, "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
             "source": source or {"record_uuid": str(uuid.uuid4()), "block_index": 0, "kind": "answered_question",
                                  "first_hand": first_hand}}
    value.update(more)
    return f"card/{seat}/{card_id}", value


def _again(value, **changes) -> dict:
    """A later version of the same card: the same id, time, type, seat and source unless changed."""
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in value.items()}
    out.update(changes)
    if "text" in changes:
        out["text_sha256"] = hashlib.sha256(changes["text"].encode()).hexdigest()
    return out


@pytest.fixture()
def cards(app_conn, owner_conn, monkeypatch):
    """A claimed card namespace, a seat that may write it, and that seat's label. The namespace is
    named through the environment half of the setting: this module runs the handlers without Django
    configured, as tests/test_memory_skills.py does."""
    ns = f"shared/cards-{uuid.uuid4().hex[:12]}"
    group = uuid.uuid4()      # one trust group, so a grant to a second seat needs no expiry
    seat = {**_peer(owner_conn, "seat", trust_group=group), "group": group}
    _claim(app_conn, seat, ns)
    monkeypatch.setenv("MESHY_CARDS_NAMESPACE", ns)
    monkeypatch.setattr(handlers, "_ROSTER_LABELS", {})
    assert V.namespace() == ns
    return ns, seat, _label(seat)


def _write(conn, peer, ns, key, value):
    return _call(conn, peer, "memory.write", {"namespace": ns, "key": key, "value": value})


def _refused(conn, peer, ns, key, value) -> str:
    with pytest.raises(HandlerError) as exc:
        _write(conn, peer, ns, key, value)
    return exc.value.code


def _accepted(conn, peer, ns, key, value) -> int:
    """The version a write produced. A refusal fails HERE, on an assertion that names its code, so a
    test that expects a write never fails only because the handler raised."""
    try:
        return _write(conn, peer, ns, key, value)["version"]
    except HandlerError as exc:
        assert False, f"expected the write to be accepted; refused with {exc.code}: {exc.message}"


def _second_writer(app_conn, owner_conn, ns, holder, tag) -> dict:
    """Another seat with WRITE on the namespace, as every seat has on the real one."""
    peer = _peer(owner_conn, tag, trust_group=holder["group"])
    _call(app_conn, holder, "memory.grant", {"namespace": ns, "grantee_peer_id": str(peer["peer_id"]),
                                             "permission": "write"})
    return peer


def _heads(conn, ns) -> int:
    return conn.execute("SELECT count(*) FROM shared_memory WHERE namespace = %s AND is_head", (ns,)).fetchone()[0]


def _head_value(conn, ns, key):
    return conn.execute("SELECT value FROM shared_memory WHERE namespace = %s AND key = %s AND is_head",
                        (ns, key)).fetchone()[0]


def _redact(conn, ns, key):
    """What memory.redact leaves of a card: no `text`, no `supersedes`."""
    # The immutability trigger only, never USER: the card head record's trigger (sql_meshychat/057) must
    # still see this write, as it sees every real one, or the record keeps the old value.
    conn.execute("ALTER TABLE shared_memory DISABLE TRIGGER shared_memory_immutability")
    conn.execute("UPDATE shared_memory SET value = %s::jsonb WHERE namespace = %s AND key = %s",
                 ('{"redacted": true, "reason_code": "credential"}', ns, key))
    conn.execute("ALTER TABLE shared_memory ENABLE TRIGGER shared_memory_immutability")
    conn.commit()


# --------------------------------------------------------------------------- the card itself

def test_a_well_formed_card_is_written_and_reads_back(app_conn, owner_conn, cards):
    ns, seat, label = cards
    key, value = _card(label, question="Deploy or hold?", options=["Deploy", "Hold"], answer="Deploy",
                       thread_id="prod-rollout-ea0a130")
    assert _write(app_conn, seat, ns, key, value)["version"] == 1
    assert _head_value(owner_conn, ns, key) == value


def test_work_in_progress_and_an_open_question_are_cards(app_conn, owner_conn, cards):
    """The two types that stop two seats doing one thing: a Commitment (who is building what, in which
    thread, done when) and a Question (asked of the operator, not yet answered)."""
    ns, seat, label = cards
    key, value = _card(label, card_type="Commitment", text="Commitment | the card server PR",
                       thread_id="transcripts-to-mesh-graph-20260930", done_when="PR merged and deployed")
    assert _write(app_conn, seat, ns, key, value)["version"] == 1
    key, value = _card(label, card_type="Question", text="Question | deploy now or hold for the fix?",
                       question="Deploy now or hold?", options=["Deploy now", "Hold"])
    assert _write(app_conn, seat, ns, key, value)["version"] == 1
    for bad in ({"thread_id": "has a space"}, {"thread_id": 7}, {"done_when": "  "}, {"done_when": "x" * 501}):
        key, value = _card(label, card_type="Commitment", **bad)
        assert _refused(app_conn, seat, ns, key, value) == "E_CARD_SCHEMA", bad


def test_the_rules_bind_the_card_namespace_only(app_conn, owner_conn, cards):
    """Control for every refusal below: the same malformed value is an ordinary row elsewhere."""
    ns, seat, label = cards
    other = f"memtest/other-{uuid.uuid4().hex[:10]}"
    _claim(app_conn, seat, other)
    assert _write(app_conn, seat, other, "note/x", {"body": "anything"})["version"] == 1
    assert _refused(app_conn, seat, ns, "note/x", {"body": "anything"}) == "E_CARD_KEY"
    assert _heads(owner_conn, ns) == 0


@pytest.mark.parametrize("fault, code", [
    (lambda v: v.pop("text_sha256"), "E_CARD_SCHEMA"),                        # a required field
    (lambda v: v.update(surprise=1), "E_CARD_SCHEMA"),                        # an unknown field
    (lambda v: v.update(schema="card.v2"), "E_CARD_SCHEMA"),
    (lambda v: v.update(type="Opinion"), "E_CARD_SCHEMA"),
    (lambda v: v.update(text="   "), "E_CARD_SCHEMA"),
    (lambda v: v.update(text="x" * 1501, text_sha256=hashlib.sha256(b"x" * 1501).hexdigest()), "E_CARD_SCHEMA"),
    (lambda v: v.update(text=v["text"] + " (edited)"), "E_CARD_DIGEST"),      # the digest no longer matches
    (lambda v: v.update(status="superseded"), "E_CARD_SCHEMA"),               # superseded is derived
    (lambda v: v.update(id="0" * 32), "E_CARD_KEY"),                          # not the key's id
    (lambda v: v["source"].pop("record_uuid"), "E_CARD_SCHEMA"),
    (lambda v: v["source"].update(block_index=True), "E_CARD_SCHEMA"),        # bool is not an index
    (lambda v: v.update(free_text="yes"), "E_CARD_SCHEMA"),
    (lambda v: v.update(notes="n" * 9_000), "E_CARD_SCHEMA"),                 # over the size bound
], ids=["missing", "unknown", "schema", "type", "blank-text", "long-text", "digest", "stored-superseded",
        "id", "source-missing", "source-bool", "bool-field", "size"])
def test_a_malformed_card_is_refused_and_nothing_is_written(app_conn, owner_conn, cards, fault, code):
    ns, seat, label = cards
    key, value = _card(label)
    fault(value)
    assert _refused(app_conn, seat, ns, key, value) == code
    assert _heads(owner_conn, ns) == 0
    key2, good = _card(label)                                                 # control: the namespace takes a good card
    assert _write(app_conn, seat, ns, key2, good)["version"] == 1


@pytest.mark.parametrize("field", sorted(V.FORBIDDEN_PROSE_FIELDS))
def test_a_second_prose_field_is_refused_as_one_the_walker_would_embed(app_conn, cards, field):
    """Refused by ITS rule, not only by the allowlist: the message says why, so a later change that adds
    such a field to the allowlist still meets this refusal."""
    ns, seat, label = cards
    key, value = _card(label)
    value[field] = "a longer field the walker would embed in place of text " * 5
    with pytest.raises(HandlerError) as exc:
        _write(app_conn, seat, ns, key, value)
    assert exc.value.code == "E_CARD_SCHEMA" and "embedded in its place" in exc.value.message


@pytest.mark.parametrize("time", [
    "2026-09-30T07:13:17Z",            # no milliseconds: sorts AFTER 07:37:17.834Z as a string
    "2026-09-30T07:13:17.8Z",          # one digit: a real instant, the wrong width
    "2026-09-30T07:13:17.834567Z",     # microseconds: a real instant, the wrong width
    "2026-09-30T07:13:17.834+00:00",   # an offset spelling
    "2026-09-30T09:13:17.834+02:00",
    "2026-09-30 07:13:17.834Z",
    "2026-13-08T07:37:17.834Z",        # the shape, not a real instant
    20260930,
])
def test_a_time_in_any_other_spelling_is_refused(app_conn, owner_conn, cards, time):
    ns, seat, label = cards
    key, value = _card(label, time=time)
    assert _refused(app_conn, seat, ns, key, value) == "E_CARD_TIME"
    assert _heads(owner_conn, ns) == 0


def test_a_card_dated_ahead_of_the_clock_is_refused_and_one_inside_the_skew_is_not(app_conn, cards):
    ns, seat, label = cards
    now = datetime.now(timezone.utc)

    def stamp(delta):
        return (now + delta).strftime("%Y-%m-%dT%H:%M:%S.") + f"{(now + delta).microsecond // 1000:03d}Z"
    key, value = _card(label, time=stamp(timedelta(hours=1)))
    assert _refused(app_conn, seat, ns, key, value) == "E_CARD_TIME"
    key, value = _card(label, time=stamp(timedelta(minutes=2)))
    assert _write(app_conn, seat, ns, key, value)["version"] == 1


@pytest.mark.parametrize("card_type, version, ok", [
    ("Ruling", "1.7", True), ("Ruling", "1.10", True), ("Ruling", "2.0", True),
    ("Ruling", "1.6", False), ("Ruling", "1.4", False),      # keyed on the transcript record, not the call
    ("Candidate", "1.4", True), ("Candidate", "1.10", True),
    ("Candidate", "1.3", False), ("Candidate", "1.2", False),  # the id held a hash of the kept fields
    ("Candidate", "1.7.1", False), ("Candidate", "v1.7", False),
])
def test_a_card_from_a_loader_whose_ids_were_not_stable_is_refused(app_conn, cards, card_type, version, ok):
    """Compared as numbers: as text "1.10" sorts below "1.4", and the newest loader would be refused."""
    ns, seat, label = cards
    key, value = _card(label, card_type=card_type, loader_version=version)
    if ok:
        assert _accepted(app_conn, seat, ns, key, value) == 1
    else:
        shaped = version.count(".") == 1 and version.replace(".", "").isdigit()
        assert _refused(app_conn, seat, ns, key, value) == ("E_CARD_LOADER" if shaped else "E_CARD_SCHEMA")
    key, value = _card(label, card_type=card_type)                            # control: no version, written
    assert _accepted(app_conn, seat, ns, key, value) == 1


# --------------------------------------------------------------------------- whose card it is

def test_a_seat_cannot_write_a_card_as_another_seat_or_under_its_key(app_conn, owner_conn, cards):
    """Three forgeries, each refused. B's key with A's name inside; A's key and A's name, signed by B;
    and A's KEY with B's own name inside, which would put a card of B's in A's key space."""
    ns, seat_a, label_a = cards
    seat_b = _second_writer(app_conn, owner_conn, ns, seat_a, "other")
    label_b = _label(seat_b)
    key_b, as_a = _card(label_b)
    as_a["seat"] = label_a
    assert _refused(app_conn, seat_b, ns, key_b, as_a) == "E_CARD_SEAT_MISMATCH"
    key_a, whole = _card(label_a)
    assert _refused(app_conn, seat_b, ns, key_a, whole) == "E_CARD_SEAT_MISMATCH"
    key_a2, own_name = _card(label_a)
    own_name["seat"] = label_b
    assert _refused(app_conn, seat_b, ns, key_a2, own_name) == "E_CARD_SEAT_MISMATCH"
    assert _heads(owner_conn, ns) == 0
    key, own = _card(label_b)                                                 # control: B writes as B
    assert _write(app_conn, seat_b, ns, key, own)["version"] == 1


def test_the_seat_is_the_one_stamped_label_not_any_label_the_caller_answers_to(app_conn, owner_conn, cards,
                                                                               monkeypatch):
    """A peer answers to its roster label, its peers.name and peer-<id8>. A card under each would be one
    record under three keys, and superseding one would leave two active. With a roster label, that label
    is the seat and the other two are refused; without one, peer-<id8> is."""
    ns, seat, fallback = cards
    name = owner_conn.execute("SELECT name FROM peers WHERE id = %s", (seat["peer_id"],)).fetchone()[0]
    key, value = _card(name)                                                   # peers.name: one of its labels
    assert _refused(app_conn, seat, ns, key, value) == "E_CARD_SEAT_MISMATCH"
    monkeypatch.setattr(handlers, "_ROSTER_LABELS", {str(seat["peer_id"]): "rostered-seat"})
    key, value = _card(fallback)
    assert _refused(app_conn, seat, ns, key, value) == "E_CARD_SEAT_MISMATCH"   # no longer the stamped label
    key, value = _card("rostered-seat")
    assert _write(app_conn, seat, ns, key, value)["version"] == 1              # control


def test_a_later_version_of_a_card_is_its_first_writers(app_conn, owner_conn, cards, monkeypatch):
    """The roster gives seat A's label to another peer (a re-identified node, or an error). That peer
    now passes the label rule, and still cannot replace A's card."""
    ns, seat_a, _ = cards
    monkeypatch.setattr(handlers, "_ROSTER_LABELS", {str(seat_a["peer_id"]): "seat-a"})
    key, value = _card("seat-a")
    _write(app_conn, seat_a, ns, key, value)
    seat_b = _second_writer(app_conn, owner_conn, ns, seat_a, "squat")
    monkeypatch.setattr(handlers, "_ROSTER_LABELS", {str(seat_b["peer_id"]): "seat-a"})
    assert _refused(app_conn, seat_b, ns, key, _again(value, text="Ruling | a different answer")) == "E_CARD_NOT_AUTHOR"
    assert _head_value(owner_conn, ns, key)["text"] == value["text"]
    monkeypatch.setattr(handlers, "_ROSTER_LABELS", {str(seat_a["peer_id"]): "seat-a"})
    tightened = _again(value, text="Ruling | deploy now [one line removed]")
    assert _write(app_conn, seat_a, ns, key, tightened)["version"] == 2       # control: the author's own


@pytest.mark.parametrize("change, name", [
    ({"time": "2026-09-30T08:36:00.000Z"}, "time"),
    ({"type": "Claim"}, "type"),
    ({"source": {"record_uuid": "another-record", "block_index": 0, "kind": "answered_question",
                 "first_hand": True}}, "source.record_uuid"),
    ({"source": {"record_uuid": "fixed", "block_index": 3, "kind": "answered_question",
                 "first_hand": True}}, "source.block_index"),
    ({"source": {"record_uuid": "fixed", "block_index": 0, "kind": "answered_question",
                 "first_hand": False}}, "source.first_hand"),
    ({"source": {"record_uuid": "fixed", "block_index": 0, "kind": "typed_prompt",
                 "first_hand": True}}, "source.kind"),
])
def test_a_later_version_keeps_the_fields_every_order_rests_on(app_conn, owner_conn, cards, change, name):
    """`time` decides which card may supersede which, and `type` what may supersede it. Checked once at
    the write, they must not move afterwards: a card whose time moved LATER could name a card that
    names it."""
    ns, seat, label = cards
    key, value = _card(label, source={"record_uuid": "fixed", "block_index": 0, "kind": "answered_question",
                                      "first_hand": True})
    _write(app_conn, seat, ns, key, value)
    with pytest.raises(HandlerError) as exc:
        _write(app_conn, seat, ns, key, _again(value, **change))
    assert exc.value.code == "E_CARD_FROZEN" and name in exc.value.message
    assert _write(app_conn, seat, ns, key, _again(value, text="Ruling | reworded"))["version"] == 2   # control


@pytest.mark.parametrize("first, then", [("operator", "seat"), ("seat", "operator")])
def test_a_later_version_keeps_who_decided(app_conn, owner_conn, cards, first, then):
    """`owner` says who decided, the operator or the seat itself, and a card is read as a fact once written. A later
    version that moved the decision between them would rewrite it without a new card (#253), so owner is frozen as
    time and type are. Both directions are refused; the control rewords the text with the owner unchanged."""
    ns, seat, label = cards
    who = {"operator": "operator", "seat": label}
    key, value = _card(label, card_type="Candidate", text="Candidate | the plan is to deploy at noon")
    value = _again(value, owner=who[first])
    _write(app_conn, seat, ns, key, value)
    with pytest.raises(HandlerError) as exc:
        _write(app_conn, seat, ns, key, _again(value, owner=who[then]))
    assert exc.value.code == "E_CARD_FROZEN" and "owner" in exc.value.message, exc.value.message
    assert _write(app_conn, seat, ns, key, _again(value, text="Candidate | reworded"))["version"] == 2   # control


def test_a_redacted_card_is_not_written_again(app_conn, owner_conn, cards):
    """The loader's next rebuild would otherwise write the redacted text back under the same key."""
    ns, seat, label = cards
    key, value = _card(label)
    _write(app_conn, seat, ns, key, value)
    _redact(owner_conn, ns, key)
    assert _refused(app_conn, seat, ns, key, _again(value)) == "E_CARD_REDACTED"
    assert "text" not in _head_value(owner_conn, ns, key)
    key2, other = _card(label)                                                # control: another key is written
    assert _write(app_conn, seat, ns, key2, other)["version"] == 1


def test_a_first_hand_ruling_cannot_turn_relayed_and_keep_what_it_superseded(app_conn, owner_conn, cards):
    """Both Codex reviews of #169: edges are checked when ADDED, so a recorded decision Ruling that named another
    seat's card and then became relayed in version 2 kept an edge no relayed card may hold."""
    ns, seat_a, label_a = cards
    seat_b = _second_writer(app_conn, owner_conn, ns, seat_a, "flip")
    label_b = _label(seat_b)
    a_key, a = _card(label_a, time="2026-09-30T06:36:00.000Z", text="Ruling | a")
    _write(app_conn, seat_a, ns, a_key, a)
    b_key, b = _card(label_b, time="2026-09-30T07:36:00.000Z", text="Ruling | b", supersedes=[a_key])
    _write(app_conn, seat_b, ns, b_key, b)
    relayed = _again(b, source=dict(b["source"], first_hand=False))
    assert _refused(app_conn, seat_b, ns, b_key, relayed) == "E_CARD_FROZEN"
    assert _head_value(owner_conn, ns, b_key)["source"]["first_hand"] is True
    assert _accepted(app_conn, seat_b, ns, b_key, _again(b, text="Ruling | b, reworded")) == 2   # control


@pytest.mark.parametrize("owner_value, ok", [("operator", True), ("SEAT", True), ("another-seat", False),
                                             ("", False), ("Operator", False), (7, False)])
def test_the_owner_is_the_operator_or_the_cards_own_seat(app_conn, cards, owner_value, ok):
    """dxxxxx on #169: any string was accepted, so a card could put a decision in another seat's mouth.
    "operator" is the writing seat's own attestation, which the signature attributes to it."""
    ns, seat, label = cards
    key, value = _card(label, card_type="Decision")
    value["owner"] = label if owner_value == "SEAT" else owner_value
    if ok:
        assert _accepted(app_conn, seat, ns, key, value) == 1
    else:
        assert _refused(app_conn, seat, ns, key, value) == "E_CARD_SCHEMA"


def test_prune_never_compacts_the_card_namespace_whatever_its_policy(app_conn, owner_conn, monkeypatch):
    """dxxxxx on #169. The namespace is claimed keep_all; this is what holds if it is ever claimed
    high_churn, where prune would delete the versions that record who changed a supersession."""
    ns = f"shared/cards-{uuid.uuid4().hex[:12]}"
    other = f"shared/other-{uuid.uuid4().hex[:12]}"
    seat = _peer(owner_conn, "pruner")
    _claim(app_conn, seat, ns, retention_policy="high_churn")
    _claim(app_conn, seat, other, retention_policy="high_churn")
    monkeypatch.setenv("MESHY_CARDS_NAMESPACE", ns)
    monkeypatch.setattr(handlers, "_ROSTER_LABELS", {})
    key, value = _card(_label(seat))
    _write(app_conn, seat, ns, key, value)
    _write(app_conn, seat, ns, key, _again(value, text="Ruling | version two"))
    with pytest.raises(HandlerError) as exc:
        _call(app_conn, seat, "memory.prune", {"namespace": ns})
    assert exc.value.code == "E_BAD_REQUEST" and "card namespace" in exc.value.message
    assert owner_conn.execute("SELECT count(*) FROM shared_memory WHERE namespace = %s", (ns,)).fetchone()[0] == 2
    _call(app_conn, seat, "memory.write", {"namespace": other, "key": "k", "value": {"x": 1}})
    _call(app_conn, seat, "memory.write", {"namespace": other, "key": "k", "value": {"x": 2}})
    _call(app_conn, seat, "memory.prune", {"namespace": other})                # control: elsewhere it compacts
    assert owner_conn.execute("SELECT count(*) FROM shared_memory WHERE namespace = %s", (other,)).fetchone()[0] == 1


def test_a_card_has_one_author_and_one_status_channel(app_conn, owner_conn, cards):
    """nxxxxx's swarm on the rules. memory.write takes an `author` and a row `status` for any row. On a
    card, a supplied author made it read as its seat's in /v1/cards and as someone else's to a generic
    reader of the row; and a row status (WONTFIX, say) was a second status that memory.query honoured
    while /v1/cards derived another."""
    ns, seat, label = cards
    key, value = _card(label)
    for extra, code in (({"author": "kxxx"}, "E_CARD_SEAT_MISMATCH"), ({"author_label": "kxxx"}, "E_CARD_SEAT_MISMATCH"),
                        ({"status": "OPEN"}, "E_CARD_SCHEMA"), ({"status": "WONTFIX"}, "E_CARD_SCHEMA")):
        with pytest.raises(HandlerError) as exc:
            _call(app_conn, seat, "memory.write", {"namespace": ns, "key": key, "value": value, **extra})
        assert exc.value.code == code, extra
    assert _heads(owner_conn, ns) == 0
    _call(app_conn, seat, "memory.write", {"namespace": ns, "key": key, "value": value, "author": label})   # control
    row = owner_conn.execute("SELECT author_label, status FROM shared_memory WHERE namespace = %s AND key = %s",
                             (ns, key)).fetchone()
    assert tuple(row) == (label, None)


# --------------------------------------------------------------------------- supersedes

def test_supersedes_names_only_earlier_head_cards(app_conn, owner_conn, cards):
    ns, seat, label = cards
    old_key, old = _card(label, time="2026-09-30T07:11:56.780Z", text="Ruling | deploy now, clean later")
    _write(app_conn, seat, ns, old_key, old)

    def attempt(**more):
        return _card(label, time="2026-09-30T07:13:17.834Z", text=f"Ruling | {uuid.uuid4().hex}", **more)

    key, value = attempt(supersedes=[f"card/{label}/{'f' * 32}"])              # no such card
    assert _refused(app_conn, seat, ns, key, value) == "E_CARD_SUPERSEDES"
    key, value = attempt(supersedes=["msg/1790752419314-kxxx-2d49cf"])         # not a card key
    assert _refused(app_conn, seat, ns, key, value) == "E_CARD_SUPERSEDES"
    key, value = attempt(supersedes=[old_key, old_key])                        # repeated
    assert _refused(app_conn, seat, ns, key, value) == "E_CARD_SUPERSEDES"
    key, value = attempt(supersedes=[f"card/{label}/{i:032x}" for i in range(17)])   # over the bound
    assert _refused(app_conn, seat, ns, key, value) == "E_CARD_SUPERSEDES"
    key, value = _card(label, time="2026-09-30T07:11:56.780Z", supersedes=[old_key])   # the SAME instant
    assert _refused(app_conn, seat, ns, key, value) == "E_CARD_SUPERSEDES"
    key, value = _card(label, time="2026-09-30T06:36:00.000Z", supersedes=[old_key])   # earlier than its target
    assert _refused(app_conn, seat, ns, key, value) == "E_CARD_SUPERSEDES"
    assert _heads(owner_conn, ns) == 1

    key, value = attempt(supersedes=[old_key])                                 # control
    assert _write(app_conn, seat, ns, key, value)["version"] == 1


def test_a_card_may_name_sixteen_targets_and_not_seventeen(app_conn, owner_conn, cards):
    """memory-bxxxxx on #169: the bound was tested only on the refusing side, with keys that did not exist."""
    from apps.cards import store
    ns, seat, label = cards
    targets = []
    for i in range(17):
        key, value = _card(label, time=f"2026-09-30T06:36:{i:02d}.000Z", text=f"Ruling | target {i}")
        _write(app_conn, seat, ns, key, value)
        targets.append(key)
    key, value = _card(label, time="2026-09-30T08:36:00.000Z", text="Ruling | names seventeen", supersedes=targets)
    assert _refused(app_conn, seat, ns, key, value) == "E_CARD_SUPERSEDES"
    key, value = _card(label, time="2026-09-30T08:36:00.000Z", text="Ruling | names sixteen", supersedes=targets[:16])
    assert _accepted(app_conn, seat, ns, key, value) == 1
    assert store.superseders(owner_conn, ns) == {t: [key] for t in targets[:16]}
    owner_conn.commit()


def test_superseded_follows_the_head_versions_of_both_cards(app_conn, owner_conn, cards):
    """memory-bxxxxx on #169, two gaps. The TARGET has two versions: it is superseded once, by one card,
    not once per version. Then the NAMING card's later version clears its list: the target is active
    again, because only a head names anything."""
    from apps.cards import store
    ns, seat, label = cards
    a_key, a = _card(label, time="2026-09-30T06:36:00.000Z", text="Ruling | a")
    _write(app_conn, seat, ns, a_key, a)
    _write(app_conn, seat, ns, a_key, _again(a, text="Ruling | a, reworded"))               # the target, version 2
    b_key, b = _card(label, time="2026-09-30T07:36:00.000Z", text="Ruling | b", supersedes=[a_key])
    _write(app_conn, seat, ns, b_key, b)
    assert store.superseders(owner_conn, ns) == {a_key: [b_key]}, "one edge, to the head of a two-version target"
    owner_conn.commit()
    assert _accepted(app_conn, seat, ns, b_key, _again(b, text="Ruling | b, withdrawn", supersedes=[])) == 2
    assert store.superseders(owner_conn, ns) == {}, "version 1 of b still holds the list, and is not a head"
    owner_conn.commit()


def test_seat_effect_is_a_string_field_kept_apart_from_the_answer(monkeypatch):
    """approvals_cards.py keeps a seat's reading of the operator's answer in `seat_effect`, never in `answer`
    (<thread>, kxxx 2a0503). The pure rule: a string passes, anything else is refused, and the
    field is not prose the walker would embed (`text` stays the card's only prose)."""
    monkeypatch.setenv("MESHY_CARDS_NAMESPACE", "shared/cards-pure")
    now = datetime(2026, 9, 30, 23, 36, tzinfo=timezone.utc)
    key, value = _card("seat-x", answer="Verbatim answer: \"Keep 1 hour\"", seat_effect="The install keeps 60 min.")
    assert V.validate_card("shared/cards-pure", key, value, now=now) is True
    assert "seat_effect" not in V.FORBIDDEN_PROSE_FIELDS
    key, value = _card("seat-x", seat_effect=["not", "a string"])
    with pytest.raises(HandlerError) as exc:
        V.validate_card("shared/cards-pure", key, value, now=now)
    assert "seat_effect" in exc.value.message


def test_a_card_that_names_itself_is_refused_for_that_reason():
    """The pure rule, on its own: through the handler a self-reference also fails the target lookup, so
    only this test fails when the rule is removed."""
    key, value = _card("seat-x")
    value["supersedes"] = [key]
    with pytest.raises(HandlerError) as exc:
        V.supersedes_of(value, own_key=key)
    assert "cannot supersede itself" in exc.value.message
    assert V.supersedes_of({"supersedes": [key]}, own_key="card/seat-x/" + "0" * 32) == [key]   # control


def test_two_cards_cannot_supersede_each_other_by_any_order_of_writes(app_conn, owner_conn, cards):
    """memory-bxxxxx's cycle: A at 07:00, B at 08:00 naming A, then A version 2 moved to 09:00 naming
    B. The move is refused (the time is frozen); and with the time kept, B is not earlier than A."""
    ns, seat, label = cards
    a_key, a = _card(label, time="2026-09-30T06:36:00.000Z", text="Ruling | a")
    _write(app_conn, seat, ns, a_key, a)
    b_key, b = _card(label, time="2026-09-30T07:36:00.000Z", text="Ruling | b", supersedes=[a_key])
    _write(app_conn, seat, ns, b_key, b)
    moved = _again(a, time="2026-09-30T08:36:00.000Z", supersedes=[b_key])
    assert _refused(app_conn, seat, ns, a_key, moved) == "E_CARD_FROZEN"
    assert _refused(app_conn, seat, ns, a_key, _again(a, supersedes=[b_key])) == "E_CARD_SUPERSEDES"
    assert "supersedes" not in _head_value(owner_conn, ns, a_key)


def test_a_later_version_must_carry_its_supersedes_or_clear_it_explicitly(app_conn, owner_conn, cards):
    """The loader does not know what a card supersedes. Its next publish would write version 2 without
    the field, and every card it replaced would read as current again, silently."""
    ns, seat, label = cards
    a_key, a = _card(label, time="2026-09-30T06:36:00.000Z", text="Ruling | a")
    _write(app_conn, seat, ns, a_key, a)
    b_key, b = _card(label, time="2026-09-30T07:36:00.000Z", text="Ruling | b", supersedes=[a_key])
    _write(app_conn, seat, ns, b_key, b)
    dropped = _again(b, text="Ruling | b, republished")
    del dropped["supersedes"]
    assert _refused(app_conn, seat, ns, b_key, dropped) == "E_CARD_SUPERSEDES"
    assert _head_value(owner_conn, ns, b_key)["supersedes"] == [a_key]
    nulled = _again(b, text="Ruling | b, republished", supersedes=None)       # present and null is not a clearing
    assert _refused(app_conn, seat, ns, b_key, nulled) == "E_CARD_SUPERSEDES"
    assert _head_value(owner_conn, ns, b_key)["supersedes"] == [a_key]
    assert _write(app_conn, seat, ns, b_key, _again(b, text="Ruling | b, republished"))["version"] == 2   # carried
    assert _write(app_conn, seat, ns, b_key, _again(b, text="Ruling | b, withdrawn", supersedes=[]))["version"] == 3


def test_only_the_targets_a_version_adds_are_checked(app_conn, owner_conn, cards):
    """B supersedes A; A is then redacted. B must still be writable with A in its list (to tighten B's
    own text), or the redaction of A would lock B for ever. A NEW target that is not a card is refused."""
    ns, seat, label = cards
    a_key, a = _card(label, time="2026-09-30T06:36:00.000Z", text="Ruling | a")
    _write(app_conn, seat, ns, a_key, a)
    b_key, b = _card(label, time="2026-09-30T07:36:00.000Z", text="Ruling | b", supersedes=[a_key])
    _write(app_conn, seat, ns, b_key, b)
    _redact(owner_conn, ns, a_key)
    assert _accepted(app_conn, seat, ns, b_key, _again(b, text="Ruling | b, tightened")) == 2
    c_key, c = _card(label, time="2026-09-30T08:36:00.000Z", text="Ruling | c", supersedes=[a_key])
    assert _refused(app_conn, seat, ns, c_key, c) == "E_CARD_SUPERSEDES"      # a NEW edge to a tombstone


def test_a_claim_supersedes_only_a_claim(app_conn, cards):
    """The record's rule 4: a card a model wrote never replaces one taken from the operator's lines."""
    ns, seat, label = cards
    ruling_key, ruling = _card(label, time="2026-09-30T06:36:00.000Z", text="Ruling | first-hand")
    claim_key, claim = _card(label, time="2026-09-30T06:36:00.000Z", text="Claim | a summary", card_type="Claim",
                             first_hand=False)
    _write(app_conn, seat, ns, ruling_key, ruling)
    _write(app_conn, seat, ns, claim_key, claim)
    key, value = _card(label, time="2026-09-30T07:36:00.000Z", text="Claim | newer summary", card_type="Claim",
                       supersedes=[ruling_key])
    assert _refused(app_conn, seat, ns, key, value) == "E_CARD_SUPERSEDES"
    key, value = _card(label, time="2026-09-30T07:36:00.000Z", text="Claim | newer summary", card_type="Claim",
                       first_hand=False, supersedes=[claim_key])
    assert _write(app_conn, seat, ns, key, value)["version"] == 1              # control: Claim over Claim
    key, value = _card(label, time="2026-09-30T08:36:00.000Z", text="Ruling | newer ruling", supersedes=[claim_key])
    assert _write(app_conn, seat, ns, key, value)["version"] == 1              # control: a Ruling may replace a Claim


def test_a_relayed_card_never_supersedes_a_first_hand_one(app_conn, cards):
    """The morning's case: a ruling relayed through another seat met one a recorded decision. A relayed
    Ruling (first_hand false) is a card, and it replaces nothing the operator said directly."""
    ns, seat, label = cards
    direct_key, direct = _card(label, time="2026-09-30T07:11:56.780Z", text="Ruling | deploy now")
    relayed_key, relayed = _card(label, time="2026-09-30T07:08:13.000Z", text="Ruling | hold, as relayed",
                                 first_hand=False)
    _write(app_conn, seat, ns, direct_key, direct)
    _write(app_conn, seat, ns, relayed_key, relayed)
    key, value = _card(label, time="2026-09-30T07:36:00.000Z", text="Ruling | hold, relayed again", first_hand=False,
                       supersedes=[direct_key])
    assert _refused(app_conn, seat, ns, key, value) == "E_CARD_SUPERSEDES"
    key, value = _card(label, time="2026-09-30T07:36:00.000Z", text="Ruling | hold, relayed again", first_hand=False,
                       supersedes=[relayed_key])
    assert _write(app_conn, seat, ns, key, value)["version"] == 1              # control: relayed over relayed
    key, value = _card(label, time="2026-09-30T08:06:00.000Z", text="Ruling | first-hand, final",
                       supersedes=[relayed_key])
    assert _write(app_conn, seat, ns, key, value)["version"] == 1              # control: on record over relayed


def test_another_seats_card_is_superseded_only_by_a_first_hand_ruling(app_conn, owner_conn, cards):
    """uxxx's hole: any seat could name another seat's Ruling and drop it from every seat's default
    read. Across seats, only the operator's own answer does that."""
    ns, seat_a, label_a = cards
    seat_b = _second_writer(app_conn, owner_conn, ns, seat_a, "pxx")
    label_b = _label(seat_b)
    theirs_key, theirs = _card(label_a, time="2026-09-30T07:08:13.000Z", text="Ruling | hold for the fix")
    _write(app_conn, seat_a, ns, theirs_key, theirs)
    for card_type in ("Candidate", "Decision", "Commitment"):
        key, value = _card(label_b, time="2026-09-30T07:36:00.000Z", text=f"{card_type} | mine", card_type=card_type,
                           supersedes=[theirs_key])
        assert _refused(app_conn, seat_b, ns, key, value) == "E_CARD_SUPERSEDES", card_type
    relayed_key, relayed = _card(label_a, time="2026-09-30T07:06:00.000Z", text="Ruling | relayed to A",
                                 first_hand=False)
    _write(app_conn, seat_a, ns, relayed_key, relayed)
    key, value = _card(label_b, time="2026-09-30T07:36:00.000Z", text="Ruling | relayed to B", first_hand=False,
                       supersedes=[relayed_key])                               # a Ruling, but a recorded decision
    assert _refused(app_conn, seat_b, ns, key, value) == "E_CARD_SUPERSEDES"
    key, value = _card(label_b, time="2026-09-30T07:13:17.834Z", text="Ruling | deploy ea0a130 now",
                       supersedes=[theirs_key])
    assert _write(app_conn, seat_b, ns, key, value)["version"] == 1            # control: a recorded decision Ruling
    own_key, own = _card(label_b, time="2026-09-30T05:36:00.000Z", text="Candidate | my own earlier note",
                         card_type="Candidate")
    _write(app_conn, seat_b, ns, own_key, own)
    key, value = _card(label_b, time="2026-09-30T08:36:00.000Z", text="Decision | replaces my own note",
                       card_type="Decision", supersedes=[own_key])
    assert _write(app_conn, seat_b, ns, key, value)["version"] == 1            # control: within one seat, any type


# --------------------------------------------------------------------------- a redaction cannot slip between check and insert

def test_a_redaction_that_lands_between_the_check_and_the_insert_does_not_leave_the_text(app_conn, owner_conn, cards,
                                                                                         monkeypatch):
    """My Codex review of #169: the head was read without a lock, so memory.redact could tombstone it
    after the card rules passed and before the next version was inserted, and that version carried the
    redacted text back as the new head.

    The race is forced, not waited for: the first thing memory.write does after the card rules is read
    the namespace's retention policy, and at that moment a second session redacts the key. With the
    head locked FOR UPDATE by the rules, the redaction waits for the write to commit and then redacts
    BOTH versions. Without the lock it commits at once, and version 2 is written over a tombstone."""
    import threading

    from apps.memoryskills import db as memdb
    from tests.test_memory_skills import DSN

    ns, seat, label = cards
    key, value = _card(label, text="Ruling | the text a redaction must remove")
    _write(app_conn, seat, ns, key, value)

    other = memdb.connect(DSN)
    state: dict = {}

    def redact_now():
        try:
            _call(other, seat, "memory.redact", {"namespace": ns, "key": key, "reason_code": "credential"})
            state["redacted"] = True
        except Exception as exc:  # judged below, never swallowed
            state["error"] = repr(exc)

    real = handlers._namespace_retention_policy
    redactor = threading.Thread(target=redact_now)

    def policy_then_race(conn, namespace):
        if namespace == ns and not redactor.is_alive() and "started" not in state:
            state["started"] = True
            redactor.start()
            redactor.join(timeout=1.5)          # long enough to commit if nothing makes it wait
            state["finished_inside_the_write"] = not redactor.is_alive()
        return real(conn, namespace)

    monkeypatch.setattr(handlers, "_namespace_retention_policy", policy_then_race)
    try:
        outcome = None
        try:
            outcome = _write(app_conn, seat, ns, key, _again(value, text="Ruling | the text, reworded"))["version"]
        except HandlerError as exc:
            outcome = exc.code
        redactor.join(timeout=20.0)
    finally:
        other.close()
    assert state.get("started") and not redactor.is_alive() and state.get("redacted") is True, (state, outcome)
    with_text = owner_conn.execute("SELECT count(*) FROM shared_memory WHERE namespace = %s AND key = %s "
                                   "AND value ? 'text'", (ns, key)).fetchone()[0]
    owner_conn.commit()
    assert with_text == 0, (f"{with_text} version(s) of a redacted card still hold its text; the write was "
                            f"{outcome!r} and the redaction finished inside it: {state.get('finished_inside_the_write')}")
    assert state["finished_inside_the_write"] is False, "the redaction did not wait for the write that held the head"


# --------------------------------------------------------------------------- the pure half

def test_the_forbidden_prose_fields_are_every_field_the_walker_could_embed_instead_of_text():
    """If the walker's content class grows, this fails until the card rule is read again: a new
    content-class field a card may carry would be embedded in place of `text` when it is longer."""
    from apps.meshsem import graphtext
    assert V.FORBIDDEN_PROSE_FIELDS == (set(graphtext.CONTENT_CLASS) - {"text"}) | {"title"}
    assert not (V.FORBIDDEN_PROSE_FIELDS & (V.REQUIRED_FIELDS | V.OPTIONAL_FIELDS))
    key, value = _card("seat-x")
    assert graphtext.row_text(value) == ("text", value["text"])               # what is embedded is the text, unprefixed


def test_validate_card_checks_nothing_outside_the_card_namespace():
    assert V.validate_card("shared/mesh-chat", "msg/1-x-y", {"anything": 1}) is False
    assert V.validate_card("shared/wiki", "card/x/" + "0" * 32, "not even an object") is False


# --------------------------------------------------------------------------- attribution (item 7, loader 1.19)

def _att(speaker="operator", quoted_author=None, extraction="claude_code/answered_question", confirmation="answered"):
    return {"speaker": speaker, "quoted_author": quoted_author, "extraction": extraction, "confirmation": confirmation}


def test_attribution_shape_is_refused_field_by_field_with_a_control(app_conn, cards):
    ns, seat, label = cards
    times = iter(f"2026-10-10T{h:02d}:00:00.000Z" for h in range(1, 20))
    key, value = _card(label, time=next(times), attribution=_att())
    assert _accepted(app_conn, seat, ns, key, value) == 1                                  # control
    bad_ones = (_att(speaker="seat"), _att(confirmation="promoted"), _att(confirmation=None),
                _att(quoted_author="Not A Label!"), _att(extraction="Claude Code/answered"),
                {**_att(), "extra": 1}, {k: v for k, v in _att().items() if k != "quoted_author"}, "operator")
    for bad in bad_ones:
        key, value = _card(label, time=next(times), attribution=bad)
        assert _refused(app_conn, seat, ns, key, value) == "E_CARD_SCHEMA", bad
    for good_qa in ("unknown", "txxxx"):                                                   # controls: not null
        key, value = _card(label, time=next(times), first_hand=False, attribution=_att(quoted_author=good_qa))
        assert _accepted(app_conn, seat, ns, key, value) == 1, good_qa
    # "promoted" on a Candidate, which has no type rule: only the confirmation set can refuse it (a Ruling would be
    # refused by "a Ruling is answered" too, and the same code would hide a missing check).
    cand = dict(card_type="Candidate", text="Candidate | maybe ship")
    key, value = _card(label, time=next(times), attribution=_att(confirmation="promoted"), **cand)
    assert _refused(app_conn, seat, ns, key, value) == "E_CARD_SCHEMA"
    key, value = _card(label, time=next(times), attribution=_att(confirmation="typed"), **cand)
    assert _accepted(app_conn, seat, ns, key, value) == 1                                  # control


def test_attribution_must_agree_with_the_card_type(app_conn, cards):
    ns, seat, label = cards
    key, value = _card(label, time="2026-10-02T00:36:00.000Z", attribution=_att(confirmation="typed"))
    assert _refused(app_conn, seat, ns, key, value) == "E_CARD_SCHEMA"                     # a Ruling is answered
    q = dict(card_type="Question", text="Question | ship it?", first_hand=False)
    # first_hand True here, so a recorded decision agreement holds and only "a Question is the agent's" can refuse.
    key, value = _card(label, time="2026-10-02T01:36:00.000Z", card_type="Question", text="Question | ship it?",
                       first_hand=True, attribution=_att(speaker="operator", confirmation="asked"))
    assert _refused(app_conn, seat, ns, key, value) == "E_CARD_SCHEMA"                     # a Question is the agent's
    key, value = _card(label, time="2026-10-02T02:36:00.000Z",
                       attribution=_att(speaker="agent", confirmation="asked"), **q)
    assert _accepted(app_conn, seat, ns, key, value) == 1                                  # control
    # txxxx's two (review of #232). A Ruling by the agent: first_hand False, so a recorded decision agreement holds and only
    # the Ruling rule can refuse it.
    key, value = _card(label, time="2026-10-02T03:36:00.000Z", first_hand=False,
                       attribution=_att(speaker="agent", confirmation="answered"))
    assert _refused(app_conn, seat, ns, key, value) == "E_CARD_SCHEMA"
    # "asked" on a Candidate (no type rule of its own): only the asked-is-a-Question's rule can refuse it.
    cand = dict(card_type="Candidate", text="Candidate | ship?")
    key, value = _card(label, time="2026-10-02T04:36:00.000Z", attribution=_att(confirmation="asked"), **cand)
    assert _refused(app_conn, seat, ns, key, value) == "E_CARD_SCHEMA"
    key, value = _card(label, time="2026-10-02T05:36:00.000Z", attribution=_att(confirmation="typed"), **cand)
    assert _accepted(app_conn, seat, ns, key, value) == 1                                  # control


def test_first_hand_must_agree_with_attribution_only_on_a_card_born_with_it(app_conn, cards):
    """txxxx's catch: every card written before attribution says first_hand true and the field is frozen, so a stored
    Question re-versioned with speaker "agent" must be accepted; a card BORN with attribution must agree."""
    ns, seat, label = cards
    q = dict(card_type="Question", text="Question | ship it?")
    key, value = _card(label, time="2026-10-02T00:36:00.000Z", first_hand=True,
                       attribution=_att(speaker="agent", confirmation="asked"), **q)
    assert _refused(app_conn, seat, ns, key, value) == "E_CARD_SCHEMA"                     # born with it: disagrees
    legacy_key, legacy = _card(label, time="2026-10-02T01:36:00.000Z", first_hand=True, **q)
    assert _accepted(app_conn, seat, ns, legacy_key, legacy) == 1
    again = _again(legacy, attribution=_att(speaker="agent", confirmation="asked"))
    assert _accepted(app_conn, seat, ns, legacy_key, again) == 2                           # the re-version, (a)


def test_once_a_head_carries_attribution_it_is_carried_and_its_first_hand_is_frozen(app_conn, cards):
    ns, seat, label = cards
    key, value = _card(label, time="2026-10-02T00:36:00.000Z", attribution=_att())
    assert _accepted(app_conn, seat, ns, key, value) == 1
    dropped = {k: v for k, v in _again(value, text="Ruling | deploy now, v2").items() if k != "attribution"}
    assert _refused(app_conn, seat, ns, key, dropped) == "E_CARD_FROZEN"
    flipped = _again(value, attribution=_att(quoted_author="unknown"))                     # no a recorded decision
    assert _refused(app_conn, seat, ns, key, flipped) == "E_CARD_SCHEMA"                   # born with it: disagrees
    corrected = _again(value, attribution=_att(extraction="codex/answered_question"))     # control: a recorded decision
    assert _accepted(app_conn, seat, ns, key, corrected) == 2
    # A card born BEFORE attribution has no agreement check, so the freeze is what holds a recorded decision once a later
    # version adds attribution: the edges that version allowed were checked once.
    old_key, old = _card(label, time="2026-10-02T01:36:00.000Z", text="Ruling | an older card")
    assert _accepted(app_conn, seat, ns, old_key, old) == 1
    gained = _again(old, attribution=_att())
    assert _accepted(app_conn, seat, ns, old_key, gained) == 2
    assert _refused(app_conn, seat, ns, old_key, _again(gained, attribution=_att(quoted_author="unknown"))) \
        == "E_CARD_FROZEN"


def test_attribution_is_the_first_hand_authority_for_supersession(app_conn, cards):
    """One authority per card: a legacy Question re-versioned as the agent's still says first_hand true, and must no
    longer count a recorded decision when it names a recorded decision card."""
    ns, seat, label = cards
    direct_key, direct = _card(label, time="2026-10-02T00:36:00.000Z", text="Ruling | deploy now")
    assert _accepted(app_conn, seat, ns, direct_key, direct) == 1
    key, value = _card(label, time="2026-10-02T01:36:00.000Z", card_type="Question", text="Question | hold?",
                       first_hand=True)
    assert _accepted(app_conn, seat, ns, key, value) == 1
    agent_view = _again(value, attribution=_att(speaker="agent", confirmation="asked"), supersedes=[direct_key])
    assert _refused(app_conn, seat, ns, key, agent_view) == "E_CARD_SUPERSEDES"
    legacy_view = _again(value, supersedes=[direct_key])                                   # control: the legacy flag
    assert _accepted(app_conn, seat, ns, key, legacy_view) == 2


def test_lone_surrogates_in_card_value_are_refused_with_schema_error(app_conn, cards):
    """#322 Point 2: a lone surrogate in a card's value is refused by validate_card and memory.write with
    E_CARD_SCHEMA rather than propagating an unhandled UnicodeEncodeError as E_INTERNAL."""
    from tests.test_memory_skills import _envelope
    ns, seat, label = cards
    key, value = _card(label, time="2026-10-02T00:36:00.000Z")
    bad_notes = dict(value, notes="corrupt \ud800 surrogate")
    env = _envelope(seat, "memory.write", {"namespace": ns, "key": key, "value": value})
    env["payload"]["args"]["value"] = bad_notes
    with pytest.raises(HandlerError) as exc_write:
        handlers.dispatch("memory.write", env, seat["peer_id"], app_conn)
    assert exc_write.value.code == "E_CARD_SCHEMA"
    bad_text = dict(value, text="Ruling | \ud800")
    with pytest.raises(HandlerError) as exc:
        V.validate_card(ns, key, bad_text)
    assert exc.value.code == "E_CARD_SCHEMA"
    assert "no lone surrogates" in str(exc.value)
    assert _accepted(app_conn, seat, ns, key, value) == 1                                  # control
