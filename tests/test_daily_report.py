import poly_mm_pro_max as mod


def _row(ts, strategy="RED_UP", cycle=1, layer=1, outcome="win",
         pnl="+1.0000", fill_verified="True"):
    return {
        "ts": ts, "strategy": strategy, "cycle": str(cycle), "layer": str(layer),
        "market_slug": "btc-test", "direction": "UP",
        "stake_usdc": "5.0000", "requested_price": "0.5000",
        "fill_price": "0.5000", "fill_size": "10.000000",
        "fill_verified": fill_verified, "outcome": outcome,
        "pnl_estimate": pnl, "accumulated_loss": "0.0000",
    }


def test_aggregate_empty_returns_zeros():
    s = mod.PolyQuickTrader._aggregate_daily_journal([], "2026-05-25")
    assert s["total_rows"] == 0
    assert s["cycle_count"] == 0
    assert s["pnl_estimate_sum"] == 0.0


def test_aggregate_filters_by_date():
    rows = [
        _row("2026-05-25T01:00:00+00:00", cycle=1, outcome="win", pnl="+1.0"),
        _row("2026-05-26T01:00:00+00:00", cycle=2, outcome="loss", pnl="-2.0"),
    ]
    s = mod.PolyQuickTrader._aggregate_daily_journal(rows, "2026-05-25")
    assert s["total_rows"] == 1
    assert s["win_count"] == 1
    assert s["loss_count"] == 0
    assert abs(s["pnl_estimate_sum"] - 1.0) < 1e-9


def test_aggregate_counts_distinct_cycles():
    rows = [
        _row("2026-05-25T01:00:00+00:00", cycle=1, layer=1, outcome="loss", pnl="-5.0"),
        _row("2026-05-25T01:15:00+00:00", cycle=1, layer=2, outcome="win", pnl="+3.0"),
        _row("2026-05-25T02:00:00+00:00", cycle=2, layer=1, outcome="loss", pnl="-5.0"),
    ]
    s = mod.PolyQuickTrader._aggregate_daily_journal(rows, "2026-05-25")
    assert s["cycle_count"] == 2
    assert s["total_rows"] == 3


def test_aggregate_max_consecutive_loss_layers():
    rows = [
        _row("2026-05-25T01:00:00+00:00", cycle=1, layer=1, outcome="loss"),
        _row("2026-05-25T01:15:00+00:00", cycle=1, layer=2, outcome="loss"),
        _row("2026-05-25T01:30:00+00:00", cycle=1, layer=3, outcome="loss"),
        _row("2026-05-25T01:45:00+00:00", cycle=1, layer=4, outcome="win"),
    ]
    s = mod.PolyQuickTrader._aggregate_daily_journal(rows, "2026-05-25")
    assert s["max_consecutive_loss_layers"] == 3


def test_aggregate_counts_timeouts_and_unverified():
    rows = [
        _row("2026-05-25T01:00:00+00:00", cycle=1, outcome="pending_timeout"),
        _row("2026-05-25T02:00:00+00:00", cycle=2, outcome="win",
             fill_verified="False"),
    ]
    s = mod.PolyQuickTrader._aggregate_daily_journal(rows, "2026-05-25")
    assert s["pending_timeout_count"] == 1
    assert s["unverified_fill_count"] == 1
    assert s["anomaly_count"] == 2


def test_aggregate_handles_malformed_pnl():
    rows = [
        _row("2026-05-25T01:00:00+00:00", pnl=""),
        _row("2026-05-25T02:00:00+00:00", pnl="bad"),
        _row("2026-05-25T03:00:00+00:00", pnl="+2.5"),
    ]
    s = mod.PolyQuickTrader._aggregate_daily_journal(rows, "2026-05-25")
    assert abs(s["pnl_estimate_sum"] - 2.5) < 1e-9


def test_aggregate_rejects_nan_and_inf_pnl():
    """Codex Phase 11 V3 warn: NaN/Inf in pnl_estimate must not poison
    the daily P&L sum (NaN + anything = NaN, ditto Inf)."""
    rows = [
        _row("2026-05-25T01:00:00+00:00", pnl="nan"),
        _row("2026-05-25T02:00:00+00:00", pnl="inf"),
        _row("2026-05-25T03:00:00+00:00", pnl="-inf"),
        _row("2026-05-25T04:00:00+00:00", pnl="+3.0"),
    ]
    s = mod.PolyQuickTrader._aggregate_daily_journal(rows, "2026-05-25")
    # Only the +3.0 row should contribute; NaN/Inf rows skipped.
    assert abs(s["pnl_estimate_sum"] - 3.0) < 1e-9
    assert s["total_rows"] == 4  # all 4 rows still counted in total_rows


def test_render_includes_date_and_caveat():
    stats = mod.PolyQuickTrader._aggregate_daily_journal([
        _row("2026-05-25T01:00:00+00:00", pnl="+1.0"),
    ], "2026-05-25")
    body = mod.PolyQuickTrader._render_daily_report_md(stats)
    assert "2026-05-25" in body
    assert "USDC" in body
    assert "pnl_estimate" in body or "估算" in body
    assert body.endswith("\n")
