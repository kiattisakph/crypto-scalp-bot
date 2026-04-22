"""Unit tests for WatchlistManager.

Tests filter rules, blacklist/pattern exclusion, top-N sorting,
grace policy, and watchlist change event emission.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from core.config import WatchlistConfig
from core.models import TickerData
from strategy.watchlist_manager import WatchlistManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@dataclass
class FakePositionChecker:
    """Stub position checker for unit tests."""

    open_symbols: set[str]

    def has_position(self, symbol: str) -> bool:
        return symbol in self.open_symbols


def _make_ticker(
    symbol: str = "SOLUSDT",
    pct: float = 10.0,
    price: float = 100.0,
    volume: float = 50_000_000.0,
) -> TickerData:
    return TickerData(
        symbol=symbol,
        price_change_pct=pct,
        last_price=price,
        quote_volume=volume,
    )


def _default_config(**overrides) -> WatchlistConfig:
    defaults = dict(
        top_n=3,
        min_change_pct_24h=3.0,
        min_volume_usdt_24h=10_000_000,
        blacklist=[],
        blacklist_patterns=[],
    )
    defaults.update(overrides)
    # Use a tiny positive value instead of 0.0 to satisfy validators
    # while still effectively disabling the filter for isolation tests.
    if defaults["min_change_pct_24h"] == 0.0:
        defaults["min_change_pct_24h"] = 1e-9
    if defaults["min_volume_usdt_24h"] == 0.0:
        defaults["min_volume_usdt_24h"] = 1e-9
    return WatchlistConfig(**defaults)


# ---------------------------------------------------------------------------
# Filter rule tests
# ---------------------------------------------------------------------------

class TestFilterRules:
    """Tests for individual filter criteria."""

    def test_usdt_suffix_required(self) -> None:
        """Symbols not ending with USDT are excluded."""
        config = _default_config(min_change_pct_24h=0.0, min_volume_usdt_24h=0.0)
        wm = WatchlistManager(config=config)
        tickers = [
            _make_ticker(symbol="SOLBTC", pct=20.0),
            _make_ticker(symbol="SOLUSDT", pct=5.0),
        ]
        filtered = wm._filter_symbols(tickers)
        assert len(filtered) == 1
        assert filtered[0].symbol == "SOLUSDT"

    def test_blacklist_exclusion(self) -> None:
        """Symbols in the blacklist are excluded."""
        config = _default_config(
            blacklist=["LUNAUSDT"],
            min_change_pct_24h=0.0,
            min_volume_usdt_24h=0.0,
        )
        wm = WatchlistManager(config=config)
        tickers = [
            _make_ticker(symbol="LUNAUSDT", pct=50.0),
            _make_ticker(symbol="SOLUSDT", pct=10.0),
        ]
        filtered = wm._filter_symbols(tickers)
        assert len(filtered) == 1
        assert filtered[0].symbol == "SOLUSDT"

    def test_blacklist_pattern_exclusion(self) -> None:
        """Symbols containing a blacklist pattern are excluded."""
        config = _default_config(
            blacklist_patterns=["UP", "DOWN"],
            min_change_pct_24h=0.0,
            min_volume_usdt_24h=0.0,
        )
        wm = WatchlistManager(config=config)
        tickers = [
            _make_ticker(symbol="BTCUPUSDT", pct=20.0),
            _make_ticker(symbol="ETHDOWNUSDT", pct=15.0),
            _make_ticker(symbol="SOLUSDT", pct=10.0),
        ]
        filtered = wm._filter_symbols(tickers)
        assert len(filtered) == 1
        assert filtered[0].symbol == "SOLUSDT"

    def test_min_change_pct_filter(self) -> None:
        """Symbols below min_change_pct_24h are excluded."""
        config = _default_config(min_change_pct_24h=5.0, min_volume_usdt_24h=0.0)
        wm = WatchlistManager(config=config)
        tickers = [
            _make_ticker(symbol="SOLUSDT", pct=4.9),
            _make_ticker(symbol="ETHUSDT", pct=5.0),
            _make_ticker(symbol="BTCUSDT", pct=10.0),
        ]
        filtered = wm._filter_symbols(tickers)
        symbols = {t.symbol for t in filtered}
        assert "SOLUSDT" not in symbols
        assert "ETHUSDT" in symbols
        assert "BTCUSDT" in symbols

    def test_min_volume_filter(self) -> None:
        """Symbols below min_volume_usdt_24h are excluded."""
        config = _default_config(min_change_pct_24h=0.0, min_volume_usdt_24h=10_000_000)
        wm = WatchlistManager(config=config)
        tickers = [
            _make_ticker(symbol="SOLUSDT", volume=9_999_999),
            _make_ticker(symbol="ETHUSDT", volume=10_000_000),
        ]
        filtered = wm._filter_symbols(tickers)
        assert len(filtered) == 1
        assert filtered[0].symbol == "ETHUSDT"

    def test_min_price_filter(self) -> None:
        """Symbols with last_price <= 0.0001 are excluded."""
        config = _default_config(min_change_pct_24h=0.0, min_volume_usdt_24h=0.0)
        wm = WatchlistManager(config=config)
        tickers = [
            _make_ticker(symbol="DUSTUSDT", pct=50.0, price=0.0001),
            _make_ticker(symbol="SOLUSDT", pct=10.0, price=0.00011),
        ]
        filtered = wm._filter_symbols(tickers)
        assert len(filtered) == 1
        assert filtered[0].symbol == "SOLUSDT"

    def test_all_filters_combined(self) -> None:
        """Only symbols passing ALL filters are included."""
        config = _default_config(
            blacklist=["BADUSDT"],
            blacklist_patterns=["DOWN"],
            min_change_pct_24h=3.0,
            min_volume_usdt_24h=10_000_000,
        )
        wm = WatchlistManager(config=config)
        tickers = [
            _make_ticker(symbol="BADUSDT", pct=20.0),           # blacklisted
            _make_ticker(symbol="XDOWNUSDT", pct=20.0),         # pattern match
            _make_ticker(symbol="LOWUSDT", pct=2.0),            # below min change
            _make_ticker(symbol="THINUSDT", pct=10.0, volume=5_000_000),  # below min volume
            _make_ticker(symbol="DUSTUSDT", pct=10.0, price=0.00005),     # below min price
            _make_ticker(symbol="SOLUSDT", pct=10.0),           # passes all
        ]
        filtered = wm._filter_symbols(tickers)
        assert len(filtered) == 1
        assert filtered[0].symbol == "SOLUSDT"


# ---------------------------------------------------------------------------
# Top-N sorting tests
# ---------------------------------------------------------------------------

class TestTopNSorting:
    """Tests for top-N selection and sorting."""

    @pytest.mark.asyncio
    async def test_top_n_selects_highest_pct(self) -> None:
        """Top-N symbols are those with the highest price_change_pct."""
        config = _default_config(top_n=2, min_change_pct_24h=0.0, min_volume_usdt_24h=0.0)
        wm = WatchlistManager(config=config)
        wm.update_tickers([
            _make_ticker(symbol="AAUSDT", pct=5.0),
            _make_ticker(symbol="BBUSDT", pct=15.0),
            _make_ticker(symbol="CCUSDT", pct=10.0),
        ])
        await wm.refresh()
        active = wm.get_active_symbols()
        assert active == ["BBUSDT", "CCUSDT"]

    @pytest.mark.asyncio
    async def test_top_n_with_fewer_qualifying(self) -> None:
        """When fewer symbols qualify than top_n, all qualifying are returned."""
        config = _default_config(top_n=10, min_change_pct_24h=5.0, min_volume_usdt_24h=0.0)
        wm = WatchlistManager(config=config)
        wm.update_tickers([
            _make_ticker(symbol="AAUSDT", pct=6.0),
            _make_ticker(symbol="BBUSDT", pct=3.0),  # below threshold
        ])
        await wm.refresh()
        active = wm.get_active_symbols()
        assert active == ["AAUSDT"]

    @pytest.mark.asyncio
    async def test_sorted_descending_by_pct(self) -> None:
        """Active symbols are sorted by price_change_pct descending."""
        config = _default_config(top_n=5, min_change_pct_24h=0.0, min_volume_usdt_24h=0.0)
        wm = WatchlistManager(config=config)
        wm.update_tickers([
            _make_ticker(symbol="AAUSDT", pct=1.0),
            _make_ticker(symbol="BBUSDT", pct=3.0),
            _make_ticker(symbol="CCUSDT", pct=2.0),
        ])
        await wm.refresh()
        assert wm.get_active_symbols() == ["BBUSDT", "CCUSDT", "AAUSDT"]


# ---------------------------------------------------------------------------
# Grace policy tests
# ---------------------------------------------------------------------------

class TestGracePolicy:
    """Tests for grace policy retention of symbols with open positions."""

    @pytest.mark.asyncio
    async def test_symbol_with_position_retained(self) -> None:
        """A symbol with an open position stays in the watchlist even if it
        would otherwise be dropped by ranking."""
        checker = FakePositionChecker(open_symbols={"OLDUSDT"})
        config = _default_config(top_n=1, min_change_pct_24h=0.0, min_volume_usdt_24h=0.0)
        wm = WatchlistManager(config=config, position_checker=checker)

        # Seed the initial watchlist with OLDUSDT.
        wm._active_symbols = ["OLDUSDT"]

        # New tickers: NEWUSDT has higher pct, OLDUSDT has lower.
        wm.update_tickers([
            _make_ticker(symbol="NEWUSDT", pct=20.0),
            _make_ticker(symbol="OLDUSDT", pct=1.0),
        ])
        await wm.refresh()

        active = wm.get_active_symbols()
        assert "NEWUSDT" in active
        assert "OLDUSDT" in active  # retained by grace policy

    @pytest.mark.asyncio
    async def test_symbol_without_position_dropped(self) -> None:
        """A symbol without an open position is dropped when it falls out of top-N."""
        checker = FakePositionChecker(open_symbols=set())
        config = _default_config(top_n=1, min_change_pct_24h=0.0, min_volume_usdt_24h=0.0)
        wm = WatchlistManager(config=config, position_checker=checker)

        wm._active_symbols = ["OLDUSDT"]
        wm.update_tickers([
            _make_ticker(symbol="NEWUSDT", pct=20.0),
            _make_ticker(symbol="OLDUSDT", pct=1.0),
        ])
        await wm.refresh()

        active = wm.get_active_symbols()
        assert "NEWUSDT" in active
        assert "OLDUSDT" not in active

    def test_has_open_position_delegates(self) -> None:
        """has_open_position delegates to the position checker."""
        checker = FakePositionChecker(open_symbols={"SOLUSDT"})
        wm = WatchlistManager(config=_default_config(), position_checker=checker)
        assert wm.has_open_position("SOLUSDT") is True
        assert wm.has_open_position("ETHUSDT") is False

    def test_has_open_position_without_checker(self) -> None:
        """has_open_position returns False when no checker is configured."""
        wm = WatchlistManager(config=_default_config())
        assert wm.has_open_position("SOLUSDT") is False


# ---------------------------------------------------------------------------
# Watchlist change event tests
# ---------------------------------------------------------------------------

class TestWatchlistChangeEvent:
    """Tests for on_watchlist_changed callback emission."""

    @pytest.mark.asyncio
    async def test_callback_emitted_on_change(self) -> None:
        """on_watchlist_changed is called with correct added/removed lists."""
        recorded: list[tuple[list[str], list[str]]] = []

        async def on_changed(added: list[str], removed: list[str]) -> None:
            recorded.append((added, removed))

        config = _default_config(top_n=2, min_change_pct_24h=0.0, min_volume_usdt_24h=0.0)
        wm = WatchlistManager(config=config)
        wm.on_watchlist_changed = on_changed

        # First refresh: everything is added.
        wm.update_tickers([
            _make_ticker(symbol="AAUSDT", pct=10.0),
            _make_ticker(symbol="BBUSDT", pct=5.0),
        ])
        await wm.refresh()
        assert len(recorded) == 1
        assert set(recorded[0][0]) == {"AAUSDT", "BBUSDT"}
        assert recorded[0][1] == []

        # Second refresh: BBUSDT replaced by CCUSDT.
        recorded.clear()
        wm.update_tickers([
            _make_ticker(symbol="AAUSDT", pct=10.0),
            _make_ticker(symbol="CCUSDT", pct=8.0),
        ])
        await wm.refresh()
        assert len(recorded) == 1
        assert set(recorded[0][0]) == {"CCUSDT"}
        assert set(recorded[0][1]) == {"BBUSDT"}

    @pytest.mark.asyncio
    async def test_no_callback_when_unchanged(self) -> None:
        """on_watchlist_changed is NOT called when the watchlist is unchanged."""
        call_count = 0

        async def on_changed(added: list[str], removed: list[str]) -> None:
            nonlocal call_count
            call_count += 1

        config = _default_config(top_n=2, min_change_pct_24h=0.0, min_volume_usdt_24h=0.0)
        wm = WatchlistManager(config=config)
        wm.on_watchlist_changed = on_changed

        tickers = [
            _make_ticker(symbol="AAUSDT", pct=10.0),
            _make_ticker(symbol="BBUSDT", pct=5.0),
        ]
        wm.update_tickers(tickers)
        await wm.refresh()
        assert call_count == 1  # initial population

        # Same tickers again — no change.
        wm.update_tickers(tickers)
        await wm.refresh()
        assert call_count == 1  # no additional call

    @pytest.mark.asyncio
    async def test_grace_symbol_not_in_removed(self) -> None:
        """A grace-policy symbol should not appear in the removed list."""
        recorded: list[tuple[list[str], list[str]]] = []

        async def on_changed(added: list[str], removed: list[str]) -> None:
            recorded.append((added, removed))

        checker = FakePositionChecker(open_symbols={"BBUSDT"})
        config = _default_config(top_n=1, min_change_pct_24h=0.0, min_volume_usdt_24h=0.0)
        wm = WatchlistManager(config=config, position_checker=checker)
        wm.on_watchlist_changed = on_changed

        # Seed with both symbols.
        wm._active_symbols = ["AAUSDT", "BBUSDT"]

        # Only CCUSDT qualifies for top-1 now.
        wm.update_tickers([
            _make_ticker(symbol="CCUSDT", pct=20.0),
            _make_ticker(symbol="AAUSDT", pct=1.0),
            _make_ticker(symbol="BBUSDT", pct=0.5),
        ])
        await wm.refresh()

        assert len(recorded) == 1
        added, removed = recorded[0]
        assert "CCUSDT" in added
        assert "AAUSDT" in removed
        # BBUSDT has an open position — must NOT be in removed.
        assert "BBUSDT" not in removed


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Edge case tests."""

    @pytest.mark.asyncio
    async def test_empty_tickers(self) -> None:
        """Refresh with no tickers produces an empty watchlist."""
        config = _default_config()
        wm = WatchlistManager(config=config)
        wm.update_tickers([])
        await wm.refresh()
        assert wm.get_active_symbols() == []

    @pytest.mark.asyncio
    async def test_no_qualifying_symbols(self) -> None:
        """Refresh where no symbols pass filters produces an empty watchlist."""
        config = _default_config(min_change_pct_24h=100.0)
        wm = WatchlistManager(config=config)
        wm.update_tickers([
            _make_ticker(symbol="SOLUSDT", pct=5.0),
        ])
        await wm.refresh()
        assert wm.get_active_symbols() == []

    def test_update_tickers_stores_snapshot(self) -> None:
        """update_tickers stores a copy of the ticker list."""
        config = _default_config()
        wm = WatchlistManager(config=config)
        original = [_make_ticker()]
        wm.update_tickers(original)
        # Mutating the original should not affect the stored copy.
        original.clear()
        assert len(wm._tickers) == 1

    def test_get_active_symbols_returns_copy(self) -> None:
        """get_active_symbols returns a copy, not the internal list."""
        config = _default_config()
        wm = WatchlistManager(config=config)
        wm._active_symbols = ["SOLUSDT"]
        result = wm.get_active_symbols()
        result.append("ETHUSDT")
        assert wm._active_symbols == ["SOLUSDT"]
