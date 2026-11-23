# Energy Planner — Home Assistant add-on

Plans a Growatt hybrid inverter + battery **hour by hour**: when to charge from the grid, when to
store solar instead of selling it, when to discharge, and when to charge the car. It replaces the threshold ladders in the
Node-RED flow (forecast ≥ 10 / 7 / 4 kWh → SoC 10 / 30 / 45 %) with one optimisation over the next
24-48 hours.

It writes the inverter registers and controls the charger and the car while its two HA helpers are
on (see *Production* below). Switching a helper off is the emergency stop: that part then writes
and calls nothing (shown as "Manuel styring" in the panel), so it can be controlled by hand.

## Production

Create two helpers (Settings → Devices & services → Helpers → Toggle):

| Helper | When on |
| --- | --- |
| `input_boolean.energy_planner_produktion` | Every planning cycle (5 min and at each hour) the add-on writes the house-battery registers: battery first (the all-day slot, Enabled/Disabled), charge from grid, max SoC, load-first min SoC and charge rate, and with `export_control` the export limit (0 % when the current hour's sell price is below `export_min_price`, 0.00 kr; otherwise 100 %). While the plan charges the car on sun in the hour (car connected and below its limit), export stays open down to `export_car_min_price` (−0.10 kr), so the car controller sees the real surplus instead of panels throttled to the house load. Remove the export limit from Node-RED when switching over. Only values that differ are written; there is no rate limit. Time slots are never written. |
| `input_boolean.energy_planner_bil_produktion` | Every car cycle (2 min) the add-on carries out the car controller's calls: charger current, the start button and `kia_uvo.start_charge` / `stop_charge` (needs `car_device_id`). The same Kia call is not repeated within 10 minutes while the charger status is unchanged (Kia's cloud quota). |

- **The car is paused with its charging switch** (`car_charge_switch_entity`,
  `switch.ioniq_9_ev_charging`, turned off) and resumed with `kia_uvo.start_charge`.
  `kia_uvo.stop_charge` makes the IONIQ end the charging session: the THOR registers an unplug 3
  minutes later and cannot start again without re-plugging. The switch only pauses (the THOR stays
  in `suspended_ev`). Leave the option empty to use `kia_uvo.stop_charge` again.
- **SoC between the car's updates:** the Kia integration only reports every 15 minutes. Between
  reports the car's SoC is the last reported one (time from `car_soc_time_entity`,
  `sensor.ioniq_9_last_updated_at`) plus the energy the charger delivered since
  (`charger_power_entity`, sampled every 5 s) × `car_eta`. So the controller stops at the limit or
  target instead of up to ~3 % past it. A full charge (≥ 99 %) still only counts from the car's own
  report. Shown in the Bil tab; `soc_estimated`, `soc_kia`, `soc_kia_at` on the car sensor.
- **At least 2 kWh per charge** (`car_min_session_kwh` 2, 0 = off): a planned session (hours with
  car charging, pauses up to `car_pause_gap_hours` included) that brings the car less is taken out of
  the plan, the preview ("Sæt bilen til") and the controller never starts the charger for it (unless
  the car is at its critical SoC). A charge already running may finish.
- **Short gaps are not paused** (`car_pause_gap_hours` 2, `car_pause_min_saving_dkk` 0.20): when the
  plan wants a one-hour pause while charging from the grid, the next planned charge is within the gap
  and waiting saves less than 0.20 kr/kWh, the car keeps charging at `car_min_amps` instead (a pause
  through Kia has been followed by the charger ending the session 3 minutes later). 0 = off.
- **Diagnosis** (Bil tab): from the start of a charge to 10 minutes after it last stopped (a pause
  that resumes within 10 minutes stays in the same trace; at most 24 h; or by hand: "Start diagnose
  nu"), every 5 s the add-on records what changes on the charger's and the car's entities (by
  object-id prefix, plus `car_trace_prefixes`): states and attributes, the charger's heartbeat
  timestamps left out, big JSON attributes (the car's vehicle data) only by the fields that changed
  (e.g. `…ChargingConnector.LockState 2 → 1`). The add-on's own decisions, calls (before they are
  sent) and replies (with the time they took) are written in between, so cause and effect read in
  order. `/data/car_traces/` (latest 10), `GET api/car/traces`.
- **Turn off the THOR's own automatic charging schedule** (ShinePhone; in HA
  `time.…_automatic_charging_start` / `_stop`, option `charger_schedule_entities`). At the schedule's
  start or stop time the charger ends the running session ("Unknown Stop" on its display), and a
  session paused through Kia cannot be restarted without re-plugging. The panel warns while a
  schedule is set (start ≠ stop), and when the charger ends a session on its own while the plan has
  more for the car.
- If the car cannot be paused through Kia (an error such as a rejected PIN, or the car ignoring the
  stop): no repeated PIN attempts (6 h pause, or switch the car helper off and on), other errors
  back off 2 / 4 / 8 / 15 min, the charger is set to 6 A, and while charging from the grid with
  nothing more planned before the deadline the charger's stop button is pressed as an emergency
  stop (`car_thor_stop_fallback`). The THOR cannot be restarted from the add-on after that: re-plug
  the cable. The warning is shown in the panel and on `sensor.energy_planner_car`.
  The emergency stop needs all of: the pause wanted for at least 5 min, the plan having nothing more
  for the car, and the charger still saying `charging` when read again just before the press. A Kia
  call that returns an error while the charger shows the car paused counts as a success; a call
  with no reply in time counts as unknown ("?" in the log) and is judged by the charger status.
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
- Check once on the inverter that load first with min SoC at (or just above) the SoC does not
  discharge and does not charge from the grid.

## How it decides

Every 5 minutes, just after each full hour (and whenever you press *Kør planlægning nu*) it solves a linear program over the
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
- **100 % now and then** (`car_full_charge`): only when power is at rock bottom (a negative sell
  price before the deadline, or the night's cheapest hours among the 5 % cheapest of the last 30
  days), at most every `car_full_min_days` (14) days, never forced.
- **Trips**: `input_datetime.ioniq9_afgang` (date and time) and optionally
  `input_number.ioniq9_afgang_soc` (default 100 %). Planned once the departure is inside the horizon.
- **"Hvis bilen sættes til nu"**: while the car is unplugged (and its SoC is known), the plan is also
  solved as if it were plugged in now, without writing anything. The Bil tab shows whether and when
  it would charge, how much and at what grid cost, with the chart; `sensor.energy_planner_car` has
  `preview_kwh`, `preview_window` and `preview_text`. An extra charge can be ordered before plugging
  in and shows in the preview at once.
- **Extra charge** (Bil tab, card "Ekstra ladning"): +10 % or +20 % on top of the car's SoC now, or
  an own target SoC. The target is locked when ordered (it does not creep up while the car charges)
  and is reached by the next deadline, or by the trip if one is set, in the cheapest hours; the
  daily limit is lifted to it (max 100 %). One order at a time; it expires by itself after its
  deadline and survives restarts (`/data/car_extra.json`). API: `POST api/car/extra` with
  `{"pct": 20}` or `{"target": 80}`, `DELETE api/car/extra`.

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

## Battery taper and car vs battery

The battery's BMS takes less power in the last part. The add-on learns the charge power per 2 % SoC
bin above 80 % from HA's 5-minute statistics (`battery_charge_power_entity` and `battery_soc_entity`,
about 10 days; 90th percentile, non-increasing, never above `battery_max_power_kw`). Without enough
data it uses the default: full power up to `battery_taper_start_pct` (90 %), then down to
`battery_taper_end_kw` (1 kW) at 100 %. The plan limits the energy stored per hour to what the curve
allows from the SoC at the start of the hour, so it does not count on reaching 100 % faster than
the battery can. The Plan tab shows the curve. For a 4 kW / 7.75 kWh battery it matters little
(it is full within the hour anyway); it matters when the last cheap hour starts high.

Car and battery in the same cheap hours: no fixed priority. The car's minimum by the deadline comes
first (a near-hard target), the main fuse shares the grid, and beyond that the cheapest overall
result wins (the battery when tomorrow's peak saves more than the car's 1.25 kr/kWh). The plan's
tooltip and the "Nu" tile say how an hour is split and why.

## Garage heat pump

`climate.varmepumpe` (`heat_pump_entity`) gets its setpoint from the price: the garage is a heat
store, so in the cheapest third of the next 24 hours the setpoint goes up by `heat_pump_band` (2 °C),
in the dearest third down by it, otherwise it stays at `heat_pump_setpoint` (12 °C, the garage then
measures about 14 °C). Hours where the plan curtails sun or exports at a sell price <= 0 count as
cheap. Safety from the garage sensors (`heat_pump_temp_entity`, `heat_pump_humidity_entity`): never
lower below `heat_pump_room_min` (11 °C, warm up below 10 °C), never raise above `heat_pump_room_max`
(17 °C), never lower at humidity above `heat_pump_humidity_max` (70 %) or without a reading. The
compressor regulates by itself; the mode (heat/off) is never touched. Written with
`climate.set_temperature` at most once per hour while the helper
`input_boolean.energy_planner_varmepumpe_produktion` is on (create it; off = manual control). The
The load forecast follows the control: every hour the level the heat pump ran with is logged
(`/data/heat_pump_levels.json`), its mean use per level is learned from its meter
(`heat_pump_power_entity`, recent days weighted most), and from two hours ahead the next 24 hours use
that value for the hour's level instead of the heat pump's clock-hour profile (until a level has 3
hours of data, the profile stays). The
Plan tab shows the setpoint, why, the garage's temperature and humidity and the next 24 hours;
`sensor.energy_planner_heat_pump` has the same for dashboards.

## Main fuse

The car (16 A = 11 kW over three phases) and grid charging of the house battery (4 kW ≈ 6 A per
phase) on top of the house load can come close to a 25 A main fuse.

- **The plan** keeps the worst phase below `main_fuse_amps` − `fuse_margin_amps` (25 − 3 = 22 A)
  in every hour. Car and battery are three-phase; the house load is counted as if it were all on one
  phase (conservative). The LP decides which of the two gets less. Example: car 16 A + house
  0.8 kW (3.5 A) leaves about 2.5 A, i.e. ~1.7 kW, for the battery.
- **The live guard** uses the grid current per phase (`grid_current_entities`, three sensors, the
  sign is ignored). Must be the grid meter's currents, not the inverter's AC output. When the
  highest phase would exceed the fuse − 1 A, the car controller lowers the car's amps (not below 6 A;
  the reason says "begrænset af hovedsikringen"). If the car is at 6 A or not charging, the battery
  check every minute lowers the grid charge rate (production only) and gives it back in 10 % steps
  once there is 3 A of room. Without the sensors, only the plan's limit applies, and the panel
  shows a warning.
- The Plan tab shows the highest phase against the fuse.

## The panel

Opens from the sidebar (*Energy Planner*), through ingress.

- **Plan**: what the planner would do now and over the horizon. Chart: prices (estimated hours
  hatched), energy per hour (charging above the line, discharging and export below, the mode as a
  strip on top, the car's ~11 kWh bars cut with a break mark so the house flows stay readable) and
  the SoC of the house battery and the car. Hover or use the arrow keys for every value of an hour.
  "Planner mod inverteren nu" shows each register the planner would set next to its current value.
- **Bil**: the car controller's decision and the calls it would make, the car plan, and its log.
- **Sol**: the solar forecast for today and the coming two days (p10–p90 band with the correction,
  Solcast's p50 dashed, what the plan uses, today's actual production as bars); forecast against
  actual day by day (today, yesterday and the 12 days before): actual, Solcast p10/p50/p90, Solcast
  with the current correction, and the forecast from the evening before (what the plan counted on
  at 20:00 the day before, kept in `/data/pv_dayahead.json` from 0.9.0 on), with a table of daily
  totals; and the shading correction (moved here from the Plan tab).
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
| Buy / sell price per hour | Strømligning: `sensor.stromligning_current_price_vat`, `sensor.stromligning_production_price_vat` (`prices`); forecast `sensor.stromligning_forecasts_vat`; fallback `sensor.kob_af_el`, `sensor.salg_af_el` (`raw_today`, `raw_tomorrow`) — see *Prices* below |
| Solar forecast per hour | Solcast `detailedHourly` of `…forecast_today`, `_tomorrow`, `_day_3` — p50 blended with p10 (`pv_p10_weight`) |
| Battery SoC | `sensor.growatt_battery_soc` |
| Base load | Hourly means of `sensor.growatt_total_house_load` **minus** EV charging (`car_power_entity`, default `sensor.ioniq_9_ladeeffekt`; hours without its statistics and above `car_detect_kw` are left out as car charging) (and minus grid charging of the battery if the sensor includes it), per (weekday/weekend, hour) over `profile_days` (56), recent days weighted most (half-life `profile_half_life_days`, 10) plus the same weeks last year (`profile_last_year_weight`, 0.5). Appliances with their own meters (`load_submeters`) get a plain mean each, the rest a trimmed mean; appliances that run when needed have their timing smoothed (weekday and weekend pooled, ±`load_submeters_smooth_hours` = 2 h, the day's energy kept), while `load_submeters_steady` (the heat pump) keep their hourly profile; in the current hour the meters' live power replaces their profile (next hour: half) |

### Prices

Settlement is per hour, so quarter-hour prices are always averaged to the hour. Every hour of the
plan gets its price from the first of:

1. the price sensors (`price_buy_entity`, `price_sell_entity`, Strømligning),
2. the fallback sensors (`price_buy_fallback_entity`, `price_sell_fallback_entity`, the old Energi
   Data Service templates) for hours the price sensors lack,
3. Strømligning's forecast (`price_buy_forecast_entity`), remembered per hour (the sensor starts two
   days ahead, so tomorrow's forecast is the one it showed yesterday; kept in `/data`, and read back
   from HA's history at start); the sell price of a forecast hour is the
   forecast minus the buy - sell difference at the same hour of the last known day,
4. the same hour of the last known day.

3 and 4 are "estimated" (italic in the plan table, the source in the tooltip), and only fill up to
`min_horizon_hours`. Known prices are never replaced by a forecast.

The economy and the 30-day price history (the 100 % rule) also take each hour from the price sensor
and else from the fallback. Strømligning's buy sensor has `state_class: total`, so HA keeps its
hourly `state` rather than a `mean`; both are read. Once Strømligning has a month or so of history
(the Økonomi tab's longest period you use), clear the two fallback options; nothing else changes.

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
and the actual production (`pv_power_entity` statistics), over the last `pv_dampening_days` (60).
Clear days are rare, so the window is long, but recent weeks count most: an hour `age` days old
weighs `0.5^(age / pv_dampening_half_life_days)` (14) in the fit, so the sun's changing path and
growing trees are followed. Each past hour is measured against the clear sky of its own weeks
(the highest p90 within ±10 days of it). Solcast only keeps 14 days of history, so the longer
window fills up from the add-on's own recordings (kept for 180 days).
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

## Front page

Two ready-made card stacks for the Home Assistant front page: `dashboards/forside-tile.yaml` (HA's
own tile cards) and `dashboards/forside-mushroom.yaml` (Mushroom from HACS), one card per line. Paste one via Edit
dashboard → Add card → Manual. They show:

- **Sæt bilen til?** `sensor.energy_planner_car_advice`: `plug_in` (would charge if plugged in now,
  with when, how much and the cost), `not_needed`, or while connected `charging` / `waiting` /
  `full`; attributes `title`, `text`, `icon`, `color`.
- **Elpris nu** `sensor.energy_planner_price`: the hour's buy price, `level_da`
  (billig/normal/dyr, the cheapest/dearest third of the next 24 h) and `cheapest_text` (the cheapest
  3 consecutive hours, e.g. "billigst i nat kl. 02–05 · 1,12 kr") for washing and drying.
- **Husbatteri** `sensor.energy_planner_mode`: `title` ("Lader fra nettet · 41 %") and
  `next_action`.
- **I dag** `sensor.energy_planner_savings_today`: the installation's value today, as "Anlægsværdi"
  in the Økonomi tab (consumption at the buy price minus the net cost), so far (the counters, whole
  hours) and `expected_dkk` for the whole day (plus the plan's hours to midnight, the current hour in
  full). `title` "Anlægget: +12 kr i dag", `text` "sol +14 kr · batteri −2 kr · forventet +20 kr i alt
  · sol 9,2 af 18,4 kWh": sun stored instead of sold is a lost sale in the battery's part, matched by
  the sun's part until it is used. `solar_value_dkk` / `battery_value_dkk` are attributes.
- **Maskiner** `sensor.energy_planner_appliances`: the best start of each appliance in the next 24
  hours (`title` "Opvask i nat kl. 03 · Vask nu · Tørretumbler kl. 13", `text` one line each with the
  cost and the saving against starting now, `appliances` the details). See *Appliances* below.
- **Advarsler** `sensor.energy_planner_warnings` (number of messages, `messages`): shown only when
  something needs attention (car/Kia problems, manual control, planning errors, missing entities,
  the THOR's own schedule).

## Appliances

`appliances` lists the meters: `Opvask=sensor.shelly_opvaskemaskine_power,Vask+Tørretumbler=sensor.bryggers_shelly_vask_og_torre_effekt`.
A meter with two names serves two appliances: a run whose mean power while running is at least
`appliance_dryer_min_w` (700 W) is the second (the dryer), otherwise the first.

Runs are found in the meters' 5-minute statistics once an hour (power above 15 W, pauses up to 15 min
inside a run, at least 0.1 kWh) and kept in `/data/appliance_runs.json` (the latest 20 each). From 3
runs the program is learned: median length and energy, and the share of the energy per hour from the
start. Before that: dishwasher 2.5 h / 1.0 kWh, washer 2 h / 0.8 kWh, dryer 2 h / 2.0 kWh.

The cost of a start uses the plan: per hour, sun the plan would curtail is free, sun it would sell
costs the sell price, the rest the buy price. The best start is the cheapest whole hour (or now) from
which the program ends within the next 24 hours; "nu" when waiting saves less than 0.25 kr.

## Heat pump off in the dearest hours

A dear hour without sun to sell is "off" when it is one of the `heat_pump_off_hours` (4) dearest of
the next 24 hours, or its price is at least the `heat_pump_off_percentile` (90) of the last 30 days'
prices - so a dear day can switch it off for longer. Then the add-on calls `climate.set_hvac_mode`
`off` and remembers the mode it had; when the hour is no longer "off" it switches it on again with
that mode (a heat pump switched off by someone else is left alone). Below `heat_pump_off_min_temp`
(10 °C in the garage) it runs whatever the price, until the garage is a degree warmer; too humid or no
temperature reading: no switching off.

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
| `grid_charge` | on | on | SoC after the last planned grid hour; once reached, grid charging goes off and max SoC rises to the end of the charging run if the sun continues it (checked every minute) | current |
| `solar_charge` | on | off | SoC at the end of the charging run (rate capped if part of the sun is sold) | current |
| `discharge` | off (load first) | off | – | `soc_floor_pct` + what later, dearer hours need before the next charge (the configured floor in the dearest hour) |
| `hold` | off (load first) | off | – | the SoC when the hold started (kept while sun charges the battery) |
| `hold` while the plan sells the sun | on | off | current SoC | current |

`hold` is load first with the floor at the SoC: the battery does not discharge, and surplus sun is
stored instead of sold (also sun the forecast did not expect). Only when the plan sells the sun in
that hour is it battery first with max SoC = SoC, so the sun goes out. Within a clock hour, hold and
discharge only switch for at least 0.15 kWh through the battery in the rest of the hour, and floor
changes below 2 percentage points are not written (except when the mode changes). Every value is chosen so a
planner that stops refreshing leaves the inverter at the planned state, not charging on.

## Not in this version

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
