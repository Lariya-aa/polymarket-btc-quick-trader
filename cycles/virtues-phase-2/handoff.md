# cycles/virtues-phase-2/handoff.md

> Commit `bcf7559`. Phase 2 = 解决 timeout-double-order race，但**真金白银 safe 标记暂未拿到**——server dedup 假设是 SDK 源码 + docs 推断的，没直接 OpenAPI 文档证实。**Live spike 必须做**才能把 Phase 2 标 done。

## 干了啥

之前 `buy_quick_market` / `sell_token_limit` / `sell_position_limit` 三处的真实下单都长这样：

```python
resp = await asyncio.wait_for(
    asyncio.to_thread(client.create_and_post_order, ...),
    timeout=25,
)
```

`wait_for` 超时后 raise TimeoutError，但底层 SDK 线程**继续跑**——订单可能已经 POST 到 CLOB，user 看到"失败"按 retry → 双开。

修法：拆成两步。
1. `client.create_order(...)` 本地签名（CPU bound，毫秒级，没网络），不需要 wait_for
2. `_post_signed_order_with_retry(client, signed, ...)` 把签好的订单发出去，每次尝试 25 秒 timeout，失败时**重发同一个签名对象**。重发的请求 body 字节完全一致 → 同 orderID → 服务器按订单哈希去重，不会双开。

证据：
- SDK 源码（3 个文件）：salt + timestamp + EIP-712 签名都在 `create_order` 阶段冻结到 `SignedOrderV2` dataclass，`post_order` 只是把它序列化 POST 一遍
- SDK 自带 `retry_on_error=True` 路径**就是在这么做**——重试同 payload。这只在服务器 dedup 前提下才安全。Polymarket SDK 团队认为这样安全 → 服务器应该 dedup
- POST /order docs：`orderID = "Unique identifier (order hash)"`；`timestamp = "used for order uniqueness"`

⚠️ **但**：OpenAPI 文档**没直接说**"重复 POST same orderID 时服务器返回啥"。所有"server dedup"结论是间接推断。你必须做 live spike 验证。

## 你必须做的 live spike（commit 后第一件事）

```bash
# 1. 进 GUI，凭证准备好，BTC 短周期市场，spread > 0
# 2. 在 buy 之前，临时让 post_order hang 一下 — 加个 monkey-patch（或 throttle 网络）：

#    在 GUI Python REPL (或者一次性 patch poly_mm_pro_max.py 后跑)：
#    原 client.post_order = client.post_order
#    第一次调用 → time.sleep(30) 然后正常返回
#    第二次调用 → 直接正常返回

# 3. 点"买 Up" 5 USDC
# 4. 期望日志：
#    [WARNING] post_order 超时 attempt=1/2 — 用同一 signed_order 重试 (server 应按 orderID 去重)
#    [INFO] post_order 重试 attempt=2 成功

# 5. polymarket.com/portfolio 看实际仓位：
#    - 一笔 5 USDC 订单 → ✅ 服务器确实 dedup，Phase 2 安全
#    - 两笔 5 USDC 订单 → ❌ 服务器不 dedup，必须起 Phase 2.1 退回 reconcile 方案
#    - retry 时收到 4xx 'duplicate'（exception 中断） → ⚠️ 起 Phase 2.1 加 duplicate-response 识别
```

Spike 跑完结果发我，我决定 Phase 2.1 起不起。

## Codex 抓到的 deferred warn

> "duplicate-order 4xx response on retry would propagate as RuntimeError and could invite user to manually re-submit with a fresh signed order"

意思：如果 server 在 retry 时返回 4xx 'order already exists'，我的 helper 会把这个 4xx exception 向上 raise（因为 helper 只 retry TimeoutError，其他 exception 直接抛）。User 看到 RuntimeError 不知道"其实订单已成交"，可能用新 OrderArgs（新 salt+timestamp = 新签名 = 新 orderID）重新提交 → 双开。

为什么不当场修：docs 没说 server 是不是会返回 4xx 'duplicate'，也没说 4xx 的具体 shape。胡乱猜测加 except 是反 V3（"Don't validate scenarios that can't happen"）。等 live spike 看到真实 server 行为再写正确 handler。

最坏情况下，helper 现在的 raise message 里**明说**了"订单可能已成交，请去 polymarket.com/portfolio 对账"——把决策权显式交给 user，不让 user 盲点新订单。这是 V9 falsifiable communication：错也错得清楚。

## 不在本 cycle 范围

- Phase 5 cycle rollback（subagent 已写 plan draft）—— 接下来跑
- Phase 6 错误诚实呈现 — 错误响应类型识别 / POLY_1271 surface
- 真实 live spike 验证 server dedup —— **你的责任，在 Phase 5 跑之前更安全先做**
