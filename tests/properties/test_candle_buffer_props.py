"""Property-based tests for CandleBuffer.

# Feature: crypto-scalp-bot, Property 7: CandleBuffer size invariant and FIFO ordering
"""
from __future__ import annotations

import asyncio

import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from utils.candle_buffer import CandleBuffer

# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

_reasonable_price = st.floats(min_value=0.0001, max_value=100_000.0, allow_nan=False, allow_infinity=False)
_reasonable_volume = st.floats(min_value=0.0, max_value=1e12, allow_nan=False, allow_infinity=False)
_timestamp = st.integers(min_value=1_600_000_000_000, max_value=1_900_000_000_000)


def _candle_strategy(ts: st.SearchStrategy[int] | None = None) -> st.SearchStrategy[dict]:
    """Generate a single candle dict with valid OHLCV data."""
    ts_strat = ts if ts is not None else _timestamp
    return st.fixed_dictionaries({
        "open": _reasonable_price,
        "high": _reasonable_price,
        "low": _reasonable_price,
        "close": _reasonable_price,
        "volume": _reasonable_volume,
        "timestamp": ts_strat,
    })


_candle = _candle_strategy()


# ---------------------------------------------------------------------------
# Property 7: CandleBuffer size invariant and FIFO ordering
# ---------------------------------------------------------------------------


class TestCandleBufferSizeInvariant:
    """Buffer never exceeds max_size for any (symbol, timeframe) pair."""

    @settings(max_examples=100)
    @given(
        max_size=st.integers(min_value=1, max_value=50),
        candles=st.lists(_candle, min_size=1, max_size=120),
    )
    @pytest.mark.asyncio
    async def test_buffer_never_exceeds_max_size(
        self, max_size: int, candles: list[dict]
    ) -> None:
        """For any sequence of candle additions, the buffer size never exceeds max_size."""
        buf = CandleBuffer(max_size=max_size)
        for c in candles:
            await buf.add("SOLUSDT", "3m", c)

        df = await buf.get_df("SOLUSDT", "3m")
        assert len(df) <= max_size


class TestCandleBufferFIFOEviction:
    """Oldest candle is evicted when buffer is at capacity."""

    @settings(max_examples=100)
    @given(
        max_size=st.integers(min_value=1, max_value=20),
        data=st.data(),
    )
    @pytest.mark.asyncio
    async def test_oldest_candle_evicted_at_capacity(
        self, max_size: int, data: st.DataObject
    ) -> None:
        """When more than max_size candles are added, the oldest are dropped."""
        # Generate candles with strictly increasing timestamps so we can verify order
        num_candles = data.draw(st.integers(min_value=max_size + 1, max_value=max_size + 30))
        base_ts = 1_700_000_000_000
        candles = []
        for i in range(num_candles):
            c = data.draw(_candle_strategy(ts=st.just(base_ts + i)))
            candles.append(c)

        buf = CandleBuffer(max_size=max_size)
        for c in candles:
            await buf.add("ETHUSDT", "15m", c)

        df = await buf.get_df("ETHUSDT", "15m")
        assert len(df) == max_size

        # The buffer should contain the last max_size candles
        expected_timestamps = [c["timestamp"] for c in candles[-max_size:]]
        actual_timestamps = df["timestamp"].tolist()
        assert actual_timestamps == expected_timestamps


class TestCandleBufferDataFrameColumns:
    """Returned DataFrame always has the correct columns."""

    @settings(max_examples=100)
    @given(candles=st.lists(_candle, min_size=0, max_size=50))
    @pytest.mark.asyncio
    async def test_dataframe_has_correct_columns(self, candles: list[dict]) -> None:
        """DataFrame columns are always open, high, low, close, volume, timestamp."""
        buf = CandleBuffer(max_size=100)
        for c in candles:
            await buf.add("BTCUSDT", "3m", c)

        df = await buf.get_df("BTCUSDT", "3m")
        assert list(df.columns) == ["open", "high", "low", "close", "volume", "timestamp"]


class TestCandleBufferChronologicalOrder:
    """Candles in the DataFrame are in chronological (insertion) order."""

    @settings(max_examples=100)
    @given(
        max_size=st.integers(min_value=2, max_value=30),
        data=st.data(),
    )
    @pytest.mark.asyncio
    async def test_candles_in_chronological_order(
        self, max_size: int, data: st.DataObject
    ) -> None:
        """Candles are returned in the order they were inserted (FIFO)."""
        num_candles = data.draw(st.integers(min_value=1, max_value=max_size + 20))
        base_ts = 1_700_000_000_000
        candles = []
        for i in range(num_candles):
            c = data.draw(_candle_strategy(ts=st.just(base_ts + i * 1000)))
            candles.append(c)

        buf = CandleBuffer(max_size=max_size)
        for c in candles:
            await buf.add("XRPUSDT", "3m", c)

        df = await buf.get_df("XRPUSDT", "3m")
        if len(df) > 1:
            timestamps = df["timestamp"].tolist()
            # Timestamps should be strictly increasing (chronological)
            for j in range(1, len(timestamps)):
                assert timestamps[j] > timestamps[j - 1]
