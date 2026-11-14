"""Real sensor data from 2026-11-14 (Aalborg, DK1). Prices in DKK/kWh incl. tariffs/taxes."""
BUY_TODAY = [2.057, 1.999, 1.962, 1.972, 1.945, 1.941, 2.184, 2.286, 2.299, 2.193, 2.036, 1.891,
             1.873, 1.969, 2.153, 2.284, 2.4, 3.116, 2.981, 2.922, 2.827, 2.121, 2.056, 2.027]
SELL_TODAY = [1.373, 1.326, 1.297, 1.305, 1.283, 1.28, 1.299, 1.38, 1.39, 1.306, 1.181, 1.065,
              1.05, 1.127, 1.273, 1.379, 1.471, 1.517, 1.409, 1.362, 1.286, 1.248, 1.196, 1.173]

# Solcast detailedHourly for 2026-11-14: (pv_estimate, pv_estimate10, pv_estimate90) per hour 00..23
SOLCAST_HOURLY = [(0, 0, 0)] * 8 + [
    (0.1175, 0.0858, 0.1291), (0.5216, 0.3623, 0.7321), (0.8177, 0.3049, 1.7414),
    (1.1698, 0.893, 1.3891), (0.9645, 0.518, 2.0717), (1.1461, 0.355, 3.8403),
    (0.4612, 0.1399, 2.8978), (0.0738, 0.0267, 0.593),
] + [(0, 0, 0)] * 8


def raw_prices(day, values):
    return [{"hour": f"{day}T{h:02d}:00:00+01:00", "price": p} for h, p in enumerate(values)]


def price_attrs(values, day="2026-11-14"):
    return {"raw_today": raw_prices(day, values), "raw_tomorrow": None}


def solcast_attrs(day="2026-11-14"):
    return {"detailedHourly": [
        {"period_start": f"{day}T{h:02d}:00:00+01:00", "pv_estimate": a, "pv_estimate10": b, "pv_estimate90": c}
        for h, (a, b, c) in enumerate(SOLCAST_HOURLY)]}
