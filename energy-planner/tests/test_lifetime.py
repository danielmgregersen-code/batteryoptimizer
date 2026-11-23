from datetime import date, timedelta

import pytest

import lifetime as lm

INSTALL = date(2023, 7, 1)
CHANGE = date(2026, 1, 1)


def span(a, b):
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


def test_sources_by_priority_and_the_estimate_uses_pre_2026_rates_of_the_same_month():
    end = date(2026, 3, 31)
    solar = {d: (20.0 if 4 <= d.month <= 9 else 5.0) for d in span(INSTALL, end)}
    # old sensor from 25 Oct 2023: 1.0 kr/kWh before 2026, measured 2026 at 0.5 kr/kWh (lower tax)
    old = {d: solar[d] * (1.5 if d.month in (7, 8) else 1.0) for d in span(date(2023, 10, 25), date(2025, 12, 31))}
    measured = {d: solar[d] * 0.5 for d in span(CHANGE, end)}
    res = lm.daily_values(INSTALL, end, measured, old, solar, CHANGE)
    by = {x["day"]: x for x in res["days"]}
    assert by["2023-07-10"]["source"] == "estimated" and by["2023-07-10"]["value"] == pytest.approx(20.0 * 1.5)
    assert by["2023-09-10"]["value"] == pytest.approx(20.0 * 1.0)          # never the 2026 rate
    assert by["2024-05-01"]["source"] == "old" and by["2026-02-01"]["source"] == "measured"
    assert res["rates"][7] == pytest.approx(1.5) and res["rates"][1] == pytest.approx(1.0)


def test_days_without_solar_use_the_months_mean_value_per_day():
    end = date(2024, 12, 31)
    old = {d: 10.0 for d in span(date(2023, 10, 25), end)}
    res = lm.daily_values(INSTALL, end, {}, old, {}, CHANGE)
    assert res["days"][0]["source"] == "estimated" and res["days"][0]["value"] == pytest.approx(10.0)


def test_expected_per_year_and_payback():
    days = [{"day": d.isoformat(), "value": 30.0, "source": "old", "solar_kwh": 10.0} for d in span(INSTALL, date(2025, 6, 30))]
    s = lm.summary(days, 5.0, 110000, 25, INSTALL, date(2025, 7, 1), CHANGE)
    assert s["total_dkk"] == pytest.approx(30.0 * len(days) + 5.0, abs=1)
    assert s["expected_per_year_dkk"] == pytest.approx(30.0 * 365)
    left = 110000 - s["total_dkk"]
    assert s["payback_date"] == (date(2025, 7, 1) + timedelta(days=round(left / (30 * 365) * 365.25))).isoformat()
    assert not s["paid_back"] and s["paid_pct"] == pytest.approx(100 * s["total_dkk"] / 110000, abs=0.1)
    assert s["expected_life_dkk"] > s["total_dkk"] and s["end_of_life"] == "2048-07-01"
    rich = [dict(x, value=200.0) for x in days]
    s = lm.summary(rich, 0.0, 110000, 25, INSTALL, date(2025, 7, 1), CHANGE)
    assert s["paid_back"] and s["payback_date"] == (INSTALL + timedelta(days=549)).isoformat()   # day 550 passes 110 000


def test_after_the_tax_change_the_forecast_uses_only_the_new_days_scaled_by_solar():
    days = []
    for d in span(date(2024, 1, 1), date(2026, 6, 30)):
        sol = 20.0 if 4 <= d.month <= 9 else 5.0
        val = sol * (1.0 if d < CHANGE else 0.5)
        days.append({"day": d.isoformat(), "value": val, "source": "old", "solar_kwh": sol})
    exp = lm.expected_per_year(days, CHANGE, lm.solar_weights(days, CHANGE))
    true_year = sum((20.0 if 4 <= m <= 9 else 5.0) * 0.5 * lm._month_days(m) for m in range(1, 13))
    assert exp["dkk"] == pytest.approx(true_year, rel=0.02) and "siden 2026-01-01" in exp["basis"]


def test_goals_per_day_week_month_and_year():
    weights = {m: (2.0 if 4 <= m <= 9 else 1.0) for m in range(1, 13)}
    gpd = lm.goal_per_day(9000.0, weights)
    assert sum(gpd[m] * lm._month_days(m) for m in range(1, 13)) == pytest.approx(9000.0)
    assert gpd[7] > gpd[1]
    today = date(2026, 11, 19)                                            # a Thursday
    days = [{"day": d.isoformat(), "value": 30.0, "source": "measured", "solar_kwh": 5.0} for d in span(date(2025, 1, 1), today - timedelta(days=1))]
    g = lm.goals(days, 12.0, today, gpd, date(2048, 7, 1))
    assert g["days"][-1] == {"day": "2026-11-19", "value": 12.0, "goal": round(gpd[11], 2), "source": "today", "partial": True}
    w = g["periods"]["week"]
    assert w["value"] == pytest.approx(3 * 30 + 12) and w["goal_sofar"] == pytest.approx(round(4 * gpd[11]))
    assert w["goal"] == pytest.approx(round(7 * gpd[11]))
    assert g["periods"]["month"]["value"] == pytest.approx(18 * 30 + 12)
    assert g["months"][-1]["month"] == "2026-11" and g["months"][0]["month"] == "2025-01"
    assert g["forecast"][0]["month"] == "2026-11" and g["forecast"][0]["value"] == pytest.approx(round(gpd[11] * 11))
    assert g["forecast"][-1]["month"] == "2048-07"


def test_the_goal_is_payback_in_10_years_spread_by_the_historical_value():
    # spring earns most (sun and high prices), summer less, winter little - the goal follows the value
    val = {1: 3, 2: 8, 3: 30, 4: 55, 5: 60, 6: 40, 7: 35, 8: 30, 9: 25, 10: 12, 11: 5, 12: 2}
    days = [{"day": d.isoformat(), "value": float(val[d.month]), "source": "old", "solar_kwh": (30.0 if 6 <= d.month <= 8 else 4.0)}
            for d in span(date(2024, 1, 1), date(2025, 12, 31))]
    s = lm.summary(days, 0.0, 120000, 25, INSTALL, date(2026, 1, 15), CHANGE, goal_years=10)
    g = s["goals"]
    assert g["goal_per_year_dkk"] == 12000 and g["goal_payback_years"] == 10
    gpd = {int(k): v for k, v in g["goal_by_month"].items()}
    assert sum(gpd[m] * lm._month_days(m) for m in range(1, 13)) == pytest.approx(12000, abs=2)
    assert gpd[5] > gpd[7] > gpd[1]                                      # May above July, not by the sun
    assert gpd[5] / gpd[1] == pytest.approx(60 / 3, rel=0.01)


def test_a_glitch_in_the_old_sensor_is_left_out_and_estimated_instead():
    old = {d: 20.0 + (d.day % 7) for d in span(date(2024, 1, 1), date(2024, 12, 31))}
    old[date(2024, 6, 14)] = -2293.0
    solar = {d: 10.0 for d in old}
    res = lm.daily_values(date(2024, 1, 1), date(2024, 12, 31), {}, old, solar, CHANGE)
    by = {x["day"]: x for x in res["days"]}
    assert by["2024-06-14"]["source"] == "estimated" and 15 < by["2024-06-14"]["value"] < 30
    assert res["rejected"] == [{"day": "2024-06-14", "value": -2293.0, "source": "old"}]
    assert by["2024-06-13"]["source"] == "old"
