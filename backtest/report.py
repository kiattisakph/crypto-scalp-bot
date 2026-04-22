"""Backtest report generation — formatted console output and CSV export."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from loguru import logger

from backtest.backtest_engine import BacktestStats


def print_report(stats: BacktestStats, initial_balance: float) -> None:
    """Print a formatted backtest report to the console."""
    sep = "=" * 60
    thin = "-" * 40

    print(sep)
    print("  BACKTEST REPORT — Filtered Replay")
    print(sep)

    if stats.total_trades == 0:
        print("\n  No trades were executed during this period.")
        print(f"  Signals generated: {stats.total_signals}")
        print(f"  Watchlist rotations: {stats.watchlist_rotations}")
        print(sep)
        return

    # --- Overview ---
    print(f"\n  Period:            {stats.trades[0].entry_at.strftime('%Y-%m-%d %H:%M')} → {stats.trades[-1].exit_at.strftime('%Y-%m-%d %H:%M')}")
    print(f"  Initial balance:   ${initial_balance:>12,.2f}")
    print(f"  Final balance:     ${stats.trades[-1].pnl_usdt + initial_balance:>12,.2f}")
    print(f"  Net PnL:           ${stats.total_pnl_usdt:>12,.2f}")
    print(f"  Net PnL %:         {stats.total_pnl_usdt / initial_balance * 100:>11.2f}%")

    # --- Performance ---
    print(f"\n{thin}")
    print("  PERFORMANCE")
    print(thin)
    print(f"  Total trades:      {stats.total_trades:>12}")
    print(f"  Winning:           {stats.winning_trades:>12}  ({stats.win_rate:.1f}%)")
    print(f"  Losing:            {stats.losing_trades:>12}")
    print(f"  Break-even:        {stats.break_even_trades:>12}")
    print(f"  Profit factor:     {stats.profit_factor:>12.2f}")
    print(f"  Sharpe ratio:      {stats.sharpe_ratio:>12.2f}")
    print(f"  Max drawdown:      {stats.max_drawdown_pct:>11.2f}%")
    print(f"  Avg trade PnL %:   {stats.avg_pnl_pct:>11.2f}%")
    print(f"  Avg win PnL %:     {stats.avg_win_pct:>11.2f}%")
    print(f"  Avg loss PnL %:    {stats.avg_loss_pct:>11.2f}%")

    # --- Signal stats ---
    print(f"\n{thin}")
    print("  SIGNALS")
    print(thin)
    print(f"  Total signals:     {stats.total_signals:>12}")
    print(f"  Trades executed:   {stats.total_trades:>12}  ({stats.total_trades / max(stats.total_signals, 1) * 100:.1f}% of signals)")
    print(f"  Rejected (risk):   {stats.total_rejected:>12}")
    print(f"  Watchlist changes: {stats.watchlist_rotations:>12}")

    # --- Exit reasons ---
    print(f"\n{thin}")
    print("  EXIT REASONS")
    print(thin)
    for reason, count in sorted(stats.exit_reason_freq.items(), key=lambda x: -x[1]):
        pct = count / stats.total_trades * 100
        print(f"  {reason:<20} {count:>6}  ({pct:.1f}%)")

    # --- Side distribution ---
    print(f"\n{thin}")
    print("  SIDE DISTRIBUTION")
    print(thin)
    for side, count in sorted(stats.side_freq.items(), key=lambda x: -x[1]):
        print(f"  {side:<20} {count:>6}")

    # --- Top symbols ---
    print(f"\n{thin}")
    print("  TOP SYMBOLS (by trade count)")
    print(thin)
    sorted_syms = sorted(stats.symbol_freq.items(), key=lambda x: -x[1])
    for sym, count in sorted_syms[:15]:
        sym_trades = [t for t in stats.trades if t.symbol == sym]
        sym_pnl = sum(t.pnl_usdt for t in sym_trades)
        sym_wins = sum(1 for t in sym_trades if t.pnl_usdt > 0)
        sym_wr = sym_wins / len(sym_trades) * 100 if sym_trades else 0
        print(f"  {sym:<16} {count:>4} trades  PnL ${sym_pnl:>10.2f}  WR {sym_wr:.0f}%")

    # --- Daily PnL ---
    if stats.daily_pnl:
        print(f"\n{thin}")
        print("  DAILY PnL")
        print(thin)
        for day, pnl in sorted(stats.daily_pnl.items()):
            bar = "+" if pnl > 0 else ""
            marker = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
            print(f"  {day}  {marker} ${bar}{pnl:.2f}")

    print(f"\n{sep}")


def save_report(
    stats: BacktestStats,
    initial_balance: float,
    output_dir: str = "./backtest_output",
) -> Path:
    """Save full report to CSV and JSON files.

    Returns:
        Path to the output directory.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # --- Trade log CSV ---
    csv_path = out / f"trades_{timestamp}.csv"
    with open(csv_path, "w", encoding="utf-8") as f:
        f.write("symbol,side,entry_price,exit_price,quantity,leverage,")
        f.write("pnl_usdt,pnl_pct,exit_reason,entry_at,exit_at\n")
        for t in stats.trades:
            f.write(
                f"{t.symbol},{t.side},{t.entry_price},{t.exit_price},"
                f"{t.quantity},{t.leverage},{t.pnl_usdt:.4f},{t.pnl_pct:.2f},"
                f"{t.exit_reason},{t.entry_at},{t.exit_at}\n"
            )
    logger.info("backtest.report | Saved {n} trades to {path}", n=len(stats.trades), path=csv_path)

    # --- Stats JSON ---
    json_path = out / f"stats_{timestamp}.json"
    stats_dict = {
        "initial_balance": initial_balance,
        "final_balance": initial_balance + stats.total_pnl_usdt,
        "total_pnl_usdt": stats.total_pnl_usdt,
        "total_pnl_pct": stats.total_pnl_usdt / initial_balance * 100 if initial_balance > 0 else 0,
        "total_trades": stats.total_trades,
        "winning_trades": stats.winning_trades,
        "losing_trades": stats.losing_trades,
        "win_rate": stats.win_rate,
        "profit_factor": stats.profit_factor,
        "sharpe_ratio": stats.sharpe_ratio,
        "max_drawdown_pct": stats.max_drawdown_pct,
        "avg_pnl_pct": stats.avg_pnl_pct,
        "avg_win_pct": stats.avg_win_pct,
        "avg_loss_pct": stats.avg_loss_pct,
        "total_signals": stats.total_signals,
        "total_rejected": stats.total_rejected,
        "watchlist_rotations": stats.watchlist_rotations,
        "symbol_freq": stats.symbol_freq,
        "exit_reason_freq": stats.exit_reason_freq,
        "side_freq": stats.side_freq,
        "daily_pnl": stats.daily_pnl,
        "period_start": stats.trades[0].entry_at.isoformat() if stats.trades else None,
        "period_end": stats.trades[-1].exit_at.isoformat() if stats.trades else None,
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(stats_dict, f, indent=2, default=str)
    logger.info("backtest.report | Saved stats to {path}", path=json_path)

    # --- Equity curve CSV ---
    if stats.equity_curve:
        eq_path = out / f"equity_{timestamp}.csv"
        with open(eq_path, "w", encoding="utf-8") as f:
            f.write("timestamp,equity\n")
            for ts, equity in stats.equity_curve:
                f.write(f"{ts},{equity:.4f}\n")
        logger.info("backtest.report | Saved equity curve ({n} points) to {path}", n=len(stats.equity_curve), path=eq_path)

    return out
