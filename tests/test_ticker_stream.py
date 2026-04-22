"""Unit tests for TickerStream message parsing and callback dispatch."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.models import TickerData
from streams.ticker_stream import TickerStream


# ---------------------------------------------------------------------------
# _parse_tickers
# ---------------------------------------------------------------------------


class TestParseTickers:
    """Tests for TickerStream._parse_tickers static method."""

    def test_parses_valid_ticker_entries(self) -> None:
        raw = [
            {"s": "SOLUSDT", "P": "5.23", "c": "150.50", "q": "50000000"},
            {"s": "ETHUSDT", "P": "-1.10", "c": "3200.00", "q": "120000000"},
        ]
        result = TickerStream._parse_tickers(raw)

        assert len(result) == 2
        assert result[0].symbol == "SOLUSDT"
        assert result[0].price_change_pct == pytest.approx(5.23)
        assert result[0].last_price == pytest.approx(150.50)
        assert result[0].quote_volume == pytest.approx(50_000_000)
        assert result[1].symbol == "ETHUSDT"

    def test_skips_malformed_entries(self) -> None:
        raw = [
            {"s": "SOLUSDT", "P": "5.23", "c": "150.50", "q": "50000000"},
            {"s": "BADENTRY"},  # missing fields
            {"s": "ETHUSDT", "P": "1.0", "c": "3200", "q": "100000000"},
        ]
        result = TickerStream._parse_tickers(raw)

        assert len(result) == 2
        assert result[0].symbol == "SOLUSDT"
        assert result[1].symbol == "ETHUSDT"

    def test_empty_list_returns_empty(self) -> None:
        assert TickerStream._parse_tickers([]) == []

    def test_handles_non_numeric_values(self) -> None:
        raw = [{"s": "SOLUSDT", "P": "not_a_number", "c": "150", "q": "100"}]
        result = TickerStream._parse_tickers(raw)
        assert len(result) == 0


# ---------------------------------------------------------------------------
# _handle_message
# ---------------------------------------------------------------------------


class TestHandleMessage:
    """Tests for TickerStream._handle_message."""

    @pytest.fixture
    def stream(self) -> TickerStream:
        mock_client = MagicMock()
        ts = TickerStream(mock_client)
        return ts

    @pytest.mark.asyncio
    async def test_invokes_callback_with_parsed_tickers(
        self, stream: TickerStream
    ) -> None:
        callback = AsyncMock()
        stream.on_ticker_update = callback

        msg = {
            "stream": "!ticker@arr",
            "data": [
                {"s": "SOLUSDT", "P": "5.0", "c": "150.0", "q": "50000000"},
            ],
        }
        await stream._handle_message(msg)

        callback.assert_awaited_once()
        tickers = callback.call_args[0][0]
        assert len(tickers) == 1
        assert tickers[0].symbol == "SOLUSDT"

    @pytest.mark.asyncio
    async def test_skips_message_without_data_field(
        self, stream: TickerStream
    ) -> None:
        callback = AsyncMock()
        stream.on_ticker_update = callback

        await stream._handle_message({"stream": "!ticker@arr"})
        callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_message_with_non_list_data(
        self, stream: TickerStream
    ) -> None:
        callback = AsyncMock()
        stream.on_ticker_update = callback

        await stream._handle_message({"stream": "!ticker@arr", "data": "bad"})
        callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_crash_when_callback_not_set(
        self, stream: TickerStream
    ) -> None:
        stream.on_ticker_update = None
        msg = {
            "stream": "!ticker@arr",
            "data": [
                {"s": "SOLUSDT", "P": "5.0", "c": "150.0", "q": "50000000"},
            ],
        }
        # Should not raise
        await stream._handle_message(msg)

    @pytest.mark.asyncio
    async def test_handles_empty_data_array(
        self, stream: TickerStream
    ) -> None:
        callback = AsyncMock()
        stream.on_ticker_update = callback

        msg = {"stream": "!ticker@arr", "data": []}
        await stream._handle_message(msg)

        callback.assert_awaited_once()
        tickers = callback.call_args[0][0]
        assert tickers == []


# ---------------------------------------------------------------------------
# Reconnect task tracking
# ---------------------------------------------------------------------------


class TestReconnectTaskTracking:
    """Tests for TickerStream reconnect task lifecycle management."""

    @pytest.fixture
    def stream(self) -> TickerStream:
        mock_client = MagicMock()
        return TickerStream(mock_client)

    def test_reconnect_task_initially_none(self, stream: TickerStream) -> None:
        assert stream._reconnect_task is None

    @pytest.mark.asyncio
    async def test_listen_stores_reconnect_task_on_unexpected_disconnect(
        self, stream: TickerStream
    ) -> None:
        """When _listen ends unexpectedly while connected, the reconnect
        task must be stored in _reconnect_task so disconnect() can cancel it.
        """
        # Simulate: stream thinks it's connected, socket breaks immediately
        stream._connected = True

        mock_socket = MagicMock()
        mock_stream = AsyncMock()
        mock_stream.recv = AsyncMock(side_effect=Exception("ws broke"))
        mock_socket.__aenter__ = AsyncMock(return_value=mock_stream)
        mock_socket.__aexit__ = AsyncMock(return_value=None)
        stream._socket = mock_socket

        # Patch _reconnect_loop to just record it was called, then stop
        reconnect_started = asyncio.Event()

        async def fake_reconnect_loop() -> None:
            reconnect_started.set()
            # Wait until cancelled
            try:
                await asyncio.sleep(999)
            except asyncio.CancelledError:
                raise

        with patch.object(stream, "_reconnect_loop", side_effect=fake_reconnect_loop):
            # Run _listen — it will break on recv, then create reconnect task
            await stream._listen()

        # Give the event loop a tick to start the task
        await asyncio.sleep(0)

        assert stream._reconnect_task is not None
        assert not stream._reconnect_task.done()

        # Cleanup
        stream._connected = False
        stream._reconnect_task.cancel()
        try:
            await stream._reconnect_task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_disconnect_cancels_reconnect_task(
        self, stream: TickerStream
    ) -> None:
        """disconnect() must cancel a running reconnect task."""
        stream._connected = True

        cancelled = asyncio.Event()

        async def slow_reconnect() -> None:
            try:
                await asyncio.sleep(999)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        task = asyncio.create_task(slow_reconnect())
        stream._reconnect_task = task

        # Let the task start running so it reaches the sleep
        await asyncio.sleep(0)

        await stream.disconnect()

        assert cancelled.is_set()
        assert stream._reconnect_task is None

    @pytest.mark.asyncio
    async def test_disconnect_cancels_both_listen_and_reconnect_tasks(
        self, stream: TickerStream
    ) -> None:
        """disconnect() must cancel both tasks if both are alive."""
        stream._connected = True

        listen_cancelled = asyncio.Event()
        reconnect_cancelled = asyncio.Event()

        async def fake_listen() -> None:
            try:
                await asyncio.sleep(999)
            except asyncio.CancelledError:
                listen_cancelled.set()
                raise

        async def fake_reconnect() -> None:
            try:
                await asyncio.sleep(999)
            except asyncio.CancelledError:
                reconnect_cancelled.set()
                raise

        stream._listen_task = asyncio.create_task(fake_listen())
        stream._reconnect_task = asyncio.create_task(fake_reconnect())

        # Let both tasks start running
        await asyncio.sleep(0)

        await stream.disconnect()

        assert listen_cancelled.is_set()
        assert reconnect_cancelled.is_set()
        assert stream._listen_task is None
        assert stream._reconnect_task is None

    @pytest.mark.asyncio
    async def test_reconnect_clears_own_ref_on_success(
        self, stream: TickerStream
    ) -> None:
        """After successful reconnect, _reconnect_task should be None
        and _listen_task should be the new listen task.
        """
        stream._connected = True

        mock_socket = MagicMock()
        mock_stream = AsyncMock()
        # Make listen break immediately so we can inspect state
        mock_stream.recv = AsyncMock(side_effect=asyncio.CancelledError)
        mock_socket.__aenter__ = AsyncMock(return_value=mock_stream)
        mock_socket.__aexit__ = AsyncMock(return_value=None)

        mock_bm = MagicMock()
        mock_bm.futures_multiplex_socket.return_value = mock_socket

        with patch("streams.ticker_stream.BinanceSocketManager", return_value=mock_bm):
            with patch("streams.ticker_stream.asyncio.sleep", new_callable=AsyncMock):
                # Run reconnect loop — it should succeed on first attempt
                await stream._reconnect_loop()

        assert stream._reconnect_task is None
        assert stream._listen_task is not None

        # Cleanup
        stream._connected = False
        stream._listen_task.cancel()
        try:
            await stream._listen_task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# Exception visibility
# ---------------------------------------------------------------------------


class TestExceptionVisibility:
    """Tests for _on_task_done exception logging."""

    @pytest.mark.asyncio
    async def test_on_task_done_logs_exception(self) -> None:
        """_on_task_done should log when a task has an unhandled exception."""

        async def failing_task() -> None:
            raise RuntimeError("boom")

        task = asyncio.create_task(failing_task())
        try:
            await task
        except RuntimeError:
            pass

        # Should not raise — just logs
        TickerStream._on_task_done(task)

    @pytest.mark.asyncio
    async def test_on_task_done_ignores_cancelled(self) -> None:
        """_on_task_done should silently ignore cancelled tasks."""

        async def cancellable() -> None:
            await asyncio.sleep(999)

        task = asyncio.create_task(cancellable())
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Should not raise
        TickerStream._on_task_done(task)

    @pytest.mark.asyncio
    async def test_on_task_done_ignores_successful(self) -> None:
        """_on_task_done should do nothing for successfully completed tasks."""

        async def ok_task() -> None:
            pass

        task = asyncio.create_task(ok_task())
        await task

        # Should not raise
        TickerStream._on_task_done(task)
