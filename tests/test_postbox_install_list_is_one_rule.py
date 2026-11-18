"""The postbox's install instructions name one rule, never a hand-copied list of files, and it is the update's rule.

WHY. Four places listed the files to copy into a seat's postbox folder, and every list had drifted from the
others and from the code (REFACTOR-QUEUE-2026-11 C3). `postbox.py` imports `codex_front`, `graphline`,
`owedline`, `context_worker` and `parked` inside `try`, and `turn_reporter` and `turnstate` inside a function,
so a folder built from a short list starts and silently runs without those features. The rule is now "every
`.py` file directly in `tools/postbox/` (what `git ls-tree --name-only HEAD tools/postbox/` lists), except
`kimi_arm_hook.py`".

pxx's review of #647: the first version of the rule used `git ls-files 'tools/postbox/*.py'`, whose `*` crosses
`/`, so it named 36 files (14 nested, from subfolders a flat copy would mangle) while `docs/client-update.md`
compares 22. These tests therefore check the FILE SET as well as the wording: the install rule and the update
loop use the same selector, and that selector's set holds every module `postbox.py` can import and nothing
from a subfolder.

Read from the files and git, no database.

Run:  pytest tests/test_postbox_install_list_is_one_rule.py -q     (no database)
"""
from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POSTBOX = ROOT / "tools" / "postbox"
SELECTOR = "git ls-tree --name-only HEAD tools/postbox/"
RULE = (f"every `.py` file directly in `tools/postbox/` (what `{SELECTOR}` lists, the set docs/client-update.md "
        "compares), except `kimi_arm_hook.py`")
#: Where a seat is told what to copy, and how many times each says it.
PLACES = {
    ROOT / "README.md": 1,
    POSTBOX / "README.md": 2,
    POSTBOX / "SEAT-SETUP.md": 1,
}
#: The shape of the old hand lists: three or more postbox modules named in a row.
HAND_LIST = re.compile(r"`postbox\.py`, `postbox_os\.py`, `postbox_child\.py`")


def _flat(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_each_install_place_states_the_rule_and_lists_no_files():
    for path, count in PLACES.items():
        text = _flat(path)
        assert text.count(RULE) == count, f"{path.relative_to(ROOT)} should state the copy rule {count} time(s)"
        assert not HAND_LIST.search(text), f"{path.relative_to(ROOT)} lists postbox files by hand again"
        assert "git ls-files 'tools/postbox/*.py'" not in text, f"{path.relative_to(ROOT)} uses the nested selector"


def test_the_hand_list_pattern_matches_the_old_text():
    """Positive control: the pattern above catches the list this file replaced."""
    old = ("copy `postbox.py`, `postbox_os.py`, `postbox_child.py`, `meshy_channel.py`, `wait_next.py`, "
           "`graphline.py`, `delivery_bank.py` and `mark_handled.py`")
    assert HAND_LIST.search(old)


def test_the_install_rule_and_the_update_loop_select_the_same_files():
    """The update loop in docs/client-update.md lists the folder with the same ls-tree (at <sha>, not HEAD) and
    keeps its .py files; the install rule must name that set and no other."""
    loop = _flat(ROOT / "docs" / "client-update.md")
    assert "git ls-tree --name-only <sha> tools/postbox/ | grep '\\.py$'" in loop, \
        "docs/client-update.md no longer selects the postbox files the way the install rule does"
    listed = subprocess.run(["git", "-C", str(ROOT), "ls-tree", "--name-only", "HEAD", "tools/postbox/"],
                            capture_output=True, text=True, check=True).stdout.split()
    py = [p for p in listed if p.endswith(".py")]
    assert py and all(p.count("/") == 2 for p in py), py          # tools/postbox/<file>.py, nothing nested
    nested = subprocess.run(["git", "-C", str(ROOT), "ls-files", "tools/postbox/*.py"], capture_output=True,
                            text=True, check=True).stdout.split()
    assert len(nested) > len(py), "control: the pathspec form must select more (it crosses '/'), or this proves nothing"


def test_every_module_the_postbox_imports_is_in_the_rules_set():
    """The rule is only right while everything postbox.py can import is a top-level file of tools/postbox/."""
    listed = subprocess.run(["git", "-C", str(ROOT), "ls-tree", "--name-only", "HEAD", "tools/postbox/"],
                            capture_output=True, text=True, check=True).stdout.split()
    rule_set = {Path(p).stem for p in listed if p.endswith(".py")}
    local = {p.stem for p in POSTBOX.glob("*.py")}
    imported = set()
    for node in ast.walk(ast.parse((POSTBOX / "postbox.py").read_text(encoding="utf-8"))):
        names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                 else [node.module] if isinstance(node, ast.ImportFrom) and node.module and not node.level else [])
        imported.update(n.split(".")[0] for n in names if n.split(".")[0] in local)
    assert {"delivery_bank", "postbox_os", "context_worker", "turn_reporter"} <= imported, imported
    assert imported <= rule_set, sorted(imported - rule_set)
