"""Work-claim adapter for a hosted task board: NOT INCLUDED in the open-source release.

See board_tasks.py. These names stay so the command line builds; every claim operation raises BoardUnavailable.
"""
from __future__ import annotations

import time

from .board_tasks import NOT_INCLUDED, BoardUnavailable

EXIT_OK, EXIT_REFUSED, EXIT_USAGE, EXIT_LOST, EXIT_BOARD = 0, 1, 2, 3, 4
EXIT_PRESENCE = 5
PRESENCE_TTL_SECONDS = 3600


class Refused(RuntimeError):
    pass


class Lost(RuntimeError):
    pass


class Me:
    def __init__(self, seat, nonce, box_user):
        self.seat, self.nonce, self.box_user = seat, nonce, box_user


class ClaimBoard:
    def __init__(self, *args, **kwargs):
        raise BoardUnavailable(NOT_INCLUDED)


class _Lock:
    def __init__(self, *args, **kwargs):
        raise BoardUnavailable(NOT_INCLUDED)


def _now() -> float:
    return time.time()


def session_key(override=None):
    return override


def session_nonce(seat, key) -> str:
    return ""


def box_user(seat):
    return None


def remember_box_user(seat, user) -> None:
    return None


def presence_for(verb, issue, report):
    return None


def run(*args, **kwargs):
    raise BoardUnavailable(NOT_INCLUDED)


def run_pause(*args, **kwargs):
    raise BoardUnavailable(NOT_INCLUDED)
