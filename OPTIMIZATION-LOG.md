# OPTIMIZATION-LOG.md

> Branch: `optimization/virtues-pass` (off `main`).
> This file is **a high-level index, not a per-commit changelog**.
> For per-commit history use `git log --oneline main..HEAD` directly.
>
> Why: earlier versions of this doc hardcoded commit counts and test
> counts that went stale within hours. Reviewers (Codex pass-2 + pass-3)
> flagged the drift twice. Now we point readers at commands instead.

---

## Quick verification

```bash
git checkout optimization/virtues-pass

git log --oneline main..HEAD            # current commit list
git log --oneline main..HEAD | wc -l    # commit count

python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest tests/ -q              # current test count

./PolyMarketMaker.command               # manual GUI smoke
```

---

## What this branch contains, by theme

The branch is a multi-cycle effort: **virtues review → fix → re-review**.
Below is the thematic grouping of commits. Use `git log --grep=<pattern>`
to pull each subset.

### 1. Virtues pass-1 fixes (from the initial static review)

Themes — full list via `git log --grep="virtues" --grep="logger"
--grep="signal" --grep="MiniMax" --grep="input" --grep="order"
--grep="positions" --grep="scan" --grep="shell" --grep="deps"
main..HEAD`:

- Root logger leak in `load_config_from_local` (UI panel never saw config errors)
- Probability-model magic constants named + `math.exp` replacement
- "MiniMax 失败 已用本地信号" misleading UI text
- Reject `inf`/`nan` from quick-buy form input
- `client_order_id` (later renamed) + `TimeoutError` no-retry handling
- `fetch_positions` distinguishes API failure from empty
- HTML-scrape fallback extended to 1h and 1d
- Shell strict mode + `requirements*.txt` + initial pytest baseline

### 2. Cross-platform + packaging

- `fcntl` import made lazy on Unix; `msvcrt` on Windows
- `user_data_dir()` for cross-OS config/log location
- PyInstaller spec, macOS `.dmg` script, Windows `.ps1` script
- GitHub Actions workflow (initially), `PACKAGING.md`

### 3. Multi-category market support (NBA / NFL / 世界杯 / 新上线)

- `QuickMarket` → `PolyMarket`; `up_*/down_*` → `yes_*/no_*`
- Generic `fetch_tag_markets()` + `fetch_newly_listed_markets()` + `CATEGORIES` registry
- Category Combobox in UI
- Extensible AI signal dispatch (`_local_signal_for`); BTC keeps the Binance heuristic, others go MiniMax-only
- `MARKETS.md` explaining the extension point

### 4. Virtues pass-2 fixes (Codex caught 2 new blockers)

- NaN/inf hardened across the order pipeline (`best_ask_for_token`, `clamp_price`, buy/sell input guards)
- `outcomes=["Yes","No"]` validation gate in `_build_market` (rejects Knicks-vs-Cavaliers-shaped markets so 买 Yes routes correctly)
- Doc thread-leak on order timeout (inline NOTE)
- Symmetric tests: sell-reject, `prob_down` NaN path
- Cleanup: dead `category_code`, duplicate `period`, `dependabot.yml`, docs say "see pytest output" instead of literal counts

### 5. Virtues pass-3 fixes (Codex caught 3 more blockers, dug into integration)

- **Block retry while order in flight**: TimeoutError no longer auto-re-enables
  buy/sell. A reconcile modal forces user to confirm portfolio.com check
  before retry. (`_prompt_reconcile_after_timeout`)
- **`push_trade_result` surfaces positions API failure** in the ServerChan
  notification instead of saying "当前没有可见持仓" when the positions
  API just failed
- **CI: SHA-pin every action** + scope `contents: write` to release-upload
  steps only. Default workflow perms are `contents: read`.
- `_float_or_zero` rejects NaN/inf (chokepoint between API → display/PnL)

---

## Risk inventory (what may still bite)

| # | Risk | Mitigation in place | Outstanding |
|---|---|---|---|
| 1 | Order timeout → exchange may still execute | `_prompt_reconcile_after_timeout` modal forces user reconciliation | `py_clob_client_v2` has no per-request transport timeout we can wire in; switching client lib would be a separate effort |
| 2 | Position API hiccup hides successful trade | `push_trade_result` surfaces `last_positions_fetch_error` inline | — |
| 3 | NBA/NFL/WC team-vs-team markets misrouted via Yes/No buttons | `_is_yes_no_market` filter rejects non-binary outcomes | NBA/NFL etc. now show fewer markets — only "is X the champion?" Yes/No, not "Knicks vs Cavs" |
| 4 | NaN/inf from upstream API → display or order math | Tightened `_float_or_zero` + per-call `math.isfinite` checks; `parse_minimax_json` clamps | — |
| 5 | Supply chain via mutable actions | SHA-pinned all 4 actions; scoped `contents: write` | Dependabot will raise PRs on bumps; user must review the bump diffs |
| 6 | 1700+ line single file | Split-monofile work intentionally deferred (would benefit from interactive GUI testing) | Open as future task |

---

## What was deliberately NOT done

| Item | Reason |
|---|---|
| Split `poly_mm_pro_max.py` into modules | High-risk refactor; needs GUI smoke testing per change |
| `aiohttp.ClientSession` global reuse | Cross-cutting; defer to next branch |
| Worker-pattern dedup | Cosmetic, low payoff, risks threading |
| `OrderArgs(client_order_id=...)` exchange-side idempotency | `py_clob_client_v2.OrderArgs` doesn't accept the field; would need a different client library |
| Replace `asyncio.to_thread` with cancellable transport | Same — needs library swap |
| Code-sign + notarize `.dmg` | $99/yr Apple Dev account + $300+/yr Windows cert; user decision |

---

## How to continue

Each future review cycle:

```bash
# Run both reviewers in parallel
kimi --print --final-message-only -p "$(cat .ai-cycle/virtues-review/PROMPT.md)" \
  --work-dir . > .ai-cycle/virtues-review/kimi-review-pass<N>.md &
codex exec --cd . -s read-only --ignore-user-config \
  --output-last-message .ai-cycle/virtues-review/codex-review-pass<N>.md \
  "$(cat .ai-cycle/virtues-review/PROMPT.md)" \
  > .ai-cycle/virtues-review/codex-events-pass<N>.log 2>&1 &
wait

# Then write COMPARISON-PASS<N>.md and fix findings.
```

The pattern across passes 1→2→3:
- Kimi catches **drift + symmetry + dead code** — gets less novel each cycle.
- Codex catches **deeper integration bugs** — keeps finding new ones a layer down.
- Together they're complementary; neither alone is sufficient.

---

*Last regenerated: see `git log -1 OPTIMIZATION-LOG.md`*
