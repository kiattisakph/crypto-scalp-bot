"""Core backtest engine — Filtered Replay.

Walks through historical candles chronologically, reconstructs the dynamic
watchlist at each refresh interval, evaluates signals for watchlist symbols,
and simulates trade execution.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
from loguru import logger

from core.config import AppConfig
from core.enums import ExitReason, SignalDirection
from core.models import Signal

from backtest.data_fetcher import DataFetcher


@dataclass
class BacktestTrade:
    """A completed trade record from the backtest."""

    symbol: str
    side: str
    entry_price: float
    exit_price: float
    quantity: float
    leverage: int
    pnl_usdt: float
    pnl_pct: float
    exit_reason: str
    entry_at: datetime
    exit_at: datetime
    indicators: dict = field(default_factory=dict)


@dataclass
class BacktestStats:
    """Aggregated statistics from a completed backtest."""

    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    break_even_trades: int = 0
    total_pnl_usdt: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    max_drawdown_pct: float = 0.0
    peak_balance: float = 0.0
    avg_pnl_pct: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    profit_factor: float = 0.0
    win_rate: float = 0.0
    sharpe_ratio: float = 0.0
    max_concurrent: int = 0
    total_signals: int = 0
    total_rejected: int = 0
    symbols_traded: set[str] = field(default_factory=set)
    symbol_freq: dict[str, int] = field(default_factory=dict)
    exit_reason_freq: dict[str, int] = field(default_factory=dict)
    side_freq: dict[str, int] = field(default_factory=dict)
    watchlist_rotations: int = 0
    trades: list[BacktestTrade] = field(default_factory=list)
    daily_pnl: dict[str, float] = field(default_factory=dict)
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    hourly_pnl: list[tuple[datetime, float]] = field(default_factory=list)


class _BacktestPosition:
    """Simplified position for backtest (no exchange calls)."""

    def __init__(
        self,
        symbol: str,
        side: SignalDirection,
        entry_price: float,
        quantity: float,
        leverage: int,
        tp1_pct: float,
        tp2_pct: float,
        tp3_pct: float,
        tp1_ratio: float,
        tp2_ratio: float,
        sl_pct: float,
        trailing_pct: float,
        max_hold_min: int,
        opened_at: datetime,
        indicators: dict,
        atr_mode: bool = True,
        atr_tp1_mult: float = 0.8,
        atr_tp2_mult: float = 1.5,
        atr_tp3_mult: float = 2.5,
        atr_sl_mult: float = 1.0,
        atr_trailing_mult: float = 0.5,
        atr_value: float | None = None,
    ) -> None:
        self.symbol = symbol
        self.side = side
        self.entry_price = entry_price
        self.quantity = quantity
        self.original_quantity = quantity
        self.leverage = leverage
        self.opened_at = opened_at
        self.indicators = indicators
        self.atr_mode = atr_mode
        self._atr_value = atr_value

        # TP/SL levels
        if atr_mode and atr_value is not None:
            # ATR-based TP/SL
            if side == SignalDirection.LONG:
                self.tp1_price = entry_price + atr_value * atr_tp1_mult
                self.tp2_price = entry_price + atr_value * atr_tp2_mult
                self.tp3_price = entry_price + atr_value * atr_tp3_mult
                self.sl_price = entry_price - atr_value * atr_sl_mult
            else:
                self.tp1_price = entry_price - atr_value * atr_tp1_mult
                self.tp2_price = entry_price - atr_value * atr_tp2_mult
                self.tp3_price = entry_price - atr_value * atr_tp3_mult
                self.sl_price = entry_price + atr_value * atr_sl_mult
        else:
            # Fixed-percentage TP/SL
            if side == SignalDirection.LONG:
                self.tp1_price = entry_price * (1 + tp1_pct / 100)
                self.tp2_price = entry_price * (1 + tp2_pct / 100)
                self.tp3_price = entry_price * (1 + tp3_pct / 100)
                self.sl_price = entry_price * (1 - sl_pct / 100)
            else:
                self.tp1_price = entry_price * (1 - tp1_pct / 100)
                self.tp2_price = entry_price * (1 - tp2_pct / 100)
                self.tp3_price = entry_price * (1 - tp3_pct / 100)
                self.sl_price = entry_price * (1 + sl_pct / 100)

        self.tp1_close_ratio = tp1_ratio
        self.tp2_close_ratio = tp2_ratio
        self.trailing_stop_pct = trailing_pct

        if atr_mode:
            self._trail_distance = atr_tp3_mult * atr_trailing_mult / atr_tp3_mult * atr_trailing_mult
            # Use the same distance ratio: (tp3 - tp2) / atr_tp3_mult * atr_trailing_mult
            self._trail_distance = (self.tp3_price - self.tp2_price) / atr_tp3_mult * atr_trailing_mult
        else:
            self._trail_distance = None

        self.max_hold_min = max_hold_min

        # State
        self.tp1_hit = False
        self.tp2_hit = False
        self.trailing_active = False
        self.trailing_price = 0.0
        self.realized_pnl_usdt = 0.0

    @property
    def is_open(self) -> bool:
        return self.quantity > 0

    def check_exits(self, current_price: float, current_time: datetime) -> list[dict]:
        """Check exit conditions. Returns list of exit events.

        Each exit event is a dict with: type, price, quantity, reason
        """
        events: list[dict] = []

        # 1. Stop loss
        if self._is_sl_hit(current_price):
            # Record remaining PnL before closing
            remaining_pnl = self._calc_pnl(self.quantity, current_price)
            self.realized_pnl_usdt += remaining_pnl
            self.quantity = 0
            events.append({
                "type": "full_close",
                "price": current_price,
                "reason": ExitReason.SL.value,
            })
            return events

        # 2. TP1
        if not self.tp1_hit and self._is_tp_hit(self.tp1_price, current_price):
            close_qty = self.original_quantity * self.tp1_close_ratio
            close_qty = min(close_qty, self.quantity)
            if close_qty > 0:
                pnl = self._calc_pnl(close_qty, current_price)
                self.realized_pnl_usdt += pnl
                self.quantity -= close_qty
                events.append({
                    "type": "partial_close",
                    "price": current_price,
                    "quantity": close_qty,
                    "pnl": pnl,
                    "reason": ExitReason.TP1.value,
                })
            self.tp1_hit = True
            # Move SL to breakeven
            self.sl_price = self.entry_price
            if self.quantity <= 0:
                return events

        # 3. TP2
        if self.tp1_hit and not self.tp2_hit and self._is_tp_hit(self.tp2_price, current_price):
            close_qty = self.original_quantity * self.tp2_close_ratio
            close_qty = min(close_qty, self.quantity)
            if close_qty > 0:
                pnl = self._calc_pnl(close_qty, current_price)
                self.realized_pnl_usdt += pnl
                self.quantity -= close_qty
                events.append({
                    "type": "partial_close",
                    "price": current_price,
                    "quantity": close_qty,
                    "pnl": pnl,
                    "reason": ExitReason.TP2.value,
                })
            self.tp2_hit = True
            if self.quantity <= 0:
                return events

        # 4. TP3 — activate trailing
        if self.tp2_hit and not self.trailing_active and self._is_tp_hit(self.tp3_price, current_price):
            self.trailing_active = True
            if self.atr_mode and self._trail_distance is not None:
                if self.side == SignalDirection.LONG:
                    self.trailing_price = current_price - self._trail_distance
                else:
                    self.trailing_price = current_price + self._trail_distance
            else:
                if self.side == SignalDirection.LONG:
                    self.trailing_price = current_price * (1 - self.trailing_stop_pct / 100)
                else:
                    self.trailing_price = current_price * (1 + self.trailing_stop_pct / 100)
            events.append({
                "type": "trailing_activated",
                "price": current_price,
            })

        # 5. Trailing stop
        if self.trailing_active:
            self._update_trailing(current_price)
            if self._is_trailing_hit(current_price):
                remaining_pnl = self._calc_pnl(self.quantity, current_price)
                self.realized_pnl_usdt += remaining_pnl
                events.append({
                    "type": "full_close",
                    "price": current_price,
                    "reason": ExitReason.TP3.value,
                })
                self.quantity = 0
                return events

        # 6. Time-based force close
        elapsed = (current_time - self.opened_at).total_seconds() / 60
        if elapsed >= self.max_hold_min:
            remaining_pnl = self._calc_pnl(self.quantity, current_price)
            self.realized_pnl_usdt += remaining_pnl
            events.append({
                "type": "full_close",
                "price": current_price,
                "reason": ExitReason.TIME.value,
            })
            self.quantity = 0

        return events

    def _is_sl_hit(self, price: float) -> bool:
        if self.side == SignalDirection.LONG:
            return price <= self.sl_price
        return price >= self.sl_price

    def _is_tp_hit(self, tp_price: float, price: float) -> bool:
        if self.side == SignalDirection.LONG:
            return price >= tp_price
        return price <= tp_price

    def _is_trailing_hit(self, price: float) -> bool:
        if self.side == SignalDirection.LONG:
            return price <= self.trailing_price
        return price >= self.trailing_price

    def _update_trailing(self, price: float) -> None:
        if self.atr_mode and self._trail_distance is not None:
            if self.side == SignalDirection.LONG:
                new_trail = price - self._trail_distance
                if new_trail > self.trailing_price:
                    self.trailing_price = new_trail
            else:
                new_trail = price + self._trail_distance
                if new_trail < self.trailing_price:
                    self.trailing_price = new_trail
        else:
            trail_pct = self.trailing_stop_pct / 100
            if self.side == SignalDirection.LONG:
                new_trail = price * (1 - trail_pct)
                if new_trail > self.trailing_price:
                    self.trailing_price = new_trail
            else:
                new_trail = price * (1 + trail_pct)
                if new_trail < self.trailing_price:
                    self.trailing_price = new_trail

    def _calc_pnl(self, quantity: float, exit_price: float) -> float:
        if self.side == SignalDirection.LONG:
            return (exit_price - self.entry_price) * quantity
        return (self.entry_price - exit_price) * quantity


class _BacktestRiskGuard:
    """Simplified RiskGuard for backtest (no DB/Telegram)."""

    def __init__(
        self,
        risk_per_trade_pct: float,
        sl_pct: float,
        max_concurrent: int,
        max_daily_loss_pct: float,
        max_drawdown_pct: float,
        min_free_margin_pct: float,
        atr_mode: bool = True,
        atr_sl_mult: float = 1.0,
    ) -> None:
        self.risk_per_trade_pct = risk_per_trade_pct
        self.sl_pct = sl_pct
        self.max_concurrent = max_concurrent
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_drawdown_pct = max_drawdown_pct
        self.min_free_margin_pct = min_free_margin_pct
        self.atr_mode = atr_mode
        self.atr_sl_mult = atr_sl_mult

        self.daily_loss_usdt = 0.0
        self.peak_balance = 0.0
        self.drawdown_pct = 0.0
        self.halted = False

    def check_trade(
        self,
        entry_price: float,
        balance: float,
        open_positions: int,
        atr_value: float | None = None,
    ) -> tuple[bool, float, str]:
        """Returns (approved, position_size, reject_reason)."""
        if self.halted:
            return False, 0.0, "bot is halted"

        daily_loss_pct = abs(self.daily_loss_usdt) / balance * 100 if balance > 0 else 0.0
        if daily_loss_pct >= self.max_daily_loss_pct:
            self.halted = True
            return False, 0.0, f"daily_loss {daily_loss_pct:.2f}% >= {self.max_daily_loss_pct}%"

        if self.drawdown_pct >= self.max_drawdown_pct:
            self.halted = True
            return False, 0.0, f"drawdown {self.drawdown_pct:.2f}% >= {self.max_drawdown_pct}%"

        if open_positions >= self.max_concurrent:
            return False, 0.0, f"max_concurrent ({self.max_concurrent}) reached"

        risk_amount = balance * self.risk_per_trade_pct / 100
        if self.atr_mode and atr_value is not None:
            sl_distance = atr_value * self.atr_sl_mult
        else:
            sl_distance = entry_price * self.sl_pct / 100
        position_size = risk_amount / sl_distance

        if position_size <= 0:
            return False, 0.0, "position size too small"

        return True, position_size, ""

    def record_pnl(self, pnl_usdt: float, balance: float) -> None:
        if pnl_usdt < 0:
            self.daily_loss_usdt += pnl_usdt

        if balance > self.peak_balance:
            self.peak_balance = balance

        if self.peak_balance > 0:
            dd = (self.peak_balance - balance) / self.peak_balance * 100
            if dd > self.drawdown_pct:
                self.drawdown_pct = dd


class BacktestEngine:
    """Runs the Filtered Replay backtest.

    Args:
        config: Application config (strategy, risk, watchlist params).
        initial_balance: Starting USDT balance for the backtest.
    """

    def __init__(
        self,
        config: AppConfig,
        initial_balance: float = 10_000.0,
    ) -> None:
        self._config = config
        self._initial_balance = initial_balance
        self._klines_15m: dict[str, pd.DataFrame] = {}
        self._klines_3m: dict[str, pd.DataFrame] = {}
        self._positions: dict[str, _BacktestPosition] = {}
        self._trades: list[BacktestTrade] = []
        self._risk: _BacktestRiskGuard | None = None
        self._balance = initial_balance

        # Tracking
        self._signal_cooldowns: dict[str, datetime] = {}
        self._total_signals = 0
        self._total_rejected = 0
        self._watchlist_rotations = 0
        self._prev_watchlist: set[str] = set()
        self._symbol_freq: dict[str, int] = {}
        self._equity_curve: list[tuple[datetime, float]] = []
        self._hourly_pnl: list[tuple[datetime, float]] = []

    # ------------------------------------------------------------------
    # Load data
    # ------------------------------------------------------------------

    def load_data(
        self,
        klines_15m: dict[str, list[dict]],
        klines_3m: dict[str, list[dict]],
    ) -> None:
        """Load pre-fetched kline data into DataFrames."""
        for symbol, candles in klines_15m.items():
            df = pd.DataFrame(candles)
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            for col in ("open", "high", "low", "close", "volume"):
                df[col] = df[col].astype(float)
            df.sort_values("timestamp", inplace=True)
            df.reset_index(drop=True, inplace=True)
            self._klines_15m[symbol] = df

        for symbol, candles in klines_3m.items():
            df = pd.DataFrame(candles)
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            for col in ("open", "high", "low", "close", "volume"):
                df[col] = df[col].astype(float)
            df.sort_values("timestamp", inplace=True)
            df.reset_index(drop=True, inplace=True)
            self._klines_3m[symbol] = df

        logger.info(
            "backtest.engine | Loaded data: {n15} symbols (15m), {n3} symbols (3m)",
            n15=len(self._klines_15m),
            n3=len(self._klines_3m),
        )

    # ------------------------------------------------------------------
    # Phase 1 — Identify qualifying symbols from 15m data
    # ------------------------------------------------------------------

    def identify_qualifying_symbols(
        self,
        blacklist: list[str] | None = None,
        blacklist_patterns: list[str] | None = None,
        min_change_pct: float = 3.0,
        min_volume: float = 10_000_000,
        top_n: int = 5,
    ) -> list[str]:
        """Scan 15m data to find symbols that ever qualified for the watchlist.

        Returns:
            Sorted list of qualifying symbols.
        """
        refresh_sec = 300  # 5 min
        if not self._klines_15m:
            logger.warning("backtest.engine | No 15m data loaded")
            return []

        # Find global time range
        all_starts = []
        all_ends = []
        for df in self._klines_15m.values():
            all_starts.append(df["timestamp"].min())
            all_ends.append(df["timestamp"].max())
        global_start = min(all_starts)
        global_end = max(all_ends)

        qualifying: set[str] = set()
        current = global_start
        logger.info(
            "backtest.engine | Identifying qualifying symbols from {start} to {end}",
            start=global_start.strftime("%Y-%m-%d %H:%M"),
            end=global_end.strftime("%Y-%m-%d %H:%M"),
        )

        while current <= global_end - timedelta(hours=24):
            lookback_24h = current - timedelta(hours=24)
            candidates: list[tuple[str, float, float]] = []

            for symbol, df in self._klines_15m.items():
                if not symbol.endswith("USDT"):
                    continue
                if blacklist and symbol in blacklist:
                    continue
                if blacklist_patterns:
                    if any(pat in symbol for pat in blacklist_patterns):
                        continue

                # Get price at current time and 24h ago
                now_candle = self._get_candle_at_or_before(df, current)
                prev_candle = self._get_candle_at_or_before(df, lookback_24h)
                if now_candle is None or prev_candle is None:
                    continue

                now_close = now_candle["close"]
                prev_close = prev_candle["close"]
                if prev_close <= 0.0001 or now_close <= 0.0001:
                    continue

                change_pct = (now_close - prev_close) / prev_close * 100
                if change_pct < min_change_pct:
                    continue

                # 24h volume
                vol = df[
                    (df["timestamp"] >= lookback_24h) & (df["timestamp"] <= current)
                ]["volume"].sum()
                volume_usdt = (
                    df[
                        (df["timestamp"] >= lookback_24h) & (df["timestamp"] <= current)
                    ]["close"]
                    * df[
                        (df["timestamp"] >= lookback_24h) & (df["timestamp"] <= current)
                    ]["volume"]
                ).sum()
                if volume_usdt < min_volume:
                    continue

                candidates.append((symbol, change_pct, volume_usdt))

            # Top N by change_pct
            candidates.sort(key=lambda x: x[1], reverse=True)
            for sym, _, _ in candidates[:top_n]:
                qualifying.add(sym)

            current += timedelta(seconds=refresh_sec)

        result = sorted(qualifying)
        logger.info(
            "backtest.engine | Found {n} qualifying symbols",
            n=len(result),
        )
        if result:
            logger.info(
                "backtest.engine | Symbols: {syms}",
                syms=", ".join(result[:30]) + ("..." if len(result) > 30 else ""),
            )
        return result

    @staticmethod
    def _get_candle_at_or_before(
        df: pd.DataFrame, time: datetime
    ) -> dict | None:
        """Get the latest candle with timestamp <= time."""
        mask = df["timestamp"] <= time
        if not mask.any():
            return None
        idx = df[mask].index[-1]
        row = df.loc[idx]
        return {
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]),
            "timestamp": row["timestamp"],
        }

    # ------------------------------------------------------------------
    # Phase 2 — Run the backtest
    # ------------------------------------------------------------------

    def run(self, verbose: bool = False) -> BacktestStats:
        """Execute the Filtered Replay backtest.

        Args:
            verbose: Log every trade event.

        Returns:
            BacktestStats with all results.
        """
        self._risk = _BacktestRiskGuard(
            risk_per_trade_pct=self._config.risk.risk_per_trade_pct,
            sl_pct=self._config.strategy.exit.sl_pct,
            max_concurrent=self._config.risk.max_concurrent_positions,
            max_daily_loss_pct=self._config.risk.max_daily_loss_pct,
            max_drawdown_pct=self._config.risk.max_drawdown_pct,
            min_free_margin_pct=self._config.risk.min_free_margin_pct,
            atr_mode=self._config.strategy.exit.atr_mode,
            atr_sl_mult=self._config.strategy.exit.atr_sl_mult,
        )
        self._balance = self._initial_balance
        self._positions.clear()
        self._trades.clear()
        self._signal_cooldowns.clear()
        self._total_signals = 0
        self._total_rejected = 0
        self._watchlist_rotations = 0
        self._prev_watchlist.clear()
        self._symbol_freq.clear()
        self._equity_curve.clear()
        self._hourly_pnl.clear()

        entry_cfg = self._config.strategy.entry
        exit_cfg = self._config.strategy.exit
        signal_tf = self._config.strategy.signal_timeframe
        trend_tf = self._config.strategy.trend_timeframe
        refresh_sec = self._config.watchlist.refresh_interval_sec
        cooldown_min = entry_cfg.signal_cooldown_min

        # Build unified timeline from all 3m candles
        timestamps = self._build_timeline()
        if not timestamps:
            logger.error("backtest.engine | No timeline data — cannot run backtest")
            return self._build_stats()

        logger.info(
            "backtest.engine | Running backtest: {start} → {end} | "
            "{n} timestamps | balance={bal:.2f}",
            start=timestamps[0].strftime("%Y-%m-%d %H:%M"),
            end=timestamps[-1].strftime("%Y-%m-%d %H:%M"),
            n=len(timestamps),
            bal=self._balance,
        )

        next_refresh = timestamps[0]
        current_watchlist: list[str] = []

        for ts in timestamps:
            # Refresh watchlist
            if ts >= next_refresh:
                current_watchlist = self._refresh_watchlist(ts)
                next_refresh = ts + timedelta(seconds=refresh_sec)

            # Check exits for all open positions
            closed_symbols = set()
            for sym, pos in list(self._positions.items()):
                current_price = self._get_latest_price(sym, ts)
                if current_price is None:
                    continue
                events = pos.check_exits(current_price, ts)
                for event in events:
                    if event["type"] == "full_close":
                        self._close_position(sym, pos, event, ts)
                        closed_symbols.add(sym)
                    elif event["type"] == "partial_close":
                        self._risk.record_pnl(event["pnl"], self._balance)
                        if verbose:
                            logger.debug(
                                "backtest | {sym} {reason} price={price:.6f} "
                                "qty={qty:.4f} pnl={pnl:.4f}",
                                sym=sym,
                                reason=event["reason"],
                                price=event["price"],
                                qty=event["quantity"],
                                pnl=event["pnl"],
                            )

            # Try entry for each watchlist symbol with a 3m candle
            for sym in current_watchlist:
                if sym in self._positions:
                    continue
                if sym in closed_symbols:
                    continue

                df_3m = self._klines_3m.get(sym)
                df_15m = self._klines_15m.get(sym)
                if df_3m is None or df_15m is None:
                    continue

                # Get candle data up to this timestamp
                sub_3m = df_3m[df_3m["timestamp"] <= ts].tail(120)
                sub_15m = df_15m[df_15m["timestamp"] <= ts].tail(120)
                if len(sub_3m) == 0 or len(sub_15m) == 0:
                    continue

                # Check if a candle just closed at this timestamp
                last_candle = sub_3m.iloc[-1]
                if last_candle["timestamp"] != ts:
                    continue

                # Cooldown check
                if sym in self._signal_cooldowns:
                    elapsed = (ts - self._signal_cooldowns[sym]).total_seconds() / 60
                    if elapsed < cooldown_min:
                        continue

                # Signal evaluation
                try:
                    signal = self._evaluate_signal(sym, sub_3m, sub_15m)
                except Exception:
                    continue

                if signal is None:
                    continue

                # Market regime check: skip if ADX indicates sideways market.
                adx_value = signal.indicators.get("adx")
                adx_threshold = self._config.strategy.entry.adx_trend_threshold
                if adx_value is not None and adx_value < adx_threshold:
                    continue

                self._total_signals += 1
                entry_price = float(last_candle["close"])

                # Extract ATR from signal snapshot
                atr_value = signal.indicators.get("atr")

                # Risk check
                approved, position_size, reason = self._risk.check_trade(
                    entry_price=entry_price,
                    balance=self._balance,
                    open_positions=len(self._positions),
                    atr_value=atr_value,
                )

                if not approved:
                    self._total_rejected += 1
                    if verbose:
                        logger.debug(
                            "backtest | {sym} signal rejected: {reason}",
                            sym=sym,
                            reason=reason,
                        )
                    continue

                # Open position
                leverage = self._config.risk.leverage
                self._open_position(
                    symbol=sym,
                    signal=signal,
                    entry_price=entry_price,
                    quantity=position_size,
                    leverage=leverage,
                    opened_at=ts,
                    atr_value=atr_value,
                )

            # Record equity
            equity = self._balance + self._unrealized_pnl()
            self._equity_curve.append((ts, equity))
            if self._hourly_pnl:
                last_h = self._hourly_pnl[-1][0]
                if (ts - last_h).total_seconds() >= 3600:
                    self._hourly_pnl.append((ts, self._balance))
            else:
                self._hourly_pnl.append((ts, self._balance))

        # Close remaining positions at last timestamp
        last_ts = timestamps[-1]
        for sym, pos in list(self._positions.items()):
            price = self._get_latest_price(sym, last_ts) or pos.entry_price
            self._force_close_at(sym, pos, price, last_ts)

        logger.info(
            "backtest.engine | Backtest complete: {n} trades, balance {start:.2f} → {end:.2f}",
            n=len(self._trades),
            start=self._initial_balance,
            end=self._balance,
        )

        return self._build_stats()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_timeline(self) -> list[datetime]:
        """Build sorted list of unique 3m candle timestamps."""
        ts_set: set[datetime] = set()
        for df in self._klines_3m.values():
            for ts in df["timestamp"]:
                ts_set.add(ts)
        timeline = sorted(ts_set)
        return timeline

    def _refresh_watchlist(self, ts: datetime) -> list[str]:
        """Rebuild watchlist at a given timestamp."""
        lookback_24h = ts - timedelta(hours=24)
        entry_cfg = self._config.strategy.entry
        wc = self._config.watchlist

        candidates: list[tuple[str, float]] = []

        for symbol, df in self._klines_15m.items():
            if not symbol.endswith("USDT"):
                continue
            if symbol in wc.blacklist:
                continue
            if any(pat in symbol for pat in wc.blacklist_patterns):
                continue

            now_c = self._get_candle_at_or_before(df, ts)
            prev_c = self._get_candle_at_or_before(df, lookback_24h)
            if now_c is None or prev_c is None:
                continue

            prev_close = prev_c["close"]
            if prev_close <= 0.0001 or now_c["close"] <= 0.0001:
                continue

            change_pct = (now_c["close"] - prev_close) / prev_close * 100
            if change_pct < wc.min_change_pct_24h:
                continue

            # Volume check
            mask = (df["timestamp"] >= lookback_24h) & (df["timestamp"] <= ts)
            vol_usdt = (df.loc[mask, "close"] * df.loc[mask, "volume"]).sum()
            if vol_usdt < wc.min_volume_usdt_24h:
                continue

            candidates.append((symbol, change_pct))

        candidates.sort(key=lambda x: x[1], reverse=True)
        new_watchlist = [s for s, _ in candidates[:wc.top_n]]

        # Track rotations
        added = set(new_watchlist) - self._prev_watchlist
        removed = self._prev_watchlist - set(new_watchlist)
        if added or removed:
            self._watchlist_rotations += 1

        self._prev_watchlist = set(new_watchlist)
        return new_watchlist

    def _evaluate_signal(
        self, symbol: str, df_3m: pd.DataFrame, df_15m: pd.DataFrame
    ) -> Signal | None:
        """Evaluate entry signal using the existing SignalEngine logic."""
        from strategy.signal_engine import SignalEngine

        engine = SignalEngine(self._config.strategy.entry)
        return engine.evaluate(symbol, df_3m, df_15m)

    def _open_position(
        self,
        symbol: str,
        signal: Signal,
        entry_price: float,
        quantity: float,
        leverage: int,
        opened_at: datetime,
        atr_value: float | None = None,
    ) -> None:
        exit_cfg = self._config.strategy.exit
        pos = _BacktestPosition(
            symbol=symbol,
            side=signal.direction,
            entry_price=entry_price,
            quantity=quantity,
            leverage=leverage,
            tp1_pct=exit_cfg.tp1_pct,
            tp2_pct=exit_cfg.tp2_pct,
            tp3_pct=exit_cfg.tp3_pct,
            tp1_ratio=exit_cfg.tp1_close_ratio,
            tp2_ratio=exit_cfg.tp2_close_ratio,
            sl_pct=exit_cfg.sl_pct,
            trailing_pct=exit_cfg.trailing_stop_pct,
            max_hold_min=exit_cfg.max_hold_min,
            opened_at=opened_at,
            indicators=signal.indicators,
            atr_mode=exit_cfg.atr_mode,
            atr_tp1_mult=exit_cfg.atr_tp1_mult,
            atr_tp2_mult=exit_cfg.atr_tp2_mult,
            atr_tp3_mult=exit_cfg.atr_tp3_mult,
            atr_sl_mult=exit_cfg.atr_sl_mult,
            atr_trailing_mult=exit_cfg.atr_trailing_mult,
            atr_value=atr_value,
        )
        self._positions[symbol] = pos
        self._signal_cooldowns[symbol] = opened_at

        logger.info(
            "backtest | OPEN {side} {sym} @ {price:.6f} qty={qty:.4f} "
            "TP1={tp1:.6f} SL={sl:.6f}",
            side=signal.direction.value,
            sym=symbol,
            price=entry_price,
            qty=quantity,
            tp1=pos.tp1_price,
            sl=pos.sl_price,
        )

    def _close_position(
        self,
        symbol: str,
        pos: _BacktestPosition,
        event: dict,
        exit_time: datetime,
    ) -> None:
        exit_price = event["price"]
        pnl = pos.realized_pnl_usdt
        entry_notional = pos.entry_price * pos.original_quantity

        trade = BacktestTrade(
            symbol=symbol,
            side=pos.side.value,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            quantity=pos.original_quantity,
            leverage=pos.leverage,
            pnl_usdt=pnl,
            pnl_pct=pnl_pct,
            exit_reason=event["reason"],
            entry_at=pos.opened_at,
            exit_at=exit_time,
            indicators=pos.indicators,
        )
        self._trades.append(trade)

        self._balance += pnl
        self._risk.record_pnl(pnl, self._balance)

        self._symbol_freq[symbol] = self._symbol_freq.get(symbol, 0) + 1

        logger.info(
            "backtest | CLOSE {sym} {reason} pnl={pnl:+.4f} ({pct:+.2f}%) "
            "balance={bal:.2f}",
            sym=symbol,
            reason=event["reason"],
            pnl=pnl,
            pct=pnl_pct,
            bal=self._balance,
        )

        self._positions.pop(symbol, None)

    def _force_close_at(
        self,
        symbol: str,
        pos: _BacktestPosition,
        price: float,
        ts: datetime,
    ) -> None:
        pnl = pos.realized_pnl_usdt + pos._calc_pnl(pos.quantity, price)
        pos.realized_pnl_usdt = pnl
        pos.quantity = 0
        entry_notional = pos.entry_price * pos.original_quantity
        pnl_pct = (pnl / entry_notional * 100 * pos.leverage) if entry_notional > 0 else 0.0

        trade = BacktestTrade(
            symbol=symbol,
            side=pos.side.value,
            entry_price=pos.entry_price,
            exit_price=price,
            quantity=pos.original_quantity,
            leverage=pos.leverage,
            pnl_usdt=pnl,
            pnl_pct=pnl_pct,
            exit_reason="BACKTEST_END",
            entry_at=pos.opened_at,
            exit_at=ts,
            indicators=pos.indicators,
        )
        self._trades.append(trade)
        self._balance += pnl
        self._positions.pop(symbol, None)

    def _get_latest_price(self, symbol: str, ts: datetime) -> float | None:
        """Get the close price of the latest 3m candle <= ts."""
        df = self._klines_3m.get(symbol)
        if df is None:
            return None
        mask = df["timestamp"] <= ts
        if not mask.any():
            return None
        idx = df[mask].index[-1]
        return float(df.loc[idx, "close"])

    def _unrealized_pnl(self) -> float:
        """Calculate unrealized PnL from open positions using latest prices."""
        total = 0.0
        for sym, pos in self._positions.items():
            price = self._get_latest_price(sym, self._equity_curve[-1][0] if self._equity_curve else datetime.utcnow())
            if price is not None:
                total += pos._calc_pnl(pos.quantity, price)
        return total

    def _build_stats(self) -> BacktestStats:
        """Build final statistics from completed backtest."""
        stats = BacktestStats()
        stats.trades = list(self._trades)
        stats.total_trades = len(self._trades)
        stats.total_signals = self._total_signals
        stats.total_rejected = self._total_rejected
        stats.watchlist_rotations = self._watchlist_rotations
        stats.symbols_traded = set(self._symbol_freq.keys())
        stats.symbol_freq = dict(self._symbol_freq)
        stats.equity_curve = list(self._equity_curve)
        stats.hourly_pnl = list(self._hourly_pnl)

        if not self._trades:
            return stats

        wins = [t for t in self._trades if t.pnl_usdt > 0]
        losses = [t for t in self._trades if t.pnl_usdt < 0]
        break_even = [t for t in self._trades if t.pnl_usdt == 0]

        stats.winning_trades = len(wins)
        stats.losing_trades = len(losses)
        stats.break_even_trades = len(break_even)
        stats.total_pnl_usdt = sum(t.pnl_usdt for t in self._trades)
        stats.gross_profit = sum(t.pnl_usdt for t in wins)
        stats.gross_loss = abs(sum(t.pnl_usdt for t in losses))
        stats.profit_factor = (
            stats.gross_profit / stats.gross_loss if stats.gross_loss > 0 else float("inf")
        )
        stats.win_rate = len(wins) / stats.total_trades * 100 if stats.total_trades > 0 else 0
        stats.avg_pnl_pct = (
            sum(t.pnl_pct for t in self._trades) / stats.total_trades
        )
        stats.avg_win_pct = (
            sum(t.pnl_pct for t in wins) / len(wins) if wins else 0
        )
        stats.avg_loss_pct = (
            sum(t.pnl_pct for t in losses) / len(losses) if losses else 0
        )

        # Max drawdown from equity curve
        if stats.equity_curve:
            peak = stats.equity_curve[0][1]
            max_dd = 0.0
            for _, equity in stats.equity_curve:
                if equity > peak:
                    peak = equity
                dd = (peak - equity) / peak * 100 if peak > 0 else 0
                if dd > max_dd:
                    max_dd = dd
            stats.max_drawdown_pct = max_dd
            stats.peak_balance = peak

        # Exit reason frequency
        for t in self._trades:
            stats.exit_reason_freq[t.exit_reason] = (
                stats.exit_reason_freq.get(t.exit_reason, 0) + 1
            )

        # Side frequency
        for t in self._trades:
            stats.side_freq[t.side] = stats.side_freq.get(t.side, 0) + 1

        # Daily PnL
        for t in self._trades:
            day = t.exit_at.strftime("%Y-%m-%d")
            stats.daily_pnl[day] = stats.daily_pnl.get(day, 0.0) + t.pnl_usdt

        # Sharpe ratio (simplified — from hourly returns)
        if len(stats.hourly_pnl) >= 2:
            returns = []
            for i in range(1, len(stats.hourly_pnl)):
                prev_bal = stats.hourly_pnl[i - 1][1]
                curr_bal = stats.hourly_pnl[i][1]
                if prev_bal > 0:
                    returns.append((curr_bal - prev_bal) / prev_bal)
            if returns and len(returns) >= 2:
                import statistics
                avg_ret = statistics.mean(returns)
                std_ret = statistics.stdev(returns)
                if std_ret > 0:
                    stats.sharpe_ratio = (avg_ret / std_ret) * (24 * 365) ** 0.5

        # Max concurrent positions
        max_conc = 0
        for _, eq in stats.equity_curve:
            current_open = len([
                t for t in stats.trades
                if t.entry_at <= _ and t.exit_at >= _
            ])
            if current_open > max_conc:
                max_conc = current_open
        # Simplified: just use max positions at any point
        stats.max_concurrent = max_conc

        return stats
