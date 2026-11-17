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
    d = heatpump.decide(dear_first(), NOW, 10.8, 50.0, OPTS)
    assert d["setpoint"] == 12 and "10.8" in d["reason"]                 # below 11: no lowering
    assert heatpump.decide(dear_first(), NOW, 9.8, 50.0, OPTS)["setpoint"] == 14    # below 10: warm up
    assert heatpump.decide(cheap_first(), NOW, 17.5, 50.0, OPTS)["setpoint"] == 12  # above 17: no raising


def test_humidity_and_missing_readings_never_lower():
    d = heatpump.decide(dear_first(), NOW, 14.0, 75.0, OPTS)
    assert d["setpoint"] == 12 and "fugt" in d["reason"]
    assert heatpump.decide(dear_first(), NOW, None, None, OPTS)["setpoint"] == 12
    assert heatpump.decide(cheap_first(), NOW, None, None, OPTS)["setpoint"] == 14
