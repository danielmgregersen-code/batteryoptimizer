"""Base-load profile from HA long-term statistics (hourly means).

Base load = total house load minus EV charging (and minus battery charging from the grid, if the
house-load sensor turns out to include it), so the profile describes the part of the consumption
that repeats day after day. A trimmed mean per (weekday/weekend, hour of day) drops the most
extreme hours (a car hour the charger sensor missed, a space heater) but, unlike the median, still
counts the ordinary peaks such as cooking: the plan needs the expected energy, and the median of a
right-skewed load underestimates it.

Does the house-load sensor include the battery's charging? On the Growatt SPH it does not (the
battery has its own charge-power sensor), so the default is "no". For other setups "auto" detects
it from the data (only when the car's statistics exist, see below): at night (no sun, car not charging) the base load is compared between
hours where the battery charges from the grid and hours where it is idle. If the base load is
higher by more than half the charging power, the sensor counts the charging as house load, and
`battery charge - solar` (the part of the charging the sun cannot have covered) is subtracted.
"""
import math
from datetime import datetime
from statistics import median

from inputs import parse_ts

TRIM = 0.15             # share dropped at each end
CHARGING_W = 1000.0     # battery charging at least this much counts as a charging hour
IDLE_W = 100.0          # battery (and car) below this counts as idle
NIGHT_PV_W = 150.0      # sun below this counts as night
INCLUDED_RATIO = 0.5    # base-load difference / charging power above this: charging is included
MIN_CHARGING_HOURS = 3
MIN_IDLE_HOURS = 10


def trimmed_mean(values, trim: float = TRIM) -> float:
    vals = sorted(values)
    k = int(math.floor(len(vals) * trim))
    kept = vals[k:len(vals) - k] or vals
    return sum(kept) / len(kept)


def _by_start(rows) -> dict:
    out = {}
    for r in rows or []:
        mean = r.get("mean")
        if mean is None:
            continue
        out[parse_ts(r["start"])] = float(mean)
    return out


def _power_by_start(rows) -> tuple:
    """Hourly average power in W from statistics rows: the mean of a power sensor, or the hourly
    change of an energy counter in kWh (kWh in one hour = average kW). -> ({start: W}, kind)."""
    out, kind = {}, None
    for r in rows or []:
        if r.get("mean") is not None:
            out[parse_ts(r["start"])] = float(r["mean"])
            kind = kind or "power"
        elif r.get("change") is not None:
            out[parse_ts(r["start"])] = max(0.0, float(r["change"])) * 1000.0
            kind = kind or "energy"
    return out, kind


def detect_battery_in_load(load: dict, car: dict, bat: dict, pv: dict) -> dict:
    """Compare night-time base load with and without the battery charging (see module doc)."""
    charging, idle, charge_w = [], [], []
    for start, load_w in load.items():
        if start not in bat or pv.get(start, 0.0) >= NIGHT_PV_W:
            continue
        car_w = max(0.0, car.get(start, 0.0))
        base = load_w - car_w
        b = bat[start]
        if b >= CHARGING_W:
            charging.append(base)
            charge_w.append(b)
        elif b < IDLE_W and car_w < IDLE_W:
            idle.append(base)
    out = {"night_charging_hours": len(charging), "night_idle_hours": len(idle), "included": None}
    if len(charging) < MIN_CHARGING_HOURS or len(idle) < MIN_IDLE_HOURS:
        return out
    diff = median(charging) - median(idle)
    charge = median(charge_w)
    ratio = diff / charge if charge > 0 else 0.0
    out.update({
        "base_while_charging_kw": round(median(charging) / 1000.0, 2),
        "base_while_idle_kw": round(median(idle) / 1000.0, 2),
        "charge_kw": round(charge / 1000.0, 2),
        "ratio": round(ratio, 2),
        "included": ratio > INCLUDED_RATIO,
    })
    return out


def build_profile(load_rows, car_rows, tz, default_kwh: float, bat_rows=None, pv_rows=None,
                  battery_mode: str = "no", car_detect_w: float = 4000.0) -> dict:
    """-> {"table": {(is_weekend, hour): kWh}, "hours_of_data": int, "default": kWh, "diagnostics": {...}}.

    battery_mode: "no" (default: the Growatt house load does not include battery charging),
    "auto" (detect), "yes" (always subtract grid charging).
    car_detect_w: in hours without car statistics, a base load above this is taken to be the car
    charging and the hour is left out (the user's old rule of thumb: more than ~4 kW is the car).
    """
    load = _by_start(load_rows)
    car, car_kind = _power_by_start(car_rows)
    bat = _by_start(bat_rows)
    pv = _by_start(pv_rows)

    # Without the car's statistics, night car charging looks like extra house load exactly when the
    # battery charges (both use the cheapest hours), and would be mistaken for battery charging.
    if not bat:
        detection = {"included": None, "reason": "no_battery_statistics"}
    elif car_rows is None or not car:
        detection = {"included": None, "reason": "no_car_statistics"}
    else:
        detection = detect_battery_in_load(load, car, bat, pv)
    subtract = battery_mode == "yes" or (battery_mode == "auto" and detection.get("included") is True)

    samples: dict = {}
    n = corrected = car_hours = excluded = 0
    for start, load_w in load.items():
        car_w = max(0.0, car.get(start, 0.0))
        if car_w >= IDLE_W:
            car_hours += 1
        base_w = load_w - car_w
        if subtract and start in bat:
            grid_charge = max(0.0, bat[start] - max(0.0, pv.get(start, 0.0)))
            if grid_charge > 0:
                corrected += 1
            base_w -= grid_charge
        if start not in car and car_detect_w and base_w > car_detect_w:
            excluded += 1          # no car data for this hour and far above a house's load: the car
            continue
        base_w = max(0.0, base_w)
        local = start.astimezone(tz)
        samples.setdefault((local.weekday() >= 5, local.hour), []).append(base_w / 1000.0)
        n += 1

    # Fallback for sparse cells: same hour regardless of weekday/weekend.
    by_hour: dict = {}
    for (_, hour), vals in samples.items():
        by_hour.setdefault(hour, []).extend(vals)

    table = {}
    for weekend in (False, True):
        for hour in range(24):
            vals = samples.get((weekend, hour), [])
            if len(vals) >= 3:
                table[(weekend, hour)] = trimmed_mean(vals)
            elif by_hour.get(hour):
                table[(weekend, hour)] = trimmed_mean(by_hour[hour])
            else:
                table[(weekend, hour)] = default_kwh
    diagnostics = {
        "battery_mode": battery_mode,
        "battery_detection": detection,
        "battery_subtracted": subtract,
        "battery_corrected_hours": corrected,
        "car_rows": len(car),
        "car_kind": car_kind,
        "car_excluded_hours": excluded,
        "car_detect_kw": round(car_detect_w / 1000.0, 1) if car_detect_w else None,
        "car_hours": car_hours,
        "battery_rows": len(bat),
        "pv_rows": len(pv),
    }
    return {"table": table, "hours_of_data": n, "default": default_kwh, "diagnostics": diagnostics}


def load_for_hour(profile: dict, hour_utc: datetime, tz) -> float:
    local = hour_utc.astimezone(tz)
    return profile["table"].get((local.weekday() >= 5, local.hour), profile["default"])


def profile_view(profile: dict) -> dict:
    """The profile for the panel: 24 values for weekdays and weekends plus the diagnostics."""
    t = profile["table"]
    return {
        "weekday": [round(t[(False, h)], 3) for h in range(24)],
        "weekend": [round(t[(True, h)], 3) for h in range(24)],
        "hours_of_data": profile["hours_of_data"],
        "diagnostics": profile.get("diagnostics", {}),
        "statistics_rows": profile.get("statistics_rows", {}),
    }
