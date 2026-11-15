# Changelog

## 0.7.0

The car: an occasional 100 % at rock-bottom prices, and trips.

- **100 % when power is at rock bottom** (`car_full_charge`): only if the last full charge is at
  least `car_full_min_days` (14) ago, the car is connected, and either a sell price before the
  deadline is negative (sun on a summer day) or the cheapest hours needed for 100 % average among
  the `car_full_cheap_percentile` (5) % cheapest hourly prices of the last 30 days. Never forced,
  however long ago the last full charge was. The last full charge comes from the car SoC
  statistics and from what the add-on sees (`/data/car_full.json`). Charged as late as possible
  before the deadline so the car does not sit at 100 % for long.
- **Trips**: set `input_datetime.ioniq9_afgang` (date and time) and optionally
  `input_number.ioniq9_afgang_soc` (default `car_departure_soc`, 100 %). Once the departure is within
  the plan's horizon, the car is planned to that SoC by then in the cheapest hours, and is assumed
  connected until it leaves. The daily 42 % by 06:00 still applies.
- The controller stops at the effective limit (90 %, or 100 % / the trip's SoC).
- Car tab: "100 %-ladning" and "Afgang" tiles; `sensor.energy_planner_car` gets `last_full`,
  `days_since_full`, `full_charge`, `departure`, `departure_soc`, `departure_ok`, `max_soc_pct`.

## 0.6.1

Car production: safe when the car cannot be paused through Kia.

- Kia calls go over the WebSocket API, so a failure carries HA's message (e.g. "PIN verification
  failed") instead of "500 Internal Server Error".
- PIN / access errors: no new Kia calls for 6 hours or until the car helper is switched off and on
  (repeated rejected PIN attempts can lock Hyundai's remote control). Other errors back off
  2 / 4 / 8 / 15 minutes instead of retrying every 2 minutes. A missing `car_device_id` counts as a
  failure too.
- While the car cannot be paused (failed, blocked or ignored stop): the charger is set to 6 A.
- Emergency stop on the charger (`charger_stop_button`, `car_thor_stop_fallback: true`): only while
  charging from the grid (not on sun), when Kia has given up (PIN/access error, two failures, or a
  stop the car ignored for 10 minutes) and nothing more is planned before the deadline or the car
  is at its max SoC. The charger cannot be restarted from the add-on afterwards: re-plug the cable.
- The warning is shown at the top of the panel and as `warning` on `sensor.energy_planner_car`; the
  car log shows every executed call with its result.

## 0.6.0

**Production.** Two HA helpers switch the add-on from dry-run to control; missing or off = dry-run.

- `input_boolean.energy_planner_produktion`: writes the house-battery registers every cycle (battery
  first all-day slot, charge from grid, max SoC, min SoC, charge rate). Only differing values, no
  rate limit, in a safe order; clipped to hard limits; read back. Export limit and time slots are
  never written. Manual changes are overwritten at the next cycle while the helper is on.
- `input_boolean.energy_planner_bil_produktion`: carries out the car controller's calls (charger
  current, start button, Kia start/stop with the new `car_device_id`). A repeated Kia call within
  10 minutes with unchanged charger status is skipped (cloud quota).
- Service allowlist per entity: `select` / `number` / `button` only on those registers and the
  charger's controls.
- Panel: the header pill shows "Produktion: batteri + bil", the register table shows what was
  written, the car tab shows the executed calls. Sensors' `dry_run` attribute follows the helpers.

## 0.5.0

- **New "Økonomi" tab**: what the installation cost and earned, hour by hour from HA's long-term
  statistics of the energy counters and the price sensors, so it has history from before the add-on.
  Periods today / 7 / 30 / 90 days / 1 year.
  - Tiles: system value (saved compared with buying everything from the grid), net cost, the sun's
    and the battery's contribution, sold for, bought for (price paid vs. average), the car at the
    buy price, self-sufficiency.
  - Charts: system value per hour/day stacked sun + battery (negative while charging from the grid),
    money in and out, cumulative system value, energy (solar, bought, sold); table per day.
  - Hours without price statistics fall back to the prices in the add-on's log; missing entities and
    skipped hours are shown.
- `GET /api/economy?days=N` (cached 10 minutes). The statistics websocket accepts large responses.

## 0.4.2

- **No SoC floor in the dearest hours.** While discharging, the load-first minimum SoC was the SoC
  the plan expected at the end of the hour (since 0.1.1), so even in the peak hour a load above the
  forecast would have been bought at the peak price instead of taken from the battery. The floor is
  now the configured `soc_floor_pct` plus only what later hours that are *dearer* than now need from
  the battery before it is charged again. In the dearest hour it is the configured floor (10 %).

## 0.4.1

- **The solar correction learns right away.** At start the add-on fetches Solcast's own 14 days of
  undampened forecast history (`solcast_solar.query_forecast_data`) and learns from it together with
  the actual production already in HA's statistics, instead of recording forecasts for two weeks
  first. Its own recordings take precedence; hours the decision log saw with the export limit at 0
  are marked as throttled. A failed fetch (older integration, unexpected format) is shown in the
  "Solkorrektion" card and retried after 6 hours; planning carries on.
- `query_forecast_data` (read-only) joins the allowlist of services the add-on may call.

## 0.4.0

Solar correction for shading (trees).

- Learns per hour of the day how much of Solcast's forecast actually arrives (14 days of recorded
  forecasts vs. actual production). The correction scales with the clouds:
  `factor = 1 - shade[hour] x clearness`, with clearness from Solcast's own forecast relative to
  the clear-sky level of that hour, so overcast hours are hardly dampened. Export-limited and
  clipped hours are left out.
- The planner uses the corrected forecast (p50, p10 and p90).
- `solcast_push: true` sets the factors in the Solcast integration daily (`set_dampening`), so HA's
  Solcast sensors match. Learning uses the undampened forecast, so nothing is dampened twice.
  Solcast's own automated dampening, if enabled, takes precedence and the add-on's pauses.
- Manual hours: `pv_dampening_manual: "14:0.3, 15:0.2"`; `pv_dampening: auto / manual / off`.
- New `sensor.energy_planner_pv_dampening`, "Solkorrektion" card in the panel, correction factor in
  the plan chart's tooltip.
- The only services the add-on can call are Solcast's dampening services (allowlist).

## 0.3.6

- `car_power_entity` and `charger_power_entity` default to `sensor.ioniq_9_ladeeffekt` (W,
  measurement, long history; now a mirror of the THOR's charging power). The car is subtracted from
  the base-load profile hour by hour again; the 4 kW rule of thumb only covers hours the sensor
  has no statistics for.

## 0.3.5

- `car_power_entity` back to the THOR's charging power: `sensor.ioniq_9_ladeeffekt` changed meaning
  over time (first an estimate, now a reading of the charger) and is not a sound history.
- **Hours without car statistics:** a base load above `car_detect_kw` (4 kW) is taken to be the car
  charging and the hour is left out of the profile, instead of being counted as house load. Once the
  THOR's power has long-term statistics (`state_class: measurement`) it is used hour by hour; hours
  it covers are never left out by the rule of thumb.
- The panel's "Forbrugsprofil" card shows how many hours were subtracted and left out.

## 0.3.4

- **The car is now subtracted from the base-load profile.** The default car entity
  (`sensor.garage_growatt_thor_ev_charger_charging_power`) has no long-term statistics, so the car's
  night charging was counted as 3-4 kW of house load. `car_power_entity` now defaults to
  `sensor.ioniq_9_ladeeffekt`, which has statistics.
- `car_power_entity` may be a power sensor (W/kW, hourly mean) or an energy counter (kWh,
  total/total_increasing: hourly change = average kW); the panel shows which was used.
- New `charger_power_entity` (default the THOR's live charging power) for the car controller and
  the evaluation samples, which need the live value rather than statistics.

## 0.3.3

- `load_includes_battery_charging` defaults to `no`: on the SPH10000TL3 the house load does not
  include the battery's charging (it has its own sensor). `auto` remains available for other setups.
- The `auto` detection only runs when the car charger has long-term statistics. Without them the
  car's night charging (same cheapest hours as the battery) looked like battery charging in the
  house load and could be subtracted by mistake.
- Missing car-charger statistics is now a warning at the top of the panel: it is the likely reason
  for a night-time base load of 3-4 kW (the car is then counted as house load).

## 0.3.2

- **Night-time house load too high.** Some Growatt integrations count the battery's charging as
  house load, so Node-RED's night charging (02-05, ~4 kW) ended up in the base-load profile. The
  add-on now detects it from the statistics (night hours with and without battery charging) and
  subtracts the grid charging (`battery charge - solar`) from the profile. Override with
  `load_includes_battery_charging: yes / no` (default `auto`).
- Statistics are requested in W, so a charger reporting kW is subtracted correctly.
- Plan tab: new "Forbrugsprofil" card with the profile per hour (weekday/weekend) and what was
  found: whether the car is subtracted, and whether the house load includes battery charging.
- `dashboards/solceller.yaml`: a complete Solceller dashboard view (sections) built on the
  planner's sensors and apexcharts-card.

## 0.3.1

Dashboard-friendly sensors.

- `sensor.energy_planner_mode` gets Danish attributes: `label`, `detail`, `summary` (one sentence:
  now and next), `next_action`, `next_change` and `upcoming` (the next six actions with time and
  explanation). The panel's "Kommende" list now comes from the same code.
- `sensor.energy_planner_car` gets `label`, `reason_da`, `next_window` (e.g. "i morgen 04:00–06:00 ·
  14,3 kWh, à 1,94 kr · til 42 %"), `next_start`, `planned_kwh` and `deadline`.
- New `sensor.energy_planner_cost_today`: realised grid cost so far today (DKK), with import/export,
  price paid vs. average, agreement with Node-RED and yesterday's total as attributes.

## 0.3.0

New panel (still dry-run).

- **Plan** tab: status tiles (mode now, house battery with target/floor, price now, saving vs.
  solar-only, car), a chart of the whole horizon in three aligned panels (prices with the estimated
  hours hatched, energy per hour with the mode strip, battery and car SoC) with one crosshair and a
  tooltip per hour, a "coming up" list of the planned actions in plain language, a table of what the
  planner would set next to what the inverter registers say now, and the hourly table.
- **Bil** tab: car SoC and charger status, what the controller would do and which calls it would
  make, the car's planned charging against the price, and the controller's log.
- **Evaluering** tab (7 / 14 / 30 days): realised grid cost per day, price paid per kWh against the
  period average, agreement with Node-RED over time (planner vs. registers strip), solar actual vs.
  forecast with a suggested `pv_p10_weight`, consumption actual vs. forecast, and the recent decisions.
- Decision log also records the forecasts for the current hour and load/car samples;
  `/api/evaluation?days=N` returns days, summary and timeline.
- Light and dark theme, phone layout, keyboard support on the charts. No external scripts: the panel
  works without internet access.
- `tools/dev_server.py` runs the panel against a fake Home Assistant for UI work.

## 0.2.0

The car is part of the plan (still dry-run: nothing is written to the inverter or the charger).

- **Car in the optimisation.** While the THOR reports the car as connected, the Ioniq 9 is a
  flexible load in the LP: it must reach 42 % by 06:00 if it is below 40 %, goes straight to 22 %
  below 20 %, stops at 90 %, and is charged beyond the minimum only from energy that costs less
  than `car_value_dkk_per_kwh` at the plug (sun that sells for less, or a cheap grid hour). This
  replaces the fixed price rules of the HA automation "Solcelle- & Prisstyret ladning: IONIQ 9".
  The house battery does not feed the car in the plan unless `car_from_house_battery` is on.
- **Car controller** (every `car_interval_seconds`, 120 s): the automation's mechanics (6-16 A,
  start button from `preparing`, pause/resume through `kia_uvo` with minimum charge time and
  cooldown, solar-surplus following with the same thresholds), but driven by the plan for the
  current hour. Publishes `sensor.energy_planner_car` (what it *would* do, with the service calls
  it would make) and logs changes to `/data/car_decisions.jsonl` (`/api/car`) next to the
  charger's actual status and current.
- Plan table, `sensor.energy_planner_plan` and the panel show the car's planned energy and SoC.
- `config.yaml` options are checked against the defaults in a test.

## 0.1.1

Fixes from the first review (still dry-run):

- **Leftover battery energy** is now valued at the cheapest price in the horizon × the discharge
  efficiency. 0.1.0 used the buy-back cost, which is more than any use of the energy can return,
  so the battery was held through moderately priced hours.
- **Estimated prices beyond the published ones**: until tomorrow's prices are out (~13:00) the
  horizon is extended to `min_horizon_hours` (30) with the last known day's price at the same
  local hour. Without this the plan ended at midnight and could not weigh today's sun against
  tonight's grid price. Estimated hours are flagged in the plan and the UI.
- **`hold` is now battery first with max SoC = current SoC.** 0.1.0 mapped it to load first with
  a raised floor, where surplus sun still goes into the battery, so "sell the sun now" could not
  be carried out.
- **Grid-charge target** is the SoC after the last planned grid hour, not after the whole
  charging run: charging the plan leaves to the sun is no longer done from the grid.
- **Discharge floor** is the SoC the plan expects at the end of the current step instead of a
  hard 10 %. Solar charging caps the rate when the plan sells part of the sun.
- **Time zone** is the real `Europe/Copenhagen` (from `TZ`), not a fixed UTC offset: the 0.1.0
  profile put summer-time hours an hour off, and after a DST change the whole plan would be.
- `sensor.energy_planner_soc_target` is always published, `unknown` (attribute `active: false`)
  when no target applies, instead of keeping a stale value.
- Decision log also records energy counters and the current prices; `/api/evaluation` gives the
  realised grid cost per day, so the dry-run can be judged in kroner.
- Load profile uses a 15 % trimmed mean instead of the median (the median of a right-skewed load
  underestimates the energy). Missing statistics are retried next cycle, not after 6 hours.
- Entity check: missing/unavailable configured entities are shown in the UI and the log.
- One planning cycle at a time (background loop vs. "run now").

## 0.1.0

- First version, **dry-run only**: plans the next 24-48 hours hour by hour and publishes what it
  *would* do. It never writes to the inverter.
- Linear-programming planner (HiGHS via SciPy) over buy/sell prices from Energi Data Service,
  Solcast hourly forecast (p50 blended with p10), a base-load profile from HA long-term statistics
  and the battery's real limits and losses. Grid charging at night versus selling the sun is decided
  by the numbers, not by a rule.
- Publishes `sensor.energy_planner_mode`, `_soc_target`, `_soc_floor`, `_charge_rate` and `_plan`.
- Logs every decision next to the register values Node-RED actually has, in `/data/decisions.jsonl`
  (`/api/decisions` in the UI), so the two can be compared before anything is switched over.
