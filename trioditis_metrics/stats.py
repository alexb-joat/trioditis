"""Small statistics for Trioditis's reports and refits. Stdlib only.

Every rate here comes with its uncertainty: 1 error in 3 calls and 1,000 in
3,000 are both "33%", but only the second is a finding.
"""

from __future__ import annotations

import functools
import math
import random
import statistics
from collections import Counter
from fractions import Fraction

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


def sign_test(b: int, c: int) -> float:
    """Two-sided exact p-value for two settings scored on the same rows (McNemar's exact test).

    `b` rows went right under the first setting only, `c` under the second only; rows both got right or both
    got wrong say nothing about which is better and are left out. Under "no difference" each of the b + c
    disagreements is a fair coin, so the p-value is twice the chance of a split at least this lopsided, at
    most 1. 1.0 when the settings never disagree."""
    if b < 0 or c < 0:
        raise ValueError(f"need b >= 0 and c >= 0, got b={b}, c={c}")
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(b, c) + 1))
    return min(1.0, 2 * tail / 2 ** n)


def mcnemar(b: int, c: int) -> dict:
    """McNemar's test in its four usual forms, for two settings scored on the same rows: do they differ?

    `b` rows went right under the first setting only, `c` under the second only (the same counts
    `sign_test` takes). Only these disagreements matter. Each form gives a p-value; the smaller it is, the
    less a fair coin would split the disagreements this unevenly.

    - `exact_p` is `sign_test(b, c)`. It is safe, but with few disagreements it is cautious: it rejects
      less often than its level says. Use it as the stated rule when the rule must never over-claim.
    - `mid_p` is the exact p-value minus the chance of the observed split itself (Fagerland, Lydersen and
      Laake 2013). It is less cautious and in their trials kept its level, but that is not guaranteed.
      Use it to see how much of the exact p-value is only caution. It is 1 when b = c.
    - `chi2` is (b - c)^2 / (b + c), looked up in a chi-square table with 1 degree of freedom (`chi2_p`).
      It is a large-sample shortcut; with under about 25 disagreements it is not reliable.
    - `chi2_cc` is the same with 1 taken off |b - c| first, the "continuity correction" (`chi2_cc_p`).
      It lands close to `exact_p`, so it is as cautious.

    With no disagreements every p is 1.0 and both statistics are 0.0."""
    if b < 0 or c < 0:
        raise ValueError(f"need b >= 0 and c >= 0, got b={b}, c={c}")
    m = b + c
    out = {"b": b, "c": c, "discordant": m, "exact_p": sign_test(b, c), "mid_p": 1.0,
           "chi2": 0.0, "chi2_p": 1.0, "chi2_cc": 0.0, "chi2_cc_p": 1.0}
    if m == 0:
        return out
    low = min(b, c)
    tail = sum(math.comb(m, i) for i in range(low + 1))                 # P(X <= low) times 2**m
    out["mid_p"] = (2 * tail - math.comb(m, low)) / 2 ** m             # 2 * (P(X <= low) - P(X = low) / 2)
    out["chi2"] = (b - c) ** 2 / m
    out["chi2_cc"] = max(0, abs(b - c) - 1) ** 2 / m
    out["chi2_p"] = math.erfc(math.sqrt(out["chi2"] / 2))              # chi-square, 1 df: P(Z^2 >= x)
    out["chi2_cc_p"] = math.erfc(math.sqrt(out["chi2_cc"] / 2))
    return out


def paired_difference(b: int, c: int, n: int, z: float = Z95) -> tuple[float, float, float] | None:
    """(difference, low, high) for two settings scored on the same n rows: the first setting's share
    right minus the second's, as a fraction, with its interval.

    `b` rows went right under the first setting only, `c` under the second only. The interval is the
    Wald interval after adding 0.5 to each of the paired table's four cells (b + 0.5, c + 0.5, n + 2),
    the "Wald+2" interval of Agresti and Min (Statistics in Medicine 24, 2005, 729-740): the plain
    Wald interval is too narrow with few disagreements and has zero width with none. The difference
    returned is the plain (b - c) / n. None when there is no data."""
    if b < 0 or c < 0 or b + c > n:
        raise ValueError(f"need b >= 0, c >= 0 and b + c <= n, got b={b}, c={c}, n={n}")
    if n <= 0:
        return None
    bb, cc, nn = b + 0.5, c + 0.5, n + 2
    centre = (bb - cc) / nn
    half = z * math.sqrt((bb + cc) - (bb - cc) ** 2 / nn) / nn
    return (b - c) / n, max(-1.0, centre - half), min(1.0, centre + half)


@functools.cache
def _rejection_cutoff(m: int, alpha: float) -> int:
    """The largest k for which `sign_test(k, m - k) < alpha` with k below m / 2 (the same holds for m - k);
    -1 when no split of m disagreements is lopsided enough. One pass over the running count of fair-coin
    sequences with at most k heads, in whole numbers: a split k rejects when 2 * that count < alpha * 2**m."""
    limit = Fraction(alpha) * 2 ** m
    count, running, cutoff = 1, 0, -1                       # count = C(m, k)
    for k in range((m + 1) // 2):
        running += count
        if 2 * running >= limit:
            break
        cutoff = k
        count = count * (m - k) // (k + 1)
    return cutoff


def _binomial_pmf(n: int, k: int, p: float) -> float:
    """P(X = k) for X ~ Binomial(n, p). Computed from logarithms (`lgamma`), so n in the thousands does
    not overflow a float; p of exactly 0 or 1 is handled apart because log(0) is undefined."""
    if p in (0.0, 1.0):
        return float(k == (n if p == 1.0 else 0))
    log_pmf = (math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)
               + k * math.log(p) + (n - k) * math.log1p(-p))
    return math.exp(log_pmf)


def sign_test_power(discordant: int, share: float, alpha: float = 0.05) -> float:
    """The chance that `sign_test` finds a difference, planned before the data exists.

    Suppose the two settings will disagree on `discordant` rows and that each disagreement goes to the
    first setting with probability `share` (0.65 for a 65:35 split). The result is the chance that the
    two-sided exact test then gives p < `alpha`. It is a saw-tooth in `discordant`, not a smooth curve:
    the test can only reject at whole counts, so one more disagreement can lower it (at share 0.65, 114
    give 0.902 and 115 give 0.888). With no disagreements it is 0.0."""
    if discordant < 0 or not 0 <= share <= 1 or not 0 < alpha < 1:
        raise ValueError(f"need discordant >= 0, 0 <= share <= 1, 0 < alpha < 1; got {discordant}, {share}, {alpha}")
    cutoff = _rejection_cutoff(discordant, alpha)
    return min(1.0, math.fsum(_binomial_pmf(discordant, k, share) + _binomial_pmf(discordant, discordant - k, share)
                              for k in range(cutoff + 1)))


def discordant_needed(share: float, power: float = 0.8, alpha: float = 0.05,
                      limit: int = 1000) -> tuple[int | None, int | None]:
    """How many disagreements `sign_test` needs to find a `share` split with chance `power`.

    Returns two numbers: the smallest count that reaches `power`, and the smallest count from which every
    count up to `limit` does. Because the power is a saw-tooth (see `sign_test_power`), the first can be
    followed by counts that fall short again; plan on the second. A number is None when `limit` does not
    reach `power` (for the first, at any count; for the second, at `limit` itself)."""
    if not 0 < power <= 1 or limit < 0:
        raise ValueError(f"need 0 < power <= 1 and limit >= 0, got power={power}, limit={limit}")
    powers = [sign_test_power(m, share, alpha) for m in range(limit + 1)]
    first = next((m for m, p in enumerate(powers) if p >= power), None)
    stable = None
    for m in range(limit, -1, -1):
        if powers[m] < power:
            break
        stable = m
    return first, stable


def paired_power(n: int, discordant_rate: float, share: float, alpha: float = 0.05) -> float:
    """The chance that `sign_test` finds a difference over `n` rows, planned before the data exists.

    Each row is a disagreement with probability `discordant_rate` (the settings differ on that share of
    rows), and each disagreement goes to the first setting with probability `share`. The number of
    disagreements is not known in advance, so this weighs `sign_test_power` at every count m by the
    chance of m disagreements among n rows, Binomial(n, rate). It is exact, so the time grows with the
    square of n: well under a second for n up to 1,000, seconds for n in the thousands."""
    if n < 0 or not 0 <= discordant_rate <= 1:
        raise ValueError(f"need n >= 0 and 0 <= discordant_rate <= 1, got n={n}, discordant_rate={discordant_rate}")
    return min(1.0, math.fsum(_binomial_pmf(n, m, discordant_rate) * sign_test_power(m, share, alpha)
                              for m in range(n + 1)))


def _kappa(pairs: list[tuple[str, str]]) -> float | None:
    n = len(pairs)
    observed = sum(a == b for a, b in pairs) / n
    first, second = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    chance = sum(first[c] * second[c] for c in first) / (n * n)
    return None if chance >= 1 else (observed - chance) / (1 - chance)


def cohen_kappa(pairs, resamples: int = 2000, seed: int = 0) -> dict | None:
    """Agreement between two labellers on the same items, beyond what their label frequencies give by chance.

    `pairs` holds (first label, second label) per item. Kappa is 1 when they always agree, 0 when they
    agree as often as two labellers with these label frequencies would by chance, below 0 when less. The
    95% interval is the 2.5th to 97.5th percentile of kappa over `resamples` bootstrap resamples of the
    items (drawn with a fixed `seed`, so a repeat run prints the same interval). None when there are no
    items or when both labellers used one and the same label throughout (chance agreement is then 1 and
    kappa has no value)."""
    pairs = list(pairs)
    if not pairs:
        return None
    kappa = _kappa(pairs)
    if kappa is None:
        return None
    rng = random.Random(seed)  # noqa: S311  (resampling for an interval, not a secret)
    n = len(pairs)
    draws = sorted(k for k in (_kappa([pairs[rng.randrange(n)] for _ in range(n)]) for _ in range(resamples))
                   if k is not None)
    lo, hi = (quantile(draws, 0.025), quantile(draws, 0.975)) if draws else (kappa, kappa)
    return {"kappa": kappa, "lo": lo, "hi": hi, "n": n, "agree": sum(a == b for a, b in pairs)}


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
