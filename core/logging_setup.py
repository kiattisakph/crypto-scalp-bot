"""Logging configuration for crypto-scalp-bot.

Configures loguru sinks for console output, general bot log file,
and a separate trade-specific log file. All timestamps are UTC.

Console output uses colour, compact timestamps, and level icons for
quick visual scanning. File output uses full timestamps and fixed-width
fields for grep-friendliness.

Usage:
    from core.logging_setup import setup_logging
    setup_logging(log_level="INFO")
"""
from __future__ import annotations

import sys

from loguru import logger

# ---------------------------------------------------------------------------
# Format strings
# ---------------------------------------------------------------------------

# Console: compact time, level icon, coloured level + message.
# The pipe-separated component prefix inside the message (e.g.
# "position | TP1 hit ...") is kept as-is — loguru's colour markup
# makes the level stand out enough.
_CONSOLE_FORMAT = (
    "<light-black>{time:HH:mm:ss!UTC}</light-black> "
    "{level.icon} "
    "<level>{level: <8}</level>| "
    "{message}"
)

# File: full ISO timestamp, fixed-width level, plain text.
_FILE_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS!UTC} | "
    "{level: <8} | "
    "{message}"
)

# Default component name when logger.bind(component=...) has not been called.
_DEFAULT_COMPONENT = "bot"


def _component_filter(record: dict) -> bool:
    """Ensure every log record has a 'component' extra field."""
    record["extra"].setdefault("component", _DEFAULT_COMPONENT)
    return True


def _trade_filter(record: dict) -> bool:
    """Only allow records whose component is 'trade' into the trades log."""
    record["extra"].setdefault("component", _DEFAULT_COMPONENT)
    return record["extra"]["component"] == "trade"


def setup_logging(log_level: str = "INFO") -> None:
    """Configure loguru sinks for the application.

    Removes the default stderr sink and adds:
      1. Console (stderr) — all messages at *log_level* and above.
      2. ``logs/bot.log`` — all messages, 10 MB rotation, 7-day retention.
      3. ``logs/trades.log`` — trade-specific messages only, same rotation/retention.

    Args:
        log_level: Minimum log level sourced from the ``LOG_LEVEL`` env var.
    """
    # Remove any previously configured sinks (including the default one).
    logger.remove()

    level = log_level.upper()

    # 1. Console sink — stderr with colour and icons
    logger.add(
        sys.stderr,
        level=level,
        format=_CONSOLE_FORMAT,
        filter=_component_filter,
        colorize=True,
    )

    # 2. General bot log file — plain text, full timestamps
    logger.add(
        "logs/bot.log",
        level=level,
        format=_FILE_FORMAT,
        filter=_component_filter,
        rotation="10 MB",
        retention="7 days",
        encoding="utf-8",
    )

    # 3. Trade-specific log file
    logger.add(
        "logs/trades.log",
        level=level,
        format=_FILE_FORMAT,
        filter=_trade_filter,
        rotation="10 MB",
        retention="7 days",
        encoding="utf-8",
    )

    logger.bind(component="config").info(
        "Logging configured — level={level}", level=level,
    )
