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


# Two-sided 95% Student t quantiles for 1-30 degrees of freedom (standard tables); above 30, Z95.
_T95 = (12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228, 2.201, 2.179, 2.160, 2.145,
        2.131, 2.120, 2.110, 2.101, 2.093, 2.086, 2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048,
        2.045, 2.042)


def t95(df: int) -> float:
    """Two-sided 95% t quantile; the normal value once df passes the table."""
    if df < 1:
        raise ValueError(f"need df >= 1, got {df}")
    return _T95[df - 1] if df <= len(_T95) else Z95


def ratio_interval(pairs) -> dict | None:
    """How many times larger `a` is than `b`, over paired measurements [(a, b), ...]: the geometric
    mean of a/b with a 95% t-interval on the log scale. Ratios multiply, so logs make them additive
    and a 2x and a 0.5x cancel out. Pairs with a zero or negative side are skipped and counted.
    With one usable pair there is a ratio but no interval (lo and hi are None)."""
    logs, skipped = [], 0
    for a, b in pairs:
        if a is None or b is None or a <= 0 or b <= 0:
            skipped += 1
            continue
        logs.append(math.log(a / b))
    n = len(logs)
    if n == 0:
        return None
    mean = statistics.fmean(logs)
    out = {"ratio": math.exp(mean), "lo": None, "hi": None, "n": n, "skipped": skipped,
           "ratios": [math.exp(x) for x in logs]}
    if n >= 2:
        half = t95(n - 1) * statistics.stdev(logs) / math.sqrt(n)
        out["lo"], out["hi"] = math.exp(mean - half), math.exp(mean + half)
    return out


def ratio_estimate(pairs, min_var: float = 0.0) -> dict | None:
    """sum(y) / sum(x) over pairs [(y, x), ...], with a 95% t-interval: the ratio estimator of survey
    sampling (Cochran, Sampling Techniques, 3rd ed., section 6.3), its standard error from the spread
    of y - R·x. Unlike the mean of y/x it weighs each pair by its size, so a pair with a tiny x can't
    dominate. None when sum(x) is not positive; with one pair there is a value but no interval. The
    lower bound stops at 0, for quantities that can't be negative. `min_var` is a known floor on the
    variance of each y around R·x (rounding, say), so a few pairs that happen to agree can't give a
    narrower interval than the measurement allows."""
    pairs = list(pairs)
    n, sx, sy = len(pairs), sum(x for _, x in pairs), sum(y for y, _ in pairs)
    if n == 0 or sx <= 0:
        return None
    r = sy / sx
    out = {"value": r, "lo": None, "hi": None, "se": None, "n": n}
    if n >= 2:
        var = max(sum((y - r * x) ** 2 for y, x in pairs) / (n - 1), min_var)
        se = math.sqrt(var / n) / (sx / n)
        half = t95(n - 1) * se
        out.update(lo=max(0.0, r - half), hi=r + half, se=se)
    return out
