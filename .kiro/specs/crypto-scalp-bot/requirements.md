# Requirements Document

## Introduction

crypto-scalp-bot is an automated trading bot for Binance USDT-M Perpetual Futures using a Top Gainers Scalping strategy. The Bot dynamically selects the top 5 symbols with the highest 24-hour price change, monitors them via WebSocket streams, and executes scalping trades with multi-signal confirmation. The system runs 24/7 on a Contabo VPS via Docker, with Telegram alerts for all trading events and risk management safeguards.

Tech stack: Python 3.11, python-binance, pandas_ta, SQLite (aiosqlite), Telegram Bot API, pydantic-settings, loguru, Docker.

## Glossary

- **Bot**: The crypto-scalp-bot automated trading system
- **WatchlistManager**: The component responsible for dynamically selecting and ranking the top N symbols based on 24-hour price change percentage
- **SignalEngine**: The component that calculates technical indicators and generates entry/exit signals using pandas_ta
- **StrategyOrchestrator**: The TopGainersScalping component that coordinates signal evaluation, risk checks, and order placement
- **OrderManager**: The component that places, cancels, and modifies orders via the Binance REST API
- **PositionManager**: The component that tracks open positions in memory and manages TP/SL levels
- **RiskGuard**: The component that enforces portfolio-level risk limits and halt logic
- **CandleBuffer**: A rolling buffer that stores the latest N candles per symbol per timeframe as a pandas DataFrame
- **KlineStream**: The component that dynamically subscribes and unsubscribes to kline WebSocket streams based on the active watchlist
- **TickerStream**: The component that subscribes to the `!ticker@arr` WebSocket for market-wide ticker data
- **TelegramAlert**: The component that sends event notifications to a configured Telegram chat
- **TradeRepository**: The component that performs CRUD operations on the SQLite trades and daily_stats tables
- **BotEngine**: The entry point that initializes all components, wires dependencies, starts WebSocket streams, and handles graceful shutdown
- **Symbol**: A Binance USDT-M Perpetual Futures trading pair ending in "USDT" (e.g., SOLUSDT)
- **Candle**: A single OHLCV (open, high, low, close, volume) data point for a specific timeframe
- **TP1**: Take Profit level 1 at 0.8% from entry price
- **TP2**: Take Profit level 2 at 1.5% from entry price
- **TP3**: Take Profit level 3 at 2.5% from entry price with trailing stop
- **SL**: Stop Loss at 1.0% from entry price
- **Breakeven**: Moving the stop loss to the entry price after TP1 is hit
- **Daily_Loss**: The cumulative realized loss for the current trading day as a percentage of starting balance
- **Session_Drawdown**: The maximum peak-to-trough decline during the current bot session as a percentage
- **Free_Margin**: The percentage of account balance not currently used as margin for open positions
- **Cooldown**: A per-symbol timer that prevents re-entry for a configurable number of minutes after an entry
- **Halt**: A state where the Bot stops opening new positions due to a risk limit breach
- **Grace_Policy**: The rule that a Symbol with an open position is retained in the watchlist even if it no longer qualifies by ranking

## Requirements

### Requirement 1: Configuration Loading and Validation

**User Story:** As a trader, I want the Bot to load and validate all configuration from `.env` and `config.yaml` at startup, so that I can control trading parameters without modifying code.

#### Acceptance Criteria

1. WHEN the Bot starts, THE Bot SHALL load API keys, Telegram credentials, database path, and log level from the `.env` file
2. WHEN the Bot starts, THE Bot SHALL load watchlist, strategy, and risk parameters from the `config.yaml` file
3. THE Bot SHALL validate all configuration values using pydantic-settings schema validation
4. IF a required configuration value is missing or invalid, THEN THE Bot SHALL log the validation error and terminate with a non-zero exit code
5. WHEN the `BINANCE_TESTNET` environment variable is set to `true`, THE Bot SHALL connect to the Binance Testnet API endpoints instead of mainnet endpoints
6. WHEN the `BINANCE_TESTNET` environment variable is set to `false` or is absent, THE Bot SHALL connect to the Binance mainnet API endpoints

### Requirement 2: Bot Lifecycle Management

**User Story:** As a trader, I want the Bot to start up cleanly and shut down gracefully, so that no positions are left unmanaged.

#### Acceptance Criteria

1. WHEN the Bot starts, THE BotEngine SHALL initialize the database, load daily risk state, connect the TickerStream, and start the StrategyOrchestrator in sequence
2. WHEN the Bot receives a SIGTERM or SIGINT signal, THE BotEngine SHALL close all open positions via the OrderManager before disconnecting WebSocket streams and shutting down
3. WHEN the Bot starts successfully, THE TelegramAlert SHALL send a "Bot started" notification
4. WHEN the Bot shuts down, THE TelegramAlert SHALL send a "Bot stopped" notification
5. THE Bot SHALL run as a long-lived process inside a Docker container with `restart: unless-stopped` policy
6. THE Bot SHALL start successfully both in a local Python venv environment and in a Docker container without code changes

### Requirement 3: Dynamic Watchlist Management

**User Story:** As a trader, I want the Bot to automatically select the top gaining symbols, so that I trade only the highest-momentum assets.

#### Acceptance Criteria

1. THE WatchlistManager SHALL subscribe to the `!ticker@arr` WebSocket stream to receive real-time ticker data for all Binance USDT-M Perpetual Futures symbols
2. WHEN filtering symbols, THE WatchlistManager SHALL include only symbols where the symbol name ends with "USDT"
3. WHEN filtering symbols, THE WatchlistManager SHALL exclude symbols that appear in the configured blacklist
4. WHEN filtering symbols, THE WatchlistManager SHALL exclude symbols whose names contain any of the configured blacklist_patterns (e.g., "UP", "DOWN")
5. WHEN filtering symbols, THE WatchlistManager SHALL include only symbols where the 24-hour price change percentage is greater than or equal to the configured `min_change_pct_24h`
6. WHEN filtering symbols, THE WatchlistManager SHALL include only symbols where the 24-hour quote volume in USDT is greater than or equal to the configured `min_volume_usdt_24h`
7. WHEN filtering symbols, THE WatchlistManager SHALL include only symbols where the last price is greater than 0.0001 USDT
8. WHEN the refresh interval elapses, THE WatchlistManager SHALL sort qualifying symbols by 24-hour price change percentage in descending order and select the top N symbols as defined by the `top_n` configuration
9. WHILE a Symbol has an open position, THE WatchlistManager SHALL retain that Symbol in the active watchlist regardless of its current ranking (Grace_Policy)
10. WHEN the watchlist changes, THE WatchlistManager SHALL emit an event containing the list of added symbols and the list of removed symbols
11. WHEN the watchlist changes, THE TelegramAlert SHALL send a notification listing the added and removed symbols

### Requirement 4: Kline Stream Management

**User Story:** As a trader, I want the Bot to subscribe to candlestick data for watched symbols only, so that resources are used efficiently.

#### Acceptance Criteria

1. WHEN a Symbol is added to the watchlist, THE KlineStream SHALL subscribe to `{symbol}@kline_3m` and `{symbol}@kline_15m` WebSocket streams for that Symbol
2. WHEN a Symbol is removed from the watchlist and has no open position, THE KlineStream SHALL unsubscribe from the `{symbol}@kline_3m` and `{symbol}@kline_15m` WebSocket streams for that Symbol
3. WHEN a kline WebSocket message indicates a closed candle (field `x` is `true`), THE KlineStream SHALL forward the closed candle data to the CandleBuffer
4. WHILE a Symbol has an open position, THE KlineStream SHALL maintain the kline subscriptions for that Symbol even if the Symbol is removed from the watchlist

### Requirement 5: Candle Buffer Management

**User Story:** As a trader, I want candle data stored in a rolling buffer, so that the SignalEngine always has sufficient historical data for indicator calculation.

#### Acceptance Criteria

1. THE CandleBuffer SHALL store up to the configured `candle_buffer_size` most recent closed candles per Symbol per timeframe
2. WHEN a new closed candle is added and the buffer is at capacity, THE CandleBuffer SHALL discard the oldest candle for that Symbol and timeframe
3. WHEN the SignalEngine requests candle data, THE CandleBuffer SHALL return a pandas DataFrame with columns: open, high, low, close, volume, timestamp
4. THE CandleBuffer SHALL be safe for concurrent access within an asyncio event loop

### Requirement 6: Entry Signal Generation

**User Story:** As a trader, I want the Bot to generate entry signals based on multi-indicator confirmation, so that trades are only taken when multiple conditions align.

#### Acceptance Criteria

1. WHEN a 3-minute candle closes for a watched Symbol, THE SignalEngine SHALL calculate RSI(14), EMA(9), EMA(21), and 20-period volume moving average on the 3-minute DataFrame
2. WHEN a 15-minute candle closes for a watched Symbol, THE SignalEngine SHALL calculate EMA(20) and EMA(50) on the 15-minute DataFrame
3. WHEN all of the following conditions are true simultaneously, THE SignalEngine SHALL generate a LONG entry signal for the Symbol: (a) 15m EMA_20 is above 15m EMA_50, (b) 3m RSI_14 is between 50 and 70 inclusive, (c) 3m EMA_9 crossed above 3m EMA_21 within the last 2 candles, (d) the latest 3m candle volume exceeds the 20-period volume moving average multiplied by the configured `volume_multiplier`, (e) the latest 3m candle is bullish (close > open), (f) the latest 3m close price is below the nearest resistance level multiplied by (1 - `resistance_buffer_pct` / 100)
4. WHEN all of the following conditions are true simultaneously, THE SignalEngine SHALL generate a SHORT entry signal for the Symbol: (a) 15m EMA_20 is below 15m EMA_50, (b) 3m RSI_14 is between 30 and 50 inclusive, (c) 3m EMA_9 crossed below 3m EMA_21 within the last 2 candles, (d) the latest 3m candle volume exceeds the 20-period volume moving average multiplied by the configured `volume_multiplier`, (e) the latest 3m candle is bearish (close < open), (f) the latest 3m close price is above the nearest support level multiplied by (1 + `resistance_buffer_pct` / 100)
5. WHEN the SignalEngine generates an entry signal, THE SignalEngine SHALL include the signal direction, a confidence score, and a snapshot of all indicator values at the time of signal generation

### Requirement 7: Trade Execution

**User Story:** As a trader, I want the Bot to execute trades automatically when signals are confirmed, so that I do not miss momentum opportunities.

#### Acceptance Criteria

1. WHEN the SignalEngine generates an entry signal and the RiskGuard approves the trade, THE StrategyOrchestrator SHALL instruct the OrderManager to open a position in the signal direction
2. WHEN opening a position, THE OrderManager SHALL set the leverage to the configured `leverage` value for the Symbol before placing the order
3. WHEN opening a position, THE OrderManager SHALL place a market order with the quantity calculated by the RiskGuard
4. IF the Binance API returns an error when placing an order, THEN THE OrderManager SHALL retry the request up to 3 times with exponential backoff before logging the failure
5. WHILE a Symbol has an active Cooldown timer, THE StrategyOrchestrator SHALL suppress entry signals for that Symbol
6. WHEN a position is opened for a Symbol, THE StrategyOrchestrator SHALL start a Cooldown timer of the configured `signal_cooldown_min` minutes for that Symbol

### Requirement 8: Position Management and Exit Strategy

**User Story:** As a trader, I want multi-level take profit and automatic stop loss management, so that profits are locked in progressively while limiting downside.

#### Acceptance Criteria

1. WHEN a position is opened, THE PositionManager SHALL set TP1 at the configured `tp1_pct` percentage from the entry price, TP2 at `tp2_pct`, TP3 at `tp3_pct`, and SL at `sl_pct` from the entry price in the direction opposite to the trade
2. WHEN the current price reaches TP1, THE PositionManager SHALL close the configured `tp1_close_ratio` fraction of the position quantity
3. WHEN TP1 is hit, THE PositionManager SHALL move the stop loss to the entry price (Breakeven)
4. WHEN the current price reaches TP2, THE PositionManager SHALL close the configured `tp2_close_ratio` fraction of the original position quantity
5. WHEN the current price reaches TP3, THE PositionManager SHALL activate a trailing stop at the configured `trailing_stop_pct` percentage from the highest price reached (for LONG) or lowest price reached (for SHORT)
6. WHEN the current price reaches the SL level, THE PositionManager SHALL close the entire remaining position
7. WHEN a position has been open for longer than the configured `max_hold_min` minutes, THE PositionManager SHALL force close the entire remaining position
8. WHEN a position is closed for any reason, THE PositionManager SHALL emit a position closed event containing the trade result, exit reason, and PnL

### Requirement 9: Risk Management

**User Story:** As a trader, I want the Bot to enforce strict risk limits, so that a series of losing trades does not deplete my account.

#### Acceptance Criteria

1. WHEN calculating position size for a new trade, THE RiskGuard SHALL use the formula: risk_amount = balance × (risk_per_trade_pct / 100), sl_distance = entry_price × (sl_pct / 100), position_size = risk_amount / sl_distance
2. WHEN a new trade is requested, THE RiskGuard SHALL approve the trade only if all of the following conditions are met: (a) the current Daily_Loss is less than the configured `max_daily_loss_pct`, (b) the current Session_Drawdown is less than the configured `max_drawdown_pct`, (c) the number of open positions is less than the configured `max_concurrent_positions`, (d) the current Free_Margin percentage is greater than or equal to the configured `min_free_margin_pct`
3. IF any risk check fails, THEN THE RiskGuard SHALL reject the trade and log the specific risk condition that was breached
4. IF the Daily_Loss exceeds the configured `max_daily_loss_pct`, THEN THE RiskGuard SHALL trigger a Halt state, stop the Bot from opening new positions, and instruct the TelegramAlert to send a risk halt notification
5. IF the Session_Drawdown exceeds the configured `max_drawdown_pct`, THEN THE RiskGuard SHALL trigger a Halt state, stop the Bot from opening new positions, and instruct the TelegramAlert to send a risk halt notification
6. WHEN the Bot starts, THE RiskGuard SHALL load the current day's accumulated loss and trade statistics from the daily_stats table in the database

### Requirement 10: Trade Persistence

**User Story:** As a trader, I want every trade recorded in a database, so that I can review performance and debug issues.

#### Acceptance Criteria

1. WHEN a position is opened, THE TradeRepository SHALL insert a new record into the trades table with status "OPEN", including symbol, side, entry_price, quantity, leverage, entry_at, and signal_snapshot as a JSON string
2. WHEN a position is closed, THE TradeRepository SHALL update the corresponding trades record with exit_price, pnl_usdt, pnl_pct, exit_reason, exit_at, and status "CLOSED"
3. THE TradeRepository SHALL store the exit_reason as one of the following values: TP1, TP2, TP3, SL, TIME, REVERSAL, HALT
4. WHEN the Bot starts for the first time, THE TradeRepository SHALL create the trades and daily_stats tables if they do not exist
5. WHEN a trade is closed, THE TradeRepository SHALL update the daily_stats record for the current date with the accumulated total_trades, winning_trades, total_pnl_usdt, and max_drawdown_pct

### Requirement 11: Telegram Notifications

**User Story:** As a trader, I want real-time Telegram alerts for all trading events, so that I can monitor the Bot remotely.

#### Acceptance Criteria

1. THE TelegramAlert SHALL send notifications for the following events: bot started, bot stopped, watchlist changed, position opened, TP1 hit, TP2 hit, position closed, SL hit, risk halt triggered, and WebSocket reconnected
2. WHEN sending a position opened notification, THE TelegramAlert SHALL include the symbol, direction, entry price, position size, stop loss price, and TP1 target price
3. WHEN sending a position closed notification, THE TelegramAlert SHALL include the symbol, exit reason, and PnL in USDT
4. WHEN sending a risk halt notification, THE TelegramAlert SHALL include the specific risk limit that was breached and the current value
5. IF the Telegram API is unreachable, THEN THE TelegramAlert SHALL log the failure and continue Bot operation without interruption

### Requirement 12: WebSocket Reconnection

**User Story:** As a trader, I want the Bot to automatically recover from WebSocket disconnections, so that trading continues without manual intervention.

#### Acceptance Criteria

1. WHEN a WebSocket connection is lost, THE Bot SHALL attempt to reconnect using exponential backoff starting at 1 second, doubling each attempt, up to a maximum interval of 30 seconds
2. WHEN a WebSocket reconnection succeeds, THE Bot SHALL re-subscribe to all previously active streams
3. WHEN a WebSocket reconnection succeeds, THE TelegramAlert SHALL send a reconnection notification including the duration of the disconnection
4. IF a WebSocket connection remains disconnected for longer than 60 seconds, THEN THE Bot SHALL close all open positions via the OrderManager and send a Telegram alert before continuing reconnection attempts

### Requirement 13: Logging

**User Story:** As a trader, I want structured logs for all Bot activity, so that I can diagnose issues and audit behavior.

#### Acceptance Criteria

1. THE Bot SHALL write log output to both the console and a file at `logs/bot.log` using loguru
2. THE Bot SHALL rotate the `logs/bot.log` file when it reaches 10 MB and retain log files for 7 days
3. THE Bot SHALL write trade-specific events to a separate `logs/trades.log` file
4. WHEN logging an event, THE Bot SHALL include the timestamp in UTC, log level, component name, and a descriptive message
5. THE Bot SHALL use the log level specified by the `LOG_LEVEL` environment variable

### Requirement 14: Docker Deployment

**User Story:** As a trader, I want to deploy the Bot via Docker on my VPS, so that it runs reliably with minimal setup.

#### Acceptance Criteria

1. THE Bot SHALL include a Dockerfile based on `python:3.11-slim` that installs dependencies from `requirements.txt` and runs `main.py` as the entrypoint
2. THE Bot SHALL include a `docker-compose.yml` that mounts volumes for the SQLite database directory (`./data`), log directory (`./logs`), and `config.yaml`
3. THE Bot SHALL configure Docker JSON file logging with a maximum size of 10 MB and a maximum of 5 log files
4. WHEN deployed via docker-compose, THE Bot SHALL use the `unless-stopped` restart policy
