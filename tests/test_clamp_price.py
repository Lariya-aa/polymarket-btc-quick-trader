"""Tests for clamp_price + price_decimals.

clamp_price keeps the limit price within (tick, 1-tick) and rounds to the
tick's decimal precision. It's called by every order path so a regression
here can submit orders the exchange rejects.
"""


def test_price_decimals_simple(bag):
    assert bag.price_decimals("0.01") == 2
    assert bag.price_decimals("0.001") == 3
    assert bag.price_decimals("0.1") == 1
    assert bag.price_decimals("1") == 0


def test_clamp_price_within_bounds(bag):
    assert bag.clamp_price(0.5, "0.01") == 0.5
    assert bag.clamp_price(0.42, "0.01") == 0.42


def test_clamp_price_too_high_pulls_below_one(bag):
    # 1.5 → max valid is 1 - tick = 0.99
    assert bag.clamp_price(1.5, "0.01") == 0.99
    assert bag.clamp_price(1.5, "0.001") == 0.999


def test_clamp_price_too_low_pulls_above_zero(bag):
    # -0.5 → min valid is tick
    assert bag.clamp_price(-0.5, "0.01") == 0.01
    assert bag.clamp_price(0.0, "0.001") == 0.001


def test_clamp_price_rounds_to_tick_decimals(bag):
    # tick 0.01 has 2 decimals → 0.123456 → 0.12
    assert bag.clamp_price(0.123456, "0.01") == 0.12
    # tick 0.001 has 3 decimals → 0.123456 → 0.123
    assert bag.clamp_price(0.123456, "0.001") == 0.123
