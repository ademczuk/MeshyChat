"""Hosted task-board adapter: NOT INCLUDED in the open-source release.

The original deployment connects MeshyChat nodes to a hosted task board (read a node's open items, claim work, read
a ranked work queue). That adapter talks to one organisation's private service and is left out of this release.
This module keeps the names the rest of the client imports, so `orient` and the command line work without it:
every board read raises `BoardUnavailable`, which the callers already report as "board unavailable".

To connect your own board, replace this module (and board_queue.py, board_claim.py, board_dispatch.py) with an
adapter that keeps these names and signatures.
"""
from __future__ import annotations

import json
from pathlib import Path

NOT_INCLUDED = "the hosted task-board adapter is not included in the open-source release"

#: Tool names an adapter may call. Empty: there is no board.
READ_TOOLS: frozenset = frozenset()
CLAIM_TOOLS: frozenset = frozenset()
#: Codex-runtime nodes that may use the board; none without an adapter.
CODEX_BOARD_SEATS: frozenset = frozenset()
#: Environment variables that mark a Codex sandbox (used to refuse board writes from one).
_CODEX_ENV = ("CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED", "CODEX_THREAD_ID", "CODEX_HOME")
LINE_SEPARATORS = (0x85, 0x2028, 0x2029)


class BoardUnavailable(RuntimeError):
    """No board: the adapter is not part of this release."""


def _q(text) -> str:
    """Text for the terminal: quoted, with newlines, controls and the Unicode line separators escaped."""
    out = json.dumps(str(text), ensure_ascii=False)
    for cp in LINE_SEPARATORS:
        out = out.replace(chr(cp), chr(92) + "u%04x" % cp)
    return out


def plugin_root():
    raise BoardUnavailable(NOT_INCLUDED)


class McpBoard:
    def __init__(self, *args, **kwargs):
        raise BoardUnavailable(NOT_INCLUDED)


def codex_refusal(seat, state_file=None):
    return None


def fleet_seats_roster():
    return []


def default_cursor_path(seat) -> Path:
    return Path.home() / ".meshychat" / "tasks" / ("%s.cursor.json" % seat)


def load_owner_map(path):
    return {}


def run_tasks(*args, **kwargs):
    raise BoardUnavailable(NOT_INCLUDED)


def scan_complete(report) -> bool:
    return True


def format_text(report) -> str:
    return "board tasks: not included in this release\n"
