"""An owed ask shows when its authority ends (kxxx 21b848 item 2, after the window_end design of ab614c): a directive's
expires_at, or relayed words' window_end plus 30 min. Database-free: the server's helper and query text, and the
client's brief rendering."""
from __future__ import annotations

from datetime import datetime, timezone

from apps.mesh import graph
from tools.meshy_node import cli

NOW = datetime(2026, 11, 11, 10, 0, tzinfo=timezone.utc)


def test_a_directive_ask_expires_at_its_directive_expiry():
    assert graph.owed_expiry("2026-11-11T11:00:00Z", None, False) == "2026-11-11T11:00:00Z"
    # A directive's own expiry wins over a window it also carries (the server bounded it by that window at write).
    assert graph.owed_expiry("2026-11-11T11:00:00Z", "2026-11-11T12:00:00Z", True) == "2026-11-11T11:00:00Z"


def test_relayed_words_expire_30_min_after_their_named_window_and_never_without_one():
    assert graph.owed_expiry(None, "2026-11-11T11:00:00Z", True) == "2026-11-11T11:30:00Z"
    assert graph.owed_expiry(None, None, True) is None                   # no window: no expiry (28 Oct rule)
    assert graph.owed_expiry(None, "2026-11-11T11:00:00Z", False) is None  # a window alone is not a relay


def test_an_unreadable_directive_expiry_is_left_out_and_an_unreadable_relay_window_is_expired():
    """pxx a974e2 P3-2: owed showed no expiry for an unreadable window_end while the reader treated the same relay as
    expired. Now both say expired, so brief never shows a relay as live that read will not act on."""
    for bad in ("2026-11-11T11:00:00", "soon", 5, ""):
        assert graph.owed_expiry(bad, None, False) is None
        assert graph.owed_expiry(None, bad, True) == graph.OWED_EXPIRED_UNKNOWN
        assert graph.owed_expiry(None, bad, False) is None                  # no relay: a window means nothing
    assert graph.owed_expiry(None, None, True) is None                       # no window at all: still no expiry


def test_the_server_and_the_reader_agree_on_every_window():
    from tools.meshy_node import node
    for window in ("2026-11-11T11:00:00Z", "2026-11-11T11:00:00", "soon", 5, "", None):
        reader = node.relay_expiry({"window_end": window})
        server = graph.owed_expiry(None, window, True)
        if reader is None:
            assert server is None, window
        elif reader == node._EXPIRED_UNKNOWN:
            assert server == graph.OWED_EXPIRED_UNKNOWN, window
        else:
            assert server == reader.strftime("%Y-%m-%dT%H:%M:%SZ"), window


def test_brief_prints_an_unknowable_relay_end_as_expired():
    from tools.meshy_node import cli
    assert "EXPIRED" in cli._expiry_cell({"expires_at": graph.OWED_EXPIRED_UNKNOWN})


def test_the_margin_matches_the_write_rule():
    from apps.memoryskills import validation
    assert graph.OWED_WINDOW_MARGIN_S == getattr(validation, "WINDOW_MARGIN_S", 1800) == 1800


def test_the_owed_query_reads_the_three_fields_from_the_head():
    import inspect
    src = inspect.getsource(graph.owed_acks)
    for col in ("sm.value->'directive'->>'expires_at' AS directive_expires_at", "sm.value->>'window_end' AS window_end",
                "(sm.value->'operator_words') IS NOT NULL AS relayed"):
        assert col in src
    assert graph.OwedAck.__dataclass_fields__["expires_at"].default is None


def test_brief_marks_a_live_and_an_expired_ask_and_leaves_others_alone():
    assert cli._expiry_cell({"expires_at": "2026-11-11T11:00:00Z"}, now=NOW) == "  expires 2026-11-11T11:00:00Z"
    assert cli._expiry_cell({"expires_at": "2026-11-11T09:00:00Z"}, now=NOW) == "  EXPIRED 2026-11-11T09:00:00Z"
    assert cli._expiry_cell({"expires_at": "2026-11-11T10:00:00Z"}, now=NOW).startswith("  EXPIRED")
    for row in ({}, {"expires_at": None}, {"expires_at": "tomorrow"}, None):
        assert cli._expiry_cell(row, now=NOW) == ""


def test_the_brief_sections_print_the_cell(capsys):
    row = {"hit": {"key": "msg/1-oxxxxxx-aaaaaa", "author": "oxxxxxx", "recorded_at": "2026-11-11T09:00:00+00:00"},
           "state": "open", "done_when": None, "expires_at": "2000-01-01T00:00:00Z"}
    cli._brief_owed_split({"items": [row], "counts": {"open": 1}}, None)
    assert "EXPIRED 2000-01-01T00:00:00Z" in capsys.readouterr().out
    cli._brief_rows("owed", {"items": [dict(row, owing_label="kxxx")]}, "owing_label")
    assert "EXPIRED 2000-01-01T00:00:00Z" in capsys.readouterr().out
