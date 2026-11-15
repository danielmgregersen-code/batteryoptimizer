"""Production: write the planner's setpoints to the inverter registers (house battery).

Only when the production helper (input_boolean) is on. Every cycle the setpoints are compared with the
registers' current values and only differing values are written. There is deliberately no rate limit
(the user changes registers by hand at short intervals too); the only thing avoided is a call that
would change nothing.

Order matters so the inverter never passes through an unintended state:
  1. switch off what goes off (charge from grid before battery first),
  2. limits (min / max SoC in an order that never makes min > max, then the charge rate),
  3. switch on what goes on (battery first before charge from grid).

Battery first is only the one all-day slot (`reg_battery_first`, Enabled/Disabled); time slots are
never touched. The export limit is not written (it stays with Node-RED).
"""
from datetime import datetime

ON = ("enabled", "enable", "on", "true", "1")
OFF = ("disabled", "disable", "off", "false", "0")


def switch_on(ha, entity_id: str) -> bool:
    """Production helper state; missing or anything but 'on' means dry-run."""
    return bool(entity_id) and str(ha.text(entity_id) or "").lower() == "on"


def _is_on(value) -> bool:
    return str(value or "").strip().lower() in ON


def _option(ha, entity_id: str, on: bool) -> str:
    """The select's own spelling of Enabled / Disabled."""
    opts = (ha.attributes(entity_id) or {}).get("options") or []
    wanted = ON if on else OFF
    for o in opts:
        if str(o).strip().lower() in wanted:
            return o
    return "Enabled" if on else "Disabled"


def _number(ha, entity_id: str):
    s = ha.state(entity_id) if hasattr(ha, "state") else None
    try:
        return float((s or {}).get("state")), (s or {}).get("attributes") or {}
    except (TypeError, ValueError):
        return None, (s or {}).get("attributes") or {}


def desired(sp: dict, opts: dict) -> dict:
    """{register key: value} the setpoints ask for, clipped to hard limits. max SoC is left as it is
    while discharging (load first does not use it)."""
    floor, ceiling = int(opts["soc_floor_pct"]), int(opts["soc_ceiling_pct"])
    min_soc = int(min(max(sp["min_soc_pct"], floor), 100))
    out = {"reg_battery_first": bool(sp["battery_first"]), "reg_grid_charge": bool(sp["grid_charge"]),
           "reg_min_soc": min_soc, "reg_charge_rate": int(min(max(sp["charge_rate_pct"], 10), 100))}
    if sp.get("max_soc_pct") is not None:
        out["reg_max_soc"] = int(min(max(sp["max_soc_pct"], min_soc, floor), ceiling))
    return out


def apply(ha, opts: dict, sp: dict, now: datetime) -> list:
    """Write the differing registers in a safe order. -> [{key, entity, from, to, status, error?}]."""
    want = desired(sp, opts)
    at = now.isoformat(timespec="seconds")
    results = []

    current = {}
    for key in want:
        entity = opts.get(key)
        if not entity:
            continue
        if key in ("reg_battery_first", "reg_grid_charge"):
            current[key] = ha.text(entity)
        else:
            current[key] = _number(ha, entity)[0]

    def differs(key):
        if key not in current:
            return False
        if key in ("reg_battery_first", "reg_grid_charge"):
            return _is_on(current[key]) != want[key]
        return current[key] is None or abs(current[key] - want[key]) >= 0.5

    offs = [k for k in ("reg_grid_charge", "reg_battery_first") if k in want and not want[k]]
    ons = [k for k in ("reg_battery_first", "reg_grid_charge") if k in want and want[k]]
    limits = ["reg_min_soc", "reg_max_soc"]
    cur_max = current.get("reg_max_soc")
    if "reg_max_soc" in want and cur_max is not None and want["reg_min_soc"] > cur_max:
        limits = ["reg_max_soc", "reg_min_soc"]          # raise max first so min never exceeds it
    limits.append("reg_charge_rate")

    for key in offs + limits + ons:
        if key not in want or not differs(key):
            continue
        entity = opts[key]
        res = {"key": key, "entity": entity, "from": current.get(key), "to": want[key], "at": at}
        try:
            if key in ("reg_battery_first", "reg_grid_charge"):
                option = _option(ha, entity, want[key])
                res["to"] = option
                ha.call_service("select", "select_option", {"entity_id": entity, "option": option})
                confirmed = _is_on(ha.text(entity)) == want[key]
            else:
                value, attrs = _number(ha, entity)
                v = want[key]
                if attrs.get("min") is not None:
                    v = max(v, float(attrs["min"]))
                if attrs.get("max") is not None:
                    v = min(v, float(attrs["max"]))
                res["to"] = v
                ha.call_service("number", "set_value", {"entity_id": entity, "value": v})
                back = _number(ha, entity)[0]
                confirmed = back is not None and abs(back - v) < 0.5
            # The integration may only show the new value after its next poll: "written" is not an
            # error, the next cycle compares again.
            res["status"] = "confirmed" if confirmed else "written"
        except Exception as e:
            res["status"] = "error"
            res["error"] = str(e)
        results.append(res)
    return results


def allowed_entities(opts: dict) -> list:
    """Entities the entity services may act on: the written registers and the charger's controls."""
    from settings import WRITE_REGISTER_KEYS
    ids = [opts.get(k) for k in WRITE_REGISTER_KEYS]
    ids += [opts.get("charger_current_entity"), opts.get("charger_start_button"), opts.get("charger_stop_button")]
    return [e for e in ids if e]
