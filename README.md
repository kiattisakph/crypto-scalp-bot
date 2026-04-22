<div align="center">

# crypto-scalp-bot

[![Python](https://img.shields.io/badge/Python-3.11-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![Binance](https://img.shields.io/badge/Binance-Futures-F0B90B?style=for-the-badge&logo=binance&logoColor=white)](https://www.binance.com/)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?style=for-the-badge&logo=docker&logoColor=white)](https://www.docker.com/)
[![Telegram](https://img.shields.io/badge/Telegram-Alerts-26A5E4?style=for-the-badge&logo=telegram&logoColor=white)](https://core.telegram.org/bots)

**Automated scalping bot for Binance USDT-M Perpetual Futures — Top Gainers strategy**

[Features](#features) • [Getting Started](#getting-started) • [Configuration](#configuration) • [Usage](#usage) • [Backtesting](#backtesting) • [Architecture](#architecture) • [Risk Management](#risk-management) • [FAQ](#faq)

</div>

---

## Overview

`crypto-scalp-bot` dynamically selects the **top 5 symbols** with the highest 24h price change on Binance Futures, monitors them via WebSocket in real-time, and executes scalping trades with multi-signal confirmation (EMA crossover + RSI + volume + trend filter).

The bot is designed to run 24/7 on a VPS via Docker with full self-management: automatic watchlist rotation, multi-level take-profit, trailing stops, risk halts, and Telegram alerts.

---

## Official Links

| Resource | URL |
|----------|-----|
| Binance Futures | https://www.binance.com/en/futures |
| API Management | https://www.binance.com/en/my/settings/api-management |
| Binance Futures API Docs | https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info |
| python-binance | https://github.com/sammchardy/python-binance |

---

## Features

<table>
<tr>
<td width="50%">

| Feature | Status |
|---------|:------:|
| Dynamic top-N watchlist (24h gainers) | ✅ |
| Multi-timeframe signal (3m + 15m) | ✅ |
| EMA crossover + RSI + volume confirmation | ✅ |
| 3-level take-profit (TP1/TP2/TP3) | ✅ |
| Trailing stop after TP3 | ✅ |
| SL to breakeven after TP1 | ✅ |
| Force close at max hold time | ✅ |
| Configurable leverage & position sizing | ✅ |
| LONG + SHORT support | ✅ |
| Per-symbol signal cooldown | ✅ |
| Filtered Replay backtest engine | ✅ |
| ADX Market Regime Detection | ✅ |
| Funding Rate Filter | ✅ |

</td>
<td width="50%">

| Feature | Status |
|---------|:------:|
| Binance Futures REST + WebSocket | ✅ |
| Exchange-side order reconciliation | ✅ |
| Daily loss & session drawdown halt | ✅ |
| Free margin check before every trade | ✅ |
| Telegram alerts for all events | ✅ |
| SQLite trade history | ✅ |
| Demo / Mainnet switching via `.env` | ✅ |
| Graceful shutdown (SIGTERM/SIGINT) | ✅ |
| Docker + docker-compose production ready | ✅ |
| Structured logging with loguru | ✅ |

</td>
</tr>
</table>

---

## Getting Started

### Prerequisites

- **Python** 3.11+
- **Binance account** with Futures enabled
- **Binance API key** with Read + Trade + Futures permissions (no withdrawal needed)
- **Telegram bot** (optional, for alerts)
- **Docker** (optional, for production deployment)

### Installation

```bash
git clone <repository-url>
cd crypto-scalp-bot
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Environment Setup

```bash
cp .env.example .env
# Edit .env with your API keys
```

### Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| python-binance | 1.0.19 | Binance REST + WebSocket client |
| pandas | 2.3.2 | OHLCV data handling |
| pandas_ta | 0.4.71b0 | Technical indicators (EMA, RSI) |
| numpy | 2.2.6 | Numerical operations |
| aiosqlite | 0.20.0 | Async SQLite database |
| pydantic-settings | 2.7.1 | Config validation (.env + YAML) |
| pyyaml | 6.0.2 | YAML config parsing |
| loguru | 0.7.3 | Structured logging |
| httpx | 0.28.1 | Async HTTP (Telegram alerts) |

---

## Configuration

### `.env` — API Keys & Secrets

```env
BINANCE_API_KEY=your_api_key_here
BINANCE_API_SECRET=your_api_secret_here
BINANCE_DEMO=false                 # true = demo trading, false = mainnet

TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here

DB_PATH=./data/trades.db
LOG_LEVEL=INFO
```

### `config.yaml` — Strategy & Risk Parameters

```yaml
watchlist:
  top_n: 5                          # Number of symbols to monitor
  min_change_pct_24h: 3.0           # Minimum 24h % change
  min_volume_usdt_24h: 10_000_000   # Minimum 24h volume (USDT)
  refresh_interval_sec: 300         # Re-rank every 5 minutes
  blacklist: ["USDCUSDT", "BUSDUSDT", "BTCDOMUSDT"]
  blacklist_patterns: ["UP", "DOWN"]

strategy:
  signal_timeframe: "3m"
  trend_timeframe: "15m"
  candle_buffer_size: 100

  entry:
    rsi_period: 14
    atr_period: 14                  # ATR period for volatility-based TP/SL
    adx_period: 14                  # ADX period for market regime detection
    adx_trend_threshold: 20.0       # ADX below this = sideways market (skip entry)
    rsi_long_min: 50
    rsi_long_max: 70
    rsi_short_min: 30
    rsi_short_max: 50
    ema_fast: 9
    ema_slow: 21
    ema_trend_fast: 20
    ema_trend_slow: 50
    volume_multiplier: 1.5
    resistance_buffer_pct: 0.3
    signal_cooldown_min: 15

    # Funding rate filter
    max_funding_rate_pct: 0.05             # reject if |funding| > 0.05%
    reject_funding_against_position: true  # skip LONG on positive, SHORT on negative

  exit:
    atr_mode: true                  # ATR-based TP/SL (recommended)
    atr_tp1_mult: 0.8               # TP1 = entry ± ATR × 0.8
    atr_tp2_mult: 1.5               # TP2 = entry ± ATR × 1.5
    atr_tp3_mult: 2.5               # TP3 = entry ± ATR × 2.5
    atr_sl_mult: 1.0                # SL = entry ∓ ATR × 1.0
    atr_trailing_mult: 0.5          # Trailing stop = ATR × 0.5
    # Fallback fixed % (used when atr_mode: false)
    tp1_pct: 0.8
    tp2_pct: 1.5
    tp3_pct: 2.5
    tp1_close_ratio: 0.4            # Close 40% at TP1
    tp2_close_ratio: 0.4            # Close 40% at TP2
    trailing_stop_pct: 0.5
    sl_pct: 1.0
    max_hold_min: 30                # Force close after 30 min

risk:
  risk_per_trade_pct: 1.0           # Risk 1% of balance per trade
  leverage: 5
  max_concurrent_positions: 3
  max_daily_loss_pct: 3.0           # Halt if daily loss exceeds 3%
  max_drawdown_pct: 5.0             # Halt if session drawdown exceeds 5%
  min_free_margin_pct: 30.0         # Require 30% free margin before opening
```

---

## Usage

### Local Development

```bash
source venv/bin/activate
python main.py
```

### Production (Docker)

```bash
# Build and start
docker compose up -d

# View logs
docker compose logs -f bot

# Stop
docker compose down
```

---

## Backtesting

### Filtered Replay Approach

Unlike traditional backtests that run on a fixed set of symbols, this bot uses a **dynamic watchlist** — it selects the top 5 symbols by 24h price change and rotates every 5 minutes. The backtest engine reconstructs this exact behavior using historical data:

```
Phase 1: Fetch 15m klines for ALL USDT symbols
         ↓
         Calculate 24h price change every 5 minutes
         ↓
         Identify every symbol that EVER qualified for the watchlist
         ↓
Phase 2: Fetch 3m klines ONLY for qualifying symbols (saves ~90% data)
         ↓
         Walk timeline chronologically, one 3m candle at a time
         ↓
         Every 5 min → refresh watchlist (top 5 by 24h change)
         ↓
         Check signal ONLY for symbols in the current watchlist
         ↓
         If signal fires → risk check → open position
         ↓
         Every price tick → check TP/SL/trailing/time exit
         ↓
         Collect: trades, PnL, win rate, drawdown, symbol frequency
```

This approach produces backtest results that closely match what the live bot would have experienced — including watchlist rotation, symbol frequency, and signal rejection rates.

### CLI Usage

```bash
# Step 1: Fetch historical data (runs once, then cached)
python3 backtest_main.py \
  --start 2025-01-01 --end 2025-04-01 \
  --fetch-only --data-dir ./bt_data

# Step 2: Run the backtest
python3 backtest_main.py \
  --start 2025-01-01 --end 2025-04-01 \
  --balance 10000 \
  --data-dir ./bt_data

# Optional: verbose mode (log every trade event)
python3 backtest_main.py \
  --start 2025-01-01 --end 2025-04-01 \
  --balance 10000 --verbose \
  --data-dir ./bt_data

# Use Binance testnet data
python3 backtest_main.py \
  --start 2025-01-01 --end 2025-04-01 \
  --demo --balance 10000 \
  --data-dir ./bt_data
```

### CLI Options

| Flag | Description | Default |
|------|-------------|---------|
| `--start` | Start date (YYYY-MM-DD) | *required* |
| `--end` | End date (YYYY-MM-DD) | *required* |
| `--balance` | Initial USDT balance | `10000` |
| `--fetch-only` | Only download data, skip backtest | `false` |
| `--data-dir` | Directory for cached data + reports | `./bt_data` |
| `--demo` | Fetch from Binance testnet | `false` (mainnet) |
| `--config` | Path to config.yaml | `./config.yaml` |
| `--verbose` | Log every trade event | `false` |

### Output Files

After running the backtest, three files are saved to `./bt_data/reports/`:

| File | Contents |
|------|----------|
| `trades_YYYYMMDD_HHMMSS.csv` | Full trade log (symbol, side, entry, exit, PnL, reason) |
| `stats_YYYYMMDD_HHMMSS.json` | Aggregated stats (win rate, profit factor, Sharpe, drawdown, etc.) |
| `equity_YYYYMMDD_HHMMSS.csv` | Equity curve — timestamped balance over time |

### Console Report Example

```
============================================================
  BACKTEST REPORT — Filtered Replay
============================================================

  Period:            2025-01-01 00:00 → 2025-04-01 23:57
  Initial balance:   $   10,000.00
  Final balance:     $   12,345.67
  Net PnL:           $    2,345.67
  Net PnL %:              23.46%

----------------------------------------
  PERFORMANCE
----------------------------------------
  Total trades:                892
  Winning:                     520  (58.3%)
  Losing:                      340
  Break-even:                   32
  Profit factor:                1.34
  Sharpe ratio:                 2.15
  Max drawdown:                  4.20%
  Avg trade PnL %:               0.12%
  Avg win PnL %:                 0.65%
  Avg loss PnL %:               -0.48%

----------------------------------------
  SIGNALS
----------------------------------------
  Total signals:              1247
  Trades executed:             892  (71.5% of signals)
  Rejected (risk):             355
  Watchlist changes:           312

----------------------------------------
  EXIT REASONS
----------------------------------------
  TP1                     280  (31.4%)
  SL                      245  (27.5%)
  TP2                     180  (20.2%)
  TP3                      95  (10.7%)
  TIME                     92  (10.3%)

----------------------------------------
  TOP SYMBOLS (by trade count)
----------------------------------------
  DOGEUSDT          89 trades  PnL $    456.78  WR 62%
  PEPEUSDT          76 trades  PnL $    312.45  WR 55%
  SOLUSDT           62 trades  PnL $    289.10  WR 61%
```

### Data Caching

The backtest engine automatically caches fetched data to avoid re-downloading:

```
bt_data/
├── klines_15m.yaml           # All USDT symbols, 15m candles (cached)
├── klines_3m.yaml            # Qualifying symbols only, 3m candles (cached)
├── qualifying_symbols.txt    # Symbols that ever made the watchlist
└── reports/                  # Backtest output
    ├── trades_*.csv
    ├── stats_*.json
    └── equity_*.csv
```

To re-fetch data for a new date range, delete the cache files and run with `--fetch-only` again.

---

## Architecture

### Component Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                         BotEngine                               │
│              (lifecycle + component wiring)                      │
├──────────┬──────────┬──────────┬──────────┬─────────────────────┤
│ Streams  │ Strategy │Execution │   Risk   │     Storage         │
├──────────┼──────────┼──────────┼──────────┼─────────────────────┤
│ Ticker   │ Watchlist│ Order    │ Risk     │ Database (SQLite)   │
│ Stream   │ Manager  │ Manager  │ Guard    │ Trade Repository    │
│          │          │          │          │                     │
│ Kline    │ Signal   │ Position │          │                     │
│ Stream   │ Engine   │ Manager  │          │                     │
│          │          │          │          │                     │
│ UserData │ TopGainer│          │          │                     │
│ Stream   │ Scalping │          │          │                     │
└──────────┴──────────┴──────────┴──────────┴─────────────────────┘
                                                    │
                                            ┌───────┴───────┐
                                            │   Telegram    │
                                            │    Alerts     │
                                            └───────────────┘
```

### Project Structure

```
crypto-scalp-bot/
├── backtest_main.py                # Backtest CLI entry point
├── main.py                         # Live bot entry point
├── config.yaml                     # Strategy + risk parameters
├── .env                            # API keys (git-ignored)
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
│
├── backtest/
│   ├── data_fetcher.py             # Historical kline fetcher (2-phase)
│   ├── backtest_engine.py          # Filtered Replay engine
│   └── report.py                   # Report generation (CSV/JSON)
│
├── core/
│   ├── bot.py                      # BotEngine — orchestrate all components
│   ├── config.py                   # Load + validate config (pydantic-settings)
│   ├── enums.py                    # Signal, OrderSide, ExitReason enums
│   ├── models.py                   # Shared dataclasses (Position, Signal, etc.)
│   └── logging_setup.py            # Loguru configuration
│
├── streams/
│   ├── ticker_stream.py            # !ticker@arr WebSocket — market overview
│   ├── kline_stream.py             # {symbol}@kline_3m/15m per symbol
│   └── user_data_stream.py         # User data stream — order reconciliation
│
├── strategy/
│   ├── watchlist_manager.py        # Dynamic top-N symbol selection + rotation
│   ├── signal_engine.py            # Indicator calculation + entry signals
│   └── top_gainers_scalping.py     # Strategy orchestrator — main logic
│
├── execution/
│   ├── order_manager.py            # Place/cancel/modify orders via REST
│   └── position_manager.py         # Track open positions + TP/SL management
│
├── risk/
│   └── risk_guard.py               # Portfolio-level guards + halt logic
│
├── storage/
│   ├── database.py                 # SQLite connection + migrations
│   └── trade_repository.py         # CRUD for trade history
│
├── notification/
│   └── telegram_alert.py           # Telegram Bot alert sender
│
├── utils/
│   ├── candle_buffer.py            # Rolling candle buffer per symbol/timeframe
│   └── time_utils.py               # Timezone helpers (UTC)
│
├── tests/                          # pytest + hypothesis
│   └── test_backtest_engine.py     # Backtest engine unit tests
│
├── data/                           # SQLite database (git-ignored)
└── logs/                           # Loguru output (git-ignored)
```

### WebSocket Streams

| Stream | URL | Purpose |
|--------|-----|---------|
| All Tickers | `wss://fstream.binance.com/ws/!ticker@arr` | Watchlist ranking + live prices |
| Kline 3m | `wss://fstream.binance.com/ws/{symbol}@kline_3m` | Entry signal (per symbol) |
| Kline 15m | `wss://fstream.binance.com/ws/{symbol}@kline_15m` | Trend filter (per symbol) |
| User Data | Binance listenKey stream | Order fill reconciliation |

### Signal Logic

**LONG entry** (all conditions must be true):

| Timeframe | Condition |
|-----------|-----------|
| 15m | EMA(20) > EMA(50) — uptrend confirmed |
| 3m | RSI(14) between 50–70 |
| 3m | EMA(9) crossed above EMA(21) within last 2 candles |
| 3m | Volume > 1.5× average (20-period) |
| 3m | Bullish candle (close > open) |
| 3m | Price below resistance buffer |

**SHORT entry** — mirror conditions with inverted thresholds.

### Exit Strategy Flow

```
Position Opened
    │
    ├── Price hits TP1 (0.8%)
    │   ├── Close 40% of position
    │   └── Move SL → entry price (breakeven)
    │
    ├── Price hits TP2 (1.5%)
    │   └── Close 40% of original quantity
    │
    ├── Price hits TP3 (2.5%)
    │   └── Activate trailing stop (0.5% from high/low)
    │       └── Trailing stop triggered → close remaining 20%
    │
    ├── Price hits SL (1.0% or breakeven after TP1)
    │   └── Close entire remaining position
    │
    └── Time exceeds max_hold_min (30 min)
        └── Force close entire remaining position
```

---

## Risk Management

### Position Sizing Formula

```
risk_amount   = balance × risk_per_trade_pct / 100
sl_distance   = entry_price × sl_pct / 100
position_size = risk_amount / sl_distance
```

### Pre-Trade Checks

Every trade must pass **all** of these before opening:

| Check | Condition |
|-------|-----------|
| Daily loss | `daily_loss < max_daily_loss_pct` (3%) |
| Session drawdown | `session_drawdown < max_drawdown_pct` (5%) |
| Open positions | `open_positions < max_concurrent_positions` (3) |
| Free margin | `free_margin_pct ≥ min_free_margin_pct` (30%) |
| Symbol cooldown | No entry on same symbol within `signal_cooldown_min` (15 min) |

If **any** check fails → trade is rejected and the failing condition is logged.

### Risk Parameters Summary

| Parameter | ATR Mode | Fixed Mode | Config Key |
|-----------|----------|------------|------------|
| Risk per trade | 1% of balance | 1% of balance | `risk.risk_per_trade_pct` |
| Leverage | 5× fixed | 5× fixed | `risk.leverage` |
| Stop Loss | ATR × 1.0 | 1.0% from entry | `strategy.exit.atr_sl_mult` / `sl_pct` |
| TP1 | ATR × 0.8 | 0.8% from entry | `strategy.exit.atr_tp1_mult` / `tp1_pct` |
| TP2 | ATR × 1.5 | 1.5% from entry | `strategy.exit.atr_tp2_mult` / `tp2_pct` |
| TP3 | ATR × 2.5 | 2.5% from entry | `strategy.exit.atr_tp3_mult` / `tp3_pct` |
| Trailing stop | ATR × 0.5 | 0.5% from price | `strategy.exit.atr_trailing_mult` / `trailing_stop_pct` |
| TP1 close ratio | 40% | 40% | `strategy.exit.tp1_close_ratio` |
| TP2 close ratio | 40% | 40% | `strategy.exit.tp2_close_ratio` |
| Breakeven move | SL → entry after TP1 | SL → entry after TP1 | Automatic |
| Max hold time | 30 min | 30 min | `strategy.exit.max_hold_min` |
| Max concurrent | 3 positions | 3 positions | `risk.max_concurrent_positions` |
| Daily loss halt | −3% | −3% | `risk.max_daily_loss_pct` |
| Session drawdown halt | −5% | −5% | `risk.max_drawdown_pct` |
| Min free margin | 30% | 30% | `risk.min_free_margin_pct` |

### ATR-Based TP/SL

When `atr_mode: true` (default), TP/SL levels are calculated using the **Average True Range (ATR)** of each symbol at entry time. This automatically adjusts to each coin's volatility:

- **BTC (low volatility, ATR ~0.05%)**: Tight TP/SL — e.g. TP1 at ~0.04%, SL at ~0.05%
- **DOGE (high volatility, ATR ~5%)**: Wide TP/SL — e.g. TP1 at ~4%, SL at ~5%

The ATR is calculated on 3-minute candles using the configured `atr_period` (default 14). Position sizing also uses ATR-based SL distance — wider SL means smaller position size, keeping risk per trade consistent across all coins.

To use fixed percentages instead, set `atr_mode: false` in `config.yaml`.

### Market Regime Detection (ADX)

The bot uses the **Average Directional Index (ADX)** to detect market regime and suppress entries during sideways/choppy markets where trend-following strategies tend to whipsaw.

- **ADX ≥ threshold (default 20)**: Market is trending — signals are allowed
- **ADX < threshold (default 20)**: Market is sideways/choppy — signals are skipped

ADX is calculated on 3-minute candles using the configured `adx_period` (default 14). The check happens *after* a signal fires but *before* any position is opened, so it acts as a final gatekeeper.

This feature works alongside all other signal conditions — it doesn't replace them, it adds an extra layer of protection against entering trades in low-momentum environments.

To disable regime detection, set `adx_trend_threshold: 0` in `config.yaml`.

### Funding Rate Filter

Before opening a position, the bot checks the current **funding rate** via the Binance premium index API. This prevents entering trades where the funding cost would erode profitability:

- **Magnitude filter**: If `|funding_rate| > max_funding_rate_pct`, the trade is rejected. This avoids extreme funding scenarios (common during hype or panic).
- **Direction filter** (default: enabled):
  - **Positive funding** = longs pay shorts → rejects **LONG** entries
  - **Negative funding** = shorts pay longs → rejects **SHORT** entries

This is particularly important for scalping, where even a single funding payment (every 8 hours on Binance) can wipe out the entire profit from a small scalp.

| Setting | Effect |
|---------|--------|
| `max_funding_rate_pct: 0.05` | Reject if |funding| > 0.05% per interval |
| `reject_funding_against_position: true` | Skip trades where funding works against you |
| `reject_funding_against_position: false` | Only check magnitude, ignore direction |

To disable the filter entirely, set `max_funding_rate_pct` to a very high value (e.g., `1.0`).

### Halt Behavior

When daily loss or session drawdown limit is reached:
- Bot **immediately stops** opening new positions
- Existing positions continue to be managed (TP/SL still active)
- Telegram alert sent with `⛔ HALT` message

---

## Telegram Alerts

| Event | Example |
|-------|---------|
| Bot started | `🟢 crypto-scalp-bot started` |
| Bot stopped | `🔴 Bot stopped` |
| Watchlist changed | `📋 Watchlist: +SOLUSDT −LINKUSDT` |
| Position opened | `📈 LONG SOLUSDT @$145.20 │ Size: 0.5 │ SL: $143.75 │ TP1: $146.36` |
| TP1 hit | `✅ TP1 SOLUSDT +0.8% │ PnL: +$4.00` |
| TP2 hit | `✅ TP2 SOLUSDT +1.5% │ PnL: +$7.50` |
| Position closed | `🏁 CLOSED SOLUSDT │ Reason: TP3 │ PnL: +$12.30` |
| SL hit | `🛑 SL SOLUSDT −1.0% │ PnL: −$5.00` |
| Risk halt | `⛔ HALT — Daily loss limit reached (−3%)` |
| Reconnect | `⚠️ WebSocket reconnected after 45s` |

---

## Logging

Uses `loguru` with output to both console and rotating log files.

```
logs/
├── bot.log          # All events — rotation every 10MB, 7 days retention
└── trades.log       # Trade events only
```

**Log format example:**
```
2026-04-22 14:32:01 | INFO | watchlist | Watchlist updated: ['SOLUSDT', 'SUIUSDT', 'PEPEUSDT']
2026-04-22 14:33:15 | INFO | signal | LONG signal: SOLUSDT | RSI=58.3 | EMA9>EMA21 | Vol=2.1x
2026-04-22 14:33:16 | INFO | order | Opened LONG SOLUSDT | entry=145.20 | qty=0.50 | SL=143.75
2026-04-22 14:38:42 | INFO | position | TP1 hit SOLUSDT | exit=146.36 | pnl=+4.00 USDT
```

---

## FAQ

<details>
<summary><b>What API key permissions are needed?</b></summary>

Enable **Read**, **Trade**, and **Futures**. Do **not** enable Withdrawals. Restrict to your server IP if possible.
</details>

<details>
<summary><b>How do I switch to demo trading?</b></summary>

Set `BINANCE_DEMO=true` in your `.env` file. No code changes needed. This uses Binance's Demo Trading mode with virtual funds.
</details>

<details>
<summary><b>Why is system time important?</b></summary>

Binance rejects requests with timestamp drift. Ensure NTP sync: `timedatectl` on Linux or Settings → Time & Language → Sync now on Windows.
</details>

<details>
<summary><b>What happens on WebSocket disconnect?</b></summary>

The bot uses exponential backoff (1s → 2s → 4s → … → 30s max). If disconnected for more than 60 seconds, all open positions are closed as a safety measure, and a Telegram alert is sent.
</details>

<details>
<summary><b>What happens on graceful shutdown?</b></summary>

On SIGTERM/SIGINT, the bot closes all open positions at market price, disconnects all streams, closes the database, and sends a "Bot stopped" Telegram alert before exiting.
</details>

<details>
<summary><b>Can I change the number of monitored symbols?</b></summary>

Yes. Set `watchlist.top_n` in `config.yaml` to any number. The bot will dynamically track that many top gainers.
</details>

<details>
<summary><b>How is position size calculated?</b></summary>

`position_size = (balance × risk_per_trade_pct / 100) / (entry_price × sl_pct / 100)`. This ensures you risk exactly the configured percentage per trade regardless of the symbol's price.
</details>

---

## Disclaimer

<div align="center">

⚠️ **Trading cryptocurrencies and futures involves substantial risk of loss.** ⚠️

This software is provided for educational and research purposes only. Past performance does not guarantee future results. Use stop losses, start with small position sizes, and never invest more than you can afford to lose. The authors are not responsible for any financial losses incurred through use of this bot.

</div>

---

## Version History

### v1.4.0 — Funding Rate Filter

- Pre-trade funding rate check via Binance premium index API
- Magnitude filter: rejects trades when |funding| > threshold
- Direction filter: skips LONG on positive funding, SHORT on negative funding
- Configurable via `max_funding_rate_pct` and `reject_funding_against_position`
- Graceful degradation: if funding rate fetch fails, trade proceeds (safe default)
- 7 new unit tests covering all filter combinations

### v1.3.0 — Market Regime Detection

- ADX-based market regime detection — suppresses entries during sideways/choppy markets
- Configurable via `adx_period` and `adx_trend_threshold` in entry config
- Applied in both live trading and backtest engine for consistent results
- 26 backtest tests covering position exits, risk checks, ATR TP/SL, and ADX regime

### v1.2.0 — ATR-Based TP/SL

- Volatility-adjusted TP/SL using Average True Range (ATR)
- Automatic scaling: tight levels for BTC, wide levels for meme coins
- ATR-based position sizing — wider SL = smaller position, same risk $
- Configurable via `atr_mode`, `atr_tp{n}_mult`, `atr_sl_mult`, `atr_trailing_mult`
- Backward compatible: `atr_mode: false` falls back to fixed % TP/SL

### v1.1.0 — Backtesting Engine

- Filtered Replay backtest engine for dynamic watchlist strategies
- Two-phase data fetcher (15m all-symbols → 3m qualifying-only)
- CLI with data caching, CSV/JSON/Console reports
- Equity curve, daily PnL, Sharpe ratio, max drawdown tracking
- 22 unit tests covering position exits, risk checks, engine logic, and ATR TP/SL

### v1.0.0 — Initial Release

- Top Gainers Scalping strategy with dynamic watchlist
- Multi-timeframe signal engine (3m + 15m)
- 3-level take-profit with trailing stop
- Exchange-side order reconciliation via user data stream
- Risk management: daily loss halt, session drawdown halt, margin checks
- SQLite trade history with full audit trail
- Telegram alerts for all trading events
- Docker production deployment
- Demo/Mainnet switching via environment variable

### Planned (v2.0)

- Web dashboard / monitoring UI
- Multi-exchange support
- Advanced signal models
