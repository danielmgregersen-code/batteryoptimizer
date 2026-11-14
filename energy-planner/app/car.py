"""The car: planning parameters for the optimiser and the fast charge controller.

Planning (every planning cycle): while the car is connected it is a flexible load in the LP. It is
assumed to stay connected until the next deadline (06:00), must reach the minimum SoC (42 %) by
then if it is below the start threshold (40 %), must charge as fast as possible to the critical
stop (22 %) if it is below the critical start (20 %), and is charged beyond that only when the
energy costs less than `car_value_dkk_per_kwh` at the plug (sun that sells for less, or a cheap
grid hour). This replaces the fixed price rules of the HA automation
("Solcelle- & Prisstyret ladning: IONIQ 9").

Control (every `car_interval_seconds`): the same mechanics as the automation (amps 6-16 on the
THOR, start button from `preparing`, pause/resume through the car's cloud API with a minimum
charge time and a cooldown), but *what* to do comes from the plan for the current hour:
  plan says grid/battery power for the car  -> charge at the planned power (at least 6 A)
  plan says sun for the car, or the sun now sells for less than the car value
                                            -> follow the actual solar surplus (start/stop hysteresis)
  otherwise                                 -> pause
Hard rules on top: below the critical start always charge at 16 A; at the max SoC always stop.

In this version nothing is called: the controller publishes what it *would* do
(`sensor.energy_planner_car`) and logs it next to what the charger actually does.
"""
import math
from datetime import datetime, timedelta

from optimizer import CarParams

CONNECTED = ("preparing", "charging", "suspended_ev", "suspended_evse", "finishing")
SUSPENDED = ("suspended_ev", "suspended_evse", "finishing")
PLAN_KW_MIN = 0.3   # planned car power below this counts as "no car charging this hour"


def watt_per_amp(opts) -> float:
    return 230.0 * int(opts["car_phases"])


def car_max_kw(opts) -> float:
    return int(opts["car_max_amps"]) * watt_per_amp(opts) / 1000.0


def next_deadline(now_utc: datetime, tz, hour: int) -> datetime:
    local = now_utc.astimezone(tz)
    d = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    if d <= local:
        d = (local + timedelta(days=1)).replace(hour=hour, minute=0, second=0, microsecond=0)
    return d


def needs(soc: float, charging: bool, opts) -> dict:
    """The automation's hysteresis: start below X, keep going (while charging) until Y."""
    crit = soc < opts["car_critical_start_pct"] or (charging and soc < opts["car_critical_stop_pct"])
    minimum = soc < opts["car_min_start_pct"] or (charging and soc < opts["car_min_stop_pct"])
    return {"critical": bool(crit), "minimum": bool(minimum)}


def build_car_params(horizon, now_utc: datetime, tz, opts, status: str, soc_pct: float):
    """CarParams for the LP, or None when the car is not connected / unknown."""
    if status not in CONNECTED or soc_pct is None:
        return None, {"connected": False}
    cap = float(opts["car_battery_kwh"])
    eta = float(opts["car_eta"])
    max_kw = car_max_kw(opts)
    deadline = next_deadline(now_utc, tz, int(opts["car_deadline_hour"]))
    available = [s < deadline for s in horizon.starts]
    soc0 = cap * soc_pct / 100.0
    max_kwh = cap * float(opts["car_max_soc_pct"]) / 100.0
    need = needs(soc_pct, status == "charging", opts)

    targets = {}
    # Minimum by the deadline: at the end of the last step that ends at or before it.
    if need["minimum"]:
        steps = [i for i, s in enumerate(horizon.starts) if s + timedelta(hours=1) <= deadline]
        if steps:
            targets[steps[-1]] = min(cap * float(opts["car_min_stop_pct"]) / 100.0, max_kwh)
    # Critical: as fast as possible up to the critical stop.
    if need["critical"]:
        crit_kwh = min(cap * float(opts["car_critical_stop_pct"]) / 100.0, max_kwh)
        reach = soc0
        for i, dt in enumerate(horizon.dt):
            if not available[i] or reach >= crit_kwh:
                break
            reach = min(crit_kwh, reach + max_kw * dt * eta)
            targets[i] = max(targets.get(i, 0.0), reach)

    params = CarParams(available=available, max_kw=max_kw, eta=eta, soc0_kwh=soc0, max_kwh=max_kwh,
                       value_dkk_per_kwh=float(opts["car_value_dkk_per_kwh"]), targets=targets,
                       allow_battery=bool(opts.get("car_from_house_battery", False)))
    info = {"connected": True, "status": status, "soc_pct": soc_pct, "deadline": deadline.isoformat(),
            "need_critical": need["critical"], "need_minimum": need["minimum"]}
    return params, info


def plan_for_now(snapshot: dict, now_utc: datetime, tz):
    """The plan row for the current hour (or None if the plan is stale or has no car)."""
    if not snapshot:
        return None
    hour = now_utc.astimezone(tz).replace(minute=0, second=0, microsecond=0)
    for row in snapshot.get("plan", []):
        if datetime.fromisoformat(row["t"]) == hour and "car_grid" in row:
            return row
    return None


def decide(live: dict, plan_row, opts) -> dict:
    """One control step. live: status, car_soc, pv_w, load_w, charger_w, house_soc,
    bat_charge_w, amps, secs_since_status, sell. Returns the decision and the calls it implies."""
    status = live.get("status")
    out = {"action": "none", "target_amps": None, "reason": "", "calls": []}
    if status not in CONNECTED:
        out["reason"] = f"not connected ({status})"
        return out
    soc = live.get("car_soc")
    if soc is None:
        out["reason"] = "car SoC unknown"
        return out

    charging = status == "charging"
    need = needs(soc, charging, opts)
    wpa = watt_per_amp(opts)
    amin, amax = int(opts["car_min_amps"]), int(opts["car_max_amps"])

    def amps_for(kw):
        return int(min(max(math.ceil(kw * 1000.0 / wpa - 1e-9), amin), amax))

    # Solar surplus as the automation computes it: sun minus house load without the car, minus what
    # the house battery is taking while it is below its priority SoC.
    house_w = max(0.0, live.get("load_w", 0.0) - live.get("charger_w", 0.0))
    reserved = live.get("bat_charge_w", 0.0) if live.get("house_soc", 0.0) < opts["car_house_priority_soc"] else 0.0
    surplus = round(live.get("pv_w", 0.0) - house_w - reserved)
    stop_w = 0 if live.get("house_soc", 0.0) >= opts["car_buffer_soc"] else opts["car_stop_surplus_w"]
    has_surplus = (surplus >= opts["car_start_surplus_w"]) if not charging else (surplus >= stop_w)

    planned_forced_kw = planned_pv_kw = 0.0
    if plan_row:
        planned_forced_kw = float(plan_row["car_grid"]) + float(plan_row["car_bat"])
        planned_pv_kw = float(plan_row["car_pv"])
        dt = float(plan_row.get("dt", 1.0)) or 1.0
        planned_forced_kw /= dt
        planned_pv_kw /= dt
    sell = live.get("sell")
    sun_is_cheap = sell is not None and sell <= float(opts["car_value_dkk_per_kwh"])

    want, amps, reason = False, None, ""
    if soc >= opts["car_max_soc_pct"]:
        reason = f"car at max SoC ({soc:.0f} %)"
    elif need["critical"]:
        want, amps, reason = True, amax, "critical SoC"
    elif planned_forced_kw > PLAN_KW_MIN:
        want = True
        amps = amps_for(planned_forced_kw + planned_pv_kw)
        reason = f"plan: {planned_forced_kw + planned_pv_kw:.1f} kW this hour"
    elif (planned_pv_kw > PLAN_KW_MIN or sun_is_cheap) and has_surplus \
            and live.get("house_soc", 0.0) >= opts["car_house_min_soc"]:
        want = True
        amps = int(min(max(int(surplus / wpa), amin), amax))
        reason = f"solar surplus {surplus:.0f} W"
    elif plan_row is None and need["minimum"]:
        # No current plan (planner failing): fall back to charging the minimum at full power.
        want, amps, reason = True, amax, "no plan, below minimum SoC"
    else:
        reason = "plan: no car charging this hour" if plan_row else "no plan"

    secs = live.get("secs_since_status", 9999)
    if want:
        out.update(action="charge", target_amps=amps, reason=reason)
        if amps != live.get("amps"):
            out["calls"].append({"service": "number.set_value", "entity_id": opts["charger_current_entity"],
                                 "value": amps})
        if status == "preparing":
            out["calls"].append({"service": "button.press", "entity_id": opts["charger_start_button"]})
        elif status in SUSPENDED:
            may_start = (need["critical"] or need["minimum"] or status != "suspended_ev"
                         or secs >= opts["car_cooldown_s"])
            if may_start:
                out["calls"].append({"service": "kia_uvo.start_charge"})
            else:
                out["reason"] += f" (cooldown, {int(opts['car_cooldown_s'] - secs)} s left)"
    else:
        out.update(action="pause" if charging else "idle", reason=reason)
        if charging:
            may_stop = (soc >= opts["car_max_soc_pct"] or live.get("house_soc", 0.0) < opts["car_house_min_soc"]
                        or secs >= opts["car_min_charge_s"])
            if may_stop:
                out["calls"].append({"service": "kia_uvo.stop_charge"})
            else:
                out["action"] = "charge"
                out["reason"] += f" (minimum charge time, {int(opts['car_min_charge_s'] - secs)} s left)"
    out["surplus_w"] = surplus
    return out
