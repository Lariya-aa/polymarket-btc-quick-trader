# MARKETS.md — Multi-category market support

> 之前是 BTC-only 应用，现在支持 NBA / NFL / 世界杯 / 新上线盘口。
> 这份文档说明**怎么加新类目**——从代码角度而不是 UI 操作。

---

## 现状（6 commit 落地于 `optimization/virtues-pass`）

| commit | 主题 |
|---|---|
| `fd4962f` | refactor: QuickMarket → PolyMarket; up_*/down_* → yes_*/no_* |
| `5cc16e8` | feat: 通用 `fetch_tag_markets` + `fetch_newly_listed_markets` + CATEGORIES |
| `819b837` | feat(ui): 类目下拉框 + 注册表分发 |
| `9e7829b` | refactor(signal): 按类目分发 AI 本地信号 |
| `ac5689c` | test: 11 个新测试覆盖注册表 / fetcher / 分发 |
| `(this commit)` | docs: MARKETS.md |

---

## 架构

```
                   ┌──────────────────────────────────────┐
                   │ UI: ttk.Combobox (cbo_category)      │
                   │      [ BTC – 短周期      ▾ ]         │
                   │      [ NBA ]                         │
                   │      [ NFL ]                         │
                   │      [ 世界杯 ]                       │
                   │      [ 新上线盘口 ]                    │
                   └────────────────┬─────────────────────┘
                                    │  user 点"扫描"
                                    ▼
              scan_quick_button_clicked()
                                    │
                                    │ 读取 cbo_category.get()
                                    ▼
                  CATEGORIES[label] → (code, method_name, kwargs)
                                    │
                                    │ getattr(self, method_name)(**kwargs)
                                    ▼
              ┌─────────────────────┴───────────────────────────┐
              ▼                                                 ▼
  fetch_quick_btc_markets()                    fetch_tag_markets(tag_slug=..., ...)
  (HTML scrape + slug fallback)                fetch_newly_listed_markets(...)
              │                                                 │
              │                                                 │
              └────────────┬────────────────────────────────────┘
                           ▼
                  _build_market(event, market, now, category, subject)
                           │
                           ▼
                  PolyMarket(category="...", subject="...", ...)
                           │
                           ▼
                  render_quick_markets → Treeview 表格
```

AI 按钮独立一条链：

```
                  predict_quick_button_clicked()
                           │
                           │ 读 selected market.category
                           ▼
                  _local_signal_for(market)
                           │
              ┌────────────┴────────────┐
              ▼ category=="BTC"         ▼ else
  fetch_btc_signal(...)         minimal market-quote shell
  (Binance K线 + 启发式)         (only quotes; no prob)
              │                         │
              └────────────┬────────────┘
                           ▼
              fetch_minimax_prediction(signal, market)
                  (MiniMax-M2.7, JSON-mode)
                           │
                           ▼
                  render_signal(signal)
                  (双形态: BTC 显示本地 prob+RSI；
                  其他只显示 LLM)
```

---

## 怎么加一个新类目

**全部改动 = `CATEGORIES` 注册表里加一行**，假设要加"电竞"：

```python
# poly_mm_pro_max.py 顶部
CATEGORIES = {
    "BTC – 短周期":   ("BTC", "fetch_quick_btc_markets",    {}),
    "NBA":            ("NBA", "fetch_tag_markets",          {"tag_slug": "nba", "category": "NBA", "subject_label": "NBA"}),
    "NFL":            ("NFL", "fetch_tag_markets",          {"tag_slug": "nfl", "category": "NFL", "subject_label": "NFL"}),
    "世界杯":         ("WC",  "fetch_tag_markets",          {"tag_slug": "fifa-world-cup", "category": "WC", "subject_label": "WC"}),
    "新上线盘口":     ("NEW", "fetch_newly_listed_markets", {}),
+   "电竞":           ("ESPORTS", "fetch_tag_markets",      {"tag_slug": "esports", "category": "ESPORTS", "subject_label": "电竞"}),
}
```

下次启动 GUI，下拉框就多一项。`fetch_tag_markets` 是通用的——它对任何 Polymarket tag 都工作，只要 Gamma API 有对应 `/events?tag_slug=...` 数据。

**找正确的 tag_slug**：

```bash
curl -s 'https://gamma-api.polymarket.com/tags?limit=500' \
  | python3 -c "import json,sys; [print(t['label'],'|',t['slug']) for t in json.load(sys.stdin) if 'esport' in (t.get('slug') or '').lower()]"
```

---

## 怎么给新类目加本地分析层（"保留后续其他分析数据接口"）

目前只有 BTC 有本地启发式（Binance K线 + EMA/RSI/动量）。其它类目调 `_local_signal_for` 时返回的是 market-quote shell——只把盘口信息喂给 MiniMax。

要给 NBA 加自己的本地分析（比如查 ESPN 战绩 API 给出胜率估计）：

```python
async def _local_signal_for(self, selected_market: PolyMarket | None):
    category = selected_market.category if selected_market else "BTC"
    if category == "BTC":
        return await self.fetch_btc_signal(selected_market)
+   if category == "NBA":
+       return await self.fetch_nba_signal(selected_market)
    # market-quote shell fallback
    ...

+async def fetch_nba_signal(self, market: PolyMarket):
+    # 拉 ESPN 战绩 / Elo / 主客场 / 伤病等，算 prob_up
+    return {
+        "category": "NBA",
+        "fetched_at": datetime.now().strftime("%H:%M:%S"),
+        "market_period": "",
+        "market_question": market.question,
+        "prob_up": 0.62,                # 你算出来的 Yes 概率
+        "prob_down": 0.38,
+        "confidence": 0.4,              # 0..1
+        "ema_fast": 0, "rsi": 50,        # render_signal 引用了这两个，给 0 / 50 不影响展示
+        # ... 其他你想喂给 MiniMax 的 features
+    }
```

`render_signal` 看到 `prob_up` 就走 "本地+LLM" 渲染路径；MiniMax 收到完整的 local signal + market block。

---

## PolyMarket dataclass 字段速查

```python
@dataclass
class PolyMarket:
    slug: str           # 市场 slug, 用于下单 / 跳转
    event_slug: str     # 父事件 slug, 用于 polymarket.com/event/<slug>
    question: str       # "Will Lakers win?" / "Bitcoin Up or Down 5m?"
    yes_id: str         # CLOB token ID for Yes
    no_id: str          # CLOB token ID for No
    tick_size: str      # "0.01" / "0.001" / ...
    period: str         # BTC: "5m"/"15m"/"1h"/...; else: ""
    end_dt: datetime | None
    ended: bool
    yes_bid: float      # best Yes 买价
    yes_ask: float      # best Yes 卖价
    no_bid: float       # 1 - yes_ask
    no_ask: float       # 1 - yes_bid
    spread: float       # yes_ask - yes_bid
    volume24h: float
    category: str = "BTC"   # "BTC" / "NBA" / "NFL" / "WC" / "NEW" / ...
    subject: str = ""       # 表格第一列显示文字; BTC 为 "5m" 等
```

---

## 已知未做

| 项 | 状态 | 理由 |
|---|---|---|
| Tab UI（每个类目一个 tab） | 没做 | 你选了 Combobox 方案 |
| 多 outcome 市场（W/D/L 三选一） | 没做 | 当前架构全是 Yes/No；Polymarket 大部分本来就是 Yes/No。需要的时候再说。 |
| NBA/NFL 本地启发式 | 没做 | 留接口；你说"保留后续接口"。框架已经具备：见上方"怎么加本地分析"。 |
| 类目颜色区分 / 图标 | 没做 | Treeview 不便加 tag-style 着色；如果需要可以加 `tags` 参数染色。 |

---

## 怎么验证

```bash
.venv/bin/pytest tests/ -v          # 78 passed
.venv/bin/python poly_mm_pro_max.py  # 启动 GUI
# 依次切换 5 个类目 + 点"扫描"，每个都应返回若干市场
# AI按钮：BTC 走老路；其它走 MiniMax-only
```

---

*最后更新：2026-05-23*
