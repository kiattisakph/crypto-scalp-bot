"""Unit tests for futures user-data stream order reconciliation parsing."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from streams.user_data_stream import UserDataStream


def _order_update(**overrides) -> dict:
    order = {
        "s": "SOLUSDT",
        "i": 111,
        "c": "csb_test",
        "S": "SELL",
        "o": "STOP_MARKET",
        "X": "FILLED",
        "x": "TRADE",
        "ap": "99.0",
        "L": "99.0",
        "l": "1.0",
        "z": "1.0",
        "rp": "-1.0",
        "R": False,
        "cp": True,
        "sp": "99.0",
    }
    order.update(overrides)
    return {"e": "ORDER_TRADE_UPDATE", "o": order}


def test_parse_order_update() -> None:
    update = UserDataStream._parse_order_update(_order_update())

    assert update.symbol == "SOLUSDT"
    assert update.order_id == 111
    assert update.side == "SELL"
    assert update.order_type == "STOP_MARKET"
    assert update.status == "FILLED"
    assert update.avg_price == pytest.approx(99.0)
    assert update.last_fill_qty == pytest.approx(1.0)
    assert update.realized_pnl_usdt == pytest.approx(-1.0)
    assert update.close_position is True


@pytest.mark.asyncio
async def test_handle_message_invokes_callback_for_order_update() -> None:
    stream = UserDataStream(MagicMock())
    callback = AsyncMock()
    stream.on_order_update = callback

    await stream._handle_message(_order_update())

    callback.assert_awaited_once()
    update = callback.call_args.args[0]
    assert update.symbol == "SOLUSDT"
    assert update.order_id == 111


@pytest.mark.asyncio
async def test_handle_message_ignores_non_order_events() -> None:
    stream = UserDataStream(MagicMock())
    callback = AsyncMock()
    stream.on_order_update = callback

    await stream._handle_message({"e": "ACCOUNT_UPDATE", "a": {}})

    callback.assert_not_awaited()
