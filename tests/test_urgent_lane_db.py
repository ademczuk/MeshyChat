"""The urgent lane through the real memory.write and memory.edit (a test database): the hourly allowance is counted
from the stored rows, urgent_declined is accepted only at the limit, an edit never adds or keeps urgency, and plain
mail is never counted. Every refusal has its positive control. kxxx ef884a; oxxxxxx 34bbcb."""
from __future__ import annotations

import pytest

from apps.memoryskills import handlers, validation
from apps.memoryskills.errors import HandlerError
from tests.test_edit import _edit_refused, _versions  # noqa: F401
from tests.test_mesh_authorship import (  # noqa: F401
    MESH, _admit, _envelope, _key, _msg, _peer, _value, _write, app_conn, owner_conn,
)


@pytest.fixture
def seats(owner_conn, monkeypatch):
    sender, coordinator = (_peer(owner_conn, t) for t in ("usa", "uco"))
    for p in (sender, coordinator):
        _admit(owner_conn, MESH, p)
    monkeypatch.setenv(validation.URGENT_LANE_ENV, "1")
    monkeypatch.setattr(handlers, "URGENT_RATE_PER_HOUR", 2)
    return sender, coordinator


def _send(app_conn, sender, to, **fields):
    key = _key(sender["name"])
    value = dict(_value(sender["name"], "a parked question"), to=[to["fallback"]], **fields)
    _write(app_conn, sender, _msg(MESH, key, value))
    return key, value


def test_the_allowance_is_counted_from_stored_rows_and_plain_mail_is_not(app_conn, seats):
    sender, coord = seats
    _send(app_conn, sender, coord)                                     # plain: never counted
    _send(app_conn, sender, coord, urgent=True)
    _send(app_conn, sender, coord, urgent=True)
    with pytest.raises(HandlerError) as err:
        _send(app_conn, sender, coord, urgent=True)
    assert err.value.code == "E_RATE_LIMITED" and "urgent_declined" in str(err.value)
    assert _send(app_conn, sender, coord)[0]                           # CONTROL: plain mail still flows


def test_urgent_declined_is_the_senders_own_label_and_is_never_counted(app_conn, seats):
    """GitHub Codex on cb0f3db4: a declined resend is accepted whether or not a slot freed meanwhile, and it never
    uses the allowance (it is delivered as ordinary)."""
    sender, coord = seats
    assert _send(app_conn, sender, coord, urgent_declined="rate_limited")[0]        # below the limit: accepted
    _send(app_conn, sender, coord, urgent=True)
    _send(app_conn, sender, coord, urgent=True)                                     # limit 2 (fixture): now at it
    assert _send(app_conn, sender, coord, urgent_declined="rate_limited")[0]        # at the limit: accepted


def test_the_switch_off_refuses_at_write(app_conn, seats, monkeypatch):
    sender, coord = seats
    monkeypatch.setenv(validation.URGENT_LANE_ENV, "0")
    with pytest.raises(HandlerError) as err:
        _send(app_conn, sender, coord, urgent=True)
    assert err.value.code == "E_FEATURE_OFF"


def test_an_edit_never_adds_or_keeps_urgency(app_conn, owner_conn, seats):
    """oxxxxxx 34bbcb point 4: an edit that tries to add urgent is refused; an urgent message is not edited either."""
    sender, coord = seats
    plain_key, plain = _send(app_conn, sender, coord)
    err = _edit_refused(app_conn, sender, plain_key, dict(plain, body="edited", urgent=True), 1)
    assert err.code == "E_INVALID_ARGS" and "urgent" in str(err) and _versions(owner_conn, plain_key) == 1
    key, value = _send(app_conn, sender, coord, urgent=True)
    err = _edit_refused(app_conn, sender, key, dict(value, body="edited"), 1)
    assert err.code == "E_INVALID_ARGS" and _versions(owner_conn, key) == 1


def test_the_rate_query_reads_one_hour_of_keys():
    import inspect
    src = inspect.getsource(handlers._urgent_count)
    assert "key >= %s AND key < %s" in src and "LIMIT %s" in src and "value ? 'urgent'" in src


def test_redacting_an_urgent_message_does_not_refill_the_allowance(app_conn, seats):
    """GitHub Codex on e70b2c21: the tombstone keeps `urgent`, so the redacted row still counts."""
    from tests.test_edit import _redact
    sender, coord = seats
    key, _ = _send(app_conn, sender, coord, urgent=True)
    _send(app_conn, sender, coord, urgent=True)
    assert _redact(app_conn, sender, key)
    with pytest.raises(HandlerError) as err:
        _send(app_conn, sender, coord, urgent=True)
    assert err.value.code == "E_RATE_LIMITED"
