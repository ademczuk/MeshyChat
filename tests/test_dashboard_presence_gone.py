"""REFACTOR-QUEUE-2026-11 B3: apps/dashboard/presence.py read `ws_online` from a Redis key nothing writes, and has
been dead since e1b56b16 (2026-11-09), when the dashboard's peers roster took its liveness from hub_connections. Its
only reference was an unused import in views.py. It is deleted, and with it the second copy of the RESP Redis client
(meshychat/urls.py keeps the only one). These pin both, and need no database."""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_the_dead_redis_presence_module_is_gone():
    assert not (ROOT / "apps" / "dashboard" / "presence.py").exists()
    assert importlib.util.find_spec("apps.dashboard.presence") is None


def test_every_sibling_module_views_imports_is_used():
    """An unused `from . import x` kept a dead module looking alive; this refuses the next one."""
    tree = ast.parse((ROOT / "apps" / "dashboard" / "views.py").read_text(encoding="utf-8"))
    bound = [alias.asname or alias.name for node in tree.body
             if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module is None
             for alias in node.names]
    used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert bound, "views.py imports its sibling modules with `from . import ...`"
    assert [name for name in bound if name not in used] == []
