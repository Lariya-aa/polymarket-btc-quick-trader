"""Tests for the multi-category expansion:

- CATEGORIES registry shape
- fetch_tag_markets and fetch_newly_listed_markets (mocked aiohttp)
- _build_market category propagation
- _local_signal_for dispatch by category

We don't run the real HTTP — instead we monkeypatch `fetch_json` (the one
async helper both fetchers funnel through) to return canned payloads.
"""

import asyncio
import inspect
from datetime import datetime, timezone

import pytest

import poly_mm_pro_max as M


# ── CATEGORIES registry shape ─────────────────────────────────────────────


def test_categories_registry_has_required_entries():
    labels = list(M.CATEGORIES.keys())
    # First entry must be BTC (Combobox default).
    assert labels[0].startswith("BTC")
    # All five user-requested categories must be present.
    needed = {"BTC", "NBA", "NFL", "WC", "NEW"}
    found = {entry[0] for entry in M.CATEGORIES.values()}
    assert needed.issubset(found), f"missing categories: {needed - found}"


def test_categories_each_entry_is_dispatchable_shape():
    for label, entry in M.CATEGORIES.items():
        assert isinstance(entry, tuple) and len(entry) == 3, label
        code, method_name, kwargs = entry
        assert isinstance(code, str) and code, label
        assert isinstance(method_name, str) and method_name, label
        assert isinstance(kwargs, dict), label
        # Method must exist on PolyQuickTrader and be async.
        method = getattr(M.PolyQuickTrader, method_name, None)
        assert method is not None, f"{label}: method {method_name} not found"
        assert inspect.iscoroutinefunction(method), f"{label}: {method_name} must be async"


# ── _build_market category propagation ────────────────────────────────────


def _basic_event_market():
    event = {"slug": "evt-x", "title": "X event", "endDate": "2099-01-01T00:00:00Z"}
    market = {
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "clobTokenIds": '["yes-id", "no-id"]',
        "outcomes": '["Yes", "No"]',
        "bestBid": "0.4",
        "bestAsk": "0.6",
        "endDate": "2099-01-01T00:00:00Z",
        "orderPriceMinTickSize": "0.01",
        "question": "Will X happen?",
        "slug": "evt-x",
    }
    return event, market


def test_build_market_propagates_category_and_subject(bag):
    event, market = _basic_event_market()
    out = bag._build_market(event, market, datetime(2026, 5, 23, tzinfo=timezone.utc),
                            category="NBA", subject="Lakers")
    assert out is not None
    assert out.category == "NBA"
    assert out.subject == "Lakers"
    # BTC-only field: period stays empty for non-BTC categories.
    assert out.period == ""


def test_build_market_btc_fills_period(bag):
    event = {"slug": "btc-updown-5m-1700000000", "title": "BTC Up/Down 5m",
             "endDate": "2099-01-01T00:00:00Z"}
    market = {
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "clobTokenIds": '["yes-id", "no-id"]',
        "outcomes": '["Yes", "No"]',
        "bestBid": "0.45",
        "bestAsk": "0.55",
        "slug": "btc-updown-5m-1700000000",
        "question": "Will Bitcoin go up or down in 5 minutes?",
        "endDate": "2099-01-01T00:00:00Z",
    }
    out = bag._build_market(event, market, datetime(2026, 5, 23, tzinfo=timezone.utc),
                            category="BTC", subject="5m")
    assert out is not None
    assert out.category == "BTC"
    assert out.period == "5m"


# ── fetch_tag_markets ─────────────────────────────────────────────────────


def _stub_events_response(events_list):
    """Replace PolyQuickTrader.fetch_json with an async stub returning the
    given list. Returns the stub so tests can inspect the URL/params it
    was called with."""
    calls = []

    async def fake_fetch_json(self, url, params=None, quiet_404=False):
        calls.append({"url": url, "params": params, "quiet_404": quiet_404})
        return events_list

    return fake_fetch_json, calls


def test_fetch_tag_markets_passes_tag_slug_to_gamma(bag, monkeypatch):
    events = [
        {
            "slug": "nba-event-1",
            "title": "Lakers vs Warriors",
            "endDate": "2099-01-01T00:00:00Z",
            "markets": [
                {
                    "active": True, "closed": False, "acceptingOrders": True,
                    "clobTokenIds": '["yes", "no"]',
                    "outcomes": '["Yes", "No"]',
                    "bestBid": "0.3", "bestAsk": "0.4",
                    "endDate": "2099-01-01T00:00:00Z",
                    "question": "Will Lakers win?", "slug": "nba-event-1",
                }
            ],
        }
    ]
    fake, calls = _stub_events_response(events)
    monkeypatch.setattr(M.PolyQuickTrader, "fetch_json", fake)

    markets = asyncio.run(bag.fetch_tag_markets("nba", "NBA", subject_label="NBA"))
    assert len(markets) == 1
    assert markets[0].category == "NBA"
    assert markets[0].subject == "NBA"
    assert markets[0].yes_bid == 0.3
    # Confirms we hit the right endpoint with the right filter.
    assert calls[0]["url"].endswith("/events")
    assert calls[0]["params"]["tag_slug"] == "nba"
    assert calls[0]["params"]["closed"] == "false"


def test_fetch_tag_markets_handles_non_list_response(bag, monkeypatch):
    # API hiccup: returns None or a dict, not a list. Must not crash.
    async def fake_fetch_json(self, url, params=None, quiet_404=False):
        return None
    monkeypatch.setattr(M.PolyQuickTrader, "fetch_json", fake_fetch_json)
    assert asyncio.run(bag.fetch_tag_markets("nba", "NBA")) == []


def test_fetch_tag_markets_filters_closed_markets(bag, monkeypatch):
    events = [
        {
            "slug": "closed-evt", "title": "X", "endDate": "2099-01-01T00:00:00Z",
            "markets": [
                {
                    "active": True, "closed": True, "acceptingOrders": True,
                    "clobTokenIds": '["a","b"]',
                    "outcomes": '["Yes", "No"]',
                    "bestBid": "0.4", "bestAsk": "0.5",
                    "endDate": "2099-01-01T00:00:00Z",
                    "question": "x?", "slug": "closed-evt",
                }
            ],
        }
    ]
    fake, _ = _stub_events_response(events)
    monkeypatch.setattr(M.PolyQuickTrader, "fetch_json", fake)
    assert asyncio.run(bag.fetch_tag_markets("nba", "NBA")) == []


# ── fetch_newly_listed_markets ────────────────────────────────────────────


def test_fetch_newly_listed_applies_volume_floor(bag, monkeypatch):
    events = [
        {
            "slug": "dust", "title": "no liquidity", "volume24hr": 0,
            "endDate": "2099-01-01T00:00:00Z",
            "markets": [
                {"active": True, "closed": False, "acceptingOrders": True,
                 "clobTokenIds": '["a","b"]', "outcomes": '["Yes","No"]',
                 "bestBid": "0.4", "bestAsk": "0.5",
                 "endDate": "2099-01-01T00:00:00Z", "question": "?", "slug": "dust"}
            ],
        },
        {
            "slug": "real", "title": "real one", "volume24hr": 5000,
            "endDate": "2099-01-01T00:00:00Z",
            "markets": [
                {"active": True, "closed": False, "acceptingOrders": True,
                 "clobTokenIds": '["a","b"]', "outcomes": '["Yes","No"]',
                 "bestBid": "0.4", "bestAsk": "0.5",
                 "endDate": "2099-01-01T00:00:00Z", "question": "?", "slug": "real"}
            ],
        },
    ]
    fake, calls = _stub_events_response(events)
    monkeypatch.setattr(M.PolyQuickTrader, "fetch_json", fake)
    markets = asyncio.run(bag.fetch_newly_listed_markets(limit=10, min_volume_24h=100.0))
    assert len(markets) == 1
    assert markets[0].slug == "real"
    assert markets[0].category == "NEW"
    # subject hint = first hyphenated token of event slug, uppercased, max 6 chars.
    assert markets[0].subject == "REAL"


def test_fetch_newly_listed_passes_createdAt_sort(bag, monkeypatch):
    fake, calls = _stub_events_response([])
    monkeypatch.setattr(M.PolyQuickTrader, "fetch_json", fake)
    asyncio.run(bag.fetch_newly_listed_markets())
    assert calls[0]["params"]["order"] == "createdAt"
    assert calls[0]["params"]["ascending"] == "false"


# ── _local_signal_for dispatch ────────────────────────────────────────────


def _make_market(category):
    return M.PolyMarket(
        slug="x", event_slug="x", question="Q?",
        yes_id="y", no_id="n", tick_size="0.01",
        period="", end_dt=None, ended=False,
        yes_bid=0.3, yes_ask=0.4, no_bid=0.6, no_ask=0.7,
        spread=0.1, volume24h=100.0,
        category=category, subject=category,
    )


def test_local_signal_for_non_btc_returns_market_shell(bag):
    m = _make_market("NBA")
    sig = asyncio.run(bag._local_signal_for(m))
    assert sig["category"] == "NBA"
    assert sig["yes_bid"] == 0.3
    assert sig["no_ask"] == 0.7
    # No probability prediction from local side for non-BTC.
    assert "prob_up" not in sig
    assert "rsi" not in sig


def test_local_signal_for_no_market_defaults_to_btc(bag, monkeypatch):
    # When no market is selected, we go down the BTC path. fetch_btc_signal
    # would hit Binance; stub it.
    async def fake_btc(self, m):
        return {"prob_up": 0.5, "category": "BTC", "fetched_at": "00:00:00"}
    monkeypatch.setattr(M.PolyQuickTrader, "fetch_btc_signal", fake_btc)
    sig = asyncio.run(bag._local_signal_for(None))
    assert sig["category"] == "BTC"
