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


CONFIG_FILE = "poly_config_pro.json"
LOG_FILE = "poly_mm_pro_max.log"
LOCK_FILE = "/tmp/poly_mm_pro_max.lock"
GAMMA_EVENT_SLUG_URL = "https://gamma-api.polymarket.com/events/slug"
POLYMARKET_BASE_URL = "https://polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
MINIMAX_CHAT_URL = "https://api.minimaxi.com/v1/chat/completions"
MINIMAX_MODEL = "MiniMax-M2.7"
CHAIN_ID = 137
PAPER_SIGNAL_THRESHOLD = 0.65


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
        self.paper_results = []
        self.paper_strategy_running = False
        self.paper_strategy_stop_requested = threading.Event()
        self.log_context = threading.local()

        self.logger = logging.getLogger("PolyQuickTrader")
        self.logger.setLevel(logging.INFO)
        self.logger.handlers.clear()

        self.setup_ui()
        self.logger.addFilter(self.log_category_filter)

        manual_handler = TkinterLogHandler(self.manual_log_text, category="manual")
        manual_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
        self.logger.addHandler(manual_handler)

        auto_handler = TkinterLogHandler(self.auto_log_text, category="auto")
        auto_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
        self.logger.addHandler(auto_handler)

        file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        self.logger.addHandler(file_handler)

        self.load_config_from_local()
        self.load_env_file()
        self.load_credentials_from_env()

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

        main_notebook = ttk.Notebook(self.root)
        main_notebook.pack(fill="both", expand=True, padx=15, pady=5)

        manual_tab = ttk.Frame(main_notebook)
        auto_tab = ttk.Frame(main_notebook)
        main_notebook.add(manual_tab, text="手动交易")
        main_notebook.add(auto_tab, text="自动模拟")

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
        paper_frame = ttk.LabelFrame(auto_tab, text=" AI 判断自动模拟止盈 ", padding=10)
        paper_frame.pack(fill="x", padx=0, pady=5)
        paper_row1 = ttk.Frame(paper_frame)
        paper_row1.pack(fill="x", pady=(0, 4))
        paper_row2 = ttk.Frame(paper_frame)
        paper_row2.pack(fill="x")

        ttk.Label(paper_row1, text="模拟金额:").pack(side="left", padx=(4, 4))
        self.ent_paper_usdc = ttk.Entry(paper_row1, width=7)
        self.ent_paper_usdc.insert(0, "5")
        self.ent_paper_usdc.pack(side="left", padx=4)
        ttk.Label(paper_row1, text=f"开单阈值: 单边>{PAPER_SIGNAL_THRESHOLD:.0%}").pack(side="left", padx=(8, 4))
        ttk.Label(paper_row1, text="模拟止盈:").pack(side="left", padx=(8, 4))
        self.ent_paper_take_profit = ttk.Entry(paper_row1, width=7)
        self.ent_paper_take_profit.insert(0, "0.60")
        self.ent_paper_take_profit.pack(side="left", padx=4)
        ttk.Label(paper_row1, text="轮询秒:").pack(side="left", padx=(8, 4))
        self.ent_paper_poll_seconds = ttk.Entry(paper_row1, width=7)
        self.ent_paper_poll_seconds.insert(0, "3")
        self.ent_paper_poll_seconds.pack(side="left", padx=4)
        ttk.Label(paper_row2, text="开盘前判断秒:").pack(side="left", padx=(4, 4))
        self.ent_paper_decision_lead_seconds = ttk.Entry(paper_row2, width=7)
        self.ent_paper_decision_lead_seconds.insert(0, "120")
        self.ent_paper_decision_lead_seconds.pack(side="left", padx=4)
        ttk.Label(paper_row2, text="模拟轮数:").pack(side="left", padx=(8, 4))
        self.ent_paper_rounds = ttk.Entry(paper_row2, width=7)
        self.ent_paper_rounds.insert(0, "40")
        self.ent_paper_rounds.pack(side="left", padx=4)
        ttk.Label(paper_row2, text="最多小时:").pack(side="left", padx=(8, 4))
        self.ent_paper_max_hours = ttk.Entry(paper_row2, width=7)
        self.ent_paper_max_hours.insert(0, "12")
        self.ent_paper_max_hours.pack(side="left", padx=4)
        self.btn_overnight_paper = ttk.Button(paper_row2, text="通宵参数", width=12, command=self.apply_overnight_paper_defaults)
        self.btn_overnight_paper.pack(side="left", padx=(10, 4))
        self.btn_stop_paper_strategy = ttk.Button(paper_row2, text="停止模拟", width=12, command=self.stop_paper_strategy_clicked, state="disabled")
        self.btn_stop_paper_strategy.pack(side="left", padx=4)

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
        self.btn_paper_strategy = ttk.Button(auto_ctrl_frame, text="模拟自动策略", width=16, command=self.paper_strategy_button_clicked)
        self.btn_paper_strategy.pack(side="left", padx=4)

        paper_result_frame = ttk.LabelFrame(auto_tab, text=" 模拟结果 ", padding=6)
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
            "paper_usdc": self.ent_paper_usdc.get().strip(),
            "paper_take_profit": self.ent_paper_take_profit.get().strip(),
            "paper_poll_seconds": self.ent_paper_poll_seconds.get().strip(),
            "paper_decision_lead_seconds": self.ent_paper_decision_lead_seconds.get().strip(),
            "paper_rounds": self.ent_paper_rounds.get().strip(),
            "paper_max_hours": self.ent_paper_max_hours.get().strip(),
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
            self._set_entry(self.ent_paper_usdc, config.get("paper_usdc", "5"))
            self._set_entry(self.ent_paper_take_profit, config.get("paper_take_profit", "0.60"))
            self._set_entry(self.ent_paper_poll_seconds, config.get("paper_poll_seconds", "3"))
            self._set_entry(self.ent_paper_decision_lead_seconds, config.get("paper_decision_lead_seconds", "120"))
            self._set_entry(self.ent_paper_rounds, config.get("paper_rounds", "40"))
            self._set_entry(self.ent_paper_max_hours, config.get("paper_max_hours", "12"))
        except Exception as e:
            logging.error("加载配置文件失败: %s", e)

    def _set_entry(self, entry, value):
        entry.delete(0, tk.END)
        entry.insert(0, str(value))

    def apply_overnight_paper_defaults(self):
        self._set_entry(self.ent_paper_poll_seconds, "3")
        self._set_entry(self.ent_paper_decision_lead_seconds, "120")
        self._set_entry(self.ent_paper_rounds, "40")
        self._set_entry(self.ent_paper_max_hours, "12")
        self.log_auto(logging.INFO, "已套用通宵模拟参数: 40 轮 / 12 小时 / 轮询 3 秒 / 开盘前 120 秒判断。")

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

    async def fetch_json(self, url: str, params=None):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12), headers={"User-Agent": "Mozilla/5.0"}) as session:
                async with session.get(url, params=params) as response:
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
        match = re.search(r"\{.*\}", cleaned, flags=re.S)
        if match:
            cleaned = match.group(0)
        if not cleaned.startswith("{"):
            raise ValueError("MiniMax 未返回 JSON 对象")
        parsed = json.loads(cleaned)
        prob_up = min(max(float(parsed.get("prob_up", 0.5)), 0.0), 1.0)
        parsed["prob_up"] = prob_up
        parsed["prob_down"] = min(max(float(parsed.get("prob_down", 1.0 - prob_up)), 0.0), 1.0)
        if parsed.get("action") not in {"BUY_UP", "BUY_DOWN", "NO_TRADE"}:
            parsed["action"] = "NO_TRADE"
        if parsed.get("confidence") not in {"LOW", "MEDIUM", "HIGH"}:
            parsed["confidence"] = "LOW"
        return parsed

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

    def paper_strategy_button_clicked(self):
        if self.paper_strategy_running:
            messagebox.showinfo("模拟策略", "模拟自动策略正在运行中。")
            return
        try:
            config = {
                "usdc": float(self.ent_paper_usdc.get().strip()),
                "take_profit": float(self.ent_paper_take_profit.get().strip()),
                "signal_threshold": PAPER_SIGNAL_THRESHOLD,
                "poll_seconds": float(self.ent_paper_poll_seconds.get().strip()),
                "decision_lead_seconds": float(self.ent_paper_decision_lead_seconds.get().strip()),
                "rounds": int(float(self.ent_paper_rounds.get().strip())),
                "max_hours": float(self.ent_paper_max_hours.get().strip()),
            }
        except ValueError:
            messagebox.showerror("参数错误", "模拟策略参数必须是数字。")
            return
        if config["usdc"] <= 0 or not (0 < config["take_profit"] < 1):
            messagebox.showerror("参数错误", "请确认：金额>0，止盈价在 0 到 1 之间。")
            return
        if config["poll_seconds"] < 1:
            messagebox.showerror("参数错误", "轮询秒数不要低于 1 秒。")
            return
        if config["decision_lead_seconds"] < 0 or config["decision_lead_seconds"] > 600:
            messagebox.showerror("参数错误", "开盘前判断秒建议在 0 到 600 秒之间。")
            return
        if config["rounds"] <= 0 or config["rounds"] > 500:
            messagebox.showerror("参数错误", "模拟轮数必须在 1 到 500 之间。")
            return
        if config["max_hours"] <= 0 or config["max_hours"] > 168:
            messagebox.showerror("参数错误", "最多小时必须在 0 到 168 之间。")
            return

        self.paper_strategy_running = True
        self.paper_strategy_stop_requested.clear()
        self.paper_results = []
        self.render_paper_results()
        self.btn_paper_strategy.configure(state="disabled", text="模拟运行中")
        self.btn_stop_paper_strategy.configure(state="normal")
        self.log_auto(
            logging.INFO,
            "启动连续模拟策略: rounds=%s | max_hours=%.2f | next15m | usdc=%.2f | take_profit=%.2f | signal_threshold=%.0f%% | lead=%.0fs",
            config["rounds"],
            config["max_hours"],
            config["usdc"],
            config["take_profit"],
            config["signal_threshold"] * 100,
            config["decision_lead_seconds"],
        )

        def worker():
            self.set_log_category("auto")
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(self.run_paper_strategy_series(config))
                self.root.after(0, lambda result=result: self.log_auto(logging.INFO, "模拟策略结束: %s", result))
            except Exception as e:
                error_text = str(e) or repr(e)
                self.root.after(0, lambda err=error_text: self.log_auto(logging.ERROR, "模拟策略失败: %s", err))
            finally:
                loop.close()
                self.paper_strategy_running = False
                self.root.after(0, lambda: self.btn_paper_strategy.configure(state="normal", text="模拟自动策略"))
                self.root.after(0, lambda: self.btn_stop_paper_strategy.configure(state="disabled"))

        threading.Thread(target=worker, daemon=True).start()

    def stop_paper_strategy_clicked(self):
        if self.paper_strategy_running:
            self.paper_strategy_stop_requested.set()
            self.btn_stop_paper_strategy.configure(state="disabled")
            self.log_auto(logging.WARNING, "已请求停止连续模拟；当前等待/轮询会尽快退出。")

    async def run_paper_strategy_series(self, config):
        started_at = time.time()
        deadline = started_at + config["max_hours"] * 3600
        results = []
        monitor_tasks = []
        seen_slugs = set()
        for round_index in range(1, config["rounds"] + 1):
            if self.paper_strategy_stop_requested.is_set():
                break
            if time.time() >= deadline:
                self.logger.warning("连续模拟达到最多小时限制，停止。")
                break
            try:
                result = await self.run_paper_next_15m_strategy(config, round_index, seen_slugs, deadline)
            except RuntimeError as e:
                if self.paper_strategy_stop_requested.is_set():
                    self.logger.info("连续模拟已停止: %s", e)
                    break
                raise
            results.append(result)
            self.paper_results.append(result)
            self.root.after(0, self.render_paper_results)
            if result.get("entered") and result.get("status") == "OPEN":
                monitor_tasks.append(asyncio.create_task(self.monitor_paper_position(config, result)))
            self.logger.info(
                "连续模拟第 %s/%s 轮入场判断完成: status=%s direction=%s entry=%s slug=%s",
                round_index,
                config["rounds"],
                result.get("status"),
                result.get("direction") or "--",
                "--" if result.get("entry") is None else f"{float(result['entry']):.4f}",
                result.get("slug"),
            )
            await self.sleep_with_stop(2)

        if monitor_tasks:
            self.logger.info("所有入场判断已完成，等待 %s 个模拟持仓监控结束。", len(monitor_tasks))
            monitor_results = await asyncio.gather(*monitor_tasks, return_exceptions=True)
            for monitor_result in monitor_results:
                if isinstance(monitor_result, Exception):
                    self.logger.warning("模拟持仓监控异常: %s", monitor_result)

        summary = self.paper_series_summary(results)
        await self.push_to_server_chan("Polymarket 连续模拟总结", summary)
        return summary.replace("\n", " | ")

    async def run_paper_next_15m_strategy(self, config, round_index=1, seen_slugs=None, deadline=None):
        market = await self.fetch_next_15m_market()
        if not market:
            raise RuntimeError("没有找到下一轮 15m 市场")
        while seen_slugs is not None and market.slug in seen_slugs:
            self.logger.info("已跑过 %s，等待下一轮 15m 市场。", market.slug)
            await self.sleep_with_stop(5)
            market = await self.fetch_next_15m_market()
            if not market:
                raise RuntimeError("没有找到下一轮 15m 市场")
        if seen_slugs is not None:
            seen_slugs.add(market.slug)

        self.logger.info("模拟第 %s 轮目标市场: %s | %s | end=%s", round_index, market.slug, market.question, market.end_dt)
        start_ts = self.market_start_timestamp(market)
        if start_ts:
            decision_ts = start_ts - config["decision_lead_seconds"]
            wait_seconds = decision_ts - time.time()
            if wait_seconds > 0:
                if deadline and time.time() + wait_seconds > deadline:
                    raise RuntimeError("达到最多小时限制，未进入下一轮判断窗口")
                decision_time = datetime.fromtimestamp(decision_ts).astimezone().strftime("%H:%M:%S")
                self.logger.info("模拟等待到开盘前 %.0f 秒再判断: 约 %s，等待 %.0f 秒", config["decision_lead_seconds"], decision_time, wait_seconds)
                await self.sleep_with_stop(wait_seconds)
            else:
                self.logger.info("当前已进入开盘前 %.0f 秒窗口，立即判断。", config["decision_lead_seconds"])
            market = await self.fetch_market_by_slug(market.slug) or market
        if self.paper_strategy_stop_requested.is_set():
            raise RuntimeError("用户停止模拟")

        signal = await self.fetch_btc_signal(market)
        minimax_key = self.ent_minimax_key.get().strip()
        decision = await self.fetch_minimax_prediction(signal, minimax_key, market) if minimax_key else self.local_structured_decision(signal, market, "未配置 MiniMax")
        prob_up = self.normalize_probability(decision.get("prob_up"), signal.get("prob_up", 0.5))
        prob_down = self.normalize_probability(decision.get("prob_down"), 1.0 - prob_up)
        if prob_up >= prob_down:
            direction = "UP"
            action = "BUY_UP"
            prob = prob_up
        else:
            direction = "DOWN"
            action = "BUY_DOWN"
            prob = prob_down

        self.logger.info(
            "模拟入场判断: action=%s prob=%.1f%% threshold=%.1f%% source=%s reason=%s",
            action,
            prob * 100,
            config["signal_threshold"] * 100,
            decision.get("source", "MINIMAX"),
            decision.get("reason", ""),
        )
        if prob <= config["signal_threshold"]:
            result = f"未入场: 单边最高概率 {prob:.2f} 未超过固定阈值 {config['signal_threshold']:.2f}"
            await self.push_paper_strategy_result("未入场", market, decision, None, result)
            return {"round": round_index, "slug": market.slug, "status": "NO_TRADE", "direction": "", "entry": None, "exit": None, "pnl": 0.0, "pnl_pct": 0.0, "entered": False, "result": result}

        market = await self.fetch_market_by_slug(market.slug) or market
        token_id = market.yes_id if direction == "UP" else market.no_id
        entry_book = await self.best_bid_ask_for_token(token_id)
        entry = entry_book["ask"] if entry_book["ask"] is not None else (market.up_ask if direction == "UP" else market.down_ask)
        paper = {
            "direction": direction,
            "token_id": token_id,
            "entry": entry,
            "size": config["usdc"] / entry,
            "notional": config["usdc"],
            "take_profit": config["take_profit"],
            "entered_at": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S"),
            "start_price": None,
            "exit": None,
            "exit_reason": "",
        }
        self.logger.info("模拟买入: %s entry=%.4f size=%.4f notional=%.2f", direction, paper["entry"], paper["size"], paper["notional"])
        return {
            "round": round_index,
            "slug": market.slug,
            "status": "OPEN",
            "direction": direction,
            "entry": paper["entry"],
            "current": paper["entry"],
            "high": paper["entry"],
            "exit": None,
            "pnl": 0.0,
            "pnl_pct": 0.0,
            "entered": True,
            "result": f"OPEN | {direction} entry={paper['entry']:.4f} notional={paper['notional']:.2f} USDC",
            "_market": market,
            "_decision": decision,
            "_paper": paper,
            "_start_ts": start_ts,
        }

    def normalize_probability(self, value, default):
        try:
            prob = float(default if value is None else value)
        except (TypeError, ValueError):
            prob = float(default)
        if prob > 1.0 and prob <= 100.0:
            prob /= 100.0
        return min(max(prob, 0.0), 1.0)

    async def monitor_paper_position(self, config, row):
        market = row["_market"]
        decision = row["_decision"]
        paper = row["_paper"]
        direction = paper["direction"]
        token_id = paper["token_id"]
        start_ts = row.get("_start_ts")
        if start_ts and time.time() < start_ts:
            wait_seconds = min(max(0, start_ts - time.time()), 1200)
            self.logger.info("模拟持仓等待周期开始: %s %.0f 秒", market.slug, wait_seconds)
            await self.sleep_with_stop(wait_seconds)
        if self.paper_strategy_stop_requested.is_set():
            row["status"] = "STOPPED"
            row["result"] = "STOPPED | 用户停止模拟"
            self.root.after(0, self.render_paper_results)
            return row
        paper["start_price"] = await self.fetch_latest_btc_price()

        while True:
            now = datetime.now(timezone.utc)
            latest = await self.fetch_market_by_slug(market.slug)
            if latest:
                market = latest
            book = await self.best_bid_ask_for_token(token_id)
            sell_bid = book["bid"] if book["bid"] is not None else (market.up_bid if direction == "UP" else market.down_bid)
            paper["current"] = sell_bid
            paper["high"] = max(float(paper.get("high") or paper["entry"]), sell_bid)
            row.update({
                "current": paper["current"],
                "high": paper["high"],
            })
            self.root.after(0, self.render_paper_results)
            self.logger.info(
                "模拟监控: %s token=%s sell_bid=%.4f high_bid=%.4f target=%.4f | book_bid=%s book_ask=%s | gamma up %.4f/%.4f down %.4f/%.4f",
                direction,
                token_id[:12],
                sell_bid,
                paper["high"],
                paper["take_profit"],
                "--" if book["bid"] is None else f"{book['bid']:.4f}",
                "--" if book["ask"] is None else f"{book['ask']:.4f}",
                market.up_bid,
                market.up_ask,
                market.down_bid,
                market.down_ask,
            )
            if sell_bid >= paper["take_profit"]:
                paper["exit"] = sell_bid
                paper["exit_reason"] = "TAKE_PROFIT"
                break
            if market.end_dt and now >= market.end_dt:
                end_price = await self.fetch_latest_btc_price()
                up_wins = end_price >= paper["start_price"] if paper["start_price"] else False
                direction_wins = (direction == "UP" and up_wins) or (direction == "DOWN" and not up_wins)
                paper["exit"] = 1.0 if direction_wins else 0.0
                paper["exit_reason"] = "SETTLED_WIN" if direction_wins else "SETTLED_LOSS"
                paper["end_price"] = end_price
                break
            if self.paper_strategy_stop_requested.is_set():
                paper["exit"] = sell_bid
                paper["exit_reason"] = "STOPPED"
                break
            await self.sleep_with_stop(config["poll_seconds"])

        pnl = (paper["exit"] - paper["entry"]) * paper["size"]
        paper["pnl"] = pnl
        paper["pnl_pct"] = pnl / paper["notional"] * 100
        result = f"{paper['exit_reason']} | {direction} entry={paper['entry']:.4f} exit={paper['exit']:.4f} pnl={pnl:+.2f} USDC ({paper['pnl_pct']:+.2f}%)"
        await self.push_paper_strategy_result("已结束", market, decision, paper, result)
        row.update({
            "status": paper["exit_reason"],
            "exit": paper["exit"],
            "pnl": pnl,
            "pnl_pct": paper["pnl_pct"],
            "result": result,
        })
        self.root.after(0, self.render_paper_results)
        return row

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

    async def sleep_with_stop(self, seconds):
        end = time.time() + max(0, seconds)
        while time.time() < end:
            if self.paper_strategy_stop_requested.is_set():
                return
            await asyncio.sleep(min(1.0, end - time.time()))

    def paper_series_summary(self, results):
        entered = [r for r in results if r.get("entered")]
        pnl = sum(float(r.get("pnl", 0.0)) for r in entered)
        tp = sum(1 for r in entered if r.get("status") == "TAKE_PROFIT")
        wins = sum(1 for r in entered if r.get("status") in {"TAKE_PROFIT", "SETTLED_WIN"})
        lines = [
            "### Polymarket 连续模拟总结",
            "",
            f"- 总轮数: `{len(results)}`",
            f"- 入场轮数: `{len(entered)}`",
            f"- 止盈次数: `{tp}`",
            f"- 盈利/胜利次数: `{wins}`",
            f"- 模拟总盈亏: `{pnl:+.2f}` USDC",
        ]
        if entered:
            lines.append("")
            lines.append("最近入场结果:")
            for r in entered[-6:]:
                lines.append(f"- `{r.get('status')}` | `{r.get('slug')}` | `{r.get('pnl', 0):+.2f}` USDC")
        return "\n".join(lines)

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
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.get(url, params=params) as response:
                    if response.status != 200:
                        raise RuntimeError(f"Binance ticker HTTP {response.status}")
                    data = await response.json()
                    return float(data["price"])
        except Exception:
            url = "https://data-api.binance.vision/api/v3/ticker/price"
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.get(url, params=params) as response:
                    if response.status != 200:
                        raise RuntimeError(f"Binance vision ticker HTTP {response.status}")
                    data = await response.json()
                    return float(data["price"])

    async def push_paper_strategy_result(self, status, market, decision, paper, result):
        content = (
            "### Polymarket 模拟自动策略\n\n"
            f"- 状态: `{status}`\n"
            f"- 市场: `{market.slug}`\n"
            f"- 标题: {market.question}\n"
            f"- 建议: `{decision.get('action')}`\n"
            f"- 概率: Up `{decision.get('prob_up', 0) * 100:.1f}%` / Down `{decision.get('prob_down', 0) * 100:.1f}%`\n"
            f"- 来源: `{decision.get('source', 'MINIMAX')}`\n"
            f"- 结果: `{result}`\n"
        )
        if paper:
            content += (
                f"\n- 方向: `{paper['direction']}`\n"
                f"- 入场: `{paper['entry']:.4f}`\n"
                f"- 出场: `{paper['exit']:.4f}`\n"
                f"- 数量: `{paper['size']:.4f}`\n"
                f"- 盈亏: `{paper['pnl']:+.2f}` USDC (`{paper['pnl_pct']:+.2f}%`)\n"
            )
        await self.push_to_server_chan("Polymarket 模拟策略结果", content)

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
        resp = await asyncio.wait_for(
            asyncio.to_thread(
                client.create_and_post_order,
                order_args=OrderArgs(token_id=token_id, price=float(price), size=float(size), side=Side.BUY),
                options=PartialCreateOrderOptions(tick_size=tick_size or market.tick_size),
                order_type=OrderType.GTC,
                post_only=False,
            ),
            timeout=25,
        )
        if isinstance(resp, dict) and resp.get("success") is False:
            raise RuntimeError(f"交易所拒绝订单: {resp}")
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
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12), headers={"User-Agent": "Mozilla/5.0"}) as session:
                async with session.get("https://data-api.polymarket.com/positions", params=params) as response:
                    if response.status != 200:
                        self.logger.warning("持仓接口返回 HTTP %s", response.status)
                        return []
                    data = await response.json()
        except Exception as e:
            self.logger.warning("读取持仓失败: %s", e)
            return []
        return data if isinstance(data, list) else []

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
        resp = await asyncio.wait_for(
            asyncio.to_thread(
                client.create_and_post_order,
                order_args=OrderArgs(token_id=token_id, price=float(price), size=float(size), side=Side.SELL),
                options=PartialCreateOrderOptions(tick_size=tick_size),
                order_type=OrderType.GTC,
                post_only=False,
            ),
            timeout=25,
        )
        if isinstance(resp, dict) and resp.get("success") is False:
            raise RuntimeError(f"交易所拒绝订单: {resp}")
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
    lock_file = open(LOCK_FILE, "w", encoding="utf-8")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
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
