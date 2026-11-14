import asyncio
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

import economy
from evaluation import evaluate
from homeassistant import HomeAssistant
from planner import Planner, PlannerError
from settings import counter_ids, load_options

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("energy_planner")

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
VERSION = os.environ.get("ADDON_VERSION") or "dev"

planner = Planner(HomeAssistant())
_stop = threading.Event()


def _loop():
    while not _stop.is_set():
        try:
            snap = planner.run_once()
            log.info("plan: mode=%s soc=%s%% savings_vs_solar_only=%s DKK (dry-run)",
                     snap["mode"], snap["soc_pct"], snap["savings_vs_solar_only_dkk"])
        except PlannerError as e:
            planner.last_error = str(e)
            log.warning("planning skipped: %s", e)
        except Exception as e:  # never let the loop die
            planner.last_error = str(e)
            log.exception("planning failed: %s", e)
        _stop.wait(int(load_options()["interval_minutes"]) * 60)


def _car_loop():
    _stop.wait(20)   # let the first plan land
    while not _stop.is_set():
        opts = load_options()
        try:
            d = planner.car_cycle()
            if d:
                log.debug("car: %s %s (%s)", d["action"], d["target_amps"], d["reason"])
        except Exception as e:  # never let the loop die
            log.exception("car control failed: %s", e)
        _stop.wait(max(30, int(opts.get("car_interval_seconds", 120))))


@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=_loop, daemon=True, name="planner-loop").start()
    threading.Thread(target=_car_loop, daemon=True, name="car-loop").start()
    yield
    _stop.set()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def _since(entries: list, days: int) -> list:
    """Entries of the last `days` days (by their `updated` timestamp)."""
    if not entries:
        return entries
    try:
        last = datetime.fromisoformat(entries[-1]["updated"])
    except (KeyError, ValueError):
        return entries
    cutoff = last - timedelta(days=days)
    out = []
    for e in entries:
        try:
            if datetime.fromisoformat(e["updated"]) >= cutoff:
                out.append(e)
        except (KeyError, ValueError):
            continue
    return out


@app.get("/api/plan")
def get_plan():
    if planner.latest is None:
        raise HTTPException(status_code=503, detail=planner.last_error or "no plan yet")
    return {**planner.latest, "last_error": planner.last_error}


@app.get("/api/decisions")
def get_decisions(limit: int = 200):
    return planner.recent_decisions(min(limit, 4000))


@app.get("/api/car")
def get_car(limit: int = 200):
    """Latest car decision and the log of changes (what the controller would have done)."""
    return {"latest": planner.car_latest, "log": planner.recent_car_decisions(min(limit, 2000))}


@app.get("/api/evaluation")
def get_evaluation(days: int = 14):
    """Realised cost, forecast quality and agreement with Node-RED over the last `days` days."""
    days = max(1, min(days, 60))
    decisions = _since(planner.recent_decisions(4000), days)
    result = evaluate(decisions, counter_ids(load_options()), planner.tz)
    result["decisions"] = decisions[-96:]
    return result


_economy_cache: dict = {}
ECONOMY_CACHE_S = 600
ECONOMY_PERIODS = (1, 7, 30, 90, 365)


@app.get("/api/economy")
async def get_economy(days: int = 30):
    """What the installation cost and earned over the last `days` days (HA long-term statistics)."""
    days = min(ECONOMY_PERIODS, key=lambda p: abs(p - days))
    hit = _economy_cache.get(days)
    if hit and time.monotonic() - hit[0] < ECONOMY_CACHE_S:
        return hit[1]
    try:
        res = await asyncio.to_thread(economy.report, planner.ha, load_options(), planner.tz, days,
                                      datetime.now(timezone.utc), planner.recent_decisions(4000))
    except Exception as e:  # statistics unavailable (no websocket, HA restarting)
        raise HTTPException(status_code=503, detail=f"statistics unavailable: {e}")
    _economy_cache[days] = (time.monotonic(), res)
    return res


@app.post("/api/run")
async def run_now():
    try:
        return await asyncio.to_thread(planner.run_once)
    except PlannerError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
        # Version in the asset URLs so a browser never mixes a new page with a cached old script.
        return f.read().replace("{{VERSION}}", VERSION)
