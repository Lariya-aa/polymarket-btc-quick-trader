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
    sessions = []
    async def go():
        sessions.append(mod._get_aiohttp_session())
    _run(go())
    # second _run uses a new loop, gets a new session
    _run(go())
    assert sessions[0] is not sessions[1]


def test_close_all_clears_cache():
    async def make():
        mod._get_aiohttp_session()
    _run(make())
    # _run itself calls _close_aiohttp_sessions in its finally
    assert all(s.closed for s in mod._AIOHTTP_SESSIONS.values()) or not mod._AIOHTTP_SESSIONS


def test_call_sites_pass_explicit_timeouts():
    """Source-level audit per Codex Phase 7 warn: every session.get/post
    call site MUST pass a `timeout=` kwarg. The shared session should NOT
    silently inherit a default that masks per-site needs (e.g., MiniMax
    needs 35s while Binance needs 10s)."""
    import re
    import pathlib
    src = pathlib.Path(mod.__file__).read_text()
    # Match `session.get(...)` or `session.post(...)` up to matching paren
    # (non-greedy across newlines so multi-line calls are captured too).
    calls = re.findall(r"session\.(?:get|post)\([^)]*?\)", src, flags=re.S)
    missing = [c[:120] for c in calls if "timeout=" not in c]
    assert not missing, f"{len(missing)} call site(s) without explicit timeout=: {missing}"


def test_per_request_timeout_can_override_session_default():
    """Verify aiohttp accepts a per-request `timeout=` kwarg even when the
    session has a default. This is the plan's V7 contract."""
    import aiohttp

    async def go():
        session = mod._get_aiohttp_session(default_timeout_total=99)
        req_timeout = aiohttp.ClientTimeout(total=5)
        try:
            async with session.request(
                "GET", "http://127.0.0.1:1/probe",
                timeout=req_timeout,
                allow_redirects=False,
            ) as _:
                pass
        except (aiohttp.ClientConnectorError, aiohttp.ClientError, OSError, asyncio.TimeoutError):
            pass  # connect failure is fine; we're testing kwarg acceptance

    _run(go())
