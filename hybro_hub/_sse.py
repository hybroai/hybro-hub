"""Internal SSE stream parser for httpx responses."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
from httpx_sse import aconnect_sse

from hybro_hub.errors import raise_for_status
from hybro_hub.models import StreamEvent


async def iter_sse_events(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    **kwargs,
) -> AsyncIterator[StreamEvent]:
    """Open an SSE connection and yield ``StreamEvent`` objects.

    Uses ``httpx-sse`` under the hood to handle the SSE framing, reconnection
    headers, and newline-delimited ``data:`` fields.
    """
    async with aconnect_sse(client, method, url, **kwargs) as event_source:
        resp = event_source.response
        if not resp.is_success:
            try:
                raw = await resp.aread()
                body = json.loads(raw)
            except Exception:
                body = {}
            raise_for_status(
                resp.status_code,
                body=body,
                headers=dict(resp.headers),
                fallback_text=f"HTTP {resp.status_code}",
            )
        async for sse in event_source.aiter_sse():
            if not sse.data:
                continue
            try:
                payload = json.loads(sse.data)
            except json.JSONDecodeError:
                payload = {"raw": sse.data}
            yield StreamEvent(data=payload)
