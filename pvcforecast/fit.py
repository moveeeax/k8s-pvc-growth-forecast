"""Least-squares growth fit, resize handling and days-to-full classification."""

from __future__ import annotations

from typing import List, NamedTuple, Optional, Tuple

from .prom import Sample, SeriesKey, VolumeSeries

STATUS_CRITICAL = "critical"
STATUS_WARNING = "warning"
STATUS_OK = "ok"
STATUS_STABLE = "stable"
STATUS_LOW_CONFIDENCE = "low-confidence"
STATUS_NO_DATA = "no-data"

#: Severity order used for sorting and for ``--fail-on`` comparisons.
SEVERITY = {
    STATUS_CRITICAL: 4,
    STATUS_WARNING: 3,
    STATUS_OK: 2,
    STATUS_LOW_CONFIDENCE: 1,
    STATUS_STABLE: 0,
    STATUS_NO_DATA: 0,
}

#: Anything further out than this is capacity planning fiction, not a forecast.
DEFAULT_HORIZON_DAYS = 3650.0

MIN_SAMPLES = 3

SECONDS_PER_DAY = 86400.0


class Fit(NamedTuple):
    slope_per_day: float
    intercept: float
    r2: float
    samples: int


class Forecast(NamedTuple):
    key: SeriesKey
    status: str
    used_bytes: Optional[float]
    capacity_bytes: Optional[float]
    free_bytes: Optional[float]
    growth_bytes_per_day: Optional[float]
    r2: Optional[float]
    days_to_full: Optional[float]
    samples: int
    resized_at: Optional[float]
    note: str

    @property
    def used_percent(self) -> Optional[float]:
        if not self.capacity_bytes or self.used_bytes is None:
            return None
        return 100.0 * self.used_bytes / self.capacity_bytes


def linear_fit(points: List[Sample]) -> Fit:
    """Ordinary least squares of value against time, with time measured in days.

    Time is re-based on the first sample so the intercept stays in byte territory and the
    slope reads directly as bytes per day.
    """
    n = len(points)
    if n < 2:
        raise ValueError("need at least two points to fit a line")
    t0 = points[0][0]
    xs = [(ts - t0) / SECONDS_PER_DAY for ts, _ in points]
    ys = [value for _, value in points]
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx == 0.0:
        # Every sample shares a timestamp; no slope is defined.
        return Fit(0.0, mean_y, 0.0, n)
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x
    sst = sum((y - mean_y) ** 2 for y in ys)
    if sst == 0.0:
        # A perfectly flat series is perfectly explained by a flat line.
        return Fit(slope, intercept, 1.0, n)
    ssr = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
    r2 = 1.0 - ssr / sst
    if r2 < 0.0:
        r2 = 0.0
    return Fit(slope, intercept, r2, n)


def last_capacity_change(capacity: List[Sample]) -> Tuple[Optional[float], Optional[float]]:
    """Return ``(capacity_now, timestamp_of_last_change)`` for a capacity series.

    A PVC expand shows up as a step in ``kubelet_volume_stats_capacity_bytes``. Everything
    before that step belongs to a different volume size, so the caller refits after it.
    """
    if not capacity:
        return None, None
    capacity_now = capacity[-1][1]
    change_ts = None
    for index in range(1, len(capacity)):
        previous = capacity[index - 1][1]
        current = capacity[index][1]
        if current != previous:
            change_ts = capacity[index][0]
    return capacity_now, change_ts


def forecast_series(
    series: VolumeSeries,
    warning_days: float,
    critical_days: float,
    min_r2: float,
    horizon_days: float = DEFAULT_HORIZON_DAYS,
) -> Forecast:
    """Fit one PVC and classify it."""
    key = series.key
    capacity_now, resized_at = last_capacity_change(series.capacity)

    if capacity_now is None or capacity_now <= 0:
        return Forecast(
            key, STATUS_NO_DATA, None, None, None, None, None, None, 0, None,
            "no capacity series (kubelet_volume_stats_capacity_bytes missing for this claim)",
        )

    used_points = series.used
    if resized_at is not None:
        after = [point for point in used_points if point[0] >= resized_at]
        if len(after) >= MIN_SAMPLES:
            used_points = after
        else:
            used_bytes = used_points[-1][1] if used_points else None
            free = capacity_now - used_bytes if used_bytes is not None else None
            return Forecast(
                key, STATUS_NO_DATA, used_bytes, capacity_now, free, None, None, None,
                len(after), resized_at,
                "resized {} samples ago; not enough post-resize data to fit".format(len(after)),
            )

    if len(used_points) < MIN_SAMPLES:
        used_bytes = used_points[-1][1] if used_points else None
        free = capacity_now - used_bytes if used_bytes is not None else None
        return Forecast(
            key, STATUS_NO_DATA, used_bytes, capacity_now, free, None, None, None,
            len(used_points), resized_at,
            "only {} used-bytes samples in the window".format(len(used_points)),
        )

    fit = linear_fit(used_points)
    used_bytes = used_points[-1][1]
    free_bytes = max(capacity_now - used_bytes, 0.0)
    growth = fit.slope_per_day
    if resized_at is not None:
        note = "refit on the {} samples after the resize at {}".format(
            len(used_points), _iso_hint(resized_at)
        )
    else:
        note = ""

    if growth <= 0.0:
        return Forecast(
            key, STATUS_STABLE, used_bytes, capacity_now, free_bytes, growth, fit.r2, None,
            fit.samples, resized_at, note or "flat or shrinking",
        )

    days_to_full = free_bytes / growth
    if days_to_full > horizon_days:
        return Forecast(
            key, STATUS_STABLE, used_bytes, capacity_now, free_bytes, growth, fit.r2, days_to_full,
            fit.samples, resized_at,
            note or "growth too slow to matter (>{:.0f} days)".format(horizon_days),
        )

    if fit.r2 < min_r2:
        return Forecast(
            key, STATUS_LOW_CONFIDENCE, used_bytes, capacity_now, free_bytes, growth, fit.r2,
            days_to_full, fit.samples, resized_at,
            note or "R2 {:.2f} below --min-r2 {:.2f}; treat the forecast as a hint".format(fit.r2, min_r2),
        )

    if days_to_full <= critical_days:
        status = STATUS_CRITICAL
    elif days_to_full <= warning_days:
        status = STATUS_WARNING
    else:
        status = STATUS_OK
    return Forecast(
        key, status, used_bytes, capacity_now, free_bytes, growth, fit.r2, days_to_full,
        fit.samples, resized_at, note,
    )


def _iso_hint(ts: Optional[float]) -> str:
    if ts is None:  # pragma: no cover - guarded by caller
        return "unknown"
    import datetime

    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def forecast_all(
    series: List[VolumeSeries],
    warning_days: float,
    critical_days: float,
    min_r2: float,
    horizon_days: float = DEFAULT_HORIZON_DAYS,
) -> List[Forecast]:
    """Forecast every PVC, soonest-to-fill first."""
    forecasts = [
        forecast_series(item, warning_days, critical_days, min_r2, horizon_days) for item in series
    ]

    def sort_key(forecast: Forecast):
        has_days = forecast.days_to_full is not None and forecast.status != STATUS_STABLE
        return (
            0 if has_days else 1,
            forecast.days_to_full if has_days else 0.0,
            -SEVERITY[forecast.status],
            str(forecast.key),
        )

    forecasts.sort(key=sort_key)
    return forecasts


def worst_status(forecasts: List[Forecast]) -> str:
    """Highest severity present, ignoring rows that carry no actionable signal."""
    worst = STATUS_STABLE
    for forecast in forecasts:
        if SEVERITY[forecast.status] > SEVERITY[worst]:
            worst = forecast.status
    return worst


def should_fail(forecasts: List[Forecast], fail_on: str) -> bool:
    """``--fail-on`` gate. Low-confidence and stable rows never trip it."""
    if fail_on == "never":
        return False
    threshold = SEVERITY[STATUS_CRITICAL] if fail_on == "critical" else SEVERITY[STATUS_WARNING]
    for forecast in forecasts:
        if forecast.status in (STATUS_LOW_CONFIDENCE, STATUS_STABLE, STATUS_NO_DATA):
            continue
        if SEVERITY[forecast.status] >= threshold:
            return True
    return False
