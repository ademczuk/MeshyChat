"""Divergences 111 and 112 name what #637 put on the wire, and cannot outlive it.

WHY. #637 (14 Nov 2026) added `mode=deterministic` to 037's two summary routes and `order=causal` /
`provenance=1` to the thread route, and merged with no divergence entry: a reader of DIVERGENCES.md learned
that summaries are served only by the model (037), which stopped being true. REFACTOR-QUEUE-2026-11 A7.

Read from the source with `ast`, so no Django settings and no database: the version string, the tier, the
sentence and excerpt limits and the high-signal types in `apps/meshsum/deterministic.py`, the tier and the mode
values the summary views accept, and the thread route's provenance cap in `apps/mesh/views.py`. Each must appear
in its entry, and each entry must exist exactly once. A changed value with an unchanged entry fails here.

Run:  pytest tests/test_deterministic_summary_is_documented.py -q     (no database)
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIVERGENCES = ROOT / "docs" / "DIVERGENCES.md"
DETERMINISTIC = ROOT / "apps" / "meshsum" / "deterministic.py"
SUMMARY_VIEWS = ROOT / "apps" / "meshsum" / "views.py"
MESH_VIEWS = ROOT / "apps" / "mesh" / "views.py"


def _module_constants(path: Path) -> dict[str, object]:
    out: dict[str, object] = {}
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                out[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                continue
    return out


def _frozenset_literal(path: Path, name: str) -> set[str]:
    """`NAME = frozenset({...})`: the literal set inside the call."""
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id == name
                and isinstance(node.value, ast.Call) and node.value.args):
            return set(ast.literal_eval(node.value.args[0]))
    raise AssertionError(f"{name} is not a frozenset literal in {path}")


def _entry(number: str) -> str:
    text = DIVERGENCES.read_text(encoding="utf-8")
    starts = [m.start() for m in re.finditer(rf"^## {number}\b", text, re.M)]
    assert len(starts) == 1, f"divergence {number} must have exactly one heading, found {len(starts)}"
    rest = text[starts[0]:]
    nxt = re.search(r"^## (P-)?\d{3}\b", rest[3:], re.M)
    return rest if nxt is None else rest[:nxt.start() + 3]


def test_divergence_111_names_the_deterministic_summary_as_built():
    consts = _module_constants(DETERMINISTIC)
    entry = _entry("111")
    assert f'`{consts["DETERMINISTIC_SUMMARY_VERSION"]}`' in entry or f'"{consts["DETERMINISTIC_SUMMARY_VERSION"]}"' in entry
    assert f'at most {consts["MAX_EXTRACTIVE_SENTENCES"]} of the covered messages' in entry
    assert f'cut to {consts["MAX_EXCERPT_CHARS"]} characters' in entry
    for msg_type in _frozenset_literal(DETERMINISTIC, "HIGH_SIGNAL_TYPES"):
        assert re.search(rf"\b{msg_type}\b", entry), f"type {msg_type} is picked first but 111 does not say so"
    views = SUMMARY_VIEWS.read_text(encoding="utf-8")
    tier = re.search(r'"tier": "(deterministic_[a-z_]+)"', views)
    assert tier, "the deterministic answer's tier is not where this test reads it"
    assert f"`{tier.group(1)}`" in entry or f'"{tier.group(1)}"' in entry
    accepted = re.search(r'if mode not in \("model", "deterministic"\)', views)
    assert accepted, "the summary views no longer accept exactly mode=model|deterministic: update 111"
    assert "`mode=model|deterministic`" in entry


def test_divergence_037_points_at_111():
    assert "**Amended by 111.**" in _entry("037")


def test_divergence_112_names_the_thread_route_options_as_built():
    consts = _module_constants(MESH_VIEWS)
    entry = _entry("112")
    assert f'capped at {consts["PROVENANCE_LIMIT_MAX"]} (`PROVENANCE_LIMIT_MAX`)' in entry
    views = MESH_VIEWS.read_text(encoding="utf-8")
    assert 'if order not in ("stream", "causal")' in views, "the thread route's order values changed: update 112"
    assert "`order=stream|causal`" in entry
    fields = consts["ENVELOPE_FIELDS"]
    for added in ("closes", "declines", "refines"):
        assert added in fields and f"`{added}`" in entry


def test_the_check_fails_on_a_wrong_number():
    """Positive control: the same assertion over a version the code does not have is false."""
    assert "`deterministic-v0`" not in _entry("111")
    assert "capped at 99 (`PROVENANCE_LIMIT_MAX`)" not in _entry("112")
