"""Shared test setup.

The application is a single-file Tkinter GUI. Most pure helpers we want to
test live on `PolyQuickTrader` as methods that never touch `self`. To call
them in a CI / headless environment we have to import the module, which
unconditionally `import tkinter`s at top level. Some CPython builds (e.g.
Homebrew python@3.14 without python-tk) don't ship _tkinter, so we stub
out the tkinter surface area to a minimal shim before the module is
loaded.

This conftest runs once per pytest session.
"""
import sys
import types
from unittest.mock import MagicMock

import pytest


def _install_tkinter_stub() -> None:
    if "tkinter" in sys.modules:
        return
    tk_mod = types.ModuleType("tkinter")
    tk_mod.Tk = MagicMock()
    tk_mod.END = "end"
    tk_mod.StringVar = MagicMock()

    # Submodules referenced in `from tkinter import messagebox, scrolledtext, ttk`
    for sub in ("messagebox", "scrolledtext", "ttk"):
        sub_mod = types.ModuleType(f"tkinter.{sub}")
        # Generic MagicMock-style attribute lookups satisfy any name access.
        sub_mod.__getattr__ = lambda name: MagicMock()  # type: ignore[attr-defined]
        sys.modules[f"tkinter.{sub}"] = sub_mod
        setattr(tk_mod, sub, sub_mod)

    sys.modules["tkinter"] = tk_mod


_install_tkinter_stub()


def _ensure_repo_on_path() -> None:
    import pathlib
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))


_ensure_repo_on_path()


import poly_mm_pro_max as M  # noqa: E402  (must come after stub + path)


class _PureBag:
    """Stand-in `self` so pure methods on PolyQuickTrader can be called
    without running its Tkinter-heavy __init__. Any attribute access is
    redirected to the real class; callable attributes get bound to this
    bag. Lets tests write `bag.clamp_price(0.5, '0.01')` even though
    clamp_price internally calls `self.price_decimals(...)`.

    Some methods log via `self.logger.warning/info/error`. We attach a
    real logger so those calls don't blow up under test. The handlers
    drop everything; tests don't assert on log lines.
    """

    def __init__(self):
        import logging
        self.logger = logging.getLogger("test_bag")
        # Silence test-run output. Tests don't assert on log content;
        # for those that need to (e.g. via caplog) pytest's caplog
        # fixture still works because we use the standard logging stack.
        self.logger.addHandler(logging.NullHandler())

    def __getattr__(self, name):
        attr = getattr(M.PolyQuickTrader, name, None)
        if attr is None:
            raise AttributeError(name)
        if callable(attr):
            return attr.__get__(self, _PureBag)
        return attr


@pytest.fixture
def bag():
    return _PureBag()
