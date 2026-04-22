"""Market-wide ticker WebSocket stream for crypto-scalp-bot.

Subscribes to the ``!ticker@arr`` futures WebSocket stream to receive
real-time 24-hour ticker data for all USDT-M Perpetual Futures symbols.
Parsed ticker snapshots are forwarded to a configurable callback.

Includes exponential backoff reconnection logic with disconnect timeout
handling via async callbacks wired by BotEngine.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

from binance import AsyncClient, BinanceSocketManager
from loguru import logger

from core.models import TickerData

log = logger.bind(component="stream")

# Reconnection backoff constants
_INITIAL_BACKOFF = 1.0
_MAX_BACKOFF = 30.0
_DISCONNECT_TIMEOUT = 60.0


class TickerStream:
    """Subscribes to the Binance ``!ticker@arr`` futures WebSocket stream.

    On each message the raw ticker array is parsed into a list of
    :class:`TickerData` objects and forwarded to the ``on_ticker_update``
    callback.

    Reconnection is handled automatically with exponential backoff when
    the WebSocket disconnects unexpectedly. Two optional callbacks allow
    BotEngine to wire higher-level behaviour (position closing, Telegram
    alerts) without the stream layer importing execution or notification
    modules.

    Args:
        client: An initialised ``binance.AsyncClient`` instance.
    """

    def __init__(self, client: AsyncClient) -> None:
        self._client = client
        self._bm: BinanceSocketManager | None = None
        self._socket: Any = None
        self._listen_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._connected: bool = False

        # Callback — set by BotEngine before calling connect().
        self.on_ticker_update: Callable[[list[TickerData]], Awaitable[None]] | None = None

        # Reconnection callbacks — wired by BotEngine.
        # Called when disconnected > 60s so BotEngine can close positions.
        self.on_disconnect_timeout: Callable[[], Awaitable[None]] | None = None
        # Called on successful reconnect with disconnection duration in seconds.
        self.on_reconnected: Callable[[float], Awaitable[None]] | None = None

    async def connect(self) -> None:
        """Open the WebSocket connection to ``!ticker@arr``."""
        if self._connected:
            log.warning("TickerStream | Already connected, skipping")
            return

        self._bm = BinanceSocketManager(self._client)
        self._socket = self._bm.futures_multiplex_socket(
            streams=["!ticker@arr"],
        )
        self._connected = True
        self._listen_task = asyncio.create_task(self._listen())
        self._listen_task.add_done_callback(self._on_task_done)
        log.info("TickerStream | Connected to !ticker@arr")

    async def disconnect(self) -> None:
        """Close the WebSocket connection and cancel all background tasks."""
        self._connected = False

        # Cancel the reconnect task first — it may be waiting on a sleep
        # or trying to re-create the listen task.
        if self._reconnect_task is not None:
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass
            self._reconnect_task = None

        if self._listen_task is not None:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
            self._listen_task = None

        if self._socket is not None:
            try:
                await self._socket.__aexit__(None, None, None)
            except Exception as exc:
                log.warning(
                    "TickerStream | Error closing socket: {error}",
                    error=str(exc),
                )
            self._socket = None

        self._bm = None
        log.info("TickerStream | Disconnected")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _on_task_done(task: asyncio.Task[None]) -> None:
        """Log unhandled exceptions from background tasks.

        Attached via ``add_done_callback`` so that exceptions in the
        listen or reconnect tasks are always surfaced in the logs
        rather than silently swallowed until garbage collection.
        """
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.exception(
                "TickerStream | Background task failed: {error}",
                error=str(exc),
            )

    async def _listen(self) -> None:
        """Read messages from the WebSocket and dispatch to callback.

        When the loop ends unexpectedly (not due to an explicit
        ``disconnect()`` call), a reconnection loop is started
        automatically with exponential backoff.
        """
        try:
            async with self._socket as stream:
                while self._connected:
                    try:
                        msg = await stream.recv()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        log.warning(
                            "TickerStream | recv error: {error}",
                            error=str(exc),
                        )
                        break

                    await self._handle_message(msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(
                "TickerStream | Listen loop ended: {error}",
                error=str(exc),
            )

        # If we're still supposed to be connected, the disconnect was
        # unexpected — start the reconnection loop.
        if self._connected:
            log.warning("TickerStream | Unexpected disconnect, starting reconnection")
            self._reconnect_task = asyncio.create_task(
                self._reconnect_loop()
            )
            self._reconnect_task.add_done_callback(self._on_task_done)

    async def _reconnect_loop(self) -> None:
        """Attempt to reconnect with exponential backoff.

        Backoff schedule: 1s → 2s → 4s → 8s → 16s → 30s (max).
        Calls ``on_disconnect_timeout`` once when disconnected > 60s.
        Calls ``on_reconnected`` with duration on successful reconnect.
        Resets backoff after a successful reconnection.
        """
        backoff = _INITIAL_BACKOFF
        disconnect_start = time.monotonic()
        timeout_fired = False

        while self._connected:
            # Check disconnect timeout before sleeping.
            elapsed = time.monotonic() - disconnect_start
            if elapsed > _DISCONNECT_TIMEOUT and not timeout_fired:
                timeout_fired = True
                log.warning(
                    "TickerStream | Disconnected > {timeout}s, triggering timeout callback",
                    timeout=_DISCONNECT_TIMEOUT,
                )
                if self.on_disconnect_timeout is not None:
                    try:
                        await self.on_disconnect_timeout()
                    except Exception:
                        log.exception("TickerStream | on_disconnect_timeout callback failed")

            await asyncio.sleep(backoff)

            try:
                # Clean up old socket before reconnecting.
                if self._socket is not None:
                    try:
                        await self._socket.__aexit__(None, None, None)
                    except Exception:
                        pass
                    self._socket = None

                self._bm = BinanceSocketManager(self._client)
                self._socket = self._bm.futures_multiplex_socket(
                    streams=["!ticker@arr"],
                )

                duration = time.monotonic() - disconnect_start
                log.info(
                    "TickerStream | Reconnected after {duration:.1f}s",
                    duration=duration,
                )

                # Notify BotEngine of successful reconnection.
                if self.on_reconnected is not None:
                    try:
                        await self.on_reconnected(duration)
                    except Exception:
                        log.exception("TickerStream | on_reconnected callback failed")

                # Restart the listen loop (which will handle further
                # disconnects if they occur).
                self._reconnect_task = None
                self._listen_task = asyncio.create_task(self._listen())
                self._listen_task.add_done_callback(self._on_task_done)
                return

            except Exception as exc:
                log.warning(
                    "TickerStream | Reconnection attempt failed: {error} — retrying in {backoff}s",
                    error=str(exc),
                    backoff=backoff,
                )
                backoff = min(backoff * 2, _MAX_BACKOFF)

    async def _handle_message(self, msg: dict) -> None:
        """Parse a raw WebSocket message and invoke the callback.

        The multiplex socket wraps payloads as::

            {"stream": "!ticker@arr", "data": [ ... ]}

        Each element in ``data`` is a 24-hour ticker object.

        Args:
            msg: Raw message dict from the WebSocket.
        """
        try:
            data = msg.get("data")
            if data is None:
                # Some messages (e.g. error events) lack a data field.
                log.warning(
                    "TickerStream | Message missing 'data' field: {msg}",
                    msg=msg,
                )
                return

            if not isinstance(data, list):
                log.warning(
                    "TickerStream | Expected list in 'data', got {t}",
                    t=type(data).__name__,
                )
                return

            tickers = self._parse_tickers(data)

            if self.on_ticker_update is not None:
                await self.on_ticker_update(tickers)

        except (KeyError, TypeError, ValueError) as exc:
            log.warning(
                "TickerStream | Malformed message skipped: {error}",
                error=str(exc),
            )

    @staticmethod
    def _parse_tickers(raw_tickers: list[dict]) -> list[TickerData]:
        """Convert raw ticker dicts to ``TickerData`` objects.

        Malformed individual ticker entries are skipped with a warning.

        Args:
            raw_tickers: List of raw ticker dicts from the WebSocket.

        Returns:
            Parsed list of ``TickerData`` objects.
        """
        result: list[TickerData] = []
        for t in raw_tickers:
            try:
                result.append(
                    TickerData(
                        symbol=str(t["s"]),
                        price_change_pct=float(t["P"]),
                        last_price=float(t["c"]),
                        quote_volume=float(t["q"]),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                log.warning(
                    "TickerStream | Skipping malformed ticker entry: {error}",
                    error=str(exc),
                )
        return result
