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


def weighted_trimmed_mean(pairs, trim: float = TRIM) -> float:
    """Trimmed mean of (value, weight) pairs: `trim` of the total *weight* is dropped at each end."""
    pairs = sorted((v, w) for v, w in pairs if w > 0)
    total = sum(w for _, w in pairs)
    if total <= 0:
        return 0.0
    lo, hi = trim * total, (1.0 - trim) * total
    acc = num = den = 0.0
    for v, w in pairs:
        a, b = acc, acc + w
        ov = max(0.0, min(b, hi) - max(a, lo))
        num += v * ov
        den += ov
        acc = b
    return num / den if den > 0 else sum(v * w for v, w in pairs) / total


def weighted_mean(pairs) -> float:
    total = sum(w for _, w in pairs)
    return sum(v * w for v, w in pairs) / total if total > 0 else 0.0


def sample_weight(start: datetime, now: datetime, half_life_days: float, last_year_weight: float) -> float:
    """Recent hours count most (0.5^(age / half life)). Hours from about a year ago (the same weeks
    last year) count `last_year_weight` x 0.5^(distance from today's date one year ago / half life)."""
    if now is None or not half_life_days:
        return 1.0
    age = (now - start).total_seconds() / 86400.0
    if age > 180:
        return last_year_weight * 0.5 ** (abs(age - 365.25) / half_life_days) if last_year_weight else 0.0
    return 0.5 ** (max(0.0, age) / half_life_days)


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
                  battery_mode: str = "no", car_detect_w: float = 4000.0, submeter_rows: dict = None,
                  now: datetime = None, half_life_days: float = None, last_year_weight: float = 0.0,
                  steady_submeters=(), smooth_hours: int = 2) -> dict:
    """-> {"table": {(is_weekend, hour): kWh}, "hours_of_data": int, "default": kWh, "diagnostics": {...}}.

    battery_mode: "no" (default: the Growatt house load does not include battery charging),
    "auto" (detect), "yes" (always subtract grid charging).
    car_detect_w: in hours without car statistics, a base load above this is taken to be the car
    charging and the hour is left out (the user's old rule of thumb: more than ~4 kW is the car).
    submeter_rows: {entity: statistics rows} of appliances measured separately (heat pump, washer /
    dryer, dishwasher, hob). Each gets its own plain (untrimmed) mean per cell, so an appliance that
    runs on some days only counts with its expected energy instead of being trimmed away; the rest
    (base load minus the submeters) keeps the trimmed mean.
    now / half_life_days / last_year_weight: sample weights (see sample_weight); without `now` every
    hour counts the same.
    Appliances that run when needed (washer, dryer, dishwasher, hob) have no fixed hour, so their
    timing is smoothed: weekdays and weekends are pooled and each hour gets the triangular-weighted
    mean of +-`smooth_hours` around it (the day's energy is kept). `steady_submeters` (a heat pump
    run by its thermostat) keep their own profile per (weekday/weekend, hour).
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

    subs = {}
    for e, rows in (submeter_rows or {}).items():
        vals, _ = _power_by_start(rows)
        subs[e] = vals
    active = [e for e, v in subs.items() if v]          # submeters with statistics at all
    samples: dict = {}                                  # cell -> [(base kWh, weight)]
    rest: dict = {}                                     # cell -> [(rest kWh, weight)] (complete hours)
    sub_samples: dict = {}                              # cell -> {entity: [(kWh, weight)]}
    last_year_hours = 0
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
        cell = (local.weekday() >= 5, local.hour)
        w = sample_weight(start, now, half_life_days, last_year_weight)
        if w <= 0:
            continue
        if now is not None and (now - start).days > 180:
            last_year_hours += 1
        samples.setdefault(cell, []).append((base_w / 1000.0, w))
        if active and all(start in subs[e] for e in active):
            parts = {e: max(0.0, subs[e][start]) / 1000.0 for e in active}
            rest.setdefault(cell, []).append((max(0.0, base_w / 1000.0 - sum(parts.values())), w))
            for e, v in parts.items():
                sub_samples.setdefault(cell, {}).setdefault(e, []).append((v, w))
        n += 1

    # Fallback for sparse cells: same hour regardless of weekday/weekend.
    by_hour: dict = {}
    for (_, hour), vals in samples.items():
        by_hour.setdefault(hour, []).extend(vals)

    smoothed = _smooth_submeters(sub_samples, [e for e in active if e not in set(steady_submeters)], smooth_hours)
    table, parts = {}, {}
    for weekend in (False, True):
        for hour in range(24):
            cell = (weekend, hour)
            vals = samples.get(cell, [])
            if len(rest.get(cell, [])) >= 3:
                # split: the even rest (trimmed) plus each submeter's expected energy (plain mean)
                r = weighted_trimmed_mean(rest[cell])
                sp = {e: smoothed[e][hour] if e in smoothed else weighted_mean(v)
                      for e, v in sub_samples[cell].items()}
                parts[cell] = {"rest": r, "subs": sp}
                table[cell] = r + sum(sp.values())
            elif len(vals) >= 3:
                table[cell] = weighted_trimmed_mean(vals)
            elif by_hour.get(hour):
                table[cell] = weighted_trimmed_mean(by_hour[hour])
            else:
                table[cell] = default_kwh
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
        "submeters": {e: len(v) for e, v in subs.items()},
        "split_cells": len(parts),
        "smoothed_submeters": sorted(smoothed),
        "smooth_hours": smooth_hours,
        "half_life_days": half_life_days,
        "last_year_hours": last_year_hours,
        "last_year_weight": last_year_weight,
    }
    return {"table": table, "parts": parts, "hours_of_data": n, "default": default_kwh, "diagnostics": diagnostics}


def _smooth_submeters(sub_samples: dict, ids: list, k: int) -> dict:
    """{entity: [24 kWh]}: weekday and weekend pooled per hour, then a circular triangular moving
    average over +-k hours. A circular average keeps the sum over the day (the expected energy)."""
    out = {}
    for e in ids:
        pooled = []
        for hour in range(24):
            pairs = [x for we in (False, True) for x in (sub_samples.get((we, hour)) or {}).get(e, [])]
            pooled.append(weighted_mean(pairs) if pairs else None)
        if all(v is None for v in pooled):
            continue
        sm = []
        for hour in range(24):
            num = den = 0.0
            for d in range(-k, k + 1):
                v = pooled[(hour + d) % 24]
                if v is None:
                    continue
                w = k + 1 - abs(d)
                num += w * v
                den += w
            sm.append(num / den if den else 0.0)
        out[e] = sm
    return out


def load_for_hour(profile: dict, hour_utc: datetime, tz, live: dict = None, now_hour: datetime = None) -> float:
    """Expected base load (kWh) for an hour. live: {submeter: kW right now}. In the current hour the
    submeters' profile is replaced by what they draw now (a dryer running, the hob on); in the next
    hour, a submeter that is running (> 50 W) counts the average of now and its profile."""
    local = hour_utc.astimezone(tz)
    cell = (local.weekday() >= 5, local.hour)
    base = profile["table"].get(cell, profile["default"])
    part = (profile.get("parts") or {}).get(cell)
    if not live or now_hour is None or part is None:
        return base
    ahead = round((hour_utc - now_hour).total_seconds() / 3600.0)
    if ahead == 0:
        return part["rest"] + sum(live.get(e, v) for e, v in part["subs"].items())
    if ahead == 1:
        return part["rest"] + sum((live[e] + v) / 2 if live.get(e, 0.0) > 0.05 else v
                                  for e, v in part["subs"].items())
    return base


def profile_view(profile: dict) -> dict:
    """The profile for the panel: 24 values for weekdays and weekends plus the diagnostics."""
    t = profile["table"]
    return {
        "weekday": [round(t[(False, h)], 3) for h in range(24)],
        "weekend": [round(t[(True, h)], 3) for h in range(24)],
        "parts": {("weekend" if we else "weekday"): {
            "rest": [round(profile["parts"][(we, h)]["rest"], 3) if (we, h) in profile.get("parts", {}) else None
                     for h in range(24)],
            "subs": {e: [round(profile["parts"][(we, h)]["subs"].get(e, 0.0), 3) if (we, h) in profile.get("parts", {})
                         else None for h in range(24)]
                     for e in sorted({e for p in profile.get("parts", {}).values() for e in p["subs"]})},
        } for we in (False, True)} if profile.get("parts") else None,
        "hours_of_data": profile["hours_of_data"],
        "diagnostics": profile.get("diagnostics", {}),
        "statistics_rows": profile.get("statistics_rows", {}),
    }
