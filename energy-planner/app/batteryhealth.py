"""The house battery's real capacity (state of health), measured from data.

The BMS reports the SoC as a share of the battery's real capacity, so a charge or a discharge that
moves the SoC far shows the capacity: energy / ΔSoC. HA's 5-minute statistics (about 10 days) give
the SoC (mean) and the battery's input and output counters (change per 5 minutes). A run is a stretch
of intervals where only one counter moves (short idle intervals allowed), with the SoC between
LOW_PCT and HIGH_PCT (away from the BMS's recalibration at the ends) moving at least MIN_SWING points.

A charge run gives capacity / η_charge (a little high), a discharge run capacity × η_discharge (a
little low). The estimate is the geometric mean of the two medians: the losses nearly cancel, so it
does not depend on whether the counters measure on the AC or the DC side. The runs are kept in /data
so the history grows beyond HA's 10 days.
"""
import math
from datetime import timedelta

from inputs import parse_ts

LOW_PCT, HIGH_PCT = 10.0, 95.0
MIN_SWING = 25.0
EPS_KWH = 0.005
MAX_IDLE = 3              # idle intervals allowed inside a run
MIN_RUNS = 5              # per direction before the estimate is used
LAST_RUNS = 30
KEEP = 200
STEP = timedelta(minutes=5)


def _series(rows, key):
    out = {}
    for r in rows or []:
        try:
            v = r.get(key)
            if v is None:
                continue
            out[parse_ts(r["start"])] = float(v)
        except (KeyError, TypeError, ValueError):
            continue
    return out


def find_runs(soc_rows, in_rows, out_rows, min_swing: float = MIN_SWING) -> list:
    """5-minute statistics -> [{start, dir, soc_from, soc_to, kwh, raw_kwh}]."""
    soc, e_in, e_out = _series(soc_rows, "mean"), _series(in_rows, "change"), _series(out_rows, "change")
    times = sorted(t for t in soc if t in e_in and t in e_out)
    runs, cur = [], None

    def close():
        nonlocal cur
        if cur and cur["steps"]:
            steps = cur["steps"]
            while steps and steps[-1][2] == 0:          # trailing idle intervals
                steps.pop()
            if len(steps) >= 2:
                first, last = steps[0], steps[-1]
                d_soc = last[1] - first[1]
                # the SoC is the interval's mean (its middle): half of the first and the last interval's energy
                kwh = sum(s[2] for s in steps) - (first[2] + last[2]) / 2.0
                sign = 1 if cur["dir"] == "charge" else -1
                if sign * d_soc >= min_swing and kwh > 0:
                    runs.append({"start": first[0].isoformat(), "dir": cur["dir"],
                                 "soc_from": round(first[1], 1), "soc_to": round(last[1], 1),
                                 "kwh": round(kwh, 3), "raw_kwh": round(kwh / (abs(d_soc) / 100.0), 3)})
        cur = None

    prev_t = None
    for t in times:
        s, i, o = soc[t], max(0.0, e_in[t]), max(0.0, e_out[t])
        if prev_t is not None and t - prev_t > STEP + timedelta(seconds=30):
            close()                                       # a gap in the data
        prev_t = t
        if i > EPS_KWH and o > EPS_KWH:
            close()                                       # both counters: not a clean run
            continue
        d = "charge" if i > EPS_KWH else "discharge" if o > EPS_KWH else None
        inside = LOW_PCT <= s <= HIGH_PCT
        if d is None:
            if cur:
                cur["idle"] += 1
                if cur["idle"] > MAX_IDLE or not inside:
                    close()
                else:
                    cur["steps"].append((t, s, 0.0))
            continue
        if not inside or (cur and cur["dir"] != d):
            close()
        if not inside:
            continue
        if cur is None:
            cur = {"dir": d, "steps": [], "idle": 0}
        cur["idle"] = 0
        cur["steps"].append((t, s, i if d == "charge" else o))
    close()
    return runs


def remember(store: list, runs: list, keep: int = KEEP) -> list:
    have = {(r["start"], r["dir"]): r for r in store or []}
    for r in runs:
        have[(r["start"], r["dir"])] = r
    return sorted(have.values(), key=lambda r: r["start"])[-keep:]


def _median(vals):
    v = sorted(vals)
    n = len(v)
    return None if not n else (v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2.0)


def estimate(runs: list, nominal_kwh: float) -> dict:
    """-> {kwh, soh_pct, charge_runs, discharge_runs, learned}; kwh None without both directions."""
    ch = [r["raw_kwh"] for r in runs if r["dir"] == "charge"][-LAST_RUNS:]
    dis = [r["raw_kwh"] for r in runs if r["dir"] == "discharge"][-LAST_RUNS:]
    mc, md = _median(ch), _median(dis)
    kwh = math.sqrt(mc * md) if mc and md else None
    return {"kwh": round(kwh, 2) if kwh else None,
            "soh_pct": round(kwh / nominal_kwh * 100.0, 1) if kwh and nominal_kwh else None,
            "charge_runs": len(ch), "discharge_runs": len(dis),
            "charge_median_kwh": round(mc, 2) if mc else None, "discharge_median_kwh": round(md, 2) if md else None,
            "learned": bool(kwh) and len(ch) >= MIN_RUNS and len(dis) >= MIN_RUNS}
