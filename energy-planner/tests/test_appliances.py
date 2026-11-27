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
# ---- washer and heat pump dryer on one meter, shaped as measured (Nov 2026) ---------------------
# The washer: a 1–2 kW heating spike, then a steady 45–90 W, a 200–450 W spin at the end.
# The heat pump dryer: 350–850 W swinging every 5–10 minutes, rising over its first half hour.
LOW = [75, 74, 79, 77, 72, 73, 73, 75, 68, 67, 73, 72, 68, 76, 72, 72, 67, 71, 71, 67, 64, 72, 68, 50, 39, 69]
DRY = [430, 610, 670, 712, 761, 380, 640, 500, 375, 551, 571, 365, 400, 525, 538, 371, 370, 501, 509, 450,
       354, 374, 489, 490, 447, 351, 394, 500, 493, 360, 552, 351, 354, 472, 549]          # 175 min, ~1.4 kWh


def rows_of(watts, start=T0):
    return [{"start": (start + timedelta(minutes=5 * i)).timestamp() * 1000, "mean": w} for i, w in enumerate(watts)]


def kwh(watts):
    return sum(watts) / 12000.0


def sport():
    return [28, 1012] + LOW[:6] + [91, 120]                                  # 50 min, ~0.13 kWh


def w20():
    return [24, 40, 820, 290] + LOW + [105, 123, 106, 80, 190, 218, 370, 185]   # ~3 h, ~0.36 kWh


def eco60():
    return [24, 32, 574, 1953, 1940, 1933, 1764] + LOW + LOW[:8] + [95, 241, 456, 289]   # ~3.6 h, ~0.97 kWh


def history(*progs):
    rows, t = [], T0
    for f in progs:
        rows += rows_of(f(), start=t)
        t += timedelta(hours=6)
    return rows, t


def split(watts, programs=None):
    return appl.split_shared(rows_of(watts), 200, 1300, programs=programs)


def test_washes_alone_are_washes_and_dryings_alone_are_the_dryer():
    for f in (sport, w20, eco60):
        assert [k for k, _ in split(f())] == [0]
    out = split(DRY)
    assert [k for k, _ in out] == [1] and out[0][1]["kwh"] == pytest.approx(kwh(DRY), abs=0.01)


def test_the_dryer_is_found_whatever_appliance_dryer_min_w_says():
    assert appl.dryer_band({"appliance_dryer_min_w": 400})[0] == appl.DRYER_ON_W
    assert appl.dryer_band({"appliance_dryer_min_w": 700, "appliance_dryer_max_w": 1300}) == (appl.DRYER_ON_W, 1300)


def test_a_wash_straight_followed_by_the_dryer_is_two_runs():
    wash = w20()
    out = split(wash + DRY)
    assert [k for k, _ in out] == [0, 1]
    assert out[0][1]["kwh"] == pytest.approx(kwh(wash), rel=0.1)
    assert out[1][1]["kwh"] == pytest.approx(kwh(DRY), rel=0.1)


def test_a_wash_starting_during_the_drying_without_programmes():
    total = DRY + [0] * 30
    wash = eco60()
    for i, x in enumerate(wash):
        total[5 + i] += x
    total = [x for x in total]
    while total and total[-1] == 0:
        total.pop()
    out = split(total)
    by = {k: sum(r["kwh"] for kk, r in out if kk == k) for k in (0, 1)}
    assert by[1] == pytest.approx(kwh(DRY), rel=0.1)
    assert by[0] == pytest.approx(kwh(wash), rel=0.2)
    assert by[0] + by[1] == pytest.approx(kwh(total), abs=0.01)


def test_washes_that_ran_alone_give_the_programmes():
    rows, _ = history(sport, w20, eco60, sport, w20, eco60)
    progs = appl.wash_programs(appl.wash_only(rows, 200, 1300))
    by = {p["name"]: p for p in progs}
    assert set(by) == {"Sport", "20°", "60° eco"}
    assert by["60° eco"]["kwh"] == pytest.approx(kwh(eco60()), rel=0.05)
    assert appl.wash_programs(appl.wash_only(history(sport, w20, eco60)[0], 200, 1300)) == []   # once each: not enough


def test_a_programme_under_the_dryer_is_the_washer_and_the_rest_the_dryer():
    rows, _ = history(sport, w20, eco60, sport, w20, eco60)
    progs = appl.wash_programs(appl.wash_only(rows, 200, 1300))
    wash = eco60()
    total = DRY + [0] * 20
    for i, x in enumerate(wash):
        total[5 + i] += x
    while total[-1] == 0:
        total.pop()
    out = split(total, progs)
    washes = [r for k, r in out if k == 0]
    assert [r.get("program") for r in washes] == ["60° eco"]
    assert sum(r["kwh"] for r in washes) == pytest.approx(kwh(wash), rel=0.15)
    assert sum(r["kwh"] for _, r in out) == pytest.approx(kwh(total), abs=0.01)


# ---- the real meter: 17–27 Nov 2026, 8 washes, 5 dryings and one of both at once -------------------

def _real():
    import json
    import os
    fx = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "laundry_5min.json")))
    t0 = datetime.fromisoformat(fx["start"])
    return [{"start": (t0 + timedelta(minutes=5 * i)).isoformat(), "mean": w} for i, w in enumerate(fx["w"])]


def test_the_real_meter_is_split_into_its_washes_and_dryings():
    rows = _real()
    progs = appl.wash_programs(appl.wash_only(rows, 200, 1300))
    out = appl.split_shared(rows, 200, 1300, programs=progs)
    local = lambda r: datetime.fromisoformat(r["start"]).astimezone(TZ).strftime("%d %H:%M")   # noqa: E731
    dryings = {local(r): r["kwh"] for k, r in out if k == 1}
    washes = {local(r): r["kwh"] for k, r in out if k == 0}
    assert set(dryings) == {"17 15:45", "17 20:30", "19 18:40", "22 01:35", "26 15:15", "26 20:10"}
    assert set(washes) == {"17 07:00", "19 12:25", "20 16:00", "21 12:45", "21 19:00", "22 12:25", "24 13:05",
                           "26 10:45", "26 15:40"}
    assert dryings["19 18:40"] == pytest.approx(2.16, abs=0.05) and dryings["22 01:35"] == pytest.approx(2.0, abs=0.05)
    assert 1.6 <= dryings["26 15:15"] <= 1.95 and 0.35 <= washes["26 15:40"] <= 0.7    # both at once
    total = sum(x["mean"] for x in rows) / 12000.0
    assert sum(r["kwh"] for _, r in out) == pytest.approx(total, abs=0.15)          # all but tiny standby
    prog = appl.program("Tørretumbler", [r for k, r in out if k == 1])
    assert prog["learned"] and 1.3 <= prog["kwh"] <= 2.0 and prog["minutes"] > 120
    assert max(prog["profile"]) < 0.7                                                # spread over the hours


def test_a_dryer_is_learned_from_its_first_run_and_a_programme_becomes_a_program():
    run = {"start": "2026-11-20T10:00:00+00:00", "minutes": 135, "kwh": 1.4, "mean_w": 620, "profile": [0.6, 0.6, 0.2]}
    prog = appl.program("Tørretumbler", [run])
    assert prog["learned"] and prog["kwh"] == 1.4 and prog["mean_w"] == 620
    assert not appl.program("Vask", [run])["learned"]
    p = {"name": "60° eco", "minutes": 170, "kwh": 0.8, "runs": 2, "template": eco60()}
    pr = appl.program_from_template(p)
    assert pr["minutes"] == 170 and len(pr["profile"]) == -(-len(eco60()) // 12)
    assert sum(pr["profile"]) == pytest.approx(kwh(eco60()), abs=0.01)


def test_weighted_program_averages_the_programmes_by_use():
    p20 = {"name": "20°", "minutes": 80, "runs": 3, "starts": ["a", "b", "c"], "template": w20()}
    p60 = {"name": "60° eco", "minutes": 170, "runs": 1, "starts": ["d"], "template": eco60()}
    under = [{"start": "x", "program": "60° eco"}, {"start": "d", "program": "60° eco"}]   # one new, one already counted
    prog = appl.weighted_program([p20, p60], under)
    assert [(m["name"], m["runs"]) for m in prog["mix"]] == [("20°", 3), ("60° eco", 2)]
    assert prog["kwh"] == pytest.approx((3 * kwh(w20()) + 2 * kwh(eco60())) / 5, abs=0.01)
    assert prog["minutes"] == round((3 * 80 + 2 * 170) / 5)
    assert sum(prog["profile"]) == pytest.approx(prog["kwh"], abs=0.01)
