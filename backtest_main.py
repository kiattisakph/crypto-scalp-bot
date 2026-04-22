#!/usr/bin/env python3
"""Backtest entry point for crypto-scalp-bot.

Usage:
    python backtest_main.py --start 2025-01-01 --end 2025-04-01 --balance 10000
    python backtest_main.py --start 2025-01-01 --end 2025-04-01 --fetch-only
    python backtest_main.py --start 2025-01-01 --end 2025-04-01 --data-dir ./bt_data

Options:
    --start       Start date (YYYY-MM-DD)
    --end         End date (YYYY-MM-DD)
    --balance     Initial balance (default: 10000)
    --fetch-only  Only fetch data, don't run backtest
    --data-dir    Directory to save/load fetched data (default: ./bt_data)
    --demo        Use Binance testnet (default: mainnet)
    --config      Path to config.yaml (default: ./config.yaml)
    --verbose     Log every trade event
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

import yaml
from loguru import logger


def main() -> None:
    parser = argparse.ArgumentParser(description="Crypto Scalp Bot Backtest")
    parser.add_argument("--start", required=True, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", required=True, help="End date (YYYY-MM-DD)")
    parser.add_argument("--balance", type=float, default=10_000.0, help="Initial balance")
    parser.add_argument("--fetch-only", action="store_true", help="Only fetch data")
    parser.add_argument("--data-dir", default="./bt_data", help="Data directory")
    parser.add_argument("--demo", action="store_true", help="Use testnet")
    parser.add_argument("--config", default="./config.yaml", help="Config file")
    parser.add_argument("--verbose", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    # Setup logging
    logger.remove()
    logger.add(sys.stderr, level="INFO", format="{time:HH:mm:ss} | {level} | {message}")
    if args.verbose:
        logger.add(sys.stderr, level="DEBUG", format="{time:HH:mm:ss} | {level} | {message}")

    start_dt = datetime.strptime(args.start, "%Y-%m-%d")
    end_dt = datetime.strptime(args.end, "%Y-%m-%d")
    if end_dt <= start_dt:
        logger.error("--end must be after --start")
        sys.exit(1)

    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    # Run
    asyncio.run(run_backtest(args, start_dt, end_dt, data_dir))


async def run_backtest(
    args,
    start_dt: datetime,
    end_dt: datetime,
    data_dir: Path,
) -> None:
    """Main backtest orchestration."""
    from core.config import AppConfig, StrategyConfig, WatchlistConfig, RiskConfig
    from core.config import EntryConfig, ExitConfig

    from backtest.data_fetcher import DataFetcher
    from backtest.backtest_engine import BacktestEngine
    from backtest.report import print_report, save_report

    # Load config
    config_path = Path(args.config)
    if config_path.exists():
        with open(config_path) as f:
            raw = yaml.safe_load(f)
        config = AppConfig(**raw)
        logger.info("Loaded config from {path}", path=config_path)
    else:
        logger.warning("Config not found at {path}, using defaults", path=config_path)
        config = AppConfig(
            watchlist=WatchlistConfig(),
            strategy=StrategyConfig(),
            risk=RiskConfig(),
        )

    # ---- Data fetching phase ----
    klines_15m: dict[str, list[dict]] = {}
    klines_3m: dict[str, list[dict]] = {}

    cache_15m = data_dir / "klines_15m.yaml"
    cache_3m = data_dir / "klines_3m.yaml"
    cache_qualifying = data_dir / "qualifying_symbols.txt"

    if cache_15m.exists():
        logger.info("Loading cached 15m data from {path}", path=cache_15m)
        klines_15m = _load_yaml_data(cache_15m)
    else:
        fetcher = DataFetcher(demo=args.demo)
        try:
            await fetcher.connect()
            klines_15m = await fetcher.fetch_all_15m(start_dt, end_dt)
            _save_yaml_data(cache_15m, klines_15m)
            logger.info("Saved 15m data to {path}", path=cache_15m)
        finally:
            await fetcher.close()

    # Identify qualifying symbols
    qualifying: list[str] = []
    if cache_qualifying.exists():
        logger.info("Loading cached qualifying symbols")
        qualifying = cache_qualifying.read_text().strip().split("\n")
    else:
        engine = BacktestEngine(config)
        engine.load_data(klines_15m, {})
        qualifying = engine.identify_qualifying_symbols(
            blacklist=config.watchlist.blacklist,
            blacklist_patterns=config.watchlist.blacklist_patterns,
            min_change_pct=config.watchlist.min_change_pct_24h,
            min_volume=config.watchlist.min_volume_usdt_24h,
            top_n=config.watchlist.top_n,
        )
        cache_qualifying.write_text("\n".join(qualifying))
        logger.info(
            "Identified {n} qualifying symbols, saved to {path}",
            n=len(qualifying),
            path=cache_qualifying,
        )

    if args.fetch_only:
        print(f"\nFetch complete: {len(klines_15m)} symbols (15m)")
        print(f"Qualifying symbols: {len(qualifying)}")
        print(f"  {', '.join(qualifying[:30])}")
        return

    # Fetch 3m data for qualifying symbols
    if cache_3m.exists():
        logger.info("Loading cached 3m data from {path}", path=cache_3m)
        klines_3m = _load_yaml_data(cache_3m)
    else:
        # Only fetch symbols that have 15m data
        qualifying_with_15m = [s for s in qualifying if s in klines_15m]
        logger.info(
            "Fetching 3m data for {n}/{total} qualifying symbols "
            "(missing 15m data for {missing})",
            n=len(qualifying_with_15m),
            total=len(qualifying),
            missing=len(qualifying) - len(qualifying_with_15m),
        )
        if qualifying_with_15m:
            fetcher = DataFetcher(demo=args.demo)
            try:
                await fetcher.connect()
                klines_3m = await fetcher.fetch_3m_for_symbols(
                    qualifying_with_15m, start_dt, end_dt,
                )
                _save_yaml_data(cache_3m, klines_3m)
                logger.info("Saved 3m data to {path}", path=cache_3m)
            finally:
                await fetcher.close()
        else:
            logger.warning("No qualifying symbols have 15m data")

    # ---- Backtest phase ----
    engine = BacktestEngine(config, initial_balance=args.balance)
    engine.load_data(klines_15m, klines_3m)
    stats = engine.run(verbose=args.verbose)

    # ---- Report phase ----
    print_report(stats, args.balance)
    output_dir = save_report(stats, args.balance, str(data_dir / "reports"))

    print(f"\nReport saved to: {output_dir}")


def _load_yaml_data(path: Path) -> dict:
    """Load candle data from a YAML file."""
    with open(path) as f:
        return yaml.safe_load(f)


def _save_yaml_data(path: Path, data: dict) -> None:
    """Save candle data to a YAML file."""
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False)


if __name__ == "__main__":
    main()
