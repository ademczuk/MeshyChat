"""One conversion of a read-model value into what `JsonResponse` can encode.

apps/board/views.py and apps/mesh/views.py each carried this function, character for
character. Their routes put the board's and the mesh graph's dataclasses (acts, edges, hits,
each with a `recorded_at` datetime) on the wire through it, so a change to one copy - a
datetime rendered another way, a tuple left as a tuple - would make two read surfaces over
one log disagree about the same row. Both bind it as `_jsonable`.

What it does, and nothing more: a dataclass becomes the dict `dataclasses.asdict` gives (its
field order, which is the wire's key order), recursed; a datetime its `isoformat()`; a list
or tuple a list; a dict a dict with its values converted; anything else passes through.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt


def jsonable(obj):
    if dataclasses.is_dataclass(obj):
        return {k: jsonable(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, _dt.datetime):
        return obj.isoformat()
    if isinstance(obj, (list, tuple)):
        return [jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {k: jsonable(v) for k, v in obj.items()}
    return obj


__all__ = ["jsonable"]
