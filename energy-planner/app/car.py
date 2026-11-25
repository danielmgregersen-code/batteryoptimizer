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

Two exceptions lift the daily limit (car_max_soc_pct, 90 %): a charge to 100 % when power is at rock
bottom (full_charge_decision, never forced) and a trip (departure_info: the car at a given SoC at a
given time).

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


def last_full_from_stats(rows, threshold: float = 99.0):
    """Start of the latest hour in which the car SoC statistics reached `threshold` (max or mean)."""
    from inputs import parse_ts
    latest = None
    for r in rows or []:
        v = r.get("max") if r.get("max") is not None else r.get("mean")
        if v is None or float(v) < threshold:
            continue
        t = parse_ts(r["start"])
        latest = t if latest is None or t > latest else latest
    return latest


def percentile(values, pct: float):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    k = (len(vals) - 1) * pct / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def full_charge_decision(horizon, now_utc: datetime, tz, opts, soc_pct: float, last_full,
                         price_history) -> dict:
    """Charge to 100 % only when power is at rock bottom, and not more often than car_full_min_days.

    Rock bottom: a negative sell price in the hours up to the deadline (sun on a summer day), or the
    cheapest hours needed for 100 % average among the car_full_cheap_percentile % cheapest hourly
    prices of the last 30 days. Never forced, however long ago the last full charge was.
    """
    out = {"active": False, "status": "off", "last_full": last_full.isoformat() if last_full else None,
           "days_since": None, "threshold": None, "night_price": None}
    if not opts.get("car_full_charge"):
        return out
    if last_full is not None:
        out["days_since"] = (now_utc - last_full).days
    if soc_pct is not None and soc_pct >= 99:
        out["status"] = "full"
        return out
    min_days = int(opts["car_full_min_days"])
    if out["days_since"] is not None and out["days_since"] < min_days:
        out["status"] = "too_soon"
        out["possible_from"] = (last_full + timedelta(days=min_days)).astimezone(tz).date().isoformat()
        return out
    deadline = next_deadline(now_utc, tz, int(opts["car_deadline_hour"]))
    window = [i for i, s in enumerate(horizon.starts) if s + timedelta(hours=1) <= deadline]
    if not window or soc_pct is None:
        out["status"] = "waiting"
        return out
    cap, rate = float(opts["car_battery_kwh"]), car_max_kw(opts) * float(opts["car_eta"])
    hours = max(1, math.ceil((100.0 - soc_pct) / 100.0 * cap / rate - 1e-9))
    cheapest = sorted(horizon.buy[i] for i in window)[:hours]
    out["night_price"] = round(sum(cheapest) / len(cheapest), 3)
    out["threshold"] = percentile(price_history, float(opts["car_full_cheap_percentile"])) \
        if len(price_history or []) >= 24 * 7 else None
    negative_sell = any(horizon.sell[i] < 0 for i in window)
    cheap = out["threshold"] is not None and out["night_price"] <= out["threshold"]
    out["active"] = bool(negative_sell or cheap)
    out["status"] = "planned" if out["active"] else "waiting"
    out["why"] = "negative_sell" if negative_sell else ("cheap" if cheap else None)
    return out


def departure_info(departure, soc_target: float, horizon, now_utc: datetime) -> dict:
    """A trip: the car must be at soc_target when it leaves. Planned once the departure is inside
    the horizon; ignored once it has passed."""
    if departure is None:
        return {"set": False}
    out = {"set": True, "at": departure.isoformat(), "soc": soc_target, "step": None}
    if departure <= now_utc:
        out["status"] = "passed"
        return out
    steps = [i for i, s in enumerate(horizon.starts) if s + timedelta(hours=1) <= departure]
    end = horizon.starts[-1] + timedelta(hours=1) if horizon.starts else now_utc
    if departure > end or not steps:
        out["status"] = "beyond_horizon"
        return out
    out["status"] = "planned"
    out["step"] = steps[-1]
    return out


def extra_info(extra, horizon, now_utc: datetime) -> dict:
    """An extra charge ordered in the panel: reach target_pct by "until" (the next deadline or the
    trip). Planned at the last step that ends at or before "until"; "passed" once it is over."""
    if not extra or extra.get("target_pct") is None or not extra.get("until"):
        return {"set": False}
    out = {**extra, "set": True, "step": None}
    until = datetime.fromisoformat(extra["until"])
    if until <= now_utc:
        out["status"] = "passed"
        return out
    steps = [i for i, s in enumerate(horizon.starts) if s + timedelta(hours=1) <= until]
    if not steps:
        out["status"] = "passed" if not horizon.starts else "too_soon"
        return out
    out["status"] = "planned"
    out["step"] = steps[-1]
    return out


def build_car_params(horizon, now_utc: datetime, tz, opts, status: str, soc_pct: float,
                     full: dict = None, departure: dict = None, extra: dict = None):
    """CarParams for the LP, or None when the car is not connected / unknown.

    full: full_charge_decision(); departure: departure_info(); extra: an "extra charge" ordered in the
    panel ({"target_pct", "until"}). Each lifts the charge limit and adds a (soft) target: 100 % by the
    deadline, the trip's SoC by the departure, or the ordered SoC by its "until".
    """
    if status not in CONNECTED or soc_pct is None:
        return None, {"connected": False}
    cap = float(opts["car_battery_kwh"])
    eta = float(opts["car_eta"])
    max_kw = car_max_kw(opts)
    deadline = next_deadline(now_utc, tz, int(opts["car_deadline_hour"]))
    full = full or {}
    departure = departure or {}
    dep_active = departure.get("status") in ("planned", "beyond_horizon")
    # Until a coming trip, the car is assumed to stay connected (not only until 06:00).
    until = max(deadline, datetime.fromisoformat(departure["at"])) if dep_active else deadline
    ext = extra_info(extra, horizon, now_utc)
    if ext.get("status") == "planned":
        until = max(until, datetime.fromisoformat(ext["until"]))
    available = [s < until for s in horizon.starts]
    soc0 = cap * soc_pct / 100.0
    max_pct = float(opts["car_max_soc_pct"])
    if full.get("active"):
        max_pct = 100.0
    if departure.get("status") == "planned":
        max_pct = max(max_pct, float(departure["soc"]))
    if ext.get("status") == "planned":
        max_pct = max(max_pct, float(ext["target_pct"]))
    max_pct = min(max_pct, 100.0)
    max_kwh = cap * max_pct / 100.0
    need = needs(soc_pct, status == "charging", opts)

    targets = {}
    if full.get("active"):
        steps = [i for i, s in enumerate(horizon.starts) if s + timedelta(hours=1) <= deadline]
        if steps:
            targets[steps[-1]] = cap
    if departure.get("status") == "planned":
        i = departure["step"]
        targets[i] = max(targets.get(i, 0.0), cap * float(departure["soc"]) / 100.0)
    if ext.get("status") == "planned":
        i = ext["step"]
        targets[i] = max(targets.get(i, 0.0), cap * float(ext["target_pct"]) / 100.0)
    # Minimum by the deadline: at the end of the last step that ends at or before it.
    if need["minimum"]:
        steps = [i for i, s in enumerate(horizon.starts) if s + timedelta(hours=1) <= deadline]
        if steps:
            i = steps[-1]
            targets[i] = max(targets.get(i, 0.0), min(cap * float(opts["car_min_stop_pct"]) / 100.0, max_kwh))
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
            "need_critical": need["critical"], "need_minimum": need["minimum"], "max_soc_pct": max_pct, "extra": ext}
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


def fuse_room_amps(live: dict, opts):
    """Amps per phase the car may draw without the worst phase going above the main fuse minus 1 A.
    live["phase_amps"]: the grid currents per phase (sign ignored: the fuse sees both directions).
    The car's own draw is taken out of the measurement, so the result does not depend on the amps
    set now. Returns (room, highest phase) or (None, None) without measurements."""
    phases = live.get("phase_amps")
    if not phases:
        return None, None
    imax = max(abs(float(a)) for a in phases)
    car_draw = max(0.0, float(live.get("charger_w") or 0.0)) / watt_per_amp(opts) if live.get("status") == "charging" else 0.0
    room = math.floor(float(opts["main_fuse_amps"]) - 1.0 - max(0.0, imax - car_draw))
    return room, imax


def full_power_amps(live: dict, opts) -> int:
    """The highest current the car may draw: car_max_amps, within the main fuse minus fuse_margin_amps
    on the worst phase (the house's own draw measured, the car's taken out)."""
    amax = int(opts["car_max_amps"])
    phases = live.get("phase_amps")
    if not phases or not opts.get("main_fuse_amps"):
        return amax
    imax = max(abs(float(a)) for a in phases)
    car_draw = max(0.0, float(live.get("charger_w") or 0.0)) / watt_per_amp(opts) if live.get("status") == "charging" else 0.0
    room = math.floor(float(opts["main_fuse_amps"]) - float(opts.get("fuse_margin_amps", 0) or 0) - max(0.0, imax - car_draw))
    return int(max(int(opts["car_min_amps"]), min(amax, room)))


def _car_kwh(r) -> float:
    return float((r or {}).get("car_grid") or 0) + float((r or {}).get("car_pv") or 0) + float((r or {}).get("car_bat") or 0)


def short_gap(plan_row, ahead, opts):
    """While the car charges from the grid and the plan wants a pause for this hour only: is the next
    planned charge close (car_pause_gap_hours) and the saving of waiting small (below
    car_pause_min_saving_dkk per kWh)? Then a pause is not worth it (and risks the charger ending the
    session). -> saving in DKK/kWh, or None when the pause is worth it."""
    gap_h = float(opts.get("car_pause_gap_hours", 0) or 0)
    if gap_h <= 0 or not plan_row or not ahead:
        return None
    from datetime import datetime, timedelta
    start = datetime.fromisoformat(plan_row["t"])
    planned = [r for r in ahead if (r.get("car_grid") or 0) + (r.get("car_pv") or 0) > 0.1]
    if not planned:
        return None
    first = datetime.fromisoformat(planned[0]["t"])
    if first - (start + timedelta(hours=1)) > timedelta(hours=gap_h) - timedelta(minutes=1):
        return None
    # the planned hours of the run that follows the gap
    run = [planned[0]]
    for r in planned[1:]:
        if datetime.fromisoformat(r["t"]) - datetime.fromisoformat(run[-1]["t"]) <= timedelta(hours=1):
            run.append(r)
        else:
            break
    later = sum(r["buy"] for r in run) / len(run)
    saving = float(plan_row["buy"]) - later
    return saving if saving < float(opts.get("car_pause_min_saving_dkk", 0.20)) else None


def small_sessions(car_kwh, min_kwh: float, gap_hours: float = 0, keep_first: bool = False) -> list:
    """Steps of the planned charging sessions that bring less than `min_kwh` to the car (not worth
    plugging in or starting the charger for). A session is the hours with > 0.1 kWh to the car, with
    pauses of at most `gap_hours` inside it. keep_first: the session that includes step 0 is kept (the
    charger is charging now and may finish)."""
    if not min_kwh or min_kwh <= 0:
        return []
    gap = int(max(0.0, float(gap_hours or 0)))
    sessions, last = [], None
    for i, k in enumerate(car_kwh):
        if k > 0.1:
            if sessions and i - last - 1 <= gap:
                sessions[-1].append(i)
            else:
                sessions.append([i])
            last = i
    out = []
    for steps in sessions:
        if keep_first and steps[0] == 0:
            continue
        if sum(float(car_kwh[j]) for j in steps) < min_kwh:
            out += steps
    return out


def grid_sessions(grid_kwh, min_kwh: float = 0.1) -> list:
    """Runs of consecutive steps with more than min_kwh to the car from the grid (or house battery)."""
    out = []
    for i, k in enumerate(grid_kwh):
        if float(k) > min_kwh:
            if out and out[-1][-1] == i - 1:
                out[-1].append(i)
            else:
                out.append([i])
    return out


def consolidate_window(grid_kwh, buy, cap_kwh, available, keep_first: bool = False) -> list:
    """The planned grid charging of the car in one block instead of several with pauses between them
    (the THOR ends the session ~3 min after a pause). -> the steps between the first and the last
    session to take out of the car's availability, so the cheapest run of consecutive steps that can
    hold the same energy is left; [] when there is nothing to merge or no such run.
    cap_kwh: per step, the most the car can take (max kW x step length). keep_first: charging now,
    so the block starts in step 0."""
    sessions = grid_sessions(grid_kwh)
    if len(sessions) < 2:
        return []
    a0, b0 = sessions[0][0], sessions[-1][-1]
    span = list(range(a0, b0 + 1))
    if not all(available[i] for i in span):
        return []                       # unplugged in between: the gap is not a pause
    energy = sum(float(grid_kwh[i]) for i in span)
    starts = [a0] if (keep_first and a0 == 0) else span
    best = None
    for a in starts:
        got, cost, b = 0.0, 0.0, a
        while b <= b0 and got < energy - 1e-6:
            take = min(float(cap_kwh[b]), energy - got)
            got += take
            cost += take * float(buy[b])
            b += 1
        if got < energy - 1e-6:
            continue
        if best is None or cost < best[0] - 1e-9:
            best = (cost, a, b - 1)
    if best is None:
        return []
    _, a, b = best
    return [i for i in span if not a <= i <= b]


def session_kwh(plan_row, ahead, sun_is_cheap: bool = False, gap_hours: float = 0) -> float:
    """Energy for the car in the session that starts this hour: the planned car energy of this hour and
    the following ones (pauses of at most `gap_hours` included). When the sun is cheap, an hour's
    planned export counts too (the solar controller may take it)."""
    gap = int(max(0.0, float(gap_hours or 0)))
    total, idle = 0.0, 0
    for r in [plan_row, *(ahead or [])]:
        if not r:
            break
        car = float(r.get("car_grid") or 0) + float(r.get("car_pv") or 0) + float(r.get("car_bat") or 0)
        k = car if car > 0.1 else (float(r.get("pv_exp") or 0) if sun_is_cheap else 0.0)
        if k > 0.1:
            total += k
            idle = 0
        else:
            idle += 1
            if idle > gap:
                break
    return total


def decide(live: dict, plan_row, opts, ahead=None) -> dict:
    """One control step. live: status, car_soc, pv_w, load_w, charger_w, house_soc,
    bat_charge_w, amps, secs_since_status, sell. ahead: the plan's rows after the current hour.
    Returns the decision and the calls it implies."""
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

    want, amps, reason, source = False, None, "", "plan"
    if soc >= opts["car_max_soc_pct"]:
        reason, source = f"car at max SoC ({soc:.0f} %)", "max_soc"
    elif need["critical"]:
        want, amps, reason, source = True, amax, "critical SoC", "critical"
    elif planned_forced_kw > PLAN_KW_MIN and opts.get("car_full_power", True) and live.get("now"):
        # Full power (less charging loss than a low current spread over the hour): the last hour of a
        # session charges at full power from its start and stops when its energy is in (the plan is
        # re-made every few minutes); the first hour of a session that goes on starts as late as it
        # can at full power and runs on into the next hour without a pause.
        from datetime import datetime, timedelta
        full = full_power_amps(live, opts)
        full_kw = full * wpa / 1000.0
        energy = (float(plan_row["car_grid"]) + float(plan_row["car_bat"]) + float(plan_row["car_pv"]))
        continues = bool(ahead) and _car_kwh(ahead[0]) > 0.1
        now_l = datetime.fromisoformat(live["now"])
        hour_end = now_l.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        left_min = (hour_end - now_l).total_seconds() / 60.0
        need_min = energy / full_kw * 60.0 + 2.0 if full_kw > 0 else 60.0
        if continues and not charging and need_min < left_min:
            start_at = hour_end - timedelta(minutes=need_min)
            reason, source = f"plan: waiting to start at full power at {start_at:%H:%M}", "plan_wait"
        else:
            want, amps = True, full
            reason = f"plan: {energy:.1f} kWh this hour at full power ({full} A)"
    elif planned_forced_kw > PLAN_KW_MIN:
        want = True
        amps = amps_for(planned_forced_kw + planned_pv_kw)
        reason = f"plan: {planned_forced_kw + planned_pv_kw:.1f} kW this hour"
    elif (planned_pv_kw > PLAN_KW_MIN or sun_is_cheap) and has_surplus \
            and live.get("house_soc", 0.0) >= opts["car_house_min_soc"]:
        want = True
        amps = int(min(max(int(surplus / wpa), amin), amax))
        reason, source = f"solar surplus {surplus:.0f} W", "solar"
    elif plan_row is None and live.get("plan_pending"):
        reason, source = "waiting for a plan with the car", "plan_pending"
    elif plan_row is None and need["minimum"]:
        # No current plan (planner failing): fall back to charging the minimum at full power.
        want, amps, reason, source = True, amax, "no plan, below minimum SoC", "fallback"
    else:
        reason = "plan: no car charging this hour" if plan_row else "no plan"
        gap = short_gap(plan_row, ahead, opts) if charging and live.get("pv_w", 0.0) < 1000 else None
        if gap is not None:
            want, amps = True, amin
            reason = f"short gap, saves only {gap:.2f} DKK/kWh: keep charging at {amin} A"

    # Not worth starting the charger for a few hundred Wh: only when the session brings at least
    # car_min_session_kwh (the critical SoC and the no-plan fallback excepted).
    min_kwh = float(opts.get("car_min_session_kwh", 0) or 0)
    if want and not charging and min_kwh > 0 and source in ("plan", "solar") and plan_row:
        got = session_kwh(plan_row, ahead, sun_is_cheap, opts.get("car_pause_gap_hours", 0))
        if got < min_kwh:
            want, amps = False, None
            reason, source = f"only {got:.1f} kWh planned (under {min_kwh:g} kWh): not starting", "min_session"

    secs = live.get("secs_since_status", 9999)
    room, imax = fuse_room_amps(live, opts) if opts.get("main_fuse_amps") else (None, None)
    out["fuse"] = {"room_amps": room, "max_phase_amps": None if imax is None else round(imax, 1),
                   "limited": False}
    if want and room is not None and room < amps:
        amps = max(amin, room)
        out["fuse"]["limited"] = True
        out["fuse"]["at_minimum"] = room < amin           # the car cannot go lower: the battery must
        reason += f" (fuse limit, highest phase {imax:.0f} A)"
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
    # "solar" when the sun drives the decision (charging on surplus, or pausing while there is real
    # sun): the emergency stop on the charger is only for charging from the grid.
    out["source"] = "solar" if source == "solar" or live.get("pv_w", 0.0) >= 1000 else source
    return out


# ---- SoC between the car's (15-minute) cloud updates ---------------------------------------
GAP_S = 60   # power samples further apart than this are a gap, not energy


def add_energy(log, t, power_w, last):
    """Append to the charging log [(t, cumulative kWh)]; `last` = (t, W) of the previous sample.
    -> the new `last`."""
    cum = log[-1][1] if log else 0.0
    if last is not None and power_w is not None:
        dt = (t - last[0]).total_seconds()
        if 0 < dt < GAP_S:
            cum += (max(0.0, last[1]) + max(0.0, power_w)) / 2.0 * dt / 3_600_000.0
    if power_w is not None:
        log.append((t, cum))
    return (t, power_w) if power_w is not None else None


def _cum_at(log, t):
    if t <= log[0][0]:
        return log[0][1]
    if t >= log[-1][0]:
        return log[-1][1]
    lo, hi = 0, len(log) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if log[mid][0] <= t:
            lo = mid
        else:
            hi = mid
    (t0, c0), (t1, c1) = log[lo], log[hi]
    span = (t1 - t0).total_seconds()
    return c0 + (c1 - c0) * ((t - t0).total_seconds() / span if span else 0.0)


def estimate_soc(soc, anchor_t, log, now, eta: float, cap_kwh: float) -> dict:
    """The car's SoC now from its last reported SoC (at anchor_t) plus what the charger delivered
    since. -> {"soc", "kia_soc", "kia_at", "kwh_since", "estimated", "uncertain"}."""
    out = {"soc": soc, "kia_soc": soc, "kia_at": anchor_t.isoformat() if anchor_t else None,
           "kwh_since": 0.0, "estimated": False, "uncertain": False}
    if soc is None or anchor_t is None or not log or anchor_t >= log[-1][0]:
        return out
    kwh = max(0.0, _cum_at(log, now) - _cum_at(log, anchor_t))
    out["uncertain"] = anchor_t < log[0][0]
    if kwh > 0.01:
        out.update(soc=round(min(100.0, soc + kwh * eta / cap_kwh * 100.0), 1), kwh_since=round(kwh, 2),
                   estimated=True)
    return out
