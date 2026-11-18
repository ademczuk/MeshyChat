"""A write's replay dedup answers only for the write's own (namespace, key) (B07 slice 4; design
docs/design/biscuit-capabilities.md 5.2, the dedup row, and critique C14; divergence 040).

WHAT CHANGED, FOR EVERY WRITE, TOKEN OR NOT. handle_write's replay dedup (`_existing_by_dedup`)
finds a row by payload hash OR call_id in ANY
namespace and answers it as the caller's replay: `{"namespace", "key", "version", "id",
"replay": true}` of THAT row, and the new write silently does not land. Under a key-scoped
authority that answers with another key's namespace, key and version. The design leaves open
whether the no-token path changes; this slice changes it for everyone, and says so here and in
040: a dedup hit on another (namespace, key) is refused E_REPLAY, naming nothing of that row.
Every write's authorised resource is its own `Key(key)` since slice 2, so "the authorised
(namespace, key)" and "the write's own" are one thing, with a token or without.

A payload-hash hit is always the same (namespace, key) (the hash covers the args), so only a
call_id reused for a different write can reach the refusal; the exact replay is answered as
before (the control). Both places the dedup is read are held: the read before the write, and
the re-read after a unique-violation race (handlers.py, handle_write).

Run:  pytest tests/test_write_dedup_scope.py -q
Needs the dev database (tests/conftest.py builds and drops its own).
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")

from apps.memoryskills import db, handlers  # noqa: E402
from apps.memoryskills.errors import HandlerError  # noqa: E402
from tests.test_memory_skills import DSN, _call, _claim, _envelope, _ns, _peer  # noqa: E402


@pytest.fixture()
def owner_conn():
    with psycopg.connect(DSN) as conn:
        yield conn


@pytest.fixture()
def app_conn():
    conn = db.connect(DSN)
    try:
        yield conn
    finally:
        conn.close()


def _rows(owner_conn, ns: str) -> list:
    with owner_conn.cursor() as cur:
        cur.execute("SELECT key, version FROM shared_memory WHERE namespace = %s "
                    "ORDER BY key, version", (ns,))
        return cur.fetchall()


@pytest.fixture()
def written(app_conn, owner_conn):
    """Signer S's first write, k1 under call_id C, and the exact replay answered as before."""
    s = _peer(owner_conn, "dedup")
    ns = _ns("dedup")
    _claim(app_conn, s, ns)
    call_id = f"call-{uuid.uuid4()}"
    env = _envelope(s, "memory.write", {"namespace": ns, "key": "k1", "value": {"n": 1}},
                    call_id=call_id)
    first = handlers.dispatch("memory.write", env, s["peer_id"], app_conn)
    assert first["replay"] is False and first["key"] == "k1"
    # CONTROL: the SAME envelope again is the replay of the same write, as before. Since
    # divergence 039 (integ/r11: main's, which this branch's base did not have) a replay
    # answers in the first write's shape, every field read from the row, with replay true.
    again = handlers.dispatch("memory.write", env, s["peer_id"], app_conn)
    assert again == {**first, "replay": True}, again
    return s, ns, call_id, first


def test_a_call_id_reused_for_another_key_is_refused_and_names_nothing_of_that_row(
        written, app_conn, owner_conn):
    s, ns, call_id, first = written
    with pytest.raises(HandlerError) as err:
        _call(app_conn, s, "memory.write", {"namespace": ns, "key": "k2", "value": {"n": 2}},
              call_id=call_id)
    assert err.value.code == "E_REPLAY", (err.value.code, err.value.message)
    for leaked in ("k1", first["id"], ns):
        assert leaked not in err.value.message, err.value.message
    assert _rows(owner_conn, ns) == [("k1", 1)], "nothing may be written, and k1 is intact"
    # CONTROL: the same write under a call_id of its own lands.
    landed = _call(app_conn, s, "memory.write", {"namespace": ns, "key": "k2",
                                                  "value": {"n": 2}})
    assert landed["replay"] is False and landed["key"] == "k2"


def test_a_call_id_reused_in_another_namespace_is_refused(written, app_conn, owner_conn):
    s, ns, call_id, _first = written
    other = _ns("dedup2")
    _claim(app_conn, s, other)
    with pytest.raises(HandlerError) as err:
        _call(app_conn, s, "memory.write", {"namespace": other, "key": "k1",
                                            "value": {"n": 3}}, call_id=call_id)
    assert err.value.code == "E_REPLAY"
    assert _rows(owner_conn, other) == []


def test_the_race_re_read_is_held_to_the_same_key(written, app_conn, owner_conn,
                                                   monkeypatch):
    """The second read: the dedup lookup before the write misses (as when the other write had
    not committed yet), the INSERT hits the call_id's unique index, and the re-read finds the
    other key's row. It is refused there too, not answered as a replay."""
    s, ns, call_id, _first = written
    real = handlers._existing_by_dedup
    calls = []

    def first_misses(conn, jcs_sha, cid):
        calls.append(cid)
        return None if len(calls) == 1 else real(conn, jcs_sha, cid)

    monkeypatch.setattr(handlers, "_existing_by_dedup", first_misses)
    with pytest.raises(HandlerError) as err:
        _call(app_conn, s, "memory.write", {"namespace": ns, "key": "k2", "value": {"n": 4}},
              call_id=call_id)
    assert len(calls) == 2, "control: the write reached the race path's re-read"
    assert err.value.code == "E_REPLAY", (err.value.code, err.value.message)
    assert _rows(owner_conn, ns) == [("k1", 1)]
