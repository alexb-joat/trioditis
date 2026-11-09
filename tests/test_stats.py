import pytest

from trioditis_metrics.stats import beta_mean, ewma, ewma_chart, quantile, ratio_estimate, wilson


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
