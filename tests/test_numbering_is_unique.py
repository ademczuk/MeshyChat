"""Two branches each took "the next number", and both were right until they merged.

THE REAL FAILURE THIS FILE IS PROVED AGAINST. On 2026-09-21 four divergence-number collisions
landed in one day between two seats working the same repository from different machines. The
fourth: one branch documented its work as divergence 012 while origin/main had already pushed
012 (the meshy-memory route) and 013 (the wire rename). The textual merge conflict in
docs/DIVERGENCES.md is the only thing that surfaced it, and a conflict resolved by keeping
both sides produces a file with two `## 012` headings and nothing to say so.

The migration numbers under sql_meshychat/ are one fetch away from the same fate, with a worse
failure shape: `sqlmigrate.discover()` would sort the pair, the second file's SQL would run,
and the ledger's PRIMARY KEY would refuse its row afterwards. That check now lives in
`discover()` itself, the seam every apply path crosses, and the tests here prove it refuses.

House rules (docs/V1-DESIGN-RULES.md): every negative has a positive control in the SAME
test (C1), and a gate that has never refused is unarmed (C2), so each checker below is run
once against the real artifact mutated into the failure and once against the real artifact
as it stands.

Run:  pytest tests/test_numbering_is_unique.py -v
Needs no database.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from apps.core import schema_sets, sqlmigrate  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DIVERGENCES = ROOT / "docs" / "DIVERGENCES.md"

#: A divergence heading: `## 014, ...`, `## 004 — ...`, or the proposal series `## P-002, ...`.
HEADING = re.compile(r"^## (?P<series>P-)?(?P<number>\d{3})\b", re.M)

#: The forms code uses to cite an entry (the project's design log (not part of this release) 014`, `divergence 011`,
#: `MeshyChat divergence 001`). Measured when first written: 18 citations across apps/,
#: meshychat/, sql_meshychat/ and tests/, every one resolving to a heading. Scanned so a
#: renumbered entry cannot leave a stale citation behind: the fourth collision was renumbered
#: in the document and cited by its old number in four source files until someone grepped.
CITATION = re.compile(r"(?:docs/DIVERGENCES\.md|[Dd]ivergence) (?P<number>\d{3})\b")

#: Where citations are read. Measured on main 890b61e (2026-10-10): 1,827 citations in tracked
#: files, of which the .py/.sql scan of apps/, meshychat/, sql_meshychat/ and tests/ read 1,396.
#: When 073 was renumbered 079 on merge (that entry's "Numbering:" line), its references in
#: tools/meshy_node, README.md and docs/document-passing.md had to be found and changed with
#: nothing to check them: that scan read none of the three. Widened to read 1,757: tools/ (the client, the postbox and the
#: meshy-mod plugin under tools/postbox/mod/meshy-mod; 121 in .py, 5 in .md, 3 in .js, 2 in
#: .ts), docs/ (169 in .md besides the definitions file), apps/ Markdown (13) and the
#: top-level README.md (48). Every one resolved to a heading when measured, so nothing is
#: allowlisted. Still unread: 12 in deploy/ (.ps1, .yml), pyproject.toml, requirements*.txt,
#: .gitignore, .gitattributes and one calibration .json.
CITED_DIRS = ("apps", "docs", "meshychat", "sql_meshychat", "tests", "tools")
#: Read as files: the top level itself is not walked (it holds .venv and other local state).
CITED_FILES = ("README.md",)
CITED_SUFFIXES = frozenset({".py", ".sql", ".md", ".js", ".ts"})
#: Never walked: vendored or generated trees (node_modules appears when a seat runs npm in the
#: plugin folder, dist when it builds, and meshy-mod/.claude-plugin/types/ is written by Claude
#: Code on each load, per that folder's .gitignore; .claude-plugin/ holds nothing else but
#: plugin.json), and Python's own (.venv, __pycache__). Pruned before descent, so an installed
#: node_modules costs the suite nothing.
SKIPPED_DIRS = frozenset({"node_modules", "dist", ".claude-plugin", ".venv", "__pycache__"})
#: The file that defines the numbers is not read as a citer. HEADING parses it, and its own
#: cross-references (58 when measured, every one resolving) are checked by
#: test_divergences_own_cross_references_name_existing_entries, so a stale number in it is
#: reported once, by the test about that file.
DEFINITIONS = Path("docs", "DIVERGENCES.md")


def divergence_numbers(text: str) -> list[tuple[str, int, int]]:
    """[(series, number, line)] in document order. Series is '' or 'P-'."""
    out = []
    for m in HEADING.finditer(text):
        line = text.count("\n", 0, m.start()) + 1
        out.append((m.group("series") or "", int(m.group("number")), line))
    return out


def check_unique_and_increasing(text: str) -> dict[str, int]:
    """Raise on a duplicate or out-of-order number; return the next free number per series."""
    by_series: dict[str, list[tuple[int, int]]] = {}
    for series, number, line in divergence_numbers(text):
        by_series.setdefault(series, []).append((number, line))
    if not by_series:
        raise AssertionError("no divergence headings found: the parser cannot see the file")
    next_free = {}
    for series, entries in by_series.items():
        seen: dict[int, int] = {}
        last = 0
        for number, line in entries:
            label = f"{series}{number:03d}"
            if number in seen:
                raise AssertionError(
                    f"divergence {label} is claimed twice, lines {seen[number]} and {line}. "
                    f"Two branches took the same next number. Renumber the one that landed "
                    f"second to {series}{max(n for n, _ in entries) + 1:03d}."
                )
            if number < last:
                raise AssertionError(
                    f"divergence {label} at line {line} is out of order after "
                    f"{series}{last:03d}; entries are appended, never inserted."
                )
            seen[number] = line
            last = number
        next_free[series] = last + 1
    return next_free


def _citing_files(root: Path) -> list[Path]:
    out = [root / name for name in CITED_FILES if (root / name).is_file()]
    definitions = root / DEFINITIONS
    for d in CITED_DIRS:
        for dirpath, dirnames, filenames in os.walk(root / d):
            dirnames[:] = sorted(n for n in dirnames if n not in SKIPPED_DIRS)
            for name in sorted(filenames):
                path = Path(dirpath, name)
                if path.suffix in CITED_SUFFIXES and path != definitions:
                    out.append(path)
    return out


def _citations(text: str) -> list[tuple[int, int]]:
    """[(number, line)] for every citation in `text`."""
    return [(int(m.group("number")), text.count("\n", 0, m.start()) + 1)
            for m in CITATION.finditer(text)]


def _cited_numbers(root: Path = ROOT) -> list[tuple[Path, int, int]]:
    """The scan: [(path relative to root, number, line)] for every citation in the files it
    reads under `root`. Takes the root so a test can run it on a tree it built."""
    out = []
    for path in _citing_files(root):
        text = path.read_text(encoding="utf-8", errors="replace")
        out.extend((path.relative_to(root), n, line) for n, line in _citations(text))
    return out


def entry_numbers(definitions: str) -> set[int]:
    """The numbered (not P-) entries a definitions text has headings for."""
    return {n for s, n, _ in divergence_numbers(definitions) if s == ""}


def dangling_citations(cited: list[tuple[Path, int, int]],
                       numbers: set[int]) -> list[tuple[str, int, int]]:
    """[(posix path, number, line)] for each scanned citation naming none of `numbers`."""
    return [(p.as_posix(), n, line) for p, n, line in cited if n not in numbers]


def dangling_cross_references(definitions: str, numbers: set[int]) -> list[tuple[int, int]]:
    """[(number, line)] for each citation inside the definitions text naming none of `numbers`."""
    return [(n, line) for n, line in _citations(definitions) if n not in numbers]


# ----------------------------------------------------------------- divergence headings

def test_divergence_numbers_are_unique_and_increasing():
    """The real document as it stands, plus the real document mutated into the failure."""
    text = DIVERGENCES.read_text(encoding="utf-8")
    found = divergence_numbers(text)
    assert len(found) >= 14, f"positive control: expected at least 14 headings, parsed {len(found)}"
    assert ("", 14, 818) in found or any(n == 14 for _, n, _ in found), (
        "positive control: the parser must see divergence 014, the entry the fourth "
        "collision was renumbered to"
    )
    next_free = check_unique_and_increasing(text)
    assert next_free[""] == max(n for s, n, _ in found if s == "") + 1

    # C1/C2: the same checker must REFUSE the real file with the last heading's number
    # duplicated onto its predecessor. This is the shape the 2026-09-21 conflict would
    # have produced had both sides been kept: two headings, one number.
    numbers = [n for s, n, _ in found if s == ""]
    top, prev = numbers[-1], numbers[-2]
    mutated = text.replace(f"## {prev:03d},", f"## {top:03d},", 1)
    assert mutated != text, "mutation did not apply: the predecessor heading is not comma-form"
    with pytest.raises(AssertionError, match=f"divergence {top:03d} is claimed twice"):
        check_unique_and_increasing(mutated)


def test_divergence_numbers_are_appended_not_inserted():
    text = DIVERGENCES.read_text(encoding="utf-8")
    numbers = [n for s, n, _ in divergence_numbers(text) if s == ""]
    # Real order passes; swap two adjacent headings and it must refuse.
    check_unique_and_increasing(text)
    a, b = numbers[-2], numbers[-1]
    swapped = (text.replace(f"## {a:03d},", "## TMP,", 1)
                   .replace(f"## {b:03d},", f"## {a:03d},", 1)
                   .replace("## TMP,", f"## {b:03d},", 1))
    assert swapped != text
    with pytest.raises(AssertionError, match="out of order"):
        check_unique_and_increasing(swapped)


def test_every_code_citation_names_an_existing_divergence():
    """A renumbered entry must not leave its old number in a comment, a README or the plugin."""
    cited = _cited_numbers()
    assert cited, "positive control: no code citations found, the scanner cannot see them"
    assert any(n == 14 for _, n, _ in cited), (
        "positive control: divergence 014 is cited from apps/jobs/registry.py, apps/mesh, "
        "meshychat/urls.py and sql_meshychat/004; the scanner must find at least one"
    )
    # Positive controls for each area the scan reads, every one cited from on 890b61e: a
    # dropped directory, file type or file shows up here as an area with nothing found.
    areas = {(p.parts[0] if len(p.parts) > 1 else p.name, p.suffix) for p, _, _ in cited}
    for area in [("apps", ".py"), ("apps", ".md"), ("meshychat", ".py"),
                 ("sql_meshychat", ".sql"), ("tests", ".py"), ("docs", ".md"),
                 ("tools", ".py"), ("tools", ".md"), ("tools", ".js"), ("tools", ".ts"),
                 ("README.md", ".md")]:
        assert area in areas, f"positive control: no citation found in {area}, the scan cannot see it"
    assert DEFINITIONS not in {p for p, _, _ in cited}, (
        "docs/DIVERGENCES.md defines the numbers and is checked by its own test, not the scan"
    )
    dangling = dangling_citations(cited, entry_numbers(DIVERGENCES.read_text(encoding="utf-8")))
    assert not dangling, (
        f"code cites divergence numbers with no heading in docs/DIVERGENCES.md: {dangling}. "
        "Either the entry was renumbered and the citation was not, or the citation is wrong."
    )


def test_the_citation_scan_reads_tools_the_plugin_and_the_markdown(tmp_path):
    """The widened scan, proved on a tree built for it: a stale number in each place is found.

    One stale citation (a number above the only heading, 001) per directory and file type the
    scan reads, a resolving citation beside them as the control (C1), a stale one inside each
    folder it must never walk, and one in the definitions file, which is not a citer. The
    expected set is written out here rather than derived from CITED_DIRS or CITED_SUFFIXES, so
    shrinking either constant fails this test.
    """
    # Each body's {:03d} becomes that file's number when written: this file holds no citation
    # itself, or the real-tree scan, which reads tests/, would report the numbers below.
    files = {
        "docs/DIVERGENCES.md": ("## 001, the only entry\n\nRenumbered from divergence {:03d}.\n", 800),
        "apps/a.py": ("# divergence {:03d}\n", 901),
        "meshychat/m.py": ("# divergence {:03d}\n", 902),
        "sql_meshychat/001_s.sql": ("-- divergence {:03d}\n", 903),
        "tests/t.py": ("# divergence {:03d}\n", 904),
        "tools/meshy_node/x.py": ("x = 1\n# docs/DIVERGENCES.md {:03d}\n", 905),
        "tools/postbox/README.md": ("# Postbox\n\nSee Divergence {:03d}.\n", 906),
        "tools/postbox/mod/meshy-mod/hooks/h.js": ("// divergence {:03d}\n", 907),
        "tools/postbox/mod/meshy-mod/tests/h.test.ts": ("// divergence {:03d}\n", 908),
        "docs/design/d.md": ("Per divergence {:03d}.\n", 909),
        "README.md": ("# MeshyChat\n\nSee docs/DIVERGENCES.md {:03d}.\n", 910),
        "tools/ok.py": ("# divergence {:03d} resolves\n", 1),
        "tools/postbox/mod/meshy-mod/node_modules/dep/index.js": ("// divergence {:03d}\n", 950),
        "tools/postbox/mod/meshy-mod/dist/h.js": ("// divergence {:03d}\n", 951),
        "tools/postbox/mod/meshy-mod/.claude-plugin/types/mod.d.ts": ("// divergence {:03d}\n", 954),
        "tools/.venv/lib/site.py": ("# divergence {:03d}\n", 952),
        "tools/meshy_node/__pycache__/x.py": ("# divergence {:03d}\n", 953),
    }
    for rel, (body, number) in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body.format(number), encoding="utf-8")

    cited = _cited_numbers(tmp_path)
    definitions = (tmp_path / DEFINITIONS).read_text(encoding="utf-8")
    numbers = entry_numbers(definitions)
    assert numbers == {1}, "control: the built definitions file parses"
    found = sorted(dangling_citations(cited, numbers))
    assert found == [
        ("README.md", 910, 3),
        ("apps/a.py", 901, 1),
        ("docs/design/d.md", 909, 1),
        ("meshychat/m.py", 902, 1),
        ("sql_meshychat/001_s.sql", 903, 1),
        ("tests/t.py", 904, 1),
        ("tools/meshy_node/x.py", 905, 2),
        ("tools/postbox/README.md", 906, 3),
        ("tools/postbox/mod/meshy-mod/hooks/h.js", 907, 1),
        ("tools/postbox/mod/meshy-mod/tests/h.test.ts", 908, 1),
    ], found
    assert ("tools/ok.py", 1, 1) in [(p.as_posix(), n, line) for p, n, line in cited], (
        "control: the resolving citation is read, and not reported"
    )
    # The definitions file's stale number is the other test's to report, and it does.
    assert dangling_cross_references(definitions, numbers) == [(800, 3)]


def test_divergences_own_cross_references_name_existing_entries():
    """The definitions file's own `divergence NNN` references, apart from the scan (C1, C2)."""
    text = DIVERGENCES.read_text(encoding="utf-8")
    refs = list(CITATION.finditer(text))
    assert len(refs) >= 50, f"positive control: 58 cross-references when measured, parsed {len(refs)}"
    numbers = entry_numbers(text)
    dangling = dangling_cross_references(text, numbers)
    assert dangling == [], (
        f"docs/DIVERGENCES.md cross-references numbers it has no heading for: {dangling}"
    )

    # The real file with its last cross-reference pointed one past the last entry must refuse.
    past = max(numbers) + 1
    last = refs[-1]
    mutated = text[:last.start("number")] + f"{past:03d}" + text[last.end("number"):]
    line = text.count("\n", 0, last.start()) + 1
    assert dangling_cross_references(mutated, numbers) == [(past, line)]


# ------------------------------------------------------------------ migration versions

def _write(dir_: Path, name: str) -> Path:
    p = dir_ / name
    p.write_bytes(b"SELECT 1;\n")   # LF on purpose: discover() refuses CRLF first
    return p


def test_discover_refuses_two_files_claiming_one_version(tmp_path):
    """Positive control and refusal in one test, per C1."""
    ok = tmp_path / "ok"
    ok.mkdir()
    _write(ok, "004_first.sql")
    _write(ok, "005_second.sql")
    plan = sqlmigrate.discover(ok)
    assert [v for v, *_ in plan] == [4, 5], "positive control: two distinct versions discover"

    dup = tmp_path / "dup"
    dup.mkdir()
    _write(dup, "004_first.sql")
    _write(dup, "004_second.sql")
    _write(dup, "007_later.sql")
    with pytest.raises(ValueError) as e:
        sqlmigrate.discover(dup)
    msg = str(e.value)
    assert "two migrations claim version 4" in msg
    assert "004_first.sql" in msg and "004_second.sql" in msg, "both claimants must be named"
    assert "next free version in dup is 8" in msg, "the message must hand back the fix"


def test_discover_refuses_the_duplicate_before_reading_bytes(tmp_path):
    """The version check comes first, so a CRLF duplicate is reported as a duplicate.

    Order matters for the reader: the fix for a collision is a rename, the fix for CRLF is
    .gitattributes, and the first error printed is the one that gets acted on.
    """
    d = tmp_path / "d"
    d.mkdir()
    _write(d, "004_first.sql")
    (d / "004_second.sql").write_bytes(b"SELECT 1;\r\n")
    with pytest.raises(ValueError, match="two migrations claim version 4"):
        sqlmigrate.discover(d)


def test_discover_refuses_one_crlf_file_among_distinct_versions(tmp_path):
    """The CRLF refusal itself (status item F26), reached: distinct versions, one file CRLF.

    docs/ISSUE-RESOLUTION-MAP.md closes the 2026-06-25 checksum crash-loop "by construction"
    on this refusal, and the only CRLF fixture above is a DUPLICATE, which the version check
    refuses first, so no test reached it: `if b"\\r\\n" in raw:` -> `if False:` survived.
    Control first: the same directory with the file LF discovers both versions.
    """
    d = tmp_path / "d"
    d.mkdir()
    _write(d, "004_first.sql")
    second = _write(d, "005_second.sql")
    assert [v for v, *_ in sqlmigrate.discover(d)] == [4, 5], "control: LF discovers"

    second.write_bytes(b"SELECT 1;\r\nSELECT 2;\r\n")
    with pytest.raises(ValueError) as e:
        sqlmigrate.discover(d)
    msg = str(e.value)
    assert "005_second.sql contains CRLF" in msg, msg
    assert "004_first.sql" not in msg, "only the CRLF file is named"
    assert ".gitattributes" in msg, "the message must hand back the fix"


def test_every_declared_set_has_unique_versions_today():
    """The live seam: the real directories, through the real discover()."""
    for s in schema_sets.SETS:
        plan = sqlmigrate.discover(s.sql_dir)      # raises on a duplicate
        versions = [v for v, *_ in plan]
        assert len(versions) == len(set(versions)), s.name
        assert versions == sorted(versions), s.name
