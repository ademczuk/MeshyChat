"""The echo-credit report (divergence 085, issue #142 phase 1): a seat repeating another seat's message without
credit, found deterministically and reported daily. No model; nothing blocks a send.

  * credit: a reply to the source, its key cited, or an explicit attribution of its author; a seat's NAME used
    as subject matter is not credit (the 6 Oct finding: a "names a seat" rule wrongly credited 3 of 25);
  * repeats: exact (normalized) and near (shingle Jaccard, or the source contained in a longer message); a
    reworded restatement is NOT caught (phase 2's job, stated so it is not mistaken for coverage);
  * only an EARLIER message by ANOTHER author is a source;
  * the database report judges the window against the lookback and carries keys, labels and scores only.

Run:  pytest tests/test_mesh_echo.py -q
Mutants (apps/mesh/echo.py; each seen to fail a test here):
  E1  any mention of the author's name counts as credit       test_a_name_used_as_subject_matter_is_not_credit
  E2  a later message can be the source of an earlier one     test_only_an_earlier_message_by_another_seat_is_a_source
  E3  the same author repeating itself is reported            test_only_an_earlier_message_by_another_seat_is_a_source
  E4  containment is ignored (a pasted source in a long note) test_a_source_pasted_into_a_longer_message_is_found
  E5  bare "as X" / "from X" counts as credit                 test_a_name_used_as_subject_matter_is_not_credit
  E6  a typographic apostrophe loses credit                   test_explicit_attribution_is_credit
  E7  near matching skips the minimum length                  test_short_messages_do_not_match_on_boilerplate
  E8  ineligible sources are scored (the heading blow-up)     test_ineligible_sources_are_never_scored
"""
from __future__ import annotations

import datetime as _dt
import json

import pytest

from apps.mesh import echo
from apps.mesh.echo import Message

T0 = _dt.datetime(2026, 10, 6, 12, 0, tzinfo=_dt.timezone.utc)
FINDING = ("The replay ledger rejects a second envelope with the same nonce within the window, but the "
           "pruning job deletes rows older than one hour, so a delayed retry after that is accepted twice.")


def _m(key, author, minutes, body, reply=None) -> Message:
    return Message(f"msg/{1790000000000 + minutes}-{key}", author, T0 + _dt.timedelta(minutes=minutes), body, reply)


SRC = _m("aaaa", "txxxx", 0, FINDING)


@pytest.mark.parametrize("body", [
    "per txxxx: " + FINDING,
    "via @txxxx. " + FINDING,
    "h/t txxxx " + FINDING,
    "credit to txxxx. " + FINDING,
    "As txxxx found, " + FINDING,
    "txxxx's finding, restated: " + FINDING,
    "txxxx\u2019s finding, restated: " + FINDING,              # a typographic apostrophe (E6)
    "As txxxx said: " + FINDING,
    FINDING + " (thanks to txxxx)",
])
def test_explicit_attribution_is_credit(body):
    assert echo.credits(_m("bbbb", "uxxx", 5, body), SRC)


def test_a_reply_or_a_cited_key_is_credit():
    assert echo.credits(_m("bbbb", "uxxx", 5, FINDING, reply=SRC.key), SRC)
    assert echo.credits(_m("bbbb", "uxxx", 5, FINDING + " see " + SRC.key), SRC)


@pytest.mark.parametrize("body", [
    "Restart the txxxx daemon first. " + FINDING,           # the seat as the thing being acted on
    FINDING + " This also affects txxxx and uxxx.",          # named among those affected
    "txxxx is down again; meanwhile: " + FINDING,           # status about the seat
    "txxxxqube flagged it: " + FINDING,                      # a longer word containing the name
    "Run the worker as txxxx. " + FINDING,                   # bare "as X" (E5)
    "Disconnect from txxxx before retrying. " + FINDING,     # bare "from X" (E5)
])
def test_a_name_used_as_subject_matter_is_not_credit(body):
    """MUTANT E1."""
    assert not echo.credits(_m("bbbb", "uxxx", 5, body), SRC)


def test_exact_and_near_repeats_are_found_and_uncredited_ones_reported():
    exact = _m("bbbb", "uxxx", 5, FINDING.upper().replace(",", ""))
    near = _m("cccc", "pxx", 6, FINDING.replace("one hour", "sixty minutes"))
    echoes, credited = echo.find_echoes([exact, near], [SRC, exact, near])
    got = {(e.key, e.source_key, e.kind) for e in echoes}
    assert (exact.key, SRC.key, "exact") in got
    assert (near.key, SRC.key, "near") in got
    assert credited == 0


def test_a_credited_repeat_is_counted_not_reported():
    later = _m("bbbb", "uxxx", 5, "per txxxx: " + FINDING)
    echoes, credited = echo.find_echoes([later], [SRC, later])
    assert echoes == [] and credited == 1


def test_a_reworded_restatement_is_not_caught_here():
    """Phase 2's case (a model trial), stated so this report is not mistaken for covering it."""
    reworded = _m("bbbb", "uxxx", 5, "If a retry arrives more than an hour late the nonce table has already been "
                                     "cleaned, so the duplicate envelope gets through a second time.")
    echoes, _ = echo.find_echoes([reworded], [SRC, reworded])
    assert echoes == []


def test_only_an_earlier_message_by_another_seat_is_a_source():
    """MUTANTS E2 (a later message as source) and E3 (the same author)."""
    own = _m("bbbb", "txxxx", 5, FINDING)                    # txxxx repeating itself
    first = _m("cccc", "uxxx", 10, FINDING + " Also the retry window.")
    echoes, _ = echo.find_echoes([SRC, own, first], [SRC, own, first])
    pairs = {(e.key, e.source_key) for e in echoes}
    assert (own.key, SRC.key) not in pairs, "a seat repeating itself is not an echo"
    assert (SRC.key, first.key) not in pairs, "a later message cannot be the source of an earlier one"
    assert (first.key, SRC.key) in pairs


def test_a_source_pasted_into_a_longer_message_is_found():
    """MUTANT E4: the source is inside a much longer note, so Jaccard is low but containment is high."""
    filler = " ".join(f"item{i} status{i}" for i in range(80))      # unique words: many distinct shingles
    long_note = _m("bbbb", "uxxx", 5, "Status for today. " + filler + " " + FINDING + " " + filler.upper())
    echoes, _ = echo.find_echoes([long_note], [SRC, long_note])
    (e,) = echoes
    assert e.kind == "near" and e.containment >= echo.CONTAIN_MIN and e.jaccard < echo.NEAR_MIN_JACCARD


@pytest.mark.parametrize("text", ["ok thanks, will do",
                                  "Thanks for the update, will do",               # 6 words: shingles exist (E7)
                                  "Thanks for the update, will do it today"])     # 8 words: the control, reported
def test_short_messages_do_not_match_on_boilerplate(text):
    a = _m("aaaa", "txxxx", 0, text)
    b = _m("bbbb", "uxxx", 5, text)
    echoes, _ = echo.find_echoes([b], [a, b])
    n = len(echo.tokens(text))
    if n < echo.NEAR_MIN_TOKENS:
        assert echoes == [], f"{n} tokens matched: boilerplate, not an echo"
    else:
        assert echoes, "control: at the minimum length an identical repeat is reported"


def test_ineligible_sources_are_never_scored(monkeypatch):
    """MUTANT E8 (Codex on #147). 300 messages by ONE author share a heading: none can be another's source, so
    none may be scored. Counted, not timed: a timing threshold would flake on a loaded gate."""
    calls = []
    real = echo._score
    monkeypatch.setattr(echo, "_score", lambda *a: calls.append(a) or real(*a))
    heading = "Daily status report for the mesh, part one: "
    msgs = [_m(f"k{i:04d}", "txxxx", i, heading + " ".join(f"w{i}x{j}" for j in range(30))) for i in range(300)]
    echoes, _ = echo.find_echoes(msgs, msgs)
    assert echoes == [] and calls == [], f"{len(calls)} ineligible pairs were scored"


# --------------------------------------------------------------------------- the database report

from tests.test_mesh_routes import NS, P, _peer, _row_at, client, mesh_settings, owner  # noqa: E402,F401


def test_the_report_judges_the_window_against_the_lookback_and_carries_no_text(owner):
    now = _dt.datetime.now(_dt.timezone.utc)
    a, b = _peer(owner, "src"), _peer(owner, "rep")
    src_key, _ = _row_at(owner, a, {"body": FINDING, "thread": "t"}, now - _dt.timedelta(days=2))
    rep_key, _ = _row_at(owner, b, {"body": FINDING, "thread": "t"}, now - _dt.timedelta(hours=2))
    ok_key, _ = _row_at(owner, b, {"body": FINDING + " " + src_key, "thread": "t"}, now - _dt.timedelta(hours=1))
    with owner.transaction():
        owner.execute("SET LOCAL row_security = off")
        out = echo.report(owner, since=now - _dt.timedelta(days=1), until=now + _dt.timedelta(minutes=1))
    pairs = {(e["key"], e["source_key"]) for e in out["echoes"]}
    assert (rep_key, src_key) in pairs, "the repeat of a message from the lookback was not reported"
    assert (ok_key, src_key) not in pairs and out["credited_repeats"] >= 1
    assert "replay ledger" not in json.dumps(out), "the report carried message text"


def test_a_short_message_is_not_a_near_echo_of_a_longer_source():
    """MUTANT E7: the length guard is on BOTH sides. A six-word note against an eight-word source that contains
    it has Jaccard 0.5 (2 shingles shared of 4), so only the guard on the NEW message keeps it out."""
    src = _m("aaaa", "txxxx", 0, "Thanks for the update, will do it today")
    short = _m("bbbb", "uxxx", 5, "Thanks for the update, will do")
    echoes, _ = echo.find_echoes([short], [src, short])
    assert echoes == []
