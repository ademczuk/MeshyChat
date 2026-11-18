"""Delay observations are distinct from idleness, unknown and overflow; the CLI sends no mail."""
import datetime as dt
import json
from types import SimpleNamespace

import pytest

from tools.meshy_node import cli, fleet, node, turn
from tools.postbox import turn_reporter

NOW = dt.datetime(2026, 11, 4, 12, tzinfo=dt.timezone.utc)
SINCE = (NOW-dt.timedelta(hours=2)).isoformat()


def peer(count=3, *, state="closed", age=2460, report_age=0):
    return {"peer_id": "abcdef12", "turn_state": state, "turn_item_at": NOW.isoformat(),
            "turn_reported_at": (NOW-dt.timedelta(seconds=report_age)).isoformat(), "unsurfaced": count,
            "unsurfaced_since": SINCE,
            "unsurfaced_oldest_at": (NOW-dt.timedelta(seconds=age)).isoformat() if count and count > 0 else None}


@pytest.mark.parametrize("facts,expected", [
    (peer(), (3, "3/41m")), (peer(0), (0, "0")), (peer(-1), (-1, "? partial")),
    (peer(None), (None, "?")), (peer(3, report_age=600), (None, "?")),
    (peer(1, age=299), (None, "?")), (peer(1, age=300), (1, "1/5m")),
    (peer(3, state="open"), (3, "3/41m")), (peer(True), (None, "?")),
])
def test_count_time_and_freshness_are_validated(monkeypatch, facts, expected):
    monkeypatch.setattr(fleet, "_now", lambda: NOW)
    assert fleet.unsurfaced_status(facts) == expected


def test_idle_over_has_separate_delay_and_partial_sections_and_never_sends(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(fleet, "_now", lambda: NOW)
    monkeypatch.setattr(node, "load_state", lambda *a, **kw: {"base_url": "http://fixture"})
    held_peer = peer(2, state="open")
    held_peer.update(turn_item_at=(NOW-dt.timedelta(minutes=20)).isoformat(), unread_count=2)
    peers = [peer(), peer(-1), peer(None, report_age=600), held_peer]
    answer = {"as_of": NOW.isoformat(), "recipients": [
        {"label": label, "peers": [value]} for label, value in zip(["delayed", "partial", "unknown", "held"], peers)]}
    calls = []
    monkeypatch.setattr(node, "recipients", lambda *a, **kw: calls.append(kw) or answer)
    args = ["fleet", "--base-url", "http://fixture", "--state-file", str(tmp_path/"s"),
            "--seats", "delayed,partial,unknown,held", "--work", "--idle-over", "20", "--json"]
    assert not cli._mutating_command(cli.build_parser().parse_args(args))
    assert cli.main(args) == 3
    result = json.loads(capsys.readouterr().out)
    assert result["rows"] == []
    assert [r["seat"] for r in result["unsurfaced"]] == ["delayed"]
    assert [r["seat"] for r in result["incomplete"]] == ["partial"]
    assert [r["seat"] for r in result["held"]] == ["held"]
    assert result["held"][0]["held"] == 2 and result["held"][0]["lost"] == "deferred"
    assert [r["seat"] for r in result["unknown"]] == ["unknown"]
    assert calls == [{"work": True}]
    args.remove("--json")
    assert cli.main(args) == 3
    text = capsys.readouterr().out
    assert "delays as well as possible losses" in text and "3/41m" in text
    assert "Delayed or missing notices (listed apart from idle seats)" in text
    assert "Incomplete notice observations (never counted as zero)" in text
    assert "Held notices on open turns (not called lost or idle)" in text


def test_open_held_then_closed_lost_and_arrival_before_close_is_zero(tmp_path, monkeypatch):
    from tests.test_postbox_unsurfaced import Box, START
    box = Box(tmp_path)
    box.hand()
    clock = [START + dt.timedelta(seconds=400)]
    monkeypatch.setattr(fleet, "_now", lambda: clock[0])
    def row(state):
        observation = box.tick(clock[0], state)
        facts = {"peer_id": "own", "turn_state": state, "turn_item_at": clock[0].isoformat(),
                 "turn_reported_at": clock[0].isoformat(), **observation}
        rows, _ = fleet.collect(["seat"], lambda labels: {"recipients": [{"label": "seat", "peers": [facts]}]}, work=True)
        return rows[0]
    held = row("open")
    assert held["held"] == 1 and held["held_label"] == "1/6m"
    assert held["unsurfaced"] is None and held["lost"] == "deferred"
    lost = row("closed")
    assert lost["unsurfaced"] == 1 and lost["lost"] == "1/6m" and lost["held"] is None
    box.surface(clock[0])
    assert row("closed")["unsurfaced"] == 0
    # A second notice lands while the turn is still open, so closing invents no loss.
    box.hand("msg/next", clock[0])
    clock[0] += dt.timedelta(seconds=300)
    assert row("open")["held"] == 1
    box.surface(clock[0], "msg/next")
    assert row("open")["held"] == 0
    assert row("closed")["unsurfaced"] == 0


def test_reporter_and_proved_client_send_only_two_more_scalar_fields(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(node, "require_peer", lambda *a: None)
    monkeypatch.setattr(node, "fresh_jwt", lambda *a: "test-token")
    monkeypatch.setattr(node, "_proved_headers", lambda *a: {"DPoP": "test-proof"})
    monkeypatch.setattr(node, "http_json", lambda *a, **kw: calls.append(kw) or (200, {"ok": True, "applied": True}))
    observation = {"unsurfaced": 3, "unsurfaced_oldest_at": (NOW-dt.timedelta(minutes=41)).isoformat(), "unsurfaced_since": SINCE}
    turn.publish(tmp_path/"PRIVATE", {"base_url": "http://fixture"}, state_value="closed",
                 item_at=NOW.isoformat(), observation=observation)
    assert calls[0]["body"] == {"state": "closed", "item_at": NOW.isoformat(), **observation}
    assert calls[0]["headers"] == {"DPoP": "test-proof"}
    child = []
    monkeypatch.setattr(turn_reporter.postbox_os, "run_bounded", lambda argv, **kw:
        child.append((argv, kw)) or SimpleNamespace(returncode=0, stdout='{"ok":true,"applied":true}'))
    ident = {"python": "python", "client": "local-client", "state": "local-state", "base_url": "http://fixture"}
    assert turn_reporter.run_child(ident, {"state": "closed", "item_at": NOW.isoformat(), **observation})
    argv, opts = child[0]
    assert argv[-6:] == ["--unsurfaced", "3", "--unsurfaced-since", SINCE, "--unsurfaced-oldest-at", observation["unsurfaced_oldest_at"]]
    assert opts["creationflags"] == turn_reporter.postbox_os.NO_WINDOW and opts["timeout"] == 15
    assert "PRIVATE" not in str(calls)
    with pytest.raises(node.Refused):
        turn.publish(tmp_path/"s", {}, state_value="closed", item_at=NOW.isoformat(),
                     observation={**observation, "key": "msg/private"})
    with pytest.raises(node.Refused):
        turn.publish(tmp_path/"s", {}, state_value="closed", item_at=NOW.isoformat(),
                     observation={"unsurfaced": 3, "unsurfaced_oldest_at": "PRIVATE-PATH", "unsurfaced_since": SINCE})
    with pytest.raises(node.Refused):
        turn.publish(tmp_path/"s", {}, state_value="closed", item_at=NOW.isoformat(),
                     observation={**observation, "unsurfaced_since": "PRIVATE-PATH"})
    assert len(calls) == 1


def test_reporter_observation_reaches_wire_without_extra_refresh_requests(monkeypatch):
    observations, sent = [], []
    class Observer:
        def update(self, ident, sources, **kw):
            observations.append((sources, kw))
            return {"unsurfaced": 3, "unsurfaced_oldest_at": (NOW-dt.timedelta(minutes=41)).isoformat(), "unsurfaced_since": SINCE}
    reporter = turn_reporter.Reporter(tracker=Observer())
    reporter.files = {"local-session": ((1, 2, 100), "closed", NOW), "child": ((1, 3, 100), None, None)}
    monkeypatch.setattr(reporter, "observe", lambda sources: {"state": "closed", "item_at": NOW.isoformat()})
    def send(ident, body):
        sent.append(body)
        return True
    for now in (0, 29, 30, 119, 120):
        reporter.tick({"name": "seat"}, ["local-session", "child"], now=now, send=send)
    assert len(sent) == 2
    assert set(sent[0]) == {"state", "item_at", "unsurfaced", "unsurfaced_oldest_at", "unsurfaced_since"}
    assert all(row[0] == ["local-session"] for row in observations)
    assert observations[0][1]["expected_inodes"] == {"local-session": (1, 2)}
    assert "local-session" not in json.dumps(sent)


def test_zero_without_coverage_baseline_is_unknown_and_table_shows_since(monkeypatch):
    monkeypatch.setattr(fleet, "_now", lambda: NOW)
    facts = peer(0)
    facts.pop("unsurfaced_since")
    assert fleet.unsurfaced_status(facts) == (None, "?")
    facts["unsurfaced_since"] = SINCE
    rows, as_of = fleet.collect(["seat"], lambda labels: {"as_of": NOW.isoformat(),
        "recipients": [{"label": "seat", "peers": [facts]}]}, work=True)
    assert rows[0]["unsurfaced_since"] == SINCE and rows[0]["since"] == "2h"
    text = fleet.render(rows, as_of, work=True)
    assert "since" in text and "2h" in text and "zero means none since that baseline" in text
