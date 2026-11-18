"""The graph line exists twice: the postbox writes it into a notice (tools/postbox/graphline.py) and
meshy-mod adds it inside a Claude seat when the notice lacks one (tools/postbox/mod/meshy-mod/hooks/meshy.js).
The mod knows a notice already carries a line only by finding graphline's PREFIX in it, so the two files
must spell it the same. Each side pins its own copy in its own test harness (pytest here, `claude plugin
test` for the mod); this is the one test that sees both.

Mutants (each applied and seen to fail here):
  P1 meshy.js GRAPH_PREFIX gains a hyphen ('routing-only')     test_the_mod_dedupes_on_graphlines_exact_prefix
  P2 meshy.js dedupes on a shorter literal, not GRAPH_PREFIX    test_the_mod_dedupes_on_graphlines_exact_prefix
  P3 meshy.js drops the start-of-notice half of the dedupe     test_the_mod_dedupes_on_graphlines_exact_prefix
  P4 meshy.js drops the start-of-line half of the dedupe       test_the_mod_dedupes_on_graphlines_exact_prefix
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PB = ROOT / "tools" / "postbox"
sys.path.insert(0, str(PB))
import graphline  # noqa: E402

MOD = PB / "mod" / "meshy-mod" / "hooks" / "meshy.js"


def test_the_mod_dedupes_on_graphlines_exact_prefix():
    src = MOD.read_text(encoding="utf-8")
    m = re.search(r"^const GRAPH_PREFIX = '([^']*)'$", src, re.M)
    assert m, "meshy.js no longer declares GRAPH_PREFIX as a plain string constant"
    assert m.group(1) == graphline.PREFIX
    # and the dedupe uses that constant, not a second literal that could drift on its own. It matches the
    # prefix only where graphline writes it, at the start of the notice or of one of its lines
    assert "text.startsWith(GRAPH_PREFIX)" in src
    assert r"text.includes('\n' + GRAPH_PREFIX)" in src
    assert "text.startsWith('graph (routing" not in src
    assert "text.includes('graph (routing" not in src
    assert r"text.includes('\ngraph (routing" not in src
