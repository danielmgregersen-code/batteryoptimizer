"""Minimal Home Assistant client (REST for states, WebSocket for long-term statistics)."""
import json
import logging
import os
from datetime import datetime

import requests

log = logging.getLogger("energy_planner.ha")

BASE_URL = "http://supervisor/core/api"
WS_URL = "ws://supervisor/core/websocket"


# The only services this add-on may call; anything else is refused here, whatever the caller asks.
ALLOWED_SERVICES = {
    ("solcast_solar", "set_dampening"),
    ("solcast_solar", "get_dampening"),
    ("solcast_solar", "query_forecast_data"),   # read-only: Solcast's forecast history
    ("kia_uvo", "start_charge"),                # car (production, car switch on)
    ("kia_uvo", "stop_charge"),
}
# Entity services: only on the entities the planner allows (the inverter registers it writes and the
# charger's current / start button). The export limit and everything else stay off limits.
ENTITY_SERVICES = {("select", "select_option"), ("number", "set_value"), ("button", "press"),
                   ("climate", "set_temperature"), ("switch", "turn_off")}


class ServiceNotAllowed(Exception):
    pass


class ServiceCallError(Exception):
    """A service call HA rejected, with HA's message (and error code when given)."""

    def __init__(self, message: str, code: str = None):
        super().__init__(message)
        self.code = code


class HomeAssistant:
    def __init__(self, token: str = None, base_url: str = BASE_URL, ws_url: str = WS_URL):
        self.token = token or os.getenv("SUPERVISOR_TOKEN", "")
        self.base_url = base_url
        self.ws_url = ws_url
        self.allowed_entities = frozenset()

    def set_allowed_entities(self, entity_ids):
        """Entities the entity services (select / number / button) may act on."""
        self.allowed_entities = frozenset(e for e in entity_ids if e)

    @property
    def _headers(self):
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    def state(self, entity_id: str):
        """Full state object, or None if the entity does not exist / the call fails."""
        try:
            r = requests.get(f"{self.base_url}/states/{entity_id}", headers=self._headers, timeout=15)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            log.warning("state(%s) failed: %s", entity_id, e)
            return None

    def all_states(self) -> list:
        """Every entity's state object (GET /api/states); [] if the call fails."""
        try:
            r = requests.get(f"{self.base_url}/states", headers=self._headers, timeout=20)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            log.warning("all_states failed: %s", e)
            return []

    def attributes(self, entity_id: str):
        s = self.state(entity_id)
        return (s or {}).get("attributes")

    def number(self, entity_id: str):
        s = self.state(entity_id)
        if not s:
            return None
        try:
            return float(s["state"])
        except (TypeError, ValueError):
            return None

    def text(self, entity_id: str):
        s = self.state(entity_id)
        return None if not s else s.get("state")

    def set_state(self, entity_id: str, state, attributes: dict = None):
        """Publish a (non-persistent) sensor state. Overwritten on every planning cycle."""
        try:
            r = requests.post(
                f"{self.base_url}/states/{entity_id}",
                headers=self._headers,
                data=json.dumps({"state": state, "attributes": attributes or {}}),
                timeout=15,
            )
            r.raise_for_status()
        except requests.RequestException as e:
            log.warning("set_state(%s) failed: %s", entity_id, e)

    def _check_allowed(self, domain: str, service: str, data: dict):
        if (domain, service) in ENTITY_SERVICES:
            entity = (data or {}).get("entity_id")
            if not entity or entity not in self.allowed_entities:
                raise ServiceNotAllowed(f"{domain}.{service} on {entity} is not allowed")
        elif (domain, service) not in ALLOWED_SERVICES:
            raise ServiceNotAllowed(f"{domain}.{service} is not allowed")

    def call_service_ws(self, domain: str, service: str, data: dict = None):
        """Call an allowlisted service over the WebSocket API. Unlike REST (which only answers
        "500 Internal Server Error"), a failure raises ServiceCallError with HA's own message,
        e.g. "Failed to stop charge: PIN verification failed ..."."""
        from websockets.sync.client import connect

        self._check_allowed(domain, service, data)
        with connect(self.ws_url, open_timeout=15, close_timeout=5) as ws:
            json.loads(ws.recv())  # auth_required
            ws.send(json.dumps({"type": "auth", "access_token": self.token}))
            auth = json.loads(ws.recv())
            if auth.get("type") != "auth_ok":
                raise RuntimeError(f"websocket auth failed: {auth}")
            ws.send(json.dumps({"id": 1, "type": "call_service", "domain": domain, "service": service,
                                "service_data": data or {}}))
            while True:
                msg = json.loads(ws.recv(timeout=90))   # remote car commands can take a while
                if msg.get("id") == 1:
                    if not msg.get("success"):
                        err = msg.get("error") or {}
                        raise ServiceCallError(err.get("message") or str(err), err.get("code"))
                    return msg.get("result")

    def call_service(self, domain: str, service: str, data: dict = None, return_response: bool = False):
        """Call an allowlisted service. Returns the JSON response (with `service_response` when
        return_response is set), or raises."""
        self._check_allowed(domain, service, data)
        url =f"{self.base_url}/services/{domain}/{service}" + ("?return_response" if return_response else "")
        r = requests.post(url, headers=self._headers, data=json.dumps(data or {}), timeout=30)
        r.raise_for_status()
        try:
            return r.json()
        except ValueError:
            return None

    def statistics(self, statistic_ids: list, start: datetime, end: datetime, period: str = "hour") -> dict:
        """recorder/statistics_during_period over the WebSocket API -> {statistic_id: [rows]}."""
        from websockets.sync.client import connect

        # max_size=None: a year of hourly statistics for several entities is several MB.
        with connect(self.ws_url, open_timeout=15, close_timeout=5, max_size=None) as ws:
            json.loads(ws.recv())  # auth_required
            ws.send(json.dumps({"type": "auth", "access_token": self.token}))
            auth = json.loads(ws.recv())
            if auth.get("type") != "auth_ok":
                raise RuntimeError(f"websocket auth failed: {auth}")
            ws.send(json.dumps({
                "id": 1,
                "type": "recorder/statistics_during_period",
                "start_time": start.isoformat(),
                "end_time": end.isoformat(),
                "statistic_ids": statistic_ids,
                "period": period,
                # mean for power sensors, change for energy counters (kWh per hour = average kW),
                # state for a price sensor with state_class total (Strømligning)
                "types": ["mean", "change", "max", "state"],
                # Convert on the HA side, so a sensor reporting kW / Wh lines up with W / kWh.
                "units": {"power": "W", "energy": "kWh"},
            }))
            while True:
                msg = json.loads(ws.recv())
                if msg.get("id") == 1:
                    if not msg.get("success"):
                        raise RuntimeError(f"statistics query failed: {msg.get('error')}")
                    return msg.get("result") or {}
