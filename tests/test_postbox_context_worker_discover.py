"""context.discover in the postbox context worker (goal 27 Oct, item 1): new session files are enrolled on their own.

Every new Claude or Codex session writes a NEW transcript file. A fixed context.pins list therefore stops uploading
at the seat's next session, silently, as oxxxxxx's card feed did when its --files list missed a session for four
days. These tests pin the discovery rules; tools/mutants/lists/postbox_context_worker_discover.py breaks each one.
"""
from __future__ import annotations

import datetime
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PB_DIR = ROOT / "tools" / "postbox"
if str(PB_DIR) not in sys.path:
    sys.path.insert(0, str(PB_DIR))


def _load(name: str):
    spec = importlib.util.spec_from_file_location("pb_ctx_disc_" + name, PB_DIR / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cw = _load("context_worker")
NOW = 1_800_000_000.0                      # the wall clock the tests hand the worker


def _touch(path: Path, age_h: float = 0.0, text: str = '{"type":"user"}\n') -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.utime(path, (NOW - age_h * 3600, NOW - age_h * 3600))
    return path


def _codex_meta(path: Path, *, originator="codex-tui", cwd="C:/a/b", source="cli", age_h=0.0) -> Path:
    return _touch(path, age_h=age_h, text=json.dumps({"type": "session_meta", "payload": {
        "originator": originator, "cwd": cwd, "source": source}}) + "\n")


def _ident(tmp_path: Path, name: str = "kxxx", discover=None, allowed=None) -> dict:
    root = tmp_path / "projects"
    root.mkdir(exist_ok=True)
    state = tmp_path / (name + "-state.json")
    state.write_text("{}", encoding="utf-8")
    client = tmp_path / "client"
    client.mkdir(exist_ok=True)
    ctx = {"enabled": True, "seat": name, "dialect": "claude", "app_root": str(ROOT),
           "allowed_roots": [str(allowed or root)], "pins": [], "namespace": "shared/transcript-context"}
    if discover is not False:
        ctx["discover"] = discover if discover is not None else [
            {"root": str(root), "pattern": "*.jsonl", "recursive": True, "max_age_h": 48}]
    return {"name": name, "cli": "claude", "enabled": True, "python": sys.executable, "client": str(client),
            "state": str(state), "base_url": "http://127.0.0.1:6310", "context": ctx}


class Run:
    """One worker identity driven pass by pass, with a fake upload child and a controllable monotonic clock."""

    def __init__(self, tmp_path: Path, ident: dict, box=None):
        self.ident, self.box = ident, box if box is not None else {}
        self.store = str(tmp_path / "ctx_store" / ident["name"])
        self.mono, self.state, self.status, self.quarantined = 1000.0, {}, {}, set()
        self.notes: list[str] = []
        self.ctrl = cw.RateController(
            probe_fn=lambda _u: True, mono_fn=lambda: self.mono,
            now_fn=lambda: datetime.datetime(2026, 10, 27, 12, 0, tzinfo=datetime.timezone.utc))

    def pass_(self, advance_s: float = 0.0, wall_s: float = NOW, extra: dict | None = None, **kw):
        self.mono += advance_s
        drained = json.dumps({"status": "drained", "context_held": [], "items_committed": 0, "batches_committed": 0,
                              "accounting": {"emitted": 0, "excluded": {}, "pending": 0, "failed": {}},
                              "conflicts": [], **(extra or {})})
        return cw.run_seat_pass(
            self.ident, store_dir=self.store, rate_controller=self.ctrl, box_enrolled_inodes=self.box,
            quarantined_seats=self.quarantined, missing_driver_logged=set(),
            set_status=lambda name, **kw: self.status.setdefault(name, {}).update(kw), note=self.notes.append,
            runner=lambda _argv, _cwd, _t: (0, drained, ""), mono_fn=lambda: self.mono,
            discover_state=self.state, wall_fn=lambda: wall_s, **kw)

    def enrolled(self):
        return sorted(Path(e["path"]).name for e in cw.read_ledger(os.path.join(self.store, "enrollment.json"))
                      ["files"].values())


def test_candidates_are_new_matching_recent_files_oldest_first(tmp_path):
    root = tmp_path / "projects"
    _touch(root / "a" / "old-but-in-window.jsonl", age_h=40)
    _touch(root / "a" / "fresh.jsonl", age_h=1)
    _touch(root / "b" / "deep" / "subagent.jsonl", age_h=2)
    _touch(root / "a" / "too-old.jsonl", age_h=49)
    _touch(root / "a" / "notes.txt", age_h=1)
    known_path = _touch(root / "a" / "known.jsonl", age_h=1)
    spec = [{"root": str(root), "pattern": "*.jsonl", "recursive": True, "max_age_h": 48}]
    got = cw.discover_candidates(spec, known={os.path.normcase(str(known_path))}, known_inodes=set(), now_s=NOW)
    assert [Path(p).name for p in got] == ["old-but-in-window.jsonl", "subagent.jsonl", "fresh.jsonl"]
    flat = cw.discover_candidates([dict(spec[0], recursive=False)], known=set(), known_inodes=set(), now_s=NOW)
    assert flat == [], "not recursive: only files directly under the root"


def test_a_new_session_file_is_enrolled_at_the_next_discovery_and_not_before(tmp_path):
    ident = _ident(tmp_path)
    run = Run(tmp_path, ident)
    root = tmp_path / "projects"
    _touch(root / "proj" / "session-1.jsonl")
    run.pass_()
    assert run.enrolled() == ["session-1.jsonl"] and run.status["kxxx"]["context_discovered"] == 1
    _touch(root / "proj" / "session-2.jsonl")
    run.pass_(advance_s=cw.DISCOVER_INTERVAL_S - 1)
    assert run.enrolled() == ["session-1.jsonl"], "roots are walked at most every DISCOVER_INTERVAL_S"
    run.pass_(advance_s=1)
    assert run.enrolled() == ["session-1.jsonl", "session-2.jsonl"]


def test_at_most_eight_new_files_are_enrolled_per_discovery_oldest_first(tmp_path):
    ident = _ident(tmp_path)
    run = Run(tmp_path, ident)
    for n in range(10):
        _touch(tmp_path / "projects" / "p" / ("s%02d.jsonl" % n), age_h=10 - n)
    run.pass_()
    assert run.enrolled() == ["s%02d.jsonl" % n for n in range(cw.DISCOVER_MAX_PER_ROUND)]
    run.pass_(advance_s=cw.DISCOVER_INTERVAL_S)
    assert len(run.enrolled()) == 10


def test_a_root_outside_allowed_roots_enrolls_nothing_and_does_not_quarantine(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    _touch(elsewhere / "x.jsonl")
    ident = _ident(tmp_path, discover=[{"root": str(elsewhere), "recursive": True}])
    run = Run(tmp_path, ident)
    run.pass_()
    assert run.enrolled() == [] and "kxxx" not in run.quarantined


def test_another_seats_file_under_a_shared_root_is_skipped_not_quarantined(tmp_path):
    theirs = _touch(tmp_path / "projects" / "p" / "theirs.jsonl")
    mine = _touch(tmp_path / "projects" / "p" / "mine.jsonl")
    st = os.stat(theirs)
    run = Run(tmp_path, _ident(tmp_path), box={(int(st.st_dev), int(st.st_ino)): "someone-else"})
    run.pass_()
    assert run.enrolled() == [mine.name] and "kxxx" not in run.quarantined


def test_an_identity_without_discover_behaves_as_before(tmp_path):
    _touch(tmp_path / "projects" / "p" / "s.jsonl")
    run = Run(tmp_path, _ident(tmp_path, discover=False))
    run.pass_()
    assert run.enrolled() == [] and "context_discovered" not in run.status.get("kxxx", {})


def test_exclude_globs_match_the_path_relative_to_the_root(tmp_path):
    root = tmp_path / "projects"
    _touch(root / "proj" / "main.jsonl")
    _touch(root / "proj" / "sess" / "subagents" / "agent-1.jsonl")
    spec = [{"root": str(root), "recursive": True, "exclude_globs": ["*/subagents/*"]}]
    got = cw.discover_candidates(spec, known=set(), known_inodes=set(), now_s=NOW)
    assert [Path(p).name for p in got] == ["main.jsonl"]


def test_a_renamed_file_already_enrolled_by_inode_is_not_enrolled_twice(tmp_path):
    run = Run(tmp_path, _ident(tmp_path))
    first = _touch(tmp_path / "projects" / "p" / "a.jsonl")
    run.pass_()
    moved = first.with_name("a-renamed.jsonl")
    os.replace(first, moved)
    os.utime(moved, (NOW, NOW))
    run.pass_(advance_s=cw.DISCOVER_INTERVAL_S)
    assert run.enrolled() == ["a.jsonl"], "same (dev, ino): one source, never two"


def test_the_walk_stops_at_its_entry_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(cw, "DISCOVER_MAX_ENTRIES", 3)
    for n in range(6):
        _touch(tmp_path / "projects" / ("f%d.jsonl" % n))
    spec = [{"root": str(tmp_path / "projects"), "recursive": True}]
    assert len(cw.discover_candidates(spec, known=set(), known_inodes=set(), now_s=NOW)) == 3


def test_the_walk_never_descends_through_a_junction(tmp_path):
    import subprocess
    outside = tmp_path / "outside"
    _touch(outside / "secret-session.jsonl")
    root = tmp_path / "projects"
    root.mkdir()
    link = root / "jump"
    made = None
    if os.name == "nt":
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
    if made is None or made.returncode != 0:
        import pytest
        pytest.skip("cannot create a junction here")
    spec = [{"root": str(root), "recursive": True}]
    assert cw.discover_candidates(spec, known=set(), known_inodes=set(), now_s=NOW) == []



def test_a_later_identitys_pin_under_an_earlier_identitys_root_is_never_discovered_by_the_earlier(tmp_path):
    # GitHub Codex on #431 (context_worker.py:1049, P1): kxxx's discovery ran first over a shared root holding
    # other's not-yet-enrolled pin, enrolled it as kxxx's, and other was quarantined (duplicate_inode_across_seats).
    root = tmp_path / "projects"
    theirs = root / "p" / "theirs.jsonl"
    theirs.parent.mkdir(parents=True)
    theirs.write_text('{"type":"user"}\n', encoding="utf-8")         # recent by the real clock the loop uses
    mine = root / "p" / "mine.jsonl"
    mine.write_text('{"type":"user"}\n', encoding="utf-8")
    kxxx = _ident(tmp_path, "kxxx")
    other = _ident(tmp_path, "other", discover=False)
    other["context"]["pins"] = [str(theirs)]
    status = {}
    drained = json.dumps({"status": "drained", "context_held": [], "items_committed": 0, "batches_committed": 0,
                          "accounting": {"emitted": 0, "excluded": {}, "pending": 0, "failed": {}}, "conflicts": []})
    cw.run_context_worker(
        [kxxx, other], store_root=str(tmp_path / "stores"),
        set_status=lambda name, **kw: status.setdefault(name, {}).update(kw), note=lambda _m: None,
        rate_controller=cw.RateController(probe_fn=lambda _u: True),
        runner=lambda _argv, _cwd, _t: (0, drained, ""), sleep_fn=lambda _s: None, max_rounds=1)

    def enrolled(name):
        led = cw.read_ledger(os.path.join(str(tmp_path / "stores"), name, "enrollment.json"))
        return sorted(Path(e["path"]).name for e in led["files"].values())
    assert enrolled("kxxx") == ["mine.jsonl"], "kxxx discovered another identity's pin"
    assert enrolled("other") == ["theirs.jsonl"]
    assert status.get("other", {}).get("context_error") is None, status.get("other")


def test_the_walk_reads_a_large_directory_only_up_to_its_cap_and_closes_it(tmp_path, monkeypatch):
    # GitHub Codex on #431 (context_worker.py:432, P2): list(os.scandir(...)) read the whole directory before the cap.
    root = tmp_path / "big"
    root.mkdir()
    for n in range(50):
        (root / ("f%02d.jsonl" % n)).write_text("{}\n", encoding="utf-8")
    real, consumed, closed = os.scandir, [], []

    class Counting:
        def __init__(self, path):
            self.it = real(path)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            closed.append(True)
            self.it.close()
            return False

        def __iter__(self):
            for entry in self.it:
                consumed.append(entry.name)
                yield entry

    monkeypatch.setattr(cw.os, "scandir", Counting)
    got = list(cw._bounded_walk(str(root), True, 3))
    assert len(got) == 3 and len(consumed) <= 4, (len(got), len(consumed))
    assert closed, "the directory iterator was left open"


def test_a_pin_reached_by_another_path_is_reserved_by_its_inode(tmp_path):
    # The same file under two names (a hard link): kxxx's discovery sees the original name, other pins the link.
    root = tmp_path / "projects"
    theirs = root / "p" / "theirs.jsonl"
    theirs.parent.mkdir(parents=True)
    theirs.write_text('{"type":"user"}\n', encoding="utf-8")
    alias = root / "q" / "alias.jsonl"
    alias.parent.mkdir(parents=True)
    os.link(theirs, alias)
    kxxx = _ident(tmp_path, "kxxx", discover=[{"root": str(root / "p"), "pattern": "*.jsonl", "max_age_h": 48}])
    other = _ident(tmp_path, "other", discover=False)
    other["context"]["pins"] = [str(alias)]
    status = {}
    drained = json.dumps({"status": "drained", "context_held": [], "items_committed": 0, "batches_committed": 0,
                          "accounting": {"emitted": 0, "excluded": {}, "pending": 0, "failed": {}}, "conflicts": []})
    cw.run_context_worker(
        [kxxx, other], store_root=str(tmp_path / "stores"),
        set_status=lambda name, **kw: status.setdefault(name, {}).update(kw), note=lambda _m: None,
        rate_controller=cw.RateController(probe_fn=lambda _u: True),
        runner=lambda _argv, _cwd, _t: (0, drained, ""), sleep_fn=lambda _s: None, max_rounds=1)
    led = cw.read_ledger(os.path.join(str(tmp_path / "stores"), "kxxx", "enrollment.json"))
    assert led["files"] == {}, "kxxx discovered the pinned file under its other name"
    assert status.get("other", {}).get("context_error") is None, status.get("other")


def test_an_antigravity_pin_reserves_the_full_transcript_its_enrollment_takes(tmp_path):
    session = tmp_path / "brain" / "s1" / ".system_generated" / "logs"
    session.mkdir(parents=True)
    compact = session / "transcript.jsonl"
    compact.write_text("{}\n", encoding="utf-8")
    (session / "transcript_full.jsonl").write_text("{}\n", encoding="utf-8")
    ident = {"name": "ag", "cli": "antigravity", "context": {"dialect": "antigravity", "pins": [str(compact)]}}
    got = {os.path.normcase(p) for p in cw._box_pin_paths([ident])}
    assert os.path.normcase(os.path.abspath(str(session / "transcript_full.jsonl"))) in got, got
    assert os.path.normcase(os.path.abspath(str(compact))) in got


def test_an_antigravity_pin_reserves_its_full_transcript_before_it_exists(tmp_path):
    # a reviewer f414df: _box_pin_paths runs once at startup; a transcript_full.jsonl created later must already be
    # reserved, or an earlier seat's discovery takes it and antigravity is quarantined.
    session = tmp_path / "brain" / "s2" / ".system_generated" / "logs"
    session.mkdir(parents=True)
    compact = session / "transcript.jsonl"
    compact.write_text("{}\n", encoding="utf-8")
    ident = {"name": "ag", "cli": "antigravity", "context": {"dialect": "antigravity", "pins": [str(compact)]}}
    got = {os.path.normcase(p) for p in cw._box_pin_paths([ident])}
    assert not (session / "transcript_full.jsonl").exists(), "control: the full transcript does not exist yet"
    assert os.path.normcase(os.path.abspath(str(session / "transcript_full.jsonl"))) in got, got


def test_a_full_transcript_appearing_between_rounds_is_not_discovered_by_another_seat(tmp_path):
    # The same race through run_context_worker: round 1 sees only the compact pin; the full transcript appears; round 2
    # runs kxxx's discovery again (the monotonic clock jumps past DISCOVER_INTERVAL_S) and must not take it.
    root = tmp_path / "projects"
    session = root / "brain" / "s3" / ".system_generated" / "logs"
    session.mkdir(parents=True)
    compact = session / "transcript.jsonl"
    compact.write_text('{"type":"user"}\n', encoding="utf-8")
    full = session / "transcript_full.jsonl"
    kxxx = _ident(tmp_path, "kxxx")
    ag = _ident(tmp_path, "ag", discover=False)
    ag["cli"] = "antigravity"
    ag["context"].update(dialect="antigravity", pins=[str(compact)])
    clock = [1000.0]

    def mono():
        clock[0] += 400.0
        return clock[0]
    calls = []
    drained = json.dumps({"status": "drained", "context_held": [], "items_committed": 0, "batches_committed": 0,
                          "accounting": {"emitted": 0, "excluded": {}, "pending": 0, "failed": {}}, "conflicts": []})

    def runner(_argv, _cwd, _t):
        calls.append(1)
        if not full.exists():
            full.write_text('{"type":"user"}\n', encoding="utf-8")         # appears after the first pass
            (root / "late.jsonl").write_text('{"type":"user"}\n', encoding="utf-8")   # the control, for kxxx
        return 0, drained, ""
    cw.run_context_worker(
        [kxxx, ag], store_root=str(tmp_path / "stores"), set_status=lambda name, **kw: None, note=lambda _m: None,
        rate_controller=cw.RateController(probe_fn=lambda _u: True, mono_fn=mono), runner=runner, mono_fn=mono,
        sleep_fn=lambda _s: None, max_rounds=3)
    assert full.exists() and calls, "control: the full transcript appeared during the run"
    led = cw.read_ledger(os.path.join(str(tmp_path / "stores"), "kxxx", "enrollment.json"))
    names = sorted(Path(e["path"]).name for e in led["files"].values())
    assert "late.jsonl" in names, ("control: kxxx's discovery ran after the file appeared", names)
    assert not any(n.startswith("transcript") for n in names), names


# ---- seat_marker, the stall hint and the visited line (seat-handover item 1, 2 Nov 2026) -------------------------
# On HQB one Claude project folder holds more than one seat's sessions, so memory-bxxxxx pinned one named file, and at
# its 2 Nov cut-over the new session was not uploaded until a hand edit. seat_marker lets discovery take only files
# whose start names one of this seat's identities in a meshy channel_open record, as every channel session's does.

def _chan(identity: str, kind: str = "channel_open", source: str = "plugin:meshy:meshy", rtype: str = "user") -> str:
    tag = ('<channel source="%s" kind="%s" identity="%s">\n[CHANNEL] MeshyChat channel open for %s (x). New mail '
           'arrives here as events.\n</channel>' % (source, kind, identity, identity))
    return json.dumps({"type": rtype, "message": {"role": rtype, "content": tag}}) + "\n"


def _session(path: Path, *lines: str, age_h: float = 0.0, pad: int = 0) -> Path:
    head = json.dumps({"type": "user", "message": {"role": "user", "content": "x" * pad}}) + "\n" if pad else ""
    return _touch(path, age_h=age_h, text=head + "".join(lines) + '{"type":"assistant"}\n')


def _marker_spec(root: Path, marker=True) -> list[dict]:
    return [{"root": str(root), "pattern": "*.jsonl", "recursive": False, "max_age_h": 48, "seat_marker": marker}]


def test_the_seat_marker_reads_the_channel_open_record_the_channel_writes(tmp_path):
    p = _session(tmp_path / "s.jsonl", _chan("kxxx-prod"))
    assert cw.seat_marker_in_prefix(str(p), {"kxxx-prod"})
    assert not cw.seat_marker_in_prefix(str(p), {"kxxx"}), "another identity's name"
    assert not cw.seat_marker_in_prefix(str(p), set()), "no names match nothing"
    listed = tmp_path / "l.jsonl"
    rec = json.loads(_chan("kxxx-prod"))
    rec["message"]["content"] = [{"type": "text", "text": rec["message"]["content"]}]
    _touch(listed, text=json.dumps(rec) + "\n")
    assert cw.seat_marker_in_prefix(str(listed), {"kxxx-prod"}), "a content list of text blocks"
    event = json.loads(_chan("kxxx-prod", kind="message"))
    event["message"]["content"] += "\nA peer quoting your channel_open line."     # past the byte prefilter
    assert not cw.seat_marker_in_prefix(str(_touch(tmp_path / "k.jsonl", text=json.dumps(event) + "\n")),
                                        {"kxxx-prod"}), "a message event is not a channel_open, even one naming it"
    assert not cw.seat_marker_in_prefix(str(_session(tmp_path / "o.jsonl", _chan("kxxx-prod", source="slack"))),
                                        {"kxxx-prod"}), "only a meshy channel"
    assert not cw.seat_marker_in_prefix(str(_session(tmp_path / "a.jsonl", _chan("kxxx-prod", rtype="assistant"))),
                                        {"kxxx-prod"}), "the seat quoting the tag in its own text"
    quoted = json.loads(_chan("kxxx-prod"))
    quoted["message"]["content"] = "look at this: " + quoted["message"]["content"]
    assert not cw.seat_marker_in_prefix(str(_touch(tmp_path / "q.jsonl", text=json.dumps(quoted) + "\n")),
                                        {"kxxx-prod"}), "a prompt that only mentions the tag"


def test_the_seat_marker_reads_only_its_prefix_and_skips_a_cut_line(tmp_path):
    late = _session(tmp_path / "late.jsonl", _chan("kxxx-prod"), pad=cw.SEAT_MARKER_PREFIX_BYTES)
    assert not cw.seat_marker_in_prefix(str(late), {"kxxx-prod"}), "past the prefix"
    line = _chan("kxxx-prod")
    edge = tmp_path / "edge.jsonl"
    edge.write_bytes(b"x" * (cw.SEAT_MARKER_PREFIX_BYTES - len(line) // 2) + b"\n" + line.encode())
    assert not cw.seat_marker_in_prefix(str(edge), {"kxxx-prod"}), "a line the limit cuts does not parse"
    assert cw.seat_marker_in_prefix(str(edge), {"kxxx-prod"}, limit=cw.SEAT_MARKER_PREFIX_BYTES * 2)


def test_seat_marker_discovery_takes_only_this_seats_sessions_from_a_shared_root(tmp_path):
    root = tmp_path / "projects"
    _session(root / "mine.jsonl", _chan("kxxx"), _chan("kxxx-prod"), age_h=2)
    _session(root / "theirs.jsonl", _chan("nxxxxx-prod"), age_h=1)
    _session(root / "plain.jsonl", age_h=1)
    got = cw.discover_candidates(_marker_spec(root), known=set(), known_inodes=set(), now_s=NOW,
                                 seat_names={"kxxx-prod"})
    assert [Path(p).name for p in got] == ["mine.jsonl"]
    listed = cw.discover_candidates(_marker_spec(root, ["nxxxxx-prod"]), known=set(), known_inodes=set(), now_s=NOW,
                                    seat_names={"kxxx-prod"})
    assert [Path(p).name for p in listed] == ["theirs.jsonl"], "a list names the identities itself"
    for bad in ("yes", [], [""], 1):
        assert cw.discover_candidates(_marker_spec(root, bad), known=set(), known_inodes=set(), now_s=NOW,
                                      seat_names={"kxxx-prod"}) == [], bad
    unmarked = [dict(_marker_spec(root)[0], seat_marker=False)]
    assert len(cw.discover_candidates(unmarked, known=set(), known_inodes=set(), now_s=NOW)) == 3


def test_a_session_whose_marker_arrives_later_is_enrolled_a_round_later(tmp_path):
    root = tmp_path / "projects"
    ident = _ident(tmp_path, discover=_marker_spec(root))
    run = Run(tmp_path, ident)
    young = _session(root / "young.jsonl")
    run.pass_()
    assert run.enrolled() == [], "no channel_open yet"
    _session(young, _chan("kxxx"))
    run.pass_(advance_s=cw.DISCOVER_INTERVAL_S)
    assert run.enrolled() == ["young.jsonl"], "a miss is not remembered"


def test_seat_marker_on_a_dialect_without_channel_records_is_a_config_error(tmp_path):
    ident = _ident(tmp_path, discover=_marker_spec(tmp_path / "projects"))
    ident["context"]["dialect"] = "codex"
    run = Run(tmp_path, ident)
    assert run.pass_()["outcome"] == "config_error"
    assert run.status["kxxx"]["context_error"] == "seat_marker_needs_claude"


def test_seat_identity_names_groups_configured_identities_by_their_seat():
    idents = [{"name": "bxxxxx-prod", "context": {"seat": "memory-bxxxxx"}},
              {"name": "bxxxxx-test", "seat": "memory-bxxxxx"},
              {"name": "uxxx-prod", "context": {"seat": "uxxx"}},
              {"name": "no-seat"}, {"context": {"seat": "x"}}, "junk"]
    assert cw.seat_identity_names(idents) == {"memory-bxxxxx": frozenset({"bxxxxx-prod", "bxxxxx-test"}),
                                              "uxxx": frozenset({"uxxx-prod"})}


def test_the_worker_accepts_a_channel_open_from_any_identity_of_the_seat(tmp_path):
    root = tmp_path / "projects"
    _session(root / "mine.jsonl", _chan("kxxx-test"))
    ident = _ident(tmp_path, discover=_marker_spec(root))
    other = {"name": "kxxx-test", "enabled": False, "context": {"seat": "kxxx"}}
    status: dict = {}
    drained = json.dumps({"status": "drained", "context_held": [], "items_committed": 0, "conflicts": []})
    cw.run_context_worker(
        [ident], all_identities=[ident, other], store_root=str(tmp_path / "ctx_store"),
        set_status=lambda name, **kw: status.setdefault(name, {}).update(kw), note=lambda _m: None,
        rate_controller=cw.RateController(probe_fn=lambda _u: True),
        runner=lambda _argv, _cwd, _t: (0, drained, ""), sleep_fn=lambda _s: None, max_rounds=1)
    ledger = cw.read_ledger(str(tmp_path / "ctx_store" / "kxxx" / "enrollment.json"))
    assert [Path(e["path"]).name for e in ledger["files"].values()] == ["mine.jsonl"]


def test_a_stalled_upload_is_noted_once_a_day_while_a_newer_unenrolled_file_grows(tmp_path):
    root = tmp_path / "projects"
    ident = _ident(tmp_path, discover=_marker_spec(root))
    run = Run(tmp_path, ident)
    notes: dict = {}
    _session(root / "old.jsonl", _chan("kxxx"), age_h=7)
    run.pass_(seat_notes=notes)
    assert run.enrolled() == ["old.jsonl"] and run.status["kxxx"]["context_stall"] is None
    _session(root / "new.jsonl", age_h=0.5)              # this seat's new session, its marker not written yet
    run.pass_(advance_s=cw.DISCOVER_INTERVAL_S, seat_notes=notes)
    stall = run.status["kxxx"]["context_stall"]
    assert Path(stall["enrolled"]).name == "old.jsonl" and Path(stall["newer"]).name == "new.jsonl"
    assert stall["idle_h"] == 7.0 and stall["newer_age_min"] == 30.0
    stalled = lambda: [n for n in run.notes if "may have stopped following" in n]  # noqa: E731
    assert len(stalled()) == 1 and "old.jsonl" in stalled()[0] and "new.jsonl" in stalled()[0]
    run.pass_(advance_s=cw.DISCOVER_INTERVAL_S, seat_notes=notes)
    assert len(stalled()) == 1, "once per file per day"
    run.pass_(advance_s=cw.DISCOVER_INTERVAL_S, wall_s=NOW + 86400 - 2 * 3600, seat_notes=notes)
    assert len(stalled()) == 1, "next day, but the newer file is now idle 22 h itself: no hint"
    _session(root / "new.jsonl", _chan("kxxx"), age_h=0.0)
    run.pass_(advance_s=cw.DISCOVER_INTERVAL_S, seat_notes=notes)
    assert "new.jsonl" in run.enrolled() and run.status["kxxx"]["context_stall"] is None, "cleared once enrolled"


def test_no_stall_while_the_seat_writes_or_when_the_newer_file_is_another_seats(tmp_path):
    root = tmp_path / "projects"
    _session(root / "mine.jsonl", _chan("kxxx"), age_h=5)
    _session(root / "other.jsonl", age_h=0.2)
    run = Run(tmp_path, _ident(tmp_path, discover=_marker_spec(root)))
    run.pass_()
    assert run.status["kxxx"]["context_stall"] is None, "5 h idle is under DISCOVER_STALL_H"
    _session(root / "mine.jsonl", _chan("kxxx"), age_h=8)
    st = os.stat(root / "other.jsonl")
    run.box[(int(st.st_dev), int(st.st_ino))] = "nxxxxx"
    run.pass_(advance_s=cw.DISCOVER_INTERVAL_S)
    assert run.status["kxxx"]["context_stall"] is None, "a newer file another seat enrolled is not this seat's stall"
    del run.box[(int(st.st_dev), int(st.st_ino))]
    run.pass_(advance_s=cw.DISCOVER_INTERVAL_S)
    assert Path(run.status["kxxx"]["context_stall"]["newer"]).name == "other.jsonl", "control: unenrolled, it is"


def test_the_sources_visited_line_is_written_when_the_order_or_progress_changes(tmp_path):
    root = tmp_path / "projects"
    _session(root / "a.jsonl", _chan("kxxx"), age_h=3)
    _session(root / "b.jsonl", _chan("kxxx"), age_h=1)
    run = Run(tmp_path, _ident(tmp_path, discover=_marker_spec(root)))
    notes: dict = {}
    run.pass_(extra={"visited": [{"source_index": 1, "batches": 4}, {"source_index": 0, "batches": 0}]},
              seat_notes=notes)
    visited = lambda: [n for n in run.notes if "context pass visited" in n]  # noqa: E731
    assert visited() == ["kxxx: context pass visited b.jsonl (4 batches), a.jsonl (0 batches)"]
    run.pass_(extra={"visited": [{"source_index": 1, "batches": 2}, {"source_index": 0, "batches": 0}]},
              seat_notes=notes)
    assert len(visited()) == 1, "same order, same progress: no line"
    run.pass_(extra={"visited": [{"source_index": 1, "batches": 0}, {"source_index": 0, "batches": 3},
                                 {"source_index": 9, "batches": 1}, {"source_index": "0", "batches": 1}]},
              seat_notes=notes)
    assert visited()[-1] == "kxxx: context pass visited b.jsonl (0 batches), a.jsonl (3 batches)", \
        "out-of-range and malformed rows are dropped"


# ---- where the channel_open sits, by box (kxxx's P1 on #486) ------------------------------------------------------
# HQB (Claude Code 2.1.296, 2 Nov): a queue-operation record with the tag as its top-level `content`, then a user
# record, both within the first 26 KB. HQD (kxxx's 218956a8): first one at byte 498,395, in queue-operation,
# api-request-blob and attachment records, behind SessionStart hook output.

def _tag(identity: str) -> str:
    return json.loads(_chan(identity))["message"]["content"]


def test_a_queue_operation_record_carries_the_marker_as_hqb_writes_it(tmp_path):
    rec = {"type": "queue-operation", "operation": "enqueue", "timestamp": "2026-11-02T09:45:31.000Z",
           "sessionId": "1cba694e", "content": _tag("kxxx-prod")}
    p = _touch(tmp_path / "q.jsonl", text=json.dumps(rec) + "\n")
    assert cw.seat_marker_in_prefix(str(p), {"kxxx-prod"})


def test_an_attachment_or_request_blob_record_carries_the_marker_at_any_shallow_depth(tmp_path):
    att = {"type": "attachment", "attachment": {"type": "hook_additional_context", "content": [_tag("kxxx-prod")]}}
    blob = {"type": "api-request-blob", "request": {"messages": [{"role": "user", "content": [
        {"type": "text", "text": _tag("kxxx-prod")}]}]}}
    for i, rec in enumerate((att, blob)):
        p = _touch(tmp_path / ("r%d.jsonl" % i), text=json.dumps(rec) + "\n")
        assert cw.seat_marker_in_prefix(str(p), {"kxxx-prod"}), rec["type"]
    deep: dict = {"type": "attachment"}
    node = deep
    for _ in range(cw._MARKER_WALK_DEPTH + 1):
        node["x"] = {}
        node = node["x"]
    node["content"] = _tag("kxxx-prod")
    p = _touch(tmp_path / "deep.jsonl", text=json.dumps(deep) + "\n")
    assert not cw.seat_marker_in_prefix(str(p), {"kxxx-prod"}), "deeper than _MARKER_WALK_DEPTH is not read"


def test_a_tool_result_that_quotes_a_channel_open_is_not_a_marker(tmp_path):
    rec = {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": _tag("kxxx-prod")},
        {"type": "tool_result", "tool_use_id": "t2", "content": [{"type": "text", "text": _tag("kxxx-prod")}]}]}}
    p = _touch(tmp_path / "t.jsonl", text=json.dumps(rec) + "\n")
    assert not cw.seat_marker_in_prefix(str(p), {"kxxx-prod"}), "tool output can quote another seat's transcript"


def test_a_marker_past_64_kb_but_inside_the_prefix_is_found_as_on_hqd(tmp_path):
    hooks = "".join(json.dumps({"type": "attachment", "attachment": {"content": "h" * 4000}}) + "\n"
                    for _ in range(125))                                     # about 500 KB of hook output first
    rec = {"type": "queue-operation", "operation": "enqueue", "content": _tag("kxxx-prod")}
    p = _touch(tmp_path / "hqd.jsonl", text=hooks + json.dumps(rec) + "\n")
    assert 498_000 < hooks.encode().__len__() < cw.SEAT_MARKER_PREFIX_BYTES
    assert cw.seat_marker_in_prefix(str(p), {"kxxx-prod"})


# ---- Codex session_meta filters and visible discover-spec problems --------------------------------------------

def test_session_meta_originator_enrolls_only_the_interactive_rollout(tmp_path):
    root = tmp_path / "projects"
    _codex_meta(root / "interactive.jsonl", age_h=2)
    _codex_meta(root / "review.jsonl", originator="codex_exec", source="exec", age_h=1)
    ident = _ident(tmp_path, discover=[{"root": str(root), "session_meta": {"originator": "codex-tui"}}])
    ident["context"]["dialect"] = "codex"
    run = Run(tmp_path, ident)
    run.pass_()
    assert run.enrolled() == ["interactive.jsonl"]
    assert run.status["kxxx"]["context_discover_problems"] is None


def test_without_session_meta_filter_both_codex_rollouts_are_still_enrolled(tmp_path):
    root = tmp_path / "projects"
    _codex_meta(root / "interactive.jsonl", age_h=2)
    _codex_meta(root / "review.jsonl", originator="codex_exec", source="exec", age_h=1)
    ident = _ident(tmp_path)
    ident["context"]["dialect"] = "codex"
    run = Run(tmp_path, ident)
    run.pass_()
    assert run.enrolled() == ["interactive.jsonl", "review.jsonl"]


@pytest.mark.parametrize("prefix,cwd,matches", [
    ("a/b", "a/b", True),
    ("a/b", "a/b/src", True),
    ("a/b", r"a\b\src", True),
    (r"a\b", "a/b/../b/src", True),
    ("a/b", "a/bc", False),
    ("a/b", r"a\bc\src", False),
    ("a/b", "elsewhere/b", False),
    ("A/B", "a/b", os.path.normcase("A") == os.path.normcase("a")),
])
def test_session_meta_cwd_prefix_is_normalized_and_respects_path_boundaries(tmp_path, prefix, cwd, matches):
    root = tmp_path / "projects"
    # Native absolute roots keep this test valid on Windows and on the Mac/Linux review hosts.
    prefix, cwd = str(tmp_path) + "/" + prefix, str(tmp_path) + "/" + cwd
    _codex_meta(root / "rollout.jsonl", cwd=cwd)
    spec = [{"root": str(root), "session_meta": {"cwd_prefix": prefix}}]
    got = cw.discover_candidates(spec, known=set(), known_inodes=set(), now_s=NOW)
    assert bool(got) is matches


def test_session_meta_requires_every_configured_field_and_missing_fields_fail_closed(tmp_path):
    root = tmp_path / "projects"
    prefix = str(tmp_path / "workspace" / "a" / "b")
    _codex_meta(root / "match.jsonl", cwd=prefix)
    _codex_meta(root / "wrong-originator.jsonl", originator="codex_exec", source="exec", cwd=prefix)
    _codex_meta(root / "wrong-cwd.jsonl", cwd=prefix + "c")
    _touch(root / "missing-cwd.jsonl", text=json.dumps({
        "type": "session_meta", "payload": {"originator": "codex-tui"}}) + "\n")
    _touch(root / "missing-originator.jsonl", text=json.dumps({
        "type": "session_meta", "payload": {"cwd": prefix}}) + "\n")
    spec = [{"root": str(root), "session_meta": {"originator": "codex-tui", "cwd_prefix": prefix}}]
    got = cw.discover_candidates(spec, known=set(), known_inodes=set(), now_s=NOW)
    assert [Path(p).name for p in got] == ["match.jsonl"]


@pytest.mark.parametrize("relative", ["workspace", "./workspace", "../workspace", "C:workspace"])
def test_relative_cwd_prefix_is_refused_visibly_instead_of_using_the_worker_cwd(tmp_path, relative):
    root = tmp_path / "projects"
    _codex_meta(root / "rollout.jsonl", cwd=os.path.abspath(relative))
    problems = []
    got = cw.discover_candidates([{"root": str(root), "session_meta": {"cwd_prefix": relative}}],
                                 known=set(), known_inodes=set(), now_s=NOW, problems=problems)
    assert got == []
    assert problems == [{"spec_index": 0, "key": "session_meta.cwd_prefix",
                         "code": "invalid_session_meta_filter"}]


@pytest.mark.skipif(os.name != "nt", reason="Windows drive semantics")
def test_session_meta_cwd_prefix_refuses_a_different_windows_drive(tmp_path):
    path = _codex_meta(tmp_path / "rollout.jsonl", cwd="D:/a/b")
    assert not cw.session_meta_matches(str(path), {"cwd_prefix": "C:/a/b"})


@pytest.mark.parametrize("first", [
    '{"type":"event_msg","payload":{}}\n',
    "not json\n",
    "[]\n",
    '{"type":"session_meta","payload":[]}\n',
    '{"type":"session_meta","payload":{"originator":"codex-tui"}}',  # incomplete JSONL record
])
def test_session_meta_rejects_bad_or_incomplete_first_record_even_with_a_later_match(tmp_path, first):
    root = tmp_path / "projects"
    valid = '{"type":"session_meta","payload":{"originator":"codex-tui"}}\n'
    # The no-newline case remains incomplete instead of being joined to a second record.
    text = first + valid if first.endswith("\n") else first
    _touch(root / "rollout.jsonl", text=text)
    got = cw.discover_candidates([{"root": str(root), "session_meta": {"originator": "codex-tui"}}],
                                 known=set(), known_inodes=set(), now_s=NOW)
    assert got == []


def test_session_meta_rejects_an_oversize_first_line_and_accepts_the_same_file_without_filter(tmp_path):
    root = tmp_path / "projects"
    _touch(root / "rollout.jsonl", text=json.dumps({"type": "session_meta", "payload": {
        "originator": "codex-tui", "padding": "x" * cw.SESSION_META_MAX_LINE_BYTES}}) + "\n")
    spec = {"root": str(root), "session_meta": {"originator": "codex-tui"}}
    assert cw.discover_candidates([spec], known=set(), known_inodes=set(), now_s=NOW) == []
    assert len(cw.discover_candidates([{"root": str(root)}], known=set(), known_inodes=set(), now_s=NOW)) == 1


def test_session_meta_first_line_read_never_exceeds_its_cap(tmp_path, monkeypatch):
    path = _codex_meta(tmp_path / "rollout.jsonl")
    raw = path.read_bytes()
    reads = []

    class HeaderOnly:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def readline(self, limit):
            reads.append(limit)
            return raw[:limit]

    monkeypatch.setattr(cw, "open", lambda *_args, **_kw: HeaderOnly(), raising=False)
    assert cw.session_meta_matches(str(path), {"originator": "codex-tui"}, limit=len(raw))
    assert not cw.session_meta_matches(str(path), {"originator": "codex-tui"}, limit=len(raw) - 1)
    assert reads == [len(raw), len(raw) - 1]


def test_session_meta_content_is_read_only_after_other_candidate_checks(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    good = _codex_meta(root / "good.jsonl")
    _codex_meta(root / "old.jsonl", age_h=49)
    _codex_meta(root / "excluded.jsonl")
    known = _codex_meta(root / "known.jsonl")
    claimed = _codex_meta(root / "claimed.jsonl")
    _codex_meta(root / "wrong-extension.txt")
    st = os.stat(claimed)
    reads = []
    real = cw.session_meta_matches

    def checked(path, wanted):
        reads.append(Path(path).name)
        return real(path, wanted)

    monkeypatch.setattr(cw, "session_meta_matches", checked)
    spec = [{"root": str(root), "exclude_globs": ["excluded.jsonl"],
             "session_meta": {"originator": "codex-tui"}}]
    got = cw.discover_candidates(spec, known={os.path.normcase(str(known))},
                                 known_inodes={(int(st.st_dev), int(st.st_ino))}, now_s=NOW)
    assert got == [str(good)]
    assert reads == ["good.jsonl"]


def test_unknown_discover_key_refuses_only_that_spec_and_is_visible_then_cleared(tmp_path):
    root = tmp_path / "projects"
    _codex_meta(root / "bad" / "refused.jsonl")
    _codex_meta(root / "good" / "accepted.jsonl")
    specs = [{"root": str(root / "bad"), "max_sources": 1}, {"root": str(root / "good")}]
    ident = _ident(tmp_path, discover=specs)
    run = Run(tmp_path, ident)
    run.pass_()
    assert run.enrolled() == ["accepted.jsonl"]
    assert run.status["kxxx"]["context_discover_problems"] == [
        {"spec_index": 0, "key": "max_sources", "code": "unknown_discover_key"}]
    assert any("spec 0" in n and "max_sources" in n for n in run.notes)
    assert "kxxx" not in run.quarantined
    del specs[0]["max_sources"]
    run.pass_(advance_s=cw.DISCOVER_INTERVAL_S)
    assert run.enrolled() == ["accepted.jsonl", "refused.jsonl"]
    assert run.status["kxxx"]["context_discover_problems"] is None


@pytest.mark.parametrize("meta,key", [
    (None, "session_meta"),
    ("codex-tui", "session_meta"),
    ({"originator": ""}, "session_meta.originator"),
    ({"originator": 1}, "session_meta.originator"),
    ({"cwd_prefix": " "}, "session_meta.cwd_prefix"),
    ({"cwd_prefix": "C:/a/\0"}, "session_meta.cwd_prefix"),
    ({"source": "exec"}, "session_meta.source"),
])
def test_invalid_session_meta_specs_enroll_nothing_and_report_the_field(tmp_path, meta, key):
    root = tmp_path / "projects"
    _codex_meta(root / "rollout.jsonl")
    problems = []
    got = cw.discover_candidates([{"root": str(root), "session_meta": meta}], known=set(),
                                 known_inodes=set(), now_s=NOW, problems=problems)
    assert got == [] and len(problems) == 1
    assert problems[0]["spec_index"] == 0 and problems[0]["key"] == key
