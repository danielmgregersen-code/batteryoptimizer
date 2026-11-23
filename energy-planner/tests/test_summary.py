from datetime import date

from summary import num, reason_da, rel_day, upcoming


def row(t, mode, soc, grid_bat=0.0, pv_bat=0.0, bat_load=0.0, pv_exp=0.0, buy=2.0, car_grid=0.0, car_soc=None):
    return {"t": t, "mode": mode, "soc_pct": soc, "grid_bat": grid_bat, "pv_bat": pv_bat, "bat_load": bat_load,
            "pv_exp": pv_exp, "buy": buy, "car_grid": car_grid, "car_pv": 0.0, "car_bat": 0.0, "car_soc": car_soc}


def test_upcoming_groups_modes_and_crosses_midnight():
    rows = [row("2026-11-14T22:00+01:00", "hold", 50, pv_exp=0.0),
            row("2026-11-14T23:00+01:00", "hold", 50),
            row("2026-11-15T00:00+01:00", "grid_charge", 80, grid_bat=2.5, buy=1.2, car_grid=11.0, car_soc=40),
            row("2026-11-15T01:00+01:00", "discharge", 70, bat_load=0.7, buy=3.0, car_soc=40)]
    items = upcoming(rows, True, date(2026, 11, 14))
    assert [i["label"] for i in items] == ["Holder batteriet", "Lader fra nettet", "Lader bilen", "Aflader til huset"]
    assert items[0]["when"] == "22:00–00:00"
    assert items[1]["when"] == "i morgen 00:00–01:00" and "2,5 kWh fra nettet à 1,20 kr" in items[1]["text"]
    assert items[2]["text"] == "11,0 kWh, à 1,20 kr · til 40 %"


def test_formatting_helpers():
    assert num(1.25, 1) in ("1,2", "1,3") and num(None) == "–"
    assert rel_day(date(2026, 11, 15), date(2026, 11, 14)) == "i morgen "
    assert rel_day(date(2026, 11, 17), date(2026, 11, 14)) == "tirs. "


def test_reason_da():
    assert reason_da("plan: 7.0 kW this hour") == "Planen: 7,0 kW denne time"
    assert reason_da("solar surplus 5400 W (cooldown, 1200 s left)") == "Soloverskud 5,4 kW (cooldown, 20 min tilbage)"
    assert reason_da("something new") == "something new"


# ---- front-page texts (0.20.0) -----------------------------------------------------
from datetime import datetime as _dt, timedelta as _td
from zoneinfo import ZoneInfo as _Z

from summary import car_advice, cheapest_window, price_attributes, when_text

_TZ = _Z("Europe/Copenhagen")
_NOW = _dt(2026, 11, 20, 20, 30, tzinfo=_TZ)


def _rows(prices, start=_NOW.replace(minute=0)):
    return [{"t": (start + _td(hours=i)).isoformat(), "buy": b, "sell": 0.4} for i, b in enumerate(prices)]


def test_cheapest_window_and_its_text():
    prices = [2.5, 2.2, 1.9, 1.6, 1.4, 1.2, 1.1, 1.0, 1.15, 1.5, 2.0] + [2.2] * 13     # 20:00 ..
    w = cheapest_window(_rows(prices), _NOW)
    assert w["start"].hour == 2 and w["end"].hour == 5 and round(w["avg"], 3) == round((1.1 + 1.0 + 1.15) / 3, 3)
    assert when_text(w["start"], w["end"], _NOW) == "i nat kl. 02–05"
    assert when_text(_NOW.replace(hour=21), _NOW.replace(hour=23), _NOW) == "i aften kl. 21–23"
    attrs = price_attributes(_rows(prices), [(None, "dear")], _NOW)
    assert attrs["level_da"] == "dyr" and attrs["text"].startswith("Dyr time · 2,50 kr · billigst i nat kl. 02–05")


def test_car_advice_states():
    pv = {"planned_kwh": 14.35, "cost_dkk": 28.0, "soc_at_deadline": 42.0, "soc_now": 30.0,
          "deadline": "2026-11-21T06:00:00+01:00",
          "windows": [{"start": "2026-11-21T04:00:00+01:00", "end": "2026-11-21T06:00:00+01:00", "solar": False}]}
    st, a = car_advice({"connected": False}, pv, {}, _NOW)
    assert st == "plug_in" and a["title"] == "Sæt bilen til" and a["text"] == "lader 14,3 kWh i nat kl. 04–06 · ca. 28 kr · når 42 %"
    st, a = car_advice({"connected": False}, {**pv, "planned_kwh": 0.0, "soc_now": 65.0}, {}, _NOW)
    assert st == "not_needed" and a["text"] == "65 % · ingen billige timer før kl. 06"
    st, a = car_advice({"connected": False}, {**pv, "planned_kwh": 0.0, "soc_now": 55.0, "small_dropped": True,
                                              "min_session_kwh": 2.0}, {}, _NOW)
    assert st == "not_needed" and a["text"] == "55 % · under 2 kWh at hente før kl. 06"
    st, _ = car_advice({"connected": True, "max_soc_pct": 90}, None, {"action": "charge", "reason_da": "Planen: 11 kW",
                                                                    "soc_estimate": {"soc": 55.0}}, _NOW)
    assert st == "charging"
    assert car_advice({"connected": True, "max_soc_pct": 90}, None, {"action": "idle", "soc_estimate": {"soc": 90.0}}, _NOW)[0] == "full"
    assert car_advice({"connected": False}, None, {}, _NOW)[0] == "unknown"


def test_car_advice_after_the_charger_ended_the_session():
    pv = {"planned_kwh": 6.0, "cost_dkk": 10.0, "soc_at_deadline": 60.0, "soc_now": 42.0,
          "deadline": "2026-11-21T06:00:00+01:00",
          "windows": [{"start": "2026-11-21T02:00:00+01:00", "end": "2026-11-21T04:00:00+01:00", "solar": False}]}
    idle = {"live": {"status": "idle"}, "soc_estimate": {"soc": 42.0}}
    st, a = car_advice({"connected": False}, pv, idle, _NOW)
    assert st == "replug" and a["title"] == "Opladningen er afsluttet"
    assert a["text"] == "tag stikket ud og sæt det i igen · lader 6,0 kWh i nat kl. 02–04"
    st, a = car_advice({"connected": False}, {**pv, "planned_kwh": 0.0}, idle, _NOW)
    assert st == "replug" and a["text"] == "laderen har afsluttet sessionen · 42 %" and a["color"] == "green"
