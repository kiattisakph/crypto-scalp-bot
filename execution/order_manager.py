"""Binance REST API wrapper for order execution.

Provides the OrderManager class that handles leverage setting, market order
placement, and retry logic with exponential backoff for Binance USDT-M
Perpetual Futures.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any

from binance import AsyncClient
from binance.exceptions import BinanceAPIException
from loguru import logger

from core.config import EnvSettings
from core.enums import OrderSide, SignalDirection

MAX_RETRIES = 3
FILL_CHECK_RETRIES = 5
FILL_CHECK_DELAY_SEC = 0.2
_TERMINAL_ORDER_STATUSES = {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}


@dataclass
class OrderResult:
    """Result of an order placement on Binance.

    Attributes:
        order_id: Exchange-assigned order ID.
        symbol: The trading pair symbol.
        side: BUY or SELL.
        quantity: Executed quantity.
        status: Order status string from the exchange.
        avg_price: Average fill price (0.0 if not available).
        raw: Full raw response dict from the exchange.
    """

    order_id: int
    symbol: str
    side: str
    quantity: float
    status: str
    avg_price: float = 0.0
    raw: dict | None = None


@dataclass
class ExchangePosition:
    """Open position snapshot loaded from Binance during startup recovery."""

    symbol: str
    side: SignalDirection
    quantity: float
    entry_price: float
    leverage: int
    raw: dict | None = None


class OrderManager:
    """Binance REST API wrapper for futures order execution.

    Handles leverage setting, market order placement for opening and closing
    positions, and automatic retry with exponential backoff on API errors.
    Supports demo/mainnet switching via the ``BINANCE_DEMO`` config flag.

    Args:
        env: Environment settings containing API keys and demo flag.
    """

    def __init__(self, env: EnvSettings) -> None:
        self._env = env
        self._client: AsyncClient | None = None

    async def connect(self) -> None:
        """Create and authenticate the AsyncClient connection.

        Connects to demo or mainnet based on ``BINANCE_DEMO`` config.
        """
        self._client = await AsyncClient.create(
            api_key=self._env.binance_api_key,
            api_secret=self._env.binance_api_secret,
            demo=self._env.binance_demo,
        )
        mode = "demo" if self._env.binance_demo else "mainnet"
        logger.info("order | Connected to Binance {mode}", mode=mode)

    async def close(self) -> None:
        """Close the AsyncClient connection."""
        if self._client:
            await self._client.close_connection()
            self._client = None
            logger.info("order | Disconnected from Binance")

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        """Set the leverage for a symbol with retry logic.

        Args:
            symbol: The trading pair symbol (e.g. "SOLUSDT").
            leverage: Leverage multiplier to set.

        Raises:
            BinanceAPIException: If all retry attempts fail.
        """
        async def _do_set_leverage() -> Any:
            assert self._client is not None
            return await self._client.futures_change_leverage(
                symbol=symbol,
                leverage=leverage,
            )

        await self._execute_with_retry(
            f"set_leverage({symbol}, {leverage}x)",
            _do_set_leverage,
        )
        logger.info(
            "order | Leverage set: {symbol} → {leverage}x",
            symbol=symbol,
            leverage=leverage,
        )

    async def get_symbol_price(self, symbol: str) -> float:
        """Fetch the latest futures symbol price from Binance."""
        async def _do_get_price() -> dict:
            assert self._client is not None
            return await self._client.futures_symbol_ticker(symbol=symbol)

        raw = await self._execute_with_retry(
            f"get_symbol_price({symbol})",
            _do_get_price,
        )
        try:
            price = float(raw.get("price", 0.0))
        except (TypeError, ValueError):
            price = 0.0
        if price <= 0:
            raise ValueError(f"Invalid latest price for {symbol}: {raw!r}")
        return price

    async def get_funding_rate(self, symbol: str) -> float:
        """Fetch the current funding rate for a symbol.

        Returns:
            Funding rate as a decimal (e.g. 0.0001 = 0.01%).
            0.0 if the rate cannot be fetched.
        """
        async def _do_get_funding() -> dict:
            assert self._client is not None
            return await self._client.futures_mark_price(symbol=symbol)

        try:
            raw = await self._execute_with_retry(
                f"get_funding_rate({symbol})",
                _do_get_funding,
            )
            return float(raw.get("lastFundingRate", 0.0))
        except Exception:
            logger.warning(
                "order | Failed to fetch funding rate for {symbol}",
                symbol=symbol,
            )
            return 0.0

    async def get_spread_pct(self, symbol: str) -> float:
        """Fetch the current bid-ask spread as a percentage.

        Returns:
            Spread percentage (e.g. 0.05 means 0.05%).
            0.0 if the spread cannot be determined.
        """
        async def _do_get_depth() -> dict:
            assert self._client is not None
            return await self._client.futures_order_book(
                symbol=symbol,
                limit=5,
            )

        try:
            raw = await self._execute_with_retry(
                f"get_spread_pct({symbol})",
                _do_get_depth,
            )
            bids = raw.get("bids", [])
            asks = raw.get("asks", [])
            if not bids or not asks:
                logger.warning(
                    "order | Empty order book for {symbol}, cannot compute spread",
                    symbol=symbol,
                )
                return 0.0

            best_bid = float(bids[0][0])
            best_ask = float(asks[0][0])
            mid = (best_bid + best_ask) / 2.0
            spread = best_ask - best_bid

            if mid <= 0:
                return 0.0

            spread_pct = (spread / mid) * 100.0
            logger.debug(
                "order | Spread for {symbol}: bid={bid} ask={ask} "
                "mid={mid} spread_pct={sp:.4f}%",
                symbol=symbol,
                bid=best_bid,
                ask=best_ask,
                mid=mid,
                sp=spread_pct,
            )
            return spread_pct
        except Exception:
            logger.warning(
                "order | Failed to fetch order book for {symbol}",
                symbol=symbol,
            )
            return 0.0

    async def open_position(
        self,
        symbol: str,
        side: OrderSide,
        quantity: float,
    ) -> OrderResult:
        """Place a market order to open a position.

        Args:
            symbol: The trading pair symbol.
            side: BUY or SELL.
            quantity: Order quantity.

        Returns:
            OrderResult with fill details.

        Raises:
            BinanceAPIException: If all retry attempts fail.
        """
        raw = await self._create_order_with_retry(
            f"open_position({symbol}, {side.value}, {quantity})",
            symbol,
            {
                "symbol": symbol,
                "side": side.value,
                "type": "MARKET",
                "quantity": quantity,
            },
        )
        raw = await self._resolve_market_order(symbol, raw)
        result = self._parse_order_response(raw)
        logger.info(
            "order | Position opened: {symbol} {side} qty={qty} avg_price={price}",
            symbol=result.symbol,
            side=result.side,
            qty=result.quantity,
            price=result.avg_price,
        )
        return result

    async def close_position(
        self,
        symbol: str,
        side: OrderSide,
        quantity: float,
    ) -> OrderResult:
        """Place a market order to close (reduce) a position.

        The ``side`` should be the closing side — BUY to close a SHORT,
        SELL to close a LONG.

        Args:
            symbol: The trading pair symbol.
            side: BUY or SELL (closing side).
            quantity: Quantity to close.

        Returns:
            OrderResult with fill details.

        Raises:
            BinanceAPIException: If all retry attempts fail.
        """
        raw = await self._create_order_with_retry(
            f"close_position({symbol}, {side.value}, {quantity})",
            symbol,
            {
                "symbol": symbol,
                "side": side.value,
                "type": "MARKET",
                "quantity": quantity,
                "reduceOnly": "true",
            },
        )
        raw = await self._resolve_market_order(symbol, raw)
        result = self._parse_order_response(raw)
        logger.info(
            "order | Position closed: {symbol} {side} qty={qty} avg_price={price}",
            symbol=result.symbol,
            side=result.side,
            qty=result.quantity,
            price=result.avg_price,
        )
        return result

    async def place_stop_loss(
        self,
        symbol: str,
        side: OrderSide,
        stop_price: float,
    ) -> OrderResult:
        """Place an exchange-side stop-loss order for the full position.

        Uses ``STOP_MARKET`` with ``closePosition=true`` so the order protects
        the whole remaining one-way futures position even after partial exits.
        The ``side`` must be the closing side: SELL for LONG, BUY for SHORT.
        """
        raw = await self._create_order_with_retry(
            f"place_stop_loss({symbol}, {side.value}, {stop_price})",
            symbol,
            {
                "symbol": symbol,
                "side": side.value,
                "type": "STOP_MARKET",
                "stopPrice": stop_price,
                "closePosition": "true",
                "workingType": "MARK_PRICE",
            },
        )
        result = self._parse_order_response(raw)
        logger.info(
            "order | Stop loss placed: {symbol} {side} stop={stop} order_id={order_id}",
            symbol=symbol,
            side=side.value,
            stop=stop_price,
            order_id=result.order_id,
        )
        return result

    async def cancel_order(self, symbol: str, order_id: int) -> None:
        """Cancel an existing futures order."""
        async def _do_cancel() -> dict:
            assert self._client is not None
            return await self._client.futures_cancel_order(
                symbol=symbol,
                orderId=order_id,
            )

        await self._execute_with_retry(
            f"cancel_order({symbol}, {order_id})",
            _do_cancel,
        )
        logger.info(
            "order | Cancelled order: {symbol} order_id={order_id}",
            symbol=symbol,
            order_id=order_id,
        )

    async def replace_stop_loss(
        self,
        symbol: str,
        old_order_id: int,
        side: OrderSide,
        stop_price: float,
    ) -> OrderResult:
        """Replace an existing stop-loss order without leaving a gap."""
        new_stop = await self.place_stop_loss(symbol, side, stop_price)
        try:
            await self.cancel_order(symbol, old_order_id)
        except Exception:
            logger.exception(
                "order | Failed to cancel old stop for {symbol} order_id={order_id}",
                symbol=symbol,
                order_id=old_order_id,
            )
        return new_stop

    async def get_open_positions(self) -> list[ExchangePosition]:
        """Fetch non-zero futures positions from Binance."""
        assert self._client is not None
        raw_positions = await self._client.futures_position_information()

        result: list[ExchangePosition] = []
        for raw in raw_positions:
            amount = float(raw.get("positionAmt", 0.0))
            if amount == 0:
                continue

            side = SignalDirection.LONG if amount > 0 else SignalDirection.SHORT
            result.append(
                ExchangePosition(
                    symbol=str(raw.get("symbol", "")),
                    side=side,
                    quantity=abs(amount),
                    entry_price=float(raw.get("entryPrice", 0.0)),
                    leverage=int(float(raw.get("leverage", 0) or 0)),
                    raw=raw,
                )
            )

        return result

    async def get_open_stop_orders(self, symbol: str) -> list[OrderResult]:
        """Return open close-position STOP_MARKET orders for a symbol."""
        assert self._client is not None
        raw_orders = await self._client.futures_get_open_orders(symbol=symbol)

        result: list[OrderResult] = []
        for raw in raw_orders:
            close_position = str(raw.get("closePosition", "")).lower() == "true"
            if raw.get("type") != "STOP_MARKET" or not close_position:
                continue
            result.append(self._parse_order_response(raw))

        return result

    async def get_order(self, symbol: str, order_id: int) -> OrderResult | None:
        """Fetch a single order by ID from Binance.

        Returns:
            OrderResult if found, None on failure.
        """
        assert self._client is not None
        try:
            raw = await self._client.futures_get_order(
                symbol=symbol,
                orderId=order_id,
            )
        except Exception:
            logger.debug(
                "order | Failed to fetch order {order_id} for {symbol}",
                order_id=order_id,
                symbol=symbol,
            )
            return None

        if not raw:
            return None
        return self._parse_order_response(raw)

    async def cancel_all_stop_orders(self, symbol: str) -> int:
        """Cancel every open STOP_MARKET order for *symbol* on the exchange.

        This is a safety-net sweep used when a targeted cancel fails, to
        prevent orphaned stop orders from triggering on a future position.

        Returns:
            Number of orders successfully cancelled.
        """
        try:
            open_stops = await self.get_open_stop_orders(symbol)
        except Exception:
            logger.exception(
                "order | Failed to fetch open stop orders for {symbol}",
                symbol=symbol,
            )
            return 0

        cancelled = 0
        for stop in open_stops:
            try:
                await self.cancel_order(symbol, stop.order_id)
                cancelled += 1
            except Exception:
                logger.warning(
                    "order | Sweep cancel failed for {symbol} order_id={order_id}",
                    symbol=symbol,
                    order_id=stop.order_id,
                )

        if cancelled:
            logger.info(
                "order | Sweep cancelled {count} orphaned stop(s) for {symbol}",
                count=cancelled,
                symbol=symbol,
            )
        return cancelled

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _create_order_with_retry(
        self,
        operation: str,
        symbol: str,
        params: dict[str, Any],
    ) -> dict:
        """Create an order with idempotent retries.

        A stable ``newClientOrderId`` is used across all attempts. If the
        create call times out or returns a retryable server error after Binance
        accepted the order, the retry path first queries by ``origClientOrderId``
        and returns the existing order instead of submitting a duplicate.
        """
        assert self._client is not None
        client_order_id = self._new_client_order_id()
        order_params = dict(params)
        order_params["newClientOrderId"] = client_order_id

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                raw = await self._client.futures_create_order(**order_params)
                raw.setdefault("clientOrderId", client_order_id)
                return raw
            except BinanceAPIException as exc:
                if exc.status_code == 401:
                    logger.critical(
                        "order | API key invalid — cannot continue: {err}",
                        err=str(exc),
                    )
                    raise

                existing = await self._find_order_by_client_id(symbol, client_order_id)
                if existing is not None:
                    existing.setdefault("clientOrderId", client_order_id)
                    logger.warning(
                        "order | {op} recovered existing order after API error | client_order_id={cid}",
                        op=operation,
                        cid=client_order_id,
                    )
                    return existing

                if attempt == MAX_RETRIES:
                    logger.error(
                        "order | {op} failed after {n} attempts: {err}",
                        op=operation,
                        n=MAX_RETRIES,
                        err=str(exc),
                    )
                    raise

                backoff = self._retry_delay(exc, attempt)
                logger.warning(
                    "order | {op} attempt {a}/{n} failed: {err} — retrying in {b}s",
                    op=operation,
                    a=attempt,
                    n=MAX_RETRIES,
                    err=str(exc),
                    b=backoff,
                )
                await asyncio.sleep(backoff)

            except Exception:
                existing = await self._find_order_by_client_id(symbol, client_order_id)
                if existing is not None:
                    existing.setdefault("clientOrderId", client_order_id)
                    logger.warning(
                        "order | {op} recovered existing order after transport error | client_order_id={cid}",
                        op=operation,
                        cid=client_order_id,
                    )
                    return existing

                if attempt == MAX_RETRIES:
                    logger.exception(
                        "order | {op} failed after {n} attempts",
                        op=operation,
                        n=MAX_RETRIES,
                    )
                    raise

                backoff = 2 ** (attempt - 1)
                logger.warning(
                    "order | {op} attempt {a}/{n} unexpected error — retrying in {b}s",
                    op=operation,
                    a=attempt,
                    n=MAX_RETRIES,
                    b=backoff,
                )
                await asyncio.sleep(backoff)

        raise RuntimeError(f"Retry loop exited unexpectedly for {operation}")  # pragma: no cover

    async def _resolve_market_order(self, symbol: str, raw: dict) -> dict:
        """Resolve a MARKET order to actual execution details."""
        current = dict(raw)

        for attempt in range(1, FILL_CHECK_RETRIES + 1):
            result = self._parse_order_response(current)
            status = result.status.upper()

            if status == "FILLED" and result.quantity > 0 and result.avg_price > 0:
                return current

            if status in _TERMINAL_ORDER_STATUSES:
                if result.quantity > 0 and result.avg_price > 0:
                    logger.warning(
                        "order | Terminal non-FILLED market order had partial execution: "
                        "{symbol} status={status} qty={qty} avg_price={price}",
                        symbol=symbol,
                        status=status,
                        qty=result.quantity,
                        price=result.avg_price,
                    )
                    return current
                raise RuntimeError(
                    f"Market order {symbol} ended with status={status} and no fill"
                )

            fetched = await self._fetch_order_status(symbol, current)
            if fetched is not None:
                current = fetched

            if attempt < FILL_CHECK_RETRIES:
                await asyncio.sleep(FILL_CHECK_DELAY_SEC)

        result = self._parse_order_response(current)
        if result.quantity > 0 and result.avg_price > 0:
            logger.warning(
                "order | Market order not fully resolved after fill checks: "
                "{symbol} status={status} qty={qty} avg_price={price}",
                symbol=symbol,
                status=result.status,
                qty=result.quantity,
                price=result.avg_price,
            )
            return current

        raise RuntimeError(
            f"Market order {symbol} did not report an executed quantity after fill checks"
        )

    async def _fetch_order_status(self, symbol: str, raw: dict) -> dict | None:
        """Fetch latest order status using orderId or clientOrderId."""
        assert self._client is not None
        order_id = raw.get("orderId")
        client_order_id = raw.get("clientOrderId") or raw.get("origClientOrderId")

        try:
            if order_id:
                return await self._client.futures_get_order(
                    symbol=symbol,
                    orderId=order_id,
                )
            if client_order_id:
                return await self._client.futures_get_order(
                    symbol=symbol,
                    origClientOrderId=client_order_id,
                )
        except Exception:
            logger.debug(
                "order | Failed to fetch order status for {symbol}",
                symbol=symbol,
            )
            return None

        return None

    async def _find_order_by_client_id(
        self,
        symbol: str,
        client_order_id: str,
    ) -> dict | None:
        """Return an order by client ID, or None when it cannot be found."""
        assert self._client is not None
        try:
            order = await self._client.futures_get_order(
                symbol=symbol,
                origClientOrderId=client_order_id,
            )
        except Exception:
            logger.debug(
                "order | No existing order found for client_order_id={cid}",
                cid=client_order_id,
            )
            return None

        if not order:
            return None
        return order

    @staticmethod
    def _new_client_order_id() -> str:
        """Create a Binance-compatible client order id."""
        return f"csb_{uuid.uuid4().hex[:28]}"

    @staticmethod
    def _retry_delay(exc: BinanceAPIException, attempt: int) -> int | float:
        """Return retry delay for Binance API errors."""
        if exc.status_code != 429:
            return 2 ** (attempt - 1)

        if hasattr(exc, "response") and exc.response is not None:
            try:
                return int(exc.response.headers.get("Retry-After", attempt))
            except (TypeError, ValueError):
                return attempt
        return attempt

    async def _execute_with_retry(
        self,
        operation: str,
        func: Any,
    ) -> Any:
        """Execute an async operation with exponential backoff retry.

        Retries up to ``MAX_RETRIES`` times (3) with backoff of 1s, 2s, 4s.
        Non-retryable errors (HTTP 401 auth failure) are raised immediately.

        Args:
            operation: Human-readable description for logging.
            func: Async callable to execute.

        Returns:
            The result of the successful call.

        Raises:
            BinanceAPIException: On non-retryable errors or after all retries.
            Exception: On unexpected errors after all retries.
        """
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return await func()
            except BinanceAPIException as exc:
                # Auth failure — not retryable
                if exc.status_code == 401:
                    logger.critical(
                        "order | API key invalid — cannot continue: {err}",
                        err=str(exc),
                    )
                    raise

                # Rate limited — respect Retry-After if present
                if exc.status_code == 429:
                    retry_after = attempt  # sensible default
                    if hasattr(exc, "response") and exc.response is not None:
                        retry_after = int(
                            exc.response.headers.get("Retry-After", attempt)
                        )
                    logger.warning(
                        "order | Rate limited on {op}, waiting {s}s",
                        op=operation,
                        s=retry_after,
                    )
                    await asyncio.sleep(retry_after)
                    continue

                if attempt == MAX_RETRIES:
                    logger.error(
                        "order | {op} failed after {n} attempts: {err}",
                        op=operation,
                        n=MAX_RETRIES,
                        err=str(exc),
                    )
                    raise

                backoff = 2 ** (attempt - 1)  # 1s, 2s, 4s
                logger.warning(
                    "order | {op} attempt {a}/{n} failed: {err} — retrying in {b}s",
                    op=operation,
                    a=attempt,
                    n=MAX_RETRIES,
                    err=str(exc),
                    b=backoff,
                )
                await asyncio.sleep(backoff)

            except Exception:
                if attempt == MAX_RETRIES:
                    logger.exception(
                        "order | {op} failed after {n} attempts",
                        op=operation,
                        n=MAX_RETRIES,
                    )
                    raise

                backoff = 2 ** (attempt - 1)
                logger.warning(
                    "order | {op} attempt {a}/{n} unexpected error — retrying in {b}s",
                    op=operation,
                    a=attempt,
                    n=MAX_RETRIES,
                    b=backoff,
                )
                await asyncio.sleep(backoff)

        # Should never reach here, but satisfy type checker
        raise RuntimeError(f"Retry loop exited unexpectedly for {operation}")  # pragma: no cover

    @staticmethod
    def _parse_order_response(raw: dict) -> OrderResult:
        """Parse a Binance futures order response into an OrderResult.

        Args:
            raw: Raw response dict from ``futures_create_order``.

        Returns:
            Parsed OrderResult.
        """
        avg_price = float(raw.get("avgPrice", 0))
        if avg_price == 0 and raw.get("fills"):
            # Calculate weighted average from fills
            total_qty = 0.0
            total_cost = 0.0
            for fill in raw["fills"]:
                qty = float(fill["qty"])
                price = float(fill["price"])
                total_qty += qty
                total_cost += qty * price
            if total_qty > 0:
                avg_price = total_cost / total_qty

        return OrderResult(
            order_id=int(raw.get("orderId", 0)),
            symbol=raw.get("symbol", ""),
            side=raw.get("side", ""),
            quantity=float(raw.get("executedQty", raw.get("origQty", 0))),
            status=raw.get("status", ""),
            avg_price=avg_price,
            raw=raw,
        )
