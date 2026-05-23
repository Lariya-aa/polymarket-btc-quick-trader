"""Tests for parse_minimax_json.

This parser sits between the LLM and the order-decision UI label. A subtle
bug here could mislabel an action — what the user reads on screen drives
their click. Cases below cover what we know the MiniMax-M2.7 model emits:
think-blocks, JSON wrapped in prose, out-of-range floats, missing fields.
"""

import pytest


def test_parses_minimal_json(bag):
    out = bag.parse_minimax_json('{"prob_up": 0.7, "action": "BUY_UP"}')
    assert out["prob_up"] == 0.7
    assert out["prob_down"] == pytest.approx(0.3)
    assert out["action"] == "BUY_UP"
    assert out["confidence"] == "LOW"  # default when missing


def test_strips_think_block(bag):
    raw = '<think>internal cot</think>{"prob_up": 0.4, "action": "BUY_DOWN", "confidence": "HIGH"}'
    out = bag.parse_minimax_json(raw)
    assert out["prob_up"] == 0.4
    assert out["action"] == "BUY_DOWN"
    assert out["confidence"] == "HIGH"


def test_handles_prose_around_json(bag):
    raw = 'Here you go: {"prob_up": 0.55, "action": "NO_TRADE", "confidence": "MEDIUM"} done'
    out = bag.parse_minimax_json(raw)
    assert out["prob_up"] == pytest.approx(0.55)
    assert out["action"] == "NO_TRADE"


def test_out_of_range_prob_is_clamped(bag):
    out = bag.parse_minimax_json('{"prob_up": 1.5, "action": "BUY_UP"}')
    assert out["prob_up"] == 1.0
    out = bag.parse_minimax_json('{"prob_up": -0.3, "action": "BUY_DOWN"}')
    assert out["prob_up"] == 0.0


def test_unknown_action_collapses_to_no_trade(bag):
    out = bag.parse_minimax_json('{"prob_up": 0.5, "action": "GO_WILD"}')
    assert out["action"] == "NO_TRADE"


def test_unknown_confidence_collapses_to_low(bag):
    out = bag.parse_minimax_json('{"prob_up": 0.5, "action": "BUY_UP", "confidence": "EXTREME"}')
    assert out["confidence"] == "LOW"


def test_non_json_raises(bag):
    with pytest.raises(ValueError):
        bag.parse_minimax_json("totally not json at all")


def test_only_think_block_raises(bag):
    # Edge case: model returned only thinking, nothing usable.
    with pytest.raises(ValueError):
        bag.parse_minimax_json("<think>still thinking...</think>")


def test_prob_down_defaults_to_complement(bag):
    out = bag.parse_minimax_json('{"prob_up": 0.6, "action": "BUY_UP"}')
    assert out["prob_down"] == pytest.approx(0.4)


def test_nan_prob_forces_no_trade(bag):
    # MiniMax has been observed to emit "nan" / "inf" as strings. float()
    # accepts them, min/max passes them through, and they then propagate
    # as NaN into UI labels and (worse) order sizing. Parser must
    # downgrade to NO_TRADE + 50/50 prob.
    import math
    out = bag.parse_minimax_json('{"prob_up": "nan", "action": "BUY_UP"}')
    assert math.isfinite(out["prob_up"])
    assert math.isfinite(out["prob_down"])
    assert out["action"] == "NO_TRADE"
    assert out["confidence"] == "LOW"
    assert "非有限" in out.get("reason", "")


def test_inf_prob_forces_no_trade(bag):
    import math
    out = bag.parse_minimax_json('{"prob_up": "inf", "action": "BUY_DOWN"}')
    assert math.isfinite(out["prob_up"])
    assert out["action"] == "NO_TRADE"


def test_negative_inf_prob_forces_no_trade(bag):
    import math
    out = bag.parse_minimax_json('{"prob_up": "-inf", "action": "BUY_UP"}')
    assert math.isfinite(out["prob_up"])
    assert out["action"] == "NO_TRADE"


def test_garbage_prob_string_falls_back_to_half(bag):
    # Non-numeric, non-special-float garbage: parser should fall back
    # to the 0.5 default rather than crashing.
    out = bag.parse_minimax_json('{"prob_up": "hello", "action": "BUY_UP"}')
    assert out["prob_up"] == 0.5
