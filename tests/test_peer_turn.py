"""Turn reports authenticate ownership and do not turn a never-beaten peer into active presence."""
import datetime as dt
import json

import pytest

from apps.mesh import work
from apps.peers import presence, turn
from tests.test_mesh_routes import _auth, _peer, client, mesh_settings, owner  # noqa: F401


@pytest.fixture(autouse=True)
def reset_turn_limits():
    turn._reports.clear()
    work.forget()
    yield
    turn._reports.clear()
    work.forget()


def post(client, peer, state="open", **extra):
    body = {"state": state, "item_at": dt.datetime.now(dt.timezone.utc).isoformat(), **extra}
    return client.post("/v1/peers/me/turn", data=json.dumps(body), content_type="application/json",
                       **_auth(peer["peer_id"]))


def test_token_ownership_upsert_and_existing_presence_are_separate(client, owner):
    a, b = _peer(owner, "turn-a"), _peer(owner, "turn-b")
    assert post(client, a).status_code == 200
    with owner.cursor() as cur:
        cur.execute("SELECT state,last_beat,last_activity,blocked_on,note,turn_state FROM mesh_presence WHERE peer_id=%s",
                    (a["peer_id"],))
        assert cur.fetchone() == ("active", None, None, None, None, "open")
    assert presence.states_of(owner, [a["peer_id"]])[str(a["peer_id"])]["effective_state"] == "unknown"
    # The body cannot choose another peer, and a report cannot fabricate its row.
    assert post(client, a, peer_id=str(b["peer_id"])).status_code == 400
    assert turn.read(owner, b["peer_id"])["turn_state"] is None
    with owner.cursor() as cur:
        cur.execute("UPDATE mesh_presence SET state='asleep',last_beat=NOW(),note='keep',last_activity=NOW(),blocked_on=%s "
                    "WHERE peer_id=%s", (b["peer_id"], a["peer_id"]))
        cur.execute("SELECT state,last_beat,last_activity,note,state_expires_at,blocked_on FROM mesh_presence WHERE peer_id=%s",
                    (a["peer_id"],))
        before = cur.fetchone()
    assert post(client, a, "closed").status_code == 200
    with owner.cursor() as cur:
        cur.execute("SELECT state,last_beat,last_activity,note,state_expires_at,blocked_on FROM mesh_presence WHERE peer_id=%s",
                    (a["peer_id"],))
        assert cur.fetchone() == before


def test_turn_writes_no_presence_notification_with_a_beat_positive_control(client, owner):
    from apps.wsgw.constants import PRESENCE_ROW_NOTIFY_CHANNEL
    a = _peer(owner, "turn-no-notify")
    owner.execute("LISTEN " + PRESENCE_ROW_NOTIFY_CHANNEL)
    assert post(client, a).status_code == 200
    notifications = list(owner.notifies(timeout=0.1))
    assert not any(str(a["peer_id"]) in item.payload for item in notifications)
    presence.beat(owner, a["peer_id"], {"state": "active"})
    notifications = list(owner.notifies(timeout=0.1))
    assert any(str(a["peer_id"]) in item.payload for item in notifications)


@pytest.mark.parametrize("extra", [{"state": "idle"}, {"item_at": "not-a-date"},
    {"item_at": "2026-11-04T12:00:00"}, {"item_at": True},
    {"item_at": "2999-01-01T00:00:00Z"}, {"text": "must not leave box"}])
def test_invalid_payload_is_refused_before_rate_budget(client, owner, extra):
    a = _peer(owner, "turn-invalid")
    assert post(client, a, **extra).status_code == 400
    assert str(a["peer_id"]) not in turn._reports


def test_four_in_a_rolling_minute_and_new_peer_cannot_reset_full_table(client, owner, monkeypatch):
    a = _peer(owner, "turn-limit")
    now = [0.0]
    monkeypatch.setattr(turn, "_clock", lambda: now[0])
    for _ in range(4):
        assert post(client, a).status_code == 200
    assert post(client, a).status_code == 429
    monkeypatch.setattr(turn, "TRACKED_MAX", 1)
    b = _peer(owner, "turn-limit-other")
    assert post(client, b).status_code == 429
    assert len(turn._reports) == 1 and str(a["peer_id"]) in turn._reports
    now[0] = 60.0
    assert post(client, b).status_code == 200


def test_turn_expiry_is_not_held_by_the_heavy_work_cache(client, owner):
    a = _peer(owner, "turn-expire")
    assert post(client, a, "closed").status_code == 200
    first = work.snapshot(owner, a["peer_id"], [a["name"]])
    assert first["turn_state"] == "closed"
    owner.execute("UPDATE mesh_presence SET turn_reported_at=NOW()-INTERVAL '601 seconds' WHERE peer_id=%s",
                  (a["peer_id"],))
    held = work.snapshot(owner, a["peer_id"], [a["name"]])
    assert held["work_as_of"] == first["work_as_of"]
    assert held["turn_state"] is None and held["turn_item_at"] is None
    assert post(client, a).status_code == 200
    assert work.snapshot(owner, a["peer_id"], [a["name"]])["turn_state"] == "open"


def test_old_reports_cannot_revert_a_newer_session_and_routes_need_auth(client, owner):
    a = _peer(owner, "turn-order")
    fresh = post(client, a)
    assert fresh.status_code == 200 and fresh.json() == {"ok": True, "applied": True}
    old = post(client, a, "closed", item_at="2020-01-01T00:00:00Z")
    assert old.status_code == 200 and old.json() == {"ok": True, "applied": False}
    assert turn.read(owner, a["peer_id"])["turn_state"] == "open"
    assert client.post("/v1/peers/me/turn", data="{}", content_type="application/json").status_code == 401
    assert client.get("/v1/peers/me/turn").status_code == 405
