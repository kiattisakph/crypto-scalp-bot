---
inclusion: auto
name: architecture
description: Component architecture, data flow, and async rules. Use when creating or modifying any component, stream, or strategy file.
---

# Architecture Rules

## Single-Process Async Architecture

- The entire bot runs in **one asyncio event loop** in a single process
- All I/O (WebSocket, REST API, database, Telegram) runs cooperatively via async/await
- No threading, no multiprocessing, no subprocess for business logic
- No blocking calls in the event loop — ever

## Component Layers and Dependencies

Dependencies flow **top-down only**. Lower layers must never import from upper layers.

```
Core Layer (config, enums, bot engine)
    ↓
Streams Layer (ticker_stream, kline_stream)
    ↓
Strategy Layer (watchlist_manager, signal_engine, top_gainers_scalping)
    ↓
Execution Layer (order_manager, position_manager)
    ↓
Risk Layer (risk_guard)
    ↓
Support Layer (candle_buffer, trade_repository, database, telegram_alert, time_utils)
```

### Dependency Rules

- `BotEngine` wires all components together — it is the only component that knows about all others
- `TopGainersScalping` (strategy orchestrator) coordinates strategy → risk → execution flow
- Components communicate via **async callbacks**, not by importing each other directly
- `WatchlistManager` does NOT import `KlineStream` — it emits `on_watchlist_changed` and BotEngine wires the callback
- `PositionManager` does NOT import `TradeRepository` — it emits `on_position_closed` and BotEngine wires the callback

## Data Flow

```
Binance WebSocket
    │
    ├── !ticker@arr ──→ TickerStream ──→ WatchlistManager
    │                                        │
    │                                        ├── on_watchlist_changed ──→ KlineStream (subscribe/unsubscribe)
    │                                        └── on_watchlist_changed ──→ TelegramAlert
    │
    └── {symbol}@kline_3m/15m ──→ KlineStream ──→ CandleBuffer
                                                       │
                                                       └── on_candle_closed ──→ SignalEngine
                                                                                    │
                                                                                    └── Signal ──→ TopGainersScalping
                                                                                                      │
                                                                                                      ├── RiskGuard.check_trade()
                                                                                                      ├── OrderManager.open_position()
                                                                                                      └── PositionManager.open()
                                                                                                              │
                                                                                                              ├── check_exits() on each tick
                                                                                                              ├── OrderManager.close_position()
                                                                                                              ├── TradeRepository (insert/update)
                                                                                                              └── TelegramAlert
```

## WebSocket Stream Responsibilities

| Stream | Component | Purpose |
|---|---|---|
| `!ticker@arr` | `TickerStream` → `WatchlistManager` | Market-wide ticker data for symbol ranking |
| `{symbol}@kline_3m` | `KlineStream` → `CandleBuffer` → `SignalEngine` | 3-minute candles for entry signal indicators |
| `{symbol}@kline_15m` | `KlineStream` → `CandleBuffer` → `SignalEngine` | 15-minute candles for trend filter (EMA 20/50) |

### Stream Lifecycle

- `TickerStream`: connected at bot startup, disconnected at shutdown. Always active.
- `KlineStream`: dynamically subscribes/unsubscribes per symbol based on watchlist changes.
  - Subscribe when symbol enters watchlist
  - Unsubscribe when symbol leaves watchlist AND has no open position
  - Keep subscription alive while symbol has an open position (grace policy)

## Startup Sequence (strict order)

1. Load and validate config (`.env` + `config.yaml`)
2. Initialize database (create tables if needed)
3. Load daily risk state from database
4. Connect `TickerStream` (`!ticker@arr`)
5. Start `TopGainersScalping` strategy loop
6. Send "Bot started" Telegram alert

## Shutdown Sequence (strict order)

1. Receive SIGTERM/SIGINT
2. Close all open positions via `OrderManager`
3. Disconnect `KlineStream` (all symbols)
4. Disconnect `TickerStream`
5. Close database connection
6. Send "Bot stopped" Telegram alert

## State Management

- **In-memory**: open positions, candle buffers, watchlist, cooldown timers, risk state
- **SQLite**: trade history (trades table), daily statistics (daily_stats table)
- On startup, risk state is rehydrated from `daily_stats` table
- No external cache (Redis, etc.) — everything is in-process
