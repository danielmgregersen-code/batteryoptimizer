"""One planning cycle: read HA -> build horizon -> solve -> setpoints -> publish + log (dry-run)."""
import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone

from inputs import UTC, build_horizon, hour_floor, parse_prices, parse_solcast, persistence_forecast
from optimizer import BatteryParams, solve, terminal_value_for
from loadprofile import build_profile, load_for_hour
from settings import REGISTER_KEYS, load_options, local_tz, split_csv
from setpoints import derive_setpoints

log = logging.getLogger("energy_planner")

DECISION_LOG = "/data/decisions.jsonl"
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
    def __init__(self, ha, tz=None, log_path: str = DECISION_LOG, options_fn=load_options):
        self.ha = ha
        self.tz = tz or local_tz()
        self.log_path = log_path
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
            if opts.get("car_power_entity"):
                ids.append(opts["car_power_entity"])
            start = now - timedelta(days=int(opts["profile_days"]))
            stats = self.ha.statistics(ids, start, now, "hour")
            prof = build_profile(
                stats.get(opts["house_load_entity"]),
                stats.get(opts.get("car_power_entity", "")),
                self.tz,
                default_kwh,
            )
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
        pv, _, _ = parse_solcast(sol_attrs, float(opts["pv_p10_weight"]))
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
        plan = solve(horizon.buy, horizon.sell, horizon.pv, horizon.load, horizon.dt, soc0, params, tv)
        solar_only = solve(horizon.buy, horizon.sell, horizon.pv, horizon.load, horizon.dt, soc0, params, tv,
                           allow_grid_charge=False)
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
            })

        actual = {k: self.ha.text(opts[k]) for k in REGISTER_KEYS if opts.get(k)}
        counters = {e: self.ha.number(e) for e in split_csv(opts.get("counter_entities", ""))}
        snapshot = {
            "updated": now.astimezone(self.tz).isoformat(timespec="seconds"),
            "dry_run": True,
            "soc_pct": soc_pct,
            "mode": sp["mode"],
            "setpoints": sp,
            "actual_registers": actual,
            "counters": counters,
            "price_now": {"buy": round(float(plan.buy[0]), 4), "sell": round(float(plan.sell[0]), 4)},
            "savings_vs_solar_only_dkk": round(float(savings), 2),
            "net_cost_dkk": round(plan.net_cost, 2),
            "horizon_hours": len(horizon),
            "profile_hours_of_data": profile["hours_of_data"],
            "entity_check": self.entity_check,
            "warnings": warnings,
            "plan": rows,
        }
        with self._lock:
            self.latest = snapshot
            self.last_error = None
        self._publish(snapshot)
        self._log_decision(snapshot)
        return snapshot

    # ---- outputs -------------------------------------------------------------
    def _publish(self, snap: dict):
        sp = snap["setpoints"]
        common = {"dry_run": True, "updated": snap["updated"]}
        self.ha.set_state("sensor.energy_planner_mode", snap["mode"], {
            **common, "friendly_name": "Energy Planner mode", **sp,
            "savings_vs_solar_only_dkk": snap["savings_vs_solar_only_dkk"],
            "horizon_hours": snap["horizon_hours"],
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
            "plan": [[r["t"], r["grid_bat"], r["pv_bat"], r["bat_load"], r["pv_exp"], r["soc_pct"]]
                     for r in snap["plan"]],
            "columns": ["t", "grid_to_battery_kwh", "pv_to_battery_kwh", "battery_to_load_kwh",
                        "pv_export_kwh", "soc_end_pct"],
        })

    def _log_decision(self, snap: dict):
        entry = {k: snap.get(k) for k in ("updated", "soc_pct", "mode", "setpoints", "actual_registers",
                                          "counters", "price_now", "savings_vs_solar_only_dkk",
                                          "horizon_hours", "car")}
        try:
            os.makedirs(os.path.dirname(self.log_path) or ".", exist_ok=True)
            with open(self.log_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
            with open(self.log_path) as f:
                lines = f.readlines()
            if len(lines) > MAX_LOG_LINES:
                with open(self.log_path, "w") as f:
                    f.writelines(lines[-KEEP_LOG_LINES:])
        except OSError as e:
            log.warning("could not write decision log: %s", e)

    def recent_decisions(self, limit: int = 200) -> list:
        try:
            with open(self.log_path) as f:
                lines = f.readlines()[-limit:]
            return [json.loads(l) for l in lines if l.strip()]
        except (OSError, ValueError):
            return []


def evaluate(decisions: list, import_counter: str, export_counter: str, tz) -> list:
    """Realised grid cost per local day from the logged counters and prices.

    Between two consecutive log entries the counter deltas are priced at the earlier entry's
    prices. This is what actually happened (under Node-RED during the dry-run); compare it with
    the plan's net cost to judge the planner.
    """
    days: dict = {}
    for a, b in zip(decisions, decisions[1:]):
        ca, cb = a.get("counters") or {}, b.get("counters") or {}
        pa = a.get("price_now") or {}
        try:
            d_imp = cb[import_counter] - ca[import_counter]
            d_exp = cb[export_counter] - ca[export_counter]
            buy, sell = float(pa["buy"]), float(pa["sell"])
        except (KeyError, TypeError):
            continue
        if d_imp < 0 or d_exp < 0 or d_imp > 50 or d_exp > 50:   # counter reset or gap
            continue
        day = datetime.fromisoformat(a["updated"]).astimezone(tz).date().isoformat()
        e = days.setdefault(day, {"day": day, "import_kwh": 0.0, "export_kwh": 0.0, "cost_dkk": 0.0})
        e["import_kwh"] += d_imp
        e["export_kwh"] += d_exp
        e["cost_dkk"] += d_imp * buy - d_exp * sell
    return [{k: (round(v, 2) if isinstance(v, float) else v) for k, v in e.items()} for e in days.values()]
