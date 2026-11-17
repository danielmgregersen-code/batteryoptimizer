"""The house battery's charge power at high SoC (the BMS tapers the last part), learned from data.

Learning: HA's 5-minute statistics (kept ~10 days) of the battery charge power and the SoC. Within
5 minutes the SoC moves at most a few percent, so each sample pairs a power with a SoC. Per 2 % bin
from 80 % up, the 90th percentile of the charging samples (> 300 W) is what the battery takes at
that SoC when nothing else limits it. The curve is made non-increasing and never above the
configured maximum; bins with too few samples fall back to the default (full power up to
`battery_taper_start_pct`, then linear down to `battery_taper_end_kw` at 100 %).

Planning: the hourly plan cannot see what happens within an hour (from 80 % the battery may reach
90 % in ten minutes and then slow down). So the curve is turned into the energy the battery can
store in a step of `dt` hours starting at SoC s, g(s), by stepping through it minute by minute;
its upper concave hull gives a few lines `stored <= a + b * s` that the LP adds per step.
"""
import math

from inputs import parse_ts

BIN_PCT = 2
LOW_PCT = 80
MIN_SAMPLES = 3
CHARGING_W = 300.0


def default_curve(max_kw: float, start_pct: float = 90.0, end_kw: float = 1.0) -> list:
    pts = [(LOW_PCT, max_kw), (start_pct, max_kw), (100.0, min(end_kw, max_kw))]
    return [(round(s, 1), round(k, 3)) for s, k in pts]


def _percentile(vals, q):
    vals = sorted(vals)
    if not vals:
        return None
    k = (len(vals) - 1) * q / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def learn(power_rows, soc_rows, max_kw: float, start_pct: float = 90.0, end_kw: float = 1.0) -> dict:
    """-> {"curve": [(soc_pct, kw)], "learned": bool, "samples": {bin: n}, "default": [...]}."""
    soc = {}
    for r in soc_rows or []:
        if r.get("mean") is not None:
            soc[parse_ts(r["start"])] = float(r["mean"])
    bins = {}
    for r in power_rows or []:
        p, t = r.get("mean"), parse_ts(r["start"])
        if p is None or t not in soc or float(p) < CHARGING_W:
            continue
        s = soc[t]
        if s < LOW_PCT:
            continue
        b = min(100 - BIN_PCT, int((s - LOW_PCT) // BIN_PCT) * BIN_PCT + LOW_PCT)
        bins.setdefault(b, []).append(float(p) / 1000.0)
    default = default_curve(max_kw, start_pct, end_kw)
    counts = {b: len(v) for b, v in sorted(bins.items())}
    good = {b: min(max_kw, _percentile(v, 90)) for b, v in bins.items() if len(v) >= MIN_SAMPLES}
    # learned only where the battery is clearly limited: the top bins must have data
    if not good or max(good) < 94:
        return {"curve": default, "learned": False, "samples": counts, "default": default}
    pts = [(LOW_PCT, max_kw)]
    for b in sorted(good):
        pts.append((b + BIN_PCT / 2.0, good[b]))
    pts.append((100.0, min(pts[-1][1], good[max(good)])))
    # non-increasing with SoC (a later bin can never allow more than an earlier one)
    out, cur = [], max_kw
    for s, k in pts:
        cur = min(cur, k)
        out.append((round(s, 1), round(cur, 3)))
    return {"curve": out, "learned": True, "samples": counts, "default": default}


def power_at(curve: list, soc_pct: float, max_kw: float) -> float:
    if not curve or soc_pct <= curve[0][0]:
        return max_kw
    for (s0, k0), (s1, k1) in zip(curve, curve[1:]):
        if soc_pct <= s1:
            return k0 + (k1 - k0) * (soc_pct - s0) / max(1e-9, s1 - s0)
    return curve[-1][1]


def stored_in_step(curve: list, cap_kwh: float, max_kw: float, s_kwh: float, dt: float) -> float:
    """kWh the battery can take in dt hours starting at s_kwh (the curve is power into the battery)."""
    minutes = max(1, int(round(dt * 60)))
    s = s_kwh
    for _ in range(minutes):
        if s >= cap_kwh:
            break
        p = power_at(curve, s / cap_kwh * 100.0, max_kw)
        s = min(cap_kwh, s + p / 60.0)
    return s - s_kwh


def step_lines(curve: list, cap_kwh: float, max_kw: float, dt: float, from_pct: float = 60.0) -> list:
    """[(a, b)] with stored <= a + b * s (s = SoC kWh at the start of the step): the upper concave
    hull of g(s) where it falls below the plain power limit."""
    pts = []
    for i in range(int(from_pct), 101):
        s = cap_kwh * i / 100.0
        pts.append((s, stored_in_step(curve, cap_kwh, max_kw, s, dt)))
    hull = []
    for p in pts:                       # upper hull (Andrew's monotone chain, upper part)
        while len(hull) >= 2 and _cross(hull[-2], hull[-1], p) >= 0:
            hull.pop()
        hull.append(p)
    lines = []
    for (x0, y0), (x1, y1) in zip(hull, hull[1:]):
        b = (y1 - y0) / (x1 - x0)
        if b < -1e-6:
            lines.append((y0 - b * x0, b))
    return lines


def _cross(o, a, b):
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
