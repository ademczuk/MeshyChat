"""`python -m tools.meshy_node owed`: the acks this node owes (GET /v1/mesh/owed).

The route has existed since divergence 018 and the client had no command for it, so a seat could
not read its own ack debts without hand-built HTTP (found 2026-10-01 while a roll call was being
assembled by hand). Against a recording server, no database:

  * the command sends a bearer GET to /v1/mesh/owed, with `limit` only when one is given, so the
    server's default and bounds apply otherwise;
  * a bare command adds the two client-side settlement buckets; `--counts-only` prints their counts only;
  * exit codes as cli.py says: 0 on a 200, 1 on any other answer.

Run:  pytest tests/test_meshy_node_owed.py -q     (needs no database)
"""
from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
import types
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from apps.mesh import views as server_views  # noqa: E402
from apps.peers import errors as server_errors  # noqa: E402
from tools.meshy_node import cli, node, wire  # noqa: E402

ANSWER = {
    "label": "otter", "label_source": "roster",
    "labels": [{"label": "otter", "source": "roster"}],
    # One item in the shape apps/mesh/views.py `owed` answers with: graph.OwedAck as JSON.
    "owed": [{"hit": {"key": "msg/1790000000000-aaaa1111", "version": 1, "edge_type": "owes_ack",
                      "dst": "otter", "src_field": "to", "recorded_at": "2026-10-01T10:00:00+00:00",
                      "author": "heron"},
              "thread_id": "ops/lease", "answered_in_thread": False}],
    "coverage": {"projected": 1, "total": 1},
}


def _as_the_owed_view_answers(query: str, body: dict) -> tuple[int, dict]:
    """The owed route's own answer to `query`, its list taken from `body`. apps/mesh/views.py `_owed_filters`,
    called as the owed view calls it, parses the bounds and builds the `filters` echo, so this stand-in echoes
    exactly what the route applies rather than a copy of its rule; a bound the view refuses is the view's 422."""
    request = types.SimpleNamespace(GET=dict(urllib.parse.parse_qsl(query, keep_blank_values=True)))
    try:
        *_, applied = server_views._owed_filters(request, with_thread=True)
    except server_errors.ApiError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    return 200, ({**body, "filters": applied} if applied else body)


class _Server(http.server.BaseHTTPRequestHandler):
    seen: list = []
    status = 200
    body: dict = ANSWER
    routes: dict = {}  # path -> (status, body), for a test whose command calls more than one route
    echo = None  # (query, body) -> (status, body): answer as the route does (_as_the_owed_view_answers)

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        type(self).seen.append({"path": parsed.path,
                                "query": dict(urllib.parse.parse_qsl(parsed.query)),
                                "headers": {k.lower(): v for k, v in self.headers.items()}})
        status, body = type(self).routes.get(parsed.path, (type(self).status, type(self).body))
        if type(self).echo is not None:
            status, body = type(self).echo(parsed.query, body)
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


@pytest.fixture()
def server():
    handler = type("S", (_Server,), {"seen": [], "status": 200, "body": ANSWER, "routes": {}, "echo": None})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    handler.base_url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield handler
    srv.shutdown()
    srv.server_close()


def _state(tmp_path: Path, base_url: str) -> Path:
    seed = wire.new_seed()
    path = tmp_path / "node.json"
    path.write_text(json.dumps({
        "schema": node.STATE_SCHEMA, "base_url": base_url, "status": "minted",
        "signing_seed_b64url": wire.b64u(seed),
        "public_key_b64url": wire.b64u(wire.public_key_of(seed)),
        "peer_id": str(uuid.uuid4()), "owner_id": str(uuid.uuid4()), "kid": "desktop-x",
        "jwt": "h.p.s", "jwt_expires_at": "2999-01-01T00:00:00Z",
        "memory_peer_id": node.DEFAULT_MEMORY_PEER_ID}), encoding="utf-8")
    return path


def _run(server, path, *args) -> int:
    return cli.main(["owed", "--base-url", server.base_url, "--state-file", str(path), *args])


def test_owed_sends_no_limit_and_prints_the_answer_with_bearer_auth(tmp_path, server, capsys):
    path = _state(tmp_path, server.base_url)
    assert _run(server, path, "--state", "all", "--since", "all") == 0
    assert json.loads(capsys.readouterr().out) == ANSWER
    (req,) = server.seen
    assert req["path"] == "/v1/mesh/owed"
    assert req["query"] == {}
    assert req["headers"]["authorization"] == "Bearer h.p.s"


def test_a_bare_owed_asks_for_the_last_14_days_and_all_asks_for_history(tmp_path, server, capsys, monkeypatch):
    """2026-10-07: unbounded, owed was mostly history (3,518 of 3,859 unclosed owed asks on production predated
    the 27 Sep cutover). A bare `owed` now sends since = now - 14 days, and the echo check applies to it like
    any named filter. `--since all` sends no since at all, the old unbounded answer.
    MUTANT: drop the default; the bare call sends no since. MUTANT: treat `all` as a relative age; it is refused."""
    path = _state(tmp_path, server.base_url)
    fixed = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)
    real = cli._owed_since
    monkeypatch.setattr(cli, "_owed_since", lambda raw, now=None: real(raw, now=lambda: fixed))
    server.echo = _as_the_owed_view_answers
    assert _run(server, path) == 0
    capsys.readouterr()
    # SD 255: the list is state=open; the two add-ons ask for the other states over the same window
    assert [r["query"] for r in server.seen] == [
        {"state": "open", "since": "2026-09-23T12:00:00Z"},
        {"state": "active", "since": "2026-09-23T12:00:00Z", "limit": str(cli.OWED_ADDON_LIMIT)},
        {"state": "answered", "since": "2026-09-23T12:00:00Z", "limit": str(cli.OWED_ADDON_LIMIT)}]
    del server.seen[:]
    assert _run(server, path, "--since", "all") == 0
    capsys.readouterr()
    assert [r["query"] for r in server.seen] == [{"state": "open"},
                                                 {"state": "active", "limit": str(cli.OWED_ADDON_LIMIT)},
                                                 {"state": "answered", "limit": str(cli.OWED_ADDON_LIMIT)}]


def test_a_bare_owed_lists_reply_owed_and_answered_still_open_and_state_all_lists_every_state(tmp_path, server, capsys):
    """2026-10-10: with no state, owed listed every state, so answered asks filled it (on pxx, 38 rows, all
    `answered`). #373 then sent state=open,active,answered from a bare `owed`, and SD 255 (7 Nov) narrowed the bare
    list to state=open (the next test); #373's view is `--state open,active,answered`. The echo check applies to both.
    `--state all` sends no state, the old answer.
    MUTANT: drop the bare state; the bare call sends no state and is not refused. MUTANT: send `all` as a state; the
    query names it."""
    path = _state(tmp_path, server.base_url)
    server.body = {**ANSWER, "filters": {"state": ["open", "active", "answered"]}}
    assert _run(server, path, "--state", cli.OWED_DEFAULT_STATE, "--since", "all") == 0
    capsys.readouterr()
    assert server.seen[-1]["query"] == {"state": "open,active,answered"}
    server.body = ANSWER
    assert _run(server, path, "--since", "all") == 1, "an answer that does not echo the bare state is refused"
    assert json.loads(capsys.readouterr().out)["error"] == node.E_FILTERS_NOT_APPLIED
    assert server.seen[-1]["query"] == {"state": "open"}, "no add-on runs after a refused list"
    assert _run(server, path, "--state", "all", "--since", "all") == 0
    assert json.loads(capsys.readouterr().out) == ANSWER
    assert server.seen[-1]["query"] == {}


def test_bare_owed_splits_reply_owed_from_answered_still_open_and_counts_only_hides_routing(tmp_path, server, capsys):
    """PR3 steps 2 and 3: the normal read shows the two settlement buckets, while the
    weekly measurement form exposes their counts only.

    MUTANT: omit answered from the default state; the answered row never reaches either
    output. MUTANT: count answered as reply owed; the two bucket totals are wrong.
    MUTANT: print the raw owed rows under --counts-only; a measurement leaks routing data.
    """
    path = _state(tmp_path, server.base_url)
    reply = {"hit": {"key": "msg/1790000000100-heron-open", "author": "heron"}, "state": "open", "msg_type": "question"}
    active = {"hit": {"key": "msg/1790000000101-heron-active", "author": "heron"}, "state": "active", "msg_type": "request"}
    answered = {"hit": {"key": "msg/1790000000102-heron-answered", "author": "heron"}, "state": "answered"}
    server.body = {"owed": [reply, active, answered],
                   "filters": {"state": ["open", "active", "answered"]}}
    # SD 255: #373's view is now asked for by name, and it is the same view
    assert _run(server, path, "--state", "open,active,answered", "--since", "all") == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["buckets"]["reply_owed"] == {"count": 2, "items": [reply, active], "truncated": False}
    assert shown["buckets"]["answered_still_open"] == {"count": 1, "items": [answered], "truncated": False}
    assert server.seen[-1]["query"] == {"state": "open,active,answered"}

    # --counts-only with no --state still reads #373's state; SD 255 only adds the open/active split
    assert _run(server, path, "--since", "all", "--counts-only") == 0
    measured = json.loads(capsys.readouterr().out)
    assert measured == {"reply_owed": 2, "answered_still_open": 1,
                        "reply_owed_by_type": {"question": 1, "request": 1, "unknown": 0},
                        "answered_still_open_by_type": {"question": 0, "request": 0, "unknown": 1},
                        "open": 1, "replied_in_thread_not_linked": 1,
                        "truncated": False, "filters": {"state": ["open", "active", "answered"]}}
    assert server.seen[-1]["query"] == {"state": "open,active,answered"}
    assert "msg/" not in json.dumps(measured) and "heron" not in json.dumps(measured)


def test_counts_only_deduplicates_the_same_ask_owed_under_an_alias(tmp_path, server, capsys):
    path = _state(tmp_path, server.base_url)
    question = {"hit": {"key": "msg/1790000000100-heron-question", "author": "heron"},
                "state": "open", "msg_type": "question"}
    server.body = {"owed": [question, {**question, "hit": {**question["hit"], "dst": "alias"}}],
                   "filters": {"state": ["open", "active", "answered"]}}
    assert _run(server, path, "--since", "all", "--counts-only") == 0
    measured = json.loads(capsys.readouterr().out)
    assert measured["reply_owed"] == 1
    assert measured["reply_owed_by_type"] == {"question": 1, "request": 0, "unknown": 0}


@pytest.mark.parametrize(("state", "expected"), [
    ("open,active", {"reply_owed": 1, "answered_still_open": None,
                     "reply_owed_by_type": {"question": 1, "request": 0, "unknown": 0},
                     "answered_still_open_by_type": None}),
    ("answered", {"reply_owed": None, "answered_still_open": 1,
                  "reply_owed_by_type": None,
                  "answered_still_open_by_type": {"question": 0, "request": 1, "unknown": 0}}),
])
def test_counts_only_marks_buckets_outside_its_state_filter_null(tmp_path, server, capsys, state, expected):
    path = _state(tmp_path, server.base_url)
    server.body = {"owed": [
        {"hit": {"key": "msg/1790000000100-heron-question"}, "state": "open", "msg_type": "question"},
        {"hit": {"key": "msg/1790000000101-heron-request"}, "state": "answered", "msg_type": "request"},
    ], "filters": {"state": state.split(",")}}
    assert _run(server, path, "--state", state, "--since", "all", "--counts-only") == 0
    measured = json.loads(capsys.readouterr().out)
    assert {key: measured[key] for key in expected} == expected


def test_owed_sends_a_given_limit(tmp_path, server, capsys):
    path = _state(tmp_path, server.base_url)
    assert _run(server, path, "--limit", "5", "--state", "all", "--since", "all") == 0
    capsys.readouterr()
    (req,) = server.seen
    assert req["path"] == "/v1/mesh/owed"
    assert req["query"] == {"limit": "5"}


def test_owed_sends_exactly_the_given_state_and_iso_filters(tmp_path, server, capsys):
    """The client does not rename, widen, or invent query filters before the route sees them.

    MUTANT: omit `state` from node.owed's params; this recording server sees a missing filter.
    """
    path = _state(tmp_path, server.base_url)
    server.body = {**ANSWER, "filters": {"state": ["open", "active", "answered"],
                                        "since": "2026-10-03T10:00:00+02:00"}}
    assert _run(server, path, "--state", "open,active,answered", "--since",
                "2026-10-03T10:00:00+02:00") == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["filters"] == server.body["filters"] and shown["buckets"]["reply_owed"]["count"] == 0
    (req,) = server.seen
    assert req["query"] == {"state": "open,active,answered", "since": "2026-10-03T10:00:00+02:00"}


def test_owed_sends_a_given_thread_beside_the_default_state_and_checks_its_echo(tmp_path, server, capsys):
    """#155 (carried forward from #165 by a reviewer). `--thread` is sent as `thread`, beside the state a bare
    `owed` sends since #195, and the answer must echo both. The stand-in answers as the route does: its echo is
    the owed view's own `_owed_filters`, so #165's fixed reply, which echoed the thread alone, is no longer the
    route's answer once a bare `owed` sends state=open,active,answered.

    MUTANT: drop `thread` from node.owed's params; the query has no thread. MUTANT: drop the thread argument at
    cli.py's node.owed call; likewise. MUTANT: drop #195's `states=` there; the query has no state.
    """
    path = _state(tmp_path, server.base_url)
    server.echo = _as_the_owed_view_answers
    assert _run(server, path, "--thread", "ops/hints", "--since", "all") == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["filters"] == {"state": ["open"], "thread": "ops/hints"}
    assert shown["buckets"]["reply_owed"]["count"] == 1, "ANSWER's one row has no state: the route filtered it open"
    assert {r["path"] for r in server.seen} == {"/v1/mesh/owed"}
    # SD 255: the list and both add-ons stay in the one thread
    assert [r["query"] for r in server.seen] == [
        {"state": "open", "thread": "ops/hints"},
        {"state": "active", "thread": "ops/hints", "limit": str(cli.OWED_ADDON_LIMIT)},
        {"state": "answered", "thread": "ops/hints", "limit": str(cli.OWED_ADDON_LIMIT)}]
    active = shown["buckets"]["replied_in_thread_not_linked"]
    assert active["count"] == 1 and "list" not in active, "one active key fits the page: shown, no list command"
    assert shown["buckets"]["answered_not_closed"]["list"].endswith("--thread ops/hints")
    del server.seen[:]
    assert _run(server, path, "--thread", "ops/hints", "--state", "all", "--since", "all") == 0
    assert json.loads(capsys.readouterr().out) == {**ANSWER, "filters": {"thread": "ops/hints"}}
    assert server.seen[-1]["query"] == {"thread": "ops/hints"}
    assert _run(server, path, "--thread", "", "--since", "all") == 1, "a blank thread is the route's 422"
    out = json.loads(capsys.readouterr().out)
    assert out["http_status"] == 422 and out["body"]["error"] == "E_VALIDATION", out


def test_a_filter_the_server_did_not_echo_exits_1_and_prints_no_list(tmp_path, server, capsys):
    """Roadmap item 8.0. A server before divergence 070 ignores `state` and `since` and answers
    200 with the whole list. Measured on production 2026-10-04: `owed --state open --since 14d`
    from a main client printed the unfiltered 100 rows with exit 0. The echo is the only thing
    that tells the two answers apart, so a missing or different echo is refused.

    CONTROL: the same answer WITH the echo exits 0 (the test above), and `owed --state all --since all`
    against the echo-less answer exits 0 (the last assert), so only a filter that was sent is checked.
    MUTANT: make node.filters_echoed return True; every case here exits 0 and prints the list.
    MUTANT: compare only the `state` key; the wrong-since case exits 0.
    MUTANT: drop `expected["thread"]` in node.owed; the pre-#155 server's answer exits 0.
    """
    path = _state(tmp_path, server.base_url)
    cases = [
        (ANSWER, ["--state", "open", "--since", "all"], {"state": ["open"]}),     # a pre-070 server
        (ANSWER, ["--since", "2026-10-03T10:00:00Z"],
         {"state": ["open"], "since": "2026-10-03T10:00:00Z"}),                   # the bare state (SD 255), unechoed
        ({**ANSWER, "filters": {"state": ["open", "active", "answered"]}}, ["--state", "open", "--since", "all"],
         {"state": ["open"]}),                                                    # another filter applied
        ({**ANSWER, "filters": {"state": ["open"], "since": "2026-10-01T00:00:00Z"}},
         ["--state", "open", "--since", "2026-10-03T10:00:00Z"],
         {"state": ["open"], "since": "2026-10-03T10:00:00Z"}),                   # the wrong since
        ({**ANSWER, "filters": {"state": ["open"]}},
         ["--state", "open", "--since", "2026-10-03T10:00:00Z"],
         {"state": ["open"], "since": "2026-10-03T10:00:00Z"}),                   # half an echo
        ({**ANSWER, "filters": {"state": ["open"]}}, ["--state", "open", "--thread", "ops/hints", "--since", "all"],
         {"state": ["open"], "thread": "ops/hints"}),                              # a server before #155
        ({**ANSWER, "filters": {"state": ["open"], "thread": "ops/other"}},
         ["--state", "open", "--thread", "ops/hints", "--since", "all"],
         {"state": ["open"], "thread": "ops/hints"}),                              # another thread applied
    ]
    for body, flags, sent in cases:
        server.body = body
        assert _run(server, path, *flags) == 1, (flags, body.get("filters"))
        out = json.loads(capsys.readouterr().out)
        assert out["error"] == node.E_FILTERS_NOT_APPLIED and out["sent"] == sent, out
        assert out["echoed"] == body.get("filters"), out
        assert "owed" not in out, "the list must not be printed as if it were filtered"
    server.body = ANSWER
    assert _run(server, path, "--state", "all", "--since", "all") == 0 and json.loads(capsys.readouterr().out) == ANSWER


def _accepted_send(tmp_path, server, monkeypatch, capsys, *flags) -> list[dict]:
    """One `send` through cli.main that the (faked) server ACCEPTS: asserts exactly one signed write
    was invoked, with the type the flags named, and that the command exits 0 and prints the result.
    Returns the stderr JSON lines."""
    path = _state(tmp_path, server.base_url)
    invoked = []

    def fake_invoke(*args, **kwargs):
        invoked.append(kwargs)
        return {"status": "completed", "result": {}, "result_signature_verified": True}

    monkeypatch.setattr(node, "invoke", fake_invoke)
    assert cli.main(["send", "--base-url", server.base_url, "--state-file", str(path),
                     "--to", "otter", "--thread", "ops/hints", "--body", "synthetic", *flags]) == 0
    (call,) = invoked
    wanted_type = flags[flags.index("--type") + 1] if "--type" in flags else "note"
    assert call["args"]["value"]["type"] == wanted_type, call["args"]["value"]
    captured = capsys.readouterr()
    assert json.loads(captured.out)["status"] == "completed"
    return [json.loads(line) for line in captured.err.splitlines() if line.strip()]


@pytest.mark.parametrize("msg_type", ["request", "question"])
def test_an_ask_sent_without_expects_reply_gets_one_hint_and_is_still_sent(tmp_path, server, monkeypatch,
                                                                           capsys, msg_type):
    """Roadmap item 8.0. Only `requires_ack` makes the owes_ack edge; a `request` without it is in
    nobody's `owed`. The hint is one stderr line AFTER an accepted send, never a refusal. The two
    types are written out here, not read from cli.ASK_TYPES, so emptying that constant fails this.

    CONTROL: the same type WITH --expects-reply, and a plain note, print no hint.
    MUTANT: drop the `not requires_ack` test in cli._send_hints; the flagged ask hints too.
    MUTANT: return [] from cli._send_hints; the first assert finds no hint.
    MUTANT: set cli.ASK_TYPES to (); likewise.
    """
    (hint,) = _accepted_send(tmp_path, server, monkeypatch, capsys, "--type", msg_type)
    assert hint["hint"] == "ask_not_tracked" and hint["type"] == msg_type, hint
    assert "--expects-reply" in hint["note"], hint
    assert _accepted_send(tmp_path, server, monkeypatch, capsys, "--type", msg_type, "--expects-reply") == []
    assert _accepted_send(tmp_path, server, monkeypatch, capsys) == [], "the default type is a note"


@pytest.mark.parametrize("msg_type", ["answer", "ack"])
def test_an_answer_sent_without_in_reply_to_gets_one_hint(tmp_path, server, monkeypatch, capsys, msg_type):
    """Only `in_reply_to` makes the replies_to edge that marks an ask answered (owed state
    `answered`, divergence 070). On production 96.9% of answers carry it (2026-10-04), so this is
    a guard for the rest. The types are literals here, as above.

    CONTROL: the same answer WITH --in-reply-to prints no hint, and so does one that only --closes the
    ask (#89: a close is a link too; the hint used to tell that seat its answer was unlinked).
    MUTANT: drop the `not in_reply_to` test in cli._send_hints; the linked answer hints too.
    MUTANT: drop the `not closes` test, or the closes argument at the call; the closing answer hints.
    MUTANT: set cli.ANSWER_TYPES to (); the first assert finds no hint.
    """
    (hint,) = _accepted_send(tmp_path, server, monkeypatch, capsys, "--type", msg_type)
    assert hint["hint"] == "answer_not_linked" and "--in-reply-to" in hint["note"], hint
    assert _accepted_send(tmp_path, server, monkeypatch, capsys, "--type", msg_type,
                          "--in-reply-to", "msg/1790000000000-aaaa1111") == []
    assert _accepted_send(tmp_path, server, monkeypatch, capsys, "--type", msg_type,
                          "--closes", "msg/1790000000000-aaaa1111") == []


def test_a_send_the_server_did_not_accept_prints_no_hint(tmp_path, server, monkeypatch, capsys):
    """A hint is about a message that exists. A refused write gets the failure and nothing else.

    MUTANT: print the hints whether or not the send succeeded; stderr is not empty here.
    """
    path = _state(tmp_path, server.base_url)
    monkeypatch.setattr(node, "invoke", lambda *a, **k: {"status": "failed", "error": "E_X"})
    assert cli.main(["send", "--base-url", server.base_url, "--state-file", str(path),
                     "--to", "otter", "--thread", "ops/hints", "--type", "request",
                     "--body", "synthetic"]) == 1
    assert capsys.readouterr().err.strip() == ""


@pytest.mark.parametrize("error", [OSError("the pipe is closed"), ValueError("I/O operation on closed file")])
def test_a_hint_that_cannot_be_written_does_not_fail_an_accepted_send(tmp_path, server, monkeypatch, capsys,
                                                                      error):
    """The message is already stored when a hint prints. `main` maps OSError to exit 2 "unreachable",
    and a caller that retries an exit 2 would send the message twice, so a stderr that is closed or
    broken must leave the exit code at 0.

    CONTROL: with a working stderr the same send prints its hint (the tests above).
    MUTANT: write the hint with a bare sys.stderr.write; cli.main returns 2 (OSError) or raises.
    """
    path = _state(tmp_path, server.base_url)
    invoked = []

    def fake_invoke(*args, **kwargs):
        invoked.append(kwargs)
        return {"status": "completed", "result": {}, "result_signature_verified": True}

    class Broken:
        def write(self, text):
            raise error

        def flush(self):
            pass

    monkeypatch.setattr(node, "invoke", fake_invoke)
    monkeypatch.setattr(sys, "stderr", Broken())
    assert cli.main(["send", "--base-url", server.base_url, "--state-file", str(path),
                     "--to", "otter", "--thread", "ops/hints", "--type", "request",
                     "--body", "synthetic"]) == 0
    assert len(invoked) == 1


#: A child that runs the real CLI with node.invoke faked to accept the send. argv: state file, type.
_CHILD_SEND = r'''
import sys
from tools.meshy_node import cli, node
node.invoke = lambda *a, **k: {"status": "completed", "result": {}, "result_signature_verified": True}
sys.exit(cli.main(["send", "--base-url", "http://127.0.0.1:9", "--state-file", sys.argv[1],
                   "--to", "otter", "--thread", "ops/hints", "--type", sys.argv[2], "--body", "synthetic"]))
'''


@pytest.mark.parametrize("msg_type", ["request", "note"])
def test_a_closed_stderr_pipe_leaves_an_accepted_send_at_exit_0(tmp_path, msg_type):
    """kxxx's Codex review of #88, reproduced on Windows: with stderr a pipe nobody reads, the hint's
    write fails and is caught, but its text stays buffered, the interpreter's flush at exit fails on
    it again, and an ACCEPTED send exits 120. A caller that retries on a non-zero exit then sends the
    message twice. Only a real process can see the exit-time flush, so this runs the CLI in a child
    whose stderr pipe has no reader.

    CONTROL: `note` prints no hint and exits 0 either way.
    MUTANT: replace the swap to the null device in cli._stderr_line with `pass`; `request` exits 120.
    Dropping only the explicit flush is an equivalent mutant here: the default stderr is
    line-buffered, so the newline already flushes inside `write`.
    """
    path = _state(tmp_path, "http://127.0.0.1:9")
    r, w = os.pipe()
    os.close(r)                                  # no reader: every write to the child's stderr fails
    try:
        proc = subprocess.run([sys.executable, "-c", _CHILD_SEND, str(path), msg_type], cwd=ROOT,
                              stderr=w, stdout=subprocess.PIPE, timeout=120,
                              env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    finally:
        os.close(w)
    assert proc.returncode == 0, (msg_type, proc.returncode)
    assert json.loads(proc.stdout)["status"] == "completed"


def test_relative_owed_since_uses_an_injected_clock_and_utc_z():
    """A relative cutoff is deterministic, UTC, and calculated before the request.

    MUTANT: add rather than subtract the duration; the exact cutoff is after the fixed clock.
    """
    fixed = datetime(2026, 10, 3, 12, 30, 45, tzinfo=timezone.utc)
    assert cli._owed_since("14d", now=lambda: fixed) == "2026-09-19T12:30:45Z"
    assert cli._owed_since("36h", now=lambda: fixed) == "2026-10-02T00:30:45Z"


def test_done_when_without_an_ack_flag_exits_2_and_never_sends(tmp_path, server, monkeypatch, capsys):
    """A completion condition without an ask is refused before node.send can reach a server.

    MUTANT: remove cli.py's `done_when` guard; the fake sender records an unintended write.
    """
    path = _state(tmp_path, server.base_url)
    sent = []
    monkeypatch.setattr(node, "send", lambda *args, **kwargs: sent.append((args, kwargs)))
    assert cli.main(["send", "--base-url", server.base_url, "--state-file", str(path),
                     "--to", "otter", "--thread", "ops/reply", "--body", "ack?",
                     "--done-when", "post receipt"]) == 2
    assert sent == [] and server.seen == []
    assert "--done-when requires" in capsys.readouterr().err


def test_expects_reply_alias_signs_the_existing_requires_ack_field(tmp_path, server, monkeypatch, capsys):
    """`--expects-reply` is one spelling of the established field, never a new wire field.

    MUTANT: give the alias a separate argparse destination; `requires_ack` is false here.
    MUTANT: omit `done_when` when node.send builds the value; the condition disappears.
    """
    path = _state(tmp_path, server.base_url)
    invoked = []

    def fake_invoke(*args, **kwargs):
        invoked.append(kwargs)
        return {"status": "completed", "result": {}, "result_signature_verified": True}

    monkeypatch.setattr(node, "invoke", fake_invoke)
    assert cli.main(["send", "--base-url", server.base_url, "--state-file", str(path),
                     "--to", "otter", "--thread", "ops/reply", "--body", "ack?",
                     "--expects-reply", "--done-when", "post receipt"]) == 0
    capsys.readouterr()
    (call,) = invoked
    value = call["args"]["value"]
    assert value["requires_ack"] is True and value["done_when"] == "post receipt"
    assert "expects_reply" not in value


def test_send_closes_is_repeatable_and_refuses_a_non_msg_key_before_sending(tmp_path, server, monkeypatch, capsys):
    """MUTANT: remove cli.py's closes guard; the fake invoke receives a bad signed send.

    MUTANT: omit closes while building wire.mesh_message; the two explicit settlements
    disappear from the value the node signs.
    """
    path = _state(tmp_path, server.base_url)
    invoked = []
    monkeypatch.setattr(node, "invoke", lambda *args, **kwargs: invoked.append(kwargs) or {
        "status": "completed", "result": {}, "result_signature_verified": True})
    assert cli.main(["send", "--base-url", server.base_url, "--state-file", str(path),
                     "--to", "otter", "--thread", "ops/close", "--body", "settled",
                     "--closes", "msg/1760000000000-a", "--closes", "msg/1760000000001-b"]) == 0
    capsys.readouterr()
    assert invoked[0]["args"]["value"]["closes"] == ["msg/1760000000000-a", "msg/1760000000001-b"]
    invoked.clear()
    assert cli.main(["send", "--base-url", server.base_url, "--state-file", str(path),
                     "--to", "otter", "--thread", "ops/close", "--body", "settled",
                     "--closes", "wiki/not-a-message"]) == 2
    assert invoked == [] and "--closes values" in capsys.readouterr().err


@pytest.mark.parametrize("keep_open, expected", [(False, ["msg/1790000000000-heron-question"]), (True, None)])
def test_an_answer_to_an_already_read_owed_question_signs_a_close_unless_kept_open(tmp_path, server, monkeypatch, capsys,
                                                                                     keep_open, expected):
    """PR3 step 1: the client decides before signing. The existing projection tests prove
    a signed closes edge is closed after a catch-up tick; this test proves the new client
    writes that edge only for an owed, verified question.

    MUTANT: bypass the owed-question check; --keep-open still closes. MUTANT: remove the
    append of the reply key; the normal answer is only answered. MUTANT: write closes after
    node.send; the signed value has no close.
    """
    key = "msg/1790000000000-heron-question"
    path = _state(tmp_path, server.base_url)
    invoked = []
    monkeypatch.setattr(node, "owed_key", lambda *args, **kwargs: {"owed": [
        {"hit": {"key": key, "version": 1}, "thread_id": "ops/close", "state": "open", "msg_type": "question"}]})
    monkeypatch.setattr(node, "ledger_records", lambda *args: [{"key": key, "version": 1, "at": "now"}])
    monkeypatch.setattr(node, "invoke", lambda *args, **kwargs: invoked.append(kwargs) or {
        "status": "completed", "result": {}, "result_signature_verified": True})
    flags = ["--keep-open"] if keep_open else []
    assert cli.main(["send", "--base-url", server.base_url, "--state-file", str(path),
                     "--to", "heron", "--thread", "ops/close", "--type", "answer",
                     "--in-reply-to", key, "--body", "settled", *flags]) == 0
    capsys.readouterr()
    value = invoked[0]["args"]["value"]
    assert value.get("closes") == expected


def test_an_answer_to_an_owed_request_does_not_sign_a_close(tmp_path, server, monkeypatch, capsys):
    """PR3 step 1 control: work requests remain for the requester to accept.

    MUTANT: accept request in the auto-close type check; this reply wrongly closes it.
    """
    key = "msg/1790000000003-heron-request"
    path = _state(tmp_path, server.base_url)
    invoked = []
    monkeypatch.setattr(node, "owed_key", lambda *args, **kwargs: {"owed": [
        {"hit": {"key": key, "version": 1}, "thread_id": "ops/request", "state": "open", "msg_type": "request"}]})
    monkeypatch.setattr(node, "ledger_records", lambda *args: [{"key": key, "version": 1, "at": "now"}])
    monkeypatch.setattr(node, "invoke", lambda *args, **kwargs: invoked.append(kwargs) or {
        "status": "completed", "result": {}, "result_signature_verified": True})
    assert cli.main(["send", "--base-url", server.base_url, "--state-file", str(path),
                     "--to", "heron", "--thread", "ops/request", "--type", "answer",
                     "--in-reply-to", key, "--body", "work is ready"]) == 0
    capsys.readouterr()
    assert "closes" not in invoked[0]["args"]["value"]


def test_an_ack_to_a_read_owed_question_does_not_sign_a_close(tmp_path, server, monkeypatch, capsys):
    key = "msg/1790000000000-heron-question"
    path = _state(tmp_path, server.base_url)
    invoked = []
    monkeypatch.setattr(node, "owed_key", lambda *args, **kwargs: {"owed": [
        {"hit": {"key": key, "version": 1}, "thread_id": "ops/close", "state": "open", "msg_type": "question"}]})
    monkeypatch.setattr(node, "ledger_records", lambda *args: [{"key": key, "version": 1, "at": "now"}])
    monkeypatch.setattr(node, "invoke", lambda *args, **kwargs: invoked.append(kwargs) or {
        "status": "completed", "result": {}, "result_signature_verified": True})
    assert cli.main(["send", "--base-url", server.base_url, "--state-file", str(path), "--to", "heron",
                     "--thread", "ops/close", "--type", "ack", "--in-reply-to", key, "--body", "seen"]) == 0
    assert "closes" not in invoked[0]["args"]["value"]
    capsys.readouterr()


def test_an_unread_owed_question_is_not_fetched_and_is_not_closed(tmp_path, server, monkeypatch, capsys):
    """The pre-send settlement decision consults only routing metadata and the verified read ledger.

    This is deliberately a normal send as well as the refusal control below: an unanswered ask remains
    unread until the operator explicitly reads it, then sends an answer that can carry its close.
    """
    key = "msg/1790000000000-heron-question"
    path = _state(tmp_path, server.base_url)
    invoked, reads = [], []
    monkeypatch.setattr(node, "owed_key", lambda *args, **kwargs: {"owed": [
        {"hit": {"key": key, "version": 2}, "thread_id": "ops/close", "state": "open", "msg_type": "question"}]})
    monkeypatch.setattr(node, "ledger_records", lambda *args: [])
    monkeypatch.setattr(node, "read", lambda *args, **kwargs: reads.append(kwargs))
    monkeypatch.setattr(node, "invoke", lambda *args, **kwargs: invoked.append(kwargs) or {
        "status": "completed", "result": {}, "result_signature_verified": True})
    assert cli.main(["send", "--base-url", server.base_url, "--state-file", str(path), "--to", "heron",
                     "--thread", "ops/close", "--type", "answer", "--in-reply-to", key,
                     "--body", "answer without a fetch"]) == 0
    captured = capsys.readouterr()
    assert "closes" not in invoked[0]["args"]["value"] and reads == []
    assert captured.err.strip() == (f"owed close: not signed for {key}: not read by this seat yet; read it, then answer; "
                                    f"send --type answer --closes {key} on its thread to settle it")


def test_a_refused_answer_does_not_fetch_an_unread_question(tmp_path, server, monkeypatch, capsys):
    key = "msg/1790000000000-heron-question"
    path = _state(tmp_path, server.base_url)
    reads = []
    monkeypatch.setattr(node, "owed_key", lambda *args, **kwargs: {"owed": [
        {"hit": {"key": key, "version": 1}, "thread_id": "ops/close", "state": "open", "msg_type": "question"}]})
    monkeypatch.setattr(node, "ledger_records", lambda *args: [])
    monkeypatch.setattr(node, "read", lambda *args, **kwargs: reads.append(kwargs))
    monkeypatch.setattr(cli.sendguard, "admit", lambda *args, **kwargs: (_ for _ in ()).throw(node.Refused("body too large")))
    assert cli.main(["send", "--base-url", server.base_url, "--state-file", str(path), "--to", "heron",
                     "--thread", "ops/close", "--type", "answer", "--in-reply-to", key,
                     "--body", "x" * 16385]) == 2
    assert reads == []
    assert "body too large" in capsys.readouterr().err


def test_an_owed_row_without_type_is_not_closed_and_explains_the_older_server(tmp_path, server, monkeypatch, capsys):
    key = "msg/1790000000000-heron-question"
    path = _state(tmp_path, server.base_url)
    invoked = []
    monkeypatch.setattr(node, "owed_key", lambda *args, **kwargs: {"owed": [
        {"hit": {"key": key, "version": 1}, "thread_id": "ops/close", "state": "open"}]})
    monkeypatch.setattr(node, "ledger_records", lambda *args: [{"key": key, "version": 1, "at": "now"}])
    monkeypatch.setattr(node, "invoke", lambda *args, **kwargs: invoked.append(kwargs) or {
        "status": "completed", "result": {}, "result_signature_verified": True})
    assert cli.main(["send", "--base-url", server.base_url, "--state-file", str(path), "--to", "heron",
                     "--thread", "ops/close", "--type", "answer", "--in-reply-to", key, "--body", "answer"]) == 0
    assert "closes" not in invoked[0]["args"]["value"]
    assert "the server does not say whether it is a question" in capsys.readouterr().err


def test_auto_close_finds_the_oldest_of_101_questions_in_its_thread(tmp_path, server, monkeypatch, capsys):
    oldest = "msg/1790000000000-heron-oldest"
    path = _state(tmp_path, server.base_url)
    asked, invoked = [], []
    rows = [{"hit": {"key": oldest, "version": 1}, "thread_id": "ops/long", "state": "open", "msg_type": "question"}]
    rows.extend({"hit": {"key": f"msg/1790000000{n:03d}-heron-new", "version": 1}, "thread_id": "ops/long",
                 "state": "open", "msg_type": "question"} for n in range(1, 101))

    def owed(*args, **kwargs):
        asked.append(kwargs)
        return {"owed": rows[:1]}

    monkeypatch.setattr(node, "owed_key", owed)
    monkeypatch.setattr(node, "ledger_records", lambda *args: [{"key": oldest, "version": 1, "at": "now"}])
    monkeypatch.setattr(node, "invoke", lambda *args, **kwargs: invoked.append(kwargs) or {
        "status": "completed", "result": {}, "result_signature_verified": True})
    assert cli.main(["send", "--base-url", server.base_url, "--state-file", str(path), "--to", "heron",
                     "--thread", "ops/long", "--type", "answer", "--in-reply-to", oldest, "--body", "settled"]) == 0
    assert invoked[0]["args"]["value"]["closes"] == [oldest]
    assert asked[0] == {"key": oldest}
    capsys.readouterr()


def test_auto_close_does_not_guess_when_the_single_key_is_not_owed(tmp_path, server, monkeypatch, capsys):
    key = "msg/1790000000000-heron-oldest"
    path = _state(tmp_path, server.base_url)
    invoked = []
    monkeypatch.setattr(node, "owed_key", lambda *args, **kwargs: {"owed": []})
    monkeypatch.setattr(node, "invoke", lambda *args, **kwargs: invoked.append(kwargs) or {
        "status": "completed", "result": {}, "result_signature_verified": True})
    assert cli.main(["send", "--base-url", server.base_url, "--state-file", str(path), "--to", "heron",
                     "--thread", "ops/long", "--type", "answer", "--in-reply-to", key, "--body", "settled"]) == 0
    assert "closes" not in invoked[0]["args"]["value"]
    assert "owed close:" not in capsys.readouterr().err


def test_an_answer_to_an_owed_question_in_another_thread_says_how_to_settle_it(tmp_path, server, monkeypatch, capsys):
    key = "msg/1790000000000-heron-question"
    path = _state(tmp_path, server.base_url)
    invoked = []

    def owed(*args, **kwargs):
        assert kwargs == {"key": key}
        return {"owed": [{"hit": {"key": key, "version": 1}, "thread_id": "ops/right", "state": "open",
                          "msg_type": "question"}], "truncated": False}

    monkeypatch.setattr(node, "owed_key", owed)
    monkeypatch.setattr(node, "invoke", lambda *args, **kwargs: invoked.append(kwargs) or {
        "status": "completed", "result": {}, "result_signature_verified": True})
    assert cli.main(["send", "--base-url", server.base_url, "--state-file", str(path), "--to", "heron",
                     "--thread", "ops/wrong", "--type", "answer", "--in-reply-to", key, "--body", "settled"]) == 0
    assert "closes" not in invoked[0]["args"]["value"]
    note = capsys.readouterr().err
    assert "ops/right" in note and f"--closes {key} on its thread" in note


def test_mentions_client_forwards_ref_and_optional_limit(tmp_path, server, capsys):
    """MUTANT: omit ref from node.mentions; the recording server receives an unusable read."""
    path = _state(tmp_path, server.base_url)
    assert cli.main(["mentions", "--base-url", server.base_url, "--state-file", str(path),
                     "--ref", "PR 73", "--limit", "7"]) == 0
    capsys.readouterr()
    (request,) = server.seen
    assert request["path"] == "/v1/mesh/mentions" and request["query"] == {"ref": "PR 73", "limit": "7"}


def test_mentions_by_peer_asks_for_the_holders_and_only_when_asked(tmp_path, server, capsys):
    """Divergence 090. MUTANT: drop `by` from node.mentions; the server answers hits, not holders. MUTANT: send
    by=peer always; a plain mentions read changes shape under every older caller."""
    path = _state(tmp_path, server.base_url)
    assert cli.main(["mentions", "--base-url", server.base_url, "--state-file", str(path),
                     "--ref", "hash:09cb6def", "--by-peer"]) == 0
    assert cli.main(["mentions", "--base-url", server.base_url, "--state-file", str(path),
                     "--ref", "hash:09cb6def"]) == 0
    capsys.readouterr()
    held, plain = server.seen
    assert held["query"] == {"ref": "hash:09cb6def", "by": "peer"}
    assert plain["query"] == {"ref": "hash:09cb6def"}


def test_brief_marks_truncated_sections_and_refuses_an_unechoed_filter(tmp_path, server, capsys):
    """On a server without /v1/mesh/brief (404) brief composes owed and awaiting and says so.
    MUTANT: remove brief's filters check; an old server is falsely called complete."""
    path = _state(tmp_path, server.base_url)
    server.routes = {"/v1/mesh/brief": (404, {"error": "E_NOT_FOUND"})}
    server.body = {**ANSWER, "truncated": True,
                   "filters": {"state": ["open", "active"], "since": "2026-09-19T12:30:45Z"}}
    assert cli.main(["brief", "--base-url", server.base_url, "--state-file", str(path),
                     "--since", "2026-09-19T12:30:45Z"]) == 0
    out = capsys.readouterr().out
    assert "composed from owed and awaiting" in out
    assert "reply owed: 1 (truncated)\n" in out, "the composed brief prints the route's split view (SD 255)"
    server.body = {**ANSWER, "truncated": False}
    assert cli.main(["brief", "--base-url", server.base_url, "--state-file", str(path),
                     "--since", "2026-09-19T12:30:45Z"]) == 1
    assert "brief incomplete: owed response did not echo requested filters" in capsys.readouterr().out


def test_owed_and_awaiting_help_list_closed_state():
    """MUTANT: omit `closed` from either help; the terminal contract hides a valid state."""
    parser = cli.build_parser()
    owed_help = next(action.help for action in parser._subparsers._group_actions[0].choices["owed"]._actions
                     if action.dest == "state")
    awaiting_help = next(action.help for action in parser._subparsers._group_actions[0].choices["awaiting"]._actions
                         if action.dest == "state")
    assert "closed" in owed_help and "closed" in awaiting_help


def test_any_answer_but_200_exits_1_with_the_body(tmp_path, server, capsys):
    server.status = 503
    server.body = {"error": "E_MESH_DISABLED", "message": "not available"}
    path = _state(tmp_path, server.base_url)
    assert _run(server, path) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["http_status"] == 503 and out["body"] == server.body


SINCE = "2026-09-19T12:30:45Z"
BRIEF = {
    "label": "memory-bxxxxx", "label_source": "roster", "labels": [{"label": "memory-bxxxxx", "source": "roster"}],
    "as_of": "2026-10-04T12:30:45+00:00",
    # the route echoes `since` in its own spelling, with an offset
    "filters": {"since": "2026-09-19T12:30:45+00:00", "days": None, "limit": 20, "help_after_h": 4},
    "owed": {"items": ANSWER["owed"], "counts": {"open": 2, "active": 1}, "truncated": False},
    "awaiting": {"items": [{"hit": {"key": "msg/1790000000001-mine", "recorded_at": "2026-10-04T10:00:00+00:00",
                                    "author": "memory-bxxxxx"},
                            "owing_label": "otter", "state": "open", "done_when": None}],
                 "counts": {"open": 1, "active": 0}, "truncated": False},
    "threads": {"items": [{"thread_id": "ops/lease", "new": 3, "last_recorded_at": "2026-10-04T11:00:00+00:00",
                           "last_author": "heron"}], "truncated": False},
    "help_wanted": {"items": [{"key": "msg/1790000000002-ask", "author": "uxxx", "thread_id": "ops/help",
                               "recorded_at": "2026-10-04T06:00:00+00:00", "done_when": None}], "truncated": True},
    "coverage": {"projected": 1, "total": 1},
}


def _brief(server, path, *args) -> int:
    return cli.main(["brief", "--base-url", server.base_url, "--state-file", str(path), "--since", SINCE, *args])


def test_brief_reads_the_route_in_one_call_and_prints_every_section(tmp_path, server, capsys):
    """Roadmap 8.3: the brief route plus one answered-only owed read, with the window and
    bounds checked. Since SD 255 the owed section prints as two, each "of" its own state's count: the row with no
    state stays a reply owed (never hidden), and the active count has no row in this page.
    MUTANT: drop the "of TOTAL" suffix; "reply owed: 1 of 2" is not printed. MUTANT: total both states (#373's
    count); "1 of 3" is printed."""
    path = _state(tmp_path, server.base_url)
    server.routes = {"/v1/mesh/brief": (200, BRIEF)}
    assert _brief(server, path) == 0
    out = capsys.readouterr().out
    assert [r["path"] for r in server.seen] == ["/v1/mesh/brief", "/v1/mesh/owed"]
    assert server.seen[0]["query"] == {"since": SINCE, "limit": "20", "help_after_h": "4"}
    assert "reply owed: 1 of 2\n" in out and "replied in thread, not linked: 0 of 1\n" in out
    assert "awaiting: 1" in out and "otter" in out
    assert "threads with news: 1" in out and "ops/lease  3 new, last heron" in out
    assert "help wanted: 1 (truncated)" in out and "msg/1790000000002-ask  uxxx" in out
    assert "composed" not in out


def test_brief_shows_reply_owed_and_answered_still_open_buckets(tmp_path, server, capsys):
    """PR3 step 2: one planted thread has open, active and answered asks. The brief keeps
    the server's reply-owed counts and adds the separately read answered, still-open bucket.

    MUTANT: do not read answered owed asks; the second bucket is empty. MUTANT: use the
    answered count for reply owed; the first bucket loses the server's two states.
    """
    path = _state(tmp_path, server.base_url)
    answered = {"hit": {"key": "msg/1790000000009-heron-answered", "author": "heron",
                         "recorded_at": "2026-10-04T10:00:00+00:00"},
                "thread_id": "ops/lease", "state": "answered"}
    server.routes = {
        "/v1/mesh/brief": (200, BRIEF),
        "/v1/mesh/owed": (200, {"owed": [answered], "truncated": False,
                                 "filters": {"state": ["answered"], "since": SINCE}}),
    }
    assert _brief(server, path, "--json") == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["owed"]["counts"] == {"open": 2, "active": 1}
    assert shown["answered_still_open"] == {"count": 1, "items": [answered], "truncated": False}
    assert [r["path"] for r in server.seen] == ["/v1/mesh/brief", "/v1/mesh/owed"]
    assert server.seen[-1]["query"] == {"state": "answered", "since": SINCE}

    assert _brief(server, path) == 0
    out = capsys.readouterr().out
    # SD 255: the text view gives the answered bucket as one count line; its rows stay in --json (above)
    assert "reply owed: 1 of 2\n" in out
    assert f"answered, not closed: 1  (`owed --state answered --since {SINCE} --limit 2000` lists them)" in out
    assert "msg/1790000000009-heron-answered" not in out


def test_brief_leaves_only_answered_still_open_unavailable_when_its_lookup_times_out(tmp_path, server, monkeypatch, capsys):
    path = _state(tmp_path, server.base_url)
    server.routes = {"/v1/mesh/brief": (200, BRIEF)}
    timeouts = []

    def stopped(fetcher, labels, timeout):
        timeouts.append(timeout)
        return None

    monkeypatch.setattr(cli.recipientwarn, "_within", stopped)
    assert _brief(server, path, "--json") == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["answered_still_open"] == {"count": None, "items": [], "truncated": False}
    assert timeouts == [cli.LEFT_OPEN_TIMEOUT_S]


def test_brief_sends_via_notice_only_when_the_caller_set_the_variable(tmp_path, server, capsys, monkeypatch):
    """A brief read for a notice says so (divergence 072: brief_notice), and the echo check is unchanged, since
    the route echoes the window and bounds and not `via`. CONTROL: unset, the query is exactly the seat's.
    MUTANT: leave `**_via()` out of node.brief; the notice case sends no via."""
    path = _state(tmp_path, server.base_url)
    server.routes = {"/v1/mesh/brief": (200, BRIEF)}
    monkeypatch.setenv(node.VIA_ENV, "notice")
    assert _brief(server, path) == 0
    assert server.seen[-2]["query"] == {"since": SINCE, "limit": "20", "help_after_h": "4", "via": "notice"}
    monkeypatch.delenv(node.VIA_ENV)
    assert _brief(server, path) == 0
    assert server.seen[-2]["query"] == {"since": SINCE, "limit": "20", "help_after_h": "4"}
    capsys.readouterr()


@pytest.mark.parametrize("filters", [
    {"since": "2026-09-19T12:30:45+00:00", "days": None, "limit": 10, "help_after_h": 4},      # another limit
    {"since": "2026-09-19T12:30:45+00:00", "days": None, "limit": 20, "help_after_h": 8},      # another help window
    {"since": "2026-09-20T12:30:45+00:00", "days": None, "limit": 20, "help_after_h": 4},      # another window
    {"since": "2026-09-19T12:30:45+00:00", "days": 14, "limit": 20, "help_after_h": 4},        # a days window
    {"since": "2026-09-19T12:30:45+00:00", "limit": 20, "help_after_h": 4},                    # no days at all
    None,                                                                                      # no echo at all
])
def test_brief_refuses_an_answer_that_does_not_echo_its_window_and_bounds(tmp_path, server, capsys, filters):
    """MUTANT: compare only the bounds; another window passes. MUTANT: drop the days test; a days window
    passes. MUTANT: ignore help_after_h; another help window passes. MUTANT: read days with get; an echo
    without days passes (codex on #99)."""
    path = _state(tmp_path, server.base_url)
    body = {k: v for k, v in BRIEF.items() if k != "filters"}
    if filters is not None:
        body["filters"] = filters
    server.routes = {"/v1/mesh/brief": (200, body)}
    assert _brief(server, path) == 1
    out = capsys.readouterr().out
    assert "brief incomplete: the brief response did not echo the requested window and bounds" in out
    assert "owed:" not in out


def test_brief_json_is_the_route_answer_or_the_composed_one(tmp_path, server, capsys):
    """--json is for a program (meshy-mod's /brief): one JSON object on stdout, the route's answer as it came, or
    the composed one marked "composed", whose one-line notice goes to stderr so stdout still parses.
    MUTANT: print the composed notice to stdout under --json; stdout no longer parses.
    MUTANT: ignore --json on the route; stdout is the text form."""
    path = _state(tmp_path, server.base_url)
    server.routes = {"/v1/mesh/brief": (200, BRIEF)}
    assert _brief(server, path, "--json") == 0
    shown = json.loads(capsys.readouterr().out)
    assert {key: value for key, value in shown.items() if key != "answered_still_open"} == BRIEF
    assert shown["answered_still_open"]["count"] is None
    server.routes = {"/v1/mesh/brief": (404, {"error": "E_NOT_FOUND"})}
    server.body = {**ANSWER, "filters": {"state": ["open", "active"], "since": SINCE}}
    assert _brief(server, path, "--json") == 0
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert out["composed"] is True and out["owed"]["owed"] == ANSWER["owed"], out
    assert "composed from owed and awaiting" in captured.err
    server.routes = {"/v1/mesh/brief": (200, {**BRIEF, "filters": None})}
    assert _brief(server, path, "--json") == 1
    assert json.loads(capsys.readouterr().out)["error"].startswith("brief incomplete")


def test_brief_says_how_many_rows_of_each_section_it_does_not_print(tmp_path, server, capsys):
    """Ten rows a section; the route sends up to 20, so the rest is counted, for threads and help wanted
    as for owed and awaiting (codex on #99). MUTANT: drop the count after threads; "and 1 more" is missing."""
    path = _state(tmp_path, server.base_url)
    threads = [{"thread_id": f"ops/t{n}", "new": 1, "last_recorded_at": "2026-10-04T11:00:00+00:00",
                "last_author": "heron"} for n in range(11)]
    wanted = [{"key": f"msg/179000000{n:04d}-ask", "author": "uxxx", "thread_id": "ops/help",
               "recorded_at": "2026-10-04T06:00:00+00:00", "done_when": None} for n in range(12)]
    server.routes = {"/v1/mesh/brief": (200, {**BRIEF, "threads": {"items": threads, "truncated": False},
                                               "help_wanted": {"items": wanted, "truncated": False}})}
    assert _brief(server, path) == 0
    out = capsys.readouterr().out
    head, _, rest = out.partition("help wanted:")
    assert "threads with news: 11" in head and "ops/t9 " in head and "ops/t10" not in head
    assert head.rstrip().endswith("and 1 more")
    assert "msg/1790000000009-ask" in rest and "msg/1790000000010-ask" not in rest
    assert rest.rstrip().endswith("and 2 more")


def test_brief_json_reports_an_unreachable_server_as_one_json_object(tmp_path, capsys):
    """Codex on #99: with --json a refused connection printed nothing on stdout and left through main's stderr
    handler with exit 2. It is one JSON object now; the text form keeps main's report.
    MUTANT: re-raise OSError under --json too."""
    import socket
    with socket.socket() as s:          # a port that was free a moment ago: nothing listens on it now
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    path = _state(tmp_path, base)
    rc = cli.main(["brief", "--base-url", base, "--state-file", str(path), "--since", SINCE, "--json"])
    captured = capsys.readouterr()
    assert rc == 1 and json.loads(captured.out)["error"].startswith("brief failed: unreachable"), captured
    rc = cli.main(["brief", "--base-url", base, "--state-file", str(path), "--since", SINCE])
    captured = capsys.readouterr()
    assert rc == 2 and captured.out == "" and "unreachable" in captured.err


def test_brief_falls_back_only_when_the_route_is_absent(tmp_path, server, capsys):
    """A 404 is an older server; any other failure is a failure, not a reason to compose.
    MUTANT: fall back on every ServerSaid; the 503 composes a brief from owed and awaiting."""
    path = _state(tmp_path, server.base_url)
    server.routes = {"/v1/mesh/brief": (503, {"error": "E_MESH_DISABLED"})}
    assert _brief(server, path) == 1
    assert "brief failed: HTTP 503" in capsys.readouterr().out
    assert [r["path"] for r in server.seen] == ["/v1/mesh/brief"]



def _owed_row(key, author, thread, state="active"):
    return {"hit": {"key": key, "version": 1, "edge_type": "owes_ack", "dst": "me", "src_field": "to:array",
                    "recorded_at": "2026-10-07T10:00:00+00:00", "author": author},
            "thread_id": thread, "answered_in_thread": True, "answered_directly": False, "state": state}


ASKED = "msg/1790000000001-heron-ask001"     # the ask an answer replies to
SIBLING = "msg/1790000000002-heron-ask002"   # another ask from the same peer, same thread
OWED_SIX = {"owed": [
    _owed_row(ASKED, "otter", "ops/hints"),
    _owed_row(SIBLING, "otter", "ops/hints"),
    _owed_row("msg/1790000000003-heron-ask003", "otter", "ops/other"),   # another thread
    _owed_row("msg/1790000000004-heron-ask004", "heron", "ops/hints"),   # another sender
    _owed_row("msg/1790000000005-heron-ask005", "otter", "ops/hints", state="answered"),
]}


def _left_open(tmp_path, server, monkeypatch, capsys, owed_answer, *flags):
    """An accepted answer with node.owed faked; returns (stderr JSON lines, the owed calls made)."""
    calls = []

    def fake_owed(path, state, **kwargs):
        calls.append(kwargs)
        if isinstance(owed_answer, BaseException):
            raise owed_answer
        return owed_answer

    monkeypatch.setattr(node, "owed", fake_owed)
    path = _state(tmp_path, server.base_url)
    monkeypatch.setattr(node, "invoke", lambda *a, **k: {"status": "completed", "result": {},
                                                         "result_signature_verified": True})
    argv = ["send", "--base-url", server.base_url, "--state-file", str(path), "--thread", "ops/hints",
            "--to", "otter", "--body", "synthetic", "--keep-open", *flags]
    assert cli.main(argv) == 0
    captured = capsys.readouterr()
    return [json.loads(line) for line in captured.err.splitlines() if line.strip()], calls


def test_an_answer_names_the_other_asks_it_leaves_owed_in_its_thread(tmp_path, server, monkeypatch, capsys):
    """Measured 2026-10-07: six asks from one peer stayed in a seat's `owed` for up to 17 hours, every
    one handled, because each reply linked one ask while answering several. The hint names only the
    asks from a `to` recipient, in the answer's thread, still open or active, other than the linked one.

    MUTANT: drop the thread test in cli._asks_left_open; the other thread's ask is named too.
    MUTANT: drop the author test; heron's ask is named too.
    MUTANT: drop the state test; the answered ask is named too.
    MUTANT: drop `not in linked`; the replied-to ask is named as left open.
    MUTANT: delete the call site in cmd send; no hint.
    """
    lines, calls = _left_open(tmp_path, server, monkeypatch, capsys, OWED_SIX,
                              "--type", "answer", "--in-reply-to", ASKED)
    (hint,) = [l for l in lines if l.get("hint") == "asks_left_open"]
    assert hint["asks"] == [{"key": SIBLING, "author": "otter"}] and hint["more"] == 0, hint
    assert hint["thread"] == "ops/hints" and "--closes <key>" in hint["note"], hint
    (call,) = calls
    assert call["states"] == "open,active" and call["since"], call


def test_an_answer_that_closes_the_rest_gets_no_hint(tmp_path, server, monkeypatch, capsys):
    """CONTROL for the test above: the same answer closing the sibling leaves nothing to name."""
    lines, _ = _left_open(tmp_path, server, monkeypatch, capsys, OWED_SIX,
                          "--type", "answer", "--in-reply-to", ASKED, "--closes", SIBLING)
    assert [l for l in lines if l.get("hint") == "asks_left_open"] == []


@pytest.mark.parametrize("owed_answer", [node.FiltersNotApplied({"error": "x"}), OSError("down"), {"owed": "x"}, None])
def test_a_lookup_that_fails_is_no_hint_and_the_send_still_exits_0(tmp_path, server, monkeypatch, capsys,
                                                                    owed_answer):
    """The message is stored before the lookup runs: a failed, unechoed or malformed answer is no hint,
    and the exit code stays 0, since a caller that retries a non-zero exit would send the answer twice."""
    lines, _ = _left_open(tmp_path, server, monkeypatch, capsys, owed_answer,
                          "--type", "answer", "--in-reply-to", ASKED)
    assert [l for l in lines if l.get("hint") == "asks_left_open"] == []


@pytest.mark.parametrize("flags", [("--type", "note"), ("--type", "request", "--expects-reply"),
                                   ("--type", "request", "--in-reply-to", ASKED), ("--type", "answer")])
def test_only_a_linked_answer_looks_anything_up(tmp_path, server, monkeypatch, capsys, flags):
    """A note, an ask (even one that replies to something), or an answer that links nothing (which
    `answer_not_linked` already covers) costs no lookup.

    MUTANT: drop the ANSWER_TYPES test at the call site; the note and the ask look up `owed`.
    MUTANT: drop the `in_reply_to or closes` test; the unlinked answer looks up `owed`.
    """
    _, calls = _left_open(tmp_path, server, monkeypatch, capsys, OWED_SIX, *flags)
    assert calls == []


def test_rows_without_a_state_repeated_or_malformed_are_never_named(tmp_path, server, monkeypatch, capsys):
    """Codex on this hint: a missing state must not read as open, a repeated row names its key once,
    and a row whose key is not a msg/ key is skipped.

    MUTANT: default a missing state to "open"; the stateless row is named.
    MUTANT: drop the `seen` test; the sibling is named twice.
    """
    stateless = _owed_row("msg/1790000000006-heron-ask006", "otter", "ops/hints")
    del stateless["state"]
    owed = {"owed": [_owed_row(SIBLING, "otter", "ops/hints"), _owed_row(SIBLING, "otter", "ops/hints"),
                     stateless, _owed_row("not-a-key", "otter", "ops/hints"), "junk", {"hit": "junk"}]}
    lines, _ = _left_open(tmp_path, server, monkeypatch, capsys, owed, "--type", "answer", "--in-reply-to", ASKED)
    (hint,) = [l for l in lines if l.get("hint") == "asks_left_open"]
    assert hint["asks"] == [{"key": SIBLING, "author": "otter"}], hint


def test_several_recipients_are_named_per_ask_and_a_long_list_is_capped(tmp_path, server, monkeypatch, capsys):
    """Each named ask carries its author, so an answer to two seats says whose each one is, and the
    hint stops at cli.LEFT_OPEN_MAX with the rest counted.

    MUTANT: drop the author from each named ask; the authors are lost.
    MUTANT: drop the cap; all twelve are named and `more` is 0.
    """
    rows = [_owed_row("msg/17900000001%02d-heron-ask1%02d" % (i, i), "otter" if i % 2 else "heron", "ops/hints")
            for i in range(12)]
    lines, _ = _left_open(tmp_path, server, monkeypatch, capsys, {"owed": rows},
                          "--type", "answer", "--in-reply-to", ASKED, "--to", "heron")
    (hint,) = [l for l in lines if l.get("hint") == "asks_left_open"]
    assert len(hint["asks"]) == cli.LEFT_OPEN_MAX and hint["more"] == 2, hint
    assert {a["author"] for a in hint["asks"]} == {"otter", "heron"}, hint


def test_an_old_ask_of_this_thread_behind_a_long_backlog_is_still_named(tmp_path, server, monkeypatch, capsys):
    """kxxx on #152: without a limit the route answers its default 100 asks across all threads, newest first, and
    the thread filter runs after it, so a seat with a backlog lost this thread's older asks. The lookup asks for
    cli.LEFT_OPEN_LIMIT rows; here the sibling sits behind 150 newer asks from other threads. A list the server
    cut says so with `truncated`.

    MUTANT: ask for limit=None again; the fake below answers like the route's default and cuts at 100.
    MUTANT: drop the `truncated` copy; the second assert finds no flag.
    """
    newer = [_owed_row("msg/17900001%05d-heron-new%03d" % (i, i), "otter", "ops/elsewhere") for i in range(150)]
    rows = newer + [_owed_row(SIBLING, "otter", "ops/hints")]

    def route_like(path, state, *, limit, **kwargs):  # the route's own default when no limit is sent
        cut = 100 if limit is None else limit
        return {"owed": rows[:cut], "truncated": len(rows) > cut}

    monkeypatch.setattr(node, "owed", route_like)
    path = _state(tmp_path, server.base_url)
    monkeypatch.setattr(node, "invoke", lambda *a, **k: {"status": "completed", "result": {},
                                                         "result_signature_verified": True})
    argv = ["send", "--base-url", server.base_url, "--state-file", str(path), "--thread", "ops/hints",
            "--to", "otter", "--body", "synthetic", "--type", "answer", "--keep-open", "--in-reply-to", ASKED]
    assert cli.main(argv) == 0
    lines = [json.loads(l) for l in capsys.readouterr().err.splitlines() if l.strip()]
    (hint,) = [l for l in lines if l.get("hint") == "asks_left_open"]
    assert hint["asks"] == [{"key": SIBLING, "author": "otter"}] and "truncated" not in hint, hint
    monkeypatch.setattr(node, "owed", lambda *a, **k: {"owed": [_owed_row(SIBLING, "otter", "ops/hints")],
                                                       "truncated": True})
    assert cli.main(argv) == 0
    lines = [json.loads(l) for l in capsys.readouterr().err.splitlines() if l.strip()]
    (hint,) = [l for l in lines if l.get("hint") == "asks_left_open"]
    assert hint["truncated"] is True, hint


def test_a_cut_list_with_no_match_here_still_says_it_was_cut(tmp_path, server, monkeypatch, capsys):
    """kxxx on #152 (a Codex finding he confirmed): with LEFT_OPEN_LIMIT newer asks elsewhere, this thread's ask sits
    at the recorded rule, the server cuts the list before it, nothing returned matches, and the hint went quiet. A cut list
    with no match now gives a hint with no asks, `truncated`, and a note that the list was not seen whole.

    CONTROL: the same answer not cut gives no hint at all (nothing is owed here).
    MUTANT: drop the `not asks and truncated` branch; the first assert finds no hint.
    MUTANT: emit it whether or not the list was cut; the control finds a hint.
    """
    elsewhere = [_owed_row("msg/179000020%04d-heron-far%03d" % (i, i), "otter", "ops/elsewhere")
                 for i in range(cli.LEFT_OPEN_LIMIT)]
    rows = elsewhere + [_owed_row(SIBLING, "otter", "ops/hints")]

    def route_like(path, state, *, limit, **kwargs):
        cut = 100 if limit is None else limit
        return {"owed": rows[:cut], "truncated": len(rows) > cut}

    monkeypatch.setattr(node, "owed", route_like)
    monkeypatch.setattr(node, "invoke", lambda *a, **k: {"status": "completed", "result": {},
                                                         "result_signature_verified": True})
    path = _state(tmp_path, server.base_url)
    argv = ["send", "--base-url", server.base_url, "--state-file", str(path), "--thread", "ops/hints",
            "--to", "otter", "--body", "synthetic", "--type", "answer", "--keep-open", "--in-reply-to", ASKED]
    assert cli.main(argv) == 0
    lines = [json.loads(l) for l in capsys.readouterr().err.splitlines() if l.strip()]
    (hint,) = [l for l in lines if l.get("hint") == "asks_left_open"]
    assert hint["asks"] == [] and hint["truncated"] is True and "could not see the whole list" in hint["note"], hint
    monkeypatch.setattr(node, "owed", lambda *a, **k: {"owed": elsewhere[:10], "truncated": False})
    assert cli.main(argv) == 0
    lines = [json.loads(l) for l in capsys.readouterr().err.splitlines() if l.strip()]
    assert [l for l in lines if l.get("hint") == "asks_left_open"] == []


def _owed_key_rows(rows_by_state, asked):
    """Model the single-key route, including answered and closed rows."""
    def owed(*args, **kwargs):
        asked.append(kwargs["key"])
        return {"owed": [row for row in rows_by_state() if row["hit"]["key"] == kwargs["key"]]}
    return owed


def _send(server, path, msg_type, key, body, *extra):
    return cli.main(["send", "--base-url", server.base_url, "--state-file", str(path), "--to", "heron",
                     "--thread", "ops/close", "--type", msg_type, "--in-reply-to", key, "--body", body, *extra])


def test_an_answer_after_an_ack_closes_the_question(tmp_path, server, monkeypatch, capsys):
    """pxx 7762d8 / GitHub Codex r4359878147: any linked reply (an ack included) makes the ask "answered",
    and the later real answer must still close it."""
    key = "msg/1790000000000-heron-question"
    path = _state(tmp_path, server.base_url)
    state = {"value": "open"}
    invoked, asked = [], []
    monkeypatch.setattr(node, "owed_key", _owed_key_rows(lambda: [
        {"hit": {"key": key, "version": 1}, "thread_id": "ops/close", "state": state["value"],
         "msg_type": "question"}], asked))
    monkeypatch.setattr(node, "ledger_records", lambda *args: [{"key": key, "version": 1, "at": "now"}])
    monkeypatch.setattr(node, "invoke", lambda *args, **kwargs: invoked.append(kwargs) or {
        "status": "completed", "result": {}, "result_signature_verified": True})
    assert _send(server, path, "ack", key, "seen, working on it") == 0
    assert "closes" not in invoked[-1]["args"]["value"]
    state["value"] = "answered"                                 # the server's view after the ack
    assert _send(server, path, "answer", key, "here is the answer") == 0
    assert invoked[-1]["args"]["value"]["closes"] == [key]
    capsys.readouterr()


def test_a_second_answer_after_keep_open_closes_the_question(tmp_path, server, monkeypatch, capsys):
    key = "msg/1790000000000-heron-question"
    path = _state(tmp_path, server.base_url)
    state = {"value": "open"}
    invoked = []
    monkeypatch.setattr(node, "owed_key", _owed_key_rows(lambda: [
        {"hit": {"key": key, "version": 1}, "thread_id": "ops/close", "state": state["value"],
         "msg_type": "question"}], []))
    monkeypatch.setattr(node, "ledger_records", lambda *args: [{"key": key, "version": 1, "at": "now"}])
    monkeypatch.setattr(node, "invoke", lambda *args, **kwargs: invoked.append(kwargs) or {
        "status": "completed", "result": {}, "result_signature_verified": True})
    assert _send(server, path, "answer", key, "part one", "--keep-open") == 0
    assert "closes" not in invoked[-1]["args"]["value"]
    state["value"] = "answered"
    assert _send(server, path, "answer", key, "part two") == 0
    assert invoked[-1]["args"]["value"]["closes"] == [key]
    capsys.readouterr()


def test_an_ack_to_an_answered_question_still_does_not_close(tmp_path, server, monkeypatch, capsys):
    key = "msg/1790000000000-heron-question"
    path = _state(tmp_path, server.base_url)
    invoked = []
    monkeypatch.setattr(node, "owed_key", _owed_key_rows(lambda: [
        {"hit": {"key": key, "version": 1}, "thread_id": "ops/close", "state": "answered",
         "msg_type": "question"}], []))
    monkeypatch.setattr(node, "ledger_records", lambda *args: [{"key": key, "version": 1, "at": "now"}])
    monkeypatch.setattr(node, "invoke", lambda *args, **kwargs: invoked.append(kwargs) or {
        "status": "completed", "result": {}, "result_signature_verified": True})
    assert _send(server, path, "ack", key, "still looking") == 0
    assert "closes" not in invoked[-1]["args"]["value"]
    capsys.readouterr()


def test_a_closed_question_is_never_closed_again(tmp_path, server, monkeypatch, capsys):
    """Control: widening the states to answered must not reach a row that is already closed."""
    key = "msg/1790000000000-heron-question"
    path = _state(tmp_path, server.base_url)
    invoked = []
    monkeypatch.setattr(node, "owed_key", lambda *args, **kwargs: {"owed": [
        {"hit": {"key": key, "version": 1}, "thread_id": "ops/close", "state": "closed", "msg_type": "question"}]})
    monkeypatch.setattr(node, "ledger_records", lambda *args: [{"key": key, "version": 1, "at": "now"}])
    monkeypatch.setattr(node, "invoke", lambda *args, **kwargs: invoked.append(kwargs) or {
        "status": "completed", "result": {}, "result_signature_verified": True})
    assert _send(server, path, "answer", key, "late answer") == 0
    assert "closes" not in invoked[-1]["args"]["value"]
    capsys.readouterr()


# ---- SD 255 (round 1 item 15, 7 Nov 2026): a bare `owed` lists the open asks; an answer that expects a reply hints ---

def _as_owed_acks_answers(query: str, body: dict) -> tuple[int, dict]:
    """The owed route as apps/mesh/graph.owed_acks answers it: the state filter applied in the query BEFORE the limit
    (the route's default 100), the rows newest first as `body` lists them, and `truncated` when the limit cut them.
    The echo is the view's own (_as_the_owed_view_answers)."""
    status, echoed = _as_the_owed_view_answers(query, body)
    if status != 200:
        return status, echoed
    q = dict(urllib.parse.parse_qsl(query, keep_blank_values=True))
    states = q["state"].split(",") if "state" in q else ["open", "active", "answered"]
    limit = int(q.get("limit", 100))
    rows = [r for r in body["owed"] if r.get("state") in states]
    return 200, {**echoed, "owed": rows[:limit], "truncated": len(rows) > limit}


def _row(n: int, state: str, thread: str = "ops/busy") -> dict:
    """One owed ask; a larger n is newer."""
    return {"hit": {"key": f"msg/17900000{n:05d}-heron-{state}", "author": "heron",
                    "recorded_at": f"2026-10-{1 + n // 24:02d}T{n % 24:02d}:00:00+00:00"},
            "state": state, "thread_id": thread, "msg_type": "question"}


def test_a_bare_owed_lists_an_open_ask_that_answered_rows_would_push_past_the_limit(tmp_path, server, capsys):
    """The bug itself (pxx 515e7a C). On 7 Nov pxx's own bare `owed` came back as 100 answered rows with truncated
    true, so an older open ask was past the limit. Here three answered asks are newer than one open ask, all to this
    seat. CONTROL, the base's bare call (asked for by name now): state=open,active,answered with --limit 3 cuts the open
    ask. Since SD 255 the bare call asks for state=open, which the route filters before LIMIT, so it is listed.
    MUTANT: send OWED_DEFAULT_STATE from a bare `owed`; the open ask is cut, as at the base."""
    path = _state(tmp_path, server.base_url)
    old_open = _row(1, "open")
    server.body = {**ANSWER, "owed": [_row(30, "answered"), _row(20, "answered"), _row(10, "answered"), old_open]}
    server.echo = _as_owed_acks_answers
    assert _run(server, path, "--state", "open,active,answered", "--limit", "3", "--since", "all") == 0
    base = json.loads(capsys.readouterr().out)
    assert base["truncated"] is True and old_open["hit"]["key"] not in json.dumps(base), "the control shows the bug"
    assert _run(server, path, "--limit", "3", "--since", "all") == 0
    shown = json.loads(capsys.readouterr().out)
    assert [r["hit"]["key"] for r in shown["owed"]] == [old_open["hit"]["key"]] and shown["truncated"] is False
    assert shown["buckets"]["reply_owed"]["count"] == 1
    assert shown["buckets"]["answered_not_closed"] == {
        "count": 3, "truncated": False, "list": "owed --state answered --since all --limit 2000"}
    assert shown["summary"] == "open 1, replied in thread not linked 0, answered not closed 3"
    assert "-answered" not in json.dumps(shown["owed"]) + json.dumps(shown["buckets"]), "no answered row is printed"


def test_a_bare_owed_shows_every_active_key_that_fits_the_page_and_else_the_oldest(tmp_path, server, capsys):
    """pxx 515e7a A: an active ask is the risky class (one status post in a busy thread turns every unanswered ask
    there active), so its keys are not cut at 10 while they fit in the page (--limit, else the route's 100). Past
    that the OLDEST OWED_ACTIVE_OLDEST_SHOWN show, with the exact command for the rest. None is an owed row.
    MUTANT: cap at 10 always; 12 that fit the default page are cut. MUTANT: show the newest; the oldest key is
    missing. MUTANT: leave out `list`; a capped list gives no way to the rest."""
    path = _state(tmp_path, server.base_url)
    active = [_row(n, "active") for n in range(40, 28, -1)]          # 12, newest first as the route answers
    server.body = {**ANSWER, "owed": [_row(50, "open")] + active}
    server.echo = _as_owed_acks_answers
    assert _run(server, path, "--since", "all") == 0
    shown = json.loads(capsys.readouterr().out)
    summary = shown["buckets"]["replied_in_thread_not_linked"]
    assert summary["count"] == 12
    assert [r["key"] for r in summary["oldest_first"]] == [r["hit"]["key"] for r in active[::-1]]
    assert "more" not in summary and "list" not in summary
    assert [r["state"] for r in shown["owed"]] == ["open"]
    assert shown["summary"] == "open 1, replied in thread not linked 12, answered not closed 0"
    assert _run(server, path, "--limit", "5", "--since", "all") == 0
    capped = json.loads(capsys.readouterr().out)["buckets"]["replied_in_thread_not_linked"]
    assert ([r["key"] for r in capped["oldest_first"]]
            == [r["hit"]["key"] for r in active[::-1][:cli.OWED_ACTIVE_OLDEST_SHOWN]])
    assert capped["more"] == 12 - cli.OWED_ACTIVE_OLDEST_SHOWN
    assert capped["list"] == "owed --state active --since all --limit 2000" and "cut" not in capped


def test_a_cut_active_list_says_its_oldest_are_the_oldest_of_the_newest(tmp_path, server, capsys, monkeypatch):
    """pxx 863dfd: the route orders newest first before LIMIT, so when the active query is cut the client holds the
    NEWEST rows, and their oldest is not the oldest of all. It says so, and gives the command for more.
    MUTANT: drop the `cut` line; a cut list reads as complete. MUTANT: count it without the +; likewise."""
    path = _state(tmp_path, server.base_url)
    active = [_row(n, "active") for n in range(40, 28, -1)]
    server.body = {**ANSWER, "owed": active}
    server.echo = _as_owed_acks_answers
    monkeypatch.setattr(cli, "OWED_ADDON_LIMIT", 4)
    assert _run(server, path, "--since", "all") == 0
    shown = json.loads(capsys.readouterr().out)
    summary = shown["buckets"]["replied_in_thread_not_linked"]
    assert summary["truncated"] is True and [r["key"] for r in summary["oldest_first"]] == [
        r["hit"]["key"] for r in active[:4][::-1]], "the four newest, oldest of them first"
    assert "oldest of the newest 4, not of all" in summary["cut"] and summary["list"] in summary["cut"]
    assert shown["summary"] == "open 0, replied in thread not linked 4+, answered not closed 0"


def test_a_bare_owed_add_on_that_fails_is_unavailable_never_zero(tmp_path, server, capsys, monkeypatch):
    """An add-on that errs, or does not answer in OWED_ADDON_TIMEOUT_S, says so. The open list still prints, and the
    command exits 0. MUTANT: return an empty bucket from _owed_addon on a failure; the summary reads 0."""
    path = _state(tmp_path, server.base_url)
    server.body = {**ANSWER, "owed": [_row(5, "open"), _row(4, "active"), _row(3, "answered")]}

    def active_fails(query, body):
        if "state=active" in query:
            return 503, {"error": "E_BUSY"}
        return _as_owed_acks_answers(query, body)

    server.echo = active_fails
    assert _run(server, path, "--since", "all") == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["buckets"]["replied_in_thread_not_linked"] == {"count": None, "unavailable": "HTTP 503"}
    assert shown["buckets"]["answered_not_closed"]["count"] == 1
    assert shown["summary"] == "open 1, replied in thread not linked ?, answered not closed 1"
    timeouts = []
    monkeypatch.setattr(cli.recipientwarn, "_within", lambda fetcher, labels, t: timeouts.append(t))
    assert _run(server, path, "--since", "all") == 0
    shown = json.loads(capsys.readouterr().out)
    assert [r["hit"]["key"] for r in shown["owed"]] == [_row(5, "open")["hit"]["key"]]
    assert shown["summary"] == "open 1, replied in thread not linked ?, answered not closed ?"
    assert shown["buckets"]["answered_not_closed"] == {
        "count": None, "unavailable": f"no answer within the {cli.OWED_ADDON_TIMEOUT_S:g} s budget"}
    # ONE budget for both (pxx 863dfd): the second query gets what the first left, never a fresh 2 s
    assert len(timeouts) == 2 and 0 < timeouts[1] <= timeouts[0] <= cli.OWED_ADDON_TIMEOUT_S, timeouts


def test_the_bare_owed_add_ons_share_one_budget_and_the_one_it_leaves_no_time_for_is_unavailable(
        tmp_path, server, capsys, monkeypatch):
    """The two add-ons run one after another (two at once could both refresh the token in the state file) under one
    OWED_ADDON_TIMEOUT_S. A clock stand-in makes the first take the whole budget, without sleeping.
    MUTANT: give each add-on its own fresh budget; the answered one runs and reads 1."""
    import time as real_time
    path = _state(tmp_path, server.base_url)
    server.body = {**ANSWER, "owed": [_row(5, "open"), _row(4, "active"), _row(3, "answered")]}
    server.echo = _as_owed_acks_answers
    ticks = [0.0, 0.0]                  # the budget's start and the active query's turn; every later read is 5 s on

    class _Clock:
        def monotonic(self):
            return ticks.pop(0) if ticks else 5.0

        def __getattr__(self, name):
            return getattr(real_time, name)

    monkeypatch.setattr(cli, "time", _Clock())
    assert _run(server, path, "--since", "all") == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["buckets"]["replied_in_thread_not_linked"]["count"] == 1
    assert shown["buckets"]["answered_not_closed"] == {
        "count": None, "unavailable": f"not started: the {cli.OWED_ADDON_TIMEOUT_S:g} s budget was spent"}
    assert [r["query"].get("state") for r in server.seen] == ["open", "active"], "the answered query never ran"


def test_counts_only_counts_a_row_without_a_state_as_open_like_the_bare_view(tmp_path, server, capsys):
    """pxx 5e4563 INFO: --counts-only's `open` follows the bare view's rule, so a row with no state field is open
    (never hidden), and the two reconcile. MUTANT: count only state == "open"; `open` reads 0."""
    path = _state(tmp_path, server.base_url)
    server.body = {"owed": [{"hit": {"key": "msg/1790000000200-heron-nostate", "author": "heron"}}],
                   "filters": {"state": ["open", "active", "answered"]}}
    assert _run(server, path, "--since", "all", "--counts-only") == 0
    measured = json.loads(capsys.readouterr().out)
    assert measured["open"] == 1 and measured["replied_in_thread_not_linked"] == 0


def test_a_listed_command_quotes_its_values_and_parses_back_with_the_real_parser(tmp_path, server, capsys):
    """pxx 5e4563 P2-1 (codex-pxx-cli 226efb): _owed_since passes a space-separated ISO time through unchanged, so an
    unquoted --since in the printed command was two arguments and the copy exited 2. The command is parsed back
    here with cli's own parser. MUTANT: drop shlex.quote; the split leaves `00:00:00+00:00` unrecognized."""
    import shlex
    path = _state(tmp_path, server.base_url)
    server.body = {**ANSWER, "owed": [_row(5, "open", "ops/two words"), _row(4, "answered", "ops/two words")]}
    server.echo = _as_owed_acks_answers
    since = "2026-11-01 00:00:00+00:00"
    assert _run(server, path, "--since", since, "--thread", "ops/two words") == 0
    listed = json.loads(capsys.readouterr().out)["buckets"]["answered_not_closed"]["list"]
    try:
        args = cli.build_parser().parse_args(shlex.split(listed) + ["--base-url", "http://x", "--state-file", "s"])
    except SystemExit as exc:                              # argparse refuses it: the bug this test exists for
        pytest.fail(f"the printed command does not parse: {listed!r} (exit {exc.code})")
    assert (args.cmd, args.state, args.since, args.thread, args.limit) == (
        "owed", "answered", since, "ops/two words", 2000), listed


def test_the_text_brief_names_its_own_window_in_the_commands_it_prints(tmp_path, server, capsys):
    """pxx 5e4563 P2-2 (codex-pxx-cli's case: a brief on an older window showed 10 active asks and 1 more, and the
    follow-up `owed --state active` read 14 days and listed none). The active and answered commands now carry the
    brief's --since. MUTANT: print the bare `owed --state active`; the window is not in it."""
    path = _state(tmp_path, server.base_url)
    active = {"hit": {"key": "msg/1790000000011-heron-active", "author": "heron",
                      "recorded_at": "2026-10-01T10:00:00+00:00"}, "thread_id": "ops/lease", "state": "active"}
    brief = {**BRIEF, "owed": {"items": [active], "counts": {"open": 0, "active": 11}, "truncated": False}}
    server.routes = {"/v1/mesh/brief": (200, brief),
                     "/v1/mesh/owed": (200, {"owed": [], "truncated": False,
                                             "filters": {"state": ["answered"], "since": SINCE}})}
    assert _brief(server, path) == 0
    out = capsys.readouterr().out
    assert "replied in thread, not linked: 1 of 11\n" in out
    assert f"`owed --state active --since {SINCE} --limit 2000` lists them all" in out


def test_the_composed_brief_prints_the_same_split_as_the_route(tmp_path, server, capsys):
    """pxx 5e4563 P2-3: on a server without /v1/mesh/brief (404) the composed brief printed open and active together
    as "reply owed" and listed answered rows. It now goes through the same split and the answered count line.
    MUTANT: print the composed owed rows as one section; the active ask is counted as a reply owed."""
    path = _state(tmp_path, server.base_url)
    rows = [_row(9, "open"), _row(8, "active"), _row(7, "active")]
    server.routes = {"/v1/mesh/brief": (404, {"error": "E_NOT_FOUND"})}

    def answer(query, body):
        q = dict(urllib.parse.parse_qsl(query))
        if "help_after_h" in q:                            # the brief route's own query: this server has none
            return 404, {"error": "E_NOT_FOUND"}
        if q.get("state") == "answered":
            return 200, {"owed": [_row(1, "answered")], "truncated": False,
                         "filters": {"state": ["answered"], "since": SINCE}}
        return 200, {"owed": rows, "awaiting": [], "truncated": False,
                     "filters": {"state": ["open", "active"], "since": SINCE}}
    server.echo = answer
    assert _brief(server, path) == 0
    out = capsys.readouterr().out
    assert "reply owed: 1\n" in out and "replied in thread, not linked: 2\n" in out
    assert out.index(_row(7, "active")["hit"]["key"]) < out.index(_row(8, "active")["hit"]["key"]), "oldest first"
    assert "answered, not closed: 1" in out and _row(1, "answered")["hit"]["key"] not in out


@pytest.mark.parametrize("msg_type", ["answer", "ack"])
def test_an_answer_that_expects_a_reply_gets_one_hint_and_stdout_stays_one_document(tmp_path, server, monkeypatch,
                                                                                    capsys, msg_type):
    """SD 255 item 15 (b): 34 of the ~100 asks still open on one seat were answers sent with --requires-ack (oxxxxxx
    05d6c9). An answer that expects a reply puts itself in each recipient's `owed`, so the send says so: one stderr
    line after the accepted send, never a refusal. stdout stays the one JSON document (_accepted_send parses it
    whole), which conformance.send_via (#541) reads (pxx 515e7a B). The types are literals, as in the tests above.
    CONTROL: the same answer without the flag, and a question or request with it, print no such hint.
    MUTANT: drop the `requires_ack` test in cli._send_hints; the unflagged answer hints. MUTANT: test ASK_TYPES
    there; the answer gets none and the question does."""
    linked = ("--in-reply-to", "msg/1790000000000-aaaa1111")

    def ours(hints):
        return [h for h in hints if h.get("hint") == "answer_expects_reply"]

    (hint,) = ours(_accepted_send(tmp_path, server, monkeypatch, capsys, "--type", msg_type, *linked,
                                  "--expects-reply"))
    assert hint["type"] == msg_type and "--type question or request" in hint["note"], hint
    assert ours(_accepted_send(tmp_path, server, monkeypatch, capsys, "--type", msg_type, *linked,
                               "--requires-ack")) == [hint], "the alias is the same flag"
    assert ours(_accepted_send(tmp_path, server, monkeypatch, capsys, "--type", msg_type, *linked)) == []
    for ask in ("question", "request"):
        assert ours(_accepted_send(tmp_path, server, monkeypatch, capsys, "--type", ask, "--expects-reply")) == []
