"""memory.redact and the rows DERIVED from a row's text (divergence 053; docs/design/redaction.md section 2 rows 5 to 7, step 3,
tests 5 and 7).

THE BUG THIS FILE EXISTS FOR (found 2026-09-30). The handler deleted the derived rows (the lexical terms, the vectors, the
inferred edges and their audits, the related runs and edges) under `rls.server_internal` alone. sql_meshychat/008 makes those
tables' DELETE policies ALSO need `app.projection_writer`, and a DELETE whose policy is false does not raise: it matches zero
rows. So on a real database step 3 removed nothing, and no test noticed because none of them had a derived row to remove (test
6's census found the text only in rows 1, 2 and 4 of the design's section 2, where the design asked for row 5, `mesh_lexical`).

Each derived row is seeded through tests/test_projection_writer.py's own insert for that table, on a real written row, on the
OWNER connection (which bypasses row-level security: the truth, not a view of it). The handler runs as meshychat_app under FORCE RLS,
as a request does. CONTROLS: every seeded row is counted BEFORE (so an empty count after is a deletion and not an insert that
never landed), the routing edge that must STAY is counted after (the redaction deletes what came from the text and not what
came from the routing), and the census finds the marker in `mesh_lexical.lexemes` before and nowhere after.

Run:  .venv313/Scripts/python.exe -m pytest tests/test_redaction_derived.py -q -p no:cacheprovider
"""
from __future__ import annotations

import secrets
import sys
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_memory_skills import (  # noqa: E402,F401
    _call, app_conn, owner_conn,
)
from tests.test_projection_writer import _case, _count  # noqa: E402
from tests.test_redaction import _redact, _world, census  # noqa: E402

MARKER = "zqderived" + secrets.token_hex(6)
#: What comes from the TEXT and must go: every derived table that names a row and holds something read out of its body.
DELETED = ("mesh_embeddings", "mesh_inferred_edges", "mesh_inferred_audits", "mesh_lexical",
           "mesh_related_edges", "mesh_related_runs")
#: What comes from the ROUTING and must stay (`authored_by` is the authenticated author, a fact about who sent it).
KEPT = ("mesh_edges",)


def _msg_key(tag: str) -> str:
    """A mail-shaped key (`msg/<epoch-ms>-<label>-<hex>`): mesh_related_edges' CHECK wants msg/ keys at both ends, and a msg/ row
    in a scratch namespace that names no sender claims nothing (validation.claims_mesh_sender)."""
    return f"msg/{int(time.time() * 1000) + secrets.randbelow(1000)}-{tag}-{secrets.token_hex(3)}"


def _write_msg(app_conn, peer, ns, key, body, **extra):
    value = {"body": body, "to": ["b-seat"], "type": "status", **extra}
    _call(app_conn, peer, "memory.write", {"namespace": ns, "key": key, "value": value})


def _row_id(owner_conn, ns, key):
    with owner_conn.cursor() as cur:
        cur.execute("SELECT id FROM shared_memory WHERE namespace = %s AND key = %s AND is_head", (ns, key))
        (rid,) = cur.fetchone()
    owner_conn.commit()
    return rid


def _seed(owner_conn, older: dict, newer: dict) -> dict:
    """One derived row per table for the OLDER row (the related edge runs newer to older, the newer row being the source), each
    counted before the redaction. The lexical row carries the marker as a lexeme, as the job's would carry the body's terms."""
    seeded = {}
    for table in DELETED + KEPT:
        case = _case(table, {**older, "newer_id": newer["id"], "newer_key": newer["key"]})
        if table == "mesh_lexical":
            case["insert"] = case["insert"].replace("'{xx}'", "%s::text[]")
            case["params"] = (case["params"][0], case["params"][1], case["params"][2], f"{{{MARKER}}}")
        with owner_conn.cursor() as cur:
            cur.execute(case["insert"], case["params"])
        owner_conn.commit()
        assert _count(owner_conn, table, case) == 1, f"control: the {table} row was not seeded"
        seeded[table] = case
    return seeded


def test_5_the_derived_rows_of_a_redacted_row_are_deleted_and_the_routing_edge_stays(app_conn, owner_conn):
    author, sibling, stranger, ns = _world(app_conn, owner_conn, "rd5")
    older_key, newer_key = _msg_key("a"), _msg_key("a")
    _write_msg(app_conn, author, ns, older_key, f"text {MARKER} text")
    _write_msg(app_conn, author, ns, newer_key, "a later message", in_reply_to=older_key)
    older = {"id": _row_id(owner_conn, ns, older_key), "key": older_key}
    newer = {"id": _row_id(owner_conn, ns, newer_key), "key": newer_key}
    seeded = _seed(owner_conn, older, newer)

    assert "mesh_lexical.lexemes" in census(owner_conn, MARKER), "control: the census finds the term where the design says it must"

    _redact(app_conn, author, ns, older_key)

    for table in DELETED:
        assert _count(owner_conn, table, seeded[table]) == 0, f"{table} still holds a row derived from the redacted text"
    for table in KEPT:
        assert _count(owner_conn, table, seeded[table]) == 1, f"{table}: the routing edge was deleted with the text"
    assert census(owner_conn, MARKER) == [], "the marker is still somewhere"


def test_5_a_row_of_another_key_keeps_its_derived_rows(app_conn, owner_conn):
    """The deletes are by the redacted row's id and no other's: the newer row, which is not redacted, keeps a derived row of its own,
    except the related edge, which names the redacted row at one end and goes."""
    author, sibling, stranger, ns = _world(app_conn, owner_conn, "rd5b")
    older_key, newer_key = _msg_key("a"), _msg_key("a")
    _write_msg(app_conn, author, ns, older_key, "older")
    _write_msg(app_conn, author, ns, newer_key, "newer")
    older = {"id": _row_id(owner_conn, ns, older_key), "key": older_key}
    newer = {"id": _row_id(owner_conn, ns, newer_key), "key": newer_key}
    _seed(owner_conn, older, newer)
    others = {}
    for table in ("mesh_embeddings", "mesh_lexical", "mesh_inferred_audits"):
        case = _case(table, {**newer, "newer_id": newer["id"], "newer_key": newer["key"]})
        with owner_conn.cursor() as cur:
            cur.execute(case["insert"], case["params"])
        owner_conn.commit()
        others[table] = case
    _redact(app_conn, author, ns, older_key)
    for table, case in others.items():
        assert _count(owner_conn, table, case) == 1, f"{table}: a row of an UNREDACTED key was deleted"


def test_7_the_reply_to_a_redacted_row_still_resolves(app_conn, owner_conn):
    """Design test 7: `in_reply_to` of a reply still names the redacted key, the reply is untouched, and the reply's own declared
    edge to it (`replies_to`, from the routing field) is kept."""
    author, sibling, stranger, ns = _world(app_conn, owner_conn, "rd7")
    older_key, newer_key = _msg_key("a"), _msg_key("a")
    _write_msg(app_conn, author, ns, older_key, f"text {MARKER}")
    _write_msg(app_conn, author, ns, newer_key, "the reply", in_reply_to=older_key)
    reply_id = _row_id(owner_conn, ns, newer_key)
    tag = f"rd7-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute("INSERT INTO mesh_edges (src_id, src_key, src_version, edge_type, dst, src_field, recorded_at) "
                    "VALUES (%s, %s, 1, 'replies_to', %s, %s, now())", (reply_id, newer_key, older_key, tag))
    owner_conn.commit()

    _redact(app_conn, author, ns, older_key)

    with owner_conn.cursor() as cur:
        cur.execute("SELECT value ->> 'in_reply_to' FROM shared_memory WHERE id = %s", (reply_id,))
        assert cur.fetchone()[0] == older_key, "the reply lost its in_reply_to"
        cur.execute("SELECT count(*) FROM mesh_edges WHERE src_field = %s AND dst = %s", (tag, older_key))
        assert cur.fetchone()[0] == 1, "the reply's declared edge to the redacted row was deleted"
        cur.execute("SELECT value ->> 'redacted' FROM shared_memory WHERE namespace = %s AND key = %s", (ns, older_key))
        assert cur.fetchone()[0] == "true", "control: the row it names IS redacted"
    owner_conn.commit()


def test_a_delete_a_policy_filtered_ends_the_call_instead_of_answering_success(app_conn, owner_conn):
    """The postcondition. Called from a context that lacks `app.projection_writer` (a plain server_internal request, which is
    exactly where the first build ran), `delete_derived` finds the rows still there and refuses; called from the writer context it
    deletes them. So the bug cannot come back as a SILENT one: a redaction that leaves the text findable is an error, and the
    transaction, tombstone included, is rolled back."""
    from apps.core import rls
    from apps.memoryskills import redact
    from apps.memoryskills.errors import HandlerError

    author, sibling, stranger, ns = _world(app_conn, owner_conn, "rd5c")
    older_key, newer_key = _msg_key("a"), _msg_key("a")
    _write_msg(app_conn, author, ns, older_key, f"text {MARKER}")
    _write_msg(app_conn, author, ns, newer_key, "later")
    older = {"id": _row_id(owner_conn, ns, older_key), "key": older_key}
    newer = {"id": _row_id(owner_conn, ns, newer_key), "key": newer_key}
    seeded = _seed(owner_conn, older, newer)

    with pytest.raises(HandlerError, match="still there after the delete"):
        with rls.server_internal(app_conn) as c, c.cursor() as cur:
            redact.delete_derived(cur, older["id"])
    assert all(_count(owner_conn, t, seeded[t]) == 1 for t in DELETED), "the refused call changed something"

    with rls.projection_writer(app_conn) as c, c.cursor() as cur:
        redact.delete_derived(cur, older["id"])            # control: the writer context deletes them
    assert all(_count(owner_conn, t, seeded[t]) == 0 for t in DELETED)


# ------------------------------------------------------------------------- the review of #32 at a55a06b (memory-bxxxxx, 01:55Z)

def _thread_edge(owner_conn, row_id, key, thread):
    """What the edge projection writes for a row (apps/mesh/projection.py _INSERT): an `in_thread` edge whose DESTINATION is the
    thread id."""
    with owner_conn.cursor() as cur:
        cur.execute("INSERT INTO mesh_edges (src_id, src_key, src_version, edge_type, dst, src_field, recorded_at) "
                    "VALUES (%s, %s, 1, 'in_thread', %s, 'thread_id', now())", (row_id, key, thread))
    owner_conn.commit()


def test_blank_thread_id_removes_the_thread_text_from_the_edge_projection_and_only_this_rows_edge(app_conn, owner_conn):
    """Blocking 1 of the review (memory-bxxxxx's probe). `mesh_edges` is kept on purpose, but its `in_thread` edge holds the thread id
    as its destination, and the thread id is the one routing field that can be text. After `blank_thread_id` the census must not
    find it. CONTROLS: the census finds the seeded edge before; the OTHER row of the thread keeps its own edge; and a redaction
    WITHOUT blank_thread_id keeps the edge (the thread stays, by default, for grouping)."""
    author, sibling, stranger, ns = _world(app_conn, owner_conn, "rdthread")
    thread = f"t/zqthread{secrets.token_hex(5)}"
    k1, k2, k3 = _msg_key("a"), _msg_key("a"), _msg_key("a")
    for k in (k1, k2, k3):
        _write_msg(app_conn, author, ns, k, "plain body", thread_id=thread)
    ids = {k: _row_id(owner_conn, ns, k) for k in (k1, k2, k3)}
    for k in (k1, k2, k3):
        _thread_edge(owner_conn, ids[k], k, thread)
    assert "mesh_edges.dst" in census(owner_conn, thread.split("/")[1]), "control: the census finds the seeded edge"

    _redact(app_conn, author, ns, k3)                                  # no blanking: the thread stays for grouping
    with owner_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM mesh_edges WHERE src_id = %s AND edge_type = 'in_thread'", (ids[k3],))
        assert cur.fetchone()[0] == 1, "a redaction without blank_thread_id took the thread edge"
    owner_conn.commit()

    out = _redact(app_conn, author, ns, k1, blank_thread_id=True)["redacted"][0]
    assert out["thread_id_blanked"] is True
    with owner_conn.cursor() as cur:
        cur.execute("SELECT src_id FROM mesh_edges WHERE edge_type = 'in_thread' AND dst = %s ORDER BY src_id", (thread,))
        left = [r[0] for r in cur.fetchall()]
    owner_conn.commit()
    assert sorted(left, key=str) == sorted([ids[k2], ids[k3]], key=str), "only the blanked row's thread edge should have gone"


def test_the_stored_write_and_read_blanking_refuse_when_a_policy_filtered_them(app_conn, owner_conn):
    """Point 4. An UPDATE a row-level policy filters matches zero rows and raises nothing, so the blanking of the stored calls and
    the tombstone's own UPDATE end in a postcondition, as delete_derived does. Here a cursor that reports what a filtered statement
    would (one that changed nothing, and a row still there) meets each guard; a true one passes."""
    from apps.memoryskills import redact
    from apps.memoryskills.errors import HandlerError

    class Scripted:
        """Answers like the statements of `_blank_stored_calls`, in order: the write UPDATE, a walk of one page of the unpruned
        reads with its UPDATE, then the two postcondition SELECTs (the second behind its own walk of that page)."""

        def __init__(self, leftovers):
            self.leftovers = list(leftovers)
            self.rowcount = 0
            self.selects = 0

        def execute(self, sql, params=None):
            self.last = sql
            self.rowcount = 0 if sql.lstrip().startswith("UPDATE") else -1

        def fetchall(self):
            assert "ORDER BY completed_at, id LIMIT" in self.last, self.last            # only the walk reads a page
            return [("00000000-0000-0000-0000-000000000001", None)]

        def fetchone(self):
            self.selects += 1
            return (1,) if self.leftovers.pop(0) else None

    with pytest.raises(HandlerError, match="stored write call.*still carries its value"):
        redact._blank_stored_calls(Scripted([True]), "ns", "msg/1-a-000001", ["call-1"])
    with pytest.raises(HandlerError, match="stored read result.*still there"):
        redact._blank_stored_calls(Scripted([False, True]), "ns", "msg/1-a-000001", ["call-1"])
    assert redact._blank_stored_calls(Scripted([False, False]), "ns", "msg/1-a-000001", ["call-1"]) == {
        "write_calls": 0, "read_results": 0}                                                    # control: nothing left, it answers


def test_a_tombstone_update_that_changed_no_row_refuses_the_whole_call(app_conn, owner_conn):
    """The tombstone's UPDATE is checked for exactly one row changed. A cursor wrapper reports zero for that statement (what a
    filtering policy would do) and the call is refused."""
    from apps.core import rls
    from apps.memoryskills import redact
    from apps.memoryskills.errors import HandlerError
    from psycopg.rows import dict_row

    author, sibling, stranger, ns = _world(app_conn, owner_conn, "rdcount")
    key = _msg_key("a")
    _write_msg(app_conn, author, ns, key, "plain body")

    class ZeroRows:
        def __init__(self, cur):
            self.cur = cur
            self._zero = False

        def execute(self, sql, params=None):
            self.cur.execute(sql, params)
            self._zero = sql.lstrip().startswith("UPDATE shared_memory SET value")

        @property
        def rowcount(self):
            return 0 if self._zero else self.cur.rowcount

        def __getattr__(self, name):
            return getattr(self.cur, name)

    with pytest.raises(HandlerError, match="changed 0 rows, not one"):
        with rls.projection_writer(app_conn), app_conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT id, version, value, signed_bytes, payload_jcs_sha256, created_by_peer_id FROM shared_memory "
                        "WHERE namespace = %s AND key = %s", (ns, key))
            found = cur.fetchone()
            redact._redact_one(ZeroRows(cur), found, ns, key, author["peer_id"], author["owner_id"], "sent_in_error", False,
                               b"req", "call-zero", [])
    assert owner_conn.execute("SELECT value ->> 'body' FROM shared_memory WHERE namespace = %s AND key = %s", (ns, key)
                              ).fetchone()[0] == "plain body", "the refused call left a tombstone behind"
    owner_conn.commit()


def test_the_record_and_the_tombstone_render_the_instant_in_utc_whatever_the_sessions_zone(app_conn, owner_conn):
    """Point 8. psycopg returns a timestamptz in the SESSION's zone, so `isoformat()` alone gave `03:50:20+02:00` on a database that
    runs Europe/Berlin, and the stored tombstone's `redacted_at` differed by the server's setting for one instant, while the event
    frame (`to_char(... AT TIME ZONE 'UTC', ...Z)`) is UTC. Run in a Berlin session: the result and the tombstone are UTC with a Z, and
    they name the instant the record holds."""
    import datetime as dt

    author, sibling, stranger, ns = _world(app_conn, owner_conn, "rdzone")
    key = _msg_key("a")
    _write_msg(app_conn, author, ns, key, "plain body")
    app_conn.execute("SET TIME ZONE 'Europe/Berlin'")
    app_conn.commit()
    try:
        out = _redact(app_conn, author, ns, key)["redacted"][0]
    finally:
        app_conn.execute("SET TIME ZONE 'UTC'")
        app_conn.commit()
    assert out["redacted_at"].endswith("Z") and "+" not in out["redacted_at"], out["redacted_at"]
    stored = owner_conn.execute("SELECT value ->> 'redacted_at' FROM shared_memory WHERE namespace = %s AND key = %s",
                                (ns, key)).fetchone()[0]
    record_at = owner_conn.execute("SELECT redacted_at FROM shared_memory_redaction WHERE namespace = %s AND key = %s",
                                   (ns, key)).fetchone()[0]
    owner_conn.commit()
    assert stored == out["redacted_at"], (stored, out["redacted_at"])
    assert dt.datetime.fromisoformat(stored.replace("Z", "+00:00")) == record_at.astimezone(dt.timezone.utc)
