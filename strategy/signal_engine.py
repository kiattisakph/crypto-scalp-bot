"""Entry signal generation for crypto-scalp-bot.

Calculates technical indicators on 3-minute and 15-minute candle
DataFrames using pandas_ta and generates LONG/SHORT entry signals
when all required conditions are met simultaneously.
"""
from __future__ import annotations

import math

import pandas as pd
import pandas_ta as ta
from loguru import logger

from core.config import EntryConfig
from core.enums import SignalDirection
from core.models import Signal


class SignalEngine:
    """Evaluates multi-indicator entry conditions for a given symbol.

    All indicator parameters are driven by the ``EntryConfig`` loaded
    from ``config.yaml``.  No magic numbers — every threshold and period
    comes from configuration.

    Args:
        config: Entry configuration with indicator periods, RSI ranges,
            volume multiplier, and resistance buffer settings.
    """

    def __init__(self, config: EntryConfig) -> None:
        self._config = config

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def evaluate(
        self,
        symbol: str,
        df_3m: pd.DataFrame,
        df_15m: pd.DataFrame,
    ) -> Signal | None:
        """Evaluate entry conditions for *symbol*.

        Calculates indicators on both timeframes and checks all six
        LONG or SHORT conditions.  Returns a ``Signal`` when all
        conditions for one direction are met, or ``None`` otherwise.

        Args:
            symbol: The trading pair symbol (e.g. ``"SOLUSDT"``).
            df_3m: 3-minute candle DataFrame with columns
                ``open, high, low, close, volume, timestamp``.
            df_15m: 15-minute candle DataFrame with columns
                ``open, high, low, close, volume, timestamp``.

        Returns:
            A ``Signal`` with direction, confidence, and indicator
            snapshot, or ``None`` if conditions are not met.
        """
        # --- Validate input data ---
        if not self._has_enough_data(symbol, df_3m, df_15m):
            return None

        # --- Calculate 15m indicators ---
        indicators_15m = self._calc_15m_indicators(df_15m)
        if indicators_15m is None:
            logger.warning(
                "signal | {symbol} | 15m indicators contain NaN, skipping",
                symbol=symbol,
            )
            return None

        # --- Calculate 3m indicators ---
        indicators_3m = self._calc_3m_indicators(df_3m)
        if indicators_3m is None:
            logger.warning(
                "signal | {symbol} | 3m indicators contain NaN, skipping",
                symbol=symbol,
            )
            return None

        # --- Build full indicator snapshot ---
        snapshot = {**indicators_15m, **indicators_3m}

        latest_volume = float(df_3m["volume"].iloc[-1])

        # --- Check LONG conditions ---
        if self._check_long_conditions(indicators_15m, indicators_3m, df_3m):
            confidence = self._calc_confidence(
                indicators_3m, SignalDirection.LONG, latest_volume,
            )
            logger.info(
                "signal | {symbol} | LONG signal generated | confidence={confidence:.3f}",
                symbol=symbol,
                confidence=confidence,
            )
            return Signal(
                direction=SignalDirection.LONG,
                confidence=confidence,
                indicators=snapshot,
            )

        # --- Check SHORT conditions ---
        if self._check_short_conditions(indicators_15m, indicators_3m, df_3m):
            confidence = self._calc_confidence(
                indicators_3m, SignalDirection.SHORT, latest_volume,
            )
            logger.info(
                "signal | {symbol} | SHORT signal generated | confidence={confidence:.3f}",
                symbol=symbol,
                confidence=confidence,
            )
            return Signal(
                direction=SignalDirection.SHORT,
                confidence=confidence,
                indicators=snapshot,
            )

        return None

    # ------------------------------------------------------------------
    # Data validation
    # ------------------------------------------------------------------

    def _has_enough_data(
        self,
        symbol: str,
        df_3m: pd.DataFrame,
        df_15m: pd.DataFrame,
    ) -> bool:
        """Return ``True`` if both DataFrames have enough rows for indicators.

        The 15m frame needs at least ``ema_trend_slow`` rows (default 50)
        and the 3m frame needs at least ``max(ema_slow, rsi_period, 20)``
        rows (the 20 is for the volume MA).
        """
        min_15m = self._config.ema_trend_slow
        min_3m = max(self._config.ema_slow, self._config.rsi_period, 20)

        if df_15m is None or len(df_15m) < min_15m:
            logger.debug(
                "signal | {symbol} | Insufficient 15m data: {n}/{required}",
                symbol=symbol,
                n=0 if df_15m is None else len(df_15m),
                required=min_15m,
            )
            return False

        if df_3m is None or len(df_3m) < min_3m:
            logger.debug(
                "signal | {symbol} | Insufficient 3m data: {n}/{required}",
                symbol=symbol,
                n=0 if df_3m is None else len(df_3m),
                required=min_3m,
            )
            return False

        return True

    # ------------------------------------------------------------------
    # Indicator calculation
    # ------------------------------------------------------------------

    def _calc_15m_indicators(self, df_15m: pd.DataFrame) -> dict | None:
        """Calculate 15-minute trend indicators.

        Returns:
            Dict with ``ema_trend_fast`` and ``ema_trend_slow`` values,
            or ``None`` if any result is NaN.
        """
        ema_fast = ta.ema(df_15m["close"], length=self._config.ema_trend_fast)
        ema_slow = ta.ema(df_15m["close"], length=self._config.ema_trend_slow)

        if ema_fast is None or ema_slow is None:
            return None

        ema_fast_val = ema_fast.iloc[-1]
        ema_slow_val = ema_slow.iloc[-1]

        if _is_nan(ema_fast_val) or _is_nan(ema_slow_val):
            return None

        return {
            "ema_trend_fast": float(ema_fast_val),
            "ema_trend_slow": float(ema_slow_val),
        }

    def _calc_3m_indicators(self, df_3m: pd.DataFrame) -> dict | None:
        """Calculate 3-minute entry indicators.

        Returns:
            Dict with ``rsi``, ``ema_fast``, ``ema_slow``, ``volume_ma``,
            ``atr``, ``adx``, ``ema_fast_prev``, ``ema_slow_prev`` values, or
            ``None`` if any result is NaN.
        """
        rsi = ta.rsi(df_3m["close"], length=self._config.rsi_period)
        ema_fast = ta.ema(df_3m["close"], length=self._config.ema_fast)
        ema_slow = ta.ema(df_3m["close"], length=self._config.ema_slow)
        volume_ma = ta.sma(df_3m["volume"], length=20)
        atr = ta.atr(df_3m["high"], df_3m["low"], df_3m["close"],
                     length=self._config.atr_period)
        adx = ta.adx(df_3m["high"], df_3m["low"], df_3m["close"],
                     length=self._config.adx_period)

        if any(s is None for s in (rsi, ema_fast, ema_slow, volume_ma, atr, adx)):
            return None

        rsi_val = rsi.iloc[-1]
        ema_fast_val = ema_fast.iloc[-1]
        ema_slow_val = ema_slow.iloc[-1]
        volume_ma_val = volume_ma.iloc[-1]
        atr_val = atr.iloc[-1]
        adx_val = adx["ADX_14"].iloc[-1] if "ADX_14" in adx.columns else float("nan")

        # Previous values for crossover detection (up to 2 candles back).
        ema_fast_prev1 = ema_fast.iloc[-2] if len(ema_fast) >= 2 else float("nan")
        ema_slow_prev1 = ema_slow.iloc[-2] if len(ema_slow) >= 2 else float("nan")
        ema_fast_prev2 = ema_fast.iloc[-3] if len(ema_fast) >= 3 else float("nan")
        ema_slow_prev2 = ema_slow.iloc[-3] if len(ema_slow) >= 3 else float("nan")

        vals = [
            rsi_val,
            ema_fast_val,
            ema_slow_val,
            volume_ma_val,
            atr_val,
            adx_val,
            ema_fast_prev1,
            ema_slow_prev1,
        ]
        if any(_is_nan(v) for v in vals):
            return None

        return {
            "rsi": float(rsi_val),
            "ema_fast": float(ema_fast_val),
            "ema_slow": float(ema_slow_val),
            "volume_ma": float(volume_ma_val),
            "atr": float(atr_val),
            "adx": float(adx_val),
            "ema_fast_prev1": float(ema_fast_prev1),
            "ema_slow_prev1": float(ema_slow_prev1),
            "ema_fast_prev2": float(ema_fast_prev2) if not _is_nan(ema_fast_prev2) else None,
            "ema_slow_prev2": float(ema_slow_prev2) if not _is_nan(ema_slow_prev2) else None,
        }

    # ------------------------------------------------------------------
    # Condition checks
    # ------------------------------------------------------------------

    def _check_long_conditions(
        self,
        ind_15m: dict,
        ind_3m: dict,
        df_3m: pd.DataFrame,
    ) -> bool:
        """Return ``True`` if all six LONG entry conditions are met.

        Conditions (all must be true simultaneously):
        1. 15m trend: EMA_trend_fast > EMA_trend_slow (uptrend).
        2. 3m RSI in [rsi_long_min, rsi_long_max].
        3. 3m EMA_fast crossed above EMA_slow within last 2 candles.
        4. Latest 3m volume > volume_ma × volume_multiplier.
        5. Latest 3m candle is bullish (close > open).
        6. Latest 3m close < nearest resistance × (1 - resistance_buffer_pct/100).
        """
        cfg = self._config

        # 1. 15m uptrend
        if ind_15m["ema_trend_fast"] <= ind_15m["ema_trend_slow"]:
            return False

        # 2. RSI range
        if not (cfg.rsi_long_min <= ind_3m["rsi"] <= cfg.rsi_long_max):
            return False

        # 3. EMA crossover within last 2 candles
        if not self._ema_crossed_above(ind_3m):
            return False

        # 4. Volume spike
        latest_volume = float(df_3m["volume"].iloc[-1])
        if latest_volume <= ind_3m["volume_ma"] * cfg.volume_multiplier:
            return False

        # 5. Bullish candle
        latest_close = float(df_3m["close"].iloc[-1])
        latest_open = float(df_3m["open"].iloc[-1])
        if latest_close <= latest_open:
            return False

        # 6. Resistance buffer — close must be below resistance threshold.
        # Use the highest high in the lookback as a proxy for nearest resistance.
        resistance = self._nearest_resistance(df_3m)
        buffer_threshold = resistance * (1 - cfg.resistance_buffer_pct / 100)
        if latest_close >= buffer_threshold:
            return False

        return True

    def _check_short_conditions(
        self,
        ind_15m: dict,
        ind_3m: dict,
        df_3m: pd.DataFrame,
    ) -> bool:
        """Return ``True`` if all six SHORT entry conditions are met.

        Conditions (all must be true simultaneously):
        1. 15m trend: EMA_trend_fast < EMA_trend_slow (downtrend).
        2. 3m RSI in [rsi_short_min, rsi_short_max].
        3. 3m EMA_fast crossed below EMA_slow within last 2 candles.
        4. Latest 3m volume > volume_ma × volume_multiplier.
        5. Latest 3m candle is bearish (close < open).
        6. Latest 3m close > nearest support × (1 + resistance_buffer_pct/100).
        """
        cfg = self._config

        # 1. 15m downtrend
        if ind_15m["ema_trend_fast"] >= ind_15m["ema_trend_slow"]:
            return False

        # 2. RSI range
        if not (cfg.rsi_short_min <= ind_3m["rsi"] <= cfg.rsi_short_max):
            return False

        # 3. EMA crossover within last 2 candles
        if not self._ema_crossed_below(ind_3m):
            return False

        # 4. Volume spike
        latest_volume = float(df_3m["volume"].iloc[-1])
        if latest_volume <= ind_3m["volume_ma"] * cfg.volume_multiplier:
            return False

        # 5. Bearish candle
        latest_close = float(df_3m["close"].iloc[-1])
        latest_open = float(df_3m["open"].iloc[-1])
        if latest_close >= latest_open:
            return False

        # 6. Support buffer — close must be above support threshold.
        # Use the lowest low in the lookback as a proxy for nearest support.
        support = self._nearest_support(df_3m)
        buffer_threshold = support * (1 + cfg.resistance_buffer_pct / 100)
        if latest_close <= buffer_threshold:
            return False

        return True

    # ------------------------------------------------------------------
    # Crossover detection
    # ------------------------------------------------------------------

    def _ema_crossed_above(self, ind_3m: dict) -> bool:
        """Return ``True`` if EMA_fast crossed above EMA_slow within 2 candles.

        A crossover is detected when EMA_fast is currently above EMA_slow
        AND at some point in the previous 1–2 candles EMA_fast was at or
        below EMA_slow.
        """
        # Current bar: fast must be above slow.
        if ind_3m["ema_fast"] <= ind_3m["ema_slow"]:
            return False

        # Check 1 candle ago.
        if ind_3m["ema_fast_prev1"] <= ind_3m["ema_slow_prev1"]:
            return True

        # Check 2 candles ago (if available).
        if ind_3m["ema_fast_prev2"] is not None and ind_3m["ema_slow_prev2"] is not None:
            if ind_3m["ema_fast_prev2"] <= ind_3m["ema_slow_prev2"]:
                return True

        return False

    def _ema_crossed_below(self, ind_3m: dict) -> bool:
        """Return ``True`` if EMA_fast crossed below EMA_slow within 2 candles.

        A crossover is detected when EMA_fast is currently below EMA_slow
        AND at some point in the previous 1–2 candles EMA_fast was at or
        above EMA_slow.
        """
        # Current bar: fast must be below slow.
        if ind_3m["ema_fast"] >= ind_3m["ema_slow"]:
            return False

        # Check 1 candle ago.
        if ind_3m["ema_fast_prev1"] >= ind_3m["ema_slow_prev1"]:
            return True

        # Check 2 candles ago (if available).
        if ind_3m["ema_fast_prev2"] is not None and ind_3m["ema_slow_prev2"] is not None:
            if ind_3m["ema_fast_prev2"] >= ind_3m["ema_slow_prev2"]:
                return True

        return False

    # ------------------------------------------------------------------
    # Support / Resistance proxies
    # ------------------------------------------------------------------

    def _nearest_resistance(self, df_3m: pd.DataFrame) -> float:
        """Return the nearest resistance level as the highest high in the lookback.

        Uses the full DataFrame as the lookback window.  This is a
        simple proxy — a production system might use pivot points or
        order-book data.
        """
        return float(df_3m["high"].max())

    def _nearest_support(self, df_3m: pd.DataFrame) -> float:
        """Return the nearest support level as the lowest low in the lookback.

        Uses the full DataFrame as the lookback window.  This is a
        simple proxy — a production system might use pivot points or
        order-book data.
        """
        return float(df_3m["low"].min())

    # ------------------------------------------------------------------
    # Confidence scoring
    # ------------------------------------------------------------------

    def _calc_confidence(
        self,
        ind_3m: dict,
        direction: SignalDirection,
        latest_volume: float,
    ) -> float:
        """Calculate a confidence score in the range (0, 1].

        The score is a simple average of three normalised sub-scores:

        1. **RSI strength** — how far the RSI is from the midpoint of
           its valid range toward the favourable extreme.
        2. **EMA spread** — the relative distance between the fast and
           slow EMAs (larger spread → stronger momentum).
        3. **Volume ratio** — how much the latest volume exceeds the
           volume MA threshold (capped at 2× the multiplier).

        All sub-scores are clamped to [0, 1].

        Args:
            ind_3m: 3-minute indicator dictionary.
            direction: Signal direction (LONG or SHORT).
            latest_volume: The latest 3m candle volume.

        Returns:
            Confidence score between 0.01 and 1.0.
        """
        cfg = self._config

        # RSI sub-score
        if direction == SignalDirection.LONG:
            rsi_range = cfg.rsi_long_max - cfg.rsi_long_min
            rsi_score = (ind_3m["rsi"] - cfg.rsi_long_min) / rsi_range if rsi_range > 0 else 0.5
        else:
            rsi_range = cfg.rsi_short_max - cfg.rsi_short_min
            rsi_score = (cfg.rsi_short_max - ind_3m["rsi"]) / rsi_range if rsi_range > 0 else 0.5

        # EMA spread sub-score (normalised by slow EMA to make it relative)
        ema_spread = abs(ind_3m["ema_fast"] - ind_3m["ema_slow"])
        ema_score = min(ema_spread / ind_3m["ema_slow"], 1.0) if ind_3m["ema_slow"] != 0 else 0.0

        # Volume sub-score — ratio of latest volume to the threshold,
        # normalised so that 1× threshold = 0.5 and 2× threshold = 1.0.
        vol_threshold = ind_3m["volume_ma"] * cfg.volume_multiplier
        if vol_threshold > 0:
            vol_ratio = latest_volume / vol_threshold
            vol_score = min(vol_ratio / 2.0, 1.0)
        else:
            vol_score = 0.5

        confidence = (rsi_score + ema_score + vol_score) / 3.0
        return max(0.01, min(confidence, 1.0))


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------

def _is_nan(value: float) -> bool:
    """Return ``True`` if *value* is NaN or not a finite number."""
    if value is None:
        return True
    try:
        return math.isnan(value) or not math.isfinite(value)
    except (TypeError, ValueError):
        return True
