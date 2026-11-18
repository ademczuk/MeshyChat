"""memory.redact (divergence 053, apps/memoryskills/redact.py).

Design: docs/design/redaction.md section 6. The numbers in the test names are the design's.
Every positive test is paired with what must now be ABSENT, and every refusal with a control
showing the same caller can do the neighbouring thing, so a refusal is about the rule.

Run:  .venv/Scripts/python.exe -m pytest tests/test_redaction.py -q -p no:cacheprovider
"""
from __future__ import annotations

import base64
import json
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from apps.memoryskills.errors import HandlerError  # noqa: E402

from tests.test_memory_skills import (  # noqa: E402,F401
    _call, _claim, _ns, _peer, app_conn, owner_conn,
)

MARKER = "zq-redaction-marker-9c41"
VALUE = {"schema_version": 1, "from": "a-seat", "to": ["b-seat"], "cc": ["c-seat"],
         "type": "status", "thread_id": "t/plain", "in_reply_to": "msg/0-x-000000",
         "body": f"text {MARKER} text", "subject": f"s {MARKER}", "ref": f"r {MARKER}",
         "session_label": f"l {MARKER}", "surprise": {"nested": MARKER}}


def _world(app_conn, owner_conn, tag):
    group = uuid.uuid4()
    author = _peer(owner_conn, tag + "a", trust_group=group)
    sibling = _peer(owner_conn, tag + "s", owner_id=author["owner_id"])
    stranger = _peer(owner_conn, tag + "x", trust_group=group)
    ns = _ns(tag)
    _claim(app_conn, author, ns)
    for p in (sibling, stranger):
        _call(app_conn, author, "memory.grant",
              {"namespace": ns, "grantee_peer_id": str(p["peer_id"]), "permission": "write"})
    return author, sibling, stranger, ns


def _write(app_conn, peer, ns, key=None, value=VALUE):
    key = key or f"k-{uuid.uuid4().hex[:8]}"
    _call(app_conn, peer, "memory.write", {"namespace": ns, "key": key, "value": value})
    return key


def _redact(app_conn, peer, ns, key, **more):
    return _call(app_conn, peer, "memory.redact",
                 {"namespace": ns, "key": key, "reason_code": "sent_in_error", **more})


def census(owner_conn, marker=MARKER) -> list[str]:
    """Test 6: every text, jsonb, bytea and text[] column of every table in the schema, the
    list taken from information_schema and never typed, searched for the marker plain and
    base64-encoded. Returns `table.column` for each column holding it."""
    needles = [marker, base64.b64encode(marker.encode()).decode().rstrip("="),
               base64.urlsafe_b64encode(marker.encode()).decode().rstrip("=")]
    hits = []
    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT c.table_name, c.column_name, c.data_type FROM information_schema.columns c "
            "JOIN information_schema.tables t USING (table_schema, table_name) "
            "WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE' "
            "  AND c.data_type IN ('text', 'jsonb', 'json', 'bytea', 'character varying', 'ARRAY') "
            "ORDER BY 1, 2")
        columns = cur.fetchall()
        assert len(columns) > 100, "the census must see the whole schema"
        for table, column, data_type in columns:
            test = (f"position(convert_to(%s, 'UTF8') in \"{column}\") > 0"
                    if data_type == "bytea" else f"position(%s in \"{column}\"::text) > 0")
            cur.execute(f"SELECT 1 FROM \"{table}\" WHERE "
                        + " OR ".join(test for _ in needles) + " LIMIT 1", needles)
            if cur.fetchone():
                hits.append(f"{table}.{column}")
    owner_conn.commit()
    return hits


def _records(owner_conn, ns, key) -> int:
    with owner_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM shared_memory_redaction WHERE namespace = %s AND key = %s",
                    (ns, key))
        n = cur.fetchone()[0]
    owner_conn.commit()
    return n


def test_1_4_the_author_redacts_and_a_reader_sees_the_record_without_the_text(app_conn, owner_conn):
    author, sibling, stranger, ns = _world(app_conn, owner_conn, "rd1")
    key = _write(app_conn, author, ns)
    before = _call(app_conn, stranger, "memory.read", {"namespace": ns, "key": key})["memo"]
    assert MARKER in json.dumps(before), "control: the text was readable"

    out = _redact(app_conn, author, ns, key)
    assert [r["version"] for r in out["redacted"]] == [1]
    assert out["redacted"][0]["acted_as"] == "author"
    assert out["already_redacted"] == [] and out["not_redacted_other_owner"] == []

    memo = _call(app_conn, stranger, "memory.read", {"namespace": ns, "key": key})["memo"]
    assert MARKER not in json.dumps(memo)
    value = memo["value"]
    assert value["redacted"] is True and value["reason_code"] == "sent_in_error"
    for kept in ("from", "to", "cc", "thread_id", "in_reply_to", "type", "schema_version"):
        assert value[kept] == VALUE[kept], kept
    for gone in ("body", "subject", "ref", "session_label", "surprise"):
        assert gone not in value, gone
    assert memo["tags"] == ["redacted"]
    assert memo["key"] == key and memo["created_at"] == before["created_at"]

    listed = _call(app_conn, stranger, "memory.query", {"namespace": ns})
    assert MARKER not in json.dumps(listed)


def test_2_a_seat_of_another_owner_is_refused(app_conn, owner_conn):
    author, sibling, stranger, ns = _world(app_conn, owner_conn, "rd2")
    key = _write(app_conn, author, ns)
    with pytest.raises(HandlerError) as refused:
        _redact(app_conn, stranger, ns, key)
    assert refused.value.code == "E_PERMISSION_DENIED"
    assert _records(owner_conn, ns, key) == 0
    memo = _call(app_conn, author, "memory.read", {"namespace": ns, "key": key})["memo"]
    assert MARKER in json.dumps(memo), "the row is unchanged"
    # Control: the refused seat holds a write grant and can write, so the refusal is the
    # owner rule and not a missing permission.
    _write(app_conn, stranger, ns)


def test_the_same_owners_other_seat_redacts_as_owner(app_conn, owner_conn):
    author, sibling, stranger, ns = _world(app_conn, owner_conn, "rdown")
    key = _write(app_conn, author, ns)
    out = _redact(app_conn, sibling, ns, key)
    assert out["redacted"][0]["acted_as"] == "owner"
    assert out["redacted"][0]["redacted_by_peer_id"] == str(sibling["peer_id"])
    assert MARKER not in json.dumps(
        _call(app_conn, author, "memory.read", {"namespace": ns, "key": key}))


def test_3_an_author_whose_write_grant_is_gone_is_refused(app_conn, owner_conn):
    author, sibling, stranger, ns = _world(app_conn, owner_conn, "rd3")
    key = _write(app_conn, sibling, ns)
    _call(app_conn, author, "memory.grant",
          {"namespace": ns, "grantee_peer_id": str(sibling["peer_id"]), "revoke": True})
    with pytest.raises(HandlerError) as refused:
        _redact(app_conn, sibling, ns, key)
    assert refused.value.code == "E_PERMISSION_DENIED"
    assert _records(owner_conn, ns, key) == 0


def test_6_the_census_finds_the_text_before_and_nowhere_after(app_conn, owner_conn):
    marker = f"zq-census-{uuid.uuid4().hex}"
    author, sibling, stranger, ns = _world(app_conn, owner_conn, "rd6")
    key = _write(app_conn, author, ns, value=dict(VALUE, body=marker, subject=marker,
                                                   ref="r", session_label="l", surprise=marker))
    found = census(owner_conn, marker)
    for must in ("shared_memory.value", "shared_memory.signed_bytes",
                 "shared_memory.provenance_jsonb"):
        assert must in found, f"control: the census must find the text in {must}, found {found}"
    _redact(app_conn, author, ns, key)
    assert census(owner_conn, marker) == []


def test_8_a_repeat_changes_nothing_and_answers_the_first_record(app_conn, owner_conn):
    author, sibling, stranger, ns = _world(app_conn, owner_conn, "rd8")
    key = _write(app_conn, author, ns)
    first = _redact(app_conn, author, ns, key)["redacted"][0]
    again = _redact(app_conn, sibling, ns, key)
    assert again["redacted"] == [] and again["already_redacted"] == [first]
    assert _records(owner_conn, ns, key) == 1


def test_9_all_versions_and_the_versions_of_another_owner(app_conn, owner_conn):
    author, sibling, stranger, ns = _world(app_conn, owner_conn, "rd9")
    key = _write(app_conn, author, ns)
    _write(app_conn, sibling, ns, key=key, value=dict(VALUE, body=f"two {MARKER}"))
    _write(app_conn, stranger, ns, key=key, value=dict(VALUE, body=f"three {MARKER}"))
    out = _redact(app_conn, author, ns, key, version="all")
    assert [r["version"] for r in out["redacted"]] == [1, 2]
    assert [r["acted_as"] for r in out["redacted"]] == ["author", "owner"]
    assert [v["version"] for v in out["not_redacted_other_owner"]] == [3]
    # The text another owner wrote is still there, and the result said so.
    head = _call(app_conn, author, "memory.read", {"namespace": ns, "key": key})["memo"]
    assert head["version"] == 3 and MARKER in json.dumps(head)

    # One version by number.
    key2 = _write(app_conn, author, ns)
    _write(app_conn, author, ns, key=key2, value=dict(VALUE, body="second, harmless"))
    out2 = _redact(app_conn, author, ns, key2, version=1)
    assert [r["version"] for r in out2["redacted"]] == [1]
    head2 = _call(app_conn, author, "memory.read", {"namespace": ns, "key": key2})["memo"]
    assert head2["value"]["body"] == "second, harmless"


def test_15_a_write_after_a_redaction_is_an_ordinary_new_version(app_conn, owner_conn):
    author, sibling, stranger, ns = _world(app_conn, owner_conn, "rd15")
    key = _write(app_conn, author, ns)
    _redact(app_conn, author, ns, key)
    _write(app_conn, author, ns, key=key, value=dict(VALUE, body="corrected", subject="s",
                                                     ref="r", session_label="l", surprise=1))
    head = _call(app_conn, author, "memory.read", {"namespace": ns, "key": key})["memo"]
    assert head["version"] == 2 and head["value"]["body"] == "corrected"
    assert MARKER not in json.dumps(head)


def test_21_a_thread_id_that_carries_the_text_can_be_blanked(app_conn, owner_conn):
    author, sibling, stranger, ns = _world(app_conn, owner_conn, "rd21")
    key = _write(app_conn, author, ns, value=dict(VALUE, thread_id=f"t/{MARKER}"))
    kept = _write(app_conn, author, ns, value=dict(VALUE, thread_id=f"t/{MARKER}"))
    out = _redact(app_conn, author, ns, key, blank_thread_id=True)["redacted"][0]
    memo = _call(app_conn, author, "memory.read", {"namespace": ns, "key": key})["memo"]
    assert memo["value"]["thread_id"] == f"redacted/{out['redaction_id']}"
    assert out["thread_id_blanked"] is True and MARKER not in json.dumps(memo)
    # Control: without the flag the thread id stays, text and all.
    _redact(app_conn, author, ns, kept)
    assert _call(app_conn, author, "memory.read",
                 {"namespace": ns, "key": kept})["memo"]["value"]["thread_id"] == f"t/{MARKER}"


@pytest.mark.parametrize("bad", [
    {"reason_code": "because"}, {"reason_code": ""}, {"version": 0}, {"version": "1"},
    {"version": True}, {"blank_thread_id": "yes"},
])
def test_bad_arguments_are_refused_and_change_nothing(app_conn, owner_conn, bad):
    author, sibling, stranger, ns = _world(app_conn, owner_conn, "rdbad")
    key = _write(app_conn, author, ns)
    with pytest.raises(HandlerError) as refused:
        _call(app_conn, author, "memory.redact",
              {"namespace": ns, "key": key, "reason_code": "sent_in_error", **bad})
    assert refused.value.code in ("E_INVALID_ARGS", "E_BAD_REQUEST")
    assert _records(owner_conn, ns, key) == 0


def test_an_unknown_key_is_refused(app_conn, owner_conn):
    author, sibling, stranger, ns = _world(app_conn, owner_conn, "rdunk")
    with pytest.raises(HandlerError) as refused:
        _redact(app_conn, author, ns, "no-such-key")
    assert refused.value.code == "E_UNKNOWN_KEY"


# ------------------------------------------------------------------ test 20: stored calls

def _stored_call(owner_conn, peer, skill, call_id, *, args, result=None, payload=None,
                 status="completed"):
    """One `skill_calls` row as the invoke route stores it: the signed payload in `args`, the
    answer in `result` and `result_payload`. The handler tests above dispatch in-process, where
    no call row exists, so these are seeded."""
    from psycopg.types.json import Jsonb
    with owner_conn.cursor() as cur:
        skill_id = uuid.uuid4()
        cur.execute("INSERT INTO skills (id, peer_id, name, version, mode, share_mode, "
                    "input_schema) VALUES (%s,%s,%s,'1','sync','private','{}')",
                    (skill_id, peer["peer_id"], f"rd.{skill_id.hex[:8]}"))
        cur.execute(
            "INSERT INTO skill_calls (call_id, caller_peer_id, caller_owner_id, target_peer_id, "
            " target_owner_id, skill_id, skill_name, skill_version, mode, transport, status, "
            " args, args_signature, args_signing_kid, deadline_at, result, result_payload, "
            " result_signature, result_signing_kid, completed_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,'1','sync','http',%s,%s,'sig','kid', "
            " now() + interval '1 hour', %s, %s, 'rsig', 'rkid', now())",
            (call_id, peer["peer_id"], peer["owner_id"], peer["peer_id"], peer["owner_id"],
             skill_id, skill, status, Jsonb(args),
             Jsonb(result) if result is not None else None,
             Jsonb(payload) if payload is not None else None))
    owner_conn.commit()


def _call_row(owner_conn, call_id) -> dict:
    from psycopg.rows import dict_row
    with owner_conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT args, result, result_payload, args_signature, result_signature "
                    "FROM skill_calls WHERE call_id = %s", (call_id,))
        row = cur.fetchone()
    owner_conn.commit()
    return row


def test_20_the_stored_write_call_and_unpruned_reads_lose_the_text(app_conn, owner_conn):
    marker = f"zq-calls-{uuid.uuid4().hex}"
    author, sibling, stranger, ns = _world(app_conn, owner_conn, "rd20")
    key, other_key = f"k-{uuid.uuid4().hex[:8]}", f"k-{uuid.uuid4().hex[:8]}"
    value = dict(VALUE, body=marker, subject="s", ref="r", session_label="l", surprise=1)
    write_id = f"call-{uuid.uuid4()}"
    _call(app_conn, author, "memory.write", {"namespace": ns, "key": key, "value": value},
          call_id=write_id)
    _write(app_conn, author, ns, key=other_key, value=value)

    _stored_call(owner_conn, author, "memory.write", write_id,
                 args={"v": 1, "kind": "skill.call", "call_id": write_id,
                       "args": {"namespace": ns, "key": key, "value": value, "tags": [marker]}},
                 result={"op": "memory.write", "key": key, "tags": [marker], "version": 1},
                 payload={"call_id": write_id,
                          "result": {"op": "memory.write", "key": key, "tags": [marker]}})
    read_id, other_read = f"call-{uuid.uuid4()}", f"call-{uuid.uuid4()}"
    _stored_call(owner_conn, stranger, "memory.read", read_id,
                 args={"args": {"namespace": ns, "key": key}},
                 result={"op": "memory.read", "memo": {"key": key, "value": value}},
                 payload={"result": {"memo": {"key": key, "value": value}}})
    _stored_call(owner_conn, stranger, "memory.read", other_read,
                 args={"args": {"namespace": ns, "key": other_key}},
                 result={"op": "memory.read", "memo": {"key": other_key, "value": value}})

    before = census(owner_conn, marker)
    for must in ("skill_calls.args", "skill_calls.result", "skill_calls.result_payload"):
        assert must in before, f"control: {must} held the text, found {before}"

    out = _redact(app_conn, author, ns, key)
    assert out["stored_calls_blanked"] == {"write_calls": 1, "read_results": 1}

    write = _call_row(owner_conn, write_id)
    assert write["args"]["args"] == {"namespace": ns, "key": key, "redacted": True}
    assert write["args"]["call_id"] == write_id and write["args_signature"] == "sig"
    assert write["result"]["tags"] == ["redacted"] and write["result"]["version"] == 1
    assert write["result_payload"]["result"]["tags"] == ["redacted"]

    read = _call_row(owner_conn, read_id)
    assert read["result"]["pruned"] is True and read["result"]["pruned_reason"] == "redaction"
    assert read["result"]["original_result_signature"] == "rsig"
    assert read["result_payload"] is None and read["result_signature"] is None

    # The read of ANOTHER key is untouched: the blanking follows the key, not the text.
    other = _call_row(owner_conn, other_read)
    assert other["result"]["memo"]["value"]["body"] == marker
    # So the census still finds the marker, in exactly the places the other key holds it.
    after = census(owner_conn, marker)
    assert "skill_calls.args" not in after and "skill_calls.result_payload" not in after
    assert "skill_calls.result" in after and "shared_memory.value" in after

    _redact(app_conn, author, ns, other_key)
    assert census(owner_conn, marker) == []


def test_20b_a_stored_list_result_that_previews_the_row_loses_the_text(app_conn, owner_conn):
    """memory.list answers with a preview of each row, so it is one of the reads (sql_meshychat/
    028). The control is a list result that names only another key."""
    marker = f"zq-list-{uuid.uuid4().hex}"
    author, sibling, stranger, ns = _world(app_conn, owner_conn, "rd20b")
    key = _write(app_conn, author, ns, value=dict(VALUE, body=marker, subject="s", ref="r",
                                                  session_label="l", surprise=1))
    listed, other = f"call-{uuid.uuid4()}", f"call-{uuid.uuid4()}"
    _stored_call(owner_conn, stranger, "memory.list", listed, args={"args": {"namespace": ns}},
                 result={"op": "memory.list", "results": [{"key": key, "preview": marker}]})
    _stored_call(owner_conn, stranger, "memory.list", other, args={"args": {"namespace": ns}},
                 result={"op": "memory.list", "results": [{"key": "k-other", "preview": "p"}]})
    assert "skill_calls.result" in census(owner_conn, marker)

    out = _redact(app_conn, author, ns, key)
    assert out["stored_calls_blanked"]["read_results"] == 1
    assert _call_row(owner_conn, listed)["result"]["pruned_reason"] == "redaction"
    assert _call_row(owner_conn, other)["result"]["results"][0]["key"] == "k-other"
    assert census(owner_conn, marker) == []


def test_the_hourly_prune_replaces_a_list_result_as_it_does_a_read(owner_conn, app_conn):
    from apps.calls import retention
    marker = f"zq-listprune-{uuid.uuid4().hex}"
    author, sibling, stranger, ns = _world(app_conn, owner_conn, "rdlp")
    old, fresh = f"call-{uuid.uuid4()}", f"call-{uuid.uuid4()}"
    for call_id in (old, fresh):
        _stored_call(owner_conn, stranger, "memory.list", call_id,
                     args={"args": {"namespace": ns}},
                     result={"op": "memory.list", "results": [{"key": "k", "preview": marker}]})
    with owner_conn.cursor() as cur:
        cur.execute("UPDATE skill_calls SET completed_at = now() - interval '2 hours' "
                    "WHERE call_id = %s", (old,))
    owner_conn.commit()
    assert "memory.list" in retention.READ_SKILLS
    retention.prune_read_result_payloads(app_conn, older_than_secs=3600, limit=100)
    assert _call_row(owner_conn, old)["result"]["pruned_reason"] == "read_result_retention"
    # Control: the one that ended a moment ago is inside its hour and keeps its result.
    assert _call_row(owner_conn, fresh)["result"]["results"][0]["preview"] == marker


def test_20c_a_stored_read_of_a_key_with_a_non_ascii_character_is_found(app_conn, owner_conn):
    """Review of #32, point 5. jsonb prints a non-ASCII character as itself while json.dumps escapes it as \\uXXXX, so a search built
    the second way never matched a key like this one. CONTROLS: the stored read holds the text before (the census, a raw search, finds
    it), and a read of another key that shares the ASCII part of this one is left alone."""
    marker = f"zq-uni-{uuid.uuid4().hex}"
    author, sibling, stranger, ns = _world(app_conn, owner_conn, "rd20c")
    stem = uuid.uuid4().hex[:8]
    key, other = f"k-caf\u00e9-{stem}", f"k-cafe-{stem}"
    value = dict(VALUE, body=marker, subject="s", ref="r", session_label="l", surprise=1)
    _write(app_conn, author, ns, key=key, value=value)
    read_id, other_read = f"call-{uuid.uuid4()}", f"call-{uuid.uuid4()}"
    _stored_call(owner_conn, stranger, "memory.read", read_id, args={"args": {"namespace": ns, "key": key}},
                 result={"op": "memory.read", "memo": {"key": key, "value": {"body": marker}}})
    _stored_call(owner_conn, stranger, "memory.read", other_read, args={"args": {"namespace": ns, "key": other}},
                 result={"op": "memory.read", "memo": {"key": other, "value": {"body": marker}}})
    assert "skill_calls.result" in census(owner_conn, marker), "control: the stored read holds the text"

    out = _redact(app_conn, author, ns, key)
    assert out["stored_calls_blanked"]["read_results"] == 1
    assert _call_row(owner_conn, read_id)["result"]["pruned_reason"] == "redaction"
    assert _call_row(owner_conn, other_read)["result"]["memo"]["key"] == other, "the read of the other key was blanked"
