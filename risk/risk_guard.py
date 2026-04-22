"""Portfolio-level risk enforcement for crypto-scalp-bot.

Enforces daily loss limits, session drawdown limits, concurrent position
caps, and free-margin requirements before any new trade is opened.
Calculates position size using the mandatory formula from risk rules.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Awaitable, Callable

from loguru import logger

from core.config import ExitConfig, RiskConfig
from core.models import RiskCheckResult
from notification.telegram_alert import TelegramAlert
from storage.trade_repository import TradeRepository
from utils.time_utils import utc_now


class RiskGuard:
    """Enforces portfolio-level risk limits and halt logic.

    All threshold values are read from the provided ``RiskConfig`` and
    ``ExitConfig`` objects — no hardcoded magic numbers.

    Args:
        risk_config: Portfolio-level risk parameters.
        exit_config: Exit / stop-loss parameters (needed for ``sl_pct``).
        trade_repo: Repository used to load daily stats on startup.
        telegram: Alert sender for halt notifications.
    """

    def __init__(
        self,
        risk_config: RiskConfig,
        exit_config: ExitConfig,
        trade_repo: TradeRepository,
        telegram: TelegramAlert,
    ) -> None:
        self._risk_config = risk_config
        self._exit_config = exit_config
        self._trade_repo = trade_repo
        self._telegram = telegram

        # Mutable session state
        self._daily_loss_usdt: float = 0.0
        self._session_peak_balance: float = 0.0
        self._session_drawdown_pct: float = 0.0
        self._halted: bool = False

        # Kelly Criterion rolling window of recent trade PnLs.
        window = risk_config.kelly_window if risk_config.kelly_enabled else 0
        self._kelly_pnls: deque[float] = deque(maxlen=window)

        # Async callback to flatten all open positions on halt.
        # Wired by BotEngine — RiskGuard never imports execution layer.
        self.on_flatten_all: Callable[[], Awaitable[None]] | None = None

    # ------------------------------------------------------------------
    # Daily state loading
    # ------------------------------------------------------------------

    async def load_daily_state(self) -> None:
        """Load the current day's accumulated loss from the database.

        Called once at startup so the RiskGuard resumes with the correct
        daily-loss figure if the bot is restarted mid-day.
        """
        today = utc_now().strftime("%Y-%m-%d")
        stats = await self._trade_repo.get_daily_stats(today)
        self._daily_loss_usdt = await self._trade_repo.get_realized_loss_for_date(today)

        if stats is not None:
            self._halted = stats.halted
            logger.info(
                "risk_guard | Loaded daily state for {date} | "
                "daily_loss_usdt={loss:.4f} halted={halted}",
                date=today,
                loss=self._daily_loss_usdt,
                halted=self._halted,
            )
        else:
            self._halted = False
            logger.info(
                "risk_guard | No daily stats for {date}, loaded realized loss={loss:.4f}",
                date=today,
                loss=self._daily_loss_usdt,
            )

    # ------------------------------------------------------------------
    # Kelly Criterion
    # ------------------------------------------------------------------

    def _calc_kelly_pct(self) -> float | None:
        """Calculate the Kelly-optimal risk percentage.

        Uses the rolling window of recent trade PnLs to estimate:
        - Win rate (p)
        - Payoff ratio (b = avg_win / avg_loss)
        - Kelly fraction: f* = (bp - q) / b

        Returns the fractional Kelly risk percentage, or ``None`` when
        there is insufficient data or the edge is non-positive.
        """
        if not self._risk_config.kelly_enabled:
            return None
        if len(self._kelly_pnls) < self._risk_config.kelly_min_trades:
            return None

        wins = [p for p in self._kelly_pnls if p > 0]
        losses = [p for p in self._kelly_pnls if p <= 0]

        if not wins or not losses:
            return None

        win_rate = len(wins) / len(self._kelly_pnls)
        loss_rate = 1.0 - win_rate
        avg_win = sum(wins) / len(wins)
        avg_loss = abs(sum(losses) / len(losses))

        if avg_loss == 0:
            return None

        payoff_ratio = avg_win / avg_loss  # b in Kelly formula

        # Full Kelly: f* = (bp - q) / b
        kelly_full = (payoff_ratio * win_rate - loss_rate) / payoff_ratio

        if kelly_full <= 0:
            # No edge — don't trade
            logger.warning(
                "risk_guard | Kelly edge non-positive | "
                "win_rate={wr:.2f} payoff={pr:.2f} kelly={k:.4f}",
                wr=win_rate,
                pr=payoff_ratio,
                k=kelly_full,
            )
            return None

        # Apply fractional Kelly for reduced variance
        kelly_fractional = kelly_full * self._risk_config.kelly_fraction

        # Convert to percentage of balance and apply safety cap
        kelly_pct = min(kelly_fractional * 100, self._risk_config.kelly_max_pct)

        logger.info(
            "risk_guard | Kelly calculated | win_rate={wr:.2f} payoff={pr:.2f} "
            "kelly_full={kf:.4f} kelly_frac={kc:.4f} risk_pct={pct:.2f}%",
            wr=win_rate,
            pr=payoff_ratio,
            kf=kelly_full,
            kc=kelly_fractional,
            pct=kelly_pct,
        )
        return kelly_pct

    # ------------------------------------------------------------------
    # Pre-trade risk check
    # ------------------------------------------------------------------

    def check_trade(
        self,
        entry_price: float,
        balance: float,
        open_position_count: int = 0,
        free_margin_pct: float = 100.0,
        atr_value: float | None = None,
        confidence: float = 1.0,
    ) -> RiskCheckResult:
        """Check all risk conditions and calculate position size.

        All four conditions must pass for the trade to be approved:
        1. Daily loss < ``max_daily_loss_pct``
        2. Session drawdown < ``max_drawdown_pct``
        3. Open positions < ``max_concurrent_positions``
        4. Free margin >= ``min_free_margin_pct``

        Position size formula:
            risk_amount  = balance * risk_per_trade_pct / 100
            sl_distance  = ATR × atr_sl_mult   (ATR mode)
                         = entry_price × sl_pct / 100  (fixed mode)
            position_size = risk_amount / (leverage × sl_distance)

        When ``confidence_scaling_enabled`` is true, the risk percentage
        is multiplied by a confidence factor:
            confidence_factor = max(confidence_min_pct, confidence ** confidence_exponent)
            effective_risk_pct = base_risk_pct × confidence_factor

        Args:
            entry_price: Intended entry price for the trade.
            balance: Current account balance in USDT.
            open_position_count: Number of currently open positions.
            free_margin_pct: Percentage of balance available as free margin.
            atr_value: Average True Range at entry time (optional).
                When provided and ``atr_mode`` is enabled, SL distance
                is calculated as ``atr_value × atr_sl_mult`` instead of
                a fixed percentage.
            confidence: Signal confidence in (0, 1]. Used for
                confidence-based risk scaling when enabled.

        Returns:
            A :class:`RiskCheckResult` — approved with ``position_size``
            or rejected with a human-readable ``reject_reason``.
        """
        self.set_session_peak_balance(balance)

        # --- Condition 1: daily loss ---
        daily_loss_pct = abs(self._daily_loss_usdt) / balance * 100 if balance > 0 else 0.0
        if daily_loss_pct >= self._risk_config.max_daily_loss_pct:
            reason = (
                f"daily_loss {daily_loss_pct:.2f}% >= "
                f"max_daily_loss_pct {self._risk_config.max_daily_loss_pct}%"
            )
            logger.warning("risk_guard | Trade rejected: {reason}", reason=reason)
            return RiskCheckResult(approved=False, reject_reason=reason)

        # --- Condition 2: session drawdown ---
        if self._session_drawdown_pct >= self._risk_config.max_drawdown_pct:
            reason = (
                f"session_drawdown {self._session_drawdown_pct:.2f}% >= "
                f"max_drawdown_pct {self._risk_config.max_drawdown_pct}%"
            )
            logger.warning("risk_guard | Trade rejected: {reason}", reason=reason)
            return RiskCheckResult(approved=False, reject_reason=reason)

        # --- Condition 3: concurrent positions ---
        if open_position_count >= self._risk_config.max_concurrent_positions:
            reason = (
                f"open_positions {open_position_count} >= "
                f"max_concurrent_positions {self._risk_config.max_concurrent_positions}"
            )
            logger.warning("risk_guard | Trade rejected: {reason}", reason=reason)
            return RiskCheckResult(approved=False, reject_reason=reason)

        # --- Condition 4: free margin ---
        if free_margin_pct < self._risk_config.min_free_margin_pct:
            reason = (
                f"free_margin_pct {free_margin_pct:.2f}% < "
                f"min_free_margin_pct {self._risk_config.min_free_margin_pct}%"
            )
            logger.warning("risk_guard | Trade rejected: {reason}", reason=reason)
            return RiskCheckResult(approved=False, reject_reason=reason)

        # --- Halted check (covers both daily loss and session drawdown) ---
        if self._halted:
            reason = "bot is halted due to risk limit breach"
            logger.warning("risk_guard | Trade rejected: {reason}", reason=reason)
            return RiskCheckResult(approved=False, reject_reason=reason)

        # --- Position sizing ---
        # Risk per trade: dollar amount we're willing to lose if SL hits.
        # Use Kelly Criterion when enabled and sufficient data exists.
        kelly_pct = self._calc_kelly_pct()
        if kelly_pct is not None:
            risk_pct = kelly_pct
            logger.info(
                "risk_guard | Using Kelly risk_pct={pct:.2f}% (base={base}%)",
                pct=kelly_pct,
                base=self._risk_config.risk_per_trade_pct,
            )
        else:
            risk_pct = self._risk_config.risk_per_trade_pct

        # Confidence-based volatility adjustment.
        if self._risk_config.confidence_scaling_enabled:
            exp = self._risk_config.confidence_exponent
            min_factor = self._risk_config.confidence_min_pct
            confidence_factor = max(min_factor, confidence ** exp)
            risk_pct *= confidence_factor
            logger.debug(
                "risk_guard | Confidence scaling applied | conf={c:.3f} "
                "factor={f:.3f} risk_pct={pct:.2f}%",
                c=confidence,
                f=confidence_factor,
                pct=risk_pct,
            )

        risk_amount = balance * risk_pct / 100

        if atr_value is not None and self._exit_config.atr_mode:
            sl_distance = atr_value * self._exit_config.atr_sl_mult
        else:
            sl_distance = entry_price * self._exit_config.sl_pct / 100

        # Position size so that loss at SL equals risk_amount.
        # With leverage, notional = position_size * entry_price * leverage,
        # and loss = notional * (sl_distance / entry_price)
        #          = position_size * leverage * sl_distance.
        # Therefore: position_size = risk_amount / (leverage * sl_distance).
        leverage = self._risk_config.leverage
        position_size = risk_amount / (leverage * sl_distance)

        if position_size <= 0:
            reason = "calculated position size too small"
            logger.warning("risk_guard | Trade rejected: {reason}", reason=reason)
            return RiskCheckResult(approved=False, reject_reason=reason)

        logger.info(
            "risk_guard | Trade approved | size={size:.6f} "
            "risk_amount={risk:.4f} sl_distance={sl:.4f}",
            size=position_size,
            risk=risk_amount,
            sl=sl_distance,
        )
        return RiskCheckResult(approved=True, position_size=position_size)

    def set_session_peak_balance(self, balance: float) -> None:
        """Initialise or raise the session peak balance used for drawdown."""
        if balance > self._session_peak_balance:
            self._session_peak_balance = balance

    # ------------------------------------------------------------------
    # PnL recording
    # ------------------------------------------------------------------

    def record_pnl(self, pnl_usdt: float, balance: float = 0.0) -> None:
        """Record realized PnL and update daily loss / session drawdown.

        Args:
            pnl_usdt: Realized profit (positive) or loss (negative) in USDT.
            balance: Current account balance used for drawdown calculation.
        """
        # Track trade PnL for Kelly Criterion rolling window.
        if self._risk_config.kelly_enabled:
            self._kelly_pnls.append(pnl_usdt)

        # Update daily cumulative loss (only losses count)
        if pnl_usdt < 0:
            self._daily_loss_usdt += pnl_usdt  # pnl_usdt is negative

        # Update session drawdown tracking
        if balance > 0:
            if balance > self._session_peak_balance:
                self._session_peak_balance = balance
            if self._session_peak_balance > 0:
                drawdown = (
                    (self._session_peak_balance - balance)
                    / self._session_peak_balance
                    * 100
                )
                if drawdown > self._session_drawdown_pct:
                    self._session_drawdown_pct = drawdown

        logger.debug(
            "risk_guard | Recorded PnL {pnl:.4f} USDT | "
            "daily_loss={loss:.4f} session_dd={dd:.2f}%",
            pnl=pnl_usdt,
            loss=self._daily_loss_usdt,
            dd=self._session_drawdown_pct,
        )

    # ------------------------------------------------------------------
    # Halt logic
    # ------------------------------------------------------------------

    def is_halted(self) -> bool:
        """Return whether the bot is in a halted state.

        Returns:
            ``True`` if daily loss or session drawdown limits have been
            exceeded, ``False`` otherwise.
        """
        return self._halted

    async def _trigger_halt(self, reason: str, value: float) -> None:
        """Enter the halted state, send a Telegram alert, and flatten all positions.

        When a risk limit is breached the bot must immediately:
        1. Set the halted flag (blocks new entries).
        2. Persist the halt state to the database.
        3. Send a Telegram ``⛔ HALT`` alert.
        4. Force-close every open position at market price via the
           ``on_flatten_all`` callback (wired by BotEngine).

        Args:
            reason: Human-readable description of the breached limit.
            value: The current value that triggered the halt.
        """
        self._halted = True
        logger.critical(
            "risk_guard | ⛔ HALT triggered | {reason} = {value:.2f}",
            reason=reason,
            value=value,
        )
        today = utc_now().strftime("%Y-%m-%d")
        try:
            await self._trade_repo.mark_daily_halted(today)
        except Exception:
            logger.exception(
                "risk_guard | Failed to persist halt state for {date}",
                date=today,
            )
        await self._telegram.notify_risk_halt(reason, value)

        # Flatten all open positions immediately
        if self.on_flatten_all is not None:
            try:
                await self.on_flatten_all()
            except Exception:
                logger.exception(
                    "risk_guard | Failed to flatten positions on halt"
                )

    async def check_halt_conditions(self, balance: float) -> None:
        """Evaluate halt conditions after PnL recording.

        Should be called after :meth:`record_pnl` to check whether the
        daily loss or session drawdown thresholds have been breached.

        Args:
            balance: Current account balance in USDT.
        """
        if self._halted:
            return

        # Daily loss halt
        daily_loss_pct = abs(self._daily_loss_usdt) / balance * 100 if balance > 0 else 0.0
        if daily_loss_pct >= self._risk_config.max_daily_loss_pct:
            await self._trigger_halt("daily_loss", daily_loss_pct)
            return

        # Session drawdown halt
        if self._session_drawdown_pct >= self._risk_config.max_drawdown_pct:
            await self._trigger_halt("session_drawdown", self._session_drawdown_pct)
