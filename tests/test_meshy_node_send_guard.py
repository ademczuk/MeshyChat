"""`send` refuses volume and size before anything is sent (kxxx's ruling of 2026-09-22, thread meshychat-send-volume).

THE FAILURE: one seat sent 39 messages to one peer in about two hours, mostly bundles of inline source files, one bundle per
revision, each fixing the previous review before it was verified. See tools/meshy_node/sendguard.py for the rule, the numbers
(6 large messages, a body of 4 KB or more, per recipient per 30 minutes; 16 KB) and the override (`--bulk-ack "<reason>"`, written into the message).

EVERY CONTROL SITS BESIDE ITS REFUSAL, AND EVERY CONTROL HAS A MUTANT. `CONTROLS` are checked against the real module and then
against textual mutants of tools/meshy_node/sendguard.py and cli.py; each mutant must fail at least one control, so a control
that cannot go red is caught here rather than trusted. The replay test runs the recipient and time sequence of the REAL 39
sends (labels and timestamps only, tests/fixtures/send_volume_replay.json) through the check and asserts where it refuses.

No network and no database: node.send is replaced, as tests/test_meshy_node_self_send.py does.

Run:  .venv/Scripts/python.exe -m pytest tests/test_meshy_node_send_guard.py -q -p no:cacheprovider
"""
from __future__ import annotations

import json
import sys
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.meshy_node import cli, node, sendguard  # noqa: E402

WINDOW, LIMIT, MAXB = sendguard.WINDOW_S, sendguard.MAX_PER_RECIPIENT, sendguard.MAX_BODY_BYTES
BIG = "x" * sendguard.COUNTED_MIN_BYTES         # the smallest body the volume limit counts (divergence 068, amended)
ANNOUNCE = "pushed abc1234def: apps/x/handler.py and tests/test_handler.py, review when you can"
BUNDLE = "```python\n" + "def f(x):\n    return x + 1\n" * 120 + "```\nthe full apps/x/handler.py, as above"   # about 3.2 KB, under BIG
SRC = (ROOT / "tools" / "meshy_node" / "sendguard.py").read_text(encoding="utf-8")
CLI_SRC = (ROOT / "tools" / "meshy_node" / "cli.py").read_text(encoding="utf-8")


class Refused(Exception):
    pass


# ----------------------------------------------------------------------------------- controls on the module API
def _send(mod, state, to, *, cc=None, body=BIG, subject=None, ack=None, now=1000.0, record=True):
    """One send through admit and (when the server accepted it) record, as cli.run does, size included. Returns (body, subject)."""
    out = mod.admit(state, to=to, cc=cc, body=body, subject=subject, bulk_ack=ack, refused=Refused, now=now)
    if record:
        mod.record(state, to=to, cc=cc, key="msg/1", now=now, body_bytes=len(out[0].encode("utf-8")), body=out[0])
    return out


def c_six_pass_seventh_refused(mod, tmp):
    st = tmp / "s.json"
    for i in range(LIMIT):
        _send(mod, st, ["peer"], now=1000.0 + i)
    with pytest.raises(Refused) as e:
        _send(mod, st, ["peer"], now=1000.0 + LIMIT)
    msg = str(e.value)
    assert str(LIMIT) in msg and "--bulk-ack" in msg and "peer" in msg


def c_seventh_passes_with_ack_and_reason_is_in_the_message(mod, tmp):
    st = tmp / "s.json"
    for i in range(LIMIT):
        _send(mod, st, ["peer"], now=1000.0 + i)
    body, subject = _send(mod, st, ["peer"], body=BIG + "payload", ack="one verified review, fixes batched", now=1010.0)
    assert body.startswith("[bulk-ack] one verified review, fixes batched\n") and body.endswith("payload")
    assert subject == "bulk-ack"
    assert _send(mod, st, ["other"], body="x", subject="kept", ack="r", now=1011.0)[1] == "kept"


def c_two_recipients_do_not_share_a_count(mod, tmp):
    st = tmp / "s.json"
    for i in range(LIMIT):
        _send(mod, st, ["a"], now=1000.0 + i)
    _send(mod, st, ["b"], now=1010.0)                      # b has sent nothing
    with pytest.raises(Refused):
        _send(mod, st, ["a", "b"], now=1011.0)             # a is at the limit, whoever else is named


def c_cc_counts_and_case_is_ignored(mod, tmp):
    st = tmp / "s.json"
    for i in range(LIMIT):
        _send(mod, st, ["x"], cc=["Peer"], now=1000.0 + i)
    with pytest.raises(Refused):
        _send(mod, st, ["PEER"], now=1010.0)


def c_the_window_expires(mod, tmp):
    st = tmp / "s.json"
    for i in range(LIMIT):
        _send(mod, st, ["peer"], now=1000.0 + i)
    _send(mod, st, ["peer"], now=1000.0 + WINDOW + LIMIT, record=False)   # the oldest entries are past the window
    with pytest.raises(Refused):
        _send(mod, st, ["peer"], now=1000.0 + WINDOW - 2)                 # still inside it


def c_an_empty_reason_is_refused(mod, tmp):
    st = tmp / "s.json"
    for reason in ("", "   ", "\n\t"):
        with pytest.raises(Refused):
            _send(mod, st, ["peer"], ack=reason)


def c_size_limit_and_its_override(mod, tmp):
    st = tmp / "s.json"
    _send(mod, st, ["peer"], body="x" * MAXB, record=False)                 # exactly the limit passes
    with pytest.raises(Refused) as e:
        _send(mod, st, ["peer"], body="x" * (MAXB + 1), record=False)
    assert "share" in str(e.value) and "--bulk-ack" in str(e.value) and str(MAXB) in str(e.value)
    assert _send(mod, st, ["peer"], body="x" * (MAXB + 1), ack="a spec the peer asked for by name", record=False)[0].startswith("[bulk-ack]")
    with pytest.raises(Refused):                                             # bytes, not characters
        _send(mod, st, ["peer"], body="é" * (MAXB // 2 + 1), record=False)


def c_an_unreadable_ledger_is_refused_not_guessed(mod, tmp):
    st = tmp / "s.json"
    mod.ledger_path(st).write_text("not json", encoding="utf-8")
    with pytest.raises(Refused) as e:
        _send(mod, st, ["peer"], record=False)
    assert str(mod.ledger_path(st)) in str(e.value)
    assert _send(mod, st, ["peer"], ack="ledger damaged, volume unknown", record=False)[0].startswith("[bulk-ack]")
    mod.record(st, to=["peer"], cc=None, key="msg/2", now=1000.0)                     # a record after damage starts a clean ledger
    assert json.loads(mod.ledger_path(st).read_text(encoding="utf-8"))[0]["to"] == "peer"


def c_short_messages_are_never_refused_or_counted(mod, tmp):
    st = tmp / "s.json"
    for i in range(LIMIT * 3):
        _send(mod, st, ["peer"], body="a short coordination note", now=1000.0 + i * 70)
    led = mod.ledger_path(st)
    assert not led.exists() or json.loads(led.read_text(encoding="utf-8")) == [], "a short message entered the ledger"
    for i in range(LIMIT):                                              # and they leave the full budget for large ones
        _send(mod, st, ["peer"], now=2500.0 + i)
    with pytest.raises(Refused):
        _send(mod, st, ["peer"], now=2510.0)


def c_pair_loop_two_way_ping_pong_and_thirty_one_way_cap(mod, tmp):
    # 1. Ordinary one-way catch-up (15 short sends in 10 min with < 6 replies) is NOT refused (divergence 068)
    st_catchup = tmp / "s_catchup.json"
    for i in range(15):
        _send(mod, st_catchup, ["peer"], body=f"short catch-up {i}", now=1000.0 + i)
    assert _send(mod, st_catchup, ["peer"], body="sixteenth short catch-up", now=1020.0)[0] == "sixteenth short catch-up"

    # 2. Two-way ping-pong (>= 6 sends to peer AND >= 6 replies from peer in 10 min) IS refused
    st_pingpong = tmp / "s_pingpong.json"
    for i in range(mod.PAIR_LOOP_TWO_WAY_MIN):
        _send(mod, st_pingpong, ["peer"], body=f"ping {i}", now=1000.0 + i * 2)
        mod.record_inbound(st_pingpong, sender="Peer", now=1001.0 + i * 2)
    with pytest.raises(Refused) as e_pp:
        _send(mod, st_pingpong, ["peer"], body="seventh ping in ping-pong loop", now=1020.0)
    assert "pair-loop guard: 6 sends to peer and 6 replies from peer in 10 min" in str(e_pp.value)
    assert "--bulk-ack" in str(e_pp.value)
    assert _send(mod, st_pingpong, ["peer"], body="seventh ping", ack="interactive pair debug", now=1021.0)[0].startswith("[bulk-ack]")

    # 3. One-way burst cap (30 sends in 10 min) IS refused on the 31st send
    st = tmp / "s.json"
    for i in range(mod.PAIR_LOOP_ONE_WAY_CAP):
        _send(mod, st, ["peer"], body=f"short ping {i}", now=1000.0 + i)
    with pytest.raises(Refused) as e:
        _send(mod, st, ["peer"], body="short ping 30", now=1050.0)
    assert "30 sends to peer in 10 min: pair-loop one-way cap" in str(e.value) and "--bulk-ack" in str(e.value)
    assert _send(mod, st, ["peer"], body="short ping 30", ack="interactive pair debug", now=1051.0)[0].startswith("[bulk-ack]")
    assert _send(mod, st, ["peer"], body="short ping after window", now=1000.0 + mod.PAIR_LOOP_WINDOW_S + 60.0, record=False)[0] == "short ping after window"
    # Records with body_bytes set but body omitted also update the pairwise loop ledger
    st_no_body = tmp / "s_no_body.json"
    for i in range(mod.PAIR_LOOP_ONE_WAY_CAP):
        mod.record(st_no_body, to=["peer"], cc=None, key=f"msg/{i}", now=2000.0 + i, body_bytes=200)
    with pytest.raises(Refused) as e2:
        _send(mod, st_no_body, ["peer"], body="thirty-first send after bodyless records", now=2050.0, record=False)
    assert "30 sends to peer in 10 min: pair-loop one-way cap" in str(e2.value)

    # 4. Idempotent op_drain re-record counts a settled operation key at most once in .sendpairs
    st_idem = tmp / "s_idem.json"
    for _ in range(mod.PAIR_LOOP_ONE_WAY_CAP + 5):
        mod.record(st_idem, to=["peer"], cc=None, key="msg/op-fixed-1", now=3000.0, body_bytes=40, body="op body", idempotent=True)
    assert len(json.loads(mod.pair_ledger_path(st_idem).read_text(encoding="utf-8"))) == 1
    assert _send(mod, st_idem, ["peer"], body="second distinct send", now=3010.0, record=False)[0] == "second distinct send"

    # 5. Late-recorded older op (sent_at < current time) keeps newer .sendpairs entries inside the window
    st_ooo = tmp / "s_ooo.json"
    for i in range(mod.PAIR_LOOP_ONE_WAY_CAP - 1):
        mod.record(st_ooo, to=["peer"], cc=None, key=f"msg/new-{i}", now=4050.0 + i, body_bytes=40, body="newer")
    # Late op_drain record for an op sent 40s earlier (4010.0 < 4050.0): must NOT evict the 29 newer entries
    mod.record(st_ooo, to=["peer"], cc=None, key="msg/late-op", now=4010.0, body_bytes=40, body="older", idempotent=True)
    with pytest.raises(Refused) as e_ooo:
        _send(mod, st_ooo, ["peer"], body="thirty-first after out-of-order op_drain", now=4100.0, record=False)
    assert "30 sends to peer in 10 min: pair-loop one-way cap" in str(e_ooo.value)

    # 6. pair_loop=False (offer/pickup path) bypasses check_pair_loop, and reset_ledgers clears all 3 sidecars
    assert mod.admit(st_ooo, to=["peer"], cc=None, body="meshy-offer/1 ...", subject=None,
                     bulk_ack=None, refused=Refused, now=4100.0, pair_loop=False) == ("meshy-offer/1 ...", None)
    mod.reset_ledgers(st_ooo)
    assert not mod.ledger_path(st_ooo).exists()
    assert not mod.token_ledger_path(st_ooo).exists()
    assert not mod.pair_ledger_path(st_ooo).exists()
    assert _send(mod, st_ooo, ["peer"], body="clean after reset_ledgers", now=4101.0, record=False)[0] == "clean after reset_ledgers"

    # 7. Future-dated pair entries (clock correction) and Infinity stamps do not refuse current short sends
    st_future = tmp / "s_future.json"
    future = 31537000.0
    for i in range(mod.PAIR_LOOP_ONE_WAY_CAP):
        mod.record(st_future, to=["peer"], cc=None, key=f"msg/fut-{i}", now=future + i, body_bytes=5, body="short")
    assert mod.refusals(st_future, to=["peer"], cc=None, body="short", now=1000.0) == []
    assert _send(mod, st_future, ["peer"], body="short", now=1000.0, record=False)[0] == "short"
    # Infinity stamp in .sendpairs (and direct check_pair_loop) never refuses
    st_inf = tmp / "s_inf.json"
    mod.pair_ledger_path(st_inf).write_text(
        json.dumps([{"to": "peer", "t": float("inf"), "key": f"msg/inf-{i}"} for i in range(mod.PAIR_LOOP_ONE_WAY_CAP)]),
        encoding="utf-8",
    )
    assert mod.refusals(st_inf, to=["peer"], cc=None, body="short", now=1000.0) == []
    assert mod.check_pair_loop(
        [{"to": "peer", "t": float("inf")} for _ in range(mod.PAIR_LOOP_ONE_WAY_CAP)],
        "peer",
        1000.0,
    ) is None


def c_short_messages_pass_while_large_ones_are_at_the_limit(mod, tmp):
    st = tmp / "s.json"
    for i in range(LIMIT):
        _send(mod, st, ["peer"], now=1000.0 + i)
    assert _send(mod, st, ["peer"], body="ack, merging now", now=1010.0)[0] == "ack, merging now"
    mod.ledger_path(st).write_text("not json", encoding="utf-8")       # a damaged ledger does not refuse what it never counts
    assert _send(mod, st, ["peer"], body="still fine", now=1011.0, record=False)[0] == "still fine"


def c_the_threshold_is_bytes_and_exact(mod, tmp):
    st = tmp / "s.json"
    for i in range(LIMIT):
        _send(mod, st, ["peer"], body="x" * (mod.COUNTED_MIN_BYTES - 1), now=1000.0 + i)   # one byte short: not counted
    _send(mod, st, ["peer"], body="x" * (mod.COUNTED_MIN_BYTES - 1), now=1010.0)
    for i in range(LIMIT):
        _send(mod, st, ["peer"], body="x" * mod.COUNTED_MIN_BYTES, now=1700.0 + i)       # exactly the threshold: counted
    with pytest.raises(Refused):
        _send(mod, st, ["peer"], body="x" * mod.COUNTED_MIN_BYTES, now=1710.0)
    with pytest.raises(Refused):                                        # bytes, not characters
        _send(mod, st, ["peer"], body="é" * (mod.COUNTED_MIN_BYTES // 2), now=1711.0)


def c_an_announced_bundle_is_refused_and_the_ack_passes(mod, tmp):
    st = tmp / "s.json"
    assert mod.is_file_bundle(BUNDLE) and not mod.is_file_bundle(ANNOUNCE)
    _send(mod, st, ["peer"], body=ANNOUNCE, now=1000.0)
    with pytest.raises(Refused) as e:
        _send(mod, st, ["peer"], body=BUNDLE, now=1010.0)
    assert "announced" in str(e.value) and "--bulk-ack" in str(e.value) and "share" in str(e.value)
    assert _send(mod, st, ["peer"], body=BUNDLE, ack="peer asked for handler.py by name", now=1011.0)[0].startswith("[bulk-ack]")


def c_an_unannounced_bundle_or_another_recipient_passes(mod, tmp):
    st = tmp / "s.json"
    assert _send(mod, st, ["peer"], body=BUNDLE, now=1000.0)[0] == BUNDLE              # nothing announced yet
    _send(mod, st, ["peer"], body=ANNOUNCE, now=1001.0)
    assert _send(mod, st, ["other"], body=BUNDLE, now=1002.0)[0] == BUNDLE             # announced to someone else
    assert _send(mod, st, ["peer"], body="thanks, I read apps/x/handler.py", now=1003.0)[0].startswith("thanks")   # prose names it: fine


def c_the_token_ledger_holds_hashes_only(mod, tmp):
    st = tmp / "s.json"
    _send(mod, st, ["peer"], body=ANNOUNCE, now=1000.0)
    text = mod.token_ledger_path(st).read_text(encoding="utf-8")
    assert "handler.py" not in text and "abc1234def" not in text and "apps/x" not in text
    entry = json.loads(text)[0]
    # The send's message key may ride along (GitHub Codex r4366959130); it names a message, never a file.
    assert set(entry) - {"key"} == {"to", "t", "h"} and all(len(h) == 16 for h in entry["h"])
    assert not entry.get("key") or entry["key"].startswith("msg/")


def c_the_bundle_window_expires_and_a_damaged_token_ledger_refuses_nothing(mod, tmp):
    st = tmp / "s.json"
    _send(mod, st, ["peer"], body=ANNOUNCE, now=1000.0)
    assert _send(mod, st, ["peer"], body=BUNDLE, now=1000.0 + WINDOW + 1, record=False)[0] == BUNDLE
    with pytest.raises(Refused):
        _send(mod, st, ["peer"], body=BUNDLE, now=1000.0 + WINDOW - 1, record=False)
    mod.token_ledger_path(st).write_text("not json", encoding="utf-8")
    assert _send(mod, st, ["peer"], body=BUNDLE, now=1005.0, record=False)[0] == BUNDLE


def c_damaged_hash_elements_are_ignored_not_raised(mod, tmp):
    st = tmp / "s.json"
    good = sorted(mod.token_hashes(ANNOUNCE))
    mod.token_ledger_path(st).write_text(json.dumps([{"to": "peer", "t": 1000.0, "h": [{}, 7, None, "XYZ"]},
                                                     {"to": "peer", "t": 1000.0, "h": [{}] + good}]), encoding="utf-8")
    with pytest.raises(Refused):                        # the damaged entry is skipped, the good hashes still count
        _send(mod, st, ["peer"], body=BUNDLE, now=1010.0, record=False)
    assert _send(mod, st, ["peer"], body=BUNDLE, ack="asked by name", now=1011.0)[0].startswith("[bulk-ack]")


def c_concurrent_records_lose_nothing(mod, tmp):
    import threading
    st = tmp / "s.json"
    errors, n_threads, per = [], 8, 6

    def worker(i):
        try:
            for j in range(per):
                mod.record(st, to=[f"p{i}"], cc=None, key=f"msg/{i}-{j}", now=1000.0 + j, body_bytes=8192, body=ANNOUNCE)
        except Exception as exc:                        # noqa: BLE001 - any raise is the failure under test
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert not errors, errors
    assert len(json.loads(mod.ledger_path(st).read_text(encoding="utf-8"))) == n_threads * per
    assert len(json.loads(mod.token_ledger_path(st).read_text(encoding="utf-8"))) == n_threads * per
    assert not list(tmp.glob("*.tmp"))


def c_a_cited_sha_or_message_key_is_not_an_announcement(mod, tmp):
    st = tmp / "s.json"
    sha_note = "base main 79f8215, answering msg/1790504437795-memory-bxxxxx-2bd732: the gate passed"
    excerpt = ("```text\n" + "    rerun on base main 79f8215 for msg/1790504437795-memory-bxxxxx-2bd732: ok\n" * 40
               + "```\nlog excerpt, as above")
    assert mod.is_file_bundle(excerpt) and not mod.token_hashes(sha_note) and not mod.token_hashes(excerpt)
    _send(mod, st, ["peer"], body=sha_note, now=1000.0)
    assert _send(mod, st, ["peer"], body=excerpt, now=1010.0)[0] == excerpt      # the sha and the key matched before
    _send(mod, st, ["peer"], body=ANNOUNCE, now=1020.0)                           # control: a named file still does
    with pytest.raises(Refused):
        _send(mod, st, ["peer"], body=excerpt + " of apps/x/handler.py", now=1030.0, record=False)


def c_a_held_lock_records_nothing_and_a_leftover_lock_file_blocks_nothing(mod, tmp):
    st = tmp / "s.json"
    mod.record(st, to=["peer"], cc=None, key="msg/0", now=1000.0, body_bytes=8192, body=ANNOUNCE)
    lock = st.with_name("s.json" + mod.LOCK_SUFFIX)
    assert lock.exists(), "the lock file is kept, never deleted"
    before = (mod.ledger_path(st).read_text(encoding="utf-8"), mod.token_ledger_path(st).read_text(encoding="utf-8"))
    saved, mod.LOCK_WAIT_S = mod.LOCK_WAIT_S, 0.2
    try:
        with mod._ledger_lock(st) as owned:               # another holder, alive, past the wait
            assert owned
            missed = mod.record(st, to=["peer"], cc=None, key="msg/1", now=1001.0, body_bytes=8192, body=ANNOUNCE)
            assert missed and "lock" in missed, missed
    finally:
        mod.LOCK_WAIT_S = saved
    after = (mod.ledger_path(st).read_text(encoding="utf-8"), mod.token_ledger_path(st).read_text(encoding="utf-8"))
    assert after == before, "a send that did not own the lock wrote a ledger"
    mod.record(st, to=["peer"], cc=None, key="msg/2", now=1002.0, body_bytes=8192, body=ANNOUNCE)   # released: records
    assert len(json.loads(mod.ledger_path(st).read_text(encoding="utf-8"))) == 2


def c_an_unopenable_lock_file_records_nothing_and_raises_nothing(mod, tmp):
    st = tmp / "s.json"
    st.with_name("s.json" + mod.LOCK_SUFFIX).mkdir()        # the OS refuses to open it as a file
    missed = mod.record(st, to=["peer"], cc=None, key="msg/1", now=1000.0, body_bytes=8192, body=ANNOUNCE)
    assert missed and "could not be written" in missed, missed
    assert not mod.ledger_path(st).exists() and not mod.token_ledger_path(st).exists()
    assert mod.record(tmp / "t.json", to=["peer"], cc=None, key="msg/2", now=1000.0, body_bytes=8192) is None
    bad = tmp / "u.json"
    mod.ledger_path(bad).write_bytes(b"\xff\xfe not utf-8 [")      # a ledger that does not decode
    missed = mod.record(bad, to=["peer"], cc=None, key="msg/3", now=1000.0, body_bytes=8192)
    assert missed is None or "could not be written" in missed, missed


CONTROLS = [c_six_pass_seventh_refused, c_seventh_passes_with_ack_and_reason_is_in_the_message, c_two_recipients_do_not_share_a_count,
            c_cc_counts_and_case_is_ignored, c_the_window_expires, c_an_empty_reason_is_refused, c_size_limit_and_its_override,
            c_an_unreadable_ledger_is_refused_not_guessed, c_short_messages_are_never_refused_or_counted,
            c_short_messages_pass_while_large_ones_are_at_the_limit, c_the_threshold_is_bytes_and_exact,
            c_an_announced_bundle_is_refused_and_the_ack_passes, c_an_unannounced_bundle_or_another_recipient_passes,
            c_the_token_ledger_holds_hashes_only, c_the_bundle_window_expires_and_a_damaged_token_ledger_refuses_nothing,
            c_damaged_hash_elements_are_ignored_not_raised, c_concurrent_records_lose_nothing,
            c_a_cited_sha_or_message_key_is_not_an_announcement,
            c_a_held_lock_records_nothing_and_a_leftover_lock_file_blocks_nothing,
            c_an_unopenable_lock_file_records_nothing_and_raises_nothing,
            c_pair_loop_two_way_ping_pong_and_thirty_one_way_cap]


@pytest.mark.parametrize("control", CONTROLS, ids=lambda c: c.__name__)
def test_each_control_passes_on_the_real_module(control, tmp_path):
    control(sendguard, tmp_path)


def _mutant(source, old, new, name):
    assert source.count(old) == 1, (name, old)
    mod = types.ModuleType("sendguard_mutant")
    mod.__file__ = str(ROOT / "tools" / "meshy_node" / "sendguard.py")
    exec(compile(source.replace(old, new), mod.__file__, "exec"), mod.__dict__)
    return mod


MUTANTS = {
    "limit-off-by-one": ("if n >= MAX_PER_RECIPIENT:", "if n > MAX_PER_RECIPIENT:"),
    "recipients-share-one-count": ('n = sum(1 for e in entries if e["to"] == label)', "n = sum(1 for e in entries)"),
    "window-never-expires": ("if now - e[\"t\"] < WINDOW_S:", "if True:"),
    "reason-not-written-into-the-message": ('return f"[bulk-ack] {reason}\\n{body}", subject if subject else "bulk-ack"',
                                            'return body, subject'),
    "empty-reason-accepted": ('raise ValueError("--bulk-ack needs a non-empty reason: it is written into the message the recipient reads")',
                              "return None"),
    "size-limit-gone": ("if size > MAX_BODY_BYTES:", "if size > 10 ** 9:"),
    "size-counts-characters-not-bytes": ("size = len(body.encode(\"utf-8\"))", "size = len(body)"),
    "cc-not-counted": ("for label in (to or []) + (cc or []):", "for label in (to or []):"),
    "case-matters": ("folded = label.casefold()", "folded = label"),
    "unreadable-ledger-guessed-empty": ("raise LedgerUnreadable(\"not JSON\") from exc", "return []"),
    "short-messages-refused-again": ("if size < COUNTED_MIN_BYTES:", "if False:"),
    "short-sends-recorded": ("if body_bytes is None or body_bytes >= COUNTED_MIN_BYTES:", "if True:"),
    "bundle-overlap-ignored": ("            if named:", "            if False:"),
    "bundle-share-ignored": ('return len(body.encode("utf-8")) >= BUNDLE_MIN_BYTES and file_share(body) >= BUNDLE_MIN_SHARE',
                             "return True"),
    "shas-and-keys-are-tokens": ("    found = set(_PATH.findall(body)) | set(_BASENAME.findall(body))\n",
                                 "    found = set(re.findall(r'\\b[0-9a-f]{7,64}\\b', body.lower())) | set(_PATH.findall(body)) | set(_BASENAME.findall(body))\n"),
    "tokens-stored-raw": ('return {hashlib.sha256(t.encode("utf-8")).hexdigest()[:16] for t in found}', "return found"),
    "bundle-window-never-expires": ('        if e["t"] > now - WINDOW_S:\n            hashes', '        if True:\n            hashes'),
    "token-hash-elements-unchecked": ('hashes = [h for h in e["h"] if isinstance(h, str) and _HASH.fullmatch(h)]', 'hashes = list(e["h"])'),
    "record-raises-on-a-refused-file": ("    except Exception as exc:  ",
                                        "    except ZeroDivisionError as exc:  "),
    "record-raises-on-an-undecodable-ledger": ("    except Exception as exc:  ", "    except OSError as exc:  "),
    "ledger-written-without-the-lock": ("        if not owned:                                  # never write",
                                        "        if False:                                      # never write"),
    "threshold-off-by-one": ("if body_bytes is None or body_bytes >= COUNTED_MIN_BYTES:",
                             "if body_bytes is None or body_bytes > COUNTED_MIN_BYTES:"),
    "pair-loop-not-checked-in-refusals": ("        pair_why = check_pair_loop(pair_seen, label, now)",
                                          "        pair_why = None"),
    "pair-loop-not-skipped-when-disabled": ("    pair_seen = _pair_entries(pair_ledger_path(state_file), now) if pair_loop else []",
                                            "    pair_seen = _pair_entries(pair_ledger_path(state_file), now)"),
    "pair-loop-lower-bound-evicts-newer": ("def _pair_entries(path: Path, now: float, window_s: int = PAIR_LOOP_WINDOW_S) -> list[dict]:\n"
                                           '    """The pair-loop ledger entries inside `window_s`.\n\n'
                                           "    Missing or unreadable ledgers return `[]` (fails open on disk damage, unlike the volume ledger's\n"
                                           "    `LedgerUnreadable`): the pair-loop check is a coordination safety net and must not brick ordinary\n"
                                           "    short sends if its sidecar file is damaged.\n"
                                           '    """\n'
                                           "    try:\n"
                                           '        data = json.loads(path.read_text(encoding="utf-8"))\n'
                                           "    except (OSError, ValueError):\n"
                                           "        return []\n"
                                           "    if not isinstance(data, list):\n"
                                           "        return []\n"
                                           "    out = []\n"
                                           "    for e in data:\n"
                                           '        if not (isinstance(e, dict) and isinstance(e.get("t"), (int, float)) and not isinstance(e["t"], bool)):\n'
                                           "            continue\n"
                                           '        if not (now - e["t"] < window_s):',
                                           "def _pair_entries(path: Path, now: float, window_s: int = PAIR_LOOP_WINDOW_S) -> list[dict]:\n"
                                           '    """The pair-loop ledger entries inside `window_s`.\n\n'
                                           "    Missing or unreadable ledgers return `[]` (fails open on disk damage, unlike the volume ledger's\n"
                                           "    `LedgerUnreadable`): the pair-loop check is a coordination safety net and must not brick ordinary\n"
                                           "    short sends if its sidecar file is damaged.\n"
                                           '    """\n'
                                           "    try:\n"
                                           '        data = json.loads(path.read_text(encoding="utf-8"))\n'
                                           "    except (OSError, ValueError):\n"
                                           "        return []\n"
                                           "    if not isinstance(data, list):\n"
                                           "        return []\n"
                                           "    out = []\n"
                                           "    for e in data:\n"
                                           '        if not (isinstance(e, dict) and isinstance(e.get("t"), (int, float)) and not isinstance(e["t"], bool)):\n'
                                           "            continue\n"
                                           '        if not (0 <= now - e["t"] < window_s):'),
    "pair-loop-idempotent-ignored": ("                paired = ({entry.get(\"to\") for entry in pentries if stored_key and entry.get(\"key\") == stored_key}\n"
                                     "                          if idempotent else set())",
                                     "                paired = set()"),
    "pair-loop-future-stamp-counted": ('        if not (math.isfinite(e["t"]) and 0 <= now - e["t"] < window_s):',
                                       '        if not (now - e["t"] < window_s):'),
}


@pytest.mark.parametrize("name", sorted(MUTANTS))
def test_each_mutant_fails_at_least_one_control(name, tmp_path_factory):
    old, new = MUTANTS[name]
    mod = _mutant(SRC, old, new, name)
    failed = []
    for control in CONTROLS:
        try:
            control(mod, tmp_path_factory.mktemp("m"))
        except BaseException:                             # pytest.raises failures are Failed (not an Exception), a wrong value is AssertionError
            failed.append(control.__name__)
    assert failed, f"mutant {name!r} passed every control: a control that cannot go red"


@pytest.mark.parametrize("name,control", [
    ("ledger-written-without-the-lock", "c_a_held_lock_records_nothing_and_a_leftover_lock_file_blocks_nothing"),
    ("shas-and-keys-are-tokens", "c_a_cited_sha_or_message_key_is_not_an_announcement"),
])
def test_the_named_control_kills_the_mutant_by_its_assertion(name, control, tmp_path):
    """The general harness counts any raise as a kill. These must fail for the property they guard."""
    old, new = MUTANTS[name]
    mod = _mutant(SRC, old, new, name)
    fn = {c.__name__: c for c in CONTROLS}[control]
    (tmp_path / "real").mkdir()
    (tmp_path / "mut").mkdir()
    fn(sendguard, tmp_path / "real")                   # passes on the real module
    with pytest.raises((AssertionError, pytest.fail.Exception)):
        fn(mod, tmp_path / "mut")


# ----------------------------------------------------------------------------------- the real sequence of 39
def test_the_real_39_sends_would_have_been_refused_from_the_seventh(tmp_path):
    """Labels and epoch milliseconds only. Every real send is recorded (it did happen); a send is counted refused when the check
    would have said no to it. The first refusal for the main recipient is its 7th message, exactly kxxx's arithmetic."""
    rows = json.loads((ROOT / "tests" / "fixtures" / "send_volume_replay.json").read_text(encoding="utf-8"))["sends"]
    st, refused_at, per = tmp_path / "s.json", [], {}
    for i, r in enumerate(rows, 1):
        now = r["t_ms"] / 1000.0
        why = sendguard.refusals(st, to=r["to"][:1], cc=r["to"][1:], body=BIG, now=now)   # the burst was bundles
        for label in sendguard.recipients(r["to"][:1], r["to"][1:]):
            per[label] = per.get(label, 0) + 1
        if why:
            refused_at.append(i)
        sendguard.record(st, to=r["to"][:1], cc=r["to"][1:], key="k", now=now)
    main = [i for i, r in enumerate(rows, 1) if "codex-oxxxxxx-cli" in r["to"]]
    assert len(main) == 39
    assert refused_at and refused_at[0] == main[LIMIT], "the first refusal is the 7th message to that peer"
    assert len(refused_at) > 0 and per["codex-oxxxxxx-cli"] == 39
    assert [i for i, r in enumerate(rows, 1) if r["to"] == ["kxxx"]][0] not in refused_at, "a recipient never messaged before is not refused"
    ledger = sendguard.ledger_path(st).read_text(encoding="utf-8")
    assert all(set(e) == {"to", "t", "key"} for e in json.loads(ledger)), "the ledger holds recipient, time and key only"


# ----------------------------------------------------------------------------------- through the CLI
@pytest.fixture()
def cli_send(monkeypatch, tmp_path):
    calls, outcome = [], {"ok": True}
    monkeypatch.setattr(node, "send", lambda *a, **kw: calls.append(kw) or {"status": "completed", "key": "msg/9"})
    monkeypatch.setattr(node, "succeeded", lambda r: outcome["ok"])
    monkeypatch.setattr(node, "load_state", lambda *a, **kw: {"label": "me"}, raising=False)
    (tmp_path / "state.json").write_text("{}", encoding="utf-8")

    def go(*extra, body=BIG):
        return cli.main(["send", "--base-url", "http://127.0.0.1:1", "--state-file", str(tmp_path / "state.json"),
                         "--thread", "t", "--body", body, *extra])
    return go, calls, outcome, tmp_path / "state.json.sendledger"


def test_six_sends_pass_the_seventh_is_refused_with_exit_2_and_nothing_sent(cli_send, capsys):
    go, calls, _o, _l = cli_send
    for _ in range(LIMIT):
        assert go("--to", "peer") == 0
    assert len(calls) == LIMIT
    assert go("--to", "peer") == 2
    assert len(calls) == LIMIT, "the refused send reached node.send: it must stop before anything is sent"
    assert "--bulk-ack" in capsys.readouterr().err


def test_the_seventh_passes_with_a_reason_and_the_reason_is_in_the_stored_value(cli_send):
    go, calls, _o, _l = cli_send
    for _ in range(LIMIT):
        go("--to", "peer")
    assert go("--to", "peer", "--bulk-ack", "the peer asked for this file by name") == 0
    last = calls[-1]
    assert last["body"].startswith("[bulk-ack] the peer asked for this file by name\n") and last["subject"] == "bulk-ack"


def test_a_failed_send_is_not_counted(cli_send):
    go, calls, outcome, ledger = cli_send
    outcome["ok"] = False
    for _ in range(LIMIT + 3):
        go("--to", "peer")
    assert not ledger.exists() or json.loads(ledger.read_text(encoding="utf-8")) == []
    outcome["ok"] = True
    assert go("--to", "peer") == 0, "refused sends would have been counted had the failures been"


def test_the_ledger_never_holds_a_body(cli_send):
    go, _calls, _o, ledger = cli_send
    secret = "THE-BODY-TEXT-NOBODY-SHOULD-FIND-IN-THE-LEDGER"
    go("--to", "peer", "--cc", "other", body=secret + BIG)         # large, so it is counted and recorded
    text = ledger.read_text(encoding="utf-8")
    assert secret not in text and {e["to"] for e in json.loads(text)} == {"peer", "other"}
    assert set(json.loads(text)[0]) == {"to", "t", "key"}


def test_an_empty_bulk_ack_is_refused_before_anything_is_sent(cli_send):
    go, calls, _o, _l = cli_send
    assert go("--to", "peer", "--bulk-ack", " ") == 2 and calls == []


def test_an_announced_bundle_is_refused_through_the_cli(cli_send):
    go, calls, _o, _l = cli_send
    assert go("--to", "peer", body=ANNOUNCE) == 0
    assert go("--to", "peer", body=BUNDLE) == 2 and len(calls) == 1, "the bundle reached node.send"


def test_pair_loop_is_refused_through_the_cli(cli_send, capsys):
    go, calls, _o, ledger = cli_send
    for i in range(sendguard.PAIR_LOOP_ONE_WAY_CAP):
        assert go("--to", "peer", body=f"short coordination note {i}") == 0
    assert not ledger.exists() or json.loads(ledger.read_text(encoding="utf-8")) == []
    assert go("--to", "peer", body="thirty-first short ping") == 2
    assert len(calls) == sendguard.PAIR_LOOP_ONE_WAY_CAP
    assert "30 sends to peer in 10 min: pair-loop one-way cap" in capsys.readouterr().err


def test_two_way_ping_pong_refused_through_cli_read_and_send_and_killed_by_mutant(tmp_path, monkeypatch, capsys):
    """kxxx 76a999 on #351:
    1. Deliver/read 6 messages from B through the real CLI `read` path (`cli.main(["read", ...])`),
       send 6 short replies to B through `cli.main(["send", ...])`, and verify the 7th send to B is refused
       by the two-way ping-pong guard (`pair-loop guard: 6 sends to peer and 6 replies from peer in 10 min`).
    2. Reading the same message key twice counts it only once (`key` deduplication in `record_inbound`).
    3. A CLI mutant that removes the `sendguard.record_inbound(...)` call in `cmd == "read"` fails this test.
    """
    calls = []
    monkeypatch.setattr(node, "send", lambda *a, **kw: calls.append(kw) or {"status": "completed", "key": f"msg/out-{len(calls)}"})
    monkeypatch.setattr(node, "succeeded", lambda r: True)
    monkeypatch.setattr(node, "load_state", lambda *a, **kw: {"label": "me"}, raising=False)
    monkeypatch.setattr(
        node,
        "read",
        lambda state_file, state, *, key, **kw: {
            "key": key,
            "from": "peer",
            "author": "peer",
            "body": "inbound reply",
            "signature_verified": True,
        },
    )

    def run_scenario(cli_mod, state_path: Path):
        state_path.write_text("{}", encoding="utf-8")
        # 1. Read the same message 6 times -> deduplicated by key, so only 1 inbound reply recorded
        for _ in range(6):
            assert cli_mod.main(["read", "--base-url", "http://127.0.0.1:1", "--state-file", str(state_path), "--key", "msg/in-1"]) == 0
        # Read 5 more distinct inbound messages from "peer" (total 6 distinct replies from "peer")
        for idx in range(2, 7):
            assert cli_mod.main(["read", "--base-url", "http://127.0.0.1:1", "--state-file", str(state_path), "--key", f"msg/in-{idx}"]) == 0
        # Send 6 short replies to "peer" -> all 6 succeed
        for idx in range(1, 7):
            rc = cli_mod.main([
                "send", "--base-url", "http://127.0.0.1:1", "--state-file", str(state_path),
                "--thread", "t", "--to", "peer", "--body", f"short reply {idx}",
            ])
            assert rc == 0, f"send {idx} unexpectedly refused"
        # 7th send to "peer" must be refused with exit code 2 by the two-way ping-pong rule
        return cli_mod.main([
            "send", "--base-url", "http://127.0.0.1:1", "--state-file", str(state_path),
            "--thread", "t", "--to", "peer", "--body", "seventh ping-pong send",
        ])

    rc_real = run_scenario(cli, tmp_path / "real_state.json")
    err_real = capsys.readouterr().err
    assert rc_real == 2
    assert "pair-loop guard: 6 sends to peer and 6 replies from peer in 10 min" in err_real
    assert "--bulk-ack" in err_real

    # Mutant: remove `sendguard.record_inbound` from `cmd == "read"` in cli.py
    target_call = 'sendguard.record_inbound(args.state_file, sender=msg_sender, key=str(msg_doc.get("key") or args.key))'
    assert CLI_SRC.count(target_call) == 1
    mut_mod = types.ModuleType("tools.meshy_node.cli_read_mutant")
    mut_mod.__package__, mut_mod.__file__ = "tools.meshy_node", str(ROOT / "tools" / "meshy_node" / "cli.py")
    exec(compile(CLI_SRC.replace(target_call, "pass"), mut_mod.__file__, "exec"), mut_mod.__dict__)
    rc_mut = run_scenario(mut_mod, tmp_path / "mut_state.json")
    assert rc_mut == 0, "mutant without record_inbound in cli read must fail to refuse the 7th send"


def test_divergence_068_multi_recipient_and_hz_build_burst_not_refused(tmp_path):
    """Divergence 068 & kxxx 3c6e9f:
    1. 300 short sends spread across 30 recipients in one day (10 sends per recipient in 10 min) are NOT refused.
    2. 15 one-way short sends to a single recipient in 10 min (without >= 6 replies) are NOT refused.
    """
    st = tmp_path / "s068.json"
    for r_idx in range(30):
        recipient = f"peer-{r_idx}"
        for m_idx in range(10):
            now = 1000.0 + m_idx * 10
            why = sendguard.refusals(st, to=[recipient], cc=None, body=f"short ack {m_idx}", now=now)
            assert why == [], f"unexpected refusal for {recipient} at send {m_idx}: {why}"
            sendguard.record(st, to=[recipient], cc=None, key=f"msg/{r_idx}-{m_idx}", now=now, body_bytes=20, body="short")


def test_seven_offer_pickup_round_trips_pass_while_ordinary_send_pair_trips_at_seventh(tmp_path, monkeypatch):
    """7 offer/pickup round trips between two peers in 10 minutes pass without tripping check_pair_loop,
    and a control doing 6 ordinary sends + 6 inbound replies trips at the 7th."""
    from tools.meshy_node import offer

    alice_st = tmp_path / "alice.json"
    bob_st = tmp_path / "bob.json"
    alice_st.write_text("{}", encoding="utf-8")
    bob_st.write_text("{}", encoding="utf-8")

    sent_msgs = []
    monkeypatch.setattr(node, "send", lambda *a, **kw: sent_msgs.append(kw) or {"status": "completed", "key": f"msg/{len(sent_msgs)}"})
    monkeypatch.setattr(node, "succeeded", lambda r: True)

    for i in range(7):
        now = 5000.0 + i * 10
        # 1. Alice offers a file to Bob (offer.py admit + record path, plus Bob reading the offer)
        meta = {
            "name": f"part-{i}.bin",
            "size_bytes": 128,
            "sha256": "a" * 64,
            "url": f"http://127.0.0.1:9000/{'t' * 43}",
            "expires": "2026-10-16T11:42:00Z",
            "for_peer": "11111111-1111-1111-1111-111111111111",
        }
        obody = offer.offer_body(meta)
        body_out, subj_out = sendguard.admit(
            alice_st, to=["bob"], cc=None, body=obody, subject=None,
            bulk_ack=None, refused=node.Refused, now=now, pair_loop=False,
        )
        assert body_out == obody and subj_out is None
        sendguard.record(alice_st, to=["bob"], cc=None, key=f"msg/offer-{i}", now=now)
        sendguard.record_inbound(bob_st, sender="alice", key=f"msg/offer-{i}", now=now + 1)

        # 2. Bob sends the pickup receipt to Alice via offer._receipt, and Alice reads the receipt
        args = types.SimpleNamespace(state_file=bob_st, author_family="claude", sender="bob", key=f"msg/offer-{i}")
        receipt_info, ok = offer._receipt(
            args, {"label": "bob"},
            {"from": "alice", "thread_id": "t-offer"},
            {"name": meta["name"], "size_bytes": meta["size_bytes"], "sha256": meta["sha256"]},
        )
        assert ok is True and receipt_info["sent"] is True
        sendguard.record_inbound(alice_st, sender="bob", key=str(receipt_info["key"]), now=now + 2)

    # Even after 7 offer/pickup round trips, an ordinary short send from Alice to Bob is admitted
    assert sendguard.admit(
        alice_st, to=["bob"], cc=None, body="ordinary follow-up note", subject=None,
        bulk_ack=None, refused=node.Refused, now=5100.0,
    ) == ("ordinary follow-up note", None)

    # Control: an ordinary send pair doing 6 sends + 6 replies in 10 minutes trips at the 7th send
    ctrl_st = tmp_path / "ctrl_alice.json"
    ctrl_st.write_text("{}", encoding="utf-8")
    for i in range(6):
        t_now = 6000.0 + i * 10
        sendguard.admit(ctrl_st, to=["bob"], cc=None, body=f"ordinary msg {i}", subject=None,
                        bulk_ack=None, refused=node.Refused, now=t_now)
        sendguard.record(ctrl_st, to=["bob"], cc=None, key=f"msg/ctrl-out-{i}", now=t_now,
                         body_bytes=20, body=f"ordinary msg {i}")
        sendguard.record_inbound(ctrl_st, sender="bob", key=f"msg/ctrl-in-{i}", now=t_now + 1)
    with pytest.raises(node.Refused, match="pair-loop guard: 6 sends to bob and 6 replies from bob in 10 min"):
        sendguard.admit(ctrl_st, to=["bob"], cc=None, body="ordinary msg 6", subject=None,
                        bulk_ack=None, refused=node.Refused, now=6070.0)


def test_a_send_the_server_accepted_stays_exit_0_when_the_ledger_cannot_be_written(cli_send, capsys):
    go, calls, _o, ledger = cli_send
    ledger.with_name("state.json" + sendguard.LOCK_SUFFIX).mkdir()      # the lock file cannot be opened
    assert go("--to", "peer") == 0 and len(calls) == 1, "an accepted send reported failure: a retry would send it twice"
    assert "send_not_recorded" in capsys.readouterr().err
    assert not ledger.exists()


def test_the_cli_records_tokens_or_the_bundle_rule_is_blind(tmp_path, monkeypatch):
    old, new = ('body_bytes=len(text.encode("utf-8")), body=text)', 'body_bytes=len(text.encode("utf-8")))')
    assert CLI_SRC.count(old) == 1
    mod = types.ModuleType("tools.meshy_node.cli_mutant")
    mod.__package__, mod.__file__ = "tools.meshy_node", str(ROOT / "tools" / "meshy_node" / "cli.py")
    exec(compile(CLI_SRC.replace(old, new), mod.__file__, "exec"), mod.__dict__)
    calls = []
    monkeypatch.setattr(node, "send", lambda *a, **kw: calls.append(kw) or {"status": "completed", "key": "msg/9"})
    monkeypatch.setattr(node, "succeeded", lambda r: True)
    monkeypatch.setattr(node, "load_state", lambda *a, **kw: {"label": "me"}, raising=False)
    (tmp_path / "state.json").write_text("{}", encoding="utf-8")
    base = ["send", "--base-url", "http://127.0.0.1:1", "--state-file", str(tmp_path / "state.json"), "--thread", "t", "--to", "peer"]
    mod.main(base + ["--body", ANNOUNCE])
    assert mod.main(base + ["--body", BUNDLE]) == 0, "the mutant should miss the announcement, so the real CLI's refusal is load-bearing"


CLI_MUTANTS = {
    "failed-send-counted": ("        if ok:                                             # a send the server did not accept is not counted\n",
                            "        if True:\n"),
    "body-stored-in-the-ledger": ('key=result.get("key", "") if isinstance(result, dict) else "",', "key=text,"),
    "size-not-passed-so-short-sends-count": ('body_bytes=len(text.encode("utf-8")), body=text)', 'body_bytes=None, body=text)'),
}


@pytest.mark.parametrize("name", sorted(CLI_MUTANTS))
def test_each_cli_mutant_is_caught(name, tmp_path, monkeypatch):
    old, new = CLI_MUTANTS[name]
    assert CLI_SRC.count(old) == 1, name
    mod = types.ModuleType("tools.meshy_node.cli_mutant")
    mod.__package__, mod.__file__ = "tools.meshy_node", str(ROOT / "tools" / "meshy_node" / "cli.py")
    exec(compile(CLI_SRC.replace(old, new), mod.__file__, "exec"), mod.__dict__)
    calls, outcome = [], {"ok": False}
    monkeypatch.setattr(node, "send", lambda *a, **kw: calls.append(kw) or {"status": "completed", "key": "msg/9"})
    monkeypatch.setattr(node, "succeeded", lambda r: outcome["ok"])
    monkeypatch.setattr(node, "load_state", lambda *a, **kw: {"label": "me"}, raising=False)
    (tmp_path / "state.json").write_text("{}", encoding="utf-8")
    ledger = tmp_path / "state.json.sendledger"
    secret = "BODY-" + "z" * sendguard.COUNTED_MIN_BYTES       # large, so a real send of it is counted
    base = ["send", "--base-url", "http://127.0.0.1:1", "--state-file", str(tmp_path / "state.json"), "--thread", "t", "--to", "peer"]
    mod.main(base + ["--body", "a short note"])               # a failed short send
    caught = False
    outcome["ok"] = True
    mod.main(base + ["--body", "a short note"])               # an accepted short send: never counted
    if ledger.exists() and json.loads(ledger.read_text(encoding="utf-8")):
        caught = True                                         # size-not-passed-so-short-sends-count
    ledger.unlink(missing_ok=True)
    outcome["ok"] = False
    mod.main(base + ["--body", secret])                       # a failed large send
    if ledger.exists() and json.loads(ledger.read_text(encoding="utf-8")):
        caught = True                                         # failed-send-counted
    outcome["ok"] = True
    mod.main(base + ["--body", secret])
    if ledger.exists() and secret in ledger.read_text(encoding="utf-8"):
        caught = True                                         # body-stored-in-the-ledger
    assert caught, f"cli mutant {name!r} was not caught by the controls"


def test_an_op_id_retry_is_counted_once(cli_send, monkeypatch):
    """PR 2 acceptance 1 at the CLI: the first verified success of an op id is recorded in the send ledger; a
    retry with the same op id that finds it already sent is not recorded again."""
    go, _calls, _o, ledger = cli_send
    answers = iter([{"status": "completed", "key": "msg/9", "first_verified_success": True, "already_sent": False},
                    {"status": "completed", "key": "msg/9", "already_sent": True}])
    monkeypatch.setattr(node, "op_send", lambda *a, **kw: next(answers))
    claimed = [False]
    def count_once(_path, _op_id, record):
        if claimed[0]:
            return False, None
        claimed[0] = True
        return True, record({"to": ["peer"], "body": BIG}, "msg/9")
    monkeypatch.setattr(node, "op_record_sendguard", count_once)
    assert go("--to", "peer", "--op-id", "once") == 0
    assert go("--to", "peer", "--op-id", "once") == 0
    assert len(json.loads(ledger.read_text(encoding="utf-8"))) == 1


def test_a_proven_op_retry_bypasses_the_full_volume_guard(cli_send, monkeypatch):
    go, calls, _o, _ledger = cli_send
    for _ in range(LIMIT):
        assert go("--to", "peer") == 0
    stored = {"state": "succeeded", "key": "msg/first", "status": "completed", "already_sent": True,
              "value": {"to": ["peer"], "body": BIG}}
    monkeypatch.setattr(node, "op_preflight", lambda *a, **kw: stored)
    monkeypatch.setattr(node, "op_record_sendguard", lambda *a, **kw: (False, None))
    assert go("--to", "peer", "--op-id", "first") == 0
    assert len(calls) == LIMIT, "a proven retry must not reach the send path"



def test_an_idempotent_record_after_a_crash_between_the_ledgers_writes_the_tokens_once(tmp_path, monkeypatch):
    """The volume ledger written, the token ledger not (a crash between the two): the op path's re-record of the same
    key adds the missing token entries exactly once and leaves the volume ledger as it was (GitHub Codex r4366959130)."""
    state = tmp_path / "seat.json"
    body = "see tools/meshy_node/cli.py and notes.txt " + "x" * 4096
    real = sendguard._write_json
    calls = []

    def crash_on_token_write(path, data):
        calls.append(path)
        if path == sendguard.token_ledger_path(state) and len(calls) == 2:
            raise OSError("crashed before the token ledger was written")
        return real(path, data)

    monkeypatch.setattr(sendguard, "_write_json", crash_on_token_write)
    sendguard.record(state, to=["nxxxxx"], cc=None, key="msg/1", body_bytes=len(body), body=body, idempotent=True)
    monkeypatch.setattr(sendguard, "_write_json", real)
    volume_before = json.loads(sendguard.ledger_path(state).read_text(encoding="utf-8"))
    assert not sendguard.token_ledger_path(state).exists()

    for _ in range(2):                                     # the retry, then a second retry: still once
        sendguard.record(state, to=["nxxxxx"], cc=None, key="msg/1", body_bytes=len(body), body=body, idempotent=True)
    tokens = json.loads(sendguard.token_ledger_path(state).read_text(encoding="utf-8"))
    assert [e["to"] for e in tokens] == ["nxxxxx"] and tokens[0]["key"] == "msg/1"
    assert sorted(tokens[0]["h"]) == sorted(sendguard.token_hashes(body))
    assert json.loads(sendguard.ledger_path(state).read_text(encoding="utf-8")) == volume_before


def test_old_token_entries_without_a_key_still_count(tmp_path):
    """An entry written before keys were stored is kept and still counts for the bundle rule; it never matches a key."""
    state = tmp_path / "seat.json"
    now = time.time()
    sendguard.token_ledger_path(state).write_text(json.dumps([{"to": "nxxxxx", "t": now, "h": ["0" * 16]}]),
                                                  encoding="utf-8")
    entries = sendguard._token_entries(sendguard.token_ledger_path(state), now)
    assert entries == [{"to": "nxxxxx", "t": now, "h": ["0" * 16]}]
