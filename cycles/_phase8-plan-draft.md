# Plan: Phase 8 — Pure-function regression tests (V6 Test-Driven Diligence)

## Why

VIRTUES-PLAN.md Phase 8 / V6: the existing test suite (`tests/test_pure.py`, `test_lock.py`, `test_settle.py`, `test_observability.py`, `test_retry_post.py`, `test_rollback.py` = 42 tests) covers `_extract_fill`, `_float_or_zero`, lock semantics, settlement state machine, and rollback. But the math primitives that drive **martingale stake sizing, P&L estimation, K-line classification, signal indicators, and price clamping** have no direct regression coverage. A typo in `reversal_factors` (sign flip) or `reversal_stakes` (off-by-one in `accumulated_loss` update) would silently corrupt live trading and only be caught by full end-to-end replay. Phase 8 locks each formula with deterministic inputs whose outputs were verified by hand (or by the in-code docstring `5 -> 10.36 -> 21.48`).

## Files in scope (whitelist)

- `tests/test_helpers.py` — **NEW**. ~14 test functions covering 8 pure helpers.

## Out of scope (explicit DON'T)

- DO NOT modify `poly_mm_pro_max.py` — Phase 8 is tests only. If a test reveals a bug in the source, write `BLOCKED ON: …` note and stop, do not fix in this cycle.
- DO NOT mock `fetch_positions`, `fetch_btc_15m_klines`, `buy_quick_market`, or any async I/O — those belong to Phase 4/5/9 test files.
- DO NOT touch existing test files (`test_pure.py`, `test_lock.py`, `test_settle.py`, `test_observability.py`, `test_retry_post.py`, `test_rollback.py`) — additive only.
- DO NOT introduce `pytest.fixture` parametrization beyond `pytest.mark.parametrize` (the existing tests use plain `def test_...`; match the style).
- DO NOT add `numpy`/`scipy` deps — compute reference values inline with stdlib math.
- DO NOT change `pytest.ini` configuration.
- DO NOT assert against tolerances looser than `1e-6` for stake/factor computations (these are pure float math; 1e-9 is achievable for the canonical case but 1e-6 is a safe ceiling).
- DO NOT introduce property-based tests (`hypothesis`) — out of scope.

## Helpers under test (with file anchors)

| Helper | Method | Line ~ | Pure? |
|---|---|---|---|
| `reversal_factors(entry, fee)` | method on `PolyQuickTrader` | 1256 | yes (no `self.` use) |
| `reversal_stakes(initial, entry, max_layers, fee)` | method | 1261 | yes |
| `matching_streak(colors, target)` | method | 1172 | yes |
| `kline_color(row)` | method | 1272 | yes |
| `price_decimals(tick_size)` | method | 2421 | yes |
| `clamp_price(price, tick_size)` | method | 2426 | yes (uses `price_decimals`) |
| `ema(values, period)` | method | 2404 | yes |
| `rsi(values, period)` | method | 2411 | yes |

All eight read only their arguments — no `self.*` attribute access. Tests invoke via the existing pattern:
```python
inst = mod.PolyQuickTrader.__new__(mod.PolyQuickTrader)
mod.PolyQuickTrader.reversal_factors(inst, 0.5, 0.07)
```
(matches `tests/test_settle.py` / `test_observability.py` style.)

## The change (literal spec)

### Create `tests/test_helpers.py`

```python
"""Regression tests for pure math helpers in poly_mm_pro_max.

Strategy: each helper gets at least one "canonical" test pinning the formula
to a hand-computed value, plus edge cases (empty input, boundary values,
sign flips). A future typo in any of these formulas must fail at least one
test.
"""
import math

import pytest

import poly_mm_pro_max as mod


def _inst():
    return mod.PolyQuickTrader.__new__(mod.PolyQuickTrader)


# ---------------------------------------------------------------- reversal_factors

def test_reversal_factors_canonical_entry_half_fee_007():
    """Reference: docstring/UI text 5 -> 10.36 -> 21.48 uses entry=0.5, fee=0.07."""
    wf, lf = mod.PolyQuickTrader.reversal_factors(_inst(), 0.5, 0.07)
    # wf = 1/0.5 - 1 - 0.07*0.5 = 2 - 1 - 0.035 = 0.965
    # lf = 1 + 0.07*0.5 = 1.035
    assert abs(wf - 0.965) < 1e-9
    assert abs(lf - 1.035) < 1e-9


def test_reversal_factors_zero_fee_yields_inverse_minus_one():
    wf, lf = mod.PolyQuickTrader.reversal_factors(_inst(), 0.4, 0.0)
    # wf = 1/0.4 - 1 = 1.5; lf = 1.0
    assert abs(wf - 1.5) < 1e-9
    assert abs(lf - 1.0) < 1e-9


def test_reversal_factors_entry_one_means_no_payout():
    # entry=1.0: 1/1 - 1 - fee*0 = 0; lf = 1.0
    wf, lf = mod.PolyQuickTrader.reversal_factors(_inst(), 1.0, 0.07)
    assert wf == 0.0
    assert lf == 1.0


# ---------------------------------------------------------------- reversal_stakes

def test_reversal_stakes_canonical_5_then_10_36_then_21_48():
    """Locks the docstring example `5 -> 10.36 -> 21.48` (poly_mm_pro_max.py L453)."""
    stakes, wf, lf, target = mod.PolyQuickTrader.reversal_stakes(
        _inst(), 5.0, 0.5, 3, 0.07
    )
    assert len(stakes) == 3
    # Hand-computed:
    # wf=0.965, lf=1.035, target = 0.965*5 = 4.825
    # s1 = (0 + 4.825) / 0.965 = 5.0
    # acc1 = 1.035 * 5.0 = 5.175
    # s2 = (5.175 + 4.825) / 0.965 = 10.36269430...
    # acc2 = 5.175 + 1.035 * 10.36269 = 15.90039...
    # s3 = (15.90039 + 4.825) / 0.965 = 21.47710...
    assert abs(stakes[0] - 5.0) < 1e-9
    assert abs(stakes[1] - 10.362694300518135) < 1e-6
    assert abs(stakes[2] - 21.477103589430083) < 1e-6
    assert abs(wf - 0.965) < 1e-9
    assert abs(lf - 1.035) < 1e-9
    assert abs(target - 4.825) < 1e-9


def test_reversal_stakes_monotonic_increase():
    stakes, *_ = mod.PolyQuickTrader.reversal_stakes(_inst(), 5.0, 0.5, 5, 0.07)
    for a, b in zip(stakes, stakes[1:]):
        assert b > a, f"martingale must be strictly increasing, got {stakes}"


def test_reversal_stakes_max_layers_zero_returns_empty():
    stakes, wf, lf, target = mod.PolyQuickTrader.reversal_stakes(
        _inst(), 5.0, 0.5, 0, 0.07
    )
    assert stakes == []


def test_reversal_stakes_recoups_accumulated_loss_when_layer_n_wins():
    """If layer N WINs at fill=entry, net cycle P&L must equal target_profit."""
    stakes, wf, lf, target = mod.PolyQuickTrader.reversal_stakes(
        _inst(), 5.0, 0.5, 4, 0.07
    )
    accumulated = sum(lf * s for s in stakes[:-1])
    size = stakes[-1] / 0.5  # buy_quick_market shares for stake at entry=0.5
    pnl = (1.0 - 0.5) * size - accumulated
    assert abs(pnl - target) < 1e-6


# ---------------------------------------------------------------- matching_streak

def test_matching_streak_all_same():
    assert mod.PolyQuickTrader.matching_streak(_inst(), ["R", "R", "R"], "R") == 3


def test_matching_streak_breaks_on_first_mismatch_from_end():
    assert mod.PolyQuickTrader.matching_streak(_inst(), ["G", "R", "R", "R"], "R") == 3
    assert mod.PolyQuickTrader.matching_streak(_inst(), ["R", "R", "R", "G"], "R") == 0


def test_matching_streak_empty():
    assert mod.PolyQuickTrader.matching_streak(_inst(), [], "R") == 0


def test_matching_streak_doji_doesnt_count():
    # 'D' (doji) is its own color; it breaks an R-streak.
    assert mod.PolyQuickTrader.matching_streak(_inst(), ["R", "R", "D", "R"], "R") == 1


# ---------------------------------------------------------------- kline_color

def test_kline_color_red_when_close_below_open():
    row = [0, "50000", "50100", "49900", "49950"]  # ts, open, high, low, close
    assert mod.PolyQuickTrader.kline_color(_inst(), row) == "R"


def test_kline_color_green_when_close_above_open():
    row = [0, "50000", "50200", "49900", "50100"]
    assert mod.PolyQuickTrader.kline_color(_inst(), row) == "G"


def test_kline_color_doji_when_close_equals_open():
    row = [0, "50000", "50100", "49900", "50000"]
    assert mod.PolyQuickTrader.kline_color(_inst(), row) == "D"


# ---------------------------------------------------------------- price_decimals

@pytest.mark.parametrize("tick,expected", [
    ("0.01", 2),
    ("0.001", 3),
    ("0.0001", 4),
    ("1", 0),
    ("0.1", 1),
])
def test_price_decimals_canonical(tick, expected):
    assert mod.PolyQuickTrader.price_decimals(_inst(), tick) == expected


# ---------------------------------------------------------------- clamp_price

def test_clamp_price_inside_range_rounds_to_tick():
    # tick=0.01 → decimals=2; price=0.4567 → 0.46
    assert mod.PolyQuickTrader.clamp_price(_inst(), 0.4567, "0.01") == 0.46


def test_clamp_price_clamps_above_one_minus_tick():
    # tick=0.01 → max allowed = 1 - 0.01 = 0.99
    assert mod.PolyQuickTrader.clamp_price(_inst(), 0.999, "0.01") == 0.99


def test_clamp_price_clamps_below_tick():
    # tick=0.01 → min allowed = 0.01
    assert mod.PolyQuickTrader.clamp_price(_inst(), 0.001, "0.01") == 0.01


def test_clamp_price_finer_tick_preserves_precision():
    # tick=0.0001 → 4 decimals
    assert mod.PolyQuickTrader.clamp_price(_inst(), 0.45678, "0.0001") == 0.4568


# ---------------------------------------------------------------- ema

def test_ema_constant_input_returns_constant():
    inst = _inst()
    values = [50.0] * 30
    assert abs(mod.PolyQuickTrader.ema(inst, values, 10) - 50.0) < 1e-9


def test_ema_monotonic_ramp_lags_below_latest():
    """For a strictly increasing series, EMA must be below the last value
    (it lags) but above the first value."""
    inst = _inst()
    values = [float(i) for i in range(1, 21)]  # 1..20
    result = mod.PolyQuickTrader.ema(inst, values, 5)
    assert values[0] < result < values[-1]


def test_ema_step_function_known_value():
    """EMA(period=2) of [0, 0, 0, 100]: alpha = 2/3.
    After step 0,0,0,100:
      e0=0; e1 = 2/3*0 + 1/3*0 = 0; e2=0; e3 = 2/3*100 + 1/3*0 = 66.6666...
    """
    inst = _inst()
    result = mod.PolyQuickTrader.ema(inst, [0.0, 0.0, 0.0, 100.0], 2)
    assert abs(result - 200.0 / 3.0) < 1e-9


# ---------------------------------------------------------------- rsi

def test_rsi_all_gains_returns_100():
    # rsi formula: if losses == 0 → 100.0 exactly
    inst = _inst()
    values = [float(i) for i in range(1, 30)]  # strictly up
    assert mod.PolyQuickTrader.rsi(inst, values, 14) == 100.0


def test_rsi_all_losses_returns_zero():
    inst = _inst()
    values = [float(30 - i) for i in range(0, 30)]  # 30, 29, ..., 1
    # losses>0, gains=0 → rs=0 → rsi=100 - 100/(1+0) = 0
    assert mod.PolyQuickTrader.rsi(inst, values, 14) == 0.0


def test_rsi_equal_gains_and_losses_returns_50():
    inst = _inst()
    # +1, -1, +1, -1, ...; gains=losses → rs=1 → rsi=50
    values = [10.0]
    for i in range(28):
        values.append(values[-1] + (1.0 if i % 2 == 0 else -1.0))
    result = mod.PolyQuickTrader.rsi(inst, values, 14)
    assert abs(result - 50.0) < 1e-9
```

## Verification commands

1. **File exists**:
   ```bash
   wc -l tests/test_helpers.py
   ```
   Expected: ~150 lines.

2. **Run new tests**:
   ```bash
   .venv/bin/python -m pytest tests/test_helpers.py -v
   ```
   Expected: ~25 passed (parametrize expands `price_decimals` to 5).

3. **Sandbox-friendly variant** (Lesson 2):
   ```bash
   .venv/bin/python -m pytest tests/test_helpers.py -p no:cacheprovider --capture=no
   ```

4. **Full suite still green**:
   ```bash
   .venv/bin/python -m pytest tests/ -v
   ```
   Expected: 42 + ~25 = **~67 passed**.

5. **Falsification sanity** (Phase 8 explicit V6 requirement): manually flip a sign in `reversal_factors` and confirm ≥ 1 test fails; revert.

## Live verification owed (user)

None — pure unit tests.

## Schema source

None — all formulas are internal.

## Lesson alignment

- Lesson 5 (untracked file false BLOCK): expect; override after manual check.
- Lesson 2 (pytest sandbox): use `-p no:cacheprovider --capture=no`.
