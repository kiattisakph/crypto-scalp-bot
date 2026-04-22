"""Historical data fetcher for backtest Filtered Replay.

Two-phase data collection:
  Phase 1 — Fetch 15m klines for ALL USDT symbols → compute 24h change
            → determine which symbols ever qualified for the watchlist.
  Phase 2 — Fetch 3m klines ONLY for qualifying symbols.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

import pandas as pd
from binance import AsyncClient
from loguru import logger


# Binance kline endpoint returns max 1000 candles per request.
_KLINE_LIMIT = 1000

_DF_COLUMNS = ["open", "high", "low", "close", "volume", "timestamp"]


class DataFetcher:
    """Fetches historical kline data from Binance USDT-M Perpetual Futures.

    Args:
        demo: Use testnet when True.
    """

    def __init__(self, demo: bool = False) -> None:
        self._demo = demo
        self._client: AsyncClient | None = None

    async def connect(self, api_key: str = "", api_secret: str = "") -> None:
        """Create AsyncClient connection."""
        self._client = await AsyncClient.create(
            api_key=api_key,
            api_secret=api_secret,
            demo=self._demo,
        )
        mode = "testnet" if self._demo else "mainnet"
        logger.info("backtest.fetcher | Connected to Binance {mode}", mode=mode)

    async def close(self) -> None:
        """Close the connection."""
        if self._client:
            await self._client.close_connection()
            self._client = None

    # ------------------------------------------------------------------
    # Phase 1 — Fetch 15m klines for all USDT symbols
    # ------------------------------------------------------------------

    async def fetch_all_15m(
        self,
        start: datetime,
        end: datetime,
    ) -> dict[str, list[dict]]:
        """Fetch 15m klines for every USDT perpetual symbol.

        Returns:
            Dict mapping symbol → list of candle dicts.
        """
        symbols = await self._get_usdt_symbols()
        logger.info(
            "backtest.fetcher | Phase 1: fetching 15m klines for {n} symbols "
            "({start} → {end})",
            n=len(symbols),
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
        )

        result: dict[str, list[dict]] = {}
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)

        for i, symbol in enumerate(symbols):
            candles = await self._fetch_kline_range(
                symbol, "15m", start_ms, end_ms,
            )
            if candles:
                result[symbol] = candles

            if (i + 1) % 50 == 0:
                logger.info(
                    "backtest.fetcher | Phase 1 progress: {done}/{total} symbols",
                    done=i + 1,
                    total=len(symbols),
                )
            # Respect rate limit
            await asyncio.sleep(0.12)

        logger.info(
            "backtest.fetcher | Phase 1 complete: {n} symbols with data",
            n=len(result),
        )
        return result

    # ------------------------------------------------------------------
    # Phase 2 — Fetch 3m klines for qualifying symbols
    # ------------------------------------------------------------------

    async def fetch_3m_for_symbols(
        self,
        symbols: list[str],
        start: datetime,
        end: datetime,
    ) -> dict[str, list[dict]]:
        """Fetch 3m klines for a specific set of symbols.

        Returns:
            Dict mapping symbol → list of candle dicts.
        """
        logger.info(
            "backtest.fetcher | Phase 2: fetching 3m klines for {n} symbols "
            "({start} → {end})",
            n=len(symbols),
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
        )

        result: dict[str, list[dict]] = {}
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)

        for i, symbol in enumerate(symbols):
            candles = await self._fetch_kline_range(
                symbol, "3m", start_ms, end_ms,
            )
            if candles:
                result[symbol] = candles

            if (i + 1) % 20 == 0:
                logger.info(
                    "backtest.fetcher | Phase 2 progress: {done}/{total}",
                    done=i + 1,
                    total=len(symbols),
                )
            await asyncio.sleep(0.12)

        logger.info(
            "backtest.fetcher | Phase 2 complete: {n} symbols with data",
            n=len(result),
        )
        return result

    # ------------------------------------------------------------------
    # Utility — convert raw kline dicts
    # ------------------------------------------------------------------

    @staticmethod
    def candles_to_df(candles: list[dict]) -> pd.DataFrame:
        """Convert list of candle dicts to a DataFrame."""
        if not candles:
            return pd.DataFrame(columns=_DF_COLUMNS)
        return pd.DataFrame(candles, columns=_DF_COLUMNS)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_usdt_symbols(self) -> list[str]:
        """Return all USDT-margined perpetual symbols, sorted."""
        assert self._client is not None
        info = await self._client.futures_exchange_info()
        symbols = [
            s["symbol"]
            for s in info["symbols"]
            if s["quoteAsset"] == "USDT"
            and s["status"] == "TRADING"
            and "USDT" in s["symbol"]
            # Exclude UP/DOWN/leveraged tokens
            and "UP" not in s["symbol"]
            and "DOWN" not in s["symbol"]
            and s["symbol"] not in ("USDCUSDT", "BUSDUSDT", "BTCDOMUSDT")
        ]
        symbols.sort()
        logger.info(
            "backtest.fetcher | Found {n} USDT perpetual symbols",
            n=len(symbols),
        )
        return symbols

    async def _fetch_kline_range(
        self,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
    ) -> list[dict]:
        """Fetch all klines for a symbol between start_ms and end_ms.

        Handles pagination (1000 candles per request).
        """
        all_klines: list[list] = []
        current_start = start_ms

        while current_start < end_ms:
            assert self._client is not None
            klines = await self._client.futures_klines(
                symbol=symbol,
                interval=interval,
                startTime=current_start,
                endTime=end_ms,
                limit=_KLINE_LIMIT,
            )
            if not klines:
                break
            all_klines.extend(klines)

            # Advance past the last candle we received
            last_open_time = klines[-1][0]
            if last_open_time <= current_start:
                break  # safety: avoid infinite loop
            current_start = last_open_time + 1

            # Respect rate limit between pages
            await asyncio.sleep(0.05)

        return [self._kline_to_dict(k, interval) for k in all_klines]

    @staticmethod
    def _kline_to_dict(k: list, interval: str) -> dict:
        """Convert a raw Binance kline list to a candle dict.

        Binance kline format:
        [0] open_time, [1] open, [2] high, [3] low, [4] close,
        [5] volume, [6] close_time, [7] quote_volume, ...
        """
        return {
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
            "timestamp": datetime.utcfromtimestamp(k[0] / 1000),
        }
