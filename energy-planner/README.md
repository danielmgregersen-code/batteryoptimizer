# Energy Planner — Home Assistant add-on

Plans a Growatt hybrid inverter + battery **hour by hour**: when to charge from the grid, when to
store solar instead of selling it, when to discharge, and when to charge the car. It replaces the threshold ladders in the
Node-RED flow (forecast ≥ 10 / 7 / 4 kWh → SoC 10 / 30 / 45 %) with one optimisation over the next
24-48 hours.

**This version is dry-run.** It reads Home Assistant, computes a plan, publishes it as sensors and
logs it next to what Node-RED and the car automation actually did. It does not write to the
inverter or the charger, and the code has no way to call a service.

## How it decides

Every 15 minutes (and whenever you press *Kør planlægning nu*) it solves a linear program over the
hours for which both a buy and a sell price exist (today, and tomorrow once the prices are out).
Before tomorrow's prices are published the horizon is extended to `min_horizon_hours` with
**estimated** prices: the last known day's price at the same local hour (shown in italics).

- Minimise `buy × import − sell × export + battery wear`, with leftover battery energy credited at
  the cheapest price in the horizon × the discharge efficiency (what a stored kWh can at least save).
- Battery limits: capacity, max charge/discharge power, SoC floor and ceiling, and separate losses for
  solar → battery, grid → battery and battery → house.
- The battery only serves the house. It is never sold to the grid (no arbitrage in this version).

Consequences that fall out of the numbers rather than out of rules:

- If selling solar pays more than charging from the grid at night (after losses and wear), the solar
  is sold and the battery is filled at night.
- If selling pays little, the solar is stored for the evening peak.
- A cheap hour before the peak is used for grid charging only if the spread covers the losses and wear.

## The car

Replaces the HA automation *"Solcelle- & Prisstyret ladning: IONIQ 9"* in two layers.

**In the plan** (every planning cycle), while `charger_status_entity` is `preparing`, `charging`,
`suspended_ev`, `suspended_evse` or `finishing`:

- The car is assumed connected until the next `car_deadline_hour` (06:00).
- Below `car_min_start_pct` (40 %; 42 % while charging) it must reach `car_min_stop_pct` (42 %)
  by the deadline: the cheapest hours before 06:00 are used, the night's prices estimated if
  they are not out yet. Below `car_critical_start_pct` (20 %) it charges straight to 22 %.
- Beyond that it is charged up to `car_max_soc_pct` (90 %) only from energy that costs less than
  `car_value_dkk_per_kwh` (1.25) at the plug: sun that sells for less, or grid hours that cost less.
  This one number replaces the automation's three price rules (sun if sale ≤ 0.50, full power if
  sale < 0.10, winter grid if buy ≤ 1.25). Note that it is more willing to put sun into the car
  than the 0.50 rule: with one value per kWh, sun selling at 1.05 is cheaper than grid at 1.25.
- The house battery is not used for the car in the plan (`car_from_house_battery: false`).

**The controller** (every `car_interval_seconds`) keeps the automation's mechanics: amps 6-16,
start button from `preparing`, `kia_uvo.start_charge` / `stop_charge` to resume/pause with
`car_min_charge_s` and `car_cooldown_s`, solar-surplus following with `car_start_surplus_w`,
`car_stop_surplus_w`/`car_buffer_soc` and the house-battery reservation below
`car_house_priority_soc`. What to do comes from the plan for the current hour:

| Situation | Controller |
| --- | --- |
| car ≥ max SoC | pause |
| below critical SoC | charge at max amps |
| plan has grid power for the car this hour | charge at the planned power (≥ 6 A) |
| plan has sun for the car, or the sun sells for less than the car value now | follow the actual surplus |
| no plan available and below the minimum | charge at max amps (fallback) |
| otherwise | pause |

`sensor.energy_planner_car` shows the decision (`charge` / `pause` / `idle` / `none`) with
`target_amps`, `reason` and the service calls it would make. `/api/car` has the log of changes.

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
| `sensor.energy_planner_soc_target` | Battery-first max SoC: charging target, or the current SoC in `hold`; `unknown` while discharging |
| `sensor.energy_planner_soc_floor` | Load-first minimum SoC: the SoC planned at the end of the current step while discharging, otherwise the current SoC |
| `sensor.energy_planner_charge_rate` | Battery-first charge rate in % of the inverter |
| `sensor.energy_planner_plan` | Plan table as an attribute (`plan` / `columns`, incl. car energy and SoC) — usable from an ApexCharts card |
| `sensor.energy_planner_car` | What the car controller would do now (attributes: `target_amps`, `reason`, `calls`) |

The sidebar panel shows the same plan hour by hour, plus the register values currently in use.

### How the modes map onto the inverter

| Mode | Battery first | Grid charge | Max SoC | Min SoC |
| --- | --- | --- | --- | --- |
| `grid_charge` | on | on | SoC after the last planned grid hour | current |
| `solar_charge` | on | off | SoC at the end of the charging run (rate capped if part of the sun is sold) | current |
| `discharge` | off (load first) | off | – | SoC planned after this step |
| `hold` | on | off | current SoC | current |

`hold` relies on battery first not discharging and stopping at max SoC, with surplus sun going to
export. **Verify that on the SPH10000TL3 before anything is written.** Every value is chosen so a
planner that stops refreshing leaves the inverter at the planned state, not charging on.

## Dry-run checklist

1. Let it run for a week or two.
2. Compare `/api/decisions` (also `/data/decisions.jsonl`): the planned mode and setpoints versus
   `actual_registers`. Look at the hours where they disagree and decide who was right.
   `/api/evaluation` gives the realised grid cost per day from the logged counters and prices.
3. Check the entity warnings in the panel: every configured entity must exist.
4. Only then move the night-charging and SoC-target branches out of Node-RED.

When the planner takes over, the Node-RED car guard must restore the **planner's floor**
(`sensor.energy_planner_soc_floor`) instead of a fixed 10 % when the house load drops below 4 kW.

## Not in this version

- Writing to the inverter, and the export limit. The export decision is only reported as `export_hint`.
- Calling the charger or the car. The car plan and controller are dry-run like the rest.
- Knowing when the car leaves: it is assumed connected until the next deadline.
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
