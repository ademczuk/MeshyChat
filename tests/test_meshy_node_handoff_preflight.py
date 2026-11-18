"""`python -m tools.meshy_node handoff-preflight` (tools/meshy_node/handoff.py): can the board record this review
verdict, and if not, what first (SD 265 item 1; design note 97284f; pxx's read 2c3a0a).

No database, no server and no board: the facts are fixtures, and GET /v1/cards and the signed card reads are
stand-ins. TKT-970's three refusals (unbound 409, rounds spent 422, the $60 cap after the recorded rule's $100) are fixtures
modelled on oxxxxxx's description (97c0ac, 3b47a8), not on the board's records, which this file never read.

Run:  pytest tests/test_meshy_node_handoff_preflight.py -q     (needs no database)
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.meshy_node import cards, cli, handoff, node  # noqa: E402

NOW = datetime(2026, 11, 8, 1, 0, tzinfo=timezone.utc)
OXXXXXx = "610170d5-09fb-5385-9ea4-fc3df696b431"
TXXXX = "2d6d1628-bdc0-5034-a975-e0ffe6e2e9c4"
MARKER = "PLANTED-DOCUMENT-MARKER-9b2d"
K1, K2, K3 = ("card/oxxxxxx/" + c * 32 for c in "123")


def _facts(at=NOW, **over):
    f = {"schema": "handoff-facts/1", "issue": "TKT-970", "verdict": "changes", "execution_run_id": "run-3",
         "bound_handoff": {"id": "ho-1", "execution_run_id": "run-3"}, "rounds_recorded": 1, "max_review_rounds": 3,
         "result_cap": {"scope": ["TKT-958", "TKT-831"], "amount_cents": 10000, "revision": 2},
         "reads": [{"what": "issue", "at": (at - timedelta(minutes=2)).isoformat(), "sha256": "a" * 64},
                   {"what": "review card", "at": (at - timedelta(minutes=1)).isoformat(), "sha256": "b" * 64}]}
    f.update(over)
    return f


def _card(text, *, signer=OXXXXXx, verified=True, redacted=False, thread=handoff.REGISTRY_THREAD, schema="card.v1",
          seat="oxxxxxx"):
    msg = {"value": {"text": text, "schema": schema, "seat": seat, "thread_id": thread},
           "signature_verified": verified and not redacted, "signed_by": signer if verified and not redacted else None}
    if redacted:
        msg["redacted"] = {"reason_code": "fixture", "redacted_at": NOW.isoformat()}
    return msg


#: the recorded rule's registry: revision 1 capped TKT-958 at $60, revision 2 made it one $100 across TKT-958 and TKT-831.
REG = {K1: _card("CAP: TKT-958; CENTS: 6000; REVISION: 1; ROW: 1201"),
       K2: _card(f"CAP: TKT-831,TKT-958; CENTS: 10000; REVISION: 2; ROW: 1258\n{MARKER} the card's own words")}
SIXTY = {"scope": ["TKT-958"], "amount_cents": 6000, "revision": 1}


def _judge(facts=None, registry=REG, now=NOW):
    f = handoff.facts(facts if facts is not None else _facts(), now)
    reg = handoff.registry(list(registry), lambda key: registry[key], OXXXXXx)
    return handoff.judge(f, reg, now)


def test_tkt970s_three_refusals_alone_and_together_with_a_recordable_control():
    """CONTROL: bound, one round below the max, the current cap. Then each of TKT-970's refusals alone, then all three:
    every reason listed, and the route by pxx's fixed order (rebind before refresh_cap before close_on_delta).
    MUTANTS: the binding not checked; the run not compared; only the first reason listed; the order changed."""
    ok = _judge()
    assert ok["recordable"] and ok["refusal_reasons"] == [] and ok["next_route"] == "record"
    assert (ok["round"], ok["max_review_rounds"], ok["rounds_remaining"]) == (2, 3, 1)
    assert ok["cap_scope_hash"] == ok["carried_cap_hash"] and ok["binding"] == {"handoff_id": "ho-1",
                                                                                 "bound_to_current_run": True}
    unbound = _judge(_facts(bound_handoff=None))
    assert unbound["refusal_reasons"] == ["unbound"] and unbound["next_route"] == "rebind"
    other_run = _judge(_facts(bound_handoff={"id": "ho-1", "execution_run_id": "run-2"}))
    assert other_run["refusal_reasons"] == ["unbound"] and other_run["binding"]["bound_to_current_run"] is False
    spent = _judge(_facts(rounds_recorded=2))
    assert spent["refusal_reasons"] == ["exhausted"] and spent["next_route"] == "close_on_delta"
    assert spent["rounds_remaining"] == 0
    stale = _judge(_facts(result_cap=SIXTY))
    assert stale["refusal_reasons"] == ["cap_stale"] and stale["next_route"] == "refresh_cap"
    assert stale["cap"]["current_revision"] == 2 and stale["cap"]["carried_revision"] == 1
    every = _judge(_facts(bound_handoff=None, rounds_recorded=2, result_cap=SIXTY))
    assert every["refusal_reasons"] == ["unbound", "cap_stale", "exhausted"] and every["next_route"] == "rebind"
    assert not every["recordable"]


def test_the_round_boundary_one_below_is_recordable_and_the_max_refuses_a_changes_verdict():
    """pxx A: `round` is the round this verdict records (rounds_recorded + 1). MUTANTS: changes at the max allowed;
    every verdict refused at the max; a verdict past the max allowed."""
    assert _judge(_facts(rounds_recorded=1))["recordable"]                               # changes, round 2 of 3
    assert _judge(_facts(rounds_recorded=2))["refusal_reasons"] == ["exhausted"]         # changes, round 3 of 3
    accept_at_max = _judge(_facts(rounds_recorded=2, verdict="accept"))
    assert accept_at_max["recordable"] and accept_at_max["assumptions"] == [handoff.ASSUME_AT_MAX]
    assert _judge(_facts(rounds_recorded=1, verdict="accept"))["assumptions"] == []
    assert _judge(_facts(rounds_recorded=3, verdict="reject"))["refusal_reasons"] == ["exhausted"]   # round 4 of 3
    one = _facts(max_review_rounds=1, rounds_recorded=0)
    assert _judge(one)["refusal_reasons"] == ["exhausted"]
    assert _judge({**one, "verdict": "reject"})["recordable"]


def test_facts_older_than_ten_minutes_are_refreshed_first_judged_by_their_oldest_read():
    """MUTANTS: `>=` for `>`; the age taken from the newest read; refresh_facts not first."""
    old = NOW - timedelta(seconds=handoff.FACTS_MAX_AGE_S + 1)
    reads = [{"what": "issue", "at": old.isoformat(), "sha256": "c" * 64},
             {"what": "review card", "at": NOW.isoformat(), "sha256": "d" * 64}]
    stale = _judge(_facts(reads=reads, bound_handoff=None))
    assert stale["refusal_reasons"] == ["facts_stale", "unbound"] and stale["next_route"] == "refresh_facts"
    assert stale["facts"]["oldest_read_age_s"] == handoff.FACTS_MAX_AGE_S + 1
    edge = [{"what": "issue", "at": (NOW - timedelta(seconds=handoff.FACTS_MAX_AGE_S)).isoformat(), "sha256": "c" * 64}]
    assert _judge(_facts(reads=edge))["recordable"]


def test_the_cap_hash_is_canonical_and_amounts_are_integer_cents():
    """pxx C. MUTANTS: the scope not sorted; whitespace in the JSON; a float accepted as cents."""
    canon = '{"amount_cents":10000,"revision":2,"scope":["TKT-831","TKT-958"]}'
    want = hashlib.sha256(canon.encode("utf-8")).hexdigest()
    assert handoff.cap_hash({"scope": ["TKT-958", "TKT-831"], "amount_cents": 10000, "revision": 2}) == want
    assert handoff.cap_hash({"scope": ["TKT-831", "TKT-958"], "amount_cents": 10000, "revision": 2}) == want
    assert _judge()["cap_scope_hash"] == want
    for bad in (10000.0, True, "10000", -1):
        with pytest.raises(handoff.Unjudgeable, match="amount_cents must be an integer"):
            _judge(_facts(result_cap={"scope": ["TKT-958"], "amount_cents": bad, "revision": 1}))
    with pytest.raises(handoff.Unjudgeable, match="whole numbers"):
        _judge(registry={K1: _card("CAP: TKT-958; CENTS: 100.50; REVISION: 1")})


def test_the_registrys_highest_revision_wins_and_a_tie_with_another_cap_refuses():
    """MUTANTS: the lowest revision wins; a tie with two caps is not refused."""
    reverse = dict(reversed(list(REG.items())))
    assert _judge(registry=reverse)["cap"]["current_key"] == K2
    with pytest.raises(handoff.Unjudgeable, match="two registry cards claim revision 2 for TKT-958"):
        _judge(registry={**REG, K3: _card("CAP: TKT-958; CENTS: 9000; REVISION: 2")})
    same = {**REG, K3: _card("CAP: TKT-958,TKT-831; CENTS: 10000; REVISION: 2; ROW: 1258")}
    assert _judge(registry=same)["recordable"], "the same cap twice at one revision is one cap"


def test_a_card_that_does_not_verify_or_another_peer_signed_refuses_and_a_withdrawn_one_is_left_out():
    """MUTANTS: an unverified card trusted; another peer's card trusted; a withdrawn card refuses; a card without a
    CAP line refuses."""
    for bad, why in ((_card("CAP: TKT-958; CENTS: 1; REVISION: 9", verified=False), "did not verify"),
                     (_card("CAP: TKT-958; CENTS: 1; REVISION: 9", signer=TXXXX), "signed by another peer")):
        with pytest.raises(handoff.Unjudgeable, match=why):
            _judge(registry={**REG, K3: bad})

    def boom(key):
        raise OSError("connection reset")
    with pytest.raises(handoff.Unjudgeable, match=r"could not be read \(OSError\)"):
        handoff.registry([K1], boom, OXXXXXx)
    kept = _judge(registry={**REG, K3: _card("CAP: TKT-958; CENTS: 1; REVISION: 9", redacted=True)})
    assert kept["recordable"] and kept["registry"]["withdrawn_after_listing"] == [K3] and kept["registry"]["caps"] == 2
    assert "withdrawn before the listing is not listed" in kept["registry"]["note"]


def test_a_card_moved_off_the_registry_thread_after_listing_is_not_a_registry_card():
    """pxx P2 F2: the signed head's thread_id was never checked, so a card moved to another budget's thread after the
    listing still set the cap. It is left out and counted; codex's case (revision 2 moved) is then cap_stale. A card
    that is not oxxxxxx's card.v1 refuses. MUTANTS: the thread not checked; the schema or seat not checked."""
    moved = {**REG, K2: _card(REG[K2]["value"]["text"], thread="another-budget")}
    got = _judge(registry=moved)
    assert got["refusal_reasons"] == ["cap_stale"] and got["registry"]["moved"] == [K2]
    assert got["cap"]["current_revision"] == 1 and "no cap for TKT-831" in got["cap"]["why"], "only revision 1 is left"
    for bad in (_card("CAP: TKT-958; CENTS: 1; REVISION: 9", schema="card.v0"),
                _card("CAP: TKT-958; CENTS: 1; REVISION: 9", seat="txxxx")):
        with pytest.raises(handoff.Unjudgeable, match="is not a card.v1 card of oxxxxxx's"):
            _judge(registry={**REG, K3: bad})


def test_a_oxxxxxx_card_without_a_cap_line_is_counted_and_a_cap_line_that_does_not_parse_refuses():
    """MUTANTS: a card without a CAP line refuses; a CAP line with an unknown field is taken."""
    note = _judge(registry={**REG, K3: _card("a note on the registry, no cap here")})
    assert note["recordable"] and note["registry"]["other"] == 1
    with pytest.raises(handoff.Unjudgeable, match="CAP line does not parse"):
        _judge(registry={**REG, K3: _card("CAP: TKT-958; CENTS: 5; REVISION: 3; COLOR: blue")})


def test_a_result_without_a_cap_is_stale_only_where_the_registry_caps_the_issue():
    """MUTANTS: a missing cap never stale; an issue of the result's scope the registry does not list passes."""
    free = _judge(_facts(result_cap=None))
    assert free["recordable"] and free["cap"]["why"].startswith("no cap carried and none listed")
    owed = _judge(_facts(issue="TKT-958", result_cap=None))
    assert owed["refusal_reasons"] == ["cap_stale"] and owed["cap"]["current_revision"] == 2
    unlisted = _judge(_facts(result_cap={"scope": ["TKT-998"], "amount_cents": 10000, "revision": 2}))
    assert unlisted["refusal_reasons"] == ["cap_stale"] and "no cap for TKT-998" in unlisted["cap"]["why"]


@pytest.mark.parametrize("change, why", [
    ({"body": "x"}, "does not know: body"),
    ({"reads": []}, "name no board read"),
    ({"reads": [{"what": "issue", "at": NOW.isoformat(), "sha256": "A" * 64}]}, "64 lower-case hex"),
    ({"reads": [{"what": "issue", "at": "2026-11-08T00:59:00", "sha256": "a" * 64}]}, "with an offset"),
    ({"reads": [{"what": "issue", "at": (NOW + timedelta(minutes=3)).isoformat(), "sha256": "a" * 64}]}, "ahead of"),
    ({"reads": [{"what": "issue", "at": NOW.isoformat(), "sha256": "a" * 64, "raw": "x"}]}, r"reads\[0\] must be"),
    ({"schema": "handoff-facts/2"}, "schema must be"),
    ({"issue": "tkt-970"}, "issue must be"),
    ({"verdict": "approve"}, "verdict must be"),
    ({"rounds_recorded": -1}, "rounds_recorded must be"),
    ({"max_review_rounds": True}, "max_review_rounds must be"),
    ({"bound_handoff": {"id": "ho-1", "execution_run_id": "run-3", "key": "x"}}, "bound_handoff must be"),
    ({"result_cap": {"scope": ["TKT-958", "TKT-958"], "amount_cents": 1, "revision": 1}}, "distinct issue ids"),
])
def test_the_facts_refuse_an_unknown_field_and_every_malformed_value(change, why):
    """MUTANTS: an unknown field allowed; an empty reads list allowed; a read ahead of the clock allowed."""
    with pytest.raises(handoff.Unjudgeable, match=why):
        handoff.facts(_facts(**change), NOW)
    with pytest.raises(handoff.Unjudgeable, match="lack reads"):
        handoff.facts({k: v for k, v in _facts().items() if k != "reads"}, NOW)


def test_the_answer_holds_ids_counts_and_hashes_never_a_document():
    """pxx D: a registry card's own words never reach the answer or its text."""
    for result in (_judge(), _judge(_facts(result_cap=SIXTY, bound_handoff=None))):
        assert MARKER not in json.dumps(result) and MARKER not in handoff.render(result)
    assert MARKER in REG[K2]["value"]["text"], "control: the card does carry it"


def test_the_text_view_says_the_state_the_route_the_cap_and_the_note():
    text = handoff.render(_judge(_facts(bound_handoff=None, rounds_recorded=2, result_cap=SIXTY))).splitlines()
    assert text[0] == ("handoff-preflight TKT-970, changes verdict, round 3 of 3: NOT recordable (unbound, cap_stale, "
                       "exhausted)")
    assert text[1].startswith("next: rebind: ask the binding owner (oxxxxxx)")
    assert text[2] == "rounds remaining after this one: 0"
    assert text[3].startswith("cap: the result carries revision 1 (") and f"card {K2}" in text[3]
    assert text[4] == f"registry: 2 card(s) on {handoff.REGISTRY_THREAD}, 2 with a CAP line"
    assert text[5] == "facts: 2 board read(s), the oldest 2 min old (limit 10 min)"
    assert text[-1] == handoff.NOTE and "prevents mistakes, not misreporting" in text[-1]


# --- the command ---------------------------------------------------------------------------------------------------

@pytest.fixture
def board(monkeypatch):
    """GET /v1/cards and the signed card reads as stand-ins. `board.pages` is the list answers in order."""
    class Board:
        pages = [{"cards": [{"key": K1}, {"key": K2}], "next": None}]
        registry = dict(REG)
        queries, reads = [], []
    monkeypatch.setattr(cli.node, "load_state", lambda path, base_url: {"base_url": base_url, "peer_id": "p",
                                                                        "label": "kxxx"})

    def list_cards(path, state, query, **kw):
        Board.queries.append(dict(query))
        return Board.pages[min(len(Board.queries), len(Board.pages)) - 1]

    def read(path, state, *, key, namespace=None, **kw):
        Board.reads.append((key, namespace))
        return Board.registry[key]
    monkeypatch.setattr(node, "cards", list_cards)
    monkeypatch.setattr(node, "read", read)
    return Board


def _run(tmp_path, facts, *extra):
    path = tmp_path / "facts.json"
    path.write_text(json.dumps(facts), encoding="utf-8")
    return cli.main(["handoff-preflight", "--base-url", "http://127.0.0.1:9", "--state-file",
                     str(tmp_path / "state.json"), "--facts", str(path), *extra])


def test_the_command_lists_oxxxxxxs_cards_reads_each_signed_and_exits_by_the_answer(tmp_path, board, capsys):
    """Exit 0 recordable, 1 not, 2 cannot judge. MUTANTS: only active cards listed; the cards read in another
    namespace; exit 0 when not recordable; the signer not oxxxxxx's pinned peer."""
    now = datetime.now(timezone.utc)
    assert _run(tmp_path, _facts(at=now), "--json") == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["recordable"] and shown["registry"]["cards"] == 2
    assert board.queries[0] == {"thread": handoff.REGISTRY_THREAD, "seat": "oxxxxxx", "status": "any",
                                "limit": str(handoff.REGISTRY_MAX_CARDS)}
    assert board.reads == [(K1, cards.namespace()), (K2, cards.namespace())]
    assert _run(tmp_path, _facts(at=now, bound_handoff=None)) == 1
    assert capsys.readouterr().out.startswith("handoff-preflight TKT-970, changes verdict, round 2 of 3: NOT")
    board.registry[K2] = _card("CAP: TKT-958; CENTS: 1; REVISION: 2", signer=TXXXX)
    assert _run(tmp_path, _facts(at=now)) == 2
    shown = json.loads(capsys.readouterr().out)
    assert shown["error"] == "handoff_unjudgeable" and "another peer" in shown["detail"]
    assert next(p for p, label in node.SEAT_DIRECTIVE_PEERS.items() if label == "oxxxxxx") == OXXXXXx


def test_the_registry_is_paged_to_its_end_and_a_registry_too_long_refuses(tmp_path, board, capsys):
    """MUTANTS: the first page taken as the registry; no bound on the pages."""
    now = datetime.now(timezone.utc)
    board.pages = [{"cards": [{"key": K1}], "next": "cursor-1"}, {"cards": [{"key": K2}], "next": None}]
    assert _run(tmp_path, _facts(at=now), "--json") == 0
    assert json.loads(capsys.readouterr().out)["registry"]["cards"] == 2
    assert board.queries[1]["after"] == "cursor-1"
    board.queries.clear()
    board.pages = [{"cards": [{"key": K1}], "next": "again"}]
    assert _run(tmp_path, _facts(at=now)) == 2
    assert f"within {cli.HANDOFF_REGISTRY_PAGES} pages" in json.loads(capsys.readouterr().out)["detail"]
    board.queries.clear()
    board.pages = [{"cards": [{"key": "card/oxxxxxx/" + f"{i:032x}"} for i in range(51)], "next": None}]
    assert _run(tmp_path, _facts(at=now)) == 2
    assert "more than 50 cards" in json.loads(capsys.readouterr().out)["detail"]
    board.queries.clear()
    board.pages = [{"cards": [{"key": "msg/1794000000000-oxxxxxx-abcdef"}], "next": None}]   # MUTANT: key not checked
    assert _run(tmp_path, _facts(at=now)) == 2
    assert "without a card key" in json.loads(capsys.readouterr().out)["detail"]


@pytest.mark.parametrize("nxt", ["absent", False, 0, [], {}, ""])
def test_a_registry_listing_ends_only_on_an_exact_null_next(tmp_path, board, capsys, nxt):
    """pxx P2 F3: a missing or malformed `next` was taken as the end, so a newer revision on page 2 was never read.
    MUTANT: any falsy `next` ends the listing."""
    page = {"cards": [{"key": K1}]}
    if nxt != "absent":
        page["next"] = nxt
    board.pages = [page]
    assert _run(tmp_path, _facts(at=datetime.now(timezone.utc))) == 2
    assert "`next` cursor" in json.loads(capsys.readouterr().out)["detail"]


def test_a_registry_listing_that_fails_is_cannot_judge_never_not_recordable(tmp_path, board, monkeypatch, capsys):
    """pxx P2 F4: a ServerSaid from the listing escaped as exit 1, read as "not recordable". MUTANT: not caught."""
    def down(path, state, query, **kw):
        raise node.ServerSaid(503, {"error": "E_UNAVAILABLE", "message": "a body that must not be echoed"})
    monkeypatch.setattr(node, "cards", down)
    assert _run(tmp_path, _facts(at=datetime.now(timezone.utc))) == 2
    shown = json.loads(capsys.readouterr().out)
    assert shown["detail"] == "the registry could not be listed (ServerSaid, HTTP 503)" and "must not" not in str(shown)


def test_the_facts_age_is_judged_after_the_registry_reads(tmp_path, board, monkeypatch, capsys):
    """pxx P2 F1: staleness was judged at the command's start, so facts 590 s old plus 20 s of registry reads still
    passed. The clock is read again after the last registry read. MUTANT: the start clock used for the judgement."""
    start = datetime(2026, 11, 8, 2, 0, tzinfo=timezone.utc)
    clocks = iter([start, start + timedelta(seconds=20)])
    monkeypatch.setattr(cli, "_handoff_now", lambda: next(clocks))
    reads = [{"what": "issue", "at": (start - timedelta(seconds=590)).isoformat(), "sha256": "a" * 64}]
    assert _run(tmp_path, _facts(at=start, reads=reads), "--json") == 1
    shown = json.loads(capsys.readouterr().out)
    assert shown["refusal_reasons"] == ["facts_stale"] and shown["facts"]["oldest_read_age_s"] == 610


def test_a_facts_file_that_is_not_json_is_refused_and_the_cli_default_matches_the_module(tmp_path, board):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert cli.main(["handoff-preflight", "--base-url", "http://127.0.0.1:9", "--state-file",
                     str(tmp_path / "state.json"), "--facts", str(bad)]) == 2
    assert board.queries == [] and board.reads == []
    assert cli.HANDOFF_REGISTRY_THREAD == handoff.REGISTRY_THREAD
