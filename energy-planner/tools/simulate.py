"""Offline run of the optimiser on real sensor data (no Home Assistant needed).

    python tools/simulate.py                 # flat 0.8 kW load (what the add-on assumes before it has statistics)
    python tools/simulate.py --shaped        # example load shape: low at night, peak in the evening
"""
import argparse
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))
sys.path.insert(0, os.path.join(HERE, "..", "tests"))

from fixtures import BUY_TODAY, SELL_TODAY, price_attrs, solcast_attrs  # noqa: E402
from inputs import build_horizon, parse_prices, parse_solcast  # noqa: E402
from optimizer import solve, terminal_value_for  # noqa: E402
from planner import battery_params  # noqa: E402
from settings import DEFAULTS  # noqa: E402
from setpoints import derive_setpoints  # noqa: E402

TZ = ZoneInfo("Europe/Copenhagen")


def shaped(hour_local):
    if 17 <= hour_local < 21:
        return 1.8
    if 6 <= hour_local < 17:
        return 1.0
    if 21 <= hour_local < 23:
        return 1.2
    return 0.5


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shaped", action="store_true")
    ap.add_argument("--soc", type=float, default=73.0)
    ap.add_argument("--now", default="2026-11-14T12:30:00+01:00")
    ap.add_argument("--p10", type=float, default=DEFAULTS["pv_p10_weight"])
    args = ap.parse_args()

    now = datetime.fromisoformat(args.now).astimezone(timezone.utc)
    buy, sell = parse_prices(price_attrs(BUY_TODAY)), parse_prices(price_attrs(SELL_TODAY))
    pv, _, _ = parse_solcast([solcast_attrs()], args.p10)
    load_fn = (lambda h: shaped(h.astimezone(TZ).hour)) if args.shaped else (lambda h: DEFAULTS["default_load_kw"])

    hz = build_horizon(now, buy, sell, pv, load_fn)
    p = battery_params(DEFAULTS)
    soc0 = p.capacity_kwh * args.soc / 100
    tv = terminal_value_for(hz.buy, p)
    plan = solve(hz.buy, hz.sell, hz.pv, hz.load, hz.dt, soc0, p, tv)
    solar_only = solve(hz.buy, hz.sell, hz.pv, hz.load, hz.dt, soc0, p, tv, allow_grid_charge=False)

    print(f"{'hour':>5} {'buy':>5} {'sell':>5} {'pv':>5} {'load':>5} | {'pv>bat':>6} {'grid>bat':>8} {'bat>house':>9} {'export':>6} {'grid>house':>10} | {'SoC%':>4}")
    for i, t in enumerate(hz.starts):
        print(f"{t.astimezone(TZ).strftime('%H:%M'):>5} {plan.buy[i]:5.2f} {plan.sell[i]:5.2f} {plan.pv[i]:5.2f} {plan.load[i]:5.2f} | "
              f"{plan.pv_bat[i]:6.2f} {plan.grid_bat[i]:8.2f} {plan.bat_load[i]:9.2f} {plan.pv_exp[i]:6.2f} {plan.grid_load[i]:10.2f} | "
              f"{plan.soc_end[i] / p.capacity_kwh * 100:4.0f}")
    sp = derive_setpoints(plan, args.soc, p, DEFAULTS["inverter_kw"], DEFAULTS["soc_floor_pct"], DEFAULTS["soc_ceiling_pct"])
    print("\nnow:", sp)
    print(f"net cost {plan.net_cost:.2f} DKK | solar-only {solar_only.net_cost:.2f} DKK | saving {solar_only.net_cost - plan.net_cost:.2f} DKK")


if __name__ == "__main__":
    main()
