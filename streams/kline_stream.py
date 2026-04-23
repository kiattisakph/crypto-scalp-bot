"""Per-symbol kline WebSocket stream for crypto-scalp-bot.

Dynamically subscribes and unsubscribes to kline futures WebSocket
streams for the configured timeframes based on the active watchlist.
Only closed candles (``x=true``) are forwarded to the
``on_candle_closed`` callback.

The timeframes are read from config (``signal_timeframe`` and
``trend_timeframe``) and passed at construction time -- no hardcoded
interval values.

Includes exponential backoff reconnection logic per symbol with
disconnect timeout handling via async callbacks wired by BotEngine.

Uses direct WebSocket connections via ``core.binance_client`` instead
of the ``python-binance`` wrapper.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

from core.binance_client import BinanceClient, ReconnectingWebSocket
from loguru import logger

log = logger.bind(component="stream")

# Reconnection backoff constants
_INITIAL_BACKOFF = 1.0
_MAX_BACKOFF = 30.0
_DISCONNECT_TIMEOUT = 60.0


class KlineStream:
    """Manages per-symbol kline WebSocket subscriptions.

    Supports dynamic subscribe/unsubscribe for individual symbols.
    Each subscription opens streams for the timeframes provided at
    construction (read from config).  Only closed candles are forwarded
    to the callback.

    Reconnection is handled automatically per symbol with exponential
    backoff when a WebSocket disconnects unexpectedly. Two optional
    callbacks allow BotEngine to wire higher-level behaviour (position
    closing, Telegram alerts) without the stream layer importing
    execution or notification modules.

    Args:
        client: An initialised ``BinanceClient`` instance.
        timeframes: Kline interval strings to subscribe per symbol
            (e.g. ``["3m", "15m"]``).  Sourced from
            ``strategy.signal_timeframe`` and ``strategy.trend_timeframe``
            in config.
    """

    def __init__(self, client: BinanceClient, timeframes: list[str]) -> None:
        if not timeframes:
            raise ValueError("timeframes must contain at least one interval")

        self._client = client
        self._timeframes: list[str] = list(timeframes)
        self._valid_timeframes: set[str] = set(timeframes)

        # symbol -> asyncio.Task running the listen loop for that symbol
        self._tasks: dict[str, asyncio.Task[None]] = {}
        # symbol -> socket context manager (for cleanup)
        self._sockets: dict[str, Any] = {}
        # Track subscribed symbols for potential resubscription
        self._subscribed_symbols: set[str] = set()

        # Callback -- set by BotEngine before subscribing.
        # Signature: (symbol, timeframe, candle_data) -> Awaitable[None]
        self.on_candle_closed: (
            Callable[[str, str, dict], Awaitable[None]] | None
        ) = None

        # Reconnection callbacks -- wired by BotEngine.
        # Called when any symbol disconnected > 60s so BotEngine can close positions.
        self.on_disconnect_timeout: Callable[[], Awaitable[None]] | None = None
        # Called on successful reconnect with disconnection duration in seconds.
        self.on_reconnected: Callable[[float], Awaitable[None]] | None = None
        # Called per-symbol after successful reconnect so BotEngine can
        # backfill missed candles from REST before the stream resumes.
        # Signature: (symbol) -> Awaitable[None]
        self.on_symbol_reconnected: (
            Callable[[str], Awaitable[None]] | None
        ) = None

    async def subscribe(self, symbol: str) -> None:
        """Subscribe to kline streams for a symbol.

        Opens ``{symbol}@kline_{tf}`` WebSocket streams for each
        configured timeframe.  If the symbol is already subscribed,
        this is a no-op.

        Args:
            symbol: Trading pair symbol (e.g. ``"SOLUSDT"``).
        """
        lower = symbol.lower()
        if symbol in self._subscribed_symbols:
            log.debug(
                "KlineStream | Already subscribed to {symbol}, skipping",
                symbol=symbol,
            )
            return

        self._subscribed_symbols.add(symbol)

        streams = [f"{lower}@kline_{tf}" for tf in self._timeframes]
        socket = ReconnectingWebSocket(self._client, streams=streams)
        self._sockets[symbol] = socket
        self._tasks[symbol] = asyncio.create_task(
            self._listen(symbol, socket),
        )
        log.debug(
            "KlineStream | Subscribed to {symbol} kline streams",
            symbol=symbol,
        )

    async def unsubscribe(self, symbol: str) -> None:
        """Unsubscribe from kline streams for a symbol.

        Cancels the listener task and closes the WebSocket connection.
        If the symbol is not currently subscribed, this is a no-op.

        Args:
            symbol: Trading pair symbol (e.g. ``"SOLUSDT"``).
        """
        if symbol not in self._subscribed_symbols:
            log.debug(
                "KlineStream | Not subscribed to {symbol}, skipping unsubscribe",
                symbol=symbol,
            )
            return

        self._subscribed_symbols.discard(symbol)

        task = self._tasks.pop(symbol, None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        socket = self._sockets.pop(symbol, None)
        if socket is not None:
            try:
                await socket.__aexit__(None, None, None)
            except Exception as exc:
                log.warning(
                    "KlineStream | Error closing socket for {symbol}: {error}",
                    symbol=symbol,
                    error=str(exc),
                )

        log.debug(
            "KlineStream | Unsubscribed from {symbol} kline streams",
            symbol=symbol,
        )

    async def disconnect(self) -> None:
        """Disconnect all kline streams.

        Cancels all listener tasks and closes all WebSocket connections.
        """
        symbols = list(self._subscribed_symbols)
        for symbol in symbols:
            await self.unsubscribe(symbol)
        log.debug("KlineStream | All streams disconnected")

    def get_subscribed_symbols(self) -> set[str]:
        """Return the set of currently subscribed symbols.

        Returns:
            A copy of the subscribed symbols set.
        """
        return set(self._subscribed_symbols)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _listen(self, symbol: str, socket: ReconnectingWebSocket) -> None:
        """Read messages from a symbol's kline WebSocket.

        When the loop ends unexpectedly (not due to an explicit
        ``unsubscribe()`` call), a per-symbol reconnection loop is
        started automatically with exponential backoff.

        Args:
            symbol: The trading pair symbol this socket belongs to.
            socket: The ``ReconnectingWebSocket`` context manager.
        """
        try:
            async with socket as stream:
                while symbol in self._subscribed_symbols:
                    try:
                        msg = await stream.recv()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        log.warning(
                            "KlineStream | recv error for {symbol}: {error}",
                            symbol=symbol,
                            error=str(exc),
                        )
                        break

                    await self._handle_message(symbol, msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(
                "KlineStream | Listen loop ended for {symbol}: {error}",
                symbol=symbol,
                error=str(exc),
            )

        # If the symbol is still supposed to be subscribed, the disconnect
        # was unexpected -- start the reconnection loop for this symbol.
        if symbol in self._subscribed_symbols:
            log.warning(
                "KlineStream | Unexpected disconnect for {symbol}, starting reconnection",
                symbol=symbol,
            )
            self._tasks[symbol] = asyncio.create_task(
                self._reconnect_symbol(symbol),
            )

    async def _reconnect_symbol(self, symbol: str) -> None:
        """Attempt to reconnect a single symbol with exponential backoff.

        Backoff schedule: 1s -> 2s -> 4s -> 8s -> 16s -> 30s (max).
        Calls ``on_disconnect_timeout`` once when disconnected > 60s.
        Calls ``on_reconnected`` with duration on successful reconnect.
        Resets backoff after a successful reconnection.

        Args:
            symbol: The trading pair symbol to reconnect.
        """
        backoff = _INITIAL_BACKOFF
        disconnect_start = time.monotonic()
        timeout_fired = False

        while symbol in self._subscribed_symbols:
            # Check disconnect timeout before sleeping.
            elapsed = time.monotonic() - disconnect_start
            if elapsed > _DISCONNECT_TIMEOUT and not timeout_fired:
                timeout_fired = True
                log.warning(
                    "KlineStream | {symbol} disconnected > {timeout}s, triggering timeout callback",
                    symbol=symbol,
                    timeout=_DISCONNECT_TIMEOUT,
                )
                if self.on_disconnect_timeout is not None:
                    try:
                        await self.on_disconnect_timeout()
                    except Exception:
                        log.exception(
                            "KlineStream | on_disconnect_timeout callback failed for {symbol}",
                            symbol=symbol,
                        )

            await asyncio.sleep(backoff)

            try:
                # Clean up old socket.
                old_socket = self._sockets.pop(symbol, None)
                if old_socket is not None:
                    try:
                        await old_socket.__aexit__(None, None, None)
                    except Exception:
                        pass

                lower = symbol.lower()
                streams = [f"{lower}@kline_{tf}" for tf in self._timeframes]
                new_socket = ReconnectingWebSocket(self._client, streams=streams)
                self._sockets[symbol] = new_socket

                duration = time.monotonic() - disconnect_start
                log.debug(
                    "KlineStream | {symbol} reconnected after {duration:.1f}s",
                    symbol=symbol,
                    duration=duration,
                )

                # Notify BotEngine of successful reconnection.
                if self.on_reconnected is not None:
                    try:
                        await self.on_reconnected(duration)
                    except Exception:
                        log.exception(
                            "KlineStream | on_reconnected callback failed for {symbol}",
                            symbol=symbol,
                        )

                # Backfill missed candles before resuming the stream so
                # the CandleBuffer is up-to-date when the listen loop
                # starts forwarding new closed candles.
                if self.on_symbol_reconnected is not None:
                    try:
                        await self.on_symbol_reconnected(symbol)
                    except Exception:
                        log.exception(
                            "KlineStream | on_symbol_reconnected callback failed for {symbol}",
                            symbol=symbol,
                        )

                # Restart the listen loop for this symbol.
                self._tasks[symbol] = asyncio.create_task(
                    self._listen(symbol, new_socket),
                )
                return

            except Exception as exc:
                log.warning(
                    "KlineStream | Reconnection attempt failed for {symbol}: {error} -- retrying in {backoff}s",
                    symbol=symbol,
                    error=str(exc),
                    backoff=backoff,
                )
                backoff = min(backoff * 2, _MAX_BACKOFF)

    async def _handle_message(self, symbol: str, msg: dict) -> None:
        """Parse a kline WebSocket message and forward closed candles.

        The multiplex socket wraps payloads as::

            {"stream": "solusdt@kline_3m", "data": {"e": "kline", "k": {...}}}

        Only candles where ``k["x"]`` is ``True`` (closed) are forwarded.

        Args:
            symbol: The trading pair symbol.
            msg: Raw message dict from the WebSocket.
        """
        try:
            data = msg.get("data")
            if data is None:
                log.warning(
                    "KlineStream | Message missing 'data' field for {symbol}: {msg}",
                    symbol=symbol,
                    msg=msg,
                )
                return

            kline = data.get("k")
            if kline is None:
                log.warning(
                    "KlineStream | Message missing 'k' field for {symbol}",
                    symbol=symbol,
                )
                return

            # Only forward closed candles.
            if not kline.get("x", False):
                return

            timeframe = self._parse_timeframe(kline.get("i", ""))
            if timeframe is None:
                log.warning(
                    "KlineStream | Unknown interval '{interval}' for {symbol}",
                    interval=kline.get("i"),
                    symbol=symbol,
                )
                return

            candle_data = self._parse_candle(kline)

            if self.on_candle_closed is not None:
                await self.on_candle_closed(symbol, timeframe, candle_data)

        except (KeyError, TypeError, ValueError) as exc:
            log.warning(
                "KlineStream | Malformed kline message skipped for {symbol}: {error}",
                symbol=symbol,
                error=str(exc),
            )

    def _parse_timeframe(self, interval: str) -> str | None:
        """Validate a Binance kline interval against configured timeframes.

        Args:
            interval: Binance interval string (e.g. ``"3m"``, ``"15m"``).

        Returns:
            The timeframe string, or ``None`` if not in the configured set.
        """
        return interval if interval in self._valid_timeframes else None

    @staticmethod
    def _parse_candle(kline: dict) -> dict:
        """Extract OHLCV + timestamp from a raw kline dict.

        Args:
            kline: The ``k`` sub-object from a kline WebSocket message.

        Returns:
            Dict with keys: open, high, low, close, volume, timestamp.
        """
        return {
            "open": float(kline["o"]),
            "high": float(kline["h"]),
            "low": float(kline["l"]),
            "close": float(kline["c"]),
            "volume": float(kline["v"]),
            "timestamp": int(kline["t"]),
        }
