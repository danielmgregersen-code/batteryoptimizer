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
