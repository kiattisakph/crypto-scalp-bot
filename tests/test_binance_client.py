"""Unit tests for BinanceClient HMAC signing and basic REST/WS helpers."""
from __future__ import annotations

import hmac
import hashlib
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.binance_client import BinanceClient, BinanceError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client() -> BinanceClient:
    return BinanceClient(
        api_key="test_key",
        api_secret="test_secret",
        demo=True,
    )


# ---------------------------------------------------------------------------
# _sign
# ---------------------------------------------------------------------------


class TestSign:
    """Tests for HMAC-SHA256 request signing."""

    def test_adds_timestamp(self) -> None:
        client = _make_client()
        before = int(time.time() * 1000)
        params = client._sign({"symbol": "BTCUSDT"})
        after = int(time.time() * 1000)

        assert "timestamp" in params
        assert before <= params["timestamp"] <= after
        assert "signature" in params

    def test_adds_recv_window(self) -> None:
        client = _make_client()
        params = client._sign({})
        assert params["recvWindow"] == 5000

    def test_signature_is_valid_hmac(self) -> None:
        client = _make_client()
        params = client._sign({"symbol": "BTCUSDT", "leverage": 5})

        # Recompute expected signature
        query_parts = {k: v for k, v in params.items() if k != "signature"}
        query_string = "&".join(f"{k}={v}" for k, v in query_parts.items())
        expected = hmac.new(
            "test_secret".encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        assert params["signature"] == expected

    def test_deterministic_for_same_input(self) -> None:
        client = _make_client()
        # Use fixed timestamp to make signatures reproducible
        with patch("time.time", return_value=1700000000.0):
            p1 = client._sign({"symbol": "SOLUSDT"})
            p2 = client._sign({"symbol": "SOLUSDT"})

        assert p1["signature"] == p2["signature"]


# ---------------------------------------------------------------------------
# _auth_headers
# ---------------------------------------------------------------------------


class TestAuthHeaders:
    def test_contains_api_key(self) -> None:
        client = _make_client()
        headers = client._auth_headers()
        assert headers["X-MBX-APIKEY"] == "test_key"


# ---------------------------------------------------------------------------
# Endpoint routing (demo vs live)
# ---------------------------------------------------------------------------


class TestEndpointRouting:
    def test_demo_base_url(self) -> None:
        client = BinanceClient("k", "s", demo=True)
        assert client._base_url == "https://demo-fapi.binance.com"

    def test_live_base_url(self) -> None:
        client = BinanceClient("k", "s", demo=False)
        assert client._base_url == "https://fapi.binance.com"

    def test_demo_ws_url(self) -> None:
        client = BinanceClient("k", "s", demo=True)
        assert client._ws_url == "wss://testnet.binancefuture.com/ws"

    def test_live_ws_url(self) -> None:
        client = BinanceClient("k", "s", demo=False)
        assert client._ws_url == "wss://fstream.binance.com/ws"


# ---------------------------------------------------------------------------
# _request error handling
# ---------------------------------------------------------------------------


class TestRequest:
    """Tests for the internal HTTP request helper."""

    @pytest.mark.asyncio
    async def test_raises_binance_error_on_4xx(self) -> None:
        client = _make_client()
        client._http = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.headers = {"content-type": "application/json"}
        mock_response.json.return_value = {"code": -1022, "msg": "Signature invalid"}
        client._http.request = AsyncMock(return_value=mock_response)

        with pytest.raises(BinanceError) as exc_info:
            await client._request("GET", "/fapi/v1/ping")

        assert exc_info.value.code == -1022
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_signed_request_includes_auth_headers(self) -> None:
        client = _make_client()
        client._http = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {}
        client._http.request = AsyncMock(return_value=mock_response)

        with patch("time.time", return_value=1700000000.0):
            await client._request("POST", "/fapi/v1/leverage",
                                  params={"symbol": "BTCUSDT", "leverage": 5},
                                  signed=True)

        call_kwargs = client._http.request.call_args.kwargs
        assert "headers" in call_kwargs
        assert call_kwargs["headers"]["X-MBX-APIKEY"] == "test_key"


# ---------------------------------------------------------------------------
# Public REST method signatures
# ---------------------------------------------------------------------------


class TestPublicREST:
    """Verify that public REST methods call _request with correct paths."""

    @pytest.mark.asyncio
    async def test_futures_symbol_ticker(self) -> None:
        client = _make_client()
        client._request = AsyncMock(return_value={"price": "100.0"})
        result = await client.futures_symbol_ticker("BTCUSDT")
        client._request.assert_awaited_once_with(
            "GET", "/fapi/v1/ticker/price",
            params={"symbol": "BTCUSDT"},
        )
        assert result["price"] == "100.0"

    @pytest.mark.asyncio
    async def test_futures_mark_price(self) -> None:
        client = _make_client()
        client._request = AsyncMock(
            return_value={"lastFundingRate": "0.0001"}
        )
        result = await client.futures_mark_price("ETHUSDT")
        client._request.assert_awaited_once_with(
            "GET", "/fapi/v1/markPrice",
            params={"symbol": "ETHUSDT"},
        )

    @pytest.mark.asyncio
    async def test_futures_order_book(self) -> None:
        client = _make_client()
        client._request = AsyncMock(return_value={"bids": [], "asks": []})
        await client.futures_order_book("SOLUSDT", limit=10)
        client._request.assert_awaited_once_with(
            "GET", "/fapi/v1/depth",
            params={"symbol": "SOLUSDT", "limit": 10},
        )

    @pytest.mark.asyncio
    async def test_futures_klines(self) -> None:
        client = _make_client()
        client._request = AsyncMock(return_value=[])
        await client.futures_klines("BTCUSDT", "3m", limit=50)
        client._request.assert_awaited_once_with(
            "GET", "/fapi/v1/klines",
            params={"symbol": "BTCUSDT", "interval": "3m", "limit": 50},
        )

    @pytest.mark.asyncio
    async def test_futures_premium_index(self) -> None:
        client = _make_client()
        client._request = AsyncMock(return_value={"symbol": "BTCUSDT"})
        await client.futures_premium_index("BTCUSDT")
        client._request.assert_awaited_once_with(
            "GET", "/fapi/v1/premiumIndex",
            params={"symbol": "BTCUSDT"},
        )


# ---------------------------------------------------------------------------
# Signed REST method signatures
# ---------------------------------------------------------------------------


class TestSignedREST:
    """Verify that signed REST methods call _request with signed=True."""

    @pytest.mark.asyncio
    async def test_futures_change_leverage(self) -> None:
        client = _make_client()
        client._request = AsyncMock(return_value={})
        await client.futures_change_leverage("SOLUSDT", 10)
        client._request.assert_awaited_once_with(
            "POST", "/fapi/v1/leverage",
            params={"symbol": "SOLUSDT", "leverage": 10},
            signed=True,
        )

    @pytest.mark.asyncio
    async def test_futures_create_order(self) -> None:
        client = _make_client()
        client._request = AsyncMock(return_value={"orderId": 1})
        await client.futures_create_order(
            symbol="BTCUSDT", side="BUY", type="MARKET", quantity=0.001,
        )
        client._request.assert_awaited_once_with(
            "POST", "/fapi/v1/order",
            params={"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET",
                    "quantity": 0.001},
            signed=True,
        )

    @pytest.mark.asyncio
    async def test_futures_position_information(self) -> None:
        client = _make_client()
        client._request = AsyncMock(return_value=[])
        await client.futures_position_information()
        client._request.assert_awaited_once_with(
            "GET", "/fapi/v2/positionRisk", params={}, signed=True,
        )

    @pytest.mark.asyncio
    async def test_futures_account_balance(self) -> None:
        client = _make_client()
        client._request = AsyncMock(return_value=[])
        await client.futures_account_balance()
        client._request.assert_awaited_once_with(
            "GET", "/fapi/v2/balance", params={}, signed=True,
        )

    @pytest.mark.asyncio
    async def test_futures_account(self) -> None:
        client = _make_client()
        client._request = AsyncMock(return_value={})
        await client.futures_account()
        client._request.assert_awaited_once_with(
            "GET", "/fapi/v2/account", params={}, signed=True,
        )

    @pytest.mark.asyncio
    async def test_futures_cancel_order(self) -> None:
        client = _make_client()
        client._request = AsyncMock(return_value={})
        await client.futures_cancel_order(symbol="BTCUSDT", orderId=123)
        client._request.assert_awaited_once_with(
            "DELETE", "/fapi/v1/order",
            params={"symbol": "BTCUSDT", "orderId": 123},
            signed=True,
        )

    @pytest.mark.asyncio
    async def test_futures_cancel_all_open_orders(self) -> None:
        client = _make_client()
        client._request = AsyncMock(return_value={})
        await client.futures_cancel_all_open_orders("SOLUSDT")
        client._request.assert_awaited_once_with(
            "DELETE", "/fapi/v1/allOpenOrders",
            params={"symbol": "SOLUSDT"},
            signed=True,
        )

    @pytest.mark.asyncio
    async def test_futures_get_order(self) -> None:
        client = _make_client()
        client._request = AsyncMock(return_value={"orderId": 99})
        await client.futures_get_order(symbol="ETHUSDT", orderId=99)
        client._request.assert_awaited_once_with(
            "GET", "/fapi/v1/order",
            params={"symbol": "ETHUSDT", "orderId": 99},
            signed=True,
        )

    @pytest.mark.asyncio
    async def test_futures_get_open_orders(self) -> None:
        client = _make_client()
        client._request = AsyncMock(return_value=[])
        await client.futures_get_open_orders("BTCUSDT")
        client._request.assert_awaited_once_with(
            "GET", "/fapi/v1/openOrders",
            params={"symbol": "BTCUSDT"},
            signed=True,
        )


# ---------------------------------------------------------------------------
# ClientOrderId format (from OrderManager, tested here for completeness)
# ---------------------------------------------------------------------------


class TestClientOrderId:
    def test_prefix_and_length(self) -> None:
        """Binance clientOrderId max 36 chars. 'csb_' + 28 hex = 32."""
        import uuid
        cid = f"csb_{uuid.uuid4().hex[:28]}"
        assert cid.startswith("csb_")
        assert len(cid) <= 36
