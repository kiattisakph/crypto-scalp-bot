---
inclusion: auto
name: error-handling
description: Error handling patterns for WebSocket, Binance API, and async operations. Use when implementing connection handling or order execution.
---

# Error Handling Rules

## General Principles

- **Never use bare `except:`** — always catch specific exception types
- **Always use `logger.exception()`** for unexpected errors — it includes the full traceback
- **Critical errors must send a Telegram alert** before raising or halting
- **Never let notification failures crash the bot** — Telegram errors are logged and swallowed
- **Never silently swallow errors** — at minimum, log a warning

## WebSocket Reconnection

Follow the exponential backoff policy exactly:

```
Attempt 1: wait 1s
Attempt 2: wait 2s
Attempt 3: wait 4s
Attempt 4: wait 8s
Attempt 5: wait 16s
Attempt 6+: wait 30s (max)
```

### Reconnection Rules

- On disconnect: start reconnection loop with backoff
- On successful reconnect: re-subscribe to ALL previously active streams
- On successful reconnect: send Telegram alert with disconnection duration
- **If disconnected > 60 seconds**: close ALL open positions via OrderManager, send Telegram alert, then continue reconnection attempts
- Reset backoff counter after successful reconnection

### Implementation Pattern

```python
async def _reconnect_loop(self) -> None:
    backoff = 1.0
    max_backoff = 30.0
    disconnect_start = time.monotonic()

    while not self._connected:
        try:
            await self._connect()
            await self._resubscribe_all()
            duration = time.monotonic() - disconnect_start
            await self._telegram.notify_reconnected(duration)
            break
        except Exception:
            logger.exception("websocket | Reconnection failed, retrying in {backoff}s", backoff=backoff)

            elapsed = time.monotonic() - disconnect_start
            if elapsed > 60 and not self._positions_closed:
                await self._close_all_positions()
                self._positions_closed = True

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)
```

## Binance REST API Error Handling

### Retry Policy

- **Max retries**: 3
- **Backoff**: exponential (1s, 2s, 4s)
- **Retryable errors**: network timeouts, HTTP 5xx, HTTP 429 (rate limit)
- **Non-retryable errors**: HTTP 400 (bad request), HTTP 401 (auth), insufficient balance

### Error Patterns

```python
async def _execute_with_retry(self, operation: str, func, *args) -> Any:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return await func(*args)
        except BinanceAPIException as e:
            if e.status_code == 401:
                logger.critical("order | API key invalid — halting bot")
                await self._telegram.notify_risk_halt("API key invalid", "")
                raise
            if e.status_code == 429:
                retry_after = int(e.response.headers.get("Retry-After", attempt))
                logger.warning("order | Rate limited, waiting {s}s", s=retry_after)
                await asyncio.sleep(retry_after)
                continue
            if attempt == MAX_RETRIES:
                logger.error("order | {op} failed after {n} attempts: {e}", op=operation, n=MAX_RETRIES, e=str(e))
                raise
            logger.warning("order | {op} attempt {a}/{n} failed: {e}", op=operation, a=attempt, n=MAX_RETRIES, e=str(e))
            await asyncio.sleep(2 ** (attempt - 1))
        except Exception:
            if attempt == MAX_RETRIES:
                logger.exception("order | {op} failed after {n} attempts", op=operation, n=MAX_RETRIES)
                raise
            await asyncio.sleep(2 ** (attempt - 1))
```

## Telegram Error Handling

- **Never block or crash** on Telegram failures
- Log the failure as a warning and continue
- If Telegram token is invalid at startup, log error but continue bot operation
- Queue messages if rate-limited, drop oldest if queue > 100

```python
async def send(self, message: str) -> None:
    try:
        await self._client.send_message(chat_id=self._chat_id, text=message)
    except Exception:
        logger.warning("telegram | Failed to send alert: {msg}", msg=message[:50])
```

## Database Error Handling

- **Schema migration failure**: log critical error, terminate bot (cannot operate without DB)
- **Disk full**: log critical error, enter halt state (cannot persist trades safely)
- **Database locked**: aiosqlite handles via thread — retry with short delay
- **Corrupt database**: log critical error, terminate bot, require manual intervention

## Configuration Error Handling

- **Missing `.env` or `config.yaml`**: pydantic-settings raises `ValidationError` → log error → `sys.exit(1)`
- **Invalid config values**: pydantic catches these → log specific validation errors → `sys.exit(1)`
- Never start the bot with invalid configuration

## Strategy Error Handling

- **Insufficient candle data**: `SignalEngine` returns `None` → `StrategyOrchestrator` skips evaluation → log debug
- **NaN in indicators**: `SignalEngine` detects NaN → returns `None` → log warning
- **Position size = 0**: `RiskGuard` rejects trade with reason "position size too small"

## Critical Error Escalation

When a critical error occurs, follow this order:
1. Log the error with `logger.critical()` or `logger.exception()`
2. Send Telegram alert (best effort — don't crash if this fails)
3. Enter halt state OR terminate, depending on severity
