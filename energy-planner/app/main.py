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
from planner import LOG_READ, Planner, PlannerError
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
            log.info("plan: mode=%s soc=%s%% savings_vs_solar_only=%s DKK",
                     snap["mode"], snap["soc_pct"], snap["savings_vs_solar_only_dkk"])
        except PlannerError as e:
            planner.last_error = str(e)
            log.warning("planning skipped: %s", e)
        except Exception as e:  # never let the loop die
            planner.last_error = str(e)
            log.exception("planning failed: %s", e)
        _stop.wait(seconds_to_next_cycle(datetime.now(planner.tz), int(load_options()["interval_minutes"])))


def seconds_to_next_cycle(now: datetime, interval_minutes: int) -> float:
    """The interval, but never past the start of the next hour (+20 s): a new hour's setpoints are
    written within half a minute instead of up to an interval late."""
    next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1, seconds=20)
    return max(5.0, min(interval_minutes * 60.0, (next_hour - now).total_seconds()))


def _battery_fast_loop():
    _stop.wait(30)
    while not _stop.is_set():
        try:
            planner.battery_fast_cycle()
        except Exception as e:  # never let the loop die
            log.exception("battery fast check failed: %s", e)
        _stop.wait(60)


def _trace_loop():
    """Charger status every 5 s; a diagnosis trace runs from the start of a charge to 10 min after it stops."""
    _stop.wait(25)
    while not _stop.is_set():
        try:
            planner.trace_tick(manual=_trace_request.is_set())
            _trace_request.clear()
        except Exception as e:  # never let the loop die
            log.exception("car trace failed: %s", e)
        _stop.wait(5)


_trace_request = threading.Event()


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
    threading.Thread(target=_battery_fast_loop, daemon=True, name="battery-fast-loop").start()
    threading.Thread(target=_car_loop, daemon=True, name="car-loop").start()
    threading.Thread(target=_trace_loop, daemon=True, name="car-trace").start()
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
    decisions = _since(planner.recent_decisions(LOG_READ), days)
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
                                      datetime.now(timezone.utc), planner.recent_decisions(LOG_READ))
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


_solar_cache: dict = {}
SOLAR_CACHE_S = 300


async def _cached(key, fn, *args):
    hit = _solar_cache.get(key)
    if hit and time.monotonic() - hit[0] < SOLAR_CACHE_S:
        return hit[1]
    try:
        res = await asyncio.to_thread(fn, *args)
    except Exception as e:  # statistics unavailable (no websocket, HA restarting)
        raise HTTPException(status_code=503, detail=f"statistics unavailable: {e}")
    _solar_cache[key] = (time.monotonic(), res)
    return res


@app.get("/api/solar")
async def get_solar(day: str = None):
    """One day hour by hour: actual, Solcast p10/p50/p90, corrected, and the evening-before forecast."""
    from datetime import date
    try:
        d = date.fromisoformat(day) if day else datetime.now(timezone.utc).astimezone(planner.tz).date()
    except ValueError:
        raise HTTPException(status_code=400, detail="day must be YYYY-MM-DD")
    return await _cached(("day", d.isoformat()), planner.solar_report, d)


@app.get("/api/solar/days")
async def get_solar_days(days: int = 14):
    days = max(1, min(days, 60))
    return await _cached(("days", days), planner.solar_days, days)


async def _replan():
    """Plan again right away so the panel shows the effect of a change at once."""
    try:
        snap = await asyncio.to_thread(planner.run_once)
        return {"car": snap.get("car"), "upcoming": snap.get("upcoming")}
    except PlannerError as e:
        return {"error": str(e)}


@app.get("/api/car/traces")
def get_car_traces():
    """Diagnosis traces: what changed on the charger and the car after charging stopped."""
    return planner.trace.list()


@app.post("/api/car/trace")
def start_car_trace():
    _trace_request.set()
    return {"requested": True}


@app.post("/api/car/extra")
async def order_car_extra(body: dict):
    """Extra charge before the next deadline/trip: {"pct": 20} (on top of the SoC now) or {"target": 80}."""
    kind = "pct" if "pct" in body else "target" if "target" in body else None
    if kind is None:
        raise HTTPException(status_code=400, detail='send {"pct": n} or {"target": n}')
    try:
        order = await asyncio.to_thread(planner.set_car_extra, kind, body[kind])
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"order": order, **(await _replan())}


@app.delete("/api/car/extra")
async def cancel_car_extra():
    planner.clear_car_extra()
    return {"order": None, **(await _replan())}


@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
        # Version in the asset URLs so a browser never mixes a new page with a cached old script.
        return f.read().replace("{{VERSION}}", VERSION)
