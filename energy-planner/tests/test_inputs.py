from datetime import datetime, timedelta, timezone

import pytest

from fixtures import BUY_TODAY, price_attrs, raw_prices, solcast_attrs
from inputs import UTC, build_horizon, hour_floor, parse_prices, parse_solcast, parse_ts


def test_parse_prices_hourly_maps_to_utc_hours():
    prices = parse_prices(price_attrs(BUY_TODAY))
    assert len(prices) == 24
    # 17:00 local (+01:00) is 16:00 UTC and is the 3.116 peak
    assert prices[datetime(2026, 11, 14, 16, tzinfo=UTC)] == pytest.approx(3.116)


def test_parse_prices_quarter_hours_are_averaged_per_hour():
    quarters = [{"hour": f"2026-11-14T10:{m:02d}:00+01:00", "price": p}
                for m, p in zip((0, 15, 30, 45), (1.0, 2.0, 3.0, 4.0))]
    prices = parse_prices({"raw_today": quarters})
    assert prices == {datetime(2026, 11, 14, 9, tzinfo=UTC): pytest.approx(2.5)}


def test_parse_prices_includes_tomorrow_and_skips_none():
    attrs = {"raw_today": raw_prices("2026-11-14", [1.0]), "raw_tomorrow": raw_prices("2026-11-15", [2.0, None])}
    assert len(parse_prices(attrs)) == 2


def test_solcast_hourly_values_sum_to_the_daily_total():
    blended, p50, p10 = parse_solcast([solcast_attrs()], 0.0)
    assert sum(p50.values()) == pytest.approx(5.2722, abs=1e-3)  # sensor state of forecast_today
    assert sum(p10.values()) < sum(p50.values())
    assert blended == p50


def test_solcast_blend_weights_p10():
    blended, p50, p10 = parse_solcast([solcast_attrs()], 0.5)
    h = datetime(2026, 11, 14, 12, tzinfo=UTC)  # 13:00 local: p50 1.1461, p10 0.355
    assert blended[h] == pytest.approx(0.5 * 1.1461 + 0.5 * 0.355)


def test_solcast_half_hour_fallback_counts_half_an_hour():
    attrs = {"detailedForecast": [
        {"period_start": "2026-11-14T11:00:00+01:00", "pv_estimate": 1.3936, "pv_estimate10": 1.1103},
        {"period_start": "2026-11-14T11:30:00+01:00", "pv_estimate": 0.9459, "pv_estimate10": 0.6757}]}
    _, p50, _ = parse_solcast([attrs])
    assert p50[datetime(2026, 11, 14, 10, tzinfo=UTC)] == pytest.approx(1.1698, abs=1e-3)


def test_parse_ts_epoch_milliseconds_and_naive():
    assert parse_ts(1763114400000) == datetime(2025, 11, 14, 10, 0, tzinfo=UTC)
    assert parse_ts("2026-11-14T10:00:00Z") == datetime(2026, 11, 14, 10, tzinfo=UTC)


def test_horizon_scales_the_current_hour_and_stops_where_prices_end():
    buy = parse_prices(price_attrs(BUY_TODAY))
    sell = dict(buy)
    pv = {h: 1.0 for h in buy}
    now = datetime(2026, 11, 14, 11, 30, tzinfo=UTC)  # 12:30 local
    hz = build_horizon(now, buy, sell, pv, lambda h: 2.0)
    assert hz.dt[0] == pytest.approx(0.5)
    assert hz.pv[0] == pytest.approx(0.5) and hz.load[0] == pytest.approx(1.0)
    assert hz.pv[1] == pytest.approx(1.0)
    assert len(hz) == 12  # 11:00 UTC .. 22:00 UTC (=23:00 local), prices end at 24:00 local
    assert hz.starts[0] == hour_floor(now)


def test_horizon_empty_when_current_hour_has_no_price():
    now = datetime(2026, 11, 20, 12, tzinfo=UTC)
    assert len(build_horizon(now, {}, {}, {}, lambda h: 0)) == 0


def test_persistence_forecast_repeats_the_last_known_day_by_local_hour():
    from zoneinfo import ZoneInfo
    from inputs import persistence_forecast
    tz = ZoneInfo("Europe/Copenhagen")
    buy = parse_prices(price_attrs(BUY_TODAY))
    last = max(buy)
    fc = persistence_forecast(buy, last + timedelta(hours=7), tz)
    assert len(fc) == 6                                      # 00:00 .. 05:00 local tomorrow
    assert fc[last + timedelta(hours=1)] == pytest.approx(BUY_TODAY[0])
    assert fc[last + timedelta(hours=6)] == pytest.approx(BUY_TODAY[5])
    hz = build_horizon(datetime(2026, 11, 14, 21, tzinfo=UTC), {**fc, **buy}, {**fc, **buy}, {},
                       lambda h: 1.0, forecast_hours=set(fc))
    assert len(hz) == 8 and hz.forecast == [False, False] + [True] * 6


# ---- Strømligning ---------------------------------------------------------------------------
from zoneinfo import ZoneInfo  # noqa: E402

from inputs import derive_sell_forecast, fill_prices, merge_prices, price_series  # noqa: E402

CPH = ZoneInfo("Europe/Copenhagen")


def strom(day, values, minutes=60):
    """Strømligning's `prices` attribute: [{price, start, end}] with local offsets."""
    out = []
    t0 = datetime.fromisoformat(f"{day}T00:00:00+01:00")
    for i, p in enumerate(values):
        s = t0 + timedelta(minutes=minutes * i)
        out.append({"price": p, "start": s.isoformat(), "end": (s + timedelta(minutes=minutes)).isoformat()})
    return {"prices": out}


def test_parse_prices_reads_stromlignings_prices_attribute():
    got = parse_prices(strom("2026-11-20", [1.0 + h / 10 for h in range(24)]))
    assert len(got) == 24 and got[datetime(2026, 11, 19, 23, tzinfo=UTC)] == pytest.approx(1.0)
    q = parse_prices(strom("2026-11-20", [1.0, 2.0, 3.0, 4.0], minutes=15))
    assert q == {datetime(2026, 11, 19, 23, tzinfo=UTC): pytest.approx(2.5)}     # always the hour's mean


def test_merge_prefers_the_primary_per_hour():
    a, b = datetime(2026, 11, 20, tzinfo=UTC), datetime(2026, 11, 21, tzinfo=UTC)
    assert merge_prices({a: 1.0}, {a: 9.0, b: 2.0}) == {a: 1.0, b: 2.0}
    assert merge_prices({}, {b: 2.0}) == {b: 2.0}


def test_sell_forecast_keeps_the_same_hours_buy_sell_difference():
    k = datetime(2026, 11, 20, 17, tzinfo=UTC)                 # 18 local
    f = k + timedelta(days=2)
    assert derive_sell_forecast({f: 3.0}, {k: 2.5}, {k: 1.0}, CPH) == {f: pytest.approx(1.5)}


def _day(day, values):
    return parse_prices(strom(day, values))


def test_fill_now_tomorrow_from_the_old_sensor_and_the_forecast_after():
    buy = merge_prices(_day("2026-11-20", [2.0] * 24), _day("2026-11-21", [1.5] * 24))
    sell = merge_prices(_day("2026-11-20", [1.0] * 24), _day("2026-11-21", [0.5] * 24))
    fc = _day("2026-11-22", [3.0] * 24)
    until = datetime(2026, 11, 22, 11, tzinfo=UTC)
    b, s, src = fill_prices(buy, sell, fc, until, CPH)
    h = datetime(2026, 11, 22, 5, tzinfo=UTC)
    assert src[h] == "forecast" and b[h] == 3.0 and s[h] == pytest.approx(2.0)    # 3.0 - (1.5 - 0.5)
    assert all(v == "forecast" for v in src.values()) and max(b) == until - timedelta(hours=1)


def test_fill_before_13_repeats_the_hours_the_forecast_lacks():
    buy, sell = _day("2026-11-20", [2.0 + h / 100 for h in range(24)]), _day("2026-11-20", [1.0] * 24)
    fc = _day("2026-11-22", [3.0] * 24)
    until = datetime(2026, 11, 22, 3, tzinfo=UTC)             # into the forecast day
    b, s, src = fill_prices(buy, sell, fc, until, CPH)
    t = datetime(2026, 11, 21, 6, tzinfo=UTC)                 # tomorrow 07 local: no forecast
    assert src[t] == "repeat" and b[t] == pytest.approx(2.07)
    f = datetime(2026, 11, 22, 1, tzinfo=UTC)
    assert src[f] == "forecast" and b[f] == 3.0
    # normal operation: tomorrow is known in the main sensor, so nothing is filled before the forecast
    buy2 = merge_prices(buy, _day("2026-11-21", [1.8] * 24))
    sell2 = merge_prices(sell, _day("2026-11-21", [0.9] * 24))
    _, _, src2 = fill_prices(buy2, sell2, fc, until, CPH)
    assert set(src2.values()) == {"forecast"}


def test_fill_over_dst_and_without_forecast():
    buy, sell = _day("2026-10-24", [2.0] * 24), _day("2026-10-24", [1.0] * 24)
    until = datetime(2026, 10, 26, tzinfo=UTC)
    b, s, src = fill_prices(buy, sell, {}, until, CPH)
    assert set(src.values()) == {"repeat"} and len(b) == len(set(b)) and max(b) == until - timedelta(hours=1)


def test_price_series_uses_mean_else_state():
    rows = [{"start": 1763600400000, "mean": 2.0}, {"start": 1763604000000, "state": 2.5}, {"start": 1763607600000}]
    got = price_series(rows)
    assert list(got.values()) == [2.0, 2.5]
