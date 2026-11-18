"""The HTTP scaffolding the call transports, the board and the mesh graph share, pinned as ONE.

apps/calls/views.py and apps/a2a/views.py each carried the same request helpers (the
415/400/422 body parse, the 422 constructor, the `{peer_id}` 400, the 404-then-403 call
read, the envelope's field checks, the sync wait's settings read) and the same
connection-and-error body under their route wrappers; apps/board/views.py a third copy of the
body parse. apps/board/views.py and apps/mesh/views.py each carried the read-model-to-JSON
conversion, and the board's write door and the capability routes inlined
`peer_limit.http_response`. Each is now one function, bound under the old private names.

What each test holds, and why it is not just an identity check:
  * the bindings ARE the shared functions (on the old layout `apps.calls.http` and
    `apps.core.jsonable` do not exist, so every test here fails at its import);
  * the connection body logs under EACH module's own logger (`meshychat.calls`,
    `meshychat.a2a`) and answers both with the same bytes - the attribution a shared body
    could have lost;
  * each transport's sync wait is bounded by ITS OWN ceiling constant;
  * the stream's page drain carries the cursor across pages and the terminal frame is the
    bytes the inline construction gave.

No database: every connection here is a stub, so the file runs anywhere.

Run:  pytest tests/test_http_scaffold_shared.py -q -p no:cacheprovider
"""
from __future__ import annotations

import contextlib
import dataclasses
import datetime as _dt
import json
import logging
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
import django  # noqa: E402

django.setup()

from django.test import RequestFactory  # noqa: E402


def test_both_transports_bind_the_one_set_of_request_helpers():
    from apps.a2a import views as a2a_views
    from apps.calls import http
    from apps.calls import views as calls_views

    for name, shared in (("_json_body", http.json_body),
                         ("_unprocessable", http.unprocessable),
                         ("_peer_uuid", http.peer_uuid),
                         ("_resolve_visible_call", http.resolve_visible_call),
                         ("_envelope_from", http.envelope_from)):
        assert getattr(calls_views, name) is shared, name
        assert getattr(a2a_views, name) is shared, name
    # The board keeps its own copy of the body parse: binding it here would make importing
    # apps.board.views load apps.calls' modules.
    # CONTROL: what stays per module stays per module (a test patches each one's loop).
    assert calls_views._poll_sleep is not a2a_views._poll_sleep
    assert calls_views._await_terminal is not a2a_views._await_terminal


def _serving_stub(monkeypatch):
    from apps.core import dbconn
    monkeypatch.setattr(dbconn, "serving_connection",
                        lambda *a, **k: contextlib.nullcontext(object()))


def test_one_connection_body_logs_under_each_transports_own_logger(monkeypatch, caplog):
    """`calls._route` and `a2a._guarded` both run `http.serve`, each passing its own
    logger: an unhandled error is the same 500 envelope from both, and its traceback is
    logged under the module the route belongs to."""
    from apps.a2a import views as a2a_views
    from apps.calls import http
    from apps.calls import views as calls_views

    _serving_stub(monkeypatch)
    loggers: list[str] = []
    real_serve = http.serve

    def spying_serve(log, handler, request, /, *args, **kwargs):
        loggers.append(log.name)
        return real_serve(log, handler, request, *args, **kwargs)

    monkeypatch.setattr(http, "serve", spying_serve)

    def handler(request, conn, call_id):
        raise RuntimeError("tenant-private detail")

    request = RequestFactory().get("/probe")
    bodies = {}
    for name, route in (("meshychat.calls", calls_views._route("GET")(handler)),
                        ("meshychat.a2a", a2a_views._route("GET")(handler))):
        caplog.clear()
        with caplog.at_level(logging.ERROR):
            response = route(request, call_id="c-1")
        assert response.status_code == 500
        assert [r.name for r in caplog.records] == [name], caplog.records
        assert "tenant-private detail" not in response.content.decode()
        bodies[name] = (response.content, response["Content-Type"])
    assert loggers == ["meshychat.calls", "meshychat.a2a"]
    assert bodies["meshychat.calls"] == bodies["meshychat.a2a"]
    assert json.loads(bodies["meshychat.a2a"][0]) == {"error": "E_INTERNAL",
                                                      "message": "internal error"}


def test_each_transport_bounds_the_one_sync_wait_by_its_own_ceiling(monkeypatch):
    from apps.a2a import views as a2a_views
    from apps.calls import http
    from apps.calls import views as calls_views

    seen: list[float] = []
    monkeypatch.setattr(http, "sync_poll_seconds", lambda ceiling: seen.append(ceiling) or 1.5)
    assert calls_views._sync_poll_seconds() == 1.5
    assert a2a_views._sync_poll_seconds() == 1.5
    assert seen == [calls_views.SYNC_TIMEOUT_SECONDS, a2a_views.A2A_SYNC_TIMEOUT_SECONDS]
    # Read at call time, so a patched ceiling is the one applied.
    monkeypatch.setattr(a2a_views, "A2A_SYNC_TIMEOUT_SECONDS", 7.0)
    a2a_views._sync_poll_seconds()
    assert seen[-1] == 7.0


@dataclasses.dataclass(frozen=True)
class _Act:
    key: str
    recorded_at: _dt.datetime
    tags: tuple


def test_the_board_and_the_mesh_graph_convert_through_one_jsonable():
    from apps.board import views as board_views
    from apps.core.jsonable import jsonable
    from apps.mesh import views as mesh_views

    assert board_views._jsonable is jsonable
    assert mesh_views._jsonable is jsonable
    at = _dt.datetime(2026, 10, 2, 12, 0, tzinfo=_dt.UTC)
    got = jsonable({"acts": [_Act("msg/1", at, ("a", "b"))], "n": 1})
    assert got == {"acts": [{"key": "msg/1", "recorded_at": "2026-10-02T12:00:00+00:00",
                             "tags": ["a", "b"]}], "n": 1}
    # The dataclass's field order is the wire's key order.
    assert list(got["acts"][0]) == ["key", "recorded_at", "tags"]


class _Auth:
    def __init__(self, peer_id):
        self.peer_id = peer_id


def test_the_board_door_and_the_capability_routes_render_429_through_peer_limit(monkeypatch):
    from apps.board import views as board_views
    from apps.core import peer_limit
    from apps.memoryskills import views as cap_views

    _serving_stub(monkeypatch)
    peer = uuid.uuid4()

    def refuse(peer_id, *, transport, bucket=peer_limit.CALLS):
        raise peer_limit.PeerRateLimited(str(peer_id), transport, bucket, 4)

    monkeypatch.setattr(peer_limit, "admit", refuse)
    rendered: list = []
    real = peer_limit.http_response

    def spying_render(exc):
        response = real(exc)
        rendered.append(response)
        return response

    monkeypatch.setattr(peer_limit, "http_response", spying_render)
    for module in (board_views, cap_views):
        monkeypatch.setattr(module, "authenticate_peer", lambda conn, request: _Auth(peer))
    monkeypatch.setattr(cap_views, "_tokens_available", lambda: True)
    monkeypatch.setattr(cap_views.capability_keys, "current", lambda: object())

    request = RequestFactory().post("/probe", data="{}", content_type="application/json")
    board = board_views._write_route(lambda request, conn, auth: None)(request)
    cap = cap_views._route(lambda *a: None)(request)
    assert rendered == [board, cap]
    for response in (board, cap):
        assert response.status_code == 429
        assert response["Retry-After"] == "4"
        assert json.loads(response.content)["error"] == peer_limit.E_RATE_LIMITED


def test_the_stream_drains_a_page_through_one_helper_and_closes_with_one_frame(monkeypatch):
    from apps.a2a import types
    from apps.a2a import views as a2a_views
    from apps.calls import reads

    monkeypatch.setattr(a2a_views, "STREAM_CHUNK_PAGE", 2)
    asked: list = []
    pages = [reads.Rows(rows=[{"sequence": 3, "chunk_type": types.CHUNK_TYPE_TEXT,
                               "content": b"a", "file_artifact_id": None},
                              {"sequence": 4, "chunk_type": types.CHUNK_TYPE_TEXT,
                               "content": b"b", "file_artifact_id": None}]),
             reads.Empty(scope="probe")]

    def list_chunks(conn, call_id, since_seq, *, limit=None):
        asked.append((since_seq, limit))
        return pages.pop(0)

    monkeypatch.setattr(reads, "list_chunks", list_chunks)

    def run(gen):
        frames = []
        while True:
            try:
                frames.append(next(gen))
            except StopIteration as stop:
                return frames, stop.value

    frames, result = run(a2a_views._drain_page(None, "c-1", 2, False))
    assert result == (2, 4, True)          # a FULL page, the cursor moved, append now on
    assert frames == [
        a2a_views._sse_frame("TaskArtifactUpdateEvent",
                             types.chunk_to_artifact_event("c-1", {"chunk_type": "text",
                                                                   "content": b"a",
                                                                   "file_artifact_id": None},
                                                           False)),
        a2a_views._sse_frame("TaskArtifactUpdateEvent",
                             types.chunk_to_artifact_event("c-1", {"chunk_type": "text",
                                                                   "content": b"b",
                                                                   "file_artifact_id": None},
                                                           True)),
    ]
    frames, result = run(a2a_views._drain_page(None, "c-1", 4, True))
    assert (frames, result) == ([], (None, 4, True))   # not Rows: no frame, cursor kept
    assert asked == [(2, 2), (4, 2)], "the page size is the module's, read per page"

    assert a2a_views._terminal_frame("c-1", types.TASK_STATE_FAILED) == a2a_views._sse_frame(
        "TaskStatusUpdateEvent", types.task_status_update_event(
            task_id="c-1", status=types.task_status(state=types.TASK_STATE_FAILED)))
