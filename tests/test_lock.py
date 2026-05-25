import importlib
import os

import pytest


@pytest.fixture
def lock_module(monkeypatch, tmp_path):
    """Reload poly_mm_pro_max with LOCK_FILE pointed at tmp_path."""
    import poly_mm_pro_max as mod
    lock_path = str(tmp_path / "test.lock")
    monkeypatch.setattr(mod, "LOCK_FILE", lock_path)
    return mod, lock_path


def test_acquire_lock_writes_pid_on_success(lock_module):
    mod, lock_path = lock_module
    lock = mod.acquire_single_instance_lock()
    try:
        assert lock is not None
        assert lock.closed is False
        with open(lock_path, "r") as f:
            content = f.read().strip()
        assert content == str(os.getpid())
    finally:
        if lock is not None:
            lock.close()


def test_acquire_lock_returns_none_when_already_held(lock_module, monkeypatch):
    mod, lock_path = lock_module

    def raise_blocking(*args, **kwargs):
        raise BlockingIOError(11, "Resource temporarily unavailable")

    monkeypatch.setattr(mod.fcntl, "flock", raise_blocking)
    result = mod.acquire_single_instance_lock()
    assert result is None


def test_acquire_lock_does_not_truncate_existing_pid_on_failure(lock_module, monkeypatch):
    mod, lock_path = lock_module
    # Simulate prior instance: lock file already has PID 12345
    with open(lock_path, "w") as f:
        f.write("12345")

    def raise_blocking(*args, **kwargs):
        raise BlockingIOError(11, "Resource temporarily unavailable")

    monkeypatch.setattr(mod.fcntl, "flock", raise_blocking)
    result = mod.acquire_single_instance_lock()
    assert result is None
    # CRITICAL: previous PID must still be readable
    with open(lock_path, "r") as f:
        content = f.read().strip()
    assert content == "12345"


def test_acquire_lock_closes_fd_on_failure(lock_module, monkeypatch):
    mod, lock_path = lock_module
    captured = {}

    real_open = mod.__builtins__["open"] if isinstance(mod.__builtins__, dict) else __builtins__.open

    def tracking_open(path, *args, **kwargs):
        f = real_open(path, *args, **kwargs)
        captured["file"] = f
        return f

    def raise_blocking(*args, **kwargs):
        raise BlockingIOError(11, "Resource temporarily unavailable")

    monkeypatch.setattr(mod, "open", tracking_open, raising=False)
    monkeypatch.setattr(mod.fcntl, "flock", raise_blocking)
    mod.acquire_single_instance_lock()
    assert captured["file"].closed is True


def test_acquire_lock_truncates_stale_pid_on_success(lock_module):
    mod, lock_path = lock_module
    # Pre-existing stale content longer than new PID's digits
    with open(lock_path, "w") as f:
        f.write("99999999999")
    lock = mod.acquire_single_instance_lock()
    try:
        assert lock is not None
        with open(lock_path, "r") as f:
            content = f.read().strip()
        # Must be exactly the new PID, no leftover digits from stale 99999...
        assert content == str(os.getpid())
        assert "9" not in content or content == str(os.getpid())
    finally:
        if lock is not None:
            lock.close()


def test_acquire_lock_handles_non_blocking_oserror(lock_module, monkeypatch):
    # Per Codex Phase 3 review warn: flock can raise OSError variants other
    # than BlockingIOError (e.g., PermissionError, ENOLCK on exotic FS).
    # Failure path must still close fd and return None, not propagate.
    mod, lock_path = lock_module

    def raise_permission_error(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(mod.fcntl, "flock", raise_permission_error)
    result = mod.acquire_single_instance_lock()
    assert result is None
