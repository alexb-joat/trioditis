import pytest

from trioditis_metrics.stats import beta_mean, ewma, ewma_chart, quantile, wilson


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
