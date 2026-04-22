"""Telegram notification sender for crypto-scalp-bot.

Sends event notifications to a configured Telegram chat via the Bot API.
All send failures are logged and swallowed — bot operation must never be
interrupted by a notification error.
"""
from __future__ import annotations

import httpx
from loguru import logger

from core.enums import ExitReason, SignalDirection


class TelegramAlert:
    """Sends trading event notifications to Telegram.

    Args:
        bot_token: Telegram Bot API token.
        chat_id: Target Telegram chat ID.
    """

    _BASE_URL = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self, bot_token: str, chat_id: str) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._url = self._BASE_URL.format(token=bot_token)
        self._client = httpx.AsyncClient(timeout=10.0)

    async def close(self) -> None:
        """Close the underlying HTTP client.

        Safe to call multiple times; subsequent calls are no-ops.
        Should be called during bot shutdown after the last message
        has been sent.
        """
        await self._client.aclose()

    async def send(self, message: str) -> None:
        """Send a text message to the configured Telegram chat.

        Reuses a persistent ``httpx.AsyncClient`` to avoid creating a
        new TCP connection and TLS handshake on every message.

        On any HTTP or API error the failure is logged and the exception
        is **not** re-raised so that bot operation continues uninterrupted.

        Args:
            message: The text to send.
        """
        try:
            response = await self._client.post(
                self._url,
                json={"chat_id": self._chat_id, "text": message},
            )
            if response.status_code != 200:
                logger.warning(
                    "telegram | Non-200 response: {status} | {body}",
                    status=response.status_code,
                    body=response.text,
                )
        except httpx.HTTPError as exc:
            logger.warning(
                "telegram | Failed to send message: {error}",
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # Helper methods — format a message and delegate to send()
    # ------------------------------------------------------------------

    async def notify_started(self) -> None:
        """Send a bot-started notification."""
        await self.send("🟢 Bot started")

    async def notify_stopped(self) -> None:
        """Send a bot-stopped notification."""
        await self.send("🔴 Bot stopped")

    async def notify_watchlist_changed(
        self,
        added: list[str],
        removed: list[str],
    ) -> None:
        """Send a watchlist-changed notification.

        Args:
            added: Symbols added to the watchlist.
            removed: Symbols removed from the watchlist.
        """
        lines = ["📋 Watchlist updated"]
        if added:
            lines.append(f"  Added: {', '.join(added)}")
        if removed:
            lines.append(f"  Removed: {', '.join(removed)}")
        await self.send("\n".join(lines))

    @staticmethod
    def format_position_opened(
        symbol: str,
        direction: SignalDirection,
        entry_price: float,
        quantity: float,
        sl_price: float,
        tp1_price: float,
    ) -> str:
        """Build the position-opened message string.

        Args:
            symbol: Trading pair symbol.
            direction: LONG or SHORT.
            entry_price: Entry price.
            quantity: Position size (quantity).
            sl_price: Stop-loss price.
            tp1_price: Take-profit level 1 price.

        Returns:
            Formatted notification message.
        """
        emoji = "📈" if direction == SignalDirection.LONG else "📉"
        return (
            f"{emoji} Position Opened — {direction.value}\n"
            f"  Symbol: {symbol}\n"
            f"  Direction: {direction.value}\n"
            f"  Entry Price: {entry_price}\n"
            f"  Size: {quantity}\n"
            f"  Stop Loss: {sl_price}\n"
            f"  TP1 Target: {tp1_price}"
        )

    async def notify_position_opened(
        self,
        symbol: str,
        direction: SignalDirection,
        entry_price: float,
        quantity: float,
        sl_price: float,
        tp1_price: float,
    ) -> None:
        """Send a position-opened notification.

        Args:
            symbol: Trading pair symbol.
            direction: LONG or SHORT.
            entry_price: Entry price.
            quantity: Position size (quantity).
            sl_price: Stop-loss price.
            tp1_price: Take-profit level 1 price.
        """
        msg = self.format_position_opened(
            symbol, direction, entry_price, quantity, sl_price, tp1_price,
        )
        await self.send(msg)

    @staticmethod
    def format_position_closed(
        symbol: str,
        exit_reason: ExitReason,
        pnl_usdt: float,
    ) -> str:
        """Build the position-closed message string.

        Args:
            symbol: Trading pair symbol.
            exit_reason: Why the position was closed.
            pnl_usdt: Realized PnL in USDT.

        Returns:
            Formatted notification message.
        """
        emoji = "💰" if pnl_usdt >= 0 else "💸"
        return (
            f"{emoji} Position Closed\n"
            f"  Symbol: {symbol}\n"
            f"  Exit Reason: {exit_reason.value}\n"
            f"  PnL: {pnl_usdt} USDT"
        )

    async def notify_position_closed(
        self,
        symbol: str,
        exit_reason: ExitReason,
        pnl_usdt: float,
    ) -> None:
        """Send a position-closed notification.

        Args:
            symbol: Trading pair symbol.
            exit_reason: Why the position was closed.
            pnl_usdt: Realized PnL in USDT.
        """
        msg = self.format_position_closed(symbol, exit_reason, pnl_usdt)
        await self.send(msg)

    async def notify_risk_halt(self, reason: str, value: float) -> None:
        """Send a risk-halt notification.

        Args:
            reason: The specific risk limit that was breached.
            value: The current value that triggered the halt.
        """
        await self.send(
            f"⛔ HALT — Risk limit breached\n"
            f"  Reason: {reason}\n"
            f"  Current Value: {value}"
        )

    async def notify_reconnected(self, duration_sec: float) -> None:
        """Send a WebSocket-reconnected notification.

        Args:
            duration_sec: Duration of the disconnection in seconds.
        """
        await self.send(
            f"🔄 WebSocket reconnected\n"
            f"  Downtime: {duration_sec:.1f}s"
        )

    async def notify_reconciliation(
        self,
        symbol: str,
        action: str,
        details: str,
    ) -> None:
        """Send a periodic reconciliation event notification.

        Args:
            symbol: Trading pair symbol involved.
            action: What the reconciliation did (e.g. "phantom_closed").
            details: Human-readable description.
        """
        await self.send(
            f"🔍 RECONCILIATION\n"
            f"  Symbol: {symbol}\n"
            f"  Action: {action}\n"
            f"  {details}"
        )
