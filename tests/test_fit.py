"""Fit and classification behaviour, checked against the committed fixtures."""

from __future__ import annotations

import os

import pytest

from pvcforecast.fit import (
    STATUS_LOW_CONFIDENCE,
    STATUS_NO_DATA,
    STATUS_OK,
    STATUS_STABLE,
    STATUS_WARNING,
    forecast_all,
    forecast_series,
    last_capacity_change,
    linear_fit,
    should_fail,
)
from pvcforecast.prom import SeriesKey, VolumeSeries, load_fixture

GiB = 1024 ** 3
FIXTURE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "testdata", "range.json")


@pytest.fixture(scope="module")
def by_claim():
    series = load_fixture(FIXTURE)
    return {item.key.claim: item for item in series}


@pytest.fixture(scope="module")
def forecasts(by_claim):
    series = list(by_claim.values())
    return {item.key.claim: item for item in forecast_all(series, 30.0, 7.0, 0.5)}


def test_linear_fit_recovers_a_known_slope():
    points = [(1000.0 + i * 3600.0, 10.0 * GiB + i * GiB / 24.0) for i in range(48)]
    fit = linear_fit(points)
    assert fit.slope_per_day == pytest.approx(GiB, rel=1e-9)
    assert fit.r2 == pytest.approx(1.0, abs=1e-9)
    assert fit.samples == 48


def test_linear_fit_of_a_flat_series_is_flat_and_explained():
    points = [(float(i), 5.0 * GiB) for i in range(10)]
    fit = linear_fit(points)
    assert fit.slope_per_day == pytest.approx(0.0, abs=1e-6)
    assert fit.r2 == 1.0


def test_linear_fit_needs_two_points():
    with pytest.raises(ValueError):
        linear_fit([(0.0, 1.0)])


def test_steady_growth_forecast_is_ten_days(forecasts):
    item = forecasts["postgres-data"]
    assert item.status == STATUS_WARNING
    assert item.days_to_full == pytest.approx(10.0, abs=0.5)
    assert item.growth_bytes_per_day == pytest.approx(GiB, rel=0.02)
    assert item.r2 > 0.99


def test_resized_volume_is_refit_on_the_post_resize_segment(by_claim, forecasts):
    item = forecasts["metrics-data"]
    assert item.resized_at is not None
    assert item.capacity_bytes == 200 * GiB
    # The post-resize slope is 1 GiB/day...
    assert item.growth_bytes_per_day == pytest.approx(GiB, rel=0.05)
    assert item.samples < 169  # ...fit on a subset of the window
    assert item.status == STATUS_OK
    assert item.days_to_full == pytest.approx(161.0, rel=0.05)
    # ...and a naive whole-window fit would have reported a much steeper climb.
    naive = linear_fit(by_claim["metrics-data"].used)
    assert naive.slope_per_day > 2 * GiB


def test_flat_volume_is_stable_without_a_forecast(forecasts):
    item = forecasts["redis-cache"]
    assert item.status == STATUS_STABLE
    assert abs(item.growth_bytes_per_day) < 0.05 * GiB


def test_shrinking_volume_is_stable_and_never_critical(forecasts):
    item = forecasts["log-archive"]
    assert item.status == STATUS_STABLE
    assert item.growth_bytes_per_day < 0
    assert item.days_to_full is None


def test_noisy_series_is_low_confidence(forecasts):
    item = forecasts["scratch"]
    assert item.status == STATUS_LOW_CONFIDENCE
    assert item.r2 < 0.5
    assert item.days_to_full is not None  # the number is kept, just not trusted


def test_low_confidence_and_stable_rows_do_not_fail_the_run(by_claim):
    quiet = [by_claim[name] for name in ("scratch", "redis-cache", "log-archive")]
    assert should_fail(forecast_all(quiet, 30.0, 7.0, 0.5), "warning") is False


def test_fail_on_levels(by_claim):
    all_series = list(by_claim.values())
    forecasts = forecast_all(all_series, 30.0, 7.0, 0.5)
    assert should_fail(forecasts, "warning") is True
    assert should_fail(forecasts, "critical") is False
    assert should_fail(forecasts, "never") is False
    tight = forecast_all(all_series, 30.0, 12.0, 0.5)
    assert should_fail(tight, "critical") is True


def test_findings_are_sorted_soonest_first(by_claim):
    forecasts = forecast_all(list(by_claim.values()), 30.0, 7.0, 0.5)
    assert forecasts[0].key.claim == "postgres-data"
    assert forecasts[-1].status == STATUS_STABLE


def test_min_r2_moves_a_row_in_and_out_of_confidence(by_claim):
    lenient = forecast_all([by_claim["scratch"]], 365.0, 30.0, 0.0)[0]
    assert lenient.status != STATUS_LOW_CONFIDENCE
    strict = forecast_all([by_claim["postgres-data"]], 30.0, 7.0, 0.999999)[0]
    assert strict.status in (STATUS_WARNING, STATUS_LOW_CONFIDENCE)


def test_last_capacity_change_picks_the_latest_step():
    capacity = [(0.0, 10.0), (1.0, 10.0), (2.0, 20.0), (3.0, 20.0), (4.0, 40.0), (5.0, 40.0)]
    now, change = last_capacity_change(capacity)
    assert now == 40.0
    assert change == 4.0
    assert last_capacity_change([]) == (None, None)
    assert last_capacity_change([(0.0, 5.0), (1.0, 5.0)]) == (5.0, None)


def test_missing_capacity_series_is_no_data():
    series = VolumeSeries(SeriesKey("prod", "orphan"), used=[(0.0, 1.0), (3600.0, 2.0), (7200.0, 3.0)])
    item = forecast_series(series, 30.0, 7.0, 0.5)
    assert item.status == STATUS_NO_DATA
    assert "capacity" in item.note


def test_too_few_samples_is_no_data():
    series = VolumeSeries(
        SeriesKey("prod", "fresh"),
        used=[(0.0, 1.0 * GiB), (3600.0, 1.1 * GiB)],
        capacity=[(0.0, 10.0 * GiB), (3600.0, 10.0 * GiB)],
    )
    item = forecast_series(series, 30.0, 7.0, 0.5)
    assert item.status == STATUS_NO_DATA
    assert item.samples == 2


def test_resize_with_too_little_history_after_it_is_no_data():
    used = [(float(i) * 3600.0, (10.0 + i) * GiB) for i in range(6)]
    capacity = [(float(i) * 3600.0, (20.0 if i < 5 else 40.0) * GiB) for i in range(6)]
    item = forecast_series(VolumeSeries(SeriesKey("prod", "justgrew"), used, capacity), 30.0, 7.0, 0.5)
    assert item.status == STATUS_NO_DATA
    assert item.resized_at is not None
    assert "post-resize" in item.note


def test_used_percent_is_derived_from_the_current_capacity(forecasts):
    item = forecasts["postgres-data"]
    assert item.used_percent == pytest.approx(90.0, abs=0.5)
