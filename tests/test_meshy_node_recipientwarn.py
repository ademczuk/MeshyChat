"""The send path's recipient warnings (divergence 083, issue #140): warn, never refuse. No database.

  * the rules: published asleep, or an expired state (a beat on record, effective unknown) -> unavailable;
    never published (no beat) -> nothing; >= BEHIND_MIN_UNREAD unread -> behind; malformed parts skipped;
  * the cache answers within its TTL and refetches after; any raise or malformed answer is no warning;
  * the log line holds counts and the key, and never raises;
  * through the CLI: the warning is printed and the message sent is byte for byte what it would have been;
    a failing check still sends; --no-recipient-check makes no call; a send the guard refuses makes no call.

Run:  pytest tests/test_meshy_node_recipientwarn.py -q
Mutants (tools/meshy_node/recipientwarn.py unless named; each seen to fail a test here):
  W1  a never-published peer warns as unavailable          test_rules
  W2  the cache is never used                              test_cache_ttl
  W3  a fetch error raises instead of failing open         test_fail_open
  W4  (cli.py) the check runs before the guard's refusal   test_a_refused_send_makes_no_check
  W5  the fetch is not bounded by wall time                 test_a_slow_server_is_abandoned_within_the_deadline
  W6  (node.py) the check signs in when the token is stale  test_an_expiring_token_skips_the_check_without_a_call
  W7  the shape of an answer is not checked by fetch         test_fetch_itself_rejects_a_malformed_answer_live_or_cached
  W8  (cli.py) the cache survives a successful send          test_consecutive_sends_see_the_count_the_first_send_made
  W9  a label that reaches no peer is silent (#239)          test_a_label_that_reaches_no_peer_is_named
  W10 all/both are taken for unresolved labels               test_a_label_that_reaches_no_peer_is_named
"""
from __future__ import annotations

import json
import time

import pytest

from tools.meshy_node import cli, node, recipientwarn as rw, sendguard


def _answer(**peer) -> dict:
    base = {"peer_id": "p1", "effective_state": "active", "note": None, "beat_age_secs": 10,
            "state_expires_at": None, "unread_count": 0, "unread_oldest_age_secs": None}
    return {"recipients": [{"label": "otter", "peers": [{**base, **peer}]}]}


def _kinds(answer) -> list[str]:
    return [w["warning"] for w in rw.warnings(answer)]


def test_rules():
    """MUTANT W1."""
    assert _kinds(_answer(effective_state="asleep", note="compacting")) == ["recipient_unavailable"]
    assert _kinds(_answer(effective_state="unknown", beat_age_secs=4000)) == ["recipient_unavailable"]
    assert _kinds(_answer(effective_state="unknown", beat_age_secs=None)) == [], "never published must not warn"
    assert _kinds(_answer(effective_state="active")) == []
    assert _kinds(_answer(unread_count=rw.BEHIND_MIN_UNREAD)) == ["recipient_behind"]
    assert _kinds(_answer(unread_count=rw.BEHIND_MIN_UNREAD - 1)) == []
    assert _kinds(_answer(unread_count=True)) == []
    both = _kinds(_answer(effective_state="asleep", unread_count=9))
    assert both == ["recipient_unavailable", "recipient_behind"], "the two states stay distinct"
    assert rw.warnings({"recipients": ["x", {"label": "a", "peers": ["y", None]}]}) == []
    assert rw.warnings(None) == [] and rw.warnings({"nope": 1}) == []


def test_cache_ttl(tmp_path):
    """MUTANT W2. One fetch serves a burst within the TTL; after it, a new fetch."""
    st = tmp_path / "s.json"
    calls = []

    def fetcher(labels):
        calls.append(labels)
        return _answer()

    assert rw.fetch(st, ["otter"], fetcher, now=1000.0) == _answer()
    assert rw.fetch(st, ["otter"], fetcher, now=1000.0 + rw.CACHE_TTL_S - 1) == _answer()
    assert len(calls) == 1
    rw.fetch(st, ["otter"], fetcher, now=1000.0 + rw.CACHE_TTL_S + 1)
    assert len(calls) == 2
    rw.fetch(st, ["heron"], fetcher, now=1000.0 + rw.CACHE_TTL_S + 2)        # other labels: not the cached answer
    assert len(calls) == 3 and calls[-1] == ["heron"]


def test_fail_open(tmp_path):
    """MUTANT W3. A raising fetcher, a malformed answer and an unreadable cache are all no warning."""
    st = tmp_path / "s.json"

    def boom(labels):
        raise TimeoutError("slow server")

    assert rw.fetch(st, ["otter"], boom, now=1.0) is None
    assert rw.fetch(st, ["otter"], lambda labels: ["not", "a", "dict"], now=2.0) is None
    rw.cache_path(st).write_text("not json", encoding="utf-8")
    assert rw.fetch(st, ["otter"], lambda labels: _answer(), now=3.0) == _answer()
    assert rw.fetch(st, [], lambda labels: _answer(), now=4.0) is None


def test_log_holds_counts_and_never_raises(tmp_path):
    st = tmp_path / "s.json"
    ans = _answer(effective_state="asleep", note="private-ish note", unread_count=7)
    rw.log(st, key="msg/1", answer=ans, warned=rw.warnings(ans), now=5.0)
    (line,) = rw.log_path(st).read_text(encoding="utf-8").splitlines()
    rec = json.loads(line)
    assert rec["key"] == "msg/1" and rec["warned"] == ["recipient_behind", "recipient_unavailable"]
    assert rec["recipients"] == [{"label": "otter", "effective_state": "asleep", "beat_age_secs": 10, "unread_count": 7,
                                  "unanswered_from_you": None}]
    assert "private-ish note" not in line
    rw.log(tmp_path / "missing-dir" / "s.json", key="msg/2", answer=ans, warned=[], now=6.0)   # unwritable: silent
    rw.log(st, key="msg/3", answer=None, warned=[], now=7.0)
    assert len(rw.log_path(st).read_text(encoding="utf-8").splitlines()) == 1


@pytest.fixture
def cli_send(monkeypatch, tmp_path):
    sent, checks, status = [], [], {"answer": _answer(effective_state="asleep", note="compacting", unread_count=9)}
    monkeypatch.setattr(node, "send", lambda *a, **kw: sent.append(kw) or {"status": "completed", "key": "msg/9"})
    monkeypatch.setattr(node, "succeeded", lambda r: True)
    monkeypatch.setattr(node, "load_state", lambda *a, **kw: {"label": "me"}, raising=False)

    def recipient_status(path, state, labels, *, timeout):
        checks.append((labels, timeout))
        if isinstance(status["answer"], Exception):
            raise status["answer"]
        return status["answer"]

    monkeypatch.setattr(node, "recipient_status", recipient_status)
    (tmp_path / "state.json").write_text("{}", encoding="utf-8")

    def go(*extra, body="hello"):
        return cli.main(["send", "--base-url", "http://127.0.0.1:1", "--state-file", str(tmp_path / "state.json"),
                         "--thread", "t", "--to", "otter", "--body", body, *extra])
    return go, sent, checks, status, tmp_path / "state.json"


def test_a_warning_never_changes_the_send(cli_send, capsys):
    go, sent, checks, _status, st = cli_send
    assert go() == 0
    err = capsys.readouterr().err
    assert "recipient_unavailable" in err and "recipient_behind" in err
    assert checks == [(["otter"], rw.TIMEOUT_S)]
    warned_kw = dict(sent[0])
    rw.cache_path(st).unlink(missing_ok=True)
    assert go("--no-recipient-check") == 0
    assert sent[1] == warned_kw, "a warned send differs from an unchecked one"
    assert len(checks) == 1, "--no-recipient-check still called the server"
    assert len(rw.log_path(st).read_text(encoding="utf-8").splitlines()) == 1


def test_a_failing_check_still_sends(cli_send, capsys):
    go, sent, _checks, status, _st = cli_send
    status["answer"] = node.ServerSaid(503, {"error": "E_DB_UNAVAILABLE"})
    assert go() == 0 and len(sent) == 1
    assert "recipient_" not in capsys.readouterr().err


def test_a_refused_send_makes_no_check(cli_send, capsys):
    """MUTANT W4. A body over the guard's hard limit is refused locally; the recipient check must not run."""
    go, sent, checks, _status, _st = cli_send
    assert go(body="x" * (sendguard.MAX_BODY_BYTES + 1)) == 2
    assert sent == [] and checks == []


def test_a_slow_server_is_abandoned_within_the_deadline(tmp_path):
    """MUTANT W5 (Codex on #145). A socket timeout bounds each read, not the whole answer: a server that
    trickles bytes, or a sign-in on the way, could hold the send. Measured in wall time, not by the
    timeout argument passed."""
    def slow(labels):
        time.sleep(5)
        return _answer(effective_state="asleep")

    t0 = time.monotonic()
    assert rw.fetch(tmp_path / "s.json", ["otter"], slow, now=1.0, deadline_s=0.5) is None
    assert time.monotonic() - t0 < 1.5, "the check waited past its deadline"
    assert not rw.cache_path(tmp_path / "s.json").exists(), "an abandoned answer was cached"


def test_an_expiring_token_skips_the_check_without_a_call(tmp_path, monkeypatch):
    """MUTANT W6. A token about to expire is not refreshed for an advisory check: no sign-in, no GET."""
    def no_http(*a, **kw):
        raise AssertionError("the check made a network call with an expiring token")

    monkeypatch.setattr(node, "http_json", no_http)
    monkeypatch.setattr(node, "signin", no_http)
    state = {"base_url": "http://127.0.0.1:1", "peer_id": "00000000-0000-0000-0000-000000000001",
             "status": "minted", "jwt": "h.p.s", "jwt_expires_at": "2000-01-01T00:00:00Z"}
    monkeypatch.setattr(node, "require_peer", lambda path, st: st)
    with pytest.raises(node.Refused):
        node.recipient_status(tmp_path / "s.json", state, ["otter"], timeout=rw.TIMEOUT_S)


@pytest.mark.parametrize("bad", [{"recipients": [{"peers": 1}]}, {"recipients": [1]}, {"recipients": 1},
                                 {"recipients": [{"peers": [1]}]}])
def test_malformed_answers_still_send(cli_send, capsys, bad):
    """Codex on #145: a malformed answer, live or cached, used to raise before the send."""
    go, sent, _checks, status, st = cli_send
    status["answer"] = bad
    assert go() == 0 and len(sent) == 1
    rw.cache_path(st).write_text(json.dumps({"t": time.time(), "to": ["otter"], "answer": bad}), encoding="utf-8")
    assert go() == 0 and len(sent) == 2
    assert "recipient_" not in capsys.readouterr().err


def test_fetch_itself_rejects_a_malformed_answer_live_or_cached(tmp_path):
    """MUTANT W7. The shape check is its own layer: fetch answers None for a malformed live answer, and a
    malformed cached answer is not served (the live one is fetched instead)."""
    st = tmp_path / "s.json"
    assert rw.fetch(st, ["otter"], lambda labels: {"recipients": [{"peers": 1}]}, now=1.0) is None
    rw.cache_path(st).write_text(json.dumps({"t": 2.0, "to": ["otter"], "answer": {"recipients": 1}}), encoding="utf-8")
    assert rw.fetch(st, ["otter"], lambda labels: _answer(), now=3.0) == _answer()



@pytest.mark.parametrize("body_bytes,unanswered,warns", [
    (2048, 1, True), (2047, 1, False), (2048, 0, False), (10_000, 3, True),
    (True, 1, False), (2048, True, False), (None, 1, False), (2048, None, False), ("4096", 1, False)])
def test_the_advisory_rule(body_bytes, unanswered, warns):
    """Divergence 084 (#141): body >= 2048 bytes AND >= 1 unanswered send. Malformed is False. MUTANTS A1, A2."""
    assert sendguard.advisory(body_bytes, unanswered) is warns


def test_the_unanswered_warning_needs_the_body_size():
    ans = _answer(unanswered_from_you=2)
    assert "recipient_unanswered" in _kinds_with(ans, 4096)
    assert "recipient_unanswered" not in _kinds_with(ans, 100)
    assert "recipient_unanswered" not in [w["warning"] for w in rw.warnings(ans)], "no size given: no advisory"


def _kinds_with(answer, body_bytes):
    return [w["warning"] for w in rw.warnings(answer, body_bytes=body_bytes)]


def test_the_advisory_is_shadow_logged_not_printed_and_the_send_goes_out(cli_send, capsys):
    """Divergence 084: for two weeks the advisory is recorded, never shown. MUTANT A3 (cli.py prints shadows)."""
    go, sent, _checks, status, st = cli_send
    status["answer"] = _answer(unanswered_from_you=1)
    assert go(body="y" * 3000) == 0 and len(sent) == 1
    assert "recipient_unanswered" not in capsys.readouterr().err, "a shadow warning was printed"
    rw.cache_path(st).unlink(missing_ok=True)
    assert go(body="short") == 0 and len(sent) == 2
    lines = [json.loads(l) for l in rw.log_path(st).read_text(encoding="utf-8").splitlines()]
    assert [l["body_bytes"] for l in lines] == [3000, 5]
    assert lines[0]["warned"] == ["recipient_unanswered"] and lines[1]["warned"] == []



def test_consecutive_sends_see_the_count_the_first_send_made(cli_send):
    """MUTANT W8 (Codex on #146). The first send caches unanswered 0; the server's count is 1 after it. With
    the cache kept, a second large send within the TTL logs no advisory. No manual cache clearing here."""
    go, sent, checks, status, st = cli_send
    status["answer"] = _answer(unanswered_from_you=0)
    assert go(body="y" * 3000) == 0
    status["answer"] = _answer(unanswered_from_you=1)           # what the server answers after that send
    assert go(body="y" * 3000) == 0 and len(sent) == 2
    assert len(checks) == 2, "the second send reused the first send's cached counts"
    lines = [json.loads(l) for l in rw.log_path(st).read_text(encoding="utf-8").splitlines()]
    assert lines[0]["warned"] == [] and lines[1]["warned"] == ["recipient_unanswered"]


def _unresolved(label="codex-pxx-cli") -> dict:
    return {"recipients": [{"label": label, "peers": []}]}


def test_a_label_that_reaches_no_peer_is_named():
    """MUTANTS W9, W10 (#239). The server answers an empty peer list for a label no peer answers to: a roster entry
    added after the server started was the measured case. all/both address everyone and are never unresolved."""
    ws = rw.warnings(_unresolved())
    assert [w["warning"] for w in ws] == ["recipient_unresolved"] and ws[0]["label"] == "codex-pxx-cli"
    assert "no peer answers to this label" in ws[0]["note_for_sender"]
    for broadcast in ("all", "both", "ALL", " Both "):
        assert rw.warnings(_unresolved(broadcast)) == [], broadcast
    assert _kinds(_answer(effective_state="active")) == [], "a label that reaches a peer is not unresolved"
    mixed = {"recipients": [{"label": "otter", "peers": [{"peer_id": "p1", "effective_state": "active"}]},
                            {"label": "nobody", "peers": []}]}
    assert [(w["warning"], w["label"]) for w in rw.warnings(mixed)] == [("recipient_unresolved", "nobody")]
    assert rw.warnings({"recipients": [{"label": None, "peers": []}]}) == [], "a malformed label is no warning"


def test_the_broadcast_labels_match_the_servers():
    from apps.memoryskills import roster
    from apps.wsgw import mesh_notify
    assert rw.BROADCAST_LABELS == roster.BROADCAST_LABELS == mesh_notify.BROADCAST_LABELS


def test_an_unresolved_label_is_printed_and_the_send_is_unchanged(cli_send, capsys):
    go, sent, _checks, status, st = cli_send
    status["answer"] = _unresolved("otter")
    assert go() == 0 and len(sent) == 1
    assert "recipient_unresolved" in capsys.readouterr().err
    warned_kw = dict(sent[0])
    rw.cache_path(st).unlink(missing_ok=True)
    assert go("--no-recipient-check") == 0
    assert sent[1] == warned_kw, "the warning changed what was sent"
