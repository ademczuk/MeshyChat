"""B02 phase 0: the reference client checks a row's status fields against the args its
author signed (docs/design/signed-bytes-status.md on fin/design-signed-bytes, section 4.1
phase 0, Q13, test 18; the operator approved all fourteen recommendations).

THE GAP (design 1.6, probe P4). `wire.author_signature_ok` proves the stored namespace, key
and value are what the row's writer signed. It says nothing about the row's `status`,
`claimed_status`, `pre_discipline`, `tags` or `author_label`: a memo whose status reads
CONFIRMED while the signed args say OPEN, or whose tags or author label were changed,
still reads `signature_verified: true`. Those fields are either the signed args or a pure
function of them (design 2.3), so a client that has verified the payload can derive them
and compare, today, with no server change, for historical rows as well, and no server can
downgrade that check (Q13).

WHAT `read` NOW PRINTS after `signature_verified` / `signed_by` / `signature_error`:
  * `status_verified`: true only when the author's signature verified in this call AND
    every one of the five fields the memo serves equals the value 2.3's rules derive from
    the verified args. The rules are memory.write's own (apps/memoryskills/handlers.py
    handle_write): pre_discipline absent or null is false, a JSON boolean is itself;
    status is the signed string, else "OPEN" for a non-custody shared/wiki write, else null;
    claimed_status is the signed string on a custody write only; tags are the signed list
    of strings, or the comma string split and stripped, else []; author_label is the signed
    `author` (else `author_label`) when it is a non-empty string, and otherwise the SERVER
    stamped it from state the client does not have, so it is not compared.
  * `status_error` when false, "<CODE>: <field>: <why>; ...", naming every field:
      E_STATUS_CONTRADICTS_SIGNATURE  a field whose arg the writer SIGNED differs (the
                                      #922 shape: a valid signature over another status)
      E_STATUS_DEFAULT_DIFFERS        the writer signed no such arg (or signed a
                                      claimed_status off a custody write, which
                                      memory.write discards), and the row differs from the
                                      rule's default: reported as a default, not a
                                      contradiction (design 3.3 step 6: rule drift)
      E_STATUS_UNDERIVABLE            a signed arg the rules refuse (a non-boolean
                                      pre_discipline, a non-string status, ...: Q3)
      E_STATUS_NOT_SERVED             the memo does not carry the field
    or, when the author's signature did not verify, that signature's own code: nothing was
    compared (a rotated key is E_SIGNATURE_KEY_UNAVAILABLE, never a contradiction).
  * `author_signed`: true only when the signature verified, the writer signed an author
    label, and it is the one printed as `author`.

Every refusal sits beside the control one variable away (docs/V1-DESIGN-RULES.md C1). The
fake server signs through the SERVER's primitives, not wire.py (C5). Synthetic data only.

Run:  .venv/Scripts/python.exe -m pytest tests/test_meshy_node_status.py -q -p no:cacheprovider
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_meshy_node import (ABSENT, AUTHOR_PEER, HITS, PRINTED,  # noqa: E402
                                   READ_KEY, STORED, _FakeMemory, _state_file)
from tools.meshy_node import cli, node, wire  # noqa: E402

MESH = "shared/mesh-chat"
WIKI = "shared/wiki"
CONTRADICTS = "E_STATUS_CONTRADICTS_SIGNATURE"
DEFAULT_DIFFERS = "E_STATUS_DEFAULT_DIFFERS"
UNDERIVABLE = "E_STATUS_UNDERIVABLE"
NOT_SERVED = "E_STATUS_NOT_SERVED"
#: What memory.write stores for a write that signs namespace, key and value only, outside
#: shared/wiki: handle_write's defaults, as handle_read serves them (_row_to_memo).
DEFAULTS = {"status": None, "tags": [], "author_label": "peer-5a5a5a5a",
            "pre_discipline": False, "claimed_status": None}
KEY = "note/phase0-probe"


def _memo(signed: dict, served: dict, *, namespace: str = MESH) -> dict:
    """A memo as memory.read serves it with include_provenance: the served fields are
    DEFAULTS updated by `served` (ABSENT drops one), and the provenance payload's args are
    namespace, key and value plus `signed`. The signature itself is the caller's verdict
    (status_check takes it): these tests are about the derivation, and the fake-server
    tests below run the whole read with a real signature."""
    memo = {"namespace": namespace, "key": KEY, "version": 1, "value": STORED,
            "created_by_peer_id": AUTHOR_PEER, **DEFAULTS,
            "provenance": {"payload": {
                "v": 1, "kind": "skill.call", "skill_name": "memory.write",
                "caller_peer_id": AUTHOR_PEER,
                "args": {"namespace": namespace, "key": KEY, "value": STORED, **signed}},
                "signature": "synthetic", "signing_kid": "desktop-author"}}
    for field, value in served.items():
        if value is ABSENT:
            memo.pop(field)
        else:
            memo[field] = value
    return memo


def _check(memo, verified: bool = True, why: str | None = None) -> dict:
    return wire.status_check(memo, verified, why)


# ------------------------------------------------------------------ the rules, field by field

#: id, namespace, signed args, the served fields that AGREE with them, the field then
#: changed, its changed value, the code that change must read. Each row is a control (the
#: agreeing memo reads true) and a refusal one variable away.
DISAGREEMENTS = [
    ("status-signed", MESH, {"status": "OPEN"}, {"status": "OPEN"},
     "status", "CONFIRMED", CONTRADICTS),
    ("status-wiki-default", WIKI, {"tags": ["ops"]}, {"status": "OPEN", "tags": ["ops"]},
     "status", None, DEFAULT_DIFFERS),
    ("status-custody-wiki", WIKI, {"pre_discipline": True, "tags": ["ops"]},
     {"pre_discipline": True, "status": None, "tags": ["ops"]},
     "status", "OPEN", DEFAULT_DIFFERS),
    ("status-mesh-default", MESH, {}, {}, "status", "CONFIRMED", DEFAULT_DIFFERS),
    ("pre-signed-false", MESH, {"pre_discipline": False}, {},
     "pre_discipline", True, CONTRADICTS),
    ("pre-retyped", MESH, {"pre_discipline": True, "claimed_status": "OPEN"},
     {"pre_discipline": True, "claimed_status": "OPEN"}, "pre_discipline", 1, CONTRADICTS),
    ("pre-default", MESH, {}, {}, "pre_discipline", True, DEFAULT_DIFFERS),
    ("claimed-custody", MESH, {"pre_discipline": True, "claimed_status": "OPEN"},
     {"pre_discipline": True, "claimed_status": "OPEN"},
     "claimed_status", "CONFIRMED", CONTRADICTS),
    ("claimed-off-custody", MESH, {"claimed_status": "OPEN"}, {},
     "claimed_status", "OPEN", DEFAULT_DIFFERS),
    ("tags-list", MESH, {"tags": ["ops", "lease"]}, {"tags": ["ops", "lease"]},
     "tags", ["ops", "leash"], CONTRADICTS),
    ("tags-order", MESH, {"tags": ["ops", "lease"]}, {"tags": ["ops", "lease"]},
     "tags", ["lease", "ops"], CONTRADICTS),
    ("tags-comma", MESH, {"tags": " ops, lease ,,x"}, {"tags": ["ops", "lease", "x"]},
     "tags", [" ops", " lease ", "", "x"], CONTRADICTS),
    ("tags-default", MESH, {}, {}, "tags", ["ops"], DEFAULT_DIFFERS),
    ("author", MESH, {"author": "desktop-a"}, {"author_label": "desktop-a"},
     "author_label", "desktop-b", CONTRADICTS),
    ("author-label-arg", MESH, {"author_label": "desktop-a"}, {"author_label": "desktop-a"},
     "author_label", "desktop-b", CONTRADICTS),
    ("author-empty-falls-back", MESH, {"author": "", "author_label": "desktop-a"},
     {"author_label": "desktop-a"}, "author_label", "desktop-b", CONTRADICTS),
    ("author-null", MESH, {"author": "desktop-a"}, {"author_label": "desktop-a"},
     "author_label", None, CONTRADICTS),
    # Review of 0b08f4e, finding 1: shapes an HONEST write stores exactly so (handle_write
    # keeps a list of tags as sent, norm_tags strips a comma string's parts with Python's
    # str.strip, and `author` wins over `author_label`). Each control reads true; the
    # differential in tests/test_meshy_node_status_differential.py writes the same shapes
    # through the real handler.
    ("tags-list-duplicates", MESH, {"tags": ["ops", "ops"]}, {"tags": ["ops", "ops"]},
     "tags", ["ops"], CONTRADICTS),
    ("tags-list-padded", MESH, {"tags": [" ops "]}, {"tags": [" ops "]},
     "tags", ["ops"], CONTRADICTS),
    ("tags-comma-unicode-space", MESH, {"tags": "ops,\tlease,\u00a0x"},
     {"tags": ["ops", "lease", "x"]}, "tags", ["ops", "\tlease", "\u00a0x"], CONTRADICTS),
    ("author-and-label-both-signed", MESH, {"author": "desktop-a", "author_label": "desktop-b"},
     {"author_label": "desktop-a"}, "author_label", "desktop-b", CONTRADICTS),
    # Finding 2: an EMPTY signed arg is still signed. The writer signed tags [] (or ""),
    # so other tags contradict it; the writer signed status "", which memory.write
    # refuses, so a row serving the wiki default OPEN for it contradicts the signature.
    ("tags-signed-empty-list", MESH, {"tags": []}, {}, "tags", ["ops"], CONTRADICTS),
    ("tags-signed-empty-string", MESH, {"tags": ""}, {}, "tags", ["ops"], CONTRADICTS),
    ("status-signed-empty", WIKI, {"status": "", "tags": ["ops"]},
     {"status": "", "tags": ["ops"]}, "status", "OPEN", CONTRADICTS),
]


@pytest.mark.parametrize("namespace,signed,agreeing,field,changed,code",
                         [row[1:] for row in DISAGREEMENTS],
                         ids=[row[0] for row in DISAGREEMENTS])
def test_status_verified_is_false_with_a_typed_code_on_each_disagreement(
        namespace, signed, agreeing, field, changed, code):
    """Design test 18, field by field. The memo that agrees with the rules reads
    `status_verified: true`; the same memo with ONE served field changed reads false,
    with the code that says whether the writer signed that field (a contradiction) or the
    server defaulted it (a default that differs), and the field named.
    Kills: any field left out of the comparison; the wiki default without its custody
    term (status-custody-wiki's control); claimed_status taken off a non-custody write
    (claimed-off-custody); tags compared as a set (tags-order); the comma form not split
    and stripped (tags-comma); `author_label` ignored as the author arg
    (author-label-arg); a Python `==` that reads 1 as true (pre-retyped); a default
    reported as a contradiction and back (the *-default rows). Review of 0b08f4e: list
    tags deduplicated (tags-list-duplicates) or stripped (tags-list-padded); comma parts
    stripped of spaces only, not Python's whitespace (tags-comma-unicode-space);
    `author_label` read before `author` (author-and-label-both-signed); an empty signed
    list, string or status taken for none (tags-signed-empty-*, status-signed-empty)."""
    got = _check(_memo(signed, agreeing, namespace=namespace))
    assert got["status_verified"] is True and "status_error" not in got, ("control", got)

    got = _check(_memo(signed, {**agreeing, field: changed}, namespace=namespace))
    assert got["status_verified"] is False, got
    assert got["status_error"].startswith(code + ": " + field + ": "), got["status_error"]


#: id, signed args the rules refuse, the served fields memory.write stores for them today
#: (or would, where it refuses the write), the field the error must name first, and the
#: derivable twin of the same args with its served fields (the control).
REFUSED = [
    ("pre-string", {"pre_discipline": "false"}, {"pre_discipline": True}, "pre_discipline",
     {"pre_discipline": False}, {}),
    ("pre-number", {"pre_discipline": 0}, {}, "pre_discipline", {"pre_discipline": False}, {}),
    ("pre-string-custody-served", {"pre_discipline": "yes", "claimed_status": "OPEN"},
     {"pre_discipline": True, "claimed_status": "OPEN"}, "pre_discipline",
     {"pre_discipline": True, "claimed_status": "OPEN"},
     {"pre_discipline": True, "claimed_status": "OPEN"}),
    ("status-list", {"status": ["OPEN"]}, {"status": "OPEN"}, "status",
     {"status": "OPEN"}, {"status": "OPEN"}),
    ("status-on-custody", {"pre_discipline": True, "status": "OPEN"},
     {"pre_discipline": True}, "status", {"pre_discipline": True}, {"pre_discipline": True}),
    ("claimed-number", {"pre_discipline": True, "claimed_status": 5},
     {"pre_discipline": True, "claimed_status": "5"}, "claimed_status",
     {"pre_discipline": True, "claimed_status": "5"},
     {"pre_discipline": True, "claimed_status": "5"}),
    ("tags-non-string", {"tags": ["ops", 1, True]}, {"tags": ["ops", "1", "True"]}, "tags",
     {"tags": ["ops", "1", "True"]}, {"tags": ["ops", "1", "True"]}),
    ("tags-object", {"tags": {"a": 1}}, {}, "tags", {"tags": []}, {}),
    ("author-number", {"author": 5}, {"author_label": "5"}, "author_label",
     {"author": "5"}, {"author_label": "5"}),
]


@pytest.mark.parametrize("signed,served,field,twin,twin_served",
                         [row[1:] for row in REFUSED], ids=[row[0] for row in REFUSED])
def test_a_signed_arg_the_rules_refuse_is_underivable_not_true(signed, served, field, twin,
                                                               twin_served):
    """Design 2.3 and Q3: pre_discipline must be a JSON boolean, status and claimed_status
    strings, tags a list of strings or a comma string, author a string, and a custody
    write carries no status. memory.write today coerces instead (G3: the string "false"
    stores a custody row, a list of tags stores Python reprs), so a row can exist whose
    served field matches the coercion. The client does not reproduce the coercion: it
    says it could not derive the field (E_STATUS_UNDERIVABLE, the field named), never
    true. Control: the derivable twin of the same args reads true.
    Kills: pre_discipline by Python truthiness (pre-string); str() of each tag
    (tags-non-string); the custody refusal dropped (status-on-custody). Review of 0b08f4e,
    finding 2: status and claimed_status taken as defaults when pre_discipline is
    underivable. They depend on it, so the error names them as underivable too, and every
    field it names is one this row's args make underivable (the pre-* rows)."""
    got = _check(_memo(twin, twin_served))
    assert got["status_verified"] is True, ("control: the derivable twin", got)

    got = _check(_memo(signed, served))
    assert got["status_verified"] is False, got
    assert got["status_error"].startswith(f"{UNDERIVABLE}: {field}: "), got["status_error"]
    depends = ["status", "claimed_status"] if field == "pre_discipline" else []
    assert _named(got["status_error"]) == [field, *depends], got["status_error"]
    if depends:
        assert got["status_error"].endswith(
            "status: it depends on pre_discipline, which is underivable; claimed_status: "
            "it depends on pre_discipline, which is underivable"), got["status_error"]


def _named(error: str) -> list[str]:
    """The fields a status_error names, in order: "<CODE>: <field>: <why>; <field>: ..."."""
    return re.findall(r"(?:^E_[A-Z_]+: |; )([a-z_]+): ", error)


@pytest.mark.parametrize("field", ["status", "tags", "author_label", "pre_discipline",
                                   "claimed_status"])
def test_a_field_the_memo_does_not_carry_is_not_served_not_null(field):
    """A memo that omits a field is not a memo whose field is null: a server that drops
    `status` would otherwise read true for every row whose derived status is null.
    E_STATUS_NOT_SERVED, the field named. Control: the same memo carrying it.
    Kills: `memo.get(field)` in place of a presence check."""
    assert _check(_memo({}, {}))["status_verified"] is True, "control"
    got = _check(_memo({}, {field: ABSENT}))
    assert got["status_verified"] is False, got
    assert got["status_error"] == f"{NOT_SERVED}: {field}: the memo does not carry it", got


def test_a_claimed_status_memory_write_discards_is_named_as_signed_not_as_none():
    """Review of 0b08f4e, finding 3. Off a custody write memory.write discards a signed
    claimed_status (handle_write keeps it only when pre_discipline), so the rule gives
    null, and a row serving one is a default that differs: that verdict and code were
    right. The reason said "the writer signed none", which is false: the writer signed
    one. It now names what was signed and why the rule does not keep it, and the phrase
    "signed none" is absent. Control, one variable away: the same row where the writer
    signed no claimed_status still says "signed none".
    Kills: the discarded arg reported with the unsigned text (0b08f4e itself), or as a
    contradiction of the signature (the row serves exactly what was signed)."""
    got = _check(_memo({"claimed_status": "OPEN"}, {"claimed_status": "OPEN"}))
    assert got["status_verified"] is False, got
    assert got["status_error"] == (
        f'{DEFAULT_DIFFERS}: claimed_status: the writer signed "OPEN", which memory.write '
        'keeps on a custody write only, so its rule gives null, the row says "OPEN"'), got
    assert "signed none" not in got["status_error"], got

    got = _check(_memo({"claimed_status": 5}, {"claimed_status": "5"}))
    assert got["status_error"].startswith(
        f"{DEFAULT_DIFFERS}: claimed_status: the writer signed 5, which memory.write "), got

    control = _check(_memo({}, {"claimed_status": "OPEN"}))
    assert control["status_error"] == (
        f"{DEFAULT_DIFFERS}: claimed_status: the writer signed none, so memory.write's "
        'rule gives null, the row says "OPEN"'), ("control", control)


def test_every_disagreement_is_named_and_a_contradiction_outranks_the_rest():
    """One memo, three problems: a signed status contradicted, default tags that differ,
    and an author the rules refuse. The code is the most serious (a contradiction of what
    was signed) and the reason names all three fields, so fixing one does not hide the
    next. Kills: the first problem found returned alone."""
    got = _check(_memo({"status": "OPEN", "author": 5},
                       {"status": "CONFIRMED", "tags": ["ops"], "author_label": "5"}))
    assert got["status_verified"] is False, got
    error = got["status_error"]
    assert error.startswith(CONTRADICTS + ": status: "), error
    assert "; tags: " in error and "; author_label: " in error, error


# ------------------------------------------------------------------ never true unverified

@pytest.mark.parametrize("why", [
    "E_SIGNATURE_KEY_UNAVAILABLE: the keys peer x publishes hold no Ed25519 key 'k'",
    "E_SIGNATURE_INVALID: the envelope's signature does not verify",
    "E_SIGNATURE_NOT_THE_MESSAGE: the stored value is not the value the envelope wrote",
    "E_SIGNATURE_NO_ENVELOPE: the row carries no signed envelope"])
def test_nothing_is_compared_when_the_authors_signature_did_not_verify(why):
    """Design test 18's last clause: a rotated key is E_SIGNATURE_KEY_UNAVAILABLE, not a
    contradiction. Whatever the signature's failure, `status_verified` is false with THAT
    code, for a memo that agrees with its args and for one that contradicts them alike,
    and `author_signed` is false. Control: the agreeing memo with the signature verified.
    Kills: the comparison run on an unverified payload (it reads true for the agreeing
    memo, and CONTRADICTS for the other)."""
    code = why.split(":", 1)[0]
    agreeing = _memo({"status": "OPEN", "author": "desktop-a"},
                     {"status": "OPEN", "author_label": "desktop-a"})
    assert _check(agreeing) == {"status_verified": True, "author_signed": True}, "control"
    contradicting = _memo({"status": "OPEN"}, {"status": "CONFIRMED"})
    for memo in (agreeing, contradicting):
        got = _check(memo, False, why)
        assert got["status_verified"] is False and got["author_signed"] is False, got
        assert got["status_error"].startswith(code + ": "), got["status_error"]
        assert CONTRADICTS not in got["status_error"], got["status_error"]


@pytest.mark.parametrize("memo", [
    None, {}, {"provenance": None}, {"provenance": {"payload": {"args": "not an object"}}},
    {**_memo({}, {}), "tags": [float("nan")]},
], ids=["none", "empty", "no-provenance", "args-not-object", "tags-not-json"])
def test_the_check_never_raises_on_what_the_server_sent(memo):
    """Like the signature checks, it fails closed on malformed input: a memo that is not
    one, a verdict of true beside a payload with no args, a served value RFC 8785 cannot
    serialise. Each is `status_verified: false`, never an exception that ends the listen
    or the inbox reading it."""
    got = _check(memo, True, None)
    assert got["status_verified"] is False and got["author_signed"] is False, got
    assert got["status_error"].split(":", 1)[0] in (UNDERIVABLE, NOT_SERVED), got


# ------------------------------------------------------------------ author_signed

def test_author_signed_is_true_only_for_a_signed_label_that_is_the_one_printed():
    """`author` is a label. `author_signed` says the writer signed THIS label: false when
    the server stamped it (no author arg: the roster or `peer-<id8>`, which the client
    cannot derive, so it is not compared and does not make status_verified false); true
    when the signed label is the served one; false when they differ (and status_verified
    is then false too). Kills: author_signed read from the args alone."""
    assert _check(_memo({}, {})) == {"status_verified": True, "author_signed": False}
    assert _check(_memo({"author": "desktop-a"}, {"author_label": "desktop-a"})) == {
        "status_verified": True, "author_signed": True}
    got = _check(_memo({"author": "desktop-a"}, {"author_label": "desktop-b"}))
    assert got["status_verified"] is False and got["author_signed"] is False, got


# ------------------------------------------------------------------ the whole read, signed

@pytest.fixture()
def fake_memory():
    fake = _FakeMemory()
    try:
        yield fake
    finally:
        fake.close()


def _read(fake: _FakeMemory, path: Path, capsys) -> dict:
    assert cli.main(["read", "--base-url", fake.base_url, "--state-file", str(path),
                     "--key", READ_KEY]) == 0
    return json.loads(capsys.readouterr().out)


SIGNED = {"status": "OPEN", "tags": ["ops", "lease"], "author": "desktop-a"}


@pytest.mark.parametrize("field,changed", [("status", "CONFIRMED"), ("tags", ["ops"]),
                                           ("author_label", "desktop-b")])
def test_read_catches_each_p4_case_that_signature_verified_passed(tmp_path, fake_memory,
                                                                  capsys, field, changed):
    """Design probe P4, through the whole `read` (the signed invoke, the result signature,
    the author's published key): the author signed status OPEN, tags [ops, lease] and
    author desktop-a; the row serves one of them changed. `signature_verified` stays true
    (the value IS what was signed; that was P4's finding) and `status_verified` is now
    false, E_STATUS_CONTRADICTS_SIGNATURE, naming the field. Printed after the signature
    fields. Control, one variable apart: the row as signed reads true, author_signed true.
    Kills: node.read not running the check, or printing it before it ran."""
    path = _state_file(tmp_path, fake_memory.base_url)
    fake_memory.signed_args = dict(SIGNED)
    fake_memory.served = {"status": "OPEN", "tags": ["ops", "lease"],
                          "author_label": "desktop-a"}
    assert _read(fake_memory, path, capsys) == {**PRINTED, "author_signed": True}, "control"

    fake_memory.served[field] = changed
    got = _read(fake_memory, path, capsys)
    assert got["signature_verified"] is True and got["signed_by"] == AUTHOR_PEER, got
    assert got["status_verified"] is False, got
    # author_signed is about the author label alone: still the signed one unless it changed.
    assert got["author_signed"] is (field != "author_label"), got
    assert got["status_error"].startswith(f"{CONTRADICTS}: {field}: "), got["status_error"]
    assert list(got)[-5:] == ["signature_verified", "signed_by", "status_verified",
                              "status_error", "author_signed"], list(got)


def test_read_with_a_rotated_key_is_key_unavailable_not_a_contradiction(tmp_path,
                                                                        fake_memory, capsys):
    """Design test 18: the author's key rotated since the write (its published JWKS no
    longer holds the kid) and the row's status contradicts what was signed. Nothing was
    verified, so nothing was compared: `status_error` carries E_SIGNATURE_KEY_UNAVAILABLE,
    the signature's own code. Control: the same row with the key still published reads
    the contradiction."""
    path = _state_file(tmp_path, fake_memory.base_url)
    fake_memory.signed_args = {"status": "OPEN"}
    fake_memory.served = {"status": "CONFIRMED"}
    got = _read(fake_memory, path, capsys)
    assert got["status_error"].startswith(CONTRADICTS + ": status: "), ("control", got)

    fake_memory.author_jwks_mode = "rotated"
    got = _read(fake_memory, path, capsys)
    assert got["signature_verified"] is False, got
    assert got["status_verified"] is False and got["author_signed"] is False, got
    assert got["status_error"].startswith("E_SIGNATURE_KEY_UNAVAILABLE: "), got


def test_inbox_bodies_carries_each_hits_own_status_verdict(tmp_path, fake_memory, capsys):
    """inbox --bodies reads each hit through `read`, so each message carries its own
    `status_verified`: the hit whose row serves a status nobody signed reads
    E_STATUS_DEFAULT_DIFFERS beside hits that read true."""
    fake_memory.inbox_hits = [dict(h) for h in HITS]
    fake_memory.served_for[HITS[1]["key"]] = {"status": "CONFIRMED"}
    path = _state_file(tmp_path, fake_memory.base_url)
    assert cli.main(["inbox", "--base-url", fake_memory.base_url, "--state-file", str(path),
                     "--bodies"]) == 0
    reads = [h["read"] for h in json.loads(capsys.readouterr().out)["hits"]]
    assert [r["status_verified"] for r in reads] == [True, False, True], reads
    assert [r["signature_verified"] for r in reads] == [True, True, True], reads
    assert reads[1]["status_error"].startswith(DEFAULT_DIFFERS + ": status: "), reads[1]
    assert "status_error" not in reads[0] and "status_error" not in reads[2], reads


# ------------------------------------------------------------------ the README says it

def test_the_readme_names_every_status_code_the_client_can_print():
    """README "8. Chat", the `read` bullet: `status_verified`, `status_error` and
    `author_signed` are documented, and the E_STATUS_ codes it names are exactly the ones
    wire.py defines, both ways: a code added without a line fails, and so does a line for
    a code the client no longer has."""
    codes = {v for k, v in vars(wire).items()
             if k.startswith("STATUS_") and isinstance(v, str) and v.startswith("E_STATUS_")}
    assert codes == {CONTRADICTS, DEFAULT_DIFFERS, UNDERIVABLE, NOT_SERVED}, codes
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    bullet = text.split("* `read --key K [--version N]`", 1)[1].split("\n* ", 1)[0]
    for name in ("`status_verified`", "`status_error`", "`author_signed`"):
        assert name in bullet, name
    assert set(re.findall(r"E_STATUS_[A-Z_]+", text)) == codes, re.findall(
        r"E_STATUS_[A-Z_]+", text)
    assert all(f"`{code}`" in bullet for code in codes), bullet


def _read_bullet() -> str:
    """README "8. Chat", the `read` bullet, its whitespace collapsed (the file wraps)."""
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    return " ".join(text.split("* `read --key K [--version N]`", 1)[1].split("\n* ", 1)[0]
                    .split())


def test_status_verified_is_not_bound_to_the_version_and_the_readme_says_so():
    """Review of 0b08f4e, finding 4: a residual, documented rather than closed. The
    signed args carry no version, and neither check reads `version` or `is_head`
    (author_signature_ok binds namespace, key, value and writer), so a server that serves
    a row with ANOTHER version's envelope from the same writer over the same value reads
    true, with that envelope's fields. In shared/mesh-chat a sender may write later
    versions of its own msg/ key (divergence 028). The README said this of
    `signature_verified` only, and "a server cannot switch it off" read as covering it.
    Pinned both ways: the memo at version 7, not the head, reads exactly as the head
    does; and the `read` bullet says `status_verified` does not cover `version`.
    Kills: the README clause dropped (0b08f4e itself)."""
    head = _memo({"status": "OPEN"}, {"status": "OPEN"})
    other = {**head, "version": 7, "is_head": False}
    assert _check(head) == _check(other) == {"status_verified": True,
                                             "author_signed": False}
    bullet = _read_bullet()
    assert ("Like `signature_verified`, it does not cover `version`: another version's "
            "envelope from the same writer over the same value reads true for this row, "
            "with that envelope's fields") in bullet, bullet


def test_the_readme_says_what_author_is_and_which_defaults_read_can_meet():
    """Review of 0b08f4e, finding 5. `author` is the row's author_label: the label the
    writer SIGNED when it signed one (handle_write stores `author`, else `author_label`),
    and only otherwise the server's stamp; the README called it "the label the SERVER
    stamped" and said the signature checks do not cover it because "the server assigns"
    it, which `author_signed: true` rows now contradict. And `read` asks for
    shared/mesh-chat only, so the shared/wiki OPEN default it described can never apply
    to a row `read` prints; nor does "the default memory.write stores" cover a stamped
    author, which is not compared. Each stale phrase must be ABSENT, from the README and
    from the two docstrings that carried it, and its replacement present.
    Kills: any one of the old phrases left in place (0b08f4e has all of them)."""
    bullet = _read_bullet()
    for present in (
            "`author` (the row's `author_label`: the label the writer signed, else the one "
            "the SERVER stamped for the verified writer; `author_signed` below says which)",
            "not `version` or `recorded_at`, which the server assigns, nor `author`",
            "the default memory.write stores for a shared/mesh-chat row, the one namespace "
            "`read` asks for",
            "AND `author` is the label the writer signed, compared only when the writer "
            "signed one"):
        assert present in bullet, (present, bullet)
    for stale in ("`author` (the label the SERVER stamped for the verified writer)",
                  "`recorded_at` or `author`, which the server assigns",
                  "`OPEN` for a `shared/wiki` entry",
                  "and `author_label` (printed as `author`) are what memory.write's own "
                  "rules derive"):
        assert stale not in bullet, (stale, bullet)
    for doc in (node.read.__doc__, wire.author_signature_ok.__doc__):
        doc = " ".join(doc.split())
        assert "which the SERVER stamped for the verified writer" not in doc, doc
        assert "author_label, which the server assigns" not in doc, doc
