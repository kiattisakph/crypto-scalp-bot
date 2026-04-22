"""Unit tests for the RiskGuard risk enforcement module."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from core.config import ExitConfig, RiskConfig
from core.models import DailyStats
from risk.risk_guard import RiskGuard


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


@pytest.fixture
def risk_config() -> RiskConfig:
    """Default risk configuration for tests."""
    return RiskConfig(
        risk_per_trade_pct=1.0,
        leverage=5,
        max_concurrent_positions=3,
        max_daily_loss_pct=3.0,
        max_drawdown_pct=5.0,
        min_free_margin_pct=30.0,
    )


@pytest.fixture
def exit_config() -> ExitConfig:
    """Default exit configuration for tests."""
    return ExitConfig(sl_pct=1.0)


@pytest.fixture
def mock_trade_repo() -> MagicMock:
    """Mock TradeRepository."""
    repo = MagicMock()
    repo.get_daily_stats = AsyncMock(return_value=None)
    repo.get_realized_loss_for_date = AsyncMock(return_value=0.0)
    repo.mark_daily_halted = AsyncMock()
    return repo


@pytest.fixture
def mock_telegram() -> MagicMock:
    """Mock TelegramAlert."""
    tg = MagicMock()
    tg.notify_risk_halt = AsyncMock()
    tg.send = AsyncMock()
    return tg


@pytest.fixture
def guard(
    risk_config: RiskConfig,
    exit_config: ExitConfig,
    mock_trade_repo: MagicMock,
    mock_telegram: MagicMock,
) -> RiskGuard:
    """RiskGuard instance with default config and mocked dependencies."""
    return RiskGuard(
        risk_config=risk_config,
        exit_config=exit_config,
        trade_repo=mock_trade_repo,
        telegram=mock_telegram,
    )


# ------------------------------------------------------------------
# Position size calculation
# ------------------------------------------------------------------


class TestPositionSizing:
    """Tests for the mandatory position sizing formula."""

    def test_position_size_formula_basic(self, guard: RiskGuard) -> None:
        """Verify the exact formula: risk_amount / sl_distance."""
        # balance=10_000, risk_per_trade_pct=1.0, sl_pct=1.0, entry=50_000
        # risk_amount = 10_000 * 1.0 / 100 = 100
        # sl_distance = 50_000 * 1.0 / 100 = 500
        # position_size = 100 / 500 = 0.2
        result = guard.check_trade(entry_price=50_000.0, balance=10_000.0)
        assert result.approved is True
        assert result.position_size == pytest.approx(0.2)

    def test_position_size_formula_different_values(
        self,
        mock_trade_repo: MagicMock,
        mock_telegram: MagicMock,
    ) -> None:
        """Verify formula with non-default config values."""
        rc = RiskConfig(
            risk_per_trade_pct=2.0,
            leverage=10,
            max_concurrent_positions=5,
            max_daily_loss_pct=5.0,
            max_drawdown_pct=10.0,
            min_free_margin_pct=20.0,
        )
        ec = ExitConfig(sl_pct=0.5)
        g = RiskGuard(rc, ec, mock_trade_repo, mock_telegram)

        # risk_amount = 5_000 * 2.0 / 100 = 100
        # sl_distance = 100.0 * 0.5 / 100 = 0.5
        # position_size = 100 / 0.5 = 200
        result = g.check_trade(entry_price=100.0, balance=5_000.0)
        assert result.approved is True
        assert result.position_size == pytest.approx(200.0)


# ------------------------------------------------------------------
# Risk check conditions
# ------------------------------------------------------------------


class TestRiskChecks:
    """Tests for the four pre-trade risk conditions."""

    def test_approved_when_all_conditions_pass(self, guard: RiskGuard) -> None:
        result = guard.check_trade(
            entry_price=100.0,
            balance=10_000.0,
            open_position_count=0,
            free_margin_pct=80.0,
        )
        assert result.approved is True
        assert result.position_size > 0
        assert guard._session_peak_balance == pytest.approx(10_000.0)

    def test_rejected_daily_loss_exceeded(self, guard: RiskGuard) -> None:
        # Simulate daily loss of 350 USDT on 10_000 balance = 3.5% > 3.0%
        guard._daily_loss_usdt = -350.0
        result = guard.check_trade(entry_price=100.0, balance=10_000.0)
        assert result.approved is False
        assert "daily_loss" in result.reject_reason

    def test_rejected_session_drawdown_exceeded(self, guard: RiskGuard) -> None:
        guard._session_drawdown_pct = 6.0  # > 5.0% max
        result = guard.check_trade(entry_price=100.0, balance=10_000.0)
        assert result.approved is False
        assert "session_drawdown" in result.reject_reason

    def test_rejected_max_concurrent_positions(self, guard: RiskGuard) -> None:
        result = guard.check_trade(
            entry_price=100.0,
            balance=10_000.0,
            open_position_count=3,  # == max_concurrent_positions
        )
        assert result.approved is False
        assert "open_positions" in result.reject_reason

    def test_rejected_insufficient_free_margin(self, guard: RiskGuard) -> None:
        result = guard.check_trade(
            entry_price=100.0,
            balance=10_000.0,
            free_margin_pct=20.0,  # < 30.0% min
        )
        assert result.approved is False
        assert "free_margin_pct" in result.reject_reason

    def test_rejected_when_halted(self, guard: RiskGuard) -> None:
        guard._halted = True
        result = guard.check_trade(entry_price=100.0, balance=10_000.0)
        assert result.approved is False
        assert "halted" in result.reject_reason


# ------------------------------------------------------------------
# PnL recording
# ------------------------------------------------------------------


class TestRecordPnl:
    """Tests for PnL recording and drawdown tracking."""

    def test_loss_accumulates_daily_loss(self, guard: RiskGuard) -> None:
        guard.record_pnl(-50.0, balance=10_000.0)
        assert guard._daily_loss_usdt == pytest.approx(-50.0)
        guard.record_pnl(-30.0, balance=9_950.0)
        assert guard._daily_loss_usdt == pytest.approx(-80.0)

    def test_profit_does_not_affect_daily_loss(self, guard: RiskGuard) -> None:
        guard.record_pnl(100.0, balance=10_100.0)
        assert guard._daily_loss_usdt == pytest.approx(0.0)

    def test_session_drawdown_tracked(self, guard: RiskGuard) -> None:
        # Set peak balance, then record a loss
        guard._session_peak_balance = 10_000.0
        guard.record_pnl(-200.0, balance=9_800.0)
        # drawdown = (10_000 - 9_800) / 10_000 * 100 = 2.0%
        assert guard._session_drawdown_pct == pytest.approx(2.0)

    def test_session_peak_updates_on_higher_balance(self, guard: RiskGuard) -> None:
        guard.record_pnl(100.0, balance=10_100.0)
        assert guard._session_peak_balance == pytest.approx(10_100.0)

    def test_first_loss_counts_drawdown_after_pretrade_peak(
        self, guard: RiskGuard
    ) -> None:
        guard.check_trade(
            entry_price=100.0,
            balance=10_000.0,
            open_position_count=0,
            free_margin_pct=80.0,
        )
        guard.record_pnl(-200.0, balance=9_800.0)
        assert guard._session_drawdown_pct == pytest.approx(2.0)


# ------------------------------------------------------------------
# Halt logic
# ------------------------------------------------------------------


class TestHaltLogic:
    """Tests for halt state transitions."""

    def test_is_halted_initially_false(self, guard: RiskGuard) -> None:
        assert guard.is_halted() is False

    @pytest.mark.asyncio
    async def test_halt_on_daily_loss(
        self, guard: RiskGuard, mock_trade_repo: MagicMock, mock_telegram: MagicMock
    ) -> None:
        # Simulate daily loss exceeding threshold
        guard._daily_loss_usdt = -350.0  # 3.5% of 10_000
        await guard.check_halt_conditions(balance=10_000.0)
        assert guard.is_halted() is True
        mock_trade_repo.mark_daily_halted.assert_awaited_once()
        mock_telegram.notify_risk_halt.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_halt_on_session_drawdown(
        self, guard: RiskGuard, mock_trade_repo: MagicMock, mock_telegram: MagicMock
    ) -> None:
        guard._session_drawdown_pct = 6.0  # > 5.0% max
        await guard.check_halt_conditions(balance=10_000.0)
        assert guard.is_halted() is True
        mock_trade_repo.mark_daily_halted.assert_awaited_once()
        mock_telegram.notify_risk_halt.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_double_halt(
        self, guard: RiskGuard, mock_telegram: MagicMock
    ) -> None:
        guard._halted = True
        guard._daily_loss_usdt = -500.0
        await guard.check_halt_conditions(balance=10_000.0)
        # Should not send another alert
        mock_telegram.notify_risk_halt.assert_not_awaited()


# ------------------------------------------------------------------
# Daily state loading
# ------------------------------------------------------------------


class TestLoadDailyState:
    """Tests for loading daily state from the database."""

    @pytest.mark.asyncio
    async def test_load_existing_stats(
        self, guard: RiskGuard, mock_trade_repo: MagicMock
    ) -> None:
        mock_trade_repo.get_daily_stats.return_value = DailyStats(
            date="2024-01-15",
            starting_balance=10_000.0,
            total_trades=5,
            winning_trades=2,
            total_pnl_usdt=50.0,
            max_drawdown_pct=1.5,
            halted=False,
        )
        mock_trade_repo.get_realized_loss_for_date.return_value = -150.0
        await guard.load_daily_state()
        assert guard._daily_loss_usdt == pytest.approx(-150.0)
        assert guard._halted is False

    @pytest.mark.asyncio
    async def test_load_no_stats_starts_fresh(
        self, guard: RiskGuard, mock_trade_repo: MagicMock
    ) -> None:
        mock_trade_repo.get_daily_stats.return_value = None
        await guard.load_daily_state()
        assert guard._daily_loss_usdt == pytest.approx(0.0)
        assert guard._halted is False

    @pytest.mark.asyncio
    async def test_load_positive_pnl_sets_zero_loss(
        self, guard: RiskGuard, mock_trade_repo: MagicMock
    ) -> None:
        mock_trade_repo.get_daily_stats.return_value = DailyStats(
            date="2024-01-15",
            starting_balance=10_000.0,
            total_trades=3,
            winning_trades=3,
            total_pnl_usdt=200.0,  # positive day
            max_drawdown_pct=0.5,
            halted=False,
        )
        mock_trade_repo.get_realized_loss_for_date.return_value = 0.0
        await guard.load_daily_state()
        # Positive PnL means no daily loss
        assert guard._daily_loss_usdt == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_load_daily_loss_not_offset_by_wins(
        self, guard: RiskGuard, mock_trade_repo: MagicMock
    ) -> None:
        mock_trade_repo.get_daily_stats.return_value = DailyStats(
            date="2024-01-15",
            starting_balance=10_000.0,
            total_trades=4,
            winning_trades=2,
            total_pnl_usdt=250.0,  # net winning day
            max_drawdown_pct=0.5,
            halted=False,
        )
        mock_trade_repo.get_realized_loss_for_date.return_value = -300.0

        await guard.load_daily_state()

        assert guard._daily_loss_usdt == pytest.approx(-300.0)

    @pytest.mark.asyncio
    async def test_load_halted_state(
        self, guard: RiskGuard, mock_trade_repo: MagicMock
    ) -> None:
        mock_trade_repo.get_daily_stats.return_value = DailyStats(
            date="2024-01-15",
            starting_balance=10_000.0,
            total_trades=10,
            winning_trades=3,
            total_pnl_usdt=-400.0,
            max_drawdown_pct=4.0,
            halted=True,
        )
        mock_trade_repo.get_realized_loss_for_date.return_value = -400.0
        await guard.load_daily_state()
        assert guard._halted is True
