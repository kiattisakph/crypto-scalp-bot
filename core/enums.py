"""Enum definitions for crypto-scalp-bot."""
from __future__ import annotations

from enum import Enum


class SignalDirection(str, Enum):
    """Direction of a trading signal."""

    LONG = "LONG"
    SHORT = "SHORT"


class OrderSide(str, Enum):
    """Side of an order on the exchange."""

    BUY = "BUY"
    SELL = "SELL"


class ExitReason(str, Enum):
    """Reason a position was closed."""

    TP1 = "TP1"
    TP2 = "TP2"
    TP3 = "TP3"
    SL = "SL"
    TIME = "TIME"
    REVERSAL = "REVERSAL"
    HALT = "HALT"
    RECONCILED = "RECONCILED"
    LIQUIDATION = "LIQUIDATION"
    EXTERNAL = "EXTERNAL"


class PositionStatus(str, Enum):
    """Status of a tracked position."""

    OPEN = "OPEN"
    CLOSED = "CLOSED"
