"""Entry point for crypto-scalp-bot.

Loads configuration, configures logging, creates the BotEngine,
and runs the main event loop.
"""
from __future__ import annotations

import asyncio
import sys

from core.bot import BotEngine
from core.config import load_config
from core.logging_setup import setup_logging


async def run_bot() -> None:
    """Create and run the bot, ensuring shutdown cleanup on cancellation."""
    # 1. Load and validate configuration
    env, config = load_config()

    # 2. Configure logging
    setup_logging(log_level=env.log_level)

    # 3. Create BotEngine and run
    bot = BotEngine(env=env, config=config)
    try:
        await bot.start()
    finally:
        await bot.stop()


def main() -> None:
    """Load config, set up logging, and start the bot."""
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
