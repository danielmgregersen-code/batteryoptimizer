# Energy Planner — Home Assistant add-on

Plans a Growatt hybrid inverter + battery **hour by hour**: when to charge from the grid, when to
store solar instead of selling it, when to discharge, and when to charge the car. It replaces the threshold ladders in the
Node-RED flow (forecast ≥ 10 / 7 / 4 kWh → SoC 10 / 30 / 45 %) with one optimisation over the next
24-48 hours.

**Dry-run by default.** It reads Home Assistant, computes a plan, publishes it as sensors and logs
it next to what Node-RED and the car automation actually did. Two HA helpers switch each part to
production (see *Production* below); while a helper is missing or off, that part writes nothing.

## Production

Create two helpers (Settings → Devices & services → Helpers → Toggle):

| Helper | When on |
| --- | --- |
| `input_boolean.energy_planner_produktion` | Every planning cycle (15 min) the add-on writes the house-battery registers: battery first (the all-day slot, Enabled/Disabled), charge from grid, max SoC, load-first min SoC and charge rate. Only values that differ are written; there is no rate limit. Time slots and the export limit are never written. |
| `input_boolean.energy_planner_bil_produktion` | Every car cycle (2 min) the add-on carries out the car controller's calls: charger current, the start button and `kia_uvo.start_charge` / `stop_charge` (needs `car_device_id`). The same Kia call is not repeated within 10 minutes while the charger status is unchanged (Kia's cloud quota). |

- Write order: what goes off first (charge from grid before battery first), then the limits
  (min/max SoC so that min never exceeds max, charge rate), then what goes on.
- Values are clipped (min SoC ≥ `soc_floor_pct`, max SoC ≥ min SoC, charge rate 10–100) and read
  back; the panel shows "Skrevet"/"Sendt"/"Fejl" per register. A failed planning cycle writes nothing.
- While the battery helper is on, a manual change to a register is overwritten at the next cycle:
  switch the helper off to take over by hand. If the add-on stops, the registers keep the last
  values, which are chosen to stop at the planned state (no grid charging left running).
- The add-on does not touch Node-RED: stop Node-RED's night-charging and SoC branches (and the car
  automation) yourself when you switch the helpers on.
- The services the code can call are limited by an allowlist in `homeassistant.py`: `select` /
  `number` / `button` only on the registers above and the charger's current and start button,
  `kia_uvo.start_charge` / `stop_charge`, and Solcast's dampening services.
- Before switching the battery on, check once on the inverter that `hold` (battery first with
  max SoC = current SoC) neither charges nor discharges the battery.

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

## The panel

Opens from the sidebar (*Energy Planner*), through ingress.

- **Plan**: what the planner would do now and over the horizon. Chart: prices (estimated hours
  hatched), energy per hour (charging above the line, discharging and export below, the mode as a
  strip on top, the car's ~11 kWh bars cut with a break mark so the house flows stay readable) and
  the SoC of the house battery and the car. Hover or use the arrow keys for every value of an hour.
  "Planner mod inverteren nu" shows each register the planner would set next to its current value.
- **Bil**: the car controller's decision and the calls it would make, the car plan, and its log.
- **Økonomi**: what the installation cost and earned, from HA's long-term statistics (so with history
  from before the add-on): today / 7 / 30 / 90 days / 1 year. System value (what the panels and
  battery saved compared with buying everything), split into the sun's and the battery's part;
  net cost, bought and sold for; the car at the buy price; self-sufficiency. Charts per hour (up to
  7 days) or day: system value (sun + battery, the battery negative when it charges from the grid),
  money in and out, cumulative value, energy; and a table per day. Formulas in `app/economy.py`.
- **Evaluering**: what actually happened over 7/14/30 days. Daily grid cost, price paid per kWh,
  how often the planner agreed with Node-RED (a strip of both over time), solar actual vs. forecast
  with the least-squares best `pv_p10_weight`, consumption actual vs. forecast.

To work on the panel without Home Assistant: `python tools/dev_server.py` and open
`http://127.0.0.1:8010/` (real 2026-11-14 prices and forecast, a connected car, 14 days of
synthetic log).

## Inputs

| What | Source (default entity) |
| --- | --- |
| Buy / sell price per hour | `sensor.kob_af_el`, `sensor.salg_af_el` (`raw_today`, `raw_tomorrow`) |
| Solar forecast per hour | Solcast `detailedHourly` of `…forecast_today`, `_tomorrow`, `_day_3` — p50 blended with p10 (`pv_p10_weight`) |
| Battery SoC | `sensor.growatt_battery_soc` |
| Base load | Hourly means of `sensor.growatt_total_house_load` **minus** EV charging (`car_power_entity`, default `sensor.ioniq_9_ladeeffekt`; hours without its statistics and above `car_detect_kw` are left out as car charging) (and minus grid charging of the battery if the sensor includes it), trimmed mean per (weekday/weekend, hour) over `profile_days` |

Until statistics are available (or if the query fails) it plans with `default_load_kw` and says so in
the UI.

If `car_power_entity` has no long-term statistics for an hour, the car cannot be subtracted; such
hours with more than `car_detect_kw` (4 kW) of load are taken to be car charging and left out, so the
car's night charging does not become 3-4 kW of base load.

On the SPH10000TL3 the house load does not include the battery's charging, so
`load_includes_battery_charging` defaults to `no`. For integrations that do count it, `auto` checks
the statistics (only when the car's statistics exist): at night (no sun, car not charging) it compares the base load in hours where the
battery charges with hours where it is idle. If the difference is more than half the charging
power, the grid part of the charging (`battery charge - solar`) is subtracted from the profile.
`load_includes_battery_charging` (`auto` / `yes` / `no`) overrides it. The Plan tab's
"Forbrugsprofil" card shows the profile and what was found.

## Solar correction (trees and other shading)

Solcast does not know about the trees. The add-on learns, per hour of the day, how much of the
forecast actually arrives, from Solcast's forecast (recorded every cycle in `/data/pv_history.json`)
and the actual production (`pv_power_entity` statistics), over the last `pv_dampening_days` (14).
At start it also fetches Solcast's own 14 days of undampened forecast history
(`solcast_solar.query_forecast_data`), so it can learn right away; its own recordings take precedence.

Shading only takes the direct sun, so the correction depends on the clouds:

    factor = 1 - shade[hour] x clearness        clearness = Solcast p50 / clear-sky p50 for that hour

On a clear day a shaded hour gets the full correction; on an overcast day almost none. The clear-sky
reference is the highest p90 Solcast gave for that hour in the last three weeks, so Solcast's own
forecast serves as the cloud forecast (no weather integration needed). `shade` is fitted by least
squares, which weights the clear hours most. Hours with the export limit at 0 (panels throttled) and
hours at the inverter's limit are left out.

- `pv_dampening`: `auto` (learn, plus the manual hours), `manual` (only the manual hours) or `off`.
- `pv_dampening_manual`: fixed clear-sky factors, e.g. `14:0.3, 15:0.2`.
- `solcast_push: true` also sets the factors in the Solcast integration (`solcast_solar.set_dampening`)
  once a day, or sooner if they move a lot, so HA's own Solcast sensors match. Solcast only takes one
  factor per hour, so it gets today's (from today's clearness); the planner itself corrects every
  hour of the horizon with its own clearness. The add-on knows what it has set and learns against
  the undampened forecast, so nothing is dampened twice.
- If Solcast's own *automated dampening* is enabled, the add-on uses Solcast's corrected forecast
  and pauses its own correction (the two cannot both set the dampening).

The Plan tab's "Solkorrektion" card shows Solcast, actual and corrected per hour, the factors, and
what is set in Solcast.

## Dashboard

`dashboards/solceller.yaml` is a ready-made Solceller view (sections layout, needs apexcharts-card):
the planner's status and next action, live tiles, solar today vs. forecast, price / planned energy /
planned SoC for the next 30 hours, cost today, "Kommende", the planner vs. register table, the car,
and the register controls. It is one view: paste it in *Edit view → ⋮ → Edit in YAML* (not the raw
configuration editor, which expects a whole dashboard). `dashboards/solceller-dashboard.yaml` is the
same as a complete dashboard for the raw configuration editor of a new dashboard. Solcast `detailedHourly` values are average kW over the hour, i.e. kWh per hour.

## Outputs

Non-persistent sensors, rewritten every cycle:

| Entity | Meaning |
| --- | --- |
| `sensor.energy_planner_mode` | `grid_charge`, `solar_charge`, `discharge` or `hold` for the current hour. Attributes: all setpoints, saving vs. solar-only, and in Danish `label`, `detail`, `summary`, `next_action`, `next_change`, `upcoming` (list of `label` / `when` / `text`) |
| `sensor.energy_planner_soc_target` | Battery-first max SoC: charging target, or the current SoC in `hold`; `unknown` while discharging |
| `sensor.energy_planner_soc_floor` | Load-first minimum SoC: while discharging, the configured floor plus the reserve for later hours that are dearer than now; otherwise the current SoC |
| `sensor.energy_planner_charge_rate` | Battery-first charge rate in % of the inverter |
| `sensor.energy_planner_plan` | Plan table as an attribute (`plan` / `columns`, incl. car energy and SoC) — usable from an ApexCharts card |
| `sensor.energy_planner_car` | What the car controller would do now. Attributes: `label`, `target_amps`, `reason` / `reason_da`, `calls`, `next_window`, `next_start`, `planned_kwh`, `deadline` |
| `sensor.energy_planner_pv_dampening` | Mean solar correction factor in daylight hours. Attributes: `factors` / `factors_csv` (today's 24 factors, as sent to Solcast), `clear_sky_factors`, `manual_hours`, `applied_in_solcast`, `last_pushed`, `solcast_auto` |
| `sensor.energy_planner_cost_today` | Realised grid cost today in DKK. Attributes: `import_kwh`, `export_kwh`, `avg_buy_paid`, `avg_buy_market`, `agreement_pct`, `yesterday_dkk` |

The sidebar panel shows the same plan hour by hour, plus the register values currently in use.

### How the modes map onto the inverter

| Mode | Battery first | Grid charge | Max SoC | Min SoC |
| --- | --- | --- | --- | --- |
| `grid_charge` | on | on | SoC after the last planned grid hour | current |
| `solar_charge` | on | off | SoC at the end of the charging run (rate capped if part of the sun is sold) | current |
| `discharge` | off (load first) | off | – | `soc_floor_pct` + what later, dearer hours need before the next charge (the configured floor in the dearest hour) |
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
python tools/dev_server.py             # the panel against a fake Home Assistant
```
