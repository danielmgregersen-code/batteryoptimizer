# Changelog

## 0.29.0

- **One charge instead of several with a pause between them** (`car_one_session`,
  `car_one_session_max_dkk` 5 kr). Last night's trace: the pause at 05:51:06 made the car unlock the
  connector, and the THOR ended the session 3:02 later (registered as an unplug). Monday it was 3:03
  after the pause. The THOR only takes 6–32 A, so it cannot pause from the charger. Instead the plan
  now merges the car's grid charging into the cheapest run of consecutive hours when that costs at
  most 5 kr more and reaches the same targets. The only stop left is at the end of the night.
- **No full-power start before the plan knows the car:** at 01:29 the plan had been made while the
  car's SoC was unknown, so the controller fell back to "ingen plan – under minimum, lader fuldt" and
  paused 2 minutes later. Now it plans again at once and waits ("Venter på en plan med bilen", at most
  10 min, critical SoC excepted).

## 0.28.0

- **The car charges at full power** (`car_full_power`): in an hour where the plan charges from the grid,
  at the highest current the main fuse allows (minus `fuse_margin_amps`) instead of a low current spread
  over the hour - less charging loss. The last hour of a session stops when its energy is in; the first
  hour of a longer session starts late at full power and runs on into the next hour, so no extra pause.

## 0.27.2

- **Goals follow the historical value:** the 12 000 kr a year are spread over the months by what each
  month has earned on average (sun and prices), not by the solar production alone - spring weighs most.
- **Errors in the sources are left out:** a day far outside the normal range (e.g. -2 293 kr from a
  glitch in the old sensor) is not counted; the next source (an estimate) is used and the day is listed
  on the card.

## 0.27.1

- **Realistic goals:** the goal is the payback the installation was bought for - `installation_cost_dkk`
  (now 120 000 kr) in `goal_payback_years` (10), i.e. 12 000 kr a year - spread over the months by the
  sun, so a July day has a higher goal than a January day and a year adds up to 12 000 kr. "Overblik
  mål" shows the goal line (paid back in 10 years) next to the actual total and the forecast.

## 0.27.0

- **Goals on the Livstid card:** the expected value per day (the yearly forecast spread over the months
  by the sun) against the actual days: tiles for today / this week / this month / this year, "Overblik
  mål" (the total and the dashed forecast to the end of life against the price), the week's days
  against the goal (page back with ‹ ›), a results table per day for a chosen month with coloured
  percentages, and "År status" per month.

## 0.26.0

- **Lifetime value, forecast and payback** (Økonomi tab, "Livstid"): what the installation has earned
  since July 2023 - the add-on's measured value where it has data, the old value sensor
  (`sensor.power_sum_vaerdi_af_anlaeg_kr`) before that, and for July - October 2023 an estimate from
  the solar production x the value per kWh of the same months in 2023 - 2025 (never 2026, after the
  tax cut). Paid back in % of 110 000 kr with the expected date, expected value per year (from 2026's
  level once there is enough of it) and over 25 years; a monthly chart by source and the total against
  the price.

## 0.25.0

- **Diagnosis traces cover the whole charge:** a trace starts when charging starts and runs until 10
  minutes after it last stopped (a pause that resumes within 10 minutes stays in the same trace, at
  most 24 h). The add-on's decisions, its calls (written before they are sent) and their replies
  with the time they took are in the trace, so what happened before a stop can be read in order.
- Less noise: the charger's heartbeat timestamps are left out, and the car's vehicle data is stored
  only by the fields that changed (e.g. `ChargingConnector.LockState 2 → 1`).
- Panel: the trace shows start, end, duration and the clock time of each change, the add-on's rows
  in bold, and a filter "Kun add-on, status og ladestikket".

## 0.24.6

- **The car card after the THOR ended the session** (status `idle`): "Opladningen er afsluttet" -
  "tag stikket ud og sæt det i igen · lader 6 kWh i nat kl. 02–04" when the plan wants to charge,
  otherwise "laderen har afsluttet sessionen". The Mushroom card takes its colour from the sensor.
- Diagnosis traces name the add-on's pause even when the car stops before the call returns (it showed
  "ukendt").

## 0.24.5

- Panel clean-up: the "Planner mod inverteren nu" card and the "Pris nu" and "Hovedsikring" tiles are
  gone from the Plan tab; the badge at the top is only shown when something is under manual control
  ("Manuel styring: bilen").
- The charger's `idle` status (the THOR has ended the session, the cable has to be re-plugged) reads
  "Afsluttet: stikket skal ud og i igen"; `available` reads "Ledig (intet stik)".
- The battery's lower SoC limit is called "grænse" (was "gulv") everywhere.

## 0.24.4

- **The I dag card shows "Anlægsværdi"** (the same number as the Økonomi tab), with the sun's and the
  battery's part in the text: "Anlægget: +12 kr i dag" · "sol +14 kr · batteri −2 kr · forventet
  +20 kr i alt". The expected total counts the plan's hours to midnight (the current hour in full).

## 0.24.3

- **The I dag card no longer subtracts the battery's SoC drop.** "Forventet" valued the change of
  the SoC from midnight to midnight, so energy charged yesterday and used today was taken off again
  when it was used. It is now a plain cash view: a charge costs in the hour it happens, using the
  battery earns the buy price in the hour it is used.

## 0.24.2

- **Battery value: sun stored in battery first while the house buys costs the buy price.** In battery
  first the Growatt fills the battery from the sun while the house takes from the grid; that sun would
  otherwise have covered the house, so it was valued too cheaply (at the sell price). The share of each
  hour in battery first comes from the decision log (the Økonomi tab and the I dag card), the plan's
  own flows for the rest of the day.
- The I dag card's "forventet" counts the current hour in full (the counters stop at the last whole
  hour, so the part of it that has passed was missing).

## 0.24.1

- **Tomorrow's prices from Strømligning's forecast, also before 13:00:** the forecast sensor starts
  two days ahead, so tomorrow's forecast was only in it yesterday and the plan fell back to "same hour
  as today". Every forecast seen is now kept per hour (`/data/price_forecast.json`, the newest wins),
  and at start the last 3 days of the sensor are read from HA's history.

## 0.24.0

- **When to start the appliances:** new `sensor.energy_planner_appliances` and a "Maskiner" card
  (panel and the front-page stacks): the best start of the dishwasher, the washing machine and the
  dryer in the next 24 hours, with the cost and the saving against starting now. Programs are learned
  from the Shelly meters (washer and dryer share one meter and are told apart by power,
  `appliance_dryer_min_w`).
- **Heat pump off in the dearest hours:** the `heat_pump_off_hours` (4) dearest hours of the day, and
  every hour above the `heat_pump_off_percentile` (90) of the last 30 days' prices, switch the garage
  heat pump off; it is switched on again (in its previous mode) when the price falls. Below
  `heat_pump_off_min_temp` (10 °C) it runs whatever the price.

## 0.23.0

- **The I dag card shows what the battery has earned or cost:** compared with using / selling the sun
  directly. Sun stored in the battery is a lost sale (negative) until it is used in a dear hour.
  "Tjent 7 kr i dag" / "Kostet 5 kr indtil nu", "forventet +12 kr i alt". The old number (sun and
  battery against no installation) is kept as the attribute `system_value_dkk`.
- **No charging for less than 2 kWh** (`car_min_session_kwh`): smaller planned sessions are taken
  out of the plan and the "Sæt bilen til" preview, and the controller does not start the charger for
  them (sun included). The critical SoC is exempt, and a charge already running may finish.

## 0.22.1

- **"Forventet i alt" on the I dag card counted the battery twice:** the energy stored so far today
  was taken off once in the counters (sun not sold) and again as the SoC drop from now to midnight.
  The SoC change is now valued from midnight to midnight (SoC at the day's start from the decision
  log, else the SoC sensor's statistics). New attributes `soc_start_pct` and `battery_adjust_dkk`.

## 0.22.0

- **Prices from Strømligning:** `price_buy_entity` / `price_sell_entity` now default to
  `sensor.stromligning_current_price_vat` / `sensor.stromligning_production_price_vat`, and the new
  `price_buy_forecast_entity` (`sensor.stromligning_forecasts_vat`) fills the hours without a known
  price (sell derived from the same hour's buy - sell difference) before falling back to "same hour
  as the last known day".
- **The old sensors as fallback** (`price_buy_fallback_entity` = `sensor.kob_af_el`,
  `price_sell_fallback_entity` = `sensor.salg_af_el`): they fill hours Strømligning lacks, in the
  plan, the economy and the price history, until Strømligning has history enough. Clear the two
  options then.
- Statistics also read `state` (Strømligning's buy sensor has `state_class: total`, no `mean`).
- The plan tooltip and table show where an estimated price comes from.

## 0.21.0

- **"Holder batteriet" is load first with the floor at the SoC** (the user's idea): no discharge,
  and surplus sun goes into the battery instead of being sold. It sold sun at 1,69 kr when the
  battery was "held" with battery first and max SoC = SoC. Only when the plan sells the sun in that
  hour is it still battery first with max SoC = SoC.
- **Calmer registers:** within a clock hour hold and discharge only switch for at least 0.15 kWh
  through the battery in the rest of the hour (they flipped every 5 minutes on near-ties); a hold
  keeps the floor it started with; floor changes under 2 percentage points are not written.
- The plan table and tooltip show Sol → hus and Net → hus.

## 0.20.1

- **"I dag" shows savings:** new `sensor.energy_planner_savings_today`: saved so far today (from the
  counters, as "Anlægsværdi" in the Økonomi tab) and expected for the whole day (the plan's remaining
  hours plus the value of the battery's SoC change by midnight).
- Today's solar production now comes from the solar counter (it showed 0,0 kWh).
- The front-page card stacks show one card per line.

## 0.20.0

- **Front-page cards:** new sensors `sensor.energy_planner_car_advice` ("Sæt bilen til" with when,
  how much and the cost, or what the car does while connected), `sensor.energy_planner_price` (price
  level now and the cheapest 3 hours ahead) and `sensor.energy_planner_warnings` (only what needs
  attention); `sensor.energy_planner_mode` gets `title` / `text` / `soc_pct`,
  `sensor.energy_planner_cost_today` gets today's solar forecast and production and a `text`.
- Ready-made card stacks: `dashboards/forside-tile.yaml` (HA tile cards) and
  `dashboards/forside-mushroom.yaml` (Mushroom).

## 0.19.0

- **Car SoC between the Kia updates:** the last reported SoC plus what the THOR delivered since
  (charger power sampled every 5 s, × `car_eta`). The controller and the plan use it, so the car
  stops at its limit or target instead of up to 2–3 % past it. No extra wake-ups of the car.
  The Bil tab shows "beregnet: Kia 63,5 % kl. 11:24 + 1,8 kWh".

## 0.18.0

- **Found: why the THOR ended the session 3 minutes after a pause.** The diagnosis traces show that
  after `kia_uvo.stop_charge` the THOR registers an *unplug* (session end and unplug time at the same
  moment, voltage 0 on all phases) 3:01 later, while a pause from the Kia app keeps the session.
  Turning off `switch.ioniq_9_ev_charging` pauses the same way the app does, and
  `kia_uvo.start_charge` resumes.
- The car is now paused with `car_charge_switch_entity` (default `switch.ioniq_9_ev_charging`);
  resuming still uses `kia_uvo.start_charge`. The same Kia safeguards apply (quota, backoff, the
  charger emergency stop only as a last resort). The car log shows "bil pause (ladeswitch)".

## 0.17.0

- **No pause for a short gap that saves little:** when the plan wants to pause the car between two
  planned charging hours, the next one is within `car_pause_gap_hours` (2) and waiting saves less
  than `car_pause_min_saving_dkk` (0.20 kr/kWh), the car keeps charging at 6 A instead. Twice now
  the THOR ended the session 3 minutes after a pause through Kia.
- **Diagnosis traces** (Bil tab, "Diagnose: hvad skete der efter pausen"): after the charger leaves
  "charging", or started by hand, all charger and car entities are sampled every 5 s for 10 min and
  every change is listed with its time, plus what triggered the stop.

## 0.16.0

- **Heat pump use forecast by level:** the level the garage heat pump runs with is logged every hour
  and its mean use in cheap / normal / dear hours is learned from its meter. The load forecast for
  the next 24 hours (from two hours ahead) uses the learned value for each hour's level instead of
  the clock-hour profile, which cannot know where the cheap hours fall. Only while the add-on
  controls the heat pump; the profile is used until a level has data. Shown in the heat pump card.

## 0.15.0

- **Battery taper in the plan:** the charge power at high SoC is learned from the 5-minute
  statistics (default: full up to 90 %, then down to 1 kW at 100 %) and limits the energy stored per
  hour from the SoC at the start of the hour. New card "Batteriets ladekurve" in the Plan tab.
- **Why car and battery share a cheap hour as they do:** the plan tooltip and the "Nu" tile say e.g.
  "Sikringen deler nettet: bilen 11,0 kW, batteriet 1,7 kW" or that the car's minimum goes first.

## 0.14.0

- **Garage heat pump on cheap hours:** the setpoint of `climate.varmepumpe` moves ±2 °C around 12 °C
  with the price (14 °C in the cheapest third of the next 24 hours, 10 °C in the dearest third),
  with limits from the garage's temperature (11–17 °C) and humidity (no lowering above 70 %). Set at
  most once per hour while `input_boolean.energy_planner_varmepumpe_produktion` is on. New card in
  the Plan tab and `sensor.energy_planner_heat_pump`.

## 0.13.0

- **"Hvis bilen sættes til nu":** while the car is unplugged, a second plan is solved as if it were
  plugged in now (nothing is written; the house battery follows the real plan). The Bil tab shows a
  one-liner ("Ville lade 14,3 kWh i nat kl. 04–06 (net ca. 28 kr) og nå 42 % kl. 06." or why it
  would not charge), the chart and a "Hvis sat til nu" tile. The car sensor gets `preview_kwh`,
  `preview_window` and `preview_text` for dashboards.
- Extra charge can be ordered while unplugged; it shows in the preview straight away.
- Dev server: `--unplugged`.

## 0.12.0

- **Production is the normal mode; dry-run is gone from the panel, sensors and dashboards.** The
  production helpers stay as the emergency stop: switched off, the panel shows "Manuel styring"
  (the add-on writes and calls nothing). Header pill "Produktion" (green) / "Manuel styring"
  (amber); the car tab says "Styringen", "Kald nu" and "Handling" instead of "ville".
- The Node-RED comparison (agreement tile, planner-vs-Node-RED strip, agreement column) is removed
  from the evaluation tab: in production the registers follow the plan.
- Sensor attribute `dry_run` is replaced by `production` (true / false) on
  `sensor.energy_planner_mode` and `sensor.energy_planner_car`; the dashboards show the export limit
  as a controlled register and "manuel styring" only when production is off.

## 0.11.1

- **The car can charge on sun while export is closed.** With export at 0 % and the house battery
  full, the inverter throttles the panels to the house load, so the car controller saw no surplus.
  Export now stays open while the plan charges the car on sun in the current hour (car connected,
  below its limit), down to `export_car_min_price` (−0.10 kr). The car takes nearly all of the
  surplus, so only a few øre are exported. Shown as "100 % (bil)" and on the mode sensor as
  `export_reason` (price / car / stopped).

## 0.11.0

- **Export control** (`export_control`, `export_min_price` 0.00 kr): with battery production on, the
  add-on now writes the export limit (`number.growatt_grid_export_limit`): 0 % when the current
  hour's sell price is below the limit, otherwise 100 %. The hourly price is what is settled, so
  quarter-hour prices are never used. Written right after each hour change; remove the export limit
  from Node-RED when switching over. Exporting wears neither battery nor panels, hence 0.00 kr.
- The plan never exports in hours below the limit (stores the sun in the battery or car, or
  curtails). The panel shows the export limit as a written register and "eksport stoppet"; the
  mode sensor has `export_allowed` / `export_limit_pct`.

## 0.10.1

- **Smoothed timing for appliances that run when needed** (washer/dryer, dishwasher, hob): they have
  no fixed hour, so their expected energy is no longer placed hour by hour from a few days of
  history. Weekdays and weekends are pooled and each hour takes a triangular-weighted mean of ±2 h
  (`load_submeters_smooth_hours`); the day's energy is unchanged. Meters in `load_submeters_steady`
  (default the heat pump) keep their profile per hour. Their energy still counts untrimmed, and
  the current hour still uses their live power.

## 0.10.0

- **Expected house load follows the season:** the profile uses 56 days (`profile_days`) where
  recent days count most (`profile_half_life_days`, 10), plus the same weeks last year from HA's
  statistics (`profile_last_year_weight`, 0.5; 0 = off), so last winter's pattern (dryer, heat pump)
  is there before this winter's has built up.
- **Own meters** (`load_submeters`: heat pump, dishwasher, washer/dryer, hob): each gets its own
  plain mean per hour, so appliances that run on some days only count with their expected energy
  instead of being trimmed away as outliers; the rest of the house keeps the trimmed mean (weighted
  by sample weight). The current hour uses what the meters draw right now (a running dryer, the hob
  on), the next hour half of it while they run.
- The "Forbrugsprofil" card shows the profile stacked per meter (weekday/weekend) and the new
  diagnostics; `pred` logs the profile value and the meters' live power.

## 0.9.0

- **New "Sol" tab.** The solar forecast for today and the next two days hour by hour: the p10–p90
  band (with the correction), Solcast's p50, what the plan uses and today's actual production.
  Forecast against actual day by day (today, yesterday and the 12 days before) with Solcast
  p10/p50/p90, Solcast with the correction, actual, and the forecast from the evening before; a
  table of daily totals with the deviation in %. Tiles for today / tomorrow / day 3 and yesterday's
  hit rate. The shading correction card moved here from the Plan tab and shows the effective
  (weighted) days per hour.
- **The forecast from the evening before** is kept from 20:00 for the next day
  (`/data/pv_dayahead.json`), so it can be compared with what actually came.
- **The shading correction learns from a longer window with more weight on recent days**:
  `pv_dampening_days` 14 → 60 (max 180), new `pv_dampening_half_life_days` (14). Past hours are
  measured against the clear sky of their own weeks. The add-on keeps 180 days of its own recordings
  (Solcast only 14), so the window fills up over time.
- API: `GET api/solar?day=YYYY-MM-DD`, `GET api/solar/days?days=14`; the plan has `solar_forecast`.

## 0.8.2

- **The THOR's own charging schedule ended last night's session.** The log shows the add-on only
  paused the car through Kia at 02:55; at 02:58:41 the charger ended the session by itself
  ("Unknown Stop"), just before its automatic charging window (03:00–05:00). The explanation in
  0.8.1 (an emergency stop from the add-on) was wrong; its extra safeguards stay.
- Warning in the panel and on the car sensor while the THOR has a schedule set
  (`charger_schedule_entities`, start ≠ stop): turn it off in ShinePhone.
- Warning when the charger ends a session on its own (charging/paused → finishing/idle/available
  with nothing sent from here) while the plan has more for the car: re-plug the cable.

## 0.8.1

- **No more emergency stop on the charger after a pause that worked.** A Kia stop that came back with
  an error (or late) while the car did pause was counted as a failure, and with an earlier failure the
  charger's stop button was pressed 3 min later, based on the status read before the call. The THOR
  then ended the session, and the cheap hours planned after were lost. Now:
  - the charger status is read again after a failing Kia call and right before an emergency stop; a
    car that has paused means success,
  - no reply in time is "unknown", not a failure,
  - the emergency stop needs the pause wanted for at least 5 min and nothing more planned for the car
    (being at a lifted limit, e.g. an extra charge, is no longer enough).

## 0.8.0

- **Main fuse** (`main_fuse_amps` 25, `fuse_margin_amps` 3): the plan never puts the car and grid
  charging of the battery together above the fuse minus the margin on the worst phase (house load
  counted as one phase). Tonight's 16 A car + 4 kW battery becomes 16 A car + ~1.7 kW battery, or
  whatever split is cheapest.
- **Live fuse guard** from the grid currents per phase (`grid_current_entities`): lowers the car's
  amps (min. 6 A), then the battery's grid charge rate, when the highest phase would exceed the
  fuse − 1 A; restores when there is room. Tile "Hovedsikring" in the Plan tab. The default entity
  names are a guess — check them in the configuration (the panel warns if they are missing).

## 0.7.2

- **Extra charge for the car from the panel**: in the Bil tab, "Ekstra ladning" orders +10 % or
  +20 % on top of the car's SoC now, or an own target SoC. Useful when you expect tomorrow's prices
  to be higher than the plan's estimate. The target is locked at the order, planned in the cheapest
  hours before the next deadline (or the trip, if set), lifts the daily limit (max 100 %) and expires
  by itself. Can be cancelled. The car sensor gets an `extra` attribute.
- The daily minimum (42 % by 06:00) no longer replaces a higher target in the same hour (e.g. 100 %
  or an extra charge) when the car is below the minimum.

## 0.7.1

- **No more selling the sun the plan wanted stored.** When grid charging is followed by solar
  charging (e.g. grid to 72 %, then sun to 97 %), the inverter used to stop at the grid target and
  export the sun until the next plan. Now a check every minute hands over as soon as the grid target
  is reached: charge from grid off, max SoC = the target of the whole charging run. The grid still
  never charges beyond its own target.
- **Plans at every hour change:** besides the interval, a cycle runs 20 s after each full hour, so a
  new hour's setpoints are written at once instead of up to an interval late.
- `interval_minutes` defaults to 5 (was 15). The decision log keeps about 31 days at that rate.
- The panel shows the setpoints in force (after a hand-over) and "…, derefter sol til 97 %".

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
