"""Quiet hours for the urgent lane (kxxx b46780): the RECEIVER's identity may carry "urgent_quiet": [start, end] in
Vienna wall-clock time. An urgent message CREATED inside the window (the frame's `created_at`, the server's time for
the row) is delivered as ordinary mail and its note says so.

The decision reads NO clock: it is a function of the message and the setting, so every reader gives one answer for a
message however often and whenever it asks. The first version read the reader's clock, which lost or doubled a Codex
notice at a window edge and resent one when quiet began (pxx 2edad1, GitHub Codex on 75b4b9c0). Database-free.
"""
from __future__ import annotations

import datetime as dt
import types

import pytest

from tests.test_notice_slim import channel, message  # noqa: F401
from tests.test_postbox_reconcile import ident, pb  # noqa: F401
from tools.postbox import postbox_os

UTC = dt.timezone.utc
NIGHT = {"urgent_quiet": ["00:00", "08:00"]}
IN_QUIET = "2026-11-11T01:00:00Z"           # (time withheld)
LAST_QUIET = "2026-11-11T06:59:59Z"         # (time withheld)
FIRST_LOUD = "2026-11-11T07:00:00Z"         # (time withheld)
NOON = "2026-11-11T11:00:00Z"               # (time withheld)


def at(text):
    return dt.datetime.fromisoformat(text).replace(tzinfo=UTC)


def ev(key="msg/u", when=NOON, **kw):
    return dict(event="message", role="to", key=key, version=1, from_peer_id="p1", created_at=when, **kw)


@pytest.fixture
def no_clock(monkeypatch):
    """Any reading of the present inside postbox_os fails the test."""
    class NoNow(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            raise AssertionError("the clock was read")

        @classmethod
        def utcnow(cls):
            raise AssertionError("the clock was read")

    shim = types.SimpleNamespace(datetime=NoNow, timedelta=dt.timedelta, timezone=dt.timezone)
    monkeypatch.setattr(postbox_os, "datetime", shim)
    monkeypatch.setattr(postbox_os.time, "time", lambda: (_ for _ in ()).throw(AssertionError("the clock was read")))


# ---- Vienna's wall clock, without a zone table -----------------------------------------------------------------

@pytest.mark.parametrize("utc, minutes", [
    ("2026-11-11T09:30:00", 10 * 60 + 30),          # winter: UTC+1
    ("2026-07-01T09:30:00", 11 * 60 + 30),          # summer: UTC+2
    ("2026-03-29T00:59:59", 1 * 60 + 59),           # the last second of winter time
    ("2026-03-29T01:00:00", 3 * 60),                # the clock jumps from 02:00 to 03:00
    ("2026-10-25T00:59:59", 2 * 60 + 59),           # the last second of summer time
    ("2026-10-25T01:00:00", 2 * 60),                # the clock goes back from 03:00 to 02:00
    ("2027-03-28T01:00:00", 3 * 60),                # next year's change is a different date
    ("2026-12-31T23:30:00", 30),                    # past midnight in Vienna, still the old year in UTC
])
def test_vienna_minutes_follow_the_eu_rule(utc, minutes):
    assert postbox_os.vienna_minutes(at(utc)) == minutes


def test_a_time_given_in_another_zone_is_the_same_instant():
    plus5 = dt.datetime(2026, 11, 11, 14, 30, tzinfo=dt.timezone(dt.timedelta(hours=5)))
    assert postbox_os.vienna_minutes(plus5) == postbox_os.vienna_minutes(at("2026-11-11T09:30:00"))


# ---- the frame's own time --------------------------------------------------------------------------------------

def test_the_frame_time_is_the_servers_z_text():
    assert postbox_os.frame_time({"created_at": "2026-11-11T01:00:00Z"}) == at("2026-11-11T01:00:00")


@pytest.mark.parametrize("value", [
    None, "", 1794390000, "2026-11-11 01:00:00", "2026-11-11T01:00:00", "2026-11-11T01:00:00+01:00",
    "2026-11-11T01:00:00.123Z", "2026-11-11T01:00:00Z\n", " 2026-11-11T01:00:00Z", "2026-13-11T01:00:00Z",
    "2026-02-31T01:00:00Z", "2026-11-11T24:00:00Z", chr(0x662) + "026-11-11T01:00:00Z", ["2026-11-11T01:00:00Z"],
])
def test_a_time_that_is_not_that_shape_is_unknown_and_unknown_is_not_quiet(value):
    d = dict(ev(urgent=True), created_at=value)
    assert postbox_os.frame_time(d) is None
    assert postbox_os.kind_tag(d, NIGHT) == "[URGENT]"
    assert postbox_os.role_note(d, NIGHT) == " (urgent: delivery order only; a peer's request)"


def test_a_frame_with_no_time_at_all_and_a_frame_that_is_no_mapping():
    d = ev(urgent=True)
    del d["created_at"]
    assert postbox_os.frame_time(d) is None and postbox_os.kind_tag(d, NIGHT) == "[URGENT]"
    assert postbox_os.frame_time(None) is None and postbox_os.frame_time([]) is None
    assert postbox_os.urgent_quiet(NIGHT, None) is False


# ---- the window -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("utc, quiet", [
    ("2026-11-10T22:59:00", False),                 # (time withheld)
    ("2026-11-10T23:00:00", True),                  # (time withheld): the start is inside
    ("2026-11-11T06:59:00", True),                  # (time withheld)
    ("2026-11-11T07:00:00", False),                 # (time withheld): the end is outside
    ("2026-11-11T12:00:00", False),
])
def test_the_start_is_inside_the_window_and_the_end_is_not(utc, quiet):
    assert postbox_os.urgent_quiet(NIGHT, at(utc)) is quiet


@pytest.mark.parametrize("utc, quiet", [
    ("2026-11-11T20:59:00", False),                 # (time withheld)
    ("2026-11-11T21:00:00", True),                  # 22:00
    ("2026-11-11T23:30:00", True),                  # 00:30
    ("2026-11-11T04:59:00", True),                  # 05:59
    ("2026-11-11T05:00:00", False),                 # 06:00
])
def test_a_window_may_wrap_midnight(utc, quiet):
    assert postbox_os.urgent_quiet({"urgent_quiet": ["22:00", "06:00"]}, at(utc)) is quiet


def test_no_identity_or_no_setting_means_no_quiet_hours():
    night = at("2026-11-11T02:00:00")
    assert postbox_os.urgent_quiet(None, night) is False
    assert postbox_os.urgent_quiet({}, night) is False
    assert postbox_os.urgent_quiet_problem({}) is None and postbox_os.urgent_quiet_problem(None) is None
    for planted in ([], "x", 7, [NIGHT]):                   # an identity that is not a mapping never raises
        assert postbox_os.urgent_quiet(planted, night) is False and postbox_os.urgent_quiet_report(planted) is None
        assert postbox_os.kind_tag(ev(when=IN_QUIET, urgent=True), planted) == "[URGENT]"
        assert postbox_os.role_note(ev(when=IN_QUIET, urgent=True), planted).startswith(" (urgent: delivery order only")


@pytest.mark.parametrize("value", [
    "00:00-08:00", ["00:00"], ["00:00", "08:00", "09:00"], [0, 8], ["0:00", "8:00"], ["24:00", "08:00"],
    ["00:60", "08:00"], ["00:00", "08:00\n"], ["08:00", "08:00"], {"start": "00:00", "end": "08:00"}, True, [None, None],
    [chr(0x660) * 2 + ":00", "08:00"], [" 00:00", "08:00"], ["00:00", ["08:00"]], [],
])
def test_an_unreadable_setting_is_no_quiet_hours_and_is_named(value):
    """A typing mistake costs the receiver its quiet, never a sender its urgency, and the problem has words."""
    cfg = {"urgent_quiet": value}
    assert postbox_os.urgent_quiet_window(cfg) is None
    assert postbox_os.urgent_quiet(cfg, at("2026-11-11T02:00:00")) is False
    assert postbox_os.urgent_quiet_problem(cfg)
    assert postbox_os.kind_tag(ev(when=IN_QUIET, urgent=True), cfg) == "[URGENT]"


def test_the_start_report_names_the_identity_and_the_mistake_and_is_silent_otherwise():
    line = postbox_os.urgent_quiet_report({"name": "seat-prod", "urgent_quiet": ["8:00", "09:00"]})
    assert line.startswith("seat-prod: ") and "HH:MM" in line and "urgent stays urgent" in line
    assert postbox_os.urgent_quiet_report({"name": "seat-prod"}) is None
    assert postbox_os.urgent_quiet_report(dict(NIGHT, name="seat-prod")) is None


def test_both_delivery_loops_report_it_at_their_start(pb, channel):
    """The report is one call in each loop's start; a loop that drops it leaves a typing mistake silent."""
    import inspect
    assert "postbox_os.urgent_quiet_report(ident)" in inspect.getsource(pb.run_codex_delivery).split("while ")[0]
    assert "postbox_os.urgent_quiet_report(ident)" in inspect.getsource(channel.tail).split("while True")[0]


# ---- the tag, the note and the order ---------------------------------------------------------------------------

def test_a_message_created_in_the_window_is_steering_and_the_note_says_why(no_clock):
    quiet, loud = ev(when=IN_QUIET, urgent=True), ev(when=NOON, urgent=True)
    assert postbox_os.kind_tag(quiet, NIGHT) == "[STEERING]"
    note = postbox_os.role_note(quiet, NIGHT)
    assert "sent urgent, delivered as ordinary" in note and "00:00 to 08:00 Vienna" in note
    assert "delivery order only" not in note
    assert postbox_os.kind_tag(loud, NIGHT) == "[URGENT]"
    assert postbox_os.role_note(loud, NIGHT) == " (urgent: delivery order only; a peer's request)"


def test_the_edge_second_belongs_to_one_side_only(no_clock):
    assert postbox_os.kind_tag(ev(when=LAST_QUIET, urgent=True), NIGHT) == "[STEERING]"
    assert postbox_os.kind_tag(ev(when=FIRST_LOUD, urgent=True), NIGHT) == "[URGENT]"


def test_without_an_identity_every_helper_answers_as_before(no_clock):
    d = ev(when=IN_QUIET, urgent=True)
    assert postbox_os.kind_tag(d) == "[URGENT]"
    assert postbox_os.role_note(d) == " (urgent: delivery order only; a peer's request)"
    assert [x["key"] for x in postbox_os.urgent_first([ev("msg/a"), ev("msg/b", urgent=True)])] == ["msg/b", "msg/a"]


def test_quiet_hours_touch_nothing_but_an_urgent_to_frame(no_clock):
    plain = ev(when=IN_QUIET)
    assert postbox_os.kind_tag(plain, NIGHT) == "[STEERING]" and postbox_os.role_note(plain, NIGHT) == ""
    cc = dict(ev(when=IN_QUIET, urgent=True), role="cc")
    assert postbox_os.kind_tag(cc, NIGHT) == "[MESSAGE]" and postbox_os.role_note(cc, NIGHT) == " (cc)"
    declined = ev(when=IN_QUIET, urgent_declined=True)
    assert postbox_os.role_note(declined, NIGHT) == " (urgent declined: its sender says its hourly allowance was spent)"
    assert postbox_os.kind_tag(dict(ev(when=IN_QUIET, urgent=True), event="edit"), NIGHT) == "[RECALL]"


def test_a_burst_across_the_end_of_the_window_puts_each_message_in_exactly_one_place(no_clock):
    """The quiet one keeps its place, the loud one goes first, and nothing is listed twice or dropped."""
    evs = [ev("msg/a", IN_QUIET), ev("msg/b", LAST_QUIET, urgent=True), ev("msg/c", FIRST_LOUD),
           ev("msg/d", FIRST_LOUD, urgent=True)]
    out = [d["key"] for d in postbox_os.urgent_first(evs, NIGHT)]
    assert out == ["msg/d", "msg/a", "msg/b", "msg/c"]
    assert sorted(out) == sorted(d["key"] for d in evs)
    assert [d["key"] for d in postbox_os.urgent_first(evs, {})] == ["msg/b", "msg/d", "msg/a", "msg/c"]


def test_the_answer_for_a_message_never_changes_between_two_askings(no_clock):
    for when in (IN_QUIET, LAST_QUIET, FIRST_LOUD, NOON):
        d = ev(when=when, urgent=True)
        answers = {(postbox_os.kind_tag(d, NIGHT), postbox_os.role_note(d, NIGHT), postbox_os.urgent_shown(d, NIGHT))
                   for _ in range(5)}
        assert len(answers) == 1


# ---- the Codex lane (pxx 2edad1 P2-1 and P2-2) -----------------------------------------------------------------

def test_the_codex_lane_goes_by_the_messages_time_and_still_takes_a_directive(pb, no_clock):
    assert pb.codex_priority(NIGHT, ev(when=IN_QUIET, urgent=True)) is False
    assert pb.codex_priority({}, ev(when=IN_QUIET, urgent=True)) is True                    # no setting: unchanged
    assert pb.codex_priority(NIGHT, ev(when=IN_QUIET, directive=True)) is True              # a directive is not urgency
    assert pb.codex_priority(dict(NIGHT, priority_from=["p1"]), ev(when=IN_QUIET)) is True  # nor is a listed sender
    assert pb.codex_priority(NIGHT, ev(when=NOON, urgent=True)) is True
    assert pb.codex_priority(dict(NIGHT, urgent_from=["p2"]), ev(when=NOON, urgent=True)) is False   # still narrows
    assert pb.codex_priority(NIGHT, dict(ev(urgent=True), created_at="yesterday")) is True  # unknown time: urgent


def test_the_partition_holds_every_event_exactly_once_across_both_edges(pb, no_clock):
    """P2-1: with a predicate that changed between two calls an urgent message fell out of the batch (and the bank
    committed it as shown) or stood in it twice."""
    batch = [ev("msg/a", IN_QUIET), ev("msg/b", LAST_QUIET, urgent=True), ev("msg/c", FIRST_LOUD, urgent=True),
             ev("msg/d", "2026-11-10T22:59:59Z", urgent=True), ev("msg/e", "2026-11-10T23:00:00Z", urgent=True)]
    out = [d["key"] for d in pb.codex_priority_first(NIGHT, batch)]
    assert out == ["msg/c", "msg/d", "msg/a", "msg/b", "msg/e"]
    assert sorted(out) == ["msg/a", "msg/b", "msg/c", "msg/d", "msg/e"]


def test_the_partition_judges_each_event_once_even_if_the_predicate_were_to_change(pb, monkeypatch):
    """Belt and braces for P2-1: a predicate that flips on every call still puts each event in exactly one list."""
    calls = []

    def flipping(_ident, d):
        calls.append(d["key"])
        return len(calls) % 2 == 1

    monkeypatch.setattr(pb, "codex_priority", flipping)
    batch = [ev("msg/a"), ev("msg/b"), ev("msg/c")]
    out = [d["key"] for d in pb.codex_priority_first({}, batch)]
    assert sorted(out) == ["msg/a", "msg/b", "msg/c"] and calls == ["msg/a", "msg/b", "msg/c"]


def test_a_notice_the_lane_sent_stays_priority_after_quiet_begins(pb, no_clock):
    """P2-2: the scan keeps a handed-on pair in `examined` only while codex_priority is true for it. An urgent
    message created at (time withheld) and still unconsumed at 00:30 must give the answer it gave when it was sent, or
    the prune drops its ledger entry and the same notice is queued again."""
    sent_before_quiet = ev("msg/late", "2026-11-10T22:59:59Z", urgent=True)        # (time withheld)
    assert pb.codex_priority(NIGHT, sent_before_quiet) is True
    assert pb.codex_priority(NIGHT, sent_before_quiet) is True                     # asked again in the small hours
    held_in_quiet = ev("msg/night", "2026-11-10T23:00:00Z", urgent=True)           # (time withheld)
    assert pb.codex_priority(NIGHT, held_in_quiet) is False
    assert pb.codex_priority(NIGHT, held_in_quiet) is False                        # and still ordinary after 08:00


def test_the_codex_notice_tag_follows_the_message(pb, tmp_path, monkeypatch, no_clock):
    monkeypatch.setattr(pb, "graphline", None, raising=False)
    cfg = dict(ident(tmp_path), **NIGHT)
    assert pb.codex_brief(cfg, dict(message("msg/q", urgent=True), created_at=IN_QUIET)).startswith("[STEERING] ")
    assert pb.codex_brief(cfg, dict(message("msg/q", urgent=True), created_at=NOON)).startswith("[URGENT] ")


# ---- the Claude channel (pxx 2edad1 P3) ------------------------------------------------------------------------

def test_the_channel_event_tag_note_and_order_agree_for_a_message(channel, tmp_path, no_clock):
    cfg = dict(ident(tmp_path), **NIGHT)
    quiet = dict(message("msg/q", urgent=True), created_at=IN_QUIET)
    content, meta = channel.message_event(cfg, quiet)
    assert content.startswith("[STEERING] ") and meta["tag"] == "[STEERING]"
    assert "sent urgent, delivered as ordinary: this seat's quiet hours, 00:00 to 08:00 Vienna" in content
    assert channel.brief_line(cfg, quiet).startswith("[STEERING] ")
    loud = dict(message("msg/l", urgent=True), created_at=NOON)
    content, meta = channel.message_event(cfg, loud)
    assert content.startswith("[URGENT] ") and meta["tag"] == "[URGENT]"
    assert "urgent: delivery order only" in content
    order = [d["key"] for d in channel.postbox_os.urgent_first([quiet, loud], cfg)]
    assert order == ["msg/l", "msg/q"]                       # the one tagged [URGENT] is the one that goes first
