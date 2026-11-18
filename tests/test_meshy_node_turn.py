"""Proved turn payloads, report lapse and actionable idle filtering without sending mail."""
import datetime as dt

import pytest

from tools.meshy_node import cli, fleet, node, turn

NOW = dt.datetime(2026, 11, 4, 12, tzinfo=dt.timezone.utc)


def facts(state="open", *, age=0, report_age=0):
    return {"turn_state": state, "turn_item_at": (NOW-dt.timedelta(seconds=age)).isoformat(),
            "turn_reported_at": (NOW-dt.timedelta(seconds=report_age)).isoformat()}


@pytest.mark.parametrize("state,age,report_age,expected", [
    ("open", 299, 0, "working"), ("open", 300, 0, "quiet"), ("closed", 2000, 599, "idle"),
    ("closed", 2000, 600, "?"), ("open", 0, 601, "?"), (None, 0, 0, "?")])
def test_report_boundaries_never_turn_missing_or_stale_into_idle(monkeypatch, state, age, report_age, expected):
    monkeypatch.setattr(fleet, "_now", lambda: NOW)
    assert fleet.turn_status(facts(state, age=age, report_age=report_age))[0] == expected
    assert fleet.turn_status({}) == ("?", None)


def test_idle_filter_uses_open_not_active_and_lists_unknown_apart():
    base = {"state": "idle", "turn_age_secs": 1200, "unread": 0, "owed": {"open": 0, "active": 7}}
    active = dict(base, seat="only-active")
    owed = dict(base, seat="owed", owed={"open": 1, "active": 0})
    unread = dict(base, seat="unread", unread=1, state="quiet")
    unknown = dict(base, seat="unknown", state="?", unread=4)
    recent = dict(owed, seat="recent", turn_age_secs=1199)
    working = dict(unread, seat="working", state="working")
    alerts, apart = fleet.idle_over([active, owed, unread, unknown, recent, working], 20)
    assert [row["seat"] for row in alerts] == ["owed", "unread"]
    assert apart == [unknown]


def test_cli_idle_exit_and_unknown_output_and_plain_route_remain_read_only(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(node, "load_state", lambda *a, **k: {"base_url": "http://fixture"})
    monkeypatch.setattr(fleet, "_now", lambda: NOW)
    payload = {"as_of": NOW.isoformat(), "recipients": [{"label": "idle-seat", "peers": [
        {"peer_id": "a", "unread_count": 2, **facts("closed", age=1200)}]},
        {"label": "unknown-seat", "peers": [{"peer_id": "b", "unread_count": 9}]}]}
    requests = []
    monkeypatch.setattr(node, "recipients", lambda *a, **kw: requests.append(kw) or payload)
    args = ["fleet", "--base-url", "http://fixture", "--state-file", str(tmp_path/"s"),
            "--seats", "idle-seat,unknown-seat", "--work", "--idle-over", "20"]
    assert not cli._mutating_command(cli.build_parser().parse_args(args))
    assert cli.main(args) == 3
    output = capsys.readouterr().out
    assert "Unknown turn reports (listed apart" in output
    assert output.index("unknown-seat") > output.index("Unknown turn reports")
    assert requests == [{"work": True}]
    payload["recipients"][0]["peers"][0]["unread_count"] = 0
    assert cli.main(args) == 0


def test_turn_payload_is_proved_and_contains_only_state_and_timestamp(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(node, "require_peer", lambda *a: None)
    monkeypatch.setattr(node, "fresh_jwt", lambda *a: "fake")
    monkeypatch.setattr(node, "_proved_headers", lambda *a: {"DPoP": "proof"})
    monkeypatch.setattr(node, "http_json", lambda *a, **kw: sent.append((a, kw)) or (200, {"ok": True}))
    assert turn.publish(tmp_path/"s", {"base_url": "http://fixture"}, state_value="open", item_at=NOW.isoformat()) == {"ok": True}
    args, kw = sent[0]
    assert args == ("POST", "http://fixture", "/v1/peers/me/turn")
    assert kw["body"] == {"state": "open", "item_at": NOW.isoformat()} and kw["headers"] == {"DPoP": "proof"}
    with pytest.raises(node.Refused):
        turn.publish(tmp_path/"s", {}, state_value="idle", item_at="bad")
    assert len(sent) == 1
