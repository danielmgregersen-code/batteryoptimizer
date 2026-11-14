import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from fixtures import BUY_TODAY, SELL_TODAY, price_attrs, solcast_attrs
from planner import Planner, PlannerError
from settings import DEFAULTS

TZ = ZoneInfo("Europe/Copenhagen")
UTC = timezone.utc


class FakeHA:
    def __init__(self, attrs, numbers=None, texts=None, stats=None):
        self.attrs, self.numbers, self.texts, self.stats = attrs, numbers or {}, texts or {}, stats
        self.published = {}

    def attributes(self, e):
        return self.attrs.get(e)

    def number(self, e):
        return self.numbers.get(e)

    def text(self, e):
        return self.texts.get(e)

    def set_state(self, e, state, attributes=None):
        self.published[e] = (state, attributes or {})

    def statistics(self, ids, start, end, period="hour"):
        if self.stats is None:
            raise RuntimeError("no websocket in this test")
        return self.stats


def make_ha(**kw):
    attrs = {
        DEFAULTS["price_buy_entity"]: price_attrs(BUY_TODAY),
        DEFAULTS["price_sell_entity"]: price_attrs(SELL_TODAY),
        "sensor.solcast_pv_forecast_forecast_today": solcast_attrs(),
    }
    return FakeHA(attrs, numbers={DEFAULTS["battery_soc_entity"]: 73.0},
                  texts={"select.growatt_battery_first_time_2": "Enabled"}, **kw)


def test_run_once_produces_a_plan_publishes_sensors_and_logs(tmp_path):
    ha = make_ha()
    p = Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl"))
    snap = p.run_once(datetime(2026, 11, 14, 11, 30, tzinfo=UTC))   # 12:30 local
    assert snap["dry_run"] is True
    assert snap["horizon_hours"] == 12
    assert snap["mode"] in {"grid_charge", "solar_charge", "discharge", "hold"}
    assert snap["savings_vs_solar_only_dkk"] >= -1e-6
    assert len(snap["plan"]) == 12 and snap["plan"][0]["t"].startswith("2026-11-14T12:00")
    assert snap["actual_registers"]["reg_battery_first"] == "Enabled"
    assert "sensor.energy_planner_mode" in ha.published and "sensor.energy_planner_plan" in ha.published
    state, attrs = ha.published["sensor.energy_planner_plan"]
    assert len(attrs["plan"]) == 12
    lines = (tmp_path / "d.jsonl").read_text().strip().splitlines()
    assert json.loads(lines[0])["mode"] == snap["mode"]


def test_falls_back_to_default_load_when_statistics_fail(tmp_path):
    p = Planner(make_ha(), tz=TZ, log_path=str(tmp_path / "d.jsonl"))
    snap = p.run_once(datetime(2026, 11, 14, 11, 30, tzinfo=UTC))
    assert snap["profile_hours_of_data"] == 0
    assert snap["plan"][3]["load"] == pytest.approx(DEFAULTS["default_load_kw"])


def test_uses_statistics_when_available(tmp_path):
    start = datetime(2026, 10, 17, tzinfo=UTC)
    rows = [{"start": (start + timedelta(hours=i)).timestamp() * 1000, "mean": 1500.0} for i in range(24 * 20)]
    stats = {DEFAULTS["house_load_entity"]: rows, DEFAULTS["car_power_entity"]: []}
    p = Planner(make_ha(stats=stats), tz=TZ, log_path=str(tmp_path / "d.jsonl"))
    snap = p.run_once(datetime(2026, 11, 14, 11, 30, tzinfo=UTC))
    assert snap["profile_hours_of_data"] == 24 * 20
    assert snap["plan"][3]["load"] == pytest.approx(1.5)


def test_missing_prices_raise_instead_of_guessing(tmp_path):
    ha = make_ha()
    ha.attrs.pop(DEFAULTS["price_sell_entity"])
    with pytest.raises(PlannerError):
        Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl")).run_once(datetime(2026, 11, 14, 11, 30, tzinfo=UTC))


def test_missing_soc_raises(tmp_path):
    ha = make_ha()
    ha.numbers.clear()
    with pytest.raises(PlannerError):
        Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl")).run_once(datetime(2026, 11, 14, 11, 30, tzinfo=UTC))


def test_missing_solar_forecast_warns_but_plans(tmp_path):
    ha = make_ha()
    ha.attrs.pop("sensor.solcast_pv_forecast_forecast_today")
    snap = Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl")).run_once(datetime(2026, 11, 14, 11, 30, tzinfo=UTC))
    assert snap["warnings"] and all(r["pv"] == 0 for r in snap["plan"])


def test_decision_log_is_trimmed(tmp_path, monkeypatch):
    import planner as mod
    monkeypatch.setattr(mod, "MAX_LOG_LINES", 5)
    monkeypatch.setattr(mod, "KEEP_LOG_LINES", 2)
    p = Planner(make_ha(), tz=TZ, log_path=str(tmp_path / "d.jsonl"))
    for _ in range(7):
        p.run_once(datetime(2026, 11, 14, 11, 30, tzinfo=UTC))
    assert len(p.recent_decisions(100)) <= 5
