import math
from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from trioditis_metrics.stats import (
    beta_mean,
    cohen_kappa,
    ewma,
    ewma_chart,
    mcnemar,
    quantile,
    ratio_estimate,
    sign_test,
    wilson,
)


def test_wilson_matches_published_values():
    lo, hi = wilson(8, 10)
    assert lo == pytest.approx(0.4902, abs=1e-4) and hi == pytest.approx(0.9433, abs=1e-4)
    lo, hi = wilson(0, 10)  # no successes still leaves room above zero
    assert lo == 0.0 and hi == pytest.approx(0.2775, abs=1e-4)
    assert wilson(10, 10)[1] == 1.0
    assert wilson(0, 0) is None
    with pytest.raises(ValueError):
        wilson(3, 2)


def test_more_data_narrows_the_interval():
    small, large = wilson(3, 10), wilson(300, 1000)
    assert large[1] - large[0] < (small[1] - small[0]) / 5


def test_quantile_interpolates():
    assert quantile([4, 1, 3, 2], 0.5) == 2.5
    assert quantile([1, 2, 3, 4], 0) == 1 and quantile([1, 2, 3, 4], 1) == 4
    assert quantile([10, 20, 30, 40, 50], 0.95) == pytest.approx(48.0)
    assert quantile([], 0.5) is None
    with pytest.raises(ValueError):
        quantile([1], 1.5)


def test_ewma_starts_at_the_first_value():
    assert ewma([10, 0, 0], alpha=0.5) == [10, 5, 2.5]
    assert ewma([]) == []


def test_chart_stays_quiet_on_a_stable_series():
    stable = [0.1 if i % 2 else -0.1 for i in range(60)]
    assert ewma_chart(stable)["alarms"] == []


def test_chart_raises_one_alarm_for_a_lasting_shift():
    series = [0.1 if i % 2 else -0.1 for i in range(30)] + [1.0 + (0.1 if i % 2 else -0.1) for i in range(30)]
    chart = ewma_chart(series)
    assert len(chart["alarms"]) == 1 and 30 <= chart["alarms"][0] <= 32
    assert all(p["out"] for p in chart["points"] if p["i"] >= chart["alarms"][0])


def test_chart_with_a_known_target_charts_from_the_start():
    chart = ewma_chart([0.0, 0.0, 5.0], mu0=0.0, sigma=1.0)
    assert [p["i"] for p in chart["points"]] == [0, 1, 2] and chart["alarms"] == [2]


def test_chart_cannot_judge_a_flat_baseline():
    chart = ewma_chart([0.0] * 10 + [1.0] * 5)
    assert chart["alarms"] == [] and chart["points"] == []


def test_beta_mean_shrinks_small_samples_towards_the_prior():
    assert beta_mean(0, 0, 0.2) == 0.2
    assert beta_mean(5, 10, 0.2, strength=10) == pytest.approx(0.35)
    assert beta_mean(500, 1000, 0.2, strength=10) == pytest.approx(0.497, abs=1e-3)


def test_ratio_interval_recovers_a_known_ratio():
    import math
    import random

    from trioditis_metrics.stats import ratio_interval
    rng = random.Random(7)  # noqa: S311
    pairs = []
    for _ in range(40):
        b = rng.uniform(1_000, 20_000)
        pairs.append((b * 2.5 * math.exp(rng.gauss(0, 0.2)), b))
    r = ratio_interval(pairs)
    assert r["n"] == 40 and r["lo"] < 2.5 < r["hi"]
    assert r["ratio"] == pytest.approx(2.5, rel=0.1)


def test_ratio_interval_small_samples_and_bad_pairs():
    from trioditis_metrics.stats import ratio_interval, t95
    assert ratio_interval([]) is None
    one = ratio_interval([(30, 10), (5, 0)])
    assert one["ratio"] == pytest.approx(3.0) and one["lo"] is None and one["skipped"] == 1
    # Geometric, not arithmetic: 2x and 0.5x average to 1x.
    assert ratio_interval([(2, 1), (1, 2)])["ratio"] == pytest.approx(1.0)
    # The five ratios from a worked example: exp(mean(log r)) with t(4) = 2.776.
    r = ratio_interval([(1.3, 1), (1.9, 1), (1.5, 1), (2.6, 1), (1.4, 1)])
    assert r["ratio"] == pytest.approx(1.6826, abs=1e-3)
    assert r["lo"] == pytest.approx(1.1859, abs=1e-3) and r["hi"] == pytest.approx(2.3874, abs=1e-3)
    assert t95(4) == 2.776 and t95(500) == pytest.approx(1.96, abs=1e-3)


def test_ratio_estimate_by_hand():
    # sum y / sum x = 13/6; residuals -1/6, -1/3, 1/2 -> variance 0.3889/2; se = sqrt(0.19444/3) / 2
    est = ratio_estimate([(2, 1), (4, 2), (7, 3)])
    assert est["value"] == pytest.approx(13 / 6) and est["n"] == 3
    assert est["se"] == pytest.approx(0.127294, abs=1e-6)
    assert est["lo"] == pytest.approx(13 / 6 - 4.303 * 0.127294, abs=1e-5)
    assert est["hi"] == pytest.approx(13 / 6 + 4.303 * 0.127294, abs=1e-5)


def test_ratio_estimate_floor_edges_and_weighting():
    # a variance floor widens an interval the pairs alone would make too narrow
    assert ratio_estimate([(2, 1), (4, 2), (6, 3)])["se"] == 0
    assert ratio_estimate([(2, 1), (4, 2), (6, 3)], min_var=1)["se"] == pytest.approx((1 / 3) ** 0.5 / 2)
    one = ratio_estimate([(5, 2)])
    assert one["value"] == 2.5 and one["lo"] is None and one["hi"] is None
    assert ratio_estimate([]) is None and ratio_estimate([(1, 0)]) is None
    # a pair with a tiny x doesn't dominate the way a mean of ratios would let it
    assert ratio_estimate([(1, 0.01), (10, 10), (10, 10)])["value"] == pytest.approx(21 / 20.01)
    assert ratio_estimate([(0, 1), (1, 1), (0, 1)])["lo"] == 0.0  # never below zero


def test_sign_test_matches_hand_computed_values():
    assert sign_test(0, 0) == 1.0                          # never disagree: no evidence
    assert sign_test(0, 4) == pytest.approx(2 / 16)        # 2 * P(0 of 4 heads)
    assert sign_test(0, 5) == pytest.approx(2 / 32)        # 0.0625: five straight is still not below 0.05
    assert sign_test(0, 6) == pytest.approx(2 / 64)        # 0.03125
    assert sign_test(1, 5) == pytest.approx(14 / 64)       # 2 * (1 + 6) / 2**6
    assert sign_test(2, 10) == pytest.approx(158 / 4096)   # 2 * (1 + 12 + 66) / 2**12
    assert sign_test(5, 5) == 1.0                          # an even split: the doubled tail passes 1 and is capped
    assert sign_test(3, 1) == sign_test(1, 3) == pytest.approx(2 * 5 / 16)  # symmetric in b and c
    assert sign_test(0, 1) == 1.0                          # one disagreement says nothing


def test_sign_test_refuses_negative_counts():
    with pytest.raises(ValueError, match="b >= 0"):
        sign_test(-1, 3)


def test_cohen_kappa_matches_a_hand_worked_table_and_repeats_exactly():
    # 50 items: both yes 20, both no 15, first yes / second no 5, first no / second yes 10.
    # Observed agreement 35/50 = 0.7; chance = 0.5 * 0.6 + 0.5 * 0.4 = 0.5; kappa = (0.7 - 0.5) / 0.5 = 0.4.
    pairs = [("yes", "yes")] * 20 + [("no", "no")] * 15 + [("yes", "no")] * 5 + [("no", "yes")] * 10
    out = cohen_kappa(pairs)
    assert out["kappa"] == pytest.approx(0.4) and (out["n"], out["agree"]) == (50, 35)
    assert out["lo"] < 0.4 < out["hi"] and 0.1 < out["lo"] and out["hi"] < 0.7
    assert cohen_kappa(pairs) == out                       # the resampling is seeded
    assert cohen_kappa(reversed(pairs))["kappa"] == pytest.approx(0.4)


def test_cohen_kappa_at_the_edges():
    assert cohen_kappa([]) is None
    assert cohen_kappa([("a", "a")] * 5) is None           # one label throughout: no value
    full = cohen_kappa([("a", "a"), ("b", "b")] * 10)
    assert full["kappa"] == 1.0 and full["hi"] == 1.0
    assert cohen_kappa([("a", "b"), ("b", "a")] * 10)["kappa"] == pytest.approx(-1.0)


# ---------------------------------------------------------------- McNemar's test in full

def binomial_half_pmf(m: int) -> list[Fraction]:
    """P(X = i) for X ~ Binomial(m, 1/2), i = 0..m, built from the ratio of neighbours (the recurrence
    P(i + 1) = P(i) * (m - i) / (i + 1)), so it shares no code with `math.comb`."""
    pmf = [Fraction(1, 2 ** m)]
    for i in range(m):
        pmf.append(pmf[-1] * (m - i) / (i + 1))
    return pmf


def chi2_1df_tail(x: float) -> float:
    """P(chi-square with 1 degree of freedom >= x), from the power series of the regularised incomplete gamma
    function, P(1/2, y) = y^(1/2) e^-y / Gamma(3/2) * sum_k y^k / ((3/2)(5/2)...(3/2 + k - 1)), with y = x / 2.
    It uses neither `math.erfc` nor `math.erf`, which is what `mcnemar` uses."""
    y = x / 2
    term, total = 1.0, 1.0
    for k in range(1, 400):
        term *= y / (1.5 + k - 1)
        total += term
    return 1 - math.sqrt(y) * math.exp(-y) / (math.sqrt(math.pi) / 2) * total


def test_mcnemar_matches_hand_values_for_20_against_28():
    """b = 20 rows only the first setting got right, c = 28 only the second (m = 48 disagreements).
    Derived on 25 Nov 2026 from the definitions, then compared with the function:
      exact  = 2 * P(X <= 20) for X ~ Binomial(48, 1/2)            = 0.312327
      mid-p  = exact - P(X = 20)    (b != c)                        = 0.252870
      chi2   = (20 - 28)^2 / 48 = 64 / 48                           = 1.333333, p = erfc(sqrt(1.333333 / 2)) = 0.248213
      chi2cc = (|20 - 28| - 1)^2 / 48 = 49 / 48                     = 1.020833, p = erfc(sqrt(1.020833 / 2)) = 0.312321
    The two chi-square p-values are checked a second way below, by a power series that does not use erfc."""
    out = mcnemar(20, 28)
    assert (out["b"], out["c"], out["discordant"]) == (20, 28, 48)
    assert out["exact_p"] == pytest.approx(0.312327, abs=5e-7)
    assert out["mid_p"] == pytest.approx(0.252870, abs=5e-7)
    assert out["chi2"] == pytest.approx(64 / 48) and out["chi2_p"] == pytest.approx(0.248213, abs=5e-7)
    assert out["chi2_cc"] == pytest.approx(49 / 48) and out["chi2_cc_p"] == pytest.approx(0.312321, abs=5e-7)
    assert list(out) == ["b", "c", "discordant", "exact_p", "mid_p", "chi2", "chi2_p", "chi2_cc", "chi2_cc_p"]


@pytest.mark.parametrize("b, c", [(20, 28), (1, 7), (6, 16), (0, 5), (3, 3), (40, 90)])
def test_mcnemar_agrees_with_an_independent_brute_force_version(b, c):
    """Exact and mid-p again, from exact fractions of the Binomial(m, 1/2) probabilities; the chi-square
    p-values again, from a power series. Equal to 12 decimals."""
    m, low = b + c, min(b, c)
    pmf = binomial_half_pmf(m)
    tail = sum(pmf[: low + 1])
    exact, mid = min(Fraction(1), 2 * tail), 2 * (tail - pmf[low] / 2)
    out = mcnemar(b, c)
    assert out["exact_p"] == pytest.approx(float(exact), abs=1e-12)
    assert out["mid_p"] == pytest.approx(float(mid), abs=1e-12)
    for stat, p in (("chi2", "chi2_p"), ("chi2_cc", "chi2_cc_p")):
        want = chi2_1df_tail(out[stat]) if out[stat] else 1.0
        assert out[p] == pytest.approx(want, abs=1e-12)


def test_mcnemar_reproduces_the_worked_examples_of_fagerland_lydersen_and_laake():
    """Fagerland MW, Lydersen S, Laake P. The McNemar test for binary matched-pairs data: mid-p and asymptotic
    are better than exact conditional. BMC Medical Research Methodology 2013, 13:91
    (https://bmcmedresmethodol.biomedcentral.com/articles/10.1186/1471-2288-13-91, opened 25 Nov 2026).
    Table 1 (airway hyper-responsiveness before and after stem cell transplantation, 21 children): the two
    disagreement cells are 1 and 7. The text prints asymptotic p = 0.034 and exact p = 0.070.
    Table 2 (complete response before and after consolidation, 161 patients): the disagreement cells are 6 and
    16. The text prints asymptotic p = 0.033 and exact p = 0.053.
    "Asymptotic" is the chi-square without a correction. Table 1 and the asymptotic value of Table 2 hold to the
    printed three decimals. The exact value of Table 2 does not: this function gives 0.052479 (2 * 110056 /
    2**22), which prints as 0.052, one unit in the last place below the paper's 0.053. Why the paper has 0.053
    was not checked; the test holds the value to within 0.001. The paper's text prints no mid-p for these
    tables, so the mid-p values here are not checked against it (the paper defines mid-p as the exact p-value
    minus half the point probability of the observed result, which is what the brute-force test above uses)."""
    t1, t2 = mcnemar(1, 7), mcnemar(6, 16)
    assert round(t1["chi2_p"], 3) == 0.034 and round(t1["exact_p"], 3) == 0.070
    assert round(t2["chi2_p"], 3) == 0.033
    assert t2["exact_p"] == pytest.approx(2 * 110056 / 2 ** 22) and abs(t2["exact_p"] - 0.053) < 1e-3


def test_mcnemar_with_no_disagreements_says_there_is_no_evidence():
    assert mcnemar(0, 0) == {"b": 0, "c": 0, "discordant": 0, "exact_p": 1.0, "mid_p": 1.0,
                             "chi2": 0.0, "chi2_p": 1.0, "chi2_cc": 0.0, "chi2_cc_p": 1.0}


def test_mcnemar_even_split_and_the_continuity_correction_floor():
    even = mcnemar(5, 5)
    assert even["exact_p"] == even["mid_p"] == even["chi2_p"] == even["chi2_cc_p"] == 1.0
    one = mcnemar(0, 1)                                    # one disagreement: |b - c| - 1 = 0, so no statistic
    assert one["exact_p"] == 1.0 and one["mid_p"] == 0.5 and one["chi2"] == 1.0 and one["chi2_cc"] == 0.0


def test_mcnemar_refuses_negative_counts():
    for b, c in ((-1, 3), (3, -1)):
        with pytest.raises(ValueError, match="b >= 0"):
            mcnemar(b, c)


COUNTS = st.integers(min_value=0, max_value=300)


@given(COUNTS, COUNTS)
def test_mcnemar_is_symmetric_and_its_p_values_are_ordered(b, c):
    out, flipped = mcnemar(b, c), mcnemar(c, b)
    for key in ("discordant", "exact_p", "mid_p", "chi2", "chi2_p", "chi2_cc", "chi2_cc_p"):
        assert out[key] == flipped[key]
    assert 0 <= out["mid_p"] <= out["exact_p"] <= 1
    assert out["exact_p"] == sign_test(b, c)
    assert out["chi2_cc"] <= out["chi2"] and out["chi2_p"] <= out["chi2_cc_p"] <= 1
    if b == c:
        assert out["mid_p"] == 1.0
    elif b + c:
        pmf = binomial_half_pmf(b + c)
        assert out["mid_p"] == pytest.approx(float(2 * sum(pmf[: min(b, c) + 1]) - pmf[min(b, c)]), abs=1e-12)
