"""Unit tests for KlineStream message parsing, candle forwarding, and subscription tracking."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from streams.kline_stream import KlineStream


# ---------------------------------------------------------------------------
# _parse_candle
# ---------------------------------------------------------------------------


class TestParseCandle:
    """Tests for KlineStream._parse_candle static method."""

    def test_parses_valid_kline(self) -> None:
        kline = {
            "o": "150.00",
            "h": "155.00",
            "l": "149.00",
            "c": "153.50",
            "v": "12345.67",
            "t": 1700000000000,
        }
        result = KlineStream._parse_candle(kline)

        assert result["open"] == pytest.approx(150.00)
        assert result["high"] == pytest.approx(155.00)
        assert result["low"] == pytest.approx(149.00)
        assert result["close"] == pytest.approx(153.50)
        assert result["volume"] == pytest.approx(12345.67)
        assert result["timestamp"] == 1700000000000

    def test_raises_on_missing_field(self) -> None:
        kline = {"o": "150.00", "h": "155.00"}  # missing l, c, v, t
        with pytest.raises(KeyError):
            KlineStream._parse_candle(kline)


# ---------------------------------------------------------------------------
# _parse_timeframe
# ---------------------------------------------------------------------------


class TestParseTimeframe:
    """Tests for KlineStream._parse_timeframe instance method."""

    def _make_stream(self, timeframes: list[str]) -> KlineStream:
        mock_client = MagicMock()
        return KlineStream(mock_client, timeframes=timeframes)

    def test_valid_3m(self) -> None:
        stream = self._make_stream(["3m", "15m"])
        assert stream._parse_timeframe("3m") == "3m"

    def test_valid_15m(self) -> None:
        stream = self._make_stream(["3m", "15m"])
        assert stream._parse_timeframe("15m") == "15m"

    def test_unknown_interval_returns_none(self) -> None:
        stream = self._make_stream(["3m", "15m"])
        assert stream._parse_timeframe("1h") is None
        assert stream._parse_timeframe("") is None
        assert stream._parse_timeframe("5m") is None

    def test_custom_timeframes_accepted(self) -> None:
        stream = self._make_stream(["5m", "1h"])
        assert stream._parse_timeframe("5m") == "5m"
        assert stream._parse_timeframe("1h") == "1h"
        assert stream._parse_timeframe("3m") is None
        assert stream._parse_timeframe("15m") is None


# ---------------------------------------------------------------------------
# _handle_message — closed candle forwarding
# ---------------------------------------------------------------------------


class TestHandleMessage:
    """Tests for KlineStream._handle_message."""

    @pytest.fixture
    def stream(self) -> KlineStream:
        mock_client = MagicMock()
        ks = KlineStream(mock_client, timeframes=["3m", "15m"])
        return ks

    @pytest.mark.asyncio
    async def test_forwards_closed_candle(self, stream: KlineStream) -> None:
        callback = AsyncMock()
        stream.on_candle_closed = callback

        msg = {
            "stream": "solusdt@kline_3m",
            "data": {
                "e": "kline",
                "k": {
                    "s": "SOLUSDT",
                    "i": "3m",
                    "o": "150.0",
                    "h": "155.0",
                    "l": "149.0",
                    "c": "153.0",
                    "v": "1000.0",
                    "t": 1700000000000,
                    "x": True,
                },
            },
        }
        await stream._handle_message("SOLUSDT", msg)

        callback.assert_awaited_once()
        args = callback.call_args[0]
        assert args[0] == "SOLUSDT"
        assert args[1] == "3m"
        assert args[2]["close"] == pytest.approx(153.0)

    @pytest.mark.asyncio
    async def test_does_not_forward_open_candle(
        self, stream: KlineStream
    ) -> None:
        callback = AsyncMock()
        stream.on_candle_closed = callback

        msg = {
            "stream": "solusdt@kline_3m",
            "data": {
                "e": "kline",
                "k": {
                    "s": "SOLUSDT",
                    "i": "3m",
                    "o": "150.0",
                    "h": "155.0",
                    "l": "149.0",
                    "c": "153.0",
                    "v": "1000.0",
                    "t": 1700000000000,
                    "x": False,
                },
            },
        }
        await stream._handle_message("SOLUSDT", msg)
        callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_forwards_15m_closed_candle(
        self, stream: KlineStream
    ) -> None:
        callback = AsyncMock()
        stream.on_candle_closed = callback

        msg = {
            "stream": "ethusdt@kline_15m",
            "data": {
                "e": "kline",
                "k": {
                    "s": "ETHUSDT",
                    "i": "15m",
                    "o": "3200.0",
                    "h": "3250.0",
                    "l": "3190.0",
                    "c": "3240.0",
                    "v": "5000.0",
                    "t": 1700000000000,
                    "x": True,
                },
            },
        }
        await stream._handle_message("ETHUSDT", msg)

        callback.assert_awaited_once()
        args = callback.call_args[0]
        assert args[0] == "ETHUSDT"
        assert args[1] == "15m"

    @pytest.mark.asyncio
    async def test_skips_message_without_data(
        self, stream: KlineStream
    ) -> None:
        callback = AsyncMock()
        stream.on_candle_closed = callback

        await stream._handle_message("SOLUSDT", {"stream": "solusdt@kline_3m"})
        callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_message_without_kline_field(
        self, stream: KlineStream
    ) -> None:
        callback = AsyncMock()
        stream.on_candle_closed = callback

        msg = {"stream": "solusdt@kline_3m", "data": {"e": "something_else"}}
        await stream._handle_message("SOLUSDT", msg)
        callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_unknown_interval(self, stream: KlineStream) -> None:
        callback = AsyncMock()
        stream.on_candle_closed = callback

        msg = {
            "stream": "solusdt@kline_1h",
            "data": {
                "e": "kline",
                "k": {
                    "s": "SOLUSDT",
                    "i": "1h",
                    "o": "150.0",
                    "h": "155.0",
                    "l": "149.0",
                    "c": "153.0",
                    "v": "1000.0",
                    "t": 1700000000000,
                    "x": True,
                },
            },
        }
        await stream._handle_message("SOLUSDT", msg)
        callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_crash_when_callback_not_set(
        self, stream: KlineStream
    ) -> None:
        stream.on_candle_closed = None
        msg = {
            "stream": "solusdt@kline_3m",
            "data": {
                "e": "kline",
                "k": {
                    "s": "SOLUSDT",
                    "i": "3m",
                    "o": "150.0",
                    "h": "155.0",
                    "l": "149.0",
                    "c": "153.0",
                    "v": "1000.0",
                    "t": 1700000000000,
                    "x": True,
                },
            },
        }
        # Should not raise
        await stream._handle_message("SOLUSDT", msg)

    @pytest.mark.asyncio
    async def test_handles_malformed_kline_data(
        self, stream: KlineStream
    ) -> None:
        callback = AsyncMock()
        stream.on_candle_closed = callback

        msg = {
            "stream": "solusdt@kline_3m",
            "data": {
                "e": "kline",
                "k": {
                    "s": "SOLUSDT",
                    "i": "3m",
                    "x": True,
                    # Missing OHLCV fields
                },
            },
        }
        # Should not raise — logs warning and skips
        await stream._handle_message("SOLUSDT", msg)
        callback.assert_not_awaited()


# ---------------------------------------------------------------------------
# Subscription tracking
# ---------------------------------------------------------------------------


class TestSubscriptionTracking:
    """Tests for KlineStream subscribe/unsubscribe tracking."""

    @pytest.fixture
    def stream(self) -> KlineStream:
        mock_client = MagicMock()
        return KlineStream(mock_client, timeframes=["3m", "15m"])

    def test_initial_state_empty(self, stream: KlineStream) -> None:
        assert stream.get_subscribed_symbols() == set()

    @pytest.mark.asyncio
    async def test_subscribe_adds_symbol(self, stream: KlineStream) -> None:
        # We need to mock ReconnectingWebSocket to avoid real connections
        with pytest.MonkeyPatch.context() as mp:
            mock_socket = MagicMock()
            mock_socket.__aenter__ = AsyncMock(return_value=mock_socket)
            mock_socket.__aexit__ = AsyncMock(return_value=None)
            mock_socket.recv = AsyncMock(side_effect=Exception("test stop"))

            mp.setattr(
                "streams.kline_stream.ReconnectingWebSocket",
                lambda client, streams: mock_socket,
            )

            await stream.subscribe("SOLUSDT")
            assert "SOLUSDT" in stream.get_subscribed_symbols()

            # Clean up the task
            await stream.unsubscribe("SOLUSDT")
            assert "SOLUSDT" not in stream.get_subscribed_symbols()


# ---------------------------------------------------------------------------
# Config-driven timeframes
# ---------------------------------------------------------------------------


class TestConfigDrivenTimeframes:
    """Tests verifying KlineStream uses config timeframes, not hardcoded values."""

    def test_empty_timeframes_raises(self) -> None:
        mock_client = MagicMock()
        with pytest.raises(ValueError, match="at least one interval"):
            KlineStream(mock_client, timeframes=[])

    @pytest.mark.asyncio
    async def test_subscribe_uses_configured_timeframes(self) -> None:
        """Verify subscribe builds stream names from the provided timeframes."""
        mock_client = MagicMock()
        stream = KlineStream(mock_client, timeframes=["5m", "1h"])

        captured_streams: list[str] = []

        def _fake_ws(client, streams):
            captured_streams.extend(streams)
            mock_socket = MagicMock()
            mock_socket.__aenter__ = AsyncMock(return_value=mock_socket)
            mock_socket.__aexit__ = AsyncMock(return_value=None)
            mock_socket.recv = AsyncMock(side_effect=Exception("test stop"))
            return mock_socket

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "streams.kline_stream.ReconnectingWebSocket",
                _fake_ws,
            )

            await stream.subscribe("SOLUSDT")

            assert "solusdt@kline_5m" in captured_streams
            assert "solusdt@kline_1h" in captured_streams
            assert "solusdt@kline_3m" not in captured_streams
            assert "solusdt@kline_15m" not in captured_streams

            await stream.unsubscribe("SOLUSDT")

    @pytest.mark.asyncio
    async def test_handle_message_forwards_custom_timeframe(self) -> None:
        """Verify closed candles with custom timeframes are forwarded."""
        mock_client = MagicMock()
        stream = KlineStream(mock_client, timeframes=["5m", "1h"])
        callback = AsyncMock()
        stream.on_candle_closed = callback

        msg = {
            "stream": "solusdt@kline_5m",
            "data": {
                "e": "kline",
                "k": {
                    "s": "SOLUSDT",
                    "i": "5m",
                    "o": "150.0",
                    "h": "155.0",
                    "l": "149.0",
                    "c": "153.0",
                    "v": "1000.0",
                    "t": 1700000000000,
                    "x": True,
                },
            },
        }
        await stream._handle_message("SOLUSDT", msg)

        callback.assert_awaited_once()
        args = callback.call_args[0]
        assert args[0] == "SOLUSDT"
        assert args[1] == "5m"

    @pytest.mark.asyncio
    async def test_handle_message_rejects_non_configured_timeframe(self) -> None:
        """Verify candles outside configured timeframes are dropped."""
        mock_client = MagicMock()
        stream = KlineStream(mock_client, timeframes=["5m", "1h"])
        callback = AsyncMock()
        stream.on_candle_closed = callback

        msg = {
            "stream": "solusdt@kline_3m",
            "data": {
                "e": "kline",
                "k": {
                    "s": "SOLUSDT",
                    "i": "3m",
                    "o": "150.0",
                    "h": "155.0",
                    "l": "149.0",
                    "c": "153.0",
                    "v": "1000.0",
                    "t": 1700000000000,
                    "x": True,
                },
            },
        }
        await stream._handle_message("SOLUSDT", msg)
        callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_single_timeframe_works(self) -> None:
        """Verify KlineStream works with a single configured timeframe."""
        mock_client = MagicMock()
        stream = KlineStream(mock_client, timeframes=["1m"])
        callback = AsyncMock()
        stream.on_candle_closed = callback

        msg = {
            "stream": "ethusdt@kline_1m",
            "data": {
                "e": "kline",
                "k": {
                    "s": "ETHUSDT",
                    "i": "1m",
                    "o": "3200.0",
                    "h": "3250.0",
                    "l": "3190.0",
                    "c": "3240.0",
                    "v": "5000.0",
                    "t": 1700000000000,
                    "x": True,
                },
            },
        }
        await stream._handle_message("ETHUSDT", msg)

        callback.assert_awaited_once()
        assert callback.call_args[0][1] == "1m"
