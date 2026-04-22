"""Property-based tests for WatchlistManager.

Covers Properties 2–5 from the design document:
- Property 2: Watchlist filter correctness
- Property 3: Watchlist top-N sorting
- Property 4: Grace policy retention
- Property 5: Watchlist change diff correctness
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from core.config import WatchlistConfig
from core.models import TickerData
from strategy.watchlist_manager import WatchlistManager


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Strategy for generating valid USDT symbol names.
_usdt_symbol = st.from_regex(r"[A-Z]{2,6}USDT", fullmatch=True)

# Strategy for generating non-USDT symbol names.
_non_usdt_symbol = st.from_regex(r"[A-Z]{2,6}BTC", fullmatch=True)

# Strategy for generating a single TickerData with a USDT symbol.
_ticker_data = st.builds(
    TickerData,
    symbol=_usdt_symbol,
    price_change_pct=st.floats(min_value=-50.0, max_value=200.0, allow_nan=False, allow_infinity=False),
    last_price=st.floats(min_value=0.0, max_value=100_000.0, allow_nan=False, allow_infinity=False),
    quote_volume=st.floats(min_value=0.0, max_value=1e12, allow_nan=False, allow_infinity=False),
)

# Strategy for generating a mixed list of tickers (some USDT, some not).
_ticker_list = st.lists(
    st.one_of(_ticker_data, st.builds(
        TickerData,
        symbol=_non_usdt_symbol,
        price_change_pct=st.floats(min_value=-50.0, max_value=200.0, allow_nan=False, allow_infinity=False),
        last_price=st.floats(min_value=0.0001, max_value=100_000.0, allow_nan=False, allow_infinity=False),
        quote_volume=st.floats(min_value=0.0, max_value=1e12, allow_nan=False, allow_infinity=False),
    )),
    min_size=0,
    max_size=30,
)

# Strategy for generating a WatchlistConfig with randomised thresholds.
_watchlist_config = st.builds(
    WatchlistConfig,
    top_n=st.integers(min_value=1, max_value=20),
    min_change_pct_24h=st.floats(min_value=1e-9, max_value=50.0, allow_nan=False, allow_infinity=False),
    min_volume_usdt_24h=st.floats(min_value=1e-9, max_value=1e9, allow_nan=False, allow_infinity=False),
    refresh_interval_sec=st.just(300),
    max_concurrent_positions=st.just(3),
    blacklist=st.lists(st.from_regex(r"[A-Z]{2,6}USDT", fullmatch=True), max_size=5),
    blacklist_patterns=st.lists(st.from_regex(r"[A-Z]{2,4}", fullmatch=True), max_size=3),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _qualifies(t: TickerData, cfg: WatchlistConfig) -> bool:
    """Reference implementation: does *t* pass all filter criteria?"""
    if not t.symbol.endswith("USDT"):
        return False
    if t.symbol in cfg.blacklist:
        return False
    for pat in cfg.blacklist_patterns:
        if pat in t.symbol:
            return False
    if t.price_change_pct < cfg.min_change_pct_24h:
        return False
    if t.quote_volume < cfg.min_volume_usdt_24h:
        return False
    if t.last_price <= 0.0001:
        return False
    return True


@dataclass
class _FakePositionChecker:
    """Stub position checker for property tests."""

    open_symbols: set[str]

    def has_position(self, symbol: str) -> bool:
        return symbol in self.open_symbols


# ---------------------------------------------------------------------------
# Property 2: Watchlist filter correctness
# ---------------------------------------------------------------------------

# Feature: crypto-scalp-bot, Property 2: Watchlist filter correctness
@settings(max_examples=100)
@given(tickers=_ticker_list, config=_watchlist_config)
def test_filter_correctness(tickers: list[TickerData], config: WatchlistConfig) -> None:
    """Every symbol in the filtered output satisfies ALL filter criteria,
    and no qualifying symbol is excluded from the output.
    """
    wm = WatchlistManager(config=config)
    filtered = wm._filter_symbols(tickers)

    filtered_symbols = {t.symbol for t in filtered}

    # Every symbol in output must satisfy all criteria.
    for t in filtered:
        assert _qualifies(t, config), (
            f"Symbol {t.symbol} in output but does not qualify"
        )

    # Every qualifying symbol must be in the output.
    for t in tickers:
        if _qualifies(t, config):
            assert t.symbol in filtered_symbols, (
                f"Symbol {t.symbol} qualifies but was excluded"
            )


# ---------------------------------------------------------------------------
# Property 3: Watchlist top-N sorting
# ---------------------------------------------------------------------------

# Feature: crypto-scalp-bot, Property 3: Watchlist top-N sorting
@settings(max_examples=100)
@given(tickers=_ticker_list, config=_watchlist_config)
def test_top_n_sorting(tickers: list[TickerData], config: WatchlistConfig) -> None:
    """Output is sorted by price_change_pct DESC and length is
    min(len(qualifying), top_n).
    """
    wm = WatchlistManager(config=config)
    wm.update_tickers(tickers)
    asyncio.run(wm.refresh())

    active = wm.get_active_symbols()

    # Determine expected qualifying count.
    qualifying = [t for t in tickers if _qualifies(t, config)]
    # Deduplicate by symbol, keeping the first occurrence (highest pct after sort).
    seen: set[str] = set()
    unique_qualifying: list[TickerData] = []
    for t in sorted(qualifying, key=lambda x: x.price_change_pct, reverse=True):
        if t.symbol not in seen:
            seen.add(t.symbol)
            unique_qualifying.append(t)

    expected_len = min(len(unique_qualifying), config.top_n)
    assert len(active) >= expected_len, (
        f"Expected at least {expected_len} symbols, got {len(active)}"
    )

    # The first top_n symbols should be sorted by price_change_pct DESC.
    # (Grace symbols may be appended at the end, so we only check the
    # top_n portion.)
    #
    # Build a lookup of the best (highest) qualifying pct per symbol.
    # A symbol may appear multiple times in the raw ticker list with
    # different values; the WatchlistManager keeps the highest pct entry
    # after deduplication, so the test must do the same.
    best_pct: dict[str, float] = {}
    for t in tickers:
        if _qualifies(t, config):
            prev = best_pct.get(t.symbol)
            if prev is None or t.price_change_pct > prev:
                best_pct[t.symbol] = t.price_change_pct

    top_portion = active[:expected_len]
    for i in range(len(top_portion) - 1):
        pct_i = best_pct.get(top_portion[i])
        pct_j = best_pct.get(top_portion[i + 1])
        if pct_i is not None and pct_j is not None:
            assert pct_i >= pct_j, (
                f"Symbols not sorted DESC: {top_portion[i]} ({pct_i}) < {top_portion[i + 1]} ({pct_j})"
            )


# ---------------------------------------------------------------------------
# Property 4: Grace policy retention
# ---------------------------------------------------------------------------

# Feature: crypto-scalp-bot, Property 4: Grace policy retention
@settings(max_examples=100)
@given(
    tickers=st.lists(_ticker_data, min_size=3, max_size=20),
    top_n=st.integers(min_value=1, max_value=5),
)
def test_grace_policy_retention(tickers: list[TickerData], top_n: int) -> None:
    """Symbols with open positions remain in the watchlist regardless of ranking."""
    config = WatchlistConfig(
        top_n=top_n,
        min_change_pct_24h=1e-9,
        min_volume_usdt_24h=1e-9,
    )

    # Deduplicate tickers by symbol.
    seen: set[str] = set()
    unique_tickers: list[TickerData] = []
    for t in tickers:
        if t.symbol not in seen and t.last_price > 0.0001:
            seen.add(t.symbol)
            unique_tickers.append(t)

    if len(unique_tickers) < 2:
        return  # Not enough data for a meaningful test.

    # Sort by pct DESC and pick a symbol that would NOT make the top_n cut.
    sorted_tickers = sorted(unique_tickers, key=lambda x: x.price_change_pct, reverse=True)
    if len(sorted_tickers) <= top_n:
        return  # All symbols would make the cut; nothing to test.

    # Pick the last symbol (lowest pct) as the one with an open position.
    grace_symbol = sorted_tickers[-1].symbol

    checker = _FakePositionChecker(open_symbols={grace_symbol})
    wm = WatchlistManager(config=config, position_checker=checker)

    # First refresh to populate the initial watchlist (must include grace_symbol
    # so it's in _active_symbols for the grace check on the second refresh).
    all_tickers = list(unique_tickers)
    wm.update_tickers(all_tickers)
    # Manually set the active symbols to include the grace symbol so the
    # grace policy can detect it on the next refresh.
    wm._active_symbols = [t.symbol for t in sorted_tickers]

    # Second refresh — grace symbol should be retained.
    asyncio.run(wm.refresh())

    active = wm.get_active_symbols()
    assert grace_symbol in active, (
        f"Grace symbol {grace_symbol} was dropped despite having an open position"
    )


# ---------------------------------------------------------------------------
# Property 5: Watchlist change diff correctness
# ---------------------------------------------------------------------------

# Feature: crypto-scalp-bot, Property 5: Watchlist change diff correctness
@settings(max_examples=100)
@given(
    old_symbols=st.lists(st.from_regex(r"[A-Z]{3,5}USDT", fullmatch=True), min_size=0, max_size=10, unique=True),
    new_symbols=st.lists(st.from_regex(r"[A-Z]{3,5}USDT", fullmatch=True), min_size=0, max_size=10, unique=True),
    grace_symbols=st.lists(st.from_regex(r"[A-Z]{3,5}USDT", fullmatch=True), min_size=0, max_size=3, unique=True),
)
def test_watchlist_change_diff(
    old_symbols: list[str],
    new_symbols: list[str],
    grace_symbols: list[str],
) -> None:
    """added = new - old, removed = old - new (excluding grace symbols)."""
    recorded_added: list[str] = []
    recorded_removed: list[str] = []

    async def _on_changed(added: list[str], removed: list[str]) -> None:
        recorded_added.extend(added)
        recorded_removed.extend(removed)

    # Build tickers for the new symbols with qualifying data.
    new_tickers = [
        TickerData(symbol=s, price_change_pct=10.0, last_price=1.0, quote_volume=1e9)
        for s in new_symbols
    ]

    # Grace symbols need tickers too (they may not be in new_symbols).
    grace_set = set(grace_symbols)
    for s in grace_symbols:
        if s not in new_symbols:
            new_tickers.append(
                TickerData(symbol=s, price_change_pct=-1.0, last_price=1.0, quote_volume=1e9)
            )

    checker = _FakePositionChecker(open_symbols=grace_set)
    config = WatchlistConfig(
        top_n=100,  # Large enough to include all qualifying symbols.
        min_change_pct_24h=1e-9,
        min_volume_usdt_24h=1e-9,
    )
    wm = WatchlistManager(config=config, position_checker=checker)
    wm.on_watchlist_changed = _on_changed

    # Set the old watchlist state directly.
    wm._active_symbols = list(old_symbols)

    # Refresh with new tickers.
    wm.update_tickers(new_tickers)
    asyncio.run(wm.refresh())

    old_set = set(old_symbols)
    new_set = set(new_symbols)

    expected_added = new_set - old_set
    # Grace symbols that were in old but not in new should NOT be removed.
    expected_removed = old_set - new_set - grace_set

    # Also account for grace symbols that were in old_symbols and still
    # have positions — they should be retained, not removed.
    # Grace symbols not in old_symbols but added via grace policy count as added.
    grace_added = {s for s in grace_symbols if s not in old_set and s in wm.get_active_symbols()}
    expected_added = expected_added | grace_added

    assert set(recorded_added) == expected_added, (
        f"Added mismatch: got {set(recorded_added)}, expected {expected_added}"
    )
    assert set(recorded_removed) == expected_removed, (
        f"Removed mismatch: got {set(recorded_removed)}, expected {expected_removed}"
    )
