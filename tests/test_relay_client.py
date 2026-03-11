"""Tests for hub.relay_client."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from hub.config import PublishQueueConfig
from hub.relay_client import RelayClient, _MAX_RETRY_DELAY, _SSE_TIMEOUT


@pytest.fixture
def relay():
    return RelayClient(
        gateway_url="https://api.hybro.ai",
        hub_id="hub-123",
        api_key="hba_test",
    )


@pytest.fixture
def relay_with_queue(relay, tmp_path):
    cfg = PublishQueueConfig(max_retries_critical=3, max_retries_normal=2)
    relay.init_queue(tmp_path / "queue.db", cfg)
    yield relay
    relay.close_queue()


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


def _make_error_resp(status_code):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = "error"
    exc = httpx.HTTPStatusError("err", request=MagicMock(), response=resp)
    resp.raise_for_status = MagicMock(side_effect=exc)
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
    async def test_publish_success_no_queue(self, relay):
        """On success, no disk queue interaction occurs."""
        mock_resp = _make_mock_resp(204)
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.is_closed = False
        _attach_mock_client(relay, mock_client)

        await relay.publish("room-1", [{"type": "agent_response", "agent_message_id": "m1"}])
        mock_client.post.assert_called_once()
        assert relay._queue is None  # queue not initialised

    @pytest.mark.asyncio
    async def test_publish_uses_api_key_header(self, relay):
        mock_resp = _make_mock_resp(204)
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.is_closed = False
        _attach_mock_client(relay, mock_client)

        await relay.publish("room-1", [{"type": "agent_response", "agent_message_id": "m1"}])
        call_kwargs = mock_client.post.call_args
        assert call_kwargs[1]["headers"]["X-API-Key"] == "hba_test"

    @pytest.mark.asyncio
    async def test_publish_does_not_raise_on_network_error(self, relay):
        """publish() must never raise — failures are swallowed and logged."""
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
        mock_client.is_closed = False
        _attach_mock_client(relay, mock_client)

        # Should not raise
        await relay.publish("room-1", [{"type": "agent_response", "agent_message_id": "m1"}])

    @pytest.mark.asyncio
    async def test_publish_does_not_raise_on_4xx(self, relay):
        resp = _make_error_resp(403)
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=resp)
        mock_client.is_closed = False
        _attach_mock_client(relay, mock_client)

        await relay.publish("room-1", [{"type": "agent_response", "agent_message_id": "m1"}])

    @pytest.mark.asyncio
    async def test_publish_queues_event_on_transient_failure(self, relay_with_queue):
        """Transient network failures should result in the event being queued."""
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
        mock_client.is_closed = False
        _attach_mock_client(relay_with_queue, mock_client)

        await relay_with_queue.publish(
            "room-1", [{"type": "agent_response", "agent_message_id": "m1"}]
        )

        stats = await relay_with_queue.get_queue_stats()
        assert stats["total"] == 1
        assert stats["critical"] == 1

    @pytest.mark.asyncio
    async def test_publish_does_not_queue_4xx_event(self, relay_with_queue):
        """Permanent 4xx errors should not be queued."""
        resp = _make_error_resp(403)
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=resp)
        mock_client.is_closed = False
        _attach_mock_client(relay_with_queue, mock_client)

        await relay_with_queue.publish(
            "room-1", [{"type": "agent_response", "agent_message_id": "m1"}]
        )

        stats = await relay_with_queue.get_queue_stats()
        assert stats["total"] == 0


class TestTryPublishWithRetry:
    @pytest.mark.asyncio
    async def test_returns_true_on_success(self, relay):
        mock_resp = _make_mock_resp(204)
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.is_closed = False
        _attach_mock_client(relay, mock_client)

        delivered, error_type = await relay._try_publish_with_retry("room", [{}], max_retries=1)
        assert delivered is True
        assert error_type is None

    @pytest.mark.asyncio
    async def test_returns_permanent_on_4xx(self, relay):
        resp = _make_error_resp(403)
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=resp)
        mock_client.is_closed = False
        _attach_mock_client(relay, mock_client)

        delivered, error_type = await relay._try_publish_with_retry("room", [{}], max_retries=1)
        assert delivered is False
        assert error_type == "permanent"

    @pytest.mark.asyncio
    async def test_returns_transient_on_connect_error(self, relay):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
        mock_client.is_closed = False
        _attach_mock_client(relay, mock_client)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            delivered, error_type = await relay._try_publish_with_retry(
                "room", [{}], max_retries=1
            )
        assert delivered is False
        assert error_type == "transient"


class TestQueueManagement:
    def test_init_queue_creates_queue(self, relay, tmp_path):
        cfg = PublishQueueConfig()
        relay.init_queue(tmp_path / "q.db", cfg)
        assert relay._queue is not None
        relay.close_queue()
        assert relay._queue is None

    @pytest.mark.asyncio
    async def test_get_queue_stats_without_queue(self, relay):
        stats = await relay.get_queue_stats()
        assert stats == {"total": 0, "critical": 0, "normal": 0}


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
        assert _SSE_TIMEOUT.read == 90

