"""B02 phase A1 in the handler: memory.write stores the bytes it verified, and the rows it
writes satisfy the database's agree-trigger. Divergence 039, design
docs/design/signed-bytes-status.md (fin/design-signed-bytes) section 4.4.

  1   the stored bytes are the signed bytes: verify_strict over `signed_bytes` holds under
      the writer's published key, and their SHA-256 is `payload_jcs_sha256`;
  3   the shared/wiki default status derives (no signed status, OPEN stored, the trigger
      satisfied), and a custody write to shared/wiki keeps a NULL status;
  12  census: every INSERT into shared_memory under apps/ is handle_write's.

The key used to verify is read from peer_keys by this file, not taken from the handler's
provenance, so the check does not use the instrument it is checking (C5).

Run:  .venv/Scripts/python.exe -m pytest tests/test_signed_bytes.py -q
"""
from __future__ import annotations

import ast
import hashlib
import re
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from apps.crypto import primitives as P  # noqa: E402
from apps.memoryskills import handlers  # noqa: E402

from tests.test_memory_skills import (  # noqa: E402,F401
    _call, _claim, _envelope, _ns, _peer, app_conn, owner_conn, wiki_ns,
)


def _row(owner_conn, namespace: str, key: str) -> dict:
    cur = owner_conn.execute(
        "SELECT signed_bytes, payload_jcs_sha256, provenance_jsonb, status, pre_discipline, "
        "       claimed_status, tags, author_label "
        "FROM shared_memory WHERE namespace = %s AND key = %s AND is_head", (namespace, key))
    names = [d.name for d in cur.description]
    row = cur.fetchone()
    owner_conn.commit()
    assert row is not None, f"no head at {namespace}/{key}"
    return dict(zip(names, row))


def _published_key(owner_conn, peer_id, kid) -> bytes:
    got = owner_conn.execute("SELECT public_key FROM peer_keys WHERE peer_id = %s AND kid = %s",
                             (peer_id, kid)).fetchone()
    owner_conn.commit()
    return P.b64u_decode(got[0])


#: Values whose canonical form differs from json.dumps: an integral float, a small
#: exponent, a non-ASCII and an astral string, a key order RFC 8785 and sort_keys disagree
#: on (a BMP key after an astral one), a negative zero, nesting.
AWKWARD_VALUES = [
    {"text": "plain"},
    {"n": 1.0, "m": 100.0, "tiny": 1e-7, "neg0": -0.0, "f": 0.1},
    {"s": "caf\u00e9 \u2028 \U0001F600", "\U0001F600": 1, "\uff41": 2},
    {"nested": [1, [2, {"b": None, "a": True}], "x"]},
]


@pytest.mark.parametrize("value", AWKWARD_VALUES, ids=["plain", "numbers", "unicode", "nested"])
def test_1_the_stored_bytes_are_the_bytes_the_signature_covers(app_conn, owner_conn, value):
    peer = _peer(owner_conn, "sb1")
    ns = _ns("sb1")
    _claim(app_conn, peer, ns)
    key = f"k-{uuid.uuid4().hex[:8]}"
    env = _envelope(peer, "memory.write", {"namespace": ns, "key": key, "value": value})
    handlers.dispatch("memory.write", env, peer["peer_id"], app_conn)

    row = _row(owner_conn, ns, key)
    stored = bytes(row["signed_bytes"])
    assert stored == P.jcs(env["payload"]), "the column must be the RFC 8785 bytes that were signed"
    assert hashlib.sha256(stored).hexdigest() == row["payload_jcs_sha256"]
    pub = _published_key(owner_conn, peer["peer_id"], peer["kid"])
    signature = P.b64u_decode(row["provenance_jsonb"]["signature"])
    assert P.verify_strict(pub, stored, signature)
    # Control: the same check refuses bytes one byte away, so it is not vacuous.
    assert not P.verify_strict(pub, stored + b" ", signature)


def test_3_the_wiki_default_status_derives_and_custody_keeps_null(app_conn, owner_conn, wiki_ns):
    peer = wiki_ns
    key = f"wiki-{uuid.uuid4().hex[:8]}"
    out = _call(app_conn, peer, "memory.write",
                {"namespace": "shared/wiki", "key": key, "value": {"title": "t"}, "tags": ["x"]})
    row = _row(owner_conn, "shared/wiki", key)
    assert out["status"] == "OPEN" and row["status"] == "OPEN"
    assert "status" not in row["provenance_jsonb"]["payload"]["args"], "nothing signed OPEN"
    assert row["signed_bytes"] is not None

    # A custody write to shared/wiki (the owner is admin): no default, status NULL. The
    # SQL rule without its pre_discipline term derives OPEN here and refuses this write.
    key2 = f"wiki-{uuid.uuid4().hex[:8]}"
    _call(app_conn, peer, "memory.write",
          {"namespace": "shared/wiki", "key": key2, "value": {"title": "t"},
           "pre_discipline": True, "claimed_status": "CONFIRMED"})
    row2 = _row(owner_conn, "shared/wiki", key2)
    assert (row2["status"], row2["pre_discipline"], row2["claimed_status"]) == (
        None, True, "CONFIRMED")
    assert row2["signed_bytes"] is not None


# ============================================================ 12: every INSERT is the handler's

_INSERT = re.compile(r"INSERT\s+INTO\s+shared_memory(?![\w])", re.I)


def _inserts_under(root: Path) -> list[tuple[str, str]]:
    """(module, enclosing function) for every string constant under `root` that INSERTs
    into shared_memory itself (not shared_memory_grant, _namespace or _legacy_verdict)."""
    found = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text("utf-8"))
        module = ".".join(path.relative_to(ROOT).with_suffix("").parts)
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                        and _INSERT.search(node.value)):
                    found.append((module, fn.name))
        # A module-level constant is a statement some function will run: count it too.
        for node in tree.body:
            for sub in ast.walk(node):
                if (not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                        and isinstance(sub, ast.Constant) and isinstance(sub.value, str)
                        and _INSERT.search(sub.value)):
                    found.append((module, "<module>"))
    return found


def test_12_every_insert_into_shared_memory_under_apps_is_handle_writes():
    """Structural, and backed by test 2's database-level refusal, which catches a path this
    census misses (a string built at runtime)."""
    found = _inserts_under(ROOT / "apps")
    # The second inserter is memory.edit (divergence 059): a later version of a mail key, on the edit path, which sql_meshychat/029 lets
    # in only with the path's mark and its own checks (tests/test_edit_db_rule.py), and whose row carries the editor's signed request.
    assert set(found) == {("apps.memoryskills.handlers", "handle_write"), ("apps.memoryskills.edit", "handle_edit")}, found
    # Positive control: the same scan finds the tests' own direct inserts.
    in_tests = _inserts_under(ROOT / "tests")
    assert any(m == "tests.fixtures.signed_rows" for m, _ in in_tests), in_tests
    assert not _INSERT.search("INSERT INTO shared_memory_grant (namespace)")


# ================================================== 4: strict JSON types at the write seam (Q3)
#
# Before A1 the handler read these args by Python truthiness and str(): the STRING "false"
# made a namespace admin's write a custody row, a list status raised TypeError (E_INTERNAL
# over W1), and list tags stored Python reprs (design G3, probes P2b and C4). Each refusal
# below sits beside the same write with the arg correctly typed.

def _admin_world(app_conn, owner_conn):
    peer = _peer(owner_conn, "sb4")
    ns = _ns("sb4")
    _claim(app_conn, peer, ns)           # the claimant owns ns, so it is admin there
    return peer, ns


def _rows_at(owner_conn, ns, key) -> int:
    n = owner_conn.execute("SELECT count(*) FROM shared_memory WHERE namespace = %s AND "
                           "key = %s", (ns, key)).fetchone()[0]
    owner_conn.commit()
    return n


STRICT_CASES = {
    # case: (bad args, expected code, the same write correctly typed)
    "pre_discipline_string": ({"pre_discipline": "false"}, "E_INVALID_ARGS",
                              {"pre_discipline": False}),
    "pre_discipline_number": ({"pre_discipline": 1}, "E_INVALID_ARGS",
                              {"pre_discipline": True, "claimed_status": "OPEN"}),
    "status_list": ({"status": ["OPEN"]}, "E_INVALID_STATUS", {"status": "OPEN"}),
    "status_object": ({"status": {"s": "OPEN"}}, "E_INVALID_STATUS", {"status": "OPEN"}),
    "claimed_status_number": ({"pre_discipline": True, "claimed_status": 5}, "E_INVALID_ARGS",
                              {"pre_discipline": True, "claimed_status": "OPEN"}),
    "claimed_status_non_custody": ({"claimed_status": ["x"]}, "E_INVALID_ARGS",
                                   {"claimed_status": "x"}),
    "author_number": ({"author": 7}, "E_INVALID_ARGS", {"author": "seven"}),
    "author_label_list": ({"author_label": ["a"]}, "E_INVALID_ARGS", {"author_label": "a"}),
    "tags_mixed_list": ({"tags": [1, True, None, {"a": 1}]}, "E_INVALID_ARGS",
                        {"tags": ["1", "True"]}),
}


@pytest.mark.parametrize("case", sorted(STRICT_CASES))
def test_4_an_arg_of_the_wrong_json_type_is_refused_typed_and_writes_nothing(
        case, app_conn, owner_conn):
    peer, ns = _admin_world(app_conn, owner_conn)
    bad, code, good = STRICT_CASES[case]
    key = f"k-{uuid.uuid4().hex[:8]}"
    with pytest.raises(handlers.HandlerError) as refused:
        _call(app_conn, peer, "memory.write", {"namespace": ns, "key": key, "value": {"x": 1},
                                               **bad})
    assert refused.value.code == code, refused.value
    assert _rows_at(owner_conn, ns, key) == 0

    out = _call(app_conn, peer, "memory.write", {"namespace": ns, "key": key,
                                                 "value": {"x": 1}, **good})
    assert out["op"] == "memory.write" and _rows_at(owner_conn, ns, key) == 1


def test_4_false_is_not_custody_and_comma_tags_are_still_accepted(app_conn, owner_conn):
    """The two spellings Q3 keeps: a JSON false, and a comma-string tags arg (normalised
    by the handler; not checked by the trigger, nor by anything else on a row with bytes)."""
    peer, ns = _admin_world(app_conn, owner_conn)
    key = f"k-{uuid.uuid4().hex[:8]}"
    out = _call(app_conn, peer, "memory.write", {"namespace": ns, "key": key, "value": {},
                                                 "pre_discipline": False, "tags": " a, b ,,c"})
    assert out["pre_discipline"] is False
    row = _row(owner_conn, ns, key)
    assert (row["pre_discipline"], row["tags"]) == (False, ["a", "b", "c"])


# ============================== 5: numbers RFC 8785 cannot hold as integers, refused (Q4)
#
# 2**53 - 1 is the largest integer an IEEE double holds exactly. A float at or beyond 2**53
# is integral, JSONB stores it as that integer, psycopg reads it back as a Python int, and
# rfc8785 refuses the int: the row was written and then no memory.read of it could be
# signed, and a W1 retry of the write answered 500 (design G1, probe P1).

MAX_SAFE = 2**53 - 1


@pytest.mark.parametrize("value", [
    {"n": 1e16}, {"n": -1e16}, {"n": float(2**53)}, {"n": 1.5e300},
    {"deep": [1, {"n": 1e20}]},
], ids=["1e16", "neg1e16", "2^53-float", "1.5e300", "nested"])
def test_5_a_number_beyond_2_53_minus_1_is_refused_at_the_seam(app_conn, owner_conn, value):
    peer, ns = _admin_world(app_conn, owner_conn)
    key = f"k-{uuid.uuid4().hex[:8]}"
    with pytest.raises(handlers.HandlerError) as refused:
        _call(app_conn, peer, "memory.write", {"namespace": ns, "key": key, "value": value})
    assert refused.value.code == "E_NUMBER_OUT_OF_RANGE", refused.value
    assert _rows_at(owner_conn, ns, key) == 0


def _sort_keys_envelope(peer, args):
    """Build a non-canonical signed envelope: json.dumps(sort_keys=True) bytes carry any
    int. rfc8785 cannot serialise this payload, so no RFC 8785 signature over it exists."""
    import json
    env = _envelope(peer, "memory.write", dict(args, value={"n": 0}))
    env["payload"]["args"] = args
    raw = json.dumps(env["payload"], sort_keys=True, separators=(",", ":")).encode()
    env["signature"] = P.b64u(P.sign(peer["seed"], raw))
    return env


def test_5_an_integer_beyond_2_53_minus_1_is_refused_typed_not_internal(app_conn, owner_conn):
    """The int form never reached the table before (rfc8785 raised while hashing, an
    untyped fault); it is refused with the same code as the float form, before the
    signature is looked at."""
    peer, ns = _admin_world(app_conn, owner_conn)
    key = f"k-{uuid.uuid4().hex[:8]}"
    env = _sort_keys_envelope(peer, {"namespace": ns, "key": key, "value": {"n": 2**53}})
    with pytest.raises(handlers.HandlerError) as refused:
        handlers.dispatch("memory.write", env, peer["peer_id"], app_conn)
    assert refused.value.code == "E_NUMBER_OUT_OF_RANGE"
    assert _rows_at(owner_conn, ns, key) == 0


@pytest.mark.parametrize("value", [
    {"n": 1e15}, {"n": MAX_SAFE}, {"n": -MAX_SAFE}, {"n": float(MAX_SAFE)}, {"n": 0.5},
], ids=["1e15", "max-int", "neg-max-int", "max-float", "half"])
def test_5_the_largest_safe_numbers_are_written_and_read_back(app_conn, owner_conn, value):
    """The control: at and below 2**53 - 1 the write lands and memory.read returns the
    value, with provenance, and the read result is canonicalizable (what W1 signs)."""
    peer, ns = _admin_world(app_conn, owner_conn)
    key = f"k-{uuid.uuid4().hex[:8]}"
    _call(app_conn, peer, "memory.write", {"namespace": ns, "key": key, "value": value})
    out = _call(app_conn, peer, "memory.read", {"namespace": ns, "key": key,
                                                "include_provenance": True})
    assert out["memo"]["value"] == value
    P.jcs(out)          # raises if the read result could not be signed


# ================================= the SQL rule agrees with the handler on legitimate writes
#
# Adversarial review of fin/b02-a1, finding 2. A SQL rule that drifts from the handler's
# refuses a legitimate write with an untyped 500. Every list the suite wrote was sorted and
# unique, and no write signed both author spellings, so a trigger that sorted the tags (M1)
# or let author_label win (M2) passed it all.

def test_2_a_tags_list_lands_in_the_order_and_with_the_repeats_the_writer_signed(
        app_conn, owner_conn):
    peer, ns = _admin_world(app_conn, owner_conn)
    key = f"k-{uuid.uuid4().hex[:8]}"
    tags = ["zeta", "alpha", "alpha", ""]
    out = _call(app_conn, peer, "memory.write",
                {"namespace": ns, "key": key, "value": {"x": 1}, "tags": tags})
    assert out["tags"] == tags
    row = _row(owner_conn, ns, key)
    assert row["tags"] == tags and row["signed_bytes"] is not None


def test_2_a_signed_author_wins_over_a_signed_author_label(app_conn, owner_conn):
    """handle_write reads `author or author_label`; the trigger must pick the same one."""
    peer, ns = _admin_world(app_conn, owner_conn)
    key = f"k-{uuid.uuid4().hex[:8]}"
    out = _call(app_conn, peer, "memory.write",
                {"namespace": ns, "key": key, "value": {"x": 1},
                 "author": "first-spelling", "author_label": "second-spelling"})
    assert out["author_label"] == "first-spelling"
    assert _row(owner_conn, ns, key)["author_label"] == "first-spelling"


# ======================================== 16 (and G4): the write result and the replay result

RESULT_FIELDS = ("op", "namespace", "key", "version", "id", "status", "pre_discipline",
                 "claimed_status", "author_label", "tags", "created_at")


@pytest.mark.parametrize("args", [
    {"status": "PARTIAL", "tags": "b, a"},
    {"pre_discipline": True, "claimed_status": "CONFIRMED", "tags": ["t"]},
    {"author": "a-signed-author"},
], ids=["status-comma-tags", "custody", "signed-author"])
def test_16_a_replay_answers_what_the_first_write_answered(app_conn, owner_conn, args):
    """The handler's dedup replay is W3's answer to a re-sent act (apps/board/declare.declare
    calls handlers.dispatch): before A1 it carried op, namespace, key, version, is_head, id
    and replay only, so a replayed write never said what status, author or tags the row
    holds (design 1.3 and finding 11). The first write lacked author_label and tags (G4)."""
    peer, ns = _admin_world(app_conn, owner_conn)
    key = f"k-{uuid.uuid4().hex[:8]}"
    env = _envelope(peer, "memory.write", {"namespace": ns, "key": key, "value": {"x": 1},
                                           **args})
    first = handlers.dispatch("memory.write", env, peer["peer_id"], app_conn)
    again = handlers.dispatch("memory.write", env, peer["peer_id"], app_conn)
    assert (first["replay"], again["replay"]) == (False, True)
    assert {f: again.get(f, "<absent>") for f in RESULT_FIELDS} == {
        f: first.get(f, "<absent>") for f in RESULT_FIELDS}

    row = _row(owner_conn, ns, key)
    assert first["author_label"] == row["author_label"]
    assert first["tags"] == row["tags"]
    assert (first["status"], first["pre_discipline"], first["claimed_status"]) == (
        row["status"], row["pre_discipline"], row["claimed_status"])
    if "author" not in args:
        assert first["author_label"] == f"peer-{str(peer['peer_id'])[:8]}", "the server's stamp"


# ================================ the replay answers only the signer who wrote the row (16)
#
# Adversarial review of fin/b02-a1, finding 4. The dedup finds a row by payload hash OR
# call_id in any namespace. A matching call_id can belong to another signer. Before A1 it
# answered the row as the caller's own replay whoever the caller was. That answer named
# the other row's namespace, key, id and version; since test 16 it also carries its status,
# custody, author label, tags and creation time. Knowing someone's call_id (a uuid4 by
# default) was enough to read those fields of a row the caller cannot read, and the
# caller's own write silently did not land.

SECRET = {"status": "OPEN", "tags": ["client-acme", "incident-7"],
          "author": "alice-private-label"}


def _alice_and_bob(app_conn, owner_conn):
    alice, ns_a = _admin_world(app_conn, owner_conn)
    bob, ns_b = _admin_world(app_conn, owner_conn)
    cid = f"call-{uuid.uuid4()}"
    first = _call(app_conn, alice, "memory.write",
                  {"namespace": ns_a, "key": "secret-key", "value": {"x": 1}, **SECRET},
                  call_id=cid)
    with pytest.raises(handlers.HandlerError) as denied:                # control
        _call(app_conn, bob, "memory.read", {"namespace": ns_a, "key": "secret-key"})
    assert denied.value.code == "E_PERMISSION_DENIED"
    return alice, ns_a, bob, ns_b, cid, first


def _assert_refused_without_the_row(refused, ns_a) -> None:
    assert refused.value.code == "E_REPLAY", refused.value
    text = str(refused.value)
    for leaked in (ns_a, "secret-key", "client-acme", "incident-7", "alice-private-label"):
        assert leaked not in text, leaked


def test_16_another_signers_call_id_is_refused_not_answered_with_their_row(
        app_conn, owner_conn):
    alice, ns_a, bob, ns_b, cid, first = _alice_and_bob(app_conn, owner_conn)
    with pytest.raises(handlers.HandlerError) as refused:
        _call(app_conn, bob, "memory.write",
              {"namespace": ns_b, "key": "mine", "value": {"y": 2}}, call_id=cid)
    _assert_refused_without_the_row(refused, ns_a)
    assert _rows_at(owner_conn, ns_b, "mine") == 0

    # One variable apart, the signer: alice re-sending under the same call_id is a replay.
    again = _call(app_conn, alice, "memory.write",
                  {"namespace": ns_a, "key": "secret-key", "value": {"x": 1}, **SECRET},
                  call_id=cid)
    assert (again["replay"], again["id"], again["tags"]) == (True, first["id"], SECRET["tags"])


def test_16_the_concurrent_write_path_refuses_another_signers_call_id_too(
        app_conn, owner_conn, monkeypatch):
    """The same rule on the second place a replay is answered: the read-before-write
    misses (a concurrent insert), the INSERT meets the UNIQUE index on the signed call_id,
    and handle_write re-reads the dedup to tell a replay from a race."""
    alice, ns_a, bob, ns_b, cid, _ = _alice_and_bob(app_conn, owner_conn)
    real, seen = handlers._existing_by_dedup, []

    def the_first_read_misses(conn, jcs_sha, call_id):
        seen.append(call_id)
        return None if len(seen) == 1 else real(conn, jcs_sha, call_id)

    monkeypatch.setattr(handlers, "_existing_by_dedup", the_first_read_misses)
    with pytest.raises(handlers.HandlerError) as refused:
        _call(app_conn, bob, "memory.write",
              {"namespace": ns_b, "key": "mine", "value": {"y": 2}}, call_id=cid)
    assert seen == [cid, cid], "control: the INSERT ran and the concurrent path re-read"
    _assert_refused_without_the_row(refused, ns_a)
    assert _rows_at(owner_conn, ns_b, "mine") == 0
