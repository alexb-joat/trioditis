import math
from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from trioditis_metrics.stats import (
    _rejection_cutoff,
    beta_mean,
    cohen_kappa,
    discordant_needed,
    ewma,
    ewma_chart,
    mcnemar,
    paired_power,
    quantile,
    ratio_estimate,
    sign_test,
    sign_test_power,
    stratified_proportion,
    stratified_sample,
    stratum_allocation,
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


# ---------------------------------------------------------------- power and the disagreements needed

@pytest.mark.parametrize("alpha", [0.05, 0.01, 0.10])
def test_the_rejection_region_is_exactly_where_sign_test_rejects(alpha):
    """For every number of disagreements m up to 200: the splits k (of m) that `sign_test(k, m - k)` calls
    significant at alpha are exactly k <= cutoff and k >= m - cutoff, where the cutoff comes from one pass over
    whole-number counts and never calls sign_test."""
    for m in range(201):
        cutoff = _rejection_cutoff(m, alpha)
        by_sign_test = {k for k in range(m + 1) if sign_test(k, m - k) < alpha}
        assert by_sign_test == {k for k in range(m + 1) if k <= cutoff or k >= m - cutoff}, m


def test_power_matches_the_three_figures_the_study_plan_printed_before_its_data_existed():
    """docs/route-accuracy.md, "What this n can show" (written 13 Nov 2026, before the held-out prompts were
    labelled): 115 disagreements find a 65:35 split with 89% and a 60:40 split with 54%; 80 find a 65:35 split
    with 72%. Held here to the printed whole percent."""
    assert round(100 * sign_test_power(115, 0.65)) == 89
    assert round(100 * sign_test_power(115, 0.60)) == 54
    assert round(100 * sign_test_power(80, 0.65)) == 72
    assert [round(sign_test_power(115, s), 3) for s in (0.65, 0.60)] + [round(sign_test_power(80, 0.65), 3)] == [0.888, 0.54, 0.724]


def test_power_is_a_saw_tooth_not_a_smooth_curve():
    """At share 0.65, 114 disagreements give 0.902 and 115 give 0.888: one more disagreement, less power."""
    assert round(sign_test_power(114, 0.65), 3) == 0.902 and round(sign_test_power(115, 0.65), 3) == 0.888


def test_power_matches_values_computed_with_two_separate_implementations():
    """Computed on 25 Nov 2026 by the brief's author with two separate implementations (not this one); held to
    the four decimals printed."""
    assert round(sign_test_power(48, 0.65), 4) == 0.4699 and round(sign_test_power(48, 0.60), 4) == 0.2146
    assert round(paired_power(315, 115 / 315, 0.65), 4) == 0.8855
    assert round(paired_power(100, 0.48, 0.65), 4) == 0.5018


def test_disagreements_needed_for_80_percent_power():
    """Same source as above: at share 0.65 the first count with 80% power is 90 and from 97 on every count has
    it; at share 0.60 the first is 199 and from 210 on."""
    assert discordant_needed(0.65) == (90, 97)
    assert discordant_needed(0.60) == (199, 210)
    first, stable = discordant_needed(0.65)
    assert sign_test_power(first, 0.65) >= 0.8 > sign_test_power(first - 1, 0.65)
    assert sign_test_power(stable - 1, 0.65) < 0.8 and all(sign_test_power(m, 0.65) >= 0.8 for m in range(stable, 400))


def test_disagreements_needed_says_none_when_the_limit_does_not_reach_the_power():
    assert discordant_needed(0.5, limit=300) == (None, None)             # no real difference: it never gets there
    assert discordant_needed(0.60, limit=150) == (None, None)
    first, stable = discordant_needed(0.60, limit=205)                   # reached at 199, but not yet to stay
    assert first == 199 and stable is None


def test_power_with_the_exact_fraction_enumeration_of_a_small_case():
    """m = 10 disagreements, share 7/10, alpha 0.05, worked as exact fractions: the test rejects when the first
    setting wins k <= 1 or k >= 9 of the 10 (2 * 11 / 1024 = 0.0215 < 0.05, but 2 * 56 / 1024 = 0.109 is not)."""
    share = Fraction(7, 10)
    want = sum(math.comb(10, k) * share ** k * (1 - share) ** (10 - k) for k in (0, 1, 9, 10))
    assert sign_test_power(10, 0.7) == pytest.approx(float(want), abs=1e-12)
    # by hand: 0.3**10 + 10 * 0.7 * 0.3**9 + 10 * 0.7**9 * 0.3 + 0.7**10
    #        = 0.0000059049 + 0.0001377810 + 0.1210608210 + 0.0282475249 = 0.1494520318
    assert float(want) == pytest.approx(0.1494520318, abs=1e-10)
    # a rate of 1/2 over 10 rows: the number of disagreements m is Binomial(10, 1/2); weigh each m's power
    exact = sum(Fraction(math.comb(10, m), 2 ** 10) * sum(
        math.comb(m, k) * share ** k * (1 - share) ** (m - k) for k in range(m + 1) if sign_test(k, m - k) < 0.05)
        for m in range(11))
    assert paired_power(10, 0.5, 0.7) == pytest.approx(float(exact), abs=1e-12)


def test_the_log_space_sum_matches_a_plain_sum_of_binomial_terms():
    """For 115 disagreements the plain terms C(115, k) * 0.65**k * 0.35**(115 - k) fit a float, so they check
    the logarithm version."""
    plain = sum(math.comb(115, k) * 0.65 ** k * 0.35 ** (115 - k) for k in range(116) if sign_test(k, 115 - k) < 0.05)
    assert sign_test_power(115, 0.65) == pytest.approx(plain, abs=1e-12)


def test_thousands_of_disagreements_do_not_overflow():
    """C(5000, 2500) is about 10**1503, far beyond a float; the log-space terms stay finite. At share 0.65 the
    test is all but certain to reject; at 0.5 its chance is the test's size, never above alpha."""
    assert sign_test_power(5000, 0.65) == pytest.approx(1.0, abs=1e-12)
    assert 0.0 < sign_test_power(5000, 0.5) <= 0.05
    assert 0.0 < paired_power(1000, 0.5, 0.55) <= 1.0                    # 0.7 s on 25 Nov 2026; n in the thousands takes seconds


def test_power_at_the_edges():
    assert sign_test_power(0, 0.65) == 0.0                                # nothing to test
    assert sign_test_power(5, 0.99) == 0.0                                # five straight is p = 0.0625: it cannot reject
    assert sign_test_power(6, 1.0) == sign_test_power(6, 0.0) == 1.0      # six straight is p = 0.03125, and it happens
    assert paired_power(50, 0.0, 0.65) == 0.0 and paired_power(0, 0.4, 0.65) == 0.0
    assert paired_power(115, 1.0, 0.65) == pytest.approx(sign_test_power(115, 0.65))


def test_power_refuses_nonsense():
    for bad in ((-1, 0.6, 0.05), (10, 1.2, 0.05), (10, -0.1, 0.05), (10, 0.6, 0.0), (10, 0.6, 1.0)):
        with pytest.raises(ValueError):
            sign_test_power(*bad)
    with pytest.raises(ValueError):
        paired_power(10, 1.5, 0.6)
    with pytest.raises(ValueError):
        paired_power(-1, 0.5, 0.6)
    with pytest.raises(ValueError):
        discordant_needed(0.6, power=0.0)


@given(st.integers(min_value=0, max_value=400), st.floats(min_value=0.0, max_value=1.0))
def test_power_is_a_probability_symmetric_in_the_share(m, share):
    power = sign_test_power(m, share)
    assert 0.0 <= power <= 1.0
    assert power == pytest.approx(sign_test_power(m, 1 - share), abs=1e-9)


@given(st.integers(min_value=0, max_value=400))
def test_with_no_real_difference_the_test_rejects_at_most_alpha_of_the_time(m):
    assert sign_test_power(m, 0.5) <= 0.05


# ---------------------------------------------------------------- stratified sampling by route

def test_allocation_by_largest_remainder_worked_by_hand():
    """Strata of 10, 30 and 60 rows (N = 100), a sample of 7. The exact quotas are 0.7, 2.1 and 4.2, so the
    whole parts are 0, 2 and 4 (6 rows) and the one row left goes to the largest remainder, 0.7: stratum a."""
    assert stratum_allocation({"a": 10, "b": 30, "c": 60}, 7) == {"a": 1, "b": 2, "c": 4}
    # Three strata of 5, a sample of 10: quotas 3.33 each, whole parts 3, 3, 3; the one row left goes to a tie, so to
    # the name that sorts first. With two rows left (a sample of 11: quotas 3.67), the first two names take them.
    assert stratum_allocation({"z": 5, "y": 5, "x": 5}, 10) == {"x": 4, "y": 3, "z": 3}
    assert stratum_allocation({"z": 5, "y": 5, "x": 5}, 11) == {"x": 4, "y": 4, "z": 3}
    assert stratum_allocation({"b": 1, "a": 1}, 1) == {"a": 1, "b": 0}
    assert stratum_allocation({"code": 25, "reasoning": 25, "writing": 25, "quick": 25}, 20) == dict.fromkeys(
        ("code", "reasoning", "writing", "quick"), 5)
    assert stratum_allocation({"a": 3, "b": 4}, 0) == {"a": 0, "b": 0}
    assert stratum_allocation({"a": 3, "b": 4}, 7) == {"a": 3, "b": 4}


def test_allocation_refuses_a_sample_larger_than_the_population():
    with pytest.raises(ValueError, match="size"):
        stratum_allocation({"a": 3, "b": 4}, 8)
    with pytest.raises(ValueError):
        stratum_allocation({"a": 3}, -1)


@given(st.dictionaries(st.text(min_size=1, max_size=3), st.integers(min_value=0, max_value=200), min_size=1, max_size=8),
       st.data())
def test_allocation_adds_up_stays_within_each_stratum_and_is_a_floor_or_a_ceiling(sizes, data):
    total = sum(sizes.values())
    size = data.draw(st.integers(min_value=0, max_value=total))
    got = stratum_allocation(sizes, size)
    assert sum(got.values()) == size and set(got) == set(sizes)
    for name, n in sizes.items():
        assert 0 <= got[name] <= n
        if total:
            assert size * n // total <= got[name] <= -(-size * n // total)


def test_a_stratified_sample_keeps_the_allocation_and_repeats_with_its_seed():
    items = [("code", i) for i in range(10)] + [("writing", i) for i in range(30)] + [("quick", i) for i in range(60)]
    sample = stratified_sample(items, key=lambda item: item[0], size=7, seed=3)
    assert len(sample) == 7 and len(set(sample)) == 7 and set(sample) <= set(items)       # a subset, no repeats
    counts = {name: sum(item[0] == name for item in sample) for name in ("code", "writing", "quick")}
    assert counts == stratum_allocation({"code": 10, "writing": 30, "quick": 60}, 7)       # 1, 2 and 4
    assert sample == stratified_sample(items, key=lambda item: item[0], size=7, seed=3)    # same seed, same sample
    assert [items.index(s) for s in sample] == sorted(items.index(s) for s in sample)      # original order kept
    assert any(stratified_sample(items, key=lambda item: item[0], size=7, seed=s) != sample for s in range(4, 12))


def test_a_stratified_sample_of_everything_or_nothing_and_too_many():
    items = list(range(12))
    assert stratified_sample(items, key=lambda x: x % 3, size=12) == items
    assert stratified_sample(items, key=lambda x: x % 3, size=0) == []
    with pytest.raises(ValueError):
        stratified_sample(items, key=lambda x: x % 3, size=13)


POPULATION = {"a": [1, 0, 0, 0], "b": [1, 1, 1, 0, 0]}        # two strata of 4 and 5 units, made up


def exact_estimate_and_variance(samples):
    """The stratified estimate and its estimated variance as exact fractions, from the definitions."""
    total = sum(len(v) for v in POPULATION.values())
    estimate, variance = Fraction(0), Fraction(0)
    for name, outcomes in samples.items():
        big_n, n = len(POPULATION[name]), len(outcomes)
        mean = Fraction(sum(outcomes), n)
        s2 = sum((x - mean) ** 2 for x in outcomes) / (n - 1)
        estimate += Fraction(big_n, total) * mean
        variance += Fraction(big_n, total) ** 2 * (1 - Fraction(n, big_n)) * s2 / n
    return estimate, variance


def test_the_stratified_estimate_and_its_variance_are_exactly_unbiased_on_a_tiny_population():
    """Strata of 4 and 5 units, samples of 2 and 2: C(4, 2) * C(5, 2) = 60 equally likely samples. Over all of
    them, as exact fractions: the mean estimate is the population proportion (4/9); the variance of the
    estimates is sum of W_h^2 * (1 - n_h/N_h) * S_h^2 / n_h with the population variance S_h^2 (divisor N_h - 1);
    and the mean of the estimated variances is that same number. Also, for each of the 60 samples the function's
    float estimate and standard error equal the fractions'."""
    import itertools
    total = sum(map(len, POPULATION.values()))
    truth = Fraction(sum(map(sum, POPULATION.values())), total)
    assert truth == Fraction(4, 9)
    estimates, variances = [], []
    for take_a in itertools.combinations(POPULATION["a"], 2):
        for take_b in itertools.combinations(POPULATION["b"], 2):
            samples = {"a": list(take_a), "b": list(take_b)}
            estimate, variance = exact_estimate_and_variance(samples)
            got = stratified_proportion(samples, {k: len(v) for k, v in POPULATION.items()})
            assert got["estimate"] == pytest.approx(float(estimate), abs=1e-12)
            assert got["se"] == pytest.approx(math.sqrt(variance), abs=1e-12)
            estimates.append(estimate)
            variances.append(variance)
    assert len(estimates) == 60
    mean_estimate = sum(estimates) / 60
    assert mean_estimate == truth
    true_variance = sum(Fraction(len(POPULATION[name]), total) ** 2 * (1 - Fraction(2, len(POPULATION[name])))
                        * population_variance(POPULATION[name]) / 2 for name in POPULATION)
    assert sum((e - mean_estimate) ** 2 for e in estimates) / 60 == true_variance
    assert sum(variances) / 60 == true_variance


def population_variance(values):
    """S^2 of a finite population: squared deviations over N - 1 (not N)."""
    mean = Fraction(sum(values), len(values))
    return sum((v - mean) ** 2 for v in values) / (len(values) - 1)


def test_the_stratified_proportion_by_hand():
    """Two strata of 40 and 60 rows. 4 of 10 sampled in the first, 3 of 10 in the second (n_h = 10 each).
    Estimate = 0.4 * 0.4 + 0.6 * 0.3 = 0.34. Sample variances s_h^2 = 10 * p * (1 - p) / 9 = 0.2666... and 0.2333...
    Variance = 0.4^2 * (1 - 10/40) * 0.26667 / 10 + 0.6^2 * (1 - 10/60) * 0.23333 / 10
             = 0.16 * 0.75 * 0.026667 + 0.36 * 0.83333 * 0.023333 = 0.0032 + 0.007 = 0.0102; se = 0.100995."""
    out = stratified_proportion({"a": [1] * 4 + [0] * 6, "b": [1] * 3 + [0] * 7}, {"a": 40, "b": 60})
    assert out["estimate"] == pytest.approx(0.34) and out["se"] == pytest.approx(math.sqrt(0.0102))
    assert out["low"] == pytest.approx(0.34 - 1.959964 * 0.100995, abs=1e-5) and out["n"] == 20
    assert out["high"] == pytest.approx(0.34 + 1.959964 * 0.100995, abs=1e-5)


def test_the_stratified_interval_stays_inside_0_and_1_and_a_census_has_no_error():
    out = stratified_proportion({"a": [1, 1, 1], "b": [1, 1, 0]}, {"a": 100, "b": 100})
    assert 0.0 <= out["low"] <= out["estimate"] <= out["high"] <= 1.0
    everything = stratified_proportion({"a": [1, 0, 0, 0], "b": [1, 1, 1, 0, 0]}, {"a": 4, "b": 5})
    assert everything["estimate"] == pytest.approx(4 / 9) and everything["se"] == 0.0
    hot = stratified_proportion({"a": [1] * 9 + [0], "b": [1] * 10}, {"a": 50, "b": 50})
    assert hot["high"] == 1.0


def test_one_sampled_row_in_a_stratum_gives_an_estimate_and_no_standard_error():
    out = stratified_proportion({"a": [1], "b": [1, 0, 0]}, {"a": 10, "b": 10})
    assert out["estimate"] == pytest.approx(0.5 * 1 + 0.5 * (1 / 3))
    assert out["se"] is None and out["low"] is None and out["high"] is None


def test_the_stratified_proportion_refuses_a_sample_that_does_not_fit_its_population():
    with pytest.raises(ValueError, match="same strata"):
        stratified_proportion({"a": [1, 0]}, {"a": 10, "b": 10})
    with pytest.raises(ValueError, match="stratum 'a'"):
        stratified_proportion({"a": []}, {"a": 10})
    with pytest.raises(ValueError, match="stratum 'a'"):
        stratified_proportion({"a": [1, 0, 1]}, {"a": 2})
    with pytest.raises(ValueError, match="stratum 'a'"):
        stratified_proportion({"a": [1, 2]}, {"a": 10})
