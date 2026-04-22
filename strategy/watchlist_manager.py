"""Dynamic watchlist management for crypto-scalp-bot.

Filters and ranks Binance USDT-M Perpetual Futures symbols based on
24-hour price change, volume, and configurable filter rules. Maintains
a grace policy for symbols with open positions.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from loguru import logger

from core.config import WatchlistConfig
from core.models import TickerData


class PositionChecker(Protocol):
    """Protocol for checking whether a symbol has an open position.

    This avoids a direct import of PositionManager, keeping the
    dependency graph clean (strategy layer does not import execution layer).
    """

    def has_position(self, symbol: str) -> bool:
        """Return True if *symbol* has an open position."""
        ...


class WatchlistManager:
    """Dynamically selects and ranks the top-N symbols by 24h price change.

    Args:
        config: Watchlist configuration (top_n, filters, blacklist, etc.).
        position_checker: Object implementing ``has_position(symbol)`` to
            support the grace policy.
    """

    def __init__(
        self,
        config: WatchlistConfig,
        position_checker: PositionChecker | None = None,
    ) -> None:
        self._config = config
        self._position_checker = position_checker
        self._tickers: list[TickerData] = []
        self._active_symbols: list[str] = []

        # Callback invoked when the watchlist changes.
        # Signature: (added: list[str], removed: list[str]) -> Awaitable[None]
        self.on_watchlist_changed: Callable[
            [list[str], list[str]], Awaitable[None]
        ] | None = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def update_tickers(self, tickers: list[TickerData]) -> None:
        """Store the latest ticker snapshot.

        Args:
            tickers: Full list of ticker data received from the
                ``!ticker@arr`` WebSocket stream.
        """
        self._tickers = list(tickers)

    async def refresh(self) -> None:
        """Re-rank symbols, apply filters, and emit watchlist changes.

        Filtering pipeline (all conditions must be satisfied):
        1. Symbol name ends with ``USDT``.
        2. Symbol is not in the configured blacklist.
        3. Symbol name does not contain any blacklist pattern.
        4. 24h price change % ≥ ``min_change_pct_24h``.
        5. 24h quote volume ≥ ``min_volume_usdt_24h``.
        6. Last price > 0.0001.

        Qualifying symbols are sorted by ``price_change_pct`` descending
        and the top ``top_n`` are selected.  Symbols with open positions
        are retained regardless of ranking (grace policy).
        """
        qualifying = self._filter_symbols(self._tickers)

        # Deduplicate by symbol, keeping the entry with the highest
        # price_change_pct.  The ticker stream should not send duplicates,
        # but we handle it defensively.
        best_by_symbol: dict[str, TickerData] = {}
        for t in qualifying:
            prev = best_by_symbol.get(t.symbol)
            if prev is None or t.price_change_pct > prev.price_change_pct:
                best_by_symbol[t.symbol] = t
        qualifying = list(best_by_symbol.values())

        # Sort by 24h price change descending and take top_n.
        qualifying.sort(key=lambda t: t.price_change_pct, reverse=True)
        top_symbols = [t.symbol for t in qualifying[: self._config.top_n]]

        # Grace policy: retain symbols with open positions.
        grace_symbols = self._get_grace_symbols(top_symbols)

        # Merge top symbols with grace symbols, preserving order.
        new_symbols = list(top_symbols)
        for sym in grace_symbols:
            if sym not in new_symbols:
                new_symbols.append(sym)

        old_set = set(self._active_symbols)
        new_set = set(new_symbols)

        added = [s for s in new_symbols if s not in old_set]
        # Grace symbols should not appear in the removed list.
        grace_set = set(grace_symbols)
        removed = [s for s in self._active_symbols if s not in new_set and s not in grace_set]

        self._active_symbols = new_symbols

        if added or removed:
            logger.info(
                "watchlist | Watchlist updated: added={added}, removed={removed}",
                added=added,
                removed=removed,
            )
            if self.on_watchlist_changed is not None:
                await self.on_watchlist_changed(added, removed)

    def get_active_symbols(self) -> list[str]:
        """Return the current active watchlist.

        Returns:
            List of symbol strings currently being watched.
        """
        return list(self._active_symbols)

    def has_open_position(self, symbol: str) -> bool:
        """Check whether *symbol* has an open position via the position checker.

        Args:
            symbol: The trading pair symbol to check.

        Returns:
            ``True`` if the symbol has an open position, ``False`` otherwise
            or if no position checker is configured.
        """
        if self._position_checker is None:
            return False
        return self._position_checker.has_position(symbol)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _filter_symbols(self, tickers: list[TickerData]) -> list[TickerData]:
        """Apply all filter criteria to the ticker list.

        Args:
            tickers: Raw ticker data to filter.

        Returns:
            List of ``TickerData`` that satisfy every filter condition.
        """
        result: list[TickerData] = []
        for t in tickers:
            if not self._passes_filters(t):
                continue
            result.append(t)
        return result

    def _passes_filters(self, t: TickerData) -> bool:
        """Return ``True`` if a single ticker passes all filter rules."""
        # 1. USDT suffix
        if not t.symbol.endswith("USDT"):
            return False

        # 2. Not in blacklist
        if t.symbol in self._config.blacklist:
            return False

        # 3. Does not contain any blacklist pattern
        for pattern in self._config.blacklist_patterns:
            if pattern in t.symbol:
                return False

        # 4. Minimum 24h price change
        if t.price_change_pct < self._config.min_change_pct_24h:
            return False

        # 5. Minimum 24h quote volume
        if t.quote_volume < self._config.min_volume_usdt_24h:
            return False

        # 6. Minimum price
        if t.last_price <= 0.0001:
            return False

        return True

    def _get_grace_symbols(self, top_symbols: list[str]) -> list[str]:
        """Return symbols with open positions that are not in *top_symbols*.

        These symbols must be retained in the watchlist regardless of
        their ranking.

        Args:
            top_symbols: Symbols already selected by ranking.

        Returns:
            List of symbols retained by grace policy.
        """
        if self._position_checker is None:
            return []

        grace: list[str] = []
        # Check all symbols from the previous watchlist that didn't make
        # the new top-N cut.
        for sym in self._active_symbols:
            if sym not in top_symbols and self._position_checker.has_position(sym):
                grace.append(sym)
        return grace
