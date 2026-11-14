"""Add-on options. Defaults mirror config.yaml so the app also runs outside HA."""
import json
import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

OPTIONS_FILE = "/data/options.json"

DEFAULTS = {
    "price_buy_entity": "sensor.kob_af_el",
    "price_sell_entity": "sensor.salg_af_el",
    "solcast_entities": (
        "sensor.solcast_pv_forecast_forecast_today,"
        "sensor.solcast_pv_forecast_forecast_tomorrow,"
        "sensor.solcast_pv_forecast_forecast_day_3"
    ),
    "battery_soc_entity": "sensor.growatt_battery_soc",
    "house_load_entity": "sensor.growatt_total_house_load",
    # Car charging for the base-load profile, from long-term statistics. A power sensor (W/kW,
    # hourly mean) or an energy counter (kWh, total/total_increasing: hourly change) both work.
    # sensor.ioniq_9_ladeeffekt: W, measurement, with history from before the THOR (then an estimate,
    # now a mirror of the THOR's charging power).
    "car_power_entity": "sensor.ioniq_9_ladeeffekt",
    # Hours without car statistics: a base load above this is taken to be car charging and left out
    # of the profile (the THOR's statistics only start once it has a state_class).
    "car_detect_kw": 4.0,
    # The charger's live power (W) for the car controller's surplus calculation.
    "charger_power_entity": "sensor.ioniq_9_ladeeffekt",
    "reg_battery_first": "select.growatt_battery_first_time_2",
    "reg_max_soc": "number.growatt_battery_first_maximum_soc",
    "reg_min_soc": "number.growatt_load_first_battery_minimum_soc",
    "reg_charge_rate": "number.growatt_battery_first_charge_rate",
    "reg_grid_charge": "select.growatt_battery_first_charge_from_grid",
    "reg_export_limit": "number.growatt_grid_export_limit",
    "battery_capacity_kwh": 7.75,
    "battery_max_power_kw": 4.0,
    "inverter_kw": 10.0,
    "soc_floor_pct": 10,
    "soc_ceiling_pct": 100,
    "eta_pv_to_battery": 0.97,
    "eta_grid_to_battery": 0.94,
    "eta_battery_to_load": 0.95,
    "wear_cost_dkk_per_kwh": 0.25,
    "pv_p10_weight": 0.3,
    # Learned shading per hour on top of Solcast (see pvdampening.py): auto / manual / off.
    "pv_dampening": "auto",
    "pv_dampening_days": 14,
    "pv_dampening_manual": "",        # e.g. "14:0.3,15:0.2" = clear-sky factor for those hours
    "solcast_push": False,            # also set the factors in the Solcast integration (daily)
    "profile_days": 28,
    "default_load_kw": 0.8,
    # Does sensor.growatt_total_house_load count the battery's charging as house load? On the
    # SPH10000TL3 it does not. auto = detect from the statistics, yes = always subtract.
    "load_includes_battery_charging": "no",
    "interval_minutes": 15,
    "min_horizon_hours": 30,
    # Cumulative energy counters (kWh) logged with every decision, so the realised cost of what
    # actually happened can be computed afterwards (/api/evaluation).
    "counter_entities": (
        "sensor.growatt_total_grid_import,"
        "sensor.growatt_total_grid_export,"
        "sensor.growatt_total_battery_input_energy,"
        "sensor.growatt_total_battery_output_energy,"
        "sensor.growatt_total_solar_energy"
    ),
    "grid_import_counter": "sensor.growatt_total_grid_import",
    "grid_export_counter": "sensor.growatt_total_grid_export",
    "battery_input_counter": "sensor.growatt_total_battery_input_energy",
    "battery_output_counter": "sensor.growatt_total_battery_output_energy",
    "solar_counter": "sensor.growatt_total_solar_energy",
    # --- car (replaces the "Solcelle- & Prisstyret ladning: IONIQ 9" automation; dry-run) ---
    "car_enabled": True,
    "car_soc_entity": "sensor.ioniq_9_ev_battery_level",
    "charger_status_entity": "sensor.garage_growatt_thor_ev_charger_status",
    "charger_current_entity": "number.garage_growatt_thor_ev_charger_maximum_current",
    "charger_start_button": "button.garage_growatt_thor_ev_charger_start_charging",
    "pv_power_entity": "sensor.growatt_pv_power_total",
    "battery_charge_power_entity": "sensor.growatt_battery_charge_power",
    "car_battery_kwh": 110.0,
    "car_phases": 3,
    "car_min_amps": 6,
    "car_max_amps": 16,
    "car_eta": 0.92,                 # 10.2 kW into the car of 11.04 kW at the plug
    "car_critical_start_pct": 20,
    "car_critical_stop_pct": 22,
    "car_min_start_pct": 40,
    "car_min_stop_pct": 42,
    "car_max_soc_pct": 90,
    "car_deadline_hour": 6,
    "car_value_dkk_per_kwh": 1.25,   # max price at the plug for charging beyond the minimum
    "car_from_house_battery": False, # let the plan feed the car from the house battery
    "car_start_surplus_w": 4200,
    "car_stop_surplus_w": 1500,      # stop threshold while the house battery is below car_buffer_soc
    "car_buffer_soc": 50,            # at/above this the house battery may buffer the car (stop at 0 W)
    "car_house_priority_soc": 80,    # below this the house battery's charging is reserved
    "car_house_min_soc": 30,         # never solar-charge the car with the house battery below this
    "car_min_charge_s": 1200,
    "car_cooldown_s": 1800,
    "car_interval_seconds": 120,
}

REGISTER_KEYS = [
    "reg_battery_first",
    "reg_max_soc",
    "reg_min_soc",
    "reg_charge_rate",
    "reg_grid_charge",
    "reg_export_limit",
]


def load_options(path: str = OPTIONS_FILE) -> dict:
    opts = dict(DEFAULTS)
    if os.path.exists(path):
        with open(path) as f:
            opts.update(json.load(f))
    return opts


def split_csv(value: str) -> list:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def local_tz(name: str = None):
    """The real IANA zone (with DST), from Supervisor's TZ. Never a fixed UTC offset."""
    for candidate in (name, os.environ.get("TZ"), "Europe/Copenhagen"):
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return ZoneInfo("UTC")


def counter_ids(opts: dict) -> dict:
    return {"import": opts["grid_import_counter"], "export": opts["grid_export_counter"],
            "battery_in": opts["battery_input_counter"], "battery_out": opts["battery_output_counter"],
            "solar": opts["solar_counter"]}
