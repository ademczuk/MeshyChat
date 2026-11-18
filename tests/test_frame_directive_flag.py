"""The `directive` routing flag on a mesh-message frame (oxxxxxx f38d28 item 1, Tue 10 Nov 2026).

The postbox priority lane (tools/postbox/postbox.py codex_priority) delivers a frame with `directive: true` next.
The flag comes from the stored row (`value ? 'directive'`, the twelfth nudge column), which memory.write keeps only
after validate_directive, and appears on a `to` frame only. Pure functions; no database.
"""
import uuid

import pytest

from apps.wsgw import envelope, mesh_notify

AUTHOR = uuid.UUID(int=7)
CREATED = "2026-11-10T06:00:00Z"


def _cols(*, is_object=True, has_directive=False):
    return (CREATED, is_object, True, ["codex-oxxxxxx-cli"], False, None, "do this", 1, "oxxxxxx", None, True,
            has_directive, False, False)          # the urgent lane's two flags follow (tests/test_urgent_lane.py)


def _nudge(**kw):
    return mesh_notify.nudge_from_columns("msg/1-x", AUTHOR, _cols(**kw))


def test_the_nudge_reads_the_directive_as_its_twelfth_column():
    assert mesh_notify.NUDGE_COLUMNS == 14                 # 12 + the urgent lane's two (kxxx ef884a)
    assert "(sm.value ? 'directive'), " in mesh_notify.nudge_columns("sm.")
    assert _nudge(has_directive=True).directive is True
    assert _nudge(has_directive=False).directive is False


@pytest.mark.parametrize("role, shown", [("to", True), ("cc", False), ("broadcast", False)])
def test_the_flag_is_on_a_to_frame_only(role, shown):
    frame = _nudge(has_directive=True).frame(role)
    assert ("directive" in frame) is shown
    if shown:
        assert frame["directive"] is True


def test_a_row_without_a_directive_has_no_flag_on_any_frame():
    for role in ("to", "cc", "broadcast"):
        assert "directive" not in _nudge(has_directive=False).frame(role)


@pytest.mark.parametrize("raw", [None, "true", 1])
def test_only_a_real_true_from_the_database_sets_it(raw):
    assert _nudge(has_directive=raw).directive is False


def test_a_row_that_is_not_an_object_never_carries_it():
    assert _nudge(is_object=False, has_directive=True).directive is False


def test_the_builder_default_is_no_flag():
    assert "directive" not in envelope.mesh_message("msg/1-x", AUTHOR, None, CREATED, role="to")
    frame = envelope.mesh_message("msg/1-x", AUTHOR, ["a"], CREATED, role="to", requires_ack=True, directive=True)
    keys = list(frame)
    assert keys.index("requires_ack") < keys.index("directive")       # the documented field order
