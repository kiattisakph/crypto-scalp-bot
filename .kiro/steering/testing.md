---
inclusion: fileMatch
fileMatchPattern: ["tests/**/*.py", "tests/*.py"]
---

# Testing Rules

## Framework

- **Unit tests**: `pytest` + `pytest-asyncio`
- **Property-based tests**: `hypothesis`
- **Mocking**: `unittest.mock` / `pytest-mock`
- **Database tests**: in-memory SQLite via `aiosqlite` with `:memory:` connection

## What to Test

### Must Test (business logic)

| Component | Test File | What to Verify |
|---|---|---|
| `SignalEngine` | `tests/test_signal_engine.py` | Indicator calculation, LONG/SHORT signal conditions, edge cases (NaN, insufficient data) |
| `WatchlistManager` | `tests/test_watchlist_manager.py` | Filter rules (USDT suffix, blacklist, patterns, min change, min volume, min price), top-N sorting, grace policy |
| `RiskGuard` | `tests/test_risk_guard.py` | Position sizing formula, all 4 risk checks, halt trigger, daily state loading |
| `PositionManager` | `tests/test_position_manager.py` | TP1/TP2/TP3/SL level calculation, partial close ratios, breakeven move, trailing stop, force close at max_hold_min |
| `CandleBuffer` | `tests/test_candle_buffer.py` | Rolling buffer size limit, FIFO eviction, DataFrame output format, concurrent access safety |
| `TradeRepository` | `tests/test_trade_repository.py` | Insert open trade, close trade update, daily stats accumulation |
| `Config` | `tests/test_config.py` | Valid config loads correctly, missing fields rejected, invalid types rejected |

### What NOT to Test (mock these)

- **Binance WebSocket connections** — mock `TickerStream` and `KlineStream` to emit fake data
- **Binance REST API calls** — mock `AsyncClient` responses in `OrderManager` tests
- **Telegram Bot API** — mock HTTP calls, verify message format only
- **Docker deployment** — infrastructure, not code
- **Log file rotation** — loguru configuration, not business logic

## Property-Based Tests

Located in `tests/properties/`. Use `hypothesis` with `@settings(max_examples=100)`.

| File | Properties Covered |
|---|---|
| `test_watchlist_props.py` | Filter correctness, top-N sorting, grace policy retention, change diff |
| `test_signal_engine_props.py` | LONG signal generation, SHORT signal generation |
| `test_position_manager_props.py` | TP/SL level calculation, TP1 partial close + breakeven, TP2 partial close, trailing stop, SL full close, time-based force close |
| `test_risk_guard_props.py` | Position size formula, risk approval/rejection, halt trigger |

Each property test must include a tag comment:
```python
# Feature: crypto-scalp-bot, Property 2: Watchlist filter correctness
```

## Test File Location

All tests go in the `tests/` directory at project root:
```
tests/
├── conftest.py                     # Shared fixtures
├── test_config.py
├── test_signal_engine.py
├── test_watchlist_manager.py
├── test_risk_guard.py
├── test_candle_buffer.py
├── test_position_manager.py
├── test_trade_repository.py
├── test_telegram_alert.py
├── test_order_manager.py
├── properties/
│   ├── test_watchlist_props.py
│   ├── test_signal_engine_props.py
│   ├── test_position_manager_props.py
│   └── test_risk_guard_props.py
└── integration/
    ├── test_bot_lifecycle.py
    ├── test_trade_flow.py
    └── test_websocket_reconnect.py
```

## Test Fixtures (conftest.py)

Shared fixtures should include:
- `app_config` — valid `AppConfig` loaded from test config.yaml
- `env_settings` — valid `EnvSettings` with testnet=true
- `mock_binance_client` — mocked `AsyncClient`
- `in_memory_db` — aiosqlite `:memory:` connection with schema created
- `sample_ticker_data` — list of `TickerData` for watchlist tests
- `sample_candles_3m` / `sample_candles_15m` — DataFrames with enough rows for indicator calculation

## Running Tests

```bash
# Run all tests
pytest tests/ -v

# Run specific test file
pytest tests/test_signal_engine.py -v

# Run property-based tests only
pytest tests/properties/ -v

# Run with async support
pytest tests/ -v --asyncio-mode=auto
```

## Test Rules

- Every new component must have a corresponding test file
- Tests must not depend on external services (Binance, Telegram) — always mock
- Database tests use in-memory SQLite, never touch the real database
- Property tests must run with at least 100 examples
- Test names should describe the behavior: `test_long_signal_generated_when_all_conditions_met`
- Use `pytest.mark.asyncio` for all async test functions
