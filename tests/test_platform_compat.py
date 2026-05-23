"""Tests for the cross-platform path helpers and single-instance lock.

These live module-level (not on PolyQuickTrader), so we import them
directly rather than via the `bag` fixture.

The lock test runs on whatever platform the test host happens to be —
both branches of acquire_single_instance_lock can't be exercised
simultaneously, but the current-platform branch is what would crash a
packaged build if broken, so this is the high-value test to keep green.
"""

import os
import sys

import pytest

import poly_mm_pro_max as M


# ── path helpers ──────────────────────────────────────────────────────────


def test_user_data_dir_exists_after_call(tmp_path, monkeypatch):
    # Redirect each platform's base dir into tmp_path so the test doesn't
    # touch the real user profile.
    if sys.platform == "darwin":
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(os.path, "expanduser", lambda p: str(fake_home) if p == "~/Library/Application Support" else p)
    elif sys.platform == "win32":
        monkeypatch.setenv("APPDATA", str(tmp_path))
    else:
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    path = M.user_data_dir()
    assert os.path.isdir(path), f"user_data_dir() should mkdir: {path}"
    assert path.endswith(M.APP_DIR_NAME)


def test_config_path_lives_under_user_data_dir():
    assert os.path.dirname(M.config_path()) == M.user_data_dir()
    assert os.path.basename(M.config_path()) == M.CONFIG_FILE


def test_log_path_lives_under_user_data_dir():
    assert os.path.dirname(M.log_path()) == M.user_data_dir()
    assert os.path.basename(M.log_path()) == M.LOG_FILE


def test_lock_path_lives_under_tempdir():
    import tempfile
    assert os.path.dirname(M.lock_path()) == tempfile.gettempdir()
    assert os.path.basename(M.lock_path()) == M.LOCK_FILE


# ── single-instance lock ──────────────────────────────────────────────────


@pytest.fixture
def isolated_lock_path(tmp_path, monkeypatch):
    """Redirect lock_path() to a per-test file so the test doesn't fight
    a developer who happens to have the real GUI running."""
    fake = str(tmp_path / "test.lock")
    monkeypatch.setattr(M, "lock_path", lambda: fake)
    yield fake


def test_first_acquire_succeeds(isolated_lock_path):
    lock = M.acquire_single_instance_lock()
    assert lock is not None
    assert os.path.exists(isolated_lock_path)
    # PID written for diagnostics.
    with open(isolated_lock_path, "r") as f:
        content = f.read().strip()
    assert content == str(os.getpid()), f"expected PID in lock file, got {content!r}"
    lock.close()


def test_second_acquire_returns_none(isolated_lock_path, capsys):
    first = M.acquire_single_instance_lock()
    assert first is not None
    try:
        second = M.acquire_single_instance_lock()
        assert second is None, "second acquire should fail while first is held"
        captured = capsys.readouterr()
        assert "already running" in captured.err
    finally:
        first.close()


def test_lock_released_after_close(isolated_lock_path):
    # After the holder closes its handle, the next acquire should succeed.
    first = M.acquire_single_instance_lock()
    assert first is not None
    first.close()
    second = M.acquire_single_instance_lock()
    assert second is not None
    second.close()


def test_stale_lock_file_can_be_acquired(isolated_lock_path):
    # Simulate a prior crashed process: lock file exists on disk but
    # nobody holds the lock. The next acquire should succeed because the
    # OS frees the lock when the prior process exits.
    with open(isolated_lock_path, "w") as f:
        f.write("99999")  # fake PID
    lock = M.acquire_single_instance_lock()
    assert lock is not None
    lock.close()
