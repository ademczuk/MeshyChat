"""sql_meshychat/029 (divergence 059): the database rule under memory.edit. Design: docs/design/edit.md section 3 and tests 5, 6 and 6b of
its section 7.

Every case is a raw INSERT as meshychat_app in the server_internal context the handler itself uses: what a code path that skipped
handle_edit would do. Each REFUSED case is one variable apart from a control that passes, so a refusal is about the rule and not the
statement being wrong for another reason. The mark (`app.mesh_edit_owner`) is set by the test where a case needs it, and the cases
that matter most are the ones that set it CORRECTLY and are refused anyway: the mark says which path is inserting and authorizes
nothing, so the owner of the inserting PEER (read from peers, never the row's own owner column), version 1's owner, the previous
version (looked up as version - 1, not as the head) and the frozen fields are what hold.

Run:  .venv313/Scripts/python.exe -m pytest tests/test_edit_db_rule.py -q -p no:cacheprovider
"""
from __future__ import annotations

import re
import sys
import uuid
from pathlib import Path

import psycopg
import psycopg.errors
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from apps.core import rls  # noqa: E402
from apps.memoryskills import handlers  # noqa: E402
from tests.fixtures import signed_rows as sr  # noqa: E402
from tests.test_mesh_authorship import (  # noqa: E402,F401
    MESH, _admit, _envelope, _head, _key, _msg, _peer, _rows, _value, _write, app_conn, owner_conn,
)


def _author_and_sibling(owner_conn, app_conn, tag, body="the original body"):
    """A message written by its author, and a second seat of the SAME owner (admitted to write)."""
    author = _peer(owner_conn, tag + "a")
    _admit(owner_conn, MESH, author)
    sibling = _peer(owner_conn, tag + "s", same_owner_as=author)
    _admit(owner_conn, MESH, sibling)
    key = _key(author["name"])
    value = _value(author["name"], body)
    _write(app_conn, author, _msg(MESH, key, value))
    return author, sibling, key, value


def _direct(conn, head: dict, *, peer: dict, value: dict, mark=None, skill: str = "memory.edit",
            owner_column=None) -> None:
    """Retire `head` and insert the next version as raw SQL on `conn` (meshychat_app, the server_internal context), signed as `peer`
    with a `skill` payload. `mark` is put in app.mesh_edit_owner first; None sets none. `owner_column` is what the new row's own
    created_by_owner_id says (the peer's owner when None). The refusal rolls the head's retirement back with it."""
    p = sr.payload(peer["peer_id"], head["namespace"], head["key"], value, skill_name=skill,
                   extra_args={"expected_head_version": head["version"]})
    cols = sr.columns(p, peer_id=peer["peer_id"], owner_id=owner_column or peer["owner_id"],
                      version=head["version"] + 1, parent_id=head["id"], author_label=peer["fallback"])
    with rls.server_internal(conn), conn.cursor() as cur:
        if mark is not None:
            cur.execute("SELECT set_config('app.mesh_edit_owner', %s, true)", (str(mark),))
        cur.execute("UPDATE shared_memory SET is_head = FALSE WHERE id = %s", (head["id"],))
        sr.insert(conn, cols)


def _unchanged(owner_conn, key, versions=1):
    rows = _rows(owner_conn, MESH, key)
    assert len(rows) == versions and rows[-1][1] is True, f"the refused insert left something behind: {rows}"


def test_5_a_later_version_with_no_mark_is_refused_for_the_author_and_for_a_second_writer(app_conn, owner_conn):
    """011's refusal of a second writer keeps its own words; the AUTHOR's later version, an unguarded edit path until now (nothing
    froze to or cc on it), gets the new refusal that names memory.edit. Both roll the head's retirement back."""
    author, sibling, key, value = _author_and_sibling(owner_conn, app_conn, "d5")
    stranger = _peer(owner_conn, "d5x")
    _admit(owner_conn, MESH, stranger)
    head = _head(owner_conn, MESH, key)

    with pytest.raises(psycopg.errors.CheckViolation, match="E_NOT_MESSAGE_AUTHOR"):
        _direct(app_conn, head, peer=stranger, value=dict(value, body="TAKEN OVER"))
    _unchanged(owner_conn, key)
    with pytest.raises(psycopg.errors.CheckViolation, match="E_NOT_MESSAGE_AUTHOR"):
        _direct(app_conn, head, peer=sibling, value=dict(value, body="a sibling, no mark"))
    _unchanged(owner_conn, key)
    with pytest.raises(psycopg.errors.CheckViolation, match="E_USE_MEMORY_EDIT"):
        _direct(app_conn, head, peer=author, value=dict(value, body="the author's own, no mark"))
    _unchanged(owner_conn, key)

    # Control, one variable apart: the same statements with the edit path's mark, by a sibling of the author's owner.
    _direct(app_conn, head, peer=sibling, value=dict(value, body="an edit"), mark=author["owner_id"])
    assert [(r[0], r[1], r[2]) for r in _rows(owner_conn, MESH, key)] == [
        (1, False, str(author["peer_id"])), (2, True, str(sibling["peer_id"]))]


def test_6_the_mark_alone_authorizes_nothing_each_case_one_variable_from_the_control(app_conn, owner_conn):
    """Every case below sets the mark, most of them CORRECTLY, and is refused. The control (test 5's last statement, and the last one
    here) shows the same insert passes when every condition holds."""
    author, sibling, key, value = _author_and_sibling(owner_conn, app_conn, "d6")
    other = _peer(owner_conn, "d6o")                                    # another owner, admitted to write
    _admit(owner_conn, MESH, other)
    head = _head(owner_conn, MESH, key)
    edited = dict(value, body="an edit")

    # the peer belongs to another owner, and the mark names that owner: version 1's owner is what it must equal
    with pytest.raises(psycopg.errors.CheckViolation, match="E_NOT_MESSAGE_AUTHOR.*may not write"):
        _direct(app_conn, head, peer=other, value=edited, mark=other["owner_id"])
    _unchanged(owner_conn, key)

    # the mark names the RIGHT owner and the row's own owner column names it too, but the inserting PEER is another owner's:
    # a check that trusted the row's column would pass, and the database reads the peer's owner from peers
    with pytest.raises(psycopg.errors.CheckViolation, match="E_NOT_MESSAGE_AUTHOR.*may not write"):
        _direct(app_conn, head, peer=other, value=edited, mark=author["owner_id"], owner_column=author["owner_id"])
    _unchanged(owner_conn, key)

    # the peer IS a sibling of version 1's owner (so its owner is version 1's), but the mark names ANOTHER owner: the mark must be the
    # inserting peer's owner as well, so a caller that marks the transaction with an owner other than its own is refused
    with pytest.raises(psycopg.errors.CheckViolation, match="E_NOT_MESSAGE_AUTHOR.*may not write"):
        _direct(app_conn, head, peer=sibling, value=edited, mark=other["owner_id"])
    _unchanged(owner_conn, key)

    # the peer, its owner and the mark are all right, but the row's own owner column names ANOTHER owner: memory.redact reads that column, so it
    # must agree with the peer's owner (hosted review of #35)
    with pytest.raises(psycopg.errors.CheckViolation, match="E_NOT_MESSAGE_AUTHOR.*may not write"):
        _direct(app_conn, head, peer=sibling, value=edited, mark=author["owner_id"], owner_column=other["owner_id"])
    _unchanged(owner_conn, key)

    # everything right again, but the stored request is a memory.write payload: a later version written as an ordinary write, through the mark
    with pytest.raises(psycopg.errors.CheckViolation, match="E_USE_MEMORY_EDIT.*memory.edit request"):
        _direct(app_conn, head, peer=sibling, value=edited, mark=author["owner_id"], skill="memory.write")
    _unchanged(owner_conn, key)

    # (A peer unknown to `peers` cannot reach the trigger: shared_memory.created_by_peer_id is a foreign key to it. The trigger's
    # NULL-owner refusal is the defence behind that key, and is not reachable by an insert.)

    # right peer, right owner, right mark, but a field other than the body differs: never a send to anyone new
    for field, changed in (("to", ["someone-else"]), ("cc", ["a-cc-seat"]), ("thread_id", "other/thread"),
                           ("type", "question"), ("subject", "a-subject"), ("ts", "2026-09-24T00:00:00Z")):
        with pytest.raises(psycopg.errors.CheckViolation, match="E_INVALID_ARGS.*changes the body only"):
            _direct(app_conn, head, peer=sibling, value=dict(edited, **{field: changed}), mark=author["owner_id"])
        _unchanged(owner_conn, key)
    # and the body must still be a string
    with pytest.raises(psycopg.errors.CheckViolation, match="changes the body only"):
        _direct(app_conn, head, peer=sibling, value=dict(edited, body=5), mark=author["owner_id"])
    _unchanged(owner_conn, key)

    # the mark set OUTSIDE the server-internal context (the owner role bypasses row-level security, so it can try): the insert
    # is then not the edit path at all, so a sibling (a second writer) gets 011's own refusal
    with pytest.raises(psycopg.errors.CheckViolation, match="E_NOT_MESSAGE_AUTHOR.*may not write version 2"):
        with owner_conn.transaction():
            owner_conn.execute("SELECT set_config('app.mesh_edit_owner', %s, true)", (str(author["owner_id"]),))
            owner_conn.execute("UPDATE shared_memory SET is_head = FALSE WHERE id = %s", (head["id"],))
            p = sr.payload(sibling["peer_id"], MESH, key, edited, skill_name="memory.edit",
                           extra_args={"expected_head_version": 1})
            sr.insert(owner_conn, sr.columns(p, peer_id=sibling["peer_id"], owner_id=sibling["owner_id"], version=2,
                                             parent_id=head["id"], author_label=sibling["fallback"]))
    _unchanged(owner_conn, key)

    # CONTROL: every condition met
    _direct(app_conn, head, peer=sibling, value=edited, mark=author["owner_id"])
    assert [(r[0], r[1]) for r in _rows(owner_conn, MESH, key)] == [(1, False), (2, True)]


def test_6b_the_previous_version_is_version_minus_one_not_the_head(app_conn, owner_conn):
    """The handler retires the head BEFORE the insert, so a lookup by is_head finds nothing and a frozen-field comparison against
    NULL passes anything. Version 3 changes `to` against version 2: refused; the same insert changing only the body passes."""
    author, sibling, key, value = _author_and_sibling(owner_conn, app_conn, "d6b")
    head = _head(owner_conn, MESH, key)
    _direct(app_conn, head, peer=sibling, value=dict(value, body="second"), mark=author["owner_id"])
    head = _head(owner_conn, MESH, key)
    assert head["version"] == 2
    with pytest.raises(psycopg.errors.CheckViolation, match="changes the body only"):
        _direct(app_conn, head, peer=author, value=dict(value, body="third", to=["all", "one-more"]), mark=author["owner_id"])
    _unchanged(owner_conn, key, versions=2)
    _direct(app_conn, head, peer=author, value=dict(value, body="third"), mark=author["owner_id"])   # control
    assert [r[0] for r in _rows(owner_conn, MESH, key)] == [1, 2, 3]


def test_7_a_redacted_head_cannot_be_edited_by_any_path(app_conn, owner_conn):
    author, sibling, key, value = _author_and_sibling(owner_conn, app_conn, "d7")
    head = _head(owner_conn, MESH, key)
    handlers.dispatch("memory.redact", _envelope(author, "memory.redact", {
        "namespace": MESH, "key": key, "reason_code": "sent_in_error"}), author["peer_id"], app_conn)
    with pytest.raises(psycopg.errors.CheckViolation, match="E_MESSAGE_REDACTED"):
        _direct(app_conn, head, peer=sibling, value=dict(value, body="over the tombstone"), mark=author["owner_id"])
    assert [r[0] for r in _rows(owner_conn, MESH, key)] == [1]


def test_the_agree_trigger_admits_a_memory_edit_payload_only_for_a_later_mail_version_on_the_edit_path(app_conn, owner_conn):
    """014's skill_name check still refuses every payload that is not memory.write, except this one case. Each condition of the case is
    taken away ALONE, so that each refusal is that condition's: the version (a first version), the namespace (a message-shaped key
    elsewhere), the key (a key of the mail namespace that is not a message), the skill (another skill's payload, every other condition
    met) and the mark (with 011's single-writer rule out of the way for one transaction: it runs first and refuses an unmarked later
    version by itself)."""
    author, sibling, key, value = _author_and_sibling(owner_conn, app_conn, "d8")
    head = _head(owner_conn, MESH, key)
    # version: a memory.edit payload as a FIRST version of a mail key, with the mark: version 1 is not an edit
    fresh = _key(author["name"])
    p = sr.payload(author["peer_id"], MESH, fresh, _value(author["name"]), skill_name="memory.edit",
                   extra_args={"expected_head_version": 1})
    with pytest.raises(psycopg.errors.CheckViolation, match="E_SIGNED_BYTES_DISAGREE.*skill_name"):
        with rls.server_internal(app_conn), app_conn.cursor() as cur:
            cur.execute("SELECT set_config('app.mesh_edit_owner', %s, true)", (str(author["owner_id"]),))
            sr.insert(app_conn, sr.columns(p, peer_id=author["peer_id"], owner_id=author["owner_id"], version=1,
                                           author_label=author["fallback"]))
    # namespace and key: a LATER version signed as a memory.edit call, the mark set, of something that is not a mail message: the wiki's
    # confirmation shape (neither its namespace nor its key is a message's, so either condition alone would refuse it), a message-shaped
    # key of another namespace (only the namespace refuses it) and a key of the mail namespace that is not a message (only the key does)
    wiki_peer = _peer(owner_conn, "d8w")
    _admit(owner_conn, "shared/wiki", wiki_peer)

    def a_later_version_is_refused(peer, namespace, k):
        extra = {"tags": ["test"]}
        with rls.server_internal(app_conn):
            rid = sr.insert(app_conn, sr.row(peer["peer_id"], peer["owner_id"], namespace, k, {"title": "t", "body": "b"}, extra_args=extra))
        p2 = sr.payload(peer["peer_id"], namespace, k, {"title": "t", "body": "c"}, skill_name="memory.edit", extra_args=extra)
        with pytest.raises(psycopg.errors.CheckViolation, match="E_SIGNED_BYTES_DISAGREE.*skill_name"):
            with rls.server_internal(app_conn), app_conn.cursor() as cur:
                cur.execute("SELECT set_config('app.mesh_edit_owner', %s, true)", (str(peer["owner_id"]),))
                cur.execute("UPDATE shared_memory SET is_head = FALSE WHERE id = %s", (rid,))
                sr.insert(app_conn, sr.columns(p2, peer_id=peer["peer_id"], owner_id=peer["owner_id"], version=2, parent_id=rid,
                                               author_label=peer["fallback"], tags=["test"]))

    tail = uuid.uuid4().hex[:8]
    a_later_version_is_refused(wiki_peer, "shared/wiki", f"d8-{tail}")
    a_later_version_is_refused(wiki_peer, "shared/wiki", f"msg/1790000000000-d8-{tail}")
    a_later_version_is_refused(author, MESH, f"d8-not-a-message-{tail}")
    # skill and mark: the edit's own insert as the owner role, in a transaction that is ROLLED BACK whatever happens, with 011's single-writer
    # rule disabled inside it. That rule runs FIRST and on the marked path now refuses anything but a memory.edit request, and an unmarked later
    # version by itself, so either would hide the agree trigger's own skill and mark conditions. CONTROL: with the mark and a memory.edit
    # request, the same construction is admitted.
    def by_the_owner_role_without_the_single_writer_rule(mark, skill="memory.edit"):
        with owner_conn.transaction(force_rollback=True):
            owner_conn.execute("ALTER TABLE shared_memory DISABLE TRIGGER shared_memory_mesh_single_writer_trg")
            if mark is not None:
                owner_conn.execute("SELECT set_config('app.mesh_edit_owner', %s, true)", (str(mark),))
            owner_conn.execute("UPDATE shared_memory SET is_head = FALSE WHERE id = %s", (head["id"],))
            p3 = sr.payload(sibling["peer_id"], MESH, key, dict(value, body="an edit"), skill_name=skill,
                            extra_args={"expected_head_version": 1})
            sr.insert(owner_conn, sr.columns(p3, peer_id=sibling["peer_id"], owner_id=sibling["owner_id"], version=2,
                                             parent_id=head["id"], author_label=sibling["fallback"]))
            return owner_conn.execute("SELECT count(*) FROM shared_memory WHERE namespace = %s AND key = %s", (MESH, key)).fetchone()[0]

    assert by_the_owner_role_without_the_single_writer_rule(author["owner_id"]) == 2,         "control: with the mark, and 011's rule out of the way, the agree trigger admits the edit"
    # skill: another skill's payload, the mark set and every other condition met: only memory.write and memory.edit are ever admitted as the
    # signed bytes of a row, and memory.edit only on the edit path
    with pytest.raises(psycopg.errors.CheckViolation, match="E_SIGNED_BYTES_DISAGREE.*skill_name"):
        by_the_owner_role_without_the_single_writer_rule(author["owner_id"], skill="memory.redact")
    # mark: a memory.edit request with no mark
    with pytest.raises(psycopg.errors.CheckViolation, match="E_SIGNED_BYTES_DISAGREE.*skill_name"):
        by_the_owner_role_without_the_single_writer_rule(None)
    _unchanged(owner_conn, key)

    # CONTROL: the mail edit by a sibling with the mark passes the same trigger
    _direct(app_conn, head, peer=sibling, value=dict(value, body="an edit"), mark=author["owner_id"])


def test_the_edit_record_is_append_only_holds_no_text_and_goes_with_its_row(app_conn, owner_conn):
    author, sibling, key, value = _author_and_sibling(owner_conn, app_conn, "d9")
    head = _head(owner_conn, MESH, key)
    _direct(app_conn, head, peer=sibling, value=dict(value, body="an edit"), mark=author["owner_id"])
    new = _head(owner_conn, MESH, key)
    # version = prior + 1 is the table's own CHECK: the record is the handler's in every other way (bound to its row), so nothing else refuses it
    with pytest.raises(psycopg.errors.CheckViolation) as exc:
        owner_conn.execute(
            "INSERT INTO shared_memory_edit (memory_id, namespace, key, version, prior_version, edited_by_peer_id, "
            "edited_by_owner_id) VALUES (%s, %s, %s, 2, 5, %s, %s)",
            (new["id"], MESH, key, sibling["peer_id"], sibling["owner_id"]))
    assert exc.value.diag.constraint_name, "refused by something other than a named CHECK constraint (the binding trigger has no name)"
    owner_conn.execute(
        "INSERT INTO shared_memory_edit (memory_id, namespace, key, version, prior_version, edited_by_peer_id, "
        "edited_by_owner_id, request_call_id) VALUES (%s, %s, %s, 2, 1, %s, %s, 'call-x')",
        (new["id"], MESH, key, sibling["peer_id"], sibling["owner_id"]))
    for statement in ("UPDATE shared_memory_edit SET request_call_id = 'y' WHERE memory_id = %s",
                      "DELETE FROM shared_memory_edit WHERE memory_id = %s"):
        with pytest.raises(psycopg.errors.CheckViolation, match="E_EDIT_APPEND_ONLY"):
            owner_conn.execute(statement, (new["id"],))
    types = {r[0]: r[1] for r in owner_conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'shared_memory_edit'").fetchall()}
    assert not {c for c, t in types.items() if t in ("bytea", "jsonb", "json")}, types      # the row holds the signed request, this holds none
    # the cascade from the row still removes the record (retention): delete the message's rows
    owner_conn.execute("DELETE FROM shared_memory WHERE namespace = %s AND key = %s AND version = 2", (MESH, key))
    assert owner_conn.execute("SELECT count(*) FROM shared_memory_edit WHERE memory_id = %s", (new["id"],)).fetchone()[0] == 0


def test_the_edit_record_is_for_the_server_alone(app_conn, owner_conn):
    """Two walls behind the append-only trigger, each against its control. Row-level security shows the record only to the server-internal
    context (the handler and the wait's reads run in it), and meshychat_app holds no UPDATE, DELETE or TRUNCATE on it, so a change is refused
    as a missing privilege before the trigger's own refusal is reached."""
    author, sibling, key, value = _author_and_sibling(owner_conn, app_conn, "d10")
    handlers.dispatch("memory.edit", _envelope(sibling, "memory.edit", {
        "namespace": MESH, "key": key, "value": dict(value, body="an edit"), "expected_head_version": 1}), sibling["peer_id"], app_conn)
    count = "SELECT count(*) FROM shared_memory_edit WHERE key = %s"
    assert owner_conn.execute(count, (key,)).fetchone()[0] == 1, "control: the edit wrote its record"
    with rls.server_internal(app_conn):
        assert app_conn.execute(count, (key,)).fetchone()[0] == 1, "control: the server-internal context sees the record"
    assert app_conn.execute(count, (key,)).fetchone()[0] == 0, "a session of meshychat_app outside that context saw the record"
    have = {priv: owner_conn.execute("SELECT has_table_privilege('meshychat_app', 'shared_memory_edit', %s)", (priv,)).fetchone()[0]
            for priv in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")}
    assert have == {"SELECT": True, "INSERT": True, "UPDATE": False, "DELETE": False, "TRUNCATE": False}, have
    for statement in ("UPDATE shared_memory_edit SET request_call_id = 'y' WHERE key = %s",
                      "DELETE FROM shared_memory_edit WHERE key = %s"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with rls.server_internal(app_conn):
                app_conn.execute(statement, (key,))
    assert owner_conn.execute(count, (key,)).fetchone()[0] == 1


def test_the_edit_record_is_bound_to_its_row_each_condition_alone(app_conn, owner_conn):
    """memory-bxxxxx's review of #35: `memory.read`, the message frame and the event take an editor and a time from the record, so the
    database holds it to its row. Each condition is taken away ALONE, the record otherwise exactly what the handler writes; the control is
    that record."""
    author, sibling, key, value = _author_and_sibling(owner_conn, app_conn, "d11")
    other = _peer(owner_conn, "d11o")                                   # a peer of ANOTHER owner
    head = _head(owner_conn, MESH, key)
    _direct(app_conn, head, peer=sibling, value=dict(value, body="an edit"), mark=author["owner_id"])
    new = _head(owner_conn, MESH, key)
    insert = ("INSERT INTO shared_memory_edit (memory_id, namespace, key, version, prior_version, edited_by_peer_id, edited_by_owner_id) "
              "VALUES (%s, %s, %s, %s, %s, %s, %s)")
    fields = ("memory_id", "namespace", "key", "version", "prior_version", "peer", "owner")
    bound = (new["id"], MESH, key, 2, 1, sibling["peer_id"], sibling["owner_id"])        # what the handler writes

    def refused(**changed):
        args = list(bound)
        for name, replacement in changed.items():
            args[fields.index(name)] = replacement
        with pytest.raises(psycopg.errors.CheckViolation, match="E_EDIT_RECORD_UNBOUND"):
            owner_conn.execute(insert, args)

    refused(namespace="shared/wiki")
    refused(key=_key(author["name"]))
    refused(version=3, prior_version=2)                                  # consistent with itself (the CHECK holds), not the row's version
    refused(peer=author["peer_id"], owner=author["owner_id"])            # a consistent editor, but the row was created by the sibling
    refused(owner=other["owner_id"])                                     # the editor's owner is what `peers` says, not what the record says
    refused(memory_id=uuid.uuid4())                                      # no such row
    # the row must carry a memory.edit request: version 1 is a memory.write one, and the record is bound to it in every other way
    with pytest.raises(psycopg.errors.CheckViolation, match="E_EDIT_RECORD_UNBOUND"):
        owner_conn.execute(insert, (head["id"], MESH, key, 1, 0, author["peer_id"], author["owner_id"]))
    count = "SELECT count(*) FROM shared_memory_edit WHERE key = %s"
    assert owner_conn.execute(count, (key,)).fetchone()[0] == 0, "a refused record was written"
    owner_conn.execute(insert, bound)                                    # CONTROL: the handler's own record
    assert owner_conn.execute(count, (key,)).fetchone()[0] == 1


def _function_text(path: str, name: str) -> str:
    s = (ROOT / path).read_text(encoding="utf-8")
    start = s.index(f"CREATE OR REPLACE FUNCTION {name}(")
    return s[start:s.index("\n$$;", start) + len("\n$$;")]


def test_029s_copies_of_the_functions_it_replaces_match_their_sources():
    """The down script restores 011's and 014's functions as they stand in their own files (verbatim), and the up script's agree
    function is 014's with ONE block changed: everything before and after the skill_name check is byte for byte 014's. So there are
    not three copies to drift."""
    up = (ROOT / "sql_meshychat" / "029_mesh_message_edit.sql").read_text(encoding="utf-8")
    down = (ROOT / "sql_meshychat" / "down" / "029_mesh_message_edit_down.sql").read_text(encoding="utf-8")
    agree_014 = _function_text("sql_meshychat/014_shared_memory_signed_bytes.sql", "shared_memory_signed_bytes_agree")
    single_011 = _function_text("sql_meshychat/011_mesh_message_single_writer.sql", "shared_memory_mesh_message_single_writer")
    assert agree_014 in down, "the down script's agree function is not 014's"
    assert single_011 in down, "the down script's single-writer function is not 011's"
    agree_029 = _function_text("sql_meshychat/029_mesh_message_edit.sql", "shared_memory_signed_bytes_agree")
    start = "    IF p ->> 'skill_name' IS DISTINCT FROM 'memory.write' THEN\n"
    end = "    IF p ->> 'caller_peer_id' IS DISTINCT FROM NEW.created_by_peer_id::text THEN\n"
    a014, b014 = agree_014.split(start, 1)[0], agree_014.split(end, 1)[1]
    a029, b029 = agree_029.split(start, 1)[0], agree_029.split(end, 1)[1]
    assert (a029, b029) == (a014, b014), "029's agree function differs from 014's outside the skill_name block"
    assert "memory.edit" in agree_029.split(start, 1)[1].split(end, 1)[0]
    assert re.search(r"CREATE OR REPLACE FUNCTION shared_memory_mesh_message_single_writer", up)
