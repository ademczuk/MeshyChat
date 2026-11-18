"""Seat usage on the turn report (seat-state v2.1 moved into the turn report, memory-bxxxxx afd116).

usage_at is when the seat OBSERVED the numbers, a report without usage keeps the stored snapshot, an older snapshot
never replaces a newer one, and only the listed fields are accepted."""
import datetime as dt
import json

import pytest

from apps.mesh import work
from apps.peers import errors, turn
from tests.test_mesh_routes import _auth, _peer, client, mesh_settings, owner  # noqa: F401

NOW = dt.datetime(2026, 11, 5, 12, 0, tzinfo=dt.timezone.utc)


@pytest.fixture(autouse=True)
def reset_turn_limits():
    turn._reports.clear()
    work.forget()
    yield
    turn._reports.clear()
    work.forget()


def _iso(when):
    return when.isoformat()


def _usage(as_of, **extra):
    body = {"as_of": _iso(as_of), "model": "claude-opus-5-5", "context_used_pct": 45.5,
            "limits": [{"window": "seven_day", "used_pct": 38, "resets_at": _iso(as_of + dt.timedelta(days=2)),
                        "source": "statusline"}]}
    body.update(extra)
    return body


def post(client, peer, **extra):
    body = {"state": "open", "item_at": dt.datetime.now(dt.timezone.utc).isoformat(), **extra}
    return client.post("/v1/peers/me/turn", data=json.dumps(body), content_type="application/json",
                       **_auth(peer["peer_id"]))


def test_usage_is_stored_with_its_observation_time_and_read_back(client, owner):
    a = _peer(owner, "usage-a")
    seen = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=3)
    assert post(client, a, usage=_usage(seen)).status_code == 200
    got = turn.read(owner, a["peer_id"])
    assert dt.datetime.fromisoformat(got["usage_at"]) == seen              # the observation time, not the receipt
    assert got["usage"] == {"model": "claude-opus-5-5", "context_used_pct": 45.5,
                            "limits": [{"window": "seven_day", "used_pct": 38.0,
                                        "resets_at": (seen + dt.timedelta(days=2)).isoformat(), "source": "statusline"}]}


def test_a_report_without_usage_keeps_the_snapshot_and_an_older_one_never_replaces_it(client, owner):
    a = _peer(owner, "usage-keep")
    newer = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=2)
    assert post(client, a, usage=_usage(newer, context_used_pct=60)).status_code == 200
    assert post(client, a).status_code == 200                                    # no usage this tick
    turn._reports.clear()
    kept = turn.read(owner, a["peer_id"])
    assert kept["usage"] is not None, "a report without usage cleared the stored snapshot"
    assert kept["usage"]["context_used_pct"] == 60.0
    older = newer - dt.timedelta(minutes=10)
    assert post(client, a, usage=_usage(older, context_used_pct=10)).status_code == 200
    got = turn.read(owner, a["peer_id"])
    assert got["usage"]["context_used_pct"] == 60.0 and dt.datetime.fromisoformat(got["usage_at"]) == newer
    turn._reports.clear()
    later = newer + dt.timedelta(minutes=1)
    assert post(client, a, usage=_usage(later, context_used_pct=70)).status_code == 200       # CONTROL: newer wins
    assert turn.read(owner, a["peer_id"])["usage"]["context_used_pct"] == 70.0


def test_usage_rides_on_the_unsurfaced_shape_too(client, owner):
    a = _peer(owner, "usage-five")
    now = dt.datetime.now(dt.timezone.utc)
    extra = {"unsurfaced": 0, "unsurfaced_oldest_at": None, "unsurfaced_since": _iso(now - dt.timedelta(hours=1)),
             "usage": _usage(now - dt.timedelta(seconds=30))}
    assert post(client, a, **extra).status_code == 200
    assert turn.read(owner, a["peer_id"])["usage"]["model"] == "claude-opus-5-5"


@pytest.mark.parametrize("bad", [
    "not an object",
    {"model": "claude-opus-5-5"},                                                    # no as_of
    {"as_of": "2026-11-05T11:59:00", "model": "x"},                                  # no offset
    {"as_of": "2026-11-05T12:05:00+00:00"},                                          # in the future
    {"as_of": "2026-11-04T11:00:00+00:00"},                                          # over a day old
    {"as_of": "2026-11-05T11:59:00+00:00", "cost": 1.2},                              # a field not on the list
    {"as_of": "2026-11-05T11:59:00+00:00", "context_used_pct": 101},
    {"as_of": "2026-11-05T11:59:00+00:00", "context_used_pct": True},
    {"as_of": "2026-11-05T11:59:00+00:00", "model": "a path/with spaces"},
    {"as_of": "2026-11-05T11:59:00+00:00", "limits": [{"window": "weekly", "source": "statusline"}]},
    {"as_of": "2026-11-05T11:59:00+00:00", "limits": [{"window": "seven_day", "source": "codex-log"}]},
    {"as_of": "2026-11-05T11:59:00+00:00", "limits": [{"window": "seven_day", "source": "statusline"},
                                                      {"window": "seven_day", "source": "statusline"}]},
    {"as_of": "2026-11-05T11:59:00+00:00", "limits": [{"window": "seven_day", "source": "statusline", "x": 1}]},
    {"as_of": "2026-11-05T11:59:00+00:00", "limits": [{"window": "seven_day"}]},
    {"as_of": "2026-11-05T11:59:00+00:00", "limits": [{"window": "%dm" % m, "source": "unknown"} for m in (1, 2, 3, 4, 5)]},
    # Codex P2 on 12744402: an unhashable source raised TypeError in the set test, a 500 instead of a 400.
    {"as_of": "2026-11-05T11:59:00+00:00", "limits": [{"window": "five_hour", "source": []}]},
    {"as_of": "2026-11-05T11:59:00+00:00", "limits": [{"window": "five_hour", "source": {}}]},
    {"as_of": "2026-11-05T11:59:00+00:00", "limits": [{"window": ["five_hour"], "source": "statusline"}]},
])
def test_a_usage_off_the_list_is_refused_and_never_stored(bad):
    with pytest.raises(errors.ApiError):
        turn.validate_usage({"usage": bad}, now=NOW)


def test_the_listed_fields_pass_and_minutes_windows_and_nulls_are_kept():
    usage, at = turn.validate_usage({"usage": {
        "as_of": "2026-11-05T11:59:00Z", "model": None, "context_used_pct": None,
        "limits": [{"window": "300m", "used_pct": None, "resets_at": None, "source": "codex-app-server"},
                   {"window": "five_hour", "used_pct": 12.345, "source": "statusline"}]}}, now=NOW)
    assert at == dt.datetime(2026, 11, 5, 11, 59, tzinfo=dt.timezone.utc)
    assert usage == {"model": None, "context_used_pct": None, "limits": [
        {"window": "300m", "used_pct": None, "resets_at": None, "source": "codex-app-server"},
        {"window": "five_hour", "used_pct": 12.35, "resets_at": None, "source": "statusline"}]}
    assert turn.validate_usage({}, now=NOW) == (None, None)                                   # CONTROL: none sent


def test_a_bad_usage_refuses_the_whole_report_over_http(client, owner):
    a = _peer(owner, "usage-bad")
    now = dt.datetime.now(dt.timezone.utc)
    assert post(client, a, usage={"as_of": _iso(now), "cost": 3}).status_code == 400
    assert turn.read(owner, a["peer_id"])["turn_state"] is None
    assert post(client, a, usage={"as_of": _iso(now)}).status_code == 200                     # CONTROL
