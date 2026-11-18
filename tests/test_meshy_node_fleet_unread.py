"""`python -m tools.meshy_node fleet-unread`: the operator's fleet backlog routes (divergence 081)
from the client, so the HQB exporter and the operator need no hand-built HTTP. Against
tests/test_meshy_node_owed.py's recording server, no database:

  * without --peer, a bearer GET to /v1/mesh/fleet/unread with only the window it is given;
  * with --peer, a GET to /v1/mesh/fleet/unread/<that id>, `limit` allowed;
  * the client refuses (exit 2, nothing sent) a --peer that is not a UUID and a --limit without
    --peer, which the counts route would refuse anyway;
  * a refusal by the server (403 for a caller without the authority or the scope) exits 1 with
    its body, as every other subcommand does.

Run:  pytest tests/test_meshy_node_fleet_unread.py -q     (needs no database)
Mutants (each seen to fail a test here, tools/meshy_node/node.py):
  C1  the peer id is not checked                         test_a_peer_that_is_not_a_uuid_is_refused_before_sending
  C2  --limit is sent to the counts route                test_limit_without_peer_is_refused_before_sending
  C3  --peer is ignored (the counts route is called)     test_peer_reads_that_peers_waiting_messages
"""
from __future__ import annotations

import json
import uuid

from tests.test_meshy_node_owed import _state, server  # noqa: F401
from tools.meshy_node import cli

COUNTS = {"as_of": "2026-10-06T15:00:00+00:00",
          "filters": {"since": "2026-09-29T15:00:00+00:00", "days": 7, "retention_days": 90,
                      "capped": False},
          "fleet": {"peers": [{"peer_id": str(uuid.uuid4()), "label": "otter", "label_source": "roster",
                               "count": 2, "oldest_at": "2026-10-06T10:00:00+00:00",
                               "oldest_age_secs": 18000, "addressed": True}],
                    "candidates": 1, "cap": 200, "truncated": False,
                    "sources": {"addressed": 1, "senders": 0, "roster": 1}, "unanswered_labels": 0},
          "coverage": {"projected": 1, "total": 1}}


def _run(server, path, *args) -> int:
    return cli.main(["fleet-unread", "--base-url", server.base_url, "--state-file", str(path), *args])


def test_counts_send_only_the_window_given_and_print_the_answer(tmp_path, server, capsys):
    server.body = COUNTS
    path = _state(tmp_path, server.base_url)
    assert _run(server, path) == 0
    assert json.loads(capsys.readouterr().out) == COUNTS
    assert _run(server, path, "--days", "3") == 0
    capsys.readouterr()
    first, second = server.seen
    assert first["path"] == "/v1/mesh/fleet/unread" and first["query"] == {}
    assert second["path"] == "/v1/mesh/fleet/unread" and second["query"] == {"days": "3"}
    assert first["headers"]["authorization"].startswith("Bearer ")


def test_peer_reads_that_peers_waiting_messages(tmp_path, server, capsys):
    """MUTANT C3."""
    path = _state(tmp_path, server.base_url)
    peer = str(uuid.uuid4())
    assert _run(server, path, "--peer", peer.upper(), "--limit", "5") == 0
    capsys.readouterr()
    (request,) = server.seen
    assert request["path"] == f"/v1/mesh/fleet/unread/{peer}" and request["query"] == {"limit": "5"}


def test_a_peer_that_is_not_a_uuid_is_refused_before_sending(tmp_path, server, capsys):
    """MUTANT C1: a path segment is never anything but a UUID."""
    path = _state(tmp_path, server.base_url)
    assert _run(server, path, "--peer", "../inbox") == 2
    assert server.seen == []


def test_limit_without_peer_is_refused_before_sending(tmp_path, server, capsys):
    """MUTANT C2."""
    path = _state(tmp_path, server.base_url)
    assert _run(server, path, "--limit", "5") == 2
    assert server.seen == []


def test_a_server_refusal_exits_1_with_its_body(tmp_path, server, capsys):
    server.status = 403
    server.body = {"error": "E_SCOPE_REQUIRED", "message": "this route needs the bootstrap authority "
                                                           "or the mesh:backlog:read scope"}
    path = _state(tmp_path, server.base_url)
    assert _run(server, path) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["http_status"] == 403 and out["body"] == server.body
