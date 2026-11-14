# Changelog

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
