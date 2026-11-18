"""What the two meshsem CLIs (`python -m apps.meshsem`, `python -m apps.meshsem.related_cli`)
print and decide the same way. No Django here: each CLI bootstraps Django itself, before any
apps import, and imports this after."""
from __future__ import annotations

from typing import Callable


def drain(tick: Callable[[], object], report: Callable[[object], None],
          failed: Callable[[object], bool], until_done: bool) -> int:
    """`catch-up [--until-done]`: one tick, or ticks until one makes no progress. Exit 1 on
    the first tick `failed` refuses (the job would report an error), else 0."""
    while True:
        r = tick()
        report(r)
        if failed(r):
            return 1
        if not until_done or not r.more or r.rows_processed == 0:
            return 0


def print_attempts(attempts) -> None:
    """Every provider attempt that was not admitted, with its reason."""
    for a in attempts:
        if a.get("verdict") != "admitted":
            print(f"  provider {a.get('id')}: {a.get('verdict')} {a.get('reason', '')}")
