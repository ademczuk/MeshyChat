"""Tests for tools/postbox/context_worker.py and postbox.py wiring (ingest plan Steps 3 and 10).

Covers:
  - File-identity enrollment (`enroll_file`, `verified_enrolled_sources`): symlink, Windows reparse point
    (0x400 + directory junction where supported), non-regular file, outside-allowed-roots, cross-seat
    duplicate `(dev, ino)` (hardlink + synthetic), changed `(dev, ino)` after replacement, and
    `transcript.jsonl -> transcript_full.jsonl` upgrade for `antigravity`.
  - macOS (`darwin`) interpreter-match guard + `allow_distinct_context_python: true` override.
  - Vienna `00:00..07:00` `8 writes/s` vs `07:00..24:00` `2 writes/s` budget, `/v1/livez` 60s sampling,
    two-consecutive slow/failed probes latching `2 writes/s` for the remainder of that Vienna night,
    single-slow-then-fast recovery without latching, and automatic reset on the next Vienna night.
  - Pacing math (`writes_in_payload`, `pace_sleep_s`) and `more_work` detection (`status == "progressed"`
    or `items_committed > 0`, covering mid-batch item progress).
  - `context_held` projection (`>= 3600s` kept, `< 3600s` suppressed, unexpected content/path keys stripped,
    malformed `src`/`code` scrubbed, cleared back to `None` when resolved).
  - All `context_upload` exit codes (`0`, `2`, `3`, `4` conflict quarantine, `5` transient unreachable),
    missing `context_upload` driver handling (`driver_missing` logged once, no crash loop), child timeout
    containment, shared `store_dir` refusal, postbox.py opt-in wiring, and mail-thread fault isolation.

Run (no database needed):
  .venv/Scripts/python.exe -m pytest --noconftest tests/test_postbox_context_worker.py -q -p no:cacheprovider
"""
from __future__ import annotations

import ast
import datetime
import email.message
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PB_DIR = ROOT / "tools" / "postbox"
if str(PB_DIR) not in sys.path:
    sys.path.insert(0, str(PB_DIR))


def _load_pb_module(name: str):
    spec = importlib.util.spec_from_file_location("pb_ctx_test_" + name, PB_DIR / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


cw = _load_pb_module("context_worker")


# ---- Platform & import safety -----------------------------------------------------------------------------------

def test_no_windows_only_module_imported_at_top_level():
    for script in ("context_worker.py", "postbox.py"):
        tree = ast.parse((PB_DIR / script).read_text(encoding="utf-8"))
        top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
        names = {a.name for n in top if isinstance(n, ast.Import) for a in n.names}
        names |= {n.module for n in top if isinstance(n, ast.ImportFrom) and n.module}
        assert not (names & {"msvcrt", "ctypes", "winreg"}), (script, names)


# ---- Enrollment & file identity guards --------------------------------------------------------------------------

def test_enroll_file_records_identity_and_upgrades_antigravity_transcript_full(tmp_path):
    allowed = tmp_path / "brain"
    logs = allowed / "conv-123" / ".system_generated" / "logs"
    logs.mkdir(parents=True)
    compact = logs / "transcript.jsonl"
    full = logs / "transcript_full.jsonl"
    compact.write_text('{"step_index":0}\n', encoding="utf-8")

    # Without transcript_full.jsonl, compact path enrolls directly
    ledger1 = str(tmp_path / "seat1" / "enrollment.json")
    box_inodes: dict = {}
    ok1, reason1, entry1 = cw.enroll_file(
        ledger1,
        seat="antigravity",
        dialect="antigravity",
        file_path=str(compact),
        allowed_roots=[str(allowed)],
        box_enrolled_inodes=box_inodes,
        now_fn=lambda: "2026-10-26T08:00:00.000Z",
    )
    assert ok1 is True and reason1 == "enrolled"
    assert entry1 is not None and entry1["path"] == os.path.abspath(str(compact))
    assert entry1["first_seen_size"] == compact.stat().st_size
    assert entry1["enrolled_at"] == "2026-10-26T08:00:00.000Z"

    # Re-enrolling same unchanged file returns already_enrolled
    ok_again, reason_again, _ = cw.enroll_file(
        ledger1,
        seat="antigravity",
        dialect="antigravity",
        file_path=str(compact),
        allowed_roots=[str(allowed)],
        box_enrolled_inodes=box_inodes,
    )
    assert ok_again is True and reason_again == "already_enrolled"

    # When regular transcript_full.jsonl exists beside transcript.jsonl, antigravity upgrades to full
    logs2 = allowed / "conv-456" / ".system_generated" / "logs"
    logs2.mkdir(parents=True)
    compact2 = logs2 / "transcript.jsonl"
    full2 = logs2 / "transcript_full.jsonl"
    compact2.write_text('{"step_index":0,"truncated_fields":["content"]}\n', encoding="utf-8")
    full2.write_text('{"step_index":0,"content":"complete"}\n', encoding="utf-8")

    ok2, reason2, entry2 = cw.enroll_file(
        ledger1,
        seat="antigravity",
        dialect="antigravity",
        file_path=str(compact2),
        allowed_roots=[str(allowed)],
        box_enrolled_inodes=box_inodes,
    )
    assert ok2 is True and reason2 == "enrolled"
    assert entry2 is not None and entry2["path"] == os.path.abspath(str(full2))

    # Non-antigravity dialect never rewrites transcript.jsonl -> transcript_full.jsonl
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    c_compact = claude_dir / "transcript.jsonl"
    c_full = claude_dir / "transcript_full.jsonl"
    c_compact.write_text("{}\n", encoding="utf-8")
    c_full.write_text("{}\n", encoding="utf-8")
    ledger_c = str(tmp_path / "claude_store" / "enrollment.json")
    ok_c, _, entry_c = cw.enroll_file(
        ledger_c,
        seat="claude-seat",
        dialect="claude",
        file_path=str(c_compact),
        allowed_roots=[str(claude_dir)],
    )
    assert ok_c is True and entry_c is not None and entry_c["path"] == os.path.abspath(str(c_compact))


def test_upgrade_antigravity_path_refuses_non_regular_or_reparse_full_sibling(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    compact = logs / "transcript.jsonl"
    full = logs / "transcript_full.jsonl"
    compact.write_text("{}\n", encoding="utf-8")

    # Directory named transcript_full.jsonl -> stays on compact
    full.mkdir()
    assert cw.upgrade_antigravity_path(str(compact), "antigravity") == str(compact)
    full.rmdir()

    full.write_text("{}\n", encoding="utf-8")
    assert cw.upgrade_antigravity_path(str(compact), "antigravity") == str(full)
    assert cw.upgrade_antigravity_path(str( logs / "other.jsonl"), "antigravity") == str(logs / "other.jsonl")

    real_lstat = cw.os.lstat

    class _FakeStat:
        def __init__(self, mode, attrs=0):
            self.st_mode = mode
            self.st_file_attributes = attrs

    monkeypatch.setattr(
        cw.os,
        "lstat",
        lambda p: _FakeStat(stat.S_IFLNK | 0o777, 0) if os.path.abspath(p) == os.path.abspath(str(full)) else real_lstat(p),
    )
    assert cw.upgrade_antigravity_path(str(compact), "antigravity") == str(compact)

    monkeypatch.setattr(
        cw.os,
        "lstat",
        lambda p: _FakeStat(stat.S_IFREG | 0o600, cw.REPARSE_POINT_ATTR)
        if os.path.abspath(p) == os.path.abspath(str(full))
        else real_lstat(p),
    )
    assert cw.upgrade_antigravity_path(str(compact), "antigravity") == str(compact)


def test_enroll_file_refuses_outside_root_non_regular_symlink_and_reparse_point(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret_file = outside / "secret.jsonl"
    secret_file.write_text("{}\n", encoding="utf-8")
    ledger = str(tmp_path / "store" / "enrollment.json")

    # 1. Invalid dialect & empty path
    assert cw.enroll_file(ledger, seat="s1", dialect="bogus", file_path=str(secret_file), allowed_roots=[str(allowed)]) == (
        False,
        "invalid_dialect",
        None,
    )
    assert cw.enroll_file(ledger, seat="s1", dialect="claude", file_path="  ", allowed_roots=[str(allowed)]) == (
        False,
        "missing_file",
        None,
    )

    # 2. Outside allowed roots / empty roots / root path itself / non-regular directory
    assert cw.enroll_file(ledger, seat="s1", dialect="claude", file_path=str(secret_file), allowed_roots=[]) == (
        False,
        "outside_allowed_roots",
        None,
    )
    assert cw.enroll_file(ledger, seat="s1", dialect="claude", file_path=str(secret_file), allowed_roots=[str(allowed)]) == (
        False,
        "outside_allowed_roots",
        None,
    )
    assert cw.enroll_file(ledger, seat="s1", dialect="claude", file_path=str(allowed), allowed_roots=[str(allowed)]) == (
        False,
        "outside_allowed_roots",
        None,
    )
    subdir = allowed / "adir"
    subdir.mkdir()
    assert cw.enroll_file(ledger, seat="s1", dialect="claude", file_path=str(subdir), allowed_roots=[str(allowed)]) == (
        False,
        "not_regular_file",
        None,
    )
    assert cw.enroll_file(
        ledger, seat="s1", dialect="claude", file_path=str(allowed / "missing.jsonl"), allowed_roots=[str(allowed)]
    ) == (False, "missing_file", None)

    # 3. Real OS directory junction (Windows) or symlink (POSIX) inside allowed root -> refused
    link_dir = allowed / "linked_escape"
    created_real_link = False
    if os.name == "nt":
        proc = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link_dir), str(outside)],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )
        created_real_link = proc.returncode == 0
    else:
        os.symlink(outside, link_dir)
        created_real_link = True
    assert created_real_link is True
    try:
        escaped_target = link_dir / "secret.jsonl"
        ok_esc, reason_esc, _ = cw.enroll_file(
            ledger, seat="s1", dialect="claude", file_path=str(escaped_target), allowed_roots=[str(allowed)]
        )
        assert ok_esc is False and reason_esc == "symlink_or_reparse"
    finally:
        if os.name == "nt":
            os.rmdir(link_dir)
        else:
            os.unlink(link_dir)

    # 4. Deterministic S_IFLNK and FILE_ATTRIBUTE_REPARSE_POINT (0x400) checks on the leaf file and root
    good_file = allowed / "sess.jsonl"
    good_file.write_text('{"ok":1}\n', encoding="utf-8")
    real_lstat = cw.os.lstat

    class _FakeLeafStat:
        def __init__(self, base, *, mode=None, attrs=0):
            self.st_mode = base.st_mode if mode is None else mode
            self.st_dev = base.st_dev
            self.st_ino = base.st_ino
            self.st_size = base.st_size
            self.st_file_attributes = attrs

    base_st = real_lstat(good_file)
    monkeypatch.setattr(
        cw.os,
        "lstat",
        lambda p: _FakeLeafStat(base_st, mode=stat.S_IFLNK | 0o777)
        if os.path.abspath(p) == os.path.abspath(str(good_file))
        else real_lstat(p),
    )
    assert cw.enroll_file(ledger, seat="s1", dialect="claude", file_path=str(good_file), allowed_roots=[str(allowed)]) == (
        False,
        "symlink_or_reparse",
        None,
    )

    monkeypatch.setattr(
        cw.os,
        "lstat",
        lambda p: _FakeLeafStat(base_st, attrs=cw.REPARSE_POINT_ATTR)
        if os.path.abspath(p) == os.path.abspath(str(good_file))
        else real_lstat(p),
    )
    assert cw.enroll_file(ledger, seat="s1", dialect="claude", file_path=str(good_file), allowed_roots=[str(allowed)]) == (
        False,
        "symlink_or_reparse",
        None,
    )

    # Allowed root itself is a symlink/reparse point -> refused
    root_st = real_lstat(allowed)
    monkeypatch.setattr(
        cw.os,
        "lstat",
        lambda p: _FakeLeafStat(root_st, attrs=cw.REPARSE_POINT_ATTR)
        if os.path.abspath(p) == os.path.abspath(str(allowed))
        else real_lstat(p),
    )
    assert cw.enroll_file(ledger, seat="s1", dialect="claude", file_path=str(good_file), allowed_roots=[str(allowed)]) == (
        False,
        "symlink_or_reparse",
        None,
    )


def test_enroll_and_verify_refuse_duplicate_inode_across_seats_and_changed_file_identity(tmp_path, monkeypatch):
    root_a = tmp_path / "root_a"
    root_b = tmp_path / "root_b"
    root_a.mkdir()
    root_b.mkdir()
    file_a = root_a / "rollout.jsonl"
    file_a.write_text('{"a":1}\n', encoding="utf-8")
    file_b = root_b / "hardlink.jsonl"
    os.link(file_a, file_b)  # same (dev, ino) across two seats' directories

    box_inodes: dict = {}
    ledger_a = str(tmp_path / "store_a" / "enrollment.json")
    ledger_b = str(tmp_path / "store_b" / "enrollment.json")

    ok_a, reason_a, entry_a = cw.enroll_file(
        ledger_a,
        seat="seat-a",
        dialect="codex",
        file_path=str(file_a),
        allowed_roots=[str(root_a)],
        box_enrolled_inodes=box_inodes,
    )
    assert ok_a is True and reason_a == "enrolled" and entry_a is not None

    # Second seat attempting to enroll the same (dev, ino) is refused!
    ok_b, reason_b, _ = cw.enroll_file(
        ledger_b,
        seat="seat-b",
        dialect="codex",
        file_path=str(file_b),
        allowed_roots=[str(root_b)],
        box_enrolled_inodes=box_inodes,
    )
    assert ok_b is False and reason_b == "duplicate_inode_across_seats"

    # Unenrolled file in root_a is NEVER returned by verified_enrolled_sources
    unenrolled = root_a / "never_enrolled.jsonl"
    unenrolled.write_text('{"secret":"never_read"}\n', encoding="utf-8")
    srcs, err = cw.verified_enrolled_sources(
        ledger_a, seat="seat-a", dialect="codex", allowed_roots=[str(root_a)], box_enrolled_inodes={}
    )
    assert err is None and srcs == [os.path.abspath(str(file_a))]

    # Replacing file_a with a new inode triggers file_identity_changed on both enroll_file and verified_enrolled_sources
    real_lstat = cw.os.lstat
    orig_st = real_lstat(file_a)

    class _ChangedInoStat:
        st_mode = orig_st.st_mode
        st_dev = orig_st.st_dev
        st_ino = orig_st.st_ino + 999999
        st_size = orig_st.st_size
        st_file_attributes = getattr(orig_st, "st_file_attributes", 0)

    monkeypatch.setattr(
        cw.os,
        "lstat",
        lambda p: _ChangedInoStat() if os.path.abspath(p) == os.path.abspath(str(file_a)) else real_lstat(p),
    )
    ok_ch, reason_ch, _ = cw.enroll_file(
        ledger_a, seat="seat-a", dialect="codex", file_path=str(file_a), allowed_roots=[str(root_a)]
    )
    assert ok_ch is False and reason_ch == "file_identity_changed"
    srcs_ch, err_ch = cw.verified_enrolled_sources(
        ledger_a, seat="seat-a", dialect="codex", allowed_roots=[str(root_a)]
    )
    assert srcs_ch == [] and err_ch == "file_identity_changed"


# ---- macOS (`darwin`) interpreter-match guard (#355) ------------------------------------------------------------

def test_darwin_interpreter_match_guard_and_override(tmp_path):
    py1 = tmp_path / "py1"
    py2 = tmp_path / "py2"
    py1.write_text("", encoding="utf-8")
    py2.write_text("", encoding="utf-8")

    ident_mismatch = {"name": "pxx", "python": str(py2)}
    # Non-darwin platforms allow distinct python without flag
    assert cw.check_interpreter_match(ident_mismatch, postbox_python=str(py1), platform="win32") == (True, None)
    assert cw.check_interpreter_match(ident_mismatch, postbox_python=str(py1), platform="linux") == (True, None)

    # darwin refuses distinct python by default
    assert cw.check_interpreter_match(ident_mismatch, postbox_python=str(py1), platform="darwin") == (
        False,
        "darwin_distinct_python",
    )
    # darwin refuses missing/blank python
    assert cw.check_interpreter_match({"name": "pxx", "python": "  "}, postbox_python=str(py1), platform="darwin") == (
        False,
        "missing_python",
    )
    # darwin allows matching python
    assert cw.check_interpreter_match({"name": "pxx", "python": str(py1)}, postbox_python=str(py1), platform="darwin") == (
        True,
        None,
    )
    # darwin allows explicit override `allow_distinct_context_python: True` (must be strict bool True)
    assert cw.check_interpreter_match(
        {"name": "pxx", "python": str(py2), "allow_distinct_context_python": True},
        postbox_python=str(py1),
        platform="darwin",
    ) == (True, None)
    assert cw.check_interpreter_match(
        {"name": "pxx", "python": str(py2), "allow_distinct_context_python": "yes"},
        postbox_python=str(py1),
        platform="darwin",
    ) == (False, "darwin_distinct_python")


# ---- Vienna backfill rate budget & /v1/livez controller ---------------------------------------------------------

def test_vienna_rate_controller_day_night_single_slow_recovery_and_two_strike_latch():
    # Note: 2026-10-26 is CET (UTC+1). 01:00 UTC == (time withheld) (night); 06:00 UTC == (time withheld) (daytime).
    current_utc = [datetime.datetime(2026, 10, 26, 8, 0, 0, tzinfo=datetime.timezone.utc)]  # (time withheld) (day)
    mono = [1000.0]
    probe_results: list[bool] = []
    probe_calls: list[str] = []

    def fake_probe(url: str) -> bool:
        probe_calls.append(url)
        return probe_results.pop(0) if probe_results else True

    rc = cw.RateController(probe_fn=fake_probe, now_fn=lambda: current_utc[0], mono_fn=lambda: mono[0])

    # 1. Daytime ((time withheld)): 2.0 writes/s, max_batches=1 (<=66 calls < 120 bucket burst), no /v1/livez probe
    assert rc.budget("http://127.0.0.1:6310") == (2.0, 1)
    assert probe_calls == []

    # 2. Boundary (time withheld) (06:00 UTC): still daytime (07:00..24:00) -> 2.0 writes/s
    current_utc[0] = datetime.datetime(2026, 10, 26, 6, 0, 0, tzinfo=datetime.timezone.utc)
    assert rc.budget("http://127.0.0.1:6310") == (2.0, 1)
    assert probe_calls == []

    # 3. Night window (time withheld) (01:00 UTC): probes /v1/livez -> fast (True) -> 8.0 writes/s, max_batches=1
    current_utc[0] = datetime.datetime(2026, 10, 26, 1, 0, 0, tzinfo=datetime.timezone.utc)
    probe_results.append(True)
    assert rc.budget("http://127.0.0.1:6310") == (8.0, 1)
    assert len(probe_calls) == 1

    # Within 60s (e.g. +30s), cached health is reused without re-probing
    mono[0] += 30.0
    assert rc.budget("http://127.0.0.1:6310") == (8.0, 1)
    assert len(probe_calls) == 1

    # 4. At +60s: ONE slow/failed /v1/livez sample -> does NOT latch yet (stays 8.0 writes/s)
    mono[0] += 30.0
    probe_results.append(False)
    assert rc.budget("http://127.0.0.1:6310") == (8.0, 1)
    assert rc.consecutive_slow == 1 and rc.latched_night is None

    # Followed at +60s by a fast sample -> resets consecutive_slow to 0!
    mono[0] += 60.0
    probe_results.append(True)
    assert rc.budget("http://127.0.0.1:6310") == (8.0, 1)
    assert rc.consecutive_slow == 0 and rc.latched_night is None

    # Another single slow sample after recovery STILL does not latch (proves streak was reset)
    mono[0] += 60.0
    probe_results.append(False)
    assert rc.budget("http://127.0.0.1:6310") == (8.0, 1)
    assert rc.consecutive_slow == 1 and rc.latched_night is None

    # 5. SECOND consecutive slow/failed sample -> latches to 2.0 writes/s for remainder of 2026-10-26 night!
    mono[0] += 60.0
    probe_results.append(False)
    assert rc.budget("http://127.0.0.1:6310") == (2.0, 1)
    assert rc.latched_night == "2026-10-26"

    # Later the same night ((time withheld) == 04:30 UTC, +3 hours): stays latched at 2.0 writes/s without probing
    probes_before = len(probe_calls)
    current_utc[0] = datetime.datetime(2026, 10, 26, 4, 30, 0, tzinfo=datetime.timezone.utc)
    mono[0] += 10800.0
    assert rc.budget("http://127.0.0.1:6310") == (2.0, 1)
    assert len(probe_calls) == probes_before

    # 6. Next Vienna night (2026-10-27 (time withheld) == 00:30 UTC): latch resets, 8.0 writes/s resumes!
    current_utc[0] = datetime.datetime(2026, 10, 27, 0, 30, 0, tzinfo=datetime.timezone.utc)
    mono[0] += 72000.0
    probe_results.append(True)
    assert rc.budget("http://127.0.0.1:6310") == (8.0, 1)
    assert len(probe_calls) == probes_before + 1


def test_probe_livez_checks_elapsed_status_and_exceptions():
    class _Resp:
        def __init__(self, status: int):
            self.status = status

        def read(self, _n: int = -1) -> bytes:
            return b"ok"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    clock = [0.0]

    def fast_open(url, timeout=2.0):
        assert url == "http://127.0.0.1:6310/v1/livez" and timeout == 2.0
        clock[0] += 0.2
        return _Resp(200)

    assert cw.probe_livez("http://127.0.0.1:6310/", opener=fast_open, mono_fn=lambda: clock[0]) is True

    # Slow > 2.0s -> False
    def slow_open(_url, timeout=2.0):
        clock[0] += 2.05
        return _Resp(200)

    assert cw.probe_livez("http://127.0.0.1:6310", opener=slow_open, mono_fn=lambda: clock[0]) is False

    # HTTP 500 -> False
    assert cw.probe_livez("http://127.0.0.1:6310", opener=lambda u, timeout=2.0: _Resp(503), mono_fn=lambda: 1.0) is False
    assert cw.probe_livez("http://127.0.0.1:6310", opener=lambda u, timeout=2.0: _Resp(199), mono_fn=lambda: 1.0) is False

    # Network error -> False
    def err_open(_url, timeout=2.0):
        raise urllib.error.URLError("connection refused")

    assert cw.probe_livez("http://127.0.0.1:6310", opener=err_open, mono_fn=lambda: 1.0) is False


def test_writes_in_payload_and_pace_sleep_math():
    assert cw.writes_in_payload(None) == 0
    assert cw.writes_in_payload({"status": "drained", "batches_committed": 0, "items_committed": 0}) == 0
    assert cw.writes_in_payload({"status": "progressed", "batches_committed": 0, "items_committed": 0}) == 1
    assert (
        cw.writes_in_payload(
            {
                "status": "progressed",
                "batches_committed": 1,
                "items_committed": 3,
                "accounting": {"failed": {"E_CONTEXT_SCHEMA": 2}},
            }
        )
        == 6
    )
    # At 2 writes/s, 4 writes require 2.0s total; if pass took 0.6s, sleep 1.4s
    assert abs(cw.pace_sleep_s(4, 0.6, 2.0) - 1.4) < 1e-9
    # At 8 writes/s, 8 writes require 1.0s total; if pass took 1.2s, sleep 0.0s
    assert cw.pace_sleep_s(8, 1.2, 8.0) == 0.0
    assert cw.pace_sleep_s(0, 0.1, 2.0) == 0.0
    assert cw.pace_sleep_s(4, -1.0, 2.0) == 2.0
    assert cw.pace_sleep_s(4, 0.1, 0.0) == 0.0


def test_conflict_codes_keep_only_fixed_codes_never_messages_paths_or_text():
    """pxx 20988f: an exit 4 must name its cause in status.json, but only the uploader's fixed codes go there."""
    assert cw.sanitize_conflict_codes(None) is None and cw.sanitize_conflict_codes([]) is None
    raw = [
        {"src": None, "code": "E_CONTEXT_SCREEN_UNAVAILABLE: local context preflight refused the batch"},
        {"src": "a" * 32, "code": "E_CONTEXT_SCHEMA: item 3 text 'the user said my password is hunter2'"},
        {"src": None, "code": "store: enrollment mismatch requires an explicit reviewed generation"},
        {"src": None, "code": "source unreadable"},
        {"src": None, "code": "C:\\Users\\user\\.claude\\projects\\x.jsonl changed"},
        {"src": None, "code": "Some Free Text With Capitals"},
        {"src": None, "code": "source unreadable"},
        "not a dict",
        {"code": 5},
    ]
    out = cw.sanitize_conflict_codes(raw)
    assert out == ["E_CONTEXT_SCREEN_UNAVAILABLE", "E_CONTEXT_SCHEMA",
                   "store: enrollment mismatch requires an explicit reviewed generation", "source unreadable", "other"]
    flat = " ".join(out)
    assert "hunter2" not in flat and "Users" not in flat and "a" * 32 not in flat
    assert len(cw.sanitize_conflict_codes([{"code": "E_X%02d" % i} for i in range(9)])) == 5
    # dedupe on its own (pxx #474 INFO: a mutant without it passed the case above, the cap hid it)
    assert cw.sanitize_conflict_codes([{"code": "source missing"}] * 3) == ["source missing"]
    # fullmatch: a trailing newline, a relative path or a dotted name is not a fixed phrase
    assert cw.sanitize_conflict_codes([{"code": "source missing\n"}, {"code": "tools/x.py"},
                                       {"code": "a.b"}]) == ["other"]


def test_conflict_codes_do_not_go_stale_after_a_worker_restart(tmp_path):
    """pxx on #474 P2-1: guarded_context_worker restarts the loop with a fresh quarantined set while postbox's status
    survives, so a later ok pass must clear the old codes, not leave them beside context_conflict false."""
    statuses: dict[str, dict] = {}

    def rec_status(name, **kw):
        statuses.setdefault(name, {}).update(kw)

    ident = _make_ident(tmp_path, "kxxx", "claude")
    pin = Path(ident["context"]["allowed_roots"][0]) / "sess1.jsonl"
    pin.write_text('{"type":"user"}\n', encoding="utf-8")
    ident["context"]["pins"] = [str(pin)]
    ident["context"]["namespace"] = "shared/transcript-context"
    responses = [(4, json.dumps({"status": "conflict", "conflicts": [{"src": None, "code": "readback_differs"}]}), ""),
                 (0, json.dumps({"status": "drained", "seat": "kxxx", "passes": 1, "batches_committed": 0,
                                 "items_committed": 0, "last_manifest_key": None, "context_held": [],
                                 "accounting": {}, "conflicts": []}), ""),
                 (3, json.dumps({"status": "held", "context_held": [], "conflicts": []}), "")]

    def fake_runner(argv, _cwd, _timeout):
        return responses.pop(0)

    rc_ctrl = cw.RateController(
        probe_fn=lambda _u: True,
        now_fn=lambda: datetime.datetime(2026, 10, 26, 12, 0, 0, tzinfo=datetime.timezone.utc),
        mono_fn=lambda: 10.0,
    )

    def one_pass():
        # a fresh quarantined set each time stands for the worker loop restarted by guarded_context_worker
        return cw.run_seat_pass(ident, store_dir=str(tmp_path / "ctx_store" / "kxxx"), rate_controller=rc_ctrl,
                                box_enrolled_inodes={}, quarantined_seats=set(), missing_driver_logged=set(),
                                set_status=rec_status, note=lambda _m: None, runner=fake_runner)

    assert one_pass()["outcome"] == "conflict"
    assert statuses["kxxx"]["context_conflict_codes"] == ["readback_differs"]
    assert one_pass()["outcome"] == "ok"
    assert statuses["kxxx"]["context_conflict"] is False and statuses["kxxx"]["context_conflict_codes"] is None
    statuses["kxxx"]["context_conflict_codes"] = ["stale"]
    assert one_pass()["outcome"] == "held"
    assert statuses["kxxx"]["context_conflict_codes"] is None


# ---- context_held >= 3600s sanitization -------------------------------------------------------------------------

def test_sanitize_context_held_filters_under_1h_and_strips_content_and_paths():
    assert cw.sanitize_context_held(None) is None
    assert cw.sanitize_context_held("bad") is None
    assert cw.sanitize_context_held([{"src": "a" * 32, "age_s": 3599.9, "code": "E_CONTEXT_SCHEMA"}]) is None
    assert cw.sanitize_context_held([{"src": "a" * 32, "age_s": True, "code": "E_CONTEXT_SCHEMA"}]) is None

    dirty = [
        "not-a-dict",
        {"src": "a" * 32, "gen": 1, "batch": 2, "first_ord": 0, "last_ord": 9, "code": "E_CONTEXT_SCHEMA", "age_s": 120},
        {
            "src": "b" * 32,
            "gen": 1,
            "batch": 3,
            "first_ord": 10,
            "last_ord": 25,
            "code": "E_CONTEXT_SCHEMA",
            "age_s": 3600,
            "text": "TOP-SECRET-OPERATOR-PROMPT",
            "path": "/Users/pxx/.claude/projects/secret-path.jsonl",
            "prompt": "MUST-NEVER-LEAK",
        },
        {
            "src": "/leaked/file/path/in/src",
            "gen": True,
            "batch": 4,
            "first_ord": 26,
            "last_ord": 30,
            "code": "raw secret string with spaces / slashes",
            "age_s": 3700.1234,
        },
    ]
    out = cw.sanitize_context_held(dirty)
    assert out == [
        {
            "src": "b" * 32,
            "gen": 1,
            "batch": 3,
            "first_ord": 10,
            "last_ord": 25,
            "code": "E_CONTEXT_SCHEMA",
            "age_s": 3600,
        },
        {
            "src": None,
            "gen": None,
            "batch": 4,
            "first_ord": 26,
            "last_ord": 30,
            "code": "UNKNOWN_CODE",
            "age_s": 3700.123,
        },
    ]
    serialized = json.dumps(out)
    assert "TOP-SECRET" not in serialized
    assert "secret-path" not in serialized
    assert "MUST-NEVER-LEAK" not in serialized
    assert "/leaked/" not in serialized


# ---- End-to-end worker round-robin, exit codes, missing driver, timeout, and fault isolation --------------------

def _make_ident(tmp_path: Path, name: str, dialect: str = "claude", *, pins=(), store_dir=None) -> dict:
    root = tmp_path / ("root_" + name)
    root.mkdir(exist_ok=True)
    state = tmp_path / (name + "-state.json")
    state.write_text("{}", encoding="utf-8")
    client = tmp_path / "client"
    client.mkdir(exist_ok=True)
    ctx: dict = {
        "enabled": True,
        "seat": name,
        "dialect": dialect,
        "app_root": str(ROOT),
        "allowed_roots": [str(root)],
        "pins": list(pins),
    }
    if store_dir is not None:
        ctx["store_dir"] = str(store_dir)
    return {
        "name": name,
        "cli": dialect,
        "enabled": True,
        "python": sys.executable,
        "client": str(client),
        "state": str(state),
        "base_url": "http://127.0.0.1:6310",
        "context": ctx,
    }


def test_seat_pass_exit_codes_held_clear_conflict_quarantine_and_missing_driver(tmp_path):
    statuses: dict[str, dict] = {}
    notes: list[str] = []

    def rec_status(name, **kw):
        statuses.setdefault(name, {}).update(kw)

    ident = _make_ident(tmp_path, "kxxx", "claude")
    pin = Path(ident["context"]["allowed_roots"][0]) / "sess1.jsonl"
    pin.write_text('{"type":"user"}\n', encoding="utf-8")
    ident["context"]["pins"] = [str(pin)]
    ident["context"]["namespace"] = "shared/transcript-context"

    store_dir = str(tmp_path / "ctx_store" / "kxxx")
    rc_ctrl = cw.RateController(
        probe_fn=lambda _u: True,
        now_fn=lambda: datetime.datetime(2026, 10, 26, 12, 0, 0, tzinfo=datetime.timezone.utc),  # (time withheld) (2/s)
        mono_fn=lambda: 10.0,
    )
    box_inodes: dict = {}
    quarantined: set = set()
    missing_logged: set = set()
    seen_argvs: list[list[str]] = []
    responses: list[tuple[int | None, str, str]] = []

    def fake_runner(argv, _cwd, _timeout):
        seen_argvs.append(argv)
        return responses.pop(0)

    # 1. Missing driver (`No module named ... context_upload`) -> status driver_missing, logged ONCE
    responses.append((1, "", "python: No module named 'tools.transcript_cards.context_upload'\n"))
    responses.append((1, "", "python: No module named 'tools.transcript_cards.context_upload'\n"))
    r1 = cw.run_seat_pass(
        ident,
        store_dir=store_dir,
        rate_controller=rc_ctrl,
        box_enrolled_inodes=box_inodes,
        quarantined_seats=quarantined,
        missing_driver_logged=missing_logged,
        set_status=rec_status,
        note=notes.append,
        runner=fake_runner,
    )
    r2 = cw.run_seat_pass(
        ident,
        store_dir=store_dir,
        rate_controller=rc_ctrl,
        box_enrolled_inodes=box_inodes,
        quarantined_seats=quarantined,
        missing_driver_logged=missing_logged,
        set_status=rec_status,
        note=notes.append,
        runner=fake_runner,
    )
    assert r1["outcome"] == "driver_missing" and r2["outcome"] == "driver_missing"
    assert statuses["kxxx"]["context_worker"] == "driver_missing"
    assert sum(1 for n in notes if "not installed yet" in n) == 1

    # Verify exact CLI contract (no --rate-limit flag; --source, --namespace, --max-batches 1, --json)
    argv0 = seen_argvs[0]
    assert "--rate-limit" not in argv0
    assert argv0[:3] == [sys.executable, "-m", "tools.transcript_cards.context_upload"]
    assert "--seat" in argv0 and argv0[argv0.index("--seat") + 1] == "kxxx"
    assert "--dialect" in argv0 and argv0[argv0.index("--dialect") + 1] == "claude"
    assert "--source" in argv0 and argv0[argv0.index("--source") + 1] == os.path.abspath(str(pin))
    assert "--namespace" in argv0 and argv0[argv0.index("--namespace") + 1] == "shared/transcript-context"
    assert "--max-batches" in argv0 and argv0[argv0.index("--max-batches") + 1] == "1"
    assert argv0[-1] == "--json"

    # 2. Exit 3 (held): < 3600s suppressed -> context_held=None; >= 3600s sanitized -> published
    responses.append(
        (
            3,
            json.dumps(
                {
                    "status": "held",
                    "seat": "kxxx",
                    "passes": 1,
                    "batches_committed": 0,
                    "items_committed": 0,
                    "last_manifest_key": None,
                    "context_held": [
                        {"src": "c" * 32, "gen": 1, "batch": 1, "first_ord": 0, "last_ord": 4, "code": "E_CONTEXT_SCHEMA", "age_s": 900}
                    ],
                    "accounting": {"emitted": 5, "excluded": {}, "pending": 5, "failed": {"E_CONTEXT_SCHEMA": 1}},
                    "conflicts": [],
                }
            ),
            "",
        )
    )
    rh_young = cw.run_seat_pass(
        ident,
        store_dir=store_dir,
        rate_controller=rc_ctrl,
        box_enrolled_inodes=box_inodes,
        quarantined_seats=quarantined,
        missing_driver_logged=missing_logged,
        set_status=rec_status,
        note=notes.append,
        runner=fake_runner,
    )
    assert rh_young["outcome"] == "held" and statuses["kxxx"]["context_held"] is None

    responses.append(
        (
            3,
            json.dumps(
                {
                    "status": "held",
                    "seat": "kxxx",
                    "passes": 1,
                    "batches_committed": 0,
                    "items_committed": 0,
                    "last_manifest_key": None,
                    "context_held": [
                        {
                            "src": "c" * 32,
                            "gen": 1,
                            "batch": 1,
                            "first_ord": 0,
                            "last_ord": 4,
                            "code": "E_CONTEXT_SCHEMA",
                            "age_s": 3605,
                            "secret_body": "DO-NOT-COPY",
                        }
                    ],
                    "accounting": {"emitted": 0, "excluded": {}, "pending": 5, "failed": {"E_CONTEXT_SCHEMA": 1}},
                    "conflicts": [],
                }
            ),
            "",
        )
    )
    rh_old = cw.run_seat_pass(
        ident,
        store_dir=store_dir,
        rate_controller=rc_ctrl,
        box_enrolled_inodes=box_inodes,
        quarantined_seats=quarantined,
        missing_driver_logged=missing_logged,
        set_status=rec_status,
        note=notes.append,
        runner=fake_runner,
    )
    assert rh_old["outcome"] == "held"
    assert statuses["kxxx"]["context_held"] == [
        {"src": "c" * 32, "gen": 1, "batch": 1, "first_ord": 0, "last_ord": 4, "code": "E_CONTEXT_SCHEMA", "age_s": 3605}
    ]
    assert "DO-NOT-COPY" not in json.dumps(statuses)

    # 3. Exit 0 (mid-batch items_committed > 0 even if status="drained"): clears context_held & sets more_work=True!
    mono_vals = [100.0, 100.2]
    responses.append(
        (
            0,
            json.dumps(
                {
                    "status": "drained",
                    "seat": "kxxx",
                    "passes": 1,
                    "batches_committed": 0,
                    "items_committed": 4,
                    "last_manifest_key": "ctxm/kxxx/" + ("c" * 32) + "/0001/000001",
                    "context_held": [],
                    "accounting": {"emitted": 0, "excluded": {}, "pending": 0, "failed": {}},
                    "conflicts": [],
                }
            ),
            "",
        )
    )
    r_ok = cw.run_seat_pass(
        ident,
        store_dir=store_dir,
        rate_controller=rc_ctrl,
        box_enrolled_inodes=box_inodes,
        quarantined_seats=quarantined,
        missing_driver_logged=missing_logged,
        set_status=rec_status,
        note=notes.append,
        runner=fake_runner,
        mono_fn=lambda: mono_vals.pop(0),
    )
    assert r_ok["outcome"] == "ok" and r_ok["more_work"] is True
    assert abs(r_ok["sleep_s"] - 1.8) < 1e-6  # 4 writes / 2.0 writes/s - 0.2s elapsed == 1.8s
    assert statuses["kxxx"]["context_held"] is None
    assert statuses["kxxx"]["context_worker"] == "ok"

    # 4. Exit 5 (unreachable) and Exit 2 (config_error) -> non-quarantining
    responses.append((5, json.dumps({"status": "unreachable", "context_held": []}), ""))
    assert (
        cw.run_seat_pass(
            ident,
            store_dir=store_dir,
            rate_controller=rc_ctrl,
            box_enrolled_inodes=box_inodes,
            quarantined_seats=quarantined,
            missing_driver_logged=missing_logged,
            set_status=rec_status,
            note=notes.append,
            runner=fake_runner,
        )["outcome"]
        == "unreachable"
    )
    assert statuses["kxxx"]["context_worker"] == "unreachable"

    responses.append((2, json.dumps({"status": "usage", "error": "bad args"}), ""))
    assert (
        cw.run_seat_pass(
            ident,
            store_dir=store_dir,
            rate_controller=rc_ctrl,
            box_enrolled_inodes=box_inodes,
            quarantined_seats=quarantined,
            missing_driver_logged=missing_logged,
            set_status=rec_status,
            note=notes.append,
            runner=fake_runner,
        )["outcome"]
        == "config_error"
    )
    assert statuses["kxxx"]["context_config_error"] is True

    # 5. Exit 4 (conflict): marks context_conflict=True and quarantines seat from further automatic retries
    responses.append((4, json.dumps({"status": "conflict", "conflicts": [{"src": None, "code": "readback_differs"}]}), ""))
    r_conf = cw.run_seat_pass(
        ident,
        store_dir=store_dir,
        rate_controller=rc_ctrl,
        box_enrolled_inodes=box_inodes,
        quarantined_seats=quarantined,
        missing_driver_logged=missing_logged,
        set_status=rec_status,
        note=notes.append,
        runner=fake_runner,
    )
    assert r_conf["outcome"] == "conflict"
    assert statuses["kxxx"]["context_conflict"] is True
    assert statuses["kxxx"]["context_conflict_codes"] == ["readback_differs"]
    assert any("exited 4 (conflict: readback_differs)" in n for n in notes)
    assert "kxxx" in quarantined

    # Subsequent pass on quarantined seat does NOT spawn subprocess
    calls_before = len(seen_argvs)
    r_q = cw.run_seat_pass(
        ident,
        store_dir=store_dir,
        rate_controller=rc_ctrl,
        box_enrolled_inodes=box_inodes,
        quarantined_seats=quarantined,
        missing_driver_logged=missing_logged,
        set_status=rec_status,
        note=notes.append,
        runner=fake_runner,
    )
    assert r_q["outcome"] == "quarantined" and len(seen_argvs) == calls_before


def test_f3_deferred_sources_are_visible_in_status_and_carry_no_paths_or_text(tmp_path):
    """txxxx on #471: a seat that reports F3 on must see which of its sources still upload text-only."""
    assert cw.sanitize_f3_deferred(None) is None and cw.sanitize_f3_deferred([]) is None
    assert cw.sanitize_f3_deferred(["C:/Users/x/.claude/a.jsonl", "secret text", 7,
                                    {"src": "/etc/passwd", "gen": 1}, {"src": "a" * 32, "gen": True}]) is None
    assert cw.sanitize_f3_deferred([{"src": "a" * 32, "gen": 1}, {"src": "b" * 32, "gen": 2},
                                    {"src": None, "gen": 1}, {"src": "a" * 12, "gen": 1}]) == {
        "count": 3, "sources": [{"src": None, "gen": 1}, {"src": "a" * 32, "gen": 1}, {"src": "b" * 32, "gen": 2}]}
    statuses: dict[str, dict] = {}

    def rec_status(name, **kw):
        statuses.setdefault(name, {}).update(kw)

    ident = _make_ident(tmp_path, "kxxx", "claude")
    pin = Path(ident["context"]["allowed_roots"][0]) / "sess1.jsonl"
    pin.write_text('{"type":"user"}\n', encoding="utf-8")
    ident["context"]["pins"] = [str(pin)]
    ident["context"]["namespace"] = "shared/transcript-context"
    ident["context"]["f3"] = True
    responses = [(0, json.dumps({"status": "drained", "seat": "kxxx", "passes": 1, "batches_committed": 0,
                                 "items_committed": 0, "last_manifest_key": None, "context_held": [],
                                 "accounting": {}, "conflicts": [], "reason": None,
                                 "f3_deferred": [{"src": "0123456789abcdef" * 2, "gen": 1}]}), ""),
                 (0, json.dumps({"status": "drained", "seat": "kxxx", "passes": 1, "batches_committed": 0,
                                 "items_committed": 0, "last_manifest_key": None, "context_held": [],
                                 "accounting": {}, "conflicts": [], "reason": None, "f3_deferred": []}), "")]

    def fake_runner(argv, _cwd, _timeout):
        return responses.pop(0)

    rc_ctrl = cw.RateController(
        probe_fn=lambda _u: True,
        now_fn=lambda: datetime.datetime(2026, 10, 26, 12, 0, 0, tzinfo=datetime.timezone.utc),
        mono_fn=lambda: 10.0,
    )
    kw = dict(store_dir=str(tmp_path / "ctx_store" / "kxxx"), rate_controller=rc_ctrl, box_enrolled_inodes={},
              quarantined_seats=set(), missing_driver_logged=set(), set_status=rec_status, note=lambda _m: None,
              runner=fake_runner)
    assert cw.run_seat_pass(ident, **kw)["outcome"] == "ok"
    assert statuses["kxxx"]["context_f3_deferred"] == {"count": 1,
                                                       "sources": [{"src": "0123456789abcdef" * 2, "gen": 1}]}
    assert cw.run_seat_pass(ident, **kw)["outcome"] == "ok"
    assert statuses["kxxx"]["context_f3_deferred"] is None, "a pass with nothing deferred clears the field"


def test_real_subprocess_timeout_and_end_to_end_worker_fault_isolation(tmp_path):
    # 1. Real subprocess timeout kills hung child cleanly and returns (None, ..., ...)
    rc_hang, _, _ = cw._run_subprocess([sys.executable, "-c", "import time; time.sleep(30)"], None, 0.2)
    assert rc_hang is None

    # 2. Round-robin worker isolates shared store_dir clashes, crashing seats, and timeouts from healthy seats
    shared_dir = tmp_path / "shared_store"
    s_clash1 = _make_ident(tmp_path, "clash1", "claude", store_dir=shared_dir)
    s_clash2 = _make_ident(tmp_path, "clash2", "claude", store_dir=shared_dir)

    s_boom = _make_ident(tmp_path, "boom", "codex")
    p_boom = Path(s_boom["context"]["allowed_roots"][0]) / "b.jsonl"
    p_boom.write_text("{}\n", encoding="utf-8")
    s_boom["context"]["pins"] = [str(p_boom)]

    s_good = _make_ident(tmp_path, "good", "antigravity")
    p_good = Path(s_good["context"]["allowed_roots"][0]) / "transcript.jsonl"
    p_good_full = Path(s_good["context"]["allowed_roots"][0]) / "transcript_full.jsonl"
    p_good.write_text("{}\n", encoding="utf-8")
    p_good_full.write_text('{"step_index":0}\n', encoding="utf-8")
    s_good["context"]["pins"] = [str(p_good)]

    s_disabled = _make_ident(tmp_path, "disabled", "claude")
    s_disabled["context"]["enabled"] = False

    statuses: dict[str, dict] = {}
    notes: list[str] = []
    sleeps: list[float] = []

    def rec_status(name, **kw):
        statuses.setdefault(name, {}).update(kw)

    good_calls = 0

    def mixed_runner(argv, _cwd, _timeout):
        nonlocal good_calls
        seat_idx = argv.index("--seat") + 1
        seat = argv[seat_idx]
        if seat == "boom":
            raise RuntimeError("simulated runner explosion")
        good_calls += 1
        # For "good", verify transcript.jsonl was upgraded to transcript_full.jsonl!
        src_arg = argv[argv.index("--source") + 1]
        assert src_arg == os.path.abspath(str(p_good_full))
        # Round 1 commits 1 batch + 1 item (2 writes -> ~1.0s pace, more_work=True);
        # Round 2 reports 0 writes and drained (more_work=False -> triggers 5.0s idle sleep before Round 3).
        committed = 1 if good_calls == 1 else 0
        return (
            0,
            json.dumps(
                {
                    "status": "drained",
                    "seat": seat,
                    "passes": 1,
                    "batches_committed": committed,
                    "items_committed": committed,
                    "last_manifest_key": "ctxm/good/" + ("d" * 32) + "/0001/000001",
                    "context_held": [],
                    "accounting": {"emitted": committed, "excluded": {}, "pending": 0, "failed": {}},
                }
            ),
            "",
        )

    clock = [50.0]

    def fake_sleep(dt: float):
        sleeps.append(dt)
        clock[0] += dt

    rc_ctrl = cw.RateController(
        probe_fn=lambda _u: True,
        now_fn=lambda: datetime.datetime(2026, 10, 26, 12, 0, 0, tzinfo=datetime.timezone.utc),
        mono_fn=lambda: clock[0],
    )
    cw.run_context_worker(
        [s_clash1, s_clash2, s_boom, s_good, s_disabled],
        store_root=str(tmp_path / "stores"),
        set_status=rec_status,
        note=notes.append,
        rate_controller=rc_ctrl,
        runner=mixed_runner,
        mono_fn=lambda: clock[0],
        sleep_fn=fake_sleep,
        idle_sleep_s=5.0,
        max_rounds=4,
    )

    assert statuses["clash1"].get("context_error") == "shared_store_dir"
    assert statuses["clash2"].get("context_error") == "shared_store_dir"
    assert statuses["boom"]["context_worker"] == "crashed"
    assert statuses["good"]["context_worker"] == "ok"
    assert "disabled" not in statuses
    # Box pacing sleep (2 writes / 2.0 writes/s = ~1.0s), remaining per-seat cooldown (7.0s split 5.0s + 2.0s), and idle (5.0s)
    assert any(0.9 <= s <= 1.0 for s in sleeps) and 5.0 in sleeps and any(1.9 <= s <= 2.1 for s in sleeps)


def test_real_child_context_upload_module_invocation_and_corrupt_ledger_quarantine(tmp_path):
    # Create a real tools.transcript_cards.context_upload stub inside ident["client"]
    ident = _make_ident(tmp_path, "livechild", "claude")
    client_dir = Path(ident["client"])
    mod_dir = client_dir / "tools" / "transcript_cards"
    mod_dir.mkdir(parents=True, exist_ok=True)
    (client_dir / "tools" / "__init__.py").write_text("", encoding="utf-8")
    (mod_dir / "__init__.py").write_text("", encoding="utf-8")
    (mod_dir / "context_upload.py").write_text(
        "import json, sys\n"
        "argv = sys.argv[1:]\n"
        "assert '--json' in argv and '--source' in argv and '--rate-limit' not in argv\n"
        "seat = argv[argv.index('--seat') + 1]\n"
        "print(json.dumps({\n"
        "    'status': 'drained',\n"
        "    'seat': seat,\n"
        "    'passes': 1,\n"
        "    'batches_committed': 1,\n"
        "    'items_committed': 0,\n"
        "    'last_manifest_key': 'ctxm/' + seat + '/' + ('a' * 32) + '/0001/000001',\n"
        "    'context_held': [\n"
        "        {'src': 'b' * 32, 'gen': 1, 'batch': 2, 'first_ord': 0, 'last_ord': 4,\n"
        "         'code': 'E_TIMEOUT', 'age_s': 4000, 'raw_line': 'NEVER_LEAK'}\n"
        "    ],\n"
        "    'conflicts': [],\n"
        "    'accounting': {'emitted': 2, 'excluded': {}, 'pending': 0, 'failed': {}},\n"
        "}))\n",
        encoding="utf-8",
    )
    src_file = Path(ident["context"]["allowed_roots"][0]) / "session.jsonl"
    src_file.write_text('{"type":"user"}\n', encoding="utf-8")
    ident["context"]["pins"] = [str(src_file)]
    ident["python"] = sys.executable
    # A client-only install: app_root without the uploader, so the child starts in the client and runs its stub.
    ident["context"]["app_root"] = str(_app_tree(tmp_path / "app_only", uploader=False))

    statuses: dict[str, dict] = {}
    notes: list[str] = []
    quarantined: set[str] = set()
    rc_ctrl = cw.RateController(
        probe_fn=lambda _u: True,
        now_fn=lambda: datetime.datetime(2026, 10, 26, 12, 0, 0, tzinfo=datetime.timezone.utc),
        mono_fn=lambda: 10.0,
    )
    store_dir = str(tmp_path / "livechild_store")
    res = cw.run_seat_pass(
        ident,
        store_dir=store_dir,
        rate_controller=rc_ctrl,
        box_enrolled_inodes={},
        quarantined_seats=quarantined,
        missing_driver_logged=set(),
        set_status=lambda n, **kw: statuses.setdefault(n, {}).update(kw),
        note=notes.append,
    )
    assert res["outcome"] == "ok"
    assert statuses["livechild"]["context_worker"] == "ok"
    assert "NEVER_LEAK" not in json.dumps(statuses)
    assert statuses["livechild"]["context_held"][0]["code"] == "E_TIMEOUT"

    # Corrupting enrollment.json quarantines only that seat cleanly without crashing the worker thread
    ledger_file = Path(store_dir) / "enrollment.json"
    ledger_file.write_text("{not valid json", encoding="utf-8")
    res_corrupt = cw.run_seat_pass(
        ident,
        store_dir=store_dir,
        rate_controller=rc_ctrl,
        box_enrolled_inodes={},
        quarantined_seats=quarantined,
        missing_driver_logged=set(),
        set_status=lambda n, **kw: statuses.setdefault(n, {}).update(kw),
        note=notes.append,
    )
    assert res_corrupt["outcome"] == "conflict"
    assert "livechild" in quarantined
    assert statuses["livechild"]["context_error"] == "corrupt_enrollment_ledger"


def test_postbox_main_spawns_context_worker_only_when_an_active_identity_enables_context(tmp_path, monkeypatch):
    pb = _load_pb_module("postbox")
    assert pb.context_worker is not None

    # Default identities (no `context` key or `enabled: False`) -> build_context_worker_thread returns None
    plain = [
        {"name": "bxxxxx-prod", "enabled": True, "cli": "claude"},
        {"name": "kxxx", "enabled": True, "cli": "claude", "context": {"enabled": False}},
        {"name": "string-seat", "enabled": True, "cli": "claude", "context": {"enabled": "yes"}},
        {"name": "off-seat", "enabled": False, "cli": "claude", "context": {"enabled": True}},
    ]
    assert pb.context_worker.context_enabled_identities(plain) == []
    assert pb.build_context_worker_thread({}, plain) is None

    # With context_worker=None (fallback when module absent), returns None even when opted in
    opted_in = plain + [{"name": "antigravity-prod", "enabled": True, "cli": "antigravity", "context": {"enabled": True}}]
    monkeypatch.setattr(pb, "context_worker", None)
    assert pb.build_context_worker_thread({}, opted_in) is None
    monkeypatch.undo()

    # With active opted-in identity, builds a single daemon thread targeting guarded_context_worker
    custom_root = str(tmp_path / "ctx_custom")
    th = pb.build_context_worker_thread({"context_store_root": custom_root}, opted_in)
    assert th is not None and th.daemon is True
    assert th._target is pb.context_worker.guarded_context_worker
    assert [i["name"] for i in th._args[0]] == ["antigravity-prod"]
    assert th._kwargs["store_root"] == custom_root

    # Default store_root falls back to os.path.join(pb.HERE, "context")
    th_default = pb.build_context_worker_thread({}, opted_in)
    assert th_default is not None
    assert th_default._kwargs["store_root"] == os.path.join(pb.HERE, "context")


def test_guarded_context_worker_restarts_after_unexpected_outer_exception():
    calls = 0
    notes: list[str] = []
    sleeps: list[float] = []

    def flaky_enabled(_idents):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient outer fault")
        return []

    orig = cw.context_enabled_identities
    cw.context_enabled_identities = flaky_enabled
    try:
        cw.guarded_context_worker([], pause_s=12.5, sleep_fn=sleeps.append, note=notes.append, store_root="unused", set_status=lambda *a, **k: None)
    finally:
        cw.context_enabled_identities = orig
    assert calls == 2
    assert sleeps == [12.5]
    assert any("context_worker crashed, restarting in 12 s" in n for n in notes)


def test_resolve_seat_for_prod_identities_and_per_base_url_rate_isolation(tmp_path):
    # 1. Production `<seat>-prod` entries resolve caller seat from `ident['seat']` or `state['label']`
    root = tmp_path / "allowed"
    root.mkdir()
    src = root / "s.jsonl"
    src.write_text('{"x":1}\n', encoding="utf-8")
    st_file = tmp_path / "antigravity-prod-state.json"
    st_file.write_text(json.dumps({"label": "antigravity", "status": "minted"}), encoding="utf-8")

    seen_argv: list[list[str]] = []

    def fake_runner(argv, cwd, timeout_s):
        seen_argv.append(list(argv))
        return 0, json.dumps({"status": "drained", "batches_committed": 0, "items_committed": 0}), ""

    ident_state_lbl = {
        "name": "antigravity-prod",
        "cli": "antigravity",
        "base_url": "http://127.0.0.1:6310",
        "state": str(st_file),
        "python": sys.executable,
        "context": {"enabled": True, "app_root": str(ROOT), "allowed_roots": [str(root)], "pins": [str(src)]},
    }
    res = cw.run_seat_pass(
        ident_state_lbl,
        store_dir=str(tmp_path / "store1"),
        rate_controller=cw.RateController(now_fn=lambda: datetime.datetime(2026, 10, 26, 10, 0, tzinfo=datetime.timezone.utc)),
        box_enrolled_inodes={},
        quarantined_seats=set(),
        missing_driver_logged=set(),
        set_status=lambda *a, **k: None,
        note=lambda *a, **k: None,
        runner=fake_runner,
    )
    assert res["outcome"] == "ok"
    seat_idx = seen_argv[0].index("--seat")
    assert seen_argv[0][seat_idx + 1] == "antigravity"

    ident_top_seat = {
        "name": "bxxxxx-prod",
        "seat": "memory-bxxxxx",
        "cli": "claude",
        "base_url": "http://127.0.0.1:6310",
        "state": str(tmp_path / "missing-state.json"),
        "python": sys.executable,
        "context": {"enabled": True},
    }
    assert cw.resolve_seat(ident_top_seat) == "memory-bxxxxx"

    # 2. Multi-server isolation: latching slow `:17070` test server at night does NOT latch healthy `:6310` prod server
    mono = [100.0]
    night_dt = datetime.datetime(2026, 10, 26, 2, 0, tzinfo=datetime.timezone.utc)
    rc = cw.RateController(
        probe_fn=lambda url: "6310" in url,
        now_fn=lambda: night_dt,
        mono_fn=lambda: mono[0],
    )
    assert rc.budget("http://127.0.0.1:17070") == (8.0, 1)
    mono[0] += 61.0
    assert rc.budget("http://127.0.0.1:17070") == (2.0, 1)  # latched on :17070
    assert rc.budget("http://127.0.0.1:6310") == (8.0, 1)   # :6310 stays unlatched!


def test_per_seat_bucket_pacing_night_window_and_rate_limited_backoff_and_latch(tmp_path):
    # 1. Backoff math: named seconds + 25% (5a1714 / 11d720)
    assert cw.rate_limit_backoff_s({"reason": "rate_limited", "retry_after_s": 16}) == 20.0
    assert cw.rate_limit_backoff_s({"reason": "rate_limited", "retry_after": 8}) == 10.0
    assert cw.rate_limit_backoff_s({"reason": "rate_limited"}) == 12.5

    # 2. Vienna per-seat rates: 0.25/s day (07:00..22:00), 0.35/s night (22:00..07:00), midnight-continuous latch
    # 2026-10-26 is CET (UTC+1): 20:59 UTC == (time withheld) (day); 21:30 UTC == (time withheld) (night 2026-10-27)
    now_utc = [datetime.datetime(2026, 10, 26, 20, 59, tzinfo=datetime.timezone.utc)]
    clock = [1000.0]
    rc = cw.RateController(probe_fn=lambda _u: True, now_fn=lambda: now_utc[0], mono_fn=lambda: clock[0])

    assert rc.seat_rate("kxxx") == 0.25
    now_utc[0] = datetime.datetime(2026, 10, 26, 21, 30, tzinfo=datetime.timezone.utc)  # (time withheld)
    assert rc.seat_rate("kxxx") == 0.35
    assert rc.seat_rate("txxxx") == 0.35

    # mail_rate_limited=True drops and latches only that seat to day rate (0.25/s) across midnight until (time withheld)
    assert rc.seat_rate("txxxx", ident={"mail_rate_limited": True}) == 0.25
    assert rc.seat_latched_night("txxxx") == "2026-10-27"
    assert rc.seat_rate("kxxx") == 0.35  # kxxx unaffected

    # 3. Seat pass hitting HTTP 429 (`rc=5, reason="rate_limited"`) at (time withheld) backs off +25% and latches past midnight
    ident_a = _make_ident(tmp_path, "kxxx", "claude")
    pin_a = Path(ident_a["context"]["allowed_roots"][0]) / "a.jsonl"
    pin_a.write_text('{"x":1}\n', encoding="utf-8")
    ident_a["context"]["pins"] = [str(pin_a)]

    ident_b = _make_ident(tmp_path, "dxxxxx", "claude")
    pin_b = Path(ident_b["context"]["allowed_roots"][0]) / "b.jsonl"
    pin_b.write_text('{"x":2}\n', encoding="utf-8")
    ident_b["context"]["pins"] = [str(pin_b)]

    statuses: dict[str, dict] = {}
    seat_next: dict[str, float] = {}
    calls: list[str] = []

    def runner_429_then_ok(argv, _cwd, _timeout):
        seat = argv[argv.index("--seat") + 1]
        calls.append(seat)
        if seat == "kxxx" and calls.count("kxxx") == 1:
            return 5, json.dumps({"status": "unreachable", "reason": "rate_limited", "retry_after_s": 20}), ""
        return 0, json.dumps({"status": "progressed", "batches_committed": 1, "items_committed": 3}), ""

    r_lim = cw.run_seat_pass(
        ident_a,
        store_dir=str(tmp_path / "st_a"),
        rate_controller=rc,
        box_enrolled_inodes={},
        quarantined_seats=set(),
        missing_driver_logged=set(),
        set_status=lambda n, **kw: statuses.setdefault(n, {}).update(kw),
        note=lambda *_a: None,
        seat_next_mono=seat_next,
        runner=runner_429_then_ok,
        mono_fn=lambda: clock[0],
    )
    assert r_lim["outcome"] == "rate_limited"
    assert statuses["kxxx"]["context_worker"] == "rate_limited"
    assert statuses["kxxx"]["context_backoff_s"] == 25.0  # 20s * 1.25
    assert seat_next["kxxx"] == 1025.0

    # At +10s (1010.0 < 1025.0): kxxx is paced (no child spawned), while dxxxxx still runs immediately!
    clock[0] = 1010.0
    r_paced = cw.run_seat_pass(
        ident_a,
        store_dir=str(tmp_path / "st_a"),
        rate_controller=rc,
        box_enrolled_inodes={},
        quarantined_seats=set(),
        missing_driver_logged=set(),
        set_status=lambda n, **kw: statuses.setdefault(n, {}).update(kw),
        note=lambda *_a: None,
        seat_next_mono=seat_next,
        runner=runner_429_then_ok,
        mono_fn=lambda: clock[0],
    )
    assert r_paced["outcome"] == "paced" and calls == ["kxxx"]

    r_b = cw.run_seat_pass(
        ident_b,
        store_dir=str(tmp_path / "st_b"),
        rate_controller=rc,
        box_enrolled_inodes={},
        quarantined_seats=set(),
        missing_driver_logged=set(),
        set_status=lambda n, **kw: statuses.setdefault(n, {}).update(kw),
        note=lambda *_a: None,
        seat_next_mono=seat_next,
        runner=runner_429_then_ok,
        mono_fn=lambda: clock[0],
    )
    assert r_b["outcome"] == "ok" and calls == ["kxxx", "dxxxxx"]
    # dxxxxx was unlatched at (time withheld) (0.35/s): 4 writes / 0.35 = 11.42857s cooldown
    assert abs(seat_next["dxxxxx"] - (1010.0 + 4.0 / 0.35)) < 1e-6

    # Past midnight ((time withheld) == 00:30 UTC on 2026-10-27): kxxx is STILL latched to 0.25/s!
    now_utc[0] = datetime.datetime(2026, 10, 27, 0, 30, tzinfo=datetime.timezone.utc)
    clock[0] = 1030.0
    assert rc.seat_rate("kxxx") == 0.25
    r_a2 = cw.run_seat_pass(
        ident_a,
        store_dir=str(tmp_path / "st_a"),
        rate_controller=rc,
        box_enrolled_inodes={},
        quarantined_seats=set(),
        missing_driver_logged=set(),
        set_status=lambda n, **kw: statuses.setdefault(n, {}).update(kw),
        note=lambda *_a: None,
        seat_next_mono=seat_next,
        runner=runner_429_then_ok,
        mono_fn=lambda: clock[0],
    )
    assert r_a2["outcome"] == "ok"
    # Latched at 0.25/s -> 4 writes / 0.25 = 16.0s cooldown (1030.0 + 16.0 == 1046.0)
    assert abs(seat_next["kxxx"] - 1046.0) < 1e-6

    # Next Vienna night ((time withheld) on 2026-10-27 == 21:30 UTC): latch resets back to 0.35/s!
    now_utc[0] = datetime.datetime(2026, 10, 27, 21, 30, tzinfo=datetime.timezone.utc)
    assert rc.seat_rate("kxxx") == 0.35


def test_tzdata_free_postbox_import_and_eu_central_dst_fallback_and_compact_supersede(tmp_path, monkeypatch):
    # 1. Codex P1 regression: postbox.py --status and context_worker import succeed under PYTHONTZPATH="" and -S
    env = dict(os.environ, PYTHONTZPATH="", PYTHONDONTWRITEBYTECODE="1")
    r_sub = subprocess.run(
        [sys.executable, "-S", str(PB_DIR / "postbox.py"), "--status"],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert r_sub.returncode == 0, (r_sub.stdout, r_sub.stderr)
    assert "ZoneInfoNotFoundError" not in r_sub.stderr

    # Pure-stdlib EU Central Time fallback matches exact CET/CEST DST transitions when ZoneInfo is unavailable
    monkeypatch.setattr(cw, "_VIENNA_ZONEINFO", False)
    # 2026 spring forward: last Sunday of March is 2026-03-29 at 01:00 UTC (01:59 CET -> 03:00 CEST)
    dt_before_mar = datetime.datetime(2026, 3, 29, 0, 59, tzinfo=datetime.timezone.utc)
    dt_at_mar = datetime.datetime(2026, 3, 29, 1, 0, tzinfo=datetime.timezone.utc)
    assert cw._vienna_local(dt_before_mar).hour == 1 and cw._vienna_local(dt_before_mar).tzname() == "CET"
    assert cw._vienna_local(dt_at_mar).hour == 3 and cw._vienna_local(dt_at_mar).tzname() == "CEST"
    # 2026 autumn fall-back: last Sunday of October is 2026-10-25 at 01:00 UTC (02:59 CEST -> 02:00 CET)
    dt_before_oct = datetime.datetime(2026, 10, 25, 0, 59, tzinfo=datetime.timezone.utc)
    dt_at_oct = datetime.datetime(2026, 10, 25, 1, 0, tzinfo=datetime.timezone.utc)
    assert cw._vienna_local(dt_before_oct).hour == 2 and cw._vienna_local(dt_before_oct).tzname() == "CEST"
    assert cw._vienna_local(dt_at_oct).hour == 2 and cw._vienna_local(dt_at_oct).tzname() == "CET"

    # 2. Codex P2 regression: enrolling Antigravity transcript.jsonl before transcript_full.jsonl exists,
    # then upgrading once transcript_full.jsonl appears, retires the compact entry so only full remains active
    # and subsequent compact removal never quarantines the seat.
    allowed = tmp_path / "brain"
    logs = allowed / "conv-up-1" / ".system_generated" / "logs"
    logs.mkdir(parents=True)
    compact = logs / "transcript.jsonl"
    full = logs / "transcript_full.jsonl"
    compact.write_text('{"step_index":0}\n', encoding="utf-8")

    ledger = str(tmp_path / "store" / "enrollment.json")
    box_inodes: dict = {}
    ok1, reason1, _ = cw.enroll_file(
        ledger,
        seat="antigravity",
        dialect="antigravity",
        file_path=str(compact),
        allowed_roots=[str(allowed)],
        box_enrolled_inodes=box_inodes,
    )
    assert ok1 is True and reason1 == "enrolled"
    s1, err1 = cw.verified_enrolled_sources(
        ledger, seat="antigravity", dialect="antigravity", allowed_roots=[str(allowed)], box_enrolled_inodes={}
    )
    assert err1 is None and s1 == [os.path.abspath(str(compact))]

    # Now transcript_full.jsonl is created; next pass upgrades and retires compact from ledger["files"]
    full.write_text('{"step_index":0,"content":"full"}\n', encoding="utf-8")
    ok2, reason2, _ = cw.enroll_file(
        ledger,
        seat="antigravity",
        dialect="antigravity",
        file_path=str(compact),
        allowed_roots=[str(allowed)],
        box_enrolled_inodes=box_inodes,
    )
    assert ok2 is True and reason2 == "enrolled"
    loaded_after = cw.read_ledger(ledger)
    assert os.path.normcase(os.path.abspath(str(compact))) not in loaded_after["files"]
    assert os.path.normcase(os.path.abspath(str(compact))) in loaded_after.get("superseded", {})
    s2, err2 = cw.verified_enrolled_sources(
        ledger, seat="antigravity", dialect="antigravity", allowed_roots=[str(allowed)], box_enrolled_inodes={}
    )
    assert err2 is None and s2 == [os.path.abspath(str(full))]

    # Removing obsolete compact file afterwards must NOT trigger file_identity_changed!
    compact.unlink()
    ok3, reason3, _ = cw.enroll_file(
        ledger,
        seat="antigravity",
        dialect="antigravity",
        file_path=str(compact),
        allowed_roots=[str(allowed)],
        box_enrolled_inodes=box_inodes,
    )
    assert ok3 is True and reason3 == "already_enrolled"
    s3, err3 = cw.verified_enrolled_sources(
        ledger, seat="antigravity", dialect="antigravity", allowed_roots=[str(allowed)], box_enrolled_inodes={}
    )
    assert err3 is None and s3 == [os.path.abspath(str(full))]

    # 3. Codex P1 regression (4375868180): after daemon restart, persisted inode ownership from seat B
    # must be preloaded BEFORE processing earlier seat A's pins in [A, B], so A is refused and B stays healthy.
    shared_root = tmp_path / "shared_tx"
    shared_root.mkdir()
    b_log = shared_root / "b_session.jsonl"
    b_log.write_text('{"type":"user"}\n', encoding="utf-8")
    store_root = tmp_path / "restart_stores"
    b_ledger = str(store_root / "seat-b" / "enrollment.json")
    ok_b_pre, _, _ = cw.enroll_file(
        b_ledger,
        seat="seat-b",
        dialect="claude",
        file_path=str(b_log),
        allowed_roots=[str(shared_root)],
    )
    assert ok_b_pre is True

    # Now seat-a (ordered BEFORE seat-b!) tries to pin b_log on restart
    ident_stealer = {
        "name": "seat-a",
        "seat": "seat-a",
        "python": sys.executable,
        "state": str(tmp_path / "a.json"),
        "base_url": "http://127.0.0.1:6310",
        "context": {
            "enabled": True,
            "app_root": str(ROOT),
            "dialect": "claude",
            "allowed_roots": [str(shared_root)],
            "pins": [str(b_log)],
        },
    }
    ident_owner = {
        "name": "seat-b",
        "seat": "seat-b",
        "python": sys.executable,
        "state": str(tmp_path / "b.json"),
        "base_url": "http://127.0.0.1:6310",
        "context": {
            "enabled": True,
            "app_root": str(ROOT),
            "dialect": "claude",
            "allowed_roots": [str(shared_root)],
            "pins": [str(b_log)],
        },
    }
    uploaded_by: list[str] = []
    restart_statuses: dict[str, dict] = {}
    cw.run_context_worker(
        [ident_stealer, ident_owner],
        store_root=str(store_root),
        set_status=lambda n, **kw: restart_statuses.setdefault(n, {}).update(kw),
        note=lambda *_a: None,
        rate_controller=cw.RateController(probe_fn=lambda _u: True),
        runner=lambda argv, _c, _t: (
            uploaded_by.append(argv[argv.index("--seat") + 1])
            or (0, json.dumps({"status": "drained", "batches_committed": 0, "items_committed": 0}), "")
        ),
        sleep_fn=lambda _s: None,
        max_rounds=1,
    )
    assert uploaded_by == ["seat-b"]
    assert restart_statuses["seat-a"]["context_worker"] == "conflict"
    assert restart_statuses["seat-a"]["context_error"] == "duplicate_inode_across_seats"
    assert restart_statuses["seat-b"]["context_worker"] == "ok"

    # 4. Codex P1 regression (4375989307): disabled / context-disabled identity with custom context.store_dir
    # outside context_store_root still reserves its persisted inode via build_context_worker_thread(cfg, active)
    # without receiving capture passes!
    custom_disabled_store = tmp_path / "custom_outside_store_b"
    custom_disabled_log = shared_root / "disabled_b.jsonl"
    custom_disabled_log.write_text('{"type":"user"}\n', encoding="utf-8")
    assert cw.enroll_file(
        str(custom_disabled_store / "enrollment.json"),
        seat="disabled-b",
        dialect="claude",
        file_path=str(custom_disabled_log),
        allowed_roots=[str(shared_root)],
    )[0] is True

    ident_disabled_b = {
        "name": "disabled-b",
        "seat": "disabled-b",
        "enabled": False,
        "python": sys.executable,
        "state": str(tmp_path / "db.json"),
        "base_url": "http://127.0.0.1:6310",
        "context": {
            "enabled": True,
            "store_dir": str(custom_disabled_store),
            "dialect": "claude",
            "allowed_roots": [str(shared_root)],
            "pins": [str(custom_disabled_log)],
        },
    }
    ident_steal_disabled = {
        "name": "stealer-a",
        "seat": "stealer-a",
        "enabled": True,
        "python": sys.executable,
        "state": str(tmp_path / "sa.json"),
        "base_url": "http://127.0.0.1:6310",
        "context": {
            "enabled": True,
            "app_root": str(ROOT),
            "dialect": "claude",
            "allowed_roots": [str(shared_root)],
            "pins": [str(custom_disabled_log)],
        },
    }
    import tools.postbox.postbox as pb_mod

    th = pb_mod.build_context_worker_thread(
        {"context_store_root": str(tmp_path / "isolated_ctx_root"), "identities": [ident_steal_disabled, ident_disabled_b]},
        [ident_steal_disabled],
    )
    assert th is not None
    assert th._args == ([ident_steal_disabled],)
    assert th._kwargs["all_identities"] == [ident_steal_disabled, ident_disabled_b]

    dis_statuses: dict[str, dict] = {}
    dis_uploaded: list[str] = []
    cw.run_context_worker(
        list(th._args[0]),
        all_identities=th._kwargs["all_identities"],
        store_root=th._kwargs["store_root"],
        set_status=lambda n, **kw: dis_statuses.setdefault(n, {}).update(kw),
        note=lambda *_a: None,
        rate_controller=cw.RateController(probe_fn=lambda _u: True),
        runner=lambda argv, _c, _t: (
            dis_uploaded.append(argv[argv.index("--seat") + 1])
            or (0, json.dumps({"status": "drained", "batches_committed": 0, "items_committed": 0}), "")
        ),
        sleep_fn=lambda _s: None,
        max_rounds=1,
    )
    assert dis_uploaded == []
    assert "disabled-b" not in dis_statuses
    assert dis_statuses["stealer-a"]["context_worker"] == "conflict"
    assert dis_statuses["stealer-a"]["context_error"] == "duplicate_inode_across_seats"

    # 5. Codex P2 regression (4375989313): if an enrolled Antigravity compact transcript.jsonl is replaced by
    # sibling transcript_full.jsonl while the daemon is stopped, startup upgrades the persisted entry before
    # verified_enrolled_sources runs instead of quarantining on file_identity_changed!
    ag_stop_dir = tmp_path / "ag_stopped_upgrade"
    ag_stop_dir.mkdir()
    ag_c = ag_stop_dir / "transcript.jsonl"
    ag_c.write_text('{"step_index":0}\n', encoding="utf-8")
    ag_stop_stores = tmp_path / "ag_stop_stores"
    assert cw.enroll_file(
        str(ag_stop_stores / "ag-seat" / "enrollment.json"),
        seat="antigravity",
        dialect="antigravity",
        file_path=str(ag_c),
        allowed_roots=[str(ag_stop_dir)],
    )[0] is True
    # Daemon stops: compact file disappears and transcript_full.jsonl takes its place (even with pins=[]!)
    ag_c.unlink()
    ag_f = ag_stop_dir / "transcript_full.jsonl"
    ag_f.write_text('{"step_index":0,"full":true}\n', encoding="utf-8")

    ident_ag_restart = {
        "name": "ag-seat",
        "seat": "antigravity",
        "python": sys.executable,
        "state": str(tmp_path / "ag.json"),
        "base_url": "http://127.0.0.1:6310",
        "context": {
            "enabled": True,
            "app_root": str(ROOT),
            "dialect": "antigravity",
            "allowed_roots": [str(ag_stop_dir)],
            "pins": [],
        },
    }
    ag_restart_sources: list[str] = []
    ag_restart_statuses: dict[str, dict] = {}
    cw.run_context_worker(
        [ident_ag_restart],
        store_root=str(ag_stop_stores),
        set_status=lambda n, **kw: ag_restart_statuses.setdefault(n, {}).update(kw),
        note=lambda *_a: None,
        rate_controller=cw.RateController(probe_fn=lambda _u: True),
        runner=lambda argv, _c, _t: (
            ag_restart_sources.append(argv[argv.index("--source") + 1])
            or (0, json.dumps({"status": "drained", "batches_committed": 0, "items_committed": 0}), "")
        ),
        sleep_fn=lambda _s: None,
        max_rounds=1,
    )
    assert ag_restart_sources == [os.path.abspath(str(ag_f))]
    assert ag_restart_statuses["ag-seat"]["context_worker"] == "ok"

    # 6. Codex P2 regression (4376114662): valid JSON with malformed files entry (e.g. {"v":1,"files":{"bad":null}})
    # raises LedgerError inside read_ledger() so _preload_box_inodes() quarantines ONLY that seat while healthy seats run!
    bad_entry_stores = tmp_path / "bad_entry_stores"
    (bad_entry_stores / "bad-seat").mkdir(parents=True)
    (bad_entry_stores / "bad-seat" / "enrollment.json").write_text('{"v":1,"files":{"bad-entry":null}}', encoding="utf-8")
    with pytest.raises(cw.LedgerError, match="corrupt enrollment ledger"):
        cw.read_ledger(str(bad_entry_stores / "bad-seat" / "enrollment.json"))
    bad_sup_ledger = tmp_path / "bad_superseded.json"
    bad_sup_ledger.write_text('{"v":1,"files":{},"superseded":{"s1":null}}', encoding="utf-8")
    with pytest.raises(cw.LedgerError, match="corrupt enrollment ledger"):
        cw.read_ledger(str(bad_sup_ledger))
    good_after_bad_log = shared_root / "good_after_bad.jsonl"
    good_after_bad_log.write_text('{"type":"user"}\n', encoding="utf-8")
    ident_bad_entry = {
        "name": "bad-seat",
        "seat": "bad-seat",
        "python": sys.executable,
        "state": str(tmp_path / "be.json"),
        "base_url": "http://127.0.0.1:6310",
        "context": {
            "enabled": True,
            "app_root": str(ROOT),
            "dialect": "claude",
            "allowed_roots": [str(shared_root)],
            "pins": [],
        },
    }
    ident_good_after_bad = {
        "name": "good-after-bad",
        "seat": "good-after-bad",
        "python": sys.executable,
        "state": str(tmp_path / "gab.json"),
        "base_url": "http://127.0.0.1:6310",
        "context": {
            "enabled": True,
            "app_root": str(ROOT),
            "dialect": "claude",
            "allowed_roots": [str(shared_root)],
            "pins": [str(good_after_bad_log)],
        },
    }
    gab_uploaded: list[str] = []
    gab_statuses: dict[str, dict] = {}
    cw.run_context_worker(
        [ident_bad_entry, ident_good_after_bad],
        store_root=str(bad_entry_stores),
        set_status=lambda n, **kw: gab_statuses.setdefault(n, {}).update(kw),
        note=lambda *_a: None,
        rate_controller=cw.RateController(probe_fn=lambda _u: True),
        runner=lambda argv, _c, _t: (
            gab_uploaded.append(argv[argv.index("--seat") + 1])
            or (0, json.dumps({"status": "drained", "batches_committed": 0, "items_committed": 0}), "")
        ),
        sleep_fn=lambda _s: None,
        max_rounds=1,
    )
    assert gab_uploaded == ["good-after-bad"]
    assert gab_statuses["bad-seat"]["context_worker"] == "conflict"
    assert gab_statuses["bad-seat"]["context_error"] == "corrupt_enrollment_ledger"
    assert gab_statuses["good-after-bad"]["context_worker"] == "ok"

    # 7. Codex P1 regression (4376304550): if disabled/context-disabled seat B enrolled an Antigravity compact
    # transcript.jsonl and sibling transcript_full.jsonl appears while stopped, _preload_box_inodes() preserves
    # ownership of BOTH the compact source and its full-log replacement so enabled seat A cannot steal either!
    dis_ag_dir = tmp_path / "disabled_ag_upgrade"
    dis_ag_dir.mkdir()
    dis_ag_c = dis_ag_dir / "transcript.jsonl"
    dis_ag_c.write_text('{"step_index":0}\n', encoding="utf-8")
    dis_ag_stores = tmp_path / "dis_ag_stores"
    assert cw.enroll_file(
        str(dis_ag_stores / "seat-b" / "enrollment.json"),
        seat="seat-b",
        dialect="antigravity",
        file_path=str(dis_ag_c),
        allowed_roots=[str(dis_ag_dir)],
    )[0] is True
    dis_ag_f = dis_ag_dir / "transcript_full.jsonl"
    dis_ag_f.write_text('{"step_index":0,"full":true}\n', encoding="utf-8")
    b_ledger = cw.read_ledger(str(dis_ag_stores / "seat-b" / "enrollment.json"))
    [b_entry] = list(b_ledger["files"].values())
    f_st = os.stat(dis_ag_f)
    assert (int(f_st.st_dev), int(f_st.st_ino)) in cw._entry_reserved_inodes(b_entry)

    ident_steal_full = {
        "name": "seat-a-ag",
        "seat": "seat-a-ag",
        "python": sys.executable,
        "state": str(tmp_path / "sa1.json"),
        "base_url": "http://127.0.0.1:6310",
        "context": {
            "enabled": True,
            "app_root": str(ROOT),
            "dialect": "antigravity",
            "allowed_roots": [str(dis_ag_dir)],
            "pins": [str(dis_ag_c)],
        },
    }
    ident_steal_compact = {
        "name": "seat-a-cl",
        "seat": "seat-a-cl",
        "python": sys.executable,
        "state": str(tmp_path / "sa2.json"),
        "base_url": "http://127.0.0.1:6310",
        "context": {
            "enabled": True,
            "app_root": str(ROOT),
            "dialect": "claude",
            "allowed_roots": [str(dis_ag_dir)],
            "pins": [str(dis_ag_c)],
        },
    }
    ident_dis_b = {
        "name": "seat-b",
        "seat": "seat-b",
        "enabled": False,
        "context": {
            "enabled": False,
            "dialect": "antigravity",
            "allowed_roots": [str(dis_ag_dir)],
            "pins": [],
        },
    }
    steal_uploaded: list[str] = []
    steal_statuses: dict[str, dict] = {}
    cw.run_context_worker(
        [ident_steal_full, ident_steal_compact],
        all_identities=[ident_steal_full, ident_steal_compact, ident_dis_b],
        store_root=str(dis_ag_stores),
        set_status=lambda n, **kw: steal_statuses.setdefault(n, {}).update(kw),
        note=lambda *_a: None,
        rate_controller=cw.RateController(probe_fn=lambda _u: True),
        runner=lambda argv, _c, _t: (
            steal_uploaded.append(argv[argv.index("--seat") + 1])
            or (0, json.dumps({"status": "drained", "batches_committed": 0, "items_committed": 0}), "")
        ),
        sleep_fn=lambda _s: None,
        max_rounds=1,
    )
    assert steal_uploaded == []
    assert steal_statuses["seat-a-ag"]["context_worker"] == "conflict"
    assert steal_statuses["seat-a-ag"]["context_error"] == "duplicate_inode_across_seats"
    assert steal_statuses["seat-a-cl"]["context_worker"] == "conflict"
    assert steal_statuses["seat-a-cl"]["context_error"] == "duplicate_inode_across_seats"

    # 8. Codex P1 regression (4376395407): two-round reproduction where disabled seat B has enrolled compact
    # transcript.jsonl at startup, and sibling transcript_full.jsonl is created AFTER round 1 (between rounds)!
    # Enabled seat A pinning transcript_full.jsonl gets missing_file in round 1 and MUST be refused with
    # duplicate_inode_across_seats in round 2 with ZERO uploader invocations!
    r2_dir = tmp_path / "r2_disabled_ag_upgrade"
    r2_dir.mkdir()
    r2_c = r2_dir / "transcript.jsonl"
    r2_c.write_text('{"step_index":0}\n', encoding="utf-8")
    r2_stores = tmp_path / "r2_stores"
    assert cw.enroll_file(
        str(r2_stores / "seat-b" / "enrollment.json"),
        seat="seat-b",
        dialect="antigravity",
        file_path=str(r2_c),
        allowed_roots=[str(r2_dir)],
    )[0] is True
    r2_f = r2_dir / "transcript_full.jsonl"  # does NOT exist at startup!

    ident_r2_a = {
        "name": "r2-seat-a",
        "seat": "r2-seat-a",
        "python": sys.executable,
        "state": str(tmp_path / "r2a.json"),
        "base_url": "http://127.0.0.1:6310",
        "context": {
            "enabled": True,
            "app_root": str(ROOT),
            "dialect": "claude",
            "allowed_roots": [str(r2_dir)],
            "pins": [str(r2_f)],
        },
    }
    ident_r2_b = {
        "name": "seat-b",
        "seat": "seat-b",
        "enabled": False,
        "context": {
            "enabled": False,
            "dialect": "antigravity",
            "allowed_roots": [str(r2_dir)],
            "pins": [],
        },
    }
    r2_uploaded: list[str] = []
    r2_statuses: dict[str, dict] = {}
    r2_sleeps = 0

    def r2_sleep(_s):
        nonlocal r2_sleeps
        r2_sleeps += 1
        if r2_sleeps == 1:
            r2_f.write_text('{"step_index":0,"full":true}\n', encoding="utf-8")

    cw.run_context_worker(
        [ident_r2_a],
        all_identities=[ident_r2_a, ident_r2_b],
        store_root=str(r2_stores),
        set_status=lambda n, **kw: r2_statuses.setdefault(n, {}).update(kw),
        note=lambda *_a: None,
        rate_controller=cw.RateController(probe_fn=lambda _u: True),
        runner=lambda argv, _c, _t: (
            r2_uploaded.append(argv[argv.index("--seat") + 1])
            or (0, json.dumps({"status": "drained", "batches_committed": 0, "items_committed": 0}), "")
        ),
        sleep_fn=r2_sleep,
        max_rounds=2,
    )
    assert r2_uploaded == []
    assert r2_statuses["r2-seat-a"]["context_worker"] == "conflict"
    assert r2_statuses["r2-seat-a"]["context_error"] == "duplicate_inode_across_seats"


def test_app_root_validation_and_pythonpath_injection_for_client_only_install(tmp_path):
    # 1. Missing or invalid context.app_root refuses with config_error / app_root_missing BEFORE spawning child
    ident_missing = _make_ident(tmp_path, "no-approot", "claude")
    ident_missing["context"].pop("app_root", None)
    pin = Path(ident_missing["context"]["allowed_roots"][0]) / "sess.jsonl"
    pin.write_text('{"type":"user"}\n', encoding="utf-8")
    ident_missing["context"]["pins"] = [str(pin)]

    statuses: dict[str, dict] = {}
    quarantined: set[str] = set()
    spawned: list[tuple[list[str], dict[str, str] | None]] = []

    def fake_runner_4(argv, _cwd, _timeout, env_extra=None):
        spawned.append((list(argv), env_extra))
        return 0, json.dumps({"status": "drained", "batches_committed": 0, "items_committed": 0}), ""

    rc_ctrl = cw.RateController(
        probe_fn=lambda _u: True,
        now_fn=lambda: datetime.datetime(2026, 10, 26, 12, 0, tzinfo=datetime.timezone.utc),
    )

    r_miss = cw.run_seat_pass(
        ident_missing,
        store_dir=str(tmp_path / "st_miss"),
        rate_controller=rc_ctrl,
        box_enrolled_inodes={},
        quarantined_seats=quarantined,
        missing_driver_logged=set(),
        set_status=lambda n, **kw: statuses.setdefault(n, {}).update(kw),
        note=lambda *_a: None,
        runner=fake_runner_4,
    )
    assert r_miss["outcome"] == "config_error" and r_miss["more_work"] is False
    assert statuses["no-approot"]["context_worker"] == "config_error"
    assert statuses["no-approot"]["context_config_error"] is True
    assert statuses["no-approot"]["context_error"] == "app_root_missing"
    assert "no-approot" not in quarantined
    assert spawned == []

    # Wrong app_root (has apps/context/ directory, but NO validation.py) -> config_error / app_root_missing
    partial_app = tmp_path / "partial_app_root"
    (partial_app / "apps" / "context").mkdir(parents=True)
    ident_missing["context"]["app_root"] = str(partial_app)
    r_no_val = cw.run_seat_pass(
        ident_missing,
        store_dir=str(tmp_path / "st_miss"),
        rate_controller=rc_ctrl,
        box_enrolled_inodes={},
        quarantined_seats=quarantined,
        missing_driver_logged=set(),
        set_status=lambda n, **kw: statuses.setdefault(n, {}).update(kw),
        note=lambda *_a: None,
        runner=fake_runner_4,
    )
    assert r_no_val["outcome"] == "config_error"
    assert statuses["no-approot"]["context_error"] == "app_root_missing"
    assert spawned == []

    # 2. Valid app_root injects PYTHONPATH=<app_root> into fake 4-arg runner
    ident_missing["context"]["app_root"] = str(ROOT)
    r_ok_env = cw.run_seat_pass(
        ident_missing,
        store_dir=str(tmp_path / "st_ok"),
        rate_controller=rc_ctrl,
        box_enrolled_inodes={},
        quarantined_seats=quarantined,
        missing_driver_logged=set(),
        set_status=lambda n, **kw: statuses.setdefault(n, {}).update(kw),
        note=lambda *_a: None,
        runner=fake_runner_4,
    )
    assert r_ok_env["outcome"] == "ok"
    assert len(spawned) == 1
    assert spawned[0][1] == {"PYTHONPATH": os.path.abspath(str(ROOT))}

    # 3. Real subprocess on a client-only install (cwd has tools/transcript_cards ONLY, NO apps/):
    # `tools` resolves from client cwd first while `apps.context.validation` imports cleanly from PYTHONPATH=app_root
    ident_client_only = _make_ident(tmp_path, "client-only", "claude")
    c_dir = tmp_path / "client_only_tree"
    (c_dir / "tools" / "transcript_cards").mkdir(parents=True)
    (c_dir / "tools" / "__init__.py").write_text("CLIENT_TREE = True\n", encoding="utf-8")
    (c_dir / "tools" / "transcript_cards" / "__init__.py").write_text("", encoding="utf-8")
    (c_dir / "tools" / "transcript_cards" / "context_upload.py").write_text(
        "import json, tools\n"
        "from apps.context.validation import validate_context\n"
        "assert getattr(tools, 'CLIENT_TREE', False) is True\n"
        "assert callable(validate_context)\n"
        "print(json.dumps({'status': 'drained', 'batches_committed': 0, 'items_committed': 0}))\n",
        encoding="utf-8",
    )
    assert not (c_dir / "apps").exists()
    c_pin = Path(ident_client_only["context"]["allowed_roots"][0]) / "c.jsonl"
    c_pin.write_text('{"type":"user"}\n', encoding="utf-8")
    ident_client_only["client"] = str(c_dir)
    ident_client_only["context"]["pins"] = [str(c_pin)]
    ident_client_only["context"]["app_root"] = str(_app_tree(tmp_path / "app_only", uploader=False))

    r_real = cw.run_seat_pass(
        ident_client_only,
        store_dir=str(tmp_path / "st_real_client_only"),
        rate_controller=rc_ctrl,
        box_enrolled_inodes={},
        quarantined_seats=set(),
        missing_driver_logged=set(),
        set_status=lambda n, **kw: statuses.setdefault(n, {}).update(kw),
        note=lambda *_a: None,
    )
    assert r_real["outcome"] == "ok"
    assert statuses["client-only"]["context_worker"] == "ok"


def test_drained_seat_60s_backoff_skips_unchanged_rounds_and_wakes_on_source_touch_or_60s_floor(tmp_path):
    ident = _make_ident(tmp_path, "quiet-seat", "claude")
    src = Path(ident["context"]["allowed_roots"][0]) / "quiet.jsonl"
    src.write_text('{"type":"user","text":"hello"}\n', encoding="utf-8")
    ident["context"]["pins"] = [str(src)]

    clock = [100.0]
    sleeps: list[float] = []
    calls: list[float] = []
    statuses: dict[str, dict] = {}

    def fake_sleep(dt: float):
        sleeps.append(dt)
        clock[0] += dt

    def drained_runner(_argv, _cwd, _timeout):
        calls.append(clock[0])
        return 0, json.dumps({"status": "drained", "batches_committed": 0, "items_committed": 0}), ""

    rc_ctrl = cw.RateController(
        probe_fn=lambda _u: True,
        now_fn=lambda: datetime.datetime(2026, 10, 26, 12, 0, tzinfo=datetime.timezone.utc),
        mono_fn=lambda: clock[0],
    )

    # 1. Two rounds over an unchanged drained seat spawn ONLY ONE child (round 2 at +5s backs off)
    drained_map: dict = {}
    store_dir = str(tmp_path / "quiet_store")
    r1 = cw.run_seat_pass(
        ident,
        store_dir=store_dir,
        rate_controller=rc_ctrl,
        box_enrolled_inodes={},
        quarantined_seats=set(),
        missing_driver_logged=set(),
        set_status=lambda n, **kw: statuses.setdefault(n, {}).update(kw),
        note=lambda *_a: None,
        drained_state=drained_map,
        runner=drained_runner,
        mono_fn=lambda: clock[0],
    )
    assert r1["outcome"] == "ok" and r1["more_work"] is False and len(calls) == 1

    clock[0] = 105.0  # +5s (< 60s), source untouched
    r2 = cw.run_seat_pass(
        ident,
        store_dir=store_dir,
        rate_controller=rc_ctrl,
        box_enrolled_inodes={},
        quarantined_seats=set(),
        missing_driver_logged=set(),
        set_status=lambda n, **kw: statuses.setdefault(n, {}).update(kw),
        note=lambda *_a: None,
        drained_state=drained_map,
        runner=drained_runner,
        mono_fn=lambda: clock[0],
    )
    assert r2["outcome"] == "drained_backoff" and len(calls) == 1

    # 2. Touching / appending to the enrolled source wakes the seat immediately on the next round (at +10s < 60s)
    clock[0] = 110.0
    src.write_text('{"type":"user","text":"hello"}\n{"type":"assistant","text":"world"}\n', encoding="utf-8")
    r3 = cw.run_seat_pass(
        ident,
        store_dir=store_dir,
        rate_controller=rc_ctrl,
        box_enrolled_inodes={},
        quarantined_seats=set(),
        missing_driver_logged=set(),
        set_status=lambda n, **kw: statuses.setdefault(n, {}).update(kw),
        note=lambda *_a: None,
        drained_state=drained_map,
        runner=drained_runner,
        mono_fn=lambda: clock[0],
    )
    assert r3["outcome"] == "ok" and len(calls) == 2

    # Unchanged at +15s (125.0 < 170.0) -> still backed off
    clock[0] = 125.0
    r4 = cw.run_seat_pass(
        ident,
        store_dir=store_dir,
        rate_controller=rc_ctrl,
        box_enrolled_inodes={},
        quarantined_seats=set(),
        missing_driver_logged=set(),
        set_status=lambda n, **kw: statuses.setdefault(n, {}).update(kw),
        note=lambda *_a: None,
        drained_state=drained_map,
        runner=drained_runner,
        mono_fn=lambda: clock[0],
    )
    assert r4["outcome"] == "drained_backoff" and len(calls) == 2

    # 3. At +60s floor (170.0 >= 110.0 + 60.0), seat spawns even with unchanged file so age-held partial batches flush
    clock[0] = 170.0
    r5 = cw.run_seat_pass(
        ident,
        store_dir=store_dir,
        rate_controller=rc_ctrl,
        box_enrolled_inodes={},
        quarantined_seats=set(),
        missing_driver_logged=set(),
        set_status=lambda n, **kw: statuses.setdefault(n, {}).update(kw),
        note=lambda *_a: None,
        drained_state=drained_map,
        runner=drained_runner,
        mono_fn=lambda: clock[0],
    )
    assert r5["outcome"] == "ok" and len(calls) == 3

    # 4. Also verify run_context_worker end-to-end: 2 rounds on unchanged drained seat spawn 1 child,
    # while more_work=True keeps the 5s / rate-paced cadence without entering 60s drained backoff.
    worker_calls: list[int] = []
    w_clock = [500.0]
    cw.run_context_worker(
        [ident],
        store_root=str(tmp_path / "w_stores"),
        set_status=lambda *_a, **_k: None,
        note=lambda *_a: None,
        rate_controller=cw.RateController(probe_fn=lambda _u: True, mono_fn=lambda: w_clock[0]),
        runner=lambda _a, _c, _t: (
            worker_calls.append(1)
            or (0, json.dumps({"status": "drained", "batches_committed": 0, "items_committed": 0}), "")
        ),
        mono_fn=lambda: w_clock[0],
        sleep_fn=lambda dt: w_clock.__setitem__(0, w_clock[0] + dt),
        idle_sleep_s=5.0,
        max_rounds=3,
    )
    assert len(worker_calls) == 1  # rounds 2 and 3 (+5s, +10s) skipped via drained_backoff!


def test_upgraded_antigravity_compact_inode_stays_reserved_live_and_across_restarts(tmp_path):
    """Codex P1 regression (4376998926): upgrading an enrolled compact transcript.jsonl to transcript_full.jsonl
    retains seat ownership of the compact file's (dev, ino) both live and across daemon restarts (even after the
    original compact path is unlinked while a hardlink alias survives), without quarantining the upgrading seat."""
    b_logs = tmp_path / "b_root" / "brain" / "conv-1" / ".system_generated" / "logs"
    b_logs.mkdir(parents=True)
    b_compact = b_logs / "transcript.jsonl"
    b_compact.write_text('{"step_index":0,"content":"secret-b"}\n', encoding="utf-8")

    a_root = tmp_path / "a_root"
    a_root.mkdir()
    a_alias = a_root / "stolen_alias.jsonl"
    os.link(str(b_compact), str(a_alias))

    stores = tmp_path / "stores"
    ident_b = {
        "name": "seat-b",
        "seat": "seat-b",
        "python": sys.executable,
        "state": str(tmp_path / "b.json"),
        "base_url": "http://127.0.0.1:6310",
        "context": {
            "enabled": True,
            "app_root": str(ROOT),
            "dialect": "antigravity",
            "allowed_roots": [str(tmp_path / "b_root")],
            "pins": [str(b_compact)],
        },
    }
    ident_a = {
        "name": "seat-a",
        "seat": "seat-a",
        "python": sys.executable,
        "state": str(tmp_path / "a.json"),
        "base_url": "http://127.0.0.1:6310",
        "context": {
            "enabled": True,
            "app_root": str(ROOT),
            "dialect": "claude",
            "allowed_roots": [str(a_root)],
            "pins": [str(a_alias)],
        },
    }

    rc_ctrl = cw.RateController(probe_fn=lambda _u: True)
    box_inodes: dict = {}
    quarantined: set[str] = set()
    statuses: dict[str, dict] = {}
    uploaded_seats: list[str] = []

    def rec_runner(argv, _c, _t):
        uploaded_seats.append(argv[argv.index("--seat") + 1])
        return 0, json.dumps({"status": "drained", "batches_committed": 0, "items_committed": 0}), ""

    # 1. Seat B enrolls compact transcript.jsonl first; then transcript_full.jsonl appears and Seat B upgrades live.
    r_b1 = cw.run_seat_pass(
        ident_b,
        store_dir=str(stores / "seat-b"),
        rate_controller=rc_ctrl,
        box_enrolled_inodes=box_inodes,
        quarantined_seats=quarantined,
        missing_driver_logged=set(),
        set_status=lambda n, **kw: statuses.setdefault(n, {}).update(kw),
        note=lambda *_a: None,
        runner=rec_runner,
    )
    assert r_b1["outcome"] == "ok"

    b_full = b_logs / "transcript_full.jsonl"
    b_full.write_text('{"step_index":0,"content":"secret-b-full"}\n', encoding="utf-8")
    r_b2 = cw.run_seat_pass(
        ident_b,
        store_dir=str(stores / "seat-b"),
        rate_controller=rc_ctrl,
        box_enrolled_inodes=box_inodes,
        quarantined_seats=quarantined,
        missing_driver_logged=set(),
        set_status=lambda n, **kw: statuses.setdefault(n, {}).update(kw),
        note=lambda *_a: None,
        runner=rec_runner,
    )
    assert r_b2["outcome"] == "ok"

    # Seat A now attempts live enrollment of its hardlink alias to Seat B's compact file -> MUST be refused!
    r_a_live = cw.run_seat_pass(
        ident_a,
        store_dir=str(stores / "seat-a"),
        rate_controller=rc_ctrl,
        box_enrolled_inodes=box_inodes,
        quarantined_seats=quarantined,
        missing_driver_logged=set(),
        set_status=lambda n, **kw: statuses.setdefault(n, {}).update(kw),
        note=lambda *_a: None,
        runner=rec_runner,
    )
    assert r_a_live["outcome"] == "conflict"
    assert statuses["seat-a"]["context_error"] == "duplicate_inode_across_seats"
    assert "seat-a" not in uploaded_seats

    # 2. Unlink Seat B's obsolete compact path while Seat A's hardlink still keeps the compact inode alive,
    # then simulate a fresh daemon restart: Seat B must stay healthy (ok) while Seat A is STILL refused!
    b_compact.unlink()
    ident_b["context"]["pins"] = [str(b_full)]
    restart_statuses: dict[str, dict] = {}
    restart_uploaded: list[tuple[str, list[str]]] = []

    def restart_runner(argv, _c, _t):
        s_name = argv[argv.index("--seat") + 1]
        srcs = [argv[i + 1] for i, tok in enumerate(argv) if tok == "--source"]
        restart_uploaded.append((s_name, srcs))
        return 0, json.dumps({"status": "drained", "batches_committed": 0, "items_committed": 0}), ""

    cw.run_context_worker(
        [ident_b, ident_a],
        store_root=str(stores),
        set_status=lambda n, **kw: restart_statuses.setdefault(n, {}).update(kw),
        note=lambda *_a: None,
        rate_controller=cw.RateController(probe_fn=lambda _u: True),
        runner=restart_runner,
        sleep_fn=lambda _s: None,
        max_rounds=1,
    )
    assert restart_statuses["seat-b"]["context_worker"] == "ok"
    assert restart_statuses["seat-a"]["context_worker"] == "conflict"
    assert restart_statuses["seat-a"]["context_error"] == "duplicate_inode_across_seats"
    assert restart_uploaded == [("seat-b", [str(b_full.resolve())])] or (
        len(restart_uploaded) == 1
        and restart_uploaded[0][0] == "seat-b"
        and os.path.normcase(restart_uploaded[0][1][0]) == os.path.normcase(str(b_full))
    )

    # 3. Disabled Seat B across restart ALSO retains superseded compact inode ownership against enabled Seat A2.
    ident_b_disabled = dict(ident_b, enabled=False, context=dict(ident_b["context"], enabled=False))
    ident_a2 = dict(ident_a, name="seat-a2", seat="seat-a2")
    dis_statuses: dict[str, dict] = {}
    dis_uploaded: list[str] = []
    cw.run_context_worker(
        [ident_a2],
        all_identities=[ident_a2, ident_b_disabled],
        store_root=str(stores),
        set_status=lambda n, **kw: dis_statuses.setdefault(n, {}).update(kw),
        note=lambda *_a: None,
        rate_controller=cw.RateController(probe_fn=lambda _u: True),
        runner=lambda argv, _c, _t: (
            dis_uploaded.append(argv[argv.index("--seat") + 1])
            or (0, json.dumps({"status": "drained", "batches_committed": 0, "items_committed": 0}), "")
        ),
        sleep_fn=lambda _s: None,
        max_rounds=1,
    )
    assert dis_uploaded == []
    assert dis_statuses["seat-a2"]["context_worker"] == "conflict"
    assert dis_statuses["seat-a2"]["context_error"] == "duplicate_inode_across_seats"

    # 4. Direct enrollment when BOTH compact and full already exist reserves sibling compact inode immediately,
    # and corrupting superseded dev/ino types raises LedgerError.
    d_logs = tmp_path / "direct_b" / "logs"
    d_logs.mkdir(parents=True)
    d_c = d_logs / "transcript.jsonl"
    d_f = d_logs / "transcript_full.jsonl"
    d_c.write_text('{"step_index":0}\n', encoding="utf-8")
    d_f.write_text('{"step_index":0,"full":true}\n', encoding="utf-8")
    d_alias = a_root / "direct_alias.jsonl"
    os.link(str(d_c), str(d_alias))
    direct_inodes: dict = {}
    d_ledger = str(tmp_path / "direct_store" / "enrollment.json")
    assert cw.enroll_file(
        d_ledger,
        seat="seat-b",
        dialect="antigravity",
        file_path=str(d_c),
        allowed_roots=[str(tmp_path / "direct_b")],
        box_enrolled_inodes=direct_inodes,
    )[:2] == (True, "enrolled")
    assert cw.enroll_file(
        str(tmp_path / "direct_a" / "enrollment.json"),
        seat="seat-a",
        dialect="claude",
        file_path=str(d_alias),
        allowed_roots=[str(a_root)],
        box_enrolled_inodes=direct_inodes,
    ) == (False, "duplicate_inode_across_seats", None)

    bad_sup = json.loads(Path(d_ledger).read_text(encoding="utf-8"))
    sup_key = next(iter(bad_sup["superseded"]))
    bad_sup["superseded"][sup_key]["dev"] = True
    Path(d_ledger).write_text(json.dumps(bad_sup), encoding="utf-8")
    try:
        cw.read_ledger(d_ledger)
    except cw.LedgerError:
        pass
    else:
        raise AssertionError("boolean dev in superseded ledger entry must raise LedgerError")

    # 5. Unlinked compact file BEFORE upgrade (while Seat A still holds a hardlink to old_c's inode):
    # upgrade_antigravity_path still upgrades Seat B's pin to transcript_full.jsonl, and enroll_file MUST
    # retain old_c['dev']/old_c['ino'] in box_enrolled_inodes even though compact_abs is already absent!
    u_logs = tmp_path / "unlinked_b" / "logs"
    u_logs.mkdir(parents=True)
    u_c = u_logs / "transcript.jsonl"
    u_c.write_text('{"step_index":0}\n', encoding="utf-8")
    u_alias = a_root / "unlinked_alias.jsonl"
    os.link(str(u_c), str(u_alias))
    u_inodes: dict = {}
    u_ledger_b = str(tmp_path / "unlinked_store_b" / "enrollment.json")
    assert cw.enroll_file(
        u_ledger_b,
        seat="seat-b",
        dialect="antigravity",
        file_path=str(u_c),
        allowed_roots=[str(tmp_path / "unlinked_b")],
        box_enrolled_inodes=u_inodes,
    )[:2] == (True, "enrolled")
    u_f = u_logs / "transcript_full.jsonl"
    u_f.write_text('{"step_index":0,"full":true}\n', encoding="utf-8")
    u_c.unlink()
    assert cw.enroll_file(
        u_ledger_b,
        seat="seat-b",
        dialect="antigravity",
        file_path=str(u_c),
        allowed_roots=[str(tmp_path / "unlinked_b")],
        box_enrolled_inodes=u_inodes,
    )[:2] == (True, "enrolled")
    assert cw.enroll_file(
        str(tmp_path / "unlinked_store_a" / "enrollment.json"),
        seat="seat-a",
        dialect="claude",
        file_path=str(u_alias),
        allowed_roots=[str(a_root)],
        box_enrolled_inodes=u_inodes,
    ) == (False, "duplicate_inode_across_seats", None)


if __name__ == "__main__":
    extra = ["--noconftest", "-p", "no:cacheprovider"] if "MESHY_TEMPLATE_DSN" not in os.environ else []
    raise SystemExit(pytest.main(extra + [__file__]))


def _app_tree(root: Path, *, uploader: bool, marker: str = "DEPLOYED") -> Path:
    """A minimal app_root: apps/context/validation.py (validate_context, MARKER), and with `uploader` a
    tools/transcript_cards package whose context_upload reports which apps and sibling module it loaded."""
    (root / "apps" / "context").mkdir(parents=True)
    (root / "apps" / "__init__.py").write_text("", encoding="utf-8")
    (root / "apps" / "context" / "__init__.py").write_text("", encoding="utf-8")
    (root / "apps" / "context" / "validation.py").write_text(
        "MARKER = %r\ndef validate_context(*a, **k):\n    return None\n" % marker, encoding="utf-8")
    if uploader:
        tc = root / "tools" / "transcript_cards"
        tc.mkdir(parents=True)
        (tc / "sibling.py").write_text("MARKER = %r\n" % marker, encoding="utf-8")
        (tc / "context_upload.py").write_text(
            "import json, sys\n"
            "from apps.context import validation\n"
            "from tools.transcript_cards import sibling\n"
            "if validation.MARKER != 'DEPLOYED' or sibling.MARKER != 'DEPLOYED':\n"
            "    print(json.dumps({'status': 'conflict', 'conflicts': [{'code': 'loaded ' + validation.MARKER + '/' + sibling.MARKER}]}))\n"
            "    sys.exit(4)\n"
            "print(json.dumps({'status': 'drained', 'batches_committed': 0, 'items_committed': 0}))\n",
            encoding="utf-8")
    return root


def _old_full_client(root: Path) -> Path:
    """An older full checkout used as `client`: its own apps/ and a tools/transcript_cards/ (no __init__.py, so
    `tools` is a namespace package) with older siblings and no context_upload, as HQD's clients were on 29 Oct."""
    (root / "apps" / "context").mkdir(parents=True)
    (root / "apps" / "__init__.py").write_text("", encoding="utf-8")
    (root / "apps" / "context" / "__init__.py").write_text("", encoding="utf-8")
    (root / "apps" / "context" / "validation.py").write_text(
        "MARKER = 'OLD'\ndef validate_context(*a, **k):\n    return None\n", encoding="utf-8")
    (root / "tools" / "transcript_cards").mkdir(parents=True)
    (root / "tools" / "transcript_cards" / "sibling.py").write_text("MARKER = 'OLD'\n", encoding="utf-8")
    return root


def _pass(ident, tmp_path, statuses, store="st", runner=None):
    rc_ctrl = cw.RateController(
        probe_fn=lambda _u: True,
        now_fn=lambda: datetime.datetime(2026, 10, 29, 14, 0, tzinfo=datetime.timezone.utc),
    )
    kw = {"runner": runner} if runner is not None else {}
    return cw.run_seat_pass(
        ident,
        store_dir=str(tmp_path / store),
        rate_controller=rc_ctrl,
        box_enrolled_inodes={},
        quarantined_seats=set(),
        missing_driver_logged=set(),
        set_status=lambda n, **k: statuses.setdefault(n, {}).update(k),
        note=lambda *_a: None,
        **kw,
    )


def test_an_old_full_client_cannot_shadow_the_deployed_uploader(tmp_path, monkeypatch):
    """HQD, 29 Oct: the worker started the uploader in `client`, an older full checkout, so `python -m` took its old
    apps/ and old tools/transcript_cards siblings ahead of PYTHONPATH=app_root (exit 4, then rc=1). A real child,
    started the way the worker starts it, now loads every module from the deployed app_root."""
    ident = _make_ident(tmp_path, "shadowed", "claude")
    ident["client"] = str(_old_full_client(tmp_path / "old_client"))
    ident["context"]["app_root"] = str(_app_tree(tmp_path / "deployed", uploader=True))
    pin = Path(ident["context"]["allowed_roots"][0]) / "s.jsonl"
    pin.write_text('{"type":"user"}\n', encoding="utf-8")
    ident["context"]["pins"] = [str(pin)]
    statuses: dict[str, dict] = {}
    assert cw.resolve_launch_dir(ident, str(tmp_path / "deployed")) == (str(tmp_path / "deployed"), None)
    r = _pass(ident, tmp_path, statuses)
    assert r["outcome"] == "ok", statuses
    assert statuses["shadowed"]["context_worker"] == "ok"
    # Control: started in the old client, as before the fix, the same child loads OLD modules and reports a conflict.
    monkeypatch.setattr(cw, "resolve_launch_dir", lambda ident_, app_root: (ident_["client"], None))
    control: dict[str, dict] = {}
    r_old = _pass(ident, tmp_path, control, store="st_control")
    assert r_old["outcome"] == "conflict", control


def test_a_client_only_install_whose_client_also_holds_apps_is_refused_before_any_child(tmp_path):
    ident = _make_ident(tmp_path, "client-apps", "claude")
    ident["client"] = str(_old_full_client(tmp_path / "old_client"))
    ident["context"]["app_root"] = str(_app_tree(tmp_path / "app_only", uploader=False))
    pin = Path(ident["context"]["allowed_roots"][0]) / "s.jsonl"
    pin.write_text('{"type":"user"}\n', encoding="utf-8")
    ident["context"]["pins"] = [str(pin)]
    statuses: dict[str, dict] = {}
    spawned: list = []
    r = _pass(ident, tmp_path, statuses, runner=lambda argv, cwd, timeout, env=None: spawned.append(argv) or (0, "{}", ""))
    assert r["outcome"] == "config_error"
    assert statuses["client-apps"]["context_error"] == "client_shadows_app_root"
    assert spawned == []


def test_context_f3_flag_forwarded_to_uploader_only_for_strict_boolean_true(tmp_path):
    ident = _make_ident(tmp_path, "ag-f3", "antigravity")
    conv_dir = Path(ident["context"]["allowed_roots"][0]) / "21599c0d-70d0-4d25-8ff1-dbe7d9d67763" / ".system_generated" / "logs"
    conv_dir.mkdir(parents=True, exist_ok=True)
    pin = conv_dir / "transcript_full.jsonl"
    pin.write_text('{"step_index":0,"source":"USER_EXPLICIT","type":"USER_INPUT","created_at":"2026-10-29T12:00:00Z","content":"hi"}\n', encoding="utf-8")
    ident["context"]["pins"] = [str(pin)]
    for bad_val in (None, False, "true", "false", 1, ["true"]):
        ident["context"].pop("f3", None) if bad_val is None else ident["context"].__setitem__("f3", bad_val)
        argv_off = cw.build_upload_argv(ident, store_dir=str(tmp_path / "st"), seat="antigravity",
                                        dialect="antigravity", sources=[str(pin)], max_batches=1)
        assert "--f3" not in argv_off, f"bad_val={bad_val!r} unexpectedly forwarded --f3"
    ident["context"]["f3"] = True
    seen, statuses = [], {}
    r = _pass(ident, tmp_path, statuses, store="st_f3_on",
              runner=lambda argv, cwd, timeout, env=None: seen.append(list(argv)) or (
                  0, '{"status":"drained","batches_committed":0,"items_committed":0,"context_held":[]}\n', ""))
    assert r["outcome"] == "ok" and len(seen) == 1 and "--f3" in seen[0] and seen[0][-1] == "--json"


def test_store_full_conflict_phrase_and_unreachable_reason_are_surfaced_without_paths(tmp_path):
    ident = _make_ident(tmp_path, "ag-diag", "codex")
    pin = Path(ident["context"]["allowed_roots"][0]) / "rollout.jsonl"
    pin.write_text('{"type":"session_meta"}\n', encoding="utf-8")
    ident["context"]["pins"] = [str(pin)]

    assert cw.sanitize_conflict_codes(
        [{"src": None, "code": "store: private capture store reached configured byte limit"}]
    ) == ["store: private capture store reached configured byte limit"]
    assert cw.sanitize_unreachable_reason("StoreBusy") == "StoreBusy"
    assert cw.sanitize_unreachable_reason("C:/Users/secret/path") == "other"
    assert cw.sanitize_unreachable_reason("../escape") == "other"
    assert cw.sanitize_unreachable_reason(None) is None

    statuses: dict[str, dict] = {}
    notes: list[str] = []
    seat_notes: dict = {}
    rc_ctrl = cw.RateController()
    box_inodes: dict = {}
    quarantined: set[str] = set()
    missing_logged: set[str] = set()
    responses = [
        (5, json.dumps({"status": "unreachable", "reason": "StoreBusy", "context_held": []}), ""),
        (5, json.dumps({"status": "unreachable", "reason": "StoreBusy", "context_held": []}), ""),
        (0, json.dumps({"status": "drained", "batches_committed": 0, "items_committed": 0, "context_held": []}), ""),
        (5, json.dumps({"status": "unreachable", "reason": "C:/private/file", "context_held": []}), ""),
        (4, json.dumps({"status": "conflict", "conflicts": [
            {"src": None, "code": "store: private capture store reached configured byte limit"}]}), ""),
    ]

    def run_one():
        rc, out, err = responses.pop(0)
        return cw.run_seat_pass(
            ident,
            store_dir=str(tmp_path / "st_diag"),
            rate_controller=rc_ctrl,
            box_enrolled_inodes=box_inodes,
            quarantined_seats=quarantined,
            missing_driver_logged=missing_logged,
            set_status=lambda n, **kw: statuses.setdefault(n, {}).update(kw),
            note=notes.append,
            runner=lambda argv, cwd, timeout, env=None: (rc, out, err),
            seat_notes=seat_notes,
        )

    assert run_one()["outcome"] == "unreachable"
    assert statuses["ag-diag"]["context_error"] == "StoreBusy"
    assert sum("exited 5 (unreachable: StoreBusy)" in n for n in notes) == 1
    # Duplicate consecutive reason does not spam postbox.err.log
    assert run_one()["outcome"] == "unreachable"
    assert sum("exited 5 (unreachable: StoreBusy)" in n for n in notes) == 1
    # Recovery clears context_error and resets unreachable note state
    assert run_one()["outcome"] == "ok"
    assert statuses["ag-diag"]["context_error"] is None
    # Path-bearing reason is scrubbed to 'other'
    assert run_one()["outcome"] == "unreachable"
    assert statuses["ag-diag"]["context_error"] == "other"
    assert any("exited 5 (unreachable: other)" in n for n in notes)
    assert not any("private" in n for n in notes)
    # StoreFull conflict quarantines with exact lowercase phrase
    assert run_one()["outcome"] == "conflict"
    assert statuses["ag-diag"]["context_conflict_codes"] == ["store: private capture store reached configured byte limit"]
    assert "ag-diag" in quarantined


# ---- A deleted transcript is not a conflict (4 Nov 2026) ---------------------------------------------------------
# Claude Code deletes old session transcripts at startup. Any enrolled file gone used to return no sources and
# file_identity_changed (or outside_allowed_roots, when the session's directory went too), which quarantined the seat.

def _enrolled_files(tmp_path):
    root = tmp_path / "root"
    (root / "sess" / "subagents").mkdir(parents=True)
    old, sub, live = root / "old.jsonl", root / "sess" / "subagents" / "agent-1.jsonl", root / "live.jsonl"
    ledger = str(tmp_path / "store" / "enrollment.json")
    for path in (old, sub, live):
        path.write_text('{"type":"user"}\n', encoding="utf-8")
        ok, reason, _ = cw.enroll_file(ledger, seat="s", dialect="claude", file_path=str(path), allowed_roots=[str(root)])
        assert ok, reason
    return root, ledger, old, sub, live


def _verified(ledger, roots):
    return cw.verified_enrolled_sources(ledger, seat="s", dialect="claude", allowed_roots=roots)


def test_a_deleted_enrolled_transcript_is_left_out_not_a_conflict(tmp_path):
    root, ledger, old, sub, live = _enrolled_files(tmp_path)
    sources, error = _verified(ledger, [str(root)])
    assert error is None and len(sources) == 3, "control: all three verify while they exist"
    old.unlink()
    sources, error = _verified(ledger, [str(root)])
    assert error is None and sorted(sources) == sorted([os.path.abspath(str(sub)), os.path.abspath(str(live))])


def test_a_deleted_session_directory_is_left_out_too(tmp_path):
    root, ledger, old, sub, live = _enrolled_files(tmp_path)
    shutil.rmtree(root / "sess")
    sources, error = _verified(ledger, [str(root)])
    assert error is None and sorted(sources) == sorted([os.path.abspath(str(old)), os.path.abspath(str(live))])


def test_a_root_that_has_gone_stays_a_refusal(tmp_path):
    root, ledger, _old, _sub, _live = _enrolled_files(tmp_path)
    shutil.rmtree(root)
    assert _verified(ledger, [str(root)]) == ([], "outside_allowed_roots")


def test_a_file_gone_outside_the_allowed_roots_stays_a_refusal(tmp_path):
    """All of them gone, so the refusal cannot come from a file that still exists."""
    root, ledger, _old, _sub, _live = _enrolled_files(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    shutil.rmtree(root)
    assert _verified(ledger, [str(other)]) == ([], "outside_allowed_roots")


def test_a_stat_failure_that_is_not_missing_stays_a_conflict(tmp_path, monkeypatch):
    root, ledger, old, _sub, _live = _enrolled_files(tmp_path)
    real_lstat = cw.os.lstat

    def refused(path, *args, **kwargs):
        if os.path.abspath(str(path)) == os.path.abspath(str(old)):
            raise PermissionError(13, "denied")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(cw.os, "lstat", refused)
    assert _verified(ledger, [str(root)]) == ([], "file_identity_changed")


def test_a_file_in_place_of_a_directory_stays_a_refusal(tmp_path):
    """The directory above the transcript became a file: that is not an absence, and it must not read as one."""
    root, ledger, _old, _sub, _live = _enrolled_files(tmp_path)
    shutil.rmtree(root / "sess")
    (root / "sess").write_text("not a directory", encoding="utf-8")
    assert _verified(ledger, [str(root)]) == ([], "outside_allowed_roots")


def test_a_directory_swapped_for_a_link_stays_a_refusal(tmp_path):
    """a reviewer on #515: os.lstat follows the directories above the file, so a junction to a place without it says
    "no such file" too. The walk from the root must stop at the link and leave the refusal standing."""
    root, ledger, _old, _sub, _live = _enrolled_files(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    shutil.rmtree(root / "sess")
    link = root / "sess"
    if os.name == "nt":
        proc = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(elsewhere)], capture_output=True,
                              text=True, stdin=subprocess.DEVNULL)
        assert proc.returncode == 0, proc.stderr
    else:
        os.symlink(elsewhere, link)
    try:
        sources, error = _verified(ledger, [str(root)])
        assert sources == [] and error == "outside_allowed_roots"
    finally:
        os.rmdir(link) if os.name == "nt" else os.unlink(link)


def test_a_replaced_transcript_stays_a_conflict(tmp_path):
    root, ledger, old, _sub, _live = _enrolled_files(tmp_path)
    second = tmp_path / "second.jsonl"      # made while `old` exists, so it cannot have old's inode
    second.write_text('{"type":"user"}\n', encoding="utf-8")
    os.replace(second, old)
    assert _verified(ledger, [str(root)]) == ([], "file_identity_changed")


def _pass_for(ident, store_dir, statuses, notes, argvs):
    drained = json.dumps({"status": "drained", "seat": ident["name"], "passes": 1, "batches_committed": 0,
                          "items_committed": 0, "last_manifest_key": None, "context_held": [],
                          "accounting": {}, "conflicts": []})

    def runner(argv, _cwd, _timeout):
        argvs.append(argv)
        return 0, drained, ""

    return cw.run_seat_pass(
        ident, store_dir=store_dir,
        rate_controller=cw.RateController(
            probe_fn=lambda _u: True,
            now_fn=lambda: datetime.datetime(2026, 10, 26, 12, 0, 0, tzinfo=datetime.timezone.utc),
            mono_fn=lambda: 10.0),
        box_enrolled_inodes={}, quarantined_seats=set(), missing_driver_logged=set(),
        set_status=lambda name, **kw: statuses.setdefault(name, {}).update(kw), note=notes.append, runner=runner)


def _sources_in(argv):
    return [argv[i + 1] for i, arg in enumerate(argv) if arg == "--source"]


def test_a_deleted_pin_that_was_enrolled_no_longer_stops_the_seat(tmp_path):
    ident = _make_ident(tmp_path, "kxxx", "claude")
    root = Path(ident["context"]["allowed_roots"][0])
    gone, kept = root / "gone.jsonl", root / "kept.jsonl"
    for path in (gone, kept):
        path.write_text('{"type":"user"}\n', encoding="utf-8")
    ident["context"]["pins"] = [str(gone), str(kept)]
    ident["context"]["namespace"] = "shared/transcript-context"
    store_dir = str(tmp_path / "ctx_store" / "kxxx")
    statuses, notes, argvs = {}, [], []

    first = _pass_for(ident, store_dir, statuses, notes, argvs)
    assert first["outcome"] != "config_error", statuses
    assert sorted(_sources_in(argvs[-1])) == sorted([os.path.abspath(str(gone)), os.path.abspath(str(kept))])

    gone.unlink()
    second = _pass_for(ident, store_dir, statuses, notes, argvs)
    assert second["outcome"] not in ("config_error", "conflict"), (second, statuses)
    assert _sources_in(argvs[-1]) == [os.path.abspath(str(kept))]


def test_a_deleted_antigravity_pin_stored_under_its_full_sibling_is_gone_too(tmp_path):
    """a reviewer on #515: the pin names transcript.jsonl, the ledger holds transcript_full.jsonl, and with the
    directory gone upgrade_antigravity_path can no longer map one to the other."""
    root = tmp_path / "root"
    conv = root / "conv"
    conv.mkdir(parents=True)
    compact, full = conv / "transcript.jsonl", conv / "transcript_full.jsonl"
    compact.write_text('{"type":"user"}\n', encoding="utf-8")
    full.write_text('{"type":"user"}\n', encoding="utf-8")
    ledger = str(tmp_path / "store" / "enrollment.json")
    ok, reason, _ = cw.enroll_file(ledger, seat="s", dialect="antigravity", file_path=str(compact), allowed_roots=[str(root)])
    assert ok, reason
    assert not cw._enrolled_pin_is_gone(ledger, str(compact), "s", "antigravity", [str(root)]), "control: it still exists"
    shutil.rmtree(conv)
    assert cw._enrolled_pin_is_gone(ledger, str(compact), "s", "antigravity", [str(root)])


def test_a_pin_of_another_seat_or_dialect_is_not_this_seats_to_skip(tmp_path):
    root, ledger, old, _sub, _live = _enrolled_files(tmp_path)
    old.unlink()
    assert cw._enrolled_pin_is_gone(ledger, str(old), "s", "claude", [str(root)]), "control: its own seat and dialect"
    assert not cw._enrolled_pin_is_gone(ledger, str(old), "another", "claude", [str(root)])
    assert not cw._enrolled_pin_is_gone(ledger, str(old), "s", "codex", [str(root)])


def test_a_pin_never_enrolled_and_missing_stays_a_config_error(tmp_path):
    ident = _make_ident(tmp_path, "kxxx", "claude")
    root = Path(ident["context"]["allowed_roots"][0])
    ident["context"]["pins"] = [str(root / "typo.jsonl")]
    ident["context"]["namespace"] = "shared/transcript-context"
    statuses, notes, argvs = {}, [], []
    outcome = _pass_for(ident, str(tmp_path / "ctx_store" / "kxxx"), statuses, notes, argvs)
    assert outcome["outcome"] == "config_error" and statuses["kxxx"]["context_error"] == "missing_file"
    assert argvs == []
