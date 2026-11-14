"""Pure parsing of HA sensor attributes into hourly series (all keys are UTC hour starts).

Everything is normalised to UTC so DST changes never produce duplicate or missing
hours; conversion to local time happens only where a local hour-of-day is needed.
"""
from datetime import datetime, timedelta, timezone

UTC = timezone.utc


def parse_ts(value) -> datetime:
    """ISO string / epoch (s or ms) / datetime -> aware datetime in UTC."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        seconds = value / 1000.0 if value > 1e11 else float(value)
        return datetime.fromtimestamp(seconds, tz=UTC)
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.astimezone()  # naive -> assume local time
    return dt.astimezone(UTC)


def hour_floor(dt: datetime) -> datetime:
    return dt.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def parse_prices(attrs: dict) -> dict:
    """Energi Data Service attributes (raw_today / raw_tomorrow) -> {hour_utc: DKK/kWh}.

    Works for hourly entries and for finer ones (15 min): entries are averaged per hour.
    """
    buckets: dict = {}
    for key in ("raw_today", "raw_tomorrow"):
        for entry in attrs.get(key) or []:
            ts = entry.get("hour") or entry.get("start")
            price = entry.get("price")
            if ts is None or price is None:
                continue
            buckets.setdefault(hour_floor(parse_ts(ts)), []).append(float(price))
    return {h: sum(v) / len(v) for h, v in buckets.items()}


def forecast_entries_to_hourly(entries, default_minutes: int = 30) -> dict:
    """Solcast entries (period_start + pv_estimate / 10 / 90 in average kW over the period) ->
    {utc_hour: (p50, p10, p90)} in kWh per hour.

    The period length is read from the spacing of period_start (30 or 60 min); with a single entry
    `default_minutes` is assumed. Entries without a usable period_start or pv_estimate are skipped;
    a missing p10 / p90 falls back to p50.
    """
    rows = []
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        try:
            t = parse_ts(e["period_start"])
            v50 = float(e["pv_estimate"])
        except (KeyError, TypeError, ValueError):
            continue
        v10, v90 = e.get("pv_estimate10"), e.get("pv_estimate90")
        try:
            v10 = float(v10) if v10 is not None else v50
            v90 = float(v90) if v90 is not None else v50
        except (TypeError, ValueError):
            v10 = v90 = v50
        rows.append((t, v50, v10, v90))
    rows.sort(key=lambda r: r[0])
    gaps = [(b[0] - a[0]).total_seconds() / 60.0 for a, b in zip(rows, rows[1:])]
    gaps = [g for g in gaps if g > 0]
    minutes = min(gaps) if gaps else float(default_minutes)
    minutes = min(max(minutes, 5.0), 60.0)
    share = minutes / 60.0
    out: dict = {}
    for t, v50, v10, v90 in rows:
        h = hour_floor(t)
        a = out.get(h, (0.0, 0.0, 0.0))
        out[h] = (a[0] + v50 * share, a[1] + v10 * share, a[2] + v90 * share)
    return out


def parse_solcast(attrs_list: list, p10_weight: float = 0.0, with_p90: bool = False) -> tuple:
    """Solcast forecast sensors -> ({hour: kWh blended}, {hour: kWh p50}, {hour: kWh p10}[, {hour: p90}]).

    `detailedHourly` values are average kW over the hour, i.e. numerically kWh per
    hour (the 24 values sum to the sensor's daily kWh total). `detailedForecast` is
    30-minute data in kW, so each entry counts for half an hour.
    """
    p50: dict = {}
    p10: dict = {}
    p90: dict = {}
    for attrs in attrs_list:
        if not attrs:
            continue
        hourly = attrs.get("detailedHourly")
        if hourly:
            for e in hourly:
                h = hour_floor(parse_ts(e["period_start"]))
                p50[h] = float(e.get("pv_estimate", 0.0))
                p10[h] = float(e.get("pv_estimate10", e.get("pv_estimate", 0.0)))
                p90[h] = float(e.get("pv_estimate90", e.get("pv_estimate", 0.0)))
            continue
        half = attrs.get("detailedForecast")
        if half:
            for h, (v50, v10, v90) in forecast_entries_to_hourly(half, default_minutes=30).items():
                p50[h], p10[h], p90[h] = v50, v10, v90
    w = min(max(p10_weight, 0.0), 1.0)
    blended = {h: (1 - w) * p50[h] + w * p10.get(h, p50[h]) for h in p50}
    if with_p90:
        return blended, p50, p10, p90
    return blended, p50, p10


def persistence_forecast(prices: dict, until: datetime, tz) -> dict:
    """Fill the hours after the last known price up to `until` (exclusive) with the latest known
    price at the same local hour of day.

    Danish buy/sell prices repeat a daily shape (the grid tariff steps 00-06 / 06-17 / 17-21 /
    21-24 and the spot price's day shape), so "same hour as the last known day" is a sane,
    conservative stand-in until tomorrow's prices are published around 13:00. Without it the plan
    would end at midnight in the morning and could never weigh today's sun against tonight's grid
    price. Returns only the forecast hours.
    """
    if not prices:
        return {}
    by_local_hour = {}
    for h in sorted(prices):
        by_local_hour[h.astimezone(tz).hour] = prices[h]
    out = {}
    t = max(prices) + timedelta(hours=1)
    while t < until:
        price = by_local_hour.get(t.astimezone(tz).hour)
        if price is None:
            break
        out[t] = price
        t += timedelta(hours=1)
    return out


class Horizon:
    """Aligned per-step inputs for the optimiser. Step 0 is the (partial) current hour."""

    def __init__(self, starts, buy, sell, pv, load, dt, forecast=None):
        self.starts = starts
        self.buy = buy
        self.sell = sell
        self.pv = pv
        self.load = load
        self.dt = dt
        self.forecast = forecast or [False] * len(starts)  # True where the price is estimated

    def __len__(self):
        return len(self.starts)


def build_horizon(now: datetime, buy: dict, sell: dict, pv: dict, load_fn, max_hours: int = 48,
                  forecast_hours: set = None) -> Horizon:
    """Hours from the current one to the last hour with both a buy and a sell price.

    pv: {hour: kWh}; load_fn(hour_utc) -> kWh for a full hour. The current hour is scaled
    by its remaining fraction so the plan does not count energy that has already passed.
    forecast_hours: hours whose prices are estimates (flagged in the horizon).
    """
    forecast_hours = forecast_hours or set()
    now = now.astimezone(UTC)
    start = hour_floor(now)
    elapsed = (now - start).total_seconds() / 3600.0
    frac0 = max(0.1, 1.0 - elapsed)

    starts, b, s, p, l, dts, fc = [], [], [], [], [], [], []
    t = start
    while len(starts) < max_hours and t in buy and t in sell:
        f = frac0 if not starts else 1.0
        starts.append(t)
        b.append(buy[t])
        s.append(sell[t])
        p.append(pv.get(t, 0.0) * f)
        l.append(load_fn(t) * f)
        dts.append(f)
        fc.append(t in forecast_hours)
        t = t + timedelta(hours=1)
    return Horizon(starts, b, s, p, l, dts, fc)
