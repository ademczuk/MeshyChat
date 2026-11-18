"""`python -m tools.meshy_node rollcall`: ask each seat to answer and report which did.

On 2026-10-01 a roll call, a ping and six three-seat relays were assembled by hand from `send` and
`read`, and each seat typed its own reply format. The hand-built form is easy to get wrong in ways
this command removes: a broadcast wakes nobody (every wake rule accepts only role to or cc), an old
message in the same thread can be mistaken for an answer, and a reply is credited by its `from`
label, which the writer chooses.

What is pinned here, with node.send, node.inbox and node.read_or_error replaced (no network, no
database, no real clock):

  * one request per seat, addressed to that seat alone, with requires_ack and that seat's own token;
  * a message is a seat's reply only if its signature verified, it is in the roll call's thread, it
    carries THAT seat's token, it was recorded after the request, and its signer has not already
    answered for another seat; `from` and `in_reply_to` credit nobody;
  * `expect` is required: every seat asked is pinned to a peer id, and only that peer's signature
    answers for it (operator ruling, 2026-10-01);
  * a key that was in the inbox before the sends is never read; a key whose read failed is read again;
  * a seat that does not answer is silent when the timeout passes, and the command exits 1;
  * the sender's own label is not sent to (divergence 020: never delivered) and is not a failure;
  * a send or a poll that raises does not lose the seats already seen.

A first version credited replies by `from` and `in_reply_to` and shared one token; a text-only
review by a second model found that, and the cases marked (review) are its findings.

Run:  .venv/Scripts/python.exe -m pytest tests/test_meshy_node_rollcall.py -q -p no:cacheprovider
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.meshy_node import cli, node  # noqa: E402

BASE = "a1b2c3d4e5f60718"
THREAD = "rollcall-" + BASE[:8]
SENT_AT = "2026-10-01 13:16:55.000000+00:00"
PEER = {"nxxxxx": "peer-nxxxxx", "antigravity": "peer-antigravity"}


def tok(n: int) -> str:
    """The token of the n-th seat in `to`."""
    return "%s-%d" % (BASE, n)


class World:
    """A fake mesh: what was sent, what the inbox shows on each poll, what each key reads as."""

    def __init__(self):
        self.sent: list[dict] = []
        self.inbox_pages: list[list[dict]] = []    # one list of hits per inbox call; the last repeats
        self.messages: dict[str, dict] = {}
        self.read_keys: list[str] = []
        self.read_fails: dict[str, int] = {}       # key -> how many reads of it fail first
        self.now = 0.0
        self.sleeps: list[float] = []
        self.send_ok = True
        self.send_raises_for: set = set()
        self.inbox_raises_on_call = None
        self.inbox_calls = 0

    def send(self, path, state, **kw):
        if kw["to"][0] in self.send_raises_for:
            raise OSError("connection refused")
        self.sent.append(kw)
        return {"key": "msg/sent-%s" % kw["to"][0], "status": "completed" if self.send_ok else "failed",
                "result": {"created_at": SENT_AT}, "result_signature_verified": True}

    def inbox(self, path, state, *, limit, include_cc=True, bodies=False):
        self.inbox_calls += 1
        if self.inbox_calls == self.inbox_raises_on_call:
            raise node.ServerSaid(503, {"error": "E_DOWN"})
        page = self.inbox_pages.pop(0) if len(self.inbox_pages) > 1 else (self.inbox_pages or [[]])[0]
        return {"label": "uxxx", "hits": page, "truncated": True}    # always true for a busy seat

    def read_or_error(self, path, state, key, *, version=None, poll_timeout=None):
        self.read_keys.append(key)
        if self.read_fails.get(key, 0) > 0:
            self.read_fails[key] -= 1
            return "read_error", {"error": "E_READ_HTTP", "key": key}
        return "read", self.messages[key]

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def _reply(key, signer, body, *, sender="someone", thread=THREAD, verified=True, to_key=None,
           at="2026-10-01T13:17:51+00:00"):
    return {"key": key, "from": sender, "thread_id": thread, "in_reply_to": to_key, "body": body,
            "recorded_at": at, "signature_verified": verified,
            "signed_by": signer if verified else None}


def _hit(key):
    return {"key": key, "version": 1, "edge_type": "addressed_to", "dst": "uxxx"}


@pytest.fixture()
def world(monkeypatch):
    w = World()
    monkeypatch.setattr(node, "send", w.send)
    monkeypatch.setattr(node, "inbox", w.inbox)
    monkeypatch.setattr(node, "read_or_error", w.read_or_error)
    monkeypatch.setattr(node, "require_peer", lambda path, state: None)
    return w


def _call(w, to, **kw):
    kw.setdefault("expect", PEER)
    return node.rollcall(Path("state.json"), {"label": "uxxx", "peer_id": "0" * 36}, to=to,
                         token=BASE, clock=w.clock, sleep=w.sleep, **kw)


def _states(out):
    return {s["label"]: s["state"] for s in out["seats"]}


def test_each_seat_gets_its_own_request_and_token_and_a_signed_reply_carrying_it_counts(world):
    world.messages = {
        "msg/r-nxxxxx": _reply("msg/r-nxxxxx", PEER["nxxxxx"], "nxxxxx, " + tok(0)),
        "msg/r-anti": _reply("msg/r-anti", PEER["antigravity"], "antigravity " + tok(1),
                             at="2026-10-01T13:17:14+00:00")}
    world.inbox_pages = [[], [_hit("msg/r-anti")], [_hit("msg/r-anti"), _hit("msg/r-nxxxxx")]]

    out = _call(world, ["nxxxxx", "antigravity"])

    assert [s["to"] for s in world.sent] == [["nxxxxx"], ["antigravity"]], "one request per seat, never one to all"
    assert all(s["requires_ack"] and s["msg_type"] == "request" and s["thread_id"] == THREAD
               for s in world.sent)
    assert tok(0) in world.sent[0]["body"] and tok(1) in world.sent[1]["body"]
    assert tok(1) not in world.sent[0]["body"], "a seat is never shown another seat's token"
    seats = {s["label"]: s for s in out["seats"]}
    assert seats["nxxxxx"]["state"] == "replied" and seats["nxxxxx"]["reply_key"] == "msg/r-nxxxxx"
    assert seats["nxxxxx"]["latency_s"] == 56.0 and seats["antigravity"]["latency_s"] == 19.0
    assert seats["antigravity"]["signed_by"] == PEER["antigravity"]
    assert seats["nxxxxx"]["expected"] == PEER["nxxxxx"]
    assert out["silent"] == [] and out["unmatched"] == [] and out["error"] is None
    assert out["thread_id"] == THREAD and world.sleeps == [10.0]


def test_a_seat_that_never_answers_is_silent_at_the_timeout(world):
    world.messages = {"msg/r-anti": _reply("msg/r-anti", PEER["antigravity"], tok(1))}
    world.inbox_pages = [[], [_hit("msg/r-anti")]]

    out = _call(world, ["nxxxxx", "antigravity"], timeout=30.0, interval=10.0)

    assert out["silent"] == ["nxxxxx"] and _states(out) == {"nxxxxx": "silent", "antigravity": "replied"}
    assert world.now == 30.0, "it waited the whole timeout for the silent seat and then stopped"
    assert world.read_keys == ["msg/r-anti"], "a key read to a verdict is not read again"


def test_the_last_sleep_does_not_run_past_the_timeout(world):
    """(review) A 10 second interval against a 25 second timeout sleeps 10, 10 and 5, not 30."""
    world.inbox_pages = [[]]

    _call(world, ["nxxxxx"], timeout=25.0, interval=10.0)

    assert world.sleeps == [10.0, 10.0, 5.0] and world.now == 25.0


def test_a_message_that_was_in_the_inbox_before_the_sends_is_never_read(world):
    world.messages = {"msg/old": _reply("msg/old", PEER["nxxxxx"], tok(0))}
    world.inbox_pages = [[_hit("msg/old")]]

    out = _call(world, ["nxxxxx"], timeout=20.0)

    assert out["silent"] == ["nxxxxx"] and world.read_keys == []


@pytest.mark.parametrize("why, reply", [
    ("no token in the body", dict(body="nxxxxx here")),
    ("another seat's token", dict(body="nxxxxx, " + tok(1))),
    ("the signature did not verify", dict(body=tok(0), verified=False)),
    ("recorded before the request was sent", dict(body=tok(0), at="2026-10-01T13:16:00+00:00")),
])
def test_a_message_in_the_thread_that_proves_nothing_answers_nobody(world, why, reply):
    world.messages = {"msg/r": _reply("msg/r", PEER["nxxxxx"], **reply)}
    world.inbox_pages = [[], [_hit("msg/r")]]

    out = _call(world, ["nxxxxx"], timeout=20.0)

    assert out["silent"] == ["nxxxxx"] and out["unmatched"] == ["msg/r"], why


@pytest.mark.parametrize("why, fields", [
    ("a signer is named but the signature did not verify", {"signature_verified": False, "signed_by": "peer-nxxxxx"}),
    ("the signature verified but no signer is named", {"signature_verified": True, "signed_by": None}),
])
def test_the_signature_and_the_signer_are_each_required(world, why, fields):
    """`read` sets `signed_by` only when the signature verified, so today the two checks see the same
    messages. They are pinned apart here so that neither can be dropped while the other hides it."""
    world.messages = {"msg/r": dict(_reply("msg/r", PEER["nxxxxx"], tok(0)), **fields)}
    world.inbox_pages = [[], [_hit("msg/r")]]

    out = _call(world, ["nxxxxx"], timeout=20.0)

    assert out["silent"] == ["nxxxxx"] and out["unmatched"] == ["msg/r"], why


def test_mail_in_another_thread_is_ignored_even_with_the_token(world):
    world.messages = {"msg/x": _reply("msg/x", PEER["nxxxxx"], tok(0), thread="other")}
    world.inbox_pages = [[], [_hit("msg/x")]]

    out = _call(world, ["nxxxxx"], timeout=20.0)

    assert out["silent"] == ["nxxxxx"] and out["unmatched"] == []


def test_from_and_in_reply_to_credit_nobody(world):
    """(review) Both are chosen by the writer. A message that names nxxxxx as its sender and cites
    nxxxxx's request, but carries no token, is not nxxxxx's reply."""
    world.messages = {"msg/r": _reply("msg/r", "peer-mallory", "present", sender="nxxxxx",
                                      to_key="msg/sent-nxxxxx")}
    world.inbox_pages = [[], [_hit("msg/r")]]

    out = _call(world, ["nxxxxx"], timeout=20.0)

    assert out["silent"] == ["nxxxxx"] and out["unmatched"] == ["msg/r"]


def test_one_signer_answers_for_one_seat_at_most(world):
    """(review) Two labels pinned to the SAME peer are one seat asked twice, and it is credited once."""
    world.messages = {"msg/a": _reply("msg/a", PEER["antigravity"], tok(1)),
                      "msg/b": _reply("msg/b", PEER["antigravity"], tok(0))}
    world.inbox_pages = [[], [_hit("msg/a"), _hit("msg/b")]]

    out = _call(world, ["nxxxxx", "antigravity"], timeout=20.0,
                expect={"nxxxxx": PEER["antigravity"], "antigravity": PEER["antigravity"]})

    assert _states(out) == {"nxxxxx": "silent", "antigravity": "replied"}
    assert out["unmatched"] == ["msg/b"] and out["silent"] == ["nxxxxx"]


def test_only_the_expected_peers_signature_answers_for_a_seat(world):
    """(review) A token is not a secret: the request sits in a shared namespace. Another peer that
    holds nxxxxx's token and signs with its own key answers nobody."""
    world.messages = {"msg/forged": _reply("msg/forged", "peer-mallory", tok(0)),
                      "msg/real": _reply("msg/real", PEER["nxxxxx"], tok(0))}
    world.inbox_pages = [[], [_hit("msg/forged")], [_hit("msg/forged"), _hit("msg/real")]]

    out = _call(world, ["nxxxxx"], expect={"Nxxxxx": PEER["nxxxxx"]})

    seat = out["seats"][0]
    assert seat["state"] == "replied" and seat["reply_key"] == "msg/real"
    assert seat["signed_by"] == seat["expected"] == PEER["nxxxxx"]
    assert out["unmatched"] == ["msg/forged"] and out["silent"] == []


def test_a_second_reply_for_a_seat_that_already_answered_is_unmatched(world):
    world.messages = {"msg/r1": _reply("msg/r1", PEER["nxxxxx"], tok(0)),
                      "msg/r2": _reply("msg/r2", PEER["nxxxxx"], tok(0))}
    world.inbox_pages = [[], [_hit("msg/r1"), _hit("msg/r2")]]

    out = _call(world, ["nxxxxx", "antigravity"], timeout=20.0)

    assert out["seats"][0]["reply_key"] == "msg/r1" and out["unmatched"] == ["msg/r2"]
    assert out["silent"] == ["antigravity"]


def test_a_read_that_failed_is_tried_again_on_the_next_poll(world):
    """(review) A transient read error must not turn a seat that answered into a silent one."""
    world.messages = {"msg/r": _reply("msg/r", PEER["nxxxxx"], tok(0))}
    world.read_fails = {"msg/r": 1}
    world.inbox_pages = [[], [_hit("msg/r")]]

    out = _call(world, ["nxxxxx"])

    assert out["silent"] == [] and world.read_keys == ["msg/r", "msg/r"] and out["read_errors"] == []


def test_a_key_that_never_reads_is_reported(world):
    world.messages = {"msg/r": _reply("msg/r", PEER["nxxxxx"], tok(0))}
    world.read_fails = {"msg/r": 99}
    world.inbox_pages = [[], [_hit("msg/r")]]

    out = _call(world, ["nxxxxx"], timeout=20.0)

    assert out["silent"] == ["nxxxxx"] and out["read_errors"] == ["msg/r"]


def test_a_seat_with_no_expected_peer_is_refused_before_anything_is_sent(world):
    """Operator ruling 2026-10-01: without the peer id a reply shows only that some peer answered,
    so the command does not run half-pinned."""
    world.inbox_pages = [[]]

    with pytest.raises(node.Refused) as err:
        _call(world, ["nxxxxx", "antigravity"], expect={"nxxxxx": PEER["nxxxxx"]})

    assert "antigravity" in str(err.value) and world.sent == []


def test_the_senders_own_label_is_not_sent_to_and_is_not_a_failure(world):
    world.inbox_pages = [[]]

    out = _call(world, ["uxxx"], expect={})

    assert world.sent == [] and out["seats"][0]["state"] == "self" and out["silent"] == []
    assert world.sleeps == [], "nothing to wait for"


def test_a_send_the_server_refused_is_reported_and_not_waited_for(world):
    world.send_ok = False
    world.inbox_pages = [[]]

    out = _call(world, ["nxxxxx"], timeout=60.0)

    assert out["seats"][0]["state"] == "send_failed" and out["silent"] == ["nxxxxx"]
    assert world.sleeps == [], "a request that was never stored cannot be answered"


def test_a_send_that_raises_fails_that_seat_and_the_others_are_still_asked(world):
    """(review) One unreachable send must not lose the whole report."""
    world.send_raises_for = {"nxxxxx"}
    world.messages = {"msg/r-anti": _reply("msg/r-anti", PEER["antigravity"], tok(1))}
    world.inbox_pages = [[], [_hit("msg/r-anti")]]

    out = _call(world, ["nxxxxx", "antigravity"])

    assert _states(out) == {"nxxxxx": "send_failed", "antigravity": "replied"}
    assert "OSError" in out["seats"][0]["error"] and out["silent"] == ["nxxxxx"]


def test_a_poll_that_raises_ends_the_wait_and_keeps_what_was_seen(world):
    """(review) The baseline inbox call is the first; the third call (second poll) fails."""
    world.messages = {"msg/r-anti": _reply("msg/r-anti", PEER["antigravity"], tok(1))}
    world.inbox_pages = [[], [_hit("msg/r-anti")]]
    world.inbox_raises_on_call = 3

    out = _call(world, ["nxxxxx", "antigravity"])

    assert _states(out) == {"nxxxxx": "silent", "antigravity": "replied"}
    assert "ServerSaid" in out["error"] and out["silent"] == ["nxxxxx"]


# --------------------------------------------------------------------------------------------- the command

@pytest.fixture()
def command(monkeypatch, tmp_path):
    calls = []
    answer = {"thread_id": THREAD, "timeout_s": 5.0, "seats": [], "silent": [], "unmatched": [],
              "read_errors": [], "error": None}

    def fake(path, state, **kw):
        calls.append(kw)
        return answer

    monkeypatch.setattr(node, "rollcall", fake)
    monkeypatch.setattr(node, "load_state", lambda *a, **kw: {"label": "uxxx"}, raising=False)
    (tmp_path / "state.json").write_text("{}", encoding="utf-8")
    base = ["rollcall", "--base-url", "http://127.0.0.1:1", "--state-file", str(tmp_path / "state.json")]
    return calls, answer, base


def test_the_command_passes_its_arguments_and_exits_zero_when_everyone_answered(command, capsys):
    calls, answer, base = command
    assert cli.main(base + ["--to", "nxxxxx,antigravity", "--timeout", "5", "--interval", "1",
                            "--expect", "nxxxxx=peer-nxxxxx", "--expect", "antigravity=peer-anti"]) == 0
    assert calls[0]["to"] == ["nxxxxx", "antigravity"] and calls[0]["timeout"] == 5.0
    assert calls[0]["interval"] == 1.0 and calls[0]["thread_id"] is None
    assert calls[0]["expect"] == {"nxxxxx": "peer-nxxxxx", "antigravity": "peer-anti"}
    assert json.loads(capsys.readouterr().out) == answer


def test_the_command_exits_one_when_a_seat_stayed_silent_or_polling_failed(command, capsys):
    calls, answer, base = command
    answer["silent"] = ["nxxxxx"]
    assert cli.main(base + ["--to", "nxxxxx", "--expect", "nxxxxx=p"]) == 1
    answer["silent"], answer["error"] = [], "ServerSaid: 503"
    assert cli.main(base + ["--to", "nxxxxx", "--expect", "nxxxxx=p"]) == 1
    capsys.readouterr()


def test_the_command_without_expect_does_not_run(command, capsys):
    calls, _answer, base = command
    with pytest.raises(SystemExit) as err:
        cli.main(base + ["--to", "nxxxxx"])
    assert err.value.code == 2 and calls == []
    capsys.readouterr()


@pytest.mark.parametrize("bad", [["--expect", "nxxxxx=p", "--timeout", "0"],
                                 ["--expect", "nxxxxx=p", "--interval", "-1"],
                                 ["--expect", "nxxxxx"], ["--expect", "=peer"],
                                 ["--expect", "nxxxxx=p", "--expect", "kxxx=peer-kxxx"]])
def test_a_malformed_command_is_refused_before_anything_is_sent(command, capsys, bad):
    calls, _answer, base = command
    assert cli.main(base + ["--to", "nxxxxx"] + bad) == 2
    assert calls == []
