"""Relay client — SSE subscribe + HTTP publish to hybro.ai relay service.

Maintains a persistent SSE connection to the cloud relay and provides
methods to publish events, register the hub, and sync agents.

Publish uses a two-layer reliability strategy:
  1. Immediate delivery with up to 3 retries (aggressive backoff: 1s, 2s, 4s).
  2. On persistent failure, the event is persisted to a SQLite-backed queue
     which a background drain loop retries with conservative backoff.

The caller (HubDaemon) should never see publish() raise; failures are logged
and either retried immediately or queued to disk.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator

import httpx
from httpx_sse import aconnect_sse

from .publish_queue import CRITICAL_EVENTS, PublishQueue, _coerce_agent_message_id

if TYPE_CHECKING:
    from .config import PublishQueueConfig

logger = logging.getLogger(__name__)

MAX_RECONNECT_DELAY = 60

# Short timeout for regular HTTP calls (register, sync, publish, status)
_HTTP_TIMEOUT = httpx.Timeout(connect=10, read=30, write=10, pool=10)

# SSE connections are long-lived. The backend sends heartbeats every ~30s,
# so a 90s read timeout (3x headroom) detects zombie connections while
# tolerating normal jitter.
_SSE_TIMEOUT = httpx.Timeout(connect=10, read=90, write=10, pool=10)

# Max delay between background retry attempts (1 hour)
_MAX_RETRY_DELAY = 3600


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
        self._queue: PublishQueue | None = None

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

    # ──── Queue management ────

    def init_queue(self, db_path: Path, config: PublishQueueConfig) -> None:
        """Initialise the disk-backed publish queue. Must be called before publish()."""
        self._queue = PublishQueue(db_path, config)
        self._queue.open()

    def close_queue(self) -> None:
        """Flush WAL and close the queue database."""
        if self._queue:
            self._queue.close()
            self._queue = None

    async def get_queue_stats(self) -> dict[str, int]:
        """Return queue depth stats (total / critical / normal)."""
        if self._queue is None:
            return {"total": 0, "critical": 0, "normal": 0}
        return await self._queue.get_stats()

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
        """Publish events with immediate retry, falling back to disk queue.

        Never raises — failures are logged and queued for background retry.
        """
        for event in events:
            event_type = event.get("type", "unknown")
            agent_message_id = _coerce_agent_message_id(event)
            priority = 1 if event_type in CRITICAL_EVENTS else 0

            delivered, error_type = await self._try_publish_with_retry(
                room_id, [event], max_retries=3
            )

            if delivered or error_type == "permanent":
                # Delivered OK, or a 4xx that will never succeed — don't queue.
                continue

            if self._queue is None:
                logger.warning(
                    "Publish queue not initialised — dropping %s event (room=%s)",
                    event_type, room_id[:8],
                )
                continue

            try:
                await self._queue.enqueue(
                    room_id, agent_message_id, event, priority=priority
                )
                logger.warning(
                    "Queued %s event for background retry (room=%s, msg=%s)",
                    event_type, room_id[:8], agent_message_id[:8],
                )
            except Exception:
                logger.exception(
                    "Failed to enqueue %s event — dropping (room=%s)",
                    event_type, room_id[:8],
                )

    async def _try_publish_with_retry(
        self,
        room_id: str,
        events: list[dict],
        *,
        max_retries: int = 3,
    ) -> tuple[bool, str | None]:
        """Attempt HTTP publish with exponential backoff.

        Returns (delivered, error_type):
          (True,  None)        — success
          (False, "permanent") — 4xx error; don't retry or queue
          (False, "transient") — network/5xx error; safe to queue
        """
        last_exc: Exception | None = None
        for attempt in range(max_retries):
            try:
                client = await self._get_http_client()
                resp = await client.post(
                    f"{self._base}/api/v1/relay/hub/{self._hub_id}/publish",
                    json={"room_id": room_id, "events": events},
                    headers={"X-API-Key": self._api_key},
                )
                resp.raise_for_status()
                return (True, None)

            except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout) as exc:
                last_exc = exc
                delay = min(2 ** attempt, 30)   # 1s, 2s, 4s … max 30s
                logger.warning(
                    "Publish attempt %d/%d failed: %s — retrying in %ds",
                    attempt + 1, max_retries, exc, delay,
                )
                await asyncio.sleep(delay)

            except httpx.HTTPStatusError as exc:
                if exc.response.status_code >= 500:
                    last_exc = exc
                    delay = min(2 ** attempt, 30)
                    logger.warning(
                        "Publish got %d — retrying in %ds",
                        exc.response.status_code, delay,
                    )
                    await asyncio.sleep(delay)
                else:
                    # 4xx — permanent; don't retry or queue
                    logger.error(
                        "Publish rejected with %d: %s",
                        exc.response.status_code, exc.response.text,
                    )
                    return (False, "permanent")

        logger.error("Publish failed after %d attempts: %s", max_retries, last_exc)
        return (False, "transient")

    # ──── Background drain (called from HubDaemon) ────

    async def drain_queued_events(self, *, batch_size: int = 20) -> None:
        """Process up to *batch_size* events currently ready for retry."""
        if self._queue is None:
            return
        import json as _json
        import time as _time

        now = _time.time()
        batch = await self._queue.get_ready_events(now, limit=batch_size)

        for event_id, room_id, agent_message_id, event_json, retry_count, max_retries in batch:
            if retry_count >= max_retries:
                logger.warning(
                    "Event %d exceeded max retries (%d) — dropping (msg=%s)",
                    event_id, max_retries, agent_message_id[:8],
                )
                await self._queue.delete(event_id)
                continue

            try:
                event = _json.loads(event_json)
            except (ValueError, TypeError):
                logger.error("Corrupt event %d in queue — dropping", event_id)
                await self._queue.delete(event_id)
                continue

            delivered, error_type = await self._try_publish_with_retry(
                room_id, [event], max_retries=1
            )

            if delivered:
                await self._queue.delete(event_id)
                logger.info(
                    "Delivered queued event %d (msg=%s)", event_id, agent_message_id[:8]
                )
            elif error_type == "permanent":
                logger.warning("Event %d got permanent error — dropping", event_id)
                await self._queue.delete(event_id)
            else:
                # Conservative backoff: 30s, 60s, 120s … max 1h
                delay = min(30 * (2 ** retry_count), _MAX_RETRY_DELAY)
                await self._queue.update_retry(
                    event_id, retry_count + 1, _time.time() + delay, error_type
                )

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
        import json as _json

        client = await self._get_sse_client()
        url = f"{self._base}/api/v1/relay/hub/{self._hub_id}/events"
        headers = {"X-API-Key": self._api_key}

        async with aconnect_sse(client, "GET", url, headers=headers) as event_source:
            async for sse in event_source.aiter_sse():
                if self._should_stop:
                    return
                try:
                    data = _json.loads(sse.data)
                except (ValueError, TypeError):
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
