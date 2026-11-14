import asyncio
import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from homeassistant import HomeAssistant
from planner import Planner, PlannerError, evaluate
from settings import load_options

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("energy_planner")

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
    t = threading.Thread(target=_loop, daemon=True, name="planner-loop")
    t.start()
    threading.Thread(target=_car_loop, daemon=True, name="car-loop").start()
    yield
    _stop.set()


app = FastAPI(lifespan=lifespan)


@app.get("/api/plan")
def get_plan():
    if planner.latest is None:
        raise HTTPException(status_code=503, detail=planner.last_error or "no plan yet")
    return planner.latest


@app.get("/api/decisions")
def get_decisions(limit: int = 200):
    return planner.recent_decisions(min(limit, 2000))


@app.get("/api/car")
def get_car(limit: int = 200):
    """Latest car decision and the log of changes (what the controller would have done)."""
    return {"latest": planner.car_latest, "log": planner.recent_car_decisions(min(limit, 2000))}


@app.get("/api/evaluation")
def get_evaluation():
    """Realised grid cost per day from the decision log (what actually happened)."""
    opts = load_options()
    return evaluate(planner.recent_decisions(2000), opts["grid_import_counter"],
                    opts["grid_export_counter"], planner.tz)


@app.post("/api/run")
async def run_now():
    try:
        return await asyncio.to_thread(planner.run_once)
    except PlannerError as e:
        raise HTTPException(status_code=503, detail=str(e))


INDEX_HTML = """<!doctype html>
<html lang="da"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Energy Planner</title>
<style>
 body{font:14px system-ui,sans-serif;margin:16px;color:#222;background:#fff}
 @media(prefers-color-scheme:dark){body{color:#ddd;background:#111}th{background:#222}}
 h1{font-size:18px;margin:0 0 4px} .muted{opacity:.65} .box{margin:10px 0;padding:8px 12px;border:1px solid #8884;border-radius:8px}
 table{border-collapse:collapse;width:100%} th,td{padding:3px 8px;text-align:right;border-bottom:1px solid #8883;white-space:nowrap}
 th:first-child,td:first-child{text-align:left} th{background:#eee;position:sticky;top:0}
 .grid_charge{background:#2a7fff22} .solar_charge{background:#2ecc7122} .discharge{background:#ff9f4322}
 .wrap{overflow-x:auto} button{padding:4px 10px} .fc{font-style:italic;opacity:.7}
</style></head><body>
<h1>Energy Planner <span class="muted">(dry-run — skriver ikke til inverteren)</span></h1>
<div id="status" class="box">Henter plan…</div>
<div id="car" class="box" style="display:none"></div>
<div class="wrap"><table id="t"></table></div>
<p class="muted">Priser i DKK/kWh, energi i kWh pr. time. <i>Kursiv pris</i> = estimeret (samme time som sidste kendte dag). <button onclick="run()">Kør planlægning nu</button></p>
<script>
const f=(v,d=2)=>v==null?'':Number(v).toFixed(d);
async function load(){
  const r=await fetch('api/plan'); const s=document.getElementById('status');
  if(!r.ok){s.textContent='Ingen plan endnu: '+(await r.json()).detail;return;}
  const p=await r.json(), sp=p.setpoints;
  s.innerHTML='<b>Nu: '+sp.mode+'</b> · SoC '+f(p.soc_pct,0)+' % · mål '+(sp.max_soc_pct??'–')+' % · gulv '+sp.min_soc_pct+
    ' % · ladehastighed '+sp.charge_rate_pct+' % · eksport: '+sp.export_hint+
    '<br>Besparelse mod "kun sol til batteri": <b>'+f(p.savings_vs_solar_only_dkk)+' DKK</b> over '+p.horizon_hours+' timer'+
    '<br><span class="muted">Opdateret '+p.updated+' · forbrugsprofil bygget på '+p.profile_hours_of_data+' timer</span>'+
    (p.warnings.length?'<br>⚠ '+p.warnings.join('; '):'')+
    '<br><span class="muted">Nuværende registre: '+Object.entries(p.actual_registers).map(([k,v])=>k.replace('reg_','')+'='+v).join(', ')+'</span>'+
    (()=>{const bad=Object.entries(p.entity_check||{}).filter(([e,v])=>v!='ok');return bad.length?'<br>⚠ Entiteter: '+bad.map(([e,v])=>e+' ('+v+')').join(', '):'';})();
  const car=p.car&&p.car.connected;
  let h='<tr><th>Time</th><th>Køb</th><th>Salg</th><th>Sol</th><th>Forbrug</th><th>Sol→bat</th><th>Net→bat</th><th>Bat→hus</th><th>Eksport</th><th>SoC %</th>'+
    (car?'<th>Sol→bil</th><th>Net→bil</th><th>Bat→bil</th><th>Bil %</th>':'')+'</tr>';
  for(const r of p.plan){
    const c=r.grid_bat>0.05?'grid_charge':r.pv_bat>0.05?'solar_charge':r.bat_load>0.05?'discharge':'';
    const fc=r.forecast_price?' class="fc"':'';
    h+='<tr class="'+c+'"><td>'+r.t.slice(5,16).replace('T',' ')+'</td><td'+fc+'>'+f(r.buy)+'</td><td'+fc+'>'+f(r.sell)+'</td><td>'+f(r.pv)+'</td><td>'+f(r.load)+
       '</td><td>'+f(r.pv_bat)+'</td><td>'+f(r.grid_bat)+'</td><td>'+f(r.bat_load)+'</td><td>'+f(r.pv_exp)+'</td><td>'+f(r.soc_pct,0)+'</td>'+
       (car?'<td>'+f(r.car_pv)+'</td><td>'+f(r.car_grid)+'</td><td>'+f(r.car_bat)+'</td><td>'+f(r.car_soc,0)+'</td>':'')+'</tr>';
  }
  document.getElementById('t').innerHTML=h;
  const cr=await fetch('api/car'); const cb=document.getElementById('car');
  if(cr.ok){const c=(await cr.json()).latest; const pc=p.car||{};
    if(c||pc.enabled){cb.style.display='';
      cb.innerHTML='<b>Bil: '+(c?c.action:'–')+'</b>'+(c&&c.target_amps?' · '+c.target_amps+' A':'')+(c?' · '+c.reason:'')+
        (pc.connected?'<br>SoC '+f(pc.soc_pct,0)+' % · planlagt '+f(pc.planned_kwh,1)+' kWh frem til '+pc.deadline.slice(11,16)+
          (pc.shortfall_kwh>0.05?' · ⚠ mangler '+f(pc.shortfall_kwh,1)+' kWh':''):'<br><span class="muted">Ikke tilsluttet</span>')+
        (c&&c.calls.length?'<br><span class="muted">Ville kalde: '+c.calls.map(x=>x.service+(x.value!=null?'='+x.value:'')).join(', ')+'</span>':'');}}
}
async function run(){await fetch('api/run',{method:'POST'});load();}
load(); setInterval(load,60000);
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML
