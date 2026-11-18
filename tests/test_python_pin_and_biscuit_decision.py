"""Slice 0 of B07 (docs/design/biscuit-capabilities.md, section 6.2): the runtime and the record.

Divergence 040. Two decisions the operator approved on 2026-09-24, and this file is what makes
each one a checked statement rather than a remembered one:

  * Q1, the pin. biscuit-python 0.4.0 publishes no cp314 wheel and does not build from source
    on 3.14 (design 1.1), so the server runs on CPython 3.13 and `pyproject.toml` says so.
    Every venv instruction the repository gives must name 3.13, or a fresh clone builds a
    3.14 venv that slice 4's `biscuit_auth` import cannot install into.
  * Q9, the reversal. README.md listed biscuit capability tokens under "Dropped by decision"
    and docs/ISSUE-RESOLUTION-MAP.md said the drop "must be revisited" if tokens were ever
    switched on. The operator chose Biscuit; README, the MAP and docs/STATUS.md must stop
    contradicting the design.

House rules (docs/V1-DESIGN-RULES.md): every negative carries a positive control in the SAME
test (C1), so each parser that proves biscuit ABSENT from a list must, in the same run, find
the entries that are still there (billing, WebAuthn, TOTP, pgvector). A parser that finds
nothing would otherwise pass every absence check.

Run:  pytest tests/test_python_pin_and_biscuit_decision.py -v
Needs no database.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

from packaging.specifiers import SpecifierSet

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
README = ROOT / "README.md"
REQUIREMENTS_DEV = ROOT / "requirements-dev.txt"
MAP = ROOT / "docs" / "ISSUE-RESOLUTION-MAP.md"
DIVERGENCES = ROOT / "docs" / "DIVERGENCES.md"
STATUS = ROOT / "docs" / "STATUS.md"

#: Versions the pin must admit and refuse. 3.13.12 is the interpreter the slice-0 suite ran
#: on; 3.14.3 is the one HQD's shared `.venv` runs, which cannot install biscuit-python.
ADMIT = ("3.13.0", "3.13.12", "3.13.99")
REFUSE = ("3.12.10", "3.14.0", "3.14.3", "3.15.0")

#: What README and the MAP still list as dropped. The positive control for every absence check.
STILL_DROPPED_README = ("billing/tier/Stripe", "WebAuthn", "TOTP", "pgvector")
STILL_DROPPED_MAP = ("Billing", "WebAuthn", "TOTP", "pgvector")

#: The COMMAND of a venv step, matched at its start: bash `python3.13`, or the Windows
#: launcher. A 3.13 that appears only in a trailing comment does not count.
VENV_313 = re.compile(r"(?:python3\.13|py -3\.13|py -V:\S*3\.13\S*)\s+-m venv\b")

#: Words that say the biscuit drop still stands. A cell or line that names the tokens must
#: carry none of them.
DROP_STANDS = ("still not implemented", "must be revisited", "mechanism decision")
NAMES_THE_TOKENS = ("biscuit", "capability token", "meshy_biscuit_root_sk")

#: What each approved answer must say, in its own row of 040's table (design section 7, as
#: approved 2026-09-24). A question number with the wrong answer beside it is not a record.
APPROVED = {
    "Q1": ("CPython 3.13", "requires-python"),
    "Q2": ("only namespace admins mint in v1",),
    "Q3": ("24 hours at most",),
    "Q4": ("opt-in", "`object_ownership`", "OFF for `shared/wiki`"),
    "Q5": ("keeps raising", "OWN key"),
    "Q6": ("ONLY when a token was used",),
    "Q7": ("verify_strict",),
    "Q8": ("never pruned",),
    "Q9": ("reversed",),
    "Q10": ("in a file named by `MESHY_CAPABILITY_ROOT_KEY_PATH`",),
    "Q11": ('"object-capability-v1"', "`tools/meshy_node`"),
    "Q12": ("OUT of v1",),
}


def _read(path: Path) -> str:
    # Text mode: CRLF (README and docs/*.md are CRLF in the working tree) reads as "\n".
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------------- parsers

def pin_faults(spec: SpecifierSet) -> list[str]:
    """What is wrong with a requires-python specifier, as readable faults."""
    faults = [f"refuses {v}" for v in ADMIT if not spec.contains(v)]
    faults += [f"admits {v}" for v in REFUSE if spec.contains(v)]
    return faults


def section(text: str, heading: str) -> str:
    """From `heading` (a `## ` line) to the next `## ` heading or `---` rule."""
    assert text.count(f"\n{heading}") == 1, f"expected exactly one {heading!r}"
    body = text.split(f"\n{heading}", 1)[1]
    return re.split(r"\n## |\n---\n", body, maxsplit=1)[0]


def subsection(text: str, heading: str) -> str:
    """From `heading` (a `### ` line) to the next `## ` or `### ` heading."""
    assert text.count(f"\n{heading}") == 1, f"expected exactly one {heading!r}"
    body = text.split(f"\n{heading}", 1)[1]
    return re.split(r"\n#{2,3} ", body, maxsplit=1)[0]


def readme_dropped_paragraph(text: str) -> str:
    """README's single `**Dropped by decision:**` paragraph, whitespace collapsed."""
    found = re.findall(r"^\*\*Dropped by decision:\*\*(.*?)(?=\n\n|\n\*\*|\Z)", text, re.M | re.S)
    assert len(found) == 1, f"expected ONE 'Dropped by decision' paragraph, found {len(found)}"
    return " ".join(found[0].split())


def readme_dropped(text: str) -> list[str]:
    """The items of README's `**Dropped by decision:**` list, in order."""
    listed = readme_dropped_paragraph(text).split(" See ", 1)[0]
    return [item.strip(" .") for item in listed.split(",") if item.strip(" .")]


def readme_reversed(text: str) -> str:
    found = re.findall(r"^\*\*Reversed by decision:\*\*(.*?)(?=\n\n|\n\*\*|\Z)", text, re.M | re.S)
    assert len(found) == 1, f"expected ONE 'Reversed by decision' paragraph, found {len(found)}"
    return " ".join(found[0].split())


def table_rows(md: str) -> list[list[str]]:
    """Body rows of every markdown table in `md`, header and rule rows dropped."""
    rows = []
    for line in md.splitlines():
        if not line.startswith("|") or re.match(r"^\|[-:| ]+\|$", line.strip()):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        rows.append(cells)
    return [r for r in rows if r[0] not in {"area", "upstream", "question"}]


def fenced_lines(md: str) -> list[str]:
    """The lines inside ``` fences: the commands a reader copies, not the prose around them."""
    out, inside = [], False
    for line in md.splitlines():
        if line.lstrip().startswith("```"):
            inside = not inside
            continue
        if inside:
            out.append(line)
    return out


def venv_commands(lines: list[str]) -> list[str]:
    """For each line that mentions `-m venv`, its command part: a leading `#` (a comment header
    such as requirements-dev.txt's) is dropped, then everything from the first ` #` (a trailing
    comment) on. A 3.13 named only in the trailing comment is therefore not seen."""
    out = []
    for line in lines:
        if not re.search(r"-m venv\b", line):
            continue
        body = re.sub(r"^\s*#\s*", "", line)
        out.append(re.split(r"\s#", body, maxsplit=1)[0].strip())
    return out


def says_the_drop_stands(unit: str) -> bool:
    """True when `unit` (a cell, a line) names the tokens AND says their drop still stands."""
    low = unit.lower()
    return any(n in low for n in NAMES_THE_TOKENS) and any(p in low for p in DROP_STANDS)


# -------------------------------------------------------------------------------- Q1: the pin

def test_pyproject_pins_cpython_313_and_refuses_314():
    doc = tomllib.loads(_read(PYPROJECT))
    # A [project] table and one uv setting, nothing else: no [build-system], so `pip install -r`
    # is unchanged; no [tool.pytest.ini_options], so tests/conftest.py stays the only pytest
    # config; and `[tool.uv] managed = false`, the operator's decision of 2026-09-24, so
    # `uv run` and `uv sync` leave a checkout's venv alone instead of deleting or emptying the
    # shared `.venv` on HQD (divergence 040, section 2).
    assert set(doc) == {"project", "tool"}, sorted(doc)
    assert doc["tool"] == {"uv": {"managed": False}}, doc["tool"]
    # The boolean itself: TOML's `managed = 0` compares equal to False in Python, and uv
    # 0.10.9 refuses it ("invalid type: integer `0`, expected a boolean").
    assert doc["tool"]["uv"]["managed"] is False, doc["tool"]
    project = doc["project"]
    # And the table states the pin and nothing more. A `dependencies` list here would be a
    # second dependency list, which `pip install -r` ignores and uv installs.
    assert set(project) == {"name", "version", "requires-python"}, sorted(project)
    assert project["name"] and project["version"], project
    spec = SpecifierSet(project["requires-python"])
    assert pin_faults(spec) == [], (str(spec), pin_faults(spec))
    # C1: the checker refuses the one-sided pin that would still admit 3.14.
    assert "admits 3.14.3" in pin_faults(SpecifierSet(">=3.13"))
    assert "refuses 3.13.12" in pin_faults(SpecifierSet(">=3.12,<3.13"))


def test_every_venv_instruction_names_cpython_313():
    readme = _read(README)
    fresh = section(readme, "## Fresh install")
    # The fresh install's own venv step, not only the one under "Run the tests".
    fresh_venv = venv_commands(fenced_lines(fresh))
    assert fresh_venv, "README's fresh-install section has no venv step"
    assert all(VENV_313.match(cmd) for cmd in fresh_venv), fresh_venv
    assert "CPython 3.13" in fresh, "the fresh-install section must name the interpreter"

    header = [ln for ln in _read(REQUIREMENTS_DEV).splitlines() if ln.startswith("#")]
    everywhere = venv_commands(fenced_lines(readme)) + venv_commands(header)
    # Positive control: the fresh install, "Run the tests", and requirements-dev.txt's header.
    assert len(everywhere) >= 3, everywhere
    stale = [cmd for cmd in everywhere if not VENV_313.match(cmd)]
    assert not stale, f"venv instructions that do not name 3.13: {stale}"
    # C1 for the comment rule: a 3.13 that is only in a trailing comment is refused.
    assert venv_commands(["python -m venv .venv  # or python3.13 -m venv .venv"]) == [
        "python -m venv .venv"]
    assert not VENV_313.match("python -m venv .venv")


def test_install_hazards_on_a_shared_checkout_are_recorded():
    """uv reads `[project]`; pip does not. In a checkout whose `.venv` is on another Python,
    `uv run` or `uv sync` deleted it and made an empty 3.13 one, until `[tool.uv] managed =
    false` (the operator's decision, 2026-09-24) told uv to leave the project alone; and a
    3.13 venv made over it keeps the old version's compiled packages. README's Python step
    and 040's hand-off both say the guard is in place, what it costs (uv no longer checks the
    pin), and how a seat moves instead. Each positive probe is paired with the old sentence
    that must now be ABSENT: a record corrected in place, not appended beside."""
    step0 = " ".join(subsection(section(_read(README), "## Fresh install"), "### 0. Python").split())
    # Positive control: the parser reads the step it already had.
    assert "`requires-python`" in step0, step0
    for needle in ("`[tool.uv] managed = false`", "marked as unmanaged", "`uv run`",
                   "`uv sync`", "no dependencies", "new directory", "`--clear`",
                   "no longer checks the pin"):
        assert needle in step0, (needle, step0)
    for gone in ("as above, not uv", "they delete it"):
        assert gone not in step0, (gone, step0)

    text = _read(DIVERGENCES)
    entry = section(text, re.findall(r"^## 040\b.*$", text, re.M)[0])
    handoff = " ".join(
        subsection(entry, "### 2. What must change elsewhere before slice 4 reaches main").split())
    # Positive control: the parser reads the hand-off it already had.
    assert "BEFORE slice 4 lands" in handoff, handoff
    for needle in ("`uv sync`", "managed = false", "The guard is in place", "marked as unmanaged",
                   "`.venv313`", "new directory", "`--clear`", "cp314"):
        assert needle in handoff, (needle, handoff)
    for gone in ("is not taken here", "Until then, install with pip"):
        assert gone not in handoff, (gone, handoff)


# --------------------------------------------------------------------------- Q9: the reversal

def test_readme_no_longer_drops_biscuit_and_records_the_reversal():
    readme = _read(README)
    dropped = readme_dropped(readme)
    # Positive control, same parser, same run: the entries that ARE still dropped.
    missing = [d for d in STILL_DROPPED_README if d not in dropped]
    assert not missing, f"the parser lost {missing}; it read {dropped}"
    assert not [d for d in dropped if "biscuit" in d.lower()], dropped
    # The whole paragraph, not only the list before " See ": no sentence after the list may
    # keep the tokens dropped either.
    paragraph = readme_dropped_paragraph(readme)
    assert "pgvector" in paragraph, paragraph
    assert "biscuit" not in paragraph.lower(), paragraph

    reversal = readme_reversed(readme)
    for needle in ("biscuit capability tokens", "The operator chose Biscuit",
                   "object-level authorisation", "#913", "#523", "2026-09-24",
                   "`docs/design/biscuit-capabilities.md`", "divergence 040"):
        assert needle in reversal, (needle, reversal)


def test_map_no_longer_lists_biscuit_as_dropped_pending_revisit():
    text = _read(MAP)
    dropped = table_rows(section(text, "## Dropped from scope"))
    areas = [row[0] for row in dropped]
    missing = [d for d in STILL_DROPPED_MAP if not any(d in a for a in areas)]
    assert not missing, f"the parser lost {missing}; it read {areas}"
    # Every cell, not only the area: the old row under another label is still the old row.
    named = [r for r in dropped if any(n in " ".join(r).lower() for n in NAMES_THE_TOKENS)]
    assert not named, named

    # The row still exists, rewritten: its own trigger is quoted and said to be met.
    biscuit = [r for r in table_rows(text) if "biscuit" in r[0].lower()]
    assert len(biscuit) == 1, biscuit
    why = biscuit[0][-1]
    for needle in ("The operator chose Biscuit", "2026-09-24",
                   "divergence 040", "must be revisited", "is met"):
        assert needle in why, (needle, why)


def test_status_no_longer_lists_biscuit_as_deliberately_not_implemented():
    text = _read(STATUS)
    not_built = section(text, "## Deliberately NOT implemented")
    bullets = re.findall(r"^- \*\*(.+?)\*\*", not_built, re.M)
    # Positive control: the parser finds the entry that IS still there.
    assert any("pgvector" in b for b in bullets), bullets
    assert not [b for b in bullets if any(n in b.lower() for n in NAMES_THE_TOKENS)], bullets
    # Anywhere in the file, a table row included: fin/status 7e1e2ba adds a B07 row BLOCKED on
    # "a mechanism decision (biscuit-python 0.4.0 works ...)", which would contradict 040.
    stands = [ln for ln in text.splitlines() if says_the_drop_stands(ln)]
    assert not stands, stands
    # C1 for the line check, on that row's words (a fixture: this base has no B07 row).
    assert says_the_drop_stands(
        "| B07 | ... | BLOCKED | ... | a mechanism decision (biscuit-python 0.4.0 works, see "
        '"Deliberately NOT implemented", or key-scoped grants); L |')
    assert not says_the_drop_stands("- **pgvector.** In `jo-db` only")

    # The reversal is recorded where the entry was, in the design's words.
    reversal = " ".join(not_built.split())
    for needle in ("The operator chose Biscuit", "#913", "#523", "2026-09-24",
                   "`docs/design/biscuit-capabilities.md`", "divergence 040"):
        assert needle in reversal, (needle, reversal)


def test_divergence_040_records_the_pin_and_all_twelve_answers():
    text = _read(DIVERGENCES)
    headings = re.findall(r"^## (\d{3})\b", text, re.M)
    assert "034" in headings, "positive control: the parser must see divergence 034"
    heading = re.findall(r"^## 040\b.*$", text, re.M)
    assert len(heading) == 1, f"expected ONE divergence 040 heading, found {heading}"
    entry = section(text, heading[0])
    flat = " ".join(entry.split())
    for needle in ("requires-python", "cp314", "PYO3_USE_ABI3_FORWARD_COMPATIBILITY",
                   "slice 4", "MESHY_CAPABILITY_ROOT_KEY_PATH",
                   "Never the key itself in an environment variable"):
        assert needle in flat, needle
    rows = {r[0]: " | ".join(r[1:]) for r in table_rows(entry) if re.fullmatch(r"Q\d+", r[0])}
    assert set(rows) == {f"Q{n}" for n in range(1, 13)}, sorted(rows)
    # Each answer, in its own row: the number alone does not record the decision.
    wrong = {q: [n for n in needles if n not in rows[q]] for q, needles in APPROVED.items()}
    assert not any(wrong.values()), {q: w for q, w in wrong.items() if w}
    # Q10 names the key's PATH; the key itself is never in an environment variable.
    assert not re.search(r"MESHY_CAPABILITY_ROOT_KEY(?!_PATH)", rows["Q10"]), rows["Q10"]
