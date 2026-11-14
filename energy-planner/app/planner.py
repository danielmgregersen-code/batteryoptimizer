"""One planning cycle: read HA -> build horizon -> solve -> setpoints -> publish + log (dry-run)."""
import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone

from inputs import UTC, build_horizon, parse_prices, parse_solcast
from optimizer import BatteryParams, solve, terminal_value_for
from loadprofile import build_profile, load_for_hour
from settings import REGISTER_KEYS, load_options, split_csv
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
        self.tz = tz or datetime.now().astimezone().tzinfo
        self.log_path = log_path
        self.options_fn = options_fn
        self._profile = None
        self._profile_at = None
        self._lock = threading.Lock()
        self.latest = None
        self.last_error = None

    # ---- consumption profile -------------------------------------------------
    def _profile_for(self, opts: dict, now: datetime):
        if self._profile and self._profile_at and now - self._profile_at < PROFILE_MAX_AGE:
            return self._profile
        default_kwh = float(opts["default_load_kw"])
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
            if prof["hours_of_data"] < 48:
                log.warning("only %s hours of load statistics; profile falls back to default", prof["hours_of_data"])
        except Exception as e:  # statistics are best-effort; plan with the default load instead
            log.warning("could not build load profile: %s", e)
            prof = build_profile([], [], self.tz, default_kwh)
        self._profile, self._profile_at = prof, now
        return prof

    # ---- one cycle -----------------------------------------------------------
    def run_once(self, now: datetime = None) -> dict:
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

        profile = self._profile_for(opts, now)
        horizon = build_horizon(now, buy, sell, pv, lambda h: load_for_hour(profile, h, self.tz))
        if len(horizon) == 0:
            raise PlannerError("no prices for the current hour")

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
            })

        actual = {k: self.ha.text(opts[k]) for k in REGISTER_KEYS if opts.get(k)}
        snapshot = {
            "updated": now.astimezone(self.tz).isoformat(timespec="seconds"),
            "dry_run": True,
            "soc_pct": soc_pct,
            "mode": sp["mode"],
            "setpoints": sp,
            "actual_registers": actual,
            "savings_vs_solar_only_dkk": round(float(savings), 2),
            "net_cost_dkk": round(plan.net_cost, 2),
            "horizon_hours": len(horizon),
            "profile_hours_of_data": profile["hours_of_data"],
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
        if sp["max_soc_pct"] is not None:
            self.ha.set_state("sensor.energy_planner_soc_target", sp["max_soc_pct"], {
                **common, "friendly_name": "Energy Planner SoC target", "unit_of_measurement": "%"})
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
        entry = {k: snap[k] for k in ("updated", "soc_pct", "mode", "setpoints", "actual_registers",
                                      "savings_vs_solar_only_dkk", "horizon_hours")}
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
