"""Work-dispatch planner for a hosted task board: NOT INCLUDED in the open-source release.

See board_tasks.py. These names stay so the command line builds; planning raises BoardUnavailable.
"""
from __future__ import annotations

from .board_tasks import NOT_INCLUDED, BoardUnavailable


def plan(*args, **kwargs):
    raise BoardUnavailable(NOT_INCLUDED)


def render(*args, **kwargs) -> str:
    return "board dispatch: not included in this release\n"
