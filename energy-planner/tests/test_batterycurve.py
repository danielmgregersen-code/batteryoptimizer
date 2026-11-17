from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

import batterycurve as bc
from optimizer import BatteryParams, solve

UTC = timezone.utc
CAP, MAXKW = 7.75, 4.0


def samples(n_days=10):
    """5-minute samples: charging sessions from 60 % to 100 % with a taper above 90 %."""
    power, soc = [], []
    t = datetime(2026, 11, 1, tzinfo=UTC)
    for _ in range(n_days):
        s = 60.0
        while s < 99.9:
            p = MAXKW if s < 90 else MAXKW - (MAXKW - 1.0) * (s - 90) / 10
            power.append({"start": t.timestamp() * 1000, "mean": p * 1000})
            soc.append({"start": t.timestamp() * 1000, "mean": s})
            s = min(100.0, s + p / 12 / CAP * 100)
            t += timedelta(minutes=5)
        t += timedelta(hours=20)
    return power, soc


def test_learns_the_taper_from_5_minute_samples():
    info = bc.learn(*samples(), MAXKW)
    assert info["learned"]
    assert bc.power_at(info["curve"], 85, MAXKW) == pytest.approx(4.0, abs=0.05)
    assert bc.power_at(info["curve"], 95, MAXKW) == pytest.approx(2.5, abs=0.4)
    assert bc.power_at(info["curve"], 99, MAXKW) == pytest.approx(1.3, abs=0.4)
    ks = [k for _, k in info["curve"]]
    assert all(a >= b for a, b in zip(ks, ks[1:])) and max(ks) <= MAXKW


def test_too_little_data_gives_the_default_curve():
    p, s = samples(1)
    info = bc.learn(p[:5], s[:5], MAXKW)
    assert not info["learned"] and info["curve"] == bc.default_curve(MAXKW)


def test_step_energy_and_lines():
    curve = bc.default_curve(MAXKW)
    assert bc.stored_in_step(curve, CAP, MAXKW, 0.3 * CAP, 1.0) == pytest.approx(MAXKW, abs=0.01)
    assert bc.stored_in_step(curve, CAP, MAXKW, 0.5 * CAP, 1.0) < MAXKW      # reaches 90 % after 46 min
    near_full = bc.stored_in_step(curve, CAP, MAXKW, 0.85 * CAP, 1.0)
    assert near_full < 0.15 * CAP + 1e-6 and near_full == pytest.approx(0.15 * CAP, abs=0.02)
    lines = bc.step_lines(curve, CAP, MAXKW, 1.0)
    assert lines
    for i in range(60, 101):
        s = CAP * i / 100
        g = bc.stored_in_step(curve, CAP, MAXKW, s, 1.0)
        assert min(a + b * s for a, b in lines) >= g - 1e-6          # never stricter than the curve


def test_plan_does_not_count_on_full_power_up_to_100_percent():
    P = BatteryParams(CAP, MAXKW, 0.775, CAP, 0.97, 0.94, 0.95, 0.0)
    buy = [0.5, 3.0, 3.0, 3.0]
    T = len(buy)
    args = (buy, [0.1] * T, [0.0] * T, [2.5] * T, np.ones(T), CAP * 0.80, P, 0.5 * 0.95)
    free = solve(*args)
    curve = [(70, MAXKW), (80, MAXKW), (100, 0.3)]              # a steep taper above 80 %
    lines = [bc.step_lines(curve, CAP, MAXKW, 1.0)] * T
    taper = solve(*args, charge_lines=lines)
    assert free.soc_end[0] == pytest.approx(CAP, abs=0.01)      # without the taper: full in one hour
    stored = taper.soc_end[0] - CAP * 0.80
    possible = bc.stored_in_step(curve, CAP, MAXKW, CAP * 0.80, 1.0)
    assert possible < 0.2 * CAP and stored <= possible + 1e-6
    assert taper.soc_end[0] < CAP - 0.01
