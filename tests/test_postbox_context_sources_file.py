"""The upload child's source list past Windows' command-line limit (dxxxxx 32c666, 8 Nov 2026).

HQB's codex-bxxxxx-cli enrolled 431 rollouts; the worker passed each as `--source <path>`, the command line passed
32,767 characters, Windows refused to start the child (WinError 206), `_run_subprocess` turned that into rc -1, and
the seat stopped uploading on 7 Nov (time withheld) with no log line. These tests pin both halves: the list moves to a
private file past the budget, and a child that cannot start is logged once.
Database-free."""
from __future__ import annotations

import datetime
import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PB_DIR = ROOT / "tools" / "postbox"
if str(PB_DIR) not in sys.path:
    sys.path.insert(0, str(PB_DIR))
TC_DIR = ROOT / "tools" / "transcript_cards"
if str(TC_DIR) not in sys.path:
    sys.path.insert(0, str(TC_DIR))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("pb_ctx_sources_cw", PB_DIR / "context_worker.py")
cw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cw)
import context_upload as cu  # noqa: E402

DRAINED = json.dumps({"status": "drained", "batches_committed": 0, "items_committed": 0, "context_held": []})


def _ident(tmp_path: Path, name: str, n_pins: int) -> dict:
    root = tmp_path / ("root_" + name)
    root.mkdir()
    pins = []
    for i in range(n_pins):
        # rollout-like names: about 110 characters of path each, as on HQB
        p = root / ("rollout-2026-11-08T16-40-12-019a5b2c-%04d-7d3e-9f10-aabbccddeeff-padding-to-length.jsonl" % i)
        p.write_text('{"type":"session_meta"}\n', encoding="utf-8")
        pins.append(str(p))
    state = tmp_path / (name + "-state.json")
    state.write_text("{}", encoding="utf-8")
    (tmp_path / "client").mkdir(exist_ok=True)
    return {
        "name": name, "cli": "codex", "enabled": True, "python": sys.executable,
        "client": str(tmp_path / "client"), "state": str(state), "base_url": "http://127.0.0.1:6310",
        "context": {"enabled": True, "seat": name, "dialect": "codex", "app_root": str(ROOT),
                    "allowed_roots": [str(root)], "pins": pins},
    }


def _pass(ident, tmp_path, statuses, notes, runner, seat_notes=None):
    return cw.run_seat_pass(
        ident,
        store_dir=str(tmp_path / ("st_" + ident["name"])),
        rate_controller=cw.RateController(
            probe_fn=lambda _u: True,
            now_fn=lambda: datetime.datetime(2026, 11, 8, 16, 0, tzinfo=datetime.timezone.utc)),
        box_enrolled_inodes={},
        quarantined_seats=set(),
        missing_driver_logged=set(),
        set_status=lambda n, **k: statuses.setdefault(n, {}).update(k),
        note=notes.append,
        runner=runner,
        seat_notes=seat_notes,
    )


def _sources_in(argv):
    return [argv[i + 1] for i, tok in enumerate(argv) if tok == "--source"]


def test_a_short_source_list_stays_inline(tmp_path):
    ident = _ident(tmp_path, "few", 3)
    argvs, statuses, notes = [], {}, []
    r = _pass(ident, tmp_path, statuses, notes, lambda argv, cwd, t, env=None: argvs.append(argv) or (0, DRAINED, ""))
    assert r["outcome"] == "ok", statuses
    assert "--sources-file" not in argvs[0]
    assert len(_sources_in(argvs[0])) == 3
    assert not (tmp_path / "st_few" / cw.UPLOAD_SOURCES_FILE).exists()


def test_a_long_source_list_goes_to_a_private_file_and_the_command_line_fits(tmp_path):
    """RED before the fix: 431 sources made a command line of about 50,000 characters."""
    ident = _ident(tmp_path, "many", 431)
    argvs, statuses, notes = [], {}, []
    r = _pass(ident, tmp_path, statuses, notes, lambda argv, cwd, t, env=None: argvs.append(argv) or (0, DRAINED, ""))
    assert r["outcome"] == "ok", statuses
    argv = argvs[0]
    assert len(subprocess.list2cmdline(argv)) < 32767, "Windows would refuse to start this child (WinError 206)"
    assert len(subprocess.list2cmdline(argv)) <= cw.CMDLINE_BUDGET_CHARS < 32767
    assert _sources_in(argv) == []
    path = argv[argv.index("--sources-file") + 1]
    assert Path(path).parent == (tmp_path / "st_many").resolve() or Path(path).parent == Path(os.path.abspath(tmp_path / "st_many"))
    listed = json.loads(Path(path).read_text(encoding="utf-8"))["sources"]
    assert sorted(os.path.normcase(s) for s in listed) == sorted(
        os.path.normcase(os.path.abspath(p)) for p in ident["context"]["pins"])
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_the_child_reads_the_sources_file(tmp_path, monkeypatch):
    seen = {}

    def fake_run_pass(cfg, transport):
        seen["sources"] = list(cfg.sources)

        class _R:
            def as_json(self):
                return {"status": "drained", "batches_committed": 0, "items_committed": 0,
                        "accounting": {"emitted": 0}, "context_held": []}

            def exit_code(self):
                return cu.EXIT_OK
        return _R()

    monkeypatch.setattr(cu, "run_pass", fake_run_pass)
    monkeypatch.setattr(cu, "NodeTransport", lambda *a, **k: object())
    f = tmp_path / "upload-sources.json"
    f.write_text(json.dumps({"sources": ["a.jsonl", "b.jsonl"]}), encoding="utf-8")
    base = ["--state-file", "s", "--base-url", "http://x", "--store-dir", str(tmp_path), "--seat", "x",
            "--dialect", "codex", "--json"]
    assert cu.main(base + ["--sources-file", str(f)]) == cu.EXIT_OK
    assert seen["sources"] == ["a.jsonl", "b.jsonl"]
    assert cu.main(base + ["--source", "c.jsonl", "--sources-file", str(f)]) == cu.EXIT_OK
    assert seen["sources"] == ["c.jsonl", "a.jsonl", "b.jsonl"]


def test_a_bad_or_missing_source_list_is_a_usage_fault(tmp_path):
    base = ["--state-file", "s", "--base-url", "http://x", "--store-dir", str(tmp_path), "--seat", "x",
            "--dialect", "codex", "--json"]
    assert cu.main(base) == cu.EXIT_USAGE
    assert cu.main(base + ["--sources-file", str(tmp_path / "absent.json")]) == cu.EXIT_USAGE
    for body in ('{"sources": "a"}', '["a"]', '{"sources": ["a", ""]}', '{"sources": [1]}', "not json"):
        f = tmp_path / "bad.json"
        f.write_text(body, encoding="utf-8")
        assert cu.main(base + ["--sources-file", str(f)]) == cu.EXIT_USAGE, body


def test_a_child_that_cannot_start_is_logged_once_per_error(tmp_path):
    """RED before the fix: rc -1 set the status and wrote no note, so the stop was silent."""
    ident = _ident(tmp_path, "nostart", 2)
    statuses, notes, seat_notes = {}, [], {}
    fail = lambda argv, cwd, t, env=None: (-1, "", "OSError: [WinError 206] The filename or extension is too long")
    for _ in range(3):
        r = _pass(ident, tmp_path, statuses, notes, fail, seat_notes)
        assert r["outcome"] == "error"
    assert statuses["nostart"]["context_error"] == "rc=-1"
    failed = [n for n in notes if "context_upload failed" in n]
    assert failed == ["nostart: context_upload failed (rc=-1): OSError: [WinError 206] The filename or extension is "
                      "too long"]
    # a success clears it, so the next failure is logged again
    _pass(ident, tmp_path, statuses, notes, lambda argv, cwd, t, env=None: (0, DRAINED, ""), seat_notes)
    _pass(ident, tmp_path, statuses, notes, fail, seat_notes)
    assert len([n for n in notes if "context_upload failed" in n]) == 2
