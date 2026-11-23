"""Plain-language (Danish) summary of a plan: what happens now and what comes next.

Used both by the panel ("Kommende") and by the sensor attributes for dashboards, so the two
always say the same thing.
"""
from datetime import datetime, timedelta

MODE_LABEL = {
    "grid_charge": "Lader fra nettet",
    "solar_charge": "Lader fra sol",
    "discharge": "Aflader til huset",
    "hold": "Holder batteriet",
}
CAR_ACTION_LABEL = {"charge": "Lader", "pause": "Pause", "idle": "Venter", "none": "Ikke tilsluttet"}
WEEKDAY = ["man.", "tirs.", "ons.", "tors.", "fre.", "lør.", "søn."]
MAX_ITEMS = 9


def num(v, d=1) -> str:
    """Danish decimal comma."""
    if v is None:
        return "–"
    return f"{v:.{d}f}".replace(".", ",")


def rel_day(day, today) -> str:
    diff = (day - today).days
    if diff == 0:
        return ""
    if diff == 1:
        return "i morgen "
    if diff == -1:
        return "i går "
    return WEEKDAY[day.weekday()] + " "


def _span(rows, a, b, today) -> str:
    ta = datetime.fromisoformat(rows[a]["t"])
    tb = datetime.fromisoformat(rows[b]["t"]) + timedelta(hours=1)
    text = f"{rel_day(ta.date(), today)}{ta:%H:%M}–{tb:%H:%M}"
    last_hour = datetime.fromisoformat(rows[b]["t"])
    if last_hour.date() != ta.date():
        text += " " + rel_day(last_hour.date(), today).strip()
    return text.strip()


def _segments(rows, key):
    segs = []
    for i, r in enumerate(rows):
        k = key(r)
        if segs and segs[-1][0] == k:
            segs[-1][2] = i
        else:
            segs.append([k, i, i])
    return segs


def car_in(r) -> float:
    return (r.get("car_grid") or 0.0) + (r.get("car_pv") or 0.0) + (r.get("car_bat") or 0.0)


def mode_detail(sp: dict) -> str:
    m = sp["mode"]
    if m == "grid_charge":
        return f"til {sp['max_soc_pct']} % med {num(sp['charge_kw'])} kW"
    if m == "solar_charge":
        return f"til {sp['max_soc_pct']} %" + (f", ladehastighed {sp['charge_rate_pct']} %" if sp["charge_rate_pct"] < 100 else "")
    if m == "discharge":
        return f"{num(sp['discharge_kw'])} kW, grænse {sp['min_soc_pct']} %"
    if sp.get("export_hint") == "export":
        return "solen sælges, batteriet gemmes"
    if sp.get("hold_via") == "load_first":
        return f"batteriet gemmes (grænse {sp['min_soc_pct']} %), overskudssol lagres"
    return "batteriet gemmes til senere"


def upcoming(rows: list, car_connected: bool, today, car_label: str = "Lader bilen") -> list:
    """Segments of equal mode (and car charging windows), in time order."""
    def total(a, b, f):
        return sum(f(r) for r in rows[a:b + 1])

    def wavg(a, b, f, w):
        W = total(a, b, w)
        return total(a, b, lambda r: f(r) * w(r)) / W if W > 1e-9 else None

    items = []
    for mode, a, b in _segments(rows, lambda r: r["mode"]):
        end = rows[b]
        if mode == "grid_charge":
            text = (f"til {num(end['soc_pct'], 0)} % · {num(total(a, b, lambda r: r['grid_bat']))} kWh fra nettet "
                    f"à {num(wavg(a, b, lambda r: r['buy'], lambda r: r['grid_bat']), 2)} kr")
        elif mode == "solar_charge":
            text = f"til {num(end['soc_pct'], 0)} % · {num(total(a, b, lambda r: r['pv_bat']))} kWh sol"
        elif mode == "discharge":
            out = total(a, b, lambda r: r["bat_load"] + (r.get("car_bat") or 0.0))
            price = wavg(a, b, lambda r: r["buy"], lambda r: r["bat_load"] + 1e-9)
            text = f"{num(out)} kWh til huset, sparer køb à {num(price, 2)} kr · ned til {num(end['soc_pct'], 0)} %"
        else:
            exp = total(a, b, lambda r: r["pv_exp"])
            text = f"batteriet står på {num(end['soc_pct'], 0)} %" + (f" · sælger {num(exp)} kWh sol" if exp > 0.05 else "")
        items.append({"kind": "battery", "mode": mode, "label": MODE_LABEL.get(mode, mode),
                      "start": rows[a]["t"], "end": _end_iso(rows[b]), "when": _span(rows, a, b, today),
                      "text": text, "_i": a})
    if car_connected:
        for charging, a, b in _segments(rows, lambda r: car_in(r) > 0.05):
            if not charging:
                continue
            kwh = total(a, b, car_in)
            pv = total(a, b, lambda r: r.get("car_pv") or 0.0)
            cost = total(a, b, lambda r: (r.get("car_grid") or 0.0) * r["buy"])
            src = "mest sol" if pv > kwh / 2 else f"à {num(cost / max(kwh, 1e-9), 2)} kr"
            items.append({"kind": "car", "mode": "car", "label": car_label,
                          "start": rows[a]["t"], "end": _end_iso(rows[b]), "when": _span(rows, a, b, today),
                          "text": f"{num(kwh)} kWh, {src} · til {num(rows[b].get('car_soc'), 0)} %",
                          "kwh": round(kwh, 1), "_i": a})
    items.sort(key=lambda x: x["_i"])
    for it in items:
        it.pop("_i")
    return items[:MAX_ITEMS]


def _end_iso(row) -> str:
    return (datetime.fromisoformat(row["t"]) + timedelta(hours=1)).isoformat(timespec="minutes")


def summary_attributes(sp: dict, items: list) -> dict:
    """Dashboard-ready attributes for sensor.energy_planner_mode."""
    label = MODE_LABEL.get(sp["mode"], sp["mode"])
    detail = mode_detail(sp)
    battery = [it for it in items if it["kind"] == "battery"]
    nxt = battery[1] if len(battery) > 1 else None
    text = f"{label}, {detail}"
    if nxt:
        text += f". Derefter: {nxt['label'].lower()} {nxt['when']}"
    return {
        "label": label,
        "detail": detail,
        "summary": text,
        "next_action": f"{nxt['label']} {nxt['when']}" if nxt else None,
        "next_change": nxt["start"] if nxt else None,
        "upcoming": [{k: it[k] for k in ("label", "when", "text", "start", "end")} for it in items[:6]],
        "export_allowed": sp.get("export_allowed"),
        "export_limit_pct": sp.get("export_limit_pct"),
        "export_reason": sp.get("export_reason"),
    }


_REASONS = [
    (r"^not connected \((.*)\)$", lambda m: "Ikke tilsluttet"),
    (r"^car SoC unknown$", lambda m: "Bilens SoC er ukendt"),
    (r"^car at max SoC \((\d+) %\)$", lambda m: f"Bilen er på maks ({m[1]} %)"),
    (r"^critical SoC$", lambda m: "Kritisk lav SoC – lader fuldt"),
    (r"^only ([\d.]+) kWh planned \(under ([\d.]+) kWh\): not starting$",
     lambda m: f"Kun {num(float(m[1]))} kWh planlagt (under {num(float(m[2]), 0)} kWh): starter ikke"),
    (r"^plan: ([\d.]+) kW this hour$", lambda m: f"Planen: {num(float(m[1]))} kW denne time"),
    (r"^solar surplus (-?\d+) W$", lambda m: f"Soloverskud {num(float(m[1]) / 1000)} kW"),
    (r"^short gap, saves only (-?[\d.]+) DKK/kWh: keep charging at (\d+) A$",
     lambda m: f"Kort hul, sparer kun {num(float(m[1]), 2)} kr/kWh: fortsætter på {m[2]} A"),
    (r"^no plan, below minimum SoC$", lambda m: "Ingen plan – under minimum, lader fuldt"),
    (r"^plan: no car charging this hour$", lambda m: "Planen lader ikke bilen denne time"),
    (r"^no plan$", lambda m: "Ingen plan"),
]


def reason_da(reason: str) -> str:
    """The car controller's reason in Danish (the controller logs it in English)."""
    import re
    if not reason:
        return ""
    suffix = ""
    sm = re.search(r" \((cooldown|minimum charge time), (-?\d+) s left\)$", reason)
    if sm:
        reason = reason[:sm.start()]
        what = "cooldown" if sm[1] == "cooldown" else "minimum ladetid"
        suffix = f" ({what}, {max(1, round(int(sm[2]) / 60))} min tilbage)"
    fm = re.search(r" \(fuse limit, highest phase (\d+) A\)$", reason)
    if fm:
        reason = reason[:fm.start()]
        suffix = f" (begrænset af hovedsikringen, højeste fase {fm[1]} A)" + suffix
    for pattern, f in _REASONS:
        m = re.match(pattern, reason)
        if m:
            return f(m) + suffix
    return reason + suffix


# ---- front-page sensors (short Danish texts for dashboard cards) ------------------------------
LEVEL_DA = {"cheap": "billig", "normal": "normal", "dear": "dyr"}
LEVEL_COLOR = {"cheap": "green", "normal": "grey", "dear": "red"}


def when_text(start: datetime, end: datetime, now: datetime) -> str:
    """"i nat kl. 02–05", "i aften kl. 20–23", "i dag kl. 13–16", "i morgen kl. 10–13"."""
    s, e, n = start.astimezone(now.tzinfo), end.astimezone(now.tzinfo), now
    days = (s.date() - n.date()).days
    if days == 0:
        rel = "i nat" if s.hour < 6 else "i aften" if s.hour >= 18 else "i dag"
    elif days == 1 and s.hour < 8:
        rel = "i nat"
    elif days == 1:
        rel = "i morgen"
    else:
        rel = WEEKDAY[s.weekday()]
    return f"{rel} kl. {s:%H}–{e:%H}"


def cheapest_window(rows: list, now: datetime, hours: int = 3):
    """The cheapest run of `hours` consecutive plan hours within the next 24 h ->
    {"start", "end", "avg"} (datetimes) or None."""
    end = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=24)
    rs = [(datetime.fromisoformat(r["t"]), r["buy"]) for r in rows if datetime.fromisoformat(r["t"]) < end]
    best = None
    for i in range(0, len(rs) - hours + 1):
        win = rs[i:i + hours]
        if any((win[k + 1][0] - win[k][0]) != timedelta(hours=1) for k in range(hours - 1)):
            continue
        avg = sum(b for _, b in win) / hours
        if best is None or avg < best["avg"] - 1e-9:
            best = {"start": win[0][0], "end": win[-1][0] + timedelta(hours=1), "avg": avg}
    return best


def price_attributes(rows: list, levels: list, now: datetime) -> dict:
    """sensor.energy_planner_price: the hour's price, its level, and the cheapest 3 hours ahead."""
    r0 = rows[0]
    level = levels[0][1] if levels else "normal"
    win = cheapest_window(rows, now)
    cheapest = f"billigst {when_text(win['start'], win['end'], now)} · {num(win['avg'], 2)} kr" if win else None
    return {
        "level": level, "level_da": LEVEL_DA[level], "sell": round(r0["sell"], 3),
        "cheapest_start": win and win["start"].isoformat(), "cheapest_end": win and win["end"].isoformat(),
        "cheapest_avg": win and round(win["avg"], 3), "cheapest_text": cheapest,
        "text": f"{LEVEL_DA[level].capitalize()} time · {num(r0['buy'], 2)} kr" + (f" · {cheapest}" if cheapest else ""),
        "icon": {"cheap": "mdi:cash-check", "normal": "mdi:cash", "dear": "mdi:cash-remove"}[level],
        "color": LEVEL_COLOR[level],
    }


def car_advice(car_info: dict, preview: dict, decision: dict, now: datetime) -> tuple:
    """sensor.energy_planner_car_advice -> (state, attributes)."""
    soc = ((decision or {}).get("soc_estimate") or {}).get("soc")
    if soc is None:
        soc = (car_info or {}).get("soc_pct")
    base = {"soc": soc, "kwh": None, "window": None, "cost_dkk": None}
    if (car_info or {}).get("connected"):
        action = (decision or {}).get("action")
        limit = car_info.get("max_soc_pct") or 90
        if soc is not None and soc >= float(limit) - 0.5:
            return "full", {**base, "title": "Bilen er ladet", "text": f"{num(soc, 0)} % (loft {num(limit, 0)} %)",
                            "icon": "mdi:car-electric", "color": "green"}
        if action == "charge":
            return "charging", {**base, "title": "Lader nu", "text": (decision.get("reason_da") or "")
                                + (f" · {num(soc, 0)} %" if soc is not None else ""),
                                "icon": "mdi:ev-station", "color": "blue"}
        nxt = (decision or {}).get("next_window")
        return "waiting", {**base, "title": "Venter på billig strøm",
                           "text": (nxt or "planen lader ikke bilen lige nu") + (f" · {num(soc, 0)} %" if soc is not None else ""),
                           "icon": "mdi:car-clock", "color": "grey"}
    if not preview:
        return "unknown", {**base, "title": "Bilen er ikke tilsluttet", "text": "bilens SoC er ukendt",
                           "icon": "mdi:car-off", "color": "grey"}
    w = (preview.get("windows") or [None])[0]
    window = when_text(datetime.fromisoformat(w["start"]), datetime.fromisoformat(w["end"]), now) if w else None
    if preview.get("planned_kwh", 0) > 0.1:
        cost = "på sol" if w and w.get("solar") and preview["cost_dkk"] < 0.5 else f"ca. {num(preview['cost_dkk'], 0)} kr"
        return "plug_in", {**base, "kwh": preview["planned_kwh"], "window": window, "cost_dkk": preview["cost_dkk"],
                           "title": "Sæt bilen til",
                           "text": f"lader {num(preview['planned_kwh'])} kWh {window} · {cost} · når {num(preview['soc_at_deadline'], 0)} %",
                           "icon": "mdi:power-plug", "color": "orange"}
    return "not_needed", {**base, "title": "Ikke nødvendigt at sætte bilen til",
                          "text": f"{num(preview['soc_now'], 0)} % · "
                                  + (f"under {num(preview.get('min_session_kwh'), 0)} kWh at hente" if preview.get("small_dropped")
                                     else "ingen billige timer") + " før kl. "
                                  f"{datetime.fromisoformat(preview['deadline']).astimezone(now.tzinfo):%H}",
                          "icon": "mdi:car-electric", "color": "green"}


def _start_text(a: dict, now: datetime) -> str:
    if a.get("now"):
        return "nu"
    s = datetime.fromisoformat(a["start"]).astimezone(now.tzinfo)
    days = (s.date() - now.date()).days
    rel = "" if days == 0 and s.hour >= 6 else "i nat " if (days == 0 or (days == 1 and s.hour < 8)) \
        else "i morgen " if days == 1 else WEEKDAY[s.weekday()] + " "
    return f"{rel}kl. {s:%H}"


def appliance_advice(items: list, now: datetime) -> tuple:
    """sensor.energy_planner_appliances -> (state, attributes): when each appliance is best started."""
    if not items:
        return 0, {"title": "Ingen maskiner", "text": ""}
    title = " · ".join(f"{a['name']} {_start_text(a, now)}" for a in items)
    lines = []
    for a in items:
        if a.get("now"):
            what = "på sol" if a.get("solar") else "ingen billigere tid de næste 24 timer"
            line = f"{a['name']}: start nu ({what}, ca. {num(a['cost_dkk'], 1)} kr)"
        else:
            line = (f"{a['name']}: {_start_text(a, now)} · {num(a['cost_dkk'], 1)} kr"
                    + (" på sol" if a.get("solar") else "")
                    + (f", sparer {num(a['saving_dkk'], 1)} kr mod nu" if a.get("saving_dkk") is not None else ""))
        lines.append(line)
    return len(items), {"title": title, "text": "\n".join(lines), "lines": lines}
