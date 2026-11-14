"""Run the panel locally against a fake Home Assistant (no HA needed) - for working on the UI.

    python tools/dev_server.py [--port 8010] [--no-car] [--data-dir /tmp/ep]

Uses the real 2026-11-14 prices and Solcast forecast from the tests, a connected car at 30 % and
14 days of synthetic decision log so the evaluation tab has something to show. Nothing here is
used by the add-on itself.
"""
import argparse
import json
import math
import os
import random
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))
sys.path.insert(0, os.path.join(HERE, "..", "tests"))

from fixtures import BUY_TODAY, SELL_TODAY, SOLCAST_HOURLY  # noqa: E402
from settings import DEFAULTS, counter_ids  # noqa: E402

TZ = ZoneInfo("Europe/Copenhagen")
UTC = timezone.utc


def raw(day, values):
    return [{"hour": f"{day}T{h:02d}:00:00+01:00", "price": p} for h, p in enumerate(values)]


def solcast(day, scale=1.0):
    return {"detailedHourly": [
        {"period_start": f"{day}T{h:02d}:00:00+01:00", "pv_estimate": a * scale,
         "pv_estimate10": b * scale, "pv_estimate90": c * scale}
        for h, (a, b, c) in enumerate(SOLCAST_HOURLY)]}


class FakeHA:
    def __init__(self, car=True):
        today = datetime.now(TZ).date()
        d0, d1 = today.isoformat(), (today + timedelta(days=1)).isoformat()
        self.attrs = {
            DEFAULTS["price_buy_entity"]: {"raw_today": raw(d0, BUY_TODAY), "raw_tomorrow": None},
            DEFAULTS["price_sell_entity"]: {"raw_today": raw(d0, SELL_TODAY), "raw_tomorrow": None},
            "sensor.solcast_pv_forecast_forecast_today": solcast(d0),
            "sensor.solcast_pv_forecast_forecast_tomorrow": solcast(d1, 1.6),
        }
        hour = datetime.now(TZ).hour
        self.states = {
            DEFAULTS["battery_soc_entity"]: "73",
            DEFAULTS["price_buy_entity"]: str(BUY_TODAY[hour]),
            DEFAULTS["price_sell_entity"]: str(SELL_TODAY[hour]),
            DEFAULTS["house_load_entity"]: "820",
            DEFAULTS["car_power_entity"]: "0",
            DEFAULTS["pv_power_entity"]: "1150",
            DEFAULTS["battery_charge_power_entity"]: "300",
            "select.growatt_battery_first_time_2": "Enabled",
            "number.growatt_battery_first_maximum_soc": "100",
            "number.growatt_load_first_battery_minimum_soc": "10",
            "number.growatt_battery_first_charge_rate": "100",
            "select.growatt_battery_first_charge_from_grid": "Disabled",
            "number.growatt_grid_export_limit": "100",
        }
        for e in DEFAULTS["solcast_entities"].split(","):
            self.states[e] = "5.27"
        for e in counter_ids(DEFAULTS).values():
            self.states[e] = "1000"
        if car:
            self.states.update({
                DEFAULTS["charger_status_entity"]: "suspended_evse",
                DEFAULTS["car_soc_entity"]: "30",
                DEFAULTS["charger_current_entity"]: "16",
                DEFAULTS["charger_start_button"]: "2026-11-14T05:02:11+00:00",
            })
        self.published = {}

    def state(self, e):
        if e not in self.states:
            return None
        return {"state": self.states[e], "attributes": self.attrs.get(e, {}),
                "last_changed": (datetime.now(UTC) - timedelta(minutes=47)).isoformat()}

    def attributes(self, e):
        return self.attrs.get(e)

    def number(self, e):
        try:
            return float(self.states[e])
        except (KeyError, ValueError):
            return None

    def text(self, e):
        return self.states.get(e)

    def set_state(self, e, state, attributes=None):
        self.published[e] = (state, attributes)

    def statistics(self, ids, start, end, period="hour"):
        rows = {i: [] for i in ids}
        t = start.replace(minute=0, second=0, microsecond=0)
        while t < end:
            hl = t.astimezone(TZ).hour
            base = 450 + (900 if 17 <= hl < 21 else 350 if 6 <= hl < 17 else 150 if 21 <= hl < 23 else 0)
            rows[ids[0]].append({"start": t.timestamp() * 1000, "mean": base + random.uniform(-80, 120)})
            t += timedelta(hours=1)
        return rows


def synthetic_log(path, days=14):
    """Plausible decision-log entries every 15 min: counters, prices, forecasts, both sides' modes."""
    rnd = random.Random(7)
    ids = counter_ids(DEFAULTS)
    c = {ids["import"]: 5200.0, ids["export"]: 3100.0, ids["solar"]: 9800.0,
         ids["battery_in"]: 6580.0, ids["battery_out"]: 6486.0}
    now = datetime.now(TZ).replace(second=0, microsecond=0)
    t = (now - timedelta(days=days)).replace(minute=0)
    soc = 50.0
    lines = []
    while t < now - timedelta(minutes=15):
        hl = t.hour
        day_shift = 0.25 * math.sin(t.toordinal())
        buy = BUY_TODAY[hl] + day_shift + rnd.uniform(-0.05, 0.05)
        sell = SELL_TODAY[hl] + day_shift * 0.8
        sun = SOLCAST_HOURLY[hl]
        weather = 0.4 + 1.4 * abs(math.sin(t.toordinal() * 1.3))
        pv_kw = sun[0] * weather * rnd.uniform(0.8, 1.2)
        load_kw = 0.5 + (1.3 if 17 <= hl < 21 else 0.5 if 6 <= hl < 17 else 0.0) + rnd.uniform(0, 0.3)
        planned = ("grid_charge" if hl in (3, 4) and day_shift < 0 else "discharge" if 17 <= hl < 21
                   else "solar_charge" if sun[0] > 0.3 else "hold")
        nr_grid = hl in (2, 3, 4)
        nr_bf = nr_grid or (8 <= hl < 16)
        regs = {"reg_battery_first": "Enabled" if nr_bf else "Disabled",
                "reg_grid_charge": "Enabled" if nr_grid else "Disabled",
                "reg_max_soc": "80", "reg_min_soc": "10", "reg_charge_rate": "100", "reg_export_limit": "100"}
        dt = 0.25
        bat_in = (3.5 if nr_grid else min(pv_kw, 3.0) if nr_bf else 0.0) * dt
        bat_out = (min(load_kw, 4.0) if not nr_bf and soc > 12 else 0.0) * dt
        soc = max(10.0, min(100.0, soc + (bat_in * 0.97 - bat_out / 0.95) / 7.75 * 100))
        net = load_kw * dt + bat_in - bat_out - pv_kw * dt
        imp, exp = max(0.0, net), max(0.0, -net)
        entry = {
            "updated": t.isoformat(timespec="seconds"), "soc_pct": round(soc, 1), "mode": planned,
            "setpoints": {"mode": planned, "max_soc_pct": 90 if planned.endswith("charge") else None,
                          "min_soc_pct": 10, "charge_rate_pct": 40 if planned == "grid_charge" else 100},
            "actual_registers": regs, "counters": dict(c),
            "price_now": {"buy": round(buy, 4), "sell": round(sell, 4)},
            "pred": {"pv_kw": round(sun[0] * 0.79 + sun[1] * 0.21, 3), "pv_p50_kw": sun[0], "pv_p10_kw": sun[1],
                     "load_kw": round(load_kw * rnd.uniform(0.85, 1.1), 3)},
            "samples": {"house_load_w": load_kw * 1000, "car_w": 11000.0 if hl == 4 and t.day % 3 == 0 else 0.0},
            "pv_p10_weight": 0.3, "savings_vs_solar_only_dkk": 0.0, "horizon_hours": 30, "car": None,
        }
        car_kwh = (11.0 * dt) if entry["samples"]["car_w"] else 0.0
        c[ids["import"]] += imp + car_kwh
        c[ids["export"]] += exp
        c[ids["solar"]] += pv_kw * dt
        c[ids["battery_in"]] += bat_in
        c[ids["battery_out"]] += bat_out
        lines.append(json.dumps(entry))
        t += timedelta(minutes=15)
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8010)
    ap.add_argument("--no-car", action="store_true")
    ap.add_argument("--data-dir", default=None)
    args = ap.parse_args()
    data = args.data_dir or tempfile.mkdtemp(prefix="energy-planner-")
    os.makedirs(data, exist_ok=True)
    os.environ.setdefault("TZ", "Europe/Copenhagen")

    import uvicorn

    import main as app_main
    from planner import Planner

    synthetic_log(os.path.join(data, "decisions.jsonl"))
    fake = FakeHA(car=not args.no_car)
    app_main.planner = Planner(fake, tz=TZ, log_path=os.path.join(data, "decisions.jsonl"),
                               car_log_path=os.path.join(data, "car_decisions.jsonl"))
    app_main.planner.run_once()
    app_main.planner.car_cycle()
    print(f"data in {data}; open http://127.0.0.1:{args.port}/")
    uvicorn.run(app_main.app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
