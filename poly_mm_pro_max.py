import asyncio
import fcntl
import json
import logging
import os
import re
import sys
import threading
import time
import tkinter as tk
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from tkinter import messagebox, scrolledtext, ttk

import aiohttp
from py_clob_client_v2 import OrderArgs, OrderType, PartialCreateOrderOptions, Side
from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import ApiCreds

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
        # GC stale cached sessions whose loops have been closed by their
        # short-lived GUI-click workers (Final Codex P2: each click runs
        # asyncio.new_event_loop()+close(); without this, every click left
        # a dead-loop session in the cache forever).
        stale_keys = []
        for k, s in _AIOHTTP_SESSIONS.items():
            try:
                if getattr(s, "_loop", None) is not None and s._loop.is_closed():
                    stale_keys.append(k)
            except Exception:
                pass
        for k in stale_keys:
            _AIOHTTP_SESSIONS.pop(k, None)
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


CONFIG_FILE = "poly_config_pro.json"
LOG_FILE = "poly_mm_pro_max.log"
LOCK_FILE = "/tmp/poly_mm_pro_max.lock"
GAMMA_EVENT_SLUG_URL = "https://gamma-api.polymarket.com/events/slug"
POLYMARKET_BASE_URL = "https://polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
MINIMAX_CHAT_URL = "https://api.minimaxi.com/v1/chat/completions"
MINIMAX_MODEL = "MiniMax-M2.7"
CHAIN_ID = 137
REVERSAL_MODE_RED_UP = "三连阴转UP"
REVERSAL_MODE_GREEN_DOWN = "三连阳转DOWN"


@dataclass
class QuickMarket:
    slug: str
    event_slug: str
    question: str
    yes_id: str
    no_id: str
    tick_size: str
    period: str
    end_dt: datetime | None
    ended: bool
    up_bid: float
    up_ask: float
    down_bid: float
    down_ask: float
    spread: float
    volume24h: float


class TkinterLogHandler(logging.Handler):
    def __init__(self, text_widget, category=None):
        super().__init__()
        self.text_widget = text_widget
        self.category = category

    def emit(self, record):
        if self.category and getattr(record, "category", "manual") != self.category:
            return
        msg = self.format(record)

        def append():
            self.text_widget.configure(state="normal")
            self.text_widget.insert(tk.END, msg + "\n")
            self.text_widget.see(tk.END)
            self.text_widget.configure(state="disabled")

        self.text_widget.after(0, append)


class PolyQuickTrader:
    def __init__(self, root):
        self.root = root
        self.root.title("Polymarket BTC 快速交易工具")
        self.root.geometry("1060x860")

        self.latest_quick_markets: list[QuickMarket] = []
        self.latest_positions = []
        self.latest_signal = None
        self.last_credential_error = None
        self.paper_results = []
        self.live_results = []
        # Phase 10 account refresh state
        self.account_last_refresh_ts: float = 0.0
        self.account_last_balance_usdc = None
        self.account_last_positions_value = None
        self.account_last_positions_count: int = 0
        self.account_last_error: str = ""
        self.live_auto_enabled = False
        self.live_auto_running = False
        self.live_auto_stop_requested = threading.Event()
        self.reversal_running = False
        self.reversal_stop_requested = threading.Event()
        self.log_context = threading.local()

        self.logger = logging.getLogger("PolyQuickTrader")
        self.logger.setLevel(logging.INFO)
        self.logger.handlers.clear()
        self.logger.filters.clear()

        self.setup_ui()
        self.logger.addFilter(self.log_category_filter)

        manual_handler = TkinterLogHandler(self.manual_log_text, category="manual")
        manual_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
        self.logger.addHandler(manual_handler)

        auto_handler = TkinterLogHandler(self.auto_log_text, category="auto")
        auto_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
        self.logger.addHandler(auto_handler)

        live_handler = TkinterLogHandler(self.live_log_text, category="live")
        live_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
        self.logger.addHandler(live_handler)

        file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        self.logger.addHandler(file_handler)

        self.load_config_from_local()
        self.load_env_file()
        self.load_credentials_from_env()
        # Phase 11: schedule daily report check (idempotent, fires once per UTC day).
        self.root.after(5_000, self._daily_report_tick)
        self.root.after(3_000, self._periodic_account_refresh_tick)

    def log_category_filter(self, record):
        record.category = getattr(self.log_context, "category", "manual")
        return True

    def set_log_category(self, category):
        self.log_context.category = category

    def log_auto(self, level, message, *args):
        old_category = getattr(self.log_context, "category", "manual")
        self.log_context.category = "auto"
        try:
            self.logger.log(level, message, *args)
        finally:
            self.log_context.category = old_category

    def log_live(self, level, message, *args):
        old_category = getattr(self.log_context, "category", "manual")
        self.log_context.category = "live"
        try:
            self.logger.log(level, message, *args)
        finally:
            self.log_context.category = old_category

    def setup_ui(self):
        api_frame = ttk.LabelFrame(self.root, text=" 1. 凭证配置（私钥和 Key 不会写入本地配置文件） ", padding=10)
        api_frame.pack(fill="x", padx=15, pady=5)

        ttk.Label(api_frame, text="Polygon 钱包私钥:").grid(row=0, column=0, sticky="w", pady=3)
        self.ent_priv_key = ttk.Entry(api_frame, show="*", width=82)
        self.ent_priv_key.grid(row=0, column=1, columnspan=3, sticky="w", pady=3, padx=5)

        ttk.Label(api_frame, text="CLOB API Key:").grid(row=1, column=0, sticky="w", pady=3)
        self.ent_api_key = ttk.Entry(api_frame, width=32)
        self.ent_api_key.grid(row=1, column=1, sticky="w", pady=3, padx=5)

        ttk.Label(api_frame, text="CLOB Secret:").grid(row=1, column=2, sticky="w", pady=3)
        self.ent_secret = ttk.Entry(api_frame, show="*", width=36)
        self.ent_secret.grid(row=1, column=3, sticky="w", pady=3, padx=5)

        ttk.Label(api_frame, text="Passphrase:").grid(row=2, column=0, sticky="w", pady=3)
        self.ent_passphrase = ttk.Entry(api_frame, show="*", width=32)
        self.ent_passphrase.grid(row=2, column=1, sticky="w", pady=3, padx=5)

        ttk.Label(api_frame, text="Server酱 SendKey:").grid(row=2, column=2, sticky="w", pady=3)
        self.ent_sendkey = ttk.Entry(api_frame, show="*", width=36)
        self.ent_sendkey.grid(row=2, column=3, sticky="w", pady=3, padx=5)

        ttk.Label(api_frame, text="Funder 地址:").grid(row=3, column=0, sticky="w", pady=3)
        self.ent_funder = ttk.Entry(api_frame, width=50)
        self.ent_funder.grid(row=3, column=1, columnspan=2, sticky="w", pady=3, padx=5)

        ttk.Label(api_frame, text="签名类型:").grid(row=3, column=3, sticky="w", pady=3)
        self.cbo_signature_type = ttk.Combobox(api_frame, width=8, state="readonly")
        self.cbo_signature_type["values"] = ("0", "1", "2", "3")
        self.cbo_signature_type.set("3")
        self.cbo_signature_type.grid(row=3, column=3, sticky="e", pady=3, padx=5)

        ttk.Label(api_frame, text="MiniMax Token Plan Key:").grid(row=4, column=0, sticky="w", pady=3)
        self.ent_minimax_key = ttk.Entry(api_frame, show="*", width=82)
        self.ent_minimax_key.grid(row=4, column=1, columnspan=3, sticky="w", pady=3, padx=5)

        self.main_notebook = ttk.Notebook(self.root)
        self.main_notebook.pack(fill="both", expand=True, padx=15, pady=5)

        manual_tab = ttk.Frame(self.main_notebook)
        auto_tab = ttk.Frame(self.main_notebook)
        self.live_tab = ttk.Frame(self.main_notebook)
        self.main_notebook.add(manual_tab, text="手动交易")
        self.main_notebook.add(auto_tab, text="自动模拟")

        quick_frame = ttk.LabelFrame(manual_tab, text=" BTC 短周期手动买卖 ", padding=10)
        quick_frame.pack(fill="x", padx=0, pady=5)

        quick_ctrl_frame = ttk.Frame(quick_frame)
        quick_ctrl_frame.pack(fill="x", pady=(0, 8))
        self.btn_scan_quick = ttk.Button(quick_ctrl_frame, text="扫描短周期", width=14, command=self.scan_quick_button_clicked)
        self.btn_scan_quick.pack(side="left", padx=4)
        self.btn_predict_quick = ttk.Button(quick_ctrl_frame, text="AI概率判断", width=14, command=self.predict_quick_button_clicked)
        self.btn_predict_quick.pack(side="left", padx=4)
        ttk.Label(quick_ctrl_frame, text="买入金额:").pack(side="left", padx=(12, 4))
        self.ent_quick_usdc = ttk.Entry(quick_ctrl_frame, width=8)
        self.ent_quick_usdc.insert(0, "5")
        self.ent_quick_usdc.pack(side="left", padx=4)
        ttk.Label(quick_ctrl_frame, text="最高价:").pack(side="left", padx=(8, 4))
        self.ent_quick_max_price = ttk.Entry(quick_ctrl_frame, width=8)
        self.ent_quick_max_price.insert(0, "0.60")
        self.ent_quick_max_price.pack(side="left", padx=4)
        self.btn_buy_up = ttk.Button(quick_ctrl_frame, text="买 Up", width=10, command=lambda: self.buy_selected_quick_market("UP"))
        self.btn_buy_up.pack(side="left", padx=4)
        self.btn_buy_down = ttk.Button(quick_ctrl_frame, text="买 Down", width=10, command=lambda: self.buy_selected_quick_market("DOWN"))
        self.btn_buy_down.pack(side="left", padx=4)
        reversal_frame = ttk.LabelFrame(auto_tab, text=" 反转策略研究 ", padding=10)
        reversal_frame.pack(fill="x", padx=0, pady=5)
        reversal_row1 = ttk.Frame(reversal_frame)
        reversal_row1.pack(fill="x", pady=(0, 4))
        reversal_row2 = ttk.Frame(reversal_frame)
        reversal_row2.pack(fill="x")
        ttk.Label(reversal_row1, text="策略:").pack(side="left", padx=(4, 4))
        self.cbo_reversal_mode = ttk.Combobox(reversal_row1, width=14, state="readonly", values=(REVERSAL_MODE_RED_UP, REVERSAL_MODE_GREEN_DOWN))
        self.cbo_reversal_mode.set(REVERSAL_MODE_RED_UP)
        self.cbo_reversal_mode.pack(side="left", padx=4)
        ttk.Label(reversal_row1, text="首单U:").pack(side="left", padx=(8, 4))
        self.ent_reversal_usdc = ttk.Entry(reversal_row1, width=7)
        self.ent_reversal_usdc.insert(0, "5")
        self.ent_reversal_usdc.pack(side="left", padx=4)
        ttk.Label(reversal_row1, text="最多单数:").pack(side="left", padx=(8, 4))
        self.ent_reversal_layers = ttk.Entry(reversal_row1, width=7)
        self.ent_reversal_layers.insert(0, "3")
        self.ent_reversal_layers.pack(side="left", padx=4)
        ttk.Label(reversal_row1, text="回测天数:").pack(side="left", padx=(8, 4))
        self.ent_reversal_days = ttk.Entry(reversal_row1, width=7)
        self.ent_reversal_days.insert(0, "365")
        self.ent_reversal_days.pack(side="left", padx=4)
        ttk.Label(reversal_row1, text="入场价:").pack(side="left", padx=(8, 4))
        self.ent_reversal_entry = ttk.Entry(reversal_row1, width=7)
        self.ent_reversal_entry.insert(0, "0.50")
        self.ent_reversal_entry.pack(side="left", padx=4)
        self.btn_reversal_backtest = ttk.Button(reversal_row2, text="反转策略回测", width=16, command=self.reversal_backtest_button_clicked)
        self.btn_reversal_backtest.pack(side="left", padx=4)
        self.btn_reversal_live = ttk.Button(reversal_row2, text="反转策略实时模拟", width=18, command=self.reversal_live_button_clicked)
        self.btn_reversal_live.pack(side="left", padx=4)
        self.btn_stop_reversal = ttk.Button(reversal_row2, text="停止反转模拟", width=18, command=self.stop_reversal_clicked, state="disabled")
        self.btn_stop_reversal.pack(side="left", padx=4)

        self.lbl_quick_signal = ttk.Label(quick_frame, text="只做辅助判断；每次真实下单前都会确认。", foreground="#475569")
        self.lbl_quick_signal.pack(fill="x", pady=(0, 8))

        self.quick_tree = ttk.Treeview(
            quick_frame,
            columns=("period", "end", "up", "down", "spread", "volume", "question"),
            show="headings",
            height=8,
        )
        quick_headings = {
            "period": "周期",
            "end": "结束时间",
            "up": "Up买/卖",
            "down": "Down买/卖",
            "spread": "价差",
            "volume": "24h量",
            "question": "市场",
        }
        quick_widths = {"period": 60, "end": 135, "up": 85, "down": 85, "spread": 60, "volume": 80, "question": 500}
        for col, title in quick_headings.items():
            self.quick_tree.heading(col, text=title)
            self.quick_tree.column(col, width=quick_widths[col], anchor="center" if col != "question" else "w")
        self.quick_tree.pack(fill="x", expand=False)

        auto_ctrl_frame = ttk.Frame(auto_tab)
        auto_ctrl_frame.pack(fill="x", pady=(0, 8))
        self.btn_strategy_logic = ttk.Button(auto_ctrl_frame, text="反转策略说明", width=16, command=self.show_strategy_logic)
        self.btn_strategy_logic.pack(side="left", padx=4)
        self.btn_enable_live_auto = ttk.Button(auto_ctrl_frame, text="显示反转实盘", width=20, command=self.enable_live_auto_tab)
        self.btn_enable_live_auto.pack(side="left", padx=4)

        paper_result_frame = ttk.LabelFrame(auto_tab, text=" 反转模拟结果 ", padding=6)
        paper_result_frame.pack(fill="x", pady=(8, 0))
        self.paper_tree = ttk.Treeview(
            paper_result_frame,
            columns=("round", "status", "direction", "entry", "current", "high", "exit", "pnl", "pct", "slug"),
            show="headings",
            height=5,
        )
        paper_headings = {
            "round": "轮次",
            "status": "结果",
            "direction": "方向",
            "entry": "买入",
            "current": "当前可卖",
            "high": "最高可卖",
            "exit": "卖出",
            "pnl": "盈亏",
            "pct": "盈亏%",
            "slug": "市场",
        }
        paper_widths = {
            "round": 55,
            "status": 105,
            "direction": 55,
            "entry": 60,
            "current": 70,
            "high": 70,
            "exit": 60,
            "pnl": 70,
            "pct": 70,
            "slug": 390,
        }
        for col, title in paper_headings.items():
            self.paper_tree.heading(col, text=title)
            self.paper_tree.column(col, width=paper_widths[col], anchor="center" if col != "slug" else "w")
        self.paper_tree.pack(fill="x", expand=False)

        self.lbl_account_status = ttk.Label(
            manual_tab,
            text="账户状态加载中...",
            foreground="#475569",
            font=("Helvetica", 10),
        )
        self.lbl_account_status.pack(fill="x", padx=0, pady=(4, 0))

        pos_frame = ttk.LabelFrame(manual_tab, text=" 持仓与卖出 ", padding=10)
        pos_frame.pack(fill="x", padx=0, pady=5)

        self.positions_tree = ttk.Treeview(
            pos_frame,
            columns=("outcome", "size", "avg", "cur", "value", "pnl", "pct", "title"),
            show="headings",
            height=5,
        )
        headings = {
            "outcome": "方向",
            "size": "数量",
            "avg": "均价",
            "cur": "现价",
            "value": "现值",
            "pnl": "浮盈亏",
            "pct": "浮盈亏%",
            "title": "市场",
        }
        widths = {"outcome": 70, "size": 80, "avg": 65, "cur": 65, "value": 75, "pnl": 75, "pct": 75, "title": 470}
        for col, title in headings.items():
            self.positions_tree.heading(col, text=title)
            self.positions_tree.column(col, width=widths[col], anchor="center" if col != "title" else "w")
        self.positions_tree.pack(fill="x", expand=False)

        pos_btn_frame = ttk.Frame(pos_frame)
        pos_btn_frame.pack(fill="x", pady=(8, 0))
        self.btn_refresh_positions = ttk.Button(pos_btn_frame, text="刷新持仓", width=12, command=self.refresh_positions_button_clicked)
        self.btn_refresh_positions.pack(side="left", padx=4)
        self.btn_open_market = ttk.Button(pos_btn_frame, text="打开市场", width=12, command=self.open_selected_position_market)
        self.btn_open_market.pack(side="left", padx=4)
        self.btn_sell_limit = ttk.Button(pos_btn_frame, text="限价卖出选中", width=16, command=self.sell_selected_position_limit)
        self.btn_sell_limit.pack(side="left", padx=4)
        self.btn_save_config = ttk.Button(pos_btn_frame, text="保存非敏感配置", width=16, command=self.save_config_to_local)
        self.btn_save_config.pack(side="left", padx=4)

        manual_log_frame = ttk.LabelFrame(manual_tab, text=" 手动交易日志 ", padding=10)
        manual_log_frame.pack(fill="both", expand=True, padx=0, pady=5)
        self.manual_log_text = scrolledtext.ScrolledText(
            manual_log_frame,
            state="disabled",
            height=12,
            bg="#1f2937",
            fg="#e5e7eb",
            font=("Consolas", 10),
        )
        self.manual_log_text.pack(fill="both", expand=True)

        auto_log_frame = ttk.LabelFrame(auto_tab, text=" 自动模拟日志 ", padding=10)
        auto_log_frame.pack(fill="both", expand=True, padx=0, pady=5)
        self.auto_log_text = scrolledtext.ScrolledText(
            auto_log_frame,
            state="disabled",
            height=16,
            bg="#111827",
            fg="#e5e7eb",
            font=("Consolas", 10),
        )
        self.auto_log_text.pack(fill="both", expand=True)

        self.setup_live_auto_tab()

    def setup_live_auto_tab(self):
        live_frame = ttk.LabelFrame(self.live_tab, text=" 反转策略实盘 ", padding=10)
        live_frame.pack(fill="x", padx=0, pady=5)
        live_row1 = ttk.Frame(live_frame)
        live_row1.pack(fill="x", pady=(0, 4))
        live_row2 = ttk.Frame(live_frame)
        live_row2.pack(fill="x")

        ttk.Label(live_row1, text="策略:").pack(side="left", padx=(4, 4))
        self.cbo_live_mode = ttk.Combobox(live_row1, width=14, state="readonly", values=(REVERSAL_MODE_RED_UP, REVERSAL_MODE_GREEN_DOWN))
        self.cbo_live_mode.set(REVERSAL_MODE_RED_UP)
        self.cbo_live_mode.pack(side="left", padx=4)
        ttk.Label(live_row1, text="首单U:").pack(side="left", padx=(8, 4))
        self.ent_live_usdc = ttk.Entry(live_row1, width=7)
        self.ent_live_usdc.insert(0, "5")
        self.ent_live_usdc.pack(side="left", padx=4)
        ttk.Label(live_row1, text="最多单数:").pack(side="left", padx=(8, 4))
        self.ent_live_layers = ttk.Entry(live_row1, width=7)
        self.ent_live_layers.insert(0, "3")
        self.ent_live_layers.pack(side="left", padx=4)
        ttk.Label(live_row1, text="最高入场价:").pack(side="left", padx=(8, 4))
        self.ent_live_entry = ttk.Entry(live_row1, width=7)
        self.ent_live_entry.insert(0, "0.50")
        self.ent_live_entry.pack(side="left", padx=4)
        ttk.Label(live_row2, text="最多小时:").pack(side="left", padx=(8, 4))
        self.ent_live_max_hours = ttk.Entry(live_row2, width=7)
        self.ent_live_max_hours.insert(0, "24")
        self.ent_live_max_hours.pack(side="left", padx=4)
        self.btn_start_live_auto = ttk.Button(live_row2, text="启动反转实盘", width=18, command=self.live_auto_button_clicked)
        self.btn_start_live_auto.pack(side="left", padx=(10, 4))
        self.btn_stop_live_auto = ttk.Button(live_row2, text="停止反转实盘", width=18, command=self.stop_live_auto_clicked, state="disabled")
        self.btn_stop_live_auto.pack(side="left", padx=4)

        live_result_frame = ttk.LabelFrame(self.live_tab, text=" 反转实盘结果 ", padding=6)
        live_result_frame.pack(fill="x", pady=(8, 0))
        self.live_tree = ttk.Treeview(
            live_result_frame,
            columns=("round", "status", "direction", "entry", "current", "high", "exit", "pnl", "pct", "slug"),
            show="headings",
            height=5,
        )
        headings = {
            "round": "轮次",
            "status": "状态",
            "direction": "方向",
            "entry": "买入",
            "current": "当前可卖",
            "high": "最高可卖",
            "exit": "卖出",
            "pnl": "盈亏",
            "pct": "盈亏%",
            "slug": "市场",
        }
        widths = {"round": 55, "status": 105, "direction": 55, "entry": 60, "current": 70, "high": 70, "exit": 60, "pnl": 70, "pct": 70, "slug": 390}
        for col, title in headings.items():
            self.live_tree.heading(col, text=title)
            self.live_tree.column(col, width=widths[col], anchor="center" if col != "slug" else "w")
        self.live_tree.pack(fill="x", expand=False)

        live_log_frame = ttk.LabelFrame(self.live_tab, text=" 实盘自动日志 ", padding=10)
        live_log_frame.pack(fill="both", expand=True, padx=0, pady=5)
        self.live_log_text = scrolledtext.ScrolledText(
            live_log_frame,
            state="disabled",
            height=16,
            bg="#0f172a",
            fg="#e5e7eb",
            font=("Consolas", 10),
        )
        self.live_log_text.pack(fill="both", expand=True)

    def show_strategy_logic(self):
        messagebox.showinfo(
            "反转策略逻辑",
            "当前策略逻辑：\n\n"
            "1. 只观察 BTCUSDT 15m K 线。\n"
            "2. 三连阴转 UP：出现新的连续 3 根阴线后，下一根买 UP。\n"
            "3. 三连阳转 DOWN：出现新的连续 3 根阳线后，下一根买 DOWN。\n"
            "4. 如果本单结算亏损，下一根继续按同方向加注。\n"
            "5. 加注金额按手续费和回本目标自动计算，默认 5 -> 10.36 -> 21.48。\n"
            "6. 最多跑设置的单数，默认 3 单，失败后停止本周期并等待下一次新的触发。\n"
            "7. 回测和实时模拟不会真实下单；隐藏的实盘页会真实提交买单。",
        )

    def enable_live_auto_tab(self):
        if self.live_auto_enabled:
            self.main_notebook.select(self.live_tab)
            return
        if not messagebox.askyesno(
            "显示反转实盘",
            "反转实盘会在触发信号后真实提交 Polymarket UP 或 DOWN 买入订单。\n\n"
            "确认后才会显示实盘分页。是否继续？",
        ):
            return
        if not messagebox.askyesno(
            "二次确认",
            "请再次确认：你理解反转实盘策略可能连续亏损，并愿意自行承担风险。\n\n显示反转实盘分页？",
        ):
            return
        self.live_auto_enabled = True
        self.main_notebook.add(self.live_tab, text="反转实盘")
        self.main_notebook.select(self.live_tab)
        self.log_live(logging.WARNING, "反转实盘分页已显示；启动前请再次确认策略和参数。")

    def load_credentials_from_env(self):
        env_map = {
            self.ent_priv_key: "POLY_PRIVATE_KEY",
            self.ent_sendkey: "SERVERCHAN_SENDKEY",
            self.ent_funder: "POLY_FUNDER_ADDRESS",
            self.ent_minimax_key: "MINIMAX_TOKEN_PLAN_KEY",
        }
        loaded = []
        for entry, name in env_map.items():
            value = os.getenv(name, "").strip()
            if value:
                entry.insert(0, value)
                loaded.append(name)
        if not self.ent_minimax_key.get().strip():
            value = os.getenv("MINIMAX_API_KEY", "").strip()
            if value:
                self.ent_minimax_key.insert(0, value)
                loaded.append("MINIMAX_API_KEY")
        sig_type = os.getenv("POLY_SIGNATURE_TYPE", "").strip()
        if sig_type in {"0", "1", "2", "3"}:
            self.cbo_signature_type.set(sig_type)
        if loaded:
            self.logger.info("已从环境变量读取凭证: %s", ", ".join(loaded))

    def load_env_file(self):
        path = os.path.expanduser("~/.poly_mm_env")
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    if line.startswith("export "):
                        line = line[len("export "):].strip()
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key and key not in os.environ:
                        os.environ[key] = value
        except Exception as e:
            self.logger.warning("读取 ~/.poly_mm_env 失败: %s", e)

    def safe_config(self):
        return {
            "funder": self.ent_funder.get().strip(),
            "signature_type": self.cbo_signature_type.get(),
            "quick_usdc": self.ent_quick_usdc.get().strip(),
            "quick_max_price": self.ent_quick_max_price.get().strip(),
            "reversal_mode": self.cbo_reversal_mode.get().strip(),
            "reversal_usdc": self.ent_reversal_usdc.get().strip(),
            "reversal_layers": self.ent_reversal_layers.get().strip(),
            "reversal_days": self.ent_reversal_days.get().strip(),
            "reversal_entry": self.ent_reversal_entry.get().strip(),
            "live_mode": self.cbo_live_mode.get().strip(),
            "live_usdc": self.ent_live_usdc.get().strip(),
            "live_layers": self.ent_live_layers.get().strip(),
            "live_entry": self.ent_live_entry.get().strip(),
            "live_max_hours": self.ent_live_max_hours.get().strip(),
        }

    def save_config_to_local(self):
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self.safe_config(), f, indent=4, ensure_ascii=False)
            os.chmod(CONFIG_FILE, 0o600)
            self.logger.info("已保存非敏感配置。")
        except Exception as e:
            self.logger.error("保存配置失败: %s", e)

    def load_config_from_local(self):
        if not os.path.exists(CONFIG_FILE):
            return
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
            leaked_keys = {"priv_key", "api_key", "secret", "passphrase", "sendkey", "minimax_key"} & set(config)
            if leaked_keys:
                self.logger.warning("检测到旧配置含敏感字段，本次不会回填: %s", ", ".join(sorted(leaked_keys)))
            self._set_entry(self.ent_funder, config.get("funder", ""))
            if str(config.get("signature_type", "")).strip() in {"0", "1", "2", "3"}:
                self.cbo_signature_type.set(str(config.get("signature_type")).strip())
            self._set_entry(self.ent_quick_usdc, config.get("quick_usdc", "5"))
            self._set_entry(self.ent_quick_max_price, config.get("quick_max_price", "0.60"))
            self.cbo_reversal_mode.set(config.get("reversal_mode", REVERSAL_MODE_RED_UP))
            self._set_entry(self.ent_reversal_usdc, config.get("reversal_usdc", "5"))
            self._set_entry(self.ent_reversal_layers, config.get("reversal_layers", "3"))
            self._set_entry(self.ent_reversal_days, config.get("reversal_days", "365"))
            self._set_entry(self.ent_reversal_entry, config.get("reversal_entry", "0.50"))
            self.cbo_live_mode.set(config.get("live_mode", config.get("reversal_mode", REVERSAL_MODE_RED_UP)))
            self._set_entry(self.ent_live_usdc, config.get("live_usdc", "5"))
            self._set_entry(self.ent_live_layers, config.get("live_layers", config.get("reversal_layers", "3")))
            self._set_entry(self.ent_live_entry, config.get("live_entry", config.get("reversal_entry", "0.50")))
            self._set_entry(self.ent_live_max_hours, config.get("live_max_hours", "24"))
        except Exception as e:
            logging.error("加载配置文件失败: %s", e)

    def _set_entry(self, entry, value):
        entry.delete(0, tk.END)
        entry.insert(0, str(value))

    def validate_credentials_config(self):
        config = {
            "priv_key": self.ent_priv_key.get().strip(),
            "api_key": self.ent_api_key.get().strip(),
            "secret": self.ent_secret.get().strip(),
            "passphrase": self.ent_passphrase.get().strip(),
            "funder": self.ent_funder.get().strip(),
            "signature_type": int(self.cbo_signature_type.get()),
        }
        if not config["priv_key"]:
            raise ValueError("缺少 Polygon 钱包私钥。")
        if config["signature_type"] != 0 and not config["funder"]:
            raise ValueError("签名类型不是 0 时必须填写 Funder 地址。网页 Polymarket 余额通常要用签名类型 3 + Funder 地址。")
        api_values = [config["api_key"], config["secret"], config["passphrase"]]
        if any(api_values) and not all(api_values):
            raise ValueError("CLOB API Key、Secret、Passphrase 要么都填，要么都留空让脚本自动派生。")
        return config

    async def derive_api_creds(self):
        try:
            self.logger.info("正在用私钥派生 CLOB API 凭证...")
            temp_client = ClobClient(host=CLOB_HOST, chain_id=CHAIN_ID, key=self.ent_priv_key.get().strip(), retry_on_error=True)
            creds = await asyncio.to_thread(temp_client.derive_api_key)
            self.logger.info("CLOB API 凭证派生成功。")
            self.last_credential_error = None
            return creds
        except Exception as e:
            # Surface the raw SDK error so users (esp. POLY_1271 deposit-wallet
            # users hitting "signer != API KEY") have a clue without diving into
            # the log file. Status bar is updated; the next order attempt will
            # also see the recorded reason via self.last_credential_error.
            err_text = f"{type(e).__name__}: {e}"
            self.logger.error("派生 CLOB API 凭证失败: %s", err_text, exc_info=True)
            self.last_credential_error = err_text
            try:
                self.root.after(0, lambda et=err_text: self.lbl_quick_signal.configure(
                    text=f"⚠️ CLOB 凭证派生失败: {et[:120]}", foreground="#b91c1c"
                ))
            except Exception:
                pass  # UI may not be initialized in non-GUI test paths
            return None

    def build_client(self, config, creds):
        kwargs = {
            "host": CLOB_HOST,
            "chain_id": CHAIN_ID,
            "key": config["priv_key"],
            "creds": creds,
            "retry_on_error": True,
        }
        if config["signature_type"] != 0:
            kwargs["signature_type"] = config["signature_type"]
            kwargs["funder"] = config["funder"]
        return ClobClient(**kwargs)

    async def fetch_json(self, url: str, params=None):
        try:
            session = _get_aiohttp_session()
            async with session.get(url, params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=aiohttp.ClientTimeout(total=12)) as response:
                if response.status != 200:
                    self.logger.warning("GET %s 返回 HTTP %s", url, response.status)
                    return None
                return await response.json()
        except Exception as e:
            self.logger.warning("GET %s 失败: %s", url, e)
            return None

    def scan_quick_button_clicked(self):
        self.btn_scan_quick.configure(state="disabled")
        self.logger.info("开始扫描 BTC 短周期 Up/Down 市场。")

        def worker():
            loop = asyncio.new_event_loop()
            try:
                markets = loop.run_until_complete(self.fetch_quick_btc_markets())
                self.latest_quick_markets = markets
                self.root.after(0, lambda: self.render_quick_markets(markets))
                self.root.after(0, lambda: self.logger.info("短周期市场扫描完成: %s 个候选。", len(markets)))
            except Exception as e:
                error_text = str(e) or repr(e)
                self.root.after(0, lambda err=error_text: self.logger.error("短周期市场扫描失败: %s", err))
            finally:
                loop.close()
                self.root.after(0, lambda: self.btn_scan_quick.configure(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

    async def fetch_quick_btc_markets(self):
        url = f"{POLYMARKET_BASE_URL}/crypto/bitcoin"
        try:
            session = _get_aiohttp_session()
            async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=aiohttp.ClientTimeout(total=12)) as response:
                if response.status != 200:
                    self.logger.warning("BTC 页面返回 HTTP %s", response.status)
                    return []
                html = await response.text()
        except Exception as e:
            self.logger.warning("读取 BTC 页面失败: %s", e)
            return []

        slugs = []
        for match in re.finditer(r'href="/event/([^"?#/]+)', html):
            slug = match.group(1)
            if slug.endswith("/live"):
                slug = slug.rsplit("/", 1)[0]
            if slug in slugs:
                continue
            if slug.startswith("btc-updown-") or slug.startswith("bitcoin-up-or-down-"):
                slugs.append(slug)

        for slug in self.generated_btc_updown_slugs():
            if slug not in slugs:
                slugs.insert(0, slug)

        markets = []
        now = datetime.now(timezone.utc)
        for slug in slugs[:80]:
            event = await self.fetch_json(f"{GAMMA_EVENT_SLUG_URL}/{slug}")
            if not isinstance(event, dict):
                continue
            for market in event.get("markets") or []:
                item = self.quick_market_candidate(event, market, now)
                if item:
                    markets.append(item)

        markets.sort(key=lambda item: (item.ended, item.end_dt or datetime.max.replace(tzinfo=timezone.utc)))
        return markets[:20]

    def generated_btc_updown_slugs(self):
        now_ts = int(time.time())
        slugs = []
        for period, seconds in (("5m", 300), ("15m", 900), ("4h", 14400)):
            base = now_ts - (now_ts % seconds)
            for offset in (-2, -1, 0, 1, 2):
                start_ts = base + offset * seconds
                if start_ts > 0:
                    slugs.append(f"btc-updown-{period}-{start_ts}")
        return slugs

    def quick_market_candidate(self, event: dict, market: dict, now: datetime):
        if market.get("closed") is True or market.get("active") is False or market.get("acceptingOrders") is False:
            return None
        token_ids = self._parse_token_ids(market.get("clobTokenIds"))
        if len(token_ids) < 2:
            return None
        question = market.get("question") or event.get("title") or ""
        slug = market.get("slug") or event.get("slug") or ""
        if "bitcoin" not in question.lower() and "btc" not in slug.lower():
            return None
        if "up" not in question.lower() or "down" not in question.lower():
            return None

        end_dt = self._parse_datetime(market.get("endDate") or event.get("endDate"))
        best_bid = self._optional_float(market.get("bestBid"))
        best_ask = self._optional_float(market.get("bestAsk"))
        if best_bid is None or best_ask is None or best_bid <= 0 or best_ask >= 1 or best_bid >= best_ask:
            return None

        return QuickMarket(
            slug=slug,
            event_slug=event.get("slug") or slug,
            question=question,
            yes_id=token_ids[0],
            no_id=token_ids[1],
            tick_size=str(market.get("orderPriceMinTickSize") or "0.01"),
            period=self.quick_period_from_slug_or_title(slug, question),
            end_dt=end_dt,
            ended=bool(end_dt and end_dt <= now),
            up_bid=best_bid,
            up_ask=best_ask,
            down_bid=max(0.0, 1.0 - best_ask),
            down_ask=min(1.0, 1.0 - best_bid),
            spread=best_ask - best_bid,
            volume24h=self._float_or_zero(market.get("volume24hrClob") or market.get("volume24hr")),
        )

    def quick_period_from_slug_or_title(self, slug: str, question: str):
        match = re.search(r"updown-(\d+[mh])-", slug)
        if match:
            return match.group(1)
        lower = question.lower()
        if "15" in lower and ("minute" in lower or "min" in lower):
            return "15m"
        if "5" in lower and ("minute" in lower or "min" in lower):
            return "5m"
        if "hour" in lower or re.search(r"\d+(am|pm)", lower):
            return "1h"
        if re.search(r"\bon\s+[a-z]+-\d{1,2}-\d{4}\b", slug) or " on " in lower:
            return "1d"
        return "?"

    def render_quick_markets(self, markets):
        for item in self.quick_tree.get_children():
            self.quick_tree.delete(item)
        for index, market in enumerate(markets):
            end_text = "--"
            if market.end_dt:
                end_text = market.end_dt.astimezone().strftime("%m-%d %H:%M")
            if market.ended:
                end_text += " 已结束"
            values = (
                market.period,
                end_text,
                f"{market.up_bid:.2f}/{market.up_ask:.2f}",
                f"{market.down_bid:.2f}/{market.down_ask:.2f}",
                f"{market.spread:.2f}",
                f"{market.volume24h:.0f}",
                market.question[:100],
            )
            self.quick_tree.insert("", "end", iid=str(index), values=values)

    def selected_quick_market(self):
        selected = self.quick_tree.selection()
        if not selected:
            messagebox.showinfo("提示", "请先在短周期市场表里选中一行。")
            return None
        idx = int(selected[0])
        if idx >= len(self.latest_quick_markets):
            messagebox.showinfo("提示", "选中的短周期市场已过期，请重新扫描。")
            return None
        return self.latest_quick_markets[idx]

    def predict_quick_button_clicked(self):
        self.btn_predict_quick.configure(state="disabled", text="判断中...")
        self.lbl_quick_signal.configure(text=f"正在计算 BTC 短周期概率... {datetime.now().strftime('%H:%M:%S')}")
        self.logger.info("开始计算 BTC 短周期 AI 概率。")
        selected_market = None
        selected = self.quick_tree.selection()
        if selected:
            idx = int(selected[0])
            if idx < len(self.latest_quick_markets):
                selected_market = self.latest_quick_markets[idx]
        minimax_key = self.ent_minimax_key.get().strip()

        def worker():
            loop = asyncio.new_event_loop()
            try:
                signal = loop.run_until_complete(self.fetch_btc_signal(selected_market))
                signal["llm"] = loop.run_until_complete(self.fetch_minimax_prediction(signal, minimax_key, selected_market)) if minimax_key else None
                self.latest_signal = signal
                self.root.after(0, lambda: self.render_btc_signal(signal))
            except Exception as e:
                error_text = str(e) or repr(e)
                self.root.after(0, lambda err=error_text: self.logger.error("AI概率判断失败: %s", err))
            finally:
                loop.close()
                self.root.after(0, lambda: self.btn_predict_quick.configure(state="normal", text="AI概率判断"))

        threading.Thread(target=worker, daemon=True).start()

    async def fetch_btc_signal(self, selected_market: QuickMarket | None = None):
        horizon_minutes = self.market_horizon_minutes(selected_market)
        lookback = max(80, min(1000, horizon_minutes * 4 + 40))
        params = {"symbol": "BTCUSDT", "interval": "1m", "limit": str(lookback)}
        url = "https://api.binance.com/api/v3/klines"
        try:
            session = _get_aiohttp_session()
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status != 200:
                    raise RuntimeError(f"Binance HTTP {response.status}")
                klines = await response.json()
        except Exception:
            url = "https://data-api.binance.vision/api/v3/klines"
            session = _get_aiohttp_session()
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status != 200:
                    raise RuntimeError(f"Binance vision HTTP {response.status}")
                klines = await response.json()

        closes = [float(row[4]) for row in klines]
        if len(closes) < 30:
            raise RuntimeError("K线数据不足")

        current = closes[-1]
        fast_window = max(3, min(60, horizon_minutes // 3))
        mid_window = max(5, min(240, horizon_minutes))
        slow_window = max(10, min(720, horizon_minutes * 2))
        ret_fast = self.window_return(closes, fast_window)
        ret_mid = self.window_return(closes, mid_window)
        ret_slow = self.window_return(closes, slow_window)
        ema_fast_period = max(5, min(60, max(5, horizon_minutes // 2)))
        ema_slow_period = max(12, min(240, max(12, horizon_minutes * 2)))
        ema_fast = self.ema(closes[-max(ema_slow_period * 4, 30):], ema_fast_period)
        ema_slow = self.ema(closes[-max(ema_slow_period * 4, 30):], ema_slow_period)
        rsi_period = max(7, min(28, horizon_minutes if horizon_minutes <= 60 else 14))
        rsi = self.rsi(closes, rsi_period)
        returns = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))]
        vol_window = max(15, min(240, horizon_minutes * 2))
        recent_returns = returns[-vol_window:]
        mean_return = sum(recent_returns) / len(recent_returns)
        vol = max(0.0001, (sum((x - mean_return) ** 2 for x in recent_returns) / len(recent_returns)) ** 0.5)
        momentum = 0.50 * ret_fast + 0.35 * ret_mid + 0.15 * ret_slow
        trend = (ema_fast / ema_slow - 1.0) if ema_slow else 0.0
        rsi_bias = (rsi - 50.0) / 10000.0
        z = max(-2.0, min(2.0, (momentum + trend + rsi_bias) / (vol * 3.0)))
        raw_prob_up = 1.0 / (1.0 + pow(2.718281828, -z))
        prob_up = 0.5 + (raw_prob_up - 0.5) * 0.6
        return {
            "fetched_at": datetime.now().strftime("%H:%M:%S"),
            "price": current,
            "market_period": selected_market.period if selected_market else "未选中",
            "market_question": selected_market.question if selected_market else "",
            "horizon_minutes": horizon_minutes,
            "prob_up": prob_up,
            "prob_down": 1.0 - prob_up,
            "confidence": abs(prob_up - 0.5) * 2.0,
            "ret_fast": ret_fast,
            "ret_mid": ret_mid,
            "ret_slow": ret_slow,
            "fast_window": fast_window,
            "mid_window": mid_window,
            "slow_window": slow_window,
            "ema_fast": ema_fast,
            "ema_slow": ema_slow,
            "rsi": rsi,
            "vol": vol,
        }

    def market_horizon_minutes(self, selected_market: QuickMarket | None):
        if selected_market and selected_market.end_dt:
            seconds_left = (selected_market.end_dt - datetime.now(timezone.utc)).total_seconds()
            if seconds_left > 0:
                return max(3, min(1440, int(seconds_left / 60)))
        period = selected_market.period if selected_market else ""
        mapping = {"5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}
        return mapping.get(period, 15)

    def window_return(self, closes, minutes: int):
        steps = max(1, min(minutes, len(closes) - 1))
        return closes[-1] / closes[-steps - 1] - 1.0

    async def fetch_minimax_prediction(self, signal, api_key: str, selected_market: QuickMarket | None = None):
        market_block = {}
        if selected_market:
            market_block = {
                "question": selected_market.question,
                "period": selected_market.period,
                "end_time": selected_market.end_dt.isoformat() if selected_market.end_dt else None,
                "up_bid": selected_market.up_bid,
                "up_ask": selected_market.up_ask,
                "down_bid": selected_market.down_bid,
                "down_ask": selected_market.down_ask,
                "spread": selected_market.spread,
                "volume24h": selected_market.volume24h,
            }

        payload = {
            "model": MINIMAX_MODEL,
            "temperature": 0.2,
            "top_p": 0.9,
            "max_completion_tokens": 220,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "name": "TradingRiskAnalyst",
                    "content": (
                        "只输出 JSON。不要解释。不要推理过程。不要 markdown。"
                        "JSON keys: prob_up, prob_down, action, confidence, edge_summary, reason, risk。"
                    ),
                },
                {
                    "role": "user",
                    "name": "User",
                    "content": json.dumps(
                        {
                            "rule": "Return compact JSON only. action in BUY_UP, BUY_DOWN, NO_TRADE. confidence in LOW, MEDIUM, HIGH. Choose NO_TRADE if edge unclear.",
                            "local": self.compact_signal(signal),
                            "market": market_block,
                        },
                        ensure_ascii=False,
                        default=str,
                    ),
                },
            ],
        }

        try:
            body = await self.post_minimax_with_retry(api_key, payload)
        except Exception as e:
            error_text = f"{type(e).__name__}: {str(e) or repr(e)}"
            self.logger.error("MiniMax 大模型预测失败: %s", error_text)
            return self.local_structured_decision(signal, selected_market, f"MiniMax请求失败: {error_text}")

        try:
            data = json.loads(body)
            content = data["choices"][0]["message"]["content"]
            finish_reason = data["choices"][0].get("finish_reason")
            if finish_reason == "length":
                self.logger.warning("MiniMax 输出被截断，改用本地结构化决策。")
                return self.local_structured_decision(signal, selected_market, "MiniMax输出被截断")
            parsed = self.parse_minimax_json(content)
            parsed["usage"] = data.get("usage") or {}
            parsed["source"] = "MINIMAX"
            return parsed
        except Exception as e:
            self.logger.error("MiniMax 返回解析失败: %s | 原文=%s", e, body[:500])
            return self.local_structured_decision(signal, selected_market, f"MiniMax返回解析失败: {e}")

    async def post_minimax_with_retry(self, api_key: str, payload: dict):
        last_error = None
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
                    body = await response.text()
                    if response.status != 200:
                        raise RuntimeError(f"MiniMax HTTP {response.status}: {body[:500]}")
                    return body
            except Exception as e:
                last_error = e
                self.logger.warning("MiniMax 请求第 %s 次失败: %s: %s", attempt, type(e).__name__, str(e) or repr(e))
                if attempt < 2:
                    await asyncio.sleep(1.5)
        raise last_error

    def compact_signal(self, signal):
        return {
            "period": signal.get("market_period"),
            "horizon_min": signal.get("horizon_minutes"),
            "price": round(float(signal.get("price", 0)), 2),
            "p_up": round(float(signal.get("prob_up", 0.5)), 4),
            "p_down": round(float(signal.get("prob_down", 0.5)), 4),
            "confidence": round(float(signal.get("confidence", 0)), 4),
            "r_fast": round(float(signal.get("ret_fast", 0)), 5),
            "r_mid": round(float(signal.get("ret_mid", 0)), 5),
            "r_slow": round(float(signal.get("ret_slow", 0)), 5),
            "rsi": round(float(signal.get("rsi", 50)), 2),
            "vol": round(float(signal.get("vol", 0)), 6),
        }

    def local_structured_decision(self, signal, selected_market: QuickMarket | None = None, fallback_reason=""):
        prob_up = min(max(float(signal.get("prob_up", 0.5)), 0.0), 1.0)
        prob_down = 1.0 - prob_up
        confidence_value = abs(prob_up - 0.5) * 2.0
        confidence = "LOW"
        if confidence_value >= 0.55:
            confidence = "HIGH"
        elif confidence_value >= 0.30:
            confidence = "MEDIUM"

        action = "NO_TRADE"
        edge_summary = "优势不足"
        reason = "本地概率与盘口优势不够明确"
        if selected_market:
            up_ask = float(selected_market.up_ask)
            down_ask = float(selected_market.down_ask)
            up_edge = prob_up - up_ask
            down_edge = prob_down - down_ask
            min_edge = 0.04
            if up_edge >= min_edge and confidence_value >= 0.25:
                action = "BUY_UP"
                edge_summary = f"Up 概率高于买价约 {up_edge * 100:.1f} 个百分点"
                reason = "本地趋势偏上且相对盘口有正边际"
            elif down_edge >= min_edge and confidence_value >= 0.25:
                action = "BUY_DOWN"
                edge_summary = f"Down 概率高于买价约 {down_edge * 100:.1f} 个百分点"
                reason = "本地趋势偏下且相对盘口有正边际"
        elif confidence_value >= 0.45:
            action = "BUY_UP" if prob_up > prob_down else "BUY_DOWN"
            edge_summary = "仅基于本地概率"
            reason = "未选中盘口，无法校验赔率边际"

        return {
            "prob_up": prob_up,
            "prob_down": prob_down,
            "action": action,
            "confidence": confidence,
            "edge_summary": edge_summary,
            "reason": reason,
            "risk": fallback_reason or "短周期噪声高，必须控制仓位和价格上限",
            "source": "LOCAL_FALLBACK",
            "usage": {},
        }

    def parse_minimax_json(self, content: str):
        cleaned = re.sub(r"<think>.*?</think>", "", content or "", flags=re.S).strip()
        if not cleaned and content:
            cleaned = content.split("</think>", 1)[-1].strip() if "</think>" in content else content.strip()
        # Prefer the LAST balanced {...} block — LLMs often emit reasoning
        # chain + final JSON; greedy `\{.*\}` would over-capture both.
        candidates = re.findall(r"\{[^{}]*\}", cleaned, flags=re.S)
        if candidates:
            cleaned = candidates[-1]
        if not cleaned.startswith("{"):
            raise ValueError("MiniMax 未返回 JSON 对象")
        parsed = json.loads(cleaned)
        prob_up = self._safe_prob(parsed.get("prob_up"), default=0.5)
        prob_down = self._safe_prob(parsed.get("prob_down"), default=1.0 - prob_up)
        parsed["prob_up"] = prob_up
        parsed["prob_down"] = prob_down
        if parsed.get("action") not in {"BUY_UP", "BUY_DOWN", "NO_TRADE"}:
            parsed["action"] = "NO_TRADE"
        if parsed.get("confidence") not in {"LOW", "MEDIUM", "HIGH"}:
            parsed["confidence"] = "LOW"
        return parsed

    @staticmethod
    def _safe_prob(value, default):
        try:
            v = float(value)
        except (TypeError, ValueError):
            return default
        if v != v or v in (float("inf"), float("-inf")):
            return default
        return min(max(v, 0.0), 1.0)

    def render_btc_signal(self, signal):
        direction = "Up" if signal["prob_up"] >= 0.5 else "Down"
        text = (
            f"{signal['fetched_at']} | {signal.get('market_period', '')}/{signal.get('horizon_minutes', '--')}m | 本地: {direction} "
            f"Up {signal['prob_up'] * 100:.1f}% / Down {signal['prob_down'] * 100:.1f}% "
            f"| 置信 {signal['confidence'] * 100:.0f}% | RSI {signal['rsi']:.1f}"
        )
        llm = signal.get("llm")
        if llm:
            if llm.get("error"):
                text += " | MiniMax失败，已用本地信号"
            else:
                action_map = {"BUY_UP": "买Up", "BUY_DOWN": "买Down", "NO_TRADE": "不交易"}
                source_label = "MiniMax" if llm.get("source") != "LOCAL_FALLBACK" else "本地兜底"
                text += (
                    f" | {source_label}: Up {llm['prob_up'] * 100:.1f}% / Down {llm['prob_down'] * 100:.1f}% "
                    f"| {action_map.get(llm.get('action'), '不交易')} | {llm.get('confidence', 'LOW')}"
                )
        self.lbl_quick_signal.configure(text=text)
        self.logger.info(
            "本地概率[%s/%sm]: Up %.1f%% / Down %.1f%%，置信 %.0f%%，%sm %.3f%%，%sm %.3f%%，%sm %.3f%%，RSI %.1f",
            signal.get("market_period", ""),
            signal.get("horizon_minutes", ""),
            signal["prob_up"] * 100,
            signal["prob_down"] * 100,
            signal["confidence"] * 100,
            signal["fast_window"],
            signal["ret_fast"] * 100,
            signal["mid_window"],
            signal["ret_mid"] * 100,
            signal["slow_window"],
            signal["ret_slow"] * 100,
            signal["rsi"],
        )
        if llm:
            if llm.get("error"):
                self.logger.warning("MiniMax 综合预测不可用: %s", llm["error"])
            else:
                source_label = "MiniMax综合" if llm.get("source") != "LOCAL_FALLBACK" else "本地兜底"
                self.logger.info(
                    "%s: Up %.1f%% / Down %.1f%% | 动作=%s | 置信=%s | %s | 风险=%s | tokens=%s",
                    source_label,
                    llm["prob_up"] * 100,
                    llm["prob_down"] * 100,
                    llm.get("action"),
                    llm.get("confidence"),
                    llm.get("reason", ""),
                    llm.get("risk", ""),
                    (llm.get("usage") or {}).get("total_tokens", "--"),
                )

    def reversal_config_from_ui(self):
        try:
            config = {
                "mode": self.cbo_reversal_mode.get().strip() or REVERSAL_MODE_RED_UP,
                "initial_usdc": float(self.ent_reversal_usdc.get().strip()),
                "max_layers": int(float(self.ent_reversal_layers.get().strip())),
                "days": int(float(self.ent_reversal_days.get().strip())),
                "entry_price": float(self.ent_reversal_entry.get().strip()),
                "fee_rate": 0.07,
            }
        except ValueError:
            messagebox.showerror("参数错误", "反转策略参数必须是数字。")
            return None
        if config["initial_usdc"] <= 0:
            messagebox.showerror("参数错误", "首单U必须大于 0。")
            return None
        if config["max_layers"] <= 0 or config["max_layers"] > 10:
            messagebox.showerror("参数错误", "最多单数建议在 1 到 10 之间。")
            return None
        if config["days"] <= 0 or config["days"] > 1000:
            messagebox.showerror("参数错误", "回测天数必须在 1 到 1000 之间。")
            return None
        if not (0 < config["entry_price"] < 1):
            messagebox.showerror("参数错误", "入场价必须在 0 到 1 之间。")
            return None
        if config["mode"] not in {REVERSAL_MODE_RED_UP, REVERSAL_MODE_GREEN_DOWN}:
            messagebox.showerror("参数错误", "请选择有效的反转策略。")
            return None
        return config

    def reversal_profile(self, mode):
        if mode == REVERSAL_MODE_GREEN_DOWN:
            return {
                "mode": REVERSAL_MODE_GREEN_DOWN,
                "label": "三连阳转阴 DOWN",
                "trigger_color": "G",
                "trigger_name": "阳线",
                "win_color": "R",
                "direction": "DOWN",
                "token_attr": "no_id",
                "ask_attr": "down_ask",
                "settlement_bid_attr": "down_bid",
            }
        return {
            "mode": REVERSAL_MODE_RED_UP,
            "label": "三连阴转阳 UP",
            "trigger_color": "R",
            "trigger_name": "阴线",
            "win_color": "G",
            "direction": "UP",
            "token_attr": "yes_id",
            "ask_attr": "up_ask",
            "settlement_bid_attr": "up_bid",
        }

    def matching_streak(self, colors, target_color):
        streak = 0
        for color in reversed(colors):
            if color == target_color:
                streak += 1
            else:
                break
        return streak

    def reversal_backtest_button_clicked(self):
        config = self.reversal_config_from_ui()
        if not config:
            return
        self.btn_reversal_backtest.configure(state="disabled")
        profile = self.reversal_profile(config["mode"])
        self.log_auto(
            logging.INFO,
            "开始%s回测: days=%s initial=%.2f layers=%s entry=%.2f",
            profile["label"],
            config["days"],
            config["initial_usdc"],
            config["max_layers"],
            config["entry_price"],
        )

        def worker():
            self.set_log_category("auto")
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(self.run_reversal_backtest(config))
                self.root.after(0, lambda result=result: self.log_auto(logging.INFO, "%s", result))
            except Exception as e:
                error_text = str(e) or repr(e)
                self.root.after(0, lambda err=error_text: self.log_auto(logging.ERROR, "反转策略回测失败: %s", err))
            finally:
                loop.close()
                self.root.after(0, lambda: self.btn_reversal_backtest.configure(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

    def reversal_live_button_clicked(self):
        if self.reversal_running:
            messagebox.showinfo("反转策略实时模拟", "反转策略实时模拟正在运行中。")
            return
        config = self.reversal_config_from_ui()
        if not config:
            return
        self.reversal_running = True
        self.reversal_stop_requested.clear()
        profile = self.reversal_profile(config["mode"])
        self.btn_reversal_live.configure(state="disabled", text="反转模拟中")
        self.btn_stop_reversal.configure(state="normal")
        self.log_auto(
            logging.INFO,
            "启动%s实时模拟: initial=%.2f layers=%s entry=%.2f",
            profile["label"],
            config["initial_usdc"],
            config["max_layers"],
            config["entry_price"],
        )

        def worker():
            self.set_log_category("auto")
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(self.run_reversal_live_sim(config))
                self.root.after(0, lambda result=result: self.log_auto(logging.INFO, "反转策略实时模拟结束: %s", result))
            except Exception as e:
                error_text = str(e) or repr(e)
                self.root.after(0, lambda err=error_text: self.log_auto(logging.ERROR, "反转策略实时模拟失败: %s", err))
            finally:
                loop.close()
                self.reversal_running = False
                self.root.after(0, lambda: self.btn_reversal_live.configure(state="normal", text="反转策略实时模拟"))
                self.root.after(0, lambda: self.btn_stop_reversal.configure(state="disabled"))

        threading.Thread(target=worker, daemon=True).start()

    def stop_reversal_clicked(self):
        if self.reversal_running:
            self.reversal_stop_requested.set()
            self.btn_stop_reversal.configure(state="disabled")
            self.log_auto(logging.WARNING, "已请求停止反转策略实时模拟。")

    def reversal_factors(self, entry_price: float, fee_rate: float = 0.07):
        win_factor = 1.0 / entry_price - 1.0 - fee_rate * (1.0 - entry_price)
        loss_factor = 1.0 + fee_rate * (1.0 - entry_price)
        return win_factor, loss_factor

    def reversal_stakes(self, initial_usdc: float, entry_price: float, max_layers: int, fee_rate: float = 0.07):
        win_factor, loss_factor = self.reversal_factors(entry_price, fee_rate)
        target_profit = win_factor * initial_usdc
        stakes = []
        accumulated_loss = 0.0
        for _ in range(max_layers):
            stake = (accumulated_loss + target_profit) / win_factor
            stakes.append(stake)
            accumulated_loss += loss_factor * stake
        return stakes, win_factor, loss_factor, target_profit

    def kline_color(self, row):
        open_price = float(row[1])
        close_price = float(row[4])
        if close_price < open_price:
            return "R"
        if close_price > open_price:
            return "G"
        return "D"

    def fmt_kline_time(self, row):
        return datetime.fromtimestamp(row[0] / 1000, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")

    async def fetch_btc_15m_klines(self, days=7, limit=1000):
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - int(days * 24 * 3600 * 1000)
        rows = []
        current = start_ms
        urls = ["https://api.binance.com/api/v3/klines", "https://data-api.binance.vision/api/v3/klines"]
        while current < end_ms:
            params = {
                "symbol": "BTCUSDT",
                "interval": "15m",
                "startTime": str(current),
                "endTime": str(end_ms),
                "limit": str(limit),
            }
            data = None
            last_error = None
            for url in urls:
                try:
                    session = _get_aiohttp_session()
                    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as response:
                        if response.status != 200:
                            raise RuntimeError(f"Binance kline HTTP {response.status}")
                        data = await response.json()
                        break
                except Exception as e:
                    last_error = e
            if data is None:
                raise RuntimeError(f"读取 Binance 15m K 线失败: {last_error}")
            if not data:
                break
            rows.extend(data)
            next_start = int(data[-1][0]) + 15 * 60 * 1000
            if next_start <= current:
                break
            current = next_start
            await asyncio.sleep(0.02)

        now_ms = int(time.time() * 1000)
        deduped = {}
        for row in rows:
            if int(row[6]) <= now_ms:
                deduped[int(row[0])] = row
        return [deduped[key] for key in sorted(deduped)]

    async def run_reversal_backtest(self, config):
        rows = await self.fetch_btc_15m_klines(config["days"])
        if len(rows) < 10:
            raise RuntimeError("K 线数量不足，无法回测。")
        profile = self.reversal_profile(config.get("mode", REVERSAL_MODE_RED_UP))
        colors = [self.kline_color(row) for row in rows]
        stakes, win_factor, loss_factor, target_profit = self.reversal_stakes(
            config["initial_usdc"],
            config["entry_price"],
            config["max_layers"],
            config["fee_rate"],
        )
        cycles = []
        i = 2
        while i < len(colors) - 1:
            is_new_trigger = colors[i - 2:i + 1] == [profile["trigger_color"]] * 3 and (i < 3 or colors[i - 3] != profile["trigger_color"])
            if not is_new_trigger:
                i += 1
                continue
            trigger_index = i
            pnl = None
            rows_used = []
            last_trade_index = trigger_index
            for layer, stake in enumerate(stakes, start=1):
                trade_index = trigger_index + layer
                if trade_index >= len(colors):
                    break
                last_trade_index = trade_index
                win = colors[trade_index] == profile["win_color"]
                rows_used.append({
                    "layer": layer,
                    "time": self.fmt_kline_time(rows[trade_index]),
                    "stake": stake,
                    "win": win,
                    "color": colors[trade_index],
                })
                if win:
                    pnl = target_profit
                    break
            if pnl is None and len(rows_used) == len(stakes):
                pnl = -sum(loss_factor * stake for stake in stakes[:len(rows_used)])
            elif pnl is None:
                break
            cycles.append({
                "trigger": self.fmt_kline_time(rows[trigger_index]),
                "pnl": pnl,
                "win": pnl > 0,
                "layers": len(rows_used),
                "rows": rows_used,
            })
            i = last_trade_index + 1
            while i < len(colors) and colors[i] == profile["trigger_color"]:
                i += 1

        total_pnl = sum(item["pnl"] for item in cycles)
        wins = sum(1 for item in cycles if item["win"])
        losses = len(cycles) - wins
        equity = 0.0
        peak = 0.0
        max_drawdown = 0.0
        for item in cycles:
            equity += item["pnl"]
            peak = max(peak, equity)
            max_drawdown = min(max_drawdown, equity - peak)
        full_loss = -sum(loss_factor * stake for stake in stakes)
        lines = [
            f"{profile['label']} 回测完成",
            f"区间: {self.fmt_kline_time(rows[0])} 到 {self.fmt_kline_time(rows[-1])}",
            f"K线数: {len(rows)} | 触发周期: {len(cycles)} | 胜: {wins} | 败: {losses} | 胜率: {(wins / len(cycles) * 100 if cycles else 0):.2f}%",
            f"首单: {config['initial_usdc']:.2f}U | 入场价: {config['entry_price']:.2f} | 最多单数: {config['max_layers']}",
            f"下注序列: {' -> '.join(f'{x:.2f}' for x in stakes)}",
            f"单周期赢目标: +{target_profit:.2f}U | 单周期全亏: {full_loss:.2f}U",
            f"总盈亏: {total_pnl:+.2f}U | 最大回撤: {max_drawdown:.2f}U",
        ]
        if cycles:
            lines.append("最近5次触发:")
            for item in cycles[-5:]:
                lines.append(f"- {item['trigger']} | {'WIN' if item['win'] else 'LOSS'} | layers={item['layers']} | pnl={item['pnl']:+.2f}U")
        return "\n".join(lines)

    async def run_reversal_live_sim(self, config):
        profile = self.reversal_profile(config.get("mode", REVERSAL_MODE_RED_UP))
        stakes, win_factor, loss_factor, target_profit = self.reversal_stakes(
            config["initial_usdc"],
            config["entry_price"],
            config["max_layers"],
            config["fee_rate"],
        )
        self.log_auto(logging.INFO, "%s 下注序列: %s", profile["label"], " -> ".join(f"{x:.2f}" for x in stakes))
        seen_triggers = set()
        cycle_count = 0
        while not self.reversal_stop_requested.is_set():
            rows = await self.fetch_btc_15m_klines(3)
            if len(rows) < 6:
                await self.sleep_with_stop(60, self.reversal_stop_requested)
                continue
            colors = [self.kline_color(row) for row in rows]
            i = len(rows) - 1
            trigger_found = colors[i - 2:i + 1] == [profile["trigger_color"]] * 3 and (i < 3 or colors[i - 3] != profile["trigger_color"])
            trigger_key = int(rows[i][0])
            if not trigger_found or trigger_key in seen_triggers:
                streak = self.matching_streak(colors, profile["trigger_color"])
                self.log_auto(logging.INFO, "%s 实时等待中: 当前连续%s=%s | 最新K线=%s", profile["label"], profile["trigger_name"], streak, self.fmt_kline_time(rows[-1]))
                await self.sleep_with_stop(60, self.reversal_stop_requested)
                continue
            seen_triggers.add(trigger_key)
            cycle_count += 1
            self.log_auto(logging.INFO, "触发%s实时模拟 #%s: 第3根=%s", profile["label"], cycle_count, self.fmt_kline_time(rows[i]))
            accumulated_loss = 0.0
            cycle_done = False
            for layer, stake in enumerate(stakes, start=1):
                trade_open = trigger_key + layer * 15 * 60 * 1000
                while not self.reversal_stop_requested.is_set():
                    recent = await self.fetch_btc_15m_klines(3)
                    trade_rows = [row for row in recent if int(row[0]) == trade_open]
                    if trade_rows:
                        trade_row = trade_rows[0]
                        break
                    wait_until = datetime.fromtimestamp((trade_open + 15 * 60 * 1000) / 1000, tz=timezone.utc).astimezone().strftime("%H:%M:%S")
                    self.log_auto(logging.INFO, "等待第 %s 单结算: 约 %s", layer, wait_until)
                    await self.sleep_with_stop(60, self.reversal_stop_requested)
                if self.reversal_stop_requested.is_set():
                    break
                win = self.kline_color(trade_row) == profile["win_color"]
                if win:
                    pnl = target_profit
                    status = "REVERSAL_WIN"
                    cycle_done = True
                else:
                    loss = loss_factor * stake
                    accumulated_loss += loss
                    pnl = -loss
                    status = "REVERSAL_LOSS" if layer == len(stakes) else "REVERSAL_NEXT"
                row = {
                    "round": f"R{cycle_count}-{layer}",
                    "slug": f"BTCUSDT 15m {self.fmt_kline_time(trade_row)}",
                    "status": status,
                    "direction": profile["direction"],
                    "entry": config["entry_price"],
                    "current": 1.0 if win else 0.0,
                    "high": 1.0 if win else 0.0,
                    "exit": 1.0 if win else 0.0,
                    "pnl": pnl,
                    "pnl_pct": pnl / stake * 100 if stake else 0.0,
                    "entered": True,
                    "result": f"{status} | stake={stake:.2f}U | pnl={pnl:+.2f}U",
                }
                self.paper_results.append(row)
                self.root.after(0, self.render_paper_results)
                self.log_auto(logging.INFO, "%s 实时第 %s 单: %s", profile["label"], layer, row["result"])
                if cycle_done or layer == len(stakes):
                    break
            await self.sleep_with_stop(60, self.reversal_stop_requested)
        return f"已停止，触发周期={cycle_count}"

    def live_auto_button_clicked(self):
        if self.live_auto_running:
            messagebox.showinfo("反转策略实盘", "反转策略实盘正在运行中。")
            return
        try:
            config = {
                "mode": self.cbo_live_mode.get().strip() or REVERSAL_MODE_RED_UP,
                "initial_usdc": float(self.ent_live_usdc.get().strip()),
                "max_layers": int(float(self.ent_live_layers.get().strip())),
                "entry_price": float(self.ent_live_entry.get().strip()),
                "max_hours": float(self.ent_live_max_hours.get().strip()),
                "fee_rate": 0.07,
            }
        except ValueError:
            messagebox.showerror("参数错误", "反转策略实盘参数必须是数字。")
            return
        if config["mode"] not in {REVERSAL_MODE_RED_UP, REVERSAL_MODE_GREEN_DOWN}:
            messagebox.showerror("参数错误", "请选择有效的实盘反转策略。")
            return
        if config["initial_usdc"] <= 0:
            messagebox.showerror("参数错误", "首单U必须大于 0。")
            return
        if config["max_layers"] <= 0 or config["max_layers"] > 10:
            messagebox.showerror("参数错误", "最多单数建议在 1 到 10 之间。")
            return
        if not (0 < config["entry_price"] < 1):
            messagebox.showerror("参数错误", "最高入场价必须在 0 到 1 之间。")
            return
        if config["max_hours"] <= 0 or config["max_hours"] > 168:
            messagebox.showerror("参数错误", "最多小时必须在 0 到 168 之间。")
            return
        profile = self.reversal_profile(config["mode"])
        stakes, _, _, _ = self.reversal_stakes(config["initial_usdc"], config["entry_price"], config["max_layers"], config["fee_rate"])
        if not messagebox.askyesno(
            "确认启动反转实盘",
            f"即将启动真实{profile['label']}策略：\n\n"
            f"下注序列: {' -> '.join(f'{x:.2f}' for x in stakes)} USDC\n"
            f"最高入场价: {config['entry_price']:.4f}\n"
            f"最多小时: {config['max_hours']:.2f}\n\n"
            f"触发后程序会真实提交 {profile['direction']} 买入订单，可能连续亏损。确认启动？",
        ):
            return

        self.live_auto_running = True
        self.live_auto_stop_requested.clear()
        self.live_results = []
        self.render_live_results()
        self.btn_start_live_auto.configure(state="disabled", text="反转实盘运行中")
        self.btn_stop_live_auto.configure(state="normal")
        self.log_live(
            logging.WARNING,
            "启动%s实盘: max_hours=%.2f | initial=%.2f | layers=%s | max_entry=%.2f",
            profile["label"],
            config["max_hours"],
            config["initial_usdc"],
            config["max_layers"],
            config["entry_price"],
        )

        def worker():
            self.set_log_category("live")
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(self.run_reversal_live_real(config))
                self.root.after(0, lambda result=result: self.log_live(logging.WARNING, "反转策略实盘结束: %s", result))
            except Exception as e:
                error_text = str(e) or repr(e)
                self.root.after(0, lambda err=error_text: self.log_live(logging.ERROR, "反转策略实盘失败: %s", err))
            finally:
                loop.close()
                self.live_auto_running = False
                self.root.after(0, lambda: self.btn_start_live_auto.configure(state="normal", text="启动反转实盘"))
                self.root.after(0, lambda: self.btn_stop_live_auto.configure(state="disabled"))

        threading.Thread(target=worker, daemon=True).start()

    def stop_live_auto_clicked(self):
        if self.live_auto_running:
            self.live_auto_stop_requested.set()
            self.btn_stop_live_auto.configure(state="disabled")
            self.log_live(logging.WARNING, "已请求停止反转策略实盘；不会新增下一单。")

    async def run_reversal_live_real(self, config):
        profile = self.reversal_profile(config.get("mode", REVERSAL_MODE_RED_UP))
        stakes, win_factor, loss_factor, target_profit = self.reversal_stakes(
            config["initial_usdc"],
            config["entry_price"],
            config["max_layers"],
            config["fee_rate"],
        )
        deadline = time.time() + config["max_hours"] * 3600
        self.log_live(logging.WARNING, "%s 实盘下注序列: %s", profile["label"], " -> ".join(f"{x:.2f}" for x in stakes))
        seen_triggers = set()
        cycle_count = 0
        heartbeat_interval_cycles = 10
        last_heartbeat_cycle = 0
        cycle_pnl_running = 0.0
        while not self.live_auto_stop_requested.is_set() and time.time() < deadline:
            if cycle_count - last_heartbeat_cycle >= heartbeat_interval_cycles and cycle_count > 0:
                remaining_hours = max(0.0, (deadline - time.time()) / 3600.0)
                await self.push_to_server_chan(
                    "Polymarket 反转实盘心跳",
                    f"### Polymarket 反转实盘心跳\n\n- 策略: `{profile['label']}`\n- 已触发周期: `{cycle_count}`\n- 累计估算 pnl: `{cycle_pnl_running:+.2f}` USDC\n- 剩余小时: `{remaining_hours:.1f}`",
                )
                last_heartbeat_cycle = cycle_count
            rows = await self.fetch_btc_15m_klines(3)
            if len(rows) < 6:
                await self.sleep_with_stop(60, self.live_auto_stop_requested)
                continue
            colors = [self.kline_color(row) for row in rows]
            i = len(rows) - 1
            trigger_found = colors[i - 2:i + 1] == [profile["trigger_color"]] * 3 and (i < 3 or colors[i - 3] != profile["trigger_color"])
            trigger_key = int(rows[i][0])
            if not trigger_found or trigger_key in seen_triggers:
                streak = self.matching_streak(colors, profile["trigger_color"])
                self.log_live(logging.INFO, "%s 实盘等待中: 当前连续%s=%s | 最新K线=%s", profile["label"], profile["trigger_name"], streak, self.fmt_kline_time(rows[-1]))
                await self.sleep_with_stop(60, self.live_auto_stop_requested)
                continue

            seen_triggers.add(trigger_key)
            cycle_count += 1
            self.log_live(logging.WARNING, "触发%s实盘 #%s: 第3根=%s", profile["label"], cycle_count, self.fmt_kline_time(rows[i]))
            accumulated_loss = 0.0
            cycle_open_positions: list[dict] = []
            for layer, stake in enumerate(stakes, start=1):
                try:
                    if self.live_auto_stop_requested.is_set() or time.time() >= deadline:
                        break
                    trade_open = trigger_key + layer * 15 * 60 * 1000
                    wait_seconds = max(0, trade_open / 1000 - time.time())
                    if wait_seconds > 0:
                        self.log_live(logging.INFO, "等待第 %s 单对应市场开盘，约 %.0f 秒", layer, wait_seconds)
                        await self.sleep_with_stop(wait_seconds, self.live_auto_stop_requested)
                    if self.live_auto_stop_requested.is_set():
                        break

                    target_slug = f"btc-updown-15m-{int(trade_open / 1000)}"
                    market = await self.fetch_market_by_slug(target_slug)
                    for _ in range(3):
                        if market or self.live_auto_stop_requested.is_set():
                            break
                        self.log_live(logging.INFO, "等待 Polymarket 创建目标市场: %s", target_slug)
                        await self.sleep_with_stop(5, self.live_auto_stop_requested)
                        market = await self.fetch_market_by_slug(target_slug)
                    if not market:
                        self.log_live(logging.WARNING, "未找到反转实盘目标市场，跳过本周期: %s", target_slug)
                        if cycle_open_positions:
                            await self._flatten_cycle_positions(cycle_open_positions, f"market_not_found:{target_slug}", cycle_count)
                        break
                    token_id = getattr(market, profile["token_attr"])
                    book = await self.best_bid_ask_for_token(token_id)
                    ask = book["ask"] if book["ask"] is not None else getattr(market, profile["ask_attr"])
                    if ask > config["entry_price"]:
                        self.log_live(logging.WARNING, "第 %s 单跳过: %s ask=%.4f 高于最高入场价 %.4f", layer, profile["direction"], ask, config["entry_price"])
                        if cycle_open_positions:
                            await self._flatten_cycle_positions(cycle_open_positions, f"ask_too_high:{ask:.4f}>{config['entry_price']:.4f}", cycle_count)
                        break

                    buy_details = await self.buy_quick_market(market, profile["direction"], stake, config["entry_price"])
                    entry = float(buy_details["price"])
                    size = float(buy_details["size"])
                    fill_verified = bool(buy_details.get("fill_verified"))
                    fill_status = buy_details.get("fill_status", "unknown")
                    cycle_open_positions.append({
                        "layer": layer,
                        "token_id": buy_details["token_id"],
                        "tick_size": str(buy_details.get("tick_size") or "0.01"),
                        "fill_size": float(buy_details["size"]),
                        "entry": float(buy_details["price"]),
                    })
                    if not fill_verified:
                        self.log_live(logging.WARNING, "第 %s 单成交价未验证（fill_status=%s），后续 PnL 使用 limit 估算", layer, fill_status)
                    row = {
                        "round": f"R{cycle_count}-{layer}",
                        "slug": market.slug,
                        "status": "OPEN_REAL",
                        "direction": profile["direction"],
                        "entry": entry,
                        "current": entry,
                        "high": entry,
                        "exit": None,
                        "pnl": -accumulated_loss,
                        "pnl_pct": 0.0,
                        "entered": True,
                        "result": f"OPEN_REAL | stake={stake:.2f}U | entry={entry:.4f} | verified={fill_verified}",
                    }
                    self.live_results.append(row)
                    self.root.after(0, self.render_live_results)
                    await self.push_to_server_chan(
                        "Polymarket 反转实盘买入",
                        f"### Polymarket 反转实盘买入\n\n- 策略: `{profile['label']}`\n- 周期: `{cycle_count}`\n- 层数: `{layer}`\n- 方向: `{profile['direction']}`\n- 市场: `{market.slug}`\n- 金额: `{stake:.2f}` USDC\n- 入场: `{entry:.4f} (verified={fill_verified})`\n- 数量: `{size:.4f}`",
                    )

                    wait_until_close = 0
                    if market.end_dt:
                        wait_until_close = max(0, market.end_dt.timestamp() - time.time() + 2)
                    if wait_until_close > 0:
                        self.log_live(logging.INFO, "等待第 %s 单结算，约 %.0f 秒", layer, wait_until_close)
                        await self.sleep_with_stop(wait_until_close)
                    # market end_dt + 5min grace as upper bound for settlement detection;
                    # fall back to 30 min from now if end_dt missing.
                    settle_deadline = (market.end_dt.timestamp() + 300) if market.end_dt else (time.time() + 1800)
                    token_id_settled = getattr(market, profile["token_attr"])
                    outcome = await self._settle_from_positions(token_id_settled, settle_deadline, self.live_auto_stop_requested)
                    if outcome == "win":
                        pnl = (1.0 - entry) * size - accumulated_loss
                        row.update({"status": "REVERSAL_REAL_WIN", "current": 1.0, "high": 1.0, "exit": 1.0, "pnl": pnl, "pnl_pct": pnl / stake * 100 if stake else 0.0})
                        self.root.after(0, self.render_live_results)
                        self.log_live(logging.WARNING, "%s 实盘第 %s 单胜 (data-api redeemable=True): pnl≈%+.2fU", profile["label"], layer, pnl)
                        await self.push_to_server_chan(
                            "Polymarket 反转实盘结果",
                            f"### Polymarket 反转实盘结果\n\n- 策略: `{profile['label']}`\n- 周期: `{cycle_count}`\n- 层数: `{layer}`\n- 方向: `{profile['direction']}`\n- 市场: `{market.slug}`\n- 结果: `WIN`\n- 入场: `{entry:.4f} (verified={fill_verified})`\n- 结算: `data-api redeemable`\n- 本行盈亏估算: `{pnl:+.2f}` USDC",
                        )
                        self._append_trade_journal({
                            "ts": datetime.now(timezone.utc).isoformat(),
                            "strategy": profile["label"], "cycle": cycle_count, "layer": layer,
                            "market_slug": market.slug, "direction": profile["direction"],
                            "stake_usdc": f"{stake:.4f}", "requested_price": f"{config['entry_price']:.4f}",
                            "fill_price": f"{entry:.4f}", "fill_size": f"{size:.6f}",
                            "fill_verified": str(fill_verified), "outcome": outcome,
                            "pnl_estimate": f"{pnl:+.4f}", "accumulated_loss": f"{accumulated_loss:.4f}",
                        })
                        # WIN path: `pnl` already subtracted accumulated_loss for this cycle, but
                        # the prior LOSS layers already added their own -loss to cycle_pnl_running.
                        # Adding `pnl` directly here would double-count earlier losses. Restore the
                        # accumulated_loss term so the running total is the sum of per-layer outcomes.
                        cycle_pnl_running += pnl + accumulated_loss
                        cycle_open_positions = [p for p in cycle_open_positions if p["layer"] != layer]
                        break
                    if outcome == "pending_timeout":
                        self.log_live(logging.ERROR, "%s 实盘第 %s 单结算异常 (deadline 超出，需人工介入)", profile["label"], layer)
                        await self.push_to_server_chan(
                            "Polymarket 反转实盘 ⚠️ 结算异常",
                            f"### ⚠️ 反转实盘结算异常\n\n- 策略: `{profile['label']}`\n- 周期: `{cycle_count}`\n- 层数: `{layer}`\n- 市场: `{market.slug}`\n- token: `{token_id_settled[:12]}...`\n- 入场: `{entry:.4f}`\n- 仓位结算状态超 deadline 未确定，请去 polymarket.com/portfolio 人工核对",
                        )
                        self._append_trade_journal({
                            "ts": datetime.now(timezone.utc).isoformat(),
                            "strategy": profile["label"], "cycle": cycle_count, "layer": layer,
                            "market_slug": market.slug, "direction": profile["direction"],
                            "stake_usdc": f"{stake:.4f}", "requested_price": f"{config['entry_price']:.4f}",
                            "fill_price": f"{entry:.4f}", "fill_size": f"{size:.6f}",
                            "fill_verified": str(fill_verified), "outcome": outcome,
                            "pnl_estimate": "0.0000", "accumulated_loss": f"{accumulated_loss:.4f}",
                        })
                        if cycle_open_positions:
                            await self._flatten_cycle_positions(cycle_open_positions, f"pending_timeout:layer{layer}", cycle_count)
                        return f"已停止 (结算异常 + 回滚)，触发周期={cycle_count}"
                    # outcome == "loss"
                    loss = entry * size
                    accumulated_loss += loss
                    pnl = -loss
                    row.update({"status": "REVERSAL_REAL_LOSS" if layer == len(stakes) else "REVERSAL_REAL_NEXT", "current": 0.0, "high": row.get("high", entry), "exit": 0.0, "pnl": pnl, "pnl_pct": pnl / stake * 100 if stake else 0.0})
                    self.root.after(0, self.render_live_results)
                    self.log_live(logging.WARNING, "%s 实盘第 %s 单负 (data-api 仓位归零): loss≈%.2fU", profile["label"], layer, loss)
                    await self.push_to_server_chan(
                        "Polymarket 反转实盘结果",
                        f"### Polymarket 反转实盘结果\n\n- 策略: `{profile['label']}`\n- 周期: `{cycle_count}`\n- 层数: `{layer}`\n- 方向: `{profile['direction']}`\n- 市场: `{market.slug}`\n- 结果: `LOSS`\n- 入场: `{entry:.4f} (verified={fill_verified})`\n- 结算: `data-api 仓位归零`\n- 本行盈亏估算: `{pnl:+.2f}` USDC",
                    )
                    self._append_trade_journal({
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "strategy": profile["label"], "cycle": cycle_count, "layer": layer,
                        "market_slug": market.slug, "direction": profile["direction"],
                        "stake_usdc": f"{stake:.4f}", "requested_price": f"{config['entry_price']:.4f}",
                        "fill_price": f"{entry:.4f}", "fill_size": f"{size:.6f}",
                        "fill_verified": str(fill_verified), "outcome": outcome,
                        "pnl_estimate": f"{pnl:+.4f}", "accumulated_loss": f"{accumulated_loss:.4f}",
                    })
                    cycle_pnl_running += pnl
                    cycle_open_positions = [p for p in cycle_open_positions if p["layer"] != layer]
                except Exception as exc:
                    self.log_live(logging.ERROR, "反转实盘 layer %s 异常中断: %s", layer, exc)
                    # Phase 2 retry-post timeout (RuntimeError) can raise AFTER
                    # the exchange actually accepted the order. cycle_open_positions
                    # only has confirmed layers — probe data-api for THIS layer's
                    # token to detect on-chain orphan and include it in flatten.
                    pending_token_id = locals().get("token_id")
                    if pending_token_id and not any(p.get("layer") == layer for p in cycle_open_positions):
                        try:
                            probe_positions = await self._fetch_positions_raw()
                            probe_match = next((p for p in probe_positions if p.get("asset") == pending_token_id), None)
                            if probe_match and self._float_or_zero(probe_match.get("size")) > 0.000001:
                                pending_market = locals().get("market")
                                pending_tick = str(getattr(pending_market, "tick_size", None) or "0.01") if pending_market else "0.01"
                                cycle_open_positions.append({
                                    "layer": layer,
                                    "token_id": pending_token_id,
                                    "tick_size": pending_tick,
                                    "fill_size": self._float_or_zero(probe_match.get("size")),
                                    "entry": self._float_or_zero(probe_match.get("avgPrice")),
                                })
                                self.log_live(logging.WARNING, "layer %s 异常时仓位仍在 size=%.4f，加入回滚", layer, self._float_or_zero(probe_match.get("size")))
                        except Exception as probe_exc:
                            self.log_live(logging.ERROR, "layer %s 异常后查仓位失败: %s — flatten 可能不完整", layer, probe_exc)
                    if cycle_open_positions:
                        await self._flatten_cycle_positions(cycle_open_positions, f"layer_exception:{type(exc).__name__}:{str(exc)[:120]}", cycle_count)
                    raise
                if self.live_auto_stop_requested.is_set():
                    break
            await self.sleep_with_stop(60, self.live_auto_stop_requested)
        return f"已停止，触发周期={cycle_count}"

    def render_paper_results(self):
        for item in self.paper_tree.get_children():
            self.paper_tree.delete(item)
        for index, row in enumerate(self.paper_results):
            entry = row.get("entry")
            current = row.get("current")
            high = row.get("high")
            exit_price = row.get("exit")
            values = (
                row.get("round", index + 1),
                row.get("status", ""),
                row.get("direction", ""),
                "--" if entry is None else f"{float(entry):.4f}",
                "--" if current is None else f"{float(current):.4f}",
                "--" if high is None else f"{float(high):.4f}",
                "--" if exit_price is None else f"{float(exit_price):.4f}",
                f"{float(row.get('pnl', 0.0)):+.2f}",
                f"{float(row.get('pnl_pct', 0.0)):+.2f}%",
                row.get("slug", ""),
            )
            self.paper_tree.insert("", "end", iid=str(index), values=values)

    def render_live_results(self):
        for item in self.live_tree.get_children():
            self.live_tree.delete(item)
        for index, row in enumerate(self.live_results):
            entry = row.get("entry")
            current = row.get("current")
            high = row.get("high")
            exit_price = row.get("exit")
            values = (
                row.get("round", index + 1),
                row.get("status", ""),
                row.get("direction", ""),
                "--" if entry is None else f"{float(entry):.4f}",
                "--" if current is None else f"{float(current):.4f}",
                "--" if high is None else f"{float(high):.4f}",
                "--" if exit_price is None else f"{float(exit_price):.4f}",
                f"{float(row.get('pnl', 0.0)):+.2f}",
                f"{float(row.get('pnl_pct', 0.0)):+.2f}%",
                row.get("slug", ""),
            )
            self.live_tree.insert("", "end", iid=str(index), values=values)

    async def sleep_with_stop(self, seconds, stop_event=None):
        end = time.time() + max(0, seconds)
        while time.time() < end:
            if stop_event is not None and stop_event.is_set():
                return
            await asyncio.sleep(min(1.0, end - time.time()))

    async def fetch_next_15m_market(self):
        now_ts = int(time.time())
        base = now_ts - (now_ts % 900)
        candidates = [base + 900, base + 1800, base]
        for start_ts in candidates:
            if start_ts < now_ts - 60:
                continue
            market = await self.fetch_market_by_slug(f"btc-updown-15m-{start_ts}")
            if market and not market.ended:
                return market
        return None

    async def fetch_market_by_slug(self, slug: str):
        event = await self.fetch_json(f"{GAMMA_EVENT_SLUG_URL}/{slug}")
        if not isinstance(event, dict):
            return None
        now = datetime.now(timezone.utc)
        for market in event.get("markets") or []:
            item = self.quick_market_candidate(event, market, now)
            if item:
                return item
        return None

    def market_start_timestamp(self, market: QuickMarket):
        match = re.search(r"btc-updown-\d+m-(\d+)", market.slug)
        if not match:
            return None
        return int(match.group(1))

    async def fetch_latest_btc_price(self):
        url = "https://api.binance.com/api/v3/ticker/price"
        params = {"symbol": "BTCUSDT"}
        try:
            session = _get_aiohttp_session()
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status != 200:
                    raise RuntimeError(f"Binance ticker HTTP {response.status}")
                data = await response.json()
                return float(data["price"])
        except Exception:
            url = "https://data-api.binance.vision/api/v3/ticker/price"
            session = _get_aiohttp_session()
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status != 200:
                    raise RuntimeError(f"Binance vision ticker HTTP {response.status}")
                data = await response.json()
                return float(data["price"])

    def buy_selected_quick_market(self, direction: str):
        market = self.selected_quick_market()
        if not market:
            return
        try:
            usdc_amount = float(self.ent_quick_usdc.get().strip())
            max_price = float(self.ent_quick_max_price.get().strip())
        except ValueError:
            messagebox.showerror("参数错误", "买入金额和最高价必须是数字。")
            return
        if usdc_amount <= 0 or max_price <= 0 or max_price >= 1:
            messagebox.showerror("参数错误", "买入金额必须大于 0，最高价必须在 0 到 1 之间。")
            return
        if market.ended:
            messagebox.showerror("市场已结束", "选中的短周期市场已经结束，请重新扫描。")
            return
        if not messagebox.askyesno(
            "确认快速买入",
            f"市场: {market.question}\n方向: {direction}\n金额: {usdc_amount:.2f} USDC\n最高可接受价格: {max_price:.4f}\n\n这是真实交易操作，可能立即成交。确认继续？",
        ):
            return

        self.btn_buy_up.configure(state="disabled")
        self.btn_buy_down.configure(state="disabled")
        self.logger.info("开始快速买入: %s | %s | %.2f USDC | max_price=%.4f", market.slug, direction, usdc_amount, max_price)

        def worker():
            loop = asyncio.new_event_loop()
            try:
                resp = loop.run_until_complete(self.buy_quick_market(market, direction, usdc_amount, max_price))
                self.root.after(0, lambda: self.logger.info("快速买入提交结果: %s", resp))
            except Exception as e:
                error_text = str(e) or repr(e)
                self.root.after(0, lambda err=error_text: self.logger.error("快速买入失败: %s", err))
            finally:
                loop.close()
                self.root.after(0, lambda: self.btn_buy_up.configure(state="normal"))
                self.root.after(0, lambda: self.btn_buy_down.configure(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

    async def buy_quick_market(self, market: QuickMarket, direction: str, usdc_amount: float, max_price: float):
        config = self.validate_credentials_config()
        creds = ApiCreds(config["api_key"], config["secret"], config["passphrase"]) if config["api_key"] else await self.derive_api_creds()
        if creds is None:
            raise RuntimeError("无法派生 CLOB API 凭证")
        client = self.build_client(config, creds)
        token_id = market.yes_id if direction == "UP" else market.no_id
        self.logger.info("读取订单簿: %s token=%s", direction, token_id[:12])
        ask_price, tick_size = await self.best_ask_for_token(client, token_id)
        if ask_price is None:
            raise RuntimeError("订单簿没有可买卖价")
        if ask_price > max_price:
            raise RuntimeError(f"盘口卖价 {ask_price:.4f} 高于最高价 {max_price:.4f}，已拒绝下单")
        price = self.clamp_price(ask_price, tick_size or market.tick_size)
        size = usdc_amount / price
        if size < 5.0:
            raise RuntimeError(f"买入金额太小，按价格 {price:.4f} 至少需要 {price * 5:.2f} USDC 才满足 5 份最小下单量")
        self.logger.info("提交买入订单: %s price=%.4f size=%.4f tick=%s", direction, price, size, tick_size or market.tick_size)
        signed = await asyncio.to_thread(
            client.create_order,
            order_args=OrderArgs(token_id=token_id, price=float(price), size=float(size), side=Side.BUY),
            options=PartialCreateOrderOptions(tick_size=tick_size or market.tick_size),
        )
        resp = await self._post_signed_order_with_retry(client, signed, order_type=OrderType.GTC, post_only=False)
        self._assert_order_response_ok(resp, action_name="快速买入")
        fill = self._extract_fill(resp, limit_price=price, limit_size=size)
        if fill["verified"]:
            self.logger.info("成交确认: limit=%.4f×%.4f → fill=%.4f×%.4f status=%s", price, size, fill["fill_price"], fill["fill_size"], fill["status"])
        else:
            self.logger.warning("未拿到真实成交数据，仍用 limit 估算: status=%s resp_keys=%s", fill["status"], list(resp.keys()) if isinstance(resp, dict) else type(resp).__name__)
        await self.push_trade_result("快速买入", market.question, direction, fill["fill_size"], fill["fill_price"], resp, market_slug=market.slug)
        return {"response": resp, "token_id": token_id, "price": fill["fill_price"], "size": fill["fill_size"], "tick_size": tick_size or market.tick_size, "fill_status": fill["status"], "fill_verified": fill["verified"]}

    async def best_ask_for_token(self, client, token_id: str):
        orderbook = await asyncio.wait_for(asyncio.to_thread(client.get_order_book, token_id), timeout=15)
        raw_asks = orderbook.get("asks", []) if isinstance(orderbook, dict) else getattr(orderbook, "asks", None) or []
        tick_size = str((orderbook.get("tick_size") if isinstance(orderbook, dict) else getattr(orderbook, "tick_size", None)) or "0.01")
        asks = [float(self._book_level_value(level, "price")) for level in raw_asks if self._book_level_value(level, "price") is not None]
        if not asks:
            return None, tick_size
        best_ask = min(asks)
        self.logger.info("订单簿 best_ask=%.4f tick=%s", best_ask, tick_size)
        return best_ask, tick_size

    async def best_bid_ask_for_token(self, token_id: str):
        try:
            client = ClobClient(host=CLOB_HOST, chain_id=CHAIN_ID, retry_on_error=True)
            orderbook = await asyncio.wait_for(asyncio.to_thread(client.get_order_book, token_id), timeout=8)
            raw_bids = orderbook.get("bids", []) if isinstance(orderbook, dict) else getattr(orderbook, "bids", None) or []
            raw_asks = orderbook.get("asks", []) if isinstance(orderbook, dict) else getattr(orderbook, "asks", None) or []
            tick_size = str((orderbook.get("tick_size") if isinstance(orderbook, dict) else getattr(orderbook, "tick_size", None)) or "0.01")
            bids = [float(self._book_level_value(level, "price")) for level in raw_bids if self._book_level_value(level, "price") is not None]
            asks = [float(self._book_level_value(level, "price")) for level in raw_asks if self._book_level_value(level, "price") is not None]
            return {
                "bid": max(bids) if bids else None,
                "ask": min(asks) if asks else None,
                "tick_size": tick_size,
            }
        except Exception as e:
            self.logger.warning("读取 CLOB 订单簿失败 token=%s: %s", token_id[:12], e)
            return {"bid": None, "ask": None, "tick_size": "0.01"}

    async def sell_token_limit(self, token_id: str, size: float, price: float, tick_size: str):
        config = self.validate_credentials_config()
        creds = ApiCreds(config["api_key"], config["secret"], config["passphrase"]) if config["api_key"] else await self.derive_api_creds()
        if creds is None:
            raise RuntimeError("无法派生 CLOB API 凭证")
        client = self.build_client(config, creds)
        price = self.clamp_price(price, tick_size)
        self.logger.warning("提交实盘自动卖出订单: token=%s price=%.4f size=%.4f tick=%s", token_id[:12], price, size, tick_size)
        signed = await asyncio.to_thread(
            client.create_order,
            order_args=OrderArgs(token_id=token_id, price=float(price), size=float(size), side=Side.SELL),
            options=PartialCreateOrderOptions(tick_size=tick_size),
        )
        resp = await self._post_signed_order_with_retry(client, signed, order_type=OrderType.GTC, post_only=False)
        self._assert_order_response_ok(resp, action_name="限价卖出")
        return resp

    async def fetch_positions(self):
        user = self.ent_funder.get().strip()
        if not user:
            return []
        params = {
            "user": user,
            "limit": "50",
            "sizeThreshold": "0",
            "sortBy": "CURRENT",
            "sortDirection": "DESC",
        }
        try:
            session = _get_aiohttp_session()
            async with session.get("https://data-api.polymarket.com/positions", params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=aiohttp.ClientTimeout(total=12)) as response:
                if response.status != 200:
                    self.logger.warning("持仓接口返回 HTTP %s", response.status)
                    return []
                data = await response.json()
        except Exception as e:
            self.logger.warning("读取持仓失败: %s", e)
            return []
        return data if isinstance(data, list) else []

    async def _fetch_positions_value(self):
        """Return total value of user's positions (USDC) via data-api /value.

        Returns None on failure (caller preserves last known value).
        Returns 0.0 when the user has no positions (empty array response).
        """
        user = self.ent_funder.get().strip()
        if not user:
            return None
        try:
            session = _get_aiohttp_session()
            async with session.get(
                "https://data-api.polymarket.com/value",
                params={"user": user},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status != 200:
                    self.logger.warning("positions value HTTP %s", response.status)
                    return None
                data = await response.json()
        except Exception as e:
            self.logger.warning("positions value 读取失败: %s", e)
            return None
        if not isinstance(data, list) or not data:
            return 0.0
        try:
            return float(data[0].get("value", 0.0) or 0.0)
        except (TypeError, ValueError):
            return None

    POLYGON_USDC_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    POLYGON_RPC_URL = "https://polygon-rpc.com"

    async def _fetch_usdc_balance_onchain(self):
        """Return funder address's free USDC balance on Polygon via JSON-RPC.

        Uses ERC20 balanceOf selector 0x70a08231. USDC has 6 decimals.
        Returns None on failure (caller preserves last known value).
        """
        user = self.ent_funder.get().strip()
        if not user or not user.startswith("0x") or len(user) != 42:
            return None
        addr_clean = user[2:].lower()
        data_field = "0x70a08231" + "0" * 24 + addr_clean
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [
                {"to": self.POLYGON_USDC_CONTRACT, "data": data_field},
                "latest",
            ],
            "id": 1,
        }
        try:
            session = _get_aiohttp_session()
            async with session.post(
                self.POLYGON_RPC_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status != 200:
                    self.logger.warning("USDC balance RPC HTTP %s", response.status)
                    return None
                resp_json = await response.json()
        except Exception as e:
            self.logger.warning("USDC balance RPC 读取失败: %s", e)
            return None
        result_hex = resp_json.get("result")
        if not isinstance(result_hex, str) or not result_hex.startswith("0x"):
            self.logger.warning("USDC balance RPC 响应缺 result: %s", resp_json)
            return None
        try:
            return int(result_hex, 16) / 1_000_000.0
        except ValueError:
            return None

    async def _fetch_positions_raw(self):
        """
        Like fetch_positions() but raises on HTTP/transport error instead
        of silently returning []. Settlement-critical callers MUST use this
        so a network failure is never confused with "user has no positions".
        """
        user = self.ent_funder.get().strip()
        if not user:
            return []
        params = {
            "user": user,
            "limit": "50",
            "sizeThreshold": "0",
            "sortBy": "CURRENT",
            "sortDirection": "DESC",
        }
        session = _get_aiohttp_session()
        async with session.get("https://data-api.polymarket.com/positions", params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=aiohttp.ClientTimeout(total=12)) as response:
            if response.status != 200:
                raise RuntimeError(f"positions HTTP {response.status}")
            data = await response.json()
        return data if isinstance(data, list) else []

    async def _settle_from_positions(self, token_id: str, deadline_ts: float, stop_event=None):
        """
        Poll data-api /positions to determine WIN/LOSS for a token_id.
        Returns: 'win' | 'loss' | 'pending_timeout'.

        Per data-api OpenAPI (Position.redeemable):
          - asset == token_id + size > 0 + redeemable == True  → WIN
          - asset == token_id + size > 0 + redeemable == False → still settling, retry
          - asset not in positions list                         → LOSS (loser side burned)

        Uses _fetch_positions_raw so a transient HTTP/network failure is
        retried (does NOT advance the absent-state machine that would
        otherwise misfire as LOSS — protects against double-failure
        misclassification feeding bad data to martingale.)

        Backoff: exponential 5s → 60s cap. Aborts if stop_event set or time > deadline_ts.
        """
        backoff = 5.0
        last_state = None
        while time.time() < deadline_ts:
            if stop_event is not None and stop_event.is_set():
                return "pending_timeout"
            try:
                positions = await self._fetch_positions_raw()
            except Exception as e:
                self.logger.warning("settle 阶段 positions fetch 失败 (重试): %s", e)
                # V8 final-review patch: reset absent-streak so a transient
                # HTTP failure between two successful absent polls cannot
                # collapse into a false LOSS. We require two CONSECUTIVE
                # successful absent polls, not just two-total.
                last_state = None
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 60.0)
                continue
            match = next((p for p in positions if p.get("asset") == token_id), None)
            if match is None:
                # Position not present → loser side burned by resolution.
                # Guard against transient empty-list (fetch failure) by requiring
                # this state to persist one cycle.
                if last_state == "absent":
                    return "loss"
                last_state = "absent"
            else:
                size = self._float_or_zero(match.get("size"))
                if size <= 0.000001:
                    if last_state == "absent":
                        return "loss"
                    last_state = "absent"
                elif match.get("redeemable") is True:
                    return "win"
                else:
                    last_state = "pending"
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.5, 60.0)
        return "pending_timeout"

    async def _flatten_cycle_positions(self, open_positions: list[dict], reason: str, cycle_count: int):
        """
        Best-effort flatten of every position opened in the current reversal cycle
        that has not yet resolved. Used when the layer loop aborts mid-cycle so
        the user is not left holding wrong-direction bags.

        For each open position: read best_bid_ask_for_token; pick a marketable
        sell price (0.95 * best_bid for slippage tolerance), floored at tick_size
        to avoid clamp-to-zero on thin books; submit sell_token_limit. Log each
        step. Push one Server酱 critical summary at the end.

        Known limitation: sell_token_limit still uses Phase 2's retry-post path,
        so a timeout-then-double race is still possible on the rollback sells
        themselves. The Server酱 push tells the user to reconcile manually at
        polymarket.com/portfolio as the last line of defense.
        """
        results = []
        for pos in open_positions:
            token_id = pos["token_id"]
            try:
                book = await self.best_bid_ask_for_token(token_id)
                bid = book.get("bid")
                tick_size = book.get("tick_size") or pos["tick_size"]
                tick_value = float(tick_size) if tick_size else 0.01
                if bid is None or bid <= 0:
                    sell_price = self.clamp_price(tick_value, tick_size)
                    self.log_live(logging.WARNING, "rollback layer %s 无 bid，使用最低价 %.4f", pos["layer"], sell_price)
                else:
                    raw_price = max(bid * 0.95, tick_value)
                    sell_price = self.clamp_price(raw_price, tick_size)
                resp = await self.sell_token_limit(token_id, pos["fill_size"], sell_price, tick_size)
                self.log_live(logging.WARNING, "rollback layer %s 卖出: size=%.4f price=%.4f resp=%s", pos["layer"], pos["fill_size"], sell_price, str(resp)[:120])
                results.append({"layer": pos["layer"], "ok": True, "price": sell_price, "size": pos["fill_size"]})
            except Exception as e:
                self.log_live(logging.ERROR, "rollback layer %s 卖出失败: %s", pos["layer"], e)
                results.append({"layer": pos["layer"], "ok": False, "error": str(e)[:200], "token_id": token_id[:12]})
        try:
            lines = []
            for r in results:
                if r["ok"]:
                    lines.append(f"- layer {r['layer']}: OK {r['price']:.4f} x {r['size']:.4f}")
                else:
                    lines.append(f"- layer {r['layer']}: FAIL {r.get('error', '')}")
            await self.push_to_server_chan(
                "Polymarket 反转实盘 ⚠️ 周期回滚",
                f"### ⚠️ 反转实盘周期中断回滚\n\n- 周期: `{cycle_count}`\n- 原因: `{reason}`\n- 回滚结果:\n" + "\n".join(lines) + "\n\n请去 polymarket.com/portfolio 人工对账。",
            )
        except Exception as e:
            self.logger.error("rollback Server酱 推送失败: %s", e)
        return results

    async def _post_signed_order_with_retry(
        self,
        client,
        signed_order,
        order_type=OrderType.GTC,
        post_only: bool = False,
        max_attempts: int = 2,
        per_attempt_timeout: float = 25.0,
    ):
        """
        Post a pre-signed CLOB order with retry-safe semantics.

        Per Phase 2 research (cycles/_phase2-research.md): SignedOrderV2 is
        byte-level frozen (salt + timestamp + EIP-712 signature all set in
        create_order). Re-posting the same instance produces byte-identical
        request bodies → identical orderID (order hash) → server dedup.
        py_clob_client_v2's own retry_on_error path uses this assumption.

        On per-attempt TimeoutError we retry the SAME signed_order. If all
        attempts time out, raise a RuntimeError indicating user should
        reconcile at polymarket.com/portfolio (the order may have landed).

        WARNING: server-side dedup is inferred, not OpenAPI-documented.
        Phase 2 commit MUST be followed by a live spike (see handoff).
        """
        last_exc = None
        for attempt in range(1, max_attempts + 1):
            try:
                resp = await asyncio.wait_for(
                    asyncio.to_thread(client.post_order, signed_order, order_type, post_only),
                    timeout=per_attempt_timeout,
                )
                if attempt > 1:
                    self.logger.info("post_order 重试 attempt=%s 成功", attempt)
                return resp
            except asyncio.TimeoutError as e:
                last_exc = e
                self.logger.warning(
                    "post_order 超时 attempt=%s/%s — 用同一 signed_order 重试 (server 应按 orderID 去重)",
                    attempt, max_attempts,
                )
                continue
        raise RuntimeError(
            f"post_order 重试 {max_attempts} 次仍超时；订单可能已成交，请去 polymarket.com/portfolio 对账"
        ) from last_exc

    @staticmethod
    def _append_trade_journal(row: dict, path: str = "trade_journal.csv"):
        """
        Append a single trade settlement row to CSV at `path`.
        Creates the file with header on first write. Best-effort: caller's
        flow must not depend on this succeeding (logs warning on error).

        Required row keys:
          ts, strategy, cycle, layer, market_slug, direction,
          stake_usdc, requested_price, fill_price, fill_size,
          fill_verified, outcome, pnl_estimate, accumulated_loss
        """
        import csv
        fieldnames = [
            "ts", "strategy", "cycle", "layer", "market_slug", "direction",
            "stake_usdc", "requested_price", "fill_price", "fill_size",
            "fill_verified", "outcome", "pnl_estimate", "accumulated_loss",
        ]
        try:
            need_header = not os.path.exists(path) or os.path.getsize(path) == 0
            with open(path, "a", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                if need_header:
                    writer.writeheader()
                writer.writerow({k: row.get(k, "") for k in fieldnames})
        except OSError as e:
            logging.getLogger("PolyQuickTrader").warning("trade journal append 失败: %s", e)

    @staticmethod
    def _aggregate_daily_journal(rows: list[dict], target_date_utc: str) -> dict:
        """Pure aggregator over trade_journal rows (csv.DictReader-parsed).
        target_date_utc: 'YYYY-MM-DD'. Counts cycles via (strategy,cycle) tuple."""
        same_day = [r for r in rows if r.get("ts", "")[:10] == target_date_utc]
        cycles = {(r["strategy"], r["cycle"]) for r in same_day}
        wins = [r for r in same_day if r.get("outcome") == "win"]
        losses = [r for r in same_day if r.get("outcome") == "loss"]
        timeouts = [r for r in same_day if r.get("outcome") == "pending_timeout"]
        unverified = [r for r in same_day if r.get("fill_verified") == "False"]
        pnl_sum = 0.0
        # Per-cycle aggregation per Final Codex P2 BLOCKER: trade_journal
        # writes one row per layer. The WIN layer's pnl_estimate already
        # subtracted accumulated_loss (Phase 9 invariant), so simply
        # summing every row across a lose-then-win cycle counted prior
        # losses twice. Aggregate per (strategy, cycle): if any row is WIN
        # its pnl IS the net; otherwise sum all rows (LOSS-only / timeout-
        # only cycles have no double-count).
        cycles_for_pnl: dict[tuple, list[dict]] = {}
        for r in same_day:
            cycles_for_pnl.setdefault((r["strategy"], r["cycle"]), []).append(r)
        for cycle_rows in cycles_for_pnl.values():
            win_row = next((r for r in cycle_rows if r.get("outcome") == "win"), None)
            if win_row is not None:
                contributing_rows = [win_row]
            else:
                contributing_rows = cycle_rows
            for r in contributing_rows:
                try:
                    v = float(r.get("pnl_estimate", "0") or "0")
                except (TypeError, ValueError):
                    continue
                # V3 guard (Phase 11 Codex warn): NaN/Inf in pnl_estimate would
                # poison the entire daily P&L sum (NaN + anything == NaN).
                if v != v or v in (float("inf"), float("-inf")):
                    continue
                pnl_sum += v
        # Max consecutive loss layers within any single (strategy,cycle).
        max_consec_loss = 0
        per_cycle: dict[tuple, list[dict]] = {}
        for r in same_day:
            per_cycle.setdefault((r["strategy"], r["cycle"]), []).append(r)
        for rs in per_cycle.values():
            try:
                rs_sorted = sorted(rs, key=lambda x: int(x.get("layer", 0)))
            except (TypeError, ValueError):
                rs_sorted = rs
            run = best = 0
            for r in rs_sorted:
                if r.get("outcome") == "loss":
                    run += 1
                    best = max(best, run)
                else:
                    run = 0
            max_consec_loss = max(max_consec_loss, best)
        return {
            "date": target_date_utc,
            "total_rows": len(same_day),
            "cycle_count": len(cycles),
            "win_count": len(wins),
            "loss_count": len(losses),
            "pending_timeout_count": len(timeouts),
            "unverified_fill_count": len(unverified),
            "pnl_estimate_sum": pnl_sum,
            "max_consecutive_loss_layers": max_consec_loss,
            "anomaly_count": len(timeouts) + len(unverified),
        }

    @staticmethod
    def _render_daily_report_md(stats: dict) -> str:
        lines = [
            f"# Polymarket BTC 反转实盘日报 — {stats['date']} (UTC)",
            "",
            f"- 触发周期数: **{stats['cycle_count']}**",
            f"- 总成交行数: {stats['total_rows']}",
            f"- WIN: {stats['win_count']}",
            f"- LOSS: {stats['loss_count']}",
            f"- 估算 P&L 累计: **{stats['pnl_estimate_sum']:+.4f} USDC** "
            "（基于 trade_journal.csv 的 pnl_estimate，仍是 model 估算）",
            f"- 最大单 cycle 连亏层数: {stats['max_consecutive_loss_layers']}",
            "",
            "## 异常",
            f"- pending_timeout: {stats['pending_timeout_count']}",
            f"- fill_verified=False: {stats['unverified_fill_count']}",
            f"- 异常总计: **{stats['anomaly_count']}**",
            "",
            "链上真实余额变化以 polymarket.com/portfolio 为准。",
        ]
        return "\n".join(lines) + "\n"

    def _daily_report_tick(self):
        """Tk root.after callback. Checks whether a new UTC day has rolled
        over since the last report; if so, spawns a worker thread to
        generate + push report for yesterday. Re-schedules itself."""
        try:
            # V3 guard (Phase 11 Codex warn): if a previous worker is still
            # running (slow journal read / Server酱 push), the 60s tick must
            # NOT spawn a second concurrent worker — that would duplicate the
            # report + race the marker write.
            if getattr(self, "_daily_report_running", False):
                return
            today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            last_path = os.path.join("reports", ".daily_report_last.txt")
            last_reported = ""
            if os.path.exists(last_path):
                try:
                    with open(last_path, "r", encoding="utf-8") as f:
                        last_reported = f.read().strip()
                except OSError:
                    pass
            # Report for "yesterday" (everything earlier than today_utc).
            yesterday = (datetime.now(timezone.utc).date() -
                         __import__("datetime").timedelta(days=1)).strftime("%Y-%m-%d")
            if last_reported != yesterday:
                self._daily_report_running = True
                def worker(day=yesterday, last=last_path):
                    try:
                        loop = asyncio.new_event_loop()
                        try:
                            loop.run_until_complete(self._generate_and_push_daily_report(day, last))
                        finally:
                            loop.close()
                    except Exception as e:
                        self.logger.warning("日报生成异常: %s", e)
                    finally:
                        self._daily_report_running = False
                threading.Thread(target=worker, daemon=True).start()
        finally:
            self.root.after(60_000, self._daily_report_tick)

    def _periodic_account_refresh_tick(self):
        """Tk root.after callback. Spawns worker thread that fetches
        positions + positions value + free USDC balance, updates
        lbl_account_status, then re-schedules itself."""
        try:
            if getattr(self, "_account_refresh_running", False):
                return
            self._account_refresh_running = True

            def worker():
                try:
                    loop = asyncio.new_event_loop()
                    try:
                        positions, pos_value, balance = loop.run_until_complete(
                            self._gather_account_refresh()
                        )
                    finally:
                        loop.close()
                    self.root.after(0, lambda: self._apply_account_refresh(
                        positions, pos_value, balance
                    ))
                except Exception as e:
                    self.logger.warning("account refresh worker 异常: %s", e)
                    self.account_last_error = str(e)[:120]
                finally:
                    self._account_refresh_running = False

            threading.Thread(target=worker, daemon=True).start()
        finally:
            self.root.after(60_000, self._periodic_account_refresh_tick)

    async def _gather_account_refresh(self):
        positions_task = asyncio.create_task(self.fetch_positions())
        value_task = asyncio.create_task(self._fetch_positions_value())
        balance_task = asyncio.create_task(self._fetch_usdc_balance_onchain())
        positions = await positions_task
        pos_value = await value_task
        balance = await balance_task
        return positions, pos_value, balance

    def _apply_account_refresh(self, positions, pos_value, balance):
        now = time.time()
        # Phase 10 Codex blocker: only update refresh_ts when at least one
        # explicit data source succeeded. fetch_positions() returns []
        # ambiguously (could be HTTP failure or genuinely-no-positions), so
        # use pos_value/balance None-vs-not-None as the unambiguous signal.
        # If both are None, the network is bad — keep prior refresh_ts so
        # _render_account_status_label produces the stale (orange) marker.
        any_success = (pos_value is not None) or (balance is not None)
        if positions is not None and any_success:
            # Only trust the positions list if at least one other source
            # also returned cleanly — else we may have an empty list from
            # a silent HTTP failure inside fetch_positions().
            self.latest_positions = positions
            self.account_last_positions_count = sum(
                1 for p in positions if self._float_or_zero(p.get("size")) > 0.000001
            )
            self.render_positions(positions)
        if pos_value is not None:
            self.account_last_positions_value = pos_value
        if balance is not None:
            self.account_last_balance_usdc = balance
        if any_success:
            self.account_last_refresh_ts = now
        # else: keep prior refresh_ts so the age computation in
        # _render_account_status_label produces the stale orange marker.
        self._render_account_status_label(now)

    def _render_account_status_label(self, now=None):
        if now is None:
            now = time.time()
        age = max(0.0, now - self.account_last_refresh_ts) if self.account_last_refresh_ts else None
        b = self.account_last_balance_usdc
        v = self.account_last_positions_value
        n = self.account_last_positions_count
        b_str = f"${b:.2f}" if b is not None else "--"
        v_str = f"${v:.2f}" if v is not None else "--"
        if age is None:
            text = f"余额: {b_str} | 持仓市值: {v_str} | 持仓: {n} 条 | 加载中..."
            color = "#475569"
        else:
            text = f"余额: {b_str} | 持仓市值: {v_str} | 持仓: {n} 条 | 刷新: {int(age)}s 前"
            color = "#475569" if age < 90 else "#d97706"
        self.lbl_account_status.configure(text=text, foreground=color)

    async def _generate_and_push_daily_report(self, target_date_utc: str, last_path: str):
        import csv as _csv
        path = "trade_journal.csv"
        if not os.path.exists(path):
            self.logger.info("daily report skip: trade_journal.csv 不存在")
            return
        rows: list[dict] = []
        try:
            with open(path, "r", encoding="utf-8", newline="") as f:
                rows = list(_csv.DictReader(f))
        except OSError as e:
            self.logger.warning("daily report 读取 journal 失败: %s", e)
            return
        stats = PolyQuickTrader._aggregate_daily_journal(rows, target_date_utc)
        if stats["total_rows"] == 0:
            self.logger.info("daily report skip: %s 当日无交易", target_date_utc)
            # Still mark as reported so we don't retry every minute.
            self._mark_daily_reported(target_date_utc, last_path)
            return
        body = PolyQuickTrader._render_daily_report_md(stats)
        os.makedirs("reports", exist_ok=True)
        out_path = os.path.join("reports", f"daily_report_{target_date_utc}.md")
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(body)
        except OSError as e:
            self.logger.warning("daily report 写文件失败: %s", e)
        await self.push_to_server_chan(
            f"Polymarket 日报 {target_date_utc}",
            body,
        )
        self._mark_daily_reported(target_date_utc, last_path)

    @staticmethod
    def _mark_daily_reported(target_date_utc: str, last_path: str):
        try:
            os.makedirs(os.path.dirname(last_path) or ".", exist_ok=True)
            with open(last_path, "w", encoding="utf-8") as f:
                f.write(target_date_utc)
        except OSError:
            pass

    def refresh_positions_button_clicked(self):
        def worker():
            loop = asyncio.new_event_loop()
            try:
                positions = loop.run_until_complete(self.fetch_positions())
                self.latest_positions = positions
                self.root.after(0, lambda: self.render_positions(positions))
                self.root.after(0, lambda: self.logger.info("已刷新持仓: %s 条", len(positions)))
            finally:
                loop.close()

        threading.Thread(target=worker, daemon=True).start()

    def render_positions(self, positions):
        for item in self.positions_tree.get_children():
            self.positions_tree.delete(item)
        visible_index = 0
        for index, p in enumerate(positions):
            if self._float_or_zero(p.get("size")) <= 0.000001:
                continue
            values = (
                p.get("outcome", ""),
                f"{self._float_or_zero(p.get('size')):.2f}",
                f"{self._float_or_zero(p.get('avgPrice')):.4f}",
                f"{self._float_or_zero(p.get('curPrice')):.4f}",
                f"{self._float_or_zero(p.get('currentValue')):.2f}",
                f"{self._float_or_zero(p.get('cashPnl')):.2f}",
                f"{self._float_or_zero(p.get('percentPnl')):.2f}%",
                str(p.get("title", ""))[:100],
            )
            self.positions_tree.insert("", "end", iid=str(index), values=values)
            visible_index += 1
        if visible_index == 0:
            self.logger.info("当前没有可显示持仓。")

    def selected_position(self):
        selected = self.positions_tree.selection()
        if not selected:
            messagebox.showinfo("提示", "请先在持仓表里选中一行。")
            return None
        idx = int(selected[0])
        if idx >= len(self.latest_positions):
            messagebox.showinfo("提示", "选中的持仓已过期，请先刷新持仓。")
            return None
        return self.latest_positions[idx]

    def open_selected_position_market(self):
        position = self.selected_position()
        if not position:
            return
        slug = position.get("slug") or position.get("eventSlug")
        if slug:
            webbrowser.open(f"https://polymarket.com/event/{slug}")

    def sell_selected_position_limit(self):
        position = self.selected_position()
        if not position:
            return
        size = self._float_or_zero(position.get("size"))
        price = self._float_or_zero(position.get("curPrice"))
        if size <= 0 or price <= 0:
            messagebox.showerror("无法卖出", "选中持仓缺少有效数量或现价。")
            return
        text = (
            f"将提交 SELL 限价单：\n\n"
            f"市场: {position.get('title', '')}\n"
            f"方向: {position.get('outcome', '')}\n"
            f"数量: {size:.2f}\n"
            f"限价: {price:.4f}\n\n"
            "这是真实交易操作，可能立即成交。确认继续？"
        )
        if not messagebox.askyesno("确认限价卖出", text):
            return

        def worker():
            loop = asyncio.new_event_loop()
            try:
                resp = loop.run_until_complete(self.sell_position_limit(position, size, price))
                self.root.after(0, lambda: self.logger.info("卖出限价单提交结果: %s", resp))
            except Exception as e:
                error_text = str(e) or repr(e)
                self.root.after(0, lambda err=error_text: self.logger.error("卖出限价单失败: %s", err))
            finally:
                loop.close()

        threading.Thread(target=worker, daemon=True).start()

    async def sell_position_limit(self, position, size: float, price: float):
        config = self.validate_credentials_config()
        creds = ApiCreds(config["api_key"], config["secret"], config["passphrase"]) if config["api_key"] else await self.derive_api_creds()
        if creds is None:
            raise RuntimeError("无法派生 CLOB API 凭证")
        client = self.build_client(config, creds)
        token_id = str(position.get("asset"))
        tick_size = str(position.get("orderPriceMinTickSize") or "0.01")
        price = self.clamp_price(price, tick_size)
        self.logger.info("提交卖出订单: %s price=%.4f size=%.4f tick=%s", token_id[:12], price, size, tick_size)
        signed = await asyncio.to_thread(
            client.create_order,
            order_args=OrderArgs(token_id=token_id, price=float(price), size=float(size), side=Side.SELL),
            options=PartialCreateOrderOptions(tick_size=tick_size),
        )
        resp = await self._post_signed_order_with_retry(client, signed, order_type=OrderType.GTC, post_only=False)
        self._assert_order_response_ok(resp, action_name="持仓限价卖出")
        await self.push_trade_result(
            "限价卖出",
            position.get("title", ""),
            position.get("outcome", ""),
            size,
            price,
            resp,
            market_slug=position.get("slug") or position.get("eventSlug"),
        )
        return resp

    async def push_trade_result(self, action, market_title, direction, size, price, resp, market_slug=""):
        order_id = ""
        status = ""
        if isinstance(resp, dict):
            order_id = resp.get("orderID") or resp.get("id") or ""
            status = str(resp.get("status") or resp.get("success") or "")
        await asyncio.sleep(1.5)
        positions = await self.fetch_positions()
        self.latest_positions = positions
        self.root.after(0, lambda positions=positions: self.render_positions(positions))
        pnl_block = self.positions_pnl_markdown(positions, market_title, market_slug)
        content = (
            "### Polymarket 交易提交结果\n\n"
            f"- 操作: `{action}`\n"
            f"- 市场: {market_title}\n"
            f"- 方向: `{direction}`\n"
            f"- 数量: `{size:.4f}`\n"
            f"- 价格: `{price:.4f}`\n"
            f"- 状态: `{status}`\n"
            f"- 订单: `{order_id}`\n\n"
            f"{pnl_block}\n\n"
            f"原始返回: `{str(resp)[:500]}`"
        )
        await self.push_to_server_chan(f"Polymarket {action}结果", content)

    def positions_pnl_markdown(self, positions, market_title="", market_slug=""):
        visible = [p for p in positions if self._float_or_zero(p.get("size")) > 0.000001]
        total_value = sum(self._float_or_zero(p.get("currentValue")) for p in visible)
        total_pnl = sum(self._float_or_zero(p.get("cashPnl")) for p in visible)
        total_cost = total_value - total_pnl
        total_pct = (total_pnl / total_cost * 100.0) if abs(total_cost) > 0.000001 else 0.0

        related = []
        market_title_lower = str(market_title or "").lower()
        market_slug_lower = str(market_slug or "").lower()
        for p in visible:
            title = str(p.get("title", ""))
            slug = str(p.get("slug") or p.get("eventSlug") or "")
            if (market_slug_lower and market_slug_lower in slug.lower()) or (market_title_lower and market_title_lower == title.lower()):
                related.append(p)

        rows = [
            "### 当前持仓盈亏\n",
            f"- 持仓数: `{len(visible)}`",
            f"- 总现值: `{total_value:.2f}` USDC",
            f"- 总浮盈亏: `{total_pnl:+.2f}` USDC (`{total_pct:+.2f}%`)",
        ]
        if related:
            rows.append("\n相关市场持仓:")
            for p in related[:4]:
                rows.append(self.position_summary_line(p))
        elif visible:
            rows.append("\n当前主要持仓:")
            for p in visible[:4]:
                rows.append(self.position_summary_line(p))
        else:
            rows.append("\n当前没有可见持仓。")
        return "\n".join(rows)

    def position_summary_line(self, p):
        return (
            f"- {p.get('outcome', '')} `{self._float_or_zero(p.get('size')):.2f}` 份 | "
            f"均价 `{self._float_or_zero(p.get('avgPrice')):.4f}` | "
            f"现价 `{self._float_or_zero(p.get('curPrice')):.4f}` | "
            f"现值 `{self._float_or_zero(p.get('currentValue')):.2f}` | "
            f"浮盈亏 `{self._float_or_zero(p.get('cashPnl')):+.2f}` USDC "
            f"(`{self._float_or_zero(p.get('percentPnl')):+.2f}%`) | "
            f"{str(p.get('title', ''))[:80]}"
        )

    async def push_to_server_chan(self, title, content):
        sendkey = self.ent_sendkey.get().strip()
        if not sendkey:
            return
        url = f"https://sctapi.ftqq.com/{sendkey}.send"
        try:
            session = _get_aiohttp_session()
            async with session.post(url, data={"title": title, "desp": content}, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status >= 400:
                    self.logger.warning("推送返回 HTTP %s", response.status)
        except Exception as e:
            self.logger.error("推送异常: %s", e)

    def ema(self, values, period: int):
        alpha = 2.0 / (period + 1.0)
        result = values[0]
        for value in values[1:]:
            result = alpha * value + (1.0 - alpha) * result
        return result

    def rsi(self, values, period: int):
        changes = [values[i] - values[i - 1] for i in range(1, len(values))]
        recent = changes[-period:]
        gains = sum(max(x, 0.0) for x in recent) / period
        losses = sum(max(-x, 0.0) for x in recent) / period
        if losses <= 0:
            return 100.0
        rs = gains / losses
        return 100.0 - (100.0 / (1.0 + rs))

    def price_decimals(self, tick_size: str) -> int:
        if "." not in tick_size:
            return 0
        return len(tick_size.rstrip("0").split(".", 1)[1])

    def clamp_price(self, price: float, tick_size: str) -> float:
        tick = float(tick_size)
        decimals = self.price_decimals(tick_size)
        return round(min(max(price, tick), 1.0 - tick), decimals)

    @staticmethod
    def _extract_fill(resp, limit_price, limit_size):
        if not isinstance(resp, dict):
            return {"fill_price": float(limit_price), "fill_size": float(limit_size), "status": "unverified", "verified": False}
        status_raw = resp.get("status")
        status = str(status_raw).lower() if status_raw else ""
        if status != "matched":
            return {"fill_price": float(limit_price), "fill_size": float(limit_size), "status": status or "unverified", "verified": False}
        making = resp.get("makingAmount")
        taking = resp.get("takingAmount")
        try:
            making_f = float(making) if making is not None else None
            taking_f = float(taking) if taking is not None else None
        except (TypeError, ValueError):
            making_f = taking_f = None
        if making_f is None or taking_f is None or making_f <= 0 or taking_f <= 0:
            return {"fill_price": float(limit_price), "fill_size": float(limit_size), "status": status, "verified": False}
        fill_price = making_f / taking_f
        fill_size = taking_f / 1_000_000.0
        if not (0.0 < fill_price < 1.0):
            return {"fill_price": float(limit_price), "fill_size": float(limit_size), "status": status, "verified": False}
        return {"fill_price": fill_price, "fill_size": fill_size, "status": status, "verified": True}

    @staticmethod
    def _assert_order_response_ok(resp, action_name="订单"):
        """
        Raise RuntimeError with a specific status hint if resp is not a
        success-shaped CLOB response. Per Polymarket order-lifecycle docs,
        success statuses are: live | matched | delayed. Anything else
        (unmatched, missing status field, success: false) must abort.
        """
        if not isinstance(resp, dict):
            raise RuntimeError(f"{action_name}失败: 非 dict 响应 {type(resp).__name__}")
        if resp.get("success") is False:
            raise RuntimeError(f"{action_name}失败: 交易所拒绝 {resp}")
        status = str(resp.get("status") or "").lower()
        if status not in {"live", "matched", "delayed"}:
            raise RuntimeError(f"{action_name}失败: 状态 '{status or '<missing>'}'，订单未稳定落地，请去 polymarket.com/portfolio 对账")

    def _parse_token_ids(self, raw):
        if isinstance(raw, list):
            return [str(x) for x in raw]
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
            return [str(x) for x in parsed]
        except Exception:
            return []

    def _parse_datetime(self, raw):
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return None

    def _float_or_zero(self, value):
        try:
            result = float(value)
        except (TypeError, ValueError):
            return 0.0
        if result != result or result in (float("inf"), float("-inf")):
            return 0.0
        return result

    def _optional_float(self, value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _book_level_value(self, level, key: str):
        if isinstance(level, dict):
            return level.get(key)
        return getattr(level, key, None)


def acquire_single_instance_lock():
    lock_file = open(LOCK_FILE, "a+", encoding="utf-8")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("PolyQuickTrader is already running.", file=sys.stderr)
        lock_file.close()
        return None
    lock_file.seek(0)
    lock_file.truncate(0)
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    return lock_file


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
