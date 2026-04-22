"""Property-based tests for SignalEngine signal generation.

Uses hypothesis to vary base price and volume spike magnitude while
constructing DataFrames that deterministically satisfy all six LONG
or SHORT entry conditions.  The decline/rise ratios are fixed at
values proven to produce the required RSI ranges with the appropriate
EMA(9)/EMA(21) crossover direction.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from hypothesis import given, settings, HealthCheck, strategies as st

from core.config import EntryConfig
from core.enums import SignalDirection
from core.models import Signal
from strategy.signal_engine import SignalEngine


# ---------------------------------------------------------------------------
# Fixed config
# ---------------------------------------------------------------------------

_DEFAULT_ENTRY_CONFIG = EntryConfig()

# These ratios are calibrated so that the resulting price series produces
# RSI ≈ 69.7 (within [50, 70]) and an EMA(9) > EMA(21) crossover within
# the last 2 candles.  Because the ratios are proportional to base_price,
# the RSI value is scale-invariant.
_DECLINE_RATIO = 0.0003   # per-candle decline as fraction of base_price
_RISE_RATIO = 0.0020      # per-candle rise as fraction of base_price

# SHORT ratios: gentle rise followed by a sharp drop.  Produces
# RSI ≈ 30–50 and an EMA(9) < EMA(21) crossover within the last 2 candles.
_SHORT_RISE_RATIO = 0.0003   # per-candle rise as fraction of base_price
_SHORT_DROP_RATIO = 0.0020   # per-candle drop as fraction of base_price


# ---------------------------------------------------------------------------
# DataFrame builders — shared
# ---------------------------------------------------------------------------

def _build_uptrend_15m_df(
    base_price: float,
    trend_strength: float,
) -> pd.DataFrame:
    """Build a 60-candle 15m DataFrame with a steady uptrend.

    The linear drift ensures EMA(20) > EMA(50) for any positive
    trend_strength.
    """
    num = 60
    closes = np.array([
        base_price + trend_strength * i for i in range(num)
    ])
    opens = closes - 0.01
    highs = np.maximum(opens, closes) + 0.05
    lows = np.minimum(opens, closes) - 0.05
    volumes = np.full(num, 1000.0)
    timestamps = pd.date_range("2025-01-01", periods=num, freq="15min")

    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows,
        "close": closes, "volume": volumes, "timestamp": timestamps,
    })


def _build_downtrend_15m_df(
    base_price: float,
    trend_strength: float,
) -> pd.DataFrame:
    """Build a 60-candle 15m DataFrame with a steady downtrend.

    The linear drift ensures EMA(20) < EMA(50) for any positive
    trend_strength.
    """
    num = 60
    closes = np.array([
        base_price - trend_strength * i for i in range(num)
    ])
    opens = closes + 0.01
    highs = np.maximum(opens, closes) + 0.05
    lows = np.minimum(opens, closes) - 0.05
    volumes = np.full(num, 1000.0)
    timestamps = pd.date_range("2025-01-01", periods=num, freq="15min")

    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows,
        "close": closes, "volume": volumes, "timestamp": timestamps,
    })


# ---------------------------------------------------------------------------
# DataFrame builders — LONG
# ---------------------------------------------------------------------------

def _build_long_3m_df(
    base_price: float,
    volume_spike_factor: float,
) -> pd.DataFrame:
    """Build a 50-candle 3m DataFrame satisfying all LONG conditions.

    The price series has a gentle decline followed by a 4-candle rise,
    producing:
      - EMA(9) crossing above EMA(21) within the last 2 candles
      - RSI(14) ≈ 69.7  (in [50, 70])
      - Bullish last candle (close > open)
      - Volume spike on the last candle
      - Last close below resistance × (1 − buffer)
    """
    num = 50
    decline_len = num - 4
    decline_step = base_price * _DECLINE_RATIO
    decline_prices = np.array([
        base_price - decline_step * i for i in range(decline_len)
    ])

    rise_step = base_price * _RISE_RATIO
    last_decline = decline_prices[-1]
    rise_prices = np.array([
        last_decline + rise_step * (i + 1) for i in range(4)
    ])

    closes = np.concatenate([decline_prices, rise_prices])

    # Opens: slightly below close (bullish), last candle clearly bullish
    opens = closes - base_price * 0.0001
    opens[-1] = closes[-1] - base_price * 0.002

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


# ---------------------------------------------------------------------------
# DataFrame builders — SHORT
# ---------------------------------------------------------------------------

def _build_short_3m_df(
    base_price: float,
    volume_spike_factor: float,
) -> pd.DataFrame:
    """Build a 50-candle 3m DataFrame satisfying all SHORT conditions.

    The price series has a gentle rise followed by a 4-candle drop,
    producing:
      - EMA(9) crossing below EMA(21) within the last 2 candles
      - RSI(14) ≈ 30–50  (in [30, 50])
      - Bearish last candle (close < open)
      - Volume spike on the last candle
      - Last close above nearest support × (1 + resistance_buffer_pct/100)
    """
    num = 50
    rise_len = num - 4

    # Gentle rise phase — pushes EMA(9) above EMA(21) before the drop.
    rise_step = base_price * _SHORT_RISE_RATIO
    rise_prices = np.array([
        base_price + rise_step * i for i in range(rise_len)
    ])

    # Sharp drop phase — pulls EMA(9) below EMA(21) and drops RSI.
    drop_step = base_price * _SHORT_DROP_RATIO
    last_rise = rise_prices[-1]
    drop_prices = np.array([
        last_rise - drop_step * (i + 1) for i in range(4)
    ])

    closes = np.concatenate([rise_prices, drop_prices])

    # Opens: slightly above close (bearish), last candle clearly bearish.
    opens = closes + base_price * 0.0001
    opens[-1] = closes[-1] + base_price * 0.002

    highs = np.maximum(opens, closes) + base_price * 0.0005
    lows = np.minimum(opens, closes) - base_price * 0.0005

    # Support dip early in the series so that the last close is well above
    # support × (1 + buffer).  The lowest low must be far enough below the
    # last close.
    lows[2] = closes[-1] - base_price * 0.02

    # Volume: flat baseline, spike on last candle.
    volumes = np.full(num, 100.0)
    volumes[-1] = 100.0 * volume_spike_factor

    timestamps = pd.date_range("2025-01-01", periods=num, freq="3min")

    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows,
        "close": closes, "volume": volumes, "timestamp": timestamps,
    })


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

_base_price = st.floats(
    min_value=1.0, max_value=50_000.0,
    allow_nan=False, allow_infinity=False,
)

_trend_strength = st.floats(
    min_value=0.05, max_value=2.0,
    allow_nan=False, allow_infinity=False,
)

# Must exceed config volume_multiplier (default 1.5) to guarantee spike.
_volume_spike = st.floats(
    min_value=3.0, max_value=20.0,
    allow_nan=False, allow_infinity=False,
)


# ---------------------------------------------------------------------------
# Property 8: LONG signal generation
# ---------------------------------------------------------------------------
# Feature: crypto-scalp-bot, Property 8: LONG signal generation


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.filter_too_much, HealthCheck.too_slow],
)
@given(
    base_price=_base_price,
    trend_strength=_trend_strength,
    volume_spike_factor=_volume_spike,
)
def test_long_signal_generated_when_all_conditions_met(
    base_price: float,
    trend_strength: float,
    volume_spike_factor: float,
) -> None:
    """When all six LONG conditions are simultaneously true, SignalEngine
    returns a Signal with direction=LONG, confidence > 0, and a non-empty
    indicators dict.

    Conditions (all must hold):
    1. 15m EMA(20) > EMA(50)  — uptrend
    2. 3m RSI(14) ∈ [50, 70]
    3. 3m EMA(9) crossed above EMA(21) within last 2 candles
    4. Latest 3m volume > volume_ma_20 × volume_multiplier
    5. Latest 3m candle is bullish (close > open)
    6. Latest 3m close < nearest resistance × (1 − resistance_buffer_pct/100)

    **Validates: Requirements 6.3, 6.5**
    """
    cfg = _DEFAULT_ENTRY_CONFIG
    engine = SignalEngine(config=cfg)

    df_15m = _build_uptrend_15m_df(base_price, trend_strength)
    df_3m = _build_long_3m_df(base_price, volume_spike_factor)

    result = engine.evaluate("TESTUSDT", df_3m, df_15m)

    # --- Assertions ---
    assert result is not None, (
        "SignalEngine returned None when all LONG conditions should be met "
        f"(base_price={base_price:.2f}, trend_strength={trend_strength:.4f}, "
        f"volume_spike={volume_spike_factor:.1f})"
    )
    assert isinstance(result, Signal)
    assert result.direction == SignalDirection.LONG, (
        f"Expected LONG, got {result.direction}"
    )
    assert result.confidence > 0, (
        f"Confidence must be > 0, got {result.confidence}"
    )
    assert isinstance(result.indicators, dict)
    assert len(result.indicators) > 0, "indicators dict must not be empty"

    # Verify indicator snapshot contains expected keys
    expected_keys = {
        "ema_trend_fast", "ema_trend_slow",
        "rsi", "ema_fast", "ema_slow", "volume_ma",
    }
    assert expected_keys.issubset(result.indicators.keys()), (
        f"Missing keys. Expected {expected_keys}, got {set(result.indicators.keys())}"
    )


# ---------------------------------------------------------------------------
# Property 9: SHORT signal generation
# ---------------------------------------------------------------------------
# Feature: crypto-scalp-bot, Property 9: SHORT signal generation


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.filter_too_much, HealthCheck.too_slow],
)
@given(
    base_price=_base_price,
    trend_strength=_trend_strength,
    volume_spike_factor=_volume_spike,
)
def test_short_signal_generated_when_all_conditions_met(
    base_price: float,
    trend_strength: float,
    volume_spike_factor: float,
) -> None:
    """When all six SHORT conditions are simultaneously true, SignalEngine
    returns a Signal with direction=SHORT, confidence > 0, and a non-empty
    indicators dict.

    Conditions (all must hold):
    1. 15m EMA(20) < EMA(50)  — downtrend
    2. 3m RSI(14) ∈ [30, 50]
    3. 3m EMA(9) crossed below EMA(21) within last 2 candles
    4. Latest 3m volume > volume_ma_20 × volume_multiplier
    5. Latest 3m candle is bearish (close < open)
    6. Latest 3m close > nearest support × (1 + resistance_buffer_pct/100)

    **Validates: Requirements 6.4, 6.5**
    """
    cfg = _DEFAULT_ENTRY_CONFIG
    engine = SignalEngine(config=cfg)

    df_15m = _build_downtrend_15m_df(base_price, trend_strength)
    df_3m = _build_short_3m_df(base_price, volume_spike_factor)

    result = engine.evaluate("TESTUSDT", df_3m, df_15m)

    # --- Assertions ---
    assert result is not None, (
        "SignalEngine returned None when all SHORT conditions should be met "
        f"(base_price={base_price:.2f}, trend_strength={trend_strength:.4f}, "
        f"volume_spike={volume_spike_factor:.1f})"
    )
    assert isinstance(result, Signal)
    assert result.direction == SignalDirection.SHORT, (
        f"Expected SHORT, got {result.direction}"
    )
    assert result.confidence > 0, (
        f"Confidence must be > 0, got {result.confidence}"
    )
    assert isinstance(result.indicators, dict)
    assert len(result.indicators) > 0, "indicators dict must not be empty"

    # Verify indicator snapshot contains expected keys
    expected_keys = {
        "ema_trend_fast", "ema_trend_slow",
        "rsi", "ema_fast", "ema_slow", "volume_ma",
    }
    assert expected_keys.issubset(result.indicators.keys()), (
        f"Missing keys. Expected {expected_keys}, got {set(result.indicators.keys())}"
    )
