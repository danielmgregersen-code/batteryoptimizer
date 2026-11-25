"""Diagnosis: what the charger, the car and the add-on did during a charge and the 10 minutes after.

A watcher reads the charger status every few seconds. A trace starts when charging starts (or when
it stops without a trace running, e.g. after a restart, or by hand from the panel) and runs until 10
minutes after the charger last left "charging"; a pause that resumes within those 10 minutes stays in
the same trace (at most MAX_DURATION). Every sample reads all HA states, keeps the charger's and the
car's entities (by object-id prefix) and stores only what changed, with the seconds since the start:
the state, the attributes (noise like the charger's message timestamps left out) and, for big
JSON attributes such as the car's vehicle data, only the fields that changed ("…LockState 2 → 1").
Only the key rows are kept (the add-on's own rows, the charger's and the car's status, plug and
charging states, the connector lock and the car's charging sequence; KEY_STATE / KEY_FIELD) and the
starting picture without attributes, unless car_trace_full is on: a night's charge was 30 000 rows.
The add-on writes its own decisions and calls into the running trace (entity "add-on"), so cause
and effect can be read in order. Saved in /data/car_traces/, the latest 10 kept.
"""
import json
import os
import re
import threading
from datetime import datetime, timedelta

DURATION = timedelta(minutes=10)          # after the charger last left "charging"
MAX_DURATION = timedelta(hours=24)
KEEP = 10
MAX_LEAVES = 60                           # changed fields of one JSON attribute per sample
IGNORED_ATTRS = {"friendly_name", "icon", "unit_of_measurement", "device_class", "state_class",
                 "entity_picture", "supported_features", "attribution",
                 "last_message_at", "last_heartbeat_at"}          # the charger's 5-second heartbeat
QUIET_ACTIONS = {"Heartbeat", "CallResult"}
KEY_STATE = re.compile(r"status|ev_charging$|plugged|lasetilstand")     # entities whose state is kept
KEY_FIELD = re.compile(r"LockState|ConnectorFastening|SequenceDetails")  # attribute fields that are kept


def is_key(entity: str, what: str) -> bool:
    """A row the short trace keeps (the panel's "Kun add-on, status og ladestikket")."""
    return (entity == "add-on" or (what == "state" and bool(KEY_STATE.search(entity)))
            or bool(KEY_FIELD.search(what)))


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


def _leaves(value, path=""):
    """A nested dict / list -> {path: leaf value}."""
    out = {}
    if isinstance(value, dict):
        for k, v in value.items():
            out.update(_leaves(v, f"{path}.{k}" if path else str(k)))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            out.update(_leaves(v, f"{path}[{i}]"))
    else:
        out[path] = value
    return out


def _parse_json(v):
    if isinstance(v, str) and v[:1] in "{[":
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def attr_changes(key: str, a, b) -> list:
    """[(what, from, to)] for one attribute; nested values only by their changed fields."""
    if key == "last_message_action" and a in QUIET_ACTIONS and b in QUIET_ACTIONS:
        return []
    pa, pb = _parse_json(a), _parse_json(b)
    if isinstance(pa, (dict, list)) and isinstance(pb, (dict, list)):
        la, lb = _leaves(pa), _leaves(pb)
        diff = [(f"{key}.{p}", la.get(p), lb.get(p)) for p in sorted(set(la) | set(lb)) if la.get(p) != lb.get(p)]
        if len(diff) > MAX_LEAVES:
            diff = diff[:MAX_LEAVES] + [(f"{key}…", None, f"{len(diff) - MAX_LEAVES} felter mere")]
        return diff
    return [(key, a, b)]


class CarTrace:
    def __init__(self, data_dir: str):
        self.dir = os.path.join(data_dir, "car_traces")
        self.current = None
        self._last = {}
        self._lock = threading.Lock()

    def active(self) -> bool:
        return self.current is not None

    def start(self, now: datetime, trigger: str, status_before=None, status_after=None, charging: bool = False):
        """charging: the charge is running (the trace ends 10 min after it stops); otherwise the 10
        minutes run from now."""
        with self._lock:
            if self.current is not None:
                return False
            self.current = {"started": now.isoformat(timespec="seconds"), "trigger": trigger,
                            "status_before": status_before, "status_after": status_after,
                            "initial": {}, "changes": [], "ended": None,
                            "stopped_at": None if charging else now.isoformat(timespec="seconds")}
            self._last = {}
            return True

    def _secs(self, tr, now) -> int:
        return int((now - datetime.fromisoformat(tr["started"])).total_seconds())

    def event(self, now: datetime, kind: str, text: str, data: dict = None):
        """An add-on decision or call, written into the running trace."""
        with self._lock:
            tr = self.current
            if tr is None:
                return
            e = {"s": self._secs(tr, now), "entity": "add-on", "what": kind, "from": None, "to": text}
            if data:
                e["data"] = data
            tr["changes"].append(e)

    def sample(self, states: list, pfx: list, now: datetime, charging: bool = None, full: bool = False):
        """Record what changed since the last sample; finish 10 min after charging stopped.
        full: every change and the attributes of the starting picture, not only the key rows."""
        with self._lock:
            tr = self.current
            if tr is None:
                return
            t0 = datetime.fromisoformat(tr["started"])
            secs = int((now - t0).total_seconds())
            if charging is True:
                tr["stopped_at"] = None
            elif charging is False and tr.get("stopped_at") is None:
                tr["stopped_at"] = now.isoformat(timespec="seconds")
            def add(eid, what, x, y):
                if full or is_key(eid, what):
                    tr["changes"].append({"s": secs, "entity": eid, "what": what, "from": x, "to": y})

            for st in states or []:
                eid = st.get("entity_id", "")
                if not any(_object_id(eid).startswith(p) for p in pfx):
                    continue
                v = _view(st)
                old = self._last.get(eid)
                if old is None:
                    if not tr.get("initialised"):
                        tr["initial"][eid] = v if full else {"state": v["state"]}
                    else:
                        add(eid, "state", None, v["state"])
                elif old != v:
                    if old["state"] != v["state"]:
                        add(eid, "state", old["state"], v["state"])
                    for k in sorted(set(old["attributes"]) | set(v["attributes"])):
                        a, b = old["attributes"].get(k), v["attributes"].get(k)
                        if a != b:
                            for what, x, y in attr_changes(k, a, b):
                                add(eid, what, x, y)
                self._last[eid] = v
            tr["initialised"] = True
            stopped = tr.get("stopped_at")
            if (stopped and now - datetime.fromisoformat(stopped) >= DURATION) or now - t0 >= MAX_DURATION:
                tr["ended"] = now.isoformat(timespec="seconds")
                self._save(tr)
                self.current = None
            elif secs % 60 < 6:
                self._save(tr)                 # visible while it runs

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
