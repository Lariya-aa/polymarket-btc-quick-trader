---
cycle_id: virtues-phase-2
commit_sha: bcf7559
branch: ai-cycle/virtues-phase-2
parent: 634c028 (main)
date: 2026-05-26
deferred_warn: duplicate-response handling (no docs evidence; needs live spike)
live_spike_required: true
---

# cycles/virtues-phase-2/agent.md

## Virtues
V1✓ V2✓ V3⚠ V4✓ V5✓ V6✓ V7 n/a V8⚠ V9✓

V3/V8 marked ⚠ not ✓: server-side dedup is INFERRED from SDK source + indirect docs evidence (orderID = order hash, timestamp "for order uniqueness", SDK retry_on_error path), not OpenAPI-documented. Live spike confirmation owed before declaring fully verified.

## Files
- `poly_mm_pro_max.py` modify +50/-25 (new `_post_signed_order_with_retry` helper + 3 sites refactored: buy_quick_market / sell_token_limit / sell_position_limit split create_order+post)
- `tests/test_retry_post.py` new +110 (5 cases verifying retry uses same signed instance)

## Key contract
`_post_signed_order_with_retry(client, signed_order, order_type, post_only, max_attempts=2, per_attempt_timeout=25.0)` — retries the SAME SignedOrderV2 on TimeoutError. Non-Timeout exceptions propagate without retry.

## Codex review
verdict=BLOCK (false positive on plan_coverage for untracked test file). 1 deferred warn (duplicate-response handling — no docs evidence, awaiting live spike).

## Test results
`pytest tests/` → **42 passed** (12+6+10+9+5)

## What's NOT done in this cycle
- **Live spike**: need to confirm server dedup behavior with real traffic. See handoff.
- **Duplicate-response patch**: requires knowing actual server response shape (200+same orderID vs 4xx). Defer to Phase 2.1 after spike.

## Schema source
SDK source code (3 files) + 3 docs URLs documented in `cycles/_phase2-research.md`.

## Live verification owed (user, REQUIRED before declaring Phase 2 safe)
1. Capture a real SignedOrderV2 (set breakpoint or log raw `signed` in `_post_signed_order_with_retry` for one buy)
2. Monkey-patch `client.post_order` to `time.sleep(30)` first call, then real POST second call
3. Trigger small (0.5 USDC) buy via GUI
4. Expected: log shows `post_order 超时 attempt=1/2 ... attempt=2/2 成功`
5. Check polymarket.com/portfolio: should be **exactly ONE order** (not two)
6. If two orders: Phase 2.1 must add reconcile-before-retry (server doesn't dedup) — file as new BLOCKER
