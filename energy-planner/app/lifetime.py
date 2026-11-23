"""The installation's value over its lifetime, the expected value per year and the payback.

Day by day from the installation date, the best source is used:

  1. measured  - the add-on's own economy (economy.compute: consumption at the buy price minus the
                 net cost) for days with at least MIN_HOURS hours of counters and prices,
  2. old       - the daily change of an older value sensor (lifetime_value_entity),
  3. estimated - the day's solar production x the value per produced kWh of the same calendar month,
                 learned from days with source 1 or 2 *before* `price_change` (the electricity tax
                 was cut sharply in 2026, so later days would understate the early years). Without
                 solar data for the day: the mean value per day of that calendar month (before
                 `price_change`).

Expected per year: the last 365 days. From 90 days after `price_change` on, only days from then on
are used, scaled to a year by the calendar months' weight in the solar production, so the new tax
level drives the forecast.
"""
from datetime import date, datetime, timedelta

MIN_HOURS = 22
MIN_NEW_DAYS = 90


def _d(v) -> date:
    return v if isinstance(v, date) and not isinstance(v, datetime) else date.fromisoformat(str(v)[:10])


def daily_values(start: date, end: date, measured: dict, old: dict, solar: dict, price_change: date) -> dict:
    """-> {"days": [{day, value, source, solar_kwh}], "rates": {month: kr/kWh}, "per_day": {month: kr}}.
    measured / old / solar: {date: value} (measured only for well-covered days)."""
    ref_val, ref_sol, ref_days = {}, {}, {}
    d = start
    while d <= end:
        v = measured.get(d, old.get(d))
        if v is not None and d < price_change:
            m = d.month
            ref_val[m] = ref_val.get(m, 0.0) + v
            ref_days[m] = ref_days.get(m, 0) + 1
            if solar.get(d) is not None:
                ref_sol[m] = ref_sol.get(m, 0.0) + solar[d]
        d += timedelta(days=1)
    rates = {m: ref_val[m] / ref_sol[m] for m in ref_val if ref_sol.get(m, 0) > 1.0}
    all_sol = sum(ref_sol.values())
    overall = sum(ref_val.values()) / all_sol if all_sol > 1.0 else None
    per_day = {m: ref_val[m] / ref_days[m] for m in ref_val}
    overall_day = sum(ref_val.values()) / sum(ref_days.values()) if ref_days else 0.0
    days = []
    d = start
    while d <= end:
        if d in measured:
            v, src = measured[d], "measured"
        elif d in old:
            v, src = old[d], "old"
        else:
            rate = rates.get(d.month, overall)
            if solar.get(d) is not None and rate is not None:
                v = solar[d] * rate
            else:
                v = per_day.get(d.month, overall_day)
            src = "estimated"
        days.append({"day": d.isoformat(), "value": float(v), "source": src, "solar_kwh": solar.get(d)})
        d += timedelta(days=1)
    return {"days": days, "rates": {m: round(r, 3) for m, r in sorted(rates.items())},
            "per_day": {m: round(v, 2) for m, v in sorted(per_day.items())}}


def expected_per_year(days: list, price_change: date, solar_weight: dict = None) -> dict:
    """-> {"dkk", "basis"}: the last 365 days, or the days since price_change scaled to a year."""
    recent = days[-365:]
    new = [x for x in days if _d(x["day"]) >= price_change]
    if len(new) >= MIN_NEW_DAYS and len(new) < 365 and solar_weight:
        # value per day of each calendar month since the change, months without data from the weights
        by_m = {}
        for x in new:
            a = by_m.setdefault(_d(x["day"]).month, [0.0, 0])
            a[0] += x["value"]
            a[1] += 1
        w_have = sum(solar_weight.get(m, 0.0) for m in by_m)
        mean_day = {m: a[0] / a[1] for m, a in by_m.items()}
        if w_have > 0:
            # a year = the months seen at their own rate + the rest scaled by solar weight
            seen = sum(mean_day[m] * _month_days(m) for m in by_m)
            total_w = sum(solar_weight.values())
            year = seen * (total_w / w_have) if w_have < total_w else seen
            return {"dkk": round(year, 0), "basis": f"{len(new)} dage siden {price_change.isoformat()}, skaleret med solproduktionen"}
    return {"dkk": round(sum(x["value"] for x in recent), 0), "basis": f"de seneste {len(recent)} dage"}


def _month_days(m: int) -> int:
    return [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]


def solar_weights(days: list, price_change: date) -> dict:
    """Mean solar kWh per calendar month (before price_change) - how a year is spread over months."""
    acc = {}
    for x in days:
        dd = _d(x["day"])
        if dd < price_change and x.get("solar_kwh") is not None:
            a = acc.setdefault(dd.month, [0.0, 0])
            a[0] += x["solar_kwh"]
            a[1] += 1
    return {m: a[0] / a[1] * _month_days(m) for m, a in acc.items() if a[1]}


def summary(days: list, today_dkk: float, cost: float, life_years: float, install: date, today: date,
            price_change: date, goal_years: float = 10.0) -> dict:
    """goal_years: the payback the installation was bought for; the goal per year is cost / goal_years,
    spread over the calendar months by their share of the solar production."""
    total = sum(x["value"] for x in days) + (today_dkk or 0.0)
    by_src = {}
    for x in days:
        a = by_src.setdefault(x["source"], {"dkk": 0.0, "days": 0})
        a["dkk"] += x["value"]
        a["days"] += 1
    months = {}
    for x in days:
        m = x["day"][:7]
        e = months.setdefault(m, {"month": m, "value": 0.0, "estimated": 0.0, "old": 0.0, "measured": 0.0, "solar_kwh": 0.0})
        e["value"] += x["value"]
        e[x["source"]] += x["value"]
        e["solar_kwh"] += x.get("solar_kwh") or 0.0
    cum, paid_on = 0.0, None
    for x in days:
        cum += x["value"]
        if paid_on is None and cost and cum >= cost:
            paid_on = x["day"]
    exp = expected_per_year(days, price_change, solar_weights(days, price_change))
    per_year = exp["dkk"]
    remaining = max(0.0, (cost or 0.0) - total)
    if cost and paid_on is None and per_year > 0:
        payback = today + timedelta(days=round(remaining / per_year * 365.25))
    else:
        payback = date.fromisoformat(paid_on) if paid_on else None
    end_of_life = date(install.year + int(life_years), install.month, install.day) if life_years else None
    years_left = max(0.0, (end_of_life - today).days / 365.25) if end_of_life else 0.0
    goal_year = (cost / goal_years) if cost and goal_years else per_year
    gl = goals(days, today_dkk, today, goal_per_day(goal_year, solar_weights(days, price_change)), end_of_life)
    gl.update(goal_per_year_dkk=round(goal_year, 0), goal_payback_years=goal_years)
    return {
        "goals": gl,
        "total_dkk": round(total, 0), "today_dkk": round(today_dkk or 0.0, 2),
        "by_source": {k: {"dkk": round(v["dkk"], 0), "days": v["days"]} for k, v in by_src.items()},
        "months": [{k: (round(v, 1) if isinstance(v, float) else v) for k, v in e.items()} for e in months.values()],
        "cost_dkk": cost, "paid_pct": round(100.0 * total / cost, 1) if cost else None,
        "remaining_dkk": round(remaining, 0), "expected_per_year_dkk": per_year, "expected_basis": exp["basis"],
        "payback_date": payback.isoformat() if payback else None, "paid_back": paid_on is not None,
        "payback_years": round((payback - install).days / 365.25, 1) if payback else None,
        "expected_life_dkk": round(total + per_year * years_left, 0), "life_years": life_years,
        "end_of_life": end_of_life.isoformat() if end_of_life else None,
        "installed": install.isoformat(),
    }


# ---- goals: the expected value per day, to compare the actual days against ---------------------
def goal_per_day(per_year: float, weights: dict) -> dict:
    """{calendar month: kr per day}: the expected value per year spread over the months by their share
    of the solar production (a flat year without weights)."""
    total = sum(weights.values()) if weights else 0.0
    if not total:
        return {m: per_year / 365.0 for m in range(1, 13)}
    return {m: per_year * weights.get(m, 0.0) / total / _month_days(m) for m in range(1, 13)}


def goals(days: list, today_dkk: float, today: date, goal_by_month: dict, end_of_life: date = None) -> dict:
    """The recent days with their goal (today included, partial), the totals for today / this week /
    this month / this year against the goal so far and for the whole period, the months of this and
    last year, and the forecast per month to the end of the installation's life."""
    g = lambda d: goal_by_month.get(d.month, 0.0)  # noqa: E731
    recent = [{"day": x["day"], "value": round(x["value"], 2), "goal": round(g(_d(x["day"])), 2), "source": x["source"]}
              for x in days[-400:]]
    recent.append({"day": today.isoformat(), "value": round(today_dkk or 0.0, 2), "goal": round(g(today), 2),
                   "source": "today", "partial": True})
    by_day = {x["day"]: x for x in recent}
    week0 = today - timedelta(days=today.weekday())
    month0 = today.replace(day=1)
    year0 = today.replace(month=1, day=1)

    def period(a: date, b: date) -> dict:
        val = goal_sofar = goal_all = 0.0
        d = a
        while d <= b:
            gd = g(d)
            goal_all += gd
            if d <= today:
                goal_sofar += gd
                val += by_day.get(d.isoformat(), {}).get("value", 0.0)
            d += timedelta(days=1)
        return {"value": round(val, 0), "goal_sofar": round(goal_sofar, 0), "goal": round(goal_all, 0),
                "pct": round(100.0 * val / goal_sofar, 0) if goal_sofar > 0 else None}
    next_month = (month0 + timedelta(days=32)).replace(day=1)
    periods = {"today": period(today, today), "week": period(week0, week0 + timedelta(days=6)),
               "month": period(month0, next_month - timedelta(days=1)),
               "year": period(year0, today.replace(month=12, day=31))}
    months = {}
    for x in days:
        dd = _d(x["day"])
        if dd.year >= today.year - 1:
            e = months.setdefault(x["day"][:7], {"month": x["day"][:7], "value": 0.0, "goal": 0.0})
            e["value"] += x["value"]
            e["goal"] += g(dd)
    e = months.setdefault(today.isoformat()[:7], {"month": today.isoformat()[:7], "value": 0.0, "goal": 0.0})
    e["value"] += today_dkk or 0.0
    e["goal"] += g(today)
    forecast = []
    if end_of_life:
        m = next_month
        rest_this = g(today) * ((next_month - today).days - 1)      # the days after today this month
        forecast.append({"month": today.isoformat()[:7], "value": round(rest_this, 0)})
        while m <= end_of_life:
            forecast.append({"month": m.isoformat()[:7], "value": round(g(m) * _month_days(m.month), 0)})
            m = (m + timedelta(days=32)).replace(day=1)
    return {"days": recent, "periods": periods, "goal_by_month": {k: round(v, 2) for k, v in goal_by_month.items()},
            "months": [{k: (round(v, 0) if isinstance(v, float) else v) for k, v in e.items()} for e in sorted(months.values(), key=lambda x: x["month"])],
            "forecast": forecast}
