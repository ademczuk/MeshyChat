"""#199 step 1: the live and catch-up `mesh_message` frame says whether the message asks for an answer.

Why (12 Oct, kxxx's check of nxxxxx's audit on #199): `requires_ack` never reached the wake layer. On HQD, 0 of
1,286 message lines in kxxx's postbox log carried it, so a client could not tell a cc copy that asks nothing from
one that asks for an answer, and #199's hold rule (PR #289, off by default) has nothing to act on. The frame now
carries `requires_ack` as the mesh graph reads the row (`apps/mesh/normalize.requires_ack`), so the frame and the
owed edge never disagree. Both frame paths build from `nudge_columns` and `nudge_from_columns`, so one change
covers the push and the addressed long-poll.
"""
from __future__ import annotations

import uuid

from apps.core import rls
from apps.wsgw import envelope, mesh_notify
from tests.test_mesh_wait_addressed import _mesh_owner, _peer, _row, owner  # noqa: F401  (fixture and helpers)

AUTHOR = uuid.UUID("11111111-1111-1111-1111-111111111111")
CREATED = "2026-10-13T12:00:00Z"


def _cols(*, is_object=True, requires_ack=None):
    """The fourteen `nudge_columns` of one row; the eleventh is the row's `value -> 'requires_ack'`, the twelfth
    whether it holds a directive, the last two the urgent lane's flags."""
    return (CREATED, is_object, True, ["kxxx"], False, None, "hello", 1, "seat", None, requires_ack, False, False, False)


def _frame(**kw):
    return mesh_notify.nudge_from_columns("msg/1-x", AUTHOR, _cols(**kw)).frame("cc")


def test_the_frame_carries_requires_ack_as_the_graph_reads_it():
    assert _frame(requires_ack=True)["requires_ack"] is True
    assert _frame(requires_ack=False)["requires_ack"] is False
    assert _frame(requires_ack=None)["requires_ack"] is False, "absent asks nothing, as normalize reads it"
    # the legacy strings normalize accepts, and nothing else
    assert _frame(requires_ack="true")["requires_ack"] is True
    assert _frame(requires_ack="True")["requires_ack"] is True
    assert _frame(requires_ack="false")["requires_ack"] is False
    assert _frame(requires_ack=1)["requires_ack"] is False, "1 is not a claim anybody made"


def test_a_row_that_is_not_an_object_has_no_requires_ack():
    assert "requires_ack" not in _frame(is_object=False, requires_ack=None)


def test_the_field_sits_after_role_and_the_builder_default_omits_it():
    keys = list(_frame(requires_ack=True))
    assert keys.index("requires_ack") == keys.index("role") + 1, keys
    assert "requires_ack" not in envelope.mesh_message("msg/1-x", AUTHOR, None, CREATED, role="to")


def test_the_columns_read_the_stored_value(owner):
    """Through the real SQL (nudge_columns as load_nudge runs it): the jsonb comes back as the stored value, so
    true, false, a legacy string and an absent field each give the frame normalize would give."""
    _mesh_owner(owner)
    author = _peer(owner, "ack-a")
    reader = _peer(owner, "ack-r")
    cases = [({"requires_ack": True}, True), ({"requires_ack": False}, False),
             ({"requires_ack": "true"}, True), ({}, False)]
    for extra, want in cases:
        value = {"to": [author["name"]], "cc": [reader["name"]], "body": "fyi", "type": "note", **extra}
        row = _row(owner, author, value)
        with rls.server_internal(owner):
            nudge = mesh_notify.load_nudge(owner, row["key"], row["author"])
        assert nudge.frame("cc")["requires_ack"] is want, (extra, nudge.requires_ack)
