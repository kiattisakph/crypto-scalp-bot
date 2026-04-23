"""Direct HTTP and WebSocket client for Binance USDT-M Futures API.

Replaces the ``python-binance`` wrapper with raw ``httpx`` for REST
and ``websockets`` for real-time streams.  All signed requests use
HMAC-SHA256 as documented in the Binance API reference.

**REST base URLs**
- Demo: ``https://demo-fapi.binance.com``
- Live: ``https://fapi.binance.com``

**WebSocket URL**
- ``wss://fstream.binance.com/ws`` (live)
- ``wss://testnet.binancefuture.com/ws`` (demo)
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import ssl
import time
from collections import defaultdict
from typing import Any, AsyncIterator

import certifi
import httpx
from loguru import logger
from websockets.asyncio.client import connect as ws_connect
from websockets.frames import Close

log = logger.bind(component="binance_client")

# Base URLs
_LIVE_BASE = "https://fapi.binance.com"
_DEMO_BASE = "https://demo-fapi.binance.com"
_LIVE_WS = "wss://fstream.binance.com/ws"
_DEMO_WS = "wss://testnet.binancefuture.com/ws"

# WebSocket multiplex sub-URL
_WS_MULTIPLEX = "/stream?streams="

# Time sync tolerance (ms) — Binance rejects requests with
# recvWindow default 5000, but we include timestamp for signing.
_RECV_WINDOW = 5000


class BinanceError(Exception):
    """Base exception for Binance API errors."""

    def __init__(self, status_code: int, code: int, msg: str) -> None:
        super().__init__(f"Binance API error {code} (HTTP {status_code}): {msg}")
        self.status_code = status_code
        self.code = code
        self.msg = msg


class BinanceClient:
    """Low-level REST + WebSocket client for Binance USDT-M Futures.

    This is a drop-in replacement for the ``python-binance`` ``AsyncClient``
    and ``BinanceSocketManager``.  It provides the same method signatures
    that ``OrderManager`` and the stream modules depend on, but calls the
    Binance HTTP API directly with HMAC-SHA256 signing.

    **Usage**::

        client = BinanceClient(api_key, api_secret, demo=True)
        await client.connect()
        price = await client.futures_symbol_ticker(symbol="BTCUSDT")
        await client.close()

    Args:
        api_key: Binance API key.
        api_secret: Binance API secret.
        demo: Use testnet endpoints when ``True``.
    """

    def __init__(self, api_key: str, api_secret: str, demo: bool = False) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._demo = demo
        self._base_url = _DEMO_BASE if demo else _LIVE_BASE
        self._ws_url = _DEMO_WS if demo else _LIVE_WS
        self._http: httpx.AsyncClient | None = None
        self._ws_ssl_context = ssl.create_default_context(cafile=certifi.where())

        # Listen key state (for user-data WebSocket)
        self._listen_key: str | None = None
        self._listen_key_expiry: float = 0.0
        self._listen_key_refresh_task: asyncio.Task[None] | None = None

    async def connect(self) -> None:
        """Initialise the HTTP client pool."""
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(10.0, connect=5.0),
            headers={"Accept": "application/json"},
        )
        mode = "demo" if self._demo else "mainnet"
        log.info("binance | Connected to Binance {mode} (direct HTTP)", mode=mode)

    async def close(self) -> None:
        """Shut down HTTP client and cancel listen-key refresh."""
        if self._listen_key_refresh_task:
            self._listen_key_refresh_task.cancel()
            try:
                await self._listen_key_refresh_task
            except asyncio.CancelledError:
                pass
            self._listen_key_refresh_task = None

        if self._http:
            await self._http.aclose()
            self._http = None
            log.info("binance | HTTP client closed")

    # ------------------------------------------------------------------
    # HMAC-SHA256 signing
    # ------------------------------------------------------------------

    def _sign(self, params: dict[str, Any]) -> dict[str, Any]:
        """Add ``timestamp``, ``recvWindow``, and ``signature`` to params."""
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = _RECV_WINDOW
        query = "&".join(f"{k}={v}" for k, v in params.items())
        signature = hmac.new(
            self._api_secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        params["signature"] = signature
        return params

    def _auth_headers(self) -> dict[str, str]:
        return {"X-MBX-APIKEY": self._api_key}

    # ------------------------------------------------------------------
    # Internal HTTP helpers
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        signed: bool = False,
    ) -> Any:
        """Execute an HTTP request and parse JSON response."""
        assert self._http is not None
        kwargs: dict[str, Any] = {}
        if signed:
            params = self._sign(params or {})
            kwargs["headers"] = self._auth_headers()

        resp = await self._http.request(method, path, params=params, **kwargs)

        if resp.status_code >= 400:
            body = resp.json() if resp.headers.get("content-type", "").startswith(
                "application/json"
            ) else {"code": resp.status_code, "msg": resp.text}
            raise BinanceError(
                status_code=resp.status_code,
                code=body.get("code", resp.status_code),
                msg=body.get("msg", resp.text),
            )
        return resp.json()

    # ------------------------------------------------------------------
    # Public REST (no signature)
    # ------------------------------------------------------------------

    async def futures_ping(self) -> dict:
        """Test connectivity."""
        return await self._request("GET", "/fapi/v1/ping")

    async def futures_symbol_ticker(self, symbol: str) -> dict:
        """Latest price for a symbol."""
        return await self._request("GET", "/fapi/v1/ticker/price", params={"symbol": symbol})

    async def futures_mark_price(self, symbol: str) -> dict:
        """Mark price and funding rate."""
        return await self._request("GET", "/fapi/v1/markPrice", params={"symbol": symbol})

    async def futures_order_book(self, symbol: str, limit: int = 5) -> dict:
        """Order book depth."""
        return await self._request(
            "GET", "/fapi/v1/depth", params={"symbol": symbol, "limit": limit}
        )

    async def futures_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 500,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[list]:
        """Historical klines (candlesticks).

        Returns a list of lists. Each row::

            [
                open_time, open, high, low, close, volume,
                close_time, quote_volume, trades, taker_buy_base,
                taker_buy_quote, ignore
            ]
        """
        params: dict[str, Any] = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        }
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time
        return await self._request("GET", "/fapi/v1/klines", params=params)

    async def futures_exchange_info(self) -> dict:
        """Current exchange trading rules and symbol info."""
        return await self._request("GET", "/fapi/v1/exchangeInfo")

    async def futures_premium_index(self, symbol: str | None = None) -> Any:
        """Premium index / funding rate.

        With no symbol, returns a list for all symbols.
        """
        params: dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol
        return await self._request("GET", "/fapi/v1/premiumIndex", params=params)

    async def futures_ticker_24hr(self, symbol: str | None = None) -> Any:
        """24-hour rolling window ticker.

        With no symbol, returns a list for all symbols.
        """
        params: dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol
        return await self._request("GET", "/fapi/v1/ticker/24hr", params=params)

    async def futures_open_interest(self, symbol: str) -> dict:
        """Current open interest for a symbol."""
        return await self._request(
            "GET", "/fapi/v1/openInterest", params={"symbol": symbol}
        )

    # ------------------------------------------------------------------
    # Signed REST (requires API key + signature)
    # ------------------------------------------------------------------

    async def futures_change_leverage(self, symbol: str, leverage: int) -> dict:
        """Set leverage for a symbol."""
        return await self._request(
            "POST",
            "/fapi/v1/leverage",
            params={"symbol": symbol, "leverage": leverage},
            signed=True,
        )

    async def futures_create_order(self, **params: Any) -> dict:
        """Create a new order.

        Supported order types depend on the ``type`` parameter:
        - ``MARKET``: quantity-based market order
        - ``STOP_MARKET``, ``TAKE_PROFIT_MARKET``: conditional orders
          with ``closePosition=true`` for full position close
        """
        return await self._request(
            "POST", "/fapi/v1/order", params=params, signed=True
        )

    async def futures_cancel_order(self, symbol: str, orderId: int, **params: Any) -> dict:
        """Cancel an existing order."""
        req = {"symbol": symbol, "orderId": orderId, **params}
        return await self._request("DELETE", "/fapi/v1/order", params=req, signed=True)

    async def futures_cancel_all_open_orders(self, symbol: str) -> dict:
        """Cancel all open orders for a symbol."""
        return await self._request(
            "DELETE", "/fapi/v1/allOpenOrders",
            params={"symbol": symbol},
            signed=True,
        )

    async def futures_get_order(self, symbol: str, **params: Any) -> dict:
        """Query order by ``orderId`` or ``origClientOrderId``."""
        return await self._request(
            "GET", "/fapi/v1/order",
            params={"symbol": symbol, **params},
            signed=True,
        )

    async def futures_get_open_orders(self, symbol: str) -> list[dict]:
        """All open orders for a symbol."""
        return await self._request(
            "GET", "/fapi/v1/openOrders",
            params={"symbol": symbol},
            signed=True,
        )

    async def futures_position_information(self) -> list[dict]:
        """Current positions for all symbols."""
        return await self._request(
            "GET", "/fapi/v2/positionRisk", params={}, signed=True
        )

    async def futures_account_balance(self) -> list[dict]:
        """Account balance for all assets."""
        return await self._request(
            "GET", "/fapi/v2/balance", params={}, signed=True
        )

    async def futures_account(self) -> dict:
        """Full account information including positions and margin."""
        return await self._request(
            "GET", "/fapi/v2/account", params={}, signed=True
        )

    # ------------------------------------------------------------------
    # Listen key management (for user-data WebSocket)
    # ------------------------------------------------------------------

    async def _futures_stream_create_listen_key(self) -> str:
        """Create a user-data stream listen key."""
        return await self._request(
            "POST", "/fapi/v1/listenKey", params={}, signed=True
        )

    async def _futures_stream_keepalive_listen_key(self) -> dict:
        """Extend listen key validity."""
        return await self._request(
            "PUT", "/fapi/v1/listenKey", params={}, signed=True
        )

    async def get_listen_key(self) -> str:
        """Get a valid listen key, creating one if needed."""
        if self._listen_key and time.time() < self._listen_key_expiry - 60:
            return self._listen_key

        result = await self._futures_stream_create_listen_key()
        self._listen_key = result.get("listenKey", "")
        # Default validity is 60 minutes; refresh at 30 min
        self._listen_key_expiry = time.time() + 1800

        # Start background refresh
        if not self._listen_key_refresh_task:
            self._listen_key_refresh_task = asyncio.create_task(
                self._refresh_listen_key_loop()
            )

        return self._listen_key

    async def _refresh_listen_key_loop(self) -> None:
        """Periodically extend listen key validity."""
        while True:
            await asyncio.sleep(1800)  # 30 minutes
            try:
                await self._futures_stream_keepalive_listen_key()
                self._listen_key_expiry = time.time() + 1800
                log.debug("binance | Listen key refreshed")
            except Exception:
                log.exception("binance | Failed to refresh listen key")
                # Reset so next call creates a new key
                self._listen_key = None
                self._listen_key_expiry = 0.0
                break

    # ------------------------------------------------------------------
    # WebSocket helpers — raw async generators
    # ------------------------------------------------------------------

    async def ws_connect(
        self,
        streams: list[str],
    ) -> AsyncIterator[dict]:
        """Open a WebSocket connection and yield parsed messages.

        Args:
            streams: List of stream names, e.g. ``["btcusdt@kline_3m"]``
                or ``["!ticker@arr"]``.

        Yields:
            Parsed JSON message dicts.

        Raises:
            Exception: Re-raises on fatal errors; auto-reconnects on
            transient failures.
        """
        url = self._ws_url
        if len(streams) > 1 or (len(streams) == 1 and streams[0] != ""):
            # Multiplex socket: /stream?streams=s1/s2/s3
            url = self._ws_url + _WS_MULTIPLEX + "/".join(streams)

        backoff = 1.0
        max_backoff = 30.0

        while True:
            try:
                async with ws_connect(url, ssl=self._ws_ssl_context) as ws:
                    backoff = 1.0  # reset on successful connect
                    async for raw in ws:
                        msg = json.loads(raw)
                        yield msg
            except asyncio.CancelledError:
                raise
            except (ConnectionClosed, OSError) as exc:
                log.warning(
                    "binance | WebSocket disconnected: {error} — reconnecting in {b}s",
                    error=str(exc),
                    b=backoff,
                )
            except Exception as exc:
                log.warning(
                    "binance | WebSocket error: {error} — reconnecting in {b}s",
                    error=str(exc),
                    b=backoff,
                )

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)


class ConnectionClosed(Exception):
    """WebSocket connection was closed unexpectedly."""

    pass


# ------------------------------------------------------------------
# Reconnecting WebSocket wrapper — replaces BinanceSocketManager's
# ReconnectingWebsocket for use by the stream modules.
# ------------------------------------------------------------------


class ReconnectingWebSocket:
    """Auto-reconnecting WebSocket wrapper.

    This mimics the interface of the ``BinanceSocketManager``
    ``ReconnectingWebsocket`` context manager used by the existing
    stream modules, so the listen loops can be kept mostly unchanged.

    **Usage**::

        rc_ws = ReconnectingWebSocket(client, streams=["!ticker@arr"])
        async with rc_ws as stream:
            while True:
                msg = await stream.recv()
                ...

    The ``recv()`` method yields parsed JSON messages.
    """

    def __init__(self, client: BinanceClient, streams: list[str]) -> None:
        self._client = client
        self._streams = streams
        self._ws: Any = None
        self._connected = False

    async def __aenter__(self) -> "ReconnectingWebSocket":
        self._ws = self._client.ws_connect(self._streams)
        self._connected = True
        return self

    async def __aexit__(self, *args: Any) -> None:
        self._connected = False
        # The async generator is cleaned up when we exit the outer
        # `async with ws_connect` context, so no explicit close needed.
        self._ws = None

    async def recv(self) -> dict:
        """Receive the next parsed JSON message."""
        if self._ws is None:
            raise RuntimeError("WebSocket not connected")
        return await self._ws.__anext__()
