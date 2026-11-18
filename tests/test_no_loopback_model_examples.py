"""No config example, README or help text suggests a 127.0.0.1 model provider.

WHY. By operator ruling (2026-09-23) HQD is dedicated to the mesh and runs NO model
servers; the providers are HQB's, on the LAN (192.168.50.10:7875 embed, :7876 rerank, :7872
chat). The repository still told an operator otherwise in three places: apps/meshsem's
README declared this box's own embedder (127.0.0.1:8103) and reranker (127.0.0.1:8100) as
fallbacks, apps/modelhost/registry.py's docstring example declared a 127.0.0.1:8100
reranker, and `python -m apps.board derive --help` offered http://127.0.0.1:7872. A copied
example is how configuration gets written, so an example is configuration.

WHAT IS SCANNED: the operator-facing text, i.e. every .md and .py under apps/, tools/,
meshychat/ and docs/, and the top-level README. tests/ is NOT scanned: the fakes there
really do listen on 127.0.0.1, which is the point of a fake. docs/DIVERGENCES.md is not
scanned either: it is a dated record, and "measured against HQD at 127.0.0.1:8103 on
2026-09-23" is history, not advice. Two shapes are refused: a provider entry whose
base_url is loopback, and a help or doc example ("e.g. http://127.0.0.1:<model port>").

Run:  pytest tests/test_no_loopback_model_examples.py -q      (no database needed)
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: A provider entry (JSON or a Python mapping) whose base_url is loopback.
LOOPBACK_BASE_URL = re.compile(
    r"""["']?base_url["']?\s*[:=]\s*["']https?://(?:127\.\d+\.\d+\.\d+|localhost|\[::1\])""")
#: An example in prose or help text pointing at a loopback MODEL port (7870-7899, 8090-8109).
LOOPBACK_EXAMPLE = re.compile(
    r"e\.g\.\s*`?https?://(?:127\.\d+\.\d+\.\d+|localhost):(?:78[7-9]\d|809\d|810\d)\b")

EXCLUDED = {"docs/DIVERGENCES.md"}


def _operator_text():
    files = [ROOT / "README.md"]
    for sub in ("apps", "tools", "meshychat", "docs"):
        files += [p for p in (ROOT / sub).rglob("*") if p.suffix in (".md", ".py")
                  and "__pycache__" not in p.parts]
    for p in sorted(set(files)):
        rel = str(p.relative_to(ROOT)).replace("\\", "/")
        if rel not in EXCLUDED:
            yield rel, p.read_text(encoding="utf-8")


def _hits(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines()
            if LOOPBACK_BASE_URL.search(ln) or LOOPBACK_EXAMPLE.search(ln)]


def test_the_scanner_catches_each_shape_it_refuses():
    """Positive controls: planted lines of both shapes are caught, a LAN entry is not."""
    assert _hits('  {"id": "embed-local", "kind": "embed", "base_url": "http://127.0.0.1:8103",')
    assert _hits("base_url='http://localhost:8100'")
    assert _hits('help="... (e.g. http://127.0.0.1:7872). Omitted: rules only"')
    assert not _hits('  {"id": "embed-hqb-5090", "kind": "embed", '
                     '"base_url": "http://192.168.50.10:7875",')
    assert not _hits('help="the server, e.g. http://127.0.0.1:8765"'), (
        "the meshy_node --base-url example names the MESH server, not a model")


def test_no_operator_facing_text_suggests_a_loopback_model_provider():
    scanned, found = 0, {}
    for rel, text in _operator_text():
        scanned += 1
        hits = _hits(text)
        if hits:
            found[rel] = hits
    assert scanned > 50, f"control: only {scanned} files scanned; the walk is broken"
    assert not found, (
        "HQD runs no model servers (operator ruling, 2026-09-23); the providers are HQB's "
        f"on 192.168.50.10. Loopback model examples remain: {found}")


def test_the_documented_providers_are_hqbs():
    """The example an operator copies names HQB, not a loopback model server."""
    readme = (ROOT / "apps" / "meshsem" / "README.md").read_text(encoding="utf-8")
    for port in ("7875", "7876", "7872"):
        assert f'"base_url": "http://192.168.50.10:{port}"' in readme, port
