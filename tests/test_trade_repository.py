"""Unit tests for TradeRepository CRUD operations."""
from __future__ import annotations

from datetime import datetime

import pytest
import pytest_asyncio

from core.enums import ExitReason
from core.models import DailyStats, ExitData, OpenTradeRecord, TradeRecord
from storage.database import Database
from storage.trade_repository import TradeRepository


@pytest_asyncio.fixture
async def repo(in_memory_db: Database) -> TradeRepository:
    """Provide a TradeRepository backed by an in-memory database."""
    return TradeRepository(in_memory_db)


def _make_trade(**overrides) -> TradeRecord:
    """Helper to build a TradeRecord with sensible defaults."""
    defaults = dict(
        symbol="SOLUSDT",
        side="LONG",
        entry_price=150.0,
        quantity=1.0,
        leverage=5,
        entry_at=datetime(2025, 6, 1, 12, 0, 0),
        signal_snapshot='{"rsi": 55}',
        status="OPEN",
    )
    defaults.update(overrides)
    return TradeRecord(**defaults)


def _make_exit(**overrides) -> ExitData:
    """Helper to build an ExitData with sensible defaults."""
    defaults = dict(
        exit_price=151.2,
        pnl_usdt=1.2,
        pnl_pct=0.8,
        exit_reason=ExitReason.TP1,
        exit_at=datetime(2025, 6, 1, 12, 15, 0),
    )
    defaults.update(overrides)
    return ExitData(**defaults)


# ---------------------------------------------------------------------------
# insert_trade
# ---------------------------------------------------------------------------


class TestInsertTrade:
    """Tests for TradeRepository.insert_trade."""

    @pytest.mark.asyncio
    async def test_returns_positive_id(self, repo: TradeRepository) -> None:
        trade = _make_trade()
        trade_id = await repo.insert_trade(trade)
        assert trade_id is not None
        assert trade_id > 0

    @pytest.mark.asyncio
    async def test_sequential_ids(self, repo: TradeRepository) -> None:
        id1 = await repo.insert_trade(_make_trade(symbol="SOLUSDT"))
        id2 = await repo.insert_trade(_make_trade(symbol="ETHUSDT"))
        assert id2 > id1

    @pytest.mark.asyncio
    async def test_persists_all_fields(self, repo: TradeRepository) -> None:
        trade = _make_trade(
            symbol="BTCUSDT",
            side="SHORT",
            entry_price=60000.0,
            quantity=0.5,
            leverage=10,
            signal_snapshot='{"ema9": 59000}',
        )
        trade_id = await repo.insert_trade(trade)

        conn = await repo._conn()
        cursor = await conn.execute(
            "SELECT symbol, side, entry_price, quantity, leverage, status, signal_snapshot "
            "FROM trades WHERE id = ?",
            (trade_id,),
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "BTCUSDT"
        assert row[1] == "SHORT"
        assert row[2] == 60000.0
        assert row[3] == 0.5
        assert row[4] == 10
        assert row[5] == "OPEN"
        assert row[6] == '{"ema9": 59000}'


# ---------------------------------------------------------------------------
# close_trade
# ---------------------------------------------------------------------------


class TestCloseTrade:
    """Tests for TradeRepository.close_trade."""

    @pytest.mark.asyncio
    async def test_updates_trade_to_closed(self, repo: TradeRepository) -> None:
        trade_id = await repo.insert_trade(_make_trade())
        exit_data = _make_exit()
        await repo.close_trade(trade_id, exit_data)

        conn = await repo._conn()
        cursor = await conn.execute(
            "SELECT status, exit_price, pnl_usdt, pnl_pct, exit_reason, exit_at "
            "FROM trades WHERE id = ?",
            (trade_id,),
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "CLOSED"
        assert row[1] == 151.2
        assert row[2] == 1.2
        assert row[3] == 0.8
        assert row[4] == "TP1"
        assert row[5] is not None

    @pytest.mark.asyncio
    async def test_close_with_loss(self, repo: TradeRepository) -> None:
        trade_id = await repo.insert_trade(_make_trade())
        exit_data = _make_exit(
            exit_price=148.5,
            pnl_usdt=-1.5,
            pnl_pct=-1.0,
            exit_reason=ExitReason.SL,
        )
        await repo.close_trade(trade_id, exit_data)

        conn = await repo._conn()
        cursor = await conn.execute(
            "SELECT pnl_usdt, exit_reason FROM trades WHERE id = ?",
            (trade_id,),
        )
        row = await cursor.fetchone()
        assert row[0] == -1.5
        assert row[1] == "SL"

    @pytest.mark.asyncio
    async def test_close_nonexistent_trade_raises(self, repo: TradeRepository) -> None:
        exit_data = _make_exit()
        with pytest.raises(ValueError, match="No trade found with id=9999"):
            await repo.close_trade(9999, exit_data)

    @pytest.mark.asyncio
    async def test_all_exit_reasons(self, repo: TradeRepository) -> None:
        for reason in ExitReason:
            trade_id = await repo.insert_trade(_make_trade())
            exit_data = _make_exit(exit_reason=reason)
            await repo.close_trade(trade_id, exit_data)

            conn = await repo._conn()
            cursor = await conn.execute(
                "SELECT exit_reason FROM trades WHERE id = ?",
                (trade_id,),
            )
            row = await cursor.fetchone()
            assert row[0] == reason.value


# ---------------------------------------------------------------------------
# get_open_trades
# ---------------------------------------------------------------------------


class TestGetOpenTrades:
    """Tests for TradeRepository.get_open_trades."""

    @pytest.mark.asyncio
    async def test_returns_only_open_trades(self, repo: TradeRepository) -> None:
        open_id = await repo.insert_trade(_make_trade(symbol="SOLUSDT"))
        closed_id = await repo.insert_trade(_make_trade(symbol="ETHUSDT"))
        await repo.close_trade(closed_id, _make_exit())

        rows = await repo.get_open_trades()

        assert len(rows) == 1
        assert isinstance(rows[0], OpenTradeRecord)
        assert rows[0].id == open_id
        assert rows[0].symbol == "SOLUSDT"
        assert rows[0].side == "LONG"
        assert rows[0].entry_price == 150.0


# ---------------------------------------------------------------------------
# get_realized_loss_for_date
# ---------------------------------------------------------------------------


class TestGetRealizedLossForDate:
    """Tests for realized daily loss aggregation."""

    @pytest.mark.asyncio
    async def test_sums_only_closed_losing_trades_for_date(
        self, repo: TradeRepository
    ) -> None:
        win_id = await repo.insert_trade(_make_trade(symbol="SOLUSDT"))
        loss_id = await repo.insert_trade(_make_trade(symbol="ETHUSDT"))
        other_day_loss_id = await repo.insert_trade(_make_trade(symbol="BTCUSDT"))
        open_loss_id = await repo.insert_trade(_make_trade(symbol="XRPUSDT"))

        await repo.close_trade(win_id, _make_exit(pnl_usdt=500.0))
        await repo.close_trade(loss_id, _make_exit(pnl_usdt=-200.0))
        await repo.close_trade(
            other_day_loss_id,
            _make_exit(
                pnl_usdt=-300.0,
                exit_at=datetime(2025, 6, 2, 12, 15, 0),
            ),
        )

        result = await repo.get_realized_loss_for_date("2025-06-01")

        assert result == pytest.approx(-200.0)

        conn = await repo._conn()
        cursor = await conn.execute(
            "SELECT status FROM trades WHERE id = ?",
            (open_loss_id,),
        )
        row = await cursor.fetchone()
        assert row[0] == "OPEN"


# ---------------------------------------------------------------------------
# update_daily_stats
# ---------------------------------------------------------------------------


class TestUpdateDailyStats:
    """Tests for TradeRepository.update_daily_stats."""

    @pytest.mark.asyncio
    async def test_creates_new_row_on_first_call(self, repo: TradeRepository) -> None:
        await repo.update_daily_stats("2025-06-01", 5.0, True)
        stats = await repo.get_daily_stats("2025-06-01")
        assert stats is not None
        assert stats.total_trades == 1
        assert stats.winning_trades == 1
        assert stats.total_pnl_usdt == 5.0

    @pytest.mark.asyncio
    async def test_increments_on_subsequent_calls(self, repo: TradeRepository) -> None:
        await repo.update_daily_stats("2025-06-01", 5.0, True)
        await repo.update_daily_stats("2025-06-01", -2.0, False)
        await repo.update_daily_stats("2025-06-01", 3.0, True)

        stats = await repo.get_daily_stats("2025-06-01")
        assert stats is not None
        assert stats.total_trades == 3
        assert stats.winning_trades == 2
        assert stats.total_pnl_usdt == pytest.approx(6.0)

    @pytest.mark.asyncio
    async def test_losing_trade_not_counted_as_win(self, repo: TradeRepository) -> None:
        await repo.update_daily_stats("2025-06-01", -1.0, False)
        stats = await repo.get_daily_stats("2025-06-01")
        assert stats is not None
        assert stats.winning_trades == 0

    @pytest.mark.asyncio
    async def test_separate_dates_are_independent(self, repo: TradeRepository) -> None:
        await repo.update_daily_stats("2025-06-01", 10.0, True)
        await repo.update_daily_stats("2025-06-02", -3.0, False)

        stats_day1 = await repo.get_daily_stats("2025-06-01")
        stats_day2 = await repo.get_daily_stats("2025-06-02")

        assert stats_day1 is not None
        assert stats_day1.total_pnl_usdt == 10.0
        assert stats_day1.total_trades == 1

        assert stats_day2 is not None
        assert stats_day2.total_pnl_usdt == -3.0
        assert stats_day2.total_trades == 1


# ---------------------------------------------------------------------------
# mark_daily_halted
# ---------------------------------------------------------------------------


class TestMarkDailyHalted:
    """Tests for persisting daily halt state."""

    @pytest.mark.asyncio
    async def test_marks_existing_daily_stats_halted(self, repo: TradeRepository) -> None:
        await repo.update_daily_stats("2025-06-01", 5.0, True)

        await repo.mark_daily_halted("2025-06-01")

        stats = await repo.get_daily_stats("2025-06-01")
        assert stats is not None
        assert stats.halted is True
        assert stats.total_trades == 1
        assert stats.total_pnl_usdt == 5.0

    @pytest.mark.asyncio
    async def test_creates_halted_daily_stats_row(self, repo: TradeRepository) -> None:
        await repo.mark_daily_halted("2025-06-01")

        stats = await repo.get_daily_stats("2025-06-01")
        assert stats is not None
        assert stats.halted is True
        assert stats.total_trades == 0


# ---------------------------------------------------------------------------
# get_daily_stats
# ---------------------------------------------------------------------------


class TestGetDailyStats:
    """Tests for TradeRepository.get_daily_stats."""

    @pytest.mark.asyncio
    async def test_returns_none_for_nonexistent_date(self, repo: TradeRepository) -> None:
        result = await repo.get_daily_stats("2099-01-01")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_correct_dataclass(self, repo: TradeRepository) -> None:
        await repo.update_daily_stats("2025-06-01", 7.5, True)
        stats = await repo.get_daily_stats("2025-06-01")
        assert isinstance(stats, DailyStats)
        assert stats.date == "2025-06-01"
        assert stats.halted is False
        assert stats.max_drawdown_pct == 0.0

    @pytest.mark.asyncio
    async def test_default_starting_balance_is_zero(self, repo: TradeRepository) -> None:
        await repo.update_daily_stats("2025-06-01", 1.0, True)
        stats = await repo.get_daily_stats("2025-06-01")
        assert stats is not None
        assert stats.starting_balance == 0.0
