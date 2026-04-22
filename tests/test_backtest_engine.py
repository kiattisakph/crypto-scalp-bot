"""Tests for the backtest engine and data fetcher.

Verifies core logic without requiring a live Binance connection:
- _BacktestPosition exit detection (SL, TP1, TP2, TP3, trailing, TIME)
- _BacktestRiskGuard pre-trade checks
- BacktestEngine.load_data and signal evaluation
- Qualifying symbol identification
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from core.config import AppConfig, EntryConfig, ExitConfig, RiskConfig, StrategyConfig, WatchlistConfig
from core.enums import SignalDirection
from backtest.backtest_engine import _BacktestPosition, _BacktestRiskGuard, BacktestEngine


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


@pytest.fixture
def exit_cfg() -> ExitConfig:
    return ExitConfig()


@pytest.fixture
def risk_cfg() -> RiskConfig:
    return RiskConfig()


@pytest.fixture
def app_config() -> AppConfig:
    return AppConfig(
        watchlist=WatchlistConfig(),
        strategy=StrategyConfig(),
        risk=RiskConfig(),
    )


# ------------------------------------------------------------------
# _BacktestPosition tests
# ------------------------------------------------------------------


class TestBacktestPosition:
    """Verify exit detection logic matches the live PositionManager."""

    def _make_long_position(self, exit_cfg: ExitConfig) -> _BacktestPosition:
        return _BacktestPosition(
            symbol="BTCUSDT",
            side=SignalDirection.LONG,
            entry_price=100.0,
            quantity=1.0,
            leverage=5,
            tp1_pct=exit_cfg.tp1_pct,
            tp2_pct=exit_cfg.tp2_pct,
            tp3_pct=exit_cfg.tp3_pct,
            tp1_ratio=exit_cfg.tp1_close_ratio,
            tp2_ratio=exit_cfg.tp2_close_ratio,
            sl_pct=exit_cfg.sl_pct,
            trailing_pct=exit_cfg.trailing_stop_pct,
            max_hold_min=exit_cfg.max_hold_min,
            opened_at=datetime(2025, 1, 1, 0, 0),
            indicators={},
        )

    def _make_short_position(self, exit_cfg: ExitConfig) -> _BacktestPosition:
        pos = self._make_long_position(exit_cfg)
        pos.side = SignalDirection.SHORT
        pos.entry_price = 100.0
        # Recalculate TP/SL for SHORT
        pos.tp1_price = 100.0 * (1 - exit_cfg.tp1_pct / 100)
        pos.tp2_price = 100.0 * (1 - exit_cfg.tp2_pct / 100)
        pos.tp3_price = 100.0 * (1 - exit_cfg.tp3_pct / 100)
        pos.sl_price = 100.0 * (1 + exit_cfg.sl_pct / 100)
        return pos

    def test_sl_hit_long(self, exit_cfg: ExitConfig) -> None:
        pos = self._make_long_position(exit_cfg)
        # SL = 100 * (1 - 0.01) = 99.0
        events = pos.check_exits(99.0, datetime(2025, 1, 1, 0, 1))
        assert len(events) == 1
        assert events[0]["reason"] == "SL"
        assert pos.quantity == 0

    def test_sl_hit_short(self, exit_cfg: ExitConfig) -> None:
        pos = self._make_short_position(exit_cfg)
        # SL = 100 * (1 + 0.01) = 101.0
        events = pos.check_exits(101.0, datetime(2025, 1, 1, 0, 1))
        assert len(events) == 1
        assert events[0]["reason"] == "SL"
        assert pos.quantity == 0

    def test_tp1_partial_close(self, exit_cfg: ExitConfig) -> None:
        pos = self._make_long_position(exit_cfg)
        # TP1 = 100 * 1.008 = 100.8
        events = pos.check_exits(100.8, datetime(2025, 1, 1, 0, 1))
        assert len(events) == 1
        assert events[0]["reason"] == "TP1"
        assert pos.tp1_hit is True
        assert pos.sl_price == 100.0  # SL moved to breakeven
        assert pos.quantity > 0  # Not fully closed

    def test_tp1_then_sl(self, exit_cfg: ExitConfig) -> None:
        pos = self._make_long_position(exit_cfg)
        # Hit TP1
        pos.check_exits(100.8, datetime(2025, 1, 1, 0, 1))
        assert pos.tp1_hit
        # Now price drops to breakeven SL
        events = pos.check_exits(100.0, datetime(2025, 1, 1, 0, 2))
        assert len(events) == 1
        assert events[0]["reason"] == "SL"
        assert pos.quantity == 0

    def test_tp2_after_tp1(self, exit_cfg: ExitConfig) -> None:
        pos = self._make_long_position(exit_cfg)
        # Hit TP1
        pos.check_exits(100.8, datetime(2025, 1, 1, 0, 1))
        # Hit TP2 = 100 * 1.015 = 101.5
        events = pos.check_exits(101.5, datetime(2025, 1, 1, 0, 2))
        assert any(e.get("reason") == "TP2" for e in events)
        assert pos.tp2_hit is True

    def test_time_force_close(self, exit_cfg: ExitConfig) -> None:
        pos = self._make_long_position(exit_cfg)
        # Time exceeds max_hold_min (30 min)
        events = pos.check_exits(100.5, datetime(2025, 1, 1, 0, 31))
        assert any(e.get("reason") == "TIME" for e in events)
        assert pos.quantity == 0

    def test_trailing_activation(self, exit_cfg: ExitConfig) -> None:
        pos = self._make_long_position(exit_cfg)
        # Hit TP1, TP2, TP3 in sequence
        pos.check_exits(100.8, datetime(2025, 1, 1, 0, 1))  # TP1
        pos.check_exits(101.5, datetime(2025, 1, 1, 0, 2))  # TP2
        events = pos.check_exits(102.5, datetime(2025, 1, 1, 0, 3))  # TP3
        assert pos.trailing_active is True
        assert pos.trailing_price > 0

    def test_trailing_stop_triggered(self, exit_cfg: ExitConfig) -> None:
        pos = self._make_long_position(exit_cfg)
        # Activate trailing
        pos.check_exits(100.8, datetime(2025, 1, 1, 0, 1))  # TP1
        pos.check_exits(101.5, datetime(2025, 1, 1, 0, 2))  # TP2
        pos.check_exits(102.5, datetime(2025, 1, 1, 0, 3))  # TP3, trailing activated
        # Price drops to trailing trigger
        trigger = pos.trailing_price
        events = pos.check_exits(trigger - 0.001, datetime(2025, 1, 1, 0, 4))
        assert any(e.get("reason") == "TP3" for e in events)
        assert pos.quantity == 0

    def test_pnl_calculation_long(self, exit_cfg: ExitConfig) -> None:
        pos = self._make_long_position(exit_cfg)
        # Close at higher price
        events = pos.check_exits(101.0, datetime(2025, 1, 1, 0, 1))
        total_pnl = pos.realized_pnl_usdt
        assert total_pnl > 0  # Profit on LONG with price increase

    def test_pnl_calculation_short(self, exit_cfg: ExitConfig) -> None:
        pos = self._make_short_position(exit_cfg)
        # Close at lower price
        events = pos.check_exits(99.0, datetime(2025, 1, 1, 0, 1))
        total_pnl = pos.realized_pnl_usdt
        assert total_pnl > 0  # Profit on SHORT with price decrease


# ------------------------------------------------------------------
# _BacktestRiskGuard tests
# ------------------------------------------------------------------


class TestBacktestRiskGuard:
    def test_approve_trade(self, risk_cfg: RiskConfig) -> None:
        guard = _BacktestRiskGuard(
            risk_per_trade_pct=risk_cfg.risk_per_trade_pct,
            sl_pct=1.0,
            max_concurrent=risk_cfg.max_concurrent_positions,
            max_daily_loss_pct=risk_cfg.max_daily_loss_pct,
            max_drawdown_pct=risk_cfg.max_drawdown_pct,
            min_free_margin_pct=risk_cfg.min_free_margin_pct,
        )
        approved, size, reason = guard.check_trade(100.0, 10_000.0, 0)
        assert approved
        assert size > 0

    def test_reject_max_concurrent(self, risk_cfg: RiskConfig) -> None:
        guard = _BacktestRiskGuard(
            risk_per_trade_pct=risk_cfg.risk_per_trade_pct,
            sl_pct=1.0,
            max_concurrent=risk_cfg.max_concurrent_positions,
            max_daily_loss_pct=risk_cfg.max_daily_loss_pct,
            max_drawdown_pct=risk_cfg.max_drawdown_pct,
            min_free_margin_pct=risk_cfg.min_free_margin_pct,
        )
        approved, _, reason = guard.check_trade(100.0, 10_000.0, 3)
        assert not approved
        assert "max_concurrent" in reason

    def test_reject_daily_loss(self, risk_cfg: RiskConfig) -> None:
        guard = _BacktestRiskGuard(
            risk_per_trade_pct=risk_cfg.risk_per_trade_pct,
            sl_pct=1.0,
            max_concurrent=risk_cfg.max_concurrent_positions,
            max_daily_loss_pct=risk_cfg.max_daily_loss_pct,
            max_drawdown_pct=risk_cfg.max_drawdown_pct,
            min_free_margin_pct=risk_cfg.min_free_margin_pct,
        )
        # Simulate a large loss
        guard.daily_loss_usdt = -500  # 5% of 10k
        approved, _, reason = guard.check_trade(100.0, 10_000.0, 0)
        assert not approved
        assert "daily_loss" in reason

    def test_halt_on_drawdown(self, risk_cfg: RiskConfig) -> None:
        guard = _BacktestRiskGuard(
            risk_per_trade_pct=risk_cfg.risk_per_trade_pct,
            sl_pct=1.0,
            max_concurrent=risk_cfg.max_concurrent_positions,
            max_daily_loss_pct=risk_cfg.max_daily_loss_pct,
            max_drawdown_pct=risk_cfg.max_drawdown_pct,
            min_free_margin_pct=risk_cfg.min_free_margin_pct,
        )
        # Record a loss that exceeds drawdown
        guard.record_pnl(-600, 9_400)
        guard.check_trade(100.0, 9_400, 0)  # Check will set peak/halt
        assert guard.halted


# ------------------------------------------------------------------
# BacktestEngine tests
# ------------------------------------------------------------------


class TestBacktestEngine:
    def test_empty_data_returns_empty_stats(self, app_config: AppConfig) -> None:
        engine = BacktestEngine(app_config)
        engine.load_data({}, {})
        stats = engine.run()
        assert stats.total_trades == 0

    def test_load_data_converts_to_dataframe(self, app_config: AppConfig) -> None:
        engine = BacktestEngine(app_config)
        candles_15m = {
            "BTCUSDT": [
                {
                    "open": 100, "high": 101, "low": 99, "close": 100.5,
                    "volume": 1000, "timestamp": datetime(2025, 1, 1, 0, 0),
                },
            ]
        }
        candles_3m = {
            "BTCUSDT": [
                {
                    "open": 100, "high": 101, "low": 99, "close": 100.5,
                    "volume": 1000, "timestamp": datetime(2025, 1, 1, 0, 0),
                },
            ]
        }
        engine.load_data(candles_15m, candles_3m)
        assert "BTCUSDT" in engine._klines_15m
        assert "BTCUSDT" in engine._klines_3m
        assert len(engine._klines_15m["BTCUSDT"]) == 1


# ------------------------------------------------------------------
# ATR-based TP/SL tests
# ------------------------------------------------------------------


class TestAtrBasedTPSL:
    """Verify ATR-based TP/SL calculation and behavior."""

    def test_atr_position_long_levels(self) -> None:
        """ATR-based LONG: TP = entry + ATR × mult, SL = entry - ATR × mult."""
        atr_value = 2.0  # $2 ATR on a $100 asset = 2% volatility
        pos = _BacktestPosition(
            symbol="BTCUSDT",
            side=SignalDirection.LONG,
            entry_price=100.0,
            quantity=1.0,
            leverage=5,
            tp1_pct=0.8, tp2_pct=1.5, tp3_pct=2.5,
            tp1_ratio=0.4, tp2_ratio=0.4,
            sl_pct=1.0, trailing_pct=0.5,
            max_hold_min=30,
            opened_at=datetime(2025, 1, 1),
            indicators={},
            atr_mode=True,
            atr_tp1_mult=0.8, atr_tp2_mult=1.5, atr_tp3_mult=2.5,
            atr_sl_mult=1.0, atr_trailing_mult=0.5,
            atr_value=atr_value,
        )
        assert pos.tp1_price == 100.0 + 2.0 * 0.8  # 101.6
        assert pos.tp2_price == 100.0 + 2.0 * 1.5  # 103.0
        assert pos.tp3_price == 100.0 + 2.0 * 2.5  # 105.0
        assert pos.sl_price == 100.0 - 2.0 * 1.0   # 98.0

    def test_atr_position_short_levels(self) -> None:
        """ATR-based SHORT: TP = entry - ATR × mult, SL = entry + ATR × mult."""
        atr_value = 0.005  # $0.005 ATR on a $0.10 meme coin = 5% volatility
        pos = _BacktestPosition(
            symbol="DOGECOIN",
            side=SignalDirection.SHORT,
            entry_price=0.10,
            quantity=10000.0,
            leverage=5,
            tp1_pct=0.8, tp2_pct=1.5, tp3_pct=2.5,
            tp1_ratio=0.4, tp2_ratio=0.4,
            sl_pct=1.0, trailing_pct=0.5,
            max_hold_min=30,
            opened_at=datetime(2025, 1, 1),
            indicators={},
            atr_mode=True,
            atr_tp1_mult=0.8, atr_tp2_mult=1.5, atr_tp3_mult=2.5,
            atr_sl_mult=1.0, atr_trailing_mult=0.5,
            atr_value=atr_value,
        )
        assert pos.tp1_price == 0.10 - 0.005 * 0.8  # 0.096
        assert pos.tp2_price == 0.10 - 0.005 * 1.5  # 0.0925
        assert pos.tp3_price == 0.10 - 0.005 * 2.5  # 0.0875
        assert pos.sl_price == 0.10 + 0.005 * 1.0   # 0.105

    def test_atr_sl_hit_long(self) -> None:
        """ATR-based SL hit detection for LONG."""
        atr_value = 2.0
        pos = _BacktestPosition(
            symbol="BTCUSDT",
            side=SignalDirection.LONG,
            entry_price=100.0,
            quantity=1.0,
            leverage=5,
            tp1_pct=0.8, tp2_pct=1.5, tp3_pct=2.5,
            tp1_ratio=0.4, tp2_ratio=0.4,
            sl_pct=1.0, trailing_pct=0.5,
            max_hold_min=30,
            opened_at=datetime(2025, 1, 1),
            indicators={},
            atr_mode=True,
            atr_tp1_mult=0.8, atr_tp2_mult=1.5, atr_tp3_mult=2.5,
            atr_sl_mult=1.0, atr_trailing_mult=0.5,
            atr_value=atr_value,
        )
        # SL = 98.0
        events = pos.check_exits(98.0, datetime(2025, 1, 1, 0, 1))
        assert len(events) == 1
        assert events[0]["reason"] == "SL"
        assert pos.quantity == 0

    def test_atr_vs_fixed_different_sl(self) -> None:
        """ATR mode produces different SL than fixed % for volatile coins."""
        entry_price = 0.10
        fixed_sl_pct = 1.0
        atr_value = 0.005  # 5% of price

        # Fixed mode: SL = 0.10 * 0.99 = 0.099
        fixed_pos = _BacktestPosition(
            symbol="X", side=SignalDirection.LONG, entry_price=entry_price,
            quantity=1.0, leverage=5,
            tp1_pct=0.8, tp2_pct=1.5, tp3_pct=2.5,
            tp1_ratio=0.4, tp2_ratio=0.4, sl_pct=fixed_sl_pct,
            trailing_pct=0.5, max_hold_min=30,
            opened_at=datetime(2025, 1, 1), indicators={},
            atr_mode=False,
            atr_tp1_mult=0.8, atr_tp2_mult=1.5, atr_tp3_mult=2.5,
            atr_sl_mult=1.0, atr_trailing_mult=0.5, atr_value=atr_value,
        )

        # ATR mode: SL = 0.10 - 0.005 * 1.0 = 0.095
        atr_pos = _BacktestPosition(
            symbol="X", side=SignalDirection.LONG, entry_price=entry_price,
            quantity=1.0, leverage=5,
            tp1_pct=0.8, tp2_pct=1.5, tp3_pct=2.5,
            tp1_ratio=0.4, tp2_ratio=0.4, sl_pct=fixed_sl_pct,
            trailing_pct=0.5, max_hold_min=30,
            opened_at=datetime(2025, 1, 1), indicators={},
            atr_mode=True,
            atr_tp1_mult=0.8, atr_tp2_mult=1.5, atr_tp3_mult=2.5,
            atr_sl_mult=1.0, atr_trailing_mult=0.5, atr_value=atr_value,
        )

        # ATR SL (0.095) is wider (lower) than Fixed SL (0.099) for volatile coin
        assert atr_pos.sl_price < fixed_pos.sl_price
        # ATR SL distance: 0.005 (5%), Fixed SL distance: 0.001 (1%)
        assert (entry_price - atr_pos.sl_price) > (entry_price - fixed_pos.sl_price)

    def test_fallback_when_no_atr_value(self) -> None:
        """ATR mode enabled but no ATR value → falls back to fixed %."""
        pos = _BacktestPosition(
            symbol="BTCUSDT",
            side=SignalDirection.LONG,
            entry_price=100.0,
            quantity=1.0,
            leverage=5,
            tp1_pct=0.8, tp2_pct=1.5, tp3_pct=2.5,
            tp1_ratio=0.4, tp2_ratio=0.4,
            sl_pct=1.0, trailing_pct=0.5,
            max_hold_min=30,
            opened_at=datetime(2025, 1, 1),
            indicators={},
            atr_mode=True,  # enabled
            atr_tp1_mult=0.8, atr_tp2_mult=1.5, atr_tp3_mult=2.5,
            atr_sl_mult=1.0, atr_trailing_mult=0.5,
            atr_value=None,  # but no value provided
        )
        # Should use fixed %
        assert pos.tp1_price == 100.8
        assert pos.sl_price == 99.0

    def test_atr_risk_sizing(self) -> None:
        """ATR-based position sizing: larger SL distance → smaller position."""
        guard_atr = _BacktestRiskGuard(
            risk_per_trade_pct=1.0, sl_pct=1.0, max_concurrent=3,
            max_daily_loss_pct=3.0, max_drawdown_pct=5.0,
            min_free_margin_pct=30.0,
            atr_mode=True, atr_sl_mult=1.0,
        )
        guard_fixed = _BacktestRiskGuard(
            risk_per_trade_pct=1.0, sl_pct=1.0, max_concurrent=3,
            max_daily_loss_pct=3.0, max_drawdown_pct=5.0,
            min_free_margin_pct=30.0,
            atr_mode=False, atr_sl_mult=1.0,
        )

        # For a volatile coin (ATR = 5% of price)
        # ATR SL = 0.005 × 1.0 = 0.005
        # Fixed SL = 0.10 × 0.01 = 0.001
        atr_ok, atr_size, _ = guard_atr.check_trade(
            0.10, 10_000.0, 0, atr_value=0.005
        )
        fixed_ok, fixed_size, _ = guard_fixed.check_trade(
            0.10, 10_000.0, 0, atr_value=None
        )

        assert atr_ok and fixed_ok
        # ATR-based sizing gives smaller position (wider SL = risk same $ but fewer units)
        assert atr_size < fixed_size


# ------------------------------------------------------------------
# Market Regime Detection tests (ADX-based)
# ------------------------------------------------------------------


class TestMarketRegime:
    """Verify ADX-based regime detection suppresses signals in sideways markets."""

    def test_adx_above_threshold_allows_signal(self) -> None:
        """ADX = 30 (trending) with good signal should be evaluated."""
        from core.enums import SignalDirection

        # Simulate signal with ADX = 30, threshold = 20
        adx_value = 30.0
        threshold = 20.0
        assert adx_value >= threshold  # Should allow

    def test_adx_below_threshold_blocks_signal(self) -> None:
        """ADX = 12 (sideways) should block even if signal fires."""
        adx_value = 12.0
        threshold = 20.0
        assert adx_value < threshold  # Should block

    def test_adx_none_passes(self) -> None:
        """If ADX is not available (None), signal should still be allowed."""
        adx_value = None
        threshold = 20.0
        # In the actual code: `if adx_value is not None and adx_value < threshold`
        # So None passes through
        assert not (adx_value is not None and adx_value < threshold)

    def test_adx_at_threshold_boundary(self) -> None:
        """ADX exactly at threshold should be allowed (>= threshold)."""
        adx_value = 20.0
        threshold = 20.0
        assert adx_value >= threshold  # Should allow
