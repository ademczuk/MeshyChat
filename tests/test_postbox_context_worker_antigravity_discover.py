"""Tests for fail-closed `antigravity_session` discovery filtering stacked on PR #504 (`6a29d086`)."""
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


def _load_cw():
    spec = importlib.util.spec_from_file_location("pb_ctx_ag_disc", PB_DIR / "context_worker.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cw = _load_cw()
NOW = 1_800_000_000.0

MAIN_UUID_1 = "21599c0d-70d0-4d25-8ff1-dbe7d9d67763"
MAIN_UUID_2 = "8e6dd3e5-863f-4f84-a617-0a9f69c9d020"
SUB_UUID_1 = "9b32aba1-4529-4d12-a582-4d2f3e97c8ae"
SUB_UUID_2 = "e25e7741-0f25-4b83-8a6d-4343f717a012"
PROBE_UUID = "c633226f-9727-44b4-a292-7285133f8c17"


def _write_transcript(
    brain_root: Path,
    conv_id: str,
    filename: str = "transcript.jsonl",
    *,
    source: str = "USER_EXPLICIT",
    rec_type: str = "USER_INPUT",
    step_index: object = 0,
    content: str = "<USER_REQUEST>\nSession resumed. Per GEMINI.md: arm your waiter.\n</USER_REQUEST>",
    age_h: float = 0.0,
    raw_text: str | None = None,
) -> Path:
    path = brain_root / conv_id / ".system_generated" / "logs" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    if raw_text is None:
        rec = {
            "step_index": step_index,
            "source": source,
            "type": rec_type,
            "status": "DONE",
            "created_at": "2026-11-04T08:00:00Z",
            "content": content,
        }
        raw_text = json.dumps(rec) + "\n"
    path.write_text(raw_text, encoding="utf-8")
    ts = NOW - age_h * 3600.0
    os.utime(path, (ts, ts))
    return path


def _ag_ident(tmp_path: Path, discover_specs: list[dict], *, dialect: str = "antigravity") -> dict:
    brain = tmp_path / "brain"
    brain.mkdir(exist_ok=True)
    state = tmp_path / "antigravity-prod-state.json"
    state.write_text("{}", encoding="utf-8")
    client = tmp_path / "client"
    client.mkdir(exist_ok=True)
    return {
        "name": "antigravity-prod",
        "cli": dialect,
        "enabled": True,
        "python": sys.executable,
        "client": str(client),
        "state": str(state),
        "base_url": "http://127.0.0.1:6310",
        "context": {
            "enabled": True,
            "seat": "antigravity",
            "dialect": dialect,
            "app_root": str(ROOT),
            "allowed_roots": [str(brain)],
            "pins": [],
            "discover": discover_specs,
            "namespace": "shared/transcript-context",
        },
    }


class AgRun:
    def __init__(self, tmp_path: Path, ident: dict):
        self.ident = ident
        self.store = str(tmp_path / "ctx_store" / ident["name"])
        self.mono = 1000.0
        self.disc_state: dict[str, float] = {}
        self.status: dict = {}
        self.notes: list[str] = []
        self.quarantined: set = set()
        self.box: dict = {}
        self.ctrl = cw.RateController(
            probe_fn=lambda _u: True,
            mono_fn=lambda: self.mono,
            now_fn=lambda: datetime.datetime(2026, 11, 4, 10, 0, tzinfo=datetime.timezone.utc),
        )

    def pass_(self, advance_s: float = 0.0, wall_s: float = NOW):
        self.mono += advance_s
        drained = json.dumps({
            "status": "drained",
            "context_held": [],
            "items_committed": 0,
            "batches_committed": 0,
            "accounting": {"emitted": 0, "excluded": {}, "pending": 0, "failed": {}},
            "conflicts": [],
        })
        return cw.run_seat_pass(
            self.ident,
            store_dir=self.store,
            rate_controller=self.ctrl,
            box_enrolled_inodes=self.box,
            quarantined_seats=self.quarantined,
            missing_driver_logged=set(),
            set_status=lambda name, **kw: self.status.setdefault(name, {}).update(kw),
            note=self.notes.append,
            runner=lambda _argv, _cwd, _t: (0, drained, ""),
            mono_fn=lambda: self.mono,
            discover_state=self.disc_state,
            wall_fn=lambda: wall_s,
        )

    def ledger(self) -> dict:
        return cw.read_ledger(os.path.join(self.store, "enrollment.json"))

    def enrolled_rel(self, brain: Path) -> list[str]:
        return sorted(
            Path(e["path"]).relative_to(brain).as_posix()
            for e in self.ledger()["files"].values()
        )


def test_antigravity_session_true_enrolls_main_session_and_rejects_subagents_and_probes(tmp_path):
    brain = tmp_path / "brain"
    _write_transcript(brain, MAIN_UUID_1, "transcript.jsonl", age_h=2.0)
    _write_transcript(brain, MAIN_UUID_1, "transcript_full.jsonl", age_h=1.9)
    _write_transcript(
        brain,
        SUB_UUID_1,
        "transcript.jsonl",
        source="SYSTEM_SDK",
        rec_type="SYSTEM_MESSAGE",
        content="Session resumed. Per GEMINI.md: quoted inside subagent.",
        age_h=1.0,
    )
    _write_transcript(
        brain,
        SUB_UUID_2,
        "transcript_full.jsonl",
        source="SYSTEM",
        rec_type="SYSTEM_MESSAGE",
        content="<USER_REQUEST>\nSession resumed. Per GEMINI.md",
        age_h=0.8,
    )
    _write_transcript(
        brain,
        PROBE_UUID,
        "transcript.jsonl",
        source="USER_EXPLICIT",
        rec_type="USER_INPUT",
        content="<USER_REQUEST>\n# Adversarial review: four lines an employee will say on Monday\n</USER_REQUEST>",
        age_h=0.5,
    )
    _write_transcript(
        brain,
        "11111111-2222-3333-4444-555555555555",
        "transcript.jsonl",
        source="USER_EXPLICIT",
        rec_type="USER_INPUT",
        content="Please inspect this literal: <USER_REQUEST>\nSession resumed. Per GEMINI.md",
        age_h=0.4,
    )
    scratch = brain / MAIN_UUID_1 / "scratch" / "transcript.jsonl"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    scratch.write_text(
        json.dumps({"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT",
                    "content": "Session resumed. Per GEMINI.md"}) + "\n",
        encoding="utf-8",
    )
    _write_transcript(brain, "not-a-uuid-folder", "transcript.jsonl", age_h=0.3)

    specs = [{"root": str(brain), "pattern": "transcript*.jsonl", "recursive": True,
              "max_age_h": 48, "antigravity_session": True}]
    run = AgRun(tmp_path, _ag_ident(tmp_path, specs))
    res = run.pass_()
    assert res["outcome"] == "ok"
    assert run.status["antigravity-prod"]["context_discover_problems"] is None
    assert run.enrolled_rel(brain) == [f"{MAIN_UUID_1}/.system_generated/logs/transcript_full.jsonl"]
    assert run.status["antigravity-prod"]["context_discovered"] == 1

    # On the next discovery round, the superseded compact transcript.jsonl is already known and not re-read!
    reads = []
    real_match = cw.antigravity_session_matches
    cw.antigravity_session_matches = lambda p, w, **kw: (reads.append(p), real_match(p, w, **kw))[1]
    try:
        run.pass_(advance_s=cw.DISCOVER_INTERVAL_S)
    finally:
        cw.antigravity_session_matches = real_match
    assert MAIN_UUID_1 not in " ".join(reads), "superseded compact sibling should be skipped via known/known_inodes"


def test_session_id_file_pins_active_uuid_fails_closed_during_cutover_and_rotates_cleanly(tmp_path):
    brain = tmp_path / "brain"
    pin_file = tmp_path / "antigravity-conversation.id"
    _write_transcript(brain, MAIN_UUID_1, "transcript_full.jsonl", age_h=3.0)
    _write_transcript(brain, MAIN_UUID_2, "transcript_full.jsonl", age_h=1.0)
    _write_transcript(
        brain,
        SUB_UUID_1,
        "transcript_full.jsonl",
        source="SYSTEM_SDK",
        rec_type="SYSTEM_MESSAGE",
        content="The following is a <SYSTEM_MESSAGE>...",
        age_h=0.5,
    )

    specs = [{
        "root": str(brain),
        "pattern": "transcript*.jsonl",
        "recursive": True,
        "max_age_h": 48,
        "antigravity_session": {
            "session_id_file": str(pin_file),
            "startup_prefixes": ["Session resumed. Per GEMINI.md"],
        },
    }]
    run = AgRun(tmp_path, _ag_ident(tmp_path, specs))

    run.pass_()
    assert run.enrolled_rel(brain) == []
    assert run.status["antigravity-prod"]["context_discover_problems"] is None

    for sentinel in ("NEW\n", "PENDING_NEW\n", "not-a-uuid\n", "\n"):
        pin_file.write_text(sentinel, encoding="utf-8")
        run.pass_(advance_s=cw.DISCOVER_INTERVAL_S)
        assert run.enrolled_rel(brain) == [], sentinel

    pin_file.write_text(SUB_UUID_1.upper() + "\n", encoding="utf-8")
    run.pass_(advance_s=cw.DISCOVER_INTERVAL_S)
    assert run.enrolled_rel(brain) == []

    pin_file.write_text(MAIN_UUID_1.upper() + "\n", encoding="utf-8")
    run.pass_(advance_s=cw.DISCOVER_INTERVAL_S)
    assert run.enrolled_rel(brain) == [f"{MAIN_UUID_1}/.system_generated/logs/transcript_full.jsonl"]

    pin_file.write_text(MAIN_UUID_2 + "\n", encoding="utf-8")
    run.pass_(advance_s=cw.DISCOVER_INTERVAL_S)
    assert run.enrolled_rel(brain) == sorted([
        f"{MAIN_UUID_1}/.system_generated/logs/transcript_full.jsonl",
        f"{MAIN_UUID_2}/.system_generated/logs/transcript_full.jsonl",
    ])


@pytest.mark.parametrize("raw_line", [
    "",
    "not json\n",
    "[]\n",
    '{"source":"USER_EXPLICIT","type":"USER_INPUT","step_index":0,"content":"Session resumed. Per GEMINI.md"}',
    '{"source":"SYSTEM_SDK","type":"USER_INPUT","step_index":0,"content":"Session resumed. Per GEMINI.md"}\n',
    '{"source":"USER_EXPLICIT","type":"SYSTEM_MESSAGE","step_index":0,"content":"Session resumed. Per GEMINI.md"}\n',
    '{"source":"USER_EXPLICIT","type":"USER_INPUT","step_index":1,"content":"Session resumed. Per GEMINI.md"}\n',
    '{"source":"USER_EXPLICIT","type":"USER_INPUT","step_index":"0","content":"Session resumed. Per GEMINI.md"}\n',
    '{"source":"USER_EXPLICIT","type":"USER_INPUT","step_index":0,"content":"   "}\n',
])
def test_antigravity_session_rejects_malformed_or_incomplete_first_record(tmp_path, raw_line):
    brain = tmp_path / "brain"
    second_valid = json.dumps({
        "step_index": 0,
        "source": "USER_EXPLICIT",
        "type": "USER_INPUT",
        "content": "Session resumed. Per GEMINI.md",
    }) + "\n"
    text = raw_line + second_valid if raw_line.endswith("\n") else raw_line
    _write_transcript(brain, MAIN_UUID_1, "transcript.jsonl", raw_text=text)
    spec = [{"root": str(brain), "pattern": "transcript*.jsonl", "recursive": True, "antigravity_session": True}]
    assert cw.discover_candidates(spec, known=set(), known_inodes=set(), now_s=NOW) == []


def test_antigravity_session_header_read_is_bounded_by_limit(tmp_path):
    brain = tmp_path / "brain"
    p = _write_transcript(brain, MAIN_UUID_1, "transcript.jsonl")
    size = len(p.read_bytes())
    wanted = cw._spec_antigravity_session({"antigravity_session": True})
    assert cw.antigravity_session_matches(str(p), wanted, limit=size)
    assert not cw.antigravity_session_matches(str(p), wanted, limit=size - 1)


@pytest.mark.parametrize("bad_filter,exp_key,exp_code", [
    (None, "antigravity_session", "invalid_antigravity_session_filter"),
    ("yes", "antigravity_session", "invalid_antigravity_session_filter"),
    (0, "antigravity_session", "invalid_antigravity_session_filter"),
    (1, "antigravity_session", "invalid_antigravity_session_filter"),
    ([], "antigravity_session", "invalid_antigravity_session_filter"),
    ({}, "antigravity_session", "invalid_antigravity_session_filter"),
    ({"unknown_key": True}, "antigravity_session.unknown_key", "unknown_antigravity_session_key"),
    ({"session_id_file": None}, "antigravity_session.session_id_file", "invalid_antigravity_session_filter"),
    ({"session_id_file": ""}, "antigravity_session.session_id_file", "invalid_antigravity_session_filter"),
    ({"session_id_file": "relative/id.txt"}, "antigravity_session.session_id_file", "invalid_antigravity_session_filter"),
    ({"session_id_file": "C:/bad/\0path"}, "antigravity_session.session_id_file", "invalid_antigravity_session_filter"),
    ({"startup_prefixes": None}, "antigravity_session.startup_prefixes", "invalid_antigravity_session_filter"),
    ({"startup_prefixes": []}, "antigravity_session.startup_prefixes", "invalid_antigravity_session_filter"),
    ({"startup_prefixes": ["   "]}, "antigravity_session.startup_prefixes", "invalid_antigravity_session_filter"),
    ({"startup_prefixes": ["bad\0prefix"]}, "antigravity_session.startup_prefixes", "invalid_antigravity_session_filter"),
    ({"startup_prefixes": "Session resumed. Per GEMINI.md"}, "antigravity_session.startup_prefixes", "invalid_antigravity_session_filter"),
])
def test_invalid_antigravity_session_specs_report_problem_and_keep_valid_sibling_spec(tmp_path, bad_filter, exp_key, exp_code):
    bad_root = tmp_path / "brain_bad"
    good_root = tmp_path / "brain_good"
    _write_transcript(bad_root, MAIN_UUID_1, "transcript.jsonl")
    good_p = _write_transcript(good_root, MAIN_UUID_2, "transcript.jsonl")
    specs = [
        {"root": str(bad_root), "pattern": "transcript*.jsonl", "recursive": True, "antigravity_session": bad_filter},
        {"root": str(good_root), "pattern": "transcript*.jsonl", "recursive": True, "antigravity_session": True},
    ]
    problems: list[dict] = []
    got = cw.discover_candidates(specs, known=set(), known_inodes=set(), now_s=NOW, problems=problems)
    assert got == [str(good_p)]
    assert problems == [{"spec_index": 0, "key": exp_key, "code": exp_code}]


def test_antigravity_session_on_non_antigravity_dialect_is_config_error(tmp_path):
    brain = tmp_path / "brain"
    specs = [{"root": str(brain), "pattern": "transcript*.jsonl", "recursive": True, "antigravity_session": True}]
    ident = _ag_ident(tmp_path, specs, dialect="codex")
    run = AgRun(tmp_path, ident)
    res = run.pass_()
    assert res["outcome"] == "config_error"
    assert run.status["antigravity-prod"]["context_error"] == "antigravity_session_needs_antigravity"


def test_antigravity_session_reads_content_only_after_non_content_checks(tmp_path, monkeypatch):
    brain = tmp_path / "brain"
    good = _write_transcript(brain, MAIN_UUID_1, "transcript.jsonl", age_h=1.0)
    _write_transcript(brain, MAIN_UUID_2, "transcript.jsonl", age_h=60.0)
    known = _write_transcript(brain, SUB_UUID_1, "transcript.jsonl", age_h=1.0)
    claimed = _write_transcript(brain, SUB_UUID_2, "transcript.jsonl", age_h=1.0)
    excluded = _write_transcript(brain, PROBE_UUID, "transcript.jsonl", age_h=1.0)
    st_claimed = os.stat(claimed)

    opened: list[str] = []
    real_match = cw.antigravity_session_matches

    def spy_match(path, wanted, **kw):
        opened.append(path)
        return real_match(path, wanted, **kw)

    monkeypatch.setattr(cw, "antigravity_session_matches", spy_match)
    specs = [{
        "root": str(brain),
        "pattern": "transcript*.jsonl",
        "recursive": True,
        "max_age_h": 48,
        "exclude_globs": [f"{PROBE_UUID}/*"],
        "antigravity_session": True,
    }]
    got = cw.discover_candidates(
        specs,
        known={os.path.normcase(str(known))},
        known_inodes={(int(st_claimed.st_dev), int(st_claimed.st_ino))},
        now_s=NOW,
    )
    assert got == [str(good)]
    assert opened == [str(good)]
    assert excluded.exists()


@pytest.mark.parametrize("bad_full_raw", [
    "",
    '{"step_index":0,"source":"USER_EXPLICIT","type":"USER_INPUT","content":"Session resumed. Per GEMINI.md: hi"}',
    '{"step_index":0,"source":"SYSTEM","type":"USER_INPUT","content":"Session resumed. Per GEMINI.md: hi"}\n',
    '{"step_index":0,"source":"USER_EXPLICIT","type":"USER_INPUT","content":"Investigate subagent task"}\n',
])
def test_route1_good_compact_with_invalid_full_keeps_compact_until_full_validates(tmp_path, bad_full_raw):
    brain = tmp_path / "brain"
    specs = [{"root": str(brain), "pattern": "transcript*.jsonl", "recursive": True, "antigravity_session": True}]
    ident = _ag_ident(tmp_path, specs)
    run = AgRun(tmp_path, ident)

    compact_bad = _write_transcript(brain, MAIN_UUID_1, "transcript.jsonl", age_h=0.5)
    full_bad = _write_transcript(brain, MAIN_UUID_1, "transcript_full.jsonl", age_h=0.4, raw_text=bad_full_raw)
    assert cw.antigravity_session_matches(str(full_bad), {"session_id_file": None, "startup_prefixes": cw.DEFAULT_ANTIGRAVITY_STARTUP_PREFIXES}) is False

    compact_ctrl = _write_transcript(brain, MAIN_UUID_2, "transcript.jsonl", age_h=0.3)
    full_ctrl = _write_transcript(brain, MAIN_UUID_2, "transcript_full.jsonl", age_h=0.2)

    res1 = run.pass_()
    assert res1["outcome"] == "ok"
    ledger1 = cw.read_ledger(os.path.join(run.store, "enrollment.json"))
    enrolled1 = {os.path.normcase(e["path"]) for e in ledger1["files"].values()}
    assert os.path.normcase(str(compact_bad)) in enrolled1
    assert os.path.normcase(str(full_bad)) not in enrolled1
    assert os.path.normcase(str(full_ctrl)) in enrolled1
    assert os.path.normcase(str(compact_ctrl)) not in enrolled1

    # Once the invalid/partial full sibling completes a valid main-session first record, the next pass upgrades cleanly.
    _write_transcript(brain, MAIN_UUID_1, "transcript_full.jsonl", age_h=0.1)
    res2 = run.pass_(advance_s=cw.DISCOVER_INTERVAL_S + 1.0)
    assert res2["outcome"] == "ok"
    ledger2 = cw.read_ledger(os.path.join(run.store, "enrollment.json"))
    enrolled2 = {os.path.normcase(e["path"]) for e in ledger2["files"].values()}
    assert os.path.normcase(str(full_bad)) in enrolled2
    assert os.path.normcase(str(compact_bad)) not in enrolled2
    assert os.path.normcase(str(compact_bad)) in (ledger2.get("superseded") or {})


@pytest.mark.parametrize("bad_full_raw", [
    "",
    '{"step_index":0,"source":"USER_EXPLICIT","type":"USER_INPUT","content":"Session resumed. Per GEMINI.md: hi"}',
    '{"step_index":0,"source":"SYSTEM","type":"USER_INPUT","content":"Session resumed. Per GEMINI.md: hi"}\n',
])
def test_route2_persisted_compact_skips_invalid_full_without_error_then_upgrades_across_cutover(tmp_path, bad_full_raw):
    brain = tmp_path / "brain"
    sid_file = tmp_path / "antigravity-conversation.id"
    sid_file.write_text(MAIN_UUID_1 + "\n", encoding="utf-8")
    specs = [{
        "root": str(brain),
        "pattern": "transcript*.jsonl",
        "recursive": True,
        "antigravity_session": {
            "session_id_file": str(sid_file),
            "startup_prefixes": list(cw.DEFAULT_ANTIGRAVITY_STARTUP_PREFIXES),
        },
    }]
    ident = _ag_ident(tmp_path, specs)
    run = AgRun(tmp_path, ident)

    compact_p = _write_transcript(brain, MAIN_UUID_1, "transcript.jsonl", age_h=0.5)
    assert run.pass_()["outcome"] == "ok"
    ledger_initial = cw.read_ledger(os.path.join(run.store, "enrollment.json"))
    assert {os.path.normcase(e["path"]) for e in ledger_initial["files"].values()} == {os.path.normcase(str(compact_p))}

    # Session ID file cuts over to NEW, and an empty/partial/wrong-source full sibling appears after enrollment.
    sid_file.write_text("NEW\n", encoding="utf-8")
    full_p = _write_transcript(brain, MAIN_UUID_1, "transcript_full.jsonl", age_h=0.4, raw_text=bad_full_raw)
    res_bad = run.pass_(advance_s=1.0)
    assert res_bad["outcome"] == "ok"
    assert run.status["antigravity-prod"].get("context_error") is None
    assert "antigravity-prod" not in run.quarantined
    ledger_mid = cw.read_ledger(os.path.join(run.store, "enrollment.json"))
    assert {os.path.normcase(e["path"]) for e in ledger_mid["files"].values()} == {os.path.normcase(str(compact_p))}

    # Good full control: once the full sibling has a valid first record, the already-enrolled session upgrades cleanly.
    _write_transcript(brain, MAIN_UUID_1, "transcript_full.jsonl", age_h=0.1)
    res_good = run.pass_(advance_s=1.0)
    assert res_good["outcome"] == "ok"
    ledger_final = cw.read_ledger(os.path.join(run.store, "enrollment.json"))
    assert {os.path.normcase(e["path"]) for e in ledger_final["files"].values()} == {os.path.normcase(str(full_p))}
    assert os.path.normcase(str(compact_p)) in (ledger_final.get("superseded") or {})


def test_route2_preload_box_inodes_skips_invalid_full_upgrade_on_startup_and_upgrades_valid(tmp_path):
    brain = tmp_path / "brain"
    specs = [{"root": str(brain), "pattern": "transcript*.jsonl", "recursive": True, "antigravity_session": True}]
    ident = _ag_ident(tmp_path, specs)
    store_root = tmp_path / "ctx_store"
    ident["context"]["store_dir"] = str(store_root / ident["name"])
    run = AgRun(tmp_path, ident)

    compact_p = _write_transcript(brain, MAIN_UUID_1, "transcript.jsonl", age_h=0.5)
    assert run.pass_()["outcome"] == "ok"

    full_p = _write_transcript(brain, MAIN_UUID_1, "transcript_full.jsonl", age_h=0.4, raw_text="")
    dirs, clashes = cw._seat_store_dirs([ident], str(store_root))
    box_inodes: dict = {}
    quarantined: set = set()
    status: dict = {}
    cw._preload_box_inodes(
        [ident], [ident], dirs, clashes, str(store_root),
        box_enrolled_inodes=box_inodes,
        quarantined_seats=quarantined,
        set_status=lambda n, **kw: status.setdefault(n, {}).update(kw),
        note=lambda _m: None,
        now_fn=lambda: "2026-11-04T19:00:00Z",
    )
    assert not quarantined
    ledger_after_bad = cw.read_ledger(os.path.join(dirs["antigravity-prod"], "enrollment.json"))
    assert {os.path.normcase(e["path"]) for e in ledger_after_bad["files"].values()} == {os.path.normcase(str(compact_p))}

    _write_transcript(brain, MAIN_UUID_1, "transcript_full.jsonl", age_h=0.1)
    box_inodes2: dict = {}
    cw._preload_box_inodes(
        [ident], [ident], dirs, clashes, str(store_root),
        box_enrolled_inodes=box_inodes2,
        quarantined_seats=quarantined,
        set_status=lambda n, **kw: status.setdefault(n, {}).update(kw),
        note=lambda _m: None,
        now_fn=lambda: "2026-11-04T19:01:00Z",
    )
    ledger_after_good = cw.read_ledger(os.path.join(dirs["antigravity-prod"], "enrollment.json"))
    assert {os.path.normcase(e["path"]) for e in ledger_after_good["files"].values()} == {os.path.normcase(str(full_p))}


def test_explicit_pin_upgrades_immediately_even_when_discover_uses_antigravity_session(tmp_path):
    brain = tmp_path / "brain"
    pinned_compact = _write_transcript(brain, SUB_UUID_1, "transcript.jsonl", content="Custom pinned session")
    pinned_full = _write_transcript(brain, SUB_UUID_1, "transcript_full.jsonl", content="Custom pinned session")
    specs = [{"root": str(brain), "pattern": "transcript*.jsonl", "recursive": True, "antigravity_session": True}]
    ident = _ag_ident(tmp_path, specs)
    ident["context"]["pins"] = [str(pinned_compact)]
    run = AgRun(tmp_path, ident)
    assert run.pass_()["outcome"] == "ok"
    ledger = cw.read_ledger(os.path.join(run.store, "enrollment.json"))
    assert {os.path.normcase(e["path"]) for e in ledger["files"].values()} == {os.path.normcase(str(pinned_full))}


def test_session_id_file_accepts_utf8_bom(tmp_path):
    brain = tmp_path / "brain"
    sid_file = tmp_path / "antigravity-conversation.id"
    sid_file.write_bytes(("\ufeff" + MAIN_UUID_1 + "\r\n").encode("utf-8"))
    good = _write_transcript(brain, MAIN_UUID_1, "transcript.jsonl")
    _write_transcript(brain, MAIN_UUID_2, "transcript.jsonl")
    specs = [{
        "root": str(brain),
        "pattern": "transcript*.jsonl",
        "recursive": True,
        "antigravity_session": {"session_id_file": str(sid_file)},
    }]
    assert cw.discover_candidates(specs, known=set(), known_inodes=set(), now_s=NOW) == [str(good)]
