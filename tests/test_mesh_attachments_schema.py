"""`attachments`, the optional mesh message v1 field that cites files in the file service (docs/DIVERGENCES.md 079).

What is under test is the write seam's shape check, `validate_mesh_message_v1` (apps/memoryskills/validation.py), and
the client's restatement of it, `wire.attachments_problem` (tools/meshy_node/wire.py), which refuses the same value
before anything is signed. Every rule has its refusal beside a control one variable away (docs/V1-DESIGN-RULES.md C1):

  * the field is OPTIONAL: absent is accepted; present, it is a list of 1 to 8;
  * each entry has EXACTLY file_id, name, size_bytes and sha256;
  * file_id is a canonical lowercase UUID, and no file_id appears twice;
  * name is a plain file name of 1 to 255 characters: no '/' or '\\', no control character, not '.' or '..';
  * size_bytes is a non-negative int (a bool is refused) of at most 2^53-1;
  * sha256 is exactly 64 lowercase hex characters.

Nothing here touches a database: whether the file EXISTS is not the seam's question (the file service's access check is
the gate, at download time), so a well-formed reference to a file nobody uploaded is accepted, and that is pinned too.

THE CASE TABLE IS ALSO A MUTANT HARNESS, as tests/test_meshy_node_send_guard.py does it: each textual mutant of
validation.py (and of the client's wire.py) must turn at least one case's verdict, so a case that cannot go red is
caught here rather than trusted. And every case goes through BOTH checks, which must agree (C5: two implementations
agreeing, not one agreeing with itself).

All values are synthetic.

Run:  .venv/Scripts/python.exe -m pytest tests/test_mesh_attachments_schema.py -q -p no:cacheprovider
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from apps.memoryskills import validation  # noqa: E402
from apps.memoryskills.errors import HandlerError  # noqa: E402
from tools.meshy_node import node, wire  # noqa: E402

NS = validation.MESH_CHAT_NAMESPACE
KEY = "msg/1760000000000-desktop-a-abcdef"
SHA = "0123456789abcdef" * 4
ID1 = "00000000-0000-4000-8000-000000000001"
ID2 = "00000000-0000-4000-8000-000000000002"
VALIDATION_SRC = (ROOT / "apps" / "memoryskills" / "validation.py").read_text(encoding="utf-8")
WIRE_SRC = (ROOT / "tools" / "meshy_node" / "wire.py").read_text(encoding="utf-8")


def ref(**over) -> dict:
    """One valid reference, with fields replaced (or removed, by passing the sentinel DROP)."""
    item = {"file_id": ID1, "name": "serial.log", "size_bytes": 243_000, "sha256": SHA}
    for field, value in over.items():
        if value is DROP:
            item.pop(field)
        else:
            item[field] = value
    return item


DROP = object()


def message(**extra) -> dict:
    value = {"schema_version": 1, "to": ["desktop-b"], "from": "desktop-a", "type": "note",
             "thread_id": "ops/serial", "ts": "2026-10-05T12:00:00Z", "body": "the boot log, attached",
             "author_family": "claude"}
    value.update(extra)
    return value


# (case id, the `attachments` value or ABSENT, accepted?, the text a refusal must contain)
ABSENT = object()
CASES = [
    # --- the field
    ("absent", ABSENT, True, None),
    ("one", [ref()], True, None),
    ("eight", [ref(file_id=f"00000000-0000-4000-8000-{i:012d}", name=f"part{i}.log") for i in range(8)], True, None),
    ("nine", [ref(file_id=f"00000000-0000-4000-8000-{i:012d}", name=f"part{i}.log") for i in range(9)], False,
     "1 to 8"),
    ("empty-list", [], False, "1 to 8"),
    ("null", None, False, "1 to 8"),
    ("object-not-list", ref(), False, "1 to 8"),
    ("string-not-list", "serial.log", False, "1 to 8"),
    # --- the keys
    ("entry-not-object", ["serial.log"], False, "exactly the keys"),
    ("missing-sha256", [ref(sha256=DROP)], False, "exactly the keys"),
    ("missing-file-id", [ref(file_id=DROP)], False, "exactly the keys"),
    ("extra-key", [{**ref(), "url": "https://example.invalid/x"}], False, "exactly the keys"),
    # --- file_id
    ("file-id-v4-random", [ref(file_id="5f0c3f5e-8a8e-4c64-9d34-2b9b2d0e6f11")], True, None),
    ("file-id-upper-hex", [ref(file_id="5F0C3F5E-8A8E-4C64-9D34-2B9B2D0E6F11")], False, "canonical lowercase UUID"),
    ("file-id-no-hyphens", [ref(file_id="5f0c3f5e8a8e4c649d342b9b2d0e6f11")], False, "canonical lowercase UUID"),
    ("file-id-braces", [ref(file_id="{5f0c3f5e-8a8e-4c64-9d34-2b9b2d0e6f11}")], False, "canonical lowercase UUID"),
    ("file-id-urn", [ref(file_id="urn:uuid:5f0c3f5e-8a8e-4c64-9d34-2b9b2d0e6f11")], False, "canonical lowercase UUID"),
    ("file-id-not-uuid", [ref(file_id="serial-log")], False, "canonical lowercase UUID"),
    ("file-id-int", [ref(file_id=1)], False, "canonical lowercase UUID"),
    ("two-distinct-ids", [ref(), ref(file_id=ID2, name="other.log")], True, None),
    ("duplicate-id", [ref(), ref(name="other.log")], False, "duplicate file_id"),
    ("same-name-distinct-ids", [ref(), ref(file_id=ID2)], True, None),
    # --- name
    ("name-one-char", [ref(name="a")], True, None),
    ("name-255", [ref(name="n" * 251 + ".log")], True, None),
    ("name-256", [ref(name="n" * 252 + ".log")], False, "plain file name"),
    ("name-empty", [ref(name="")], False, "plain file name"),
    ("name-slash", [ref(name="logs/serial.log")], False, "plain file name"),
    ("name-backslash", [ref(name="logs\\serial.log")], False, "plain file name"),
    ("name-dot", [ref(name=".")], False, "plain file name"),
    ("name-dotdot", [ref(name="..")], False, "plain file name"),
    ("name-three-dots", [ref(name="...")], True, None),
    ("name-hidden", [ref(name=".env.example")], True, None),
    ("name-unicode", [ref(name="journal-été.txt")], True, None),
    ("name-space", [ref(name="boot log 1.txt")], True, None),
    ("name-newline", [ref(name="serial\n.log")], False, "plain file name"),
    ("name-nul", [ref(name="serial\x00.log")], False, "plain file name"),
    ("name-del", [ref(name="serial\x7f.log")], False, "plain file name"),
    ("name-not-string", [ref(name=7)], False, "plain file name"),
    # --- size_bytes
    ("size-zero", [ref(size_bytes=0)], True, None),
    ("size-max", [ref(size_bytes=2**53 - 1)], True, None),
    ("size-over-max", [ref(size_bytes=2**53)], False, "size_bytes"),
    ("size-negative", [ref(size_bytes=-1)], False, "size_bytes"),
    ("size-true", [ref(size_bytes=True)], False, "size_bytes"),
    ("size-false", [ref(size_bytes=False)], False, "size_bytes"),
    ("size-float", [ref(size_bytes=1.0)], False, "size_bytes"),
    ("size-string", [ref(size_bytes="1")], False, "size_bytes"),
    # --- sha256
    ("sha-upper", [ref(sha256=SHA.upper())], False, "64 lowercase hex"),
    ("sha-63", [ref(sha256=SHA[:63])], False, "64 lowercase hex"),
    ("sha-65", [ref(sha256=SHA + "0")], False, "64 lowercase hex"),
    ("sha-trailing-newline", [ref(sha256=SHA + "\n")], False, "64 lowercase hex"),
    ("sha-not-hex", [ref(sha256="g" * 64)], False, "64 lowercase hex"),
    ("sha-not-string", [ref(sha256=0)], False, "64 lowercase hex"),
]


def _value(attachments):
    return message() if attachments is ABSENT else message(attachments=attachments)


def _server_verdict(mod, attachments) -> tuple[bool, str]:
    try:
        mod.validate_mesh_message_v1(NS, KEY, _value(attachments))
    except HandlerError as exc:
        assert exc.code == "E_MESH_SCHEMA", exc.code
        return False, exc.message
    return True, ""


def _client_verdict(mod, attachments) -> bool:
    if attachments is ABSENT:
        return True
    return mod.attachments_problem(attachments) is None


# ----------------------------------------------------------------------------------- every rule, both checks

@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_the_server_check(case):
    _name, attachments, accepted, needle = case
    ok, said = _server_verdict(validation, attachments)
    assert ok is accepted, said
    if not accepted:
        assert needle in said, said
        assert "attachments" in said, "the refusal names the field"


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_the_client_check_agrees_with_the_server(case):
    """The client refuses before signing exactly what the server would refuse after: not stricter, not looser."""
    _name, attachments, accepted, _needle = case
    assert _client_verdict(wire, attachments) is accepted


def test_a_refusal_never_echoes_the_value():
    """A refused name may be the content a sender should not have put there; no message here echoes a value."""
    secret = "CLIENT-NAME-IN-A-FILE-NAME/serial.log"
    with pytest.raises(HandlerError) as refused:
        validation.validate_mesh_message_v1(NS, KEY, message(attachments=[ref(name=secret)]))
    assert secret not in refused.value.message and "attachments[0].name" in refused.value.message


def test_the_index_of_the_bad_entry_is_named():
    with pytest.raises(HandlerError) as refused:
        validation.validate_mesh_message_v1(NS, KEY, message(attachments=[ref(), ref(file_id=ID2, sha256="x")]))
    assert "attachments[1].sha256" in refused.value.message


def test_existence_is_not_the_seams_question():
    """A well-formed reference to a file nobody uploaded passes: the seam has no database, and the file service's
    access check at download time is the gate (E_FILE_NOT_FOUND there, as for a guessed id)."""
    nobody = "ffffffff-ffff-4fff-bfff-ffffffffffff"
    assert validation.validate_mesh_message_v1(NS, KEY, message(attachments=[ref(file_id=nobody)])) is False


def test_the_client_restates_the_servers_numbers_and_fields():
    assert "attachments" in validation.MESH_MESSAGE_V1_OPTIONAL_FIELDS
    assert wire.V1_OPTIONAL == validation.MESH_MESSAGE_V1_OPTIONAL_FIELDS
    assert wire.MAX_ATTACHMENTS == validation.MESH_MAX_ATTACHMENTS == 8
    assert wire.ATTACHMENT_FIELDS == validation.MESH_ATTACHMENT_FIELDS
    assert wire.MAX_ATTACHMENT_NAME_CHARS == validation.MESH_ATTACHMENT_NAME_MAX_CHARS == 255
    assert wire.MAX_ATTACHMENT_SIZE_BYTES == validation.MESH_ATTACHMENT_MAX_SIZE_BYTES == 2**53 - 1


# ----------------------------------------------------------------------------------- refused before signing

def test_the_client_refuses_a_bad_value_before_anything_is_signed(monkeypatch):
    """`node.send` builds the value through wire.mesh_message, which refuses a bad `attachments` (NotV1, exit 2 in
    the CLI) BEFORE `invoke` signs or sends. CONTROL: the same send with a good reference reaches invoke, with the
    field; and with no attachments the field is absent, not null."""
    invoked = []
    monkeypatch.setattr(node, "invoke", lambda *a, **kw: invoked.append(kw) or {"status": "completed"})
    state = {"status": node.STATUS_MINTED, "peer_id": ID1, "label": "desktop-a"}
    common = dict(to=["desktop-b"], thread_id="ops/serial", body="the log", msg_type="note", author_family="claude")

    for bad in ([], [ref(name="../etc/passwd")], [ref(), ref()]):
        with pytest.raises(wire.NotV1):
            node.send(Path("s.json"), state, attachments=bad, **common)
    assert invoked == [], "a refused value reached invoke"

    node.send(Path("s.json"), state, attachments=[ref()], **common)
    assert invoked[-1]["args"]["value"]["attachments"] == [ref()]
    node.send(Path("s.json"), state, **common)
    assert "attachments" not in invoked[-1]["args"]["value"], "an unset optional must be OMITTED, not null"


def test_an_edit_cannot_change_the_attachments():
    """memory.edit (divergence 059) freezes everything but `body` by comparing the WHOLE value, so attachments are
    frozen with no list to keep in step. Pinned here at the seam's level: the field validates in both versions."""
    head = message(attachments=[ref()])
    edited = {**head, "body": "the boot log, attached (corrected)"}
    assert {k for k in edited if edited[k] != head[k]} == {"body"}
    validation.validate_mesh_message_v1(NS, KEY, edited)


def test_a_redaction_tombstone_drops_the_attachments():
    """A file name is text a sender chose, so a redacted row keeps no `attachments` (they are not routing,
    redact.ROUTING_FIELDS). The FILE is not deleted by the redaction; divergence 079 says so. CONTROL: the routing
    fields of the same value stay."""
    from apps.memoryskills import redact
    stone = redact.tombstone(message(attachments=[ref()]), reason_code="sent_in_error",
                             redacted_at="2026-10-05T12:00:00Z")
    assert "attachments" not in stone and "attachments" not in redact.ROUTING_FIELDS
    assert stone["to"] == ["desktop-b"] and stone["thread_id"] == "ops/serial"


# ----------------------------------------------------------------------------------- the cases can go red

def _load(source: str, old: str, new: str, *, package: str, filename: Path, name: str):
    assert source.count(old) == 1, (name, old)
    mod = types.ModuleType(f"{package}._attachments_mutant")
    mod.__package__, mod.__file__ = package, str(filename)
    exec(compile(source.replace(old, new), mod.__file__, "exec"), mod.__dict__)
    return mod


SERVER_MUTANTS = {
    "cap-nine": ("MESH_MAX_ATTACHMENTS = 8", "MESH_MAX_ATTACHMENTS = 9"),
    "empty-list-accepted": ("or not 1 <= len(attachments) <= MESH_MAX_ATTACHMENTS)",
                            "or not 0 <= len(attachments) <= MESH_MAX_ATTACHMENTS)"),
    "extra-keys-accepted": ("set(item) != MESH_ATTACHMENT_FIELDS", "not MESH_ATTACHMENT_FIELDS <= set(item)"),
    "any-uuid-spelling": ("return str(_uuid.UUID(raw)) == raw", "return bool(_uuid.UUID(raw))"),
    "slash-accepted": ('and name not in (".", "..") and "/" not in name and "\\\\" not in name',
                       'and name not in (".", "..") and "\\\\" not in name'),
    "backslash-accepted": ('and name not in (".", "..") and "/" not in name and "\\\\" not in name',
                           'and name not in (".", "..") and "/" not in name'),
    "dot-names-accepted": ('and name not in (".", "..") and "/" not in name', 'and "/" not in name'),
    "control-characters-accepted": ("and not any(ord(char) < 32 or ord(char) == 127 for char in name))",
                                    ")"),
    "name-unbounded": ("isinstance(name, str) and 1 <= len(name) <= MESH_ATTACHMENT_NAME_MAX_CHARS",
                       "isinstance(name, str) and 1 <= len(name)"),
    "bool-size-accepted": ("if type(size) is not int or not 0 <= size <= MESH_ATTACHMENT_MAX_SIZE_BYTES:",
                           "if not isinstance(size, int) or not 0 <= size <= MESH_ATTACHMENT_MAX_SIZE_BYTES:"),
    "size-unbounded": ("if type(size) is not int or not 0 <= size <= MESH_ATTACHMENT_MAX_SIZE_BYTES:",
                       "if type(size) is not int or not 0 <= size:"),
    "negative-size-accepted": ("if type(size) is not int or not 0 <= size <= MESH_ATTACHMENT_MAX_SIZE_BYTES:",
                               "if type(size) is not int or not size <= MESH_ATTACHMENT_MAX_SIZE_BYTES:"),
    "sha-any-case": ('MESH_SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}")', 'MESH_SHA256_HEX_RE = re.compile(r"[0-9a-fA-F]{64}")'),
    "sha-prefix-match": ("MESH_SHA256_HEX_RE.fullmatch(sha256)", "MESH_SHA256_HEX_RE.match(sha256)"),
    "duplicates-accepted": ("if len(set(file_ids)) != len(file_ids):\n        _mesh_schema_error(\"attachments",
                            "if False:\n        _mesh_schema_error(\"attachments"),
    "field-never-checked": ('    if "attachments" in value:\n        _validate_mesh_attachments(value["attachments"])\n', ""),
    "field-not-in-v1": ('"closes", "attachments", "operator_words", "directive", "urgent", "urgent_declined", "window_end",\n}',
                        '"closes", "operator_words", "directive", "urgent", "urgent_declined", "window_end",\n}'),
}

CLIENT_MUTANTS = {
    "client-cap-nine": ("MAX_ATTACHMENTS = 8", "MAX_ATTACHMENTS = 9"),
    "client-empty-list-accepted": ("not 1 <= len(items) <= MAX_ATTACHMENTS", "not 0 <= len(items) <= MAX_ATTACHMENTS"),
    "client-any-uuid-spelling": ("str(uuid.UUID(file_id)) == file_id", "bool(uuid.UUID(file_id))"),
    "client-slash-accepted": ('or name in (".", "..") or "/" in name or', 'or name in (".", "..") or'),
    "client-dot-names-accepted": ('or name in (".", "..") or "/" in name or', 'or "/" in name or'),
    "client-bool-size-accepted": ("if type(size) is not int or not 0 <= size <= MAX_ATTACHMENT_SIZE_BYTES:",
                                  "if not isinstance(size, int) or not 0 <= size <= MAX_ATTACHMENT_SIZE_BYTES:"),
    "client-sha-any-case": ('_SHA256_HEX = re.compile(r"[0-9a-f]{64}")', '_SHA256_HEX = re.compile(r"[0-9a-fA-F]{64}")'),
    "client-duplicates-accepted": ("    if len(set(file_ids)) != len(file_ids):\n        return \"attachments must not",
                                   "    if False:\n        return \"attachments must not"),
}


@pytest.mark.parametrize("name", sorted(SERVER_MUTANTS))
def test_each_server_mutant_turns_a_verdict(name):
    old, new = SERVER_MUTANTS[name]
    mod = _load(VALIDATION_SRC, old, new, package="apps.memoryskills",
                filename=ROOT / "apps" / "memoryskills" / "validation.py", name=name)
    turned = [case for case, attachments, accepted, _n in CASES
              if _server_verdict(mod, attachments)[0] is not accepted]
    assert turned, f"server mutant {name!r} passed every case: a case that cannot go red"


@pytest.mark.parametrize("name", sorted(CLIENT_MUTANTS))
def test_each_client_mutant_turns_a_verdict(name):
    old, new = CLIENT_MUTANTS[name]
    mod = _load(WIRE_SRC, old, new, package="tools.meshy_node",
                filename=ROOT / "tools" / "meshy_node" / "wire.py", name=name)
    turned = [case for case, attachments, accepted, _n in CASES
              if _client_verdict(mod, attachments) is not accepted]
    assert turned, f"client mutant {name!r} passed every case: a case that cannot go red"
