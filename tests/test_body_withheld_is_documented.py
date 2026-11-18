"""`GET /v1/mesh/context`'s `anchor.body_withheld` is documented where the context response's
fields are listed, value for value, and the documentation cannot outlive the field.

WHY. Divergence 027 added `body_withheld` to the anchor: null when the body is shown, else the
first of memory.read's gates the caller would fail. The README that lists the context response
(apps/meshsem/README.md, "What it serves") named the field in one sentence and none of its
values, so a reader who got `not_authorized` or `no_proof_of_possession` had nowhere to learn
what it meant or what to do. Now it has a table: one row per value.

BOTH DIRECTIONS, read from the source (no database, no server):

  * the code still emits the field (`context_for` builds the anchor with a `body_withheld`
    key) exactly when the README documents it;
  * the README's rows are exactly the values the code can return: search.py's WITHHELD_*
    constants plus `apps/calls/pipeline.refusal_without_envelope`'s REFUSED_*, and every
    `return` in the pipeline function and in each of 027's gate functions (GATE: since
    divergence 040's slice 2 review the gate is a reader half and a key half, composed by
    `_anchor_body_withheld`, and asked about the namespace by `namespace_body_withheld`) is
    one of them, None, the pipeline's answer, or another gate function's (itself proven
    here). A new reason added to any of them without a row fails here, and so does a row
    for a reason the code no longer has.

Run:  pytest tests/test_body_withheld_is_documented.py -q     (no database)
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "apps" / "meshsem" / "README.md"
SEARCH = ROOT / "apps" / "meshsem" / "search.py"
PIPELINE = ROOT / "apps" / "calls" / "pipeline.py"
#: The heading the table sits under, inside "What it serves".
HEADING = "### `anchor.body_withheld`"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _constants(tree: ast.Module, prefix: str) -> dict[str, str]:
    """Module-level `NAME = "value"` assignments whose name starts with `prefix`."""
    out = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id.startswith(prefix)
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            out[node.targets[0].id] = node.value.value
    return out


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    found = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
    assert len(found) == 1, f"control: {name} is where it was"
    return found[0]


def _returns(fn: ast.FunctionDef) -> list[ast.expr | None]:
    return [n.value for n in ast.walk(fn) if isinstance(n, ast.Return)]


#: 027's gate: every function in search.py whose answer is a withheld reason.
GATE = ("_reader_withheld", "_key_withheld", "_anchor_body_withheld",
        "namespace_body_withheld")
#: ... and those that answer for many keys at once, a dict of such reasons by key.
GATE_BY_KEY = ("body_withheld_many",)


def _code_reasons() -> set[str]:
    """Every value 027's gate can return, proven from the returns of each of its functions."""
    search, pipeline = _tree(SEARCH), _tree(PIPELINE)
    withheld, refused = _constants(search, "WITHHELD_"), _constants(pipeline, "REFUSED_")
    assert withheld and refused, "control: the constants are where they were"

    for value in _returns(_function(pipeline, "refusal_without_envelope")):
        ok = (value is None or (isinstance(value, ast.Constant) and value.value is None)
              or (isinstance(value, ast.Name) and value.id in refused))
        assert ok, f"refusal_without_envelope returns {ast.unparse(value)}, not a REFUSED_*"

    answers = {"pipeline.refusal_without_envelope", *GATE}
    def reason(value, passed_on) -> bool:
        return (value is None or (isinstance(value, ast.Constant) and value.value is None)
                or (isinstance(value, ast.Name) and (value.id in withheld
                                                     or value.id in passed_on))
                or (isinstance(value, ast.Call) and ast.unparse(value.func) in GATE))

    for name in (*GATE, *GATE_BY_KEY):
        gate = _function(search, name)
        passed_on = {t.id for n in ast.walk(gate) if isinstance(n, ast.Assign)
                     and isinstance(n.value, ast.Call)
                     and ast.unparse(n.value.func) in answers
                     for t in n.targets if isinstance(t, ast.Name)}
        if name in GATE:
            for value in _returns(gate):
                assert reason(value, passed_on), \
                    f"{name} returns {ast.unparse(value)}, not a documented reason"
            continue
        # A gate by key returns ONE dict, and every value it stores in it is a reason.
        (answers_name,) = {ast.unparse(v) for v in _returns(gate)}
        stored = [n.value for n in ast.walk(gate) if isinstance(n, ast.Assign)
                  for t in n.targets if isinstance(t, ast.Subscript)
                  and ast.unparse(t.value) == answers_name]
        assert stored, f"control: {name} stores its answers in {answers_name}"
        for value in stored:
            assert reason(value, passed_on), \
                f"{name} stores {ast.unparse(value)}, not a documented reason"
    return set(withheld.values()) | set(refused.values())


def _code_emits_the_field() -> bool:
    """`context_for` builds a dict with a literal "body_withheld" key: the anchor."""
    fn = _function(_tree(SEARCH), "context_for")
    return any(isinstance(n, ast.Dict) and any(isinstance(k, ast.Constant)
                                               and k.value == "body_withheld" for k in n.keys)
               for n in ast.walk(fn))


def _documented_rows() -> dict[str, str] | None:
    """The table under HEADING: first cell (a backticked value) -> the rest of the row.
    None when the README has no such section."""
    text = README.read_text(encoding="utf-8")
    assert "## What it serves" in text, "control: the README's response section is there"
    if HEADING not in text:
        return None
    section = text.split(HEADING, 1)[1]
    section = re.split(r"\n#{2,3} ", section, maxsplit=1)[0]
    rows = {}
    for line in section.splitlines():
        m = re.match(r"^\|\s*`([^`]+)`\s*\|(.*)\|\s*$", line)
        if m:
            rows[m.group(1)] = m.group(2)
    return rows


def test_the_field_is_documented_exactly_when_the_code_emits_it():
    emits, rows = _code_emits_the_field(), _documented_rows()
    assert emits, "control: context_for still builds the anchor with body_withheld"
    assert rows is not None, (f"{README.relative_to(ROOT)} has no {HEADING!r} section, and "
                              "the code emits anchor.body_withheld")
    assert "null" in rows, "the table says what null (the body is shown) means"


def test_every_value_the_code_can_return_has_a_row_and_no_row_is_stale():
    rows = _documented_rows() or {}
    documented = set(rows) - {"null"}
    code = _code_reasons()
    assert code - documented == set(), f"returned by the code, not documented: {code - documented}"
    assert documented - code == set(), f"documented, not returned by the code: {documented - code}"


def test_each_row_says_what_it_means_and_what_to_do():
    """Two non-empty cells after the value: what it means, what the reader does."""
    rows = _documented_rows()
    assert rows, "no rows to check"
    for value, rest in rows.items():
        cells = [c.strip() for c in rest.split("|")]
        assert len(cells) == 2 and all(cells), f"row {value!r}: {cells}"


def test_the_section_sits_in_what_it_serves():
    """Under the section that lists the context response, not appended elsewhere."""
    text = README.read_text(encoding="utf-8")
    serves = text.split("## What it serves", 1)[1].split("\n## ", 1)[0]
    assert HEADING in serves, f"{HEADING!r} is not inside 'What it serves'"
