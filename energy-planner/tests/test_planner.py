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

    def state(self, e):
        if e in self.texts:
            return {"state": self.texts[e], "last_changed": getattr(self, "last_changed", "2026-11-14T10:00:00+00:00")}
        return None

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
    # 12 known hours (to midnight) + estimated prices up to 30 h from the current hour
    assert snap["horizon_hours"] == 30
    assert sum(r["forecast_price"] for r in snap["plan"]) == 18
    assert snap["mode"] in {"grid_charge", "solar_charge", "discharge", "hold"}
    assert snap["savings_vs_solar_only_dkk"] >= -1e-6
    assert len(snap["plan"]) == 30 and snap["plan"][0]["t"].startswith("2026-11-14T12:00")
    assert snap["actual_registers"]["reg_battery_first"] == "Enabled"
    assert "sensor.energy_planner_mode" in ha.published and "sensor.energy_planner_plan" in ha.published
    state, attrs = ha.published["sensor.energy_planner_plan"]
    assert len(attrs["plan"]) == 30
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


def test_default_timezone_follows_dst(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Copenhagen")
    p = Planner(make_ha())
    assert datetime(2026, 7, 1, 10, tzinfo=UTC).astimezone(p.tz).hour == 12    # summer: UTC+2
    assert datetime(2026, 12, 1, 10, tzinfo=UTC).astimezone(p.tz).hour == 11   # winter: UTC+1


def test_soc_target_is_unknown_when_not_in_battery_first(tmp_path):
    ha = make_ha()
    p = Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl"))
    p.run_once(datetime(2026, 11, 14, 16, 30, tzinfo=UTC))    # 17:30 local, the 3.1 DKK peak
    state, attrs = ha.published["sensor.energy_planner_soc_target"]
    assert ha.published["sensor.energy_planner_mode"][0] == "discharge"
    assert state == "unknown" and attrs["active"] is False


def test_counters_prices_and_forecasts_are_logged_and_evaluated(tmp_path):
    from evaluation import evaluate
    from settings import counter_ids
    ha = make_ha()
    p = Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl"))
    ids = counter_ids(DEFAULTS)
    for i, minute in enumerate((0, 15, 30)):
        ha.numbers[ids["import"]], ha.numbers[ids["export"]] = 100.0 + i, 50.0 + 0.5 * i
        ha.numbers[ids["solar"]], ha.numbers[ids["battery_in"]], ha.numbers[ids["battery_out"]] = 10.0 + 0.3 * i, 5.0, 4.0
        p.run_once(datetime(2026, 11, 14, 11, minute, tzinfo=UTC))
    log = p.recent_decisions()
    assert log[0]["counters"][ids["import"]] == 100.0 and log[0]["price_now"]["buy"] == pytest.approx(1.873)
    assert log[0]["pred"]["pv_p50_kw"] == pytest.approx(0.9645, abs=1e-3)   # 12:00 local and "pv_kw" in log[0]["pred"]
    day = evaluate(log, ids, TZ)["days"][0]
    assert day["day"] == "2026-11-14" and day["import_kwh"] == 2.0 and day["export_kwh"] == 1.0
    assert day["cost_dkk"] == pytest.approx(round(2 * 1.873 - 1.0 * 1.05, 2))
    assert day["pv_actual_kwh"] == pytest.approx(0.6)


def test_entity_check_reports_missing_entities(tmp_path):
    p = Planner(make_ha(), tz=TZ, log_path=str(tmp_path / "d.jsonl"))
    snap = p.run_once(datetime(2026, 11, 14, 11, 30, tzinfo=UTC))
    assert snap["entity_check"]["select.growatt_battery_first_time_2"] == "ok"
    assert snap["entity_check"]["number.growatt_grid_export_limit"] == "missing"


def car_ha():
    ha = make_ha()
    ha.texts[DEFAULTS["charger_status_entity"]] = "suspended_evse"
    ha.numbers[DEFAULTS["car_soc_entity"]] = 30.0
    return ha


def test_connected_car_is_planned_to_the_minimum_by_06(tmp_path):
    ha = car_ha()
    p = Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl"), car_log_path=str(tmp_path / "c.jsonl"))
    snap = p.run_once(datetime(2026, 11, 14, 19, 30, tzinfo=UTC))      # 20:30 local
    car = snap["car"]
    assert car["connected"] and car["need_minimum"] and car["shortfall_kwh"] == 0
    by_t = {r["t"][11:16]: r for r in snap["plan"]}
    assert by_t["05:00"]["car_soc"] == pytest.approx(42.0, abs=0.1)
    assert by_t["07:00"]["car_grid"] == 0                                # deadline passed
    assert "car_kwh" in ha.published["sensor.energy_planner_plan"][1]["columns"]


def test_car_cycle_publishes_and_logs_only_changes(tmp_path):
    ha = car_ha()
    p = Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl"), car_log_path=str(tmp_path / "c.jsonl"))
    now = datetime(2026, 11, 14, 19, 30, tzinfo=UTC)
    p.run_once(now)
    for minute in (30, 32, 34):
        d = p.car_cycle(now.replace(minute=minute))
    state, attrs = ha.published["sensor.energy_planner_car"]
    assert attrs["dry_run"] is True and state == d["action"]
    assert len(p.recent_car_decisions()) == 1


def test_car_disabled_leaves_the_car_out(tmp_path):
    ha = car_ha()
    opts = {**DEFAULTS, "car_enabled": False}
    p = Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl"), options_fn=lambda: opts)
    snap = p.run_once(datetime(2026, 11, 14, 19, 30, tzinfo=UTC))
    assert snap["car"] == {"enabled": False} and "car_grid" not in snap["plan"][0]
    assert p.car_cycle() is None


def test_mode_sensor_has_danish_summary_and_upcoming(tmp_path):
    ha = car_ha()
    p = Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl"), car_log_path=str(tmp_path / "c.jsonl"))
    snap = p.run_once(datetime(2026, 11, 14, 11, 30, tzinfo=UTC))       # 12:30 local
    state, attrs = ha.published["sensor.energy_planner_mode"]
    assert attrs["label"] in ("Lader fra nettet", "Lader fra sol", "Aflader til huset", "Holder batteriet")
    assert attrs["summary"].startswith(attrs["label"])
    assert attrs["next_action"] and attrs["next_change"] > snap["plan"][0]["t"]
    kinds = [it["kind"] for it in snap["upcoming"]]
    assert "car" in kinds                                                # the car's night window
    car = next(it for it in snap["upcoming"] if it["kind"] == "car")
    # 12:00 (1.87 kr) is cheaper than the estimated night (1.94 kr), so the car's minimum goes there
    assert car["when"].startswith("12:00") and "kWh" in car["text"]
    assert attrs["upcoming"][0]["when"].startswith("12:00")


def test_car_sensor_has_danish_reason_and_next_window(tmp_path):
    ha = car_ha()
    p = Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl"), car_log_path=str(tmp_path / "c.jsonl"))
    now = datetime(2026, 11, 14, 19, 30, tzinfo=UTC)
    p.run_once(now)
    p.car_cycle(now)
    _, attrs = ha.published["sensor.energy_planner_car"]
    assert attrs["reason_da"] == "Planen lader ikke bilen denne time" and attrs["label"] == "Venter"
    assert attrs["next_window"].startswith("i morgen 04:00–06:00") and attrs["planned_kwh"] > 10


def test_cost_today_sensor(tmp_path):
    from settings import counter_ids
    ha = make_ha()
    ids = counter_ids(DEFAULTS)
    p = Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl"))
    for i, minute in enumerate((0, 15, 30)):
        ha.numbers[ids["import"]], ha.numbers[ids["export"]] = 100.0 + i, 50.0
        p.run_once(datetime(2026, 11, 14, 11, minute, tzinfo=UTC))
    state, attrs = ha.published["sensor.energy_planner_cost_today"]
    assert state == pytest.approx(2 * 1.873, abs=0.01) and attrs["unit_of_measurement"] == "DKK"
    assert attrs["import_kwh"] == 2.0 and attrs["yesterday_dkk"] is None


def test_missing_car_statistics_is_a_visible_warning(tmp_path):
    start = datetime(2026, 10, 17, tzinfo=UTC)
    rows = [{"start": (start + timedelta(hours=i)).timestamp() * 1000, "mean": 1500.0} for i in range(24 * 20)]
    stats = {DEFAULTS["house_load_entity"]: rows}                       # no car statistics
    snap = Planner(make_ha(stats=stats), tz=TZ, log_path=str(tmp_path / "d.jsonl")).run_once(
        datetime(2026, 11, 14, 11, 30, tzinfo=UTC))
    assert any("no long-term statistics" in w for w in snap["warnings"])
    assert snap["profile"]["diagnostics"]["battery_mode"] == "no"
