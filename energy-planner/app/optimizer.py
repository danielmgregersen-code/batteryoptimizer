"""Hour-by-hour linear program: when to charge from sun, charge from grid, discharge, sell.

Per step t the model chooses how to split solar and load over the battery, the house and the
grid, minimising  sum(buy * import) - sum(sell * export) + wear - value of leftover energy.

Energy balance per step (kWh):
    pv        = pv_load + pv_bat + pv_exp + pv_curt
    load      = pv_load + bat_load + grid_load
    soc_t     = soc_t-1 + eta_pv*pv_bat + eta_grid*grid_bat - bat_load/eta_discharge

The battery can only serve the house (bat_load <= load): selling stored energy to the grid
(arbitrage) is deliberately not possible. A kWh of sun is therefore stored only if it is worth
more than selling it now, i.e. if the stored kWh later replaces a more expensive purchase than
the cheapest grid charging available. That trade-off is what the solver finds on its own.
"""
from dataclasses import dataclass

import numpy as np
from scipy.optimize import linprog

NV = 8
TIE_BREAK_DKK = 1e-4
PV_LOAD, PV_BAT, PV_EXP, PV_CURT, BAT_LOAD, GRID_LOAD, GRID_BAT, SOC = range(NV)


@dataclass
class BatteryParams:
    capacity_kwh: float
    max_power_kw: float
    soc_floor_kwh: float
    soc_ceiling_kwh: float
    eta_pv_bat: float
    eta_grid_bat: float
    eta_discharge: float
    wear_dkk_per_kwh: float


@dataclass
class Plan:
    pv_load: np.ndarray
    pv_bat: np.ndarray
    pv_exp: np.ndarray
    pv_curt: np.ndarray
    bat_load: np.ndarray
    grid_load: np.ndarray
    grid_bat: np.ndarray
    soc_end: np.ndarray  # kWh stored at the end of each step
    buy: np.ndarray
    sell: np.ndarray
    pv: np.ndarray
    load: np.ndarray
    dt: np.ndarray
    soc0: float
    net_cost: float  # DKK over the horizon, credited for energy left in the battery

    def __len__(self):
        return len(self.buy)


def terminal_value_for(buy, params: BatteryParams) -> float:
    """DKK per kWh stored at the end of the horizon.

    Leftover energy could be bought back at the cheapest known hour, so it is worth that price
    per stored kWh. This stops the plan from draining the battery just because the horizon ends.
    """
    return float(min(buy)) / params.eta_grid_bat


def solve(buy, sell, pv, load, dt, soc0_kwh, params: BatteryParams,
          terminal_value: float, allow_grid_charge: bool = True) -> Plan:
    buy, sell, pv, load, dt = (np.asarray(a, dtype=float) for a in (buy, sell, pv, load, dt))
    T = len(buy)
    if T == 0:
        raise ValueError("empty horizon")
    n = T * NV

    def ix(t, k):
        return t * NV + k

    A_eq = np.zeros((3 * T, n))
    b_eq = np.zeros(3 * T)
    A_ub = np.zeros((2 * T, n))
    b_ub = np.zeros(2 * T)
    for t in range(T):
        r = 3 * t
        for k in (PV_LOAD, PV_BAT, PV_EXP, PV_CURT):
            A_eq[r, ix(t, k)] = 1.0
        b_eq[r] = pv[t]

        for k in (PV_LOAD, BAT_LOAD, GRID_LOAD):
            A_eq[r + 1, ix(t, k)] = 1.0
        b_eq[r + 1] = load[t]

        A_eq[r + 2, ix(t, SOC)] = 1.0
        A_eq[r + 2, ix(t, PV_BAT)] = -params.eta_pv_bat
        A_eq[r + 2, ix(t, GRID_BAT)] = -params.eta_grid_bat
        A_eq[r + 2, ix(t, BAT_LOAD)] = 1.0 / params.eta_discharge
        if t == 0:
            b_eq[r + 2] = soc0_kwh
        else:
            A_eq[r + 2, ix(t - 1, SOC)] = -1.0

        A_ub[2 * t, ix(t, PV_BAT)] = params.eta_pv_bat
        A_ub[2 * t, ix(t, GRID_BAT)] = params.eta_grid_bat
        b_ub[2 * t] = params.max_power_kw * dt[t]
        A_ub[2 * t + 1, ix(t, BAT_LOAD)] = 1.0 / params.eta_discharge
        b_ub[2 * t + 1] = params.max_power_kw * dt[t]

    lo = min(params.soc_floor_kwh, soc0_kwh)
    hi = max(params.soc_ceiling_kwh, soc0_kwh)
    bounds = []
    for t in range(T):
        for k in range(NV):
            if k == SOC:
                bounds.append((lo, hi))
            elif k == GRID_BAT and not allow_grid_charge:
                bounds.append((0.0, 0.0))
            else:
                bounds.append((0.0, None))

    c = np.zeros(n)
    for t in range(T):
        c[ix(t, GRID_LOAD)] = buy[t]
        # Tiny preference for charging as late as possible among equally priced hours: keeps the
        # plan unique (no flip-flopping between replans) and avoids sitting at a high SoC.
        c[ix(t, GRID_BAT)] = buy[t] + params.wear_dkk_per_kwh + TIE_BREAK_DKK * (T - t)
        c[ix(t, PV_BAT)] = params.wear_dkk_per_kwh
        c[ix(t, PV_EXP)] = -sell[t]
    c[ix(T - 1, SOC)] -= terminal_value

    res = linprog(c, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq, bounds=bounds, method="highs")
    if not res.success:
        raise RuntimeError(f"optimiser failed: {res.message}")

    x = np.clip(res.x.reshape(T, NV), 0.0, None)
    return Plan(
        pv_load=x[:, PV_LOAD], pv_bat=x[:, PV_BAT], pv_exp=x[:, PV_EXP], pv_curt=x[:, PV_CURT],
        bat_load=x[:, BAT_LOAD], grid_load=x[:, GRID_LOAD], grid_bat=x[:, GRID_BAT],
        soc_end=x[:, SOC], buy=buy, sell=sell, pv=pv, load=load, dt=dt, soc0=soc0_kwh,
        net_cost=float(res.fun + terminal_value * soc0_kwh),
    )
