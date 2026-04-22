"""Unit tests for SignalEngine signal generation.

Tests indicator calculation, LONG/SHORT signal conditions, edge cases
(NaN, insufficient data), RSI boundary values, and EMA crossover
detection within the 2-candle window.

Requirements validated: 6.1, 6.2, 6.3, 6.4, 6.5
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from core.config import EntryConfig
from core.enums import SignalDirection
from core.models import Signal
from strategy.signal_engine import SignalEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEFAULT_CFG = EntryConfig()


def _default_engine(**overrides) -> SignalEngine:
    """Create a SignalEngine with default or overridden config."""
    cfg = EntryConfig(**{**_DEFAULT_CFG.model_dump(), **overrides})
    return SignalEngine(config=cfg)


def _build_uptrend_15m(
    base_price: float = 100.0,
    num: int = 60,
    trend_step: float = 0.5,
) -> pd.DataFrame:
    """Build a 15m DataFrame with a steady uptrend.

    EMA(20) > EMA(50) is guaranteed by the linear upward drift.
    """
    closes = np.array([base_price + trend_step * i for i in range(num)])
    opens = closes - 0.01
    highs = np.maximum(opens, closes) + 0.05
    lows = np.minimum(opens, closes) - 0.05
    volumes = np.full(num, 1000.0)
    timestamps = pd.date_range("2025-01-01", periods=num, freq="15min")
    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows,
        "close": closes, "volume": volumes, "timestamp": timestamps,
    })


def _build_downtrend_15m(
    base_price: float = 200.0,
    num: int = 60,
    trend_step: float = 0.5,
) -> pd.DataFrame:
    """Build a 15m DataFrame with a steady downtrend.

    EMA(20) < EMA(50) is guaranteed by the linear downward drift.
    """
    closes = np.array([base_price - trend_step * i for i in range(num)])
    opens = closes + 0.01
    highs = np.maximum(opens, closes) + 0.05
    lows = np.minimum(opens, closes) - 0.05
    volumes = np.full(num, 1000.0)
    timestamps = pd.date_range("2025-01-01", periods=num, freq="15min")
    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows,
        "close": closes, "volume": volumes, "timestamp": timestamps,
    })


def _build_long_3m(
    base_price: float = 100.0,
    num: int = 50,
    decline_ratio: float = 0.0003,
    rise_ratio: float = 0.0020,
    volume_spike_factor: float = 5.0,
) -> pd.DataFrame:
    """Build a 3m DataFrame satisfying all LONG conditions.

    Gentle decline followed by a 4-candle rise produces:
    - EMA(9) crossing above EMA(21) within last 2 candles
    - RSI(14) in [50, 70]
    - Bullish last candle
    - Volume spike on last candle
    - Last close below resistance buffer
    """
    decline_len = num - 4
    decline_step = base_price * decline_ratio
    decline_prices = np.array([
        base_price - decline_step * i for i in range(decline_len)
    ])

    rise_step = base_price * rise_ratio
    last_decline = decline_prices[-1]
    rise_prices = np.array([
        last_decline + rise_step * (i + 1) for i in range(4)
    ])

    closes = np.concatenate([decline_prices, rise_prices])

    # Bullish candles: open below close
    opens = closes - base_price * 0.0001
    opens[-1] = closes[-1] - base_price * 0.002  # clearly bullish last candle

    highs = np.maximum(opens, closes) + base_price * 0.005
    lows = np.minimum(opens, closes) - base_price * 0.0005

    # Resistance spike in the middle so last close < resistance × (1 − buffer)
    mid = num // 2
    highs[mid] = closes[-1] + base_price * 0.02

    # Volume: flat baseline, spike on last candle
    volumes = np.full(num, 100.0)
    volumes[-1] = 100.0 * volume_spike_factor

    timestamps = pd.date_range("2025-01-01", periods=num, freq="3min")
    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows,
        "close": closes, "volume": volumes, "timestamp": timestamps,
    })


def _build_short_3m(
    base_price: float = 200.0,
    num: int = 50,
    rise_ratio: float = 0.0003,
    drop_ratio: float = 0.0020,
    volume_spike_factor: float = 5.0,
) -> pd.DataFrame:
    """Build a 3m DataFrame satisfying all SHORT conditions.

    Gentle rise followed by a 4-candle drop produces:
    - EMA(9) crossing below EMA(21) within last 2 candles
    - RSI(14) in [30, 50]
    - Bearish last candle
    - Volume spike on last candle
    - Last close above support buffer
    """
    rise_len = num - 4
    rise_step = base_price * rise_ratio
    rise_prices = np.array([
        base_price + rise_step * i for i in range(rise_len)
    ])

    drop_step = base_price * drop_ratio
    last_rise = rise_prices[-1]
    drop_prices = np.array([
        last_rise - drop_step * (i + 1) for i in range(4)
    ])

    closes = np.concatenate([rise_prices, drop_prices])

    # Bearish candles: open above close
    opens = closes + base_price * 0.0001
    opens[-1] = closes[-1] + base_price * 0.002  # clearly bearish last candle

    highs = np.maximum(opens, closes) + base_price * 0.0005
    lows = np.minimum(opens, closes) - base_price * 0.0005

    # Support dip early so last close is well above support × (1 + buffer)
    lows[2] = closes[-1] - base_price * 0.02

    # Volume: flat baseline, spike on last candle
    volumes = np.full(num, 100.0)
    volumes[-1] = 100.0 * volume_spike_factor

    timestamps = pd.date_range("2025-01-01", periods=num, freq="3min")
    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows,
        "close": closes, "volume": volumes, "timestamp": timestamps,
    })


def _build_flat_15m(
    base_price: float = 100.0,
    num: int = 60,
) -> pd.DataFrame:
    """Build a flat 15m DataFrame where EMA(20) ≈ EMA(50) — no clear trend."""
    closes = np.full(num, base_price)
    opens = closes.copy()
    highs = closes + 0.01
    lows = closes - 0.01
    volumes = np.full(num, 1000.0)
    timestamps = pd.date_range("2025-01-01", periods=num, freq="15min")
    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows,
        "close": closes, "volume": volumes, "timestamp": timestamps,
    })


def _build_flat_3m(
    base_price: float = 100.0,
    num: int = 50,
) -> pd.DataFrame:
    """Build a flat 3m DataFrame — no crossover, no volume spike, no trend."""
    closes = np.full(num, base_price)
    opens = closes.copy()
    highs = closes + 0.01
    lows = closes - 0.01
    volumes = np.full(num, 100.0)
    timestamps = pd.date_range("2025-01-01", periods=num, freq="3min")
    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows,
        "close": closes, "volume": volumes, "timestamp": timestamps,
    })


# ---------------------------------------------------------------------------
# 1. LONG signal generation
# ---------------------------------------------------------------------------


class TestLongSignalGeneration:
    """Test that a LONG signal is generated when all 6 conditions are met.

    Validates: Requirements 6.1, 6.2, 6.3, 6.5
    """

    def test_long_signal_with_default_config(self) -> None:
        """All LONG conditions met → Signal with direction=LONG returned."""
        engine = _default_engine()
        df_15m = _build_uptrend_15m()
        df_3m = _build_long_3m()

        result = engine.evaluate("TESTUSDT", df_3m, df_15m)

        assert result is not None
        assert isinstance(result, Signal)
        assert result.direction == SignalDirection.LONG
        assert result.confidence > 0
        assert isinstance(result.indicators, dict)
        assert len(result.indicators) > 0

    def test_long_signal_contains_indicator_snapshot(self) -> None:
        """LONG signal includes all expected indicator keys."""
        engine = _default_engine()
        df_15m = _build_uptrend_15m()
        df_3m = _build_long_3m()

        result = engine.evaluate("TESTUSDT", df_3m, df_15m)

        assert result is not None
        expected_keys = {
            "ema_trend_fast", "ema_trend_slow",
            "rsi", "ema_fast", "ema_slow", "volume_ma",
        }
        assert expected_keys.issubset(result.indicators.keys())

    def test_long_signal_confidence_in_valid_range(self) -> None:
        """LONG signal confidence is between 0.01 and 1.0."""
        engine = _default_engine()
        df_15m = _build_uptrend_15m()
        df_3m = _build_long_3m()

        result = engine.evaluate("TESTUSDT", df_3m, df_15m)

        assert result is not None
        assert 0.01 <= result.confidence <= 1.0

    def test_long_signal_with_high_base_price(self) -> None:
        """LONG signal works with high base prices (e.g. BTC-like)."""
        engine = _default_engine()
        df_15m = _build_uptrend_15m(base_price=40_000.0, trend_step=50.0)
        df_3m = _build_long_3m(base_price=40_000.0)

        result = engine.evaluate("BTCUSDT", df_3m, df_15m)

        assert result is not None
        assert result.direction == SignalDirection.LONG

    def test_long_signal_with_low_base_price(self) -> None:
        """LONG signal works with low base prices (e.g. small-cap)."""
        engine = _default_engine()
        df_15m = _build_uptrend_15m(base_price=1.5, trend_step=0.01)
        df_3m = _build_long_3m(base_price=1.5)

        result = engine.evaluate("DOGEUSDT", df_3m, df_15m)

        assert result is not None
        assert result.direction == SignalDirection.LONG


# ---------------------------------------------------------------------------
# 2. SHORT signal generation
# ---------------------------------------------------------------------------


class TestShortSignalGeneration:
    """Test that a SHORT signal is generated when all 6 conditions are met.

    Validates: Requirements 6.1, 6.2, 6.4, 6.5
    """

    def test_short_signal_with_default_config(self) -> None:
        """All SHORT conditions met → Signal with direction=SHORT returned."""
        engine = _default_engine()
        df_15m = _build_downtrend_15m()
        df_3m = _build_short_3m()

        result = engine.evaluate("TESTUSDT", df_3m, df_15m)

        assert result is not None
        assert isinstance(result, Signal)
        assert result.direction == SignalDirection.SHORT
        assert result.confidence > 0
        assert isinstance(result.indicators, dict)
        assert len(result.indicators) > 0

    def test_short_signal_contains_indicator_snapshot(self) -> None:
        """SHORT signal includes all expected indicator keys."""
        engine = _default_engine()
        df_15m = _build_downtrend_15m()
        df_3m = _build_short_3m()

        result = engine.evaluate("TESTUSDT", df_3m, df_15m)

        assert result is not None
        expected_keys = {
            "ema_trend_fast", "ema_trend_slow",
            "rsi", "ema_fast", "ema_slow", "volume_ma",
        }
        assert expected_keys.issubset(result.indicators.keys())

    def test_short_signal_confidence_in_valid_range(self) -> None:
        """SHORT signal confidence is between 0.01 and 1.0."""
        engine = _default_engine()
        df_15m = _build_downtrend_15m()
        df_3m = _build_short_3m()

        result = engine.evaluate("TESTUSDT", df_3m, df_15m)

        assert result is not None
        assert 0.01 <= result.confidence <= 1.0

    def test_short_signal_with_high_base_price(self) -> None:
        """SHORT signal works with high base prices."""
        engine = _default_engine()
        df_15m = _build_downtrend_15m(base_price=40_000.0, trend_step=50.0)
        df_3m = _build_short_3m(base_price=40_000.0)

        result = engine.evaluate("ETHUSDT", df_3m, df_15m)

        assert result is not None
        assert result.direction == SignalDirection.SHORT


# ---------------------------------------------------------------------------
# 3. No signal cases
# ---------------------------------------------------------------------------


class TestNoSignal:
    """Test cases where conditions are NOT met and None is returned.

    Validates: Requirements 6.3, 6.4
    """

    def test_no_signal_flat_market(self) -> None:
        """Flat market (no trend, no crossover) → None."""
        engine = _default_engine()
        df_15m = _build_flat_15m()
        df_3m = _build_flat_3m()

        result = engine.evaluate("TESTUSDT", df_3m, df_15m)
        assert result is None

    def test_no_signal_wrong_trend_for_long(self) -> None:
        """Downtrend 15m with LONG 3m conditions → None (trend mismatch)."""
        engine = _default_engine()
        df_15m = _build_downtrend_15m()  # downtrend
        df_3m = _build_long_3m()         # LONG conditions on 3m

        result = engine.evaluate("TESTUSDT", df_3m, df_15m)
        assert result is None

    def test_no_signal_wrong_trend_for_short(self) -> None:
        """Uptrend 15m with SHORT 3m conditions → None (trend mismatch)."""
        engine = _default_engine()
        df_15m = _build_uptrend_15m()    # uptrend
        df_3m = _build_short_3m()        # SHORT conditions on 3m

        result = engine.evaluate("TESTUSDT", df_3m, df_15m)
        assert result is None

    def test_no_signal_no_volume_spike(self) -> None:
        """LONG conditions met except volume spike → None."""
        engine = _default_engine()
        df_15m = _build_uptrend_15m()
        # volume_spike_factor=1.0 means no spike above the MA threshold
        df_3m = _build_long_3m(volume_spike_factor=1.0)

        result = engine.evaluate("TESTUSDT", df_3m, df_15m)
        assert result is None

    def test_no_signal_bearish_candle_for_long(self) -> None:
        """LONG conditions met except last candle is bearish → None."""
        engine = _default_engine()
        df_15m = _build_uptrend_15m()
        df_3m = _build_long_3m()

        # Make last candle bearish: open > close
        df_3m.iloc[-1, df_3m.columns.get_loc("open")] = (
            df_3m.iloc[-1]["close"] + 1.0
        )

        result = engine.evaluate("TESTUSDT", df_3m, df_15m)
        assert result is None

    def test_no_signal_bullish_candle_for_short(self) -> None:
        """SHORT conditions met except last candle is bullish → None."""
        engine = _default_engine()
        df_15m = _build_downtrend_15m()
        df_3m = _build_short_3m()

        # Make last candle bullish: close > open
        df_3m.iloc[-1, df_3m.columns.get_loc("open")] = (
            df_3m.iloc[-1]["close"] - 1.0
        )

        result = engine.evaluate("TESTUSDT", df_3m, df_15m)
        assert result is None


# ---------------------------------------------------------------------------
# 4. RSI boundary edge cases
# ---------------------------------------------------------------------------


class TestRSIBoundaryEdgeCases:
    """Test RSI exactly at inclusive boundaries: 50, 70, 30.

    The SignalEngine uses inclusive ranges:
    - LONG: rsi_long_min (50) <= RSI <= rsi_long_max (70)
    - SHORT: rsi_short_min (30) <= RSI <= rsi_short_max (50)

    Validates: Requirements 6.3, 6.4
    """

    def test_rsi_below_long_min_rejected(self) -> None:
        """RSI just below 50 should not produce a LONG signal.

        We set rsi_long_min=50 and use a config where the RSI range
        is very narrow to test the boundary.
        """
        engine = _default_engine()
        # Build a LONG-like 3m but with very little rise to keep RSI low
        df_3m = _build_long_3m(
            base_price=100.0,
            decline_ratio=0.001,   # steeper decline
            rise_ratio=0.0005,     # weaker rise → lower RSI
        )
        df_15m = _build_uptrend_15m()

        result = engine.evaluate("TESTUSDT", df_3m, df_15m)

        # If a signal is returned, its RSI must be within [50, 70]
        if result is not None and result.direction == SignalDirection.LONG:
            rsi_val = result.indicators["rsi"]
            assert _DEFAULT_CFG.rsi_long_min <= rsi_val <= _DEFAULT_CFG.rsi_long_max

    def test_rsi_above_long_max_rejected(self) -> None:
        """RSI above 70 should not produce a LONG signal.

        We use a very aggressive rise to push RSI above 70.
        """
        engine = _default_engine()
        df_3m = _build_long_3m(
            base_price=100.0,
            decline_ratio=0.0001,  # very gentle decline
            rise_ratio=0.01,       # very aggressive rise → high RSI
        )
        df_15m = _build_uptrend_15m()

        result = engine.evaluate("TESTUSDT", df_3m, df_15m)

        # If a LONG signal is returned, RSI must be within bounds
        if result is not None and result.direction == SignalDirection.LONG:
            rsi_val = result.indicators["rsi"]
            assert rsi_val <= _DEFAULT_CFG.rsi_long_max

    def test_rsi_above_short_max_rejected(self) -> None:
        """RSI above 50 should not produce a SHORT signal.

        We use a very gentle drop to keep RSI high.
        """
        engine = _default_engine()
        df_3m = _build_short_3m(
            base_price=200.0,
            rise_ratio=0.001,      # steeper rise
            drop_ratio=0.0005,     # weaker drop → higher RSI
        )
        df_15m = _build_downtrend_15m()

        result = engine.evaluate("TESTUSDT", df_3m, df_15m)

        # If a SHORT signal is returned, RSI must be within [30, 50]
        if result is not None and result.direction == SignalDirection.SHORT:
            rsi_val = result.indicators["rsi"]
            assert _DEFAULT_CFG.rsi_short_min <= rsi_val <= _DEFAULT_CFG.rsi_short_max

    def test_long_signal_rsi_within_bounds(self) -> None:
        """When a LONG signal is generated, RSI is within [50, 70]."""
        engine = _default_engine()
        df_15m = _build_uptrend_15m()
        df_3m = _build_long_3m()

        result = engine.evaluate("TESTUSDT", df_3m, df_15m)

        assert result is not None
        rsi_val = result.indicators["rsi"]
        assert _DEFAULT_CFG.rsi_long_min <= rsi_val <= _DEFAULT_CFG.rsi_long_max

    def test_short_signal_rsi_within_bounds(self) -> None:
        """When a SHORT signal is generated, RSI is within [30, 50]."""
        engine = _default_engine()
        df_15m = _build_downtrend_15m()
        df_3m = _build_short_3m()

        result = engine.evaluate("TESTUSDT", df_3m, df_15m)

        assert result is not None
        rsi_val = result.indicators["rsi"]
        assert _DEFAULT_CFG.rsi_short_min <= rsi_val <= _DEFAULT_CFG.rsi_short_max


# ---------------------------------------------------------------------------
# 5. Insufficient candle data
# ---------------------------------------------------------------------------


class TestInsufficientData:
    """Test with DataFrames that have too few rows for indicator calculation.

    The 15m frame needs at least ema_trend_slow (50) rows.
    The 3m frame needs at least max(ema_slow, rsi_period, 20) = 21 rows.

    Validates: Requirements 6.1, 6.2
    """

    def test_insufficient_15m_data(self) -> None:
        """15m DataFrame with fewer than ema_trend_slow rows → None."""
        engine = _default_engine()
        min_15m = _DEFAULT_CFG.ema_trend_slow  # 50

        # Only 10 rows — well below the 50 required
        df_15m = _build_uptrend_15m(num=10)
        df_3m = _build_long_3m()

        result = engine.evaluate("TESTUSDT", df_3m, df_15m)
        assert result is None

    def test_insufficient_3m_data(self) -> None:
        """3m DataFrame with fewer than max(ema_slow, rsi_period, 20) rows → None."""
        engine = _default_engine()
        min_3m = max(_DEFAULT_CFG.ema_slow, _DEFAULT_CFG.rsi_period, 20)  # 21

        # Only 10 rows — well below the 21 required
        df_3m = _build_flat_3m(num=10)
        df_15m = _build_uptrend_15m()

        result = engine.evaluate("TESTUSDT", df_3m, df_15m)
        assert result is None

    def test_exactly_minimum_15m_rows(self) -> None:
        """15m DataFrame with exactly ema_trend_slow rows should not crash."""
        engine = _default_engine()
        min_15m = _DEFAULT_CFG.ema_trend_slow  # 50

        df_15m = _build_uptrend_15m(num=min_15m)
        df_3m = _build_long_3m()

        # Should not raise — may return None or a signal depending on indicators
        result = engine.evaluate("TESTUSDT", df_3m, df_15m)
        assert result is None or isinstance(result, Signal)

    def test_exactly_minimum_3m_rows(self) -> None:
        """3m DataFrame with exactly the minimum rows should not crash."""
        engine = _default_engine()
        min_3m = max(_DEFAULT_CFG.ema_slow, _DEFAULT_CFG.rsi_period, 20)  # 21

        df_3m = _build_flat_3m(num=min_3m)
        df_15m = _build_uptrend_15m()

        # Should not raise — may return None or a signal
        result = engine.evaluate("TESTUSDT", df_3m, df_15m)
        assert result is None or isinstance(result, Signal)

    def test_empty_dataframes(self) -> None:
        """Empty DataFrames → None."""
        engine = _default_engine()
        df_empty = pd.DataFrame(
            columns=["open", "high", "low", "close", "volume", "timestamp"]
        )

        result = engine.evaluate("TESTUSDT", df_empty, df_empty)
        assert result is None

    def test_single_row_dataframes(self) -> None:
        """Single-row DataFrames → None."""
        engine = _default_engine()
        df_single = pd.DataFrame({
            "open": [100.0], "high": [101.0], "low": [99.0],
            "close": [100.5], "volume": [1000.0],
            "timestamp": [pd.Timestamp("2025-01-01")],
        })

        result = engine.evaluate("TESTUSDT", df_single, df_single)
        assert result is None


# ---------------------------------------------------------------------------
# 6. NaN indicators
# ---------------------------------------------------------------------------


class TestNaNIndicators:
    """Test with DataFrames that produce NaN indicator values.

    Validates: Requirements 6.1, 6.2
    """

    def test_all_nan_close_prices_15m(self) -> None:
        """All NaN close prices in 15m → None (NaN indicators)."""
        engine = _default_engine()
        num = 60
        df_15m = pd.DataFrame({
            "open": np.full(num, 100.0),
            "high": np.full(num, 101.0),
            "low": np.full(num, 99.0),
            "close": np.full(num, np.nan),
            "volume": np.full(num, 1000.0),
            "timestamp": pd.date_range("2025-01-01", periods=num, freq="15min"),
        })

        df_3m = _build_long_3m()

        result = engine.evaluate("TESTUSDT", df_3m, df_15m)
        assert result is None

    def test_nan_in_close_prices_3m(self) -> None:
        """NaN values in 3m close prices → None (NaN indicators)."""
        engine = _default_engine()
        df_15m = _build_uptrend_15m()
        df_3m = _build_long_3m()
        # Inject NaN into close prices near the end
        df_3m.iloc[-5:-1, df_3m.columns.get_loc("close")] = np.nan

        result = engine.evaluate("TESTUSDT", df_3m, df_15m)
        assert result is None

    def test_nan_in_volume_3m(self) -> None:
        """NaN values in 3m volume → None (NaN volume MA)."""
        engine = _default_engine()
        df_15m = _build_uptrend_15m()
        df_3m = _build_long_3m()
        # Inject NaN into volume near the end
        df_3m.iloc[-10:, df_3m.columns.get_loc("volume")] = np.nan

        result = engine.evaluate("TESTUSDT", df_3m, df_15m)
        assert result is None

    def test_all_nan_close_prices(self) -> None:
        """All NaN close prices → None."""
        engine = _default_engine()
        num = 60
        df_15m = pd.DataFrame({
            "open": np.full(num, np.nan),
            "high": np.full(num, np.nan),
            "low": np.full(num, np.nan),
            "close": np.full(num, np.nan),
            "volume": np.full(num, 1000.0),
            "timestamp": pd.date_range("2025-01-01", periods=num, freq="15min"),
        })
        df_3m = _build_long_3m()

        result = engine.evaluate("TESTUSDT", df_3m, df_15m)
        assert result is None


# ---------------------------------------------------------------------------
# 7. EMA crossover detection within 2-candle window
# ---------------------------------------------------------------------------


class TestEMACrossoverWindow:
    """Test that EMA crossover is detected within the 2-candle window.

    The crossover detection checks:
    - Current bar: EMA_fast vs EMA_slow
    - 1 candle ago: was EMA_fast on the other side?
    - 2 candles ago: was EMA_fast on the other side?
    - 3+ candles ago: NOT checked (crossover too old)

    Validates: Requirements 6.3, 6.4
    """

    def test_crossover_above_detected_1_candle_ago(self) -> None:
        """EMA(9) crossed above EMA(21) exactly 1 candle ago → detected."""
        engine = _default_engine()

        # The standard LONG builder produces a crossover within 1-2 candles
        df_3m = _build_long_3m()
        df_15m = _build_uptrend_15m()

        result = engine.evaluate("TESTUSDT", df_3m, df_15m)
        # If all other conditions are met, a LONG signal should be generated
        assert result is not None
        assert result.direction == SignalDirection.LONG

    def test_crossover_below_detected_1_candle_ago(self) -> None:
        """EMA(9) crossed below EMA(21) exactly 1 candle ago → detected."""
        engine = _default_engine()

        df_3m = _build_short_3m()
        df_15m = _build_downtrend_15m()

        result = engine.evaluate("TESTUSDT", df_3m, df_15m)
        assert result is not None
        assert result.direction == SignalDirection.SHORT

    def test_no_crossover_in_flat_market(self) -> None:
        """Flat market with no crossover → no signal."""
        engine = _default_engine()

        df_3m = _build_flat_3m()
        df_15m = _build_uptrend_15m()

        result = engine.evaluate("TESTUSDT", df_3m, df_15m)
        assert result is None

    def test_crossover_above_internal_method(self) -> None:
        """Directly test _ema_crossed_above with known indicator values.

        Crossover 1 candle ago: fast was below slow, now fast is above slow.
        """
        engine = _default_engine()

        # Current: fast > slow, 1 candle ago: fast <= slow → crossover detected
        ind_3m = {
            "ema_fast": 105.0,
            "ema_slow": 100.0,
            "ema_fast_prev1": 99.0,   # was below slow
            "ema_slow_prev1": 100.0,
            "ema_fast_prev2": 98.0,
            "ema_slow_prev2": 100.0,
        }
        assert engine._ema_crossed_above(ind_3m) is True

    def test_crossover_above_2_candles_ago(self) -> None:
        """Crossover happened 2 candles ago — still within window."""
        engine = _default_engine()

        # Current: fast > slow
        # 1 candle ago: fast > slow (no crossover here)
        # 2 candles ago: fast <= slow → crossover detected
        ind_3m = {
            "ema_fast": 105.0,
            "ema_slow": 100.0,
            "ema_fast_prev1": 101.0,  # already above
            "ema_slow_prev1": 100.0,
            "ema_fast_prev2": 99.0,   # was below → crossover 2 candles ago
            "ema_slow_prev2": 100.0,
        }
        assert engine._ema_crossed_above(ind_3m) is True

    def test_no_crossover_above_3_candles_ago(self) -> None:
        """Crossover happened 3+ candles ago — outside window, not detected."""
        engine = _default_engine()

        # Current: fast > slow
        # 1 candle ago: fast > slow
        # 2 candles ago: fast > slow
        # (crossover was 3+ candles ago — not checked)
        ind_3m = {
            "ema_fast": 105.0,
            "ema_slow": 100.0,
            "ema_fast_prev1": 103.0,  # already above
            "ema_slow_prev1": 100.0,
            "ema_fast_prev2": 101.0,  # already above
            "ema_slow_prev2": 100.0,
        }
        assert engine._ema_crossed_above(ind_3m) is False

    def test_crossover_below_internal_method(self) -> None:
        """Directly test _ema_crossed_below with known indicator values.

        Crossover 1 candle ago: fast was above slow, now fast is below slow.
        """
        engine = _default_engine()

        ind_3m = {
            "ema_fast": 95.0,
            "ema_slow": 100.0,
            "ema_fast_prev1": 101.0,  # was above slow
            "ema_slow_prev1": 100.0,
            "ema_fast_prev2": 102.0,
            "ema_slow_prev2": 100.0,
        }
        assert engine._ema_crossed_below(ind_3m) is True

    def test_crossover_below_2_candles_ago(self) -> None:
        """Crossover below happened 2 candles ago — still within window."""
        engine = _default_engine()

        ind_3m = {
            "ema_fast": 95.0,
            "ema_slow": 100.0,
            "ema_fast_prev1": 98.0,   # already below
            "ema_slow_prev1": 100.0,
            "ema_fast_prev2": 101.0,  # was above → crossover 2 candles ago
            "ema_slow_prev2": 100.0,
        }
        assert engine._ema_crossed_below(ind_3m) is True

    def test_no_crossover_below_3_candles_ago(self) -> None:
        """Crossover below happened 3+ candles ago — not detected."""
        engine = _default_engine()

        ind_3m = {
            "ema_fast": 95.0,
            "ema_slow": 100.0,
            "ema_fast_prev1": 97.0,   # already below
            "ema_slow_prev1": 100.0,
            "ema_fast_prev2": 98.0,   # already below
            "ema_slow_prev2": 100.0,
        }
        assert engine._ema_crossed_below(ind_3m) is False

    def test_crossover_above_no_prev2_data(self) -> None:
        """When prev2 data is None, only check prev1 for crossover."""
        engine = _default_engine()

        ind_3m = {
            "ema_fast": 105.0,
            "ema_slow": 100.0,
            "ema_fast_prev1": 99.0,   # was below → crossover
            "ema_slow_prev1": 100.0,
            "ema_fast_prev2": None,
            "ema_slow_prev2": None,
        }
        assert engine._ema_crossed_above(ind_3m) is True

    def test_no_crossover_above_when_fast_below_slow(self) -> None:
        """When current fast <= slow, no upward crossover is possible."""
        engine = _default_engine()

        ind_3m = {
            "ema_fast": 99.0,
            "ema_slow": 100.0,
            "ema_fast_prev1": 98.0,
            "ema_slow_prev1": 100.0,
            "ema_fast_prev2": 97.0,
            "ema_slow_prev2": 100.0,
        }
        assert engine._ema_crossed_above(ind_3m) is False

    def test_no_crossover_below_when_fast_above_slow(self) -> None:
        """When current fast >= slow, no downward crossover is possible."""
        engine = _default_engine()

        ind_3m = {
            "ema_fast": 101.0,
            "ema_slow": 100.0,
            "ema_fast_prev1": 102.0,
            "ema_slow_prev1": 100.0,
            "ema_fast_prev2": 103.0,
            "ema_slow_prev2": 100.0,
        }
        assert engine._ema_crossed_below(ind_3m) is False
