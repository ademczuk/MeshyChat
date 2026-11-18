"""Ranked work-queue adapter for a hosted task board: NOT INCLUDED in the open-source release.

See board_tasks.py. These names stay so the command line builds; every board operation raises BoardUnavailable.
"""
from __future__ import annotations

import re
from typing import Any

from .board_tasks import NOT_INCLUDED, BoardUnavailable

EXIT_OK, EXIT_LOST, EXIT_USAGE, EXIT_BOARD = 0, 3, 2, 4
DEFAULT_TARGET = ""
DEFAULT_DAYS = 14
MAX_EXTRA = 100
QUEUE_DOC_KEY = ""

_ID_RE = re.compile(r"[A-Z][A-Z0-9]{1,9}-\d{1,7}")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2060-\u2069\u061c\u180e\ufeff\ud800-\udfff]")


class ReadBudgetSpent(RuntimeError):
    pass


class Anchor:
    def __init__(self, *args, **kwargs):
        raise BoardUnavailable(NOT_INCLUDED)


class ViewBoard:
    def __init__(self, *args, **kwargs):
        raise BoardUnavailable(NOT_INCLUDED)


def is_id(value: Any) -> bool:
    return isinstance(value, str) and _ID_RE.fullmatch(value) is not None


def is_queue_key(value: Any) -> bool:
    return False


def safe_text(text: Any, *, keep_newlines: bool = False) -> str:
    s = "" if text is None else str(text)
    if keep_newlines:
        return "\n".join(_CTRL_RE.sub(" ", line) for line in s.replace("\r\n", "\n").replace("\r", "\n").split("\n"))
    return _CTRL_RE.sub(" ", s.replace("\r\n", " ").replace("\n", " ").replace("\r", " "))


def queue_rows(body):
    raise BoardUnavailable(NOT_INCLUDED)


def partial_reasons(parsed):
    return []


def frame(*args, **kwargs):
    raise BoardUnavailable(NOT_INCLUDED)


def refresh(*args, **kwargs):
    raise BoardUnavailable(NOT_INCLUDED)
