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


def _night_charging_rows(include_in_load: bool, days=21):
    """Base load 0.4 kW at night; the battery charges 4 kW from the grid 02-05 every night."""
    start = datetime(2026, 10, 20, 0, tzinfo=TZ).astimezone(UTC)
    load, bat, pv, car = [], [], [], []
    for i in range(24 * days):
        t = start + timedelta(hours=i)
        hl = t.astimezone(TZ).hour
        charging = 2 <= hl < 5
        b = 4000.0 if charging else 0.0
        sun = 2000.0 if 10 <= hl < 14 else 0.0
        base = 400.0 if hl < 6 else 900.0
        ms = t.timestamp() * 1000
        load.append({"start": ms, "mean": base + (b if include_in_load else 0.0)})
        bat.append({"start": ms, "mean": b})
        pv.append({"start": ms, "mean": sun})
        car.append({"start": ms, "mean": 0.0})
    return load, bat, pv, car


def test_battery_charging_in_house_load_is_detected_and_removed():
    load, bat, pv, car = _night_charging_rows(include_in_load=True)
    prof = build_profile(load, car, TZ, 0.8, bat_rows=bat, pv_rows=pv, battery_mode="auto")
    d = prof["diagnostics"]
    assert d["battery_detection"]["included"] is True and d["battery_subtracted"]
    assert d["battery_detection"]["base_while_charging_kw"] == pytest.approx(4.4)
    assert prof["table"][(False, 3)] == pytest.approx(0.4)          # not 4.4 kW at 03:00


def test_house_load_without_battery_charging_is_left_alone():
    load, bat, pv, car = _night_charging_rows(include_in_load=False)
    prof = build_profile(load, car, TZ, 0.8, bat_rows=bat, pv_rows=pv, battery_mode="auto")
    assert prof["diagnostics"]["battery_detection"]["included"] is False
    assert not prof["diagnostics"]["battery_subtracted"]
    assert prof["table"][(False, 3)] == pytest.approx(0.4)


def test_battery_mode_override():
    load, bat, pv, car = _night_charging_rows(include_in_load=True)
    prof = build_profile(load, car, TZ, 0.8, bat_rows=bat, pv_rows=pv)              # default "no"
    assert prof["table"][(False, 3)] == pytest.approx(4.4)
    assert prof["diagnostics"]["battery_detection"]["included"] is True   # still reported


def test_detection_needs_enough_night_hours():
    load, bat, pv, car = _night_charging_rows(include_in_load=True, days=1)
    prof = build_profile(load[:4], car[:4], TZ, 0.8, bat_rows=bat[:4], pv_rows=pv[:4], battery_mode="auto")
    assert prof["diagnostics"]["battery_detection"]["included"] is None
    assert not prof["diagnostics"]["battery_subtracted"]


def test_without_car_statistics_car_hours_are_left_out():
    """The THOR has no statistics before its state_class was set: >4 kW hours are taken as the car."""
    start = datetime(2026, 10, 19, 0, tzinfo=UTC)
    load = rows(start, 24 * 14, lambda t: 11500.0 if t.astimezone(TZ).hour == 3 and t.day % 2 else 500.0)
    prof = build_profile(load, [], TZ, 0.8)
    assert prof["table"][(False, 3)] == pytest.approx(0.5)          # not (11.5 + 0.5) / 2
    assert prof["diagnostics"]["car_excluded_hours"] == 8           # odd days 19.10-1.11


def test_car_statistics_win_over_the_rule_of_thumb():
    start = datetime(2026, 10, 19, 0, tzinfo=UTC)
    load = rows(start, 24 * 14, lambda t: 4500.0 if t.astimezone(TZ).hour == 18 else 500.0)  # a real 4.5 kW hour
    car = rows(start, 24 * 14, lambda t: 0.0)                                                # car data says: not the car
    prof = build_profile(load, car, TZ, 0.8)
    assert prof["table"][(False, 18)] == pytest.approx(4.5) and prof["diagnostics"]["car_excluded_hours"] == 0


def test_car_energy_counter_is_converted_to_average_power():
    """sensor.ioniq_9_ladeeffekt may be a kWh counter: its hourly change is the average kW."""
    start = datetime(2026, 10, 19, 0, tzinfo=UTC)
    load = rows(start, 24 * 14, lambda t: 11500.0 if t.astimezone(TZ).hour == 3 else 500.0)
    car = [{"start": r["start"], "change": 11.0 if r["mean"] > 5000 else 0.0} for r in load]
    prof = build_profile(load, car, TZ, 0.8)
    assert prof["diagnostics"]["car_kind"] == "energy" and prof["diagnostics"]["car_hours"] == 14
    assert prof["table"][(False, 3)] == pytest.approx(0.5)


def test_missing_car_statistics_blocks_detection_and_is_not_mistaken_for_battery():
    """Car and battery both charge in the cheapest night hours. Without the car's statistics the car
    looks like battery charging in the house load - the detection must not draw that conclusion."""
    load, bat, pv, _ = _night_charging_rows(include_in_load=False)
    for r, b in zip(load, bat):
        if b["mean"] > 0:
            r["mean"] += 11000.0          # the car charges in the same hours, car sensor has no statistics
    prof = build_profile(load, [], TZ, 0.8, bat_rows=bat, pv_rows=pv, battery_mode="auto")
    det = prof["diagnostics"]["battery_detection"]
    assert det["included"] is None and det["reason"] == "no_car_statistics"
    assert not prof["diagnostics"]["battery_subtracted"]
    assert prof["diagnostics"]["car_rows"] == 0
