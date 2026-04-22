"""Position tracking and TP/SL exit management.

Provides the PositionManager class that tracks open positions in memory,
calculates TP/SL levels from config, and evaluates exit conditions on each
price update. Communicates position close events via an async callback.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from loguru import logger

from core.config import ExitConfig
from core.enums import ExitReason, OrderSide, SignalDirection
from core.models import Position, TradeResult
from utils.time_utils import minutes_elapsed


class PositionManager:
    """Tracks open positions in memory and manages TP/SL exit logic.

    Calculates TP1/TP2/TP3/SL levels at position open using config-driven
    percentages. On each price tick, evaluates exit conditions in priority
    order: SL → TP1 → TP2 → TP3 → trailing stop → time-based force close.

    Args:
        exit_config: Exit strategy parameters (TP/SL percentages, ratios,
            trailing stop, max hold time).
        close_order_fn: Async callable to execute a closing order on the
            exchange. Signature: ``(symbol, side, quantity) -> Any``.
    """

    def __init__(
        self,
        exit_config: ExitConfig,
        close_order_fn: Callable[[str, OrderSide, float], Awaitable[Any]],
        replace_stop_order_fn: (
            Callable[[str, int, OrderSide, float], Awaitable[Any]] | None
        ) = None,
        cancel_order_fn: Callable[[str, int], Awaitable[Any]] | None = None,
        cancel_all_stops_fn: Callable[[str], Awaitable[int]] | None = None,
    ) -> None:
        self._exit_config = exit_config
        self._close_order_fn = close_order_fn
        self._replace_stop_order_fn = replace_stop_order_fn
        self._cancel_order_fn = cancel_order_fn
        self._cancel_all_stops_fn = cancel_all_stops_fn
        self._positions: dict[str, Position] = {}
        self.on_position_closed: Callable[[TradeResult], Awaitable[None]] | None = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def open(
        self,
        symbol: str,
        side: SignalDirection,
        entry_price: float,
        quantity: float,
        leverage: int,
        atr_value: float | None = None,
    ) -> Position:
        """Open a new position and calculate TP/SL levels.

        TP/SL levels are calculated using one of two modes:
        - **ATR-based** (when *atr_value* is provided and ``atr_mode`` is True):
          TP = atr_value × atr_tp{n}_mult, SL = atr_value × atr_sl_mult
        - **Fixed-percentage** (fallback):
          TP = entry × (1 ± tp_pct / 100), SL = entry × (1 ∓ sl_pct / 100)

        Args:
            symbol: The trading pair symbol (e.g. "SOLUSDT").
            side: LONG or SHORT direction.
            entry_price: Price at which the position was opened.
            quantity: Position quantity.
            leverage: Leverage multiplier applied.
            atr_value: Average True Range at entry time. When provided,
                TP/SL levels are set using ATR multipliers.

        Returns:
            The newly created Position with calculated TP/SL levels.
        """
        cfg = self._exit_config

        if atr_value is not None and cfg.atr_mode:
            # ATR-based TP/SL
            tp1 = entry_price + atr_value * cfg.atr_tp1_mult * (1 if side == SignalDirection.LONG else -1)
            tp2 = entry_price + atr_value * cfg.atr_tp2_mult * (1 if side == SignalDirection.LONG else -1)
            tp3 = entry_price + atr_value * cfg.atr_tp3_mult * (1 if side == SignalDirection.LONG else -1)
            sl = entry_price - atr_value * cfg.atr_sl_mult * (1 if side == SignalDirection.LONG else -1)
        else:
            # Fixed-percentage TP/SL
            if side == SignalDirection.LONG:
                tp1 = entry_price * (1 + cfg.tp1_pct / 100)
                tp2 = entry_price * (1 + cfg.tp2_pct / 100)
                tp3 = entry_price * (1 + cfg.tp3_pct / 100)
                sl = entry_price * (1 - cfg.sl_pct / 100)
            else:
                tp1 = entry_price * (1 - cfg.tp1_pct / 100)
                tp2 = entry_price * (1 - cfg.tp2_pct / 100)
                tp3 = entry_price * (1 - cfg.tp3_pct / 100)
                sl = entry_price * (1 + cfg.sl_pct / 100)

        position = Position(
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            quantity=quantity,
            original_quantity=quantity,
            leverage=leverage,
            tp1_price=tp1,
            tp2_price=tp2,
            tp3_price=tp3,
            sl_price=sl,
        )
        self._positions[symbol] = position

        logger.info(
            "position | 📈 Opened {side} {symbol}: entry={entry} qty={qty} "
            "TP1={tp1} TP2={tp2} TP3={tp3} SL={sl}",
            side=side.value,
            symbol=symbol,
            entry=entry_price,
            qty=quantity,
            tp1=tp1,
            tp2=tp2,
            tp3=tp3,
            sl=sl,
        )
        return position

    def restore(
        self,
        symbol: str,
        side: SignalDirection,
        entry_price: float,
        quantity: float,
        original_quantity: float,
        leverage: int,
        opened_at: datetime,
        trade_id: int = 0,
    ) -> Position:
        """Restore an exchange position into memory after restart."""
        position = self.open(symbol, side, entry_price, quantity, leverage)
        position.original_quantity = max(original_quantity, quantity)
        position.opened_at = opened_at
        position.trade_id = trade_id

        # If the exchange quantity is smaller than the DB entry quantity, a
        # partial exit likely happened before the crash. Protect the remainder
        # at breakeven rather than restoring the original stop.
        if quantity < position.original_quantity:
            position.tp1_hit = True
            position.sl_price = position.entry_price

        logger.info(
            "position | 🔄 Restored {side} {symbol}: entry={entry} qty={qty} "
            "original_qty={original_qty} trade_id={trade_id}",
            side=side.value,
            symbol=symbol,
            entry=entry_price,
            qty=quantity,
            original_qty=position.original_quantity,
            trade_id=trade_id,
        )
        return position

    async def check_exits(self, symbol: str, current_price: float) -> None:
        """Evaluate exit conditions for a position at the current price.

        Checks are evaluated in priority order:
        1. Stop loss → close entire remaining position
        2. TP1 → partial close + move SL to breakeven
        3. TP2 → partial close
        4. TP3 → activate trailing stop
        5. Trailing stop trigger → close remaining position
        6. Time-based force close → close remaining position

        Args:
            symbol: The trading pair symbol.
            current_price: Latest market price for the symbol.
        """
        position = self._positions.get(symbol)
        if position is None:
            return

        # 1. Stop loss check
        if self._is_sl_hit(position, current_price):
            await self._close_full(position, current_price, ExitReason.SL)
            return

        # 2. TP1 — partial close + breakeven SL
        if not position.tp1_hit and self._is_tp_hit(position, position.tp1_price, current_price):
            await self._handle_tp1(position, current_price)
            # Position may still be open with remaining quantity
            if position.quantity <= 0:
                return

        # 3. TP2 — partial close
        if position.tp1_hit and not position.tp2_hit and self._is_tp_hit(position, position.tp2_price, current_price):
            await self._handle_tp2(position, current_price)
            if position.quantity <= 0:
                return

        # 4. TP3 — activate trailing stop
        if position.tp2_hit and not position.trailing_active and self._is_tp_hit(position, position.tp3_price, current_price):
            self._activate_trailing(position, current_price)

        # 5. Trailing stop — update and check trigger
        if position.trailing_active:
            self._update_trailing_price(position, current_price)
            if self._is_trailing_triggered(position, current_price):
                await self._close_full(position, current_price, ExitReason.TP3)
                return

        # 6. Time-based force close
        if minutes_elapsed(position.opened_at) >= self._exit_config.max_hold_min:
            logger.warning(
                "position | ⏰ Time limit exceeded for {symbol}, force closing",
                symbol=symbol,
            )
            await self._close_full(position, current_price, ExitReason.TIME)

    async def force_close(
        self,
        symbol: str,
        fallback_price: float,
        reason: ExitReason = ExitReason.HALT,
    ) -> None:
        """Force-close a tracked position and emit the normal close callback."""
        position = self._positions.get(symbol)
        if position is None:
            return

        await self._close_full(position, fallback_price, reason)

    def get_open_positions(self) -> list[Position]:
        """Return all currently open positions.

        Returns:
            List of open Position objects.
        """
        return list(self._positions.values())

    def get_position(self, symbol: str) -> Position | None:
        """Return the tracked position for *symbol*, if any."""
        return self._positions.get(symbol)

    def has_position(self, symbol: str) -> bool:
        """Check whether a position is open for the given symbol.

        Args:
            symbol: The trading pair symbol.

        Returns:
            True if an open position exists for the symbol.
        """
        return symbol in self._positions

    def remove(self, symbol: str) -> None:
        """Remove a locally tracked position without submitting an order."""
        self._positions.pop(symbol, None)

    async def reconcile_exchange_close(
        self,
        symbol: str,
        exit_price: float,
        reason: ExitReason,
        realized_pnl_usdt: float | None = None,
        closed_quantity: float = 0.0,
    ) -> bool:
        """Sync a position that was closed directly by the exchange.

        This is used for exchange-side protective stops. It must not submit
        another close order or cancel the filled stop order; it only updates
        local state and emits the normal close callback.
        """
        position = self._positions.get(symbol)
        if position is None:
            return False

        qty = closed_quantity if closed_quantity > 0 else position.quantity
        qty = min(qty, position.quantity)
        price = exit_price if exit_price > 0 else position.sl_price

        if realized_pnl_usdt is None:
            self._record_realized_pnl(position, qty, price)
        else:
            position.realized_pnl_usdt += realized_pnl_usdt

        position.quantity -= qty
        if position.quantity > 0:
            logger.warning(
                "position | Exchange close for {symbol} was partial: closed={closed} remaining={remaining}",
                symbol=symbol,
                closed=qty,
                remaining=position.quantity,
            )
            return False

        logger.info(
            "position | 🔁 Reconciled exchange-side close {symbol} — reason={reason} price={price}",
            symbol=symbol,
            reason=reason.value,
            price=price,
        )

        position.quantity = 0
        position.stop_order_id = 0
        await self._emit_closed(position, price, reason)
        return True

    # ------------------------------------------------------------------
    # TP/SL hit detection helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_sl_hit(position: Position, current_price: float) -> bool:
        """Check if the stop loss level has been reached.

        For LONG positions, SL is hit when price drops to or below SL.
        For SHORT positions, SL is hit when price rises to or above SL.
        """
        if position.side == SignalDirection.LONG:
            return current_price <= position.sl_price
        return current_price >= position.sl_price

    @staticmethod
    def _is_tp_hit(
        position: Position,
        tp_price: float,
        current_price: float,
    ) -> bool:
        """Check if a take-profit level has been reached.

        For LONG positions, TP is hit when price rises to or above the level.
        For SHORT positions, TP is hit when price drops to or below the level.
        """
        if position.side == SignalDirection.LONG:
            return current_price >= tp_price
        return current_price <= tp_price

    @staticmethod
    def _is_trailing_triggered(position: Position, current_price: float) -> bool:
        """Check if the trailing stop has been triggered.

        For LONG: triggered when price drops to or below trailing_price.
        For SHORT: triggered when price rises to or above trailing_price.
        """
        if position.side == SignalDirection.LONG:
            return current_price <= position.trailing_price
        return current_price >= position.trailing_price

    # ------------------------------------------------------------------
    # Exit action handlers
    # ------------------------------------------------------------------

    async def _handle_tp1(self, position: Position, current_price: float) -> None:
        """Handle TP1 hit: partial close and move SL to breakeven.

        Closes ``tp1_close_ratio`` of the current quantity and moves the
        stop loss to the entry price (breakeven). This is mandatory per
        risk rules and must never be skipped.
        """
        close_qty = position.original_quantity * self._exit_config.tp1_close_ratio
        close_qty = min(close_qty, position.quantity)
        closed_qty = 0.0

        if close_qty > 0:
            closing_side = self._closing_side(position.side)
            try:
                close_result = await self._close_order_fn(
                    position.symbol, closing_side, close_qty,
                )
            except Exception:
                logger.exception(
                    "position | Failed to close TP1 partial for {symbol}",
                    symbol=position.symbol,
                )
                return

            closed_qty = min(self._filled_quantity(close_result, close_qty), position.quantity)
            if closed_qty <= 0:
                logger.warning(
                    "position | TP1 close for {symbol} returned zero fill",
                    symbol=position.symbol,
                )
                return

            exit_price = self._filled_price(close_result, current_price)
            self._record_realized_pnl(position, closed_qty, exit_price)
            position.quantity -= closed_qty

        # Move SL to breakeven — mandatory, non-negotiable
        position.sl_price = position.entry_price
        position.tp1_hit = True
        await self._replace_exchange_stop(position)

        logger.info(
            "position | 🎯 TP1 hit {symbol}: closed {qty}, SL → breakeven ({entry})",
            symbol=position.symbol,
            qty=closed_qty,
            entry=position.entry_price,
        )

        # If the partial close emptied the position, emit close event
        if position.quantity <= 0:
            await self._emit_closed(position, current_price, ExitReason.TP1)

    async def _handle_tp2(self, position: Position, current_price: float) -> None:
        """Handle TP2 hit: partial close of original quantity fraction."""
        close_qty = position.original_quantity * self._exit_config.tp2_close_ratio
        close_qty = min(close_qty, position.quantity)
        closed_qty = 0.0

        if close_qty > 0:
            closing_side = self._closing_side(position.side)
            try:
                close_result = await self._close_order_fn(
                    position.symbol, closing_side, close_qty,
                )
            except Exception:
                logger.exception(
                    "position | Failed to close TP2 partial for {symbol}",
                    symbol=position.symbol,
                )
                return

            closed_qty = min(self._filled_quantity(close_result, close_qty), position.quantity)
            if closed_qty <= 0:
                logger.warning(
                    "position | TP2 close for {symbol} returned zero fill",
                    symbol=position.symbol,
                )
                return

            exit_price = self._filled_price(close_result, current_price)
            self._record_realized_pnl(position, closed_qty, exit_price)
            position.quantity -= closed_qty

        position.tp2_hit = True

        logger.info(
            "position | 🎯 TP2 hit {symbol}: closed {qty}, remaining={remaining}",
            symbol=position.symbol,
            qty=closed_qty,
            remaining=position.quantity,
        )

        if position.quantity <= 0:
            await self._emit_closed(position, current_price, ExitReason.TP2)

    def _activate_trailing(self, position: Position, current_price: float) -> None:
        """Activate trailing stop at TP3 hit.

        Sets the trailing stop price at ``trailing_stop_pct`` from the
        current price (fixed mode) or at ATR-based distance.
        """
        position.trailing_active = True
        cfg = self._exit_config

        if cfg.atr_mode:
            trail_distance = (position.tp3_price - position.tp2_price) / cfg.atr_tp3_mult * cfg.atr_trailing_mult
        else:
            trail_distance = current_price * cfg.trailing_stop_pct / 100

        if position.side == SignalDirection.LONG:
            position.trailing_price = current_price - trail_distance
        else:
            position.trailing_price = current_price + trail_distance

        logger.info(
            "position | 🔻 Trailing stop activated {symbol}: trigger={trigger}",
            symbol=position.symbol,
            trigger=position.trailing_price,
        )

    def _update_trailing_price(self, position: Position, current_price: float) -> None:
        """Update the trailing stop price as price moves favorably.

        For LONG: ratchet trailing price up as price makes new highs.
        For SHORT: ratchet trailing price down as price makes new lows.
        """
        cfg = self._exit_config

        if cfg.atr_mode:
            trail_distance = (position.tp3_price - position.tp2_price) / cfg.atr_tp3_mult * cfg.atr_trailing_mult
            if position.side == SignalDirection.LONG:
                new_trailing = current_price - trail_distance
                if new_trailing > position.trailing_price:
                    position.trailing_price = new_trailing
            else:
                new_trailing = current_price + trail_distance
                if new_trailing < position.trailing_price:
                    position.trailing_price = new_trailing
        else:
            trailing_pct = cfg.trailing_stop_pct / 100
            if position.side == SignalDirection.LONG:
                new_trailing = current_price * (1 - trailing_pct)
                if new_trailing > position.trailing_price:
                    position.trailing_price = new_trailing
            else:
                new_trailing = current_price * (1 + trailing_pct)
                if new_trailing < position.trailing_price:
                    position.trailing_price = new_trailing

    async def _close_full(
        self,
        position: Position,
        current_price: float,
        reason: ExitReason,
    ) -> None:
        """Close the entire remaining position and emit the close event."""
        exit_price = current_price
        if position.quantity > 0:
            closing_side = self._closing_side(position.side)
            requested_qty = position.quantity
            try:
                close_result = await self._close_order_fn(
                    position.symbol, closing_side, requested_qty,
                )
            except Exception:
                logger.exception(
                    "position | Failed to close full position for {symbol} ({reason})",
                    symbol=position.symbol,
                    reason=reason.value,
                )
                # Cancel the exchange stop to prevent orphaned orders.
                # The position stays tracked locally so the next tick retries
                # the close, but the stop must not linger on Binance.
                await self._cancel_exchange_stop(position)
                return

            exit_price = self._filled_price(close_result, current_price)

            closed_qty = min(self._filled_quantity(close_result, requested_qty), position.quantity)
            if closed_qty <= 0:
                logger.warning(
                    "position | Full close for {symbol} returned zero fill",
                    symbol=position.symbol,
                )
                return

            self._record_realized_pnl(position, closed_qty, exit_price)
            position.quantity -= closed_qty
            if position.quantity > 0:
                logger.warning(
                    "position | Partial full-close fill for {symbol}: closed={closed} remaining={remaining}",
                    symbol=position.symbol,
                    closed=closed_qty,
                    remaining=position.quantity,
                )
                return

        logger.info(
            "position | 📉 Closed {symbol} — reason={reason} price={price}",
            symbol=position.symbol,
            reason=reason.value,
            price=exit_price,
        )

        position.quantity = 0
        await self._emit_closed(position, exit_price, reason)

    async def _emit_closed(
        self,
        position: Position,
        exit_price: float,
        reason: ExitReason,
    ) -> None:
        """Remove the position from tracking and invoke the close callback."""
        await self._cancel_exchange_stop(position)
        self._positions.pop(position.symbol, None)

        pnl_usdt = position.realized_pnl_usdt
        pnl_pct = self._calculate_pnl_pct(position, pnl_usdt)

        result = TradeResult(
            trade_id=position.trade_id,
            symbol=position.symbol,
            side=position.side.value,
            entry_price=position.entry_price,
            exit_price=exit_price,
            pnl_usdt=pnl_usdt,
            pnl_pct=pnl_pct,
            exit_reason=reason,
        )

        if self.on_position_closed is not None:
            try:
                await self.on_position_closed(result)
            except Exception:
                logger.exception(
                    "position | on_position_closed callback failed for {symbol}",
                    symbol=position.symbol,
                )

    # ------------------------------------------------------------------
    # Utility helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _closing_side(side: SignalDirection) -> OrderSide:
        """Return the order side needed to close a position.

        LONG positions are closed with SELL, SHORT with BUY.
        """
        if side == SignalDirection.LONG:
            return OrderSide.SELL
        return OrderSide.BUY

    @staticmethod
    def _filled_quantity(close_result: Any, fallback_qty: float) -> float:
        """Return executed close quantity from an order result."""
        raw_qty = getattr(close_result, "quantity", 0.0)
        if not isinstance(raw_qty, (int, float, str)):
            return fallback_qty
        try:
            qty = float(raw_qty or 0.0)
        except (TypeError, ValueError):
            qty = 0.0
        if qty > 0:
            return qty
        return fallback_qty

    @staticmethod
    def _filled_price(close_result: Any, fallback_price: float) -> float:
        """Return executed average price from an order result."""
        raw_price = getattr(close_result, "avg_price", 0.0)
        if not isinstance(raw_price, (int, float, str)):
            return fallback_price
        try:
            price = float(raw_price or 0.0)
        except (TypeError, ValueError):
            price = 0.0
        if price > 0:
            return price
        return fallback_price

    def _record_realized_pnl(
        self,
        position: Position,
        quantity: float,
        exit_price: float,
    ) -> None:
        """Accumulate realized PnL for one filled close execution."""
        position.realized_pnl_usdt += self._calculate_fill_pnl_usdt(
            position,
            quantity,
            exit_price,
        )

    async def _replace_exchange_stop(self, position: Position) -> None:
        """Move the exchange-side stop order to the current position SL."""
        if position.stop_order_id <= 0 or self._replace_stop_order_fn is None:
            return

        try:
            result = await self._replace_stop_order_fn(
                position.symbol,
                position.stop_order_id,
                self._closing_side(position.side),
                position.sl_price,
            )
        except Exception:
            logger.exception(
                "position | Failed to replace exchange stop for {symbol}",
                symbol=position.symbol,
            )
            return

        position.stop_order_id = int(getattr(result, "order_id", 0) or 0)
        logger.info(
            "position | Exchange stop replaced {symbol}: stop={stop} order_id={order_id}",
            symbol=position.symbol,
            stop=position.sl_price,
            order_id=position.stop_order_id,
        )

    async def _cancel_exchange_stop(self, position: Position) -> None:
        """Cancel the exchange-side stop order after the position closes.

        If the targeted cancel by order ID fails, falls back to a sweep
        that fetches all open STOP_MARKET orders for the symbol and cancels
        them. This prevents orphaned stop orders from affecting future
        positions on the same symbol.
        """
        if self._cancel_order_fn is None:
            return

        if position.stop_order_id <= 0:
            # No known order ID — run a sweep just in case an order exists
            await self._sweep_orphaned_stops(position.symbol)
            return

        try:
            await self._cancel_order_fn(position.symbol, position.stop_order_id)
            position.stop_order_id = 0
            return
        except Exception:
            logger.exception(
                "position | Failed to cancel exchange stop for {symbol} order_id={order_id}, "
                "falling back to sweep",
                symbol=position.symbol,
                order_id=position.stop_order_id,
            )

        # Targeted cancel failed — sweep all stop orders for this symbol
        await self._sweep_orphaned_stops(position.symbol)
        position.stop_order_id = 0

    async def _sweep_orphaned_stops(self, symbol: str) -> None:
        """Cancel all open STOP_MARKET orders for *symbol* as a safety net."""
        if self._cancel_all_stops_fn is None:
            logger.warning(
                "position | Cannot sweep orphaned stops for {symbol}: no sweep function configured",
                symbol=symbol,
            )
            return

        try:
            cancelled = await self._cancel_all_stops_fn(symbol)
            if cancelled > 0:
                logger.warning(
                    "position | Swept {count} orphaned stop order(s) for {symbol}",
                    count=cancelled,
                    symbol=symbol,
                )
        except Exception:
            logger.exception(
                "position | Sweep of orphaned stops failed for {symbol}",
                symbol=symbol,
            )

    @staticmethod
    def _calculate_fill_pnl_usdt(
        position: Position,
        quantity: float,
        exit_price: float,
    ) -> float:
        """Calculate realized PnL in USDT for one close fill."""
        if position.side == SignalDirection.LONG:
            price_diff = exit_price - position.entry_price
        else:
            price_diff = position.entry_price - exit_price

        return price_diff * quantity

    @staticmethod
    def _calculate_pnl_pct(position: Position, pnl_usdt: float) -> float:
        """Calculate realized PnL percentage across all partial exits."""
        entry_notional = position.entry_price * position.original_quantity
        if entry_notional == 0:
            return 0.0

        return pnl_usdt / entry_notional * 100 * position.leverage
