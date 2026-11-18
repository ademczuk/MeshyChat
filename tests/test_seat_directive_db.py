"""the directive rule through the real memory.write, memory.read, memory.edit and memory.redact (a test database): the
server stores a directive only from a roster seat, says in the signed read whether THIS caller is its target, stops
saying so when the switch goes off, refuses to edit or redact one, and limits each signer per hour. Every refusal has
its positive control."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from apps.memoryskills import handlers, validation
from apps.memoryskills.errors import HandlerError
from tests.test_edit import _edit_refused, _redact, _versions  # noqa: F401
from tests.test_mesh_authorship import (  # noqa: F401
    MESH, _admit, _envelope, _key, _msg, _peer, _value, _write, app_conn, owner_conn,
)


def _directive(target, hours=2, target_peer=None):
    exp = (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"v": 1, "words": ["run the the recorded rule units with the Kimi swarm, source-only"], "target": target,
            "target_peer": target_peer, "expires_at": exp}


@pytest.fixture
def seats(owner_conn, monkeypatch):
    """A roster seat (sender), its target and a third seat; only the sender is on the roster."""
    sender, target, other = (_peer(owner_conn, t) for t in ("dsa", "dta", "doa"))
    for p in (sender, target, other):
        _admit(owner_conn, MESH, p)
    monkeypatch.setattr(validation, "SEAT_DIRECTIVE_PEERS", frozenset({str(sender["peer_id"])}))
    monkeypatch.setenv(validation.SEAT_DIRECTIVES_ENV, "1")
    return sender, target, other


def _send(app_conn, sender, target, *, extra_to=(), hours=2):
    key = _key(sender["name"])
    value = dict(_value(sender["name"], "context for the directive"), to=[target["fallback"], *extra_to],
                 directive=_directive(target["fallback"], hours, str(target["peer_id"])))
    _write(app_conn, sender, _msg(MESH, key, value))
    return key, value


def _memo(app_conn, peer, key):
    return handlers.dispatch("memory.read", _envelope(peer, "memory.read", {"namespace": MESH, "key": key}),
                             peer["peer_id"], app_conn)["memo"]


def test_the_target_alone_is_told_and_the_switch_stops_a_stored_directive(app_conn, seats, monkeypatch):
    sender, target, other = seats
    key, _ = _send(app_conn, sender, target, extra_to=[other["fallback"]])
    assert _memo(app_conn, target, key)["directive_addressed_to_caller"] is True
    assert _memo(app_conn, target, key)["seat_directives"] == "on"
    assert _memo(app_conn, other, key)["directive_addressed_to_caller"] is False     # on the to line, not the target
    assert _memo(app_conn, sender, key)["directive_addressed_to_caller"] is False
    monkeypatch.setenv(validation.SEAT_DIRECTIVES_ENV, "0")
    m = _memo(app_conn, target, key)
    assert m["seat_directives"] == "off" and m["directive_addressed_to_caller"] is False
    plain_key = _key(sender["name"])
    _write(app_conn, sender, _msg(MESH, plain_key, _value(sender["name"], "plain")))
    assert "directive_addressed_to_caller" not in _memo(app_conn, target, plain_key)     # CONTROL: no field, no bit


def test_a_seat_off_the_roster_and_the_switch_off_are_refused(app_conn, seats, monkeypatch):
    sender, target, other = seats
    value = dict(_value(other["name"], "x"), to=[target["fallback"]],
                 directive=_directive(target["fallback"], target_peer=str(target["peer_id"])))
    with pytest.raises(HandlerError) as err:
        _write(app_conn, other, _msg(MESH, _key(other["name"]), value))
    assert err.value.code == "E_PERMISSION_DENIED"
    monkeypatch.setenv(validation.SEAT_DIRECTIVES_ENV, "0")
    with pytest.raises(HandlerError) as err:
        _send(app_conn, sender, target)
    assert err.value.code == "E_FEATURE_OFF"
    monkeypatch.setenv(validation.SEAT_DIRECTIVES_ENV, "1")
    assert _send(app_conn, sender, target)[0]                                            # CONTROL


def test_a_directive_is_never_edited_or_added_by_an_edit(app_conn, owner_conn, seats):
    sender, target, _ = seats
    key, value = _send(app_conn, sender, target)
    err = _edit_refused(app_conn, sender, key, dict(value, body="an edited body"), 1)
    assert err.code == "E_INVALID_ARGS" and "directive" in str(err)
    assert _versions(owner_conn, key) == 1
    plain_key = _key(sender["name"])
    plain = dict(_value(sender["name"], "plain"), to=[target["fallback"]])
    _write(app_conn, sender, _msg(MESH, plain_key, plain))
    err = _edit_refused(app_conn, sender, plain_key,
                        dict(plain, directive=_directive(target["fallback"], target_peer=str(target["peer_id"]))), 1)
    assert err.code == "E_INVALID_ARGS" and _versions(owner_conn, plain_key) == 1


def test_a_directive_is_never_redacted_and_a_plain_message_still_is(app_conn, owner_conn, seats):
    sender, target, _ = seats
    key, _ = _send(app_conn, sender, target)
    with pytest.raises(HandlerError) as err:
        _redact(app_conn, sender, key)
    assert err.value.code == "E_DIRECTIVE_IMMUTABLE"
    plain_key = _key(sender["name"])
    _write(app_conn, sender, _msg(MESH, plain_key, _value(sender["name"], "plain")))
    assert _redact(app_conn, sender, plain_key)                                          # CONTROL


def test_a_seat_s_hourly_allowance_is_counted_from_the_stored_rows(app_conn, seats, monkeypatch):
    sender, target, _ = seats
    monkeypatch.setattr(handlers, "DIRECTIVE_RATE_PER_HOUR", 2)
    _send(app_conn, sender, target)
    _send(app_conn, sender, target)
    with pytest.raises(HandlerError) as err:
        _send(app_conn, sender, target)
    assert err.value.code == "E_RATE_LIMITED"
    plain_key = _key(sender["name"])                                                     # CONTROL: plain mail flows
    _write(app_conn, sender, _msg(MESH, plain_key, dict(_value(sender["name"], "plain"), to=[target["fallback"]])))


def test_a_signer_removed_from_the_roster_stops_its_stored_directives_at_the_next_read(app_conn, seats, monkeypatch):
    """codex-oxxxxxx-admin-cli ef2965, P2-3: the signed read bit requires the signer to be on the server's CURRENT
    roster, so a client that still pins the old roster is told the directive is not addressed to it."""
    sender, target, _ = seats
    key, _ = _send(app_conn, sender, target)
    assert _memo(app_conn, target, key)["directive_addressed_to_caller"] is True              # CONTROL
    monkeypatch.setattr(validation, "SEAT_DIRECTIVE_PEERS", frozenset())
    m = _memo(app_conn, target, key)
    assert m["directive_signer_on_roster"] is False and m["directive_addressed_to_caller"] is False


def test_two_concurrent_directives_cannot_both_take_the_last_slot(owner_conn, seats, monkeypatch):
    """codex-oxxxxxx-admin-cli ef2965, P2-4: with one slot left, two sends racing on two connections; the advisory lock
    inside the write's transaction lets exactly one through."""
    import threading
    from apps.memoryskills import db
    from tests.test_mesh_authorship import DSN
    sender, target, _ = seats
    monkeypatch.setattr(handlers, "DIRECTIVE_RATE_PER_HOUR", 1)
    gate, results = threading.Barrier(2), []

    def one():
        conn = db.connect(DSN)
        try:
            gate.wait(timeout=10)
            _send(conn, sender, target)
            results.append("ok")
        except HandlerError as exc:
            results.append(exc.code)
        finally:
            conn.close()
    threads = [threading.Thread(target=one) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(results) == ["E_RATE_LIMITED", "ok"], results


def test_a_moved_label_never_gives_one_directive_a_second_target(app_conn, owner_conn, seats):
    """pxx e4345f, codex-pxx-cli F3: the directive is addressed to the PEER fixed at write. The target label moves from
    A to B afterwards: B is not addressed, A still is."""
    sender, a, b = seats
    key = _key(sender["name"])
    label = a["name"]
    value = dict(_value(sender["name"], "x"), to=[label], directive=_directive(label, target_peer=str(a["peer_id"])))
    _write(app_conn, sender, _msg(MESH, key, value))
    assert _memo(app_conn, a, key)["directive_addressed_to_caller"] is True
    owner_conn.execute("UPDATE peers SET name = %s WHERE id = %s", (label + "-old", a["peer_id"]))
    owner_conn.execute("UPDATE peers SET name = %s WHERE id = %s", (label, b["peer_id"]))
    assert _memo(app_conn, b, key)["directive_addressed_to_caller"] is False
    assert _memo(app_conn, a, key)["directive_addressed_to_caller"] is True


def test_a_target_peer_that_does_not_hold_the_label_is_refused_at_write(app_conn, seats):
    sender, a, b = seats
    value = dict(_value(sender["name"], "x"), to=[a["name"]], directive=_directive(a["name"], target_peer=str(b["peer_id"])))
    with pytest.raises(HandlerError) as err:
        _write(app_conn, sender, _msg(MESH, _key(sender["name"]), value))
    assert err.value.code == "E_INVALID_ARGS" and "target_peer" in str(err.value)
    ok = dict(value, directive=_directive(a["name"], target_peer=str(a["peer_id"])))                   # CONTROL
    assert _write(app_conn, sender, _msg(MESH, _key(sender["name"]), ok))


def test_an_identical_committed_replay_returns_its_result_after_quota_and_label_move(app_conn, owner_conn, seats,
                                                                                      monkeypatch):
    """codex-oxxxxxx-admin-cli bc9a07, P2-2: the replay check runs before a new directive's admission, so the same
    signed envelope sent again (a lost reply) returns the stored write even with the hour's allowance spent and the
    target label moved. CONTROL: a NEW directive is still refused, and no second row is written."""
    sender, a, b = seats
    monkeypatch.setattr(handlers, "DIRECTIVE_RATE_PER_HOUR", 1)
    key = _key(sender["name"])
    label = a["name"]
    value = dict(_value(sender["name"], "x"), to=[label], directive=_directive(label, target_peer=str(a["peer_id"])))
    env = _envelope(sender, "memory.write", _msg(MESH, key, value))
    first = handlers.dispatch("memory.write", env, sender["peer_id"], app_conn)
    owner_conn.execute("UPDATE peers SET name = %s WHERE id = %s", (label + "-old", a["peer_id"]))
    owner_conn.execute("UPDATE peers SET name = %s WHERE id = %s", (label, b["peer_id"]))
    again = handlers.dispatch("memory.write", env, sender["peer_id"], app_conn)
    assert again.get("version") == first.get("version") and again.get("key", key) == first.get("key", key)
    assert _versions(owner_conn, key) == 1
    with pytest.raises(HandlerError) as err:                                                       # CONTROL
        _send(app_conn, sender, b)
    assert err.value.code == "E_RATE_LIMITED"


def test_a_changed_directive_under_a_used_call_id_is_refused_never_answered_as_its_replay(app_conn, owner_conn, seats):
    """pxx 81b486 / codex-pxx-cli R2: the dedup also matches call_id. The IDENTICAL envelope returns the stored write;
    the same call_id with any changed field (body, target, target_peer, expiry) is E_REPLAY, and nothing is stored."""
    from tests.test_mesh_authorship import P
    sender, a, b = seats
    key = _key(sender["name"])
    label = a["name"]
    value = dict(_value(sender["name"], "x"), to=[label, b["name"]],
                 directive=_directive(label, target_peer=str(a["peer_id"])))
    env = _envelope(sender, "memory.write", _msg(MESH, key, value))
    first = handlers.dispatch("memory.write", env, sender["peer_id"], app_conn)
    again = handlers.dispatch("memory.write", env, sender["peer_id"], app_conn)               # identical: the replay
    assert again.get("version") == first.get("version")

    def resigned(new_value):
        payload = dict(env["payload"], args=_msg(MESH, key, new_value))                       # SAME call_id
        return {"payload": payload, "signature": P.b64u(P.sign(sender["seed"], P.jcs(payload))),
                "signing_kid": sender["kid"]}
    later = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for field, changed in (("body", dict(value, body="changed")),
                           ("target", dict(value, directive=_directive(b["name"], target_peer=str(b["peer_id"])))),
                           ("target_peer", dict(value, directive=_directive(label, target_peer=str(b["peer_id"])))),
                           ("expiry", dict(value, directive={**value["directive"], "expires_at": later}))):
        with pytest.raises(HandlerError) as err:
            handlers.dispatch("memory.write", resigned(changed), sender["peer_id"], app_conn)
        assert err.value.code == "E_REPLAY", field
    assert _versions(owner_conn, key) == 1


def test_a_replay_that_drops_the_directive_is_refused(app_conn, owner_conn, seats):
    """codex-oxxxxxx-admin-cli ba83d3: the stored message is a directive; the same call_id re-signed as an ordinary
    message (directive omitted) must not get the stored write back."""
    from tests.test_mesh_authorship import P
    sender, a, b = seats
    key = _key(sender["name"])
    value = dict(_value(sender["name"], "x"), to=[a["name"]], directive=_directive(a["name"], target_peer=str(a["peer_id"])))
    env = _envelope(sender, "memory.write", _msg(MESH, key, value))
    handlers.dispatch("memory.write", env, sender["peer_id"], app_conn)
    plain = {k: v for k, v in value.items() if k != "directive"}
    payload = dict(env["payload"], args=_msg(MESH, key, plain))
    resigned = {"payload": payload, "signature": P.b64u(P.sign(sender["seed"], P.jcs(payload))), "signing_kid": sender["kid"]}
    with pytest.raises(HandlerError) as err:
        handlers.dispatch("memory.write", resigned, sender["peer_id"], app_conn)
    assert err.value.code == "E_REPLAY"
    assert _versions(owner_conn, key) == 1


def test_an_identical_replay_after_the_key_skew_returns_its_write_and_a_new_one_is_refused(app_conn, owner_conn,
                                                                                            seats, monkeypatch):
    """pxx f6f4d3: the key-time check is admission only, after the replay check. 'Eleven minutes later' is simulated
    by a key-time check that refuses everything: the identical envelope still returns its stored write, and a NEW
    directive is refused by it."""
    from apps.memoryskills.errors import HandlerError as HE
    sender, a, _ = seats
    key = _key(sender["name"])
    value = dict(_value(sender["name"], "x"), to=[a["name"]], directive=_directive(a["name"], target_peer=str(a["peer_id"])))
    env = _envelope(sender, "memory.write", _msg(MESH, key, value))
    first = handlers.dispatch("memory.write", env, sender["peer_id"], app_conn)

    def eleven_minutes_later(_key, now=None):
        raise HE("E_INVALID_ARGS", "a directive's msg/ key must carry the current time (simulated +11 min)")
    monkeypatch.setattr(handlers, "check_directive_key_time", eleven_minutes_later)
    again = handlers.dispatch("memory.write", env, sender["peer_id"], app_conn)
    assert again.get("version") == first.get("version") and _versions(owner_conn, key) == 1
    with pytest.raises(HandlerError) as err:
        _send(app_conn, sender, a)
    assert err.value.code == "E_INVALID_ARGS" and "current time" in str(err.value)


def test_an_identical_replay_after_expiry_returns_its_write_and_a_new_expired_one_is_refused(app_conn, owner_conn,
                                                                                              seats, monkeypatch):
    """pxx 9e5d48 / GitHub Codex on a4e0235c: 'after expiry' simulated by an expiry check that refuses everything. The
    identical envelope returns its stored write (one version); a NEW directive is refused by it."""
    from apps.memoryskills.errors import HandlerError as HE
    sender, a, _ = seats
    key = _key(sender["name"])
    value = dict(_value(sender["name"], "x"), to=[a["name"]], directive=_directive(a["name"], target_peer=str(a["peer_id"])))
    env = _envelope(sender, "memory.write", _msg(MESH, key, value))
    first = handlers.dispatch("memory.write", env, sender["peer_id"], app_conn)

    def expired(_value, now=None):
        raise HE("E_INVALID_ARGS", "directive.expires_at is not in the future")
    monkeypatch.setattr(handlers, "check_directive_not_expired", expired)
    again = handlers.dispatch("memory.write", env, sender["peer_id"], app_conn)
    assert again.get("version") == first.get("version") and _versions(owner_conn, key) == 1
    with pytest.raises(HandlerError) as err:
        _send(app_conn, sender, a)
    assert err.value.code == "E_INVALID_ARGS" and "not in the future" in str(err.value)
