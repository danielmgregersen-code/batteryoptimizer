# Energy Planner — Home Assistant add-on

Plans a Growatt hybrid inverter + battery **hour by hour**: when to charge from the grid, when to
store solar instead of selling it, when to discharge. It replaces the threshold ladders in the
Node-RED flow (forecast ≥ 10 / 7 / 4 kWh → SoC 10 / 30 / 45 %) with one optimisation over the next
24-48 hours.

**Version 0.1 is dry-run.** It reads Home Assistant, computes a plan, publishes it as sensors and
logs it next to what Node-RED actually did. It does not write to the inverter.

## How it decides

Every 15 minutes (and whenever you press *Kør planlægning nu*) it solves a linear program over the
hours for which both a buy and a sell price exist (today, and tomorrow once the prices are out):

- Minimise `buy × import − sell × export + battery wear`, with leftover battery energy credited at
  what it would cost to buy back at the cheapest known hour.
- Battery limits: capacity, max charge/discharge power, SoC floor and ceiling, and separate losses for
  solar → battery, grid → battery and battery → house.
- The battery only serves the house. It is never sold to the grid (no arbitrage in this version).

Consequences that fall out of the numbers rather than out of rules:

- If selling solar pays more than charging from the grid at night (after losses and wear), the solar
  is sold and the battery is filled at night.
- If selling pays little, the solar is stored for the evening peak.
- A cheap hour before the peak is used for grid charging only if the spread covers the losses and wear.

## Inputs

| What | Source (default entity) |
| --- | --- |
| Buy / sell price per hour | `sensor.kob_af_el`, `sensor.salg_af_el` (`raw_today`, `raw_tomorrow`) |
| Solar forecast per hour | Solcast `detailedHourly` of `…forecast_today`, `_tomorrow`, `_day_3` — p50 blended with p10 (`pv_p10_weight`) |
| Battery SoC | `sensor.growatt_battery_soc` |
| Base load | Hourly means of `sensor.growatt_total_house_load` **minus** EV charging power, median per (weekday/weekend, hour) over `profile_days` |

Until statistics are available (or if the query fails) it plans with `default_load_kw` and says so in
the UI. Solcast `detailedHourly` values are average kW over the hour, i.e. kWh per hour.

## Outputs

Non-persistent sensors, rewritten every cycle:

| Entity | Meaning |
| --- | --- |
| `sensor.energy_planner_mode` | `grid_charge`, `solar_charge`, `discharge` or `hold` for the current hour (attributes: all setpoints, saving vs. solar-only) |
| `sensor.energy_planner_soc_target` | Battery-first max SoC for the current charging run (only while charging) |
| `sensor.energy_planner_soc_floor` | Load-first minimum SoC: the configured floor while discharging, the current SoC to hold the battery |
| `sensor.energy_planner_charge_rate` | Battery-first charge rate in % of the inverter |
| `sensor.energy_planner_plan` | Plan table as an attribute (`plan` / `columns`) — usable from an ApexCharts card |

The sidebar panel shows the same plan hour by hour, plus the register values currently in use.

## Dry-run checklist

1. Let it run for a week or two.
2. Compare `/api/decisions` (also `/data/decisions.jsonl`): the planned mode and setpoints versus
   `actual_registers`. Look at the hours where they disagree and decide who was right.
3. Only then move the night-charging and SoC-target branches out of Node-RED.

When the planner takes over, the Node-RED car guard must restore the **planner's floor**
(`sensor.energy_planner_soc_floor`) instead of a fixed 10 % when the house load drops below 4 kW.

## Not in this version

- Writing to the inverter, and the export limit. The export decision is only reported as `export_hint`.
- The car. Charging is modelled as outside the plan (manual charging is simply extra load that is
  removed from the base-load profile). Solar-surplus charging stays in the charger's PV-linkage mode.
- Temperature dependence of the heat pump (it is part of the base load for now).
- Selling battery energy to the grid.

## Assumptions to check

These are starting values in the add-on options, not measurements:

- Efficiencies: solar → battery 0.97, grid → battery 0.94, battery → house 0.95. The Growatt battery
  in/out counters appear to measure at the battery, so they cannot give the inverter losses.
- Wear cost 0.25 DKK per kWh charged.
- `charge_rate` is treated as a percentage of the inverter's 10 kW (`inverter_kw`).

## Development

```
cd energy-planner/tests && python -m pytest -q
python tools/simulate.py --shaped      # optimiser on real prices/forecast, no Home Assistant needed
```
