---
cycle_id: virtues-final-patches
commit_sha: 1b4e4a8
branch: ai-cycle/virtues-final-patches → main
parent: 0e8d902 (main)
date: 2026-05-26
type: cross-phase hotfix bundle (not a standard ai-trio cycle)
triggered_by: codex review --base origin/main on prior HEAD
---

# cycles/virtues-final-patches/agent.md

## Virtues (all PASS after patch)
V1✓ V2✓ V3✓ V4✓ V5✓ V6✓ V7✓ V8✓ V9 n/a

## Why this is not a standard ai-trio cycle

After all 10 phases (1/2/3/4/5/6/7/8/9/11) landed via the
plan → Kimi → Codex review → human gate flow, a final `codex review
--base origin/main` ran against the full 26-commit diff. Codex flagged
3 cross-phase issues that span multiple cycles. Since each finding is
small (~5 lines), bundling them into one hotfix commit was more
economical than spawning 3 separate ai-trio cycles. No Kimi
implementation pass — Claude wrote the patches directly.

## Files
- `poly_mm_pro_max.py` modify +33 / -8
- `tests/test_settle.py` modify +21 / -0 (1 new test)
- `tests/test_daily_report.py` modify +48 / -9 (3 new tests, 2 updated)

## Findings closed

### [P1] _settle_from_positions absent-state machine
**Before**: HTTP error between two successful "asset absent" polls did NOT reset `last_state`. Sequence `absent → 503 → absent` was classified as 2 absent polls = LOSS, even though only 1 was actually a successful absent. Single transient 503 could flip still-settling cycle to false LOSS → wrong-direction martingale doubling at next layer.
**After**: except branch sets `last_state = None`. Contract is now "two CONSECUTIVE successful absent polls", not "two-total".
**Test**: `test_settle_absent_then_http_failure_then_absent_resets_streak`

### [P2] _aggregate_daily_journal cross-cycle double-count
**Before**: journal writes one row per layer; WIN layer's `pnl_estimate` already subtracts `accumulated_loss` (Phase 9 invariant). Summing every row across a lose-then-win cycle double-counted prior losses (e.g., showed -2.522 for cycle that actually netted +2.978).
**After**: per-cycle dedup. For each `(strategy, cycle)`, if any row has `outcome=="win"`, that row's pnl IS the net cycle pnl (no further summation). Otherwise sum all rows.
**Test**: `test_aggregate_does_not_double_count_late_win_cycle`. Two existing tests updated to use distinct cycles (multiple WIN rows in one cycle is not a real shape).

### [P2] _get_aiohttp_session stale-loop accumulation
**Before**: each GUI-click ran in fresh `asyncio.new_event_loop()+close()`. Cached session entries (bound to closed loops) accumulated in `_AIOHTTP_SESSIONS` forever, leaking dead sockets / connectors.
**After**: each call sweeps `_AIOHTTP_SESSIONS` for entries whose `session._loop.is_closed()` and pops them. Dead sessions get GC'd by Python.

## Test results
`pytest tests/` → **106 passed** (was 103, +3 new)

## Codex final-review verdict
This commit closes all 3 substantive findings from
`codex review --base origin/main` on prior HEAD `0e8d902`.
