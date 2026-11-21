from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import heatpump
from settings import DEFAULTS

TZ = ZoneInfo("Europe/Copenhagen")
NOW = datetime(2026, 11, 17, 12, 5, tzinfo=TZ)
OPTS = dict(DEFAULTS)


def rows(buys, sell=0.5, start=NOW.replace(minute=0)):
    return [{"t": (start + timedelta(hours=i)).isoformat(), "buy": b, "sell": sell, "pv_exp": 0.0, "pv_curt": 0.0}
            for i, b in enumerate(buys)]


def dear_first():
    return rows([3.0] + [1.0 + 0.05 * i for i in range(23)])


def cheap_first():
    return rows([0.5] + [1.0 + 0.05 * i for i in range(23)])


def mid_first():
    return rows([1.6] + [1.0 + 0.05 * i for i in range(23)])


def test_cheap_dear_and_normal_hours_move_the_setpoint_by_the_band():
    assert heatpump.decide(cheap_first(), NOW, 14.0, 50.0, OPTS)["setpoint"] == 14
    assert heatpump.decide(dear_first(), NOW, 14.0, 50.0, OPTS)["setpoint"] == 10
    d = heatpump.decide(mid_first(), NOW, 14.0, 50.0, OPTS)
    assert d["setpoint"] == 12 and d["level"] == "normal"
    assert len(d["plan"]) == 24 and sum(x["level"] == "cheap" for x in d["plan"]) == 8


def test_free_energy_counts_as_cheap():
    r = dear_first()
    r[0].update(pv_curt=1.2)
    assert heatpump.decide(r, NOW, 14.0, 50.0, OPTS)["setpoint"] == 14
    r = dear_first()
    r[0].update(pv_exp=2.0, sell=-0.05)
    assert heatpump.decide(r, NOW, 14.0, 50.0, OPTS)["setpoint"] == 14


def test_room_temperature_limits():
    OPTS = {**globals()["OPTS"], "heat_pump_off_hours": 0}                 # the setpoint rules alone
    d = heatpump.decide(dear_first(), NOW, 10.8, 50.0, OPTS)
    assert d["setpoint"] == 12 and "10.8" in d["reason"]                 # below 11: no lowering
    assert heatpump.decide(dear_first(), NOW, 9.8, 50.0, OPTS)["setpoint"] == 14    # below 10: warm up
    assert heatpump.decide(cheap_first(), NOW, 17.5, 50.0, OPTS)["setpoint"] == 12  # above 17: no raising


def test_humidity_and_missing_readings_never_lower():
    d = heatpump.decide(dear_first(), NOW, 14.0, 75.0, OPTS)
    assert d["setpoint"] == 12 and "fugt" in d["reason"]
    assert heatpump.decide(dear_first(), NOW, None, None, OPTS)["setpoint"] == 12
    assert heatpump.decide(cheap_first(), NOW, None, None, OPTS)["setpoint"] == 14


def test_learns_the_heat_pumps_use_per_level():
    from datetime import timezone
    now = datetime(2026, 11, 17, 12, tzinfo=timezone.utc)
    levels, power = {}, {}
    for i in range(1, 15 * 24):
        t = now - timedelta(hours=i)
        lv = ["cheap", "normal", "dear"][i % 3]
        heatpump.record_level(levels, t, lv, 12, applied=(i % 7 != 0), now_utc=now)
        power[t] = {"cheap": 450.0, "normal": 250.0, "dear": 80.0}[lv]
    got = heatpump.learn_levels(levels, power, now)
    assert got["cheap"]["kwh"] == 0.45 and got["normal"]["kwh"] == 0.25 and got["dear"]["kwh"] == 0.08
    assert got["cheap"]["hours"] < 15 * 8                              # hours without control are left out
    assert heatpump.learn_levels({}, power, now) == {}


def test_the_dearest_hours_switch_off_and_the_rest_of_the_dear_third_lowers():
    d = heatpump.decide(dear_first(), NOW, 14.0, 50.0, OPTS)
    assert d["hvac"] == "off" and d["level"] == "off" and "dyreste" in d["reason"]
    assert sum(x["level"] == "off" for x in d["plan"]) == 4
    assert sum(x["level"] in ("off", "dear") for x in d["plan"]) == 8
    late = rows([1.0 + 0.05 * i for i in range(23)] + [1.3])  # now: a cheap hour
    assert heatpump.decide(late, NOW, 14.0, 50.0, OPTS)["hvac"] == "on"
    assert heatpump.decide(dear_first(), NOW, 14.0, 50.0, {**OPTS, "heat_pump_off_hours": 0})["hvac"] == "on"


def test_a_dear_day_against_the_last_30_days_switches_off_longer():
    history = [1.0 + 0.01 * (i % 100) for i in range(720)]      # 1.00-1.99 kr, 90th percentile ~1.89
    day = rows([2.73 - 0.01 * i for i in range(24)])
    d = heatpump.decide(day, NOW, 14.0, 50.0, OPTS, history=history)
    assert d["hvac"] == "off" and "30 dage" in d["reason"]
    assert sum(x["level"] == "off" for x in d["plan"]) == 8      # the whole dear third, not only 4


def test_minimum_temperature_runs_whatever_the_price_with_hysteresis():
    d = heatpump.decide(dear_first(), NOW, 9.6, 50.0, OPTS)
    assert d["hvac"] == "on" and d["forced_on"] and d["setpoint"] == 14      # below room_min - 1: warms up
    d = heatpump.decide(dear_first(), NOW, 10.5, 50.0, OPTS, forced_on=True)
    assert d["hvac"] == "on" and d["forced_on"] and "uanset pris" in d["reason"]
    d = heatpump.decide(dear_first(), NOW, 11.2, 50.0, OPTS, forced_on=True)
    assert d["hvac"] == "off" and not d["forced_on"]


def test_humidity_missing_reading_and_sun_never_switch_off():
    assert heatpump.decide(dear_first(), NOW, 14.0, 75.0, OPTS)["hvac"] == "on"
    assert heatpump.decide(dear_first(), NOW, None, None, OPTS)["hvac"] == "on"
    r = dear_first()
    r[0].update(pv_exp=1.5, sell=0.8)
    assert heatpump.decide(r, NOW, 14.0, 50.0, OPTS)["hvac"] == "on"
