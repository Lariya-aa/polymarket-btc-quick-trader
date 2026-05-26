import asyncio
from unittest import mock

import pytest

import poly_mm_pro_max as mod


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_trader():
    inst = mod.PolyQuickTrader.__new__(mod.PolyQuickTrader)
    inst.logger = mod.logging.getLogger("test_account_refresh")
    inst.ent_funder = mock.MagicMock()
    inst.ent_funder.get.return_value = "0x1941F17823FD51AD8887417100619EB42eEc0d6A"
    inst.POLYGON_USDC_CONTRACT = mod.PolyQuickTrader.POLYGON_USDC_CONTRACT
    inst.POLYGON_RPC_URL = mod.PolyQuickTrader.POLYGON_RPC_URL
    return inst


class _FakeCtx:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload
    async def __aenter__(self):
        return self
    async def __aexit__(self, *_):
        return False
    async def json(self):
        return self._payload


def test_usdc_balance_hex_to_float():
    """0x5F5E100 = 100000000 → 100.0 USDC (6 decimals)."""
    inst = _make_trader()
    fake_session = mock.MagicMock()
    fake_session.post = mock.MagicMock(return_value=_FakeCtx(200, {"jsonrpc": "2.0", "id": 1, "result": "0x5f5e100"}))
    with mock.patch.object(mod, "_get_aiohttp_session", return_value=fake_session):
        result = _run(inst._fetch_usdc_balance_onchain())
    assert result == 100.0


def test_usdc_balance_invalid_funder_returns_none():
    inst = _make_trader()
    inst.ent_funder.get.return_value = "not-an-address"
    result = _run(inst._fetch_usdc_balance_onchain())
    assert result is None


def test_usdc_balance_empty_funder_returns_none():
    inst = _make_trader()
    inst.ent_funder.get.return_value = ""
    result = _run(inst._fetch_usdc_balance_onchain())
    assert result is None


def test_usdc_balance_rpc_500_returns_none():
    inst = _make_trader()
    fake_session = mock.MagicMock()
    fake_session.post = mock.MagicMock(return_value=_FakeCtx(500, {}))
    with mock.patch.object(mod, "_get_aiohttp_session", return_value=fake_session):
        result = _run(inst._fetch_usdc_balance_onchain())
    assert result is None


def test_usdc_balance_missing_result_returns_none():
    inst = _make_trader()
    fake_session = mock.MagicMock()
    fake_session.post = mock.MagicMock(return_value=_FakeCtx(200, {"jsonrpc": "2.0", "id": 1, "error": "x"}))
    with mock.patch.object(mod, "_get_aiohttp_session", return_value=fake_session):
        result = _run(inst._fetch_usdc_balance_onchain())
    assert result is None


def test_positions_value_parses_first_entry():
    inst = _make_trader()
    fake_session = mock.MagicMock()
    fake_session.get = mock.MagicMock(return_value=_FakeCtx(200, [{"user": "0xabc", "value": 42.5}]))
    with mock.patch.object(mod, "_get_aiohttp_session", return_value=fake_session):
        result = _run(inst._fetch_positions_value())
    assert result == 42.5


def test_positions_value_empty_array_returns_zero():
    inst = _make_trader()
    fake_session = mock.MagicMock()
    fake_session.get = mock.MagicMock(return_value=_FakeCtx(200, []))
    with mock.patch.object(mod, "_get_aiohttp_session", return_value=fake_session):
        result = _run(inst._fetch_positions_value())
    assert result == 0.0


def test_render_account_status_shows_stale_color_after_90s():
    inst = _make_trader()
    inst.account_last_refresh_ts = 1000.0
    inst.account_last_balance_usdc = 50.0
    inst.account_last_positions_value = 20.0
    inst.account_last_positions_count = 3
    inst.lbl_account_status = mock.MagicMock()
    inst._float_or_zero = mod.PolyQuickTrader._float_or_zero.__get__(inst)

    inst._render_account_status_label(now=1100.0)
    args = inst.lbl_account_status.configure.call_args
    assert args.kwargs.get("foreground") == "#d97706"
    assert "100s" in args.kwargs.get("text", "")


def test_render_account_status_fresh_color_before_90s():
    inst = _make_trader()
    inst.account_last_refresh_ts = 1000.0
    inst.account_last_balance_usdc = 50.0
    inst.account_last_positions_value = 20.0
    inst.account_last_positions_count = 3
    inst.lbl_account_status = mock.MagicMock()
    inst._float_or_zero = mod.PolyQuickTrader._float_or_zero.__get__(inst)

    inst._render_account_status_label(now=1030.0)
    args = inst.lbl_account_status.configure.call_args
    assert args.kwargs.get("foreground") == "#475569"
    assert "30s" in args.kwargs.get("text", "")


def test_apply_refresh_preserves_ts_when_all_sources_fail():
    """Codex Phase 10 V3 blocker: if pos_value=None AND balance=None,
    the refresh failed end-to-end. _apply_account_refresh must NOT
    update account_last_refresh_ts — otherwise the stale orange label
    never fires."""
    inst = _make_trader()
    inst.account_last_refresh_ts = 1000.0
    inst.account_last_balance_usdc = 99.0
    inst.account_last_positions_value = 50.0
    inst.account_last_positions_count = 2
    inst.latest_positions = [{"size": "1.0"}]
    inst.lbl_account_status = mock.MagicMock()
    inst._float_or_zero = mod.PolyQuickTrader._float_or_zero.__get__(inst)
    inst.render_positions = mock.MagicMock()

    pre_ts = inst.account_last_refresh_ts
    # All 3 sources failed: positions=[] (silent fail), pos_value=None, balance=None
    mod.PolyQuickTrader._apply_account_refresh.__get__(inst)([], None, None)
    # ts MUST NOT advance to current time (would suppress stale marker)
    assert inst.account_last_refresh_ts == pre_ts
    # Prior values preserved (not zeroed)
    assert inst.account_last_balance_usdc == 99.0
    assert inst.account_last_positions_value == 50.0
    # render_positions NOT called (since we don't trust the empty list)
    inst.render_positions.assert_not_called()


def test_apply_refresh_advances_ts_when_balance_succeeds():
    """One source succeeding is enough to mark the cycle fresh."""
    inst = _make_trader()
    inst.account_last_refresh_ts = 1000.0
    inst.account_last_balance_usdc = None
    inst.account_last_positions_value = None
    inst.account_last_positions_count = 0
    inst.lbl_account_status = mock.MagicMock()
    inst._float_or_zero = mod.PolyQuickTrader._float_or_zero.__get__(inst)
    inst.render_positions = mock.MagicMock()

    mod.PolyQuickTrader._apply_account_refresh.__get__(inst)(None, None, 42.0)
    assert inst.account_last_refresh_ts > 1000.0
    assert inst.account_last_balance_usdc == 42.0
