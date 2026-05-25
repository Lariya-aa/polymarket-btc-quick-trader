# cycles/virtues-phase-9/handoff.md

> Commit `d195ab9`. Phase 9 = observability 打底（journal CSV + heartbeat 推送 + NaN guard）+ Codex 发现的 PnL 累加 bug 修复。

## 干了啥

三件事 + 1 个 inline bug 修复：

### 1. Trade journal CSV
`trade_journal.csv` 在项目根目录（已加 `.gitignore`）。每次反转 cycle 的每个 layer settlement 后，append 一行：

```
ts, strategy, cycle, layer, market_slug, direction, stake_usdc,
requested_price, fill_price, fill_size, fill_verified, outcome,
pnl_estimate, accumulated_loss
```

事后想看胜率、最大连亏、PnL 分布，直接 `awk` / 导入 Excel / pandas 即可。`outcome` 列含 `win` / `loss` / `pending_timeout` 三种。

### 2. 心跳推送
反转实盘每**10 个 cycle** push 一条 Server酱：

```
- 策略: 三连阴转 UP
- 已触发周期: 23
- 累计估算 pnl: +12.34 USDC
- 剩余小时: 8.5
```

长跑 24 小时不再"完全没消息"。

### 3. NaN/Inf guard
`_float_or_zero` 之前用 `try/except (TypeError, ValueError)`，但 `float("nan")` / `float("inf")` 是**合法**的——结果是 NaN/Inf 穿透到 UI label、position 求和、PnL 显示，出现"nan"字符串。现在显式 reject NaN/Inf → 0.0。

### 4. PnL 累加 fix（Codex 发现的真 BLOCKER）

原 plan 设计 `cycle_pnl_running += pnl` 在每个 layer settlement 后累加。**但 win 分支的 `pnl` 是 `(1.0 - entry) * size - accumulated_loss`**——已经把前面 layer 累计的 loss 减掉了。如果 LOSS layers 前面也 `cycle_pnl_running += -loss`，等 WIN 时 prior losses 被算了**两次**。

举例：cycle 是"layer 1 lose 5.5, layer 2 win"，真实 pnl = +2.978（输 5.5 + 赚 8.478 → 净 +2.978）。
- 旧（错）：`-5.5 + (gross_8.478 - 5.5) = -2.522`
- 新（对）：`-5.5 + (pnl 2.978 + accumulated_loss 5.5) = +2.978`

Codex 标 blocker 我 inline 修了，加了一个 documentation-as-test 锁住公式。

## 你看到的差异

反转实盘运行**期间**：
- 每 10 cycle 收到一条 `Polymarket 反转实盘心跳` Server酱
- `trade_journal.csv` 每次 layer settlement 后追加一行（你可以 `tail -f` 看实时流水）

反转实盘**结果不变**（pnl 数字、Server酱 WIN/LOSS push 都不动）—— 这次只动**记录**和**累加显示**，不动决策路径。

UI 异常情况：如果 MiniMax 返回 NaN / 某个上游返回 inf，UI label 不再显示 "nan"，而是 "0.00"。

## 你要做的 live verification

跑 ≥10 个 cycle 反转实盘后：
1. 看 `cat trade_journal.csv` 应有 header + 多行
2. 收到 `反转实盘心跳` Server酱推送
3. 触发一个 NaN 场景（构造个测试 mock，或者跳过这步如果不容易复现）

## 不在本 cycle 范围 (Codex deferred warn)

Journal 路径默认 `trade_journal.csv` CWD-relative。如果你用 `LaunchAgent` / 桌面 launcher 启 GUI，CWD 可能是 `/` 或 `$HOME` 而非 repo 目录，journal 会落到那儿（不进 `.gitignore` 保护）。当前 launchd 脚本 `run_poly_mm.sh` 有 `cd "$APP_HOME"`，所以你这个用法 OK。但跨平台严格的修法应该用 `Path(__file__).parent / "trade_journal.csv"`。等做 Phase 11 daily report 时一起调整。

## 解锁

- **Phase 11 (daily report)**：现在有数据源 (`trade_journal.csv`)，daily report 只是聚合统计 + Server酱 push
- **Phase 10 / 12 上层观察性**：心跳基础设施可复用
