import numpy as np
import pytest

from fixtures import BUY_TODAY, SELL_TODAY
from optimizer import BatteryParams, solve, terminal_value_for

CAP = 7.75


def params(**kw):
    base = dict(capacity_kwh=CAP, max_power_kw=4.0, soc_floor_kwh=0.1 * CAP, soc_ceiling_kwh=CAP,
                eta_pv_bat=0.97, eta_grid_bat=0.94, eta_discharge=0.95, wear_dkk_per_kwh=0.0)
    base.update(kw)
    return BatteryParams(**base)


def run(buy, sell, pv, load, soc0_pct=50.0, p=None, tv=None, **kw):
    p = p or params()
    T = len(buy)
    tv = terminal_value_for(buy, p) if tv is None else tv
    return solve(buy, sell, pv, load, np.ones(T), CAP * soc0_pct / 100, p, tv, **kw), p


def test_energy_balances_and_soc_chain_hold():
    buy, sell = BUY_TODAY, SELL_TODAY
    pv = [0.0] * 9 + [0.5, 0.8, 1.2, 1.0, 1.1, 0.5, 0.1] + [0.0] * 8
    load = [0.6] * 24
    plan, p = run(buy, sell, pv, load, soc0_pct=40)
    assert np.allclose(plan.pv_load + plan.pv_bat + plan.pv_exp + plan.pv_curt, plan.pv, atol=1e-6)
    assert np.allclose(plan.pv_load + plan.bat_load + plan.grid_load, plan.load, atol=1e-6)
    prev = plan.soc0
    for t in range(len(plan)):
        expect = prev + p.eta_pv_bat * plan.pv_bat[t] + p.eta_grid_bat * plan.grid_bat[t] - plan.bat_load[t] / p.eta_discharge
        assert plan.soc_end[t] == pytest.approx(expect, abs=1e-6)
        prev = plan.soc_end[t]
    assert plan.soc_end.min() >= p.soc_floor_kwh - 1e-6 and plan.soc_end.max() <= p.soc_ceiling_kwh + 1e-6


def test_power_limits_are_respected():
    plan, p = run([1.0] * 3 + [5.0] * 3, [0.5] * 6, [0.0] * 6, [3.0] * 6, soc0_pct=10)
    assert (p.eta_grid_bat * plan.grid_bat + p.eta_pv_bat * plan.pv_bat).max() <= p.max_power_kw + 1e-6
    assert (plan.bat_load / p.eta_discharge).max() <= p.max_power_kw + 1e-6


def test_cheap_night_expensive_evening_charges_at_night_and_discharges_in_the_peak():
    buy = [0.5] * 6 + [1.5] * 11 + [3.0] * 4 + [1.5] * 3
    sell = [0.3] * 24
    plan, _ = run(buy, sell, [0.0] * 24, [0.7] * 24, soc0_pct=10)
    assert plan.grid_bat[:6].sum() > 5.0           # fills the battery in the cheap hours
    assert plan.grid_bat[6:].sum() < 1e-6          # and not at any other time
    assert np.allclose(plan.bat_load[17:21], plan.load[17:21])   # the whole 3.0 DKK peak comes from the battery


def test_sells_the_sun_when_the_night_price_is_lower_than_what_the_sale_pays():
    """The user's rule: if selling pays more than charging at night costs, charge at night instead."""
    buy = [0.6] * 6 + [1.8] * 11 + [2.4] * 4 + [1.8] * 3
    sell = [0.5] * 9 + [1.6] * 8 + [0.5] * 7              # selling at midday pays 1.6
    pv = [0.0] * 10 + [2.0, 3.0, 3.0, 2.0] + [0.0] * 10
    plan, _ = run(buy, sell, pv, [0.5] * 24, soc0_pct=10)
    assert plan.pv_exp.sum() > 7.0                        # nearly all the sun is sold
    assert plan.pv_bat.sum() < 0.5
    assert plan.grid_bat[:6].sum() > 4.0                  # the battery is filled from the night grid


def test_stores_the_sun_when_selling_pays_little():
    buy = [1.0] * 24
    buy[17:21] = [3.0] * 4
    sell = [0.1] * 24
    pv = [0.0] * 10 + [2.0, 3.0, 3.0, 2.0] + [0.0] * 10
    plan, _ = run(buy, sell, pv, [0.5] * 24, soc0_pct=10)
    assert plan.pv_bat.sum() > 5.0
    assert np.allclose(plan.bat_load[17:21], plan.load[17:21])


def test_negative_sell_price_curtails_instead_of_exporting():
    plan, _ = run([1.0] * 4, [-0.5] * 4, [3.0] * 4, [0.0] * 4, soc0_pct=100)
    assert plan.pv_exp.sum() == pytest.approx(0.0, abs=1e-6)
    assert plan.pv_curt.sum() == pytest.approx(12.0, abs=1e-6)


def test_wear_cost_suppresses_marginal_cycling():
    buy = [1.0] * 12 + [1.2] * 12                       # 0.2 spread: not worth 2 x 6 % losses + wear
    sell = [0.1] * 24
    cheap, _ = run(buy, sell, [0.0] * 24, [0.5] * 24, soc0_pct=10, p=params(wear_dkk_per_kwh=0.0))
    costly, _ = run(buy, sell, [0.0] * 24, [0.5] * 24, soc0_pct=10, p=params(wear_dkk_per_kwh=0.5))
    assert costly.grid_bat.sum() <= cheap.grid_bat.sum() + 1e-9
    assert costly.grid_bat.sum() < 1e-6


def test_grid_charging_can_be_disabled_and_costs_more():
    buy = [0.5] * 6 + [1.5] * 11 + [3.0] * 4 + [1.5] * 3
    args = (buy, [0.3] * 24, [0.0] * 24, [0.7] * 24)
    with_grid, p = run(*args, soc0_pct=10)
    without, _ = run(*args, soc0_pct=10, allow_grid_charge=False)
    assert without.grid_bat.sum() == pytest.approx(0.0, abs=1e-9)
    assert without.net_cost > with_grid.net_cost


def test_below_floor_start_state_is_still_feasible():
    plan, p = run([1.0] * 4, [0.5] * 4, [0.0] * 4, [1.0] * 4, soc0_pct=3)
    assert plan.soc_end[0] >= CAP * 0.03 - 1e-6


def test_partial_first_hour_limits_power_by_remaining_time():
    p = params()
    plan = solve([0.5, 3.0], [0.3, 0.3], [0, 0], [0, 0], np.array([0.25, 1.0]), CAP * 0.1, p, 0.0)
    assert (p.eta_grid_bat * plan.grid_bat[0]) <= p.max_power_kw * 0.25 + 1e-6


def test_real_afternoon_of_2026_11_14():
    """12:00-24:00 on the user's data. The 3.1 DKK peak (17-21) must be served from the battery."""
    sl = slice(12, 24)
    pv = [h[0] for h in __import__("fixtures").SOLCAST_HOURLY][sl]
    load = [1.0] * 5 + [1.8] * 4 + [1.0] * 3          # placeholder load profile
    buy, sell = BUY_TODAY[sl], SELL_TODAY[sl]
    plan, p = run(buy, sell, pv, load, soc0_pct=73, p=params(wear_dkk_per_kwh=0.25))
    assert plan.bat_load[5:9].sum() > 3.0              # 17:00-21:00
    assert plan.grid_load[5:8].sum() < plan.load[5:8].sum() * 0.5
