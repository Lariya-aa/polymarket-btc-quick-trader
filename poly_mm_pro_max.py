import asyncio
import json
import logging
import math
import os
import re
import sys
import tempfile
import threading
import time
import tkinter as tk
import uuid
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from tkinter import messagebox, scrolledtext, ttk

import aiohttp
from py_clob_client_v2 import OrderArgs, OrderType, PartialCreateOrderOptions, Side
from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import ApiCreds


# Filenames only — resolved at runtime by config_path() / log_path() /
# lock_path() below. They live under user_data_dir() so a packaged .app
# or .exe (which has unpredictable CWD) writes to a stable per-user
# location instead of scattering files next to the binary.
CONFIG_FILE = "poly_config_pro.json"
LOG_FILE = "poly_mm_pro_max.log"
LOCK_FILE = "poly_mm_pro_max.lock"
APP_DIR_NAME = "PolyMarketTrader"
GAMMA_EVENT_SLUG_URL = "https://gamma-api.polymarket.com/events/slug"
POLYMARKET_BASE_URL = "https://polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
MINIMAX_CHAT_URL = "https://api.minimaxi.com/v1/chat/completions"
MINIMAX_MODEL = "MiniMax-M2.7"
CHAIN_ID = 137

# --- BTC short-horizon probability model tuning ---------------------------
# These constants drive the heuristic in fetch_btc_signal(). They are NOT
# back-tested — they encode a conservative prior (always pull toward 0.5)
# and were chosen by hand. Treat the displayed Up/Down probability as a
# rough nudge, not as edge. UI should label this signal as "未经回测".
PROB_MOMENTUM_FAST_WEIGHT = 0.50  # short-window return weight in momentum blend
PROB_MOMENTUM_MID_WEIGHT = 0.35   # mid-window return weight
PROB_MOMENTUM_SLOW_WEIGHT = 0.15  # long-window return weight (sum = 1.0)
PROB_VOL_SCALE = 3.0              # divides (momentum+trend+rsi_bias) by vol*this
PROB_Z_CLAMP = 2.0                # sigmoid input bounded to [-CLAMP, +CLAMP]
PROB_SHRINK_TOWARD_HALF = 0.6     # final = 0.5 + (sigmoid - 0.5) * this.
                                  # 0.6 keeps output ~[0.27, 0.73] — a deliberate
                                  # cap so the GUI never shows extreme confidence
                                  # on a signal we can't validate.

# --- Order safety ----------------------------------------------------------
# Order submission wraps the CLOB call in asyncio.wait_for. A timeout does
# NOT prove the order was rejected — the exchange may have accepted it but
# the response was lost. We therefore generate a local_attempt_id per
# attempt (logged client-side only — py_clob_client_v2.OrderArgs has no
# client-ID field; we verified its signature is
#   (token_id, price, size, side, expiration=0, builder_code=..., metadata=..., user_usdc_balance=None)
# so the UUID is purely a local reconciliation anchor) and refuse to
# silently retry on TimeoutError; the user must reconcile by hand. See
# buy_quick_market / sell_position_limit.
ORDER_SUBMIT_TIMEOUT_SECONDS = 25

# --- MiniMax token budgets -------------------------------------------------
# MiniMax-M2.7 is a thinking model: it emits a <think>...</think> chain-of-
# thought block before the actual JSON. The system prompt asks it not to,
# but the model still thinks — it just hides the block from `content` while
# still consuming tokens. If max_completion_tokens is too small the whole
# budget goes to thinking and the JSON gets cut off mid-stream (finish_reason
# = "length"), triggering the repair path. We give the primary call enough
# headroom to fit thinking + JSON in one shot most of the time, and the
# repair call enough to also include thinking + a compact final JSON.
MINIMAX_PRIMARY_TOKEN_BUDGET = 2000
MINIMAX_REPAIR_TOKEN_BUDGET = 800

# --- Categories registry ---------------------------------------------------
# Each entry is the UI label shown in the dropdown, mapped to a tuple:
#   (category_code, fetcher_method_name, fetcher_kwargs_dict)
# The fetcher_method_name is resolved on `self` at click time so the
# fetcher can stay an async method on PolyQuickTrader. To add a new
# category, append one entry here — UI dispatch picks it up automatically.
CATEGORIES = {
    "BTC – 短周期":   ("BTC", "fetch_quick_btc_markets",    {}),
    "NBA":            ("NBA", "fetch_tag_markets",          {"tag_slug": "nba",            "category": "NBA", "subject_label": "NBA"}),
    "NFL":            ("NFL", "fetch_tag_markets",          {"tag_slug": "nfl",            "category": "NFL", "subject_label": "NFL"}),
    "世界杯":         ("WC",  "fetch_tag_markets",          {"tag_slug": "fifa-world-cup", "category": "WC",  "subject_label": "WC"}),
    "新上线盘口":     ("NEW", "fetch_newly_listed_markets", {}),
}


@dataclass
class PolyMarket:
    """One Polymarket binary outcome market (Yes / No).

    Naming uses Polymarket's native vocabulary (Yes/No) rather than the
    UI-shorthand Up/Down that this app started with for BTC-only use.
    For BTC-Up/Down markets, Yes == "BTC will go up" and No == "BTC will
    go down" — same tokens, just different display labels in the UI.

    `category` and `subject` were added when the app expanded beyond BTC:
        category ∈ {"BTC", "NBA", "NFL", "WC", "NEW"}
        subject  = short label shown in the first table column. For BTC
                   it's the period ("5m"/"15m"/...); for sport markets
                   it's a compact form of the matchup or league tag.
    """
    slug: str
    event_slug: str
    question: str
    yes_id: str
    no_id: str
    tick_size: str
    period: str
    end_dt: datetime | None
    ended: bool
    yes_bid: float
    yes_ask: float
    no_bid: float
    no_ask: float
    spread: float
    volume24h: float
    category: str = "BTC"
    subject: str = ""


def user_data_dir() -> str:
    """Per-OS writable directory for the app's config, log, and any
    future state file.

    macOS:   ~/Library/Application Support/PolyMarketTrader
    Windows: %APPDATA%\\PolyMarketTrader  (typically C:\\Users\\X\\AppData\\Roaming)
    Linux:   $XDG_DATA_HOME/PolyMarketTrader  or  ~/.local/share/PolyMarketTrader

    The directory is created on first call. Used so a packaged .app /
    .exe (whose CWD is unpredictable) doesn't drop files into random
    locations next to the binary.
    """
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    elif sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    path = os.path.join(base, APP_DIR_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def config_path() -> str:
    return os.path.join(user_data_dir(), CONFIG_FILE)


def log_path() -> str:
    return os.path.join(user_data_dir(), LOG_FILE)


def lock_path() -> str:
    # Lock lives in tempdir, not user-data, so a wedged lock doesn't
    # persist across reboots (most OSes wipe tempdir on boot).
    return os.path.join(tempfile.gettempdir(), LOCK_FILE)


class TkinterLogHandler(logging.Handler):
    def __init__(self, text_widget):
        super().__init__()
        self.text_widget = text_widget

    def emit(self, record):
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

        self.latest_quick_markets: list[PolyMarket] = []
        self.latest_positions = []
        self.latest_signal = None
        self.last_positions_fetch_error: str | None = None

        self.logger = logging.getLogger("PolyQuickTrader")
        self.logger.setLevel(logging.INFO)
        self.logger.handlers.clear()

        self.setup_ui()
        handler = TkinterLogHandler(self.log_text)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
        self.logger.addHandler(handler)

        file_handler = logging.FileHandler(log_path(), encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        self.logger.addHandler(file_handler)

        self.load_config_from_local()
        self.load_env_file()
        self.load_credentials_from_env()

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

        quick_frame = ttk.LabelFrame(self.root, text=" 2. 市场列表（多类目） ", padding=10)
        quick_frame.pack(fill="x", padx=15, pady=5)

        quick_ctrl_frame = ttk.Frame(quick_frame)
        quick_ctrl_frame.pack(fill="x", pady=(0, 8))
        ttk.Label(quick_ctrl_frame, text="类目:").pack(side="left", padx=(0, 4))
        # Category Combobox controls which fetcher runs on "扫描". The keys
        # of CATEGORIES are the human-readable labels shown here; dispatch
        # happens in scan_quick_button_clicked.
        self.cbo_category = ttk.Combobox(quick_ctrl_frame, width=18, state="readonly")
        self.cbo_category["values"] = tuple(CATEGORIES.keys())
        self.cbo_category.set(next(iter(CATEGORIES)))  # default = first entry (BTC)
        self.cbo_category.pack(side="left", padx=(0, 8))
        self.btn_scan_quick = ttk.Button(quick_ctrl_frame, text="扫描", width=10, command=self.scan_quick_button_clicked)
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
        # Buy buttons use Polymarket-native Yes/No labels. For BTC markets
        # Yes ≡ Up token, No ≡ Down token (legacy internal "UP"/"DOWN"
        # direction strings retained for backward compat with the buy path).
        self.btn_buy_up = ttk.Button(quick_ctrl_frame, text="买 Yes", width=10, command=lambda: self.buy_selected_quick_market("UP"))
        self.btn_buy_up.pack(side="left", padx=4)
        self.btn_buy_down = ttk.Button(quick_ctrl_frame, text="买 No", width=10, command=lambda: self.buy_selected_quick_market("DOWN"))
        self.btn_buy_down.pack(side="left", padx=4)

        self.lbl_quick_signal = ttk.Label(quick_frame, text="只做辅助判断；每次真实下单前都会确认。", foreground="#475569")
        self.lbl_quick_signal.pack(fill="x", pady=(0, 8))

        self.quick_tree = ttk.Treeview(
            quick_frame,
            columns=("subject", "end", "yes", "no", "spread", "volume", "question"),
            show="headings",
            height=8,
        )
        quick_headings = {
            "subject": "类型",
            "end": "结束时间",
            "yes": "Yes买/卖",
            "no": "No买/卖",
            "spread": "价差",
            "volume": "24h量",
            "question": "市场",
        }
        quick_widths = {"subject": 70, "end": 135, "yes": 85, "no": 85, "spread": 60, "volume": 80, "question": 490}
        for col, title in quick_headings.items():
            self.quick_tree.heading(col, text=title)
            self.quick_tree.column(col, width=quick_widths[col], anchor="center" if col != "question" else "w")
        self.quick_tree.pack(fill="x", expand=False)

        pos_frame = ttk.LabelFrame(self.root, text=" 3. 持仓与卖出 ", padding=10)
        pos_frame.pack(fill="x", padx=15, pady=5)

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

        log_frame = ttk.LabelFrame(self.root, text=" 4. 运行日志 ", padding=10)
        log_frame.pack(fill="both", expand=True, padx=15, pady=5)
        self.log_text = scrolledtext.ScrolledText(
            log_frame,
            state="disabled",
            height=18,
            bg="#1f2937",
            fg="#e5e7eb",
            font=("Consolas", 10),
        )
        self.log_text.pack(fill="both", expand=True)

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
            "category": self.cbo_category.get(),
        }

    def save_config_to_local(self):
        try:
            path = config_path()
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.safe_config(), f, indent=4, ensure_ascii=False)
            # Best-effort: 0o600 is a no-op on Windows but harmless.
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            self.logger.info("已保存非敏感配置 → %s", path)
        except Exception as e:
            self.logger.error("保存配置失败: %s", e)

    def load_config_from_local(self):
        path = config_path()
        # Migration: older versions wrote the config next to poly_mm_pro_max.py
        # (CWD-relative). If the user has an old file there and no new file in
        # user-data dir yet, move it. One-shot, safe to re-run.
        legacy = CONFIG_FILE  # CWD-relative
        if not os.path.exists(path) and os.path.exists(legacy):
            try:
                os.replace(legacy, path)
                self.logger.info("已迁移旧配置 %s → %s", legacy, path)
            except OSError as e:
                self.logger.warning("迁移旧配置失败: %s", e)
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                config = json.load(f)
            leaked_keys = {"priv_key", "api_key", "secret", "passphrase", "sendkey", "minimax_key"} & set(config)
            if leaked_keys:
                self.logger.warning("检测到旧配置含敏感字段，本次不会回填: %s", ", ".join(sorted(leaked_keys)))
            self._set_entry(self.ent_funder, config.get("funder", ""))
            if str(config.get("signature_type", "")).strip() in {"0", "1", "2", "3"}:
                self.cbo_signature_type.set(str(config.get("signature_type")).strip())
            self._set_entry(self.ent_quick_usdc, config.get("quick_usdc", "5"))
            self._set_entry(self.ent_quick_max_price, config.get("quick_max_price", "0.60"))
            saved_category = config.get("category")
            if saved_category and saved_category in CATEGORIES:
                self.cbo_category.set(saved_category)
        except Exception as e:
            self.logger.error("加载配置文件失败: %s", e)

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
            return creds
        except Exception as e:
            self.logger.error("派生 CLOB API 凭证失败: %s", e)
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

    async def fetch_json(self, url: str, params=None, quiet_404: bool = False):
        # quiet_404=True is used by speculative slug probes (the generated
        # btc-updown-<period>-<ts> fallback) where 404 is an expected miss,
        # not an error. HTML-scraped slugs keep the WARNING because a 404
        # there means the link on polymarket.com is stale and worth seeing.
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12), headers={"User-Agent": "Mozilla/5.0"}) as session:
                async with session.get(url, params=params) as response:
                    if response.status == 404 and quiet_404:
                        return None
                    if response.status != 200:
                        self.logger.warning("GET %s 返回 HTTP %s", url, response.status)
                        return None
                    return await response.json()
        except Exception as e:
            self.logger.warning("GET %s 失败: %s", url, e)
            return None

    def scan_quick_button_clicked(self):
        # Read the selected category and resolve which fetcher to call.
        # Falls back to the first registry entry (BTC) if the dropdown
        # is empty for any reason.
        label = self.cbo_category.get() or next(iter(CATEGORIES))
        if label not in CATEGORIES:
            self.logger.error("未知类目: %s", label)
            return
        category_code, method_name, kwargs = CATEGORIES[label]
        fetcher = getattr(self, method_name, None)
        if not callable(fetcher):
            self.logger.error("找不到 fetcher 方法: %s", method_name)
            return

        self.btn_scan_quick.configure(state="disabled")
        self.logger.info("开始扫描类目: %s", label)

        def worker():
            loop = asyncio.new_event_loop()
            try:
                markets = loop.run_until_complete(fetcher(**kwargs))
                self.latest_quick_markets = markets
                self.root.after(0, lambda: self.render_quick_markets(markets))
                self.root.after(0, lambda lbl=label, n=len(markets): self.logger.info("%s 扫描完成: %s 个候选。", lbl, n))
            except Exception as e:
                error_text = str(e) or repr(e)
                self.root.after(0, lambda err=error_text: self.logger.error("市场扫描失败: %s", err))
            finally:
                loop.close()
                self.root.after(0, lambda: self.btn_scan_quick.configure(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

    async def fetch_quick_btc_markets(self):
        url = f"{POLYMARKET_BASE_URL}/crypto/bitcoin"
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12), headers={"User-Agent": "Mozilla/5.0"}) as session:
                async with session.get(url) as response:
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

        # Generated slugs are speculative probes of period-aligned timestamps;
        # most will 404. Track them so fetch_json doesn't WARN-log each miss.
        generated_slugs = set(self.generated_btc_updown_slugs())
        for slug in generated_slugs:
            if slug not in slugs:
                slugs.insert(0, slug)

        markets = []
        now = datetime.now(timezone.utc)
        for slug in slugs[:80]:
            event = await self.fetch_json(
                f"{GAMMA_EVENT_SLUG_URL}/{slug}",
                quiet_404=slug in generated_slugs,
            )
            if not isinstance(event, dict):
                continue
            for market in event.get("markets") or []:
                item = self.quick_market_candidate(event, market, now)
                if item:
                    markets.append(item)

        markets.sort(key=lambda item: (item.ended, item.end_dt or datetime.max.replace(tzinfo=timezone.utc)))
        return markets[:20]

    async def fetch_tag_markets(self, tag_slug: str, category: str, subject_label: str | None = None, limit: int = 30):
        """Generic discovery for any Polymarket tag (NBA, NFL, fifa-world-cup, ...).
        Returns up to `limit` PolyMarkets in an open state, sorted by end-date ascending.

        `subject_label` overrides the per-market subject column. Defaults to the
        category short name (e.g. "NBA") when not provided.
        """
        url = "https://gamma-api.polymarket.com/events"
        params = {
            "tag_slug": tag_slug,
            "closed": "false",
            "limit": str(limit),
            "order": "startDate",
            "ascending": "true",
        }
        events = await self.fetch_json(url, params=params)
        if not isinstance(events, list):
            self.logger.warning("标签 %s 返回非列表数据，跳过", tag_slug)
            return []
        markets: list[PolyMarket] = []
        now = datetime.now(timezone.utc)
        for event in events:
            if not isinstance(event, dict):
                continue
            label = subject_label or category
            for market in event.get("markets") or []:
                item = self._build_market(event, market, now, category=category, subject=label)
                if item:
                    markets.append(item)
        markets.sort(key=lambda item: (item.ended, item.end_dt or datetime.max.replace(tzinfo=timezone.utc)))
        return markets[:limit]

    async def fetch_newly_listed_markets(self, limit: int = 30, min_volume_24h: float = 100.0):
        """Recently-listed (createdAt desc) Polymarket events with a small
        volume floor so dust markets don't dominate the list.

        Returns up to `limit` PolyMarkets across any category. `subject`
        is the event slug's first hyphen-prefixed token (best-effort
        category hint) since we don't know the tag without an extra API
        call per event.
        """
        url = "https://gamma-api.polymarket.com/events"
        params = {
            "closed": "false",
            "limit": str(limit * 3),  # over-fetch so the volume filter still gives us `limit` rows
            "order": "createdAt",
            "ascending": "false",
        }
        events = await self.fetch_json(url, params=params)
        if not isinstance(events, list):
            self.logger.warning("新上线接口返回非列表数据，跳过")
            return []
        markets: list[PolyMarket] = []
        now = datetime.now(timezone.utc)
        for event in events:
            if not isinstance(event, dict):
                continue
            if self._float_or_zero(event.get("volume24hr")) < min_volume_24h:
                continue
            ev_slug = event.get("slug") or ""
            subject_hint = ev_slug.split("-", 1)[0].upper()[:6] if ev_slug else "NEW"
            for market in event.get("markets") or []:
                item = self._build_market(event, market, now, category="NEW", subject=subject_hint)
                if item:
                    markets.append(item)
            if len(markets) >= limit:
                break
        # Preserve the API's createdAt-desc order. We previously re-sorted
        # by end_dt desc here, which inverted intent — newly-listed markets
        # often have end_dt far in the future, so end_dt-desc bubbled
        # not-recently-listed-but-far-out markets to the top. The /events
        # endpoint already returns rows in createdAt-desc order per the
        # query params, so no local sort is needed.
        return markets[:limit]

    def generated_btc_updown_slugs(self):
        # Fallback: when scraping polymarket.com/crypto/bitcoin yields no
        # event links (HTML structure changed, regional block, etc.) we
        # generate slugs by aligning the current unix time to each period
        # boundary and probing ±2 boundaries around now. Covers all five
        # horizons the UI offers (5m/15m/1h/4h/1d) so the scanner stays
        # usable even when the HTML scrape returns zero candidates.
        now_ts = int(time.time())
        slugs = []
        for period, seconds in (("5m", 300), ("15m", 900), ("1h", 3600), ("4h", 14400), ("1d", 86400)):
            base = now_ts - (now_ts % seconds)
            for offset in (-2, -1, 0, 1, 2):
                start_ts = base + offset * seconds
                if start_ts > 0:
                    slugs.append(f"btc-updown-{period}-{start_ts}")
        return slugs

    def quick_market_candidate(self, event: dict, market: dict, now: datetime):
        # BTC short-cycle gate: only keep markets whose question is the
        # "BTC up or down" phrasing this scanner is meant for. Then build
        # a PolyMarket via the shared constructor.
        question = market.get("question") or event.get("title") or ""
        slug = market.get("slug") or event.get("slug") or ""
        if "bitcoin" not in question.lower() and "btc" not in slug.lower():
            return None
        if "up" not in question.lower() or "down" not in question.lower():
            return None
        return self._build_market(
            event, market, now,
            category="BTC",
            subject=self.quick_period_from_slug_or_title(slug, question),
        )

    def _build_market(self, event: dict, market: dict, now: datetime, category: str, subject: str):
        """Shared PolyMarket constructor used by every category-specific
        scanner. Returns None if the raw event/market dict fails the
        common preconditions: must be open, must have two clobTokenIds,
        must have a sensible bid/ask pair.

        Category-specific filtering (e.g. "is this a BTC up/down market?")
        is the caller's job — by the time we get here, the raw market is
        assumed to belong in the requested category.
        """
        if market.get("closed") is True or market.get("active") is False or market.get("acceptingOrders") is False:
            return None
        token_ids = self._parse_token_ids(market.get("clobTokenIds"))
        if len(token_ids) < 2:
            return None
        question = market.get("question") or event.get("title") or ""
        slug = market.get("slug") or event.get("slug") or ""

        end_dt = self._parse_datetime(market.get("endDate") or event.get("endDate"))
        best_bid = self._optional_float(market.get("bestBid"))
        best_ask = self._optional_float(market.get("bestAsk"))
        if best_bid is None or best_ask is None or best_bid <= 0 or best_ask >= 1 or best_bid >= best_ask:
            return None

        return PolyMarket(
            slug=slug,
            event_slug=event.get("slug") or slug,
            question=question,
            yes_id=token_ids[0],
            no_id=token_ids[1],
            tick_size=str(market.get("orderPriceMinTickSize") or "0.01"),
            period=self.quick_period_from_slug_or_title(slug, question) if category == "BTC" else "",
            end_dt=end_dt,
            ended=bool(end_dt and end_dt <= now),
            yes_bid=best_bid,
            yes_ask=best_ask,
            no_bid=max(0.0, 1.0 - best_ask),
            no_ask=min(1.0, 1.0 - best_bid),
            spread=best_ask - best_bid,
            volume24h=self._float_or_zero(market.get("volume24hrClob") or market.get("volume24hr")),
            category=category,
            subject=subject,
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
            # `subject` falls back to `period` for BTC rows (which sets
            # period during _build_market) so legacy rows still show
            # "5m"/"15m"/etc in the first column.
            label = market.subject or market.period or market.category
            values = (
                label,
                end_text,
                f"{market.yes_bid:.2f}/{market.yes_ask:.2f}",
                f"{market.no_bid:.2f}/{market.no_ask:.2f}",
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
        selected_market = None
        selected = self.quick_tree.selection()
        if selected:
            idx = int(selected[0])
            if idx < len(self.latest_quick_markets):
                selected_market = self.latest_quick_markets[idx]
        category = selected_market.category if selected_market else "BTC"
        self.lbl_quick_signal.configure(text=f"正在计算 [{category}] 概率... {datetime.now().strftime('%H:%M:%S')}")
        self.logger.info("开始计算 [%s] AI 概率。", category)
        minimax_key = self.ent_minimax_key.get().strip()

        def worker():
            loop = asyncio.new_event_loop()
            try:
                # Dispatch local signal by category. BTC has a real Binance-
                # candle-based heuristic; other categories return a minimal
                # "shell" signal made of market quotes only. Both shapes are
                # consumed identically by fetch_minimax_prediction and render_signal.
                signal = loop.run_until_complete(self._local_signal_for(selected_market))
                signal["llm"] = loop.run_until_complete(self.fetch_minimax_prediction(signal, minimax_key, selected_market)) if minimax_key else None
                self.latest_signal = signal
                self.root.after(0, lambda: self.render_signal(signal))
            except Exception as e:
                error_text = str(e) or repr(e)
                self.root.after(0, lambda err=error_text: self.logger.error("AI概率判断失败: %s", err))
            finally:
                loop.close()
                self.root.after(0, lambda: self.btn_predict_quick.configure(state="normal", text="AI概率判断"))

        threading.Thread(target=worker, daemon=True).start()

    async def _local_signal_for(self, selected_market: PolyMarket | None):
        """Dispatch the per-category local signal provider.

        Returns a dict with at minimum: category, fetched_at, market_period,
        market_question. BTC adds the full Binance-derived heuristic
        (prob_up, ema_fast, rsi, etc). Other categories return only market
        quote info because we don't have a domain model for them yet —
        MiniMax does all the actual probability estimation.

        Future categories that want their own local heuristic can branch
        here without touching the rest of the pipeline.
        """
        category = selected_market.category if selected_market else "BTC"
        if category == "BTC":
            return await self.fetch_btc_signal(selected_market)
        # Non-BTC fallback: build a market-only signal. No probability
        # prediction from our side. MiniMax gets just the market quote.
        m = selected_market
        return {
            "category": category,
            "fetched_at": datetime.now().strftime("%H:%M:%S"),
            "market_period": m.subject if m else "",
            "market_question": m.question if m else "",
            "horizon_minutes": None,
            "yes_bid": m.yes_bid if m else None,
            "yes_ask": m.yes_ask if m else None,
            "no_bid": m.no_bid if m else None,
            "no_ask": m.no_ask if m else None,
            "spread": m.spread if m else None,
            "volume24h": m.volume24h if m else None,
            # Leave probability fields absent — render_signal/MiniMax handle that.
        }

    async def fetch_btc_signal(self, selected_market: PolyMarket | None = None):
        horizon_minutes = self.market_horizon_minutes(selected_market)
        lookback = max(80, min(1000, horizon_minutes * 4 + 40))
        params = {"symbol": "BTCUSDT", "interval": "1m", "limit": str(lookback)}
        url = "https://api.binance.com/api/v3/klines"
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.get(url, params=params) as response:
                    if response.status != 200:
                        raise RuntimeError(f"Binance HTTP {response.status}")
                    klines = await response.json()
        except Exception:
            url = "https://data-api.binance.vision/api/v3/klines"
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.get(url, params=params) as response:
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
        momentum = (
            PROB_MOMENTUM_FAST_WEIGHT * ret_fast
            + PROB_MOMENTUM_MID_WEIGHT * ret_mid
            + PROB_MOMENTUM_SLOW_WEIGHT * ret_slow
        )
        trend = (ema_fast / ema_slow - 1.0) if ema_slow else 0.0
        rsi_bias = (rsi - 50.0) / 10000.0
        z = max(-PROB_Z_CLAMP, min(PROB_Z_CLAMP, (momentum + trend + rsi_bias) / (vol * PROB_VOL_SCALE)))
        raw_prob_up = 1.0 / (1.0 + math.exp(-z))
        prob_up = 0.5 + (raw_prob_up - 0.5) * PROB_SHRINK_TOWARD_HALF
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

    def market_horizon_minutes(self, selected_market: PolyMarket | None):
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

    async def fetch_minimax_prediction(self, signal, api_key: str, selected_market: PolyMarket | None = None):
        market_block = {}
        if selected_market:
            market_block = {
                "category": selected_market.category,
                "question": selected_market.question,
                "subject": selected_market.subject,
                "period": selected_market.period,
                "end_time": selected_market.end_dt.isoformat() if selected_market.end_dt else None,
                "yes_bid": selected_market.yes_bid,
                "yes_ask": selected_market.yes_ask,
                "no_bid": selected_market.no_bid,
                "no_ask": selected_market.no_ask,
                "spread": selected_market.spread,
                "volume24h": selected_market.volume24h,
            }

        payload = {
            "model": MINIMAX_MODEL,
            "temperature": 0.2,
            "top_p": 0.9,
            "max_completion_tokens": MINIMAX_PRIMARY_TOKEN_BUDGET,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "name": "TradingRiskAnalyst",
                    "content": (
                        "只输出 JSON。不要解释。不要推理过程。不要 markdown。"
                        "JSON keys: prob_up, prob_down, action, confidence, edge_summary, reason, risk。"
                        "prob_up = Yes 结算概率；BUY_UP = 买 Yes，BUY_DOWN = 买 No。"
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
            return {"error": error_text}

        try:
            data = json.loads(body)
            content = data["choices"][0]["message"]["content"]
            finish_reason = data["choices"][0].get("finish_reason")
            if finish_reason == "length":
                self.logger.warning("MiniMax 输出被截断，尝试二次 JSON 修复。")
                fixed = await self.repair_minimax_json(api_key, content)
                if fixed:
                    fixed["usage"] = data.get("usage") or {}
                    return fixed
                return {"error": "MiniMax 输出被截断"}
            parsed = self.parse_minimax_json(content)
            parsed["usage"] = data.get("usage") or {}
            return parsed
        except Exception as e:
            self.logger.error("MiniMax 返回解析失败: %s | 原文=%s", e, body[:500])
            return {"error": f"返回解析失败: {e}"}

    async def post_minimax_with_retry(self, api_key: str, payload: dict):
        last_error = None
        timeout = aiohttp.ClientTimeout(total=35, connect=10, sock_read=30)
        for attempt in range(1, 3):
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(
                        MINIMAX_CHAT_URL,
                        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                        json=payload,
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

    async def repair_minimax_json(self, api_key: str, content: str):
        payload = {
            "model": MINIMAX_MODEL,
            "temperature": 0,
            "max_completion_tokens": MINIMAX_REPAIR_TOKEN_BUDGET,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": "只输出 JSON，不要解释。字段: prob_up, prob_down, action, confidence, edge_summary, reason, risk。",
                },
                {
                    "role": "user",
                    "content": (
                        "把下面内容压缩成最终交易 JSON。若信息不足，action=NO_TRADE, confidence=LOW。\n"
                        + (content or "")[-2500:]
                    ),
                },
            ],
        }
        try:
            body = await self.post_minimax_with_retry(api_key, payload)
            data = json.loads(body)
            parsed = self.parse_minimax_json(data["choices"][0]["message"]["content"])
            return parsed
        except Exception as e:
            self.logger.warning("MiniMax 二次 JSON 修复失败: %s: %s", type(e).__name__, str(e) or repr(e))
            return None

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

    def parse_minimax_json(self, content: str):
        cleaned = re.sub(r"<think>.*?</think>", "", content or "", flags=re.S).strip()
        if not cleaned and content:
            cleaned = content.split("</think>", 1)[-1].strip() if "</think>" in content else content.strip()
        match = re.search(r"\{.*\}", cleaned, flags=re.S)
        if match:
            cleaned = match.group(0)
        if not cleaned.startswith("{"):
            raise ValueError("MiniMax 未返回 JSON 对象")
        parsed = json.loads(cleaned)
        # Coerce and clamp prob_up to [0,1]. Reject non-finite values
        # (NaN, inf, -inf) outright — they survive float() and min/max,
        # then propagate into UI / order sizing as NaN. Treat as
        # NO_TRADE so the user never sees a malformed prediction.
        try:
            raw_up = float(parsed.get("prob_up", 0.5))
        except (TypeError, ValueError):
            raw_up = 0.5
        if not math.isfinite(raw_up):
            parsed["prob_up"] = 0.5
            parsed["prob_down"] = 0.5
            parsed["action"] = "NO_TRADE"
            parsed["confidence"] = "LOW"
            parsed["reason"] = "MiniMax 返回非有限概率，已忽略"
            return parsed
        prob_up = min(max(raw_up, 0.0), 1.0)
        try:
            raw_down = float(parsed.get("prob_down", 1.0 - prob_up))
        except (TypeError, ValueError):
            raw_down = 1.0 - prob_up
        if not math.isfinite(raw_down):
            raw_down = 1.0 - prob_up
        parsed["prob_up"] = prob_up
        parsed["prob_down"] = min(max(raw_down, 0.0), 1.0)
        if parsed.get("action") not in {"BUY_UP", "BUY_DOWN", "NO_TRADE"}:
            parsed["action"] = "NO_TRADE"
        if parsed.get("confidence") not in {"LOW", "MEDIUM", "HIGH"}:
            parsed["confidence"] = "LOW"
        return parsed

    def render_signal(self, signal):
        """Render local + LLM signals to the signal label and log panel.

        Tolerant of two signal shapes:
          - BTC full signal: has prob_up, prob_down, confidence, rsi, ret_*
            → renders local probability + indicator details.
          - Non-BTC minimal signal: market quotes only, no prob fields
            → renders only market info + LLM prediction (if present).
        """
        category = signal.get("category", "BTC")
        llm = signal.get("llm")
        has_local_prob = "prob_up" in signal

        if has_local_prob:
            direction = "Yes" if signal["prob_up"] >= 0.5 else "No"
            text = (
                f"{signal['fetched_at']} | [{category}] {signal.get('market_period', '')}/{signal.get('horizon_minutes', '--')}m "
                f"| 本地: {direction} | Yes {signal['prob_up'] * 100:.1f}% / No {signal['prob_down'] * 100:.1f}% "
                f"| 置信 {signal['confidence'] * 100:.0f}% | RSI {signal['rsi']:.1f}"
            )
        else:
            text = (
                f"{signal['fetched_at']} | [{category}] {signal.get('market_question', '')[:40]} "
                f"| 盘口 Yes {signal.get('yes_bid', 0):.2f}/{signal.get('yes_ask', 0):.2f} "
                f"No {signal.get('no_bid', 0):.2f}/{signal.get('no_ask', 0):.2f}"
            )

        if llm:
            if llm.get("error"):
                text += " | MiniMax 不可用" + ("，仅本地概率" if has_local_prob else "")
            else:
                action_map = {"BUY_UP": "买Yes", "BUY_DOWN": "买No", "NO_TRADE": "不交易"}
                text += (
                    f" | MiniMax: Yes {llm['prob_up'] * 100:.1f}% / No {llm['prob_down'] * 100:.1f}% "
                    f"| {action_map.get(llm.get('action'), '不交易')} | {llm.get('confidence', 'LOW')}"
                )
        self.lbl_quick_signal.configure(text=text)

        if has_local_prob:
            self.logger.info(
                "本地概率[%s/%sm]: Yes %.1f%% / No %.1f%%，置信 %.0f%%，%sm %.3f%%，%sm %.3f%%，%sm %.3f%%，RSI %.1f",
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
        else:
            self.logger.info(
                "盘口[%s]: %s | Yes %.2f/%.2f | No %.2f/%.2f | 24h量=%.0f",
                category,
                signal.get("market_question", "")[:60],
                signal.get("yes_bid") or 0,
                signal.get("yes_ask") or 0,
                signal.get("no_bid") or 0,
                signal.get("no_ask") or 0,
                signal.get("volume24h") or 0,
            )

        if llm:
            if llm.get("error"):
                self.logger.warning("MiniMax 综合预测不可用: %s", llm["error"])
            else:
                self.logger.info(
                    "MiniMax综合: Yes %.1f%% / No %.1f%% | 动作=%s | 置信=%s | %s | 风险=%s | tokens=%s",
                    llm["prob_up"] * 100,
                    llm["prob_down"] * 100,
                    llm.get("action"),
                    llm.get("confidence"),
                    llm.get("reason", ""),
                    llm.get("risk", ""),
                    (llm.get("usage") or {}).get("total_tokens", "--"),
                )

    def _display_direction(self, direction: str) -> str:
        """Map the internal direction string ("UP" / "DOWN") to the
        user-facing label ("Yes" / "No"). Yes corresponds to the Up
        token, No to the Down token. The internal string is kept for
        order-routing back-compat; this helper is for any text the user
        sees (confirmation dialog, push notification, etc.)."""
        if direction == "UP":
            return "Yes"
        if direction == "DOWN":
            return "No"
        return direction

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
        if not (math.isfinite(usdc_amount) and math.isfinite(max_price)):
            messagebox.showerror("参数错误", "买入金额和最高价必须是有限数值。")
            return
        if usdc_amount <= 0 or max_price <= 0 or max_price >= 1:
            messagebox.showerror("参数错误", "买入金额必须大于 0，最高价必须在 0 到 1 之间。")
            return
        if market.ended:
            messagebox.showerror("市场已结束", "选中的短周期市场已经结束，请重新扫描。")
            return
        if not messagebox.askyesno(
            "确认快速买入",
            f"市场: {market.question}\n方向: {self._display_direction(direction)}\n金额: {usdc_amount:.2f} USDC\n最高可接受价格: {max_price:.4f}\n\n这是真实交易操作，可能立即成交。确认继续？",
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

    async def buy_quick_market(self, market: PolyMarket, direction: str, usdc_amount: float, max_price: float):
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
        local_attempt_id = str(uuid.uuid4())
        self.logger.info(
            "提交买入订单: %s price=%.4f size=%.4f tick=%s local_attempt_id=%s",
            direction, price, size, tick_size or market.tick_size, local_attempt_id,
        )
        try:
            resp = await asyncio.wait_for(
                asyncio.to_thread(
                    client.create_and_post_order,
                    order_args=OrderArgs(token_id=token_id, price=float(price), size=float(size), side=Side.BUY),
                    options=PartialCreateOrderOptions(tick_size=tick_size or market.tick_size),
                    order_type=OrderType.GTC,
                    post_only=False,
                ),
                timeout=ORDER_SUBMIT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"提交买入订单超时（local_attempt_id={local_attempt_id}）。"
                f"订单状态未知，可能已被交易所接收。请打开 https://polymarket.com/portfolio "
                f"核对后再决定下一步，切勿直接点重试。"
            )
        if isinstance(resp, dict) and resp.get("success") is False:
            raise RuntimeError(f"交易所拒绝订单 (local_attempt_id={local_attempt_id}): {resp}")
        await self.push_trade_result("快速买入", market.question, direction, size, price, resp, market_slug=market.slug)
        return resp

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

    async def fetch_positions(self):
        # Sets self.last_positions_fetch_error to None on success or to a
        # human-readable string on any failure. Callers can read this to
        # distinguish "user has zero open positions" (success, empty list)
        # from "the positions API failed" (any non-None error). Returning
        # only [] in both cases was misleading to the user.
        user = self.ent_funder.get().strip()
        if not user:
            self.last_positions_fetch_error = None
            return []
        params = {
            "user": user,
            "limit": "50",
            "sizeThreshold": "0",
            "sortBy": "CURRENT",
            "sortDirection": "DESC",
        }
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12), headers={"User-Agent": "Mozilla/5.0"}) as session:
                async with session.get("https://data-api.polymarket.com/positions", params=params) as response:
                    if response.status != 200:
                        msg = f"HTTP {response.status}"
                        self.logger.error("持仓接口返回 %s", msg)
                        self.last_positions_fetch_error = msg
                        return []
                    data = await response.json()
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            self.logger.error("读取持仓失败: %s", msg)
            self.last_positions_fetch_error = msg
            return []
        # Only treat the call as successful if the response shape is what
        # the API contract promises (a list). Other shapes (dict with an
        # error key, malformed JSON parsed as something else) used to be
        # silently dropped to [] with the error flag still cleared —
        # exactly the "API failed looks like empty portfolio" hazard the
        # error flag was added to prevent. Order matters: classify first,
        # then set the flag.
        if not isinstance(data, list):
            shape = type(data).__name__
            msg = f"unexpected response shape: {shape}"
            self.logger.error("持仓接口返回非列表 (%s)", shape)
            self.last_positions_fetch_error = msg
            return []
        self.last_positions_fetch_error = None
        return data

    def refresh_positions_button_clicked(self):
        def worker():
            loop = asyncio.new_event_loop()
            try:
                positions = loop.run_until_complete(self.fetch_positions())
                self.latest_positions = positions
                self.root.after(0, lambda: self.render_positions(positions))
                err = self.last_positions_fetch_error
                if err:
                    self.root.after(0, lambda e=err: self.logger.error(
                        "⚠ 持仓接口失败 (%s) — 上方显示的可能不是最新持仓。下单/卖出前请到 polymarket.com/portfolio 核对。", e))
                else:
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
        local_attempt_id = str(uuid.uuid4())
        self.logger.info(
            "提交卖出订单: %s price=%.4f size=%.4f tick=%s local_attempt_id=%s",
            token_id[:12], price, size, tick_size, local_attempt_id,
        )
        try:
            resp = await asyncio.wait_for(
                asyncio.to_thread(
                    client.create_and_post_order,
                    order_args=OrderArgs(token_id=token_id, price=float(price), size=float(size), side=Side.SELL),
                    options=PartialCreateOrderOptions(tick_size=tick_size),
                    order_type=OrderType.GTC,
                    post_only=False,
                ),
                timeout=ORDER_SUBMIT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"提交卖出订单超时（local_attempt_id={local_attempt_id}）。"
                f"订单状态未知，可能已被交易所接收。请打开 https://polymarket.com/portfolio "
                f"核对后再决定下一步，切勿直接点重试。"
            )
        if isinstance(resp, dict) and resp.get("success") is False:
            raise RuntimeError(f"交易所拒绝订单 (local_attempt_id={local_attempt_id}): {resp}")
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
            f"- 方向: `{self._display_direction(direction)}`\n"
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
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.post(url, data={"title": title, "desp": content}) as response:
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
            return float(value)
        except (TypeError, ValueError):
            return 0.0

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
    """Return an open file handle that holds an OS-level lock, or None if
    another instance is already running.

    Implementation differs by platform because Python's stdlib doesn't
    ship a portable advisory file-lock primitive:
      - Unix (macOS/Linux): fcntl.flock with LOCK_EX | LOCK_NB
      - Windows:            msvcrt.locking with LK_NBLCK on 1 byte

    On both platforms the OS releases the lock automatically when the
    process exits (or when the returned handle is GC'd / closed). We
    never need to delete the lock file.
    """
    path = lock_path()

    if sys.platform == "win32":
        import msvcrt
        # Open in append-update mode so an existing lockfile (from a
        # crashed prior run) is not truncated before we attempt to lock.
        lock_file = open(path, "a+", encoding="utf-8")
        try:
            # msvcrt.locking() locks `nbytes` from the current file
            # position. Ensure the file has at least 1 byte to lock
            # against, then reset position before calling.
            if lock_file.tell() == 0:
                lock_file.write(" ")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            lock_file.close()
            print("PolyQuickTrader is already running.", file=sys.stderr)
            return None
        # We hold the lock — overwrite the body with the current PID for
        # diagnostics. (Truncating doesn't release the lock.)
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(str(os.getpid()))
        lock_file.flush()
        return lock_file

    # Unix path (macOS, Linux, BSD)
    import fcntl
    lock_file = open(path, "w", encoding="utf-8")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        lock_file.close()
        print("PolyQuickTrader is already running.", file=sys.stderr)
        return None
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
    root.mainloop()
