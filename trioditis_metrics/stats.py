"""Small statistics for Trioditis's reports and refits. Stdlib only.

Every rate here comes with its uncertainty: 1 error in 3 calls and 1,000 in
3,000 are both "33%", but only the second is a finding.
"""

from __future__ import annotations

import math
import statistics

Z95 = 1.959963984540054  # two-sided 95%


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float] | None:
    """Interval for a proportion k/n (Wilson score). Unlike k/n ± z·√(p(1−p)/n) it stays
    inside [0, 1] and is not zero-width at 0 of n or n of n. None when there is no data."""
    if n <= 0:
        return None
    if not 0 <= k <= n:
        raise ValueError(f"need 0 <= k <= n, got k={k}, n={n}")
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    # At 0 of n or n of n the bound is exactly 0 or 1; say so instead of 0.9999999999999999.
    return (0.0 if k == 0 else max(0.0, centre - half)), (1.0 if k == n else min(1.0, centre + half))


def quantile(xs, q: float) -> float | None:
    """Quantile by linear interpolation between order statistics (R's and numpy's default)."""
    if not 0 <= q <= 1:
        raise ValueError(f"q must be in [0, 1], got {q}")
    xs = sorted(xs)
    if not xs:
        return None
    pos = (len(xs) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def ewma(xs, alpha: float = 0.2) -> list[float]:
    """Exponentially weighted moving average, started at the first value."""
    out: list[float] = []
    for x in xs:
        out.append(x if not out else alpha * x + (1 - alpha) * out[-1])
    return out


def ewma_chart(xs, alpha: float = 0.2, L: float = 3.0, mu0: float | None = None,
               sigma: float | None = None, warmup: int = 10) -> dict:
    """EWMA control chart (Roberts 1959; limits as in Montgomery, Statistical Quality Control).

    The target `mu0` and the spread `sigma` come from the first `warmup` points unless both are
    given; the chart then runs on the points after them. Limits widen from the start towards
    mu0 ± L·sigma·√(alpha/(2−alpha)). `alarms` holds the index where each excursion begins,
    so a lasting shift raises one alarm, not one per point. A baseline with no spread can't
    be judged: no limits, no alarms.
    """
    xs = list(xs)
    if mu0 is None or sigma is None:
        base = xs[:warmup]
        if len(base) < 2:
            return {"mu0": mu0, "sigma": sigma, "points": [], "alarms": []}
        mu0 = statistics.fmean(base) if mu0 is None else mu0
        sigma = statistics.stdev(base) if sigma is None else sigma
        start = warmup
    else:
        start = 0
    if sigma <= 0:
        return {"mu0": mu0, "sigma": sigma, "points": [], "alarms": []}
    z, points, alarms, was_out = mu0, [], [], False
    for i, x in enumerate(xs[start:], start=1):
        z = alpha * x + (1 - alpha) * z
        width = L * sigma * math.sqrt(alpha / (2 - alpha) * (1 - (1 - alpha) ** (2 * i)))
        out = not (mu0 - width <= z <= mu0 + width)
        index = start + i - 1
        points.append({"i": index, "x": x, "ewma": z, "lo": mu0 - width, "hi": mu0 + width, "out": out})
        if out and not was_out:
            alarms.append(index)
        was_out = out
    return {"mu0": mu0, "sigma": sigma, "points": points, "alarms": alarms}


def beta_mean(k: int, n: int, prior_mean: float, strength: float = 10.0) -> float:
    """Posterior mean of a rate under a Beta prior worth `strength` observations at
    `prior_mean`: few observations stay near the prior, many speak for themselves."""
    if strength < 0 or not 0 <= prior_mean <= 1:
        raise ValueError("need strength >= 0 and 0 <= prior_mean <= 1")
    if n + strength == 0:
        return prior_mean
    return (k + prior_mean * strength) / (n + strength)
