# Plan: Phase 11 — Daily trade report (V9 Falsifiable Communication)

## Why

VIRTUES-PLAN-ADDENDUM.md Phase 11 (V9 / V8 / V6): Phase 9 lands `trade_journal.csv` with 14-column schema, but the user has no daily push summarizing yesterday's activity. Without a daily report, the user has to `awk` / `tail -f` the CSV manually to spot anomalies (rising consecutive-loss streak, growing `pending_timeout` count). Phase 11 generates `reports/daily_report_YYYY-MM-DD.md` summarizing the previous UTC day's rows and pushes it via Server酱.

## Trigger choice — recommended

No daemon exists; GUI runs Tk mainloop + per-click `asyncio.new_event_loop()`. Two viable triggers:

**(A) Tk `root.after(60_000, ...)` self-rescheduling tick** — runs whether reversal_live is on or off; uses an idempotency file `reports/.daily_report_last.txt` so it fires once per UTC day. Spawns a `threading.Thread` + fresh `asyncio.new_event_loop()` for the Server酱 push, matching the 9 existing per-click loop patterns.

**(B) Inline check inside `run_reversal_live_real` heartbeat (L1581)** — zero new infra but only fires when strategy is running.

**Recommendation: Option A.** Falls back to (B) only if user objects to a Tk-scheduled background tick.

## Files in scope (whitelist)

- `poly_mm_pro_max.py` — add `_aggregate_daily_journal(rows, target_date)` pure helper, `_render_daily_report_md(stats)` pure helper, `_daily_report_tick()` Tk-scheduled callback, register `root.after` in `PolyQuickTrader.__init__`.
- `tests/test_daily_report.py` — **NEW**. ≥7 tests covering the pure aggregator and renderer.
- `.gitignore` — add `reports/` line.

## Out of scope (explicit DON'T)

- DO NOT modify `_append_trade_journal` (Phase 9 — frozen contract).
- DO NOT change CSV schema (14 columns locked).
- DO NOT add a real cron / launchd timer / external scheduler. Only Tk `root.after`.
- DO NOT touch `run_reversal_live_real` heartbeat block (option B is fallback only).
- DO NOT alter the `push_to_server_chan` signature.
- DO NOT compute "real" P&L from account-balance diff (that needs Phase 10's periodic balance refresh, which doesn't exist yet). Use `sum(pnl_estimate)` from CSV with explicit caveat in the report header.
- DO NOT rotate/archive `trade_journal.csv` — read-only aggregation.
- DO NOT block the Tk mainloop on report generation (must run on a worker thread, same pattern as `refresh_positions_button_clicked` L2205).
- DO NOT silently swallow Server酱 push failures — log at WARNING.
- DO NOT generate reports for the current (incomplete) UTC day — only for yesterday and earlier.
- DO NOT introduce `pandas` dep; use stdlib `csv`.

## Schema source

Internal: `_append_trade_journal` docstring (`poly_mm_pro_max.py` L2178-2204) defines the 14-column CSV header **verbatim**:
```
ts, strategy, cycle, layer, market_slug, direction,
stake_usdc, requested_price, fill_price, fill_size,
fill_verified, outcome, pnl_estimate, accumulated_loss
```
Where `outcome ∈ {"win", "loss", "pending_timeout"}` and `fill_verified ∈ {"True", "False"}`. Source is the Phase 9 commit (`d195ab9`) and `tests/test_observability.py:test_journal_writes_header_on_first_call` lock the order.

## The change (literal spec)

### Step 1 — Add pure aggregator

Insert immediately after `_append_trade_journal` (anchor: line that says `logging.getLogger("PolyQuickTrader").warning("trade journal append 失败: %s", e)`, then a blank line, then the next `def`):

```python
    @staticmethod
    def _aggregate_daily_journal(rows: list[dict], target_date_utc: str) -> dict:
        """Pure aggregator over trade_journal rows (csv.DictReader-parsed).
        target_date_utc: 'YYYY-MM-DD'. Counts cycles via (strategy,cycle) tuple."""
        same_day = [r for r in rows if r.get("ts", "")[:10] == target_date_utc]
        cycles = {(r["strategy"], r["cycle"]) for r in same_day}
        wins = [r for r in same_day if r.get("outcome") == "win"]
        losses = [r for r in same_day if r.get("outcome") == "loss"]
        timeouts = [r for r in same_day if r.get("outcome") == "pending_timeout"]
        unverified = [r for r in same_day if r.get("fill_verified") == "False"]
        pnl_sum = 0.0
        for r in same_day:
            try:
                pnl_sum += float(r.get("pnl_estimate", "0") or "0")
            except (TypeError, ValueError):
                pass
        # Max consecutive loss layers within any single (strategy,cycle).
        max_consec_loss = 0
        per_cycle: dict[tuple, list[dict]] = {}
        for r in same_day:
            per_cycle.setdefault((r["strategy"], r["cycle"]), []).append(r)
        for rs in per_cycle.values():
            try:
                rs_sorted = sorted(rs, key=lambda x: int(x.get("layer", 0)))
            except (TypeError, ValueError):
                rs_sorted = rs
            run = best = 0
            for r in rs_sorted:
                if r.get("outcome") == "loss":
                    run += 1
                    best = max(best, run)
                else:
                    run = 0
            max_consec_loss = max(max_consec_loss, best)
        return {
            "date": target_date_utc,
            "total_rows": len(same_day),
            "cycle_count": len(cycles),
            "win_count": len(wins),
            "loss_count": len(losses),
            "pending_timeout_count": len(timeouts),
            "unverified_fill_count": len(unverified),
            "pnl_estimate_sum": pnl_sum,
            "max_consecutive_loss_layers": max_consec_loss,
            "anomaly_count": len(timeouts) + len(unverified),
        }

    @staticmethod
    def _render_daily_report_md(stats: dict) -> str:
        lines = [
            f"# Polymarket BTC 反转实盘日报 — {stats['date']} (UTC)",
            "",
            f"- 触发周期数: **{stats['cycle_count']}**",
            f"- 总成交行数: {stats['total_rows']}",
            f"- WIN: {stats['win_count']}",
            f"- LOSS: {stats['loss_count']}",
            f"- 估算 P&L 累计: **{stats['pnl_estimate_sum']:+.4f} USDC** "
            "（基于 trade_journal.csv 的 pnl_estimate，仍是 model 估算）",
            f"- 最大单 cycle 连亏层数: {stats['max_consecutive_loss_layers']}",
            "",
            "## 异常",
            f"- pending_timeout: {stats['pending_timeout_count']}",
            f"- fill_verified=False: {stats['unverified_fill_count']}",
            f"- 异常总计: **{stats['anomaly_count']}**",
            "",
            "链上真实余额变化以 polymarket.com/portfolio 为准。",
        ]
        return "\n".join(lines) + "\n"
```

### Step 2 — Tk-scheduled tick + report I/O

Insert after `_render_daily_report_md`:

```python
    def _daily_report_tick(self):
        """Tk root.after callback. Checks whether a new UTC day has rolled
        over since the last report; if so, spawns a worker thread to
        generate + push report for yesterday. Re-schedules itself."""
        try:
            today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            last_path = os.path.join("reports", ".daily_report_last.txt")
            last_reported = ""
            if os.path.exists(last_path):
                try:
                    with open(last_path, "r", encoding="utf-8") as f:
                        last_reported = f.read().strip()
                except OSError:
                    pass
            # Report for "yesterday" (everything earlier than today_utc).
            yesterday = (datetime.now(timezone.utc).date() -
                         __import__("datetime").timedelta(days=1)).strftime("%Y-%m-%d")
            if last_reported != yesterday:
                def worker(day=yesterday, last=last_path):
                    try:
                        loop = asyncio.new_event_loop()
                        try:
                            loop.run_until_complete(self._generate_and_push_daily_report(day, last))
                        finally:
                            loop.close()
                    except Exception as e:
                        self.logger.warning("日报生成异常: %s", e)
                threading.Thread(target=worker, daemon=True).start()
        finally:
            self.root.after(60_000, self._daily_report_tick)

    async def _generate_and_push_daily_report(self, target_date_utc: str, last_path: str):
        import csv as _csv
        path = "trade_journal.csv"
        if not os.path.exists(path):
            self.logger.info("daily report skip: trade_journal.csv 不存在")
            return
        rows: list[dict] = []
        try:
            with open(path, "r", encoding="utf-8", newline="") as f:
                rows = list(_csv.DictReader(f))
        except OSError as e:
            self.logger.warning("daily report 读取 journal 失败: %s", e)
            return
        stats = PolyQuickTrader._aggregate_daily_journal(rows, target_date_utc)
        if stats["total_rows"] == 0:
            self.logger.info("daily report skip: %s 当日无交易", target_date_utc)
            # Still mark as reported so we don't retry every minute.
            self._mark_daily_reported(target_date_utc, last_path)
            return
        body = PolyQuickTrader._render_daily_report_md(stats)
        os.makedirs("reports", exist_ok=True)
        out_path = os.path.join("reports", f"daily_report_{target_date_utc}.md")
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(body)
        except OSError as e:
            self.logger.warning("daily report 写文件失败: %s", e)
        await self.push_to_server_chan(
            f"Polymarket 日报 {target_date_utc}",
            body,
        )
        self._mark_daily_reported(target_date_utc, last_path)

    @staticmethod
    def _mark_daily_reported(target_date_utc: str, last_path: str):
        try:
            os.makedirs(os.path.dirname(last_path) or ".", exist_ok=True)
            with open(last_path, "w", encoding="utf-8") as f:
                f.write(target_date_utc)
        except OSError:
            pass
```

### Step 3 — Wire the tick into `__init__`

Anchor: search for `class PolyQuickTrader` then the end of its `__init__` method. The `__init__` ends just before the next method (`def derive_api_creds` ~ L598). At the **last line of `__init__`** add:

```python
        # Phase 11: schedule daily report check (idempotent, fires once per UTC day).
        self.root.after(5_000, self._daily_report_tick)
```

(5 s startup delay so we don't fire during Tk widget construction.)

### Step 4 — `.gitignore`

**Before** (anchor: `trade_journal.csv` line)
```
trade_journal.csv
```

**After**
```
trade_journal.csv
reports/
```

### Step 5 — `tests/test_daily_report.py` (NEW)

```python
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


def test_render_includes_date_and_caveat():
    stats = mod.PolyQuickTrader._aggregate_daily_journal([
        _row("2026-05-25T01:00:00+00:00", pnl="+1.0"),
    ], "2026-05-25")
    body = mod.PolyQuickTrader._render_daily_report_md(stats)
    assert "2026-05-25" in body
    assert "USDC" in body
    assert "pnl_estimate" in body or "估算" in body
    assert body.endswith("\n")
```

## Verification commands

1. **Helpers exist**: `grep -n "_aggregate_daily_journal\|_render_daily_report_md\|_daily_report_tick" poly_mm_pro_max.py` — expect ≥ 4 hits.
2. **New tests**: `.venv/bin/python -m pytest tests/test_daily_report.py -v` — expect 7 passed.
3. **Full suite**: `.venv/bin/python -m pytest tests/ -v` — expect 42 + 7 = 49 passed.
4. **Sandbox variant**: `.venv/bin/python -m pytest tests/test_daily_report.py -p no:cacheprovider --capture=no`.
5. **.gitignore**: `grep -c "^reports/" .gitignore` — expect 1.
6. **Import sanity**: `.venv/bin/python -c "import poly_mm_pro_max; print('import ok')"`.

## Live verification owed (user)

1. Run GUI across a UTC midnight; within 60 s after 00:00 expect:
   - `reports/daily_report_<yesterday>.md` created
   - Server酱 push titled `Polymarket 日报 <YYYY-MM-DD>`
   - `reports/.daily_report_last.txt` = `<yesterday>`
2. Cross-check WIN/LOSS counts vs `awk -F, '$1 ~ /^<date>/ {print $12}' trade_journal.csv | sort | uniq -c`.
3. Run 2 days in a row — fires twice, no duplicate.

## Dependencies / blockers

- **Not BLOCKED** — Tk `root.after` trigger is implementable with existing primitives.
- `pnl_estimate` accuracy is model-bound (chain-truth needs Phase 10 balance diff, not landed). Report header documents this caveat.
- Lesson 5 applies (untracked new test file + new `reports/` dir → expect Codex `plan_coverage` false BLOCK).
