# Battery Optimizer – notes for Claude

## Commit identity (do this before the first commit in every session)

```bash
git config user.name "Daniel A. Gregersen"
git config user.email "241836239+danielmgregersen-code@users.noreply.github.com"
```

This is the owner's GitHub noreply address, so commits count on their profile. Never use their
private email. Claude is credited as co-author through the `Co-Authored-By` trailer in the commit
message.

## Workflow

- Work on `main` and push to `main`. No pull requests unless asked.
- Every change: bump `version` in `energy-planner/config.yaml`, add a `CHANGELOG.md` entry, update
  `energy-planner/README.md`, and add or adjust tests. Run them with:
  `cd energy-planner && python -m pytest -q`. Check the panel's syntax with
  `node -e "new Function(require('fs').readFileSync('app/static/app.js','utf8'))"`.
- Reply to the owner in Danish.
- No model identifiers in commits, code or docs.

## Standing rules for the add-on (Energy Planner)

- Never change Node-RED from the add-on (its flow is switched off).
- No rate limit on the inverter's register writes.
- Battery first uses only the all-day slot `select.growatt_battery_first_time_2`.
- The THOR stop button is only a night-time emergency fallback while charging from the grid. A
  stopped THOR session cannot be restarted without re-plugging.
- Pause the car with `switch.ioniq_9_ev_charging` (turn off); resume with `kia_uvo.start_charge`.
- 100 % car charge only at rock-bottom prices, never forced.
- Settle with the hourly price, never quarter-hour prices.
- The washer and the heat pump dryer share one meter: see the appliance section of the README and
  `tests/fixtures/laundry_5min.json` (the real meter data the split is tested against).
