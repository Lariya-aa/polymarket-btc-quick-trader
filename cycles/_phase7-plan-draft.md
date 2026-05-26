# Plan: Phase 7 — Shared aiohttp ClientSession (V7 Resource Stewardship)

## Why

VIRTUES-PLAN.md Phase 7 / V7: 11 separate `async with aiohttp.ClientSession(...)` blocks each create + tear down a connection pool per call. Long-running reversal_live (30 min + 24 h) does thousands of polls (`_settle_from_positions` backoff, `fetch_btc_15m_klines`, `fetch_positions`, `push_to_server_chan`). Each session creates a TCP/TLS handshake and leaves connectors to GC. Phase 10 (60 s account refresh) and Phase 11 (daily report push) will make this worse — 1440+ sessions/day. Goal: one shared `aiohttp.ClientSession` per running loop, with per-call timeout preserved.

**Key constraint:** the GUI creates a fresh `loop = asyncio.new_event_loop()` per button click (L639, 802, 1199, 1235, 1544, 1888, 2207, 2280). A naive module-level `ClientSession` would bind to whichever loop happened to create it and fail on the next click ("Event loop is closed"). Mitigation: key the cache by `id(asyncio.get_running_loop())`.

## Files in scope (whitelist)

- `poly_mm_pro_max.py` — add `_get_aiohttp_session(loop, timeout)` helper, replace 11 call sites, add Tk `WM_DELETE_WINDOW` handler in `__main__`.
- `tests/test_session.py` — **NEW**. Pure-loop tests that two calls in the same loop reuse the session, that different loops get different sessions, that explicit timeout per call works.

## Out of scope (explicit DON'T)

- DO NOT touch any CLOB client (`ClobClient`) usage — it's a separate sync SDK with its own httpx pool; not aiohttp.
- DO NOT touch `_fetch_positions_raw` body's RuntimeError-on-non-200 contract (Phase 4 territory) — only swap the `ClientSession` construction.
- DO NOT change per-call timeout values (12 / 10 / 15 / 35 s) — see table below; downstream code depends on these.
- DO NOT introduce a `__del__` / atexit hook on the session — close path is via Tk WM_DELETE_WINDOW only.
- DO NOT add `connector=TCPConnector(limit=…)` tuning; default connector is fine for this workload.
- DO NOT modify `push_to_server_chan` retry semantics; only swap session construction.
- DO NOT modify `LaunchAgent` / `watchdog` shell scripts — `install_launch_agent.sh`, `run_poly_mm.sh`, `watch_poly_mm.sh` stay untouched.
- DO NOT change `fetch_quick_btc_markets` HTML-parsing logic — only swap session.
- DO NOT introduce a third-party HTTP lib (requests, httpx) or async caching dep.
- DO NOT rename `aiohttp` imports or move them.
- DO NOT change `fetch_json` return type (`None` on failure).

## Call-site inventory (anchor strings + observed timeouts)

| # | Line ~ | Host / purpose | Timeout |
|---|---|---|---|
| 1 | 624 | `fetch_json` (gamma + generic) | 12 |
| 2 | 657 | `fetch_quick_btc_markets` — polymarket.com/crypto/bitcoin HTML scrape | 12 |
| 3 | 823 | `fetch_btc_signal` — Binance klines primary | 10 |
| 4 | 830 | `fetch_btc_signal` — Binance vision fallback | 10 |
| 5 | 971 | `post_minimax_with_retry` — minimaxi.com LLM | 35 (connect=10, sock_read=30) |
| 6 | 1302 | `fetch_btc_15m_klines` — Binance multi-url loop | 15 |
| 7 | 1846 | `fetch_latest_btc_price` — Binance ticker primary | 10 |
| 8 | 1854 | `fetch_latest_btc_price` — Binance ticker fallback | 10 |
| 9 | 1995 | `fetch_positions` — data-api positions (silent) | 12 |
| 10 | 2022 | `_fetch_positions_raw` — data-api positions (raises) | 12 |
| 11 | 2397 | `push_to_server_chan` — sctapi.ftqq.com | 10 |

Line numbers will shift after Phase 5/6 — Kimi **MUST anchor on the unique string** `aiohttp.ClientSession(timeout=` and the surrounding function name, not the line number.

## The change (literal spec)

### Step 1 — Add module-level session cache

Insert immediately after the existing `import` block (i.e., after L19 `from py_clob_client_v2.clob_types import ApiCreds`, before any `dataclass`/class definitions). Anchor: insert after the line `from py_clob_client_v2.clob_types import ApiCreds`.

```python
# --- Phase 7: shared aiohttp session per asyncio loop ----------------------
_AIOHTTP_SESSIONS: "dict[int, aiohttp.ClientSession]" = {}
_AIOHTTP_SESSIONS_LOCK = threading.Lock()


def _get_aiohttp_session(default_timeout_total: float = 12.0,
                        headers: dict | None = None) -> aiohttp.ClientSession:
    """
    Return a shared aiohttp.ClientSession bound to the current running loop.

    Keyed by id(loop) so each GUI-click loop gets its own session and closure
    of a previous loop doesn't poison subsequent clicks. Callers SHOULD pass
    a per-request `timeout=` to `session.get/post/...` when they need a
    timeout different from `default_timeout_total` — the session's default
    is only used when the caller omits `timeout=`.
    """
    loop = asyncio.get_running_loop()
    key = id(loop)
    with _AIOHTTP_SESSIONS_LOCK:
        existing = _AIOHTTP_SESSIONS.get(key)
        if existing is not None and not existing.closed:
            return existing
        timeout = aiohttp.ClientTimeout(total=default_timeout_total)
        session = aiohttp.ClientSession(timeout=timeout, headers=headers or {})
        _AIOHTTP_SESSIONS[key] = session
        return session


async def _close_aiohttp_sessions():
    """Close all cached sessions. Called from Tk WM_DELETE_WINDOW handler."""
    with _AIOHTTP_SESSIONS_LOCK:
        sessions = list(_AIOHTTP_SESSIONS.values())
        _AIOHTTP_SESSIONS.clear()
    for s in sessions:
        if not s.closed:
            try:
                await s.close()
            except Exception:
                pass
```

### Step 2 — Replace each of the 11 call sites

Pattern A (no custom headers, simple total timeout):

**Before** (anchor: any line matching `async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:`)
```python
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.get(url, params=params) as response:
                    ...
```

**After**
```python
            session = _get_aiohttp_session()
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as response:
                ...
```

Pattern B (with `User-Agent` header — sites 1, 2, 9, 10):

**Before**
```python
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12), headers={"User-Agent": "Mozilla/5.0"}) as session:
                async with session.get(url) as response:
                    ...
```

**After**
```python
            session = _get_aiohttp_session()
            async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=aiohttp.ClientTimeout(total=12)) as response:
                ...
```

(Pass User-Agent at the request level, not the session level, because the shared session is also used by Binance / Server酱 and they don't want the Mozilla UA.)

Pattern C (MiniMax — keep custom `connect`/`sock_read`):

**Before** (L968-971 region)
```python
        timeout = aiohttp.ClientTimeout(total=35, connect=10, sock_read=30)
        for attempt in range(1, 3):
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(
                        MINIMAX_CHAT_URL,
                        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                        json=payload,
                    ) as response:
```

**After**
```python
        timeout = aiohttp.ClientTimeout(total=35, connect=10, sock_read=30)
        for attempt in range(1, 3):
            try:
                session = _get_aiohttp_session()
                async with session.post(
                    MINIMAX_CHAT_URL,
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json=payload,
                    timeout=timeout,
                ) as response:
```

Apply these patterns to all 11 sites listed in the inventory. Each `async with aiohttp.ClientSession(...) as session:` collapses by exactly **one indent level** — the outer `async with` is deleted, the inner `async with session.X(...)` shifts left by 4 spaces.

### Step 3 — Tk shutdown hook in `__main__`

**Before** (L2509-2517):
```python
if __name__ == "__main__":
    instance_lock = acquire_single_instance_lock()
    if instance_lock is None:
        sys.exit(0)
    root = tk.Tk()
    style = ttk.Style(root)
    style.theme_use("clam")
    app = PolyQuickTrader(root)
    root.mainloop()
```

**After**:
```python
if __name__ == "__main__":
    instance_lock = acquire_single_instance_lock()
    if instance_lock is None:
        sys.exit(0)
    root = tk.Tk()
    style = ttk.Style(root)
    style.theme_use("clam")
    app = PolyQuickTrader(root)

    def _on_close():
        try:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(_close_aiohttp_sessions())
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()
        except Exception:
            pass
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", _on_close)
    root.mainloop()
```

Note: `_close_aiohttp_sessions` walks the dict on a *new* loop, but sessions were created on now-defunct per-click loops. `session.close()` calls `connector.close()` which doesn't require the original loop — it just releases sockets. If aiohttp emits a "Unclosed connector" warning on shutdown, that's acceptable (LaunchAgent restart still works).

### Step 4 — `tests/test_session.py` (NEW)

```python
import asyncio
import pytest
import poly_mm_pro_max as mod


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.run_until_complete(mod._close_aiohttp_sessions())
        loop.close()


def test_same_loop_returns_same_session():
    async def go():
        s1 = mod._get_aiohttp_session()
        s2 = mod._get_aiohttp_session()
        assert s1 is s2
        assert not s1.closed
    _run(go())


def test_different_loops_get_different_sessions():
    seen = []
    async def go():
        seen.append(id(mod._get_aiohttp_session()))
    _run(go())
    # second _run uses a new loop, gets a new session
    _run(go())
    assert len(set(seen)) == 2


def test_close_all_clears_cache():
    async def make():
        mod._get_aiohttp_session()
    _run(make())
    # _run itself calls _close_aiohttp_sessions in its finally
    assert all(s.closed for s in mod._AIOHTTP_SESSIONS.values()) or not mod._AIOHTTP_SESSIONS
```

## Verification commands

1. **No bare `aiohttp.ClientSession(` call sites remain** (other than the one inside `_get_aiohttp_session`):
   ```bash
   grep -cE "aiohttp\.ClientSession\(" poly_mm_pro_max.py
   ```
   Expected: **1** (the constructor inside `_get_aiohttp_session`).

2. **Cache helper present**:
   ```bash
   grep -n "_get_aiohttp_session\|_close_aiohttp_sessions" poly_mm_pro_max.py
   ```
   Expected: ≥ 13 lines (2 def + 11 call sites + close-on-exit).

3. **Tk WM_DELETE_WINDOW wired**:
   ```bash
   grep -n "WM_DELETE_WINDOW" poly_mm_pro_max.py
   ```
   Expected: 1 hit.

4. **Run tests**:
   ```bash
   .venv/bin/python -m pytest tests/test_session.py -v
   ```
   Expected: 3 passed. Plus full suite still green:
   ```bash
   .venv/bin/python -m pytest tests/ -v
   ```
   Expected: 42 + 3 = 45 passed.

5. **Import sanity**:
   ```bash
   .venv/bin/python -c "import poly_mm_pro_max; print('import ok')"
   ```

## Live verification owed (user)

Run reversal_live (or paper-sim, which exercises same fetch paths) for 30 minutes, then:
```bash
lsof -p $(pgrep -f poly_mm_pro_max) | grep TCP | wc -l
```
Expected: stable ≤ ~10 sockets over time. Pre-Phase-7 baseline: linearly growing. Also close the GUI window and confirm no `Unclosed client session` traceback in `poly_mm_pro_max.log` (Unclosed-connector warnings are tolerated).

## Schema source

None — pure refactor of internal HTTP-pool usage. No external API contract is touched.
