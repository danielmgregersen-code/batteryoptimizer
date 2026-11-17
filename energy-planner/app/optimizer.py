"""Hour-by-hour linear program: when to charge from sun, charge from grid, discharge, sell,
and when to charge the car.

Per step t the model chooses how to split solar and load over the battery, the house, the car
and the grid, minimising  sum(buy * import) - sum(sell * export) + wear - value of leftover energy.

Energy balance per step (kWh):
    pv        = pv_load + pv_bat + pv_exp + pv_curt + pv_car
    load      = pv_load + bat_load + grid_load                     (house base load, car excluded)
    soc_t     = soc_t-1 + eta_pv*pv_bat + eta_grid*grid_bat - (bat_load + bat_car)/eta_discharge
    car_t     = car_t-1 + eta_car*(pv_car + bat_car + grid_car)

The battery can only serve the house and the car: selling stored energy to the grid (arbitrage)
is deliberately not possible. A kWh of sun is therefore stored only if it is worth more than
selling it now, i.e. if the stored kWh later replaces a more expensive purchase than the cheapest
grid charging available. That trade-off is what the solver finds on its own.

The car (optional) is a flexible load: it may only charge while connected, must reach a minimum
energy by a deadline (soft, with a large penalty so an unreachable target never makes the problem
infeasible), and every kWh it holds at the end is credited at what the user is willing to pay for
charging (car value). So the car is charged beyond the minimum only from sun that sells for less
than that, or from grid hours that cost less.
"""
from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import linprog

NV = 13
TIE_BREAK_DKK = 1e-4
SHORTFALL_DKK_PER_KWH = 50.0   # penalty for missing a car minimum: far above any price
(PV_LOAD, PV_BAT, PV_EXP, PV_CURT, BAT_LOAD, GRID_LOAD, GRID_BAT, SOC,
 PV_CAR, BAT_CAR, GRID_CAR, CAR, SHORT) = range(NV)


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
class CarParams:
    available: list            # per step: connected (bool)
    max_kw: float              # at the plug
    eta: float                 # plug -> car battery
    soc0_kwh: float
    max_kwh: float             # charge limit (e.g. 90 %)
    value_dkk_per_kwh: float   # willingness to pay per kWh at the plug for charging beyond the minimum
    targets: dict = field(default_factory=dict)   # {step: kWh in the car at the end of that step}
    # House battery -> car in the plan. Off by default: the car is valued at the plug price, the
    # house battery's leftover energy only at its safe lower bound, so the LP would happily drain
    # the house battery into the car. Short buffering during solar charging is the fast
    # controller's job (as in the HA automation), not the hourly plan's.
    allow_battery: bool = False


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
    net_cost: float  # DKK over the horizon, credited for energy left in the battery (and car)
    pv_car: np.ndarray = None
    bat_car: np.ndarray = None
    grid_car: np.ndarray = None
    car_end: np.ndarray = None   # kWh in the car at the end of each step
    car_shortfall: float = 0.0   # kWh missing from the car targets

    def __len__(self):
        return len(self.buy)


def terminal_value_for(buy, params: BatteryParams) -> float:
    """DKK per kWh stored at the end of the horizon.

    A stored kWh can at most replace eta_discharge kWh of later purchases. Valuing it at the
    cheapest price in the horizon times eta_discharge is the safe lower bound: it keeps the plan
    from dumping the battery just because the horizon ends, but never makes holding energy look
    better than using it in any hour that is dearer than the cheapest one. (Valuing it at the
    buy-back cost min(buy)/eta_grid, as 0.1.0 did, is about 12 % higher than any use of the
    energy can return, so the battery sat idle through moderately priced hours.)
    """
    return float(min(buy)) * params.eta_discharge


PHASE_KV = 0.23          # kV per phase: 1 kW on one phase = 4.35 A, 1 kW over three phases = 1.45 A


def solve(buy, sell, pv, load, dt, soc0_kwh, params: BatteryParams,
          terminal_value: float, allow_grid_charge: bool = True, car: CarParams = None,
          fuse_amps: float = None, export_min_price: float = None) -> Plan:
    """fuse_amps: amps per phase available for the main fuse (fuse minus margin), or None.
    Per step the worst phase is kept below it: car and grid charging of the battery are
    three-phase, the house load is counted as if it were all on one phase (conservative).
    export_min_price: no export in hours whose (hourly) sell price is below it."""
    buy, sell, pv, load, dt = (np.asarray(a, dtype=float) for a in (buy, sell, pv, load, dt))
    T = len(buy)
    if T == 0:
        raise ValueError("empty horizon")
    n = T * NV

    def ix(t, k):
        return t * NV + k

    A_eq = np.zeros((4 * T, n))
    b_eq = np.zeros(4 * T)
    ub_rows, ub_rhs = [], []

    def ub(coefs, rhs):
        row = np.zeros(n)
        for (t, k), v in coefs.items():
            row[ix(t, k)] = v
        ub_rows.append(row)
        ub_rhs.append(rhs)

    car_eta = car.eta if car else 1.0
    for t in range(T):
        r = 4 * t
        for k in (PV_LOAD, PV_BAT, PV_EXP, PV_CURT, PV_CAR):
            A_eq[r, ix(t, k)] = 1.0
        b_eq[r] = pv[t]

        for k in (PV_LOAD, BAT_LOAD, GRID_LOAD):
            A_eq[r + 1, ix(t, k)] = 1.0
        b_eq[r + 1] = load[t]

        A_eq[r + 2, ix(t, SOC)] = 1.0
        A_eq[r + 2, ix(t, PV_BAT)] = -params.eta_pv_bat
        A_eq[r + 2, ix(t, GRID_BAT)] = -params.eta_grid_bat
        A_eq[r + 2, ix(t, BAT_LOAD)] = 1.0 / params.eta_discharge
        A_eq[r + 2, ix(t, BAT_CAR)] = 1.0 / params.eta_discharge
        if t == 0:
            b_eq[r + 2] = soc0_kwh
        else:
            A_eq[r + 2, ix(t - 1, SOC)] = -1.0

        A_eq[r + 3, ix(t, CAR)] = 1.0
        for k in (PV_CAR, BAT_CAR, GRID_CAR):
            A_eq[r + 3, ix(t, k)] = -car_eta
        if t == 0:
            b_eq[r + 3] = car.soc0_kwh if car else 0.0
        else:
            A_eq[r + 3, ix(t - 1, CAR)] = -1.0

        ub({(t, PV_BAT): params.eta_pv_bat, (t, GRID_BAT): params.eta_grid_bat}, params.max_power_kw * dt[t])
        ub({(t, BAT_LOAD): 1.0 / params.eta_discharge, (t, BAT_CAR): 1.0 / params.eta_discharge},
           params.max_power_kw * dt[t])
        if car:
            avail = 1.0 if car.available[t] else 0.0
            ub({(t, PV_CAR): 1.0, (t, BAT_CAR): 1.0, (t, GRID_CAR): 1.0}, car.max_kw * dt[t] * avail)
            if t in car.targets:
                # car_t + short_t >= target
                ub({(t, CAR): -1.0, (t, SHORT): -1.0}, -float(car.targets[t]))
        if fuse_amps is not None:
            three, one = 1.0 / (3 * PHASE_KV), 1.0 / PHASE_KV          # A per kW
            # never infeasible: the house load alone may exceed it (then car and battery get nothing)
            rhs = max(float(fuse_amps) * dt[t], load[t] * one)
            ub({(t, GRID_LOAD): one, (t, GRID_BAT): three, (t, GRID_CAR): three}, rhs)

    lo = min(params.soc_floor_kwh, soc0_kwh)
    hi = max(params.soc_ceiling_kwh, soc0_kwh)
    car_hi = max(car.max_kwh, car.soc0_kwh) if car else 0.0
    bounds = []
    for t in range(T):
        for k in range(NV):
            if k == SOC:
                bounds.append((lo, hi))
            elif k == GRID_BAT and not allow_grid_charge:
                bounds.append((0.0, 0.0))
            elif k == PV_EXP and export_min_price is not None and sell[t] < export_min_price:
                bounds.append((0.0, 0.0))
            elif k in (PV_CAR, BAT_CAR, GRID_CAR, SHORT) and not car:
                bounds.append((0.0, 0.0))
            elif k == BAT_CAR and not car.allow_battery:
                bounds.append((0.0, 0.0))
            elif k == CAR:
                bounds.append((0.0, car_hi) if car else (0.0, 0.0))
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
        # Among equally good options, discharge early rather than late (holding energy has no value
        # the model can see, and an early discharge leaves room for unforecast sun).
        c[ix(t, BAT_LOAD)] = -TIE_BREAK_DKK * (T - t)
        if car:
            c[ix(t, GRID_CAR)] = buy[t] + TIE_BREAK_DKK * (T - t)
            c[ix(t, SHORT)] = SHORTFALL_DKK_PER_KWH
    c[ix(T - 1, SOC)] -= terminal_value
    if car:
        # A kWh in the car is worth what it would cost at the plug: value / eta per stored kWh.
        c[ix(T - 1, CAR)] -= car.value_dkk_per_kwh / car.eta

    A_ub = np.array(ub_rows)
    b_ub = np.array(ub_rhs)
    res = linprog(c, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq, bounds=bounds, method="highs")
    if not res.success:
        raise RuntimeError(f"optimiser failed: {res.message}")

    x = np.clip(res.x.reshape(T, NV), 0.0, None)
    car_credit = (car.value_dkk_per_kwh / car.eta * car.soc0_kwh) if car else 0.0
    shortfall = float(x[:, SHORT].sum()) if car else 0.0
    return Plan(
        pv_load=x[:, PV_LOAD], pv_bat=x[:, PV_BAT], pv_exp=x[:, PV_EXP], pv_curt=x[:, PV_CURT],
        bat_load=x[:, BAT_LOAD], grid_load=x[:, GRID_LOAD], grid_bat=x[:, GRID_BAT],
        soc_end=x[:, SOC], buy=buy, sell=sell, pv=pv, load=load, dt=dt, soc0=soc0_kwh,
        # Net cost excludes the shortfall penalty (it is a modelling device, not money).
        net_cost=float(res.fun + terminal_value * soc0_kwh + car_credit - SHORTFALL_DKK_PER_KWH * shortfall),
        pv_car=x[:, PV_CAR], bat_car=x[:, BAT_CAR], grid_car=x[:, GRID_CAR], car_end=x[:, CAR],
        car_shortfall=shortfall,
    )
