"""Unit tests for PositionManager exchange-side stop management."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.config import ExitConfig
from core.enums import ExitReason, OrderSide, SignalDirection
from execution.position_manager import PositionManager


@pytest.mark.asyncio
async def test_tp1_replaces_exchange_stop_at_breakeven() -> None:
    close_order = AsyncMock(return_value=SimpleNamespace(quantity=0.4))
    replace_stop = AsyncMock(return_value=SimpleNamespace(order_id=222))
    cancel_order = AsyncMock()
    manager = PositionManager(
        exit_config=ExitConfig(),
        close_order_fn=close_order,
        replace_stop_order_fn=replace_stop,
        cancel_order_fn=cancel_order,
    )
    position = manager.open("SOLUSDT", SignalDirection.LONG, 100.0, 1.0, 5)
    position.stop_order_id = 111

    await manager.check_exits("SOLUSDT", position.tp1_price)

    close_order.assert_awaited_once_with("SOLUSDT", OrderSide.SELL, 0.4)
    replace_stop.assert_awaited_once_with(
        "SOLUSDT",
        111,
        OrderSide.SELL,
        100.0,
    )
    assert position.sl_price == 100.0
    assert position.stop_order_id == 222


@pytest.mark.asyncio
async def test_full_close_cancels_exchange_stop() -> None:
    close_order = AsyncMock()
    replace_stop = AsyncMock()
    cancel_order = AsyncMock()
    manager = PositionManager(
        exit_config=ExitConfig(),
        close_order_fn=close_order,
        replace_stop_order_fn=replace_stop,
        cancel_order_fn=cancel_order,
    )
    position = manager.open("SOLUSDT", SignalDirection.LONG, 100.0, 1.0, 5)
    position.stop_order_id = 111

    await manager.check_exits("SOLUSDT", position.sl_price)

    close_order.assert_awaited_once_with("SOLUSDT", OrderSide.SELL, 1.0)
    cancel_order.assert_awaited_once_with("SOLUSDT", 111)
    assert not manager.has_position("SOLUSDT")


@pytest.mark.asyncio
async def test_force_close_emits_closed_callback_and_uses_fill_price() -> None:
    close_order = AsyncMock(return_value=SimpleNamespace(avg_price=101.0))
    cancel_order = AsyncMock()
    manager = PositionManager(
        exit_config=ExitConfig(),
        close_order_fn=close_order,
        cancel_order_fn=cancel_order,
    )
    callback = AsyncMock()
    manager.on_position_closed = callback
    position = manager.open("SOLUSDT", SignalDirection.LONG, 100.0, 1.0, 5)
    position.trade_id = 42
    position.stop_order_id = 111

    await manager.force_close("SOLUSDT", fallback_price=100.0, reason=ExitReason.HALT)

    close_order.assert_awaited_once_with("SOLUSDT", OrderSide.SELL, 1.0)
    cancel_order.assert_awaited_once_with("SOLUSDT", 111)
    callback.assert_awaited_once()
    result = callback.call_args.args[0]
    assert result.trade_id == 42
    assert result.exit_price == 101.0
    assert result.pnl_usdt == 1.0
    assert result.exit_reason == ExitReason.HALT
    assert not manager.has_position("SOLUSDT")


@pytest.mark.asyncio
async def test_tp1_uses_actual_close_fill_quantity() -> None:
    close_order = AsyncMock(return_value=SimpleNamespace(quantity=0.25))
    manager = PositionManager(
        exit_config=ExitConfig(),
        close_order_fn=close_order,
    )
    position = manager.open("SOLUSDT", SignalDirection.LONG, 100.0, 1.0, 5)

    await manager.check_exits("SOLUSDT", position.tp1_price)

    close_order.assert_awaited_once_with("SOLUSDT", OrderSide.SELL, 0.4)
    assert position.quantity == pytest.approx(0.75)
    assert position.tp1_hit is True


@pytest.mark.asyncio
async def test_partial_tp_pnl_uses_each_fill_price() -> None:
    close_order = AsyncMock(
        side_effect=[
            SimpleNamespace(quantity=0.4, avg_price=101.0),
            SimpleNamespace(quantity=0.4, avg_price=102.0),
            SimpleNamespace(quantity=0.2, avg_price=102.0),
        ],
    )
    manager = PositionManager(
        exit_config=ExitConfig(
            tp1_pct=1.0,
            tp2_pct=2.0,
            tp3_pct=3.0,
            tp1_close_ratio=0.4,
            tp2_close_ratio=0.4,
            trailing_stop_pct=0.5,
            sl_pct=1.0,
            max_hold_min=30,
        ),
        close_order_fn=close_order,
    )
    callback = AsyncMock()
    manager.on_position_closed = callback
    position = manager.open("SOLUSDT", SignalDirection.LONG, 100.0, 1.0, 5)
    position.trade_id = 42

    await manager.check_exits("SOLUSDT", 101.0)
    await manager.check_exits("SOLUSDT", 102.0)
    await manager.check_exits("SOLUSDT", 103.0)
    await manager.check_exits("SOLUSDT", 102.0)

    callback.assert_awaited_once()
    result = callback.call_args.args[0]
    assert result.trade_id == 42
    assert result.pnl_usdt == pytest.approx(1.6)
    assert result.pnl_pct == pytest.approx(8.0)
    assert result.exit_reason == ExitReason.TP3
    assert not manager.has_position("SOLUSDT")


@pytest.mark.asyncio
async def test_exchange_stop_fill_reconciles_without_duplicate_close() -> None:
    close_order = AsyncMock()
    cancel_order = AsyncMock()
    manager = PositionManager(
        exit_config=ExitConfig(),
        close_order_fn=close_order,
        cancel_order_fn=cancel_order,
    )
    callback = AsyncMock()
    manager.on_position_closed = callback
    position = manager.open("SOLUSDT", SignalDirection.LONG, 100.0, 1.0, 5)
    position.trade_id = 42
    position.stop_order_id = 111

    reconciled = await manager.reconcile_exchange_close(
        symbol="SOLUSDT",
        exit_price=99.0,
        reason=ExitReason.SL,
        realized_pnl_usdt=-1.0,
        closed_quantity=1.0,
    )

    assert reconciled is True
    close_order.assert_not_awaited()
    cancel_order.assert_not_awaited()
    callback.assert_awaited_once()
    result = callback.call_args.args[0]
    assert result.trade_id == 42
    assert result.exit_price == 99.0
    assert result.pnl_usdt == -1.0
    assert result.exit_reason == ExitReason.SL
    assert not manager.has_position("SOLUSDT")
