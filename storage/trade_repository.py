"""CRUD operations for trade history and daily statistics.

Provides async methods to insert/close trades and manage daily stats
in the SQLite database via the :class:`Database` connection manager.
"""
from __future__ import annotations

from datetime import datetime

import aiosqlite
from loguru import logger

from core.enums import ExitReason
from core.models import DailyStats, ExitData, OpenTradeRecord, TradeRecord
from storage.database import Database


class TradeRepository:
    """Repository for persisting trade records and daily statistics.

    Args:
        database: An initialised :class:`Database` instance.
    """

    def __init__(self, database: Database) -> None:
        self._database = database

    async def _conn(self) -> aiosqlite.Connection:
        """Shortcut to obtain the active database connection."""
        return await self._database.get_connection()

    async def insert_trade(self, trade: TradeRecord) -> int:
        """Insert a new open trade record.

        Args:
            trade: The trade data to persist.

        Returns:
            The auto-generated row ID of the inserted trade.
        """
        conn = await self._conn()
        cursor = await conn.execute(
            """
            INSERT INTO trades (
                symbol, side, entry_price, quantity, leverage,
                entry_at, signal_snapshot, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade.symbol,
                trade.side,
                trade.entry_price,
                trade.quantity,
                trade.leverage,
                trade.entry_at.isoformat(),
                trade.signal_snapshot,
                trade.status,
            ),
        )
        await conn.commit()
        trade_id = cursor.lastrowid
        logger.info(
            "trade_repository | Inserted trade {id} for {symbol} ({side})",
            id=trade_id,
            symbol=trade.symbol,
            side=trade.side,
        )
        return trade_id  # type: ignore[return-value]

    async def close_trade(self, trade_id: int, exit_data: ExitData) -> None:
        """Update an open trade with exit information and mark it CLOSED.

        Args:
            trade_id: The database ID of the trade to close.
            exit_data: Exit details (price, PnL, reason, timestamp).

        Raises:
            ValueError: If no trade with the given ID exists.
        """
        conn = await self._conn()
        cursor = await conn.execute(
            """
            UPDATE trades
               SET exit_price  = ?,
                   pnl_usdt    = ?,
                   pnl_pct     = ?,
                   exit_reason  = ?,
                   exit_at      = ?,
                   status       = 'CLOSED'
             WHERE id = ?
            """,
            (
                exit_data.exit_price,
                exit_data.pnl_usdt,
                exit_data.pnl_pct,
                exit_data.exit_reason.value,
                exit_data.exit_at.isoformat(),
                trade_id,
            ),
        )
        await conn.commit()

        if cursor.rowcount == 0:
            raise ValueError(f"No trade found with id={trade_id}")

        logger.info(
            "trade_repository | Closed trade {id} | reason={reason} pnl={pnl:.4f} USDT",
            id=trade_id,
            reason=exit_data.exit_reason.value,
            pnl=exit_data.pnl_usdt,
        )

    async def get_open_trades(self) -> list[OpenTradeRecord]:
        """Return all trades still marked OPEN in the database."""
        conn = await self._conn()
        cursor = await conn.execute(
            """
            SELECT id, symbol, side, entry_price, quantity, leverage,
                   entry_at, signal_snapshot
              FROM trades
             WHERE status = 'OPEN'
             ORDER BY entry_at ASC, id ASC
            """
        )
        rows = await cursor.fetchall()

        result: list[OpenTradeRecord] = []
        for row in rows:
            entry_at = datetime.fromisoformat(row[6])
            result.append(
                OpenTradeRecord(
                    id=row[0],
                    symbol=row[1],
                    side=row[2],
                    entry_price=row[3],
                    quantity=row[4],
                    leverage=row[5],
                    entry_at=entry_at,
                    signal_snapshot=row[7] or "",
                )
            )

        return result

    async def get_realized_loss_for_date(self, date: str) -> float:
        """Return cumulative realized losses for trades closed on *date*.

        Profitable trades are intentionally ignored so daily loss recovery
        cannot be reduced by later wins.
        """
        conn = await self._conn()
        cursor = await conn.execute(
            """
            SELECT COALESCE(SUM(pnl_usdt), 0)
              FROM trades
             WHERE status = 'CLOSED'
               AND pnl_usdt < 0
               AND substr(exit_at, 1, 10) = ?
            """,
            (date,),
        )
        row = await cursor.fetchone()
        return float(row[0] if row is not None and row[0] is not None else 0.0)

    async def mark_daily_halted(self, date: str) -> None:
        """Persist that trading is halted for the given date."""
        conn = await self._conn()
        await conn.execute(
            """
            INSERT INTO daily_stats (date, halted)
            VALUES (?, 1)
            ON CONFLICT(date) DO UPDATE SET halted = 1
            """,
            (date,),
        )
        await conn.commit()

    async def update_daily_stats(
        self, date: str, pnl: float, is_win: bool
    ) -> None:
        """Atomically upsert daily statistics for the given date.

        Uses a single ``INSERT … ON CONFLICT DO UPDATE`` with relative
        increments (``total_trades + 1``) so the operation is atomic at
        the SQLite level.  This prevents lost updates when multiple
        position-close callbacks interleave at ``await`` points.

        Args:
            date: Date string in ``YYYY-MM-DD`` format.
            pnl: Realized PnL in USDT for the trade being recorded.
            is_win: Whether the trade was profitable.
        """
        conn = await self._conn()
        win_increment = 1 if is_win else 0

        await conn.execute(
            """
            INSERT INTO daily_stats (date, total_trades, winning_trades, total_pnl_usdt)
            VALUES (?, 1, ?, ?)
            ON CONFLICT(date) DO UPDATE
               SET total_trades   = total_trades   + 1,
                   winning_trades = winning_trades  + ?,
                   total_pnl_usdt = total_pnl_usdt + ?
            """,
            (date, win_increment, pnl, win_increment, pnl),
        )
        await conn.commit()
        logger.debug(
            "trade_repository | Updated daily stats for {date} | pnl={pnl:.4f}",
            date=date,
            pnl=pnl,
        )

    async def get_daily_stats(self, date: str) -> DailyStats | None:
        """Retrieve daily statistics for the given date.

        Args:
            date: Date string in ``YYYY-MM-DD`` format.

        Returns:
            A :class:`DailyStats` instance, or ``None`` if no record exists
            for the requested date.
        """
        conn = await self._conn()
        cursor = await conn.execute(
            """
            SELECT date, starting_balance, total_trades, winning_trades,
                   total_pnl_usdt, max_drawdown_pct, halted
              FROM daily_stats
             WHERE date = ?
            """,
            (date,),
        )
        row = await cursor.fetchone()

        if row is None:
            return None

        return DailyStats(
            date=row[0],
            starting_balance=row[1] if row[1] is not None else 0.0,
            total_trades=row[2],
            winning_trades=row[3],
            total_pnl_usdt=row[4],
            max_drawdown_pct=row[5],
            halted=bool(row[6]),
        )
