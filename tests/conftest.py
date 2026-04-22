"""Shared test fixtures for crypto-scalp-bot.

Provides reusable fixtures for configuration objects, in-memory database,
mock Binance client, and sample market data used across the test suite.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pandas as pd
import pytest
import pytest_asyncio

from core.config import (
    AppConfig,
    EntryConfig,
    EnvSettings,
    ExitConfig,
    RiskConfig,
    StrategyConfig,
    WatchlistConfig,
)
from core.models import TickerData
from storage.database import Database


# ---------------------------------------------------------------------------
# Configuration fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def env_settings() -> EnvSettings:
    """Provide valid EnvSettings with demo mode enabled.

    Uses placeholder credentials suitable for unit tests where no real
    exchange connection is made.
    """
    return EnvSettings(
        binance_api_key="test_api_key",
        binance_api_secret="test_api_secret",
        binance_demo=True,
        telegram_bot_token="test_telegram_token",
        telegram_chat_id="test_chat_id",
        db_path=":memory:",
        log_level="DEBUG",
    )


@pytest.fixture
def app_config() -> AppConfig:
    """Provide a valid AppConfig with default values from config.yaml.

    All sub-configs use their pydantic defaults, matching the production
    config.yaml structure.
    """
    return AppConfig(
        watchlist=WatchlistConfig(
            blacklist=["USDCUSDT", "BUSDUSDT", "BTCDOMUSDT"],
            blacklist_patterns=["UP", "DOWN"],
        ),
        strategy=StrategyConfig(
            entry=EntryConfig(),
            exit=ExitConfig(),
        ),
        risk=RiskConfig(),
    )


# ---------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def in_memory_db() -> Database:
    """Provide an initialised in-memory SQLite database.

    The database is created fresh for each test and closed automatically
    after the test completes.
    """
    db = Database(":memory:")
    await db.init()
    yield db  # type: ignore[misc]
    await db.close()


# ---------------------------------------------------------------------------
# Mock Binance client fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_binance_client() -> AsyncMock:
    """Provide a mocked python-binance AsyncClient.

    Pre-configures common futures methods with sensible return values
    so tests can focus on business logic rather than mock setup.
    """
    client = AsyncMock()

    # Account balance
    client.futures_account_balance = AsyncMock(
        return_value=[{"asset": "USDT", "balance": "10000.0"}],
    )

    # Leverage setting
    client.futures_change_leverage = AsyncMock(
        return_value={"leverage": 5, "symbol": "SOLUSDT"},
    )

    # Order placement — returns a realistic order response
    client.futures_create_order = AsyncMock(
        return_value={
            "orderId": 123456,
            "symbol": "SOLUSDT",
            "side": "BUY",
            "type": "MARKET",
            "origQty": "1.0",
            "executedQty": "1.0",
            "avgPrice": "150.0",
            "status": "FILLED",
        },
    )

    # Connection teardown
    client.close_connection = AsyncMock()

    return client


# ---------------------------------------------------------------------------
# Sample market data fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_ticker_data() -> list[TickerData]:
    """Provide a list of TickerData for watchlist tests.

    Includes a mix of qualifying and non-qualifying symbols to exercise
    filter logic (USDT suffix, volume, price change, blacklist patterns).
    """
    return [
        TickerData(
            symbol="SOLUSDT",
            price_change_pct=8.5,
            last_price=150.0,
            quote_volume=50_000_000.0,
        ),
        TickerData(
            symbol="ETHUSDT",
            price_change_pct=5.2,
            last_price=3500.0,
            quote_volume=200_000_000.0,
        ),
        TickerData(
            symbol="BTCUSDT",
            price_change_pct=3.1,
            last_price=65000.0,
            quote_volume=500_000_000.0,
        ),
        TickerData(
            symbol="DOGEUSDT",
            price_change_pct=12.0,
            last_price=0.15,
            quote_volume=80_000_000.0,
        ),
        TickerData(
            symbol="XRPUSDT",
            price_change_pct=4.0,
            last_price=0.60,
            quote_volume=30_000_000.0,
        ),
        # Non-qualifying: below min volume
        TickerData(
            symbol="LOWVOLUSDT",
            price_change_pct=10.0,
            last_price=1.0,
            quote_volume=1_000_000.0,
        ),
        # Non-qualifying: negative price change
        TickerData(
            symbol="NEGUSDT",
            price_change_pct=-2.0,
            last_price=5.0,
            quote_volume=50_000_000.0,
        ),
        # Non-qualifying: blacklist pattern "UP"
        TickerData(
            symbol="BTCUPUSDT",
            price_change_pct=15.0,
            last_price=10.0,
            quote_volume=100_000_000.0,
        ),
        # Non-qualifying: not USDT pair
        TickerData(
            symbol="SOLBTC",
            price_change_pct=6.0,
            last_price=0.002,
            quote_volume=20_000_000.0,
        ),
    ]


@pytest.fixture
def sample_candles_3m() -> pd.DataFrame:
    """Provide a 3-minute candle DataFrame with enough rows for indicators.

    Contains 100 candles with a gentle uptrend suitable for EMA, RSI,
    and volume moving average calculations. The data is synthetic but
    produces valid indicator values (no NaN in the tail).
    """
    np.random.seed(42)
    n = 100
    base_price = 150.0
    # Gentle uptrend with noise
    trend = np.linspace(0, 5, n)
    noise = np.random.normal(0, 0.5, n)
    closes = base_price + trend + noise

    opens = closes - np.random.uniform(0.1, 0.5, n)
    highs = np.maximum(opens, closes) + np.random.uniform(0.1, 1.0, n)
    lows = np.minimum(opens, closes) - np.random.uniform(0.1, 1.0, n)
    volumes = np.random.uniform(1000, 5000, n)

    # Make the last candle bullish (close > open) with a volume spike
    opens[-1] = closes[-1] - 0.5
    volumes[-1] = volumes[:-1].mean() * 2.0

    timestamps = pd.date_range(
        start="2026-04-22 00:00:00",
        periods=n,
        freq="3min",
        tz=timezone.utc,
    )

    return pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
        "timestamp": timestamps,
    })


@pytest.fixture
def sample_candles_15m() -> pd.DataFrame:
    """Provide a 15-minute candle DataFrame with enough rows for trend EMAs.

    Contains 60 candles with a clear uptrend so that EMA(20) > EMA(50),
    suitable for testing the 15m trend filter in the SignalEngine.
    """
    np.random.seed(123)
    n = 60
    base_price = 148.0
    # Clear uptrend for EMA(20) > EMA(50)
    trend = np.linspace(0, 8, n)
    noise = np.random.normal(0, 0.3, n)
    closes = base_price + trend + noise

    opens = closes - np.random.uniform(0.05, 0.3, n)
    highs = np.maximum(opens, closes) + np.random.uniform(0.1, 0.8, n)
    lows = np.minimum(opens, closes) - np.random.uniform(0.1, 0.8, n)
    volumes = np.random.uniform(5000, 15000, n)

    timestamps = pd.date_range(
        start="2026-04-21 09:00:00",
        periods=n,
        freq="15min",
        tz=timezone.utc,
    )

    return pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
        "timestamp": timestamps,
    })
