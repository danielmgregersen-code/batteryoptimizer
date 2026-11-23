"""Add-on options. Defaults mirror config.yaml so the app also runs outside HA."""
import json
import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

OPTIONS_FILE = "/data/options.json"

DEFAULTS = {
    "price_buy_entity": "sensor.stromligning_current_price_vat",
    "price_sell_entity": "sensor.stromligning_production_price_vat",
    "price_buy_forecast_entity": "sensor.stromligning_forecasts_vat",
    "price_buy_fallback_entity": "sensor.kob_af_el",
    "price_sell_fallback_entity": "sensor.salg_af_el",
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
    "pv_dampening_days": 60,
    "pv_dampening_half_life_days": 14,
    "pv_dampening_manual": "",        # e.g. "14:0.3,15:0.2" = clear-sky factor for those hours
    "solcast_push": False,            # also set the factors in the Solcast integration (daily)
    "profile_days": 56,
    "profile_half_life_days": 10,
    "profile_last_year_weight": 0.5,
    "load_submeters": "sensor.shelly_varmepumpe_power,sensor.shelly_opvaskemaskine_power,sensor.bryggers_shelly_vask_og_torre_effekt,sensor.kokken_shelly_kogeplade_effekt",
    "load_submeters_steady": "sensor.shelly_varmepumpe_power",
    "load_submeters_smooth_hours": 2,
    "default_load_kw": 0.8,
    # Does sensor.growatt_total_house_load count the battery's charging as house load? On the
    # SPH10000TL3 it does not. auto = detect from the statistics, yes = always subtract.
    "load_includes_battery_charging": "no",
    "interval_minutes": 5,
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
    # --- car (replaces the "Solcelle- & Prisstyret ladning: IONIQ 9" automation) ---
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
    # 100 % now and then, only when power is at rock bottom (never forced), at most every N days
    "car_full_charge": True,
    "car_full_min_days": 14,
    "car_full_cheap_percentile": 5,    # night price among the N % cheapest hours of the last 30 days
    # A trip: the car at car_departure_soc (or the helper's value) at this date and time
    "car_departure_entity": "input_datetime.ioniq9_afgang",
    "car_departure_soc_entity": "input_number.ioniq9_afgang_soc",
    "car_departure_soc": 100,
    "car_start_surplus_w": 4200,
    "car_stop_surplus_w": 1500,      # stop threshold while the house battery is below car_buffer_soc
    "car_buffer_soc": 50,            # at/above this the house battery may buffer the car (stop at 0 W)
    "car_house_priority_soc": 80,    # below this the house battery's charging is reserved
    "car_house_min_soc": 30,         # never solar-charge the car with the house battery below this
    "car_min_charge_s": 1200,
    "car_cooldown_s": 1800,
    "car_interval_seconds": 120,
    # --- production: HA helpers that let the add-on write (off or missing = manual control, nothing written) ---
    "production_switch_entity": "input_boolean.energy_planner_produktion",
    "car_production_switch_entity": "input_boolean.energy_planner_bil_produktion",
    "car_device_id": "",
    "car_charge_switch_entity": "switch.ioniq_9_ev_charging",
    "car_soc_time_entity": "sensor.ioniq_9_last_updated_at",
    "car_pause_gap_hours": 2,
    "car_min_session_kwh": 2.0,
    "car_pause_min_saving_dkk": 0.20,
    "car_trace_prefixes": "",               # the IONIQ 9's device id for kia_uvo.start_charge / stop_charge
    # Emergency stop on the charger when the car cannot be paused through Kia, only while charging
    # from the grid (not on sun) and when nothing more is planned before the deadline. The THOR cannot
    # be restarted from here afterwards (the cable has to be re-plugged), but the car is not overcharged.
    "car_thor_stop_fallback": True,
    "charger_stop_button": "button.garage_growatt_thor_ev_charger_stop_charging",
    "charger_schedule_entities": "time.garage_growatt_thor_ev_charger_automatic_charging_start,time.garage_growatt_thor_ev_charger_automatic_charging_stop",
    "heat_pump_entity": "climate.varmepumpe",
    "heat_pump_setpoint": 12,
    "heat_pump_power_entity": "sensor.shelly_varmepumpe_power",
    "heat_pump_band": 2,
    "heat_pump_temp_entity": "sensor.lumi_lumi_weather_temperature",
    "heat_pump_humidity_entity": "sensor.lumi_lumi_weather_humidity",
    "heat_pump_room_min": 11,
    "heat_pump_room_max": 17,
    "heat_pump_humidity_max": 70,
    "heat_pump_off_hours": 4,
    "heat_pump_off_percentile": 90,
    "heat_pump_off_min_temp": 10,
    "appliances": "Opvask=sensor.shelly_opvaskemaskine_power,Vask+Tørretumbler=sensor.bryggers_shelly_vask_og_torre_effekt",
    "appliance_dryer_min_w": 700,
    "lifetime_value_entity": "sensor.power_sum_vaerdi_af_anlaeg_kr",
    "installation_date": "2023-07-01",
    "installation_cost_dkk": 120000,
    "goal_payback_years": 10,
    "installation_life_years": 25,
    "lifetime_price_change": "2026-01-01",
    "heat_pump_production_switch_entity": "input_boolean.energy_planner_varmepumpe_produktion",
    "battery_taper_start_pct": 90,
    "battery_taper_end_kw": 1.0,
    "export_control": True,
    "export_min_price": 0.0,
    "export_car_min_price": -0.10,
    "main_fuse_amps": 25,
    "fuse_margin_amps": 3,
    "grid_current_entities": "sensor.growatt_grid_current_l1,sensor.growatt_grid_current_l2,sensor.growatt_grid_current_l3",
}

# Registers the planner writes in production (the export limit only with export_control).
WRITE_REGISTER_KEYS = ["reg_battery_first", "reg_grid_charge", "reg_max_soc", "reg_min_soc", "reg_charge_rate"]


def write_register_keys(opts: dict) -> list:
    return WRITE_REGISTER_KEYS + (["reg_export_limit"] if opts.get("export_control") else [])

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
