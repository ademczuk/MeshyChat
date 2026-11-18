"""The served app loads the fleet roster ONCE at startup (divergence 034; operator decision
2026-09-23).

WHY. Divergence 028 binds a mesh message's `from` and author to the caller's labels: the
roster label, peers.name and peer-<id8> (`apps.mesh.views.labels_for`). The served app loaded
no roster (018 (b): `handlers.prime_roster_labels` had no caller outside the tests), so a
fleet peer whose `from` and `--author` are its ROSTER label, as the AgentMeshy clients send,
was refused E_FROM_MISMATCH / E_AUTHOR_MISMATCH. Measured on the production standby
(028): 3 of the 11 peers that wrote the mesh in a week send under a label that is neither
their peers.name nor peer-<id8>.

THE CONTRACT, each pinned below:
  * `MESHY_FLEET_ROSTER_PATH` names roster JSON: an object whose "peers" maps a peer id to
    its label. Other fields are ignored. tests/fixtures/fleet_roster.json is synthetic JSON
    in that shape.
  * It is read ONCE, at startup (the AppConfig's ready(), which daphne and uvicorn reach
    through meshychat.asgi), into the label map 028's check consults. A later edit of the
    file changes nothing in the running process.
  * Unset: no roster, as before, said once in the log.
  * Set, and unreadable or malformed: the process REFUSES TO START, naming the variable,
    the path and the fault. A roster that silently loaded empty would re-create exactly the
    refusals above at cutover.
  * No aliasing: `<label>-cc` is not the roster label (operator ruling, 028).

The subprocess runs are the served app's own startup (tests/fixtures/roster_startup_child.py
imports meshychat.asgi), writing through the real handler on the run's database. Every
refusal sits beside the control that differs from it in the one variable under test.

Run:  pytest tests/test_fleet_roster_startup.py -q -p no:cacheprovider
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import types
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

from tests.test_mesh_authorship import DSN, MESH, _peers  # noqa: E402

ENV = "MESHY_FLEET_ROSTER_PATH"
FIXTURE = ROOT / "tests" / "fixtures" / "fleet_roster.json"
CHILD = ROOT / "tests" / "fixtures" / "roster_startup_child.py"
PID = "00000000-0000-4000-8000-00000000000a"
PID2 = "00000000-0000-4000-8000-00000000000b"


def _two(label1: str, label2: str) -> bytes:
    return json.dumps({"peers": {PID: label1, PID2: label2}}).encode()


def _one(label: str) -> bytes:
    return json.dumps({"peers": {PID: label}}).encode()


# --------------------------------------------------------------------------- parsing

def _roster():
    """Imported per test, so a tree without the module fails each test by name."""
    from apps.memoryskills import roster
    return roster


def test_the_fixture_has_the_expected_shape_and_parses_to_its_peers():
    """Exactly `peers`, id to label, byte for byte: provisional_peers and the operator
    fields are not labels (only `peers` supplies labels)."""
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert set(doc) == {"version", "updated", "notes", "operator", "operator_peer_id",
                        "peers", "provisional_peers"}, "control: the fixture is the shape"
    labels = _roster().parse(FIXTURE.read_bytes())
    assert labels == doc["peers"], labels
    assert not set(doc["provisional_peers"]) & set(labels)


MALFORMED = [
    ("not utf-8", b'{"peers": {"' + PID.encode() + b'": "\xff"}}', "is not UTF-8"),
    ("not json", b'{"peers": {', "is not JSON"),
    ("a byte order mark", b'\xef\xbb\xbf{"peers": {"' + PID.encode() + b'": "seat"}}',
     "is not JSON"),
    ("a list", b"[]", "is not a JSON object"),
    ("no peers", b'{"version": 1}', 'no "peers" object'),
    ("peers a list", b'{"peers": []}', 'no "peers" object'),
    ("peers empty", b'{"peers": {}}', '"peers" is empty'),
    ("a label not a string", b'{"peers": {"' + PID.encode() + b'": 7}}', "label"),
    ("an empty label", b'{"peers": {"' + PID.encode() + b'": ""}}', "label"),
    ("a padded label", b'{"peers": {"' + PID.encode() + b'": " seat"}}', "label"),
    ("an id that is not a uuid", b'{"peers": {"seat": "seat"}}', "is not a uuid"),
    ("an upper-case id", b'{"peers": {"' + PID.upper().encode() + b'": "seat"}}',
     "canonical"),
    ("a duplicated id", b'{"peers": {"' + PID.encode() + b'": "a", "' + PID.encode()
     + b'": "b"}}', "appears twice"),
    # REVIEW F3 of fin/r1-small: the syntax was strict and the label rules 028 depends on
    # were not checked. Each of these parsed, and each lets one peer send as, or receive
    # as, another.
    ("one label for two peers", _two("seat", "seat"), "is also the label of"),
    ("a zero-width space", _one("sea\u200bt"), "is not a label"),
    ("the Kelvin sign", _one("\u212aeith"), "is not a label"),
    ("a non-ascii letter", _one("s\u00e9at"), "is not a label"),
    ("a label longer than 64", _one("s" * 65), "is not a label"),
    ("a leading punctuation mark", _one(".seat"), "is not a label"),
    ("another peer's fallback shape", _one("peer-0123abcd"), "fallback"),
    ("another peer's fallback shape, upper case", _one("PEER-0123ABCD"), "fallback"),
    ("the broadcast label all", _one("all"), "broadcast"),
    ("the broadcast label both, any case", _one("Both"), "broadcast"),
]


@pytest.mark.parametrize("raw,needle", [(r, n) for _, r, n in MALFORMED],
                         ids=[name for name, _, _ in MALFORMED])
def test_a_malformed_roster_is_refused_and_named(raw, needle):
    """Each is refused with a message naming the fault. None of them is "no roster":
    every one would have left the label map empty, which is the refusal at cutover this
    exists to prevent. Control: the fixture parses (above), and so does the smallest
    valid roster."""
    roster = _roster()
    with pytest.raises(roster.RosterRefused) as refused:
        roster.parse(raw)
    assert needle in str(refused.value), str(refused.value)
    assert roster.parse(b'{"peers": {"' + PID.encode() + b'": "seat"}}') == {PID: "seat"}


def test_the_label_rules_keep_what_is_one_peers_own():
    """The controls for F2 and F3, each one variable from a refusal above: two peers with
    DIFFERENT labels; a peer whose label is its OWN fallback (the server's name for it
    anyway); the v1 label alphabet at its limits (64 characters, '_', '.', '-').

    LOADED AND WARNED, NOT REFUSED, because the fleet's real roster carries both an upper-case
    label (2 of its 21 peers) and one pair of labels differing only by case. The fixture
    reproduces both shapes. 028 and inbox compare labels
    exactly, so the pair are two labels there and neither peer can send as the other;
    the push and /v1/mesh/wait lower-case them, so there they are one. Refusing either
    would stop the server on that roster; `warnings` names each, and startup logs
    them (test_2). The broadcast set is the push's."""
    roster = _roster()
    assert roster.parse(_two("seat", "seat-2")) == {PID: "seat", PID2: "seat-2"}
    assert roster.warnings(roster.parse(_two("seat", "seat-2"))) == []
    own = f"peer-{PID[:8]}"
    assert roster.parse(_one(own)) == {PID: own}
    edge = "s" + "_.-9" * 15 + "abc"
    assert len(edge) == 64 and roster.parse(_one(edge)) == {PID: edge}

    upper = roster.parse(_two("Mixed-Case", "seat"))
    assert upper == {PID: "Mixed-Case", PID2: "seat"}
    (said,) = roster.warnings(upper)
    assert "'Mixed-Case'" in said and PID in said and "has upper-case letters" in said, said

    pair = roster.parse(_two("Seat", "seat"))
    assert pair == {PID: "Seat", PID2: "seat"}
    said = roster.warnings(pair)
    assert len(said) == 2 and "has upper-case letters" in said[0], said
    assert "differ only by case" in said[1] and PID in said[1] and PID2 in said[1], said

    from apps.wsgw.mesh_notify import BROADCAST_LABELS
    assert roster.BROADCAST_LABELS == BROADCAST_LABELS


# --------------------------------------------------------------------------- the served app

def _child(env_extra: dict) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != ENV and not k.startswith("PYTEST_")}
    env.update({"DJANGO_SETTINGS_MODULE": "meshychat.settings", **env_extra})
    return subprocess.run([sys.executable, str(CHILD)], env=env, cwd=str(ROOT),
                          capture_output=True, text=True, timeout=180)


def _result(proc: subprocess.CompletedProcess) -> dict:
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("ROSTER_RESULT ")]
    assert proc.returncode == 0 and len(lines) == 1, (
        f"child exit {proc.returncode}\nstdout:\n{proc.stdout[-1500:]}\n"
        f"stderr:\n{proc.stderr[-2500:]}")
    return json.loads(lines[0][len("ROSTER_RESULT "):])


def _log_lines(proc: subprocess.CompletedProcess, needle: str) -> list[str]:
    return [ln for ln in proc.stdout.splitlines() if needle in ln]


@pytest.fixture(scope="module")
def seat(tmp_path_factory):
    """A peer granted write on the mesh, its roster label (NOT its peers.name, so only the
    roster can make it the caller's), a roster file in the fixture's shape naming it, and
    the signing material the child writes with."""
    with psycopg.connect(DSN, autocommit=True) as conn:
        (p,) = _peers(conn, "roster")
    label = f"fixture-seat-{p['peer_id'].hex[:6]}"
    assert label != p["name"] and label != p["fallback"]
    tmp = tmp_path_factory.mktemp("roster")
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    doc["peers"][str(p["peer_id"])] = label
    roster = tmp / "fleet_roster.json"
    roster.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    writer = tmp / "writer.json"
    writer.write_text(json.dumps({"peer_id": str(p["peer_id"]), "seed_hex": p["seed"].hex(),
                                  "kid": p["kid"], "label": label}), encoding="utf-8")
    return types.SimpleNamespace(peer=p, label=label, roster=roster, writer=writer)


def _rows(peer_id) -> list[tuple]:
    with psycopg.connect(DSN, autocommit=True) as conn:
        return conn.execute("SELECT author_label, value->>'from' FROM shared_memory "
                            "WHERE namespace = %s AND created_by_peer_id = %s ORDER BY key",
                            (MESH, peer_id)).fetchall()


def test_1_without_the_setting_there_is_no_roster_and_the_seat_label_is_refused(seat):
    """Today's behaviour, pinned as the served app's: no roster, said once in the log, and
    the peer's roster label is not its label, so each write under it is refused with
    028's codes and nothing is written."""
    proc = _child({"ROSTER_CHILD_PEERS": str(seat.peer["peer_id"]),
                   "ROSTER_CHILD_WRITER": str(seat.writer)})
    out = _result(proc)
    assert out["labels"] == {str(seat.peer["peer_id"]): None}, out
    said = _log_lines(proc, "no fleet roster")
    assert len(said) == 1 and ENV in said[0], proc.stdout[-1500:]
    assert out["writes"] == {"from_and_author": "E_AUTHOR_MISMATCH",
                             "from_only": "E_FROM_MISMATCH",
                             "author_cc": "E_AUTHOR_MISMATCH"}, out
    assert _rows(seat.peer["peer_id"]) == []


def test_2_with_the_setting_the_roster_label_is_the_callers_and_is_read_once(seat):
    """The same peer, the same writes, one variable apart: the roster file named by
    MESHY_FLEET_ROSTER_PATH. Loaded at startup (one log line with the count), so the
    roster label is the caller's: `from` and author under it are ACCEPTED; `<label>-cc`
    is still refused (no aliasing). The file is overwritten after startup and the running
    process's labels do not change: the bytes are read once."""
    proc = _child({ENV: str(seat.roster), "ROSTER_CHILD_PEERS": str(seat.peer["peer_id"]),
                   "ROSTER_CHILD_WRITER": str(seat.writer), "ROSTER_CHILD_REWRITE": "1"})
    out = _result(proc)
    pid = str(seat.peer["peer_id"])
    assert out["labels"] == {pid: seat.label}, out
    assert out["after_rewrite"] == {pid: seat.label}, "the file was read again"
    loaded = _log_lines(proc, "fleet roster loaded")
    assert len(loaded) == 1 and str(seat.roster) in loaded[0] and "5 peers" in loaded[0], \
        proc.stdout[-1500:]
    assert _log_lines(proc, "no fleet roster") == []
    # REVIEW F2/F3 of fin/r1-small: the fixture's upper-case label, and its pair that
    # differs only by case (both shapes occur in the fleet's real roster), load; startup says so
    # once each, at WARNING, naming the peers, what cannot work, 034 and the push/inbox
    # split. Nothing else is named.
    upper = _log_lines(proc, "has upper-case letters")
    assert len(upper) == 1 and " WARNING " in upper[0], proc.stdout[-2500:]
    assert "'Fixture-Mixed-Case'" in upper[0] and "00000000-0000-4000-8000-000000000003" \
        in upper[0], upper[0]
    assert "034" in upper[0] and "inbox" in upper[0], upper[0]
    pair = _log_lines(proc, "differ only by case")
    assert len(pair) == 1 and " WARNING " in pair[0], proc.stdout[-2500:]
    assert "00000000-0000-4000-8000-000000000003" in pair[0] and \
        "00000000-0000-4000-8000-000000000004" in pair[0], pair[0]
    assert "034" in pair[0] and "push" in pair[0], pair[0]
    assert all(seat.label not in ln for ln in upper + pair), (upper, pair)
    assert out["writes"] == {"from_and_author": "ok", "from_only": "ok",
                             "author_cc": "E_AUTHOR_MISMATCH"}, out
    assert _rows(seat.peer["peer_id"]) == [(seat.label, seat.label)] * 2


@pytest.mark.parametrize("fault", ["not_json", "missing", "empty_peers"])
def test_3_a_set_path_that_is_unreadable_or_malformed_refuses_startup(tmp_path, fault):
    """The served app does not start: exit non-zero before it could serve, the error
    naming the variable, the path and the fault. Control: the fixture itself starts (the
    same child, the same variable, a well-formed file)."""
    path = tmp_path / "fleet_roster.json"
    if fault == "not_json":
        path.write_text('{"peers": {', encoding="utf-8")
    elif fault == "empty_peers":
        path.write_text('{"version": 1, "peers": {}}', encoding="utf-8")
    proc = _child({ENV: str(path)})
    assert proc.returncode != 0, proc.stdout[-800:]
    assert "ROSTER_RESULT" not in proc.stdout
    assert ENV in proc.stderr and str(path) in proc.stderr, proc.stderr[-2000:]
    reason = {"not_json": "is not JSON", "missing": "cannot be read",
              "empty_peers": '"peers" is empty'}[fault]
    assert reason in proc.stderr, proc.stderr[-2000:]

    control = _child({ENV: str(FIXTURE)})
    assert _result(control)["labels"] == {}
    assert len(_log_lines(control, "fleet roster loaded")) == 1
