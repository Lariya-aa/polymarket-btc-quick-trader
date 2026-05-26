import asyncio
import time

import pytest

import poly_mm_pro_max as mod


class _FakeStop:
    def __init__(self):
        self._set = False

    def is_set(self):
        return self._set

    def set(self):
        self._set = True


def _make_trader(monkeypatch, fake_positions_sequence):
    """Build a PolyQuickTrader-like object with mocked _fetch_positions_raw.

    Each element of fake_positions_sequence is either:
      - a list of position dicts (success)
      - an Exception instance (transport failure: raised by raw fetcher)
    """
    inst = mod.PolyQuickTrader.__new__(mod.PolyQuickTrader)
    inst._float_or_zero = mod.PolyQuickTrader._float_or_zero.__get__(inst)
    inst.logger = mod.logging.getLogger("test_settle")
    calls = {"count": 0}

    async def fake_fetch_positions_raw():
        idx = min(calls["count"], len(fake_positions_sequence) - 1)
        calls["count"] += 1
        item = fake_positions_sequence[idx]
        if isinstance(item, Exception):
            raise item
        return item

    inst._fetch_positions_raw = fake_fetch_positions_raw
    return inst, calls


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_settle_win_when_redeemable_true(monkeypatch):
    _orig_sleep = mod.asyncio.sleep
    monkeypatch.setattr(mod.asyncio, "sleep", lambda *_a, **_kw: _orig_sleep(0))
    inst, _ = _make_trader(monkeypatch, [
        [{"asset": "tok-A", "size": 10.0, "redeemable": True}],
    ])
    result = _run(inst._settle_from_positions("tok-A", time.time() + 60))
    assert result == "win"


def test_settle_loss_when_asset_absent_for_two_cycles(monkeypatch):
    _orig_sleep = mod.asyncio.sleep
    monkeypatch.setattr(mod.asyncio, "sleep", lambda *_a, **_kw: _orig_sleep(0))
    inst, _ = _make_trader(monkeypatch, [
        [{"asset": "other-tok", "size": 5.0, "redeemable": False}],
        [{"asset": "other-tok", "size": 5.0, "redeemable": False}],
    ])
    result = _run(inst._settle_from_positions("tok-A", time.time() + 60))
    assert result == "loss"


def test_settle_pending_then_win(monkeypatch):
    # First poll: position still pending. Second poll: redeemable=True.
    _orig_sleep = mod.asyncio.sleep
    monkeypatch.setattr(mod.asyncio, "sleep", lambda *_a, **_kw: _orig_sleep(0))
    inst, calls = _make_trader(monkeypatch, [
        [{"asset": "tok-A", "size": 10.0, "redeemable": False}],
        [{"asset": "tok-A", "size": 10.0, "redeemable": True}],
    ])
    result = _run(inst._settle_from_positions("tok-A", time.time() + 60))
    assert result == "win"
    assert calls["count"] == 2


def test_settle_timeout(monkeypatch):
    _orig_sleep = mod.asyncio.sleep
    monkeypatch.setattr(mod.asyncio, "sleep", lambda *_a, **_kw: _orig_sleep(0))
    # Always pending, never resolves
    inst, _ = _make_trader(monkeypatch, [
        [{"asset": "tok-A", "size": 10.0, "redeemable": False}],
    ])
    # deadline already in the past → first iteration exits
    result = _run(inst._settle_from_positions("tok-A", time.time() - 1))
    assert result == "pending_timeout"


def test_settle_stop_event_aborts(monkeypatch):
    _orig_sleep = mod.asyncio.sleep
    monkeypatch.setattr(mod.asyncio, "sleep", lambda *_a, **_kw: _orig_sleep(0))
    stop = _FakeStop()
    stop.set()
    inst, _ = _make_trader(monkeypatch, [[]])
    result = _run(inst._settle_from_positions("tok-A", time.time() + 60, stop))
    assert result == "pending_timeout"


def test_settle_transient_empty_then_win(monkeypatch):
    # fetch_positions transient empty → must NOT be classified as loss after one
    # cycle (requires two consecutive absent states). Recovers to win.
    _orig_sleep = mod.asyncio.sleep
    monkeypatch.setattr(mod.asyncio, "sleep", lambda *_a, **_kw: _orig_sleep(0))
    inst, calls = _make_trader(monkeypatch, [
        [],  # transient empty
        [{"asset": "tok-A", "size": 10.0, "redeemable": True}],
    ])
    result = _run(inst._settle_from_positions("tok-A", time.time() + 60))
    assert result == "win"
    assert calls["count"] == 2


def test_settle_size_below_threshold_treated_as_absent(monkeypatch):
    _orig_sleep = mod.asyncio.sleep
    monkeypatch.setattr(mod.asyncio, "sleep", lambda *_a, **_kw: _orig_sleep(0))
    inst, _ = _make_trader(monkeypatch, [
        [{"asset": "tok-A", "size": 0.0000001, "redeemable": False}],
        [{"asset": "tok-A", "size": 0.0000001, "redeemable": False}],
    ])
    result = _run(inst._settle_from_positions("tok-A", time.time() + 60))
    assert result == "loss"


def test_settle_http_failure_does_not_count_as_loss(monkeypatch):
    # CRITICAL (per V8 Codex warn): two consecutive _fetch_positions_raw
    # failures must NOT be classified as LOSS even though they have no
    # successful "absent" state. Recovers to WIN once fetch succeeds.
    _orig_sleep = mod.asyncio.sleep
    monkeypatch.setattr(mod.asyncio, "sleep", lambda *_a, **_kw: _orig_sleep(0))
    inst, calls = _make_trader(monkeypatch, [
        RuntimeError("positions HTTP 503"),
        RuntimeError("positions HTTP 503"),
        [{"asset": "tok-A", "size": 10.0, "redeemable": True}],
    ])
    result = _run(inst._settle_from_positions("tok-A", time.time() + 60))
    assert result == "win"
    assert calls["count"] == 3


def test_settle_http_failure_then_real_absent_two_cycles(monkeypatch):
    # Two HTTP failures + two real-absent successful fetches → LOSS
    # (the two absent cycles must come from successful fetches, not
    # mixed with HTTP errors).
    _orig_sleep = mod.asyncio.sleep
    monkeypatch.setattr(mod.asyncio, "sleep", lambda *_a, **_kw: _orig_sleep(0))
    inst, calls = _make_trader(monkeypatch, [
        RuntimeError("positions HTTP 503"),
        [{"asset": "other-tok", "size": 5.0}],
        [{"asset": "other-tok", "size": 5.0}],
    ])
    result = _run(inst._settle_from_positions("tok-A", time.time() + 60))
    assert result == "loss"
    assert calls["count"] == 3


def test_settle_absent_then_http_failure_then_absent_resets_streak(monkeypatch):
    """Final Codex P1 BLOCKER: absent → HTTP fail → absent must NOT classify
    as loss. The two absent polls have to be consecutive successful fetches,
    not just two-total. Otherwise a single transient 503 in the middle of a
    still-settling market can flip the cycle to a false LOSS and trigger
    martingale doubling at the next layer.
    """
    _orig_sleep = mod.asyncio.sleep
    monkeypatch.setattr(mod.asyncio, "sleep", lambda *_a, **_kw: _orig_sleep(0))
    inst, calls = _make_trader(monkeypatch, [
        [{"asset": "other-tok", "size": 5.0}],          # absent #1 (success)
        RuntimeError("positions HTTP 503"),             # transient error
        [{"asset": "other-tok", "size": 5.0}],          # absent again (single)
        [{"asset": "tok-A", "size": 10.0, "redeemable": True}],  # recovers to WIN
    ])
    result = _run(inst._settle_from_positions("tok-A", time.time() + 60))
    assert result == "win"
    assert calls["count"] == 4
