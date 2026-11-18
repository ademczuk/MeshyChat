"""The bodies memory.write, memory.edit and memory.redact share, each spelled ONCE.

WHY. memory.edit (divergence 059) writes a row "exactly as memory.write does" and tells the
authors of the direct replies "as redaction tells them". Those were pasted copies: the
envelope checks and the provenance dict of handle_write, the reply-author read of
handle_redact, and the peer-<id8> fallback label in three files beside the regex that must
recognise it. A copy that drifted would stamp, store or notify differently on one path and
no test of the other path would see it. These pin that there is one body for each and what
it answers; the database-backed behaviour is in test_edit.py, test_redaction*.py,
test_memory_skills.py and test_mesh_authorship.py.

Run:  pytest tests/test_memoryskills_shared_bodies.py -q -p no:cacheprovider
"""
from __future__ import annotations

import ast
import hashlib
import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")

from apps.memoryskills import edit, handlers, redact, roster  # noqa: E402
from apps.memoryskills.errors import HandlerError  # noqa: E402

PID = "00000000-0000-4000-8000-00000000000a"
OTHER = "11111111-0000-4000-8000-00000000000b"


# ------------------------------------------------------------------ the fallback label

def test_the_fallback_label_is_written_and_recognised_by_one_pair():
    for peer in (PID, uuid.UUID(PID), str(uuid.uuid4()), uuid.uuid4()):
        label = handlers.fallback_label(peer)
        assert label == "peer-" + str(peer)[:8]
        assert handlers.FALLBACK_LABEL_RE.match(label), label
    # Control: the regex is not a catch-all.
    assert not handlers.FALLBACK_LABEL_RE.match("peer-0000000")
    assert not handlers.FALLBACK_LABEL_RE.match("peer-0000000A")


def test_the_stamped_label_is_the_roster_label_first_then_the_fallback(monkeypatch):
    monkeypatch.setattr(handlers, "_ROSTER_LABELS", {})
    assert handlers.stamped_label(PID) == "peer-00000000"
    monkeypatch.setattr(handlers, "_ROSTER_LABELS", {PID: "seat"})
    assert handlers.stamped_label(PID) == "seat"
    assert handlers.stamped_label(uuid.UUID(PID)) == "seat"
    assert handlers.stamped_label(OTHER) == "peer-11111111"


def test_the_roster_refuses_another_peers_fallback_by_the_same_pair():
    roster._check_label(PID, handlers.fallback_label(PID), {})          # its own: loaded
    with pytest.raises(roster.RosterRefused, match="fallback label for ANOTHER peer"):
        roster._check_label(PID, handlers.fallback_label(OTHER), {})


def _sources() -> dict[str, str]:
    return {p.name: p.read_text("utf-8") for p in (ROOT / "apps" / "memoryskills").glob("*.py")}


def test_no_memoryskills_module_spells_the_fallback_by_hand():
    counts = {name: text.count('f"peer-{') for name, text in _sources().items()}
    assert {name: n for name, n in counts.items() if n} == {"handlers.py": 1}, counts   # fallback_label's own


# ------------------------------------------------------------------ the signed envelope

def test_the_envelope_refuses_in_memory_writes_order_before_anything_is_read():
    """No connection is touched before the three checks: conn=None proves it."""
    with pytest.raises(HandlerError) as e:
        handlers._signed_envelope({"signature": "s", "signing_kid": "k"}, {}, None, PID)
    assert (e.value.code, str(e.value.message)) == ("E_BAD_REQUEST", "payload.call_id missing")
    for vp in ({"signing_kid": "k"}, {"signature": "s"}, {"signature": "", "signing_kid": "k"}):
        with pytest.raises(HandlerError) as e:
            handlers._signed_envelope(vp, {"call_id": "c"}, None, PID)
        assert (e.value.code, str(e.value.message)) == (
            "E_BAD_REQUEST", "verified_payload missing signature/signing_kid for provenance")


def test_the_envelope_answers_the_verified_bytes_and_their_hash(monkeypatch):
    seen = []

    def verified(conn, peer, kid, payload, sig):
        seen.append((conn, peer, kid, payload, sig))
        return b'{"call_id":"c"}'

    monkeypatch.setattr(handlers, "_verify_provenance_signature", verified)
    out = handlers._signed_envelope({"signature": "s", "signing_kid": "k", "verified_at": "T"},
                                    {"call_id": "c"}, "conn", PID)
    assert out == ("c", "s", "k", "T", b'{"call_id":"c"}',
                   hashlib.sha256(b'{"call_id":"c"}').hexdigest())
    assert seen == [("conn", PID, "k", {"call_id": "c"}, "s")]
    # verified_at defaults to now, in now_iso's shape.
    out = handlers._signed_envelope({"signature": "s", "signing_kid": "k"}, {"call_id": "c"},
                                    "conn", PID)
    assert out[3].endswith("Z") and "T" in out[3]


def test_the_provenance_keys_and_their_order():
    p = handlers._provenance("P", "S", "K", "O", "H", "B", "F", "V", False)
    assert list(p.items()) == [
        ("payload", "P"), ("signature", "S"), ("signing_kid", "K"), ("caller_owner_id", "O"),
        ("payload_jcs_sha256", "H"), ("public_key_b64url", "B"), ("public_key_sha256", "F"),
        ("verified_at", "V"), ("pre_discipline", False)]


def _calls_in(module_text: str, function: str) -> set[str]:
    tree = ast.parse(module_text)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == function)
    out = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            out.add(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""))
    return out


def test_write_and_edit_share_the_envelope_and_the_provenance_and_redact_and_edit_the_reply_read():
    src = _sources()
    assert {"_signed_envelope", "_provenance"} <= _calls_in(src["handlers.py"], "handle_write")
    assert {"_signed_envelope", "_provenance", "read_reply_authors"} <= _calls_in(src["edit.py"], "handle_edit")
    assert "read_reply_authors" in _calls_in(src["redact.py"], "handle_redact")
    # One spelling of each under apps/memoryskills.
    joined = "\n".join(src.values())
    assert joined.count("payload.call_id missing") == 1
    assert joined.count("signing_kid for provenance") == 1
    assert joined.count("value ->> 'in_reply_to' = %s") == 1


# ------------------------------------------------------------------ the reply authors

class _Cursor:
    def __init__(self, rows):
        self.rows, self.executed = rows, []

    def execute(self, sql, params):
        self.executed.append((sql, params))

    def fetchall(self):
        return self.rows


def test_the_reply_authors_read_and_its_shape():
    pid = uuid.UUID(OTHER)
    cur = _Cursor([{"author_label": "a", "created_by_peer_id": pid}])
    out = redact.read_reply_authors(cur, "shared/mesh-chat", "msg/1-a-000001", uuid.UUID(PID))
    assert out == [{"author_label": "a", "peer_id": OTHER}]
    assert list(out[0]) == ["author_label", "peer_id"]
    assert cur.executed == [(
        "SELECT DISTINCT author_label, created_by_peer_id FROM shared_memory "
        "WHERE namespace = %s AND is_head AND value ->> 'in_reply_to' = %s "
        "  AND created_by_peer_id <> %s ORDER BY author_label",
        ("shared/mesh-chat", "msg/1-a-000001", PID))]
    assert edit.redact is redact
