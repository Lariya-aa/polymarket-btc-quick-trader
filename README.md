# Polymarket BTC Quick Trader

Tkinter desktop tool for Polymarket BTC Up/Down short-cycle markets.

## Features

- Scans BTC 5m, 15m, 1h, 4h and 1d Up/Down markets.
- Computes local BTC short-term probability from Binance 1m candles.
- Optionally asks MiniMax for a compact probability and action suggestion.
- Manually confirmed Buy Up / Buy Down orders.
- Backtests and real-time paper-runs a BTC 15m three-red-candle UP reversal strategy with capped martingale sizing.
- Hidden live trading tab for the three-red-candle strategy only; it appears after two confirmations and starting it requires another confirmation.
- Position refresh and limit-sell flow.
- ServerChan notification for submitted trades, including current position PnL snapshot.
- Separate tabs for manual trading and three-red-candle strategy research, each with its own log output.

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

Manual buy and sell actions require UI confirmation before submitting real orders. The three-red-candle live strategy is hidden by default and requires confirmations before it can place real UP buy orders.
