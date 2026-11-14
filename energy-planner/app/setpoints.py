"""Translate the first steps of a plan into the register values the Growatt flow uses today.

Registers (names as in the Node-RED flow):
  battery first on/off, charge-from-grid on/off, battery-first max SoC (target),
  battery-first charge rate (% of inverter), load-first minimum SoC (discharge floor).

How the modes map onto the Growatt priorities (verify on the inverter before writing):
  grid_charge   battery first, charge from grid, max SoC = SoC after the last planned grid hour
  solar_charge  battery first, no grid charging, max SoC = SoC at the end of the charging run
  discharge     load first, min SoC = the SoC the plan expects at the end of the current step
  hold          battery first with max SoC = current SoC and no grid charging: the battery neither
                charges nor discharges, surplus sun is exported. (Load first with a raised floor is
                NOT a hold: in load first surplus sun still goes into the battery.)

Every value is chosen so that, if the planner stops refreshing it, the inverter stops at the
planned state instead of carrying on (no "charge from the grid to 100 %" left behind).

Export limit and the car-charging guard stay in Node-RED in this version; the export
decision is only reported as a hint.
"""
import math

CHARGE_KW_MIN = 0.05      # below this a step counts as "not charging"
GRID_CHARGE_KW_MIN = 0.1
DISCHARGE_KW_MIN = 0.05
EXPORT_KW_MIN = 0.05


def _kw(energy, dt):
    return float(energy) / max(float(dt), 1e-6)


def _rate_pct(kw: float, inverter_kw: float) -> int:
    """Charge rate register: % of the inverter, rounded up to 5, within 10..100."""
    rate = math.ceil(kw / inverter_kw * 100.0 / 5.0 - 1e-9) * 5
    return int(min(max(rate, 10), 100))


def step_mode(plan, j: int) -> str:
    """The mode of step j by the same thresholds the setpoints use (for the plan table and chart)."""
    dt = plan.dt[j]
    bat_car = plan.bat_car[j] if plan.bat_car is not None else 0.0
    if _kw(plan.grid_bat[j], dt) > GRID_CHARGE_KW_MIN:
        return "grid_charge"
    if _kw(plan.pv_bat[j], dt) > CHARGE_KW_MIN:
        return "solar_charge"
    if _kw(plan.bat_load[j] + bat_car, dt) > DISCHARGE_KW_MIN:
        return "discharge"
    return "hold"


def derive_setpoints(plan, soc_now_pct: float, battery, inverter_kw: float,
                     soc_floor_pct: float, soc_ceiling_pct: float) -> dict:
    T = len(plan)
    cap = battery.capacity_kwh

    def pct(kwh):
        return kwh / cap * 100.0

    def clamp(v):
        return int(min(max(v, soc_floor_pct), soc_ceiling_pct))

    def charging(j):
        return _kw(plan.pv_bat[j] + plan.grid_bat[j], plan.dt[j]) > CHARGE_KW_MIN

    def grid_charging(j):
        return _kw(plan.grid_bat[j], plan.dt[j]) > GRID_CHARGE_KW_MIN

    chg_pv = _kw(plan.pv_bat[0], plan.dt[0])
    chg_grid = _kw(plan.grid_bat[0], plan.dt[0])
    bat_car0 = plan.bat_car[0] if plan.bat_car is not None else 0.0
    dis = _kw(plan.bat_load[0] + bat_car0, plan.dt[0])   # the car counts: on the inverter it is house load
    exp = _kw(plan.pv_exp[0], plan.dt[0])

    grid_charge = grid_charging(0)
    solar_charge = chg_pv > CHARGE_KW_MIN

    if grid_charge:
        mode = "grid_charge"
    elif solar_charge:
        mode = "solar_charge"
    elif dis > DISCHARGE_KW_MIN:
        mode = "discharge"
    else:
        mode = "hold"

    soc_now_ceil = math.ceil(soc_now_pct - 1e-9)
    max_soc = None
    charge_rate = 100
    if mode in ("grid_charge", "solar_charge"):
        # End of the contiguous charging run that starts now ...
        j = 0
        while j + 1 < T and charging(j + 1):
            j += 1
        if mode == "grid_charge":
            # ... but grid charging only up to the SoC after the last planned grid hour in that run.
            # Charging that the plan leaves to the sun must not be done from the grid.
            last_grid = max(k for k in range(j + 1) if grid_charging(k))
            j = last_grid
            charge_rate = _rate_pct(chg_pv + chg_grid, inverter_kw)
        elif exp > EXPORT_KW_MIN:
            # The plan stores only part of the sun and sells the rest now: cap the rate so the
            # battery does not swallow the part meant for export.
            charge_rate = _rate_pct(chg_pv, inverter_kw)
        max_soc = clamp(max(math.ceil(pct(plan.soc_end[j]) - 1e-9), soc_now_ceil))
    elif mode == "hold":
        max_soc = clamp(soc_now_ceil)

    if mode == "discharge":
        # Floor at the SoC the plan expects after this step (rounded down so the plan is reachable).
        # If the load is higher than forecast the battery stops there and the next replan decides.
        min_soc = clamp(math.floor(pct(plan.soc_end[0]) + 1e-9))
    else:
        min_soc = clamp(soc_now_ceil)

    return {
        "mode": mode,
        "battery_first": mode != "discharge",
        "grid_charge": bool(grid_charge),
        "max_soc_pct": max_soc,
        "charge_rate_pct": charge_rate,
        "min_soc_pct": min_soc,
        "export_hint": "export" if exp > EXPORT_KW_MIN else "none",
        "charge_kw": round(chg_pv + chg_grid, 2),
        "grid_charge_kw": round(chg_grid, 2),
        "discharge_kw": round(dis, 2),
    }
