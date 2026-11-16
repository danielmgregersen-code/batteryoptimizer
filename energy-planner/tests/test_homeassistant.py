"""The HA client against tiny local fakes of the REST and WebSocket APIs."""
import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

from homeassistant import HomeAssistant

UTC = timezone.utc


class _Rest(BaseHTTPRequestHandler):
    posted = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.endswith("sensor.exists"):
            body = json.dumps({"state": "73", "attributes": {"unit": "%"}}).encode()
            self.send_response(200)
        elif self.path.endswith("sensor.text"):
            body = json.dumps({"state": "unavailable", "attributes": {}}).encode()
            self.send_response(200)
        else:
            body = b"{}"
            self.send_response(404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers["Content-Length"])
        _Rest.posted.append((self.path, self.headers["Authorization"], json.loads(self.rfile.read(n))))
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")


def test_rest_state_number_and_set_state():
    srv = HTTPServer(("127.0.0.1", 0), _Rest)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    ha = HomeAssistant(token="tok", base_url=f"http://127.0.0.1:{srv.server_port}/api")
    assert ha.number("sensor.exists") == 73.0
    assert ha.number("sensor.missing") is None          # 404 -> None
    assert ha.number("sensor.text") is None             # "unavailable" -> None
    assert ha.attributes("sensor.exists") == {"unit": "%"}
    ha.set_state("sensor.energy_planner_mode", "hold", {"dry_run": True})
    path, auth, body = _Rest.posted[-1]
    assert path == "/api/states/sensor.energy_planner_mode" and auth == "Bearer tok"
    assert body == {"state": "hold", "attributes": {"dry_run": True}}
    srv.shutdown()


def test_websocket_statistics_protocol():
    from websockets.sync.server import serve

    seen = {}

    def handler(ws):
        ws.send(json.dumps({"type": "auth_required"}))
        seen["auth"] = json.loads(ws.recv())
        ws.send(json.dumps({"type": "auth_ok"}))
        seen["cmd"] = json.loads(ws.recv())
        ws.send(json.dumps({"id": 99, "type": "result", "success": True, "result": {}}))   # unrelated message
        ws.send(json.dumps({"id": 1, "type": "result", "success": True,
                            "result": {"sensor.x": [{"start": 1763114400000, "mean": 812.5}]}}))

    with serve(handler, "127.0.0.1", 0) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.socket.getsockname()[1]
        ha = HomeAssistant(token="tok", ws_url=f"ws://127.0.0.1:{port}")
        res = ha.statistics(["sensor.x"], datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC))
        server.shutdown()
    assert seen["auth"] == {"type": "auth", "access_token": "tok"}
    assert seen["cmd"]["type"] == "recorder/statistics_during_period"
    assert seen["cmd"]["statistic_ids"] == ["sensor.x"] and seen["cmd"]["period"] == "hour"
    assert seen["cmd"]["types"] == ["mean", "change", "max"] and seen["cmd"]["start_time"].startswith("2026-10-01")
    assert seen["cmd"]["units"] == {"power": "W", "energy": "kWh"}
    assert res["sensor.x"][0]["mean"] == 812.5


def test_web_app_smoke():
    from fastapi.testclient import TestClient

    import main

    client = TestClient(main.app)           # no lifespan: the background loop is not started
    assert "Energy Planner" in client.get("/").text
    assert client.get("/api/plan").status_code == 503
    assert client.get("/api/decisions").json() == [] or isinstance(client.get("/api/decisions").json(), list)
    page = client.get("/").text
    assert "{{VERSION}}" not in page and "static/app.js?v=" in page   # relative URLs: works under ingress
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200
    ev = client.get("/api/evaluation?days=7").json()
    assert set(ev) >= {"days", "summary", "timeline", "decisions"}
    import main as m

    class _NoStats:
        def statistics(self, *a, **k):
            raise RuntimeError("no websocket")
    m.planner.ha = _NoStats()
    m._economy_cache.clear()
    assert client.get("/api/economy?days=30").status_code == 503


def test_car_extra_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import main
    from planner import Planner
    from settings import DEFAULTS
    from test_planner import car_ha

    ha = car_ha()
    ha.numbers[DEFAULTS["car_soc_entity"]] = 45.0
    p = Planner(ha, log_path=str(tmp_path / "d.jsonl"), car_log_path=str(tmp_path / "c.jsonl"))
    monkeypatch.setattr(main, "planner", p)
    client = TestClient(main.app)
    assert client.post("/api/car/extra", json={}).status_code == 400
    assert client.post("/api/car/extra", json={"target": 30}).status_code == 400      # below the SoC
    r = client.post("/api/car/extra", json={"pct": 20}).json()
    assert r["order"]["target_pct"] == 65 and r["car"]["extra"]["status"] == "planned"
    r = client.delete("/api/car/extra").json()
    assert r["order"] is None and not r["car"]["extra"]["set"]


def test_websocket_call_service_returns_has_error_message():
    from websockets.sync.server import serve

    from homeassistant import ServiceCallError, ServiceNotAllowed

    def handler(ws):
        ws.send(json.dumps({"type": "auth_required"}))
        json.loads(ws.recv())
        ws.send(json.dumps({"type": "auth_ok"}))
        cmd = json.loads(ws.recv())
        if cmd["service"] == "stop_charge":
            ws.send(json.dumps({"id": 1, "type": "result", "success": False,
                                "error": {"code": "home_assistant_error",
                                          "message": "Failed to stop charge: PIN verification failed"}}))
        else:
            ws.send(json.dumps({"id": 1, "type": "result", "success": True, "result": {"context": {}}}))

    with serve(handler, "127.0.0.1", 0) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.socket.getsockname()[1]
        ha = HomeAssistant(token="tok", ws_url=f"ws://127.0.0.1:{port}")
        assert ha.call_service_ws("kia_uvo", "start_charge", {"device_id": "x"}) == {"context": {}}
        try:
            ha.call_service_ws("kia_uvo", "stop_charge", {"device_id": "x"})
            raise AssertionError("expected ServiceCallError")
        except ServiceCallError as e:
            assert "PIN verification failed" in str(e) and e.code == "home_assistant_error"
        try:
            ha.call_service_ws("switch", "turn_on", {})
            raise AssertionError("expected ServiceNotAllowed")
        except ServiceNotAllowed:
            pass
        server.shutdown()


def test_solar_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import main
    from test_planner import _solar_planner

    p, _ = _solar_planner(tmp_path)
    p.run_once()
    monkeypatch.setattr(main, "planner", p)
    main._solar_cache.clear()
    client = TestClient(main.app)
    assert client.get("/api/solar?day=nope").status_code == 400
    r = client.get("/api/solar?day=2026-11-13").json()
    assert r["day"] == "2026-11-13" and len(r["hours"]) == 24
    assert len(client.get("/api/solar/days?days=5").json()) == 5
