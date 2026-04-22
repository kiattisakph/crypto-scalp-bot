"""Rolling candle buffer per symbol per timeframe.

Stores the most recent N closed candles for each (symbol, timeframe) pair
and exposes them as pandas DataFrames for indicator calculation.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict, deque

import pandas as pd
from loguru import logger

# Column order returned by get_df
_DF_COLUMNS = ["open", "high", "low", "close", "volume", "timestamp"]


class CandleBuffer:
    """Thread-safe rolling buffer for OHLCV candle data.

    Args:
        max_size: Maximum number of candles stored per (symbol, timeframe) pair.
    """

    def __init__(self, max_size: int) -> None:
        self._max_size = max_size
        self._buffers: dict[str, deque[dict]] = defaultdict(
            lambda: deque(maxlen=max_size)
        )
        self._lock = asyncio.Lock()

    @property
    def max_size(self) -> int:
        """Return the configured maximum buffer size."""
        return self._max_size

    @staticmethod
    def _key(symbol: str, timeframe: str) -> str:
        """Build a composite key for the internal buffer dict."""
        return f"{symbol}:{timeframe}"

    async def add(self, symbol: str, timeframe: str, candle: dict) -> None:
        """Append a closed candle to the buffer, deduplicating by timestamp.

        If the most recent candle in the buffer shares the same timestamp
        as the incoming candle, the existing entry is replaced in-place
        rather than appending a duplicate.  This guards against duplicate
        WebSocket messages that can occur during reconnection.

        If the buffer for this (symbol, timeframe) pair is at capacity the
        oldest candle is automatically evicted (FIFO).

        Args:
            symbol: Trading pair symbol (e.g. "SOLUSDT").
            timeframe: Candle timeframe (e.g. "3m", "15m").
            candle: Dict with keys: open, high, low, close, volume, timestamp.
        """
        async with self._lock:
            key = self._key(symbol, timeframe)
            buf = self._buffers[key]
            ts = candle.get("timestamp")

            if buf and ts is not None and buf[-1].get("timestamp") == ts:
                buf[-1] = candle
                logger.debug(
                    "candle_buffer | Replaced duplicate candle for "
                    "{symbol}:{tf} ts={ts}",
                    symbol=symbol,
                    tf=timeframe,
                    ts=ts,
                )
                return

            buf.append(candle)
            logger.debug(
                "candle_buffer | Added candle for {symbol}:{tf} "
                "(buffer size: {size}/{max})",
                symbol=symbol,
                tf=timeframe,
                size=len(buf),
                max=self._max_size,
            )

    async def get_df(self, symbol: str, timeframe: str) -> pd.DataFrame:
        """Return buffered candles as a pandas DataFrame.

        The DataFrame has columns: open, high, low, close, volume, timestamp
        in chronological order (oldest first).

        Args:
            symbol: Trading pair symbol.
            timeframe: Candle timeframe.

        Returns:
            A DataFrame with the buffered candles, or an empty DataFrame
            with the correct columns if no data is available.
        """
        async with self._lock:
            key = self._key(symbol, timeframe)
            buf = self._buffers.get(key)
            if not buf:
                return pd.DataFrame(columns=_DF_COLUMNS)
            return pd.DataFrame(list(buf), columns=_DF_COLUMNS)

    async def has_enough_data(
        self, symbol: str, timeframe: str, min_candles: int
    ) -> bool:
        """Check whether the buffer has at least *min_candles* entries.

        Args:
            symbol: Trading pair symbol.
            timeframe: Candle timeframe.
            min_candles: Minimum number of candles required.

        Returns:
            True if the buffer contains at least *min_candles* candles.
        """
        async with self._lock:
            key = self._key(symbol, timeframe)
            return len(self._buffers.get(key, deque())) >= min_candles

    async def clear(self, symbol: str) -> None:
        """Remove all buffered data for a symbol across all timeframes.

        Args:
            symbol: Trading pair symbol whose data should be cleared.
        """
        async with self._lock:
            keys_to_remove = [
                k for k in self._buffers if k.startswith(f"{symbol}:")
            ]
            for k in keys_to_remove:
                del self._buffers[k]
            if keys_to_remove:
                logger.debug(
                    "candle_buffer | Cleared {n} buffer(s) for {symbol}",
                    n=len(keys_to_remove),
                    symbol=symbol,
                )

    async def backfill(
        self, symbol: str, timeframe: str, candles: list[dict]
    ) -> None:
        """Replace the buffer for a (symbol, timeframe) pair with REST data.

        Used after a WebSocket reconnect to resync the buffer with
        authoritative historical klines fetched via the REST API.
        The incoming candles must be in chronological order (oldest first).
        Only the last ``max_size`` candles are kept.

        Args:
            symbol: Trading pair symbol (e.g. "SOLUSDT").
            timeframe: Candle timeframe (e.g. "3m", "15m").
            candles: List of candle dicts with keys: open, high, low,
                close, volume, timestamp.  Oldest first.
        """
        async with self._lock:
            key = self._key(symbol, timeframe)
            buf: deque[dict] = deque(maxlen=self._max_size)
            for candle in candles:
                buf.append(candle)
            self._buffers[key] = buf
            logger.info(
                "candle_buffer | Backfilled {symbol}:{tf} with {n} candle(s)",
                symbol=symbol,
                tf=timeframe,
                n=len(buf),
            )
