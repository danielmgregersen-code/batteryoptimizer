from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import appliances as appl
from summary import appliance_advice

TZ = ZoneInfo("Europe/Copenhagen")
UTC = timezone.utc
T0 = datetime(2026, 11, 20, 18, 0, tzinfo=UTC)


def series(*segments, start=T0):
    """segments: (minutes, W) -> 5-minute statistics rows."""
    rows, t = [], start
    for minutes, w in segments:
        for _ in range(minutes // 5):
            rows.append({"start": t.timestamp() * 1000, "mean": w})
            t += timedelta(minutes=5)
    return rows


def test_runs_are_split_and_short_pauses_kept_inside():
    rows = series((30, 2000), (10, 0), (60, 150), (60, 0), (90, 2200))
    runs = appl.split_runs(rows)
    assert len(runs) == 2
    a, b = runs
    assert a["minutes"] == 100 and a["kwh"] == pytest.approx(1.0 + 0.15, abs=0.01)
    assert a["profile"][0] > a["profile"][1]                     # the heating at the start
    assert b["kwh"] == pytest.approx(3.3, abs=0.01) and b["mean_w"] == 2200
    assert appl.split_runs(series((10, 300))) == []              # 0.05 kWh: not a run


def test_washer_and_dryer_on_one_meter_are_told_apart_by_power():
    washer, dryer = appl.split_runs(series((20, 2000), (90, 120), (60, 0), (100, 1800)))
    assert appl.classify(washer, 700) == 0 and appl.classify(dryer, 700) == 1


def test_program_defaults_until_three_runs_then_the_median():
    assert appl.parse_config("Opvask=sensor.a, Vask+Tørretumbler=sensor.b") == [
        {"entity": "sensor.a", "names": ["Opvask"]}, {"entity": "sensor.b", "names": ["Vask", "Tørretumbler"]}]
    p = appl.program("Opvask", [])
    assert not p["learned"] and p["minutes"] == 150 and sum(p["profile"]) == pytest.approx(1.0)
    assert appl.program("Tørretumbler", [])["kwh"] == 2.0
    runs = [{"start": f"2026-11-1{i}T18:00:00+00:00", "minutes": m, "kwh": k, "mean_w": 900, "profile": [k * 0.8, k * 0.2]}
            for i, (m, k) in enumerate([(110, 1.1), (120, 1.0), (115, 0.9)])]
    store = appl.remember({}, "Opvask", runs)
    store = appl.remember(store, "Opvask", runs[:1])             # no duplicates
    p = appl.program("Opvask", store["Opvask"])
    assert p["learned"] and p["minutes"] == 115 and p["kwh"] == 1.0 and p["profile"] == [0.8, 0.2]


def rows(buys, sell=0.8, exp=None, start=datetime(2026, 11, 20, 20, 0, tzinfo=TZ)):
    exp = exp or {}
    return [{"t": (start + timedelta(hours=i)).isoformat(), "buy": b, "sell": sell, "dt": 1.0,
             "pv_exp": exp.get(i, 0.0), "pv_curt": 0.0} for i, b in enumerate(buys)]


def test_best_start_finds_the_cheap_night_and_the_sun():
    prog = {"minutes": 120, "kwh": 1.0, "profile": [0.6, 0.4]}
    now = datetime(2026, 11, 20, 20, 10, tzinfo=TZ)
    night = rows([3.0, 2.8, 2.5, 2.0, 1.6, 1.5, 1.5, 1.7, 2.2, 2.5])
    b = appl.best_start(prog, night, now)
    assert not b["now"] and b["start"].startswith("2026-11-21T01:00") and b["cost_dkk"] == pytest.approx(1.5)
    assert b["saving_dkk"] == pytest.approx(0.6 * 3.0 + 0.4 * 2.8 - 1.5)
    sunny = rows([2.0] * 6, sell=0.5, exp={3: 2.0, 4: 2.0})
    b = appl.best_start(prog, sunny, now)
    assert b["solar"] and b["cost_dkk"] == pytest.approx(0.5)
    flat = rows([2.0, 2.0, 1.9, 2.0])
    assert appl.best_start(prog, flat, now)["now"]               # saves under 0.25 kr: start now


def test_card_texts():
    now = datetime(2026, 11, 20, 20, 10, tzinfo=TZ)
    items = [{"name": "Opvask", "start": "2026-11-21T03:00+01:00", "now": False, "cost_dkk": 0.9, "saving_dkk": 1.6, "solar": False},
             {"name": "Tørretumbler", "start": "2026-11-21T13:00+01:00", "now": False, "cost_dkk": 0.4, "saving_dkk": 4.1, "solar": True},
             {"name": "Vask", "start": now.isoformat(), "now": True, "cost_dkk": 1.6, "saving_dkk": 0.1, "solar": False}]
    st, a = appliance_advice(items, now)
    assert st == 3 and a["title"] == "Opvask i nat kl. 03 · Tørretumbler i morgen kl. 13 · Vask nu"
    assert a["lines"][0] == "Opvask: i nat kl. 03 · 0,9 kr, sparer 1,6 kr mod nu"
    assert a["lines"][1] == "Tørretumbler: i morgen kl. 13 · 0,4 kr på sol, sparer 4,1 kr mod nu"
    assert a["lines"][2].startswith("Vask: start nu (ingen billigere tid")
