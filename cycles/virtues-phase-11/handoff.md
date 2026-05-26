# cycles/virtues-phase-11/handoff.md

> Commit `874fe25`. Phase 11 = 每天 UTC 0 点之后自动生成昨天的反转策略日报 + Server酱 推送。**Codex 这次出 PASS（第一次！）** + 2 个真 V3 warn 已 inline 修。

## 干了啥

每分钟 Tk root.after 一次 tick：
- 读 `reports/.daily_report_last.txt` 看上次生成的是哪天
- 如果跟"昨天"不同 → spawn 一个 worker thread
- worker:
  1. 读 `trade_journal.csv`（Phase 9 加的流水）
  2. 调 `_aggregate_daily_journal` 过滤昨天那 24h 数据，统计 cycle 数 / WIN-LOSS 比 / 累计 pnl_estimate / 最大连亏层数 / 异常数
  3. 调 `_render_daily_report_md` 渲染成 Markdown
  4. 写 `reports/daily_report_YYYY-MM-DD.md`
  5. Server酱 push 同样 Markdown
  6. 写 `reports/.daily_report_last.txt` 作为 idempotency marker

`reports/` 已加 .gitignore。

## Codex 抓的 2 个真 warn（已 inline 修）

### 1. NaN/Inf 污染 daily PnL

原代码 `pnl_sum += float(r.get("pnl_estimate", "0") or "0")` — Python `float("nan")` 合法返回 NaN，**NaN + 任何数 = NaN**，整天 P&L 变 "nan"。Inf 同理。

修法：
```python
v = float(r.get("pnl_estimate", "0") or "0")
if v != v or v in (float("inf"), float("-inf")):
    continue
pnl_sum += v
```

加测试 `test_aggregate_rejects_nan_and_inf_pnl` 覆盖。

### 2. 60s tick race

原 tick 看 marker 文件决定要不要 spawn worker。但 worker 写 marker 是**最后一步**（generate + push 完了才写）。如果 worker 跑得慢（CSV 大 / Server酱推送超时），60s 后下一个 tick 触发，看到 marker 还是旧日期 → 又 spawn 一个 worker → 双份日报 / Server酱 双推 / 写文件 race。

修法：instance flag `self._daily_report_running`。Tick 顶部检查；worker `finally` 重置。Marker 仍最后写（push 失败时下一 tick 重试）。

## 你看到的差异

跑反转实盘跨过 UTC midnight 后：
- `reports/daily_report_2026-05-26.md` 生成
- Server酱收到 "Polymarket BTC 反转实盘日报" 推送（详见标题 + Markdown body）
- 之后 60s tick 不重发（marker 已写）

## 你要做的 live verification

让 GUI 跑过 UTC 0 点（你时区 +8 即北京时间早 8 点）→ 应该 1 分钟内收到日报 push + `reports/` 目录下生成 .md 文件。

如果跑过多天没动 GUI，第一次启动 GUI 后 5 秒首 tick 就生成"昨天"的日报。

## 这是路线图的最后一个 phase

Phase 1/2/3/4/5/6/7/8/9/11 全部 land 进 main。Phase 10 / Phase 12 是 ADDENDUM（未 commit 到主路线图）。

下一步是最终 codex review on the whole branch + push origin / PR。
