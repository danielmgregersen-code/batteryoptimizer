from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import writer
from homeassistant import HomeAssistant, ServiceNotAllowed
from settings import DEFAULTS

TZ = ZoneInfo("Europe/Copenhagen")
NOW = datetime(2026, 11, 14, 17, 30, tzinfo=TZ)
OPTS = dict(DEFAULTS)


class RegHA:
    """Fake HA with the Growatt registers: services change the state like the integration does."""

    def __init__(self, regs, confirm=True, fail=None):
        self.regs = dict(regs)
        self.calls = []
        self.confirm, self.fail = confirm, fail or set()

    def text(self, e):
        v = self.regs.get(e)
        return None if v is None else str(v)

    def attributes(self, e):
        if e.startswith("select."):
            return {"options": ["Enabled", "Disabled"]}
        return {"min": 0, "max": 100}

    def state(self, e):
        if e not in self.regs:
            return None
        return {"state": str(self.regs[e]), "attributes": self.attributes(e)}

    def call_service(self, domain, service, data=None, return_response=False):
        e = data["entity_id"]
        if e in self.fail:
            raise RuntimeError("modbus timeout")
        self.calls.append((domain, service, e, data.get("option", data.get("value"))))
        if self.confirm:
            self.regs[e] = data.get("option", data.get("value"))


def regs(bf="Disabled", gc="Disabled", mx=100, mn=10, rate=100):
    return {OPTS["reg_battery_first"]: bf, OPTS["reg_grid_charge"]: gc, OPTS["reg_max_soc"]: mx,
            OPTS["reg_min_soc"]: mn, OPTS["reg_charge_rate"]: rate, OPTS["production_switch_entity"]: "on"}


def sp(mode, battery_first, grid_charge, max_soc, min_soc, rate=100):
    return {"mode": mode, "battery_first": battery_first, "grid_charge": grid_charge,
            "max_soc_pct": max_soc, "min_soc_pct": min_soc, "charge_rate_pct": rate}


def test_only_differing_registers_are_written():
    ha = RegHA(regs(bf="Enabled", mx=80, mn=73))
    res = writer.apply(ha, OPTS, sp("hold", True, False, 80, 73), NOW)
    assert res == [] and ha.calls == []
    res = writer.apply(ha, OPTS, sp("hold", True, False, 81, 73), NOW)
    assert [c[2] for c in ha.calls] == [OPTS["reg_max_soc"]] and res[0]["status"] == "confirmed"


def test_order_into_grid_charging_and_back():
    # discharge (load first, floor 10) -> grid charge to 90 % at 45 %
    ha = RegHA(regs(bf="Disabled", gc="Disabled", mx=100, mn=10))
    writer.apply(ha, OPTS, sp("grid_charge", True, True, 90, 40, rate=45), NOW)
    keys = [c[2] for c in ha.calls]
    assert keys == [OPTS["reg_min_soc"], OPTS["reg_max_soc"], OPTS["reg_charge_rate"],
                    OPTS["reg_battery_first"], OPTS["reg_grid_charge"]]        # switch on last
    # grid charge -> discharge: grid charging off first, then battery first off; max SoC left alone
    ha.calls.clear()
    writer.apply(ha, OPTS, sp("discharge", False, False, None, 10), NOW)
    keys = [c[2] for c in ha.calls]
    assert keys[:2] == [OPTS["reg_grid_charge"], OPTS["reg_battery_first"]]
    assert OPTS["reg_max_soc"] not in keys and ha.regs[OPTS["reg_max_soc"]] == 90


def test_min_never_exceeds_max_while_writing():
    ha = RegHA(regs(bf="Enabled", mx=50, mn=10))
    writer.apply(ha, OPTS, sp("hold", True, False, 70, 70), NOW)
    keys = [c[2] for c in ha.calls]
    assert keys.index(OPTS["reg_max_soc"]) < keys.index(OPTS["reg_min_soc"])


def test_values_are_clipped_to_hard_limits_and_select_spelling_is_used():
    ha = RegHA(regs(bf="Disabled", mn=50))
    writer.apply(ha, OPTS, sp("hold", True, False, 5, 2, rate=3), NOW)
    assert ha.regs[OPTS["reg_min_soc"]] == OPTS["soc_floor_pct"]               # never below the floor
    assert ha.regs[OPTS["reg_max_soc"]] >= ha.regs[OPTS["reg_min_soc"]]
    assert ha.regs[OPTS["reg_charge_rate"]] == 10
    assert ha.regs[OPTS["reg_battery_first"]] == "Enabled"


def test_unconfirmed_and_failed_writes_are_reported():
    ha = RegHA(regs(), confirm=False, fail={OPTS["reg_charge_rate"]})
    res = {r["key"]: r for r in writer.apply(ha, OPTS, sp("grid_charge", True, True, 90, 40, rate=45), NOW)}
    assert res["reg_max_soc"]["status"] == "written"                          # integration not polled yet
    assert res["reg_charge_rate"]["status"] == "error" and "modbus" in res["reg_charge_rate"]["error"]


def test_export_limit_is_never_written_and_not_allowed():
    assert OPTS["reg_export_limit"] not in writer.allowed_entities(OPTS)
    ha = HomeAssistant(token="t", base_url="http://127.0.0.1:9/api")
    ha.set_allowed_entities(writer.allowed_entities(OPTS))
    with pytest.raises(ServiceNotAllowed):
        ha.call_service("number", "set_value", {"entity_id": OPTS["reg_export_limit"], "value": 0})


def test_switch_state():
    ha = RegHA({"input_boolean.x": "on", "input_boolean.y": "off"})
    assert writer.switch_on(ha, "input_boolean.x")
    assert not writer.switch_on(ha, "input_boolean.y")
    assert not writer.switch_on(ha, "input_boolean.missing")
    assert not writer.switch_on(ha, "")
