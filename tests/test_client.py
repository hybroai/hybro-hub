"""Unit tests for HybroGateway client."""

import json

import httpx
import pytest
import respx

from hybro_hub.client import HybroGateway
from hybro_hub.errors import (
    AuthError,
    AgentNotFoundError,
    RateLimitError,
    GatewayError,
)
from hybro_hub.models import AgentInfo

BASE = "https://api.hybro.ai/api/v1"


@pytest.fixture
def gw():
    return HybroGateway(api_key="hba_test_key", base_url=BASE)


# =============================================================================
# discover()
# =============================================================================


class TestDiscover:
    @respx.mock
    @pytest.mark.asyncio
    async def test_returns_agents(self, gw):
        payload = {
            "query": "legal",
            "agents": [
                {"agent_id": "a1", "agent_card": {"name": "Legal Agent", "url": "https://gw/a1"}, "match_score": 0.95}
            ],
            "count": 1,
        }
        respx.post(f"{BASE}/gateway/agents/discover").mock(
            return_value=httpx.Response(200, json=payload)
        )
        agents = await gw.discover("legal")
        assert len(agents) == 1
        assert agents[0].agent_id == "a1"
        assert agents[0].match_score == 0.95

    @respx.mock
    @pytest.mark.asyncio
    async def test_discover_with_limit(self, gw):
        payload = {"query": "x", "agents": [], "count": 0}
        route = respx.post(f"{BASE}/gateway/agents/discover").mock(
            return_value=httpx.Response(200, json=payload)
        )
        await gw.discover("x", limit=3)
        req_body = json.loads(route.calls[0].request.content)
        assert req_body["limit"] == 3

    @respx.mock
    @pytest.mark.asyncio
    async def test_discover_auth_error(self, gw):
        respx.post(f"{BASE}/gateway/agents/discover").mock(
            return_value=httpx.Response(401, json={"detail": {"error": "invalid_key", "message": "Invalid API key"}})
        )
        with pytest.raises(AuthError):
            await gw.discover("test")


# =============================================================================
# send()
# =============================================================================


class TestSend:
    @respx.mock
    @pytest.mark.asyncio
    async def test_send_returns_response(self, gw):
        resp_payload = {"result": {"status": {"state": "completed"}}}
        respx.post(f"{BASE}/gateway/agents/a1/message/send").mock(
            return_value=httpx.Response(200, json=resp_payload)
        )
        result = await gw.send("a1", "Hello")
        assert result["result"]["status"]["state"] == "completed"

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_not_found(self, gw):
        respx.post(f"{BASE}/gateway/agents/bad/message/send").mock(
            return_value=httpx.Response(404, json={"detail": {"error": "agent_not_found", "message": "Not found"}})
        )
        with pytest.raises(AgentNotFoundError):
            await gw.send("bad", "Hello")

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_rate_limited(self, gw):
        respx.post(f"{BASE}/gateway/agents/a1/message/send").mock(
            return_value=httpx.Response(
                429,
                json={"detail": {"error": "rate_limit_exceeded", "message": "Too many"}},
                headers={"Retry-After": "120"},
            )
        )
        with pytest.raises(RateLimitError) as exc:
            await gw.send("a1", "Hello")
        assert exc.value.retry_after == 120


# =============================================================================
# get_card()
# =============================================================================


class TestGetCard:
    @respx.mock
    @pytest.mark.asyncio
    async def test_get_card(self, gw):
        card_resp = {"agent_id": "a1", "agent_card": {"name": "A", "url": "https://gw/a1"}}
        respx.get(f"{BASE}/gateway/agents/a1/card").mock(
            return_value=httpx.Response(200, json=card_resp)
        )
        result = await gw.get_card("a1")
        assert result["agent_id"] == "a1"


# =============================================================================
# Message construction
# =============================================================================


class TestBuildMessage:
    def test_builds_valid_message(self):
        msg = HybroGateway._build_message("Hello")
        assert msg["role"] == "user"
        assert msg["parts"][0]["text"] == "Hello"
        assert "messageId" in msg
        assert "contextId" in msg

    def test_custom_context_id(self):
        msg = HybroGateway._build_message("Hi", context_id="ctx-123")
        assert msg["contextId"] == "ctx-123"


# =============================================================================
# Error mapping
# =============================================================================


class TestErrorMapping:
    @respx.mock
    @pytest.mark.asyncio
    async def test_generic_error(self, gw):
        respx.post(f"{BASE}/gateway/agents/a1/message/send").mock(
            return_value=httpx.Response(500, json={"detail": "Internal error"})
        )
        with pytest.raises(GatewayError) as exc:
            await gw.send("a1", "Hello")
        assert exc.value.status_code == 500
