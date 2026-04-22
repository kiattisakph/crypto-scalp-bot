"""SQLite connection management for crypto-scalp-bot.

Provides async database initialization, connection access, and teardown
using aiosqlite. Creates the ``trades`` and ``daily_stats`` tables on
first run if they do not already exist.
"""
from __future__ import annotations

from pathlib import Path

import aiosqlite
from loguru import logger

# ---------------------------------------------------------------------------
# SQL DDL — table creation
# ---------------------------------------------------------------------------

_CREATE_TRADES_TABLE = """
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER  PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT     NOT NULL,
    side            TEXT     NOT NULL,
    entry_price     REAL     NOT NULL,
    exit_price      REAL,
    quantity        REAL     NOT NULL,
    leverage        INTEGER  NOT NULL,
    pnl_usdt       REAL,
    pnl_pct        REAL,
    exit_reason     TEXT,
    entry_at        DATETIME NOT NULL,
    exit_at         DATETIME,
    status          TEXT     DEFAULT 'OPEN',
    signal_snapshot TEXT,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""

_CREATE_DAILY_STATS_TABLE = """
CREATE TABLE IF NOT EXISTS daily_stats (
    id               INTEGER  PRIMARY KEY AUTOINCREMENT,
    date             TEXT     NOT NULL UNIQUE,
    starting_balance REAL,
    ending_balance   REAL,
    total_trades     INTEGER  DEFAULT 0,
    winning_trades   INTEGER  DEFAULT 0,
    total_pnl_usdt   REAL     DEFAULT 0,
    max_drawdown_pct REAL     DEFAULT 0,
    halted           INTEGER  DEFAULT 0,
    created_at       DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""


# ---------------------------------------------------------------------------
# Database class
# ---------------------------------------------------------------------------


class Database:
    """Async SQLite database manager.

    Handles connection lifecycle and schema creation for the
    ``trades`` and ``daily_stats`` tables.

    Args:
        db_path: Filesystem path to the SQLite database file.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._connection: aiosqlite.Connection | None = None

    async def init(self) -> None:
        """Open the database connection and create tables if they don't exist.

        The parent directory for the database file is created automatically
        when it does not exist.

        Raises:
            aiosqlite.Error: If the database cannot be opened or tables
                cannot be created.
        """
        # Ensure the directory for the database file exists.
        db_dir = Path(self._db_path).parent
        db_dir.mkdir(parents=True, exist_ok=True)

        self._connection = await aiosqlite.connect(self._db_path)
        # Enable WAL mode for better concurrent read performance.
        await self._connection.execute("PRAGMA journal_mode=WAL;")

        await self._connection.execute(_CREATE_TRADES_TABLE)
        await self._connection.execute(_CREATE_DAILY_STATS_TABLE)
        await self._connection.commit()

        logger.info(
            "database | Initialised SQLite database at {path}",
            path=self._db_path,
        )

    async def get_connection(self) -> aiosqlite.Connection:
        """Return the active database connection.

        Returns:
            The open ``aiosqlite.Connection``.

        Raises:
            RuntimeError: If ``init()`` has not been called yet.
        """
        if self._connection is None:
            raise RuntimeError(
                "Database not initialised. Call `await database.init()` first."
            )
        return self._connection

    async def close(self) -> None:
        """Close the database connection.

        Safe to call multiple times; subsequent calls are no-ops.
        """
        if self._connection is not None:
            await self._connection.close()
            self._connection = None
            logger.info("database | Database connection closed")
