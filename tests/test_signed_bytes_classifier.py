"""B02 phase A1: historical rows are classified, never rewritten. Divergence 039, design
docs/design/signed-bytes-status.md (fin/design-signed-bytes) sections 3.2 to 3.4, Q6, Q7
and Q14, tests 8, 9 and 17 of section 4.4.

A row with no `signed_bytes` (written before sql_meshychat/014, or by the daemon as the owner
role) gets a verdict from a background job, in an append-only table
keyed by the row's COPIED identity, with no foreign key: an ephemeral rewrite or a prune
deletes ledger rows, and a cascade would erase the verdict with them.

  8   one synthetic fixture per verdict and per note: an rfc8785 match, a sort_keys-only
      match, a hash match whose signature fails, no hash match, no envelope, a key revoked
      after the write and one revoked BEFORE it, a signed-status disagreement, a
      defaulted-status disagreement, an envelope bound to another row, a key the row's own
      provenance names but peer_keys does not, and C2's fixture (an integer beyond 2^53-1
      beside an astral key, signed over sort_keys, which only the default loader matches)
      with its twin that only the I-JSON loader matches;
  9   the verdict table's trigger refuses verified bytes that do not hash to the row, and a
      copied identity that is not the row's; meshychat_app cannot UPDATE or DELETE a verdict, and
      a request context cannot insert one;
  17  an ephemeral rewrite and a prune remove ledger rows; their verdicts remain and are
      counted.
  P   the Python derivation (apps/memoryskills/signed_bytes.derive_fields) and the SQL one
      (sm_derive_*) agree over one corpus, refusals included.

No production content in any fixture: every payload here is synthetic.

Run:  .venv/Scripts/python.exe -m pytest tests/test_signed_bytes_classifier.py -q
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
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
from apps.crypto import primitives as P  # noqa: E402
from apps.memoryskills import db  # noqa: E402
from tests.fixtures import history_rows  # noqa: E402
from tests.fixtures import signed_rows as sr  # noqa: E402

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")

WIKI = "shared/wiki"


# --------------------------------------------------------------------------- fixtures

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


def _signer(conn, tag: str) -> dict:
    owner_id, peer_id = uuid.uuid4(), uuid.uuid4()
    seed = os.urandom(32)
    pub = P.public_key_of(seed)
    kid = P.kid_for_peer_key(pub)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id) "
                    "VALUES (%s,%s,'dev',%s)",
                    (owner_id, f"lv-{tag}-{owner_id.hex[:8]}@example.test", uuid.uuid4()))
        cur.execute("INSERT INTO peers (id, owner_id, name, display_name) VALUES (%s,%s,%s,%s)",
                    (peer_id, owner_id, f"lv-{tag}-{peer_id.hex[:8]}", tag))
        cur.execute("INSERT INTO peer_keys (peer_id, kid, public_key, active) "
                    "VALUES (%s,%s,%s,TRUE)", (peer_id, kid, P.b64u(pub)))
    conn.commit()
    return {"peer_id": peer_id, "owner_id": owner_id, "seed": seed, "pub": pub, "kid": kid}


def _namespace(conn, peer: dict, name: str | None = None, policy: str = "keep_all") -> str:
    name = name or f"lvtest/{uuid.uuid4().hex[:12]}"
    conn.execute("INSERT INTO shared_memory_namespace (namespace, owner_peer_id, "
                 " owner_owner_id, retention_policy) VALUES (%s,%s,%s,%s) "
                 "ON CONFLICT (namespace) DO NOTHING",
                 (name, peer["peer_id"], peer["owner_id"], policy))
    conn.commit()
    return name


def _sort_keys(obj) -> bytes:
    """The memory daemon's hash input uses sorted keys and compact separators.

    It stays separate from this build's RFC 8785 canonicalizer so the two hash forms remain
    distinguishable.
    """
    return json.dumps(obj, separators=(",", ":"), sort_keys=True,
                      ensure_ascii=False).encode("utf-8")


def legacy(conn, signer: dict, ns: str, *, value=None, extra_args=None, canon="rfc8785",
           bad_signature=False, other_hash=False, no_envelope=False, key=None,
           signed_key=None, provenance_key=None, sign_with=None, created_at=None,
           version=1, parent_id=None, is_head=True, column_value=None, **cols) -> uuid.UUID:
    """One HISTORICAL row: inserted as the owner role with NULL signed_bytes, its provenance
    shaped as the daemon writes it, hashed by `canon`, and signed over those bytes.
    `cols` overrides the columns derived from the payload (a disagreement)."""
    key = key or f"k/{uuid.uuid4().hex[:10]}"
    value = {"text": "synthetic"} if value is None else value
    p = sr.payload(signer["peer_id"], ns, signed_key or key, value, extra_args=extra_args)
    raw = P.jcs(p) if canon == "rfc8785" else _sort_keys(p)
    seed = (sign_with or signer)["seed"]
    signature = P.sign(seed, raw)
    if bad_signature:
        signature = bytes([signature[0] ^ 1]) + signature[1:]
    prov = {"payload": p, "signature": P.b64u(signature), "signing_kid": signer["kid"],
            "public_key_b64url": P.b64u(provenance_key or signer["pub"]),
            "caller_owner_id": str(signer["owner_id"])}
    if no_envelope:
        prov = {"payload": {"call_id": p["call_id"]}}
    base = sr.columns(p, peer_id=signer["peer_id"], owner_id=signer["owner_id"],
                      with_bytes=False, version=version, parent_id=parent_id, is_head=is_head)
    base.update(key=key, provenance_jsonb=Jsonb(prov),
                payload_jcs_sha256=(hashlib.sha256(os.urandom(8)).hexdigest() if other_hash
                                    else hashlib.sha256(raw).hexdigest()))
    if created_at is not None:
        base["created_at"] = created_at
    if column_value is not None:
        base["value"] = Jsonb(column_value)
    for name, val in cols.items():
        base[name] = Jsonb(val) if name == "value" else val
    # Since A2 (divergence 051) no session can insert a row without bytes; history is
    # seeded the way it reached production, with A2's trigger off (tests/fixtures/
    # history_rows.py). 014's agree-trigger admits the owner's NULL row as before.
    conn.commit()
    return history_rows.insert(conn, base)


def _classify(app_conn):
    from apps.memoryskills import legacy_verdict
    for _ in range(50):
        if not legacy_verdict.catch_up(app_conn, max_batch=500).more:
            return
    raise AssertionError("the classifier never drained")


def verdict_of(owner_conn, rid) -> dict:
    from apps.memoryskills import legacy_verdict
    cur = owner_conn.execute(
        "SELECT * FROM shared_memory_legacy_verdict WHERE row_id = %s AND classifier_version = %s",
        (rid, legacy_verdict.CLASSIFIER_VERSION))
    names = [d.name for d in cur.description]
    row = cur.fetchone()
    owner_conn.commit()
    assert row is not None, f"no verdict for {rid}"
    return dict(zip(names, row))


@pytest.fixture()
def world(owner_conn):
    s = _signer(owner_conn, "w")
    return {"signer": s, "ns": _namespace(owner_conn, s),
            "wiki": _namespace(owner_conn, s, WIKI)}


# ====================================================================== 8: the verdicts

def test_8_an_rfc8785_row_verifies_and_its_bytes_are_recovered(owner_conn, app_conn, world):
    s = world["signer"]
    rid = legacy(owner_conn, s, world["ns"], value={"n": 1.0, "f": 0.1})
    _classify(app_conn)
    v = verdict_of(owner_conn, rid)
    assert (v["verdict"], v["loader"], v["canonicalizer"]) == (
        "verified", "json_default", "rfc8785")
    assert v["verified_kid"] == s["kid"]
    stored = owner_conn.execute("SELECT provenance_jsonb->'payload' FROM shared_memory "
                                "WHERE id = %s", (rid,)).fetchone()[0]
    owner_conn.commit()
    assert bytes(v["verified_bytes"]) == P.jcs(stored)
    assert (v["binding_failures"], v["disagreeing_signed"], v["disagreeing_defaulted"],
            v["key_revoked_before_write"]) == ([], [], [], False)


def test_8_a_sort_keys_only_row_verifies_under_sort_keys(owner_conn, app_conn, world):
    """RFC 8785 orders keys by UTF-16 code unit, sort_keys by code point: an astral key
    and a BMP key above U+D800 sort differently, and 1.0 is `1` against `1.0`."""
    s = world["signer"]
    rid = legacy(owner_conn, s, world["ns"], value={"\U0001F600": 1, "\uff41": 2, "n": 1.0},
                 canon="sort_keys")
    _classify(app_conn)
    v = verdict_of(owner_conn, rid)
    assert (v["verdict"], v["canonicalizer"]) == ("verified", "sort_keys")


def test_8_a_hash_match_whose_signature_fails_is_not_verified(owner_conn, app_conn, world):
    s = world["signer"]
    rid = legacy(owner_conn, s, world["ns"], bad_signature=True)
    good = legacy(owner_conn, s, world["ns"])
    _classify(app_conn)
    v = verdict_of(owner_conn, rid)
    assert (v["verdict"], v["canonicalizer"], v["verified_bytes"]) == (
        "hash_bound_signature_fails", "rfc8785", None)
    assert v["lenient_accepts"] is False
    assert verdict_of(owner_conn, good)["verdict"] == "verified"            # control


def test_8_no_hash_match_and_no_envelope(owner_conn, app_conn, world):
    s = world["signer"]
    a = legacy(owner_conn, s, world["ns"], other_hash=True)
    b = legacy(owner_conn, s, world["ns"], no_envelope=True)
    _classify(app_conn)
    va, vb = verdict_of(owner_conn, a), verdict_of(owner_conn, b)
    assert (va["verdict"], va["loader"], va["canonicalizer"]) == ("no_hash_match", None, None)
    assert vb["verdict"] == "no_envelope"


def test_8_the_key_is_peer_keys_not_the_one_the_row_names(owner_conn, app_conn, world):
    """A row whose provenance names a key that signed it, while peer_keys holds the
    writer's real key under that kid: the key is only as good as the table, not the row."""
    s = world["signer"]
    intruder_seed = os.urandom(32)
    intruder = {"seed": intruder_seed, "pub": P.public_key_of(intruder_seed)}
    rid = legacy(owner_conn, s, world["ns"], sign_with=intruder, provenance_key=intruder["pub"])
    _classify(app_conn)
    assert verdict_of(owner_conn, rid)["verdict"] == "hash_bound_signature_fails"


def _revoke(conn, signer: dict, at: _dt.datetime) -> None:
    conn.execute("UPDATE peer_keys SET active = FALSE, revoked_at = %s "
                 "WHERE peer_id = %s AND kid = %s", (at, signer["peer_id"], signer["kid"]))
    conn.commit()


def test_8_a_key_revoked_before_the_write_is_counted_apart(owner_conn, app_conn):
    now = _dt.datetime.now(_dt.timezone.utc)
    before, after = _signer(owner_conn, "rb"), _signer(owner_conn, "ra")
    ns = _namespace(owner_conn, before)
    early = legacy(owner_conn, before, ns, created_at=now - _dt.timedelta(days=2))
    late = legacy(owner_conn, after, ns, created_at=now - _dt.timedelta(days=2))
    _revoke(owner_conn, before, now - _dt.timedelta(days=3))     # revoked, THEN written
    _revoke(owner_conn, after, now - _dt.timedelta(days=1))      # written, then revoked
    _classify(app_conn)
    ve, vl = verdict_of(owner_conn, early), verdict_of(owner_conn, late)
    assert (ve["verdict"], ve["key_revoked_before_write"]) == ("verified", True)
    assert (vl["verdict"], vl["key_revoked_before_write"]) == ("verified", False)


def test_8_a_signed_status_disagreement_is_not_a_defaulted_one(owner_conn, app_conn, world):
    """The shared_memory analogue of the 197: the writer SIGNED OPEN and the column says
    CONFIRMED. Beside it, rule drift: a wiki row with no signed status whose column holds
    what an older daemon defaulted (here NULL, where 2.3 derives OPEN)."""
    s = world["signer"]
    signed = legacy(owner_conn, s, world["ns"], extra_args={"status": "OPEN"},
                    status="CONFIRMED")
    defaulted = legacy(owner_conn, s, world["wiki"], extra_args={"tags": ["t"]}, status=None)
    _classify(app_conn)
    vs, vd = verdict_of(owner_conn, signed), verdict_of(owner_conn, defaulted)
    assert (vs["verdict"], vs["disagreeing_signed"], vs["disagreeing_defaulted"]) == (
        "verified", ["status"], [])
    assert (vd["verdict"], vd["disagreeing_signed"], vd["disagreeing_defaulted"]) == (
        "verified", [], ["status"])


def test_8_signed_tags_author_and_custody_disagreements(owner_conn, app_conn, world):
    s = world["signer"]
    rid = legacy(owner_conn, s, world["ns"],
                 extra_args={"tags": ["a"], "author": "signed-author",
                             "pre_discipline": True, "claimed_status": "OPEN"},
                 tags=["b"], author_label="someone-else", pre_discipline=True,
                 claimed_status="CONFIRMED")
    comma = legacy(owner_conn, s, world["ns"], extra_args={"tags": "x, y"}, tags=["x"])
    _classify(app_conn)
    v = verdict_of(owner_conn, rid)
    assert sorted(v["disagreeing_signed"]) == ["author_label", "claimed_status", "tags"]
    vc = verdict_of(owner_conn, comma)
    assert (vc["disagreeing_signed"], vc["disagreeing_defaulted"]) == ([], ["tags"]), (
        "a comma-string is normalised by the server: rule drift, not a signed contradiction")


def test_8_an_envelope_bound_to_another_row_is_a_binding_failure(owner_conn, app_conn, world):
    """A signature that verifies over an envelope for some other key is not evidence for
    this row, so it is not a status question either."""
    s = world["signer"]
    rid = legacy(owner_conn, s, world["ns"], signed_key="another/key")
    val = legacy(owner_conn, s, world["ns"], value={"x": 1}, column_value={"x": 2})
    _classify(app_conn)
    v = verdict_of(owner_conn, rid)
    assert (v["verdict"], v["binding_failures"]) == ("verified", ["key"])
    assert verdict_of(owner_conn, val)["binding_failures"] == ["value"]


BINDING_CASES = {
    # field: how the SIGNED payload departs from the row it is stored on
    "kind": lambda p, other: p.update(kind="skill.result"),
    "skill_name": lambda p, other: p.update(skill_name="memory.read"),
    "caller_peer_id": lambda p, other: p.update(caller_peer_id=str(other)),
    "namespace": lambda p, other: p["args"].update(namespace="signed/elsewhere"),
    "key": lambda p, other: p["args"].update(key="signed/other-key"),
    "value": lambda p, other: p["args"].update(value={"x": 2}),
}


@pytest.mark.parametrize("field", sorted(BINDING_CASES))
def test_8_each_binding_field_is_checked_on_its_own(field):
    """Adversarial review of fin/b02-a1, finding 2 (M9: the namespace comparison dropped
    survived every test). Design 3.3 step 5 names six fields; each one, alone, makes a
    verified envelope evidence for some other row. Pure Python, no database: the row is a
    dict and the key lookup a function, so the only thing varied is the field."""
    from apps.memoryskills import legacy_verdict as lv
    seed = os.urandom(32)
    pub = P.public_key_of(seed)
    kid = P.kid_for_peer_key(pub)
    writer, other = uuid.uuid4(), uuid.uuid4()

    def row_for(p):
        raw = P.jcs(p)
        prov = {"payload": p, "signature": P.b64u(P.sign(seed, raw)), "signing_kid": kid,
                "public_key_b64url": P.b64u(pub)}
        return {"id": uuid.uuid4(), "namespace": "row/ns", "key": "row/key", "version": 1,
                "created_at": _dt.datetime.now(_dt.timezone.utc),
                "payload_jcs_sha256": hashlib.sha256(raw).hexdigest(),
                "provenance_text": json.dumps(prov), "value_text": json.dumps({"x": 1}),
                "status": None, "pre_discipline": False, "claimed_status": None,
                "author_label": "any", "tags": [], "created_by_peer_id": writer}

    def key_of(peer, k):
        return (P.b64u(pub), None) if (peer, k) == (writer, kid) else None

    good = sr.payload(writer, "row/ns", "row/key", {"x": 1})
    assert lv.classify(row_for(good), key_of).binding_failures == [], "control"
    bad = json.loads(json.dumps(good))
    BINDING_CASES[field](bad, other)
    v = lv.classify(row_for(bad), key_of)
    assert (v.verdict, v.binding_failures) == ("verified", [field])


def test_8_c2_an_int_beyond_2_53_minus_1_beside_an_astral_key_matches_the_default_loader_only(
        owner_conn, app_conn, world):
    """A client can turn 1e16 into the int 10**16 and the daemon hashes sort_keys over the
    int. JSONB keeps the value, not the type: read back with the I-JSON loader the
    int is a float, and neither canonicaliser reproduces the hash once an astral key
    differs too. Only the default loader with sort_keys does (design critique C2)."""
    s = world["signer"]
    rid = legacy(owner_conn, s, world["ns"], value={"n": 10**16, "\U0001F600": 1, "\uff41": 2},
                 canon="sort_keys")
    _classify(app_conn)
    v = verdict_of(owner_conn, rid)
    assert (v["verdict"], v["loader"], v["canonicalizer"]) == (
        "verified", "json_default", "sort_keys")


def test_8_a_float_beyond_2_53_minus_1_beside_an_astral_key_matches_the_ijson_loader_only(
        owner_conn, app_conn, world):
    """The twin: what this build wrote before A1 for a 1e16 float (design G1). rfc8785
    hashed the FLOAT; JSONB returns an int that rfc8785 refuses; only the I-JSON loader,
    which reads it back as a float, rebuilds the bytes."""
    s = world["signer"]
    rid = legacy(owner_conn, s, world["ns"], value={"n": 1e16, "\U0001F600": 1, "\uff41": 2})
    _classify(app_conn)
    v = verdict_of(owner_conn, rid)
    assert (v["verdict"], v["loader"], v["canonicalizer"]) == (
        "verified", "ijson_float", "rfc8785")


def test_8_rows_with_bytes_are_not_classified_and_a_second_pass_finds_nothing(
        owner_conn, app_conn, world):
    from apps.memoryskills import legacy_verdict
    s = world["signer"]
    p = sr.payload(s["peer_id"], world["ns"], f"k/{uuid.uuid4().hex[:8]}", {"x": 1})
    new = sr.insert(owner_conn, sr.columns(p, peer_id=s["peer_id"], owner_id=s["owner_id"]))
    owner_conn.commit()
    old = legacy(owner_conn, s, world["ns"])
    _classify(app_conn)
    assert verdict_of(owner_conn, old)["verdict"] == "verified"
    n = owner_conn.execute("SELECT count(*) FROM shared_memory_legacy_verdict WHERE row_id = %s",
                           (new,)).fetchone()[0]
    owner_conn.commit()
    assert n == 0
    assert legacy_verdict.catch_up(app_conn, max_batch=500).rows_read == 0


# ========================================================== 9: the verdict table holds itself

def _verdict_cols(owner_conn, rid, **over) -> dict:
    r = owner_conn.execute(
        "SELECT namespace, key, version, created_at, payload_jcs_sha256, "
        "       provenance_jsonb->'payload' FROM shared_memory WHERE id = %s", (rid,)).fetchone()
    owner_conn.commit()
    cols = {"row_id": rid, "row_namespace": r[0], "row_key": r[1], "row_version": r[2],
            "row_created_at": r[3], "row_payload_jcs_sha256": r[4],
            "classifier_version": f"test-{uuid.uuid4().hex[:6]}", "verdict": "verified",
            "loader": "json_default", "canonicalizer": "rfc8785", "verified_bytes": P.jcs(r[5])}
    cols.update(over)
    return cols


def _insert_verdict(conn, cols: dict) -> None:
    names = list(cols)
    conn.execute(f"INSERT INTO shared_memory_legacy_verdict ({', '.join(names)}) "
                 f"VALUES ({', '.join(['%s'] * len(names))})", [cols[n] for n in names])


def test_9_verified_bytes_must_hash_to_the_row_and_the_identity_must_be_the_rows(
        owner_conn, app_conn, world):
    s = world["signer"]
    rid = legacy(owner_conn, s, world["ns"])
    other = legacy(owner_conn, s, world["ns"])
    other_bytes = P.jcs(owner_conn.execute(
        "SELECT provenance_jsonb->'payload' FROM shared_memory WHERE id = %s",
        (other,)).fetchone()[0])
    owner_conn.commit()
    for bad, match in ((_verdict_cols(owner_conn, rid, verified_bytes=other_bytes),
                        "E_VERDICT_BYTES"),
                       (_verdict_cols(owner_conn, rid, row_key="not/the/key"),
                        "E_VERDICT_UNBOUND"),
                       (_verdict_cols(owner_conn, rid, row_id=uuid.uuid4()),
                        "E_VERDICT_UNBOUND")):
        with pytest.raises(psycopg.errors.CheckViolation, match=match):
            with rls.projection_writer(app_conn):
                _insert_verdict(app_conn, bad)
    with rls.projection_writer(app_conn):                                   # control
        _insert_verdict(app_conn, _verdict_cols(owner_conn, rid))


def test_9_verified_bytes_must_be_the_rows_payload_not_only_hash_to_its_column(
        owner_conn, app_conn, world):
    """Adversarial review of fin/b02-a1, finding 2 (HZ). Design 2.2 asks for both: the
    bytes hash to the row's payload_jcs_sha256 AND parse to its provenance payload. A row
    whose stored hash commits to some OTHER payload (an older daemon rule, a hand edit)
    lets that payload's bytes pass the hash check, and only the second check refuses them."""
    s = world["signer"]
    other = {"not": "the provenance payload"}
    rid = legacy(owner_conn, s, world["ns"],
                 payload_jcs_sha256=hashlib.sha256(P.jcs(other)).hexdigest())
    with pytest.raises(psycopg.errors.CheckViolation,
                       match=r"E_VERDICT_BYTES: verified_bytes are not the provenance payload"):
        with rls.projection_writer(app_conn):
            _insert_verdict(app_conn, _verdict_cols(owner_conn, rid,
                                                    verified_bytes=P.jcs(other)))
    ok = legacy(owner_conn, s, world["ns"])                                 # control
    with rls.projection_writer(app_conn):
        _insert_verdict(app_conn, _verdict_cols(owner_conn, ok))


@pytest.mark.parametrize("column, wrong", [
    ("row_namespace", "not/the/namespace"), ("row_key", "not/the/key"), ("row_version", 7),
    ("row_created_at", _dt.datetime(2001, 1, 1, tzinfo=_dt.timezone.utc)),
    ("row_payload_jcs_sha256", "0" * 64)])
def test_9_every_copied_identity_column_must_be_the_rows(
        owner_conn, app_conn, world, column, wrong):
    """Adversarial review of fin/b02-a1, finding 2 (M10). The copied identity is what a
    count sees after the row is gone, and the hash is what the table's CHECK binds bytes to
    then, so each column must be the row's own. A verdict with no bytes, so that no CHECK
    on the bytes answers first."""
    s = world["signer"]
    rid = legacy(owner_conn, s, world["ns"])
    bare = {"verdict": "no_envelope", "loader": None, "canonicalizer": None,
            "verified_bytes": None}
    with pytest.raises(psycopg.errors.CheckViolation, match=r"E_VERDICT_UNBOUND: the copied"):
        with rls.projection_writer(app_conn):
            _insert_verdict(app_conn, _verdict_cols(owner_conn, rid, **bare, **{column: wrong}))
    with rls.projection_writer(app_conn):                                   # control
        _insert_verdict(app_conn, _verdict_cols(owner_conn, rid, **bare))


def test_9_meshy_app_appends_only_and_only_from_the_job_context(owner_conn, app_conn, world):
    s = world["signer"]
    rid = legacy(owner_conn, s, world["ns"])
    cols = _verdict_cols(owner_conn, rid)
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="row-level security"):
        with rls.server_internal(app_conn):                   # a request's context
            _insert_verdict(app_conn, cols)
    with rls.projection_writer(app_conn):                     # the job's context: lands
        _insert_verdict(app_conn, cols)
    for sql in ("UPDATE shared_memory_legacy_verdict SET verdict = 'no_envelope' "
                "WHERE row_id = %s",
                "DELETE FROM shared_memory_legacy_verdict WHERE row_id = %s"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="permission denied"):
            with rls.projection_writer(app_conn):
                app_conn.execute(sql, (rid,))
    n = owner_conn.execute("SELECT count(*) FROM shared_memory_legacy_verdict "
                           "WHERE row_id = %s", (rid,)).fetchone()[0]
    owner_conn.commit()
    assert n == 1


# ================================================================ 17: deletion keeps verdicts

def test_17_an_ephemeral_rewrite_and_a_prune_keep_the_deleted_rows_verdicts(
        owner_conn, app_conn):
    from apps.memoryskills import handlers, legacy_verdict
    from tests.test_memory_skills import _claim, _envelope, _peer

    peer = _peer(owner_conn, "lv17")
    eph = f"lvtest/eph-{uuid.uuid4().hex[:8]}"
    churn = f"lvtest/churn-{uuid.uuid4().hex[:8]}"
    _claim(app_conn, peer, eph, retention_policy="ephemeral")
    _claim(app_conn, peer, churn, retention_policy="high_churn")
    signer = dict(peer, pub=peer["pub"])

    key = f"k/{uuid.uuid4().hex[:8]}"
    old = legacy(owner_conn, signer, eph, key=key)
    ckey = f"k/{uuid.uuid4().hex[:8]}"
    v1 = legacy(owner_conn, signer, churn, key=ckey, is_head=False)
    v2 = legacy(owner_conn, signer, churn, key=ckey, version=2, parent_id=v1, is_head=False)
    legacy(owner_conn, signer, churn, key=ckey, version=3, parent_id=v2)
    _classify(app_conn)
    before = legacy_verdict.counts(app_conn)

    # The ephemeral rewrite DELETEs the key's chain; the prune DELETEs non-head versions.
    handlers.dispatch("memory.write", _envelope(peer, "memory.write",
                                                {"namespace": eph, "key": key, "value": {}}),
                      peer["peer_id"], app_conn)
    handlers.dispatch("memory.prune", _envelope(peer, "memory.prune",
                                                {"namespace": churn, "key": ckey}),
                      peer["peer_id"], app_conn)
    gone = owner_conn.execute("SELECT count(*) FROM shared_memory WHERE id = ANY(%s)",
                              ([old, v1, v2],)).fetchone()[0]
    owner_conn.commit()
    assert gone == 0, "control: both deletion paths really deleted the ledger rows"
    for rid in (old, v1, v2):
        assert verdict_of(owner_conn, rid)["verdict"] == "verified"
    assert legacy_verdict.counts(app_conn) == before, "the counts still see them"
    assert before["verified"] >= 3


# ====================================================== P: Python and SQL derive the same

CORPUS = [
    {}, {"status": "OPEN"}, {"status": None}, {"status": ["OPEN"]}, {"status": 5},
    {"pre_discipline": True}, {"pre_discipline": False}, {"pre_discipline": "false"},
    {"pre_discipline": None}, {"pre_discipline": 1},
    {"pre_discipline": True, "status": "OPEN"},
    {"pre_discipline": True, "claimed_status": "OPEN"},
    {"pre_discipline": True, "claimed_status": 5}, {"claimed_status": 5},
    {"tags": ["a", "b"]}, {"tags": []}, {"tags": "a, b"}, {"tags": ["a", 1]},
    {"tags": {"a": 1}}, {"tags": None},
    {"author": "x"}, {"author": ""}, {"author": "", "author_label": "y"},
    {"author_label": "y"}, {"author": 7}, {"author_label": ["y"]}, {"author": None},
    {"body": {"b": 1}}, {"value": {"v": 1}, "body": {"b": 1}},
    # Adversarial review of fin/b02-a1, finding 2: every list above was sorted and unique,
    # and no entry signed both author spellings non-empty, so a SQL rule that sorted the
    # tags, or either rule that let author_label win, agreed with the other on all of it.
    {"tags": ["zeta", "alpha", "alpha", ""]}, {"author": "x", "author_label": "y"},
]


@pytest.mark.parametrize("ns", ["plain/ns", WIKI])
@pytest.mark.parametrize("extra", CORPUS, ids=[json.dumps(c, sort_keys=True) for c in CORPUS])
def test_p_the_python_and_sql_derivations_agree(owner_conn, extra, ns):
    from apps.memoryskills.signed_bytes import derive_fields
    args = {"namespace": ns, "key": "k"}
    if "body" not in extra:
        args["value"] = {"x": 1}
    args.update(extra)
    p = {"args": args}
    py = derive_fields(p)
    for field, fn in (("pre_discipline", "sm_derive_pre_discipline"),
                      ("status", "sm_derive_status"),
                      ("claimed_status", "sm_derive_claimed_status"),
                      ("author_label", "sm_derive_author_label"),
                      ("tags", "sm_derive_tags"), ("value", "sm_signed_value")):
        try:
            got = owner_conn.execute(f"SELECT {fn}(%s)", (Jsonb(p),)).fetchone()[0]
            sql = ("ok", got)
        except psycopg.errors.CheckViolation as exc:
            sql = ("refused", str(exc).split(":")[1].strip())
        owner_conn.rollback()
        mine = py[field]
        # A refusal names the arg it could not read, which for status and claimed_status
        # can be pre_discipline: both rules read it first, in both languages. A comma-string
        # tags derives None in both (not the trigger's to check).
        want = ("refused", mine.refused) if mine.refused else ("ok", mine.value)
        assert sql == want, (field, extra, sql, want)


# ============================== Q11, Q6, Q7: what memory.read and memory.query tell a reader

def _reader(app_conn, owner_conn):
    """A peer that owns a fresh namespace (so it may read it) and signs with a real key."""
    from tests.test_memory_skills import _claim, _peer
    peer = _peer(owner_conn, "lvread")
    ns = f"lvtest/read-{uuid.uuid4().hex[:8]}"
    _claim(app_conn, peer, ns)
    return peer, ns


def _read(app_conn, peer, ns, key, *, include_provenance=True) -> dict:
    from apps.memoryskills import handlers
    from tests.test_memory_skills import _envelope
    args = {"namespace": ns, "key": key}
    if include_provenance:
        args["include_provenance"] = True
    return handlers.dispatch("memory.read", _envelope(peer, "memory.read", args),
                             peer["peer_id"], app_conn)["memo"]


def test_q11_a_new_row_is_bytes_bound_and_its_bytes_verify_where_the_reader_is(
        owner_conn, app_conn):
    """The reader gets the exact bytes, so it verifies them instead of re-canonicalising a
    JSON round trip, and derives the status itself (design 2.5). `bytes_bound` says the
    database held every derived field to those bytes; it does NOT say the signature
    verified (the database cannot check one)."""
    from apps.memoryskills import handlers
    from apps.memoryskills.signed_bytes import derive_fields
    from tests.test_memory_skills import _envelope
    peer, ns = _reader(app_conn, owner_conn)
    key = f"k/{uuid.uuid4().hex[:8]}"
    handlers.dispatch("memory.write", _envelope(peer, "memory.write", {
        "namespace": ns, "key": key, "value": {"x": 1}, "status": "PARTIAL", "tags": ["a"]}),
        peer["peer_id"], app_conn)
    memo = _read(app_conn, peer, ns, key)
    assert memo["status_basis"] == "bytes_bound"
    assert "status_basis_detail" not in memo
    raw = P.b64u_decode(memo["signed_bytes"])
    assert P.verify_strict(peer["pub"], raw, P.b64u_decode(memo["provenance"]["signature"]))
    derived = derive_fields(json.loads(raw))
    assert (derived["status"].value, derived["tags"].value) == (memo["status"], memo["tags"])
    P.jcs(memo)                                    # what W1 signs must be canonicalizable

    plain = _read(app_conn, peer, ns, key, include_provenance=False)
    assert not {"signed_bytes", "status_basis", "provenance"} & set(plain), "shape unchanged"


def test_q7_an_unclassified_legacy_row_is_legacy_unverified(owner_conn, app_conn):
    peer, ns = _reader(app_conn, owner_conn)
    key = f"k/{uuid.uuid4().hex[:8]}"
    legacy(owner_conn, peer, ns, key=key)
    memo = _read(app_conn, peer, ns, key)
    assert (memo["signed_bytes"], memo["status_basis"]) == (None, "legacy_unverified")
    assert memo["status_basis_detail"]["reason"] == ["not_classified"]


def test_q6_what_each_verdict_is_served_as(owner_conn, app_conn):
    """Q6 as amended: the column is served unchanged; legacy_contradicts only for a SIGNED
    disagreement, with the signed value beside the column; a defaulted one is
    legacy_verified with a note; a binding failure, a failed signature and a key revoked
    before the write are legacy_unverified, naming why."""
    peer, ns = _reader(app_conn, owner_conn)
    k = {n: f"k/{n}-{uuid.uuid4().hex[:6]}" for n in
         ("ok", "contra", "bind", "badsig", "comma")}
    legacy(owner_conn, peer, ns, key=k["ok"])
    legacy(owner_conn, peer, ns, key=k["contra"], extra_args={"status": "OPEN"},
           status="CONFIRMED")
    legacy(owner_conn, peer, ns, key=k["bind"], signed_key="elsewhere/key")
    legacy(owner_conn, peer, ns, key=k["badsig"], bad_signature=True)
    legacy(owner_conn, peer, ns, key=k["comma"], extra_args={"tags": "x, y"}, tags=["x"])
    _classify(app_conn)
    memos = {n: _read(app_conn, peer, ns, key) for n, key in k.items()}

    assert memos["ok"]["status_basis"] == "legacy_verified"
    c = memos["contra"]
    assert (c["status_basis"], c["status"]) == ("legacy_contradicts", "CONFIRMED")
    assert c["status_basis_detail"]["signed"] == {"status": "OPEN"}
    assert memos["bind"]["status_basis"] == "legacy_unverified"
    assert memos["bind"]["status_basis_detail"]["reason"] == ["binding_failures"]
    assert memos["bind"]["status_basis_detail"]["binding_failures"] == ["key"]
    assert memos["badsig"]["status_basis"] == "legacy_unverified"
    assert memos["badsig"]["status_basis_detail"]["reason"] == ["hash_bound_signature_fails"]
    assert memos["comma"]["status_basis"] == "legacy_verified"
    assert memos["comma"]["status_basis_detail"]["defaulted_differs"] == ["tags"]
    for memo in memos.values():
        assert memo["signed_bytes"] is None
        P.jcs(memo)


def test_q6_a_key_revoked_before_the_write_is_served_unverified(owner_conn, app_conn):
    peer, ns = _reader(app_conn, owner_conn)
    now = _dt.datetime.now(_dt.timezone.utc)
    key = f"k/{uuid.uuid4().hex[:8]}"
    legacy(owner_conn, peer, ns, key=key, created_at=now - _dt.timedelta(days=2))
    # A second active key for the reader, so revoking the first leaves it able to read.
    seed = os.urandom(32)
    pub = P.public_key_of(seed)
    _revoke(owner_conn, peer, now - _dt.timedelta(days=3))
    owner_conn.execute("INSERT INTO peer_keys (peer_id, kid, public_key, active) "
                       "VALUES (%s,%s,%s,TRUE)", (peer["peer_id"], P.kid_for_peer_key(pub),
                                                  P.b64u(pub)))
    owner_conn.commit()
    _classify(app_conn)
    fresh = dict(peer, seed=seed, pub=pub, kid=P.kid_for_peer_key(pub))
    memo = _read(app_conn, fresh, ns, key)
    assert memo["status_basis"] == "legacy_unverified"
    assert memo["status_basis_detail"]["reason"] == ["key_revoked_before_write"]


def test_q11_memory_query_carries_the_same_fields_per_result(owner_conn, app_conn):
    from apps.memoryskills import handlers
    from tests.test_memory_skills import _envelope
    peer, ns = _reader(app_conn, owner_conn)
    handlers.dispatch("memory.write", _envelope(peer, "memory.write", {
        "namespace": ns, "key": "new", "value": {"x": 1}}), peer["peer_id"], app_conn)
    legacy(owner_conn, peer, ns, key="old")
    out = handlers.dispatch("memory.query", _envelope(peer, "memory.query", {
        "namespace": ns, "include_provenance": True}), peer["peer_id"], app_conn)
    basis = {m["key"]: m["status_basis"] for m in out["results"]}
    assert basis == {"new": "bytes_bound", "old": "legacy_unverified"}
    plain = handlers.dispatch("memory.query", _envelope(peer, "memory.query", {
        "namespace": ns}), peer["peer_id"], app_conn)
    assert all("status_basis" not in m for m in plain["results"])


def test_q7_gates_keep_reading_the_column_of_an_unverified_row(owner_conn, app_conn):
    """Treating an unverified PROPOSED_FIX as absent would change what the closure rule
    allows on existing chains the day the server starts (design 3.4). An unclassified,
    NULL-bytes PROPOSED_FIX by this owner still makes this owner's FIXED a self-close."""
    from apps.memoryskills import handlers
    from tests.test_memory_skills import _envelope
    peer, ns = _reader(app_conn, owner_conn)
    key = f"k/{uuid.uuid4().hex[:8]}"
    legacy(owner_conn, peer, ns, key=key, extra_args={"status": "PROPOSED_FIX"})
    with pytest.raises(handlers.HandlerError) as refused:
        handlers.dispatch("memory.write", _envelope(peer, "memory.write", {
            "namespace": ns, "key": key, "value": {"x": 2}, "status": "FIXED"}),
            peer["peer_id"], app_conn)
    assert refused.value.code == "E_DISCIPLINE_VIOLATION"
    # Control: on a chain with no PROPOSED_FIX the same close is refused for THAT reason.
    key2 = f"k/{uuid.uuid4().hex[:8]}"
    legacy(owner_conn, peer, ns, key=key2, extra_args={"status": "OPEN"})
    with pytest.raises(handlers.HandlerError) as none:
        handlers.dispatch("memory.write", _envelope(peer, "memory.write", {
            "namespace": ns, "key": key2, "value": {"x": 2}, "status": "FIXED"}),
            peer["peer_id"], app_conn)
    assert none.value.code == "E_NO_PROPOSED_FIX"
