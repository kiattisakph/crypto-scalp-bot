"""Unit tests for CandleBuffer rolling buffer operations."""
from __future__ import annotations

import asyncio

import pandas as pd
import pytest

from utils.candle_buffer import CandleBuffer


def _candle(ts: int, close: float = 100.0) -> dict:
    """Build a candle dict with a given timestamp and close price."""
    return {
        "open": close - 1.0,
        "high": close + 1.0,
        "low": close - 2.0,
        "close": close,
        "volume": 5000.0,
        "timestamp": ts,
    }


# ---------------------------------------------------------------------------
# add / get_df
# ---------------------------------------------------------------------------


class TestAddAndGetDf:
    """Tests for CandleBuffer.add and CandleBuffer.get_df."""

    @pytest.mark.asyncio
    async def test_single_candle_returned(self) -> None:
        buf = CandleBuffer(max_size=10)
        await buf.add("SOLUSDT", "3m", _candle(1000, 150.0))

        df = await buf.get_df("SOLUSDT", "3m")
        assert len(df) == 1
        assert df.iloc[0]["close"] == 150.0
        assert df.iloc[0]["timestamp"] == 1000

    @pytest.mark.asyncio
    async def test_multiple_candles_in_order(self) -> None:
        buf = CandleBuffer(max_size=10)
        for i in range(5):
            await buf.add("SOLUSDT", "3m", _candle(1000 + i, 100.0 + i))

        df = await buf.get_df("SOLUSDT", "3m")
        assert len(df) == 5
        assert df.iloc[0]["timestamp"] == 1000
        assert df.iloc[4]["timestamp"] == 1004
        assert df.iloc[4]["close"] == 104.0

    @pytest.mark.asyncio
    async def test_different_symbols_are_independent(self) -> None:
        buf = CandleBuffer(max_size=10)
        await buf.add("SOLUSDT", "3m", _candle(1000))
        await buf.add("ETHUSDT", "3m", _candle(2000))

        df_sol = await buf.get_df("SOLUSDT", "3m")
        df_eth = await buf.get_df("ETHUSDT", "3m")
        assert len(df_sol) == 1
        assert len(df_eth) == 1
        assert df_sol.iloc[0]["timestamp"] == 1000
        assert df_eth.iloc[0]["timestamp"] == 2000

    @pytest.mark.asyncio
    async def test_different_timeframes_are_independent(self) -> None:
        buf = CandleBuffer(max_size=10)
        await buf.add("SOLUSDT", "3m", _candle(1000))
        await buf.add("SOLUSDT", "15m", _candle(2000))

        df_3m = await buf.get_df("SOLUSDT", "3m")
        df_15m = await buf.get_df("SOLUSDT", "15m")
        assert len(df_3m) == 1
        assert len(df_15m) == 1


# ---------------------------------------------------------------------------
# Capacity eviction
# ---------------------------------------------------------------------------


class TestCapacityEviction:
    """Tests for FIFO eviction when buffer is at capacity."""

    @pytest.mark.asyncio
    async def test_evicts_oldest_at_capacity(self) -> None:
        buf = CandleBuffer(max_size=3)
        for i in range(5):
            await buf.add("SOLUSDT", "3m", _candle(1000 + i))

        df = await buf.get_df("SOLUSDT", "3m")
        assert len(df) == 3
        # Should contain the last 3 candles (timestamps 1002, 1003, 1004)
        assert df.iloc[0]["timestamp"] == 1002
        assert df.iloc[2]["timestamp"] == 1004

    @pytest.mark.asyncio
    async def test_max_size_one(self) -> None:
        buf = CandleBuffer(max_size=1)
        await buf.add("SOLUSDT", "3m", _candle(1000, 100.0))
        await buf.add("SOLUSDT", "3m", _candle(2000, 200.0))

        df = await buf.get_df("SOLUSDT", "3m")
        assert len(df) == 1
        assert df.iloc[0]["close"] == 200.0


# ---------------------------------------------------------------------------
# has_enough_data
# ---------------------------------------------------------------------------


class TestHasEnoughData:
    """Tests for CandleBuffer.has_enough_data."""

    @pytest.mark.asyncio
    async def test_empty_buffer_not_enough(self) -> None:
        buf = CandleBuffer(max_size=10)
        assert await buf.has_enough_data("SOLUSDT", "3m", 1) is False

    @pytest.mark.asyncio
    async def test_exact_count_is_enough(self) -> None:
        buf = CandleBuffer(max_size=10)
        for i in range(5):
            await buf.add("SOLUSDT", "3m", _candle(1000 + i))
        assert await buf.has_enough_data("SOLUSDT", "3m", 5) is True

    @pytest.mark.asyncio
    async def test_more_than_needed_is_enough(self) -> None:
        buf = CandleBuffer(max_size=10)
        for i in range(5):
            await buf.add("SOLUSDT", "3m", _candle(1000 + i))
        assert await buf.has_enough_data("SOLUSDT", "3m", 3) is True

    @pytest.mark.asyncio
    async def test_fewer_than_needed_not_enough(self) -> None:
        buf = CandleBuffer(max_size=10)
        for i in range(2):
            await buf.add("SOLUSDT", "3m", _candle(1000 + i))
        assert await buf.has_enough_data("SOLUSDT", "3m", 5) is False

    @pytest.mark.asyncio
    async def test_zero_min_candles_always_enough(self) -> None:
        buf = CandleBuffer(max_size=10)
        assert await buf.has_enough_data("SOLUSDT", "3m", 0) is True


# ---------------------------------------------------------------------------
# clear
# ---------------------------------------------------------------------------


class TestClear:
    """Tests for CandleBuffer.clear."""

    @pytest.mark.asyncio
    async def test_clear_removes_all_timeframes(self) -> None:
        buf = CandleBuffer(max_size=10)
        await buf.add("SOLUSDT", "3m", _candle(1000))
        await buf.add("SOLUSDT", "15m", _candle(2000))

        await buf.clear("SOLUSDT")

        df_3m = await buf.get_df("SOLUSDT", "3m")
        df_15m = await buf.get_df("SOLUSDT", "15m")
        assert len(df_3m) == 0
        assert len(df_15m) == 0

    @pytest.mark.asyncio
    async def test_clear_does_not_affect_other_symbols(self) -> None:
        buf = CandleBuffer(max_size=10)
        await buf.add("SOLUSDT", "3m", _candle(1000))
        await buf.add("ETHUSDT", "3m", _candle(2000))

        await buf.clear("SOLUSDT")

        df_sol = await buf.get_df("SOLUSDT", "3m")
        df_eth = await buf.get_df("ETHUSDT", "3m")
        assert len(df_sol) == 0
        assert len(df_eth) == 1

    @pytest.mark.asyncio
    async def test_clear_nonexistent_symbol_no_error(self) -> None:
        buf = CandleBuffer(max_size=10)
        await buf.clear("DOESNOTEXIST")  # should not raise


# ---------------------------------------------------------------------------
# Timestamp deduplication
# ---------------------------------------------------------------------------


class TestTimestampDedup:
    """Tests for duplicate candle detection by timestamp."""

    @pytest.mark.asyncio
    async def test_exact_duplicate_replaced_not_appended(self) -> None:
        buf = CandleBuffer(max_size=10)
        await buf.add("SOLUSDT", "3m", _candle(1000, 150.0))
        await buf.add("SOLUSDT", "3m", _candle(1000, 150.0))

        df = await buf.get_df("SOLUSDT", "3m")
        assert len(df) == 1
        assert df.iloc[0]["timestamp"] == 1000

    @pytest.mark.asyncio
    async def test_duplicate_timestamp_updates_data(self) -> None:
        buf = CandleBuffer(max_size=10)
        await buf.add("SOLUSDT", "3m", _candle(1000, 150.0))
        await buf.add("SOLUSDT", "3m", _candle(1000, 155.0))

        df = await buf.get_df("SOLUSDT", "3m")
        assert len(df) == 1
        assert df.iloc[0]["close"] == pytest.approx(155.0)

    @pytest.mark.asyncio
    async def test_different_timestamps_both_kept(self) -> None:
        buf = CandleBuffer(max_size=10)
        await buf.add("SOLUSDT", "3m", _candle(1000, 150.0))
        await buf.add("SOLUSDT", "3m", _candle(2000, 160.0))

        df = await buf.get_df("SOLUSDT", "3m")
        assert len(df) == 2

    @pytest.mark.asyncio
    async def test_duplicate_does_not_affect_buffer_size(self) -> None:
        buf = CandleBuffer(max_size=3)
        await buf.add("SOLUSDT", "3m", _candle(1000))
        await buf.add("SOLUSDT", "3m", _candle(2000))
        await buf.add("SOLUSDT", "3m", _candle(3000))
        # Duplicate of last — should replace, not evict oldest
        await buf.add("SOLUSDT", "3m", _candle(3000, 999.0))

        df = await buf.get_df("SOLUSDT", "3m")
        assert len(df) == 3
        assert df.iloc[0]["timestamp"] == 1000  # oldest preserved
        assert df.iloc[2]["close"] == pytest.approx(999.0)  # last updated

    @pytest.mark.asyncio
    async def test_dedup_is_per_symbol_timeframe(self) -> None:
        buf = CandleBuffer(max_size=10)
        await buf.add("SOLUSDT", "3m", _candle(1000, 100.0))
        await buf.add("SOLUSDT", "15m", _candle(1000, 200.0))
        await buf.add("ETHUSDT", "3m", _candle(1000, 300.0))

        df_sol_3m = await buf.get_df("SOLUSDT", "3m")
        df_sol_15m = await buf.get_df("SOLUSDT", "15m")
        df_eth_3m = await buf.get_df("ETHUSDT", "3m")
        assert len(df_sol_3m) == 1
        assert len(df_sol_15m) == 1
        assert len(df_eth_3m) == 1

    @pytest.mark.asyncio
    async def test_only_last_candle_checked_for_dedup(self) -> None:
        """Older candles with the same timestamp are not deduplicated.

        Only the most recent entry is checked — this is O(1) and covers
        the real-world reconnect scenario where the latest candle is
        re-delivered.
        """
        buf = CandleBuffer(max_size=10)
        await buf.add("SOLUSDT", "3m", _candle(1000, 100.0))
        await buf.add("SOLUSDT", "3m", _candle(2000, 200.0))
        await buf.add("SOLUSDT", "3m", _candle(3000, 300.0))
        # Re-send ts=1000 — this is NOT the last candle, so it appends
        await buf.add("SOLUSDT", "3m", _candle(1000, 100.0))

        df = await buf.get_df("SOLUSDT", "3m")
        assert len(df) == 4

    @pytest.mark.asyncio
    async def test_triple_duplicate_still_single_entry(self) -> None:
        buf = CandleBuffer(max_size=10)
        await buf.add("SOLUSDT", "3m", _candle(1000, 100.0))
        await buf.add("SOLUSDT", "3m", _candle(1000, 101.0))
        await buf.add("SOLUSDT", "3m", _candle(1000, 102.0))

        df = await buf.get_df("SOLUSDT", "3m")
        assert len(df) == 1
        assert df.iloc[0]["close"] == pytest.approx(102.0)


# ---------------------------------------------------------------------------
# Empty buffer
# ---------------------------------------------------------------------------


class TestEmptyBuffer:
    """Tests for empty buffer edge cases."""

    @pytest.mark.asyncio
    async def test_empty_buffer_returns_empty_df_with_correct_columns(self) -> None:
        buf = CandleBuffer(max_size=10)
        df = await buf.get_df("SOLUSDT", "3m")
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 0
        assert list(df.columns) == ["open", "high", "low", "close", "volume", "timestamp"]


# ---------------------------------------------------------------------------
# Backfill (REST resync after reconnect)
# ---------------------------------------------------------------------------


class TestBackfill:
    """Tests for CandleBuffer.backfill used after WebSocket reconnect."""

    @pytest.mark.asyncio
    async def test_backfill_replaces_existing_data(self) -> None:
        buf = CandleBuffer(max_size=10)
        # Pre-populate with stale data
        await buf.add("SOLUSDT", "3m", _candle(1000, 100.0))
        await buf.add("SOLUSDT", "3m", _candle(2000, 200.0))

        # Backfill with fresh REST data
        rest_candles = [_candle(3000, 300.0), _candle(4000, 400.0), _candle(5000, 500.0)]
        await buf.backfill("SOLUSDT", "3m", rest_candles)

        df = await buf.get_df("SOLUSDT", "3m")
        assert len(df) == 3
        assert df.iloc[0]["timestamp"] == 3000
        assert df.iloc[2]["close"] == pytest.approx(500.0)

    @pytest.mark.asyncio
    async def test_backfill_respects_max_size(self) -> None:
        buf = CandleBuffer(max_size=3)
        candles = [_candle(ts, float(ts)) for ts in range(1000, 1006)]
        await buf.backfill("SOLUSDT", "3m", candles)

        df = await buf.get_df("SOLUSDT", "3m")
        assert len(df) == 3
        # Only the last 3 should survive (maxlen eviction)
        assert df.iloc[0]["timestamp"] == 1003
        assert df.iloc[2]["timestamp"] == 1005

    @pytest.mark.asyncio
    async def test_backfill_empty_list_clears_buffer(self) -> None:
        buf = CandleBuffer(max_size=10)
        await buf.add("SOLUSDT", "3m", _candle(1000))
        await buf.backfill("SOLUSDT", "3m", [])

        df = await buf.get_df("SOLUSDT", "3m")
        assert len(df) == 0

    @pytest.mark.asyncio
    async def test_backfill_does_not_affect_other_symbols(self) -> None:
        buf = CandleBuffer(max_size=10)
        await buf.add("SOLUSDT", "3m", _candle(1000, 100.0))
        await buf.add("ETHUSDT", "3m", _candle(2000, 200.0))

        await buf.backfill("SOLUSDT", "3m", [_candle(5000, 500.0)])

        df_sol = await buf.get_df("SOLUSDT", "3m")
        df_eth = await buf.get_df("ETHUSDT", "3m")
        assert len(df_sol) == 1
        assert df_sol.iloc[0]["timestamp"] == 5000
        assert len(df_eth) == 1
        assert df_eth.iloc[0]["timestamp"] == 2000

    @pytest.mark.asyncio
    async def test_backfill_does_not_affect_other_timeframes(self) -> None:
        buf = CandleBuffer(max_size=10)
        await buf.add("SOLUSDT", "3m", _candle(1000, 100.0))
        await buf.add("SOLUSDT", "15m", _candle(2000, 200.0))

        await buf.backfill("SOLUSDT", "3m", [_candle(5000, 500.0)])

        df_3m = await buf.get_df("SOLUSDT", "3m")
        df_15m = await buf.get_df("SOLUSDT", "15m")
        assert len(df_3m) == 1
        assert df_3m.iloc[0]["timestamp"] == 5000
        assert len(df_15m) == 1
        assert df_15m.iloc[0]["timestamp"] == 2000

    @pytest.mark.asyncio
    async def test_add_after_backfill_appends_normally(self) -> None:
        buf = CandleBuffer(max_size=10)
        await buf.backfill("SOLUSDT", "3m", [_candle(1000), _candle(2000)])
        await buf.add("SOLUSDT", "3m", _candle(3000, 300.0))

        df = await buf.get_df("SOLUSDT", "3m")
        assert len(df) == 3
        assert df.iloc[2]["timestamp"] == 3000

    @pytest.mark.asyncio
    async def test_has_enough_data_after_backfill(self) -> None:
        buf = CandleBuffer(max_size=100)
        candles = [_candle(ts) for ts in range(1000, 1050)]
        await buf.backfill("SOLUSDT", "3m", candles)

        assert await buf.has_enough_data("SOLUSDT", "3m", 50) is True
        assert await buf.has_enough_data("SOLUSDT", "3m", 51) is False
