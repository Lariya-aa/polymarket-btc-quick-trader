import math
from unittest import mock

import pytest

import poly_mm_pro_max as mod


def test_parse_minimax_json_picks_last_brace_block():
    """LLM emits reasoning + final JSON. Greedy regex would over-capture."""
    inst = mod.PolyQuickTrader.__new__(mod.PolyQuickTrader)
    inst.parse_minimax_json = mod.PolyQuickTrader.parse_minimax_json.__get__(inst)
    content = '{"draft": "ignored reasoning"} more text {"prob_up": 0.62, "action": "BUY_UP", "confidence": "HIGH"}'
    result = inst.parse_minimax_json(content)
    assert result["prob_up"] == 0.62
    assert result["action"] == "BUY_UP"


def test_parse_minimax_json_handles_think_block_stripping():
    inst = mod.PolyQuickTrader.__new__(mod.PolyQuickTrader)
    inst.parse_minimax_json = mod.PolyQuickTrader.parse_minimax_json.__get__(inst)
    content = '<think>internal reasoning here</think>\n{"prob_up": 0.7, "action": "BUY_UP", "confidence": "MEDIUM"}'
    result = inst.parse_minimax_json(content)
    assert result["prob_up"] == 0.7


def test_parse_minimax_json_safe_prob_rejects_nan():
    """Per Phase 9 V3 pattern: NaN must not propagate."""
    assert mod.PolyQuickTrader._safe_prob(float("nan"), default=0.5) == 0.5
    assert mod.PolyQuickTrader._safe_prob(float("inf"), default=0.5) == 0.5
    assert mod.PolyQuickTrader._safe_prob("not-a-number", default=0.5) == 0.5


def test_parse_minimax_json_safe_prob_clamps_range():
    assert mod.PolyQuickTrader._safe_prob(1.5, default=0.5) == 1.0
    assert mod.PolyQuickTrader._safe_prob(-0.3, default=0.5) == 0.0
    assert mod.PolyQuickTrader._safe_prob(0.42, default=0.5) == 0.42


def test_parse_minimax_json_string_with_percent_does_not_crash():
    """Codex previously flagged: '0.62%' string would ValueError straight up."""
    inst = mod.PolyQuickTrader.__new__(mod.PolyQuickTrader)
    inst.parse_minimax_json = mod.PolyQuickTrader.parse_minimax_json.__get__(inst)
    content = '{"prob_up": "0.62%", "action": "BUY_UP", "confidence": "HIGH"}'
    result = inst.parse_minimax_json(content)
    # Garbage prob falls back to default 0.5, but parse should not crash.
    assert result["prob_up"] == 0.5
    assert result["action"] == "BUY_UP"


def test_assert_order_response_ok_passes_on_matched():
    mod.PolyQuickTrader._assert_order_response_ok({"success": True, "status": "matched", "orderID": "0xabc"})


def test_assert_order_response_ok_passes_on_live():
    mod.PolyQuickTrader._assert_order_response_ok({"success": True, "status": "live", "orderID": "0xabc"})


def test_assert_order_response_ok_passes_on_delayed():
    mod.PolyQuickTrader._assert_order_response_ok({"success": True, "status": "delayed", "orderID": "0xabc"})


def test_assert_order_response_ok_rejects_success_false():
    with pytest.raises(RuntimeError, match="拒绝"):
        mod.PolyQuickTrader._assert_order_response_ok({"success": False, "error": "banned"})


def test_assert_order_response_ok_rejects_unmatched_status():
    with pytest.raises(RuntimeError, match="未稳定落地"):
        mod.PolyQuickTrader._assert_order_response_ok({"success": True, "status": "unmatched"})


def test_assert_order_response_ok_rejects_non_dict():
    with pytest.raises(RuntimeError, match="非 dict"):
        mod.PolyQuickTrader._assert_order_response_ok(None)


def test_assert_order_response_ok_rejects_missing_status():
    """Per Codex Phase 6 blocker: a 200-shaped response missing the
    status field must abort. Treating it as success would let unparsed
    server replies flow into trade-result handling."""
    with pytest.raises(RuntimeError, match="未稳定落地"):
        mod.PolyQuickTrader._assert_order_response_ok({"success": True, "orderID": "0xabc"})


def test_assert_order_response_ok_rejects_empty_status():
    with pytest.raises(RuntimeError, match="未稳定落地"):
        mod.PolyQuickTrader._assert_order_response_ok({"success": True, "status": ""})


def test_derive_api_creds_records_last_error(monkeypatch):
    """V9 falsifiable: credential failure must update last_credential_error."""
    inst = mod.PolyQuickTrader.__new__(mod.PolyQuickTrader)
    inst.logger = mod.logging.getLogger("test_honesty")
    inst.ent_priv_key = mock.MagicMock()
    inst.ent_priv_key.get.return_value = "0xdeadbeef"
    inst.root = None  # non-GUI path
    inst.last_credential_error = None
    monkeypatch.setattr(mod, "ClobClient", mock.MagicMock(side_effect=ValueError("simulated POLY_1271 signer mismatch")))

    import asyncio
    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(inst.derive_api_creds())
    finally:
        loop.close()
    assert result is None
    assert inst.last_credential_error is not None
    assert "POLY_1271" in inst.last_credential_error or "signer mismatch" in inst.last_credential_error
