# OPTIMIZATION-LOG.md

> 这份文档记录 `optimization/virtues-pass` 分支上每一个 commit 的来龙去脉，
> 以及**有意没做的事**和为什么。**main 分支没动**。
>
> 上下文：上一轮按 `~/.claude/skills/virtues` 的 9 条原则做了静态分析
> （13 项 P0-P3 发现），本分支按"风险从小到大、能在不重启 GUI 的前提下
> 验证的优先"顺序，挑可单元化的部分落了 10 个 commit。

---

## 怎么验证整个分支

```bash
# 1. 拉这个分支
git checkout optimization/virtues-pass

# 2. 建一个 venv（避免污染系统 Python）
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt

# 3. 跑测试
.venv/bin/pytest tests/ -v          # 期望: 59 passed

# 4. 手测 GUI（这步我没法替你做）
./PolyMarketMaker.command           # 启动后只点"扫描短周期"+"AI概率判断"
                                    # 别点买/卖。只是确认 GUI 还能加载、
                                    # 概率数字仍然出现在 ~[27%, 73%] 区间
```

---

## Commit 清单（10 个，按时间倒序）

| # | SHA | Subject | Virtue |
|---|-----|---------|--------|
| 10 | `6b4cf16` | test: pytest baseline covering 11 pure helpers (59 tests, 0 failures) | 6, 8 |
| 9 | `41ff81e` | chore(deps): add requirements.txt + requirements-dev.txt + pytest.ini | 1 |
| 8 | `17acb29` | chore(shell): add set -euo pipefail to PolyMarketMaker.command | 5 |
| 7 | `e5e9503` | fix(scan): include 1h and 1d in generated BTC slug fallback | 3 |
| 6 | `3605d02` | fix(positions): distinguish API failure from genuinely empty positions | 3, 9 |
| 5 | `a294088` | fix(order): client_order_id + timeout-safe behavior on buy/sell | 3, 8 |
| 4 | `2dbf28f` | fix(input): reject inf/nan from quick-buy form fields | 3 |
| 3 | `816613c` | fix(ui): clarify MiniMax-unavailable label | 9 |
| 2 | `d6f7d7e` | refactor(signal): name probability-model constants + use math.exp | 5, 8 |
| 1 | `1720757` | fix(logger): route load_config_from_local error to UI log panel | 1, 9 |

每个 commit 自己的 message 写得比较完整（`git show <sha>` 看），下面只补充
**整体逻辑、风险点、回滚方法、做了/没做什么**。

---

## 每个 commit 的"为什么"

### 1. `1720757` — logger 路由修复

**问题**：`load_config_from_local` 的 except 用了 `logging.error(...)`（root logger），不是 `self.logger.error(...)`。`TkinterLogHandler` 只挂在 `self.logger` 上，所以**配置文件读失败这条信息永远不会出现在 UI 日志面板**——只会写到 `poly_mm_pro_max.log` 这个文件里，用户多半看不见。

**修复**：一行替换。

**回滚**：`git revert 1720757`，影响零。

### 2. `d6f7d7e` — 概率模型常量命名 + math.exp

**问题**：`fetch_btc_signal` 里有六个魔法数字（`0.50/0.35/0.15`、`3.0`、`2.0`、`0.6`、`2.718281828`），全部没注释。其中 `0.6` 是把 sigmoid 输出朝 0.5 收缩的关键系数，决定了 UI 最终显示概率的实际区间约为 `[0.27, 0.73]`——这是策略最重要的事实之一，但代码里看不出来。

**修复**：
- 把 6 个常量提到模块顶部，命名为 `PROB_MOMENTUM_FAST_WEIGHT` 等
- 加 8 行 header 注释，**明确写了"NOT back-tested"**
- 顺手把 `pow(2.718281828, -z)` 换成 `math.exp(-z)`（行为不变）
- 顺手加了 `import math`、`import uuid`（uuid 是下一个 commit 要用的）
- 顺手加了 `ORDER_SUBMIT_TIMEOUT_SECONDS = 25` 常量

**回滚风险**：纯文本重命名 + 等价数学。tests/test_signal_math.py 跑通即证明等价。

### 3. `816613c` — UI 文案

**问题**：MiniMax 调用失败时 UI 显示"MiniMax失败，已用本地信号"，暗示有切换动作；实际上**本地信号一直在算**，MiniMax 只是叠加层。文字误导用户以为系统做了什么 fallback。

**修复**："MiniMax 不可用，仅本地概率"——准确描述实际发生的事。

### 4. `2dbf28f` — 输入硬化

**问题**：`buy_selected_quick_market` 用 `float(...)` 解析买入金额和最高价，Python 的 `float()` 接受 `"inf"`、`"-inf"`、`"nan"` 作为合法输入。下面的 `usdc_amount > 0` 不会拦住 `inf`（`inf > 0` 是 True）。粘贴攻击或意外输入可能溜过去。

**修复**：加 `math.isfinite()` 检查，弹明确的错误框。

### 5. `a294088` — **本分支最重要的一个 commit**：订单幂等 + Timeout 安全

**问题**：买/卖订单都用 `asyncio.wait_for(..., timeout=25)` 包裹 CLOB 调用。**超时不等于订单被拒绝**——可能交易所已经接单，只是响应路径丢了。原代码 raise 的是普通 `RuntimeError` 或 `TimeoutError`，被外层 worker 包装成"快速买入失败: TimeoutError"。用户读到"失败"很可能再点一次按钮，造成**双下单**。

**修复**：
- 每次下单前 `client_order_id = str(uuid.uuid4())`
- 在调用 create_and_post_order **之前**把 uuid 打到日志里——这样即使后面整个进程挂掉，日志里也存了"我刚刚发起过这个 attempt"的证据
- 显式捕获 `asyncio.TimeoutError`，重新 raise 成一条措辞强硬的 RuntimeError：

  > 提交买入订单超时（client_order_id=...）。订单状态未知，可能已被交易所接收。请打开 https://polymarket.com/portfolio 核对后再决定下一步，切勿直接点重试。

- 同样改 `sell_position_limit`
- 把 `timeout=25` 换成模块常量 `ORDER_SUBMIT_TIMEOUT_SECONDS`

**没做的事**：把 `client_order_id` 实际传给 `OrderArgs(...)`。原因：我没有 `py_clob_client_v2` 的源码，不确定它是否接受这个字段；硬塞可能 raise。当前 uuid 只在客户端日志里做"对账锚点"——你按时间戳去 Polymarket 历史里查能配上。**后续 TODO**：确认 OrderArgs 接受 `client_order_id` 后改成服务端幂等。

**回滚风险**：中等。改了实际下单路径。我没法跑真实下单测试，请你**先用最小金额（USDC=5）走一次买入，再用同样的仓位走一次卖出**确认没回归。

### 6. `3605d02` — fetch_positions 错误区分

**问题**：`fetch_positions` 对"无持仓"和"API 挂了"都返回 `[]`。UI 看到 0 条都以为是空仓——但其实可能 API 网络挂了，用户基于"我已经清仓"的错觉去下新单。

**修复**：
- 给类加一个 `self.last_positions_fetch_error: str | None = None` 实例属性（在 `__init__` 初始化）
- fetch_positions 异常路径里 set 这个属性 + log ERROR（不再 WARNING）
- `refresh_positions_button_clicked` 读这个 flag，输出有区分的 log：
  - 成功路径："已刷新持仓: N 条"
  - 失败路径："⚠ 持仓接口失败 (HTTP 502) — 上方显示的可能不是最新持仓..."

**没改返回类型**：还是返回 list。其他调用方（`push_trade_result`）行为不变。

### 7. `e5e9503` — HTML scrape 兜底扩展

**问题**：扫描 BTC 短周期市场首先 scrape `polymarket.com/crypto/bitcoin` 这个 HTML 页面找 `/event/` 链接。如果前端结构变了或被地区屏蔽，就走 `generated_btc_updown_slugs` 兜底——但兜底只覆盖 5m/15m/4h，**1h 和 1d 是裸的**，扫不到。

**修复**：兜底加上 1h（3600s）和 1d（86400s）两个周期，每个周期 ±2 个时间偏移，共 25 个 slug。tests/test_compact_signal_and_slugs.py 里验证了。

### 8. `17acb29` — shell 一致性

`PolyMarketMaker.command` 没 `set -euo pipefail`，其它三个 shell 脚本都有。补齐。

### 9. `41ff81e` — requirements 文件

之前 README 让用户裸跑 `pip install aiohttp py-clob-client-v2`，没锁版本、新机不可复现。加：
- `requirements.txt`：运行时（aiohttp + py-clob-client-v2）
- `requirements-dev.txt`：`-r requirements.txt` + pytest
- `pytest.ini`：把测试目录/约定固定

### 10. `6b4cf16` — pytest 基线（59 测试）

**为什么必须有它**：上一份 virtues 报告的 Virtue 6 + 8 双双零分。**没有测试 = 任何后续 refactor 都是裸奔**。本分支前 9 个 commit 都是单点 fix，第 10 个 commit 给项目装上了 safety net，让你后面（以及 trio 流水线）有 verification 命令可填。

**测试设计要点**：
- 全部目标方法是**纯函数**（不用 self）
- `tests/conftest.py` 在 import `poly_mm_pro_max` 之前 stub 掉 tkinter，让测试在**没有 Tk 环境**（Homebrew python@3.14、CI runner、Docker）也能跑
- `bag` fixture 用 `__getattr__` 把方法绑到一个空对象，**完全绕过** PolyQuickTrader 的 `__init__`（避免它的 setup_ui、open log file、read config 等副作用）
- 59 个测试覆盖：
  - `clamp_price` / `price_decimals`：5
  - `parse_minimax_json`（含 `<think>` 剥离、JSON 嵌在散文里、越界 prob 钳制）：9
  - 各 `_helper`（_float_or_zero、_parse_token_ids、_parse_datetime、_book_level_value）：17
  - 信号数学（window_return、ema、rsi、market_horizon_minutes）：10
  - quick_market_candidate（13 个边界 case）：13
  - compact_signal + generated_btc_updown_slugs：6
- 跑：`.venv/bin/pytest tests/` → **59 passed in 0.10s**

---

## 有意没做的事（重要）

按上一份 wf-goal 使用方法的建议，**单次自动化里不该碰的事**：

| 没做 | 理由 | 你回来后该怎么办 |
|---|---|---|
| **拆分 1227 行单文件** | 这是 P2 项，正确做法是先有测试基线再拆，且必须本地启 Tkinter 验证 GUI 仍能加载。我无法启动 GUI，拆完没法证明没回归。Tests 基线现在已经有了，剩下来你来。 | 开新分支 `optimization/split-monofile`，把 trader / signal / minimax_predictor / polymarket_client / ui_layout 拆到独立模块，每次拆一块跑 `pytest && ./PolyMarketMaker.command`。 |
| **`aiohttp.ClientSession` 全局复用** | 涉及生命周期（GUI 启动时建 / 退出时关），改动跨多个方法。trio 跑这种 refactor 很容易过度。 | 自己开 `optimization/reuse-aiohttp-session`，注入 `self._session`，在 `__init__` 创建、`acquire_single_instance_lock` 释放路径里清。 |
| **worker pattern 去重** | 5 处重复但 cosmetic，对线程安全的破坏面比收益大。 | 等真正需要新增异步操作时再说。 |
| **跑 `wf ai-trio`** | 工具的人工门禁不能跳过，你不在终端，piping `yes` 会在你不看 diff 的情况下让 Kimi 写的代码自动 commit。Real-money 项目不该这样。 | 你回来后想用 trio 跑哪一项（推荐第一次跑 split-monofile），按 `~/Developer/学习/wf-goal-使用方法学习/06-端到端闭环.md` 操作。 |
| **OrderArgs 传 client_order_id** | 不确定 `py_clob_client_v2.OrderArgs` 是否接受这个 kwarg；硬塞可能 raise。 | 跑 `python3 -c "import inspect, py_clob_client_v2; print(inspect.signature(py_clob_client_v2.OrderArgs))"` 看签名；如有 `client_order_id` 字段，在 a294088 commit 的两处 `OrderArgs(...)` 调用里加上即可。 |

---

## "全部流程"按本项目的实际形态走了什么

本来 wf-goal 完整闭环是 `search → use → 编辑 plan → 新分支 → wf ai-trio → 看 diff → y/N`。本分支没用 trio，**把 trio 的"实施者+审查者"两个角色合并成"我直接当 Planner+Implementer，commit message 当审查报告，你后续阅读当人工门禁"**：

| 原 wf-goal 阶段 | 本次实际形态 | 产物 |
|---|---|---|
| `wf goal search` | virtues 报告里把 13 项发现按 P0-P3 排序 | 上一轮回答 |
| `wf goal use` | 不落 plan 文件，因为不会跑 trio；plan 等价物在 commit message 里 | 10 条 commit message |
| `git checkout -b` | ✅ `optimization/virtues-pass` | 这条分支 |
| Kimi 实施 | 我直接 Edit/Write | 10 个 commit |
| Codex 审查 | 每个 commit 里写"Why / 风险 / 回滚"段 | commit message |
| 人工门禁 | 你接下来 `git log -p main..HEAD` 审 | （等你来） |
| commit | 已落到本分支 | ✅ |

---

## 你回来后建议的 5 步

```bash
# 1. 看分支总览
git log --oneline main..HEAD

# 2. 逐 commit 审 diff（最重要的两个）
git show a294088       # 订单幂等（real-money 改动）
git show 3605d02       # positions 错误区分

# 3. 装依赖跑测试
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest tests/ -v

# 4. 启 GUI 手测（重要 — 我没法替你做）
./PolyMarketMaker.command
#   - 只点 "扫描短周期" / "AI概率判断" / "刷新持仓"
#   - 看日志面板里有没有错误
#   - 不要碰买/卖按钮（除非你想用最小金额做端到端测）

# 5. 满意就合并
git checkout main
git merge --no-ff optimization/virtues-pass
git branch -d optimization/virtues-pass
```

---

## 统计

- **commits**: 10
- **文件变动**: +2 个 shell 改动 + 1 个 Python 文件改动 + 7 个新文件
- **新增测试**: 59 个，全部通过，0.10s 跑完
- **未做**: 3 项（拆文件、Session 复用、worker dedup），见上表
- **风险最高的 commit**: `a294088`，需要你最小金额手测一次买/卖

*生成时间：2026-05-22*
