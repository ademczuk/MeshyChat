"""Work mode opts in explicitly, never invents zero counts or a measured idle/working state."""
from tools.meshy_node import fleet, node


def _answer(peer):
    return {"as_of": "2026-11-04T10:00:00Z", "recipients": [{"label": "seat", "peers": [
        {"peer_id": "abcdef12", "effective_state": "unknown", "unread_count": 2,
         "unread_oldest_age_secs": 60, **peer}]}]}


def test_work_rows_keep_counts_distinct_and_label_samples_clean():
    answer = _answer({"last_sent_at": "2026-11-04T09:58:00Z", "indexed_turn_at": "2026-11-04T09:00:00Z",
                      "work_as_of": "2026-11-04T09:59:30Z", "owed": {"open": 0, "active": 8},
                      "awaiting": {"open": 2, "active": 1, "recipients": ["pxx", "bad\x1b", "kxxx", "third"]}})
    rows, as_of = fleet.collect(["seat"], lambda labels: answer, work=True)
    assert rows[0]["owed"] == {"open": 0, "active": 8}
    assert rows[0]["sent"] == "2m" and rows[0]["indexed"] == "60m"
    assert rows[0]["awaiting_recipients"] == ["pxx", "kxxx"]
    out = fleet.render(rows, as_of, work=True)
    assert "0/8" in out and "2/1" in out and "pxx,kxxx" in out
    assert "not a measured turn state" in out and "not proof of a blocked seat" in out
    assert "turn<=" in out and "lagging upper bound on turn age" in out
    assert "\x1b" not in out


def test_older_server_and_unavailable_counts_are_unknown_not_zero():
    for payload in ({}, {"owed": None, "awaiting": None}, {"owed": {"open": False, "active": 0}}):
        rows, as_of = fleet.collect(["seat"], lambda labels: _answer(payload), work=True)
        assert rows[0]["owed"] is None and rows[0]["awaiting"] is None
        out = fleet.render(rows, as_of, work=True)
        assert "0/0" not in out
        assert rows[0]["sent"] == "?" and rows[0]["indexed"] == "?"


def test_plain_collection_ignores_extra_work_fields():
    rows, as_of = fleet.collect(["seat"], lambda labels: _answer({"owed": {"open": 1, "active": 2}}))
    assert "owed" not in rows[0] and "last_sent_at" not in rows[0]
    assert "published state is not a measured turn state" not in fleet.render(rows, as_of)


def test_work_uses_four_labels_per_call_while_plain_fleet_uses_five():
    seats = [f"seat{i}" for i in range(11)]
    for work_mode, expected in ((False, [5, 5, 1]), (True, [4, 4, 3])):
        calls = []
        def recipients(labels):
            calls.append(labels)
            return {"recipients": []}
        fleet.collect(seats, recipients, work=work_mode)
        assert [len(c) for c in calls] == expected
        assert [s for c in calls for s in c] == seats


def test_no_recent_send_is_distinct_from_a_missing_or_failed_snapshot():
    answer = _answer({"last_sent_at": None, "work_as_of": "2026-11-04T09:59:30Z"})
    answer["work"] = {"sent_days": 7, "ask_days": 14}
    rows, as_of = fleet.collect(["seat"], lambda labels: answer, work=True)
    assert rows[0]["sent"] == "none/7d"
    assert "sent window (days): 7" in fleet.render(rows, as_of, work=True)
    answer["recipients"][0]["peers"][0]["work_as_of"] = None
    rows, _ = fleet.collect(["seat"], lambda labels: answer, work=True)
    assert rows[0]["sent"] == "?"


def test_client_query_is_opt_in(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(node, "require_peer", lambda *a: None)
    monkeypatch.setattr(node, "fresh_jwt", lambda *a: "fake")
    monkeypatch.setattr(node, "http_json", lambda *a, **kw: calls.append(kw["query"]) or (200, {}))
    state = {"base_url": "http://fixture"}
    node.recipients(tmp_path / "s", state, ["seat"])
    node.recipients(tmp_path / "s", state, ["seat"], work=True)
    assert calls == [{"to": "seat"}, {"to": "seat", "detail": "work"}]


def test_cli_work_flag_routes_the_opt_in_and_is_read_only(monkeypatch, capsys, tmp_path):
    from tools.meshy_node import cli
    asked = []
    monkeypatch.setattr(node, "load_state", lambda *a, **k: {"base_url": "http://fixture", "peer_id": "fake"})
    monkeypatch.setattr(node, "recipients", lambda *a, **kw: asked.append(kw) or _answer({}))
    args = ["fleet", "--base-url", "http://fixture", "--state-file", str(tmp_path / "s"),
            "--seats", "seat", "--work"]
    assert not cli._mutating_command(cli.build_parser().parse_args(args))
    assert cli.main(args) == 0
    assert asked == [{"work": True}]
    assert "fleet work" in capsys.readouterr().out
