"""The HTTP sync wait reads terminality from the ONE terminal mapping
(`apps/calls/terminal.TERMINAL_STATUSES`, divergence 041 S6), not from a copy of it.

`views._await_terminal` shadowed the imported `terminal` module with a local tuple of the four
terminal statuses: a third copy beside the module's own and retention's, under a name that
would have made `terminal.of_row` raise AttributeError inside that function, and one a fifth
status in sql's CHECK would have left out silently. (The A2A sync wait has the same pin in
tests/test_a2a.py, retention's alias in tests/test_call_retention.py.)

Run:  .venv313/Scripts/python.exe -m pytest tests/test_call_terminal_statuses.py -q
"""
from __future__ import annotations

import inspect
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

from apps.calls import terminal  # noqa: E402
from apps.calls import views  # noqa: E402


def test_the_sync_wait_reads_terminality_from_the_one_terminal_mapping():
    assert terminal.TERMINAL_STATUSES == {"completed", "failed", "timeout", "abandoned"}
    src = inspect.getsource(views._await_terminal)
    assert "terminal.TERMINAL_STATUSES" in src          # control: the scan reads the loop
    assert not re.search(r'"(completed|failed|timeout|abandoned)"', src), src
    # The module name is not rebound inside the function, so `terminal.` there is the module.
    assert "terminal =" not in src, src
