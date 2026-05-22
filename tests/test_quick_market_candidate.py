"""Tests for quick_market_candidate filtering + period detection.

quick_market_candidate is the gate that decides what shows up in the
"BTC short-cycle markets" table. A bug here either hides legit markets
or admits non-BTC ones to the buy path.
"""

from datetime import datetime, timezone

import pytest


def _event(**overrides):
    base = {
        "slug": "btc-updown-5m-1716000000",
        "title": "Bitcoin Up or Down — 5m",
        "endDate": "2099-01-01T00:00:00Z",
        "markets": [],
    }
    base.update(overrides)
    return base


def _market(**overrides):
    base = {
        "question": "Will Bitcoin go up or down in the next 5 minutes?",
        "slug": "btc-updown-5m-1716000000",
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "clobTokenIds": '["yes-token-id", "no-token-id"]',
        "bestBid": "0.45",
        "bestAsk": "0.55",
        "orderPriceMinTickSize": "0.01",
        "volume24hrClob": "100000",
        "endDate": "2099-01-01T00:00:00Z",
    }
    base.update(overrides)
    return base


_NOW = datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)


def test_happy_path_returns_quick_market(bag):
    candidate = bag.quick_market_candidate(_event(), _market(), _NOW)
    assert candidate is not None
    assert candidate.up_bid == 0.45
    assert candidate.up_ask == 0.55
    # Down side is derived from 1 - ask / 1 - bid:
    assert candidate.down_bid == pytest.approx(0.45)  # 1 - 0.55
    assert candidate.down_ask == pytest.approx(0.55)  # 1 - 0.45
    assert candidate.spread == pytest.approx(0.10)
    assert candidate.period == "5m"


def test_closed_market_filtered(bag):
    assert bag.quick_market_candidate(_event(), _market(closed=True), _NOW) is None


def test_inactive_market_filtered(bag):
    assert bag.quick_market_candidate(_event(), _market(active=False), _NOW) is None


def test_not_accepting_orders_filtered(bag):
    assert bag.quick_market_candidate(_event(), _market(acceptingOrders=False), _NOW) is None


def test_missing_token_ids_filtered(bag):
    assert bag.quick_market_candidate(_event(), _market(clobTokenIds=None), _NOW) is None
    assert bag.quick_market_candidate(_event(), _market(clobTokenIds="[]"), _NOW) is None


def test_non_btc_question_filtered(bag):
    assert bag.quick_market_candidate(
        _event(slug="eth-something"),
        _market(question="Will Ethereum go up or down?", slug="eth-updown-5m"),
        _NOW,
    ) is None


def test_question_missing_up_or_down_filtered(bag):
    assert bag.quick_market_candidate(
        _event(),
        _market(question="Will Bitcoin moon today?"),
        _NOW,
    ) is None


def test_invalid_bid_ask_filtered(bag):
    # bid >= ask should reject
    assert bag.quick_market_candidate(_event(), _market(bestBid="0.6", bestAsk="0.5"), _NOW) is None
    # bid <= 0 should reject
    assert bag.quick_market_candidate(_event(), _market(bestBid="0"), _NOW) is None
    # ask >= 1 should reject
    assert bag.quick_market_candidate(_event(), _market(bestAsk="1.0"), _NOW) is None


def test_past_end_date_marks_ended(bag):
    candidate = bag.quick_market_candidate(
        _event(),
        _market(endDate="2020-01-01T00:00:00Z"),
        _NOW,
    )
    assert candidate is not None
    assert candidate.ended is True


def test_period_from_slug(bag):
    assert bag.quick_period_from_slug_or_title("btc-updown-15m-1716000000", "") == "15m"
    assert bag.quick_period_from_slug_or_title("btc-updown-1h-1716000000", "") == "1h"
    assert bag.quick_period_from_slug_or_title("btc-updown-4h-1716000000", "") == "4h"


def test_period_falls_back_to_question(bag):
    assert bag.quick_period_from_slug_or_title("some-other-slug", "Bitcoin 5 minute up down") == "5m"
    assert bag.quick_period_from_slug_or_title("some-other-slug", "BTC 15 min up or down") == "15m"


def test_period_unknown_returns_question_mark(bag):
    assert bag.quick_period_from_slug_or_title("some-slug", "completely unrelated") == "?"
