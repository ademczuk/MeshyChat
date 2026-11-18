"""The urgent lane (kxxx ef884a; GO from oxxxxxx 34bbcb under SD 445, Tue 10 Nov 2026): a seat's message to ONE seat
marked urgent is delivered first. Order only, never authority; at most 4 per signer per hour; over that the message
goes as ordinary, marked urgent_declined. Database-free; the stored-row cases are tests/test_urgent_lane_db.py."""
from __future__ import annotations

import argparse
import json
import sys
import uuid

import pytest

from apps.memoryskills import validation as V
from apps.memoryskills.errors import HandlerError
from apps.wsgw import envelope, mesh_notify
from tools.meshy_node import cli, node, wire
from tools.postbox import delivery_bank, parked, postbox_os

NS = V.MESH_CHAT_NAMESPACE
KEY = "msg/1794300000000-kxxx-abcdef"


@pytest.fixture
def lane_on(monkeypatch):
    monkeypatch.setenv(V.URGENT_LANE_ENV, "1")


def _v(**kw):
    v = {"to": ["oxxxxxx"], "body": "b"}
    v.update(kw)
    return v


# ---- the server's shape rules ----------------------------------------------------------------------------------

def test_both_fields_are_v1_fields_on_both_sides():
    for f in ("urgent", "urgent_declined"):
        assert f in V.MESH_MESSAGE_V1_OPTIONAL_FIELDS and f in wire.V1_OPTIONAL
    assert V.URGENT_RATE_PER_HOUR == 4


def test_urgent_to_one_seat_is_accepted(lane_on):
    V.validate_urgent(NS, KEY, _v(urgent=True))
    V.validate_urgent(NS, KEY, _v(urgent_declined="rate_limited"))
    V.validate_urgent(NS, KEY, _v())                                    # neither field: nothing to check


@pytest.mark.parametrize("value, why", [
    (_v(urgent=1), "exactly true"), (_v(urgent="true"), "exactly true"), (_v(urgent=False), "exactly true"),
    (_v(urgent_declined="spent"), "rate_limited"),
    (_v(urgent=True, urgent_declined="rate_limited"), "never both"),
    (_v(urgent=True, to=["a", "b"]), "exactly one seat"), (_v(urgent=True, to=[]), "exactly one seat"),
    (_v(urgent=True, to="oxxxxxx"), "exactly one seat"),
    (_v(urgent=True, directive={"v": 1}), "own lane"),
])
def test_the_shape_is_refused_with_the_rule_it_broke(lane_on, value, why):
    with pytest.raises(HandlerError, match=why):
        V.validate_urgent(NS, KEY, value)


def test_the_switch_off_refuses_both_fields_and_leaves_plain_mail_alone(monkeypatch):
    monkeypatch.delenv(V.URGENT_LANE_ENV, raising=False)
    for value in (_v(urgent=True), _v(urgent_declined="rate_limited")):
        with pytest.raises(HandlerError) as err:
            V.validate_urgent(NS, KEY, value)
        assert err.value.code == "E_FEATURE_OFF"
    V.validate_urgent(NS, KEY, _v())                                    # CONTROL


def test_only_on_a_mesh_msg_key(lane_on):
    with pytest.raises(HandlerError, match="msg/ key only"):
        V.validate_urgent("shared/wiki", "wiki/x", _v(urgent=True))


def test_the_write_and_edit_seams_run_the_checks():
    import inspect
    from apps.memoryskills import edit, handlers
    src = inspect.getsource(handlers.handle_write)
    assert "validate_urgent(namespace, key, value)" in src
    assert src.index("_replay_within(") < src.index("_enforce_urgent(conn, caller_peer_id, value)")
    esrc = inspect.getsource(edit.handle_edit)
    assert '("urgent", "urgent_declined")' in esrc and "an urgent message is not edited" in esrc


# ---- the frame -------------------------------------------------------------------------------------------------

AUTHOR = uuid.UUID(int=7)


def _cols(urgent=False, declined=False):
    return ("2026-11-10T15:00:00Z", True, True, ["oxxxxxx"], False, None, "a question", 1, "codex-x", None, True,
            False, urgent, declined)


def test_the_nudge_reads_fourteen_columns_the_last_two_are_the_urgent_flags():
    assert mesh_notify.NUDGE_COLUMNS == 14
    cols = mesh_notify.nudge_columns("sm.")
    assert "((sm.value -> 'urgent') = 'true'::jsonb)" in cols and cols.rstrip().endswith("(sm.value ? 'urgent_declined')")
    n = mesh_notify.nudge_from_columns("msg/1-x", AUTHOR, _cols(urgent=True))
    assert n.urgent is True and n.urgent_declined is False


@pytest.mark.parametrize("role, shown", [("to", True), ("cc", False), ("broadcast", False)])
def test_a_cc_or_broadcast_copy_arrives_untagged(role, shown):
    """oxxxxxx 34bbcb point 4: an urgent message whose only route to a seat is cc arrives untagged."""
    frame = mesh_notify.nudge_from_columns("msg/1-x", AUTHOR, _cols(urgent=True)).frame(role)
    assert ("urgent" in frame) is shown
    d = delivery_bank.routing(dict(frame, event="message"))
    assert postbox_os.kind_tag(d) == ("[URGENT]" if shown else "[MESSAGE]")


def test_a_declined_frame_says_so_and_is_steering_not_urgent():
    frame = mesh_notify.nudge_from_columns("msg/1-x", AUTHOR, _cols(declined=True)).frame("to")
    d = delivery_bank.routing(dict(frame, event="message"))
    assert d.get("urgent_declined") is True and postbox_os.kind_tag(d) == "[STEERING]"
    assert postbox_os.role_note(d) == " (urgent declined: its sender says its hourly allowance was spent)"   # SD 460


def test_envelope_defaults_are_unchanged():
    f = envelope.mesh_message("msg/1-x", AUTHOR, ["a"], "2026-11-10T15:00:00Z", role="to")
    assert "urgent" not in f and "urgent_declined" not in f


# ---- delivery order --------------------------------------------------------------------------------------------

def _ev(key, **kw):
    return dict(event="message", role="to", key=key, version=1, from_peer_id="p1", **kw)


def test_urgent_goes_first_in_a_burst_and_keeps_arrival_order_otherwise():
    evs = [_ev("msg/a"), _ev("msg/b", urgent=True), _ev("msg/c"), _ev("msg/d", urgent=True),
           dict(_ev("msg/e", urgent=True), role="cc")]
    assert [d["key"] for d in postbox_os.urgent_first(evs)] == ["msg/b", "msg/d", "msg/a", "msg/c", "msg/e"]


def test_the_codex_lane_takes_urgent_and_urgent_from_narrows_it():
    from tools.postbox import postbox as pb
    assert pb.codex_priority({}, _ev("msg/u", urgent=True)) is True
    assert pb.codex_priority({}, dict(_ev("msg/u", urgent=True), role="cc")) is False
    assert pb.codex_priority({}, _ev("msg/plain")) is False
    assert pb.codex_priority({"urgent_from": ["p1"]}, _ev("msg/u", urgent=True)) is True
    assert pb.codex_priority({"urgent_from": ["p2"]}, _ev("msg/u", urgent=True)) is False
    assert pb.codex_priority({}, _ev("msg/u", urgent_declined=True)) is False     # declined is ordinary


# ---- the client ------------------------------------------------------------------------------------------------

def _msg(**kw):
    return wire.mesh_message(to=kw.pop("to", ["oxxxxxx"]), sender="kxxx", msg_type="question", thread_id="t",
                             body="b", author_family="claude", **kw)


def test_the_wire_sets_one_field_and_refuses_before_signing():
    assert _msg(urgent=True)["urgent"] is True and "urgent_declined" not in _msg(urgent=True)
    assert _msg(urgent_declined="rate_limited")["urgent_declined"] == "rate_limited"
    assert "urgent" not in _msg() and "urgent_declined" not in _msg()
    for kw, why in ((dict(urgent=True, to=["a", "b"]), "exactly one seat"),
                    (dict(urgent=True, urgent_declined="rate_limited"), "never both"),
                    (dict(urgent_declined="other"), "rate_limited")):
        with pytest.raises(wire.NotV1, match=why):
            _msg(**kw)


def test_the_fallback_resends_only_on_a_signed_rate_limit_without_an_op_id():
    a = argparse.Namespace(urgent=True, op_id=None, no_urgent_fallback=False)
    limited = {"status": "failed", "result_signature_verified": True, "error_code": "E_RATE_LIMITED"}
    assert cli._urgent_was_rate_limited(a, limited) is True
    assert cli._urgent_was_rate_limited(a, dict(limited, result_signature_verified=False)) is False
    assert cli._urgent_was_rate_limited(a, dict(limited, error_code="E_FEATURE_OFF")) is False
    assert cli._urgent_was_rate_limited(argparse.Namespace(urgent=True, op_id="o", no_urgent_fallback=False),
                                        limited) is False
    assert cli._urgent_was_rate_limited(argparse.Namespace(urgent=True, op_id=None, no_urgent_fallback=True),
                                        limited) is False
    assert cli._urgent_was_rate_limited(argparse.Namespace(urgent=False, op_id=None, no_urgent_fallback=False),
                                        limited) is False


def test_urgent_kwargs_and_the_parser():
    assert cli._urgent_kwargs(argparse.Namespace(urgent=True, urgent_declined=False)) == \
        {"urgent": True, "urgent_declined": None}
    assert cli._urgent_kwargs(argparse.Namespace(urgent=False, urgent_declined=True)) == \
        {"urgent": False, "urgent_declined": "rate_limited"}
    with pytest.raises(node.Refused):
        cli._urgent_kwargs(argparse.Namespace(urgent=True, urgent_declined=True))
    a = cli.build_parser().parse_args(["send", "--base-url", "http://x", "--state-file", "s.json", "--to", "oxxxxxx",
                                       "--thread", "t", "--body", "b", "--urgent"])
    assert a.urgent is True and a.no_urgent_fallback is False and a.urgent_declined is False


def test_an_op_id_retry_that_changes_urgency_is_a_different_operation():
    base = dict(to=["oxxxxxx"], thread_id="t", msg_type="question", body="b", sender="kxxx", author_family="claude")
    prints = {node._op_fingerprint_for_args(**base), node._op_fingerprint_for_args(**base, urgent=True),
              node._op_fingerprint_for_args(**base, urgent_declined="rate_limited")}
    assert len(prints) == 3
    entry = {"op_id": "op1", "value": _msg(), "fingerprint": node._op_fingerprint_for_args(**base)}
    with pytest.raises(node.Refused, match="different message arguments"):
        node._op_assert_same_message(entry, dict(base, urgent=True))


# ---- the question bridge ---------------------------------------------------------------------------------------

FAKE = r'''
import json, sys, pathlib
here = pathlib.Path(sys.argv[0]).parent
with open(here / "calls.jsonl", "a", encoding="utf-8") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
op = sys.argv[sys.argv.index("--op-id") + 1]
if "--urgent" in sys.argv and (here / "limited").exists():
    print(json.dumps({"op_id": op, "status": "failed", "result_signature_verified": True, "error_code": "E_RATE_LIMITED"}))
    sys.exit(1)
print(json.dumps({"op_id": op, "key": "msg/1794300000001-x-aaaaaa", "status": "completed", "result_signature_verified": True}))
'''


def _sender(tmp_path, limited):
    (tmp_path / "s.py").write_text(FAKE, encoding="utf-8")
    if limited:
        (tmp_path / "limited").write_text("1")
    return [sys.executable, str(tmp_path / "s.py")]


def _calls(tmp_path):
    return [json.loads(x) for x in (tmp_path / "calls.jsonl").read_text(encoding="utf-8").splitlines()]


def test_the_bridge_sends_urgent_when_configured(tmp_path):
    got = parked.send_via(_sender(tmp_path, False), "oxxxxxx", "parked-x", "q", "op-1", urgent=True)
    assert got[0] is True and got[3] == parked.SENT
    (call,) = _calls(tmp_path)
    assert "--urgent" in call


def test_a_signed_rate_limit_is_its_own_outcome_and_a_declined_send_carries_the_flag(tmp_path):
    got = parked.send_via(_sender(tmp_path, True), "oxxxxxx", "parked-x", "q", "op-1", urgent=True)
    assert got[3] == parked.RATE_LIMITED and parked._outcome(got) == parked.RATE_LIMITED
    got = parked.send_via(_sender(tmp_path, True), "oxxxxxx", "parked-x", "q", "op-1-declined", declined=True)
    assert got[3] == parked.SENT
    first, second = _calls(tmp_path)
    assert "--urgent" in first and "--urgent-declined" in second and "--urgent" not in second


def test_without_urgent_the_bridge_is_unchanged(tmp_path):
    got = parked.send_via(_sender(tmp_path, True), "oxxxxxx", "parked-x", "q", "op-1")
    assert got[0] is True and len(got) == 4
    (call,) = _calls(tmp_path)
    assert "--urgent" not in call and "--urgent-declined" not in call


class _USender:
    """check()'s send: the first (urgent) call is rate-limited; the declined call answers `second`. It reads the
    ledger file at the second call, to prove the declined intent was written first (F2; pxx 4ac538 P2-1)."""

    def __init__(self, ledger_path, second):
        self.ledger_path, self.second, self.calls, self.seen = ledger_path, second, [], None

    def __call__(self, argv, to, thread, body, op_id, declined=False):
        self.calls.append((op_id, declined))
        if not declined:
            return False, False, "rate limited", parked.RATE_LIMITED
        self.seen = json.loads(open(self.ledger_path, encoding="utf-8").read())
        return self.second


def _check_once(tmp_path, second):
    from tests.test_postbox_parked import NOW, _asking, _setup, ident
    watch, ledger = _setup(tmp_path, *_asking())
    send = _USender(ledger.path, second)
    sent = parked.check(ident(tmp_path), watch, ledger, send=send, now=NOW)
    return sent, send, ledger


def test_a_rate_limited_parked_question_records_the_declined_op_before_sending_it(tmp_path):
    sent, send, ledger = _check_once(tmp_path, (True, False, "msg/1-x", parked.SENT))
    (op, _), (dop, declined) = send.calls
    assert declined is True and dop == op + "-declined"
    (row,) = send.seen.values()
    assert row["state"] == "sending-declined" and row["declined_op_id"] == dop     # written BEFORE the send
    (tid, final), = ledger.rows.items()
    assert sent == [tid] and final["state"] == "sent"


def test_a_lost_declined_answer_is_reconciled_against_the_declined_op(tmp_path):
    from tests.test_postbox_parked import NOW, ident
    sent, send, ledger = _check_once(tmp_path, (False, False, "timed out", parked.UNKNOWN))
    (tid, row), = ledger.rows.items()
    assert sent == [] and row["state"] == "unknown" and row["declined_op_id"].endswith("-declined")
    asked = []

    def status(op_id):
        asked.append(op_id)
        return (parked.SENT, "msg/1-x", "") if op_id == row["declined_op_id"] else ("refused-by-server", None, "x")

    later = NOW + __import__("datetime").timedelta(seconds=parked.RECONCILE_EVERY_S + 1)
    done = parked.reconcile(ident(tmp_path), ledger, status=status, send=send, now=later)
    assert asked == [row["declined_op_id"]] and done == [tid] and ledger.rows[tid]["state"] == "sent"


def test_a_crash_between_the_declined_intent_and_its_answer_stays_held():
    assert "sending-declined" in parked.HELD
    assert parked._row_problem({"state": "sending-declined", "op_id": "o"}) == "no declined_op_id"
    assert parked._row_problem({"state": "sending-declined", "op_id": "o", "declined_op_id": "o-declined"}) is None


def test_a_list_or_dict_urgent_declined_is_a_schema_error_not_a_crash(lane_on):
    """pxx 4ac538 P2-3: an unhashable value in the frozenset test raised TypeError."""
    for bad in ([], {}, ["rate_limited"]):
        with pytest.raises(HandlerError, match="rate_limited"):
            V.validate_urgent(NS, KEY, _v(urgent_declined=bad))


def test_the_tag_needs_a_to_frame_even_if_a_copy_somehow_carries_the_flag():
    """Defence in depth: the server sets the flag on `to` frames only, and the tag checks the role again."""
    for role in ("cc", "broadcast"):
        assert postbox_os.kind_tag(dict(_ev("msg/x", urgent=True), role=role)) == "[MESSAGE]"
    assert postbox_os.kind_tag(_ev("msg/x", urgent=True)) == "[URGENT]"


class _ChainSender:
    """Answers in order; records (op_id, declined) and the ledger as read at each call."""

    def __init__(self, ledger_path, answers):
        self.ledger_path, self.answers, self.calls = ledger_path, list(answers), []

    def __call__(self, argv, to, thread, body, op_id, declined=False):
        try:
            seen = json.loads(open(self.ledger_path, encoding="utf-8").read())
        except FileNotFoundError:
            seen = {}
        self.calls.append((op_id, declined, seen))
        return self.answers.pop(0)


def test_a_rate_limited_pointer_goes_again_as_ordinary_recorded_first(tmp_path):
    """pxx 59f198: question urgent -> RATE_LIMITED; declined -> REFUSED for its text; pointer urgent -> RATE_LIMITED;
    the pointer must then go declined under its own op, written to the ledger before that send."""
    from tests.test_postbox_parked import NOW, _asking, _setup, ident
    watch, ledger = _setup(tmp_path, *_asking())
    rl = (False, False, "rate limited", parked.RATE_LIMITED)
    send = _ChainSender(ledger.path, [rl, (False, True, "refused: text", parked.REFUSED), rl,
                                      (True, False, "msg/1-x", parked.SENT)])
    sent = parked.check(ident(tmp_path), watch, ledger, send=send, now=NOW)
    ops = [(op, d) for op, d, _ in send.calls]
    assert [d for _, d in ops] == [False, True, False, True]
    assert ops[1][0].endswith("-declined") and ops[2][0].endswith(".pointer") and ops[3][0].endswith(".pointer-declined")
    (row_at_4th,) = send.calls[3][2].values()
    assert row_at_4th["pointer_op_id"] == ops[3][0] and row_at_4th["state"] == "sending-pointer"
    (tid, final), = ledger.rows.items()
    assert sent == [tid] and final["state"] == "sent-pointer"


# ---- GitHub Codex on e70b2c21 ----------------------------------------------------------------------------------

def test_a_redacted_message_still_counts_and_the_tombstone_shape_is_unchanged():
    """handlers.py:903 (GitHub Codex on e70b2c21) and pxx 6a9f85 P2-1: the database's immutability trigger allows only
    the routing keys in a tombstone, so `urgent` is NOT kept; the count reads redacted rows of the signer instead."""
    import inspect
    from apps.memoryskills import handlers, redact
    assert "urgent" not in redact.ROUTING_FIELDS
    t = redact.tombstone({"urgent": True, "to": ["oxxxxxx"], "body": "secret"}, reason_code="sent_in_error",
                         redacted_at="2026-11-10T16:00:00Z")
    assert "urgent" not in t and t["redacted"] is True and "body" not in t
    assert "(value ? 'urgent' OR value ? 'redacted')" in inspect.getsource(handlers._urgent_count)


def test_a_rate_limit_answer_for_another_operation_is_not_this_ones(tmp_path):
    """parked.py:983: an answer naming another op stays UNKNOWN, never starts a fallback."""
    limited = json.dumps({"op_id": "other", "status": "failed", "result_signature_verified": True,
                          "error_code": "E_RATE_LIMITED"})
    assert parked.rate_limited(limited, "op-1") is False
    assert parked.rate_limited(limited.replace('"other"', '"op-1"'), "op-1") is True
    no_op = json.dumps({"status": "failed", "result_signature_verified": True, "error_code": "E_RATE_LIMITED"})
    assert parked.rate_limited(no_op, "op-1") is True


def test_status_reads_a_journalled_rate_limit_of_an_urgent_op_as_rate_limited(monkeypatch):
    spec = {"argv": ["py"] + parked._PEEK_HEAD + ["http://x", "--state-file", "s.json"], "cwd": None}
    answer = {"op_id": "op-1", "state": "refused", "already_sent": False, "error_code": "E_RATE_LIMITED",
              "urgent": True}
    monkeypatch.setattr(parked, "_run_bounded", lambda *a, **k: (0, json.dumps(answer), False, ""))
    assert parked.status_via(spec, "op-1")[0] == parked.RATE_LIMITED
    monkeypatch.setattr(parked, "_run_bounded",
                        lambda *a, **k: (0, json.dumps({k2: x for k2, x in answer.items() if k2 != "urgent"}), False, ""))
    assert parked.status_via(spec, "op-1")[0] == "refused-by-server"      # not urgent: the old final refusal


def test_reconcile_starts_the_declined_send_after_a_lost_rate_limited_answer(tmp_path):
    """parked.py:1166: the first urgent answer was lost (UNKNOWN); the op journal later says rate limited."""
    from tests.test_postbox_parked import NOW, _asking, _setup, ident
    watch, ledger = _setup(tmp_path, *_asking())
    send = _ChainSender(ledger.path, [(False, False, "lost", parked.UNKNOWN), (True, False, "msg/1-x", parked.SENT)])
    assert parked.check(ident(tmp_path), watch, ledger, send=send, now=NOW) == []
    (tid, row), = ledger.rows.items()
    assert row["state"] == "unknown" and "declined_op_id" not in row
    later = NOW + __import__("datetime").timedelta(seconds=parked.RECONCILE_EVERY_S + 1)
    done = parked.reconcile(ident(tmp_path), ledger, now=later, send=send,
                            status=lambda op: (parked.RATE_LIMITED, None, "spent"))
    assert done == [tid] and ledger.rows[tid]["state"] == "sent"
    op2, declined, seen = send.calls[1]
    assert declined is True and op2 == row["op_id"] + "-declined"
    assert seen[tid]["state"] == "sending-declined" and seen[tid]["declined_op_id"] == op2      # recorded first


def test_the_mod_reads_an_urgent_brief_line_like_any_other():
    """pxx 5e889f (codex-pxx-cli): meshy.js BRIEF_LINE must match the channel's [URGENT] brief line, or the batch's
    urgent message gets no graph line and no 'asks you' mark. The JS literal is the same regex syntax in Python."""
    import pathlib
    import re
    from tools.postbox import meshy_channel
    js = (pathlib.Path(__file__).resolve().parents[1] / "tools/postbox/mod/meshy-mod/hooks/meshy.js").read_text(
        encoding="utf-8")
    m = re.search(r"^const BRIEF_LINE = /(.+)/gm$", js, re.M)
    rx = re.compile(m.group(1), re.M)
    ident = {"name": "oxxxxxx-prod", "client": "c", "python": "p", "base_url": "http://x", "state": "s.json"}
    for d in (_ev("msg/1794300000000-x-aaaaaa", urgent=True), _ev("msg/1794300000001-x-bbbbbb"),
              dict(_ev("msg/1794300000002-x-cccccc"), role="cc")):
        line = meshy_channel.brief_line(ident, dict(d, created_at="2026-11-10T16:00:00Z"), False)
        assert rx.findall(line) == [d["key"]], line


# ---- GitHub Codex on cb0f3db4 ----------------------------------------------------------------------------------

def test_a_lost_rate_limited_pointer_goes_again_declined_on_reconcile(tmp_path):
    from tests.test_postbox_parked import NOW, _asking, _setup, ident
    watch, ledger = _setup(tmp_path, *_asking())
    rl = (False, False, "rate limited", parked.RATE_LIMITED)
    send = _ChainSender(ledger.path, [rl, (False, True, "refused: text", parked.REFUSED),
                                      (False, False, "lost", parked.UNKNOWN), (True, False, "msg/1-x", parked.SENT)])
    assert parked.check(ident(tmp_path), watch, ledger, send=send, now=NOW) == []
    (tid, row), = ledger.rows.items()
    assert row["state"] == "unknown-pointer" and row["pointer_op_id"].endswith(".pointer")
    later = NOW + __import__("datetime").timedelta(seconds=parked.RECONCILE_EVERY_S + 1)
    done = parked.reconcile(ident(tmp_path), ledger, now=later, send=send,
                            status=lambda op: (parked.RATE_LIMITED, None, "spent"))
    assert done == [tid] and ledger.rows[tid]["state"] == "sent-pointer"
    op4, declined, seen = send.calls[3]
    assert declined is True and op4.endswith(".pointer-declined") and seen[tid]["pointer_op_id"] == op4


def test_a_declined_pointer_that_is_rate_limited_again_is_not_resent_forever(tmp_path):
    from tests.test_postbox_parked import NOW, ident
    ledger = parked.Ledger(str(tmp_path / "l.json"))
    ledger.put("t1", {"at": NOW.isoformat(), "state": "unknown-pointer", "op_id": "o", "thread": "th",
                      "pointer_op_id": "o.pointer-declined", "attempts": 1})
    sent = []
    later = NOW + __import__("datetime").timedelta(seconds=parked.RECONCILE_EVERY_S + 1)
    parked.reconcile(ident(tmp_path), ledger, now=later, send=lambda *a, **k: sent.append(a),
                     status=lambda op: (parked.RATE_LIMITED, None, "spent"))
    assert sent == [] and ledger.rows["t1"]["state"] == "unknown-pointer"


def test_the_server_never_refuses_a_declined_label_for_the_count():
    import inspect
    from apps.memoryskills import handlers
    src = inspect.getsource(handlers._enforce_urgent)
    assert 'if "urgent" not in value:\n        return' in src and "send it as urgent" not in src



def test_the_peek_carries_the_refusals_code_and_urgency_end_to_end(tmp_path, monkeypatch):
    """pxx a3724e (GitHub Codex on 2a70e2b7): status_via reads send-status --peek, which is node.op_peek; the journal
    holds the signed refusal and the signed value, and the peek must pass on exactly those two facts."""
    state = tmp_path / "state.json"
    entry = {"schema": node.OPS_SCHEMA, "op_id": "op-1", "key": "msg/1-x", "state": "refused",
             "value": {"urgent": True, "to": ["oxxxxxx"], "body": "the question"},
             "result": {"status": "failed", "result_signature_verified": True, "error_code": "E_RATE_LIMITED"}}
    node._op_write(state, entry)
    peek = node.op_peek(state, "op-1")
    assert peek["error_code"] == "E_RATE_LIMITED" and peek["urgent"] is True and "body" not in json.dumps(peek)
    spec = {"argv": ["py"] + parked._PEEK_HEAD + ["http://x", "--state-file", str(state)], "cwd": None}
    monkeypatch.setattr(parked, "_run_bounded", lambda *a, **k: (0, json.dumps(peek), False, ""))
    assert parked.status_via(spec, "op-1")[0] == parked.RATE_LIMITED
    plain = dict(entry, value={"to": ["oxxxxxx"]})
    node._op_write(state, plain)
    assert "urgent" not in node.op_peek(state, "op-1")
    unsigned = dict(entry, result={"status": "failed", "error_code": "E_RATE_LIMITED"})
    node._op_write(state, unsigned)
    assert "error_code" not in node.op_peek(state, "op-1")          # only a signed refusal's code is passed on
