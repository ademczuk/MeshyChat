"""apps/calls/events.py: the one `skill_call_events` insert for a call's transitions.

Six copies of the statement lived in apps/calls: route.py's four guarded transitions,
repository.py's `created`, reads.py's `caller_disconnected`, webhooks.py's `webhook_fired`. What
these hold:

  * the row each caller wrote: its event_type; `details` NULL when none is given and the jsonb
    object when one is (never `{}`, a different stored value); source 'meshy' with no peer unless
    the caller names them, as the cancel names 'caller' and the authenticated canceller;
  * it runs on the caller's cursor in the caller's transaction, so a transition rolled back
    takes its event with it (the atomicity `cancel_to_abandoned` and route.py rely on);
  * the census: no module under apps/calls but the helper spells the standalone statement. The
    deadline sweep's `timeout` events come from a CTE in the one statement that times the calls
    out (apps/calls/janitor.py), a different shape, and stay.

Run:  .venv313/Scripts/python.exe -m pytest tests/test_call_events.py -q
"""
from __future__ import annotations

import ast
import os
import re
import sys
import uuid
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

from apps.calls import events  # noqa: E402
from apps.core import dbconn, rls  # noqa: E402
from tests.fixtures.call_rows import seed_call, seed_peer, seed_skill  # noqa: E402

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")


@pytest.fixture()
def owner():
    with psycopg.connect(DSN, autocommit=True) as c:
        yield c


@pytest.fixture()
def app():
    c = dbconn.connect(DSN)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture()
def call(owner) -> dict:
    caller, target = seed_peer(owner, "ev-caller"), seed_peer(owner, "ev-target")
    call_id = seed_call(owner, caller, target, seed_skill(owner, target))
    return {"call_id": call_id, "caller": caller["peer_id"]}


def _events(owner, call_id: str) -> list[tuple]:
    return owner.execute("SELECT event_type, details, source, source_peer_id "
                         "FROM skill_call_events WHERE call_id = %s ORDER BY id",
                         (call_id,)).fetchall()


class _Abort(Exception):
    pass


# ------------------------------------------------------------------ the rows each caller wrote

def test_insert_event_writes_the_row_each_caller_wrote(owner, app, call):
    """In the order the callers write them: `created` (repository), the route's status,
    `running`, an unexecuted failure with its typed details, the cancel's `caller_disconnected`
    with the reason or with NULL, and the doorbell's `webhook_fired`."""
    cid, canceller = call["call_id"], call["caller"]
    with rls.server_internal(app):
        with app.cursor() as cur:
            events.insert_event(cur, cid, "created")
            events.insert_event(cur, cid, "queued")
            events.insert_event(cur, cid, "running")
            events.insert_event(cur, cid, "failed",
                                details={"code": "E_UNVERIFIED_CALL", "message": "refused"})
            events.insert_event(cur, cid, "caller_disconnected", details={"reason": "bye"},
                                source="caller", source_peer_id=str(canceller))
            events.insert_event(cur, cid, "caller_disconnected", details=None,
                                source="caller", source_peer_id=str(canceller))
            events.insert_event(cur, cid, "webhook_fired",
                                details={"attempt_id": 7, "http_status": 204})
    assert _events(owner, cid) == [
        ("created", None, "meshy", None),
        ("queued", None, "meshy", None),
        ("running", None, "meshy", None),
        ("failed", {"code": "E_UNVERIFIED_CALL", "message": "refused"}, "meshy", None),
        ("caller_disconnected", {"reason": "bye"}, "caller", canceller),
        ("caller_disconnected", None, "caller", canceller),
        ("webhook_fired", {"attempt_id": 7, "http_status": 204}, "meshy", None),
    ]


def test_insert_event_lands_with_the_callers_transaction_or_not_at_all(owner, app, call):
    cid = call["call_id"]
    with pytest.raises(_Abort):
        with rls.server_internal(app):
            with app.cursor() as cur:
                events.insert_event(cur, cid, "running")
            raise _Abort
    assert _events(owner, cid) == []


# ------------------------------------------------------------------ the census

#: The standalone statement: the table, its column list, then VALUES. The janitor's CTE form
#: follows its column list with SELECT and does not match.
_STANDALONE = re.compile(r"INSERT INTO skill_call_events\s*\([^)]*\)\s*VALUES", re.I)


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


def _spells_it(path: Path) -> bool:
    return bool(_STANDALONE.search("\n".join(_code_strings(path.read_text(encoding="utf-8")))))


def test_no_module_in_apps_calls_but_the_helper_spells_the_event_insert():
    calls = ROOT / "apps" / "calls"
    assert _spells_it(calls / "events.py"), "control: the scan does not see the helper's SQL"
    assert not _spells_it(calls / "janitor.py"), "control: the sweep's CTE is a different shape"
    offenders = sorted(p.name for p in calls.rglob("*.py")
                       if p.name != "events.py" and _spells_it(p))
    assert not offenders, f"{offenders}: write the event through apps/calls/events.py instead"
