"""When a job runs next. Jitter is mandatory, and that is the whole point of the module.

THE FAILURE THIS PREVENTS. docs/V1-DESIGN-RULES.md E7: an hourly full-table audit with
its timer anchored to process start can produce a 2.5-minute fleet-wide stall every
hour, at a fixed minute. The fixed minute is the part that
scales badly. Every instance that boots in the same deploy shares a start time, so every
instance runs the heavy job in the same minute, forever, and the load they generate is a
spike rather than a distribution.

SO ZERO JITTER IS REFUSED RATHER THAN DEFAULTED. A4 says no enforcement behind a
default-off flag, and a jitter fraction that can be set to 0 is that flag wearing a
number. A caller who genuinely wants a fixed instant does not want a scheduler.

AND THE SCHEDULE IS ANCHORED TO COMPLETION, NOT TO A GRID. `next_run_at` is written as
`now() + delay` when the job finishes (apps/jobs/leases.release). A job that overruns its
interval therefore spaces out instead of queueing up against itself, which is the second
half of the #385 shape: the stall was long enough that the next tick was already due.
"""
from __future__ import annotations

import random

#: Bounds on the jitter fraction. The upper bound keeps a schedule recognizable: at 0.5
#: an "hourly" job runs anywhere from 30 to 90 minutes apart, which is the most spread
#: that can still honestly be called hourly.
MIN_JITTER_FRAC = 0.01
MAX_JITTER_FRAC = 0.5


def next_delay_secs(interval_secs: float, jitter_frac: float,
                    rng: random.Random | None = None) -> float:
    """A jittered delay in seconds, symmetric around `interval_secs`.

    `rng` is injectable so a test can pin the draw. It is NOT seeded from anything
    process-derived by default: seeding per process would make every instance in a
    deploy draw the same sequence, which reintroduces the synchronized spike this
    module exists to break.
    """
    if interval_secs <= 0:
        raise ValueError("interval_secs must be > 0")
    if not (MIN_JITTER_FRAC <= jitter_frac <= MAX_JITTER_FRAC):
        raise ValueError(
            f"jitter_frac must be within [{MIN_JITTER_FRAC}, {MAX_JITTER_FRAC}]; "
            f"got {jitter_frac}. Zero jitter is refused on purpose: a fixed-offset "
            "schedule synchronizes every instance onto the same minute."
        )
    r = rng or random.SystemRandom()
    spread = interval_secs * jitter_frac
    delay = interval_secs + r.uniform(-spread, spread)
    # A draw at the extreme of a wide spread can still land at or below zero for very
    # small intervals; clamp so a delay is always forward in time.
    return max(0.001, delay)


def backoff_delay_secs(interval_secs: float, consecutive_failures: int,
                       jitter_frac: float, rng: random.Random | None = None,
                       cap_multiple: int = 32) -> float:
    """Jittered exponential backoff for a failing job, capped.

    Capped because an uncapped backoff on a job that fails for a week schedules the
    retry past any window anyone is watching, and the job becomes silently retired
    rather than loudly broken.
    """
    if consecutive_failures <= 0:
        return next_delay_secs(interval_secs, jitter_frac, rng)
    multiple = min(2 ** consecutive_failures, cap_multiple)
    return next_delay_secs(interval_secs * multiple, jitter_frac, rng)
