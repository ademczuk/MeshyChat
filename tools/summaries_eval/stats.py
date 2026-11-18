"""The arithmetic the measurement decides by, fixed before any score exists (design 7).

Pure. Every function here is checked against the figures docs/design/thread-summaries.md
section 11 states (Wilson bounds, kappa at 0.95 marginals, Clopper-Pearson for 0 of 6),
so a slip in the formula is caught by the design's own numbers.
"""
from __future__ import annotations

import math
import random

Z95 = 1.959964


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    """The Wilson score interval for k successes in n. (0, 1) for an empty sample."""
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    z2 = z * z
    centre = p + z2 / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    return max(0.0, (centre - spread) / (1 + z2 / n)), min(1.0, (centre + spread) / (1 + z2 / n))


def fewest_to_clear(n: int, bound: float, z: float = Z95) -> int | None:
    """The fewest successes in n whose Wilson lower bound reaches `bound`, or None."""
    for k in range(n + 1):
        if wilson(k, n, z)[0] >= bound:
            return k
    return None


def _binom_cdf(k: int, n: int, p: float) -> float:
    return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k + 1))


def clopper_pearson_upper(k: int, n: int, alpha: float = 0.05) -> float:
    """The one-sided (1 - alpha) Clopper-Pearson upper bound on a rate, by bisection on the
    binomial CDF: the largest p with P(X <= k | p) >= alpha."""
    if k >= n:
        return 1.0
    lo, hi = 0.0, 1.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if _binom_cdf(k, n, mid) >= alpha:
            lo = mid
        else:
            hi = mid
    return lo


def cohen_kappa(a: list, b: list) -> float:
    """Cohen's kappa for two raters' binary labels on the same items."""
    if len(a) != len(b) or not a:
        raise ValueError("kappa needs two equal, non-empty label lists")
    n = len(a)
    po = sum(1 for x, y in zip(a, b) if bool(x) == bool(y)) / n
    pa, pb = sum(map(bool, a)) / n, sum(map(bool, b)) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    return 1.0 if pe == 1 else (po - pe) / (1 - pe)


def kappa_from_marginals(p_yes: float, agreement: float) -> float:
    """Kappa when both raters say yes at rate `p_yes` and agree on `agreement` of items
    (the design's arithmetic for why kappa on the live stream voids good runs, C1)."""
    pe = p_yes * p_yes + (1 - p_yes) * (1 - p_yes)
    return (agreement - pe) / (1 - pe)


def pabak(a: list, b: list) -> float:
    """Prevalence-adjusted bias-adjusted kappa: 2 * observed agreement - 1."""
    n = len(a)
    return 2 * sum(1 for x, y in zip(a, b) if bool(x) == bool(y)) / n - 1


def cluster_bootstrap(clusters: dict, stat, *, n: int = 2000, seed: int,
                      level: float = 0.95) -> tuple[float, float]:
    """A percentile interval for `stat(list_of_cluster_values)`, resampling CLUSTERS
    (threads) with replacement: sentences of one summary are not independent (7.4)."""
    keys = sorted(clusters)
    if not keys:
        return 0.0, 1.0
    rng = random.Random(seed)
    values = []
    for _ in range(n):
        pick = [clusters[keys[rng.randrange(len(keys))]] for _ in keys]
        values.append(stat(pick))
    values.sort()
    lo_i = int(math.floor((1 - level) / 2 * n))
    hi_i = int(math.ceil((1 + level) / 2 * n)) - 1
    return values[lo_i], values[min(hi_i, n - 1)]


def ratio(pick: list, num: str, den: str) -> float:
    """sum(num) / sum(den) over picked clusters, each a dict of counts."""
    d = sum(c[den] for c in pick)
    return sum(c[num] for c in pick) / d if d else 0.0


__all__ = ["Z95", "clopper_pearson_upper", "cluster_bootstrap", "cohen_kappa",
           "fewest_to_clear", "kappa_from_marginals", "pabak", "ratio", "wilson"]
