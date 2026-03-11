"""Tests for hub.relay_client."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from hub.relay_client import RelayClient


@pytest.fixture
def relay():
    return RelayClient(
        gateway_url="https://api.hybro.ai",
        hub_id="hub-123",
        api_key="hba_test",
    )


def _attach_mock_client(relay, mock_client):
    """Wire mock_client into both _http_client and _sse_client slots."""
    relay._http_client = mock_client
    relay._sse_client = mock_client


def _make_mock_resp(status_code=200, json_data=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.raise_for_status = MagicMock()
    if json_data is not None:
        resp.json.return_value = json_data
    return resp


class TestRegister:
    @pytest.mark.asyncio
    async def test_register_success(self, relay):
        mock_resp = _make_mock_resp(200, {"hub_id": "hub-123", "user_id": "user-1"})
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.is_closed = False
        _attach_mock_client(relay, mock_client)

        result = await relay.register()
        assert result["hub_id"] == "hub-123"
        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args
        assert "X-API-Key" in call_kwargs[1]["headers"]


class TestSyncAgents:
    @pytest.mark.asyncio
    async def test_sync_agents(self, relay):
        mock_resp = _make_mock_resp(200, {"synced": [{"agent_id": "a1", "local_agent_id": "l1"}]})
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.is_closed = False
        _attach_mock_client(relay, mock_client)

        synced = await relay.sync_agents([{"local_agent_id": "l1", "name": "Test"}])
        assert len(synced) == 1
        assert synced[0]["agent_id"] == "a1"


class TestPublish:
    @pytest.mark.asyncio
    async def test_publish_success(self, relay):
        mock_resp = _make_mock_resp(204)
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.is_closed = False
        _attach_mock_client(relay, mock_client)

        await relay.publish("room-1", [{"type": "agent_response", "agent_message_id": "m1", "data": {}}])
        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args
        assert call_kwargs[1]["headers"]["X-API-Key"] == "hba_test"

    @pytest.mark.asyncio
    async def test_publish_raises_on_403(self, relay):
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "Forbidden", request=MagicMock(), response=mock_resp
            )
        )
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.is_closed = False
        _attach_mock_client(relay, mock_client)

        with pytest.raises(httpx.HTTPStatusError):
            await relay.publish("room-1", [{"type": "agent_response"}])

    @pytest.mark.asyncio
    async def test_publish_raises_on_network_error(self, relay):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
        mock_client.is_closed = False
        _attach_mock_client(relay, mock_client)

        with pytest.raises(httpx.ConnectError):
            await relay.publish("room-1", [{"type": "agent_response"}])


class TestHeartbeat:
    @pytest.mark.asyncio
    async def test_heartbeat_success(self, relay):
        mock_resp = _make_mock_resp(204)
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.is_closed = False
        _attach_mock_client(relay, mock_client)

        await relay.heartbeat()
        call_kwargs = mock_client.post.call_args
        assert call_kwargs[1]["headers"]["X-API-Key"] == "hba_test"


class TestGetStatus:
    @pytest.mark.asyncio
    async def test_get_status(self, relay):
        mock_resp = _make_mock_resp(200, {"hubs": [{"hub_id": "hub-123", "is_online": True}]})
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.is_closed = False
        _attach_mock_client(relay, mock_client)

        data = await relay.get_status()
        assert data["hubs"][0]["is_online"] is True


class TestTimeoutConfig:
    def test_separate_clients_created(self, relay):
        """Verify that http and sse clients are distinct slots."""
        assert relay._http_client is None
        assert relay._sse_client is None

    def test_sse_has_read_timeout(self, relay):
        """SSE client should use a 90s read timeout for zombie detection."""
        from hub.relay_client import _SSE_TIMEOUT
        assert _SSE_TIMEOUT.read == 90
