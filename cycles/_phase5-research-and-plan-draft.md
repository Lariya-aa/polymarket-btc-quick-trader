# Phase 5 调研 + Plan Draft — Cycle Rollback (BLOCKER #4)

**Date:** 2026-05-26
**Status:** Reconnaissance complete; plan draft is literal enough to hand to Kimi.

---

## Part 1 — Codebase reconnaissance

### 1.1 `run_reversal_live_real` layer loop — break paths and state

File: `poly_mm_pro_max.py` lines `1565-1738` (note: original brief cited L1615-1681 against the pre-Phase-4 source; Phase 4 has since rewritten the settle block to L1669-1734, so the layer loop is now L1606-1736).

The `for layer, stake in enumerate(stakes, start=1)` loop body has these mid-cycle exit paths:

| Path | Lines | Risk |
|---|---|---|
| `stop_requested or deadline` → `break` | 1607-1608, 1614-1615 | benign — happens before any buy |
| `market` lookup fails after 3 retries → `break` | 1625-1627 | **orphan risk** if a prior layer is filled |
| `ask > entry_price` → `break` | 1631-1633 | **orphan risk** if prior layer is filled |
| `buy_quick_market` raises (CLOB reject, network, POLY_1271, OrderNotFilled) | 1635 (no try/except) | **orphan risk** — uncaught exception bubbles out of `for` and out of `while`, killing the whole strategy with prior layers still open |
| `outcome == "pending_timeout"` → `return ...` | 1698-1713 | **orphan risk** — exits cycle with the just-bought layer + any prior layers still open |
| `stop_requested` after LOSS → `break` | 1735-1736 | benign — LOSS already burned the position |

`accumulated_loss` is initialized at L1605 (per cycle, outside the `for`), accumulated on LOSS at L1716, and consumed inside WIN pnl at L1675. It resets on next `while` iteration.

**State that must be preserved across the loop to know which positions are open:** for each layer that has reached past L1656 (`live_results.append(row)`) but not yet had a `outcome in {"win","loss"}` resolution, we need to remember `(layer, token_id, fill_size, tick_size, entry_price)`. `live_results` already has `slug` and `entry`, but not `token_id` / `tick_size` / `fill_size` — those live in the `buy_details` dict returned at L1916.

### 1.2 Sell primitives — which to use

- `sell_token_limit(token_id, size, price, tick_size)` at L1947-1967 — **simpler**, takes raw token_id; uses `create_and_post_order` with `Side.SELL`; rejects on `success is False`. **This is the right primitive for rollback** because we already have `token_id` + `tick_size` from `buy_details` and don't need a separate `fetch_positions` round-trip.
- `sell_position_limit(position, size, price)` at L2181-2212 — takes a data-api Position dict, reads `position["asset"]` and `position["orderPriceMinTickSize"]`. Would force us to first call `fetch_positions`, find the asset, then sell. Unnecessary indirection; also susceptible to data-api lag (just-bought position might not appear in `/positions` for several seconds).

### 1.3 `_settle_from_positions` (Phase 4, L2015-2064)

Uses `_fetch_positions_raw` (L1992-2013) which **raises** on HTTP failure (vs `fetch_positions` L1969-1990 which silently returns `[]`). Phase 5's rollback must use `sell_token_limit` directly — we already have the token_id from the in-memory tracking, so we don't need to re-query positions.

### 1.4 `best_bid_ask_for_token` (L1929-1945)

Returns `{"bid": float|None, "ask": float|None, "tick_size": str}`. **Catches all exceptions and returns `{"bid": None, "ask": None, "tick_size": "0.01"}` on error.** This is critical: rollback must handle the `bid is None` case (no liquidity / orderbook fetch failure).

---

## Part 2 — Plan Draft (ai-trio format)

### Why

VIRTUES-PLAN.md BLOCKER #4 (V1 Integrity + V3 Robustness + Safety): When the martingale layer loop aborts mid-cycle, layers 1..N-1 that already filled are abandoned with the user's USDC stuck on the wrong-direction tokens. The martingale's whole point — "double after loss to recover" — is voided because the loss side stays open. Phase 5 must guarantee that any mid-cycle abort flattens all opened positions of the current cycle before exiting.

### Files in scope

- `poly_mm_pro_max.py` — add `cycle_open_positions` accumulator + new `_flatten_cycle_positions` async helper + wrap the `for layer` body in try/except + flatten on `break`-on-market-missing and `break`-on-ask-too-high.
- `tests/test_rollback.py` — NEW. Mock `buy_quick_market`/`sell_token_limit`/`best_bid_ask_for_token` to exercise rollback paths.

### Out of scope (explicit DON'T)

- DO NOT modify `_extract_fill` (Phase 1 territory).
- DO NOT modify `_settle_from_positions` / `_fetch_positions_raw` (Phase 4 territory).
- DO NOT touch any `asyncio.wait_for(asyncio.to_thread(...))` wrapping — that is Phase 2's BLOCKER #3 surface. Phase 5 wraps `sell_token_limit` as-is and acknowledges the timeout-then-double race is still present until Phase 2.
- DO NOT change `buy_quick_market` return shape.
- DO NOT change `run_reversal_live_sim` or `run_reversal_backtest` (paper / backtest do not have real orphan risk).
- DO NOT add fund-circuit-breaker logic (deferred per VIRTUES-PLAN.md until after Phase 4).
- DO NOT change `live_results` row schema (UI render fragility).
- DO NOT introduce a new dependency.

### The change (literal spec)

#### Step 1 — Accumulate open positions per cycle

In `run_reversal_live_real`, right after `accumulated_loss = 0.0` at L1605, add:

```python
            accumulated_loss = 0.0
            cycle_open_positions: list[dict] = []
```

After a successful `buy_quick_market` call at L1635 — i.e. right after `fill_status = buy_details.get("fill_status", "unknown")` at L1639 and before the `if not fill_verified:` log — append the freshly opened position:

```python
                cycle_open_positions.append({
                    "layer": layer,
                    "token_id": buy_details["token_id"],
                    "tick_size": str(buy_details.get("tick_size") or "0.01"),
                    "fill_size": float(buy_details["size"]),
                    "entry": float(buy_details["price"]),
                })
```

When a layer resolves to `win` or `loss` (i.e. after the WIN `break` at L1697 and after the LOSS path's `cycle_pnl_running += pnl` at L1734), remove the just-resolved entry:

```python
                cycle_open_positions = [p for p in cycle_open_positions if p["layer"] != layer]
```

This list represents **currently-open, unresolved real positions** in the cycle.

#### Step 2 — `_flatten_cycle_positions` helper

Insert directly after `_settle_from_positions` (after L2064). Implementation:

```python
    async def _flatten_cycle_positions(self, open_positions: list[dict], reason: str, cycle_count: int):
        """
        Best-effort flatten of every position opened in the current reversal cycle
        that has not yet resolved. Used when the layer loop aborts mid-cycle so
        the user is not left holding wrong-direction bags.

        For each open position: read best_bid_ask_for_token; pick a marketable
        sell price (0.95 * best_bid for slippage tolerance), clamp to tick;
        submit sell_token_limit. Log each step. Push one Server酱 critical
        summary at the end.
        """
        results = []
        for pos in open_positions:
            token_id = pos["token_id"]
            try:
                book = await self.best_bid_ask_for_token(token_id)
                bid = book.get("bid")
                tick_size = book.get("tick_size") or pos["tick_size"]
                if bid is None or bid <= 0:
                    sell_price = self.clamp_price(0.01, tick_size)
                    self.log_live(logging.WARNING, "rollback layer %s 无 bid，使用最低价 %.4f", pos["layer"], sell_price)
                else:
                    sell_price = self.clamp_price(bid * 0.95, tick_size)
                resp = await self.sell_token_limit(token_id, pos["fill_size"], sell_price, tick_size)
                self.log_live(logging.WARNING, "rollback layer %s 卖出: size=%.4f price=%.4f resp=%s", pos["layer"], pos["fill_size"], sell_price, str(resp)[:120])
                results.append({"layer": pos["layer"], "ok": True, "price": sell_price, "size": pos["fill_size"]})
            except Exception as e:
                self.log_live(logging.ERROR, "rollback layer %s 卖出失败: %s", pos["layer"], e)
                results.append({"layer": pos["layer"], "ok": False, "error": str(e)[:200], "token_id": token_id[:12]})
        try:
            lines = [f"- layer {r['layer']}: {'OK ' + format(r['price'], '.4f') + ' x ' + format(r['size'], '.4f') if r['ok'] else 'FAIL ' + r.get('error', '')}" for r in results]
            await self.push_to_server_chan(
                "Polymarket 反转实盘 ⚠️ 周期回滚",
                f"### ⚠️ 反转实盘周期中断回滚\n\n- 周期: `{cycle_count}`\n- 原因: `{reason}`\n- 回滚结果:\n" + "\n".join(lines) + "\n\n请去 polymarket.com/portfolio 人工对账。",
            )
        except Exception as e:
            self.logger.error("rollback Server酱 推送失败: %s", e)
        return results
```

#### Step 3 — Wrap layer loop body + call flatten on aborts

Wrap the existing for-loop body in `try`. On the two `break` paths that risk orphans (market-not-found L1627, ask-too-high L1633) and on any uncaught exception, call `_flatten_cycle_positions(cycle_open_positions, reason, cycle_count)` then break out of the for-loop. Concretely:

- At L1625-1627 change `break` to: `await self._flatten_cycle_positions(cycle_open_positions, f"market_not_found:{target_slug}", cycle_count); break`
- At L1631-1633 change `break` to: `await self._flatten_cycle_positions(cycle_open_positions, f"ask_too_high:{ask:.4f}>{config['entry_price']:.4f}", cycle_count); break`
- Wrap the body L1609-1736 in `try/except Exception as exc: await self._flatten_cycle_positions(cycle_open_positions, f"layer_exception:{type(exc).__name__}:{str(exc)[:120]}", cycle_count); raise` — re-raise so the outer `while` does not silently continue with corrupted state.
- For `pending_timeout` at L1698-1713: before the `return`, also call flatten. The just-bought layer's token is in `cycle_open_positions`. Reason: `f"pending_timeout:layer{layer}"`.

#### Step 4 — Tests (`tests/test_rollback.py`)

Mock `sell_token_limit` + `best_bid_ask_for_token` + `push_to_server_chan` on a `PolyQuickTrader.__new__` instance. 5 cases:

1. `bid=0.40` → `sell_token_limit` called with `price≈0.38` (0.95×bid clamped).
2. `bid is None` → sell at clamped `0.01` floor.
3. 2 layers, first sell raises, second succeeds → result list has both (one fail, one ok); loop did not abort.
4. `push_to_server_chan` called once with both layer numbers in body.
5. `bid=0.371 tick=0.01` → sell price aligned to 0.01 grid.

### Verification commands

```bash
grep -n "_flatten_cycle_positions" poly_mm_pro_max.py     # >= 5 (def + 4 call sites)
grep -n "cycle_open_positions" poly_mm_pro_max.py         # >= 4 (init, append, remove×2, pass to flatten)
.venv/bin/python -m pytest tests/test_rollback.py -v       # 5 passed
.venv/bin/python -m pytest tests/ -v                       # 37 + 5 = 42 passed
.venv/bin/python -c "import poly_mm_pro_max; print('ok')"
```

### Live verification owed (user)

Stub: monkeypatch `fetch_market_by_slug` to return `None` on the 2nd layer call after a real layer-1 buy. Run reversal live with 0.5 USDC stake. Expect:
1. Layer 1 buys (real fill).
2. Layer 2 market-lookup fails → log `rollback layer 1 卖出: ...`.
3. Server酱 push `⚠️ 周期回滚`.
4. polymarket.com/portfolio: position flat after a minute (or sell order resting at marketable price if not immediately matched).

---

## Part 3 — Risks + open questions

1. **Phase 2 race still uncovered.** `sell_token_limit` (L1955) still uses `asyncio.wait_for(asyncio.to_thread(create_and_post_order))` — same race as BLOCKER #3. A rollback sell that times out client-side but actually fills server-side could, on the next cycle iteration, double-handle the position. Documented in code comment + user handoff; unfixed until Phase 2.

2. **`bid × 0.95` may clamp to zero.** On thin markets, `0.95 × bid` could fall below the smallest non-zero tick → `clamp_price` returns `0.0` → order rejected. Mitigation: floor with `max(sell_price, float(tick_size))` before submit. Plan covers the `bid is None` case with a `0.01` floor; same logic must apply when `0.95 * bid < tick`.

3. **`pending_timeout` is settlement-stuck, not exposure-stuck.** When `_settle_from_positions` returns `pending_timeout`, the market hasn't resolved — selling aborts a still-valid bet that might have won. **Recommendation: still flatten** since the user has given up on the cycle (deadline exceeded) and orphan exposure is worse than slippage on a possibly-winning position. Flag in handoff.

4. **`try/finally` for KeyboardInterrupt / OOM?** Plan uses `try/except Exception` which does NOT catch `KeyboardInterrupt`/`SystemExit`. `try/finally` would also run on normal LOSS-continue paths unless we track a flag. **Recommendation: stick with `try/except Exception` for v1**; Ctrl-C orphan rescue is a Tk-shutdown-hook concern, out of Phase 5 scope.

5. **Plan literal enough — no scope reduction recommended.** 4 call-site edits + 1 helper + 1 accumulator, all unambiguous against current source.
