"""Dispatch is queue evidence; only later session text or a covering own read settles it."""
import datetime as dt
import json
import os

import pytest

from tools.postbox import unsurfaced as u

START = dt.datetime(2026, 11, 1, 10, 21, 20, tzinfo=dt.timezone.utc)
KEY = "msg/1793528130216-kxxx-33d6ae"


def append(path, record):
    with path.open("a", encoding="utf-8") as out:
        out.write(json.dumps(record) + "\n")


class Box:
    def __init__(self, tmp_path, *, historical=False):
        self.root = tmp_path
        self.spool = tmp_path / "spool"
        self.dispatch = tmp_path / "spool.codex.offset.dispatch.jsonl"
        self.skips = tmp_path / "spool.codex.offset.skips.jsonl"
        self.transcript = tmp_path / "session.jsonl"
        self.checkpoint = tmp_path / "observer.json"
        self.state = tmp_path / "state.json"
        self.state.write_text(json.dumps({"peer_id": "own-peer"}))
        self.ident = {"name": "seat", "log": str(self.spool), "state": str(self.state)}
        self.transcript.write_text("old history is deliberately not parsed\n")
        self.dispatch.write_text("")
        if historical:
            self.hand(at=START-dt.timedelta(days=20))
        self.tracker = u.Tracker(str(self.checkpoint))
        self.status = []
        assert self.tick(START)["unsurfaced"] == 0

    def hand(self, key=KEY, at=None, version=1, identity="seat"):
        append(self.dispatch, {"at": (at or START + dt.timedelta(seconds=4)).isoformat(),
               "identity": identity, "consumer": "codex:session", "from": 0, "to": 100,
               "events": [{"event": "message", "key": key, "version": version}]})

    def surface(self, at, key=KEY, shape="codex"):
        row = ({"type": "event_msg", "payload": {"type": "user_message", "message": key}}
               if shape == "codex" else {"type": "user", "message": {"role": "user", "content": key}})
        append(self.transcript, {"timestamp": at.isoformat(), **row})

    def read(self, key=KEY, version=1, peer="own-peer"):
        # The read needn't follow dispatch: an already-read notice doesn't become lost.
        path = self.root / "state.json.read.json"
        temp = self.root / "next-read.json"
        temp.write_text(json.dumps({"schema_version": 1, "peer_id": peer,
            "reads": [{"key": key, "version": version, "at": START.isoformat()}]}))
        os.replace(temp, path)

    def tick(self, at, state="closed"):
        return self.tracker.update(self.ident, [str(self.transcript)], now=at, state=state,
                                   status=lambda **kw: self.status.append(kw))


def test_33d6ae_delayed_timeline_is_one_then_zero_only_when_it_lands(tmp_path):
    b = Box(tmp_path)
    dispatch = START + dt.timedelta(seconds=4)
    b.hand(at=dispatch)
    assert b.tick(dispatch)["unsurfaced"] == 0
    # The turn at 10:42:10 did not carry the notice. It is held/open, not settled.
    assert b.tick(START.replace(hour=10, minute=42, second=10), "open")["unsurfaced"] == 1
    assert KEY in b.tracker.pending
    before = START.replace(hour=11, minute=7, second=7)
    assert b.tick(before) == {"unsurfaced": 1, "unsurfaced_oldest_at": dispatch.isoformat(), "unsurfaced_since": START.isoformat()}
    landed = START.replace(hour=11, minute=7, second=8)
    b.surface(landed)
    assert b.tick(landed) == {"unsurfaced": 0, "unsurfaced_oldest_at": None, "unsurfaced_since": START.isoformat()}
    assert KEY not in b.tracker.pending


def test_pxx_shaped_27_never_settle_by_the_passage_of_time(tmp_path):
    b = Box(tmp_path)
    for i in range(27):
        b.hand(f"msg/pxx-{i}", START + dt.timedelta(seconds=i + 1))
    later = START + dt.timedelta(hours=2)
    assert b.tick(later)["unsurfaced"] == 27
    assert b.tick(later + dt.timedelta(hours=10))["unsurfaced"] == 27
    assert len(b.tracker.pending) == 27


@pytest.mark.parametrize("shape", ["codex", "claude"])
def test_both_session_shapes_settle_growth_after_dispatch(tmp_path, shape):
    b = Box(tmp_path); b.hand()
    later = START + dt.timedelta(seconds=400)
    assert b.tick(later)["unsurfaced"] == 1
    b.surface(later, shape=shape)
    assert b.tick(later)["unsurfaced"] == 0


def test_exact_300_second_bound_and_open_turn_deferral(tmp_path):
    b = Box(tmp_path); dispatch = START + dt.timedelta(seconds=4); b.hand(at=dispatch)
    assert b.tick(dispatch + dt.timedelta(seconds=299))["unsurfaced"] == 0
    assert b.tick(dispatch + dt.timedelta(seconds=300))["unsurfaced"] == 1
    assert b.tick(dispatch + dt.timedelta(seconds=400), "open")["unsurfaced"] == 1
    assert len(b.tracker.pending) == 1
    assert b.tick(dispatch + dt.timedelta(seconds=401))["unsurfaced"] == 1


def test_covering_lookup_is_own_peer_bound_and_old_versions_do_not_settle(tmp_path):
    b = Box(tmp_path); b.hand(version=2); later = START + dt.timedelta(seconds=400)
    b.read(version=2, peer="someone-else")
    assert b.tick(later)["unsurfaced"] == 1
    b.read(version=1)
    assert b.tick(later)["unsurfaced"] == 1
    b.read(version=2)
    assert b.tick(later)["unsurfaced"] == 0


def test_damaged_lookup_schema_is_incomplete_evidence(tmp_path):
    b = Box(tmp_path); b.hand(); later = START + dt.timedelta(seconds=400)
    (tmp_path / "state.json.read.json").write_text(json.dumps({"schema_version": 9, "reads": []}))
    assert b.tick(later)["unsurfaced"] == u.INCOMPLETE


def test_already_read_skip_requires_reason_key_and_covering_version(tmp_path):
    b = Box(tmp_path); b.hand(version=2); later = START + dt.timedelta(seconds=400)
    for record in ({"key": KEY, "version": 2, "reason": "held"},
                   {"key": "msg/another", "version": 2, "reason": "already_read"},
                   {"key": KEY, "version": 1, "reason": "already_read"}):
        append(b.skips, record)
    assert b.tick(later)["unsurfaced"] == 1
    append(b.skips, {"key": KEY, "version": 2, "reason": "already_read"})
    assert b.tick(later)["unsurfaced"] == 0


@pytest.mark.parametrize("replace", [False, True], ids=["truncate", "replace"])
def test_reset_is_incomplete_not_an_invented_loss_or_settlement(tmp_path, replace):
    b = Box(tmp_path); b.hand(); later = START + dt.timedelta(seconds=400)
    assert b.tick(later)["unsurfaced"] == 1
    if replace:
        old = tmp_path / "old-session"
        os.replace(b.transcript, old)
    b.transcript.write_text("")
    assert b.tick(later)["unsurfaced"] == u.INCOMPLETE
    assert KEY in b.tracker.pending
    assert b.tracker.pending[KEY]["uncertain"] is True
    b.read()
    assert b.tick(later)["unsurfaced"] == 0


@pytest.mark.parametrize("stream", ["dispatch", "skips", "transcript"])
@pytest.mark.parametrize("keep_unread_row", [False, True], ids=["at-offset", "above-offset"])
def test_same_inode_shrink_cannot_hide_unread_evidence(tmp_path, monkeypatch, stream, keep_unread_row):
    b = Box(tmp_path)
    later = START + dt.timedelta(seconds=400)
    path = getattr(b, stream)
    if stream == "transcript":
        b.hand()  # Keep transcript growth eligible for the bounded read.
        control = {"timestamp": later.isoformat(), "payload": "unrelated"}
    elif stream == "dispatch":
        control = {"at": later.isoformat(), "identity": "seat", "events": []}
    else:
        path.touch()  # Box creates skip logs lazily; measure an existing empty file.
        control = {"key": KEY, "version": 1, "reason": "held"}
    baseline = path.stat().st_size
    append(path, control)
    first_end = path.stat().st_size
    append(path, control)
    second_end = path.stat().st_size
    if stream == "transcript":
        b.surface(later)
    elif stream == "dispatch":
        b.hand()
    else:
        append(path, {"key": KEY, "version": 1, "reason": "already_read"})
    monkeypatch.setattr(u, "TRANSCRIPT_BYTES_PER_PASS" if stream == "transcript"
                        else "LOG_BYTES_PER_PASS", first_end - baseline)
    assert b.tick(later)["unsurfaced"] == u.INCOMPLETE
    cursor = b.tracker.streams[str(path)]
    assert cursor["offset"] == first_end < cursor["size"]
    cut = second_end if keep_unread_row else first_end
    assert cursor["offset"] <= cut < cursor["size"]
    identity = (path.stat().st_dev, path.stat().st_ino)
    # Preserve the inode and a complete JSONL prefix: no parse/reset error can
    # accidentally kill the mutant in place of the missing-evidence assertion.
    with path.open("r+b") as handle:
        handle.truncate(cut)
    assert (path.stat().st_dev, path.stat().st_ino) == identity
    b.tracker = u.Tracker(str(b.checkpoint))  # The previous size survives restart.
    if stream == "transcript":
        b.read()  # Settling the known key must not hide the lost unread growth.
    observed = later + dt.timedelta(seconds=1)
    result = b.tick(observed)
    assert not b.tracker.pending
    assert result["unsurfaced"] == u.INCOMPLETE
    assert result["unsurfaced_since"] == START.isoformat()
    assert b.tracker.gap and b.tracker.last_fault_at == observed.isoformat()
    assert json.loads(b.checkpoint.read_text())["gap"] is True
    # A later fault-free pass may start a new, explicitly recorded window.
    recovered_at = observed + dt.timedelta(seconds=30)
    recovered = b.tick(recovered_at)
    assert recovered["unsurfaced"] == 0
    assert recovered["unsurfaced_since"] == recovered_at.isoformat()
    assert b.status[-1]["unsurfaced_last_issue"] == {
        "kind": "gap", "since": START.isoformat(), "rebased_at": recovered_at.isoformat()}


def test_overflow_keeps_newest_then_starts_explicit_new_baseline_after_settlement(tmp_path, monkeypatch):
    b = Box(tmp_path); monkeypatch.setattr(u, "MAX_PENDING", 2)
    for i in range(3):
        b.hand(f"msg/overflow-{i}", START + dt.timedelta(seconds=i + 1))
    later = START + dt.timedelta(seconds=400)
    assert b.tick(later)["unsurfaced"] == u.INCOMPLETE
    assert set(b.tracker.pending) == {"msg/overflow-1", "msg/overflow-2"}
    assert b.status[-1]["unsurfaced_overflow"] is True
    for key in list(b.tracker.pending):
        b.surface(later, key)
    recovered = b.tick(later)
    assert recovered["unsurfaced"] == 0
    assert recovered["unsurfaced_since"] == later.isoformat()
    assert b.status[-1]["unsurfaced_last_issue"] == {
        "kind": "overflow", "since": START.isoformat(), "rebased_at": later.isoformat()}
    assert not b.tracker.pending
    b.tracker = u.Tracker(str(b.checkpoint))
    assert b.tick(later)["unsurfaced_since"] == later.isoformat()
    assert b.status[-1]["unsurfaced_last_issue"]["kind"] == "overflow"


@pytest.mark.parametrize("capacity", [2, 2048])
def test_same_pass_overflow_and_covering_skips_cannot_hide_the_dropped_notice(tmp_path, monkeypatch, capacity):
    b = Box(tmp_path)
    monkeypatch.setattr(u, "MAX_PENDING", capacity)
    keys = [f"msg/f-{i}" for i in range(capacity + 1)]
    event = {"at": (START + dt.timedelta(seconds=4)).isoformat(), "identity": "seat",
             "events": [{"event": "message", "key": key, "version": 1} for key in keys]}
    # Compact but real JSONL keeps even the production-capacity flood in one pass.
    b.dispatch.write_text(json.dumps(event, separators=(",", ":")) + "\n", encoding="utf-8")
    b.skips.write_text("".join(json.dumps({"key": key, "version": 1, "reason": "already_read"},
                                        separators=(",", ":")) + "\n" for key in keys[1:]), encoding="utf-8")
    assert b.dispatch.stat().st_size + b.skips.stat().st_size < u.LOG_BYTES_PER_PASS
    later = START + dt.timedelta(seconds=400)
    result = b.tick(later)
    assert not b.tracker.pending  # The evicted oldest key has no settlement evidence.
    assert result["unsurfaced"] == u.INCOMPLETE
    assert result["unsurfaced_since"] == START.isoformat()
    assert b.status[-1]["unsurfaced_overflow"] is True
    assert b.status[-1]["unsurfaced_backlog"] is False
    # The loss is checkpointed before a later fault-free pass starts a new window.
    b.tracker = u.Tracker(str(b.checkpoint))
    recovered = b.tick(later + dt.timedelta(seconds=30))
    assert recovered["unsurfaced"] == 0
    assert recovered["unsurfaced_since"] == (later + dt.timedelta(seconds=30)).isoformat()
    assert b.status[-1]["unsurfaced_last_issue"]["kind"] == "overflow"


def test_saved_offsets_survive_restart_and_repeated_dispatch_keeps_first_age(tmp_path):
    b = Box(tmp_path); first = START + dt.timedelta(seconds=4); b.hand(at=first)
    later = START + dt.timedelta(seconds=400)
    assert b.tick(later)["unsurfaced"] == 1
    b.tracker = u.Tracker(str(b.checkpoint))
    b.hand(at=later)
    assert b.tick(later)["unsurfaced_oldest_at"] == first.isoformat()
    b.surface(later)
    assert b.tick(later)["unsurfaced"] == 0


def test_old_or_prefix_text_does_not_settle_a_later_exact_key(tmp_path):
    b = Box(tmp_path); b.hand(); later = START + dt.timedelta(seconds=400)
    b.surface(START, KEY)
    b.surface(later, KEY + "-another")
    assert b.tick(later)["unsurfaced"] == 1
    b.surface(later, KEY)
    assert b.tick(later)["unsurfaced"] == 0


def test_rotation_reuses_inode_cursor_instead_of_replaying_settled_notices(tmp_path):
    b = Box(tmp_path); b.hand(); later = START + dt.timedelta(seconds=400)
    b.surface(later)
    assert b.tick(later)["unsurfaced"] == 0
    os.replace(b.dispatch, str(b.dispatch) + ".1")
    b.dispatch.write_text("")
    assert b.tick(later)["unsurfaced"] == 0
    b.hand("msg/new", later)
    assert b.tick(later + dt.timedelta(seconds=300))["unsurfaced"] == 1


def test_cold_start_ignores_historical_dispatch_and_shows_durable_since(tmp_path):
    b = Box(tmp_path, historical=True)
    assert b.tick(START)["unsurfaced"] == 0
    assert not b.tracker.pending and not b.tracker.gap
    assert b.tracker.streams[str(b.dispatch)]["offset"] == b.dispatch.stat().st_size
    assert b.tick(START)["unsurfaced_since"] == START.isoformat()
    b.tracker = u.Tracker(str(b.checkpoint))
    assert b.tick(START+dt.timedelta(hours=10))["unsurfaced"] == 0
    assert b.tracker.since == START.isoformat()


def test_second_rotation_preserves_cursor_after_archive_names_shift(tmp_path):
    b = Box(tmp_path); b.hand()
    later = START+dt.timedelta(seconds=400)
    b.surface(later)
    assert b.tick(later)["unsurfaced"] == 0
    os.replace(b.dispatch, str(b.dispatch)+".1")
    b.dispatch.write_text("")
    assert b.tick(later)["unsurfaced"] == 0
    b.hand("msg/second-log", later)
    b.surface(later, "msg/second-log")
    assert b.tick(later)["unsurfaced"] == 0
    os.replace(str(b.dispatch)+".1", str(b.dispatch)+".2")
    os.replace(b.dispatch, str(b.dispatch)+".1")
    b.dispatch.write_text("")
    assert b.tick(later+dt.timedelta(seconds=300))["unsurfaced"] == 0
    b.hand("msg/third-log", later+dt.timedelta(seconds=301))
    assert b.tick(later+dt.timedelta(seconds=601))["unsurfaced"] == 1


def test_bounded_growth_and_dispatch_backlog_do_not_consume_surface_evidence_early(tmp_path, monkeypatch):
    b = Box(tmp_path); b.hand(); later = START + dt.timedelta(seconds=400)
    b.surface(later)
    monkeypatch.setattr(u, "LOG_BYTES_PER_PASS", 32)
    assert b.tick(later)["unsurfaced"] == u.INCOMPLETE
    assert b.tracker.streams[str(b.transcript)]["offset"] < b.transcript.stat().st_size
    monkeypatch.setattr(u, "LOG_BYTES_PER_PASS", 256 * 1024)
    assert b.tick(later)["unsurfaced"] == 0
    # A partial transcript row is retained at its boundary, and never yields zero.
    b.hand("msg/second", later)
    b.surface(later, "msg/second")
    monkeypatch.setattr(u, "TRANSCRIPT_BYTES_PER_PASS", 32)
    assert b.tick(later + dt.timedelta(seconds=300))["unsurfaced"] == u.INCOMPLETE
    monkeypatch.setattr(u, "TRANSCRIPT_BYTES_PER_PASS", 256 * 1024)
    assert b.tick(later + dt.timedelta(seconds=300))["unsurfaced"] == 0


def test_missing_unread_log_and_damaged_checkpoint_rebase_only_after_gap_is_reported(tmp_path):
    b = Box(tmp_path)
    # A retained incomplete row is pending coverage, not a successful observation.
    with b.dispatch.open("ab") as out:
        out.write(b'{"at":')
    assert b.tick(START)["unsurfaced"] == u.INCOMPLETE
    b.dispatch.unlink()
    assert b.tick(START)["unsurfaced"] == u.INCOMPLETE
    later = START + dt.timedelta(seconds=30)
    assert b.tick(later)["unsurfaced"] == 0
    assert b.tracker.since == later.isoformat()
    assert b.status[-1]["unsurfaced_last_issue"]["kind"] == "gap"
    b.checkpoint.write_text("not-json")
    b.tracker = u.Tracker(str(b.checkpoint))
    assert b.tick(START)["unsurfaced"] == u.INCOMPLETE
    assert b.tick(later)["unsurfaced"] == 0
    assert b.tracker.since == later.isoformat()


def test_empty_pending_map_skips_transcript_bytes_and_enrolled_inode_is_rechecked(tmp_path, monkeypatch):
    b = Box(tmp_path)
    used = []
    original = b.tracker._stream
    def record(path, budget, **kw):
        result = original(path, budget, **kw)
        if kw.get("transcript"):
            used.append(result[1])
        return result
    monkeypatch.setattr(b.tracker, "_stream", record)
    with b.transcript.open("a") as out:
        out.write("not parsed without pending keys" * 10000)
    assert b.tick(START)["unsurfaced"] == 0
    assert sum(used) == 0
    info = b.transcript.stat()
    with pytest.raises(ValueError, match="unsurfaced_source_identity"):
        b.tracker.update(b.ident, [str(b.transcript)], state="closed", now=START,
                         expected_inodes={str(b.transcript): (info.st_dev, info.st_ino + 1)})


def test_real_dispatch_shape_without_version_uses_canonical_legacy_version(tmp_path):
    b = Box(tmp_path)
    append(b.dispatch, {"identity": "seat", "at": (START+dt.timedelta(seconds=4)).isoformat(),
        "events": [{"created_at": START.isoformat(), "event": "message", "key": KEY,
                    "requires_ack": True, "role": "to", "rx_at": START.isoformat()}]})
    later = START+dt.timedelta(seconds=400)
    assert b.tick(later)["unsurfaced"] == 1
    assert b.tracker.pending[KEY]["version"] == 1
    assert not b.tracker.gap
    b.read()
    assert b.tick(later)["unsurfaced"] == 0


@pytest.mark.parametrize("version", [None, True, 0, -1, "1"])
def test_explicit_malformed_version_is_not_legacy_default(tmp_path, version):
    b = Box(tmp_path); b.hand(version=version)
    later = START+dt.timedelta(seconds=400)
    assert b.tick(later)["unsurfaced"] == u.INCOMPLETE
    assert not b.tracker.pending
    assert b.tick(later+dt.timedelta(seconds=30))["unsurfaced"] == 0
    assert b.tracker.last_issue["kind"] == "gap"


@pytest.mark.parametrize("birth_metadata", [True, False])
def test_new_session_after_since_preserves_27_and_reads_its_first_growth(tmp_path, monkeypatch, birth_metadata):
    from types import SimpleNamespace
    b = Box(tmp_path)
    for i in range(27):
        b.hand(f"msg/pxx-{i}", START+dt.timedelta(seconds=i+1))
    later = START+dt.timedelta(seconds=400)
    assert b.tick(later)["unsurfaced"] == 27
    path = tmp_path/"new-session.jsonl"
    append(path, {"timestamp": later.isoformat(), "type": "session_meta", "payload": {"id": "new"}})
    original = u.delivery_bank.open_regular_read
    def opened(source, **kwargs):
        fd, info = original(source, **kwargs)
        if str(source) == str(path):
            info = SimpleNamespace(st_dev=info.st_dev, st_ino=info.st_ino, st_size=info.st_size,
                                   st_birthtime=later.timestamp() if birth_metadata else None)
        return fd, info
    monkeypatch.setattr(u.delivery_bank, "open_regular_read", opened)
    sources = [str(b.transcript), str(path)]
    def tick():
        return b.tracker.update(b.ident, sources, state="closed", now=later)
    assert tick()["unsurfaced"] == 27
    assert not b.tracker.gap and not any(i["uncertain"] for i in b.tracker.pending.values())
    append(path, {"timestamp": later.isoformat(), "type": "user", "message": "msg/pxx-0"})
    assert tick()["unsurfaced"] == 26


def test_malformed_row_gap_can_end_only_in_a_new_coverage_window(tmp_path):
    b = Box(tmp_path)
    with b.dispatch.open("a") as out:
        out.write("malformed\n")
    later = START+dt.timedelta(seconds=30)
    assert b.tick(later)["unsurfaced"] == u.INCOMPLETE
    assert b.tracker.since == START.isoformat()
    fresh = later+dt.timedelta(seconds=30)
    assert b.tick(fresh)["unsurfaced"] == 0
    assert b.tracker.since == fresh.isoformat()
    assert b.tracker.last_issue["since"] == START.isoformat()


def test_unreadable_ledger_prevents_rebase_until_the_evidence_is_healthy(tmp_path):
    b = Box(tmp_path)
    (tmp_path/"state.json.read.json").write_text('{"schema_version":9,"reads":[]}')
    later = START+dt.timedelta(seconds=30)
    assert b.tick(later)["unsurfaced"] == u.INCOMPLETE
    assert b.tick(later+dt.timedelta(seconds=30))["unsurfaced"] == u.INCOMPLETE
    assert b.tracker.since == START.isoformat()
    b.read("msg/another")
    fresh = later+dt.timedelta(seconds=60)
    assert b.tick(fresh)["unsurfaced"] == 0
    assert b.tracker.since == fresh.isoformat()


@pytest.mark.parametrize("fault", ["malformed", "torn", "reset", "overflow", "late-file"])
def test_quiet_rebase_retains_missing_notice_and_original_age(tmp_path, monkeypatch, fault):
    from types import SimpleNamespace
    b = Box(tmp_path); b.hand()
    later = START+dt.timedelta(seconds=400)
    assert b.tick(later)["unsurfaced"] == 1
    if fault == "malformed":
        with b.dispatch.open("a") as out:
            out.write("broken\n")
    elif fault == "torn":
        with b.dispatch.open("a") as out:
            out.write('{"events":')
    elif fault == "reset":
        b.transcript.write_text("")
    elif fault == "overflow":
        monkeypatch.setattr(u, "MAX_PENDING", 1)
        b.hand("msg/newest", START+dt.timedelta(seconds=5))
    else:
        path = tmp_path/"late.jsonl"
        append(path, {"timestamp": (START-dt.timedelta(days=1)).isoformat()})
        original = u.delivery_bank.open_regular_read
        def opened(source, **kwargs):
            fd, info = original(source, **kwargs)
            if str(source) == str(path):
                info = SimpleNamespace(st_dev=info.st_dev, st_ino=info.st_ino, st_size=info.st_size,
                                       st_birthtime=(START-dt.timedelta(days=1)).timestamp())
            return fd, info
        monkeypatch.setattr(u.delivery_bank, "open_regular_read", opened)
        b.tracker.update(b.ident, [str(b.transcript), str(path)], state="closed", now=later)
    assert b.tick(later)["unsurfaced"] == u.INCOMPLETE
    retained = dict(b.tracker.pending)
    assert len(retained) == 1
    assert b.tick(later+dt.timedelta(seconds=1799))["unsurfaced"] == u.INCOMPLETE
    b.tracker = u.Tracker(str(b.checkpoint))
    recovered = b.tick(later+dt.timedelta(seconds=1800))
    assert recovered["unsurfaced"] == 1
    assert set(b.tracker.pending) == set(retained)
    assert recovered["unsurfaced_oldest_at"] == next(iter(retained.values()))["at"]
    assert recovered["unsurfaced_since"] == (later+dt.timedelta(seconds=1800)).isoformat()
    assert b.tracker.last_issue is not None
    b.read(next(iter(retained)))
    assert b.tick(later+dt.timedelta(seconds=1801))["unsurfaced"] == 0


def test_new_fault_restarts_quiet_clock_and_persistent_fault_never_claims_recovery(tmp_path):
    b = Box(tmp_path); b.hand()
    later = START+dt.timedelta(seconds=400)
    b.transcript.write_text("")
    assert b.tick(later)["unsurfaced"] == u.INCOMPLETE
    second = later+dt.timedelta(seconds=1700)
    with b.dispatch.open("a") as out:
        out.write("broken\n")
    assert b.tick(second)["unsurfaced"] == u.INCOMPLETE
    assert b.tick(later+dt.timedelta(seconds=1800))["unsurfaced"] == u.INCOMPLETE
    assert b.tick(second+dt.timedelta(seconds=1800))["unsurfaced"] == 1


def test_naturally_completed_backlog_does_not_lend_its_quiet_clock_to_a_new_tail(tmp_path):
    b = Box(tmp_path); b.hand()
    later = START+dt.timedelta(seconds=400)
    assert b.tick(later)["unsurfaced"] == 1
    row = json.dumps({"identity": "seat", "at": later.isoformat(), "events": []})
    with b.dispatch.open("a") as out:
        out.write(row[:-1])
    assert b.tick(later)["unsurfaced"] == u.INCOMPLETE
    with b.dispatch.open("a") as out:
        out.write(row[-1:]+"\n")
    assert b.tick(later+dt.timedelta(seconds=30))["unsurfaced"] == 1
    fresh = later+dt.timedelta(hours=6)
    with b.dispatch.open("a") as out:
        out.write('{"events":')
    assert b.tick(fresh)["unsurfaced"] == u.INCOMPLETE
    assert b.tick(fresh+dt.timedelta(seconds=1799))["unsurfaced"] == u.INCOMPLETE
    assert b.tick(fresh+dt.timedelta(seconds=1800))["unsurfaced"] == 1
