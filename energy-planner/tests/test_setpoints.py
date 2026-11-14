import numpy as np
import pytest

from optimizer import BatteryParams, solve, terminal_value_for
from setpoints import derive_setpoints

CAP = 7.75
P = BatteryParams(CAP, 4.0, 0.775, CAP, 0.97, 0.94, 0.95, 0.0)
BUY_CHEAP_NIGHT = [0.5] * 2 + [1.5] * 11 + [3.0] * 4 + [1.5] * 3


def plan_for(buy, soc_pct, pv=None, load=0.7, sell=0.3):
    T = len(buy)
    return solve(buy, [sell] * T if np.isscalar(sell) else sell, pv or [0.0] * T, [load] * T, np.ones(T),
                 CAP * soc_pct / 100, P, terminal_value_for(buy, P))


def sp(plan, soc_pct):
    return derive_setpoints(plan, soc_pct, P, 10.0, 10, 100)


def test_night_grid_charge_sets_battery_first_target_and_rate():
    s = sp(plan_for(BUY_CHEAP_NIGHT, 10), 10)
    assert s["mode"] == "grid_charge" and s["battery_first"] and s["grid_charge"]
    assert s["max_soc_pct"] == 100                       # the run of cheap hours fills the battery
    assert 10 <= s["charge_rate_pct"] <= 100 and s["charge_rate_pct"] % 5 == 0
    # 2.98 kWh stored in the first cheap hour (the full 4 kW is saved for the later one) = 3.2 kW from
    # the grid = 32 % of a 10 kW inverter, rounded up to the next 5 %
    assert s["charge_rate_pct"] == 35


def test_peak_hour_discharges_down_to_the_floor():
    buy = [3.0] * 3 + [1.0] * 3                           # peak now, cheap later
    s = sp(plan_for(buy, 80, load=1.0), 80)
    assert s["mode"] == "discharge" and not s["battery_first"]
    assert s["min_soc_pct"] == 10 and s["max_soc_pct"] is None


def test_hold_pins_the_floor_at_the_current_soc():
    buy = [1.0, 3.0, 3.0]                                 # cheaper now: spend grid now, keep battery for the peak
    s = sp(plan_for(buy, 60, load=1.0), 60)
    assert s["mode"] == "hold"
    assert s["min_soc_pct"] == 60 and not s["battery_first"]


def test_solar_charge_has_no_grid_charge_and_full_rate():
    buy = [1.0] * 3 + [3.0] * 3
    pv = [3.0, 0, 0, 0, 0, 0]
    s = sp(plan_for(buy, 20, pv=pv, load=0.5, sell=0.1), 20)
    assert s["mode"] == "solar_charge" and s["battery_first"] and not s["grid_charge"]
    assert s["charge_rate_pct"] == 100
    assert s["max_soc_pct"] is not None and s["max_soc_pct"] > 20


def test_export_hint_when_the_plan_sells_sun_now():
    buy = [2.0] * 3
    pv = [2.0, 0, 0]
    s = sp(plan_for(buy, 100, pv=pv, load=0.2, sell=1.8), 100)
    assert s["export_hint"] == "export"
