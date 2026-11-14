from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import pvdampening as pvd
from homeassistant import HomeAssistant, ServiceNotAllowed

TZ = ZoneInfo("Europe/Copenhagen")
UTC = timezone.utc
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
REF = {10: 1.0, 11: 1.6, 12: 2.0, 13: 2.0, 14: 1.6, 15: 0.9}     # clear-sky kWh per local hour
TRUE_SHADE = {14: 0.7}                                          # a tree in front of the 14:00 sun


def synthetic(days=14, applied=1.0, limited_hour=None):
    """History + actuals: clearness varies by day; the 14:00 hour loses 70 % of the direct sun."""
    history, actual = {}, {}
    for d in range(1, days + 1):
        k = [0.2, 0.5, 0.9, 1.0, 0.35, 0.8, 0.6][d % 7]
        for hl, ref in REF.items():
            t = (datetime(2026, 11, 14, hl, tzinfo=TZ) - timedelta(days=d)).astimezone(UTC)
            f = ref * k
            history[t.isoformat()] = {"p50": f * applied, "p10": f * 0.6 * applied, "p90": ref * applied,
                                      "applied": applied, "limited": hl == limited_hour}
            a = f * (1 - TRUE_SHADE.get(hl, 0.0) * k)
            if hl == limited_hour:
                a = 0.0                        # export limit 0: the inverter throttles the panels
            actual[t] = a * 1000.0
    return history, actual


def model(history, actual):
    ref = pvd.clear_reference(history, {}, [1.0] * 24, TZ, NOW)
    return pvd.fit(history, actual, ref, TZ, NOW, 14, 10.0), ref


def test_learns_the_shaded_hour_and_leaves_the_others():
    m, _ = model(*synthetic())
    assert m["shade"][14] == pytest.approx(0.7, abs=0.05)
    assert m["shade"][12] == pytest.approx(0.0, abs=0.02)
    assert m["hours"][14] == 14


def test_correction_is_strong_on_clear_and_weak_on_cloudy_hours():
    assert pvd.factor(0.7, 1.0) == pytest.approx(0.3)
    assert pvd.factor(0.7, 0.2) == pytest.approx(0.86)
    m, ref = model(*synthetic())
    shade = pvd.effective_shade(m["shade"], {}, "auto")
    clear_day = datetime(2026, 11, 15, 14, tzinfo=TZ).astimezone(UTC)
    c50, _, _, fac = pvd.correct({clear_day: 1.6}, {}, {}, [1.0] * 24, shade, ref, TZ)
    assert fac[clear_day] == pytest.approx(0.3, abs=0.05) and c50[clear_day] == pytest.approx(0.48, abs=0.08)
    grey = {clear_day: 0.3}
    _, _, _, fac = pvd.correct(grey, {}, {}, [1.0] * 24, shade, ref, TZ)
    assert fac[clear_day] > 0.85


def test_no_double_dampening_after_a_push():
    """Values read from Solcast while it dampened by 0.5 are undampened before learning."""
    m1, _ = model(*synthetic(applied=1.0))
    m2, _ = model(*synthetic(applied=0.5))
    assert m2["shade"] == m1["shade"]
    m, ref = model(*synthetic(applied=0.5))
    t = datetime(2026, 11, 15, 14, tzinfo=TZ).astimezone(UTC)
    applied = [0.5] * 24
    c50, _, _, _ = pvd.correct({t: 0.8}, {}, {}, applied, pvd.effective_shade(m["shade"], {}, "auto"), ref, TZ)
    assert c50[t] == pytest.approx(1.6 * 0.3, abs=0.08)        # 0.8 / 0.5 undampened, then x 0.3


def test_export_limited_hours_are_not_mistaken_for_shade():
    m, _ = model(*synthetic(limited_hour=12))
    assert m["shade"][12] == 0.0 and m["hours"][12] == 0


def test_few_days_stay_close_to_no_dampening():
    m, _ = model(*synthetic(days=2))
    assert m["shade"][14] == 0.0                                  # fewer than MIN_HOURS days


def test_manual_hours_and_modes():
    assert pvd.parse_manual("14:0.3, 15:0,2; x:1, 30:0.5") == {14: 0.3, 15: 0.2}
    assert pvd.parse_manual("14:1,15:0.2") == {14: 1.0, 15: 0.2}
    assert pvd.parse_manual("") == {}
    learned = [0.0] * 24
    learned[14] = 0.7
    assert pvd.effective_shade(learned, {15: 0.2}, "auto")[14] == 0.7
    assert pvd.effective_shade(learned, {15: 0.2}, "auto")[15] == pytest.approx(0.8)
    assert pvd.effective_shade(learned, {15: 0.2}, "manual")[14] == 0.0
    assert pvd.effective_shade(learned, {15: 0.2}, "off") == [0.0] * 24


def test_static_factors_use_todays_clearness():
    shade = [0.0] * 24
    shade[14] = 0.7
    ref = [0.0] * 24
    ref[14] = 1.6
    today_clear = {datetime(2026, 11, 14, 14, tzinfo=TZ).astimezone(UTC): 1.6}
    today_grey = {datetime(2026, 11, 14, 14, tzinfo=TZ).astimezone(UTC): 0.32}
    assert pvd.static_factors(shade, today_clear, [1.0] * 24, ref, TZ)[14] == 0.3
    assert pvd.static_factors(shade, today_grey, [1.0] * 24, ref, TZ)[14] == 0.86
    assert pvd.static_factors(shade, {}, [1.0] * 24, ref, TZ)[14] == 0.3     # no forecast: clear-sky factor
    assert len(pvd.static_factors(shade, {}, [1.0] * 24, ref, TZ)) == 24


def test_parse_get_dampening():
    hourly = ",".join(["1.0"] * 13 + ["0.3"] + ["1.0"] * 10)
    assert pvd.parse_get_dampening({"service_response": {"data": [{"site": "all", "damp_factor": hourly}]}})[13] == 0.3
    half = ",".join(["1.0"] * 26 + ["0.2", "0.4"] + ["1.0"] * 20)
    assert pvd.parse_get_dampening({"data": {"damp_factor": half}})[13] == pytest.approx(0.3)
    assert pvd.parse_get_dampening({"service_response": {}}) is None
    assert pvd.parse_get_dampening(None) is None


def test_push_once_a_day_or_on_a_large_change():
    tz = TZ
    base = [1.0] * 24
    assert pvd.push_due(base, base, None, NOW, tz)
    same_day = (NOW - timedelta(hours=1)).isoformat()
    assert not pvd.push_due(base, base, same_day, NOW, tz)
    moved = base[:]
    moved[14] = 0.5
    assert not pvd.push_due(moved, base, same_day, NOW, tz)                   # < 3 h since the last push
    assert pvd.push_due(moved, base, (NOW - timedelta(hours=4)).isoformat(), NOW, tz)
    assert pvd.push_due(base, base, (NOW - timedelta(days=1)).isoformat(), NOW, tz)


def test_solcast_auto_dampening_is_detected():
    assert pvd.solcast_auto_dampening([{"detailedForecast": [{"period_start": "x", "pv_estimate": 1, "dampening_factor": 0.8}]}])
    assert not pvd.solcast_auto_dampening([{"detailedForecast": [{"period_start": "x", "pv_estimate": 1}]}, None])


def test_only_solcast_dampening_services_may_be_called():
    ha = HomeAssistant(token="t", base_url="http://127.0.0.1:9/api")
    for domain, service in (("select", "select_option"), ("number", "set_value"), ("kia_uvo", "start_charge")):
        with pytest.raises(ServiceNotAllowed):
            ha.call_service(domain, service, {})


def test_history_record_trims_old_hours():
    h = {}
    old = NOW - timedelta(days=70)
    pvd.record(h, old, 1.0, 0.5, 1.5, 1.0, False, old)
    pvd.record(h, NOW, 1.0, 0.5, 1.5, 0.8, True, NOW)
    assert list(h) == [NOW.isoformat()] and h[NOW.isoformat()]["applied"] == 0.8 and h[NOW.isoformat()]["limited"]


def test_a_very_clear_forecast_does_not_rescale_the_history():
    history, actual = synthetic()
    tomorrow = {datetime(2026, 11, 15, 14, tzinfo=TZ).astimezone(UTC): 1.6 * 1.6}   # unusually high p90
    ref = pvd.clear_reference(history, tomorrow, [1.0] * 24, TZ, NOW)
    assert ref[14] == pytest.approx(1.6)
    m = pvd.fit(history, actual, ref, TZ, NOW, 14, 10.0)
    assert m["shade"][14] == pytest.approx(0.7, abs=0.05)
    # hours with no history fall back to the forecast
    assert pvd.clear_reference({}, tomorrow, [1.0] * 24, TZ, NOW)[14] == pytest.approx(2.56)


def _entries(day_hour, minutes=30, n=2, p50=2.0, with_bands=True):
    t0 = datetime(2026, 11, 13, day_hour, tzinfo=TZ)
    out = []
    for i in range(n):
        e = {"period_start": (t0 + timedelta(minutes=minutes * i)).isoformat(), "pv_estimate": p50}
        if with_bands:
            e.update(pv_estimate10=p50 / 2, pv_estimate90=p50 * 1.5)
        out.append(e)
    return out


def test_parse_forecast_history_30_and_60_minutes_and_wrappers():
    h = datetime(2026, 11, 13, 12, tzinfo=TZ).astimezone(UTC)
    half = pvd.parse_forecast_history({"service_response": {"data": _entries(12)}})
    assert half[h] == pytest.approx((2.0, 1.0, 3.0))                  # two half hours at 2 kW = 2 kWh
    hourly = pvd.parse_forecast_history({"data": _entries(12, minutes=60, n=2)})
    assert hourly[h] == pytest.approx((2.0, 1.0, 3.0))                # one full hour at 2 kW
    assert len(hourly) == 2
    bare = pvd.parse_forecast_history(_entries(12, with_bands=False))
    assert bare[h] == pytest.approx((2.0, 2.0, 2.0))                  # missing p10/p90 -> p50
    dt_objects = [{"period_start": datetime(2026, 11, 13, 12, 0, tzinfo=TZ), "pv_estimate": 1.0},
                  {"period_start": datetime(2026, 11, 13, 12, 30, tzinfo=TZ), "pv_estimate": 1.0},
                  {"period_start": "garbage", "pv_estimate": 5}, {"pv_estimate": 5}, "x"]
    assert pvd.parse_forecast_history({"data": dt_objects}) == {h: pytest.approx((1.0, 1.0, 1.0))}
    assert pvd.parse_forecast_history({"service_response": {}}) == {}
    assert pvd.parse_forecast_history(None) == {}


def test_backfill_keeps_own_recordings_and_skips_the_current_hour():
    now = datetime(2026, 11, 14, 12, 20, tzinfo=UTC)
    own = datetime(2026, 11, 14, 9, tzinfo=UTC)
    history = {own.isoformat(): {"p50": 9.0, "p10": 9.0, "p90": 9.0, "applied": 0.5, "limited": False}}
    hourly = {own: (1.0, 0.5, 1.5),
              datetime(2026, 11, 14, 10, tzinfo=UTC): (1.0, 0.5, 1.5),
              datetime(2026, 11, 14, 11, tzinfo=UTC): (1.2, 0.6, 1.8),
              datetime(2026, 11, 14, 12, tzinfo=UTC): (1.3, 0.6, 1.9),          # current hour: not finished
              datetime(2026, 8, 1, 12, tzinfo=UTC): (1.0, 1.0, 1.0)}            # older than HISTORY_DAYS
    limited = {datetime(2026, 11, 14, 11, tzinfo=UTC)}
    assert pvd.backfill(history, hourly, now, limited) == 2
    assert history[own.isoformat()]["p50"] == 9.0                              # own recording wins
    e11 = history[datetime(2026, 11, 14, 11, tzinfo=UTC).isoformat()]
    assert e11["applied"] == 1.0 and e11["limited"] is True
    assert datetime(2026, 11, 14, 12, tzinfo=UTC).isoformat() not in history


def test_query_forecast_data_is_allowed():
    from homeassistant import ALLOWED_SERVICES
    assert ("solcast_solar", "query_forecast_data") in ALLOWED_SERVICES
    assert len(ALLOWED_SERVICES) == 3
