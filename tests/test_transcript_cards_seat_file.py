"""--seat-file: a transcript of this seat that holds no marker is read when it is named (tools/transcript_cards).

The load hook (#217) names a continuation transcript of the seat's session: the harness opens a new file for it,
and none of its own prompts holds `echo <seat>-shell-ok`, so marker discovery never picks it. The hook passes
`--seat-file NAME` once per name, in one loader stage, when the loader declares the flag with action="append".

Checked:
  * a named transcript with no marker is read and its card made; unnamed, it is not;
  * the name is kept in cursor.json, so a later run without the flag reads it, and so does --rebuild;
  * the flag repeats, and the report lists the names;
  * a path, a name that is not there, a case-different name, a non-.jsonl, a state-looking name, a link, a
    folder: refused with exit 2 and nothing written;
  * the flag with --codex-sessions or --kimi-sessions is refused;
  * the declaration matches the hook's two source patterns (load_hook.SEAT_FILE_FLAG_RE and its append test).

Run:  python -m pytest tests/test_transcript_cards_seat_file.py -q -p no:cacheprovider
Mutants (each seen to fail a test here): run() drops seat_files; main() does not pass seat_file to run();
seat_file_problem accepts any name; the exact-listdir check removed; the link check removed.
"""
import json
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LOADER = ROOT / "tools" / "transcript_cards" / "transcript_cards.py"
sys.path.insert(0, str(LOADER.parent))
import transcript_cards as tc  # noqa: E402

SEAT = "txxxx"


def _typed(uuid, text, session, ts):
    return {"type": "user", "uuid": uuid, "timestamp": ts, "sessionId": session, "promptSource": "typed",
            "origin": {"kind": "human"}, "parentUuid": "p-" + uuid, "message": {"role": "user", "content": text}}


def _write(path, records):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


@pytest.fixture
def proj(tmp_path):
    """A project folder: M.jsonl holds the marker, C.jsonl is the same seat's continuation and holds none."""
    p = tmp_path / "proj"
    p.mkdir()
    _write(p / "M.jsonl", [_typed("m1", "echo %s-shell-ok" % SEAT, "S1", "2026-10-10T18:00:00.000Z"),
                           _typed("m2", "Ship the loader change to the gate tonight.", "S1",
                                  "2026-10-10T18:01:00.000Z")])
    _write(p / "C.jsonl", [_typed("c1", "Keep the continuation's decisions on the same seat.", "S2",
                                  "2026-10-10T18:30:00.000Z")])
    return p


def _uuids(out):
    path = Path(out) / "cards.jsonl"
    if not path.exists():
        return set()
    cards = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return {c["source"].get("transcript_uuid") or c["source"]["record_uuid"] for c in cards}


def _main(capsys, *args):
    rc = tc.main(list(args))
    out = capsys.readouterr().out
    return rc, (json.loads(out) if rc == 0 else None)


def test_unnamed_the_continuation_is_not_read(proj, tmp_path, capsys):
    rc, report = _main(capsys, "--project-dir", str(proj), "--seat", SEAT, "--out", str(tmp_path / "o"))
    assert rc == 0 and report["files_read"] == ["M.jsonl"] and report["seat_files_named"] == []
    assert "c1" not in _uuids(tmp_path / "o")


def test_a_named_continuation_is_read_and_kept_in_the_cursor(proj, tmp_path, capsys):
    out = tmp_path / "o"
    rc, report = _main(capsys, "--project-dir", str(proj), "--seat", SEAT, "--out", str(out), "--seat-file", "C.jsonl")
    assert rc == 0 and report["files_read"] == ["C.jsonl", "M.jsonl"] and report["seat_files_named"] == ["C.jsonl"]
    assert {"m2", "c1"} <= _uuids(out)
    cursor = json.loads((out / "cursor.json").read_text(encoding="utf-8"))
    assert cursor["seat_files"].get("C.jsonl") is True

    _write(proj / "C.jsonl", [_typed("c1", "Keep the continuation's decisions on the same seat.", "S2",
                                     "2026-10-10T18:30:00.000Z"),
                              _typed("c2", "Hold the next tree until the fix lands.", "S2",
                                     "2026-10-10T18:40:00.000Z")])
    rc, report = _main(capsys, "--project-dir", str(proj), "--seat", SEAT, "--out", str(out))
    assert rc == 0 and "C.jsonl" in report["files_read"] and "c2" in _uuids(out), "a later run reads it unnamed"


def test_rebuild_keeps_a_named_file(proj, tmp_path, capsys):
    out = tmp_path / "o"
    _main(capsys, "--project-dir", str(proj), "--seat", SEAT, "--out", str(out), "--seat-file", "C.jsonl")
    rc, report = _main(capsys, "--project-dir", str(proj), "--seat", SEAT, "--out", str(out), "--rebuild",
                       "--seat-file", "C.jsonl")
    assert rc == 0 and "C.jsonl" in report["files_read"] and "c1" in _uuids(out)


def test_the_flag_repeats(proj, tmp_path, capsys):
    _write(proj / "D.jsonl", [_typed("d1", "Name the second continuation too.", "S3", "2026-10-10T18:50:00.000Z")])
    out = tmp_path / "o"
    rc, report = _main(capsys, "--project-dir", str(proj), "--seat", SEAT, "--out", str(out),
                       "--seat-file", "C.jsonl", "--seat-file", "D.jsonl")
    assert rc == 0 and report["seat_files_named"] == ["C.jsonl", "D.jsonl"]
    assert {"c1", "d1"} <= _uuids(out)


def test_a_continuation_alone_does_not_exit_3_when_named(tmp_path, capsys):
    p = tmp_path / "solo"
    p.mkdir()
    _write(p / "C.jsonl", [_typed("c1", "Only a continuation is here.", "S2", "2026-10-10T18:30:00.000Z")])
    assert tc.main(["--project-dir", str(p), "--seat", SEAT, "--out", str(tmp_path / "a")]) == 3
    capsys.readouterr()
    rc, report = _main(capsys, "--project-dir", str(p), "--seat", SEAT, "--out", str(tmp_path / "b"),
                       "--seat-file", "C.jsonl")
    assert rc == 0 and report["files_read"] == ["C.jsonl"]


@pytest.mark.parametrize("name, why", [
    ("../C.jsonl", "not a path"),
    ("sub" + os.sep + "C.jsonl", "not a path"),
    ("missing.jsonl", "exactly that name"),
    ("c.JSONL", "exactly that name"),
    ("notes.txt", ".jsonl"),
    ("state.jsonl", "state or key material"),
    ("folder.jsonl", "regular file"),
])
def test_a_bad_name_is_refused_and_nothing_is_written(proj, tmp_path, capsys, name, why):
    (proj / "notes.txt").write_text("x", encoding="utf-8")
    (proj / "state.jsonl").write_text("{}\n", encoding="utf-8")
    (proj / "folder.jsonl").mkdir()
    out = tmp_path / "o"
    with pytest.raises(SystemExit) as exc:
        tc.main(["--project-dir", str(proj), "--seat", SEAT, "--out", str(out), "--seat-file", name])
    assert exc.value.code == 2
    assert why in capsys.readouterr().err
    assert not out.exists()


def test_a_link_is_refused(proj, tmp_path, capsys):
    try:
        os.symlink(proj / "C.jsonl", proj / "L.jsonl")
    except (OSError, NotImplementedError):
        pytest.skip("no symbolic links here")
    with pytest.raises(SystemExit) as exc:
        tc.main(["--project-dir", str(proj), "--seat", SEAT, "--out", str(tmp_path / "o"), "--seat-file", "L.jsonl"])
    assert exc.value.code == 2 and "regular file" in capsys.readouterr().err


def test_seat_file_problem_on_a_link_without_privilege(proj, monkeypatch):
    """The link branch on a box that cannot make links (Windows without Developer Mode)."""
    real = os.path.islink
    monkeypatch.setattr(tc.os.path, "islink", lambda p: str(p).endswith("C.jsonl") or real(p))
    assert tc.seat_file_problem(str(proj), "C.jsonl") == "not a regular file"


@pytest.mark.parametrize("flag", ["--codex-sessions", "--kimi-sessions"])
def test_only_with_a_project_dir(tmp_path, capsys, flag):
    with pytest.raises(SystemExit) as exc:
        tc.main([flag, str(tmp_path), "--seat", SEAT, "--out", str(tmp_path / "o"), "--seat-file", "C.jsonl"])
    assert exc.value.code == 2 and "--project-dir" in capsys.readouterr().err


def test_the_declaration_matches_the_load_hooks_patterns():
    """#217's load_hook finds the flag with SEAT_FILE_FLAG_RE and reads action="append" from the same call. Both
    patterns are copied here so this test holds before #217 lands; the call is on one line so both find it."""
    source = LOADER.read_text(encoding="utf-8")
    flag = re.search(r"""add_argument\(\s*["']--seat-file["']""", source)
    assert flag is not None
    line = source[flag.start():source.index("\n", flag.start())]
    assert re.search(r"""\baction\s*=\s*["']append["']""", line)
