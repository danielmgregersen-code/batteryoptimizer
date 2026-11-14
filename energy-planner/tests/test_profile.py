from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from loadprofile import build_profile, load_for_hour

TZ = ZoneInfo("Europe/Copenhagen")
UTC = timezone.utc


def rows(start, hours, fn):
    return [{"start": (start + timedelta(hours=i)).timestamp() * 1000, "mean": fn(start + timedelta(hours=i))}
            for i in range(hours)]


def test_profile_subtracts_car_and_ignores_outliers():
    start = datetime(2026, 10, 19, 0, tzinfo=UTC)  # a Monday
    load = rows(start, 24 * 14, lambda t: 1000.0)
    car = rows(start, 24 * 14, lambda t: 11000.0 if t.hour == 0 and t.day == 20 else 0.0)
    # the car hour would otherwise be 12 kW; with the car removed the base load stays 1 kW
    load[1]["mean"] = 12000.0
    prof = build_profile(load, car, TZ, default_kwh=0.5)
    assert prof["hours_of_data"] == 24 * 14
    assert prof["table"][(False, 14)] == pytest.approx(1.0)


def test_profile_falls_back_to_default_without_data():
    prof = build_profile([], [], TZ, default_kwh=0.8)
    assert prof["table"][(True, 3)] == pytest.approx(0.8)
    assert load_for_hour(prof, datetime(2026, 11, 14, 12, tzinfo=UTC), TZ) == pytest.approx(0.8)


def test_profile_separates_weekend_from_weekday():
    start = datetime(2026, 10, 19, 0, tzinfo=UTC)
    load = rows(start, 24 * 28, lambda t: 2000.0 if t.astimezone(TZ).weekday() >= 5 else 500.0)
    prof = build_profile(load, [], TZ, 0.8)
    assert prof["table"][(False, 12)] == pytest.approx(0.5)
    assert prof["table"][(True, 12)] == pytest.approx(2.0)


def test_rows_without_mean_are_ignored():
    prof = build_profile([{"start": 0, "mean": None}], None, TZ, 0.8)
    assert prof["hours_of_data"] == 0


def test_trimmed_mean_counts_ordinary_peaks_but_drops_extremes():
    from loadprofile import trimmed_mean
    vals = [0.5] * 14 + [2.0] * 5 + [12.0]        # 5 cooking hours, one missed car hour
    assert trimmed_mean(vals) == pytest.approx((0.5 * 11 + 2.0 * 3) / 14)   # 3 cut at each end
    assert trimmed_mean(vals) > 0.5               # the median (0.5) would ignore the cooking
