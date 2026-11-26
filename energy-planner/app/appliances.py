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


def _grid(pts):
    """[(t, W)] -> (times, W) on a 5-minute grid, missing slots 0."""
    t0, step = pts[0][0], timedelta(minutes=5)
    n = int((pts[-1][0] - t0) / step) + 1
    times = [t0 + i * step for i in range(n)]
    w = [0.0] * n
    for t, v in pts:
        i = int(round((t - t0) / step))
        if 0 <= i < n:
            w[i] = v
    return times, w


def _floor_split(w, lo_w: float, hi_w: float):
    """The dryer as the steady floor (0.34.0) -> (dryer per slot, [(a, b)] dryer stretches)."""
    n = len(w)
    base = [min(w[max(0, i - BASE_HALF):i + BASE_HALF + 1]) for i in range(n)]
    stretches = _stretches([lo_w <= b <= hi_w for b in base], DRYER_MIN_SLOTS)
    dryer, spans = [0.0] * n, []
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
                    end = i
                else:
                    break
        spans.append((a, end))
    return dryer, spans


def _segments(w, on_w: float = ON_W, gap_min: int = GAP_MIN) -> list:
    """[(a, b)] index ranges of the meter's runs (slots >= on_w, pauses up to gap_min inside)."""
    out, a, last = [], None, None
    gap = gap_min // 5 + 1
    for i, x in enumerate(w):
        if x >= on_w:
            if a is None or i - last > gap:
                if a is not None:
                    out.append((a, last))
                a = i
            last = i
    if a is not None:
        out.append((a, last))
    return out


# ---- the washer's programmes, learned from washes that ran alone (0.35.0) ----------------------
SPIKE_SLOTS = 6          # the start spike: the highest 5-minute mean in the first 30 minutes
PROGRAM_MIN_RUNS = 2
MAX_PROGRAMS = 4
WASH_ONLY_KEEP = 30
MATCH_MAX_ERR = 0.5      # a programme matches when the squared error is at most this share of its own
MAX_WASHES = 3           # washes looked for under one drying
HOT_SPIKE_W = 1200       # a start spike from this is the heating of a hot (60 °C) programme
MAX_LEFT = 0.25          # the matches must explain at least 75 % of the washer's rest in the run


def wash_only(rows, lo_w: float, hi_w: float, on_w: float = ON_W, gap_min: int = GAP_MIN,
              min_kwh: float = MIN_KWH) -> list:
    """Runs of a shared meter in which the dryer did not run -> [{start, minutes, kwh, spike_w, slots}]."""
    pts = _points(rows)
    if not pts:
        return []
    times, w = _grid(pts)
    _, spans = _floor_split(w, lo_w, hi_w)
    out = []
    for a, b in _segments(w, on_w, gap_min):
        if any(not (sb < a or sa > b) for sa, sb in spans):
            continue
        slots = [round(x) for x in w[a:b + 1]]
        kwh = sum(slots) / 12000.0
        if kwh < min_kwh:
            continue
        out.append({"start": times[a].isoformat(), "minutes": (b - a + 1) * 5, "kwh": round(kwh, 3),
                    "spike_w": max(slots[:SPIKE_SLOTS]), "slots": slots})
    return out


def _close(x, y, abs_tol, rel_tol):
    return abs(x - y) <= max(abs_tol, rel_tol * max(x, y))


def wash_programs(runs: list) -> list:
    """Washes that ran alone, grouped by length and start spike -> programmes
    [{name, minutes, kwh, spike_w, runs, template}] (template: median W per 5 minutes from the start)."""
    groups = []
    for r in sorted(runs or [], key=lambda r: r["start"]):
        for g in groups:
            if _close(r["minutes"], g["minutes"], 20, 0.2) and _close(r["spike_w"], g["spike_w"], 300, 0.3):
                g["members"].append(r)
                g["minutes"] = _median([m["minutes"] for m in g["members"]])
                g["spike_w"] = _median([m["spike_w"] for m in g["members"]])
                break
        else:
            groups.append({"minutes": r["minutes"], "spike_w": r["spike_w"], "members": [r]})
    progs = []
    for g in groups:
        ms = g["members"]
        if len(ms) < PROGRAM_MIN_RUNS:
            continue
        length = int(_median([len(m["slots"]) for m in ms]))
        template = [_median([m["slots"][j] if j < len(m["slots"]) else 0.0 for m in ms]) for j in range(length)]
        progs.append({"minutes": length * 5, "kwh": round(sum(template) / 12000.0, 3),
                      "spike_w": round(_median([m["spike_w"] for m in ms])), "runs": len(ms),
                      "template": [round(x) for x in template], "starts": [m["start"] for m in ms]})
    progs = sorted(progs, key=lambda p: -p["runs"])[:MAX_PROGRAMS]
    # names from what they look like: the short one is the sport programme, the biggest spike the
    # 60 °C eco, the rest 20 °C
    for i, p in enumerate(sorted(progs, key=lambda p: p["minutes"])):
        p["name"] = f"Program {chr(65 + i)}"
    short = [p for p in progs if p["minutes"] <= 60]
    if short:
        min(short, key=lambda p: p["minutes"])["name"] = "Sport"
    rest = [p for p in progs if p["name"] != "Sport"]
    if len(rest) == 1 and not short and rest[0]["spike_w"] < HOT_SPIKE_W:
        rest[0]["name"] = "Vask"
    else:
        for p in rest:
            p["name"] = "60° eco" if p["spike_w"] >= HOT_SPIKE_W else "20°"
    seen = {}
    for p in sorted(progs, key=lambda p: p["minutes"]):         # two alike: "20°", "20° (2)"
        seen[p["name"]] = seen.get(p["name"], 0) + 1
        if seen[p["name"]] > 1:
            p["name"] += f" ({seen[p['name']]})"
    return sorted(progs, key=lambda p: p["minutes"])


def _match(r, programs, a, b):
    """The best (error share, start, programme) for a programme placed in r[a..b] (at most 2 slots past b)."""
    best = None
    for p in programs:
        t = p["template"]
        ref = sum(x * x for x in t) or 1.0
        need = 0.5 * sum(t)
        for s in range(a, b + 3 - len(t) + 1):
            if s < a:
                continue
            seg = [r[s + j] if s + j <= b else 0.0 for j in range(len(t))]
            if sum(seg) < need or not _close(max(seg[:SPIKE_SLOTS]), p["spike_w"], 150, 0.3):
                continue                              # too little energy, or not this programme's start spike
            err = sum((x - y) ** 2 for x, y in zip(seg, t)) / ref
            if best is None or err < best[0]:
                best = (err, s, p)
    return best


def split_series(pts, lo_w: float, hi_w: float, programs: list = None, labels: list = None):
    """One meter for a washing machine and a heat pump dryer -> (washer [(t, W)], dryer [(t, W)]),
    every 5 minutes split so the two add up to the measurement, also while both run.

    The heat pump dryer is a steady floor of lo_w..hi_w for hours; the washing machine (20 °C or a
    60 °C eco programme) uses little and comes and goes (drum on and off, short spins, pauses). So
    the minimum over ±BASE_HALF slots is the dryer on its own; where it stays in the band for at
    least DRYER_MIN_SLOTS the dryer runs at the median of that floor, min(W, L), and the washer is the
    rest. With learned washer programmes (wash_programs), a run with the dryer in it is searched for
    them: a programme that fits the washer's rest is placed there (washer = min(W, programme)) and
    everything else in that run is the dryer. labels: a list that receives (start time, programme)."""
    if not pts:
        return [], []
    times, w = _grid(pts)
    n = len(w)
    dryer, spans = _floor_split(w, lo_w, hi_w)
    if programs:
        for a, b in _segments(w):
            if not any(not (sb < a or sa > b) for sa, sb in spans):
                continue                              # no dryer in this run: all washer
            r = [w[i] - dryer[i] for i in range(n)]
            rest0 = sum(r[a:b + 1])
            washer = [0.0] * n
            found, run_labels = 0, []
            while found < MAX_WASHES:
                m = _match(r, programs, a, b)
                if not m or m[0] > MATCH_MAX_ERR:
                    break
                _, s, p = m
                for j, x in enumerate(p["template"]):
                    if s + j <= b:
                        washer[s + j] = min(w[s + j], x)
                        r[s + j] = 0.0
                found += 1
                run_labels.append((times[s], p["name"]))
            if found and sum(r[a:b + 1]) > MAX_LEFT * rest0:
                found = 0                             # a washer's rest the programmes do not explain: floor split
            if found:                                 # the programmes are the washer, the rest the dryer
                if labels is not None:
                    labels.extend(run_labels)
                for i in range(a, b + 1):
                    dryer[i] = w[i] - washer[i]
    washer = [(times[i], w[i] - dryer[i]) for i in range(n)]
    return washer, [(times[i], dryer[i]) for i in range(n)]


def split_shared(rows, lo_w: float, hi_w: float, on_w: float = ON_W, gap_min: int = GAP_MIN,
                 min_kwh: float = MIN_KWH, programs: list = None) -> list:
    """5-minute rows of a meter shared by a washing machine (0) and a heat pump dryer (1) ->
    [(0 | 1, run)] sorted by start; a wash recognised as a learned programme carries "program"."""
    labels = []
    washer, dryer = split_series(_points(rows), lo_w, hi_w, programs, labels)
    washes = _runs(washer, on_w, gap_min, min_kwh)
    for r in washes:
        t = datetime.fromisoformat(r["start"])
        hit = [name for lt, name in labels if abs((lt - t).total_seconds()) <= 15 * 60]
        if hit:
            r["program"] = hit[0]
    out = [(0, r) for r in washes] + [(1, r) for r in _runs(dryer, on_w, gap_min, min_kwh)]
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


def program(name: str, runs: list, min_runs: int = None) -> dict:
    """The learned program (or the default) -> {minutes, kwh, profile, runs, learned}. A dryer counts
    as learned from its first run (its measured energy beats the default)."""
    if min_runs is None:
        min_runs = 1 if kind_of(name) == "dryer" else MIN_RUNS
    if len(runs or []) >= min_runs:
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
                "runs": len(runs), "learned": True,
                "mean_w": round(_median([r["mean_w"] for r in runs if r.get("mean_w")] or [0]))}
    minutes, kwh, share = DEFAULTS[kind_of(name)]
    return {"minutes": minutes, "kwh": kwh, "profile": [round(kwh * x, 3) for x in share],
            "runs": len(runs or []), "learned": False}


def program_from_template(p: dict) -> dict:
    """A learned washer programme (wash_programs) as a program for best_start: kWh per hour from the
    start out of its 5-minute curve."""
    prof = []
    for j, w in enumerate(p["template"]):
        h = j // 12
        while len(prof) <= h:
            prof.append(0.0)
        prof[h] += w / 12000.0
    return {"minutes": int(p["minutes"]), "kwh": round(sum(prof), 2), "profile": [round(x, 3) for x in prof],
            "runs": p["runs"], "learned": True}


def weighted_program(programs: list, washes: list = None) -> dict:
    """One program for the washing machine: its learned programmes averaged, weighted by how often each
    is used (the washes alone it was learned from, plus washes recognised as it under the dryer;
    each wash counted once). -> a program for best_start plus "mix" [{name, share}]."""
    weights = []
    for p in programs:
        starts = set(p.get("starts") or [])
        starts |= {r["start"] for r in washes or [] if r.get("program") == p["name"]}
        weights.append(max(len(starts), int(p.get("runs") or 0), 1))
    total = float(sum(weights))
    progs = [program_from_template(p) for p in programs]
    hours = max(len(pr["profile"]) for pr in progs)
    profile = [sum(wt * (pr["profile"][h] if h < len(pr["profile"]) else 0.0) for wt, pr in zip(weights, progs)) / total
               for h in range(hours)]
    minutes = sum(wt * pr["minutes"] for wt, pr in zip(weights, progs)) / total
    return {"minutes": int(round(minutes)), "kwh": round(sum(profile), 2), "profile": [round(x, 3) for x in profile],
            "runs": int(total), "learned": True,
            "mix": [{"name": p["name"], "share": round(wt / total, 2), "runs": wt}
                    for p, wt in sorted(zip(programs, weights), key=lambda x: -x[1])]}


DRYER_FLOOR_W = 250.0    # automatic: the heat pump dryer's floor is at least this (a washer alone never holds it)
OLD_DEFAULT_MIN_W = 700  # appliance_dryer_min_w's default before 0.34.0 (a whole run's mean): read as automatic


def dryer_band(opts: dict) -> tuple:
    """(lo, hi) W of the dryer's steady floor from the options; 0 / empty / the old 700 = automatic."""
    lo = float(opts.get("appliance_dryer_min_w") or 0)
    if lo <= 0 or lo == OLD_DEFAULT_MIN_W:
        lo = DRYER_FLOOR_W
    return lo, float(opts.get("appliance_dryer_max_w") or 1500)


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
