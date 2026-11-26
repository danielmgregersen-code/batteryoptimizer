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


def _points(rows) -> list:
    """5-minute statistics rows ({start, mean W}) -> sorted [(t, W)]."""
    pts = []
    for r in rows or []:
        try:
            pts.append((_ts(r["start"]), max(0.0, float(r.get("mean") or 0.0))))
        except (KeyError, TypeError, ValueError):
            continue
    pts.sort()
    return pts


def _run_slots(pts, on_w: float = ON_W, gap_min: int = GAP_MIN) -> list:
    """[(t, W)] -> runs as lists of the slots at or above on_w, pauses up to gap_min inside a run."""
    runs, cur, last_on = [], None, None
    for t, w in pts:
        if w >= on_w:
            if cur is None or (t - last_on) > timedelta(minutes=gap_min + 5):
                cur = []
                runs.append(cur)
            cur.append((t, w))
            last_on = t
    return runs


def _run_from_slots(slots) -> dict:
    """A run's slots -> {start, minutes, kwh, mean_w, profile (kWh per hour from the start)}."""
    first = slots[0][0]
    profile, kwh = [], 0.0
    for t, w in slots:
        k = w / 1000.0 / 12.0
        h = int((t - first).total_seconds() // 3600)
        while len(profile) <= h:
            profile.append(0.0)
        profile[h] += k
        kwh += k
    return {"start": first.isoformat(), "minutes": int((slots[-1][0] - first).total_seconds() / 60) + 5,
            "kwh": round(kwh, 3), "mean_w": round(kwh / (len(slots) * 5 / 60.0) * 1000.0),
            "profile": [round(x, 3) for x in profile]}


def _runs(pts, on_w, gap_min, min_kwh) -> list:
    out = []
    for slots in _run_slots(pts, on_w, gap_min):
        if sum(w for _, w in slots) / 12000.0 >= min_kwh:
            out.append(_run_from_slots(slots))
    return out


def split_runs(rows, on_w: float = ON_W, gap_min: int = GAP_MIN, min_kwh: float = MIN_KWH) -> list:
    """5-minute statistics rows ({start, mean W}) -> runs [{start, minutes, kwh, mean_w, profile}],
    profile = kWh per hour from the start."""
    return _runs(_points(rows), on_w, gap_min, min_kwh)


# ---- one meter, a washing machine and a heat pump dryer (0.34.0) ------------------------------
BASE_HALF = 2            # the floor is the minimum over ±2 slots (25 minutes)
DRYER_MIN_SLOTS = 8      # the dryer runs when the floor stays in its band for 40 minutes
RESID_MIN_W = 80.0       # a rest under this inside a dryer stretch is the dryer's own variation
TAIL_SLOTS = 3           # the dryer's cool-down after its stretch (low power, no washer)


def _stretches(flags, min_len: int, max_gap: int = 1) -> list:
    """[(a, b)] index ranges where flags are true, gaps of at most max_gap false slots inside."""
    out, a, last = [], None, None
    for i, f in enumerate(flags):
        if f:
            if a is None or i - last - 1 > max_gap:
                if a is not None and last - a + 1 >= min_len:
                    out.append((a, last))
                a = i
            last = i
    if a is not None and last - a + 1 >= min_len:
        out.append((a, last))
    return out


def split_series(pts, lo_w: float, hi_w: float):
    """One meter for a washing machine and a heat pump dryer -> (washer [(t, W)], dryer [(t, W)]),
    every 5 minutes split so the two add up to the measurement, also while both run.

    The heat pump dryer is a steady floor of lo_w..hi_w for hours; the washing machine (20 °C or a
    60 °C eco programme) uses little and comes and goes (drum on and off, short spins, pauses). So
    the minimum over ±BASE_HALF slots is the dryer on its own; where it stays in the band for at
    least DRYER_MIN_SLOTS the dryer runs at the median of that floor, min(W, L), and the washer is the
    rest."""
    if not pts:
        return [], []
    t0, step = pts[0][0], timedelta(minutes=5)
    n = int((pts[-1][0] - t0) / step) + 1
    times = [t0 + i * step for i in range(n)]
    w = [0.0] * n
    for t, v in pts:
        i = int(round((t - t0) / step))
        if 0 <= i < n:
            w[i] = v
    base = [min(w[max(0, i - BASE_HALF):i + BASE_HALF + 1]) for i in range(n)]
    stretches = _stretches([lo_w <= b <= hi_w for b in base], DRYER_MIN_SLOTS)
    dryer = [0.0] * n
    for a, b in stretches:
        level = sorted(base[a:b + 1])[(b - a) // 2]
        # the window's edges pull the floor down at the stretch's ends: those slots too, if high enough
        near = max(lo_w, 0.8 * level)              # close to the dryer's level (not a washer's spin)
        for _ in range(BASE_HALF):
            if a > 0 and near <= w[a - 1] <= hi_w + RESID_MIN_W and dryer[a - 1] == 0:
                a -= 1
            else:
                break
        end = b
        for _ in range(BASE_HALF):
            if end + 1 < n and w[end + 1] >= near:
                end += 1
            else:
                break
        for i in range(a, end + 1):
            d = min(w[i], level)
            if w[i] - d < RESID_MIN_W:
                d = w[i]
            dryer[i] = d
        if w[end] - dryer[end] < 1e-9:              # no washer at the end: the dryer's cool-down
            for i in range(end + 1, min(n, end + 1 + TAIL_SLOTS)):
                if 0 < w[i] < lo_w:
                    dryer[i] = w[i]
                else:
                    break
    washer = [(times[i], w[i] - dryer[i]) for i in range(n)]
    return washer, [(times[i], dryer[i]) for i in range(n)]


def split_shared(rows, lo_w: float, hi_w: float, on_w: float = ON_W, gap_min: int = GAP_MIN,
                 min_kwh: float = MIN_KWH) -> list:
    """5-minute rows of a meter shared by a washing machine (0) and a heat pump dryer (1) ->
    [(0 | 1, run)] sorted by start."""
    washer, dryer = split_series(_points(rows), lo_w, hi_w)
    out = [(0, r) for r in _runs(washer, on_w, gap_min, min_kwh)] + [(1, r) for r in _runs(dryer, on_w, gap_min, min_kwh)]
    return sorted(out, key=lambda x: x[1]["start"])


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
