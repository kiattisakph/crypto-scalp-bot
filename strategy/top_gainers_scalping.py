"""Strategy orchestrator for the Top Gainers Scalping strategy.

Coordinates signal evaluation, risk checks, and order placement by wiring
together the WatchlistManager, SignalEngine, RiskGuard, OrderManager,
PositionManager, CandleBuffer, TelegramAlert, and TradeRepository.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from loguru import logger

from core.config import AppConfig
from core.enums import ExitReason, OrderSide, SignalDirection
from core.models import ExitData, RiskCheckResult, TradeRecord, TradeResult
from execution.order_manager import OrderManager
from execution.position_manager import PositionManager
from notification.telegram_alert import TelegramAlert
from risk.risk_guard import RiskGuard
from storage.trade_repository import TradeRepository
from strategy.signal_engine import SignalEngine
from strategy.watchlist_manager import WatchlistManager
from utils.candle_buffer import CandleBuffer
from utils.time_utils import minutes_elapsed, utc_now


@dataclass(frozen=True)
class _EntryReservation:
    """Risk-approved entry context reserved under the entry lock."""

    risk_result: RiskCheckResult
    balance: float
    sizing_price: float
    atr_value: float | None = None


class TopGainersScalping:
    """Strategy orchestrator that coordinates the signal-to-trade flow.

    Wires together all core components and manages the main trading loop:
    candle close → signal evaluation → risk check → order placement.

    Args:
        watchlist_manager: Dynamic watchlist selector.
        signal_engine: Entry signal generator.
        risk_guard: Portfolio-level risk enforcer.
        order_manager: Binance REST API order executor.
        position_manager: In-memory position tracker with TP/SL logic.
        candle_buffer: Rolling candle buffer per symbol/timeframe.
        telegram: Telegram notification sender.
        trade_repo: Trade history persistence layer.
        config: Full application configuration.
        get_balance: Async callable returning the current USDT balance.
        get_free_margin_pct: Async callable returning available margin percentage.
        get_current_price: Async callable returning a live executable price.
    """

    def __init__(
        self,
        watchlist_manager: WatchlistManager,
        signal_engine: SignalEngine,
        risk_guard: RiskGuard,
        order_manager: OrderManager,
        position_manager: PositionManager,
        candle_buffer: CandleBuffer,
        telegram: TelegramAlert,
        trade_repo: TradeRepository,
        config: AppConfig,
        get_balance: Callable[[], Awaitable[float]],
        get_free_margin_pct: Callable[[], Awaitable[float]] | None = None,
        get_current_price: Callable[[str], Awaitable[float]] | None = None,
        get_funding_rate: Callable[[str], Awaitable[float]] | None = None,
        get_spread_pct: Callable[[str], Awaitable[float]] | None = None,
    ) -> None:
        self._watchlist_manager = watchlist_manager
        self._signal_engine = signal_engine
        self._risk_guard = risk_guard
        self._order_manager = order_manager
        self._position_manager = position_manager
        self._candle_buffer = candle_buffer
        self._telegram = telegram
        self._trade_repo = trade_repo
        self._config = config
        self._get_balance = get_balance
        self._get_free_margin_pct = get_free_margin_pct or self._default_free_margin_pct
        self._get_current_price = get_current_price
        self._get_funding_rate = get_funding_rate
        self._get_spread_pct = get_spread_pct

        # Per-symbol cooldown tracking: symbol → last entry datetime (UTC).
        self._cooldowns: dict[str, datetime] = {}

        # Serialises pre-trade risk checks and reserves pending entries so
        # concurrent candle callbacks cannot exceed max_concurrent_positions.
        self._entry_lock = asyncio.Lock()
        self._pending_entries: set[str] = set()

        # Background task for periodic watchlist refresh.
        self._refresh_task: asyncio.Task[None] | None = None

        # Wire the position closed callback.
        self._position_manager.on_position_closed = self._on_position_closed

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Begin listening for candle close events and start the watchlist refresh timer."""
        self._refresh_task = asyncio.create_task(self._watchlist_refresh_loop())
        logger.info("strategy | TopGainersScalping started")

    async def stop(self) -> None:
        """Cancel the watchlist refresh task and stop the strategy loop."""
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
            self._refresh_task = None
        logger.info("strategy | TopGainersScalping stopped")

    # ------------------------------------------------------------------
    # Watchlist refresh timer
    # ------------------------------------------------------------------

    async def _watchlist_refresh_loop(self) -> None:
        """Periodically refresh the watchlist at the configured interval."""
        interval = self._config.watchlist.refresh_interval_sec
        while True:
            try:
                await asyncio.sleep(interval)
                await self._watchlist_manager.refresh()
                logger.debug("strategy | Watchlist refresh completed")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("strategy | Error during watchlist refresh")

    @staticmethod
    async def _default_free_margin_pct() -> float:
        """Fallback for tests or custom wiring that has not provided margin data."""
        return 100.0

    # ------------------------------------------------------------------
    # Main trading logic callback
    # ------------------------------------------------------------------

    async def on_candle_closed(self, symbol: str, timeframe: str) -> None:
        """Handle a closed candle event — the main trading logic entry point.

        Only evaluates signals on the configured signal timeframe (3m by
        default). Gets DataFrames from CandleBuffer for both signal and
        trend timeframes, evaluates the signal via SignalEngine, and if
        approved by RiskGuard, places the order.

        Args:
            symbol: The trading pair symbol (e.g. "SOLUSDT").
            timeframe: The candle timeframe (e.g. "3m", "15m").
        """
        try:
            await self._process_candle(symbol, timeframe)
        except Exception:
            logger.exception(
                "strategy | Error processing candle close for {symbol} {tf}",
                symbol=symbol,
                tf=timeframe,
            )

    async def _process_candle(self, symbol: str, timeframe: str) -> None:
        """Internal candle processing logic, separated for clean error handling."""
        signal_tf = self._config.strategy.signal_timeframe
        trend_tf = self._config.strategy.trend_timeframe

        # Only evaluate signals on the signal timeframe.
        if timeframe != signal_tf:
            return

        # Check if there's enough data in the buffer before evaluating.
        min_3m = max(
            self._config.strategy.entry.ema_slow,
            self._config.strategy.entry.rsi_period,
            20,
        )
        min_15m = self._config.strategy.entry.ema_trend_slow

        has_3m = await self._candle_buffer.has_enough_data(symbol, signal_tf, min_3m)
        has_15m = await self._candle_buffer.has_enough_data(symbol, trend_tf, min_15m)

        if not has_3m or not has_15m:
            logger.debug(
                "strategy | {symbol} insufficient buffer data (3m={has_3m}, 15m={has_15m})",
                symbol=symbol,
                has_3m=has_3m,
                has_15m=has_15m,
            )
            return

        # Get DataFrames from CandleBuffer.
        df_3m = await self._candle_buffer.get_df(symbol, signal_tf)
        df_15m = await self._candle_buffer.get_df(symbol, trend_tf)

        # Evaluate signal via SignalEngine.
        signal = self._signal_engine.evaluate(symbol, df_3m, df_15m)
        if signal is None:
            return

        # Market regime check: skip entry if ADX indicates sideways/choppy market.
        adx_value = signal.indicators.get("adx")
        adx_threshold = self._config.strategy.entry.adx_trend_threshold
        if adx_value is not None and adx_value < adx_threshold:
            logger.debug(
                "strategy | {symbol} signal skipped — market sideways "
                "(ADX {adx:.1f} < {threshold:.1f})",
                symbol=symbol,
                adx=adx_value,
                threshold=adx_threshold,
            )
            return

        # Skip if there's already an open position for this symbol.
        if self._position_manager.has_position(symbol):
            logger.debug(
                "strategy | {symbol} signal ignored — position already open",
                symbol=symbol,
            )
            return

        # Check cooldown: suppress if within signal_cooldown_min of last entry.
        cooldown_min = self._config.strategy.entry.signal_cooldown_min
        if symbol in self._cooldowns:
            elapsed = minutes_elapsed(self._cooldowns[symbol])
            if elapsed < cooldown_min:
                logger.debug(
                    "strategy | {symbol} signal suppressed — cooldown active "
                    "({elapsed:.1f}/{cooldown} min)",
                    symbol=symbol,
                    elapsed=elapsed,
                    cooldown=cooldown_min,
                )
                return

        candle_close = float(df_3m["close"].iloc[-1])
        sizing_price = await self._get_sizing_price(symbol, candle_close)
        if sizing_price <= 0:
            logger.warning(
                "strategy | {symbol} trade skipped — no valid live price for sizing",
                symbol=symbol,
            )
            return

        # Extract ATR from signal snapshot for risk-based sizing.
        atr_value = signal.indicators.get("atr")

        # Funding rate filter: skip if funding rate exceeds threshold or works against position.
        if self._get_funding_rate is not None:
            funding_rate = await self._get_funding_rate(symbol)
            max_funding = self._config.strategy.entry.max_funding_rate_pct

            if abs(funding_rate) > max_funding:
                logger.debug(
                    "strategy | {symbol} signal skipped — funding rate too high "
                    "({rate:.4f}% > {max:.4f}%)",
                    symbol=symbol,
                    rate=funding_rate * 100,
                    max=max_funding * 100,
                )
                return

            if self._config.strategy.entry.reject_funding_against_position:
                # Positive funding = longs pay shorts → bad for LONG
                # Negative funding = shorts pay longs → bad for SHORT
                is_against = (
                    (signal.direction == SignalDirection.LONG and funding_rate > 0)
                    or (signal.direction == SignalDirection.SHORT and funding_rate < 0)
                )
                if is_against:
                    logger.debug(
                        "strategy | {symbol} signal skipped — funding against position "
                        "(rate={rate:.4f}%, direction={dir})",
                        symbol=symbol,
                        rate=funding_rate * 100,
                        dir=signal.direction.value,
                    )
                    return

        # Slippage protection: check bid-ask spread before sending market order.
        if self._get_spread_pct is not None:
            spread_pct = await self._get_spread_pct(symbol)
            max_spread = self._config.risk.max_spread_pct
            if spread_pct > max_spread:
                logger.debug(
                    "strategy | {symbol} signal skipped — spread too wide "
                    "({spread:.4f}% > {max:.4f}%)",
                    symbol=symbol,
                    spread=spread_pct,
                    max=max_spread,
                )
                return

        reservation = await self._reserve_entry(symbol, sizing_price, atr_value, signal.confidence)
        if reservation is None:
            return

        logger.info(
            "strategy | {symbol} passed all pre-order checks | direction={direction} "
            "confidence={confidence:.3f} price={price:.8f} qty={qty:.8f} atr={atr}",
            symbol=symbol,
            direction=signal.direction.value,
            confidence=signal.confidence,
            price=sizing_price,
            qty=reservation.risk_result.position_size,
            atr=atr_value,
        )

        try:
            # --- Trade approved — execute ---
            leverage = self._config.risk.leverage
            quantity = reservation.risk_result.position_size
            order_side = (
                OrderSide.BUY if signal.direction == SignalDirection.LONG else OrderSide.SELL
            )

            # Set leverage.
            try:
                await self._order_manager.set_leverage(symbol, leverage)
            except Exception:
                logger.exception(
                    "strategy | {symbol} failed to set leverage, skipping trade",
                    symbol=symbol,
                )
                return

            # Place market order.
            try:
                order_result = await self._order_manager.open_position(
                    symbol, order_side, quantity,
                )
            except Exception:
                logger.exception(
                    "strategy | {symbol} failed to place order, skipping trade",
                    symbol=symbol,
                )
                return

            executed_quantity = order_result.quantity
            if executed_quantity <= 0:
                logger.error(
                    "strategy | {symbol} order returned zero executed quantity, skipping",
                    symbol=symbol,
                )
                return

            fill_price = (
                order_result.avg_price
                if order_result.avg_price > 0
                else sizing_price
            )
            executed_quantity = await self._trim_excess_fill_risk(
                symbol=symbol,
                direction=signal.direction,
                fill_price=fill_price,
                executed_quantity=executed_quantity,
                balance=reservation.balance,
                atr_value=reservation.atr_value,
            )
            if executed_quantity <= 0:
                return

            # Open position via PositionManager.
            position = self._position_manager.open(
                symbol=symbol,
                side=signal.direction,
                entry_price=fill_price,
                quantity=executed_quantity,
                leverage=leverage,
                atr_value=reservation.atr_value,
            )

            # Place the exchange-side protective stop before continuing. If this
            # fails, the position must not remain open and unmanaged.
            stop_side = self._closing_side(signal.direction)
            try:
                stop_result = await self._order_manager.place_stop_loss(
                    symbol=symbol,
                    side=stop_side,
                    stop_price=position.sl_price,
                )
            except Exception:
                logger.exception(
                    "strategy | {symbol} failed to place exchange stop, emergency closing",
                    symbol=symbol,
                )
                await self._close_unprotected_position(position)
                return

            position.stop_order_id = stop_result.order_id

            # Record the trade in TradeRepository.  The position is already
            # protected on the exchange (SL placed), so a DB failure must not
            # crash the flow — the position continues to be managed in memory.
            trade_record = TradeRecord(
                symbol=symbol,
                side=signal.direction.value,
                entry_price=fill_price,
                quantity=executed_quantity,
                leverage=leverage,
                entry_at=utc_now(),
                signal_snapshot=json.dumps(signal.indicators),
            )
            trade_id = await self._safe_insert_trade(trade_record)

            # Set the trade_id on the position (0 if insert failed).
            position.trade_id = trade_id

            # Start cooldown timer for the symbol.
            self._cooldowns[symbol] = utc_now()

            # Send position opened Telegram alert.
            await self._telegram.notify_position_opened(
                symbol=symbol,
                direction=signal.direction,
                entry_price=fill_price,
                quantity=executed_quantity,
                sl_price=position.sl_price,
                tp1_price=position.tp1_price,
            )

            logger.info(
                "strategy | {symbol} {direction} position opened | "
                "entry={entry} qty={qty} trade_id={tid}",
                symbol=symbol,
                direction=signal.direction.value,
                entry=fill_price,
                qty=executed_quantity,
                tid=trade_id,
            )
        finally:
            await self._release_entry(symbol)

    async def _reserve_entry(
        self,
        symbol: str,
        sizing_price: float,
        atr_value: float | None = None,
        confidence: float = 1.0,
    ) -> _EntryReservation | None:
        """Run the pre-trade risk check under lock and reserve the symbol."""
        async with self._entry_lock:
            if symbol in self._pending_entries:
                logger.debug(
                    "strategy | {symbol} signal ignored — entry already pending",
                    symbol=symbol,
                )
                return None

            if self._position_manager.has_position(symbol):
                logger.debug(
                    "strategy | {symbol} signal ignored — position already open",
                    symbol=symbol,
                )
                return None

            balance = await self._get_balance()
            free_margin_pct = await self._get_free_margin_pct()
            open_position_count = (
                len(self._position_manager.get_open_positions())
                + len(self._pending_entries)
            )

            risk_result = self._risk_guard.check_trade(
                entry_price=sizing_price,
                balance=balance,
                open_position_count=open_position_count,
                free_margin_pct=free_margin_pct,
                atr_value=atr_value,
                confidence=confidence,
            )

            if not risk_result.approved:
                logger.info(
                    "strategy | {symbol} trade rejected by RiskGuard: {reason}",
                    symbol=symbol,
                    reason=risk_result.reject_reason,
                )
                return None

            self._pending_entries.add(symbol)
            return _EntryReservation(
                risk_result=risk_result,
                balance=balance,
                sizing_price=sizing_price,
                atr_value=atr_value,
            )

    async def _release_entry(self, symbol: str) -> None:
        """Release a pending entry reservation."""
        async with self._entry_lock:
            self._pending_entries.discard(symbol)

    # ------------------------------------------------------------------
    # Force close all positions
    # ------------------------------------------------------------------

    async def close_all_positions(self) -> None:
        """Force-close all open positions via OrderManager.

        Used during shutdown or halt scenarios. The close goes through
        PositionManager so in-memory state, DB, PnL, and alerts stay synced.
        """
        positions = self._position_manager.get_open_positions()
        if not positions:
            logger.info("strategy | No open positions to close")
            return

        logger.info(
            "strategy | Force-closing {n} open position(s)",
            n=len(positions),
        )

        for position in positions:
            try:
                await self._position_manager.force_close(
                    position.symbol,
                    fallback_price=position.entry_price,
                    reason=ExitReason.HALT,
                )
                logger.info(
                    "strategy | Force-closed {symbol} ({side})",
                    symbol=position.symbol,
                    side=position.side.value,
                )
            except Exception:
                logger.exception(
                    "strategy | Failed to force-close {symbol}",
                    symbol=position.symbol,
                )

    # ------------------------------------------------------------------
    # Position closed callback
    # ------------------------------------------------------------------

    async def _on_position_closed(self, result: TradeResult) -> None:
        """Handle a position closed event from PositionManager.

        Records PnL in RiskGuard, checks halt conditions, saves the trade
        close to TradeRepository, updates daily stats, and sends a Telegram
        notification.

        Args:
            result: Summary of the completed trade.
        """
        try:
            # Get current balance for risk calculations.
            balance = await self._get_balance()

            # Record PnL in RiskGuard.
            self._risk_guard.record_pnl(result.pnl_usdt, balance)

            # Check halt conditions.
            await self._risk_guard.check_halt_conditions(balance)

            # Save trade close to TradeRepository.
            exit_data = ExitData(
                exit_price=result.exit_price,
                pnl_usdt=result.pnl_usdt,
                pnl_pct=result.pnl_pct,
                exit_reason=result.exit_reason,
                exit_at=utc_now(),
            )

            if result.trade_id > 0:
                try:
                    await self._trade_repo.close_trade(result.trade_id, exit_data)
                except Exception:
                    logger.exception(
                        "strategy | Failed to close trade {trade_id} in DB for {symbol}",
                        trade_id=result.trade_id,
                        symbol=result.symbol,
                    )
            else:
                # trade_id=0 means the DB insert on open failed. Log a
                # standalone record so the trade is not completely lost.
                logger.error(
                    "strategy | {symbol} closed with trade_id=0 — DB record missing | "
                    "side={side} entry={entry} exit={exit} pnl={pnl:.4f} reason={reason}",
                    symbol=result.symbol,
                    side=result.side,
                    entry=result.entry_price,
                    exit=result.exit_price,
                    pnl=result.pnl_usdt,
                    reason=result.exit_reason.value,
                )
                # Attempt to insert a retroactive closed trade so the record
                # exists in the DB for history and daily stats.
                await self._insert_retroactive_closed_trade(result, exit_data)

            # Update daily stats.
            today = utc_now().strftime("%Y-%m-%d")
            is_win = result.pnl_usdt > 0
            await self._trade_repo.update_daily_stats(today, result.pnl_usdt, is_win)

            # Send position closed Telegram alert.
            await self._telegram.notify_position_closed(
                symbol=result.symbol,
                exit_reason=result.exit_reason,
                pnl_usdt=result.pnl_usdt,
            )

            logger.info(
                "strategy | {symbol} position closed | reason={reason} "
                "pnl={pnl:.4f} USDT",
                symbol=result.symbol,
                reason=result.exit_reason.value,
                pnl=result.pnl_usdt,
            )
        except Exception:
            logger.exception(
                "strategy | Error handling position closed for {symbol}",
                symbol=result.symbol,
            )

    # ------------------------------------------------------------------
    # DB resilience helpers
    # ------------------------------------------------------------------

    async def _safe_insert_trade(self, trade_record: TradeRecord) -> int:
        """Insert a trade record, returning 0 instead of crashing on failure.

        The position is already protected on the exchange (SL placed) so a
        DB failure must not prevent the bot from managing the position.  A
        Telegram alert is sent so the operator knows the record is missing.
        """
        try:
            return await self._trade_repo.insert_trade(trade_record)
        except Exception:
            logger.exception(
                "strategy | {symbol} DB insert failed — position will run "
                "without a trade record",
                symbol=trade_record.symbol,
            )
            try:
                await self._telegram.send(
                    f"⚠️ DB INSERT FAIL | {trade_record.symbol} "
                    f"{trade_record.side} @ {trade_record.entry_price} | "
                    "Position is protected but has no DB record"
                )
            except Exception:
                logger.warning(
                    "strategy | Failed to send DB-insert-fail Telegram alert "
                    "for {symbol}",
                    symbol=trade_record.symbol,
                )
            return 0

    async def _insert_retroactive_closed_trade(
        self,
        result: TradeResult,
        exit_data: ExitData,
    ) -> None:
        """Insert a complete trade record retroactively when the open insert failed.

        Creates the trade as OPEN then immediately closes it so the DB has
        a full history row.  Best-effort — failures are logged, not raised.
        """
        try:
            record = TradeRecord(
                symbol=result.symbol,
                side=result.side,
                entry_price=result.entry_price,
                quantity=0.0,
                leverage=0,
                entry_at=exit_data.exit_at,
                signal_snapshot="{}",
            )
            trade_id = await self._trade_repo.insert_trade(record)
            await self._trade_repo.close_trade(trade_id, exit_data)
            logger.info(
                "strategy | Retroactive trade record inserted for {symbol} "
                "trade_id={tid}",
                symbol=result.symbol,
                tid=trade_id,
            )
        except Exception:
            logger.exception(
                "strategy | Failed to insert retroactive trade for {symbol}",
                symbol=result.symbol,
            )

    @staticmethod
    def _closing_side(direction: SignalDirection) -> OrderSide:
        """Return the order side needed to close a signal direction."""
        return OrderSide.SELL if direction == SignalDirection.LONG else OrderSide.BUY

    async def _get_sizing_price(self, symbol: str, candle_close: float) -> float:
        """Return a live price for risk sizing, falling back only when unwired."""
        if self._get_current_price is None:
            return candle_close

        try:
            live_price = await self._get_current_price(symbol)
        except Exception:
            logger.exception(
                "strategy | {symbol} failed to fetch live sizing price",
                symbol=symbol,
            )
            return 0.0

        if live_price <= 0:
            logger.warning(
                "strategy | {symbol} invalid live sizing price: {price}",
                symbol=symbol,
                price=live_price,
            )
            return 0.0

        return live_price

    async def _trim_excess_fill_risk(
        self,
        symbol: str,
        direction: SignalDirection,
        fill_price: float,
        executed_quantity: float,
        balance: float,
        atr_value: float | None = None,
    ) -> float:
        """Reduce post-fill quantity if slippage makes risk exceed budget."""
        max_quantity = self._max_risk_quantity(fill_price, balance, atr_value)
        if max_quantity <= 0:
            closed_quantity = await self._emergency_close_fill(
                symbol,
                direction,
                executed_quantity,
            )
            return max(executed_quantity - closed_quantity, 0.0)

        tolerance = max(executed_quantity * 1e-9, 1e-12)
        if executed_quantity <= max_quantity + tolerance:
            return executed_quantity

        excess_quantity = executed_quantity - max_quantity
        closing_side = self._closing_side(direction)
        logger.warning(
            "strategy | {symbol} fill risk exceeds budget, trimming excess "
            "qty={excess} executed={executed} max={max_qty}",
            symbol=symbol,
            excess=excess_quantity,
            executed=executed_quantity,
            max_qty=max_quantity,
        )

        try:
            close_result = await self._order_manager.close_position(
                symbol,
                closing_side,
                excess_quantity,
            )
        except Exception:
            logger.exception(
                "strategy | {symbol} failed to trim excess fill risk, emergency closing",
                symbol=symbol,
            )
            closed_quantity = await self._emergency_close_fill(
                symbol,
                direction,
                executed_quantity,
            )
            return max(executed_quantity - closed_quantity, 0.0)

        closed_quantity = min(
            self._filled_quantity(close_result, excess_quantity),
            executed_quantity,
        )
        remaining_quantity = executed_quantity - closed_quantity
        if remaining_quantity > max_quantity + tolerance:
            logger.critical(
                "strategy | {symbol} fill still exceeds risk after trim, emergency closing",
                symbol=symbol,
            )
            closed_quantity = await self._emergency_close_fill(
                symbol,
                direction,
                remaining_quantity,
            )
            return max(remaining_quantity - closed_quantity, 0.0)

        return remaining_quantity

    def _max_risk_quantity(self, fill_price: float, balance: float, atr_value: float | None = None) -> float:
        """Calculate max quantity whose SL distance stays within risk budget."""
        risk_amount = balance * self._config.risk.risk_per_trade_pct / 100
        leverage = self._config.risk.leverage
        exit_cfg = self._config.strategy.exit

        if atr_value is not None and exit_cfg.atr_mode:
            sl_distance = atr_value * exit_cfg.atr_sl_mult
        else:
            sl_distance = fill_price * exit_cfg.sl_pct / 100

        if risk_amount <= 0 or sl_distance <= 0:
            return 0.0
        return risk_amount / (leverage * sl_distance)

    async def _emergency_close_fill(
        self,
        symbol: str,
        direction: SignalDirection,
        quantity: float,
    ) -> float:
        """Close a just-filled entry that cannot be made compliant with risk."""
        if quantity <= 0:
            return 0.0
        try:
            close_result = await self._order_manager.close_position(
                symbol,
                self._closing_side(direction),
                quantity,
            )
        except Exception:
            logger.critical(
                "strategy | Failed to emergency close oversize fill for {symbol}",
                symbol=symbol,
            )
            return 0.0

        return min(self._filled_quantity(close_result, quantity), quantity)

    @staticmethod
    def _filled_quantity(close_result, fallback_qty: float) -> float:
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

    async def _close_unprotected_position(self, position) -> None:
        """Emergency-close a position whose exchange-side SL could not be placed."""
        try:
            await self._order_manager.close_position(
                position.symbol,
                self._closing_side(position.side),
                position.quantity,
            )
        except Exception:
            logger.critical(
                "strategy | Failed to emergency close unprotected {symbol}",
                symbol=position.symbol,
            )
            return

        remove = getattr(self._position_manager, "remove", None)
        if remove is not None:
            remove(position.symbol)

        logger.critical(
            "strategy | Emergency closed unprotected {symbol}",
            symbol=position.symbol,
        )
