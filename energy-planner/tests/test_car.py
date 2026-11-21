from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pytest

import car as carmod
from inputs import Horizon
from optimizer import BatteryParams, solve
from settings import DEFAULTS

TZ = ZoneInfo("Europe/Copenhagen")
UTC = timezone.utc
CAP = 7.75
P = BatteryParams(CAP, 4.0, 0.775, CAP, 0.97, 0.94, 0.95, 0.25)
OPTS = dict(DEFAULTS)


def horizon(start_local: datetime, buy, sell=None, pv=None, load=0.5):
    T = len(buy)
    starts = [(start_local + timedelta(hours=i)).astimezone(UTC) for i in range(T)]
    return Horizon(starts, list(buy), list(sell or [0.3] * T), list(pv or [0.0] * T), [load] * T, [1.0] * T)


def plan(hz, soc_pct=50, status="suspended_ev", car_soc=30, **opt):
    opts = {**OPTS, **opt}
    cp, info = carmod.build_car_params(hz, hz.starts[0], TZ, opts, status, car_soc)
    p = solve(hz.buy, hz.sell, hz.pv, hz.load, hz.dt, CAP * soc_pct / 100, P, min(hz.buy) * 0.95, car=cp)
    return p, cp, info


# ---- planning ----------------------------------------------------------------

def test_not_connected_means_no_car_in_the_plan():
    hz = horizon(datetime(2026, 11, 14, 22, tzinfo=TZ), [1.0] * 4)
    cp, info = carmod.build_car_params(hz, hz.starts[0], TZ, OPTS, "available", 30)
    assert cp is None and info == {"connected": False}


def test_minimum_is_charged_in_the_cheapest_hours_before_the_deadline():
    # 22:00 .. 09:00; cheapest night hours are 02-04; the deadline is 06:00
    buy = [2.0, 2.0, 1.8, 1.8, 1.0, 1.1, 1.2, 1.9, 1.9, 2.5, 2.5, 2.5]
    hz = horizon(datetime(2026, 11, 14, 22, tzinfo=TZ), buy)
    p, cp, info = plan(hz, car_soc=30, car_value_dkk_per_kwh=0.5)   # nothing beyond the minimum
    need_kwh = (0.42 - 0.30) * 110                                 # 13.2 kWh into the car
    charged = p.pv_car + p.bat_car + p.grid_car
    assert info["need_minimum"] and not info["need_critical"]
    assert p.car_end[7] == pytest.approx(need_kwh + 33.0, abs=0.05)   # 42 % at 06:00
    assert charged[4:7].sum() == pytest.approx(need_kwh / 0.92, abs=0.05)   # 02:00-05:00, cheapest first
    assert charged[8:].sum() == pytest.approx(0.0, abs=1e-6)       # unavailable after the deadline
    assert p.car_shortfall == pytest.approx(0.0, abs=1e-6)


def test_above_the_minimum_only_cheap_energy_goes_into_the_car():
    buy = [1.1, 1.5, 1.5, 1.5]                  # 1.1 is below the 1.25 car value, 1.5 is not
    hz = horizon(datetime(2026, 11, 14, 1, tzinfo=TZ), buy)
    p, _, _ = plan(hz, car_soc=60)
    charged = p.pv_car + p.bat_car + p.grid_car
    assert charged[0] == pytest.approx(11.04, abs=0.01)    # full power in the cheap hour
    assert charged[1:].sum() == pytest.approx(0.0, abs=1e-6)


def test_sun_selling_below_the_car_value_goes_into_the_car():
    buy = [2.0] * 4
    sell = [0.4] * 4
    pv = [6.0] * 4
    hz = horizon(datetime(2026, 11, 14, 10, tzinfo=TZ), buy, sell, pv, load=0.5)
    p, _, _ = plan(hz, soc_pct=100, car_soc=60)
    assert p.pv_car.sum() > 15.0 and p.pv_exp.sum() < 1.0


def test_critical_charges_immediately_even_at_a_high_price():
    buy = [3.0, 0.5, 0.5]
    hz = horizon(datetime(2026, 11, 14, 17, tzinfo=TZ), buy)
    p, cp, info = plan(hz, car_soc=15, car_min_start_pct=10, car_min_stop_pct=12)   # critical only
    assert info["need_critical"] and not info["need_minimum"]
    # straight to 22 % in the first (3.0 DKK) hour, not waiting for the 0.5 DKK hours
    assert (p.grid_car[0] + p.bat_car[0] + p.pv_car[0]) == pytest.approx(0.07 * 110 / 0.92, abs=0.01)


def test_house_battery_is_not_drained_into_the_car_by_default():
    hz = horizon(datetime(2026, 11, 14, 1, tzinfo=TZ), [1.1, 1.5, 1.5, 1.5])
    p, _, _ = plan(hz, soc_pct=90, car_soc=60)
    assert p.bat_car.sum() == pytest.approx(0.0, abs=1e-9)
    p2, _, _ = plan(hz, soc_pct=90, car_soc=60, car_from_house_battery=True)
    assert p2.bat_car.sum() > 1.0


def test_unreachable_minimum_is_a_shortfall_not_an_error():
    hz = horizon(datetime(2026, 11, 14, 5, tzinfo=TZ), [1.0, 1.0])    # one hour to 06:00
    p, _, _ = plan(hz, car_soc=10)
    assert p.car_shortfall > 0
    assert p.car_end[0] == pytest.approx(10 / 100 * 110 + 11.04 * 0.92, abs=0.05)


# ---- control -----------------------------------------------------------------

def live(**kw):
    base = {"status": "charging", "car_soc": 60.0, "pv_w": 0.0, "load_w": 500.0, "charger_w": 0.0,
            "house_soc": 70.0, "bat_charge_w": 0.0, "amps": 6, "secs_since_status": 5000, "sell": 1.3}
    base.update(kw)
    return base


def row(grid=0.0, pv=0.0, bat=0.0, dt=1.0, exp=0.0):
    return {"car_grid": grid, "car_pv": pv, "car_bat": bat, "dt": dt, "pv_exp": exp}


def services(d):
    return [c["service"] for c in d["calls"]]


def test_not_connected_does_nothing():
    d = carmod.decide(live(status="available"), row(), OPTS)
    assert d["action"] == "none" and d["calls"] == []


def test_critical_charges_at_16_a_and_presses_start_from_preparing():
    d = carmod.decide(live(status="preparing", car_soc=15, amps=6), row(), OPTS)
    assert d["action"] == "charge" and d["target_amps"] == 16
    assert services(d) == ["number.set_value", "button.press"]


def test_planned_grid_power_sets_the_amps():
    d = carmod.decide(live(amps=16), row(grid=7.0), OPTS)    # 7 kW -> 10.1 A -> 11 A
    assert d["action"] == "charge" and d["target_amps"] == 11


def test_planned_power_for_a_partial_hour_is_converted_to_kw():
    d = carmod.decide(live(amps=16), row(grid=2.76, dt=0.25), OPTS)   # 11.04 kW
    assert d["target_amps"] == 16


def test_resumes_a_paused_car_through_the_car_api():
    """The automation's resume never fired: it tests `maa_start_bil`, the variable is `maa_starte_bil`."""
    d = carmod.decide(live(status="suspended_evse", amps=16), row(grid=11.0), OPTS)
    assert "kia_uvo.start_charge" in services(d)


def test_cooldown_after_a_car_side_pause():
    d = carmod.decide(live(status="suspended_ev", amps=16, secs_since_status=600), row(grid=11.0), OPTS)
    assert d["action"] == "charge" and "kia_uvo.start_charge" not in services(d)
    assert "cooldown" in d["reason"]


def test_solar_follow_with_start_threshold():
    sunny = live(status="suspended_evse", pv_w=6000, load_w=600, sell=0.4, amps=6)
    d = carmod.decide(sunny, row(exp=3.0), OPTS)               # sun sells below the car value
    assert d["action"] == "charge" and d["target_amps"] == 7  # 5400 W / 690 W per A
    weak = live(status="suspended_evse", pv_w=4000, load_w=600, sell=0.4)
    assert carmod.decide(weak, row(exp=3.0), OPTS)["action"] == "idle"   # below the 4200 W start
    d = carmod.decide(sunny, row(exp=0.5), OPTS)               # too little sun left to be worth a start
    assert d["action"] == "idle" and "under 2 kWh" in d["reason"]


def test_small_sessions_are_found_merged_over_short_pauses_and_the_running_one_kept():
    assert carmod.small_sessions([0.1, 0.5, 0, 0, 0, 3.0, 2.0], 2.0, gap_hours=2) == [1]
    assert carmod.small_sessions([0.8, 0, 0.8, 0.8], 2.0, gap_hours=2) == []       # one session of 2.4 kWh
    assert carmod.small_sessions([0.8, 0, 0, 0, 0.8], 2.0, gap_hours=2) == [0, 4]
    assert carmod.small_sessions([0.8, 0, 0, 0, 0.8], 2.0, gap_hours=2, keep_first=True) == [4]
    assert carmod.small_sessions([0.5], 0) == []


def test_no_start_for_less_than_the_minimum_session_but_a_running_charge_continues():
    waiting = live(status="suspended_evse")
    d = carmod.decide(waiting, {**row(grid=0.5), "t": "2026-11-21T02:00:00+01:00"}, OPTS,
                      ahead=[{**row(grid=0.4), "t": "2026-11-21T03:00:00+01:00"}])
    assert d["action"] == "idle" and services(d) == []
    d = carmod.decide(waiting, row(grid=1.5), OPTS, ahead=[row(grid=1.5)])
    assert d["action"] == "charge"
    d = carmod.decide(live(), row(grid=0.5), OPTS)             # charging already: it may finish
    assert d["action"] == "charge"
    assert carmod.decide(live(status="suspended_evse", car_soc=5.0), row(grid=0.5), OPTS)["action"] == "charge"


def test_no_solar_charging_when_the_sun_sells_well_and_the_plan_says_no():
    d = carmod.decide(live(status="suspended_evse", pv_w=8000, load_w=600, sell=1.5), row(), OPTS)
    assert d["action"] == "idle"


def test_pause_respects_the_minimum_charge_time():
    d = carmod.decide(live(secs_since_status=300), row(), OPTS)
    assert d["action"] == "charge" and services(d) == [] and "minimum charge time" in d["reason"]
    d = carmod.decide(live(secs_since_status=5000), row(), OPTS)
    assert d["action"] == "pause" and services(d) == ["kia_uvo.stop_charge"]


def test_max_soc_stops_even_when_the_plan_wants_power():
    d = carmod.decide(live(car_soc=90, secs_since_status=10), row(grid=11.0), OPTS)
    assert d["action"] == "pause" and services(d) == ["kia_uvo.stop_charge"]


def test_without_a_plan_the_minimum_is_still_charged():
    d = carmod.decide(live(status="suspended_evse", car_soc=35, amps=16), None, OPTS)
    assert d["action"] == "charge" and d["target_amps"] == 16


def test_next_deadline():
    assert carmod.next_deadline(datetime(2026, 11, 14, 21, tzinfo=UTC), TZ, 6) == datetime(2026, 11, 15, 6, tzinfo=TZ)
    assert carmod.next_deadline(datetime(2026, 11, 15, 3, tzinfo=UTC), TZ, 6) == datetime(2026, 11, 15, 6, tzinfo=TZ)


# ---- 100 % at rock-bottom prices, and trips --------------------------------------------------------
HISTORY = [1.0 + 0.01 * i for i in range(30 * 24)]      # last 30 days: 1.00 .. 8.19 kr; 5th pct ~ 1.36


def night(buy=None, sell=None, pv=None, start_local=datetime(2026, 11, 14, 22, tzinfo=TZ)):
    buy = buy or [2.0] * 2 + [1.2] * 6 + [2.5] * 4       # 22..09: cheap 00-06
    return horizon(start_local, buy, sell or [0.5] * len(buy), pv)


def full(hz, soc=60, days=20, history=HISTORY, **opt):
    now = hz.starts[0]
    last = now - timedelta(days=days) if days is not None else None
    return carmod.full_charge_decision(hz, now, TZ, {**OPTS, **opt}, soc, last, history)


def test_full_charge_never_more_often_than_the_minimum_interval():
    d = full(night([0.2] * 12), days=10)
    assert not d["active"] and d["status"] == "too_soon" and d["possible_from"] == "2026-11-18"


def test_full_charge_only_at_rock_bottom_and_never_forced():
    assert full(night([1.2] * 12), days=20)["active"]             # 1.20 <= 5th percentile (1.36)
    assert not full(night([1.9] * 12), days=20)["active"]          # ordinary night: wait
    assert not full(night([1.9] * 12), days=60)["active"]          # long ago, still not forced
    assert full(night([1.2] * 12), days=None)["active"]            # never been full: allowed
    d = full(night([1.9] * 12), days=20)
    assert d["status"] == "waiting" and d["threshold"] == pytest.approx(1.359, abs=0.01)


def test_negative_sell_price_triggers_a_full_charge():
    buy = [2.0] * 4
    sell = [0.3, -0.2, -0.1, 0.3]
    hz = horizon(datetime(2026, 6, 14, 11, tzinfo=TZ), buy, sell, [6.0] * 4)
    d = full(hz, days=20, history=[])                             # no price history needed
    assert d["active"] and d["why"] == "negative_sell"


def test_full_charge_plans_100_percent_in_the_cheapest_hours_as_late_as_possible():
    hz = night()
    f = full(hz, soc=60, days=20, history=[1.2] * 100 + [3.0] * 700)   # 5th percentile = 1.20
    assert f["active"]
    cp, info = carmod.build_car_params(hz, hz.starts[0], TZ, OPTS, "suspended_evse", 60, full=f)
    assert info["max_soc_pct"] == 100.0
    p = solve(hz.buy, hz.sell, hz.pv, hz.load, hz.dt, CAP * 0.5, P, min(hz.buy) * 0.95, car=cp)
    assert p.car_end[7] == pytest.approx(110.0, abs=0.1)          # 100 % at 06:00
    charged = p.pv_car + p.grid_car
    assert charged[:2].sum() == pytest.approx(0.0, abs=1e-6)      # not in the dear evening hours
    assert p.car_shortfall == pytest.approx(0.0, abs=1e-6)


def test_daily_limit_without_a_full_charge():
    hz = night()
    cp, info = carmod.build_car_params(hz, hz.starts[0], TZ, OPTS, "suspended_evse", 60,
                                       full={"active": False})
    assert info["max_soc_pct"] == 90 and cp.max_kwh == pytest.approx(99.0)


def test_departure_target_and_availability_until_the_trip():
    hz = night([2.0] * 2 + [1.2] * 6 + [2.5] * 6)                 # 22:00 .. 11:00
    dep_at = datetime(2026, 11, 15, 10, 0, tzinfo=TZ).astimezone(UTC)
    dep = carmod.departure_info(dep_at, 100, hz, hz.starts[0])
    assert dep["status"] == "planned" and dep["step"] == 11       # 09:00-10:00 is the last hour
    cp, info = carmod.build_car_params(hz, hz.starts[0], TZ, OPTS, "suspended_evse", 50, departure=dep)
    assert info["max_soc_pct"] == 100 and cp.available[11] and not cp.available[12]
    p = solve(hz.buy, hz.sell, hz.pv, hz.load, hz.dt, CAP * 0.5, P, min(hz.buy) * 0.95, car=cp)
    assert p.car_end[11] == pytest.approx(110.0, abs=0.1)
    assert p.car_end[7] >= 0.42 * 110 - 0.1                       # and 42 % at 06:00 still holds (it is above)


def test_extra_charge_target_by_the_deadline_lifts_the_limit():
    hz = night()                                                  # 22:00 .. 09:00, cheap 00-06
    until = datetime(2026, 11, 15, 6, 0, tzinfo=TZ).isoformat()
    extra = {"target_pct": 95, "until": until, "label": "Mål 95 %"}
    cp, info = carmod.build_car_params(hz, hz.starts[0], TZ, OPTS, "suspended_evse", 60, extra=extra)
    assert info["extra"]["status"] == "planned" and info["extra"]["step"] == 7
    assert info["max_soc_pct"] == 95 and cp.targets[7] == pytest.approx(0.95 * 110)
    p = solve(hz.buy, hz.sell, hz.pv, hz.load, hz.dt, CAP * 0.5, P, min(hz.buy) * 0.95, car=cp)
    assert p.car_end[7] == pytest.approx(0.95 * 110, abs=0.1)
    assert (p.grid_car + p.pv_car)[:2].sum() == pytest.approx(0.0, abs=1e-6)   # only in the cheap hours


def test_extra_charge_passed_is_ignored():
    hz = night()
    past = {"target_pct": 95, "until": (hz.starts[0] - timedelta(hours=1)).isoformat()}
    cp, info = carmod.build_car_params(hz, hz.starts[0], TZ, OPTS, "suspended_evse", 60, extra=past)
    assert info["extra"]["status"] == "passed" and info["max_soc_pct"] == 90 and not cp.targets
    assert carmod.extra_info(None, hz, hz.starts[0]) == {"set": False}


def test_minimum_does_not_lower_a_higher_target_at_the_deadline():
    hz = night()
    extra = {"target_pct": 80, "until": datetime(2026, 11, 15, 6, 0, tzinfo=TZ).isoformat()}
    cp, _ = carmod.build_car_params(hz, hz.starts[0], TZ, OPTS, "suspended_evse", 30, extra=extra)
    assert cp.targets[7] == pytest.approx(0.80 * 110)              # not 42 %


def test_departure_beyond_the_horizon_or_passed():
    hz = night()
    far = carmod.departure_info(hz.starts[0] + timedelta(days=3), 100, hz, hz.starts[0])
    assert far["status"] == "beyond_horizon" and far["step"] is None
    cp, info = carmod.build_car_params(hz, hz.starts[0], TZ, OPTS, "suspended_evse", 50, departure=far)
    assert info["max_soc_pct"] == 90 and all(cp.available)          # connected until the trip, no target yet
    past = carmod.departure_info(hz.starts[0] - timedelta(hours=1), 100, hz, hz.starts[0])
    assert past["status"] == "passed"
    assert carmod.departure_info(None, 100, hz, hz.starts[0]) == {"set": False}


def test_decide_stops_at_the_effective_limit():
    d = carmod.decide(live(car_soc=92, secs_since_status=5000), row(grid=11.0), {**OPTS, "car_max_soc_pct": 100})
    assert d["action"] == "charge"
    d = carmod.decide(live(car_soc=92, secs_since_status=5000), row(grid=11.0), OPTS)
    assert d["action"] == "pause"


def test_last_full_from_statistics():
    rows = [{"start": datetime(2026, 11, d, 4, tzinfo=UTC).timestamp() * 1000, "max": m}
            for d, m in ((1, 100.0), (5, 98.0), (8, 99.4), (10, 60.0))]
    assert carmod.last_full_from_stats(rows) == datetime(2026, 11, 8, 4, tzinfo=UTC)
    assert carmod.last_full_from_stats([]) is None


def test_fuse_guard_lowers_the_car_amps_and_never_below_6():
    opts = {**OPTS, "main_fuse_amps": 25}
    # charging at 16 A (11 kW), worst phase 27 A: without the car 11 A -> room 13 A
    lv = live(status="charging", car_soc=30, charger_w=11040, amps=16, phase_amps=[27.0, 20.0, -21.0])
    d = carmod.decide(lv, row(grid=11.0), opts)
    assert d["target_amps"] == 13 and d["fuse"]["limited"] and "fuse limit" in d["reason"]
    lv["phase_amps"] = [38.0, 30.0, 30.0]                     # house 22 A on a phase: room 2 A
    d = carmod.decide(lv, row(grid=11.0), opts)
    assert d["target_amps"] == 6 and d["fuse"]["at_minimum"]
    lv["phase_amps"] = [18.0, 17.0, 17.0]                     # plenty of room: the plan's 16 A
    d = carmod.decide(lv, row(grid=11.0), opts)
    assert d["target_amps"] == 16 and not d["fuse"]["limited"]
    lv.pop("phase_amps")                                      # no sensors: no guard
    assert carmod.decide(lv, row(grid=11.0), opts)["target_amps"] == 16


def test_fuse_reason_in_danish():
    from summary import reason_da
    assert "hovedsikringen, højeste fase 27 A" in reason_da("plan: 11.0 kW this hour (fuse limit, highest phase 27 A)")


# ---- short gaps (0.17.0) ---------------------------------------------------------
def prow(t, buy, kwh=0.0):
    return {"t": t, "buy": buy, "sell": 0.5, "car_grid": kwh, "car_pv": 0.0, "car_bat": 0.0, "dt": 1.0}


NOW_ROW = prow("2026-11-18T02:00:00+01:00", 1.30)


def ahead(*rows):
    return list(rows)


def test_short_gap_with_a_small_saving_keeps_charging_at_6_a():
    lv = live(status="charging", car_soc=35, amps=16)
    nxt = ahead(prow("2026-11-18T03:00:00+01:00", 1.20, 11.0), prow("2026-11-18T04:00:00+01:00", 1.22, 5.0))
    d = carmod.decide(lv, NOW_ROW, OPTS, ahead=nxt)
    assert d["action"] == "charge" and d["target_amps"] == 6 and "short gap" in d["reason"]
    assert "kia_uvo.stop_charge" not in services(d)


def test_a_worthwhile_or_long_gap_still_pauses():
    lv = live(status="charging", car_soc=35, amps=16)
    dear_now = ahead(prow("2026-11-18T03:00:00+01:00", 1.05, 11.0))            # saves 0.25 kr/kWh
    assert carmod.decide(lv, NOW_ROW, OPTS, ahead=dear_now)["action"] == "pause"
    far = ahead(prow("2026-11-18T03:00:00+01:00", 1.25), prow("2026-11-18T04:00:00+01:00", 1.25),
                prow("2026-11-18T05:00:00+01:00", 1.25, 11.0))                 # 3 h away
    assert carmod.decide(lv, NOW_ROW, OPTS, ahead=far)["action"] == "pause"
    near = ahead(prow("2026-11-18T03:00:00+01:00", 1.25, 11.0))
    assert carmod.decide(lv, NOW_ROW, {**OPTS, "car_pause_gap_hours": 0}, ahead=near)["action"] == "pause"
    assert carmod.decide(live(status="charging", car_soc=35, amps=16, pv_w=3000), NOW_ROW, OPTS, ahead=near)["action"] != "charge" \
        or "short gap" not in carmod.decide(live(status="charging", car_soc=35, amps=16, pv_w=3000), NOW_ROW, OPTS, ahead=near)["reason"]


def test_short_gap_reason_in_danish():
    from summary import reason_da
    assert reason_da("short gap, saves only 0.12 DKK/kWh: keep charging at 6 A") == "Kort hul, sparer kun 0,12 kr/kWh: fortsætter på 6 A"


# ---- estimated SoC (0.19.0) ------------------------------------------------------
def charge_log(start, minutes, watts, step_s=5):
    log, last = [], None
    for i in range(int(minutes * 60 / step_s) + 1):
        last = carmod.add_energy(log, start + timedelta(seconds=i * step_s), watts, last)
    return log


def test_estimate_soc_adds_what_the_charger_delivered():
    t0 = datetime(2026, 11, 18, 11, 0, tzinfo=UTC)
    log = charge_log(t0 - timedelta(minutes=5), 20, 11000.0)
    est = carmod.estimate_soc(60.0, t0, log, t0 + timedelta(minutes=15), 0.92, 110.0)
    assert est["estimated"] and est["kwh_since"] == pytest.approx(2.75, abs=0.02)
    assert est["soc"] == pytest.approx(60 + 2.75 * 0.92 / 110 * 100, abs=0.05)
    assert not est["uncertain"]
    later = carmod.estimate_soc(60.0, t0 + timedelta(minutes=30), log, t0 + timedelta(minutes=30), 0.92, 110.0)
    assert later["soc"] == 60.0 and not later["estimated"]                   # a fresh report wins
    assert carmod.estimate_soc(99.0, t0, log, t0 + timedelta(minutes=15), 0.92, 110.0)["soc"] == 100.0


def test_gaps_in_the_power_log_are_not_energy():
    t0 = datetime(2026, 11, 18, 11, 0, tzinfo=UTC)
    log, last = [], None
    last = carmod.add_energy(log, t0, 11000.0, last)
    last = carmod.add_energy(log, t0 + timedelta(minutes=10), 11000.0, last)   # 10 min without samples
    assert log[-1][1] == 0.0
