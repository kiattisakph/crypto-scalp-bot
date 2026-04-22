"""UTC timezone helpers for crypto-scalp-bot."""
from __future__ import annotations

from datetime import datetime, timezone


def utc_now() -> datetime:
    """Return the current UTC datetime (timezone-aware).

    Returns:
        A timezone-aware datetime set to the current UTC time.
    """
    return datetime.now(timezone.utc)


def minutes_elapsed(since: datetime) -> float:
    """Calculate the number of minutes elapsed since the given datetime.

    If *since* is naive (no tzinfo), it is assumed to be UTC.

    Args:
        since: The reference datetime to measure from.

    Returns:
        Elapsed time in minutes (always >= 0).
    """
    now = utc_now()
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    delta = now - since
    return max(delta.total_seconds() / 60.0, 0.0)
