"""The Claude channel and the waiter pass over mail the seat already read, as the Codex delivery does (#58 (a)).

Before: only postbox.py's Codex delivery passed `fetched` to Bank.collect, so a Claude or Antigravity seat that had
read a message (meshy_node read writes the read ledger) was woken for it again when its spool line arrived.
CONTROL: an unread message still wakes in the same pass.
MUTANT: drop `fetched=` at either call site; the read message is delivered again. Database-free.
"""
from __future__ import annotations

import json
from pathlib import Path

from tests.test_notice_slim import channel, ident, message  # noqa: F401
from tools.meshy_node import node as meshy_node

PEER = "peer-claude-seat-0001"


def _read(i, key, version=1):
    state_path = i["state"]
    state = json.loads(Path(state_path).read_text(encoding="utf-8"))
    msg = {"key": key, "version": version, "thread_id": "t1", "signature_verified": True, "status_verified": True}
    assert meshy_node.note_fetched(state_path, msg) is True, "the fetched ledger was not written"
    meshy_node.note_read(state_path, state, msg)


def _seat(tmp_path):
    i = ident(tmp_path)
    Path(i["state"]).write_text(json.dumps({"peer_id": PEER}), encoding="utf-8")
    # The opt-in fetched ledger exists for an opted-in seat; note_fetched writes only when it does (pxx 07754b (b)).
    Path(i["state"] + ".fetched.jsonl").touch()
    Path(i["log"]).touch()
    return i


def test_the_channel_does_not_wake_for_a_message_the_seat_already_read(channel, monkeypatch, tmp_path):
    i = _seat(tmp_path)
    _read(i, "msg/already-read")
    with open(i["log"], "ab") as f:
        for d in (message("msg/already-read"), message("msg/unread")):
            f.write(json.dumps(d).encode("utf-8") + b"\n")
    out = []
    monkeypatch.setattr(channel, "push", lambda content, **meta: out.append((content, meta)))
    channel._deliver_once(i, channel.delivery_bank.Bank(i, i["log"] + ".channel.offset", 0, "claude-channel"),
                          hold_options={})
    pushed = "\n".join(content for content, _ in out)
    assert "msg/unread" in pushed, "control: an unread message still wakes"
    assert "msg/already-read" not in pushed, "the channel woke for a message the seat had already read"


def test_the_waiter_passes_the_read_ledger_to_collect(monkeypatch, tmp_path):
    """wait_next's collect call carries a `fetched` that reads this identity's ledger (the same reader the channel
    and the Codex delivery use). MUTANT: drop `fetched=` from wait_next.py; `seen` is empty."""
    import importlib.util
    from tests.test_postbox_delivery_bank import PB
    spec = importlib.util.spec_from_file_location("wait_next_fetched_under_test", PB / "wait_next.py")
    wn = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wn)
    src = (PB / "wait_next.py").read_text(encoding="utf-8")
    call = src[src.index("bank.collect(wanted, at_turn_boundary"):]
    call = call[:call.index(")\n") + 1]
    assert "fetched=delivery_bank.fetched_reader(ident, getattr(bank, \"report\", None))" in call, call

    i = _seat(tmp_path)
    _read(i, "msg/already-read")
    reader = wn.delivery_bank.fetched_reader(i)
    assert "msg/already-read" in reader(), "the reader returns this identity's read ledger"
