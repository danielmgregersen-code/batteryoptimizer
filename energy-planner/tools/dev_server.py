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


TREE_SHADE = {13: 0.6, 14: 0.75, 15: 0.5}   # local hours behind the trees (share of direct sun lost)


def synthetic_sun(t):
    """(Solcast p50, p90, actual) in kWh for a past hour: day-to-day cloudiness, trees at 13-15."""
    hl = t.astimezone(TZ).hour
    clear = SOLCAST_HOURLY[hl][2]
    k = 0.25 + 0.75 * abs(math.sin(t.toordinal() * 1.7))
    f = clear * k
    actual = f * (1 - TREE_SHADE.get(hl, 0.0) * k) * random.Random(int(t.timestamp())).uniform(0.9, 1.1)
    return f, clear, actual


def synthetic_submeters(t):
    """Heat pump, dishwasher, washer/dryer and hob (W, hourly mean) like the user's Shelly meters."""
    hp, dish, wash, hob = DEFAULTS["load_submeters"].split(",")
    lt = t.astimezone(TZ)
    day = lt.toordinal()
    return {
        hp: 250.0 if lt.month in (11, 12, 1, 2, 3) else 60.0,
        dish: 900.0 if lt.hour == 20 and day % 2 == 0 else 0.0,
        wash: (1800.0 if lt.month in (10, 11, 12, 1, 2, 3) else 600.0) if lt.hour in (10, 11) and day % 3 == 0 else 0.0,
        hob: 1100.0 if lt.hour == 17 else 300.0 if lt.hour == 12 else 0.0,
    }


class FakeHA:
    def __init__(self, car=True):
        today = datetime.now(TZ).date()
        d0, d1 = today.isoformat(), (today + timedelta(days=1)).isoformat()
        self.attrs = {
            DEFAULTS["price_buy_entity"]: {"raw_today": raw(d0, BUY_TODAY), "raw_tomorrow": None},
            DEFAULTS["price_sell_entity"]: {"raw_today": raw(d0, SELL_TODAY), "raw_tomorrow": None},
            "sensor.solcast_pv_forecast_forecast_today": solcast(d0),
            "sensor.solcast_pv_forecast_forecast_tomorrow": solcast(d1, 1.6),
            "sensor.solcast_pv_forecast_forecast_day_3": solcast((today + timedelta(days=2)).isoformat(), 0.7),
        }
        hour = datetime.now(TZ).hour
        self.states = {
            DEFAULTS["battery_soc_entity"]: "73",
            DEFAULTS["price_buy_entity"]: str(BUY_TODAY[hour]),
            DEFAULTS["price_sell_entity"]: str(SELL_TODAY[hour]),
            DEFAULTS["house_load_entity"]: "820",
            DEFAULTS["charger_power_entity"]: "0",
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
        for e, v in synthetic_submeters(datetime.now(TZ)).items():
            self.states[e] = str(v)
        self.states[DEFAULTS["load_submeters"].split(",")[2]] = "1950"     # the dryer runs right now
        for e in counter_ids(DEFAULTS).values():
            self.states[e] = "1000"
        for e, amps in zip(DEFAULTS["grid_current_entities"].split(","), ("21.4", "18.9", "19.6")):
            self.states[e] = amps
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
        if e.startswith("select."):
            return {"options": ["Enabled", "Disabled"]}
        return self.attrs.get(e)

    def number(self, e):
        try:
            return float(self.states[e])
        except (KeyError, ValueError):
            return None

    def text(self, e):
        return self.states.get(e)

    def call_service(self, domain, service, data=None, return_response=False):
        print(f"[fake HA] {domain}.{service} {data or ''}")
        if domain == "select":
            self.states[data["entity_id"]] = data["option"]
            return {}
        if domain == "number":
            self.states[data["entity_id"]] = str(data["value"])
            return {}
        if domain in ("button", "kia_uvo"):
            return {}
        if service == "get_dampening":
            return {"service_response": {"data": [{"site": "all", "damp_factor": ",".join(["1.0"] * 24)}]}}
        if service == "query_forecast_data":
            # Solcast's forecast history: 30-minute entries in kW, like detailedForecast
            start = datetime.fromisoformat(data["start_date_time"])
            end = datetime.fromisoformat(data["end_date_time"])
            out, t = [], start
            while t < end:
                f, clear, _ = synthetic_sun(t)
                for half in (0, 30):
                    out.append({"period_start": (t + timedelta(minutes=half)).isoformat(),
                                "pv_estimate": f, "pv_estimate10": f * 0.6, "pv_estimate90": clear})
                t += timedelta(hours=1)
            return {"changed_states": [], "service_response": {"data": out}}
        return {}

    def set_state(self, e, state, attributes=None):
        self.published[e] = (state, attributes)

    def statistics(self, ids, start, end, period="hour"):
        """Hourly means like HA's long-term statistics. As on the SPH10000TL3, the house load includes
        the car but not the battery's charging (that has its own sensor)."""
        rows = {i: [] for i in ids}
        load_id, car_id = DEFAULTS["house_load_entity"], DEFAULTS["car_power_entity"]
        bat_id, pv_id = DEFAULTS["battery_charge_power_entity"], DEFAULTS["pv_power_entity"]
        t = start.replace(minute=0, second=0, microsecond=0)
        while t < end:
            hl = t.astimezone(TZ).hour
            ms = t.timestamp() * 1000
            base = 450 + (900 if 17 <= hl < 21 else 350 if 6 <= hl < 17 else 150 if 21 <= hl < 23 else 0)
            bat = 4000.0 if 2 <= hl < 5 and t.day % 4 else 0.0
            car = 11000.0 if hl == 4 and t.day % 5 == 0 else 0.0
            pv = synthetic_sun(t)[2] * 1000
            subs = synthetic_submeters(t)
            for sid, v in subs.items():
                if sid in rows:
                    rows[sid].append({"start": ms, "mean": v})
            base += sum(subs.values())
            rows.get(load_id, []).append({"start": ms, "mean": base + random.uniform(-80, 120) + car})
            if car_id in rows:
                rows[car_id].append({"start": ms, "mean": car})
            if bat_id in rows:
                rows[bat_id].append({"start": ms, "mean": bat})
            if pv_id in rows:
                rows[pv_id].append({"start": ms, "mean": pv})
            self._economy_rows(rows, t, ms, hl, (base + car) / 1000.0, pv / 1000.0, bat / 1000.0)
            t += timedelta(hours=1)
        return rows

    @staticmethod
    def _economy_rows(rows, t, ms, hl, load, pv, grid_bat):
        """Counters (hourly change, kWh) and prices (hourly mean) for the economy tab."""
        ids = counter_ids(DEFAULTS)
        pv_load = min(pv, load)
        surplus = pv - pv_load
        pv_bat = surplus * 0.6
        bat_out = min(max(0.0, load - pv_load), 1.9) if 17 <= hl < 21 else 0.0
        imp = max(0.0, load - pv_load - bat_out) + grid_bat
        exp = surplus - pv_bat
        shift = 0.25 * math.sin(t.toordinal())
        change = {ids["import"]: imp, ids["export"]: exp, ids["solar"]: pv,
                  ids["battery_in"]: pv_bat + grid_bat, ids["battery_out"]: bat_out}
        for sid, v in change.items():
            if sid in rows:
                rows[sid].append({"start": ms, "change": round(v, 4)})
        for sid, base in ((DEFAULTS["price_buy_entity"], BUY_TODAY), (DEFAULTS["price_sell_entity"], SELL_TODAY)):
            if sid in rows:
                rows[sid].append({"start": ms, "mean": round(base[hl] + shift, 4)})


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
    ap.add_argument("--production", action="store_true", help="turn both production helpers on")
    ap.add_argument("--departure", action="store_true", help="a trip tomorrow 09:00 at 100 %%")
    ap.add_argument("--kia-pin-error", action="store_true",
                    help="car charging at 45 %%, Kia rejects calls with a PIN error (shows the fallback)")
    ap.add_argument("--data-dir", default=None)
    args = ap.parse_args()
    data = args.data_dir or tempfile.mkdtemp(prefix="energy-planner-")
    os.makedirs(data, exist_ok=True)
    os.environ.setdefault("TZ", "Europe/Copenhagen")

    import uvicorn

    import main as app_main
    from planner import Planner

    synthetic_log(os.path.join(data, "decisions.jsonl"))
    # No pv_history.json: the planner fetches Solcast's forecast history (query_forecast_data) itself.
    # The evening-before forecast only exists from 0.9.0 on: make up the last 10 days of it.
    dayahead, t = {}, (datetime.now(TZ) - timedelta(days=10)).replace(hour=0, minute=0, second=0, microsecond=0)
    while t < datetime.now(TZ):
        f, clear, _ = synthetic_sun(t)
        if f > 0.01:
            wobble = random.Random(int(t.timestamp()) // 86400).uniform(0.75, 1.3)
            dayahead[t.astimezone(timezone.utc).isoformat()] = {"p50": f * wobble, "p10": f * 0.6, "p90": clear,
                                                               "plan": f * wobble * 0.85, "at": ""}
        t += timedelta(hours=1)
    with open(os.path.join(data, "pv_dayahead.json"), "w") as fh:
        json.dump(dayahead, fh)
    fake = FakeHA(car=not args.no_car)
    for key in ("production_switch_entity", "car_production_switch_entity"):
        fake.states[DEFAULTS[key]] = "on" if args.production else "off"
    if args.departure:
        dep = (datetime.now(TZ) + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        fake.states[DEFAULTS["car_departure_entity"]] = dep.strftime("%Y-%m-%d %H:%M:%S")
        fake.attrs[DEFAULTS["car_departure_entity"]] = {"has_date": True, "has_time": True}
    if args.kia_pin_error:
        from homeassistant import ServiceCallError

        def ws(domain, service, data=None):
            print(f"[fake HA] ws {domain}.{service} -> PIN error")
            raise ServiceCallError("Failed to stop charge: PIN verification failed, ensure PIN is entered correctly.")
        fake.call_service_ws = ws
        fake.states[DEFAULTS["charger_status_entity"]] = "charging"
        fake.states[DEFAULTS["car_soc_entity"]] = "45"
        fake.states[DEFAULTS["car_production_switch_entity"]] = "on"
        fake.states[DEFAULTS["pv_power_entity"]] = "0"
    dev_opts = {**DEFAULTS, "car_device_id": "dev-car"}
    app_main.planner = Planner(fake, tz=TZ, log_path=os.path.join(data, "decisions.jsonl"),
                               car_log_path=os.path.join(data, "car_decisions.jsonl"), options_fn=lambda: dev_opts)
    app_main.planner.run_once()
    app_main.planner.car_cycle(datetime.now(UTC).replace(hour=19, minute=30) if args.kia_pin_error else None)
    print(f"data in {data}; open http://127.0.0.1:{args.port}/")
    uvicorn.run(app_main.app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
