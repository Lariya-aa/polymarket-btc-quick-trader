# Plan: Phase 12 — Parallel reversal strategies (RED_UP + GREEN_DOWN) [HIGH RISK]

> ⚠️ **PHASE 12 IS HIGH RISK** ⚠️
>
> First feature-driven change to the reversal-live module after 11 risk-driven hardening phases. Rewrites global single-strategy state (`live_auto_running` boolean → `dict[mode, ...]`) and the live-tab UI. Defects here resurrect bug classes Phases 1/4/5 already crushed — fill verification, settlement misclassification, mid-cycle rollback — but now multiplied by two concurrent paths sharing the same wallet, aiohttp session, and Server酱 channel.
>
> **Pre-conditions — all must be met before this plan executes**:
> 1. Phase 1 (real fill `/1e6` scaling) — ✅ landed `c7b1486`, **live-verify owed** (real 0.5 USDC buy log shows shares not fixed-math).
> 2. Phase 4 (data-api `redeemable` settlement) — ✅ landed `476a7b2` + final-patch `1b4e4a8`, **live-verify owed** (WIN/LOSS log = `data-api redeemable` / `data-api 仓位归零`).
> 3. Phase 5 (cycle rollback) — ✅ landed `2ebc0a5`, **live-verify owed** (stub-fail → rollback flatten observed).
> 4. Phase 10 (periodic balance refresh) — must land BEFORE Phase 12. Phase 12 reads `self.account_last_balance_usdc`.
>
> **If any of (1)-(4) is not live-verified, this plan MUST NOT be implemented.** A second AI-trio cycle with fresh Virtues scrutiny is required before any code lands.

## Why

`VIRTUES-PLAN-ADDENDUM.md` Phase 12 (V1 / V3 / V7 / V8): `live_auto_running` (L140), `live_auto_stop_requested` (L141), `live_results` (L138) are single-instance — cannot run RED_UP and GREEN_DOWN concurrently. Users on 24h sessions miss the symmetric trigger and lose half the opportunity surface.

Phase 12 enables BOTH strategies to run side-by-side with independent state, stop, stakes — plus a shared aggregate **exposure cap** so the wallet cannot over-commit.

## Schema source

**No new external API.** Phase 12 reuses every endpoint already verified by earlier phases (`/order` Phase 1, `data-api /positions` Phase 4, USDC balance Phase 10). **All risk is internal**: the dict-keyed state model + exposure aggregation formula.

## Files in scope (whitelist)

- `poly_mm_pro_max.py` — refactor 5 state fields to dicts, parameterize `run_reversal_live_real`/`live_auto_button_clicked`/`stop_live_auto_clicked` by mode, refactor `setup_live_auto_tab` into two columns, add `_total_open_exposure_usdc` helper, add `daily_max_exposure_usdc` Entry + config field.
- `tests/test_parallel.py` — **NEW**. ≥11 cases.
- `poly_config_pro.example.json` — add `"daily_max_exposure_usdc": 50` example.

## Out of scope (explicit DON'T)

1. DO NOT change `_settle_from_positions` (Phase 4), `_flatten_cycle_positions` (Phase 5), `_append_trade_journal` / `_aggregate_daily_journal` / `_render_daily_report_md` (Phase 9/11), `_daily_report_tick`, `_periodic_account_refresh_tick`, `_get_aiohttp_session`, `buy_quick_market` / `sell_token_limit` / `sell_position_limit` / `_extract_fill`, `fetch_btc_15m_klines`, `run_reversal_backtest`, `run_reversal_live_sim`, `reversal_factors` / `reversal_stakes` — all frozen contracts.
2. DO NOT introduce `threading.Lock` around `live_results` — partitioning by mode (`live_results[mode]`) means workers never write the same list.
3. DO NOT introduce new aiohttp session, `asyncio.create_task`, or auto-start of the second mode — both buttons MUST be user-clicked.
4. DO NOT cancel open orders to enforce the exposure cap. Cap is enforced **only at new-cycle gate** — mid-cycle enforcement would race Phase 5 rollback.
5. DO NOT alter Server酱 push messages beyond per-mode `策略:` tagging (already present).
6. DO NOT touch `run_reversal_backtest` or `run_reversal_live_sim`. Real-money path only.

## The change (literal spec)

### Step 1 — Refactor instance state to dict-per-mode

In `PolyQuickTrader.__init__` (~L138), **before** `self.live_results = []`/`live_auto_running = False`/`live_auto_stop_requested = threading.Event()`. **After**:

```python
        # Phase 12: per-mode independent live-trading state. Keyed by REVERSAL_MODE_*.
        self.live_results: dict[str, list] = {
            REVERSAL_MODE_RED_UP: [], REVERSAL_MODE_GREEN_DOWN: [],
        }
        self.live_auto_enabled = False  # gates live-tab visibility (unchanged)
        self.live_auto_running: dict[str, bool] = {
            REVERSAL_MODE_RED_UP: False, REVERSAL_MODE_GREEN_DOWN: False,
        }
        self.live_auto_stop_requested: dict[str, threading.Event] = {
            REVERSAL_MODE_RED_UP: threading.Event(),
            REVERSAL_MODE_GREEN_DOWN: threading.Event(),
        }
```

### Step 2 — Refactor `setup_live_auto_tab` into two columns

`setup_live_auto_tab` (L430-499) builds one widget set + one tree. Refactor: replace `cbo_live_mode` combobox with two LabelFrame columns (left = RED_UP, right = GREEN_DOWN). Each column's Entry/Button becomes `dict[str, ttk.Widget]` keyed by mode (`ent_live_usdc[mode]`, `ent_live_layers[mode]`, `ent_live_entry[mode]`, `ent_live_max_hours[mode]`, `btn_start_live_auto[mode]`, `btn_stop_live_auto[mode]`). Mode fixed per column.

Below the two columns: ONE shared `live_tree` Treeview + new `策略` column. New `ent_daily_max_exposure` Entry (default `50`, validated `float > 0`). NO new ttk styles, NO grid change.

### Step 3 — Parameterize `live_auto_button_clicked` by mode

`live_auto_button_clicked` (~L1567) reads `self.cbo_live_mode.get()`. Replace with `live_auto_button_clicked(mode: str)`, wire buttons via `functools.partial(self.live_auto_button_clicked, REVERSAL_MODE_RED_UP)`. Body: read all entry widgets via `[mode]` index, validate `daily_max_exposure_usdc` (raise messagebox on parse error / `≤0`), set `self.live_auto_running[mode] = True`, `self.live_auto_stop_requested[mode].clear()`, `self.live_results[mode] = []`, configure `self.btn_start_live_auto[mode]` disabled and `self.btn_stop_live_auto[mode]` normal, spawn worker thread (one daemon thread + `asyncio.new_event_loop()` per mode, same shape as today). Finally clause clears `self.live_auto_running[mode]`. Two start buttons clickable independently.

### Step 4 — Parameterize `run_reversal_live_real`

In `run_reversal_live_real(config)` (~L1648), bind `stop_event = self.live_auto_stop_requested[config["mode"]]` at top. Replace every `self.live_auto_stop_requested` (L1663/1673/1682/1692/1698/1699/1864) with `stop_event`. `_settle_from_positions(..., self.live_auto_stop_requested)` (L1769) → pass `stop_event`. `self.live_results.append(row)` (L1752) → `self.live_results[config["mode"]].append(row)`.

### Step 5 — Exposure cap enforcement (THE risk-bearing addition)

New helper, inserted immediately after `_settle_from_positions`:

```python
    def _total_open_exposure_usdc(self) -> float:
        """Sum stake across active layers in every mode. New-cycle gate only
        (mid-cycle would race Phase 5 rollback)."""
        total = 0.0
        for rows in self.live_results.values():
            for row in rows:
                if row.get("status") in ("OPEN_REAL", "REVERSAL_REAL_NEXT"):
                    try:
                        total += float(row.get("entry", 0)) * float(row.get("size", 0))
                    except (TypeError, ValueError):
                        pass
        return total
```

In `run_reversal_live_real`, immediately AFTER `cycle_count += 1` (L1686) and BEFORE `accumulated_loss = 0.0`:

```python
            current_exposure = self._total_open_exposure_usdc()
            cap = config.get("daily_max_exposure_usdc", float("inf"))
            if current_exposure + sum(stakes) > cap:
                self.log_live(logging.WARNING,
                    "%s 周期 #%s 拒绝触发: 预计暴露 $%.2f + 现有 $%.2f > 上限 $%.2f",
                    profile["label"], cycle_count, sum(stakes), current_exposure, cap)
                await self.push_to_server_chan(
                    "Polymarket 反转实盘 ⚠️ 暴露超限",
                    f"### ⚠️ 暴露超限\n\n- 策略: `{profile['label']}`\n- 周期: `{cycle_count}`\n- 预计新增: `${sum(stakes):.2f}`\n- 现有: `${current_exposure:.2f}`\n- 上限: `${cap:.2f}`")
                seen_triggers.add(trigger_key)
                continue
```

### Step 6 — `render_live_results` shows mode column

`render_live_results` (L1889) iterates `self.live_results` list. Replace with flatten across mode dicts: `flat = [(m, r) for m, rows in self.live_results.items() for r in rows]`, then iterate; prepend mode label as first column (add `策略` to Treeview columns in Step 2).

### Step 7 — `stop_live_auto_clicked(mode)`

```python
    def stop_live_auto_clicked(self, mode: str):
        if self.live_auto_running.get(mode, False):
            self.live_auto_stop_requested[mode].set()
            self.btn_stop_live_auto[mode].configure(state="disabled")
            self.log_live(logging.WARNING, "已请求停止 %s 实盘; 不会新增下一单。", mode)
```

### Step 8 — Tests (`tests/test_parallel.py`)

≥10 cases. Tk-free `PolyQuickTrader.__new__` (per `test_settle.py` / `test_daily_report.py`):

1. `test_state_dicts_independent` — `live_auto_running[RED_UP]=True`; `[GREEN_DOWN]` stays False.
2. `test_stop_event_isolation` — `[RED_UP].set()`; `[GREEN_DOWN].is_set()` False.
3. `test_live_results_partition` — append to one mode, other empty.
4. `test_exposure_calc_single_mode` — 2 OPEN_REAL rows; `_total_open_exposure_usdc()` = sum.
5. `test_exposure_calc_cross_mode` — both modes populated; aggregate.
6. `test_exposure_cap_rejects_new_cycle` — projected > cap → reject, trigger_key in seen_triggers, push_to_server_chan called once.
7. `test_exposure_cap_below_proceeds` — projected ≤ cap → proceeds.
8. `test_exposure_skips_closed_rows` — REVERSAL_REAL_WIN/LOSS not counted.
9. `test_render_filters_by_mode` — builds rows from BOTH dicts.
10. `test_button_clicked_isolation` — `live_auto_button_clicked(RED_UP)` doesn't touch `[GREEN_DOWN]`.
11. `test_settle_stop_event_uses_correct_mode` — mock `_settle_from_positions`; receives `[RED_UP]` not `[GREEN_DOWN]`.

Sandbox-friendly variant (Lesson 2): `pytest tests/test_parallel.py -p no:cacheprovider --capture=no -q`.

## Verification commands

1. **Dict state**:
   ```bash
   grep -nE "self\.live_auto_running\[|self\.live_auto_stop_requested\[|self\.live_results\[" poly_mm_pro_max.py | head
   ```
   Expected: ≥20 lines.

2. **No boolean state left**:
   ```bash
   grep -nE "self\.live_auto_running\s*=\s*(True|False)" poly_mm_pro_max.py
   ```
   Expected: empty.

3. **Exposure cap helper**:
   ```bash
   grep -n "_total_open_exposure_usdc\|daily_max_exposure_usdc\|暴露超限" poly_mm_pro_max.py
   ```
   Expected: ≥4 lines.

4. **Two start buttons**:
   ```bash
   grep -nE "btn_start_live_auto\[(REVERSAL_MODE_RED_UP|REVERSAL_MODE_GREEN_DOWN)\]" poly_mm_pro_max.py
   ```
   Expected: ≥2 distinct mode-keyed accesses.

5. **No `cbo_live_mode` left**:
   ```bash
   grep -n "cbo_live_mode" poly_mm_pro_max.py
   ```
   Expected: empty.

6. **Frozen helpers untouched** — line counts of `_settle_from_positions`, `_flatten_cycle_positions`, `_aggregate_daily_journal`, `_daily_report_tick`, `_periodic_account_refresh_tick` identical to main (baseline first).

7. **Tests pass** (both invocations — Lesson 2):
   ```bash
   .venv/bin/python -m pytest tests/test_parallel.py -v
   .venv/bin/python -m pytest tests/test_parallel.py -p no:cacheprovider --capture=no -q
   ```
   Expected: 11 passed each.

8. **Full suite**:
   ```bash
   .venv/bin/python -m pytest tests/ -q
   ```
   Expected: 106 + 8 (Phase 10) + 11 (Phase 12) = 125 passed.

9. **Import OK**:
   ```bash
   .venv/bin/python -c "import poly_mm_pro_max; print('import ok')"
   ```

10. **Plan-coverage false-positive ack** (Lesson 5): `tests/test_parallel.py` NEW + untracked → Codex `git diff --name-only` will BLOCK. Override pre-authorized provided `git status --porcelain` shows `?? tests/test_parallel.py`, all 11 tests pass on both invocations, AND user confirmed Phase 1/4/5/10 live-verified.

## Live verification owed (user, post-commit)

⚠️ **Minimum stake (0.5 USDC initial, max_layers=2) for ALL Phase 12 live verification.**

1. GUI shows two columns; each independently editable.
2. Click 启动 RED_UP only → only RED_UP runs; GREEN_DOWN still enabled.
3. Click 启动 GREEN_DOWN while RED_UP running → both run; `live_tree` shows interleaved `策略` column.
4. Click 停止 RED_UP → only RED_UP stops; GREEN_DOWN continues. Verify per-mode log tagging.
5. `daily_max_exposure_usdc=5` + start both with initial=5 → second mode triggers → Server酱 `⚠️ 暴露超限` + seen_triggers entry, no buy.
6. `trade_journal.csv` rows from both strategies; `(strategy, cycle)` tuples unique per mode.
7. Phase 11 daily report next morning aggregates BOTH strategies separately.
8. `lsof -p $(pgrep -f poly_mm_pro_max) | grep TCP | wc -l` stable over 30 min — Phase 7 contract held.
9. Stop on a stopped mode does NOT touch other mode's state.

## Relationship to other phases

- **Hard-depends on** Phase 1 ✅, 4 ✅, 5 ✅, 10 (must land first).
- **Coexists with** Phase 11 (groups by `strategy`) and Phase 7 (loop-id keyed; two threads = two loops = two sessions, Phase 7 final-patch `1b4e4a8`).
- **Does NOT touch** Phase 2, 3, 6, 8, 9.

## Plan-coverage false-positive ack (Lesson 5)

`tests/test_parallel.py` NEW but un-`git add`-ed. Codex `git diff --name-only` will BLOCK on plan_coverage. Override pre-authorized if `git status --porcelain` shows `?? tests/test_parallel.py`, all 11 tests pass, AND user confirmed Phase 1/4/5/10 live-verified.
