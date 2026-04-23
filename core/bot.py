"""BotEngine — lifecycle management and component wiring for crypto-scalp-bot.

Initialises all components, wires callbacks between them, and manages
the startup/shutdown sequences.  BotEngine is the ONLY component that
knows about all others.
"""
from __future__ import annotations

import asyncio
import signal
from datetime import datetime
from typing import Any

from loguru import logger

from core.binance_client import BinanceClient
from core.config import AppConfig, EnvSettings, load_config
from core.enums import ExitReason, OrderSide, SignalDirection
from core.logging_setup import setup_logging
from core.models import OpenTradeRecord, OrderUpdate, Position, TradeRecord
from execution.order_manager import OrderManager
from execution.position_manager import PositionManager
from notification.telegram_alert import TelegramAlert
from risk.risk_guard import RiskGuard
from storage.database import Database
from storage.trade_repository import TradeRepository
from strategy.signal_engine import SignalEngine
from strategy.top_gainers_scalping import TopGainersScalping
from strategy.watchlist_manager import WatchlistManager
from streams.kline_stream import KlineStream
from streams.ticker_stream import TickerStream
from streams.user_data_stream import UserDataStream
from utils.candle_buffer import CandleBuffer
from utils.time_utils import utc_now

log = logger.bind(component="bot")


class BotEngine:
    """Orchestrates all bot components and manages the lifecycle.

    BotEngine is the single entry point that wires every component
    together via callbacks, starts the bot in the correct sequence,
    and handles graceful shutdown on SIGTERM/SIGINT.

    Args:
        env: Validated environment settings.
        config: Validated application configuration.
    """

    def __init__(self, env: EnvSettings, config: AppConfig) -> None:
        self._env = env
        self._config = config

        # Lifecycle flag
        self._stop_event = asyncio.Event()
        self._stopping = False

        # Components — initialised in start()
        self._database: Database | None = None
        self._trade_repo: TradeRepository | None = None
        self._telegram: TelegramAlert | None = None
        self._order_manager: OrderManager | None = None
        self._position_manager: PositionManager | None = None
        self._risk_guard: RiskGuard | None = None
        self._candle_buffer: CandleBuffer | None = None
        self._watchlist_manager: WatchlistManager | None = None
        self._signal_engine: SignalEngine | None = None
        self._strategy: TopGainersScalping | None = None
        self._ticker_stream: TickerStream | None = None
        self._kline_stream: KlineStream | None = None
        self._user_data_stream: UserDataStream | None = None
        self._latest_ticker_prices: dict[str, float] = {}
        self._external_stop_fills: dict[int, dict[str, float]] = {}
        self._pending_fill_symbols: dict[int, str] = {}
        self._reconciliation_task: asyncio.Task[None] | None = None
        self._shutdown_task: asyncio.Task[None] | None = None
        self._shutdown_signal_count = 0

    # ------------------------------------------------------------------
    # Lifecycle — start
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the bot following the strict startup sequence.

        1. Initialise database (create tables if needed).
        2. Load daily risk state from database.
        3. Connect OrderManager to Binance.
        4. Wire all components and callbacks.
        5. Connect UserDataStream for exchange-side reconciliation.
        6. Connect TickerStream (``!ticker@arr``).
        7. Start TopGainersScalping strategy loop.
        8. Send "Bot started" Telegram alert.
        9. Wait until stop is triggered.
        """
        log.info("🚀 Starting BotEngine…")

        # --- Register signal handlers ---
        self._register_signal_handlers()

        # --- 1. Initialise database ---
        self._database = Database(self._env.db_path)
        await self._database.init()

        # --- 2. Create support components ---
        self._trade_repo = TradeRepository(self._database)
        self._telegram = TelegramAlert(
            bot_token=self._env.telegram_bot_token,
            chat_id=self._env.telegram_chat_id,
        )

        # --- 3. Connect OrderManager ---
        self._order_manager = OrderManager(self._env)
        await self._order_manager.connect()

        # --- 4. Create risk guard and load daily state ---
        self._risk_guard = RiskGuard(
            risk_config=self._config.risk,
            exit_config=self._config.strategy.exit,
            trade_repo=self._trade_repo,
            telegram=self._telegram,
        )
        await self._risk_guard.load_daily_state()
        self._risk_guard.set_session_peak_balance(await self._get_balance())

        # --- 5. Create execution components ---
        self._position_manager = PositionManager(
            exit_config=self._config.strategy.exit,
            close_order_fn=self._order_manager.close_position,
            replace_stop_order_fn=self._order_manager.replace_stop_loss,
            cancel_order_fn=self._order_manager.cancel_order,
            cancel_all_stops_fn=self._order_manager.cancel_all_stop_orders,
        )
        await self._recover_open_positions()

        # --- 6. Create strategy components ---
        self._candle_buffer = CandleBuffer(
            max_size=self._config.strategy.candle_buffer_size,
        )
        self._signal_engine = SignalEngine(self._config.strategy.entry)

        self._watchlist_manager = WatchlistManager(
            config=self._config.watchlist,
            position_checker=self._position_manager,
        )

        self._strategy = TopGainersScalping(
            watchlist_manager=self._watchlist_manager,
            signal_engine=self._signal_engine,
            risk_guard=self._risk_guard,
            order_manager=self._order_manager,
            position_manager=self._position_manager,
            candle_buffer=self._candle_buffer,
            telegram=self._telegram,
            trade_repo=self._trade_repo,
            config=self._config,
            get_balance=self._get_balance,
            get_free_margin_pct=self._get_free_margin_pct,
            get_current_price=self._get_current_price,
            get_funding_rate=self._get_funding_rate,
            get_spread_pct=self._get_spread_pct,
        )

        # --- 7. Create stream components ---
        assert self._order_manager._client is not None
        client: BinanceClient = self._order_manager._client

        self._ticker_stream = TickerStream(client)
        self._user_data_stream = UserDataStream(client)
        self._kline_stream = KlineStream(
            client,
            timeframes=[
                self._config.strategy.signal_timeframe,
                self._config.strategy.trend_timeframe,
            ],
        )

        # --- 8. Wire all callbacks ---
        self._wire_callbacks()

        # --- 9. Connect user-data stream for exchange-side reconciliation ---
        await self._user_data_stream.connect()

        # --- 10. Start periodic exchange reconciliation ---
        self._reconciliation_task = asyncio.create_task(
            self._periodic_reconciliation_loop()
        )
        self._reconciliation_task.add_done_callback(self._on_task_done)

        # --- 11. Connect TickerStream ---
        await self._ticker_stream.connect()

        # --- 12. Start strategy ---
        await self._strategy.start()

        # --- 13. Send "Bot started" alert ---
        await self._telegram.notify_started()
        log.info("🚀 BotEngine started successfully")

        # --- 14. Wait until stop is triggered ---
        await self._stop_event.wait()

    # ------------------------------------------------------------------
    # Lifecycle — stop
    # ------------------------------------------------------------------

    async def stop(self) -> None:
        """Shut down the bot following the strict shutdown sequence.

        1. Close all open positions via strategy.
        2. Stop strategy loop.
        3. Disconnect KlineStream (all symbols).
        4. Disconnect TickerStream.
        5. Disconnect UserDataStream.
        6. Close OrderManager connection.
        7. Close database connection.
        8. Send "Bot stopped" Telegram alert.
        """
        if self._stopping:
            return
        self._stopping = True
        log.info("🛑 Stopping BotEngine…")

        # 1. Close all open positions
        if self._strategy is not None:
            try:
                await self._strategy.close_all_positions()
            except Exception:
                log.exception("Error closing positions during shutdown")

        # 2. Stop strategy loop
        if self._strategy is not None:
            try:
                await self._strategy.stop()
            except Exception:
                log.exception("Error stopping strategy during shutdown")

        # 3. Disconnect KlineStream
        if self._kline_stream is not None:
            try:
                await self._kline_stream.disconnect()
            except Exception:
                log.exception("Error disconnecting KlineStream during shutdown")

        # 4. Disconnect TickerStream
        if self._ticker_stream is not None:
            try:
                await self._ticker_stream.disconnect()
            except Exception:
                log.exception("Error disconnecting TickerStream during shutdown")

        # 5. Disconnect UserDataStream
        if self._user_data_stream is not None:
            try:
                await self._user_data_stream.disconnect()
            except Exception:
                log.exception("Error disconnecting UserDataStream during shutdown")

        # 5b. Cancel periodic reconciliation
        if self._reconciliation_task is not None:
            self._reconciliation_task.cancel()
            try:
                await self._reconciliation_task
            except asyncio.CancelledError:
                pass
            self._reconciliation_task = None

        # 6. Close OrderManager
        if self._order_manager is not None:
            try:
                await self._order_manager.close()
            except Exception:
                log.exception("Error closing OrderManager during shutdown")

        # 7. Close database
        if self._database is not None:
            try:
                await self._database.close()
            except Exception:
                log.exception("Error closing database during shutdown")

        # 8. Send "Bot stopped" alert
        if self._telegram is not None:
            try:
                await self._telegram.notify_stopped()
            except Exception:
                log.exception("Error sending stop notification")
            try:
                await self._telegram.close()
            except Exception:
                log.exception("Error closing Telegram client")

        log.info("🛑 BotEngine stopped")
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------

    def _register_signal_handlers(self) -> None:
        """Register SIGTERM and SIGINT handlers that trigger ``stop()``."""
        loop = asyncio.get_running_loop()

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(
                sig,
                self._request_shutdown,
                sig,
            )

        log.info("Signal handlers registered (SIGTERM, SIGINT)")

    def _request_shutdown(self, sig: signal.Signals) -> None:
        """Request graceful shutdown from a process signal.

        A second signal exits the main wait loop so Ctrl-C is still usable
        if a network call or exchange cleanup step is taking too long.
        """
        self._shutdown_signal_count += 1

        if self._shutdown_signal_count == 1:
            log.warning(
                "Received {signal_name}; starting graceful shutdown",
                signal_name=sig.name,
            )
            self._shutdown_task = asyncio.create_task(self.stop())
            self._shutdown_task.add_done_callback(self._on_task_done)
            return

        log.critical(
            "Received {signal_name} again during shutdown; forcing event loop exit",
            signal_name=sig.name,
        )
        self._stop_event.set()

    @staticmethod
    def _on_task_done(task: asyncio.Task[None]) -> None:
        """Log unhandled exceptions from background tasks."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.exception(
                "Background task failed: {error}",
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # Callback wiring
    # ------------------------------------------------------------------

    def _wire_callbacks(self) -> None:
        """Wire all inter-component callbacks.

        This is where BotEngine fulfils its role as the sole component
        that knows about all others.
        """
        assert self._ticker_stream is not None
        assert self._kline_stream is not None
        assert self._user_data_stream is not None
        assert self._watchlist_manager is not None
        assert self._candle_buffer is not None
        assert self._strategy is not None
        assert self._telegram is not None

        # TickerStream → WatchlistManager
        self._ticker_stream.on_ticker_update = self._on_ticker_update

        # WatchlistManager → KlineStream subscribe/unsubscribe + Telegram
        self._watchlist_manager.on_watchlist_changed = self._on_watchlist_changed

        # KlineStream → CandleBuffer + strategy on_candle_closed
        self._kline_stream.on_candle_closed = self._on_candle_closed

        # UserDataStream → exchange-side order reconciliation
        self._user_data_stream.on_order_update = self._on_order_update

        # RiskGuard → flatten all positions on halt
        self._risk_guard.on_flatten_all = self._flatten_all_positions

        # TickerStream reconnection callbacks
        self._ticker_stream.on_disconnect_timeout = self._on_disconnect_timeout
        self._ticker_stream.on_reconnected = self._on_reconnected

        # UserDataStream reconnection callbacks
        self._user_data_stream.on_disconnect_timeout = self._on_disconnect_timeout
        self._user_data_stream.on_reconnected = self._on_reconnected

        # KlineStream reconnection callbacks
        self._kline_stream.on_disconnect_timeout = self._on_disconnect_timeout
        self._kline_stream.on_reconnected = self._on_reconnected
        self._kline_stream.on_symbol_reconnected = self._on_symbol_reconnected

        log.info("All component callbacks wired")

    # ------------------------------------------------------------------
    # Callback implementations
    # ------------------------------------------------------------------

    async def _on_ticker_update(self, tickers: list[Any]) -> None:
        """Forward ticker data to WatchlistManager and trigger refresh.

        Args:
            tickers: List of TickerData from TickerStream.
        """
        assert self._watchlist_manager is not None
        for ticker in tickers:
            try:
                price = float(ticker.last_price)
            except (TypeError, ValueError):
                continue
            if price > 0:
                self._latest_ticker_prices[ticker.symbol] = price

        self._watchlist_manager.update_tickers(tickers)
        await self._check_position_exits_from_tickers(tickers)
        await self._watchlist_manager.refresh()

    async def _check_position_exits_from_tickers(self, tickers: list[Any]) -> None:
        """Run live-price exit checks for currently open positions."""
        if self._position_manager is None:
            return

        open_symbols = {
            position.symbol
            for position in self._position_manager.get_open_positions()
        }
        if not open_symbols:
            return

        for ticker in tickers:
            if ticker.symbol not in open_symbols:
                continue
            try:
                await self._position_manager.check_exits(
                    ticker.symbol,
                    ticker.last_price,
                )
            except Exception:
                log.exception(
                    "Error checking exits for {symbol} from ticker update",
                    symbol=ticker.symbol,
                )

    async def _on_watchlist_changed(
        self,
        added: list[str],
        removed: list[str],
    ) -> None:
        """Handle watchlist changes: subscribe/unsubscribe KlineStream + Telegram.

        Args:
            added: Symbols added to the watchlist.
            removed: Symbols removed from the watchlist.
        """
        assert self._kline_stream is not None
        assert self._telegram is not None
        assert self._position_manager is not None

        log.info(
            "watchlist | Applying change: added={added}, removed={removed}",
            added=added,
            removed=removed,
        )

        # Subscribe new symbols to KlineStream
        for symbol in added:
            try:
                await self._kline_stream.subscribe(symbol)
            except Exception:
                log.exception(
                    "Failed to subscribe KlineStream for {symbol}",
                    symbol=symbol,
                )

        # Unsubscribe removed symbols (only if no open position)
        for symbol in removed:
            if not self._position_manager.has_position(symbol):
                try:
                    await self._kline_stream.unsubscribe(symbol)
                except Exception:
                    log.exception(
                        "Failed to unsubscribe KlineStream for {symbol}",
                        symbol=symbol,
                    )

        # Send Telegram notification
        try:
            await self._telegram.notify_watchlist_changed(added, removed)
        except Exception:
            log.exception("Failed to send watchlist change notification")

    async def _on_candle_closed(
        self,
        symbol: str,
        timeframe: str,
        candle_data: dict,
    ) -> None:
        """Add candle to buffer and trigger strategy evaluation.

        Args:
            symbol: The trading pair symbol.
            timeframe: The candle timeframe (e.g. "3m", "15m").
            candle_data: OHLCV candle dict.
        """
        assert self._candle_buffer is not None
        assert self._strategy is not None

        await self._candle_buffer.add(symbol, timeframe, candle_data)
        await self._strategy.on_candle_closed(symbol, timeframe)

    async def _on_order_update(self, update: OrderUpdate) -> None:
        """Reconcile exchange-side fills that reduce a tracked position.

        Handles ALL external close events — protective stops, manual
        closes, liquidations, ADL, and any other reduce-only order that
        did not originate from the bot's local exit flow.

        Bot-originated orders (client_order_id starting with ``csb_``)
        are skipped because the local PositionManager exit flow already
        handles them.

        For partial fills (PARTIALLY_FILLED), the fill data is accumulated
        **and** applied to the local position immediately so that qty/PnL
        stay in sync even if the stream drops before the final FILLED event.
        """
        if self._position_manager is None:
            return

        position = self._position_manager.get_position(update.symbol)
        if position is None:
            return

        if not self._is_relevant_external_fill(update, position):
            return

        status = update.status.upper()

        # --- Apply partial fills immediately to keep local state in sync ---
        if status == "PARTIALLY_FILLED" and update.last_fill_qty > 0:
            # Accumulate raw totals BEFORE applying, but do NOT mark as
            # applied yet — only mark after successful reconciliation to
            # avoid double-counting if the reconcile call fails.
            self._accumulate_external_fill_raw(update)

            fill_price = update.last_fill_price or update.avg_price
            pnl = update.realized_pnl_usdt if update.execution_type.upper() == "TRADE" else None
            try:
                await self._position_manager.reconcile_exchange_close(
                    symbol=update.symbol,
                    exit_price=fill_price,
                    reason=self._classify_external_exit(update, position),
                    realized_pnl_usdt=pnl,
                    closed_quantity=update.last_fill_qty,
                )
            except Exception:
                log.exception(
                    "Order update partial-fill reconciliation failed for {symbol}",
                    symbol=update.symbol,
                )
                return

            # Reconcile succeeded — now mark this leg as applied
            fill = self._external_stop_fills.get(update.order_id)
            if fill is not None:
                fill["applied_qty"] += update.last_fill_qty
                fill["applied_pnl"] += pnl if pnl is not None else 0.0

            # Track order_id → symbol mapping for REST reconciliation
            self._pending_fill_symbols[update.order_id] = update.symbol
            return

        if status != "FILLED":
            # Accumulate non-trade events (e.g. NEW) without applying
            self._accumulate_external_fill_raw(update)
            return

        # --- FILLED: final fill event ---
        # Accumulate the final leg's raw data
        self._accumulate_external_fill_raw(update)

        fill = self._external_stop_fills.pop(update.order_id, {})
        self._pending_fill_symbols.pop(update.order_id, None)

        # Determine the remaining qty that hasn't been applied yet.
        # Partial fills were already applied incrementally, so only the
        # last fill leg (the one that moved status to FILLED) is pending.
        already_applied = fill.get("applied_qty", 0.0)
        total_filled = (
            fill.get("qty", 0.0)
            or update.cumulative_filled_qty
            or position.quantity
        )
        remaining_qty = max(total_filled - already_applied, 0.0)
        if remaining_qty <= 0:
            remaining_qty = position.quantity

        exit_price = self._external_fill_price(update, fill)

        # PnL: use only the portion not yet applied
        total_pnl = self._external_realized_pnl(update, fill)
        already_applied_pnl = fill.get("applied_pnl", 0.0)
        if total_pnl is not None:
            remaining_pnl: float | None = total_pnl - already_applied_pnl
        else:
            remaining_pnl = None

        exit_reason = self._classify_external_exit(update, position)

        try:
            reconciled = await self._position_manager.reconcile_exchange_close(
                symbol=update.symbol,
                exit_price=exit_price,
                reason=exit_reason,
                realized_pnl_usdt=remaining_pnl,
                closed_quantity=remaining_qty,
            )
        except Exception:
            log.exception(
                "Order update reconciliation failed for {symbol}",
                symbol=update.symbol,
            )
            return

        if reconciled:
            log.warning(
                "🔁 Order update reconciled external close for {symbol} | "
                "reason={reason} order_type={order_type} order_id={order_id}",
                symbol=update.symbol,
                reason=exit_reason.value,
                order_type=update.order_type,
                order_id=update.order_id,
            )

    def _is_relevant_external_fill(
        self,
        update: OrderUpdate,
        position: Position,
    ) -> bool:
        """Return True for any fill that reduces a tracked position.

        Accepts protective stops, manual market/limit closes,
        liquidations, and ADL — anything on the closing side that is
        not a bot-originated order.

        Bot-originated orders use a ``csb_`` prefix on the client order
        ID and are already handled by the local exit flow.
        """
        if update.order_id <= 0:
            return False

        status = update.status.upper()
        if status not in {"PARTIALLY_FILLED", "FILLED"}:
            return False

        # Must be on the closing side of the position
        if update.side.upper() != self._closing_side(position.side).value:
            return False

        # Skip bot-originated orders — the local exit flow handles them.
        # The bot's own protective stop is an exception: it was placed by
        # the bot but fills on the exchange side without local involvement,
        # so it must be reconciled here.
        if update.client_order_id.startswith("csb_"):
            order_type = update.order_type.upper()
            if order_type not in {"STOP", "STOP_MARKET"}:
                return False

        return True

    def _classify_external_exit(
        self,
        update: OrderUpdate,
        position: Position,
    ) -> ExitReason:
        """Determine the ExitReason for an external fill.

        Classification priority:
        1. Liquidation (maker_type == "LIQUIDATION") → LIQUIDATION
        2. Protective stop (STOP/STOP_MARKET matching position) → SL
        3. Everything else (manual close, limit, etc.) → EXTERNAL
        """
        if update.maker_type.upper() == "LIQUIDATION":
            return ExitReason.LIQUIDATION

        order_type = update.order_type.upper()
        if order_type in {"STOP", "STOP_MARKET"}:
            return ExitReason.SL

        return ExitReason.EXTERNAL

    def _accumulate_external_fill_raw(self, update: OrderUpdate) -> None:
        """Accumulate raw per-trade fill data until the order is FILLED.

        Only tracks raw totals (qty, pnl, notional). The ``applied_qty``
        and ``applied_pnl`` fields are updated separately by the caller
        after a successful reconciliation to avoid double-counting on
        failure.
        """
        if update.execution_type.upper() != "TRADE" or update.last_fill_qty <= 0:
            return

        fill = self._external_stop_fills.setdefault(
            update.order_id,
            {"qty": 0.0, "pnl": 0.0, "notional": 0.0, "applied_qty": 0.0, "applied_pnl": 0.0},
        )
        fill["qty"] += update.last_fill_qty
        fill["pnl"] += update.realized_pnl_usdt
        price = update.last_fill_price or update.avg_price
        if price > 0:
            fill["notional"] += price * update.last_fill_qty

    def _external_fill_price(
        self,
        update: OrderUpdate,
        fill: dict[str, float],
    ) -> float:
        qty = fill.get("qty", 0.0)
        notional = fill.get("notional", 0.0)
        if qty > 0 and notional > 0:
            return notional / qty
        if update.avg_price > 0:
            return update.avg_price
        if update.last_fill_price > 0:
            return update.last_fill_price
        return self._latest_ticker_prices.get(update.symbol, 0.0)

    @staticmethod
    def _external_realized_pnl(
        update: OrderUpdate,
        fill: dict[str, float],
    ) -> float | None:
        if fill:
            return fill.get("pnl", 0.0)
        if update.execution_type.upper() == "TRADE":
            return update.realized_pnl_usdt
        return None

    async def _on_disconnect_timeout(self) -> None:
        """Handle WebSocket disconnect timeout — close all positions."""
        log.warning("⚡ WebSocket disconnect timeout — closing all positions")
        if self._strategy is not None:
            try:
                await self._strategy.close_all_positions()
            except Exception:
                log.exception("Error closing positions on disconnect timeout")

    async def _flatten_all_positions(self) -> None:
        """Force-close every open position at market price on risk halt.

        Called by RiskGuard via the ``on_flatten_all`` callback when a
        daily-loss or session-drawdown halt is triggered.  Each position
        is closed individually so a single failure does not prevent the
        remaining positions from being flattened.
        """
        if self._position_manager is None:
            return

        positions = self._position_manager.get_open_positions()
        if not positions:
            log.info("risk_halt | No open positions to flatten")
            return

        log.warning(
            "risk_halt | ⛔ Flattening {n} open position(s)",
            n=len(positions),
        )

        for position in positions:
            try:
                price = self._latest_ticker_prices.get(position.symbol, 0.0)
                if price <= 0 and self._order_manager is not None:
                    try:
                        price = await self._order_manager.get_symbol_price(
                            position.symbol,
                        )
                    except Exception:
                        price = position.entry_price

                await self._position_manager.force_close(
                    position.symbol,
                    fallback_price=price,
                    reason=ExitReason.HALT,
                )
                log.warning(
                    "risk_halt | Flattened {symbol}",
                    symbol=position.symbol,
                )
            except Exception:
                log.exception(
                    "risk_halt | Failed to flatten {symbol}",
                    symbol=position.symbol,
                )

    async def _on_reconnected(self, duration_sec: float) -> None:
        """Handle WebSocket reconnection — reconcile pending fills and alert.

        After any stream reconnection, pending partial fills may have
        completed on the exchange while the stream was down. Query the
        exchange via REST to resolve them before resuming normal flow.

        Args:
            duration_sec: Duration of the disconnection in seconds.
        """
        await self._reconcile_pending_fills()

        if self._telegram is not None:
            try:
                await self._telegram.notify_reconnected(duration_sec)
            except Exception:
                log.exception("Error sending reconnection notification")

    async def _reconcile_pending_fills(self) -> None:
        """Resolve partial fills stranded in ``_external_stop_fills`` via REST.

        When the user-data stream drops mid-fill, accumulated partial fill
        data sits in ``_external_stop_fills`` without a terminal FILLED
        event. This method queries each pending order via REST:

        - If the order is now FILLED: complete the reconciliation with
          accurate fill data from the exchange.
        - If the order is still PARTIALLY_FILLED: update the accumulated
          data and let the stream continue when it reconnects.
        - If the order is CANCELED/EXPIRED: clean up the stale entry.
        """
        if not self._external_stop_fills or self._order_manager is None or self._position_manager is None:
            return

        pending = dict(self._external_stop_fills)
        log.info(
            "reconciliation | Checking {count} pending partial fill(s) via REST",
            count=len(pending),
        )

        for order_id, fill in pending.items():
            # We need the symbol to query the order — find it from tracked positions
            symbol = self._find_symbol_for_stop_order(order_id)
            if not symbol:
                log.warning(
                    "reconciliation | Cannot find symbol for pending order_id={order_id}, skipping",
                    order_id=order_id,
                )
                continue

            position = self._position_manager.get_position(symbol)
            if position is None:
                # Position already closed by another path — clean up
                self._external_stop_fills.pop(order_id, None)
                self._pending_fill_symbols.pop(order_id, None)
                continue

            try:
                rest_order = await self._order_manager.get_order(symbol, order_id)
            except Exception:
                log.warning(
                    "reconciliation | Failed to query order {order_id} for {symbol}",
                    order_id=order_id,
                    symbol=symbol,
                )
                continue

            if rest_order is None:
                log.warning(
                    "reconciliation | Order {order_id} not found on exchange for {symbol}",
                    order_id=order_id,
                    symbol=symbol,
                )
                continue

            # Re-check after the REST await — the stream callback may have
            # already resolved this order while we were waiting.
            if order_id not in self._external_stop_fills:
                log.debug(
                    "reconciliation | Order {order_id} for {symbol} resolved by stream during REST query",
                    order_id=order_id,
                    symbol=symbol,
                )
                continue

            position = self._position_manager.get_position(symbol)
            if position is None:
                self._external_stop_fills.pop(order_id, None)
                self._pending_fill_symbols.pop(order_id, None)
                continue

            status = rest_order.status.upper()
            already_applied_qty = fill.get("applied_qty", 0.0)
            already_applied_pnl = fill.get("applied_pnl", 0.0)

            if status == "FILLED":
                # Order completed while stream was down — finish reconciliation
                rest_qty = rest_order.quantity
                rest_price = rest_order.avg_price

                remaining_qty = max(rest_qty - already_applied_qty, 0.0)
                if remaining_qty <= 0:
                    remaining_qty = position.quantity

                # PnL from REST avg_price for the unapplied portion
                if rest_price > 0 and remaining_qty > 0:
                    if position.side == SignalDirection.LONG:
                        remaining_pnl: float | None = (rest_price - position.entry_price) * remaining_qty
                    else:
                        remaining_pnl = (position.entry_price - rest_price) * remaining_qty
                else:
                    remaining_pnl = None

                exit_price = rest_price if rest_price > 0 else self._latest_ticker_prices.get(symbol, position.sl_price)

                try:
                    reconciled = await self._position_manager.reconcile_exchange_close(
                        symbol=symbol,
                        exit_price=exit_price,
                        reason=ExitReason.SL,
                        realized_pnl_usdt=remaining_pnl,
                        closed_quantity=remaining_qty,
                    )
                except Exception:
                    log.exception(
                        "reconciliation | Failed to reconcile completed stop {order_id} for {symbol}",
                        order_id=order_id,
                        symbol=symbol,
                    )
                    continue

                # Clean up only after successful reconciliation so the
                # entry survives for retry on the next cycle if it fails.
                self._external_stop_fills.pop(order_id, None)
                self._pending_fill_symbols.pop(order_id, None)

                if reconciled:
                    log.warning(
                        "reconciliation | 🔁 Resolved pending stop fill via REST: "
                        "{symbol} order_id={order_id} price={price} qty={qty}",
                        symbol=symbol,
                        order_id=order_id,
                        price=exit_price,
                        qty=rest_qty,
                    )
                    if self._telegram is not None:
                        try:
                            await self._telegram.notify_reconciliation(
                                symbol=symbol,
                                action="pending_fill_resolved",
                                details=(
                                    f"Stop order {order_id} completed during stream gap. "
                                    f"Reconciled at price={exit_price:.6f} qty={rest_qty:.6f}"
                                ),
                            )
                        except Exception:
                            log.warning(
                                "reconciliation | Failed to send pending fill alert for {symbol}",
                                symbol=symbol,
                            )

            elif status == "PARTIALLY_FILLED":
                # Still partially filled — apply any fills missed during the gap.
                # NOTE: rest_order.avg_price is the weighted average across ALL
                # fills, not just the missed ones. Using it for the missed portion
                # is an approximation; the error is bounded by the price spread
                # during the fill sequence, which is typically negligible for
                # stop orders.
                rest_qty = rest_order.quantity
                if rest_qty > already_applied_qty:
                    missed_qty = rest_qty - already_applied_qty
                    rest_price = rest_order.avg_price

                    if rest_price <= 0:
                        log.warning(
                            "reconciliation | No avg_price for partial fill {order_id} on {symbol}, "
                            "skipping PnL — will retry next cycle",
                            order_id=order_id,
                            symbol=symbol,
                        )
                        continue

                    if position.side == SignalDirection.LONG:
                        missed_pnl: float | None = (rest_price - position.entry_price) * missed_qty
                    else:
                        missed_pnl = (position.entry_price - rest_price) * missed_qty

                    try:
                        await self._position_manager.reconcile_exchange_close(
                            symbol=symbol,
                            exit_price=rest_price,
                            reason=ExitReason.SL,
                            realized_pnl_usdt=missed_pnl,
                            closed_quantity=missed_qty,
                        )
                    except Exception:
                        log.exception(
                            "reconciliation | Failed to apply missed partial fill for {symbol}",
                            symbol=symbol,
                        )
                        continue

                    fill["applied_qty"] = rest_qty
                    fill["applied_pnl"] = fill.get("applied_pnl", 0.0) + (missed_pnl or 0.0)
                    fill["qty"] = rest_qty

                    log.info(
                        "reconciliation | Updated pending partial fill from REST: "
                        "{symbol} order_id={order_id} applied_qty={qty}",
                        symbol=symbol,
                        order_id=order_id,
                        qty=rest_qty,
                    )

            elif status in {"CANCELED", "EXPIRED", "REJECTED"}:
                # Order is terminal but not filled — clean up
                self._external_stop_fills.pop(order_id, None)
                self._pending_fill_symbols.pop(order_id, None)
                log.warning(
                    "reconciliation | Pending stop order {order_id} for {symbol} "
                    "ended with status={status}, cleaning up",
                    order_id=order_id,
                    symbol=symbol,
                    status=status,
                )

    def _find_symbol_for_stop_order(self, order_id: int) -> str | None:
        """Find the symbol associated with a pending fill order.

        Checks two sources:
        1. ``_pending_fill_symbols`` — populated when partial fills are
           received via the user-data stream.
        2. Position ``stop_order_id`` — matches the bot's own protective
           stop orders.
        """
        # Fast path: explicit mapping from stream events
        symbol = self._pending_fill_symbols.get(order_id)
        if symbol is not None:
            return symbol

        # Fallback: scan tracked positions for matching stop_order_id
        if self._position_manager is None:
            return None

        for position in self._position_manager.get_open_positions():
            if position.stop_order_id == order_id:
                return position.symbol

        return None

    async def _on_symbol_reconnected(self, symbol: str) -> None:
        """Backfill candle buffer from REST after a per-symbol reconnect.

        Fetches the most recent historical klines for each configured
        timeframe via the Binance REST API and replaces the in-memory
        buffer so the strategy never evaluates signals on stale or
        gap-ridden data.

        Args:
            symbol: The trading pair symbol that just reconnected.
        """
        assert self._candle_buffer is not None
        assert self._order_manager is not None
        assert self._order_manager._client is not None

        client: BinanceClient = self._order_manager._client
        timeframes = [
            self._config.strategy.signal_timeframe,
            self._config.strategy.trend_timeframe,
        ]
        limit = self._config.strategy.candle_buffer_size

        for tf in timeframes:
            try:
                candles = await self._fetch_rest_klines(client, symbol, tf, limit)
                await self._candle_buffer.backfill(symbol, tf, candles)
                log.info(
                    "Resync | Backfilled {symbol}:{tf} with {n} candles from REST",
                    symbol=symbol,
                    tf=tf,
                    n=len(candles),
                )
            except Exception:
                log.exception(
                    "Resync | Failed to backfill {symbol}:{tf} from REST",
                    symbol=symbol,
                    tf=tf,
                )

    @staticmethod
    async def _fetch_rest_klines(
        client: BinanceClient,
        symbol: str,
        interval: str,
        limit: int,
    ) -> list[dict]:
        """Fetch closed historical klines from the Binance REST API.

        Requests ``limit + 1`` rows and drops the last one because the
        Binance API includes the currently-open (incomplete) candle as
        the final entry.

        Args:
            client: A ``BinanceClient`` instance.
            symbol: Trading pair symbol (e.g. ``"SOLUSDT"``).
            interval: Kline interval string (e.g. ``"3m"``, ``"15m"``).
            limit: Maximum number of closed candles to return.

        Returns:
            List of candle dicts (oldest first) with keys:
            open, high, low, close, volume, timestamp.
        """
        raw = await client.futures_klines(
            symbol=symbol,
            interval=interval,
            limit=limit + 1,
        )

        # Drop the last row — it is the currently-open candle.
        closed_rows = raw[:-1] if raw else []

        candles: list[dict] = []
        for row in closed_rows:
            candles.append({
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
                "timestamp": int(row[0]),
            })
        return candles

    # ------------------------------------------------------------------
    # Periodic exchange reconciliation
    # ------------------------------------------------------------------

    async def _periodic_reconciliation_loop(self) -> None:
        """Poll exchange positions periodically to catch missed fills.

        When the user-data WebSocket disconnects and reconnects, any
        ORDER_TRADE_UPDATE events that occurred during the gap are lost.
        This loop detects two kinds of drift:

        1. **Phantom positions** — tracked locally but no longer on the
           exchange (the fill event was missed). These are reconciled by
           removing the local position and emitting the normal close
           callback.
        2. **Orphan positions** — present on the exchange but not tracked
           locally (e.g. manual trade). These are logged as warnings.

        The interval is driven by ``risk.reconciliation_interval_sec``
        from config.yaml.
        """
        interval = self._config.risk.reconciliation_interval_sec
        log.info(
            "reconciliation | Periodic loop started — interval={interval}s",
            interval=interval,
        )

        while True:
            await asyncio.sleep(interval)
            try:
                await self._reconcile_pending_fills()
                await self._reconcile_exchange_positions()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("reconciliation | Periodic reconciliation cycle failed")

    async def _reconcile_exchange_positions(self) -> None:
        """Run one reconciliation cycle against the exchange.

        Fetches all non-zero positions from Binance and compares them
        with the in-memory tracked positions.
        """
        if self._order_manager is None or self._position_manager is None:
            return

        try:
            exchange_positions = await self._order_manager.get_open_positions()
        except Exception:
            log.warning("reconciliation | Failed to fetch exchange positions, skipping cycle")
            return

        exchange_symbols: dict[str, Any] = {
            ep.symbol: ep for ep in exchange_positions
        }
        tracked_positions = self._position_manager.get_open_positions()

        # --- Detect phantom positions (local but not on exchange) ---
        for position in tracked_positions:
            if position.symbol not in exchange_symbols:
                await self._handle_phantom_position(position)
            else:
                # Position exists on both sides — check for quantity drift
                exchange_pos = exchange_symbols[position.symbol]
                await self._handle_quantity_drift(position, exchange_pos)

        # --- Detect orphan positions (exchange but not local) ---
        tracked_symbols = {p.symbol for p in tracked_positions}
        for symbol, exchange_pos in exchange_symbols.items():
            if symbol not in tracked_symbols:
                log.warning(
                    "reconciliation | Orphan position on exchange: {symbol} "
                    "qty={qty} side={side} — not tracked locally",
                    symbol=symbol,
                    qty=exchange_pos.quantity,
                    side=exchange_pos.side.value,
                )
                if self._telegram is not None:
                    try:
                        await self._telegram.notify_reconciliation(
                            symbol=symbol,
                            action="orphan_detected",
                            details=(
                                f"Exchange has {exchange_pos.side.value} "
                                f"qty={exchange_pos.quantity} but bot is not tracking it"
                            ),
                        )
                    except Exception:
                        log.warning(
                            "reconciliation | Failed to send orphan alert for {symbol}",
                            symbol=symbol,
                        )

    async def _handle_phantom_position(self, position: Position) -> None:
        """Reconcile a position tracked locally but gone from the exchange.

        The exchange-side fill was missed (user-data stream gap). We
        resolve this by fetching the last trade for the symbol to get
        the actual exit price, then reconciling the local position.
        """
        log.warning(
            "reconciliation | 👻 Phantom position detected: {symbol} — "
            "tracked locally but not on exchange, reconciling",
            symbol=position.symbol,
        )

        exit_price = await self._estimate_phantom_exit_price(position)

        try:
            reconciled = await self._position_manager.reconcile_exchange_close(
                symbol=position.symbol,
                exit_price=exit_price,
                reason=ExitReason.RECONCILED,
                realized_pnl_usdt=None,
                closed_quantity=position.quantity,
            )
        except Exception:
            log.exception(
                "reconciliation | Failed to reconcile phantom {symbol}",
                symbol=position.symbol,
            )
            return

        if reconciled:
            log.warning(
                "reconciliation | Phantom {symbol} reconciled — "
                "exit_price={price} reason=RECONCILED",
                symbol=position.symbol,
                price=exit_price,
            )
            if self._telegram is not None:
                try:
                    await self._telegram.notify_reconciliation(
                        symbol=position.symbol,
                        action="phantom_closed",
                        details=(
                            f"Position was closed on exchange but event was missed. "
                            f"Reconciled at price={exit_price:.6f}"
                        ),
                    )
                except Exception:
                    log.warning(
                        "reconciliation | Failed to send phantom alert for {symbol}",
                        symbol=position.symbol,
                    )

    async def _handle_quantity_drift(
        self,
        position: Position,
        exchange_pos: Any,
    ) -> None:
        """Detect and reconcile when local and exchange quantities diverge.

        A partial fill may have been missed. If the exchange quantity is
        smaller than the local quantity, a partial close happened that
        the bot didn't see. Queries the stop order via REST when possible
        to get accurate fill price/PnL instead of estimating.
        """
        local_qty = position.quantity
        exchange_qty = exchange_pos.quantity

        # Allow small floating-point tolerance
        tolerance = max(local_qty * 1e-6, 1e-8)
        if abs(local_qty - exchange_qty) <= tolerance:
            return

        if exchange_qty < local_qty:
            missed_qty = local_qty - exchange_qty

            # Try to get accurate fill data from the stop order via REST
            fill_price = 0.0
            fill_pnl: float | None = None
            if position.stop_order_id > 0 and self._order_manager is not None:
                try:
                    rest_order = await self._order_manager.get_order(
                        position.symbol, position.stop_order_id,
                    )
                    if rest_order is not None and rest_order.avg_price > 0:
                        fill_price = rest_order.avg_price
                        if position.side == SignalDirection.LONG:
                            fill_pnl = (fill_price - position.entry_price) * missed_qty
                        else:
                            fill_pnl = (position.entry_price - fill_price) * missed_qty
                        log.info(
                            "reconciliation | Got fill data from stop order REST: "
                            "{symbol} order_id={order_id} price={price}",
                            symbol=position.symbol,
                            order_id=position.stop_order_id,
                            price=fill_price,
                        )
                except Exception:
                    log.warning(
                        "reconciliation | Failed to query stop order for drift: {symbol}",
                        symbol=position.symbol,
                    )

            log.warning(
                "reconciliation | ⚠️ Quantity drift for {symbol}: "
                "local={local_qty} exchange={exchange_qty} — "
                "partial fill may have been missed",
                symbol=position.symbol,
                local_qty=local_qty,
                exchange_qty=exchange_qty,
            )

            # Apply the missed partial close with fill data if available
            position_still_open = True
            if fill_price > 0:
                try:
                    reconciled = await self._position_manager.reconcile_exchange_close(
                        symbol=position.symbol,
                        exit_price=fill_price,
                        reason=ExitReason.RECONCILED,
                        realized_pnl_usdt=fill_pnl,
                        closed_quantity=missed_qty,
                    )
                    if reconciled:
                        position_still_open = False
                except Exception:
                    log.exception(
                        "reconciliation | Failed to reconcile drift for {symbol}",
                        symbol=position.symbol,
                    )
                    # Fall back to direct qty adjustment
                    position.quantity = exchange_qty
            else:
                # No fill data available — adjust qty directly
                position.quantity = exchange_qty

            # Only adjust TP1/breakeven if the position is still open
            if position_still_open and not position.tp1_hit and exchange_qty < position.original_quantity:
                position.tp1_hit = True
                position.sl_price = position.entry_price
                log.info(
                    "reconciliation | Marked TP1 hit for {symbol} due to "
                    "quantity drift, SL moved to breakeven",
                    symbol=position.symbol,
                )

            if self._telegram is not None:
                try:
                    await self._telegram.notify_reconciliation(
                        symbol=position.symbol,
                        action="quantity_drift",
                        details=(
                            f"Local qty={local_qty:.6f} → exchange qty={exchange_qty:.6f} "
                            f"(missed partial close of {missed_qty:.6f})"
                            + (f" fill_price={fill_price:.6f}" if fill_price > 0 else "")
                        ),
                    )
                except Exception:
                    log.warning(
                        "reconciliation | Failed to send drift alert for {symbol}",
                        symbol=position.symbol,
                    )
        else:
            # Exchange has more than local — unusual, just log
            log.warning(
                "reconciliation | Exchange quantity exceeds local for {symbol}: "
                "local={local_qty} exchange={exchange_qty}",
                symbol=position.symbol,
                local_qty=local_qty,
                exchange_qty=exchange_qty,
            )

    async def _estimate_phantom_exit_price(self, position: Position) -> float:
        """Best-effort estimate of exit price for a phantom position.

        Tries in order:
        1. Latest ticker price from cache
        2. REST API symbol price
        3. Stop-loss price as last resort
        """
        # Try cached ticker price
        cached = self._latest_ticker_prices.get(position.symbol, 0.0)
        if cached > 0:
            return cached

        # Try REST API
        if self._order_manager is not None:
            try:
                return await self._order_manager.get_symbol_price(position.symbol)
            except Exception:
                log.warning(
                    "reconciliation | Failed to fetch price for phantom {symbol}",
                    symbol=position.symbol,
                )

        # Fallback to SL price
        return position.sl_price

    # ------------------------------------------------------------------
    # Startup recovery
    # ------------------------------------------------------------------

    async def _recover_open_positions(self) -> None:
        """Recover live Binance positions into in-memory state on startup."""
        assert self._order_manager is not None
        assert self._position_manager is not None

        open_trades = await self._load_open_trades_for_recovery()
        trades_by_symbol = {trade.symbol: trade for trade in open_trades}

        try:
            exchange_positions = await self._order_manager.get_open_positions()
        except Exception:
            log.exception("Failed to load open positions from Binance during recovery")
            raise

        if not exchange_positions:
            log.info("Recovery | No open Binance positions found")
            return

        for exchange_position in exchange_positions:
            trade = trades_by_symbol.get(exchange_position.symbol)
            position = await self._restore_exchange_position(exchange_position, trade)
            await self._ensure_recovered_stop(position)

        log.info(
            "Recovery | Restored {n} open position(s)",
            n=len(exchange_positions),
        )

    async def _load_open_trades_for_recovery(self) -> list[OpenTradeRecord]:
        assert self._trade_repo is not None

        try:
            return await self._trade_repo.get_open_trades()
        except Exception:
            log.exception("Recovery | Failed to load OPEN trades from DB; continuing with exchange state")
            return []

    async def _restore_exchange_position(
        self,
        exchange_position: Any,
        trade: OpenTradeRecord | None,
    ) -> Position:
        assert self._position_manager is not None
        assert self._trade_repo is not None

        side = self._recovery_side(exchange_position, trade)
        entry_price = (
            trade.entry_price
            if trade is not None and trade.entry_price > 0
            else exchange_position.entry_price
        )
        leverage = (
            trade.leverage
            if trade is not None and trade.leverage > 0
            else exchange_position.leverage or self._config.risk.leverage
        )
        original_quantity = (
            trade.quantity
            if trade is not None and trade.quantity > 0
            else exchange_position.quantity
        )
        opened_at = trade.entry_at if trade is not None else utc_now()
        trade_id = trade.id if trade is not None else 0

        # When no DB trade matches the exchange position, insert a
        # surrogate OPEN record so close_trade() will find it later.
        if trade_id == 0:
            trade_id = await self._insert_surrogate_trade(
                symbol=exchange_position.symbol,
                side=side,
                entry_price=entry_price,
                quantity=original_quantity,
                leverage=leverage,
                opened_at=opened_at,
            )

        return self._position_manager.restore(
            symbol=exchange_position.symbol,
            side=side,
            entry_price=entry_price,
            quantity=exchange_position.quantity,
            original_quantity=original_quantity,
            leverage=leverage,
            opened_at=opened_at,
            trade_id=trade_id,
        )

    @staticmethod
    def _recovery_side(exchange_position: Any, trade: OpenTradeRecord | None) -> SignalDirection:
        if trade is not None:
            try:
                return SignalDirection(trade.side)
            except ValueError:
                log.warning(
                    "Recovery | Invalid DB side for {symbol}: {side}; using exchange side",
                    symbol=trade.symbol,
                    side=trade.side,
                )
        return exchange_position.side

    async def _insert_surrogate_trade(
        self,
        symbol: str,
        side: SignalDirection,
        entry_price: float,
        quantity: float,
        leverage: int,
        opened_at: datetime,
    ) -> int:
        """Insert a surrogate OPEN trade for a recovered exchange position.

        When the bot restarts and finds a live Binance position with no
        matching DB record, this creates one so that ``close_trade()``
        will find it later.  Best-effort — returns 0 on failure so the
        position is still managed in memory.

        Args:
            symbol: Trading pair symbol.
            side: LONG or SHORT direction.
            entry_price: Position entry price.
            quantity: Original position quantity.
            leverage: Leverage multiplier.
            opened_at: Estimated open time.

        Returns:
            The new trade row ID, or 0 if the insert failed.
        """
        assert self._trade_repo is not None

        record = TradeRecord(
            symbol=symbol,
            side=side.value,
            entry_price=entry_price,
            quantity=quantity,
            leverage=leverage,
            entry_at=opened_at,
            signal_snapshot="{}",
        )
        try:
            trade_id = await self._trade_repo.insert_trade(record)
            log.info(
                "Recovery | Inserted surrogate trade {id} for {symbol} "
                "(no DB match found)",
                id=trade_id,
                symbol=symbol,
            )
            if self._telegram is not None:
                try:
                    await self._telegram.send(
                        f"🔄 RECOVERY | {symbol} {side.value} restored from "
                        f"exchange with no DB record — surrogate trade #{trade_id} created"
                    )
                except Exception:
                    log.warning(
                        "Recovery | Failed to send surrogate trade Telegram alert for {symbol}",
                        symbol=symbol,
                    )
            return trade_id
        except Exception:
            log.exception(
                "Recovery | Failed to insert surrogate trade for {symbol}; "
                "position will run with trade_id=0",
                symbol=symbol,
            )
            return 0

    async def _ensure_recovered_stop(self, position: Position) -> None:
        """Attach or place an exchange-side stop for a recovered position."""
        assert self._order_manager is not None
        assert self._position_manager is not None

        stop_side = self._closing_side(position.side)
        try:
            open_stops = await self._order_manager.get_open_stop_orders(position.symbol)
        except Exception:
            log.exception(
                "Recovery | Failed to load open stop orders for {symbol}",
                symbol=position.symbol,
            )
            open_stops = []

        matching_stops = [stop for stop in open_stops if stop.side == stop_side.value]
        if matching_stops:
            existing_stop = matching_stops[0]
            position.stop_order_id = existing_stop.order_id
            await self._align_recovered_stop_price(position, existing_stop)
            log.info(
                "Recovery | Attached existing stop for {symbol}: order_id={order_id}",
                symbol=position.symbol,
                order_id=position.stop_order_id,
            )
            return

        try:
            stop_result = await self._order_manager.place_stop_loss(
                symbol=position.symbol,
                side=stop_side,
                stop_price=position.sl_price,
            )
        except Exception:
            log.exception(
                "Recovery | Failed to place stop for recovered {symbol}; emergency closing",
                symbol=position.symbol,
            )
            await self._emergency_close_recovered_position(position)
            return

        position.stop_order_id = stop_result.order_id
        log.info(
            "Recovery | Placed stop for recovered {symbol}: order_id={order_id}",
            symbol=position.symbol,
            order_id=position.stop_order_id,
        )

    async def _align_recovered_stop_price(self, position: Position, stop_order: Any) -> None:
        """Move an existing recovered stop when its price is stale."""
        assert self._order_manager is not None

        raw = stop_order.raw or {}
        try:
            existing_stop_price = float(raw.get("stopPrice", 0.0))
        except (TypeError, ValueError):
            existing_stop_price = 0.0

        if existing_stop_price == 0.0:
            return

        tolerance = max(position.entry_price * 0.000001, 0.00000001)
        if abs(existing_stop_price - position.sl_price) <= tolerance:
            return

        try:
            new_stop = await self._order_manager.replace_stop_loss(
                position.symbol,
                stop_order.order_id,
                self._closing_side(position.side),
                position.sl_price,
            )
        except Exception:
            log.exception(
                "Recovery | Failed to align stop price for {symbol}",
                symbol=position.symbol,
            )
            return

        position.stop_order_id = new_stop.order_id

    @staticmethod
    def _closing_side(side: SignalDirection) -> OrderSide:
        return OrderSide.SELL if side == SignalDirection.LONG else OrderSide.BUY

    async def _emergency_close_recovered_position(self, position: Position) -> None:
        assert self._order_manager is not None
        assert self._position_manager is not None

        try:
            await self._order_manager.close_position(
                position.symbol,
                self._closing_side(position.side),
                position.quantity,
            )
        except Exception:
            log.critical(
                "Recovery | Failed to emergency close unprotected {symbol}",
                symbol=position.symbol,
            )
            return

        self._position_manager.remove(position.symbol)
        log.critical(
            "Recovery | Emergency closed unprotected {symbol}",
            symbol=position.symbol,
        )

    # ------------------------------------------------------------------
    # Balance helper
    # ------------------------------------------------------------------

    async def _get_balance(self) -> float:
        """Fetch the current USDT balance from Binance.

        Returns:
            The available USDT balance as a float.
        """
        assert self._order_manager is not None
        assert self._order_manager._client is not None

        try:
            balances = await self._order_manager._client.futures_account_balance()
            for b in balances:
                if b.get("asset") == "USDT":
                    return float(b.get("balance", 0.0))
            log.warning("USDT balance not found in account response")
            return 0.0
        except Exception:
            log.exception("Failed to fetch account balance")
            return 0.0

    async def _get_free_margin_pct(self) -> float:
        """Fetch available futures margin as a percentage of wallet balance."""
        assert self._order_manager is not None
        assert self._order_manager._client is not None

        try:
            account = await self._order_manager._client.futures_account()
            available_balance = float(account.get("availableBalance", 0.0))
            wallet_balance = float(account.get("totalWalletBalance", 0.0))
            if wallet_balance <= 0:
                log.warning("Cannot calculate free margin pct: totalWalletBalance <= 0")
                return 0.0
            return available_balance / wallet_balance * 100
        except Exception:
            log.exception("Failed to fetch free margin percentage")
            return 0.0

    async def _get_current_price(self, symbol: str) -> float:
        """Return latest live price for entry sizing."""
        cached_price = self._latest_ticker_prices.get(symbol, 0.0)
        if cached_price > 0:
            return cached_price

        assert self._order_manager is not None
        try:
            return await self._order_manager.get_symbol_price(symbol)
        except Exception:
            log.exception("Failed to fetch current price for {symbol}", symbol=symbol)
            return 0.0

    async def _get_funding_rate(self, symbol: str) -> float:
        """Return current funding rate for the symbol.

        Returns 0.0 on failure so the filter passes through
        when the rate cannot be fetched (degrade gracefully).
        """
        assert self._order_manager is not None
        try:
            return await self._order_manager.get_funding_rate(symbol)
        except Exception:
            log.exception("Failed to fetch funding rate for {symbol}", symbol=symbol)
            return 0.0

    async def _get_spread_pct(self, symbol: str) -> float:
        """Return current bid-ask spread as a percentage for the symbol.

        Returns 0.0 on failure — the spread check is a gate, so 0.0
        means "cannot determine" and should be treated as safe-to-proceed.
        """
        assert self._order_manager is not None
        try:
            return await self._order_manager.get_spread_pct(symbol)
        except Exception:
            log.exception("Failed to fetch spread for {symbol}", symbol=symbol)
            return 0.0
