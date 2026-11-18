"""Opt-in fleet facts: approved counts-only audience, bounded windows and fault isolation."""
import datetime as dt
import uuid
from types import SimpleNamespace

import pytest

from apps.mesh import graph, work
from apps.memoryskills import handlers
from tests.fixtures import signed_rows as sr
from tests.test_mesh_routes import (  # noqa: F401
    _auth, _peer, _row as fixture_row, _row_at as fixture_row_at, client, mesh_settings, owner, P,
)


@pytest.fixture(autouse=True)
def clear_work_cache():
    work.forget()
    yield
    work.forget()


def _get(client, caller, labels, detail="work"):
    suffix = "" if detail is None else "&detail=" + detail
    return client.get("/v1/mesh/recipients?to=" + ",".join(labels) + suffix,
                      **_auth(caller["peer_id"]))


def _row(conn, peer, value):
    # Route resolution uses peers.name; _peer's separate fixture label is not that identity.
    return fixture_row(conn, dict(peer, label=peer["name"]), value)


def _row_at(conn, peer, value, created_at):
    return fixture_row_at(conn, dict(peer, label=peer["name"]), value, created_at)


def test_default_send_path_never_reads_work_facts(client, owner, monkeypatch):
    a, b = _peer(owner, "wa"), _peer(owner, "wb")
    monkeypatch.setattr(work, "snapshot", lambda *a, **k: pytest.fail("default route did extra work"))
    answer = _get(client, a, [b["name"]], detail=None)
    assert answer.status_code == 200
    body = answer.json()
    assert "work" not in body
    assert not {"last_sent_at", "indexed_turn_at", "owed", "awaiting"} & body["recipients"][0]["peers"][0].keys()


def test_every_peer_gets_exact_windowed_counts_and_only_recipient_labels(client, owner):
    a, b = _peer(owner, "wc"), _peer(owner, "wd")
    _row(owner, b, {"to": [a["name"]], "thread": "fleet-work", "body": "private",
                    "requires_ack": True})
    _row(owner, a, {"to": [b["name"]], "thread": "fleet-work-2", "body": "private",
                    "requires_ack": True})
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=20)
    _row_at(owner, a, {"to": [b["name"]], "thread": "fleet-work-old", "requires_ack": True}, old)
    P.rebuild(owner)
    answer = _get(client, a, [a["name"], b["name"]])
    assert answer.status_code == 200, answer.content
    body = answer.json()
    rows = {r["label"]: r["peers"][0] for r in body["recipients"]}
    mine, other = rows[a["name"]], rows[b["name"]]
    assert body["work"] == {"counts_scope": "recipients", "held_seconds": 60, "ask_days": 14, "sent_days": 7}
    for peer in (a, b):
        row = rows[peer["name"]]
        since = dt.datetime.fromisoformat(row["work_as_of"]) - dt.timedelta(days=14)
        counts = graph.awaiting_counts(owner, [peer["name"]], since=since)
        params = {"state": "open,active", "since": since.isoformat()}
        owed = client.get("/v1/mesh/owed", params, **_auth(peer["peer_id"])).json()["owed"]
        awaiting = client.get("/v1/mesh/awaiting", params, **_auth(peer["peer_id"])).json()["awaiting"]
        for state in ("open", "active"):
            assert row["owed"][state] == sum(r["state"] == state for r in owed)
            assert row["awaiting"][state] == sum(r["state"] == state for r in awaiting) == counts[state]
    assert mine["owed"]["open"] == other["owed"]["open"] == 1
    assert mine["awaiting"]["recipients"] == [b["name"]]
    assert other["awaiting"]["recipients"] == [a["name"]]
    assert mine["last_sent_at"] is not None and other["last_sent_at"] is not None
    assert mine["indexed_turn_at"] is None
    assert "private" not in str(body)


def test_one_label_with_two_peers_keeps_writer_timestamps_and_empty_peer_distinct(client, owner, monkeypatch):
    label = "fleet-shared-" + uuid.uuid4().hex[:12]
    writer = _peer(owner, "shared-writer")
    empty = _peer(owner, "shared-empty")
    # Duplicate peers.name values are contested under divergence 066; a shared roster alias is valid.
    monkeypatch.setattr(handlers, "_ROSTER_LABELS", {
        str(writer["peer_id"]): label, str(empty["peer_id"]): label,
    })
    _row(owner, writer, {"to": ["unrelated-label"], "body": "private shared-label send"})
    P.rebuild(owner)
    answer = _get(client, writer, [label])
    assert answer.status_code == 200, answer.content
    peers = {row["peer_id"]: row for row in answer.json()["recipients"][0]["peers"]}
    assert set(peers) == {str(writer["peer_id"]), str(empty["peer_id"])}
    assert peers[str(writer["peer_id"])]["last_sent_at"] is not None
    quiet = peers[str(empty["peer_id"])]
    assert quiet["last_sent_at"] is None and quiet["indexed_turn_at"] is None
    assert quiet["owed"] == {"open": 0, "active": 0}
    assert quiet["awaiting"] == {"open": 0, "active": 0, "recipients": []}
    assert quiet["work_as_of"] is not None
    assert "private shared-label send" not in str(answer.json())


def test_optional_sql_failure_keeps_rows_and_rolls_back_before_next_peer(client, owner, monkeypatch, caplog):
    a, b = _peer(owner, "work-ok"), _peer(owner, "work-fail")
    original = work.snapshot
    def failing(conn, pid, labels):
        if str(pid) == str(b["peer_id"]):
            conn.execute("SELECT 1 / 0")  # poison the transaction unless the route uses a savepoint
        return original(conn, pid, labels)
    monkeypatch.setattr(work, "snapshot", failing)
    response = _get(client, a, [b["name"], a["name"]])
    assert response.status_code == 200, response.content
    rows = {r["label"]: r["peers"][0] for r in response.json()["recipients"]}
    assert rows[b["name"]]["owed"] is None and rows[b["name"]]["work_as_of"] is None
    assert rows[a["name"]]["owed"] == {"open": 0, "active": 0}
    assert "unread_count" in rows[b["name"]]
    assert "fleet work unavailable peer=" in caplog.text and "DivisionByZero" in caplog.text


def test_indexed_time_uses_live_heads_and_filters_namespace_and_seat(client, owner, monkeypatch):
    peer = _peer(owner, "indexed-work")
    ns, space = "shared/fleet-work-" + uuid.uuid4().hex, "fleet-work-" + uuid.uuid4().hex
    monkeypatch.setattr(work, "context_namespace", lambda: ns)
    monkeypatch.setattr(work, "_context_space", lambda: space)          # the configured space (059)
    owner.execute("INSERT INTO shared_memory_namespace (namespace, owner_peer_id, owner_owner_id, claimant_plane, "
                  "ephemeral, retention_policy) VALUES (%s,%s,%s,'dev',FALSE,'keep_all')",
                  (ns, peer["peer_id"], peer["owner_id"]))
    now = dt.datetime.now(dt.timezone.utc)
    expected = now - dt.timedelta(hours=2)
    for i, (head, seat, chunk_ns, stamp) in enumerate([
        (True, peer["name"], ns, expected),
        (False, peer["name"], ns, now - dt.timedelta(minutes=1)),
        (True, "unrelated-seat", ns, now),
        (True, peer["name"], ns + "/other", now),
    ]):
        src = uuid.uuid4().hex
        rid = sr.insert(owner, sr.row(peer["peer_id"], peer["owner_id"], ns,
                                     f"ctxm/{peer['name']}/{src}/0001/000001",
                                     {"schema": "context.v1", "kind": "manifest"},
                                     author_label=peer["name"], is_head=head))
        owner.execute("INSERT INTO mesh_ctx_chunks (src_id, space, namespace, chunk_no, seat, src, gen, batch, "
                      "first_ord, last_ord, char_start, char_end, time_first, time_last, text_sha256) "
                      "VALUES (%s,%s,%s,0,%s,%s,1,1,1,1,0,1,%s,%s,%s)",
                      (rid, space, chunk_ns, seat, src, stamp, stamp, f"{i:064x}"))
    response = _get(client, peer, [peer["name"]])
    assert response.status_code == 200, response.content
    row = response.json()["recipients"][0]["peers"][0]
    assert dt.datetime.fromisoformat(row["indexed_turn_at"]) == expected


def test_awaiting_counts_are_complete_while_recipient_sample_is_capped(client, owner):
    sender = _peer(owner, "sample-sender")
    targets = [_peer(owner, f"sample-target{i}") for i in range(4)]
    for i, target in enumerate(targets):
        _row(owner, sender, {"to": [target["name"]], "thread_id": f"work-sample-{i}", "requires_ack": True})
    # A non-direct response in one thread marks that ask active, leaving the others open.
    _row(owner, targets[0], {"to": ["someone-else"], "thread_id": "work-sample-0"})
    P.rebuild(owner)
    response = _get(client, targets[0], [sender["name"]])
    assert response.status_code == 200, response.content
    row = response.json()["recipients"][0]["peers"][0]
    since = dt.datetime.fromisoformat(row["work_as_of"]) - dt.timedelta(days=14)
    counts = graph.awaiting_counts(owner, [sender["name"]], since=since)
    assert {s: row["awaiting"][s] for s in ("open", "active")} == {"open": 3, "active": 1}
    assert all(row["awaiting"][s] == counts[s] for s in ("open", "active"))
    assert len(row["awaiting"]["recipients"]) == 2
    assert set(row["awaiting"]["recipients"]) <= {t["name"] for t in targets}


def test_no_send_in_seven_days_and_unknown_label(client, owner):
    a = _peer(owner, "work-old-send")
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=8)
    _row_at(owner, a, {"to": ["someone"], "body": "old"}, old)
    response = _get(client, a, [a["name"], "absent-work-label"])
    assert response.status_code == 200, response.content
    rows = {r["label"]: r for r in response.json()["recipients"]}
    assert rows[a["name"]]["peers"][0]["last_sent_at"] is None
    assert rows["absent-work-label"]["peers"] == []
    assert _get(client, a, ["absent-work-label"]).status_code == 200


def test_unknown_detail_is_refused(client, owner):
    a = _peer(owner, "we")
    response = _get(client, a, [a["name"]], "everything")
    assert response.status_code == 422


class FakeConnection:
    def __init__(self):
        self.info = SimpleNamespace(host="h", port=1, dbname="db")
        self.calls = []
        self.stamp = dt.datetime(2026, 11, 4, tzinfo=dt.timezone.utc)
        self.fail = False

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params):
        self.calls.append((sql, params))
        if self.fail:
            raise RuntimeError("query failed")

    def fetchone(self):
        return (self.stamp,)


@pytest.fixture
def fake_facts(monkeypatch):
    # These tests isolate the expensive mail cache. Real turn reads/expiry are pinned in test_peer_turn.
    monkeypatch.setattr(work.turn, "read", lambda *a: {"turn_state": None, "turn_item_at": None,
                                                    "turn_reported_at": None})
    calls = []
    def owed(conn, labels, *, since):
        calls.append(("owed", since))
        return {"open": 2, "active": 3}
    def awaiting(conn, labels, *, states, since, limit):
        assert states == ("open", "active") and limit is None
        calls.append(("awaiting", since))
        return [SimpleNamespace(owing_label="target", state="open")]
    monkeypatch.setattr(work.graph, "owed_counts", owed)
    monkeypatch.setattr(work.graph, "awaiting", awaiting)
    monkeypatch.setattr(work.graph, "awaiting_counts", lambda *a, **k: pytest.fail("duplicate awaiting read"))
    return calls


def test_snapshot_bounds_all_ask_reads_and_reads_awaiting_once(fake_facts):
    c = FakeConnection()
    got = work.snapshot(c, "p", ["seat"])
    assert got["owed"] == {"open": 2, "active": 3}
    assert got["awaiting"] == {"open": 1, "active": 0, "recipients": ["target"]}
    as_of = dt.datetime.fromisoformat(got["work_as_of"])
    assert fake_facts == [("owed", as_of - dt.timedelta(days=14)), ("awaiting", as_of - dt.timedelta(days=14))]
    assert got["last_sent_at"] == c.stamp.isoformat()
    assert len(c.calls) == 2
    assert "created_by_peer_id = %s" in c.calls[0][0]
    assert c.calls[0][1][1] == f"msg/{int((as_of - dt.timedelta(days=7)).timestamp() * 1000)}"
    assert c.calls[0][1][3] == as_of - dt.timedelta(days=7)
    assert "sm.is_head" in c.calls[1][0]


def test_hold_expires_and_is_scoped_by_database_peer_labels_and_namespace(monkeypatch, fake_facts):
    c = FakeConnection()
    now = [0.0]
    monkeypatch.setattr(work, "_clock", lambda: now[0])
    first = work.snapshot(c, "p", ["seat"])
    first["awaiting"]["recipients"].append("not-cached")
    c.stamp += dt.timedelta(minutes=1)
    now[0] = 59
    held = work.snapshot(c, "p", ["seat"])
    assert held["awaiting"]["recipients"] == ["target"] and len(c.calls) == 2
    now[0] = 60
    fresh = work.snapshot(c, "p", ["seat"])
    assert fresh["last_sent_at"] != held["last_sent_at"] and len(c.calls) == 4
    c.info.dbname = "other-db"
    work.snapshot(c, "p", ["seat"])
    assert len(c.calls) == 6
    work.snapshot(c, "q", ["seat"])
    assert len(c.calls) == 8
    work.snapshot(c, "p", ["alias"])
    assert len(c.calls) == 10
    monkeypatch.setattr(work, "context_namespace", lambda: "other-namespace")
    work.snapshot(c, "p", ["seat"])
    assert len(c.calls) == 12


def test_failed_reads_are_not_cached_and_cache_has_a_fixed_bound(monkeypatch, fake_facts):
    c = FakeConnection()
    c.fail = True
    with pytest.raises(RuntimeError):
        work.snapshot(c, "p", ["seat"])
    assert not work._held
    c.fail = False
    monkeypatch.setattr(work, "HELD_MAX", 2)
    for pid in ("p", "q", "r"):
        work.snapshot(c, pid, [pid])
    assert len(work._held) == 2
