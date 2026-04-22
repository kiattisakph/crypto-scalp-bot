"""Unit tests for the TopGainersScalping strategy orchestrator."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from core.config import (
    AppConfig,
    EntryConfig,
    ExitConfig,
    RiskConfig,
    StrategyConfig,
    WatchlistConfig,
)
from core.enums import ExitReason, OrderSide, SignalDirection
from core.models import Position, RiskCheckResult, Signal, TradeResult
from execution.order_manager import OrderResult
from strategy.top_gainers_scalping import TopGainersScalping
from utils.candle_buffer import CandleBuffer


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


def _make_config() -> AppConfig:
    """Build a valid AppConfig for testing."""
    return AppConfig(
        watchlist=WatchlistConfig(refresh_interval_sec=300),
        strategy=StrategyConfig(
            signal_timeframe="3m",
            trend_timeframe="15m",
            candle_buffer_size=100,
            entry=EntryConfig(signal_cooldown_min=15),
            exit=ExitConfig(),
        ),
        risk=RiskConfig(leverage=5),
    )


def _make_mocks() -> dict:
    """Create all mock dependencies for TopGainersScalping."""
    watchlist_manager = MagicMock()
    watchlist_manager.refresh = AsyncMock()

    signal_engine = MagicMock()
    signal_engine.evaluate = MagicMock(return_value=None)

    risk_guard = MagicMock()
    risk_guard.check_trade = MagicMock(
        return_value=RiskCheckResult(approved=True, position_size=1.0)
    )
    risk_guard.record_pnl = MagicMock()
    risk_guard.check_halt_conditions = AsyncMock()

    order_manager = MagicMock()
    order_manager.set_leverage = AsyncMock()
    order_manager.open_position = AsyncMock(
        return_value=OrderResult(
            order_id=123,
            symbol="SOLUSDT",
            side="BUY",
            quantity=1.0,
            status="FILLED",
            avg_price=100.0,
        )
    )
    order_manager.close_position = AsyncMock(
        return_value=OrderResult(
            order_id=456,
            symbol="SOLUSDT",
            side="SELL",
            quantity=1.0,
            status="FILLED",
            avg_price=101.0,
        )
    )
    order_manager.place_stop_loss = AsyncMock(
        return_value=OrderResult(
            order_id=789,
            symbol="SOLUSDT",
            side="SELL",
            quantity=0.0,
            status="NEW",
            avg_price=0.0,
        )
    )

    position_manager = MagicMock()
    position_manager.has_position = MagicMock(return_value=False)
    position_manager.get_open_positions = MagicMock(return_value=[])
    position_manager.force_close = AsyncMock()
    position_manager.on_position_closed = None

    mock_position = Position(
        symbol="SOLUSDT",
        side=SignalDirection.LONG,
        entry_price=100.0,
        quantity=1.0,
        original_quantity=1.0,
        leverage=5,
        tp1_price=100.8,
        tp2_price=101.5,
        tp3_price=102.5,
        sl_price=99.0,
    )
    position_manager.open = MagicMock(return_value=mock_position)

    telegram = MagicMock()
    telegram.notify_position_opened = AsyncMock()
    telegram.notify_position_closed = AsyncMock()

    trade_repo = MagicMock()
    trade_repo.insert_trade = AsyncMock(return_value=42)
    trade_repo.close_trade = AsyncMock()
    trade_repo.update_daily_stats = AsyncMock()

    get_balance = AsyncMock(return_value=10000.0)
    get_free_margin_pct = AsyncMock(return_value=62.5)
    get_funding_rate = AsyncMock(return_value=0.0)  # neutral — doesn't block any direction
    get_spread_pct = AsyncMock(return_value=0.02)   # 0.02% — well within default 0.10% limit

    return {
        "watchlist_manager": watchlist_manager,
        "signal_engine": signal_engine,
        "risk_guard": risk_guard,
        "order_manager": order_manager,
        "position_manager": position_manager,
        "telegram": telegram,
        "trade_repo": trade_repo,
        "get_balance": get_balance,
        "get_free_margin_pct": get_free_margin_pct,
        "get_funding_rate": get_funding_rate,
        "get_spread_pct": get_spread_pct,
    }


@pytest_asyncio.fixture
async def candle_buffer() -> CandleBuffer:
    """Provide a CandleBuffer with enough data for signal evaluation."""
    buf = CandleBuffer(max_size=100)
    # Add enough 3m candles (need at least 21 for ema_slow).
    for i in range(50):
        await buf.add("SOLUSDT", "3m", {
            "open": 99.0 + i * 0.1,
            "high": 100.0 + i * 0.1,
            "low": 98.0 + i * 0.1,
            "close": 99.5 + i * 0.1,
            "volume": 1000.0 + i * 10,
            "timestamp": 1700000000 + i * 180,
        })
    # Add enough 15m candles (need at least 50 for ema_trend_slow).
    for i in range(60):
        await buf.add("SOLUSDT", "15m", {
            "open": 99.0 + i * 0.1,
            "high": 100.0 + i * 0.1,
            "low": 98.0 + i * 0.1,
            "close": 99.5 + i * 0.1,
            "volume": 5000.0 + i * 50,
            "timestamp": 1700000000 + i * 900,
        })
    return buf


@pytest_asyncio.fixture
async def strategy_with_buffer(candle_buffer: CandleBuffer):
    """Create a TopGainersScalping instance with real CandleBuffer and mocks."""
    config = _make_config()
    mocks = _make_mocks()
    strategy = TopGainersScalping(
        watchlist_manager=mocks["watchlist_manager"],
        signal_engine=mocks["signal_engine"],
        risk_guard=mocks["risk_guard"],
        order_manager=mocks["order_manager"],
        position_manager=mocks["position_manager"],
        candle_buffer=candle_buffer,
        telegram=mocks["telegram"],
        trade_repo=mocks["trade_repo"],
        config=config,
        get_balance=mocks["get_balance"],
        get_free_margin_pct=mocks["get_free_margin_pct"],
        get_funding_rate=mocks["get_funding_rate"],
        get_spread_pct=mocks["get_spread_pct"],
    )
    return strategy, mocks


# ------------------------------------------------------------------
# Tests: Lifecycle
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_creates_refresh_task():
    """start() should create a background task for watchlist refresh."""
    config = _make_config()
    mocks = _make_mocks()
    buf = CandleBuffer(max_size=100)
    strategy = TopGainersScalping(
        watchlist_manager=mocks["watchlist_manager"],
        signal_engine=mocks["signal_engine"],
        risk_guard=mocks["risk_guard"],
        order_manager=mocks["order_manager"],
        position_manager=mocks["position_manager"],
        candle_buffer=buf,
        telegram=mocks["telegram"],
        trade_repo=mocks["trade_repo"],
        config=config,
        get_balance=mocks["get_balance"],
    )

    await strategy.start()
    assert strategy._refresh_task is not None
    assert not strategy._refresh_task.done()

    await strategy.stop()
    assert strategy._refresh_task is None


@pytest.mark.asyncio
async def test_stop_cancels_refresh_task():
    """stop() should cancel the refresh task cleanly."""
    config = _make_config()
    mocks = _make_mocks()
    buf = CandleBuffer(max_size=100)
    strategy = TopGainersScalping(
        watchlist_manager=mocks["watchlist_manager"],
        signal_engine=mocks["signal_engine"],
        risk_guard=mocks["risk_guard"],
        order_manager=mocks["order_manager"],
        position_manager=mocks["position_manager"],
        candle_buffer=buf,
        telegram=mocks["telegram"],
        trade_repo=mocks["trade_repo"],
        config=config,
        get_balance=mocks["get_balance"],
    )

    await strategy.start()
    await strategy.stop()
    assert strategy._refresh_task is None


# ------------------------------------------------------------------
# Tests: on_candle_closed — signal flow
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ignores_non_signal_timeframe(strategy_with_buffer):
    """on_candle_closed should skip evaluation for non-signal timeframes."""
    strategy, mocks = strategy_with_buffer
    await strategy.on_candle_closed("SOLUSDT", "15m")
    mocks["signal_engine"].evaluate.assert_not_called()


@pytest.mark.asyncio
async def test_skips_when_no_signal(strategy_with_buffer):
    """When SignalEngine returns None, no order should be placed."""
    strategy, mocks = strategy_with_buffer
    mocks["signal_engine"].evaluate.return_value = None

    await strategy.on_candle_closed("SOLUSDT", "3m")

    mocks["signal_engine"].evaluate.assert_called_once()
    mocks["order_manager"].open_position.assert_not_called()


@pytest.mark.asyncio
async def test_full_long_trade_flow(strategy_with_buffer):
    """A LONG signal with risk approval should result in a full trade execution."""
    strategy, mocks = strategy_with_buffer
    mocks["signal_engine"].evaluate.return_value = Signal(
        direction=SignalDirection.LONG,
        confidence=0.75,
        indicators={"rsi": 55.0, "ema_fast": 100.0},
    )

    await strategy.on_candle_closed("SOLUSDT", "3m")

    # Verify the full flow.
    mocks["signal_engine"].evaluate.assert_called_once()
    mocks["risk_guard"].check_trade.assert_called_once()
    assert mocks["risk_guard"].check_trade.call_args.kwargs["free_margin_pct"] == 62.5
    mocks["order_manager"].set_leverage.assert_called_once_with("SOLUSDT", 5)
    mocks["order_manager"].open_position.assert_called_once_with(
        "SOLUSDT", OrderSide.BUY, 1.0,
    )
    mocks["position_manager"].open.assert_called_once()
    mocks["order_manager"].place_stop_loss.assert_called_once_with(
        symbol="SOLUSDT",
        side=OrderSide.SELL,
        stop_price=99.0,
    )
    mocks["trade_repo"].insert_trade.assert_called_once()
    mocks["telegram"].notify_position_opened.assert_called_once()


@pytest.mark.asyncio
async def test_risk_sizing_uses_live_price_not_candle_close(strategy_with_buffer):
    """Risk sizing should use a live price callback instead of stale candle close."""
    strategy, mocks = strategy_with_buffer
    get_current_price = AsyncMock(return_value=111.0)
    strategy._get_current_price = get_current_price
    mocks["signal_engine"].evaluate.return_value = Signal(
        direction=SignalDirection.LONG,
        confidence=0.75,
        indicators={"rsi": 55.0},
    )

    await strategy.on_candle_closed("SOLUSDT", "3m")

    get_current_price.assert_awaited_once_with("SOLUSDT")
    assert mocks["risk_guard"].check_trade.call_args.kwargs["entry_price"] == 111.0


@pytest.mark.asyncio
async def test_skips_trade_when_live_sizing_price_unavailable(strategy_with_buffer):
    """Production wiring should not fall back to stale candle close if live price is invalid."""
    strategy, mocks = strategy_with_buffer
    strategy._get_current_price = AsyncMock(return_value=0.0)
    mocks["signal_engine"].evaluate.return_value = Signal(
        direction=SignalDirection.LONG,
        confidence=0.75,
        indicators={"rsi": 55.0},
    )

    await strategy.on_candle_closed("SOLUSDT", "3m")

    mocks["risk_guard"].check_trade.assert_not_called()
    mocks["order_manager"].open_position.assert_not_called()


@pytest.mark.asyncio
async def test_oversize_fill_is_trimmed_before_position_tracking(strategy_with_buffer):
    """If fill slippage makes SL risk exceed budget, excess quantity is reduced."""
    strategy, mocks = strategy_with_buffer
    strategy._get_current_price = AsyncMock(return_value=100.0)

    # RiskGuard approves a position of 100.0 at the sizing price (100.0).
    # But the order fills at a worse price (200.0), making risk exceed budget.
    mocks["risk_guard"].check_trade.return_value = RiskCheckResult(
        approved=True,
        position_size=100.0,
    )
    # Fill quantity = 100.0 at avg_price = 200.0
    mocks["order_manager"].open_position.return_value = OrderResult(
        order_id=123,
        symbol="SOLUSDT",
        side="BUY",
        quantity=100.0,
        status="FILLED",
        avg_price=200.0,
    )

    # With leverage=5, fill_price=200.0, sl_pct=1.0:
    # max_qty = (10000*1/100) / (5 * 200*1/100) = 100/10 = 10.0
    # excess = 100.0 - 10.0 = 90.0
    # Close 90.0, remaining 10.0 should be tracked
    def _close_side_effect(*args, **kwargs):
        return OrderResult(
            order_id=456,
            symbol="SOLUSDT",
            side="SELL",
            quantity=kwargs.get("quantity", args[2]) if len(args) > 2 else args[2],
            status="FILLED",
            avg_price=200.0,
        )

    mocks["order_manager"].close_position.side_effect = _close_side_effect
    mocks["signal_engine"].evaluate.return_value = Signal(
        direction=SignalDirection.LONG,
        confidence=0.75,
        indicators={"rsi": 55.0},
    )

    await strategy.on_candle_closed("SOLUSDT", "3m")

    # Excess close should have been called
    assert mocks["order_manager"].close_position.await_count >= 1

    # Position should be tracked with trimmed quantity (≈10.0)
    pos_open_call = mocks["position_manager"].open.call_args
    assert pos_open_call.kwargs["quantity"] == pytest.approx(10.0, abs=0.1)
    assert pos_open_call.kwargs["entry_price"] == 200.0


@pytest.mark.asyncio
async def test_short_signal_uses_sell_side(strategy_with_buffer):
    """A SHORT signal should place a SELL order."""
    strategy, mocks = strategy_with_buffer
    mocks["signal_engine"].evaluate.return_value = Signal(
        direction=SignalDirection.SHORT,
        confidence=0.65,
        indicators={"rsi": 40.0},
    )

    await strategy.on_candle_closed("SOLUSDT", "3m")

    mocks["order_manager"].open_position.assert_called_once_with(
        "SOLUSDT", OrderSide.SELL, 1.0,
    )
    mocks["order_manager"].place_stop_loss.assert_called_once_with(
        symbol="SOLUSDT",
        side=OrderSide.BUY,
        stop_price=99.0,
    )


@pytest.mark.asyncio
async def test_uses_executed_quantity_from_order_result(strategy_with_buffer):
    """Position, trade record, and alert should use actual executed quantity."""
    strategy, mocks = strategy_with_buffer
    mocks["signal_engine"].evaluate.return_value = Signal(
        direction=SignalDirection.LONG,
        confidence=0.75,
        indicators={"rsi": 55.0},
    )
    mocks["order_manager"].open_position.return_value = OrderResult(
        order_id=123,
        symbol="SOLUSDT",
        side="BUY",
        quantity=0.6,
        status="FILLED",
        avg_price=101.0,
    )

    await strategy.on_candle_closed("SOLUSDT", "3m")

    mocks["position_manager"].open.assert_called_once_with(
        symbol="SOLUSDT",
        side=SignalDirection.LONG,
        entry_price=101.0,
        quantity=0.6,
        leverage=5,
        atr_value=None,
    )
    trade_record = mocks["trade_repo"].insert_trade.call_args.args[0]
    assert trade_record.quantity == 0.6
    assert mocks["telegram"].notify_position_opened.call_args.kwargs["quantity"] == 0.6


@pytest.mark.asyncio
async def test_skips_when_position_already_open(strategy_with_buffer):
    """Should not open a new position if one already exists for the symbol."""
    strategy, mocks = strategy_with_buffer
    mocks["signal_engine"].evaluate.return_value = Signal(
        direction=SignalDirection.LONG,
        confidence=0.75,
        indicators={"rsi": 55.0},
    )
    mocks["position_manager"].has_position.return_value = True

    await strategy.on_candle_closed("SOLUSDT", "3m")

    mocks["order_manager"].open_position.assert_not_called()


@pytest.mark.asyncio
async def test_skips_when_risk_rejected(strategy_with_buffer):
    """Should not place an order when RiskGuard rejects the trade."""
    strategy, mocks = strategy_with_buffer
    mocks["signal_engine"].evaluate.return_value = Signal(
        direction=SignalDirection.LONG,
        confidence=0.75,
        indicators={"rsi": 55.0},
    )
    mocks["risk_guard"].check_trade.return_value = RiskCheckResult(
        approved=False, reject_reason="daily_loss exceeded",
    )

    await strategy.on_candle_closed("SOLUSDT", "3m")

    mocks["order_manager"].open_position.assert_not_called()


@pytest.mark.asyncio
async def test_pending_entries_count_toward_max_concurrent(strategy_with_buffer):
    """Risk checks should count in-flight reserved entries as open slots."""
    strategy, mocks = strategy_with_buffer
    strategy._pending_entries.add("ETHUSDT")
    mocks["signal_engine"].evaluate.return_value = Signal(
        direction=SignalDirection.LONG,
        confidence=0.75,
        indicators={"rsi": 55.0},
    )

    await strategy.on_candle_closed("SOLUSDT", "3m")

    assert mocks["risk_guard"].check_trade.call_args.kwargs["open_position_count"] == 1
    assert "ETHUSDT" in strategy._pending_entries
    assert "SOLUSDT" not in strategy._pending_entries


@pytest.mark.asyncio
async def test_skips_when_entry_already_pending(strategy_with_buffer):
    """A second signal for a reserved symbol should not pass risk or open order."""
    strategy, mocks = strategy_with_buffer
    strategy._pending_entries.add("SOLUSDT")
    mocks["signal_engine"].evaluate.return_value = Signal(
        direction=SignalDirection.LONG,
        confidence=0.75,
        indicators={"rsi": 55.0},
    )

    await strategy.on_candle_closed("SOLUSDT", "3m")

    mocks["risk_guard"].check_trade.assert_not_called()
    mocks["order_manager"].open_position.assert_not_called()


# ------------------------------------------------------------------
# Tests: Cooldown
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cooldown_suppresses_signal(strategy_with_buffer):
    """After a trade, signals for the same symbol should be suppressed during cooldown."""
    strategy, mocks = strategy_with_buffer
    mocks["signal_engine"].evaluate.return_value = Signal(
        direction=SignalDirection.LONG,
        confidence=0.75,
        indicators={"rsi": 55.0},
    )

    # First trade should go through.
    await strategy.on_candle_closed("SOLUSDT", "3m")
    assert mocks["order_manager"].open_position.call_count == 1

    # Reset position check so it doesn't block on "already open".
    mocks["position_manager"].has_position.return_value = False

    # Second signal immediately after should be suppressed by cooldown.
    await strategy.on_candle_closed("SOLUSDT", "3m")
    assert mocks["order_manager"].open_position.call_count == 1  # Still 1.


@pytest.mark.asyncio
async def test_cooldown_is_per_symbol(strategy_with_buffer, candle_buffer):
    """Cooldown for one symbol should not affect another symbol."""
    strategy, mocks = strategy_with_buffer

    # Add buffer data for ETHUSDT.
    for i in range(50):
        await candle_buffer.add("ETHUSDT", "3m", {
            "open": 2000.0 + i, "high": 2010.0 + i, "low": 1990.0 + i,
            "close": 2005.0 + i, "volume": 5000.0, "timestamp": 1700000000 + i * 180,
        })
    for i in range(60):
        await candle_buffer.add("ETHUSDT", "15m", {
            "open": 2000.0 + i, "high": 2010.0 + i, "low": 1990.0 + i,
            "close": 2005.0 + i, "volume": 25000.0, "timestamp": 1700000000 + i * 900,
        })

    mocks["signal_engine"].evaluate.return_value = Signal(
        direction=SignalDirection.LONG,
        confidence=0.75,
        indicators={"rsi": 55.0},
    )

    # Trade on SOLUSDT — starts cooldown for SOLUSDT only.
    await strategy.on_candle_closed("SOLUSDT", "3m")
    assert mocks["order_manager"].open_position.call_count == 1

    # Trade on ETHUSDT should still go through (different symbol).
    await strategy.on_candle_closed("ETHUSDT", "3m")
    assert mocks["order_manager"].open_position.call_count == 2


@pytest.mark.asyncio
async def test_cooldown_expires(strategy_with_buffer):
    """After cooldown expires, signals should be allowed again."""
    strategy, mocks = strategy_with_buffer
    mocks["signal_engine"].evaluate.return_value = Signal(
        direction=SignalDirection.LONG,
        confidence=0.75,
        indicators={"rsi": 55.0},
    )

    # First trade.
    await strategy.on_candle_closed("SOLUSDT", "3m")
    assert mocks["order_manager"].open_position.call_count == 1

    # Simulate cooldown expiry by setting the cooldown time far in the past.
    strategy._cooldowns["SOLUSDT"] = datetime(2020, 1, 1, tzinfo=timezone.utc)

    # Signal should now go through.
    await strategy.on_candle_closed("SOLUSDT", "3m")
    assert mocks["order_manager"].open_position.call_count == 2


# ------------------------------------------------------------------
# Tests: close_all_positions
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_all_positions_long():
    """close_all_positions should close LONG positions with SELL side."""
    config = _make_config()
    mocks = _make_mocks()
    buf = CandleBuffer(max_size=100)

    long_pos = Position(
        symbol="SOLUSDT",
        side=SignalDirection.LONG,
        entry_price=100.0,
        quantity=1.0,
        original_quantity=1.0,
        leverage=5,
        tp1_price=100.8,
        tp2_price=101.5,
        tp3_price=102.5,
        sl_price=99.0,
    )
    mocks["position_manager"].get_open_positions.return_value = [long_pos]

    strategy = TopGainersScalping(
        watchlist_manager=mocks["watchlist_manager"],
        signal_engine=mocks["signal_engine"],
        risk_guard=mocks["risk_guard"],
        order_manager=mocks["order_manager"],
        position_manager=mocks["position_manager"],
        candle_buffer=buf,
        telegram=mocks["telegram"],
        trade_repo=mocks["trade_repo"],
        config=config,
        get_balance=mocks["get_balance"],
    )

    await strategy.close_all_positions()

    mocks["position_manager"].force_close.assert_awaited_once_with(
        "SOLUSDT",
        fallback_price=100.0,
        reason=ExitReason.HALT,
    )
    mocks["order_manager"].close_position.assert_not_called()


@pytest.mark.asyncio
async def test_close_all_positions_short():
    """close_all_positions should close SHORT positions with BUY side."""
    config = _make_config()
    mocks = _make_mocks()
    buf = CandleBuffer(max_size=100)

    short_pos = Position(
        symbol="ETHUSDT",
        side=SignalDirection.SHORT,
        entry_price=2000.0,
        quantity=0.5,
        original_quantity=0.5,
        leverage=5,
        tp1_price=1984.0,
        tp2_price=1970.0,
        tp3_price=1950.0,
        sl_price=2020.0,
    )
    mocks["position_manager"].get_open_positions.return_value = [short_pos]

    strategy = TopGainersScalping(
        watchlist_manager=mocks["watchlist_manager"],
        signal_engine=mocks["signal_engine"],
        risk_guard=mocks["risk_guard"],
        order_manager=mocks["order_manager"],
        position_manager=mocks["position_manager"],
        candle_buffer=buf,
        telegram=mocks["telegram"],
        trade_repo=mocks["trade_repo"],
        config=config,
        get_balance=mocks["get_balance"],
    )

    await strategy.close_all_positions()

    mocks["position_manager"].force_close.assert_awaited_once_with(
        "ETHUSDT",
        fallback_price=2000.0,
        reason=ExitReason.HALT,
    )
    mocks["order_manager"].close_position.assert_not_called()


@pytest.mark.asyncio
async def test_close_all_positions_empty():
    """close_all_positions with no open positions should be a no-op."""
    config = _make_config()
    mocks = _make_mocks()
    buf = CandleBuffer(max_size=100)
    mocks["position_manager"].get_open_positions.return_value = []

    strategy = TopGainersScalping(
        watchlist_manager=mocks["watchlist_manager"],
        signal_engine=mocks["signal_engine"],
        risk_guard=mocks["risk_guard"],
        order_manager=mocks["order_manager"],
        position_manager=mocks["position_manager"],
        candle_buffer=buf,
        telegram=mocks["telegram"],
        trade_repo=mocks["trade_repo"],
        config=config,
        get_balance=mocks["get_balance"],
    )

    await strategy.close_all_positions()

    mocks["order_manager"].close_position.assert_not_called()


# ------------------------------------------------------------------
# Tests: _on_position_closed callback
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_position_closed_records_pnl_and_notifies():
    """_on_position_closed should record PnL, check halt, save trade, update stats, and notify."""
    config = _make_config()
    mocks = _make_mocks()
    buf = CandleBuffer(max_size=100)

    strategy = TopGainersScalping(
        watchlist_manager=mocks["watchlist_manager"],
        signal_engine=mocks["signal_engine"],
        risk_guard=mocks["risk_guard"],
        order_manager=mocks["order_manager"],
        position_manager=mocks["position_manager"],
        candle_buffer=buf,
        telegram=mocks["telegram"],
        trade_repo=mocks["trade_repo"],
        config=config,
        get_balance=mocks["get_balance"],
    )

    result = TradeResult(
        trade_id=42,
        symbol="SOLUSDT",
        side="LONG",
        entry_price=100.0,
        exit_price=100.8,
        pnl_usdt=0.8,
        pnl_pct=0.8,
        exit_reason=ExitReason.TP1,
    )

    await strategy._on_position_closed(result)

    # Verify PnL recorded.
    mocks["risk_guard"].record_pnl.assert_called_once_with(0.8, 10000.0)

    # Verify halt conditions checked.
    mocks["risk_guard"].check_halt_conditions.assert_called_once_with(10000.0)

    # Verify trade closed in repository.
    mocks["trade_repo"].close_trade.assert_called_once()
    call_args = mocks["trade_repo"].close_trade.call_args
    assert call_args[0][0] == 42  # trade_id

    # Verify daily stats updated.
    mocks["trade_repo"].update_daily_stats.assert_called_once()

    # Verify Telegram notification sent.
    mocks["telegram"].notify_position_closed.assert_called_once_with(
        symbol="SOLUSDT",
        exit_reason=ExitReason.TP1,
        pnl_usdt=0.8,
    )


@pytest.mark.asyncio
async def test_on_position_closed_losing_trade():
    """_on_position_closed with a losing trade should still record and notify."""
    config = _make_config()
    mocks = _make_mocks()
    buf = CandleBuffer(max_size=100)

    strategy = TopGainersScalping(
        watchlist_manager=mocks["watchlist_manager"],
        signal_engine=mocks["signal_engine"],
        risk_guard=mocks["risk_guard"],
        order_manager=mocks["order_manager"],
        position_manager=mocks["position_manager"],
        candle_buffer=buf,
        telegram=mocks["telegram"],
        trade_repo=mocks["trade_repo"],
        config=config,
        get_balance=mocks["get_balance"],
    )

    result = TradeResult(
        trade_id=43,
        symbol="ETHUSDT",
        side="SHORT",
        entry_price=2000.0,
        exit_price=2020.0,
        pnl_usdt=-10.0,
        pnl_pct=-0.5,
        exit_reason=ExitReason.SL,
    )

    await strategy._on_position_closed(result)

    mocks["risk_guard"].record_pnl.assert_called_once_with(-10.0, 10000.0)
    mocks["trade_repo"].update_daily_stats.assert_called_once()
    # Verify is_win=False for losing trade.
    stats_call = mocks["trade_repo"].update_daily_stats.call_args
    assert stats_call[0][1] == -10.0  # pnl
    assert stats_call[0][2] is False  # is_win


# ------------------------------------------------------------------
# Tests: Error handling
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_candle_closed_handles_exception_gracefully(strategy_with_buffer):
    """on_candle_closed should catch and log exceptions without crashing."""
    strategy, mocks = strategy_with_buffer
    mocks["signal_engine"].evaluate.side_effect = RuntimeError("boom")

    # Should not raise.
    await strategy.on_candle_closed("SOLUSDT", "3m")

    mocks["order_manager"].open_position.assert_not_called()


@pytest.mark.asyncio
async def test_leverage_failure_skips_trade(strategy_with_buffer):
    """If set_leverage fails, the trade should be skipped."""
    strategy, mocks = strategy_with_buffer
    mocks["signal_engine"].evaluate.return_value = Signal(
        direction=SignalDirection.LONG,
        confidence=0.75,
        indicators={"rsi": 55.0},
    )
    mocks["order_manager"].set_leverage.side_effect = RuntimeError("API error")

    await strategy.on_candle_closed("SOLUSDT", "3m")

    mocks["order_manager"].open_position.assert_not_called()


@pytest.mark.asyncio
async def test_order_failure_skips_trade(strategy_with_buffer):
    """If open_position fails, the trade should be skipped."""
    strategy, mocks = strategy_with_buffer
    mocks["signal_engine"].evaluate.return_value = Signal(
        direction=SignalDirection.LONG,
        confidence=0.75,
        indicators={"rsi": 55.0},
    )
    mocks["order_manager"].open_position.side_effect = RuntimeError("API error")

    await strategy.on_candle_closed("SOLUSDT", "3m")

    mocks["position_manager"].open.assert_not_called()


@pytest.mark.asyncio
async def test_stop_loss_failure_emergency_closes_position(strategy_with_buffer):
    """If the protective stop cannot be placed, the position is closed immediately."""
    strategy, mocks = strategy_with_buffer
    mocks["signal_engine"].evaluate.return_value = Signal(
        direction=SignalDirection.LONG,
        confidence=0.75,
        indicators={"rsi": 55.0},
    )
    mocks["order_manager"].place_stop_loss.side_effect = RuntimeError("API error")

    await strategy.on_candle_closed("SOLUSDT", "3m")

    mocks["order_manager"].open_position.assert_called_once()
    mocks["order_manager"].close_position.assert_awaited_once_with(
        "SOLUSDT",
        OrderSide.SELL,
        1.0,
    )
    mocks["trade_repo"].insert_trade.assert_not_called()
    mocks["telegram"].notify_position_opened.assert_not_called()


# ------------------------------------------------------------------
# Tests: Insufficient buffer data
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skips_when_insufficient_buffer_data():
    """Should skip evaluation when buffer doesn't have enough candles."""
    config = _make_config()
    mocks = _make_mocks()
    buf = CandleBuffer(max_size=100)

    # Add only a few candles — not enough.
    for i in range(5):
        await buf.add("SOLUSDT", "3m", {
            "open": 99.0, "high": 100.0, "low": 98.0,
            "close": 99.5, "volume": 1000.0, "timestamp": 1700000000 + i * 180,
        })

    strategy = TopGainersScalping(
        watchlist_manager=mocks["watchlist_manager"],
        signal_engine=mocks["signal_engine"],
        risk_guard=mocks["risk_guard"],
        order_manager=mocks["order_manager"],
        position_manager=mocks["position_manager"],
        candle_buffer=buf,
        telegram=mocks["telegram"],
        trade_repo=mocks["trade_repo"],
        config=config,
        get_balance=mocks["get_balance"],
    )

    await strategy.on_candle_closed("SOLUSDT", "3m")

    mocks["signal_engine"].evaluate.assert_not_called()


# ------------------------------------------------------------------
# Tests: Trade record and position wiring
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trade_id_set_on_position(strategy_with_buffer):
    """After inserting a trade, the trade_id should be set on the position."""
    strategy, mocks = strategy_with_buffer
    mocks["signal_engine"].evaluate.return_value = Signal(
        direction=SignalDirection.LONG,
        confidence=0.75,
        indicators={"rsi": 55.0},
    )
    mocks["trade_repo"].insert_trade.return_value = 99

    await strategy.on_candle_closed("SOLUSDT", "3m")

    # The position returned by open() should have trade_id set.
    position = mocks["position_manager"].open.return_value
    assert position.trade_id == 99


@pytest.mark.asyncio
async def test_signal_snapshot_serialized_as_json(strategy_with_buffer):
    """The signal indicators should be serialized as JSON in the TradeRecord."""
    strategy, mocks = strategy_with_buffer
    indicators = {"rsi": 55.0, "ema_fast": 100.0, "ema_slow": 99.0}
    mocks["signal_engine"].evaluate.return_value = Signal(
        direction=SignalDirection.LONG,
        confidence=0.75,
        indicators=indicators,
    )

    await strategy.on_candle_closed("SOLUSDT", "3m")

    call_args = mocks["trade_repo"].insert_trade.call_args[0][0]
    import json
    parsed = json.loads(call_args.signal_snapshot)
    assert parsed == indicators


# ------------------------------------------------------------------
# Tests: Funding Rate Filter
# ------------------------------------------------------------------


def _make_indicators() -> dict:
    """Build a valid signal indicators dict with ADX and ATR."""
    return {
        "rsi": 55.0,
        "ema_fast": 100.0,
        "ema_slow": 99.0,
        "volume_ma": 500.0,
        "atr": 2.0,
        "adx": 30.0,
        "ema_fast_prev1": 99.5,
        "ema_slow_prev1": 99.2,
        "ema_fast_prev2": 99.0,
        "ema_slow_prev2": 99.3,
    }


@pytest.mark.asyncio
async def test_funding_rate_too_high_blocks_long(candle_buffer: CandleBuffer):
    """LONG signal should be skipped when funding rate exceeds max threshold."""
    config = _make_config()
    config.strategy.entry.max_funding_rate_pct = 0.0003  # 0.03%
    mocks = _make_mocks()
    mocks["signal_engine"].evaluate.return_value = Signal(
        direction=SignalDirection.LONG, confidence=0.75, indicators=_make_indicators(),
    )
    mocks["get_funding_rate"] = AsyncMock(return_value=0.0005)  # 0.05% — exceeds threshold

    strategy = TopGainersScalping(
        watchlist_manager=mocks["watchlist_manager"],
        signal_engine=mocks["signal_engine"],
        risk_guard=mocks["risk_guard"],
        order_manager=mocks["order_manager"],
        position_manager=mocks["position_manager"],
        candle_buffer=candle_buffer,
        telegram=mocks["telegram"],
        trade_repo=mocks["trade_repo"],
        config=config,
        get_balance=mocks["get_balance"],
        get_free_margin_pct=mocks["get_free_margin_pct"],
        get_funding_rate=mocks["get_funding_rate"],
        get_spread_pct=mocks["get_spread_pct"],
    )

    await strategy.on_candle_closed("SOLUSDT", "3m")
    mocks["order_manager"].open_position.assert_not_called()


@pytest.mark.asyncio
async def test_funding_rate_too_high_blocks_short(candle_buffer: CandleBuffer):
    """SHORT signal should be skipped when negative funding rate exceeds max threshold."""
    config = _make_config()
    config.strategy.entry.max_funding_rate_pct = 0.0003
    mocks = _make_mocks()
    mocks["signal_engine"].evaluate.return_value = Signal(
        direction=SignalDirection.SHORT, confidence=0.75, indicators=_make_indicators(),
    )
    mocks["get_funding_rate"] = AsyncMock(return_value=-0.0005)  # -0.05%

    strategy = TopGainersScalping(
        watchlist_manager=mocks["watchlist_manager"],
        signal_engine=mocks["signal_engine"],
        risk_guard=mocks["risk_guard"],
        order_manager=mocks["order_manager"],
        position_manager=mocks["position_manager"],
        candle_buffer=candle_buffer,
        telegram=mocks["telegram"],
        trade_repo=mocks["trade_repo"],
        config=config,
        get_balance=mocks["get_balance"],
        get_free_margin_pct=mocks["get_free_margin_pct"],
        get_funding_rate=mocks["get_funding_rate"],
        get_spread_pct=mocks["get_spread_pct"],
    )

    await strategy.on_candle_closed("SOLUSDT", "3m")
    mocks["order_manager"].open_position.assert_not_called()


@pytest.mark.asyncio
async def test_funding_against_position_blocks_long(candle_buffer: CandleBuffer):
    """LONG should be skipped when funding > 0 (longs pay shorts) and direction filter is on."""
    config = _make_config()
    config.strategy.entry.max_funding_rate_pct = 0.001   # high threshold
    config.strategy.entry.reject_funding_against_position = True
    mocks = _make_mocks()
    mocks["signal_engine"].evaluate.return_value = Signal(
        direction=SignalDirection.LONG, confidence=0.75, indicators=_make_indicators(),
    )
    mocks["get_funding_rate"] = AsyncMock(return_value=0.0001)  # positive = against LONG

    strategy = TopGainersScalping(
        watchlist_manager=mocks["watchlist_manager"],
        signal_engine=mocks["signal_engine"],
        risk_guard=mocks["risk_guard"],
        order_manager=mocks["order_manager"],
        position_manager=mocks["position_manager"],
        candle_buffer=candle_buffer,
        telegram=mocks["telegram"],
        trade_repo=mocks["trade_repo"],
        config=config,
        get_balance=mocks["get_balance"],
        get_free_margin_pct=mocks["get_free_margin_pct"],
        get_funding_rate=mocks["get_funding_rate"],
        get_spread_pct=mocks["get_spread_pct"],
    )

    await strategy.on_candle_closed("SOLUSDT", "3m")
    mocks["order_manager"].open_position.assert_not_called()


@pytest.mark.asyncio
async def test_funding_against_position_blocks_short(candle_buffer: CandleBuffer):
    """SHORT should be skipped when funding < 0 (shorts pay longs) and direction filter is on."""
    config = _make_config()
    config.strategy.entry.max_funding_rate_pct = 0.001
    config.strategy.entry.reject_funding_against_position = True
    mocks = _make_mocks()
    mocks["signal_engine"].evaluate.return_value = Signal(
        direction=SignalDirection.SHORT, confidence=0.75, indicators=_make_indicators(),
    )
    mocks["get_funding_rate"] = AsyncMock(return_value=-0.0001)  # negative = against SHORT

    strategy = TopGainersScalping(
        watchlist_manager=mocks["watchlist_manager"],
        signal_engine=mocks["signal_engine"],
        risk_guard=mocks["risk_guard"],
        order_manager=mocks["order_manager"],
        position_manager=mocks["position_manager"],
        candle_buffer=candle_buffer,
        telegram=mocks["telegram"],
        trade_repo=mocks["trade_repo"],
        config=config,
        get_balance=mocks["get_balance"],
        get_free_margin_pct=mocks["get_free_margin_pct"],
        get_funding_rate=mocks["get_funding_rate"],
        get_spread_pct=mocks["get_spread_pct"],
    )

    await strategy.on_candle_closed("SOLUSDT", "3m")
    mocks["order_manager"].open_position.assert_not_called()


@pytest.mark.asyncio
async def test_funding_with_position_allows_trade(candle_buffer: CandleBuffer):
    """LONG should be allowed when funding < 0 (funding favors longs) and direction filter is on."""
    config = _make_config()
    config.strategy.entry.max_funding_rate_pct = 0.001
    config.strategy.entry.reject_funding_against_position = True
    mocks = _make_mocks()
    mocks["signal_engine"].evaluate.return_value = Signal(
        direction=SignalDirection.LONG, confidence=0.75, indicators=_make_indicators(),
    )
    mocks["get_funding_rate"] = AsyncMock(return_value=-0.0001)  # negative = favors LONG

    strategy = TopGainersScalping(
        watchlist_manager=mocks["watchlist_manager"],
        signal_engine=mocks["signal_engine"],
        risk_guard=mocks["risk_guard"],
        order_manager=mocks["order_manager"],
        position_manager=mocks["position_manager"],
        candle_buffer=candle_buffer,
        telegram=mocks["telegram"],
        trade_repo=mocks["trade_repo"],
        config=config,
        get_balance=mocks["get_balance"],
        get_free_margin_pct=mocks["get_free_margin_pct"],
        get_funding_rate=mocks["get_funding_rate"],
        get_spread_pct=mocks["get_spread_pct"],
    )

    await strategy.on_candle_closed("SOLUSDT", "3m")
    mocks["order_manager"].open_position.assert_called_once()


@pytest.mark.asyncio
async def test_funding_disabled_passes_all_rates(candle_buffer: CandleBuffer):
    """When reject_funding_against_position=False, both positive and negative funding should pass."""
    config = _make_config()
    config.strategy.entry.max_funding_rate_pct = 0.001
    config.strategy.entry.reject_funding_against_position = False
    mocks = _make_mocks()
    mocks["signal_engine"].evaluate.return_value = Signal(
        direction=SignalDirection.LONG, confidence=0.75, indicators=_make_indicators(),
    )
    mocks["get_funding_rate"] = AsyncMock(return_value=0.0005)  # positive but direction filter off

    strategy = TopGainersScalping(
        watchlist_manager=mocks["watchlist_manager"],
        signal_engine=mocks["signal_engine"],
        risk_guard=mocks["risk_guard"],
        order_manager=mocks["order_manager"],
        position_manager=mocks["position_manager"],
        candle_buffer=candle_buffer,
        telegram=mocks["telegram"],
        trade_repo=mocks["trade_repo"],
        config=config,
        get_balance=mocks["get_balance"],
        get_free_margin_pct=mocks["get_free_margin_pct"],
        get_funding_rate=mocks["get_funding_rate"],
        get_spread_pct=mocks["get_spread_pct"],
    )

    await strategy.on_candle_closed("SOLUSDT", "3m")
    mocks["order_manager"].open_position.assert_called_once()


@pytest.mark.asyncio
async def test_no_funding_rate_fetcher_passes(candle_buffer: CandleBuffer):
    """When get_funding_rate is not wired (None), signal should pass through."""
    config = _make_config()
    config.strategy.entry.max_funding_rate_pct = 0.0001  # very strict
    mocks = _make_mocks()
    mocks["signal_engine"].evaluate.return_value = Signal(
        direction=SignalDirection.LONG, confidence=0.75, indicators=_make_indicators(),
    )

    strategy = TopGainersScalping(
        watchlist_manager=mocks["watchlist_manager"],
        signal_engine=mocks["signal_engine"],
        risk_guard=mocks["risk_guard"],
        order_manager=mocks["order_manager"],
        position_manager=mocks["position_manager"],
        candle_buffer=candle_buffer,
        telegram=mocks["telegram"],
        trade_repo=mocks["trade_repo"],
        config=config,
        get_balance=mocks["get_balance"],
        get_free_margin_pct=mocks["get_free_margin_pct"],
        # No get_funding_rate — should skip the check
        # No get_spread_pct — should skip the check
    )

    await strategy.on_candle_closed("SOLUSDT", "3m")
    mocks["order_manager"].open_position.assert_called_once()


# ------------------------------------------------------------------
# Spread / Slippage Protection
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spread_too_wide_rejects(candle_buffer: CandleBuffer):
    """When spread exceeds max_spread_pct, trade should be rejected."""
    config = _make_config()
    config.risk.max_spread_pct = 0.05  # very tight — 0.05%
    mocks = _make_mocks()
    mocks["get_spread_pct"].return_value = 0.10  # 0.10% > 0.05% threshold
    mocks["signal_engine"].evaluate.return_value = Signal(
        direction=SignalDirection.LONG, confidence=0.75, indicators=_make_indicators(),
    )

    strategy = TopGainersScalping(
        watchlist_manager=mocks["watchlist_manager"],
        signal_engine=mocks["signal_engine"],
        risk_guard=mocks["risk_guard"],
        order_manager=mocks["order_manager"],
        position_manager=mocks["position_manager"],
        candle_buffer=candle_buffer,
        telegram=mocks["telegram"],
        trade_repo=mocks["trade_repo"],
        config=config,
        get_balance=mocks["get_balance"],
        get_free_margin_pct=mocks["get_free_margin_pct"],
        get_funding_rate=mocks["get_funding_rate"],
        get_spread_pct=mocks["get_spread_pct"],
    )

    await strategy.on_candle_closed("SOLUSDT", "3m")
    mocks["order_manager"].open_position.assert_not_called()
    mocks["get_spread_pct"].assert_awaited_once_with("SOLUSDT")


@pytest.mark.asyncio
async def test_spread_within_limit_allows_trade(candle_buffer: CandleBuffer):
    """When spread is within limit, trade should proceed."""
    config = _make_config()
    config.risk.max_spread_pct = 0.10
    mocks = _make_mocks()
    mocks["get_spread_pct"].return_value = 0.03  # 0.03% < 0.10% threshold
    mocks["signal_engine"].evaluate.return_value = Signal(
        direction=SignalDirection.LONG, confidence=0.75, indicators=_make_indicators(),
    )

    strategy = TopGainersScalping(
        watchlist_manager=mocks["watchlist_manager"],
        signal_engine=mocks["signal_engine"],
        risk_guard=mocks["risk_guard"],
        order_manager=mocks["order_manager"],
        position_manager=mocks["position_manager"],
        candle_buffer=candle_buffer,
        telegram=mocks["telegram"],
        trade_repo=mocks["trade_repo"],
        config=config,
        get_balance=mocks["get_balance"],
        get_free_margin_pct=mocks["get_free_margin_pct"],
        get_funding_rate=mocks["get_funding_rate"],
        get_spread_pct=mocks["get_spread_pct"],
    )

    await strategy.on_candle_closed("SOLUSDT", "3m")
    mocks["order_manager"].open_position.assert_called_once()


@pytest.mark.asyncio
async def test_no_spread_checker_passes(candle_buffer: CandleBuffer):
    """When get_spread_pct is not wired (None), signal should pass through."""
    config = _make_config()
    config.risk.max_spread_pct = 0.001  # extremely tight
    mocks = _make_mocks()
    mocks["signal_engine"].evaluate.return_value = Signal(
        direction=SignalDirection.LONG, confidence=0.75, indicators=_make_indicators(),
    )

    strategy = TopGainersScalping(
        watchlist_manager=mocks["watchlist_manager"],
        signal_engine=mocks["signal_engine"],
        risk_guard=mocks["risk_guard"],
        order_manager=mocks["order_manager"],
        position_manager=mocks["position_manager"],
        candle_buffer=candle_buffer,
        telegram=mocks["telegram"],
        trade_repo=mocks["trade_repo"],
        config=config,
        get_balance=mocks["get_balance"],
        get_free_margin_pct=mocks["get_free_margin_pct"],
        # No get_spread_pct — should skip the check
    )

    await strategy.on_candle_closed("SOLUSDT", "3m")
    mocks["order_manager"].open_position.assert_called_once()
