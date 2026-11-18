"""`meshy_node review-receipt` (tools/meshy_node/review_receipt.py): is there a clean AI review of EXACTLY this head?
No GitHub, no server, no database here: GitHub and the verified read are stand-ins. The connector's state lines
below are the real shapes read from the repository's own PRs on Sun 15 Nov 2026 (shas replaced by made-up ones).

Run:  python -m pytest tests/test_meshy_node_review_receipt.py -q -p no:cacheprovider --noconftest
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.meshy_node import review_receipt as rr  # noqa: E402

HEAD = "a" * 40
OTHER = "b" * 40
REPORT = "c" * 64
TOKEN = "0123456789abcdef"
CONNECTOR = "chatgpt-codex-connector[bot]"
PXX_PEER = rr.HELPERS["codex-pxx-cli"][1]
KXXX_PEER = rr.HELPERS["codex-kxxx-cli"][1]
REPO = "sampleco/MeshyChat"


def state(sha=HEAD, status="completed", any_=False, open_=False, at="2026-11-15T10:00:00Z", login=CONNECTOR, **extra):
    review = {"commit_sha": sha, "has_any_findings": any_, "has_unresolved_findings": open_, "status": status}
    review.update(extra)
    data = {"code_review": review, "security_review": {"commit_sha": None, "has_any_findings": None,
                                                       "has_unresolved_findings": None, "status": None}}
    body = ("<!-- codex-pull-request-review-summary -->\n<!-- codex-review-state:v1 %s -->\n\n<details>\n"
            "<summary><strong>Codex Review Summary</strong></summary>" % json.dumps(data, separators=(",", ":")))
    return {"user": {"login": login}, "created_at": at, "updated_at": at, "body": body}


def line(reviewer="codex-pxx-cli", head=HEAD, p1=0, p2=0, p3=0, report=REPORT, token=TOKEN):
    return ("AI-REVIEW: fallback; reviewer: %s; head: %s; P1: %d; P2: %d; P3: %d; report sha256: %s; token: %s"
            % (reviewer, head, p1, p2, p3, report, token))


def comment(text, login="sampleco", at="2026-11-15T11:00:00Z"):
    return {"user": {"login": login}, "created_at": at, "updated_at": at, "body": text}


def message(text, signed_by=PXX_PEER, verified=True, key="msg/1794740000000-pxx-aaaaaa", **extra):
    return {"key": key, "from": "pxx", "signature_verified": verified, "signed_by": signed_by, "body": text, **extra}


def ask(comments, messages=(), head=HEAD, author="kxxx", report=REPORT, **kw):
    return rr.verdict(list(comments), list(messages), head, author_seat=author, report_sha256=report, **kw)


# ------------------------------------------------------------------------------------------------ GitHub's own review

def test_a_completed_review_of_this_head_with_nothing_open_is_clean():
    v = ask([state()])
    assert v["clean"] is True and v["source"] == "github"


@pytest.mark.parametrize("c, why", [
    (state(open_=True, any_=True), "unresolved findings"),
    (state(status="failed", open_=True, any_=True), "did not complete"),
    (state(status="in_progress"), "did not complete"),
    (state(status=None), "did not complete"),
    (state(sha=OTHER), "another head"),
    (state(sha=HEAD[:12]), "another head"),
    (state(sha=HEAD.upper()), "another head"),
    (state(sha=None), "another head"),
    (state(open_=None), "does not say whether findings are open"),
    (state(open_="false"), "does not say whether findings are open"),
    (state(open_=0), "does not say whether findings are open"),
])
def test_anything_else_from_the_connector_is_not_clean(c, why):
    v = ask([c])
    assert v["clean"] is False and why in v["why"], v


def test_only_the_connectors_own_comments_count_and_only_its_newest_state():
    planted = state(login="sampleco")                                   # a person pastes a clean state line
    assert ask([planted])["clean"] is False
    assert ask([state(login="chatgpt-codex-connector")])["clean"] is False       # not the bot's exact login
    older_clean, newer_failed = state(at="2026-11-15T09:00:00Z"), state(status="failed", at="2026-11-15T10:00:00Z")
    assert ask([older_clean, newer_failed])["clean"] is False
    assert ask([newer_failed, older_clean])["clean"] is False           # order in the list does not matter
    older_failed, newer_clean = state(status="failed", at="2026-11-15T09:00:00Z"), state(at="2026-11-15T10:00:00Z")
    assert ask([older_failed, newer_clean])["clean"] is True
    edited = dict(state(status="failed"), created_at="2026-11-15T08:00:00Z", updated_at="2026-11-15T12:00:00Z")
    assert ask([state(at="2026-11-15T10:00:00Z"), edited])["clean"] is False     # the connector edits ONE comment


@pytest.mark.parametrize("body", [
    "<!-- codex-review-state:v1 {not json} -->", "<!-- codex-review-state:v1 [] -->",
    "<!-- codex-review-state:v1 {\"code_review\": 7} -->", "<!-- codex-review-state:v1 {} -->", "", "no state here",
])
def test_an_unreadable_state_is_no_review_and_never_raises(body):
    v = ask([{"user": {"login": CONNECTOR}, "created_at": "2026-11-15T10:00:00Z", "body": body}])
    assert v["clean"] is False and v["source"] == "none"


@pytest.mark.parametrize("comments", [None, 7, "x", [None, 7, "x", {}, {"user": None}, {"user": {"login": 7}},
                                                    {"user": {"login": CONNECTOR}, "body": None}]])
def test_odd_comment_lists_are_no_review(comments):
    v = rr.verdict(comments, None, HEAD)
    assert v["clean"] is False and v["source"] == "none"


@pytest.mark.parametrize("head", [None, "", HEAD[:39], HEAD + "a", HEAD.upper(), "g" * 40, 7, HEAD + "\n"])
def test_a_head_that_is_not_a_full_sha_is_refused(head):
    v = rr.verdict([state()], [], head)
    assert v["clean"] is False and "full 40-hex sha" in v["why"]


# ------------------------------------------------------------------------------------------------ the fallback receipt

def test_a_receipt_comment_its_signed_message_and_its_report_are_clean_when_p1_and_p2_are_zero():
    v = ask([comment(line(p3=2))], [message(line(p3=2))])
    assert v["clean"] is True and v["source"] == "fallback"
    assert v["fallback"]["reviewer"] == "codex-pxx-cli" and v["fallback"]["session"] == "pxx"


@pytest.mark.parametrize("p1, p2", [(1, 0), (0, 1), (2, 3)])
def test_a_receipt_with_a_p1_or_a_p2_is_not_clean(p1, p2):
    v = ask([comment(line(p1=p1, p2=p2))], [message(line(p1=p1, p2=p2))])
    assert v["clean"] is False and v["fallback"]["state"] == "findings"


def test_the_comment_alone_is_not_a_receipt():
    v = ask([comment(line())])
    assert v["clean"] is False and v["fallback"]["state"] == "void"
    assert "no message signed by pxx's peer" in v["why"]


@pytest.mark.parametrize("msg, why", [
    (message(line(), verified=False), "no message signed by"),
    (message(line(), verified="true"), "no message signed by"),
    (message(line(), signed_by=KXXX_PEER), "no message signed by"),     # the right text under another peer's key
    (message(line(), signed_by=None), "no message signed by"),
    (message(line(), signed_by="pxx"), "no message signed by"),         # a label is not a peer id
    (message(line(), signed_by=PXX_PEER.upper()), "no message signed by"),
    (message(line(), key="card/x"), "no message signed by"),            # only a msg/ key binds label and signer
    (message(line(), key=None), "no message signed by"),
    (message(line(token="f" * 16)), "no message signed by"),            # another request's token: no replay
    (message(line(head=OTHER)), "no message signed by"),
    (message("see below\n" + line()), "no message signed by"),          # quoted lower down is not a receipt
    (message(line() + " and more"), "no message signed by"),
    (message(line(p3=1)), "disagree on p3"),
    (message(line(p2=1)), "disagree on p2"),
    (message(line(report="d" * 64)), "disagree on report"),
    (message(line(reviewer="codex-bxxxxx-cli")), "disagree on reviewer"),
])
def test_a_message_that_does_not_carry_the_same_receipt_voids_it(msg, why):
    v = ask([comment(line())], [msg])
    assert v["clean"] is False and v["fallback"]["state"] == "void" and why in v["why"], v


def test_the_from_label_proves_nothing_only_the_verified_signer_does():
    """pxx fc8e1b case A: a hand-written message saying from "pxx" with signature_verified true. A label is text."""
    forged = {"key": "msg/1-pxx-aaaaaa", "from": "pxx", "author": "pxx", "signature_verified": True, "body": line()}
    assert ask([comment(line())], [forged])["fallback"]["state"] == "void"          # no signed_by at all
    wrong = dict(forged, signed_by=KXXX_PEER)
    assert ask([comment(line())], [wrong])["fallback"]["state"] == "void"
    right_peer_other_label = message(line(), **{"from": "someone-else"})
    assert ask([comment(line())], [right_peer_other_label])["clean"] is True         # the label is not what counts


def test_a_disagreement_is_void_not_the_stricter_of_the_two():
    """pxx 9a16b8: the comment says P2 0, the message P2 1. Not "findings": void, and the PR waits."""
    v = ask([comment(line(p2=0))], [message(line(p2=1))])
    assert v["fallback"]["state"] == "void" and v["clean"] is False


@pytest.mark.parametrize("reviewer, peer, author, why", [
    ("codex-kxxx-cli", KXXX_PEER, "kxxx", "the author's own helper"),
    ("codex-pxx-cli", PXX_PEER, "pxx", "the author's own helper"),
    ("codex-pxx-cli", PXX_PEER, "codex-pxx-cli", "the author's own helper"),         # a helper's own PR
    ("codex-other-cli", PXX_PEER, "kxxx", "not a known helper"),
    ("codex-pxx-cli", PXX_PEER, None, "author seat is unknown"),
])
def test_the_authors_own_helper_an_unknown_helper_or_an_unknown_author_voids_the_receipt(reviewer, peer, author, why):
    v = ask([comment(line(reviewer=reviewer))], [message(line(reviewer=reviewer), signed_by=peer)], author=author)
    assert v["clean"] is False and v["fallback"]["state"] == "void" and why in v["why"], v


def test_a_helpers_own_pr_on_its_sessions_branch_can_be_reviewed_by_another_sessions_helper_only():
    """The Codex review of 1bff3551: kxxx/x with "author seat: codex-kxxx-cli" was an unknown author, so even pxx's
    helper was refused."""
    seat = rr.author_seat_of("kxxx/x", "author seat: codex-kxxx-cli")
    assert seat == "codex-kxxx-cli"
    ok = ask([comment(line())], [message(line())], author=seat)                       # codex-pxx-cli, signed by pxx
    assert ok["clean"] is True and ok["source"] == "fallback", ok
    own = ask([comment(line(reviewer="codex-kxxx-cli"))], [message(line(reviewer="codex-kxxx-cli"), signed_by=KXXX_PEER)],
              author=seat)
    assert own["clean"] is False and "the author's own helper" in own["why"], own
    assert rr.session_of("codex-kxxx-cli") == "kxxx" and rr.session_of("kxxx") == "kxxx" and rr.session_of("x") == "x"


def test_a_receipt_without_its_report_or_with_another_report_is_void():
    """pxx fc8e1b P3-2: the report is part of the proof."""
    v = ask([comment(line())], [message(line())], report=None)
    assert v["clean"] is False and v["fallback"]["state"] == "void" and "report was not given" in v["why"]
    v = ask([comment(line())], [message(line())], report="e" * 64)
    assert v["fallback"]["state"] == "void" and "does not hash" in v["why"]


def test_a_receipt_comment_counts_only_from_the_poster_list_and_only_as_the_first_line():
    assert ask([comment(line(), login="someone-else")], [message(line())])["fallback"]["state"] == "none"
    assert ask([comment("note\n" + line())], [message(line())])["fallback"]["state"] == "none"
    assert ask([comment(line(head=OTHER))], [message(line(head=OTHER))])["fallback"]["state"] == "none"
    assert ask([comment(line())], [message(line())], posters=frozenset())["fallback"]["state"] == "none"


def test_the_newest_receipt_for_the_head_is_the_one_judged_even_when_it_is_the_worse_one():
    old = comment(line(p2=1, token="1" * 16), at="2026-11-15T10:00:00Z")
    new = comment(line(token="2" * 16), at="2026-11-15T11:00:00Z")
    msgs = [message(line(p2=1, token="1" * 16)), message(line(token="2" * 16))]
    assert ask([old, new], msgs)["clean"] is True
    older_clean = comment(line(token="2" * 16), at="2026-11-15T09:00:00Z")
    assert ask([older_clean, old], msgs)["clean"] is False              # the newer one has a P2
    shadow = comment(line(reviewer="codex-other-cli", token="3" * 16), at="2026-11-15T12:00:00Z")
    v = ask([new, shadow], msgs)                                        # a newer receipt that fails shadows a good one
    assert v["clean"] is False and v["fallback"]["state"] == "void"


def test_two_different_receipts_at_the_same_newest_time_are_void_in_either_order():
    """The Codex review of 04287848: same second, `max` took the first listed, so a clean one could mask a P2."""
    at = "2026-11-15T11:00:00Z"
    clean, dirty = comment(line(token="1" * 16), at=at), comment(line(p2=1, token="2" * 16), at=at)
    msgs = [message(line(token="1" * 16)), message(line(p2=1, token="2" * 16))]
    for order in ([clean, dirty], [dirty, clean]):
        v = ask(order, msgs)
        assert v["clean"] is False and v["fallback"]["state"] == "void" and "same newest time" in v["why"], order
    # the same receipt twice at one time is one receipt; and a clearly newer one still decides
    assert ask([clean, dict(clean)], msgs)["clean"] is True
    older_dirty = comment(line(p2=1, token="2" * 16), at="2026-11-15T10:59:59Z")
    assert ask([older_dirty, clean], msgs)["clean"] is True
    newer_dirty = comment(line(p2=1, token="2" * 16), at="2026-11-15T11:00:01Z")
    assert ask([clean, newer_dirty], msgs)["fallback"]["state"] == "findings"
    # a tie BELOW the newest time does not matter
    assert ask([comment(line(token="3" * 16), at="2026-11-15T09:00:00Z"), comment(line(p2=1, token="4" * 16), at="2026-11-15T09:00:00Z"),
                clean], msgs)["clean"] is True
    # a receipt for ANOTHER head at the same time is not a rival
    assert ask([clean, comment(line(head=OTHER, p2=1, token="5" * 16), at=at)], msgs)["clean"] is True


def test_two_different_connector_states_at_the_same_newest_time_are_ambiguous_and_block_in_either_order():
    at = "2026-11-15T10:00:00Z"
    good, bad = state(at=at), state(open_=True, any_=True, at=at)
    receipt = [comment(line()), ]
    for order in ([good, bad], [bad, good]):
        v = ask(order)
        assert v["clean"] is False and v["github"]["state"] == "ambiguous" and "same time and differ" in v["github"]["why"]
        # the Codex review of 1bff3551: a valid clean fallback receipt beside the tie must NOT make it clean
        v = ask(order + receipt, [message(line())])
        assert v["fallback"]["state"] == "clean", v                     # the receipt itself is good
        assert v["clean"] is False and v["source"] == "github" and v["github"]["state"] == "ambiguous", v
    # a clean state tied with one that says nothing about this head: still two different answers, still blocking
    for other in (state(sha=OTHER, at=at), state(status="in_progress", at=at)):
        v = ask([good, other] + receipt, [message(line())])
        assert v["clean"] is False and v["github"]["state"] == "ambiguous", v
    # two DIFFERENT texts that say the SAME about this head are one answer
    v = ask([bad, state(open_=True, any_=False, at=at)])
    assert v["clean"] is False and v["github"]["state"] == "findings" and "agree" in v["github"]["why"], v
    v = ask([good, state(any_=True, at=at)])
    assert v["clean"] is True and v["source"] == "github", v
    v = ask([state(sha=OTHER, at=at), state(sha=OTHER, open_=True, at=at)] + receipt, [message(line())])
    assert v["github"]["state"] == "none" and v["clean"] is True and v["source"] == "fallback", v   # neither is of this head
    assert ask([good, dict(good)])["clean"] is True                    # the same state twice is one state
    assert ask([bad, state(at="2026-11-15T10:00:01Z")])["clean"] is True
    one_comment = dict(good, body=good["body"] + "\n" + bad["body"])    # two state lines in ONE comment: same time
    assert ask([one_comment])["clean"] is False


@pytest.mark.parametrize("bad", [
    line().replace("P1: 0", "P1: -1"), line().replace("P1: 0", "P1: 0.0"), line().replace("; token: " + TOKEN, ""),
    line().replace(HEAD, HEAD[:12]), line().replace("AI-REVIEW", "ai-review"), line().replace(REPORT, REPORT[:63]),
    line(reviewer="Codex Pxx"), line().replace("P2: 0", "P2: 00000"),
])
def test_a_line_that_is_not_exactly_the_fixed_shape_is_not_a_receipt(bad):
    assert ask([comment(bad)], [message(bad)])["fallback"]["state"] == "none"


# ------------------------------------------------------------------------------------------------ the two together

def test_githubs_own_review_comes_first_and_its_open_findings_outrank_a_clean_fallback():
    fallback = ([comment(line())], [message(line())])
    both = ask([state(open_=True, any_=True)] + fallback[0], fallback[1])
    assert both["clean"] is False and both["source"] == "github"        # the stricter holds
    assert ask([state()] + fallback[0], fallback[1])["source"] == "github"
    out = ask([state(status="failed")] + fallback[0], fallback[1])      # GitHub is out: the fallback answers
    assert out["clean"] is True and out["source"] == "fallback"
    other_head = ask([state(sha=OTHER)] + fallback[0], fallback[1])
    assert other_head["clean"] is True and other_head["source"] == "fallback"


def test_a_review_of_one_head_never_answers_for_another():
    comments, msgs = [state(), comment(line())], [message(line())]
    assert ask(comments, msgs, head=HEAD)["clean"] is True
    v = ask(comments, msgs, head=OTHER)
    assert v["clean"] is False and v["source"] == "none"


# ------------------------------------------------------------------------------------------------ the author's seat

@pytest.mark.parametrize("ref, body, seat", [
    ("kxxx/queue-free-section-20261115", "", "kxxx"),
    ("pxx/x", None, "pxx"),
    ("bxxxxx/prerelease-a1-a2-a5", "", "memory-bxxxxx"),
    ("codex/kxxx-tkt4236-orient-20261115", "", None),                   # a real branch of today: no seat in its prefix
    ("codex/kxxx-tkt4236", "author seat: codex-kxxx-cli", "codex-kxxx-cli"),
    ("feature/x", "Some text\n- **author seat:** pxx\nmore", "pxx"),
    ("kxxx/x", "author seat: kxxx", "kxxx"),
    ("kxxx/x", "author seat: codex-kxxx-cli", "codex-kxxx-cli"),        # one seat by two sources: the helper runs in kxxx
    ("pxx/x", "author seat: codex-pxx-cli", "codex-pxx-cli"),
    ("bxxxxx/x", "author seat: codex-bxxxxx-cli", "codex-bxxxxx-cli"),  # the branch prefix maps to memory-bxxxxx
    ("kxxx/x", "author seat: codex-pxx-cli", None),                     # another session's helper: the sources disagree
    ("pxx/x", "author seat: codex-kxxx-cli", None),
    ("kxxx/x", "author seat: pxx", None),                               # the two sources disagree
    ("feature/x", "author seat: pxx\nauthor seat: kxxx", None),         # two different lines
    ("feature/x", "author seat: somebody", None),                       # not a known seat
    ("feature/x", "the author seat: pxx is mentioned in prose", None),
    ("feature/x", "we name the author seat: pxx", None),                # the line must begin with it
    ("kxxx", "", None), ("", "", None), (None, None, None), (7, 7, None),
    ("KXXX/x", "", None),
])
def test_the_author_seat_comes_from_the_branch_prefix_or_the_pr_body_and_unknown_is_unknown(ref, body, seat):
    assert rr.author_seat_of(ref, body) == seat


# ------------------------------------------------------------------------------------------------ the live check

class World:
    """GitHub and the verified read, as stand-ins: what `check` fetches for itself."""

    def __init__(self, head=HEAD, ref="kxxx/some-branch", body="", comments=(), messages=None):
        self.pr = {"head_sha": head, "head_ref": ref, "body": body, "state": "open"}
        self.comments, self.messages, self.asked = list(comments), dict(messages or {}), []

    def fetch_pr(self, repo, pr):
        self.asked.append(("pr", repo, pr))
        return self.pr

    def fetch_comments(self, repo, pr):
        self.asked.append(("comments", repo, pr))
        return self.comments

    def read(self, key):
        self.asked.append(("read", key))
        if key not in self.messages:
            raise LookupError("no such message")
        return self.messages[key]

    def check(self, head=HEAD, keys=(), report=REPORT, repo=REPO, pr=641):
        return rr.check(repo, pr, head, message_keys=list(keys), read_message=self.read, report_sha256=report,
                        fetch_pr=self.fetch_pr, fetch_comments=self.fetch_comments)


KEY = "msg/1794740000000-pxx-aaaaaa"


def test_the_live_check_reads_github_itself_and_is_clean_for_the_prs_current_head():
    w = World(comments=[state()])
    v = w.check()
    assert v["clean"] is True and v["source"] == "github" and v["live"] is True and v["author_seat"] == "kxxx"
    assert w.asked == [("pr", REPO, 641), ("comments", REPO, 641)]
    assert rr.render(v).startswith("AI REVIEW CLEAN for %s (github)" % HEAD)


def test_a_head_that_is_not_the_prs_head_now_is_refused_before_anything_is_judged():
    """pxx fc8e1b: a stale or other head can't be asked about."""
    w = World(head=OTHER, comments=[state()])                           # the PR has moved on to OTHER
    v = w.check(head=HEAD)
    assert v["clean"] is False and "the PR's head is %s now" % OTHER[:12] in v["why"]
    assert w.asked == [("pr", REPO, 641)]                               # the comments were not even fetched


def test_the_live_check_takes_the_signer_from_its_own_read_and_the_author_from_the_pr():
    w = World(comments=[state(status="failed"), comment(line())], messages={KEY: message(line())})
    v = w.check(keys=[KEY])
    assert v["clean"] is True and v["source"] == "fallback" and v["messages_read"] == 1
    assert ("read", KEY) in w.asked
    # the same receipt on the reviewer's own PR is void: the author comes from the PR's branch, not from the caller
    own = World(ref="pxx/its-own-branch", comments=[state(status="failed"), comment(line())],
                messages={KEY: message(line())})
    assert own.check(keys=[KEY])["fallback"]["state"] == "void"
    unknown = World(ref="feature/x", comments=[state(status="failed"), comment(line())],
                    messages={KEY: message(line())})
    assert "author seat is unknown" in unknown.check(keys=[KEY])["why"]


def test_a_message_that_does_not_read_or_was_not_asked_for_is_no_proof():
    w = World(comments=[comment(line())], messages={KEY: message(line())})
    v = w.check(keys=[])                                                # the key was not given: nothing is read
    assert v["clean"] is False and v["fallback"]["state"] == "void" and v["messages_read"] == 0
    v = w.check(keys=["msg/1794740000000-pxx-bbbbbb"])                  # it does not read
    assert v["clean"] is False and v["messages_unread"] == 1
    unverified = World(comments=[comment(line())], messages={KEY: message(line(), verified=False)})
    assert unverified.check(keys=[KEY])["clean"] is False
    unsigned = {"key": KEY, "from": "pxx", "signature_verified": True, "body": line()}     # the read names no signer
    v = World(comments=[comment(line())], messages={KEY: unsigned}).check(keys=[KEY])
    assert v["clean"] is False and v["fallback"]["state"] == "void" and v["messages_read"] == 1


@pytest.mark.parametrize("kw", [
    dict(repo="not a repo"), dict(repo="a/b/c"), dict(repo="../x/y"), dict(repo=None), dict(pr=0), dict(pr=-1),
    dict(pr="641"), dict(pr=True), dict(keys=["card/x"]), dict(keys=["msg/x y"]), dict(keys=[7]),
    dict(keys=["msg/a"] * 21),
])
def test_a_question_that_is_not_well_formed_is_refused_and_nothing_is_fetched(kw):
    w = World(comments=[state()])
    v = w.check(**kw)
    assert v["clean"] is False and w.asked == []


@pytest.mark.parametrize("fault", ["pr-raises", "comments-raise", "pr-not-a-dict", "comments-bad-json"])
def test_a_github_read_that_fails_is_not_clean_and_never_raises(fault):
    w = World(comments=[state()])
    if fault == "pr-raises":
        w.fetch_pr = lambda repo, pr: (_ for _ in ()).throw(LookupError("gh api failed (exit 1): HTTP 404"))
    elif fault == "comments-raise":
        w.fetch_comments = lambda repo, pr: (_ for _ in ()).throw(LookupError("gh did not answer"))
    elif fault == "pr-not-a-dict":
        w.fetch_pr = lambda repo, pr: ["x"]
    else:
        w.fetch_comments = lambda repo, pr: json.loads("{not json")
    v = w.check()
    assert v["clean"] is False and v["source"] == "none"


def test_the_real_github_readers_ask_gh_for_gets_only_and_parse_its_answers(monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        out = (json.dumps({"head": {"sha": HEAD, "ref": "kxxx/x"}, "body": "b", "state": "open"})
               if "/pulls/" in argv[4] else
               json.dumps(state()) + "\n" + json.dumps(comment("hello")) + "\n")
        return type("P", (), {"returncode": 0, "stdout": out, "stderr": ""})()
    monkeypatch.setattr(rr.subprocess, "run", fake_run)
    assert rr.gh_pr(REPO, 641) == {"head_sha": HEAD, "head_ref": "kxxx/x", "body": "b", "state": "open"}
    assert len(rr.gh_comments(REPO, 641)) == 2
    assert all(c[:4] == ["gh", "api", "-X", "GET"] for c in calls)
    assert calls[0][4] == "repos/%s/pulls/641" % REPO and calls[1][4] == "repos/%s/issues/641/comments" % REPO
    assert "--paginate" in calls[1]
    monkeypatch.setattr(rr.subprocess, "run", lambda argv, **kw: type("P", (), {"returncode": 1, "stdout": "",
                                                                               "stderr": "gh: Not Found (HTTP 404)"})())
    with pytest.raises(LookupError):
        rr.gh_pr(REPO, 641)
    v = rr.check(REPO, 641, HEAD)                                       # the real readers, gh failing: not clean
    assert v["clean"] is False and "GitHub did not read" in v["why"]


# ------------------------------------------------------------------------------------------------ the command

def _files(tmp_path, comments, messages=None):
    cf = tmp_path / "comments.json"
    cf.write_text(json.dumps(comments), encoding="utf-8")
    argv = ["--comments-file", str(cf)]
    if messages is not None:
        mf = tmp_path / "messages.json"
        mf.write_text(json.dumps(messages), encoding="utf-8")
        argv += ["--messages-file", str(mf)]
    return argv


def test_the_file_mode_is_never_a_merge_check_even_for_inputs_that_would_read_clean(tmp_path, capsys):
    """pxx fc8e1b cases A and B, as the reviewer ran them: hand-written files for a head nobody reviewed."""
    from tools.meshy_node import cli
    rep = tmp_path / "report.md"
    rep.write_bytes(b"x")
    sha = hashlib.sha256(b"x").hexdigest()
    forged_a = _files(tmp_path, [comment(line(report=sha))], [message(line(report=sha))])
    rc = cli.main(["review-receipt", "--head", HEAD, *forged_a, "--author-seat", "kxxx", "--report", str(rep)])
    out = capsys.readouterr().out
    assert rc == 3 and out.startswith("NOT A MERGE CHECK: files given.")
    rc = cli.main(["review-receipt", "--head", HEAD, *_files(tmp_path, [state()])])                   # case B
    out = capsys.readouterr().out
    assert rc == 3 and out.startswith("NOT A MERGE CHECK: files given.")
    rc = cli.main(["review-receipt", "--head", HEAD, *_files(tmp_path, [state()]), "--json"])
    assert rc == 3 and json.loads(capsys.readouterr().out)["live"] is False


def test_the_command_checks_live_and_exits_0_only_for_a_clean_review_of_the_current_head(tmp_path, capsys, monkeypatch):
    from tools.meshy_node import cli
    w = World(comments=[state()])
    monkeypatch.setattr(rr, "gh_pr", w.fetch_pr)
    monkeypatch.setattr(rr, "gh_comments", w.fetch_comments)
    base = ["review-receipt", "--repo", REPO, "--pr", "641"]
    assert cli.main([*base, "--head", HEAD]) == 0
    assert capsys.readouterr().out.startswith("AI REVIEW CLEAN for %s (github)" % HEAD)
    assert cli.main([*base, "--head", OTHER]) == 1
    out = capsys.readouterr().out
    assert out.startswith("AI REVIEW NOT CLEAN for %s" % OTHER) and "the PR's head is %s now" % HEAD[:12] in out
    w.comments = [state(open_=True, any_=True)]
    assert cli.main([*base, "--head", HEAD, "--json"]) == 1
    v = json.loads(capsys.readouterr().out)
    assert v["clean"] is False and v["live"] is True and v["source"] == "github"


def test_the_command_refuses_a_question_it_cannot_check_with_exit_2(tmp_path, capsys):
    from tools.meshy_node import cli
    assert cli.main(["review-receipt", "--head", HEAD]) == 2                                  # no --repo, no --pr
    assert cli.main(["review-receipt", "--head", HEAD, "--repo", REPO]) == 2
    assert cli.main(["review-receipt", "--head", HEAD, "--repo", REPO, "--pr", "1", "--author-seat", "kxxx"]) == 2
    capsys.readouterr()
    assert cli.main(["review-receipt", "--head", HEAD, "--repo", REPO, "--pr", "1", "--message-key", KEY]) == 2
    assert "--message-key needs --base-url and --state-file" in capsys.readouterr().out
    assert cli.main(["review-receipt", "--head", HEAD, "--repo", REPO, "--pr", "1", "--report",
                     str(tmp_path / "none.md")]) == 2
    missing = tmp_path / "none.json"
    assert cli.main(["review-receipt", "--head", HEAD, "--comments-file", str(missing)]) == 2
    capsys.readouterr()
