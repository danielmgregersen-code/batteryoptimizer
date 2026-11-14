from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from evaluation import actual_category, evaluate, suggested_p10_weight

TZ = ZoneInfo("Europe/Copenhagen")
IDS = {"import": "imp", "export": "exp", "battery_in": "bin", "battery_out": "bout", "solar": "pv"}


def entry(t, mode, regs, imp, exp, pv, buy=2.0, sell=1.0, pred_pv=1.0, p50=1.2, p10=0.4, load=0.5, car_w=0.0):
    return {"updated": t.isoformat(), "mode": mode, "actual_registers": regs,
            "counters": {"imp": imp, "exp": exp, "pv": pv, "bin": 0.0, "bout": 0.0},
            "price_now": {"buy": buy, "sell": sell},
            "pred": {"pv_kw": pred_pv, "pv_p50_kw": p50, "pv_p10_kw": p10, "load_kw": load},
            "samples": {"car_w": car_w}, "pv_p10_weight": 0.3}


BF = {"reg_battery_first": "Enabled", "reg_grid_charge": "Disabled"}
GRID = {"reg_battery_first": "Enabled", "reg_grid_charge": "Enabled"}
LOAD = {"reg_battery_first": "Disabled", "reg_grid_charge": "Disabled"}


def test_actual_category_from_registers():
    assert actual_category(GRID) == "grid"
    assert actual_category(BF) == "battery_first"
    assert actual_category(LOAD) == "load_first"
    assert actual_category({"reg_battery_first": "unavailable"}) is None
    assert actual_category({}) is None


def test_agreement_cost_and_runs():
    t = datetime(2026, 11, 14, 10, 0, tzinfo=TZ)
    log = [
        entry(t, "grid_charge", GRID, 0.0, 0.0, 0.0),
        entry(t + timedelta(minutes=15), "hold", BF, 1.0, 0.0, 0.25),      # agrees (battery first)
        entry(t + timedelta(minutes=30), "discharge", BF, 1.5, 0.5, 0.5),  # disagrees
        entry(t + timedelta(minutes=45), "discharge", LOAD, 1.5, 1.0, 0.75),
    ]
    res = evaluate(log, IDS, TZ)
    d = res["days"][0]
    assert d["agreement_pct"] == pytest.approx(66.7, abs=0.1)
    assert d["import_kwh"] == 1.5 and d["export_kwh"] == 1.0
    assert d["cost_dkk"] == pytest.approx(1.5 * 2.0 - 1.0 * 1.0)
    assert d["avg_buy_paid"] == pytest.approx(2.0)
    assert d["pv_actual_kwh"] == pytest.approx(0.75) and d["pv_plan_kwh"] == pytest.approx(0.75)
    assert [r["planned"] for r in res["timeline"]] == ["grid", "battery_first", "load_first"]
    assert res["summary"]["agreement_pct"] == pytest.approx(66.7, abs=0.1)


def test_gaps_and_counter_resets_are_skipped():
    t = datetime(2026, 11, 14, 10, 0, tzinfo=TZ)
    log = [entry(t, "hold", BF, 10.0, 0.0, 0.0),
           entry(t + timedelta(hours=3), "hold", BF, 12.0, 0.0, 0.0),        # restart gap: ignored
           entry(t + timedelta(hours=3, minutes=15), "hold", BF, 0.5, 0.0, 0.0)]   # counter reset: no kWh
    d = evaluate(log, IDS, TZ)["days"][0]
    assert d["import_kwh"] == 0.0 and d["hours"] == pytest.approx(0.25)


def test_suggested_p10_weight_recovers_the_best_blend():
    p50 = [2.0, 3.0, 1.0, 4.0]
    p10 = [1.0, 1.0, 0.5, 2.0]
    actual = [0.6 * m + 0.4 * l for m, l in zip(p50, p10)]
    assert suggested_p10_weight(actual, p50, p10) == pytest.approx(0.4)
    assert suggested_p10_weight([5.0] * 4, p50, p10) == 0.0      # clipped
    assert suggested_p10_weight([1.0], [1.0], [1.0]) is None


def test_empty_log():
    res = evaluate([], IDS, TZ)
    assert res["days"] == [] and res["timeline"] == [] and res["summary"]["agreement_pct"] is None
