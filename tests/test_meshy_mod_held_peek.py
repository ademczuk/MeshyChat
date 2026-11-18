"""meshy-mod's held-list peek runs isolated (pxx 34f36f P2), checked with real processes.

The Stop guard runs HELD_PY as `python -I -B -c HELD_PY <client> <log> <identity>`, in the client's folder. Without
-I, sys.path[0] is the working folder, and HELD_PY's first statement imports json before it inserts tools/postbox:
a json.py in the folder the session runs from (an extracted download, say) would run on every stop. This runs the
exact HELD_PY that meshy.js defines from a folder holding such a json.py, and, as the control, the old call, which
must run it. Database-free; no server.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MESHY_JS = ROOT / "tools" / "postbox" / "mod" / "meshy-mod" / "hooks" / "meshy.js"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
PYTHON = re.sub(r"pythonw(\.exe)?$", r"python\1", sys.executable)   # a console interpreter, so stdout is captured


def held_py() -> str:
    """HELD_PY as meshy.js defines it: single-quoted JS strings joined with +, newlines written as \\n."""
    src = MESHY_JS.read_text(encoding="utf-8")
    m = re.search(r"const HELD_PY = ((?:'[^']*'\s*\+?\s*)+)\r?\n", src)
    assert m, "HELD_PY is not where this test reads it"
    return "".join(re.findall(r"'([^']*)'", m.group(1))).replace("\\n", "\n")


def download_with_json_py(tmp_path: Path) -> tuple[Path, Path]:
    folder, mark = tmp_path / "download", tmp_path / "json-ran.mark"
    folder.mkdir()
    (folder / "json.py").write_text("import pathlib\npathlib.Path(%r).write_text('x')\nraise SystemExit(3)\n" % str(mark),
                                    encoding="utf-8")
    return folder, mark


def peek(flags: list[str], cwd: Path, log: Path) -> subprocess.CompletedProcess:
    return subprocess.run([PYTHON, *flags, "-c", held_py(), str(ROOT), str(log), "seat-prod"], cwd=cwd,
                          capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW)


def test_the_peek_never_runs_a_json_py_from_the_folder_it_starts_in(tmp_path):
    folder, mark = download_with_json_py(tmp_path)
    r = peek(["-I", "-B"], folder, tmp_path / "seat.log")
    assert not mark.exists(), "the json.py in the working folder ran"
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == {"held": [], "error": None}


def test_the_old_call_from_that_folder_does_run_it(tmp_path):
    """The positive control: the probe fires on the call without -I, so its silence above means something."""
    folder, mark = download_with_json_py(tmp_path)
    peek(["-B"], folder, tmp_path / "seat.log")
    assert mark.exists(), "the control did not fire, so the test above proves nothing"
