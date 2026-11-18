"""Dropped frames must be nameable, not merely countable.

THE GAP THIS CLOSES, measured 2026-09-21 on this tree: `SendQueue` was already
bounded with drop-oldest, and already incremented `self.dropped`. Nothing read that
counter outside tests/test_ws.py (grepped, with a control showing the grep matches
where the counter IS read). So an overflowing socket discarded frames and neither the
server nor the client ever found out. docs/V1-DESIGN-RULES.md E1 names that shape: "a failure
and a pass looked identical."

The fix pairs the bounded ring with a MONOTONIC SEQUENCE (the EventHub design from
herdrdev/herdr, Apache-2.0), so a subscriber can be told which range it missed instead of
only how many. Disconnecting a subscriber with `events_lost` and making it resync from a
snapshot is a wire change proposed in docs/DIVERGENCES.md rather than taken here.

Why a sequence and not just a count: a count cannot answer the question a client has.
"You missed 5 frames" is unactionable. "You are missing sequences 4 through 8" is,
because the client knows what it received.

House rules: C1 (every negative has a positive control in the same test), B3 (absence
is typed, so None means nothing was lost rather than a zero record).

Run:  pytest tests/test_send_queue_loss.py -v   (no database needed)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from apps.wsgw.send_queue import FrameLoss, SendQueue  # noqa: E402


def test_a_queue_under_capacity_reports_no_loss():
    """Positive control for every negative below. If this fails, the others pass
    for the wrong reason because everything would look like loss."""
    q = SendQueue(capacity=4)
    for i in range(4):
        q.push_nowait({"i": i})
    assert q.dropped == 0
    assert q.take_loss() is None, "absence must be typed as None, not a zero record"
    assert len(q) == 4


def test_overflow_names_the_exact_contiguous_range_that_was_lost():
    """The whole point: the loss is a RANGE, not a tally."""
    q = SendQueue(capacity=3)
    for i in range(6):  # sequences 0..5; capacity 3, so 0,1,2 are evicted
        q.push_nowait({"i": i})

    loss = q.take_loss()
    assert loss is not None, "six frames into a capacity of three must report loss"
    assert isinstance(loss, FrameLoss)
    assert loss.count == 3
    assert (loss.first_seq, loss.last_seq) == (0, 2), (
        "the lost range must name the OLDEST and NEWEST sequence discarded"
    )
    assert loss.count == loss.last_seq - loss.first_seq + 1, "the range must be contiguous"


def test_the_survivors_start_exactly_where_the_loss_ended():
    """Proves the reported gap is the REAL gap, not a plausible-looking number.

    Without this, a loss record could be internally consistent and still describe a
    different range than the one the queue actually dropped.
    """
    q = SendQueue(capacity=3)
    for i in range(6):
        q.push_nowait({"i": i})

    loss = q.take_loss()
    seqs = [s for s, _ in q.snapshot_sequenced()]
    assert seqs == [3, 4, 5], "drop-oldest must leave the NEWEST frames"
    assert seqs[0] == loss.last_seq + 1, (
        "the first surviving sequence must follow the last lost one with no hole"
    )
    # And the payloads agree with the sequences, so the pairing is not skewed.
    assert q.snapshot() == [{"i": 3}, {"i": 4}, {"i": 5}]


def test_a_burst_accumulates_into_one_record_not_one_per_frame():
    """A notice per dropped frame is the same silent-loss problem wearing a log line."""
    q = SendQueue(capacity=2)
    for i in range(10):
        q.push_nowait({"i": i})
    loss = q.take_loss()
    assert loss.count == 8
    assert (loss.first_seq, loss.last_seq) == (0, 7)


def test_take_loss_is_consume_once():
    """Reporting the same burst forever would make one overflow look like ongoing loss."""
    q = SendQueue(capacity=2)
    for i in range(5):
        q.push_nowait({"i": i})

    first = q.take_loss()
    assert first is not None                      # positive control
    assert q.take_loss() is None, "a consumed burst must not be reported twice"

    q.push_nowait({"i": 99})                      # overflow again
    second = q.take_loss()
    assert second is not None, "a NEW overflow must still be reported after a consume"
    assert second.first_seq > first.last_seq, "the second burst must not replay the first"


def test_sequences_are_monotonic_and_never_reused_across_drain_and_refill():
    q = SendQueue(capacity=2)
    q.push_nowait("a")
    q.push_nowait("b")
    assert q.next_seq == 2
    q.pop_nowait()
    q.pop_nowait()
    assert len(q) == 0
    q.push_nowait("c")
    assert [s for s, _ in q.snapshot_sequenced()] == [2], (
        "draining must not reset the sequence; a reused sequence makes a gap unnameable"
    )


def test_the_public_frame_contract_is_unchanged():
    """The sequence is INTERNAL. Anything that looked at frames still sees frames.

    Guards the wire format: if these start returning tuples, every caller and the
    JSON on the socket changes shape.
    """
    q = SendQueue(capacity=4)
    q.push_nowait({"type": "peer.online"})
    assert q.snapshot() == [{"type": "peer.online"}]
    assert q.pop_nowait() == {"type": "peer.online"}


@pytest.mark.asyncio
async def test_async_pop_also_returns_a_bare_frame():
    q = SendQueue(capacity=2)
    await q.push({"type": "skill.invoke"})
    assert await q.pop() == {"type": "skill.invoke"}
