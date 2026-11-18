"""What a job body may return besides a row count: the count AND whether it stopped early.

STATUS D18. The runner used to infer "more work pending" from `rows >= max_batch`. That
is right for a body whose only bound is rows read, and wrong for one with a second bound:
the embed tick stops on its embed bound or its wall deadline with rows READ far under
max_batch (divergence 019's limit), and so does the related tick (032). Only the body
knows it stopped early, so the body says so.

A module of its own, importing nothing, because the job modules that return it
(apps/meshsem/embedjob.py, apps/meshsem/related.py, apps/jobs/sweeps.py) are imported BY
apps/jobs/registry.py, and importing the registry back from them would be a cycle.

A body may still return a plain int; the runner then keeps the `rows >= max_batch` rule.
"""
from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class TickResult:
    #: rows the tick processed, as an int body would have returned
    rows: int
    #: the body's own answer to "is there more to do right now", read as given
    more_work_pending: bool
