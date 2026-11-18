"""apps/jobs/cursors.py: the ONE reader and writer of `background_job_cursors`
(sql_meshychat/023), the rows the bounded jobs walk a table by.

Four job modules (the frame audit, the chain-head reconcile, frame retention, the running-call
heartbeat) each carried a private SELECT and INSERT ... ON CONFLICT over the table, three of
them naming only the columns that job used. The helper writes the row whole. What these hold:

  * the contract: an unsaved cursor reads None and starts at (0, no key, 0 laps); a save writes
    every column; a second save replaces every column and stamps `updated_at`;
  * the helper runs in the caller's transaction and opens none of its own, which the frame
    audit depends on: its two cursors move with what the tick read, or not at all;
  * the three narrow spellings, kept here verbatim, write the row `save` writes for the same
    arguments, inserting and updating, so no module's stored rows changed when it moved;
  * the census: no module under apps/ but the helper spells the table in its code, so a fifth
    hand-rolled copy fails here.

Run:  .venv313/Scripts/python.exe -m pytest tests/test_job_cursors.py -q
"""
from __future__ import annotations

import ast
import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

from apps.core import dbconn  # noqa: E402
from apps.jobs import cursors  # noqa: E402

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")


@pytest.fixture()
def app():
    """The job's own connection: meshychat_app, autocommit, as the runner hands a job body."""
    c = dbconn.connect(DSN)
    try:
        yield c
    finally:
        c.close()


def _name() -> str:
    return f"t_cursors.{uuid.uuid4().hex[:8]}"


def _row(conn, name: str) -> tuple | None:
    return conn.execute("SELECT position, position_key, laps, updated_at "
                        "FROM background_job_cursors WHERE cursor_name = %s", (name,)).fetchone()


class _Abort(Exception):
    pass


# ------------------------------------------------------------------ the contract

def test_an_unsaved_cursor_reads_none_and_starts_at_zero(app):
    """`read` says a job has never saved the cursor (the frame audit's first tick starts its new
    pass at the newest frame on that answer); `read_or_start` is where a rolling pass begins."""
    name = _name()
    assert cursors.read(app, name) is None
    assert cursors.read_or_start(app, name) == {"position": 0, "position_key": None, "laps": 0}
    assert _row(app, name) is None, "reading saved nothing"


def test_save_writes_every_column_and_a_second_save_replaces_them(app):
    name = _name()
    cursors.save(app, name, position=42, key="k", laps=3)
    first = _row(app, name)
    assert first[:3] == (42, "k", 3)
    assert cursors.read(app, name) == {"position": 42, "position_key": "k", "laps": 3}
    assert cursors.read_or_start(app, name) == cursors.read(app, name)

    cursors.save(app, name, position=7)                 # the defaults replace, not keep
    second = _row(app, name)
    assert second[:3] == (7, None, 0)
    assert second[3] >= first[3], "updated_at is stamped on the upsert"


def test_save_runs_in_the_callers_transaction_and_opens_none_of_its_own(app):
    """The frame audit saves its two cursors inside its REPEATABLE READ tick transaction so
    they move with what the tick read or not at all: a save the caller rolls back is gone."""
    name = _name()
    with pytest.raises(_Abort):
        with app.transaction():
            cursors.save(app, name, position=3)
            assert cursors.read(app, name)["position"] == 3     # visible to its own transaction
            raise _Abort
    assert cursors.read(app, name) is None


# ------------------------------------------------------------------ the rows each module wrote

#: Each narrow spelling as its module had it, with an insert and an update of the same cursor,
#: beside the `save` call that replaced it. frame_audit never wrote position_key, frame_retention
#: always wrote position 0 and never updated it, the heartbeat wrote position_key alone.
NARROW = {
    "frame_audit": (
        "INSERT INTO background_job_cursors (cursor_name, position, laps) "
        "VALUES (%s, %s, %s) ON CONFLICT (cursor_name) DO UPDATE SET "
        "position = EXCLUDED.position, laps = EXCLUDED.laps, updated_at = now()",
        [((17, 0), dict(position=17)), ((900, 2), dict(position=900, laps=2))]),
    "frame_retention": (
        "INSERT INTO background_job_cursors (cursor_name, position, position_key, "
        "laps) VALUES (%s, 0, %s, %s) ON CONFLICT (cursor_name) DO UPDATE SET "
        "position_key = EXCLUDED.position_key, laps = EXCLUDED.laps, "
        "updated_at = now()",
        [(("a-chain-id", 0), dict(key="a-chain-id", laps=0)), ((None, 1), dict(key=None, laps=1))]),
    "heartbeat": (
        "INSERT INTO background_job_cursors (cursor_name, position_key) VALUES (%s, %s) "
        "ON CONFLICT (cursor_name) DO UPDATE SET position_key = EXCLUDED.position_key, "
        "updated_at = now()",
        [(("2026-09-27T00:00:00+00:00|call-1",), dict(key="2026-09-27T00:00:00+00:00|call-1")),
         ((None,), dict(key=None))]),
}


@pytest.mark.parametrize("module", sorted(NARROW))
def test_the_narrow_spelling_each_module_had_writes_the_row_save_writes(app, module):
    sql, steps = NARROW[module]
    old, new = _name(), _name()
    for params, kwargs in steps:
        app.execute(sql, (old, *params))
        cursors.save(app, new, **kwargs)
        assert _row(app, old)[:3] == _row(app, new)[:3], (module, params, kwargs)


# ------------------------------------------------------------------ the census

def _code_strings(source: str) -> list[str]:
    """Every string literal in `source` except docstrings (the frames fixture's `code_strings`,
    on a file's text rather than an imported module)."""
    tree = ast.parse(source)
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docs.add(id(body[0].value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs]


def test_no_module_but_the_helper_spells_the_cursor_table():
    """A fifth private read/save pair over the table fails here. Docstrings may name the table
    (the job modules say which cursors they keep); code strings may not."""
    helper = ROOT / "apps" / "jobs" / "cursors.py"
    assert any("background_job_cursors" in s
               for s in _code_strings(helper.read_text(encoding="utf-8"))), \
        "control: the scan does not see the helper's own SQL"
    offenders = {}
    for path in sorted((ROOT / "apps").rglob("*.py")):
        if path == helper:
            continue
        hits = [s for s in _code_strings(path.read_text(encoding="utf-8"))
                if "background_job_cursors" in s]
        if hits:
            offenders[path.relative_to(ROOT).as_posix()] = len(hits)
    assert not offenders, (
        f"{offenders}: read and save the cursor through apps/jobs/cursors.py instead")
