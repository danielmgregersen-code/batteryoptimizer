"""The garage heat pump: move its heat to the cheap hours through the setpoint.

The garage is a heat store: in a cheap hour the setpoint goes up by `heat_pump_band`, in a dear
hour down by it, otherwise it stays at `heat_pump_setpoint`. The compressor keeps regulating by
itself; nothing is switched on or off.

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

LEVEL_LABEL = {"cheap": "billig time", "normal": "normal time", "dear": "dyr time"}


def price_levels(rows: list, now_local: datetime) -> list:
    """[(row, level)] for the plan's rows of the next 24 hours."""
    end = now_local.replace(minute=0, second=0, microsecond=0) + timedelta(hours=24)
    window = [r for r in rows if datetime.fromisoformat(r["t"]) < end]
    if not window:
        return []
    ranked = sorted(r["buy"] for r in window)
    n = len(ranked)
    lo, hi = ranked[max(0, n // 3 - 1)], ranked[min(n - 1, n - n // 3)]
    out = []
    for r in window:
        if (r.get("pv_curt") or 0) > 0.05 or ((r.get("pv_exp") or 0) > 0.05 and r["sell"] <= 0):
            level = "cheap"
        elif n >= 3 and r["buy"] <= lo:
            level = "cheap"
        elif n >= 3 and r["buy"] >= hi:
            level = "dear"
        else:
            level = "normal"
        out.append((r, level))
    return out


def setpoint_for(level: str, opts: dict) -> float:
    base, band = float(opts["heat_pump_setpoint"]), float(opts["heat_pump_band"])
    return base + band if level == "cheap" else base - band if level == "dear" else base


def decide(rows: list, now_local: datetime, room_temp, humidity, opts: dict) -> dict:
    """-> {setpoint, level, reason, plan: [{t, level, setpoint}], room_temp, humidity}."""
    base, band = float(opts["heat_pump_setpoint"]), float(opts["heat_pump_band"])
    levels = price_levels(rows, now_local)
    level = levels[0][1] if levels else "normal"
    sp = setpoint_for(level, opts)
    reason = f"{LEVEL_LABEL[level]}: {sp:g} °C"
    rmin, rmax = float(opts["heat_pump_room_min"]), float(opts["heat_pump_room_max"])
    hmax = float(opts["heat_pump_humidity_max"])
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
        "setpoint": sp, "level": level, "reason": reason,
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
