"""sql_meshychat/027 (divergence 053): the database rule under memory.redact.

Design: docs/design/redaction.md, section 5 and tests 11, 12 and 13 of its section 6. These
tests use the OWNER connection on purpose: the rule must bind the role that bypasses row-level
security too, since a BEFORE trigger fires for it.

Each refusal is paired with a control showing the same statement CAN succeed when the rule's
condition is met, so a refusal is never the statement being wrong for another reason.

Run:  .venv/Scripts/python.exe -m pytest tests/test_redaction_db_rule.py -q -p no:cacheprovider
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from apps.memoryskills import handlers  # noqa: E402

from tests import test_mesh_authorship as mesh  # noqa: E402  (its helpers for a real mail row; its fixtures stay out of this module)
from tests.test_memory_skills import (  # noqa: E402,F401
    _claim, _envelope, _ns, _peer, app_conn, owner_conn,
)

MARKER = "zq-redaction-marker-7f3a"
ENVELOPE = ("(provenance_jsonb - 'payload') || " + "jsonb_build_object('payload_redacted', true, 'payload', jsonb_build_object('call_id', provenance_jsonb -> 'payload' -> 'call_id'))")
NO_SIGNATURE = "jsonb_build_object('payload_redacted', true, 'payload', jsonb_build_object('call_id', provenance_jsonb -> 'payload' -> 'call_id'))"
#: What the handler's tombstone is for `_written`'s row ({"body": ..., "from": "a"}): the routing fields it kept, and no other.
TOMBSTONE = {"redacted": True, "reason_code": "sent_in_error", "from": "a"}


def _written(app_conn, owner_conn, tag):
    peer = _peer(owner_conn, tag)
    ns = _ns(tag)
    _claim(app_conn, peer, ns)
    key = f"k-{uuid.uuid4().hex[:8]}"
    env = _envelope(peer, "memory.write",
                    {"namespace": ns, "key": key, "value": {"body": MARKER, "from": "a"}})
    handlers.dispatch("memory.write", env, peer["peer_id"], app_conn)
    with owner_conn.cursor() as cur:
        cur.execute("SELECT id, created_by_owner_id FROM shared_memory "
                    "WHERE namespace = %s AND key = %s", (ns, key))
        memory_id, owner_id = cur.fetchone()
    owner_conn.commit()
    return peer, ns, key, memory_id, owner_id


def _record(cur, memory_id, ns, key, peer, owner_id):
    cur.execute(
        "INSERT INTO shared_memory_redaction (memory_id, namespace, key, version, "
        "redacted_by_peer_id, redacted_by_owner_id, acted_as, reason_code, request_signed_bytes) "
        "VALUES (%s, %s, %s, 1, %s, %s, 'author', 'sent_in_error', %s)",
        (memory_id, ns, key, peer["peer_id"], owner_id, b"request"))


def _tombstone(cur, memory_id, value=TOMBSTONE, extra=""):
    cur.execute(
        "UPDATE shared_memory SET value = %s::jsonb, signed_bytes = NULL, "
        f"provenance_jsonb = {ENVELOPE}, "
        f"tags = ARRAY['redacted']::text[] {extra} WHERE id = %s",
        (json.dumps(value), memory_id))


def _refused(owner_conn, fn) -> str:
    with pytest.raises(psycopg.Error) as err:
        with owner_conn.cursor() as cur:
            fn(cur)
    owner_conn.rollback()
    return str(err.value)


def _value(owner_conn, memory_id):
    with owner_conn.cursor() as cur:
        cur.execute("SELECT value, signed_bytes, tags, payload_jcs_sha256, provenance_jsonb "
                    "FROM shared_memory "
                    "WHERE id = %s", (memory_id,))
        row = cur.fetchone()
    owner_conn.commit()
    return row


def test_11_a_tombstone_update_without_a_record_is_refused(app_conn, owner_conn):
    peer, ns, key, memory_id, owner_id = _written(app_conn, owner_conn, "rd11")
    before = _value(owner_conn, memory_id)
    why = _refused(owner_conn, lambda cur: _tombstone(cur, memory_id))
    assert "immutable" in why
    assert _value(owner_conn, memory_id) == before and MARKER in json.dumps(before[0])

    # Control: the SAME statement succeeds once the record exists, in one transaction.
    with owner_conn.cursor() as cur:
        _record(cur, memory_id, ns, key, peer, owner_id)
        _tombstone(cur, memory_id)
    owner_conn.commit()
    assert MARKER in json.dumps(before[4]), "control: the envelope held the text before"
    value, signed, tags, sha, prov = _value(owner_conn, memory_id)
    assert value["redacted"] is True and signed is None and tags == ["redacted"]
    assert MARKER not in json.dumps(value) and MARKER not in json.dumps(prov)
    assert prov["signature"] == before[4]["signature"] and prov["payload_redacted"] is True
    assert prov["payload"] == {"call_id": before[4]["payload"]["call_id"]}
    assert sha == before[3], "the original payload hash is kept"


def test_12_a_record_does_not_open_any_other_column(app_conn, owner_conn):
    peer, ns, key, memory_id, owner_id = _written(app_conn, owner_conn, "rd12")

    def with_record(then):
        def run(cur):
            _record(cur, memory_id, ns, key, peer, owner_id)
            then(cur)
        return run

    for extra in (", created_at = now() - interval '1 day'", ", author_label = 'someone-else'",
                  ", payload_jcs_sha256 = 'x'", ", status = 'CONFIRMED'"):
        why = _refused(owner_conn, with_record(lambda cur, e=extra: _tombstone(cur, memory_id, extra=e)))
        assert "immutable" in why, extra

    # The tombstone SHAPE is part of the rule: a value that still carries text is refused.
    for bad in ({"redacted": True, "body": MARKER}, {"redacted": True, "subject": MARKER},
                {"redacted": False}, {"body": ""}):
        why = _refused(owner_conn, with_record(lambda cur, b=bad: _tombstone(cur, memory_id, value=b)))
        assert "immutable" in why, bad

    # Keeping the signed bytes, or the payload in the envelope, keeps the text: refused.
    def keeps_bytes(cur):
        cur.execute("UPDATE shared_memory SET value = %s::jsonb, tags = ARRAY['redacted']::text[], "
                    f"provenance_jsonb = {ENVELOPE} WHERE id = %s",
                    (json.dumps(TOMBSTONE), memory_id))
    assert "immutable" in _refused(owner_conn, with_record(keeps_bytes))

    def keeps_envelope(cur):
        cur.execute("UPDATE shared_memory SET value = %s::jsonb, signed_bytes = NULL, "
                    "tags = ARRAY['redacted']::text[] WHERE id = %s",
                    (json.dumps(TOMBSTONE), memory_id))
    assert "immutable" in _refused(owner_conn, with_record(keeps_envelope))

    def drops_the_signature(cur):
        cur.execute("UPDATE shared_memory SET value = %s::jsonb, signed_bytes = NULL, "
                    "tags = ARRAY['redacted']::text[], "
                    f"provenance_jsonb = {NO_SIGNATURE} WHERE id = %s",
                    (json.dumps(TOMBSTONE), memory_id))
    assert "immutable" in _refused(owner_conn, with_record(drops_the_signature))
    assert MARKER in json.dumps(_value(owner_conn, memory_id)[0]), "nothing above changed the row"


def test_13_a_record_is_never_changed_or_removed(app_conn, owner_conn):
    peer, ns, key, memory_id, owner_id = _written(app_conn, owner_conn, "rd13")
    with owner_conn.cursor() as cur:
        _record(cur, memory_id, ns, key, peer, owner_id)
        _tombstone(cur, memory_id)
    owner_conn.commit()

    why = _refused(owner_conn, lambda cur: cur.execute(
        "UPDATE shared_memory_redaction SET reason_code = 'credential' WHERE memory_id = %s",
        (memory_id,)))
    assert "E_REDACTION_APPEND_ONLY" in why
    why = _refused(owner_conn, lambda cur: cur.execute(
        "DELETE FROM shared_memory_redaction WHERE memory_id = %s", (memory_id,)))
    assert "E_REDACTION_APPEND_ONLY" in why

    # One record per row: a second insert is refused by the unique constraint.
    why = _refused(owner_conn, lambda cur: _record(cur, memory_id, ns, key, peer, owner_id))
    assert "shared_memory_redaction_memory_id_key" in why

    with owner_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM shared_memory_redaction WHERE memory_id = %s", (memory_id,))
        assert cur.fetchone()[0] == 1
    owner_conn.commit()


def test_the_record_goes_with_its_row(app_conn, owner_conn):
    """A delete of the ROW (a prune) takes its record along; the append-only trigger must not
    block the cascade."""
    peer, ns, key, memory_id, owner_id = _written(app_conn, owner_conn, "rdcas")
    with owner_conn.cursor() as cur:
        _record(cur, memory_id, ns, key, peer, owner_id)
        _tombstone(cur, memory_id)
    owner_conn.commit()
    with owner_conn.cursor() as cur:
        cur.execute("DELETE FROM shared_memory WHERE id = %s", (memory_id,))
        cur.execute("SELECT count(*) FROM shared_memory_redaction WHERE memory_id = %s", (memory_id,))
        assert cur.fetchone()[0] == 0
    owner_conn.commit()


def test_head_retirement_and_an_ordinary_update_are_as_before(app_conn, owner_conn):
    """What 014 allowed is still allowed, and what it refused without a record is still
    refused: the exemption added nothing else."""
    peer, ns, key, memory_id, owner_id = _written(app_conn, owner_conn, "rdold")
    why = _refused(owner_conn, lambda cur: cur.execute(
        "UPDATE shared_memory SET value = '{\"body\": \"changed\"}'::jsonb WHERE id = %s", (memory_id,)))
    assert "immutable" in why
    with owner_conn.cursor() as cur:
        cur.execute("UPDATE shared_memory SET is_head = FALSE WHERE id = %s", (memory_id,))
    owner_conn.commit()
    why = _refused(owner_conn, lambda cur: cur.execute(
        "UPDATE shared_memory SET is_head = TRUE WHERE id = %s", (memory_id,)))
    assert "resurrected" in why


def test_a_redaction_draws_its_own_commit_order_position(app_conn, owner_conn):
    """Design 5a. The stamp is keyed on the RECORD's namespace, mail only, and is drawn when the
    transaction commits. Since 031 a record is held to its row, so the record that draws a position is for a
    real MAIL row (the first version of this test named the mail for a row of another namespace, which drew a
    position for a non-mail row and leaked into test_mesh_projection's D19). The mail row has its own 'write'
    position, so only kind 'redaction' is read; the control is a record in another namespace, which draws none."""
    author = mesh._peer(owner_conn, "rdseq")
    mesh._admit(owner_conn, mesh.MESH, author)
    # These helpers normally use an autocommit fixture; ours is transactional.
    # Publish the peer, signing key and grant before the app connection uses them.
    owner_conn.commit()
    key = mesh._key(author["name"])
    mesh._write(app_conn, author, mesh._msg(mesh.MESH, key, mesh._value(author["name"], "a message")))
    head = mesh._head(owner_conn, mesh.MESH, key)
    other = _written(app_conn, owner_conn, "rdseq2")
    with owner_conn.cursor() as cur:
        cur.execute("SELECT coalesce(max(seq), 0) FROM mesh_commit_order")
        top = cur.fetchone()[0]
        _record(cur, head["id"], mesh.MESH, key, author, author["owner_id"])
        _record(cur, other[3], other[1], other[2], other[0], other[4])
    owner_conn.commit()
    with owner_conn.cursor() as cur:
        cur.execute("SELECT memory_id, kind, seq > %s FROM mesh_commit_order "
                    "WHERE memory_id = ANY(%s) AND kind = 'redaction'", (top, [head["id"], other[3]]))
        assert cur.fetchall() == [(head["id"], "redaction", True)]
    owner_conn.commit()


# ----------------------------------------------------------------------------------------------------------------------
# The exemption is ONE rewrite of THIS row by its OWN owner (memory-bxxxxx's review of #32, blocking 2). The first form asked
# only that a record exist and four field names be absent, so after one redaction it admitted any later rewrite of the
# tombstone: a re-addressed `to`, a new `from`, a new content field. Every case below is one variable from a control.

def _bound_record(cur, memory_id, ns, key, peer, owner_id, *, owner=None, by_peer=None, reason="sent_in_error", blanked=False):
    cur.execute(
        "INSERT INTO shared_memory_redaction (memory_id, namespace, key, version, redacted_by_peer_id, redacted_by_owner_id, "
        "acted_as, reason_code, thread_id_blanked, request_signed_bytes) VALUES (%s, %s, %s, 1, %s, %s, 'author', %s, %s, %s) "
        "RETURNING id", (memory_id, ns, key, (by_peer or peer)["peer_id"], owner or owner_id, reason, blanked, b"request"))
    return cur.fetchone()[0]


def _written_with(app_conn, owner_conn, tag, value):
    """`_written`, for a value of the test's own."""
    peer = _peer(owner_conn, tag)
    ns = _ns(tag)
    _claim(app_conn, peer, ns)
    key = f"k-{uuid.uuid4().hex[:8]}"
    env = _envelope(peer, "memory.write", {"namespace": ns, "key": key, "value": value})
    handlers.dispatch("memory.write", env, peer["peer_id"], app_conn)
    with owner_conn.cursor() as cur:
        cur.execute("SELECT id, created_by_owner_id FROM shared_memory WHERE namespace = %s AND key = %s", (ns, key))
        memory_id, owner_id = cur.fetchone()
    owner_conn.commit()
    return peer, ns, key, memory_id, owner_id


def test_a_redacted_row_cannot_be_rewritten_again_through_the_exemption(app_conn, owner_conn):
    """memory-bxxxxx's probe. CONTROLS: before any record the same kind of UPDATE is refused, and the handler's own redaction
    passes. After it, a second rewrite of the tombstone that re-addresses it, changes its sender, adds routing or adds a content
    field is refused each time, and the tombstone is what the handler left."""
    from tests.test_redaction import _redact
    peer, ns, key, memory_id, owner_id = _written(app_conn, owner_conn, "rdz2")
    why = _refused(owner_conn, lambda cur: cur.execute(
        "UPDATE shared_memory SET value = value || %s::jsonb WHERE id = %s", (json.dumps({"to": ["x"]}), memory_id)))
    assert "immutable" in why
    _redact(app_conn, peer, ns, key)
    tomb = _value(owner_conn, memory_id)[0]
    assert tomb["redacted"] is True, "control: the handler's own redaction passed the tightened rule"
    # `redacted_at` is the one field a tombstone can still change WITHIN the shape (no other condition refuses it), so it is the patch
    # that isolates "OLD is not already a tombstone": mutation S1 survived a list without it.
    for patch in ({"to": ["someone-else"]}, {"body_md": f"new text {MARKER}"}, {"from": "another-seat"},
                  {"thread_id": "t/other"}, {"cc": ["a-cc-seat"]}, {"title": "a title"},
                  {"redacted_at": "2031-01-01T00:00:00Z"}):
        why = _refused(owner_conn, lambda cur, p=patch: cur.execute(
            "UPDATE shared_memory SET value = value || %s::jsonb WHERE id = %s", (json.dumps(p), memory_id)))
        assert "immutable" in why, patch
    assert _value(owner_conn, memory_id)[0] == tomb


def test_the_tombstone_keys_are_a_closed_set_and_the_routing_is_the_olds(app_conn, owner_conn):
    peer, ns, key, memory_id, owner_id = _written(app_conn, owner_conn, "rdkeys")

    def attempt(value):
        def run(cur):
            _bound_record(cur, memory_id, ns, key, peer, owner_id)
            _tombstone(cur, memory_id, value=value)
        return run

    base = {"redacted": True, "reason_code": "sent_in_error", "from": "a"}
    for bad in (dict(base, body_md=MARKER), dict(base, title="t"), dict(base, surprise={"nested": 1}),
                dict(base, **{"from": "someone-else"}),                    # routing CHANGED
                dict(base, to=["x"]),                                        # routing ADDED
                dict(base, thread_id="t/x")):
        assert "immutable" in _refused(owner_conn, attempt(bad)), bad
    assert MARKER in json.dumps(_value(owner_conn, memory_id)[0]), "nothing above changed the row"
    with owner_conn.cursor() as cur:                                         # CONTROL: the handler's own shape
        _bound_record(cur, memory_id, ns, key, peer, owner_id)
        _tombstone(cur, memory_id)
    owner_conn.commit()
    assert _value(owner_conn, memory_id)[0]["redacted"] is True


def test_the_record_must_bind_the_row_and_the_reason(app_conn, owner_conn):
    """The record's owner is the row's, the redacting peer's owner (read from peers, not the record's own column) is that owner, and
    its reason code is the value's. Each alone is refused; the control is the record that binds."""
    peer, ns, key, memory_id, owner_id = _written(app_conn, owner_conn, "rdbind")
    other = _written(app_conn, owner_conn, "rdbind2")                                          # a peer of ANOTHER owner

    def attempt(unbound_allowed=False, **record):
        def run(cur):
            if unbound_allowed:
                # 031's trigger refuses these two records at the INSERT, before the tombstone is tried, with another code. Taking it away for
                # THIS transaction (DDL is transactional, `_refused` rolls it back) leaves 027's own condition as the one under test.
                cur.execute("ALTER TABLE shared_memory_redaction DISABLE TRIGGER shared_memory_redaction_bound_trg")
            _bound_record(cur, memory_id, ns, key, peer, owner_id, **record)
            _tombstone(cur, memory_id)
        return run

    assert "immutable" in _refused(owner_conn, attempt(True, owner=uuid.uuid4()))              # the record names another owner
    assert "immutable" in _refused(owner_conn, attempt(True, by_peer=other[0]))                # the peer is another owner's, the column says ours
    # A CONSISTENT record of ANOTHER owner: that peer, and its own owner in the column. Only "the record's owner is the row's" refuses
    # it (mutation S3 survived the cases above, each of which the peers-based condition refused on its own).
    assert "immutable" in _refused(owner_conn, attempt(by_peer=other[0], owner=other[4]))
    assert "immutable" in _refused(owner_conn, attempt(reason="credential"))                   # the value says sent_in_error
    with owner_conn.cursor() as cur:
        _bound_record(cur, memory_id, ns, key, peer, owner_id)
        _tombstone(cur, memory_id)
    owner_conn.commit()
    assert _value(owner_conn, memory_id)[0]["redacted"] is True


def test_the_blanked_thread_id_is_admitted_only_when_the_record_says_it_was_blanked(app_conn, owner_conn):
    peer, ns, key, memory_id, owner_id = _written_with(
        app_conn, owner_conn, "rdthr", {"body": MARKER, "from": "a", "thread_id": "t/the-thread"})

    def attempt(blanked, thread):
        def run(cur):
            rid = _bound_record(cur, memory_id, ns, key, peer, owner_id, blanked=blanked)
            _tombstone(cur, memory_id, value={"redacted": True, "reason_code": "sent_in_error", "from": "a",
                                              "thread_id": thread.replace("<id>", str(rid))})
        return run

    assert "immutable" in _refused(owner_conn, attempt(False, "redacted/<id>"))                # not flagged: the old id or nothing
    assert "immutable" in _refused(owner_conn, attempt(True, "t/some-other-thread"))           # flagged, but not the record's form
    with owner_conn.cursor() as cur:                                                            # CONTROL: flagged and the record's form
        rid = _bound_record(cur, memory_id, ns, key, peer, owner_id, blanked=True)
        _tombstone(cur, memory_id, value={"redacted": True, "reason_code": "sent_in_error", "from": "a",
                                          "thread_id": f"redacted/{rid}"})
    owner_conn.commit()
    assert _value(owner_conn, memory_id)[0]["thread_id"].startswith("redacted/")


def test_the_triggers_tombstone_keys_are_the_handlers_routing_fields_and_the_three_it_adds(owner_conn):
    """ONE list in the database, and the handler's list must be exactly it: a routing field added to redact.ROUTING_FIELDS and not
    to shared_memory_tombstone_keys() would make the trigger refuse every tombstone that kept it."""
    from apps.memoryskills import redact
    keys = owner_conn.execute("SELECT shared_memory_tombstone_keys()").fetchone()[0]
    owner_conn.commit()
    assert len(keys) == len(set(keys)), keys
    assert not set(redact.ROUTING_FIELDS) & set(redact.CARD_FIELDS)
    assert set(keys) == ({"redacted", "reason_code", "redacted_at"} | set(redact.ROUTING_FIELDS)
                         | set(redact.CARD_FIELDS)), keys
