import math

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


def test_peak_hour_discharges_down_to_the_planned_soc():
    buy = [3.0] * 3 + [1.0] * 3                           # peak now, cheap later
    s = sp(plan_for(buy, 80, load=1.0), 80)
    assert s["mode"] == "discharge" and not s["battery_first"]
    # floor = SoC after this hour (80 % - 1 kWh / 0.95 of 7.75 kWh = 66.4 %), not a hard 10 %:
    # if the planner stops refreshing, the battery stops where the plan expected it to be
    assert s["min_soc_pct"] == 66 and s["max_soc_pct"] is None


def test_hold_blocks_both_charging_and_discharging():
    # a bit cheaper now (but not enough to grid-charge for the peak): buy now, keep the battery
    buy = [2.8, 3.0, 3.0]
    s = sp(plan_for(buy, 40, load=1.5), 40)
    assert s["mode"] == "hold"
    # battery first with max SoC = current SoC: no discharge, and surplus sun is not stored either
    assert s["battery_first"] and not s["grid_charge"]
    assert s["max_soc_pct"] == 40 and s["min_soc_pct"] == 40


def test_selling_the_sun_is_a_hold_not_load_first():
    """0.1.0 mapped this to load first, where surplus sun goes into the battery anyway."""
    buy = [1.8] * 4 + [2.4] * 4 + [0.6] * 6 + [2.4] * 4
    sell = [1.6] * 4 + [0.5] * 14
    pv = [3.0] * 4 + [0.0] * 14
    s = sp(plan_for(buy, 30, pv=pv, load=0.5, sell=sell), 30)
    assert s["mode"] == "hold" and s["export_hint"] == "export"
    assert s["battery_first"] and s["max_soc_pct"] == 30


def test_grid_charge_target_leaves_room_for_the_planned_sun():
    """Grid to 55 % then sun to 100 %: the grid target must be 55 %, not 100 %."""
    buy = [0.6] + [1.0] * 3 + [3.0] * 6
    pv = [0.0, 1.5, 1.5, 1.5] + [0.0] * 6
    T = len(buy)
    plan = solve(buy, [0.2] * T, pv, [0.3] * 4 + [1.2] * 6, np.ones(T), CAP * 0.1,
                 BatteryParams(CAP, 4.0, 0.775, CAP, 0.97, 0.94, 0.95, 0.25), terminal_value_for(buy, P))
    assert plan.grid_bat[0] > 1.0 and plan.pv_bat[1:4].sum() > 3.0
    s = sp(plan, 10)
    expected = math.ceil(plan.soc_end[0] / CAP * 100 - 1e-9)
    assert s["mode"] == "grid_charge" and s["max_soc_pct"] == expected and s["max_soc_pct"] < 70


def test_partial_solar_storage_caps_the_charge_rate():
    # sell is high now and low later: store only what the evening needs, sell the rest now
    buy = [2.0] * 2 + [3.0] * 2
    sell = [1.5, 0.2, 0.2, 0.2]
    s = sp(plan_for(buy, 10, pv=[6.0, 0, 0, 0], load=1.0, sell=sell), 10)
    assert s["mode"] == "solar_charge" and s["export_hint"] == "export"
    assert s["charge_rate_pct"] < 100


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
