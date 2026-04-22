"""Property-based tests for TelegramAlert notification formatting."""
from __future__ import annotations

from hypothesis import given, settings, strategies as st

from core.enums import ExitReason, SignalDirection
from notification.telegram_alert import TelegramAlert


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_directions = st.sampled_from([SignalDirection.LONG, SignalDirection.SHORT])
_exit_reasons = st.sampled_from(list(ExitReason))

# Symbols: 3-10 uppercase letters followed by "USDT"
_symbols = st.from_regex(r"[A-Z]{3,10}USDT", fullmatch=True)

# Prices / quantities — positive finite floats
_positive_floats = st.floats(min_value=0.0001, max_value=1e9, allow_nan=False, allow_infinity=False)

# PnL can be negative, zero, or positive
_pnl_floats = st.floats(min_value=-1e9, max_value=1e9, allow_nan=False, allow_infinity=False)


# ---------------------------------------------------------------------------
# Property 20: Position opened notification completeness
# ---------------------------------------------------------------------------
# Feature: crypto-scalp-bot, Property 20: Position opened notification completeness

@settings(max_examples=100)
@given(
    symbol=_symbols,
    direction=_directions,
    entry_price=_positive_floats,
    quantity=_positive_floats,
    sl_price=_positive_floats,
    tp1_price=_positive_floats,
)
def test_position_opened_notification_contains_all_fields(
    symbol: str,
    direction: SignalDirection,
    entry_price: float,
    quantity: float,
    sl_price: float,
    tp1_price: float,
) -> None:
    """The position-opened message must contain symbol, direction, entry
    price, position size, stop loss price, and TP1 target price.

    **Validates: Requirements 11.2**
    """
    msg = TelegramAlert.format_position_opened(
        symbol=symbol,
        direction=direction,
        entry_price=entry_price,
        quantity=quantity,
        sl_price=sl_price,
        tp1_price=tp1_price,
    )

    # All required fields present
    assert symbol in msg, f"Symbol '{symbol}' missing from message"
    assert direction.value in msg, f"Direction '{direction.value}' missing from message"
    assert str(entry_price) in msg, f"Entry price '{entry_price}' missing from message"
    assert str(quantity) in msg, f"Quantity '{quantity}' missing from message"
    assert str(sl_price) in msg, f"SL price '{sl_price}' missing from message"
    assert str(tp1_price) in msg, f"TP1 price '{tp1_price}' missing from message"

    # Correct emoji based on direction
    if direction == SignalDirection.LONG:
        assert "📈" in msg
    else:
        assert "📉" in msg


# ---------------------------------------------------------------------------
# Property 21: Position closed notification completeness
# ---------------------------------------------------------------------------
# Feature: crypto-scalp-bot, Property 21: Position closed notification completeness

@settings(max_examples=100)
@given(
    symbol=_symbols,
    exit_reason=_exit_reasons,
    pnl_usdt=_pnl_floats,
)
def test_position_closed_notification_contains_all_fields(
    symbol: str,
    exit_reason: ExitReason,
    pnl_usdt: float,
) -> None:
    """The position-closed message must contain symbol, exit reason, and
    PnL in USDT.

    **Validates: Requirements 11.3**
    """
    msg = TelegramAlert.format_position_closed(
        symbol=symbol,
        exit_reason=exit_reason,
        pnl_usdt=pnl_usdt,
    )

    # All required fields present
    assert symbol in msg, f"Symbol '{symbol}' missing from message"
    assert exit_reason.value in msg, f"Exit reason '{exit_reason.value}' missing from message"
    assert str(pnl_usdt) in msg, f"PnL '{pnl_usdt}' missing from message"
    assert "USDT" in msg, "Currency label 'USDT' missing from message"

    # Correct emoji based on profit/loss
    if pnl_usdt >= 0:
        assert "💰" in msg
    else:
        assert "💸" in msg
