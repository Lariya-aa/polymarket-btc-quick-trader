"""Tests for the small signal-math primitives.

These feed fetch_btc_signal's probability calculation. None of them are
load-bearing themselves, but the relationships between them are
(momentum + trend + rsi_bias → sigmoid → probability) so each piece must
behave predictably.
"""

import pytest


def test_window_return_basic(bag):
    closes = [100.0, 101.0, 102.0, 103.0]
    # last vs (last - steps - 1) where steps=min(minutes, len-1)
    # minutes=1, steps=1 → closes[-1]/closes[-2] - 1 = 103/102 - 1
    assert bag.window_return(closes, 1) == pytest.approx(103 / 102 - 1)


def test_window_return_clamps_steps(bag):
    closes = [100.0, 110.0]
    # minutes=1000 but only one prior step → steps clamped to len-1
    assert bag.window_return(closes, 1000) == pytest.approx(0.1)


def test_ema_constant_series_equals_constant(bag):
    assert bag.ema([5.0] * 20, 10) == 5.0


def test_ema_responds_to_step_change(bag):
    # Series jumps from 100 to 200 → EMA should sit between, closer to the
    # tail. With period=5 (alpha≈0.333) the EMA after 5 step-ups is well
    # past the midpoint.
    values = [100.0] * 5 + [200.0] * 5
    result = bag.ema(values, 5)
    assert 150 < result < 200


def test_rsi_all_gains_is_100(bag):
    closes = [float(i) for i in range(1, 30)]  # monotonic rise
    assert bag.rsi(closes, 14) == 100.0


def test_rsi_all_losses_is_zero(bag):
    closes = [float(i) for i in range(30, 1, -1)]  # monotonic fall
    assert bag.rsi(closes, 14) == pytest.approx(0.0, abs=0.001)


def test_rsi_oscillating_near_50(bag):
    # +1, -1, +1, -1, ... average gain == average loss → RSI = 50.
    base = 100.0
    closes = []
    for i in range(40):
        closes.append(base + (1 if i % 2 == 0 else -1))
    assert bag.rsi(closes, 14) == pytest.approx(50.0, abs=2)


def test_market_horizon_uses_period_default_when_no_end_dt(bag):
    # No QuickMarket → mapping default falls through to 15 (the dict default key
    # behavior when period == "")
    assert bag.market_horizon_minutes(None) == 15


def test_market_horizon_known_period_strings(bag):
    # Build a minimal QuickMarket-shaped object — only .period and .end_dt are
    # read by market_horizon_minutes.
    class _Mkt:
        end_dt = None
    for period, expected in (("5m", 5), ("15m", 15), ("1h", 60), ("4h", 240), ("1d", 1440)):
        m = _Mkt()
        m.period = period
        assert bag.market_horizon_minutes(m) == expected


def test_market_horizon_uses_remaining_time_when_end_dt_in_future(bag):
    from datetime import datetime, timezone, timedelta

    class _Mkt:
        period = "5m"
        end_dt = datetime.now(timezone.utc) + timedelta(minutes=30)
    # ~30 minutes remaining → clamped into [3, 1440]
    horizon = bag.market_horizon_minutes(_Mkt())
    assert 25 <= horizon <= 31


def test_market_horizon_floor_is_three(bag):
    from datetime import datetime, timezone, timedelta

    class _Mkt:
        period = "5m"
        end_dt = datetime.now(timezone.utc) + timedelta(seconds=10)  # ~0 min
    assert bag.market_horizon_minutes(_Mkt()) == 3
