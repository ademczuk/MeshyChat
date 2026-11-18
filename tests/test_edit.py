"""memory.edit (divergence 059, apps/memoryskills/edit.py). Design: docs/design/edit.md section 7, tests 1 to 5 and 7 to 11 and the record.
The database's own cases are tests/test_edit_db_rule.py; the event's are tests/test_edit_delivery.py and tests/test_edit_push.py.

Every positive test is paired with what must now be ABSENT, and every refusal with a control showing the same seat can do the
neighbouring thing, so a refusal is about the rule. The handler runs as meshychat_app under FORCE RLS, as a request does.

Run:  .venv313/Scripts/python.exe -m pytest tests/test_edit.py -q -p no:cacheprovider
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from apps.memoryskills import edit, handlers  # noqa: E402
from apps.memoryskills.errors import HandlerError  # noqa: E402
from apps.core import rls  # noqa: E402
from apps.mesh import projection as P  # noqa: E402
from tests.test_edit_db_rule import _author_and_sibling  # noqa: E402
from tests.test_mesh_authorship import (  # noqa: E402,F401
    MESH, _admit, _envelope, _key, _msg, _peer, _refused, _rows, _value, _write, app_conn, owner_conn,
)
from tests.test_redaction import census  # noqa: E402
from tests.test_redaction_derived import DELETED, KEPT, _seed  # noqa: E402


def _args(key, value, expected):
    return {"namespace": MESH, "key": key, "value": value, "expected_head_version": expected}


def _edit(conn, peer, key, value, expected):
    return handlers.dispatch("memory.edit", _envelope(peer, "memory.edit", _args(key, value, expected)),
                             peer["peer_id"], conn)


def _edit_refused(conn, peer, key, value, expected) -> HandlerError:
    with pytest.raises(HandlerError) as exc:
        _edit(conn, peer, key, value, expected)
    return exc.value


def _redact(conn, peer, key, **more):
    return handlers.dispatch("memory.redact", _envelope(peer, "memory.redact", {
        "namespace": MESH, "key": key, "reason_code": "sent_in_error", **more}), peer["peer_id"], conn)


def _versions(owner_conn, key) -> int:
    return len(_rows(owner_conn, MESH, key))


def test_1_a_sibling_seat_edits_and_the_history_is_kept(app_conn, owner_conn):
    author, sibling, key, value = _author_and_sibling(owner_conn, app_conn, "e1")
    out = _edit(app_conn, sibling, key, dict(value, body="the corrected body"), 1)
    assert (out["version"], out["prior_version"], out["replay"], out["author_label"]) == (
        2, 1, False, sibling["fallback"]), out
    assert _rows(owner_conn, MESH, key) == [
        (1, False, str(author["peer_id"]), author["fallback"], author["name"], "the original body"),
        (2, True, str(sibling["peer_id"]), sibling["fallback"], author["name"], "the corrected body")]
    # `from` stays the ORIGINAL sender, and the row is the request the editor signed, with nothing a write would derive
    skill, tags, status = owner_conn.execute(
        "SELECT provenance_jsonb -> 'payload' ->> 'skill_name', tags, status FROM shared_memory "
        "WHERE namespace = %s AND key = %s AND version = 2", (MESH, key)).fetchone()
    assert (skill, tags, status) == ("memory.edit", [], None)
    prior, by_peer, by_owner, notify = owner_conn.execute(
        "SELECT r.prior_version, r.edited_by_peer_id, r.edited_by_owner_id, r.notify_peer_ids "
        "FROM shared_memory_edit r JOIN shared_memory sm ON sm.id = r.memory_id WHERE sm.key = %s", (key,)).fetchone()
    assert (prior, str(by_peer), str(by_owner), notify) == (1, str(sibling["peer_id"]), str(author["owner_id"]), [])
    # CONTROL: memory.write of a later version stays refused for the sibling, and the record holds no text of the new body
    # (R2 answers first: the value names the author as `from`, which is not the sibling's label; R4 would refuse it next)
    assert _refused(app_conn, sibling, _msg(MESH, key, dict(value, body="a write"))).code in (
        "E_FROM_MISMATCH", "E_NOT_MESSAGE_AUTHOR")
    marker = f"zq-edit-{uuid.uuid4().hex}"
    _edit(app_conn, author, key, dict(value, body=f"body {marker}"), 2)
    hits = census(owner_conn, marker)
    assert "shared_memory.value" in hits and not [h for h in hits if h.startswith("shared_memory_edit")], hits


def test_2_the_authors_own_seat_edits_through_the_same_door(app_conn, owner_conn):
    author, sibling, key, value = _author_and_sibling(owner_conn, app_conn, "e2")
    out = _edit(app_conn, author, key, dict(value, body="an author's edit"), 1)
    assert out["version"] == 2 and out["author_label"] == author["fallback"]
    assert [(r[0], r[1], r[5]) for r in _rows(owner_conn, MESH, key)] == [
        (1, False, "the original body"), (2, True, "an author's edit")]


def test_3_a_seat_of_another_owner_is_refused_and_the_chain_is_unchanged(app_conn, owner_conn):
    author, sibling, key, value = _author_and_sibling(owner_conn, app_conn, "e3")
    other = _peer(owner_conn, "e3x")
    _admit(owner_conn, MESH, other)
    err = _edit_refused(app_conn, other, key, dict(value, body="not mine to edit"), 1)
    assert err.code == "E_PERMISSION_DENIED" and key in err.message, err
    assert _versions(owner_conn, key) == 1
    # CONTROL: the same seat writes its own message without trouble, so the refusal is about the owner
    _write(app_conn, other, _msg(MESH, _key(other["name"]), _value(other["name"], "mine")))


FROZEN = [
    ("to", ["someone-else"], {"E_INVALID_ARGS"}),
    ("cc", ["a-cc-seat"], {"E_INVALID_ARGS"}),
    ("thread_id", "other/thread", {"E_INVALID_ARGS"}),
    ("in_reply_to", "msg/1-x-000001", {"E_INVALID_ARGS"}),
    ("type", "question", {"E_INVALID_ARGS"}),
    ("ts", "2026-09-24T00:00:00Z", {"E_INVALID_ARGS"}),
    ("author_family", "codex", {"E_INVALID_ARGS"}),
    ("requires_ack", True, {"E_INVALID_ARGS"}),
    ("subject", "a-subject", {"E_INVALID_ARGS"}),
    ("ref", "a-ref", {"E_INVALID_ARGS"}),
    ("session_label", "sess#1", {"E_INVALID_ARGS"}),
    ("from", "someone-else", {"E_KEY_LABEL_MISMATCH", "E_INVALID_ARGS"}),
    ("schema_version", 2, {"E_MESH_SCHEMA"}),
    ("surprise", 1, {"E_MESH_SCHEMA"}),
]


@pytest.mark.parametrize("field,changed,codes", FROZEN, ids=[f[0] for f in FROZEN])
def test_4_everything_but_the_body_is_frozen(app_conn, owner_conn, field, changed, codes):
    """An edit that changed `to` or `cc` would be a send to new recipients that skipped every send check; one that narrows them hides
    the history from someone who had it. Each field alone, and the control (test 1) is the same call with the body alone."""
    author, sibling, key, value = _author_and_sibling(owner_conn, app_conn, "e4")
    err = _edit_refused(app_conn, sibling, key, dict(value, body="an edit", **{field: changed}), 1)
    assert err.code in codes, err
    if codes == {"E_INVALID_ARGS"}:
        # The HANDLER refused it, not only the database: the trigger (sql_meshychat/029) refuses the same edit with the same code
        # and says "another field differs", so the code cannot tell the layers apart; the handler names the field.
        assert f"{field} differ from version 1" in err.message, err
    assert _versions(owner_conn, key) == 1, "the refused edit left a version behind"


def test_5_memory_write_of_a_later_version_is_closed_for_the_author_and_the_sibling(app_conn, owner_conn):
    """The author's own later version through memory.write was an unguarded edit path (nothing froze to or cc on it, and no seat was
    told). Every later version of a mail key is memory.edit's now. CONTROLS: a first version of a NEW key still writes."""
    author, sibling, key, value = _author_and_sibling(owner_conn, app_conn, "e5")
    err = _refused(app_conn, author, _msg(MESH, key, dict(value, body="rewritten by the author")))
    assert err.code == "E_USE_MEMORY_EDIT" and "memory.edit" in err.message, err
    assert _refused(app_conn, sibling, _msg(MESH, key, dict(value, body="rewritten by a sibling"))).code in (
        "E_FROM_MISMATCH", "E_NOT_MESSAGE_AUTHOR")
    assert _versions(owner_conn, key) == 1
    _write(app_conn, author, _msg(MESH, _key(author["name"]), _value(author["name"], "a new message")))


def test_7_redaction_after_an_edit_covers_the_versions_and_a_tombstone_is_final(app_conn, owner_conn):
    """Memory-bxxxxx's build note: redaction still works once memory.write's later version is closed. An OLD version redacted alone
    leaves the head editable; `version: all` redacts every version; and a redacted head cannot be edited (redact-then-edit would
    turn a redaction into a replace). CONTROL: the edits before any redaction pass."""
    author, sibling, key, value = _author_and_sibling(owner_conn, app_conn, "e7")
    _edit(app_conn, sibling, key, dict(value, body="second"), 1)
    _redact(app_conn, author, key, version=1)
    assert [(r[0], r[5]) for r in _rows(owner_conn, MESH, key)] == [(1, None), (2, "second")]
    _edit(app_conn, author, key, dict(value, body="third"), 2)                 # the head was not redacted
    out = _redact(app_conn, author, key, version="all")
    assert sorted(r["version"] for r in out["redacted"]) == [2, 3] and [r["version"] for r in out["already_redacted"]] == [1]
    err = _edit_refused(app_conn, sibling, key, dict(value, body="over the tombstone"), 3)
    assert err.code == "E_MESSAGE_REDACTED", err
    assert [r[5] for r in _rows(owner_conn, MESH, key)] == [None, None, None]


def test_8_the_head_it_was_made_against_and_the_version_cap(app_conn, owner_conn):
    author, sibling, key, value = _author_and_sibling(owner_conn, app_conn, "e8")
    assert _edit_refused(app_conn, sibling, key, dict(value, body="from a stale head"), 5).code == "E_WRITE_CONFLICT"
    assert _versions(owner_conn, key) == 1
    for n in range(2, 21):                                                       # versions 2 to 20: control, the right head passes
        _edit(app_conn, sibling if n % 2 else author, key, dict(value, body=f"body {n}"), n - 1)
    assert _versions(owner_conn, key) == 20
    err = _edit_refused(app_conn, author, key, dict(value, body="the twenty-first"), 20)
    assert err.code == "E_INVALID_ARGS" and "20 versions" in err.message, err
    assert _versions(owner_conn, key) == 20


def test_9_a_replayed_request_answers_the_first_result_and_writes_one_version(app_conn, owner_conn):
    author, sibling, key, value = _author_and_sibling(owner_conn, app_conn, "e9")
    envelope = _envelope(sibling, "memory.edit", _args(key, dict(value, body="once"), 1))
    first = handlers.dispatch("memory.edit", envelope, sibling["peer_id"], app_conn)
    second = handlers.dispatch("memory.edit", envelope, sibling["peer_id"], app_conn)
    assert (first["replay"], second["replay"]) == (False, True)
    assert second["version"] == first["version"] == 2 and second["id"] == first["id"]
    assert _versions(owner_conn, key) == 2
    # another signer cannot replay it (memory.write's own rule)
    with pytest.raises(HandlerError) as exc:
        handlers.dispatch("memory.edit", {**envelope}, author["peer_id"], app_conn)
    assert exc.value.code in ("E_BAD_ENVELOPE", "E_REPLAY"), exc.value


def test_10_what_was_derived_from_the_old_versions_text_is_deleted_and_the_routing_stays(app_conn, owner_conn):
    """The edit deletes the derived rows of the version it replaces, in the same transaction, by the list redaction uses (in the
    projection-writer context, with the postcondition), so the old text is not findable through a term or a vector once it is
    replaced. The routing edge stays, and a related edge naming the old version at either end goes."""
    author, sibling, key, value = _author_and_sibling(owner_conn, app_conn, "e10", body="old text zq-derived")
    newer_key = _key(author["name"])
    _write(app_conn, author, _msg(MESH, newer_key, _value(author["name"], "a later message")))
    ids = {k: owner_conn.execute("SELECT id FROM shared_memory WHERE namespace = %s AND key = %s AND is_head",
                                 (MESH, k)).fetchone()[0] for k in (key, newer_key)}
    seeded = _seed(owner_conn, {"id": ids[key], "key": key}, {"id": ids[newer_key], "key": newer_key})
    _edit(app_conn, sibling, key, dict(value, body="new text"), 1)
    for table in DELETED:
        assert _count(owner_conn, table, seeded[table]) == 0, f"{table} still holds a row derived from the OLD version"
    for table in KEPT:
        assert _count(owner_conn, table, seeded[table]) == 1, f"{table}: the routing edge went with the text"


def _mentions_of(conn, src_id):
    with rls.server_internal(conn):
        return conn.execute("SELECT dst FROM mesh_edges WHERE src_id = %s AND edge_type = 'mentions'",
                            (src_id,)).fetchall()


def test_an_edit_removes_the_old_bodys_mentions(owner_conn, app_conn, monkeypatch):
    """MUTANT: leave `mentions` out of redact.delete_derived's mesh-edge cleanup.

    The gate is open, so the old body's mentions exist before the edit: with it closed the projection never
    derived any and the test passed with the cleanup removed (codex on #93)."""
    monkeypatch.setattr("apps.meshsem.store.forbidden_rows_open", lambda: True)
    author, sibling, key, value = _author_and_sibling(owner_conn, app_conn, "e10mentions",
                                                       body="old reference acme/widget#73 c0ffee1")
    P.rebuild(owner_conn)
    old_id = owner_conn.execute("SELECT id FROM shared_memory WHERE namespace = %s AND key = %s AND version = 1",
                                (MESH, key)).fetchone()[0]
    assert _mentions_of(owner_conn, old_id), "the old body's mentions exist before the edit"
    _edit(app_conn, sibling, key, dict(value, body="replacement text"), 1)
    P.catch_up(owner_conn)
    with rls.server_internal(owner_conn):
        leftovers = owner_conn.execute("SELECT dst FROM mesh_edges WHERE src_id = %s AND edge_type = 'mentions'",
                                       (old_id,)).fetchall()
    assert leftovers == [], leftovers


def test_a_late_catch_up_or_a_rebuild_never_brings_back_a_retired_bodys_mentions(owner_conn, app_conn, monkeypatch):
    """Codex on #93: the replay reads every version, so a version edited before catch-up reached it, or any rebuild
    after an edit, re-derived the retired body's mentions. Only a head gets them; the new head's own mention is the
    control that the gate is open. MUTANT: drop the is_head test from the projection's mentions gate."""
    monkeypatch.setattr("apps.meshsem.store.forbidden_rows_open", lambda: True)
    author, sibling, key, value = _author_and_sibling(owner_conn, app_conn, "e10late",
                                                       body="old reference acme/widget#74 c0ffee2")
    _edit(app_conn, sibling, key, dict(value, body="new reference acme/widget#75"), 1)   # before any catch-up
    ids = dict(owner_conn.execute("SELECT version, id FROM shared_memory WHERE namespace = %s AND key = %s",
                                  (MESH, key)).fetchall())
    P.catch_up(owner_conn)
    assert _mentions_of(owner_conn, ids[1]) == []
    assert {r[0] for r in _mentions_of(owner_conn, ids[2])} >= {"gh:acme/widget#75"}
    P.rebuild(owner_conn)
    assert _mentions_of(owner_conn, ids[1]) == []
    assert {r[0] for r in _mentions_of(owner_conn, ids[2])} >= {"gh:acme/widget#75"}


def test_redacting_one_version_of_an_edited_ask_keeps_the_head_owed_and_awaited(owner_conn, app_conn):
    """memory-bxxxxx on #103: an ask edited to version 2, then version 1 alone redacted. The redaction takes version 1's
    owes_ack edges and nothing of the head's, so the ask stays owed by its recipient and awaited by its author, after the
    catch-up and after a rebuild, and the two agree. CONTROL: before the redaction both versions carry the edge.
    MUTANT D: delete owes_ack for every version of the KEY instead of the redacted row; the incremental owed loses the
    head while a rebuild puts it back."""
    from apps.mesh import graph as G

    author, owing = _peer(owner_conn, "rva"), _peer(owner_conn, "rvo")
    _admit(owner_conn, MESH, author)
    key = _key(author["name"])
    value = dict(_value(author["name"], "please answer"), to=[owing["name"]], requires_ack=True,
                 thread_id=f"t/rv-{uuid.uuid4().hex[:8]}")
    _write(app_conn, author, _msg(MESH, key, value))
    _edit(app_conn, author, key, dict(value, body="please answer, corrected"), 1)
    ids = dict(owner_conn.execute("SELECT version, id FROM shared_memory WHERE namespace = %s AND key = %s",
                                  (MESH, key)).fetchall())

    def state():
        every = ["open", "active", "answered", "closed"]
        with rls.server_internal(owner_conn):
            owed = {i.hit.key for i in G.owed_acks(owner_conn, owing["name"], limit=None, states=every)}
            awaiting = {i.hit.key for i in G.awaiting(owner_conn, [author["name"], author["fallback"]], limit=None,
                                                      states=every)}
            edges = {v: owner_conn.execute("SELECT count(*) FROM mesh_edges WHERE src_id = %s "
                                           "AND edge_type = 'owes_ack'", (ids[v],)).fetchone()[0] for v in (1, 2)}
        return key in owed, key in awaiting, edges

    P.catch_up(owner_conn)
    assert state() == (True, True, {1: 1, 2: 1}), "control: both versions carry the edge and the ask is owed"
    _redact(app_conn, author, key, version=1)
    P.catch_up(owner_conn)
    incremental = state()
    P.rebuild(owner_conn)
    rebuilt = state()
    assert incremental == (True, True, {1: 0, 2: 1}), incremental
    assert rebuilt == incremental


def _count(conn, table, case):
    from tests.test_projection_writer import _count as count
    return count(conn, table, case)


def test_11_the_refusals_each_beside_a_control(app_conn, owner_conn):
    author, sibling, key, value = _author_and_sibling(owner_conn, app_conn, "e11")
    edited = dict(value, body="an edit")
    good = _args(key, edited, 1)

    def refused(args, peer=sibling):
        with pytest.raises(HandlerError) as exc:
            handlers.dispatch("memory.edit", _envelope(peer, "memory.edit", args), peer["peer_id"], app_conn)
        return exc.value

    assert refused({**good, "namespace": "shared/wiki"}).code == "E_INVALID_ARGS"
    assert refused({**good, "key": "cursor/not-a-message"}).code == "E_INVALID_ARGS"
    assert refused({**good, "tags": ["x"]}).code == "E_INVALID_ARGS"                 # nothing beyond the four args
    assert refused({**good, "value": "not an object"}).code == "E_INVALID_ARGS"
    assert refused({**good, "expected_head_version": True}).code == "E_INVALID_ARGS"
    assert refused({**good, "expected_head_version": "1"}).code == "E_INVALID_ARGS"
    assert refused({**good, "value": dict(value, body="   ")}).code == "E_MESH_SCHEMA"   # an empty body
    assert refused({**good, "value": dict(value)}).code == "E_INVALID_ARGS"              # the body is unchanged: nothing to edit
    assert refused({**good, "key": _key(author["name"])}).code == "E_UNKNOWN_KEY"
    assert _versions(owner_conn, key) == 1, "a refused edit left a version behind"
    # a capability token is never accepted: an edit changes what other seats read
    env = _envelope(sibling, "memory.edit", good)
    with pytest.raises(HandlerError) as exc:
        edit.handle_edit(env, sibling["peer_id"], app_conn, principal=object())
    assert exc.value.code == "E_CAPABILITY_NOT_ACCEPTED"
    # CONTROL: the same call, with none of the above, lands
    assert _edit(app_conn, sibling, key, edited, 1)["version"] == 2


def test_12_the_record_names_the_authors_of_the_replies_and_never_the_editor(app_conn, owner_conn):
    """`notify_peer_ids` is read before the record is written: the authors of the direct replies, as they stood when the edit was made
    (a reply written afterwards is not on it, so the wait and the push cannot disagree about it). The editor is not on it."""
    author, sibling, key, value = _author_and_sibling(owner_conn, app_conn, "e12")
    replier = _peer(owner_conn, "e12r")
    _admit(owner_conn, MESH, replier)
    reply = dict(_value(replier["name"], "a reply"), in_reply_to=key)
    _write(app_conn, replier, _msg(MESH, _key(replier["name"]), reply))
    _write(app_conn, sibling, _msg(MESH, _key(sibling["name"]), dict(_value(sibling["name"], "the editor replied too"), in_reply_to=key)))
    out = _edit(app_conn, sibling, key, dict(value, body="an edit after two replies"), 1)
    assert [a["peer_id"] for a in out["reply_authors"]] == [str(replier["peer_id"])], out
    notify = owner_conn.execute("SELECT r.notify_peer_ids FROM shared_memory_edit r JOIN shared_memory sm ON sm.id = r.memory_id "
                                "WHERE sm.key = %s", (key,)).fetchone()[0]
    assert [str(p) for p in notify] == [str(replier["peer_id"])], notify
    late = _peer(owner_conn, "e12l")
    _admit(owner_conn, MESH, late)
    _write(app_conn, late, _msg(MESH, _key(late["name"]), dict(_value(late["name"], "a late reply"), in_reply_to=key)))
    later = _edit(app_conn, author, key, dict(value, body="a second edit"), 2)
    assert {a["peer_id"] for a in later["reply_authors"]} == {str(replier["peer_id"]), str(late["peer_id"]), str(sibling["peer_id"])}


def _read_row(conn, peer, key, **more):
    return handlers.dispatch("memory.read", _envelope(peer, "memory.read", {"namespace": MESH, "key": key, **more}),
                             peer["peer_id"], conn)["memo"]


def test_17_memory_read_says_a_version_an_edit_wrote_is_an_edit_and_says_nothing_of_the_others(app_conn, owner_conn):
    """`from` stays the original sender, so a reader is told the body is an edit by `edited_by` and `edited_at`, read from the server's edit
    RECORD and never from the value (docs/design/edit.md section 2, test 17). Version 1 says nothing. A version above 1 that no record
    describes (a row a path inserted without one) says nothing either: the fields come from the record, not from the version number."""
    from tests.test_edit_db_rule import _direct
    from tests.test_mesh_authorship import _head

    author, sibling, key, value = _author_and_sibling(owner_conn, app_conn, "e17")
    v1 = _read_row(app_conn, author, key)
    assert v1["version"] == 1 and not {"edited_by", "edited_at"} & set(v1), "control: version 1"
    _edit(app_conn, sibling, key, dict(value, body="the corrected body"), 1)
    head = _read_row(app_conn, author, key)
    record_at = owner_conn.execute(
        "SELECT to_char(r.edited_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') FROM shared_memory_edit r "
        "JOIN shared_memory sm ON sm.id = r.memory_id WHERE sm.key = %s", (key,)).fetchone()[0]
    assert (head["version"], head["edited_by"], head["edited_at"]) == (2, sibling["fallback"], record_at), head
    assert head["value"]["from"] == author["name"] and head["author_label"] == sibling["fallback"], "from stays; the editor is author_label"
    old = _read_row(app_conn, author, key, version=1)
    assert old["version"] == 1 and not {"edited_by", "edited_at"} & set(old), "the old version still says nothing of edits"
    assert old["value"]["body"] == "the original body" and head["value"]["body"] == "the corrected body"

    _direct(app_conn, _head(owner_conn, MESH, key), peer=sibling, value=dict(value, body="a version no record describes"),
            mark=author["owner_id"])
    third = _read_row(app_conn, author, key)
    assert third["version"] == 3 and not {"edited_by", "edited_at"} & set(third), third


#: The version a reply answered (docs/design/edit.md section 5): the highest version of the parent whose WRITE position in the commit order
#: is below the reply's own. It is a display aid, computed on demand; this is the query, and the test below is its proof.
ANSWERED_VERSION_SQL = (
    "SELECT max(p.version) FROM shared_memory p JOIN mesh_commit_order po ON po.memory_id = p.id AND po.kind = 'write' "
    "WHERE p.namespace = %s AND p.key = %s "
    "AND po.seq < (SELECT o.seq FROM mesh_commit_order o WHERE o.memory_id = %s AND o.kind = 'write')")


def test_18_the_version_a_reply_answered_is_by_commit_order_and_created_at_would_give_another(app_conn, owner_conn):
    """A reply written before the edit answered version 1; one written after answered version 2; and one whose TRANSACTION STARTED before a later
    edit committed and committed after it answered version 3 by commit order (the order every seat sees events in), where `created_at`, the
    transaction's start, would say version 2. The last is what section 5 is about, and the reason `mesh_commit_order` exists (sql_meshychat/023)."""
    import psycopg

    from tests.fixtures import signed_rows as sr
    from tests.test_mesh_authorship import DSN

    author, sibling, key, value = _author_and_sibling(owner_conn, app_conn, "e18")
    replier = _peer(owner_conn, "e18r")
    _admit(owner_conn, MESH, replier)

    def reply_via_handler() -> str:
        rkey = _key(replier["name"])
        _write(app_conn, replier, _msg(MESH, rkey, dict(_value(replier["name"], "a reply"), in_reply_to=key)))
        return rkey

    def answered(rkey: str) -> int:
        rid = owner_conn.execute("SELECT id FROM shared_memory WHERE namespace = %s AND key = %s", (MESH, rkey)).fetchone()[0]
        return owner_conn.execute(ANSWERED_VERSION_SQL, (MESH, key, rid)).fetchone()[0]

    before = reply_via_handler()
    _edit(app_conn, sibling, key, dict(value, body="the second version"), 1)
    after = reply_via_handler()

    rkey, rid = _key(replier["name"]), uuid.uuid4()
    with psycopg.connect(DSN) as tx:                       # a transaction that STARTS now and commits after the third version does
        # the start from the DATABASE clock that stamps every version, never the host's: a host a few ms ahead of the database made
        # version 3's created_at fall before `started` when the edit came within that gap, so by_created_at read 3 (flaky, about 1 in 3)
        started = tx.execute("SELECT clock_timestamp()").fetchone()[0]
        sr.insert(tx, sr.row(replier["peer_id"], replier["owner_id"], MESH, rkey,
                             dict(_value(replier["name"], "an overlapping reply"), in_reply_to=key), rid=rid,
                             call_id=f"call-{rid}", author_label=replier["fallback"], created_at=started))
        _edit(app_conn, author, key, dict(value, body="the third version"), 2)
        tx.commit()

    assert (answered(before), answered(after), answered(rkey)) == (1, 2, 3)
    by_created_at = owner_conn.execute(
        "SELECT max(p.version) FROM shared_memory p WHERE p.namespace = %s AND p.key = %s "
        "AND p.created_at < (SELECT created_at FROM shared_memory WHERE id = %s)", (MESH, key, rid)).fetchone()[0]
    assert by_created_at == 2 != answered(rkey), "the overlapping reply is the case created_at gets wrong"


def test_19_an_edit_that_waited_for_the_head_row_answers_a_conflict_not_an_unknown_key(app_conn, owner_conn):
    """Two editors of one head (memory-bxxxxx's review of #35). The first holds the head row while the second waits for it, then retires the head,
    writes version 2 and commits. The second's `SELECT ... WHERE is_head ... FOR UPDATE` is re-checked by READ COMMITTED against the updated row,
    which is no longer head, so it returns NOTHING (its statement began before the commit and never sees version 2): it used to answer
    E_UNKNOWN_KEY, `no such message`, for a message that exists. It answers E_WRITE_CONFLICT, which tells its client to read again.
    CONTROL: a key that does not exist is still E_UNKNOWN_KEY, and the first editor's version 2 is the head at the end."""
    import threading
    import time

    import psycopg

    from apps.memoryskills import db
    from tests.fixtures import signed_rows as sr
    from tests.test_mesh_authorship import DSN, _head

    author, sibling, key, value = _author_and_sibling(owner_conn, app_conn, "e19")
    head = _head(owner_conn, MESH, key)
    outcome: dict = {}
    second_conn = db.connect(DSN)                       # the second editor, a request's role
    first = psycopg.connect(DSN)                        # the first editor's transaction, the owner role; not autocommit, so it holds its lock until commit
    try:
        first.execute("SELECT id FROM shared_memory WHERE id = %s FOR UPDATE", (head["id"],))

        def second() -> None:
            try:
                outcome["result"] = _edit(second_conn, sibling, key, dict(value, body="the second editor"), 1)
            except BaseException as exc:                # noqa: BLE001 - the test reads what the call raised
                outcome["error"] = exc

        t = threading.Thread(target=second)
        t.start()
        for _ in range(150):                            # the second editor is waiting on a lock, not merely slow
            if owner_conn.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                                  "AND wait_event_type = 'Lock'").fetchone()[0]:
                break
            time.sleep(0.1)
        else:
            raise AssertionError("the second edit never waited for the head row")
        # the first editor's edit, in the edit path's own statements (test_edit_db_rule._direct does the same as meshychat_app)
        first.execute("SELECT set_config('app.server_internal', '1', true)")
        first.execute("SELECT set_config('app.mesh_edit_owner', %s, true)", (str(author["owner_id"]),))
        first.execute("UPDATE shared_memory SET is_head = FALSE WHERE id = %s", (head["id"],))
        p = sr.payload(author["peer_id"], MESH, key, dict(value, body="the first editor"), skill_name="memory.edit",
                       extra_args={"expected_head_version": 1})
        sr.insert(first, sr.columns(p, peer_id=author["peer_id"], owner_id=author["owner_id"], version=2, parent_id=head["id"],
                                    author_label=author["fallback"]))
        first.commit()
        t.join(30)
        assert not t.is_alive(), "the second edit never returned"
    finally:
        first.close()
        second_conn.close()
    err = outcome.get("error")
    assert isinstance(err, HandlerError) and err.code == "E_WRITE_CONFLICT", outcome
    assert "version" in err.message and key in err.message, err.message
    assert _versions(owner_conn, key) == 2 and _head(owner_conn, MESH, key)["version"] == 2, "the second edit left a version behind"
    # CONTROL: a key that is not there is still an unknown key
    assert _edit_refused(app_conn, sibling, _key(author["name"]), dict(value, body="nothing to edit"), 1).code == "E_UNKNOWN_KEY"


def _envelope_with_call_id(peer, skill, args, call_id):
    """`_envelope`, with the call id chosen: the signed request of a call whose id another call already used."""
    from datetime import datetime, timezone

    from apps.crypto import primitives as P

    payload = {"v": 1, "kind": "skill.call", "call_id": call_id, "caller_peer_id": str(peer["peer_id"]), "target_peer_id": str(peer["peer_id"]),
               "skill_name": skill, "skill_version": "1", "args": args, "args_attachments": [],
               "issued_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
    return {"payload": payload, "signature": P.b64u(P.sign(peer["seed"], P.jcs(payload))), "signing_kid": peer["kid"]}


def test_20_a_call_id_used_by_a_write_is_not_an_edit_replay(app_conn, owner_conn):
    """The hosted review of #35. The author reuses the call_id of the memory.write that created the message when calling memory.edit on the same key:
    the dedup finds version 1, the signer and key checks pass, and the handler answered it as a replay of an EDIT (`prior_version` 0) that changed
    nothing. Only a row with an edit record is replayed; a call_id that belongs to anything else is E_REPLAY. CONTROL: a genuine edit's replay still
    answers the first result and writes no second version."""
    author, sibling, key, value = _author_and_sibling(owner_conn, app_conn, "e20")
    write_call = owner_conn.execute("SELECT provenance_call_id FROM shared_memory WHERE namespace = %s AND key = %s AND version = 1",
                                    (MESH, key)).fetchone()[0]
    reused = _envelope_with_call_id(author, "memory.edit", _args(key, dict(value, body="an edit under the write's call id"), 1), write_call)
    with pytest.raises(HandlerError) as exc:
        handlers.dispatch("memory.edit", reused, author["peer_id"], app_conn)
    assert exc.value.code == "E_REPLAY", exc.value
    assert _versions(owner_conn, key) == 1, "the collision wrote or claimed a version"
    genuine = _envelope_with_call_id(author, "memory.edit", _args(key, dict(value, body="the real edit"), 1), f"call-{uuid.uuid4()}")
    first = handlers.dispatch("memory.edit", genuine, author["peer_id"], app_conn)
    again = handlers.dispatch("memory.edit", genuine, author["peer_id"], app_conn)
    assert (first["version"], first["replay"], again["version"], again["replay"]) == (2, False, 2, True), (first, again)
    assert _versions(owner_conn, key) == 2


def test_a_blanking_redaction_waits_for_a_projection_pass_and_any_other_redaction_does_not(owner_conn, app_conn):
    """The handler's side of the lock between a projection pass and a blanking redaction (divergence 073; Codex on #106).
    While a connection holds BLANKING_LOCK shared, as every pass does, a memory.redact with blank_thread_id waits, then
    completes when the lock is released. A redaction without it does not wait at all.
    CONTROL: the plain redaction finishes while the lock is held, so the wait is the lock's and not a slow connection's.
    MUTANT: drop the serialize_with_projection call from handle_redact; the blanking redaction finishes while the lock
    is held."""
    import threading

    from apps.memoryskills import db
    from apps.mesh import projection as proj
    from tests.test_mesh_authorship import DSN

    author, sibling, key, value = _author_and_sibling(owner_conn, app_conn, "e-lock")
    plain_key = _key(author["name"])
    _write(app_conn, author, _msg(MESH, plain_key, dict(value, body="a second row")))
    results = {}

    def redact_on_its_own_connection(name, k, **more):
        conn = db.connect(DSN)
        try:
            results[name] = _redact(conn, author, k, **more)
        finally:
            conn.close()

    with owner_conn.transaction():
        owner_conn.execute("SELECT pg_advisory_xact_lock_shared(%s, %s)", proj.BLANKING_LOCK)
        plain = threading.Thread(target=redact_on_its_own_connection, args=("plain", plain_key))
        plain.start()
        plain.join(timeout=20)
        assert not plain.is_alive() and "plain" in results, "control: a redaction that blanks nothing does not wait"
        blanking = threading.Thread(target=redact_on_its_own_connection, args=("blanking", key),
                                    kwargs={"blank_thread_id": True})
        blanking.start()
        blanking.join(timeout=3.0)
        assert blanking.is_alive() and "blanking" not in results, "a blanking redaction did not wait for the pass"
    blanking.join(timeout=30)
    assert not blanking.is_alive() and results["blanking"]["redacted"][0]["thread_id_blanked"] is True


def test_redaction_blanks_the_stored_edit_call_of_an_edited_version(app_conn, owner_conn):
    """An edited version's text is also held by its EDIT call's stored arguments (skill_calls.args), as a write's is by the
    write call. Before this fix redact.py blanked and post-checked only skill_name = 'memory.write', so after `version: all`
    the edit call still carried the edited text for the call retention while the redaction reported success (found
    2026-10-10 before memory.edit was enabled on production). CONTROL: before the redaction the census finds the text in
    skill_calls.args. MUTANT: the old filter leaves write_calls at 0 and the marker in skill_calls.args."""
    from tests.test_redaction import _call_row, _stored_call
    marker = f"zq-edit-call-{uuid.uuid4().hex}"
    author, sibling, key, value = _author_and_sibling(owner_conn, app_conn, "rdedit")
    edited = dict(value, body=marker)
    _edit(app_conn, author, key, edited, 1)
    edit_call_id = owner_conn.execute(
        "SELECT provenance_jsonb -> 'payload' ->> 'call_id' FROM shared_memory "
        "WHERE namespace = %s AND key = %s AND version = 2", (MESH, key)).fetchone()[0]
    owner_conn.commit()
    assert edit_call_id, "control: the edited version names its edit call"
    _stored_call(owner_conn, author, "memory.edit", edit_call_id,
                 args={"v": 1, "kind": "skill.call", "call_id": edit_call_id,
                       "args": _args(key, edited, 1)},
                 result={"op": "memory.edit", "key": key, "version": 2},
                 payload={"call_id": edit_call_id, "result": {"op": "memory.edit", "key": key}})
    assert "skill_calls.args" in census(owner_conn, marker), "control: the stored edit call holds the edited text"

    out = _redact(app_conn, author, key, version="all")
    assert sorted(r["version"] for r in out["redacted"]) == [1, 2]
    assert out["stored_calls_blanked"]["write_calls"] == 1, out["stored_calls_blanked"]
    row = _call_row(owner_conn, edit_call_id)
    assert row["args"]["args"] == {"namespace": MESH, "key": key, "redacted": True}
    assert row["args"]["call_id"] == edit_call_id and row["args_signature"] == "sig"
    # ABSENT now: the edited text anywhere in the stored calls.
    assert "skill_calls.args" not in census(owner_conn, marker)


def test_an_operator_relay_is_never_edited_and_a_plain_message_of_the_same_voice_still_is(app_conn, owner_conn,
                                                                                          monkeypatch):
    """GitHub Codex on #447: an edit keeps operator_words but writes a new version with a fresh created_at, so a days-old
    relay would read as just written. memory.edit refuses a message that carries operator_words; the CONTROL is the same
    voice editing its plain message through the same door."""
    from apps.memoryskills import validation
    voice = _peer(owner_conn, "ovv")
    _admit(owner_conn, MESH, voice)
    monkeypatch.setattr(validation, "OPERATOR_VOICE_PEERS", frozenset({str(voice["peer_id"])}))
    relay_key = _key(voice["name"])
    relay = dict(_value(voice["name"], "the voice's own text"), operator_words=["merge #446 once its gate passes"])
    _write(app_conn, voice, _msg(MESH, relay_key, relay))
    err = _edit_refused(app_conn, voice, relay_key, dict(relay, body="an edited body"), 1)
    assert err.code == "E_INVALID_ARGS" and "operator relay is not edited" in str(err)
    assert _versions(owner_conn, relay_key) == 1, "nothing was written"
    plain_key = _key(voice["name"])
    plain = _value(voice["name"], "a plain message")
    _write(app_conn, voice, _msg(MESH, plain_key, plain))
    assert _edit(app_conn, voice, plain_key, dict(plain, body="its edit"), 1)["version"] == 2


def test_memory_read_says_whether_an_operator_relay_is_addressed_to_the_caller(app_conn, owner_conn, monkeypatch):
    """pxx's review of #447, F2: whether a relay is addressed to the reader is the server's word, inside the signed
    memory.read result (addressed_to_caller), from the caller's own labels. A seat on the to line gets True; the CONTROL,
    the voice reading its own relay, is not on the to line and gets False; a plain message carries no flag at all."""
    from apps.memoryskills import validation
    from tests.test_edit_db_rule import _author_and_sibling as _pair
    voice, reader, _k, _v = _pair(owner_conn, app_conn, "ovr")
    monkeypatch.setattr(validation, "OPERATOR_VOICE_PEERS", frozenset({str(voice["peer_id"])}))
    key = _key(voice["name"])
    relay = dict(_value(voice["name"], "the voice's own text"), to=[reader["fallback"]],
                 operator_words=["merge #446 once its gate passes"])
    _write(app_conn, voice, _msg(MESH, key, relay))

    def _memo(peer):
        return handlers.dispatch("memory.read", _envelope(peer, "memory.read", {"namespace": MESH, "key": key}),
                                 peer["peer_id"], app_conn)["memo"]
    assert _memo(reader)["addressed_to_caller"] is True
    assert _memo(voice)["addressed_to_caller"] is False
    plain = handlers.dispatch("memory.read", _envelope(voice, "memory.read", {"namespace": MESH, "key": _k}),
                              voice["peer_id"], app_conn)["memo"]
    assert "addressed_to_caller" not in plain


def test_a_non_voice_cannot_store_operator_words_under_any_key(app_conn, owner_conn):
    """pxx's review of #447, F1: mesh-chat write grants cover the whole namespace, so the refusal holds for a non-msg/ key
    too, through the real memory.write. CONTROL: the same seat writes the same key without the field."""
    seat = _peer(owner_conn, "onv")
    _admit(owner_conn, MESH, seat)
    with pytest.raises(HandlerError) as err:
        _write(app_conn, seat, {"namespace": MESH, "key": "note/review-" + uuid.uuid4().hex[:8],
                                "value": {"operator_words": ["deploy"], "body": "x"}})
    assert err.value.code == "E_PERMISSION_DENIED"
    out = _write(app_conn, seat, {"namespace": MESH, "key": "note/review-" + uuid.uuid4().hex[:8], "value": {"body": "x"}})
    assert out.get("version") == 1 or out.get("key")
