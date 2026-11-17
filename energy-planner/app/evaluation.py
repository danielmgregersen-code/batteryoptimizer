"""Evaluation from the decision log: what actually happened and how good the forecasts were.

Every planning cycle logs the energy counters, the current prices, the plan's expectation for the
current hour (`pred`) and the register values. Between two consecutive entries:

    realised cost      = d(import) * buy - d(export) * sell             (at the earlier entry's prices)
    solar actual       = d(solar counter)        vs  pred.pv_kw * dt  (and the p50 / p10 forecasts)
    consumption actual = d(import) - d(export) + d(solar) + d(battery out) - d(battery in)
    base load actual   ~ consumption - car power sample * dt         (approximate: the car is sampled)

The planned mode is also compared with what the registers express (grid charging / battery first /
load first): from the dry-run era, when Node-RED set them. In production they follow the plan, so the
panel no longer shows it.
"""
from datetime import datetime

MAX_GAP_H = 1.0          # a longer gap between log entries is a restart: not counted
MAX_KWH_PER_GAP = 50.0   # larger counter jumps are resets / glitches

CATEGORY = {"grid_charge": "grid", "solar_charge": "battery_first", "hold": "battery_first",
            "discharge": "load_first"}


def _on(value) -> bool:
    return str(value).strip().lower() in ("enabled", "enable", "on", "true", "1")


def actual_category(registers: dict):
    """Node-RED's state from the registers it has written, or None if they are unknown."""
    if not registers:
        return None
    bf = registers.get("reg_battery_first")
    if bf in (None, "unknown", "unavailable"):
        return None
    if not _on(bf):
        return "load_first"
    return "grid" if _on(registers.get("reg_grid_charge")) else "battery_first"


def _delta(cb: dict, ca: dict, key: str):
    try:
        d = float(cb[key]) - float(ca[key])
    except (KeyError, TypeError, ValueError):
        return None
    return d if 0.0 <= d <= MAX_KWH_PER_GAP else None


def suggested_p10_weight(actual, p50, p10):
    """Least-squares weight w for (1-w)*p50 + w*p10 against the actual solar, clipped to 0..1."""
    num = sum((a - m) * (l - m) for a, m, l in zip(actual, p50, p10))
    den = sum((l - m) ** 2 for m, l in zip(p50, p10))
    if den < 1e-6:
        return None
    return round(min(max(num / den, 0.0), 1.0), 2)


def evaluate(decisions: list, counters: dict, tz) -> dict:
    """counters: {"import", "export", "battery_in", "battery_out", "solar"} -> entity ids.

    Returns {"days": [...], "summary": {...}, "timeline": [...]} (days in chronological order).
    """
    days: dict = {}
    timeline = []
    pv_a, pv_m, pv_l = [], [], []

    def day_entry(day):
        return days.setdefault(day, {
            "day": day, "hours": 0.0, "import_kwh": 0.0, "export_kwh": 0.0, "cost_dkk": 0.0,
            "buy_weighted": 0.0, "buy_hours": 0.0,
            "battery_in_kwh": 0.0, "battery_out_kwh": 0.0,
            "pv_actual_kwh": 0.0, "pv_plan_kwh": 0.0, "pv_p50_kwh": 0.0, "pv_p10_kwh": 0.0, "pv_hours": 0.0,
            "load_actual_kwh": 0.0, "load_pred_kwh": 0.0, "load_hours": 0.0,
            "agree_hours": 0.0, "compare_hours": 0.0,
        })

    for a, b in zip(decisions, decisions[1:]):
        try:
            ta = datetime.fromisoformat(a["updated"])
            tb = datetime.fromisoformat(b["updated"])
        except (KeyError, TypeError, ValueError):
            continue
        dt = (tb - ta).total_seconds() / 3600.0
        if not 0.0 < dt <= MAX_GAP_H:
            continue
        day = ta.astimezone(tz).date().isoformat()
        e = day_entry(day)
        e["hours"] += dt
        ca, cb = a.get("counters") or {}, b.get("counters") or {}
        price = a.get("price_now") or {}
        pred = a.get("pred") or {}

        planned = CATEGORY.get(a.get("mode"))
        actual = actual_category(a.get("actual_registers"))
        if planned and actual:
            e["compare_hours"] += dt
            if planned == actual:
                e["agree_hours"] += dt
        timeline.append({"start": a["updated"], "end": b["updated"], "planned": planned, "actual": actual})

        buy, sell = price.get("buy"), price.get("sell")
        if buy is not None:
            e["buy_weighted"] += float(buy) * dt
            e["buy_hours"] += dt
        d_imp, d_exp = _delta(cb, ca, counters.get("import")), _delta(cb, ca, counters.get("export"))
        if d_imp is not None and d_exp is not None and buy is not None and sell is not None:
            e["import_kwh"] += d_imp
            e["export_kwh"] += d_exp
            e["cost_dkk"] += d_imp * float(buy) - d_exp * float(sell)

        d_in, d_out = _delta(cb, ca, counters.get("battery_in")), _delta(cb, ca, counters.get("battery_out"))
        if d_in is not None and d_out is not None:
            e["battery_in_kwh"] += d_in
            e["battery_out_kwh"] += d_out

        d_pv = _delta(cb, ca, counters.get("solar"))
        if d_pv is not None and "pv_kw" in pred:
            e["pv_actual_kwh"] += d_pv
            e["pv_plan_kwh"] += pred["pv_kw"] * dt
            e["pv_p50_kwh"] += pred.get("pv_p50_kw", pred["pv_kw"]) * dt
            e["pv_p10_kwh"] += pred.get("pv_p10_kw", pred["pv_kw"]) * dt
            e["pv_hours"] += dt
            pv_a.append(d_pv)
            pv_m.append(pred.get("pv_p50_kw", pred["pv_kw"]) * dt)
            pv_l.append(pred.get("pv_p10_kw", pred["pv_kw"]) * dt)

        if None not in (d_imp, d_exp, d_pv, d_in, d_out) and "load_kw" in pred:
            consumption = d_imp - d_exp + d_pv + d_out - d_in
            car_w = (a.get("samples") or {}).get("car_w") or 0.0
            e["load_actual_kwh"] += max(0.0, consumption - max(0.0, car_w) / 1000.0 * dt)
            e["load_pred_kwh"] += pred["load_kw"] * dt
            e["load_hours"] += dt

    out_days = []
    for day in sorted(days):
        e = days[day]
        out = {
            "day": day,
            "hours": round(e["hours"], 2),
            "import_kwh": round(e["import_kwh"], 2),
            "export_kwh": round(e["export_kwh"], 2),
            "cost_dkk": round(e["cost_dkk"], 2),
            "avg_buy_market": round(e["buy_weighted"] / e["buy_hours"], 3) if e["buy_hours"] else None,
            "avg_buy_paid": None,
            "battery_in_kwh": round(e["battery_in_kwh"], 2),
            "battery_out_kwh": round(e["battery_out_kwh"], 2),
            "pv_actual_kwh": round(e["pv_actual_kwh"], 2) if e["pv_hours"] else None,
            "pv_plan_kwh": round(e["pv_plan_kwh"], 2) if e["pv_hours"] else None,
            "pv_p50_kwh": round(e["pv_p50_kwh"], 2) if e["pv_hours"] else None,
            "pv_p10_kwh": round(e["pv_p10_kwh"], 2) if e["pv_hours"] else None,
            "load_actual_kwh": round(e["load_actual_kwh"], 2) if e["load_hours"] else None,
            "load_pred_kwh": round(e["load_pred_kwh"], 2) if e["load_hours"] else None,
            "agreement_pct": round(100.0 * e["agree_hours"] / e["compare_hours"], 1) if e["compare_hours"] else None,
        }
        out_days.append(out)

    # Average price actually paid per imported kWh (import-weighted), per day.
    _import_weighted_prices(decisions, counters, tz, out_days)

    totals = {
        "days": len(out_days),
        "cost_dkk": round(sum(d["cost_dkk"] for d in out_days), 2),
        "import_kwh": round(sum(d["import_kwh"] for d in out_days), 2),
        "export_kwh": round(sum(d["export_kwh"] for d in out_days), 2),
    }
    hours_cmp = sum(days[d]["compare_hours"] for d in days)
    totals["agreement_pct"] = (round(100.0 * sum(days[d]["agree_hours"] for d in days) / hours_cmp, 1)
                               if hours_cmp else None)
    pv_act = sum(pv_a)
    pv_plan = sum(days[d]["pv_plan_kwh"] for d in days)
    totals["pv_actual_kwh"] = round(pv_act, 2)
    totals["pv_plan_kwh"] = round(pv_plan, 2)
    totals["pv_bias_pct"] = round(100.0 * (pv_plan - pv_act) / pv_act, 1) if pv_act > 0.5 else None
    totals["suggested_p10_weight"] = suggested_p10_weight(pv_a, pv_m, pv_l) if pv_act > 0.5 else None
    totals["current_p10_weight"] = (decisions[-1].get("pv_p10_weight") if decisions else None)
    return {"days": out_days, "summary": totals, "timeline": _merge_runs(timeline)}


def _import_weighted_prices(decisions, counters, tz, out_days):
    by_day = {d["day"]: d for d in out_days}
    acc: dict = {}
    for a, b in zip(decisions, decisions[1:]):
        try:
            ta, tb = datetime.fromisoformat(a["updated"]), datetime.fromisoformat(b["updated"])
        except (KeyError, TypeError, ValueError):
            continue
        if not 0.0 < (tb - ta).total_seconds() / 3600.0 <= MAX_GAP_H:
            continue
        d_imp = _delta(b.get("counters") or {}, a.get("counters") or {}, counters.get("import"))
        buy = (a.get("price_now") or {}).get("buy")
        if d_imp is None or buy is None:
            continue
        day = ta.astimezone(tz).date().isoformat()
        s = acc.setdefault(day, [0.0, 0.0])
        s[0] += d_imp * float(buy)
        s[1] += d_imp
    for day, (cost, kwh) in acc.items():
        if day in by_day and kwh > 0.05:
            by_day[day]["avg_buy_paid"] = round(cost / kwh, 3)


def _merge_runs(timeline: list) -> list:
    """Collapse consecutive intervals with the same (planned, actual) into runs."""
    runs = []
    for seg in timeline:
        if runs and runs[-1]["end"] == seg["start"] and runs[-1]["planned"] == seg["planned"] \
                and runs[-1]["actual"] == seg["actual"]:
            runs[-1]["end"] = seg["end"]
        else:
            runs.append(dict(seg))
    return runs
