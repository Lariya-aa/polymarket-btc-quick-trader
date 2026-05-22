"""Tests for compact_signal and the generated_btc_updown_slugs fallback.

compact_signal is the MiniMax input — if a key is missing the LLM gets
garbage. generated_btc_updown_slugs is the HTML-scrape safety net.
"""


def test_compact_signal_has_all_required_keys(bag):
    out = bag.compact_signal({
        "market_period": "5m",
        "horizon_minutes": 5,
        "price": 65432.123,
        "prob_up": 0.5234,
        "prob_down": 0.4766,
        "confidence": 0.0469,
        "ret_fast": 0.001234,
        "ret_mid": -0.005678,
        "ret_slow": 0.000123,
        "rsi": 53.456,
        "vol": 0.00123456,
    })
    expected_keys = {
        "period", "horizon_min", "price",
        "p_up", "p_down", "confidence",
        "r_fast", "r_mid", "r_slow",
        "rsi", "vol",
    }
    assert set(out.keys()) == expected_keys


def test_compact_signal_rounds(bag):
    out = bag.compact_signal({
        "price": 65432.123456,
        "prob_up": 0.5234567,
        "prob_down": 0.4765433,
        "confidence": 0.04691234,
        "ret_fast": 0.0012345,
        "ret_mid": -0.00567843,
        "ret_slow": 0.00012345,
        "rsi": 53.4567,
        "vol": 0.001234567,
        "market_period": "5m",
        "horizon_minutes": 5,
    })
    assert out["price"] == 65432.12
    assert out["p_up"] == 0.5235
    assert out["rsi"] == 53.46


def test_compact_signal_tolerates_missing_fields(bag):
    # Must not raise on a partial dict — production code calls this from a
    # path where the LLM step may be skipped and a stub signal is provided.
    out = bag.compact_signal({})
    assert out["p_up"] == 0.5
    assert out["p_down"] == 0.5
    assert out["rsi"] == 50.0


def test_generated_slugs_cover_all_five_periods(bag):
    slugs = bag.generated_btc_updown_slugs()
    periods_found = {s.split("-")[2] for s in slugs}
    assert periods_found == {"5m", "15m", "1h", "4h", "1d"}


def test_generated_slugs_have_five_offsets_per_period(bag):
    slugs = bag.generated_btc_updown_slugs()
    # 5 periods × 5 offsets (−2 .. +2) = 25 slugs.
    assert len(slugs) == 25


def test_generated_slugs_have_correct_prefix(bag):
    for slug in bag.generated_btc_updown_slugs():
        assert slug.startswith("btc-updown-")
        # Format: btc-updown-<period>-<unix_ts>
        parts = slug.split("-")
        assert len(parts) == 4
        assert parts[3].isdigit()
