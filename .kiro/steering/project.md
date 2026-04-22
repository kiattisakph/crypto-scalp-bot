---
inclusion: always
---

# Project: crypto-scalp-bot

## Purpose

Automated trading bot for Binance USDT-M Perpetual Futures using the **Top Gainers Scalping** strategy. Dynamically selects the top 5 symbols with the highest 24h price change, monitors them via WebSocket, and executes scalping trades with multi-signal confirmation.

## Goals

- Profit from momentum of symbols that are breaking out
- Reduce exposure by selecting only symbols with real volume + momentum
- Self-managing 24/7 system on Contabo VPS via Docker

## Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3.11 |
| Exchange SDK | `python-binance` (Binance REST + WebSocket) |
| Indicators | `pandas_ta` |
| Data | `pandas`, `numpy` |
| Database | SQLite via `aiosqlite` |
| Alert | Telegram Bot API |
| Config | `pydantic-settings` + `.env` + `config.yaml` |
| Logging | `loguru` |
| Local dev | Python venv |
| Production | Docker + docker-compose |
| Testing | `pytest` + `pytest-asyncio` + `hypothesis` |

## Project Structure

```
crypto-scalp-bot/
├── main.py                         # Entry point — start bot
├── config.yaml                     # Strategy + risk parameters
├── .env                            # API keys (git-ignored)
├── .env.example                    # Template for setup
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
│
├── core/
│   ├── __init__.py
│   ├── bot.py                      # BotEngine — orchestrate all components
│   ├── config.py                   # Load + validate config (pydantic-settings)
│   └── enums.py                    # Signal, OrderSide, ExitReason enums
│
├── streams/
│   ├── __init__.py
│   ├── ticker_stream.py            # !ticker@arr WebSocket — market overview
│   └── kline_stream.py             # {symbol}@kline_3m/15m WebSocket per symbol
│
├── strategy/
│   ├── __init__.py
│   ├── watchlist_manager.py        # Dynamic top 5 symbol selection + rotation
│   ├── signal_engine.py            # Indicator calculation + entry/exit signals
│   └── top_gainers_scalping.py     # Strategy orchestrator — main logic
│
├── execution/
│   ├── __init__.py
│   ├── order_manager.py            # Place/cancel/modify orders via REST
│   └── position_manager.py         # Track open positions + TP/SL management
│
├── risk/
│   ├── __init__.py
│   └── risk_guard.py               # Portfolio-level guards + halt logic
│
├── storage/
│   ├── __init__.py
│   ├── database.py                 # SQLite connection + migrations
│   └── trade_repository.py         # CRUD for trade history
│
├── notification/
│   ├── __init__.py
│   └── telegram_alert.py           # Telegram Bot alert sender
│
├── utils/
│   ├── __init__.py
│   ├── candle_buffer.py            # Rolling candle buffer per symbol/timeframe
│   └── time_utils.py               # Timezone helpers (UTC)
│
├── tests/
│   ├── conftest.py
│   ├── test_signal_engine.py
│   ├── test_watchlist_manager.py
│   ├── test_risk_guard.py
│   ├── test_candle_buffer.py
│   ├── test_position_manager.py
│   ├── test_trade_repository.py
│   └── properties/                  # Property-based tests (hypothesis)
│       ├── test_watchlist_props.py
│       ├── test_signal_engine_props.py
│       ├── test_position_manager_props.py
│       └── test_risk_guard_props.py
│
├── data/                            # SQLite database (git-ignored)
│   └── trades.db
│
└── logs/                            # Loguru output (git-ignored)
    ├── bot.log
    └── trades.log
```

## Out of Scope (v1)

- No web dashboard / UI
- No backtesting engine
- No multi-exchange support
- No ML-based signals
- No funding rate optimization
- No hedging logic
