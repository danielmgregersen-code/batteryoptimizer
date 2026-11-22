from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import economy

TZ = ZoneInfo("Europe/Copenhagen")
UTC = timezone.utc
IDS = {"import": "imp", "export": "exp", "solar": "sol", "battery_in": "bin", "battery_out": "bout",
       "buy": "buy", "sell": "sell", "car": "car"}


def stats_for(hours: dict):
    """hours: {local datetime: dict(imp, exp, sol, bin, bout, buy, sell, car_w)} -> statistics rows."""
    out = {k: [] for k in IDS.values()}
    for t, v in hours.items():
        ms = t.astimezone(UTC).timestamp() * 1000
        for key, sid in (("imp", "imp"), ("exp", "exp"), ("sol", "sol"), ("bin", "bin"), ("bout", "bout")):
            if key in v:
                out[sid].append({"start": ms, "change": v[key]})
        for key in ("buy", "sell"):
            if key in v:
                out[key].append({"start": ms, "mean": v[key]})
        out["car"].append({"start": ms, "mean": v.get("car_w", 0.0)})
    return out


def test_an_hour_by_hand():
    t = datetime(2026, 11, 14, 12, tzinfo=TZ)
    # 3 kWh sun: 1 kWh to the house, 1.5 kWh to the battery, 0.5 kWh sold; 0.4 kWh bought
    res = economy.compute(stats_for({t: dict(imp=0.4, exp=0.5, sol=3.0, bin=1.5, bout=0.0, buy=2.0, sell=1.2)}), IDS, TZ)
    h = res["hours"][0]
    assert h["consumption_kwh"] == pytest.approx(0.4 - 0.5 + 3.0 - 1.5)          # 1.4 kWh
    assert h["net_dkk"] == pytest.approx(0.4 * 2.0 - 0.5 * 1.2)
    assert h["without_dkk"] == pytest.approx(1.4 * 2.0)
    assert h["value_dkk"] == pytest.approx(1.4 * 2.0 - (0.4 * 2.0 - 0.5 * 1.2))
    # charging 1.5 kWh from the sun costs the sale it could have made
    assert h["battery_value_dkk"] == pytest.approx(-1.5 * 1.2)
    assert h["solar_value_dkk"] + h["battery_value_dkk"] == pytest.approx(h["value_dkk"])


def test_battery_value_negative_when_grid_charging_positive_in_the_peak():
    night = datetime(2026, 11, 14, 3, tzinfo=TZ)
    peak = datetime(2026, 11, 14, 18, tzinfo=TZ)
    res = economy.compute(stats_for({
        night: dict(imp=4.5, exp=0.0, sol=0.0, bin=4.0, bout=0.0, buy=1.2, sell=0.8),
        peak: dict(imp=0.0, exp=0.0, sol=0.0, bin=0.0, bout=1.9, buy=3.1, sell=1.5),
    }), IDS, TZ)
    n, p = res["hours"]
    assert n["battery_value_dkk"] == pytest.approx(-4.0 * 1.2) and n["solar_value_dkk"] == pytest.approx(0.0)
    assert p["battery_value_dkk"] == pytest.approx(1.9 * 3.1) and p["value_dkk"] == pytest.approx(1.9 * 3.1)
    day = res["days"][0]
    assert day["battery_value_dkk"] == pytest.approx(1.9 * 3.1 - 4.0 * 1.2)      # the night charge paid off
    assert res["totals"]["value_dkk"] == pytest.approx(day["value_dkk"])


def test_car_cost_and_shares():
    t = datetime(2026, 11, 14, 4, tzinfo=TZ)
    res = economy.compute(stats_for({t: dict(imp=11.5, exp=0.0, sol=0.0, bin=0.0, bout=0.0, buy=1.9, sell=1.2,
                                              car_w=11000.0)}), IDS, TZ)
    tot = res["totals"]
    assert tot["car_kwh"] == pytest.approx(11.0) and tot["car_dkk"] == pytest.approx(11.0 * 1.9)
    assert tot["self_sufficiency_pct"] == pytest.approx(0.0) and tot["avg_buy_paid"] == pytest.approx(1.9)


def test_price_fallback_from_the_log_and_skipped_hours():
    t1 = datetime(2026, 11, 14, 10, tzinfo=TZ)
    t2 = t1 + timedelta(hours=1)
    t3 = t1 + timedelta(hours=2)
    stats = stats_for({t1: dict(imp=1.0, exp=0.0, sol=0.0), t2: dict(imp=1.0, exp=0.0, sol=0.0),
                       t3: dict(exp=0.0)})                    # t3 has no import counter
    log = [{"updated": (t1 + timedelta(minutes=m)).isoformat(), "price_now": {"buy": 2.0 + m / 100, "sell": 1.0}}
           for m in (0, 30)]
    res = economy.compute(stats, IDS, TZ, decisions=log)
    assert len(res["hours"]) == 1 and res["hours"][0]["buy"] == pytest.approx(2.15)   # t1 via the log
    assert res["skipped_hours"] == 2 and res["totals"]["fallback_price_hours"] == 1
    assert set(res["missing"]) == {"buy", "sell", "bin", "bout"}


def test_days_follow_local_dates_across_dst():
    # 2026-10-25: summer time ends in Denmark, the local day has 25 hours
    start = datetime(2026, 10, 24, 22, tzinfo=UTC)
    hours = {}
    for i in range(26):
        t = start + timedelta(hours=i)       # UTC keys: the two local 02:00 hours compare equal in TZ
        hours[t] = dict(imp=1.0, exp=0.0, sol=0.0, bin=0.0, bout=0.0, buy=1.0, sell=0.5)
    res = economy.compute(stats_for(hours), IDS, TZ)
    d = {x["day"]: x for x in res["days"]}
    assert d["2026-10-25"]["hours"] == 25 and d["2026-10-25"]["import_kwh"] == pytest.approx(25.0)


def test_report_fetches_from_local_midnight_and_switches_to_days():
    calls = []

    class HA:
        def statistics(self, ids, start, end, period="hour"):
            calls.append((ids, start, end, period))
            return {}
    opts = {"grid_import_counter": "imp", "grid_export_counter": "exp", "solar_counter": "sol",
            "battery_input_counter": "bin", "battery_output_counter": "bout", "price_buy_entity": "buy",
            "price_sell_entity": "sell", "car_power_entity": "car"}
    now = datetime(2026, 11, 14, 13, 0, tzinfo=UTC)
    r1 = economy.report(HA(), opts, TZ, 1, now)
    assert calls[0][1] == datetime(2026, 11, 14, 0, 0, tzinfo=TZ) and calls[0][3] == "hour"
    assert "hours" in r1 and r1["period_days"] == 1
    r30 = economy.report(HA(), opts, TZ, 30, now)
    assert "hours" not in r30 and calls[1][1] == datetime(2026, 10, 16, 0, 0, tzinfo=TZ)
    assert len(r30["missing"]) == 7


def test_self_sufficiency_does_not_count_grid_charging_as_consumption():
    night = datetime(2026, 11, 14, 3, tzinfo=TZ)
    peak = datetime(2026, 11, 14, 18, tzinfo=TZ)
    res = economy.compute(stats_for({
        night: dict(imp=4.5, exp=0.0, sol=0.0, bin=4.0, bout=0.0, buy=1.2, sell=0.8),   # 0.5 house, 4 battery
        peak: dict(imp=0.1, exp=0.0, sol=0.0, bin=0.0, bout=1.9, buy=3.1, sell=1.5),    # 2.0 house, 1.9 battery
    }), IDS, TZ)
    d = res["days"][0]
    # consumption 0.5 + 2.0 = 2.5 kWh; from the grid directly 0.5 + 0.1 = 0.6 kWh -> 76 % self-supplied
    assert d["consumption_kwh"] == pytest.approx(2.5)
    assert d["self_sufficiency_pct"] == pytest.approx(76.0, abs=0.1)
    assert 0 <= d["self_sufficiency_pct"] <= 100


def test_new_price_sensor_state_with_the_old_sensors_mean_for_its_gaps():
    t1, t2 = datetime(2026, 11, 20, 10, tzinfo=TZ), datetime(2026, 11, 20, 11, tzinfo=TZ)
    stats = stats_for({t: dict(imp=1.0, exp=0.0, sol=0.0, bin=0.0, bout=0.0) for t in (t1, t2)})
    ms = lambda t: t.astimezone(UTC).timestamp() * 1000  # noqa: E731
    stats.update(new_buy=[{"start": ms(t2), "state": 2.5}], buy=[{"start": ms(t1), "mean": 2.0}, {"start": ms(t2), "mean": 9.0}],
                 new_sell=[{"start": ms(t2), "mean": 1.1}], sell=[{"start": ms(t1), "mean": 1.0}])
    ids = {**IDS, "buy": "new_buy", "sell": "new_sell", "buy_fallback": "buy", "sell_fallback": "sell"}
    res = economy.compute(stats, ids, TZ)
    assert [(h["buy"], h["sell"]) for h in res["hours"]] == [(2.0, 1.0), (2.5, 1.1)]
    assert res["missing"] == []


def test_sun_stored_in_battery_first_while_the_house_buys_costs_the_buy_price():
    t = datetime(2026, 11, 20, 11, tzinfo=TZ)
    # 1.2 kWh sun all into the battery (battery first), the house buys its 0.8 kWh meanwhile
    stats = stats_for({t: dict(imp=0.8, exp=0.0, sol=1.2, bin=1.2, bout=0.0, buy=2.0, sell=1.0)})
    plain = economy.compute(stats, IDS, TZ)["hours"][0]
    assert plain["battery_value_dkk"] == pytest.approx(-1.2 * 1.0)          # no log: as a lost sale
    log = [{"updated": (t + timedelta(minutes=m)).isoformat(), "setpoints": {"battery_first": True},
            "price_now": {"buy": 2.0, "sell": 1.0}} for m in range(0, 60, 5)]
    h = economy.compute(stats, IDS, TZ, decisions=log)["hours"][0]
    # 0.8 kWh of it would have covered the house (buy price), 0.4 kWh would have been sold
    assert h["battery_value_dkk"] == pytest.approx(-(0.8 * 2.0 + 0.4 * 1.0))
    assert h["solar_value_dkk"] + h["battery_value_dkk"] == pytest.approx(h["value_dkk"])
    half = [dict(e, setpoints={"battery_first": i % 2 == 0}) for i, e in enumerate(log)]
    h = economy.compute(stats, IDS, TZ, decisions=half)["hours"][0]
    assert h["battery_value_dkk"] == pytest.approx(-(0.4 * 2.0 + 0.8 * 1.0))
