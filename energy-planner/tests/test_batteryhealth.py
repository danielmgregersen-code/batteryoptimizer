from datetime import datetime, timedelta, timezone

import batteryhealth as bh

T0 = datetime(2026, 11, 20, 0, 0, tzinfo=timezone.utc)


def sim(cap=7.0, eta_c=0.95, eta_d=0.95, cycles=6, kw=2.0, low=15.0, high=90.0, both=False):
    """5-minute rows of a battery cycling between low and high SoC at kw (counter energy)."""
    soc_rows, in_rows, out_rows = [], [], []
    soc, t = low, T0
    step_kwh = kw / 12.0
    for _ in range(cycles):
        for direction in (1, -1):
            for _ in range(2):                       # idle between runs
                soc_rows.append({"start": t.isoformat(), "mean": soc})
                in_rows.append({"start": t.isoformat(), "change": 0.0})
                out_rows.append({"start": t.isoformat(), "change": 0.0})
                t += timedelta(minutes=5)
            while (direction == 1 and soc < high) or (direction == -1 and soc > low):
                stored = step_kwh * eta_c if direction == 1 else -step_kwh / eta_d
                before = soc
                soc = soc + stored / cap * 100.0
                soc_rows.append({"start": t.isoformat(), "mean": (before + soc) / 2})
                in_rows.append({"start": t.isoformat(), "change": step_kwh if direction == 1 else (0.01 if both else 0.0)})
                out_rows.append({"start": t.isoformat(), "change": step_kwh if direction == -1 else 0.0})
                t += timedelta(minutes=5)
    return soc_rows, in_rows, out_rows


def test_charge_and_discharge_runs_give_the_real_capacity():
    runs = bh.find_runs(*sim())
    assert {r["dir"] for r in runs} == {"charge", "discharge"} and len(runs) == 12
    ch = [r for r in runs if r["dir"] == "charge"][0]
    assert ch["raw_kwh"] > 7.0 > [r for r in runs if r["dir"] == "discharge"][0]["raw_kwh"]
    est = bh.estimate(runs, 7.75)
    assert est["learned"] and abs(est["kwh"] - 7.0) / 7.0 < 0.02
    assert abs(est["soh_pct"] - 7.0 / 7.75 * 100) < 2


def test_small_swings_the_top_and_mixed_intervals_are_left_out():
    assert bh.find_runs(*sim(low=40.0, high=55.0)) == []                 # under 25 points
    top = bh.find_runs(*sim(low=60.0, high=100.0))                         # stops at 95 %
    assert all(r["soc_to"] <= 95.0 and r["soc_from"] <= 95.0 for r in top)
    mixed = bh.find_runs(*sim(both=True))                                 # input moves while discharging
    assert all(r["dir"] == "charge" for r in mixed)


def test_a_gap_in_the_data_splits_a_run():
    soc, i, o = sim(cycles=1, low=15.0, high=90.0)
    del soc[10:13], i[10:13], o[10:13]                                    # 15 minutes missing early in the charge
    runs = bh.find_runs(soc, i, o)
    assert all(abs(r["soc_to"] - r["soc_from"]) >= 25 for r in runs)
    assert len([r for r in runs if r["dir"] == "charge"]) == 1


def test_not_learned_with_too_few_runs_and_remember_dedupes():
    runs = bh.find_runs(*sim(cycles=3))
    est = bh.estimate(runs, 7.75)
    assert est["kwh"] and not est["learned"] and est["charge_runs"] == 3
    store = bh.remember([], runs)
    assert bh.remember(store, runs) == store
