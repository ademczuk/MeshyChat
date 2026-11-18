"""docs/client-update.md updates the mod, not only the postbox files (backlog item 6, 2026-10-10).

WHY. The page compared only the postbox's `.py` files. The mod, `tools/postbox/mod/meshy-mod`, is a folder of
its own: a box copies it into its plugin marketplace, Claude Code installs that into its cache, and nothing on
the page touched either copy. Measured read-only on pxx on 2026-10-10: every postbox file equal to main
`6c67f65`, both copies of the mod main's `70ee8e0` of 2026-10-05, and 11 later commits on main touching the
mod's folder (divergence 094's question hook among them). The installed `plugin.json` said `0.6.0` while the
repository's says `0.1.0` by design (SEAT-SETUP.md, "Updating the mod"), so the version tells nothing and the
page compares files.

WHAT IS PINNED, from the page and the repository (no database, no server, no seat's files):
  * the postbox's ordered steps send the reader to "The mod" before the relaunch, and the relaunch names the mod;
  * "The mod" names the repository's folder and ends in a relaunch;
  * the repository version both pages name is the repository's own;
  * the page's comparison block, RUN in bash on copies built from HEAD in tmp_path: a copy whose only difference
    is a bumped, re-laid-out plugin.json prints `same` once for every tracked file; a changed file prints
    REPLACE, a missing one COPY, a CRLF copy `same`; a plugin.json field other than the version prints REPLACE;
  * plugin.json is compared as JSON (Codex's review of e99dc1c): reordered keys, tabs and CRLF are `same`; a space
    inside a value and a `version` below the top level are REPLACE; a bump that lost its comma or a quote, or a
    version that is no string, STOPs (exit 1) and never prints `same` for it;
  * the block fails closed (Codex's review of e99dc1c): a sha256sum that is missing, exits 127 or prints nothing,
    a PY that runs no Python, and a sha that lists no file each print one STOP line and exit 1, and no file is
    called `same`;
  * Codex's review of 76c1241: the step before the install keeps the repository's version (memory-bxxxxx's ruling)
    and picks one only for a <sha> still on 0.1.0; a `git ls-tree` that fails, even after listing some paths, and a
    MOD the block cannot list each STOP; a file in the copy that <sha> does not track prints EXTRA, except what
    Claude Code writes on load (the mod's .gitignore);
  * the boxes table has pxx's row, naming the LaunchAgent label the repository's template gives.

The block runs where bash and git are found and not on Windows, whose `bash.exe` may be WSL's, which sees other
paths. HQB and HQD run the page's blocks in Git Bash; there, with only this skip lifted, 19 of 23 passed at 18c62dc (memory-bxxxxx,
10 Oct; 15 of 18 at 76c1241). The other 4 failed in their own setup, never reaching the block: the 3 symlink PATH cases
(WinError 1314) and the ls-tree "cut short" case, whose control runs the fake git script directly (WinError 193). The tests that need a working
sha256sum skip without one; the tests of a missing or failing one bring their own PATH and run without it.

Run:  pytest tests/test_client_update_covers_the_mod.py -q -p no:cacheprovider --noconftest
Mutants (each applied to the page or the repository and seen to fail a test here, 2026-10-10):
  CU1  the mod step is dropped from the postbox's order          test_the_postbox_order_updates_the_mod_before_the_relaunch
  CU2  the mod step comes after the relaunch                     test_the_postbox_order_updates_the_mod_before_the_relaunch
  CU3  the relaunch step no longer names the mod                 test_the_postbox_order_updates_the_mod_before_the_relaunch
  CU4  "The mod" loses its relaunch step                         test_the_mod_section_names_the_folder_and_ends_in_a_relaunch
  CU5  the repository's plugin.json is bumped                    test_both_pages_name_the_repositorys_own_version
  CU6  the JSON comparison keeps the version                     test_a_copy_differing_only_in_its_version_and_layout_is_same
  CU7  plugin.json is compared byte for byte like the rest       test_a_copy_differing_only_in_its_version_and_layout_is_same
  CU8  `git ls-tree` without -r (no file in a subfolder)         test_a_copy_differing_only_in_its_version_and_layout_is_same
  CU9  no COPY branch (a missing file stops the block)           test_a_changed_file_is_replace_a_missing_one_copy_and_crlf_same
  CU10 carriage returns are not stripped                         test_a_changed_file_is_replace_a_missing_one_copy_and_crlf_same
  CU11 plugin.json is never compared                             test_a_plugin_json_field_other_than_the_version_is_replace
  CU12 pxx's row is dropped from the boxes table                 test_the_boxes_table_has_pxx_with_the_templates_launch_agent
  CU13 pxx's row names a label the template does not             test_the_boxes_table_has_pxx_with_the_templates_launch_agent
  CU14 the block's checks as they were (no pipefail, no digest   test_a_missing_or_failing_checksum_stops_the_block[exits 127]
       length; Codex's repro: every file `same`, exit 0)
  CU15 `set -o pipefail` is dropped                              test_a_plugin_json_that_does_not_parse_stops_the_block (all 3)
  CU16 the digest length is not checked                          test_a_missing_or_failing_checksum_stops_the_block[prints nothing]
  CU17 the tools are not looked for first                        test_a_missing_or_failing_checksum_stops_the_block[missing]
  CU18 PY is not tried first                                     test_a_py_that_runs_no_python_stops_the_block
  CU19 plugin.json as it was: version line and whitespace out    test_a_space_inside_a_value_is_replace,
                                                                 test_a_plugin_json_that_does_not_parse_stops_the_block (all 3),
                                                                 test_reordered_keys_tabs_and_crlf_in_plugin_json_are_same
  CU20 keys are not sorted                                       test_reordered_keys_tabs_and_crlf_in_plugin_json_are_same
  CU21 a `version` is dropped from every object, not the top's   test_a_version_below_the_top_level_is_replace
  CU22 a version that is no string is let through                test_a_plugin_json_that_does_not_parse_stops_the_block[number]
  CU23 the empty listing is not stopped                          test_a_sha_without_the_mod_stops_the_block
  CU24 the version step as it was (the box picks one, always)    test_the_install_takes_the_repositorys_version
  CU25 the listing's exit status is not checked                  test_a_listing_git_could_not_finish_stops_the_block[cut short]
  CU26 no EXTRA loop                                             test_a_file_the_sha_does_not_track_is_extra
  CU27 Claude Code's own files are not left out of EXTRA         test_a_file_the_sha_does_not_track_is_extra
  CU28 the listing of MOD is not checked                         test_a_mod_folder_that_cannot_be_listed_stops_the_block
"""
from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "docs" / "client-update.md"
SEAT_SETUP = ROOT / "tools" / "postbox" / "SEAT-SETUP.md"
MOD_DIR = "tools/postbox/mod/meshy-mod/"
PLUGIN_JSON = ".claude-plugin/plugin.json"
PLIST = ROOT / "tools" / "postbox" / "launchd" / "net.meshychat.postbox.plist.template"

BASH, GIT, SHA256 = shutil.which("bash"), shutil.which("git"), shutil.which("sha256sum")
TR, CUT, CAT, FIND = shutil.which("tr"), shutil.which("cut"), shutil.which("cat"), shutil.which("find")
runs_the_block = pytest.mark.skipif(
    os.name == "nt" or not (BASH and GIT and SHA256 and TR and CUT and FIND),
    reason="needs bash, git, tr, cut, find and sha256sum, and not Windows, where System32's bash is WSL's")
runs_the_block_on_its_own_path = pytest.mark.skipif(
    os.name == "nt" or not (BASH and GIT and TR and CUT and CAT and FIND),
    reason="needs bash, git, tr, cut, cat and find, and not Windows, where System32's bash is WSL's")


def _page() -> str:
    return PAGE.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """The body of the one `## ` section whose heading starts with `heading`."""
    found = [p for p in re.split(r"^## ", text, flags=re.M) if p.startswith(heading)]
    assert len(found) == 1, f"control: one section {heading!r}, found {len(found)}"
    return found[0]


def _ordered_steps(section: str, lead: str) -> list[str]:
    """The numbered list after `lead`, one string per step, continuation lines joined."""
    assert section.count(lead) == 1, f"control: {lead!r} once in the section"
    steps: list[str] = []
    for line in section.split(lead, 1)[1].splitlines():
        m = re.match(r"^(\d+)\. (.*)$", line)
        if m:
            assert int(m.group(1)) == len(steps) + 1, f"steps numbered in order: {line[:50]!r}"
            steps.append(m.group(2))
        elif steps and line.startswith("   "):
            steps[-1] += " " + line.strip()
        elif steps and line.strip():
            break
    return steps


def test_the_postbox_order_updates_the_mod_before_the_relaunch():
    """One step of the postbox's order sends the reader to "The mod", before the one relaunch, so a single
    relaunch loads the new channel file and the new mod; the relaunch says the mod is loaded at start too.
    MUTANTS CU1 (the step is dropped), CU2 (it follows the relaunch), CU3 (the relaunch is as it was)."""
    steps = _ordered_steps(_section(_page(), "The postbox"), "Then, in this order:")
    assert len(steps) >= 6, "control: the postbox's steps are where they were"
    relaunch = [i for i, s in enumerate(steps) if s.startswith("**Relaunch")]
    mod = [i for i, s in enumerate(steps) if '"The mod"' in s]
    assert len(relaunch) == 1, f"one relaunch step: {relaunch}"
    assert len(mod) == 1, 'one step of the order sends the reader to "The mod"'
    assert mod[0] < relaunch[0], "the mod is updated before the panes are relaunched"
    assert "the old mod" in steps[relaunch[0]], "the relaunch says a running pane keeps the old mod"


def test_the_mod_section_names_the_folder_and_ends_in_a_relaunch():
    """MUTANT CU4: "The mod" loses its relaunch step (a copy replaced on disk, the panes still on the old one)."""
    section = _section(_page(), "The mod")
    assert f"`{MOD_DIR.rstrip('/')}`" in section, "the section names the repository's folder"
    assert (ROOT / MOD_DIR / PLUGIN_JSON).is_file() and (ROOT / MOD_DIR / "hooks" / "meshy.js").is_file(), \
        "control: the folder the page names holds the mod"
    steps = _ordered_steps(section, "Then, in this order:")
    assert steps and steps[-1].startswith("**Relaunch**"), f"the last step is the relaunch: {steps[-1][:60]!r}"
    assert any(s.startswith("**Set the version**") for s in steps), "a new version, or `claude plugin update` sees nothing"


def test_both_pages_name_the_repositorys_own_version():
    """The page tells a reader to ignore the version because the repository's is fixed and only a box's copy is
    bumped. If the repository starts bumping its own, that premise is gone. MUTANT CU5: plugin.json is bumped."""
    version = json.loads((ROOT / MOD_DIR / PLUGIN_JSON).read_text(encoding="utf-8"))["version"]
    page = re.search(r"repository's `\.claude-plugin/plugin\.json` keeps\s+`\"version\": \"([^\"]+)\"`",
                     _section(_page(), "The mod"))
    setup = re.search(r"[Tt]he `plugin\.json` in the repository keeps\s+`\"version\": \"([^\"]+)\"`",
                      SEAT_SETUP.read_text(encoding="utf-8"))
    assert page and setup, "control: both pages say which version the repository keeps"
    assert page.group(1) == version, f"client-update.md says {page.group(1)}, the repository has {version}"
    assert setup.group(1) == version, f"SEAT-SETUP.md says {setup.group(1)}, the repository has {version}"


def _git(*args: str) -> bytes:
    r = subprocess.run([GIT, "-C", str(ROOT), *args], capture_output=True, timeout=60)
    if r.returncode != 0:
        pytest.skip(f"not a git checkout with history: git {args[0]} said {r.stderr[:200]!r}")
    return r.stdout


def _script(sha: str) -> str:
    """The page's comparison block with `<sha>` filled in; its `cd`, `MOD=` and `PY=` lines are the reader's, and
    the test supplies all three (the working directory and the environment)."""
    blocks = re.findall(r"```bash\n(.*?)```", _section(_page(), "The mod"), flags=re.S)
    assert len(blocks) == 1, f"control: one bash block in \"The mod\", found {len(blocks)}"
    lines = blocks[0].splitlines()
    reader = [ln for ln in lines if ln.startswith(("cd <", "MOD=", "PY="))]
    assert len(reader) == 3, f"control: the block's own cd, MOD= and PY= lines: {reader}"
    script = "\n".join(ln for ln in lines if ln not in reader).replace("<sha>", sha)
    assert not re.search(r"<[a-z][^>]*>", script), "control: no placeholder is left"
    return script


@pytest.fixture
def head_copy(tmp_path):
    """The mod as a box holds it after an update to HEAD: every tracked file from HEAD, and plugin.json bumped to
    0.6.0 and laid out again by a JSON tool, as pxx's was. Returns (sha, the tracked paths, the folder)."""
    sha = _git("rev-parse", "HEAD").decode().strip()
    tracked = [p[len(MOD_DIR):] for p in _git("ls-tree", "-r", "--name-only", sha, MOD_DIR).decode().split("\n") if p]
    assert PLUGIN_JSON in tracked and "hooks/meshy.js" in tracked, f"control: the mod is tracked: {tracked}"
    copy = tmp_path / "meshy-mod"
    for rel in tracked:
        (copy / rel).parent.mkdir(parents=True, exist_ok=True)
        (copy / rel).write_bytes(_git("show", f"{sha}:{MOD_DIR}{rel}"))
    pj = copy / PLUGIN_JSON
    data = json.loads(pj.read_text(encoding="utf-8"))
    assert data["version"] != "0.6.0", "control: the box's version differs from the repository's"
    data["version"] = "0.6.0"
    original = pj.read_bytes()
    pj.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    def without_version(b: bytes) -> bytes:
        return b"\n".join(ln for ln in b.split(b"\n") if b'"version"' not in ln)
    assert without_version(pj.read_bytes()) != without_version(original), "control: the layout differs too"
    return sha, tracked, copy


def _run(sha: str, copy: Path, *, path: str | None = None, py: str | None = None) -> subprocess.CompletedProcess:
    """The block, run in bash with MOD at `copy`, PY at this test's own Python unless `py` says otherwise, and the
    caller's PATH unless `path` replaces it."""
    env = {**os.environ, "MOD": str(copy), "PY": py if py is not None else sys.executable}
    if path is not None:
        env["PATH"] = path
    return subprocess.run([BASH, "-c", _script(sha)], cwd=ROOT, env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=120)


def _stopped(r: subprocess.CompletedProcess) -> str:
    """The block stopped: exit 1, and its only line is one STOP line, so no file was called `same` or anything
    else. Returns that line."""
    lines = r.stdout.splitlines()
    assert r.returncode == 1, f"the block exits 1 when it stops: {r.returncode}, {lines}, {r.stderr[-300:]!r}"
    assert len(lines) == 1 and lines[0].startswith("STOP "), f"one STOP line and nothing else: {lines}"
    return lines[0]


def _compare(sha: str, copy: Path) -> dict[str, str]:
    r = _run(sha, copy)
    assert r.returncode == 0, f"the block failed: {r.stdout} {r.stderr}"
    out: dict[str, str] = {}
    for line in r.stdout.splitlines():
        status, _, rel = line.partition(" ")
        assert rel.strip() not in out, f"one line per file: {line!r}"
        out[rel.strip()] = status
    return out


@runs_the_block
def test_a_copy_differing_only_in_its_version_and_layout_is_same(head_copy):
    """The box's own version and a JSON tool's layout are not a difference; every tracked file gets one line.
    MUTANTS CU6 (the version line is kept), CU7 (plugin.json byte for byte), CU8 (`git ls-tree` without -r)."""
    sha, tracked, copy = head_copy
    got = _compare(sha, copy)
    assert set(got) == set(tracked), f"one line per tracked file: {sorted(got)} vs {sorted(tracked)}"
    assert {rel: s for rel, s in got.items() if s != "same"} == {}


@runs_the_block
def test_a_changed_file_is_replace_a_missing_one_copy_and_crlf_same(head_copy):
    """MUTANTS CU9 (no COPY branch: a missing file is hashed as empty and reads REPLACE) and CU10 (carriage
    returns are not stripped, so a Windows copy reads REPLACE)."""
    sha, tracked, copy = head_copy
    with (copy / "hooks" / "meshy.js").open("a", encoding="utf-8") as f:
        f.write("\n// a box's copy from another commit\n")
    (copy / "types" / "index.d.ts").unlink()
    readme = copy / "README.md"
    lf = readme.read_bytes()
    assert b"\n" in lf and b"\r\n" not in lf, "control: the README has LF line ends to convert"
    readme.write_bytes(lf.replace(b"\n", b"\r\n"))
    got = _compare(sha, copy)
    assert got.pop("hooks/meshy.js") == "REPLACE"
    assert got.pop("types/index.d.ts") == "COPY"
    assert got.pop("README.md") == "same", "a CRLF copy of the same text is the same file"
    assert set(got.values()) == {"same"}, got


@runs_the_block
def test_a_plugin_json_field_other_than_the_version_is_replace(head_copy):
    """Only the version line is the box's own; the rest of plugin.json counts. MUTANT CU11: it is never compared."""
    sha, tracked, copy = head_copy
    pj = copy / PLUGIN_JSON
    data = json.loads(pj.read_text(encoding="utf-8"))
    data["description"] = data["description"] + " Changed."
    pj.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    got = _compare(sha, copy)
    assert got.pop(PLUGIN_JSON) == "REPLACE"
    assert set(got.values()) == {"same"}, got


def test_the_boxes_table_has_pxx_with_the_templates_launch_agent():
    """pxx's row (2026-10-10) records its postbox, its identity, its listeners' client and its mod.
    MUTANTS CU12 (the row is dropped) and CU13 (it names a LaunchAgent label the template does not)."""
    label = plistlib.loads(PLIST.read_bytes())["Label"]
    section = _section(_page(), "The boxes")
    header = [ln for ln in section.splitlines() if ln.startswith("| box |")]
    assert len(header) == 1, "control: the table's header"
    width = len(header[0].strip().strip("|").split("|"))
    rows = [ln for ln in section.splitlines() if ln.startswith("| pxx ")]
    assert len(rows) == 1, f"one row for pxx, found {len(rows)}"
    cells = [c.strip() for c in rows[0].strip().strip("|").split("|")]
    assert len(cells) == width, f"the row has the header's {width} cells: {len(cells)}"
    box, postbox, identities, client, channel = cells
    assert "macOS" in box
    assert f"LaunchAgent `{label}`" in postbox, f"the template's label is {label!r}"
    assert "`~/.meshychat/postbox`" in postbox
    assert identities.startswith("1,") and "pxx" in identities
    assert "client-main" in client and "clean" in client
    assert "meshy-mod" in channel, "the row says where the box's mod stands"


def _plugin_json(copy: Path) -> Path:
    return copy / PLUGIN_JSON


@runs_the_block
def test_reordered_keys_tabs_and_crlf_in_plugin_json_are_same(head_copy):
    """A JSON tool may sort or reorder the keys, indent with tabs, write CRLF and drop the last newline: none of it
    changes what Claude Code reads. MUTANT CU20: the keys are not sorted, so the reordered copy reads REPLACE."""
    sha, tracked, copy = head_copy
    pj = _plugin_json(copy)
    data = json.loads(pj.read_text(encoding="utf-8"))
    reordered = dict(reversed(list(data.items())))
    assert list(reordered) != list(data), "control: the key order differs"
    pj.write_bytes(json.dumps(reordered, indent="\t").replace("\n", "\r\n").encode("utf-8"))
    assert b"\r\n\t" in pj.read_bytes() and not pj.read_bytes().endswith(b"\n"), "control: tabs, CRLF, no last newline"
    got = _compare(sha, copy)
    assert set(got) == set(tracked) and set(got.values()) == {"same"}, got


@runs_the_block
def test_a_space_inside_a_value_is_replace(head_copy):
    """A string value is compared as it is: `./types /index.d.ts` is another file than `./types/index.d.ts`. Codex
    reproduced the old comparison (every whitespace character out) calling it the same. MUTANT CU19."""
    sha, tracked, copy = head_copy
    pj = _plugin_json(copy)
    data = json.loads(pj.read_text(encoding="utf-8"))
    assert data["types"] == "./types/index.d.ts", "control: the field the review changed"
    data["types"] = "./types /index.d.ts"
    pj.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    got = _compare(sha, copy)
    assert got.pop(PLUGIN_JSON) == "REPLACE"
    assert set(got.values()) == {"same"}, got


@runs_the_block
def test_a_version_below_the_top_level_is_replace(head_copy):
    """Only the TOP-LEVEL version is the box's own; a `version` inside another field counts like any value.
    MUTANT CU21: every object's `version` is left out."""
    sha, tracked, copy = head_copy
    pj = _plugin_json(copy)
    data = json.loads(pj.read_text(encoding="utf-8"))
    assert isinstance(data.get("author"), dict) and "version" not in data["author"], "control: author is an object"
    data["author"]["version"] = "0.6.0"
    pj.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    got = _compare(sha, copy)
    assert got.pop(PLUGIN_JSON) == "REPLACE"
    assert set(got.values()) == {"same"}, got


@runs_the_block
@pytest.mark.parametrize("slip", ["comma", "quote", "number"])
def test_a_plugin_json_that_does_not_parse_stops_the_block(head_copy, slip):
    """A hand bump (step 3) that lost the comma after the version, or the version's closing quote, leaves a
    manifest that does not load; Codex reproduced the old comparison calling the lost comma the same. A version
    that is a number is valid JSON but no version. Each STOPs, so step 5 cannot pass it.
    MUTANTS CU15 (no pipefail: all three print REPLACE and exit 0), CU19 (the old comparison: all three print
    `same`), CU22 (number: `same`)."""
    sha, tracked, copy = head_copy
    pj = _plugin_json(copy)
    text = pj.read_text(encoding="utf-8")
    line = '"version": "0.6.0",'
    assert text.count(line) == 1, "control: the fixture's bump is one line"
    text = text.replace(line, {"comma": '"version": "0.6.0"', "quote": '"version": "0.6.0,',
                               "number": '"version": 0.6,'}[slip])
    pj.write_text(text, encoding="utf-8")
    if slip == "number":
        assert json.loads(text)["version"] == 0.6, "control: valid JSON, a version that is no string"
    else:
        with pytest.raises(json.JSONDecodeError):
            json.loads(text)
    line = _stopped(_run(sha, copy))
    assert PLUGIN_JSON in line and "$MOD" in line, f"the STOP names the box's plugin.json: {line!r}"


def _own_path(tmp_path: Path, checksum: str | None) -> str:
    """A PATH holding the block's other tools and, as sha256sum, a script with body `checksum`, or none at all."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, at in (("git", GIT), ("tr", TR), ("cut", CUT), ("cat", CAT), ("find", FIND)):
        (bin_dir / name).symlink_to(at)
    if checksum is not None:
        fake = bin_dir / "sha256sum"
        fake.write_text("#!/bin/sh\n" + checksum + "\n", encoding="utf-8")
        fake.chmod(0o755)
    return str(bin_dir)


@runs_the_block_on_its_own_path
@pytest.mark.parametrize("checksum", [None, "cat >/dev/null; exit 127", "cat >/dev/null; exit 0"],
                         ids=["missing", "exits 127", "prints nothing"])
def test_a_missing_or_failing_checksum_stops_the_block(head_copy, tmp_path, checksum):
    """Codex's repro: with a sha256sum that fails (127, as for a command not found), both digests were empty and
    every file printed `same`, exit 0, so step 5 passed a mod that was never replaced. Now a missing one is named
    before any file, and a digest that failed or came out empty stops the block. The copy differs from HEAD in
    hooks/meshy.js, so a `same` for every file is a wrong answer. Runs without a working sha256sum: the PATH is
    the test's own. MUTANTS CU14 (exits 127), CU16 (prints nothing), CU17 (missing: the STOP does not name it)."""
    sha, tracked, copy = head_copy
    with (copy / "hooks" / "meshy.js").open("a", encoding="utf-8") as f:
        f.write("\n// a box's copy from another commit\n")
    line = _stopped(_run(sha, copy, path=_own_path(tmp_path, checksum)))
    if checksum is None:
        assert "no sha256sum" in line, f"the STOP names the missing tool: {line!r}"


@runs_the_block
def test_a_py_that_runs_no_python_stops_the_block(head_copy, tmp_path):
    """MUTANT CU18: PY is not tried before the files, so the STOP names a file and not PY."""
    sha, tracked, copy = head_copy
    line = _stopped(_run(sha, copy, py=str(tmp_path / "no-python-here")))
    assert "$PY" in line, f"the STOP names PY: {line!r}"


@runs_the_block
def test_a_sha_without_the_mod_stops_the_block(head_copy):
    """A sha from before the mod lists no file, and a block that names no tracked file has printed no REPLACE:
    read at step 5 that would pass. MUTANT CU23: the empty listing is not stopped (exit 0, every file EXTRA)."""
    sha, tracked, copy = head_copy
    first = _git("rev-list", "--max-parents=0", "HEAD").decode().split()[0]
    assert _git("ls-tree", "-r", "--name-only", first, MOD_DIR) == b"", "control: the first commit has no mod"
    assert "no file" in _stopped(_run(first, copy))


def test_the_install_takes_the_repositorys_version():
    """memory-bxxxxx's ruling (10 Oct): a behaviour change bumps the version in the repository. A box that then
    picks its own differs from main and from the other boxes, and the version comparison fails (Codex's review
    of 76c1241). The step before the install reads <sha>'s version and keeps it as it is; only a <sha> still on
    0.1.0, from before the ruling, gets a version of the box's own. Step 5 checks the installed version.
    MUTANT CU24: the step as it was (a version above the installed one, picked by the box, for every <sha>)."""
    steps = _ordered_steps(_section(_page(), "The mod"), "Then, in this order:")
    install = [i for i, s in enumerate(steps) if s.startswith("**Install it**")]
    assert len(install) == 1 and install[0] > 0, "control: one install step, and a step before it"
    step = steps[install[0] - 1]
    assert f"`git show <sha>:{MOD_DIR}{PLUGIN_JSON}`" in step, "the step reads the version at <sha>"
    assert "as it is" in step and "choose no version of your own" in step, "the repository's version is kept"
    gate, local = step.find("still says `0.1.0`"), step.find("take one above the `version`")
    assert 0 <= gate < local, "the box picks a version only for a <sha> still on 0.1.0"
    compare = [s for s in steps if s.startswith("**Compare again**")]
    assert len(compare) == 1 and "records the `version` of `<sha>`" in compare[0], "step 5 checks the version"


def _git_cut_short(tmp_path: Path) -> str:
    """A PATH whose `git` is the real one, except that `git ls-tree` prints its first line and exits 128, as when
    a subtree object is missing after some paths were listed (Codex's review of 76c1241)."""
    bin_dir = tmp_path / "git-cut-short"
    bin_dir.mkdir()
    fake = bin_dir / "git"
    fake.write_text("#!/bin/sh\n"
                    'if [ "$1" = ls-tree ]; then "REAL" "$@" | { IFS= read -r one; echo "$one"; }; exit 128; fi\n'
                    'exec "REAL" "$@"\n'.replace("REAL", GIT), encoding="utf-8")
    fake.chmod(0o755)
    return str(bin_dir) + os.pathsep + os.environ.get("PATH", "")


@runs_the_block
@pytest.mark.parametrize("how", ["cut short", "unknown sha"])
def test_a_listing_git_could_not_finish_stops_the_block(head_copy, tmp_path, how):
    """`git ls-tree` inside `$(...)` lost its exit status: a listing cut short after one path compared that one
    file and exited 0. A sha this checkout lacks fails the listing too. Each STOPs before any file.
    MUTANT CU25: the listing's exit status is not checked (cut short: `same` and EXTRA lines, exit 0)."""
    sha, tracked, copy = head_copy
    path = None
    if how == "cut short":
        path = _git_cut_short(tmp_path)
        fake = os.path.join(path.split(os.pathsep)[0], "git")
        r = subprocess.run([fake, "ls-tree", "-r", "--name-only", sha, MOD_DIR], cwd=ROOT,
                           capture_output=True, text=True)
        assert r.returncode == 128 and len(r.stdout.splitlines()) == 1 < len(tracked), \
            f"control: the fake git lists one path of {len(tracked)} and fails: {r.returncode} {r.stdout!r}"
    else:
        sha = "0" * 40
        assert subprocess.run([GIT, "-C", str(ROOT), "cat-file", "-e", f"{sha}^{{commit}}"],
                              capture_output=True).returncode != 0, "control: the checkout lacks it"
    assert "git could not list" in _stopped(_run(sha, copy, path=path))


@runs_the_block
def test_a_file_the_sha_does_not_track_is_extra(head_copy):
    """A file a later commit deleted stays in a box's copy, and Claude Code still loads it: a deleted
    hooks/hooks.json keeps its hook (Codex's review of 76c1241). The block names it EXTRA, and leaves out what
    Claude Code writes on load (the mod's .gitignore). MUTANTS CU26 (no EXTRA loop: the stale files are not
    named), CU27 (Claude Code's files are not left out: tsconfig.json reads EXTRA)."""
    sha, tracked, copy = head_copy
    ignore = _git("show", f"{sha}:{MOD_DIR}.gitignore").decode()
    assert "tsconfig.json" in ignore and ".claude-plugin/types/" in ignore, "control: the .gitignore names them"
    stale = ["hooks/hooks.old.json", "legacy.js"]
    for rel in stale + ["tsconfig.json", ".claude-plugin/types/plugin.d.ts"]:
        assert rel not in tracked, f"control: {rel} is not tracked"
        (copy / rel).parent.mkdir(parents=True, exist_ok=True)
        (copy / rel).write_text("{}\n", encoding="utf-8")
    got = _compare(sha, copy)
    assert {rel: got.pop(rel, None) for rel in stale} == dict.fromkeys(stale, "EXTRA"), got
    assert set(got) == set(tracked) and set(got.values()) == {"same"}, got


@runs_the_block
def test_a_mod_folder_that_cannot_be_listed_stops_the_block(head_copy, tmp_path):
    """Without a listing of MOD no EXTRA file can show, so a MOD the block cannot list stops it. Before, a MOD
    that was no folder printed COPY for every file and exited 0. MUTANT CU28: the listing of MOD is not checked."""
    sha, tracked, copy = head_copy
    line = _stopped(_run(sha, tmp_path / "no-such-folder"))
    assert "$MOD" in line, f"the STOP names MOD: {line!r}"
