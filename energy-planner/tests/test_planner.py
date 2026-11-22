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
    assert snap["production"]["battery"] is False
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
    assert attrs["production"] is False and state == d["action"]
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


def test_pv_dampening_learns_shade_corrects_the_plan_and_pushes_to_solcast(tmp_path):
    import pvdampening as pvd
    from fixtures import SOLCAST_HOURLY

    # 14 days of history: Solcast's forecast vs. actual, with a tree in front of the 13:00 sun
    history, actual_rows = {}, []
    for d in range(1, 15):
        k = [0.3, 0.6, 1.0, 0.9, 0.5, 0.8, 0.4][d % 7]
        for hl in range(8, 16):
            t = (datetime(2026, 11, 14, hl, tzinfo=TZ) - timedelta(days=d)).astimezone(UTC)
            clear = SOLCAST_HOURLY[hl][2]
            f = clear * k
            history[t.isoformat()] = {"p50": f, "p10": f * 0.5, "p90": clear, "applied": 1.0, "limited": False}
            a = f * (1 - (0.7 * k if hl == 13 else 0.0))
            actual_rows.append({"start": t.timestamp() * 1000, "mean": a * 1000})
    pvd.save_json(str(tmp_path / "pv_history.json"), history)
    stats = {DEFAULTS["house_load_entity"]: [], DEFAULTS["pv_power_entity"]: actual_rows}

    ha = make_ha(stats=stats)
    calls = []

    def call_service(domain, service, data=None, return_response=False):
        calls.append((domain, service, data))
        return {"service_response": {"data": [{"site": "all", "damp_factor": ",".join(["1.0"] * 24)}]}}
    ha.call_service = call_service
    opts = {**DEFAULTS, "solcast_push": True}
    p = Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl"), options_fn=lambda: opts)
    snap = p.run_once(datetime(2026, 11, 14, 10, 30, tzinfo=UTC))      # 11:30 local

    d = snap["pv_dampening"]
    assert d["learned_factors"][13] < 0.6 and d["learned_factors"][11] > 0.95
    row13 = next(r for r in snap["plan"] if r["t"][11:16] == "13:00")
    assert row13["pv_factor"] < 0.9
    assert [c[1] for c in calls] == ["get_dampening", "query_forecast_data", "set_dampening"]
    pushed = calls[2][2]["damp_factor"].split(",")
    assert len(pushed) == 24 and float(pushed[13]) < 0.9 and float(pushed[11]) > 0.95
    state, attrs = ha.published["sensor.energy_planner_pv_dampening"]
    assert attrs["factors_csv"] == calls[2][2]["damp_factor"] and attrs["applied_in_solcast"][13] < 0.9
    # the stored state survives a restart and is used to undampen Solcast's values
    assert pvd.load_json(str(tmp_path / "pv_dampening.json"), {})["applied"][13] < 0.9


def test_pv_dampening_off_and_solcast_auto(tmp_path):
    ha = make_ha()
    opts = {**DEFAULTS, "pv_dampening": "off"}
    snap = Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl"), options_fn=lambda: opts).run_once(
        datetime(2026, 11, 14, 10, 30, tzinfo=UTC))
    assert all(r["pv_factor"] in (None, 1.0) for r in snap["plan"])
    ha2 = make_ha()
    attrs = ha2.attrs["sensor.solcast_pv_forecast_forecast_today"]
    attrs["detailedForecast"] = [{"period_start": "2026-11-14T12:00:00+01:00", "pv_estimate": 1.0, "dampening_factor": 0.8}]
    snap = Planner(ha2, tz=TZ, log_path=str(tmp_path / "e.jsonl")).run_once(datetime(2026, 11, 14, 10, 30, tzinfo=UTC))
    assert snap["pv_dampening"]["solcast_auto"] is True
    assert ha2.published["sensor.energy_planner_pv_dampening"][1]["solcast_auto"] is True


def _shaded_solcast_history():
    """Solcast's 30-min forecast history (kW) and the actual production with a tree at 13:00."""
    from fixtures import SOLCAST_HOURLY
    entries, actual_rows = [], []
    for d in range(1, 15):
        k = [0.3, 0.6, 1.0, 0.9, 0.5, 0.8, 0.4][d % 7]
        for hl in range(8, 16):
            t = datetime(2026, 11, 14, hl, tzinfo=TZ) - timedelta(days=d)
            clear = SOLCAST_HOURLY[hl][2]
            f = clear * k
            for half in (0, 30):
                entries.append({"period_start": (t + timedelta(minutes=half)).isoformat(),
                                "pv_estimate": f, "pv_estimate10": f * 0.5, "pv_estimate90": clear})
            a = f * (1 - (0.7 * k if hl == 13 else 0.0))
            actual_rows.append({"start": t.astimezone(UTC).timestamp() * 1000, "mean": a * 1000})
    return entries, actual_rows


def test_solcast_history_is_fetched_at_start_and_learned_from_at_once(tmp_path):
    entries, actual_rows = _shaded_solcast_history()
    stats = {DEFAULTS["house_load_entity"]: [], DEFAULTS["pv_power_entity"]: actual_rows}
    ha = make_ha(stats=stats)
    calls = []

    def call_service(domain, service, data=None, return_response=False):
        calls.append((service, data, return_response))
        if service == "query_forecast_data":
            return {"changed_states": [], "service_response": {"data": entries}}
        raise RuntimeError("no dampening set")
    ha.call_service = call_service
    p = Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl"))       # no pv_history.json yet
    snap = p.run_once(datetime(2026, 11, 14, 10, 30, tzinfo=UTC))

    q = next(c for c in calls if c[0] == "query_forecast_data")
    assert q[1]["undampened"] is True and q[2] is True
    assert q[1]["start_date_time"].startswith("2026-10-31T10:00") and q[1]["end_date_time"].startswith("2026-11-14T10:00")
    d = snap["pv_dampening"]
    assert d["backfill"]["hours"] == 14 * 8 and "error" not in d["backfill"]
    assert d["learned_factors"][13] < 0.6 and d["learned_factors"][11] > 0.95   # learned on the first cycle
    # fetched once: the next cycle does not ask again
    p.run_once(datetime(2026, 11, 14, 10, 45, tzinfo=UTC))
    assert sum(1 for c in calls if c[0] == "query_forecast_data") == 1


def test_failed_history_fetch_does_not_stop_planning_and_is_retried(tmp_path):
    ha = make_ha()
    calls = []

    def call_service(domain, service, data=None, return_response=False):
        calls.append(service)
        if service == "query_forecast_data":
            return {"service_response": {"something": "else"}}
        raise RuntimeError("no dampening set")
    ha.call_service = call_service
    p = Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl"))
    t0 = datetime(2026, 11, 14, 10, 30, tzinfo=UTC)
    snap = p.run_once(t0)
    assert snap["mode"] and "error" in snap["pv_dampening"]["backfill"]
    p.run_once(t0 + timedelta(hours=1))
    assert calls.count("query_forecast_data") == 1                    # not before 6 hours
    p.run_once(t0 + timedelta(hours=7))
    assert calls.count("query_forecast_data") == 2


def test_production_switch_off_means_no_writes(tmp_path):
    ha = make_ha()
    ha.calls = []
    ha.call_service = lambda *a, **k: ha.calls.append(a)
    snap = Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl")).run_once(datetime(2026, 11, 14, 16, 30, tzinfo=UTC))
    assert snap["production"]["battery"] is False and snap["production"]["writes"] == []
    assert not any(c[0] in ("select", "number") for c in ha.calls)
    assert ha.published["sensor.energy_planner_mode"][1]["production"] is False


def test_production_switch_on_writes_the_registers(tmp_path):
    ha = make_ha()
    ha.texts[DEFAULTS["reg_export_limit"]] = "100"                # export free; the price is positive
    ha.texts.update({DEFAULTS["production_switch_entity"]: "on", DEFAULTS["reg_grid_charge"]: "Enabled",
                     DEFAULTS["reg_battery_first"]: "Enabled"})
    calls = []

    def call_service(domain, service, data=None, return_response=False):
        calls.append((domain, service, (data or {}).get("entity_id")))
        if domain == "select":
            ha.texts[data["entity_id"]] = data["option"]
        if domain == "solcast_solar":
            raise RuntimeError("no dampening")
    ha.call_service = call_service
    snap = Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl")).run_once(datetime(2026, 11, 14, 16, 30, tzinfo=UTC))
    assert snap["production"]["battery"] is True
    assert snap["mode"] == "discharge"                                  # 17:30 local, the peak
    written = [c[2] for c in calls if c[0] in ("select", "number")]
    assert written[:2] == [DEFAULTS["reg_grid_charge"], DEFAULTS["reg_battery_first"]]
    assert DEFAULTS["reg_export_limit"] not in written
    assert snap["actual_registers"]["reg_battery_first"] == "Disabled"   # re-read after writing
    assert ha.published["sensor.energy_planner_mode"][1]["production"] is True


def test_car_production_executes_calls_and_guards_the_kia_quota(tmp_path):
    ha = car_ha()
    ha.texts[DEFAULTS["car_production_switch_entity"]] = "on"
    ha.numbers[DEFAULTS["car_soc_entity"]] = 15.0                      # critical: charge now
    calls = []
    ha.call_service = lambda domain, service, data=None, return_response=False: calls.append((domain, service, data))
    opts = {**DEFAULTS, "car_device_id": "abc123"}
    p = Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl"), car_log_path=str(tmp_path / "c.jsonl"),
                options_fn=lambda: opts)
    now = datetime(2026, 11, 14, 19, 30, tzinfo=UTC)
    d = p.car_cycle(now)
    assert d["production"] is True
    kia = [c for c in calls if c[0] == "kia_uvo"]
    assert kia == [("kia_uvo", "start_charge", {"device_id": "abc123"})]
    p.car_cycle(now + timedelta(minutes=2))                             # still suspended: no new Kia call
    assert len([c for c in calls if c[0] == "kia_uvo"]) == 1
    p.car_cycle(now + timedelta(minutes=12))
    assert len([c for c in calls if c[0] == "kia_uvo"]) == 2
    assert p.recent_car_decisions()[-1]["executed"]


def test_car_production_without_device_id_skips_kia_and_warns(tmp_path):
    ha = car_ha()
    ha.texts[DEFAULTS["car_production_switch_entity"]] = "on"
    ha.numbers[DEFAULTS["car_soc_entity"]] = 15.0
    calls = []
    ha.call_service = lambda domain, service, data=None, return_response=False: calls.append((domain, service))
    p = Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl"), car_log_path=str(tmp_path / "c.jsonl"))
    d = p.car_cycle(datetime(2026, 11, 14, 19, 30, tzinfo=UTC))
    assert not any(c[0] == "kia_uvo" for c in calls)
    assert any(x["status"] == "skipped" and "car_device_id" in x["error"] for x in d["executed"])


# ---- car production: Kia failures, 6 A and the emergency stop on the charger ----------------------
PIN_ERROR = "Failed to stop charge: PIN verification failed, ensure PIN is entered correctly."


def kia_failing_ha(soc, message=PIN_ERROR, pv_w=0.0):
    from homeassistant import ServiceCallError
    ha = car_ha()
    ha.texts[DEFAULTS["charger_status_entity"]] = "charging"
    ha.texts[DEFAULTS["car_production_switch_entity"]] = "on"
    ha.numbers[DEFAULTS["car_soc_entity"]] = soc
    ha.numbers[DEFAULTS["charger_current_entity"]] = 16
    ha.numbers[DEFAULTS["pv_power_entity"]] = pv_w
    ha.kia = []
    ha.local = []

    def ws(domain, service, data=None):
        ha.kia.append(service)
        if message:
            raise ServiceCallError(message, "home_assistant_error")
    ha.call_service_ws = ws
    ha.call_service = lambda domain, service, data=None, return_response=False: ha.local.append(
        (domain, service, (data or {}).get("entity_id"), (data or {}).get("value")))
    return ha


def planner_for(ha, tmp_path, **opt):
    opts = {**DEFAULTS, "car_device_id": "abc123", "car_charge_switch_entity": "", **opt}
    return Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl"), car_log_path=str(tmp_path / "c.jsonl"),
                   options_fn=lambda: opts)


NIGHT = datetime(2026, 11, 14, 19, 30, tzinfo=UTC)          # 20:30 local, expensive hour


def test_pin_error_stops_kia_calls_sets_6a_and_stops_on_the_charger_when_nothing_more_is_planned(tmp_path):
    ha = kia_failing_ha(soc=45.0)                             # above the minimum, prices above the car value
    p = planner_for(ha, tmp_path)
    p.run_once(NIGHT)
    d = p.car_cycle(NIGHT)
    assert d["action"] == "pause" and ha.kia == ["stop_charge"]
    assert ("number", "set_value", DEFAULTS["charger_current_entity"], 6) in ha.local
    assert not any(x[1] == "press" for x in ha.local)          # not before the pause has been wanted 5 min
    ha.numbers[DEFAULTS["charger_current_entity"]] = 6
    d = p.car_cycle(NIGHT + timedelta(minutes=5))
    pressed = [x for x in ha.local if x[1] == "press"]
    assert pressed == [("button", "press", DEFAULTS["charger_stop_button"], None)]
    assert "PIN" in d["warning"] and "Nødstop" in d["warning"]
    # no Kia retries and no restart attempts afterwards
    ha.local.clear()
    d = p.car_cycle(NIGHT + timedelta(minutes=7))
    assert ha.kia == ["stop_charge"] and ha.local == [] and "sæt kablet i igen" in d["warning"]
    state, attrs = ha.published["sensor.energy_planner_car"]
    assert "sæt kablet i igen" in attrs["warning"]


def test_no_charger_stop_while_more_charging_is_planned_later(tmp_path):
    ha = kia_failing_ha(soc=30.0)                             # minimum still needed: charges 04-06 later
    p = planner_for(ha, tmp_path)
    p.run_once(NIGHT)
    d = p.car_cycle(NIGHT)
    assert d["action"] == "pause"                             # not now: the cheap hours come later
    assert not any(x[1] == "press" for x in ha.local)
    assert ("number", "set_value", DEFAULTS["charger_current_entity"], 6) in ha.local
    assert "6 A" in d["warning"]


def test_no_charger_stop_on_sun_or_when_disabled(tmp_path):
    ha = kia_failing_ha(soc=45.0, pv_w=3000.0)
    p = planner_for(ha, tmp_path)
    p.run_once(NIGHT)
    p.car_cycle(NIGHT)
    assert not any(x[1] == "press" for x in ha.local)
    ha2 = kia_failing_ha(soc=45.0)
    p2 = planner_for(ha2, tmp_path, car_thor_stop_fallback=False)
    p2.run_once(NIGHT)
    p2.car_cycle(NIGHT)
    assert not any(x[1] == "press" for x in ha2.local)


def test_other_kia_errors_back_off_and_need_two_failures_before_the_charger_stop(tmp_path):
    ha = kia_failing_ha(soc=45.0, message="Read timed out")
    p = planner_for(ha, tmp_path)
    p.run_once(NIGHT)
    d = p.car_cycle(NIGHT)
    assert ha.kia == ["stop_charge"] and not any(x[1] == "press" for x in ha.local)
    assert "prøver igen" in d["warning"].lower()
    p.car_cycle(NIGHT + timedelta(minutes=1))                 # within the 2 min backoff: no call
    assert ha.kia == ["stop_charge"]
    p.car_cycle(NIGHT + timedelta(minutes=2))                 # retry fails again -> second failure
    assert ha.kia == ["stop_charge", "stop_charge"]
    assert not any(x[1] == "press" for x in ha.local)         # wanted for 2 min only
    p.car_cycle(NIGHT + timedelta(minutes=5))
    assert any(x[1] == "press" for x in ha.local)             # now the emergency stop
    p.car_cycle(NIGHT + timedelta(minutes=8))                 # (after the stop: nothing more)
    assert ha.kia == ["stop_charge", "stop_charge"]


def test_switching_the_car_helper_off_and_on_clears_the_kia_block(tmp_path):
    ha = kia_failing_ha(soc=30.0)
    p = planner_for(ha, tmp_path)
    p.run_once(NIGHT)
    p.car_cycle(NIGHT)
    p.car_cycle(NIGHT + timedelta(minutes=30))
    assert ha.kia == ["stop_charge"]                          # blocked after the PIN error
    ha.texts[DEFAULTS["car_production_switch_entity"]] = "off"
    p.car_cycle(NIGHT + timedelta(minutes=32))
    ha.texts[DEFAULTS["car_production_switch_entity"]] = "on"
    p.car_cycle(NIGHT + timedelta(minutes=34))
    assert ha.kia == ["stop_charge", "stop_charge"]


def test_a_stop_the_car_ignores_counts_as_failing_after_10_minutes(tmp_path):
    ha = kia_failing_ha(soc=45.0, message=None)               # Kia says OK, car keeps charging
    p = planner_for(ha, tmp_path)
    p.run_once(NIGHT)
    d = p.car_cycle(NIGHT)
    assert d["warning"] is None and not any(x[1] == "press" for x in ha.local)
    d = p.car_cycle(NIGHT + timedelta(minutes=10))
    assert any(x[1] == "press" for x in ha.local) and "Nødstop" in d["warning"]



def test_missing_device_id_while_charging_warns_and_falls_back(tmp_path):
    ha = kia_failing_ha(soc=45.0)
    p = planner_for(ha, tmp_path, car_device_id="")
    p.run_once(NIGHT)
    d = p.car_cycle(NIGHT)
    assert ha.kia == [] and "car_device_id" in d["warning"]
    p.car_cycle(NIGHT + timedelta(minutes=6))
    assert any(x[1] == "press" for x in ha.local)              # grid charging, nothing more planned


def test_departure_helper_drives_the_plan_and_the_controller_limit(tmp_path):
    ha = car_ha()
    ha.numbers[DEFAULTS["car_soc_entity"]] = 50.0
    ha.texts[DEFAULTS["car_departure_entity"]] = "2026-11-15 09:00:00"
    ha.state = lambda e: ({"state": ha.texts[e], "attributes": {"has_date": True, "has_time": True},
                           "last_changed": "2026-11-14T10:00:00+00:00"} if e in ha.texts else None)
    p = Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl"), car_log_path=str(tmp_path / "c.jsonl"))
    snap = p.run_once(datetime(2026, 11, 14, 19, 30, tzinfo=UTC))
    car = snap["car"]
    assert car["departure"]["status"] == "planned" and car["max_soc_pct"] == 100
    assert car["departure_ok"] is True
    by_t = {r["t"][11:16]: r for r in snap["plan"]}
    assert by_t["08:00"]["car_soc"] == pytest.approx(100.0, abs=0.2)
    assert any("afgang" in it["label"] for it in snap["upcoming"] if it["kind"] == "car")
    p.car_cycle(datetime(2026, 11, 14, 19, 30, tzinfo=UTC))
    assert ha.published["sensor.energy_planner_car"][1]["max_soc_pct"] == 100


def test_extra_charge_is_locked_at_the_order_planned_and_expires(tmp_path):
    ha = car_ha()
    ha.numbers[DEFAULTS["car_soc_entity"]] = 45.0
    p = Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl"), car_log_path=str(tmp_path / "c.jsonl"))
    now = datetime(2026, 11, 14, 19, 30, tzinfo=UTC)                     # 20:30 local
    order = p.set_car_extra("pct", 20, now)
    assert order["target_pct"] == 65 and order["label"] == "+20 %"
    assert order["until"] == "2026-11-15T06:00:00+01:00"
    ha.numbers[DEFAULTS["car_soc_entity"]] = 50.0                         # charging does not move the target
    snap = p.run_once(now)
    ext = snap["car"]["extra"]
    assert ext["status"] == "planned" and ext["target_pct"] == 65 and ext["planned_pct"] == pytest.approx(65, abs=0.5)
    by_t = {r["t"][11:16]: r for r in snap["plan"]}
    assert by_t["05:00"]["car_soc"] == pytest.approx(65.0, abs=0.5)
    assert any("65 %" in it["label"] for it in snap["upcoming"] if it["kind"] == "car")
    p.car_cycle(now)
    assert ha.published["sensor.energy_planner_car"][1]["extra"]["target_pct"] == 65
    assert ha.published["sensor.energy_planner_car"][1]["max_soc_pct"] >= 65
    # survives a restart, expires after the deadline
    p2 = Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl"), car_log_path=str(tmp_path / "c.jsonl"))
    assert p2._car_extra["target_pct"] == 65
    snap = p2.run_once(datetime(2026, 11, 15, 6, 30, tzinfo=UTC))
    assert p2._car_extra is None and not snap["car"]["extra"]["set"]


def test_extra_charge_target_validation_cap_cancel_and_departure(tmp_path):
    ha = car_ha()
    ha.numbers[DEFAULTS["car_soc_entity"]] = 90.0
    p = Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl"), car_log_path=str(tmp_path / "c.jsonl"))
    now = datetime(2026, 11, 14, 19, 30, tzinfo=UTC)
    assert p.set_car_extra("pct", 20, now)["target_pct"] == 100           # capped at 100 %
    with pytest.raises(ValueError):
        p.set_car_extra("target", 80, now)                                 # below the SoC now
    with pytest.raises(ValueError):
        p.set_car_extra("target", 120, now)
    assert p.set_car_extra("target", 95, now)["label"] == "Mål 95 %"
    p.clear_car_extra()
    assert p._car_extra is None and not p.run_once(now)["car"]["extra"]["set"]
    ha.numbers[DEFAULTS["car_soc_entity"]] = None
    with pytest.raises(ValueError):
        p.set_car_extra("pct", 10, now)                                    # needs a known SoC
    assert p.set_car_extra("target", 80, now)["target_pct"] == 80          # an absolute target does not
    # with a trip set, the order lasts until the trip
    ha.texts[DEFAULTS["car_departure_entity"]] = "2026-11-15 09:00:00"
    ha.state = lambda e: ({"state": ha.texts[e], "attributes": {"has_date": True, "has_time": True}}
                          if e in ha.texts else None)
    assert p.set_car_extra("target", 80, now)["until"] == "2026-11-15T09:00:00+01:00"


def test_full_charge_seen_live_is_remembered(tmp_path):
    ha = car_ha()
    ha.numbers[DEFAULTS["car_soc_entity"]] = 99.5
    p = Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl"), car_log_path=str(tmp_path / "c.jsonl"))
    p.run_once(datetime(2026, 11, 14, 19, 30, tzinfo=UTC))
    p.car_cycle(datetime(2026, 11, 14, 19, 30, tzinfo=UTC))
    p2 = Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl"), car_log_path=str(tmp_path / "c.jsonl"))
    assert p2._last_full() == datetime(2026, 11, 14, 19, 30, tzinfo=UTC)
    ha.numbers[DEFAULTS["car_soc_entity"]] = 60.0
    snap = p2.run_once(datetime(2026, 11, 20, 19, 30, tzinfo=UTC))
    assert snap["car"]["full_charge"]["status"] == "too_soon"


def test_seconds_to_next_cycle_aligns_with_the_hour():
    from main import seconds_to_next_cycle
    assert seconds_to_next_cycle(datetime(2026, 11, 15, 11, 10, 0, tzinfo=TZ), 5) == 300
    assert seconds_to_next_cycle(datetime(2026, 11, 15, 11, 58, 0, tzinfo=TZ), 5) == 140    # 12:00:20
    assert seconds_to_next_cycle(datetime(2026, 11, 15, 11, 48, 0, tzinfo=TZ), 15) == 740   # not 12:03
    assert seconds_to_next_cycle(datetime(2026, 11, 15, 12, 0, 18, tzinfo=TZ), 5) == 300    # already in the new hour


def test_battery_fast_cycle_hands_grid_charging_over_to_the_sun(tmp_path):
    ha = make_ha()
    ha.texts.update({DEFAULTS["production_switch_entity"]: "on", DEFAULTS["reg_battery_first"]: "Enabled",
                     DEFAULTS["reg_grid_charge"]: "Enabled"})
    ha.numbers.update({DEFAULTS["reg_max_soc"]: 72.0, DEFAULTS["reg_min_soc"]: 66.0, DEFAULTS["reg_charge_rate"]: 25.0})
    calls = []

    def call_service(domain, service, data=None, return_response=False):
        calls.append((domain, (data or {}).get("entity_id"), (data or {}).get("option", (data or {}).get("value"))))
        if domain == "select":
            ha.texts[data["entity_id"]] = data["option"]
        elif domain == "number":
            ha.numbers[data["entity_id"]] = data["value"]
    ha.call_service = call_service
    ha.state = lambda e: ({"state": str(ha.numbers.get(e, ha.texts.get(e))), "attributes": {}}
                          if e in ha.numbers or e in ha.texts else None)
    p = Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl"))
    p.run_once(datetime(2026, 11, 14, 10, 40, tzinfo=UTC))
    # pretend the plan is grid 72 % then sun 97 %
    p.latest["setpoints"] = {"mode": "grid_charge", "battery_first": True, "grid_charge": True, "max_soc_pct": 72,
                             "min_soc_pct": 66, "charge_rate_pct": 25, "grid_target_pct": 72,
                             "run_target_pct": 97, "solar_rate_pct": 100}
    ha.texts.update({DEFAULTS["reg_battery_first"]: "Enabled", DEFAULTS["reg_grid_charge"]: "Enabled"})
    ha.numbers.update({DEFAULTS["reg_max_soc"]: 72.0, DEFAULTS["reg_min_soc"]: 66.0, DEFAULTS["reg_charge_rate"]: 25.0})
    calls.clear()
    ha.numbers[DEFAULTS["battery_soc_entity"]] = 70.0
    assert p.battery_fast_cycle(datetime(2026, 11, 14, 10, 50, tzinfo=UTC)) == []          # below the grid target
    ha.numbers[DEFAULTS["battery_soc_entity"]] = 72.0
    writes = p.battery_fast_cycle(datetime(2026, 11, 14, 10, 52, tzinfo=UTC))
    keys = [w["key"] for w in writes]
    assert keys[0] == "reg_grid_charge" and "reg_max_soc" in keys
    assert ha.texts[DEFAULTS["reg_grid_charge"]] == "Disabled" and ha.numbers[DEFAULTS["reg_max_soc"]] == 97
    assert p.latest["setpoints_live"]["handed_over"]
    ha.texts[DEFAULTS["production_switch_entity"]] = "off"
    assert p.battery_fast_cycle() is None


def test_plan_respects_the_main_fuse_and_reports_it(tmp_path):
    ha = car_ha()
    p = Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl"), car_log_path=str(tmp_path / "c.jsonl"))
    snap = p.run_once(datetime(2026, 11, 14, 19, 30, tzinfo=UTC))
    assert snap["fuse"]["plan_limit_amps"] == 22
    assert any("phase current sensors" in w for w in snap["warnings"])    # configured but missing
    for r in snap["plan"]:
        dt = r.get("dt", 1.0) or 1.0
        car_kw = (r.get("car_grid") or 0) / dt
        assert car_kw / 0.69 <= 22 + 0.01


def test_battery_fast_cycle_caps_the_charge_rate_when_a_phase_is_over_the_fuse(tmp_path):
    ha = make_ha()
    phases = [e for e in DEFAULTS["grid_current_entities"].split(",")]
    ha.texts.update({DEFAULTS["production_switch_entity"]: "on"})
    ha.numbers.update({DEFAULTS["reg_charge_rate"]: 40.0, DEFAULTS["reg_max_soc"]: 90.0, DEFAULTS["reg_min_soc"]: 10.0})
    ha.texts.update({DEFAULTS["reg_battery_first"]: "Enabled", DEFAULTS["reg_grid_charge"]: "Enabled"})
    ha.call_service = lambda domain, service, data=None, return_response=False: ha.numbers.__setitem__(
        data["entity_id"], data["value"]) if domain == "number" else ha.texts.__setitem__(data["entity_id"], data["option"])
    ha.state = lambda e: ({"state": str(ha.numbers.get(e, ha.texts.get(e))), "attributes": {}}
                          if e in ha.numbers or e in ha.texts else None)
    p = Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl"))
    p.run_once(datetime(2026, 11, 14, 1, 40, tzinfo=UTC))
    p.latest["setpoints"] = {"mode": "grid_charge", "battery_first": True, "grid_charge": True, "max_soc_pct": 90,
                             "min_soc_pct": 10, "charge_rate_pct": 40, "grid_target_pct": 90, "run_target_pct": 90}
    ha.numbers[DEFAULTS["reg_charge_rate"]] = 40.0
    ha.numbers.update({phases[0]: 28.0, phases[1]: 15.0, phases[2]: 15.0})   # 4 A over 24 A
    p.car_latest = None                                                       # car not charging
    writes = p.battery_fast_cycle(datetime(2026, 11, 14, 1, 50, tzinfo=UTC))
    rate = [w for w in writes if w["key"] == "reg_charge_rate"]
    assert rate and rate[0]["to"] == 10                     # 4 A x 3 phases = 2.8 kW = 28 % of 10 kW -> -30 %
    assert p.latest["setpoints_live"]["fuse_capped"]
    # the car still has room to give: the car guard acts first, the battery is left alone
    p._fuse_rate_cap = None
    ha.numbers[DEFAULTS["reg_charge_rate"]] = 40.0
    p.car_latest = {"action": "charge", "fuse": {"limited": True, "at_minimum": False}}
    assert not [w for w in p.battery_fast_cycle(datetime(2026, 11, 14, 1, 51, tzinfo=UTC)) if w["key"] == "reg_charge_rate"]
    # room again: recovers in 10 % steps back to the plan
    p.car_latest = None
    p._fuse_rate_cap = 30
    ha.numbers.update({phases[0]: 12.0, phases[1]: 12.0, phases[2]: 12.0})
    p.battery_fast_cycle(datetime(2026, 11, 14, 1, 52, tzinfo=UTC))
    assert p._fuse_rate_cap is None and ha.numbers[DEFAULTS["reg_charge_rate"]] == 40


def test_kia_stop_that_errors_but_pauses_the_car_is_not_a_failure_and_no_charger_stop(tmp_path):
    """Last night: the Kia stop came back with an error while the car did pause; 3 min later the
    charger was stopped and the cheap hours after were lost."""
    from homeassistant import ServiceCallError
    ha = kia_failing_ha(soc=45.0)
    p = planner_for(ha, tmp_path)
    p.run_once(NIGHT)

    def ws(domain, service, data=None):
        ha.kia.append(service)
        ha.texts[DEFAULTS["charger_status_entity"]] = "suspended_ev"     # the car follows ...
        raise ServiceCallError("Read timed out", "home_assistant_error")  # ... but the call fails
    ha.call_service_ws = ws
    p._kia_fails = 1                                                     # an earlier failure
    d = p.car_cycle(NIGHT)
    assert ha.kia == ["stop_charge"] and p._kia_fails == 0 and d["executed"][0]["status"] == "ok"
    for m in (2, 4, 6, 8):
        p.car_cycle(NIGHT + timedelta(minutes=m))
    assert not any(x[1] == "press" for x in ha.local)


def test_no_charger_stop_when_the_charger_no_longer_charges_at_the_fresh_read(tmp_path):
    ha = kia_failing_ha(soc=45.0)
    p = planner_for(ha, tmp_path)
    p.run_once(NIGHT)
    p.car_cycle(NIGHT)
    status = DEFAULTS["charger_status_entity"]
    real_text = ha.text
    # the cycle starts with "charging" (state), but the charger says paused when read again
    ha.text = lambda e: "suspended_ev" if e == status else real_text(e)
    ha.state = lambda e: ({"state": "charging", "last_changed": "2026-11-14T10:00:00+00:00"} if e == status
                          else ({"state": ha.texts[e]} if e in ha.texts else None))
    p.car_cycle(NIGHT + timedelta(minutes=6))
    assert not any(x[1] == "press" for x in ha.local)


def test_no_charger_stop_at_a_lifted_limit_while_more_is_planned_later(tmp_path):
    ha = kia_failing_ha(soc=60.0)
    ha.numbers[DEFAULTS["car_soc_entity"]] = 45.0
    p = planner_for(ha, tmp_path)
    p.set_car_extra("target", 70, NIGHT)                  # extra: charged in the cheap hours later
    ha.numbers[DEFAULTS["car_soc_entity"]] = 60.0
    p.run_once(NIGHT)
    assert p._car_planned_ahead(p.latest, NIGHT) > 0.5
    p.latest["car"]["max_soc_pct"] = 60                   # the car is "at the limit" right now
    for m in (0, 2, 4, 6, 8):
        p.car_cycle(NIGHT + timedelta(minutes=m))
    assert not any(x[1] == "press" for x in ha.local)


SCHED_START, SCHED_STOP = DEFAULTS["charger_schedule_entities"].split(",")


def test_thor_schedule_is_a_visible_warning(tmp_path):
    ha = car_ha()
    p = Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl"), car_log_path=str(tmp_path / "c.jsonl"))
    assert not any("ladeplan" in w for w in p.run_once(NIGHT)["warnings"])          # entities missing
    ha.texts.update({SCHED_START: "03:00:00", SCHED_STOP: "03:00:00"})
    assert not any("ladeplan" in w for w in p.run_once(NIGHT)["warnings"])          # start = stop: off
    ha.texts[SCHED_STOP] = "05:00:00"
    assert any("ladeplan (03:00–05:00)" in w for w in p.run_once(NIGHT)["warnings"])
    ha.texts[DEFAULTS["car_production_switch_entity"]] = "on"
    d = p.car_cycle(NIGHT)
    assert "ShinePhone" in d["warning"]


def test_session_ended_by_the_charger_is_detected(tmp_path):
    ha = kia_failing_ha(soc=30.0, message=None)          # minimum still needed: more planned later
    p = planner_for(ha, tmp_path)
    p.run_once(NIGHT)
    ha.texts[DEFAULTS["charger_status_entity"]] = "suspended_ev"
    d = p.car_cycle(NIGHT)
    assert not d["warning"] or "afsluttede" not in d["warning"]
    ha.texts[DEFAULTS["charger_status_entity"]] = "finishing"
    d = p.car_cycle(NIGHT + timedelta(minutes=2))
    assert "afsluttede sessionen selv kl. 20:32" in d["warning"]
    assert not any(x[1] == "press" for x in ha.local)
    ha.texts[DEFAULTS["charger_status_entity"]] = "preparing"       # re-plugged
    d = p.car_cycle(NIGHT + timedelta(minutes=10))
    assert not d["warning"] or "afsluttede" not in d["warning"]


def _solar_planner(tmp_path):
    import pvdampening as pvd
    from fixtures import SOLCAST_HOURLY, solcast_attrs
    history, actual_rows = {}, []
    for d in range(0, 15):
        for hl in range(8, 16):
            t = (datetime(2026, 11, 14, hl, tzinfo=TZ) - timedelta(days=d)).astimezone(UTC)
            clear = SOLCAST_HOURLY[hl][2]
            history[t.isoformat()] = {"p50": clear * 0.8, "p10": clear * 0.4, "p90": clear, "applied": 1.0,
                                      "limited": False}
            actual_rows.append({"start": t.timestamp() * 1000, "mean": clear * 0.6 * 1000})
    pvd.save_json(str(tmp_path / "pv_history.json"), history)
    ha = make_ha(stats={DEFAULTS["house_load_entity"]: [], DEFAULTS["pv_power_entity"]: actual_rows})
    ha.attrs["sensor.solcast_pv_forecast_forecast_tomorrow"] = solcast_attrs("2026-11-15")
    ha.attrs["sensor.solcast_pv_forecast_forecast_day_3"] = solcast_attrs("2026-11-16")
    ha.call_service = lambda *a, **k: {}
    return Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl")), ha


def test_solar_forecast_covers_three_days_and_the_evening_forecast_is_kept_once(tmp_path):
    p, ha = _solar_planner(tmp_path)
    snap = p.run_once(datetime(2026, 11, 14, 10, 30, tzinfo=UTC))
    days = {r["t"][:10] for r in snap["solar_forecast"]}
    assert days == {"2026-11-14", "2026-11-15", "2026-11-16"}
    r = next(r for r in snap["solar_forecast"] if r["t"].startswith("2026-11-15T12"))
    assert r["p10"] <= r["p50"] <= r["p90"] and r["plan"] > 0
    assert p._pv_dayahead == {}                                          # not before 20:00
    p.run_once(datetime(2026, 11, 14, 19, 10, tzinfo=UTC))               # 20:10 local
    key = datetime(2026, 11, 15, 12, tzinfo=TZ).astimezone(UTC).isoformat()
    first = dict(p._pv_dayahead[key])
    assert all(k.startswith("2026-11-1") for k in p._pv_dayahead)
    ha.attrs["sensor.solcast_pv_forecast_forecast_tomorrow"]["detailedHourly"][12]["pv_estimate"] = 0.01
    p.run_once(datetime(2026, 11, 14, 21, 10, tzinfo=UTC))               # a later read does not replace it
    assert p._pv_dayahead[key] == first
    p2 = Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl"))
    assert p2._pv_dayahead[key] == first                                 # persisted


def test_solar_report_for_a_past_day_and_today(tmp_path):
    p, ha = _solar_planner(tmp_path)
    now = datetime(2026, 11, 14, 11, 30, tzinfo=UTC)                     # 12:30 local
    p.run_once(now)
    from datetime import date
    y = p.solar_report(date(2026, 11, 13), now)
    rows = {r["t"][11:13]: r for r in y["hours"]}
    assert len(y["hours"]) == 24 and rows["12"]["actual"] == pytest.approx(rows["12"]["p90"] * 0.6, abs=0.01)
    assert rows["12"]["p10"] < rows["12"]["p50"] < rows["12"]["p90"] and "c50" in rows["12"]
    assert y["totals"]["actual"] < y["totals"]["p50"] and y["totals"]["hours_actual"] == 8
    t = p.solar_report(date(2026, 11, 14), now)
    assert t["totals"]["hours_actual"] == 4                               # 08-11 finished, 12 not yet
    assert t["totals"]["day_p50"] > t["totals"]["p50"]
    days = p.solar_days(3, now)
    assert [d["day"] for d in days] == ["2026-11-14", "2026-11-13", "2026-11-12"]
    assert days[1]["actual"] == pytest.approx(y["totals"]["actual"])


def test_a_hob_on_right_now_lifts_the_current_hour(tmp_path):
    hob = DEFAULTS["load_submeters"].split(",")[3]
    now = datetime(2026, 11, 14, 10, 30, tzinfo=UTC)
    start = now - timedelta(days=14)
    hours = [start + timedelta(hours=i) for i in range(24 * 14)]
    stats = {DEFAULTS["house_load_entity"]: [{"start": t.timestamp() * 1000, "mean": 600.0} for t in hours],
             hob: [{"start": t.timestamp() * 1000, "mean": 100.0} for t in hours]}
    for e in DEFAULTS["load_submeters"].split(",")[:3]:
        stats[e] = [{"start": t.timestamp() * 1000, "mean": 0.0} for t in hours]
    ha = make_ha(stats=stats)
    base = Planner(ha, tz=TZ, log_path=str(tmp_path / "a.jsonl")).run_once(now)
    ha.numbers[hob] = 2000.0
    snap = Planner(ha, tz=TZ, log_path=str(tmp_path / "b.jsonl")).run_once(now)
    assert base["pred"]["load_kw"] == pytest.approx(0.6, abs=0.01)
    assert snap["pred"]["load_kw"] == pytest.approx(0.5 + 2.0, abs=0.01)
    assert snap["pred"]["submeters_now_kw"] == pytest.approx(2.0) and snap["submeters_now"][hob] == 2.0
    assert snap["profile"]["parts"]["weekday"]["rest"][12] == pytest.approx(0.5, abs=0.01)



def test_export_is_stopped_in_an_hour_with_a_negative_hourly_price(tmp_path):
    from fixtures import SELL_TODAY, price_attrs
    exp = DEFAULTS["reg_export_limit"]
    ha = make_ha()
    sell = list(SELL_TODAY)
    sell[12] = -0.05                                                    # 12-13 local: negative hourly price
    ha.attrs[DEFAULTS["price_sell_entity"]] = price_attrs(sell)
    ha.numbers[exp] = 100.0
    calls = []

    def call_service(domain, service, data=None, return_response=False):
        if domain == "number":
            calls.append((data["entity_id"], data["value"]))
            ha.numbers[data["entity_id"]] = data["value"]
        elif domain == "select":
            ha.texts[data["entity_id"]] = data["option"]
    ha.call_service = call_service
    ha.state = lambda e: ({"state": str(ha.numbers.get(e, ha.texts.get(e))), "attributes": {}}
                          if e in ha.numbers or e in ha.texts else None)
    p = Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl"))
    snap = p.run_once(datetime(2026, 11, 14, 11, 0, 20, tzinfo=UTC))      # 12:00 local, production off
    assert snap["setpoints"]["export_allowed"] is False and snap["setpoints"]["export_limit_pct"] == 0
    assert calls == []
    assert all(r["pv_exp"] == 0 for r in snap["plan"] if r["t"][11:13] == "12")
    ha.texts[DEFAULTS["production_switch_entity"]] = "on"
    p.run_once(datetime(2026, 11, 14, 11, 0, 20, tzinfo=UTC))
    assert (exp, 0) in calls
    calls.clear()
    snap = p.run_once(datetime(2026, 11, 14, 12, 0, 20, tzinfo=UTC))      # 13:00: positive again
    assert snap["setpoints"]["export_allowed"] is True and (exp, 100) in calls


def _negative_noon(tmp_path, sell_noon, connected=True, car_soc=50.0):
    from fixtures import SELL_TODAY, price_attrs
    ha = car_ha() if connected else make_ha()
    if connected:
        ha.numbers[DEFAULTS["car_soc_entity"]] = car_soc
    sell = list(SELL_TODAY)
    sell[12] = sell_noon
    ha.attrs[DEFAULTS["price_sell_entity"]] = price_attrs(sell)
    ha.numbers[DEFAULTS["battery_soc_entity"]] = 100.0                    # house battery full
    p = Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl"), car_log_path=str(tmp_path / "c.jsonl"))
    return p.run_once(datetime(2026, 11, 14, 11, 0, 20, tzinfo=UTC))      # 12:00 local


def test_export_stays_open_while_the_car_charges_on_sun_down_to_the_car_limit(tmp_path):
    snap = _negative_noon(tmp_path, -0.05)
    assert snap["plan"][0]["car_pv"] > 0.1
    sp = snap["setpoints"]
    assert sp["export_allowed"] is True and sp["export_limit_pct"] == 100 and sp["export_reason"] == "car"
    assert _negative_noon(tmp_path, -0.15)["setpoints"]["export_limit_pct"] == 0           # below -0.10
    assert _negative_noon(tmp_path, -0.05, connected=False)["setpoints"]["export_reason"] == "stopped"
    assert _negative_noon(tmp_path, -0.05, car_soc=100.0)["setpoints"]["export_limit_pct"] == 0  # full (a negative sell price lifts the limit to 100 %)
    assert _negative_noon(tmp_path, 0.2)["setpoints"]["export_reason"] == "price"


def _unplugged(soc):
    ha = car_ha()
    ha.texts[DEFAULTS["charger_status_entity"]] = "available"
    ha.numbers[DEFAULTS["car_soc_entity"]] = soc
    return ha


def test_preview_shows_what_would_happen_if_the_car_were_plugged_in(tmp_path):
    ha = _unplugged(30.0)
    p = Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl"), car_log_path=str(tmp_path / "c.jsonl"))
    snap = p.run_once(datetime(2026, 11, 14, 19, 30, tzinfo=UTC))       # 20:30 local
    pv = snap["car_preview"]
    assert pv["reason"] == "minimum" and pv["soc_at_deadline"] == pytest.approx(42.0, abs=0.5)
    assert pv["planned_kwh"] == pytest.approx(0.12 * 110 / 0.92, abs=0.5) and pv["windows"]   # at the plug
    assert pv["text"].startswith("Ville lade") and "42 %" in pv["text"]
    # the real plan has no car and the house setpoints are what they would be without it
    assert not snap["car"]["connected"] and "car_grid" not in snap["plan"][0]
    alone = Planner(make_ha(), tz=TZ, log_path=str(tmp_path / "e.jsonl")).run_once(datetime(2026, 11, 14, 19, 30, tzinfo=UTC))
    assert snap["setpoints"]["mode"] == alone["setpoints"]["mode"]
    p.car_cycle(datetime(2026, 11, 14, 19, 30, tzinfo=UTC))
    attrs = ha.published["sensor.energy_planner_car"][1]
    assert attrs["preview_kwh"] == pv["planned_kwh"] and attrs["preview_text"] == pv["text"]


def test_preview_says_no_when_nothing_is_worth_charging_and_is_absent_when_plugged_or_unknown(tmp_path):
    p = Planner(_unplugged(70.0), tz=TZ, log_path=str(tmp_path / "d.jsonl"), car_log_path=str(tmp_path / "c.jsonl"))
    pv = p.run_once(datetime(2026, 11, 14, 19, 30, tzinfo=UTC))["car_preview"]
    assert pv["reason"] == "none" and pv["planned_kwh"] < 0.1 and pv["text"].startswith("Ville ikke lade")
    plugged = Planner(car_ha(), tz=TZ, log_path=str(tmp_path / "e.jsonl"), car_log_path=str(tmp_path / "f.jsonl"))
    assert plugged.run_once(datetime(2026, 11, 14, 19, 30, tzinfo=UTC))["car_preview"] is None
    ha = _unplugged(50.0)
    ha.numbers[DEFAULTS["car_soc_entity"]] = None
    unknown = Planner(ha, tz=TZ, log_path=str(tmp_path / "g.jsonl"), car_log_path=str(tmp_path / "h.jsonl"))
    assert unknown.run_once(datetime(2026, 11, 14, 19, 30, tzinfo=UTC))["car_preview"] is None


def test_an_extra_order_shows_in_the_preview(tmp_path):
    ha = _unplugged(70.0)
    p = Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl"), car_log_path=str(tmp_path / "c.jsonl"))
    now = datetime(2026, 11, 14, 19, 30, tzinfo=UTC)
    p.set_car_extra("pct", 10, now)
    pv = p.run_once(now)["car_preview"]
    assert pv["reason"] == "extra" and pv["soc_at_deadline"] == pytest.approx(80.0, abs=0.5)


def _hp_ha(production, temp=14.0):
    ha = make_ha()
    hp = DEFAULTS["heat_pump_entity"]
    ha.numbers.update({DEFAULTS["heat_pump_temp_entity"]: temp, DEFAULTS["heat_pump_humidity_entity"]: 55.0})
    ha.texts[DEFAULTS["heat_pump_production_switch_entity"]] = "on" if production else "off"
    ha.climate = {"state": "heat", "attributes": {"temperature": 12}}
    real_state = ha.state
    ha.state = lambda e: ha.climate if e == hp else real_state(e)
    calls = []

    def call_service(domain, service, data=None, return_response=False):
        if domain == "climate":
            calls.append(data)
            if service == "set_hvac_mode":
                ha.climate["state"] = data["hvac_mode"]
            else:
                ha.climate["attributes"]["temperature"] = data["temperature"]
    ha.call_service = call_service
    return ha, calls


def test_heat_pump_setpoint_follows_the_price_and_is_set_once(tmp_path):
    ha, calls = _hp_ha(production=True)
    p = Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl"))
    snap = p.run_once(datetime(2026, 11, 14, 2, 0, 20, tzinfo=UTC))       # 03:00 local, among the cheapest
    hp = snap["heat_pump"]
    assert hp["level"] == "cheap" and hp["setpoint"] == 14
    assert calls == [{"entity_id": DEFAULTS["heat_pump_entity"], "temperature": 14}]
    p.run_once(datetime(2026, 11, 14, 2, 10, tzinfo=UTC))
    assert len(calls) == 1                                                # unchanged: nothing written
    snap = p.run_once(datetime(2026, 11, 14, 15, 0, 20, tzinfo=UTC))      # 16:00, dear but not the dearest
    assert snap["heat_pump"]["setpoint"] == 10 and calls[-1]["temperature"] == 10
    assert ha.published["sensor.energy_planner_heat_pump"][0] == 10


def test_heat_pump_is_switched_off_in_the_dearest_hours_and_on_again_by_the_add_on_only(tmp_path):
    ha, calls = _hp_ha(production=True)
    p = Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl"))
    snap = p.run_once(datetime(2026, 11, 14, 16, 0, 20, tzinfo=UTC))      # 17:00, the peak
    assert snap["heat_pump"]["hvac"] == "off" and calls == [{"entity_id": DEFAULTS["heat_pump_entity"], "hvac_mode": "off"}]
    assert ha.climate["state"] == "off" and ha.published["sensor.energy_planner_heat_pump"][0] == "off"
    p.run_once(datetime(2026, 11, 14, 16, 30, tzinfo=UTC))
    assert len(calls) == 1
    snap = p.run_once(datetime(2026, 11, 14, 21, 0, 20, tzinfo=UTC))      # 22:00, cheaper: on again, then the setpoint
    assert calls[1] == {"entity_id": DEFAULTS["heat_pump_entity"], "hvac_mode": "heat"} and ha.climate["state"] == "heat"
    assert "temperature" in calls[2]
    # switched off by the user: left alone
    ha2, calls2 = _hp_ha(production=True)
    ha2.climate["state"] = "off"
    Planner(ha2, tz=TZ, log_path=str(tmp_path / "e.jsonl")).run_once(datetime(2026, 11, 14, 21, 0, 20, tzinfo=UTC))
    assert calls2 == []


def test_heat_pump_manual_control_writes_nothing(tmp_path):
    ha, calls = _hp_ha(production=False)
    snap = Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl")).run_once(datetime(2026, 11, 14, 16, 0, 20, tzinfo=UTC))
    assert snap["heat_pump"]["setpoint"] == 10 and snap["heat_pump"]["production"] is False and calls == []
    assert ha.published["sensor.energy_planner_heat_pump"][1]["production"] is False


def test_heat_pump_level_forecast_replaces_its_profile_in_the_load(tmp_path):
    hp_meter = DEFAULTS["heat_pump_power_entity"]
    now = datetime(2026, 11, 14, 10, 30, tzinfo=UTC)
    start = now - timedelta(days=14)
    hours = [start + timedelta(hours=i) for i in range(24 * 14)]
    stats = {DEFAULTS["house_load_entity"]: [{"start": t.timestamp() * 1000, "mean": 800.0} for t in hours]}
    for e in DEFAULTS["load_submeters"].split(","):
        stats[e] = [{"start": t.timestamp() * 1000, "mean": 250.0 if e == hp_meter else 0.0} for t in hours]
    ha, calls = _hp_ha(production=True)
    ha.stats = stats
    base = Planner(ha, tz=TZ, log_path=str(tmp_path / "a" / "d.jsonl")).run_once(now)
    p = Planner(ha, tz=TZ, log_path=str(tmp_path / "b" / "d.jsonl"))
    # 10 days of history: 0.6 kWh in cheap hours, 0.05 kWh in dear hours
    import heatpump as hpmod
    for i in range(1, 240):
        t = hour_floor_utc(now) - timedelta(hours=i)
        lv = ["cheap", "dear", "normal"][i % 3]
        hpmod.record_level(p._hp_levels, t, lv, 12, True, now)
        stats[hp_meter].append({"start": t.timestamp() * 1000, "mean": {"cheap": 600.0, "dear": 50.0, "normal": 250.0}[lv]})
    snap = p.run_once(now)
    assert set(snap["heat_pump"]["expected"]) == {"cheap", "normal", "dear"}
    key = lambda t: datetime.fromisoformat(t).astimezone(UTC)
    by_t = {key(r["t"]): r for r in snap["plan"]}
    base_t = {key(r["t"]): r for r in base["plan"]}
    levels = {key(x["t"]): x["level"] for x in snap["heat_pump"]["plan"]}
    later = hour_floor_utc(now) + timedelta(hours=2)                     # the live meters cover the first two
    dear = [k for k, v in levels.items() if v == "dear" and k in by_t and k >= later]
    cheap = [k for k, v in levels.items() if v == "cheap" and k in by_t and k >= later]
    assert dear and cheap
    assert by_t[dear[0]]["load"] < base_t[dear[0]]["load"] - 0.1
    assert by_t[cheap[0]]["load"] > base_t[cheap[0]]["load"] + 0.1


def hour_floor_utc(t):
    return t.replace(minute=0, second=0, microsecond=0)


def test_trace_starts_when_charging_stops_and_by_hand(tmp_path):
    ha = car_ha()
    status = DEFAULTS["charger_status_entity"]
    ha.all_states = lambda: [{"entity_id": status, "state": ha.texts[status], "attributes": {}}]
    p = Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl"), car_log_path=str(tmp_path / "c.jsonl"))
    ha.texts[status] = "charging"
    p.trace_tick(datetime(2026, 11, 18, 1, 55, tzinfo=UTC))
    assert not p.trace.active()
    ha.texts[status] = "suspended_ev"
    p.trace_tick(datetime(2026, 11, 18, 1, 55, 41, tzinfo=UTC))
    assert p.trace.active() and p.trace.list()[0]["trigger"].startswith("ukendt")
    ha.texts[status] = "finishing"
    p.trace_tick(datetime(2026, 11, 18, 1, 58, 41, tzinfo=UTC))
    ch = p.trace.list()[0]["changes"]
    assert ch[-1]["s"] == 180 and ch[-1]["to"] == "finishing"
    p2 = Planner(ha, tz=TZ, log_path=str(tmp_path / "e" / "d.jsonl"), car_log_path=str(tmp_path / "f.jsonl"))
    p2.trace_tick(datetime(2026, 11, 18, 12, 0, tzinfo=UTC), manual=True)
    assert p2.trace.active() and p2.trace.list()[0]["trigger"] == "manuel start fra panelet"



def test_pause_goes_through_the_car_charging_switch_not_stop_charge(tmp_path):
    """kia_uvo.stop_charge makes the IONIQ end the session (the THOR then registers an unplug after
    3 min); switch.ioniq_9_ev_charging only pauses it, and kia_uvo.start_charge resumes."""
    ha = kia_failing_ha(soc=45.0, message=None)
    sw = DEFAULTS["car_charge_switch_entity"]
    p = planner_for(ha, tmp_path, car_charge_switch_entity=sw)
    p.run_once(NIGHT)
    calls = []

    def ws(domain, service, data=None):
        calls.append((domain, service, data))
    ha.call_service_ws = ws
    d = p.car_cycle(NIGHT)
    assert d["action"] == "pause"
    assert calls == [("switch", "turn_off", {"entity_id": sw})]
    ex = d["executed"][0]
    assert ex["service"] == "kia_uvo.stop_charge" and ex["via"] == sw and ex["status"] == "ok"
    from writer import allowed_entities
    assert sw in allowed_entities(p.options_fn())


def test_car_cycle_uses_the_estimated_soc_between_kia_updates(tmp_path):
    ha = car_ha()
    ha.texts[DEFAULTS["charger_status_entity"]] = "charging"
    ha.numbers[DEFAULTS["car_soc_entity"]] = 88.0
    ha.texts[DEFAULTS["car_soc_time_entity"]] = "2026-11-14T19:00:00+00:00"
    ha.numbers[DEFAULTS["charger_power_entity"]] = 11000.0
    p = Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl"), car_log_path=str(tmp_path / "c.jsonl"))
    t = datetime(2026, 11, 14, 18, 59, tzinfo=UTC)
    while t <= datetime(2026, 11, 14, 19, 14, tzinfo=UTC):                 # 15 min at 11 kW after the report
        p.trace_tick(t)
        t += timedelta(seconds=5)
    now = datetime(2026, 11, 14, 19, 14, tzinfo=UTC)
    snap = p.run_once(now)
    est = snap["car"]["soc_estimate"]
    assert est["estimated"] and est["soc"] == pytest.approx(88 + 2.57 * 0.92 / 110 * 100, abs=0.3)
    d = p.car_cycle(now)
    assert d["live"]["car_soc"] > 90 and d["live"]["car_soc_kia"] == 88.0
    assert d["action"] == "pause" and "max SoC" in d["reason"]           # stops at 90 % before Kia reports it
    attrs = ha.published["sensor.energy_planner_car"][1]
    assert attrs["soc_kia"] == 88.0 and attrs["soc_estimated"] > 90


def test_front_page_sensors(tmp_path):
    ha = _unplugged(30.0)
    p = Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl"), car_log_path=str(tmp_path / "c.jsonl"))
    now = datetime(2026, 11, 14, 19, 30, tzinfo=UTC)
    p.run_once(now)
    price = ha.published["sensor.energy_planner_price"]
    assert price[1]["level"] in ("cheap", "normal", "dear") and "billigst" in price[1]["cheapest_text"]
    mode = ha.published["sensor.energy_planner_mode"][1]
    assert mode["title"].endswith("73 %") and mode["soc_pct"] == 73.0
    p.car_cycle(now)
    adv = ha.published["sensor.energy_planner_car_advice"]
    assert adv[0] == "plug_in" and adv[1]["title"] == "Sæt bilen til" and "kWh" in adv[1]["text"]
    w = ha.published["sensor.energy_planner_warnings"]
    assert w[0] >= 1 and any("Manuel styring" in m for m in w[1]["messages"])      # helpers are off in the test
    ha.texts.update({DEFAULTS["production_switch_entity"]: "on", DEFAULTS["car_production_switch_entity"]: "on",
                     DEFAULTS["heat_pump_production_switch_entity"]: "on"})
    p.entity_check = {}
    p._publish_warnings()
    assert ha.published["sensor.energy_planner_warnings"][0] == 0
    cost = ha.published["sensor.energy_planner_cost_today"][1]
    assert "kr indtil nu" in cost["text"]


def test_savings_today_from_the_counters_and_the_rest_of_the_plan(tmp_path):
    from settings import counter_ids
    ids = counter_ids(DEFAULTS)
    day = datetime(2026, 11, 14, tzinfo=TZ)
    rows = {k: [] for k in (ids["import"], ids["export"], ids["solar"], ids["battery_in"], ids["battery_out"],
                            DEFAULTS["price_buy_entity"], DEFAULTS["price_sell_entity"])}
    for h in range(11):                                                   # 00:00-11:00 local
        ms = (day + timedelta(hours=h)).astimezone(UTC).timestamp() * 1000
        sun = 2.0 if h >= 8 else 0.0
        rows[ids["import"]].append({"start": ms, "change": 0.2})
        rows[ids["export"]].append({"start": ms, "change": 0.0})
        rows[ids["solar"]].append({"start": ms, "change": sun})
        rows[ids["battery_in"]].append({"start": ms, "change": 0.0})
        rows[ids["battery_out"]].append({"start": ms, "change": 0.5 if h < 8 else 0.0})
        rows[DEFAULTS["price_buy_entity"]].append({"start": ms, "mean": 2.0})
        rows[DEFAULTS["price_sell_entity"]].append({"start": ms, "mean": 0.5})
    ha = make_ha(stats={DEFAULTS["house_load_entity"]: [], **rows})
    p = Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl"))
    p.run_once(datetime(2026, 11, 14, 10, 5, tzinfo=UTC))                # 11:05 local
    state, attrs = ha.published["sensor.energy_planner_savings_today"]
    # consumption = import + sun + battery out = 11*0.2 + 3*2 + 8*0.5 = 12.2 kWh; saved = (12.2 - 2.2) * 2 kr
    # battery: 8 h x 0.5 kWh out at 2 kr, nothing charged today = 8 kr; the whole system saved 20 kr
    assert state == pytest.approx(8.0, abs=0.01) and attrs["solar_kwh"] == 6.0
    assert attrs["system_value_dkk"] == pytest.approx(20.0, abs=0.01) and attrs["solar_value_dkk"] == pytest.approx(12.0, abs=0.01)
    assert attrs["title"] == "Tjent 8 kr i dag" and attrs["text"].startswith("forventet ")
    assert "sol 6,0 af" in attrs["text"] and attrs["expected_dkk"] == pytest.approx(state + attrs["rest_dkk"])
    assert "sol 6,0 af" in ha.published["sensor.energy_planner_cost_today"][1]["text"]


def test_savings_expected_values_the_days_soc_change_from_midnight_not_from_now(tmp_path):
    p = Planner(make_ha(stats={}), tz=TZ, log_path=str(tmp_path / "d.jsonl"))
    now = datetime(2026, 11, 20, 13, 5, tzinfo=UTC)                         # 14:05 local
    with open(tmp_path / "d.jsonl", "w") as f:
        f.write(json.dumps({"updated": "2026-11-20T00:05:00+01:00", "soc_pct": 10.0}) + "\n")
        f.write(json.dumps({"updated": "2026-11-20T14:00:00+01:00", "soc_pct": 58.0}) + "\n")
    plan = [{"t": f"2026-11-20T{h:02d}:00:00+01:00", "buy": 3.45 if 17 <= h < 21 else 2.0, "sell": 1.0,
             "load": 0.9 if 17 <= h < 21 else 0.5, "grid_load": 0.0 if 17 <= h < 21 else 0.5, "grid_bat": 0.0,
             "bat_load": 0.9 if 17 <= h < 21 else 0.0, "pv_bat": 0.0,
             "pv_exp": 0.0, "soc_pct": 10.0 if h >= 21 else 58.0 - (h - 16) * 12 if h >= 17 else 58.0}
            for h in range(14, 24)]
    p.latest = {"plan": plan, "soc_pct": 58.0}
    # so far today only sun into the battery: a lost sale of 5 kr
    p._today_economy = lambda opts, now: {"value_dkk": 0.0, "battery_value_dkk": -5.0, "solar_value_dkk": 5.0,
                                          "net_dkk": 1.0, "solar_kwh": 5.4}
    p._publish_savings(DEFAULTS, now)
    state, attrs = p.ha.published["sensor.energy_planner_savings_today"]
    assert state == -5.0 and attrs["title"] == "Kostet 5 kr indtil nu"
    assert attrs["soc_start_pct"] == 10.0 and attrs["battery_adjust_dkk"] == pytest.approx(0.0)
    assert attrs["expected_dkk"] == pytest.approx(-5.0 + 4 * 0.9 * 3.45)    # the evening's discharge counts once
    assert attrs["text"].startswith("forventet +7 kr i alt")
    # sun into the battery while the house buys (battery first) costs the buy price, and the current
    # hour (the counters stop at the last whole hour) counts in full
    plan[0].update(t="2026-11-20T14:00:00+01:00", dt=0.5, pv_bat=0.5, grid_load=0.25, buy=2.0, sell=1.0)
    p._publish_savings(DEFAULTS, now)
    _, attrs = p.ha.published["sensor.energy_planner_savings_today"]
    assert attrs["expected_dkk"] == pytest.approx(-5.0 + 4 * 0.9 * 3.45 - (0.25 * 2.0 + 0.25 * 1.0) / 0.5)


class _P:
    def __init__(self, bat_out):
        import numpy as np
        self.bat_load = np.array([bat_out, 0.0])
        self.bat_car = np.array([0.0, 0.0])


def _sp(mode, mn, via=None):
    return {"mode": mode, "min_soc_pct": mn, "hold_via": via, "battery_first": False, "grid_charge": False,
            "max_soc_pct": None, "charge_rate_pct": 100}


def test_modes_stay_calm_within_the_hour_and_the_hold_floor_stays_put(tmp_path):
    p = Planner(make_ha(), tz=TZ, log_path=str(tmp_path / "d.jsonl"))
    t = datetime(2026, 11, 20, 8, 19, tzinfo=UTC)
    first = p._stabilize(_sp("hold", 29, "load_first"), _P(0.0), t, 29.0)
    assert first["mode"] == "hold" and first["min_soc_deadband"] == 0         # a new mode writes its floor
    # 5 min later a discharge of 0.05 kWh: not worth switching within the hour
    s = p._stabilize(_sp("discharge", 10), _P(0.05), t + timedelta(minutes=5), 29.0)
    assert s["mode"] == "hold" and "kept" in s["kept_mode"]
    # the sun charges the battery: the hold keeps the floor it started with
    s = p._stabilize(_sp("hold", 35, "load_first"), _P(0.0), t + timedelta(minutes=10), 35.0)
    assert s["min_soc_pct"] == 29 and s["min_soc_deadband"] == 2
    # a real discharge switches
    s = p._stabilize(_sp("discharge", 10), _P(0.8), t + timedelta(minutes=15), 35.0)
    assert s["mode"] == "discharge" and s["min_soc_deadband"] == 0
    # a new hour always follows the plan
    s = p._stabilize(_sp("hold", 33, "load_first"), _P(0.05), t + timedelta(minutes=45), 33.0)
    assert s["mode"] == "hold" and s["min_soc_pct"] == 33


def test_plan_rows_have_sun_to_house(tmp_path):
    snap = Planner(make_ha(), tz=TZ, log_path=str(tmp_path / "d.jsonl")).run_once(datetime(2026, 11, 14, 10, 30, tzinfo=UTC))
    r = snap["plan"][1]
    assert "pv_load" in r and r["pv_load"] + r["grid_load"] == pytest.approx(r["load"] - r["bat_load"], abs=0.02)


def strom_prices(day, values):
    t0 = datetime.fromisoformat(f"{day}T00:00:00+01:00")
    return {"prices": [{"price": p, "start": (t0 + timedelta(hours=i)).isoformat(),
                        "end": (t0 + timedelta(hours=i + 1)).isoformat()} for i, p in enumerate(values)]}


def test_stromligning_today_old_sensor_tomorrow_and_forecast_after(tmp_path):
    ha = make_ha()
    ha.attrs[DEFAULTS["price_buy_entity"]] = strom_prices("2026-11-14", BUY_TODAY)
    ha.attrs[DEFAULTS["price_sell_entity"]] = strom_prices("2026-11-14", SELL_TODAY)
    ha.attrs[DEFAULTS["price_buy_fallback_entity"]] = {"raw_today": None, "raw_tomorrow": price_attrs(
        [b - 0.2 for b in BUY_TODAY], day="2026-11-15")["raw_today"]}
    ha.attrs[DEFAULTS["price_sell_fallback_entity"]] = {"raw_tomorrow": price_attrs(SELL_TODAY, day="2026-11-15")["raw_today"]}
    ha.attrs[DEFAULTS["price_buy_forecast_entity"]] = strom_prices("2026-11-16", [5.0] * 48)
    p = Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl"))
    snap = p.run_once(datetime(2026, 11, 15, 20, 30, tzinfo=UTC))   # 21:30 local on the 15th
    rows = {r["t"][:13]: r for r in snap["plan"]}
    assert rows["2026-11-15T22"]["price_source"] == "known" and rows["2026-11-15T22"]["buy"] == pytest.approx(BUY_TODAY[22] - 0.2)
    f = rows["2026-11-16T10"]
    assert f["price_source"] == "forecast" and f["forecast_price"] and f["buy"] == pytest.approx(5.0)
    assert f["sell"] == pytest.approx(5.0 - (BUY_TODAY[10] - 0.2 - SELL_TODAY[10]))
    assert not any("same hour of the last known day" in w for w in snap["warnings"])


def test_old_sensors_alone_when_stromligning_is_unavailable(tmp_path):
    ha = make_ha()
    ha.attrs[DEFAULTS["price_buy_fallback_entity"]] = ha.attrs.pop(DEFAULTS["price_buy_entity"])
    ha.attrs[DEFAULTS["price_sell_fallback_entity"]] = ha.attrs.pop(DEFAULTS["price_sell_entity"])
    snap = Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl")).run_once(datetime(2026, 11, 14, 11, 30, tzinfo=UTC))
    assert snap["horizon_hours"] == 30 and snap["plan"][0]["price_source"] == "known"
    assert snap["plan"][-1]["price_source"] == "repeat"


def test_car_price_history_merges_state_of_the_new_and_mean_of_the_old(tmp_path):
    now = datetime(2026, 11, 14, 11, 30, tzinfo=UTC)
    h1, h2 = now - timedelta(hours=3), now - timedelta(hours=2)
    stats = {DEFAULTS["price_buy_entity"]: [{"start": h2.timestamp() * 1000, "state": 2.5}],
             DEFAULTS["price_buy_fallback_entity"]: [{"start": h1.timestamp() * 1000, "mean": 1.0},
                                                     {"start": h2.timestamp() * 1000, "mean": 9.0}]}
    ha = make_ha(stats=stats)
    p = Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl"), options_fn=lambda: {**DEFAULTS, "car_enabled": True})
    p._refresh_car_history({**DEFAULTS, "car_enabled": True}, now)
    assert p._price_history == [1.0, 2.5]


def test_small_car_sessions_are_taken_out_of_the_plan_and_solved_again():
    import numpy as np
    from optimizer import CarParams

    class Pl:
        def __init__(self, kwh):
            self.pv_car, self.grid_car, self.bat_car = np.array(kwh), np.zeros(len(kwh)), np.zeros(len(kwh))
    calls = []

    def solve_fn(cp):          # the "LP": 0.1 + 0.5 kWh at 13-14 while those hours are available, 4 kWh at night
        calls.append(list(cp.available))
        return Pl([0.1 if cp.available[0] else 0.0, 0.5 if cp.available[1] else 0.0, 0, 0, 0, 0, 4.0])
    cp = CarParams(available=[True] * 7, max_kw=11, eta=0.92, soc0_kwh=50, max_kwh=99, value_dkk_per_kwh=1.0)
    pl, cp2, dropped = Planner._min_session(solve_fn, cp, DEFAULTS)
    assert dropped and cp2.available == [True, False, True, True, True, True, True] and len(calls) == 2
    assert float(pl.pv_car.sum()) == pytest.approx(4.1)       # 0.1 kWh at 13 is below the 0.1 kWh threshold, left
    running = lambda c: Pl([0.5, 0, 0, 0, 0, 0, 4.0])       # noqa: E731  charging now, 0.5 kWh left this session
    _, cp3, dropped = Planner._min_session(running, cp, DEFAULTS, keep_first=True)
    assert not dropped and cp3.available == cp.available
    _, _, dropped = Planner._min_session(solve_fn, cp, DEFAULTS, critical=True)
    assert not dropped


def test_appliance_card_is_published_with_defaults_and_learns_runs(tmp_path):
    p = Planner(make_ha(), tz=TZ, log_path=str(tmp_path / "d.jsonl"))
    snap = p.run_once(datetime(2026, 11, 14, 11, 30, tzinfo=UTC))
    assert [a["name"] for a in snap["appliances"]] == ["Opvask", "Vask", "Tørretumbler"]
    state, attrs = p.ha.published["sensor.energy_planner_appliances"]
    assert state == 3 and attrs["title"].startswith("Opvask ") and not attrs["appliances"][0]["learned"]
    # three dishwasher runs in the 5-minute statistics -> learned
    meter = "sensor.shelly_opvaskemaskine_power"
    rows = []
    for d in range(3):
        t = datetime(2026, 11, 10 + d, 19, 0, tzinfo=UTC)
        for i in range(24):                                     # 2 h: 30 min at 2 kW, then 150 W
            rows.append({"start": (t + timedelta(minutes=5 * i)).timestamp() * 1000, "mean": 2000.0 if i < 6 else 150.0})
    ha = make_ha(stats={meter: rows})
    p = Planner(ha, tz=TZ, log_path=str(tmp_path / "e.jsonl"))
    snap = p.run_once(datetime(2026, 11, 14, 11, 30, tzinfo=UTC))
    dish = snap["appliances"][0]
    assert dish["learned"] and dish["learned_runs"] == 3 and dish["minutes"] == 120
    assert dish["kwh"] == pytest.approx(1.0 + 0.225, abs=0.01)
    assert (tmp_path / "appliance_runs.json").exists()


def test_tomorrows_forecast_is_remembered_from_yesterdays_sensor_and_history(tmp_path):
    now = datetime(2026, 11, 15, 8, 30, tzinfo=UTC)                     # 09:30 on the 15th, before 13:00
    ha = make_ha()
    ha.attrs[DEFAULTS["price_buy_entity"]] = strom_prices("2026-11-15", BUY_TODAY)
    ha.attrs[DEFAULTS["price_sell_entity"]] = strom_prices("2026-11-15", SELL_TODAY)
    ha.attrs[DEFAULTS["price_buy_forecast_entity"]] = strom_prices("2026-11-17", [5.0] * 24)   # from overmorgen
    yesterday = {"attributes": strom_prices("2026-11-16", [4.0] * 24)}                       # seen yesterday
    ha.history = lambda e, start, end=None: [yesterday] if e == DEFAULTS["price_buy_forecast_entity"] else []
    p = Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl"))
    snap = p.run_once(now)
    tomorrow = [r for r in snap["plan"] if r["t"].startswith("2026-11-16")]
    assert tomorrow and all(r["price_source"] == "forecast" and r["buy"] == pytest.approx(4.0) for r in tomorrow)
    # remembered in /data: a restart without history still has it
    ha.history = lambda e, start, end=None: []
    snap = Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl")).run_once(now)
    assert all(r["price_source"] == "forecast" for r in snap["plan"] if r["t"].startswith("2026-11-16"))
