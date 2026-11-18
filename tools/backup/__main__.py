"""`python -m tools.backup ...`. See the package docstring."""
from __future__ import annotations

import sys
from pathlib import Path

# `python -m tools.backup` from the repository root already has the root on sys.path; from
# anywhere else it does not, and apps.core would not import (the sqlmigrate.py lesson).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.backup.pgbackup import main  # noqa: E402

raise SystemExit(main())
