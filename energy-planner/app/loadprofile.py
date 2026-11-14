"""Base-load profile from HA long-term statistics (hourly means).

Base load = total house load minus EV charging, so the profile describes the part of the
consumption that repeats day after day. A trimmed mean per (weekday/weekend, hour of day)
drops the most extreme hours (a car hour the charger sensor missed, a space heater) but, unlike
the median, still counts the ordinary peaks such as cooking: the plan needs the expected energy,
and the median of a right-skewed load underestimates it.
"""
import math
from datetime import datetime

from inputs import parse_ts


TRIM = 0.15   # share dropped at each end


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


def build_profile(load_rows, car_rows, tz, default_kwh: float) -> dict:
    """-> {"table": {(is_weekend, hour): kWh}, "hours_of_data": int, "default": kWh}."""
    load = _by_start(load_rows)
    car = _by_start(car_rows)
    samples: dict = {}
    n = 0
    for start, load_w in load.items():
        base_w = max(0.0, load_w - max(0.0, car.get(start, 0.0)))
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
    return {"table": table, "hours_of_data": n, "default": default_kwh}


def load_for_hour(profile: dict, hour_utc: datetime, tz) -> float:
    local = hour_utc.astimezone(tz)
    return profile["table"].get((local.weekday() >= 5, local.hour), profile["default"])
