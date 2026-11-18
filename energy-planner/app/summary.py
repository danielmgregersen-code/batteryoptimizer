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
        return f"{num(sp['discharge_kw'])} kW, gulv {sp['min_soc_pct']} %"
    return "solen sælges, batteriet gemmes" if sp.get("export_hint") == "export" else "batteriet gemmes til senere"


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
