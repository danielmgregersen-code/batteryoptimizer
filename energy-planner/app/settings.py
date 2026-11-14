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
    "car_power_entity": "sensor.garage_growatt_thor_ev_charger_charging_power",
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
    "profile_days": 28,
    "default_load_kw": 0.8,
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
