"""Property-based tests for RiskGuard risk enforcement."""
from __future__ import annotations

import math
from unittest.mock import AsyncMock, MagicMock

import pytest
from hypothesis import given, settings, strategies as st

from core.config import ExitConfig, RiskConfig
from risk.risk_guard import RiskGuard


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_guard(risk_per_trade_pct: float, sl_pct: float) -> RiskGuard:
    """Create a RiskGuard with the given risk/exit config and mock deps."""
    risk_config = RiskConfig(
        risk_per_trade_pct=risk_per_trade_pct,
        leverage=5,
        max_concurrent_positions=3,
        max_daily_loss_pct=3.0,
        max_drawdown_pct=5.0,
        min_free_margin_pct=30.0,
    )
    exit_config = ExitConfig(sl_pct=sl_pct)

    trade_repo = MagicMock()
    trade_repo.get_daily_stats = AsyncMock(return_value=None)
    trade_repo.get_realized_loss_for_date = AsyncMock(return_value=0.0)
    trade_repo.mark_daily_halted = AsyncMock()

    telegram = MagicMock()
    telegram.notify_risk_halt = AsyncMock()
    telegram.send = AsyncMock()

    return RiskGuard(
        risk_config=risk_config,
        exit_config=exit_config,
        trade_repo=trade_repo,
        telegram=telegram,
    )


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_positive_balance = st.floats(min_value=1.0, max_value=1e9, allow_nan=False, allow_infinity=False)
_positive_entry_price = st.floats(min_value=0.0001, max_value=1e9, allow_nan=False, allow_infinity=False)
_positive_risk_pct = st.floats(min_value=0.01, max_value=50.0, allow_nan=False, allow_infinity=False)
_positive_sl_pct = st.floats(min_value=0.01, max_value=50.0, allow_nan=False, allow_infinity=False)


# ---------------------------------------------------------------------------
# Property 17: Position size formula
# ---------------------------------------------------------------------------
# Feature: crypto-scalp-bot, Property 17: Position size formula


@settings(max_examples=100)
@given(
    balance=_positive_balance,
    entry_price=_positive_entry_price,
    risk_per_trade_pct=_positive_risk_pct,
    sl_pct=_positive_sl_pct,
)
def test_position_size_matches_formula(
    balance: float,
    entry_price: float,
    risk_per_trade_pct: float,
    sl_pct: float,
) -> None:
    """Position size equals (balance × risk_per_trade_pct / 100) /
    (leverage × entry_price × sl_pct / 100) and is always a positive
    finite number.

    **Validates: Requirements 9.1**
    """
    guard = _make_guard(risk_per_trade_pct=risk_per_trade_pct, sl_pct=sl_pct)

    result = guard.check_trade(entry_price=entry_price, balance=balance)

    # Trade must be approved (no risk limits breached in a fresh guard)
    assert result.approved is True, f"Trade unexpectedly rejected: {result.reject_reason}"

    # Verify the exact formula with leverage
    expected_risk_amount = balance * risk_per_trade_pct / 100
    expected_sl_distance = entry_price * sl_pct / 100
    leverage = 5  # matches _make_guard default
    expected_position_size = expected_risk_amount / (leverage * expected_sl_distance)

    assert result.position_size == expected_position_size, (
        f"position_size mismatch: got {result.position_size}, "
        f"expected {expected_position_size}"
    )

    # Result must be positive and finite
    assert result.position_size > 0, f"position_size must be positive, got {result.position_size}"
    assert math.isfinite(result.position_size), (
        f"position_size must be finite, got {result.position_size}"
    )


# ---------------------------------------------------------------------------
# Strategies for Property 18
# ---------------------------------------------------------------------------

# Risk config thresholds — keep ranges reasonable for meaningful tests
_max_daily_loss_pct = st.floats(min_value=0.5, max_value=20.0, allow_nan=False, allow_infinity=False)
_max_drawdown_pct = st.floats(min_value=0.5, max_value=20.0, allow_nan=False, allow_infinity=False)
_max_concurrent_positions = st.integers(min_value=1, max_value=20)
_min_free_margin_pct = st.floats(min_value=5.0, max_value=90.0, allow_nan=False, allow_infinity=False)

# Risk state values — can be within or exceeding limits
_daily_loss_pct_state = st.floats(min_value=0.0, max_value=30.0, allow_nan=False, allow_infinity=False)
_session_drawdown_state = st.floats(min_value=0.0, max_value=30.0, allow_nan=False, allow_infinity=False)
_open_position_count_state = st.integers(min_value=0, max_value=25)
_free_margin_pct_state = st.floats(min_value=0.0, max_value=100.0, allow_nan=False, allow_infinity=False)


def _make_guard_with_config(
    max_daily_loss_pct: float,
    max_drawdown_pct: float,
    max_concurrent_positions: int,
    min_free_margin_pct: float,
) -> RiskGuard:
    """Create a RiskGuard with fully configurable risk thresholds."""
    risk_config = RiskConfig(
        risk_per_trade_pct=1.0,
        leverage=5,
        max_concurrent_positions=max_concurrent_positions,
        max_daily_loss_pct=max_daily_loss_pct,
        max_drawdown_pct=max_drawdown_pct,
        min_free_margin_pct=min_free_margin_pct,
    )
    exit_config = ExitConfig(sl_pct=1.0)

    trade_repo = MagicMock()
    trade_repo.get_daily_stats = AsyncMock(return_value=None)
    trade_repo.get_realized_loss_for_date = AsyncMock(return_value=0.0)
    trade_repo.mark_daily_halted = AsyncMock()

    telegram = MagicMock()
    telegram.notify_risk_halt = AsyncMock()
    telegram.send = AsyncMock()

    return RiskGuard(
        risk_config=risk_config,
        exit_config=exit_config,
        trade_repo=trade_repo,
        telegram=telegram,
    )


# ---------------------------------------------------------------------------
# Property 18: Risk approval/rejection correctness
# ---------------------------------------------------------------------------
# Feature: crypto-scalp-bot, Property 18: Risk approval/rejection correctness


@settings(max_examples=100)
@given(
    max_daily_loss=_max_daily_loss_pct,
    max_drawdown=_max_drawdown_pct,
    max_positions=_max_concurrent_positions,
    min_free_margin=_min_free_margin_pct,
    daily_loss_pct=_daily_loss_pct_state,
    session_drawdown=_session_drawdown_state,
    open_positions=_open_position_count_state,
    free_margin_pct=_free_margin_pct_state,
)
def test_risk_approval_rejection_correctness(
    max_daily_loss: float,
    max_drawdown: float,
    max_positions: int,
    min_free_margin: float,
    daily_loss_pct: float,
    session_drawdown: float,
    open_positions: int,
    free_margin_pct: float,
) -> None:
    """Trade approved iff ALL four risk conditions pass; when rejected,
    reject_reason identifies the specific failing condition.

    The four conditions are:
    1. daily_loss < max_daily_loss_pct
    2. session_drawdown < max_drawdown_pct
    3. open_positions < max_concurrent_positions
    4. free_margin_pct >= min_free_margin_pct

    **Validates: Requirements 9.2, 9.3**
    """
    guard = _make_guard_with_config(
        max_daily_loss_pct=max_daily_loss,
        max_drawdown_pct=max_drawdown,
        max_concurrent_positions=max_positions,
        min_free_margin_pct=min_free_margin,
    )

    # Inject risk state into the guard.
    # daily_loss is stored as negative USDT and compared as:
    #   abs(_daily_loss_usdt) / balance * 100
    # We must compute the effective daily_loss_pct the same way the guard
    # does to avoid floating-point mismatches at boundary values.
    balance = 10_000.0
    daily_loss_usdt = -(daily_loss_pct * balance / 100.0)
    guard._daily_loss_usdt = daily_loss_usdt
    guard._session_drawdown_pct = session_drawdown

    # Recompute the effective daily_loss_pct as the guard sees it
    effective_daily_loss_pct = abs(daily_loss_usdt) / balance * 100

    entry_price = 100.0
    result = guard.check_trade(
        entry_price=entry_price,
        balance=balance,
        open_position_count=open_positions,
        free_margin_pct=free_margin_pct,
    )

    # Determine which conditions pass using the SAME comparisons as the guard
    cond_daily = effective_daily_loss_pct < max_daily_loss
    cond_drawdown = session_drawdown < max_drawdown
    cond_positions = open_positions < max_positions
    cond_margin = free_margin_pct >= min_free_margin

    all_pass = cond_daily and cond_drawdown and cond_positions and cond_margin

    if all_pass:
        assert result.approved is True, (
            f"Trade should be approved when all conditions pass. "
            f"daily_loss={effective_daily_loss_pct:.4f} < {max_daily_loss:.4f}, "
            f"drawdown={session_drawdown:.4f} < {max_drawdown:.4f}, "
            f"positions={open_positions} < {max_positions}, "
            f"margin={free_margin_pct:.4f} >= {min_free_margin:.4f}. "
            f"Got reject_reason={result.reject_reason!r}"
        )
        assert result.position_size > 0, "Approved trade must have positive position_size"
    else:
        assert result.approved is False, (
            f"Trade should be rejected when any condition fails. "
            f"daily_loss={effective_daily_loss_pct:.4f} vs {max_daily_loss:.4f}, "
            f"drawdown={session_drawdown:.4f} vs {max_drawdown:.4f}, "
            f"positions={open_positions} vs {max_positions}, "
            f"margin={free_margin_pct:.4f} vs {min_free_margin:.4f}"
        )
        assert result.reject_reason != "", "Rejected trade must have a non-empty reject_reason"

        # Verify reject_reason identifies the FIRST failing condition
        # (RiskGuard checks in order: daily_loss, drawdown, positions, margin)
        if not cond_daily:
            assert "daily_loss" in result.reject_reason, (
                f"reject_reason should mention 'daily_loss' but got: {result.reject_reason!r}"
            )
        elif not cond_drawdown:
            assert "session_drawdown" in result.reject_reason, (
                f"reject_reason should mention 'session_drawdown' but got: {result.reject_reason!r}"
            )
        elif not cond_positions:
            assert "open_positions" in result.reject_reason, (
                f"reject_reason should mention 'open_positions' but got: {result.reject_reason!r}"
            )
        elif not cond_margin:
            assert "free_margin_pct" in result.reject_reason, (
                f"reject_reason should mention 'free_margin_pct' but got: {result.reject_reason!r}"
            )


# ---------------------------------------------------------------------------
# Strategies for Property 19
# ---------------------------------------------------------------------------

# Thresholds — keep ranges reasonable
_halt_max_daily_loss_pct = st.floats(min_value=0.5, max_value=20.0, allow_nan=False, allow_infinity=False)
_halt_max_drawdown_pct = st.floats(min_value=0.5, max_value=20.0, allow_nan=False, allow_infinity=False)

# Values that EXCEED the threshold (used to trigger halt)
# We draw the excess separately and add it to the threshold to guarantee exceeding
_excess_pct = st.floats(min_value=0.01, max_value=30.0, allow_nan=False, allow_infinity=False)


# ---------------------------------------------------------------------------
# Property 19: Risk halt trigger
# ---------------------------------------------------------------------------
# Feature: crypto-scalp-bot, Property 19: Risk halt trigger


@pytest.mark.asyncio
@settings(max_examples=100)
@given(
    max_daily_loss=_halt_max_daily_loss_pct,
    excess=_excess_pct,
)
async def test_halt_triggered_by_daily_loss(
    max_daily_loss: float,
    excess: float,
) -> None:
    """When daily loss exceeds max_daily_loss_pct, check_halt_conditions
    triggers halt state and all subsequent trades are rejected.

    **Validates: Requirements 9.4, 9.5**
    """
    guard = _make_guard_with_config(
        max_daily_loss_pct=max_daily_loss,
        max_drawdown_pct=99.0,  # high so drawdown doesn't interfere
        max_concurrent_positions=10,
        min_free_margin_pct=1.0,
    )

    balance = 10_000.0
    # Set daily loss to exceed the threshold
    daily_loss_pct = max_daily_loss + excess
    guard._daily_loss_usdt = -(daily_loss_pct * balance / 100.0)

    # Guard should not be halted yet (halt requires check_halt_conditions call)
    assert guard.is_halted() is False

    # Trigger halt evaluation
    await guard.check_halt_conditions(balance)

    # Guard must now be halted
    assert guard.is_halted() is True

    # Telegram notify_risk_halt must have been called
    guard._telegram.notify_risk_halt.assert_awaited_once()
    call_args = guard._telegram.notify_risk_halt.call_args
    assert call_args[0][0] == "daily_loss"

    # All subsequent trades must be rejected
    result = guard.check_trade(
        entry_price=100.0,
        balance=balance,
        open_position_count=0,
        free_margin_pct=100.0,
    )
    assert result.approved is False
    assert "halted" in result.reject_reason.lower() or "daily_loss" in result.reject_reason.lower()


@pytest.mark.asyncio
@settings(max_examples=100)
@given(
    max_drawdown=_halt_max_drawdown_pct,
    excess=_excess_pct,
)
async def test_halt_triggered_by_session_drawdown(
    max_drawdown: float,
    excess: float,
) -> None:
    """When session drawdown exceeds max_drawdown_pct, check_halt_conditions
    triggers halt state and all subsequent trades are rejected.

    **Validates: Requirements 9.4, 9.5**
    """
    guard = _make_guard_with_config(
        max_daily_loss_pct=99.0,  # high so daily loss doesn't interfere
        max_drawdown_pct=max_drawdown,
        max_concurrent_positions=10,
        min_free_margin_pct=1.0,
    )

    balance = 10_000.0
    # Set session drawdown to exceed the threshold
    guard._session_drawdown_pct = max_drawdown + excess

    # Guard should not be halted yet
    assert guard.is_halted() is False

    # Trigger halt evaluation
    await guard.check_halt_conditions(balance)

    # Guard must now be halted
    assert guard.is_halted() is True

    # Telegram notify_risk_halt must have been called
    guard._telegram.notify_risk_halt.assert_awaited_once()
    call_args = guard._telegram.notify_risk_halt.call_args
    assert call_args[0][0] == "session_drawdown"

    # All subsequent trades must be rejected
    result = guard.check_trade(
        entry_price=100.0,
        balance=balance,
        open_position_count=0,
        free_margin_pct=100.0,
    )
    assert result.approved is False
    assert "halted" in result.reject_reason.lower() or "session_drawdown" in result.reject_reason.lower()
