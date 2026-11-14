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
