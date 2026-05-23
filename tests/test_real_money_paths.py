"""Tests for the real-money order paths (buy/sell) and fetch_positions
shape-handling. These were the four BLOCKER findings from the Codex
review pass-1.

We stub py_clob_client_v2 + the order-submission path with MagicMock /
custom sync stubs so no real HTTP / no wallet touch / no $$$ moves.
"""

import asyncio
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

import poly_mm_pro_max as M


# ── shared fixtures ───────────────────────────────────────────────────────


def _trader():
    """Build a PolyQuickTrader instance without invoking its __init__
    (which would create Tk widgets, open log files, etc.). Same trick
    the bag fixture uses, but we need a real instance because the
    methods under test call self.logger and self.ent_funder."""
    t = M.PolyQuickTrader.__new__(M.PolyQuickTrader)
    import logging
    t.logger = logging.getLogger("test_trader")
    t.logger.addHandler(logging.NullHandler())
    t.last_positions_fetch_error = None
    return t


@pytest.fixture
def trader():
    return _trader()


def _btc_market():
    return M.PolyMarket(
        slug="btc-x", event_slug="btc-x", question="Will BTC go up?",
        yes_id="yes-token", no_id="no-token",
        tick_size="0.01", period="5m", end_dt=None, ended=False,
        yes_bid=0.45, yes_ask=0.55, no_bid=0.45, no_ask=0.55,
        spread=0.1, volume24h=10000.0, category="BTC", subject="5m",
    )


# ── buy_quick_market: timeout + reject branches ───────────────────────────


def test_buy_timeout_raises_with_local_attempt_id(trader, monkeypatch):
    """The whole point of local_attempt_id: when the 25s wait_for times
    out, the user must see a uuid they can grep against the log to
    reconcile, plus a message explicitly telling them NOT to retry."""
    market = _btc_market()

    monkeypatch.setattr(trader, "validate_credentials_config", lambda: {
        "priv_key": "0xkey", "api_key": "k", "secret": "s",
        "passphrase": "p", "funder": "0xfunder", "signature_type": 3,
    })
    async def fake_best_ask(self, client, token_id):
        return 0.50, "0.01"
    monkeypatch.setattr(M.PolyQuickTrader, "best_ask_for_token", fake_best_ask)
    # Sync stub: production code wraps the CLOB call with
    # asyncio.to_thread() which expects a sync callable. An async stub
    # would bypass the threading and the timeout would never fire.
    def slow_post(*args, **kwargs):
        time.sleep(0.5)
        return {"success": True}
    fake_client = MagicMock()
    fake_client.create_and_post_order = slow_post
    monkeypatch.setattr(trader, "build_client", lambda c, cr: fake_client)
    monkeypatch.setattr(M, "ORDER_SUBMIT_TIMEOUT_SECONDS", 0.05)

    with pytest.raises(RuntimeError) as exc:
        asyncio.run(trader.buy_quick_market(market, "UP", 5.0, 0.99))
    msg = str(exc.value)
    assert "超时" in msg
    assert "local_attempt_id=" in msg
    # UUIDv4 has 4 dashes embedded in the printed id.
    assert len([c for c in msg if c == "-"]) >= 4
    # Critical for real money: don't-retry guidance must be present.
    assert "切勿" in msg or "勿" in msg


def test_buy_rejected_raises_with_local_attempt_id(trader, monkeypatch):
    """If the exchange returns success: False, surface the local id so
    the user can correlate with their wallet activity."""
    market = _btc_market()
    monkeypatch.setattr(trader, "validate_credentials_config", lambda: {
        "priv_key": "0xkey", "api_key": "k", "secret": "s",
        "passphrase": "p", "funder": "0xfunder", "signature_type": 3,
    })
    fake_client = MagicMock()
    fake_client.create_and_post_order = MagicMock(return_value={
        "success": False, "errorMsg": "insufficient balance",
    })
    monkeypatch.setattr(trader, "build_client", lambda c, cr: fake_client)
    async def fake_best_ask(self, client, token_id):
        return 0.50, "0.01"
    monkeypatch.setattr(M.PolyQuickTrader, "best_ask_for_token", fake_best_ask)

    with pytest.raises(RuntimeError) as exc:
        asyncio.run(trader.buy_quick_market(market, "UP", 5.0, 0.99))
    msg = str(exc.value)
    assert "local_attempt_id=" in msg
    assert "拒绝" in msg


def test_buy_rejects_when_ask_above_max_price(trader, monkeypatch):
    """Price-protection layer: if the order book best_ask exceeds the
    user's stated max_price, refuse to submit. No order should be
    submitted, no local_attempt_id generated."""
    market = _btc_market()
    monkeypatch.setattr(trader, "validate_credentials_config", lambda: {
        "priv_key": "0xkey", "api_key": "k", "secret": "s",
        "passphrase": "p", "funder": "0xfunder", "signature_type": 3,
    })
    submission_calls = []
    fake_client = MagicMock()
    fake_client.create_and_post_order = MagicMock(
        side_effect=lambda *a, **kw: submission_calls.append(kw) or {})
    monkeypatch.setattr(trader, "build_client", lambda c, cr: fake_client)
    async def expensive_ask(self, client, token_id):
        return 0.80, "0.01"
    monkeypatch.setattr(M.PolyQuickTrader, "best_ask_for_token", expensive_ask)

    with pytest.raises(RuntimeError) as exc:
        asyncio.run(trader.buy_quick_market(market, "UP", 5.0, 0.50))
    assert "高于最高价" in str(exc.value)
    assert submission_calls == [], "should not submit when ask > max_price"


# ── BLOCKER C1 (Codex pass-2): NaN / inf in order pipeline ────────────────


def test_buy_rejects_nan_ask_price(trader, monkeypatch):
    """If best_ask comes back as NaN (rare but possible — strings like
    'nan' survive float() coercion), every downstream guard fails
    silently because NaN comparisons are False. Must be rejected
    explicitly before reaching OrderArgs."""
    import math
    market = _btc_market()
    monkeypatch.setattr(trader, "validate_credentials_config", lambda: {
        "priv_key": "0xkey", "api_key": "k", "secret": "s",
        "passphrase": "p", "funder": "0xfunder", "signature_type": 3,
    })
    fake_client = MagicMock()
    fake_client.create_and_post_order = MagicMock(return_value={"success": True})
    monkeypatch.setattr(trader, "build_client", lambda c, cr: fake_client)
    async def nan_ask(self, client, token_id):
        return float("nan"), "0.01"
    monkeypatch.setattr(M.PolyQuickTrader, "best_ask_for_token", nan_ask)

    with pytest.raises(RuntimeError) as exc:
        asyncio.run(trader.buy_quick_market(market, "UP", 5.0, 0.99))
    assert "非有限" in str(exc.value) or "nan" in str(exc.value).lower()


def test_clamp_price_rejects_nan(bag):
    """clamp_price used min/max + round which all propagate NaN
    silently — round(NaN, 2) returns NaN. Must raise on NaN/inf."""
    with pytest.raises(ValueError):
        bag.clamp_price(float("nan"), "0.01")
    with pytest.raises(ValueError):
        bag.clamp_price(float("inf"), "0.01")
    with pytest.raises(ValueError):
        bag.clamp_price(0.5, "nan")
    with pytest.raises(ValueError):
        bag.clamp_price(0.5, "0")  # non-positive tick


def test_best_ask_drops_nan_levels(trader, monkeypatch):
    """best_ask_for_token used to coerce any 'price' field through
    float() unconditionally — so a level like {'price': 'nan'} would
    survive and dominate min(asks). Verify the filter keeps it out."""
    import math
    book = {
        "asks": [
            {"price": "nan", "size": "10"},
            {"price": "0.55", "size": "5"},
            {"price": "0.60", "size": "20"},
        ],
        "tick_size": "0.01",
    }
    fake_client = MagicMock()
    fake_client.get_order_book = MagicMock(return_value=book)
    best_ask, tick = asyncio.run(trader.best_ask_for_token(fake_client, "token-x"))
    assert best_ask == 0.55, "NaN level should be dropped, 0.55 wins"
    assert tick == "0.01"


def test_best_ask_returns_none_when_all_levels_nonfinite(trader):
    """If every ask level is non-finite the function should report
    'no asks' (None) rather than NaN."""
    book = {"asks": [{"price": "nan"}, {"price": "inf"}], "tick_size": "0.01"}
    fake_client = MagicMock()
    fake_client.get_order_book = MagicMock(return_value=book)
    best_ask, tick = asyncio.run(trader.best_ask_for_token(fake_client, "token-x"))
    assert best_ask is None


def test_sell_rejects_nan_inputs(trader, monkeypatch):
    """sell_position_limit takes size + price from caller (computed
    from a position dict). If the position has NaN size or price, the
    guard must reject before reaching OrderArgs."""
    position = {"asset": "yes-token", "outcome": "Yes",
                "orderPriceMinTickSize": "0.01"}
    monkeypatch.setattr(trader, "validate_credentials_config", lambda: {
        "priv_key": "0xkey", "api_key": "k", "secret": "s",
        "passphrase": "p", "funder": "0xfunder", "signature_type": 3,
    })
    fake_client = MagicMock()
    monkeypatch.setattr(trader, "build_client", lambda c, cr: fake_client)
    import math
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(trader.sell_position_limit(position, size=float("nan"), price=0.5))
    assert "非法" in str(exc.value)
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(trader.sell_position_limit(position, size=10.0, price=float("inf")))
    assert "非法" in str(exc.value)


# ── sell_position_limit: timeout branch ───────────────────────────────────


def test_sell_timeout_raises_with_local_attempt_id(trader, monkeypatch):
    position = {
        "asset": "yes-token", "outcome": "Yes",
        "orderPriceMinTickSize": "0.01",
        "title": "test", "slug": "test", "eventSlug": "test",
    }
    monkeypatch.setattr(trader, "validate_credentials_config", lambda: {
        "priv_key": "0xkey", "api_key": "k", "secret": "s",
        "passphrase": "p", "funder": "0xfunder", "signature_type": 3,
    })
    def slow_post(*args, **kwargs):
        time.sleep(0.5)
        return {"success": True}
    fake_client = MagicMock()
    fake_client.create_and_post_order = slow_post
    monkeypatch.setattr(trader, "build_client", lambda c, cr: fake_client)
    monkeypatch.setattr(M, "ORDER_SUBMIT_TIMEOUT_SECONDS", 0.05)

    with pytest.raises(RuntimeError) as exc:
        asyncio.run(trader.sell_position_limit(position, size=10.0, price=0.5))
    msg = str(exc.value)
    assert "超时" in msg
    assert "local_attempt_id=" in msg
    assert "卖出" in msg


def test_sell_rejected_raises_with_local_attempt_id(trader, monkeypatch):
    """Symmetric to test_buy_rejected_raises_with_local_attempt_id —
    a sell that the exchange rejects with success:False must surface
    the local_attempt_id so the user can correlate."""
    position = {
        "asset": "yes-token", "outcome": "Yes",
        "orderPriceMinTickSize": "0.01",
        "title": "test", "slug": "test", "eventSlug": "test",
    }
    monkeypatch.setattr(trader, "validate_credentials_config", lambda: {
        "priv_key": "0xkey", "api_key": "k", "secret": "s",
        "passphrase": "p", "funder": "0xfunder", "signature_type": 3,
    })
    fake_client = MagicMock()
    fake_client.create_and_post_order = MagicMock(return_value={
        "success": False, "errorMsg": "size too small",
    })
    monkeypatch.setattr(trader, "build_client", lambda c, cr: fake_client)
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(trader.sell_position_limit(position, size=10.0, price=0.5))
    msg = str(exc.value)
    assert "local_attempt_id=" in msg
    assert "拒绝" in msg


# ── fetch_positions: response-shape branches ──────────────────────────────


def test_fetch_positions_empty_user_returns_empty_no_error(trader):
    """No funder address → return empty + don't set error (this is the
    "user hasn't filled in their wallet yet" case, not an API failure)."""
    fake_entry = MagicMock()
    fake_entry.get.return_value = "   "  # whitespace
    trader.ent_funder = fake_entry
    trader.last_positions_fetch_error = "stale prior error"

    result = asyncio.run(trader.fetch_positions())
    assert result == []
    assert trader.last_positions_fetch_error is None


def test_fetch_positions_non_200_sets_error(trader, monkeypatch):
    """HTTP 502 from the positions API: error flag must be set so the
    UI shows '⚠ 持仓接口失败' instead of misleading silence."""
    fake_entry = MagicMock()
    fake_entry.get.return_value = "0xfunder"
    trader.ent_funder = fake_entry

    class _FakeResp:
        status = 502
        async def json(self): return []
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass

    class _FakeSession:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        def get(self, *a, **kw): return _FakeResp()

    monkeypatch.setattr(M.aiohttp, "ClientSession", _FakeSession)

    result = asyncio.run(trader.fetch_positions())
    assert result == []
    assert trader.last_positions_fetch_error is not None
    assert "502" in trader.last_positions_fetch_error


def test_fetch_positions_non_list_sets_error(trader, monkeypatch):
    """200 OK but body is a dict (e.g. {"error": "..."} or some other
    schema drift): MUST set last_positions_fetch_error. This is the
    BLOCKER #2 bug that was fixed in commit ca16ecd."""
    fake_entry = MagicMock()
    fake_entry.get.return_value = "0xfunder"
    trader.ent_funder = fake_entry
    trader.last_positions_fetch_error = "stale prior error"

    class _FakeResp:
        status = 200
        async def json(self): return {"error": "bad request"}
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass

    class _FakeSession:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        def get(self, *a, **kw): return _FakeResp()

    monkeypatch.setattr(M.aiohttp, "ClientSession", _FakeSession)

    result = asyncio.run(trader.fetch_positions())
    assert result == []
    assert trader.last_positions_fetch_error is not None, \
        "non-list response must set the error flag"
    assert "shape" in trader.last_positions_fetch_error or "dict" in trader.last_positions_fetch_error


def test_fetch_positions_list_response_clears_error(trader, monkeypatch):
    """Genuine empty list response should clear any prior error so the
    UI doesn't carry stale '⚠' state forever."""
    fake_entry = MagicMock()
    fake_entry.get.return_value = "0xfunder"
    trader.ent_funder = fake_entry
    trader.last_positions_fetch_error = "stale prior error"

    class _FakeResp:
        status = 200
        async def json(self): return []  # zero positions, but valid shape
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass

    class _FakeSession:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        def get(self, *a, **kw): return _FakeResp()

    monkeypatch.setattr(M.aiohttp, "ClientSession", _FakeSession)

    result = asyncio.run(trader.fetch_positions())
    assert result == []
    assert trader.last_positions_fetch_error is None


def test_fetch_positions_exception_sets_error(trader, monkeypatch):
    """Network exception path: error flag set + result is empty list."""
    fake_entry = MagicMock()
    fake_entry.get.return_value = "0xfunder"
    trader.ent_funder = fake_entry

    class _FakeSession:
        def __init__(self, *a, **kw):
            raise OSError("DNS lookup failed")

    monkeypatch.setattr(M.aiohttp, "ClientSession", _FakeSession)

    result = asyncio.run(trader.fetch_positions())
    assert result == []
    assert trader.last_positions_fetch_error is not None
    assert "OSError" in trader.last_positions_fetch_error


# ── _display_direction (UI mapping helper added in commit 160eacb) ────────


def test_display_direction_maps_up_to_yes(bag):
    assert bag._display_direction("UP") == "Yes"


def test_display_direction_maps_down_to_no(bag):
    assert bag._display_direction("DOWN") == "No"


def test_display_direction_passthrough_unknown(bag):
    assert bag._display_direction("FOO") == "FOO"


# ── BLOCKER C-P3-2 (Codex pass-3): push_trade_result must honor
#                                    last_positions_fetch_error ──────────


def test_push_trade_result_surfaces_positions_api_failure(trader, monkeypatch):
    """After a successful order, push_trade_result calls fetch_positions
    to render PnL. If that fetch fails (last_positions_fetch_error set),
    the markdown body must surface the error — not silently say
    "当前没有可见持仓", which a user reading the ServerChan
    notification could mistake for "my trade vaporized"."""
    pushed = {}

    async def fake_fetch(self):
        # Simulate fetch_positions hitting an API error path.
        self.last_positions_fetch_error = "HTTP 502"
        return []

    async def fake_push(self, title, content):
        pushed["title"] = title
        pushed["content"] = content

    monkeypatch.setattr(M.PolyQuickTrader, "fetch_positions", fake_fetch)
    monkeypatch.setattr(M.PolyQuickTrader, "push_to_server_chan", fake_push)
    trader.last_positions_fetch_error = None  # start clean
    trader.root = MagicMock()  # so render_positions dispatch via root.after works

    asyncio.run(trader.push_trade_result(
        action="快速买入", market_title="Will BTC go up?",
        direction="UP", size=10.0, price=0.5,
        resp={"orderID": "0xabc", "success": True},
        market_slug="btc-x",
    ))
    body = pushed["content"]
    assert "持仓接口失败" in body
    assert "502" in body
    assert "当前没有可见持仓" not in body, \
        "must not show the empty-positions message when API actually failed"


def test_push_trade_result_uses_pnl_when_positions_ok(trader, monkeypatch):
    """Sanity: when fetch_positions succeeds (error flag None), the
    normal positions_pnl_markdown is used."""
    pushed = {}

    async def fake_fetch(self):
        self.last_positions_fetch_error = None
        return []  # genuinely zero positions

    async def fake_push(self, title, content):
        pushed["content"] = content

    monkeypatch.setattr(M.PolyQuickTrader, "fetch_positions", fake_fetch)
    monkeypatch.setattr(M.PolyQuickTrader, "push_to_server_chan", fake_push)
    trader.last_positions_fetch_error = None
    trader.root = MagicMock()

    asyncio.run(trader.push_trade_result(
        action="快速买入", market_title="Will BTC go up?",
        direction="UP", size=10.0, price=0.5,
        resp={"orderID": "0xabc"},
        market_slug="btc-x",
    ))
    body = pushed["content"]
    assert "当前没有可见持仓" in body
    assert "持仓接口失败" not in body
