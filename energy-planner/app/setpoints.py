"""Translate the first steps of a plan into the register values the Growatt flow uses today.

Registers (names as in the Node-RED flow):
  battery first on/off, charge-from-grid on/off, battery-first max SoC (target),
  battery-first charge rate (% of inverter), load-first minimum SoC (discharge floor).
Export limit and the car-charging guard stay in Node-RED in this version; the export
decision is only reported as a hint.
"""
import math

CHARGE_KW_MIN = 0.05      # below this a step counts as "not charging"
GRID_CHARGE_KW_MIN = 0.1
DISCHARGE_KW_MIN = 0.05


def _kw(energy, dt):
    return float(energy) / max(float(dt), 1e-6)


def derive_setpoints(plan, soc_now_pct: float, battery, inverter_kw: float,
                     soc_floor_pct: float, soc_ceiling_pct: float) -> dict:
    T = len(plan)

    def charging(j):
        return _kw(plan.pv_bat[j] + plan.grid_bat[j], plan.dt[j]) > CHARGE_KW_MIN

    chg_pv = _kw(plan.pv_bat[0], plan.dt[0])
    chg_grid = _kw(plan.grid_bat[0], plan.dt[0])
    dis = _kw(plan.bat_load[0], plan.dt[0])
    exp = _kw(plan.pv_exp[0], plan.dt[0])

    grid_charge = chg_grid > GRID_CHARGE_KW_MIN
    solar_charge = chg_pv > CHARGE_KW_MIN
    is_charging = grid_charge or solar_charge

    if grid_charge:
        mode = "grid_charge"
    elif solar_charge:
        mode = "solar_charge"
    elif dis > DISCHARGE_KW_MIN:
        mode = "discharge"
    else:
        mode = "hold"

    max_soc = None
    if is_charging:
        j = 0
        while j + 1 < T and charging(j + 1):
            j += 1
        pct = math.ceil(plan.soc_end[j] / battery.capacity_kwh * 100.0 - 1e-9)
        max_soc = int(min(max(pct, soc_floor_pct), soc_ceiling_pct))

    if grid_charge:
        total_kw = chg_pv + chg_grid
        rate = math.ceil(total_kw / inverter_kw * 100.0 / 5.0) * 5
        charge_rate = int(min(max(rate, 10), 100))
    else:
        charge_rate = 100

    if mode == "discharge":
        min_soc = int(soc_floor_pct)
    else:
        min_soc = int(min(max(math.ceil(soc_now_pct - 1e-9), soc_floor_pct), soc_ceiling_pct))

    return {
        "mode": mode,
        "battery_first": bool(is_charging),
        "grid_charge": bool(grid_charge),
        "max_soc_pct": max_soc,
        "charge_rate_pct": charge_rate,
        "min_soc_pct": min_soc,
        "export_hint": "export" if exp > 0.05 else "none",
        "charge_kw": round(chg_pv + chg_grid, 2),
        "grid_charge_kw": round(chg_grid, 2),
        "discharge_kw": round(dis, 2),
    }
