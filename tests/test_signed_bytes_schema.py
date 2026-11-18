"""B02 phase A1 at the database: a shared_memory row's derived columns agree with its stored
signed bytes, whoever inserts it. Divergence 039, sql_meshychat/014, design
docs/design/signed-bytes-status.md (branch fin/design-signed-bytes) sections 2.2 to 2.4.

The tests are the design's section 4.4 list, numbered as it numbers them:

  2   a disagreeing insert is refused BY THE DATABASE, one case per derived field, each
      beside its agreeing insert, as `meshychat_app` in the server-internal context AND as the
      owner role (`meshychat_owner`, formerly `meshy`: RLS bypassed, triggers not);
  3   the shared/wiki default status derives (the database half; the handler half is in
      tests/test_signed_bytes.py);
  7   `signed_bytes` is immutable, including NULL to a value, while head retirement and a
      high_churn prune's parent unlink still work; plus a census of every column against
      both lists of the live immutability function;
  10  skill_calls: a new row whose status contradicts its signed result payload is refused by
      a CHECK added NOT VALID, and a historical violator stays readable (design 2.6, Q8);
  14  A1 is keyed to the inserting session: NULL `signed_bytes` is refused from this build's
      `meshychat_app` sessions (design Q12) and was admitted from every other session, the owner
      role and bootstrap sessions as `meshy_app` (now `meshychat_app`; review finding 1 of fin/b02-a1),
      until A2. Since A2 (divergence 051, sql_meshychat/025) those are refused too, by 025's
      trigger; A1's admission is what 025's down script gives back
      (tests/test_signed_bytes_a2.py). A row without bytes now exists only as history
      (tests/fixtures/history_rows.py).

HOUSE RULES (docs/V1-DESIGN-RULES.md). Every refusal sits beside its agreeing insert in the
same test, one variable apart (C1). Refusals are matched on the SQLSTATE class and on the
field the trigger names, never on "it raised".

Run:  .venv/Scripts/python.exe -m pytest tests/test_signed_bytes_schema.py -q
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
import uuid
from pathlib import Path

import psycopg
import psycopg.errors
import pytest
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from apps.core import rls  # noqa: E402
from apps.memoryskills import db  # noqa: E402
from tests.fixtures import history_rows  # noqa: E402
from tests.fixtures import signed_rows as sr  # noqa: E402

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")

WIKI = "shared/wiki"


# --------------------------------------------------------------------------- fixtures

@pytest.fixture()
def owner_conn():
    """The owner role `meshy` bypasses RLS and models the memory daemon's insert path.

    It seeds and inspects rows for these tests.
    """
    with psycopg.connect(DSN) as conn:
        yield conn


@pytest.fixture()
def app_conn():
    """meshychat_app, autocommit, verified by db.connect: the role this build inserts as."""
    conn = db.connect(DSN)
    try:
        yield conn
    finally:
        conn.close()


def _peer(conn, tag: str) -> dict:
    owner_id, peer_id = uuid.uuid4(), uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id) "
                    "VALUES (%s,%s,'dev',%s)",
                    (owner_id, f"sb-{tag}-{owner_id.hex[:8]}@example.test", uuid.uuid4()))
        cur.execute("INSERT INTO peers (id, owner_id, name, display_name) VALUES (%s,%s,%s,%s)",
                    (peer_id, owner_id, f"sb-{tag}-{peer_id.hex[:8]}", tag))
    conn.commit()
    return {"peer_id": peer_id, "owner_id": owner_id}


def _namespace(conn, peer: dict, name: str | None = None, policy: str = "keep_all") -> str:
    name = name or f"sbtest/{uuid.uuid4().hex[:12]}"
    with conn.cursor() as cur:
        cur.execute("INSERT INTO shared_memory_namespace (namespace, owner_peer_id, "
                    " owner_owner_id, retention_policy) VALUES (%s,%s,%s,%s) "
                    "ON CONFLICT (namespace) DO NOTHING",
                    (name, peer["peer_id"], peer["owner_id"], policy))
    conn.commit()
    return name


@pytest.fixture()
def world(owner_conn):
    writer = _peer(owner_conn, "writer")
    other = _peer(owner_conn, "other")
    return {"writer": writer, "other": other,
            "ns": _namespace(owner_conn, writer), "ns2": _namespace(owner_conn, writer),
            "wiki": _namespace(owner_conn, writer, WIKI)}


def _insert_as(role: str, owner_conn, app_conn, cols: dict):
    """One INSERT as `role`, committed or rolled back as a unit. Returns the row id."""
    if role == "meshychat_app":
        with rls.server_internal(app_conn):
            return sr.insert(app_conn, cols)
    try:
        rid = sr.insert(owner_conn, cols)
    except Exception:
        owner_conn.rollback()
        raise
    owner_conn.commit()
    return rid


def _exists(owner_conn, rid) -> bool:
    row = owner_conn.execute("SELECT 1 FROM shared_memory WHERE id = %s", (rid,)).fetchone()
    owner_conn.commit()
    return row is not None


ROLES = ("meshychat_app", "owner")


# ============================================================ 2: one case per derived field
#
# Each case: (id, how the payload is built, column overrides that DISAGREE, the field the
# refusal must name). The agreeing insert of the same payload is the control.

def _case_payload(world, case: str) -> dict:
    w = world["writer"]["peer_id"]
    ns, key = world["ns"], f"k/{uuid.uuid4().hex[:10]}"
    value = {"text": "synthetic", "n": 1}
    extra = {
        "status": {"status": "CONFIRMED"},
        "pre_discipline": {"pre_discipline": True, "claimed_status": "OPEN"},
        "claimed_status": {"pre_discipline": True, "claimed_status": "OPEN"},
        "author_label": {"author": "synthetic-author"},
        "author_label_fallback_spelling": {"author": "", "author_label": "second-spelling"},
        "tags": {"tags": ["a", "b"]},
    }.get(case, {})
    if case == "wiki_default_status":
        return sr.payload(w, world["wiki"], key, value, extra_args={"tags": ["t"]})
    return sr.payload(w, ns, key, value, extra_args=extra)


def _disagreeing(world, case: str) -> dict:
    return {
        "status": {"status": "OPEN"},
        "wiki_default_status": {"status": None},
        "pre_discipline": {"pre_discipline": False, "claimed_status": None},
        "claimed_status": {"claimed_status": "CONFIRMED"},
        "value": {"value": {"text": "synthetic", "n": 2}},
        "namespace": {"namespace": world["ns2"]},
        "key": {"key": f"k/{uuid.uuid4().hex[:10]}"},
        "created_by_peer_id": {"created_by_peer_id": world["other"]["peer_id"],
                               "created_by_owner_id": world["other"]["owner_id"]},
        "payload_jcs_sha256": {"payload_jcs_sha256": "0" * 64},
        "author_label": {"author_label": "someone-else"},
        "author_label_fallback_spelling": {"author_label": "someone-else"},
        "tags": {"tags": ["b", "a"]},
    }[case]


FIELD_CASES = ("status", "wiki_default_status", "pre_discipline", "claimed_status", "value",
               "namespace", "key", "created_by_peer_id", "payload_jcs_sha256",
               "author_label", "author_label_fallback_spelling", "tags")

#: The name the refusal must carry for each case.
NAMED = {"wiki_default_status": "status", "author_label_fallback_spelling": "author_label"}


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("case", FIELD_CASES)
def test_2_a_disagreeing_column_is_refused_beside_the_agreeing_insert(
        case, role, owner_conn, app_conn, world):
    p = _case_payload(world, case)
    w = world["writer"]
    agreeing = sr.columns(p, peer_id=w["peer_id"], owner_id=w["owner_id"])
    disagreeing = sr.columns(sr.payload(**_same_payload_args(p)), peer_id=w["peer_id"],
                             owner_id=w["owner_id"], **_disagreeing(world, case))
    field = NAMED.get(case, case)
    with pytest.raises(psycopg.errors.CheckViolation,
                       match=rf"E_SIGNED_BYTES_DISAGREE: {field}\b"):
        _insert_as(role, owner_conn, app_conn, disagreeing)
    assert not _exists(owner_conn, disagreeing["id"])

    # Control: the same payload shape with its own agreeing columns lands.
    rid = _insert_as(role, owner_conn, app_conn, agreeing)
    assert _exists(owner_conn, rid)


def _same_payload_args(p: dict) -> dict:
    """A fresh payload with the same args and a fresh call_id, so the disagreeing insert
    and its control do not collide on the UNIQUE hash or call id."""
    a = dict(p["args"])
    return {"peer_id": p["caller_peer_id"], "namespace": a.pop("namespace"),
            "key": a.pop("key"), "value": a.pop("value"), "extra_args": a}


@pytest.mark.parametrize("role", ROLES)
def test_2_the_bytes_must_be_the_provenance_payload(role, owner_conn, app_conn, world):
    """Invariant 3: bytes that hash correctly but are NOT the stored payload are refused."""
    w = world["writer"]
    p = sr.payload(w["peer_id"], world["ns"], f"k/{uuid.uuid4().hex[:8]}", {"x": 1})
    shown = dict(p, call_id=p["call_id"], args=dict(p["args"], value={"x": 2}))
    bad = sr.columns(p, peer_id=w["peer_id"], owner_id=w["owner_id"],
                     provenance_jsonb={"payload": shown})
    with pytest.raises(psycopg.errors.CheckViolation,
                       match=r"E_SIGNED_BYTES_DISAGREE: provenance_jsonb\b"):
        _insert_as(role, owner_conn, app_conn, bad)
    ok = sr.columns(p, peer_id=w["peer_id"], owner_id=w["owner_id"])
    assert _exists(owner_conn, _insert_as(role, owner_conn, app_conn, ok))


# The envelope binding the trigger requires in the bytes (design 2.3, the
# created_by_peer_id row): caller_peer_id PRESENT, kind skill.call, skill_name
# memory.write. G2's payload (no caller_peer_id, no kind) is what W3 lands today.
ENVELOPE_CASES = {
    "caller_peer_id_absent": ({"drop": ("caller_peer_id",)}, "caller_peer_id"),
    "g2_payload": ({"drop": ("caller_peer_id", "kind", "issued_at")}, "caller_peer_id"),
    "kind_absent": ({"drop": ("kind",)}, "kind"),
    "kind_wrong": ({"kind": "skill.result"}, "kind"),
    "skill_name_wrong": ({"skill_name": "memory.read"}, "skill_name"),
    "skill_name_absent": ({"drop": ("skill_name",)}, "skill_name"),
}


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("case", sorted(ENVELOPE_CASES))
def test_2_the_bytes_must_name_the_signer_the_kind_and_the_skill(
        case, role, owner_conn, app_conn, world):
    w = world["writer"]
    how, field = ENVELOPE_CASES[case]
    key = f"k/{uuid.uuid4().hex[:8]}"
    bad_p = sr.payload(w["peer_id"], world["ns"], key, {"x": 1}, **how)
    bad = sr.columns(bad_p, peer_id=w["peer_id"], owner_id=w["owner_id"])
    with pytest.raises(psycopg.errors.CheckViolation,
                       match=rf"E_SIGNED_BYTES_DISAGREE: {field}\b"):
        _insert_as(role, owner_conn, app_conn, bad)
    good_p = sr.payload(w["peer_id"], world["ns"], key, {"x": 1})
    good = sr.columns(good_p, peer_id=w["peer_id"], owner_id=w["owner_id"])
    assert _exists(owner_conn, _insert_as(role, owner_conn, app_conn, good))


# Arg TYPES the trigger refuses outright (design 2.3: pre_discipline a boolean; status and
# claimed_status strings; a tags list of strings; a custody write carries no status).
TYPE_CASES = {
    "pre_discipline_string": ({"pre_discipline": "false"}, "pre_discipline"),
    "status_list": ({"status": ["OPEN"]}, "status"),
    "claimed_status_number": ({"pre_discipline": True, "claimed_status": 5},
                              "claimed_status"),
    "custody_with_status": ({"pre_discipline": True, "status": "OPEN"}, "status"),
    "tags_non_string_element": ({"tags": ["a", 1]}, "tags"),
    "tags_object": ({"tags": {"a": 1}}, "tags"),
    "author_number": ({"author": 7}, "author_label"),
}


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("case", sorted(TYPE_CASES))
def test_2_a_signed_arg_of_the_wrong_json_type_is_refused(
        case, role, owner_conn, app_conn, world):
    """Whatever the columns say: there is no column value that agrees with these bytes."""
    w = world["writer"]
    extra, field = TYPE_CASES[case]
    key = f"k/{uuid.uuid4().hex[:8]}"
    bad_p = sr.payload(w["peer_id"], world["ns"], key, {"x": 1}, extra_args=extra)
    bad = sr.columns(bad_p, peer_id=w["peer_id"], owner_id=w["owner_id"], status=None,
                     tags=[], claimed_status=None,
                     pre_discipline=bool(extra.get("pre_discipline") is True))
    with pytest.raises(psycopg.errors.CheckViolation,
                       match=rf"E_SIGNED_BYTES_DISAGREE: {field}\b"):
        _insert_as(role, owner_conn, app_conn, bad)
    good = sr.columns(sr.payload(w["peer_id"], world["ns"], key, {"x": 1}),
                      peer_id=w["peer_id"], owner_id=w["owner_id"])
    assert _exists(owner_conn, _insert_as(role, owner_conn, app_conn, good))


@pytest.mark.parametrize("role", ROLES)
def test_2_an_unsigned_author_and_comma_tags_are_not_the_triggers_to_check(
        role, owner_conn, app_conn, world):
    """The design's two deliberate holes, pinned so a stricter trigger is a decision: an
    author the writer did not sign is the server's stamp (the roster is not in the
    database), and a comma-string tags arg is normalised by Python's str.strip, which 2.3
    leaves to an offline verifier (on a row with bytes, none is built yet)."""
    w = world["writer"]
    p = sr.payload(w["peer_id"], world["ns"], f"k/{uuid.uuid4().hex[:8]}", {"x": 1},
                   extra_args={"tags": " a, b ,,c"})
    cols = sr.columns(p, peer_id=w["peer_id"], owner_id=w["owner_id"],
                      author_label="any-server-stamp", tags=["whatever"])
    assert _exists(owner_conn, _insert_as(role, owner_conn, app_conn, cols))



def _readme_bytes_bound() -> str:
    """The README's paragraph on what memory.read adds under include_provenance, with its
    line breaks folded (README.md is CRLF in the working tree)."""
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    start = text.index("* memory.read and memory.query with `include_provenance` also answer")
    end = text.index("\n* ", start + 1)
    return " ".join(text[start:end].split())


def test_2_the_documented_meaning_of_bytes_bound_names_the_comma_tags_hole():
    """Review finding 7. The test above pins that the trigger does not check a comma-string
    tags arg, and on a row with bytes nothing else does (the classifier reads rows without
    bytes only; the re-verifier for rows with bytes is not built). The README said
    `bytes_bound` meant the database held the row's "signed author and tags" to the bytes.
    The unqualified claim must be gone, and the hole named where the claim was."""
    para = _readme_bytes_bound()
    assert "status, custody, signed author and tags to those bytes" not in para
    assert "comma-separated string" in para and "not checked" in para, para


# ===================================================== 3: the wiki default, at the database

@pytest.mark.parametrize("role", ROLES)
def test_3_a_custody_write_to_shared_wiki_has_no_default_status(
        role, owner_conn, app_conn, world):
    """The pre_discipline term of the wiki default. The mutant that drops it derives OPEN
    for this row, whose column is NULL, and refuses it. Beside it, the non-custody write
    whose NULL status IS refused (its default is OPEN)."""
    w = world["writer"]
    p = sr.payload(w["peer_id"], world["wiki"], f"k/{uuid.uuid4().hex[:8]}", {"x": 1},
                   extra_args={"pre_discipline": True, "claimed_status": "OPEN"})
    cols = sr.columns(p, peer_id=w["peer_id"], owner_id=w["owner_id"])
    assert cols["status"] is None and cols["pre_discipline"] is True
    assert _exists(owner_conn, _insert_as(role, owner_conn, app_conn, cols))

    q = sr.payload(w["peer_id"], world["wiki"], f"k/{uuid.uuid4().hex[:8]}", {"x": 1},
                   extra_args={"tags": ["t"]})
    with pytest.raises(psycopg.errors.CheckViolation,
                       match=r"E_SIGNED_BYTES_DISAGREE: status\b"):
        _insert_as(role, owner_conn, app_conn,
                   sr.columns(q, peer_id=w["peer_id"], owner_id=w["owner_id"], status=None))


# ============================================================== 14: A1 is keyed to the role

def test_14_null_signed_bytes_is_refused_from_meshy_app_by_a1_and_from_the_owner_by_a2(
        owner_conn, app_conn, world):
    """A1 refuses this build's session (divergence 039's message); the owner role, which A1
    admitted, is refused since A2 by 025's trigger (divergence 051's). One variable apart
    from both: meshychat_app with the bytes lands (the role is not refused, the NULL is)."""
    w = world["writer"]
    p = sr.payload(w["peer_id"], world["ns"], f"k/{uuid.uuid4().hex[:8]}", {"x": 1})
    cols = sr.columns(p, peer_id=w["peer_id"], owner_id=w["owner_id"], with_bytes=False)
    with pytest.raises(psycopg.errors.CheckViolation,
                       match=r"E_SIGNED_BYTES_REQUIRED: meshychat_app .*divergence 039"):
        _insert_as("meshychat_app", owner_conn, app_conn, cols)
    assert not _exists(owner_conn, cols["id"])
    # The same row, one variable apart: the inserting role.
    with pytest.raises(psycopg.errors.CheckViolation,
                       match=r"E_SIGNED_BYTES_REQUIRED: every insert .*divergence 051"):
        _insert_as("owner", owner_conn, app_conn, cols)
    assert not _exists(owner_conn, cols["id"])
    q = sr.payload(w["peer_id"], world["ns"], f"k/{uuid.uuid4().hex[:8]}", {"x": 1})
    assert _exists(owner_conn, _insert_as(
        "meshychat_app", owner_conn, app_conn,
        sr.columns(q, peer_id=w["peer_id"], owner_id=w["owner_id"])))


def test_14_a_row_without_bytes_is_held_to_nothing_by_014_and_only_history_has_one(
        owner_conn, world):
    """A1 admitted rows that carry no bytes and are classified later, phase C, and its NULL
    admission checks nothing else. That is still
    014's behaviour, so a HISTORICAL row whose columns contradict its provenance payload
    stays loadable (seeded the way a restore loads it, tests/fixtures/history_rows.py). One
    variable apart, the same row inserted by the owner today is refused by A2."""
    w = world["writer"]
    p = sr.payload(w["peer_id"], world["ns"], f"k/{uuid.uuid4().hex[:8]}", {"x": 1},
                   extra_args={"status": "OPEN"})
    cols = sr.columns(p, peer_id=w["peer_id"], owner_id=w["owner_id"], with_bytes=False,
                      status="CONFIRMED")
    with pytest.raises(psycopg.errors.CheckViolation, match=r"divergence 051"):
        _insert_as("owner", owner_conn, None, cols)
    assert _exists(owner_conn, history_rows.insert(owner_conn, cols))



def _legacy_server_conn():
    """A bootstrap session with `SET ROLE meshychat_app` (what a meshy-server run with MESHY_SET_ROLE_TO set to the app role does; before P2 that role was `meshy_app`).
    It does not use this build's session setup, so it lacks what apps.core.rls.set_role adds."""
    conn = psycopg.connect(DSN, autocommit=True)
    conn.execute("SET ROLE meshychat_app")
    assert conn.execute("SELECT current_user").fetchone()[0] == "meshychat_app", "control"
    return conn


#: Bootstrap-session shared_memory INSERT shapes for the dashboard memory-create and identity
#: rate-watch alert writes. They write no signed_bytes and both use server-internal context.
LEGACY_SERVER_INSERTS = {
    "dashboard_memory_create": (
        "INSERT INTO shared_memory (namespace, key, version, is_head, value, status, tags, "
        " provenance_jsonb, payload_jcs_sha256, created_by_peer_id, created_by_owner_id, "
        " author_label) "
        "VALUES (%(ns)s, %(key)s, 1, TRUE, %(value)s::jsonb, 'OPEN', %(tags)s::text[], "
        " %(prov)s::jsonb, %(sha)s, %(peer)s, %(owner)s, 'dashboard') RETURNING id"),
    "identity_rate_watch": (
        "INSERT INTO shared_memory "
        "(namespace, key, version, is_head, parent_id, value, status, tags, "
        " author_label, pre_discipline, claimed_status, embed_policy, provenance_jsonb, "
        " payload_jcs_sha256, created_by_peer_id, created_by_owner_id) "
        "VALUES (%(ns)s, %(key)s, 1, TRUE, NULL, %(value)s::jsonb, 'OPEN', %(tags)s, "
        " 'identity-rate-watch', FALSE, NULL, 'forbidden', %(prov)s::jsonb, %(sha)s, "
        " %(peer)s, %(owner)s) RETURNING id"),
}


def _legacy_insert(conn, shape: str, world) -> uuid.UUID:
    w = world["writer"]
    call_id = str(uuid.uuid4())
    params = {"ns": world["ns"], "key": f"k/{uuid.uuid4().hex[:8]}", "value": '{"x": 1}',
              "tags": ["dashboard"],
              "prov": '{"payload": {"call_id": "%s", "kind": "dashboard.memory.create"}}'
                      % call_id,
              "sha": hashlib.sha256(call_id.encode()).hexdigest(),
              "peer": w["peer_id"], "owner": w["owner_id"]}
    with conn.transaction():
        conn.execute("SELECT set_config('app.server_internal', '1', true)")
        return conn.execute(LEGACY_SERVER_INSERTS[shape], params).fetchone()[0]


@pytest.mark.parametrize("shape", sorted(LEGACY_SERVER_INSERTS))
def test_14_legacy_server_inserts_are_refused_since_a2(owner_conn, world, shape):
    """Adversarial review of fin/b02-a1, finding 1. Q12 assumed the memory daemon, as `meshy`,
    was the only inserter. Bootstrap sessions as `meshychat_app` also insert, so A1's refusal, keyed
    to the session marker from apps.core.rls.set_role, admitted these rows. A2 stops them: 025's
    trigger refuses them (design 4.1; divergence 051). Design 4.1 limits that handover to A2,
    once no unmarked writer remains. One variable apart, the same connection after rls.set_role
    is refused by A1's check, which fires first; the marker now decides only which trigger
    refuses. A1's admission of these shapes comes back with 025's down script
    (tests/test_signed_bytes_a2.py, test_d_a2s_down_script_gives_back_a1s_admission_exactly)."""
    conn = _legacy_server_conn()
    try:
        with pytest.raises(psycopg.errors.CheckViolation,
                           match=r"E_SIGNED_BYTES_REQUIRED: every insert .*divergence 051"):
            _legacy_insert(conn, shape, world)
        rls.set_role(conn)
        with pytest.raises(psycopg.errors.CheckViolation,
                           match=r"E_SIGNED_BYTES_REQUIRED: meshychat_app .*divergence 039"):
            _legacy_insert(conn, shape, world)
    finally:
        conn.close()


# ================================================================ 7: immutability + census

def _one_row(owner_conn, world, *, ns=None, version=1, parent_id=None, is_head=True,
             key=None, with_bytes=True):
    w = world["writer"]
    p = sr.payload(w["peer_id"], ns or world["ns"], key or f"k/{uuid.uuid4().hex[:8]}",
                   {"x": version})
    cols = sr.columns(p, peer_id=w["peer_id"], owner_id=w["owner_id"], version=version,
                      parent_id=parent_id, is_head=is_head, with_bytes=with_bytes)
    if with_bytes:
        return _insert_as("owner", owner_conn, None, cols), cols
    # A row without bytes exists only as history since A2 (divergence 051).
    owner_conn.commit()
    return history_rows.insert(owner_conn, cols), cols


def test_7_signed_bytes_cannot_be_changed_or_backfilled(owner_conn, world):
    rid, _ = _one_row(owner_conn, world)
    with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
        owner_conn.execute("UPDATE shared_memory SET signed_bytes = %s WHERE id = %s",
                           (b"{}", rid))
    owner_conn.rollback()
    with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
        owner_conn.execute("UPDATE shared_memory SET signed_bytes = NULL WHERE id = %s",
                           (rid,))
    owner_conn.rollback()

    legacy, cols = _one_row(owner_conn, world, with_bytes=False)
    with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
        owner_conn.execute("UPDATE shared_memory SET signed_bytes = %s WHERE id = %s",
                           (sr.P.jcs(cols["provenance_jsonb"].obj["payload"]), legacy))
    owner_conn.rollback()

    # Controls: head retirement, the one legal UPDATE, still works on both rows.
    owner_conn.execute("UPDATE shared_memory SET is_head = FALSE WHERE id = ANY(%s)",
                       ([rid, legacy],))
    owner_conn.commit()
    got = owner_conn.execute("SELECT count(*) FROM shared_memory WHERE id = ANY(%s) "
                             "AND NOT is_head", ([rid, legacy],)).fetchone()[0]
    owner_conn.commit()
    assert got == 2


def test_7_a_high_churn_parent_unlink_still_works(owner_conn, world):
    """063's one exemption must keep working with the new column in its list."""
    ns = _namespace(owner_conn, world["writer"], policy="high_churn")
    key = f"k/{uuid.uuid4().hex[:8]}"
    v1, _ = _one_row(owner_conn, world, ns=ns, key=key, is_head=False)
    _one_row(owner_conn, world, ns=ns, key=key, version=2, parent_id=v1)
    owner_conn.execute("UPDATE shared_memory SET parent_id = NULL WHERE parent_id = %s", (v1,))
    owner_conn.execute("DELETE FROM shared_memory WHERE id = %s", (v1,))
    owner_conn.commit()
    assert not _exists(owner_conn, v1)

    # The same unlink in a keep_all namespace is still refused (the exemption is narrow).
    key2 = f"k/{uuid.uuid4().hex[:8]}"
    k1, _ = _one_row(owner_conn, world, key=key2, is_head=False)
    _one_row(owner_conn, world, key=key2, version=2, parent_id=k1)
    with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
        owner_conn.execute("UPDATE shared_memory SET parent_id = NULL WHERE parent_id = %s",
                           (k1,))
    owner_conn.rollback()


#: Columns the immutability function deliberately does NOT hold in both lists: is_head
#: (the legal retirement), parent_id (063's high_churn unlink), updated_at, and the
#: generated provenance_call_id, which follows provenance_jsonb (design test 7).
MUTABLE_OR_DERIVED = {"is_head", "parent_id", "updated_at", "provenance_call_id"}


def _immutability_lists(owner_conn) -> tuple[set[str], set[str]]:
    src = owner_conn.execute(
        "SELECT prosrc FROM pg_proc WHERE proname = 'shared_memory_enforce_immutability'"
    ).fetchone()[0]
    owner_conn.commit()
    same = set(re.findall(r"NEW\.(\w+)\s+IS NOT DISTINCT FROM OLD\.\1\b", src))
    changed = set(re.findall(r"NEW\.(\w+)\s+IS DISTINCT FROM OLD\.\1\b", src))
    return same, changed


def test_7_census_every_column_is_in_both_lists_of_the_live_function(owner_conn):
    """A test naming `signed_bytes` alone passes again the day another column is added or the
    function changes. So every column is counted, from
    information_schema, against the function the database is actually running."""
    cols = {r[0] for r in owner_conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'shared_memory'").fetchall()}
    owner_conn.commit()
    assert {"id", "value", "status", "signed_bytes"} <= cols, sorted(cols)
    same, changed = _immutability_lists(owner_conn)
    assert same and changed, "control: the parser reads both lists"
    held = cols - MUTABLE_OR_DERIVED
    assert held - same == set(), f"missing from the high_churn exemption list: {held - same}"
    assert held - changed == set(), f"missing from the immutability list: {held - changed}"
    assert MUTABLE_OR_DERIVED <= cols, "a listed exemption is not a column any more"


# ============================================== 10: skill_calls keeps the pair, with a CHECK

MIGRATION = ROOT / "sql_meshychat" / "014_shared_memory_signed_bytes.sql"
CHECK_NAME = "skill_calls_status_matches_signed_result"


def _check_statement() -> str:
    """The migration's own ADD CONSTRAINT statement, so this test re-runs exactly what the
    migration ran: a VALID constraint would fail right here, on the seeded row."""
    text = MIGRATION.read_text("utf-8")
    m = re.search(rf"ALTER TABLE skill_calls ADD CONSTRAINT {CHECK_NAME}[^;]*;", text)
    assert m, "the migration no longer adds the constraint"
    return m.group(0)


def _skill(conn, peer: dict) -> uuid.UUID:
    sid = conn.execute(
        "INSERT INTO skills (peer_id, name, version, mode, share_mode, input_schema, "
        " is_current) VALUES (%s, %s, '1', 'sync', 'grants_only', '{}', TRUE) RETURNING id",
        (peer["peer_id"], f"sb.test.{uuid.uuid4().hex[:8]}")).fetchone()[0]
    conn.commit()
    return sid


def _skill_call(conn, peer: dict, skill_id, *, status: str, result_payload) -> str:
    call_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO skill_calls (call_id, caller_peer_id, caller_owner_id, target_peer_id, "
        " target_owner_id, skill_id, skill_name, skill_version, mode, transport, status, "
        " args, result_payload, deadline_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,'sb.test','1','sync','http',%s,'{}',%s, now())",
        (call_id, peer["peer_id"], peer["owner_id"], peer["peer_id"], peer["owner_id"],
         skill_id, status, None if result_payload is None else Jsonb(result_payload)))
    return call_id


def test_10_a_new_skill_call_whose_status_contradicts_its_signed_result_is_refused(
        owner_conn, world):
    """Design 2.6 and Q8, the #922 class: a row whose status column says one thing and whose
    signed result payload says another. NOT VALID, so a historical violator (seeded here
    exactly as the migration finds one: present before the constraint is added) stays and
    reads; every new or updated row is held."""
    peer = world["writer"]
    skill_id = _skill(owner_conn, peer)
    try:
        owner_conn.execute(f"ALTER TABLE skill_calls DROP CONSTRAINT {CHECK_NAME}")
        historical = _skill_call(owner_conn, peer, skill_id, status="completed",
                                 result_payload={"status": "failed"})
        owner_conn.execute(_check_statement())
        owner_conn.commit()
    except Exception:
        owner_conn.rollback()
        raise
    valid = owner_conn.execute("SELECT convalidated FROM pg_constraint WHERE conname = %s",
                               (CHECK_NAME,)).fetchone()
    owner_conn.commit()
    assert valid == (False,), "the constraint must be NOT VALID"
    row = owner_conn.execute("SELECT status, result_payload->>'status' FROM skill_calls "
                             "WHERE call_id = %s", (historical,)).fetchone()
    owner_conn.commit()
    assert row == ("completed", "failed"), "the historical row is kept and readable"

    with pytest.raises(psycopg.errors.CheckViolation, match=CHECK_NAME):
        _skill_call(owner_conn, peer, skill_id, status="completed",
                    result_payload={"status": "failed"})
    owner_conn.rollback()
    with pytest.raises(psycopg.errors.CheckViolation, match=CHECK_NAME):
        owner_conn.execute("UPDATE skill_calls SET error_message = 'touched' "
                           "WHERE call_id = %s", (historical,))
    owner_conn.rollback()

    # Controls: the pair agreeing lands, and so does a row with no signed result yet.
    ok = _skill_call(owner_conn, peer, skill_id, status="failed",
                     result_payload={"status": "failed"})
    pending = _skill_call(owner_conn, peer, skill_id, status="completed", result_payload=None)
    owner_conn.commit()
    n = owner_conn.execute("SELECT count(*) FROM skill_calls WHERE call_id = ANY(%s)",
                           ([ok, pending],)).fetchone()[0]
    owner_conn.commit()
    assert n == 2
