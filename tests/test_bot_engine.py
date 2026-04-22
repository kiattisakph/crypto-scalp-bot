"""Tests for BotEngine lifecycle management.

Verifies startup sequence, shutdown sequence, callback wiring,
signal handler registration, and balance fetching — all with
mocked external dependencies.
"""
from __future__ import annotations

import asyncio
import signal
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from core.bot import BotEngine
from core.config import (
    AppConfig,
    EntryConfig,
    EnvSettings,
    ExitConfig,
    RiskConfig,
    StrategyConfig,
    WatchlistConfig,
)
from core.enums import SignalDirection
from core.models import OpenTradeRecord, OrderUpdate, TickerData
from execution.order_manager import ExchangePosition, OrderResult
from execution.position_manager import PositionManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def env_settings() -> EnvSettings:
    """Provide valid EnvSettings for testing."""
    return EnvSettings(
        binance_api_key="test_key",
        binance_api_secret="test_secret",
        binance_demo=True,
        telegram_bot_token="test_token",
        telegram_chat_id="test_chat",
        db_path=":memory:",
        log_level="DEBUG",
    )


@pytest.fixture
def app_config() -> AppConfig:
    """Provide valid AppConfig for testing."""
    return AppConfig(
        watchlist=WatchlistConfig(),
        strategy=StrategyConfig(
            entry=EntryConfig(),
            exit=ExitConfig(),
        ),
        risk=RiskConfig(),
    )


@pytest.fixture
def bot_engine(env_settings: EnvSettings, app_config: AppConfig) -> BotEngine:
    """Create a BotEngine instance for testing."""
    return BotEngine(env=env_settings, config=app_config)


def _make_mock_client() -> AsyncMock:
    """Create a mock AsyncClient with futures_account_balance."""
    client = AsyncMock()
    client.futures_account_balance = AsyncMock(
        return_value=[{"asset": "USDT", "balance": "1000.0"}],
    )
    client.close_connection = AsyncMock()
    return client


# ---------------------------------------------------------------------------
# Startup tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_initialises_all_components(
    bot_engine: BotEngine,
) -> None:
    """BotEngine.start() should initialise DB, risk guard, streams, and strategy."""
    mock_client = _make_mock_client()

    with (
        patch("core.bot.Database") as MockDB,
        patch("core.bot.OrderManager") as MockOM,
        patch("core.bot.TelegramAlert") as MockTA,
        patch("core.bot.RiskGuard") as MockRG,
        patch("core.bot.PositionManager") as MockPM,
        patch("core.bot.WatchlistManager") as MockWM,
        patch("core.bot.SignalEngine") as MockSE,
        patch("core.bot.TopGainersScalping") as MockTGS,
        patch("core.bot.TickerStream") as MockTS,
        patch("core.bot.UserDataStream") as MockUS,
        patch("core.bot.KlineStream") as MockKS,
        patch("core.bot.CandleBuffer") as MockCB,
    ):
        # Set up mock returns
        mock_db = AsyncMock()
        MockDB.return_value = mock_db

        mock_om = MagicMock()
        mock_om.connect = AsyncMock()
        mock_om.close = AsyncMock()
        mock_om._client = mock_client
        mock_om.close_position = AsyncMock()
        mock_om.get_open_positions = AsyncMock(return_value=[])
        MockOM.return_value = mock_om

        mock_ta = MagicMock()
        mock_ta.notify_started = AsyncMock()
        mock_ta.notify_stopped = AsyncMock()
        MockTA.return_value = mock_ta

        mock_rg = MagicMock()
        mock_rg.load_daily_state = AsyncMock()
        MockRG.return_value = mock_rg

        mock_pm = MagicMock()
        MockPM.return_value = mock_pm

        mock_wm = MagicMock()
        MockWM.return_value = mock_wm

        mock_se = MagicMock()
        MockSE.return_value = mock_se

        mock_tgs = MagicMock()
        mock_tgs.start = AsyncMock()
        mock_tgs.stop = AsyncMock()
        mock_tgs.close_all_positions = AsyncMock()
        MockTGS.return_value = mock_tgs

        mock_ts = MagicMock()
        mock_ts.connect = AsyncMock()
        mock_ts.disconnect = AsyncMock()
        MockTS.return_value = mock_ts

        mock_us = MagicMock()
        mock_us.connect = AsyncMock()
        mock_us.disconnect = AsyncMock()
        MockUS.return_value = mock_us

        mock_ks = MagicMock()
        mock_ks.disconnect = AsyncMock()
        MockKS.return_value = mock_ks

        mock_cb = MagicMock()
        MockCB.return_value = mock_cb

        # Trigger stop shortly after start to unblock the wait
        async def trigger_stop() -> None:
            await asyncio.sleep(0.05)
            await bot_engine.stop()

        asyncio.create_task(trigger_stop())

        await bot_engine.start()

        # Verify startup sequence
        mock_db.init.assert_awaited_once()
        mock_rg.load_daily_state.assert_awaited_once()
        mock_om.connect.assert_awaited_once()
        mock_us.connect.assert_awaited_once()
        mock_ts.connect.assert_awaited_once()
        mock_tgs.start.assert_awaited_once()
        mock_ta.notify_started.assert_awaited_once()


@pytest.mark.asyncio
async def test_start_sends_bot_started_telegram_alert(
    bot_engine: BotEngine,
) -> None:
    """BotEngine.start() should send a 'Bot started' Telegram alert."""
    mock_client = _make_mock_client()

    with (
        patch("core.bot.Database") as MockDB,
        patch("core.bot.OrderManager") as MockOM,
        patch("core.bot.TelegramAlert") as MockTA,
        patch("core.bot.RiskGuard") as MockRG,
        patch("core.bot.PositionManager"),
        patch("core.bot.WatchlistManager"),
        patch("core.bot.SignalEngine"),
        patch("core.bot.TopGainersScalping") as MockTGS,
        patch("core.bot.TickerStream") as MockTS,
        patch("core.bot.UserDataStream") as MockUS,
        patch("core.bot.KlineStream"),
        patch("core.bot.CandleBuffer"),
    ):
        mock_db = AsyncMock()
        MockDB.return_value = mock_db

        mock_om = MagicMock()
        mock_om.connect = AsyncMock()
        mock_om.close = AsyncMock()
        mock_om._client = mock_client
        mock_om.close_position = AsyncMock()
        mock_om.get_open_positions = AsyncMock(return_value=[])
        MockOM.return_value = mock_om

        mock_ta = MagicMock()
        mock_ta.notify_started = AsyncMock()
        mock_ta.notify_stopped = AsyncMock()
        MockTA.return_value = mock_ta

        mock_rg = MagicMock()
        mock_rg.load_daily_state = AsyncMock()
        MockRG.return_value = mock_rg

        mock_tgs = MagicMock()
        mock_tgs.start = AsyncMock()
        mock_tgs.stop = AsyncMock()
        mock_tgs.close_all_positions = AsyncMock()
        MockTGS.return_value = mock_tgs

        mock_ts = MagicMock()
        mock_ts.connect = AsyncMock()
        mock_ts.disconnect = AsyncMock()
        MockTS.return_value = mock_ts

        mock_us = MagicMock()
        mock_us.connect = AsyncMock()
        mock_us.disconnect = AsyncMock()
        MockUS.return_value = mock_us

        async def trigger_stop() -> None:
            await asyncio.sleep(0.05)
            await bot_engine.stop()

        asyncio.create_task(trigger_stop())
        await bot_engine.start()

        mock_ta.notify_started.assert_awaited_once()


# ---------------------------------------------------------------------------
# Shutdown tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_follows_shutdown_sequence(
    bot_engine: BotEngine,
) -> None:
    """BotEngine.stop() should follow the strict shutdown sequence."""
    mock_client = _make_mock_client()

    with (
        patch("core.bot.Database") as MockDB,
        patch("core.bot.OrderManager") as MockOM,
        patch("core.bot.TelegramAlert") as MockTA,
        patch("core.bot.RiskGuard") as MockRG,
        patch("core.bot.PositionManager"),
        patch("core.bot.WatchlistManager"),
        patch("core.bot.SignalEngine"),
        patch("core.bot.TopGainersScalping") as MockTGS,
        patch("core.bot.TickerStream") as MockTS,
        patch("core.bot.UserDataStream") as MockUS,
        patch("core.bot.KlineStream") as MockKS,
        patch("core.bot.CandleBuffer"),
    ):
        mock_db = AsyncMock()
        MockDB.return_value = mock_db

        mock_om = MagicMock()
        mock_om.connect = AsyncMock()
        mock_om.close = AsyncMock()
        mock_om._client = mock_client
        mock_om.close_position = AsyncMock()
        mock_om.get_open_positions = AsyncMock(return_value=[])
        MockOM.return_value = mock_om

        mock_ta = MagicMock()
        mock_ta.notify_started = AsyncMock()
        mock_ta.notify_stopped = AsyncMock()
        MockTA.return_value = mock_ta

        mock_rg = MagicMock()
        mock_rg.load_daily_state = AsyncMock()
        MockRG.return_value = mock_rg

        mock_tgs = MagicMock()
        mock_tgs.start = AsyncMock()
        mock_tgs.stop = AsyncMock()
        mock_tgs.close_all_positions = AsyncMock()
        MockTGS.return_value = mock_tgs

        mock_ts = MagicMock()
        mock_ts.connect = AsyncMock()
        mock_ts.disconnect = AsyncMock()
        MockTS.return_value = mock_ts

        mock_us = MagicMock()
        mock_us.connect = AsyncMock()
        mock_us.disconnect = AsyncMock()
        MockUS.return_value = mock_us

        mock_ks = MagicMock()
        mock_ks.disconnect = AsyncMock()
        MockKS.return_value = mock_ks

        # Track call order
        call_order: list[str] = []
        mock_tgs.close_all_positions.side_effect = lambda: call_order.append("close_positions")
        mock_tgs.stop.side_effect = lambda: call_order.append("stop_strategy")
        mock_ks.disconnect.side_effect = lambda: call_order.append("disconnect_kline")
        mock_ts.disconnect.side_effect = lambda: call_order.append("disconnect_ticker")
        mock_us.disconnect.side_effect = lambda: call_order.append("disconnect_user_data")
        mock_om.close.side_effect = lambda: call_order.append("close_order_manager")
        mock_db.close.side_effect = lambda: call_order.append("close_db")
        mock_ta.notify_stopped.side_effect = lambda: call_order.append("notify_stopped")

        async def trigger_stop() -> None:
            await asyncio.sleep(0.05)
            await bot_engine.stop()

        asyncio.create_task(trigger_stop())
        await bot_engine.start()

        # Verify shutdown sequence order
        assert call_order == [
            "close_positions",
            "stop_strategy",
            "disconnect_kline",
            "disconnect_ticker",
            "disconnect_user_data",
            "close_order_manager",
            "close_db",
            "notify_stopped",
        ]


@pytest.mark.asyncio
async def test_stop_sends_bot_stopped_telegram_alert(
    bot_engine: BotEngine,
) -> None:
    """BotEngine.stop() should send a 'Bot stopped' Telegram alert."""
    mock_client = _make_mock_client()

    with (
        patch("core.bot.Database") as MockDB,
        patch("core.bot.OrderManager") as MockOM,
        patch("core.bot.TelegramAlert") as MockTA,
        patch("core.bot.RiskGuard") as MockRG,
        patch("core.bot.PositionManager"),
        patch("core.bot.WatchlistManager"),
        patch("core.bot.SignalEngine"),
        patch("core.bot.TopGainersScalping") as MockTGS,
        patch("core.bot.TickerStream") as MockTS,
        patch("core.bot.UserDataStream") as MockUS,
        patch("core.bot.KlineStream"),
        patch("core.bot.CandleBuffer"),
    ):
        mock_db = AsyncMock()
        MockDB.return_value = mock_db

        mock_om = MagicMock()
        mock_om.connect = AsyncMock()
        mock_om.close = AsyncMock()
        mock_om._client = mock_client
        mock_om.close_position = AsyncMock()
        mock_om.get_open_positions = AsyncMock(return_value=[])
        MockOM.return_value = mock_om

        mock_ta = MagicMock()
        mock_ta.notify_started = AsyncMock()
        mock_ta.notify_stopped = AsyncMock()
        MockTA.return_value = mock_ta

        mock_rg = MagicMock()
        mock_rg.load_daily_state = AsyncMock()
        MockRG.return_value = mock_rg

        mock_tgs = MagicMock()
        mock_tgs.start = AsyncMock()
        mock_tgs.stop = AsyncMock()
        mock_tgs.close_all_positions = AsyncMock()
        MockTGS.return_value = mock_tgs

        mock_ts = MagicMock()
        mock_ts.connect = AsyncMock()
        mock_ts.disconnect = AsyncMock()
        MockTS.return_value = mock_ts

        mock_us = MagicMock()
        mock_us.connect = AsyncMock()
        mock_us.disconnect = AsyncMock()
        MockUS.return_value = mock_us

        async def trigger_stop() -> None:
            await asyncio.sleep(0.05)
            await bot_engine.stop()

        asyncio.create_task(trigger_stop())
        await bot_engine.start()

        mock_ta.notify_stopped.assert_awaited_once()


@pytest.mark.asyncio
async def test_stop_is_idempotent(bot_engine: BotEngine) -> None:
    """Calling stop() multiple times should only execute shutdown once."""
    mock_client = _make_mock_client()

    with (
        patch("core.bot.Database") as MockDB,
        patch("core.bot.OrderManager") as MockOM,
        patch("core.bot.TelegramAlert") as MockTA,
        patch("core.bot.RiskGuard") as MockRG,
        patch("core.bot.PositionManager"),
        patch("core.bot.WatchlistManager"),
        patch("core.bot.SignalEngine"),
        patch("core.bot.TopGainersScalping") as MockTGS,
        patch("core.bot.TickerStream") as MockTS,
        patch("core.bot.UserDataStream") as MockUS,
        patch("core.bot.KlineStream"),
        patch("core.bot.CandleBuffer"),
    ):
        mock_db = AsyncMock()
        MockDB.return_value = mock_db

        mock_om = MagicMock()
        mock_om.connect = AsyncMock()
        mock_om.close = AsyncMock()
        mock_om._client = mock_client
        mock_om.close_position = AsyncMock()
        mock_om.get_open_positions = AsyncMock(return_value=[])
        MockOM.return_value = mock_om

        mock_ta = MagicMock()
        mock_ta.notify_started = AsyncMock()
        mock_ta.notify_stopped = AsyncMock()
        MockTA.return_value = mock_ta

        mock_rg = MagicMock()
        mock_rg.load_daily_state = AsyncMock()
        MockRG.return_value = mock_rg

        mock_tgs = MagicMock()
        mock_tgs.start = AsyncMock()
        mock_tgs.stop = AsyncMock()
        mock_tgs.close_all_positions = AsyncMock()
        MockTGS.return_value = mock_tgs

        mock_ts = MagicMock()
        mock_ts.connect = AsyncMock()
        mock_ts.disconnect = AsyncMock()
        MockTS.return_value = mock_ts

        mock_us = MagicMock()
        mock_us.connect = AsyncMock()
        mock_us.disconnect = AsyncMock()
        MockUS.return_value = mock_us

        async def trigger_double_stop() -> None:
            await asyncio.sleep(0.05)
            await bot_engine.stop()
            await bot_engine.stop()  # Second call should be no-op

        asyncio.create_task(trigger_double_stop())
        await bot_engine.start()

        # notify_stopped should only be called once
        assert mock_ta.notify_stopped.await_count == 1


# ---------------------------------------------------------------------------
# Callback wiring tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ticker_update_forwards_to_watchlist_manager(
    bot_engine: BotEngine,
) -> None:
    """on_ticker_update should forward tickers to WatchlistManager and refresh."""
    mock_wm = MagicMock()
    mock_wm.update_tickers = MagicMock()
    mock_wm.refresh = AsyncMock()
    bot_engine._watchlist_manager = mock_wm

    tickers = [
        TickerData(symbol="SOLUSDT", price_change_pct=5.0, last_price=100.0, quote_volume=50_000_000),
    ]

    await bot_engine._on_ticker_update(tickers)

    mock_wm.update_tickers.assert_called_once_with(tickers)
    mock_wm.refresh.assert_awaited_once()
    assert bot_engine._latest_ticker_prices["SOLUSDT"] == 100.0


@pytest.mark.asyncio
async def test_ticker_update_checks_exits_for_open_positions(
    bot_engine: BotEngine,
) -> None:
    """on_ticker_update should run exit checks using live ticker prices."""
    mock_wm = MagicMock()
    mock_wm.update_tickers = MagicMock()
    mock_wm.refresh = AsyncMock()
    bot_engine._watchlist_manager = mock_wm

    mock_pm = MagicMock()
    mock_pm.get_open_positions = MagicMock(
        return_value=[SimpleNamespace(symbol="SOLUSDT")],
    )
    mock_pm.check_exits = AsyncMock()
    bot_engine._position_manager = mock_pm

    tickers = [
        TickerData(symbol="SOLUSDT", price_change_pct=5.0, last_price=100.0, quote_volume=50_000_000),
        TickerData(symbol="ETHUSDT", price_change_pct=3.0, last_price=3000.0, quote_volume=100_000_000),
    ]

    await bot_engine._on_ticker_update(tickers)

    mock_pm.check_exits.assert_awaited_once_with("SOLUSDT", 100.0)


@pytest.mark.asyncio
async def test_watchlist_changed_subscribes_and_unsubscribes_kline(
    bot_engine: BotEngine,
) -> None:
    """on_watchlist_changed should subscribe added and unsubscribe removed symbols."""
    mock_ks = MagicMock()
    mock_ks.subscribe = AsyncMock()
    mock_ks.unsubscribe = AsyncMock()
    bot_engine._kline_stream = mock_ks

    mock_pm = MagicMock()
    mock_pm.has_position = MagicMock(return_value=False)
    bot_engine._position_manager = mock_pm

    mock_ta = MagicMock()
    mock_ta.notify_watchlist_changed = AsyncMock()
    bot_engine._telegram = mock_ta

    await bot_engine._on_watchlist_changed(
        added=["SOLUSDT", "ETHUSDT"],
        removed=["BTCUSDT"],
    )

    assert mock_ks.subscribe.await_count == 2
    mock_ks.subscribe.assert_any_await("SOLUSDT")
    mock_ks.subscribe.assert_any_await("ETHUSDT")
    mock_ks.unsubscribe.assert_awaited_once_with("BTCUSDT")
    mock_ta.notify_watchlist_changed.assert_awaited_once()


@pytest.mark.asyncio
async def test_watchlist_changed_skips_unsubscribe_for_open_position(
    bot_engine: BotEngine,
) -> None:
    """Removed symbols with open positions should NOT be unsubscribed from KlineStream."""
    mock_ks = MagicMock()
    mock_ks.subscribe = AsyncMock()
    mock_ks.unsubscribe = AsyncMock()
    bot_engine._kline_stream = mock_ks

    mock_pm = MagicMock()
    mock_pm.has_position = MagicMock(return_value=True)
    bot_engine._position_manager = mock_pm

    mock_ta = MagicMock()
    mock_ta.notify_watchlist_changed = AsyncMock()
    bot_engine._telegram = mock_ta

    await bot_engine._on_watchlist_changed(added=[], removed=["BTCUSDT"])

    mock_ks.unsubscribe.assert_not_awaited()


@pytest.mark.asyncio
async def test_candle_closed_adds_to_buffer_and_triggers_strategy(
    bot_engine: BotEngine,
) -> None:
    """on_candle_closed should add candle to buffer and call strategy."""
    mock_cb = MagicMock()
    mock_cb.add = AsyncMock()
    bot_engine._candle_buffer = mock_cb

    mock_strategy = MagicMock()
    mock_strategy.on_candle_closed = AsyncMock()
    bot_engine._strategy = mock_strategy

    candle = {"open": 100.0, "high": 105.0, "low": 99.0, "close": 103.0, "volume": 1000.0, "timestamp": 123456}

    await bot_engine._on_candle_closed("SOLUSDT", "3m", candle)

    mock_cb.add.assert_awaited_once_with("SOLUSDT", "3m", candle)
    mock_strategy.on_candle_closed.assert_awaited_once_with("SOLUSDT", "3m")


@pytest.mark.asyncio
async def test_disconnect_timeout_closes_all_positions(
    bot_engine: BotEngine,
) -> None:
    """on_disconnect_timeout should close all positions via strategy."""
    mock_strategy = MagicMock()
    mock_strategy.close_all_positions = AsyncMock()
    bot_engine._strategy = mock_strategy

    await bot_engine._on_disconnect_timeout()

    mock_strategy.close_all_positions.assert_awaited_once()


@pytest.mark.asyncio
async def test_reconnected_sends_telegram_alert(
    bot_engine: BotEngine,
) -> None:
    """on_reconnected should send a Telegram reconnection alert."""
    mock_ta = MagicMock()
    mock_ta.notify_reconnected = AsyncMock()
    bot_engine._telegram = mock_ta

    await bot_engine._on_reconnected(45.3)

    mock_ta.notify_reconnected.assert_awaited_once_with(45.3)


@pytest.mark.asyncio
async def test_order_update_reconciles_exchange_side_stop_fill(
    bot_engine: BotEngine,
    app_config: AppConfig,
) -> None:
    """A filled protective stop from user-data should close local state."""
    mock_close = AsyncMock()
    position_manager = PositionManager(
        exit_config=app_config.strategy.exit,
        close_order_fn=mock_close,
        cancel_order_fn=AsyncMock(),
    )
    callback = AsyncMock()
    position_manager.on_position_closed = callback
    position = position_manager.open("SOLUSDT", SignalDirection.LONG, 100.0, 1.0, 5)
    position.trade_id = 42
    position.stop_order_id = 111
    bot_engine._position_manager = position_manager

    await bot_engine._on_order_update(
        OrderUpdate(
            symbol="SOLUSDT",
            order_id=111,
            client_order_id="stop",
            side="SELL",
            order_type="STOP_MARKET",
            status="FILLED",
            execution_type="TRADE",
            avg_price=99.0,
            last_fill_price=99.0,
            last_fill_qty=1.0,
            cumulative_filled_qty=1.0,
            realized_pnl_usdt=-1.0,
            reduce_only=False,
            close_position=True,
            stop_price=99.0,
        )
    )

    mock_close.assert_not_awaited()
    callback.assert_awaited_once()
    result = callback.call_args.args[0]
    assert result.trade_id == 42
    assert result.pnl_usdt == pytest.approx(-1.0)
    assert result.exit_reason.value == "SL"
    assert not position_manager.has_position("SOLUSDT")


# ---------------------------------------------------------------------------
# Startup recovery tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recover_open_positions_restores_exchange_position(
    bot_engine: BotEngine,
    app_config: AppConfig,
) -> None:
    """Startup recovery should restore live Binance positions into memory."""
    mock_om = MagicMock()
    mock_om.get_open_positions = AsyncMock(
        return_value=[
            ExchangePosition(
                symbol="SOLUSDT",
                side=SignalDirection.LONG,
                quantity=0.6,
                entry_price=100.0,
                leverage=5,
            )
        ]
    )
    mock_om.get_open_stop_orders = AsyncMock(return_value=[])
    mock_om.place_stop_loss = AsyncMock(
        return_value=OrderResult(
            order_id=777,
            symbol="SOLUSDT",
            side="SELL",
            quantity=0.0,
            status="NEW",
        )
    )
    mock_om.close_position = AsyncMock()
    bot_engine._order_manager = mock_om

    position_manager = PositionManager(
        exit_config=app_config.strategy.exit,
        close_order_fn=mock_om.close_position,
        replace_stop_order_fn=AsyncMock(),
        cancel_order_fn=AsyncMock(),
    )
    bot_engine._position_manager = position_manager

    mock_repo = MagicMock()
    mock_repo.get_open_trades = AsyncMock(
        return_value=[
            OpenTradeRecord(
                id=42,
                symbol="SOLUSDT",
                side="LONG",
                entry_price=100.0,
                quantity=1.0,
                leverage=5,
                entry_at=datetime(2025, 6, 1, 12, 0, 0),
                signal_snapshot="{}",
            )
        ]
    )
    bot_engine._trade_repo = mock_repo

    await bot_engine._recover_open_positions()

    positions = position_manager.get_open_positions()
    assert len(positions) == 1
    assert positions[0].symbol == "SOLUSDT"
    assert positions[0].quantity == 0.6
    assert positions[0].original_quantity == 1.0
    assert positions[0].trade_id == 42
    assert positions[0].tp1_hit is True
    assert positions[0].sl_price == 100.0
    assert positions[0].stop_order_id == 777
    mock_om.place_stop_loss.assert_awaited_once()


# ---------------------------------------------------------------------------
# Balance helper tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_balance_returns_usdt_balance(
    bot_engine: BotEngine,
) -> None:
    """_get_balance should return the USDT balance from Binance."""
    mock_client = AsyncMock()
    mock_client.futures_account_balance = AsyncMock(
        return_value=[
            {"asset": "BTC", "balance": "0.5"},
            {"asset": "USDT", "balance": "1234.56"},
            {"asset": "ETH", "balance": "10.0"},
        ],
    )

    mock_om = MagicMock()
    mock_om._client = mock_client
    bot_engine._order_manager = mock_om

    balance = await bot_engine._get_balance()
    assert balance == 1234.56


@pytest.mark.asyncio
async def test_get_current_price_uses_cached_ticker_price(
    bot_engine: BotEngine,
) -> None:
    """_get_current_price should prefer the websocket ticker cache."""
    mock_om = MagicMock()
    mock_om.get_symbol_price = AsyncMock(return_value=101.0)
    bot_engine._order_manager = mock_om
    bot_engine._latest_ticker_prices["SOLUSDT"] = 100.0

    price = await bot_engine._get_current_price("SOLUSDT")

    assert price == 100.0
    mock_om.get_symbol_price.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_current_price_falls_back_to_rest_price(
    bot_engine: BotEngine,
) -> None:
    """If no ticker cache exists, _get_current_price should query Binance REST."""
    mock_om = MagicMock()
    mock_om.get_symbol_price = AsyncMock(return_value=101.0)
    bot_engine._order_manager = mock_om

    price = await bot_engine._get_current_price("SOLUSDT")

    assert price == 101.0
    mock_om.get_symbol_price.assert_awaited_once_with("SOLUSDT")


@pytest.mark.asyncio
async def test_get_balance_returns_zero_when_usdt_not_found(
    bot_engine: BotEngine,
) -> None:
    """_get_balance should return 0.0 if USDT is not in the response."""
    mock_client = AsyncMock()
    mock_client.futures_account_balance = AsyncMock(
        return_value=[{"asset": "BTC", "balance": "0.5"}],
    )

    mock_om = MagicMock()
    mock_om._client = mock_client
    bot_engine._order_manager = mock_om

    balance = await bot_engine._get_balance()
    assert balance == 0.0


@pytest.mark.asyncio
async def test_get_balance_returns_zero_on_api_error(
    bot_engine: BotEngine,
) -> None:
    """_get_balance should return 0.0 and not crash on API errors."""
    mock_client = AsyncMock()
    mock_client.futures_account_balance = AsyncMock(
        side_effect=Exception("API error"),
    )

    mock_om = MagicMock()
    mock_om._client = mock_client
    bot_engine._order_manager = mock_om

    balance = await bot_engine._get_balance()
    assert balance == 0.0


@pytest.mark.asyncio
async def test_get_free_margin_pct_uses_account_available_balance(
    bot_engine: BotEngine,
) -> None:
    """_get_free_margin_pct should use Binance futures account margin fields."""
    mock_client = AsyncMock()
    mock_client.futures_account = AsyncMock(
        return_value={
            "availableBalance": "2500.0",
            "totalWalletBalance": "10000.0",
        },
    )

    mock_om = MagicMock()
    mock_om._client = mock_client
    bot_engine._order_manager = mock_om

    free_margin_pct = await bot_engine._get_free_margin_pct()
    assert free_margin_pct == 25.0


@pytest.mark.asyncio
async def test_get_free_margin_pct_returns_zero_on_api_error(
    bot_engine: BotEngine,
) -> None:
    """_get_free_margin_pct should fail closed when account margin fetch fails."""
    mock_client = AsyncMock()
    mock_client.futures_account = AsyncMock(side_effect=Exception("API error"))

    mock_om = MagicMock()
    mock_om._client = mock_client
    bot_engine._order_manager = mock_om

    free_margin_pct = await bot_engine._get_free_margin_pct()
    assert free_margin_pct == 0.0


# ---------------------------------------------------------------------------
# External close reconciliation tests
# ---------------------------------------------------------------------------


def _make_order_update(**overrides) -> OrderUpdate:
    """Build an OrderUpdate with sensible defaults, overridden by kwargs."""
    defaults = dict(
        symbol="SOLUSDT",
        order_id=999,
        client_order_id="manual_close",
        side="SELL",
        order_type="MARKET",
        status="FILLED",
        execution_type="TRADE",
        avg_price=99.0,
        last_fill_price=99.0,
        last_fill_qty=1.0,
        cumulative_filled_qty=1.0,
        realized_pnl_usdt=-1.0,
        reduce_only=True,
        close_position=False,
        stop_price=0.0,
        maker_type="",
    )
    defaults.update(overrides)
    return OrderUpdate(**defaults)


def _open_tracked_position(
    bot_engine: BotEngine,
    app_config: AppConfig,
    symbol: str = "SOLUSDT",
    side: SignalDirection = SignalDirection.LONG,
    entry_price: float = 100.0,
    quantity: float = 1.0,
) -> PositionManager:
    """Wire a PositionManager with one open position into bot_engine."""
    mock_close = AsyncMock()
    pm = PositionManager(
        exit_config=app_config.strategy.exit,
        close_order_fn=mock_close,
        cancel_order_fn=AsyncMock(),
    )
    pm.on_position_closed = AsyncMock()
    position = pm.open(symbol, side, entry_price, quantity, 5)
    position.trade_id = 42
    position.stop_order_id = 111
    bot_engine._position_manager = pm
    return pm


@pytest.mark.asyncio
async def test_manual_market_close_reconciled_as_external(
    bot_engine: BotEngine,
    app_config: AppConfig,
) -> None:
    """A manual MARKET close order should reconcile with reason EXTERNAL."""
    pm = _open_tracked_position(bot_engine, app_config)

    await bot_engine._on_order_update(
        _make_order_update(
            order_type="MARKET",
            client_order_id="manual_close",
            side="SELL",
        )
    )

    pm.on_position_closed.assert_awaited_once()
    result = pm.on_position_closed.call_args.args[0]
    assert result.exit_reason.value == "EXTERNAL"
    assert result.pnl_usdt == pytest.approx(-1.0)
    assert not pm.has_position("SOLUSDT")


@pytest.mark.asyncio
async def test_manual_limit_close_reconciled_as_external(
    bot_engine: BotEngine,
    app_config: AppConfig,
) -> None:
    """A manual LIMIT close order should reconcile with reason EXTERNAL."""
    pm = _open_tracked_position(bot_engine, app_config)

    await bot_engine._on_order_update(
        _make_order_update(
            order_type="LIMIT",
            client_order_id="manual_limit",
            side="SELL",
        )
    )

    pm.on_position_closed.assert_awaited_once()
    result = pm.on_position_closed.call_args.args[0]
    assert result.exit_reason.value == "EXTERNAL"
    assert not pm.has_position("SOLUSDT")


@pytest.mark.asyncio
async def test_liquidation_reconciled_as_liquidation(
    bot_engine: BotEngine,
    app_config: AppConfig,
) -> None:
    """A liquidation fill should reconcile with reason LIQUIDATION."""
    pm = _open_tracked_position(bot_engine, app_config)

    await bot_engine._on_order_update(
        _make_order_update(
            order_type="MARKET",
            client_order_id="autoclose_liq",
            maker_type="LIQUIDATION",
            side="SELL",
        )
    )

    pm.on_position_closed.assert_awaited_once()
    result = pm.on_position_closed.call_args.args[0]
    assert result.exit_reason.value == "LIQUIDATION"
    assert not pm.has_position("SOLUSDT")


@pytest.mark.asyncio
async def test_stop_market_still_reconciled_as_sl(
    bot_engine: BotEngine,
    app_config: AppConfig,
) -> None:
    """A STOP_MARKET fill should still reconcile with reason SL."""
    pm = _open_tracked_position(bot_engine, app_config)

    await bot_engine._on_order_update(
        _make_order_update(
            order_type="STOP_MARKET",
            client_order_id="stop",
            close_position=True,
            side="SELL",
        )
    )

    pm.on_position_closed.assert_awaited_once()
    result = pm.on_position_closed.call_args.args[0]
    assert result.exit_reason.value == "SL"


@pytest.mark.asyncio
async def test_bot_originated_market_order_is_skipped(
    bot_engine: BotEngine,
    app_config: AppConfig,
) -> None:
    """Bot-originated MARKET orders (csb_ prefix) should be skipped."""
    pm = _open_tracked_position(bot_engine, app_config)

    await bot_engine._on_order_update(
        _make_order_update(
            order_type="MARKET",
            client_order_id="csb_abc123",
            side="SELL",
        )
    )

    pm.on_position_closed.assert_not_awaited()
    assert pm.has_position("SOLUSDT")


@pytest.mark.asyncio
async def test_bot_originated_stop_order_is_not_skipped(
    bot_engine: BotEngine,
    app_config: AppConfig,
) -> None:
    """Bot-originated STOP_MARKET orders (csb_ prefix) must still reconcile."""
    pm = _open_tracked_position(bot_engine, app_config)

    await bot_engine._on_order_update(
        _make_order_update(
            order_type="STOP_MARKET",
            client_order_id="csb_stop_abc123",
            close_position=True,
            side="SELL",
        )
    )

    pm.on_position_closed.assert_awaited_once()
    result = pm.on_position_closed.call_args.args[0]
    assert result.exit_reason.value == "SL"
    assert not pm.has_position("SOLUSDT")


@pytest.mark.asyncio
async def test_wrong_side_fill_is_ignored(
    bot_engine: BotEngine,
    app_config: AppConfig,
) -> None:
    """A BUY fill on a LONG position (same side) should be ignored."""
    pm = _open_tracked_position(bot_engine, app_config)

    await bot_engine._on_order_update(
        _make_order_update(side="BUY")
    )

    pm.on_position_closed.assert_not_awaited()
    assert pm.has_position("SOLUSDT")


@pytest.mark.asyncio
async def test_short_position_manual_close_reconciled(
    bot_engine: BotEngine,
    app_config: AppConfig,
) -> None:
    """A manual BUY close on a SHORT position should reconcile as EXTERNAL."""
    pm = _open_tracked_position(
        bot_engine, app_config, side=SignalDirection.SHORT,
    )

    await bot_engine._on_order_update(
        _make_order_update(
            order_type="MARKET",
            client_order_id="manual_close",
            side="BUY",
            avg_price=101.0,
            last_fill_price=101.0,
            realized_pnl_usdt=-1.0,
        )
    )

    pm.on_position_closed.assert_awaited_once()
    result = pm.on_position_closed.call_args.args[0]
    assert result.exit_reason.value == "EXTERNAL"
    assert not pm.has_position("SOLUSDT")


@pytest.mark.asyncio
async def test_partial_fill_accumulates_then_reconciles_on_filled(
    bot_engine: BotEngine,
    app_config: AppConfig,
) -> None:
    """PARTIALLY_FILLED should accumulate; FILLED should reconcile the total."""
    pm = _open_tracked_position(bot_engine, app_config, quantity=2.0)

    # First partial fill
    await bot_engine._on_order_update(
        _make_order_update(
            order_id=500,
            status="PARTIALLY_FILLED",
            last_fill_qty=0.8,
            last_fill_price=99.0,
            cumulative_filled_qty=0.8,
            realized_pnl_usdt=-0.8,
        )
    )
    pm.on_position_closed.assert_not_awaited()
    assert pm.has_position("SOLUSDT")

    # Second partial fill
    await bot_engine._on_order_update(
        _make_order_update(
            order_id=500,
            status="PARTIALLY_FILLED",
            last_fill_qty=0.7,
            last_fill_price=98.5,
            cumulative_filled_qty=1.5,
            realized_pnl_usdt=-1.05,
        )
    )
    pm.on_position_closed.assert_not_awaited()

    # Final fill
    await bot_engine._on_order_update(
        _make_order_update(
            order_id=500,
            status="FILLED",
            last_fill_qty=0.5,
            last_fill_price=98.0,
            cumulative_filled_qty=2.0,
            realized_pnl_usdt=-1.0,
        )
    )
    pm.on_position_closed.assert_awaited_once()
    result = pm.on_position_closed.call_args.args[0]
    assert result.exit_reason.value == "EXTERNAL"
    # Accumulated qty: 0.8 + 0.7 + 0.5 = 2.0
    assert not pm.has_position("SOLUSDT")


@pytest.mark.asyncio
async def test_untracked_symbol_fill_is_ignored(
    bot_engine: BotEngine,
    app_config: AppConfig,
) -> None:
    """A fill for a symbol with no tracked position should be ignored."""
    pm = _open_tracked_position(bot_engine, app_config, symbol="SOLUSDT")

    await bot_engine._on_order_update(
        _make_order_update(symbol="ETHUSDT", side="SELL")
    )

    pm.on_position_closed.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_fill_status_is_ignored(
    bot_engine: BotEngine,
    app_config: AppConfig,
) -> None:
    """Order statuses like NEW or CANCELED should be ignored."""
    pm = _open_tracked_position(bot_engine, app_config)

    for status in ("NEW", "CANCELED", "REJECTED", "EXPIRED"):
        await bot_engine._on_order_update(
            _make_order_update(status=status)
        )

    pm.on_position_closed.assert_not_awaited()
    assert pm.has_position("SOLUSDT")


# ---------------------------------------------------------------------------
# Periodic reconciliation tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_phantom_position_closes_local_state(
    bot_engine: BotEngine,
    app_config: AppConfig,
) -> None:
    """A position tracked locally but gone from exchange should be reconciled."""
    pm = _open_tracked_position(bot_engine, app_config)

    mock_om = MagicMock()
    mock_om.get_open_positions = AsyncMock(return_value=[])
    bot_engine._order_manager = mock_om
    bot_engine._latest_ticker_prices["SOLUSDT"] = 98.0

    mock_ta = MagicMock()
    mock_ta.notify_reconciliation = AsyncMock()
    bot_engine._telegram = mock_ta

    await bot_engine._reconcile_exchange_positions()

    pm.on_position_closed.assert_awaited_once()
    result = pm.on_position_closed.call_args.args[0]
    assert result.exit_reason.value == "RECONCILED"
    assert not pm.has_position("SOLUSDT")
    mock_ta.notify_reconciliation.assert_awaited_once()
    call_kwargs = mock_ta.notify_reconciliation.call_args.kwargs
    assert call_kwargs["action"] == "phantom_closed"


@pytest.mark.asyncio
async def test_reconcile_quantity_drift_syncs_local_state(
    bot_engine: BotEngine,
    app_config: AppConfig,
) -> None:
    """When exchange qty < local qty, local state should be synced down."""
    pm = _open_tracked_position(bot_engine, app_config, quantity=1.0)

    mock_om = MagicMock()
    mock_om.get_open_positions = AsyncMock(
        return_value=[
            ExchangePosition(
                symbol="SOLUSDT",
                side=SignalDirection.LONG,
                quantity=0.6,
                entry_price=100.0,
                leverage=5,
            )
        ]
    )
    bot_engine._order_manager = mock_om

    mock_ta = MagicMock()
    mock_ta.notify_reconciliation = AsyncMock()
    bot_engine._telegram = mock_ta

    await bot_engine._reconcile_exchange_positions()

    position = pm.get_position("SOLUSDT")
    assert position is not None
    assert position.quantity == pytest.approx(0.6)
    assert position.tp1_hit is True
    assert position.sl_price == position.entry_price
    mock_ta.notify_reconciliation.assert_awaited_once()
    call_kwargs = mock_ta.notify_reconciliation.call_args.kwargs
    assert call_kwargs["action"] == "quantity_drift"


@pytest.mark.asyncio
async def test_reconcile_orphan_position_sends_alert(
    bot_engine: BotEngine,
    app_config: AppConfig,
) -> None:
    """An exchange position not tracked locally should trigger an alert."""
    pm = PositionManager(
        exit_config=app_config.strategy.exit,
        close_order_fn=AsyncMock(),
        cancel_order_fn=AsyncMock(),
    )
    bot_engine._position_manager = pm

    mock_om = MagicMock()
    mock_om.get_open_positions = AsyncMock(
        return_value=[
            ExchangePosition(
                symbol="ETHUSDT",
                side=SignalDirection.SHORT,
                quantity=2.0,
                entry_price=3500.0,
                leverage=5,
            )
        ]
    )
    bot_engine._order_manager = mock_om

    mock_ta = MagicMock()
    mock_ta.notify_reconciliation = AsyncMock()
    bot_engine._telegram = mock_ta

    await bot_engine._reconcile_exchange_positions()

    mock_ta.notify_reconciliation.assert_awaited_once()
    call_kwargs = mock_ta.notify_reconciliation.call_args.kwargs
    assert call_kwargs["symbol"] == "ETHUSDT"
    assert call_kwargs["action"] == "orphan_detected"


@pytest.mark.asyncio
async def test_reconcile_matching_positions_no_action(
    bot_engine: BotEngine,
    app_config: AppConfig,
) -> None:
    """When local and exchange match, no reconciliation action is taken."""
    pm = _open_tracked_position(bot_engine, app_config, quantity=1.0)

    mock_om = MagicMock()
    mock_om.get_open_positions = AsyncMock(
        return_value=[
            ExchangePosition(
                symbol="SOLUSDT",
                side=SignalDirection.LONG,
                quantity=1.0,
                entry_price=100.0,
                leverage=5,
            )
        ]
    )
    bot_engine._order_manager = mock_om

    mock_ta = MagicMock()
    mock_ta.notify_reconciliation = AsyncMock()
    bot_engine._telegram = mock_ta

    await bot_engine._reconcile_exchange_positions()

    pm.on_position_closed.assert_not_awaited()
    mock_ta.notify_reconciliation.assert_not_awaited()
    assert pm.has_position("SOLUSDT")


@pytest.mark.asyncio
async def test_reconcile_exchange_api_failure_skips_cycle(
    bot_engine: BotEngine,
    app_config: AppConfig,
) -> None:
    """If exchange position fetch fails, the cycle should be skipped gracefully."""
    pm = _open_tracked_position(bot_engine, app_config)

    mock_om = MagicMock()
    mock_om.get_open_positions = AsyncMock(side_effect=Exception("API error"))
    bot_engine._order_manager = mock_om

    # Should not raise
    await bot_engine._reconcile_exchange_positions()

    pm.on_position_closed.assert_not_awaited()
    assert pm.has_position("SOLUSDT")


@pytest.mark.asyncio
async def test_phantom_exit_price_falls_back_to_rest_api(
    bot_engine: BotEngine,
    app_config: AppConfig,
) -> None:
    """Phantom exit price should fall back to REST API when no ticker cache."""
    pm = _open_tracked_position(bot_engine, app_config)

    mock_om = MagicMock()
    mock_om.get_open_positions = AsyncMock(return_value=[])
    mock_om.get_symbol_price = AsyncMock(return_value=97.5)
    bot_engine._order_manager = mock_om
    bot_engine._latest_ticker_prices.clear()

    mock_ta = MagicMock()
    mock_ta.notify_reconciliation = AsyncMock()
    bot_engine._telegram = mock_ta

    await bot_engine._reconcile_exchange_positions()

    pm.on_position_closed.assert_awaited_once()
    result = pm.on_position_closed.call_args.args[0]
    assert result.exit_price == pytest.approx(97.5)
    mock_om.get_symbol_price.assert_awaited_once_with("SOLUSDT")


@pytest.mark.asyncio
async def test_phantom_exit_price_falls_back_to_sl_price(
    bot_engine: BotEngine,
    app_config: AppConfig,
) -> None:
    """Phantom exit price should fall back to SL price when all else fails."""
    pm = _open_tracked_position(bot_engine, app_config)
    position = pm.get_position("SOLUSDT")
    assert position is not None
    expected_sl = position.sl_price

    mock_om = MagicMock()
    mock_om.get_open_positions = AsyncMock(return_value=[])
    mock_om.get_symbol_price = AsyncMock(side_effect=Exception("API error"))
    bot_engine._order_manager = mock_om
    bot_engine._latest_ticker_prices.clear()

    mock_ta = MagicMock()
    mock_ta.notify_reconciliation = AsyncMock()
    bot_engine._telegram = mock_ta

    await bot_engine._reconcile_exchange_positions()

    pm.on_position_closed.assert_awaited_once()
    result = pm.on_position_closed.call_args.args[0]
    assert result.exit_price == pytest.approx(expected_sl)
