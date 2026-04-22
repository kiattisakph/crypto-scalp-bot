"""Binance futures user-data stream for account/order reconciliation.

Listens to the Binance futures user-data WebSocket for ORDER_TRADE_UPDATE
events, enabling reconciliation for exchange-side events that do not
originate from the bot's local exit flow (especially protective stop-loss
orders that fill directly on Binance).

Uses direct WebSocket connections via ``core.binance_client`` instead
of the ``python-binance`` wrapper.  Listen key management (create,
keep-alive) is handled internally.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

from core.binance_client import BinanceClient
from loguru import logger

from core.models import OrderUpdate

log = logger.bind(component="stream")

_INITIAL_BACKOFF = 1.0
_MAX_BACKOFF = 30.0
_DISCONNECT_TIMEOUT = 60.0


class UserDataStream:
    """Listens to Binance futures user-data events.

    The main purpose is reconciliation for exchange-side events that do not
    originate from the bot's local exit flow, especially protective stop-loss
    orders that fill directly on Binance.

    Args:
        client: An initialised ``BinanceClient`` instance.
    """

    def __init__(self, client: BinanceClient) -> None:
        self._client = client
        self._ws_iter: Any = None
        self._listen_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._connected = False

        self.on_order_update: Callable[[OrderUpdate], Awaitable[None]] | None = None
        self.on_disconnect_timeout: Callable[[], Awaitable[None]] | None = None
        self.on_reconnected: Callable[[float], Awaitable[None]] | None = None

    async def connect(self) -> None:
        """Open the futures user-data WebSocket."""
        if self._connected:
            log.warning("UserDataStream | Already connected, skipping")
            return

        listen_key = await self._client.get_listen_key()
        self._ws_iter = self._client.ws_connect(streams=[listen_key])
        self._connected = True
        self._listen_task = asyncio.create_task(self._listen())
        self._listen_task.add_done_callback(self._on_task_done)
        log.info("UserDataStream | Connected to futures user-data stream")

    async def disconnect(self) -> None:
        """Close the user-data WebSocket and cancel background tasks."""
        self._connected = False

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

        self._ws_iter = None
        log.info("UserDataStream | Disconnected")

    @staticmethod
    def _on_task_done(task: asyncio.Task[None]) -> None:
        """Log unhandled exceptions from background stream tasks."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.exception(
                "UserDataStream | Background task failed: {error}",
                error=str(exc),
            )

    async def _listen(self) -> None:
        """Read user-data messages and dispatch parsed order updates."""
        assert self._ws_iter is not None
        try:
            while self._connected:
                try:
                    msg = await self._ws_iter.__anext__()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning(
                        "UserDataStream | recv error: {error}",
                        error=str(exc),
                    )
                    break

                await self._handle_message(msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(
                "UserDataStream | Listen loop ended: {error}",
                error=str(exc),
            )

        if self._connected:
            log.warning("UserDataStream | Unexpected disconnect, starting reconnection")
            self._reconnect_task = asyncio.create_task(self._reconnect_loop())
            self._reconnect_task.add_done_callback(self._on_task_done)

    async def _reconnect_loop(self) -> None:
        """Reconnect with exponential backoff and timeout notification."""
        backoff = _INITIAL_BACKOFF
        disconnect_start = time.monotonic()
        timeout_fired = False

        while self._connected:
            elapsed = time.monotonic() - disconnect_start
            if elapsed > _DISCONNECT_TIMEOUT and not timeout_fired:
                timeout_fired = True
                log.warning(
                    "UserDataStream | Disconnected > {timeout}s, triggering timeout callback",
                    timeout=_DISCONNECT_TIMEOUT,
                )
                if self.on_disconnect_timeout is not None:
                    try:
                        await self.on_disconnect_timeout()
                    except Exception:
                        log.exception("UserDataStream | on_disconnect_timeout callback failed")

            await asyncio.sleep(backoff)

            try:
                # Get a fresh listen key (may create new one if expired)
                listen_key = await self._client.get_listen_key()
                self._ws_iter = self._client.ws_connect(streams=[listen_key])

                duration = time.monotonic() - disconnect_start
                log.info(
                    "UserDataStream | Reconnected after {duration:.1f}s",
                    duration=duration,
                )
                if self.on_reconnected is not None:
                    try:
                        await self.on_reconnected(duration)
                    except Exception:
                        log.exception("UserDataStream | on_reconnected callback failed")

                self._reconnect_task = None
                self._listen_task = asyncio.create_task(self._listen())
                self._listen_task.add_done_callback(self._on_task_done)
                return

            except Exception as exc:
                log.warning(
                    "UserDataStream | Reconnection attempt failed: {error} -- retrying in {backoff}s",
                    error=str(exc),
                    backoff=backoff,
                )
                backoff = min(backoff * 2, _MAX_BACKOFF)

    async def _handle_message(self, msg: dict) -> None:
        """Parse a user-data message and invoke the order callback."""
        data = msg.get("data", msg)
        if not isinstance(data, dict):
            log.warning(
                "UserDataStream | Expected dict message, got {t}",
                t=type(data).__name__,
            )
            return

        if data.get("e") != "ORDER_TRADE_UPDATE":
            return

        try:
            update = self._parse_order_update(data)
        except (KeyError, TypeError, ValueError) as exc:
            log.warning(
                "UserDataStream | Malformed order update skipped: {error}",
                error=str(exc),
            )
            return

        if self.on_order_update is not None:
            await self.on_order_update(update)

    @staticmethod
    def _parse_order_update(msg: dict) -> OrderUpdate:
        """Convert a Binance futures ORDER_TRADE_UPDATE payload."""
        order = msg["o"]
        return OrderUpdate(
            symbol=str(order["s"]),
            order_id=int(order.get("i", 0)),
            client_order_id=str(order.get("c", "")),
            side=str(order.get("S", "")),
            order_type=str(order.get("o", "")),
            status=str(order.get("X", "")),
            execution_type=str(order.get("x", "")),
            avg_price=_float(order.get("ap", 0.0)),
            last_fill_price=_float(order.get("L", 0.0)),
            last_fill_qty=_float(order.get("l", 0.0)),
            cumulative_filled_qty=_float(order.get("z", 0.0)),
            realized_pnl_usdt=_float(order.get("rp", 0.0)),
            reduce_only=_bool(order.get("R", False)),
            close_position=_bool(order.get("cp", False)),
            stop_price=_float(order.get("sp", 0.0)),
            maker_type=str(order.get("mt", "")),
        )


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() == "true"
    return bool(value)
