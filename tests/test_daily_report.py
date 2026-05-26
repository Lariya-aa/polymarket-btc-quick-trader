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
    # Per-cycle aggregation: each row a distinct cycle so each contributes
    # (or doesn't) independently. Same-cycle multiple WIN rows is not a
    # real shape — a cycle WINs once then breaks.
    rows = [
        _row("2026-05-25T01:00:00+00:00", cycle=1, pnl=""),
        _row("2026-05-25T02:00:00+00:00", cycle=2, pnl="bad"),
        _row("2026-05-25T03:00:00+00:00", cycle=3, pnl="+2.5"),
    ]
    s = mod.PolyQuickTrader._aggregate_daily_journal(rows, "2026-05-25")
    assert abs(s["pnl_estimate_sum"] - 2.5) < 1e-9


def test_aggregate_rejects_nan_and_inf_pnl():
    """Codex Phase 11 V3 warn: NaN/Inf in pnl_estimate must not poison
    the daily P&L sum (NaN + anything = NaN, ditto Inf)."""
    # Distinct cycles per row so per-cycle aggregation includes each.
    rows = [
        _row("2026-05-25T01:00:00+00:00", cycle=1, pnl="nan"),
        _row("2026-05-25T02:00:00+00:00", cycle=2, pnl="inf"),
        _row("2026-05-25T03:00:00+00:00", cycle=3, pnl="-inf"),
        _row("2026-05-25T04:00:00+00:00", cycle=4, pnl="+3.0"),
    ]
    s = mod.PolyQuickTrader._aggregate_daily_journal(rows, "2026-05-25")
    # Only the +3.0 row should contribute; NaN/Inf rows skipped.
    assert abs(s["pnl_estimate_sum"] - 3.0) < 1e-9
    assert s["total_rows"] == 4


def test_aggregate_does_not_double_count_late_win_cycle():
    """Final Codex P2 BLOCKER: trade_journal writes one row per layer.
    WIN row's pnl_estimate already subtracts accumulated_loss. Summing
    all rows of a lose-then-win cycle double-counted prior losses.
    Aggregator must per-cycle dedup: WIN row's pnl IS the net cycle PnL."""
    rows = [
        # cycle 1: layer 1 loss (-5.5), layer 2 win (gross 8.478 net 2.978
        # after subtracting accumulated_loss 5.5). Net cycle PnL = +2.978.
        _row("2026-05-25T01:00:00+00:00", cycle=1, layer=1, outcome="loss", pnl="-5.5"),
        _row("2026-05-25T01:15:00+00:00", cycle=1, layer=2, outcome="win", pnl="+2.978"),
    ]
    s = mod.PolyQuickTrader._aggregate_daily_journal(rows, "2026-05-25")
    assert abs(s["pnl_estimate_sum"] - 2.978) < 1e-3  # NOT -2.522


def test_aggregate_sums_all_losses_when_no_win_in_cycle():
    """LOSS-only cycle: each row is just that layer's loss, no double-
    count. Sum all rows to get total loss."""
    rows = [
        _row("2026-05-25T01:00:00+00:00", cycle=1, layer=1, outcome="loss", pnl="-5.0"),
        _row("2026-05-25T01:15:00+00:00", cycle=1, layer=2, outcome="loss", pnl="-10.0"),
        _row("2026-05-25T01:30:00+00:00", cycle=1, layer=3, outcome="loss", pnl="-20.0"),
    ]
    s = mod.PolyQuickTrader._aggregate_daily_journal(rows, "2026-05-25")
    assert abs(s["pnl_estimate_sum"] - (-35.0)) < 1e-9


def test_render_includes_date_and_caveat():
    stats = mod.PolyQuickTrader._aggregate_daily_journal([
        _row("2026-05-25T01:00:00+00:00", pnl="+1.0"),
    ], "2026-05-25")
    body = mod.PolyQuickTrader._render_daily_report_md(stats)
    assert "2026-05-25" in body
    assert "USDC" in body
    assert "pnl_estimate" in body or "估算" in body
    assert body.endswith("\n")
