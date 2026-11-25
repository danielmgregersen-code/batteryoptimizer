import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import batterysizing as bs
from planner import battery_params
from settings import DEFAULTS

TZ = ZoneInfo("Europe/Copenhagen")


def year(days=40, spread=True, sun=True):
    t0 = datetime(2026, 5, 1, tzinfo=TZ)
    out = []
    for i in range(days * 24):
        t = (t0 + timedelta(hours=i)).astimezone(TZ)
        h = t.hour
        pv = max(0.0, math.sin((h - 6) / 12 * math.pi)) * 6.0 if sun and 6 <= h <= 18 else 0.0
        buy = 1.2 + (1.6 if spread and 17 <= h <= 20 else 0.0)
        out.append({"t": t.isoformat(), "solar_kwh": pv, "consumption_kwh": 0.6 + (1.0 if 17 <= h <= 21 else 0.0),
                    "buy": buy, "sell": max(0.1, buy - 1.0) if spread else 0.2})
    return out


def params(cap):
    return battery_params(DEFAULTS, cap)


def test_a_bigger_battery_saves_with_diminishing_returns():
    r = bs.report(year(), params, 7.4, 2.56)
    s = [x["extra_saving_per_year_dkk"] for x in r["sizes"]]
    assert [x["kwh"] for x in r["sizes"]] == [7.4, 9.96, 12.52, 15.08, 17.64]
    assert s[0] == 0 and s[1] > 0 and all(b >= a for a, b in zip(s, s[1:]))
    steps = [b - a for a, b in zip(s, s[1:])]
    assert steps[-1] < steps[0]                                    # each extra module saves less
    assert r["days"] == 40 and "40 dages data" in r["warning"]      # under 300 days: a warning


def test_no_price_spread_and_no_sun_gives_nothing():
    r = bs.report(year(days=10, spread=False, sun=False), params, 7.4, 2.56, n_max=2)
    assert all(abs(x["extra_saving_per_year_dkk"]) < 1 for x in r["sizes"])


def test_incomplete_days_are_skipped_and_counted():
    hours = year(days=5)
    del hours[30:40]                                                 # 10 hours missing on day 2
    r = bs.simulate(hours, params(7.4))
    assert r["days"] == 4 and r["skipped_days"] == 1
