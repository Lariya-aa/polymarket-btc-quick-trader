# cycles/virtues-final-patches/handoff.md

> Commit `1b4e4a8`. 不是标准 ai-trio cycle —— 是 10 个 phase 全部 land 后跑了一次 `codex review --base origin/main` 对全 branch diff 做整体 review，找出 3 个 cross-phase 真问题，bundle 进一个 hotfix commit。

## 干了啥

10 个主体 phase 都 land 进 main 后，跑了一次最终审查：
```bash
codex review --base origin/main
```

Codex 看的是 26-commit 全 diff，目标是 catch "phase N 修复破坏了 phase M 假设" 这类 cross-cycle 集成问题。结果**找出 3 个真 issue**：

### 1. [P1] Phase 4 settle absent-state machine

`_settle_from_positions` 在判 LOSS 时要求 "asset 连续两次不在 positions list"。但我 Phase 4 实现里，HTTP 错误时**没重置** `last_state`。结果：

```
poll 1: asset absent (成功) → last_state = "absent"
poll 2: HTTP 503 (重试)
poll 3: asset absent (成功) → last_state == "absent" → return LOSS
```

只有 2 次成功 absent 中夹了 1 次 HTTP 错误，被误判 LOSS。Polymarket data-api 在重启/限流时短暂 503 完全可能。误判 LOSS → 反向 martingale 加倍 → 真金白银失血。

**修法**：except 分支加 `last_state = None`。强制 "连续两次成功 absent" 才判 LOSS。

### 2. [P2] Phase 11 daily report 双计

trade_journal.csv 一行一 layer。WIN layer 的 `pnl_estimate` 公式是 `(1.0 - entry) * size - accumulated_loss` —— 已经减了前面 layer 的累计 loss。

但 `_aggregate_daily_journal` **把所有 row 的 pnl_estimate 加起来**。对于"输-然后-赢" cycle：
- layer 1 loss: pnl = -5.5
- layer 2 win: pnl = 2.978（=gross 8.478 - accumulated 5.5）
- 加起来 = -2.522 ← **错**，真实 cycle PnL 是 +2.978

**修法**：per-cycle 去重。对每个 `(strategy, cycle)`，找 WIN row，它的 pnl 就是 net cycle PnL，**单独算**；没 WIN 才 sum 所有 LOSS rows。

### 3. [P2] Phase 7 死 loop session 累积

GUI 9 处 worker 都用 `asyncio.new_event_loop() + close()` 短命 loop。`_AIOHTTP_SESSIONS` 按 `id(loop)` 缓存，loop close 后 cache 里那个 session 永远留着（bound to dead loop），新 click 又开新 loop 又 cache 一个。长跑 GUI 累积 100+ 死 session。

**修法**：每次调 `_get_aiohttp_session` 先 sweep cache，删 `session._loop.is_closed()` 的 entries。死 session 给 Python GC。

## 你看到的差异

正常情况：完全没区别。WIN/LOSS、日报、TCP 句柄数全跟之前一样。

边界情况：
- Polymarket data-api 抖动时不再误判 LOSS
- 日报 PnL 数字现在跟 `cycle_pnl_running` Server 酱 heartbeat 一致（之前两个数对不上）
- 长跑 GUI 24h `lsof -p <pid> | grep TCP | wc -l` 数字趋稳

## 你要做的 verification

正常 verification 就够（不需要单独跑 final-patches 验证）。所有 3 个修复都有 unit test 锁住。

## 收尾文档

详细 roadmap 状态见 `cycles/_final-review.md`。
