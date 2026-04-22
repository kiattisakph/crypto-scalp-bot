"""Core data model dataclasses for crypto-scalp-bot.

Defines the shared data structures used across all components:
ticker data, signals, positions, trade records, risk results, and daily stats.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from core.enums import ExitReason, SignalDirection


@dataclass
class TickerData:
    """Real-time ticker snapshot for a single symbol.

    Attributes:
        symbol: The trading pair symbol (e.g. "SOLUSDT").
        price_change_pct: 24-hour price change percentage.
        last_price: Latest traded price.
        quote_volume: 24-hour quote volume in USDT.
    """

    symbol: str
    price_change_pct: float
    last_price: float
    quote_volume: float


@dataclass
class OrderUpdate:
    """Exchange order update from the Binance futures user-data stream."""

    symbol: str
    order_id: int
    client_order_id: str
    side: str
    order_type: str
    status: str
    execution_type: str
    avg_price: float
    last_fill_price: float
    last_fill_qty: float
    cumulative_filled_qty: float
    realized_pnl_usdt: float
    reduce_only: bool
    close_position: bool
    stop_price: float
    maker_type: str = ""


@dataclass
class Signal:
    """Entry signal produced by the SignalEngine.

    Attributes:
        direction: LONG or SHORT.
        confidence: Confidence score for the signal (0–1).
        indicators: Snapshot of all indicator values at signal time.
    """

    direction: SignalDirection
    confidence: float
    indicators: dict


@dataclass
class Position:
    """In-memory representation of an open position.

    Attributes:
        symbol: The trading pair symbol.
        side: LONG or SHORT direction.
        entry_price: Price at which the position was opened.
        quantity: Current remaining quantity.
        original_quantity: Original quantity at entry (used for TP2 ratio).
        leverage: Leverage multiplier applied to the position.
        tp1_price: Take-profit level 1 price.
        tp2_price: Take-profit level 2 price.
        tp3_price: Take-profit level 3 price.
        sl_price: Stop-loss price.
        tp1_hit: Whether TP1 has been triggered.
        tp2_hit: Whether TP2 has been triggered.
        trailing_active: Whether the trailing stop is active (after TP3).
        trailing_price: Current trailing stop trigger price.
        stop_order_id: Exchange-side stop-loss order ID (0 until placed).
        realized_pnl_usdt: PnL already realized by partial exits.
        opened_at: UTC datetime when the position was opened.
        trade_id: Database trade record ID (0 until persisted).
    """

    symbol: str
    side: SignalDirection
    entry_price: float
    quantity: float
    original_quantity: float
    leverage: int
    tp1_price: float
    tp2_price: float
    tp3_price: float
    sl_price: float
    tp1_hit: bool = False
    tp2_hit: bool = False
    trailing_active: bool = False
    trailing_price: float = 0.0
    stop_order_id: int = 0
    realized_pnl_usdt: float = 0.0
    opened_at: datetime = field(default_factory=datetime.utcnow)
    trade_id: int = 0


@dataclass
class TradeRecord:
    """Record inserted into the database when a position is opened.

    Attributes:
        symbol: The trading pair symbol.
        side: "LONG" or "SHORT".
        entry_price: Price at which the position was opened.
        quantity: Position quantity.
        leverage: Leverage multiplier.
        entry_at: UTC datetime of entry.
        signal_snapshot: JSON string of indicator values at signal time.
        status: "OPEN" or "CLOSED".
    """

    symbol: str
    side: str
    entry_price: float
    quantity: float
    leverage: int
    entry_at: datetime
    signal_snapshot: str
    status: str = "OPEN"


@dataclass
class OpenTradeRecord:
    """Open trade row loaded from the database during startup recovery."""

    id: int
    symbol: str
    side: str
    entry_price: float
    quantity: float
    leverage: int
    entry_at: datetime
    signal_snapshot: str


@dataclass
class ExitData:
    """Data captured when a position is closed.

    Attributes:
        exit_price: Price at which the position was closed.
        pnl_usdt: Realized profit/loss in USDT.
        pnl_pct: Realized profit/loss as a percentage.
        exit_reason: Why the position was closed (TP1, SL, TIME, etc.).
        exit_at: UTC datetime of exit.
    """

    exit_price: float
    pnl_usdt: float
    pnl_pct: float
    exit_reason: ExitReason
    exit_at: datetime


@dataclass
class TradeResult:
    """Summary of a completed trade, emitted on position close.

    Attributes:
        trade_id: Database trade record ID.
        symbol: The trading pair symbol.
        side: "LONG" or "SHORT".
        entry_price: Price at entry.
        exit_price: Price at exit.
        pnl_usdt: Realized profit/loss in USDT.
        pnl_pct: Realized profit/loss as a percentage.
        exit_reason: Why the position was closed.
    """

    trade_id: int
    symbol: str
    side: str
    entry_price: float
    exit_price: float
    pnl_usdt: float
    pnl_pct: float
    exit_reason: ExitReason


@dataclass
class RiskCheckResult:
    """Result of a RiskGuard pre-trade check.

    Attributes:
        approved: Whether the trade is approved.
        position_size: Calculated position size (0.0 if rejected).
        reject_reason: Human-readable reason for rejection (empty if approved).
    """

    approved: bool
    position_size: float = 0.0
    reject_reason: str = ""


@dataclass
class DailyStats:
    """Aggregated statistics for a single trading day.

    Attributes:
        date: Date string in YYYY-MM-DD format.
        starting_balance: Account balance at the start of the day.
        total_trades: Total number of trades executed.
        winning_trades: Number of trades with positive PnL.
        total_pnl_usdt: Cumulative realized PnL in USDT.
        max_drawdown_pct: Maximum drawdown percentage during the day.
        halted: Whether the bot was halted due to risk limits.
    """

    date: str
    starting_balance: float
    total_trades: int
    winning_trades: int
    total_pnl_usdt: float
    max_drawdown_pct: float
    halted: bool
