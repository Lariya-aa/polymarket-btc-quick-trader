# cycles/virtues-phase-6/handoff.md

> Commit `6f422a0`. Phase 6 = 让错误对用户可见，而不是悄悄走 logger 然后 UI 卡住。三件事：POLY_1271 凭证报错 surface 到 UI、MiniMax JSON 解析鲁棒、订单响应 status 显式 allowlist。

## 干了啥

### 1. POLY_1271 用户调凭证看到的事

以前 `derive_api_creds` 失败：
```
[ERROR] 派生 CLOB API 凭证失败: ...
```
**仅在背景 log 里**。GUI 上看就是"无法派生 CLOB API 凭证"一行，不知道原因。POLY_1271 deposit wallet 用户调"signer != API KEY"完全猜不到。

现在：
- 错误 surface 到 `lbl_quick_signal` 状态栏，文字红色，前缀 `⚠️ CLOB 凭证派生失败:` + SDK 原文
- `self.last_credential_error` 实例属性记录最后一次错误（caller 可 inspect）
- `logger.error(..., exc_info=True)` 把完整 traceback 写到 log file 供事后调试
- 成功后清 `self.last_credential_error = None`

### 2. MiniMax JSON 解析不再 crash

以前 `parse_minimax_json` 两个雷：
- `re.search(r"\{.*\}", ...)` greedy regex—LLM 返回有 reasoning chain + 最终 JSON 时会一锅端
- `float(parsed.get("prob_up", 0.5))` 没 try/except—字符串 `"0.62%"` 会 crash 整个 AI worker

现在：
- `re.findall(r"\{[^{}]*\}", ...)` 取最后一个平衡 `{...}` 块（LLM 通常 reasoning-then-final 模式）
- 新 `_safe_prob(value, default)` 静态 helper：try-except + NaN/Inf 拒绝 + 范围 clamp [0, 1]，失败回 default

### 3. 订单响应 status 显式 allowlist

新增 `_assert_order_response_ok(resp, action_name)` 静态 helper，按 Polymarket order-lifecycle 文档枚举只允许 `live / matched / delayed`。三处替换 `if isinstance(resp, dict) and resp.get("success") is False: raise ...`：
- `buy_quick_market`
- `sell_token_limit`
- `sell_position_limit`

## Codex 抓的 V3 真 blocker（已 inline 修）

原 plan 写：
```python
if status and status not in {"live", "matched", "delayed"}:
    raise ...
```

**`if status and ...` 让空 status / 缺失 status 漏过**——server 返回 `{"success": True, "orderID": "0x..."}` 没 status 字段时不报错，**流入** push_trade_result 等下游。

修法去掉 `status and`：
```python
if status not in {"live", "matched", "delayed"}:
    raise RuntimeError(f"... 状态 '{status or '<missing>'}'，订单未稳定落地，请去 polymarket.com/portfolio 对账")
```

加 2 个 regression test：`rejects_missing_status` / `rejects_empty_status`。

## 你看到的差异

凭证错时：GUI 状态栏 `⚠️ CLOB 凭证派生失败: PolyApiException[...]` 红色，能复制粘贴去 grep。

MiniMax 返回怪数据时：log 一行 `MiniMax prob 解析失败用 default 0.5`，UI 仍展示"AI 中立"而不是 crash。

下订单响应缺 status：明确 RuntimeError 中断 + 指引去 polymarket.com/portfolio 对账。

## 你要做的 live verification

- 故意填错私钥 → 看红色状态栏
- 让 MiniMax 返回 `{"prob_up": "0.62%"}` → 不 crash + log 'prob 解析失败用 default'
- mock CLOB 返回 `{"success": True, "orderID": "0xabc"}`（无 status）→ 看 RuntimeError 中断
