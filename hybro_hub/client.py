"""HybroGateway — async client for the Hybro Gateway API."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import httpx

from hybro_hub._sse import iter_sse_events
from hybro_hub.errors import (
    AccessDeniedError,
    AgentCommunicationError,
    AgentNotFoundError,
    AuthError,
    GatewayError,
    RateLimitError,
    raise_for_status,
)
from hybro_hub.models import AgentInfo, DiscoveryResponse, StreamEvent

_DEFAULT_BASE_URL = "https://api.hybro.ai/api/v1"


class HybroGateway:
    """Async client for the Hybro Gateway API.

    Usage::

        async with HybroGateway(api_key="hybro_...") as gw:
            agents = await gw.discover("legal contract review")
            result = await gw.send(agents[0].agent_id, "Review this contract")
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = _DEFAULT_BASE_URL,
        *,
        timeout: float = 120.0,
        client: httpx.AsyncClient | None = None,
    ):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            headers={"X-API-Key": self._api_key},
            timeout=timeout,
        )
        if client is not None and "X-API-Key" not in (client.headers or {}):
            self._client.headers["X-API-Key"] = self._api_key

    async def __aenter__(self) -> HybroGateway:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def discover(self, query: str, *, limit: int | None = None) -> list[AgentInfo]:
        """Discover agents matching a natural-language query."""
        payload: dict = {"query": query}
        if limit is not None:
            payload["limit"] = limit

        resp = await self._client.post("/gateway/agents/discover", json=payload)
        self._raise_for_status(resp)
        body = DiscoveryResponse.model_validate(resp.json())
        return body.agents

    async def send(
        self,
        agent_id: str,
        text: str,
        *,
        context_id: str | None = None,
    ) -> dict:
        """Send a synchronous message and return the A2A response payload."""
        message = self._build_message(text, context_id=context_id)
        resp = await self._client.post(
            f"/gateway/agents/{agent_id}/message/send",
            json={"message": message},
        )
        self._raise_for_status(resp)
        return resp.json()

    async def stream(
        self,
        agent_id: str,
        text: str,
        *,
        context_id: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream a message and yield SSE events as they arrive."""
        message = self._build_message(text, context_id=context_id)
        async for event in iter_sse_events(
            self._client,
            "POST",
            f"/gateway/agents/{agent_id}/message/stream",
            json={"message": message},
        ):
            yield event

    async def get_card(self, agent_id: str) -> dict:
        """Fetch an agent's card (with gateway-masked URL)."""
        resp = await self._client.get(f"/gateway/agents/{agent_id}/card")
        self._raise_for_status(resp)
        return resp.json()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_message(text: str, *, context_id: str | None = None) -> dict:
        return {
            "role": "user",
            "parts": [{"kind": "text", "text": text}],
            "messageId": str(uuid4()),
            "contextId": context_id or str(uuid4()),
        }

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        if resp.is_success:
            return
        try:
            body = resp.json()
        except Exception:
            body = {}
        raise_for_status(
            resp.status_code,
            body=body,
            headers=dict(resp.headers),
            fallback_text=resp.text,
        )
