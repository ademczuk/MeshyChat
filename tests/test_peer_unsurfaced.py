"""Counts/times are token-owned, expire with the turn report, and never notify presence."""
import datetime as dt

import pytest

from apps.mesh import work
from apps.peers import turn
from tests.test_peer_turn import post as _post
from tests.test_mesh_routes import _peer, client, mesh_settings, owner  # noqa: F401


def baseline():
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).isoformat()


def post(client, peer, state="closed", **extra):
    if "unsurfaced" in extra:
        extra.setdefault("unsurfaced_since", baseline() if extra["unsurfaced"] is not None else None)
    return _post(client, peer, state, **extra)


@pytest.fixture(autouse=True)
def reset():
    turn._reports.clear(); work.forget()
    yield
    turn._reports.clear(); work.forget()


def oldest():
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=41)).isoformat()


def test_two_fields_are_fresh_and_old_client_or_expired_report_never_inherits_zero(client, owner):
    peer = _peer(owner, "unseen-expire")
    at = oldest()
    assert post(client, peer, "closed", unsurfaced=3, unsurfaced_oldest_at=at).json()["applied"]
    row = work.snapshot(owner, peer["peer_id"], [peer["name"]])
    assert row["unsurfaced"] == 3
    assert dt.datetime.fromisoformat(row["unsurfaced_oldest_at"]) == dt.datetime.fromisoformat(at)
    owner.execute("UPDATE mesh_presence SET turn_reported_at=NOW()-INTERVAL '601 seconds' WHERE peer_id=%s",
                  (peer["peer_id"],))
    assert work.snapshot(owner, peer["peer_id"], [peer["name"]])["unsurfaced"] is None
    assert post(client, peer, "closed").status_code == 200
    assert turn.read(owner, peer["peer_id"])["unsurfaced"] is None
    assert post(client, peer, "closed", unsurfaced=0, unsurfaced_oldest_at=None).status_code == 200
    assert turn.read(owner, peer["peer_id"])["unsurfaced"] == 0


@pytest.mark.parametrize("extra", [
    {"unsurfaced": True, "unsurfaced_oldest_at": None},
    {"unsurfaced": -2, "unsurfaced_oldest_at": None},
    {"unsurfaced": 2049, "unsurfaced_oldest_at": None},
    {"unsurfaced": 1, "unsurfaced_oldest_at": None},
    {"unsurfaced": None, "unsurfaced_oldest_at": "2026-01-01T00:00:00Z"},
    {"unsurfaced": 0, "unsurfaced_oldest_at": "2026-01-01T00:00:00Z"},
    {"unsurfaced": 1, "unsurfaced_oldest_at": "2999-01-01T00:00:00Z"},
    {"unsurfaced": 1, "unsurfaced_oldest_at": "not-a-time"},
    {"unsurfaced": 1}, {"unsurfaced_oldest_at": None},
    {"unsurfaced": 0, "unsurfaced_oldest_at": None, "key": "msg/private"},
], ids=["bool", "negative", "large", "missing-time", "unknown-time", "zero-time", "future", "bad-time",
        "missing-pair", "missing-count", "private-key"])
def test_validation_refuses_non_scalar_or_incoherent_observations_before_rate_limit(client, owner, extra):
    peer = _peer(owner, "unseen-invalid")
    assert post(client, peer, "closed", **extra).status_code == 400
    assert str(peer["peer_id"]) not in turn._reports


def test_open_reports_held_and_young_pending_is_refused_but_incomplete_is_explicit(client, owner):
    peer = _peer(owner, "unseen-open")
    assert post(client, peer, "open", unsurfaced=1, unsurfaced_oldest_at=oldest()).status_code == 200
    assert turn.read(owner, peer["peer_id"])["unsurfaced"] == 1
    assert turn.read(owner, peer["peer_id"])["turn_state"] == "open"
    young = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=230)).isoformat()
    assert post(client, peer, "closed", unsurfaced=1, unsurfaced_oldest_at=young).status_code == 400
    assert post(client, peer, "closed", unsurfaced=-1, unsurfaced_oldest_at=None).status_code == 200
    assert turn.read(owner, peer["peer_id"])["unsurfaced"] == -1


def test_server_grace_allows_clock_skew_with_an_exact_tolerance_boundary():
    now = dt.datetime(2026, 11, 4, 12, tzinfo=dt.timezone.utc)
    body = {"state": "closed", "item_at": now.isoformat(), "unsurfaced": 1,
            "unsurfaced_oldest_at": (now-dt.timedelta(seconds=239)).isoformat(),
            "unsurfaced_since": (now-dt.timedelta(hours=1)).isoformat()}
    with pytest.raises(turn.errors.ApiError):
        turn.validate_unsurfaced(body, now=now)
    body["unsurfaced_oldest_at"] = (now-dt.timedelta(seconds=240)).isoformat()
    try:
        accepted = turn.validate_unsurfaced(body, now=now)
    except turn.errors.ApiError:
        accepted = None
    # A refusal must fail the acceptance assertion, not masquerade as an error kill.
    assert accepted == (1, now-dt.timedelta(seconds=240))


def test_client_five_seconds_ahead_does_not_lose_the_whole_turn_report(client, owner, monkeypatch):
    peer = _peer(owner, "unseen-clock-skew")
    server_now = dt.datetime.now(dt.timezone.utc)
    client_now = server_now + dt.timedelta(seconds=5)
    stamp = client_now - dt.timedelta(seconds=302)
    original = turn.validate_unsurfaced
    monkeypatch.setattr(turn, "validate_unsurfaced", lambda body: original(body, now=server_now))
    response = post(client, peer, "open", item_at=client_now.isoformat(),
                    unsurfaced=1, unsurfaced_oldest_at=stamp.isoformat())
    assert response.status_code == 200
    assert response.json()["applied"] is True
    row = turn.read(owner, peer["peer_id"])
    assert row["turn_state"] == "open" and row["unsurfaced"] == 1
    assert dt.datetime.fromisoformat(row["turn_item_at"]) == client_now
    assert dt.datetime.fromisoformat(row["unsurfaced_oldest_at"]) == stamp


def test_observation_does_not_change_presence_or_notify(client, owner):
    from apps.wsgw.constants import PRESENCE_ROW_NOTIFY_CHANNEL
    peer = _peer(owner, "unseen-presence")
    assert post(client, peer).status_code == 200
    fields = "state,last_beat,last_activity,blocked_on,note,state_expires_at"
    before = owner.execute(f"SELECT {fields} FROM mesh_presence WHERE peer_id=%s", (peer["peer_id"],)).fetchone()
    owner.execute("LISTEN " + PRESENCE_ROW_NOTIFY_CHANNEL)
    assert post(client, peer, "closed", unsurfaced=27, unsurfaced_oldest_at=oldest()).status_code == 200
    assert owner.execute(f"SELECT {fields} FROM mesh_presence WHERE peer_id=%s", (peer["peer_id"],)).fetchone() == before
    assert not list(owner.notifies(timeout=0.1))


@pytest.mark.parametrize("since", [None, "private-path", "2026-11-04T10:00:00", "2999-01-01T00:00:00Z"])
def test_baseline_is_required_and_timestamp_only_before_admission(client, owner, since):
    peer = _peer(owner, "unseen-baseline")
    assert _post(client, peer, "closed", unsurfaced=0, unsurfaced_oldest_at=None,
                 unsurfaced_since=since).status_code == 400
    assert str(peer["peer_id"]) not in turn._reports


def test_since_roundtrip_and_legacy_clear(client, owner):
    peer = _peer(owner, "unseen-since")
    since = baseline()
    assert post(client, peer, unsurfaced=0, unsurfaced_oldest_at=None, unsurfaced_since=since).status_code == 200
    assert dt.datetime.fromisoformat(turn.read(owner, peer["peer_id"])["unsurfaced_since"]) == dt.datetime.fromisoformat(since)
    assert _post(client, peer).status_code == 200
    assert turn.read(owner, peer["peer_id"])["unsurfaced_since"] is None
