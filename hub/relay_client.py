"""Relay client — SSE subscribe + HTTP publish to hybro.ai relay service.

Maintains a persistent SSE connection to the cloud relay and provides
methods to publish events, register the hub, and sync agents.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

import httpx
from httpx_sse import aconnect_sse

logger = logging.getLogger(__name__)

MAX_RECONNECT_DELAY = 60

# Short timeout for regular HTTP calls (register, sync, publish, status)
_HTTP_TIMEOUT = httpx.Timeout(connect=10, read=30, write=10, pool=10)

# SSE connections are long-lived. The backend sends heartbeats every ~30s,
# so a 90s read timeout (3x headroom) detects zombie connections while
# tolerating normal jitter.
_SSE_TIMEOUT = httpx.Timeout(connect=10, read=90, write=10, pool=10)


class RelayClient:
    """Client for the hybro.ai relay service."""

    def __init__(
        self,
        *,
        gateway_url: str,
        hub_id: str,
        api_key: str,
    ) -> None:
        self._base = gateway_url.rstrip("/")
        self._hub_id = hub_id
        self._api_key = api_key
        self._http_client: httpx.AsyncClient | None = None
        self._sse_client: httpx.AsyncClient | None = None
        self._should_stop = False

    # ──── Lifecycle ────

    async def _get_http_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=_HTTP_TIMEOUT)
        return self._http_client

    async def _get_sse_client(self) -> httpx.AsyncClient:
        if self._sse_client is None or self._sse_client.is_closed:
            self._sse_client = httpx.AsyncClient(timeout=_SSE_TIMEOUT)
        return self._sse_client

    async def close(self) -> None:
        self._should_stop = True
        for client in (self._http_client, self._sse_client):
            if client is not None:
                await client.aclose()
        self._http_client = None
        self._sse_client = None

    # ──── Registration ────

    async def register(self) -> dict:
        """POST /api/v1/relay/hub/register"""
        client = await self._get_http_client()
        resp = await client.post(
            f"{self._base}/api/v1/relay/hub/register",
            json={"hub_id": self._hub_id},
            headers={"X-API-Key": self._api_key},
        )
        resp.raise_for_status()
        data = resp.json()
        logger.info("Hub %s registered for user %s", data["hub_id"], data["user_id"])
        return data

    # ──── Agent sync ────

    async def sync_agents(
        self, agents: list[dict], *, prune_missing: bool = True,
    ) -> list[dict]:
        """POST /api/v1/relay/hub/{hub_id}/agents/sync"""
        client = await self._get_http_client()
        resp = await client.post(
            f"{self._base}/api/v1/relay/hub/{self._hub_id}/agents/sync",
            json={"agents": agents, "prune_missing": prune_missing},
            headers={"X-API-Key": self._api_key},
        )
        resp.raise_for_status()
        return resp.json().get("synced", [])

    # ──── Publish ────

    async def publish(self, room_id: str, events: list[dict]) -> None:
        """POST /api/v1/relay/hub/{hub_id}/publish with API key auth."""
        client = await self._get_http_client()
        resp = await client.post(
            f"{self._base}/api/v1/relay/hub/{self._hub_id}/publish",
            json={"room_id": room_id, "events": events},
            headers={"X-API-Key": self._api_key},
        )
        resp.raise_for_status()

    # ──── Heartbeat ────

    async def heartbeat(self) -> None:
        """POST /api/v1/relay/hub/{hub_id}/heartbeat — liveness signal."""
        client = await self._get_http_client()
        resp = await client.post(
            f"{self._base}/api/v1/relay/hub/{self._hub_id}/heartbeat",
            headers={"X-API-Key": self._api_key},
        )
        resp.raise_for_status()

    # ──── SSE subscription ────

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        """Subscribe to relay SSE with auto-reconnect.

        Yields all business events (heartbeat handled internally).
        """
        delay = 1
        while not self._should_stop:
            try:
                async for event in self._sse_stream():
                    yield event
                    delay = 1
            except (httpx.HTTPError, httpx.StreamError) as exc:
                if self._should_stop:
                    return
                logger.warning(
                    "SSE connection lost: %s — reconnecting in %ds", exc, delay
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, MAX_RECONNECT_DELAY)
            except asyncio.CancelledError:
                return

    async def _sse_stream(self) -> AsyncIterator[dict[str, Any]]:
        """Single SSE connection session."""
        client = await self._get_sse_client()
        url = f"{self._base}/api/v1/relay/hub/{self._hub_id}/events"
        headers = {"X-API-Key": self._api_key}

        async with aconnect_sse(client, "GET", url, headers=headers) as event_source:
            async for sse in event_source.aiter_sse():
                if self._should_stop:
                    return
                try:
                    data = json.loads(sse.data)
                except (json.JSONDecodeError, TypeError):
                    continue

                event_type = data.get("type")

                if event_type == "connection_ready":
                    logger.info("SSE connection ready")
                    yield {"type": "_connected"}
                    continue

                if event_type == "heartbeat":
                    logger.debug("Heartbeat received")
                    continue

                yield data

    # ──── Status ────

    async def get_status(self) -> dict:
        """GET /api/v1/relay/hub/status"""
        client = await self._get_http_client()
        resp = await client.get(
            f"{self._base}/api/v1/relay/hub/status",
            headers={"X-API-Key": self._api_key},
        )
        resp.raise_for_status()
        return resp.json()
