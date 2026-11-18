"""Rule 3 of the file access check and the pickup receipt (issue #118; apps/files/grants.py).

The operator's ruling of 2026-10-12: keep the owner-wide rule, ADD that a peer a mail message addresses may download
the files it attaches, and record each download. Proved through the REAL memory.write and memory.redact handlers
(signed envelopes, meshychat_app under FORCE RLS) and the REAL /v1/files routes, against the migrated Postgres.

Every refusal sits beside its positive control in the same test: the recipient that is refused before the message is
the one admitted after it, and the bystander refused after it is refused by the same route that admits the recipient.

Run: .venv/Scripts/python.exe -m pytest tests/test_files_message_grants.py -q -p no:cacheprovider
"""
from __future__ import annotations

import datetime as _dt
import json
import uuid

import psycopg
import pytest

# The file-route harness (blob backend, JWT key, real urlconf) and its upload helper. `files_env` is autouse there and
# is applied here by importing it.
from tests.test_files import (DSN, _auth, _seed_call, _seed_skill, _upload_and_finalize, client,  # noqa: F401
                              files_env)
from tests.test_mesh_authorship import _admit, _envelope, _key, _peer

from apps.files import grants, service  # noqa: E402
from apps.files import db as filesdb  # noqa: E402
from apps.memoryskills import db as memdb, handlers  # noqa: E402

MESH = "shared/mesh-chat"


@pytest.fixture()
def seed_conn():
    """Role `meshy`, autocommit: seeding and observing only (bypasses RLS)."""
    with psycopg.connect(DSN, autocommit=True) as conn:
        yield conn


@pytest.fixture()
def write_conn():
    """The role memory.write runs as: meshychat_app under FORCE RLS."""
    conn = memdb.connect(DSN)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def no_roster(monkeypatch):
    """Labels are peers.name and peer-<id8> only, whatever roster another test loaded."""
    monkeypatch.setattr(handlers, "_ROSTER_LABELS", {})


def _seat(seed_conn, tag, **kw):
    p = _peer(seed_conn, tag, **kw)
    _admit(seed_conn, MESH, p)
    return p


def _send(write_conn, sender, *, to, cc=(), attach=(), key=None):
    """A real signed memory.write of a mail message attaching `attach` [(file_id, sha256, size)]. Returns its key.
    With `key`, a later version of that message (028 R4: its author may write one)."""
    key = key or _key(sender["name"])
    value = {"schema_version": 1, "to": list(to), "from": sender["name"], "type": "status",
             "thread_id": "files/118", "ts": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "body": "synthetic attachment probe", "author_family": "claude"}
    if cc:
        value["cc"] = list(cc)
    if attach:
        value["attachments"] = [{"file_id": f, "name": "note.txt", "size_bytes": n, "sha256": s} for f, s, n in attach]
    handlers.dispatch("memory.write", _envelope(sender, "memory.write",
                                                {"namespace": MESH, "key": key, "value": value}),
                      sender["peer_id"], write_conn)
    return key


def _redact(write_conn, peer, key):
    handlers.dispatch("memory.redact", _envelope(peer, "memory.redact",
                                                 {"namespace": MESH, "key": key, "reason_code": "sent_in_error"}),
                      peer["peer_id"], write_conn)


def _grants(seed_conn, file_id) -> list[tuple[str, str]]:
    return [(str(p), k) for p, k in seed_conn.execute(
        "SELECT peer_id, message_key FROM file_message_grants WHERE file_id = %s ORDER BY 1, 2", (file_id,))]


def _url(client, peer, fid):
    return client.get(f"/v1/files/{fid}/url", **_auth(peer["peer_id"]))


def _file(client, owner, payload=b"bytes for a recipient"):
    fid, view, sha = _upload_and_finalize(client, owner["peer_id"], payload)
    return fid, sha, len(payload)


# ----------------------------------------------------------------------------------------------------------- rule 3

def test_a_recipient_of_another_owner_downloads_and_a_bystander_does_not(seed_conn, write_conn, client):
    s, r, x = _seat(seed_conn, "s"), _seat(seed_conn, "r"), _seat(seed_conn, "x")
    fid, sha, n = _file(client, s)
    assert _url(client, r, fid).status_code == 404, "before the message the recipient is a stranger"
    key = _send(write_conn, s, to=[r["name"]], attach=[(fid, sha, n)])
    assert _grants(seed_conn, fid) == [(str(r["peer_id"]), key)]
    ok = _url(client, r, fid)
    assert ok.status_code == 200, ok.content
    assert _url(client, x, fid).status_code == 404, "a peer the message does not address stays refused"
    # Metadata follows the download rule; DELETE stays owner-only.
    assert client.get(f"/v1/files/{fid}", **_auth(r["peer_id"])).status_code == 200
    assert client.delete(f"/v1/files/{fid}", **_auth(r["peer_id"])).status_code == 404


def test_a_cc_recipient_and_a_peer_id_label_are_addressed_too(seed_conn, write_conn, client):
    s, r, c = _seat(seed_conn, "s2"), _seat(seed_conn, "r2"), _seat(seed_conn, "c2")
    fid, sha, n = _file(client, s)
    _send(write_conn, s, to=[r["fallback"]], cc=[c["name"]], attach=[(fid, sha, n)])
    assert _url(client, r, fid).status_code == 200
    assert _url(client, c, fid).status_code == 200


def test_a_message_cannot_mint_access_to_a_file_its_sender_cannot_read(seed_conn, write_conn, client):
    """The #122 lesson: attaching a guessed id must not hand the bytes to whoever the message addresses."""
    owner, m, r = _seat(seed_conn, "own"), _seat(seed_conn, "m"), _seat(seed_conn, "r3")
    fid, sha, n = _file(client, owner)
    _send(write_conn, m, to=[r["name"]], attach=[(fid, sha, n)])
    assert _grants(seed_conn, fid) == []
    assert _url(client, r, fid).status_code == 404
    # Control, one variable apart: the same message from the file's owner does grant.
    _send(write_conn, owner, to=[r["name"]], attach=[(fid, sha, n)])
    assert _url(client, r, fid).status_code == 200


@pytest.mark.parametrize("role", ["caller", "target"])
def test_a_call_participant_who_is_not_the_owner_may_attach_the_file(seed_conn, write_conn, client, role):
    """Rule 2 (grants.sender_may_read): a sender who took part in a skill call that references the file may read it,
    so its message grants the file, though it is not the file's owner. Nothing in this file put a `skill_calls` row in
    before (REFACTOR-QUEUE-2026-11 A8), so a mutant of rule 2's predicate passed the suite. Both sides of the call are
    covered: dropping the caller test or the target test fails one case each."""
    owner, m, r = _seat(seed_conn, f"own-{role}"), _seat(seed_conn, f"m-{role}"), _seat(seed_conn, f"r-{role}")
    fid, sha, n = _file(client, owner)
    # Control first, one variable apart: with no call, `m` is a stranger to the file and grants nothing.
    _send(write_conn, m, to=[r["name"]], attach=[(fid, sha, n)])
    assert _grants(seed_conn, fid) == []
    assert _url(client, r, fid).status_code == 404
    caller, target = (m, owner) if role == "caller" else (owner, m)
    skill_id = _seed_skill(seed_conn, target["peer_id"])
    _seed_call(seed_conn, caller=caller, target=target, file_id=fid, status="completed", skill_id=skill_id)
    key = _send(write_conn, m, to=[r["name"]], attach=[(fid, sha, n)])
    assert _grants(seed_conn, fid) == [(str(r["peer_id"]), key)]
    assert _url(client, r, fid).status_code == 200


def test_a_recipient_cannot_pass_the_file_on(seed_conn, write_conn, client):
    """Rule 3 does not count for a sender, so a grant is not transitive."""
    s, r, q = _seat(seed_conn, "s4"), _seat(seed_conn, "r4"), _seat(seed_conn, "q4")
    fid, sha, n = _file(client, s)
    _send(write_conn, s, to=[r["name"]], attach=[(fid, sha, n)])
    assert _url(client, r, fid).status_code == 200
    _send(write_conn, r, to=[q["name"]], attach=[(fid, sha, n)])
    assert _url(client, q, fid).status_code == 404


def test_a_broadcast_grants_nothing(seed_conn, write_conn, client, monkeypatch):
    s, x = _seat(seed_conn, "s5"), _seat(seed_conn, "x5")
    # A seat that ANSWERS to "all" and "both" (a roster can hand out any label), so the resolver alone would reach it:
    # only the broadcast filter keeps the grant out. Without this, "all" resolves to nobody and the test proves nothing.
    y = _seat(seed_conn, "y5")
    monkeypatch.setattr(handlers, "_ROSTER_LABELS", {str(x["peer_id"]): "all", str(y["peer_id"]): "both"})
    from apps.mesh.views import peers_answering
    assert peers_answering(seed_conn, "all") == [str(x["peer_id"])]
    fid, sha, n = _file(client, s)
    _send(write_conn, s, to=["all"], attach=[(fid, sha, n)])
    _send(write_conn, s, to=["both"], attach=[(fid, sha, n)])
    assert _grants(seed_conn, fid) == []
    assert _url(client, x, fid).status_code == 404
    # Control: x addressed by name in the same shape is admitted.
    _send(write_conn, s, to=["all", x["name"]], attach=[(fid, sha, n)])
    assert _url(client, x, fid).status_code == 200


def test_a_redaction_withdraws_the_grant(seed_conn, write_conn, client):
    s, r = _seat(seed_conn, "s6"), _seat(seed_conn, "r6")
    fid, sha, n = _file(client, s)
    key = _send(write_conn, s, to=[r["name"]], attach=[(fid, sha, n)])
    assert _url(client, r, fid).status_code == 200
    _redact(write_conn, s, key)
    assert _grants(seed_conn, fid) == []
    assert _url(client, r, fid).status_code == 404


def test_rule_3_is_not_part_of_call_admission(seed_conn, write_conn, client):
    """readable_row is what call admission asks (apps/calls/pipeline.py, #122): a recipient must not be able to attach
    the file to a call and so reach a call target."""
    s, r = _seat(seed_conn, "s7"), _seat(seed_conn, "r7")
    fid, sha, n = _file(client, s)
    _send(write_conn, s, to=[r["name"]], attach=[(fid, sha, n)])
    assert _url(client, r, fid).status_code == 200
    with filesdb.connection() as conn:
        assert service.readable_row(conn, fid, r["peer_id"], r["owner_id"]) is None
        assert service.readable_row(conn, fid, s["peer_id"], s["owner_id"]) is not None


# ------------------------------------------------------------------------------------------------------- receipts

def test_each_presign_leaves_a_receipt_the_owner_can_read(seed_conn, write_conn, client):
    s, r = _seat(seed_conn, "s8"), _seat(seed_conn, "r8")
    fid, sha, n = _file(client, s)
    key = _send(write_conn, s, to=[r["name"]], attach=[(fid, sha, n)])
    assert _url(client, r, fid).status_code == 200
    assert _url(client, s, fid).status_code == 200
    client.get(f"/v1/files/{fid}", **_auth(r["peer_id"]))          # metadata is not a pickup
    got = client.get(f"/v1/files/{fid}/downloads", **_auth(s["peer_id"]))
    assert got.status_code == 200, got.content
    rows = got.json()["downloads"]
    assert [(d["peer_id"], d["rule"], d["sha256"], d["message_key"]) for d in rows] == [
        (str(s["peer_id"]), grants.RULE_OWNER, sha, None), (str(r["peer_id"]), grants.RULE_MESSAGE, sha, key)]


def test_a_presign_retry_within_the_window_writes_one_receipt(seed_conn, write_conn, client):
    s, r = _seat(seed_conn, "s12"), _seat(seed_conn, "r12")
    fid, sha, n = _file(client, s)
    _send(write_conn, s, to=[r["name"]], attach=[(fid, sha, n)])
    for _ in range(3):
        assert _url(client, r, fid).status_code == 200
    assert _url(client, s, fid).status_code == 200                  # control: another reader gets its own row
    rows = client.get(f"/v1/files/{fid}/downloads", **_auth(s["peer_id"])).json()["downloads"]
    assert sorted(d["peer_id"] for d in rows) == sorted([str(r["peer_id"]), str(s["peer_id"])])


def test_the_receipts_are_the_owners_alone(seed_conn, write_conn, client):
    s, r = _seat(seed_conn, "s9"), _seat(seed_conn, "r9")
    fid, sha, n = _file(client, s)
    _send(write_conn, s, to=[r["name"]], attach=[(fid, sha, n)])
    assert _url(client, r, fid).status_code == 200                  # r may read the file...
    assert client.get(f"/v1/files/{fid}/downloads", **_auth(r["peer_id"])).status_code == 404   # ...not its receipts
    assert client.get(f"/v1/files/{fid}/downloads", **_auth(s["peer_id"])).status_code == 200   # control


def test_a_refused_download_leaves_no_receipt(seed_conn, client):
    s, x = _seat(seed_conn, "s10"), _seat(seed_conn, "x10")
    fid, sha, n = _file(client, s)
    assert _url(client, x, fid).status_code == 404
    assert seed_conn.execute("SELECT count(*) FROM file_downloads WHERE file_id = %s", (fid,)).fetchone()[0] == 0


# ------------------------------------------------------------------------------------------ later versions

def _edit(write_conn, peer, key, value):
    handlers.dispatch("memory.edit", _envelope(peer, "memory.edit",
                                               {"namespace": MESH, "key": key, "expected_head_version": 1,
                                                "value": value}),
                      peer["peer_id"], write_conn)


def _head_value(seed_conn, key):
    return dict(seed_conn.execute("SELECT value FROM shared_memory WHERE namespace = %s AND key = %s AND is_head",
                                  (MESH, key)).fetchone()[0])


def test_who_a_message_grants_cannot_change_after_it_is_sent(seed_conn, write_conn, client):
    """pxx's review asked for an edit that drops a recipient. There is none to handle: a later version through
    memory.write is refused (059, E_USE_MEMORY_EDIT) and memory.edit refuses any change but `body`, so the grant
    computed at version 1 stays exactly right."""
    s, r, x = _seat(seed_conn, "s13"), _seat(seed_conn, "r13"), _seat(seed_conn, "x13")
    fid, sha, n = _file(client, s)
    key = _send(write_conn, s, to=[r["name"]], attach=[(fid, sha, n)])
    with pytest.raises(handlers.HandlerError) as rewrite:
        _send(write_conn, s, to=[x["name"]], attach=[(fid, sha, n)], key=key)
    assert rewrite.value.code == "E_USE_MEMORY_EDIT"
    moved = dict(_head_value(seed_conn, key), to=[x["name"]])
    with pytest.raises(handlers.HandlerError):
        _edit(write_conn, s, key, moved)
    dropped = {k: v for k, v in _head_value(seed_conn, key).items() if k != "attachments"}
    with pytest.raises(handlers.HandlerError):
        _edit(write_conn, s, key, dropped)
    assert _grants(seed_conn, fid) == [(str(r["peer_id"]), key)]
    assert _url(client, r, fid).status_code == 200
    assert _url(client, x, fid).status_code == 404


def test_a_body_edit_keeps_the_grant(seed_conn, write_conn, client):
    s, r = _seat(seed_conn, "s14"), _seat(seed_conn, "r14")
    fid, sha, n = _file(client, s)
    key = _send(write_conn, s, to=[r["name"]], attach=[(fid, sha, n)])
    _edit(write_conn, s, key, dict(_head_value(seed_conn, key), body="synthetic attachment probe, corrected"))
    assert _head_value(seed_conn, key)["body"].endswith("corrected")    # control: the edit landed
    assert _grants(seed_conn, fid) == [(str(r["peer_id"]), key)]
    assert _url(client, r, fid).status_code == 200


def test_redacting_one_version_keeps_grants_while_an_unredacted_version_remains(seed_conn, write_conn, client):
    """Issue #320: redacting version 1 of an edited message (v1 + v2) must not delete the file_message_grants
    that version 2 (the unredacted head) still gives; redacting version 2 afterwards withdraws the grant."""
    s, r = _seat(seed_conn, "s14b"), _seat(seed_conn, "r14b")
    fid, sha, n = _file(client, s)
    key = _send(write_conn, s, to=[r["name"]], attach=[(fid, sha, n)])
    _edit(write_conn, s, key, dict(_head_value(seed_conn, key), body="synthetic attachment probe, v2"))

    # Redact version 1 only; version 2 is still unredacted and attaches fid.
    handlers.dispatch("memory.redact", _envelope(s, "memory.redact",
                                                 {"namespace": MESH, "key": key, "version": 1,
                                                  "reason_code": "sent_in_error"}),
                      s["peer_id"], write_conn)
    assert _grants(seed_conn, fid) == [(str(r["peer_id"]), key)]
    assert _url(client, r, fid).status_code == 200

    # Control: once version 2 is also redacted, no unredacted version attaches fid and the grant is withdrawn.
    handlers.dispatch("memory.redact", _envelope(s, "memory.redact",
                                                 {"namespace": MESH, "key": key, "version": 2,
                                                  "reason_code": "sent_in_error"}),
                      s["peer_id"], write_conn)
    assert _grants(seed_conn, fid) == []
    assert _url(client, r, fid).status_code == 404


def test_concurrent_redaction_of_final_two_versions_withdraws_grant(seed_conn, write_conn, client, monkeypatch):
    """P1 review finding on #336: when the final two live versions (v1 and v2) are redacted concurrently on two
    connections under READ COMMITTED, locking all versions of (namespace, key) in ORDER BY version FOR UPDATE
    serializes the two redactions so the second one sees the first tombstone and withdraws the grant."""
    import threading
    from apps.memoryskills import redact as redact_mod

    s, r = _seat(seed_conn, "s14c"), _seat(seed_conn, "r14c")
    fid, sha, n = _file(client, s)
    key = _send(write_conn, s, to=[r["name"]], attach=[(fid, sha, n)])
    _edit(write_conn, s, key, dict(_head_value(seed_conn, key), body="synthetic attachment probe, v2"))
    assert _grants(seed_conn, fid) == [(str(r["peer_id"]), key)]

    barrier = threading.Barrier(2, timeout=10.0)
    real_derive_owner_id = redact_mod.derive_owner_id

    def synced_derive_owner_id(cur, peer_id):
        owner = real_derive_owner_id(cur, peer_id)
        barrier.wait()
        return owner

    monkeypatch.setattr(redact_mod, "derive_owner_id", synced_derive_owner_id)

    errors = []

    def redact_version(ver: int):
        conn = memdb.connect(DSN)
        try:
            handlers.dispatch(
                "memory.redact",
                _envelope(s, "memory.redact",
                          {"namespace": MESH, "key": key, "version": ver, "reason_code": "sent_in_error"}),
                s["peer_id"],
                conn,
            )
        except BaseException as exc:  # pragma: no cover
            errors.append((ver, exc))
        finally:
            conn.close()

    t1 = threading.Thread(target=redact_version, args=(1,))
    t2 = threading.Thread(target=redact_version, args=(2,))
    t1.start()
    t2.start()
    t1.join(timeout=15.0)
    t2.join(timeout=15.0)
    assert errors == [], errors
    assert _grants(seed_conn, fid) == []
    assert _url(client, r, fid).status_code == 404



# ------------------------------------------------------------------------------------------------------------ RLS

def test_the_new_tables_answer_nothing_outside_server_internal(seed_conn, write_conn, client):
    s, r = _seat(seed_conn, "s11"), _seat(seed_conn, "r11")
    fid, sha, n = _file(client, s)
    _send(write_conn, s, to=[r["name"]], attach=[(fid, sha, n)])
    assert _url(client, r, fid).status_code == 200
    assert len(_grants(seed_conn, fid)) == 1                          # positive control: the row exists
    with filesdb.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM file_message_grants WHERE file_id = %s", (fid,))
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT count(*) FROM file_downloads WHERE file_id = %s", (fid,))
            assert cur.fetchone()[0] == 0


def test_the_rule_3_probe_uses_the_primary_key(seed_conn):
    """An index CAN serve the probe (a tiny test table may still be scanned with seqscan on, so it is turned off)."""
    seed_conn.execute("SET enable_seqscan = off")
    plan = "\n".join(r[0] for r in seed_conn.execute(
        "EXPLAIN SELECT EXISTS (SELECT 1 FROM file_message_grants WHERE file_id = %s AND peer_id = %s)",
        (str(uuid.uuid4()), str(uuid.uuid4()))))
    assert "file_message_grants_pkey" in plan, plan


def test_the_probes_use_their_indexes_as_meshy_app_under_the_policy():
    """memory-bxxxxx's review: the plan that matters is meshychat_app's under FORCE RLS (041's lesson: a lookup that used
    its index as `meshy` took 27 to 32 s as `meshy_app`, now `meshychat_app`). Both request-path reads, rule 3 and the receipt window."""
    from apps.core import rls
    with filesdb.connection() as conn:
        with rls.server_internal(conn):
            with conn.cursor() as cur:
                cur.execute("SELECT current_user, (SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user)")
                assert cur.fetchone()[1] is False                      # the control: RLS applies to this role
                cur.execute("SET LOCAL enable_seqscan = off")
                cur.execute("EXPLAIN SELECT message_key FROM public.file_message_grants WHERE file_id = %s "
                            "AND peer_id = %s ORDER BY created_at, message_key LIMIT 1",
                            (str(uuid.uuid4()), str(uuid.uuid4())))
                plan = "\n".join(r[0] for r in cur.fetchall())
                assert "file_message_grants_pkey" in plan, plan
                cur.execute("EXPLAIN SELECT 1 FROM public.file_downloads WHERE file_id = %s AND peer_id = %s "
                            "AND rule = 3 AND presigned_at > now() - make_interval(secs => 60)",
                            (str(uuid.uuid4()), str(uuid.uuid4())))
                plan = "\n".join(r[0] for r in cur.fetchall())
                assert "file_downloads_file_idx" in plan, plan


def test_both_tables_force_row_level_security(seed_conn):
    """The meshychat_app read above would also pass with FORCE removed (pxx's review, P2): read the flags themselves."""
    rows = dict((r[0], (r[1], r[2])) for r in seed_conn.execute(
        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
        "WHERE relname IN ('file_message_grants', 'file_downloads') AND relkind = 'r'"))
    assert rows == {"file_message_grants": (True, True), "file_downloads": (True, True)}, rows


def _bound(conn):
    from apps.core import rls
    return rls.server_internal(conn)


# ------------------------------------------------------------------------------- scope and bounds (pxx's review)

def test_a_key_that_is_not_a_message_grants_nothing(seed_conn, write_conn, client):
    """P1: a non-msg/ key of shared/mesh-chat keeps plain key-value versioning, so a later version could drop the
    recipient and leave its grant. Only msg/ keys grant."""
    s, r = _seat(seed_conn, "s15"), _seat(seed_conn, "r15")
    fid, sha, n = _file(client, s)
    # schema_version 1, so neither the v1 check nor anything but the KEY keeps it out.
    value = {"schema_version": 1, "to": [r["name"]],
             "attachments": [{"file_id": fid, "name": "note.txt", "size_bytes": n, "sha256": sha}]}
    lease = f"lease/probe-{uuid.uuid4().hex[:8]}"
    # Layer 1, the writer itself, called directly (two layers: test each one, or either can be deleted unnoticed).
    with _bound(write_conn) as c:
        assert grants.record_message_grants(c, namespace=MESH, key=lease, value=value, sender_peer_id=s["peer_id"],
                                            sender_owner_id=s["owner_id"]) == 0
        assert grants.record_message_grants(c, namespace=MESH, key=_key(s["name"]), value=value,
                                            sender_peer_id=s["peer_id"], sender_owner_id=s["owner_id"]) == 1
    # Layer 2, the handler: it does not hand a non-message key to the writer at all.
    called = []
    real = grants.record_message_grants
    try:
        grants.record_message_grants = lambda *a, **k: called.append(k.get("key")) or 0
        handlers.dispatch("memory.write", _envelope(s, "memory.write",
                                                    {"namespace": MESH, "key": lease + "-h", "value": value}),
                          s["peer_id"], write_conn)
        assert called == []
        msg_key = _send(write_conn, s, to=[r["name"]], attach=[(fid, sha, n)])
        assert called == [msg_key]                                         # control: a msg/ key is handed over
    finally:
        grants.record_message_grants = real


def test_the_writer_bounds_its_own_input_before_any_lookup(seed_conn, write_conn, client):
    """P2: whatever reaches the writer, it resolves at most MAX_LABELS labels, skips a file id that is not a UUID
    instead of failing the write's transaction, and ignores a value that is not v1."""
    s, r = _seat(seed_conn, "s16"), _seat(seed_conn, "r16")
    fid, sha, n = _file(client, s)
    asked = []

    def resolve(conn, label):
        asked.append(label)
        return [str(r["peer_id"])] if label == r["name"] else []

    key = _key(s["name"])
    # Both fields long: the per-field bound alone would still let 2 x MAX_LABELS through.
    value = {"schema_version": 1, "to": [f"x{i}" for i in range(5000)] + [r["name"]],
             "cc": [f"y{i}" for i in range(5000)],
             "attachments": [{"file_id": "not-a-uuid"}, {"file_id": fid}]}
    with _bound(write_conn) as c:
        got = grants.record_message_grants(c, namespace=MESH, key=key, value=value, sender_peer_id=s["peer_id"],
                                           sender_owner_id=s["owner_id"], resolve=resolve)
    assert len(asked) <= grants.MAX_LABELS, len(asked)
    assert got == 0                      # r was label 5001, past the bound: not resolved, so not granted
    value["to"] = [r["name"]]
    value.pop("cc")
    with _bound(write_conn) as c:
        got = grants.record_message_grants(c, namespace=MESH, key=key, value=value, sender_peer_id=s["peer_id"],
                                           sender_owner_id=s["owner_id"], resolve=resolve)
    assert got == 1                      # control: the bad id is skipped, the good one granted, nothing raised
    unversioned = dict(value)
    unversioned.pop("schema_version")
    with _bound(write_conn) as c:
        assert grants.record_message_grants(c, namespace=MESH, key=_key(s["name"]), value=unversioned,
                                            sender_peer_id=s["peer_id"], sender_owner_id=s["owner_id"],
                                            resolve=resolve) == 0


def test_every_file_is_granted_to_every_recipient_at_the_limits(seed_conn, write_conn, client):
    """P3: to AND cc may each hold 32 labels, with 8 files. The old 32*8 cap cut files 5 to 8 out entirely."""
    s = _seat(seed_conn, "s17")
    seats = [_seat(seed_conn, f"m{i}") for i in range(64)]
    files = [_file(client, s, payload=f"limit file {i}".encode()) for i in range(8)]
    key = _send(write_conn, s, to=[p["name"] for p in seats[:32]], cc=[p["name"] for p in seats[32:]],
                attach=files)
    counts = [seed_conn.execute("SELECT count(*) FROM file_message_grants WHERE file_id = %s AND message_key = %s",
                                (f, key)).fetchone()[0] for f, _s, _n in files]
    assert counts == [64] * 8, counts


def test_an_ambiguous_fallback_label_grants_nothing(seed_conn, write_conn, client):
    """P4: a legacy peer can hold a NAME shaped like another peer's peer-<id8>. Mail may reach both; bytes reach
    neither."""
    s, a, b = _seat(seed_conn, "s18"), _seat(seed_conn, "a18"), _seat(seed_conn, "b18")
    fid, sha, n = _file(client, s)
    value = {"schema_version": 1, "to": ["peer-11111111"], "attachments": [{"file_id": fid}]}

    def resolve(conn, label):
        return [str(a["peer_id"]), str(b["peer_id"])] if label == "peer-11111111" else [str(a["peer_id"])]

    with _bound(write_conn) as c:
        assert grants.record_message_grants(c, namespace=MESH, key=_key(s["name"]), value=value,
                                            sender_peer_id=s["peer_id"], sender_owner_id=s["owner_id"],
                                            resolve=resolve) == 0
    value["to"] = [a["name"]]                                              # control: an unambiguous label grants
    with _bound(write_conn) as c:
        assert grants.record_message_grants(c, namespace=MESH, key=_key(s["name"]), value=value,
                                            sender_peer_id=s["peer_id"], sender_owner_id=s["owner_id"],
                                            resolve=resolve) == 1


# ---------------------------------------------------------------------------- direct grants (audit finding 5)
# POST /v1/files/{id}/grants {"to": label}: the file's OWNER lets another reader download it, as a message addressing
# that reader would. For a file sent to one reviewer that another must now read (six reviewer 404s, oxxxxxx 10b8bd).

def _grant(client, peer, fid, to):
    return client.post(f"/v1/files/{fid}/grants", data=json.dumps({"to": to}), content_type="application/json",
                       **_auth(peer["peer_id"]))


def test_the_owner_grants_a_file_to_another_reader_who_then_downloads_it(seed_conn, client):
    """RED before the fix: the route does not exist (404 from the URL resolver) and the reader stays refused."""
    s, r, x = _seat(seed_conn, "dg1"), _seat(seed_conn, "dg1r"), _seat(seed_conn, "dg1x")
    fid, _sha, _n = _file(client, s)
    assert _url(client, r, fid).status_code == 404, "before the grant the reader is a stranger"
    resp = _grant(client, s, fid, r["name"])
    assert resp.status_code == 201, resp.content
    assert resp.json()["granted_peers"] == [str(r["peer_id"])]
    assert _grants(seed_conn, fid) == [(str(r["peer_id"]), "grant/" + str(s["peer_id"]))]
    assert _url(client, r, fid).status_code == 200
    assert _url(client, x, fid).status_code == 404, "a peer the grant does not name stays refused"
    # the receipt names rule 3 and the grant's key
    (row,) = seed_conn.execute("SELECT rule, message_key FROM file_downloads WHERE file_id = %s AND peer_id = %s",
                               (fid, r["peer_id"])).fetchall()
    assert row == (3, "grant/" + str(s["peer_id"]))


def test_a_repeated_grant_is_a_replay(seed_conn, client):
    s, r = _seat(seed_conn, "dg2"), _seat(seed_conn, "dg2r")
    fid, _sha, _n = _file(client, s)
    assert _grant(client, s, fid, r["name"]).status_code == 201
    assert _grant(client, s, fid, r["name"]).status_code == 201
    assert len(_grants(seed_conn, fid)) == 1


def test_only_the_owner_may_grant_and_a_reader_cannot_pass_it_on(seed_conn, write_conn, client):
    """A rule-3 reader (and a stranger) gets the 404 a missing file gets, and no row is written. CONTROL: the owner's
    identical request grants."""
    s, r, q = _seat(seed_conn, "dg3"), _seat(seed_conn, "dg3r"), _seat(seed_conn, "dg3q")
    fid, sha, n = _file(client, s)
    _send(write_conn, s, to=[r["name"]], attach=[(fid, sha, n)])
    assert _url(client, r, fid).status_code == 200
    assert _grant(client, r, fid, q["name"]).status_code == 404
    assert _grant(client, q, fid, q["name"]).status_code == 404
    assert _url(client, q, fid).status_code == 404
    assert _grant(client, s, fid, q["name"]).status_code == 201
    assert _url(client, q, fid).status_code == 200


def test_a_broadcast_an_unknown_label_or_only_yourself_grants_nothing(seed_conn, client):
    s = _seat(seed_conn, "dg4")
    fid, _sha, _n = _file(client, s)
    for to in ("all", "both", "nobody-by-this-name", s["name"]):
        resp = _grant(client, s, fid, to)
        assert resp.status_code == 400, (to, resp.content)
    assert _grants(seed_conn, fid) == []


def test_a_grant_request_is_shaped_or_refused(seed_conn, client):
    s, r = _seat(seed_conn, "dg5"), _seat(seed_conn, "dg5r")
    fid, _sha, _n = _file(client, s)
    assert client.get(f"/v1/files/{fid}/grants", **_auth(s["peer_id"])).status_code == 405
    assert _grant(client, s, fid, "").status_code == 400
    assert _grant(client, s, fid, "x" * 201).status_code == 400
    assert client.post(f"/v1/files/{fid}/grants", data="[]", content_type="application/json",
                       **_auth(s["peer_id"])).status_code == 400
    assert _grant(client, s, "not-a-uuid", r["name"]).status_code == 400
    assert _grant(client, s, str(uuid.uuid4()), r["name"]).status_code == 404
    assert _grants(seed_conn, fid) == []
