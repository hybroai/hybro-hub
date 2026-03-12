"""Tests for the internal SSE stream parser."""

import json

import httpx
import pytest
import respx

from hybro_hub._sse import iter_sse_events
from hybro_hub.models import StreamEvent

BASE = "https://api.hybro.ai/api/v1"


def _sse_body(*events: dict) -> str:
    """Build an SSE text body from a sequence of dicts."""
    lines = []
    for ev in events:
        lines.append(f"data: {json.dumps(ev)}\n\n")
    return "".join(lines)


class TestIterSseEvents:
    @respx.mock
    @pytest.mark.asyncio
    async def test_parses_single_event(self):
        sse_text = _sse_body({"type": "message", "text": "hello"})
        respx.post(f"{BASE}/stream").mock(
            return_value=httpx.Response(
                200,
                content=sse_text,
                headers={"content-type": "text/event-stream"},
            )
        )
        client = httpx.AsyncClient(base_url=BASE)
        events = []
        async for ev in iter_sse_events(client, "POST", f"{BASE}/stream"):
            events.append(ev)
        await client.aclose()
        assert len(events) == 1
        assert events[0].data["type"] == "message"
        assert not events[0].is_error

    @respx.mock
    @pytest.mark.asyncio
    async def test_parses_multiple_events(self):
        sse_text = _sse_body(
            {"seq": 1},
            {"seq": 2},
            {"error": "upstream failed"},
        )
        respx.post(f"{BASE}/stream").mock(
            return_value=httpx.Response(
                200,
                content=sse_text,
                headers={"content-type": "text/event-stream"},
            )
        )
        client = httpx.AsyncClient(base_url=BASE)
        events = []
        async for ev in iter_sse_events(client, "POST", f"{BASE}/stream"):
            events.append(ev)
        await client.aclose()

        assert len(events) == 3
        assert events[0].data["seq"] == 1
        assert events[2].is_error

    @respx.mock
    @pytest.mark.asyncio
    async def test_error_event_is_flagged(self):
        sse_text = _sse_body({"error": "something broke"})
        respx.post(f"{BASE}/stream").mock(
            return_value=httpx.Response(
                200,
                content=sse_text,
                headers={"content-type": "text/event-stream"},
            )
        )
        client = httpx.AsyncClient(base_url=BASE)
        async for ev in iter_sse_events(client, "POST", f"{BASE}/stream"):
            assert ev.is_error
            assert ev.data["error"] == "something broke"
        await client.aclose()
