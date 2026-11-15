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
    assert snap["dry_run"] is True and snap["production"]["writes"] == []
    assert not any(c[0] in ("select", "number") for c in ha.calls)
    assert ha.published["sensor.energy_planner_mode"][1]["dry_run"] is True


def test_production_switch_on_writes_the_registers(tmp_path):
    ha = make_ha()
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
    assert snap["dry_run"] is False and snap["production"]["battery"] is True
    assert snap["mode"] == "discharge"                                  # 17:30 local, the peak
    written = [c[2] for c in calls if c[0] in ("select", "number")]
    assert written[:2] == [DEFAULTS["reg_grid_charge"], DEFAULTS["reg_battery_first"]]
    assert DEFAULTS["reg_export_limit"] not in written
    assert snap["actual_registers"]["reg_battery_first"] == "Disabled"   # re-read after writing
    assert ha.published["sensor.energy_planner_mode"][1]["dry_run"] is False


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
    assert d["dry_run"] is False
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
    opts = {**DEFAULTS, "car_device_id": "abc123", **opt}
    return Planner(ha, tz=TZ, log_path=str(tmp_path / "d.jsonl"), car_log_path=str(tmp_path / "c.jsonl"),
                   options_fn=lambda: opts)


NIGHT = datetime(2026, 11, 14, 19, 30, tzinfo=UTC)          # 20:30 local, expensive hour


def test_pin_error_stops_kia_calls_sets_6a_and_stops_on_the_charger_when_nothing_more_is_planned(tmp_path):
    ha = kia_failing_ha(soc=45.0)                             # above the minimum, prices above the car value
    p = planner_for(ha, tmp_path)
    p.run_once(NIGHT)
    d = p.car_cycle(NIGHT)
    assert d["action"] == "pause" and ha.kia == ["stop_charge"]
    pressed = [x for x in ha.local if x[1] == "press"]
    assert ("number", "set_value", DEFAULTS["charger_current_entity"], 6) in ha.local
    assert pressed == [("button", "press", DEFAULTS["charger_stop_button"], None)]
    assert "PIN" in d["warning"] and "Nødstop" in d["warning"]
    # no Kia retries and no restart attempts afterwards
    ha.local.clear()
    d = p.car_cycle(NIGHT + timedelta(minutes=2))
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
    assert any(x[1] == "press" for x in ha.local)             # now the emergency stop
    p.car_cycle(NIGHT + timedelta(minutes=4))                 # (after the stop: nothing more)
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
