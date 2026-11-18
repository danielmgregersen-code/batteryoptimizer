"""Diagnosis: what changes on the charger and the car in the minutes after charging stops.

A watcher reads the charger status every few seconds. When it leaves "charging" (a pause from the
add-on, the car or the app), or when started by hand from the panel, a trace runs for 10 minutes:
every sample reads all HA states, keeps the charger's and the car's entities (by object-id prefix)
and stores only what changed (state and attributes), with the seconds since the start. Saved in
/data/car_traces/, the latest 10 kept.
"""
import json
import os
import threading
from datetime import datetime, timedelta

DURATION = timedelta(minutes=10)
KEEP = 10
IGNORED_ATTRS = {"friendly_name", "icon", "unit_of_measurement", "device_class", "state_class",
                 "entity_picture", "supported_features", "attribution"}


def _object_id(entity_id: str) -> str:
    return entity_id.split(".", 1)[1] if "." in entity_id else entity_id


def common_prefix(entity_ids) -> str:
    ids = [_object_id(e) for e in entity_ids if e]
    if not ids:
        return ""
    p = os.path.commonprefix(ids)
    if len(ids) == 1:
        p = p.rsplit("_", 1)[0] if "_" in p else p     # one entity: drop its last word
    elif "_" in p and not p.endswith("_"):
        p = p[:p.rfind("_") + 1]
    return p.rstrip("_")


def prefixes(opts: dict) -> list:
    """Object-id prefixes of the charger's and the car's entities, plus car_trace_prefixes."""
    charger = common_prefix([opts.get(k) for k in ("charger_status_entity", "charger_current_entity",
                                                   "charger_start_button", "charger_stop_button")])
    car = common_prefix([opts.get(k) for k in ("car_soc_entity", "car_power_entity")])
    extra = [x.strip() for x in str(opts.get("car_trace_prefixes") or "").split(",") if x.strip()]
    return [p for p in dict.fromkeys([charger, car, *extra]) if p and len(p) >= 3]


def _view(state: dict) -> dict:
    attrs = {k: v for k, v in (state.get("attributes") or {}).items() if k not in IGNORED_ATTRS}
    return {"state": state.get("state"), "attributes": attrs}


class CarTrace:
    def __init__(self, data_dir: str):
        self.dir = os.path.join(data_dir, "car_traces")
        self.current = None
        self._last = {}
        self._lock = threading.Lock()

    def active(self) -> bool:
        return self.current is not None

    def start(self, now: datetime, trigger: str, status_before=None, status_after=None):
        with self._lock:
            if self.current is not None:
                return False
            self.current = {"started": now.isoformat(timespec="seconds"), "trigger": trigger,
                            "status_before": status_before, "status_after": status_after,
                            "initial": {}, "changes": [], "ended": None}
            self._last = {}
            return True

    def sample(self, states: list, pfx: list, now: datetime):
        """Record what changed since the last sample; finish after DURATION."""
        with self._lock:
            tr = self.current
            if tr is None:
                return
            t0 = datetime.fromisoformat(tr["started"])
            secs = int((now - t0).total_seconds())
            for st in states or []:
                eid = st.get("entity_id", "")
                if not any(_object_id(eid).startswith(p) for p in pfx):
                    continue
                v = _view(st)
                old = self._last.get(eid)
                if old is None:
                    if not self._last_initialised(tr):
                        tr["initial"][eid] = v
                    else:
                        tr["changes"].append({"s": secs, "entity": eid, "what": "state", "from": None, "to": v["state"]})
                elif old != v:
                    if old["state"] != v["state"]:
                        tr["changes"].append({"s": secs, "entity": eid, "what": "state", "from": old["state"], "to": v["state"]})
                    for k in sorted(set(old["attributes"]) | set(v["attributes"])):
                        a, b = old["attributes"].get(k), v["attributes"].get(k)
                        if a != b:
                            tr["changes"].append({"s": secs, "entity": eid, "what": k, "from": a, "to": b})
                self._last[eid] = v
            tr["initialised"] = True
            if now - t0 >= DURATION:
                tr["ended"] = now.isoformat(timespec="seconds")
                self._save(tr)
                self.current = None
            elif secs % 60 < 6:
                self._save(tr)                 # visible while it runs

    @staticmethod
    def _last_initialised(tr) -> bool:
        return bool(tr.get("initialised"))

    def _save(self, tr: dict):
        try:
            os.makedirs(self.dir, exist_ok=True)
            name = tr["started"].replace(":", "").replace("+", "p") + ".json"
            with open(os.path.join(self.dir, name), "w") as f:
                json.dump({k: v for k, v in tr.items() if k != "initialised"}, f)
            files = sorted(f for f in os.listdir(self.dir) if f.endswith(".json"))
            for old in files[:-KEEP]:
                os.remove(os.path.join(self.dir, old))
        except OSError:
            pass

    def list(self) -> list:
        """The saved traces, newest first (the running one included)."""
        out = []
        try:
            for name in sorted(os.listdir(self.dir), reverse=True):
                if name.endswith(".json"):
                    with open(os.path.join(self.dir, name)) as f:
                        out.append(json.load(f))
        except (OSError, ValueError):
            pass
        with self._lock:
            if self.current is not None and (not out or out[0].get("started") != self.current["started"]):
                out.insert(0, {k: v for k, v in self.current.items() if k != "initialised"})
            elif self.current is not None:
                out[0] = {k: v for k, v in self.current.items() if k != "initialised"}
        return out[:KEEP]
