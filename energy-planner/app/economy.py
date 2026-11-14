"""What the installation has cost and earned, hour by hour, from HA's long-term statistics.

Per hour (kWh from the counters' hourly `change`, prices from the price sensors' hourly `mean`):

    consumption     = import - export + solar + battery out - battery in      (house incl. car)
    net cost        = import x buy - export x sell                            (what was paid)
    without system  = consumption x buy                                        (no panels, no battery)
    system value    = without system - net cost                               (what the system saved)

    sun -> battery  = min(battery in, solar);  grid -> battery = battery in - sun -> battery
    battery value   = battery out x buy - grid -> battery x buy - sun -> battery x sell
                      (energy used at the hour's price, minus what charging it cost, including the
                       sale the sun could have made instead)
    solar value     = system value - battery value                             (so the parts add up)

    car             = car kWh x buy                                            ("at the buy price")
    self-sufficiency = 1 - (import - grid -> battery) / consumption

Hours without a price fall back to the price the add-on logged in that hour; hours without counters
or any price are left out and counted.
"""
from datetime import datetime, timedelta

from inputs import parse_ts

HOURLY_MAX_DAYS = 7          # up to this period the chart shows hours, above it days


def _series(rows, key):
    out = {}
    for r in rows or []:
        v = r.get(key)
        if v is None:
            continue
        try:
            out[parse_ts(r["start"])] = float(v)
        except (KeyError, TypeError, ValueError):
            continue
    return out


def logged_prices(decisions: list) -> dict:
    """{utc_hour: (buy, sell)} averaged from the decision log's price_now (fallback for gaps)."""
    acc: dict = {}
    for e in decisions or []:
        p = e.get("price_now") or {}
        try:
            t = datetime.fromisoformat(e["updated"])
            buy, sell = float(p["buy"]), float(p["sell"])
        except (KeyError, TypeError, ValueError):
            continue
        h = parse_ts(t).replace(minute=0, second=0, microsecond=0)
        a = acc.setdefault(h, [0.0, 0.0, 0])
        a[0] += buy
        a[1] += sell
        a[2] += 1
    return {h: (a[0] / a[2], a[1] / a[2]) for h, a in acc.items()}


def compute(stats: dict, ids: dict, tz, decisions: list = None, hourly: bool = True) -> dict:
    """ids: {"import", "export", "solar", "battery_in", "battery_out", "buy", "sell", "car"} -> entity ids.

    Returns {"hours": [...] (if hourly), "days": [...], "totals": {...}, "missing": [...], "skipped_hours": n}.
    """
    get = lambda key, typ: _series(stats.get(ids.get(key) or "", []), typ)  # noqa: E731
    imp, exp = get("import", "change"), get("export", "change")
    sol, bi, bo = get("solar", "change"), get("battery_in", "change"), get("battery_out", "change")
    buy, sell = get("buy", "mean"), get("sell", "mean")
    car_w = get("car", "mean")
    fallback = logged_prices(decisions)
    missing = [ids[k] for k in ("import", "export", "solar", "battery_in", "battery_out", "buy", "sell")
               if ids.get(k) and not stats.get(ids[k])]

    hours, skipped, fallback_used = [], 0, 0
    for t in sorted(set(imp) | set(exp)):
        if t not in imp or t not in exp:
            skipped += 1
            continue
        b, s = buy.get(t), sell.get(t)
        if (b is None or s is None) and t in fallback:
            fb, fs = fallback[t]
            b = fb if b is None else b
            s = fs if s is None else s
            fallback_used += 1
        if b is None or s is None:
            skipped += 1
            continue
        i, x = max(0.0, imp[t]), max(0.0, exp[t])
        so, bin_, bout = max(0.0, sol.get(t, 0.0)), max(0.0, bi.get(t, 0.0)), max(0.0, bo.get(t, 0.0))
        cons = max(0.0, i - x + so + bout - bin_)
        net = i * b - x * s
        without = cons * b
        value = without - net
        sun_bat = min(bin_, so)
        grid_bat = bin_ - sun_bat
        bat_value = bout * b - grid_bat * b - sun_bat * s
        car_kwh = max(0.0, car_w.get(t, 0.0)) / 1000.0
        hours.append({
            "t": t.astimezone(tz).isoformat(timespec="minutes"),
            "import_kwh": i, "export_kwh": x, "solar_kwh": so, "battery_in_kwh": bin_, "battery_out_kwh": bout,
            "consumption_kwh": cons, "grid_to_battery_kwh": grid_bat, "car_kwh": car_kwh, "buy": b, "sell": s,
            "bought_dkk": i * b, "sold_dkk": x * s, "net_dkk": net, "without_dkk": without,
            "value_dkk": value, "battery_value_dkk": bat_value, "solar_value_dkk": value - bat_value,
            "car_dkk": car_kwh * b,
        })

    days: dict = {}
    sums = ("import_kwh", "export_kwh", "solar_kwh", "battery_in_kwh", "battery_out_kwh", "consumption_kwh",
            "grid_to_battery_kwh", "car_kwh", "bought_dkk", "sold_dkk", "net_dkk", "without_dkk", "value_dkk",
            "battery_value_dkk", "solar_value_dkk", "car_dkk")
    for h in hours:
        d = days.setdefault(h["t"][:10], {"day": h["t"][:10], "hours": 0, "buy_sum": 0.0, **{k: 0.0 for k in sums}})
        d["hours"] += 1
        d["buy_sum"] += h["buy"]
        for k in sums:
            d[k] += h[k]
    out_days = [_finish(d) for _, d in sorted(days.items())]

    tot = {"hours": len(hours), "buy_sum": sum(h["buy"] for h in hours), **{k: sum(h[k] for h in hours) for k in sums}}
    totals = _finish(tot)
    totals["days"] = len(out_days)
    totals["fallback_price_hours"] = fallback_used

    result = {"days": out_days, "totals": totals, "missing": missing, "skipped_hours": skipped}
    if hourly:
        result["hours"] = [{k: (round(v, 3) if isinstance(v, float) else v) for k, v in h.items()} for h in hours]
    return result


def _finish(d: dict) -> dict:
    out = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in d.items() if k != "buy_sum"}
    n = d.get("hours") or 0
    out["avg_buy_market"] = round(d["buy_sum"] / n, 3) if n else None
    out["avg_buy_paid"] = round(d["bought_dkk"] / d["import_kwh"], 3) if d["import_kwh"] > 0.05 else None
    cons = d["consumption_kwh"]
    # Share of the consumption not taken from the grid. Grid energy that went into the battery is not
    # consumption (it comes back later as battery output, which counts as self-supplied).
    grid_to_house = max(0.0, d["import_kwh"] - d.get("grid_to_battery_kwh", 0.0))
    out["self_sufficiency_pct"] = (round(min(max(100.0 * (1 - grid_to_house / cons), 0.0), 100.0), 1)
                                   if cons > 0.05 else None)
    out["solar_self_use_pct"] = round(100.0 * (1 - d["export_kwh"] / d["solar_kwh"]), 1) if d["solar_kwh"] > 0.05 else None
    return out


def entity_ids(opts: dict) -> dict:
    return {"import": opts["grid_import_counter"], "export": opts["grid_export_counter"],
            "solar": opts["solar_counter"], "battery_in": opts["battery_input_counter"],
            "battery_out": opts["battery_output_counter"], "buy": opts["price_buy_entity"],
            "sell": opts["price_sell_entity"], "car": opts.get("car_power_entity")}


def report(ha, opts: dict, tz, days: int, now: datetime, decisions: list = None) -> dict:
    """Fetch the statistics for the last `days` days (from local midnight) and compute."""
    ids = entity_ids(opts)
    local_now = now.astimezone(tz)
    start = (local_now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    stats = ha.statistics([v for v in dict.fromkeys(ids.values()) if v], start, now, "hour")
    res = compute(stats, ids, tz, decisions, hourly=days <= HOURLY_MAX_DAYS)
    res.update(period_days=days, start=start.isoformat(timespec="minutes"),
               end=local_now.isoformat(timespec="minutes"))
    return res
