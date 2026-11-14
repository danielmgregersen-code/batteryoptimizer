"""Minimal Home Assistant client (REST for states, WebSocket for long-term statistics)."""
import json
import logging
import os
from datetime import datetime

import requests

log = logging.getLogger("energy_planner.ha")

BASE_URL = "http://supervisor/core/api"
WS_URL = "ws://supervisor/core/websocket"


class HomeAssistant:
    def __init__(self, token: str = None, base_url: str = BASE_URL, ws_url: str = WS_URL):
        self.token = token or os.getenv("SUPERVISOR_TOKEN", "")
        self.base_url = base_url
        self.ws_url = ws_url

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

    def statistics(self, statistic_ids: list, start: datetime, end: datetime, period: str = "hour") -> dict:
        """recorder/statistics_during_period over the WebSocket API -> {statistic_id: [rows]}."""
        from websockets.sync.client import connect

        with connect(self.ws_url, open_timeout=15, close_timeout=5) as ws:
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
                "types": ["mean"],
            }))
            while True:
                msg = json.loads(ws.recv())
                if msg.get("id") == 1:
                    if not msg.get("success"):
                        raise RuntimeError(f"statistics query failed: {msg.get('error')}")
                    return msg.get("result") or {}
