import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import cartrace
from settings import DEFAULTS

TZ = ZoneInfo("Europe/Copenhagen")
T0 = datetime(2026, 11, 18, 2, 55, 41, tzinfo=TZ)


def states(status, plugged="on", power=11000.0, other="x"):
    return [
        {"entity_id": DEFAULTS["charger_status_entity"], "state": status, "attributes": {"friendly_name": "Status"}},
        {"entity_id": "sensor.garage_growatt_thor_ev_charger_charging_power", "state": str(power), "attributes": {}},
        {"entity_id": "binary_sensor.ioniq_9_ev_plugged_in", "state": plugged, "attributes": {"device_class": "plug"}},
        {"entity_id": "sensor.ioniq_9_ev_battery_level", "state": "63", "attributes": {}},
        {"entity_id": "sensor.kitchen_temperature", "state": other, "attributes": {}},
    ]


def test_prefixes_from_the_configured_entities():
    assert cartrace.prefixes(DEFAULTS) == ["garage_growatt_thor_ev_charger", "ioniq_9"]
    assert "my_extra" in cartrace.prefixes({**DEFAULTS, "car_trace_prefixes": "my_extra"})


def test_trace_records_only_changes_of_the_charger_and_the_car(tmp_path):
    tr = cartrace.CarTrace(str(tmp_path))
    pfx = cartrace.prefixes(DEFAULTS)
    assert tr.start(T0, "add-on'en: kia stop_charge (ok)", "charging", "suspended_ev")
    tr.sample(states("suspended_ev", power=0.0), pfx, T0)
    tr.sample(states("suspended_ev", power=0.0, other="y"), pfx, T0 + timedelta(seconds=5))   # unrelated entity
    tr.sample(states("suspended_ev", plugged="off", power=0.0), pfx, T0 + timedelta(seconds=175))
    tr.sample(states("finishing", plugged="off", power=0.0), pfx, T0 + timedelta(seconds=180))
    cur = tr.list()[0]
    assert set(cur["initial"]) == {DEFAULTS["charger_status_entity"], "sensor.garage_growatt_thor_ev_charger_charging_power",
                                   "binary_sensor.ioniq_9_ev_plugged_in", "sensor.ioniq_9_ev_battery_level"}
    assert [(c["s"], c["entity"], c["from"], c["to"]) for c in cur["changes"]] == [
        (175, "binary_sensor.ioniq_9_ev_plugged_in", "on", "off"),
        (180, DEFAULTS["charger_status_entity"], "suspended_ev", "finishing")]
    tr.sample(states("idle", plugged="off", power=0.0), pfx, T0 + timedelta(minutes=10))
    assert not tr.active()
    saved = tr.list()
    assert len(saved) == 1 and saved[0]["ended"] and saved[0]["trigger"].startswith("add-on")


def test_keeps_the_latest_ten(tmp_path):
    tr = cartrace.CarTrace(str(tmp_path))
    for i in range(12):
        t = T0 + timedelta(hours=i)
        tr.start(t, "test")
        tr.sample(states("suspended_ev"), ["ioniq_9"], t)
        tr.sample(states("suspended_ev"), ["ioniq_9"], t + timedelta(minutes=10))
    assert len(os.listdir(tmp_path / "car_traces")) == 10 and len(tr.list()) == 10


def test_a_whole_charge_with_a_short_pause_is_one_trace_ending_10_min_after_the_last_stop(tmp_path):
    tr = cartrace.CarTrace(str(tmp_path))
    pfx = cartrace.prefixes(DEFAULTS)
    assert tr.start(T0, "opladning startet", "preparing", "charging", charging=True)
    tr.sample(states("charging"), pfx, T0, charging=True)
    tr.sample(states("suspended_ev", power=0.0), pfx, T0 + timedelta(hours=1), charging=False)
    tr.sample(states("charging"), pfx, T0 + timedelta(hours=1, minutes=5), charging=True)     # resumed
    tr.sample(states("charging"), pfx, T0 + timedelta(hours=2), charging=True)
    assert tr.active()                                                # 2 h in, still the same trace
    tr.event(T0 + timedelta(hours=2, seconds=20), "kald", "sender kia_uvo.stop_charge switch.ioniq_9_ev_charging")
    tr.sample(states("suspended_ev", power=0.0), pfx, T0 + timedelta(hours=2, seconds=25), charging=False)
    tr.sample(states("suspended_ev", power=0.0), pfx, T0 + timedelta(hours=2, minutes=9), charging=False)
    assert tr.active()
    tr.sample(states("idle", power=0.0), pfx, T0 + timedelta(hours=2, minutes=10, seconds=30), charging=False)
    assert not tr.active()
    t = tr.list()[0]
    kinds = [(c["entity"], c["what"]) for c in t["changes"]]
    assert ("add-on", "kald") in kinds
    i_call = kinds.index(("add-on", "kald"))
    assert t["changes"][i_call + 1]["to"] == "suspended_ev"           # the call comes before the stop


def test_noise_is_left_out_and_json_attributes_are_diffed_by_field():
    assert cartrace.attr_changes("last_message_action", "Heartbeat", "CallResult") == []
    assert cartrace.attr_changes("last_message_action", "CallResult", "StatusNotification") == [
        ("last_message_action", "CallResult", "StatusNotification")]
    a = {"Green": {"ChargingInformation": {"ConnectorFastening": {"State": 1}, "SequenceDetails": 8}},
         "Electronics": {"ChargingConnector": {"LockState": 2}}}
    b = {"Green": {"ChargingInformation": {"ConnectorFastening": {"State": 1}, "SequenceDetails": 12}},
         "Electronics": {"ChargingConnector": {"LockState": 1}}}
    got = cartrace.attr_changes("vehicle_data", a, b)
    assert ("vehicle_data.Electronics.ChargingConnector.LockState", 2, 1) in got
    assert ("vehicle_data.Green.ChargingInformation.SequenceDetails", 8, 12) in got and len(got) == 2
    view = cartrace._view({"state": "x", "attributes": {"last_message_at": "1", "last_heartbeat_at": "2", "a": 1}})
    assert view["attributes"] == {"a": 1}
