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


# ---- one meter, washer and heat pump dryer, split slot by slot (0.34.0) ---------------------------
WASH20 = [180, 0, 220, 160, 0, 240, 200, 0, 190, 230, 170, 0, 210, 180, 500, 500]   # 80 min, drum and pauses, a spin


def rows_of(watts, start=T0):
    return [{"start": (start + timedelta(minutes=5 * i)).timestamp() * 1000, "mean": w} for i, w in enumerate(watts)]


def kwh(watts):
    return sum(watts) / 12000.0


def test_a_wash_straight_followed_by_the_dryer_is_two_runs():
    dry = [750, 780, 720, 760, 740, 770] * 4 + [150, 150, 150]
    out = appl.split_shared(rows_of(WASH20 + dry), 400, 1300)
    assert [k for k, _ in out] == [0, 1]
    assert out[0][1]["kwh"] == pytest.approx(kwh(WASH20), rel=0.1)
    assert out[1][1]["kwh"] == pytest.approx(kwh(dry), rel=0.1)


def test_a_heat_pump_dryer_under_700_w_on_average_is_the_dryer():
    dry = [650] * 24 + [120] * 6                                                  # mean well under 700 W
    out = appl.split_shared(rows_of(dry), 400, 1300)
    assert [k for k, _ in out] == [1] and out[0][1]["mean_w"] < 700


def test_a_60_eco_wash_with_a_short_heating_is_the_washer():
    out = appl.split_shared(rows_of([200, 1800, 1800, 1800] + WASH20[:-2] + [500, 500]), 400, 1300)
    assert [k for k, _ in out] == [0]


def test_both_running_at_once_are_told_apart():
    dry = [750, 780, 720, 760, 740, 770] * 5 + [150, 150, 150]                   # 150 min + cool-down
    total = list(dry)
    for i, x in enumerate(WASH20):                                                # the washer starts 30 min in
        total[6 + i] += x
    out = appl.split_shared(rows_of(total), 400, 1300)
    by = {k: r for k, r in out}
    assert set(by) == {0, 1}
    assert by[1]["kwh"] == pytest.approx(kwh(dry), rel=0.1)
    assert by[0]["kwh"] == pytest.approx(kwh(WASH20), rel=0.15)
    assert sum(r["kwh"] for _, r in out) == pytest.approx(kwh(total), abs=0.01)


def test_one_dip_inside_the_drying_does_not_split_it():
    dry = [750] * 12 + [300] + [750] * 12 + [150, 150]
    out = appl.split_shared(rows_of(dry), 400, 1300)
    assert [k for k, _ in out] == [1]


# ---- the washer's programmes as fingerprints (0.35.0) ----------------------------------------------
DRUM = [180, 0, 220, 160, 0, 240, 200, 0, 190, 230, 170, 0, 210, 180, 150, 200, 0, 220, 190, 160]


def sport():
    return [450] + DRUM[:6] + [520, 520]                    # 45 min, a small spike


def w20():
    return [600] + DRUM[:13] + [520, 520]                   # 80 min


def eco60():
    return [1800, 1500] + (DRUM * 2)[:30] + [520, 520]       # 170 min, a big spike


def history(*progs):
    rows, t = [], T0
    for f in progs:
        rows += rows_of(f(), start=t)
        t += timedelta(hours=6)
    return rows, t


def test_washes_that_ran_alone_give_the_programmes():
    rows, _ = history(sport, w20, eco60, sport, w20, eco60)
    progs = appl.wash_programs(appl.wash_only(rows, 400, 1300))
    by = {p["name"]: p for p in progs}
    assert set(by) == {"Sport", "20°", "60° eco"}
    assert by["Sport"]["minutes"] == 45 and by["20°"]["minutes"] == 80 and by["60° eco"]["minutes"] == 170
    assert by["60° eco"]["kwh"] == pytest.approx(kwh(eco60()), rel=0.05)
    assert appl.wash_programs(appl.wash_only(history(sport, w20, eco60)[0], 400, 1300)) == []   # once each: not enough


def test_a_programme_under_the_dryer_is_the_washer_and_the_rest_the_dryer():
    rows, t = history(sport, w20, eco60, sport, w20, eco60)
    progs = appl.wash_programs(appl.wash_only(rows, 400, 1300))
    dry = [750, 780, 720, 760, 740, 770] * 5 + [150, 150, 150]
    e = eco60()
    total = dry + [0] * (4 + len(e) - len(dry))
    for i, x in enumerate(e):
        total[4 + i] += x
    out = appl.split_shared(rows_of(total, start=t), 400, 1300, programs=progs)
    washes = [r for k, r in out if k == 0]
    assert len(washes) == 1 and washes[0]["program"] == "60° eco"
    assert washes[0]["kwh"] == pytest.approx(kwh(e), rel=0.05)
    assert sum(r["kwh"] for _, r in out) == pytest.approx(kwh(total), abs=0.01)


def test_two_washes_under_one_long_drying():
    rows, t = history(sport, w20, sport, w20)
    progs = appl.wash_programs(appl.wash_only(rows, 400, 1300))
    total = [750, 780, 720, 760, 740, 770] * 8 + [150, 150, 150]
    for i, x in enumerate(sport()):
        total[2 + i] += x
    for i, x in enumerate(w20()):
        total[20 + i] += x
    out = appl.split_shared(rows_of(total, start=t), 400, 1300, programs=progs)
    assert [r.get("program") for k, r in out if k == 0] == ["Sport", "20°"]


def test_an_unknown_programme_falls_back_to_the_floor():
    rows, t = history(sport, sport)
    progs = appl.wash_programs(appl.wash_only(rows, 400, 1300))
    dry = [750, 780, 720, 760, 740, 770] * 5 + [150, 150, 150]
    total = list(dry)
    for i, x in enumerate(WASH20):
        total[6 + i] += x
    with_p = appl.split_shared(rows_of(total, start=t), 400, 1300, programs=progs)
    without = appl.split_shared(rows_of(total, start=t), 400, 1300)
    assert [(k, r["kwh"]) for k, r in with_p] == [(k, r["kwh"]) for k, r in without]
