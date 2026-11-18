"""Client side of seat usage on the turn report: the publisher refuses anything off the field list before sending,
and fleet --work shows the snapshot with its age judged by when it was OBSERVED."""
import datetime as dt

import pytest

from tools.meshy_node import fleet, node, turn

AS_OF = "2026-11-05T12:00:00+00:00"


@pytest.mark.parametrize("usage", [
    "text",
    {"model": "claude-opus-5-5"},                                   # no as_of
    {"as_of": "2026-11-05T12:00:00"},                               # no timezone
    {"as_of": AS_OF, "cost": 1.0},                                  # off the list
    {"as_of": AS_OF, "transcript_path": "C:/somewhere"},            # off the list
    # Codex P2 on 12744402: nested fields were forwarded unchecked, so these reached the server before a refusal.
    {"as_of": AS_OF, "limits": [{"window": "five_hour", "source": "statusline", "transcript_path": "C:/somewhere"}]},
    {"as_of": AS_OF, "limits": [{"window": "five_hour", "source": ["statusline"]}]},
    {"as_of": AS_OF, "limits": [{"window": "five_hour", "source": "statusline", "used_pct": "12"}]},
    {"as_of": AS_OF, "limits": [{"window": "five_hour", "source": "statusline", "resets_at": {"at": 1}}]},
    {"as_of": AS_OF, "limits": {"window": "five_hour", "source": "statusline"}},
    {"as_of": AS_OF, "limits": [{"window": "%dm" % m, "source": "unknown"} for m in (1, 2, 3, 4, 5)]},
    {"as_of": AS_OF, "model": {"name": "x"}},
    {"as_of": AS_OF, "context_used_pct": True},
])
def test_the_publisher_refuses_a_usage_off_the_list_before_any_request(usage, monkeypatch):
    # Every step before the request is a no-op here, so a usage that is NOT refused locally reaches http_json and the
    # test fails (an empty state would otherwise be refused by require_peer and pass for the wrong reason).
    sent = []
    monkeypatch.setattr(node, "require_peer", lambda *a, **kw: None)
    monkeypatch.setattr(node, "fresh_jwt", lambda *a, **kw: "jwt")
    monkeypatch.setattr(node, "_proved_headers", lambda *a, **kw: {})
    monkeypatch.setattr(node, "http_json", lambda *a, **kw: sent.append((a, kw)) or (200, {}))
    with pytest.raises(node.Refused):
        turn.publish("unused", {"base_url": "http://unused"}, state_value="open", item_at=AS_OF, usage=usage)
    assert sent == [], "a refused usage reached the network"


def test_the_publisher_sends_a_usage_with_only_listed_fields(monkeypatch):
    """CONTROL for the refusals above: a snapshot of listed fields and scalar values is not refused locally."""
    usage = {"as_of": AS_OF, "model": "claude-opus-5-5", "context_used_pct": 45,
             "limits": [{"window": "five_hour", "used_pct": 12.5, "resets_at": AS_OF, "source": "statusline"},
                        {"window": "seven_day", "used_pct": None, "resets_at": None, "source": "unknown"}]}
    assert turn._usage_refusal(usage) is None


def test_usage_label_shows_context_and_windows_and_stars_an_old_observation():
    peer = {"usage": {"model": "claude-opus-5-5", "context_used_pct": 45.4,
                      "limits": [{"window": "five_hour", "used_pct": 12}, {"window": "seven_day", "used_pct": 38.6},
                                 {"window": "300m", "used_pct": None}, {"window": "10080m", "used_pct": 1}]},
            "usage_at": "2026-11-05T11:58:00+00:00"}
    assert fleet.usage_label(peer, AS_OF) == "c45 5h12 7d39 5h? 7d1"
    old = dict(peer, usage_at="2026-11-05T11:44:00+00:00")                # 16 minutes before as_of
    assert fleet.usage_label(old, AS_OF).endswith("*")
    edge = dict(peer, usage_at="2026-11-05T11:45:00+00:00")               # CONTROL: exactly 15 minutes
    assert not fleet.usage_label(edge, AS_OF).endswith("*")


def test_usage_label_says_none_or_unknown_rather_than_inventing_numbers():
    assert fleet.usage_label({}, AS_OF) == "-"
    assert fleet.usage_label({"usage": {"limits": []}, "usage_at": None}, AS_OF) == "-"
    assert fleet.usage_label({"usage": {"limits": [{"window": "weekly", "used_pct": 3}]},
                              "usage_at": "2026-11-05T11:59:00+00:00"}, AS_OF) == "?"
    assert fleet.usage_label({"usage": {"context_used_pct": True}, "usage_at": "2026-11-05T11:59:00+00:00"}, AS_OF) == "?"
