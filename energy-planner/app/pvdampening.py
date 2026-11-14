"""Learned solar dampening per hour of day: local shading (trees) on top of the Solcast forecast.

Trees block the *direct* sun. On a clear day an hour behind a tree loses most of its output; on an
overcast day the light is diffuse and the tree hardly matters. So the correction is not a fixed
factor per hour but

    factor(hour, day) = 1 - shade[h] * clearness

    clearness = Solcast p50 / clear-sky reference for that hour   (0 = overcast .. 1 = clear)
    clear-sky reference = highest p90 at that local hour over the recent weeks and the coming days

Solcast's own forecast is the cloud forecast, so no weather integration is needed. `shade[h]` is
fitted by least squares over the last `days` days:

    forecast - actual = shade[h] * (forecast * clearness)

which weights the clear, high-output hours (where shading shows) most and lets cloudy hours count
little. It is shrunk towards 0 (no dampening) when there are few hours to learn from.

Everything is learned against the *undampened* Solcast forecast: each recorded hour stores the
dampening that was set in Solcast when it was read (`applied`), and the value is divided by it.
So the add-on can push factors into Solcast without dampening twice or oscillating.

Excluded from learning: hours with the export limit at 0 (the inverter throttles the panels, which
would look like shade) and hours near the inverter's maximum (clipping).
"""
import json
import os
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
MIN_FORECAST_KWH = 0.05     # hours with less forecast sun carry no information
LAMBDA = 0.1                # shrinkage (kWh^2): with few clear hours shade stays near 0
MIN_HOURS = 3               # per hour of day, at least this many usable days
MIN_FACTOR = 0.05           # keeps every factor invertible (value / applied)
CLIP_SHARE = 0.95           # actual above this share of the inverter's power is clipping
HISTORY_DAYS = 60
CLEAR_REF_DAYS = 21


# ---------------------------------------------------------------- persistence
def load_json(path: str, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path: str, data):
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except OSError:
        pass


def record(history: dict, hour_utc: datetime, p50: float, p10: float, p90: float,
           applied: float, export_limited: bool, now: datetime) -> dict:
    """Store Solcast's value for the hour as read now (the last read within the hour wins)."""
    history[hour_utc.astimezone(UTC).isoformat()] = {
        "p50": round(p50, 4), "p10": round(p10, 4), "p90": round(p90, 4),
        "applied": round(applied, 3), "limited": bool(export_limited),
    }
    cutoff = (now - timedelta(days=HISTORY_DAYS)).astimezone(UTC).isoformat()
    for k in [k for k in history if k < cutoff]:
        del history[k]
    return history


# ---------------------------------------------------------------- model
def _undamped(e: dict, key: str) -> float:
    return float(e.get(key, 0.0)) / max(float(e.get("applied", 1.0)), MIN_FACTOR)


def clear_reference(history: dict, forecast_p90: dict, applied: list, tz, now: datetime) -> list:
    """Per local hour: the highest undampened p90 over the last weeks. The coming days' forecast is
    only used for hours without history: learning and applying must measure clearness against the
    same reference, or one unusually clear forecast would rescale the whole history."""
    ref = [0.0] * 24
    cutoff = now - timedelta(days=CLEAR_REF_DAYS)
    for k, e in history.items():
        t = datetime.fromisoformat(k)
        if t >= cutoff:
            h = t.astimezone(tz).hour
            ref[h] = max(ref[h], _undamped(e, "p90"))
    fallback = [0.0] * 24
    for t, v in forecast_p90.items():
        h = t.astimezone(tz).hour
        fallback[h] = max(fallback[h], v / max(applied[h], MIN_FACTOR))
    return [r if r > 0 else f for r, f in zip(ref, fallback)]


def clearness(p50_undamped: float, ref: float) -> float:
    if ref <= MIN_FORECAST_KWH:
        return 0.0
    return min(max(p50_undamped / ref, 0.0), 1.0)


def fit(history: dict, actual_w: dict, ref: list, tz, now: datetime, days: int, inverter_kw: float) -> dict:
    """-> {"shade": [24], "hours": [24] usable hours, "forecast": [24] mean undampened kWh,
    "actual": [24] mean kWh, "corrected": [24] mean kWh after correction}."""
    sxy, sxx, n = [0.0] * 24, [0.0] * 24, [0] * 24
    sum_f, sum_a = [0.0] * 24, [0.0] * 24
    samples = []
    cutoff = now - timedelta(days=days)
    for k, e in history.items():
        t = datetime.fromisoformat(k)
        if t < cutoff or t >= now - timedelta(hours=1) or e.get("limited"):
            continue
        a_w = actual_w.get(t)
        if a_w is None:
            continue
        a = max(0.0, a_w) / 1000.0
        f = _undamped(e, "p50")
        if f < MIN_FORECAST_KWH or a >= CLIP_SHARE * inverter_kw:
            continue
        h = t.astimezone(tz).hour
        kc = clearness(f, ref[h])
        x = f * kc
        sxy[h] += x * (f - a)
        sxx[h] += x * x
        n[h] += 1
        sum_f[h] += f
        sum_a[h] += a
        samples.append((h, f, kc, a))
    shade = []
    for h in range(24):
        s = sxy[h] / (sxx[h] + LAMBDA) if n[h] >= MIN_HOURS else 0.0
        shade.append(round(min(max(s, 0.0), 1.0 - MIN_FACTOR), 3))
    corr = [0.0] * 24
    for h, f, kc, a in samples:
        corr[h] += f * (1.0 - shade[h] * kc)
    return {
        "shade": shade,
        "hours": n,
        "forecast": [round(sum_f[h] / n[h], 3) if n[h] else None for h in range(24)],
        "actual": [round(sum_a[h] / n[h], 3) if n[h] else None for h in range(24)],
        "corrected": [round(corr[h] / n[h], 3) if n[h] else None for h in range(24)],
    }


def parse_manual(text: str) -> dict:
    """'14:0.3, 15:0,2' -> {14: 0.3, 15: 0.2}: the clear-sky factor for those hours, fixed.
    Decimal point or comma both work; pairs are separated by comma, semicolon or space."""
    import re
    out = {}
    for h, v in re.findall(r"(\d{1,2})\s*:\s*(\d+(?:[.,]\d+(?!\d)(?!\s*:))?)", text or ""):
        hour, val = int(h), float(v.replace(",", "."))
        if 0 <= hour <= 23:
            out[hour] = min(max(val, MIN_FACTOR), 1.0)
    return out


def effective_shade(model_shade: list, manual: dict, mode: str) -> list:
    """Shade per hour after the mode and the manual hours (manual factor f means shade 1 - f)."""
    if mode == "off":
        return [0.0] * 24
    base = model_shade if mode == "auto" else [0.0] * 24
    return [round(1.0 - manual[h], 3) if h in manual else base[h] for h in range(24)]


def factor(shade_h: float, kc: float) -> float:
    return max(MIN_FACTOR, 1.0 - shade_h * kc)


def correct(p50: dict, p10: dict, p90: dict, applied: list, shade: list, ref: list, tz) -> tuple:
    """Undampen Solcast's values and apply the learned correction. -> (p50, p10, p90, factors{hour: f})."""
    o50, o10, o90, fac = {}, {}, {}, {}
    for t, v in p50.items():
        h = t.astimezone(tz).hour
        a = max(applied[h], MIN_FACTOR)
        u50 = v / a
        f = factor(shade[h], clearness(u50, ref[h]))
        fac[t] = round(f, 3)
        o50[t] = u50 * f
        o10[t] = p10.get(t, v) / a * f
        o90[t] = p90.get(t, v) / a * f
    return o50, o10, o90, fac


def static_factors(shade: list, p50_today: dict, applied: list, ref: list, tz) -> list:
    """24 factors for Solcast (it only takes a fixed factor per hour): today's clearness per hour,
    or the clear-sky factor where there is no forecast for the hour today."""
    out = [round(1.0 - s, 2) for s in shade]
    for t, v in p50_today.items():
        h = t.astimezone(tz).hour
        u = v / max(applied[h], MIN_FACTOR)
        if u >= MIN_FORECAST_KWH:
            out[h] = round(factor(shade[h], clearness(u, ref[h])), 2)
    return [min(max(f, MIN_FACTOR), 1.0) for f in out]


def parse_get_dampening(resp) -> list:
    """Solcast's get_dampening response -> 24 hourly factors, or None if not hourly / unknown."""
    if not isinstance(resp, dict):
        return None
    data = resp.get("service_response", resp)
    data = data.get("data", data) if isinstance(data, dict) else data
    items = data if isinstance(data, list) else [data]
    for it in items:
        if not isinstance(it, dict):
            continue
        raw = it.get("damp_factor")
        if raw is None:
            continue
        vals = raw if isinstance(raw, list) else str(raw).split(",")
        try:
            vals = [float(v) for v in vals]
        except ValueError:
            return None
        if len(vals) == 24:
            return vals
        if len(vals) == 48:   # half-hourly: average each hour's two halves
            return [round((vals[2 * i] + vals[2 * i + 1]) / 2, 3) for i in range(24)]
    return None


def solcast_auto_dampening(attrs_list: list) -> bool:
    """Solcast's own automated dampening adds a dampening_factor to every detailedForecast entry."""
    for attrs in attrs_list:
        for e in (attrs or {}).get("detailedForecast") or []:
            if "dampening_factor" in e:
                return True
    return False


def mean_daylight(factors: list, ref: list) -> float:
    vals = [f for f, r in zip(factors, ref) if r > MIN_FORECAST_KWH]
    return round(sum(vals) / len(vals), 3) if vals else 1.0


def push_due(desired: list, applied: list, last_push: str, now: datetime, tz) -> bool:
    """Push once per local day (after midnight), or sooner if a factor moved a lot (>= 3 h apart)."""
    if last_push is None:
        return True
    last = datetime.fromisoformat(last_push)
    if last.astimezone(tz).date() != now.astimezone(tz).date():
        return True
    big = max(abs(d - a) for d, a in zip(desired, applied)) > 0.1
    return big and now - last >= timedelta(hours=3)


def csv(factors: list) -> str:
    return ",".join(f"{f:.2f}" for f in factors)

