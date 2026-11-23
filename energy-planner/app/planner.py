"""One planning cycle: read HA -> build horizon -> solve -> setpoints -> write (production) -> publish + log."""
import dataclasses
import json
import math
import logging
import os
import threading
from datetime import datetime, timedelta, timezone

import appliances as appl
import batterycurve
import car as carmod
import cartrace
import economy
import heatpump
from inputs import (SOURCE_FORECAST, UTC, build_horizon, fill_prices, hour_floor, merge_prices, parse_prices,
                    parse_solcast, parse_ts, price_series)
from optimizer import BatteryParams, solve, terminal_value_for
from loadprofile import _by_start, build_profile, load_for_hour, profile_view
import pvdampening as pvd
from settings import REGISTER_KEYS, counter_ids, load_options, local_tz, split_csv
from setpoints import adjust_for_soc, derive_setpoints, step_mode
from evaluation import evaluate
from summary import num as num_da
from summary import appliance_advice, car_advice, price_attributes, CAR_ACTION_LABEL, reason_da, summary_attributes, upcoming
import writer

log = logging.getLogger("energy_planner")

DECISION_LOG = "/data/decisions.jsonl"
CAR_LOG = "/data/car_decisions.jsonl"
KIA_REPEAT_S = 600     # the same Kia cloud call is not repeated within this while the status is unchanged
KIA_BACKOFF_MIN = (2, 4, 8, 15)      # minutes between retries after failed Kia calls
KIA_BLOCK = timedelta(hours=6)       # after a PIN / access error: no Kia calls for this long
KIA_STOP_IGNORED = timedelta(minutes=10)   # a successful stop the car has not followed after this
THOR_STOP_AFTER = timedelta(minutes=5)     # the pause must have been wanted this long before an emergency stop
NON_RETRYABLE = ("pin", "unauthorized", "unauthorised", "locked", "token", "forbidden", "401", "403")
MAX_LOG_LINES = 12000      # with a cycle every 5 min, 9000 lines are about 31 days
KEEP_LOG_LINES = 9000
LOG_READ = 9000
PROFILE_MAX_AGE = timedelta(hours=6)
GAP_DONE_MIN = 25                  # an appliance run counts as finished this long after its last power
BACKFILL_DAYS = 14                 # Solcast keeps 14 days of undampened forecast history
BACKFILL_RETRY = timedelta(hours=6)


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


def _solar_totals(rows: list) -> dict:
    """Sums over the hours that have an actual value (so forecasts compare like with like), plus the
    whole-day forecast."""
    with_actual = [r for r in rows if "actual" in r]

    def tot(rs, k):
        vals = [r[k] for r in rs if k in r]
        return round(sum(vals), 2) if vals else None
    return {"actual": tot(with_actual, "actual"), "p50": tot(with_actual, "p50"), "c50": tot(with_actual, "c50"),
            "dayahead": tot(with_actual, "dayahead"), "hours_actual": len(with_actual),
            "day_p50": tot(rows, "p50"), "day_p10": tot(rows, "p10"), "day_p90": tot(rows, "p90"),
            "day_c50": tot(rows, "c50"), "day_dayahead": tot(rows, "dayahead")}


def _preview_text(pv: dict, tz, now: datetime) -> str:
    """A Danish one-liner for the car preview (panel and sensor)."""
    def num(v, d=1):
        return f"{v:.{d}f}".replace(".", ",")
    today = now.astimezone(tz).date()

    def when(w):
        a, b = datetime.fromisoformat(w["start"]), datetime.fromisoformat(w["end"])
        day = a.date()
        rel = "i dag" if day == today else ("i nat" if (day - today).days == 1 and a.hour < 8
                                            else "i morgen" if (day - today).days == 1 else f"{day:%d.%m}")
        if day == today and a.hour >= 18:
            rel = "i aften"
        return f"{rel} kl. {a:%H}–{b:%H}"
    dl = datetime.fromisoformat(pv["deadline"]).astimezone(tz)
    if pv["reason"] == "none":
        if pv["soc_now"] >= (pv.get("max_soc_pct") or 100) - 0.5:
            return f"Ville ikke lade: bilen er på {num(pv['soc_now'], 0)} % (loftet)."
        return (f"Ville ikke lade før kl. {dl:%H}: bilen er på {num(pv['soc_now'], 0)} % (over minimum "
                f"{num(pv['min_soc_pct'], 0)} %), og ingen timer er billigere end {num(pv['value_dkk'], 2)} kr.")
    ws = pv["windows"][:2]
    parts = ", ".join(when(w) for w in ws) + (" m.fl." if len(pv["windows"]) > 2 else "")
    solar = all(w["solar"] for w in pv["windows"])
    cost = "på sol (gratis)" if solar and pv["cost_dkk"] < 0.5 else f"(net ca. {num(pv['cost_dkk'], 0)} kr)"
    return (f"Ville lade {num(pv['planned_kwh'])} kWh {parts} {cost} og nå {num(pv['soc_at_deadline'], 0)} % "
            f"kl. {dl:%H}.")


class Planner:
    def __init__(self, ha, tz=None, log_path: str = DECISION_LOG, options_fn=load_options,
                 car_log_path: str = CAR_LOG):
        self.ha = ha
        self.tz = tz or local_tz()
        self.log_path = log_path
        self.car_log_path = car_log_path
        self.car_latest = None
        self._car_last_logged = None
        self._last_kia = None          # (service, charger status, time) of the last Kia call
        self._car_stop_sent = None     # (time, via) of the last pause sent, before its call returns
        self._kia_block_until = None   # PIN / access error: no Kia calls before this
        self._kia_fails = 0            # consecutive failed Kia calls (backoff)
        self._kia_next_try = None
        self._kia_last_error = None
        self._stop_ok_at = None        # last successful kia stop while the car was charging
        self._thor_stopped = False     # emergency stop on the charger done: no restart from here
        self._pause_wanted_since = None   # since when the controller has wanted a charging car paused
        self._hp_written_hour = None      # the heat pump's setpoint is changed at most once per hour
        self._mode_state = None           # (hour, setpoints) of the last cycle: calm modes within the hour
        self._bat_curve = None            # learned charge power at high SoC (batterycurve.learn)
        self.hp_levels_path = os.path.join(os.path.dirname(log_path) or ".", "heat_pump_levels.json")
        self._hp_levels = pvd.load_json(self.hp_levels_path, {})   # the level the heat pump ran with, per hour
        self._hp_power = {}               # its meter's hourly mean power (W)
        self.appliance_runs_path = os.path.join(os.path.dirname(log_path) or ".", "appliance_runs.json")
        self._appl_runs = pvd.load_json(self.appliance_runs_path, {})   # {appliance name: [runs]}
        self._appl_learned_at = None
        self.price_fc_path = os.path.join(os.path.dirname(log_path) or ".", "price_forecast.json")
        self._price_fc = pvd.load_json(self.price_fc_path, {})          # {utc hour iso: forecast buy price}
        self._price_fc_saved = dict(self._price_fc)
        self._price_fc_backfilled = False
        self.hp_state_path = os.path.join(os.path.dirname(log_path) or ".", "heat_pump_state.json")
        # {"off_by_us": hvac mode to restore or None, "forced_on": bool}
        self._hp_state = pvd.load_json(self.hp_state_path, {})
        self.trace = cartrace.CarTrace(os.path.dirname(log_path) or ".")   # diagnosis after charging stops
        self._trace_status_prev = None
        from collections import deque
        self._charge_log = deque()        # [(t, cumulative kWh delivered by the charger)], 24 h
        self._charge_last = None
        self._car_status_prev = None      # charger status at the previous car cycle
        self._session_ended_at = None     # the charger ended the session on its own (not through HA)
        self._car_prod_prev = False
        self._fuse_rate_cap = None     # battery charge rate (%) capped live by the main fuse guard
        self.options_fn = options_fn
        self._profile = None
        self._profile_at = None
        self._lock = threading.Lock()        # guards latest / last_error
        self._run_lock = threading.Lock()    # one planning cycle at a time (loop vs. "run now")
        self.latest = None
        self.last_error = None
        self.entity_check = {}
        data_dir = os.path.dirname(log_path) or "."
        self.pv_history_path = os.path.join(data_dir, "pv_history.json")
        self.pv_state_path = os.path.join(data_dir, "pv_dampening.json")
        self._pv_history = pvd.load_json(self.pv_history_path, {})
        self._pv_state = pvd.load_json(self.pv_state_path, {"applied": [1.0] * 24, "previous": [1.0] * 24,
                                                            "pushed_at": None})
        self._pv_actual = {}
        self._solcast_synced = False
        self.car_full_path = os.path.join(data_dir, "car_full.json")
        self._car_full_seen = pvd.load_json(self.car_full_path, {}).get("at")   # last SoC >= 99 seen live
        self._car_full_stats = None    # last SoC >= 99 in HA's statistics
        self.car_extra_path = os.path.join(data_dir, "car_extra.json")
        self._car_extra = pvd.load_json(self.car_extra_path, {}) or None   # extra charge ordered in the panel
        self._price_history = []       # hourly buy prices of the last 30 days
        self._backfill = None          # {"hours", "at", "error"} of the last history fetch
        self._backfill_at = None       # when it was last attempted
        self.pv_dayahead_path = os.path.join(data_dir, "pv_dayahead.json")
        self._pv_dayahead = pvd.load_json(self.pv_dayahead_path, {})   # tomorrow's forecast as seen the evening before
        self._pv_shade, self._pv_ref = [0.0] * 24, [0.0] * 24          # the correction of the last cycle
        self._solar_forecast = []

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
            submeters = [e for e in split_csv(opts.get("load_submeters") or "") if e not in ids]
            ids += submeters
            if opts.get("heat_pump_power_entity") and opts["heat_pump_power_entity"] not in ids:
                ids.append(opts["heat_pump_power_entity"])
            start = now - timedelta(days=int(opts["profile_days"]))
            stats = self.ha.statistics(ids, start, now, "hour")
            ly_weight = float(opts.get("profile_last_year_weight") or 0.0)
            merged = stats
            if ly_weight > 0:
                # the same weeks last year (+-21 days around today's date one year ago)
                try:
                    ly_mid = now - timedelta(days=365)
                    ly = self.ha.statistics(ids, ly_mid - timedelta(days=21), ly_mid + timedelta(days=21), "hour")
                    merged = {i: list(ly.get(i) or []) + list(stats.get(i) or []) for i in ids}
                except Exception as e:
                    log.info("last year's statistics unavailable: %s", e)
            prof = build_profile(
                merged.get(opts["house_load_entity"]),
                merged.get(opts.get("car_power_entity", "")),
                self.tz,
                default_kwh,
                bat_rows=merged.get(opts.get("battery_charge_power_entity", "")),
                pv_rows=merged.get(opts.get("pv_power_entity", "")),
                battery_mode=opts.get("load_includes_battery_charging", "no"),
                car_detect_w=float(opts.get("car_detect_kw", 4.0)) * 1000.0,
                submeter_rows={e: merged.get(e) for e in submeters},
                now=now, half_life_days=float(opts.get("profile_half_life_days") or 0) or None,
                last_year_weight=ly_weight,
                steady_submeters=split_csv(opts.get("load_submeters_steady") or ""),
                smooth_hours=int(opts.get("load_submeters_smooth_hours", 2)),
            )
            det = prof["diagnostics"]["battery_detection"]
            if det.get("included") is not None:
                log.info("house load %s battery charging (night base %.2f kW while charging vs %.2f kW idle, "
                         "charging %.2f kW); grid charging %s from the profile",
                         "includes" if det["included"] else "does not include",
                         det["base_while_charging_kw"], det["base_while_idle_kw"], det["charge_kw"],
                         "subtracted" if prof["diagnostics"]["battery_subtracted"] else "not subtracted")
            prof["statistics_rows"] = {i: len(stats.get(i) or []) for i in ids}
            if opts.get("pv_power_entity"):
                self._pv_actual = _by_start(stats.get(opts["pv_power_entity"]))
                days = int(opts.get("pv_dampening_days", 14))
                if days > int(opts["profile_days"]):
                    # the solar correction learns from a longer window than the load profile
                    try:
                        pv_stats = self.ha.statistics([opts["pv_power_entity"]], now - timedelta(days=days), now, "hour")
                        self._pv_actual = {**_by_start(pv_stats.get(opts["pv_power_entity"])), **self._pv_actual}
                    except Exception as e:
                        log.info("solar statistics for %s days unavailable: %s", days, e)
            self._refresh_car_history(opts, now)
            self._learn_battery_curve(opts, now)
            if opts.get("heat_pump_power_entity"):
                self._hp_power = _by_start(stats.get(opts["heat_pump_power_entity"]))
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

    # ---- car: full charges and trips -------------------------------------------
    def battery_fast_cycle(self, now: datetime = None):
        """Every minute between plans (battery production on): re-check the latest plan's setpoints
        against the SoC right now and write what changed, e.g. hand grid charging over to the sun as
        soon as the grid target is reached. No new optimisation."""
        opts = self.options_fn()
        if not writer.switch_on(self.ha, opts.get("production_switch_entity")):
            return None
        with self._lock:
            snap = self.latest
        soc = self.ha.number(opts["battery_soc_entity"])
        if not snap or soc is None:
            return None
        now = (now or datetime.now(UTC)).astimezone(UTC)
        live = self._fuse_guard_battery(adjust_for_soc(snap["setpoints"], soc), opts)
        if hasattr(self.ha, "set_allowed_entities"):
            self.ha.set_allowed_entities(writer.allowed_entities(opts))
        writes = writer.apply(self.ha, opts, live, now.astimezone(self.tz))
        with self._lock:
            if self.latest is snap:
                snap["setpoints_live"] = live
                if writes:
                    snap["production"]["writes"] = writes
                    snap["actual_registers"] = {k: self.ha.text(opts[k]) for k in REGISTER_KEYS if opts.get(k)}
        for w in writes:
            log.info("fast: wrote %s: %s -> %s (%s)", w["entity"], w["from"], w["to"], w["status"])
        return writes

    # ---- main fuse ----------------------------------------------------------------
    def _phase_amps(self, opts: dict):
        """Grid current per phase (A), or None if not configured or any phase is unavailable."""
        ids = split_csv(opts.get("grid_current_entities") or "")
        if not ids or not opts.get("main_fuse_amps"):
            return None
        vals = [self.ha.number(e) for e in ids]
        return None if any(v is None for v in vals) else vals

    def _fuse_guard_battery(self, live: dict, opts: dict) -> dict:
        """Lower the battery's grid charge rate while the worst phase is above the fuse minus 1 A and
        the car cannot give more (it is at 6 A, or not charging). Recovers in steps of 10 % once
        there is 3 A of room again. Only while charging from the grid."""
        if not live.get("grid_charge"):
            self._fuse_rate_cap = None
            return live
        phases = self._phase_amps(opts)
        rate = int(live.get("charge_rate_pct") or 100)
        if phases:
            imax = max(abs(a) for a in phases)
            limit = float(opts["main_fuse_amps"]) - 1.0
            car = self.car_latest or {}
            car_cannot = car.get("action") != "charge" or (car.get("fuse") or {}).get("at_minimum")
            if imax > limit and car_cannot:
                over_kw = (imax - limit) * 3 * 0.23
                step = max(5, math.ceil(over_kw / float(opts["inverter_kw"]) * 100 / 5) * 5)
                self._fuse_rate_cap = max(5, min(self._fuse_rate_cap or rate, rate) - step)
                log.info("fuse: highest phase %.1f A, battery charge rate capped at %s %%", imax, self._fuse_rate_cap)
            elif self._fuse_rate_cap is not None and imax < limit - 3:
                self._fuse_rate_cap += 10
                if self._fuse_rate_cap >= rate:
                    self._fuse_rate_cap = None
        if self._fuse_rate_cap is not None and self._fuse_rate_cap < rate:
            return {**live, "charge_rate_pct": self._fuse_rate_cap, "fuse_capped": True}
        return live

    def trace_tick(self, now: datetime = None, manual: bool = False):
        """Every few seconds: start a diagnosis trace when the charger leaves "charging" (or by hand),
        and sample it while it runs."""
        opts = self.options_fn()
        if not opts.get("car_enabled") or not opts.get("charger_status_entity"):
            return
        now = (now or datetime.now(UTC)).astimezone(self.tz)
        status = self.ha.text(opts["charger_status_entity"])
        prev, self._trace_status_prev = self._trace_status_prev, status
        if opts.get("charger_power_entity"):
            self._charge_last = carmod.add_energy(self._charge_log, now.astimezone(UTC),
                                                  self.ha.number(opts["charger_power_entity"]), self._charge_last)
            while self._charge_log and now.astimezone(UTC) - self._charge_log[0][0] > timedelta(hours=24):
                self._charge_log.popleft()
        if manual or (prev == "charging" and status not in (None, "charging")):
            last = (self.car_latest or {}).get("executed") or []
            kia = next((x for x in last if x.get("service") == "kia_uvo.stop_charge"), None)
            sent = self._car_stop_sent    # set before the call: it can take longer than the car takes to stop
            recent = sent and (now.astimezone(UTC) - sent[0]).total_seconds() < 180
            trigger = ("manuel start fra panelet" if manual else
                       f"add-on'en: pause via {sent[1]}" if recent else
                       f"add-on'en: kia stop_charge ({kia.get('status')})" if kia else "ukendt (bil, app eller lader?)")
            if self.trace.start(now, trigger, prev, status):
                log.info("car trace started: %s (%s -> %s)", trigger, prev, status)
        if self.trace.active():
            states = self.ha.all_states() if hasattr(self.ha, "all_states") else []
            self.trace.sample(states, cartrace.prefixes(opts), now)

    def car_soc_now(self, opts: dict, now: datetime) -> dict:
        """The car's SoC: the last reported one plus what the charger delivered since (see
        car.estimate_soc). The report time comes from car_soc_time_entity, else the SoC's last change."""
        e = opts.get("car_soc_entity")
        soc = self.ha.number(e) if e else None
        anchor = None
        te = opts.get("car_soc_time_entity")
        raw = self.ha.text(te) if te else None
        if raw and raw not in ("unknown", "unavailable"):
            try:
                anchor = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                anchor = None
        if anchor is None and e and hasattr(self.ha, "state"):
            st = self.ha.state(e) or {}
            try:
                anchor = datetime.fromisoformat(str(st.get("last_changed")).replace("Z", "+00:00"))
            except ValueError:
                anchor = None
        if anchor is not None and anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=UTC)
        return carmod.estimate_soc(soc, anchor, list(self._charge_log), now.astimezone(UTC),
                                   float(opts["car_eta"]), float(opts["car_battery_kwh"]))

    def _heat_pump_forecast(self, opts, profile, buy, sell, pv, h_now, now):
        """{utc hour: kWh to add to the base load} for the next 24 hours: what the heat pump uses at the
        level it will run with (learned) minus its clock-hour profile. Only while the add-on controls it."""
        meter, entity = opts.get("heat_pump_power_entity"), opts.get("heat_pump_entity")
        learned = heatpump.learn_levels(self._hp_levels, self._hp_power, now,
                                        float(opts.get("profile_half_life_days") or 10))
        if not meter or not entity or not learned:
            return {}, learned
        st = self.ha.state(entity) if hasattr(self.ha, "state") else None
        if (not writer.switch_on(self.ha, opts.get("heat_pump_production_switch_entity"))
                or not st or st.get("state") in ("unavailable", "unknown")
                or (st.get("state") == "off" and not self._hp_state.get("off_by_us"))):
            return {}, learned
        rows = []
        for i in range(24):
            h = h_now + timedelta(hours=i)
            if h not in buy:
                break
            free = sell.get(h, 1.0) <= 0 and pv.get(h, 0.0) > load_for_hour(profile, h, self.tz)
            rows.append({"t": h.astimezone(self.tz).isoformat(), "buy": buy[h], "sell": sell.get(h, 0.0),
                         "pv_exp": 0.0, "pv_curt": 1.0 if free else 0.0})
        parts = profile.get("parts") or {}
        adj = {}
        for r, level in heatpump.price_levels(rows, now.astimezone(self.tz), opts, self._price_history):
            h = datetime.fromisoformat(r["t"]).astimezone(UTC)
            local = h.astimezone(self.tz)
            part = parts.get((local.weekday() >= 5, local.hour))
            if level in learned and part and meter in part["subs"]:
                adj[h] = learned[level]["kwh"] - part["subs"][meter]
        return adj, learned

    def _heat_pump_step(self, opts: dict, rows: list, now: datetime):
        """Decide the garage heat pump's setpoint (and on / off) for this hour and apply it (production
        helper on). It is only switched on again if the add-on switched it off; one the user switched
        off is left alone."""
        entity = opts.get("heat_pump_entity")
        if not entity:
            return None
        st = self.ha.state(entity) if hasattr(self.ha, "state") else None
        if not st:
            return {"available": False, "entity": entity}
        num = lambda key: self.ha.number(opts[key]) if opts.get(key) else None
        d = heatpump.decide(rows, now.astimezone(self.tz), num("heat_pump_temp_entity"),
                            num("heat_pump_humidity_entity"), opts, history=self._price_history,
                            forced_on=bool(self._hp_state.get("forced_on")))
        attrs = st.get("attributes") or {}
        current = attrs.get("temperature")
        hvac = st.get("state")
        off_by_us = self._hp_state.get("off_by_us")
        if hvac != "off" and off_by_us:
            off_by_us = None                   # switched on by someone else meanwhile: ours again from here
        production = writer.switch_on(self.ha, opts.get("heat_pump_production_switch_entity"))
        controlled = production and hvac not in ("unavailable", "unknown", None) and (hvac != "off" or off_by_us)
        d.update(entity=entity, available=True, current_setpoint=current, hvac_mode=hvac,
                 production=production, written=None, off_by_us=bool(off_by_us))
        heatpump.record_level(self._hp_levels, hour_floor(now), d["level"], d["setpoint"], bool(controlled), now)
        pvd.save_json(self.hp_levels_path, self._hp_levels)
        d["expected"] = heatpump.learn_levels(self._hp_levels, self._hp_power, now,
                                              float(opts.get("profile_half_life_days") or 10))

        def call(service, data, what):
            try:
                if hasattr(self.ha, "set_allowed_entities"):
                    self.ha.set_allowed_entities(writer.allowed_entities(opts))
                self.ha.call_service("climate", service, {"entity_id": entity, **data})
                log.info("heat pump: %s (%s)", what, d["reason"])
                return {"what": what, "status": "ok"}
            except Exception as e:
                log.warning("heat pump: could not %s: %s", what, e)
                return {"what": what, "status": "error", "error": str(e)}

        hour = now.astimezone(self.tz).replace(minute=0, second=0, microsecond=0)
        if controlled:
            if d["hvac"] == "off" and hvac != "off":
                d["written"] = call("set_hvac_mode", {"hvac_mode": "off"}, f"off (was {hvac})")
                if d["written"]["status"] == "ok":
                    off_by_us = hvac
            elif d["hvac"] == "on" and hvac == "off" and off_by_us:
                d["written"] = call("set_hvac_mode", {"hvac_mode": off_by_us}, f"on ({off_by_us})")
                if d["written"]["status"] == "ok":
                    off_by_us, hvac, current = None, off_by_us, None
            if d["hvac"] == "on" and hvac != "off":
                try:
                    differs = current is None or abs(float(current) - d["setpoint"]) >= 0.5
                except (TypeError, ValueError):
                    differs = True
                if differs and (self._hp_written_hour != hour or d.get("written")):
                    w = call("set_temperature", {"temperature": d["setpoint"]}, f"{current} -> {d['setpoint']} °C")
                    if w["status"] == "ok":
                        self._hp_written_hour = hour
                    d["written"] = {**w, "from": current, "to": d["setpoint"],
                                    **({"hvac": d["written"]} if d.get("written") else {})}
        new_state = {"off_by_us": off_by_us, "forced_on": bool(d.get("forced_on"))}
        if new_state != self._hp_state:
            self._hp_state = new_state
            pvd.save_json(self.hp_state_path, self._hp_state)
        d["off_by_us"] = bool(off_by_us)
        nxt = next((x for x in d["plan"][1:] if (x["level"] == "off") != (d["plan"][0]["level"] == "off")
                    or x["setpoint"] != d["plan"][0]["setpoint"]), None) if d["plan"] else None
        state = "off" if d["hvac"] == "off" else d["setpoint"]
        self.ha.set_state("sensor.energy_planner_heat_pump", state, {
            "friendly_name": "Energy Planner varmepumpe", "icon": "mdi:heat-pump",
            **({"unit_of_measurement": "°C"} if state != "off" else {}),
            "hvac": d["hvac"], "level": d["level"], "reason": d["reason"], "room_temp": d["room_temp"],
            "humidity": d["humidity"], "current_setpoint": current, "production": production,
            "next_change": nxt["t"] if nxt else None, "next_setpoint": nxt["setpoint"] if nxt else None,
            "next_level": nxt["level"] if nxt else None})
        return d

    def _learn_appliances(self, opts: dict, now: datetime, cfg: list):
        """Runs of the appliance meters from the 5-minute statistics (about 10 days), once an hour."""
        if self._appl_learned_at and now - self._appl_learned_at < timedelta(hours=1):
            return
        self._appl_learned_at = now
        try:
            stats = self.ha.statistics([c["entity"] for c in cfg], now - timedelta(days=10), now, "5minute")
        except Exception as e:  # best effort: the runs kept in /data and the defaults stay
            log.info("appliance runs: 5-minute statistics unavailable: %s", e)
            return
        dryer_w = float(opts.get("appliance_dryer_min_w", 700))
        for c in cfg:
            runs = appl.split_runs(stats.get(c["entity"]))
            groups = [[], []]
            for r in runs:
                if datetime.fromisoformat(r["start"]) + timedelta(minutes=r["minutes"] + GAP_DONE_MIN) > now:
                    continue                  # still running (or just paused): not complete yet
                groups[appl.classify(r, dryer_w) if len(c["names"]) > 1 else 0].append(r)
            for name, rs in zip(c["names"], groups):
                appl.remember(self._appl_runs, name, rs)
        pvd.save_json(self.appliance_runs_path, self._appl_runs)

    def _appliances_step(self, opts: dict, rows: list, now: datetime) -> list:
        """sensor.energy_planner_appliances: the best start of each appliance in the next 24 hours."""
        cfg = appl.parse_config(opts.get("appliances") or "")
        if not cfg:
            return []
        self._learn_appliances(opts, now, cfg)
        local = now.astimezone(self.tz)
        out = []
        for c in cfg:
            for name in c["names"]:
                prog = appl.program(name, self._appl_runs.get(name, []))
                best = appl.best_start(prog, rows, local)
                if best:
                    out.append({"name": name, **best, "minutes": prog["minutes"], "kwh": prog["kwh"],
                                "learned": prog["learned"], "learned_runs": prog["runs"]})
        state, attrs = appliance_advice(out, local)
        self.ha.set_state("sensor.energy_planner_appliances", state, {
            "friendly_name": "Energy Planner maskiner", "icon": "mdi:dishwasher", **attrs,
            "appliances": out, "updated": local.isoformat(timespec="seconds")})
        return out

    MODE_SWITCH_KWH = 0.15     # within an hour, hold <-> discharge only for at least this much energy
    MIN_SOC_DEADBAND = 2       # percentage points: smaller floor changes are not written

    def _stabilize(self, sp: dict, plan, now: datetime, soc_pct: float) -> dict:
        """Calmer setpoints between the 5-minute plans:
        - within the same clock hour, hold <-> discharge only switch when the new plan moves at least
          MODE_SWITCH_KWH through the battery in the rest of the hour (near-ties flipped every 5 min),
        - a hold keeps the floor it started with (it does not follow the SoC up while sun charges),
        - small changes of the floor are not written (deadband), except when the mode changes."""
        hour = hour_floor(now)
        prev_hour, prev = self._mode_state or (None, None)
        out = dict(sp)
        out["min_soc_deadband"] = self.MIN_SOC_DEADBAND
        if prev is not None and prev_hour == hour and {prev["mode"], sp["mode"]} == {"hold", "discharge"}:
            bat_out = float(plan.bat_load[0]) + (float(plan.bat_car[0]) if plan.bat_car is not None else 0.0)
            if (sp["mode"] == "discharge" and bat_out < self.MODE_SWITCH_KWH) or \
                    (sp["mode"] == "hold" and bat_out > 0.01):
                kept = dict(prev)
                kept["kept_mode"] = f"{prev['mode']} kept: {sp['mode']} would move {bat_out:.2f} kWh"
                log.info("keeping %s within the hour (%s would move %.2f kWh)", prev["mode"], sp["mode"], bat_out)
                self._mode_state = (hour, kept)
                return kept
        if prev is not None and out["mode"] == "hold" and out.get("hold_via") == "load_first" \
                and prev["mode"] == "hold" and prev.get("hold_via") == "load_first":
            out["min_soc_pct"] = prev["min_soc_pct"]           # the floor the hold started with
        if prev is None or prev["mode"] != out["mode"] or prev.get("hold_via") != out.get("hold_via"):
            out["min_soc_deadband"] = 0                        # a new mode always gets its floor
        self._mode_state = (hour, out)
        return out

    @staticmethod
    def _min_session(solve_fn, cp, opts, keep_first: bool = False, critical: bool = False):
        """Solve with the car, then take the car out of the hours of sessions that bring it less than
        car_min_session_kwh and solve again (at most 3 times). -> (plan, car params, dropped: bool)."""
        pl = solve_fn(cp)
        min_kwh = float(opts.get("car_min_session_kwh", 0) or 0)
        if cp is None or critical or min_kwh <= 0:
            return pl, cp, False
        dropped = False
        for _ in range(3):
            kwh = [float(x) for x in (pl.pv_car + pl.grid_car + pl.bat_car)]
            drop = [i for i in carmod.small_sessions(kwh, min_kwh, opts.get("car_pause_gap_hours", 0), keep_first)
                    if cp.available[i]]
            if not drop:
                break
            off = set(drop)
            cp = dataclasses.replace(cp, available=[a and i not in off for i, a in enumerate(cp.available)])
            pl, dropped = solve_fn(cp), True
        return pl, cp, dropped

    def _car_preview(self, horizon, now, opts, params, soc0, tv, fuse_amps, exp_min, car_soc, full, departure,
                     charge_lines=None):
        """What the plan would do with the car if it were plugged in now (nothing is written)."""
        cp, info = carmod.build_car_params(horizon, now, self.tz, opts, "suspended_evse", car_soc,
                                           full=full, departure=departure, extra=self._car_extra)
        if cp is None:
            return None
        pl, cp, dropped = self._min_session(
            lambda c: solve(horizon.buy, horizon.sell, horizon.pv, horizon.load, horizon.dt, soc0, params, tv,
                            car=c, fuse_amps=fuse_amps, export_min_price=exp_min, charge_lines=charge_lines),
            cp, opts, critical=car_soc is not None and car_soc < float(opts["car_critical_start_pct"]))
        cap = float(opts["car_battery_kwh"])
        rows, windows, cur = [], [], None
        for i, start in enumerate(horizon.starts):
            if not cp.available[i]:
                break
            kwh = float(pl.pv_car[i] + pl.grid_car[i] + pl.bat_car[i])
            t = start.astimezone(self.tz)
            rows.append({"t": t.isoformat(), "dt": round(float(horizon.dt[i]), 3), "buy": round(float(pl.buy[i]), 4),
                         "sell": round(float(pl.sell[i]), 4), "car_grid": round(float(pl.grid_car[i]), 2),
                         "car_pv": round(float(pl.pv_car[i]), 2), "car_bat": round(float(pl.bat_car[i]), 2),
                         "car_soc": round(float(pl.car_end[i]) / cap * 100.0, 1)})
            if kwh > 0.1:
                if cur and cur["end_i"] == i - 1:
                    cur.update(end=(t + timedelta(hours=1)).isoformat(), end_i=i,
                               solar=cur["solar"] and pl.pv_car[i] >= pl.grid_car[i])
                else:
                    cur = {"start": t.isoformat(), "end": (t + timedelta(hours=1)).isoformat(), "end_i": i,
                           "solar": bool(pl.pv_car[i] >= pl.grid_car[i])}
                    windows.append(cur)
        for w in windows:
            w.pop("end_i", None)
        planned = float((pl.pv_car + pl.grid_car + pl.bat_car).sum())
        cost = float((pl.grid_car * pl.buy).sum())
        deadline = datetime.fromisoformat(info["deadline"])
        steps = [i for i, st in enumerate(horizon.starts) if st + timedelta(hours=1) <= deadline]
        at_deadline = float(pl.car_end[steps[-1]]) / cap * 100.0 if steps else float(car_soc)
        ext = info.get("extra") or {}
        if planned <= 0.1:
            reason = "none"
        elif carmod.needs(car_soc, False, opts)["minimum"]:
            reason = "minimum"
        elif ext.get("status") == "planned":
            reason = "extra"
        elif (departure or {}).get("status") == "planned":
            reason = "departure"
        elif (full or {}).get("active"):
            reason = "full"
        else:
            reason = "cheap"
        out = {"soc_now": car_soc, "planned_kwh": round(planned, 2), "cost_dkk": round(cost, 2),
               "small_dropped": dropped, "min_session_kwh": float(opts.get("car_min_session_kwh", 0) or 0),
               "soc_at_deadline": round(at_deadline, 1), "deadline": info["deadline"],
               "max_soc_pct": info.get("max_soc_pct"), "windows": windows, "reason": reason, "rows": rows,
               "min_soc_pct": float(opts["car_min_stop_pct"]), "value_dkk": float(opts["car_value_dkk_per_kwh"])}
        out["text"] = _preview_text(out, self.tz, now)
        return out

    @staticmethod
    def _car_needs_export(plan, car_info: dict, opts: dict) -> bool:
        """Open export for the car's solar charging: connected, below its limit, sun for the car in the
        plan this hour, and the hour's sell price not below export_car_min_price."""
        if plan.pv_car is None or not car_info.get("connected"):
            return False
        soc, limit = car_info.get("soc_pct"), car_info.get("max_soc_pct")
        if soc is None or (limit is not None and soc >= float(limit) - 0.5):
            return False
        dt0 = float(plan.dt[0]) or 1.0
        if float(plan.pv_car[0]) / dt0 <= 0.1:
            return False
        return float(plan.sell[0]) >= float(opts.get("export_car_min_price", -0.10))

    def _charger_schedule(self, opts: dict):
        """The THOR's own automatic charging window as ("03:00", "05:00"), or None when it is off
        (entities not configured / unavailable, or start = stop)."""
        ids = split_csv(opts.get("charger_schedule_entities") or "")
        if len(ids) != 2:
            return None
        vals = [self.ha.text(e) for e in ids]
        if any(not v or v in ("unknown", "unavailable") for v in vals):
            return None
        start, stop = (v[:5] for v in vals)
        return None if start == stop else (start, stop)

    def _charger_schedule_warning(self, opts: dict):
        sched = self._charger_schedule(opts) if opts.get("car_enabled") else None
        if not sched:
            return None
        return (f"THOR har sin egen ladeplan ({sched[0]}–{sched[1]}). Den kan afslutte add-on'ens session ved de "
                "tider, og så kan bilen ikke lade igen, før kablet tages ud og ind. Slå den fra i ShinePhone.")

    def _learn_battery_curve(self, opts: dict, now: datetime):
        """The battery's charge power at high SoC from the 5-minute statistics (about 10 days)."""
        ids = [opts.get("battery_charge_power_entity"), opts.get("battery_soc_entity")]
        if not all(ids):
            return
        try:
            stats = self.ha.statistics(ids, now - timedelta(days=10), now, "5minute")
        except Exception as e:  # best effort: without it the default curve is used
            log.info("battery curve: 5-minute statistics unavailable: %s", e)
            return
        self._bat_curve = batterycurve.learn(stats.get(ids[0]), stats.get(ids[1]), float(opts["battery_max_power_kw"]),
                                             float(opts.get("battery_taper_start_pct", 90)),
                                             float(opts.get("battery_taper_end_kw", 1.0)))

    def _charge_lines(self, opts: dict, horizon) -> tuple:
        """Per step, the LP lines for the BMS taper (see batterycurve), and the curve used."""
        max_kw = float(opts["battery_max_power_kw"])
        info = self._bat_curve or {"curve": batterycurve.default_curve(
            max_kw, float(opts.get("battery_taper_start_pct", 90)), float(opts.get("battery_taper_end_kw", 1.0))),
            "learned": False, "samples": {}}
        cap = float(opts["battery_capacity_kwh"])
        cache = {}
        lines = []
        for dt in horizon.dt:
            key = round(float(dt), 2)
            if key not in cache:
                cache[key] = batterycurve.step_lines(info["curve"], cap, max_kw, key)
            lines.append(cache[key])
        return lines, info

    def _refresh_car_history(self, opts: dict, now: datetime):
        """Car SoC (90 days, for the last full charge) and buy prices (30 days, for "rock bottom" and the
        heat pump's "off" hours)."""
        keys = ("car_soc_entity",) if opts.get("car_enabled") else ()
        ids = [opts[k] for k in (*keys, "price_buy_entity", "price_buy_fallback_entity") if opts.get(k)]
        if not ids or not (opts.get("car_enabled") or opts.get("heat_pump_entity")):
            return
        try:
            stats = self.ha.statistics(ids, now - timedelta(days=90), now, "hour")
        except Exception as e:  # best effort: without it a 100 % charge only happens on negative prices
            log.info("car/price history unavailable: %s", e)
            return
        self._car_full_stats = carmod.last_full_from_stats(stats.get(opts.get("car_soc_entity")))
        cutoff = now - timedelta(days=30)
        prices = merge_prices(price_series(stats.get(opts.get("price_buy_entity"))),
                              price_series(stats.get(opts.get("price_buy_fallback_entity"))))
        self._price_history = [v for t, v in sorted(prices.items()) if t >= cutoff]

    def _last_full(self):
        seen = datetime.fromisoformat(self._car_full_seen) if self._car_full_seen else None
        cands = [t for t in (seen, self._car_full_stats) if t is not None]
        return max(cands) if cands else None

    def _departure(self, opts: dict):
        """The trip helper (input_datetime with date and time) as an aware UTC datetime, or None."""
        e = opts.get("car_departure_entity")
        st = self.ha.state(e) if e and hasattr(self.ha, "state") else None
        raw = (st or {}).get("state")
        if not raw or raw in ("unknown", "unavailable"):
            return None
        attrs = (st or {}).get("attributes") or {}
        if attrs.get("has_date") is False or attrs.get("has_time") is False:
            return None                      # a trip needs both a date and a time
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=self.tz)
        return dt.astimezone(self.tz)          # local, so the panel shows it as set

    def set_car_extra(self, kind: str, amount: float, now: datetime = None) -> dict:
        """Order an extra charge from the panel: "pct" adds percentage points to the car's SoC now,
        "target" is an absolute SoC. Locked at the order and valid until the trip (if set) or the next
        deadline. A new order replaces the previous one."""
        opts = self.options_fn()
        now = (now or datetime.now(UTC)).astimezone(UTC)
        amount = float(amount)
        if not opts.get("car_enabled"):
            raise ValueError("bilen er ikke slået til")
        if not 1 <= amount <= 100:
            raise ValueError("værdien skal være mellem 1 og 100")
        soc = self.ha.number(opts["car_soc_entity"])
        if kind == "pct":
            if soc is None:
                raise ValueError("bilens SoC er ukendt")
            target, label = min(100.0, soc + amount), f"+{amount:g} %"
        elif kind == "target":
            if soc is not None and amount <= soc:
                raise ValueError(f"bilen er allerede på {soc:.0f} %")
            target, label = amount, f"Mål {amount:g} %"
        else:
            raise ValueError("ukendt type")
        until = carmod.next_deadline(now, self.tz, int(opts["car_deadline_hour"]))
        dep = self._departure(opts)
        if dep is not None and dep.astimezone(UTC) > now:
            until = dep.astimezone(UTC)
        self._car_extra = {"kind": kind, "amount": amount, "label": label, "target_pct": round(target, 1),
                           "soc_at_order": soc, "until": until.astimezone(self.tz).isoformat(),
                           "requested_at": now.astimezone(self.tz).isoformat(timespec="seconds")}
        pvd.save_json(self.car_extra_path, self._car_extra)
        log.info("extra car charge ordered: %s -> %.0f %% by %s", label, target, self._car_extra["until"])
        return self._car_extra

    def clear_car_extra(self):
        if self._car_extra:
            log.info("extra car charge cancelled")
        self._car_extra = None
        pvd.save_json(self.car_extra_path, {})

    def _departure_soc(self, opts: dict) -> float:
        e = opts.get("car_departure_soc_entity")
        v = self.ha.number(e) if e else None
        return float(min(max(v if v is not None else float(opts.get("car_departure_soc", 100)), 0), 100))

    def _prices(self, opts: dict, now: datetime = None) -> tuple:
        """Known buy / sell prices (the price sensors, gaps from the fallback sensors) and the buy
        price forecast. -> (buy, sell, buy_forecast), each {utc hour: DKK/kWh}."""
        def read(key):
            e = opts.get(key)
            return parse_prices(self.ha.attributes(e) or {}) if e else {}
        buy = merge_prices(read("price_buy_entity"), read("price_buy_fallback_entity"))
        sell = merge_prices(read("price_sell_entity"), read("price_sell_fallback_entity"))
        return buy, sell, self._price_forecast(opts, read("price_buy_forecast_entity"), now)

    FORECAST_BACKFILL_DAYS = 3

    def _price_forecast(self, opts: dict, current: dict, now: datetime = None) -> dict:
        """The buy price forecast, remembered: Strømligning's forecast sensor starts two days ahead, so
        tomorrow's forecast (needed before tomorrow's real prices come at ~13:00) was only in the sensor
        yesterday. Each forecast seen is kept per hour (the newest wins) in /data; at start the last few
        days are fetched from HA's history so nothing is lost across restarts."""
        e = opts.get("price_buy_forecast_entity")
        if not e:
            return current
        now = (now or datetime.now(UTC)).astimezone(UTC)
        cache = self._price_fc
        if not self._price_fc_backfilled and hasattr(self.ha, "history"):
            self._price_fc_backfilled = True
            try:
                for st in self.ha.history(e, now - timedelta(days=self.FORECAST_BACKFILL_DAYS), now) or []:
                    for h, v in parse_prices(st.get("attributes") or {}).items():
                        cache[h.isoformat()] = v
            except Exception as ex:  # best effort: the current sensor and the saved forecasts remain
                log.info("price forecast history unavailable: %s", ex)
        for h, v in (current or {}).items():
            cache[h.isoformat()] = v
        cutoff = (hour_floor(now) - timedelta(hours=2)).isoformat()
        for k in [k for k in cache if k < cutoff]:
            del cache[k]
        if cache != self._price_fc_saved:
            pvd.save_json(self.price_fc_path, cache)
            self._price_fc_saved = dict(cache)
        return {datetime.fromisoformat(k): v for k, v in cache.items()}

    def sell_price_now(self, opts: dict):
        """The current sell price (the fallback sensor when the main one is unavailable)."""
        for key in ("price_sell_entity", "price_sell_fallback_entity"):
            v = self.ha.number(opts[key]) if opts.get(key) else None
            if v is not None:
                return v
        return None

    def _check_entities(self, opts: dict) -> dict:
        """Which configured entities are missing or unavailable (shown in the UI)."""
        ids = [opts[k] for k in ("price_buy_entity", "price_sell_entity", "price_buy_forecast_entity",
                                 "price_buy_fallback_entity", "price_sell_fallback_entity", "battery_soc_entity",
                                 "house_load_entity", "car_power_entity", "charger_power_entity") if opts.get(k)]
        ids += split_csv(opts.get("load_submeters") or "")
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

        buy, sell, buy_fc = self._prices(opts, now)
        if not buy or not sell:
            raise PlannerError("price sensors are unavailable")

        sol_attrs = [self.ha.attributes(e) for e in split_csv(opts["solcast_entities"])]
        pv, pv50, pv10, pv90 = parse_solcast(sol_attrs, float(opts["pv_p10_weight"]), with_p90=True)
        if not pv:
            warnings.append("no solar forecast available - planning without solar")
        # Statistics first: the solar correction learns from the actual production in them.
        profile = self._profile_for(opts, now)
        pv_factors, damp_info = {}, None
        if pv:
            try:
                pv, pv50, pv10, pv90, pv_factors, damp_info = self._pv_dampening(opts, now, sol_attrs, pv50, pv10, pv90)
            except Exception as e:  # the correction must never stop planning
                log.warning("solar dampening skipped: %s", e)

        soc_pct = self.ha.number(opts["battery_soc_entity"])
        if soc_pct is None:
            raise PlannerError("battery SoC is unavailable")

        # Prices for the rest of the horizon (tonight, before ~13:00) are estimated - from the price
        # sensor's forecast where it has one, else from the last known day - so the plan can weigh
        # today's sun against tonight's grid price.
        until = hour_floor(now) + timedelta(hours=int(opts["min_horizon_hours"]))
        buy, sell, price_src = fill_prices(buy, sell, buy_fc, until, self.tz)
        forecast_hours = set(price_src)

        # What the separately measured appliances draw right now (kW): the current hour uses it
        # instead of their profile, the next hour half of it while they run.
        live_subs = {}
        for e in split_csv(opts.get("load_submeters") or ""):
            v = self.ha.number(e)
            if v is not None:
                live_subs[e] = max(0.0, v) / 1000.0
        h_now = hour_floor(now)
        hp_adj, hp_expected = self._heat_pump_forecast(opts, profile, buy, sell, pv, h_now, now)

        def load_fn(h):
            base = load_for_hour(profile, h, self.tz, live=live_subs, now_hour=h_now)
            # from two hours ahead (the live meters cover the first two), the heat pump's expected use for
            # the level it will run with replaces its clock-hour profile
            if h >= h_now + timedelta(hours=2):
                base = max(0.0, base + hp_adj.get(h, 0.0))
            return base
        horizon = build_horizon(now, buy, sell, pv, load_fn, forecast_hours=forecast_hours)
        if len(horizon) == 0:
            raise PlannerError("no prices for the current hour")
        diag = profile.get("diagnostics") or {}
        if profile["hours_of_data"] and opts.get("car_power_entity") and not diag.get("car_rows"):
            warnings.append("the car charger power has no long-term statistics - hours above car_detect_kw are taken as car charging")
        n_fc = sum(horizon.forecast)
        n_pf = sum(1 for t in horizon.starts if price_src.get(t) == SOURCE_FORECAST)
        if n_fc - n_pf:
            warnings.append(f"{n_fc - n_pf} of {len(horizon)} hours use estimated prices (same hour of the last known day)")

        params = battery_params(opts)
        soc0 = params.capacity_kwh * soc_pct / 100.0
        tv = terminal_value_for(horizon.buy, params)

        car_params, car_info = None, {"enabled": bool(opts.get("car_enabled"))}
        preview_args = None
        if opts.get("car_enabled"):
            status = self.ha.text(opts["charger_status_entity"])
            soc_est = self.car_soc_now(opts, now)
            car_soc = soc_est["soc"]
            full = carmod.full_charge_decision(horizon, now, self.tz, opts, car_soc, self._last_full(),
                                               self._price_history)
            departure = carmod.departure_info(self._departure(opts), self._departure_soc(opts), horizon, now)
            if self._car_extra and carmod.extra_info(self._car_extra, horizon, now).get("status") == "passed":
                self.clear_car_extra()             # the order expires with its deadline
            car_params, info = carmod.build_car_params(horizon, now, self.tz, opts, status, car_soc,
                                                       full=full, departure=departure, extra=self._car_extra)
            car_info.update(info)
            car_info.update(full_charge=full, departure=departure, soc_estimate=soc_est)
            if status not in carmod.CONNECTED and car_soc is not None:
                preview_args = (car_soc, full, departure)
            if self._car_extra and "extra" not in car_info:   # ordered, but the car is not connected
                car_info["extra"] = {**self._car_extra, "set": True, "status": "waiting"}
            sched_warn = self._charger_schedule_warning(opts)
            if sched_warn:
                warnings.append(sched_warn)
            if status in carmod.CONNECTED and car_soc is None:
                warnings.append("car is connected but its SoC is unavailable - car left out of the plan")

        fuse_amps = (float(opts["main_fuse_amps"]) - float(opts.get("fuse_margin_amps", 0))
                     if opts.get("main_fuse_amps") else None)
        exp_min = float(opts["export_min_price"]) if opts.get("export_control") and opts.get("export_min_price") is not None else None
        charge_lines, curve_info = self._charge_lines(opts, horizon)
        crit = bool(car_params) and carmod.needs(car_soc, status == "charging", opts)["critical"]
        plan, car_params, _ = self._min_session(
            lambda c: solve(horizon.buy, horizon.sell, horizon.pv, horizon.load, horizon.dt, soc0, params, tv,
                            car=c, fuse_amps=fuse_amps, export_min_price=exp_min, charge_lines=charge_lines),
            car_params, opts, keep_first=bool(car_params) and status == "charging", critical=crit)
        solar_only = solve(horizon.buy, horizon.sell, horizon.pv, horizon.load, horizon.dt, soc0, params, tv,
                           allow_grid_charge=False, car=car_params, fuse_amps=fuse_amps, export_min_price=exp_min,
                           charge_lines=charge_lines)
        phases = self._phase_amps(opts)
        if opts.get("main_fuse_amps") and split_csv(opts.get("grid_current_entities") or "") and phases is None:
            warnings.append("the phase current sensors (grid_current_entities) are unavailable - "
                            "no live main fuse guard, the plan still respects the fuse")
        fuse = {"fuse_amps": opts.get("main_fuse_amps"), "margin_amps": opts.get("fuse_margin_amps"),
                "plan_limit_amps": fuse_amps, "phase_amps": phases,
                "max_phase_amps": round(max(abs(a) for a in phases), 1) if phases else None,
                "battery_rate_cap_pct": self._fuse_rate_cap}
        if car_params:
            dep = car_info.get("departure") or {}
            if dep.get("status") == "planned":
                reached = float(plan.car_end[dep["step"]]) / float(opts["car_battery_kwh"]) * 100.0
                car_info["departure_reached_pct"] = round(reached, 1)
                car_info["departure_ok"] = reached >= float(dep["soc"]) - 1.0
                if not car_info["departure_ok"]:
                    warnings.append(f"the car only reaches {reached:.0f} % by the departure")
            ext = car_info.get("extra") or {}
            if ext.get("status") == "planned":
                ext["planned_pct"] = round(float(plan.car_end[ext["step"]]) / float(opts["car_battery_kwh"]) * 100.0, 1)
            car_info["planned_kwh"] = round(float((plan.pv_car + plan.bat_car + plan.grid_car).sum()), 2)
            car_info["planned_cost_dkk"] = round(float((plan.grid_car * plan.buy).sum()), 2)
            car_info["shortfall_kwh"] = round(plan.car_shortfall, 2)
            if plan.car_shortfall > 0.05:
                warnings.append(f"car minimum cannot be reached by the deadline ({plan.car_shortfall:.1f} kWh short)")
        savings = solar_only.net_cost - plan.net_cost
        car_preview = None
        if preview_args:
            try:
                car_preview = self._car_preview(horizon, now, opts, params, soc0, tv, fuse_amps, exp_min, *preview_args,
                                                charge_lines=charge_lines)
            except Exception as e:  # the preview must never stop planning
                log.info("car preview skipped: %s", e)

        sp = derive_setpoints(plan, soc_pct, params, float(opts["inverter_kw"]),
                              float(opts["soc_floor_pct"]), float(opts["soc_ceiling_pct"]),
                              export_min_price=exp_min)
        sp = self._stabilize(sp, plan, now, soc_pct)
        if exp_min is not None:
            sp["export_reason"] = "price" if sp["export_allowed"] else "stopped"
            if not sp["export_allowed"] and self._car_needs_export(plan, car_info, opts):
                # Export closed and the house battery full: the inverter throttles the panels to the
                # load and the car controller would see no surplus. Keep export open while the plan
                # charges the car on sun (the car takes nearly all of it; a few øre exported).
                sp.update(export_allowed=True, export_limit_pct=100, export_reason="car")

        rows = []
        for i, start in enumerate(horizon.starts):
            rows.append({
                "t": start.astimezone(self.tz).isoformat(timespec="minutes"),
                "buy": round(float(plan.buy[i]), 3), "sell": round(float(plan.sell[i]), 3),
                "pv": round(float(plan.pv[i]), 2), "load": round(float(plan.load[i]), 2),
                "pv_bat": round(float(plan.pv_bat[i]), 2), "grid_bat": round(float(plan.grid_bat[i]), 2),
                "bat_load": round(float(plan.bat_load[i]), 2), "pv_exp": round(float(plan.pv_exp[i]), 2),
                "pv_curt": round(float(plan.pv_curt[i]), 2), "pv_load": round(float(plan.pv_load[i]), 2),
                "soc_pct": round(float(plan.soc_end[i]) / params.capacity_kwh * 100.0, 1),
                "forecast_price": bool(horizon.forecast[i]),
                "price_source": price_src.get(horizon.starts[i], "known"),
                "dt": round(float(plan.dt[i]), 3),
                "grid_load": round(float(plan.grid_load[i]), 2),
                "pv_p10": round(pv10.get(start, 0.0) * float(plan.dt[i]), 2),
                "pv_p90": round(pv90.get(start, 0.0) * float(plan.dt[i]), 2),
                "mode": step_mode(plan, i),
                "pv_factor": pv_factors.get(start),
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
            "load_profile_kw": round(load_for_hour(profile, h0, self.tz), 3),
            "submeters_now_kw": round(sum(live_subs.values()), 3) if live_subs else None,
        }
        samples = {"house_load_w": self.ha.number(opts["house_load_entity"]),
                   "car_w": self.ha.number(opts["charger_power_entity"]) if opts.get("charger_power_entity") else None}
        # Production: write the registers when the helper is on (off = manual control, nothing written).
        production = writer.switch_on(self.ha, opts.get("production_switch_entity"))
        writes = []
        live_sp = adjust_for_soc(sp, soc_pct)
        if production:
            live_sp = self._fuse_guard_battery(live_sp, opts)
            if hasattr(self.ha, "set_allowed_entities"):
                self.ha.set_allowed_entities(writer.allowed_entities(opts))
            writes = writer.apply(self.ha, opts, live_sp, now.astimezone(self.tz))
            for w in writes:
                if w["status"] == "error":
                    warnings.append(f"could not write {w['entity']}: {w.get('error')}")
                else:
                    log.info("wrote %s: %s -> %s (%s)", w["entity"], w["from"], w["to"], w["status"])
            if writes:
                actual = {k: self.ha.text(opts[k]) for k in REGISTER_KEYS if opts.get(k)}
        snapshot = {
            "updated": now.astimezone(self.tz).isoformat(timespec="seconds"),
            "production": {"battery": production,
                           "car": writer.switch_on(self.ha, opts.get("car_production_switch_entity")),
                           "writes": writes},
            "soc_pct": soc_pct,
            "mode": sp["mode"],
            "setpoints": sp,
            "setpoints_live": live_sp,
            "fuse": fuse,
            "car_preview": car_preview,
            "battery_curve": {"curve": curve_info["curve"], "learned": curve_info["learned"],
                              "samples": curve_info.get("samples", {}),
                              "default": curve_info.get("default") or batterycurve.default_curve(
                                  float(opts["battery_max_power_kw"]), float(opts.get("battery_taper_start_pct", 90)),
                                  float(opts.get("battery_taper_end_kw", 1.0)))},
            "export_min_price": exp_min,
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
            "submeters_now": {e: round(v, 3) for e, v in live_subs.items()},
            "entity_check": self.entity_check,
            "warnings": warnings,
            "car": car_info,
            "pv_dampening": damp_info,
            "solar_forecast": self._solar_forecast if pv else [],
            "plan": rows,
        }
        car_label = "Lader bilen"
        if car_info.get("departure", {}).get("status") == "planned":
            dep = datetime.fromisoformat(car_info["departure"]["at"]).astimezone(self.tz)
            car_label = f"Lader bilen til afgang {['man.', 'tirs.', 'ons.', 'tors.', 'fre.', 'lør.', 'søn.'][dep.weekday()]} {dep:%H:%M}"
        elif (car_info.get("extra") or {}).get("status") == "planned":
            car_label = f"Lader bilen til {car_info['extra']['target_pct']:.0f} % (ekstra)"
        elif car_info.get("full_charge", {}).get("active"):
            car_label = "Lader bilen til 100 % (bundpris)"
        snapshot["heat_pump"] = self._heat_pump_step(opts, rows, now)
        try:
            snapshot["appliances"] = self._appliances_step(opts, rows, now)
        except Exception as e:  # the advice must never stop planning
            log.warning("appliance advice skipped: %s", e)
            snapshot["appliances"] = []
        snapshot["upcoming"] = upcoming(rows, bool(car_params), now.astimezone(self.tz).date(), car_label)
        snapshot["summary"] = summary_attributes(sp, snapshot["upcoming"])
        with self._lock:
            self.latest = snapshot
            self.last_error = None
        self._publish(snapshot)
        self._publish_front(snapshot, now)
        self._publish_pv_dampening(snapshot)
        self._log_decision(snapshot)
        self._publish_cost(opts, now)
        self._publish_savings(opts, now)
        return snapshot

    # ---- outputs -------------------------------------------------------------
    def _publish(self, snap: dict):
        sp = snap["setpoints"]
        common = {"production": snap["production"]["battery"], "updated": snap["updated"]}
        summ = snap.get("summary", {})
        title = f"{summ.get('label', snap['mode'])} · {snap['soc_pct']:.0f} %"
        text = summ.get("detail", "") + (f" · derefter: {summ['next_action'][0].lower()}{summ['next_action'][1:]}"
                                         if summ.get("next_action") else "")
        self.ha.set_state("sensor.energy_planner_mode", snap["mode"], {
            **common, "friendly_name": "Energy Planner mode", "icon": "mdi:home-battery", **sp,
            **summ, "soc_pct": snap["soc_pct"], "title": title, "text": text,
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

    # ---- car control (fast loop) ---------------------------------------------------
    def car_cycle(self, now: datetime = None) -> dict:
        """The car controller's decision; carried out when the car production helper is on (off =
        manual control, nothing is called). Publishes and logs."""
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
            "car_soc": None,
            "pv_w": num("pv_power_entity"), "load_w": num("house_load_entity"),
            "charger_w": num("charger_power_entity"), "house_soc": num("battery_soc_entity"),
            "bat_charge_w": num("battery_charge_power_entity"),
            "amps": self.ha.number(opts["charger_current_entity"]),
            "secs_since_status": secs, "sell": self.sell_price_now(opts),
            "phase_amps": self._phase_amps(opts),
        }
        soc_est = self.car_soc_now(opts, now)
        live["car_soc"] = soc_est["soc"]
        live["car_soc_kia"] = soc_est["kia_soc"]
        with self._lock:
            snap = self.latest
        row = carmod.plan_for_now(snap, now, self.tz)
        # The plan may lift the daily limit (100 % at rock-bottom prices, or a trip).
        snap_car = (snap or {}).get("car") or {}
        opts = {**opts, "car_max_soc_pct": snap_car.get("max_soc_pct") or opts["car_max_soc_pct"]}
        if live.get("car_soc_kia") is not None and live["car_soc_kia"] >= 99:   # the car's own report only
            self._car_full_seen = now.isoformat(timespec="seconds")
            pvd.save_json(self.car_full_path, {"at": self._car_full_seen})
        ahead = []
        if row:
            row_t = datetime.fromisoformat(row["t"])
            ahead = [r for r in (snap or {}).get("plan", []) if "car_grid" in r and datetime.fromisoformat(r["t"]) > row_t]
        d = carmod.decide(live, row, opts, ahead=ahead)
        d["soc_estimate"] = soc_est
        car_production = writer.switch_on(self.ha, opts.get("car_production_switch_entity"))
        d.update({"updated": now.astimezone(self.tz).isoformat(timespec="seconds"), "production": car_production,
                  "live": live, "plan_row": row, "executed": [], "warning": None})
        if car_production and not self._car_prod_prev:
            # switching the car helper on (again) clears a Kia block and an earlier charger stop
            self._kia_block_until = self._kia_next_try = self._stop_ok_at = None
            self._kia_fails, self._thor_stopped = 0, False
        self._car_prod_prev = car_production
        status, prev = live.get("status"), self._car_status_prev
        self._car_status_prev = status
        if (prev in ("charging", "suspended_ev", "suspended_evse") and status in ("finishing", "idle", "available")
                and not self._thor_stopped and self._car_planned_ahead(snap, now) >= 0.5):
            self._session_ended_at = now
            log.warning("car: the charger ended the session on its own (%s -> %s) while the plan has more for the "
                        "car; nothing was sent from here", prev, status)
        elif status in ("preparing", "charging", "suspended_ev", "suspended_evse"):
            self._session_ended_at = None
        if self._session_ended_at and now - self._session_ended_at > timedelta(hours=12):
            self._session_ended_at = None
        if status not in carmod.CONNECTED:
            self._thor_stopped = False            # cable out: the next plug-in starts fresh
        if car_production:
            d["executed"], d["warning"] = self._car_production_step(d, opts, live, snap, now)
        extra_warn = []
        if self._session_ended_at:
            extra_warn.append(f"Laderen afsluttede sessionen selv kl. {self._session_ended_at.astimezone(self.tz):%H:%M}, "
                              "mens planen havde mere til bilen (intet blev sendt herfra). Sidder kablet stadig i, så "
                              "tag det ud og ind igen.")
        if car_production:
            sw = self._charger_schedule_warning(opts)
            if sw:
                extra_warn.append(sw)
        if extra_warn:
            d["warning"] = " ".join([w for w in [d.get("warning")] + extra_warn if w])
        with self._lock:
            self.car_latest = d
        d["reason_da"] = reason_da(d["reason"])
        car_items = [it for it in (snap or {}).get("upcoming", []) if it["kind"] == "car"]
        car_info = (snap or {}).get("car") or {}
        nxt = car_items[0] if car_items else None
        d["next_window"] = f"{nxt['when']} · {nxt['text']}" if nxt else None
        self.ha.set_state("sensor.energy_planner_car", d["action"], {
            "friendly_name": "Energy Planner car", "icon": "mdi:car-electric", "production": d["production"], "updated": d["updated"],
            "label": CAR_ACTION_LABEL.get(d["action"], d["action"]),
            "target_amps": d["target_amps"], "reason": d["reason"], "reason_da": d["reason_da"], "calls": d["calls"],
            "executed": d["executed"], "warning": d.get("warning"),
            "surplus_w": d.get("surplus_w"),
            "next_window": f"{nxt['when']} · {nxt['text']}" if nxt else None,
            "next_start": nxt["start"] if nxt else None,
            "planned_kwh": car_info.get("planned_kwh"),
            "deadline": car_info.get("deadline"),
            "max_soc_pct": opts["car_max_soc_pct"],
            "last_full": self._last_full().isoformat() if self._last_full() else None,
            "days_since_full": (car_info.get("full_charge") or {}).get("days_since"),
            "full_charge": (car_info.get("full_charge") or {}).get("status"),
            "departure": (car_info.get("departure") or {}).get("at"),
            "departure_soc": (car_info.get("departure") or {}).get("soc"),
            "departure_ok": car_info.get("departure_ok"),
            "soc_estimated": soc_est["soc"] if soc_est["estimated"] else None,
            "soc_kia": soc_est["kia_soc"], "soc_kia_at": soc_est["kia_at"],
            "preview_kwh": ((snap or {}).get("car_preview") or {}).get("planned_kwh"),
            "preview_window": (lambda w: f"{w[0]['start'][11:16]}–{w[0]['end'][11:16]}" if w else None)(
                ((snap or {}).get("car_preview") or {}).get("windows")),
            "preview_text": ((snap or {}).get("car_preview") or {}).get("text"),
            "extra": ({k: car_info["extra"].get(k) for k in ("label", "target_pct", "until", "planned_pct")}
                      if (car_info.get("extra") or {}).get("set") else None)})
        st, attrs = car_advice(car_info, (snap or {}).get("car_preview"), d, now.astimezone(self.tz))
        self.ha.set_state("sensor.energy_planner_car_advice", st, {"friendly_name": "Energy Planner bil-råd", **attrs,
                                                                 "updated": d["updated"]})
        self._publish_warnings()
        key = (d["action"], d["target_amps"], d["reason"].split(" (")[0], live["status"], live["amps"])
        if key != self._car_last_logged or d["executed"]:   # log changes and every executed call
            self._car_last_logged = key
            self._append(self.car_log_path, {k: d[k] for k in ("updated", "action", "target_amps", "reason",
                                                                "reason_da", "calls", "executed", "warning",
                                                                "production", "live")})
        return d

    def _car_production_step(self, d: dict, opts: dict, live: dict, snap: dict, now: datetime):
        """Carry out the decision's calls, and when the car cannot be paused: 6 A, and as a last
        resort (grid charging, nothing more planned) the stop button on the charger."""
        status = live.get("status")
        charging = status == "charging"
        wants_pause = d["action"] == "pause"
        if self._thor_stopped:
            return [], "Stoppet på laderen (nødstop) – sæt kablet i igen for at lade."
        executed = self._execute_car_calls(d["calls"], opts, status, now)
        if not (charging and wants_pause):
            if not charging:
                self._stop_ok_at = None
            self._pause_wanted_since = None
            return executed, self._kia_warning(now)
        if self._pause_wanted_since is None:
            self._pause_wanted_since = now

        stop = next((x for x in executed if x["service"] == "kia_uvo.stop_charge"), None)
        if stop and stop["status"] in ("ok", "unknown") and self._stop_ok_at is None:
            self._stop_ok_at = now
        if stop and stop.get("paused"):
            return executed, None                      # the car paused while the call was running
        ignored = self._stop_ok_at is not None and now - self._stop_ok_at >= KIA_STOP_IGNORED
        blocked = self._kia_block_until is not None and now < self._kia_block_until
        no_device = not opts.get("car_device_id") and not opts.get("car_charge_switch_entity")
        failing = blocked or self._kia_fails > 0 or ignored or no_device
        if not failing:
            return executed, None

        warning = (self._kia_warning(now)
                   or ("car_device_id er ikke sat i add-on'ens indstillinger, så bilen kan ikke pauses via Kia."
                       if no_device else "Bilen lader stadig, selv om styringen har bedt den stoppe."))
        amin = int(opts["car_min_amps"])
        if live.get("amps") != amin:
            executed += self._execute_car_calls([{"service": "number.set_value", "entity_id": opts["charger_current_entity"],
                                                  "value": amin}], opts, status, now)
        # Emergency stop on the charger: it cannot be restarted from here, so only when charging from
        # the grid, the plan has nothing more for the car, Kia has given up, the pause has been wanted
        # for a while, and the charger still says "charging" when read again right now.
        nothing_more = self._car_planned_ahead(snap, now) < 0.5
        kia_gave_up = blocked or self._kia_fails >= 2 or ignored or no_device
        long_enough = now - self._pause_wanted_since >= THOR_STOP_AFTER
        if (opts.get("car_thor_stop_fallback") and opts.get("charger_stop_button") and d.get("source") != "solar"
                and nothing_more and kia_gave_up and long_enough):
            fresh = self.ha.text(opts["charger_status_entity"])
            if fresh != "charging":
                log.info("car: no emergency stop, the charger now says %s", fresh)
                self._kia_fails, self._kia_next_try = 0, None
                return executed, None
            res = self._execute_car_calls([{"service": "button.press", "entity_id": opts["charger_stop_button"]}],
                                          opts, status, now)
            executed += res
            if res and res[0]["status"] == "ok":
                self._thor_stopped = True
                warning += " Nødstop på laderen brugt – sæt kablet i igen for at lade."
                mins = (now - self._pause_wanted_since).total_seconds() / 60
                log.warning("car: emergency stop on the charger: still charging after %.0f min of wanting a pause "
                            "(Kia: %s)", mins, self._kia_last_error or ("stop ignored" if ignored else "no device id"))
        else:
            warning += f" Laderen er sat til {amin} A, til bilen kan pauses."
        return executed, warning

    def _kia_warning(self, now: datetime):
        if self._kia_block_until is not None and now < self._kia_block_until:
            return (f"Kia afviste kaldet: {str(self._kia_last_error).rstrip('. ')}. Ingen nye Kia-kald før kl. "
                    f"{self._kia_block_until.astimezone(self.tz):%H:%M} (eller slå bil-produktion fra og til). "
                    "Tjek PIN-koden i Kia-integrationen.")
        if self._kia_fails:
            nxt = self._kia_next_try.astimezone(self.tz).strftime("%H:%M") if self._kia_next_try else "snart"
            return f"Kia-kald fejlede {self._kia_fails} gange ({str(self._kia_last_error).rstrip('. ')}). Prøver igen kl. {nxt}."
        return None

    @staticmethod
    def _car_planned_ahead(snap: dict, now: datetime) -> float:
        """kWh the plan still has for the car in the hours after the current one."""
        total = 0.0
        for r in (snap or {}).get("plan", []):
            try:
                start = datetime.fromisoformat(r["t"])
            except (KeyError, ValueError):
                continue
            if start > now and "car_grid" in r:
                total += (r.get("car_grid") or 0.0) + (r.get("car_pv") or 0.0) + (r.get("car_bat") or 0.0)
        return total

    def _execute_car_calls(self, calls: list, opts: dict, status: str, now: datetime) -> list:
        """Carry out calls (car production helper on). Kia calls go over the WebSocket API so a
        failure carries HA's message; PIN / access errors stop Kia calls for 6 h, other failures
        back off 2 / 4 / 8 / 15 min."""
        if hasattr(self.ha, "set_allowed_entities"):
            self.ha.set_allowed_entities(writer.allowed_entities(opts))
        done = []
        for c in calls:
            svc = c["service"]
            res = {"service": svc, "value": c.get("value")}
            try:
                if svc == "number.set_value":
                    self.ha.call_service("number", "set_value", {"entity_id": c["entity_id"], "value": c["value"]})
                elif svc == "button.press":
                    self.ha.call_service("button", "press", {"entity_id": c["entity_id"]})
                elif svc.startswith("kia_uvo."):
                    device = opts.get("car_device_id")
                    # The pause goes through the car's charging switch: kia_uvo.stop_charge makes the
                    # IONIQ end the session (the THOR registers an unplug 3 min later and cannot
                    # restart), the switch only pauses it.
                    switch = opts.get("car_charge_switch_entity") if svc == "kia_uvo.stop_charge" else None
                    if switch:
                        res["via"] = switch
                    if not device and not switch:
                        res.update(status="skipped", error="car_device_id is not set")
                        done.append(res)
                        continue
                    if self._kia_block_until is not None and now < self._kia_block_until:
                        res.update(status="skipped", error=f"Kia blocked after: {self._kia_last_error}")
                        done.append(res)
                        continue
                    if self._kia_next_try is not None and now < self._kia_next_try:
                        res.update(status="skipped", error="waiting after a failed Kia call")
                        done.append(res)
                        continue
                    last = self._last_kia
                    if last and last[0] == svc and last[1] == status and (now - last[2]).total_seconds() < KIA_REPEAT_S:
                        res.update(status="skipped", error="repeated within 10 min (Kia cloud quota)")
                        done.append(res)
                        continue
                    call = getattr(self.ha, "call_service_ws", None) or self.ha.call_service
                    if svc == "kia_uvo.stop_charge":
                        self._car_stop_sent = (now.astimezone(UTC), switch or "kia_uvo.stop_charge")
                    try:
                        if switch:
                            call("switch", "turn_off", {"entity_id": switch})
                        else:
                            call("kia_uvo", svc.split(".", 1)[1], {"device_id": device})
                    except Exception as e:
                        # The car may have followed even though the call failed or timed out: ask the
                        # charger before counting it as a failure.
                        fresh = self.ha.text(opts["charger_status_entity"]) if opts.get("charger_status_entity") else None
                        followed = ((svc == "kia_uvo.stop_charge" and status == "charging" and fresh in carmod.SUSPENDED)
                                    or (svc == "kia_uvo.start_charge" and status != "charging" and fresh == "charging"))
                        if followed or isinstance(e, TimeoutError):
                            self._last_kia = (svc, status, now)
                            res.update(status="ok" if followed else "unknown", error=str(e) or "no reply in time",
                                       paused=followed and svc == "kia_uvo.stop_charge")
                            log.info("car: %s returned %s, the charger now says %s", svc, e or "a timeout", fresh)
                            done.append(res)
                            continue
                        self._kia_failed(str(e), now)
                        raise
                    self._last_kia = (svc, status, now)
                    self._kia_fails, self._kia_next_try, self._kia_last_error = 0, None, None
                    self._kia_block_until = None
                else:
                    res.update(status="skipped", error="unknown call")
                    done.append(res)
                    continue
                res["status"] = "ok"
                log.info("car: %s %s", svc, c.get("value", ""))
            except Exception as e:
                res.update(status="error", error=str(e))
                log.warning("car call %s failed: %s", svc, e)
            done.append(res)
        return done

    def _kia_failed(self, message: str, now: datetime):
        self._kia_last_error = message
        if any(k in message.lower() for k in NON_RETRYABLE):
            self._kia_block_until = now + KIA_BLOCK
            log.warning("Kia rejected the call (%s): no Kia calls until %s", message, self._kia_block_until)
        self._kia_fails += 1
        self._kia_next_try = now + timedelta(minutes=KIA_BACKOFF_MIN[min(self._kia_fails, len(KIA_BACKOFF_MIN)) - 1])

    # ---- solar dampening ---------------------------------------------------------
    def _solcast_applied(self, sol_attrs_entities: list) -> list:
        """The dampening Solcast's values *as read now* carry: the pushed factors once Solcast has
        updated its sensors after the push, the previous ones before that."""
        st = self._pv_state
        if not st.get("pushed_at"):
            return st["applied"]
        updated = None
        for e in sol_attrs_entities[:1]:
            s = self.ha.state(e) if hasattr(self.ha, "state") else None
            updated = (s or {}).get("last_updated")
        if updated and datetime.fromisoformat(updated.replace("Z", "+00:00")) >= datetime.fromisoformat(st["pushed_at"]):
            return st["applied"]
        return st.get("previous") or st["applied"]

    def _pv_dampening(self, opts, now, sol_attrs, pv50, pv10, pv90):
        mode = opts.get("pv_dampening", "auto")
        push = bool(opts.get("solcast_push")) and mode != "off"
        entities = split_csv(opts["solcast_entities"])
        w = min(max(float(opts["pv_p10_weight"]), 0.0), 1.0)
        info = {"mode": mode, "push": push, "solcast_auto": False, "days": int(opts["pv_dampening_days"])}

        if pvd.solcast_auto_dampening(sol_attrs):
            # Solcast's own automated dampening is on: its values are already corrected (and it
            # refuses set_dampening). Use them as they are and learn nothing.
            info["solcast_auto"] = True
            blended = {h: (1 - w) * pv50[h] + w * pv10.get(h, pv50[h]) for h in pv50}
            ones = [1.0] * 24
            self._record_dayahead(now, pv50, pv10, pv90, ones, blended)
            self._solar_forecast = self._forecast_rows(pv50, pv10, pv90, ones, pv50, pv10, pv90, blended, {})
            return blended, pv50, pv10, pv90, {}, info

        if not self._solcast_synced:
            self._solcast_synced = True
            try:
                factors = pvd.parse_get_dampening(self.ha.call_service("solcast_solar", "get_dampening",
                                                                       {}, return_response=True))
            except Exception as e:  # no dampening set, old integration or service missing
                log.info("solcast get_dampening: %s", e)
                factors = None
            st = self._pv_state
            current = factors or [1.0] * 24
            if any(abs(a - b) > 0.011 for a, b in zip(current, st["applied"])):
                log.info("Solcast dampening differs from the stored one; using Solcast's: %s", pvd.csv(current))
                st.update(applied=current, previous=current, pushed_at=None)
                pvd.save_json(self.pv_state_path, st)

        self._backfill_history(now)
        applied = self._solcast_applied(entities)
        h0 = hour_floor(now)
        if h0 in pv50:
            limit = self.ha.number(opts["reg_export_limit"]) if opts.get("reg_export_limit") else None
            hl = h0.astimezone(self.tz).hour
            pvd.record(self._pv_history, h0, pv50[h0], pv10.get(h0, pv50[h0]), pv90.get(h0, pv50[h0]),
                       applied[hl], limit is not None and limit < 50, now)
            pvd.save_json(self.pv_history_path, self._pv_history)

        ref = pvd.clear_reference(self._pv_history, pv90, applied, self.tz, now)
        half = float(opts.get("pv_dampening_half_life_days") or 0) or None
        info["half_life_days"] = half
        model = pvd.fit(self._pv_history, self._pv_actual, ref, self.tz, now, info["days"], float(opts["inverter_kw"]),
                        half_life_days=half)
        manual = pvd.parse_manual(opts.get("pv_dampening_manual", ""))
        shade = pvd.effective_shade(model["shade"], manual, mode)
        c50, c10, c90, fac = pvd.correct(pv50, pv10, pv90, applied, shade, ref, self.tz)
        blended = {h: (1 - w) * c50[h] + w * c10.get(h, c50[h]) for h in c50}
        self._pv_shade, self._pv_ref = shade, ref
        self._record_dayahead(now, pv50, pv10, pv90, applied, blended)
        self._solar_forecast = self._forecast_rows(pv50, pv10, pv90, applied, c50, c10, c90, blended, fac)

        today = now.astimezone(self.tz).date()
        desired = pvd.static_factors(shade, {t: v for t, v in pv50.items() if t.astimezone(self.tz).date() == today},
                                     applied, ref, self.tz)
        st = self._pv_state
        if push and pvd.push_due(desired, st["applied"], st.get("pushed_at"), now, self.tz):
            try:
                self.ha.call_service("solcast_solar", "set_dampening", {"damp_factor": pvd.csv(desired)})
                st.update(previous=st["applied"], applied=desired, pushed_at=now.isoformat(timespec="seconds"))
                pvd.save_json(self.pv_state_path, st)
                log.info("Solcast dampening set: %s", pvd.csv(desired))
            except Exception as e:
                log.warning("solcast set_dampening failed: %s", e)
                info["push_error"] = str(e)

        info.update({
            "clear_factors": [round(1.0 - s, 2) for s in shade],
            "learned_factors": [round(1.0 - s, 2) for s in model["shade"]],
            "today_factors": desired,
            "manual_hours": sorted(manual),
            "applied_in_solcast": st["applied"],
            "last_pushed": st.get("pushed_at"),
            "hours": model["hours"], "weight": model["weight"], "forecast": model["forecast"], "actual": model["actual"],
            "corrected": model["corrected"],
            "history_hours": len(self._pv_history),
            "mean_factor": pvd.mean_daylight(desired, ref),
            "backfill": self._backfill,
        })
        return blended, c50, c10, c90, fac, info

    def _record_dayahead(self, now, pv50, pv10, pv90, applied, plan):
        """From 20:00 local: keep tomorrow's forecast as it was the evening before (first read wins)."""
        local = now.astimezone(self.tz)
        if local.hour < 20:
            return
        tomorrow = local.date() + timedelta(days=1)
        changed = False
        for t, v in pv50.items():
            if t.astimezone(self.tz).date() != tomorrow:
                continue
            key = t.astimezone(UTC).isoformat()
            if key in self._pv_dayahead:
                continue
            a = max(applied[t.astimezone(self.tz).hour], pvd.MIN_FACTOR)
            self._pv_dayahead[key] = {"p50": round(v / a, 4), "p10": round(pv10.get(t, v) / a, 4),
                                      "p90": round(pv90.get(t, v) / a, 4), "plan": round(plan.get(t, v), 4),
                                      "at": now.isoformat(timespec="seconds")}
            changed = True
        if changed:
            cutoff = (now - timedelta(days=pvd.HISTORY_DAYS)).astimezone(UTC).isoformat()
            for k in [k for k in self._pv_dayahead if k < cutoff]:
                del self._pv_dayahead[k]
            pvd.save_json(self.pv_dayahead_path, self._pv_dayahead)

    def _forecast_rows(self, pv50, pv10, pv90, applied, c50, c10, c90, plan, fac):
        """The coming days hour by hour: Solcast undampened p10/p50/p90 and the corrected values."""
        rows = []
        for t in sorted(pv50):
            a = max(applied[t.astimezone(self.tz).hour], pvd.MIN_FACTOR)
            rows.append({"t": t.astimezone(self.tz).isoformat(), "p50": round(pv50[t] / a, 3),
                         "p10": round(pv10.get(t, pv50[t]) / a, 3), "p90": round(pv90.get(t, pv50[t]) / a, 3),
                         "c50": round(c50.get(t, 0.0), 3), "c10": round(c10.get(t, 0.0), 3),
                         "c90": round(c90.get(t, 0.0), 3), "plan": round(plan.get(t, 0.0), 3),
                         "factor": fac.get(t, 1.0)})
        return rows

    def solar_report(self, day, now: datetime = None) -> dict:
        """One local day hour by hour: actual, Solcast (undampened p10/p50/p90 as recorded), the same
        with today's correction, and the forecast from the evening before. Actual from statistics."""
        opts = self.options_fn()
        now = (now or datetime.now(UTC)).astimezone(UTC)
        start = datetime(day.year, day.month, day.day, tzinfo=self.tz)
        end = start + timedelta(days=1)
        actual = {}
        if opts.get("pv_power_entity") and start.astimezone(UTC) < now:
            stats = self.ha.statistics([opts["pv_power_entity"]], start.astimezone(UTC),
                                       min(end.astimezone(UTC), now), "hour")
            actual = _by_start(stats.get(opts["pv_power_entity"]))
        rows = self._solar_rows(start, end, actual, now)
        return {"day": day.isoformat(), "hours": rows, "totals": _solar_totals(rows),
                "today": now.astimezone(self.tz).date().isoformat()}

    def solar_days(self, days: int, now: datetime = None) -> list:
        """Daily totals for the last `days` days (today first)."""
        opts = self.options_fn()
        now = (now or datetime.now(UTC)).astimezone(UTC)
        today = now.astimezone(self.tz).date()
        first = today - timedelta(days=days - 1)
        actual = {}
        if opts.get("pv_power_entity"):
            begin = datetime(first.year, first.month, first.day, tzinfo=self.tz).astimezone(UTC)
            stats = self.ha.statistics([opts["pv_power_entity"]], begin, now, "hour")
            actual = _by_start(stats.get(opts["pv_power_entity"]))
        out = []
        for i in range(days):
            d = today - timedelta(days=i)
            start = datetime(d.year, d.month, d.day, tzinfo=self.tz)
            rows = self._solar_rows(start, start + timedelta(days=1), actual, now)
            out.append({"day": d.isoformat(), **_solar_totals(rows)})
        return out

    def _solar_rows(self, start, end, actual: dict, now) -> list:
        idx = pvd._reference_index(self._pv_history, self.tz)
        rows = []
        t, stop = start.astimezone(UTC), end.astimezone(UTC)
        while t < stop:
            key = t.isoformat()
            h = t.astimezone(self.tz).hour
            e = self._pv_history.get(key)
            row = {"t": t.astimezone(self.tz).isoformat()}
            if e:
                u50, u10, u90 = (pvd._undamped(e, k) for k in ("p50", "p10", "p90"))
                kc = pvd.clearness(u50, pvd.reference_at(idx, h, t, self._pv_ref[h]))
                f = pvd.factor(self._pv_shade[h], kc)
                row.update(p50=round(u50, 3), p10=round(u10, 3), p90=round(u90, 3), c50=round(u50 * f, 3),
                           factor=round(f, 3), limited=bool(e.get("limited")))
            da = self._pv_dayahead.get(key)
            if da:
                row["dayahead"] = da["plan"]
            a = actual.get(t)
            if a is not None and t + timedelta(hours=1) <= now:
                row["actual"] = round(max(0.0, a) / 1000.0, 3)
            rows.append(row)
            t += timedelta(hours=1)
        return rows

    def _backfill_history(self, now: datetime):
        """Fetch Solcast's own (undampened) forecast history once after start, so the correction can
        learn right away instead of after two weeks of recording. Retried after 6 h on failure."""
        if self._backfill_at and (self._backfill and not self._backfill.get("error")
                                  or now - self._backfill_at < BACKFILL_RETRY):
            return
        self._backfill_at = now
        end = hour_floor(now)
        start = end - timedelta(days=BACKFILL_DAYS)
        try:
            resp = self.ha.call_service("solcast_solar", "query_forecast_data", {
                "start_date_time": start.isoformat(), "end_date_time": end.isoformat(), "undampened": True,
            }, return_response=True)
            hourly = pvd.parse_forecast_history(resp)
            if not hourly:
                raise ValueError("Solcast returned no forecast history (unexpected response format?)")
            added = pvd.backfill(self._pv_history, hourly, now, self._export_limited_hours())
            pvd.save_json(self.pv_history_path, self._pv_history)
            self._backfill = {"hours": added, "received": len(hourly), "at": now.isoformat(timespec="seconds")}
            log.info("Solcast forecast history: %s hours received, %s added", len(hourly), added)
        except Exception as e:  # old integration, unknown format, service missing: plan on as before
            self._backfill = {"hours": 0, "at": now.isoformat(timespec="seconds"), "error": str(e)}
            log.warning("could not fetch Solcast forecast history: %s", e)

    def _export_limited_hours(self) -> set:
        """UTC hours in which the decision log saw the export limit at 0 (panels throttled)."""
        hours = set()
        for e in self.recent_decisions(LOG_READ):
            try:
                limit = float((e.get("actual_registers") or {}).get("reg_export_limit"))
                t = datetime.fromisoformat(e["updated"]).astimezone(UTC)
            except (TypeError, ValueError, KeyError):
                continue
            if limit < 50:
                hours.add(t.replace(minute=0, second=0, microsecond=0))
        return hours

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
            **self._solar_today(now, t["cost_dkk"] if t else 0.0),
            "updated": now.astimezone(self.tz).isoformat(timespec="seconds")})

    def _today_economy(self, opts: dict, now: datetime):
        """Today's economy totals from the counters (economy.report, as the Økonomi tab), cached 10 min."""
        today = now.astimezone(self.tz).date()
        cached = getattr(self, "_today_econ_cache", None)
        if not cached or cached[0] != today or now - cached[1] > timedelta(minutes=10):
            try:
                tot = economy.report(self.ha, opts, self.tz, 1, now, self.recent_decisions(400))["totals"]
            except Exception as e:  # statistics unavailable: leave it out
                log.info("today's economy unavailable: %s", e)
                tot = None
            self._today_econ_cache = (today, now, tot)
        return self._today_econ_cache[2]

    def _solar_forecast_today(self, now: datetime):
        today = now.astimezone(self.tz).date().isoformat()
        fc = [r for r in (self.latest or {}).get("solar_forecast") or [] if r["t"][:10] == today]
        return round(sum(r["c50"] for r in fc), 1) if fc else None

    def _solar_text(self, solar, forecast) -> str:
        if forecast is None:
            return f"sol {num_da(solar, 1)} kWh" if solar is not None else ""
        return (f"sol {num_da(solar, 1)} af {num_da(forecast, 1)} kWh" if solar is not None
                else f"sol {num_da(forecast, 1)} kWh forventet")

    def _solar_today(self, now: datetime, cost: float) -> dict:
        """Solar forecast for today (corrected) and produced so far (the solar counter)."""
        tot = self._today_economy(self.options_fn(), now)
        solar = round(tot["solar_kwh"], 1) if tot else None
        forecast = self._solar_forecast_today(now)
        st = self._solar_text(solar, forecast)
        return {"solar_forecast_kwh": forecast, "solar_kwh": solar,
                "text": f"{num_da(cost, 0)} kr indtil nu" + (f" · {st}" if st else "")}

    def _publish_savings(self, opts: dict, now: datetime):
        """sensor.energy_planner_savings_today: the installation's value today ("Anlægsværdi" in the
        Økonomi tab: consumption at the buy price minus the net cost, the same economy.report), so far
        (the counters, whole hours) and expected for the whole day (plus the plan's hours from the current
        one to midnight). The text splits it into the sun's and the battery's part: sun stored instead of
        sold is a lost sale in the battery's part, matched by the sun's part until it is used."""
        snap = self.latest
        tot = self._today_economy(opts, now)
        if not snap or tot is None:
            return
        local = now.astimezone(self.tz)
        today = local.date().isoformat()
        rows = [r for r in snap["plan"] if r["t"][:10] == today]
        rest = 0.0
        for i, r in enumerate(rows):
            car = (r.get("car_pv") or 0) + (r.get("car_grid") or 0) + (r.get("car_bat") or 0)
            without = (r["load"] + car) * r["buy"]
            net = (r["grid_load"] + r["grid_bat"] + (r.get("car_grid") or 0)) * r["buy"] - r["pv_exp"] * r["sell"]
            v = without - net
            if i == 0 and r["t"][:13] == snap["plan"][0]["t"][:13] and 0 < (r.get("dt") or 1) < 1:
                v /= r["dt"]     # the counters stop at the last whole hour: the current hour counts in full
            rest += v
        value = round(tot["value_dkk"], 2)
        expected = round(value + rest, 2)
        solar = round(tot["solar_kwh"], 1)
        st = self._solar_text(solar, self._solar_forecast_today(now))

        def signed(v):
            return ("+" if v >= 0.5 else "−" if v <= -0.5 else "") + f"{num_da(abs(v), 0)} kr"
        parts = [f"sol {signed(tot['solar_value_dkk'])}", f"batteri {signed(tot['battery_value_dkk'])}",
                 f"forventet {signed(expected)} i alt"] + ([st] if st else [])
        self.ha.set_state("sensor.energy_planner_savings_today", value, {
            "friendly_name": "Energy Planner anlæggets værdi i dag", "icon": "mdi:piggy-bank",
            "unit_of_measurement": "DKK",
            "expected_dkk": expected, "rest_dkk": round(rest, 2), "net_dkk": round(tot["net_dkk"], 2),
            "solar_value_dkk": round(tot["solar_value_dkk"], 2), "battery_value_dkk": round(tot["battery_value_dkk"], 2),
            "system_value_dkk": value,
            "solar_kwh": solar, "solar_forecast_kwh": self._solar_forecast_today(now),
            "title": f"Anlægget: {signed(value)} i dag",
            "text": " · ".join(parts),
            "updated": local.isoformat(timespec="seconds")})

    def _publish_front(self, snap: dict, now: datetime):
        """Front-page sensors: the price now and the cheapest hours, and the warnings."""
        local = now.astimezone(self.tz)
        rows = snap["plan"]
        if rows:
            attrs = price_attributes(rows, heatpump.price_levels(rows, local), local)
            self.ha.set_state("sensor.energy_planner_price", rows[0]["buy"], {
                "friendly_name": "Energy Planner elpris", "unit_of_measurement": "DKK/kWh", **attrs,
                "updated": snap["updated"]})
        self._publish_warnings()

    def front_warnings(self) -> list:
        """Danish messages for the front page: only things that need attention."""
        opts = self.options_fn()
        out = []
        with self._lock:
            snap, car = self.latest, self.car_latest
        if self.last_error:
            out.append(f"Planlægningen fejlede: {self.last_error}")
        if car and car.get("warning"):
            out.append(car["warning"])
        off = [name for key, name in (("production_switch_entity", "husbatteriet"),
                                      ("car_production_switch_entity", "bilen"),
                                      ("heat_pump_production_switch_entity", "varmepumpen"))
               if opts.get(key) and not writer.switch_on(self.ha, opts.get(key))
               and not (key == "heat_pump_production_switch_entity" and not opts.get("heat_pump_entity"))]
        if off:
            out.append("Manuel styring af " + " og ".join(off) + ": add-on'en skriver intet.")
        bad = [e for e, v in (self.entity_check or {}).items() if v != "ok"]
        if bad:
            out.append(f"{len(bad)} entitet{'er' if len(bad) > 1 else ''} mangler eller er utilgængelig: " + ", ".join(bad[:4])
                       + (" …" if len(bad) > 4 else ""))
        sched = self._charger_schedule_warning(opts)
        if sched and not any(sched in m for m in out):
            out.append(sched)
        return out

    def _publish_warnings(self):
        msgs = self.front_warnings()
        self.ha.set_state("sensor.energy_planner_warnings", len(msgs), {
            "friendly_name": "Energy Planner advarsler", "icon": "mdi:alert" if msgs else "mdi:check-circle",
            "messages": msgs, "text": msgs[0] if msgs else "Ingen advarsler"})

    def _publish_pv_dampening(self, snap: dict):
        d = snap.get("pv_dampening")
        if not d:
            return
        attrs = {"friendly_name": "Energy Planner solkorrektion", "icon": "mdi:weather-sunny-alert",
                 "updated": snap["updated"], "mode": d["mode"], "solcast_auto": d["solcast_auto"],
                 "push": d["push"]}
        if not d["solcast_auto"]:
            attrs.update({"factors": d["today_factors"], "factors_csv": pvd.csv(d["today_factors"]),
                          "clear_sky_factors": d["clear_factors"], "manual_hours": d["manual_hours"],
                          "applied_in_solcast": d["applied_in_solcast"], "last_pushed": d["last_pushed"],
                          "days": d["days"]})
        self.ha.set_state("sensor.energy_planner_pv_dampening", d.get("mean_factor", 1.0), attrs)

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

