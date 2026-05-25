# Plan: poly_mm_pro_max.py Virtues-Driven Hardening

## Context

`poly_mm_pro_max.py` 是单文件 2221 行的 Polymarket BTC 短周期交易工具（Tkinter GUI + py_clob_client_v2 + Binance K 线 + 可选 MiniMax）。三种模式：手动交易、回测/纸面模拟、隐藏的真金白银反转策略实盘。

按 9 条 LLM dev virtues 全面 audit 后发现 **5 个 BLOCKER**（直接威胁真金白银正确性）：

| # | 位置 | 问题 | 真金白银影响 |
|---|---|---|---|
| 1 | `buy_quick_market` L1857–1858 | 把"下单时的 limit price"当成实际成交价上报；从未读 CLOB response 里的 `makingAmount`/`takingAmount` | 所有 P&L、Server 酱推送、UI 持仓全部都是请求值不是成交值 |
| 2 | `run_reversal_live_real` L1655–1681 | 用 `kline_color(...) == win_color` 判赢输；从不查 `fetch_positions` 或订单状态 | 真金白银策略根据 K 线颜色"自我感觉胜负"，跟实际持仓脱节 |
| 3 | L1845/1861/1874/1897/2031 | `asyncio.wait_for(asyncio.to_thread(...))` 超时后底层 SDK 线程**不会**取消 | "超时失败"的订单可能其实成交了，retry 一下就是双倍开仓 |
| 4 | `run_reversal_live_real` L1615–1625 | layer N 失败时 `break`，前面已成交的 layer 1..N-1 仓位**完全不管** | 部分成交的 cycle 留孤儿仓 |
| 5 | `acquire_single_instance_lock` L2201–2210 | open 时 `"w"` 模式先 truncate 再 flock | 第二个实例启动会清空第一个实例的 PID 文件 |

外加一批 WARN（broad except 吞 POLY_1271 报错、`success is False` 漏放 unmatched、NaN 在 `_float_or_zero` 中能穿透、aiohttp session 每次新建、零测试）。

本计划按 **真金白银风险优先** 排序：先把 BLOCKER 修光（Phase 1–5），再修 honest reporting + resource hygiene（Phase 6–7），最后补测试 + 观测性（Phase 8–9）。每个 phase 都是一个 atomic、可独立交付的单元——用户在任意 phase 后停下来都能拿到一个比之前更安全的工具。

之前给出的 P0-P4 路线在这里被吸收和重排：原 P0（POLY_1271 验证）合到 Phase 6 的 error surfacing；原 P1.4（结算改用真实 fill）就是 Phase 1；原 P1.3 资金熔断推迟到 Phase 4 之后再做（要等真实 P&L 数据可信再上）。

---

## Plan Overview

| Phase | Virtue | Goal | Blockers fixed | Definition of Done |
|---|---|---|---|---|
| 1 | Safety, Honesty | 上报真实成交价/量 | #1 | `buy_quick_market` 用 CLOB resp 的真实 fill 而非 limit price，UI/push 同步 |
| 2 | Safety | 干掉 timeout-导致的双开仓 race | #3 | 移除 `wait_for(to_thread(...))`，submit 后强制 reconcile 才允许 retry |
| 3 | Safety, Resource | 锁文件不再自损 | #5 | 第二实例失败时不破坏第一实例的 PID 文件 |
| 4 | Honesty, Correctness | 反转实盘按真实仓位判胜负 | #2 | WIN/LOSS 来自 `fetch_positions`/order status，不来自 K 线颜色 |
| 5 | Safety, Resilience | cycle 中途失败回滚孤儿仓 | #4 | layer N 失败时把 1..N-1 的真实持仓 flatten |
| 6 | Honesty | SDK 错误对用户可见；non-fill 不当成功 | broad except / `success is False` | POLY_1271 报错带原文显示；unmatched/partial 显式分类 |
| 7 | Resource hygiene | 共享 aiohttp session；清理 asyncgens | — | 长跑 30min 后 TCP 句柄数不增长 |
| 8 | Test diligence | 给关键纯函数补回归 | — | `pytest -q` 全绿；翻转 `reversal_factors` 公式能让测试 fail |
| 9 | Observability, Robustness | 流水 CSV、心跳推送、NaN guard | NaN/`_float_or_zero` | 每 cycle 一行 CSV；UI 永远不出现"nan" |

---

## Phase-by-Phase Detail

### Phase 1 — 真实成交价 (BLOCKER #1)
**Virtue:** Honesty, Safety, Proof of Work (8)

**Tasks (atomic commits):**
1. 在 L2159 `clamp_price` 旁加 `_extract_fill(resp) -> (price, size, status)` 纯函数，读 `makingAmount` / `takingAmount` / `status`，附 pytest 单测同提交（virtue 6）
2. `buy_quick_market` L1857–1858：用 `_extract_fill(resp)`，状态不在 `{"matched","filled"}` 抛 `OrderNotFilled`；返回字典里的 `price`/`size` 改成真实 fill
3. `run_reversal_live_real` 两个 call site (L1626, L1646) 同步使用真实 fill；Server 酱推送和 UI label 用真实 fill

**Verification:** 真实 0.5 USDC：scan → buy 一个 spread 不为零的市场 → 日志含 `resp.makingAmount=...`、`fill_price ≠ limit_price` → 跟 polymarket.com/portfolio UI 对账到分位

**User involvement:** Required — 真 CLOB 凭证 + 0.5 USDC + 实盘买入一次

**Risk:** CLOB resp schema drift。用 `.get()` + 字段缺失抛显式异常，不静默回退

---

### Phase 2 — Timeout-then-reconcile (BLOCKER #3)
**Virtue:** Safety, Resource Stewardship (7)

**Tasks:**
1. 删除 L1845/1861/1874/1897/2031 的 `asyncio.wait_for(...)` 包装；改成在 ClobClient 构造时设 HTTP 超时，让 `to_thread` 自己跑完
2. 加 `_reconcile_after_submit(client, market_id, since_ts)`，submit 后查这个 market 的 open orders + positions
3. 任何 retry 之前必须 reconcile 成功；若 reconcile 看到已成交订单，绝不重发

**Verification:** 在 dev branch 注入 `time.sleep(15)` 的 SDK stub 跑这条路径 → 日志显示"submit took 15s, reconciling… found order id X filled" → polymarket.com/portfolio 确认只有一笔成交

**User involvement:** Stub 测试不需要；live 端到端验证可选

**Risk:** 移除 wait_for 会卡 UI 吗？不会——`to_thread` 已经把阻塞调用放到 executor，Tk mainloop 不受影响

---

### Phase 3 — 单实例锁修复 (BLOCKER #5)
**Virtue:** Safety, Resource Stewardship (7)

**Tasks:**
1. `acquire_single_instance_lock` L2201–2210：`open(LOCK_FILE, "a+")` 替代 `"w"` → `flock(LOCK_EX|LOCK_NB)` → 仅 flock 成功后才 `ftruncate(0)` + 写 PID
2. `mainloop()` 外加 `try/finally` 确保 lock 文件在 Tk 退出时 close

**Verification:** 启动 A；启动 B → B 退出并显示"already running, PID=<A pid>"；`cat /tmp/poly_mm_pro_max.lock` 仍是 A 的 pid；kill A → B 重启正常

**User involvement:** None

**Risk:** 零

---

### Phase 4 — 真实仓位判胜负 (BLOCKER #2)
**Virtue:** Honesty, Proof of Work (8), Integrity (1)

**Tasks:**
1. `run_reversal_live_real` L1655–1681：替换 `kline_color(trade_rows[0]) == win_color` 为 `_settle_from_positions(client, market_id)`，读 `fetch_positions` 或 order resolution endpoint
2. 市场未 closed/resolved 时按 backoff loop 等待，绝不假设 1.0/0.0 结算
3. 真实 fill size（来自 Phase 1）作为 P&L 输入，不用请求 size
4. `_settle_from_positions` 在同一 commit 加 pytest（virtue 6）

**Verification:** 构造 fixture：K 线收红但仓位实际结算 YES → 旧代码 log LOSS，新代码 log WIN matching fixture

**User involvement:** 可选；一个真 cycle 端到端确认

**Risk:** Polymarket 结算可能延迟——backoff 加上限，超时就 push Server 酱 critical 告警让人介入

---

### Phase 5 — Cycle 回滚 (BLOCKER #4)
**Virtue:** Safety, Integrity (1), Resilience

**Tasks:**
1. `run_reversal_live_real` L1615–1625 的 layer 循环外包 try/except；layer N 失败时遍历 1..N-1 已成交层，调 `sell_position_limit` flatten
2. flatten 过程每一步 log + Server 酱推送；最终 flatten 失败时 push critical 告警含所有开放 position ID

**Verification:** Stub 第二层市场查询抛错。layer 1 成交（Phase 1 真实 fill）→ layer 2 raise → log 显示"rolling back layer 1: sold X shares @ Y" → 持仓面板回到 flat

**User involvement:** Stub 测试即可；live 验证可选

**Risk:** 快速行情下 flatten 滑点——但策略持有比 flatten 更糟（martingale 在错的方向追加），仍 flatten

---

### Phase 6 — 错误诚实呈现 (WARN 集合)
**Virtue:** Honesty, Falsifiable Communication (9)

**Tasks:**
1. `derive_api_creds` L598：把 bare `except Exception` 收窄到 SDK 已知类型；把 POLY_1271 原文报错 surface 到 UI 状态栏（消化掉之前 POLY_1271 用户的盲点）
2. 三处 `if isinstance(resp, dict) and resp.get("success") is False` (L1855/1907/2041)：改成显式检查 `status in {"matched","filled"}`，其他状态（含 `unmatched`/`partially_filled`）显式分类抛错或返回带 status 的结果
3. `parse_minimax_json` L1047：regex 收紧；`float()` 包 try/except，caller 把 None 当"无信号"，绝不让坏 LLM 输出 crash worker

**Verification:**
- 故意填错私钥 → UI 状态栏显示"POLY_1271: <SDK 原文>"，不是"无法派生 CLOB API 凭证"
- 对 spread 太大的 market 下单返回 unmatched → cycle 显式 abort，绝无误报 WIN

**User involvement:** Required —— 需要 POLY_1271 凭证路径触发一次

---

### Phase 7 — Resource hygiene
**Virtue:** Resource Stewardship (7)

**Tasks:**
1. 加模块级 lazy `aiohttp.ClientSession`，按当前 asyncio loop 缓存；Binance / gamma helpers 共用
2. 应用关闭时调 `loop.run_until_complete(loop.shutdown_asyncgens())` 再 `loop.close()`

**Verification:** 反转实盘跑 30 min，每分钟 `lsof -p <pid> | grep TCP | wc -l` —— 数字稳定，不线性增长

**Risk:** session 绑错 loop（tab 切换时新 loop） —— 用 `asyncio.get_running_loop()` 当 key 缓存

---

### Phase 8 — 纯函数测试补全
**Virtue:** Test-Driven Diligence (6)

**Tasks:**
1. 新建 `tests/test_pure.py`：覆盖 `reversal_stakes` (L1261)、`reversal_factors` (L1256)、`matching_streak` (L1172)、`kline_color` (L1272)、`clamp_price` (L2159) 含 tick_size 边界、`parse_minimax_json` (L1047) 含恶意输入、`ema`/`rsi` (L2137/2144) (`_extract_fill` / `_settle_from_positions` 已在 P1/P4 自带测试)

**Verification:** `pytest -q` 全绿；故意把 `reversal_factors` win_factor 公式翻号 → 至少一个测试 fail

**Risk:** 无

---

### Phase 9 — Journal + Heartbeat + NaN guard
**Virtue:** Falsifiable Communication (9), Robustness (3)

**Tasks:**
1. 每个 cycle 后 append 一行到 `trade_journal.csv`：`ts, market, cycle, layer, requested_price, fill_price, size, settle, pnl_actual`
2. 反转实盘每 N cycle（默认 10）push 一次心跳到 Server 酱：当前累积 pnl、剩余小时、当前 layer
3. `_float_or_zero` L2183：拒绝 NaN/inf 返回 0.0；附测试

**Verification:** 跑 3 个 cycle → CSV 有 3 行 fill_price ≠ requested 的样本；强制传 NaN → UI 永远显示"0.00"不是"nan"

---

## Explicitly Deferred

| 项目 | 不做的理由 |
|---|---|
| 单文件拆分成 strategies/clients/ui/ | 拆而不改行为=纯重构，违反"不大改"约束 (virtue 4) |
| 4-arg 通用 `_submit_order` helper | 只有 3 个 order 路径，2 用法太少，过早抽象 (virtue 4) |
| 信号融合（反转 + MiniMax 概率） | 是 feature 不是 risk；correctness 落地后再说 |
| 把 fee_rate/min_edge/0.6 等魔数挪到 config | 不修 bug 的美容工作 |
| Slug→gamma 查询替换硬编码模板 | 优先级低于 Phase 1 真实 fill；P1 落地后再看 |
| 资金硬熔断（日内最大亏损 / 余额下限） | 值得做但要等 Phase 1 + Phase 4 真实 P&L 数据可信再上 |
| 退出条件重设计（止盈止损） | 策略增量，等 correctness 全部落地 |
| 多类目市场（NBA/NFL 等） | 之前优化分支的另一条线，跟当前 BTC 反转无关 |

---

## Critical Files

- 主要修改：`/Users/yara/Developer/Projects/polymarket/polymarket-btc-quick-trader/poly_mm_pro_max.py`
- 新建：`/Users/yara/Developer/Projects/polymarket/polymarket-btc-quick-trader/tests/test_pure.py` (Phase 1/4/8/9)
- 新建：`/Users/yara/Developer/Projects/polymarket/polymarket-btc-quick-trader/trade_journal.csv` (Phase 9，自动生成)

---

## Verification Strategy (End-to-End)

Phase 1–5 完成后，按这个顺序验证一遍：

```bash
# 1. 单测
.venv/bin/pytest -q tests/

# 2. 单实例锁
./PolyMarketMaker.command &   # A
./PolyMarketMaker.command     # B → 应失败且不破坏 A 的 lockfile
cat /tmp/poly_mm_pro_max.lock # 应是 A 的 PID

# 3. 最小真金白银闭环（Phase 1 + Phase 6 验证）
# 启动 GUI → 扫描 BTC 15m → 选 spread 不为零的市场 → 买 0.5 USDC
# 期望日志看到：fill_price ≠ limit_price，resp.makingAmount=...
# 期望持仓面板 entry 跟 polymarket.com 一致

# 4. 反转实盘 paper smoke（Phase 4 + Phase 5 验证）
# 用 dev fixture 模拟"K线红但仓位结算 YES" → 日志 WIN 不是 LOSS
# 用 dev fixture 模拟"layer 2 fail" → log 显示 rollback layer 1

# 5. 长跑资源检查（Phase 7 验证）
# 启动反转实盘 30 min → lsof TCP 句柄数稳定不增长
```

**没跑过上面 5 步的 verification，任何 phase 都不算 "完成"**（virtue 1 Definition of Done）。
