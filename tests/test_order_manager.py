"""Unit tests for OrderManager execution safety."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from core.config import EnvSettings
from core.enums import OrderSide
from execution.order_manager import OrderManager


def _env() -> EnvSettings:
    return EnvSettings(
        binance_api_key="test_key",
        binance_api_secret="test_secret",
        binance_demo=True,
        telegram_bot_token="test_token",
        telegram_chat_id="test_chat",
    )


def _raw_order(**overrides) -> dict:
    data = {
        "orderId": 123,
        "symbol": "SOLUSDT",
        "side": "BUY",
        "type": "MARKET",
        "origQty": "1.0",
        "executedQty": "1.0",
        "avgPrice": "100.0",
        "status": "FILLED",
    }
    data.update(overrides)
    return data


@pytest.mark.asyncio
async def test_open_position_timeout_recovers_existing_order_without_duplicate() -> None:
    """If create_order times out after acceptance, retry must query and return it."""
    manager = OrderManager(_env())
    client = AsyncMock()
    client.futures_create_order = AsyncMock(side_effect=asyncio.TimeoutError())
    client.futures_get_order = AsyncMock(return_value=_raw_order())
    manager._client = client

    result = await manager.open_position("SOLUSDT", OrderSide.BUY, 1.0)

    assert result.order_id == 123
    assert client.futures_create_order.await_count == 1
    client.futures_get_order.assert_awaited_once()
    create_kwargs = client.futures_create_order.call_args.kwargs
    get_kwargs = client.futures_get_order.call_args.kwargs
    assert get_kwargs["origClientOrderId"] == create_kwargs["newClientOrderId"]


@pytest.mark.asyncio
async def test_open_position_retry_reuses_same_client_order_id() -> None:
    """When the first attempt is not found, retry must reuse the same client id."""
    manager = OrderManager(_env())
    client = AsyncMock()
    client.futures_create_order = AsyncMock(
        side_effect=[asyncio.TimeoutError(), _raw_order(orderId=456)]
    )
    client.futures_get_order = AsyncMock(side_effect=Exception("not found"))
    manager._client = client

    with patch("execution.order_manager.asyncio.sleep", new=AsyncMock()):
        result = await manager.open_position("SOLUSDT", OrderSide.BUY, 1.0)

    assert result.order_id == 456
    assert client.futures_create_order.await_count == 2
    first_kwargs = client.futures_create_order.await_args_list[0].kwargs
    second_kwargs = client.futures_create_order.await_args_list[1].kwargs
    assert first_kwargs["newClientOrderId"] == second_kwargs["newClientOrderId"]


@pytest.mark.asyncio
async def test_close_position_retry_is_idempotent_and_reduce_only() -> None:
    """Close retries must also use stable client ids and reduceOnly orders."""
    manager = OrderManager(_env())
    client = AsyncMock()
    client.futures_create_order = AsyncMock(
        side_effect=[asyncio.TimeoutError(), _raw_order(orderId=789, side="SELL")]
    )
    client.futures_get_order = AsyncMock(side_effect=Exception("not found"))
    manager._client = client

    with patch("execution.order_manager.asyncio.sleep", new=AsyncMock()):
        result = await manager.close_position("SOLUSDT", OrderSide.SELL, 1.0)

    assert result.order_id == 789
    first_kwargs = client.futures_create_order.await_args_list[0].kwargs
    second_kwargs = client.futures_create_order.await_args_list[1].kwargs
    assert first_kwargs["newClientOrderId"] == second_kwargs["newClientOrderId"]
    assert first_kwargs["reduceOnly"] == "true"


@pytest.mark.asyncio
async def test_open_position_polls_until_filled_and_uses_executed_qty() -> None:
    """A non-final create response should be resolved through get_order."""
    manager = OrderManager(_env())
    client = AsyncMock()
    client.futures_create_order = AsyncMock(
        return_value=_raw_order(
            orderId=321,
            executedQty="0",
            avgPrice="0",
            status="NEW",
        )
    )
    client.futures_get_order = AsyncMock(
        return_value=_raw_order(
            orderId=321,
            executedQty="0.6",
            avgPrice="101.5",
            status="FILLED",
        )
    )
    manager._client = client

    with patch("execution.order_manager.asyncio.sleep", new=AsyncMock()):
        result = await manager.open_position("SOLUSDT", OrderSide.BUY, 1.0)

    assert result.order_id == 321
    assert result.quantity == 0.6
    assert result.avg_price == 101.5
    client.futures_get_order.assert_awaited_once_with(symbol="SOLUSDT", orderId=321)


@pytest.mark.asyncio
async def test_get_symbol_price_returns_latest_futures_price() -> None:
    """Latest futures ticker price is used as live entry-sizing input."""
    manager = OrderManager(_env())
    client = AsyncMock()
    client.futures_symbol_ticker = AsyncMock(return_value={"symbol": "SOLUSDT", "price": "123.45"})
    manager._client = client

    price = await manager.get_symbol_price("SOLUSDT")

    assert price == pytest.approx(123.45)
    client.futures_symbol_ticker.assert_awaited_once_with(symbol="SOLUSDT")
