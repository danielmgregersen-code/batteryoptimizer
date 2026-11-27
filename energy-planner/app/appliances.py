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
# The heat pump dryer (measured on the shared meter, Nov 2026): 350–850 W, swinging 350 <-> 550 W
# every 5–10 minutes, rising over its first 30 minutes, hardly ever under 200 W, for 1–4.5 hours.
# The washing machine: a heating spike of 1–2 kW for 5–20 minutes, then a very steady 45–90 W for
# 2–3 hours and a spin of 200–450 W at the end.
DRYER_ON_W = 200.0       # the dryer's 15-minute median is at least this ...
DRYER_MIN_SLOTS = 6      # ... for at least 30 minutes (the washer is never above it for that long)
DRYER_MAX_GAP = 3        # slots outside the band allowed inside a drying (a washer's spike on top)
SPIKE_OVER_W = 600.0     # a washer's heating: this much above the dryer's own level
WASH_BASE_W = 70.0       # the washer's steady low phase


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


def _dryer_spans(w, hi_w: float) -> list:
    """[(a, b)] slots in which the heat pump dryer runs: its 15-minute median in DRYER_ON_W..hi_w
    for at least DRYER_MIN_SLOTS (gaps of DRYER_MAX_GAP inside), widened at its end by slots in band
    (not at its start: that may be a wash's spin just before)."""
    n = len(w)
    med = [sorted(w[max(0, i - 1):i + 2])[len(w[max(0, i - 1):i + 2]) // 2] for i in range(n)]
    spans = []
    # at least DRYER_ON_W (a washer's heating on top of the drying goes above the band), with at least
    # DRYER_MIN_SLOTS inside the band (a washer's heating alone is above it)
    for a, b in _stretches([m >= DRYER_ON_W for m in med], DRYER_MIN_SLOTS, DRYER_MAX_GAP):
        if sum(1 for m in med[a:b + 1] if DRYER_ON_W <= m <= hi_w) < DRYER_MIN_SLOTS:
            continue
        while a + 2 <= b and not all(x >= DRYER_ON_W for x in w[a:a + 3]):
            a += 1                                    # starts where it holds (not on a wash's spin)
        while b + 1 < n and DRYER_ON_W <= w[b + 1] <= hi_w:
            b += 1
        spans.append((a, b))
    return spans


def _local_level(w, a, b, i, hi_w, half: int = 3) -> float:
    """The dryer's own level around slot i: the median of the slots in band within ±half."""
    vals = [w[k] for k in range(max(a, i - half), min(b, i + half) + 1) if w[k] <= hi_w]
    return sorted(vals)[len(vals) // 2] if vals else 0.0


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
MAX_WASHES = 3           # washes looked for under one drying
HOT_SPIKE_W = 1200       # (kept) a start spike from this used to name a hot programme
HOT_HEAT_KWH = 0.4       # heating at the start from this is a hot (60 °C) programme (measured: 0.68 vs 0.1–0.2)


def wash_only(rows, lo_w: float, hi_w: float, on_w: float = ON_W, gap_min: int = GAP_MIN,
              min_kwh: float = MIN_KWH) -> list:
    """Runs of a shared meter in which the dryer did not run -> [{start, minutes, kwh, spike_w, slots}]."""
    pts = _points(rows)
    if not pts:
        return []
    times, w = _grid(pts)
    spans = _dryer_spans(w, hi_w)
    out = []
    for a, b in _segments(w, on_w, gap_min):
        if any(not (sb < a or sa > b) for sa, sb in spans):
            continue
        slots = [round(x) for x in w[a:b + 1]]
        kwh = sum(slots) / 12000.0
        if kwh < min_kwh:
            continue
        out.append({"start": times[a].isoformat(), "minutes": (b - a + 1) * 5, "kwh": round(kwh, 3),
                    "spike_w": max(slots[:SPIKE_SLOTS]), "heat_kwh": _heat_kwh(slots), "slots": slots})
    return out


def _heat_kwh(slots) -> float:
    """The heating at a wash's start: kWh above 150 W in its first 30 minutes (steadier than the highest
    5-minute mean, which depends on how the heating falls across the slots)."""
    return round(sum(max(0.0, x - 150.0) for x in slots[:SPIKE_SLOTS]) / 12000.0, 3)


def _close(x, y, abs_tol, rel_tol):
    return abs(x - y) <= max(abs_tol, rel_tol * max(x, y))


def wash_programs(runs: list) -> list:
    """Washes that ran alone, grouped by length and start spike -> programmes
    [{name, minutes, kwh, spike_w, runs, template}] (template: median W per 5 minutes from the start)."""
    groups = []
    heat = lambda r: r.get("heat_kwh") if r.get("heat_kwh") is not None else _heat_kwh(r["slots"])  # noqa: E731
    for r in sorted(runs or [], key=lambda r: r["start"]):
        for g in groups:
            if _close(r["minutes"], g["minutes"], 30, 0.2) and _close(heat(r), g["heat"], 0.1, 0.4):
                g["members"].append(r)
                g["minutes"] = _median([m["minutes"] for m in g["members"]])
                g["heat"] = _median([heat(m) for m in g["members"]])
                break
        else:
            groups.append({"minutes": r["minutes"], "heat": heat(r), "members": [r]})
    progs = []
    for g in groups:
        ms = g["members"]
        if len(ms) < PROGRAM_MIN_RUNS:
            continue
        length = int(_median([len(m["slots"]) for m in ms]))
        template = [_median([m["slots"][j] if j < len(m["slots"]) else 0.0 for m in ms]) for j in range(length)]
        # the heating does not fall in the same slot every time, so the median per slot cuts it: the
        # curve is scaled to the washes' median energy
        want = _median([m["kwh"] for m in ms]) * 12000.0
        if sum(template) > 0:
            template = [x * want / sum(template) for x in template]
        progs.append({"minutes": length * 5, "kwh": round(sum(template) / 12000.0, 3),
                      "spike_w": round(_median([m["spike_w"] for m in ms])), "runs": len(ms),
                      "heat_kwh": round(_median([heat(m) for m in ms]), 3),
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
    hot = lambda p: p["heat_kwh"] >= HOT_HEAT_KWH  # noqa: E731
    if len(rest) == 1 and not short and not hot(rest[0]):
        rest[0]["name"] = "Vask"
    else:
        for p in rest:
            p["name"] = "60° eco" if hot(p) else "20°"
    seen = {}
    for p in sorted(progs, key=lambda p: p["minutes"]):         # two alike: "20°", "20° (2)"
        seen[p["name"]] = seen.get(p["name"], 0) + 1
        if seen[p["name"]] > 1:
            p["name"] += f" ({seen[p['name']]})"
    return sorted(progs, key=lambda p: p["minutes"])


def _pick_programme(programs, spike_excess, minutes_seen):
    """The learned programme whose start spike (and length, when its end is seen) fits best."""
    best = None
    for p in programs:
        d = abs(spike_excess - p["spike_w"]) / max(300.0, 0.3 * p["spike_w"])
        if minutes_seen:
            d += abs(minutes_seen - p["minutes"]) / max(20.0, 0.2 * p["minutes"])
        if best is None or d < best[0]:
            best = (d, p)
    return best[1] if best and best[0] <= 2.0 else None


def split_series(pts, lo_w: float, hi_w: float, programs: list = None, labels: list = None):
    """One meter for a washing machine and a heat pump dryer -> (washer [(t, W)], dryer [(t, W)]),
    every 5 minutes split so the two add up to the measurement, also while both run.

    The dryer runs where the 15-minute median stays in DRYER_ON_W..hi_w for 30 minutes or more
    (_dryer_spans); outside, everything is the washer. Inside a drying, a washer is there when it
    starts with its heating spike (SPIKE_OVER_W above the dryer's own level) or was already running
    just before; its share is its learned programme's curve placed at the spike (the spike itself
    is what stands above the dryer), or without programmes the spike plus WASH_BASE_W until the
    wash ends (its tail after the drying) or the drying does. The dryer is the rest. lo_w is not
    used (kept for the callers). labels receives (start time, programme) for recognised washes."""
    if not pts:
        return [], []
    times, w = _grid(pts)
    n = len(w)
    washer = list(w)                                  # outside a drying: all washer
    for a, b in _dryer_spans(w, hi_w):
        seg = next(((sa, sb) for sa, sb in _segments(w) if sa <= a <= sb), (a, b))
        level = [_local_level(w, a, b, i, hi_w) for i in range(n)]
        spikes = [i for i in range(a, b + 1) if w[i] > level[i] + SPIKE_OVER_W]
        pre = w[seg[0]:a]
        # a wash started before the drying goes on into it, unless it already spun (it ended)
        before = (seg[0] < a and sum(pre) / 12000.0 >= 0.02
                  and not (len(pre) >= 6 and any(150 <= x <= 600 for x in pre[-4:])))
        pre_spike = None
        if before and not spikes and pre and max(pre) > SPIKE_OVER_W:
            pre_spike = seg[0] + max(range(len(pre)), key=lambda k: pre[k])
        tail_end = seg[1] if seg[1] > b else None                      # the wash goes on after the drying
        for i in range(a, b + 1):
            washer[i] = 0.0
        if not spikes and not before:
            continue                                  # only the dryer
        s0 = spikes[0] if spikes else a
        while s0 - 1 >= a and w[s0 - 1] - level[s0 - 1] > 100 and (s0 - 1) not in spikes:
            s0 -= 1                                   # the heating's ramp
        start = seg[0] if before else s0
        prog = None
        if programs and (spikes or pre_spike is not None):
            excess = max(w[i] - level[i] for i in spikes) if spikes else w[pre_spike]
            seen = (tail_end - start + 1) * 5 if tail_end is not None else None
            prog = _pick_programme(programs, excess, seen)
        if prog:
            t = prog["template"]
            js = max(range(min(SPIKE_SLOTS, len(t))), key=lambda k: t[k])
            off = (spikes[0] if spikes else pre_spike) - js
            end = min(b, off + len(t) - 1) if tail_end is None else b
            for i in range(max(a, start), end + 1):
                j = i - off
                if i in spikes:
                    washer[i] = max(0.0, w[i] - level[i])
                elif 0 <= j < len(t):
                    # the curve's own heating, where no spike is seen here, counts as its low phase
                    washer[i] = min(w[i], WASH_BASE_W if t[j] > hi_w else float(t[j]))
                else:
                    washer[i] = min(w[i], WASH_BASE_W)
            if labels is not None:
                labels.append((times[max(a, start) if not before else seg[0]], prog["name"]))
        else:
            end = b
            if tail_end is None and not before and spikes:
                end = b                               # the wash's end is not seen: until the drying ends
            for i in range(max(a, start), end + 1):
                washer[i] = max(0.0, w[i] - level[i]) if i in spikes else min(w[i], WASH_BASE_W)
    washer = [min(washer[i], w[i]) for i in range(n)]
    return [(times[i], washer[i]) for i in range(n)], [(times[i], w[i] - washer[i]) for i in range(n)]


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
        # per hour from the start: the mean energy of the runs still running in that hour (a short run
        # does not pile its whole energy into the first hour), scaled to the median energy
        prof = []
        for i in range(hours):
            vals = [r["profile"][i] for r in runs if len(r["profile"]) > i]
            prof.append(sum(vals) / len(vals) if vals else 0.0)
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
    """(lo, hi) W for the dryer. lo is fixed (DRYER_ON_W, measured): appliance_dryer_min_w is no
    longer used (a value set for the earlier "steady floor" made the dryer invisible)."""
    return DRYER_ON_W, float(opts.get("appliance_dryer_max_w") or 1500)


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
