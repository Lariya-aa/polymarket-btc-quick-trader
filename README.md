# Polymarket BTC Quick Trader

Tkinter desktop tool for Polymarket BTC Up/Down short-cycle markets.

## Features

- Scans BTC 5m, 15m, 1h, 4h and 1d Up/Down markets.
- Computes local BTC short-term probability from Binance 1m candles.
- Optionally asks MiniMax for a compact probability and action suggestion.
- Manually confirmed Buy Up / Buy Down orders.
- Paper-runs consecutive next-15m strategy rounds with a configurable pre-open decision window, simulation amount, entry cap, 0.60-style take profit, round count, time limit, stop button, per-round buy/sell/PnL table, and result notification.
- Position refresh and limit-sell flow.
- ServerChan notification for submitted trades, including current position PnL snapshot.

## Setup

Install the Python dependencies used by your environment:

```bash
pip install aiohttp py-clob-client-v2
```

Create `~/.poly_mm_env`:

```bash
cp .env.example ~/.poly_mm_env
chmod 600 ~/.poly_mm_env
open -e ~/.poly_mm_env
```

Fill in the real values in `~/.poly_mm_env`. Do not commit that file.

For Polymarket browser-deposited funds, the common setting is:

```bash
export POLY_SIGNATURE_TYPE=3
export POLY_FUNDER_ADDRESS=your_polymarket_proxy_wallet
```

## Run

```bash
./PolyMarketMaker.command
```

or:

```bash
python3 poly_mm_pro_max.py
```

## Local Config

`poly_config_pro.json` is intentionally ignored because it may contain local wallet addresses and runtime preferences. Use `poly_config_pro.example.json` as the template.

## Safety

The tool does not auto-trade. Buy and sell actions require UI confirmation before submitting real orders.
