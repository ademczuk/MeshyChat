"""Delivery latency report (#58, PR 1): one record per message from signing to handled, joined from files on disk.

Diagnosing #58 took 13 days because no single record held enqueue and consume times. These tests plant every source
the report joins (spool, two consumers' dispatch logs, the read ledger, the handled file, builder A's skip log) with
known times and assert the exact per-stage deltas, so a wrong join shows up as a wrong number, not a plausible one.

Run:  python -m pytest tests/test_postbox_latency.py -q -p no:cacheprovider --noconftest
Mutants (each must fail at least one test here; red-proved 2026-10-23):
  L1 hand-off to read clamped at 0              caught by test_per_stage_deltas_are_exact_and_unclamped
  L2 a read of any version counts               caught by test_delivered_but_never_read_is_to_only_and_version_exact
  L3 the ledger's peer id is not checked        caught by test_a_ledger_written_for_another_peer_is_ignored
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PB = ROOT / "tools" / "postbox"
sys.path.insert(0, str(PB))

import delivery_bank  # noqa: E402
import latency  # noqa: E402

BODY = "fixture body text"
SENDER = "fixture-sender-label"


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def spool_line(key, created, rx=None, role="to", version=1, event="message"):
    d = {"event": event, "type": "mesh_message", "key": key, "version": version, "role": role,
         "from": SENDER, "preview_unverified": BODY}
    if event == "edit":
        d["edited_at"] = created            # an edit line carries edited_at and no rx_at (record_listener_line)
    else:
        d["created_at"] = created
    if rx:
        d["rx_at"] = rx
    return d


def batch(at, consumer, *pairs):
    return {"at": at, "identity": "seat", "consumer": consumer, "from": 0, "to": 1,
            "events": [{"event": "message", "key": k, "version": v, "role": "to"} for k, v in pairs]
            + [{"event": "status", "state": "listening"}]}


def plant(tmp_path, peer_id="peer-A"):
    """A seat whose history holds every case: an ordinary message (a), an addressed one never read (b), a cc never
    read (c), the #58 shape read hours before its hand-off (d), and a key read at version 1 then edited (e)."""
    log = str(tmp_path / "seat.log")
    state = str(tmp_path / "seat-state.json")
    handled = str(tmp_path / "seat.handled.json")
    write_jsonl(log, [
        spool_line("msg/a", "2026-10-15T10:00:00Z", "2026-10-15T10:00:01.000Z"),
        spool_line("msg/b", "2026-10-15T10:10:00+00:00", "2026-10-15T10:10:02.000Z"),
        spool_line("msg/c", "2026-10-15T10:20:00Z", "2026-10-15T10:20:03.000Z", role="cc"),
        {"event": "status", "state": "listening"},
        spool_line("msg/d", "2026-10-15T11:00:00Z", "2026-10-15T11:00:01.000Z"),
        spool_line("msg/e", "2026-10-15T12:00:00Z", "2026-10-15T12:00:04.000Z"),
        spool_line("msg/e", "2026-10-15T12:30:00Z", version=2, event="edit"),
    ])
    write_jsonl(log + ".codex.offset.dispatch.jsonl", [
        batch("2026-10-15T10:00:05.000Z", "codex:t", ("msg/a", 1)),
        batch("2026-10-15T10:10:12.000Z", "codex:t", ("msg/b", 1)),
        batch("2026-10-15T10:20:23.000Z", "codex:t", ("msg/c", 1)),
        batch("2026-10-15T12:00:06.000Z", "codex:t", ("msg/e", 1)),
        batch("2026-10-15T12:30:09.000Z", "codex:t", ("msg/e", 2)),
        batch("2026-10-15T14:59:01.000Z", "codex:t", ("msg/d", 1)),     # 3 h 59 min after spooling (#58)
    ])
    # A second consumer handing msg/a on later: the earliest hand-off is the stage, both consumers are listed.
    write_jsonl(log + ".channel.offset.dispatch.jsonl", [batch("2026-10-15T10:00:09.000Z", "claude-channel", ("msg/a", 1))])
    write_jsonl(log + ".codex.offset.skips.jsonl", [
        {"key": "msg/x", "version": 1, "reason": "fetched_after_spooled", "spooled_at": "2026-10-15T10:04:00Z",
         "fetched_at": "2026-10-15T10:04:30Z", "at": "2026-10-15T10:05:00Z"},
        {"key": "msg/y", "version": 1, "reason": "fetched_after_spooled", "spooled_at": "2026-10-15T10:04:00Z",
         "fetched_at": "2026-10-15T10:04:40Z", "at": "2026-10-15T10:05:10Z"},
        {"key": "msg/z", "version": 1, "reason": "turn_active", "spooled_at": "2026-10-15T12:31:00Z",
         "fetched_at": None, "at": "2026-10-15T12:31:00Z"},
    ])
    with open(state, "w", encoding="utf-8") as f:
        json.dump({"peer_id": "peer-A", "label": "seat"}, f)
    with open(state + ".read.json", "w", encoding="utf-8") as f:
        json.dump({"schema_version": 1, "peer_id": peer_id, "reads": [
            {"key": "msg/a", "version": 1, "at": "2026-10-15T10:00:35Z"},
            {"key": "msg/d", "version": 1, "at": "2026-10-15T11:00:50Z"},
            {"key": "msg/e", "version": 1, "at": "2026-10-15T12:00:16Z"},
        ]}, f)
    delivery_bank.atomic_json(handled, {"schema_version": 1, "identity": "seat", "handled": [
        {"key": "msg/a", "version": 1, "disposition": "done", "signature_verified": True, "status_verified": True,
         "handled_at": "2026-10-15T10:01:35.000Z"},
        {"key": "msg/b", "version": 1, "disposition": "maybe later", "signature_verified": True,
         "status_verified": True, "handled_at": "2026-10-15T10:11:00.000Z"},          # a bad entry is skipped
    ]})
    ident = {"name": "seat", "log": log, "state": state, "handled_file": handled, "cli": "codex", "target": "t",
             "client": str(ROOT), "handled_private_acl_verified": True}
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"base_url": "http://127.0.0.1:1", "identities": [ident]}), encoding="utf-8")
    return ident, str(config)


def test_per_stage_deltas_are_exact_and_unclamped(tmp_path):
    ident, _ = plant(tmp_path)
    rep = latency.report(ident)
    assert rep["sources"] == {"spool": "ok", "reads": "ok", "handled": "ok"}
    assert (rep["messages"], rep["handed_on"], rep["read"], rep["handled"]) == (6, 6, 3, 1)
    # created to spooled: a 1, b 2, c 3, d 1, e1 4 (the edit has no rx_at, so it is not measured here)
    assert rep["stages"]["created_to_spooled"] == {"count": 5, "p50_s": 2.0, "p95_s": 4.0}
    # spooled to handed: e1 2, a 4 (codex at :05, not the channel at :09), b 10, c 20, d 14340
    assert rep["stages"]["spooled_to_handed"] == {"count": 5, "p50_s": 10.0, "p95_s": 14340.0}
    # handed to read: d -14291 (read 11:00:50, handed 14:59:01), e1 10, a 30
    assert rep["stages"]["handed_to_read"] == {"count": 3, "p50_s": 10.0, "p95_s": 30.0}
    assert rep["stages"]["read_to_handled"] == {"count": 1, "p50_s": 60.0, "p95_s": 60.0}
    assert rep["read_before_handoff"] == 1
    rows = {(r["key"], r["version"]): r for r in rep["per_key"]}
    assert rows[("msg/d", 1)]["handed_at"] == "2026-10-15T14:59:01.000Z"
    assert rows[("msg/d", 1)]["read_at"] == "2026-10-15T11:00:50.000Z"
    assert rows[("msg/a", 1)]["consumers"] == ["channel", "codex"]
    assert rows[("msg/a", 1)]["handed_at"] == "2026-10-15T10:00:05.000Z"
    assert rep["consumers"] == {"channel": 1, "codex": 6}


def test_delivered_but_never_read_is_to_only_and_version_exact(tmp_path):
    ident, _ = plant(tmp_path)
    rep = latency.report(ident)
    # b was never read; e v2 was not either (only v1 was: kxxx's rule (a), a version >= the spooled one).
    # c is a cc and asks nothing of the seat; d was read before its hand-off, so it was fetched.
    assert rep["delivered_never_read"] == [
        {"key": "msg/b", "version": 1, "handed_at": "2026-10-15T10:10:12.000Z"},
        {"key": "msg/e", "version": 2, "handed_at": "2026-10-15T12:30:09.000Z"}]
    assert rep["outside_read_window"] == []


def test_skip_counts_by_reason_and_absent_file_is_zero(tmp_path):
    ident, _ = plant(tmp_path)
    assert latency.report(ident)["skips"] == {"fetched_after_spooled": 2, "turn_active": 1}
    os.remove(ident["log"] + ".codex.offset.skips.jsonl")
    assert latency.report(ident)["skips"] == {}
    out = latency.text(latency.report(ident))
    assert "skipped wakes: 0" in out


def test_since_filters_messages_and_skips(tmp_path):
    ident, _ = plant(tmp_path)
    rep = latency.report(ident, latency.when("2026-10-15T11:00:00Z"))
    assert sorted((r["key"], r["version"]) for r in rep["per_key"]) == [("msg/d", 1), ("msg/e", 1), ("msg/e", 2)]
    assert rep["skips"] == {"turn_active": 1}


def test_a_ledger_written_for_another_peer_is_ignored(tmp_path):
    """The ledger is this identity's only when its peer_id is the state file's (node.ledger_records). Another peer's
    reads are not this seat's fetches (kxxx's rule (a)), so every addressed message it 'read' counts as unread."""
    ident, _ = plant(tmp_path, peer_id="peer-OTHER")
    rep = latency.report(ident)
    assert rep["sources"]["reads"] == "ignored: ledger written for another peer id"
    assert rep["read"] == 0 and rep["stages"]["handed_to_read"]["count"] == 0
    assert [e["key"] for e in rep["delivered_never_read"]] == ["msg/a", "msg/b", "msg/e", "msg/e", "msg/d"]


def test_no_ledger_withholds_the_unread_list(tmp_path):
    ident, _ = plant(tmp_path)
    os.remove(ident["state"] + ".read.json")
    rep = latency.report(ident)
    assert rep["sources"]["reads"] == "missing" and rep["delivered_never_read"] is None
    assert "unknown (no usable read ledger)" in latency.text(rep)


def test_cli_output_has_no_message_text(tmp_path):
    """Both invocations the README names, text and --json: keys, times and counts only, never a preview or sender."""
    _, config = plant(tmp_path)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    runs = [[sys.executable, str(PB / "latency.py"), "--identity", "seat", "--config", config],
            [sys.executable, "-m", "tools.postbox.latency", "--identity", "seat", "--config", config, "--json"]]
    outputs = []
    for argv in runs:
        r = subprocess.run(argv, cwd=str(ROOT), capture_output=True, text=True, env=env, timeout=60)
        assert r.returncode == 0, r.stderr
        outputs.append(r.stdout)
    assert "msg/b v1 handed 2026-10-15T10:10:12.000Z" in outputs[0]
    assert json.loads(outputs[1])["stages"]["spooled_to_handed"]["p95_s"] == 14340.0
    for out in outputs:
        assert BODY not in out and SENDER not in out


def test_unknown_identity_exits_2(tmp_path):
    _, config = plant(tmp_path)
    r = subprocess.run([sys.executable, str(PB / "latency.py"), "--identity", "nobody", "--config", config],
                       capture_output=True, text=True, timeout=60, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    assert r.returncode == 2


def test_a_full_ledger_judges_the_read_window_by_when_the_message_existed(tmp_path, monkeypatch):
    """Finding 7: a message read at 10:00 whose read the full ledger has since dropped (oldest kept read 12:00),
    handed on at 13:00, the #58 shape. Judged by its hand-off it was listed as never read; judged by when it existed
    it is outside the read window. CONTROL: one created after the oldest kept read is still listed as unread."""
    ident, _ = plant(tmp_path)
    write_jsonl(ident["log"], [spool_line("msg/old", "2026-10-15T09:59:00Z", "2026-10-15T09:59:01.000Z"),
                               spool_line("msg/new", "2026-10-15T12:30:00Z", "2026-10-15T12:30:01.000Z")])
    write_jsonl(ident["log"] + ".codex.offset.dispatch.jsonl", [
        batch("2026-10-15T13:00:00.000Z", "codex:t", ("msg/old", 1), ("msg/new", 1))])
    with open(ident["state"] + ".read.json", "w", encoding="utf-8") as f:
        json.dump({"schema_version": 1, "peer_id": "peer-A",
                   "reads": [{"key": "msg/other", "version": 1, "at": "2026-10-15T12:00:00Z"}]}, f)
    node = latency.import_node(ident)
    monkeypatch.setattr(node, "READ_LEDGER_MAX", 1)                 # the ledger is full at one record
    rep = latency.report(ident)
    assert [e["key"] for e in rep["outside_read_window"]] == ["msg/old"]
    assert [e["key"] for e in rep["delivered_never_read"]] == ["msg/new"]


def test_a_rotated_skip_log_is_counted_and_found(tmp_path):
    """Finding 8: delivery_bank.dispatch_line rotates the skip log to `.1` as it does the dispatch log. The rotated
    file is counted, and found even when the consumer's offset file and current skip log are both gone."""
    ident, _ = plant(tmp_path)
    skips = ident["log"] + ".codex.offset.skips.jsonl"
    os.replace(skips, skips + ".1")
    write_jsonl(skips, [{"key": "msg/w", "version": 1, "reason": "already_read", "spooled_at": None,
                         "fetched_at": "2026-10-15T13:00:00Z", "at": "2026-10-15T13:00:01Z"}])
    assert latency.report(ident)["skips"] == {"already_read": 1, "fetched_after_spooled": 2, "turn_active": 1}
    waiter = ident["log"] + ".wait-session.offset.skips.jsonl.1"
    write_jsonl(waiter, [{"key": "msg/v", "version": 1, "reason": "already_read", "at": "2026-10-15T13:00:02Z"}])
    assert latency.report(ident)["skips"]["already_read"] == 2


@pytest.mark.parametrize("field,value", [("identity", "another-seat"), ("schema_version", 2), ("handled", {})])
def test_a_handled_file_delivery_would_refuse_gives_no_times(tmp_path, field, value):
    """Finding 11: the report read handled_at from any handled file; a file of another schema or written for another
    identity, which delivery refuses whole, now gives none. CONTROL: the planted file gives one."""
    ident, _ = plant(tmp_path)
    assert latency.report(ident)["handled"] == 1
    data = delivery_bank.private_json(ident["handled_file"])
    delivery_bank.atomic_json(ident["handled_file"], dict(data, **{field: value}))
    rep = latency.report(ident)
    assert rep["handled"] == 0 and rep["sources"]["handled"].startswith("unavailable: another schema or identity")


def test_a_handled_file_without_an_attested_acl_is_reported_on_windows(tmp_path):
    ident, _ = plant(tmp_path)
    del ident["handled_private_acl_verified"]
    rep = latency.report(ident)
    assert rep["handled"] == 1
    expected = "ok" if os.name != "nt" else "ok, but its private ACL is not attested, so delivery refuses this file"
    assert rep["sources"]["handled"] == expected


def test_the_report_writes_no_bytecode(tmp_path):
    """Finding 12: sys.dont_write_bytecode was set inside import_node, after `import delivery_bank` had already written
    bytecode beside it. A copy of the two files in a fresh folder, run as a script, leaves no __pycache__ there."""
    _, config = plant(tmp_path)
    folder = tmp_path / "copy"
    folder.mkdir()
    for name in ("latency.py", "delivery_bank.py"):
        (folder / name).write_bytes((PB / name).read_bytes())
    env = {k: v for k, v in os.environ.items() if k != "PYTHONDONTWRITEBYTECODE"}
    r = subprocess.run([sys.executable, str(folder / "latency.py"), "--identity", "seat", "--config", config],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    assert not (folder / "__pycache__").exists()


def test_the_m_form_names_the_checkout_whose_reader_ran(tmp_path):
    """Finding 12's other half: under `python -m tools.postbox.latency` the current checkout's reader runs, whatever
    the identity's client is; the report says so rather than claiming the client's."""
    ident, config = plant(tmp_path)
    other = tmp_path / "another-client"
    (other / "tools" / "meshy_node").mkdir(parents=True)
    data = json.loads(Path(config).read_text())
    data["identities"][0]["client"] = str(other)
    Path(config).write_text(json.dumps(data))
    r = subprocess.run([sys.executable, "-m", "tools.postbox.latency", "--identity", "seat", "--config", config,
                        "--json"], cwd=str(ROOT), capture_output=True, text=True, timeout=60,
                       env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    assert r.returncode == 0, r.stderr
    via = json.loads(r.stdout)["sources"]["reads_via"]
    assert via.startswith("this checkout, not the identity's client: ") and via.endswith(ROOT.name)
    (tmp_path / "b").mkdir()
    assert "reads_via" not in latency.report(plant(tmp_path / "b")[0])["sources"]   # CONTROL: its own client
