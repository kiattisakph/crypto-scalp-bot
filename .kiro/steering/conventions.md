---
inclusion: always
---

# Coding Conventions

## Python Version

- Python 3.11 — use all available syntax features (match/case, ExceptionGroup, etc.)
- All code must be async/await based using `asyncio`

## Naming Conventions

- **Functions and methods**: `snake_case` — e.g., `load_config()`, `check_trade()`, `get_active_symbols()`
- **Classes**: `PascalCase` — e.g., `BotEngine`, `WatchlistManager`, `SignalEngine`
- **Constants**: `UPPER_SNAKE_CASE` — e.g., `MAX_RETRIES`, `DEFAULT_LEVERAGE`
- **Enums**: `PascalCase` class with `UPPER_SNAKE_CASE` members — e.g., `ExitReason.TP1`
- **Private methods/attributes**: prefix with single underscore `_` — e.g., `_reconnect()`, `_is_halted`
- **Module files**: `snake_case.py` — e.g., `signal_engine.py`, `candle_buffer.py`

## Type Hints

- **Required** on all function signatures (parameters and return types)
- Use `from __future__ import annotations` at the top of every module
- Use `list[str]` not `List[str]`, `dict[str, float]` not `Dict[str, float]` (Python 3.11 builtins)
- Use `X | None` not `Optional[X]`
- Use dataclasses or pydantic models for structured data, not raw dicts

## Logging

- **Always use `loguru`** — never use `print()` or stdlib `logging`
- Import as: `from loguru import logger`
- Use structured log messages with pipe-separated context:
  ```python
  logger.info("watchlist | Watchlist updated: {symbols}", symbols=active_symbols)
  logger.error("order | Failed to place order: {symbol} | {error}", symbol=symbol, error=str(e))
  ```
- Log levels:
  - `DEBUG` — indicator values, buffer states, internal decisions
  - `INFO` — trade events, watchlist changes, lifecycle events
  - `WARNING` — recoverable errors, reconnections, skipped signals
  - `ERROR` — API failures, unexpected states
  - `CRITICAL` — halt triggers, unrecoverable errors

## Configuration

- **All config values must come from `config.yaml` or `.env` only**
- Never hardcode trading parameters, API endpoints, thresholds, or timeouts
- Use `pydantic-settings` for `.env` loading and `pydantic.BaseModel` for `config.yaml` validation
- Access config through the validated config objects, never read files directly in business logic

## Async Patterns

- All I/O operations must be `async` — database, HTTP, WebSocket
- Use `asyncio.create_task()` for concurrent operations
- Never use `time.sleep()` — always `await asyncio.sleep()`
- Never use blocking I/O in the event loop — use `aiosqlite`, async HTTP clients
- Use `asyncio.Lock` if shared state needs protection (not threading.Lock)

## Imports

- Group imports: stdlib → third-party → local modules
- Use absolute imports from project root: `from core.config import load_config`
- One import per line for local modules

## Docstrings

- Use Google-style docstrings for public classes and functions
- Include Args, Returns, and Raises sections where applicable

## Error Handling

- Never use bare `except:` — always catch specific exceptions
- Use `logger.exception()` for unexpected errors (includes traceback)
- Let pydantic handle config validation — don't write manual validation
