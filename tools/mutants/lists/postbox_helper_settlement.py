"""Mutants for spawning records with no gate proof (P2-2): tests/test_postbox_helper_spawning_records.py, on every platform.

The gate's own mutants, 48 of them, live with oxxxxxx's harness for r3f; the POSIX tests they exercise are in
tests/test_postbox_helper_settlement.py.
"""
from __future__ import annotations

OS = "tools/postbox/postbox_os.py"
OWED = "tools/postbox/owedline.py"
T = ["tests/test_postbox_helper_spawning_records.py"]

MUTANTS = [
    ("helper-spawn:S1 a no-gate record blocks that kind for good", OS,
     'elif "gated" in rec and rec["gated"] is None:', "elif False:", T),
    ("helper-spawn:S2 Windows records say False, so they hold", OS,
     "gated=None if IS_WINDOWS else gated)", "gated=gated)", T),
    ("helper-spawn:S3 None is written as False", OS,
     '"gated": None if gated is None else bool(gated), "pid": None,', '"gated": bool(gated), "pid": None,', T),
    ("helper-spawn:S4 an old record never expires", OS,
     "elif _spawning_record_expired(rec):", "elif False:", T),
    ("helper-spawn:S5 an old record expires at once", OS,
     "return time.time() - started > limit + HELPER_SPAWNING_EXPIRY_MARGIN_S", "return True", T),
    ("helper-spawn:S6 the margin is not added", OS,
     "started > limit + HELPER_SPAWNING_EXPIRY_MARGIN_S", "started > limit", T),
    ("helper-spawn:S7 a record with no bound expires", OS,
     'if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in (started, limit)):\n        return False',
     'if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in (started, limit)):\n        return True', T),
    ("helper-spawn:S8 a bool passes as a time", OS,
     "if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in (started, limit)):",
     "if any(not isinstance(v, (int, float)) for v in (started, limit)):", T),
    ("helper-spawn:S9 owed starts a lookup over an unsettled one", OWED,
     'if not settled.get("settled"):', "if False:", T),
]
