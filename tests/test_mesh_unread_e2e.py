"""GET /v1/mesh/unread end to end: real nodes, the real invoke pipeline, the real client.

tests/test_mesh_unread.py plants read calls straight into skill_calls so each case controls
exactly the reads it is about. That is only as good as the planted shape, and the first version of
that file planted the bare memory.read args while the pipeline stores the WHOLE signed payload: every
test passed and the query matched no real row. So this file plants nothing. A sends to B with the
client, B asks `unread` with the client, B fetches with the client's own `read`, and the backlog has
to clear because of the row the server itself wrote.

The harness is tests/test_meshy_node_read.py's: a fresh database, the real ASGI application on a real
socket, the operator's commands; A and B hold write on shared/mesh-chat, C holds nothing.

Run:  bash ../runt-wt.sh <worktree> tests/test_mesh_unread_e2e.py -q
Mutant: U11 in tests/test_mesh_unread.py (the reads taken from the bare args) also fails here.
"""
from __future__ import annotations

from tests.test_meshy_node_read import (_fresh_limiters, _read, _send, _settle,  # noqa: F401
                                        mesh)


def _unread_keys(mesh, who: str) -> list[str]:
    code, body = mesh.rb.node("unread", mesh.states[who])
    assert code == 0, body
    return [item["key"] for item in body["unread"]["items"]]


def test_a_real_read_clears_the_backlog_the_server_reports(mesh):
    """CONTROL first: the message is backlog for B before B reads it, and was never backlog for its
    sender. Then B's own `read` clears it, through the row the invoke pipeline wrote."""
    sent = _send(mesh, "A", "B", "unread-e2e", "a synthetic message for the unread route")
    key = sent["key"]
    _settle(mesh.rb)
    assert key in _unread_keys(mesh, "B")
    assert key not in _unread_keys(mesh, "A"), "a sender's own message is not its backlog"
    code, message = _read(mesh, "B", key)
    assert code == 0 and message["key"] == key, message
    assert key not in _unread_keys(mesh, "B")


def test_a_real_edit_is_backlog_again_until_its_version_is_read(mesh):
    """The returned-version rule through the real pipeline: B reads version 1, A edits, the message
    is B's backlog again at version 2, and B's own read of the head clears it."""
    sent = _send(mesh, "A", "B", "unread-e2e-edit", "the first wording")
    key = sent["key"]
    _settle(mesh.rb)
    code, first = _read(mesh, "B", key)
    assert code == 0 and first["version"] == 1, first
    _settle(mesh.rb)
    assert key not in _unread_keys(mesh, "B")
    code, edited = mesh.rb.node("edit", mesh.states["A"], "--key", key, "--body", "the corrected wording",
                                control=True)
    assert code == 0, edited
    _settle(mesh.rb)
    code, body = mesh.rb.node("unread", mesh.states["B"])
    assert code == 0, body
    pending = [i for i in body["unread"]["items"] if i["key"] == key]
    assert pending and pending[0]["version"] == 2, body
    code, second = _read(mesh, "B", key)
    assert code == 0 and second["version"] == 2, second
    assert key not in _unread_keys(mesh, "B")


def test_a_read_that_was_refused_clears_nothing(mesh):
    """C holds no grant, so its read of B's mail is refused and the route refuses C too. CONTROL:
    B's backlog still holds a message only C tried to read."""
    sent = _send(mesh, "A", "B", "unread-e2e-refused", "another synthetic message")
    _settle(mesh.rb)
    code, refused = _read(mesh, "C", sent["key"])
    assert code == 1 and refused["error"] == "E_READ_REFUSED", refused
    code, body = mesh.rb.node("unread", mesh.states["C"])
    assert code != 0, body
    assert sent["key"] in _unread_keys(mesh, "B")
