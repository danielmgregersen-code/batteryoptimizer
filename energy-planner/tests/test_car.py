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


def row(grid=0.0, pv=0.0, bat=0.0, dt=1.0):
    return {"car_grid": grid, "car_pv": pv, "car_bat": bat, "dt": dt}


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
    d = carmod.decide(sunny, row(), OPTS)                      # sun sells below the car value
    assert d["action"] == "charge" and d["target_amps"] == 7  # 5400 W / 690 W per A
    weak = live(status="suspended_evse", pv_w=4000, load_w=600, sell=0.4)
    assert carmod.decide(weak, row(), OPTS)["action"] == "idle"   # below the 4200 W start


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
