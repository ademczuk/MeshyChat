"""`send --subject` and `--ref` are checked before anything is sent (backlog item 15, docs/DIVERGENCES.md 007).

THE FAILURE: the write seam refuses a `subject` or a `ref` that is not a slug (apps/memoryskills/validation.py
MESH_SUBJECT_RE and MESH_REF_RE: a letter or digit, then only letters, digits and _ . : / -, at most 256 and 128
characters), and the client passed any text through. Sends on 2026-10-04 and 2026-10-08 were signed, sent and refused
E_MESH_SCHEMA after the round trip. tools/meshy_node/wire.py now carries copies of both patterns (the client imports
nothing from apps/, so the copies are pinned equal here, importing both), and:

  * `send` refuses a bad value with exit 2 (node.Refused), naming the flag and the allowed shape, before any upload,
    recipient read or send; an empty value is still omitted, not refused;
  * `offer --subject` refuses the same before its socket opens;
  * wire.mesh_message refuses the same (NotV1, exit 2) for every other caller of node.send, before anything is signed.

Every case goes through BOTH checks, the server's and the client's, which must agree (C5: two implementations agreeing,
not one agreeing with itself).

No network and no database: node._opener, the one opener every request of the client takes, is replaced by a recorder
that refuses, and node.invoke by a recorder that answers.

Run:  .venv/Scripts/python.exe -m pytest tests/test_meshy_node_subject_ref.py -q -p no:cacheprovider
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from apps.memoryskills import validation  # noqa: E402
from apps.memoryskills.errors import HandlerError  # noqa: E402
from tools.meshy_node import cli, node, wire  # noqa: E402

NS = validation.MESH_CHAT_NAMESPACE
KEY = "msg/1760000000000-desktop-a-abcdef"
BASE = "http://127.0.0.1:9"


def message(**extra) -> dict:
    value = {"schema_version": 1, "to": ["desktop-b"], "from": "desktop-a", "type": "note",
             "thread_id": "ops/plan", "ts": "2026-10-10T12:00:00Z", "body": "the plan, as agreed",
             "author_family": "claude"}
    value.update(extra)
    return value


def test_the_clients_patterns_equal_the_servers():
    """The copies in wire.py are the server's patterns, character for character, flags included.

    MUTANT: widen wire.MESH_SUBJECT_RE's class with a space; this test and the `two words` case go red.
    MUTANT: wire.MESH_REF_RE with {0,128}; this test and the 129-character ref case go red."""
    assert (wire.MESH_SUBJECT_RE.pattern, wire.MESH_SUBJECT_RE.flags) == (
        validation.MESH_SUBJECT_RE.pattern, validation.MESH_SUBJECT_RE.flags)
    assert (wire.MESH_REF_RE.pattern, wire.MESH_REF_RE.flags) == (
        validation.MESH_REF_RE.pattern, validation.MESH_REF_RE.flags)


# (field, value, accepted?)
CASES = [
    ("subject", "pr/933", True),
    ("subject", "backlog-15", True),
    ("subject", "a:b/c.d_e-f", True),
    ("subject", "9", True),
    ("subject", "a" * 256, True),
    ("subject", "a" * 257, False),
    ("subject", "Re: the plan", False),
    ("subject", "two words", False),
    ("subject", "-leading", False),
    ("subject", "_leading", False),
    ("subject", "/leading", False),
    ("subject", "pr#933", False),
    ("subject", "café", False),
    ("subject", "pr/933\n", False),
    ("subject", "pr/933\t", False),
    ("subject", "", False),
    ("subject", 933, False),
    ("ref", "0123abcd", True),
    ("ref", "hash:09cb6def", True),
    ("ref", "v1.2.0", True),
    ("ref", "a" * 128, True),
    ("ref", "a" * 129, False),
    ("ref", "PR 73", False),
    ("ref", "acme/widget#73", False),
    ("ref", "0123abcd\n", False),
]


@pytest.mark.parametrize("field,value,accepted", CASES,
                         ids=[f"{f}-{i}" for i, (f, _v, _a) in enumerate(CASES)])
def test_the_client_and_the_server_agree_on_every_case(field, value, accepted):
    """Each value through the server's validator and the client's check. A refusal names the field and its bound.

    MUTANT: `.match` for `.fullmatch` in wire.subject_problem or wire.ref_problem; the trailing-newline cases go red.
    MUTANT: wire.ref_problem checks MESH_SUBJECT_RE; the 129-character ref case goes red.
    MUTANT: wire.subject_problem returns None for anything; every refused subject case goes red."""
    try:
        validation.validate_mesh_message_v1(NS, KEY, message(**{field: value}))
        server = True
    except HandlerError:
        server = False
    why = (wire.subject_problem if field == "subject" else wire.ref_problem)(value)
    assert server is accepted, "the case table is wrong about the server"
    assert (why is None) is accepted, f"the client and the server disagree on {field}={value!r}"
    if why is not None:
        most = 256 if field == "subject" else 128
        assert why.startswith(f"{field} must be 1 to {most} characters") and "_ . : / -" in why and "body" in why


# ----------------------------------------------------------------------------------- the command

@pytest.fixture()
def offline(monkeypatch, tmp_path):
    """A minted node's state file, every request the client could open recorded and refused, and node.invoke
    answering. Returns (state_file, opened, invoked)."""
    seed = wire.new_seed()
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({
        "schema": node.STATE_SCHEMA, "base_url": BASE, "status": node.STATUS_MINTED,
        "signing_seed_b64url": wire.b64u(seed), "public_key_b64url": wire.b64u(wire.public_key_of(seed)),
        "device_label": "test", "memory_peer_id": node.DEFAULT_MEMORY_PEER_ID,
        "peer_id": str(uuid.uuid4()), "owner_id": str(uuid.uuid4()), "kid": "test-kid",
        "jwt": "h.p.s", "jwt_expires_at": "2099-01-01T00:00:00Z", "label": "desktop-a"}), encoding="utf-8")
    opened, invoked = [], []

    def no_network(*_a, **_kw):
        opened.append(True)
        raise OSError("this test has no network")

    monkeypatch.setattr(node, "_opener", no_network)
    monkeypatch.setattr(node, "invoke", lambda *a, **kw: invoked.append(kw) or {
        "status": "completed", "result": {}, "result_signature_verified": True})
    return state_file, opened, invoked


def _send(state_file: Path, *extra: str) -> int:
    return cli.main(["send", "--base-url", BASE, "--state-file", str(state_file), "--to", "desktop-b",
                     "--thread", "ops/plan", "--body", "the plan, as agreed", *extra])


@pytest.mark.parametrize("flag,value,most", [("--subject", "Re: the plan", 256), ("--ref", "PR 73", 128)])
def test_send_refuses_a_bad_subject_or_ref_before_any_network_call(offline, tmp_path, capsys, flag, value, most):
    """Exit 2 with the flag, the value and the allowed shape on stderr. `--attach` and the recipient check are both on,
    so a check that ran after either would show as a request opened.

    MUTANT: drop the check in cli.run's send; the attachment's upload opens a request and the server's refusal is what
    the sender would have met.
    MUTANT: move the check below `_attach`; likewise.
    MUTANT: check only --subject in cli.run; the --ref case reaches the upload."""
    state_file, opened, invoked = offline
    log = tmp_path / "plan.log"
    log.write_bytes(b"x")
    code = _send(state_file, flag, value, "--attach", str(log))
    err = capsys.readouterr().err
    assert opened == [] and invoked == [], "a refused value reached the network"
    assert code == 2
    refused = json.loads(err.splitlines()[-1])["refused"]
    assert refused.startswith(f"{flag} {value!r} refused: ") and f"1 to {most} characters" in refused
    assert "_ . : / -" in refused and "free text belongs in the body" in refused


def test_send_signs_a_good_subject_and_ref_and_omits_an_empty_one(offline, capsys):
    """CONTROL beside the refusal: slugs are sent as given, and an empty value is still omitted, not refused.

    MUTANT: ask the check of every value that is not None; `--subject ""` exits 2.
    MUTANT: refuse when the problem is None (the test inverted); the good send exits 2."""
    state_file, opened, invoked = offline
    assert _send(state_file, "--subject", "pr/933", "--ref", "0123abcd", "--no-recipient-check",
                 "--no-card-check") == 0
    value = invoked[-1]["args"]["value"]
    assert (value["subject"], value["ref"]) == ("pr/933", "0123abcd")
    assert _send(state_file, "--subject", "", "--ref", "", "--no-recipient-check", "--no-card-check") == 0
    value = invoked[-1]["args"]["value"]
    assert "subject" not in value and "ref" not in value
    assert opened == [] and len(invoked) == 2
    capsys.readouterr()


def test_node_send_refuses_a_bad_subject_or_ref_before_anything_is_signed(monkeypatch):
    """The second guard, for a caller of node.send that is not `send` (`offer`, a script): wire.mesh_message raises
    NotV1, which cli.main exits 2 on, before invoke signs. CONTROL: the good values reach invoke.

    MUTANT: drop the subject guard in wire.mesh_message; the free-text subject reaches invoke.
    MUTANT: drop the ref guard in wire.mesh_message; the bad ref reaches invoke."""
    invoked = []
    monkeypatch.setattr(node, "invoke", lambda *a, **kw: invoked.append(kw) or {"status": "completed"})
    state = {"status": node.STATUS_MINTED, "peer_id": str(uuid.uuid4()), "label": "desktop-a"}
    common = dict(to=["desktop-b"], thread_id="ops/plan", body="the plan", msg_type="note", author_family="claude")
    with pytest.raises(wire.NotV1, match="^subject must be 1 to 256"):
        node.send(Path("s.json"), state, subject="Re: the plan", **common)
    with pytest.raises(wire.NotV1, match="^ref must be 1 to 128"):
        node.send(Path("s.json"), state, ref="PR 73", **common)
    assert invoked == [], "a refused value reached invoke"
    node.send(Path("s.json"), state, subject="pr/933", ref="0123abcd", **common)
    assert (invoked[-1]["args"]["value"]["subject"], invoked[-1]["args"]["value"]["ref"]) == ("pr/933", "0123abcd")
