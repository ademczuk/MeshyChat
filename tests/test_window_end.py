"""`window_end`: a directive or a relay lives until the window its words name ends, plus 30 minutes (kxxx ab614c, yes
from oxxxxxx 363a45 under a recorded decision, Tue 10 Nov 2026). Database-free: the validators, the wire, the CLI's
directive and the reader are called directly."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

import pytest

from apps.memoryskills import validation as V
from apps.memoryskills.errors import HandlerError
from tools.meshy_node import cli, node, wire

NS = V.MESH_CHAT_NAMESPACE
KEY = "msg/1794300000000-kxxx-abcdef"
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
VOICE = "0447bf92-25fb-5230-b531-5a0d9c00d8c7"
PEER = "5b23b084-b86b-5400-9db9-0abf5af9fbce"


def z(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def directive_value(expires, window_end=None):
    v = {"to": ["codex-kxxx-cli"], "directive": {"v": 1, "words": ["run the lab"], "target": "codex-kxxx-cli",
                                                  "target_peer": PEER, "expires_at": z(expires)}}
    if window_end is not None:
        v["window_end"] = window_end
    return v


# ---- the server ------------------------------------------------------------------------------------------------

def test_window_end_is_a_v1_field_on_both_sides():
    assert "window_end" in V.MESH_MESSAGE_V1_OPTIONAL_FIELDS and "window_end" in wire.V1_OPTIONAL
    assert V.WINDOW_MARGIN_S == node.WINDOW_MARGIN_S == 1800


def test_a_directive_may_end_at_exactly_window_end_plus_the_margin():
    end = NOW + timedelta(hours=1)
    V.validate_window_end(NS, KEY, directive_value(end + timedelta(minutes=30), z(end)), now=NOW)
    V.validate_window_end(NS, KEY, directive_value(end, z(end)), now=NOW)


def test_a_directive_that_outlives_its_window_is_refused():
    end = NOW + timedelta(hours=1)
    with pytest.raises(HandlerError, match="more than 30 min after window_end"):
        V.validate_window_end(NS, KEY, directive_value(end + timedelta(minutes=31), z(end)), now=NOW)
    # The motivating case: the lab ends at 12:45 and the directive was sent with the 24 h default.
    with pytest.raises(HandlerError, match="more than 30 min after window_end"):
        V.validate_window_end(NS, KEY, directive_value(NOW + timedelta(hours=24), z(NOW + timedelta(minutes=45))),
                              now=NOW)


def test_without_window_end_nothing_changes():
    V.validate_window_end(NS, KEY, directive_value(NOW + timedelta(hours=24)), now=NOW)
    V.check_window_not_past({"to": ["x"]}, now=NOW)


@pytest.mark.parametrize("bad", ["2026-11-10T13:00:00", "2026-11-10T13:00:00+00:00", "soon", 5, None, ""])
def test_window_end_must_be_a_z_instant(bad):
    with pytest.raises(HandlerError):
        V.validate_window_end(NS, KEY, {"operator_words": ["go"], "window_end": bad}, now=NOW)


def test_window_end_needs_a_directive_or_relayed_words_and_a_mesh_msg_key():
    end = z(NOW + timedelta(hours=1))
    with pytest.raises(HandlerError, match="goes with a directive or operator_words"):
        V.validate_window_end(NS, KEY, {"to": ["x"], "window_end": end}, now=NOW)
    with pytest.raises(HandlerError, match="msg/ key only"):
        V.validate_window_end("shared/wiki", "wiki/x", {"operator_words": ["go"], "window_end": end}, now=NOW)
    V.validate_window_end(NS, KEY, {"operator_words": ["go"], "window_end": end}, now=NOW)


def test_window_end_is_at_most_24_h_away():
    with pytest.raises(HandlerError, match="more than 24 h away"):
        V.validate_window_end(NS, KEY, {"operator_words": ["go"], "window_end": z(NOW + timedelta(hours=25))},
                              now=NOW)


def test_admission_refuses_a_window_already_closed_and_accepts_one_inside_the_margin():
    with pytest.raises(HandlerError, match="already passed"):
        V.check_window_not_past({"window_end": z(NOW - timedelta(minutes=30))}, now=NOW)
    V.check_window_not_past({"window_end": z(NOW - timedelta(minutes=29))}, now=NOW)


def test_the_write_seam_runs_both_checks():
    import inspect
    from apps.memoryskills import handlers
    src = inspect.getsource(handlers.handle_write)
    assert "validate_window_end(namespace, key, value)" in src
    # Admission comes after the replay return, as for a directive's own expiry (pxx 9e5d48).
    assert src.index("_replay_within(") < src.index("check_window_not_past(value)")


# ---- the wire --------------------------------------------------------------------------------------------------

def _msg(**kw):
    return wire.mesh_message(to=["codex-kxxx-cli"], sender="kxxx", msg_type="request", thread_id="t", body="b",
                             author_family="claude", **kw)


def test_the_wire_carries_window_end_only_beside_words_or_a_directive():
    end = z(NOW + timedelta(hours=1))
    assert _msg(operator_words=["go"], window_end=end)["window_end"] == end
    assert "window_end" not in _msg(operator_words=["go"])
    with pytest.raises(wire.NotV1, match="goes with a directive or operator_words"):
        _msg(window_end=end)
    with pytest.raises(wire.NotV1, match="ending in Z"):
        _msg(operator_words=["go"], window_end="2026-11-10T13:00:00+01:00")


# ---- the CLI's directive ---------------------------------------------------------------------------------------

def _args(tmp_path, hours=None, window_end=None):
    f = tmp_path / "d.txt"
    f.write_text("run the lab\n", encoding="utf-8")
    return argparse.Namespace(directive_file=f, directive_to="codex-kxxx-cli", directive_hours=hours,
                              window_end=window_end)


def test_with_a_window_and_no_hours_the_directive_expires_at_window_end_plus_30(tmp_path):
    end = (datetime.now(timezone.utc) + timedelta(hours=2)).replace(microsecond=0)
    d = cli._directive(_args(tmp_path, window_end=z(end)), lambda label: PEER)
    assert d["expires_at"] == z(end + timedelta(minutes=30))
    assert cli._directive_hours(_args(tmp_path, window_end=z(end))) is None


def test_hours_past_the_window_are_refused_before_signing(tmp_path):
    end = datetime.now(timezone.utc) + timedelta(hours=1)
    with pytest.raises(node.Refused, match="after --window-end"):
        cli._directive(_args(tmp_path, hours=4, window_end=z(end)), lambda label: PEER)
    d = cli._directive(_args(tmp_path, hours=1, window_end=z(end)), lambda label: PEER)
    assert d["expires_at"] <= z(end + timedelta(minutes=30))


def test_a_closed_window_is_refused_before_signing(tmp_path):
    with pytest.raises(node.Refused, match="already passed"):
        cli._directive(_args(tmp_path, window_end=z(datetime.now(timezone.utc) - timedelta(hours=1))),
                       lambda label: PEER)


def test_without_a_window_the_old_default_and_journalled_hours_hold(tmp_path):
    before = datetime.now(timezone.utc)
    d = cli._directive(_args(tmp_path), lambda label: PEER)
    exp = datetime.fromisoformat(d["expires_at"][:-1] + "+00:00")
    assert timedelta(hours=3, minutes=59) < exp - before <= timedelta(hours=4, seconds=1)
    # An --op-id retry across this change journals the same 4.0 it did before.
    assert cli._directive_hours(_args(tmp_path)) == 4.0
    assert cli._directive_hours(_args(tmp_path, hours=2.5)) == 2.5


def test_the_parser_has_window_end_and_no_hours_default():
    p = cli.build_parser() if hasattr(cli, "build_parser") else None
    if p is None:
        pytest.skip("no build_parser in this cli")
    a = p.parse_args(["send", "--base-url", "http://x", "--state-file", "s.json", "--to", "x", "--thread", "t", "--body", "b", "--window-end", "2026-11-11T11:45:00Z"])
    assert a.window_end == "2026-11-11T11:45:00Z" and a.directive_hours is None


# ---- the reader ------------------------------------------------------------------------------------------------

def relay_value(window_end=None):
    v = _msg(operator_words=["merge #446"])
    if window_end is not None:
        v["window_end"] = window_end
    return v


def test_a_relay_with_a_window_is_live_until_window_end_plus_30_then_expired():
    end = NOW + timedelta(hours=1)
    written = NOW - timedelta(minutes=5)
    live = node.operator_relay_of(relay_value(z(end)), VOICE, True, written, end + timedelta(minutes=29))
    assert not live.get("expired") and live["words"] == ["merge #446"]
    assert live["expires_at"] == z(end + timedelta(minutes=30)) and "carry no authority" in live["note"]
    dead = node.operator_relay_of(relay_value(z(end)), VOICE, True, written, end + timedelta(minutes=30))
    assert dead["expired"] is True and "words" not in dead and "ask again" in dead["note"]


def test_a_relay_that_names_no_window_never_expires_as_before():
    """pxx faf9fd P2-4: a default lifetime would narrow Bxxxxx's own "Allow relay, no limits" (28 Oct 2026)."""
    for written in (NOW - timedelta(days=30), None):
        r = node.operator_relay_of(relay_value(), VOICE, True, written, NOW)
        assert not r.get("expired") and r["words"] == ["merge #446"] and r["expires_at"] is None
        assert "name no window, so they do not expire" in r["note"]


def test_an_unreadable_window_is_expired_never_unlimited():
    assert node.operator_relay_of(relay_value("tomorrow"), VOICE, True, NOW, NOW)["expired"] is True


def test_an_expired_relay_is_a_relay_field_stripped_from_unsigned_answers():
    assert "operator_relay_expired" in node.RELAY_FIELDS
    assert node.strip_relay_fields({"a": {"operator_relay_expired": {"voice": "x"}}}) == {"a": {}}


def test_the_directive_note_says_an_expired_directive_carries_no_authority():
    assert "past that it carries no authority" in node.DIRECTIVE_NOTE


# ---- an --op-id retry ------------------------------------------------------------------------------------------

def test_a_retry_that_adds_or_changes_the_window_is_a_different_operation():
    base = dict(to=["x"], thread_id="t", msg_type="request", body="b", sender="kxxx", author_family="claude",
                operator_words=["go"])
    a = node._op_fingerprint_for_args(**base)
    b = node._op_fingerprint_for_args(**base, window_end="2026-11-10T13:00:00Z")
    c = node._op_fingerprint_for_args(**base, window_end="2026-11-10T14:00:00Z")
    assert len({a, b, c}) == 3
    entry = {"op_id": "op1", "value": {**_msg(operator_words=["go"])}, "fingerprint": a}
    with pytest.raises(node.Refused, match="different message arguments"):
        node._op_assert_same_message(entry, {**base, "window_end": "2026-11-10T13:00:00Z"})


# ---- GitHub Codex on #586 (047ba756) ---------------------------------------------------------------------------

def test_an_op_id_retry_after_the_window_still_reaches_the_journal(tmp_path):
    """cli.py:1840: the client's window refusals are for a NEW send; an --op-id send skips them so a late identical
    retry can get its stored result (the server allows that replay)."""
    a = _args(tmp_path, window_end=z(datetime.now(timezone.utc) - timedelta(hours=1)))
    a.op_id = "op-1"
    d = cli._directive(a, lambda label: PEER)
    assert d["expires_at"] < z(datetime.now(timezone.utc))
    a.directive_hours = 4
    cli._directive(a, lambda label: PEER)                     # hours past the window: no client refusal either


def test_a_window_24_h_ahead_gets_an_expiry_inside_the_24_h_limit(tmp_path):
    """cli.py:1835: window_end + 30 min would be 24.5 h away, which validate_directive refuses."""
    now = datetime.now(timezone.utc)
    d = cli._directive(_args(tmp_path, window_end=z(now + timedelta(hours=24))), lambda label: PEER)
    exp = datetime.fromisoformat(d["expires_at"][:-1] + "+00:00")
    assert exp - now <= timedelta(hours=24, seconds=2)
    V.validate_window_end(NS, KEY, directive_value(exp, z(now + timedelta(hours=24))))


def test_an_expired_relay_read_carries_no_raw_words(monkeypatch, tmp_path):
    """node.py:1858: the read drops message["operator_words"] too, so no route prints the expired words."""
    from tests.test_meshy_node_operator_relay import _read, _result, value as relay_msg
    closed = relay_msg()
    closed["window_end"] = z(datetime.now(timezone.utc) - timedelta(hours=2))
    msg = _read(monkeypatch, tmp_path, _result(closed, VOICE))
    assert "operator_relay_expired" in msg and "operator_relay" not in msg
    assert "operator_words" not in msg and "merge #446" not in str(msg)
    fresh = _read(monkeypatch, tmp_path, _result(relay_msg(), VOICE))
    assert fresh["operator_words"] == ["merge #446 once its gate passes"] and "operator_relay" in fresh


def test_the_help_says_a_windowless_relay_never_expires():
    """GitHub Codex on 793b45da, cli.py:916: the help must not promise an expiry the reader does not apply."""
    p = cli.build_parser()
    send = next(a for a in p._subparsers._group_actions[0].choices["send"]._actions if "--window-end" in a.option_strings)
    assert "NEVER expire" in send.help and "24 h after they are written" not in send.help
