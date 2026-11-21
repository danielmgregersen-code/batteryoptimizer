"""When to start the dishwasher, the washing machine and the dryer.

Each appliance has a power meter. Its runs are found in the meter's 5-minute statistics (a run: power
above ON_W, pauses up to GAP_MIN inside it) and kept in /data, so the history grows beyond HA's ~10
days of short-term statistics. The learned program is the median length and energy of the latest runs
and their mean energy per hour from the start (a dishwasher draws most in its first hour, when it
heats). Until MIN_RUNS runs are seen, a default program is used.

One meter can serve two appliances ("Vask+Tørretumbler=sensor.x"): a run whose mean power while
running is at least `appliance_dryer_min_w` is the second (the dryer), otherwise the first.

The cost of a start: per hour of the program, the energy the plan would otherwise sell (pv_exp) costs
the sell price, sun it would curtail is free, the rest costs the buy price. The best start is the
cheapest whole hour (or now) from which the program ends within the next 24 hours of the plan.
"""
import math
from datetime import datetime, timedelta, timezone

ON_W = 15.0
GAP_MIN = 15
MIN_KWH = 0.1
MIN_RUNS = 3
KEEP = 20
SMALL_SAVING_DKK = 0.25

DEFAULTS = {        # (minutes, kWh, share of the energy per hour from the start)
    "dishwasher": (150, 1.0, [0.6, 0.15, 0.25]),
    "washer": (120, 0.8, [0.7, 0.3]),
    "dryer": (120, 2.0, [0.5, 0.5]),
    "other": (120, 1.0, [0.5, 0.5]),
}


def kind_of(name: str) -> str:
    n = name.lower()
    if "opvask" in n or "dish" in n:
        return "dishwasher"
    if "tørre" in n or "tumbl" in n or "dryer" in n:
        return "dryer"
    if "vask" in n or "wash" in n:
        return "washer"
    return "other"


def parse_config(text: str) -> list:
    """"Opvaskemaskine=sensor.a, Vaskemaskine+Tørretumbler=sensor.b" ->
    [{"entity", "names": [low power name, (high power name)]}]."""
    out = []
    for part in str(text or "").split(","):
        if "=" not in part:
            continue
        names, entity = part.split("=", 1)
        names = [n.strip() for n in names.split("+") if n.strip()][:2]
        if names and entity.strip():
            out.append({"entity": entity.strip(), "names": names})
    return out


def _ts(v) -> datetime:
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000.0 if v > 1e11 else v, tz=timezone.utc)
    return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(timezone.utc)


def split_runs(rows, on_w: float = ON_W, gap_min: int = GAP_MIN, min_kwh: float = MIN_KWH) -> list:
    """5-minute statistics rows ({start, mean W}) -> runs [{start, minutes, kwh, mean_w, profile}],
    profile = kWh per hour from the start."""
    pts = []
    for r in rows or []:
        try:
            pts.append((_ts(r["start"]), max(0.0, float(r.get("mean") or 0.0))))
        except (KeyError, TypeError, ValueError):
            continue
    pts.sort()
    runs, cur, last_on = [], None, None

    def close():
        if cur and cur["kwh"] >= min_kwh:
            on = cur.pop("on_slots")
            first = cur.pop("first")
            cur["minutes"] = int((cur.pop("last") - first).total_seconds() / 60) + 5
            cur["mean_w"] = round(cur["kwh"] / (on * 5 / 60.0) * 1000.0)
            cur["kwh"] = round(cur["kwh"], 3)
            cur["profile"] = [round(x, 3) for x in cur["profile"]]
            cur["start"] = first.isoformat()
            runs.append(cur)

    for t, w in pts:
        if w >= on_w:
            if cur is None or (t - last_on) > timedelta(minutes=gap_min + 5):
                close()
                cur = {"first": t, "kwh": 0.0, "on_slots": 0, "profile": []}
            kwh = w / 1000.0 / 12.0
            h = int((t - cur["first"]).total_seconds() // 3600)
            while len(cur["profile"]) <= h:
                cur["profile"].append(0.0)
            cur["profile"][h] += kwh
            cur["kwh"] += kwh
            cur["on_slots"] += 1
            cur["last"] = t
            last_on = t
    close()
    return runs


def classify(run: dict, dryer_min_w: float) -> int:
    """0 = the meter's first appliance, 1 = its high-power second one (the dryer)."""
    return 1 if run.get("mean_w", 0) >= dryer_min_w else 0


def remember(store: dict, name: str, runs: list, keep: int = KEEP) -> dict:
    """Add runs to the store ({name: [runs]}), newest `keep` kept, no duplicates."""
    have = {r["start"]: r for r in store.get(name, [])}
    for r in runs:
        have[r["start"]] = r
    store[name] = sorted(have.values(), key=lambda r: r["start"])[-keep:]
    return store


def _median(vals):
    v = sorted(vals)
    n = len(v)
    return (v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2.0) if n else None


def program(name: str, runs: list) -> dict:
    """The learned program (or the default) -> {minutes, kwh, profile, runs, learned}."""
    if len(runs or []) >= MIN_RUNS:
        minutes = _median([r["minutes"] for r in runs])
        kwh = _median([r["kwh"] for r in runs])
        hours = max(1, math.ceil(minutes / 60.0))
        prof = [0.0] * hours
        for r in runs:
            tot = sum(r["profile"]) or 1.0
            for i, x in enumerate(r["profile"][:hours]):
                prof[i] += x / tot
        s = sum(prof) or 1.0
        return {"minutes": int(minutes), "kwh": round(kwh, 2), "profile": [round(kwh * x / s, 3) for x in prof],
                "runs": len(runs), "learned": True}
    minutes, kwh, share = DEFAULTS[kind_of(name)]
    return {"minutes": minutes, "kwh": kwh, "profile": [round(kwh * x, 3) for x in share],
            "runs": len(runs or []), "learned": False}


def _hour_cost(kwh: float, row: dict) -> float:
    """kWh used in a plan row's hour: sun it would curtail is free, sun it would sell costs the sell
    price, the rest the buy price (in the current, partial hour the row's remaining sun)."""
    k = float(kwh)
    free = min(k, float(row.get("pv_curt") or 0.0))
    k -= free
    sold = min(k, float(row.get("pv_exp") or 0.0))
    k -= sold
    return sold * float(row["sell"]) + k * float(row["buy"])


def cost_at(prog: dict, rows: list, i0: int):
    """Cost of starting at the start of row i0 (row 0: now); None when it runs past the rows."""
    prof = prog["profile"]
    if i0 + len(prof) > len(rows):
        return None, False
    total, solar = 0.0, 0.0
    for j, kwh in enumerate(prof):
        r = rows[i0 + j]
        c = _hour_cost(kwh, r)
        total += c
        if float(r.get("pv_exp") or 0) + float(r.get("pv_curt") or 0) >= 0.5 * kwh:
            solar += kwh
    return total, solar >= 0.5 * sum(prof)


def best_start(prog: dict, rows: list, now: datetime, max_hours: int = 24) -> dict:
    """-> {start, end, cost_dkk, now_cost_dkk, saving_dkk, solar, now} for the cheapest start."""
    rows = rows[:max_hours]
    now_cost, now_solar = cost_at(prog, rows, 0)
    best = None
    for i in range(len(rows)):
        c, solar = cost_at(prog, rows, i)
        if c is None:
            break
        if best is None or c < best[1] - 1e-9:
            best = (i, c, solar)
    if best is None:
        return None
    i, c, solar = best
    if now_cost is not None and now_cost - c < SMALL_SAVING_DKK:
        i, c, solar = 0, now_cost, now_solar
    start = now if i == 0 else datetime.fromisoformat(rows[i]["t"])
    end = start + timedelta(minutes=prog["minutes"])
    return {"start": start.isoformat(timespec="minutes"), "end": end.isoformat(timespec="minutes"),
            "now": i == 0, "cost_dkk": round(c, 2),
            "now_cost_dkk": round(now_cost, 2) if now_cost is not None else None,
            "saving_dkk": round((now_cost - c), 2) if now_cost is not None else None, "solar": bool(solar)}
