"""Would a bigger house battery pay? The last year's hours replayed through the planner's optimisation.

Per hour from HA's long-term statistics (economy.compute): the solar production, the house's
consumption (car included) and the hourly buy and sell price. For each local day with all its
hours the LP is solved over the day plus LOOKAHEAD_H hours with what actually came (a perfect
forecast), the day's hours are kept and the battery's SoC carried on. Done for the capacity the plan
uses now and for 1..N extra modules; the difference in net cost (import x buy - export x sell +
battery wear) is what a bigger battery would have saved. A perfect forecast makes every size look a
little better than in practice; the difference between sizes is what counts.
"""
from datetime import datetime

import numpy as np

from optimizer import solve, terminal_value_for

LOOKAHEAD_H = 12
MIN_DAYS_FULL_YEAR = 300


def _days(hours: list) -> list:
    """Hours (sorted, each with "t" local iso) -> [(day, [indices])] of complete local days only, plus
    the number of incomplete days."""
    by_day: dict = {}
    for i, h in enumerate(hours):
        by_day.setdefault(h["t"][:10], []).append(i)
    full, skipped = [], 0
    for day, idx in sorted(by_day.items()):
        if len(idx) >= 23:
            full.append((day, idx))
        else:
            skipped += 1
    return full, skipped


def simulate(hours: list, params, lookahead_h: int = LOOKAHEAD_H, progress=None) -> dict:
    """-> {net_cost_dkk, import_kwh, export_kwh, discharge_kwh, wear_dkk, days, skipped_days}."""
    days, skipped = _days(hours)
    buy = np.array([float(h["buy"]) for h in hours])
    sell = np.array([float(h["sell"]) for h in hours])
    pv = np.array([max(0.0, float(h["solar_kwh"])) for h in hours])
    load = np.array([max(0.0, float(h["consumption_kwh"])) for h in hours])
    ts = [datetime.fromisoformat(h["t"]).timestamp() for h in hours]
    soc = params.soc_floor_kwh
    last_end = None
    tot = {"net_cost_dkk": 0.0, "import_kwh": 0.0, "export_kwh": 0.0, "discharge_kwh": 0.0, "wear_dkk": 0.0}
    for n, (day, idx) in enumerate(days):
        a, b = idx[0], idx[-1] + 1
        if last_end is None or a != last_end or ts[a] - ts[a - 1] > 3700:
            soc = params.soc_floor_kwh                 # first day or a gap: start empty (no free energy)
        end = b
        while end < len(hours) and end < b + lookahead_h and ts[end] - ts[end - 1] <= 3700:
            end += 1                                    # the look-ahead, as long as the hours run on
        sl = slice(a, end)
        soc = min(max(soc, params.soc_floor_kwh), params.soc_ceiling_kwh)
        pl = solve(buy[sl], sell[sl], pv[sl], load[sl], np.ones(end - a), soc, params,
                   terminal_value_for(buy[sl], params))
        k = b - a                                       # the day's own hours
        imp = pl.grid_load[:k] + pl.grid_bat[:k]
        exp = pl.pv_exp[:k]
        dis = pl.bat_load[:k]
        wear = float(dis.sum()) * float(params.wear_dkk_per_kwh)
        tot["net_cost_dkk"] += float((imp * buy[a:b]).sum() - (exp * sell[a:b]).sum()) + wear
        tot["import_kwh"] += float(imp.sum())
        tot["export_kwh"] += float(exp.sum())
        tot["discharge_kwh"] += float(dis.sum())
        tot["wear_dkk"] += wear
        soc = float(pl.soc_end[k - 1])
        last_end = b
        if progress:
            progress(n + 1, len(days))
    return {**{k: round(v, 2) for k, v in tot.items()}, "days": len(days), "skipped_days": skipped}


def sizes(base_kwh: float, module_kwh: float, n_max: int = 4) -> list:
    return [round(base_kwh + n * module_kwh, 2) for n in range(n_max + 1)]


def report(hours: list, params_for, base_kwh: float, module_kwh: float, measured_net_dkk: float = None,
           n_max: int = 4, progress=None) -> dict:
    """params_for(capacity) -> BatteryParams. -> {sizes: [...], days, skipped_days, warning, ...}."""
    caps = sizes(base_kwh, module_kwh, n_max)
    results = []
    for j, cap in enumerate(caps):
        p = params_for(cap)
        r = simulate(hours, p, progress=(lambda i, n, j=j: progress((j * n + i) / (len(caps) * n)))
                     if progress else None)
        results.append({"modules": j, "kwh": cap, **r})
    days = results[0]["days"]
    scale = 365.0 / days if days else 0.0
    base = results[0]
    for r in results:
        r["net_cost_per_year_dkk"] = round(r["net_cost_dkk"] * scale, 0)
        r["extra_saving_per_year_dkk"] = round((base["net_cost_dkk"] - r["net_cost_dkk"]) * scale, 0)
        r["cycles_per_year"] = round(r["discharge_kwh"] / r["kwh"] * scale, 0) if r["kwh"] else None
        r["export_kwh_per_year"] = round(r["export_kwh"] * scale, 0)
        r["import_kwh_per_year"] = round(r["import_kwh"] * scale, 0)
    first = next((h["t"] for h in hours), None)
    last = hours[-1]["t"] if hours else None
    warning = None
    if days < MIN_DAYS_FULL_YEAR:
        warning = (f"Kun {days} dages data - årstallene er skaleret op og dækker ikke alle årstider "
                   "(vinteren giver mindre sol og ofte større prisforskelle).")
    return {"sizes": results, "days": days, "skipped_days": base["skipped_days"], "start": first, "end": last,
            "measured_net_dkk": None if measured_net_dkk is None else round(measured_net_dkk, 0),
            "simulated_net_dkk": round(base["net_cost_dkk"], 0), "warning": warning,
            "computed_at": datetime.now().astimezone().isoformat(timespec="minutes")}
