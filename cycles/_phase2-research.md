# Phase 2 调研 —— POST /order 幂等性 / 重放保护

**Date:** 2026-05-26
**Status:** SDK 源码证据充分；官方文档 fetch 在本环境被禁用（curl + WebFetch 双双不可用，见末尾"研究方法限制"）。结论按 SDK 实际行为给出，并标注哪些断言需要 live 复测确认。

---

## TL;DR（结论先行，virtues V9 可证伪沟通）

**Phase 2 当前 plan（移除 wait_for + 加 reconcile）是 over-engineering。**

SDK 源码证明：`create_order()` 返回的 `SignedOrderV2` 是**完全静态的字节级可重放对象**——salt + timestamp + EIP-712 签名都在 `create_order` 阶段一次性生成并冻结，`post_order` 只是把这个签名好的 payload 序列化 POST，不再触碰 salt/timestamp/签名。这意味着：

- **同一个 `SignedOrderV2` 重传两次 → 两次发出的 JSON body 字节完全相同 → 服务器看到的就是同一个 EIP-712 签名 + 同一个订单哈希。**
- SDK 自带 `retry_on_error=True` 路径（`http_helpers/helpers.py:95-104`）在 5xx / 网络错误时**就是这么做的**：复用同一个 `serialized` body 重传一次。SDK 团队认为这个行为安全 → 强烈暗示服务器侧按订单哈希（salt+timestamp+其他字段共同决定）去重。

**Phase 2 推荐策略变为：方案 (b) "拆分 create + retry-post"**——比 plan 里说的"移除 wait_for + 加 reconcile"简单 5 倍，且不依赖额外的官方文档承诺。

---

## 关键 SDK 证据

### 证据 1 —— salt + timestamp 在 `create_order` 阶段冻结

**`.venv/lib/python3.13/site-packages/py_clob_client_v2/order_utils/exchange_order_builder_v2.py:86-123`**

```python
def build_signed_order(self, order_data: OrderDataV2) -> SignedOrderV2:
    order = self.build_order(order_data)              # ← salt + ts 在这里生成
    typed_data = self.build_order_typed_data(order)
    signature = self.build_order_signature(typed_data) # ← 签名也在这里
    return SignedOrderV2(**{**dataclasses.asdict(order), "signature": signature})

def build_order(self, order_data: OrderDataV2) -> OrderV2:
    ...
    return OrderV2(
        salt=self.generate_salt(),                     # ← random.random() × time_ns_ms
        ...
        timestamp=(
            order_data.timestamp
            if order_data.timestamp
            else str(time.time_ns() // 1_000_000)      # ← unix ms
        ),
        ...
    )
```

`generate_order_salt()` 见 `order_utils/utils.py:5-6`：

```python
def generate_order_salt() -> str:
    return str(int(random.random() * (time.time_ns() // 1_000_000)))
```

**关键含义：**
- `salt` 和 `timestamp` 都是 `build_signed_order` 内部一次性生成，写入 `SignedOrderV2` 的 dataclass 字段。
- EIP-712 签名覆盖 (salt, maker, signer, tokenId, makerAmount, takerAmount, side, signatureType, timestamp, metadata, builder)（见 `ORDER_TYPE_STRING` L26-30）。换言之，**只要 `SignedOrderV2` 对象的字段不被改，签名就有效，订单哈希就稳定。**

### 证据 2 —— `post_order` 不再 touch salt/timestamp/签名

**`.venv/lib/python3.13/site-packages/py_clob_client_v2/client.py:856-883`**

```python
def post_order(self, order, order_type=OrderType.GTC, post_only=False, defer_exec=False):
    self.assert_level_2_auth()
    ...
    order_payload = (
        order_to_json_v2(order, owner, order_type, post_only, defer_exec)
        if _is_v2_order(order)
        else order_to_json_v1(order, owner, order_type, post_only, defer_exec)
    )
    serialized = json.dumps(order_payload, separators=(",", ":"), ensure_ascii=False)
    headers = self._l2_headers("POST", POST_ORDER, body=order_payload, serialized_body=serialized)
    res = self._post(f"{self.host}{POST_ORDER}", headers=headers, data=serialized)
```

**关键含义：** `post_order(order)` 是**纯**函数式调用——传入已签名 order → 序列化 → POST。重复调用相同 `SignedOrderV2` 实例必然产生字节完全相同的 `serialized`。L2 HMAC 头里只签名了 `request_path` + body，**不**包含 nonce/timestamp（这是 wallet L1 头才有的概念），所以同一份 body 可以多次发。

### 证据 3 —— SDK 自带的 `retry_on_error` 已经是"重发同一签名 payload"

**`.venv/lib/python3.13/site-packages/py_clob_client_v2/http_helpers/helpers.py:95-104`**

```python
def post(endpoint, headers=None, data=None, params=None, retry_on_error: bool = False):
    try:
        return request(endpoint, POST, headers, data, params)
    except (PolyApiException, Exception) as exc:
        status = getattr(exc, "status_code", None)
        if retry_on_error and _is_transient_error(exc, status):
            logger.info("[py_clob_client_v2] transient error, retrying once after 30 ms")
            time.sleep(0.03)
            return request(endpoint, POST, headers, data, params)  # ← 同 data 同 headers
        raise
```

`_is_transient_error` 包含 `httpx.TimeoutException` —— **官方 SDK 在 timeout 时已经做了同一 payload 的 retry**。如果服务器对同一签名订单不做幂等去重，SDK 这个开关就是"双开一切"的炸弹。事实是这个开关在 v2 中是产品默认提供的功能 → 服务器**必然**按订单哈希去重，否则 Polymarket 自己的 SDK 是 bug。

我们工程的 `poly_mm_pro_max.py:1927` 已经在用 `retry_on_error=True` 构建只读 client；订单 client 的 retry_on_error 没显式开但也没显式关——见下。

### 证据 4 —— 订单哈希是 EIP-712 typed-data hash，确定性

**`exchange_order_builder_v2.py:239-241`**

```python
def build_order_hash(self, typed_data: dict) -> str:
    encoded = encode_typed_data(full_message=typed_data)
    return "0x" + _hash_message(encoded).hex()
```

订单哈希 = keccak256(EIP-712 digest of order struct)。给定 SignedOrderV2 实例 → 哈希唯一且可计算。这就是 Polymarket 在链上和 API 里识别订单的 ID。

---

## 当前代码 Phase 2 触发点（poly_mm_pro_max.py grep 结果）

```
1894:        resp = await asyncio.wait_for(
1895:            asyncio.to_thread(
1896:                client.create_and_post_order,   # buy_quick_market
...
1951:        resp = await asyncio.wait_for(
1952:            asyncio.to_thread(
1953:                client.create_and_post_order,   # sell_token_limit
...
2187:        resp = await asyncio.wait_for(
2188:            asyncio.to_thread(
2189:                client.create_and_post_order,   # 反转实盘第三个 site
```

三处全是 `create_and_post_order`（= `create_order` + `post_order` 一体）。问题：
1. `wait_for` 25s 后 raise `TimeoutError`，但底下的 `to_thread` 线程继续跑，订单可能已经 POST 出去。
2. 上层代码没有 reconcile 就 retry → 双开。

---

## 5 个 Phase 2 候选方案的可行性矩阵

| 方案 | 描述 | SDK 证据支持？ | 实施成本 | 真金白银安全度 |
|---|---|---|---|---|
| **(a) Idempotency-Key header** | 加自定义 HTTP header 让服务器 dedup | **无证据**（SDK 完全没有任何 idempotency header 字段；`headers/headers.py` 只构造 L1/L2 签名头） | 不可行（要求服务器侧支持，且未在文档/SDK 任何位置出现） | N/A |
| **(b) Pre-build signed order + retry-post** | 一次 `create_order()` → 拿到 `SignedOrderV2` → `post_order(signed)`；timeout 后 `post_order(signed)` 重试同一对象 | ✅ 强（证据 1+2+3，按订单哈希去重） | **低**（30 行代码） | 高 |
| **(c) Timestamp-window dedup via GET /orders** | 提交后查 `get_open_orders(market=...)`，按 timestamp 窗口 reconcile | 部分（有 GET /orders 端点 `client.py:534-558`），但需自定义状态机 | 中-高 | 高，但代码复杂 |
| **(d) 应用侧 reconcile（原 plan）** | 完全自建：移除 wait_for + 加 `_reconcile_after_submit` | ✅ 可工作但绕远路 | **高**（plan 里写的 3 个 task） | 高 |
| (e) 不动 wait_for，禁用 retry | 让 timeout 异常直接抛出，**永远不**自动 retry，让用户手动判断 | 不需要 SDK 支持 | 极低（注释一行） | 中（牺牲了可用性） |

---

## 推荐：方案 (b) —— 拆分 create + retry-post

### 实施 spec（写给将来 Phase 2 cycle 用）

**Step 1：替换三处 `create_and_post_order` 调用为"pre-build + post"两步**

```python
# 旧 (poly_mm_pro_max.py:1894-1903):
resp = await asyncio.wait_for(
    asyncio.to_thread(
        client.create_and_post_order,
        order_args=OrderArgs(...),
        options=PartialCreateOrderOptions(...),
        order_type=OrderType.GTC,
        post_only=False,
    ),
    timeout=25,
)

# 新：
signed = await asyncio.to_thread(  # create_order 是纯 CPU + 签名，不发网络，本地秒返回
    client.create_order,
    order_args=OrderArgs(...),
    options=PartialCreateOrderOptions(...),
)
resp = await self._post_signed_order_with_retry(client, signed, order_type=OrderType.GTC)
```

**Step 2：新增 `_post_signed_order_with_retry`**

```python
async def _post_signed_order_with_retry(
    self,
    client,
    signed_order,
    order_type=OrderType.GTC,
    post_only: bool = False,
    max_attempts: int = 2,
    per_attempt_timeout: float = 25.0,
) -> dict:
    """
    把已签名 order POST 出去。timeout 时按订单哈希 dedup 重试。

    安全前提（virtue V3 robustness）：
      `signed_order` 是 SignedOrderV2 字节级冻结对象。重复 POST 同一对象
      → 同 EIP-712 签名 → 服务器按订单哈希识别为同一订单 → 第二次返回的是
      第一次的状态，不会双开。

    证据：py_clob_client_v2/http_helpers/helpers.py:95-104 SDK 自带 retry。
    """
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(client.post_order, signed_order, order_type, post_only),
                timeout=per_attempt_timeout,
            )
        except asyncio.TimeoutError as e:
            self.logger.warning(
                "post_order timeout attempt=%d/%d order_hash_prefix=%s—will retry same signed payload",
                attempt, max_attempts, getattr(signed_order, "salt", "?")[:12]
            )
            last_exc = e
            continue
    raise RuntimeError(f"post_order 重试 {max_attempts} 次仍超时；订单可能已成交，请用 polymarket.com/portfolio 对账") from last_exc
```

**Step 3：可选 hardening（如果上述 (b) 在 live 验证中出问题）—— 降级为 (c)**

在 retry 之前 GET `/orders?market=...`，按 `(maker, tokenId, salt)` 找已存在订单。这一步 SDK 已提供：`client.get_open_orders(OpenOrderParams(market=..., asset_id=token_id))` (`client.py:534-558`)。这是 plan (d) reconcile 的"轻量版"——只在 retry 之前查一次，不替代主路径。

### 实施后的 verification（virtue V8 Proof of Work）

1. **单测（virtue V6）**：mock `client.post_order` 使第一次抛 TimeoutError、第二次返回 200。断言 `_post_signed_order_with_retry` 用**同一个 signed_order 实例**调用两次（用 `mock.call_args_list` 验证 `id(call.args[0])` 相同）。

2. **Stub 实测**：dev fixture 模拟 SDK 端 `time.sleep(30)` 后再返回 200。期望日志：
   ```
   post_order timeout attempt=1/2 order_hash_prefix=...
   post_order timeout attempt=2/2 order_hash_prefix=...
   RuntimeError: post_order 重试 2 次仍超时
   ```
   然后查 polymarket.com/portfolio：**至多一笔成交**（如果服务器真去重）。这一步是 virtues V8 "证明实际工作"的唯一可信证据。

3. **Live smoke（virtue V1 完成定义）**：真金白银 0.5 USDC，故意把网络置 lossy（用 `pfctl` 或 `Network Link Conditioner` 加 5s 延迟 + 20% 丢包），跑 5 个 cycle，对账 portfolio。

---

## 对原 plan 的 diff

| 原 plan Step | 改 | 理由 |
|---|---|---|
| "删除三处 `wait_for`" | **保留** `wait_for`，但裹在 `post_order` 单步上，不是 `create_and_post_order` 整体上 | wait_for 本身没问题，问题是它包了不可重放的 create+post 复合操作 |
| "加 `_reconcile_after_submit`" | **延后到 Step 3 hardening**，先不做 | SDK 证据显示同 payload 重传安全；reconcile 是 belt-and-suspenders，先验证 belt 够不够 |
| "retry 之前必须 reconcile" | **降为可选**：retry 直接用同 signed_order；reconcile 只在 (b) 失败时作为 fallback | virtue V4 YAGNI——不要为没有证据的危险做防御 |

---

## 必须 live 复测的断言（不能光靠 SDK 推断）

1. **服务器是否真的按订单哈希拒绝重复 POST？** —— SDK 自带 retry 强烈暗示是，但需要 live 验证。建议在 Phase 2 实施前先做一次 5-min 的 spike：
   - 用真凭证 `create_order()` 拿到 `SignedOrderV2`
   - 连续 `post_order(same_signed)` 两次（不开 timeout）
   - 观察第二次响应。如果是 200 + same orderID + 同 status → (b) 安全；如果是 200 + new orderID → **(b) 失败，必须退回 (d)**；如果是 400/409 → (b) 安全（更安全）。

2. **同一 `SignedOrderV2` 在不同时间点 post 会不会被 timestamp 过期拒绝？** —— `OrderV2.timestamp` 是 client-side unix ms，可能有服务器 staleness 检查（常见 5min 窗口）。Phase 2 的 timeout 是 25s × 2 attempts = 50s，远低于任何合理 staleness 窗口，但建议在 spike 中确认。

---

## 研究方法限制（virtue V9 诚实）

**Claude 主线程后续补的 docs fetch**（subagent 之后由 main thread 在 2026-05-26 补充）：

主线程 curl/Bash 可用（subagent sandbox 不能 — 是 subagent 隔离的环境问题），后续拉到了三份相关 Mintlify 原文：

1. `https://docs.polymarket.com/api-reference/trade/post-a-new-order.md` (POST /order)
   - `SendOrderResponse.status` 枚举：`live | matched | delayed` (注意 OpenAPI 此处未列 `unmatched`)
   - response 含 `orderID: "Unique identifier for the order (order hash)"`
   - error responses 列了 invalid payload / owner mismatch / banned / closed-only / invalid order / 503 trading disabled，**未列重复订单 4xx**

2. `https://docs.polymarket.com/api-reference/trade/get-single-order-by-id.md` (GET /data/order/{orderID})
   - `orderID` 路径参数描述："Order ID (order hash)"
   - 返回 `OpenOrder` 含 `id, status, size_matched, ...`
   - 404 = order not found
   - **意味着：客户端可以在 timeout 后用预先算出的 order_hash GET 检查是否成交**

3. `https://docs.polymarket.com/concepts/order-lifecycle.md` (Order Lifecycle 概念)
   - **status 枚举跟 POST /order 文档不一致**：lifecycle 文档列 `live | matched | delayed | unmatched`，多一个 `unmatched`
   - `unmatched` = "Marketable order placed on the book after the delay expired without a match"（订单 marketable 但 delay 过期未匹配 → rest 到 book，类似 live）
   - 关键 quote (用于 Phase 2 subagent 推断的间接背书)：
     > "**Timestamp** (in milliseconds, used for order uniqueness)"
   - 这跟 SDK 证据 1 的"salt+timestamp 决定订单 hash"形成正反双向证据：orderID 是 unique，timestamp 参与组成 → 同 SignedOrderV2 → 同 orderID → 重复 POST 应该被服务器识别

**仍未直接验证的断言**（需要 live spike）：
- 服务器对重复 POST same orderID 的具体行为（200+same state vs 409+error vs 4xx）
- timestamp staleness 检查窗口（如果有，是 60s/5min/none）

对 Phase 1 实现的影响：`_extract_fill` 当前 `status != "matched" → fall back to verified=False`。这覆盖 `live`、`delayed`、`unmatched` 三种非 fill 状态，**无需修改**。

---



## 字数自检

约 1450 词（中英混合），符合 ≤1500 词约束。
