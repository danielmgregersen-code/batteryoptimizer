"""One planning cycle: read HA -> build horizon -> solve -> setpoints -> publish + log (dry-run)."""
import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone

import car as carmod
from inputs import UTC, build_horizon, hour_floor, parse_prices, parse_solcast, persistence_forecast
from optimizer import BatteryParams, solve, terminal_value_for
from loadprofile import build_profile, load_for_hour, profile_view
from settings import REGISTER_KEYS, counter_ids, load_options, local_tz, split_csv
from setpoints import derive_setpoints, step_mode
from evaluation import evaluate
from summary import CAR_ACTION_LABEL, reason_da, summary_attributes, upcoming

log = logging.getLogger("energy_planner")

DECISION_LOG = "/data/decisions.jsonl"
CAR_LOG = "/data/car_decisions.jsonl"
MAX_LOG_LINES = 4000
KEEP_LOG_LINES = 2000
PROFILE_MAX_AGE = timedelta(hours=6)


class PlannerError(Exception):
    pass


def battery_params(opts: dict) -> BatteryParams:
    cap = float(opts["battery_capacity_kwh"])
    return BatteryParams(
        capacity_kwh=cap,
        max_power_kw=float(opts["battery_max_power_kw"]),
        soc_floor_kwh=cap * float(opts["soc_floor_pct"]) / 100.0,
        soc_ceiling_kwh=cap * float(opts["soc_ceiling_pct"]) / 100.0,
        eta_pv_bat=float(opts["eta_pv_to_battery"]),
        eta_grid_bat=float(opts["eta_grid_to_battery"]),
        eta_discharge=float(opts["eta_battery_to_load"]),
        wear_dkk_per_kwh=float(opts["wear_cost_dkk_per_kwh"]),
    )


class Planner:
    def __init__(self, ha, tz=None, log_path: str = DECISION_LOG, options_fn=load_options,
                 car_log_path: str = CAR_LOG):
        self.ha = ha
        self.tz = tz or local_tz()
        self.log_path = log_path
        self.car_log_path = car_log_path
        self.car_latest = None
        self._car_last_logged = None
        self.options_fn = options_fn
        self._profile = None
        self._profile_at = None
        self._lock = threading.Lock()        # guards latest / last_error
        self._run_lock = threading.Lock()    # one planning cycle at a time (loop vs. "run now")
        self.latest = None
        self.last_error = None
        self.entity_check = {}

    # ---- consumption profile -------------------------------------------------
    def _profile_for(self, opts: dict, now: datetime):
        if self._profile and self._profile_at and now - self._profile_at < PROFILE_MAX_AGE:
            return self._profile
        default_kwh = float(opts["default_load_kw"])
        self.entity_check = self._check_entities(opts)
        try:
            ids = [opts["house_load_entity"]]
            for key in ("car_power_entity", "battery_charge_power_entity", "pv_power_entity"):
                if opts.get(key):
                    ids.append(opts[key])
            start = now - timedelta(days=int(opts["profile_days"]))
            stats = self.ha.statistics(ids, start, now, "hour")
            prof = build_profile(
                stats.get(opts["house_load_entity"]),
                stats.get(opts.get("car_power_entity", "")),
                self.tz,
                default_kwh,
                bat_rows=stats.get(opts.get("battery_charge_power_entity", "")),
                pv_rows=stats.get(opts.get("pv_power_entity", "")),
                battery_mode=opts.get("load_includes_battery_charging", "auto"),
            )
            det = prof["diagnostics"]["battery_detection"]
            if det.get("included") is not None:
                log.info("house load %s battery charging (night base %.2f kW while charging vs %.2f kW idle, "
                         "charging %.2f kW); grid charging %s from the profile",
                         "includes" if det["included"] else "does not include",
                         det["base_while_charging_kw"], det["base_while_idle_kw"], det["charge_kw"],
                         "subtracted" if prof["diagnostics"]["battery_subtracted"] else "not subtracted")
            prof["statistics_rows"] = {i: len(stats.get(i) or []) for i in ids}
            for i in ids:
                if not stats.get(i):
                    log.warning("no long-term statistics for %s (does it have a state_class?)", i)
            if prof["hours_of_data"] < 48:
                log.warning("only %s hours of load statistics; profile falls back to default", prof["hours_of_data"])
            self._profile, self._profile_at = prof, now
        except Exception as e:  # statistics are best-effort; plan with the default load instead
            log.warning("could not build load profile: %s", e)
            prof = build_profile([], [], self.tz, default_kwh)
            # Not cached: try the statistics again next cycle instead of in 6 hours.
        return prof

    def _check_entities(self, opts: dict) -> dict:
        """Which configured entities are missing or unavailable (shown in the UI)."""
        ids = [opts[k] for k in ("price_buy_entity", "price_sell_entity", "battery_soc_entity",
                                 "house_load_entity", "car_power_entity") if opts.get(k)]
        ids += split_csv(opts.get("solcast_entities", ""))
        ids += [opts[k] for k in REGISTER_KEYS if opts.get(k)]
        ids += split_csv(opts.get("counter_entities", ""))
        if opts.get("car_enabled"):
            ids += [opts[k] for k in ("car_soc_entity", "charger_status_entity", "charger_current_entity",
                                      "charger_start_button", "pv_power_entity",
                                      "battery_charge_power_entity") if opts.get(k)]
        result = {}
        for e in dict.fromkeys(ids):
            state = self.ha.text(e)
            result[e] = "missing" if state is None else (
                "unavailable" if state in ("unavailable", "unknown") else "ok")
        bad = {e: s for e, s in result.items() if s != "ok"}
        if bad:
            log.warning("entity check: %s", bad)
        return result

    # ---- one cycle -----------------------------------------------------------
    def run_once(self, now: datetime = None) -> dict:
        with self._run_lock:
            return self._run_once(now)

    def _run_once(self, now: datetime = None) -> dict:
        opts = self.options_fn()
        now = (now or datetime.now(UTC)).astimezone(UTC)
        warnings = []

        buy_attrs = self.ha.attributes(opts["price_buy_entity"])
        sell_attrs = self.ha.attributes(opts["price_sell_entity"])
        if not buy_attrs or not sell_attrs:
            raise PlannerError("price sensors are unavailable")
        buy = parse_prices(buy_attrs)
        sell = parse_prices(sell_attrs)

        sol_attrs = [self.ha.attributes(e) for e in split_csv(opts["solcast_entities"])]
        pv, pv50, pv10, pv90 = parse_solcast(sol_attrs, float(opts["pv_p10_weight"]), with_p90=True)
        if not pv:
            warnings.append("no solar forecast available - planning without solar")

        soc_pct = self.ha.number(opts["battery_soc_entity"])
        if soc_pct is None:
            raise PlannerError("battery SoC is unavailable")

        # Prices for the rest of the horizon (tonight, before ~13:00) are estimated from the last
        # known day so the plan can weigh today's sun against tonight's grid price.
        until = hour_floor(now) + timedelta(hours=int(opts["min_horizon_hours"]))
        buy_fc = persistence_forecast(buy, until, self.tz)
        sell_fc = persistence_forecast(sell, until, self.tz)
        forecast_hours = set(buy_fc) | set(sell_fc)
        buy, sell = {**buy_fc, **buy}, {**sell_fc, **sell}

        profile = self._profile_for(opts, now)
        horizon = build_horizon(now, buy, sell, pv, lambda h: load_for_hour(profile, h, self.tz),
                                forecast_hours=forecast_hours)
        if len(horizon) == 0:
            raise PlannerError("no prices for the current hour")
        n_fc = sum(horizon.forecast)
        if n_fc:
            warnings.append(f"{n_fc} of {len(horizon)} hours use estimated prices (same hour of the last known day)")

        params = battery_params(opts)
        soc0 = params.capacity_kwh * soc_pct / 100.0
        tv = terminal_value_for(horizon.buy, params)

        car_params, car_info = None, {"enabled": bool(opts.get("car_enabled"))}
        if opts.get("car_enabled"):
            status = self.ha.text(opts["charger_status_entity"])
            car_soc = self.ha.number(opts["car_soc_entity"])
            car_params, info = carmod.build_car_params(horizon, now, self.tz, opts, status, car_soc)
            car_info.update(info)
            if status in carmod.CONNECTED and car_soc is None:
                warnings.append("car is connected but its SoC is unavailable - car left out of the plan")

        plan = solve(horizon.buy, horizon.sell, horizon.pv, horizon.load, horizon.dt, soc0, params, tv,
                     car=car_params)
        solar_only = solve(horizon.buy, horizon.sell, horizon.pv, horizon.load, horizon.dt, soc0, params, tv,
                           allow_grid_charge=False, car=car_params)
        if car_params:
            car_info["planned_kwh"] = round(float((plan.pv_car + plan.bat_car + plan.grid_car).sum()), 2)
            car_info["planned_cost_dkk"] = round(float((plan.grid_car * plan.buy).sum()), 2)
            car_info["shortfall_kwh"] = round(plan.car_shortfall, 2)
            if plan.car_shortfall > 0.05:
                warnings.append(f"car minimum cannot be reached by the deadline ({plan.car_shortfall:.1f} kWh short)")
        savings = solar_only.net_cost - plan.net_cost

        sp = derive_setpoints(plan, soc_pct, params, float(opts["inverter_kw"]),
                              float(opts["soc_floor_pct"]), float(opts["soc_ceiling_pct"]))

        rows = []
        for i, start in enumerate(horizon.starts):
            rows.append({
                "t": start.astimezone(self.tz).isoformat(timespec="minutes"),
                "buy": round(float(plan.buy[i]), 3), "sell": round(float(plan.sell[i]), 3),
                "pv": round(float(plan.pv[i]), 2), "load": round(float(plan.load[i]), 2),
                "pv_bat": round(float(plan.pv_bat[i]), 2), "grid_bat": round(float(plan.grid_bat[i]), 2),
                "bat_load": round(float(plan.bat_load[i]), 2), "pv_exp": round(float(plan.pv_exp[i]), 2),
                "soc_pct": round(float(plan.soc_end[i]) / params.capacity_kwh * 100.0, 1),
                "forecast_price": bool(horizon.forecast[i]),
                "dt": round(float(plan.dt[i]), 3),
                "grid_load": round(float(plan.grid_load[i]), 2),
                "pv_p10": round(pv10.get(start, 0.0) * float(plan.dt[i]), 2),
                "pv_p90": round(pv90.get(start, 0.0) * float(plan.dt[i]), 2),
                "mode": step_mode(plan, i),
            })
            if car_params:
                rows[-1].update({
                    "car_pv": round(float(plan.pv_car[i]), 2), "car_grid": round(float(plan.grid_car[i]), 2),
                    "car_bat": round(float(plan.bat_car[i]), 2),
                    "car_soc": round(float(plan.car_end[i]) / float(opts["car_battery_kwh"]) * 100.0, 1),
                })

        actual = {k: self.ha.text(opts[k]) for k in REGISTER_KEYS if opts.get(k)}
        counter_entities = split_csv(opts.get("counter_entities", "")) + list(counter_ids(opts).values())
        counters = {e: self.ha.number(e) for e in dict.fromkeys(counter_entities)}
        h0, dt0 = horizon.starts[0], float(plan.dt[0])
        # What the plan expected for the current hour (as average kW), next to instantaneous samples,
        # so the evaluation can score the forecasts against the counters.
        pred = {
            "pv_kw": round(float(plan.pv[0]) / dt0, 3),
            "pv_p50_kw": round(pv50.get(h0, 0.0), 3), "pv_p10_kw": round(pv10.get(h0, 0.0), 3),
            "load_kw": round(float(plan.load[0]) / dt0, 3),
        }
        samples = {"house_load_w": self.ha.number(opts["house_load_entity"]),
                   "car_w": self.ha.number(opts["car_power_entity"]) if opts.get("car_power_entity") else None}
        snapshot = {
            "updated": now.astimezone(self.tz).isoformat(timespec="seconds"),
            "dry_run": True,
            "soc_pct": soc_pct,
            "mode": sp["mode"],
            "setpoints": sp,
            "actual_registers": actual,
            "counters": counters,
            "price_now": {"buy": round(float(plan.buy[0]), 4), "sell": round(float(plan.sell[0]), 4)},
            "pred": pred,
            "samples": samples,
            "pv_p10_weight": float(opts["pv_p10_weight"]),
            "savings_vs_solar_only_dkk": round(float(savings), 2),
            "net_cost_dkk": round(plan.net_cost, 2),
            "horizon_hours": len(horizon),
            "profile_hours_of_data": profile["hours_of_data"],
            "profile": profile_view(profile),
            "entity_check": self.entity_check,
            "warnings": warnings,
            "car": car_info,
            "plan": rows,
        }
        snapshot["upcoming"] = upcoming(rows, bool(car_params), now.astimezone(self.tz).date())
        snapshot["summary"] = summary_attributes(sp, snapshot["upcoming"])
        with self._lock:
            self.latest = snapshot
            self.last_error = None
        self._publish(snapshot)
        self._log_decision(snapshot)
        self._publish_cost(opts, now)
        return snapshot

    # ---- outputs -------------------------------------------------------------
    def _publish(self, snap: dict):
        sp = snap["setpoints"]
        common = {"dry_run": True, "updated": snap["updated"]}
        self.ha.set_state("sensor.energy_planner_mode", snap["mode"], {
            **common, "friendly_name": "Energy Planner mode", "icon": "mdi:home-battery", **sp,
            **snap.get("summary", {}),
            "savings_vs_solar_only_dkk": snap["savings_vs_solar_only_dkk"],
            "horizon_hours": snap["horizon_hours"], "car": snap.get("car"),
        })
        self.ha.set_state("sensor.energy_planner_soc_floor", sp["min_soc_pct"], {
            **common, "friendly_name": "Energy Planner SoC floor", "unit_of_measurement": "%"})
        # Always published, "unknown" while the target does not apply (load first / discharging), so
        # nothing downstream can act on a target left over from an earlier cycle.
        self.ha.set_state("sensor.energy_planner_soc_target",
                          sp["max_soc_pct"] if sp["max_soc_pct"] is not None else "unknown", {
                              **common, "friendly_name": "Energy Planner SoC target",
                              "unit_of_measurement": "%", "active": sp["max_soc_pct"] is not None})
        self.ha.set_state("sensor.energy_planner_charge_rate", sp["charge_rate_pct"], {
            **common, "friendly_name": "Energy Planner charge rate", "unit_of_measurement": "%"})
        self.ha.set_state("sensor.energy_planner_plan", snap["net_cost_dkk"], {
            **common, "friendly_name": "Energy Planner plan", "unit_of_measurement": "DKK",
            "plan": [[r["t"], r["grid_bat"], r["pv_bat"], r["bat_load"], r["pv_exp"], r["soc_pct"],
                      round(r.get("car_grid", 0.0) + r.get("car_pv", 0.0) + r.get("car_bat", 0.0), 2),
                      r.get("car_soc")]
                     for r in snap["plan"]],
            "columns": ["t", "grid_to_battery_kwh", "pv_to_battery_kwh", "battery_to_load_kwh",
                        "pv_export_kwh", "soc_end_pct", "car_kwh", "car_soc_end_pct"],
        })

    def _log_decision(self, snap: dict):
        entry = {k: snap.get(k) for k in ("updated", "soc_pct", "mode", "setpoints", "actual_registers",
                                          "counters", "price_now", "pred", "samples", "pv_p10_weight",
                                          "savings_vs_solar_only_dkk", "net_cost_dkk", "horizon_hours", "car")}
        self._append(self.log_path, entry)

    # ---- car control (fast loop, dry-run) --------------------------------------
    def car_cycle(self, now: datetime = None) -> dict:
        """What the car charger *would* be told now. Publishes and logs; calls nothing."""
        opts = self.options_fn()
        if not opts.get("car_enabled"):
            return None
        now = (now or datetime.now(UTC)).astimezone(UTC)
        st = self.ha.state(opts["charger_status_entity"]) or {}
        secs = 9999
        if st.get("last_changed"):
            try:
                secs = int((now - datetime.fromisoformat(st["last_changed"].replace("Z", "+00:00"))).total_seconds())
            except ValueError:
                pass

        def num(key, default=0.0):
            v = self.ha.number(opts[key]) if opts.get(key) else None
            return default if v is None else v

        live = {
            "status": st.get("state"),
            "car_soc": self.ha.number(opts["car_soc_entity"]),
            "pv_w": num("pv_power_entity"), "load_w": num("house_load_entity"),
            "charger_w": num("car_power_entity"), "house_soc": num("battery_soc_entity"),
            "bat_charge_w": num("battery_charge_power_entity"),
            "amps": self.ha.number(opts["charger_current_entity"]),
            "secs_since_status": secs, "sell": self.ha.number(opts["price_sell_entity"]),
        }
        with self._lock:
            snap = self.latest
        row = carmod.plan_for_now(snap, now, self.tz)
        d = carmod.decide(live, row, opts)
        d.update({"updated": now.astimezone(self.tz).isoformat(timespec="seconds"), "dry_run": True,
                  "live": live, "plan_row": row})
        with self._lock:
            self.car_latest = d
        d["reason_da"] = reason_da(d["reason"])
        car_items = [it for it in (snap or {}).get("upcoming", []) if it["kind"] == "car"]
        car_info = (snap or {}).get("car") or {}
        nxt = car_items[0] if car_items else None
        self.ha.set_state("sensor.energy_planner_car", d["action"], {
            "friendly_name": "Energy Planner car", "icon": "mdi:car-electric", "dry_run": True, "updated": d["updated"],
            "label": CAR_ACTION_LABEL.get(d["action"], d["action"]),
            "target_amps": d["target_amps"], "reason": d["reason"], "reason_da": d["reason_da"], "calls": d["calls"],
            "surplus_w": d.get("surplus_w"),
            "next_window": f"{nxt['when']} · {nxt['text']}" if nxt else None,
            "next_start": nxt["start"] if nxt else None,
            "planned_kwh": car_info.get("planned_kwh"),
            "deadline": car_info.get("deadline")})
        key = (d["action"], d["target_amps"], d["reason"].split(" (")[0], live["status"], live["amps"])
        if key != self._car_last_logged:          # log changes only, not every 2 minutes
            self._car_last_logged = key
            self._append(self.car_log_path, {k: d[k] for k in ("updated", "action", "target_amps", "reason",
                                                                "reason_da", "calls", "live")})
        return d

    def _publish_cost(self, opts: dict, now: datetime):
        """sensor.energy_planner_cost_today: realised grid cost so far today (and yesterday's total)."""
        try:
            days = {d["day"]: d for d in evaluate(self.recent_decisions(240), counter_ids(opts), self.tz)["days"]}
        except Exception as e:  # never let a reporting sensor break a planning cycle
            log.warning("cost sensor: %s", e)
            return
        today = now.astimezone(self.tz).date()
        t, y = days.get(today.isoformat()), days.get((today - timedelta(days=1)).isoformat())
        self.ha.set_state("sensor.energy_planner_cost_today", t["cost_dkk"] if t else 0.0, {
            "friendly_name": "Energy Planner netudgift i dag", "icon": "mdi:cash", "unit_of_measurement": "DKK",
            "import_kwh": t and t["import_kwh"], "export_kwh": t and t["export_kwh"],
            "avg_buy_paid": t and t["avg_buy_paid"], "avg_buy_market": t and t["avg_buy_market"],
            "agreement_pct": t and t["agreement_pct"], "hours_covered": t and t["hours"],
            "yesterday_dkk": y and y["cost_dkk"], "yesterday_import_kwh": y and y["import_kwh"],
            "updated": now.astimezone(self.tz).isoformat(timespec="seconds")})

    def _append(self, path: str, entry: dict):
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "a") as f:
                f.write(json.dumps(entry) + "\n")
            with open(path) as f:
                lines = f.readlines()
            if len(lines) > MAX_LOG_LINES:
                with open(path, "w") as f:
                    f.writelines(lines[-KEEP_LOG_LINES:])
        except OSError as e:
            log.warning("could not write %s: %s", path, e)

    def recent_car_decisions(self, limit: int = 200) -> list:
        try:
            with open(self.car_log_path) as f:
                lines = f.readlines()[-limit:]
            return [json.loads(l) for l in lines if l.strip()]
        except (OSError, ValueError):
            return []

    def recent_decisions(self, limit: int = 200) -> list:
        try:
            with open(self.log_path) as f:
                lines = f.readlines()[-limit:]
            return [json.loads(l) for l in lines if l.strip()]
        except (OSError, ValueError):
            return []

