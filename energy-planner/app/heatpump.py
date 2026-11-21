"""The garage heat pump: move its heat to the cheap hours through the setpoint.

The garage is a heat store: in a cheap hour the setpoint goes up by `heat_pump_band`, in a dear
hour down by it, otherwise it stays at `heat_pump_setpoint`. The compressor keeps regulating by
itself. In the dearest hours ("off": the heat_pump_off_hours dearest of the day, or a price above
the heat_pump_off_percentile of the last 30 days) it is switched off - unless the garage is below
heat_pump_off_min_temp (then it runs whatever the price, until a degree above), too humid or not
measured.

Price level of the current hour: its buy price ranked among the plan's hours of the next 24 hours.
The cheapest third is "cheap", the dearest third "dear". An hour where the plan exports at a sell
price <= 0 or curtails sun is always "cheap" (the energy is free).

Safety, from the garage's own sensors:
  - garage below room_min      -> never below normal; below room_min - 1 -> normal + band
  - garage above room_max      -> never above normal
  - humidity above humidity_max -> never below normal (warmth lowers the relative humidity)
  - a missing reading          -> never below normal
"""
from datetime import datetime, timedelta

LEVEL_LABEL = {"cheap": "billig time", "normal": "normal time", "dear": "dyr time", "off": "meget dyr time"}


def percentile(values, pct: float):
    """The pct-th percentile (0-100, nearest rank) of values, or None without values."""
    vals = sorted(float(v) for v in values or [] if v is not None)
    if not vals:
        return None
    k = min(len(vals) - 1, max(0, int(round(pct / 100.0 * (len(vals) - 1)))))
    return vals[k]


def off_threshold(opts: dict, history) -> float:
    """The buy price from which an hour is "off" by the long-term rule (None: rule off / no history)."""
    pct = float((opts or {}).get("heat_pump_off_percentile", 100) or 100)
    if pct >= 100 or not history or len(history) < 24:
        return None
    return percentile(history, pct)


def price_levels(rows: list, now_local: datetime, opts: dict = None, history=None) -> list:
    """[(row, level)] for the plan's rows of the next 24 hours.

    With opts, a "dear" hour without free sun is "off" (the heat pump switched off) when it is one of
    the heat_pump_off_hours dearest of the 24 hours, or its price is at least the
    heat_pump_off_percentile of the last 30 days' prices (`history`)."""
    end = now_local.replace(minute=0, second=0, microsecond=0) + timedelta(hours=24)
    window = [r for r in rows if datetime.fromisoformat(r["t"]) < end]
    if not window:
        return []
    ranked = sorted(r["buy"] for r in window)
    n = len(ranked)
    lo, hi = ranked[max(0, n // 3 - 1)], ranked[min(n - 1, n - n // 3)]
    off_n = int((opts or {}).get("heat_pump_off_hours", 0) or 0) if opts else 0
    off_day = ranked[-off_n] if off_n and n >= 3 else None
    off_long = off_threshold(opts, history) if opts else None
    out = []
    for r in window:
        if (r.get("pv_curt") or 0) > 0.05 or ((r.get("pv_exp") or 0) > 0.05 and r["sell"] <= 0):
            level = "cheap"
        elif n >= 3 and r["buy"] <= lo:
            level = "cheap"
        elif n >= 3 and r["buy"] >= hi:
            level = "dear"
            if (r.get("pv_exp") or 0) <= 0.05 and ((off_day is not None and r["buy"] >= off_day)
                                                   or (off_long is not None and r["buy"] >= off_long)):
                level = "off"
        else:
            level = "normal"
        out.append((r, level))
    return out


def setpoint_for(level: str, opts: dict) -> float:
    base, band = float(opts["heat_pump_setpoint"]), float(opts["heat_pump_band"])
    return base + band if level == "cheap" else base - band if level in ("dear", "off") else base


def decide(rows: list, now_local: datetime, room_temp, humidity, opts: dict, history=None,
           forced_on: bool = False) -> dict:
    """-> {setpoint, hvac ("on"/"off"), level, reason, forced_on, plan: [{t, level, setpoint}], room_temp,
    humidity}. forced_on: the heat pump was switched on below heat_pump_off_min_temp and stays on until
    the garage is a degree above it."""
    base, band = float(opts["heat_pump_setpoint"]), float(opts["heat_pump_band"])
    levels = price_levels(rows, now_local, opts, history)
    level = levels[0][1] if levels else "normal"
    sp = setpoint_for(level, opts)
    hvac = "on"
    reason = f"{LEVEL_LABEL[level]}: {sp:g} °C"
    rmin, rmax = float(opts["heat_pump_room_min"]), float(opts["heat_pump_room_max"])
    hmax = float(opts["heat_pump_humidity_max"])
    tmin = float(opts.get("heat_pump_off_min_temp", rmin))
    if room_temp is not None and (room_temp < tmin or (forced_on and room_temp < tmin + 1)):
        forced_on = True
    elif room_temp is not None:
        forced_on = False
    if level == "off":
        r0 = levels[0][0]
        if room_temp is None:
            reason = "ingen måling af garagens temperatur: slukker ikke"
        elif forced_on:
            sp, reason = base, f"garagen er {room_temp:.1f} °C (minimum {tmin:g}): tændt uanset pris"
        elif humidity is not None and humidity > hmax:
            reason = f"fugt {humidity:.0f} % (over {hmax:g} %): slukker ikke"
        else:
            hvac = "off"
            reason = f"slukket: {r0['buy']:.2f} kr er en af de dyreste timer".replace(".", ",")
            off_long = off_threshold(opts, history)
            if off_long is not None and r0["buy"] >= off_long:
                pct = float(opts.get("heat_pump_off_percentile", 100))
                reason = (f"slukket: {r0['buy']:.2f}".replace(".", ",")
                          + f" kr er blandt de dyreste {100 - pct:g} % de seneste 30 dage")
        if hvac == "on" and not forced_on:
            level = "dear"
    if hvac == "on":
        if room_temp is None:
            if sp < base:
                sp, reason = base, "ingen måling af garagens temperatur: holder normal"
        elif room_temp < rmin - 1:
            sp, reason = base + band, f"garagen er {room_temp:.1f} °C (under {rmin - 1:g}): varmer op"
        elif room_temp < rmin and sp < base:
            sp, reason = base, f"garagen er {room_temp:.1f} °C (under {rmin:g}): holder normal"
        elif room_temp > rmax and sp > base:
            sp, reason = base, f"garagen er {room_temp:.1f} °C (over {rmax:g}): holder normal"
        if sp < base and humidity is not None and humidity > hmax:
            sp, reason = base, f"fugt {humidity:.0f} % (over {hmax:g} %): sænker ikke"
    return {
        "setpoint": sp, "hvac": hvac, "level": level, "reason": reason, "forced_on": forced_on,
        "room_temp": room_temp, "humidity": humidity,
        "plan": [{"t": r["t"], "level": lv, "setpoint": setpoint_for(lv, opts)} for r, lv in levels],
    }


# ---- expected consumption per level (for the load forecast) ---------------------------------
MIN_LEVEL_HOURS = 3       # (weighted) hours per level before its learned value is used
LEVEL_DAYS = 28


def learn_levels(levels: dict, power_w: dict, now_utc: datetime, half_life_days: float = 10.0) -> dict:
    """Mean kWh per hour of the heat pump in cheap / normal / dear hours, from the levels it actually
    ran with (`levels`: {utc hour iso: {"level", "applied"}}) and its meter's hourly mean power
    (`power_w`: {utc datetime: W}). Recent days count most. -> {level: {"kwh", "hours"}} for the levels
    with enough data."""
    sums = {}
    cutoff = now_utc - timedelta(days=LEVEL_DAYS)
    for key, rec in levels.items():
        t = datetime.fromisoformat(key)
        if t < cutoff or t + timedelta(hours=1) > now_utc or not rec.get("applied"):
            continue
        w = power_w.get(t)
        if w is None:
            continue
        age = (now_utc - t).total_seconds() / 86400.0
        wt = 0.5 ** (age / half_life_days) if half_life_days else 1.0
        s = sums.setdefault(rec["level"], [0.0, 0.0, 0])
        s[0] += wt * max(0.0, w) / 1000.0
        s[1] += wt
        s[2] += 1
    return {lv: {"kwh": round(a / b, 3), "hours": n} for lv, (a, b, n) in sums.items()
            if b > 0 and n >= MIN_LEVEL_HOURS}


def record_level(levels: dict, hour_utc: datetime, level: str, setpoint: float, applied: bool, now_utc: datetime):
    levels[hour_utc.isoformat()] = {"level": level, "setpoint": setpoint, "applied": bool(applied)}
    cutoff = (now_utc - timedelta(days=LEVEL_DAYS + 7)).isoformat()
    for k in [k for k in levels if k < cutoff]:
        del levels[k]
