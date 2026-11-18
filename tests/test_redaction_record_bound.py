"""sql_meshychat/031 (divergence 061): a redaction record is held to its row.

memory-bxxxxx's suggestion after a test leak (the comment at the head of the migration): the commit-order stamp, the wait and the live push read the RECORD's
namespace and key, and 027's exemption reads its owner column, and nothing held them to the row the record is FOR. These tests use the OWNER connection on
purpose: the rule must bind the role that bypasses row-level security too, since a BEFORE trigger fires for it. Each condition is taken away ALONE, the record
otherwise exactly what the handler writes, and the control is that record.

Run:  .venv/Scripts/python.exe -m pytest tests/test_redaction_record_bound.py -q -p no:cacheprovider
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import psycopg
import psycopg.errors
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_memory_skills import _call, app_conn, owner_conn  # noqa: E402,F401  (fixtures)
from tests.test_redaction_db_rule import _written  # noqa: E402

INSERT = ("INSERT INTO shared_memory_redaction (memory_id, namespace, key, version, redacted_by_peer_id, redacted_by_owner_id, acted_as, "
          "reason_code, request_signed_bytes) VALUES (%s, %s, %s, %s, %s, %s, 'author', 'sent_in_error', %s)")
COUNT = "SELECT count(*) FROM shared_memory_redaction WHERE memory_id = %s"


def test_the_record_is_bound_to_its_row_each_condition_alone(app_conn, owner_conn):
    peer, ns, key, memory_id, owner_id = _written(app_conn, owner_conn, "rb1")
    other = _written(app_conn, owner_conn, "rb1o")                        # a peer of ANOTHER owner
    fields = ("memory_id", "namespace", "key", "version", "peer", "owner")
    bound = (memory_id, ns, key, 1, peer["peer_id"], owner_id)             # what the handler writes

    def refused(match="E_REDACTION_RECORD_UNBOUND", **changed):
        args = list(bound)
        for name, replacement in changed.items():
            args[fields.index(name)] = replacement
        with pytest.raises(psycopg.errors.CheckViolation, match=match):
            owner_conn.execute(INSERT, (*args, b"request"))
        owner_conn.rollback()

    refused(namespace="shared/mesh-chat")                                  # the mail's name on a row of another namespace: the leak this closes
    refused(key="k-" + uuid.uuid4().hex[:8])
    refused(version=2)                                                     # the table's own CHECK holds (version >= 1); the row is version 1
    refused(owner=uuid.uuid4())                                            # the owner is what `peers` says, not what the record says
    refused(peer=other[0]["peer_id"])                                      # the column says our owner, the redacting peer is another owner's
    refused(match="there is none", memory_id=uuid.uuid4())                 # no such row: this message, not the general one
    assert owner_conn.execute(COUNT, (memory_id,)).fetchone()[0] == 0, "a refused record was written"
    owner_conn.rollback()
    owner_conn.execute(INSERT, (*bound, b"request"))                       # CONTROL: the handler's own record
    assert owner_conn.execute(COUNT, (memory_id,)).fetchone()[0] == 1
    owner_conn.commit()


def test_a_record_cannot_claim_a_version_below_its_row(app_conn, owner_conn):
    peer, ns, key, first_id, owner_id = _written(app_conn, owner_conn, "rbv2")
    written = _call(app_conn, peer, "memory.write", {
        "namespace": ns, "key": key, "value": {"n": 2}, "expected_head_version": 1,
    })
    assert written["version"] == 2
    memory_id = owner_conn.execute(
        "SELECT id FROM shared_memory WHERE namespace = %s AND key = %s AND version = 2",
        (ns, key),
    ).fetchone()[0]
    assert memory_id != first_id
    owner_conn.commit()

    with pytest.raises(psycopg.errors.CheckViolation, match="E_REDACTION_RECORD_UNBOUND"):
        owner_conn.execute(INSERT, (memory_id, ns, key, 1, peer["peer_id"], owner_id, b"request"))
    owner_conn.rollback()
    assert owner_conn.execute(COUNT, (memory_id,)).fetchone()[0] == 0
    owner_conn.rollback()
    # The same row and record succeed when their versions agree.
    owner_conn.execute(INSERT, (memory_id, ns, key, 2, peer["peer_id"], owner_id, b"request"))
    assert owner_conn.execute(COUNT, (memory_id,)).fetchone()[0] == 1
    owner_conn.commit()


def test_a_consistent_record_of_another_owner_is_not_this_triggers_to_refuse(app_conn, owner_conn):
    """The binding says the record matches its row and that its owner is the one `peers` gives its peer. WHO MAY REDACT a row is 027's rule,
    at the tombstone: a record of another owner that is consistent with itself is admitted here and refused there. Pinned so the two rules
    stay two."""
    peer, ns, key, memory_id, owner_id = _written(app_conn, owner_conn, "rb3")
    other = _written(app_conn, owner_conn, "rb3o")
    owner_conn.execute(INSERT, (memory_id, ns, key, 1, other[0]["peer_id"], other[4], b"request"))
    assert owner_conn.execute(COUNT, (memory_id,)).fetchone()[0] == 1
    owner_conn.commit()


def test_the_handlers_own_record_meets_the_binding(app_conn, owner_conn):
    """The trigger must not stand in the handler's way: a redaction through `memory.redact` still writes its record."""
    from tests.test_redaction import _redact
    peer, ns, key, memory_id, owner_id = _written(app_conn, owner_conn, "rb2")
    _redact(app_conn, peer, ns, key)
    with owner_conn.cursor() as cur:
        cur.execute("SELECT namespace, key, version, redacted_by_owner_id FROM shared_memory_redaction WHERE memory_id = %s", (memory_id,))
        assert cur.fetchall() == [(ns, key, 1, owner_id)]
    owner_conn.commit()
